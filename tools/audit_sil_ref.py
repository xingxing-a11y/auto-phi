#!/usr/bin/env python
"""全库审计：`sil_ref` / 母带指纹 / `sil_thr` 有没有"测不出"或"偏差很大"的同类问题。

背景（2026-10-26）：`sil_ref` 兜底 2.05 s 被套到"开头就响"的 60 条曲子上 ⇒ 零点偏 2 秒、K 永远被
range 闸门拒收。这里做一次系统排查，判据全部**离线可复现**：

1. **sil_ref 偏差**：用 `leading_silence_s()` 从母带独立量一次，与 manifest 比。
   ⚠ 阈值取 **1.0 s**：端内 `EnvMatcher` 的先验窗是 ±100 帧（±1 s），
   `|Δ| > 1 s` 时真实峰会掉出窗口 ⇒ matcher 锁到错峰 ⇒ K 被 range 闸门永久拒收（正是那 60 条的症状）。
2. **指纹可用性**：`env_b64` 必须解出 `env_frames` 帧、不是全零、且与母带前 30 s 的包络相关（防"指纹与母带不匹配"）。
3. **sil_thr**：`thr` 为 null 时端内走相对判据（合法），但要统计有多少首靠这条路。

用法：PYTHONUTF8=1 .venv/Scripts/python.exe tmp/_audit_silref.py [--workers 8]
输出：tmp/_audit_silref.json + 控制台摘要
"""
from __future__ import annotations

import argparse
import base64
import json
import math
import sys
import time
import wave
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tmp"))
import tb_audio_edge as EDGE  # noqa: E402

LOG_MAX = math.log(32768.0)
WINDOW_S = 1.0          # 端内先验半宽（±100 帧 × 10 ms）


def rms_of(q: int) -> float:
    if q <= 0:
        return 0.0
    if q >= 255:
        return 32768.0
    return math.exp((q - 1) * LOG_MAX / 254.0)


def master_env(song_dir: str, frames: int = 3000) -> np.ndarray | None:
    p = ROOT / "samples" / song_dir / "music.wav"
    if not p.is_file():
        return None
    try:
        with wave.open(str(p), "rb") as w:
            sr, n, ch = w.getframerate(), w.getnframes(), w.getnchannels()
            raw = np.frombuffer(w.readframes(min(n, int(sr * 30.5))), dtype="<i2").astype(np.float32)
            if ch == 2:
                raw = raw.reshape(-1, 2).mean(1)
        hop = max(1, int(round(sr * 0.01)))
        m = len(raw) // hop
        if m == 0:
            return None
        e = np.sqrt((raw[: m * hop].reshape(m, hop) ** 2).mean(1))
        out = np.zeros(frames)
        out[: min(frames, m)] = e[: min(frames, m)]
        return out
    except Exception:
        return None


def corr(a: np.ndarray, b: np.ndarray) -> float:
    n = min(len(a), len(b))
    x, y = a[:n], b[:n]
    x = np.log(np.maximum(x, 1.0))
    y = np.log(np.maximum(y, 1.0))
    x = x - x.mean()
    y = y - y.mean()
    d = math.sqrt(float((x * x).sum() * (y * y).sum()))
    return float((x * y).sum() / d) if d > 0 else 0.0


def audit_one(e: dict) -> dict:
    key, song_dir = e["key"], e.get("song_dir") or ""
    out = {"key": key, "song_dir": song_dir, "sil_manifest": e.get("sil_ref_s"),
           "sil_thr": e.get("sil_thr"), "env_len": len(e.get("env_b64") or "")}
    menv = master_env(song_dir)
    if menv is None:
        out["err"] = "缺母带"
        return out
    out["sil_measured"] = round(EDGE.leading_silence_s(menv, 100), 3)   # menv 本身是 10 ms 帧
    out["sil_delta"] = round((e.get("sil_ref_s") or 0.0) - out["sil_measured"], 3)
    # 母带开头是否真的安静（用于分类）
    out["master_head_rms"] = float(menv[:30].mean())
    b64 = e.get("env_b64") or ""
    if b64:
        try:
            u8 = np.frombuffer(base64.b64decode(b64), dtype=np.uint8)
            ref = np.array([rms_of(int(v)) for v in u8], dtype=float)
            out["env_frames"] = int(len(u8))
            out["env_zero_frac"] = round(float((u8 == 0).mean()), 3)
            out["env_corr"] = round(corr(ref, menv), 3)
        except Exception as exc:  # noqa: BLE001
            out["env_err"] = str(exc)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=8)
    a = ap.parse_args()
    man = json.loads((ROOT / "android/play/app/src/main/assets/manifest.json")
                     .read_text(encoding="utf-8"))["songs"]
    print(f"审计 {len(man)} 条…", flush=True)
    t0 = time.time()
    rows = []
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        futs = [ex.submit(audit_one, e) for e in man]
        done = 0
        for f in as_completed(futs):
            rows.append(f.result())
            done += 1
            if done % 200 == 0:
                print(f"  {done}/{len(man)} {time.time()-t0:.0f}s", flush=True)
    (ROOT / "tmp" / "_audit_silref.json").write_text(
        json.dumps(rows, ensure_ascii=False), encoding="utf-8")

    err = [r for r in rows if "err" in r]
    big = sorted([r for r in rows if abs(r.get("sil_delta") or 0) > WINDOW_S],
                 key=lambda r: -abs(r["sil_delta"]))
    mid = [r for r in rows if 0.1 < abs(r.get("sil_delta") or 0) <= WINDOW_S]
    lowcorr = [r for r in rows if r.get("env_corr") is not None and r["env_corr"] < 0.9]
    no_thr = [r for r in rows if r.get("sil_thr") is None]
    print(f"\n完成 {len(rows)} 条，{time.time()-t0:.0f}s")
    print(f"缺母带 {len(err)} ｜ sil_thr=null {len(no_thr)} 条")
    print(f"**|Δsil_ref| > 1.0 s（会让 K 永久被拒）: {len(big)} 条**")
    for r in big[:15]:
        print(f"   {r['key']:34s} manifest={r['sil_manifest']:.3f}  实测={r['sil_measured']:.3f}"
              f"  Δ={r['sil_delta']:+.3f}  开头RMS={r['master_head_rms']:.0f}")
    print(f"0.1~1.0 s 的可疑偏差: {len(mid)} 条")
    for r in sorted(mid, key=lambda r: -abs(r["sil_delta"]))[:8]:
        print(f"   {r['key']:34s} manifest={r['sil_manifest']:.3f}  实测={r['sil_measured']:.3f}  Δ={r['sil_delta']:+.3f}")
    print(f"指纹与母带相关 < 0.9 的: {len(lowcorr)} 条")
    for r in sorted(lowcorr, key=lambda r: r["env_corr"])[:8]:
        print(f"   {r['key']:34s} corr={r['env_corr']:.3f} zero_frac={r.get('env_zero_frac')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
