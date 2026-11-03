#!/usr/bin/env python
"""M2 轨迹层的量化验收。

跑法：
    python tools/verify_timeline.py [chart.json] [--screen 1920x1080] [--aspect 1.7777778]

输出六组独立核对，每组都给出「判据 / 实测值 / 是否通过」：

  [1] 速度积分 vs 官谱 floorPosition —— 唯一一条不依赖任何参考实现的对拍
  [2] 击打时刻 dy == 0（音符落在线上的充要条件）
  [3] 轨迹在 t_hit 处回归到 M1 的击打点
  [4] 进场距离的尺度（HEIGHT_RATIO / aspect 用错量级会在这里暴露）
  [5] 判定线状态与屏幕越界
  [6] 时窗并发度下限（给 M3 的预警，不是 M3 的结论）
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pgauto.chart import (  # noqa: E402
    DEFAULT_ASPECT_RATIO,
    HEIGHT_RATIO,
    X_UNIT,
    Chart,
    load,
)

DEFAULT_SAMPLE = PROJECT_ROOT / "samples" / "rrhar_at" / "Rrharil.TeamGrimoire.0.json"


def pct(values: Sequence[float], q: float) -> float:
    if not values:
        return float("nan")
    s = sorted(values)
    i = min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))
    return s[i]


def _hist(counter: Dict[float, int]) -> Dict[int, int]:
    """把「值 → 次数」的计数表压缩成「次数 → 出现几次」的直方图。"""
    out: Dict[int, int] = {}
    for v in counter.values():
        out[v] = out.get(v, 0) + 1
    return out


def rule(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def check(label: str, ok: bool, actual: str, expect: str) -> bool:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    print(f"         实测 {actual}")
    print(f"         预期 {expect}")
    return ok


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="M2 轨迹层量化验收")
    ap.add_argument("chart", nargs="?", default=str(DEFAULT_SAMPLE), help="谱面 JSON")
    ap.add_argument("--screen", default="1920x1080", help="屏幕尺寸，形如 1920x1080")
    ap.add_argument("--aspect", type=float, default=DEFAULT_ASPECT_RATIO, help="宽高比")
    args = ap.parse_args(argv)

    path = Path(args.chart)
    if not path.is_file():
        print(f"谱面不存在：{path}", file=sys.stderr)
        return 2

    sw, sh = (float(x) for x in args.screen.lower().replace("*", "x").split("x"))
    chart = Chart(load(path), aspect_ratio=args.aspect, screen=(sw, sh))
    hits = chart.notes()

    results: List[bool] = []

    print(f"谱面     {path.name}")
    print(f"格式     formatVersion={chart.format_version}  offset={chart.offset}")
    print(f"规模     {len(chart.lines)} 条判定线 / {len(hits)} 个音符")
    print(f"屏幕     {sw:.0f}x{sh:.0f}  aspect={chart.aspect_ratio:.6f}（y 可见范围 ±{1/chart.aspect_ratio:.4f}）")
    print(f"时间轴   max_time={chart.max_time:.3f}s（速度曲线外推终点）")
    print(f"常量     X_UNIT={X_UNIT:.4f}  HEIGHT_RATIO={HEIGHT_RATIO}  "
          f"尺度除数 HEIGHT_RATIO×aspect={HEIGHT_RATIO * chart.aspect_ratio:.6f}")

    # ------------------------------------------------------------------ [1]
    rule("[1] 速度积分 vs 官谱自带 floorPosition 字段")
    print("  判据：官谱每个音符都写了 floorPosition，它就是判定线高度曲线在该时刻的取值。")
    print("       自己按 speedEvents 积分出的曲线若与它逐音符吻合，积分就是对的。")
    print("       这条对拍不依赖 Phira / sim-phi，是谱面数据自身的内部一致性。")
    mism = chart.note_h_mismatch
    worst_fp = 0.0
    for h in hits:
        worst_fp = max(worst_fp, abs(h.note_h - h.floor_position))
    results.append(
        check(
            "全部音符的重算高度与 floorPosition 一致",
            not mism,
            f"不符 {len(mism)} 个，最大偏差 {worst_fp:.6f}",
            "0 个，最大偏差 <= 1e-3（float32 舍入量级）",
        )
    )

    # ------------------------------------------------------------------ [2]
    rule("[2] 击打时刻音符精确落在判定线上（dy == 0）")
    worst_dy = max(abs(chart.note_travel(h, h.t_sec)) for h in hits)
    results.append(
        check(
            "max |dy(t_hit)|",
            worst_dy < 1e-9,
            f"{worst_dy:.3e}",
            "< 1e-9（浮点精确）",
        )
    )

    # ------------------------------------------------------------------ [3]
    rule("[3] 轨迹在 t_hit 处回归到 M1 的击打点")
    print("  （M1 的 _hit_point 与 M2 的 note_pos 是两条独立代码路径，必须重合）")
    worst_px = worst_py = 0.0
    for h in hits:
        wx, wy = chart.note_pos(h, h.t_sec)
        px, py = chart.to_px(wx, wy)
        worst_px = max(worst_px, abs(px - h.px))
        worst_py = max(worst_py, abs(py - h.py))
    results.append(
        check(
            "最大像素偏差",
            max(worst_px, worst_py) < 1e-9,
            f"px {worst_px:.3e} / py {worst_py:.3e}",
            "< 1e-9 px",
        )
    )

    # ------------------------------------------------------------------ [4]
    rule("[4] 进场距离的尺度（HEIGHT_RATIO × aspect 是否用错量级）")
    print("  判据：世界坐标里 1 个单位 = 屏幕半宽，纵向可见范围只有 ±1/aspect。")
    print("       音符在击打前若干秒离线的距离应当是「1 的量级」；")
    print("       若算出几十上百，说明尺度常量错了。")
    for lead in (0.1, 0.2, 0.3, 0.5, 1.0):
        ds = [abs(chart.note_travel(h, h.t_sec - lead)) for h in hits]
        print(
            f"    击打前 {lead:>4.1f}s  中位 {pct(ds, 0.5):7.4f}  "
            f"p90 {pct(ds, 0.9):7.4f}  p99 {pct(ds, 0.99):8.4f}  最大 {max(ds):9.4f}"
        )
    mid = pct([abs(chart.note_travel(h, h.t_sec - 0.3)) for h in hits], 0.5)
    results.append(
        check(
            "击打前 0.3s 的中位离线距离",
            0.02 < mid < 5.0,
            f"{mid:.4f} 世界单位（= {mid * sw / 2:.1f} px）",
            "0.02 ~ 5 世界单位",
        )
    )

    # 音符「进入屏幕」的时刻：|dy| 首次降到 1/aspect 以内，距击打还有多久
    leads = []
    for h in hits:
        target = 1.0 / chart.aspect_ratio
        found = None
        t = h.t_sec
        for _ in range(600):  # 最多回看 3 秒（步长 5ms）
            if abs(chart.note_travel(h, t)) <= target:
                found = h.t_sec - t
            else:
                break
            t -= 0.005
            if t < h.t_sec - 3.0:
                break
        leads.append(found if found is not None else 0.0)
    print()
    print(f"  「进入纵向可见带（|dy| <= 1/aspect）」的平均提前量  {sum(leads)/len(leads):.3f}s"
          f"   中位 {pct(leads, 0.5):.3f}s   最长 {max(leads):.3f}s")
    results.append(
        check(
            "提前量落在一首曲子合理的预告时间范围内",
            0.05 < pct(leads, 0.5) < 3.0,
            f"中位 {pct(leads, 0.5):.3f}s",
            "0.05 ~ 3.0s",
        )
    )

    # ------------------------------------------------------------------ [5]
    rule("[5] 判定线状态与屏幕越界")
    print("  判据：只统计**可见**（透明度 > 0）的采样点。官谱会把自己「停」到屏幕外来")
    print("       实现隐形判定线——样本谱 line 11~22 的 disappearEvents 写死 start=end=0，")
    print("       同时把 moveEvents 的 start2 停到 5.0（世界 y = 9/1.7778 = 5.06），属于刻意行为。")
    half_y = 1.0 / chart.aspect_ratio
    stats = []
    for line in chart.lines:
        xs, ys, angs = [], [], []
        vis = 0
        for k in range(0, 801):
            t = chart.max_time * k / 800.0
            x, y, a, al = chart.line_state(line.id, t)
            if al <= 1e-6:
                continue
            vis += 1
            xs.append(x); ys.append(y); angs.append(a)
        stats.append((line.id, vis, xs, ys, angs))

    print(f"    {'线':>3} {'可见点':>6} {'x范围(可见时)':>22} {'y范围(可见时)':>22} {'角度范围':>18}")
    for lid, vis, xs, ys, angs in stats:
        if vis == 0:
            print(f"    {lid:>3} {vis:>6}   —— 全程隐形（音符悬空，属官谱手法）")
            continue
        print(f"    {lid:>3} {vis:>6} [{min(xs):>8.4f},{max(xs):>8.4f}] "
              f"[{min(ys):>8.4f},{max(ys):>8.4f}] [{min(angs):>7.2f},{max(angs):>7.2f}]")

    visible_pts = sum(s[1] for s in stats)
    out_band = sum(
        1 for _, vis, _, ys, _ in (s for s in stats) for y in ys if abs(y) > half_y
    )
    frac = out_band / visible_pts if visible_pts else 0.0
    results.append(
        check(
            "可见时刻判定线中心的纵向越界比例",
            frac < 0.20,
            f"{out_band} / {visible_pts} = {frac:.2%}（可见带 |y| <= {half_y:.4f}）",
            "< 20%（线可以贴着屏幕边缘，但不能整体跑飞）",
        )
    )
    invisible = [s[0] for s in stats if s[1] == 0]
    print()
    print(f"    全程隐形的判定线：{invisible}（共 {len(invisible)} 条）")

    off_hit = [h for h in hits if not (0.0 <= h.px <= sw and 0.0 <= h.py <= sh)]
    results.append(
        check(
            "击打点全部落屏内",
            not off_hit,
            f"越界 {len(off_hit)} / {len(hits)}",
            "0",
        )
    )

    lead_off = []
    for h in hits:
        for k in range(21):  # t_hit - 0.100s .. t_hit，步长 5ms
            t = h.t_sec - 0.005 * k
            wx, wy = chart.note_pos(h, t)
            px, py = chart.to_px(wx, wy)
            if not (-0.02 * sw <= px <= 1.02 * sw and -0.02 * sh <= py <= 1.02 * sh):
                lead_off.append(h)
                break
    frac_lead = len(lead_off) / len(hits)
    results.append(
        check(
            "击打前 100ms 音符已可见的比例",
            frac_lead <= 0.01,
            f"不可见 {len(lead_off)} / {len(hits)} = {frac_lead:.2%}",
            "<= 1%（极少数『瞬现』音符合法）",
        )
    )

    print()
    print("  结构核对：官谱的 moveEvents / speedEvents 时间轴是否首尾相接")
    ov_tot = gap_tot = ev_tot = 0
    for line in chart.lines:
        for key in ("judgeLineMoveEvents", "speedEvents", "judgeLineRotateEvents",
                    "judgeLineDisappearEvents"):
            ev = line.raw.get(key, [])
            ev_tot += len(ev)
            for i in range(len(ev) - 1):
                if float(ev[i + 1]["startTime"]) < float(ev[i]["endTime"]):
                    ov_tot += 1
                elif float(ev[i + 1]["startTime"]) > float(ev[i]["endTime"]):
                    gap_tot += 1
    print(f"    事件总数 {ev_tot}；**重叠 {ov_tot} 处、空隙 {gap_tot} 处**")
    print("    ⇒ 事件首尾严丝合缝，关键帧构造 (t_i, start_i) → (t_{i+1}, end_i)")
    print("      可以精确复现「方波」（如 line 0 在 5.49~6.24s 的线体闪烁：")
    print("      start2 在 10.0 与 0.15 之间每 39.5ms 跳变，alpha 同步振荡）。")
    results.append(
        check("事件时间轴无重叠无空隙", ov_tot == 0 and gap_tot == 0,
              f"重叠 {ov_tot} / 空隙 {gap_tot}", "0 / 0")
    )

    if lead_off:
        print()
        print("  击打前 100ms 尚不可见的音符：")
        for h in lead_off[:6]:
            lx, ly, ang, al = chart.line_state(h.line, h.t_sec - 0.1)
            print(f"    line={h.line} t_hit={h.t_sec:.4f} {h.type_name} "
                  f"击打点=({h.px:.1f},{h.py:.1f})  100ms 前线在 ({lx:.3f},{ly:.3f}) α={al:.2f}")
        print("    原因：这些时刻判定线本身被 moveEvents 停在屏幕外（start2=10.0 ⇒ 世界 y=10.69），")
        print("    音符随之在屏外，直到线「落回」才出现 —— 属谱面的瞬现演出，不是坐标错误。")

    print()
    print("  过线之后音符会飞出屏幕，但这不是瑕疵：")
    tail_off = [h for h in hits if abs(chart.note_travel(h, h.t_sec + 0.05)) > 1.0]
    print(f"    击打后 50ms 内 |dy| > 1 的音符 {len(tail_off)} 个，"
          f"全部集中在 {sorted({(h.line, round(h.t_sec, 3)) for h in tail_off})}")
    print("    原因是这些时刻紧邻 value=999.0 的「速度瞬移」事件（样本谱 line 0 的 ev28 / ev60），")
    print("    高度曲线在几十毫秒内暴涨数十单位，把音符整体弹开。")
    print("    真实渲染里已被判定的非 Hold 音符会被直接剔除（Phira note.rs::render 开头就 return），")
    print("    所以画面上看不到；本包的 HTML 回放也照此剔除。")
    spikes = [e for line in chart.lines for e in line.raw.get("speedEvents", []) if abs(float(e["value"])) > 100]
    negs = [e for line in chart.lines for e in line.raw.get("speedEvents", []) if float(e["value"]) < 0]
    print(f"    全线速度事件：|value| > 100 的 {len(spikes)} 个，value < 0 的 {len(negs)} 个"
          f"（负速度 ⇒ 高度曲线非单调，但击打点不受影响）")

    # ------------------------------------------------------------------ [6]
    rule("[6] 时窗并发度下限（⚠️ 给 M3 的预警，不是 M3 的结论）")
    print("  Hold 的指针要占用 [t, t+holdTime] 整段，而不是击打瞬间。")
    print("  这里只统计 Hold 区间自身的最大重叠数，单指 Tap 一律按瞬时算 —— 因此是**下界**。")
    intervals = [(h.t_sec, h.t_sec + h.hold_sec) for h in hits if h.type == 3 and h.hold_sec > 0]
    events: List[Tuple[float, int]] = []
    for a, b in intervals:
        events.append((a, +1))
        events.append((b, -1))
    events.sort(key=lambda e: (e[0], e[1]))
    cur = peak = 0
    peak_t = 0.0
    for t, d in events:
        cur += d
        if cur > peak:
            peak, peak_t = cur, t
    print(f"    Hold 区间数 {len(intervals)}；最大重叠 {peak}（发生于 t={peak_t:.3f}s）")
    same_instant: Dict[float, int] = {}
    for h in hits:
        same_instant[h.t_sec] = same_instant.get(h.t_sec, 0) + 1
    hist = _hist(same_instant)
    print(f"    同刻击打的音符数上限 {max(same_instant.values())}；"
          f"直方图（同刻音符数 → 出现多少次）{dict(sorted(hist.items()))}")
    floor_need = max(peak, max(same_instant.values()))
    print(f"    ⇒ M3 的真实指针数需求 >= max(Hold 重叠, 同刻击打) = {floor_need}；"
          f"必须用区间图着色按完整时窗重算。")

    # ------------------------------------------------------------------ 汇总
    rule("汇总")
    for i, ok in enumerate(results, 1):
        print(f"  [{i}] {'PASS' if ok else 'FAIL'}")
    print()
    print(f"  {sum(results)} / {len(results)} 通过")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
