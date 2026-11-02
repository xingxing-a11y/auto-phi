"""纯 G 轮也能产出 D 样本（`§146`）—— 离线测试，不连手机。

背景（2026-10-23 晚，Ⅱ-b 转产线的前置）：
    `full_run.py` 的 μ 求解块原先被 `if ACOUSTIC and isinstance(res, dict):` 挡住，
    而 `ACOUSTIC = L4C or PAS`、纯 G 轮（`manual aud`）两者皆假 ⇒ **纯 G 轮连
    `solve_e` 都没执行过**。不是「μ 退化」，是**从未调用**。
    后果：Ⅱ-b 全部是纯 G 轮 ⇒ 设备 D 池长期停在 6 条、不再进化。

    放开门的**同时**必须保证一条不变量：

        **纯 G 轮只许写 D 池，绝不许动 e。**

    因为纯 G 轮的 `zero = t0 + G`，公式里**没有 e** —— 一旦 e 被「收敛」，下一轮
    l4c 会直接偏 20~40 ms，而日志只显示「e 1.08 → 1.12」这种看起来正常的步。

本文件分五层，每层都能**单独否决**：

    [1] `solve_e` 的两种实现**可区分**：同一组计数，`used_edge` 取 False/True
        必须分别落到 `not_edge`（无 e 字段）/ `ok`（有 e 字段）。
        ⚠ 若测试只断言「mu 有值」，把 used_edge 硬写成 True 也照样绿 ⇒ 空转。
    [2] `guard_pure_g_edit`（本地防御闸）：拦下 `code=="ok"`、剥字段、不改安全输入。
        —— `_seal` 是跨模块隐式契约，这道闸防它将来被改而无人知。
    [3] `plan_d_pool_write`（入池判据）：精确算式 / 符号 / 退化与缺 μ 的拒绝。
        —— 抽成纯函数就是为了让这条**能被执行**，而不是靠 grep 源码。
    [4] `record_d_eff` 的 `src` 标记：给了才写，`None` 时**键集合与旧版逐位相同**。
    [5] **接线**：用 AST 解析 `full_run.py`，断言 μ 求解块的 `If` 条件里不含
        `ACOUSTIC`、而 `nudge_e` 的 `If` 条件里**含** `ACOUSTIC`（P1 致命漏洞）。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TMP = ROOT / "tmp"
if str(TMP) not in sys.path:
    sys.path.insert(0, str(TMP))

AR = pytest.importorskip("audio_round")  # tmp/ 下的跑分工具，公开仓不导出 tmp ⇒ 跳过
GR = pytest.importorskip("grade_read")

# hckwaya_r2 的真实结算计数（0998147 / 99.79%，340 音符、0 Drag）。
H_KCWAYA_R2 = {"early": 0, "late": 2, "p": 338, "g": 2, "b": 0, "m": 0}
N_TOTAL, N_DRAG = 340, 0
D_USED_HKCWAYA = 7.2        # = history 里的 g_used = zero − t0（纯 G：d_fb = CLI G）


def _solve(used_edge: bool, counts=None, **kw):
    """注意：第二个位置参数是 **e_used**（新机实测 ≈1.08），不是 D！

    第一版这里误传了 7.2 ⇒ `e_new` 被 `e_hi=1.5` 截断成 1.5、`delta_e=-5.7`，
    测试当场红了（自测抓到的真实用例错误，记录在此防止回退）。
    """
    counts = counts or H_KCWAYA_R2
    return GR.solve_e(counts, 1.08, n_total=N_TOTAL, n_drag=N_DRAG,
                      used_edge=used_edge, damp=GR.DAMP_K,
                      mu_deadband_ms=GR.MU_DEADBAND_MS, **kw)


# --------------------------------------------------------------------------- #
# [1] 两种实现必须可区分
# --------------------------------------------------------------------------- #
def test_pure_g_round_yields_not_edge_with_mu():
    """★ 本轮改动的核心断言：纯 G 轮能拿到 μ，但**拿不到** e 的修正。"""
    sol = _solve(used_edge=False)
    assert sol["code"] == "not_edge", f"应为 not_edge，实得 {sol['code']}"
    assert sol["mu_ms"] == pytest.approx(21.8, abs=1.0)
    assert sol.get("sigma_ms") == pytest.approx(23.1, abs=1.5)
    assert "e_new" not in sol, "纯 G 轮不许带 e_new（zero 里没有 e）"
    assert "delta_e" not in sol, "纯 G 轮不许带 delta_e"


def test_edge_round_yields_ok_with_e_new():
    """对照组：同一组计数换成边沿轮 ⇒ code=ok 且**有** e 修正。

    这条是 [1] 能抓 bug 的前提 —— 两条一起才证明「used_edge 真的改变行为」；
    只测纯 G 的话，把 used_edge 硬写成 False（连 l4c 轮都按纯 G 处理）也会全绿。
    """
    sol = _solve(used_edge=True)
    assert sol["code"] == "ok", f"应为 ok，实得 {sol['code']}"
    assert sol["mu_ms"] is not None
    assert "e_new" in sol and "delta_e" in sol
    assert sol["delta_e"] == pytest.approx(-GR.DAMP_K * 21.8 / 1000.0, abs=0.01)


def test_pure_g_degenerate_counts_still_sealed():
    """E=L=0（无方向）时纯 G 轮也得是 not_edge，且被标记退化 ⇒ 后面不入池。"""
    sol = _solve(used_edge=False, counts={"early": 0, "late": 0, "p": 340,
                                          "g": 0, "b": 0, "m": 0})
    assert sol["code"] == "not_edge"
    assert sol.get("mu_degenerate") is True
    assert sol.get("sigma_ms") is None
    assert "e_new" not in sol


def test_pure_g_alternating_path_is_sealed_too():
    """同号门把它判成 alternating 时，纯 G 轮也不能借此拿到 e 修正。"""
    sol = _solve(used_edge=False, last_mu_ms=-200.0)
    assert sol["code"] == "not_edge", f"alternating 必须被 _seal 改回 not_edge，实得 {sol['code']}"
    assert "e_new" not in sol and "delta_e" not in sol


# --------------------------------------------------------------------------- #
# [2] 本地防御闸
# --------------------------------------------------------------------------- #
def test_guard_pure_g_edit_catches_ok():
    """★ 模拟「_seal 契约将来失效」：给了 ok + e 字段，闸必须改回 not_edge 并剥掉。"""
    sol = {"code": "ok", "mu_ms": 21.8, "e_new": 1.04, "delta_e": -0.04,
           "note": "原本的说明"}
    hit = AR.guard_pure_g_edit(sol)
    assert hit == "ok"
    assert sol["code"] == "not_edge"
    assert "e_new" not in sol and "delta_e" not in sol
    assert "本地闸门" in sol["note"]
    assert "原本的说明" in sol["note"], "原有 note 不许被覆盖（会丢证据）"


def test_guard_pure_g_edit_passes_safe_input_untouched():
    """本来就安全（not_edge）⇒ 返回 None 且**逐字段不变**（不许顺手改写）。"""
    sol = {"code": "not_edge", "mu_ms": 21.8, "sigma_ms": 23.1, "note": "x"}
    before = dict(sol)
    assert AR.guard_pure_g_edit(sol) is None
    assert sol == before


def test_guard_pure_g_edit_handles_none():
    assert AR.guard_pure_g_edit(None) is None       # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# [3] 入池判据
# --------------------------------------------------------------------------- #
def test_plan_pool_write_exact_math():
    """d_ideal = d_used − μ/1000：7.2 − 0.0218 = 7.1782。"""
    d, why = AR.plan_d_pool_write({"mu_ms": 21.8, "sigma_ms": 23.1}, D_USED_HKCWAYA)
    assert why == ""
    assert d == pytest.approx(7.1782, abs=1e-9)


def test_plan_pool_write_sign_is_minus():
    """★ 变异防线：符号反了（`+ μ`）会得 7.2218 —— 这条专门钉方向。

    μ>0 = 判定偏晚 ⇒ 说明我们用的 D 太**大**（zero 太晚）⇒ 该用的 D 更**小**。
    写成加法会让池子朝错的方向走，而且每轮都走，**看不出来**。
    """
    d, _ = AR.plan_d_pool_write({"mu_ms": 21.8, "sigma_ms": 23.1}, D_USED_HKCWAYA)
    assert d < D_USED_HKCWAYA
    assert d != pytest.approx(D_USED_HKCWAYA + 0.0218, abs=1e-9)
    d_neg, _ = AR.plan_d_pool_write({"mu_ms": -20.0, "sigma_ms": 23.1}, D_USED_HKCWAYA)
    assert d_neg > D_USED_HKCWAYA, "μ<0（偏早）⇒ 该用的 D 更**大**"


def test_plan_pool_write_rejects_degenerate():
    """σ 无拟合 / 显式退化 ⇒ 拒绝（占位 0.0 不等于「实测偏移=0」）。"""
    for sol in ({"mu_ms": 0.0, "sigma_ms": None},
                {"mu_ms": 21.8, "sigma_ms": 23.1, "mu_degenerate": True},
                {"mu_ms": 0.0, "sigma_ms": None, "mu_degenerate": True}):
        d, why = AR.plan_d_pool_write(sol, D_USED_HKCWAYA)
        assert d is None, f"{sol} 不该入池"
        assert why, "拒绝时必须给出原因（不许静默）"


def test_plan_pool_write_rejects_missing_mu():
    d, why = AR.plan_d_pool_write({"code": "unreadable"}, D_USED_HKCWAYA)
    assert d is None and "μ 缺失" in why
    d2, why2 = AR.plan_d_pool_write(None, D_USED_HKCWAYA)
    assert d2 is None and why2


def test_plan_pool_write_does_not_use_resid():
    """★ 残差恒为 0（零自由度）⇒ 判据里**不许**出现 resid。

    实测：hckwaya_r2 的 E0/L2/P338 解出 resid=0.00000，任何实现下都「看起来完美」。
    若有人把 `resid <= 0.01` 当成入池条件，这一条会露出来（它恒真，等效无判断）。
    """
    sol = _solve(used_edge=False)
    assert sol["resid"] == pytest.approx(0.0, abs=1e-12)
    # 造一条 resid 很大但 μ/σ 齐全的解 —— 判据只看 μ/σ，不该被 resid 拦住
    d, why = AR.plan_d_pool_write({"mu_ms": 21.8, "sigma_ms": 23.1, "resid": 9.9}, 7.2)
    assert d is not None and why == ""


# --------------------------------------------------------------------------- #
# [4] src 标记不许改旧 schema
# --------------------------------------------------------------------------- #
def test_record_d_eff_src_marker():
    st = {}
    AR.record_d_eff(st, "t1", 7.1782, mu_ms=21.8, d_used_s=7.2,
                    device="DEV", src=AR.D_POOL_SRC_MU)
    e = st["d_eff_recent_by_device"]["DEV"][-1]
    assert e["src"] == "mu"
    assert e["d_ideal_s"] == pytest.approx(7.1782, abs=1e-5)


def test_record_d_eff_legacy_entry_has_no_src_key():
    """★ `src=None` ⇒ 键集合必须与 2026-10-23 之前**完全一致**。

    无条件写 `entry["src"]=None` 会改掉 6 条历史样本的 schema ——
    它们是 `analyze_g_drift` / `backfill_deff` 的输入，结构变了就说不清口径。
    """
    st = {}
    AR.record_d_eff(st, "t1", 7.1782, mu_ms=21.8, d_used_s=7.2, device="DEV")
    e = st["d_eff_recent_by_device"]["DEV"][-1]
    assert set(e) == {"ts", "tag", "d_ideal_s", "mu_ms", "d_used_s"}
    assert "src" not in e


def test_record_d_eff_keeps_only_six():
    st = {}
    for i in range(9):
        AR.record_d_eff(st, f"t{i}", 7.0 + i / 100.0, device="DEV")
    pool = st["d_eff_recent_by_device"]["DEV"]
    assert len(pool) == AR.D_EFF_KEEP == 6
    assert pool[-1]["tag"] == "t8"


# --------------------------------------------------------------------------- #
# [5] 接线（AST，不是 grep）
# --------------------------------------------------------------------------- #
def _if_nodes_containing(tree: ast.AST, func_attr: str) -> list[ast.If]:
    """找出「body 里调用了 <module>.<func_attr>」的所有 If 节点（可嵌套多层）。"""
    out: list[ast.If] = []

    def walk(node: ast.AST) -> None:
        for ch in ast.iter_child_nodes(node):
            if isinstance(ch, ast.If):
                body_src = "".join(ast.unparse(s) for s in ch.body)
                tail_src = "".join(ast.unparse(s) for s in ch.orelse)
                if f".{func_attr}(" in body_src or f".{func_attr}(" in tail_src:
                    out.append(ch)
            walk(ch)

    walk(tree)
    return out


def _full_run_tree() -> ast.Module:
    return ast.parse((TMP / "full_run.py").read_text(encoding="utf-8"))


def test_gate_no_longer_depends_on_acoustic():
    """★ μ 求解块的门控**不许**再引用 ACOUSTIC（否则纯 G 轮永远不进）。

    为什么用 AST 而不是 grep：grep 依赖空格/换行原文，一次格式化就红；
    AST 直接钉住「这个 If 的条件里有没有 ACOUSTIC」这个语义。
    """
    tree = _full_run_tree()
    hits = _if_nodes_containing(tree, "solve_e_from_shots")
    assert hits, "找不到包含 solve_e_from_shots 的 If 节点 ⇒ 脚本结构变了，本测试需同步"
    tests = [ast.unparse(n.test) for n in hits]
    assert any("isinstance(res" in t for t in tests), f"门控应改为 isinstance(res,...)，实得 {tests}"
    assert not any("ACOUSTIC" in t for t in tests), \
        f"门控仍在判 ACOUSTIC ⇒ 纯 G 轮不会进块。条件：{tests}"


def test_nudge_e_is_acoustic_guarded():
    """★ P1 致命漏洞：`nudge_e` 会改 e 并写盘；纯 G 轮必须被 ACOUSTIC 挡在外面。"""
    tree = _full_run_tree()
    hits = _if_nodes_containing(tree, "nudge_e")
    assert hits, "找不到包含 nudge_e 的 If 节点"
    tests = [ast.unparse(n.test) for n in hits]
    assert any("ACOUSTIC" in t for t in tests), \
        f"nudge_e 的守卫里没有 ACOUSTIC ⇒ 纯 G 轮会改 e（下一轮偏 40ms）。条件：{tests}"


def test_records_src_and_uses_plan_function():
    """接线：池写入必须走 `plan_d_pool_write` 且带 `src=`（否则 [3][4] 的保证是死的）。"""
    src = (TMP / "full_run.py").read_text(encoding="utf-8")
    assert "plan_d_pool_write(" in src, "池写入没走纯函数 ⇒ 单测与生产是两套实现（空转）"
    assert "src=(ar_mod.D_POOL_SRC_STEP" in src or "src=ar_mod.D_POOL_SRC" in src


def test_used_edge_argument_is_wired_to_guard_ok():
    """★ M2 变异（把 `used_edge` 硬写成 True）首轮**空转**，暴露的真实盲区。

    [1] 那两条测的是 `solve_e` 本身（直接传参），够不到 `full_run.py` 的**接线点**。
    一旦有人把 `used_edge=bool(l4c.get("guard_ok"))` 写成 `used_edge=True`，
    纯 G 轮就会拿到 `code=="ok"` + `e_new` ⇒ `apply_e_update` 把 e 改掉 ⇒
    下一轮 l4c 偏 20~40 ms。而 [1] 全绿、[2] 的闸只在 `_pure_g` 分支里兜底
    （它确实会拦，但那时已经算出了错误的 e，属于「靠兜底掩盖接线错误」）。
    ⇒ 接线必须单独钉住。
    """
    tree = _full_run_tree()
    vals: list[str] = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "solve_e_from_shots"):
            for kw in node.keywords:
                if kw.arg == "used_edge":
                    vals.append(ast.unparse(kw.value))
    assert vals, "找不到 used_edge 实参 ⇒ 调用方式变了，本测试需同步"
    for v in vals:
        assert "guard_ok" in v, f"used_edge 被写死/改源（{v}）⇒ 纯 G 轮会拿到 e 修正"
