"""C 段（指针调度）单元测试。

四部分：

1. **几何** —— 切向 / 法向、判定点投影、以及「法向无限长」这条模型基石。
2. **帧化** —— 四类音符怎么落到三条队列上；Flick 的提前量；Hold 的逐毫秒展开。
3. **屏幕外兜底** —— 时间微调（默认开）/ 拉进屏幕（默认关）的开关边界。
4. **指针池** —— 复用、切向服务、长按所有权、扫动共用、事件定名，最后用真实官谱收口。

模型的三条反直觉结论各有一条测试专门钉死：
    - 判定区**法向无限长** ⇒ 切向对齐就能复用（M5 全量普查里 98% 的服务是复用）。
    - Drag / Hold 持续段**不需要 DOWN** ⇒ 一根手指能连续伺候一串音符。
    - **一次点击只能判定一个 Tap/Hold** ⇒ 同刻双押必须按两次，不能靠「判定区内已有手指」顶替。

另有一条在 J 段被**证伪后改写**的结论：
    - ~~时间微调只对 Flick 开，Tap / Drag / Hold 没有「判定线闪烁」这条依据~~
      ⇒ 实际上**有**。判定线可以恰在音符时刻整条甩出屏幕，此时换判定窗内的前一毫秒即可
      （全量 1064 谱最后 5 个未分配音符全部由此归零，理论 AP 1063 → 1064）。
      两个机制分属 `pick_head_time` 与 Flick 的 `retime`，测试要求它们不许互相冒名。
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pgauto import schedule as sch  # noqa: E402

SAMPLE_NOTES = PROJECT_ROOT / "out" / "notes.json"
CHARTS_DIR = PROJECT_ROOT / "out" / "charts"
CANVAS_W, CANVAS_H = sch.CANVAS


@pytest.fixture(scope="module")
def real():
    """真实官谱的 notes.json（Rrhar'il AT，含 lines 几何）。"""
    if not SAMPLE_NOTES.exists():
        # 谱面中间件不入库（可由 tools/extract_chart.py 重生成）⇒ 缺了就 skip，不是 error。
        pytest.skip(f"缺 {SAMPLE_NOTES}：先跑 tools/extract_chart.py")
    return sch.load_notes(SAMPLE_NOTES)


def mk(type_=sch.NOTE_TAP, t=0.0, hold=0.0, px=960.0, py=540.0,
       angle=0.0, line=-1, position_x=0.0, index=0):
    return sch.Note(
        index=index, type=type_, t_ms=t, px=px, py=py, hold_ms=hold,
        angle_deg=angle, line=line, position_x=position_x,
    )


def same_ms_up_down(events) -> list:
    """同一毫秒内、同一指针既 UP 又 DOWN 的时刻。

    这是交付物里**不该存在**的模式：`_press` 会给仍按着的指针先 `_release`
    （时刻 = `released_after + 1`）再发 DOWN，选针时若只要求 `released_after < now`，
    `released_after == now - 1` 就会让抬起落在 `now`，两条 Event 挤进同一毫秒。
    """
    seen = {}
    for e in events:
        key = (e.t_ms, e.pointer_id)
        bucket = seen.setdefault(key, set())
        if e.action in ("UP", "POINTER_UP"):
            bucket.add("U")
        elif e.action in ("DOWN", "POINTER_DOWN"):
            bucket.add("D")
    return sorted(k for k, v in seen.items() if len(v) == 2)


# ---------------------------------------------------------------- 几何

def test_tangent_dir_flips_y_because_pixels_point_down():
    """切向 = 判定线方向。世界坐标 y 向上、像素 y 向下 ⇒ 分量反号。"""
    assert sch.tangent_dir(0.0) == pytest.approx((1.0, 0.0))
    assert sch.tangent_dir(90.0) == pytest.approx((0.0, -1.0))
    assert sch.tangent_dir(180.0) == pytest.approx((-1.0, 0.0))


def test_tangent_offset_ignores_displacement_along_the_normal():
    """判定区法向无限长 ⇒ 沿法线挪多远都仍在判定区内。"""
    point = (960.0, 540.0)
    assert sch.tangent_offset((960.0, 100.0), point, 0.0) == 0.0
    assert sch.tangent_offset((960.0, 1000.0), point, 0.0) == 0.0


def test_tangent_offset_counts_displacement_along_the_line():
    point = (960.0, 540.0)
    assert sch.tangent_offset((1060.0, 540.0), point, 0.0) == pytest.approx(100.0)
    assert sch.tangent_offset((860.0, 540.0), point, 0.0) == pytest.approx(100.0)


def test_project_into_screen_is_identity_inside():
    assert sch.project_into_screen(960.0, 540.0, 0.0, CANVAS_W, CANVAS_H) == (960.0, 540.0)


def test_project_into_screen_slides_along_the_normal():
    """判定线竖直（angle=0 ⇒ 法线竖直）时，屏外的点沿竖直方向拉回来。"""
    hit = sch.project_into_screen(2880.0, 540.0, 90.0, CANVAS_W, CANVAS_H)
    assert hit is not None
    x, y = hit
    assert 0.0 <= x <= CANVAS_W and 0.0 <= y <= CANVAS_H
    assert y == pytest.approx(540.0)          # 沿法线（水平）移动，纵向不动


def test_project_into_screen_gives_up_when_the_normal_misses_the_screen():
    """水平判定线 + 横向出屏 ⇒ 竖直法线永远碰不到屏幕，必须如实返回 None。"""
    assert sch.project_into_screen(2880.0, 540.0, 0.0, CANVAS_W, CANVAS_H) is None


def test_clamp_into_screen_takes_the_nearest_edge():
    assert sch.clamp_into_screen(2880.0, 540.0, CANVAS_W, CANVAS_H) == (CANVAS_W, 540.0)
    assert sch.clamp_into_screen(-100.0, -50.0, CANVAS_W, CANVAS_H) == (0.0, 0.0)


def test_retime_offsets_go_centre_out():
    assert list(sch.retime_offsets(3)) == [0, -1, 1, -2, 2, -3, 3]


def test_nearby_ints_walks_outward_from_the_nearest_integer():
    """下发时刻的候选次序：距音符时刻**由近及远**，两侧交替扩散。"""
    assert list(sch.nearby_ints(1000.2, 2)) == [1000, 1001, 999, 1002, 998, 1003]


def test_nearby_ints_breaks_a_tie_towards_the_smaller_integer():
    """`t` 的小数部分恰为 0.5 时先给**小**的那个 —— 与旧实现 `sorted(..., (dist, t))` 同规则。"""
    assert list(sch.nearby_ints(1000.5, 1)) == [1000, 1001, 999, 1002]


def test_nearby_ints_radius_zero_is_the_pre_j_candidate_set():
    """半径 0 ⇒ 只有 `{floor, ceil}`。**这就是 J 段之前的行为，也是那 5 个音符掉分的原因**：

    音符时刻恰为整数时 `floor == ceil`，候选集**只有一个**，里面根本没有「相邻的另一个
    毫秒」可换（旧实现的 docstring 说要「改用相邻的另一个整数毫秒」，但它枚举不出来）。
    """
    assert list(sch.nearby_ints(1000.2, 0)) == [1000, 1001]
    assert list(sch.nearby_ints(1000.0, 0)) == [1000]        # ← 单一候选 ＝ 死点


class _JumpLine:
    """判定线在 t=1000.5 处**瞬变**：之前 (100,100)，之后 (900,100)。"""

    screen_w = CANVAS_W
    screen_h = CANVAS_H

    def point_px(self, note, t_ms):
        return (100.0, 100.0) if t_ms < 1000.5 else (900.0, 100.0)

    def angle_deg(self, note, t_ms):
        return 0.0


def test_geometry_is_sampled_at_the_instant_the_event_is_stamped():
    """事件被盖在**整数毫秒**上，几何就必须取**那一毫秒**的判定点。

    判定线会在事件边界瞬变：`Incyde/IN` 的 5 号线在 t=47333.333 处从
    `(595.5, 864.0) @ 0°` 跳到 `(788.0, 844.9) @ -3°` —— 相差 **192.5 像素**。
    若按音符的精确时刻取点、却把事件盖在 47333，手指就落在瞬变**之前**那条判定区
    之外：真机上那个 Tap 必 Miss，而「判定区内有手指」的宽松模型量不出来
    （手指与判定点用的是同一个错坐标，两边一起错）。
    """
    n = mk(t=1000.5, px=0.0, py=0.0, index=0)
    r = sch.build([n], geometry=_JumpLine())
    down = next(e for e in r.events if e.action == "DOWN")
    assert down.t_ms == 1000                       # 事件盖在 round(1000.5) 上
    assert down.x == pytest.approx(100.0)          # 取 1000ms 那一刻的点，不是 1000.5ms 的


class _EdgeLine:
    """判定点在 t<1000.5 时被甩到屏幕外 (5000,500)，之后回到屏幕内 (700,500)。"""

    screen_w = CANVAS_W
    screen_h = CANVAS_H

    def point_px(self, note, t_ms):
        return (5000.0, 500.0) if t_ms < 1000.5 else (700.0, 500.0)

    def angle_deg(self, note, t_ms):
        return 0.0


def test_head_time_picks_the_neighbouring_ms_that_is_reachable():
    """下发时刻不是死板的 `round(t_ms)`：那一毫秒不可达就换相邻的另一个。

    两个候选都距音符时刻不到 1ms，远在 ±80ms 的判定窗口内，不影响精度评级。
    （若只按「取整时刻取几何」而不做这一步，全量未分配会从 6 涨到 14。）
    """
    n = mk(t=1000.2, px=0.0, py=0.0, index=0)
    assert int(round(n.t_ms)) == 1000              # 更近的那个是 1000 —— 但它在屏幕外
    r = sch.build([n], geometry=_EdgeLine())
    assert not r.unassigned
    down = next(e for e in r.events if e.action == "DOWN")
    assert down.t_ms == 1001
    assert down.x == pytest.approx(700.0)


def _teleporting_line() -> sch.NotesGeometry:
    """判定线**恰在 t=1000ms 那一刻整条甩出屏幕** —— `Message/EZ` 的等比缩小版。

    bpm=120 ⇒ 1 单位 = 15.625ms；两条 move 事件在 unit 64.0（= 1000ms**整**）处硬接：
    之前 `x = 0.5`（屏幕中心），之后 `x = 3.0`（画布右侧 3 倍宽处），`y` 同时跳到 3.0
    —— 像素上就是 (960, 540) → (5760, -2160)，且**法向竖直**，所以连
    「沿法线投回屏幕」都做不到（那条法线与屏幕无交集）。

    刻意用**真实 `LineEvents`** 而不是桩对象：这个现象的本质就是「事件边界处的瞬变 +
    定位容差」，桩对象会绕过那一层。
    """
    return sch.NotesGeometry(
        lines=[sch.LineEvents(bpm=120.0, move=[
            [0.0, 64.0, 0.5, 0.5, 0.5, 0.5],
            [64.0, 1000000.0, 3.0, 3.0, 3.0, 3.0],
        ])],
        screen_w=CANVAS_W, screen_h=CANVAS_H,
    )


def test_head_time_searches_the_whole_judgment_window():
    """J 段：`{floor, ceil}` 都不可达时，**继续在判定窗内扩散**。

    判定线恰在音符时刻甩出屏幕，而 `t=999` 判定点还在屏幕中心、`|999 - 1000| = 1ms`
    远在 ±80ms 里 —— 真机上一个跟着判定线走的玩家本来就在那儿。旧实现直接报
    `unassigned`（真机上这个 Drag 必 Miss）。
    """
    note = mk(type_=sch.NOTE_DRAG, t=1000.0, px=0.0, py=0.0, index=0, line=0)
    geom = _teleporting_line()

    # 反证：音符自己那一刻**确实**够不着 —— 这就是旧实现丢它的原因
    off = geom.point_px(note, 1000.0)
    assert off == pytest.approx((5760.0, -2160.0))
    assert sch.project_into_screen(*off, geom.angle_deg(note, 1000.0),
                                   CANVAS_W, CANVAS_H) is None
    on = geom.point_px(note, 999.0)
    assert sch.project_into_screen(*on, geom.angle_deg(note, 999.0),
                                   CANVAS_W, CANVAS_H) is not None

    frames, gaps, _ = sch.build_frames([note], geom)
    assert not gaps
    assert sorted(frames) == [999]                  # 挪到前 1 毫秒
    assert frames[999].keep[0].px == pytest.approx(960.0)


class _FarLine:
    """判定点只在 `t <= 880` 时可达（比音符时刻早 120ms），其余时刻甩到屏幕外。"""

    screen_w = CANVAS_W
    screen_h = CANVAS_H

    def point_px(self, note, t_ms):
        return (960.0, 540.0) if t_ms <= 880.0 else (5760.0, -2160.0)

    def angle_deg(self, note, t_ms):
        return 0.0


def test_head_time_never_leaves_the_judgment_window():
    """扩散有上限：窗外的「可达时刻」宁可不要，也绝不假装打到。

    `t = 880` 确实可达，但它距音符时刻 120ms > 80ms —— 换上它只会把 Perfect 变成 Miss。
    于是这里必须仍然如实报 `unassigned`。
    """
    note = mk(type_=sch.NOTE_DRAG, t=1000.0, px=0.0, py=0.0, index=0)
    frames, gaps, _ = sch.build_frames([note], _FarLine())
    assert [g["reason"] for g in gaps] == ["offscreen"]
    assert not frames


def test_flick_keeps_its_own_retime_and_does_not_use_the_head_time_window():
    """Flick 的下发时刻**不扩散**，仍由它自己的「扫动基准微调」兜底。

    理由：Flick 的手指必须在 `t0` 那一刻经过判定点，而判分器只在 `[t-30, t+30]` 里找
    扫动 —— 把 `t0` 挪出去会让整条扫动离开判分窗口，反而判 Miss。
    """
    note = mk(type_=sch.NOTE_FLICK, t=1000.0, px=0.0, py=0.0, index=0, line=0)
    frames, gaps, stats = sch.build_frames([note], _teleporting_line())
    assert not gaps
    assert stats["retimed"] == 1                    # 走的仍是 Flick 那条路
    assert sorted(frames) == [1000 - sch.FLICK_LEAD_MS]


# ---------------------------------------------------------------- 帧化

def test_tap_lands_in_the_down_queue():
    frames, gaps, _ = sch.build_frames([mk(t=1000.0)], sch.StaticGeometry())
    assert not gaps
    assert len(frames[1000].down) == 1
    assert not frames[1000].keep and not frames[1000].flick


def test_drag_lands_in_the_keep_queue():
    frames, _, _ = sch.build_frames([mk(type_=sch.NOTE_DRAG, t=1000.0)], sch.StaticGeometry())
    assert len(frames[1000].keep) == 1
    assert not frames[1000].down


def test_hold_splits_into_one_head_plus_every_millisecond():
    frames, gaps, _ = sch.build_frames(
        [mk(type_=sch.NOTE_HOLD, t=1000.0, hold=7.0)], sch.StaticGeometry())
    assert not gaps
    assert len(frames[1000].down) == 1                       # 起点要 DOWN
    assert sorted(frames) == list(range(1000, 1008))          # 逐毫秒各一帧
    assert all(len(frames[t].keep) == 1 for t in range(1001, 1008))


def test_flick_is_scheduled_ahead_of_its_own_instant():
    frames, _, _ = sch.build_frames(
        [mk(type_=sch.NOTE_FLICK, t=1000.0)], sch.StaticGeometry())
    assert len(frames[1000 - sch.FLICK_LEAD_MS].flick) == 1
    assert 1000 not in frames


# ---------------------------------------------------------------- 屏幕外兜底

def test_offscreen_drag_is_reported_when_clamp_is_off():
    """默认宁可少一个音符，也不假装打到。"""
    frames, gaps, stats = sch.build_frames(
        [mk(type_=sch.NOTE_DRAG, t=1000.0, px=5000.0)], sch.StaticGeometry())
    assert stats["clamped"] == 0
    assert [g["reason"] for g in gaps] == ["offscreen"]
    assert not frames


def test_clamp_offscreen_makes_the_stream_complete_but_flags_it():
    frames, gaps, stats = sch.build_frames(
        [mk(type_=sch.NOTE_DRAG, t=1000.0, px=5000.0)], sch.StaticGeometry(),
        clamp_offscreen=True)
    assert not gaps
    assert stats["clamped"] == 1
    assert frames[1000].keep[0].px == CANVAS_W      # 摆到最近的屏幕边缘


def _line_sweeping_out_of_screen() -> sch.NotesGeometry:
    """判定线在 t≈1000ms 附近从屏内横向扫出屏幕（bpm=120 ⇒ 1 单位 = 15.625ms）。"""
    return sch.NotesGeometry(
        lines=[sch.LineEvents(bpm=120.0, move=[[62.5, 64.0, 0.4, 1.5, 0.5, 0.5]])],
        screen_w=CANVAS_W, screen_h=CANVAS_H,
    )


def test_flick_retime_finds_an_earlier_instant_inside_the_screen():
    """判定线闪烁 ⇒ 同一 Flick 在窗口内的另一个时刻仍可判（phisap algo1 的机制）。"""
    geom = _line_sweeping_out_of_screen()
    note = mk(type_=sch.NOTE_FLICK, t=1000.0, line=0, position_x=0.0)
    assert sch.project_into_screen(*geom.point_px(note, 1000.0), 0.0, CANVAS_W, CANVAS_H) is None

    frames, gaps, stats = sch.build_frames([note], geom)
    assert not gaps
    assert stats["retimed"] == 1
    pend = frames[1000 - sch.FLICK_LEAD_MS].flick[0]
    assert 0.0 <= pend.px <= CANVAS_W and 0.0 <= pend.py <= CANVAS_H


def test_flick_retime_can_be_turned_off():
    geom = _line_sweeping_out_of_screen()
    note = mk(type_=sch.NOTE_FLICK, t=1000.0, line=0, position_x=0.0)
    frames, gaps, stats = sch.build_frames([note], geom, flick_retime=False)
    assert stats["retimed"] == 0
    assert [g["stage"] for g in gaps] == ["flick"]


def test_drag_is_rescued_by_the_head_time_window_not_by_flick_retime():
    """J 段：Drag 也会因「判定线在音符时刻甩出屏幕」而被救回，但走的是**另一个机制**。

    这条测试的**前身**断言的是「时间微调只对 Flick 开 —— Tap / Drag / Hold 没有
    『闪烁』这条依据」。该断言已在 J 段被**证伪**：`Message/EZ` 的 4 个 Drag 与
    `AfterZABANIYAMUGEdit/IN` 的 1 个 Drag 全都是这么丢的（判定线恰在音符时刻整条
    甩出屏幕），而它们同样是靠「换一毫秒」救回来的。

    保留下来的是**机制不许冒名**这一半：Drag 挪的是下发时刻（`pick_head_time`），
    `stats["retimed"]`（Flick 专属的扫动基准微调）必须保持 0。
    """
    geom = _teleporting_line()
    note = mk(type_=sch.NOTE_DRAG, t=1000.0, px=0.0, py=0.0, index=0, line=0)

    # 反证：音符自己那一毫秒的判定点确实在屏幕外（旧口径下这里会留下一个 unassigned）
    assert sch.project_into_screen(
        *geom.point_px(note, 1000.0), geom.angle_deg(note, 1000.0),
        CANVAS_W, CANVAS_H) is None

    frames, gaps, stats = sch.build_frames([note], geom)
    assert not gaps
    assert stats["retimed"] == 0                    # 没走 Flick 那条路
    served = sorted(frames)
    assert served == [999]                          # 挪到**前**一毫秒，不是往后
    assert 0 < 1000 - served[0] <= sch.HEAD_WINDOW_MS


# ---------------------------------------------------------------- 指针池

def test_one_tap_uses_one_pointer():
    r = sch.build([mk(t=1000.0)])
    assert r.pointer_count == 1
    assert [e.action for e in r.events] == ["DOWN", "UP"]


def test_two_taps_at_the_same_instant_each_need_their_own_click():
    """Phigros：**一次点击只能判定一个 Tap/Hold** ⇒ 同刻同位的双押必须按两次。

    这是 `QZKagoRequiem/AT` 丢 20818 分的那个坑：`#1616` / `#1617` 同刻同位、
    但落在不同判定线上（px 都是 1.4），C 段曾把它们合并成一个 DOWN。
    真机上那个 DOWN 只能吃到 1 个，另一个是 Miss。

    2026-10-10 备注：Flick 同刻保护 pass（g3/g5 双轮定案后加）**只挪与非
    Flick 判定时刻同毫秒的 head** —— 同刻双 Tap（无 Flick 参与不在保护列，
    有 Flick 参与也不与之同帧）保持同刻双 DOWN。238 处真机 237 处成功。
    """
    a = mk(t=1000.0, px=960.0, py=540.0, index=0)
    b = mk(t=1000.0, px=960.0, py=540.0, index=1)
    r = sch.build([a, b])
    assert r.pointer_count == 2
    assert r.stats["downs"] == 2
    assert not any(s.reused for s in r.serves)
    assert r.stats["refused_share"] == 1        # 记下了「顶替被拒不」这一次
    assert r.stats["flick_guard"] == 0          # 无 Flick 参与 ⇒ 不触发保护


def test_a_drag_and_a_tap_at_the_same_instant_share_one_click():
    """但 Drag 与 Tap 同刻同区 ⇒ 一次按下就够：Drag 要的是「判定区内有手指」。

    与上一条配对：**该省的仍然省，省不掉的必须按**。
    """
    a = mk(type_=sch.NOTE_DRAG, t=1000.0, px=960.0, py=540.0, index=0)
    b = mk(t=1000.0, px=960.0, py=540.0, index=1)
    r = sch.build([a, b])
    assert r.pointer_count == 1
    assert r.stats["downs"] == 1
    assert r.stats["refused_share"] == 0


def test_same_instant_far_apart_needs_two_pointers():
    a = mk(t=1000.0, px=100.0, py=540.0, index=0)
    b = mk(t=1000.0, px=1800.0, py=540.0, index=1)
    r = sch.build([a, b])
    assert r.pointer_count == 2


def test_normal_displacement_does_not_block_reuse():
    """水平判定线：纵向差多远都共用一个判定区，一根手指足够。

    这条「法向无限长」的推论**只对 Drag / Hold 持续成立**（它们要的是「有手指」）。
    Tap / Hold 起点要的是**点击** —— 同刻两个就得按两次，见上面那两条。
    """
    a = mk(type_=sch.NOTE_DRAG, t=1000.0, px=960.0, py=100.0, index=0)
    b = mk(type_=sch.NOTE_DRAG, t=1000.0, px=960.0, py=1000.0, index=1)
    r = sch.build([a, b])
    assert r.pointer_count == 1
    assert r.stats["downs"] == 1
    assert sum(1 for s in r.serves if s.reused) == 1


def test_tangency_follows_the_line_rotation():
    """判定线转 90° 后，切向变成竖直 —— 横向差多远都不影响，纵向才要命。"""
    far_in_x = [
        mk(type_=sch.NOTE_DRAG, t=1000.0, px=100.0, py=540.0, angle=90.0, index=0),
        mk(type_=sch.NOTE_DRAG, t=1000.0, px=1800.0, py=540.0, angle=90.0, index=1),
    ]
    assert sch.build(far_in_x).pointer_count == 1

    far_in_y = [
        mk(type_=sch.NOTE_DRAG, t=1000.0, px=100.0, py=540.0, angle=90.0, index=0),
        mk(type_=sch.NOTE_DRAG, t=1000.0, px=100.0, py=1000.0, angle=90.0, index=1),
    ]
    assert sch.build(far_in_y).pointer_count == 2


def test_drag_does_not_need_a_down_of_its_own():
    """Drag 只要判定区内有手指 ⇒ 第二个 Drag 一条事件都不发。"""
    a = mk(type_=sch.NOTE_DRAG, t=1000.0, px=960.0, py=540.0, index=0)
    b = mk(type_=sch.NOTE_DRAG, t=1000.0, px=960.0, py=540.0, index=1)
    r = sch.build([a, b])
    assert sum(1 for e in r.events if e.action in ("DOWN", "POINTER_DOWN")) == 1
    assert sum(1 for s in r.serves if s.reused) == 1


def test_hold_lives_from_head_to_tail_on_one_pointer():
    n = mk(type_=sch.NOTE_HOLD, t=1000.0, hold=200.0)
    r = sch.build([n])
    stages = [s.stage for s in r.serves]
    assert stages[0] == "hold_head"
    assert stages.count("hold_keep") == 200
    assert [e.action for e in r.events if e.action in ("DOWN", "POINTER_DOWN")] == ["DOWN"]
    assert r.events[-1].action == "UP"
    assert sch.by_type(r.serves)["Hold.keep"] == 200


def _expected_sweep_moves(span=None, stride=None):
    """扫动应有的 MOVE 条数 —— **独立公式**，刻意不复制生产里的 `range(...)` 循环。

    `ceil((span-1)/stride) + 1`：前 `ceil` 项覆盖 [0, span) 的等距采样，`+1` 是
    「末条必须落在 `span-1`」（见 `FLICK_SWEEP_STRIDE_MS` 的说明）。

    ⚠️ 不照抄 `_serve_flick` 的循环：照抄的话生产端把 stride 用错时这里会跟着一起错，
    测试就空转了（本项目已踩过「两处实现同错」）。
    """
    span = sch.FLICK_SPAN_MS if span is None else span
    stride = sch.FLICK_SWEEP_STRIDE_MS if stride is None else stride
    return math.ceil((span - 1) / stride) + 1


def test_flick_sweeps_along_the_normal_and_comes_back():
    n = mk(type_=sch.NOTE_FLICK, t=1000.0, px=960.0, py=540.0)
    r = sch.build([n])
    moves = [e for e in r.events if e.action == "MOVE"]
    radius = sch.FLICK_RADIUS_RATIO * CANVAS_W
    # ★★ 2026-10-23 降采样（`FLICK_SWEEP_STRIDE_MS`）：条数按独立公式，不是 60。
    assert len(moves) == _expected_sweep_moves()
    # 必须真的比旧行为少，否则「降采样」是空转（改常量却没生效会在这里红）。
    assert len(moves) < sch.FLICK_SPAN_MS
    ys = [e.y for e in moves]
    assert all(m.x == pytest.approx(960.0) for m in moves)     # angle=0 ⇒ 法向竖直
    assert len(set(round(y, 6) for y in ys)) == len(ys)         # 每一步都真的在动
    assert ys[0] > 540.0 and ys[-1] < 540.0                     # 去而复返
    # ★ 首尾位置必须钉死：首条钉「起点 = 中心 + R」（Flick 识别的单步位移下界），
    #   末条钉「扫到 −R 一侧」。降采样若丢了末条，下面第二条会红。
    assert ys[0] == pytest.approx(540.0 + radius)
    # 末条 rate = 1 − 2·(span−1)/span（不是 δ/span —— 写成后者会算成 451.5，实为 453.0）
    assert ys[-1] == pytest.approx(
        540.0 + radius * (1.0 - 2.0 * (sch.FLICK_SPAN_MS - 1) / sch.FLICK_SPAN_MS))
    # 相邻间隔 = stride（末段允许是余数），证明是等距采样而不是随手抽两条。
    dts = [b.t_ms - a.t_ms for a, b in zip(moves, moves[1:])]
    assert all(d == sch.FLICK_SWEEP_STRIDE_MS for d in dts[:-1]), dts
    assert dts[-1] == (sch.FLICK_SPAN_MS - 1) % sch.FLICK_SWEEP_STRIDE_MS or \
        dts[-1] == sch.FLICK_SWEEP_STRIDE_MS, dts


def test_two_flicks_on_the_same_spot_get_two_sweeps():
    """★ 2026-10-25 21:0x **真机推翻旧假设**：同刻同位的两个 Flick **必须各扫一次**。

    旧断言（`share_one_sweep`）依据"一次扫动同时满足两个判定区"。
    真机（moonlight IN）证伪：开头 t=1.379 s 就是两个同刻同位的 Flick，
    旧逻辑都分给 pid0、只发一次扫动 ⇒ 结算 **P1198 / Miss 1 / 连击 1197**（= 1199−2，丢第 2 个）。
    ⇒ 现在优先给每个 Flick 自己的指针；**只有指针真的不够用时**才回落共用。
    """
    a = mk(type_=sch.NOTE_FLICK, t=1000.0, px=960.0, py=540.0, index=0)
    b = mk(type_=sch.NOTE_FLICK, t=1000.0, px=960.0, py=540.0, index=1)
    r = sch.build([a, b])
    assert r.pointer_count == 2, "两个 Flick 应该各占一只手指"
    assert sum(1 for s in r.serves if s.reused) == 0, "不应再借用同一根手指的扫动"
    assert len([e for e in r.events if e.action == "MOVE"]) == 2 * _expected_sweep_moves(), \
        "各扫一次 ⇒ MOVE 条数是两次扫动的量"


# ---------------------------------------------------------------- Flick 的「归位 MOVE」

def _tap_then_flick():
    """复刻 70MF IN 的 Miss#1 现场（`#71 Tap` → `#72 Flick`）。

    两者同在 x=960，但 Tap 在 py=896、Flick 在 py=864 —— 相距 32 画布 px。
    Flick 的扫动起点在 `864 + R = 909`，于是手指距扫动起点只有 **12.8**：
    全曲 112 个 Flick 里唯一的 `jump < 40`（第二名 46.9），而它正是唯一丢分的那颗。
    """
    tap = mk(type_=sch.NOTE_TAP, t=1000.0, px=960.0, py=896.0, index=0)
    flick = mk(type_=sch.NOTE_FLICK, t=1100.0, px=960.0, py=864.0, index=1)
    return sch.build([tap, flick]), 1100 - sch.FLICK_LEAD_MS


def test_reused_flick_pointer_gets_a_home_move_first():
    """★ 复用一根**已经按下**的指针做 Flick 时，扫动首个毫秒必须先补一条「归位 MOVE」到音符中心。

    依据参考实现 phisap `algo2.py::_flick`（`algo1.py` 的 `FLICK_START` 分支同义）：

        if pointer.note is None:  DOWN(note.pos)     # 空闲指针 → 按下
        else:                     MOVE(note.pos)     # ★ 复用指针 → 先归位

    不补的后果：扫动第一条 MOVE 落在 `note.pos + R·n`。若手指此刻**已经在那附近**，
    整段扫动就没有任何「大位移」了 —— 设备侧采到的是一串几乎静止的点 ⇒ Flick 漏判。
    """
    r, t0 = _tap_then_flick()
    R = sch.FLICK_RADIUS_RATIO * CANVAS_W
    # 2026-10-15 格 A：归位留在 `t0`，扫动整体后移 `FLICK_SWEEP_DELAY_MS`，
    # 二者不再同刻（拆散「同刻双押 Flick」挤在同一毫秒的 4 条 MOVE）。
    home = [e for e in r.events if e.t_ms == t0 and e.action == "MOVE"]
    assert len(home) == 1, "起手毫秒应只有一条「归位 MOVE」"
    assert home[0].x == pytest.approx(960.0)
    assert home[0].y == pytest.approx(864.0)          # 归位到**音符中心**
    sweep = t0 + sch.FLICK_SWEEP_DELAY_MS
    sweep0 = [e for e in r.events if e.t_ms == sweep and e.action == "MOVE"]
    assert len(sweep0) == 1, "扫动首帧应在归位后 FLICK_SWEEP_DELAY_MS 毫秒"
    assert sweep0[0].y == pytest.approx(864.0 + R)    # 然后才是扫动起点


def test_reused_flick_first_step_reaches_the_radius():
    """上一条的**量化版**：复用指针时，扫动的单步最大位移必须 ≥ R。

    这才是「修好了」的判据 —— 只看「有没有两条 MOVE」可能被原地不动的归位蒙混过去。
    """
    r, t0 = _tap_then_flick()
    R = sch.FLICK_RADIUS_RATIO * CANVAS_W
    pid = [e for e in r.events if e.t_ms == t0 and e.action == "MOVE"][0].pointer_id
    ev = [e for e in r.events if e.pointer_id == pid]
    i = next(k for k, e in enumerate(ev) if e.t_ms == t0 and e.action == "MOVE")
    win = ev[max(0, i - 1): i + sch.FLICK_SPAN_MS]   # 从「按下」那一条接上
    assert win[0].t_ms == 1000
    steps = [math.hypot(b.x - a.x, b.y - a.y) for a, b in zip(win, win[1:])]
    assert max(steps) >= R - 1e-6, f"最大单步位移 {max(steps):.1f} < R={R:.1f}"


def test_flick_sweep_speed_keeps_double_margin_over_phisap_baseline():
    """T3（2026-10-10 方案 A）：扫速必须 ≥ phisap 基线（R=30/1280）的 2 倍，且扫动窗口不变。

    依据：Gungnir g6 中 / g7、g8 丢的 Flick@84500 —— 旧扫速 1.5 画布 px/ms 贴着
    游戏 Flick 识别阈值，成败随设备当轮状态掷骰子。半径翻倍后扫速留出 2 倍余量。
    """
    old_r = 30.0 / 1280.0 * CANVAS_W
    old_speed = 2 * old_r / 60.0                     # 基线：60ms 走完 2R
    n = mk(type_=sch.NOTE_FLICK, t=1000.0, px=960.0, py=540.0)
    r = sch.build([n])
    moves = [e for e in r.events if e.action == "MOVE"]
    assert len(moves) == _expected_sweep_moves()     # 扫动**窗口**（毫秒）不变，条数变少
    # 起止时刻随 `FLICK_SWEEP_DELAY_MS` 整体后移（2026-10-15 格 A），窗口长度不变。
    assert moves[0].t_ms == 1000 - sch.FLICK_LEAD_MS + sch.FLICK_SWEEP_DELAY_MS
    assert moves[-1].t_ms == 1000 + sch.FLICK_LEAD_MS - 1 + sch.FLICK_SWEEP_DELAY_MS
    path = sum(math.hypot(b.x - a.x, b.y - a.y) for a, b in zip(moves, moves[1:]))
    # ★★ 2026-10-23：扫速是**物理量**，必须按真实时间跨度算。
    #   旧写法 `path / (len(moves) - 1)` 隐含「1 条/毫秒」；降采样后 len 变小，
    #   那样算会把扫速虚高 stride 倍 —— 分母用 `moves[-1].t_ms - moves[0].t_ms`。
    dt = moves[-1].t_ms - moves[0].t_ms
    assert dt == sch.FLICK_SPAN_MS - 1               # 窗口没被缩
    speed = path / dt
    assert speed >= 2 * old_speed - 1e-6, \
        f"扫速 {speed:.2f} px/ms < 基线 2 倍 {2 * old_speed:.2f}"


def test_fresh_flick_pointer_needs_no_home_move():
    """**新建**指针（未按下）时不该补归位 —— `DOWN` 本身就落在音符中心，补了是多此一举。

    否则每次 Flick 都会多一条冗余 MOVE（全量 44952 条里有一小半属于这一类）。
    """
    n = mk(type_=sch.NOTE_FLICK, t=1000.0, px=960.0, py=864.0)
    r = sch.build([n])
    t0 = 1000 - sch.FLICK_LEAD_MS
    sweep = t0 + sch.FLICK_SWEEP_DELAY_MS
    # 新建指针：`DOWN` 直接落在音符中心，起手毫秒**只有** DOWN（无冗余归位）。
    assert [e.action for e in r.events if e.t_ms == t0] == ["DOWN"]
    assert len([e for e in r.events if e.t_ms == sweep and e.action == "MOVE"]) == 1
    assert len([e for e in r.events if e.action == "MOVE"]) == _expected_sweep_moves()


# ---------------------------------------------------------------- 事件流

def test_just_released_pointer_is_not_reused_at_the_next_millisecond():
    """刚抬起 1ms 的指针**不再被复用** —— 否则同一毫秒会挤进 UP + DOWN 两条事件。

    旧条件只要求 `released_after < now`，于是 `released_after == now - 1` 的指针会被
    选中；`_press` 先 `_release`（时刻 = `released_after + 1` = `now`）再发 DOWN，
    两条 Event 落在同一毫秒、同一个 pointer_id 上（新旧行为的分界见 `_pick_for_down`）。

    ⚠️ 这个模式在真机上**是真的会丢分**，不是洁癖：
    70MF IN（2026-10-09）打出 **V / 99.81% / 3 Miss**，`Early = Late = 0`
    （完全没碰到，而非按早按晚），而离线两套判定模型都是 **1557/1557 满分**、
    22 项旧检查全绿 —— 它只在真机上暴露。
    全量 1064 个谱面共有 **1181 处**，收紧后归零，且未分配音符 / 指针数 / 峰值并发
    **一项都没变**（指针上限仍 9 < 10）。
    """
    drag = mk(type_=sch.NOTE_DRAG, t=1000.0, px=100.0, py=540.0, index=0)
    tap = mk(type_=sch.NOTE_TAP, t=1001.0, px=1700.0, py=540.0, index=1)
    r = sch.build([drag, tap])

    assert not same_ms_up_down(r.events), same_ms_up_down(r.events)

    drag_pid = next(e.pointer_id for e in r.events
                    if e.t_ms == 1000 and e.action in ("DOWN", "POINTER_DOWN"))
    at = [(e.action, e.pointer_id) for e in r.events if e.t_ms == 1001]
    assert len(at) == 1, at
    assert at[0][0] in ("DOWN", "POINTER_DOWN")
    assert at[0][1] != drag_pid          # 换了一根，不是「抬起又按下同一根」


def test_events_order_release_before_press_within_one_instant():
    """`events()` 的排序规则本身：同一毫秒内**先 UP、再 DOWN、后 MOVE**。

    生成阶段已经不再制造同刻 UP+DOWN（见上一条），这条是**兜底** —— 万一将来某条
    路径又造出来，定名与活跃计数仍必须正确。所以直接往 `raw` 里塞事件，绕过选针。

    顺序反了（DOWN 排在 UP 前）会把活跃数算错：抬起时 `active` 还没减，DOWN 就会被
    定名成 `POINTER_DOWN` 而不是 `DOWN`。
    """
    pl = sch._Planner(max_pointers=10, canvas=(CANVAS_W, CANVAS_H),
                      tangent_px=100.0, flick_radius_px=100.0, flick_span_ms=30)
    p0 = pl.pointers[0]
    pl._ev(1000, "down", p0, 100.0, 540.0)
    pl._ev(1001, "down", p0, 900.0, 540.0)      # 故意先写 down
    pl._ev(1001, "up", p0, 100.0, 540.0)        # 再写 up —— 排序后它必须跑到前面

    got = [(e.action, e.pointer_id) for e in pl.events()]
    assert got == [("DOWN", 0), ("UP", 0), ("DOWN", 0)]


def test_events_are_sorted_monotonic_and_balanced():
    notes = [mk(t=1000.0 + 37.0 * i, px=100.0 * (i % 9), py=540.0, index=i) for i in range(40)]
    r = sch.build(notes)
    ts = [e.t_ms for e in r.events]
    assert ts == sorted(ts)

    active = 0
    for e in r.events:
        if e.action in ("DOWN", "POINTER_DOWN"):
            active += 1
        elif e.action in ("UP", "POINTER_UP"):
            active -= 1
        assert active >= 0
        assert 0.0 <= e.x <= CANVAS_W and 0.0 <= e.y <= CANVAS_H
    assert active == 0


def test_pointer_overflow_is_reported_not_silently_dropped():
    """把池子掐到 1 个指针：必须留下 unassigned，而不是悄悄少打一个音符。"""
    a = mk(t=1000.0, px=100.0, py=540.0, index=0)
    b = mk(t=1000.0, px=1800.0, py=540.0, index=1)
    r = sch.build([a, b], max_pointers=1)
    assert r.pointer_count == 1
    assert [u["reason"] for u in r.unassigned] == ["no_pointer"]
    assert not r.ok


def test_peak_concurrency_counts_live_pointers():
    a = mk(t=1000.0, px=100.0, py=540.0, index=0)
    b = mk(t=1000.0, px=900.0, py=540.0, index=1)
    c = mk(t=1000.0, px=1700.0, py=540.0, index=2)
    assert sch.build([a, b, c]).stats["peak_concurrency"] == 3


def test_hold_keep_never_emits_a_down():
    """长按的持续段只靠「判定区内有手指」，不该再补 DOWN。"""
    n = mk(type_=sch.NOTE_HOLD, t=1000.0, hold=50.0)
    r = sch.build([n])
    downs = [e for e in r.events if e.action in ("DOWN", "POINTER_DOWN")]
    assert len(downs) == 1
    assert r.serves[0].stage == "hold_head"


def test_flick_same_instant_guard_moves_the_other_head():
    """Flick 同刻保护：非 Flick 音符的头不与 Flick 判定时刻同毫秒。

    g3/g5 双轮真机定案：Gungnir #406 Hold 起手（DOWN）与 #405 Flick 判定同毫秒
    （53250），两轮都在这里断 combo；同构形态但错开 1~30ms 的 40 处全部命中。
    对策：把非 Flick 的服务链整体延后 `lead+2`（横扫最末毫秒 t0+29 之后起步），
    判定窗 ±80ms 内零评级损失；几何按新时刻重取。
    """
    f = mk(type_=sch.NOTE_FLICK, t=1000.0, px=300.0, py=540.0, index=0)
    t = mk(t=1000.0, px=1600.0, py=540.0, index=1)
    r = sch.build([f, t])
    guard = dict((i, (a, b)) for i, a, b in r.stats["flick_guard_moves"])
    assert guard.get(1) == (1000, 1032), f"Tap 头应错峰到 1032：{guard}"
    tap_serve = next(s for s in r.serves if s.note_index == 1 and s.stage == "tap")
    assert tap_serve.t_ms == 1032
    # 位移后事件流仍合法：Tap 的 DOWN 在 1032，且 1000 时刻只有 Flick 的活动
    tap_downs = [e for e in r.events
                 if e.action in ("DOWN", "POINTER_DOWN") and e.t_ms >= 1000
                 and e.x > 1200]          # Tap 在右半屏（1600*1.0 静态几何）
    assert tap_downs and all(e.t_ms == 1032 for e in tap_downs)


def test_flick_same_instant_guard_keeps_hold_chain_together():
    """被保护的 Hold 起手挪走时，持续链必须整体跟随（keep 与 head 同步平移）。"""
    f = mk(type_=sch.NOTE_FLICK, t=1000.0, px=300.0, py=540.0, index=0)
    h = mk(type_=sch.NOTE_HOLD, t=1000.0, hold=50.0, px=1600.0, py=540.0, index=1)
    r = sch.build([f, h])
    guard = dict((i, (a, b)) for i, a, b in r.stats["flick_guard_moves"])
    assert guard.get(1) == (1000, 1032)
    keep_ts = sorted(s.t_ms for s in r.serves
                     if s.note_index == 1 and s.stage == "hold_keep")
    assert keep_ts[0] == 1033 and keep_ts[-1] == 1082   # 1033..1032+50，逐毫秒不缺
    assert len(keep_ts) == 50


# ---------------------------------------------------------------- 真实官谱

def test_real_chart_stays_within_pointer_budget(real):
    r = sch.build(real.notes, geometry=real.geometry, canvas=real.canvas)
    assert r.geometry == "exact"
    assert r.pointer_count <= sch.MAX_POINTERS, f"用到了 {r.pointer_count} 个指针"
    assert not r.unassigned, f"有 {len(r.unassigned)} 项没被分配：{r.unassigned[:3]}"
    assert sch.peak_concurrency(r.events) <= sch.MAX_POINTERS


def test_real_chart_serves_every_note(real):
    """交付物级别的不变量：每个音符都至少有一条服务记录。"""
    r = sch.build(real.notes, geometry=real.geometry, canvas=real.canvas)
    served = {s.note_index for s in r.serves}
    missing = [n.index for n in real.notes if n.index not in served]
    assert not missing, f"{len(missing)} 个音符没有任何指针为它服务"


def test_real_chart_reuse_carries_most_of_the_load(real):
    """模型的卖点：绝大多数服务靠「已有手指落在判定区内」达成，不发任何事件。"""
    r = sch.build(real.notes, geometry=real.geometry, canvas=real.canvas)
    serves = r.stats["serves"]
    assert r.stats["reuse"] > 0.9 * serves
    assert r.stats["events"] < serves / 5


def test_real_70mf_has_no_same_ms_up_down():
    """真机跑出 3 Miss 的那一首（70MF IN）不许再有同刻 UP+DOWN。

    这是本轮的**真机回归样本**：它的旧版交付物有 8 处冲突，打出 V / 99.81% / 3 Miss。
    注意断言只针对「模式消失 + 指标不退化」—— 「真机能否 AP」必须重新打一次才知道，
    离线证明不了，别把这条测试当成闭环。
    """
    notes_path = PROJECT_ROOT / "out" / "notes_70mf.json"
    if not notes_path.is_file():
        pytest.skip("需要 out/notes_70mf.json（A/B 段产出）")

    data = sch.load_notes(notes_path)
    r = sch.build(data.notes, geometry=data.geometry, canvas=data.canvas)
    assert not same_ms_up_down(r.events), same_ms_up_down(r.events)
    assert not r.unassigned, f"有 {len(r.unassigned)} 项没被分配"
    assert r.pointer_count <= sch.MAX_POINTERS
    assert sch.peak_concurrency(r.events) <= sch.MAX_POINTERS


# ---------------------------------------------------------------- J 段的真实样本

def _load_chart(song: str, difficulty: str):
    """从 B 段落的 `out/charts/` 清单里取一份谱面（与全量普查同一套路径规则）。"""
    from pgauto.chart import Chart  # noqa: PLC0415

    manifest = json.loads((CHARTS_DIR / "index.json").read_text(encoding="utf-8"))
    for row in manifest["charts"]:
        if row["song"] == song and row["difficulty"] == difficulty:
            return Chart(json.loads(
                (PROJECT_ROOT / "out" / row["path"]).read_text(encoding="utf-8")))
    raise LookupError(f"清单里没有 {song} / {difficulty}")


def _notes_of(chart):
    """谱面 → C 段输入（与 `tools/sweep_charts.py::analyse` 同一套转换）。"""
    hits = chart.notes()
    return [
        sch.Note(index=i, type=h.type, t_ms=h.t_ms, px=h.px, py=h.py,
                 hold_ms=h.hold_sec * 1000.0,
                 angle_deg=chart.line_state(h.line, h.t_sec)[2],
                 line=h.line, position_x=h.position_x, multiple=h.multiple)
        for i, h in enumerate(hits)
    ]


@pytest.mark.skipif(not (CHARTS_DIR / "index.json").is_file(),
                    reason="需要 out/charts/index.json（B 段落盘）")
def test_real_chart_whose_judge_line_leaves_at_the_notes_own_instant():
    """J 段的真实样本：`Message/EZ #56` 的判定线**恰在音符时刻**整条甩出屏幕。

    这是全量 1064 个谱面里最后一个理论掉分的音符（G/H/I 三段收尾时都留着它）。
    取证：音符 t = 146250.0（整数 ⇒ 旧实现的候选集**只有它自己**），0 号判定线在
    同一毫秒从归一化 (0.5, 0.5) 跳到 (3.0, 3.0)，像素上 (960, 540) → (5652, -2160)；
    而 t = 146249 那一刻判定点还在屏幕内。
    """
    chart = _load_chart("Message.くるぶっこちゃん.0", "EZ")
    notes = _notes_of(chart)
    note = notes[56]
    geom = sch.geometry_from_chart(chart)

    assert note.type == sch.NOTE_DRAG
    assert note.line == 0
    assert note.t_ms == pytest.approx(146250.0)

    # 反证：音符自己那一毫秒够不着，前 1 毫秒够得着
    assert geom.point_px(note, 146250.0) == pytest.approx((5652.0, -2160.0))
    assert sch.project_into_screen(
        *geom.point_px(note, 146250.0), geom.angle_deg(note, 146250.0),
        CANVAS_W, CANVAS_H) is None
    px, py = geom.point_px(note, 146249.0)
    assert 0.0 <= px <= CANVAS_W and 0.0 <= py <= CANVAS_H

    frames, gaps, _ = sch.build_frames(notes, geom)
    assert not gaps, gaps[:3]                       # 整条谱面零未分配
    served = [p for fr in frames.values() for p in fr.keep if p.note.index == 56]
    assert len(served) == 1
    assert served[0].t_ms == 146249                 # 只挪了 1 毫秒


def test_head_window_stays_inside_the_perfect_window():
    """`HEAD_WINDOW_MS` 与判分器的 `PERFECT_MS` 是**两条刻意分开的副本**，但值必须相容。

    分开，是为了让验收器与实现互相独立（共享一个常量会让「验收抓到实现」退化成
    「自己验自己」）；相容，是因为一旦 `PERFECT_MS` 收得比 `HEAD_WINDOW_MS` 更紧，
    「换一毫秒」就会把 Perfect 换成 Good / Miss —— 那时 `pick_head_time` 必须跟着收紧。
    """
    import replay_score as rs  # noqa: PLC0415

    assert sch.HEAD_WINDOW_MS <= rs.PERFECT_MS


# ---------------------------------------------------------------- 最短按压时长（2026-10-10 方案 B）

def _press_durations(events) -> list:
    """把事件流配对成按压段，返回每段时长（DOWN→UP，毫秒）。"""
    opened = {}
    durs = []
    for e in events:
        if e.action in ("DOWN", "POINTER_DOWN"):
            assert e.pointer_id not in opened, f"指针 {e.pointer_id} 未抬先按 @{e.t_ms}"
            opened[e.pointer_id] = e.t_ms
        elif e.action in ("UP", "POINTER_UP"):
            durs.append(e.t_ms - opened.pop(e.pointer_id))
    assert not opened, f"收尾仍有未抬起的指针: {opened}"
    return durs


def test_tap_press_lasts_at_least_min_press_ms():
    """T1：相邻两押复用指针时，按压时长必须 ≥ MIN_PRESS_MS（旧口径是回填出的 1ms）。

    依据：Gungnir g7/g8 丢的 Tap@85250 与 g6 丢的 Tap@115.75 都是 1ms 短按——
    60Hz 触屏每 16.7ms 采样一次，1ms 触摸可能整帧落空。人手点击自然 80~120ms。
    """
    a = mk(type_=sch.NOTE_TAP, t=1000.0, px=300.0, py=900.0, index=0)
    b = mk(type_=sch.NOTE_TAP, t=1010.0, px=1500.0, py=900.0, index=1)
    r = sch.build([a, b])
    durs = _press_durations(r.events)
    assert durs, "至少应有一次按压"
    assert all(d >= sch.MIN_PRESS_MS for d in durs), f"短按: {durs}"
    assert not r.unassigned


def test_dense_taps_keep_min_press_and_pointer_budget():
    """T2：每 5ms 一押的密集段——按压全 ≥ MIN_PRESS_MS、无未分配、不超指针上限。

    按压变长 ⇒ 指针被占更久，这条测试盯住「可用指针变少」的副作用。
    """
    notes = [mk(type_=sch.NOTE_TAP, t=1000.0 + 5.0 * i,
                px=240.0 + 200.0 * (i % 8), py=900.0, index=i)
             for i in range(12)]
    r = sch.build(notes)
    durs = _press_durations(r.events)
    assert all(d >= sch.MIN_PRESS_MS for d in durs), f"短按: {durs}"
    assert not r.unassigned
    assert r.pointer_count <= sch.MAX_POINTERS
    assert not same_ms_up_down(r.events)


def test_min_press_zero_restores_legacy_backfill(monkeypatch):
    """回滚开关：MIN_PRESS_MS=0 必须逐字节恢复旧口径（1ms 回填短按）。"""
    monkeypatch.setattr(sch, "MIN_PRESS_MS", 0)
    a = mk(type_=sch.NOTE_TAP, t=1000.0, px=300.0, py=900.0, index=0)
    b = mk(type_=sch.NOTE_TAP, t=1010.0, px=1500.0, py=900.0, index=1)
    r = sch.build([a, b])
    durs = _press_durations(r.events)
    assert min(durs) == 1, f"旧口径应复现 1ms 回填短按，实际 {durs}"


# ---------------------------------------------------------------- 同刻承诺
#
# 2026-10-21 新增。背景（`out/miss_anatomy.md` + FACTS §119）：
# burn/IN 连续两轮 `E0/L0 + Bad 0 + M5~M6`，而 destruction321/IN 同链路三轮 M0。
# 离线实测抓到唯一的结构差异：**一根手指**可以「原地不动、让音符滑走」——
#   · Drag 停在离音符 121.9~126.5px 处（10 个）；
#   · 长按的手指被判定点甩开最多 **150px**（burn 的 #705/#706，各 3446ms，
#     >100px 分别有 1027/880 个毫秒点；而 dp321/IN 的 49 个长按全部 < 75px）。
# 下面四条钉的是「同刻不再把一根手指同时记给两个相距 125px 的点」。


def _moves_at(r, t_ms):
    return [(e.pointer_id, e.x) for e in r.events if e.t_ms == t_ms and e.action == "MOVE"]


def _touches_at(r, t_ms):
    """该毫秒所有「落点」事件：新按下 + 移动（都能把手指送到某个位置）。"""
    return [(e.action, e.x) for e in r.events
            if e.t_ms == t_ms and e.action in ("DOWN", "POINTER_DOWN", "MOVE")]


def test_same_frame_moved_finger_is_not_credited_125px_away():
    """本帧被别的音符挪走的手指，不能再顶 125px 外的第二个音符。

    复现 burn `#335/#336/#337`（t=81639，x=346/720/595）：旧口径下 `#336` 把手指
    挪到 720 后，`#337`（595）以「切向偏移 125 < 150」判定可复用、**一条事件都不发**
    ⇒ 595 那个点整毫秒无人。
    """
    a = mk(type_=sch.NOTE_DRAG, t=1000.0, px=1200.0, py=540.0, index=0)
    b = mk(type_=sch.NOTE_DRAG, t=1100.0, px=720.0, py=540.0, index=1)
    c = mk(type_=sch.NOTE_DRAG, t=1100.0, px=595.0, py=540.0, index=2)
    r = sch.build([a, b, c])
    got = sorted(_touches_at(r, 1100), key=lambda z: z[1])
    assert got == [("POINTER_DOWN", 595.0), ("MOVE", 720.0)], \
        f"t=1100 应各派一根手指，实际 {got}"
    assert r.stats["chord_refused"] == 0


def test_chord_fix_is_not_vacuous(monkeypatch):
    """变异测试：把重合半径放回 150px（旧口径）⇒ 上面那条必须**报红**。

    否则「测试通过」只说明断言写松了，不说明改动生效。
    """
    monkeypatch.setattr(sch, "CHORD_COINCIDENT_FACTOR", 1.0)
    a = mk(type_=sch.NOTE_DRAG, t=1000.0, px=1200.0, py=540.0, index=0)
    b = mk(type_=sch.NOTE_DRAG, t=1100.0, px=720.0, py=540.0, index=1)
    c = mk(type_=sch.NOTE_DRAG, t=1100.0, px=595.0, py=540.0, index=2)
    r = sch.build([a, b, c])
    got = sorted(_touches_at(r, 1100), key=lambda z: z[1])
    assert got == [("MOVE", 720.0)], f"旧口径应当只发一条 MOVE（静默复用），实际 {got}"
    assert sum(1 for s in r.serves if s.note_index == 2 and s.reused) == 1
    assert r.pointer_count == 1


def test_coincident_chord_still_shares_one_finger():
    """12 个同像素 Drag（超 Android 10 指针上限）必须共用一处，不许判成「分不上」。

    依据：`DESTRUCTION321/EZ` t=75439 的 12 个同点 Drag —— 给每个音符一根手指
    在物理上不可能（>10），而「同一点」本来就是一根手指能覆盖的。
    """
    notes = [mk(type_=sch.NOTE_DRAG, t=1000.0, px=960.0, py=540.0, index=i)
             for i in range(12)]
    r = sch.build(notes)
    assert r.pointer_count == 1
    assert r.stats["downs"] == 1
    assert r.stats["chord_refused"] == 0
    assert r.stats["chord_groups"] == 1
    assert r.stats["chord_notes"] == 12
    assert not r.unassigned


def test_chord_refused_is_counted_when_pool_is_exhausted():
    """指针全被本帧占满 ⇒ 借用被报告（`chord_refused`），绝不静默。"""
    a = mk(type_=sch.NOTE_DRAG, t=1000.0, px=100.0, py=540.0, index=0)
    b = mk(type_=sch.NOTE_DRAG, t=1000.0, px=960.0, py=540.0, index=1)
    c = mk(type_=sch.NOTE_DRAG, t=1000.0, px=1030.0, py=540.0, index=2)
    r = sch.build([a, b, c], max_pointers=2)
    assert r.stats["chord_refused"] == 1, r.stats
    assert not r.unassigned
    # 借用必须体现在告警里（真机上这些位置大概率丢判定）
    assert any("借" in w for w in r.warnings), r.warnings


def test_presence_reuse_px_makes_the_finger_track_the_note():
    """`presence_reuse_px` 调小 ⇒ 手指贴着音符走（长按跟随滞后的通用修法）。"""
    a = mk(type_=sch.NOTE_DRAG, t=1000.0, px=960.0, py=540.0, index=0)
    b = mk(type_=sch.NOTE_DRAG, t=2000.0, px=1010.0, py=540.0, index=1)
    loose = sch.build([a, b])
    tight = sch.build([a, b], presence_reuse_ratio=20.0 / CANVAS_W)
    assert sum(1 for e in loose.events if e.action == "MOVE") == 0
    assert sum(1 for e in tight.events if e.action == "MOVE") == 1
    assert tight.stats["presence_reuse_px"] == pytest.approx(20.0)


def test_presence_reuse_px_defaults_to_tangent_threshold():
    a = mk(type_=sch.NOTE_DRAG, t=1000.0, px=960.0, py=540.0, index=0)
    r = sch.build([a])
    assert r.stats["presence_reuse_px"] == pytest.approx(
        sch.TANGENT_THRESHOLD_RATIO * CANVAS_W)


# ── Flick 扫动越界兜底（2026-10-22）───────────────────────────────────────────
#
# 背景：`FLICK_RADIUS_RATIO` 的注释声称「越界由 clamp_into_screen 兜底」，
# 但 `_serve_flick` 从来没调过任何 clamp。全量 1064 谱实测 **8516 条事件跑出画布、
# 分布在 84 个谱面**，最大越界 90.0px（= 半径本身）。
#
# 修法迭代过两次，**第一版是错的**（自测推翻）：
#   ① 收缩半径 ⇒ 197 处 R 被压到 0，全在 `py=1080` ⇒ 零位移 ⇒ Flick 必漏。
#   ② 推中心（现行）⇒ 位移保满 180px，末端压着音符位置。


def test_fit_flick_sweep_is_identity_in_the_middle_of_the_screen():
    """屏幕正中：上下左右都有 90px 余量 ⇒ 中心与半径都不动（不许一刀切）。"""
    for nx, ny in ((0.0, 1.0), (0.0, -1.0), (1.0, 0.0), (-1.0, 0.0)):
        cx, cy, r = sch.fit_flick_sweep(960.0, 540.0, nx, ny, 90.0, *sch.CANVAS)
        assert (cx, cy, r) == pytest.approx((960.0, 540.0, 90.0))


def test_fit_flick_sweep_pushes_the_centre_instead_of_shrinking():
    """贴下沿：中心被推上去 90−20=70px，**半径不变**。

    这是与第一版的核心区别：收缩会让 `R_eff = 20`，扫速掉到原来的 22%。
    """
    py = CANVAS_H - 20.0
    cx, cy, r = sch.fit_flick_sweep(960.0, py, 0.0, 1.0, 90.0, *sch.CANVAS)
    assert r == pytest.approx(90.0), "半径不该被压"
    assert cx == pytest.approx(960.0)
    assert cy == pytest.approx(CANVAS_H - 90.0), "中心应推到「向上 +90 / 向下 90」的正中"
    # 扫动两端一上一下：上端离音符 180px、下端正好压在音符位置（画布下沿）
    assert cy + r == pytest.approx(CANVAS_H)
    assert cy - r == pytest.approx(CANVAS_H - 180.0)


def test_fit_flick_sweep_keeps_the_note_position_reachable():
    """判定点本身（`py`）必须仍落在扫动区间内 —— 否则手指压根不经过判定区。"""
    py = CANVAS_H - 20.0
    _cx, cy, r = sch.fit_flick_sweep(960.0, py, 0.0, 1.0, 90.0, *sch.CANVAS)
    assert cy - r <= py <= cy + r


def test_fit_flick_sweep_on_the_edge_still_moves_180px():
    """音符正好在画布边线上（`py = H`）：位移仍必须满 180px，不许退化成 0。"""
    cx, cy, r = sch.fit_flick_sweep(960.0, CANVAS_H, 0.0, 1.0, 90.0, *sch.CANVAS)
    assert r == pytest.approx(90.0)
    assert cy == pytest.approx(CANVAS_H - 90.0)
    assert 2 * r == pytest.approx(180.0)


def test_fit_flick_sweep_degrades_only_when_the_canvas_is_too_narrow():
    """画布沿法向放不下 2R 时才缩半径（并把中心摆到中间）。"""
    # 200x100 的画布、R=90：竖直方向只有 100px < 180px
    cx, cy, r = sch.fit_flick_sweep(100.0, 50.0, 0.0, 1.0, 90.0, 200.0, 100.0)
    assert r == pytest.approx(50.0)
    assert cy == pytest.approx(50.0)


def test_fit_flick_sweep_degenerate_normal_is_identity():
    """法向退化（零向量）时无从判断，原样返回（不许悄悄变成 0 半径）。"""
    assert sch.fit_flick_sweep(960.0, CANVAS_H, 0.0, 0.0, 90.0,
                               *sch.CANVAS) == pytest.approx((960.0, CANVAS_H, 90.0))


def _flick_events(r):
    return [e for e in r.events if e.action == "MOVE"]


def test_flick_sweep_never_leaves_the_canvas_near_the_bottom_edge():
    """`py` 贴下沿的 Flick，整段扫动必须留在画布内，且位移仍满 180px。"""
    py = CANVAS_H - 20.0
    r = sch.build([mk(type_=sch.NOTE_FLICK, t=1000.0, px=960.0, py=py, angle=0.0)])
    moves = _flick_events(r)
    assert moves, "Flick 应该产生扫动事件"
    ys = [e.y for e in moves]
    assert max(ys) <= CANVAS_H, max(ys)
    assert min(ys) >= 0.0
    assert max(ys) - min(ys) == pytest.approx(180.0 * (1 - 1 / sch.FLICK_SPAN_MS), abs=3.0), \
        "位移必须保住（这是与「缩半径」方案的关键差别）"
    assert r.stats["flick_shifted"] == 1
    assert r.stats["flick_radius_shrunk"] == 0, "不该缩半径"
    assert r.stats["flick_radius_min"] is None


def test_flick_edge_fix_is_not_vacuous():
    """变异测试：把「推中心」换回「不处理」⇒ 必须复现越界，证明修复非空转。

    同时断言**第一版（缩半径）也会通过越界检查但位移归零** —— 说明为什么
    「不越界」是必要但不充分的条件。
    """
    py = CANVAS_H - 20.0
    r = sch.build([mk(type_=sch.NOTE_FLICK, t=1000.0, px=960.0, py=py, angle=0.0)])
    ys = [e.y for e in _flick_events(r)]
    span_kept = max(ys) - min(ys)

    # 不处理的旧行为：极端点越界 70px（py+90 = 1150 > 1080）
    old_max = py + sch.FLICK_RADIUS_RATIO * CANVAS_W
    assert old_max > CANVAS_H, "构造本身必须真的越界，否则这条测试无意义"

    # 第一版（收缩半径）：不越界，但位移只剩 20px —— 这才是必须避开的陷阱。
    # ⚠️ 这里**内联**旧算法，不能调 `sch.fit_flick_radius`：那个名字现在已委托给
    #    新函数（返回 90），自测第一次写错就在这里 —— 拿别名测旧行为必然是空转。
    def shrink_radius(px, py, nx, ny, radius, w, h):
        best = float(radius)
        for sign in (1.0, -1.0):
            for axis in ((sign * nx, sign * ny),):
                dx, dy = axis
                room = float("inf")
                if dx > 0:
                    room = min(room, (w - px) / dx)
                elif dx < 0:
                    room = min(room, (0.0 - px) / dx)
                if dy > 0:
                    room = min(room, (h - py) / dy)
                elif dy < 0:
                    room = min(room, (0.0 - py) / dy)
                if room != float("inf"):
                    best = min(best, max(0.0, room))
        return best

    old_r = shrink_radius(960.0, py, 0.0, 1.0, 90.0, *sch.CANVAS)
    old_span = 2 * old_r
    assert old_r == pytest.approx(20.0), "旧方案应把半径压到 20px"
    assert old_span == pytest.approx(40.0), "旧方案位移只有 40px"
    assert span_kept > 3 * old_span, (
        f"现行方案位移 {span_kept:.0f} 必须显著大于旧方案 {old_span:.0f}")


def test_flick_radius_untouched_away_from_the_edges():
    """中心区 Flick 的半径与事件坐标与修前逐条相同（不许全谱改动）。"""
    r = sch.build([mk(type_=sch.NOTE_FLICK, t=1000.0, px=960.0, py=540.0, angle=0.0)])
    assert r.stats["flick_radius_shrunk"] == 0
    assert r.stats["flick_shifted"] == 0
    assert r.stats["flick_radius_min"] is None
    # 仍按 ±R 全幅扫动（末采样点 rate = 1 − 2(span−1)/span，到不了 −1，正常）
    ys = [e.y for e in _flick_events(r)]
    span = sch.FLICK_SPAN_MS
    assert max(ys) == pytest.approx(540.0 + 90.0)
    assert min(ys) == pytest.approx(540.0 + 90.0 * (1.0 - 2.0 * (span - 1) / span))


def test_schedule_chart_default_is_presence_follow():
    """生产 CLI 的默认必须是「手指跟随 20px」，且留 `--legacy-presence` 回旧口径。"""
    sys.path.insert(0, str(PROJECT_ROOT / "tools"))
    import schedule_chart  # noqa: PLC0415

    args = schedule_chart.build_parser().parse_args(["dummy.json"])
    assert args.presence_reuse_px == pytest.approx(20.0)
    assert args.legacy_presence is False
    legacy = schedule_chart.build_parser().parse_args(["dummy.json", "--legacy-presence"])
    assert legacy.legacy_presence is True


# ── presence 收紧后「换新手而不挪老手」的缺陷（2026-10-22）────────────────────
#
# `presence_px` 一收紧就暴露：判定线移动让手指偏离音符超过阈值后，`_pick_for_down`
# 因 `_is_keeper` 把**正在服务这个音符的那根手指**排除掉 ⇒ 只会「新开一根」，
# 老手指留在屏幕上空转。实测 `EntrancetotheChaos/IN` 指针 4→9、峰值 4→9，
# 而同刻真实需求仍是 4。修法 = `_own_pointer()` 允许把本音符自己的手指挪过去。


class _DriftingLine:
    """判定线横向缓慢漂移：让长按遮罩的中心逐毫秒移动，从而让手指「掉出」20px 阈值。"""

    def __init__(self, speed: float = 1.0):
        self.speed = speed

    def point_px(self, note, t: float):
        return note.px + (t - note.t_ms) * self.speed, note.py

    def angle_deg(self, note, t: float):
        return 0.0

    paper_w = CANVAS_W
    screen_w = CANVAS_W
    screen_h = CANVAS_H


def test_hold_keep_follows_its_own_finger_instead_of_opening_new_ones():
    """长按持续段偏离阈值后，必须**复用自己那根手指**（MOVE），不许换手。

    判据用「hold_keep 全部由同一根手指服务」而不是「指针总数」：修前的实际行为
    不是「无限新开」（那会被 10 根上限截住），而是**在两根手指之间来回换手**，
    并在屏幕上留下一根永远不再干活的僵尸手指。所以真正的不变量是
    「同一个长按的持续段只由一根手指服务」。
    """
    px, py = 400.0, 540.0
    hold = sch.Note(index=0, type=sch.NOTE_HOLD, t_ms=1000.0, px=px, py=py,
                    hold_ms=400.0, angle_deg=0.0, line=-1, position_x=0.0)
    geom = _DriftingLine(speed=2.0)      # 400ms 漂 800px，远超 20px 阈值
    r = sch.build([hold], geometry=geom, presence_reuse_ratio=20.0 / CANVAS_W)

    keeps = [s for s in r.serves if s.stage == "hold_keep"]
    assert keeps, "长按应该有持续段"
    owners = {s.pointer for s in keeps}
    assert owners == {0}, f"长按持续段换过手：{sorted(owners)}"
    assert r.pointer_count == 1, f"长按只该用 1 根手指，用了 {r.pointer_count}"
    assert not r.unassigned
    # 手指确实跟着走了（有 MOVE），而不是原地不动
    assert sum(1 for e in r.events if e.action == "MOVE") > 20


def test_own_pointer_fix_is_not_vacuous():
    """变异测试：关掉第 ①.5 道闸 ⇒ 必须复现「换手 + 僵尸手指」，证明修复非空转。"""
    px, py = 400.0, 540.0
    hold = sch.Note(index=0, type=sch.NOTE_HOLD, t_ms=1000.0, px=px, py=py,
                    hold_ms=400.0, angle_deg=0.0, line=-1, position_x=0.0)
    geom = _DriftingLine(speed=2.0)

    fixed = sch.build([hold], geometry=geom, presence_reuse_ratio=20.0 / CANVAS_W)
    old = sch.build([hold], geometry=geom, presence_reuse_ratio=20.0 / CANVAS_W,
                    own_pointer_off=True)      # 模拟修前：没有第 ①.5 道闸

    fixed_owners = {s.pointer for s in fixed.serves if s.stage == "hold_keep"}
    old_owners = {s.pointer for s in old.serves if s.stage == "hold_keep"}
    assert fixed_owners == {0}
    assert len(old_owners) > 1, (
        f"关掉修复后应出现换手（实际仍是 {sorted(old_owners)}）—— 否则这条测试是空转")
    assert old.pointer_count > fixed.pointer_count


# ── 2026-10-22 复审补测 ───────────────────────────────────────────────────────


@pytest.mark.parametrize("px, py, nx, ny", [
    (960.0, CANVAS_H + 5.0, 0.0, 1.0),      # 本体贴下沿外 5px
    (960.0, CANVAS_H + 40.0, 0.0, 1.0),     # 本体外 40px
    (960.0, -20.0, 0.0, 1.0),               # 本体在上沿外 20px
    (CANVAS_W + 5.0, 540.0, 1.0, 0.0),      # 本体在右沿外 5px
])
def test_fit_flick_sweep_still_contains_an_offcanvas_note(px, py, nx, ny):
    """判定点**本体已在画布外**时，推中心后整段扫动仍须落在画布内。

    生产路径上 `resolve()` 用 exact / projected / clamped 三级保证判定点在屏内，
    所以这是**导出函数的契约漏洞**，不是现网故障 —— 但「消灭越界」是本函数唯一的
    职责，它必须对越界输入也成立。修前 `room()` 把负余量截成 0 ⇒ 推动量正好少掉
    越界的那一截，推完仍然越界，而「有没有推」的计数照样 +1（静默）。
    """
    cx, cy, r = sch.fit_flick_sweep(px, py, nx, ny, 90.0, *sch.CANVAS)
    assert r == pytest.approx(90.0), "画布 1080 高放得下 2R，不许缩半径"
    for sign in (1.0, -1.0):
        ex, ey = cx + sign * r * nx, cy + sign * r * ny
        assert 0.0 <= ex <= CANVAS_W and 0.0 <= ey <= CANVAS_H, (
            f"推中心后仍越界：端点 ({ex}, {ey})")

    # 对照：**自己内联**修前那版（负余量截 0），确认它真的会越界。
    # ⛔ 不能调生产函数当对照 —— 那是拿新实现测新实现，必然空转。
    def clipped(px_, py_, nx_, ny_, radius, w, h):
        def room(ux, uy):
            rr = float("inf")
            if ux > 0:
                rr = min(rr, (w - px_) / ux)
            elif ux < 0:
                rr = min(rr, (0.0 - px_) / ux)
            if uy > 0:
                rr = min(rr, (h - py_) / uy)
            elif uy < 0:
                rr = min(rr, (0.0 - py_) / uy)
            return float(radius) if rr == float("inf") else max(0.0, rr)

        rp, rn = room(nx_, ny_), room(-nx_, -ny_)
        if rp >= radius and rn >= radius:
            return px_, py_, radius
        shift = -(radius - rp) if rp < radius else radius - rn
        return px_ + shift * nx_, py_ + shift * ny_, radius

    ocx, ocy, _ = clipped(px, py, nx, ny, 90.0, *sch.CANVAS)
    leaked = [(ocx + s * 90.0 * nx, ocy + s * 90.0 * ny) for s in (1.0, -1.0)
              if not (0.0 <= ocx + s * 90.0 * nx <= CANVAS_W
                      and 0.0 <= ocy + s * 90.0 * ny <= CANVAS_H)]
    assert leaked, (
        f"对照（负余量截 0 的旧写法）没复现越界 ⇒ 这条测试是空转，"
        f"样例 ({px},{py}) 选得不对")


def test_presence_flags_are_mutually_exclusive():
    """用法段是契约：`[--presence-reuse-px 20 | --legacy-presence]` 不许同时给。

    修前两者是独立选项 ⇒ 同时给会**静默**采用 legacy（`presence_px` 判成 None），
    命令行写着 20、实际跑的是 150 —— 又一例「配置看着生效、其实没生效」。
    """
    sys.path.insert(0, str(PROJECT_ROOT / "tools"))
    import schedule_chart  # noqa: PLC0415

    with pytest.raises(SystemExit):
        schedule_chart.build_parser().parse_args(
            ["dummy.json", "--presence-reuse-px", "20", "--legacy-presence"])
    # 单独给任一个都必须仍然可用
    assert schedule_chart.build_parser().parse_args(
        ["dummy.json", "--legacy-presence"]).legacy_presence is True
    assert schedule_chart.build_parser().parse_args(
        ["dummy.json", "--presence-reuse-px", "35"]).presence_reuse_px == pytest.approx(35.0)
