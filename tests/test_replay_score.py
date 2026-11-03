"""G 段判分器（tools/replay_score.py）的单元测试。

判分器的可信度来自两件事：

  ① 边界数值对得上：±80ms Perfect / 80~160 Good / 160~180 Bad（仅 Tap）。
  ② 它**真的能抓出**「一次点击只能判一个 Tap/Hold」的违规 —— 否则「全 Perfect」
     只说明模型太松，什么也没证明。

第 ② 点是本组的核心。`test_two_taps_sharing_one_down` 刻意构造「两个同刻 Tap
共用一个 DOWN」，要求严格模型判 Miss、宽松模型判 Perfect —— 两者的差就是模型
的分辨力。若哪天这条测试过了但实现退化成宽松模型，它会立刻失败。

另外两条是**把判分器本身上限松开的地方钉死**（I 段）：Flick 的「位移」必须按手指
的完整轨迹量（判定线滑走时不能只量带内那一段），Hold 的「离开判定区」必须给一个
短暂容差。两条都**同时**补了反证断言，证明它们不是「把阈值放松到过为止」。
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "tools"))

from replay_score import (  # noqa: E402
    BAD,
    GOOD,
    MISS,
    PERFECT,
    Geom,
    Replay,
    Score,
    Verdict,
    judge_lenient,
    judge_strict,
)

import replay_score  # noqa: E402  （白盒：偶发需要直接戳内部证据函数）

CANVAS = (1920.0, 1080.0)
THRESH = 150.0          # 与生产一致：100/1280 × 1920
CENTER = (960.0, 540.0)  # position_x = 0、判定线居中时的判定点
FLICK_MIN = 60.0
LEAD = 30

#: 一条永远静止在屏幕正中央、角度恒为 0 的判定线。
LINES = [{
    "bpm": 1.0,
    "move_events": [[0.0, 1e9, 0.5, 0.5, 0.5, 0.5]],
    "rotate_events": [[0.0, 1e9, 0.0, 0.0]],
}]


def ms_to_unit(v: float) -> float:
    """毫秒 → bpm=1 时的谱面时间单位（与 `LineGeom.unit` 同式）。"""
    return v / 1875.0


#: 一条在扫动期间**向右滑走**的判定线：t=970~1030 之间 x01 从 0.5 线性走到 0.65，
#: 判定点因此从 x=960 移到 x=1248 —— 手指（横向不动）只有前半段扫动落在判定带里。
GLIDE_LINES = [{
    "bpm": 1.0,
    "move_events": [[0.0, ms_to_unit(970.0), 0.5, 0.5, 0.5, 0.5],
                    [ms_to_unit(970.0), ms_to_unit(1030.0), 0.5, 0.65, 0.5, 0.5],
                    [ms_to_unit(1030.0), 1e9, 0.65, 0.65, 0.5, 0.5]],
    "rotate_events": [[0.0, 1e9, 0.0, 0.0]],
}]

TYPE_NAMES = {1: "Tap", 2: "Drag", 3: "Hold", 4: "Flick"}


def make_geom() -> Geom:
    return Geom({"format_version": 3, "aspect_ratio": 16.0 / 9.0, "lines": LINES},
                CANVAS)


def note(idx: int, typ: int, t_ms: float, hold_ms: float = 0.0) -> dict:
    return {"index": idx, "type": typ, "type_name": TYPE_NAMES[typ],
            "t_ms": t_ms, "hold_ms": hold_ms, "line": 0, "position_x": 0.0}


def ev(t: float, action: str, pid: int, x: float, y: float) -> dict:
    return {"t_ms": t, "action": action, "pointer_id": pid, "x": x, "y": y}


def tap(t: float, pid: int = 0, x: float = CENTER[0], y: float = CENTER[1]) -> list:
    """一次按下 + 一次抬起，位置在判定点。"""
    return [ev(t, "DOWN", pid, x, y), ev(t + 1, "UP", pid, x, y)]


def replay(events: list) -> Replay:
    return Replay(events, lambda x, y: (x, y), make_geom(), CANVAS)


def strict(notes: list, events: list) -> list:
    return judge_strict(notes, replay(events), THRESH, FLICK_MIN, LEAD)


def lenient(notes: list, events: list) -> list:
    return judge_lenient(notes, replay(events), THRESH, FLICK_MIN, LEAD)


# ---------------------------------------------------------------- 几何前置


def test_static_line_puts_judge_point_at_canvas_center() -> None:
    g = make_geom()
    assert g.point(0, 0.0, 1000.0) == pytest.approx(CENTER)
    assert g.tangent(0, 1000.0) == pytest.approx((1.0, 0.0))
    # 判定区沿法向（此处即 +y）无限长 ⇒ 同 x 的点偏移都是 0
    assert g.offset((960.0, 20.0), CENTER, 0, 1000.0) == pytest.approx(0.0)
    assert g.offset((700.0, 540.0), CENTER, 0, 1000.0) == pytest.approx(260.0)


# ---------------------------------------------------------------- 关键：点击预算


def test_two_taps_sharing_one_down() -> None:
    """核心分辨力测试：一个 DOWN 不能同时喂两个 Tap。

    严格 → 第二个 Miss；宽松 → 两个都 Perfect。两套模型的差就在这里。
    """
    notes = [note(0, 1, 1000.0), note(1, 1, 1000.0)]
    events = tap(1000.0)

    s = strict(notes, events)
    assert [v.judge for v in s] == [PERFECT, MISS]
    assert "独立" in s[1].reason

    l = lenient(notes, events)
    assert [v.judge for v in l] == [PERFECT, PERFECT]


def test_click_goes_to_the_earliest_judgeable_note() -> None:
    """「优先判定最早的那个」：一次点击落在两个音符之间，归早的那个。"""
    notes = [note(0, 1, 1000.0), note(1, 1, 1040.0)]
    events = tap(1050.0)          # 距早的 50ms、距晚的 10ms

    s = strict(notes, events)
    assert s[0].judge == PERFECT and s[0].delta_ms == pytest.approx(50.0)
    assert s[1].judge == MISS     # 若按「最近」分，这条会变成 Perfect


@pytest.mark.parametrize("offset_ms,expect_sign", [
    (+50.0, +1),      # 按晚 50ms ⇒ Δ 为正
    (-50.0, -1),      # 按早 50ms ⇒ Δ 必须为**负**（★ 2026-10-16 修复前恒为正）
])
def test_strict_delta_keeps_the_early_late_sign(offset_ms, expect_sign) -> None:
    """★ 严格模型的 `delta_ms` 必须**有符号**（负=偏早、正=偏晚）。

    2026-10-16 前，Tap/Hold 起手分支把 `d = abs(dt - t)` 直接存进 `Verdict.delta_ms`
    ⇒ 该值恒 ≥ 0 ⇒ **严格模型永远报不出「偏早」**；而 `dump()` 用 `{:+.3f}` 打印、
    `--json` 原样导出 ⇒ 抢拍会被显示成 `+Δ`（看似偏晚），与宽松模型结论相反。

    判据用「整体抢拍/拖拍」构造：期望符号 = 施加的偏移符号。
    """
    notes = [note(0, 1, 1000.0)]
    events = tap(1000.0 + offset_ms)

    s = strict(notes, events)
    assert s[0].judge == PERFECT
    assert s[0].delta_ms == pytest.approx(offset_ms)   # 数值就是施加的偏移
    assert (s[0].delta_ms > 0) == (expect_sign > 0), \
        f"严格模型 Δ={s[0].delta_ms:+.3f}ms 的符号与偏移 {offset_ms:+.0f}ms 相反"

    # 宽松模型本来就是有符号的 —— 两者方向必须一致（同一次击打、同一个音符）。
    l = lenient(notes, events)
    assert (l[0].delta_ms > 0) == (expect_sign > 0), \
        f"宽松模型 Δ={l[0].delta_ms:+.3f}ms 的符号与偏移 {offset_ms:+.0f}ms 相反"


def test_tap_without_any_down_is_miss() -> None:
    """一直按着不动的手指不能满足 Tap —— Tap 要的是**新的**按下。"""
    notes = [note(0, 1, 1000.0)]
    events = [ev(800.0, "DOWN", 0, *CENTER)]     # 早了 200ms，超出 Bad 窗
    assert strict(notes, events)[0].judge == MISS


def test_down_outside_the_judge_column_does_not_count() -> None:
    notes = [note(0, 1, 1000.0)]
    events = tap(1000.0, x=700.0)                # 切向偏移 260 > 阈值 150
    assert strict(notes, events)[0].judge == MISS


# ---------------------------------------------------------------- 判定窗口边界


@pytest.mark.parametrize("delta,expected", [
    (0, PERFECT), (80, PERFECT),
    (81, GOOD), (160, GOOD),
    (161, BAD), (180, BAD),
    (181, MISS),
])
def test_tap_window_boundaries(delta: int, expected: str) -> None:
    notes = [note(0, 1, 1000.0)]
    assert strict(notes, tap(1000.0 + delta))[0].judge == expected


@pytest.mark.parametrize("delta,expected", [
    (0, PERFECT), (80, PERFECT), (81, GOOD), (160, GOOD),
    (161, MISS), (180, MISS),        # Hold **没有** Bad —— 超出 Good 直接 Miss
])
def test_hold_has_no_bad_window(delta: int, expected: str) -> None:
    notes = [note(0, 3, 1000.0, hold_ms=200.0)]     # tail = 1000+200-80 = 1120
    # 起手那根一直按到尾部，保证被测的只有「起手判定」，不会因断按变成 Miss
    events = [ev(1000.0 + delta, "DOWN", 0, *CENTER), ev(1400.0, "UP", 0, *CENTER)]
    assert strict(notes, events)[0].judge == expected


def test_drag_never_gets_good() -> None:
    """Drag 只有 Perfect / Miss：窗口内没手指就是 Miss，不会退化成 Good。"""
    notes = [note(0, 2, 1000.0)]
    events = [ev(900.0, "DOWN", 0, *CENTER), ev(910.0, "UP", 0, *CENTER)]  # 早了 90ms
    assert strict(notes, events)[0].judge == MISS


# ---------------------------------------------------------------- 各类型语义


def test_drag_does_not_need_a_down_of_its_own() -> None:
    """别人的手指按着不放，就能接住 Drag —— 这是「复用」的正当情形。"""
    notes = [note(0, 2, 1000.0)]
    events = [ev(800.0, "DOWN", 0, *CENTER), ev(1400.0, "UP", 0, *CENTER)]
    assert strict(notes, events)[0].judge == PERFECT


def test_hold_survives_release_within_tolerance() -> None:
    """Hold 允许提前 80ms 松手。"""
    notes = [note(0, 3, 1000.0, hold_ms=500.0)]     # tail = 1000+500-80 = 1420
    events = [ev(1000.0, "DOWN", 0, *CENTER), ev(1430.0, "UP", 0, *CENTER)]
    assert strict(notes, events)[0].judge == PERFECT


def test_hold_breaks_when_released_too_early() -> None:
    notes = [note(0, 3, 1000.0, hold_ms=500.0)]
    events = [ev(1000.0, "DOWN", 0, *CENTER), ev(1300.0, "UP", 0, *CENTER)]
    v = strict(notes, events)[0]
    assert v.judge == MISS and "断开" in v.reason


def test_hold_keeps_alive_when_finger_switches() -> None:
    """换手长按：一根抬起的同时另一根按下，判定不断。"""
    notes = [note(0, 3, 1000.0, hold_ms=500.0)]
    events = [ev(1000.0, "DOWN", 0, *CENTER),
              ev(1200.0, "DOWN", 1, *CENTER),
              ev(1200.0, "UP", 0, *CENTER),
              ev(1500.0, "UP", 1, *CENTER)]
    assert strict(notes, events)[0].judge == PERFECT


def _sweep(t_ms: float, span: float, pid: int = 0) -> list:
    """一段沿法向（+y）来回的扫动：t-30 起手，60 步走完 `span` 像素。"""
    out = []
    for i in range(60):
        y = CENTER[1] - span / 2 + span * i / 59
        out.append(ev(t_ms - LEAD + i, "MOVE", pid, CENTER[0], y))
    return out


def test_flick_needs_enough_displacement() -> None:
    notes = [note(0, 4, 1000.0)]
    evs = [ev(1000.0 - LEAD, "DOWN", 0, *CENTER)] + _sweep(1000.0, 30.0)
    v = strict(notes, evs)[0]
    assert v.judge == MISS and "扫动幅度" in v.reason


def test_flick_passes_with_a_wide_sweep() -> None:
    notes = [note(0, 4, 1000.0)]
    evs = [ev(1000.0 - LEAD, "DOWN", 0, *CENTER)] + _sweep(1000.0, 90.0)
    assert strict(notes, evs)[0].judge == PERFECT


def test_flick_sweep_outside_the_column_is_not_evidence() -> None:
    """扫动必须在判定带里划过；在别处划得再远也不算。"""
    notes = [note(0, 4, 1000.0)]
    evs = [ev(1000.0 - LEAD, "DOWN", 0, *CENTER)]
    for i in range(60):
        y = CENTER[1] - 45.0 + 90.0 * i / 59
        evs.append(ev(1000.0 - LEAD + i, "MOVE", 0, 700.0, y))   # x 偏了 260px
    v = strict(notes, evs)[0]
    assert v.judge == MISS and "无扫动落点" in v.reason


# ---------------------------------------------------------------- Flick：带内 vs 位移


def _in_band_span(rp: Replay, line: int, px: float, t: float, thresh: float) -> float:
    """**修之前**的口径：只在「落在判定带内」的点上量位移（本组用它做反证）。"""
    best = 0.0
    for pts in rp.traj.values():
        win = [p for p in pts if t - LEAD <= p[0] <= t + LEAD]
        ok = [p for p in win
              if rp.geom.offset((p[1], p[2]), rp.geom.point(line, px, p[0]),
                                line, p[0]) < thresh]
        for a in range(len(ok)):
            for b in range(a + 1, len(ok)):
                best = max(best, math.hypot(ok[a][1] - ok[b][1], ok[a][2] - ok[b][2]))
    return best


def test_flick_counts_the_whole_sweep_when_the_line_glides_away() -> None:
    """判定线在扫动期间滑走时，位移要按**手指的完整轨迹**量，不能只量带内那一段。

    这是 I 段修掉的假 Miss（全量 61 个）。反证断言保证它有分辨力：按旧口径
    （只在带内量）这段扫动只有 ~47px，必然 < 60px 阈值被判 Miss。
    """
    notes = [note(0, 4, 1000.0)]
    evs = [ev(1000.0 - LEAD, "DOWN", 0, *CENTER)] + _sweep(1000.0, 90.0)
    rp = Replay(evs, lambda x, y: (x, y),
                Geom({"format_version": 3, "aspect_ratio": 16.0 / 9.0,
                      "lines": GLIDE_LINES}, CANVAS), CANVAS)

    # 反证：旧口径量不出来（这正是修之前判 Miss 的原因）
    assert _in_band_span(rp, 0, 0.0, 1000.0, THRESH) < FLICK_MIN

    span, in_band = replay_score._flick_evidence(rp, 0, 0.0, 1000.0, THRESH, LEAD)
    assert in_band and span == pytest.approx(90.0, abs=1.0)
    assert judge_strict(notes, rp, THRESH, FLICK_MIN, LEAD)[0].judge == PERFECT


def test_flick_still_needs_to_touch_the_column() -> None:
    """手指划得再远，只要从没进过判定带，就不是这个音符的 Flick。"""
    notes = [note(0, 4, 1000.0)]
    evs = [ev(1000.0 - LEAD, "DOWN", 0, 700.0, 540.0)]
    for i in range(60):
        evs.append(ev(1000.0 - LEAD + i, "MOVE", 0, 700.0, 495.0 + 90.0 * i / 59))
    v = strict(notes, evs)[0]
    assert v.judge == MISS and "无扫动落点" in v.reason


# ---------------------------------------------------------------- Hold：短暂离开判定区


@pytest.mark.parametrize("back_ms,expected", [
    (1280, PERFECT),          # 缺席 1200..1279 = 80ms，正好在容差上
    (1281, MISS),             # 缺席 1200..1280 = 81ms，超过容差即断
])
def test_hold_absence_tolerance_boundary(back_ms: int, expected: str) -> None:
    """Hold 允许**短暂**离开判定区；只有连续缺席超过容差才算断。"""
    notes = [note(0, 3, 1000.0, hold_ms=500.0)]      # tail = 1420
    events = [ev(1000.0, "DOWN", 0, *CENTER),
              ev(1200.0, "MOVE", 0, 700.0, 540.0),   # 切向偏 260 ⇒ 离开判定区
              ev(back_ms, "MOVE", 0, *CENTER),
              ev(1500.0, "UP", 0, *CENTER)]
    v = strict(notes, events)[0]
    assert v.judge == expected
    if expected is MISS:
        assert "断开" in v.reason and "连续 81ms" in v.reason


def test_hold_still_breaks_on_a_long_gap() -> None:
    """真断按（缺席几百毫秒）不能被容差放过。"""
    notes = [note(0, 3, 1000.0, hold_ms=500.0)]
    events = [ev(1000.0, "DOWN", 0, *CENTER),
              ev(1200.0, "MOVE", 0, 700.0, 540.0),
              ev(1400.0, "MOVE", 0, *CENTER),
              ev(1500.0, "UP", 0, *CENTER)]
    v = strict(notes, events)[0]
    assert v.judge == MISS and "断开" in v.reason


# ---------------------------------------------------------------- 连击与计分


def test_max_combo_resets_on_miss() -> None:
    notes = [note(0, 1, 1000.0), note(1, 1, 2000.0), note(2, 1, 3000.0)]
    events = tap(1000.0, pid=0) + tap(2000.0, pid=0)      # 第三个没有按下
    sc = Score("t", strict(notes, events))
    assert [v.judge for v in sc.verdicts] == [PERFECT, PERFECT, MISS]
    assert sc.max_combo == 2


def test_all_perfect_scores_one_million() -> None:
    notes = [note(i, 1, 1000.0 + 1000 * i) for i in range(4)]
    events = [e for i in range(4) for e in tap(1000.0 + 1000 * i, pid=i)]
    sc = Score("t", strict(notes, events))
    assert sc.score == 1000000
    assert sc.max_combo == 4
    assert sc.rank.startswith("φ")


def test_score_formula_matches_the_game() -> None:
    """4 个音符、1 个 Good：900000×0.9125 + 100000×4/4 = 921250。

    注意评级是 V 而不是 S —— Phigros 里 **FC 评级会覆盖分数评级**，只要没有
    Bad/Miss，无论分数多少都是 V。
    """
    notes = [note(i, 1, 1000.0 + 1000 * i) for i in range(4)]
    events = (tap(1000.0, pid=0) + tap(2081.0, pid=1)
              + tap(3000.0, pid=2) + tap(4000.0, pid=3))
    sc = Score("t", strict(notes, events))
    assert [v.judge for v in sc.verdicts] == [PERFECT, GOOD, PERFECT, PERFECT]
    assert sc.accuracy == pytest.approx(0.9125)
    assert sc.score == 921250
    assert sc.rank == "V (Full Combo)"


def test_rank_falls_back_to_the_score_table_after_a_miss() -> None:
    """断了连击就按分数表评级：3 Perfect + 1 Miss = 675000 + 75000 = 750000 → C。"""
    notes = [note(i, 1, 1000.0 + 1000 * i) for i in range(4)]
    events = tap(1000.0, pid=0) + tap(2000.0, pid=1) + tap(3000.0, pid=2)
    sc = Score("t", strict(notes, events))
    assert sc.accuracy == pytest.approx(0.75)
    assert sc.max_combo == 3
    assert sc.score == 750000
    assert sc.rank == "C"


def test_full_combo_but_not_all_perfect_is_rank_v() -> None:
    notes = [note(0, 1, 1000.0), note(1, 1, 2000.0)]
    events = tap(1000.0, pid=0) + tap(2081.0, pid=1)
    sc = Score("t", strict(notes, events))
    assert sc.max_combo == 2
    assert sc.rank == "V (Full Combo)"


# ---------------------------------------------------------------- 段式重放


def test_fingers_at_uses_segments() -> None:
    """手指模型：按下即在场、MOVE 更新位置、抬起即离场（抬起那一毫秒已不在）。"""
    rp = replay([ev(100, "DOWN", 0, 10, 20),
                 ev(150, "MOVE", 0, 30, 40),
                 ev(200, "UP", 0, 30, 40)])
    assert rp.fingers_at(99) == {}
    assert rp.fingers_at(100) == {0: (10.0, 20.0)}
    assert rp.fingers_at(149) == {0: (10.0, 20.0)}
    assert rp.fingers_at(150) == {0: (30.0, 40.0)}
    assert rp.fingers_at(200) == {}
    assert rp.fingers_at(999) == {}


def test_down_is_recorded_in_canvas_space() -> None:
    rp = replay([ev(100, "DOWN", 3, *CENTER)])
    assert rp.downs == [(100.0, *CENTER)]
    assert len(rp.downs_near(100.0)) == 1
    assert rp.downs_near(400.0) == []       # 超出 ±180


# ---------------------------------------------------------------- 真实交付物


@pytest.mark.skipif(not (PROJECT_ROOT / "out" / "touches.json").is_file(),
                    reason="需要先跑 tools/schedule_chart.py 生成 out/touches.json")
def test_real_chart_reaches_all_perfect() -> None:
    """真实交付物：Rrhar'il AT 在严格模型下应当满 Perfect（φ）。"""
    notes_doc = json.loads((PROJECT_ROOT / "out" / "notes.json").read_text(encoding="utf-8"))
    touch_doc = json.loads((PROJECT_ROOT / "out" / "touches.json").read_text(encoding="utf-8"))
    notes = notes_doc["notes"]
    cw = float(touch_doc["canvas"]["width"])
    ch = float(touch_doc["canvas"]["height"])
    sw = float(touch_doc["screen"]["width"])
    sh = float(touch_doc["screen"]["height"])
    scale = sw / cw
    y_off = (sh - ch * scale) / 2.0

    geom = Geom(notes_doc, (cw, ch))
    rp = Replay(touch_doc["events"], lambda x, y: (x / scale, (y - y_off) / scale),
                geom, (cw, ch))
    thresh = float(touch_doc["stats"]["tangent_threshold_ratio"]) * cw
    lead = int(touch_doc["stats"]["flick_lead_ms"])

    s = Score("AT", judge_strict(notes, rp, thresh, FLICK_MIN, lead))
    assert s.total == len(notes)
    assert s.score == 1000000, s.bad[:5]
    assert all(isinstance(v, Verdict) for v in s.verdicts)
