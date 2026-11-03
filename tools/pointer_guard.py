#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""卡指针守卫 —— 检查 / 复位设备上残留的虚拟指针。

背景（2026-10-16 真机定案，FACTS §66 / §73）
--------------------------------------------
注入进行中进程被**强杀**（`kill -9` / Windows 的 `TerminateProcess` / OOM）⇒ 走不到
`finally` ⇒ 设备侧留下永久按下的虚拟指针。此时 `dumpsys input` 的 `TouchStatesByDisplay`
不再显示 `<no displays touched>`，而是

    TouchStatesByDisplay:
      DeviceId(-1):[touchingPointers=[Pointer(id=0, FINGER)],
                    downTimeInTarget=19768754000000, hoveringPointers=[], ...]

此后 InputDispatcher 认为虚拟设备仍在触摸 ⇒ **之后所有 DOWN 被吞掉**。外部表现：
「怎么点都没反应」/ 点火后 ▶ 没生效 / 整轮 NO-HIT。这正是 10-15 / 10-16 两次事故。

清法（实测，修正了 §66 的旧结论；2026-10-16 矩阵复验，4 次独立诱导）
  · 对**未按下**的指针发 `UP`  ⇒ **无效**（TouchState 一字未变）。
    机制：scrcpy 的 `INJECT_TOUCH_EVENT` **不携带 downTime**，由 server 侧自己维护；
    新会话发的 UP 与残留的 downTime **不匹配** ⇒ 被 InputDispatcher 丢弃。
  · 逐指「按一对、抬一对」  ⇒ 单指残留 `[0]` 能清；★ **三指 `[0,1,2]` 清不掉**。
  · ★ **镜像残留手势**：把残留里的**全部** pointer 依次**按下**，**再**依次抬起
    ⇒ 单指与三指**都一次清干净**；**抬指顺序无关**（逆序 `2,1,0` 与同序 `0,1,2` 都成功）。
  ⇒ **不必 `adb reboot`**。§66 的「息屏重亮无效」仍成立，被放宽的只是「**只有** reboot 能清」。
  ★ 机制读法：InputDispatcher 要的是**一个完整的多指手势**；若干个单指手势**不构成**它，
    所以「一只一只地按抬」不会把整组残留消掉。

两个子命令
----------
  check   只读。打印触摸状态。退出码：0 干净 / 3 有残留 / 4 读不到（不确定）。
  reset   起 scrcpy server → 分级复位 → 复查。`--xy` **必填**（防手滑点到屏幕）。

复位分级（每级做完都复查，并记录是**哪一级**清的）
  A  **镜像残留手势**一次（按下全部残留 id → 逆序抬起）  ← 已验证的唯一有效配方
  B  同一手法**再来一次**                              ← 防时序抖动
  C  仍脏 ⇒ 本工具不动手，打印 `adb reboot` 指引

模块级函数（给点火路径用，不是 CLI）
------------------------------------
  `preflight(adb, *, xy_hint, out) -> bool` —— 点火前的**只读**预检：只跑一条
  `dumpsys input`，**不抛异常**（fail-open）、**不复位**，检出残留就打印警告 + 复位命令。
  `tmp/full_run.py` 在进入点火动作之前调它（方案 §8.3 / FACTS §76）。

⚠️ 解析陷阱（写死在这里，免得后人重踩）
---------------------------------------
**干净的 dump 里也有 `DeviceId(-1)`**：

    Down times by device: {DeviceId(-1): 19770278000000}

这一行任何时候都在（实测），所以**不能**只按 `DeviceId(` 判残留，否则恒定误报。
真判据 = 同一行上 `DeviceId(` **与** `touchingPointers=` 同时出现。

安全
----
  · 全程不碰 Phigros、不产生任何成绩、不读写 g_state.json。
  · `reset` 会在 `--xy` 处产生一次**真实点击** ⇒ 让设备停在「点到也没关系」的界面
    （桌面 / 空白处），并避开系统手势区。手势区**按设备查表**（`GESTURE_ZONES`）：
    RMX5200（3136×1440）底边 `y≥1312`；ALT-AN00（1610×720）底边 `y≥640`、左侧 `x≤104`、
    右侧 `x≥1580`、顶部 `y≤74`。**别按比例推** —— 两机实测比例差得远（右 3.8% vs 1.9%）。
  · 任何路径都会在 `finally` 里抬指（复用 `Injector.release_all`），不会自己造出残留。

用法
----
  .venv/Scripts/python.exe tools/pointer_guard.py check
  .venv/Scripts/python.exe tools/pointer_guard.py reset --xy 720,200
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import inject

NO_TOUCHED = "<no displays touched>"
_PTR_ID_RE = re.compile(r"id=(-?\d+)")
_ANY_INT_RE = re.compile(r"-?\d+")
_DOWN_TIME_RE = re.compile(r"downTimeInTarget=(\d+)")

EXIT_CLEAN = 0
EXIT_DIRTY = 3
EXIT_UNKNOWN = 4


class DeviceTouch:
    """一个虚拟设备（`DeviceId(-1)`）上仍被认为按着的指针。"""

    __slots__ = ("device_id", "pointers", "down_time")

    def __init__(self, device_id: int, pointers: Tuple[int, ...],
                 down_time: Optional[int]):
        self.device_id = device_id
        self.pointers = pointers
        self.down_time = down_time

    def __repr__(self) -> str:                                    # pragma: no cover
        return (f"DeviceTouch({self.device_id}, {list(self.pointers)}, "
                f"{self.down_time})")


class TouchState:
    """一次 `dumpsys input` 的触摸状态快照。

    · `devices`  解析到的**仍在按下**的设备记录（空 = 干净）。
    · `saw_no_touched`  文本里出现过 `<no displays touched>`。
    · `unknown`  = 既没残留、也没看到那个标记 ⇒ 格式变了 / 命令失败
      ⇒ **不能当干净**（fail-safe，宁可多问一句）。
    """

    __slots__ = ("devices", "saw_no_touched", "raw")

    def __init__(self, devices: List[DeviceTouch], saw_no_touched: bool, raw: str = ""):
        self.devices = devices
        self.saw_no_touched = saw_no_touched
        self.raw = raw

    @property
    def pointers(self) -> Tuple[int, ...]:
        """所有设备上仍按着的指针 id（去重升序）。"""
        ids = {p for d in self.devices for p in d.pointers}
        return tuple(sorted(ids))

    @property
    def clean(self) -> bool:
        return not self.devices

    @property
    def unknown(self) -> bool:
        return self.clean and not self.saw_no_touched

    def describe(self) -> str:
        if self.devices:
            parts = []
            for d in self.devices:
                dt = "?" if d.down_time is None else str(d.down_time)
                parts.append(f"DeviceId({d.device_id}): touchingPointers="
                             f"{list(d.pointers)} downTimeInTarget={dt}")
            return "；".join(parts)
        if self.unknown:
            return "（读不到触摸状态字段 ⇒ 无法判定）"
        return f"{NO_TOUCHED}（干净）"


def parse_touch_state(text: str) -> TouchState:
    """从 `dumpsys input` 全文抽出「是否仍有指针按着」。

    判据见模块 docstring 的「解析陷阱」：`DeviceId(` 与 `touchingPointers=`
    **必须同现**。两种真实写法都支持：

        DeviceId(-1):[touchingPointers=[Pointer(id=0, FINGER)], downTimeInTarget=…]
        DeviceId(-1):[touchingPointers=[0,1,2]]
    """
    devices: List[DeviceTouch] = []
    seen: set = set()          # 真机里同一条状态会在 dump 里重复出现（实测 8 次，
                               # InputDispatcher 多个 section 各打一遍）⇒ 去重，
                               # 否则 describe() 会把 8 份一模一样的东西拼给用户看。
    for line in text.splitlines():
        if "DeviceId(" not in line or "touchingPointers=" not in line:
            continue
        # 贪婪到行末最后一个 `]`（TouchState 里还有若干个嵌套的 []）
        for dev_id, body in re.findall(r"DeviceId\((-?\d+)\):\[(.+)\]", line):
            m = re.search(r"touchingPointers=\[(.*?)\]", body)
            if m is None:
                continue
            ids = {int(x) for x in _PTR_ID_RE.findall(m.group(1))}
            if not ids:                      # 退化写法：裸 id 列表
                ids = {int(x) for x in _ANY_INT_RE.findall(m.group(1))}
            if not ids:                      # `touchingPointers=[]` ⇒ 没有按着的
                continue
            dt = _DOWN_TIME_RE.search(body)
            rec = (int(dev_id), tuple(sorted(ids)), int(dt.group(1)) if dt else None)
            if rec in seen:
                continue
            seen.add(rec)
            devices.append(DeviceTouch(*rec))
    return TouchState(devices, NO_TOUCHED in text, text)


def read_touch_state(adb: "inject.Adb") -> TouchState:
    """跑一次 `dumpsys input` 并解析。命令失败由调用方处理。"""
    return parse_touch_state(adb.shell("dumpsys input"))


# ---------------------------------------------------------------- 点火前预检


#: 系统手势区**侵入深度**（像素），键 = 横屏 `(宽, 高)`。
#: 来源 = 设备侧 `adb shell dumpsys window | grep -i gesture` 里 `InsetsSource` 的 `frame=`。
#: ★ **只能按设备查表，不能按比例推**：两机实测比例差得远
#:   （右侧 120/3136 = 3.8% vs 30/1610 = 1.9%；底边 128/1440 = 8.9% vs 80/720 = 11.1%）。
GESTURE_ZONES: dict[Tuple[int, int], dict[str, int]] = {
    (3136, 1440): {"left": 257, "right": 120, "top": 160, "bottom": 128},   # RMX5200（旧机）
    (1610, 720): {"left": 104, "right": 30, "top": 74, "bottom": 80},       # ALT-AN00（2026-10-18 实测）
}

#: 未登记机型回退用的**保守比例**（取两机实测的较大者再放宽）。
GESTURE_RATIO_FALLBACK = {"left": 0.10, "right": 0.05, "top": 0.15, "bottom": 0.15}

#: 复位提示的**兜底**坐标（读不到屏幕尺寸时用）= 新机竖屏 720×1610 的正中偏上。
#: ⚠ 不要把它当命令行示例直接抄 —— 生产路径由 `_hint_for(adb)` 按真实分辨率算。
PREFLIGHT_XY_HINT = (360, 563)


def gesture_zone(w: int, h: int) -> dict[str, int]:
    """查该分辨率的系统手势区深度；未登记机型按保守比例回退（宁可多避一点）。"""
    z = GESTURE_ZONES.get((int(w), int(h)))
    if z is not None:
        return dict(z)
    w, h = int(w), int(h)
    return {"left": int(w * GESTURE_RATIO_FALLBACK["left"]),
            "right": int(w * GESTURE_RATIO_FALLBACK["right"]),
            "top": int(h * GESTURE_RATIO_FALLBACK["top"]),
            "bottom": int(h * GESTURE_RATIO_FALLBACK["bottom"])}


def safe_reset_xy(w: int, h: int) -> Tuple[int, int]:
    """算一个「必然不落在手势区、且必然在屏内」的复位点。

    规则：**水平正中** + **垂直 35%**。

    * 水平正中：两机的左右手势区都贴着边缘（≤6.5% / ≥1.9%），正中必然在两者之外；
    * 垂直 35%：顶/底手势区最深 11.1%，35% 距两边都留 >20% 屏高余量。

    ★ 为什么必须钳制到 `[1, w-2] × [1, h-2]`：旧常量 `(720, 200)` 是给 1440 宽的旧机竖屏的，
    换到新机竖屏（720 宽）后 `x = 720` **越界** —— `reset` 会把点击发到屏幕外，
    表现为「复位了但没反应」，且没有任何报错。2026-10-18 审查发现。
    """
    w, h = int(w), int(h)
    return (max(1, min(w - 2, w // 2)), max(1, min(h - 2, int(h * 0.35))))


def _hint_for(adb: "inject.Adb") -> Tuple[int, int, int, int]:
    """按设备**当前**屏幕尺寸算复位提示坐标。读不到就按新机竖屏兜底（fail-open）。"""
    try:
        w, h, _rot = adb.screen_size()
        w, h = int(w), int(h)
    except Exception:                                             # noqa: BLE001
        w, h = 720, 1610
    x, y = safe_reset_xy(w, h)
    return x, y, w, h


def preflight(adb: "inject.Adb", *, xy_hint: Tuple[int, int] | None = None,
              out=None) -> bool:
    """点火前的**只读**预检：设备上是否还残留按住的虚拟指针。

    返回 **True = 可以继续**（干净 / 读不到 / 解析不确定）；**False = 检出残留**。
    调用方自己决定要不要因此中止（默认**不中止**）。

    ★ 三条硬约束（方案 §8.3，2026-10-16）：
      1. **fail-open**：本函数**永不**抛异常 —— adb 失败、格式变了、解析器有 bug，
         都只打印一行「无法判定」然后放行。理由：预检自己有出错的可能，不该让一个
         guard 把正常轮次拦下。
      2. **不复位**：自动复位要在就绪界面点一下屏幕，误触风险（点到 ▶ / 返回）大于
         它省下的一步。这里只**打印命令**，让操作者手动跑（10 秒）。
      3. **只读**：只跑 `dumpsys input`，不产生任何点击、不碰成绩。

    为什么值得加这一步：残留的表现是「怎么点都没反应」（之后所有 DOWN 被吞，FACTS §66），
    操作者当场就能对上号 —— 不必再去怀疑谱子 / 脚本 / 注入计划（那正是 10-16 一上午的弯路）。
    """
    emit = print if out is None else out
    try:
        st = read_touch_state(adb)
    except Exception as exc:                                      # noqa: BLE001
        emit(f"  ⚑ 卡指针预检：**读不到**（{type(exc).__name__}: {exc}）"
             f" ⇒ 本轮不判（fail-open）")
        return True
    if st.unknown:
        emit("  ⚑ 卡指针预检：dump 里找不到触摸状态字段 ⇒ **无法判定**（fail-open）")
        return True
    if st.clean:
        emit(f"  ⚑ 卡指针预检：干净（{NO_TOUCHED}）")
        return True

    # 提示坐标**按本机分辨率现算** —— 旧版是固定 (720,200)，换到 720 宽的新机竖屏后越界。
    hx, hy, w, h = _hint_for(adb)
    x, y = xy_hint if xy_hint is not None else (hx, hy)
    z = gesture_zone(w, h)
    bar = "  " + "═" * 68
    emit(bar)
    emit("  ★★ 卡指针警告：设备上仍有按住的虚拟指针 ⇒ 之后**所有 DOWN 都会被吞**")
    emit(f"     残留：{st.describe()}")
    emit("     现在点火 ⇒ ▶ 大概率不生效 / 整轮 NO-HIT。**先复位**：")
    emit(f"       ① 让设备停在点到也没关系的地方（建议先回桌面，别停在选歌页）")
    emit(f"       ② .venv/Scripts/python.exe tools/pointer_guard.py reset --xy {x},{y}")
    emit(f"          （{x},{y} 由本机 {w}×{h} 算出：水平正中 / 垂直 35%；reset 会在该点"
         f"产生**一次真实点击**。实测手势区：左 {z['left']} / 右 {z['right']} / "
         f"顶 {z['top']} / 底 {z['bottom']} px —— 别自己往边上挪）")
    emit("     ★ 复位是**镜像残留手势**，不必 adb reboot。依据：FACTS §75")
    emit(bar)
    return False


# ---------------------------------------------------------------- 复位


def _mirror(inj: "inject.Injector", ids: Sequence[int], x: int, y: int, *,
            gap_s: float = 0.08, hold_s: float = 0.30) -> None:
    """镜像一个多指手势：先把 `ids` **全部**按下，再把它们**逆序**抬起。

    ★ 2026-10-16 真机矩阵定案（`tmp/probe_stuck_clear_matrix.log`，4 次独立诱导）：
    多指残留**只能**这样清 —— 逐指「按一对、抬一对」在三指 `[0,1,2]` 上实测
    `downTimeInTarget` **一字未变**。InputDispatcher 要的是**一个完整的多指手势**，
    若干单指手势**不构成**它。（抬指顺序无关：逆序与同序都成功。）
    """
    for pid in ids:
        inj.send(inject.ACTION_DOWN, pid, x, y)
        time.sleep(gap_s)
    time.sleep(hold_s)
    for pid in reversed(list(ids)):
        inj.send(inject.ACTION_UP, pid, x, y)
        time.sleep(gap_s)


def reset(adb: "inject.Adb", jar: Path, x: int, y: int, *, hold_s: float = 0.30,
          settle_s: float = 0.5, verbose: bool = True) -> Tuple[str, TouchState, TouchState]:
    """分级复位，返回 `(清掉它的步骤, 复位前, 复位后)`；步骤 ∈ {"-", "A", "B", "none"}。

    A = 镜像残留手势**一次**成功；B = 同一手法**重试**后成功（时序抖动）；none = 都没清掉。
    """
    say = (lambda m: print(m)) if verbose else (lambda m: None)

    before = read_touch_state(adb)
    if before.clean:
        say(f"  · 起始就是干净的（{before.describe()}）⇒ reset 是 no-op，不起 server。")
        return "-", before, before

    say(f"  · 起始残留：{before.describe()}")
    server = inject.ScrcpyServer(adb, jar, verbose=verbose)
    inj = None
    cleared_by = "none"
    try:
        sock = server.start()
        inj = inject.Injector(sock, *adb.screen_size()[:2])

        cur = before
        for step in ("A", "B"):
            ids = list(cur.pointers)
            if not ids:                       # A 之后已经空了（理论上走不到，防御性）
                break
            say(f"  · [{step}] 镜像残留手势：按下 {ids} → 抬起 {list(reversed(ids))}"
                f"（点位 {x},{y}）")
            _mirror(inj, ids, x, y, hold_s=hold_s)
            time.sleep(settle_s)
            cur = read_touch_state(adb)
            if cur.clean:
                cleared_by = step
                break
    finally:
        if inj is not None:
            # 顺序照抄 tmp/full_run.py 收尾：抬指 → 关 socket → 停 server
            try:
                inj.release_all()
            except Exception:                                     # noqa: BLE001
                pass
            try:
                inj.close(release=False)                          # 上面已抬过
            except Exception:                                     # noqa: BLE001
                pass
        server.stop()

    time.sleep(0.4)
    after = read_touch_state(adb)
    return cleared_by, before, after


# ---------------------------------------------------------------- CLI


def cmd_check(args: argparse.Namespace) -> int:
    adb = inject.Adb(args.adb, args.serial)
    print(f"设备 {adb.resolve_serial()}")
    try:
        st = read_touch_state(adb)
    except Exception as exc:                                      # noqa: BLE001
        print(f"★ 读不到触摸状态：{exc!r}")
        return EXIT_UNKNOWN
    print(f"  触摸状态： {st.describe()}")
    if st.clean and not st.unknown:
        print("  ⇒ 干净：不存在卡指针，可以正常点火。")
        return EXIT_CLEAN
    if st.unknown:
        print("  ⇒ 不确定：dumpsys 里既没有残留也没有 `<no displays touched>`。")
        print("     （多半是 Android 版本改了字段名 —— 先人工 `dumpsys input | grep -A2 TouchStates`）")
        return EXIT_UNKNOWN
    print(f"  ⇒ ★ 有残留指针 {list(st.pointers)}：此后所有 DOWN 都会被吞掉。")
    print(f"     复位： {Path(__file__).name} reset --xy 720,200")
    return EXIT_DIRTY


def cmd_reset(args: argparse.Namespace) -> int:
    adb = inject.Adb(args.adb, args.serial)
    x, y = (int(v) for v in args.xy.split(","))
    print(f"设备 {adb.resolve_serial()}  点位 ({x}, {y})")
    cleared_by, before, after = reset(adb, args.server_jar, x, y,
                                     hold_s=args.hold_ms / 1000.0,
                                     settle_s=args.settle_ms / 1000.0)
    print(f"\n[结果] 起始 {before.describe()}")
    print(f"       收尾 {after.describe()}   清掉它的是：{cleared_by}")
    if after.clean and not after.unknown:
        print(f"  ⇒ PASS（步骤 {cleared_by}）—— 设备已干净，不必 adb reboot。")
        return EXIT_CLEAN
    if after.unknown:
        print("  ⇒ 不确定（见上文）。")
        return EXIT_UNKNOWN
    print("  ⇒ ★ 仍残留 ⇒ `adb reboot`（§66：息屏重亮无效）。")
    return EXIT_DIRTY


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pointer_guard",
        description="卡指针守卫：检查 / 复位设备上残留的虚拟指针（不必 adb reboot）。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--adb", type=Path, default=inject.DEFAULT_ADB, help="adb 路径")
    p.add_argument("--serial", default=None, help="设备序列号（只有一台时可省）")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("check", help="只读：打印触摸状态（0 干净 / 3 有残留 / 4 不确定）")
    c.set_defaults(func=cmd_check)

    r = sub.add_parser("reset", help="分级复位残留指针（会在 --xy 处产生一次真实点击）")
    r.add_argument("--xy", required=True, metavar="X,Y",
                   help="复位注入点，必填。竖屏 1440x3136 建议 `720,200`（顶部中央，避开 y>=1312 的手势区）")
    r.add_argument("--server-jar", type=Path, default=inject.DEFAULT_SERVER_JAR,
                   help="scrcpy-server 路径")
    r.add_argument("--hold-ms", "--down-ms", dest="hold_ms", type=int, default=300,
                   help="镜像手势「全部按下之后」的保持时间（默认 300ms；旧名 --down-ms 保留）")
    r.add_argument("--settle-ms", type=int, default=500, help="每级之间等待设备反应（默认 500ms）")
    r.set_defaults(func=cmd_reset)
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
