#!/usr/bin/env python
"""C 段可视化：指针调度甘特图（自包含单文件 HTML）。

每根指针一条泳道，横轴时间，每个音符一块（按类型着色）。
滚轮缩放、拖拽平移，块上悬停看详情。

用法：
    python tools/render_touches.py out/notes.json -o out/schedule.html
                                   [--max-pointers 10]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional, Sequence, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pgauto import schedule as sch  # noqa: E402

LANE_H = 34          # 每条泳道高度（SVG 单位）
TOP_PAD = 6
BOTTOM_PAD = 6      # SVG 底部留边（时间刻度已移出 SVG，见 .axis）
LABEL_W = 52         # 左侧指针标签列宽（HTML px）
PX_PER_MS = 0.04     # 1ms -> 0.04 SVG 单位 ⇒ 40 单位/秒
TYPE_COLORS = {1: "#4a9eff", 2: "#35c46a", 3: "#ff9f43", 4: "#b26bff"}


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="生成指针调度甘特图")
    ap.add_argument("notes", help="notes.json 路径")
    ap.add_argument("-o", "--out", default=str(PROJECT_ROOT / "out" / "schedule.html"))
    ap.add_argument("--max-pointers", type=int, default=sch.MAX_POINTERS)
    ap.add_argument("--tangent-threshold", type=float,
                    default=sch.TANGENT_THRESHOLD_RATIO)
    ap.add_argument("--no-retime-flick", action="store_true",
                    help="关掉 Flick 的判定时间微调（判定线闪烁兜底）")
    ap.add_argument("--clamp-offscreen", action="store_true",
                    help="把屏外判定点拉到屏幕边缘（事件流完整，但不保证命中）")
    return ap


HEAD = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Phigros 指针调度甘特图</title>
<style>
  :root {
    --bg: #12151c; --panel: #1a1f29; --line: #2a3140;
    --fg: #e6ebf2; --dim: #8b96a8; --accent: #4a9eff;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 22px 24px 40px;
    background: var(--bg); color: var(--fg);
    font: 13px/1.55 "Segoe UI", "Microsoft YaHei", system-ui, sans-serif;
  }
  h1 { margin: 0 0 4px; font-size: 17px; font-weight: 600; }
  .sub { color: var(--dim); margin-bottom: 16px; font-size: 12px; }
  .stats { display: flex; flex-wrap: wrap; gap: 10px; margin-bottom: 16px; }
  .stat {
    background: var(--panel); border: 1px solid var(--line); border-radius: 8px;
    padding: 7px 13px; min-width: 92px;
  }
  .stat b { display: block; font-size: 18px; font-weight: 600; line-height: 1.2; }
  .stat span { color: var(--dim); font-size: 11px; }
  .chart { display: flex; align-items: flex-start; }
  .lanes { flex: 0 0 auto; width: %(label_w)dpx; padding-top: %(top_pad)dpx; }
  .lane-label {
    height: %(lane_h)dpx; display: flex; align-items: center;
    color: var(--dim); font-size: 11px; font-variant-numeric: tabular-nums;
  }
  .lane-label em { font-style: normal; color: var(--fg); margin-right: 4px; }
  .lane-label i { font-style: normal; font-size: 10px; opacity: .65; }
  .right { flex: 1 1 auto; position: relative; min-width: 0; }
  svg { display: block; width: 100%%; touch-action: none; cursor: grab; }
  svg.drag { cursor: grabbing; }
  /* 时间刻度放在 SVG 外面：preserveAspectRatio="none" 会把 SVG 内的文字横向压扁 */
  .axis { position: relative; height: 15px; margin-top: 3px; color: var(--dim); font-size: 10.5px; }
  .axis span {
    position: absolute; top: 0; transform: translateX(-50%%);
    white-space: nowrap; font-variant-numeric: tabular-nums;
  }
  .legend { display: flex; gap: 18px; margin-top: 14px; color: var(--dim); font-size: 12px; }
  .legend i { display: inline-block; width: 11px; height: 11px; border-radius: 2px; margin-right: 6px; vertical-align: -1px; }
  .hint { margin-top: 12px; color: var(--dim); font-size: 11.5px; }
  #tip {
    position: fixed; pointer-events: none; display: none; z-index: 9;
    background: #0b0e14; border: 1px solid var(--line); border-radius: 6px;
    padding: 7px 10px; font-size: 11.5px; line-height: 1.6; white-space: nowrap;
    box-shadow: 0 6px 22px rgba(0,0,0,.5);
  }
  #tip b { color: var(--accent); }
  .lane-bg { fill: %(lane_even)s; }
  .lane-bg.odd { fill: %(lane_odd)s; }
  .grid { stroke: var(--line); stroke-width: 1; vector-effect: non-scaling-stroke; }
  .grid.minor { stroke: #202634; }
  .tick { fill: var(--dim); font-size: 10px; }
  .task { cursor: pointer; }
  .task:hover { stroke: #fff; stroke-width: 1; vector-effect: non-scaling-stroke; }
</style>
</head>
<body>
<h1>Phigros 指针调度甘特图</h1>
<div class="sub" id="sub"></div>
<div class="stats" id="stats"></div>
<div class="chart">
  <div class="lanes" id="lanes"></div>
  <div class="right">
    <svg id="gantt" preserveAspectRatio="none"></svg>
    <div class="axis" id="axis"></div>
  </div>
</div>
<div class="legend" id="legend"></div>
<div class="hint">滚轮缩放 · 按住拖动平移 · 悬停查看单个音符 · 双击复位</div>
<div id="tip"></div>
"""

TAIL = """<script>
const D = %(data)s;
const LANE_H = %(lane_h)d, TOP = %(top_pad)d, PAD_B = %(bottom_pad)d;
const PPM = %(ppm)s;
const COLORS = %(colors)s;
const NAMES = {1: "Tap", 2: "Drag", 3: "Hold", 4: "Flick"};
const TOTAL = D.total_ms * PPM;
const H = TOP + D.pointers * LANE_H + PAD_B;

const svg = document.getElementById("gantt");
svg.setAttribute("viewBox", `0 0 ${TOTAL} ${H}`);
svg.style.height = H + "px";

// ---- 泳道底纹 + 网格 + 刻度 ----
const NS = "http://www.w3.org/2000/svg";
function el(tag, attrs) {
  const n = document.createElementNS(NS, tag);
  for (const k in attrs) n.setAttribute(k, attrs[k]);
  return n;
}
for (let i = 0; i < D.pointers; i++) {
  const y = TOP + i * LANE_H;
  const bg = el("rect", { x: 0, y: y, width: TOTAL, height: LANE_H, class: "lane-bg" + (i %% 2 ? " odd" : "") });
  svg.appendChild(bg);
}
const secStep = 5;
for (let s = 0; s * 1000 <= D.total_ms; s += secStep) {
  const x = s * 1000 * PPM;
  const major = s %% 10 === 0;
  svg.appendChild(el("line", {
    x1: x, y1: TOP, x2: x, y2: TOP + D.pointers * LANE_H,
    class: "grid" + (major ? "" : " minor"),
  }));
}

// ---- 任务块 ----
const tip = document.getElementById("tip");
for (const [start, end, ptr, type, px, py, line] of D.tasks) {
  const x = start * PPM;
  const w = Math.max((end - start) * PPM, 0.9);
  const r = el("rect", {
    x: x.toFixed(2), y: (TOP + ptr * LANE_H + 2).toFixed(2),
    width: w.toFixed(2), height: LANE_H - 4,
    fill: COLORS[type] || "#888", class: "task", rx: 1,
  });
  r.addEventListener("mousemove", (ev) => {
    tip.style.display = "block";
    tip.style.left = (ev.clientX + 14) + "px";
    tip.style.top = (ev.clientY + 14) + "px";
    tip.innerHTML = `<b>${NAMES[type] || type}</b> · 指针 ${ptr}<br>`
      + `时刻 ${(start / 1000).toFixed(3)}s → ${(end / 1000).toFixed(3)}s`
      + `（${(end - start).toFixed(1)}ms）<br>`
      + `画布坐标 (${px.toFixed(1)}, ${py.toFixed(1)}) · 判定线 ${line}`;
  });
  r.addEventListener("mouseleave", () => { tip.style.display = "none"; });
  svg.appendChild(r);
}

// ---- 时间刻度：用 HTML 覆盖层画，避开 SVG 内的横向压扁 ----
const axis = document.getElementById("axis");
function renderAxis() {
  const t0 = vx / PPM / 1000;
  const span = vw / PPM / 1000;
  const steps = [0.25, 0.5, 1, 2, 5, 10, 20, 30, 60, 120];
  const step = steps.find((s) => span / s <= 14) || 300;
  let html = "";
  for (let s = Math.ceil(t0 / step) * step; s <= t0 + span + 1e-9; s += step) {
    const pct = ((s - t0) / span) * 100;
    const label = Math.abs(s - Math.round(s)) < 1e-6 ? Math.round(s) + "s" : s.toFixed(2) + "s";
    html += `<span style="left:${pct.toFixed(3)}%%">${label}</span>`;
  }
  axis.innerHTML = html;
}

// ---- 缩放 / 平移 ----
let vx = 0, vw = TOTAL;
function apply() {
  vw = Math.min(Math.max(vw, 12), TOTAL);
  vx = Math.min(Math.max(vx, 0), TOTAL - vw);
  svg.setAttribute("viewBox", `${vx} 0 ${vw} ${H}`);
  renderAxis();
}
svg.addEventListener("wheel", (e) => {
  e.preventDefault();
  const rect = svg.getBoundingClientRect();
  const ratio = (e.clientX - rect.left) / rect.width;
  const anchor = vx + ratio * vw;
  const f = e.deltaY > 0 ? 1.16 : 1 / 1.16;
  vw *= f;
  vx = anchor - ratio * vw;
  apply();
}, { passive: false });

let dragging = false, lastX = 0;
svg.addEventListener("mousedown", (e) => {
  dragging = true; lastX = e.clientX; svg.classList.add("drag");
});
window.addEventListener("mousemove", (e) => {
  if (!dragging) return;
  const rect = svg.getBoundingClientRect();
  vx -= (e.clientX - lastX) / rect.width * vw;
  lastX = e.clientX;
  apply();
});
window.addEventListener("mouseup", () => {
  dragging = false; svg.classList.remove("drag");
});
svg.addEventListener("dblclick", () => { vx = 0; vw = TOTAL; apply(); });
apply();
</script>
</body>
</html>
"""


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    path = Path(args.notes)
    if not path.is_file():
        print(f"notes.json 不存在：{path}", file=sys.stderr)
        return 2

    data = sch.load_notes(path)
    result = sch.build(
        data.notes,
        geometry=data.geometry,
        canvas=data.canvas,
        max_pointers=args.max_pointers,
        tangent_threshold_ratio=args.tangent_threshold,
        flick_retime=not args.no_retime_flick,
        clamp_offscreen=args.clamp_offscreen,
    )

    # 泳道画的是**音符**（一条起手服务 + 它占用的时长），不是逐毫秒的服务记录 ——
    # 后者在长按多的谱面上有几万条，画出来没有可读性。
    # 注意：这个局部集合不能叫 HEAD —— 会遮蔽模块级的 HEAD（HTML 模板字符串）。
    HEAD_STAGES = {"tap", "hold_head", "drag", "flick"}
    total_ms = max((e.t_ms for e in result.events), default=1)
    entries = []
    for s in result.serves:
        if s.stage not in HEAD_STAGES:
            continue
        if s.stage == "hold_head":
            dur = data.notes[s.note_index].hold_ms
        elif s.stage == "flick":
            dur = sch.FLICK_SPAN_MS
        else:
            dur = 0.0
        entries.append([s.t_ms, s.t_ms + dur, s.pointer, s.note_type,
                        s.px, s.py, data.notes[s.note_index].line])
    entries.sort(key=lambda e: (e[2], e[0]))

    per_ptr: dict = {}
    for s in result.serves:
        per_ptr[s.pointer] = per_ptr.get(s.pointer, 0) + 1

    payload = {
        "chart": data.head.get("chart", path.name),
        "total_ms": total_ms,
        "pointers": result.pointer_count,
        "peak": result.stats.get("peak_concurrency", 0),
        "note_count": len(data.notes),
        "event_count": len(result.events),
        "per_pointer": [per_ptr.get(i, 0) for i in range(result.pointer_count)],
        "tasks": entries,
    }

    lane_h = LANE_H
    head_html = HEAD % {
        "label_w": LABEL_W,
        "top_pad": TOP_PAD,
        "lane_h": lane_h,
        "lane_even": "#171c25",
        "lane_odd": "#1c2230",
    }
    tail_html = TAIL % {
        "data": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        "lane_h": lane_h,
        "top_pad": TOP_PAD,
        "bottom_pad": BOTTOM_PAD,
        "ppm": PX_PER_MS,
        "colors": json.dumps({str(k): v for k, v in TYPE_COLORS.items()}),
    }

    # 左标签列 + 统计卡片由 Python 生成，省掉一段 JS
    lanes = "".join(
        f'<div class="lane-label"><em>P{i}</em><i>{per_ptr.get(i, 0)}</i></div>'
        for i in range(result.pointer_count)
    )
    stats = "".join(
        f'<div class="stat"><b>{v}</b><span>{k}</span></div>'
        for k, v in [
            ("指针数", f"{result.pointer_count} / {args.max_pointers}"),
            ("峰值并发", payload["peak"]),
            ("音符", payload["note_count"]),
            ("触摸事件", payload["event_count"]),
            ("时长", f"{total_ms / 1000:.1f}s"),
            ("复用率", f"{100.0 * result.stats.get('reuse', 0) / max(1, result.stats.get('serves', 1)):.1f}%"),
        ]
    )
    legend = "".join(
        f'<span><i style="background:{TYPE_COLORS[t]}"></i>{n}</span>'
        for t, n in [(1, "Tap"), (2, "Drag"), (3, "Hold"), (4, "Flick")]
    )
    offscreen_bits = [
        f"时间微调 {result.stats.get('retimed', 0)}",
        f"法线投影 {result.stats.get('projected_into_screen', 0)}",
        f"拉进屏幕 {result.stats.get('clamped', 0)}",
        f"长按持续帧出屏 {result.stats.get('hold_keep_clamped', 0)}",
    ]
    sub = (
        f'{payload["chart"]} · 几何 {result.geometry} · 每指针服务次数 {payload["per_pointer"]}'
        f' · 未分配 {len(result.unassigned)} 个'
        f' · 屏外处理 {" / ".join(offscreen_bits)}'
    )

    doc = (
        head_html
        .replace('<div class="sub" id="sub"></div>', f'<div class="sub" id="sub">{sub}</div>')
        .replace('<div class="stats" id="stats"></div>', f'<div class="stats" id="stats">{stats}</div>')
        .replace('<div class="lanes" id="lanes"></div>', f'<div class="lanes" id="lanes">{lanes}</div>')
        .replace('<div class="legend" id="legend"></div>', f'<div class="legend" id="legend">{legend}</div>')
    ) + tail_html

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(doc, encoding="utf-8")
    size_kb = out.stat().st_size / 1024
    print(f"写入 {out}（{size_kb:.0f} KB）")
    print(f"  指针 {result.pointer_count} / 峰值并发 {payload['peak']} / 音符块 {len(entries)}")
    if result.unassigned:
        print(f"  ⚠️ 未分配 {len(result.unassigned)} 个")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
