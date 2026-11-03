"""验收脚本自身的测试。

一个「永远报 PASS」的验收脚本等于没有验收。所以这里**故意注入各类缺陷**，
断言 `check_chart_structure` 能抓住；同时断言它**不会**对那些从 1064 个官方
谱面里枚举出来的合法变体误报。

合法变体清单（每条都对应真实数据里的一个形态）：

| 变体 | 真实出现 |
|---|---|
| `formatVersion: 1` | 29 个谱面 / 15 首老曲 |
| 根多出 `blockAreaList` | 30 个谱面（4.x 新增的屏幕遮罩） |
| 根多出 `numOfNotes` | 6 个谱面 |
| 判定线多出 `numOfNotesAbove/Below` | 144 条判定线 |
| `speedEvents` 带 `floorPosition` | 402 条事件 / 6 个谱面 |
| v1 的 move 事件无 `start2/end2` | 28111 条 |
| v3 的 rotate/disappear 带 `start2/end2` | 48708 / 50070 条 |
| 反向区间 `endTime < startTime` | 73 处 / 16 首 |
"""

from __future__ import annotations

from collections import Counter

import pytest

from verify_extract import (
    FLOAT32_EPS,
    check_chart_structure,
    max_chart_time,
    speed_integral_at,
)

NOTE = {"type": 1, "time": 0.0, "positionX": 0.0, "holdTime": 0.0,
        "speed": 1.0, "floorPosition": 0.0}


def ev(s, e, **extra):
    """构造一个事件。形参名刻意用 `s`/`e` —— 事件自带 `start`/`end` 字段，
    若形参也叫 `start`/`end` 就会与关键字参数撞名。"""
    return {"startTime": s, "endTime": e, **extra}


def move_ev(start, end, x=0.0, y=0.0, xy2=True):
    d = ev(start, end, start=x, end=x)
    if xy2:
        d.update(start2=y, end2=y)
    return d


def make_chart(version=3, *, lines=None, **root_extra):
    """造一个最小合法谱面。"""
    if lines is None:
        lines = [make_line(version)]
    return {"formatVersion": version, "offset": 0.0,
            "judgeLineList": lines, **root_extra}


def make_line(version=3, *, notes=None, move=None, rotate=None,
              disappear=None, speed=None, **extra):
    v3 = version == 3
    return {
        "bpm": 190.0,
        "notesAbove": notes if notes is not None else [dict(NOTE)],
        "notesBelow": [],
        "speedEvents": speed if speed is not None else [ev(0.0, 1e9, value=1.0)],
        "judgeLineMoveEvents": move if move is not None else [move_ev(0.0, 100.0, xy2=v3)],
        "judgeLineRotateEvents": rotate if rotate is not None else [ev(0.0, 100.0, start=0.0, end=0.0)],
        "judgeLineDisappearEvents": disappear if disappear is not None else [ev(0.0, 100.0, start=1.0, end=1.0)],
        **extra,
    }


def check(chart) -> tuple[list[str], Counter]:
    neg = Counter()
    return check_chart_structure(chart, neg), neg


# --------------------------------------------------------------------------- #
# 合法变体：一条都不许误报
# --------------------------------------------------------------------------- #


def test_minimal_v3_chart_is_valid():
    errs, _ = check(make_chart())
    assert errs == []


def test_v1_chart_is_valid():
    """v1 的 move 事件没有 start2/end2。"""
    errs, _ = check(make_chart(version=1))
    assert errs == []


def test_v1_move_event_with_start2_is_rejected():
    """反过来：v1 不该有 start2/end2（现实里 0 条）。"""
    line = make_line(1, move=[move_ev(0.0, 100.0, xy2=True)])
    errs, _ = check(make_chart(version=1, lines=[line]))
    assert any("未知字段" in e for e in errs), errs


def test_root_may_carry_blockarea():
    """4.x 新增：30 个谱面的根多出 blockAreaList。"""
    errs, _ = check(make_chart(blockAreaList=[
        {"topRightPercentage": {"x": 1.1, "y": 1.2},
         "bottomLeftPercentage": {"x": -0.1, "y": 1.0},
         "appearTime": 1.0, "enableTime": 2.0,
         "disableTime": 3.0, "disappearTime": 3.0,
         "isSubtract": True, "rotateEvents": [], "moveEvents": [], "scaleEvents": []},
    ]))
    assert errs == []


def test_root_may_carry_numofnotes():
    """6 个谱面的根多出 numOfNotes。"""
    errs, _ = check(make_chart(numOfNotes=1300))
    assert errs == []


def test_line_may_carry_numofnotes():
    """144 条判定线多出 numOfNotes / numOfNotesAbove / numOfNotesBelow。"""
    line = make_line(numOfNotes=5, numOfNotesAbove=5, numOfNotesBelow=0)
    errs, _ = check(make_chart(lines=[line]))
    assert errs == []


def test_speed_event_may_carry_floorposition():
    """402 条 speedEvents 自带 floorPosition 快照。"""
    line = make_line(speed=[ev(0.0, 1e9, value=1.5, floorPosition=0.0)])
    errs, _ = check(make_chart(lines=[line]))
    assert errs == []


def test_rotate_and_disappear_may_carry_start2():
    """v3 的标量通道偶带 start2/end2（48708 / 50070 条）。"""
    line = make_line(
        rotate=[ev(0.0, 100.0, start=0.0, end=0.0, start2=0.0, end2=0.0)],
        disappear=[ev(0.0, 100.0, start=1.0, end=1.0, start2=0.0, end2=0.0)],
    )
    errs, _ = check(make_chart(lines=[line]))
    assert errs == []


def test_reverse_span_events_are_legal_but_counted():
    """反向区间合法，且必须被**统计**（供上层断言「没碰速度通道」）。"""
    line = make_line(disappear=[ev(912.0, 896.0, start=1.0, end=0.0)])
    errs, neg = check(make_chart(lines=[line]))
    assert errs == []
    assert neg["judgeLineDisappearEvents"] == 1


def test_hold_note_needs_positive_holdtime():
    good = dict(NOTE, type=3, holdTime=128.0)
    errs, _ = check(make_chart(lines=[make_line(notes=[good])]))
    assert errs == []
    bad = dict(NOTE, type=3, holdTime=0.0)
    errs, _ = check(make_chart(lines=[make_line(notes=[bad])]))
    assert any("holdTime" in e for e in errs), errs


def test_all_four_note_types_accepted():
    notes = [dict(NOTE, type=t, holdTime=8.0 if t == 3 else 0.0) for t in (1, 2, 3, 4)]
    errs, _ = check(make_chart(lines=[make_line(notes=notes)]))
    assert errs == []


# --------------------------------------------------------------------------- #
# 注入缺陷：一条都不许放过
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("drop", ["formatVersion", "offset", "judgeLineList"])
def test_detects_missing_root_field(drop):
    c = make_chart()
    del c[drop]
    errs, _ = check(c)
    assert errs, f"删掉 {drop} 应该报错"
    if drop != "judgeLineList":
        assert any("根缺必需字段" in e or "不在" in e for e in errs), errs


def test_detects_unknown_root_field():
    c = make_chart()
    c["somethingNew"] = 1
    errs, _ = check(c)
    assert any("未知字段" in e for e in errs), errs


def test_detects_unknown_line_field():
    line = make_line()
    line["bpmOverride"] = 200.0
    errs, _ = check(make_chart(lines=[line]))
    assert any("未知字段" in e for e in errs), errs


def test_detects_missing_event_channel():
    line = make_line()
    del line["speedEvents"]
    errs, _ = check(make_chart(lines=[line]))
    assert any("缺必需字段" in e for e in errs), errs


def test_detects_note_missing_field():
    bad = dict(NOTE)
    del bad["floorPosition"]
    errs, _ = check(make_chart(lines=[make_line(notes=[bad])]))
    assert any("字段不符" in e for e in errs), errs


def test_detects_note_with_extra_field():
    errs, _ = check(make_chart(lines=[make_line(notes=[dict(NOTE, isFake=True)])]))
    assert any("字段不符" in e for e in errs), errs


def test_detects_unknown_note_type():
    errs, _ = check(make_chart(lines=[make_line(notes=[dict(NOTE, type=9)])]))
    assert any("type" in e for e in errs), errs


def test_detects_v3_move_event_missing_start2():
    line = make_line(3, move=[move_ev(0.0, 100.0, xy2=False)])
    errs, _ = check(make_chart(lines=[line]))
    assert any("缺字段" in e and "start2" in e for e in errs), errs


def test_detects_totally_unknown_event_field():
    line = make_line(speed=[ev(0.0, 1.0, value=1.0, easingType=2)])
    errs, _ = check(make_chart(lines=[line]))
    assert any("未知字段" in e for e in errs), errs


def test_detects_bad_format_version():
    errs, _ = check(make_chart(version=2))
    assert any("formatVersion" in e for e in errs), errs


def test_detects_non_positive_bpm():
    line = make_line()
    line["bpm"] = 0.0
    errs, _ = check(make_chart(lines=[line]))
    assert any("bpm" in e for e in errs), errs


# --------------------------------------------------------------------------- #
# 独立速度积分实现
# --------------------------------------------------------------------------- #


def test_speed_integral_is_zero_without_events():
    assert speed_integral_at([], 1.0, 5.0) == 0.0


def test_speed_integral_constant_value():
    """单位速度、事件覆盖全程 → h(t) == t。"""
    evs = [ev(0.0, 1e9, value=1.0)]
    assert speed_integral_at(evs, 1.0, 0.0) == pytest.approx(0.0)
    assert speed_integral_at(evs, 1.0, 3.5) == pytest.approx(3.5)
    assert speed_integral_at(evs, 1.0, 100.0) == pytest.approx(100.0)


def test_speed_integral_is_piecewise_linear():
    """前 10 拍速度 1，之后速度 2（拍长 r=1 便于手算）。"""
    evs = [ev(0.0, 10.0, value=1.0), ev(10.0, 1e9, value=2.0)]
    assert speed_integral_at(evs, 1.0, 4.0) == pytest.approx(4.0)
    assert speed_integral_at(evs, 1.0, 10.0) == pytest.approx(10.0)
    assert speed_integral_at(evs, 1.0, 15.0) == pytest.approx(20.0)


def test_speed_integral_extrapolates_past_last_event():
    """末事件在 10 拍结束，t=15 时应按它的 value 继续外推（M2 的末帧口径）。"""
    evs = [ev(0.0, 10.0, value=1.0)]
    assert speed_integral_at(evs, 1.0, 10.0) == pytest.approx(10.0)
    assert speed_integral_at(evs, 1.0, 15.0) == pytest.approx(15.0)


def test_speed_integral_skips_reverse_spans():
    """反向区间按空窗口跳过（与 B 段同口径）。"""
    evs = [ev(0.0, 10.0, value=1.0), ev(12.0, 8.0, value=99.0), ev(20.0, 1e9, value=1.0)]
    # 反向事件不贡献任何高度；12→20 之间的空档也不累积（无事件覆盖）
    assert speed_integral_at(evs, 1.0, 20.0) == pytest.approx(10.0)
    assert speed_integral_at(evs, 1.0, 25.0) == pytest.approx(15.0)


def test_speed_integral_matches_pgauto_implementation():
    """与 B 段的 `pgauto.events.parse_speed_events` 交叉核对（同一份输入）。"""
    from pgauto.events import parse_speed_events

    evs = [ev(0.0, 5.0, value=1.0), ev(5.0, 12.0, value=2.5), ev(12.0, 40.0, value=0.5)]
    r, max_t = 1.0, 50.0
    curve = parse_speed_events(r, evs, max_t)
    for t in (0.0, 1.0, 4.999, 5.0, 7.5, 12.0, 30.0, 49.0):
        assert speed_integral_at(evs, r, t) == pytest.approx(curve.at(t), abs=1e-9), t


def test_max_chart_time_includes_hold_and_margin():
    """全谱最大时刻要算上 Hold 尾巴，并留 1s 余量。"""
    line = make_line(notes=[dict(NOTE, time=320.0, type=3, holdTime=64.0)])
    line["bpm"] = 60.0  # r = 60/32/60 = 1/32 秒/拍
    j = make_chart(lines=[line])
    # 320 拍 × 1/32 = 10s；Hold 再占 64 拍 = 2s ⇒ 12s；+1s 余量 = 13s
    assert max_chart_time(j) == pytest.approx(13.0)


def test_float32_eps_constant_matches_definition():
    assert FLOAT32_EPS == 2.0**-23
