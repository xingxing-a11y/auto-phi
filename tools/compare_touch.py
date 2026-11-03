#!/usr/bin/env python
"""端内注入「发送侧 vs 收货侧」逐帧对账。

## 这个工具回答什么问题

上一轮（10-24 第六枪，31.47% / 780 Miss）手上只有**发送侧**数据：
2775 帧、`ret=false` 0、跨帧指针流 0 违规 ⇒ 只能推到「发送侧清白」为止。
因为 `injectInputEvent` 返回 true 从来不等于「有 App 收到」（这条已在 FACTS 记过两次）。

本工具把两侧的逐帧 CSV 摆在一起，回答唯一剩下的那个问题：

    这 2775 帧，靶子到底收到了多少？丢的话丢在哪一类动作上？

## 两种输入

- 发送侧 `inject_trace.csv`（端内 `InjectTrace` 落盘）：
  `seq,t_ms,act,idx,n,ptrs,ret,cost_us,late_ms,et_late_ms,et_back_ms`
- 收货侧 `touch_recv.csv`（端内 `RecvTrace` 落盘）：
  `seq,recv_ms,et_ms,act,idx,n,ptrs,dly_ms`

两侧的 `act/idx/n/ptrs` 是同一套编码（`ptrs` = `id:x:y|id:x:y`），所以能逐帧比。

## 为什么关键帧严格对齐、MOVE 只比数量

连续 MOVE 被 InputDispatcher 合并是**正常**行为（10-21 实测 5 发 3 收，FACTS §2472）。
把它算成"丢帧"会得出「派发层丢了一半」的假结论。而 DOWN/POINTER_DOWN/
POINTER_UP/UP 是**不能**被合并的：少一条就是真的少一条。

所以：
- 关键帧（act ∈ {DOWN, UP, POINTER_DOWN, POINTER_UP}）做**有序严格匹配**
  `(act, idx, n)`，缺一条就点名；
- MOVE 只比数量，算合并率。

## 用法

    python tools/compare_touch.py <send.csv> <recv.csv> [--notes N] [--json]
    python tools/compare_touch.py tmp/_trace_run6.csv tmp/_recv.csv --notes 1286

`--notes` 给谱面音符数，用来把"丢了多少帧"换算成"能解释多少 Miss"。
不传就只报绝对值。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

SEND_COLS = ["seq", "t_ms", "act", "idx", "n", "ptrs", "ret",
             "cost_us", "late_ms", "et_late_ms", "et_back_ms"]
RECV_COLS = ["seq", "recv_ms", "et_ms", "act", "idx", "n", "ptrs", "dly_ms"]

ACT_DOWN, ACT_UP, ACT_MOVE = 0, 1, 2
ACT_CANCEL, ACT_PDOWN, ACT_PUP = 3, 5, 6
KEY_ACTS = {ACT_DOWN, ACT_UP, ACT_PDOWN, ACT_PUP}

ACT_NAME = {
    ACT_DOWN: "DOWN",
    ACT_UP: "UP",
    ACT_MOVE: "MOVE",
    ACT_CANCEL: "CANCEL",
    ACT_PDOWN: "POINTER_DOWN",
    ACT_PUP: "POINTER_UP",
}

#: 判定阈值：关键帧到达率低于它就算「派发层有罪」。
#: 为什么不是 99.9%：注入本身有 p99 21 ms 的抖动，极端情况下末尾帧可能被
#: 后一条事件的 UP 吞掉（指针已经不在数组里），属于合法合并。留 0.1% 的余量。
KEY_ARRIVAL_MIN = 0.999
#: 收货侧比发送侧多出来的关键帧占比超过它 ⇒ 输入流被污染（多半是上一次残留）
KEY_EXTRA_MAX = 0.001


class TraceError(Exception):
    """CSV 不符合契约（列数不对、表头不认识）。"""


@dataclass
class Table:
    rows: list[dict] = field(default_factory=list)
    truncated: int = 0
    path: str = ""

    @property
    def frames(self) -> int:
        return len(self.rows)


def _read(path: str, cols: list[str], label: str) -> Table:
    p = Path(path)
    if not p.is_file():
        raise TraceError(f"{label} 文件不存在：{path}")
    lines = [l for l in p.read_text(encoding="utf-8", errors="replace").splitlines() if l.strip()]
    if not lines:
        raise TraceError(f"{label} 文件是空的：{path}")

    header = lines[0].strip()
    # 表头必须认识：列序会随版本变，用名字定位而不是位置
    want = ",".join(cols)
    if header != want:
        raise TraceError(
            f"{label} 表头不符。\n  期望：{want}\n  实得：{header}\n"
            f"  ⇒ 端内落盘版本与 {Path(__file__).name} 不一致，先对齐列契约。")

    t = Table(path=path)
    for i, line in enumerate(lines[1:], start=2):
        raw = line.strip()
        if raw.startswith("#"):
            # 截断说明行：`# truncated N lines (cap M)`
            parts = raw.split()
            if len(parts) >= 3 and parts[1] == "truncated":
                try:
                    t.truncated += int(parts[2])
                except ValueError:
                    pass
            continue
        f = raw.split(",")
        if len(f) != len(cols):
            raise TraceError(f"{label} 第 {i} 行有 {len(f)} 列，应为 {len(cols)}：{raw[:120]}")
        row = dict(zip(cols, f))
        for k in ("seq", "act", "idx", "n"):
            try:
                row[k] = int(row[k])
            except (TypeError, ValueError):
                raise TraceError(f"{label} 第 {i} 行的 {k} 不是整数：{row[k]!r}")
        row["ptrs"] = _parse_ptrs(row.get("ptrs", ""))
        t.rows.append(row)
    return t


def _parse_ptrs(s: str) -> list[tuple[int, int, int]]:
    """`id:x:y|id:x:y` → [(id, x, y)]。空串 = 无指针（正常不该出现）。"""
    out: list[tuple[int, int, int]] = []
    for part in s.split("|"):
        part = part.strip()
        if not part:
            continue
        bits = part.split(":")
        if len(bits) != 3:
            raise TraceError(f"ptrs 字段格式错（应 id:x:y）：{part!r}")
        out.append((int(bits[0]), int(bits[1]), int(bits[2])))
    return out


def key_sig(row: dict) -> tuple[int, int, int]:
    """关键帧签名。MOVE 的 idx 恒为 -1，所以不用特判。"""
    return (row["act"], row["idx"], row["n"])


def _blocks(send_sigs: list, recv_sigs: list) -> list[tuple[int, int, int]]:
    """最长匹配块 (a, b, size)。三处对齐（align / buckets / coord_drift）共用它。"""
    import difflib
    return difflib.SequenceMatcher(None, send_sigs, recv_sigs,
                                   autojunk=False).get_matching_blocks()


def _match(send_sigs: list, recv_sigs: list) -> tuple[set[int], set[int]]:
    """返回两侧「已匹配」的下标集合。

    ★ 为什么不用双指针贪心（第一版就是，被测试抓到错）：
      删掉中间一条 POINTER_DOWN 后，贪心会拿**下一段**的 POINTER_DOWN 顶替它，
      于是错位吞掉后面的帧，把 1 条缺失报成 3~4 条，还指错位置。
      最有诊断价值的恰恰是「最早丢在哪」，错位会把它毁掉。
      difflib 的匹配块能正确处理中间删除，且对这种严格保序序列足够快
      （1700 个关键帧最坏 O(n*m)≈3M 次比较）。
    """
    ms: set[int] = set()
    mr: set[int] = set()
    for a, b, size in _blocks(send_sigs, recv_sigs):
        for k in range(size):
            ms.add(a + k)
            mr.add(b + k)
    return ms, mr


def _matched_pairs(sk: list[dict], rk: list[dict]) -> list[tuple[dict, dict]]:
    """按匹配块展开成逐帧配对（坐标比对用）。"""
    out = []
    for a, b, size in _blocks([key_sig(x) for x in sk], [key_sig(x) for x in rk]):
        for k in range(size):
            out.append((sk[a + k], rk[b + k]))
    return out


def align(send: Table, recv: Table) -> dict:
    sk = [r for r in send.rows if r["act"] in KEY_ACTS]
    rk = [r for r in recv.rows if r["act"] in KEY_ACTS]

    ms, mr = _match([key_sig(x) for x in sk], [key_sig(x) for x in rk])
    missing = [{"at_send_index": i, "seq": sk[i]["seq"], "sig": key_sig(sk[i]),
                "t_ms": sk[i].get("t_ms", ""), "ptrs": sk[i]["ptrs"]}
               for i in range(len(sk)) if i not in ms]
    extra = len(rk) - len(mr)

    # 严格口径缺了、但 (act, n) 能在收货侧**未匹配**的帧里找到 ⇒ 不是丢帧，是 idx 不符。
    # 用 Counter 精确配对，而不是再跑一遍序列对齐 —— 后者会把"多出来的那条"重复利用。
    loose_pool = Counter((rk[i]["act"], rk[i]["n"]) for i in range(len(rk)) if i not in mr)
    sig_only = 0
    for m in missing:
        key = (m["sig"][0], m["sig"][2])
        if loose_pool[key] > 0:
            loose_pool[key] -= 1
            sig_only += 1

    return {
        "send_keys": len(sk),
        "recv_keys": len(rk),
        "matched": len(ms),
        "missing": missing,
        "missing_n": len(missing),
        "extra": max(0, extra),
        "loose_missing": len(missing) - sig_only,
        "sig_only_mismatch": sig_only,
    }


def buckets(send: Table, recv: Table, n_buckets: int = 20) -> list[dict]:
    """按发送进度分桶算到达率 —— 用来发现「越打越糟」（累积滞后/抽稀失衡）。

    ★ 必须与 {@link align} 用同一套匹配：两处各写一遍对齐，就会出现
      「总到达率 100%、分段却报 80%」这种自相矛盾的输出。
    """
    sk = [r for r in send.rows if r["act"] in KEY_ACTS]
    rk = [r for r in recv.rows if r["act"] in KEY_ACTS]
    if not sk:
        return []
    ms, _ = _match([key_sig(x) for x in sk], [key_sig(x) for x in rk])
    arrived = [i in ms for i in range(len(sk))]
    out = []
    size = max(1, len(sk) // n_buckets)
    for b in range(0, len(sk), size):
        chunk = arrived[b:b + size]
        out.append({
            "from": b,
            "to": b + len(chunk),
            "keys": len(chunk),
            "arrived": sum(chunk),
            "rate": sum(chunk) / len(chunk) if chunk else 0.0,
        })
    return out


def action_hist(t: Table) -> dict[int, int]:
    return dict(Counter(r["act"] for r in t.rows))


def coord_drift(send: Table, recv: Table) -> dict:
    """对齐后逐帧比坐标，判断偏差是「恒定平移」还是「缩放/错位」。

    ★ 判据用**众数占比**，不是 max−min（第一版就是后者，被整首真数据打红）。
      整首 1683 个指针里 1678 个偏差恒为 −74（= 窗口 inset），只有 5 个离群：
      4 个是 ±2 px 的合并抖动，1 个是 MOVE 合并后配错帧。用 max−min 会把这 5 条
      判成「坐标变换有问题」—— 而真相是变换完全正确。
      ⇒ 只要众数占绝对多数（≥99.5%），就是恒定平移；否则才是真问题。
      缩放的形态是众数**不唯一**（每个位置一个偏差值），所以仍能被抓到。

    为什么允许非零：发送侧坐标是 int（Scheduler.toDevice 用 rint），收货侧是
    MotionEvent 的 float（窗口坐标），两者相差一个窗口原点 —— 本机横屏窗口
    是 1536x720 而屏幕 1610x720，差的 74 px 就是这个 offset。
    """
    sk = [r for r in send.rows if r["act"] in KEY_ACTS]
    rk = [r for r in recv.rows if r["act"] in KEY_ACTS]
    pairs = _matched_pairs(sk, rk)
    if not pairs:
        return {"pairs": 0}
    dxs, dys = [], []
    for s, r in pairs:
        if len(s["ptrs"]) != len(r["ptrs"]):
            continue
        for (sid, sx, sy), (rid, rx, ry) in zip(s["ptrs"], r["ptrs"]):
            if sid != rid:
                continue
            dxs.append(rx - sx)
            dys.append(ry - sy)
    if not dxs:
        return {"pairs": len(pairs), "comparable": 0}

    def stat(v):
        c = Counter(v)
        mode, cnt = c.most_common(1)[0]
        return {"min": min(v), "max": max(v),
                "abs_max": max(abs(x) for x in v),
                "mode": mode, "mode_n": cnt, "mode_frac": cnt / len(v),
                "distinct": len(c)}

    return {"pairs": len(pairs), "comparable": len(dxs),
            "dx": stat(dxs), "dy": stat(dys)}


#: 众数占到这个比例就算「恒定平移」。99.5% 是因为整首真数据里离群率是 5/1683=0.30%
#: （MOVE 合并导致的配错），再严就会把正常抖动报成 bug。
DRIFT_MODE_MIN = 0.995


# ─────────────────────────── 报告 ───────────────────────────

def report(send: Table, recv: Table, notes: int | None) -> dict:
    print(f"发送侧：{send.path}")
    print(f"  {send.frames} 帧" + (f"（明细被截断 {send.truncated} 行 ⇒ 对账不完整）"
                                 if send.truncated else ""))
    print(f"收货侧：{recv.path}")
    print(f"  {recv.frames} 条" + (f"（明细被截断 {recv.truncated} 行 ⇒ 对账不完整）"
                                  if recv.truncated else ""))

    hs, hr = action_hist(send), action_hist(recv)
    print("\n-- 动作分布（发 → 收）--")
    for act in (ACT_DOWN, ACT_PDOWN, ACT_MOVE, ACT_PUP, ACT_UP, ACT_CANCEL):
        a, b = hs.get(act, 0), hr.get(act, 0)
        if a == 0 and b == 0:
            continue
        delta = "" if a == 0 else f"  ({b - a:+d})"
        print(f"  {ACT_NAME[act]:12s} {a:6d} → {b:6d}{delta}")
    leftovers = {k: v for k, v in hr.items() if k not in ACT_NAME}
    if leftovers:
        print(f"  未知动作 {leftovers}")

    al = align(send, recv)
    print("\n-- 关键帧对账（DOWN / POINTER_DOWN / POINTER_UP / UP 严格比 act+idx+n）--")
    print(f"  发送关键帧 {al['send_keys']}，收货 {al['recv_keys']}，"
          f"匹配 {al['matched']}")
    if al["send_keys"]:
        rate = al["matched"] / al["send_keys"]
        print(f"  到达率 {rate * 100:.3f}%")
    else:
        rate = 1.0
        print("  ⚠ 发送侧没有关键帧 —— 这份 trace 不是一次完整注入")
    if al["missing_n"]:
        by_act = Counter(ACT_NAME.get(m["sig"][0], str(m["sig"][0])) for m in al["missing"])
        print(f"  ✗ 缺失 {al['missing_n']} 条，按动作：" +
              "，".join(f"{k} {v}" for k, v in by_act.most_common()))
        for m in al["missing"][:8]:
            print(f"    缺失 seq={m['seq']} t={m['t_ms']}ms "
                  f"{ACT_NAME.get(m['sig'][0], m['sig'][0])} idx={m['sig'][1]} n={m['sig'][2]}")
        if al["missing_n"] > 8:
            print(f"    …（另有 {al['missing_n'] - 8} 条）")
    if al["extra"]:
        print(f"  ⚠ 收货侧多出 {al['extra']} 条关键帧（输入流被污染？上一次残留？）")
    if al["sig_only_mismatch"]:
        print(f"  ★ 其中 {al['sig_only_mismatch']} 条是「动作在、但 idx/指针数对不上」"
              f"（宽松口径只缺 {al['loose_missing']} 条）⇒ 不是丢帧，是编码不符")

    n_move_s, n_move_r = hs.get(ACT_MOVE, 0), hr.get(ACT_MOVE, 0)
    if n_move_s:
        print(f"\n-- MOVE 合并 --  发 {n_move_s} → 收 {n_move_r}，"
              f"合并掉 {n_move_s - n_move_r} 条（{100 * (1 - n_move_r / n_move_s):.1f}%）")
        print("   ⚠ 连续 MOVE 被合并是**正常**行为，不是丢帧（10-21 实测 5 发 3 收）")

    if hr.get(ACT_CANCEL):
        print(f"\n  ✗ 收到 {hr[ACT_CANCEL]} 条 CANCEL —— 触摸流被系统/窗口切换打断，"
              f"这一轮对账不成立")

    cd = coord_drift(send, recv)
    if cd.get("comparable"):
        dx, dy = cd["dx"], cd["dy"]
        print(f"\n-- 坐标偏差（收货 − 发送，{cd['comparable']} 个指针）--")
        print(f"  dx 众数 {dx['mode']}（{dx['mode_n']}/{cd['comparable']} = "
              f"{dx['mode_frac'] * 100:.2f}%），范围 {dx['min']}..{dx['max']}")
        print(f"  dy 众数 {dy['mode']}（{dy['mode_frac'] * 100:.2f}%），"
              f"范围 {dy['min']}..{dy['max']}")
        const_ok = dx["mode_frac"] >= DRIFT_MODE_MIN and dy["mode_frac"] >= DRIFT_MODE_MIN
        if const_ok and (dx["mode"] != 0 or dy["mode"] != 0):
            outl = cd["comparable"] - dx["mode_n"]
            print(f"  ⇒ 恒定平移（dx={dx['mode']}, dy={dy['mode']}）—— 窗口 inset，不影响判定")
            if outl:
                print(f"     离群 {outl} 个（MOVE 合并后配错帧 / ±2 px 抖动），占比 "
                      f"{100 * outl / cd['comparable']:.2f}%")
        elif const_ok:
            print("  ⇒ 无平移，坐标逐点一致")
        else:
            print(f"  ✗ 偏差**没有单一众数**（{dx['distinct']} 种取值，众数仅占 "
                  f"{dx['mode_frac'] * 100:.1f}%）⇒ 缩放/错位，坐标变换有问题")

    bk = buckets(send, recv)
    if len(bk) >= 3:
        bad = [b for b in bk if b["rate"] < KEY_ARRIVAL_MIN]
        if bad:
            print(f"\n-- 分段到达率（{len(bk)} 段）-- 有 {len(bad)} 段不达标：")
            for b in bad[:6]:
                print(f"  第 {b['from']}~{b['to']} 关键帧：{b['arrived']}/{b['keys']}"
                      f" = {b['rate'] * 100:.2f}%")
        else:
            print(f"\n-- 分段到达率（{len(bk)} 段）-- 全部 {KEY_ARRIVAL_MIN * 100:.1f}% 以上 ✓")
            print("   ⇒ 没有「越打越糟」：不是累积滞后 / 抽稀失衡")

    # ── 结论 ──
    print("\n" + "=" * 60)
    verdict = _verdict(al, hr, rate)
    print(verdict["line"])
    if notes:
        miss_cover = al["missing_n"] / notes * 100
        print(f"  量级：缺 {al['missing_n']} 条关键帧 ＝ 谱面 {notes} 音符的 "
              f"{miss_cover:.2f}% ⇒ " +
              ("**能**解释大部分 Miss" if miss_cover >= 50 else
               "**不能**解释大部分 Miss（差一个量级以上）"))

    return {
        "send_frames": send.frames,
        "recv_frames": recv.frames,
        "send_truncated": send.truncated,
        "recv_truncated": recv.truncated,
        "send_hist": {ACT_NAME.get(k, str(k)): v for k, v in hs.items()},
        "recv_hist": {ACT_NAME.get(k, str(k)): v for k, v in hr.items()},
        "align": {k: v for k, v in al.items() if k != "missing"},
        "missing": [{**m, "sig": [ACT_NAME.get(m["sig"][0], m["sig"][0]),
                                  m["sig"][1], m["sig"][2]]} for m in al["missing"]],
        "coord": cd,
        "buckets": bk,
        "verdict": verdict["kind"],
        "notes": notes,
    }


def _verdict(al: dict, hr: dict, rate: float) -> dict:
    if al["send_keys"] == 0:
        return {"kind": "invalid", "line": "⇒ 这份 trace 没有关键帧，不能当对账依据。"}
    if hr.get(ACT_CANCEL):
        return {"kind": "cancel",
                "line": "⇒ 有 CANCEL：触摸流被打断，先排除窗口切换/系统手势，再重跑。"}
    # ★ 顺序要紧：如果每一条「严格缺失」都能在收货侧找到 act+n 相同的帧，
    #   那根本没有帧丢失，只是 actionIndex/指针数不符。这时按"丢帧"去查派发
    #   会白干（三处对齐里最贵的一类误判）。
    if al["missing_n"] > 0 and al["sig_only_mismatch"] == al["missing_n"]:
        return {"kind": "encoding",
                "line": (f"⇒ 帧没丢，但 {al['sig_only_mismatch']} 条 act+idx+n 不符 ⇒ "
                         f"actionIndex/指针数编码与发送侧不一致（查 Gesture/构建，不是查派发）。")}
    if rate < KEY_ARRIVAL_MIN:
        return {"kind": "dispatch_lost",
                "line": (f"⇒ **派发层有罪**：关键帧只到了 {rate * 100:.3f}%，"
                         f"其中 {al['loose_missing']} 条连 act+n 都找不到 ⇒ 真丢帧。"
                         f"事件发了但没送到 App ⇒ 别再查游戏判定、别再调 G/L。")}
    if al["extra"] > max(1, int(al["send_keys"] * KEY_EXTRA_MAX)):
        return {"kind": "polluted",
                "line": (f"⇒ 到达率够，但收货侧多出 {al['extra']} 条关键帧 ⇒ "
                         f"输入流被污染（上一次残留？），这轮不能当证据，清干净重跑。")}
    if al["sig_only_mismatch"] > 0:
        return {"kind": "encoding",
                "line": (f"⇒ 帧数对得上，但有 {al['sig_only_mismatch']} 条 act+idx+n 不符 ⇒ "
                         f"不是丢帧，是 actionIndex/指针数编码与发送侧不一致。")}
    return {"kind": "arrived",
            "line": (f"⇒ **派发层清白**：{al['send_keys']} 条关键帧到达率 "
                     f"{rate * 100:.3f}%，无 CANCEL、无污染、无编码偏差。"
                     f"事件确实送到了 App ⇒ Miss 只能出自「游戏判定」这一侧。")}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="端内注入发送侧/收货侧逐帧对账")
    ap.add_argument("send", help="inject_trace.csv（发送侧）")
    ap.add_argument("recv", help="touch_recv.csv（收货侧）")
    ap.add_argument("--notes", type=int, default=None, help="谱面音符数（做 Miss 量级对照）")
    ap.add_argument("--json", action="store_true", help="末尾附 JSON 摘要")
    a = ap.parse_args(argv)
    try:
        send = _read(a.send, SEND_COLS, "发送侧")
        recv = _read(a.recv, RECV_COLS, "收货侧")
    except TraceError as e:
        print(f"✗ {e}", file=sys.stderr)
        return 2
    summary = report(send, recv, a.notes)
    if a.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    # 派发层丢帧 / 流被打断 ⇒ 非零退出码，方便脚本判
    return 1 if summary["verdict"] in ("dispatch_lost", "cancel") else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
