"""「开头就是响的」曲子，`sil_ref` 必须是 ~0，**不许**退回 2.05 s 的经验值。

背景（2026-10-26 真机）：用户报「这首歌 K 和 lead 都测不出」。查出来是
`reference_silence()` 在 `find_silence_end` 返回 None 时一律套 `SILENCE_S_FALLBACK=2.05`，
而"返回 None"最常见的原因是**这首母带开头就响、根本没有静音→有声边沿**。
后果：零点偏 2.05 s；`K = E − S` 恒等于 ∓sil_ref ⇒ 被 range 闸门（|K|≤150 ms）永久拒收。
全库 **60 条**中招（`sil_thr=null` 的全是这批）。

判据要求：`leading_silence_s()` 必须把"开头就响"判成 0；有真静音的仍要给出真实边界。
"""

from __future__ import annotations

import struct
import sys
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pgauto.silref as EDGE  # noqa: E402  生产模块（2026-11-02 从 tmp/tb_audio_edge 搬入）


def _wav(path: Path, x: np.ndarray, sr: int = 44100) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(struct.pack("<%dh" % len(x), *x.astype("<i2")))
    return path


def test_leading_silence_zero_when_master_starts_loud(tmp_path):
    sr = 44100
    rng = np.random.default_rng(7)
    loud = (rng.normal(0, 6000, sr * 3)).clip(-32000, 32000)
    assert EDGE.leading_silence_s(loud, sr) < 0.05, "开头就响必须判成 ~0"


def test_leading_silence_measures_real_silence(tmp_path):
    sr = 44100
    rng = np.random.default_rng(8)
    x = np.concatenate([np.zeros(int(0.8 * sr)),
                        rng.normal(0, 6000, sr * 2)]).clip(-32000, 32000)
    assert abs(EDGE.leading_silence_s(x, sr) - 0.8) < 0.1


def test_reference_silence_does_not_use_fallback_for_loud_start(tmp_path):
    """真·回归：这条曾经返回 2.05 s，把 K 永久卡死在 range 闸门。"""
    sr = 44100
    rng = np.random.default_rng(9)
    x = (rng.normal(0, 7000, sr * 5)).clip(-32000, 32000)
    p = _wav(tmp_path / "loud_start.wav", x, sr)
    rs = EDGE.reference_silence(p)
    assert rs["sil_ref_s"] < 0.5, f"开头就响却给了 {rs['sil_ref_s']:.3f}s"
    assert abs(rs["sil_ref_s"] - EDGE.SILENCE_S_FALLBACK) > 1.0, "不许再套 2.05 s 兜底"
