#!/usr/bin/env python
"""C 段交付物（touches.json）的独立验收。

**刻意不 import pgauto** —— 只读两个文件，把 C 段的判定模型用**另一条代码路径**
重跑一遍：

    notes.json     B 段产出：音符 + 判定线几何事件（**原样**带出，未解码）
    touches.json   C 段产出：事件流 + 规划明细（plan）

四组判据：

    [1] 事件流合法性 —— 指针数 / 并发上限 / DOWN·UP 配对 / MOVE 只在按住期间 /
        时间单调 / 坐标落在画布内
    [2] 判定仿真（核心）—— 独立解码判定线几何 → 独立算「判定点」像素坐标 →
        用事件流推出每根手指的**影子位置**（手指不动就一直停在原地）→ 对每个音符
        按判定规则判定它到底有没有被打到：
            Tap    时刻处判定区内有手指（★ 与 C 段的**指针复用模型**同口径：
                   一根早已按住的手指也算数。是否要求「**新按下**」——
                   即「一次点击只能判一个 Tap」——由 `tools/replay_score.py`
                   的**严格模型**负责，不在本工具职责内）
            Hold   [t, t+hold] **每一毫秒**判定区内都有手指
            Drag   时刻处判定区内有手指（不需要 DOWN）
            Flick  [t-lead, t+lead] 内判定区内出现幅度足够的位移
    [3] 规划对拍 —— plan 里每条记录在该时刻确有指针按下、且落在判定区内
    [4] 规模统计

⚠️ **一处必须说清的边界**：判定区的**切向半宽**（音符有多宽）我没有权威数值，
    只能沿用参考实现 phisap 的 `100/1280`（它据此 AP 全部曲目 ⇒ 该值不超过真实宽度）。
    所以 [2] 的第一遍用它做**自洽**检查（"C 段有没有兑现自己的模型"），
    另外再用**更严的阈值**（默认 0.5 倍）跑一遍，把**余量**摊出来给读者判断。
    唯一硬断言的是：位移/按下/持续这些**模型无关**的语义。

用法：
    python tools/verify_schedule.py [--notes out/notes.json] [--touches out/touches.json]
                                    [--strict-ratio 0.5]
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from bisect import bisect_right
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[1]

DOWN_ACTIONS = ("DOWN", "POINTER_DOWN")
UP_ACTIONS = ("UP", "POINTER_UP")

#: 事件定位的容差（谱面时间单位）。
#: 音符的时刻与判定线事件的边界**必然重合**（都由整数单位表示），而 notes.json 把
#: `t_ms` 序列化成 4 位小数，反推回单位时带约 5e-6 的误差；判定线又在边界处**瞬变**，
#: 这点误差足以取到瞬变**之前**的那条事件 —— 实测判定点最大偏 87.5 像素。
#: 与 `pgauto.schedule._LOCATE_EPS` 取同一个值（1e-4 单位 ≈ 0.001ms）。
_LOCATE_EPS = 1e-4

STAGE_NAMES = {0: "Tap", 1: "Hold.start", 2: "Hold.keep", 3: "Drag", 4: "Flick"}
HEAD_STAGES = {0, 1, 3, 4}

PASS: List[str] = []
FAIL: List[str] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    (PASS if ok else FAIL).append(f"{name}{('  ' + detail) if detail else ''}")
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{('  ' + detail) if detail else ''}")
    return ok


def load(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- 几何（独立实现）


class LineGeom:
    """一条判定线的几何：位置与角度随「谱面时间单位」的线性插值。

    事件首尾相接铺满时间轴（官谱的不变量），所以定位就是「包含 t 的那一条」。
    formatVersion 1 把 x / y 打包成 `1000·x + y`（x 值域 0..880、y 值域 0..520），
    这里独立还原成 [0,1]；formatVersion 3 本来就分开。
    """

    __slots__ = ("bpm", "_m", "_r", "_ms", "_rs")

    def __init__(self, entry: Dict[str, Any], fmt: int):
        self.bpm = float(entry.get("bpm", 1.0)) or 1.0
        rows = []
        for ev in entry.get("move_events", []):
            st, en, s, e = float(ev[0]), float(ev[1]), float(ev[2]), float(ev[3])
            if fmt == 1:
                rows.append((st, en,
                             math.floor(s / 1000.0) / 880.0, math.floor(e / 1000.0) / 880.0,
                             (s % 1000.0) / 520.0, (e % 1000.0) / 520.0))
            else:
                rows.append((st, en, s, e,
                             float(ev[4]) if len(ev) > 4 else 0.0,
                             float(ev[5]) if len(ev) > 5 else 0.0))
        self._m = rows
        self._ms = [r[0] for r in rows]
        rot = [(float(ev[0]), float(ev[1]), float(ev[2]), float(ev[3]))
               for ev in entry.get("rotate_events", [])]
        self._r = rot
        self._rs = [r[0] for r in rot]

    def unit(self, ms: float) -> float:
        """毫秒 → 谱面时间单位（`chart` 里的 `time`）。"""
        return ms * self.bpm / 1875.0

    def pos01(self, ms: float) -> Tuple[float, float]:
        u = self.unit(ms)
        # 定位用 u + eps（跨过边界取**新**事件），插值仍用原始 u。
        i = bisect_right(self._ms, u + _LOCATE_EPS) - 1
        if i < 0 or i >= len(self._m):
            return 0.5, 0.5
        st, en, sx, ex, sy, ey = self._m[i]
        k = 0.0 if en == st else (u - st) / (en - st)
        return sx + (ex - sx) * k, sy + (ey - sy) * k

    def angle_deg(self, ms: float) -> float:
        u = self.unit(ms)
        i = bisect_right(self._rs, u + _LOCATE_EPS) - 1
        if i < 0 or i >= len(self._r):
            return 0.0
        st, en, s, e = self._r[i]
        k = 0.0 if en == st else (u - st) / (en - st)
        return s + (e - s) * k


class Geom:
    """判定点（像素，画布空间）与切向方向的独立计算。"""

    def __init__(self, payload: Dict[str, Any], canvas: Tuple[float, float]):
        self.lines = [LineGeom(ln, int(payload.get("format_version", 3)))
                      for ln in payload.get("lines", [])]
        self.aspect = float(payload.get("aspect_ratio", 16.0 / 9.0))
        self.w, self.h = canvas
        self.x_unit = 2.0 * 9.0 / 160.0

    @property
    def available(self) -> bool:
        return bool(self.lines)

    def point(self, line: int, position_x: float, ms: float) -> Tuple[float, float]:
        """判定线上 `positionX` 处、t 时刻的**判定点**（画布像素）。

        与 B 段 `chart.note_pos(dy=0)` 同源，但这里从事件值直接推：
            x01, y01 = 事件归一化值
            lx = -1 + 2·x01 ; ly = (-1 + 2·y01) / aspect
            世界 = (lx + base·cosθ, ly + base·sinθ)   base = positionX · 0.1125
            像素 = (W/2 + wx·W/2, H/2 - wy·W/2)
        """
        g = self.lines[line]
        x01, y01 = g.pos01(ms)
        lx = -1.0 + x01 * 2.0
        ly = (-1.0 + y01 * 2.0) / self.aspect
        base = position_x * self.x_unit
        rad = math.radians(g.angle_deg(ms))
        wx = lx + base * math.cos(rad)
        wy = ly + base * math.sin(rad)
        half = self.w / 2.0
        return half + wx * half, self.h / 2.0 - wy * half

    def tangent(self, line: int, ms: float) -> Tuple[float, float]:
        a = math.radians(self.lines[line].angle_deg(ms))
        return math.cos(a), -math.sin(a)

    def offset(self, finger: Tuple[float, float], point: Tuple[float, float],
               line: int, ms: float) -> float:
        ca, sa = self.tangent(line, ms)
        return abs((finger[0] - point[0]) * ca + (finger[1] - point[1]) * sa)

    def reachable(self, point: Tuple[float, float], line: int, ms: float,
                  thresh: float) -> bool:
        """判定区与屏幕**有没有交集** —— 即此刻存在屏幕内的手指能判到它吗。

        判定区是「沿判定线法向无限长、切向半宽 = thresh」的带。屏幕矩形在切向方向上的
        投影是一个区间（凸集 ⇒ 区间上每一点都能在屏幕内取到）。两者相交 ⇔ 该带扫到屏幕。

        返回 False 就说明这个音符此刻**数学上够不着**：任何屏幕内的位置都不在判定区内。
        验收对这类检查点只要求「确实有手指按在屏幕上」—— 手指无法离开屏幕，这是唯一
        可退的位置（长按持续段扫出屏幕时，C 段就是这么处理的）。
        """
        ca, sa = self.tangent(line, ms)
        corners = ((0.0, 0.0), (self.w, 0.0), (0.0, self.h), (self.w, self.h))
        proj = [x * ca + y * sa for x, y in corners]
        s = point[0] * ca + point[1] * sa
        return min(proj) - thresh <= s <= max(proj) + thresh


# ---------------------------------------------------------------- 判定仿真


class Sim:
    """用事件流重放每根手指的位置（手指不动就停在原地）。"""

    __slots__ = ("state", "peak", "downs", "ups", "moves")

    def __init__(self) -> None:
        self.state: Dict[int, Tuple[float, float]] = {}
        self.peak = 0
        self.downs = 0
        self.ups = 0
        self.moves = 0

    def apply(self, ev: Dict[str, Any], to_canvas) -> None:
        pid = int(ev["pointer_id"])
        act = ev["action"]
        x, y = to_canvas(float(ev["x"]), float(ev["y"]))
        if act in DOWN_ACTIONS:
            self.state[pid] = (x, y)
            self.downs += 1
            self.peak = max(self.peak, len(self.state))
        elif act == "MOVE":
            self.state[pid] = (x, y)
            self.moves += 1
        elif act in UP_ACTIONS:
            self.state.pop(pid, None)
            self.ups += 1

    def near(self, point: Tuple[float, float], line: int, ms: float,
             geom: Geom, thresh: float) -> Optional[int]:
        """判定区内有没有手指；返回切向偏移最小的那根指针 id。"""
        best, best_off = None, None
        for pid, pos in self.state.items():
            off = geom.offset(pos, point, line, ms)
            if off < thresh and (best_off is None or off < best_off):
                best, best_off = pid, off
        return best


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="touches.json 独立验收")
    ap.add_argument("--notes", default=str(PROJECT_ROOT / "out" / "notes.json"))
    ap.add_argument("--touches", default=str(PROJECT_ROOT / "out" / "touches.json"))
    ap.add_argument("--strict-ratio", type=float, default=0.5,
                    help="额外用「阈值 × 该系数」再跑一遍，摊出余量（默认 0.5）")
    ap.add_argument("--flick-min-move", type=float, default=60.0,
                    help="Flick 认定所需的最小位移（画布像素，默认 60）")
    args = ap.parse_args(argv)

    notes_doc = load(Path(args.notes))
    touch_doc = load(Path(args.touches))

    notes = notes_doc["notes"]
    events = touch_doc["events"]
    plan = touch_doc.get("plan") or []

    # ── F4（2026-10-13）时基归正 ────────────────────────────────────────────
    # touches 的 events/plan 是**音乐时基**（谱面 t_sec + 谱面 offset），而 notes 是
    # **谱面时基**。本工具全程拿 notes 的 t_ms 当基准比对 ⇒ 先把 events/plan 减掉
    # 谱面 offset 平移回谱面时基。漏做会静默误判（容差仅 ±1 ms）。
    _off_ms = float(touch_doc.get("offset_applied_ms") or 0.0)
    if _off_ms:
        events = [dict(e, t_ms=float(e["t_ms"]) - _off_ms) for e in events]
        plan = [[r[0], r[1], float(r[2]) - _off_ms, *r[3:]] for r in plan]
        print(f"  时基归正：events/plan 减 {_off_ms:+.1f} ms（谱面 offset）"
              f"⇒ 与 notes 同步为谱面时基")
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
    max_pointers = int(touch_doc.get("stats", {}).get("max_pointers", 10))
    lead = int(touch_doc.get("stats", {}).get("flick_lead_ms", 30))
    # Flick 同刻保护（schedule.py 的 guard pass）：被错峰的音符，其服务链整体
    # 平移到 to_t —— 对拍以 to_t 为期望正点，而不是音符的 floor(t_ms)。
    guard = {int(idx): (int(ft), int(tt)) for idx, ft, tt in
             (touch_doc.get("stats", {}).get("flick_guard_moves") or [])}

    print("=" * 78)
    print("[0] 交付物自述")
    print("=" * 78)
    print(f"  谱面        {touch_doc.get('chart')}")
    print(f"  几何        {touch_doc.get('geometry')}")
    print(f"  画布        {cw:g} x {ch:g}    设备 {sw:g} x {sh:g}")
    print(f"  音符 {len(notes)}   事件 {len(events)}   规划 {len(plan)}")
    print(f"  切向阈值    {thresh:.2f} 画布 px（{ratio:.6f} × 画布宽）")
    check("交付物带几何（notes.json 的 lines 段）",
          bool(notes_doc.get("lines")), f"{len(notes_doc.get('lines') or [])} 条判定线")
    check("C 段用的是精确几何", touch_doc.get("geometry") == "exact",
          str(touch_doc.get("geometry")))
    check("规划明细存在（plan 非空）", bool(plan),
          "缺 plan 则无法对拍 —— 请去掉 --no-plan 重跑")
    # ★ 2026-10-16 新增：交付物**完整性**。此前本工具从不读 `unassigned`，
    #   于是一份含「够不着的音符」（判定区整体在屏外、C 段如实报成 unassigned）
    #   的交付物，可能靠 [2] 里 `reachable==False` 的豁免拿到 23/23 的**假绿**。
    #   全库 1064 谱面实测 `unassigned` 恒为 0 ⇒ 这条不会误杀，只会拦住真缺口。
    _un = touch_doc.get("unassigned") or []
    _un_n = int(touch_doc.get("unassigned_count", len(_un)) or 0)
    check("交付物无未分配音符（unassigned）", _un_n == 0,
          f"未分配 {_un_n} 个" + (f"：{_un[:3]}" if _un else ""))

    print()
    print("=" * 78)
    print("[1] 事件流合法性")
    print("=" * 78)
    used = sorted({int(e["pointer_id"]) for e in events})
    check(f"pointer_id 全部落在 [0, {max_pointers})",
          all(0 <= p < max_pointers for p in used), f"用到 {used}")
    check(f"用到的指针数 <= {max_pointers}", len(used) <= max_pointers, f"{len(used)} 个")
    check("事件按 t_ms 非递减",
          all(events[i]["t_ms"] <= events[i + 1]["t_ms"] for i in range(len(events) - 1)))

    bad_pair: List[Tuple[Any, ...]] = []
    bad_move: List[Tuple[Any, ...]] = []
    bad_act: List[Tuple[Any, ...]] = []
    bad_range: List[Tuple[Any, ...]] = []
    active: Dict[int, float] = {}
    sim = Sim()
    for e in events:
        pid = int(e["pointer_id"])
        act = e["action"]
        t = float(e["t_ms"])
        x, y = to_canvas(float(e["x"]), float(e["y"]))
        if not (-1.0 <= x <= cw + 1.0 and -1.0 <= y <= ch + 1.0):
            bad_range.append((t, pid, round(x, 2), round(y, 2)))
        if act in DOWN_ACTIONS:
            if pid in active:
                bad_pair.append((t, pid, "重复 DOWN"))
            active[pid] = t
            sim.apply(e, to_canvas)
        elif act in UP_ACTIONS:
            if pid not in active:
                bad_pair.append((t, pid, "未 DOWN 就 UP"))
            active.pop(pid, None)
            sim.apply(e, to_canvas)
        elif act == "MOVE":
            if pid not in active:
                bad_move.append((t, pid))
            sim.apply(e, to_canvas)
        else:
            bad_act.append((t, pid, act))

    if active:
        bad_pair.extend((t, p, "结束时仍按住") for p, t in active.items())

    check("DOWN / UP 严格配对", not bad_pair, f"异常 {len(bad_pair)} 处")
    check("MOVE 都发生在按住期间", not bad_move, f"异常 {len(bad_move)} 处")
    check("action 取值合法", not bad_act, f"异常 {len(bad_act)} 处")
    check("事件坐标落在画布内", not bad_range, f"越界 {len(bad_range)} 处")

    # 同一毫秒、同一指针既 UP 又 DOWN —— 生成阶段就不该出现的模式。
    # 成因：`_press` 遇到仍按着的指针会先 `_release`（时刻 = released_after + 1）再发
    # DOWN。选针时若只要求 `released_after < now`，`released_after == now - 1` 就会让
    # 抬起落在 `now`，两条 Event 挤进同一毫秒。
    # 为什么必须拦：实测（70MF IN，2026-10-09 真机）这个模式打出 3 个 Miss
    # （V / 99.81%，Early = Late = 0），而**离线两套判定模型都是 1557/1557 满分** ——
    # 全部 22 项旧检查也全绿。它只在真机上暴露，所以只能在交付物层面杜绝。
    same_ms: Dict[Tuple[int, int], set] = defaultdict(set)
    for e in events:
        act = e["action"]
        if act in UP_ACTIONS:
            same_ms[(int(round(float(e["t_ms"]))), int(e["pointer_id"]))].add("U")
        elif act in DOWN_ACTIONS:
            same_ms[(int(round(float(e["t_ms"]))), int(e["pointer_id"]))].add("D")
    bad_same_ms = sorted(k for k, v in same_ms.items() if len(v) == 2)
    check("同一毫秒内同一指针不同时出现 UP 与 DOWN", not bad_same_ms,
          f"冲突 {len(bad_same_ms)} 处"
          + (f"（首个 {bad_same_ms[0][0]}ms ptr{bad_same_ms[0][1]}）" if bad_same_ms else ""))

    check(f"任意时刻活跃指针数 <= {max_pointers}", sim.peak <= max_pointers,
          f"峰值 {sim.peak}")
    print(f"  事件构成：DOWN {sim.downs} / MOVE {sim.moves} / UP {sim.ups}"
          f"（MOVE 占 {100.0 * sim.moves / max(1, len(events)):.1f}%）")

    print()
    print("=" * 78)
    print("[2] 判定仿真（核心）")
    print("=" * 78)
    geom = Geom(notes_doc, (cw, ch))

    if not geom.available or not plan:
        check("具备判定仿真条件", False, "缺 lines 段或 plan，跳过本组")
        return _summary()

    # ---- 预处理：每个音符的判定点与查询时刻
    taps: List[Tuple[int, float]] = []
    drags: List[Tuple[int, float]] = []
    holds: List[Tuple[int, float, int]] = []
    flicks: List[Tuple[int, float]] = []
    for i, n in enumerate(notes):
        typ = int(n["type"])
        t = float(n["t_ms"])
        if typ == 1:
            taps.append((i, t))
        elif typ == 2:
            drags.append((i, t))
        elif typ == 3:
            holds.append((i, t, int(math.ceil(float(n.get("hold_ms", 0.0))))))
        else:
            flicks.append((i, t))

    # 查询点：hold 的每一毫秒 + tap/drag/flick 的双毫秒（floor 与 floor+1）
    #
    # ⚠️ 取整必须用 floor，不能用 round。调度器把 DOWN 排在 `floor(t_ms)`
    #    （提前 ≤1ms，绝不落后），UP 回填在 DOWN+1。若音符 t_ms 带半毫秒
    #    （Gungnir Fracture IN 的 BPM 网格即如此，如 29937.5），round 会把
    #    检查点推到 29938 —— 仿真循环「先应用 t_ms ≤ 检查点的全部事件」，
    #    UP(29938) 也被应用 ⇒ 手指已抬起 ⇒ 27 个音符被误报「无手指」。
    #    floor(29937.5)=29937：DOWN 已应用、UP 未应用，正好是「按下中」。
    #
    # ⚠️ 但 DOWN 也可能排在 floor(t)+1：调度器做正点取整时 .9375 这类尾数
    #    四舍五入进位（Gungnir #477 t=61335.9375 → DOWN=61336）。只查 floor
    #    会把它误报成「无手指」。所以 tap/drag/flick 注册 **floor 与 floor+1
    #    两个检查点，任一命中即算覆盖** —— DOWN 只可能落在这两毫秒之一
    #    （正点取整约定），真缺陷（DOWN 缺失 / 排远）两处都会 miss，不放水。
    def _anchor(i: int, t: float) -> int:
        """检查点锚定毫秒：被 Flick 同刻保护挪移的音符锚在 to_t，其余 floor(t)。

        ⚠️ **已知边界（2026-10-16 记，未触发）**：本函数与 [3] 的 `ok_t` 都假定
        非 Flick 音符的 head 只落在 `{floor(t), floor(t)+1}`；但
        `pgauto/schedule.py::pick_head_time` 在「floor 与 ceil 的判定点既不
        在屏内、法向投影也失败」时会于 **±`HEAD_WINDOW_MS`(=80ms)** 内另挑一个
        整数毫秒。真发生时会**误报**缺口/时刻不符（是假警报，不是漏检，安全方向相反）。
        对全部 43 组现存交付物逐条比过 `|head − floor(t_ms)|`，**偏差全为 0** ⇒ 潜伏。
        官方谱确实存在「判定线甩到屏外做演出」的线，故理论上可达。
        """
        g = guard.get(i)
        return g[1] if g else int(math.floor(t))

    checkpoints: Dict[int, List[Tuple[str, int]]] = defaultdict(list)
    for i, t in taps:
        ms0 = _anchor(i, t)
        checkpoints[ms0].append(("tap", i))
        checkpoints[ms0 + 1].append(("tap", i))
    for i, t in drags:
        ms0 = _anchor(i, t)
        checkpoints[ms0].append(("drag", i))
        checkpoints[ms0 + 1].append(("drag", i))
    for i, t in flicks:
        ms0 = _anchor(i, t)
        checkpoints[ms0].append(("flick", i))
        checkpoints[ms0 + 1].append(("flick", i))
    for i, t, hm in holds:
        t0 = _anchor(i, t)
        for off in range(0, hm + 1):
            checkpoints[t0 + off].append(("hold", i))

    order = sorted(events, key=lambda e: float(e["t_ms"]))
    cursor = 0
    sim = Sim()
    margins: List[float] = []
    # 每音符的覆盖结果：(kind, idx) -> (命中的毫秒集合, 缺口的毫秒集合, 首个缺口明细)
    cover: Dict[Tuple[str, int], Tuple[Set[int], Dict[int, Tuple], Optional[Tuple]]] = \
        defaultdict(lambda: (set(), {}, None))
    unreachable: Dict[str, int] = defaultdict(int)

    def mark_hit(kind: str, idx: int, ms: int) -> None:
        h, g, first = cover[(kind, idx)]
        cover[(kind, idx)] = (h | {ms}, g, first)

    def mark_gap(kind: str, idx: int, ms: int, n: Dict, pt: Tuple[float, float]) -> None:
        h, g, first = cover[(kind, idx)]
        detail = (n.get("type_name"), ms, round(pt[0], 1), round(pt[1], 1))
        cover[(kind, idx)] = (h, {**g, ms: detail}, first if first is not None else detail)

    for ms in sorted(checkpoints):
        while cursor < len(order) and float(order[cursor]["t_ms"]) <= ms:
            sim.apply(order[cursor], to_canvas)
            cursor += 1
        for kind, idx in checkpoints[ms]:
            n = notes[idx]
            line = int(n["line"])
            pt = geom.point(line, float(n["position_x"]), ms)
            hit = sim.near(pt, line, ms, geom, thresh)
            if hit is not None:
                margins.append(geom.offset(sim.state[hit], pt, line, ms))
                mark_hit(kind, idx, ms)
            elif not geom.reachable(pt, line, ms, thresh) and sim.state:
                # 判定区这一刻整体离开屏幕 ⇒ 没有任何屏幕内位置能命中。
                # 只要确实有手指按在屏幕上就放行（长按的持续段就是这么退的）。
                unreachable[kind] += 1
                mark_hit(kind, idx, ms)
            else:
                mark_gap(kind, idx, ms, n, pt)

    # 汇总：
    #   tap/drag/flick —— 双毫秒（floor / floor+1）任一命中即覆盖；
    #   hold —— 逐毫秒都要求覆盖，唯一豁免是首毫秒 t0：DOWN 允许排在
    #           t0+1（正点取整进位），此时 t0+1 是持续段检查点、必然命中。
    def uncovered(kind: str, note_ids: List[int]) -> List[Tuple[int, Tuple]]:
        bad = []
        for i in note_ids:
            hits_ms, gaps, first = cover.get((kind, i), (set(), {}, None))
            if kind == "hold":
                t0 = _anchor(i, float(notes[i]["t_ms"]))
                mid_gaps = [m for m in gaps if m != t0]
                head_uncovered = t0 in gaps and (t0 + 1) not in hits_ms
                if mid_gaps or head_uncovered:
                    detail = gaps.get(min(gaps)) if gaps else first
                    bad.append((i, detail))
            else:
                if not hits_ms:
                    bad.append((i, first))
        return bad

    # Flick：只看 **MOVE** 轨迹的位移幅度。
    #
    # ⚠️ **量的是「窗口内任意两点的最大距离」，不是「首尾距离」**（口径必须与
    #    `tools/replay_score.py::_flick_evidence` 完全一致，否则两个工具会对同一个
    #    交付物给出相反结论）。
    #
    # 两个曾经踩过的坑，都源于扫动窗口里**混进了坐标等于「音符中心」的点**：
    #   1. `DOWN` 的坐标就是音符中心 ⇒ 混进来会把「首点」错认成中心，跨度砍半；
    #   2. 2026-10-09 起 `_serve_flick` 在复用已按下的指针时会先补一条
    #      **归位 MOVE**（同样落在音符中心，依据 phisap `algo2.py`）⇒ 踩的是同一个坑。
    #      实测该改动让本项从 23/23 掉到 22/23（54/112，最小幅度 43.5 = R），
    #      但真正的扫动跨度没变（仍是 2R = 90 画布 px）—— 是**口径**失真，不是回归。
    traj: Dict[int, List[Tuple[float, float, float]]] = defaultdict(list)
    for e in events:
        if e["action"] == "MOVE":
            x, y = to_canvas(float(e["x"]), float(e["y"]))
            traj[int(e["pointer_id"])].append((float(e["t_ms"]), x, y))
    flick_ok = 0
    flick_move: List[float] = []
    flick_speed: List[float] = []
    for i, t in flicks:
        n = notes[i]
        lo, hi = t - lead, t + lead
        best, best_step = 0.0, 0.0
        for pts in traj.values():
            win = [(tt, x, y) for tt, x, y in pts if lo <= tt <= hi]
            if len(win) < 2:
                continue
            span = 0.0
            for a in range(len(win)):
                for b in range(a + 1, len(win)):
                    d = math.hypot(win[a][1] - win[b][1], win[a][2] - win[b][2])
                    if d > span:
                        span = d
            if span > best:
                step = 0.0
                for a, b in zip(win, win[1:]):
                    # 时间粒度就是 1ms（注入时刻本身是整数毫秒）；同毫秒的两点
                    # （归位 MOVE 与扫动首帧）不能当成「无穷大速度」。
                    dt = max(1.0, b[0] - a[0])
                    step = max(step, math.hypot(b[1] - a[1], b[2] - a[2]) / dt)
                best, best_step = span, step
        flick_move.append(best)
        flick_speed.append(best_step)
        if best >= args.flick_min_move:
            flick_ok += 1

    n_unreachable = sum(unreachable.values())
    tap_bad = uncovered("tap", [i for i, _ in taps])
    drag_bad = uncovered("drag", [i for i, _ in drags])
    hold_bad = uncovered("hold", [i for i, _, _ in holds])
    check("Tap：时刻处判定区内有手指（复用口径）", not tap_bad,
          f"{len(taps)} 个，未覆盖 {len(tap_bad)}"
          f"（是否要求独占一次新 DOWN，见 replay_score 严格模型）")
    check("Drag：时刻处判定区内有手指", not drag_bad,
          f"{len(drags)} 个，未覆盖 {len(drag_bad)}")
    check("Hold：全程每毫秒判定区内都有手指", not hold_bad,
          f"{len(holds)} 个长按 / {sum(h for _, _, h in holds) + len(holds)} 次逐毫秒检查，"
          f"未覆盖 {len(hold_bad)}")
    if n_unreachable:
        check("判定区离开屏幕的检查点：只要求手指留在屏幕上", True,
              f"共 {n_unreachable} 个（"
              + " / ".join(f"{k} {v}" for k, v in sorted(unreachable.items()))
              + "）—— 这些时刻判定区与屏幕无交集，数学上不可命中")
    check("Flick：窗口内出现幅度足够的位移（仅计 MOVE）", flick_ok == len(flicks),
          f"{flick_ok}/{len(flicks)}（最小幅度 "
          f"{min(flick_move) if flick_move else 0:.1f} 画布 px，"
          f"阈值 {args.flick_min_move:g}；最大单步速度 "
          f"{max(flick_speed) if flick_speed else 0:.2f} px/ms）")

    if margins:
        margins.sort()
        p50 = margins[len(margins) // 2]
        p99 = margins[int(len(margins) * 0.99)]
        print(f"  切向偏移（判定区内距判定点）：中位 {p50:.2f} / p99 {p99:.2f} / "
              f"最大 {margins[-1]:.2f} 画布 px（阈值 {thresh:.2f}）")
        print(f"  检查点总数 {len(margins) + n_unreachable}")
        print(f"  ⇒ 余量：阈值每收紧一档，覆盖是否仍然完整（用同一份事件流重算）")

    # 更严阈值下重算覆盖（余量曲线）
    for factor in (args.strict_ratio, 0.25):
        s = thresh * factor
        if s <= 0:
            continue
        bad = 0
        cps = sorted(checkpoints)
        cur = 0
        sim2 = Sim()
        for ms in cps:
            while cur < len(order) and float(order[cur]["t_ms"]) <= ms:
                sim2.apply(order[cur], to_canvas)
                cur += 1
            for _kind, idx in checkpoints[ms]:
                n = notes[idx]
                pt = geom.point(int(n["line"]), float(n["position_x"]), ms)
                if sim2.near(pt, int(n["line"]), ms, geom, s) is None:
                    bad += 1
                    break
            if bad:
                break
        print(f"  阈值 × {factor:g} = {s:6.2f} px  ⇒ {'覆盖完整' if not bad else '开始出现缺口'}")

    print()
    print("=" * 78)
    print("[3] 规划对拍（plan ↔ 事件流）")
    print("=" * 78)
    head_seen: Dict[int, List[int]] = defaultdict(list)
    keep_count: Dict[int, int] = defaultdict(int)
    bad_stage, bad_time, bad_pointer = [], [], []
    for row in plan:
        stage, nidx, t, pid = int(row[0]), int(row[1]), int(row[2]), int(row[3])
        n = notes[nidx]
        if stage not in STAGE_NAMES:
            bad_stage.append((stage, nidx))
            continue
        if stage in HEAD_STAGES:
            head_seen[nidx].append(stage)
        else:
            keep_count[nidx] += 1
        # 口径与 [2] 的检查点一致：floor。调度器的「正点」在 {floor(t), ceil(t)}
        # 里挑（见 pgauto/schedule.py 的候选毫秒逻辑），音符 t_ms 带小数时
        # （70MF 也有，如 5784.7534）floor(t) 比正点最多小 1ms：
        #   - Tap / Hold 起点 / Drag：容差 ±1 天然吸收；
        #   - Hold 持续段尾部：keep 末条 = 正点 + ceil(hold_ms)，上界须 +1 才不误杀。
        t0 = int(math.floor(float(n["t_ms"])))
        if nidx in guard:
            # Flick 同刻保护的音符：服务链锚在 to_t 上（head 对 to_t ±1，
            # keep 上界 = to_t + ceil(hold) + 1 —— 平移后的口径与原口径同构）。
            _, g_to = guard[nidx]
            if stage == 4:                               # Flick 不会被保护（保护只挪非 Flick）
                ok_t = abs(t - (g_to - lead)) <= 1
            elif stage == 2:
                ok_t = g_to < t <= g_to + 1 + int(math.ceil(float(n.get("hold_ms", 0.0))))
            else:
                ok_t = abs(t - g_to) <= 1
        elif stage == 4:                                 # Flick：提前 lead
            ok_t = abs(t - (t0 - lead)) <= 1
        elif stage == 2:                                 # Hold 持续：t+1 … t+hold
            ok_t = t0 < t <= t0 + 1 + int(math.ceil(float(n.get("hold_ms", 0.0))))
        else:                                            # Tap / Hold 起点 / Drag：正点
            ok_t = abs(t - t0) <= 1
        if not ok_t:
            bad_time.append((STAGE_NAMES[stage], nidx, t, t0))
        if not (0 <= pid < max_pointers):
            bad_pointer.append((STAGE_NAMES[stage], nidx, pid))

    check("plan 里 stage 取值合法", not bad_stage, f"异常 {len(bad_stage)}")
    check("plan 里 pointer_id 落在 [0, 上限)", not bad_pointer, f"异常 {len(bad_pointer)}")
    check("plan 的时刻与阶段规则一致（Flick 提前 30ms，Hold 持续逐毫秒）", not bad_time,
          f"异常 {len(bad_time)}")

    missing_head = [i for i in range(len(notes)) if len(head_seen[i]) != 1]
    check("每个音符恰有一条起手段被规划", not missing_head,
          f"缺/重 {len(missing_head)} 个")
    exp = {i: int(math.ceil(float(n.get("hold_ms", 0.0))))
           for i, n in enumerate(notes) if int(n["type"]) == 3}
    keep_bad = [i for i, want in exp.items() if keep_count[i] != want]
    check("每个 Hold 的持续段逐毫秒不缺", not keep_bad,
          f"{len(exp)} 个长按，异常 {len(keep_bad)}")

    plan_ptr_bad = 0
    plan_pos_bad = 0
    cur = 0
    sim3 = Sim()
    plan_sorted = sorted((int(r[2]), r) for r in plan)
    for t, row in plan_sorted:
        while cur < len(order) and float(order[cur]["t_ms"]) <= t:
            sim3.apply(order[cur], to_canvas)
            cur += 1
        pid = int(row[3])
        pos = sim3.state.get(pid)
        if pos is None:
            plan_ptr_bad += 1
            continue
        n = notes[int(row[1])]
        pt = geom.point(int(n["line"]), float(n["position_x"]), t)
        if geom.offset(pos, pt, int(n["line"]), t) >= thresh:
            plan_pos_bad += 1
    check("plan 的每条记录在该时刻确有指针按下", not plan_ptr_bad,
          f"缺按 {plan_ptr_bad} 条")
    check("plan 的每条记录都落在判定区内", not plan_pos_bad,
          f"出界 {plan_pos_bad} 条")

    print()
    print("=" * 78)
    print("[4] 规模统计")
    print("=" * 78)
    stages: Dict[str, int] = defaultdict(int)
    for row in plan:
        # ★ 2026-10-16：原为 `STAGE_NAMES[int(row[0])]`。`[3]` 对非法 stage 只
        #   记 `bad_stage` 并 `continue`（容忍），到这里却直接查表 ⇒ 抛 KeyError，
        #   整个验收中断、`_summary()` 都打不出来。改用 `.get` 与 [3] 的口径对齐。
        stages[STAGE_NAMES.get(int(row[0]), f"?{int(row[0])}")] += 1
    per_ptr: Dict[int, int] = defaultdict(int)
    for row in plan:
        per_ptr[int(row[3])] += 1
    print(f"  指针 {len(used)} 个 / 峰值并发 {sim.peak}")
    print(f"  每指针承载的服务次数：{ {k: per_ptr[k] for k in sorted(per_ptr)} }")
    print(f"  规划阶段分布：{dict(sorted(stages.items()))}")
    by_type: Dict[str, int] = defaultdict(int)
    for n in notes:
        by_type[n["type_name"]] += 1
    print(f"  音符类型分布：{dict(sorted(by_type.items()))}")

    return _summary()


def _summary() -> int:
    print()
    print("=" * 78)
    print("汇总")
    print("=" * 78)
    for line in PASS:
        print(f"  PASS  {line}")
    for line in FAIL:
        print(f"  FAIL  {line}")
    print()
    print(f"  {len(PASS)} / {len(PASS) + len(FAIL)} 通过")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
