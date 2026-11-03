"""HTML 回放的数据打包测试。

这里锁的是一个**真实踩过的 bug**：把关键帧时间舍入到 N 位、而音符时刻舍入到 M 位
（N != M）时，官谱里「音符时刻恰好等于事件时刻」的重合会消失，JS 在那一瞬会取到
台阶**前**的帧 —— rotate 差 80°、高度差 0.067（≈ 324 px）。

所以本文件的核心断言是：**用打包后的扁平数组复算一遍轨迹，必须与 `Chart` 一致**。
它同时覆盖了三件事：共线合并没改变取值、时间精度一致、扁平数组的二分求值语义
（取最后一个 time <= t 的帧）与 Python 的 `AnimFloat` 相同。
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TOOLS = PROJECT_ROOT / "tools"
for p in (str(PROJECT_ROOT), str(TOOLS)):
    if p not in sys.path:
        sys.path.insert(0, p)

from pgauto.chart import HEIGHT_RATIO, X_UNIT, Chart, load  # noqa: E402
from render_chart import build_payload, flat, simplify  # noqa: E402

SAMPLE = PROJECT_ROOT / "samples" / "rrhar_at" / "Rrharil.TeamGrimoire.0.json"
CALLS = [0.0, -0.2, 0.15, -0.6]


def at_flat(f, t: float) -> float:
    """扁平数组求值 —— 与回放里 JS 的 `at()` 逐行等价（语义：取最后一个 time <= t 的帧）。"""
    n = len(f) >> 1
    if n == 0:
        return 0.0
    if t <= f[0]:
        return f[1]
    if t >= f[2 * (n - 1)]:
        return f[2 * (n - 1) + 1]
    lo, hi = 0, n - 1
    while lo < hi - 1:
        mid = (lo + hi) // 2
        if f[2 * mid] <= t:
            lo = mid
        else:
            hi = mid
    t0, v0 = f[2 * lo], f[2 * lo + 1]
    t1, v1 = f[2 * hi], f[2 * hi + 1]
    if t1 == t0:
        return v1
    return v0 + (v1 - v0) * (t - t0) / (t1 - t0)


# ---------------------------------------------------------------- simplify

def test_simplify_collapses_constant_run():
    kfs = [(float(i), 3.0) for i in range(500)]
    assert simplify(kfs) == [(0.0, 3.0), (499.0, 3.0)]


def test_simplify_collapses_collinear_run():
    kfs = [(float(i), float(i) * 2.0) for i in range(10)]
    assert simplify(kfs) == [(0.0, 0.0), (9.0, 18.0)]


def test_simplify_keeps_vertices():
    kfs = [(0.0, 0.0), (1.0, 1.0), (2.0, 0.0)]
    assert simplify(kfs) == kfs


def test_simplify_keeps_duplicate_time_steps():
    """同一时刻的两帧是「瞬变」（方波），必须原样保留，否则台阶会被抹成斜坡。"""
    kfs = [(0.0, 0.0), (1.0, 10.0), (1.0, 0.0), (2.0, 0.0)]
    assert simplify(kfs) == kfs


def test_simplify_preserves_values_everywhere():
    kfs = [(0.0, 0.0), (1.0, 1.0), (1.0, 5.0), (3.0, 5.0), (4.0, -2.0), (5.0, -2.0)]
    s = simplify(kfs)

    def py_at(seq, t):
        from bisect import bisect_right

        times = [k[0] for k in seq]
        i = bisect_right(times, t) - 1
        if i < 0:
            return seq[0][1]
        if i >= len(seq) - 1:
            return seq[-1][1]
        t0, v0 = seq[i]
        t1, v1 = seq[i + 1]
        return v1 if t1 == t0 else v0 + (v1 - v0) * (t - t0) / (t1 - t0)

    for k in range(501):
        t = 5.0 * k / 500
        assert py_at(s, t) == pytest.approx(py_at(kfs, t), abs=1e-12)


def test_flat_does_not_round_times():
    """时间必须原值透传。

    舍入会把不连续点挪动最多 5e-10s；而官谱里「音符时刻 == 事件时刻」是常态，
    台阶恰好落在击打时刻上（实测 line 14 的 rotate 在 t=13.894736842105262 处
    从 -10° 跳到 0°，而该时刻正是某个音符的击打时刻）。挪动台阶会让
    「在哪个时刻求值」的结论翻转。
    """
    t = 1408 * (60.0 / 32.0 / 190.0)
    f = flat([(t, 0.5)], True, 6)
    assert f[0] == t                       # 逐位相等，不是 approx
    assert flat([(1.0 / 3.0, 0.0)], True, 6)[0] == 1.0 / 3.0
    assert flat([(t, 1.0 / 3.0)], True, 6)[1] == pytest.approx(1.0 / 3.0, abs=1e-6)


# ---------------------------------------------------------------- 打包后复算

@pytest.fixture(scope="module")
def packed():
    if not SAMPLE.is_file():
        pytest.skip("样本谱面缺失")
    chart = Chart(load(SAMPLE))
    payload = build_payload(chart, SAMPLE.name, 1.5, True, 6)
    return chart, payload


def test_payload_times_are_bit_identical_to_the_model(packed):
    """音符时刻必须与 `Chart` 里的一致到**逐位**。

    这是「台阶恰好落在击打时刻上」这类边界情况能被正确求值的前提。
    """
    chart, payload = packed
    hits = chart.notes()
    assert len(payload["notes"]) == len(hits)
    bad = [
        i
        for i, (n, h) in enumerate(zip(payload["notes"], hits))
        if n["t"] != h.t_sec or n["nh"] != round(h.note_h, 6)
    ]
    assert bad == [], f"{len(bad)} 个音符时刻/高度不一致，例如下标 {bad[:5]}"


def test_payload_reproduces_trajectory_after_flattening(packed):
    """**核心回归**：用打包后的扁平数组（共线合并 + 取值舍入 + 二分求值）复算轨迹，
    必须与 Python 侧 `Chart.note_pos` 一致。

    这条断言若失败，说明「屏幕上演的」与「交付给 C 段的坐标」脱节了。
    """
    chart, payload = packed
    aspect = payload["meta"]["aspect"]
    lines = {ln["id"]: ln for ln in payload["lines"]}
    notes = payload["notes"]
    hits = chart.notes()
    assert len(notes) == len(hits) == 1300

    worst = (0.0, None)
    for n, h in zip(notes, hits):
        ln = lines[n["l"]]
        for dt in CALLS:
            t = n["t"] + dt
            lx = at_flat(ln["x"], t)
            ly = at_flat(ln["y"], t) / aspect
            ang = at_flat(ln["r"], t)
            dy = (n["nh"] - at_flat(ln["h"], t)) / (HEIGHT_RATIO * aspect) * n["sp"]
            if not n["u"]:
                dy = -dy
            rad = math.radians(ang)
            ca, sa = math.cos(rad), math.sin(rad)
            wx = lx + n["bx"] * ca - dy * sa
            wy = ly + n["bx"] * sa + dy * ca

            ex, ey = chart.note_pos(h, t)
            d = max(abs(wx - ex), abs(wy - ey))
            if d > worst[0]:
                worst = (d, (n["l"], round(n["t"], 4), dt))
    # 阈值 1e-5 世界单位 = 0.008 px（画布半宽 800）—— 只留取值舍入的余量
    assert worst[0] < 1e-5, f"最大偏差 {worst[0]:.3e} 世界单位 @ {worst[1]}"


def test_payload_is_json_serialisable_and_small(packed):
    _chart, payload = packed
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    assert len(data) < 2 * 1024 * 1024, f"数据 {len(data)} 字节，过大"
    assert set(payload) == {"meta", "lines", "notes"}


def test_payload_note_fields(packed):
    _chart, payload = packed
    assert set(payload["notes"][0]) == {"l", "u", "ty", "bx", "nh", "ne", "sp", "t", "hl"}
