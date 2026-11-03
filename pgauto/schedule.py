"""C 段：音符清单 → 指针规划 → 标准触摸事件序列（touches.json）。

问题
----
一条谱子有几百到几千个音符，而 Android 同一时刻最多只接受 **10 个触点**
（`MotionEvent` 的 pointer id 0..9）。C 段要回答的是：

    **能不能用 ≤10 个指针把所有音符都打到？** 能，就把它展开成可注入的事件流。

判定机制（它决定了模型的形状）
------------------------------
不靠猜。三条**独立**来源（作者自述 `QA.md` + `algo_base.py` + `algo2.py`）确认
Phigros 的判定区是一个**矩形**：

    高 ⊥ 判定线、**长度不限**；  宽 ∥ 判定线、等于 note 宽

    Tap   (1)   判定区内出现 **DOWN**
    Hold  (3)   判定区内有指针**持续按住**
    Drag  (2)   判定区内**有指针存在**即可 —— **不需要 DOWN**
    Flick (4)   判定区内出现**快速位移**

⇒ 一根手指可以**同时**为多个音符服务：只要它们在**沿判定线方向**的投影够近，
   **法向离多远都无所谓**（甚至可以在屏幕外）。这就是「8 个 hold + 一堆 drag
   只要 2 个指针」的原理。

模型
----
1. **帧**：谱面离散成 1ms 一帧的「待服务音符」列表。
   - Tap / Drag        → 自己时刻的那一帧
   - Flick             → `t - flick_lead_ms` 那一帧（滑动在 [t-30, t+30] 内完成）
   - Hold              → `t` 的一帧记 **TAP**（起点要 DOWN），`t+1 … t+hold` 的
     **每一毫秒**记一个 **DRAG**（持续按住）。后者自动享受 Drag 的复用机制，
     于是长按不再独占一根手指。**每毫秒的位置取「判定线上的判定点」**，随判定线
     移动 / 旋转而变 —— 不是「按住不动」。
2. **指针池**：固定 ≤10 个指针。
   - 需要 DOWN 的（Tap / Hold 起点 / Flick 起点）：优先**复用已用过的**指针（就近），
     没有才开新的 —— 这样指针总数被压到最小。
   - 只需要「存在」的（Drag / Hold 持续）：先找**已经有手指落在判定区内**的指针
     ⇒ **一个事件都不发**；找不到才派一个并下发 DOWN 或 MOVE。
3. **事件定名**：按「同刻先释放、再按下、后移动」排序后扫描，得到
   DOWN / POINTER_DOWN / MOVE / POINTER_UP / UP。

⚠️ **M3 的 `move_speed`（手指移动耗时）在本模型里被彻底删除。**
   注入的触点位置由事件本身直接设定，不存在「手指要花时间飞过去」——
   那个参数只对真人手指有意义。M3 靠把它调到 50（横穿屏 38ms）才把指针数压进 10，
   现在证明那 27 个「超限谱面」是建模误差，不是谱面难（见 M4/M5 全量普查）。

相对 phisap 的两处修正
----------------------
1. **同一帧内允许复用刚分配的指针。** phisap 的 `_find_available_pointers` 跳过
   `age <= 0`（本意是别抢正在 flick 的指针），副作用是同一毫秒的 N 个音符吃掉 N 个
   指针。全量普查发现 `DESTRUCTION321/EZ` 有 16 个音符落在**同一像素、同一毫秒**
   （分属 4 条判定线，此刻恰好都穿过该点）——物理上 1 个手指就够。改成只跳过
   `age < 0`（真正在 flick 的）后，全量 1064 个谱面：分配失败 **11 → 0**，
   最大指针数 **10 → 9**。
2. **释放时刻不得早于该指针的最后一条事件。** phisap 把 UP 回填到「绑定时刻 + 1ms」，
   若该指针之后还发过 keepalive MOVE（长按），就会得到「UP 之后还有 MOVE」的非法流。
   这里改成 `UP = clamp(最后一条事件的时刻, 绑定时刻 + 1, 当前时刻)`。

几何从哪来
----------
长按的跟随路径、屏幕外音符的投影，都需要**判定线**的移动 / 旋转曲线。它跟着
`notes.json` 的 `lines` 段一起走（由 B 段原样带出），所以 C 段的输入仍然是**一个文件**，
不依赖谱面本体。缺 `lines` 时退化为 `StaticGeometry`（判定线不动、长按不跟随），
并在输出的 `geometry` 字段里标明 `static`。
"""

from __future__ import annotations

import json
import math
import os
from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Set, Tuple

NOTE_TAP = 1
NOTE_DRAG = 2
NOTE_HOLD = 3
NOTE_FLICK = 4

NOTE_NAMES: Dict[int, str] = {1: "Tap", 2: "Drag", 3: "Hold", 4: "Flick"}

#: 逻辑画布（Phigros 玩法区，16:9）。距离、判定区阈值与事件坐标都在这个空间里算。
CANVAS: Tuple[float, float] = (1920.0, 1080.0)

#: 画面宽高比。判定线的 y 事件值要先除以它才进入世界坐标（见 `chart.line_state`）。
DEFAULT_ASPECT_RATIO: float = 16.0 / 9.0

#: Android 同一时刻的指针数硬上限
MAX_POINTERS: int = 10

#: Flick 提前量：`t - 30ms` 起手，滑动在 [t-30, t+30] 内完成
FLICK_LEAD_MS: int = 30
FLICK_SPAN_MS: int = 60

#: ★★★ 2026-10-23：Flick 扫动 MOVE 的**采样步长**（毫秒）。
#:
#: 背景：`_serve_flick` 原本 `for delta in range(flick_span_ms)` 逐毫秒发一条 MOVE
#: （60ms 扫动 = 60 条，间隔恒 1.0ms）。这是 phisap 的口径，在**参考实现**里没问题，
#: 但本项目的注入通道是 `PC → adb → scrcpy-server`，实测 server 消费节拍 ≈ 60 条/s
#: （`lag_ms` 只出现 16.9 / 33.7 两簇，差 16.84ms ≈ 1 帧 @59.4fps）。
#:
#: 真机代价（2026-10-23 bounded_in IN，1286 音符 / 6446 事件 / **44.1 事件/s**）：
#:   MOVE 4764 条占 74%，相邻事件间隔中位 **1.0ms**、85.5% 的间隔 < 16.9ms
#:   ⇒ 通道被打满，**其余音符的 DOWN 被量化到 16.9/33.7ms 网格**
#:   ⇒ 结算 14 Good **全部 Late、零 Early**（μ=+28.1ms、σ=24.0ms）。
#: 对照：所有 φ 轮的事件率都 ≤7.7/s（hckwaya 7.7 / cristalisia 3.8 / credits 2.5）
#:   ⇒ 「以前不会、现在会」= 第一次打高事件率的 IN 谱，撞上通道容量墙。
#:
#: 为什么可以降采样：游戏是 **60fps**，`InputDispatcher` 每帧（16.67ms）才采一次。
#:   60ms 的扫动里设备最多采到 4 个点 ⇒ 60 条里有 56 条**永远采不到**。
#:   步长 4ms 时设备采到的点位与 1ms 完全等价（同帧内取最近的一条，
#:   位置差 = 2R·(step/2)/SPAN ≈ 3px，R=90 时不可辨）。
#:
#: 保留**首尾两条**（`delta=0` 与 `delta=SPAN-1`）：首条钉「归位→扫动起点」的
#: 恒定位移 R（见 `_serve_flick` docstring 第 2 条），末条钉扫动终点 ——
#: 少了末条会让终点差 `(step-1)/SPAN·R`，Flick 可能扫不过判定区。
#:
#: 效果（bounded_in）：MOVE 4764 → ~1200，总事件 6446 → ~2900，事件率 44.1 → ~20/s，
#:   回到通道容量（60/s）的 1/3。
FLICK_SWEEP_STRIDE_MS: int = 4

#: ★ 格 A（2026-10-15）：扫动相对「归位 MOVE」的**整体延后**量（毫秒）。
#:
#: 问题：复用已按下指针时，`_serve_flick` 先补一条「归位 MOVE 到音符中心」，
#: 它与扫动首帧原本**同刻**（phisap `algo2` 口径，两条都进 `events[now]`）。
#: 单个 Flick 起手 ⇒ 该毫秒 2 条 MOVE（同指针）；**两个 Flick 同刻起手**
#: ⇒ 该毫秒挤进 **4 条 MOVE**（2 指针 × 2 条）。
#:
#: 真机代价（10-14 D 实验第 6 枪）：goodrage IN 时间对准**完美**（E0/L0）
#: 仍丢 2 触点，三重证据钉到 `#32&#33` 同刻 Flick 双押；全曲 4 处同形态
#: **只丢 1 处 ⇒ 概率 ≈ 1/4**（合 §2.19「同刻同步对 = 概率性故障点」）。
#:
#: 修法：把整段扫动后移 1ms（归位留 `now`），该毫秒只剩 2 条「归位」（分属不同
#: 指针）⇒ 密度回到常规。代价仅扫动中点 t0 → t0+1（≪ ±80ms 判定窗），
#: 且归位与扫动首帧仍只隔 1ms（位移仍「快速」，不影响 Flick 识别）。
#:
#: ⚠️ 改此值时**必须**同步 `_serve_flick` 的**复用判据**（`sw[0] != self.now + 本值`），
#: 否则「同刻**同位**双押共用一次扫动」会退化 ⇒ 各自开扫动 ⇒ 又变 4 条同刻。
#: 全库离线自检（38 谱面）：同刻同指针 ≥2 MOVE 组 2343 → 0，事件数/复用计数逐首不变。
FLICK_SWEEP_DELAY_MS: int = 1

#: 同刻同位 Flick 的策略开关（A/B 与回归用）：
#:   默认 False ⇒ 「各扫各的」，但**至少要有 2 个空闲指针**才这么做（否则共用）。
#:   `PGAUTO_FLICK_SHARE=1` ⇒ 强制总是共用（= 2026-10-25 之前的老行为）。
#: 为什么不是"有 1 个空闲就独立扫"：`祈-我ら神祖…/AT` 用满 10 指、22 组同刻 Flick，
#: 只留一根空闲就去抢，会把**正在长按**的手指抢走 ⇒ 真机症状"长 hold 按住了又松开"（2026-10-25 22:5x）。
_FLICK_SHARE: dict[str, bool] = {
    "on": os.environ.get("PGAUTO_FLICK_SHARE", "").strip() == "1"
}

#: Flick 沿判定线**法向**来回摩擦的半幅（占画布宽的比例）。
#: 曾用 30px/1280px（与 phisap 一致）。2026-10-10 真机取证（Gungnir g6 中 / g7、g8 丢，
#: 断点 = 全谱唯一天井线 Flick@84500）表明 2R/SPAN ≈ 1.5 画布 px/ms 的扫速**贴着
#: 游戏 Flick 识别阈值**，成败随设备当轮状态掷骰子。半径翻倍把扫速提到 2 倍留出余量；
#: 扫动沿法向（竖直）位移、不消耗切向判定区宽度。
#:
#: ⛔ 这里原本写着「越界由 `clamp_into_screen` 兜底」—— **那是假的**（2026-10-22 证伪）：
#: `_serve_flick` 从来没调过任何 clamp，2026-10-22 全量扫描实测 **8516 条事件跑出画布、
#: 分布在 84 个谱面**，最大越界正好 90.0px = 本半径。真正生效的兜底是
#: `fit_flick_radius()`（把半径压到屏幕内，不是逐点夹取 —— 逐点夹会让扫速在边界上掉到 0）。
FLICK_RADIUS_RATIO: float = 60.0 / 1280.0

#: Flick 扫动半径的**下限**（画布 px）。半径被屏幕边界压到低于它 ⇒ 扫速
#: `2R/SPAN` 掉到 0.33 px/ms，已低于 Flick 识别阈值 ⇒ 发 `warnings`，不许静默。
#: 取 10 而不是 30：30 是自己历史上的可用下限，10 只是「明显扫不动」的告警线。
FLICK_RADIUS_MIN_PX: float = 10.0

#: Flick 扫动可用的**底部安全上沿**（占画布高的比例）。`1.0` = 用满画布（旧行为）。
#:
#: 为什么需要它（FACTS §130 / plan `2026-10-22-safe-bottom`）：画布高 `1080` 对应真机
#: `y = 720`，而真机屏高 720 的半开区间是 `[0,720)` ⇒ **画布底边是屏外**。`fit_flick_sweep`
#: 按 `h = 1080` 推中心，就会把扫动末端正好压在 `1080` 上，那段 MOVE 落在屏外。
#:
#: 更糟的是下面这一点（本机实测，`dumpsys window` 2026-10-22 21:2x）：
#:
#:     mandatorySystemGestures frame=[0,640][1610,720]      ← 底部 80px 是**强制**手势区
#:
#: 是 `mandatory` 而非普通 `systemGestures` ⇒ 游戏没法用 `setSystemGestureExclusionRects`
#: 排除它。夹到 719（屏内最后一行）会落进这条带里，被 `GestureNavBottom` 抢流把游戏
#: 踢回桌面（§126 事故）。所以安全上沿是 **639**。
#:
#: 生产值 = `639 / 720 = 0.8875` ⇒ 画布 `958.5`。此时 `py=1080, R=90` 的 Flick 中心被
#: 推到画布 `868.5`，扫动覆盖 `778.5~958.5` = 真机 `519~639`：**位移仍是完整 120 真机 px**。
#:
#: ⛔ 别改成「缩半径」来达成同样目的：缩半径会把「部分越界但有完整位移」变成
#: 「不出界但零位移」，Flick 必漏（2026-10-22 首版踩过，197 处 R=0）。
#:
#: ⚠ 2026-10-22 21:5x 修正：这里原来写 `= 1.0`（旧行为），而注释写着「生产值 0.8875」
#: —— 默认值一关，`play.py` / `schedule_chart.py` 走默认路径就**根本没修**，
#: 只有手工传参才生效。这类「文档说已修、实际没修」是最贵的 bug 类型（教训 16）。
#: 现改为**默认即生产值**；要做 A/B 对照显式传 `1.0`（`tmp/score_diff.py` 就是这么做的）。
FLICK_BOTTOM_SAFE_RATIO: float = 639.0 / 720.0      # ≈ 0.8875 ⇒ 画布 958.5

#: 指针复用的**切向**阈值（占画布宽的比例）。判定区的宽度约等于 note 宽，
#: 沿判定线方向落在这个范围内即视为「手指在判定区内」。
#:
#: ⚠️ 这个值**没有权威来源**（2026-10-09 记：「最优可退位置距判定点 151.1px，
#: 与阈值 150 只差 1.1px，常量来源尚未查证」）。它是「**已经定住**的手指还能不能算
#: 在判定区内」的容差，**不该**被用来回答「两根手指在同一毫秒能不能共用一根」——
#: 后者见 `CHORD_COINCIDENT_RATIO`。
TANGENT_THRESHOLD_RATIO: float = 100.0 / 1280.0

#: Drag 的「在判定区内」必须**连法向一起算**（2026-10-25 真机定案）。
#:
#: 背景：`tangent_offset()` 只看切向、法向不计 —— 对 Tap 没问题，但对 **Drag** 会出事：
#: 实测 `Ajourneytothemoonlight/IN` 末段 4 个 Drag，服务手指切向差 9px（< presence 20）
#: 但法向差 18px，2D 距离**正好 20px** ⇒ 判"在区内"⇒ 不补 MOVE ⇒ 游戏那边手指不在音符上
#: ⇒ 0 Good / 0 Bad 的 Miss（连击正好断在末段）。
#: ⛔ 别用"全局收紧 presence"来修：实测 20→5 让事件 3067→4182，会把正在服务长按/别的
#:    音符的手指挪走，中段首个 Miss 从第 1193 个提前到第 934 个。
DRAG_PRESENCE_2D_PX = 5.0

#: 同一毫秒里「一个音符已经**把某根手指挪走**之后，这根手指还能不能再顶第二个音符」
#: 的**重合**半径（占画布宽的比例，默认 = 切向阈值的 1/10 ≈ 15 画布 px）。
#:
#: 为什么必须比 `TANGENT_THRESHOLD_RATIO` 严得多（2026-10-21 实测）：
#: 那个 150px 是给「手指早就定在那儿」用的容差；而一根**本帧刚被别的音符挪过去**的
#: 手指，它的位置是**已经被别的音符消费掉的**。再拿 150px 去顶第二个音符，等于宣称
#: 「一根手指同时服务两个相距 125px 的点」—— 物理上不成立。
#:
#: burn/IN 实测（`#336`/`#337`，t=81638.5）：`#335`→346、`#336`→720、`#337`→595。
#: `#336` 把 pid0 从 1204 挪到 720（发 1 条 MOVE），`#337` 随即以「切向偏移 125 <
#: 150」判定 pid0 可复用、**一条事件都不发** ⇒ 595 那个点整毫秒无人。这就是连续两轮
#: `M6`/`M5` 里那 5~6 个 Miss 的直接来源（`out/miss_anatomy.md`）。
#:
#: 反过来**必须**保留「同点共用」：`DESTRUCTION321/EZ` 的 t=75439 有 **12 个同像素
#: Drag**（超过 Android 10 指针硬上限），共用一根手指是唯一解，不能判成拒绝。
#: 15px 的重合半径足以容纳「同一点」而挡住「相距一个判定区宽度的两点」。
CHORD_COINCIDENT_FACTOR: float = 0.1
CHORD_COINCIDENT_RATIO: float = TANGENT_THRESHOLD_RATIO * CHORD_COINCIDENT_FACTOR

#: 「**已经按着**的手指离判定点多远还算在判定区内」的上限（占画布宽的比例）。
#:
#: 默认 = `TANGENT_THRESHOLD_RATIO`（保持不变）。把它调小 ⇒ 手指必须**贴着**音符，
#: 否则 planner 补一条 MOVE 把手指挪上去。为什么要这个开关：
#:
#: `TANGENT_THRESHOLD_RATIO` 的 150px **没有权威来源**（见上）。而它一旦偏大，
#: 有两类「静默漂移」会被放过 —— 都只会在真机上表现为 Miss，离线自洽检查看不见：
#:
#: ① **Drag 停在离音符 120px 处不动**：burn/IN 有 10 个 Drag 的最近手指在
#:    121.9~126.5px（t=81081~82145 两簇）。同一批里 burn 连续两轮 M6/M5，
#:    而 destruction321/IN（M0 ×3）最远只到 149.6px 但 p99 只有 108 —— 用「一个
#:    固定半宽」无法同时解释两者，所以到底是命中还是 Miss，只能上机测。
#: ② **长按的手指被判定点甩开**（更严重）：`hold_keep` 只在偏移超阈值时才补 MOVE，
#:    于是手指可以「原地不动、让音符滑走」最多 150px。burn/IN 实测：
#:    `#705`/`#706`（t=133.2s，各 3446ms）最大偏移 **150.0/149.9px**，>100px 各有
#:    1027/880 个毫秒点；另有 8 个长按在 75~86px。而 destruction321/IN 的 49 个长按
#:    **全部 < 75px**（最大 74.7），EZ 的 17 个全部 < 36px（最大 35.2）。
#:    长按「中途离开判定区」在 Phigros 里就是断按 ⇒ 计 Miss，且**不产生 Early/Late**
#:    —— 与 burn 三轮 `E0/L0 + Bad 0 + M5~M6` 的指纹完全吻合。
#:
#: 调小到 ~20px 的代价只有 MOVE 变多（每个音符每秒最多 1 条/指针），不产生
#: 「同毫秒同指针多条 MOVE」（10-14 Flick 事故那种密度问题）。
PRESENCE_REUSE_RATIO: Optional[float] = None

#: 收尾 UP（`withdraw` 抬起仍按着的指针）相对最后服务时刻的**推迟量**（毫秒）。
#:
#: 为什么需要（2026-10-11 ap1 取证）：末尾 8 连 Drag 的判定时刻 140448.43，
#: 而 withdraw 的 UP 落在 140450 —— 边距仅 **1.57ms**。游戏判定按帧走（16.8ms 一帧），
#: UP 后的下一帧再查「判定区内有无指针」时手指已抬 ⇒ 压在边上的 Drag 直接 Miss。
#: ap1 真机 5 Miss 全部集中于此（Early 0 / Late 0 / 0 Good，时基零责任）。
#: 依据永久事实「Hold 晚松无惩罚 / Drag 只需判定区内有指针存在」，
#: 把收尾 UP 推迟一个判定窗（100ms）是零代价的：它只影响 end-of-stream 的
#: 最后一次抬起，该指针之后没有任何事件，不存在副作用面。
TAIL_RELEASE_PAD_MS: int = 100

#: 一次按下（DOWN→UP）的**最短按压时长**（毫秒）。
#:
#: 为什么需要：`_release` 会把复用指针的 UP **回填**到 `released_after+1`，旧口径下
#: 大多数按压只有 1ms——60Hz 触屏每 16.7ms 采样一次，1ms 的触摸可能整帧落空、
#: 根本没被注册。真机取证（Gungnir g6~g8）：全谱 936 个按压段中位 1ms、750 个
#: ≤20ms，绝大多数能命中但**每一个都是小概率丢失点**——各轮 0~2 个随机 Miss
#: 正好是 750 次掷骰子的样子（g6 丢 115.75s Tap、g7/g8 丢 85.25s Tap，均为 1ms 短按）。
#:
#: 人手点击自然时长 80~120ms，30ms（跨 2 个采样帧）远在游戏正常输入范围内，
#: 不会引入双判（判定发生在 DOWN 时刻，UP 只结束触摸）。设 0 可完全恢复旧口径。
#:
#: ★ 2026-10-10 m2 回退：真机决定性对照（70MF IN 16，同条件唯一变量 = touches 文件）
#: 证明 30ms 强制短按在高密度谱面（峰值并发 2、953 次指针复用）上挤爆指针池调度——
#: A+B 版 F 74.88%/80M vs 旧版 V 99.81%/3M。移出游戏空间后 1ms 短按无辜（g8 解剖
#: 是游戏空间修复前的结论，被混杂因素污染）。故回退为 0（旧口径），A（Flick 半径 60）保留。
MIN_PRESS_MS: int = 0

#: Tap/Hold 起点「闲着时」的最短按下时长（毫秒）。**不影响选针规则**（那仍是 `MIN_PRESS_MS`）。
#:
#: 2026-10-25 23:1x：`祈-我ら神祖…/AT` 出现 tap 偶发纯 Miss（0G0B），而载荷里那颗 tap 是
#: `DOWN → 1 ms → UP`。若游戏按帧批量处理触点，1 ms 的按下会与抬起落在同一批里 ⇒ 等于没按。
#: 取 25 ms（60 fps 下 1.5 帧、120 fps 下 3 帧），且**只在手指不急着被复用时**才延长。
TAP_MIN_PRESS_MS: int = 25

#: 长按保持（hold_keep）的**最小发事件间隔**（ms）。
#: 2026-10-26：`玩具狂奏曲終焉 AT` 30~40 s 有 21 个长按并发 ⇒ 逐毫秒 keep 把注入流打饱和，
#: 那段 244 帧晚 30~60 ms ⇒ 106 个音符全判 Late。取 16 ms ≈ 一帧，肉眼/判定都分辨不出。
HOLD_KEEP_MIN_MS: int = 16
#: 允许的"只更新模型、不发事件"的最大漂移（画布 px）；超过就必须发，避免轨迹被压平。
HOLD_KEEP_DRIFT_PX: float = 8.0


#: 「下发时刻」的搜索半径（毫秒）。
#:
#: 事件只能盖在**整数毫秒**上，而几何必须取**那一毫秒**的判定点；判定线又会在事件
#: 边界**瞬变** —— 于是存在这样的音符：它自己那一毫秒的判定点被甩到屏幕外，而相邻的
#: 毫秒还在屏幕里。此时下发时刻要在判定窗内另挑一个（见 `build_frames.pick_head_time`）。
#:
#: Tap / Drag / Hold 的判定窗都是 ±80ms（Perfect 区），窗口内任一整数毫秒都不掉评级，
#: 所以半径取 80。Flick 不参与扩散（半径给 0），理由见 `pick_head_time`。
#:
#: ⚠️ 该值与 `tools/replay_score.py::PERFECT_MS` 是**同一条规则的两个副本**，刻意不共享
#: —— C 段与验收器要保持互相独立，共享一个常量会让「验收抓到实现」变成「自己验自己」。
HEAD_WINDOW_MS: int = 80

#: 判定线几何事件的插值精度：事件首尾相接铺满时间轴，取「包含 t 的那一条」即可。
_MISS = float("nan")

#: 事件定位的容差（谱面时间单位）。
#:
#: **音符的时刻恰好落在事件边界上是常态** —— 事件的 startTime/endTime 与音符的 time
#: 都是整数单位，所以边界重合不可避免。而 `notes.json` 把 `t_ms` 序列化成 4 位小数，
#: 反推回单位时会带约 5e-6 的误差；判定线又在边界处**瞬变**（Rrhar'il AT #57 的
#: `rotate` 在 u=1408 处从 -10° 直接跳到 0°），这点误差足以把边界点推到前一条事件里，
#: 取到瞬变**之前**的角度与位置 —— 实测击打点最大偏 87.5 像素。
#:
#: 容差取 1e-4 单位（≈0.001ms）：比序列化误差大 20 倍，比事件间隔（1 单位）小 4 个数量级。
_LOCATE_EPS = 1e-4


# ---------------------------------------------------------------- 数据模型


@dataclass
class Note:
    """C 段的输入单位（由 B 段产出）。"""

    index: int
    type: int
    t_ms: float
    px: float
    py: float
    hold_ms: float = 0.0
    angle_deg: float = 0.0
    line: int = -1
    position_x: float = 0.0
    multiple: bool = False

    @property
    def type_name(self) -> str:
        return NOTE_NAMES.get(self.type, "?")


@dataclass
class Serve:
    """一次「指针为一个音符服务」的记录 —— C 段的**规划**（plan）本身。

    它和 `TouchEvent` 是两回事：`TouchEvent` 是「发了什么指令」，
    `Serve` 是「哪根手指在替哪个音符干活、当时该在哪」。验收脚本靠它把
    「事件流」和「判定规则」对上；没有它，`DOWN` 位置与音符位置不一致时
    就无从判断是「复用」还是「出错」。
    """

    stage: str          # tap / hold_head / hold_keep / drag / flick
    note_index: int
    note_type: int
    t_ms: int
    px: float
    py: float
    angle_deg: float
    pointer: int
    reused: bool        # True = 靠已有手指落在判定区内达成，未发任何事件

    @property
    def stage_name(self) -> str:
        return {
            "tap": "Tap", "hold_head": "Hold.start", "hold_keep": "Hold.keep",
            "drag": "Drag", "flick": "Flick",
        }.get(self.stage, self.stage)


@dataclass
class TouchEvent:
    """一个逻辑触摸动作。D 段按 MotionEvent 语义消费。"""

    t_ms: int
    action: str         # DOWN / POINTER_DOWN / MOVE / POINTER_UP / UP
    pointer_id: int
    x: float
    y: float


@dataclass
class Schedule:
    """C 段的产物。"""

    serves: List[Serve]
    events: List[TouchEvent]
    pointer_count: int
    unassigned: List[Dict[str, Any]] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    geometry: str = "exact"
    stats: Dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.unassigned


# ---------------------------------------------------------------- 几何


def tangent_dir(angle_deg: float) -> Tuple[float, float]:
    """判定线在**像素空间**的切向单位向量。

    `chart.note_pos` 里局部偏移是 `R(θ)·(base_x, 0)`（世界坐标，y 向上），
    投影到像素（y 向下）后 x 分量不变、y 分量反号 ⇒ 切向 = `(cosθ, -sinθ)`。
    与 phisap 的 `(cos(-θ), sin(-θ))` 逐项相同。
    """
    a = math.radians(angle_deg)
    return math.cos(a), -math.sin(a)


def tangent_offset(
    finger: Tuple[float, float], point: Tuple[float, float], angle_deg: float
) -> float:
    """手指相对判定点的**切向**偏离量（绝对值）。法向偏离不计 —— 判定区法向无限长。"""
    ca, sa = tangent_dir(angle_deg)
    return abs((finger[0] - point[0]) * ca + (finger[1] - point[1]) * sa)


def project_into_screen(
    px: float, py: float, angle_deg: float, width: float, height: float
) -> Optional[Tuple[float, float]]:
    """把屏幕外的判定点**沿判定线法线**投影回屏幕内。

    判定区法向无限长 ⇒ 「与判定点共线、且垂直于判定线」的整条直线都等效。
    取该直线与屏幕矩形的两个交点，用它们的中点（一个交点就取它）。
    完全不相交 ⇒ 返回 None（这个音符够不着，必须显式报出来而不是假装打到）。

    对齐 phisap `algo_base.recalc_pos` 的意图（它点名批评 Nhelv 的谱面把音符
    放到了屏幕外），但换成「取交点中点」的干净写法，并把不可达情形显式化。
    """
    if 0.0 <= px <= width and 0.0 <= py <= height:
        return px, py

    a = math.radians(angle_deg)
    nx, ny = math.sin(a), math.cos(a)          # 法向单位向量

    ts: List[float] = []
    if nx != 0.0:
        for x_edge in (0.0, width):
            t = (x_edge - px) / nx
            if 0.0 <= py + t * ny <= height:
                ts.append(t)
    if ny != 0.0:
        for y_edge in (0.0, height):
            t = (y_edge - py) / ny
            if 0.0 <= px + t * nx <= width:
                ts.append(t)
    if not ts:
        return None

    t_mid = (min(ts) + max(ts)) / 2.0
    x = min(max(px + t_mid * nx, 0.0), width)
    y = min(max(py + t_mid * ny, 0.0), height)
    return x, y


@dataclass
class LineEvents:
    """一条判定线的几何事件（notes.json 原样带出，C 段不重新解析谱面）。

    - `move`：`[[startTime, endTime, start, end, start2, end2], ...]`（时间为谱面时间单位）
    - `rotate`：`[[startTime, endTime, start, end], ...]`（角度，度，逆时针）

    ⚠️ 定位用**二分**而不是线性扫描。长按的跟随路径要逐毫秒求值，一部谱面几万次；
    一条判定线的 move 事件可以有 2000 条，线性扫描会把 C 段拖到分钟级
    （实测 18.2s → 0.6s）。
    """

    bpm: float
    move: List[List[float]] = field(default_factory=list)
    rotate: List[List[float]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._m_starts = [ev[0] for ev in self.move]
        self._r_starts = [ev[0] for ev in self.rotate]

    @property
    def _r(self) -> float:
        """1 个谱面时间单位对应多少秒。"""
        return 60.0 / 32.0 / self.bpm

    def to_unit(self, t_sec: float) -> float:
        """秒 → 谱面时间单位（`chart` 里的 `time`）。"""
        return t_sec / self._r

    def pos(self, t_unit: float) -> Tuple[float, float]:
        """判定线位置的**归一化** (x, y)，两者都在 [0, 1]。与 `chart.line_state` 同源。

        定位用 `t_unit + _LOCATE_EPS`（把贴着左边界的点归给右边那条新事件），
        但**插值仍用原始 `t_unit`** —— 于是边界点取到新事件的 `start`，
        而不会因为这点容差引入可见的位置误差（`k` 只差 1e-4 量级）。
        """
        i = bisect_right(self._m_starts, t_unit + _LOCATE_EPS) - 1
        if i < 0 or i >= len(self.move):
            return _MISS, _MISS
        st, en, sx, ex, sy, ey = self.move[i]
        if not (st - _LOCATE_EPS <= t_unit <= en + _LOCATE_EPS):
            return _MISS, _MISS
        k = 0.0 if en == st else (t_unit - st) / (en - st)
        return sx + (ex - sx) * k, sy + (ey - sy) * k

    def angle(self, t_unit: float) -> float:
        i = bisect_right(self._r_starts, t_unit + _LOCATE_EPS) - 1
        if i < 0 or i >= len(self.rotate):
            return _MISS
        st, en, s, e = self.rotate[i]
        if not (st - _LOCATE_EPS <= t_unit <= en + _LOCATE_EPS):
            return _MISS
        k = 0.0 if en == st else (t_unit - st) / (en - st)
        return s + (e - s) * k


@dataclass
class NotesGeometry:
    """由 notes.json 的 `lines` 段给出的**精确**几何。"""

    lines: List[LineEvents]
    screen_w: float = CANVAS[0]
    screen_h: float = CANVAS[1]
    aspect_ratio: float = DEFAULT_ASPECT_RATIO
    x_unit: float = 2.0 * 9.0 / 160.0

    def angle_deg(self, note: Note, t_ms: float) -> float:
        if note.line < 0 or note.line >= len(self.lines):
            return note.angle_deg
        v = self.lines[note.line].angle(self.lines[note.line].to_unit(t_ms / 1000.0))
        return note.angle_deg if v != v else v      # NaN ⇒ 回落

    def point_px(self, note: Note, t_ms: float) -> Tuple[float, float]:
        """判定线上 `positionX` 处的判定点（像素）。`t == 击打时刻` 时即击打点。"""
        if note.line < 0 or note.line >= len(self.lines):
            return note.px, note.py
        line = self.lines[note.line]
        mx, my = line.pos(line.to_unit(t_ms / 1000.0))
        if mx != mx or my != my:
            return note.px, note.py
        lx = -1.0 + mx * 2.0
        ly = (-1.0 + my * 2.0) / self.aspect_ratio
        base_x = note.position_x * self.x_unit
        rad = math.radians(self.angle_deg(note, t_ms))
        wx = lx + base_x * math.cos(rad)
        wy = ly + base_x * math.sin(rad)
        half = self.screen_w / 2.0
        return half + wx * half, self.screen_h / 2.0 - wy * half


@dataclass
class StaticGeometry:
    """退化几何：判定线不动、长按不跟随。只在没有 `lines` 数据时兜底。"""

    angle_deg_const: float = 0.0
    screen_w: float = CANVAS[0]
    screen_h: float = CANVAS[1]

    def angle_deg(self, note: Note, t_ms: float) -> float:
        return note.angle_deg

    def point_px(self, note: Note, t_ms: float) -> Tuple[float, float]:
        return note.px, note.py


# ---------------------------------------------------------------- 读入


@dataclass
class ChartInput:
    """`notes.json` 读入的结果。"""

    head: Dict[str, Any]
    notes: List[Note]
    geometry: Optional[NotesGeometry]

    @property
    def canvas(self) -> Tuple[float, float]:
        scr = self.head.get("screen") or {}
        return float(scr.get("width", CANVAS[0])), float(scr.get("height", CANVAS[1]))


def _parse_line(entry: Dict[str, Any], fmt: int) -> LineEvents:
    """把一条判定线的原始事件解码成**归一化** [0,1] 的坐标。

    formatVersion 1 把 x / y 打包成 `1000·x + y`（x 值域 0..880、y 值域 0..520），
    formatVersion 3 已经是分开的两个 [0,1] 分量。两者都还原成 [0,1]，
    `NotesGeometry` 之后只认这一种。
    """
    move: List[List[float]] = []
    for ev in entry.get("move_events", []):
        st, en, s, e = float(ev[0]), float(ev[1]), float(ev[2]), float(ev[3])
        if fmt == 1:
            s2 = float(ev[4]) if len(ev) > 4 else 0.0
            e2 = float(ev[5]) if len(ev) > 5 else 0.0
            move.append([st, en,
                         (s - s % 1000.0) / 1000.0 / 880.0,
                         (e - e % 1000.0) / 1000.0 / 880.0,
                         (s % 1000.0) / 520.0,
                         (e % 1000.0) / 520.0])
            _ = (s2, e2)
        else:
            move.append([st, en, s, e,
                         float(ev[4]) if len(ev) > 4 else 0.0,
                         float(ev[5]) if len(ev) > 5 else 0.0])
    return LineEvents(
        bpm=float(entry.get("bpm", 1.0)) or 1.0,
        move=move,
        rotate=[[float(v) for v in ev] for ev in entry.get("rotate_events", [])],
    )


def geometry_from_chart(chart: Any) -> NotesGeometry:
    """从 `pgauto.chart.Chart`（或任何同形对象）构造**精确**几何。

    C 段本体不 import `chart` —— 只要对象暴露 `format_version` / `aspect_ratio` /
    `screen_w` / `screen_h` / `line_events_payload()` 就够。批量工具（sweep）用它，
    避免先把谱面落成 notes.json 再读回来。
    """
    return NotesGeometry(
        lines=[_parse_line(ln, int(chart.format_version))
               for ln in chart.line_events_payload()],
        screen_w=float(chart.screen_w),
        screen_h=float(chart.screen_h),
        aspect_ratio=float(chart.aspect_ratio),
    )


def load_notes(path: str | Path) -> ChartInput:
    """读 notes.json，返回 (头部, 音符, 几何)。

    兼容没有 `lines` 段的旧文件：那时 `geometry` 为 None，调用方应显式回落。
    """
    with Path(path).open("r", encoding="utf-8") as fh:
        raw = json.load(fh)

    notes: List[Note] = []
    for i, n in enumerate(raw["notes"]):
        notes.append(
            Note(
                index=i,
                type=int(n["type"]),
                t_ms=float(n["t_ms"]),
                px=float(n["px"]),
                py=float(n["py"]),
                hold_ms=float(n.get("hold_ms", 0.0)),
                angle_deg=float(n.get("angle_deg", 0.0)),
                line=int(n.get("line", -1)),
                position_x=float(n.get("position_x", 0.0)),
                multiple=bool(n.get("multiple", False)),
            )
        )

    lines = raw.get("lines")
    geometry = None
    if lines:
        fmt = int(raw.get("format_version", 3))
        screen = raw.get("screen") or {}
        geometry = NotesGeometry(
            lines=[_parse_line(ln, fmt) for ln in lines],
            screen_w=float(screen.get("width", CANVAS[0])),
            screen_h=float(screen.get("height", CANVAS[1])),
            aspect_ratio=float(raw.get("aspect_ratio", DEFAULT_ASPECT_RATIO)),
        )

    return ChartInput(head=raw, notes=notes, geometry=geometry)


# ---------------------------------------------------------------- 帧


@dataclass(slots=True)
class _Pending:
    """一帧里待服务的一项。"""

    stage: str
    note: Note
    t_ms: int
    px: float
    py: float
    angle_deg: float


class _Frame:
    """一帧的三条队列。

    在**建帧时**就分好组，而不是每帧现做三次列表推导 —— 一部谱面有 7 万多个帧，
    那点推导会白扔掉好几秒。
    """

    __slots__ = ("down", "flick", "keep")

    def __init__(self) -> None:
        self.down: List[_Pending] = []     # Tap / Hold 起点：需要 DOWN
        self.flick: List[_Pending] = []    # Flick：需要 DOWN + 快速位移
        self.keep: List[_Pending] = []     # Drag / Hold 持续：只需「判定区内有手指」

    def add(self, pend: _Pending) -> None:
        if pend.stage == "flick":
            self.flick.append(pend)
        elif pend.stage in ("tap", "hold_head"):
            self.down.append(pend)
        else:
            self.keep.append(pend)


def clamp_into_screen(px: float, py: float, width: float, height: float) -> Tuple[float, float]:
    """把点拉到屏幕矩形的**最近点**上。

    这是 phisap `recalc_pos` 的本意（「让该位置落在屏幕内」），但它是那个函数在
    「法线与屏幕不相交」时的退化产物：`div(sa, ca)` 除零得到 nan 并污染两条候选线，
    于是 `sumx / 2` 与 `sumy / 2` 双双落到 `0`，坐标被硬摆到屏幕角上。
    这里把它**显式化**，并且只在明确开启时才使用。
    """
    return min(max(px, 0.0), width), min(max(py, 0.0), height)


def fit_flick_sweep(px: float, py: float, nx: float, ny: float,
                    radius: float, width: float, height: float
                    ) -> Tuple[float, float, float]:
    """把 Flick 的扫动「中心」沿法向往屏内推，使整段 `±R` 都留在画布内。

    返回 `(cx, cy, R_eff)`。`R_eff` 只在**画布沿法向根本放不下 2R 时**才小于 `radius`。

    ── 为什么不是逐点 `clamp_into_screen` ──────────────────────────────────
    逐点夹取会把越界的那几毫秒压成同一个坐标（`y` 恒等于 `height`），那几毫秒的
    位移变成 0，扫速掉到 0 ⇒ Flick 漏判，而且失败得无声无息。

    ── 为什么不是收缩半径（我先干过，自测推翻了）──────────────────────────
    2026-10-22 首版实现是「按边界收缩 R」。全量扫描后发现有 **197 处 R 被压到 0**，
    它们**全部**是 `py=1080`（判定线扫到画布下沿）的 Flick —— 收缩把「部分越界但
    有完整位移」换成了「完全不出界但**零位移**」，Flick 必漏。这不是保守，是把
    「可能漏」换成「一定漏」。数据见 `out/flick_edge_and_presence_2026-10-22.md`。

    ── 本算法的做法 ──────────────────────────────────────────────────────
    扫动是 `center + rate·R·n`，只要中心到两侧边界的余量都 ≥ R，整段就都在屏内。
    哪一侧不够，就把中心朝反方向推 **不足的量**（不是缩 R）。贴下沿的
    `py=1080`、`R=90` ⇒ 中心推到 `py=990`，扫动覆盖 `900..1080`：**位移仍是完整
    180px**，且末端正好压在音符位置（`1080`）上，判定区内确实有快速位移。

    只有当「法向两侧余量之和 `< 2R`」（画布沿法向比 2R 还窄，本画布 1080 高、
    R=90 需要 180，正常不会发生）时，才退化为缩小 R —— 并必然触发告警。

    ★ **余量是带符号的**（2026-10-22 补）：判定点本身就在画布外时（`py = 1085`），
    「沿 +n 的余量」是 **−5** 而不是 0。把负余量截成 0 会让推动量少掉越界的那 5px，
    推完仍然越界 —— 而且静默通过「不越界」之外的所有检查。生产路径上 `resolve()`
    保证判定点在屏内（exact / projected / clamped 三级），所以这是**导出函数的契约
    漏洞**，不是现网故障；但它是本函数存在的唯一理由（消灭越界），必须自己成立。

    `n = (0,0)`（法向退化）时无从判断，原样返回中心与半径。
    """
    # 沿 +n / -n 到边界的**带符号**余量（负数 = 沿该方向已经越界多少）
    #
    # ★ 2026-10-22 修：法向分量里的**浮点残差**会让余量变成天文数字。
    #   `tangent_dir(90°)` 返回 `(6.12e-17, -1.0)` —— 那个 6e-17 不是真实的几何分量，
    #   但 `room()` 见到 `uy != 0` 就会算 `(0 - py) / uy` = **-6.8e17**，
    #   于是 `shift = radius - room_neg` 也变成 6.8e17，扫动坐标直接爆炸
    #   （实测 x = -3.39e17）。旧版 `h = 1080` 时被「余量都够 ⇒ 提前返回」掩盖了，
    #   把底部上沿改成 958.5 之后 `room_neg` 变负 ⇒ 立刻进入推中心分支 ⇒ 暴露。
    # 画布尺寸非法时后面的余量计算全是噪声（`h = -1e18` 会让 `room` 变正、护栏也跟着飞），
    # 先拦住。生产上 `h` 来自 `canvas[1] × flick_bottom_safe_ratio`，两个都该是正数。
    if not (width > 0.0 and height > 0.0):
        raise ValueError(f"fit_flick_sweep 的画布尺寸非法：{width!r}x{height!r}")

    _EPS = 1e-12
    if abs(nx) < _EPS:
        nx = 0.0
    if abs(ny) < _EPS:
        ny = 0.0

    def room(ux: float, uy: float) -> float:
        r = float("inf")
        if ux > 0:
            r = min(r, (width - px) / ux)
        elif ux < 0:
            r = min(r, (0.0 - px) / ux)
        if uy > 0:
            r = min(r, (height - py) / uy)
        elif uy < 0:
            r = min(r, (0.0 - py) / uy)
        return float(radius) if r == float("inf") else r

    room_pos = room(nx, ny)     # 沿 +n
    room_neg = room(-nx, -ny)   # 沿 -n
    full = room_pos + room_neg  # 法向可用总长度
    r_eff = radius
    shift = 0.0
    if room_pos >= radius and room_neg >= radius:
        return px, py, radius
    if full >= 2.0 * radius:
        # 推中心：哪一侧不足就往反方向推那么多
        if room_pos < radius:
            shift = -(radius - room_pos)
        else:
            shift = radius - room_neg
    else:
        # 画布沿法向放不下 2R ⇒ 只能缩半径，并居中
        r_eff = max(0.0, full / 2.0)
        shift = (room_pos - room_neg) / 2.0
    # 兜底：推移量不该超过画布对角线（+ 半径）。超了说明上面某个分支算错了 ——
    # **显式炸**，不要静默产出天文坐标：那会让真机注入把指针甩到几百个屏宽之外，
    # 而且症状要到打歌时才看得见（2026-10-22 实测过一次 x=-3.39e17）。
    if abs(shift) > math.hypot(width, height) + radius:
        raise ValueError(
            f"fit_flick_sweep 算出荒谬的推移量 shift={shift!r}"
            f"（中心 ({px},{py})，法向 ({nx},{ny})，画布 {width}x{height}）")
    return px + shift * nx, py + shift * ny, r_eff


def fit_flick_radius(px: float, py: float, nx: float, ny: float,
                     radius: float, width: float, height: float) -> float:
    """**已废弃**（2026-10-22 自测推翻）：只返回收缩后的半径，会造出零位移 Flick。

    保留它只是为了不让外部脚本一头撞在 `AttributeError` 上；等价于
    `fit_flick_sweep(...)[2]`。新代码请用 `fit_flick_sweep`。
    """
    return fit_flick_sweep(px, py, nx, ny, radius, width, height)[2]


def retime_offsets(lead_ms: int) -> Iterator[int]:
    """时间微调的搜索次序：距 `t0` **由近及远**（0, -1, +1, -2, +2, …）。"""
    yield 0
    for d in range(1, lead_ms + 1):
        yield -d
        yield d


def nearby_ints(t: float, radius: int) -> Iterator[int]:
    """以 `t` 为中心、半径 `radius` 毫秒内的整数，按**距 `t` 由近及远**排列。

    最近的两个整数就是 `floor(t)` / `ceil(t)`；两者等距时（`t` 的小数部分恰为 0.5）
    **先给小的那个** —— 与 `pick_head_time` 的原始实现同规则，于是绝大多数音符的
    第一个候选没有任何变化，扩窗只影响「原本两个候选都不可达」的音符。

    `radius=0` 时只给 `floor(t)` / `ceil(t)` —— `t` 是整数时退化成**单个**候选。
    这正是 J 段之前的行为（也是那 5 个音符掉分的直接原因：`t` 恰为整数，候选集
    里根本没有「相邻的另一个毫秒」可换）。
    """
    lo = int(math.floor(t))
    hi = int(math.ceil(t))
    first, second = (lo, hi) if t - lo <= hi - t else (hi, lo)
    yield first
    if hi != lo:
        yield second
    for d in range(1, radius + 1):
        a, b = lo - d, hi + d
        if t - a <= b - t:
            yield a
            yield b
        else:
            yield b
            yield a


def build_frames(
    notes: Sequence[Note],
    geometry: Any,
    *,
    flick_lead_ms: int = FLICK_LEAD_MS,
    flick_retime: bool = True,
    clamp_offscreen: bool = False,
) -> Tuple[Dict[int, _Frame], List[Dict[str, Any]], Dict[str, int]]:
    """谱面 → 帧表。返回 (帧表, 无法服务的音符, 统计)。

    **几何一律取「事件被盖的那个整数毫秒」的判定点**，不取音符的精确时刻 —— 判定线会
    在事件边界**瞬变**（`Incyde/IN` 的 5 号线在 t=47333.333 处跳 192.5 像素），取错时刻
    会让手指落在判定区外。下发时刻本身在**判定窗内的整数毫秒**里挑一个判定点可达的，
    见 `pick_head_time`。

    判定点仍落在屏幕外时，按**三级**处理 —— 分层与 phisap `algo/algo1.py` 一致：

    1. **时间微调**（Flick 专属，默认开）。Flick 的判定窗口是 `t0 ± flick_lead_ms`，
       而判定线会在极短时间内**闪到另一个位置**，同一个 Flick 因此在两个位置都判得到。
       phisap 就是靠这一条把 `DESTRUCTION 3,2,1` 那个屏外 flick 救回来的，它的注释
       写得很直白：「这个 flick 在 (w/2, h/2) 的位置闪了几下 … 相当于这个 flick 可以
       同时在 (w/2, h/2) 和 (w/2, -h/2) 判定 … 然而 (w/2, -h/2) 不可能达到，
       所以相当于是在 (w/2, h/2) 判定了」。这里在窗口内取**距 `t0` 最近**的入屏时刻。
       ⚠️ 本段原有一句「这条依据只对 Flick 成立，Tap / Drag / Hold 没有类似机制」——
       **该断言已被 J 段证伪**：`AfterZABANIYAMUGEdit/IN #154` 与 `Message/EZ` 的 4 个
       Drag 都是「判定线恰在音符时刻整条甩出屏幕」（0 号线在 t=146250 从归一化
       (0.5, 0.5) 甩到 (3.0, 3.0)），而它们**同样是靠换一毫秒救回来的**。区别只在
       实现位置：Flick 的这条微调改的是**扫动的几何基准**，Tap/Drag/Hold 的改的是
       **下发时刻本身**（在窗口内挪一毫秒），后者在 `pick_head_time` 里统一处理。
    2. **沿法线投影**（默认开）。判定区法向无限长 ⇒ 与判定点同法线的一切点等效。
    3. **拉进屏幕**（`clamp_offscreen`，**默认关**）。前两级都失败才用。它只保证
       事件流完整可注入，**不保证命中**（判定区仍在屏幕外，手指只是被摆在最近的
       屏幕边缘上），所以默认宁可把它如实报成 `unassigned`。
    """
    frames: Dict[int, _Frame] = {}
    unassigned: List[Dict[str, Any]] = []
    stats = {"projected": 0, "clamped": 0, "retimed": 0, "hold_keep_clamped": 0}

    def emit(kind: str, note: Note, t_ms: int, px: float, py: float,
             angle_deg: float, how: str) -> None:
        if how in stats:
            stats[how] += 1
        fr = frames.get(t_ms)
        if fr is None:
            fr = frames[t_ms] = _Frame()
        fr.add(_Pending(kind, note, t_ms, px, py, angle_deg))

    def resolve(note: Note, t_ms: float,
                allow_clamp: bool = True) -> Optional[Tuple[float, float, float, str]]:
        """`t_ms` 时刻判定点的**可注入**位置 → `(x, y, angle, how)`；全失败给 None。

        `allow_clamp=False` 用来把第三级（拉进屏幕）留到最后再试：Flick 要先用完
        「时间微调」这个物理正确的机制，不能被 clamp 抢先兜掉。
        """
        px, py = geometry.point_px(note, t_ms)
        ang = geometry.angle_deg(note, t_ms)
        w, h = geometry.screen_w, geometry.screen_h
        if 0.0 <= px <= w and 0.0 <= py <= h:
            return px, py, ang, "exact"
        hit = project_into_screen(px, py, ang, w, h)
        if hit is not None:
            return hit[0], hit[1], ang, "projected"
        if clamp_offscreen and allow_clamp:
            cx, cy = clamp_into_screen(px, py, w, h)
            return cx, cy, ang, "clamped"
        return None

    def pick_head_time(note: Note) -> int:
        """音符的**下发时刻**：在判定窗内挑一个「判定点可达」的整数毫秒。

        事件只能盖在**整数毫秒**上，而几何必须取**那一毫秒**的判定点（判定线会在事件
        边界瞬变）。所以下发时刻不是简单的 `round(note.t_ms)`：那一毫秒的判定点可能
        恰好被判定线甩到屏幕外。

        最干净的例子是 `Message/EZ` 的 `#56`（Drag）：音符时刻 t=146250 恰好是 0 号
        判定线甩到归一化 (3.0, 3.0)（画布外，像素 (5652, -2160)）的那一条事件边界，
        而 t=146249 判定点还在屏幕中心。旧实现只在 `{floor, ceil}` 里挑，`t` 又是整数
        ⇒ 候选集只有**一个**，两个都不可达，整条音符直接 `unassigned`、一个事件都不发。

        搜索半径按类型给（见 `HEAD_WINDOW_MS`）：

        * Tap / Drag / Hold —— ±`HEAD_WINDOW_MS`（80ms，就是 Perfect 区）。
          窗内任一整数毫秒都不掉评级（仍判 Perfect，只是 Δt 变大）。
        * Flick —— **0**（只保持 `{floor, ceil}`）。它已经有一套自己的时间微调
          （见 `build_frames` 第三级的第 1 条），而且它的扫动必须在 `[t-30, t+30]` 内
          经过判定点，把 `t0` 挪出这个窗会让整条扫动离开判分窗口、反而判 Miss。

        次序**由近及远**（`nearby_ints`），所以对绝大多数音符来说第一个候选就是
        `round(t_ms)`，输出与扩窗之前**逐字节一致**；只有原本两个候选都不可达的音符
        才会被挪动。全部不可达时返回最近的那个整数毫秒（与旧实现的兜底一致），
        交由调用方的 `clamp` 兜底或如实报成 `unassigned`。
        """
        radius = 0 if note.type == NOTE_FLICK else HEAD_WINDOW_MS
        fallback: Optional[int] = None
        for t in nearby_ints(note.t_ms, radius):
            # 只接受**落在判定窗内**的候选：超出窗口就等于掉出 Perfect 区（Drag 更是
            # 直接 Miss），「换一毫秒」就没意义了。
            if t < 0 or abs(t - note.t_ms) > HEAD_WINDOW_MS:
                continue
            if fallback is None:
                fallback = t            # 最近的整数毫秒，平手取小 —— 与旧实现的兜底一致
            if resolve(note, float(t), allow_clamp=False) is not None:
                return t
        return fallback if fallback is not None else int(math.floor(note.t_ms))

    for note in notes:
        t0 = pick_head_time(note)
        # **几何必须取「事件被盖的那个时刻」的判定点**，也就是整数毫秒 t0，
        # 而不是音符的精确时刻 `note.t_ms`（浮点）。
        #
        # 判定线会在事件边界**瞬变**：`Incyde/IN` 的 5 号线在 t=47333.333 处从
        # `(595.5, 864.0) @ 0°` 跳到 `(788.0, 844.9) @ -3°` —— 相差 **192.5 像素**。
        # 若按 `note.t_ms` 取点、却把事件盖在 47333，手指就落在**瞬变之前**那条判定区
        # 之外 192px 处：真机上那个 Tap 必 Miss。
        # （`replay_score` 的宽松模型量不出这个 —— 它只看「判定区内有手指」，
        #   手指与判定点用的是同一个错坐标，于是两边一起错、看起来完美。）
        t_head = float(t0)

        if note.type == NOTE_HOLD:
            r = resolve(note, t_head)
            if r is None:
                unassigned.append(dict(note=note.index, type=note.type_name,
                                       t_ms=t0, stage="hold_head", reason="offscreen"))
            else:
                emit("hold_head", note, t0, *r)
            hold_ms = int(math.ceil(note.hold_ms))
            kept = 0
            for off in range(1, hold_ms + 1):
                t = float(t0 + off)
                # 这里刻意不走通用兜底（allow_clamp=False），让「长按持续帧扫出屏幕」始终
                # 归到 hold_keep_clamped 这一个计数里 —— 否则 --clamp-offscreen 一开，
                # 这些帧就会被计入通用的 `clamped`，与「音符级兜底」混成一个数字。
                r = resolve(note, t, allow_clamp=False)
                if r is None:
                    # 长按一旦按下，**手指就不可能离开屏幕** —— 判定线扫出屏幕的那几毫秒，
                    # 真人玩家也只是把手指停在屏幕上。所以这里把持续点拉到屏幕边缘继续
                    # 「按住」，而不是整帧作废（作废会让 plan 断链、验收的逐毫秒判据失效）。
                    # 代价是服务点落在判定区外，因此单独计数 `hold_keep_clamped`。
                    px, py = geometry.point_px(note, t)
                    ang = geometry.angle_deg(note, t)
                    cx, cy = clamp_into_screen(px, py, geometry.screen_w, geometry.screen_h)
                    emit("hold_keep", note, t0 + off, cx, cy, ang, "hold_keep_clamped")
                    continue
                kept += 1
                emit("hold_keep", note, t0 + off, *r)
            if hold_ms and not kept:
                # 持续段一帧都没落进判定区 ⇒ 这个长按是真接不上
                unassigned.append(dict(note=note.index, type=note.type_name,
                                       t_ms=t0, stage="hold_keep", reason="offscreen"))
        elif note.type == NOTE_FLICK:
            start = max(0, t0 - flick_lead_ms)
            # 扫动在 `[t0 - 提前量, t0 - 提前量 + 跨度)` 上铺开，手指正好在 **t0** 那一刻
            # 经过判定点（`FLICK_SPAN_MS` 是提前量的 2 倍），所以基准点取 t0。
            # 顺序：先在 t0 上试 → 再在判定窗口内做时间微调 → 最后才拉进屏幕
            r = resolve(note, t_head, allow_clamp=False)
            if r is None and flick_retime:
                for dt in retime_offsets(flick_lead_ms):
                    if dt == 0 or t0 + dt < 0:
                        continue
                    r = resolve(note, float(t0 + dt), allow_clamp=False)
                    if r is not None:
                        stats["retimed"] += 1
                        break
            if r is None:
                r = resolve(note, t_head)
            if r is None:
                unassigned.append(dict(note=note.index, type=note.type_name,
                                       t_ms=start, stage="flick", reason="offscreen"))
                continue
            emit("flick", note, start, *r)
        else:
            kind = "tap" if note.type == NOTE_TAP else "drag"
            r = resolve(note, t_head)
            if r is None:
                unassigned.append(dict(note=note.index, type=note.type_name,
                                       t_ms=t0, stage=kind, reason="offscreen"))
                continue
            emit(kind, note, t0, *r)

    # ── Flick 同刻保护 pass（2026-10-10 g3/g5 双轮真机定案后加）──────────────
    #
    # 现象：Gungnir Fracture/IN 的 #406 Hold 起手 DOWN 与 #405 Flick 的**判定时刻**
    # 同毫秒（53250），g3/g5 两轮真机都在这里断 combo（404→0，Max Combo 精确对上）；
    # 而全曲 41 处「DOWN 落在 Flick 横扫窗口内」的其他 40 处（与 Flick 判定错开
    # 1~30ms）全部命中。失败处与成功处的事件流逐条同构（同样的 POINTER_DOWN +
    # 逐 ms 横扫流交错、同样的横扫路径与 DOWN 坐标），且两轮断点不漂移 —— 排除
    # 链路随机停顿，病根收敛到「Flick 判定瞬间与同毫秒新触摸」的设备/游戏侧
    # 边缘行为（离线不可复现，只能绕开）。
    #
    # 对策：非 Flick 音符的整条服务链（head + keep）与 Flick 的判定时刻错峰 ——
    # 首选延后 `flick_lead+2`（横扫最末毫秒是 t0+29，t0+32 起步；判定窗 ±80ms
    # 内零评级损失），窗放不下再提前到起手之前；几何一律按**新时刻**重取
    # （判定线瞬变，取错时刻会落到屏外 —— 与主循环同一套 resolve）。
    # 同帧多个非 Flick head（双 Tap 同刻，如 111125）随后按序各错 1ms。
    guard_moves: List[Tuple[int, int, int]] = []   # (note_index, from_t, to_t)

    def _guard_move_note(note: Note, from_t: int, to_t: int) -> bool:
        """把 note 的服务链（head + keep 链）从 from_t 整体平移到 to_t。"""
        fr = frames.get(from_t)
        if fr is None:
            return False
        head = next((p for p in fr.down if p.note.index == note.index), None)
        if head is None:
            return False
        r = resolve(note, float(to_t), allow_clamp=False)
        if r is None:
            return False
        fr.down.remove(head)
        frames.setdefault(to_t, _Frame()).add(
            _Pending(head.stage, note, to_t, r[0], r[1], r[2]))
        if note.type == NOTE_HOLD:
            hold_ms = int(math.ceil(note.hold_ms))
            for off in range(1, hold_ms + 1):
                ofr = frames.get(from_t + off)
                if ofr is None:
                    continue
                kp = next((q for q in ofr.keep if q.note.index == note.index), None)
                if kp is None:
                    continue
                ofr.keep.remove(kp)
                kt = to_t + off
                kr = resolve(note, float(kt), allow_clamp=False)
                if kr is None:
                    # 与主循环的 hold_keep 兜底一致：判定区扫出屏外时拉到屏幕边缘
                    px, py = geometry.point_px(note, float(kt))
                    ang = geometry.angle_deg(note, float(kt))
                    cx, cy = clamp_into_screen(px, py, geometry.screen_w, geometry.screen_h)
                    kr = (cx, cy, ang)
                frames.setdefault(kt, _Frame()).add(
                    _Pending(kp.stage, note, kt, kr[0], kr[1], kr[2]))
        guard_moves.append((note.index, from_t, to_t))
        return True

    # pass A：非 Flick 的 head 不与 Flick 的判定时刻同毫秒
    flick_judge = {pend.t_ms + flick_lead_ms
                   for fr in frames.values() for pend in fr.flick}
    for t in sorted(t for t in frames if t in flick_judge and frames[t].down):
        for pend in list(frames[t].down):
            note = pend.note
            for nt in (t + flick_lead_ms + 2, t - flick_lead_ms - 2):
                if nt < 0 or abs(nt - note.t_ms) > HEAD_WINDOW_MS:
                    continue
                if _guard_move_note(note, t, nt):
                    break

    # pass B（同刻双 head 错峰）**已裁撤**（2026-10-10）：错峰会把「同刻多押」
    # 从多指并行改成同一指针串行复用（DOWN 前 UP 回填），破坏 238 处已验证的
    # 成功形态、压低峰值并发语义；而其目标（238 处同刻双 DOWN 中唯一丢失的
    # 111125）大概率是随机停顿而非确定性缺陷 —— 两轮真机只在 #405/#406 一处
    # 稳定复丢，pass A 已覆盖。若 g6 在 111125 复丢，再单独评估。

    stats["flick_guard"] = len(guard_moves)
    stats["flick_guard_moves"] = guard_moves

    return frames, unassigned, stats


# ---------------------------------------------------------------- 指针池


class _Pointer:
    __slots__ = ("id", "down", "used", "px", "py", "age", "busy_until", "serve_until",
                 "bound_at", "note_index", "sweep", "click_claimed",
                 "last_keep_ms", "keep_x", "keep_y")

    def __init__(self, pid: int) -> None:
        self.id = pid
        self.down = False          # 当前是否按下
        self.used = False          # 是否被任何音符用过
        self.px = 0.0
        self.py = 0.0
        self.age = 0               # 距「绑定」的毫秒数；< 0 表示正在 flick
        self.busy_until = -(10 ** 9)   # 该指针最后一条事件（DOWN/MOVE/UP）的时刻
        self.serve_until = -(10 ** 9)  # 该指针最后一次**被记为在服务**的时刻
        # ── 长按保持的抽稀状态（2026-10-26）──
        # 真机证据：`玩具狂奏曲終焉 AT` 30~40 s 有 21 个长按并发，plan 里 hold_keep 14,553 帧，
        # 端内 Densify 扩成 MOVE 流后把自己打饱和（244 帧晚 30~60 ms）⇒ 那一段 106 个音符全 Late。
        # 修法：**同一指针的 keep 每 ≥16 ms 才发一条**（上限 8 px 漂移），与真实手指采样率一致。
        self.last_keep_ms = -(10 ** 9)
        self.keep_x = 0.0
        self.keep_y = 0.0
        self.bound_at = -(10 ** 9)     # 当前这次绑定的时刻
        self.note_index = -1
        self.sweep = None              # 本帧刚排下的扫动 (t0, t1, x0, y0, x1, y1)
        #: **当前这一次按下是否已经被某个 Tap / Hold 起点认领**。
        #:
        #: Phigros 的规则是「**一次点击只能判定一个 Tap/Hold**，优先判最早的那个」。
        #: 所以同帧两个 Tap 必须各要一次真实按下 —— 靠「判定区内已有手指」顶替，
        #: 在真机上第二个音符就是 Miss。Drag / Hold 持续段**不吃**点击（它们只要求
        #: 「判定区内有手指」），因此那些按下保持未认领，谁都可以拿去判一个 Tap。
        self.click_claimed = False

    @property
    def released_after(self) -> int:
        """该指针最早可以「合法抬起」的时刻下界（不含），取事件与服务的较晚者。

        只用 `busy_until` 是不够的：指针靠「判定区内已有手指」被复用时**一条事件都不发**，
        而手指确实留在原地继续服务。若此时按事件时刻把它判为「已闲置」并回填一个更早的 UP，
        就会**倒回头作废**它之后那些复用服务（验收脚本量到 33962 条 plan 记录「缺按」）。
        """
        return max(self.busy_until, self.serve_until)


class _Planner:
    """帧表 → 指针分配 → 原始事件。"""

    def __init__(self, max_pointers: int, canvas: Tuple[float, float],
                 tangent_px: float, flick_radius_px: float, flick_span_ms: int,
                 chord_px: Optional[float] = None,
                 presence_px: Optional[float] = None,
                 drag_presence_2d_px: Optional[float] = None,
                 own_pointer_off: bool = False,
                 flick_bottom_safe_ratio: float = FLICK_BOTTOM_SAFE_RATIO,
                 flick_sweep_stride_ms: int = FLICK_SWEEP_STRIDE_MS):
        self.pointers = [_Pointer(i) for i in range(max_pointers)]
        #: **只给变异测试用**：关掉「把本音符自己那根手指挪过去」这道闸（见 `_own_pointer`）。
        self.own_pointer_off = own_pointer_off
        self.canvas = canvas
        self.tangent_px = tangent_px
        #: 「已按着的手指能离判定点多远」的阈值；None = 用 `tangent_px`（旧口径）。
        self.presence_px = tangent_px if presence_px is None else presence_px
        # ★ Drag 专用：切向过了还要看真实 2D 距离（默认 None = 旧口径"法向无限长"）。
        #   开它才会在"切向够近、法向差得多"时补 MOVE。
        self.drag_presence_2d_px = drag_presence_2d_px
        #: 同帧「一根手指被挪走之后还能不能再顶第二个音符」的重合半径（画布 px）。
        self.chord_px = (CHORD_COINCIDENT_RATIO * canvas[0]
                         if chord_px is None else chord_px)
        self.flick_radius_px = flick_radius_px
        self.flick_span_ms = flick_span_ms
        #: 扫动 MOVE 的采样步长（毫秒）。1 = 旧行为（逐毫秒一条，60 条/次）。
        #: 见 `FLICK_SWEEP_STRIDE_MS` 的成因与真机代价。
        self.flick_sweep_stride_ms = max(1, int(flick_sweep_stride_ms))
        #: Flick 扫动的**底部安全上沿**（占画布高的比例，见 `FLICK_BOTTOM_SAFE_RATIO`）。
        #: `1.0` = 用满画布（旧行为，扫动末端可落在画布底边 = 真机屏外）。
        #: 生产值 `639/720 = 0.8875`：扫动一律收进真机 `y ≤ 639`（手势区外）。
        self.flick_bottom_safe_ratio = float(flick_bottom_safe_ratio)
        #: 扫动半径被画布边界压过的 Flick 数 / 压到的最小半径（`None` = 一个都没压）。
        #:
        #: 2026-10-22 之前这里是 **0**，因为 `_serve_flick` 根本没做边界处理
        #: （注释声称的 `clamp_into_screen` 兜底从未存在）⇒ 全量 8516 条事件跑出画布。
        self.flick_radius_shrunk = 0
        self.flick_radius_min: Optional[float] = None
        #: 扫动中心被往屏内推过的 Flick 数 / 最大推移量（画布 px）。
        #: 这是「推中心」策略的计数（**不是**缩半径）—— 绝大多数边界 Flick 走这条。
        self.flick_shifted = 0
        self.flick_shift_max = 0.0
        #: 被压过的半径分布（只有「压过」才计数）：<10 / <30 / <60 / 其余（<90）。
        #: `<10` = 扫速已低于识别阈值（见 `FLICK_RADIUS_MIN_PX`），要单独盯。
        self.flick_radius_buckets = {"lt10": 0, "lt30": 0, "lt60": 0, "ge60": 0}
        self.raw: List[Tuple[int, int, int, str, int, float, float]] = []
        self.serves: List[Serve] = []
        self.unassigned: List[Dict[str, Any]] = []
        self.warnings: List[str] = []
        self.seq = 0
        self.now = 0
        self.last_t: Optional[int] = None
        self.reused = 0
        self.stolen = 0
        #: Drag 的 2D 闸门因"指针已满"而放宽的次数（>0 说明谱面在顶 10 指上限）。
        self.presence_relaxed = 0
        #: 长按 keep 被抽稀（只更新模型、不发事件）的次数。
        self.keep_thinned = 0
        #: 「同帧按下已被别人认领 ⇒ 只能另按一次」的次数。大于 0 说明谱面里存在
        #: 同帧多个 Tap/Hold 起点（真机上的双押），这些位置是靠**多一根手指**打下来的。
        self.share_refused = 0
        #: 进行中的长按：note_index → (当前保持它的指针 id, 结束时刻)
        #:
        #: ⚠️ 这里必须记**谁是真保持者**，不能给「碰过的指针」打时间戳。
        #: 长按的持续段是逐毫秒派发的，手指会在指针之间交接；按时间戳标记会让所有
        #: 曾经帮忙保持过的指针一直「被保护」到长按结束 —— 实测 `祈-我ら神祖…/AT`
        #: 有 7 根手指被幽灵保护，7 万帧里唯一需要第 11 根的瞬间就撞在这上面。
        self.hold_owner: Dict[int, int] = {}
        self.hold_end: Dict[int, int] = {}
        #: **本帧**已经被下发过 down/move 的指针 id —— 它们的位置已经承诺给别的音符了。
        #:
        #: 用途见 `CHORD_COINCIDENT_RATIO`：同帧再想拿这根手指顶别的音符，必须几乎重合。
        #: 每帧开头清空（见 `run`）。flick 扫动写在 `now+1..` 上，不算本帧承诺。
        self.frame_busy: Set[int] = set()
        #: 同刻（同一毫秒）有 ≥2 个**不同音符**需要服务的组数 / 这些组里的音符数。
        self.chord_groups = 0
        self.chord_notes = 0
        #: 无空闲指针、只能借用「本帧已承诺给别的音符」的手指（且超出重合半径）的音符数。
        #: **> 0 就是「这一处大概率会 Miss」**，不许静默发生。
        self.chord_refused = 0
        #: 借用的明细（前若干条），用于落盘定位。
        self.chord_refused_detail: List[Dict[str, Any]] = []

    # ------------------------------------------------------------ 事件

    def _ev(self, t: int, action: str, p: _Pointer, x: float, y: float) -> None:
        rank = 0 if action == "up" else (1 if action == "down" else 2)
        self.raw.append((int(t), rank, self.seq, action, p.id, x, y))
        self.seq += 1
        if t > p.busy_until:
            p.busy_until = int(t)
        # 本帧的 down/move ⇒ 这根手指的位置已经承诺给这个音符了。
        # ⚠️ 只记 `t == now`：flick 扫动写在 `now+1 .. now+span-1` 上，属于未来帧。
        if int(t) == self.now and action in ("down", "move"):
            self.frame_busy.add(p.id)

    def _release(self, p: _Pointer) -> None:
        """抬起该指针，时刻取「合法的最早时刻」。

        **不得早于它最后一条事件（busy_until），也不得早于它最后一次被记为在服务
        的时刻（serve_until）**，否则会出现「UP 之后还有 MOVE」或「UP 倒回头作废
        复用服务」两种非法情形。候选筛选已保证 `released_after < now`，故这个时刻
        一定落在 `(released_after, now]` 内。

        ★ 2026-10-10（MIN_PRESS_MS）：还不得早于「本次按下 + MIN_PRESS_MS」。
        选针关口（`_pick_for_down`）已保证按着未满 MIN_PRESS_MS 的指针不会被选中，
        因此 `bound_at + MIN_PRESS_MS ≤ now` 恒成立——这个下限**不会**把 UP 推到
        未来、破坏事件序。设 MIN_PRESS_MS=0 时此式退化为旧口径。
        （2026-10-10 m2：已回退为 0，本下限现恒不生效。2026-10-26 的 25 ms 试验也已回退：
          真机 `祈-我ら神祖…/AT` Miss 9→11，见 `cd8d295`；`TAP_MIN_PRESS_MS` 只是**未启用**的常量。）
        """
        if not p.down:
            return
        # ⚠ 这里**不要**再写「MIN_PRESS_MS 现设为 TAP_MIN_PRESS_MS」：实测否证后已回退，
        #   当前生产值就是 0（`TAP_MIN_PRESS_MS=25` 全仓无人引用，grep 可证）。
        t_up = max(min(p.released_after + 1, self.now), p.bound_at + MIN_PRESS_MS)
        self._ev(t_up, "up", p, p.px, p.py)
        p.down = False
        self._give_up_holds(p)

    def _press(self, p: _Pointer, x: float, y: float) -> None:
        if p.down:
            self._release(p)
        self._ev(self.now, "down", p, x, y)
        p.down = True
        p.used = True
        p.px, p.py = x, y
        p.bound_at = self.now
        p.serve_until = self.now
        p.click_claimed = False        # 这是一次全新的点击，还没被任何 Tap/Hold 吃掉

    def _move(self, p: _Pointer, x: float, y: float) -> None:
        self._ev(self.now, "move", p, x, y)
        p.px, p.py = x, y

    def _move_keep(self, p: _Pointer, x: float, y: float) -> None:
        """长按保持专用的 MOVE：**每 `HOLD_KEEP_MIN_MS` 最多一条**，漂移超阈值则强制发。

        只在「距上一条 keep < 16 ms 且 距上一条真正发出的位置 < 8 px」时**只更新模型、不发事件**
        —— 模型（p.px/py）必须更新，否则复用/存在性判断会用旧位置；游戏侧最多滞后 16 ms、8 px。
        """
        import math as _m
        if (self.now - p.last_keep_ms < HOLD_KEEP_MIN_MS
                and _m.hypot(x - p.keep_x, y - p.keep_y) < HOLD_KEEP_DRIFT_PX):
            p.px, p.py = x, y
            self.keep_thinned += 1
            return
        self._ev(self.now, "move", p, x, y)
        p.px, p.py = x, y
        p.keep_x, p.keep_y = x, y
        p.last_keep_ms = self.now

    # ------------------------------------------------------------ 长按所有权

    def _claim_hold(self, note_index: int, p: _Pointer, end_ms: int) -> None:
        """把长按 `note_index` 的「当前保持者」记为指针 `p`（会从旧保持者手里移交）。"""
        self.hold_owner[note_index] = p.id
        self.hold_end[note_index] = end_ms

    def _give_up_holds(self, p: _Pointer) -> None:
        """指针被抬起 ⇒ 它不再是任何长按的保持者。"""
        if not self.hold_owner:
            return
        for n in [n for n, pid in self.hold_owner.items() if pid == p.id]:
            self.hold_owner.pop(n, None)
            self.hold_end.pop(n, None)

    def _is_keeper(self, p: _Pointer) -> bool:
        """该指针是不是某个**仍在进行**的长按的当前保持者。

        保持者的手指不能拿去按 Tap / Hold 起点 / Flick —— 那会让长按当场断掉。
        但仅仅是「碰过一次」的指针不算：它随时可以交出去。
        """
        now = self.now
        for n, end in self.hold_end.items():
            if end > now and self.hold_owner.get(n) == p.id:
                return True
        return False

    def _purge_holds(self) -> None:
        """清掉已经结束的长按登记（保持者表只留进行中的）。"""
        now = self.now
        for n in [n for n, end in self.hold_end.items() if end <= now]:
            self.hold_end.pop(n, None)
            self.hold_owner.pop(n, None)

    # ------------------------------------------------------------ 选针

    def _pointer_pressure(self) -> bool:
        """指针是否**已经用满**（没有空闲手指可新按下）。

        用途：Drag 的 2D 闸门在"还有手指可用"时严格执行（宁可多按一次也别把手指停在
        离音符 20px 的地方）；**快用满时放宽**，否则会去抢正在长按的手指 ——
        真机症状是"长 hold 按住了又松开"（`祈-我ら神祖…/AT`，2026-10-25）。
        阈值取 **剩余空闲 ≤1** 而不是 ==0：那次实测"完全用满"才放宽根本不触发，
        因为第 10 根手指在决策时还闲着，等真正满的时候已经在抢长按了。
        """
        idle = sum(1 for p in self.pointers if (not p.down) and p.sweep is None)
        return idle <= 1

    def _pick_for_down(self, px: float, py: float, *,
                       avoid_busy: bool = False) -> Optional[_Pointer]:
        """需要一个 DOWN：优先复用「已用过且不在长按中」的指针，就近取。

        ★ 复用条件写的是 `released_after < now - 1`，**不是** `released_after < now`。

        差这一毫秒的后果：`_press` 遇到仍按着的指针会先 `_release` 再发 DOWN，
        而 `_release` 的抬起时刻是 `min(released_after + 1, now)`。若
        `released_after == now - 1`，抬起就落在 `now` —— 事件流里出现
        「同一毫秒、同一指针，先 `POINTER_UP` 再 `POINTER_DOWN`」。

        把关口提前到选针：宁可多占一根指针（`fresh` 兜底，上限 10 根而峰值并发
        只有 2），也不让事件流自身产生这个模式。

        ⚠️ **2026-10-09 真机复测：这个模式不是丢分根因。**
        曾据 70MF IN 的一次真机实测（`touches` 旧版，8 处冲突、`V / 99.81% /
        3 Miss`）推断「同一毫秒内事件数为 3 的那 3 处 3/3 全丢分」。修复后**重打
        同一首**：冲突 **8 → 0**，成绩只从 99.81% 变成 **99.87%**（3 Miss →
        **2 Miss**），**没有 AP**；且回看两轮录屏的 combo 逐帧读数，那 3 处冲突
        时刻（事件 `t_ms` 31480 / 100090 / 129283）**一次都没断过**。
        ⇒ 原推断把「同时出现的两件事」误当成了因果。

        改动的正当性不受影响：它消除的是**事件流自身的非法模式**，全量 1064 谱面
        实测无副作用（未分配音符 0 → 0、指针上限 9 → 9、峰值并发 9 → 9），
        并已有 `tools/verify_schedule.py` 第 23 项 + 2 条单测兜底。
        代价：429 / 1064 谱面的指针 id 会重排（同刻同位仅换标签，语义无关）。

        真正的残留丢分（2 处）见 `out/device-test-plan.md` §4.11。

        `avoid_busy=True`（2026-10-21）：**不碰本帧已经被别的音符挪过的指针**，也
        不走「抢占」兜底 —— 抢不到就返回 None，让调用方显式决定（记账 or 判失败）。
        这是 `_serve_presence` 的第二道：先问「有没有不是本帧承诺的指针」，
        有就给这个音符一根自己的，别去借一根刚被别人挪走的手指。
        """
        limit = self.now - 1          # 最早合法抬起时刻须**严格小于** now
        # ★ 2026-10-10（MIN_PRESS_MS）：按着未满 MIN_PRESS_MS 的指针不可被选中——
        # 选中会触发 `_press → _release`，而回填式抬起正是 1ms 短按的来源。
        # 必须**严格** `< now`：抬起时刻下限是 `bound_at + MIN_PRESS_MS`，若允许
        # `== now`，UP 会被推到与新 DOWN 同一毫秒——正是本函数上文禁止的模式。
        # 设 MIN_PRESS_MS=0 时此条件恒假，退化为旧口径。
        # （2026-10-10 m2：已回退为 0，本关口现恒不生效。）
        cands = [p for p in self.pointers
                 if p.used and p.age > 0 and p.released_after < limit
                 and not (MIN_PRESS_MS > 0 and p.down
                          and p.bound_at + MIN_PRESS_MS >= self.now)
                 and not self._is_keeper(p)]
        if avoid_busy:
            cands = [p for p in cands if p.id not in self.frame_busy]
        if cands:
            return min(cands, key=lambda p: (p.px - px) ** 2 + (p.py - py) ** 2)
        fresh = [p for p in self.pointers if not p.used]
        if fresh:
            return fresh[0]
        if avoid_busy:
            return None
        fallback = [p for p in self.pointers
                    if p.released_after < limit
                    and not (MIN_PRESS_MS > 0 and p.down
                             and p.bound_at + MIN_PRESS_MS >= self.now)
                    and not self._is_keeper(p)]
        if fallback:
            self.stolen += 1
            return min(fallback, key=lambda p: (p.px - px) ** 2 + (p.py - py) ** 2)
        return None

    def _pick_for_presence(self, px: float, py: float, angle_deg: float,
                           same_frame_only: bool = False,
                           stable_only: bool = False,
                           max_2d_px: Optional[float] = None) -> Optional[_Pointer]:
        """只需要「判定区内有手指」：找切向偏移最小的那根，一个事件都不用发。

        `same_frame_only=True` 是给 Tap / Hold 起点用的「同帧顶替」：只认同一帧里
        **刚按下**的指针，且它那次按下**还没被别的 Tap/Hold 认领**（`click_claimed`）。
        少了后一个条件就会踩到「一次点击只能判一个 Tap/Hold」——
        `QZKagoRequiem/AT` 的 `#1616`/`#1617` 就是同刻同位、不同判定线的双押被
        合并成了一个 DOWN，真机上第二个必 Miss（实测丢 24 Miss + 18 Good = 20818 分）。

        `stable_only=True`（2026-10-21）：**本帧被挪过的指针只按 `chord_px` 的重合
        半径认**，其余指针仍按 `tangent_px`。理由见 `CHORD_COINCIDENT_RATIO`：
        一根刚被别的音符挪走的手指，位置已经被那个音符消费掉了，再拿 150px 的容差
        去顶第二个音符，等于宣称「一根手指同时服务两个相距 125px 的点」。
        """
        best, best_off = None, None
        for p in self.pointers:
            if not p.down or p.age < 0:
                continue
            if same_frame_only and (p.bound_at != self.now or p.click_claimed):
                continue
            thr = self.tangent_px
            if stable_only:
                thr = min(thr, self.presence_px)
            if stable_only and p.id in self.frame_busy:
                thr = self.chord_px
            off = tangent_offset((p.px, p.py), (px, py), angle_deg)
            # ★ Drag 专用：切向过了还要看**真实的 2D 距离**（法向也算），否则"贴边复用"
            #   会让手指停在离音符 20px 的地方而不补 MOVE。
            # ⚠ **指针紧张时自动放宽**（2026-10-25 22:5x）：`祈-我ら神祖…/AT` 因为这道 2D 闸门
            #   从 9 指顶到 10 指 ⇒ 抢正在长按的手指 ⇒ 真机"长 hold 按住了又松开"（12 Miss）。
            #   有手指可用时仍按 2D 严格判（moonlight 那种场景修得住的正是它）；全按满时才退回旧口径。
            if max_2d_px is not None and math.hypot(p.px - px, p.py - py) > max_2d_px:
                if not self._pointer_pressure():
                    continue
                self.presence_relaxed += 1
            if off < thr and (best_off is None or off < best_off):
                best, best_off = p, off
        return best

    # ------------------------------------------------------------ 服务

    def _serve_down(self, pend: _Pending, *, same_frame_ok: bool, hold_ms: int = 0) -> None:
        """Tap / Hold 起点 / Flick 起点：必须有一个 DOWN 落在判定区内。

        `same_frame_ok`：同一帧里**刚下发过**、且**尚未被认领**的 DOWN，只要它落在本
        音符判定区的切向阈值内，本音符就已被那个 DOWN 满足 —— 不再补发。

        这个「同帧顶替」只在按下**还没被吃掉**时成立。Phigros 是「一次点击只能判定
        一个 Tap/Hold」：`DESTRUCTION321/EZ` 那 16 个同像素同毫秒音符里 15 个是
        **Drag**（只要判定区内有手指，不吃点击）、1 个是 Hold，所以那次顶替合法；
        而两个 Tap 同帧就只能各按一次。Drag / Hold 持续段产生的按下保持未认领，
        谁都可以拿去判一个 Tap —— 那是真机上也存在的真实点击。
        """
        p = None
        if same_frame_ok:
            p = self._pick_for_presence(pend.px, pend.py, pend.angle_deg, same_frame_only=True)
        reused = p is not None
        if p is None and same_frame_ok:
            self._count_refused_share(pend)
        if p is None:
            p = self._pick_for_down(pend.px, pend.py)
            if p is None:
                self._fail(pend, "no_pointer")
                return
            self._press(p, pend.px, pend.py)
        if reused:
            self.reused += 1
        p.click_claimed = True        # 这一次点击被本音符吃掉了，不能再判第二个 Tap/Hold
        p.age = 0
        p.note_index = pend.note.index
        p.serve_until = max(p.serve_until, pend.t_ms)
        if hold_ms > 0:
            self._claim_hold(pend.note.index, p, pend.t_ms + hold_ms)
        self.serves.append(Serve(pend.stage, pend.note.index, pend.note.type, pend.t_ms,
                                 pend.px, pend.py, pend.angle_deg, p.id, reused))

    def _count_refused_share(self, pend: _Pending) -> None:
        """同帧确有按下落在本音符判定区里，但它已被别的 Tap/Hold 认领 ⇒ 只能另按一次。

        纯统计用途，只在「顶替失败」这条冷路径上跑，最多 10 次比较。
        """
        for q in self.pointers:
            if (q.down and q.bound_at == self.now and q.click_claimed
                    and tangent_offset((q.px, q.py), (pend.px, pend.py),
                                       pend.angle_deg) < self.tangent_px):
                self.share_refused += 1
                return

    def _own_pointer(self, pend: _Pending) -> Optional[_Pointer]:
        """本音符**自己上一毫秒用的那根手指** —— 可以「挪过去」，不必新开一根。

        2026-10-22 新增。没有它时 `presence_px` 一收紧就暴露一个缺陷：判定线移动
        让手指偏离音符超过阈值后，`_pick_for_down` 会因 `_is_keeper` 把**正在服务
        这个音符的那根手指**排除掉，于是永远走「新开一根」—— 老手指留在屏幕上
        **不再服务任何东西**，指针数虚增。实测 `EntrancetotheChaos/IN`
        `t=140146`：4 个 `hold_keep` 全换新手（ptr 1/2/7/8），旧 ptr 0/4/5/6
        空转 ⇒ 指针 4 → 9、峰值 4 → 9，而同刻真实需求仍是 4。

        只认这几类主人，且必须仍在屏幕上、本帧没被别的音符挪过：
          * `hold_keep`：`hold_owner[note]` 记的保持者（就是这个长按的当前手指）
          * `drag`：`p.note_index` 正是本音符的指针

        ⛔ 它若同时是**另一个进行中长按**的保持者，就不许挪 —— 那会断别人。
        """
        idx = pend.note.index
        cand: Optional[_Pointer] = None
        pid = self.hold_owner.get(idx)
        if pid is not None:
            cand = self.pointers[pid]
        else:
            for p in self.pointers:
                if p.down and p.age >= 0 and p.note_index == idx:
                    if cand is None or p.id < cand.id:
                        cand = p
        if cand is None or not cand.down or cand.age < 0:
            return None
        if cand.id in self.frame_busy:
            return None          # 本帧已经被别的音符挪走，位置已承诺给别人
        for n, end in self.hold_end.items():
            if n != idx and end > self.now and self.hold_owner.get(n) == cand.id:
                return None      # 还是别的长按的保持者，挪它会断按
        return cand

    def _finish_presence(self, pend: _Pending, p: _Pointer, hold_ms: int,
                         *, reused: bool) -> None:
        """收尾：把「已选定的指针」记到这个音符名下（必要时发 MOVE / DOWN）。"""
        if reused:
            self.reused += 1
        elif p.down:
            if hold_ms > 0:
                self._move_keep(p, pend.px, pend.py)
            else:
                self._move(p, pend.px, pend.py)
        else:
            self._press(p, pend.px, pend.py)
        p.age = 0
        p.note_index = pend.note.index
        p.serve_until = max(p.serve_until, pend.t_ms)
        if hold_ms > 0:
            self._claim_hold(pend.note.index, p, pend.t_ms + hold_ms)
        self.serves.append(Serve(pend.stage, pend.note.index, pend.note.type, pend.t_ms,
                                 pend.px, pend.py, pend.angle_deg, p.id, reused))

    def _serve_presence(self, pend: _Pending, hold_ms: int = 0) -> None:
        """Drag / Hold 持续：判定区内有手指即可。

        四道闸（2026-10-22 加第 ①.5 道，理由见 `_own_pointer`）：

        ① **稳定手指**：本帧没被挪过的指针，按 `presence_px`（默认 20px）认 ——
           贴着音符就算服务到了；本帧被挪过的，只按 `chord_px`（15px）认。
        ①.5 **本音符自己那根手指**：偏离超过阈值就把**它**挪过去。少了这一道，
           程序会一直新开手指，老手指空转（指针数虚增，`Entrance/IN` 4→9）。
        ② **给这个音符一根**：优先本帧未承诺的指针。
        ③ **真的没有针**（≤10 根全被本帧用掉 / 全在长按里）：借用本帧已承诺的那根，
           **显式记 `chord_refused`** —— 不许静默发生。
        """
        # ① 稳定手指（或几乎重合）
        #   ★ Drag 额外要求 2D 距离 ≤ DRAG_PRESENCE_2D_PX：否则"切向够近但法向差得多"
        #     会被误判成在区内（2026-10-25 末段 4 个 Drag 全丢的根因）。
        drag_2d = (self.drag_presence_2d_px
                   if (self.drag_presence_2d_px is not None and pend.note.type == 2)
                   else None)
        p = self._pick_for_presence(pend.px, pend.py, pend.angle_deg,
                                    stable_only=True, max_2d_px=drag_2d)
        if p is not None:
            self._reuse(pend, p, hold_ms)
            return

        # ①.5 本音符自己原先那根：挪过去，别新开一根
        if not self.own_pointer_off:
            own = self._own_pointer(pend)
            if own is not None:
                self._finish_presence(pend, own, hold_ms, reused=False)
                return

        # ② 一根自己的
        p = self._pick_for_down(pend.px, pend.py, avoid_busy=True)
        if p is None:
            # ③ 兜底：借一根本帧已承诺的，并记账
            q = self._pick_for_presence(pend.px, pend.py, pend.angle_deg)
            if q is not None:
                self.chord_refused += 1
                if len(self.chord_refused_detail) < 200:
                    self.chord_refused_detail.append(dict(
                        note=pend.note.index, type=pend.note.type_name, t_ms=pend.t_ms,
                        stage=pend.stage, pointer=q.id,
                        px=round(pend.px, 1), py=round(pend.py, 1),
                        finger_px=round(q.px, 1), finger_py=round(q.py, 1),
                        offset_px=round(tangent_offset((q.px, q.py), (pend.px, pend.py),
                                                       pend.angle_deg), 1)))
                self._reuse(pend, q, hold_ms)
                return
            self._fail(pend, "no_pointer")
            return

        self._finish_presence(pend, p, hold_ms, reused=False)

    def _reuse(self, pend: _Pending, p: _Pointer, hold_ms: int) -> None:
        """靠「已有手指落在判定区内」服务一个音符：一条事件都不发。"""
        self.reused += 1
        p.age = 0
        p.note_index = pend.note.index
        p.serve_until = max(p.serve_until, pend.t_ms)
        if hold_ms > 0:
            self._claim_hold(pend.note.index, p, pend.t_ms + hold_ms)
        self.serves.append(Serve(pend.stage, pend.note.index, pend.note.type, pend.t_ms,
                                 pend.px, pend.py, pend.angle_deg, p.id, True))

    def fit_radius(self, px: float, py: float, nx: float, ny: float):
        """Flick 扫动中心与半径，按画布边界调整（算法见 `fit_flick_sweep`）。

        **推中心优先、缩半径兜底**：收缩半径会把「部分越界但有位移」变成
        「不出界但零位移」⇒ Flick 必漏（2026-10-22 首版踩过，197 处 R=0）。

        ★ 底部边界用 `h × flick_bottom_safe_ratio`（**不是**画布底边 `h`）。
        画布高 1080 对应真机 y=720，而屏高 720 的半开区间是 `[0,720)` ⇒ 画布底边
        本身就在屏外；再往里 80px（真机 `640~719`）是 `mandatorySystemGestures`，
        夹进去会被系统当成 home 上滑（FACTS §126）。生产用 `639/720 = 0.8875`。
        """
        w, h = self.canvas
        h_avail = h * self.flick_bottom_safe_ratio
        cx, cy, r = fit_flick_sweep(px, py, nx, ny, self.flick_radius_px, w, h_avail)
        moved = math.hypot(cx - px, cy - py) > 1e-9
        if moved:
            self.flick_shifted += 1
            self.flick_shift_max = max(self.flick_shift_max, math.hypot(cx - px, cy - py))
        if r < self.flick_radius_px - 1e-9:
            self.flick_radius_shrunk += 1
            if self.flick_radius_min is None or r < self.flick_radius_min:
                self.flick_radius_min = r
            if r < 10.0:
                self.flick_radius_buckets["lt10"] += 1
            elif r < 30.0:
                self.flick_radius_buckets["lt30"] += 1
            elif r < 60.0:
                self.flick_radius_buckets["lt60"] += 1
            else:
                self.flick_radius_buckets["ge60"] += 1
            if r < FLICK_RADIUS_MIN_PX and len(self.warnings) < 40:
                self.warnings.append(
                    f"Flick 扫动半径被画布边界压到 {r:.1f}px"
                    f"（< {FLICK_RADIUS_MIN_PX:.0f}px）：中心 ({px:.0f},{py:.0f}) "
                    f"法向 ({nx:.2f},{ny:.2f}) ⇒ 扫速 ~{2.0 * r / self.flick_span_ms:.2f}px/ms，可能漏判")
        return cx, cy, r

    def _serve_flick(self, pend: _Pending) -> None:
        """Flick：按下之后沿判定线**法向**来回摩擦 ±R。

        两条要点：

        1. **同刻同位的双押共用一次扫动。** Flick 的判定是「判定区内出现快速位移」，
           两个判定区都覆盖同一条扫动时，一次摩擦同时满足两者 —— 与 Tap 的同刻复用
           同理。全量普查里 `祈-我ら神祖…/AT` 与 `DesultorySignals/AT` 各有 2 个
           同刻同位的 Flick，不共用就会把 10 个指针用满并失败。
        2. ★ **复用一根「已经按下」的指针时，先补一条「归位 MOVE」到音符中心**，
           再开始扫动。依据两套参考实现（phisap `algo2.py` L137–141 与
           `algo1.py` 的 `FLICK_START` 分支，二者同义）：

               if pointer.note is None:  DOWN(note.pos)     # 空闲指针 → 按下
               else:                     MOVE(note.pos)     # ★ 复用指针 → 先归位

           **为什么必须补**：扫动的第一条 MOVE 落在 `note.pos + R·n`（法向 +R）。
           若手指此刻**已经在那附近**，这一条的位移只有几 px，之后每条又只有
           `2R/SPAN`（现 R=90 画布 px 时约 3 px/ms），设备侧采到的基本是一串「几乎静止」的点 ⇒ Flick
           漏判。补一条到音符中心后，「中心 → 扫动起点」这一步**恒定产生 R 的位移**，
           把「首位移」的下界钉住。

           ⚠️ **2026-10-09 真机证据**：70MF IN 的 Miss#1 就是 `#72 Flick`，实测
           `jump`（手指 → 扫动起点）= **12.8** 画布 px —— 全曲 112 个 Flick 里
           **唯一** < 40 者（第二名 46.9、第三名 65.7，中间有明显断层）。
           全量 45531 个 Flick 的 jump 中位数 157.8、p5 = 42.9。

           ⚠️ 这条 MOVE 与扫动首帧**原本同刻**（参考实现 `algo2` 把两条都插进
           `events[now]`）。**2026-10-15 起改为扫动整体后移
           `FLICK_SWEEP_DELAY_MS`(=1) ms**，以拆散「同刻双押 Flick」挤在同一
           毫秒的 4 条 MOVE（真机取证见该常量处的注释）。
           它与「同 ms 同指针 UP+DOWN」不是一回事 —— 后者仍是禁止的（见 `_pick_for_down`）。
        """
        for q in self.pointers:
            sw = q.sweep
            if sw is None or sw[0] != self.now + FLICK_SWEEP_DELAY_MS:
                continue
            if (tangent_offset((sw[2], sw[3]), (pend.px, pend.py), pend.angle_deg) < self.tangent_px
                    and tangent_offset((sw[4], sw[5]), (pend.px, pend.py), pend.angle_deg)
                    < self.tangent_px):
                # ★★★ 2026-10-25 21:0x：**先让每个 Flick 各扫各的**，共用只作兜底。
                #   真机证据（moonlight IN）：开头 t=1.379 s 有两个同刻同位的 Flick，
                #   旧逻辑把它们都分给 pid0、只发一次扫动 ⇒ P1198 / Miss 1（连击 1197 自洽）。
                #   ⇒ 「一次扫动同时满足两个 Flick」在真机上不成立。
                #   ⚠ 兜底条件 **≥2 个空闲指针** 才独立扫（2026-10-25 22:5x 修）：
                #     `祈-我ら神祖…/AT` 用满 10 指、有 22 组同刻 Flick，只留 1 个空闲就去抢，
                #     会把正在长按的手指抢走 ⇒ 真机症状"长 hold 按住了又松开"。
                #     环境变量 `PGAUTO_FLICK_SHARE=1` 可强制回到"总是共用"（A/B 对照用）。
                idle = [c for c in self.pointers if (not c.down) and c.sweep is None]
                if _FLICK_SHARE.get("on") is False and len(idle) >= 2:
                    break                      # 有两个以上空闲指针 ⇒ 走独立扫动
                self.reused += 1
                q.serve_until = max(q.serve_until, sw[1])
                self.serves.append(Serve(pend.stage, pend.note.index, pend.note.type, pend.t_ms,
                                         pend.px, pend.py, pend.angle_deg, q.id, True))
                return

        p = self._pick_for_down(pend.px, pend.py)
        if p is None:
            self._fail(pend, "no_pointer")
            return
        if not p.down:
            self._press(p, pend.px, pend.py)
        else:
            # ★ 复用已按下的指针 ⇒ 先归位到音符中心（见 docstring 第 2 条）。
            # 归位用 `self.now`；扫动首帧 = `self.now + FLICK_SWEEP_DELAY_MS`
            # （★ 2026-10-15 格 A：原本同刻，现刻意隔开 1ms，见常量注释）。
            # 事件按 seq 排序 ⇒ 归位在前、扫动起点在后。
            self._move(p, pend.px, pend.py)
        p.age = 0
        p.note_index = pend.note.index

        ca, sa = tangent_dir(pend.angle_deg)
        nx, ny = -sa, ca                       # 法向 = 切向旋转 90°
        # ★ 边界处理：**把中心往里推**，不缩半径（见 `fit_flick_sweep`）。
        # 归位 MOVE 仍打到音符本体（手指必须先经过音符位置），扫动则围绕推过之后的中心。
        scx, scy, r = self.fit_radius(pend.px, pend.py, nx, ny)
        fx, fy = pend.px, pend.py
        lx, ly = pend.px, pend.py
        # ★★★ 2026-10-23：扫动 MOVE **降采样**（见 `FLICK_SWEEP_STRIDE_MS`）。
        #   旧行为 = `range(self.flick_span_ms)` 逐毫秒一条（60 条/次，间隔恒 1.0ms），
        #   把 PC→adb→scrcpy-server 通道（≈60 条/s）打满 ⇒ 其它音符 DOWN 被量化到
        #   16.9/33.7ms 网格 ⇒ 结算十几个 Good 全 Late。
        #   **首尾必须保留**：首条钉「归位→扫动起点」的恒定位移 R（Flick 识别下界），
        #   末条钉扫动终点（少了它终点差 (stride-1)/SPAN·R，可能扫不过判定区）。
        span = int(self.flick_span_ms)
        stride = max(1, int(self.flick_sweep_stride_ms))
        if span <= 0:
            deltas = [0]
        else:
            deltas = list(range(0, span, stride))
            if deltas[-1] != span - 1:
                deltas.append(span - 1)
        for delta in deltas:
            rate = 1.0 - 2.0 * delta / self.flick_span_ms
            ex, ey = scx + rate * r * nx, scy + rate * r * ny
            if delta == 0:
                fx, fy = ex, ey
            lx, ly = ex, ey
            self._ev(self.now + FLICK_SWEEP_DELAY_MS + delta, "move", p, ex, ey)
        p.px, p.py = lx, ly
        p.age = -self.flick_span_ms          # 滑动期间不可复用
        p.serve_until = max(p.serve_until, self.now + FLICK_SWEEP_DELAY_MS + self.flick_span_ms - 1)
        p.sweep = (self.now + FLICK_SWEEP_DELAY_MS,
                   self.now + FLICK_SWEEP_DELAY_MS + self.flick_span_ms - 1, fx, fy, lx, ly)
        self.serves.append(Serve(pend.stage, pend.note.index, pend.note.type, pend.t_ms,
                                 pend.px, pend.py, pend.angle_deg, p.id, False))

    def _fail(self, pend: _Pending, reason: str) -> None:
        self.unassigned.append(dict(note=pend.note.index, type=pend.note.type_name,
                                    t_ms=pend.t_ms, stage=pend.stage, reason=reason))

    # ------------------------------------------------------------ 主循环

    def run(self, frames: Dict[int, _Frame]) -> None:
        for t in sorted(frames):
            if self.last_t is not None:
                delta = t - self.last_t
                for p in self.pointers:
                    p.age += delta
            self.now = t
            self.frame_busy.clear()
            if self.hold_end:
                self._purge_holds()

            fr = frames[t]
            # 同刻组统计：这一毫秒要服务几个**不同音符**（hold_keep 同一音符只算一次）
            distinct = {x.note.index for x in fr.down} | {x.note.index for x in fr.flick} \
                | {x.note.index for x in fr.keep}
            if len(distinct) >= 2:
                self.chord_groups += 1
                self.chord_notes += len(distinct)
            for x in fr.down:
                self._serve_down(
                    x, same_frame_ok=True,
                    hold_ms=(int(math.ceil(x.note.hold_ms)) if x.stage == "hold_head" else 0))
            for x in fr.flick:
                self._serve_flick(x)
            for x in fr.keep:
                if x.stage == "hold_keep":
                    hold_ms = max(0, int(math.ceil(
                        x.note.t_ms + x.note.hold_ms - x.t_ms)))
                else:
                    hold_ms = 0
                self._serve_presence(x, hold_ms=hold_ms)

            self.last_t = t

        self.withdraw()

    def withdraw(self) -> None:
        """收尾：把所有还按着的指针抬起来。

        时刻必须取「最后一帧」与「所有待收指针的最后一条事件 / 服务」的**较晚者 + 1ms**
        —— 只按最后一帧算会切进仍在进行的 flick 扫动里，造出「UP 之后还有 MOVE」。

        ★ 2026-10-11（TAIL_RELEASE_PAD_MS）：t_end 从 `latest + 1` 改为
        `latest + TAIL_RELEASE_PAD_MS`——ap1 真机 5 Miss 定案：UP 与末尾 Drag
        判定时刻仅差 1.57ms，判定帧在 UP 之后查不到指针 ⇒ 压边的 Drag 丢。
        晚松对 Hold / Drag 均无惩罚（永久事实），推迟 100ms 零代价。

        ★ 2026-10-10（MIN_PRESS_MS）：还不得早于任何仍按着指针的
        「按下时刻 + MIN_PRESS_MS」—— 否则最后一帧刚按下的音符也会变成 1ms 短按。
        （2026-10-10 m2：已回退为 0，本延长现恒不生效。）
        """
        if self.last_t is None:
            return
        down = [p for p in self.pointers if p.down]
        latest = max([self.last_t] + [p.released_after for p in down])
        if MIN_PRESS_MS > 0 and down:
            latest = max(latest, max(p.bound_at + MIN_PRESS_MS for p in down))
        t_end = latest + TAIL_RELEASE_PAD_MS
        for p in down:
            self._ev(t_end, "up", p, p.px, p.py)
            p.down = False

    def events(self) -> List[TouchEvent]:
        """原始事件 → 按「同刻先释放、再按下、后移动」排序 → 定名。"""
        self.raw.sort(key=lambda e: (e[0], e[1], e[2]))
        out: List[TouchEvent] = []
        active = 0
        for t, _rank, _seq, action, pid, x, y in self.raw:
            if action == "down":
                name = "DOWN" if active == 0 else "POINTER_DOWN"
                active += 1
            elif action == "up":
                active -= 1
                name = "UP" if active == 0 else "POINTER_UP"
            else:
                name = "MOVE"
            out.append(TouchEvent(t, name, pid, x, y))
        return out


# ---------------------------------------------------------------- 端到端


def peak_concurrency(events: Sequence[TouchEvent]) -> int:
    """按事件流的**下发顺序**数同一时刻的最大活跃指针数。

    事件流已经排成「同刻先释放、再按下」，所以这就是 D 段会看到的并发峰值。
    """
    peak = 0
    active = 0
    for e in events:
        if e.action in ("DOWN", "POINTER_DOWN"):
            active += 1
            peak = max(peak, active)
        elif e.action in ("UP", "POINTER_UP"):
            active -= 1
    return peak


def build(
    notes: Sequence[Note],
    *,
    geometry: Any = None,
    canvas: Tuple[float, float] = CANVAS,
    max_pointers: int = MAX_POINTERS,
    tangent_threshold_ratio: float = TANGENT_THRESHOLD_RATIO,
    flick_lead_ms: int = FLICK_LEAD_MS,
    flick_span_ms: int = FLICK_SPAN_MS,
    flick_sweep_stride_ms: int = FLICK_SWEEP_STRIDE_MS,
    flick_radius_ratio: float = FLICK_RADIUS_RATIO,
    flick_retime: bool = True,
    clamp_offscreen: bool = False,
    presence_reuse_ratio: Optional[float] = None,
    drag_presence_2d_px: Optional[float] = None,
    own_pointer_off: bool = False,
    flick_bottom_safe_ratio: float = FLICK_BOTTOM_SAFE_RATIO,
) -> Schedule:
    """端到端：音符清单 → 指针规划 → 触摸事件序列。

    `flick_retime` / `clamp_offscreen` 是**屏幕外音符**的两级兜底（见 `build_frames`）：
    前者默认开（有参考实现依据），后者默认关（只保证可注入，不保证命中）。

    `own_pointer_off=True` 只用于**变异测试**：关掉 `_serve_presence` 的第 ①.5 道闸
    （「把本音符自己那根手指挪过去」），复现 2026-10-22 之前的缺陷
    ——「手指掉出阈值就新开一根、老手指空转」。生产代码不许传 True。
    """
    geom = geometry if geometry is not None else StaticGeometry(
        screen_w=canvas[0], screen_h=canvas[1])

    frames, frame_gaps, frames_stats = build_frames(
        notes, geom, flick_lead_ms=flick_lead_ms,
        flick_retime=flick_retime, clamp_offscreen=clamp_offscreen)
    planner = _Planner(
        max_pointers=max_pointers,
        canvas=canvas,
        tangent_px=tangent_threshold_ratio * canvas[0],
        flick_radius_px=flick_radius_ratio * canvas[0],
        flick_span_ms=flick_span_ms,
        flick_sweep_stride_ms=flick_sweep_stride_ms,
        chord_px=tangent_threshold_ratio * canvas[0] * CHORD_COINCIDENT_FACTOR,
        presence_px=(None if presence_reuse_ratio is None
                     else presence_reuse_ratio * canvas[0]),
        drag_presence_2d_px=drag_presence_2d_px,
        own_pointer_off=own_pointer_off,
        flick_bottom_safe_ratio=flick_bottom_safe_ratio,
    )
    planner.run(frames)
    events = planner.events()

    used = sorted({e.pointer_id for e in events})
    unassigned = list(frame_gaps) + planner.unassigned
    warnings: List[str] = []
    if planner.stolen:
        warnings.append(
            f"{planner.stolen} 次被迫抢占「正在服务长按」的指针 —— 该处长按可能出现瞬断")
    if frames_stats["retimed"]:
        warnings.append(
            f"{frames_stats['retimed']} 个 Flick 的判定时间被微调（±{flick_lead_ms}ms 内，"
            f"对齐 phisap 的「判定线闪烁」机制）")
    if frames_stats["clamped"]:
        warnings.append(
            f"{frames_stats['clamped']} 个判定点被拉到屏幕边缘 —— 事件流完整，但不保证命中")
    if frames_stats["hold_keep_clamped"]:
        warnings.append(
            f"{frames_stats['hold_keep_clamped']} 个长按持续帧的判定点扫出屏幕，"
            f"已按屏幕边缘继续按住（手指无法离开屏幕）")
    if frames_stats.get("flick_guard"):
        warnings.append(
            f"{frames_stats['flick_guard']} 个音符的服务链为避开 Flick 判定时刻被错峰 "
            f"（g3/g5 双轮真机定案的临界组合，判定窗内挪动零评级损失）")
    if frame_gaps:
        warnings.append(
            f"{len(frame_gaps)} 个音符的判定点在屏幕外且无可行投影，无法触碰")
    if planner.chord_refused:
        warnings.append(
            f"{planner.chord_refused} 个音符在同一毫秒里没有空闲指针，只能借用一根"
            f"已被别的音符挪走的手指（超出 {planner.chord_px:.0f}px 的重合半径）——"
            f"这些位置真机上大概率丢判定")

    return Schedule(
        serves=planner.serves,
        events=events,
        pointer_count=len(used),
        unassigned=unassigned,
        warnings=warnings,
        geometry="exact" if isinstance(geom, NotesGeometry) else "static",
        stats=dict(
            notes=len(notes),
            frames=len(frames),
            serves=len(planner.serves),
            events=len(events),
            peak_concurrency=peak_concurrency(events),
            pointer_ids=used,
            reuse=planner.reused,
            refused_share=planner.share_refused,
            chord_groups=planner.chord_groups,
            chord_notes=planner.chord_notes,
            chord_refused=planner.chord_refused,
            chord_coincident_px=round(planner.chord_px, 2),
            chord_refused_detail=planner.chord_refused_detail,
            presence_reuse_px=round(planner.presence_px, 2),
            flick_radius_px=round(planner.flick_radius_px, 2),
            flick_radius_shrunk=planner.flick_radius_shrunk,
            flick_radius_min=(None if planner.flick_radius_min is None
                              else round(planner.flick_radius_min, 2)),
            flick_radius_buckets=dict(planner.flick_radius_buckets),
            flick_shifted=planner.flick_shifted,
            flick_shift_max=round(planner.flick_shift_max, 2),
            projected_into_screen=frames_stats["projected"],
            retimed=frames_stats["retimed"],
            clamped=frames_stats["clamped"],
            hold_keep_clamped=frames_stats["hold_keep_clamped"],
            flick_guard=frames_stats.get("flick_guard", 0),
            flick_guard_moves=frames_stats.get("flick_guard_moves", []),
            downs=sum(1 for e in events if e.action in ("DOWN", "POINTER_DOWN")),
            moves=sum(1 for e in events if e.action == "MOVE"),
        ),
    )


def by_type(serves: Iterable[Serve]) -> Dict[str, int]:
    """按服务阶段统计次数（给工具与验收脚本共用）。"""
    out: Dict[str, int] = {}
    for s in serves:
        out[s.stage_name] = out.get(s.stage_name, 0) + 1
    return out
