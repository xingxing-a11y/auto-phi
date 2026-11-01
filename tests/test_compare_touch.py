"""`tools/compare_touch.py` 的测试 —— 对账判据本身必须能抓错。

★ 为什么这些用例值得写：这个分析器的输出会直接决定"下一步改哪里"。
   它若把「正常合并」判成「丢帧」，就会把人赶去重写注入层（已经白干过两次：
   §158 把「多指整帧丢」写成主假设，§159 自己的数据把它否掉）。
   它若把「真丢帧」判成「清白」，就会让人去调 G/L，同样是白跑。

   所以每条用例都对着一个**具体的误判方向**。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.compare_touch import (  # noqa: E402
    KEY_ACTS, RECV_COLS, SEND_COLS, TraceError, _read, align, coord_drift, report,
)

D, U, M, C, PD, PU = 0, 1, 2, 3, 5, 6


# ─────────────────────────── 夹具 ───────────────────────────

def srow(seq, t, act, idx, n, ptrs="0:100:200", ret=1):
    return f"{seq},{t},{act},{idx},{n},{ptrs},{ret},3000,0.5,1,0"


def rrow(seq, recv_ms, et_ms, act, idx, n, ptrs="0:100:200", dly=5):
    return f"{seq},{recv_ms},{et_ms},{act},{idx},{n},{ptrs},{dly}"


def write_send(tmp_path, rows, header=",".join(SEND_COLS), trunc=None):
    p = tmp_path / "send.csv"
    lines = [header] + rows
    if trunc:
        lines.append(f"# truncated {trunc} lines (cap 20000)")
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(p)


def write_recv(tmp_path, rows, header=",".join(RECV_COLS)):
    p = tmp_path / "recv.csv"
    p.write_text("\n".join([header] + rows) + "\n", encoding="utf-8")
    return str(p)


def stream(reps=3, move=2):
    """一段合法的单指+双指触摸流；返回 (send_rows, recv_rows)。"""
    s, r = [], []
    seq = 0
    t = 0
    for _ in range(reps):
        for act, idx, n, ptrs in (
            (D, -1, 1, "0:100:200"),
            (PD, 1, 2, "0:100:200|1:300:400"),
            (PU, 1, 2, "0:100:200|1:300:400"),
            (U, -1, 1, "0:100:200"),
        ):
            seq += 1
            t += 50
            s.append(srow(seq, t, act, idx, n, ptrs))
            r.append(rrow(seq, t + 8, t, act, idx, n, ptrs))
        for _ in range(move):          # MOVE 在两侧都记，收货侧可能被合并
            seq += 1
            t += 8
            s.append(srow(seq, t, M, -1, 1, "0:110:200", ret=1))
            r.append(rrow(seq, t + 8, t, M, -1, 1, "0:110:200"))
    # ★ 夹具自检：MOVE 必须两侧都有，否则「合并」用例是空转
    assert any(line.split(",")[2] == str(M) for line in s), "夹具没造出 MOVE（发送侧）"
    assert any(line.split(",")[3] == str(M) for line in r), "夹具没造出 MOVE（收货侧）"
    return s, r


def run(tmp_path, srows, rrows, notes=None, capsys=None):
    send = _read(write_send(tmp_path, srows), SEND_COLS, "发送侧")
    recv = _read(write_recv(tmp_path, rrows), RECV_COLS, "收货侧")
    out = report(send, recv, notes)
    text = capsys.readouterr().out if capsys else ""
    return out, text


# ─────────────────────────── 用例 ───────────────────────────

def test_all_keys_arrived_is_clean(tmp_path, capsys):
    """全到齐 ⇒ 必须判「派发层清白」，这是本工具存在的意义。"""
    s, r = stream()
    out, _ = run(tmp_path, s, r, capsys=capsys)
    assert out["verdict"] == "arrived", out["verdict"]
    assert out["align"]["matched"] == out["align"]["send_keys"]
    assert out["align"]["missing_n"] == 0


def test_move_merge_is_not_a_loss(tmp_path, capsys):
    """★ 连续 MOVE 被合并是正常行为（10-21 实测 5 发 3 收）。

    把它算成丢帧 ⇒ 会得出「派发层丢了一半」的假结论，是这个工具最容易犯的错。
    """
    s, r = stream(reps=3, move=3)
    # 收货侧把每段 MOVE 只留第一条（模拟 InputDispatcher 合并）
    kept, prev_was_move = [], False
    for line in r:
        act = int(line.split(",")[3])
        if act == M and prev_was_move:
            continue
        kept.append(line)
        prev_was_move = act == M
    out, text = run(tmp_path, s, kept, capsys=capsys)
    assert out["verdict"] == "arrived", out["verdict"]
    assert out["send_hist"]["MOVE"] > out["recv_hist"]["MOVE"], "夹具没造出合并"
    assert "合并" in text


def test_missing_key_frame_points_at_it(tmp_path, capsys):
    """删掉一条 POINTER_DOWN ⇒ 必须判「派发层有罪」并点名是哪一条。"""
    s, r = stream(reps=3)
    dropped = r[1]                       # 第一段的 POINTER_DOWN
    r = [x for x in r if x != dropped]
    out, text = run(tmp_path, s, r, capsys=capsys)
    assert out["verdict"] == "dispatch_lost", out["verdict"]
    assert out["align"]["missing_n"] == 1
    assert out["missing"][0]["sig"][0] == "POINTER_DOWN"
    assert "POINTER_DOWN" in text


def test_missing_scale_vs_notes(tmp_path, capsys):
    """量级对照：缺 1 条不该说成"能解释 780 Miss"。"""
    s, r = stream(reps=3)
    r = [x for x in r if x != r[1]]
    out, text = run(tmp_path, s, r, notes=1286, capsys=capsys)
    assert out["verdict"] == "dispatch_lost"
    assert "不能" in text, "缺 1 条却判成能解释全部 Miss"


def test_cancel_invalidates_round(tmp_path, capsys):
    """CANCEL ⇒ 流被打断，这轮不能当证据（哪怕关键帧一条不少）。"""
    s, r = stream(reps=2)
    r.insert(0, rrow(999, 10, 5, C, -1, 2, "0:100:200|1:300:400"))
    out, text = run(tmp_path, s, r, capsys=capsys)
    assert out["verdict"] == "cancel", out["verdict"]
    assert "CANCEL" in text


def test_extra_keys_means_polluted(tmp_path, capsys):
    """收货侧多出关键帧 ⇒ 上一次残留 / 别的来源在发，不能当证据。"""
    s, r = stream(reps=3)
    r.append(rrow(998, 900, 900, D, -1, 1))
    r.append(rrow(999, 950, 950, U, -1, 1))
    out, _ = run(tmp_path, s, r, capsys=capsys)
    assert out["verdict"] == "polluted", out["verdict"]


def test_same_act_but_wrong_index_is_encoding_not_loss(tmp_path, capsys):
    """act/n 对得上但 idx 不对 ⇒ 是编码问题，不是丢帧。

    区分这两种的意义：丢帧要查派发，编码错要查 Gesture/构建 —— 修法完全不同。
    """
    s, r = stream(reps=3)
    # 把第一段的 POINTER_DOWN 的 idx 从 1 改成 0
    bad = r[1].split(",")
    bad[4] = "0"
    r[1] = ",".join(bad)
    out, _ = run(tmp_path, s, r, capsys=capsys)
    assert out["align"]["sig_only_mismatch"] >= 1, out["align"]
    assert out["verdict"] == "encoding", out["verdict"]


def test_truncated_lines_are_flagged(tmp_path, capsys):
    """明细被截断 ⇒ 对账不完整，必须显式说出来（不能静默按抽样下结论）。"""
    s, r = stream(reps=2)
    send = _read(write_send(tmp_path, s, trunc=42), SEND_COLS, "发送侧")
    recv = _read(write_recv(tmp_path, r), RECV_COLS, "收货侧")
    report(send, recv, None)
    text = capsys.readouterr().out
    assert "截断 42 行" in text
    assert send.truncated == 42


def test_header_mismatch_is_loud(tmp_path):
    """表头不认识就报错，不能"按位置硬读"—— 列序会随版本变。"""
    p = write_send(tmp_path, [srow(1, 0, D, -1, 1)], header="seq,t,act,idx,n,ptrs,ret,c,l,el,eb")
    with pytest.raises(TraceError, match="表头不符"):
        _read(p, SEND_COLS, "发送侧")


def test_wrong_column_count_is_loud(tmp_path):
    p = write_send(tmp_path, ["1,0,0,-1,1,0:100:200,1,3000,0.5"])   # 少 2 列
    with pytest.raises(TraceError, match="列"):
        _read(p, SEND_COLS, "发送侧")


def test_bad_ptrs_format_is_loud(tmp_path):
    """ptrs 里出现逗号会直接把 CSV 拆坏 —— 必须在解析时就报，别一路错下去。"""
    p = write_send(tmp_path, ["1,0,0,-1,1,0:100,200,1,3000,0.5,1,0"])
    with pytest.raises(TraceError, match="列"):
        _read(p, SEND_COLS, "发送侧")


def test_coord_constant_offset_is_benign(tmp_path):
    """恒定平移 = 窗口 inset，不影响判定；随位置变化才是真 bug。"""
    s, r = stream(reps=3)
    shifted = []
    for line in r:
        f = line.split(",")
        # ptrs 是第 7 列（index 6）
        f[6] = "|".join(
            f"{p.split(':')[0]}:{int(p.split(':')[1]) + 3}:{int(p.split(':')[2]) + 5}"
            for p in f[6].split("|"))
        shifted.append(",".join(f))
    send = _read(write_send(tmp_path, s), SEND_COLS, "发送侧")
    recv = _read(write_recv(tmp_path, shifted), RECV_COLS, "收货侧")
    cd = coord_drift(send, recv)
    assert cd["dx"]["mode"] == 3 and cd["dx"]["mode_frac"] == 1.0, cd
    assert cd["dy"]["mode"] == 5 and cd["dy"]["mode_frac"] == 1.0, cd


def test_few_outliers_do_not_mean_scaling(tmp_path):
    """★ 恒定平移 + 极少数离群 ⇒ 仍判「平移」，不是「缩放」。

    这条来自整首真数据（1683 个指针里 5 个离群）：第一版判据用 max−min，
    把这 5 条报成「坐标变换有问题」，而真相是变换完全正确 —— 离群来自
    MOVE 合并后配错帧。判据用众数占比才能区分「系统平移」与「真缩放」。
    """
    s, r = stream(reps=60)
    # 全部偏 -74（模拟窗口 inset），再让**第一条单指 DOWN** 额外偏 66（模拟合并配错）。
    # ★ 离群必须放在单指帧、且总数要够大：真数据是 5/1683=0.30%，
    #   夹具若只有 90 个指针，1 条离群就占 1.1%，测的反而是另一件事。
    tail = []
    for i, line in enumerate(r):
        f = line.split(",")
        extra = 66 if i == 0 else 0
        f[6] = "|".join(
            f"{p.split(':')[0]}:{int(p.split(':')[1]) - 74 - extra}:{int(p.split(':')[2])}"
            for p in f[6].split("|"))
        tail.append(",".join(f))
    send = _read(write_send(tmp_path, s), SEND_COLS, "发送侧")
    recv = _read(write_recv(tmp_path, tail), RECV_COLS, "收货侧")
    cd = coord_drift(send, recv)
    assert cd["dx"]["mode"] == -74, cd
    assert cd["dx"]["mode_frac"] >= 0.995, cd


def test_coord_scaling_is_caught(tmp_path):
    """缩放 ⇒ 每个位置的偏差都不同 ⇒ 没有单一众数。"""
    s, r = stream(reps=15)
    scaled = []
    for line in r:
        f = line.split(",")
        f[6] = "|".join(
            f"{p.split(':')[0]}:{int(p.split(':')[1]) * 2}:{int(p.split(':')[2]) * 2}"
            for p in f[6].split("|"))
        scaled.append(",".join(f))
    send = _read(write_send(tmp_path, s), SEND_COLS, "发送侧")
    recv = _read(write_recv(tmp_path, scaled), RECV_COLS, "收货侧")
    cd = coord_drift(send, recv)
    assert cd["dx"]["mode_frac"] < 0.995, cd
    assert cd["dx"]["distinct"] > 1, cd


def test_alignment_counts_only_key_acts():
    """MOVE/CANCEL 不进关键帧集合 —— 否则 MOVE 一合并就全乱。"""
    assert M not in KEY_ACTS and C not in KEY_ACTS
    assert KEY_ACTS == {D, U, PD, PU}


def test_buckets_detect_worsening(tmp_path, capsys):
    """越打越糟（后半段全丢）必须被分段到达率抓到 —— 那是累积滞后的特征。"""
    s, r = stream(reps=10)
    # 把后半段的接收行删掉一半
    keep = r[: len(r) * 2 // 3]
    out, text = run(tmp_path, s, keep, capsys=capsys)
    assert out["verdict"] == "dispatch_lost"
    bad = [b for b in out["buckets"] if b["rate"] < 0.999]
    assert bad, "后半段丢了却没分段报警"
    assert "不达标" in text


def test_empty_recv_is_dispatch_lost(tmp_path, capsys):
    """收货侧 0 条 ⇒ 派发层完全没送到（这正是 10-24 第 4 枪的症状）。"""
    s, _ = stream(reps=2)
    out, _ = run(tmp_path, s, [], capsys=capsys)
    assert out["verdict"] == "dispatch_lost"
    assert out["align"]["matched"] == 0


def test_crlf_file_is_read_identically(tmp_path):
    """★ git 在 Windows 上会把 CSV 转成 CRLF（提交时实测有警告）。

    如果读端用 split("\\n")，每行末尾会残留 '\\r'，最后一个字段就变成 "0\r" ⇒
    整数解析炸 或者 更糟：ptrs 解析出 ("0","100","200\r") 静默取错值。
    _read 用 splitlines()，这里钉住它真的没问题。
    """
    s, _ = stream(reps=2)
    p = tmp_path / "send_crlf.csv"
    body = "\n".join([",".join(SEND_COLS)] + s) + "\n"
    p.write_bytes(body.replace("\n", "\r\n").encode("utf-8"))
    t = _read(str(p), SEND_COLS, "发送侧")
    assert t.frames == len(s), t.frames
    assert t.rows[0]["ptrs"] == [(0, 100, 200)], t.rows[0]["ptrs"]
    assert isinstance(t.rows[0]["act"], int)
