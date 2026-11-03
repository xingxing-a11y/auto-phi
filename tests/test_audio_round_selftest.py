"""`tmp/audio_round.py --selftest` 必须**能跑且通过**（不是"缺夹具就跳过"）。

背景（2026-10-25 21:5x）：该自测原先硬依赖 `tmp/_rrhar_48k.wav`（moonlight 母带切片）。
那个文件在某次清理后**不存在了**，于是自测一跑就 `FileNotFoundError` ——
而**没有任何测试去看它**，所以"回归门跑不起来"这件事静默了好几轮（正是本项目最忌讳的假绿）。

修法：`audio_round.lead_source()` 缺夹具时改用别的曲子的母带（并打印警告），
本测试把"能跑 + 通过"钉住；⛔ 别改成 `skip`：跑不了和跑过且绿在回归账上是两回事。
"""

from __future__ import annotations

import subprocess
import sys

import pytest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: 公开仓按设计不导出 `tmp/`（那里面是跑分/审计脚本），所以这里只能在**主仓**跑。
#: ⛔ 这条 skipif 不是给"夹具缺失"开后门 —— 主仓永远有 `tmp/audio_round.py`，
#:   缺 `_rrhar_48k.wav` 时 selftest 仍然会红，那正是它该干的。
pytestmark = pytest.mark.skipif(
    not (ROOT / "tmp" / "audio_round.py").exists(),
    reason="公开仓不导出 tmp/（跑分工具不在分发范围内）",
)


def test_audio_round_selftest_runs_and_passes():
    r = subprocess.run(
        [sys.executable, str(ROOT / "tmp" / "audio_round.py"), "--selftest"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")
    out = (r.stdout or "") + (r.stderr or "")
    assert r.returncode == 0, f"audio_round --selftest 失败（rc={r.returncode}）：\n{out[-2000:]}"
    assert "通过" in out or "PASS" in out.upper(), f"没看到通过标记：\n{out[-800:]}"


def test_lead_source_never_silently_missing():
    """缺 `_rrhar_48k.wav` 时必须**明确报出替代来源**，不许静默返回空。"""
    r = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0,'tmp');"
         "import audio_round as AR;"
         "x, sr = AR.lead_source();"
         "print('LEN', len(x), 'SR', sr)"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert r.returncode == 0, f"lead_source 失败：\n{r.stdout[-500:]}\n{r.stderr[-800:]}"
    out = r.stdout or ""
    assert "LEN " in out
    n = int(out.split("LEN ")[1].split()[0])
    assert n > 48000 * 5, f"垫场音频太短（{n} 帧），自测会失真"
