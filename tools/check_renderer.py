#!/usr/bin/env python
"""跨语言核对：HTML 回放里 JS 算出的坐标 vs Python 的 `Chart.note_pos`。

为什么需要这一步：回放的渲染路径是浏览器里的 JS，而 M1/M2 的结论是 Python 算的。
两者是同一套公式的**两份独立实现**（一份 Python、一份 JS），任何一个写错都会
让「肉眼看到的画面」与「真正交付给 C 段的坐标」脱节。这里把它们逐点对拍。

做法：
  1. 让无头 Chrome 打开 `out/replay.html?probe=1&probe_step=N`，
     页面在探针模式下把整份 DOM 替换成只剩 <pre id="probe"> 的坐标数组；
  2. 抓取并解析该 JSON；
  3. 用 Python 侧对同样的 (音符, 时刻) 算一遍，逐点比对。

用法：
    python tools/check_renderer.py [--replay out/replay.html] [--step 25] [--tol 0.6]
"""

from __future__ import annotations

import argparse
import html
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pgauto.chart import DEFAULT_ASPECT_RATIO, Chart, load  # noqa: E402

DEFAULT_SAMPLE = PROJECT_ROOT / "samples" / "rrhar_at" / "Rrharil.TeamGrimoire.0.json"
CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

DT_LIST = [0.0, -0.2, 0.15, -0.6]


def find_chrome() -> Optional[str]:
    for c in CHROME_CANDIDATES:
        if Path(c).is_file():
            return c
    return None


def probe(chrome: str, replay: Path, step: int) -> List[List[float]]:
    url = f"file:///{replay.resolve().as_posix()}?probe=1&probe_step={step}"
    proc = subprocess.run(
        [chrome, "--headless=new", "--disable-gpu", "--no-sandbox", "--dump-dom", url],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )
    dom = proc.stdout
    m = re.search(r'<pre id="probe">(.*?)</pre>', dom, re.S)
    if not m:
        raise RuntimeError(
            f"探针输出未找到（stdout {len(dom)} 字符，stderr {proc.stderr[-500:]!r}）"
        )
    return json.loads(html.unescape(m.group(1)))


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="JS 渲染路径 vs Python 模型 逐点核对")
    ap.add_argument("--replay", default=str(PROJECT_ROOT / "out" / "replay.html"))
    ap.add_argument("--chart", default=str(DEFAULT_SAMPLE))
    ap.add_argument("--step", type=int, default=25, help="每 N 个音符抽一个（默认 25）")
    ap.add_argument("--aspect", type=float, default=DEFAULT_ASPECT_RATIO)
    ap.add_argument("--tol", type=float, default=0.6, help="允许的最大像素偏差")
    ap.add_argument("--chrome", default=None)
    args = ap.parse_args(argv)

    chrome = args.chrome or find_chrome()
    if not chrome:
        print("找不到 Chrome/Edge，跳过（该核对需要浏览器）", file=sys.stderr)
        return 3

    replay = Path(args.replay)
    if not replay.is_file():
        print(f"回放文件不存在：{replay}（先跑 tools/render_chart.py）", file=sys.stderr)
        return 2

    # 像素比对必须在**同一个标尺**上：以回放里 canvas 的尺寸为准。
    # 世界坐标与分辨率无关，但 px/py 是 1 世界单位 = 屏幕半宽，标尺不同会差一个倍数。
    src = replay.read_text(encoding="utf-8")
    m = re.search(r'<canvas id="cv" width="(\d+)" height="(\d+)"', src)
    if not m:
        print("无法从回放里读出 canvas 尺寸", file=sys.stderr)
        return 1
    cw, chh = float(m.group(1)), float(m.group(2))

    chart = Chart(load(Path(args.chart)), aspect_ratio=args.aspect, screen=(cw, chh))
    hits = chart.notes()

    print(f"浏览器   {chrome}")
    print(f"回放     {replay.name}")
    print(f"画布     {cw:.0f}x{chh:.0f}（像素比对用这个标尺）")
    print(f"抽样     每 {args.step} 个音符取 1 个，每个取 {len(DT_LIST)} 个时刻（含 t_hit）")
    rows = probe(chrome, replay, args.step)
    print(f"探针返回 {len(rows)} 条")

    expect_n = len(range(0, len(hits), args.step)) * len(DT_LIST)
    if len(rows) != expect_n:
        print(f"条数不符：期望 {expect_n}，实得 {len(rows)}", file=sys.stderr)
        return 1

    worst = (0.0, None)
    worst_w = (0.0, None)
    k = 0
    for i in range(0, len(hits), args.step):
        h = hits[i]
        for dt in DT_LIST:
            jl, jt, jtt, jwx, jwy, jpx, jpy, jdy = rows[k]
            k += 1
            wx, wy = chart.note_pos(h, h.t_sec + dt)
            px, py = chart.to_px(wx, wy)
            dp = max(abs(px - jpx), abs(py - jpy))
            dw = max(abs(wx - jwx), abs(wy - jwy))
            if dp > worst[0]:
                worst = (dp, (h.line, round(h.t_sec, 4), dt, (round(px, 3), round(py, 3)),
                              (round(jpx, 3), round(jpy, 3))))
            if dw > worst_w[0]:
                worst_w = (dw, (h.line, round(h.t_sec, 4), dt))
            if abs(h.line - jl) > 0 or abs(h.t_sec - jt) > 1e-6:
                print(f"对齐失败 @ {k-1}: PY(line={h.line},t={h.t_sec}) JS(line={jl},t={jt})",
                      file=sys.stderr)
                return 1

    print(f"最大世界坐标偏差 {worst_w[0]:.3e}（位置 {worst_w[1]}）")
    print(f"最大像素偏差     {worst[0]:.4f} px（位置 {worst[1]}）")
    ok = worst[0] <= args.tol
    print()
    print(f"{'PASS' if ok else 'FAIL'}：JS 渲染路径与 Python 模型一致（阈值 {args.tol} px）")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
