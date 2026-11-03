#!/usr/bin/env python
"""G 段：端到端离线回放 —— 把 touches.json 灌进 Phigros 的**真实判定规则**，算理论得分。

与 `verify_schedule.py` 的分工：

    verify_schedule.py   布尔验收：「每个音符到底有没有被够到」——不区分 Perfect/Good
    replay_score.py      计分回放：「够到的质量如何、能不能 AP、扣分扣在哪」

两者都刻意**不 import pgauto**（只共享 `verify_schedule` 里那份独立几何），因此对
被测实现是同一层级的交叉验证。本工具额外把**两套判定模型**并排跑：

    宽松模型  判定区内有手指即可（= verify_schedule 的口径，也是 C 段设计时的自洽模型）
    严格模型  Tap / Hold 的起手必须**独占一次 DOWN**（= Phigros 的真实规则）

两套结果之差，就是「C 段的复用模型在真机上会丢多少分」。

── 判定规则（来源见文末） ────────────────────────────────────────────────

    音符     窗口                                    Good / Bad
    Tap      ±80ms Perfect；80~160 Good；160~180 Bad 有
    Hold     ±80ms Perfect；80~160 Good；**无 Bad**   只有 Good
    Drag     ±80ms Perfect                           无
    Flick    ±80ms Perfect                           无

    单次点击只能判定**一个** Tap/Hold，且**优先判最早的那个**可判音符。
    Hold 只要起手判到，随后全程有手指在判定区内即可（可提前 ~80ms 松手）。

    计分   ACC  = (Perfect + Good×0.65) / 物量
           得分 = round(900000 × ACC) + round(100000 × 最大连击 / 物量)
           评级 φ=1000000 / V(FC) / V≥960000 / S≥920000 / A≥880000 / B≥820000 / C≥700000

    本工具**不**模拟：判定线的「闪烁」（那是 C 段的 Flick 时间微调已吸收的物理现象）、
    长按的换手、以及 Bad 判定后音符残影的二次判定。

用法：
    python tools/replay_score.py [--notes out/notes.json] [--touches out/touches.json]
                                 [--flick-min-move 60] [--show 15] [--json out/score.json]
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from bisect import bisect_left, bisect_right
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# 与 verify_schedule.py 共享同一份**独立于 pgauto** 的几何与事件重放原语。
from verify_schedule import DOWN_ACTIONS, UP_ACTIONS, Geom  # noqa: E402

# ---------------------------------------------------------------- 规则表

PERFECT_MS = 80           # ±80ms → Perfect（所有音符）
GOOD_MS = 160             # 80~160ms → Good（**仅** Tap / Hold）
BAD_MS = 180              # 160~180ms → Bad（**仅** Tap；Hold 没有 Bad）
HOLD_RELEASE_TOL_MS = 80  # Hold 可以提前这么多毫秒松手
#: Hold 持续段**允许短暂离开判定区**而不算断。依据：fandom / 萌娘百科「Hold 音符可以
#: 短暂松开，这不会影响 Hold 的判定（据说时长是该 Hold 所在 Timing Group 的八分音符）」。
#: 八分音符在常见 BPM（150~250）下是 120~200ms，取 **80ms** 是**远严于该传闻**的保守值：
#: 它只吸收「判定线高速掠过导致的几毫秒抖动」（实测最长 17ms），不会把真正的断按放过。
#: ⚠️ 该容差的上游规则只有「据说」级别的来源，属**未定论**项，故取保守值并显式单列。
HOLD_ABSENCE_TOL_MS = 80
FLICK_MIN_MOVE_PX = 60.0  # Flick 认定所需的最小位移（画布像素）

GOOD_ACC = 0.65
LEAD_MS = 30              # Flick 扫动提前量（仅用于打印）

PERFECT, GOOD, BAD, MISS = "Perfect", "Good", "Bad", "Miss"
RANK_BY_SCORE = ((960000, "V"), (920000, "S"), (880000, "A"),
                 (820000, "B"), (700000, "C"), (0, "F"))
TYPE_NAMES = {1: "Tap", 2: "Drag", 3: "Hold", 4: "Flick"}


def outward(center: int, radius: int) -> Iterator[int]:
    """以 `center` 为中心向两侧扩散的整数序列：center, center±1, center±2, …

    判定窗口的扫描用它而不是 `range(lo, hi+1)` —— 判定点几乎总在 `center` 上命中，
    从中心出发能让常见情形一次就退出，而不是白扫 80 毫秒。
    """
    yield center
    for d in range(1, radius + 1):
        yield center - d
        yield center + d


@dataclass
class Verdict:
    index: int
    type_name: str
    t_ms: float
    judge: str
    #: Δt = 击打时刻 − 音符时刻，**有符号**（负 = 偏早，正 = 偏晚）。
    #: Flick 除外 —— 那一列存的是扫动幅度（恒 ≥ 0）。
    delta_ms: float
    reason: str = ""


@dataclass
class Score:
    label: str
    verdicts: List[Verdict] = field(default_factory=list)

    @property
    def counts(self) -> Dict[str, int]:
        c = {PERFECT: 0, GOOD: 0, BAD: 0, MISS: 0}
        for v in self.verdicts:
            c[v.judge] += 1
        return c

    @property
    def total(self) -> int:
        return len(self.verdicts)

    @property
    def accuracy(self) -> float:
        if not self.verdicts:
            return 0.0
        s = sum(1.0 if v.judge == PERFECT else GOOD_ACC if v.judge == GOOD else 0.0
                for v in self.verdicts)
        return s / len(self.verdicts)

    @property
    def max_combo(self) -> int:
        best = cur = 0
        for v in sorted(self.verdicts, key=lambda v: v.t_ms):
            if v.judge in (PERFECT, GOOD):
                cur += 1
                best = max(best, cur)
            else:
                cur = 0
        return best

    @property
    def score(self) -> int:
        n = max(1, self.total)
        return round(900000 * self.accuracy) + round(100000 * self.max_combo / n)

    @property
    def rank(self) -> str:
        c = self.counts
        if c[GOOD] == 0 and c[BAD] == 0 and c[MISS] == 0:
            return "φ (All Perfect)"
        if c[BAD] == 0 and c[MISS] == 0:
            return "V (Full Combo)"
        for lo, name in RANK_BY_SCORE:
            if self.score >= lo:
                return name
        return "F"

    @property
    def perfect(self) -> bool:
        return all(v.judge == PERFECT for v in self.verdicts)

    @property
    def bad(self) -> List[Verdict]:
        return [v for v in self.verdicts if v.judge != PERFECT]


# ---------------------------------------------------------------- 事件重放


class Replay:
    """把事件流整理成「任意时刻的活跃手指」与「按下事件清单」。

    手指模型与 `verify_schedule.Sim` 一致：**手指不动就停在原地**（只有 MOVE 才更新位置）。

    实现上把每根指针的轨迹压成**位置恒定的区段** `[t0, t1)`：
    DOWN / MOVE 切一刀，UP 收尾。查询「某时刻有哪些手指、在哪」就是每根指针二分一次
    —— O(指针数 · log 段数)。比「按毫秒预物化快照」省掉一个数量级的内存：
    全量 1064 谱面有 68 万音符，逐毫秒展开会是上亿个整数。
    """

    def __init__(self, events: Sequence[Dict[str, Any]], to_canvas,
                 geom: Geom, canvas: Tuple[float, float]):
        self.geom = geom
        self.canvas = canvas
        self.order = sorted(events, key=lambda e: float(e["t_ms"]))

        #: 所有「按下」事件：(t_ms, canvas_x, canvas_y)，按时间升序。
        self.downs: List[Tuple[float, float, float]] = []
        #: 每根指针的 MOVE 轨迹 (t_ms, x, y) —— Flick 判定用。
        self.traj: Dict[int, List[Tuple[float, float, float]]] = defaultdict(list)
        #: 每根指针的「位置恒定区段」[t0, t1) → (x, y)。
        segs: Dict[int, List[Tuple[float, float, float, float]]] = defaultdict(list)
        live: Dict[int, Tuple[float, float, float]] = {}     # pid → (t0, x, y)

        def cut(pid: int, t_end: float) -> None:
            t0, x, y = live[pid]
            if t_end > t0:
                segs[pid].append((t0, t_end, x, y))

        for e in self.order:
            t = float(e["t_ms"])
            pid, act = int(e["pointer_id"]), e["action"]
            x, y = to_canvas(float(e["x"]), float(e["y"]))
            if act in DOWN_ACTIONS:
                if pid in live:
                    cut(pid, t)
                live[pid] = (t, x, y)
                self.downs.append((t, x, y))
            elif act == "MOVE":
                if pid in live:
                    cut(pid, t)
                live[pid] = (t, x, y)
                self.traj[pid].append((t, x, y))
            elif act in UP_ACTIONS:
                if pid in live:
                    cut(pid, t)
                    del live[pid]
        for pid, (t0, x, y) in live.items():
            segs[pid].append((t0, float("inf"), x, y))

        self._segs = dict(segs)
        self._starts = {pid: [s[0] for s in rows] for pid, rows in segs.items()}
        self._down_times = [d[0] for d in self.downs]

    def fingers_at(self, ms: float) -> Dict[int, Tuple[float, float]]:
        """`ms` 时刻的活跃手指 `{pointer_id: (x, y)}`。"""
        out: Dict[int, Tuple[float, float]] = {}
        for pid, rows in self._segs.items():
            i = bisect_right(self._starts[pid], ms) - 1
            if i < 0:
                continue
            t0, t1, x, y = rows[i]
            if t0 <= ms < t1:
                out[pid] = (x, y)
        return out

    # ---- 判定原语

    def in_column(self, ms: float, line: int, position_x: float,
                  thresh: float) -> Optional[int]:
        """`ms` 时刻判定区内有没有手指；返回切向偏移最小的指针 id。"""
        pt = self.geom.point(line, position_x, ms)
        best, best_off = None, None
        for pid, pos in self.fingers_at(ms).items():
            off = self.geom.offset(pos, pt, line, ms)
            if off < thresh and (best_off is None or off < best_off):
                best, best_off = pid, off
        return best

    def downs_near(self, t: float) -> List[Tuple[int, float, float, float]]:
        """时间落在 `[t-BAD_MS, t+BAD_MS]` 内的按下事件（下标, t, x, y）。"""
        a = bisect_left(self._down_times, t - BAD_MS)
        b = bisect_right(self._down_times, t + BAD_MS)
        return [(i, *self.downs[i]) for i in range(a, b)]


# ---------------------------------------------------------------- 判定


def _grade_by_delta(d: float, type_name: str) -> str:
    """按 |Δt| 给判定。Hold 没有 Bad（超出 Good 直接 Miss）。"""
    if d <= PERFECT_MS:
        return PERFECT
    if d <= GOOD_MS:
        return GOOD
    if type_name == "Tap" and d <= BAD_MS:
        return BAD
    return MISS


def _flick_evidence(rp: Replay, line: int, px: float, t: float,
                    thresh: float, lead: int) -> Tuple[float, bool]:
    """Flick 的扫动证据 → `(窗口内最大位移, 是否在判定带内划过)`。

    **两件事必须分开量，不能取交集：**

    1. `offset_ok` —— 扫动窗口里**有没有**点落在判定带内（`≥1` 个即可）。这是
       「在判定区域内滑动」这条规则。
    2. `span` —— 该指针在窗口内的**最大两点距离**，**不按带内过滤**。这是「滑动」
       这条规则：手指的位移幅度。

    为什么不把 `span` 限制在带内的点上：判定线会在**事件边界瞬变/高速掠过**，扫动
    窗口内判定点可以跳几百到几千像素（实测 186~3491px）。此时手指在**音符时刻**正好
    落在判定点上、位移也完整（88.5px），但扫动只有一半落在「那一刻的判定带」里 —— 按
    带内量只能量到 39~57px，凭空造出 61 个假 Miss（该批的「总位移」全部是 87~88.5px）。

    这条设计不是我们自创：参考实现 phisap `algo/algo1.py` 的
    `FLICK_START=-30 / FLICK_END=30 / FLICK_RADIUS=30`（其屏幕为 1280×720，与我们的
    1920×1080 同比），扫动同样是**跨越音符时刻对称铺开**、手指在音符时刻经过判定点。
    游戏侧规则也只写「在判定区域内向任意方向滑动」，没有「全程都在区内」这种要求。

    窗口取 `[t - lead, t + lead]`（扫动窗口），不是 ±80ms 的判定窗 —— 后者会把该指针为
    **别的音符**发的 MOVE 圈进来。
    """
    span = 0.0
    offset_ok = False
    for pts in rp.traj.values():
        win = [p for p in pts if t - lead <= p[0] <= t + lead]
        if len(win) < 2:
            continue
        if not offset_ok and any(
                rp.geom.offset((p[1], p[2]), rp.geom.point(line, px, p[0]),
                               line, p[0]) < thresh for p in win):
            offset_ok = True
        for a in range(len(win)):
            for b in range(a + 1, len(win)):
                d = math.hypot(win[a][1] - win[b][1], win[a][2] - win[b][2])
                if d > span:
                    span = d
    return span, offset_ok


def _hold_break(rp: Replay, line: int, px: float, thresh: float,
                head: int, tail: int,
                tol: int = HOLD_ABSENCE_TOL_MS) -> Tuple[Optional[int], int]:
    """长按持续段 `[head, tail]` 里「离开判定区最久的那一段」→ `(段起点, 段长)`。

    只有**连续**缺席超过 `tol` 才算断（没超时第一项给 `None`）。逐毫秒地要求
    「必须一直在区内」会把判定线高速掠过时的几毫秒抖动误判成断按 —— 实测 7 处，
    最长连续缺席只有 17ms，而 Hold 本身长 366~4115ms。
    """
    longest = 0
    longest_at: Optional[int] = None
    run = 0
    start = head
    for ms in range(head, tail + 1):
        if rp.in_column(ms, line, px, thresh) is None:
            if run == 0:
                start = ms
            run += 1
            if run > longest:
                longest, longest_at = run, start
        else:
            run = 0
    return (longest_at, longest) if longest > tol else (None, longest)


def judge_strict(notes: Sequence[Dict[str, Any]], rp: Replay, thresh: float,
                 flick_min: float, lead: int) -> List[Verdict]:
    """严格模型：Tap / Hold 的起手必须**独占一次 DOWN**（Phigros 真实规则）。"""
    out: List[Verdict] = [None] * len(notes)   # type: ignore[list-item]

    # ---- 先判不消耗「点击预算」的音符（Drag / Flick）
    for i, n in enumerate(notes):
        typ = int(n["type"])
        if typ in (1, 3):
            continue
        t = float(n["t_ms"])
        line = int(n["line"])
        px = float(n["position_x"])
        name = n.get("type_name") or TYPE_NAMES[typ]

        if typ == 2:                                    # Drag：有手指即可
            hit = None
            for ms in outward(int(round(t)), PERFECT_MS):
                if rp.in_column(ms, line, px, thresh) is not None:
                    hit = ms
                    break
            out[i] = (Verdict(i, name, t, PERFECT, hit - t, "") if hit is not None
                      else Verdict(i, name, t, MISS, 0.0, "判定窗口内判定区无手指"))
            continue

        span, offset_ok = _flick_evidence(rp, line, px, t, thresh, lead)   # Flick
        if not offset_ok:
            out[i] = Verdict(i, name, t, MISS, 0.0, "判定窗口内无扫动落点")
        elif span < flick_min:
            out[i] = Verdict(i, name, t, MISS, span,
                             f"扫动幅度 {span:.1f} < {flick_min:g} 画布 px")
        else:
            out[i] = Verdict(i, name, t, PERFECT, 0.0, "")

    # ---- Tap / Hold 起手：一次点击只能判一个，且优先判最早的那个
    heads = sorted((i for i in range(len(notes)) if int(notes[i]["type"]) in (1, 3)),
                   key=lambda i: float(notes[i]["t_ms"]))
    used = [False] * len(rp.downs)

    for i in heads:
        n = notes[i]
        t = float(n["t_ms"])
        line = int(n["line"])
        px = float(n["position_x"])
        is_hold = int(n["type"]) == 3
        name = n.get("type_name") or TYPE_NAMES[int(n["type"])]

        best_i: Optional[int] = None
        best_mag: Optional[float] = None    # |Δt|：只用于「取最小」与分级
        best_d = 0.0                        # Δt = 按下时刻 − 音符时刻（**有符号**，供展示 / --json）
        best_t = t
        for di, dt, dx, dy in rp.downs_near(t):
            if used[di]:
                continue
            d = abs(dt - t)
            if best_mag is not None and d >= best_mag:
                continue
            # 这次按下必须落在判定带内，否则它够不到本音符
            if rp.geom.offset((dx, dy), rp.geom.point(line, px, dt), line, dt) >= thresh:
                continue
            # ★ 2026-10-16 修复：此处原把 `d = abs(dt - t)` 直接存进 `best_d`，
            #   于是 `Verdict.delta_ms` 恒 ≥ 0 ⇒ **严格模型永远报不出「偏早」**，
            #   而 `dump()` 用 `{:+.3f}` 打印、`--json` 原样导出 ⇒ 抢拍会被显示成 `+Δ`（看似偏晚）。
            #   同文件的 Drag 分支与宽松模型用的都是有符号 `hit - t`，此处补上一致性。
            #   取最小与分级仍用幅值 `best_mag`（`_grade_by_delta` 的语义就是 |Δt|）。
            best_i, best_mag, best_d, best_t = di, d, dt - t, dt

        if best_i is None:
            out[i] = Verdict(i, name, t, MISS, 0.0,
                             "窗口内没有独立的按下（点击被更早的音符占走了）")
            continue

        used[best_i] = True
        assert best_mag is not None
        judge = _grade_by_delta(best_mag, name)

        if is_hold and judge != MISS:
            # 持续段从**起手按下那一刻**算起，不是从音符时刻算起：起手迟了 80ms 时，
            # 音符时刻那一刻手指还没按下去，从那里查必然断。
            head = int(math.ceil(best_t))
            c = int(round(t))
            hold_ms = int(math.ceil(float(n.get("hold_ms", 0.0))))
            tail = c + hold_ms - HOLD_RELEASE_TOL_MS
            broken, run = _hold_break(rp, line, px, thresh, head, tail)
            if broken is not None:
                out[i] = Verdict(i, name, t, MISS, best_d,
                                 f"长按在 t+{broken - c}ms 处断开"
                                 f"（连续 {run}ms 判定区内无手指）")
                continue

        out[i] = Verdict(i, name, t, judge, best_d, "")

    assert all(v is not None for v in out), "有音符没有被判定"
    return out


def judge_lenient(notes: Sequence[Dict[str, Any]], rp: Replay, thresh: float,
                  flick_min: float, lead: int) -> List[Verdict]:
    """宽松模型：只要「判定区内有手指」就算够到（= verify_schedule 的口径）。

    Tap 不要求是**新的**按下 —— 一根早已按住的手指也算数。这正是 C 段的复用假设。
    """
    out: List[Verdict] = []
    for i, n in enumerate(notes):
        typ = int(n["type"])
        t = float(n["t_ms"])
        line = int(n["line"])
        px = float(n["position_x"])
        name = n.get("type_name") or TYPE_NAMES[typ]

        if typ == 4:
            span, ok = _flick_evidence(rp, line, px, t, thresh, lead)
            out.append(Verdict(i, name, t,
                               PERFECT if (ok and span >= flick_min) else MISS,
                               span, ""))
            continue

        hit = next((ms for ms in outward(int(round(t)), PERFECT_MS)
                    if rp.in_column(ms, line, px, thresh) is not None), None)
        if hit is None:
            out.append(Verdict(i, name, t, MISS, 0.0, "判定窗口内判定区无手指"))
            continue

        if typ == 3:
            head = int(math.ceil(hit))
            c = int(round(t))
            hold_ms = int(math.ceil(float(n.get("hold_ms", 0.0))))
            tail = c + hold_ms - HOLD_RELEASE_TOL_MS
            broken, run = _hold_break(rp, line, px, thresh, head, tail)
            if broken is not None:
                out.append(Verdict(i, name, t, MISS, 0.0,
                                   f"长按在 t+{broken - c}ms 处断开"
                                   f"（连续 {run}ms 判定区内无手指）"))
                continue

        out.append(Verdict(i, name, t, PERFECT, hit - t, ""))
    return out


# ---------------------------------------------------------------- 输出


def dump(sc: Score, show: int, indent: str = "  ") -> None:
    c = sc.counts
    print(f"{indent}判定   Perfect {c[PERFECT]} / Good {c[GOOD]} / "
          f"Bad {c[BAD]} / Miss {c[MISS]}   （物量 {sc.total}）")
    print(f"{indent}ACC    {100.0 * sc.accuracy:.4f}%      最大连击 {sc.max_combo} / {sc.total}")
    print(f"{indent}得分   {sc.score} → 评级 {sc.rank}")
    bad = sc.bad
    if bad and show:
        bad = sorted(bad, key=lambda v: (-(v.judge == MISS), v.t_ms))
        print(f"{indent}非 Perfect 明细（前 {min(show, len(bad))} / 共 {len(bad)}）：")
        for v in bad[:show]:
            extra = f"  ← {v.reason}" if v.reason else ""
            print(f"{indent}  #{v.index:<5} {v.type_name:<5} t={v.t_ms:>10.3f}ms  "
                  f"{v.judge:<7} Δ={v.delta_ms:+.3f}ms{extra}")
    elif not bad:
        print(f"{indent}非 Perfect 明细   无 —— 全 Perfect")


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="touches.json 端到端离线回放判分")
    ap.add_argument("--notes", default=str(PROJECT_ROOT / "out" / "notes.json"))
    ap.add_argument("--touches", default=str(PROJECT_ROOT / "out" / "touches.json"))
    ap.add_argument("--flick-min-move", type=float, default=FLICK_MIN_MOVE_PX)
    ap.add_argument("--show", type=int, default=15, help="每套模型打印多少条非 Perfect 明细")
    ap.add_argument("--json", help="把结果写成 JSON")
    args = ap.parse_args(argv)
    flick_min = args.flick_min_move

    notes_doc = json.loads(Path(args.notes).read_text(encoding="utf-8"))
    touch_doc = json.loads(Path(args.touches).read_text(encoding="utf-8"))

    notes = notes_doc["notes"]
    events = touch_doc["events"]

    # ── F4（2026-10-13）时基归正 ────────────────────────────────────────────
    # touches.events 是**音乐时基**（谱面 t_sec + 谱面 offset），notes 是**谱面时基**。
    # 本工具按 notes 的 t_ms 判窗 ⇒ 先把 events 减掉谱面 offset 归正，否则整曲错位。
    _off_ms = float(touch_doc.get("offset_applied_ms") or 0.0)
    if _off_ms:
        events = [dict(e, t_ms=float(e["t_ms"]) - _off_ms) for e in events]
        print(f"  时基归正：events 减 {_off_ms:+.1f} ms（谱面 offset）⇒ 与 notes 同步为谱面时基")
    cw = float(touch_doc["canvas"]["width"])
    ch = float(touch_doc["canvas"]["height"])
    sw = float(touch_doc["screen"]["width"])
    sh = float(touch_doc["screen"]["height"])
    scale = sw / cw
    y_off = (sh - ch * scale) / 2.0

    def to_canvas(x: float, y: float) -> Tuple[float, float]:
        return x / scale, (y - y_off) / scale

    ratio = float(touch_doc.get("stats", {}).get("tangent_threshold_ratio", 100.0 / 1280.0))
    thresh = ratio * cw
    lead = int(touch_doc.get("stats", {}).get("flick_lead_ms", LEAD_MS))

    geom = Geom(notes_doc, (cw, ch))
    if not geom.available:
        print("⚠️ notes.json 缺 `lines` 段，无法算判定点 —— 需要重跑 B 段", file=sys.stderr)
        return 2

    rp = Replay(events, to_canvas, geom, (cw, ch))

    print("=" * 78)
    print("端到端离线回放（G 段）")
    print("=" * 78)
    print(f"  谱面    {touch_doc.get('chart')}")
    print(f"  几何    {touch_doc.get('geometry')}   切向阈值 {thresh:.2f} 画布 px")
    print(f"  物量 {len(notes)}   事件 {len(events)}   按下 {len(rp.downs)} 次"
          f"   Flick 提前量 {lead}ms")
    print(f"  规则    Perfect ≤{PERFECT_MS}ms / Good ≤{GOOD_MS}ms / Bad ≤{BAD_MS}ms（仅 Tap）"
          f" / Hold 可提前 {HOLD_RELEASE_TOL_MS}ms 松手、"
          f"可离开判定区 ≤{HOLD_ABSENCE_TOL_MS}ms / Flick 最小扫动 {flick_min:g}px")
    print()

    print("=" * 78)
    print("[1] 严格模型（Phigros 真实规则：Tap/Hold 起手必须独占一次按下）")
    print("=" * 78)
    strict = Score("严格模型", judge_strict(notes, rp, thresh, flick_min, lead))
    dump(strict, args.show)

    print()
    print("=" * 78)
    print("[2] 宽松模型（判定区内有手指即可 —— verify_schedule 的口径）")
    print("=" * 78)
    lenient = Score("宽松模型", judge_lenient(notes, rp, thresh, flick_min, lead))
    dump(lenient, args.show)

    print()
    print("=" * 78)
    print("[3] 两套模型的差 = C 段复用模型在真机上的风险敞口")
    print("=" * 78)
    diff = strict.score - lenient.score
    print(f"  得分  {strict.score}（严格） vs {lenient.score}（宽松）   差 {diff:+d}")
    if diff == 0:
        print("  结论  本谱面上两套模型**完全一致** —— 它的复用没有踩到"
              "「一次点击只能判一个 Tap/Hold」这条规则")
        print("        （要判断这个缺陷在全量上的分布，用 "
              "`tools/sweep_charts.py --score`）")
    else:
        print(f"  结论  严格模型低 {abs(diff)} 分 —— 有 "
              f"{strict.counts[MISS] - lenient.counts[MISS]} 个音符在宽松模型下算命中、"
              f"真机上会判 Miss")
    if strict.perfect:
        print("  ⇒ 严格模型下 **全 Perfect**，理论 AP（φ）可行。")

    if args.json:
        payload = {
            "chart": touch_doc.get("chart"),
            "note_count": len(notes),
            "rules": {"perfect_ms": PERFECT_MS, "good_ms": GOOD_MS, "bad_ms": BAD_MS,
                      "hold_release_tol_ms": HOLD_RELEASE_TOL_MS,
                      "hold_absence_tol_ms": HOLD_ABSENCE_TOL_MS,
                      "flick_min_move_px": flick_min},
            "strict": {
                "score": strict.score, "rank": strict.rank,
                "accuracy": strict.accuracy, "max_combo": strict.max_combo,
                "counts": strict.counts,
                "worst": [{"note": v.index, "type": v.type_name, "t_ms": v.t_ms,
                           "judge": v.judge, "delta_ms": v.delta_ms, "reason": v.reason}
                          for v in strict.bad[:200]],
            },
            "lenient": {"score": lenient.score, "rank": lenient.rank,
                        "accuracy": lenient.accuracy, "max_combo": lenient.max_combo,
                        "counts": lenient.counts},
        }
        out = Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  [输出] {out}")

    return 1 if strict.counts[MISS] else 0


if __name__ == "__main__":
    raise SystemExit(main())
