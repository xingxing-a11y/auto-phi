#!/usr/bin/env python3
"""把**全部官方谱**灌进新版 C 段，回答两个问题：

    1. 「指针数 ≤ 10」在 1064 个谱面上成立吗？分布长什么样？
    2. 有没有谱面连 10 根手指都不够（那 D 段就无解了）？

M3 的结论来自单个谱面（8/10），M4 用「区间图着色」模型普查得到 27 个超限，
M5 换成「指针池 + 切向复用」模型后降到 0。本工具是该模型的**全量回归**。

加 `--score` 还能顺带跑 G 段的端到端计分（`tools/replay_score.py` 的两套判定模型）：
严格模型 = Tap/Hold 起手必须独占一次按下（Phigros 真实规则），
宽松模型 = 判定区内有手指即可（C 段设计时的自洽假设）。两者的差就是复用模型的风险敞口。

用法::

    python tools/sweep_charts.py                 # 全部（只做指针普查）
    python tools/sweep_charts.py --score         # 顺带端到端计分
    python tools/sweep_charts.py --hardest 40    # 只挑音符最多的 40 个
    python tools/sweep_charts.py --sample 120 --seed 7

输出 `out/sweep.json`。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from pgauto import schedule as sch  # noqa: E402
from pgauto.chart import Chart  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CHARTS = ROOT / "out" / "charts"
DEFAULT_OUT = ROOT / "out" / "sweep.json"


def score_chart(chart: Chart, notes: list, res, thresh: float, lead: int,
                flick_min: float) -> dict:
    """用 `replay_score` 的两套判定模型给这个谱面打分（`--score` 时才算）。

    几何走 `verify_schedule.Geom` —— 与 C 段实现**同层级的独立实现**，
    不是 `pgauto` 自己的几何对象。事件已经是画布坐标，所以映射取恒等。
    """
    import replay_score as rs  # noqa: PLC0415  只在 --score 时才需要

    cw, ch = chart.screen_w, chart.screen_h
    geom = rs.Geom({"format_version": chart.format_version,
                    "aspect_ratio": chart.aspect_ratio,
                    "lines": chart.line_events_payload()}, (cw, ch))
    note_dicts = [{"index": i, "type": n.type, "type_name": n.type_name,
                   "t_ms": n.t_ms, "hold_ms": n.hold_ms, "line": n.line,
                   "position_x": n.position_x} for i, n in enumerate(notes)]
    events = [{"t_ms": e.t_ms, "action": e.action, "pointer_id": e.pointer_id,
               "x": e.x, "y": e.y} for e in res.events]

    rp = rs.Replay(events, lambda x, y: (x, y), geom, (cw, ch))
    strict = rs.Score("strict", rs.judge_strict(note_dicts, rp, thresh, flick_min, lead))
    lenient = rs.Score("lenient",
                       rs.judge_lenient(note_dicts, rp, thresh, flick_min, lead))
    return dict(strict_score=strict.score, strict_rank=strict.rank,
                strict_miss=strict.counts[rs.MISS], strict_good=strict.counts[rs.GOOD],
                lenient_score=lenient.score, lenient_miss=lenient.counts[rs.MISS],
                score_gap=lenient.score - strict.score,
                ap=strict.score == 1000000)


def analyse(job: tuple[str, str, dict]) -> dict:
    """在子进程里跑一个谱面（直接吃谱面 JSON，不经过 notes.json）。"""
    song, difficulty, kw = job
    try:
        j = json.loads(
            (Path(kw["charts_root"]) / kw["rel_path"]).read_text(encoding="utf-8")
        )
        chart = Chart(j)
        hits = chart.notes()
        notes = [
            sch.Note(
                index=i,
                type=h.type,
                t_ms=h.t_ms,
                px=h.px,
                py=h.py,
                hold_ms=h.hold_sec * 1000.0,
                angle_deg=chart.line_state(h.line, h.t_sec)[2],
                line=h.line,
                position_x=h.position_x,
                multiple=h.multiple,
            )
            for i, h in enumerate(hits)
        ]
        cw, ch = chart.screen_w, chart.screen_h
        px = kw["presence_reuse_px"]
        res = sch.build(
            notes,
            geometry=sch.geometry_from_chart(chart),
            canvas=(cw, ch),
            max_pointers=kw["max_pointers"],
            tangent_threshold_ratio=kw["tangent_threshold"],
            clamp_offscreen=kw["clamp_offscreen"],
            presence_reuse_ratio=(None if px is None else px / cw),
        )
        #: 事件跑出画布 = 触摸落到玩法区之外（黑边里），游戏侧等同「没按在判定区」。
        over = [max(0.0, -e.x, e.x - cw, -e.y, e.y - ch) for e in res.events]
        n_out = sum(1 for d in over if d > 0.0)
        extra: dict = {}
        if kw["score"]:
            extra = score_chart(
                chart, notes, res,
                thresh=kw["tangent_threshold"] * chart.screen_w,
                lead=sch.FLICK_LEAD_MS, flick_min=kw["flick_min"])
        return dict(
            song=song, difficulty=difficulty,
            format_version=chart.format_version,
            notes=len(notes),
            pointers=res.pointer_count,
            peak=res.stats.get("peak_concurrency", 0),
            events=res.stats.get("events", 0),
            serves=res.stats.get("serves", 0),
            flicks=sum(1 for s in res.serves if s.stage == "flick"),
            reuse=res.stats.get("reuse", 0),
            refused_share=res.stats.get("refused_share", 0),
            projected=res.stats.get("projected_into_screen", 0),
            retimed=res.stats.get("retimed", 0),
            clamped=res.stats.get("clamped", 0),
            hold_keep_clamped=res.stats.get("hold_keep_clamped", 0),
            unassigned=len(res.unassigned),
            warnings=len(res.warnings),
            out_of_canvas=n_out,
            max_overflow=round(max(over, default=0.0), 2),
            flick_radius_shrunk=res.stats.get("flick_radius_shrunk", 0),
            flick_radius_min=res.stats.get("flick_radius_min"),
            flick_radius_buckets=res.stats.get("flick_radius_buckets", {}),
            flick_shifted=res.stats.get("flick_shifted", 0),
            flick_shift_max=res.stats.get("flick_shift_max", 0.0),
            presence_reuse_px=res.stats.get("presence_reuse_px"),
            ok=(not res.unassigned),
            error=None,
            **extra,
        )
    except Exception as e:  # noqa: BLE001
        return dict(song=song, difficulty=difficulty, format_version=None,
                    notes=0, pointers=0, peak=0, events=0, serves=0, flicks=0, reuse=0,
                    refused_share=0,
                    projected=0, retimed=0, clamped=0, hold_keep_clamped=0,
                    unassigned=0, warnings=0, ok=False,
                    out_of_canvas=0, max_overflow=0.0,
                    flick_radius_shrunk=0, flick_radius_min=None,
                    flick_radius_buckets={}, flick_shifted=0, flick_shift_max=0.0,
                    presence_reuse_px=None,
                    error=f"{type(e).__name__}: {e}",
                    **({} if not kw.get("score") else
                       dict(strict_score=0, strict_rank="-", strict_miss=0,
                            strict_good=0, lenient_score=0, lenient_miss=0,
                            score_gap=0, ap=False)))


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="批量跑新版 C 段，统计指针数分布")
    p.add_argument("--charts", type=Path, default=DEFAULT_CHARTS)
    p.add_argument("-o", "--out", type=Path, default=DEFAULT_OUT)
    p.add_argument("--hardest", type=int, default=0, help="只取音符最多的 N 个")
    p.add_argument("--sample", type=int, default=0, help="随机抽 N 个（配合 --seed）")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--jobs", type=int, default=0, help="并行进程数（0=CPU 数）")
    p.add_argument("--max-pointers", type=int, default=sch.MAX_POINTERS)
    p.add_argument("--tangent-threshold", type=float,
                   default=sch.TANGENT_THRESHOLD_RATIO,
                   help=f"切向复用阈值（默认 {sch.TANGENT_THRESHOLD_RATIO:.6f} = 100/1280）")
    p.add_argument("--clamp-offscreen", action="store_true",
                   help="屏外判定点拉到屏幕边缘（默认关：够不着的音符如实报未分配）")
    p.add_argument("--score", action="store_true",
                   help="额外用 replay_score 的两套判定模型打分（慢一些）")
    p.add_argument("--flick-min-move", type=float, default=60.0,
                   help="Flick 认定所需最小扫动（画布 px，默认 60；仅 --score 用）")
    p.add_argument("--presence-reuse-px", type=float, default=None,
                   help="「已按着的手指离判定点多远还算在判定区内」的画布像素上限。"
                        "省略 = 旧口径（= --tangent-threshold 的 150px）")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    # 清单在 <out>/charts/index.json，其中 path 字段相对 <out> 记录
    charts_root = args.charts.parent
    manifest = json.loads((args.charts / "index.json").read_text(encoding="utf-8"))
    records = manifest["charts"]
    print(f"[清单] {len(records)} 个谱面（{manifest.get('source')}）")

    picked = records
    if args.hardest:
        picked = sorted(records, key=lambda r: -r.get("notes", 0))[: args.hardest]
        print(f"[筛选] 音符最多的 {len(picked)} 个")
    elif args.sample:
        import random

        rng = random.Random(args.seed)
        picked = rng.sample(records, min(args.sample, len(records)))
        print(f"[筛选] 随机抽样 {len(picked)} 个（seed={args.seed}）")

    jobs = [
        (r["song"], r["difficulty"],
         {"rel_path": r["path"], "charts_root": str(charts_root),
          "max_pointers": args.max_pointers, "tangent_threshold": args.tangent_threshold,
          "clamp_offscreen": args.clamp_offscreen,
          "presence_reuse_px": args.presence_reuse_px,
          "score": args.score, "flick_min": args.flick_min_move})
        for r in picked
    ]

    t0 = time.time()
    rows: list[dict] = []
    n_jobs = args.jobs or None
    with ProcessPoolExecutor(max_workers=n_jobs) as pool:
        for i, row in enumerate(pool.map(analyse, jobs, chunksize=4), 1):
            rows.append(row)
            if i % 50 == 0 or i == len(jobs):
                print(f"  {i}/{len(jobs)} ... {time.time() - t0:.1f}s")

    elapsed = time.time() - t0
    ok = [r for r in rows if r["ok"]]
    errors = [r for r in rows if r["error"]]
    bad = [r for r in rows if not r["ok"]]

    print(f"\n{'=' * 72}\n结果\n{'=' * 72}")
    print(f"  谱面        {len(rows)}   用时 {elapsed:.1f}s")
    print(f"  通过        {len(ok)}")
    print(f"  超限/未分配 {len(bad) - len(errors)}")
    print(f"  异常        {len(errors)}")
    if rows:
        print(f"  最大指针数  {max(r['pointers'] for r in rows)} / {args.max_pointers}")
        print(f"  最大峰值    {max(r['peak'] for r in rows)}")
        tot_notes = sum(r["notes"] for r in rows)
        tot_reuse = sum(r["reuse"] for r in rows)
        tot_serves = sum(r["serves"] for r in rows)
        print(f"  音符总数    {tot_notes}")
        print(f"  服务总数    {tot_serves}   其中复用（未发事件）{tot_reuse}"
              f"  = {100.0 * tot_reuse / max(1, tot_serves):.2f}%")
        print(f"  事件总数    {sum(r['events'] for r in rows)}")
        print(f"  投影回屏    {sum(r['projected'] for r in rows)}"
              f"    时间微调 {sum(r['retimed'] for r in rows)}"
              f"    拉进屏幕 {sum(r['clamped'] for r in rows)}"
              f"    长按持续帧出屏 {sum(r['hold_keep_clamped'] for r in rows)}")
        n_share = sum(r.get("refused_share", 0) for r in rows)
        n_share_charts = len([r for r in rows if r.get("refused_share")])
        print(f"  同帧双押    {n_share} 处（分布在 {n_share_charts} 个谱面）"
              f" —— 同帧按下已被别的 Tap/Hold 吃掉，各补按一次")
        n_out = sum(r.get("out_of_canvas", 0) for r in rows)
        n_out_charts = len([r for r in rows if r.get("out_of_canvas")])
        print(f"  跑出画布    {n_out} 条事件（分布在 {n_out_charts} 个谱面）"
              f"    最大越界 {max((r.get('max_overflow') or 0) for r in rows):.1f}px")
        print(f"  Flick 压半径 {sum(r.get('flick_radius_shrunk', 0) for r in rows)} 处"
              f" / 全库 Flick {sum(r.get('flicks', 0) for r in rows)} 个"
              f"    推中心 {sum(r.get('flick_shifted', 0) for r in rows)} 处"
              f"（最大 {max((r.get('flick_shift_max') or 0) for r in rows):.1f}px）"
              f"    最小半径 {min((r.get('flick_radius_min') or 0) for r in rows
                                     if r.get('flick_radius_min')) if any(
                  r.get('flick_radius_min') for r in rows) else '-'}"
              f"    presence_reuse_px {args.presence_reuse_px or '旧口径'}")
        buckets = {}
        for r in rows:
            for k, v in (r.get("flick_radius_buckets") or {}).items():
                buckets[k] = buckets.get(k, 0) + v
        if any(buckets.values()):
            print(f"    半径分布 <10px {buckets.get('lt10', 0)}"
                  f" / <30px {buckets.get('lt30', 0)}"
                  f" / <60px {buckets.get('lt60', 0)}"
                  f" / ≥60px {buckets.get('ge60', 0)}")
        if n_out:
            print("  跑出画布明细（前 10）：")
            for r in sorted(rows, key=lambda x: -(x.get("out_of_canvas") or 0))[:10]:
                if r.get("out_of_canvas"):
                    print(f"    {r['out_of_canvas']:>4} 条  越界 {r['max_overflow']:>6.1f}px  "
                          f"{r['song']}/{r['difficulty']}")
    if bad:
        print("\n  超限明细（按指针数降序，前 20）：")
        for r in sorted(bad, key=lambda x: -x["pointers"])[:20]:
            print(f"    {r['pointers']:>3} 指针  峰值{r['peak']:>3}  {r['notes']:>5} 音符  "
                  f"{r['song']}/{r['difficulty']}" + (f"   [{r['error']}]" if r["error"] else ""))
    if errors:
        print("\n  异常明细（前 8）：")
        for r in errors[:8]:
            print(f"    {r['song']}/{r['difficulty']}: {r['error']}")

    hist: dict[int, int] = {}
    for r in rows:
        hist[r["pointers"]] = hist.get(r["pointers"], 0) + 1
    print(f"\n  指针数分布  {dict(sorted(hist.items()))}")

    if args.score:
        scored = [r for r in rows if not r["error"] and "strict_score" in r]
        ap = [r for r in scored if r["ap"]]
        diverged = [r for r in scored if r["score_gap"]]
        print(f"\n  端到端计分（严格模型：Tap/Hold 起手必须独占一次按下）")
        print(f"    满分（理论 AP）{len(ap)} / {len(scored)}   有掉分 {len(scored) - len(ap)}")
        print(f"    平均得分 {sum(r['strict_score'] for r in scored) / max(1, len(scored)):.1f}"
              f"    最低 {min((r['strict_score'] for r in scored), default=0)}")
        print(f"    两套模型（严格 vs 宽松）有差 {len(diverged)} 个谱面")
        worst = sorted(scored, key=lambda r: r["strict_score"])[:15]
        if worst and worst[0]["strict_score"] < 1000000:
            print("\n    掉分最多的 15 个：")
            for r in worst:
                print(f"      {r['strict_score']:>7}  {r['strict_rank']:<14}"
                      f" Miss {r['strict_miss']:>4}  Good {r['strict_good']:>3}"
                      f"   {r['song']}/{r['difficulty']}")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({
        "source": str(args.charts),
        "model": "pointer-pool + tangential-reuse (phisap algo2 谱系)",
        "clamp_offscreen": args.clamp_offscreen,
        "presence_reuse_px": args.presence_reuse_px,
        "scored": args.score,
        "count": len(rows), "elapsed_sec": round(elapsed, 1),
        "max_pointers": max((r["pointers"] for r in rows), default=0),
        "max_peak": max((r["peak"] for r in rows), default=0),
        "histogram": dict(sorted(hist.items())),
        "failed": bad, "rows": rows,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n[输出] {args.out}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
