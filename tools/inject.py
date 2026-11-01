#!/usr/bin/env python
"""D 段：真机注入 —— 免 root，走 scrcpy-server 的控制通道。

## 为什么是 scrcpy-server

免 root 能用的注入通道只有两条：

    adb shell input tap      每次起一个 JVM，实测 100~300ms 一次 —— 只够冒烟
    scrcpy-server            以 shell 身份 app_process 起进程，反射调
                             InputManager.injectInputEvent，无进程启动开销

本文件实现后者。**只用 `INJECT_TOUCH_EVENT` 一种消息**，不碰视频 / 音频 / 剪贴板。

## 协议依据（scrcpy v5.0.1，逐字段抄自源码，未做任何推测）

    server/src/main/java/com/genymobile/scrcpy/device/DesktopConnection.java
        socket 名 = "scrcpy"（scid == -1 时）或 "scrcpy_%08x"
        tunnel_forward=true 时 server 建 LocalServerSocket 并 accept
        send_dummy_byte 时 accept 后先写 1 字节

    server/.../control/ControlMessageReader.java  parseInjectTouchEvent()
    app/src/control_msg.c                          SC_CONTROL_MSG_TYPE_INJECT_TOUCH_EVENT
        两边字段顺序**完全一致**，共 32 字节定长、大端：

            [0]      u8    type = 2
            [1]      u8    action        0=DOWN 1=UP 2=MOVE
            [2:10]   i64   pointer_id
            [10:14]  i32   x
            [14:18]  i32   y
            [18:20]  u16   screen_width
            [20:22]  u16   screen_height
            [22:24]  u16   pressure      u16 定点数，0xFFFF = 1.0
            [24:28]  i32   action_button 恒 0
            [28:32]  i32   buttons       恒 0

    server/.../control/Controller.java  injectTouch() / getEventPointAndDisplayId()
        ★ displayData == null（即 video=false）时**坐标直通，不做任何映射**：
              } else {
                  // No display, use the raw coordinates
                  point = position.getPoint();
        源码注释原文亦承认：「it is possible to send events without video playback
        when using scrcpy-server alone」⇒ 单跑 server 是被官方支持的用法。

    server/.../control/PointersState.java
        MAX_POINTERS = 10；按 pointer_id 去重；localId 由 server 自分配
        ★ action 的 POINTER_* 语义**由 server 自动补**：
              else if (action == ACTION_DOWN) action = ACTION_POINTER_DOWN | (index << 8);
        所以我们只管发 0/1/2，多指的 index 由 server 算。

    server/.../Server.java + CleanUp.java
        CleanUp 子进程 `unlinkSelf()` 会**主动删掉设备上的 jar** —— 所以每次
        注入前都必须重新 push（这不是谁忘了清理）。

## ★ 一个必须记住的协议事实：**消息里没有时间戳**

    Controller.injectTouch() 第一行是 `long now = SystemClock.uptimeMillis();`
    注入时刻 = server **收到消息的那一刻**，客户端无法指定。

    推论：想让手指在谱面第 t 毫秒落下，唯一办法是让**消息恰好在那一刻到达**。
    所以 D 段的时序精度 = 「PC 侧发送时刻的精度」+「链路延迟的稳定度」，
    两者都必须实测（见 `probe`）。

## 用法

    # 离线自检：编码 / 映射 / 时基，不连手机
    python tools/inject.py selfcheck

    # 单点验证：在 (x, y) 按下再抬起，用来核对坐标是否 1:1
    python tools/inject.py tap --x 1568 --y 720 --hold 120

    # 吞吐 / 抖动探测（P0-D：谱面峰值 1921 events/s）
    python tools/inject.py probe --pattern burst --n 4000
    python tools/inject.py probe --pattern steady --hz 1921 --seconds 3

    # 按 touches.json 播放
    python tools/inject.py play --touches out/touches.json --countdown 5

⚠️ 除 selfcheck 外，其余命令都会**真的往手机注入触摸**。跑之前请确认手机前台
   不在会被误触的地方（probe 建议停在桌面并打开「显示触摸操作」）。
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import struct
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# ---------------------------------------------------------------- 协议常量

#: ControlMessage.TYPE_INJECT_TOUCH_EVENT —— 枚举序第 3 项（从 0 数）
MSG_TYPE_INJECT_TOUCH_EVENT = 2

#: android.view.MotionEvent
ACTION_DOWN = 0
ACTION_UP = 1
ACTION_MOVE = 2

#: 32 字节定长消息（见模块 docstring 的字段表）
TOUCH_FMT = ">BBqiiHHHii"
TOUCH_SIZE = 32
assert struct.calcsize(TOUCH_FMT) == TOUCH_SIZE, "触摸消息长度与协议表不符"

#: pressure 的定点编码：0xFFFF 解码回 1.0（Binary.u16FixedPointToFloat 取 &0xffff）
PRESSURE_MAX = 0xFFFF
PRESSURE_NONE = 0x0000

#: touches.json 的 action 名 → Android action。
#: POINTER_DOWN / POINTER_UP 只是**语义标注**（"已有手指时追加"/"抬起后仍有手指"），
#: scrcpy server 会用指针数自己判定该不该加 ACTION_POINTER_* 前缀 —— 两边自洽，
#: 所以这里一律压回 DOWN / UP / MOVE 三个裸值。
ACTION_NAMES: Dict[str, int] = {
    "DOWN": ACTION_DOWN,
    "POINTER_DOWN": ACTION_DOWN,
    "UP": ACTION_UP,
    "POINTER_UP": ACTION_UP,
    "MOVE": ACTION_MOVE,
}

#: 默认的 scrcpy 发行包（含 scrcpy-server）与 platform-tools
DEFAULT_SERVER_JAR = Path(r"D:\scrcpy-win64-v5.0.1\scrcpy-server")
DEFAULT_ADB = Path(r"D:\platform-tools\adb.exe")

DEVICE_JAR = "/data/local/tmp/scrcpy-server.jar"
SERVER_VERSION = "5.0.1"

#: 用来 `grep` 设备侧 server 进程的标识（进程名其实是 app_process）
SERVER_TAG = "com.genymobile.scrcpy.Server"

#: DesktopConnection.DEVICE_NAME_FIELD_LENGTH —— server 在 control socket 上主动写来的
#: 设备名字段长度（sendDeviceMeta，默认开启）。这是我们判断「server 真的就绪」的唯一
#: 可靠信号：TCP 连得上不代表 server 已经 accept 了。
DEVICE_META_LEN = 64


# ---------------------------------------------------------------- 编码


def build_touch(
    action: int,
    pointer_id: int,
    x: int,
    y: int,
    screen_w: int,
    screen_h: int,
    pressure: int,
) -> bytes:
    """打包一条 32 字节的 INJECT_TOUCH_EVENT。

    action_button / buttons 恒 0 —— 手指不是鼠标，Controller.java 里对非鼠标
    source 会强制 `buttons = 0`，填别的值没有意义。
    """
    return struct.pack(
        TOUCH_FMT,
        MSG_TYPE_INJECT_TOUCH_EVENT,
        action,
        pointer_id,
        int(x),
        int(y),
        int(screen_w),
        int(screen_h),
        int(pressure),
        # action_button
        0,
        # buttons
        0,
    )


def encode_from_event(
    ev: Dict[str, object],
    screen_w: int,
    screen_h: int,
    map_point,
) -> bytes:
    """把 touches.json 的一条事件翻成设备侧的一条消息。

    ⚠️ 坐标先经 `map_point` 做「竖屏坐标 → 真机横屏坐标」的变换（见 ScreenMap）。
    """
    name = str(ev["action"])
    if name not in ACTION_NAMES:
        raise ValueError(f"未知 action: {name!r}")
    action = ACTION_NAMES[name]
    x, y = map_point(float(ev["x"]), float(ev["y"]))
    pressure = PRESSURE_NONE if action == ACTION_UP else PRESSURE_MAX
    return build_touch(action, int(ev["pointer_id"]), x, y, screen_w, screen_h, pressure)


# ---------------------------------------------------------------- 坐标映射

# 真机**底部**屏外（`真机_y > h-1`）是否收进屏内。
#
# True = 收到 `SAFE_BOTTOM_Y`（**不是**最后一行 `h-1`）。这两个数差 80px，而正是这
# 80px 决定游戏是被踢回桌面还是正常打完 —— 2026-10-22 一天踩了两次，沿革如下：
#
# ① 2026-10-22 上午：改成「夹到 `h-1 = 719`」。依据是 `画布_y=1080` ⇒ `真机_y=720`
#    落在可触摸域 `[0,h)` 之外；玩具狂奏曲終焉 IN 一轮 606 条按下有 24 条在 `y=720`，
#    该轮 `Miss=41`，判定点贴底的音符 48 个。
# ② 2026-10-22 19:5x **翻车**（FACTS §126）：一条从 719 起手、随后上移 117~356px 的
#    扫动被 Honor `GestureNavBottom` 判成「上滑回桌面」，logcat 记
#    `input_cancel [GestureNavBottom] reason=input channel stole pointer stream`，
#    紧接着 `wm_set_resumed_activity: UniHomeLauncher` —— 游戏被踢到后台，剩下 24 秒
#    的注入全打在桌面和百度 App 上（用户看到的「乱打」）。夹取只挪了 1px，却跨过了
#    系统的**手势判据**，所以「只动 1px、无害」这个直觉是错的。
#    ⇒ 当时把底部改回 `False`（不夹），代价是贴底音符继续 Miss。
# ③ 2026-10-22 21:2x **现读设备定案**（本方案）：`dumpsys window` 给出
#
#        mandatorySystemGestures frame=[0,640][1610,720]      ← 底部 80px
#
#    是 **mandatory**（游戏无法用 `setSystemGestureExclusionRects` 排除）⇒ 安全上沿
#    只能是 `639`。所以收发点分开：**屏外**（`> h-1`）收到 `SAFE_BOTTOM_Y`，
#    而 `0 ≤ y ≤ h-1`（含 `640~719` 手势区）**原样保留**。
#
# 为什么 `640~719` 不许动（全库实测量化，`tmp/safe_bottom_scan.py`）：那一段有
# **29,406** 条事件（占全库 8.7%），而 `home_gesture_scan.py --dev 1610 720 --gest 640`
# 全库扫出 **0 段**「手势区内按下 + 竖直上滑」⇒ 它们本来就能正常工作。无差别夹到 639
# 只会白白吃掉切向容差（判定区切向半宽 150 画布 px = 100 真机 px，挪 81px 后只剩 19px）。
#
# ⛔ 诊断 / 复现事故时把 `SAFE_BOTTOM_Y` 设成 `h-1`（= 719），**不要**动这个开关。
CLAMP_BOTTOM_EDGE = True

#: 屏外点收进屏内的目标真机 y。**必须 < 640**（本机 mandatory 手势区上沿）。
#:
#: `639` 是「贴着实测边界」，只留 1px 余量。若真机上仍被系统抢流（判据 3/4 会抓到），
#: 降到 620 或 600 —— 每多留 1px，竖直判定线的最坏切向容差就少 1px（100 → 99 → …）。
SAFE_BOTTOM_Y = 639

#: 本机底部 **mandatory** 手势区上沿（`dumpsys window` 2026-10-22 21:2x 现读）：
#:
#:     mandatorySystemGestures frame=[0,640][1610,720]      ← 深 80px
#:
#: 只用来校验 `SAFE_BOTTOM_Y` —— 夹进这条带会被 `GestureNavBottom` 判成 home 上滑
#: （`input channel stole pointer stream` ⇒ `UniHomeLauncher`，见 FACTS §126）。
#: 换机必须重标这个值（`out/device_migration.md` 的第 ③ 项）。
GEST_BOTTOM_Y = 640

# 真机**顶部**越界（`真机_y < 0`）是否夹到 0。必须夹：`qzw_ez` 里有一条 ptr=1 的
# 轨迹 MOVE 到 y=-2…-22 后在屏外 `POINTER_UP`，屏外 UP 同样会被系统丢弃 ⇒ 指针
# 永远抬不起。夹到 0 不引入新风险（顶部实测不做切换手势：顶层 y=0 的 7 条 DOWN
# 覆盖 35 个音符且多判中，见 FACTS §124）。
CLAMP_TOP_EDGE = True


@dataclass(frozen=True)
class ScreenMap:
    """touches.json 的屏幕坐标 → 真机屏幕坐标。

    交付物里的坐标是按**竖屏**（1080x2340，`schedule_chart.py` 的交付格式：玩法区
    等比例适应宽度、垂直居中）算的，真机是**横屏** 3136x1440，两者不能混用。
    中间隔着 1920x1080 的画布：先反解回画布，再按真机的适配规则投出去。

    真机适配规则有两种，这就是 P0-A：

        fit="width"    1920 宽铺满 3136 → 1.6333 倍，画面高 1764 > 1440，**上下各裁 162px**
        fit="height"   1080 高铺满 1440 → 1.3333 倍，画面宽 2560 < 3136，**左右各留 288px 黑边**

    ★ **2026-10-09 真机实测定论：`fit="height"`，因此它是默认值。**

    定论依据：在**打歌界面**逐列测平均亮度，左右在 **x=287 / x=2847** 处**精确**突变，
    内容区宽 **2560 = 1440 × 16/9**，两侧各 288px 黑边 —— 正是 height 适配的预期值。
    （上一轮在标题页/主界面测时判成 width，是因为那两个界面**背景自身有渐晕**，
    把黑边平滑糊掉了；判 letterbox 必须在打歌界面测。）

    `fit="width"` 保留**只为复现历史判断与做对照实验**，正式打歌不要用。
    """

    canvas: Tuple[float, float]
    src_screen: Tuple[float, float]
    dst_screen: Tuple[float, float]
    fit: str = "height"

    # ---- 反解：交付物屏幕坐标 → 画布坐标
    @property
    def _src_scale(self) -> float:
        return self.src_screen[0] / self.canvas[0]

    @property
    def _src_y_off(self) -> float:
        return (self.src_screen[1] - self.canvas[1] * self._src_scale) / 2.0

    def to_canvas(self, x: float, y: float) -> Tuple[float, float]:
        k = self._src_scale
        return x / k, (y - self._src_y_off) / k

    # ---- 正投：画布坐标 → 真机屏幕坐标
    def _dst_scale(self) -> float:
        if self.fit == "height":
            return self.dst_screen[1] / self.canvas[1]
        if self.fit == "stretch":
            return self.dst_screen[0] / self.canvas[0]
        return self.dst_screen[0] / self.canvas[0]

    def from_canvas(self, cx: float, cy: float) -> Tuple[float, float]:
        k = self._dst_scale()
        if self.fit == "height":
            x_off = (self.dst_screen[0] - self.canvas[0] * k) / 2.0
            y_off = 0.0
        elif self.fit == "stretch":
            # 非等比：y 单独缩放
            ky = self.dst_screen[1] / self.canvas[1]
            return cx * k, cy * ky
        else:
            x_off = 0.0
            y_off = (self.dst_screen[1] - self.canvas[1] * k) / 2.0
        return x_off + cx * k, y_off + cy * k

    # ---- 夹进真机像素的半开区间 [0, w) × [0, h)
    def _clamp(self, dx: float, dy: float,
               clamp_bottom: Optional[bool] = None) -> Tuple[int, int, int, int]:
        """夹进可触摸域 `[0, w-1] × [0, h-1]`，并报告夹掉了多少像素。

        **三向各一套规则**（2026-10-22 21:2x 定案，沿革见模块常量 `CLAMP_BOTTOM_EDGE`）：

        | 方向 | 规则 | 理由 |
        |---|---|---|
        | 左 / 右 | 夹到 `[0, w-1]` | 屏外列号不存在的像素 |
        | 顶 | 夹到 `0`（`CLAMP_TOP_EDGE`） | 屏外 `POINTER_UP` 会被丢弃 ⇒ 指针抬不起 |
        | 底 | **只夹屏外**（`iy > h-1`）到 `SAFE_BOTTOM_Y` | `640~719` 是手势区但**本来正常工作**，动了反而吃容差 |

        ⛔ `SAFE_BOTTOM_Y` 必须 `< 640`。夹到屏内最后一行（719）会落进
        `mandatorySystemGestures`，被 `GestureNavBottom` 抢流把游戏踢回桌面（§126）。

        真机像素是一个**有限的整数集合**：宽 w 的屏幕合法列号是 `0..w-1`。
        Android 的 `Rect` 是**半开区间** `[0, w) × [0, h)`，所以
        `画布_y = 1080`（画面底边）× `720/1080` = **`真机 y = 720`** 是个
        **不存在的像素**。

        返回 `(x, y, 夹掉的 x 像素, 夹掉的 y 像素)`。

        ★ 底部只处理**屏外**（`iy > h-1`）：`0 ≤ iy ≤ h-1` 一律原样（含 `640~719`
        这段 mandatory 手势区 —— 那 29,406 条本来就能正常工作，见模块常量处的量化）。
        屏外夹到 `SAFE_BOTTOM_Y`，所以 **`ny` 现在对屏外是非零的**；`640~719` 仍为 0。

        调用方若要知道「上游画到了屏外多少」，判 `unclamped_point() > h-1`；
        要知道「这一条被夹了几 px」，看 `ny`。
        """
        if clamp_bottom is None:
            clamp_bottom = CLAMP_BOTTOM_EDGE
        w, h = int(self.dst_screen[0]), int(self.dst_screen[1])
        ix, iy = int(round(dx)), int(round(dy))
        px = min(max(ix, 0), w - 1)
        py = max(iy, 0) if CLAMP_TOP_EDGE else iy
        if clamp_bottom and iy > h - 1:
            # ⛔ 不许 `min(SAFE_BOTTOM_Y, h-1)` 式静默降级：配置错就该炸，不该悄悄
            #    退回 719 —— 719 在 mandatory 手势区里，会让整轮被踢回桌面（§126）。
            if SAFE_BOTTOM_Y > h - 1:
                raise ValueError(
                    f"SAFE_BOTTOM_Y={SAFE_BOTTOM_Y} 超出可触摸域 [0,{h - 1}]；"
                    f"夹到屏内最后一行会落进系统手势区（§126 事故）")
            if SAFE_BOTTOM_Y >= GEST_BOTTOM_Y:
                raise ValueError(
                    f"SAFE_BOTTOM_Y={SAFE_BOTTOM_Y} 落进 mandatory 手势区 "
                    f"[{GEST_BOTTOM_Y},{h})；会触发 GestureNavBottom 抢流（§126 事故）")
            py = SAFE_BOTTOM_Y
        return px, py, ix - px, iy - py

    def point(self, x: float, y: float) -> Tuple[int, int]:
        """交付物屏幕坐标 → 真机像素坐标。

        **夹取范围：`x ∈ [0, w-1]`、`y ≥ 0`；底部只有屏外（`y > h-1`）收到
        `SAFE_BOTTOM_Y = 639`，`640~719` 原样保留。**

        ⚠️ 2026-10-22 一天之内改了三版，前两版注释都曾与实现不符过，这里是**第五版**，
        沿革全部留在这里（这种东西删掉就等于让人重犯）：

        - 2026-10-15 版注释写「夹一下并保留符号便于排查」，实现里**从来没有 clamp**；
        - 2026-10-16 版改成「原样直通」，实现仍是 `int(round())` —— 这一版是对的；
        - 2026-10-22 上午改成**四边全夹**，理由是 `画布_y=1080` ⇒ `真机_y=720 = h`，
          落在可触摸域 `[0,h)` 之外；玩具狂奏曲終焉 IN 一轮 606 条按下有 24 条在
          `y=720`，该轮 `Miss=41`，判定点贴底的音符 48 个 —— 当时判为强相关；
        - 2026-10-22 19:5x **把底部改回不夹**：同一首、同一份谱、只切「夹不夹」，
          夹取版**凭空造出 7 个底部上滑手势**（不夹 0 条），`GestureNavBottom` 抢流
          把 Phigros 踢到桌面（FACTS §126）。代价是贴底音符继续 Miss；
        - 2026-10-22 21:2x **现行第五版**：现读 `dumpsys window` 发现底部是
          `mandatorySystemGestures frame=[0,640][1610,720]`（**强制**手势区，游戏无法
          用 exclusion rects 排除）⇒ 安全上沿只能是 `639`。于是把「屏外」与
          「手势区内」**分开**处理：屏外收到 `639`（能救回贴底音符），
          `640~719` 一律不动（那 29,406 条本来正常，动了只吃容差）。

        ⇒ 教训：坐标域转换的物理边界确实在这一层收，但**「夹到最近的有效像素」不等于
        「安全」** —— 屏底最后一行属于系统手势区。判据不能只看「有没有出界」，
        要看「会不会跨过系统的判据阈值」。

        诊断 / 审计请用 `unclamped_point()`；要知道有多少条屏外按下请判 `y > h-1`。
        """
        px, py, _nx, _ny = self.point_ex(x, y)
        return px, py

    def point_ex(self, x: float, y: float) -> Tuple[int, int, int, int]:
        """同 `point()`，额外返回被夹掉的像素数 `(x, y, 夹掉x, 夹掉y)`。

        生产注入循环用它累计「夹了几次 / 最大夹几 px」并打印 —— 夹取动作必须留痕，
        否则上游越界会变得更难发现。

        ★ 底部只夹**屏外**（`iy > h-1`）到 `SAFE_BOTTOM_Y`，所以 `ny` 对屏外**非零**
        （720 → 639 夹掉 81）；`640~719` 原样，`ny = 0`。
        想知道「上游把多少条画到了屏外」，判 `unclamped_point() > h-1`。
        """
        cx, cy = self.to_canvas(x, y)
        dx, dy = self.from_canvas(cx, cy)
        return self._clamp(dx, dy)

    def point_canvas(self, cx: float, cy: float) -> Tuple[int, int]:
        """**画布**坐标（1920×1080）→ 真机像素坐标，夹取口径同 `point()`。

        ⚠️ 别拿 `point()` 干这件事：`point()` 的第一步是 `to_canvas()`，
        它期望的是**交付物竖屏（1080×2340）**坐标。2026-10-22 审查发现
        `cmd_tap --from-canvas` 就是踩了这个坑 —— 传画布中心 `(960, 540)` 进去
        得到 `(1303, -387)`（应为 `(805, 360)`）。
        """
        dx, dy = self.from_canvas(cx, cy)
        px, py, _nx, _ny = self._clamp(dx, dy)
        return px, py

    def unclamped_point(self, x: float, y: float) -> Tuple[int, int]:
        """交付物屏幕坐标 → 真机像素坐标，**完全不做夹取**（保留越界信息）。

        用来回答「上游到底把判定点画到哪了」。它与 `point()` 的差出现在三处：
        左右越界、顶部越界、以及**底部屏外**（`y > h-1`，`point()` 会收到 639）。
        `640~719` 两者相同 —— 那一段 `point()` 不碰。
        """
        cx, cy = self.to_canvas(x, y)
        dx, dy = self.from_canvas(cx, cy)
        return int(round(dx)), int(round(dy))

    def describe(self) -> str:
        k = self._dst_scale()
        vis_w = self.canvas[0] * k
        vis_h = self.canvas[1] * k
        notes = []
        if vis_h > self.dst_screen[1] + 0.5:
            cut = (vis_h - self.dst_screen[1]) / 2.0
            notes.append(f"上下各裁 {cut:.0f}px")
        if vis_w > self.dst_screen[0] + 0.5:
            cut = (vis_w - self.dst_screen[0]) / 2.0
            notes.append(f"左右各裁 {cut:.0f}px")
        if vis_w < self.dst_screen[0] - 0.5:
            notes.append(f"左右各留黑边 {(self.dst_screen[0] - vis_w) / 2.0:.0f}px")
        if vis_h < self.dst_screen[1] - 0.5:
            notes.append(f"上下各留黑边 {(self.dst_screen[1] - vis_h) / 2.0:.0f}px")
        tail = ("，" + "，".join(notes)) if notes else "，无裁切无黑边"
        return (f"画布 {self.canvas[0]:g}x{self.canvas[1]:g} → 真机 "
                f"{self.dst_screen[0]:g}x{self.dst_screen[1]:g}"
                f"（fit={self.fit}，{k:.4f} 倍{tail}）")


def tap_point(sm: ScreenMap, x: float, y: float, from_canvas: bool) -> Tuple[int, int]:
    """`inject.py tap` 的坐标口径 —— 抽成函数是为了可测（不连手机也能验）。

    `from_canvas=True` ⇒ `x/y` 是**画布** 1920×1080 坐标，走 `point_canvas()`；
    `from_canvas=False` ⇒ `x/y` 已经是**真机像素**，原样取整。

    ⚠ 2026-10-22 修 bug：原实现走的是 `sm.point()`，而后者的入参是**交付物竖屏
    1080×2340** 坐标 —— `--from-canvas 960 540`（画布中心）会得到 `(1303, -387)`，
    正确值是 `(805, 360)`。
    """
    if from_canvas:
        return sm.point_canvas(x, y)
    return (int(round(x)), int(round(y)))


# ---------------------------------------------------------------- 时基


class Clock:
    """绝对时基：把「谱面毫秒」映射到 `time.perf_counter()` 的秒。"""

    __slots__ = ("t0",)

    def __init__(self, t0: Optional[float] = None):
        self.t0 = time.perf_counter() if t0 is None else t0

    def at(self, ms: float) -> float:
        return self.t0 + ms / 1000.0


def wait_until(target: float, spin: float = 0.0015) -> float:
    """等到 `target`（perf_counter 秒）。

    `time.sleep` 在 Windows 上的实际精度受调度器影响，只在远处用它省 CPU；
    进入 `spin` 窗口后改为自旋，把最后那一两毫秒咬死。
    返回实际落地时刻与目标的偏差（秒，正=晚了）。
    """
    while True:
        rem = target - time.perf_counter()
        if rem <= 0.0:
            break
        if rem > spin:
            time.sleep(rem - spin)
    return time.perf_counter() - target


# ---------------------------------------------------------------- adb 层


class Adb:
    """`adb` 的薄封装。所有命令都走绝对路径，不依赖 PATH。"""

    def __init__(self, exe: Path, serial: Optional[str] = None):
        self.exe = Path(exe)
        if not self.exe.exists():
            raise FileNotFoundError(
                f"找不到 adb：{self.exe}\n用 --adb 指定，或从 platform-tools 解压一份。"
            )
        self.serial = serial

    def _pre(self) -> List[str]:
        cmd = [str(self.exe)]
        if self.serial:
            cmd += ["-s", self.serial]
        return cmd

    def run(self, args: Sequence[str], *, binary: bool = False, timeout: float = 30.0):
        proc = subprocess.run(
            self._pre() + list(args),
            capture_output=True,
            timeout=timeout,
        )
        if proc.returncode != 0:
            err = proc.stderr.decode("utf-8", "replace").strip()
            raise RuntimeError(f"adb {' '.join(args)} 失败：{err or proc.returncode}")
        return proc.stdout if binary else proc.stdout.decode("utf-8", "replace")

    def shell(self, cmd: str, **kw):
        return self.run(["shell", cmd], **kw)

    def shell_quiet(self, cmd: str) -> str:
        """跑一条可能失败的命令，失败返回空串（用于探测性查询）。"""
        try:
            return self.shell(cmd).strip()
        except Exception:
            return ""

    # ---- 设备信息

    def devices(self) -> List[str]:
        out = self.run(["devices"])
        rows = []
        for line in out.splitlines()[1:]:
            line = line.strip()
            if line and "\t" in line:
                serial, state = line.split("\t", 1)
                if state.strip() == "device":
                    rows.append(serial)
        return rows

    def resolve_serial(self) -> str:
        if self.serial:
            return self.serial
        found = self.devices()
        if not found:
            raise RuntimeError("没有已授权的设备（adb devices 空）。检查数据线 / USB 调试授权。")
        if len(found) > 1:
            raise RuntimeError(f"接了多台设备：{found}，用 --serial 指定。")
        self.serial = found[0]
        return found[0]

    def screen_size(self) -> Tuple[int, int, int]:
        """当前显示尺寸与旋转。返回 (width, height, rotation)。

        `wm size` 给的是**自然方向**的物理尺寸，不随旋转变；所以优先从
        `dumpsys window displays` 的 `cur=WxH` 拿「当前方向」的尺寸。
        """
        text = self.shell_quiet("dumpsys window displays")
        for line in text.splitlines():
            if "cur=" in line:
                token = line.split("cur=", 1)[1].split()[0]
                if "x" in token:
                    w, _, h = token.partition("x")
                    rot = 0
                    if " r=" in line:
                        try:
                            rot = int(line.split(" r=", 1)[1].split()[0])
                        except (IndexError, ValueError):
                            rot = 0
                    return int(w), int(h), rot
        text = self.shell_quiet("wm size")
        for line in text.splitlines():
            if "x" in line and ":" in line:
                token = line.split(":", 1)[1].strip()
                w, _, h = token.partition("x")
                return int(w), int(h), 0
        raise RuntimeError("读不到屏幕尺寸（dumpsys window displays / wm size 都没解析出）")

    def ps_server(self) -> List[int]:
        """设备侧还在跑的 scrcpy server 进程 PID。

        ⚠️ 不能写成 `ps -A | grep {SERVER_TAG}`：Android(toybox) 的 `ps -A`
        只输出 NAME 列，而 server 的进程名被截成 `app_process`，因此**永远匹配
        不到**（2026-10-10 在真机上实测：server 确实在跑时该命令返回空）。
        必须用 `-o PID,ARGS` 显式请求参数列。

        grep 自己的 ARGS 里也含 SERVER_TAG（会自匹配），用 `parts[1] == "app_process"`
        之外的行予以排除 —— 真实 server 行的第二列恒为 `app_process`。
        """
        out = self.shell_quiet(f"ps -A -o PID,ARGS | grep {SERVER_TAG}")
        pids = []
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0].isdigit() and parts[1] == "app_process":
                pids.append(int(parts[0]))
        return pids

    def kill_server(self) -> int:
        """杀掉设备侧所有 scrcpy server，返回杀掉的个数。"""
        pids = self.ps_server()
        for pid in pids:
            self.shell_quiet(f"kill -9 {pid}")
        return len(pids)


# ---------------------------------------------------------------- server 生命周期


class ScrcpyServer:
    """设备侧 scrcpy-server 的完整生命周期。

    参数刻意选成「只要控制通道」：

        video=false audio=false       不要画面不要声音（纯注入，带宽占用为零）
        control=true                  唯一需要的
        tunnel_forward=true           server 建 LocalServerSocket，PC 侧用 adb forward 连进去
        send_dummy_byte=false         关掉握手字节，连上就能直接发
        log_level=info                日志够排查，又不至于把管道塞满

    注意 scid 不传 ⇒ 默认 -1 ⇒ socket 名就是 `scrcpy`（DesktopConnection.getSocketName）。
    """

    def __init__(self, adb: Adb, jar: Path, *, verbose: bool = False):
        self.adb = adb
        self.jar = Path(jar)
        self.verbose = verbose
        self.port: Optional[int] = None
        self._proc: Optional[subprocess.Popen] = None
        self.device_name: Optional[str] = None
        self._log_path = PROJECT_ROOT / "tmp" / "inject_server.log"
        self._log = None

    # ---- 启动

    def _pick_port(self) -> int:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return int(s.getsockname()[1])

    def start(self, *, timeout: float = 25.0) -> socket.socket:
        """起 server 并返回已连接的控制 socket（调用方负责 close）。"""
        if not self.jar.exists():
            raise FileNotFoundError(
                f"找不到 scrcpy-server：{self.jar}\n用 --server-jar 指定（它就在 "
                f"scrcpy 发行包根目录，与 scrcpy.exe 同级）。"
            )
        left = self.adb.kill_server()
        if left:
            self._say(f"清掉设备上残留的 server 进程 {left} 个")

        self._say(f"推送 {self.jar.name} → {DEVICE_JAR}")
        self.adb.run(["push", str(self.jar), DEVICE_JAR], timeout=60.0)

        self.port = self._pick_port()
        self.adb.run(["forward", f"tcp:{self.port}", "localabstract:scrcpy"])

        cmd = [
            "CLASSPATH=" + DEVICE_JAR,
            "app_process", "/", SERVER_TAG, SERVER_VERSION,
            "video=false",
            "audio=false",
            "control=true",
            "tunnel_forward=true",
            "send_dummy_byte=false",
            # ⚠ 三元表达式的 else 分支**必须**带上 `log_level=` 前缀。
            # 原写法 `"log_level=info" if self.verbose else "warn"` 在 verbose=False 时
            # 只发出一个**裸 token** `warn`，设备侧 Options.parse 直接抛
            # `Invalid key=value pair: "warn"` ⇒ server exit 1。
            # 生产点火路径一直传 verbose=True 所以没暴露，但默认值一直是坏的。
            "log_level=" + ("info" if self.verbose else "warn"),
        ]
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log = open(self._log_path, "wb")
        # ⚠️ stdin 必须是「不会 EOF 的管道」。
        # 设备侧的 CleanUp 子进程会阻塞在 System.in.read() 上等 EOF；而 adb shell 一旦
        # 读到 stdin EOF 就结束会话，连带把 server 一起杀掉。实测不接管 stdin 时，
        # server 在启动后 ~0.85s 就被 kill（日志特征：只有 "Device:" 和 "Cleaning up"，
        # 没有 "Controller stopped"）。给一个 PIPE 且不写不关，即可让它一直活着。
        self._proc = subprocess.Popen(
            [str(self.adb.exe), "shell", *cmd],
            stdin=subprocess.PIPE,
            stdout=self._log,
            stderr=subprocess.STDOUT,
        )
        self._say(f"server 已起（adb forward tcp:{self.port}）")
        return self.wait_ready(timeout=timeout)

    def wait_ready(self, *, timeout: float = 25.0) -> socket.socket:
        """轮询连接，直到 server 真的就绪（收到它主动发来的 64 字节设备名）。

        ⚠️ **光「TCP 连得上」不等于 server 就绪。** adb forward 在 PC 侧的监听一直
        存在，而设备侧的 LocalServerSocket 可能还没建好（server 还在起 JVM）；此时
        adb 会接受 TCP 连接后**立刻把它关掉**，客户端拿到一条死连接（recv 返回 0 字节，
        后续 sendall 仍会「成功」一小会儿再抛 ConnectionAbortedError）。

        所以必须以「server 主动写来的 64 字节设备名」为就绪判据
        （DesktopConnection.sendDeviceMeta，DEVICE_NAME_FIELD_LENGTH = 64）。
        读不满就丢弃这条连接、换一条新的重试 —— adb 每次收到新连接都会重新尝试转发。
        """
        assert self.port is not None
        deadline = time.perf_counter() + timeout
        last: str = "尚未尝试"
        while time.perf_counter() < deadline:
            if self._proc is not None and self._proc.poll() is not None:
                raise RuntimeError(
                    f"server 进程退出（code={self._proc.returncode}）。日志：{self._log_path}"
                )
            try:
                sock = socket.create_connection(("127.0.0.1", self.port), timeout=1.0)
            except OSError as exc:
                last = f"connect 失败：{exc}"
                time.sleep(0.1)
                continue
            try:
                sock.settimeout(2.0)
                meta = b""
                while len(meta) < DEVICE_META_LEN:
                    chunk = sock.recv(DEVICE_META_LEN - len(meta))
                    if not chunk:
                        break
                    meta += chunk
                if len(meta) < DEVICE_META_LEN:
                    # 死连接（adb 已把它关掉），或 server 还没走到 sendDeviceMeta
                    last = f"握手不足：只收到 {len(meta)}/{DEVICE_META_LEN} 字节"
                    sock.close()
                    time.sleep(0.1)
                    continue
                sock.settimeout(None)
                # TCP_NODELAY 开关（默认**关**）。
                #
                # 理论：32 字节小包逐毫秒下发，Nagle 会在「有未确认小段在途」时把
                # 后续小包攒住 —— 逐包立即上线应该更稳。
                # 实测（2026-10-09，70MF IN 真机）：开着 NODELAY 打出 33 Good（全部
                # Late 80~160ms）+ 3 Miss（S / 99.07%），关掉（即 Nagle 生效）的 r2 是
                # 0 Good + 2 Miss（V / 99.87%）。解释：Nagle 关掉后每条消息独立成段，
                # 8995 个小包逐个过 USB，逐包的传输开销与排队反而比攒包更抖。
                # ⇒ localhost→adb→USB 这条链路上 **Nagle 是盟友不是敌人**。
                # 需要复查时设 PHGA_NODELAY=1 再打开。
                if os.environ.get("PHGA_NODELAY") == "1":
                    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                self.device_name = meta.split(b"\0", 1)[0].decode("utf-8", "replace")
                self._say(f"server 就绪，设备名 {self.device_name!r}")
                return sock
            except OSError as exc:
                last = f"握手异常：{exc}"
                try:
                    sock.close()
                except OSError:
                    pass
                time.sleep(0.1)
        raise RuntimeError(f"连不上 server（{timeout:g}s 超时）：{last}\n日志：{self._log_path}")

    # ---- 收尾

    def stop(self) -> None:
        """断开转发、杀掉设备侧进程、删掉设备上的 jar。"""
        if self.port is not None:
            try:
                self.adb.run(["forward", "--remove", f"tcp:{self.port}"], timeout=10.0)
            except Exception:
                pass
        if self._proc is not None:
            # 先放掉 stdin，让 adb shell 自己收尾；再 terminate 兜底
            if self._proc.stdin is not None:
                try:
                    self._proc.stdin.close()
                except Exception:
                    pass
            if self._proc.poll() is None:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
        try:
            self.adb.kill_server()
            self.adb.shell_quiet(f"rm -f {DEVICE_JAR}")
        except Exception:
            pass
        if self._log is not None:
            self._log.close()
            self._log = None

    def tail_log(self, lines: int = 20) -> str:
        if not self._log_path.exists():
            return ""
        text = self._log_path.read_text("utf-8", "replace").splitlines()
        return "\n".join(text[-lines:])

    def _say(self, msg: str) -> None:
        if self.verbose:
            print(f"  · {msg}")


# ---------------------------------------------------------------- 发送器


class Injector:
    """持有一条控制 socket，往外发触摸消息。

    本类是**全项目唯一的触摸发送出口**，因此按下状态的记账也放在这里：
    `send()` 自动维护 `pressed`，`release_all()` / `close()` 用它做退出前兜底。
    调用方不需要自己维护任何状态。
    """

    def __init__(self, sock: socket.socket, screen_w: int, screen_h: int):
        self.sock = sock
        self.screen_w = screen_w
        self.screen_h = screen_h
        self.sent = 0
        #: 当前仍处于按下状态的指针 → 最后一次已知坐标。
        #: 由唯一出口 send() 维护，供 release_all() 在退出前兜底抬指。
        self.pressed: Dict[int, Tuple[int, int]] = {}

    def send(self, action: int, pointer_id: int, x: int, y: int) -> None:
        pressure = PRESSURE_NONE if action == ACTION_UP else PRESSURE_MAX
        self.sock.sendall(
            build_touch(action, pointer_id, x, y, self.screen_w, self.screen_h, pressure)
        )
        self.sent += 1
        # ACTION_NAMES 已把 POINTER_DOWN/POINTER_UP 压回 DOWN/UP（见该表注释），
        # 所以这里只认三个裸值就够。
        if action == ACTION_DOWN:
            self.pressed[pointer_id] = (x, y)
        elif action == ACTION_MOVE:
            # 只刷新「已按下」的指针 —— MOVE 不该凭空造出一个按下条目
            if pointer_id in self.pressed:
                self.pressed[pointer_id] = (x, y)
        elif action == ACTION_UP:
            self.pressed.pop(pointer_id, None)

    def tap(self, x: int, y: int, hold_ms: int = 100, pointer_id: int = 0) -> None:
        self.send(ACTION_DOWN, pointer_id, x, y)
        if hold_ms > 0:
            time.sleep(hold_ms / 1000.0)
        self.send(ACTION_UP, pointer_id, x, y)

    def release_all(self, settle_s: float = 0.08) -> int:
        """把所有**仍按下的**指针逐个抬起 —— 防「卡指针」污染下一轮。

        背景（2026-10-16 定案）：注入进行中被杀掉，真机会留下永久按下的虚拟指针
        （`dumpsys input` 里 `touchingPointers=[...]` 非空、`downTimeInTarget` 被钉死），
        此后 InputDispatcher 认为虚拟设备一直在触摸 ⇒ **之后所有 DOWN 被吞掉**，
        外部表现就是「怎么点都没反应」。**息屏重亮无效**；清法是**配对 `DOWN`+`UP`**
        （`tools/pointer_guard.py reset`）。「**只有** `adb reboot` 能清」这条已于
        2026-10-16 真机修正：对**未按下**的指针单独发 `UP` 无效（scrcpy 的
        `INJECT_TOUCH_EVENT` 不带 downTime，server 侧自持，跨会话 UP 匹配不上）。
        详见 `.workbuddy/plans/2026-10-16-stuck-pointer-release.md`。

        **覆盖**：所有能执行到这里的中断路径 —— 正常结束 / 异常 / `Ctrl+C`
        （`finally` 会走）。
        **覆盖不到**：`SIGKILL`、二次 `Ctrl+C`、被 OOM 杀 —— 那种走不到这里，
        只能靠 `tools/pointer_guard.py reset` 事后复位（或 `adb reboot`）。

        调用时机**必须早于 `close()` 与 server 停掉**（socket 断了就发不出去）。
        抬完等 `settle_s` 让 server 把 UP 转发给 InputDispatcher，再让调用方关 socket。
        返回实际抬起的指针数。
        """
        stuck = list(self.pressed)
        if not stuck:
            return 0
        for pid in stuck:
            x, y = self.pressed[pid]
            try:
                self.send(ACTION_UP, pid, x, y)
            except Exception:  # noqa: BLE001 —— 这是收尾兜底，任何异常都不该向外扩散
                break          # socket 已断，再发也无意义
        self.pressed.clear()
        time.sleep(settle_s)
        return len(stuck)

    def close(self, release: bool = True) -> None:
        """关掉控制 socket。默认先把仍按下的指针抬起来（release=False 可关掉）。"""
        if release:
            try:
                self.release_all()
            except Exception:  # noqa: BLE001
                pass
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()


# ---------------------------------------------------------------- 场景：selfcheck


def cmd_selfcheck(args: argparse.Namespace) -> int:
    """离线自检 —— 不连手机，验证编码 / 映射 / 时基三件事。"""
    print("=" * 78)
    print("[1] 32 字节消息编码")
    print("=" * 78)
    msg = build_touch(ACTION_DOWN, 0, 1568, 720, 3136, 1440, PRESSURE_MAX)
    fields = struct.unpack(TOUCH_FMT, msg)
    expect = (2, 0, 0, 1568, 720, 3136, 1440, 0xFFFF, 0, 0)
    ok = len(msg) == 32 and fields == expect
    print(f"  长度 {len(msg)}（应 32）")
    print(f"  解包 {fields}")
    print(f"  期望 {expect}")
    print(f"  [{'PASS' if ok else 'FAIL'}] 编码与 v5.0.1 字段表一致")

    print()
    print("=" * 78)
    print("[2] 坐标映射")
    print("=" * 78)
    sm = ScreenMap((1920.0, 1080.0), (1080.0, 2340.0), (3136.0, 1440.0), fit="height")
    print(f"  {sm.describe()}")

    # 画布关键点 → 真机坐标。fit=height 下 k = 1440/1080 = 1.33333、x_off = 288，
    # 期望值是手算的，不是从代码里反抄的。
    print("  画布 → 真机（fit=height，期望值手算）")
    bad = 0
    for (cx, cy), want in (
        ((960.0, 540.0), (1568, 720)),
        ((0.0, 0.0), (288, 0)),
        ((1920.0, 0.0), (2848, 0)),
        ((0.0, 1080.0), (288, 1440)),
        ((1920.0, 1080.0), (2848, 1440)),
    ):
        got = sm.from_canvas(cx, cy)
        hit = abs(got[0] - want[0]) <= 1 and abs(got[1] - want[1]) <= 1
        bad += 0 if hit else 1
        print(f"    ({cx:>6.0f},{cy:>5.0f}) → ({got[0]:>7.1f},{got[1]:>7.1f})"
              f"   期望 {want}  {'ok' if hit else 'MISMATCH'}")
    print(f"  [{'PASS' if not bad else 'FAIL'}] 画布 → 真机映射符合手算")

    # 交付物（竖屏 1080x2340）坐标 → 画布：屏幕中点应落回画布中心
    cx, cy = sm.to_canvas(540.0, 1170.0)
    hit_c = abs(cx - 960.0) <= 0.5 and abs(cy - 540.0) <= 0.5
    print(f"  交付物 → 画布：(540,1170) → ({cx:.2f},{cy:.2f})，期望 (960,540)"
          f"  {'ok' if hit_c else 'MISMATCH'}")
    px, py = sm.point(540.0, 1170.0)
    hit_e = abs(px - 1568) <= 1 and abs(py - 720) <= 1
    print(f"  端到端：(540,1170) → ({px},{py})，期望 (1568,720)"
          f"  {'ok' if hit_e else 'MISMATCH'}")

    # ★ 2026-10-22 新增：越界夹取（本机真实几何 1610x720）。
    #   上面那段用的是历史 3136x1440，屏高 1440，画布底边只会到 y=720 —— 离上界还很远，
    #   所以那条断言**测不出**「画布底边 = 屏高」这个半开区间问题。必须单独用本机尺寸验。
    #   ★ 21:2x 定案：底部**屏外收进 639**（`mandatorySystemGestures` 上沿 640），
    #     顶部**必须夹到 0**（屏外的 POINTER_UP 会被系统丢弃 ⇒ 指针永远抬不起）。
    dev = ScreenMap((1920.0, 1080.0), (1080.0, 2340.0), (1610.0, 720.0), fit="height")
    qx, qy, qnx, qny = dev.point_ex(540.0, 1473.75)      # 画布 (960,1080) = 画面底边
    ok_bottom = (qx, qy, qnx, qny) == (805, SAFE_BOTTOM_Y, 0, 720 - SAFE_BOTTOM_Y)
    print(f"  底部屏外收到 {SAFE_BOTTOM_Y}：画布底边 (960,1080) → ({qx},{qy})，"
          f"夹掉 ({qnx},{qny})，期望 (805,{SAFE_BOTTOM_Y})/(0,{720 - SAFE_BOTTOM_Y})"
          f"  {'ok' if ok_bottom else 'MISMATCH'}")
    # ★ 手势区内（640~719）必须**原样**：这是「别无差别上移」的守门断言。
    #   画布 y = 990 ⇒ 真机 660，落在 `mandatorySystemGestures [640,720)` 里。
    gx, gy, gnx, gny = dev._clamp(*dev.from_canvas(960.0, 990.0))
    ok_gest = (gy, gny) == (660, 0)
    print(f"  手势区内不动：画布 (960,990) → y={gy}，夹掉 {gny}，期望 660/0"
          f"  {'ok' if ok_gest else 'MISMATCH'}")
    # ★ `SAFE_BOTTOM_Y` 配错（≥ 屏高）必须炸，不许静默退回 719。
    ok_guard = False
    _saved = globals().get("SAFE_BOTTOM_Y")
    try:
        globals()["SAFE_BOTTOM_Y"] = 720
        dev.point_canvas(960.0, 1080.0)
    except ValueError:
        ok_guard = True
    finally:
        globals()["SAFE_BOTTOM_Y"] = _saved
    print(f"  SAFE_BOTTOM_Y=720 会抛 ValueError：{ok_guard}")
    tx, ty = dev.point_canvas(960.0, -90.0)              # 画布 y=-90（画面上方屏外）
    ok_top = (tx, ty) == (805, 0)
    print(f"  顶部夹取：画布 (960,-90) → ({tx},{ty})，期望 (805,0)"
          f"  {'ok' if ok_top else 'MISMATCH'}")
    hit_k = ok_bottom and ok_gest and ok_guard and ok_top
    good = (bad == 0) and hit_c and hit_e and hit_k
    print(f"  [{'PASS' if good else 'FAIL'}] 竖屏坐标 → 真机坐标整条链路")

    print()
    print("=" * 78)
    print("[3] 时基 busy-wait")
    print("=" * 78)
    errs = []
    t0 = time.perf_counter() + 0.2
    for i in range(200):
        target = t0 + i * 0.001  # 每 1ms 一次，正是 Hold 持续段的密度
        errs.append(wait_until(target))
    errs.sort()
    p50 = errs[len(errs) // 2]
    p99 = errs[int(len(errs) * 0.99)]
    print(f"  目标间隔 1ms，共 200 次")
    print(f"  偏差 中位 {p50 * 1e6:+.0f}us  p99 {p99 * 1e6:+.0f}us  最大 {errs[-1] * 1e6:+.0f}us")
    # 判定窗 ±80ms，这里给一个很宽的 fail 线：p99 必须在 5ms 内
    good_t = p99 < 0.005
    print(f"  [{'PASS' if good_t else 'FAIL'}] p99 偏差 < 5ms（判定窗 ±80ms）")

    allok = ok and good and good_t
    print()
    print("=" * 78)
    print(f"  {'全部通过' if allok else '有失败项'}")
    print("=" * 78)
    return 0 if allok else 1


# ---------------------------------------------------------------- 场景：tap


def cmd_tap(args: argparse.Namespace) -> int:
    adb = Adb(Path(args.adb))
    serial = adb.resolve_serial()
    w, h, rot = adb.screen_size()
    print(f"设备 {serial}  屏幕 {w}x{h}  旋转 {rot}")

    x, y = args.x, args.y
    if args.from_canvas:
        sm = ScreenMap((1920.0, 1080.0), (1080.0, 2340.0), (float(w), float(h)), fit=args.fit)
        x, y = tap_point(sm, args.x, args.y, True)
        print(f"  {sm.describe()}")

    print(f"注入坐标 ({x}, {y})，按住 {args.hold}ms")
    server = ScrcpyServer(adb, Path(args.server_jar), verbose=True)
    try:
        sock = server.start()
        inj = Injector(sock, w, h)
        inj.tap(int(x), int(y), hold_ms=args.hold)
        time.sleep(0.3)
        inj.close()
        print(f"  已发送 {inj.sent} 条消息")
    finally:
        server.stop()
    print("完成。若手机上没看到触点，先打开开发者选项里的「显示触摸操作」。")
    return 0


# ---------------------------------------------------------------- 场景：probe


def cmd_probe(args: argparse.Namespace) -> int:
    """吞吐 / 抖动探测（P0-D）。

    谱面实测峰值 **1921 events/s**（第 23s / 104s，构成是 4 个 Hold 各逐毫秒发 MOVE）。
    这里只测**发送侧**：从 PC 把消息推进 socket 的速度，以及按固定频率发时的抖动。

    ⚠️ 设备侧的生效延迟（Android 输入队列 + 进程调度）这条链**测不到回包**，
    需要另用视觉手段标定（P0-C 的 offset_sec）。本命令不假装能测它。
    """
    adb = Adb(Path(args.adb))
    serial = adb.resolve_serial()
    w, h, rot = adb.screen_size()
    print(f"设备 {serial}  屏幕 {w}x{h}  旋转 {rot}")

    server = ScrcpyServer(adb, Path(args.server_jar), verbose=True)
    try:
        sock = server.start()
        inj = Injector(sock, w, h)
        cx, cy = w // 2, h // 2

        if args.pattern == "burst":
            _probe_burst(inj, cx, cy, args)
        else:
            _probe_steady(inj, cx, cy, args)

        inj.close()
    finally:
        server.stop()
        print(f"  日志尾部：\n{server.tail_log(8)}")
    return 0


def _probe_burst(inj: Injector, cx: int, cy: int, args: argparse.Namespace) -> None:
    n = args.n
    print(f"\nburst：连续 {n} 条 MOVE，中间不做任何等待")
    inj.send(ACTION_DOWN, 0, cx, cy)
    t0 = time.perf_counter()
    for i in range(n):
        # 坐标在小范围抖动，避免被设备侧当作「无变化」优化掉
        inj.send(ACTION_MOVE, 0, cx + (i % 7), cy + (i % 5))
    dt = time.perf_counter() - t0
    inj.send(ACTION_UP, 0, cx, cy)
    rate = n / dt if dt > 0 else float("inf")
    print(f"  用时 {dt * 1000:.2f}ms  ⇒ {rate:,.0f} events/s")
    need = 1921
    print(f"  [{'PASS' if rate >= need else 'FAIL'}] 达到谱面峰值 {need} events/s"
          f"（余量 {rate / need:.1f} 倍）")


def _probe_steady(inj: Injector, cx: int, cy: int, args: argparse.Namespace) -> None:
    hz = args.hz
    seconds = args.seconds
    total = int(hz * seconds)
    period = 1.0 / hz
    print(f"\nsteady：{hz} Hz 持续 {seconds}s（共 {total} 条）")
    inj.send(ACTION_DOWN, 0, cx, cy)
    t0 = time.perf_counter() + 0.05
    errs: List[float] = []
    for i in range(total):
        errs.append(wait_until(t0 + i * period))
        inj.send(ACTION_MOVE, 0, cx + (i % 3), cy)
    dt = time.perf_counter() - t0
    inj.send(ACTION_UP, 0, cx, cy)
    errs.sort()
    p50 = errs[len(errs) // 2]
    p99 = errs[int(len(errs) * 0.99)]
    print(f"  实际用时 {dt * 1000:.1f}ms vs 目标 {total * period * 1000:.1f}ms")
    print(f"  单条偏差 中位 {p50 * 1e6:+.0f}us  p99 {p99 * 1e6:+.0f}us  "
          f"最大 {errs[-1] * 1e6:+.0f}us")
    print(f"  实测速率 {total / dt:,.0f} events/s")
    print(f"  [{'PASS' if p99 < 0.010 else 'FAIL'}] p99 抖动 < 10ms")


# ---------------------------------------------------------------- 场景：play


def load_touches(path: Path) -> Tuple[Dict[str, object], List[Dict[str, object]]]:
    doc = json.loads(path.read_text(encoding="utf-8"))
    events = doc.get("events")
    if not isinstance(events, list) or not events:
        raise ValueError(f"{path} 里没有 events 段（是不是用了 --no-events？）")
    return doc, events


def cmd_play(args: argparse.Namespace) -> int:
    doc, events = load_touches(Path(args.touches))
    canvas = (float(doc["canvas"]["width"]), float(doc["canvas"]["height"]))
    src = (float(doc["screen"]["width"]), float(doc["screen"]["height"]))

    adb = Adb(Path(args.adb))
    serial = adb.resolve_serial()
    w, h, rot = adb.screen_size()
    # ★ 横屏守卫：`screen_size()` 读的是**当前方向**尺寸。Phigros 是横屏游戏，
    # 若此刻游戏不在前台（桌面是竖屏 1440x3136），坐标会整体按竖屏映射 →
    # 全错，白跑一首还往账号里留成绩。这种错**静默发生**，所以在这里硬拦。
    if w < h:
        raise SystemExit(
            f"设备当前是竖屏 {w}x{h}（旋转 {rot}）—— Phigros 是横屏游戏，"
            f"竖屏下坐标映射会整体错位。\n"
            f"请先把游戏切到打歌界面（横屏 3136x1440）再跑。"
        )
    sm = ScreenMap(canvas, src, (float(w), float(h)), fit=args.fit)
    print(f"设备 {serial}  屏幕 {w}x{h}  旋转 {rot}")
    print(f"  {sm.describe()}")
    print(f"  事件 {len(events)} 条（{doc.get('chart')}）")

    if args.limit:
        events = events[: args.limit]
        print(f"  只播前 {len(events)} 条（--limit）")

    if args.dry_run:
        print("\ndry-run：只打印前 12 条映射结果，不连手机")
        for ev in events[:12]:
            x, y, nx, ny = sm.point_ex(float(ev["x"]), float(ev["y"]))
            flag = f"  ⚠夹掉({nx},{ny})" if (nx or ny) else ""
            # `ny = iy - py` ⇒ 原始 y = py + ny，不必再算一次坐标（同一份数据两视图）
            if ny > 0:
                flag += f"  ⚠底部屏外 y={y + ny} 已收到 {SAFE_BOTTOM_Y}"
            print(f"    t={float(ev['t_ms']):>9.3f}ms  {str(ev['action']):<12} "
                  f"ptr{ev['pointer_id']}  ({ev['x']:.1f},{ev['y']:.1f}) → ({x},{y}){flag}")
        return 0

    server = ScrcpyServer(adb, Path(args.server_jar), verbose=True)
    try:
        sock = server.start()
        inj = Injector(sock, w, h)

        for remain in range(args.countdown, 0, -1):
            print(f"  {remain}…", end="", flush=True)
            time.sleep(1.0)
        print("  GO")

        clock = Clock()
        over = 0.0
        clamp_n = 0
        clamp_max = 0
        bottom_n = 0          # 底部**屏外**条数（已收到 SAFE_BOTTOM_Y）
        h_px = int(sm.dst_screen[1])
        for ev in events:
            over = max(over, wait_until(clock.at(float(ev["t_ms"]))))
            px, py, nx, ny = sm.point_ex(float(ev["x"]), float(ev["y"]))
            if nx or ny:
                clamp_n += 1
                clamp_max = max(clamp_max, abs(nx), abs(ny))
            # ★ 判 `ny > 0` 而**不是** `py > h_px - 1`：底部夹取之后 `py` 恒 ≤ 639，
            #   用夹后坐标判会让这个计数**静默变成 0**（2026-10-22 改夹取时差点踩）。
            #   `ny = iy - py`，底部屏外时为正、顶部屏外时为负 ⇒ `ny > 0` 恰好等价
            #   于「底部屏外」，且与夹取用的是同一份数据，不会两次转换不一致。
            if ny > 0:
                bottom_n += 1
            inj.send(
                ACTION_NAMES[str(ev["action"])],
                int(ev["pointer_id"]),
                px,
                py,
            )
        print(f"  播放完成 {inj.sent} 条，最大滞后 {over * 1000:.2f}ms")
        if clamp_n:
            print(f"  ⚠ 坐标越界收进屏内 {clamp_n} 条（最大 {clamp_max}px）"
                  f" —— 上游把判定点画到了真机像素域之外 `[0,{int(sm.dst_screen[0])-1}]×"
                  f"[0,{int(sm.dst_screen[1])-1}]`")
        if bottom_n:
            print(f"  ⚠ 底部屏外 {bottom_n} 条已收到 y={SAFE_BOTTOM_Y}（手势区上沿 640 之外）："
                  f"竖直偏移不参与判定，但「竖直判定线」会吃切向容差 ⇒ 盯 Early/Late")
        inj.close()
    finally:
        server.stop()
    return 0


# ---------------------------------------------------------------- CLI


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="inject.py",
        description="Phigros 自动打歌 D 段：免 root 真机注入（scrcpy-server 控制通道）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--adb", default=str(DEFAULT_ADB), help="adb 可执行文件路径")
    ap.add_argument("--server-jar", default=str(DEFAULT_SERVER_JAR),
                    help="scrcpy-server 路径（scrcpy 发行包根目录里那个无扩展名的文件）")
    ap.add_argument("--serial", default=None, help="多设备时指定序列号")

    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("selfcheck", help="离线自检（不连手机）")

    p_tap = sub.add_parser("tap", help="注入单点，验证通道与坐标")
    p_tap.add_argument("--x", type=float, required=True)
    p_tap.add_argument("--y", type=float, required=True)
    p_tap.add_argument("--hold", type=int, default=120, help="按住时长 ms（默认 120）")
    p_tap.add_argument("--from-canvas", action="store_true",
                       help="--x/--y 按 1920x1080 画布坐标解释")
    p_tap.add_argument("--fit", choices=("width", "height", "stretch"), default="height",
                       help="适配方式；默认 height（2026-10-09 真机实测），width 仅用于对照")

    p_probe = sub.add_parser("probe", help="吞吐 / 抖动探测（P0-D）")
    p_probe.add_argument("--pattern", choices=("burst", "steady"), default="burst")
    p_probe.add_argument("--n", type=int, default=4000, help="burst 的消息条数")
    p_probe.add_argument("--hz", type=float, default=1921.0, help="steady 的目标频率")
    p_probe.add_argument("--seconds", type=float, default=3.0, help="steady 的持续时间")

    p_play = sub.add_parser("play", help="按 touches.json 播放")
    p_play.add_argument("--touches", default=str(PROJECT_ROOT / "out" / "touches.json"))
    p_play.add_argument("--fit", choices=("width", "height", "stretch"), default="height",
                        help="适配方式；默认 height（2026-10-09 真机实测），width 仅用于对照")
    p_play.add_argument("--countdown", type=int, default=5, help="倒数秒数（默认 5）")
    p_play.add_argument("--limit", type=int, default=0, help="只播前 N 条（0=全部）")
    p_play.add_argument("--dry-run", action="store_true", help="只打印映射，不注入")

    return ap


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    if args.cmd == "selfcheck":
        return cmd_selfcheck(args)

    # 其余命令都要连设备：统一在这里把 serial 塞进去
    if args.serial:
        os.environ.setdefault("ANDROID_SERIAL", args.serial)

    if args.cmd == "tap":
        return cmd_tap(args)
    if args.cmd == "probe":
        return cmd_probe(args)
    if args.cmd == "play":
        return cmd_play(args)
    raise AssertionError(args.cmd)


if __name__ == "__main__":
    raise SystemExit(main())
