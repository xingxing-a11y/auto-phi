#!/usr/bin/env python
"""C 段交付物：notes.json → touches.json（标准触摸事件序列）。

用法：
    python tools/schedule_chart.py out/notes.json -o out/touches.json
                                 [--max-pointers 10] [--screen 1080x2340]
                                 [--tangent-threshold 0.078125]
                                 [--presence-reuse-px 20 | --legacy-presence]
                                 [--no-retime-flick] [--clamp-offscreen]
                                 [--no-plan] [--no-apply-offset]

输出字段（D 段的消费契约）：
    canvas     逻辑画布（C 段内部计算空间），固定 1920x1080
    screen     目标设备分辨率；事件 x/y 已按此换算成**设备像素**
    t0_rule    t_ms 的零点约定
    geometry   exact = 用了 notes.json 的 `lines`（判定线移动 / 旋转）算长按跟随；
               static = 缺 `lines`，退化为「判定线不动、长按不跟随」，**仅供离线试跑**
    events     [t_ms, action, pointer_id, x, y]；action ∈
               DOWN / POINTER_DOWN / MOVE / POINTER_UP / UP
    plan       C 段的**规划**本身：[stage, note_index, t_ms, pointer_id, x, y]
               stage: 0=Tap 1=Hold 起点 2=Hold 持续 3=Drag 4=Flick
               有了它，验收脚本才能把「事件流」与「判定规则」对上，
               判断一个不落在音符坐标上的 DOWN 究竟是**正确复用**还是**出错**。
               （--no-plan 可省掉，只留 events。）
    unassigned 判定点够不着的音符明细；空数组 = 全部音符都能服务。

屏幕外音符的两级兜底（默认只开第一级，宁可少报也不造假）：
    ① 时间微调（`--no-retime-flick` 可关）：Flick 的判定窗口是 ±flick_lead，
       判定线的「闪烁」会让同一音符在别的位置也能判 —— 在窗口内取距 t0 最近的入屏时刻。
    ② 拉进屏幕（`--clamp-offscreen` 开）：把判定点摆到屏幕边缘。事件流因此完整可注入，
       但**不保证命中**，所以默认关闭，让够不着的音符如实出现在 unassigned 里。

屏幕映射（竖屏手机）：玩法区等比例适应宽度、垂直居中
    scale    = screen.width / canvas.width
    x_screen = x_canvas * scale
    y_screen = y_canvas * scale + (screen.height - canvas.height * scale) / 2
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

DEFAULT_SCREEN = (1080.0, 2340.0)

STAGE_CODE = {"tap": 0, "hold_head": 1, "hold_keep": 2, "drag": 3, "flick": 4}


def parse_screen(text: str) -> Tuple[float, float]:
    parts = text.lower().replace("*", "x").split("x")
    if len(parts) != 2:
        raise argparse.ArgumentTypeError(f"屏幕尺寸格式应为 宽x高，例如 1080x2340（收到 {text!r}）")
    try:
        return float(parts[0]), float(parts[1])
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"屏幕尺寸含非数字：{text!r}") from exc


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Phigros 音符 → 指针规划 → 触摸事件序列")
    ap.add_argument("notes", help="notes.json 路径（B 段产出，含 lines 几何）")
    ap.add_argument("-o", "--out", help="输出 touches.json；省略则打印到标准输出")
    ap.add_argument("--max-pointers", type=int, default=sch.MAX_POINTERS)
    ap.add_argument("--tangent-threshold", type=float,
                    default=sch.TANGENT_THRESHOLD_RATIO,
                    help="指针复用的切向阈值，占画布宽的比例"
                         f"（默认 {sch.TANGENT_THRESHOLD_RATIO:.6f} = 100/1280）")
    # ★ 这两个旗标互斥：用法段写的是 `[--presence-reuse-px 20 | --legacy-presence]`。
    #   2026-10-22 之前它们是两个独立选项 ⇒ 同时给会**静默**采用 legacy（`presence_px`
    #   被判成 None），命令行看着给了 20、实际跑的是 150。用法段是契约，这里必须拦住。
    presence = ap.add_mutually_exclusive_group()
    presence.add_argument("--presence-reuse-px", type=float, default=20.0,
                          help="「已按着的手指离判定点多远还算在判定区内」的**画布像素**上限"
                               "（默认 20 = 手指必须跟着音符走，否则补 MOVE 挪上去）。"
                               "调大 ⇒ 退回旧行为（手指原地不动也算在判定区内）。"
                               "旧口径等价于 --legacy-presence（二者互斥）")
    presence.add_argument("--legacy-presence", action="store_true",
                          help="恢复 2026-10-22 之前的旧口径：presence 阈值 = --tangent-threshold"
                               "（150px，手指原地不动也算在判定区内）。仅用于复现旧产物 / A-B 对拍")
    ap.add_argument("--drag-presence-2d", type=float, default=sch.DRAG_PRESENCE_2D_PX,
                    help="Drag 的『在判定区内』额外要求真实 2D 距离 ≤ 该画布像素。"
                         "默认 = 生产值 5（2026-10-25 起默认开）：只算切向时 moonlight/IN 末段第 2 个 Drag "
                         "（t=141.983, x=997）复用了一只停在 y=543 的手指 ⇒ 判'在区内' ⇒ 不补 MOVE ⇒ 必丢。")
    ap.add_argument("--no-drag-2d", action="store_true",
                    help="关掉 Drag 的 2D 闸门（复现 2026-10-25 之前的产物用）")
    ap.add_argument("--flick-lead", type=int, default=sch.FLICK_LEAD_MS)
    ap.add_argument("--flick-span", type=int, default=sch.FLICK_SPAN_MS)
    # ★ 底部安全上沿：Flick 扫动的边界。默认 1.0 = 用满画布（旧行为，扫动末端可落在
    #   画布底边 = 真机屏外 y=720）。生产用 639/720 = 0.8875（详见
    #   `sch.FLICK_BOTTOM_SAFE_RATIO` 与本机 mandatorySystemGestures 的实测值）。
    ap.add_argument("--flick-safe-bottom-ratio", type=float,
                    default=sch.FLICK_BOTTOM_SAFE_RATIO,
                    help="Flick 扫动可用的底部上沿，占画布高的比例。"
                         "1.0 = 用满画布（旧行为）；0.8875 = 真机 y<=639（手势区外）。"
                         "只影响 Flick，不动 Tap/Hold/Drag")
    ap.add_argument("--no-retime-flick", action="store_true",
                    help="关掉 Flick 的时间微调兜底（屏外 Flick 将直接报 unassigned）")
    ap.add_argument("--clamp-offscreen", action="store_true",
                    help="屏外判定点拉到屏幕边缘（保证事件流完整，但不保证命中）")
    ap.add_argument("--screen", type=parse_screen, default=DEFAULT_SCREEN)
    ap.add_argument("--no-plan", action="store_true",
                    help="不输出 plan（规划明细），只留 events")
    ap.add_argument("--no-apply-offset", action="store_true",
                    help="关闭谱面 offset 施加（F4，**默认施加**）"
                         "—— 仅在复现旧口径 / A-B 实验时用（见 2026-10-14-F4 方案）")
    ap.add_argument("--apply-offset", action="store_true",
                    help="（已废弃，保留兼容）默认即施加，本旗标为无操作")
    return ap


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    path = Path(args.notes)
    if not path.is_file():
        print(f"notes.json 不存在：{path}", file=sys.stderr)
        return 2

    data = sch.load_notes(path)
    canvas = data.canvas
    #: 2026-10-22：默认 20px（手指跟随）；`--legacy-presence` 回到 150px 旧口径。
    presence_px = None if args.legacy_presence else args.presence_reuse_px
    result = sch.build(
        data.notes,
        geometry=data.geometry,
        canvas=canvas,
        max_pointers=args.max_pointers,
        tangent_threshold_ratio=args.tangent_threshold,
        flick_lead_ms=args.flick_lead,
        flick_span_ms=args.flick_span,
        flick_retime=not args.no_retime_flick,
        clamp_offscreen=args.clamp_offscreen,
        presence_reuse_ratio=(None if presence_px is None
                              else presence_px / canvas[0]),
        drag_presence_2d_px=(None if args.no_drag_2d else args.drag_presence_2d),
        flick_bottom_safe_ratio=args.flick_safe_bottom_ratio,
    )

    sw, sh = args.screen
    cw, ch = canvas
    scale = sw / cw
    y_off = (sh - ch * scale) / 2.0

    # ── F4 谱面 offset：**默认施加**（2026-10-14 真机判决；旧「默认关」注释已作废）──
    # 机理：游戏只有一个播放头 P（谱面时基），音乐位置 M = P + offset
    #   · 几何按 t_sec 求（sch.build 内部，现状正确，不动）
    #   · 注入墙钟 = zero + (t_sec + offset) ⇒ 事件/规划时刻必须补 offset
    # ★ 判决证据（同曲同锚，唯一变量=是否施加）：
    #   零號車輛 IN（offset=+1000ms）不施加 ⇒ 0454077/49.83%/F；
    #   施加 ⇒ 0993165/99.70%/V ⇒ **默认施加**。
    #   `--no-apply-offset` 可关闭（复现旧口径 / A-B 实验用）。
    # ⚠ 旧结论「chrono+60ms 不加也居中」= 分辨率不足（60ms < ±80ms Perfect 窗）⇒ 已作废。
    offset_chart_ms = float(data.head.get("offset_sec") or 0.0) * 1000.0
    offset_ms = offset_chart_ms if not args.no_apply_offset else 0.0
    if args.apply_offset and not args.no_apply_offset:
        print("  ⓘ --apply-offset 已废弃（默认即施加，本旗标无操作）", file=sys.stderr)

    events = [
        {
            "t_ms": e.t_ms + offset_ms,
            "action": e.action,
            "pointer_id": e.pointer_id,
            "x": round(e.x * scale, 3),
            "y": round(e.y * scale + y_off, 3),
        }
        for e in result.events
    ]
    plan = [
        [STAGE_CODE[s.stage], s.note_index, s.t_ms + offset_ms, s.pointer,
         round(s.px, 3), round(s.py, 3)]
        for s in result.serves
    ] if not args.no_plan else []

    if offset_chart_ms and not offset_ms:
        print(f"  ⚠ 本谱面 offset = {offset_chart_ms:+.1f} ms 已被 --no-apply-offset 关闭"
              f"—— 仅在复现旧口径 / A-B 实验时如此", file=sys.stderr)
    if offset_ms:
        t_min = min((ev["t_ms"] for ev in events), default=0.0)
        if t_min < 0.0:
            print(f"  ⚠️ offset 平移后最早事件 t_ms = {t_min:.1f} < 0（音乐零点之前）"
                  f"—— 谱面 offset 符号/数值需人工复核", file=sys.stderr)

    payload = {
        "chart": data.head.get("chart", path.name),
        # F3（2026-10-13）：透传曲目录名（notes.json 的 song_dir）⇒ touches 自带「歌」。
        # 旧 notes.json 无该字段 ⇒ 空串（消费方退回别名表反查，向后兼容）。
        "song": data.head.get("song_dir", ""),
        "canvas": {"width": cw, "height": ch},
        "screen": {"width": sw, "height": sh},
        "t0_rule": (
            f"t_ms 含谱面 offset（{offset_ms:+.1f} ms）⇒ 音乐零点系"
            if offset_ms else
            "t_ms 相对谱面时间轴原点（谱面 offset=0；或经 --no-apply-offset 关闭）"
        ),
        "offset_applied_ms": offset_ms,
        "geometry": result.geometry,
        "note_count": len(data.notes),
        "pointer_count": result.pointer_count,
        "peak_concurrency": result.stats.get("peak_concurrency", 0),
        "event_count": len(events),
        "plan_count": len(plan),
        "unassigned_count": len(result.unassigned),
        "unassigned": result.unassigned,
        "warnings": result.warnings,
        "stats": {
            "serves_by_stage": sch.by_type(result.serves),
            "reuse": result.stats.get("reuse", 0),
            "refused_share": result.stats.get("refused_share", 0),
            "chord_groups": result.stats.get("chord_groups", 0),
            "chord_notes": result.stats.get("chord_notes", 0),
            "chord_refused": result.stats.get("chord_refused", 0),
            "chord_coincident_px": result.stats.get("chord_coincident_px", 0.0),
            "chord_refused_detail": result.stats.get("chord_refused_detail", []),
            "frames": result.stats.get("frames", 0),
            "projected_into_screen": result.stats.get("projected_into_screen", 0),
            "retimed": result.stats.get("retimed", 0),
            "clamped": result.stats.get("clamped", 0),
            "hold_keep_clamped": result.stats.get("hold_keep_clamped", 0),
            "flick_guard": result.stats.get("flick_guard", 0),
            "flick_guard_moves": result.stats.get("flick_guard_moves", []),
            "max_pointers": args.max_pointers,
            "tangent_threshold_ratio": args.tangent_threshold,
            "presence_reuse_px": (round(args.tangent_threshold * canvas[0], 2)
                                  if presence_px is None else presence_px),
            "presence_legacy": presence_px is None,
            "flick_radius_px": result.stats.get("flick_radius_px", 0.0),
            "flick_radius_shrunk": result.stats.get("flick_radius_shrunk", 0),
            "flick_radius_min": result.stats.get("flick_radius_min"),
            "flick_shifted": result.stats.get("flick_shifted", 0),
            "flick_shift_max": result.stats.get("flick_shift_max", 0.0),
            "flick_lead_ms": args.flick_lead,
            "flick_span_ms": args.flick_span,
        },
        "constraints": {
            "action_semantics": (
                "DOWN=第一根手指按下，POINTER_DOWN=已有手指时追加，"
                "POINTER_UP=抬起后仍有手指按下，UP=最后一根抬起。"
                "同一时刻的 MOVE 可以合并成一个 MotionEvent 下发。"
            ),
            "pointer_limit": (
                f"同一时刻最多 {args.max_pointers} 个触点（Android 硬上限）。"
                "指针**不逐个任务抬起**：一根手指会连续服务一串音符（Drag / Hold 的"
                "持续段不需要 DOWN，只要判定区里有手指在即可），事件流里表现为"
                "「DOWN 少、MOVE 多」。D 段必须按整条流回放，不能按 DOWN/UP 配对切分。"
            ),
            "t0_rule": (
                "事件时刻默认 = **谱面时基 + 谱面 offset**（音乐时基，F4 默认施加，"
                "见 offset_applied_ms 字段）；播放端把时间原点对齐到锚点 zero（音乐零点）"
                "即可直接回放。若用 --no-apply-offset，则事件时刻 = 纯谱面时基。"
            ),
        },
        "plan": plan,
        "events": events,
    }

    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        print(f"写入 {out}（{len(text) / 1024:.0f} KiB）")
        print(f"  音符 {len(data.notes)} / 事件 {len(events)} / 规划 {len(plan)}")
        print(f"  指针 {result.pointer_count}（上限 {args.max_pointers}）"
              f" / 峰值并发 {result.stats.get('peak_concurrency', 0)}")
        print(f"  几何 {result.geometry}"
              + ("（精确：判定线移动 / 旋转已计入）" if result.geometry == "exact"
                 else "（退化：判定线视为不动，仅离线试跑用）"))
        st = result.stats
        print(f"  屏外处理：时间微调 {st.get('retimed', 0)}"
              f" / 法线投影 {st.get('projected_into_screen', 0)}"
              f" / 拉进屏幕 {st.get('clamped', 0)}"
              f" / 长按持续帧出屏 {st.get('hold_keep_clamped', 0)}")
        if st.get("refused_share"):
            print(f"  同帧双押：{st['refused_share']} 处 —— 同帧按下已被别的 Tap/Hold 吃掉，"
                  f"各补按一次（Phigros：一次点击只判一个 Tap/Hold）")
        if st.get("chord_groups"):
            print(f"  同刻多押：{st['chord_groups']} 组 / {st['chord_notes']} 个音符"
                  f"（重合半径 {st.get('chord_coincident_px', 0):.1f} 画布px）"
                  f" / **借用已承诺手指 {st.get('chord_refused', 0)}**")
        print(f"  手指跟随：presence 阈值 {st.get('presence_reuse_px', 0):.0f} 画布px"
              + ("（旧口径）" if st.get("presence_legacy") else "")
              + f" / Flick 半径 {st.get('flick_radius_px', 0):.0f}px"
              + (f"，压过 {st['flick_radius_shrunk']} 处"
                 f"（最小 {st.get('flick_radius_min') or 0:.1f}px）"
                 if st.get("flick_radius_shrunk") else "，无半径收缩")
              + (f"；扫动中心内推 {st['flick_shifted']} 处"
                 f"（最大 {st.get('flick_shift_max', 0):.0f}px，保位移满幅）"
                 if st.get("flick_shifted") else ""))
        if result.unassigned:
            print(f"  ⚠️ 未能分配 {len(result.unassigned)} 项")
        for w in result.warnings:
            print(f"  ⚠️ {w}")
    else:
        print(text)

    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
