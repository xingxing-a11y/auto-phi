#!/usr/bin/env python
"""谱面 → 自包含 HTML 回放（M2 的可视化交付物）。

用法：
    python tools/render_chart.py <chart.json> [-o out/replay.html]
                                [--screen 1920x1080] [--aspect 1.7777778]
                                [--line-length 1.5] [--no-simplify]

产物是**单文件** HTML：数据内联、脚本内联，无外部依赖，双击即看。
浏览器里的 JS 与 Python 端用同一套公式（X_UNIT / HEIGHT_RATIO / aspect 一并内联），
所以画面就是 `Chart.note_pos` 的逐帧展开——用于肉眼核对轨迹是否合理。

截图（无头 Chrome）：
    chrome --headless=new --disable-gpu --screenshot=out.png \
           --window-size=1600,1000 --virtual-time-budget=4000 \
           "file:///.../replay.html?t=12.5&still=1"

⚠️ 与计划的一处偏离：计划写「输入 notes.json」，实际直接吃谱面 JSON。
   原因：`notes.json` 只有击打点，而动画需要判定线的 move/rotate/alpha/speed 四条曲线，
   这些在谱面里而不在击打点清单里。相反方向是通的——`notes.json` 的每个击打点
   都能在本回放里作为「判定线上的准星」画出来（默认打开 `准星` 开关）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pgauto.chart import (  # noqa: E402
    DEFAULT_ASPECT_RATIO,
    DEFAULT_SCREEN,
    HEIGHT_RATIO,
    X_UNIT,
    Chart,
    load,
)

CANVAS_W, CANVAS_H = 1600, 900  # 16:9，与 aspect 一致；CSS 再等比缩放

DEFAULT_SAMPLE = PROJECT_ROOT / "samples" / "rrhar_at" / "Rrharil.TeamGrimoire.0.json"

# 时间**一律不舍入**，理由（两次实测踩坑）：
#
# 1. 官谱里音符时刻与事件时刻经常逐位相同（同一拍 × 同一个 r 算出来的）。
#    若两者的舍入位数不同，重合就会消失：JS 在那一瞬会取到台阶前的帧而不是台阶后的帧。
#    line 0 的 t=23.9211 就是这样，rotate 差 80°、高度差 0.067（≈ 324 px）。
# 2. 即使两者用同一个位数，舍入本身也会把台阶挪动最多 5e-10 s。line 14 的 rotate
#    在 t=13.894736842105262 有一个 -10°→0° 的台阶，而该时刻正是某个音符的击打时刻；
#    舍入到 9 位后台阶前移 0.26 ns，「在舍入后的时刻求值」就会落到另一侧（差 10°）。
#
# 取值可以舍入（nd 位）：那只会带来 ~1e-6 的连续误差，不会翻转任何不连续点。
# 代价是 JSON 里的时间字符串变长（本样本约 +25% 体积），换来的是可精确断言的等价性。


# ------------------------------------------------------------------ 数据打包

def simplify(kfs: Sequence[Tuple[float, float]], eps: float = 1e-9) -> List[Tuple[float, float]]:
    """丢掉落在相邻两帧连线上的中间帧（曲线是分段线性的，故值不变）。

    同一时刻的重复帧是「瞬变」（方波），必须保留，否则会把跳变抹成斜坡。
    """
    if len(kfs) <= 2:
        return list(kfs)
    out = [kfs[0]]
    for i in range(1, len(kfs) - 1):
        t1, v1 = kfs[i]
        t2, v2 = kfs[i + 1]
        t0, v0 = out[-1]
        if t1 == t0 or t1 == t2:
            out.append(kfs[i])
            continue
        pred = v0 + (v2 - v0) * (t1 - t0) / (t2 - t0)
        if abs(pred - v1) > eps:
            out.append(kfs[i])
    out.append(kfs[-1])
    return out


def flat(kfs: Sequence[Tuple[float, float]], do_simplify: bool, nd: int) -> List[float]:
    """关键帧 → 扁平数组 [t0, v0, t1, v1, ...]。

    时间保持原值（见模块顶部关于「时间不舍入」的说明），取值舍入到 nd 位。
    JSON 用 `repr` 序列化 float，十进制是**可往返**的，故 JS 解析回来是同一个 double。
    """
    seq = simplify(kfs) if do_simplify else list(kfs)
    out: List[float] = []
    for t, v in seq:
        out.append(float(t))
        out.append(round(float(v), nd))
    return out


def build_payload(
    chart: Chart, chart_name: str, line_half_len: float, do_simplify: bool, nd: int
) -> Dict[str, Any]:
    hits = chart.notes()

    lines = []
    for line in chart.lines:
        lines.append(
            {
                "id": line.id,
                "x": flat(zip_times(line.move.x), do_simplify, nd),
                "y": flat(zip_times(line.move.y), do_simplify, nd),
                "r": flat(zip_times(line.rotate), do_simplify, nd),
                "a": flat(zip_times(line.alpha), do_simplify, nd),
                "h": flat(zip_times(line.height), do_simplify, nd),
            }
        )

    notes = []
    for h in hits:
        speed = 1.0 if h.type == 3 else h.speed
        end_h = (
            chart.line_height(h.line, h.t_sec + h.hold_sec) if h.hold_sec > 0 else h.note_h
        )
        notes.append(
            {
                "l": h.line,
                "u": 1 if h.above else 0,
                "ty": h.type,
                "bx": round(h.position_x * X_UNIT, nd),
                "nh": round(h.note_h, nd),
                "ne": round(end_h, nd),
                "sp": round(speed, nd),
                "t": h.t_sec,
                "hl": round(h.hold_sec, nd),
            }
        )

    return {
        "meta": {
            "chart": chart_name,
            "formatVersion": chart.format_version,
            "offset": chart.offset,
            "aspect": chart.aspect_ratio,
            "heightRatio": HEIGHT_RATIO,
            "xUnit": X_UNIT,
            "screen": [chart.screen_w, chart.screen_h],
            "canvas": [CANVAS_W, CANVAS_H],
            "duration": chart.max_time,
            "lineHalfLen": line_half_len,
            "noteHalfSize": 0.13175016,  # Phira NOTE_WIDTH_RATIO_BASE
        },
        "lines": lines,
        "notes": notes,
    }


def zip_times(anim) -> List[Tuple[float, float]]:
    """AnimFloat → 关键帧列表（保留同刻多帧的顺序）。"""
    return list(zip(anim._times, anim._values))


# ------------------------------------------------------------------ HTML 模板

HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  html, body { margin: 0; height: 100%; background: #0b0d12; }
  body {
    font: 13px/1.5 "Segoe UI", "Microsoft YaHei", system-ui, sans-serif;
    color: #e6e9f0; display: flex; flex-direction: column;
  }
  #stage { flex: 1; min-height: 0; display: grid; place-items: center; padding: 8px; }
  canvas { max-width: 100%; max-height: 100%; border-radius: 6px;
           background: #05060a; box-shadow: 0 0 0 1px #1c2130, 0 12px 32px #0009; }
  #bar { display: flex; gap: 10px; align-items: center; padding: 8px 12px;
         border-top: 1px solid #1c2130; background: #0f131b; flex-wrap: wrap; }
  button { font: inherit; color: inherit; background: #1b2230; border: 1px solid #2b3549;
           border-radius: 6px; padding: 4px 12px; cursor: pointer; }
  button:hover { background: #24304a; }
  input[type=range] { flex: 1; min-width: 180px; accent-color: #45c8ff; }
  label { display: inline-flex; gap: 5px; align-items: center; user-select: none; cursor: pointer; }
  .mono { font-family: ui-monospace, Consolas, monospace; }
  .dim { color: #8b96ab; }
  #badge { position: fixed; left: 14px; top: 12px; pointer-events: none;
           font-size: 12px; color: #cfd6e4; text-shadow: 0 1px 3px #000; }
  .still #bar, .still #badge { display: none; }
</style>
</head>
<body>
<div id="stage"><canvas id="cv" width="__CW__" height="__CH__"></canvas></div>
<div id="badge" class="mono"></div>
<div id="bar">
  <button id="play">▶ 播放</button>
  <input id="seek" type="range" min="0" max="1000" value="0" step="1">
  <span id="tl" class="mono dim">0.000 / 0.000 s</span>
  <label><input id="marks" type="checkbox" checked> 击打点准星</label>
  <label><input id="trails" type="checkbox"> 轨迹</label>
  <label>速度
    <select id="rate">
      <option value="0.25">0.25×</option>
      <option value="0.5">0.5×</option>
      <option value="1" selected>1×</option>
      <option value="2">2×</option>
    </select>
  </label>
</div>
<script id="chart-data" type="application/json">__DATA__</script>
<script>
"use strict";
const D = JSON.parse(document.getElementById("chart-data").textContent);
const M = D.meta, LINES = D.lines, NOTES = D.notes;
const W = M.canvas[0], H = M.canvas[1];

// 世界坐标 → 画布像素：1 世界单位 = 画布半宽，各向同性（与 Python 侧一致）
const PX = wx => W / 2 * (1 + wx);
const PY = wy => H / 2 - (W / 2) * wy;

// 关键帧扁平数组 [t0,v0,t1,v1,...] 求值：取最后一个 time <= t 的帧，段内线性插值
function at(f, t) {
  const n = f.length >> 1;
  if (n === 0) return 0;
  if (t <= f[0]) return f[1];
  if (t >= f[2 * (n - 1)]) return f[2 * (n - 1) + 1];
  let lo = 0, hi = n - 1;
  while (lo < hi - 1) {
    const mid = (lo + hi) >> 1;
    if (f[2 * mid] <= t) lo = mid; else hi = mid;
  }
  const t0 = f[2 * lo], v0 = f[2 * lo + 1], t1 = f[2 * hi], v1 = f[2 * hi + 1];
  if (t1 === t0) return v1;
  return v0 + (v1 - v0) * (t - t0) / (t1 - t0);
}

const NOTE_COLORS = {
  1: ["#45c8ff", "#0f3d55"],   // Tap
  2: ["#ffd54f", "#4a3c0d"],   // Drag
  3: ["#5b8cff", "#132550"],   // Hold
  4: ["#ff4d6d", "#4d1220"],   // Flick
};
const FLASH = 0.16;      // 判中后淡出时长（对齐 Phira FADEOUT_TIME）
const CULL = 3.0;        // |dy| 超过这个值就不画（远在屏幕外）

const cv = document.getElementById("cv");
const ctx = cv.getContext("2d");
let t = 0, playing = true, rate = 1, lastFrame = 0;
let showMarks = true, showTrails = false;
let peakNotes = 0;

const qs = new URLSearchParams(location.search);
if (qs.has("t")) { t = parseFloat(qs.get("t")) || 0; }
if (qs.get("still") === "1") { playing = false; document.body.classList.add("still"); }
else if (qs.get("still") === "0") { playing = true; }

const els = {
  play: document.getElementById("play"), seek: document.getElementById("seek"),
  tl: document.getElementById("tl"), badge: document.getElementById("badge"),
  marks: document.getElementById("marks"), trails: document.getElementById("trails"),
  rate: document.getElementById("rate"),
};
els.seek.max = String(Math.round(M.duration * 1000));
els.marks.checked = showMarks;
els.trails.checked = showTrails;
els.play.onclick = () => { playing = !playing; els.play.textContent = playing ? "▶ 播放" : "⏸ 暂停"; };
els.seek.oninput = () => { t = parseFloat(els.seek.value) / 1000; playing = false; els.play.textContent = "▶ 播放"; };
els.marks.onchange = () => { showMarks = els.marks.checked; };
els.trails.onchange = () => { showTrails = els.trails.checked; };
els.rate.onchange = () => { rate = parseFloat(els.rate.value); };
window.addEventListener("keydown", e => {
  if (e.code === "Space") { e.preventDefault(); els.play.onclick(); }
});

// 音符世界坐标：唯一一处轨迹公式，drawNote 与自检探针共用
// 返回 [worldX, worldY, lineX, lineY, angleDeg, dy]
function noteWorld(n, t) {
  const ln = LINES[n.l];
  const lx = at(ln.x, t), ly = at(ln.y, t) / M.aspect, ang = at(ln.r, t);
  const rad = ang * Math.PI / 180, ca = Math.cos(rad), sa = Math.sin(rad);
  const dyRaw = (n.nh - at(ln.h, t)) / (M.heightRatio * M.aspect) * n.sp;
  const dy = n.u ? dyRaw : -dyRaw;
  return [lx + n.bx * ca - dy * sa, ly + n.bx * sa + dy * ca, lx, ly, ang, dy];
}

function drawNote(n, t, hitAlpha) {
  const w = noteWorld(n, t);
  const dy = w[5];
  if (Math.abs(dy) > CULL) return 0;
  const cx = PX(w[0]), cy = PY(w[1]);
  if (cx < -300 || cx > W + 300 || cy < -300 || cy > H + 300) return 0;

  const ln = LINES[n.l];
  const ang = w[4] * Math.PI / 180;
  const col = NOTE_COLORS[n.ty] || NOTE_COLORS[1];
  const hs = M.noteHalfSize * W / 2;

  ctx.save();
  ctx.translate(cx, cy);
  ctx.rotate(-ang);          // 画布 y 向下，故取负以与世界的逆时针一致
  ctx.globalAlpha = hitAlpha;

  if (n.ty === 3 && n.hl > 0) {
    // Hold 尾巴：从头部到尾部，尾巴在图上位置按 (ne - h(t)) 算
    const dyEndRaw = (n.ne - at(ln.h, t)) / (M.heightRatio * M.aspect) * n.sp;
    const tail = n.u ? dyEndRaw : -dyEndRaw;
    let head = dy;
    if (t > n.t) head = 0;                       // 头部已贴线
    if (t < n.t + n.hl) {
      const span = (tail - head) * W / 2;
      ctx.fillStyle = col[0];
      ctx.globalAlpha = hitAlpha * 0.55;
      ctx.fillRect(-hs * 0.72, Math.min(0, -span), hs * 1.44, Math.abs(span));
      ctx.globalAlpha = hitAlpha;
    }
  }

  const r = n.ty === 4 ? hs * 1.12 : hs;         // Flick 略大
  ctx.beginPath();
  if (n.ty === 2) {                               // Drag：菱形
    ctx.moveTo(0, -r); ctx.lineTo(r * 0.78, 0); ctx.lineTo(0, r); ctx.lineTo(-r * 0.78, 0);
    ctx.closePath();
  } else {
    const rr = n.ty === 4 ? r * 0.35 : r * 0.42;
    if (ctx.roundRect) { ctx.roundRect(-r, -r, r * 2, r * 2, rr); }
    else { ctx.rect(-r, -r, r * 2, r * 2); }
  }
  ctx.fillStyle = col[1];
  ctx.fill();
  ctx.lineWidth = r * 0.34;
  ctx.strokeStyle = col[0];
  ctx.stroke();
  ctx.lineWidth = Math.max(1, r * 0.09);
  ctx.strokeStyle = "rgba(255,255,255,0.55)";
  ctx.stroke();
  ctx.restore();
  return 1;
}

function frame(dt) {
  ctx.fillStyle = "#05060a";
  ctx.fillRect(0, 0, W, H);

  let drawn = 0;
  const L = M.lineHalfLen;

  // 判定线（先画线，音符压在上面）
  for (const ln of LINES) {
    const alpha = at(ln.a, t);
    if (alpha <= 0.001) continue;               // 隐形判定线：只画音符，不画线
    const lx = at(ln.x, t), ly = at(ln.y, t) / M.aspect, ang = at(ln.r, t) * Math.PI / 180;
    const ca = Math.cos(ang), sa = Math.sin(ang);
    ctx.beginPath();
    ctx.moveTo(PX(lx - L * ca), PY(ly - L * sa));
    ctx.lineTo(PX(lx + L * ca), PY(ly + L * sa));
    ctx.strokeStyle = "rgba(255,255,255," + Math.min(1, alpha) + ")";
    ctx.lineWidth = 0.01 * W / 2;               // 对齐 Phira draw_line 的 0.01 世界单位
    ctx.lineCap = "round";
    ctx.stroke();
  }

  // 击打点准星（独立于 note_pos 的核对锚点）
  if (showMarks) {
    ctx.strokeStyle = "rgba(120,255,180,0.85)";
    ctx.lineWidth = 2;
    for (const n of NOTES) {
      if (Math.abs(n.t - t) > 0.35) continue;
      const ln = LINES[n.l];
      const lx = at(ln.x, n.t), ly = at(ln.y, n.t) / M.aspect, ang = at(ln.r, n.t) * Math.PI / 180;
      const wx = lx + n.bx * Math.cos(ang), wy = ly + n.bx * Math.sin(ang);
      const px = PX(wx), py = PY(wy), k = 9;
      ctx.beginPath();
      ctx.moveTo(px - k, py); ctx.lineTo(px + k, py);
      ctx.moveTo(px, py - k); ctx.lineTo(px, py + k);
      ctx.stroke();
    }
  }

  // 音符
  for (const n of NOTES) {
    let a = 1;
    if (t > n.t) {
      if (n.ty !== 3 && t > n.t + FLASH) continue;   // 判中即剔除（Phira note.rs::render 开头 return）
      a = Math.max(0, 1 - (t - n.t) / FLASH);
      if (n.ty === 3 && t > n.t + n.hl) continue;
    } else if (t < n.t - 6) {
      continue;                                       // 还太早，省算力
    }
    drawn += drawNote(n, t, a);
  }
  if (drawn > peakNotes) peakNotes = drawn;

  els.badge.textContent =
    M.chart + "  ·  " + t.toFixed(3) + " s  ·  屏上音符 " + drawn +
    "  ·  峰值 " + peakNotes + "  ·  共 " + NOTES.length + " 音符 / " + LINES.length + " 判定线";
  els.tl.textContent = t.toFixed(3) + " / " + M.duration.toFixed(3) + " s";
  els.seek.value = String(Math.round(t * 1000));
}

function loop(now) {
  const dt = lastFrame ? (now - lastFrame) / 1000 : 0;
  lastFrame = now;
  if (playing) {
    t += dt * rate;
    if (t > M.duration) t = 0;
  }
  frame(dt);
  requestAnimationFrame(loop);
}
frame(0);
requestAnimationFrame(loop);

// ---- 自检探针：把 JS 侧算出的坐标吐出来，供 tools/check_renderer.py 与 Python 侧逐点比对 ----
// 用法：?probe=1&probe_step=25  然后用 chrome --headless --dump-dom 抓取
// 探针模式会把整份 DOM 换成只剩 <pre id="probe">，避免把 845 KB 的数据一起 dump 出来。
if (qs.get("probe") === "1") {
  const step = Math.max(1, parseInt(qs.get("probe_step") || "25", 10));
  const rows = [];
  for (let i = 0; i < NOTES.length; i += step) {
    const n = NOTES[i];
    for (const dt of [0, -0.2, 0.15, -0.6]) {
      const tt = n.t + dt;
      const w = noteWorld(n, tt);
      rows.push([n.l, n.t, tt, w[0], w[1], PX(w[0]), PY(w[1]), w[5]]);
    }
  }
  document.documentElement.innerHTML =
    '<body><pre id="probe">' + JSON.stringify(rows) + "</pre></body>";
}
</script>
</body>
</html>
"""


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="谱面 → 自包含 HTML 回放")
    ap.add_argument("chart", nargs="?", default=str(DEFAULT_SAMPLE), help="谱面 JSON 路径")
    ap.add_argument("-o", "--out", default=str(PROJECT_ROOT / "out" / "replay.html"))
    ap.add_argument("--screen", default="1920x1080")
    ap.add_argument("--aspect", type=float, default=DEFAULT_ASPECT_RATIO)
    ap.add_argument("--line-length", type=float, default=1.5,
                    help="判定线半长（世界单位，1 = 屏幕半宽）；仅为观感，不影响坐标")
    ap.add_argument("--digits", type=int, default=9, help="取值截断小数位（时间不截断）")
    ap.add_argument("--no-simplify", action="store_true", help="不做共线关键帧合并")
    return ap


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    path = Path(args.chart)
    if not path.is_file():
        print(f"谱面不存在：{path}", file=sys.stderr)
        return 2

    sw, sh = (float(x) for x in args.screen.lower().replace("*", "x").split("x"))
    chart = Chart(load(path), aspect_ratio=args.aspect, screen=(sw, sh))

    payload = build_payload(
        chart, path.name, args.line_length, not args.no_simplify, args.digits
    )
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    raw_kf = sum(len(zip_times(a)) for ln in chart.lines
                 for a in (ln.move.x, ln.move.y, ln.rotate, ln.alpha, ln.height))
    kept_kf = sum(len(v) // 2 for ln in payload["lines"] for v in ln.values() if isinstance(v, list))

    html = (
        HTML.replace("__TITLE__", f"Phigros 回放 · {path.stem}")
        .replace("__CW__", str(CANVAS_W))
        .replace("__CH__", str(CANVAS_H))
        .replace("__DATA__", data)
    )

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")

    print(f"写入 {out}")
    print(f"  谱面     {path.name}（{len(chart.lines)} 判定线 / {len(payload['notes'])} 音符）")
    print(f"  时长     {chart.max_time:.3f}s")
    print(f"  关键帧   {raw_kf} → {kept_kf}"
          f"（{'共线合并后' if not args.no_simplify else '未合并'}，"
          f"数据 {len(data)/1024:.0f} KB / 文件 {out.stat().st_size/1024:.0f} KB）")
    print(f"  打开     file:///{out.resolve().as_posix()}")
    print(f"  单帧     ?t=<秒>&still=1")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
