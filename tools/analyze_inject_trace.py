#!/usr/bin/env python3
"""分析端内注入的逐帧追踪（`inject_trace.csv`），定位 Miss 丢在哪一层。

背景
----
2026-10-24 S1-d 端内首跑：Bounded Quietude IN 16，注入 2769 条 / false 0 /
关键事件 lag p50 2.3 ms —— 三个数全绿，成绩却是 40.73%、701 Miss。
汇总指标只能证伪「完全没发」，说明不了「多指帧有没有被派发」。而载荷里
POINTER_DOWN 占 47.09%、Miss 占 54.5%，吻合到不像巧合，却定不了案。
⇒ 本脚本消费 `InjectTrace` 落的逐帧 CSV，把四个候选机制分开。

四个假设（判据都是自定阈值，见下面的 *支持/否定* 规则，改了就是另一套结论）
------------------------------------------------------------------------
H1 通道：`injectInputEvent` 对多指帧返回 false（入队就被拒）
H2 陈旧：多指帧的 eventTime 明显落后（`et_late_ms` 大）⇒ 被当 stale 丢弃
H3 倒退：eventTime 不单调（`et_back_ms > 0`）⇒ 被 InputDispatcher 丢弃
H4 结构：POINTER_DOWN/UP 的 actionIndex 越界或不在末尾 ⇒ 非法帧必丢
H5 断裂：**跨帧**指针流不变量被破坏（单帧看都合法，串起来不是一次触摸）
（以上全否 ⇒ 事件确实发出且合法，问题在派发之后 ⇒ 走「单指串行」A/B）

H4 与 H5 的区别值得单独记住：H4 只看一帧内部（idx 在不在范围、是不是末尾），
H5 看两帧之间（指针数变化对不对、抬起的指是不是真按着）。10-24 第六枪
H1/H2/H4 全否而 Miss 780，唯一能同时解释「单帧合法 + 整串无效」的就是 H5 一类。

字段契约见 `android/injector/.../sched/InjectTrace.java` 的 HEADER。
`-` 表示缺值（如首帧的 et_back_ms）。⛔ 本脚本**绝不**把 `-` 当 0：
缺值会单独计数并打印，因为「零落后」与「没有上一帧」是两件事。

用法
----
    python tools/analyze_inject_trace.py                    # 默认路径
    python tools/analyze_inject_trace.py <csv>
    python tools/analyze_inject_trace.py <csv> --miss 780   # 带量级对照（推荐）
    python tools/analyze_inject_trace.py <csv> --json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

COLS = ("seq,t_ms,act,idx,n,ptrs,ret,cost_us,late_ms,"
        "et_late_ms,et_back_ms").split(",")

# 动作码（android.view.MotionEvent）
ACT = {0: "DOWN", 1: "UP", 2: "MOVE", 3: "CANCEL", 5: "POINTER_DOWN", 6: "POINTER_UP"}

DEFAULT_CSV = Path("tmp/inject_trace.csv")

# ── 判定阈值（自定，不是任何规范值；改这里就是换一套结论口径）─────────────
#: H1：多指 false 率 ≥ 这个值，且至少比单指高 5 个百分点，才算「通道拒绝」
H1_MULTI_FALSE = 0.05
H1_GAP = 0.05
#: H2：多指帧里 eventTime 落后 > 这个毫秒数的比例。关键帧是忙等到时刻再发的，
#:     正常情况下应该几乎为 0；成片出现说明「发的时候时间戳已经过期」。
H2_STALE_MS = 20
H2_RATE = 0.05
#: H4：结构非法帧占比
H4_RATE = 0.001
#: H5：跨帧指针流断裂帧占比
H5_RATE = 0.001
#: 按下时长统计里的「可能看不见」阈值（60 Hz 一帧 = 16.7 ms）。**只用于报告**：
#: 它不是判定，因为 PC 侧用同一份载荷（78% 的按下只持续 1 ms）能打 φ。
SHORT_PRESS_MS = 17


# ─────────────────────────── 读文件 ───────────────────────────

class TraceError(Exception):
    pass


def _num(s: str):
    """解析数值；`-` 或缺值返回 None（**绝不退化成 0**）。"""
    s = s.strip()
    if s in ("", "-"):
        return None
    return float(s) if "." in s else int(s)


def load(path: Path):
    """→ (rows, notes, header_seen)。列数不符直接抛错（契约破坏）。"""
    if not path.is_file():
        raise TraceError(f"找不到 {path}（先跑一轮端内注入，再 adb pull）")
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    rows, notes, header_seen = [], [], False
    for ln, line in enumerate(lines, 1):
        if not line.strip():
            continue
        if line.lstrip().startswith("#"):
            notes.append(line.strip())
            continue
        parts = line.split(",")
        if not header_seen and tuple(parts) == tuple(COLS):
            header_seen = True
            continue
        if len(parts) != len(COLS):
            raise TraceError(
                f"第 {ln} 行列数 {len(parts)} ≠ {len(COLS)}（locale 破列？未加 Locale.ROOT？）\n"
                f"  {line[:160]}")
        rows.append({
            "seq": int(parts[0]),
            "t_ms": float(parts[1]),
            "act": int(parts[2]),
            "idx": int(parts[3]),
            "n": int(parts[4]),
            "ptrs": parts[5],
            "ret": int(parts[6]),
            "cost_us": _num(parts[7]),
            "late_ms": _num(parts[8]),
            "et_late_ms": _num(parts[9]),
            "et_back_ms": _num(parts[10]),
        })
    if not rows:
        raise TraceError(f"{path} 里没有数据行（只有表头？{len(notes)} 条注释）")
    return rows, notes, header_seen


# ─────────────────────────── 统计 ───────────────────────────

def pct(vals, q):
    """nearest-rank。None（缺值）不参与，也不当 0。"""
    xs = [v for v in vals if v is not None]
    if not xs:
        return None
    xs.sort()
    i = min(len(xs) - 1, max(0, round(q * (len(xs) - 1))))
    return xs[i]


def summarize(rows):
    """单组（单指 / 多指 / 某动作）的画像。"""
    n = len(rows)
    if n == 0:
        return None
    late = [r["late_ms"] for r in rows]
    etl = [r["et_late_ms"] for r in rows]
    costs = [r["cost_us"] for r in rows]
    backs = [r["et_back_ms"] for r in rows]
    return {
        "n": n,
        "false": sum(1 for r in rows if r["ret"] == 0),
        "n_frames_ge2": sum(1 for r in rows if r["n"] >= 2),
        "late_p50": pct(late, 0.50), "late_p95": pct(late, 0.95),
        "late_max": pct(late, 1.0),
        "etl_p50": pct(etl, 0.50), "etl_p95": pct(etl, 0.95),
        "etl_max": pct(etl, 1.0),
        "etl_gt_thr": sum(1 for v in etl if v is not None and v > H2_STALE_MS),
        "etl_missing": sum(1 for v in etl if v is None),
        "cost_p50_us": pct(costs, 0.50), "cost_max_us": pct(costs, 1.0),
        "cost_missing": sum(1 for v in costs if v is None),
        "back_gt0": sum(1 for v in backs if v is not None and v > 0),
        "back_missing": sum(1 for v in backs if v is None),
        "back_max": pct([b for b in backs if b is not None], 1.0),
    }


def structure_errors(rows):
    """H4：MotionEvent 结构非法/可疑的帧。"""
    bad = {"idx_ge_n": [], "pd_idx_not_last": [], "pu_idx_ge_n": [], "n_lt1": []}
    for r in rows:
        n, idx, act = r["n"], r["idx"], r["act"]
        if n < 1:
            bad["n_lt1"].append(r["seq"])
        if act in (5, 6):
            if idx < 0 or idx >= n:
                bad["idx_ge_n"].append(r["seq"])
            if act == 5 and idx != n - 1:
                # Gesture 总是把新指针 append 到末尾 ⇒ idx 应为 n-1。
                # 不等就意味着「新指针插在中间」，dispatcher 看到的顺序与状态机不符。
                bad["pd_idx_not_last"].append(r["seq"])
            if act == 6 and idx >= n:
                bad["pu_idx_ge_n"].append(r["seq"])
    return bad


def _ids(ptrs: str):
    """`0:815:324|1:795:396` → [0, 1]（顺序即事件里的 pointer index 顺序）。"""
    return [int(p.split(":")[0]) for p in ptrs.split("|") if p.strip()]


def pointer_stream_errors(rows):
    """H5：跨帧指针流不变量。单帧合法但串起来非法的情况，都在这里。

    依据 AOSP `MotionEvent` 语义（每条都能让 dispatcher / 游戏侧认为
    「这一串不是同一次触摸」）：

      · `ACTION_DOWN`：只能 1 个指针，且不许有别的指头还按着
      · `ACTION_POINTER_DOWN`：指针数 = 前 + 1，`idx` 指向**新增**的那个，
        旧指针必须原样保留
      · `ACTION_POINTER_UP`：指针数 = 前（**抬起的指还在事件里**），`idx` 指向它，
        事件之后它才消失
      · `ACTION_UP`：只剩 1 个指针
      · `ACTION_MOVE`：指针集与指针数都不许变

    ⛔ 本函数第一版把 `POINTER_UP` 写成「n = 前 − 1」，于是 1966/2775 帧全报违规
    —— 判据错会冤枉真数据。所以每条规则都有对应的断言用例（见
    `tests/test_inject_trace.py::test_pointer_up_*`）。
    """
    bad = {"down_with_finger": [], "down_n": [], "pd_n": [], "pd_new_not_one": [],
           "pd_lost_old": [], "pu_n": [], "pu_set": [], "pu_idx": [],
           "up_n": [], "up_unknown": [], "move_n": [], "move_set": [], "unknown_act": []}
    down = set()
    for r in rows:
        act, idx, n = r["act"], r["idx"], r["n"]
        ids = _ids(r["ptrs"])
        idset = set(ids)
        if n != len(ids):
            # 事件声称的指针数与 ptrs 列的长度不符 ⇒ 后面所有判断都不可信
            bad["unknown_act"].append(r["seq"])
            continue
        if act == 0:                                   # ACTION_DOWN
            if n != 1:
                bad["down_n"].append(r["seq"])
            if down:
                bad["down_with_finger"].append(r["seq"])
            down = set(ids)
        elif act == 5:                                 # ACTION_POINTER_DOWN
            if n != len(down) + 1:
                bad["pd_n"].append(r["seq"])
            new = [i for i in ids if i not in down]
            if len(new) != 1:
                bad["pd_new_not_one"].append(r["seq"])
            elif ids.index(new[0]) != idx:
                bad["pd_new_not_one"].append(r["seq"])
            elif not down <= idset:
                bad["pd_lost_old"].append(r["seq"])
            down = idset
        elif act == 6:                                 # ACTION_POINTER_UP
            if n != len(down):
                bad["pu_n"].append(r["seq"])
            if idset != down:
                bad["pu_set"].append(r["seq"])
            if not (0 <= idx < n):
                bad["pu_idx"].append(r["seq"])
            else:
                down = idset - {ids[idx]}
        elif act == 1:                                 # ACTION_UP
            if n != 1:
                bad["up_n"].append(r["seq"])
            if not idset <= down:
                bad["up_unknown"].append(r["seq"])
            down = set()
        elif act == 2:                                 # ACTION_MOVE
            if n != len(down):
                bad["move_n"].append(r["seq"])
            if idset != down:
                bad["move_set"].append(r["seq"])
        else:
            bad["unknown_act"].append(r["seq"])
    return bad


def press_durations(rows):
    """每次按下持续多久（用**计划** t_ms，排除注入抖动）。

    这条统计是为「一帧之差」服务的：60 Hz 下 16.7 ms 就是一帧的宽度，
    持续时间短于它的按下，在按帧采样的游戏里可能整段落在两帧之间。
    ⛔ 但它**只是报告，不是判定**：PC 侧用同一份载荷（78% 的按下只有 1 ms）
       照样打出 φ ⇒ 短按本身不构成 Miss 的充分解释。
    """
    cur, out = {}, []
    for r in rows:
        t = r["t_ms"]
        ids = _ids(r["ptrs"])
        if r["act"] == 0 and ids:
            cur[ids[0]] = t
        elif r["act"] == 5:
            for i in ids:
                cur.setdefault(i, t)
        elif r["act"] == 6:
            idx = r["idx"]
            if 0 <= idx < len(ids) and ids[idx] in cur:
                out.append(t - cur.pop(ids[idx]))
        elif r["act"] == 1:
            for i in list(cur):
                out.append(t - cur.pop(i))
            cur.clear()
    return out

# ─────────────────────────── 报告 ───────────────────────────

def _f(v, unit="", nd=2):
    if v is None:
        return "  -"          # 缺值：不冒充 0
    if isinstance(v, float):
        return f"{v:.{nd}f}{unit}"
    return f"{v}{unit}"


def _rate(a, b):
    return f"{a}/{b} ({100.0 * a / b:.2f}%)" if b else f"{a}/0"


def report(rows, notes, path, miss=None):
    single = [r for r in rows if r["n"] == 1]
    multi = [r for r in rows if r["n"] >= 2]
    s_s, s_m = summarize(single), summarize(multi)
    bad = structure_errors(rows)
    stream = pointer_stream_errors(rows)
    holds = press_durations(rows)

    print(f"== {path} ==")
    print(f"帧 {len(rows)}：单指 {len(single)} / 多指 {len(multi)}"
          f"（多指占 {100.0 * len(multi) / len(rows):.2f}%）")
    if notes:
        for nt in notes:
            print(f"  [{nt}]")

    print("\n-- 按动作 --")
    print(f"{'动作':14s}{'帧':>7s}{'false':>7s}{'late p50':>10s}{'late p95':>10s}"
          f"{'et_late p95':>13s}{'倒退':>6s}{'cost p50':>10s}")
    for act in (0, 5, 6, 2, 1, 3):
        s = summarize([r for r in rows if r["act"] == act])
        if not s:
            continue
        print(f"{ACT.get(act, str(act)):14s}{s['n']:>7d}{s['false']:>7d}"
              f"{_f(s['late_p50']):>10s}{_f(s['late_p95']):>10s}"
              f"{_f(s['etl_p95']):>13s}{s['back_gt0']:>6d}"
              f"{_f(s['cost_p50_us'] / 1000.0):>10s}")

    print("\n-- 单指 vs 多指（核心对照）--")
    hdr = f"{'':10s}{'帧':>7s}{'false':>7s}{'et_late p50':>13s}{'et_late p95':>13s}" \
          f"{'>20ms':>8s}{'倒退':>6s}{'late p50':>10s}{'cost p95 ms':>13s}"
    print(hdr)
    for name, s in (("单指 n=1", s_s), ("多指 n>=2", s_m)):
        if not s:
            continue
        print(f"{name:10s}{s['n']:>7d}{s['false']:>7d}"
              f"{_f(s['etl_p50']):>13s}{_f(s['etl_p95']):>13s}"
              f"{s['etl_gt_thr']:>8d}{s['back_gt0']:>6d}"
              f"{_f(s['late_p50']):>10s}"
              f"{_f((s['cost_max_us'] or 0) / 1000.0):>13s}")
    # 缺值必须显式汇报（「没有上一帧」≠「零倒退」）
    missing = [(nm, s["etl_missing"], s["cost_missing"], s["back_missing"])
               for nm, s in (("单指", s_s), ("多指", s_m)) if s]
    for nm, a, b, c in missing:
        if a or b or c:
            print(f"  ⚠ {nm}缺值：et_late {a} / cost {b} / et_back {c} 帧"
                  f"（et_back 首帧必缺，属正常）")

    print("\n-- 指针数分布 --")
    from collections import Counter
    for k, v in sorted(Counter(r["n"] for r in rows).items()):
        print(f"  n={k}: {v}")

    print("\n-- H5 跨帧指针流（单帧合法 ≠ 整串合法）--")
    tot5 = sum(len(v) for v in stream.values())
    if tot5 == 0:
        print("  违规 0 帧 ⇒ 整串与 AOSP 触摸语义自洽")
    else:
        print(f"  违规 {tot5}/{len(rows)} 帧（{100.0 * tot5 / len(rows):.2f}%）")
        for k, v in stream.items():
            if v:
                print(f"    {k}: {len(v)} 帧，首例 seq={v[0]}")

    print("\n-- 按下时长（计划时间，非注入抖动；仅报告不作判定）--")
    if holds:
        lt = sum(1 for d in holds if d < SHORT_PRESS_MS)
        print(f"  按下 {len(holds)} 次：中位 {_f(pct(holds, 0.5))} ms，"
              f"p95 {_f(pct(holds, 0.95))} ms，max {_f(pct(holds, 1.0))} ms")
        print(f"  短于 {SHORT_PRESS_MS} ms：{_rate(lt, len(holds))}"
              f"  ⛔ 短按不代表 Miss：PC 侧同一份载荷同样短按却能打 φ")
    else:
        print("  没有完整的按下→抬起对（trace 被截断？）")

    print("\n-- 结论 --")
    verdicts = {}

    # H1 通道
    if s_s and s_m:
        rf_m = s_m["false"] / s_m["n"]
        rf_s = s_s["false"] / s_s["n"]
        hit = rf_m >= H1_MULTI_FALSE and rf_m - rf_s >= H1_GAP
        verdicts["H1"] = hit
        print(f"H1 通道拒绝：多指 false {_rate(s_m['false'], s_m['n'])} vs "
              f"单指 {_rate(s_s['false'], s_s['n'])} ⇒ {'支持' if hit else '否定'}"
              f"（阈 ≥{H1_MULTI_FALSE:.0%} 且高出 ≥{H1_GAP:.0%}）")
    else:
        verdicts["H1"] = None
        print("H1 通道拒绝：无对照组（全是单指或全是多指）⇒ 无法判断")

    # H2 陈旧
    if s_m:
        rm = s_m["etl_gt_thr"] / s_m["n"]
        rs = (s_s["etl_gt_thr"] / s_s["n"]) if s_s else 0.0
        hit = rm >= H2_RATE and rm > rs
        verdicts["H2"] = hit
        print(f"H2 陈旧事件：多指 et_late>{H2_STALE_MS}ms {_rate(s_m['etl_gt_thr'], s_m['n'])} vs "
              f"单指 {_rate(s_s['etl_gt_thr'], s_s['n']) if s_s else '0/0'}"
              f" ⇒ {'支持' if hit else '否定'}（阈 ≥{H2_RATE:.0%} 且高于单指）")
    else:
        verdicts["H2"] = None
        print("H2 陈旧事件：没有多指帧 ⇒ 无法判断")

    # H3 倒退
    nb = sum(s["back_gt0"] for s in (s_s, s_m) if s)
    nb_m = s_m["back_gt0"] if s_m else 0
    hit3 = nb > 0
    verdicts["H3"] = hit3
    print(f"H3 时间戳倒退：全轮 {nb} 帧 eventTime 小于上一帧（多指 {nb_m}）"
          f" ⇒ {'支持（必须修，属非法事件）' if hit3 else '否定'}")

    # H4 结构
    tot4 = sum(len(v) for v in bad.values())
    hit4 = tot4 / len(rows) >= H4_RATE
    verdicts["H4"] = hit4
    detail = "，".join(f"{k} {len(v)}" for k, v in bad.items() if v)
    print(f"H4 结构非法：{_rate(tot4, len(rows))}"
          f"{'（' + detail + '）' if detail else ''}"
          f" ⇒ {'支持' if hit4 else '否定'}（阈 ≥{H4_RATE:.1%}）")

    # H5 跨帧指针流
    tot5 = sum(len(v) for v in stream.values())
    hit5 = tot5 / len(rows) >= H5_RATE
    verdicts["H5"] = hit5
    d5 = "，".join(f"{k} {len(v)}" for k, v in stream.items() if v)
    print(f"H5 指针流断裂：{_rate(tot5, len(rows))}"
          f"{'（' + d5 + '）' if d5 else ''}"
          f" ⇒ {'支持' if hit5 else '否定'}（阈 ≥{H5_RATE:.1%}）")

    print()
    if verdicts.get("H5"):
        print("⇒ 跨帧指针流断了（H5）：dispatcher 会认为这不是同一次触摸而整串丢弃。"
              "修 Gesture 的状态机，别动坐标/判定参数。")
    elif verdicts.get("H3") or verdicts.get("H4") or verdicts.get("H1"):
        print("⇒ 有确定性缺陷（H1/H3/H4）：这些是**非法事件，该修**；但先看下面的"
              "量级对照——够不够解释本轮 Miss，再决定先动哪一处。")
    elif verdicts.get("H2"):
        print("⇒ 事件合法但时间戳过期（H2）：修 eventTime 生成（不要发过旧的时间戳），"
              "这仍不是坐标问题。")
    elif all(v is False for v in verdicts.values() if v is not None):
        print("⇒ 四假设全否：事件**入了队、时间戳有效、结构合法**。"
              "问题在派发之后（多指被合并/被游戏侧识别不到）⇒ 下一步走「单指串行」A/B，"
              "不要再调 G / L / 坐标。")
    else:
        print("⇒ 证据不足（对照组缺失）：这一轮的帧类型不构成对照，需要重打一轮。")

    # ── 量级对照：让「支持」的假设接受「够不够解释本轮 Miss」的检验 ──
    # 10-24 第六枪就是在这里差点被带偏：H3 只有 5 帧，却因为「支持」而被当成
    # 「先修它」。5 帧最多解释 5 个音符，而本轮 Miss 780 —— 数量级差 150 倍。
    if miss is not None:
        cap = {
            "H1": (s_m["false"] if s_m else 0),
            "H2": (s_m["etl_gt_thr"] if s_m else 0),
            "H3": nb,
            "H4": tot4,
            "H5": tot5,
        }
        can = {k: v for k, v in cap.items() if verdicts.get(k) and v}
        print(f"\n-- 量级对照（实际 Miss {miss}）--")
        if not can:
            print("  没有「支持」的假设 ⇒ 事件发出一侧全部清白，别在这层找原因。")
        for k, v in can.items():
            print(f"  {k} 至多解释 {v} 个音符 = Miss 的 {100.0 * v / miss:.2f}%")
        if can and max(can.values()) / max(1, miss) < 0.10:
            print("  ⇒ 数量级差一个量级以上：这些都**不是主因**，修它们不会让成绩变好；"
                  "主因在派发之后（游戏侧识别/坐标/判定），或还没被这五个假设覆盖。")

    return {"n_frames": len(rows), "n_single": len(single), "n_multi": len(multi),
            "single": s_s, "multi": s_m, "verdicts": verdicts,
            "structure": {k: len(v) for k, v in bad.items()},
            "stream": {k: len(v) for k, v in stream.items()},
            "press": {"n": len(holds),
                      "median_ms": pct(holds, 0.5),
                      "lt_short_ms": sum(1 for d in holds if d < SHORT_PRESS_MS)}}


def main(argv=None):
    ap = argparse.ArgumentParser(description="分析注入逐帧追踪 CSV")
    ap.add_argument("csv", nargs="?", type=Path, default=DEFAULT_CSV)
    ap.add_argument("--json", action="store_true", help="只输出 JSON 摘要")
    ap.add_argument("--miss", type=int, default=None,
                    help="本轮实际 Miss 数（结算页第三格）；给了才能做量级对照")
    a = ap.parse_args(argv)
    try:
        rows, notes, seen = load(a.csv)
    except TraceError as e:
        print(f"✗ {e}", file=sys.stderr)
        return 2
    if not seen:
        print("⚠ 文件里没有表头行（版本对不上？）", file=sys.stderr)
    summary = report(rows, notes, a.csv, miss=a.miss)
    if a.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
