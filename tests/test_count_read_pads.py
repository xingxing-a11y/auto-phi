"""`tmp/count_read.py` 的 PGBM 分箱补边（`PGBM_PADS`）离线测试 —— 不连手机。

病灶（2026-10-22 真机一轮，wanju_in / 1610x720）：该轮 `history` 记 `verdict=error`，
但成绩是好的（E0 / L3 / P1376 / G3 / B0 / M39）。真因是**读判器**两边同时缺读数：

  ① `M=39` 的首位 `3`：`zone_boxes` 的行投影把顶行 `536` 判成背景 ⇒ bbox 高 20 而真字形 21
     ⇒ `feature()` 按高度归一化后宽高比失真 ⇒ 只得 0.8768 < `THR_HIT_BIG=0.89`；
  ② `P=1376` 的末位 `6`：与 `8` 分数 **0.8624 vs 0.8613（margin 0.0011）**，两者都 < 0.89
     ⇒ 这是 720p 下**真的不可分**，只能靠恒等式补（设计上正确的「安全失败」）。

①能被补边修好（`0.8768 → 0.9788`），②不能。修好①之后只剩**一个**未知 ⇒
`P+G+B+M ≡ n_total` 唯一解出 `P=1376` ⇒ 整轮可判。补边是**修正分割误差**，
不降 `THR_HIT_BIG`（降门槛会拆掉「缺字形时相近数字顶替」的防线）。

本文件把四条判据钉死：
  [1] `pad_box` 的四向补边与 `bounds` 夹取（含越界不回绕）
  [2] `PGBM_PADS` 的**顺序契约**：`(0,0,0,0)` 必须最前（原样命中时与旧版逐位相同）
  [3] 真图回归：原样就能读全的图，补边版读数必须**逐个相同**（不变量）
  [4] 真图定向：本轮那张图必须读出肉眼确认的六个数；把补边关掉必须读不出（反 vacuous）
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# `count_read.py` 在 `tmp/` 下（不是包，`conftest` 只把 ROOT 与 tools 加进 sys.path）。
# 用唯一模块名按路径加载，避免与别处的同名模块串味。
_MOD = "count_read_under_test"


def _load_count_read():
    if _MOD in sys.modules:
        return sys.modules[_MOD]
    path = ROOT / "tmp" / "count_read.py"
    if not path.exists():
        # ★ 必须带 allow_module_level=True：本函数在**模块级**被调用（`cr = _load_count_read()`），
        #   不带这个参数 pytest 会把整个模块判成收集错误（Interrupted），而不是跳过。
        pytest.skip(f"缺 {path}（读判器不在此环境）", allow_module_level=True)
    spec = importlib.util.spec_from_file_location(_MOD, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[_MOD] = mod
    spec.loader.exec_module(mod)
    return mod


cr = _load_count_read()

TARGET = ROOT / "tmp" / "result_wanju_in_1.png"
N_TOTAL_WANJU_IN = 1418            # out/touches_wanju_in.json 的 note_count
EYEBALLED = {"early": 0, "late": 3, "p": 1376, "g": 3, "b": 0, "m": 39}
KES = ("early", "late", "p", "g", "b", "m")


# ---------------------------------------------------------------- [1] pad_box

def test_pad_box_expands_each_side_independently():
    assert cr.pad_box((10, 20, 30, 40), 1, 0, 0, 0) == (9, 20, 30, 40)
    assert cr.pad_box((10, 20, 30, 40), 0, 1, 0, 0) == (10, 20, 31, 40)
    assert cr.pad_box((10, 20, 30, 40), 0, 0, 1, 0) == (10, 19, 30, 40)
    assert cr.pad_box((10, 20, 30, 40), 0, 0, 0, 1) == (10, 20, 30, 41)
    assert cr.pad_box((10, 20, 30, 40), 2, 2, 1, 1) == (8, 19, 32, 41)


def test_pad_box_clamps_into_bounds_instead_of_wrapping():
    """补边必须夹进区域边界 —— 否则会把相邻字符/图外像素算进来。"""
    bounds = (11, 21, 29, 39)
    assert cr.pad_box((10, 20, 30, 40), 5, 5, 5, 5, bounds) == (11, 21, 29, 39)
    # 一侧出界、另一侧正常：只夹出界那侧
    assert cr.pad_box((12, 22, 28, 38), 9, 1, 0, 0, bounds) == (11, 22, 29, 38)


def test_pad_box_zero_pad_is_identity():
    box = (846, 537, 854, 556)
    assert cr.pad_box(box, 0, 0, 0, 0) == box
    assert cr.pad_box(box, 0, 0, 0, 0, (800, 500, 900, 600)) == box


# ------------------------------------------------- [2] PGBM_PADS 顺序契约

def test_first_pad_is_noop_so_old_behaviour_is_reproduced_first():
    """`(0,0,0,0)` 必须排最前：原样能读全的图**先**按原样读，行为与旧版逐位相同。

    这条不是风格问题 —— 一旦有人把补边放到前面，所有图都会走补边路径，
    读数可能整体漂移，而「原本对的读数不变」这条不变量就没人守了。
    """
    assert cr.PGBM_PADS[0] == (0, 0, 0, 0)


def test_pads_are_four_wide_and_non_negative():
    for p in cr.PGBM_PADS:
        assert len(p) == 4, p
        assert all(isinstance(v, int) and v >= 0 for v in p), p
    # 去重：同一补边量试两次等于白跑
    assert len(set(cr.PGBM_PADS)) == len(cr.PGBM_PADS)


# ------------------------------------------------------- [3] 真图不变量回归

def _images(limit: int = 20):
    fs = sorted((ROOT / "tmp").glob("result_*.png"))
    # ★ 缺图**不要**在模块级 skip：本函数被 `@pytest.mark.parametrize("img", _images(20))` 调用，
    #   模块级 skip 会连 [1] 补边、[2] PGBM_PADS 这两组**纯逻辑**用例一起废掉（2026-10-30 实测：
    #   整份模块被跳过后，全量从 631 掉到 604）。返回空列表 ⇒ 只有 [3] 变空参数集，其余照跑。
    # ⚠ 这些 `tmp/result_*.png` 在 2026-10-30 清理 tmp 时被一并删掉（不在 git，无法逐字节恢复）
    #   ⇒ 恢复办法：下次打 `wanju_in` 时把结算页 `adb exec-out screencap -p` 存成
    #   `tmp/result_wanju_in_1.png`（[4] 的期望值是 P1376 G3 B0 M39 E0 L3）。
    return fs[:limit]


@pytest.mark.parametrize("img", _images(20), ids=lambda p: p.name)
def test_reads_that_worked_without_padding_are_unchanged(img):
    """不变量：原样 `(0,0,0,0)` 就能读全六项的图，补边版必须**逐个相同**。

    这条能抓到「补边把已经读对的数字改坏」这类回归 —— 那是最坏的一类错：
    读数从对变错，而流水线不会报错。
    """
    base = cr.read_counts(img)
    old = {k: base.get(k) for k in KES}
    if any(old[k] is None for k in KES):
        pytest.skip("该图原样就读不全，不在本不变量覆盖范围")

    saved = cr.PGBM_PADS
    try:
        cr.PGBM_PADS = ((0, 0, 0, 0),)
        a = {k: cr.read_counts(img).get(k) for k in KES}
    finally:
        cr.PGBM_PADS = saved
    assert a == old


# --------------------------------------------------------- [4] 定向 + 反 vacuous

def _need_target():
    if not TARGET.exists():
        pytest.skip(f"缺 {TARGET}（本轮真机结算图已被清理）")


def test_wanju_in_round4_reads_all_six_counts():
    """本轮那张图必须读出肉眼确认的六个数（我人工看过原图：P1376 G3 B0 M39 E0 L3）。"""
    _need_target()
    d = cr.read_counts(TARGET, n_total=N_TOTAL_WANJU_IN)
    got = {k: d.get(k) for k in KES}
    assert got == EYEBALLED, got
    # 恒等式：每个音符只落四类之一
    assert sum(got[k] for k in ("p", "g", "b", "m")) == N_TOTAL_WANJU_IN
    # 恒等式：G ≡ Early + Late
    assert got["g"] == got["early"] + got["late"]


def test_without_pads_the_same_image_is_unreadable():
    """反 vacuous：把补边关掉，同一张图必须读不出 —— 否则说明补边根本没起作用。

    （关掉后 `P` 因 `6`/`8` 不可分、`M` 因首位 `3` 少一行，两个未知 ⇒ 不唯一 ⇒ error）
    """
    _need_target()
    saved = cr.PGBM_PADS
    try:
        cr.PGBM_PADS = ((0, 0, 0, 0),)
        d = cr.read_counts(TARGET, n_total=N_TOTAL_WANJU_IN)
    finally:
        cr.PGBM_PADS = saved
    missing = [k for k in ("p", "g", "b", "m") if d.get(k) is None]
    assert len(missing) >= 2, (missing, {k: d.get(k) for k in KES})


def test_miss_digit_is_recovered_by_vertical_padding_only():
    """`M` 的 `3` 只靠**竖直**补边就能救回来（水平补边救不回）—— 钉住病根方向。

    实测：只补左右 → 0.8879（<0.89，仍失败）；上下各 1 行 → 0.9186+。
    若有人把补边序列改成只补左右，这条会红。
    """
    _need_target()
    a = cr.load(str(TARGET))
    tpl = cr.load_tpl_big()
    if not tpl:
        pytest.skip("缺大号字模板")
    zones = cr.zones_for_screen(a.shape[1], a.shape[0])
    _rows, pgbm_row, pgbm_zones = zones
    x0, x1 = pgbm_zones["m"]

    def read_with(pad):
        s, _d = cr.read_zone(a, pgbm_row[0], pgbm_row[1], x0, x1, tpl,
                             cr.THR_WHITE, thr_hit=cr.THR_HIT_BIG,
                             thr_margin=cr.THR_MARGIN_BIG, pads=(pad,))
        return s

    assert read_with((0, 0, 0, 0)) is None          # 原样读不出
    assert read_with((1, 1, 0, 0)) is None          # 只补左右：仍读不出（病根不在横向）
    assert read_with((0, 0, 1, 1)) == "39"          # 补上下：读出来
