#!/usr/bin/env python
"""端内注入坐标审计：把「trace 里真实注入的设备坐标」与「载荷应该算出的坐标」逐帧比对。

用法：
    python tools/verify_dev_coords.py --payload <payload.json> --trace <trace.csv>
                                      [--dst 1610x720] [--tol-px 0]
                                      [--json out/dev_coords.json]

为什么要这个工具（2026-10-24 真机血案）：
    run5/run6 两枪 Miss 60~69%，此前的结论是「注入链路两侧零责」——
    因为关键帧 1682/1682 全部送达、坐标收货侧逐帧吻合。**但那比对的是
    「我们发出去的坐标」与「靶子收到的坐标」**，从头到尾没有任何一处回答：
    *「我们算出来的坐标，是不是生产端该给的那个坐标？」*

    真相：设备上跑的是**旧缓存载荷**（`canvas` 被写成 `screen` 的 1080x2340），
    `Scheduler.toDevice` 忠实执行了错误的输入 ⇒ 每一下都按 cw=1080/ch=2340 变换，
    手指被压进屏幕中央一小块（切向偏差中位 160 px）⇒ 大量 Miss。
    而 `cachePayload` 的失效判据是**字节长度**，canvas 从 1920x1080 改成 1080x2340
    **不改变长度**（"1920.0"->"1080.0"、"1080.0"->"2340.0" 都是 6 字符）⇒ 旧缓存永远命中。

    所以本工具的核心断言只有一条：**注入坐标必须等于载荷算出的坐标**。
    不相等时它会反过来诊断「哪一套 canvas 能复现 trace」，把"断言红"变成"原因可读"。

坐标公式（必须与 `sched/Scheduler.toDevice` 逐字一致，改一边就要改另一边）：
    src -> canvas:  k = srcW/canvasW; yOff = (srcH - canvasH*k)/2; (x/k, (y-yOff)/k)
    canvas -> dst:  fit=height: k = dstH/canvasH; xOff = (dstW - canvasW*k)/2
                    (xOff + cx*k, cy*k)
    取整：Java 用 Math.rint（四舍六入五取偶），Python 的 round() 同语义。

注意：`--tol-px` 默认 **0**。载荷里坐标是 round 到 3 位的 double，端内再 rint，
正常情况误差应当 **恰好 0**；一个恒定的 1 px 偏移本身就是「两套公式有细微漂移」
的症状，不该被默认容差吞掉。>0 只在排查期做粗筛时用。
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# MotionEvent action 码 —— trace 的 act 列就是这些值
ACT_DOWN = 0
ACT_UP = 1
ACT_MOVE = 2
ACT_POINTER_DOWN = 5
ACT_POINTER_UP = 6

# 关键帧 = 不带指针索引的动作；POINTER_* 的 idx 是**数组下标**，不是 pointer id
ACT_NAME = {
    ACT_DOWN: "DOWN",
    ACT_UP: "UP",
    ACT_MOVE: "MOVE",
    ACT_POINTER_DOWN: "POINTER_DOWN",
    ACT_POINTER_UP: "POINTER_UP",
}
KEY_ACTIONS = {ACT_DOWN, ACT_UP, ACT_POINTER_DOWN, ACT_POINTER_UP}
INDEXED_ACTIONS = {ACT_POINTER_DOWN, ACT_POINTER_UP}


# ---------------------------------------------------------------- 坐标变换


def to_device(
    x: float,
    y: float,
    canvas: Tuple[float, float],
    src: Tuple[float, float],
    dst: Tuple[float, float],
    fit_height: bool = True,
) -> Tuple[int, int]:
    """载荷（screen 坐标）→ 设备像素。与 `sched/Scheduler.toDevice` 逐字对应。"""
    cw, ch = canvas
    sw, sh = src
    dw, dh = dst

    src_scale = sw / cw
    src_y_off = (sh - ch * src_scale) / 2.0
    cx = x / src_scale
    cy = (y - src_y_off) / src_scale

    if fit_height:
        dst_scale = dh / ch
        x_off = (dw - cw * dst_scale) / 2.0
        y_off = 0.0
    else:
        dst_scale = dw / cw
        x_off = 0.0
        y_off = (dh - ch * dst_scale) / 2.0

    return (round(x_off + cx * dst_scale), round(y_off + cy * dst_scale))


# ---------------------------------------------------------------- 数据读取


@dataclass(frozen=True)
class Key:
    """一帧关键事件的身份。time 用 3 位小数量化 —— 两侧 CSV 精度不同。"""

    t_ms: float
    action: str
    pid: int

    def __str__(self) -> str:
        return f"t={self.t_ms:.3f} {self.action} pid={self.pid}"


def parse_size(text: str) -> Tuple[float, float]:
    parts = text.lower().replace("*", "x").split("x")
    if len(parts) != 2:
        raise argparse.ArgumentTypeError(f"尺寸格式应为 宽x高（收到 {text!r}）")
    try:
        return float(parts[0]), float(parts[1])
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"尺寸含非数字：{text!r}") from exc


def load_payload(path: Path) -> Tuple[dict, List[dict]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    for k in ("canvas", "screen", "events"):
        if k not in payload:
            raise ValueError(f"载荷缺 {k}：{path}")
    return payload, payload["events"]


def parse_ptrs(field: str) -> List[Tuple[int, int, int]]:
    """`0:815:324|1:795:396` → [(pid, x, y), ...]。空字段返回 []。"""
    if not field or field == "-":
        return []
    out: List[Tuple[int, int, int]] = []
    for chunk in field.split("|"):
        parts = chunk.split(":")
        if len(parts) != 3:
            raise ValueError(f"ptrs 段格式错误：{chunk!r}")
        out.append((int(parts[0]), int(parts[1]), int(parts[2])))
    return out


def read_trace(path: Path) -> List[dict]:
    """读逐帧 trace。**必须容忍 CRLF**：git 在 Windows 上会转换行尾。"""
    raw = path.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")
    lines = [ln for ln in raw.split("\n") if ln.strip()]
    if not lines:
        raise ValueError(f"trace 为空：{path}")
    rows = list(csv.DictReader(lines))
    for need in ("t_ms", "act", "idx", "ptrs"):
        if need not in rows[0]:
            raise ValueError(f"trace 缺列 {need}（实际列：{list(rows[0])}）")
    return rows


def frame_pointer(row: dict) -> Optional[Tuple[int, int, int]]:
    """取「这一帧讲的是哪个指针」。POINTER_* 用 idx 下标，其余用第 0 个。

    返回 (pid, x, y)；指针列表为空时返回 None（不该发生，但要显式）。
    """
    ptrs = parse_ptrs(row["ptrs"])
    if not ptrs:
        return None
    act = int(row["act"])
    idx = int(row["idx"]) if row["idx"] not in ("", "-") else -1
    if act in INDEXED_ACTIONS:
        if not (0 <= idx < len(ptrs)):
            raise ValueError(f"idx={idx} 越界（ptrs 只有 {len(ptrs)} 个）")
        return ptrs[idx]
    return ptrs[0]


# ---------------------------------------------------------------- 比对


@dataclass
class Mismatch:
    key: Key
    expect: Tuple[int, int]
    actual: Tuple[int, int]

    @property
    def dist(self) -> float:
        return math.hypot(self.actual[0] - self.expect[0], self.actual[1] - self.expect[1])


def expected_map(
    events: Sequence[dict],
    canvas: Tuple[float, float],
    src: Tuple[float, float],
    dst: Tuple[float, float],
) -> Dict[Key, Tuple[int, int]]:
    """载荷里的关键帧 → 应注入的设备坐标。同名 key 冲突即报错（不该发生）。"""
    out: Dict[Key, Tuple[int, int]] = {}
    for e in events:
        action = e["action"]
        if action not in ("DOWN", "UP", "POINTER_DOWN", "POINTER_UP"):
            continue
        key = Key(round(e["t_ms"], 3), action, int(e["pointer_id"]))
        if key in out:
            raise ValueError(f"载荷里关键帧重复：{key}")
        out[key] = to_device(e["x"], e["y"], canvas, src, dst)
    return out


def compare(
    trace_rows: Sequence[dict],
    expect: Dict[Key, Tuple[int, int]],
    tol_px: float = 0.0,
) -> Tuple[List[Mismatch], List[Key], List[str]]:
    """返回 (超差帧, 未匹配到的 trace 帧, 提示)。

    `tol_px` 默认 **0**：载荷坐标是 round 到 3 位的 double，端内再 rint，
    正常路径上误差应当恰好是 0。给 >0 只用于排查期粗筛 —— 一个恒定的 1 px
    偏移本身就是「两套公式有细微漂移」的症状，不该被吞掉。
    """
    bad: List[Mismatch] = []
    unmatched: List[Key] = []
    notes: List[str] = []
    for row in trace_rows:
        act = int(row["act"])
        if act not in KEY_ACTIONS:
            continue
        ptr = frame_pointer(row)
        if ptr is None:
            notes.append(f"t={row['t_ms']} act={ACT_NAME.get(act, act)} 指针列表为空，跳过")
            continue
        pid, ax, ay = ptr
        key = Key(round(float(row["t_ms"]), 3), ACT_NAME[act], pid)
        exp = expect.get(key)
        if exp is None:
            unmatched.append(key)
            continue
        if math.hypot(ax - exp[0], ay - exp[1]) > tol_px:
            bad.append(Mismatch(key, exp, (ax, ay)))
    return bad, unmatched, notes


def linear_fit(points: Sequence[Tuple[float, float]]) -> Tuple[float, float]:
    """最小二乘 y = a + b·x。样本退化（x 全等）时返回 (nan, nan)。"""
    n = len(points)
    if n < 2:
        return (float("nan"), float("nan"))
    sx = sum(p[0] for p in points)
    sy = sum(p[1] for p in points)
    sxx = sum(p[0] * p[0] for p in points)
    sxy = sum(p[0] * p[1] for p in points)
    den = n * sxx - sx * sx
    if den == 0:
        return (float("nan"), float("nan"))
    b = (n * sxy - sx * sy) / den
    return ((sy - b * sx) / n, b)


def diagnose(
    payload: dict,
    events: Sequence[dict],
    trace_rows: Sequence[dict],
    dst: Tuple[float, float],
) -> List[str]:
    """断言红了以后：哪一套 canvas 能复现 trace？给出可读结论而不是只报"不匹配"。"""
    out: List[str] = []
    canvas = (payload["canvas"]["width"], payload["canvas"]["height"])
    src = (payload["screen"]["width"], payload["screen"]["height"])

    candidates = {
        "载荷 canvas（正确）": canvas,
        "载荷 screen（canvas 被写成 screen 的典型错法）": src,
    }
    for label, cand in candidates.items():
        try:
            exp = expected_map(events, cand, src, dst)
        except ValueError:
            continue
        bad, unmatched, _ = compare(trace_rows, exp, 0.0)
        total = len(exp)
        hit = total - len(bad) - len(unmatched)
        if total and hit == total:
            out.append(f"   ⇒ 用「{label}」= {cand[0]:.0f}x{cand[1]:.0f} 可以 **100% 复现** trace 坐标")
        else:
            out.append(
                f"   · 「{label}」= {cand[0]:.0f}x{cand[1]:.0f}：{hit}/{total} 帧吻合"
            )

    # 无候选命中时退一步：拟合 trace = a + b·载荷坐标，报出等效缩放
    pairs_x: List[Tuple[float, float]] = []
    pairs_y: List[Tuple[float, float]] = []
    for e in events:
        action = e["action"]
        if action not in ("DOWN", "UP", "POINTER_DOWN", "POINTER_UP"):
            continue
        key = Key(round(e["t_ms"], 3), action, int(e["pointer_id"]))
        for row in trace_rows:
            if int(row["act"]) not in KEY_ACTIONS:
                continue
            ptr = frame_pointer(row)
            if ptr is None:
                continue
            pid, ax, ay = ptr
            if Key(round(float(row["t_ms"]), 3), ACT_NAME[int(row["act"])], pid) != key:
                continue
            pairs_x.append((e["x"], ax))
            pairs_y.append((e["y"], ay))
            break
    if len(pairs_x) >= 8:
        ax_, bx_ = linear_fit(pairs_x)
        ay_, by_ = linear_fit(pairs_y)
        out.append(
            f"   · 等效拟合：x_dev = {ax_:.3f} + {bx_:.6f}·x_src ; "
            f"y_dev = {ay_:.3f} + {by_:.6f}·y_src"
        )
        if math.isfinite(by_) and by_ != 0:
            guess = dst[1] / src[1]
            verdict = "≈ dstH/srcH ⇒ **canvas 被填成了 screen**" if abs(by_ - guess) < 0.002 \
                else f"≠ dstH/srcH（{guess:.6f}）⇒ 是别的参数被改过"
            out.append(f"   · 等效缩放 {by_:.6f} {verdict}")
    return out


# ---------------------------------------------------------------- 主流程


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="端内注入坐标审计（trace vs 载荷）")
    ap.add_argument("--payload", required=True, help="端内载荷 json")
    ap.add_argument("--trace", required=True, help="端内逐帧 trace CSV")
    ap.add_argument("--dst", default="1610x720", help="设备显示尺寸，默认 1610x720")
    ap.add_argument("--tol-px", type=float, default=0.0,
                    help="允许误差（px），默认 0 = 必须逐帧完全相等")
    ap.add_argument("--json", help="把机器可读结果写到这个路径")
    return ap


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    payload_path = Path(args.payload)
    trace_path = Path(args.trace)

    payload, events = load_payload(payload_path)
    canvas = (payload["canvas"]["width"], payload["canvas"]["height"])
    src = (payload["screen"]["width"], payload["screen"]["height"])
    dst = parse_size(args.dst)
    trace_rows = read_trace(trace_path)

    print(f"载荷：{payload_path}")
    print(f"  canvas {canvas[0]:.0f}x{canvas[1]:.0f}  screen {src[0]:.0f}x{src[1]:.0f}"
          f"  事件 {len(events)}")
    print(f"trace：{trace_path}  {len(trace_rows)} 行")
    print(f"目标：{dst[0]:.0f}x{dst[1]:.0f}（fit=height）")

    expect = expected_map(events, canvas, src, dst)
    bad, unmatched, notes = compare(trace_rows, expect, args.tol_px)

    n_trace_key = sum(1 for r in trace_rows if int(r["act"]) in KEY_ACTIONS)
    print()
    print(f"关键帧：载荷 {len(expect)}，trace {n_trace_key}"
          f"，匹配上 {len(expect) - len(unmatched) if len(unmatched) <= len(expect) else 0}")
    for n in notes:
        print(f"  ⚠ {n}")

    print()
    print("-- 坐标逐帧比对 --")
    if not bad:
        print("  全部关键帧坐标 **完全一致** ✓")
    else:
        dists = sorted(m.dist for m in bad)
        print(f"  超差 {len(bad)}/{len(expect)} 帧（容差 {args.tol_px} px）")
        print(f"  |Δ| 中位 {dists[len(dists) // 2]:.1f} px，最大 {dists[-1]:.1f} px")
        for m in bad[:5]:
            print(f"    {m.key}  期望 {m.expect}  实际 {m.actual}  |Δ|={m.dist:.1f}")
        print()
        print("-- 诊断：trace 用的是哪套 canvas？ --")
        for line in diagnose(payload, events, trace_rows, dst):
            print(line)

    if unmatched:
        print()
        print(f"-- trace 里有 {len(unmatched)} 帧关键帧在载荷中找不到（不许静默）--")
        for key in unmatched[:5]:
            print(f"    {key}")

    ok = not bad and not unmatched
    print()
    print("=" * 60)
    if ok:
        print("⇒ **注入坐标与载荷一致**：端内拿到的就是生产端算出的那一套坐标。")
    else:
        print("⇒ **注入坐标与载荷不一致**：端内用的不是这份载荷（或变换参数被改过）。")
        print("   先查缓存/安装包里的载荷，别再去调 G/L —— 这不是时基问题。")

    if args.json:
        Path(args.json).write_text(
            json.dumps(
                {
                    "payload": str(payload_path),
                    "trace": str(trace_path),
                    "canvas": list(canvas),
                    "screen": list(src),
                    "dst": list(dst),
                    "n_key_payload": len(expect),
                    "n_key_trace": n_trace_key,
                    "n_mismatch": len(bad),
                    "n_unmatched": len(unmatched),
                    "ok": ok,
                    "worst": [
                        {"key": str(m.key), "expect": list(m.expect),
                         "actual": list(m.actual), "dist": round(m.dist, 3)}
                        for m in sorted(bad, key=lambda m: m.dist, reverse=True)[:20]
                    ],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
