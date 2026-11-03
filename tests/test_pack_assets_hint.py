"""「缺载荷」提示必须指向**真正能生成载荷**的那条命令。

背景（2026-11-03 用户实测）：用户新人按 README 走到 `pack_assets.py`，得到
`✗ 没有可打包的曲目`。原提示指 `tools/extract_chart.py --all`，但那一步只产
`out/charts/`；照它跑完再打包，报错一字不变 —— 白绕一圈。

这条 bug 的特征是「不报错、只静默带偏」：提示语法通顺、命令真实存在、跑完也真没报错，
所以靠人读是发现不了的，必须用断言钉住「提示里出现 make_payloads、且不出现 extract_chart」。
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

pa = pytest.importorskip("pack_assets")


def test_hint_names_make_payloads():
    """正向：提示必须给出能生成 out/touches_*.json 的命令。"""
    h = pa.no_payload_hint(7)
    assert "make_payloads.py" in h


def test_hint_does_not_point_at_extract_chart():
    """反向：这是本轮修的 bug —— extract_chart 只产谱面，不是载荷的来源。

    ⛔ 别把这条删掉当成「反正都差不多」：`extract_chart` 确实是上游（谱面来源），
       写成「先跑 extract_chart」读起来很合理，正是它骗过了上一版作者。
    """
    h = pa.no_payload_hint(7)
    assert "extract_chart" not in h


def test_empty_todo_hint_also_names_make_payloads():
    assert "make_payloads.py" in pa.empty_todo_hint()
    assert "extract_chart" not in pa.empty_todo_hint()


def test_hint_carries_the_count():
    """计数不能被吞掉：用户要靠它判断「是全都缺、还是只缺几首」。"""
    assert "7" in pa.no_payload_hint(7)
    assert "0" in pa.no_payload_hint(0)