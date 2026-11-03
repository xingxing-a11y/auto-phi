#!/usr/bin/env python
"""逐曲模拟端内 K 计算链路：用母带当「完美录音」，看 K 会不会被拒或偏大。

为什么要这一步（2026-10-26）：`sil_ref` 的兜底 bug 让 60 首曲子的 `K` 恒等于 ∓sil_ref、
被 `MAX_ABS_K_MS=150` 的 range 闸门永久拒收，而面板只显示"未标定"——**离线没有任何检查能发现**。
`tmp/_audit_silref.py` 只比了 sil_ref 的数值；这里直接把端内两步算出来：

    ff  = 闸门在「录音」里的触发帧（绝对阈值=manifest.sil_thr；无 thr 则用 300 RMS 相对判据）
    E   = ff*hop − sil_ref*1000
    S   = EnvMatcher(录音包络, 母带指纹, 先验 center = sil_ref/hop − ff, ±100 帧)
    K   = E − S        ⇒ 期望 |K| 小（真机实测 −7~+53 ms）

判据：|K| > 150 ⇒ 该曲的真机上必然被 range 闸门拒收（= "测不出 K"）。
用法：PYTHONUTF8=1 .venv/Scripts/python.exe tmp/_audit_k_sim.py [--workers 8]
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

HOP = 10
LEAD_MS = 2000          # 模拟「点 ▶ 到歌曲 t=0」的间隔
QUIET_RMS = 300.0       # 端内 OnsetGate.DEFAULT_QUIET_RMS
SUSTAIN = 3             # 30 ms
MAX_ABS_K = 150.0
LOG_MAX = math.log(32768.0)


def rms_of(q: int) -> float:
    if q <= 0:
        return 0.0
    if q >= 255:
        return 32768.0
    return math.exp((q - 1) * LOG_MAX / 254.0)


def master_env(song_dir: str, seconds: float = 60.0) -> np.ndarray | None:
    p = ROOT / "samples" / song_dir / "music.wav"
    if not p.is_file():
        return None
    try:
        with wave.open(str(p), "rb") as w:
            sr, n, ch = w.getframerate(), w.getnframes(), w.getnchannels()
            raw = np.frombuffer(w.readframes(min(n, int(sr * seconds))), dtype="<i2").astype(np.float32)
            if ch == 2:
                raw = raw.reshape(-1, 2).mean(1)
        hop = max(1, int(round(sr * HOP / 1000.0)))
        m = len(raw) // hop
        return np.sqrt((raw[: m * hop].reshape(m, hop) ** 2).mean(1))
    except Exception:
        return None


def gate_edge(env: np.ndarray, thr: float | None) -> int:
    """模拟 OnsetGate 在录音里的触发帧（索引，10 ms 帧）。"""
    if thr is not None:
        # 绝对阈值 + 30 ms 持续
        for i in range(len(env) - SUSTAIN + 1):
            if (env[i:i + SUSTAIN] >= thr).all():
                return i
        return -1
    # 相对判据：前置 300 ms 安静后首个 ≥300
    for i in range(len(env)):
        if env[i] >= QUIET_RMS:
            if i == 0:
                return 0
            if (env[max(0, i - 30):i] < QUIET_RMS).all():
                return i
    return -1


def feat(x: np.ndarray) -> np.ndarray:
    mx = float(x.max())
    floor = max(mx * 0.005, 1e-9)
    y = np.log(np.maximum(x, floor))
    return y - y.mean()


def match(rec: np.ndarray, ref: np.ndarray, center: int, half: int = 100):
    def trim(x, rel):
        thr = max(float(x.max()) * rel, 1.0)
        idx = np.where(x > thr)[0]
        return (0, len(x)) if idx.size == 0 else (int(idx[0]), int(idx[-1]) + 1)
    a0, b0 = trim(rec, 0.01)
    r0, r1 = trim(ref, 0.005)
    L, R = feat(rec[a0:b0]), feat(ref[r0:r1])
    n, m = len(L), len(R)
    c = center - (r0 - a0)
    best, best_lag = -2.0, None
    for lag in range(c - half, c + half + 1):
        lo, hi = max(0, lag), min(m, lag + n)
        a, b = lo - lag, hi - lag
        if b - a < 50:
            continue
        sl, sr = L[a:b], R[lo:hi]
        d = math.sqrt(float((sl * sl).sum()) * float((sr * sr).sum()))
        cc = float((sl * sr).sum()) / d if d > 0 else 0.0
        if cc > best:
            best, best_lag = cc, lag
    if best_lag is None:
        return None
    S = -((best_lag) + r0 - a0) * HOP
    return best, S


def sim_one(e: dict) -> dict:
    key, song_dir = e["key"], e.get("song_dir") or ""
    menv = master_env(song_dir)
    if menv is None:
        return {"key": key, "err": "缺母带"}
    lead_frames = LEAD_MS // HOP
    rec = np.concatenate([np.zeros(lead_frames), menv])
    thr = e.get("sil_thr")
    edge = gate_edge(rec, thr)
    if edge < 0:
        return {"key": key, "err": "闸门未触发"}
    ff = edge
    sil_ms = float(e.get("sil_ref_s") or 0.0) * 1000.0
    E = ff * HOP - sil_ms
    ref = np.array([rms_of(int(b)) for b in base64.b64decode(e.get("env_b64") or "")])
    if ref.size == 0:
        return {"key": key, "err": "无指纹"}
    m = match(rec, ref, center=int(round(sil_ms / HOP)) - ff)
    if m is None:
        return {"key": key, "err": "匹配失败"}
    cc, S = m
    K = E - S
    return {"key": key, "song_dir": song_dir, "cc": round(cc, 3), "K": round(K, 1),
            "ok": abs(K) <= MAX_ABS_K, "thr": thr is not None, "sil": round(sil_ms, 1),
            "gate_ms": edge * HOP}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=8)
    a = ap.parse_args()
    man = json.loads((ROOT / "android/play/app/src/main/assets/manifest.json")
                     .read_text(encoding="utf-8"))["songs"]
    # 同曲不同难度共用母带 ⇒ 按 song_dir 去重（sil_ref 也同源）
    seen, todo = set(), []
    for e in man:
        d = e.get("song_dir")
        if d in seen:
            continue
        seen.add(d)
        todo.append(e)
    print(f"模拟 {len(todo)} 首（按 song_dir 去重，覆盖 {len(man)} 条）…", flush=True)
    t0 = time.time()
    rows = []
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        futs = [ex.submit(sim_one, e) for e in todo]
        done = 0
        for f in as_completed(futs):
            rows.append(f.result())
            done += 1
            if done % 100 == 0:
                print(f"  {done}/{len(todo)} {time.time()-t0:.0f}s", flush=True)
    (ROOT / "tmp" / "_audit_k_sim.json").write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    good = [r for r in rows if "err" not in r and r["ok"]]
    bad = sorted([r for r in rows if "err" not in r and not r["ok"]], key=lambda r: -abs(r["K"]))
    err = [r for r in rows if "err" in r]
    print(f"\n完成 {len(rows)} 首，{time.time()-t0:.0f}s")
    print(f"K 在范围内（|K|≤150）: {len(good)} ｜ **会被 range 拒收: {len(bad)}** ｜ 模拟失败: {len(err)}")
    for r in bad[:20]:
        print(f"   {r['key']:34s} K={r['K']:+9.1f} ms  cc={r['cc']:.3f}  sil={r['sil']:.0f}  "
              f"gate={r['gate_ms']} ms  {'绝对阈值' if r['thr'] else '相对判据'}")
    if err:
        print("失败：", [(r['key'], r['err']) for r in err[:8]])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
