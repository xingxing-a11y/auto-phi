# -*- coding: utf-8 -*-
"""从录屏里抽 combo 数字的「存在 / 宽度」时间序列（不用 OCR，不用固定阈值）。

为什么要重写 `_combo_drop.py`
------------------------------
那份用 THR=235 + 固定 crop，在 burn 上失守：p95 宽度 = 140 = 满格（背景亮部），
524 帧被判成「有字」。原因是它只看水平跨度，不看**竖直高度**。
数字高约 40 px，"COMBO" 小字高约 8 px，背景亮部往往整片 ⇒ 用高度能一刀切开。

判据
----
1. crop 数字带（x 740..880，y 18..64），阈值 > 200（数字近纯白）；
2. 每行统计亮像素数，取连续「≥3 个亮像素」的行区间；
3. 该区间高度 ∈ [25, 50] 且宽度 ≤ 120 ⇒ **判为有数字**，宽度即数字跨度。

用法：python tools/combo_live.py burn [--fps 60] → tmp/_combo_live_burn.json
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import imageio_ffmpeg
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
FF = imageio_ffmpeg.get_ffmpeg_exe()
CROP = (740, 14, 140, 66)      # x, y, w, h
# ★ 数字块 y 19..53（高 35）、"COMBO" 小字 y 63..74（高 12），中间 y 54..62 是空行
#   ⇒ 必须取「第一段连续亮行」且高度 25~45 才算数字；只看整体跨度会把小字算进去，
#   于是单数字的 combo（宽 ~20）被小字宽度（~56）盖住，宽度序列永远掉不下来。
THR = 200
H_MIN, H_MAX = 25, 50


def measure(a: np.ndarray):
    m = a > THR
    rows = m.sum(axis=1)
    q = rows >= 3
    i = 0
    while i < len(q):
        if not q[i]:
            i += 1
            continue
        j = i
        while j + 1 < len(q) and q[j + 1]:
            j += 1
        h = j - i + 1
        if H_MIN <= h <= H_MAX:                 # 第一段够高的连续亮行 = 数字块
            sub = m[i:j + 1]
            cols = np.where(sub.any(axis=0))[0]
            w = int(cols[-1] - cols[0] + 1) if cols.size else 0
            return 1, w, h, i, int(sub.sum())
        i = j + 1
    return 0, 0, 0, 0, 0


def run(tag: str, fps: float):
    vid = ROOT / f"tmp/full_{tag}.mp4"
    x, y, w, h = CROP
    cmd = [FF, "-hide_banner", "-loglevel", "error", "-i", str(vid),
           "-vf", f"fps={fps:g},crop={w}:{h}:{x}:{y},format=gray",
           "-f", "rawvideo", "-"]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    n = w * h
    rows = []
    i = 0
    while True:
        buf = p.stdout.read(n)
        if len(buf) < n:
            break
        ok, ww, hh, y0, area = measure(np.frombuffer(buf, np.uint8).reshape(h, w))
        rows.append([round(i / fps, 3), ok, ww, hh, y0, area])
        i += 1
    p.stdout.close()
    p.wait()
    out = {"tag": tag, "fps": fps, "crop": CROP, "thr": THR, "rows": rows}
    (ROOT / f"tmp/_combo_live_{tag}.json").write_text(json.dumps(out, ensure_ascii=False),
                                                      encoding="utf-8")
    ok = np.array([r[1] for r in rows])
    print(f"{tag}: 帧 {len(rows)}  有字帧 {int(ok.sum())} ({ok.mean()*100:.1f}%)")
    print(f"  宽度分位 p5/p50/p95 = {np.percentile([r[2] for r in rows if r[1]],5):.0f}/"
          f"{np.percentile([r[2] for r in rows if r[1]],50):.0f}/"
          f"{np.percentile([r[2] for r in rows if r[1]],95):.0f}")
    print("→", f"tmp/_combo_live_{tag}.json")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("tag")
    ap.add_argument("--fps", type=float, default=60.0)
    a = ap.parse_args()
    sys.exit(run(a.tag, a.fps))
