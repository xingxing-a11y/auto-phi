"""跨模块常量一致性 —— 只有**源码级**断言才不会「注释说会检查、实际没人检查」。

背景（2026-10-18）：`tmp/grade_read.py` 故意不 import `tmp/audio_round.py`
（会拉进 scrcpy / tb_* 依赖），所以 `E_DEFAULT` / `E_LO` / `E_HI` 三份常量是**两份拷贝**，
靠人工同步。当天 `audio_round.E_HI` 从 1.20 放宽到 1.50、`grade_read` 漏改，
而当时 `grade_read.py` 的注释写着「`--selftest-e` 会显式断言」——实际只有一句 `print`。
结果：287 项 pytest + 41/39 项自测**全绿通过**，不一致活了一整天。

⇒ 本文件就是那个「真正比对」。它**不 import 任何被测模块**（避免依赖），
  只读源码正则抽常量 —— 测的正是「两份拷贝是否一致」这件事本身。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
KEYS = ("E_DEFAULT", "E_LO", "E_HI")

#: 本文件测的是「`tmp/audio_round.py` 与 `tmp/grade_read.py` 两份常量拷贝是否同步」，
#: 两份源码都在 `tmp/`，而公开仓不导出它 ⇒ 只能在主仓跑。
pytestmark = pytest.mark.skipif(
    not (ROOT / "tmp" / "audio_round.py").exists(),
    reason="公开仓不导出 tmp/（被核对的两份实现都不在分发范围内）",
)


def _consts(path: Path) -> dict[str, float]:
    """从源码抽 e 的三个常量（匹配 `E_DEFAULT = 0.683` 与 `E_LO, E_HI = 0.30, 1.50`）。"""
    src = path.read_text(encoding="utf-8")
    got: dict[str, float] = {}
    m = re.search(r"^E_DEFAULT\s*=\s*([\d.]+)", src, re.M)
    if m:
        got["E_DEFAULT"] = float(m.group(1))
    m = re.search(r"^E_LO,\s*E_HI\s*=\s*([\d.]+),\s*([\d.]+)", src, re.M)
    if m:
        got["E_LO"], got["E_HI"] = float(m.group(1)), float(m.group(2))
    return got


def test_e_constants_match_between_audio_round_and_grade_read():
    """两个模块的 e 常量必须逐值相同（1e-9 容差，都是十进制字面量）。"""
    a = _consts(ROOT / "tmp" / "audio_round.py")
    g = _consts(ROOT / "tmp" / "grade_read.py")
    assert set(a) == set(KEYS), f"audio_round 抽不到全部常量：{a}"
    assert set(g) == set(KEYS), f"grade_read 抽不到全部常量：{g}"
    for k in KEYS:
        assert a[k] == g[k], f"{k}: audio_round={a[k]} vs grade_read={g[k]}"


def test_grader_read_semantic_direction():
    """顺带的语义护栏：`E_LO < 0.683 < E_HI`。

    这不是重复断言，而是防「为了消红把区间改到无意义」——
    比如把 `E_HI` 调到 0.70 能让所有夹取失效，那时两模块一致但语义错。
    """
    g = _consts(ROOT / "tmp" / "grade_read.py")
    assert g["E_LO"] < 0.683 < g["E_HI"], g


def test_e_hi_covers_current_calibrated_e():
    """上界必须覆盖**当前标定的 e**，否则 `solve_e` 会静默截断修正量。

    读 `audio_calib.json` 而不是硬编码数字：标定值随设备重标而变，但
    「`E_HI` ≥ 当前 `e_s`」这条不变量永远成立。（该文件被 gitignore ⇒ 新克隆会 skip。）
    """
    import json

    import pytest

    g = _consts(ROOT / "tmp" / "grade_read.py")
    p = ROOT / "tmp" / "audio_calib.json"
    if not p.exists():
        pytest.skip("没有 audio_calib.json（状态文件不入库）")
    e_s = float(json.loads(p.read_text(encoding="utf-8"))["e_s"])
    assert g["E_HI"] >= e_s, f"E_HI={g['E_HI']} < 当前 e_s={e_s} ⇒ 修正量会被静默截断"
