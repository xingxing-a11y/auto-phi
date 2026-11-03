"""离线复现 EnvMatcher.match，用来验证「先验 center 的符号」与「截取 30 s」的正确性。

为什么要有它（2026-10-29 教训）：
  K 审计在真机上连续翻车两次（center 写反 ⇒ 全范围搜索锁错 4.2 s；rec 整首 16934 帧
  而母带指纹只有 3000 帧 ⇒ minOverlap=4233 > 3000 ⇒ 「先验窗越界」）。
  **每次都是靠真机一轮 3 分钟去发现**，代价太高。
  这个脚本用现成的标定样本（out/env_*.csv 有捕获包络 + 头里有已知 s_ms）离线复现，
  让「改完能不能算对」在 PC 上就能判，不必上真机。

用法：
  python tools/kaudit_offline.py <env_csv> [--cut-start N] [--cut-len N] [--center MODE]
  MODE: correct（ff − silRef）| legacy（silRef − ff）| none（不传先验）
"""
from __future__ import annotations

import argparse
import base64
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "android/play/app/src/main/assets/manifest.json"

MIN_OVERLAP_FRAMES = 50
GUARD_FRAMES = 50
MAX_RMS = 32768.0
LOG_MAX = math.log(MAX_RMS)


def rms_of(q: int) -> float:
    """EnvCodec.rmsOf：u8 → RMS。"""
    if q <= 0:
        return 0.0
    if q >= 255:
        return MAX_RMS
    return math.exp((q - 1) * LOG_MAX / 254.0)


def trim(x: list[float], rel: float) -> tuple[int, int]:
    """EnvMatcher.trim：掐掉首尾低于 rel×峰值的帧。"""
    if not x:
        return 0, 0
    mx = max(x)
    thr = max(mx * rel, 1.0)
    s, e = -1, -1
    for i, v in enumerate(x):
        if v > thr:
            if s < 0:
                s = i
            e = i + 1
    if s < 0:
        return 0, len(x)
    return s, e


def feat(x: list[float]) -> list[float]:
    """EnvMatcher.feat：log(≥floor) 后去均值。"""
    if not x:
        return []
    mx = max(x)
    floor = max(mx * 0.005, 1e-9)
    y = [math.log(max(v, floor)) for v in x]
    mean = sum(y) / len(y)
    return [v - mean for v in y]


def cc_at(L: list[float], R: list[float], lag: int) -> float:
    """EnvMatcher.ccAt：L[i] ↔ R[i+lag] 的归一化互相关。"""
    n, m = len(L), len(R)
    lo = max(0, lag)
    hi = min(m, lag + n)
    a, b = lo - lag, hi - lag
    if b - a < 2:
        return float("nan")
    k = b - a
    sL = sum(L[a:a + k])
    sR = sum(R[lo:lo + k])
    mL, mR = sL / k, sR / k
    vL = vR = cov = 0.0
    for i in range(k):
        dx = L[a + i] - mL
        dy = R[lo + i] - mR
        vL += dx * dx
        vR += dy * dy
        cov += dx * dy
    if vL <= 0 or vR <= 0:
        return float("nan")
    return cov / math.sqrt(vL * vR)


def match(rec: list[float], ref: list[float], center: int, half: int = 100):
    """EnvMatcher.match 的逐行复现。center < 0 ⇒ 不加先验（与 Java 一致）。"""
    a0, b0 = trim(rec, 0.01)
    r0, r1 = trim(ref, 0.005)
    if b0 - a0 < MIN_OVERLAP_FRAMES or r1 - r0 < MIN_OVERLAP_FRAMES:
        return dict(ok=False, why=f"有效帧太少(rec={b0-a0}, ref={r1-r0})", cc=0, quality=0,
                    s_ms=None, lag=None, a0=a0, r0=r0)
    L = feat(rec[a0:b0])
    R = feat(ref[r0:r1])
    n, m = len(L), len(R)
    min_overlap = max(MIN_OVERLAP_FRAMES, n // 4)
    lo, hi = -(n - min_overlap), m - min_overlap
    if center >= 0:
        c = center - (r0 - a0)
        w = max(1, half)
        lo = max(lo, c - w)
        hi = min(hi, c + w)
    if hi < lo:
        return dict(ok=False, why=f"先验窗越界(lo={lo}, hi={hi}, n={n}, m={m}, "
                                  f"min_overlap={min_overlap}, center={center}, r0-a0={r0-a0})",
                    cc=0, quality=0, s_ms=None, lag=None, a0=a0, r0=r0,
                    n=n, m=m, min_overlap=min_overlap)
    best, best_lag, best_i = -2.0, 0, -1
    ccs = []
    for lag in range(lo, hi + 1):
        cc = cc_at(L, R, lag)
        ccs.append(cc)
        if cc == cc and cc > best:
            best, best_lag, best_i = cc, lag, len(ccs) - 1
    if best_i < 0:
        return dict(ok=False, why="无有效重叠", cc=0, quality=0, s_ms=None, lag=None,
                    a0=a0, r0=r0, n=n, m=m, min_overlap=min_overlap)
    second = -1.0
    for k, cc in enumerate(ccs):
        if abs(k - best_i) <= GUARD_FRAMES:
            continue
        if cc == cc and cc > second:
            second = cc
    quality = best / second if second > 1e-6 else float("inf")
    out_lag = best_lag + r0 - a0
    ok = best >= 0.80 and quality >= 1.10
    return dict(ok=ok, why="ok" if ok else f"质量不足(cc={best:.3f}, 质量={quality:.3f})",
                cc=best, quality=quality, s_ms=-out_lag * 10.0, lag=out_lag,
                a0=a0, r0=r0, n=n, m=m, min_overlap=min_overlap)


def load_env(path: Path):
    """读 out/env_*.csv：头一行 `# env v=1 ...`，第二行是 u8 逗号串。"""
    lines = path.read_text(encoding="utf-8").splitlines()
    head = lines[0]
    meta = {}
    for tok in head.lstrip("# ").split():
        if "=" in tok:
            k, v = tok.split("=", 1)
            meta[k] = v
    q = [int(x) for x in lines[1].split(",") if x.strip()]
    return meta, [rms_of(v) for v in q]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("env_csv")
    ap.add_argument("--cut-start", type=int, default=None,
                    help="在 rec 里从第 N 帧开始截（模拟 audit 的截取）")
    ap.add_argument("--cut-len", type=int, default=3000)
    ap.add_argument("--center", default="correct", choices=["correct", "legacy", "none"])
    args = ap.parse_args()

    meta, rec_full = load_env(Path(args.env_csv))
    song = meta["song"]
    ff = int(meta["onset_frame"])
    sil_ref_ms = float(meta["sil_ref_ms"])

    man = json.loads(MANIFEST.read_text(encoding="utf-8"))
    entry = next(s for s in man["songs"] if s["key"] == song)
    ref = [rms_of(v) for v in base64.b64decode(entry["env_b64"])]

    start = 0 if args.cut_start is None else max(0, args.cut_start)
    rec = rec_full[start:start + args.cut_len] if args.cut_start is not None else rec_full
    ff_rel = ff - start

    sil_frames = round(sil_ref_ms / 10.0)
    if args.center == "correct":
        center = ff_rel - sil_frames
    elif args.center == "legacy":
        center = sil_frames - ff_rel
    else:
        center = -1

    print(f"曲 {song}  rec={len(rec)} 帧（全 {len(rec_full)}，start={start}）  ref={len(ref)} 帧  "
          f"ff={ff} → ff_rel={ff_rel}  sil_ref={sil_ref_ms:.0f} ms = {sil_frames} 帧")
    print(f"center({args.center}) = {center}")

    r = match(rec, ref, center)
    exp_s = ff_rel * 10.0 - sil_ref_ms
    print(f"\n结果：ok={r['ok']}  why={r['why']}")
    if r.get("n"):
        print(f"  n={r['n']}  m={r['m']}  min_overlap={r['min_overlap']}  a0={r['a0']}  r0={r['r0']}")
    if r["s_ms"] is not None:
        print(f"  s_ms={r['s_ms']:.1f}   期望 s_ms={exp_s:.1f}   Δ={r['s_ms']-exp_s:+.1f} ms")
        print(f"  cc={r['cc']:.3f}  quality={r['quality']:.2f}")
    head_s = meta.get("s")
    if head_s:
        print(f"  头里记录的 s={head_s} ⇒ 与本次 Δ={float(head_s)-(r['s_ms'] or 0):+.1f} ms")
    head_k = meta.get("k_meas") or meta.get("k_ms")
    if head_k and head_k != "none":
        print(f"  头里记录的 k={head_k}")
    return 0 if r["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
