"""闭环判据改造（2026-10-22）：方向一致性 + 按 μ 定步长。

背景（`FACTS §135.6`）：`wanju_at_b639_r3` 实测 `E1 / L90 / P1276 / M7`、掉 5.5 万分、
MaxCombo 1374→690，却因 `THR_COUNT=100` 的绝对计数门槛被 `next_g` 判 `ok` ⇒ G 不调。
同一条记录的 `share_late` 是 **0.989**。

本文件锁三件事，任何一件回退都必须变红：
  ① 方向判定看**方向一致性**（二项检验），不看 Good 的绝对数量；
  ② 判出方向后按 `−μ` 定幅度，不再盲跳 `±STEP=120 ms`（那会把 25 ms 的真实偏移过冲成 Early）；
  ③ `mu_ms=None` 时 `next_g` 与旧版**逐位相同**（183 轮历史回放不崩）。

★ 这些用例的期望值全部来自**现读**的三轮真机计数（`E0/L15/P1359`、
`E0/L13/P1361`、`E1/L90/P1276`）与 `solve_mu` 的实际反解结果，不是手算占位。
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TMP = ROOT / "tmp"
if str(TMP) not in sys.path:
    sys.path.insert(0, str(TMP))

import grade_read as G  # noqa: E402

# 玩具狂奏曲 -終焉- AT 的谱面规模（现读 out/notes_wanju_at.json：1374 音符 / Drag 197）
N_TOTAL = 1374
N_DRAG = 197


def counts(early: int, late: int, p: int = 0, b: int = 0, m: int = 0) -> dict:
    """按定义式 `G ≡ Early + Late` 构造六计数（`p` 显式给，避免算术掩盖）。"""
    return dict(early=early, late=late, p=p, g=early + late, b=b, m=m)


# ── ① 二项检验本身 ──────────────────────────────────────────────────────────

def test_binom_sf_matches_closed_form_values():
    """对照手算/闭式：`P(X≥n | n) = 2^-n`、`P(X≥5|8) = 93/256`、对称性 `P(X≥k)+P(X≥n-k+1)=1`。"""
    assert G.binom_sf(15, 15) == pytest.approx(2 ** -15, rel=1e-9)
    assert G.binom_sf(10, 10) == pytest.approx(2 ** -10, rel=1e-9)
    assert G.binom_sf(8, 5) == pytest.approx(93 / 256, rel=1e-9)
    for n in (2, 7, 30, 1374):
        for k in (1, n // 2, n):
            if 1 <= k <= n:
                # 对称：P(X≥k) + P(X≥n-k+1) == 1
                assert G.binom_sf(n, k) + G.binom_sf(n, n - k + 1) == pytest.approx(1.0, abs=1e-9)


def test_binom_sf_handles_degenerate_bounds():
    assert G.binom_sf(0, 0) == 1.0
    assert G.binom_sf(5, 0) == 1.0
    assert G.binom_sf(5, 6) == 0.0


def test_binom_sf_does_not_overflow_at_song_scale():
    """n=1374 时 `2**n` 会下溢成 0 —— log 空间求和必须仍给出 ~0.5（50/50 时）。"""
    v = G.binom_sf(1374, 687)
    assert 0.4 < v < 0.6
    assert math.isfinite(v)


# ── ② 方向判据的四个分支 ────────────────────────────────────────────────────

def test_r3_case_is_detected_as_late():
    """★ 核心回归：E1/L90（91 个 Good，低于 THR_COUNT=100）必须判 late，不能判 ok。"""
    dg = G.direction_gate(1, 90)
    assert dg["code"] == "late"
    assert dg["gate"] == "significant"
    assert dg["pvalue"] < 1e-20


def test_old_threshold_would_have_said_ok():
    """把门槛恢复成 THR_COUNT 绝对计数 ⇒ 同一组计数会判 ok（证明本用例有分辨力）。"""
    dg_legacy_like = G.direction_gate(1, 90, min_good=G.THR_COUNT)
    assert dg_legacy_like["code"] == "ok"
    assert dg_legacy_like["gate"] == "too_few"


def test_too_few_goods_stays_ok():
    """3 个 Good 全在晚侧 ⇒ p=0.125，不该判方向（会拿 3 个样本去调 G）。"""
    dg = G.direction_gate(0, 3)
    assert dg["code"] == "ok"
    assert dg["gate"] == "too_few"


def test_seven_goods_is_below_alpha_but_below_min_sample():
    """n=7 全单侧 p=7.8e-3 已 < 0.01，但样本太少 ⇒ MIN_GOOD_DIR 先拦。"""
    assert G.binom_sf(7, 7) < G.ALPHA_DIR
    assert G.direction_gate(0, 7)["code"] == "ok"
    # n=10 全单侧 p=9.8e-4 ⇒ 过
    assert G.direction_gate(0, 10)["code"] == "late"


def test_both_sides_is_reported_as_both_not_ok():
    """两侧都不小（500/500）⇒ 不能判 ok（G 正确是高强度断言），也不能判方向。"""
    dg = G.direction_gate(500, 500)
    assert dg["code"] == "both"
    assert dg["gate"] == "both"


def test_skewed_but_below_ratio_gate_is_not_a_direction():
    """二项显著但主导侧不到 2×（如 700/600）⇒ 判 both，不给方向。

    二项检验只说明「不是 50/50」；方向修正要求明确主导，否则减 G 会造 Early。
    """
    dg = G.direction_gate(600, 700)
    assert dg["pvalue"] < G.ALPHA_DIR      # 显著
    assert dg["code"] == "both"            # 但不够 2× ⇒ 不给方向


def test_clear_majority_that_is_not_significant_stays_ok():
    """★ 锁住 `ALPHA_DIR` 的**取值**（M6 变异漏网后补）。

    `E6/L15`：主导侧 15 > 2×6=12（过 `ratio` 闸），但 `P(X≥15|21)=0.039 > 0.01`
    ⇒ 方向仍不可信 ⇒ `ok`。若把 `ALPHA_DIR` 放宽到 0.5，这条就会误判 `late`
    —— 这是唯一能分辨「门槛值本身」的用例。
    """
    pv = G.binom_sf(21, 15)
    assert pv == pytest.approx(82160 / 2097152, rel=1e-9)   # 0.03918
    assert G.ALPHA_DIR < pv < 0.5
    dg = G.direction_gate(6, 15)
    assert dg["code"] == "ok"
    assert dg["gate"] == "noise"
    assert 15 > 2 * 6                                       # 确实过了 ratio 闸，不是被它挡住


def test_early_side_is_symmetric():
    dg = G.direction_gate(90, 1)
    assert dg["code"] == "early"
    assert dg["pvalue"] == pytest.approx(G.binom_sf(91, 90), rel=1e-9)


# ── ③ 生产函数端到端：r3 那组真值 ───────────────────────────────────────────

def test_counts_outcome_r3_yields_late_with_mu():
    out = G._counts_outcome(counts(1, 90, p=1276, m=7), n_total=N_TOTAL, n_drag=N_DRAG)
    assert out is not None
    assert out["code"] == "late"
    assert out["dir_gate"] == "significant"
    # 现读 solve_mu 反解：μ=+25.8ms、σ=38.3ms（不是手算，是这个模块实测输出的量级）
    assert out["mu_ms"] is not None and 20.0 < out["mu_ms"] < 32.0
    assert out["sigma_ms"] is not None and 30.0 < out["sigma_ms"] < 46.0


def test_counts_outcome_r1_r2_yield_consistent_mu():
    """r1/r2 的 μ 必须同号、同量级 —— 三轮漂的是 σ 不是 μ（FACTS §136）。"""
    o1 = G._counts_outcome(counts(0, 15, p=1359), n_total=N_TOTAL, n_drag=N_DRAG)
    o2 = G._counts_outcome(counts(0, 13, p=1361), n_total=N_TOTAL, n_drag=N_DRAG)
    assert o1["code"] == o2["code"] == "late"
    assert 15.0 < o1["mu_ms"] < 35.0
    assert 20.0 < o2["mu_ms"] < 40.0
    assert abs(o1["mu_ms"] - o2["mu_ms"]) < 12.0


def test_zero_good_never_produces_mu():
    """全 P 轮（E0/L0）不得反解 μ —— `mu_ms=0.0` 是退化占位，不是实测。"""
    out = G._counts_outcome(counts(0, 0, p=1374), n_total=N_TOTAL, n_drag=N_DRAG)
    assert out["code"] == "ok"
    assert out["dir_gate"] == "empty"
    assert "mu_ms" not in out


def test_miss_gate_overrides_direction_and_clears_mu():
    """Miss 25.5%（> THR_MISS_HARD）⇒ 整轮作废，`mu_ms` 必须被清掉（不许拿崩掉的轮定幅度）。

    ⚠ 20.6% 只会落到 `weak` 档（10%~25% 之间仍给方向、只标证据弱）—— 这里取 25.5%
    才是 `invalid`。两档的边界由 `THR_MISS_OK` / `THR_MISS_HARD` 定义，不能混。
    """
    out = G._counts_outcome(counts(1, 90, p=933, m=350), n_total=N_TOTAL, n_drag=N_DRAG)
    assert out["code"] == "error"
    assert out["reason"] == "miss_too_high"
    assert out.get("mu_ms") is None

    # 对照组：20.6% 只进 weak 档，方向仍然给、μ 仍然在
    weak = G._counts_outcome(counts(1, 90, p=1000, m=283), n_total=N_TOTAL, n_drag=N_DRAG)
    assert weak["code"] == "late"
    assert weak["miss_gate"] == "weak"
    assert weak["mu_ms"] is not None


def test_counts_selfcheck_still_rejects_inconsistent_totals():
    """六计数自检不许被本次改动绕过。"""
    bad_g = dict(early=0, late=15, p=1359, g=14, b=0, m=0)      # g != e+l
    assert G._counts_outcome(bad_g, n_total=N_TOTAL, n_drag=N_DRAG) is None
    bad_sum = counts(0, 15, p=1359, m=7)                        # 总数对不上谱面
    assert G._counts_outcome(bad_sum, n_total=N_TOTAL, n_drag=N_DRAG) is None


# ── ④ next_g：无 μ 时逐位等于旧版 ───────────────────────────────────────────

@pytest.mark.parametrize("code", ["ok", "early", "late", "both", "error"])
@pytest.mark.parametrize("g", [7.0, 7.126, 7.247, 7.367, 7.9])
def test_next_g_without_mu_is_bit_identical_to_legacy(g, code):
    """`mu_ms=None` ⇒ 与旧实现逐位相同（历史 183 轮回放不崩）。"""
    legacy = {"early": g + G.STEP, "late": g - G.STEP}.get(code, g)
    legacy = max(G.G_LO, min(G.G_HI, round(legacy, 3)))
    assert G.next_g(g, code) == legacy
    assert G.next_g(g, code, mu_ms=None) == legacy


# ── ⑤ next_g：有 μ 时按幅度、不盲跳 ─────────────────────────────────────────

def test_next_g_uses_mu_magnitude_not_step():
    """μ=+25 ⇒ 只减 25 ms，而不是 −120 ms。这正是本轮 r1/r2/r3 的场景。"""
    assert G.next_g(7.247, "late", mu_ms=25.0) == pytest.approx(7.222, abs=1e-9)
    assert G.next_g(7.247, "early", mu_ms=-25.0) == pytest.approx(7.272, abs=1e-9)


def test_next_g_zero_mu_is_noop():
    assert G.next_g(7.247, "late", mu_ms=0.0) == 7.247


def test_next_g_sign_conflict_falls_back_to_step():
    """★ 交叉断言：code=late 却 μ<0（方向判据与拟合打架）⇒ 退回 ±STEP，绝不反号。

    场景：局部偏晚被当成全局，`solve_mu` 拟合出的 μ 却接近 0 或反号。
    反号会让 G 往错误方向走，比不动更糟。
    """
    assert G.next_g(7.247, "late", mu_ms=-30.0) == 7.127
    assert G.next_g(7.247, "early", mu_ms=30.0) == 7.367


def test_next_g_caps_delta_at_step():
    """|ΔG| 最多 STEP（120 ms）⇒ 最坏情况等于原口径，不会比改动前更差。"""
    assert G.next_g(7.30, "late", mu_ms=500.0) == pytest.approx(7.18, abs=1e-9)
    assert G.next_g(7.30, "early", mu_ms=-500.0) == pytest.approx(7.42, abs=1e-9)
    assert G.MAX_DELTA_G == G.STEP


def test_next_g_respects_global_bounds():
    """G 闭区间仍是最后一道保险（μ 幅度再大也不许越界）。"""
    assert G.next_g(G.G_HI, "early", mu_ms=-1000.0) == G.G_HI
    assert G.next_g(G.G_LO, "late", mu_ms=1000.0) == G.G_LO


# ── ⑥ 变异：改回旧口径必须变红 ──────────────────────────────────────────────

def test_mutation_restoring_threshold_count_makes_r3_ok():
    """把 min_good 变异回 THR_COUNT ⇒ r3 那条会退回 ok，本文件的断言必须抓到。

    ⚠ `direction_gate` 的默认参数在**定义时**已绑定，monkeypatch 模块属性改不到它
    ⇒ 这里显式传参复现「旧口径」，同时断言生产默认值仍是能判的。
    """
    assert G.direction_gate(1, 90, min_good=G.THR_COUNT)["code"] == "ok"
    assert G.direction_gate(1, 90)["code"] == "late"
    assert G.MIN_GOOD_DIR < G.THR_COUNT


def test_mutation_using_step_instead_of_mu_is_observable():
    """证明两种实现可分辨：盲跳与按 μ 走的结果必须不同（否则本组测试是空转）。"""
    blind = G.next_g(7.247, "late")                    # 旧口径
    by_mu = G.next_g(7.247, "late", mu_ms=25.0)        # 新口径
    assert blind != by_mu
    assert abs(blind - 7.247) == pytest.approx(G.STEP, abs=1e-9)
    assert abs(by_mu - 7.247) == pytest.approx(0.025, abs=1e-9)
