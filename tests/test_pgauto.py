"""pgauto 单元测试。

两部分：
1. **算术核对** —— 用最小构造谱面手算期望值。这是正确性的主要证据，
   因为期望值不来自被测实现，而是从 Phira 的公式独立推导。
2. **规模核对** —— 用真实官谱（Rrhar'il AT）核对音符总数与分布。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pgauto.anim import AnimFloat  # noqa: E402
from pgauto.chart import HEIGHT_RATIO, X_UNIT, Chart  # noqa: E402
from pgauto.events import parse_speed_events  # noqa: E402

W, H = 1920.0, 1080.0
ASPECT = 16.0 / 9.0
SAMPLE = PROJECT_ROOT / "samples" / "rrhar_at" / "Rrharil.TeamGrimoire.0.json"


# ---------------------------------------------------------------- 构造器

def make_line(**overrides):
    line = {
        "bpm": 60.0,
        "notesAbove": [],
        "notesBelow": [],
        "speedEvents": [],
        "judgeLineMoveEvents": [
            {"startTime": 0.0, "endTime": 1e9, "start": 0.5, "end": 0.5, "start2": 0.5, "end2": 0.5}
        ],
        "judgeLineRotateEvents": [
            {"startTime": 0.0, "endTime": 1e9, "start": 0.0, "end": 0.0}
        ],
        "judgeLineDisappearEvents": [
            {"startTime": 0.0, "endTime": 1e9, "start": 1.0, "end": 1.0}
        ],
    }
    line.update(overrides)
    return line


def make_chart(lines, fmt=3):
    return {"formatVersion": fmt, "offset": 0.0, "judgeLineList": lines}


def make_note(type_=1, time=32, position_x=0.0, hold_time=0.0, speed=1.0, floor_position=0.0):
    return {
        "type": type_,
        "time": time,
        "positionX": position_x,
        "holdTime": hold_time,
        "speed": speed,
        "floorPosition": floor_position,
    }


def build(*, line_overrides=None, notes=None, fmt=3, aspect=None, screen=None):
    line = make_line(**(line_overrides or {}))
    line["notesAbove"] = [make_note()] if notes is None else notes
    kwargs = {}
    if aspect is not None:
        kwargs["aspect_ratio"] = aspect
    if screen is not None:
        kwargs["screen"] = screen
    return Chart(make_chart([line], fmt=fmt), **kwargs)


def single(*, line_overrides=None, notes=None, fmt=3, aspect=None, screen=None):
    """一条判定线 + 一个音符，直接返回那唯一的击打点。"""
    return build(
        line_overrides=line_overrides, notes=notes, fmt=fmt, aspect=aspect, screen=screen
    ).notes()[0]


# ---------------------------------------------------------------- AnimFloat

def test_anim_linear_interpolation():
    a = AnimFloat([(0.0, 0.0), (2.0, 10.0)])
    assert a.at(1.0) == pytest.approx(5.0)


def test_anim_clamps_outside_range():
    a = AnimFloat([(0.0, 3.0), (2.0, 7.0)])
    assert a.at(-5.0) == pytest.approx(3.0)
    assert a.at(99.0) == pytest.approx(7.0)


def test_anim_empty_yields_zero():
    assert AnimFloat([]).at(1.0) == 0.0


def test_anim_step_at_same_time_takes_later_frame():
    """同一时刻的两帧（瞬变）应取后者，与 Phira 游标行为一致。"""
    a = AnimFloat([(0.0, 0.0), (1.0, 0.0), (1.0, 9.0), (2.0, 9.0)])
    assert a.at(1.0) == pytest.approx(9.0)


# ---------------------------------------------------------------- 时间

def test_time_unit_is_60_over_32_over_bpm():
    """bpm=60 时 1 个单位 = 60/32/60 秒；time=32 → 1.0 秒。"""
    hit = build().notes()[0]
    assert hit.t_sec == pytest.approx(1.0)
    assert hit.t_ms == pytest.approx(1000.0)


def test_bpm_scales_time_inversely():
    hit = build(line_overrides={"bpm": 120.0}).notes()[0]
    assert hit.t_sec == pytest.approx(0.5)


# ---------------------------------------------------------------- 坐标

def test_note_at_screen_center():
    hit = build().notes()[0]
    assert hit.px == pytest.approx(960.0)
    assert hit.py == pytest.approx(540.0)


def test_position_x_unit_equals_0_05625_of_width():
    """positionX 的 1 个单位 = 0.05625 × 屏宽（= X_UNIT × 半宽）。"""
    hit = build(notes=[make_note(position_x=1.0)]).notes()[0]
    assert X_UNIT == pytest.approx(0.1125)
    assert hit.px == pytest.approx(960.0 + 0.05625 * W)
    assert hit.py == pytest.approx(540.0)


def test_position_x_is_symmetric():
    left = build(notes=[make_note(position_x=-1.0)]).notes()[0]
    assert left.px == pytest.approx(960.0 - 0.05625 * W)


def test_move_event_maps_0_1_onto_full_width():
    """moveEvent x=1 表示屏幕最右。"""
    hit = single(
        line_overrides={
            "judgeLineMoveEvents": [
                {"startTime": 0.0, "endTime": 1e9, "start": 1.0, "end": 1.0, "start2": 0.5, "end2": 0.5}
            ]
        }
    )
    assert hit.px == pytest.approx(W)
    assert hit.py == pytest.approx(H / 2)


def test_move_event_y_is_top_down():
    """官谱 y 轴向上（左下角为原点）：y=1 应落在屏幕顶部。"""
    hit = single(
        line_overrides={
            "judgeLineMoveEvents": [
                {"startTime": 0.0, "endTime": 1e9, "start": 0.5, "end": 0.5, "start2": 1.0, "end2": 1.0}
            ]
        }
    )
    assert hit.py == pytest.approx(0.0)


def test_rotation_90_swings_note_upward():
    """判定线在屏幕中心旋转 +90°，positionX=1 的音符沿旋转方向移到上方。

    判定线位于中心（pos=0）时，「绕自身」与「绕屏幕中心」在代数上重合，
    故本条不能区分两种合成方式；区分见下一条。
    """
    hit = single(
        line_overrides={
            "judgeLineRotateEvents": [{"startTime": 0.0, "endTime": 1e9, "start": 90.0, "end": 90.0}]
        },
        notes=[make_note(position_x=1.0)],
    )
    assert hit.px == pytest.approx(960.0)
    assert hit.py == pytest.approx(540.0 - X_UNIT * 960.0)


def test_rotation_origin_is_the_line_itself():
    """判定线绕自身位置旋转，而不是绕屏幕中心。

    判定线位于屏幕右缘（moveEvent x=0.9）并竖直 90°。若绕屏幕中心旋转，
    线中心会被甩到 (0, 0.8)——而纵向可见范围只有 |y| <= 1/(16/9) = 0.5625，
    整条线飞出屏幕。官谱 Rrhar'il AT 的 line 2（line_x≈0.8、80°~90°）正是这种
    情形，且其上 42 个音符必须在 33.158s / 60s 被击打：能被打到的线只能绕自身旋转。
    """
    hit = single(
        line_overrides={
            "judgeLineMoveEvents": [
                {"startTime": 0.0, "endTime": 1e9, "start": 0.9, "end": 0.9, "start2": 0.5, "end2": 0.5}
            ],
            "judgeLineRotateEvents": [{"startTime": 0.0, "endTime": 1e9, "start": 90.0, "end": 90.0}],
        },
        notes=[make_note(position_x=1.0)],
    )
    assert hit.world_x == pytest.approx(-1.0 + 0.9 * 2.0)  # 线中心仍停在右缘
    assert hit.px == pytest.approx(0.9 * W)
    assert hit.world_y == pytest.approx(X_UNIT)  # 局部 x 被转到竖直方向
    assert hit.py == pytest.approx(540.0 - X_UNIT * 960.0)


def test_world_scale_is_isotropic():
    """世界坐标各向同性：1 世界单位恒等于屏幕半宽。"""
    hit = build(notes=[make_note(position_x=2.0)]).notes()[0]
    assert hit.world_x == pytest.approx(2.0 * X_UNIT)
    assert hit.px - 960.0 == pytest.approx(hit.world_x * (W / 2.0))


def test_aspect_ratio_compresses_vertical_offset():
    """y 进入世界坐标前要除以 aspect_ratio；改比例会改变纵向落点。"""
    hit = single(
        line_overrides={
            "judgeLineMoveEvents": [
                {"startTime": 0.0, "endTime": 1e9, "start": 0.5, "end": 0.5, "start2": 0.75, "end2": 0.75}
            ]
        }
    )
    expected_world_y = (-1.0 + 0.75 * 2.0) / (16.0 / 9.0)
    assert hit.world_y == pytest.approx(expected_world_y)
    assert hit.py == pytest.approx(H / 2 - expected_world_y * (W / 2.0))


def test_custom_screen_scales_pixels():
    hit_small = build(screen=(960.0, 540.0)).notes()[0]
    assert hit_small.px == pytest.approx(480.0)
    assert hit_small.py == pytest.approx(270.0)


# ---------------------------------------------------------------- 音符语义

def test_notes_below_is_flagged():
    line = make_line()
    line["notesAbove"] = []
    line["notesBelow"] = [make_note()]
    hits = Chart(make_chart([line])).notes()
    assert len(hits) == 1
    assert hits[0].above is False


def test_hold_duration_shares_time_unit():
    hit = build(notes=[make_note(type_=3, hold_time=64)]).notes()[0]
    assert hit.type_name == "Hold"
    assert hit.hold_sec == pytest.approx(2.0)


def test_multiple_hint_marks_simultaneous_notes():
    hits = build(notes=[make_note(time=32), make_note(time=32, position_x=1.0)]).notes()
    assert len(hits) == 2
    assert all(h.multiple for h in hits)


def test_multiple_hint_false_for_single_note():
    assert build().notes()[0].multiple is False


def test_unknown_format_version_rejected():
    with pytest.raises(ValueError):
        Chart({"formatVersion": 2, "offset": 0.0, "judgeLineList": []})


def test_format_v1_unpacks_1000x_plus_y():
    """v1 把 x/y 压成 1000x+y，x 域 0..880、y 域 0..520；(440,260) 应落在屏幕中心。"""
    packed = 440.0 * 1000.0 + 260.0
    hit = single(
        line_overrides={
            "judgeLineMoveEvents": [{"startTime": 0.0, "endTime": 1e9, "start": packed, "end": packed}]
        },
        fmt=1,
    )
    assert hit.px == pytest.approx(960.0)
    assert hit.py == pytest.approx(540.0)


# ---------------------------------------------------------------- 速度积分

#: dy 的尺度除数：HEIGHT_RATIO × aspect。1 单位「高度差」对应这么多世界单位。
TRAVEL_DIV = HEIGHT_RATIO * ASPECT


def speed_event(start, end, value):
    return {"startTime": start, "endTime": end, "value": value}


def test_speed_integral_single_event_extrapolates_to_max_time():
    """只有一个事件时没有可累加的前序事件，曲线直接按该事件的 value 从 0 外推。

    bpm=60 ⇒ r = 60/32/60 = 0.03125。
    """
    r = 60.0 / 32.0 / 60.0
    a = parse_speed_events(r, [speed_event(0, 32, 2.0)], max_time=5.0)
    assert a.at(0.0) == pytest.approx(0.0)
    assert a.at(2.5) == pytest.approx(5.0)  # 0 + 2.5 * 2.0
    assert a.at(5.0) == pytest.approx(10.0)


def test_speed_integral_accumulates_per_event():
    """两段：0→32 拍 @2.0，其后 @1.0。

    前段贡献 (32-0)*0.03125*2.0 = 2.0，记在第二段起点 t=1.0 上；
    之后按第二段的 value=1.0 外推到 max_time。
    """
    r = 60.0 / 32.0 / 60.0
    a = parse_speed_events(
        r, [speed_event(0, 32, 2.0), speed_event(32, 1e9, 1.0)], max_time=5.0
    )
    assert a.at(0.5) == pytest.approx(1.0)   # 2.0 * 0.5
    assert a.at(1.0) == pytest.approx(2.0)   # 前段总量
    assert a.at(3.0) == pytest.approx(4.0)   # 2.0 + (3.0-1.0) * 1.0


def test_speed_integral_clamps_first_event_start_to_zero():
    """官谱首事件 startTime 常为 -999999 之类的哨兵，被强制拉到 0。"""
    r = 60.0 / 32.0 / 60.0
    a = parse_speed_events(r, [speed_event(-999999, 32, 2.0)], max_time=5.0)
    assert a.at(5.0) == pytest.approx(10.0)


def test_speed_integral_drops_invalid_ranges():
    r = 60.0 / 32.0 / 60.0
    a = parse_speed_events(r, [speed_event(50, 10, 2.0)], max_time=5.0)
    assert a.at(5.0) == pytest.approx(0.0)  # 唯一事件被丢弃 → 常量 0


def test_speed_integral_empty_is_zero():
    assert parse_speed_events(0.03125, [], max_time=5.0).at(3.0) == 0.0


# ---------------------------------------------------------------- 音符轨迹

def _moving_line_notes(bucket, note):
    line = make_line(speedEvents=[speed_event(0, 32, 2.0), speed_event(32, 1e9, 1.0)])
    line["notesAbove"] = [note] if bucket == "notesAbove" else []
    line["notesBelow"] = [note] if bucket == "notesBelow" else []
    return Chart(make_chart([line]))


def test_note_height_comes_from_speed_integral():
    """bpm=60、音符 time=32（t=1.0s）：此时曲线值 2.0。"""
    hit = _moving_line_notes("notesAbove", make_note(time=32)).notes()[0]
    assert hit.t_sec == pytest.approx(1.0)
    assert hit.note_h == pytest.approx(2.0)


def test_note_lands_exactly_on_its_line_at_hit_time():
    """核心不变量：击打时刻 dy 恒为 0，音符落在判定线上。"""
    chart = _moving_line_notes("notesAbove", make_note(time=32))
    n = chart.notes()[0]
    assert chart.note_travel(n, n.t_sec) == pytest.approx(0.0, abs=1e-12)


def test_note_travel_before_hit_is_positive_and_scales_with_height_gap():
    """未到击打时刻时音符在线上方，距离 = 高度差 / (HEIGHT_RATIO × aspect)。

    t=0.5 时曲线值 1.0，音符高度 2.0 ⇒ 差 1.0 ⇒ dy = 1.0 / TRAVEL_DIV。
    """
    chart = _moving_line_notes("notesAbove", make_note(time=32))
    hit = chart.notes()[0]
    assert chart.note_travel(hit, 0.5) == pytest.approx(1.0 / TRAVEL_DIV)
    assert chart.note_travel(hit, 0.0) == pytest.approx(2.0 / TRAVEL_DIV)


def test_note_travel_is_negative_after_hit():
    """过线后挂到另一侧：t=1.5 时曲线已外推到 2.0 + 0.5*1.0 = 2.5，高于音符高度 2.0。"""
    chart = _moving_line_notes("notesAbove", make_note(time=32))
    hit = chart.notes()[0]
    assert chart.note_travel(hit, 1.5) == pytest.approx(-0.5 / TRAVEL_DIV)


def test_below_note_is_mirrored_about_the_line():
    """下侧音符是上侧关于判定线自身的镜像：同一时刻 dy 反号。"""
    chart_a = _moving_line_notes("notesAbove", make_note(time=32))
    chart_b = _moving_line_notes("notesBelow", make_note(time=32))
    above, below = chart_a.notes()[0], chart_b.notes()[0]

    for t in (0.0, 0.5, 1.0, 1.5):
        assert chart_b.note_travel(below, t) == pytest.approx(-chart_a.note_travel(above, t))


def test_below_and_above_share_the_same_hit_point():
    """镜像轴是判定线自身，dy=0 上的点是镜像不动点 ⇒ 击打点与上下侧无关。"""
    above = _moving_line_notes("notesAbove", make_note(time=32, position_x=1.0)).notes()[0]
    below = _moving_line_notes("notesBelow", make_note(time=32, position_x=1.0)).notes()[0]
    assert (above.px, above.py) == pytest.approx((below.px, below.py))


def test_note_travel_uses_speed_field_to_scale_distance():
    """音符 speed 放大进场距离（Phira 里乘在 dy 上）。"""
    chart_s = _moving_line_notes("notesAbove", make_note(time=32, speed=1.0))
    chart_f = _moving_line_notes("notesAbove", make_note(time=32, speed=2.0))
    assert chart_f.note_travel(chart_f.notes()[0], 0.5) == pytest.approx(
        2.0 * chart_s.note_travel(chart_s.notes()[0], 0.5)
    )


def test_hold_note_ignores_speed_field():
    """Hold（type 3）在 Phira 里被强制 speed=1.0。"""
    chart = _moving_line_notes(
        "notesAbove", make_note(type_=3, time=32, speed=3.0, hold_time=64)
    )
    hit = chart.notes()[0]
    assert hit.speed == pytest.approx(3.0)  # 字段本身保留
    assert chart.note_travel(hit, 0.5) == pytest.approx(1.0 / TRAVEL_DIV)  # 但按 1.0 算


def test_note_pos_trajectory_passes_through_the_hit_point():
    """轨迹在 t_hit 处必须与 M1 的击打点重合（两套公式的回归一致性）。"""
    line = make_line(
        judgeLineMoveEvents=[
            {"startTime": 0.0, "endTime": 1e9, "start": 0.8, "end": 0.3, "start2": 0.6, "end2": 0.4}
        ],
        judgeLineRotateEvents=[{"startTime": 0.0, "endTime": 1e9, "start": 35.0, "end": 35.0}],
        speedEvents=[speed_event(0, 32, 2.0), speed_event(32, 1e9, 1.0)],
    )
    line["notesAbove"] = [make_note(time=32, position_x=1.5)]
    chart = Chart(make_chart([line]))
    hit = chart.notes()[0]

    wx, wy = chart.note_pos(hit, hit.t_sec)
    assert (wx, wy) == pytest.approx((hit.world_x, hit.world_y))
    assert chart.to_px(wx, wy) == pytest.approx((hit.px, hit.py))


def test_note_pos_before_hit_is_further_along_the_line_normal():
    """进场阶段音符沿判定线局部 y 轴（其法线）靠近，正交于线方向。"""
    line = make_line(
        judgeLineRotateEvents=[{"startTime": 0.0, "endTime": 1e9, "start": 90.0, "end": 90.0}],
        speedEvents=[speed_event(0, 32, 2.0), speed_event(32, 1e9, 1.0)],
    )
    line["notesAbove"] = [make_note(time=32)]
    chart = Chart(make_chart([line]))
    hit = chart.notes()[0]

    # 线在 90°（竖直，位置在屏幕中心）：局部 y 轴指向世界 x 方向 ⇒ 音符按 x 移动
    before = chart.note_pos(hit, 0.5)
    assert before[1] == pytest.approx(hit.world_y)          # 世界 y 不变
    assert before[0] - hit.world_x == pytest.approx(-1.0 / TRAVEL_DIV)  # 沿世界 x 偏移


def test_no_floor_position_mismatch_on_synthetic_chart():
    """合成谱里 floorPosition 字段留 0 但重算得 2.0 ⇒ 应被记入分歧清单。"""
    chart = _moving_line_notes("notesAbove", make_note(time=32, floor_position=0.0))
    chart.notes()
    assert len(chart.note_h_mismatch) == 1
    assert chart.note_h_mismatch[0][2] == pytest.approx(2.0)


def test_floor_position_field_is_honoured_when_consistent():
    chart = _moving_line_notes("notesAbove", make_note(time=32, floor_position=2.0))
    chart.notes()
    assert chart.note_h_mismatch == []


# ---------------------------------------------------------------- 真实谱面

@pytest.fixture(scope="module")
def sample_chart():
    if not SAMPLE.is_file():
        pytest.skip("样本谱面缺失")
    return Chart(json.loads(SAMPLE.read_text(encoding="utf-8")))


@pytest.fixture(scope="module")
def hits(sample_chart):
    return sample_chart.notes()


class TestRealChart:
    def test_note_count(self, hits):
        assert len(hits) == 1300

    def test_type_distribution(self, hits):
        from collections import Counter

        dist = Counter(h.type for h in hits)
        assert dist == {1: 991, 2: 44, 3: 123, 4: 142}

    def test_times_are_sorted_and_non_negative(self, hits):
        times = [h.t_sec for h in hits]
        assert times == sorted(times)
        assert times[0] >= 0.0

    def test_all_hits_land_on_screen(self, hits):
        """全部击打点应落在屏幕内——若有越界说明坐标换算有系统性错误。"""
        outside = [h for h in hits if not (0.0 <= h.px <= W and 0.0 <= h.py <= H)]
        assert outside == [], f"{len(outside)} 个音符越界，例如 {outside[:3]}"

    def test_lines_are_indexed(self, hits):
        assert all(0 <= h.line < 24 for h in hits)

    # ---- M2 轨迹层 ----

    def test_speed_integral_matches_chart_floor_position(self, sample_chart, hits):
        """**M2 的核心验收**：自己按 speedEvents 积分出的判定线高度曲线，
        与官谱每个音符自带的 floorPosition 字段逐音符吻合。

        这条独立于任何参考实现：floorPosition 是谱面自己写下的数据。
        """
        assert sample_chart.note_h_mismatch == [], (
            f"{len(sample_chart.note_h_mismatch)} 个音符的高度与 floorPosition 不符，"
            f"例如 {sample_chart.note_h_mismatch[:3]}"
        )

    def test_trajectory_reproduces_the_hit_point(self, sample_chart, hits):
        """轨迹公式在 t_hit 处必须退化成 M1 的击打点。"""
        worst = 0.0
        for h in hits:
            wx, wy = sample_chart.note_pos(h, h.t_sec)
            worst = max(worst, abs(wx - h.world_x), abs(wy - h.world_y))
        assert worst < 1e-9, f"最大偏差 {worst}"

    def test_notes_approach_monotonically(self, sample_chart, hits):
        """进场阶段音符必须**单调**靠近判定线（高度曲线非降 ⇒ |dy| 不减）。"""
        bad = []
        for h in hits:
            prev = None
            for dt in (0.4, 0.2, 0.1, 0.05, 0.0):
                d = abs(sample_chart.note_travel(h, h.t_sec - dt))
                if prev is not None and d > prev + 1e-9:
                    bad.append((h.line, h.t_sec, dt, d, prev))
                    break
                prev = d
        assert bad == [], f"{len(bad)} 个音符靠近过程不单调，例如 {bad[:3]}"

    def test_entry_distance_is_order_one(self, sample_chart, hits):
        """尺度常量自检：音符在「击打前 0.3 秒」离线的距离应当是 1 的量级。

        若 HEIGHT_RATIO 或 aspect 用错量级，这里会立刻暴露成几十上百。
        """
        dists = sorted(
            abs(sample_chart.note_travel(h, h.t_sec - 0.3)) for h in hits
        )
        mid = dists[len(dists) // 2]
        assert 0.05 < mid < 5.0, f"中位进场距离 {mid}，量级可疑"
