"""`tools/verify_dev_coords.py` 的测试 —— 坐标审计自己必须能抓错。

★ 为什么值得写：这个工具是 2026-10-24 那场「Miss 60~69%」的唯一防线。
   当时所有既有判据全绿（关键帧 1682/1682 送达、收货侧坐标逐帧吻合），
   却没人检查「注入坐标是否等于载荷该算出的坐标」⇒ 跑了两枪坏的。
   工具若把「坐标错位」判成「一致」，就是第二次白打；若把「一致」判成「错位」，
   就会推动去重装/重刷缓存，同样白跑。

★★ 前四条用例是**真机实证的锚**：`(842,221)` / `(768,499)` 与错误口径下的
   `(815,324)` / `(795,396)` 都是 2026-10-24 用 JDK17 编译端内 `Scheduler`
   在 PC 上跑出来的真值，不是我们照抄自己的公式算出来的。所以它们能抓住
   「Python 侧与 Java 侧公式漂移」这类最贵的不一致。
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.verify_dev_coords import (  # noqa: E402
    ACT_DOWN, ACT_MOVE, ACT_POINTER_DOWN, ACT_POINTER_UP, ACT_UP,
    Key, compare, expected_map, linear_fit, main, to_device,
)

D, M, PD = ACT_DOWN, ACT_MOVE, ACT_POINTER_DOWN

CANVAS = (1920.0, 1080.0)
SCREEN = (1080.0, 2340.0)
DST = (1610.0, 720.0)


# ─────────────────────────── 真机锚（JDK 跑端内 Scheduler 得到） ───────────────────────────

@pytest.mark.parametrize(
    "x, y, canvas, want",
    [
        # 正确口径：canvas=1920x1080。这两条来自装机 asset 的载荷。
        (571.447, 1052.64, CANVAS, (842, 221)),
        (508.553, 1287.36, CANVAS, (768, 499)),
        (400.601, 1052.64, CANVAS, (640, 221)),
        # 错误口径：canvas 被填成 screen=1080x2340 —— 正是 run5/run6 实际注入的位置。
        (571.447, 1052.64, SCREEN, (815, 324)),
        (508.553, 1287.36, SCREEN, (795, 396)),
    ],
)
def test_to_device_matches_real_device_anchors(x, y, canvas, want):
    assert to_device(x, y, canvas, SCREEN, DST) == want


def test_fit_flag_actually_changes_result():
    """变异防护：fit=height 若被静默忽略，结果必须不同（否则这个参数是空转的）。"""
    a = to_device(571.447, 1052.64, CANVAS, SCREEN, DST, fit_height=True)
    b = to_device(571.447, 1052.64, CANVAS, SCREEN, DST, fit_height=False)
    assert a == (842, 221)
    assert a != b


def test_rounding_is_half_even_like_java_rint():
    """Java 用 Math.rint（五取偶）。0.5 → 0、1.5 → 2。"""
    assert round(0.5) == 0
    assert round(1.5) == 2
    assert round(2.5) == 2


# ─────────────────────────── 夹具 ───────────────────────────

TRACE_COLS = ["seq", "t_ms", "act", "idx", "n", "ptrs", "ret", "cost_us",
              "late_ms", "et_late_ms", "et_back_ms"]


def trow(seq, t, act, idx, n, ptrs, ret=1):
    return [seq, f"{t:.3f}", act, idx, n, ptrs, ret, "2000", "0.200", "0", "0"]


def write_trace(path, rows, newline="\n"):
    with open(path, "w", encoding="utf-8", newline=newline) as f:
        w = csv.writer(f, lineterminator=newline)
        w.writerow(TRACE_COLS)
        w.writerows(rows)


def write_payload(path, events, canvas=CANVAS, screen=SCREEN):
    path.write_text(
        json.dumps(
            {
                "canvas": {"width": canvas[0], "height": canvas[1]},
                "screen": {"width": screen[0], "height": screen[1]},
                "events": events,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


# 两条事件的迷你载荷：一条单指 DOWN，一条双指 POINTER_DOWN
#   期望设备坐标（canvas=1920x1080）= (842,221) / (768,499)
EVENTS = [
    {"t_ms": 3000.0, "action": "DOWN", "pointer_id": 0, "x": 571.447, "y": 1052.64},
    {"t_ms": 3000.0, "action": "POINTER_DOWN", "pointer_id": 1, "x": 508.553, "y": 1287.36},
]
# 正确注入（好载荷该得到的位置）
ROWS_GOOD = [
    trow(1, 3000.0, D, -1, 1, "0:842:221"),
    trow(2, 3000.0, PD, 1, 2, "0:842:221|1:768:499"),
]
# 事故现场：同一份好载荷，但设备上跑的是 canvas=1080x2340 的旧缓存
ROWS_BAD = [
    trow(1, 3000.0, D, -1, 1, "0:815:324"),
    trow(2, 3000.0, PD, 1, 2, "0:815:324|1:795:396"),
]


def _run(tmp_path, events, rows, newline="\n", extra_args=()):
    p = tmp_path / "payload.json"
    t = tmp_path / "trace.csv"
    write_payload(p, events)
    write_trace(t, rows, newline=newline)
    return main(["--payload", str(p), "--trace", str(t), *extra_args])


# ─────────────────────────── 判据 ───────────────────────────

def test_consistent_round_is_clean(tmp_path, capsys):
    """坐标与载荷一致 ⇒ 必须判绿（且不许多嘴说"错位"）。"""
    code = _run(tmp_path, EVENTS, ROWS_GOOD)
    out = capsys.readouterr().out
    assert code == 0
    assert "完全一致" in out
    assert "不一致" not in out


def test_two_pixel_shift_must_be_red(tmp_path, capsys):
    """变异：把实际注入的位置挪 2 px ⇒ 必须红。这是本次事故的真实形态。"""
    rows = [
        trow(1, 3000.0, D, -1, 1, "0:844:221"),
        trow(2, 3000.0, PD, 1, 2, "0:844:221|1:770:499"),
    ]
    code = _run(tmp_path, EVENTS, rows)
    out = capsys.readouterr().out
    assert code == 1
    assert "超差 2/2" in out


def test_canvas_equals_screen_is_diagnosed(tmp_path, capsys):
    """本次事故的完整形态：好载荷 + 按错 canvas 跑出来的 trace。

    两条断言缺一不可：
      ① 判红（不能放过）；
      ② 诊断里点名「screen」并且是 100% 复现 —— 否则人还得自己猜是哪一步错了。
    """
    p = tmp_path / "good.json"
    write_payload(p, EVENTS)          # canvas=1920x1080（正确）
    t = tmp_path / "trace.csv"
    write_trace(t, ROWS_BAD)          # 815:324 / 795:396 = 按 canvas=screen 算出来的
    code = main(["--payload", str(p), "--trace", str(t)])
    out = capsys.readouterr().out
    assert code == 1
    assert "100% 复现" in out
    # ★ 断言必须点名「哪一套口径」，而不是只查 "screen" 这个子串 ——
    #   输出头部本来就有一行 "screen 1080x2340"，那样的断言是空转（变异 M4 实测逃逸）。
    assert "canvas 被写成 screen" in out


def test_linear_fit_recovers_scale():
    """诊断的数学底座：给一组恰好是 0.3077 倍的点，必须把斜率还原出来。"""
    pts = [(100.0, 30.77), (200.0, 61.54), (300.0, 92.31)]
    a, b = linear_fit(pts)
    assert abs(b - 0.3077) < 1e-6
    assert abs(a) < 1e-6


def test_linear_fit_degenerate_is_nan():
    a, b = linear_fit([(5.0, 1.0), (5.0, 2.0)])
    assert a != a and b != b  # nan


def test_linear_fit_too_few_points_is_nan():
    a, b = linear_fit([(1.0, 2.0)])
    assert a != a and b != b


# ─────────────────────────── 解析与边界 ───────────────────────────

def test_crlf_trace_is_accepted(tmp_path, capsys):
    """git 在 Windows 会转换行尾；CRLF 读崩等于这个闸门在 git 之后失效。"""
    code = _run(tmp_path, EVENTS, ROWS_GOOD, newline="\r\n")
    assert code == 0


def test_pointer_index_selects_the_right_pointer(tmp_path, capsys):
    """POINTER_DOWN 的 idx 是**数组下标**：idx=1 必须取 ptrs[1]。

    如果实现写成「取第 0 个」，两条事件的 pid 会撞成 0 ⇒ 匹配直接错位，
    而输出会安静地变成「2 帧超差」，看起来像真机错位。
    """
    exp = expected_map(EVENTS, CANVAS, SCREEN, DST)
    assert Key(3000.0, "POINTER_DOWN", 1) in exp
    bad, unmatched, _ = compare([dict(zip(TRACE_COLS, r)) for r in ROWS_GOOD], exp)
    assert not bad
    assert not unmatched


def test_pointer_index_out_of_range_is_loud(tmp_path):
    """idx 越界不许静默回退到第 0 个指针。"""
    p = tmp_path / "payload.json"
    write_payload(p, EVENTS)
    t = tmp_path / "trace.csv"
    write_trace(t, [trow(1, 3000.0, PD, 3, 2, "0:815:324|1:795:396")])
    with pytest.raises(ValueError, match="越界"):
        main(["--payload", str(p), "--trace", str(t)])


def test_duplicate_key_in_payload_is_loud(tmp_path):
    """载荷里同一 (t, act, pid) 出现两次 ⇒ 归属不可能唯一，必须报错而不是后者覆盖前者。"""
    p = tmp_path / "payload.json"
    write_payload(p, [EVENTS[0], dict(EVENTS[0])])
    t = tmp_path / "trace.csv"
    write_trace(t, [trow(1, 3000.0, D, -1, 1, "0:815:324")])
    with pytest.raises(ValueError, match="重复"):
        main(["--payload", str(p), "--trace", str(t)])


def test_trace_frame_missing_from_payload_is_reported(tmp_path, capsys):
    """trace 多出一帧载荷里没有的关键帧 ⇒ 必须显式报出，不许当"对上了"。"""
    rows = ROWS_GOOD + [trow(3, 9999.0, D, -1, 1, "0:600:300")]
    code = _run(tmp_path, EVENTS, rows)
    out = capsys.readouterr().out
    assert code == 1
    assert "找不到" in out


def test_move_frames_are_ignored(tmp_path, capsys):
    """MOVE 不是关键帧，且端内按注入时钟重采样 ⇒ 不参与判定，也不许算成"未匹配"。"""
    rows = [
        trow(1, 3000.0, D, -1, 1, "0:842:221"),
        trow(2, 3167.2, M, -1, 2, "0:842:221|1:768:499"),
        trow(3, 3000.0, PD, 1, 2, "0:842:221|1:768:499"),
    ]
    code = _run(tmp_path, EVENTS, rows)
    assert code == 0


def test_json_output_is_written(tmp_path):
    p = tmp_path / "payload.json"
    write_payload(p, EVENTS)
    t = tmp_path / "trace.csv"
    write_trace(t, [trow(1, 3000.0, D, -1, 1, "0:844:221")])
    j = tmp_path / "res.json"
    main(["--payload", str(p), "--trace", str(t), "--json", str(j)])
    data = json.loads(j.read_text(encoding="utf-8"))
    assert data["n_mismatch"] == 1
    assert data["ok"] is False
    assert data["worst"][0]["dist"] == pytest.approx(2.0)


def test_tolerance_zero_still_passes_on_exact(tmp_path):
    """容差给 0 也不许把"恰好相等"判红 —— 正常情况误差应当就是 0。"""
    p = tmp_path / "payload.json"
    write_payload(p, EVENTS)
    t = tmp_path / "trace.csv"
    write_trace(t, [
        trow(1, 3000.0, D, -1, 1, "0:842:221"),
        trow(2, 3000.0, PD, 1, 2, "0:842:221|1:768:499"),
    ])
    assert main(["--payload", str(p), "--trace", str(t), "--tol-px", "0"]) == 0


def test_tolerance_is_actually_wired(tmp_path):
    """变异防护：`--tol-px` 若只是被打印、没进判定，这条必须红。

    2 px 的固定偏移 ⇒ tol=1 该红、tol=3 该绿。两向都断言，才证明容差真的参与了比较。
    """
    p = tmp_path / "payload.json"
    write_payload(p, EVENTS)
    t = tmp_path / "trace.csv"
    write_trace(t, [trow(1, 3000.0, D, -1, 1, "0:844:221")])
    assert main(["--payload", str(p), "--trace", str(t), "--tol-px", "1"]) == 1
    assert main(["--payload", str(p), "--trace", str(t), "--tol-px", "3"]) == 0
