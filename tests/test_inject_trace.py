"""`tools/analyze_inject_trace.py` 的判据自测。

为什么值得单独测
----------------
这个脚本是「多指帧为什么 Miss」的**唯一裁判**。它自己错了不会报错，只会给出
一个看起来很有道理的错误结论，然后指挥下一轮真机去修错的地方 —— 代价是一整轮。
所以下面每条用例都对应一种**会让结论反转**的具体错法：

  · 把缺值 `-` 当成 0        ⇒ "EventTime 零落后"是假象，H2 永远否定
  · 列数不校验               ⇒ locale 破列（`1,235`）时静默错位读列
  · 单指/多指分组口径错      ⇒ 对照表整个反过来
  · 结构判据只看 idx<n       ⇒ 漏掉"新指针插在中间"这种非法帧

⛔ 阈值本身是**自定口径**（不是任何规范值），所以这里只断言「同一份数据下
   判定方向必须对」，不断言阈值的具体大小 —— 阈值改了不该让测试红。
"""

from __future__ import annotations

import json

import pytest

import analyze_inject_trace as ait

HEADER = ",".join(ait.COLS)


def row(seq, act, idx, n, ret=1, cost_us=2000, late=0.0, etl=0, back="-"):
    """造一行；默认 back='-' 表示「无上一帧」，由调用方在需要的帧上给真值。"""
    ptrs = "|".join(f"{i}:{100 + i}:{200 + i}" for i in range(n))
    return f"{seq},{seq * 5:.3f},{act},{idx},{n},{ptrs},{ret},{cost_us},{late:.3f},{etl},{back}"


def write(tmp_path, rows, note=None, name="t.csv"):
    p = tmp_path / name
    body = [HEADER] + list(rows)
    if note:
        body.append(note)
    p.write_text("\n".join(body) + "\n", encoding="utf-8")
    return p


# ─────────────────────────── 缺值不许变 0 ───────────────────────────

def test_dash_is_missing_not_zero(tmp_path):
    """`-` 必须记成缺值：既不计入 `back_gt0`，也不许拉低 et_late 的分位数。"""
    rows = [
        row(1, 0, -1, 1, etl=100, back="-"),
        row(2, 2, -1, 1, etl=100, back="-"),
        row(3, 2, -1, 1, etl=100, back="-"),
    ]
    data, notes, seen = ait.load(write(tmp_path, rows))
    assert seen, "表头必须被识别"
    s = ait.summarize(data)
    assert s["etl_missing"] == 0, "etl 有值，不该报缺"
    assert s["back_missing"] == 3, f"3 帧都没有上一帧 ⇒ 缺 3，实得 {s['back_missing']}"
    assert s["back_gt0"] == 0, "缺值绝不能被当成倒退"
    # ★ 核心：若把 `-` 读成 0，p95 会变成 0；真值必须是 100
    assert s["etl_p95"] == 100, f"etl p95 必须是 100（不许被 0 拉低），实得 {s['etl_p95']}"


def test_dash_in_etlate_counts_as_missing(tmp_path):
    rows = [row(1, 0, -1, 1, etl="-"), row(2, 0, -1, 1, etl=50)]
    data, _, _ = ait.load(write(tmp_path, rows))
    s = ait.summarize(data)
    assert s["etl_missing"] == 1
    assert s["etl_p50"] == 50, "唯一有值的帧必须代表整组（缺值不许当 0 参与）"


# ─────────────────────────── 列契约 ───────────────────────────

def test_wrong_column_count_raises(tmp_path):
    """locale 破列（`1,235`）会让列数变多 —— 必须报错，不许静默错位读列。"""
    p = tmp_path / "bad.csv"
    p.write_text(HEADER + "\n1,0.000,0,-1,1,0:1:2,1,2000,0.500,1,-,EXTRA\n",
                 encoding="utf-8")
    with pytest.raises(ait.TraceError) as e:
        ait.load(p)
    assert "列数" in str(e.value)


def test_header_only_raises(tmp_path):
    p = tmp_path / "empty.csv"
    p.write_text(HEADER + "\n", encoding="utf-8")
    with pytest.raises(ait.TraceError):
        ait.load(p)


def test_missing_file_raises(tmp_path):
    with pytest.raises(ait.TraceError):
        ait.load(tmp_path / "nope.csv")


def test_note_line_is_not_data(tmp_path):
    rows = [row(1, 0, -1, 1)]
    data, notes, _ = ait.load(write(tmp_path, rows, note="# truncated 5 lines (cap 20000)"))
    assert len(data) == 1, "注释行不许被当成数据"
    assert notes and "truncated 5" in notes[0], "截断说明必须被读到（否则会当成'没发事件'）"


# ─────────────────────────── 分组口径 ───────────────────────────

def test_single_vs_multi_split_is_by_pointer_count(tmp_path):
    """单/多指必须按**该帧携带的指针数**分，而不是按 action 类型。"""
    rows = [
        row(1, 0, -1, 1),                   # DOWN，1 指 ⇒ 单指
        row(2, 5, 1, 2),                    # POINTER_DOWN，2 指 ⇒ 多指
        row(3, 2, -1, 2),                   # MOVE，2 指 ⇒ 多指
        row(4, 6, 1, 2),                    # POINTER_UP，2 指 ⇒ 多指（抬手那帧指针仍在）
        row(5, 1, -1, 1),                   # UP，1 指 ⇒ 单指
    ]
    data, _, _ = ait.load(write(tmp_path, rows))
    s = {"single": ait.summarize([r for r in data if r["n"] == 1]),
         "multi": ait.summarize([r for r in data if r["n"] >= 2])}
    assert s["single"]["n"] == 2
    assert s["multi"]["n"] == 3, f"POINTER_UP 那帧也算多指，实得 {s['multi']['n']}"


# ─────────────────────────── 四个假设的判定方向 ───────────────────────────

def _verdicts(tmp_path, rows):
    import contextlib
    import io
    data, notes, _ = ait.load(write(tmp_path, rows))
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        out = ait.report(data, notes, "t.csv")
    return out["verdicts"], buf.getvalue()


def test_h1_fires_when_multi_frames_are_rejected(tmp_path):
    """多指 10 帧里 3 帧 false、单指 0 ⇒ H1 必须为真。"""
    rows = [row(i, 0, -1, 1, ret=1) for i in range(1, 11)]
    rows += [row(100 + i, 2, -1, 2, ret=0 if i < 3 else 1) for i in range(10)]
    v, _ = _verdicts(tmp_path, rows)
    assert v["H1"] is True, v


def test_h1_quiet_when_false_evenly_spread(tmp_path):
    """单指也照样 false ⇒ 不是"拒绝多指"，H1 必须为假（防把通道坏掉误判成多指问题）。"""
    rows = [row(i, 0, -1, 1, ret=0 if i <= 3 else 1) for i in range(1, 11)]
    rows += [row(100 + i, 2, -1, 2, ret=0 if i < 3 else 1) for i in range(10)]
    v, _ = _verdicts(tmp_path, rows)
    assert v["H1"] is False, v


def test_h3_fires_on_backwards_event_time(tmp_path):
    rows = [row(1, 0, -1, 1, back="-"), row(2, 2, -1, 1, back=7),
            row(3, 2, -1, 2, back=-5)]
    v, txt = _verdicts(tmp_path, rows)
    assert v["H3"] is True, v
    assert "倒退" in txt


def test_h2_fires_on_stale_multi_frames(tmp_path):
    """多指 et_late 全 50 ms、单指 0 ⇒ H2 真。"""
    rows = [row(i, 0, -1, 1, etl=0) for i in range(1, 21)]
    rows += [row(100 + i, 2, -1, 2, etl=50) for i in range(20)]
    v, _ = _verdicts(tmp_path, rows)
    assert v["H2"] is True, v


def _mixed_legal_stream(reps=6, **kw):
    """交替「单指点按」与「双指和弦」的**合法**指针流。

    ⛔ 原来这个用例用的是「MOVE 指针数 1/2 交替」——结构上根本不是一次触摸，
       H5 加进来后它立刻变红。夹具本身不合法，就不能拿来测「全否」。
    """
    rows, seq = [], 0
    kw = {**kw}
    kw.setdefault("back", -5)
    for _ in range(reps):
        seq += 1; rows.append(row(seq, 0, -1, 1, **{**kw, "back": "-"}))
        seq += 1; rows.append(row(seq, 1, -1, 1, **kw))
        seq += 1; rows.append(row(seq, 0, -1, 1, **kw))
        seq += 1; rows.append(row(seq, 5, 1, 2, **kw))
        seq += 1; rows.append(row(seq, 2, -1, 2, **kw))
        seq += 1; rows.append(row(seq, 6, 1, 2, **kw))
        seq += 1; rows.append(row(seq, 1, -1, 1, **kw))
    return rows


def test_all_false_points_to_serial_single_finger(tmp_path):
    """五假设全否时，结论必须指向「单指串行 A/B」，而不是让人去调 G/L。"""
    rows = _mixed_legal_stream(etl=1, back=-5, late=0.3)
    v, txt = _verdicts(tmp_path, rows)
    assert all(x is False for x in v.values() if x is not None), v
    assert "单指串行" in txt


# ─────────────────────────── 结构非法帧 ───────────────────────────

def test_pointer_down_must_append_to_tail(tmp_path):
    """Gesture 总是 append 到末尾 ⇒ POINTER_DOWN 的 actionIndex 必须是 n-1。"""
    data, _, _ = ait.load(write(tmp_path, [
        row(1, 0, -1, 1),
        row(2, 5, 0, 2),      # ★ 非法：新指针插在中间（应为 idx=1）
        row(3, 5, 1, 2),      # 合法
    ]))
    bad = ait.structure_errors(data)
    assert bad["pd_idx_not_last"] == [2], bad


def test_out_of_range_action_index_is_flagged(tmp_path):
    data, _, _ = ait.load(write(tmp_path, [
        row(1, 0, -1, 1),
        row(2, 5, 5, 2),      # idx=5 但只有 2 个指针
        row(3, 6, 9, 2),
    ]))
    bad = ait.structure_errors(data)
    assert bad["idx_ge_n"] == [2, 3], bad


# ─────────────────────────── H5 跨帧指针流 ───────────────────────────

def _legal_two_finger():
    """一段合法的双指手势：DOWN → POINTER_DOWN → MOVE → POINTER_UP → UP。

    ★ 注意 POINTER_UP 那帧的 n 是 **2**（抬起的指还在事件里），这是 AOSP 语义，
      也是我第一版判据写错、把 1966/2775 帧全冤枉掉的地方。
    """
    return [
        row(1, 0, -1, 1),        # DOWN  ptr {0}
        row(2, 5, 1, 2),         # PD    ptr {0,1}，新增在 idx=1
        row(3, 2, -1, 2),        # MOVE  ptr {0,1}
        row(4, 6, 1, 2),         # PU    ptr {0,1}，抬 idx=1 ⇒ 剩 {0}
        row(5, 1, -1, 1),        # UP    ptr {0}
    ]


def test_legal_stream_has_no_violation(tmp_path):
    data, _, _ = ait.load(write(tmp_path, _legal_two_finger()))
    bad = ait.pointer_stream_errors(data)
    assert sum(len(v) for v in bad.values()) == 0, bad


def test_pointer_up_keeps_the_lifted_pointer_in_the_event(tmp_path):
    """POINTER_UP 的 n 必须等于**抬起前**的指针数；写成 n-1 就是判据错。"""
    data, _, _ = ait.load(write(tmp_path, [
        row(1, 0, -1, 1),
        row(2, 5, 1, 2),
        row(3, 6, 1, 1),      # ★ 非法：抬指那帧把抬起的指从事件里删了
    ]))
    bad = ait.pointer_stream_errors(data)
    assert bad["pu_n"] == [3], bad
    assert bad["pu_set"] == [3], bad


def test_move_must_not_change_pointer_set(tmp_path):
    """MOVE 悄悄换了指针集 ⇒ 后面的 UP 会抬错指，游戏侧整串失效。"""
    data, _, _ = ait.load(write(tmp_path, [
        row(1, 0, -1, 1),
        row(2, 5, 1, 2),
        row(3, 2, -1, 1),     # ★ 非法：MOVE 掉了 1 指，却没发 POINTER_UP
    ]))
    bad = ait.pointer_stream_errors(data)
    assert bad["move_n"] == [3] and bad["move_set"] == [3], bad


def test_pointer_up_of_unknown_pointer_is_flagged(tmp_path):
    """抬起一根从没按下过的手指（DOWN 被吞）⇒ 必须报出来。"""
    data, _, _ = ait.load(write(tmp_path, [
        row(1, 0, -1, 1),
        row(2, 1, -1, 1),     # 正常抬起
        row(3, 1, -1, 1),     # ★ 非法：没有 DOWN 就 UP
    ]))
    bad = ait.pointer_stream_errors(data)
    assert bad["up_unknown"] == [3], bad


def test_h5_verdict_fires_on_broken_stream(tmp_path):
    rows = _legal_two_finger() + [
        row(6, 0, -1, 1), row(7, 1, -1, 1),
        row(8, 0, -1, 1), row(9, 2, -1, 3),   # 3 指 MOVE：从没 POINTER_DOWN 过
    ]
    v, txt = _verdicts(tmp_path, rows)
    assert v["H5"] is True, v
    assert "指针流" in txt


def test_remaining_pointer_after_pointer_up_is_still_tracked(tmp_path):
    """抬指后剩下的那根指必须继续被跟踪。

    ⛔ 这条是补变异漏网用的：把 `down = idset - {ids[idx]}` 改成 `pass` 时，
       原有用例全绿（因为 `_legal_two_finger` 抬指后立刻 UP，看不出差别）。
       加上 PU 之后的 MOVE/UP，才会暴露「没收缩 ⇒ 后续帧误判」。
    """
    data, _, _ = ait.load(write(tmp_path, [
        row(1, 0, -1, 1),
        row(2, 5, 1, 2),      # 双指
        row(3, 6, 1, 2),      # 抬起 idx=1 ⇒ 只剩 {0}
        row(4, 2, -1, 1),     # ★ 剩下的 0 号指继续动：1 指 MOVE
        row(5, 1, -1, 1),     # 它抬起
    ]))
    bad = ait.pointer_stream_errors(data)
    assert sum(len(v) for v in bad.values()) == 0, bad


# ─────────────────────────── 按下时长 ───────────────────────────

def test_press_durations_counts_each_down_once(tmp_path):
    """按下次数必须等于 DOWN+POINTER_DOWN 的条数（多了就是把同一根指算两遍）。"""
    rows = _legal_two_finger()
    data, _, _ = ait.load(write(tmp_path, rows))
    holds = ait.press_durations(data)
    n_press = sum(1 for r in data if r["act"] in (0, 5))
    assert len(holds) == n_press == 2, (holds, n_press)


def test_press_duration_uses_planned_time(tmp_path):
    """时长用计划 t_ms（5 ms 一档），不许掺注入抖动。"""
    data, _, _ = ait.load(write(tmp_path, [
        row(1, 0, -1, 1),          # t=5
        row(4, 1, -1, 1),          # t=20 ⇒ 15 ms
    ]))
    assert ait.press_durations(data) == [15.0], ait.press_durations(data)


def test_miss_scale_check_does_not_crash(tmp_path):
    """`--miss` 与内部缺值统计同名过一次 ⇒ 直接 TypeError。这条钉住它。"""
    p = write(tmp_path, _legal_two_finger())
    rc = ait.main([str(p), "--miss", "100"])
    assert rc == 0




# ─────────────────────────── CLI ───────────────────────────

def test_cli_returns_2_on_missing_file(tmp_path, capsys):
    rc = ait.main([str(tmp_path / "absent.csv")])
    assert rc == 2
    assert "找不到" in capsys.readouterr().err


def test_cli_json_is_serializable(tmp_path, capsys):
    p = write(tmp_path, [row(1, 0, -1, 1), row(2, 2, -1, 2, etl=3, back=-5)])
    rc = ait.main([str(p), "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    # 最后一段 JSON 必须是合法 JSON（summary 里混了 None ⇒ default=str 兜住）
    at = out.index('"n_frames"')
    j = json.loads(out[out.rindex("{", 0, at):])
    assert j["n_frames"] == 2 and j["n_multi"] == 1, j
