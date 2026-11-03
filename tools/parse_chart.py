#!/usr/bin/env python
"""Phigros 官方谱 → 击打点清单（notes.json）。

用法：
    python tools/parse_chart.py <chart.json> [-o out/notes.json]
                               [--screen 1920x1080] [--aspect 1.7777778]

输出字段说明（交付给 C 段/D 段的契约）：
    t_ms      相对**谱面时间轴原点**的毫秒数；实际音乐时刻 = t_ms + offset_sec*1000
    px, py    按 --screen 换算的像素坐标，原点左上、y 向下
    nx, ny    归一化坐标（0..1），与分辨率无关
    hold_ms   Hold 音符的按住时长；其余为 0
    multiple  同一时刻是否还有其它音符需要同时击打（多指标记）
    angle_deg 判定线在击打时刻的角度（度）。C 段的**切向复用**判定要用它
    position_x 音符在判定线上的横坐标（原值）。C 段长按跟随要用它
    lines     判定线几何事件（原样带出，**未解码**）：
                  [{"bpm", "move_events": [[startTime,endTime,start,end,start2,end2],…],
                    "rotate_events": [[startTime,endTime,start,end],…]}, …]
              判定区跟着判定线移动 / 旋转，C 段规划长按的跟随路径必须知道它。
              原样带出（而不是先解码）是为了让验收脚本能**独立解码**并与 C 段对拍；
              formatVersion 1 的 `1000·x + y` 打包由消费方按根字段 `format_version` 自行还原。
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

from pgauto.chart import (  # noqa: E402
    DEFAULT_ASPECT_RATIO,
    DEFAULT_SCREEN,
    Chart,
    load,
)


def parse_screen(text: str) -> Tuple[float, float]:
    parts = text.lower().replace("*", "x").split("x")
    if len(parts) != 2:
        raise argparse.ArgumentTypeError(f"屏幕尺寸格式应为 宽x高，例如 1920x1080（收到 {text!r}）")
    try:
        return float(parts[0]), float(parts[1])
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"屏幕尺寸含非数字：{text!r}") from exc


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="解析 Phigros 官方谱，输出击打点清单")
    ap.add_argument("chart", help="谱面 JSON 路径")
    ap.add_argument("-o", "--out", help="输出文件路径；省略则打印到标准输出")
    ap.add_argument(
        "--screen",
        type=parse_screen,
        default=DEFAULT_SCREEN,
        help=f"屏幕尺寸，形如 1920x1080（默认 {int(DEFAULT_SCREEN[0])}x{int(DEFAULT_SCREEN[1])}）",
    )
    ap.add_argument(
        "--aspect",
        type=float,
        default=DEFAULT_ASPECT_RATIO,
        help=f"画面宽高比（默认 {DEFAULT_ASPECT_RATIO:.6f}，即 16:9）",
    )
    return ap


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    chart_path = Path(args.chart)
    if not chart_path.is_file():
        print(f"谱面不存在：{chart_path}", file=sys.stderr)
        return 2

    chart = Chart(load(chart_path), aspect_ratio=args.aspect, screen=args.screen)
    hits = chart.notes()

    payload = {
        "chart": chart_path.name,
        # F3（2026-10-13）：曲目录名（= margin_catalog 的 key 同构）——
        # 让 touches.json 能自带「歌」，play.py 据此反查锚定档，不依赖短名别名表。
        "song_dir": chart_path.parent.name,
        "format_version": chart.format_version,
        "offset_sec": chart.offset,
        "aspect_ratio": chart.aspect_ratio,
        "screen": {"width": chart.screen_w, "height": chart.screen_h},
        "note_count": len(hits),
        # F4（2026-10-13 / 判决 2026-10-14）：t_ms 是**几何时基**（判定线几何要按它求，勿改）。
        # 谱面 offset 的应用在 **schedule 步**（tools/schedule_chart.py）：**默认施加**
        # （真机 A/B：零號車輛 IN offset=+1000ms 施加后 49.83%→99.70%）；
        # `--no-apply-offset` 可关闭（复现旧口径用）。
        "t0_rule": "t_ms 为谱面几何时基（音乐时刻 = t_ms + offset_sec×1000；offset 由 schedule 步施加）",
        "lines": chart.line_events_payload(),
        "notes": [
            {
                "type": n.type,
                "type_name": n.type_name,
                "t_ms": round(n.t_ms, 4),
                "hold_ms": round(n.hold_sec * 1000.0, 4),
                "line": n.line,
                "above": n.above,
                "multiple": n.multiple,
                "px": round(n.px, 3),
                "py": round(n.py, 3),
                "nx": round(n.px / chart.screen_w, 6),
                "ny": round(n.py / chart.screen_h, 6),
                "world_x": round(n.world_x, 6),
                "world_y": round(n.world_y, 6),
                "angle_deg": round(chart.line_state(n.line, n.t_sec)[2], 6),
                "position_x": n.position_x,
            }
            for n in hits
        ],
    }

    text = json.dumps(payload, ensure_ascii=False, indent=2)

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        print(f"写入 {out}（{len(hits)} 个音符）")
    else:
        print(text)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
