# -*- coding: utf-8 -*-
"""输入覆盖率审计：找出「判定窗内根本没有手指按着」的音符 —— 这些必定是 Miss。

动机
----
burn IN 两轮都是 P~702 / **M 5~6** / **Bad 0 / Early 0 / Late 0**。
Bad=0 是关键：Bad 表示「有输入但位置/时机差」，Miss 表示「**没有任何输入**」
⇒ 这 5~6 个音符不是时序问题，是**事件流压根没覆盖到**。
同一套判据在 dp321 EZ 那轮应该给出 131（实测 M=131），可作交叉验证。

判据（只用事件时刻，不用坐标 —— 避免坐标系换算引入假阳性）
------------------------------------------------------------
把每个 pointer 的 DOWN→UP 拼成区间 `[t_down, t_up)`（MOVE 不改区间）。
一个音符「可被打到」当且仅当存在某个区间 I：

  * 非 Hold：`I.down ∈ [t-80, t+80]`（输入落在判定窗内）
             或 `I.down <= t-80 且 I.up > t`（手指本来就按着，Drag 段）
  * Hold  ：在上面基础上还要求 `I.up >= t + hold_ms - 80`（撑到尾部）

⛔ 阈值 80 ms = P 窗（`P ±80`，见 FACTS）。窗口宽度只影响「压边」判读，
   不影响「完全没覆盖」这一类（本工具的靶子）。

用法
----
    python tools/audit_coverage.py --tag burn
    python tools/audit_coverage.py --tag destruction321_ez --dump tmp/_cov.json
    python tools/audit_coverage.py --selftest
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WIN_MS = 80.0

DOWN_ACTS = ("DOWN", "POINTER_DOWN")
UP_ACTS = ("UP", "POINTER_UP")


def build_intervals(events: list[dict]) -> list[tuple[float, float, int, float, float]]:
    """[(down_ms, up_ms, pid, x, y)]；缺 UP 的按 +inf 收尾（视为一直在按）。"""
    open_: dict[int, tuple[float, float, float]] = {}
    out = []
    for e in sorted(events, key=lambda e: (e["t_ms"], 0)):
        pid = e["pointer_id"]
        act = e["action"]
        if act in DOWN_ACTS:
            if pid in open_:                      # 重复 DOWN ⇒ 上一段到此为止
                d0, x0, y0 = open_.pop(pid)
                out.append((d0, float(e["t_ms"]), pid, x0, y0))
            open_[pid] = (float(e["t_ms"]), float(e.get("x", 0.0)), float(e.get("y", 0.0)))
        elif act in UP_ACTS:
            if pid in open_:
                d0, x0, y0 = open_.pop(pid)
                out.append((d0, float(e["t_ms"]), pid, x0, y0))
        elif act == "MOVE":
            if pid in open_:                      # 只更新位置快照
                d0, _, _ = open_[pid]
                open_[pid] = (d0, float(e.get("x", 0.0)), float(e.get("y", 0.0)))
    for pid, (d0, x0, y0) in open_.items():
        out.append((d0, float("inf"), pid, x0, y0))
    out.sort()
    return out


def covered(note: dict, ivs: list[tuple[float, float, int, float, float]],
            win: float = WIN_MS) -> tuple[bool, str]:
    t = float(note["t_ms"])
    hold = float(note.get("hold_ms") or 0.0)
    lo, hi = t - win, t + win
    for d0, u1, _pid, _x, _y in ivs:
        spans = (d0 <= lo and u1 > t)                  # 本来就按着
        lands = (lo <= d0 <= hi)                       # 窗内按下
        if not (spans or lands):
            continue
        if hold > 0 and u1 < t + hold - win:
            continue                                   # 撑不到尾
        return True, "spans" if spans and not lands else "lands"
    return False, "none"


def audit(tag: str) -> dict:
    tp = ROOT / f"out/touches_{tag}.json"
    np_ = ROOT / f"out/notes_{tag}.json"
    d = json.loads(tp.read_text(encoding="utf-8"))
    notes = json.loads(np_.read_text(encoding="utf-8"))["notes"]
    ivs = build_intervals(d["events"])
    notes = sorted(notes, key=lambda n: n["t_ms"])
    bad = []
    for i, n in enumerate(notes):
        ok, why = covered(n, ivs)
        if not ok:
            bad.append({"idx": i, "type": n.get("type_name"), "t_ms": n["t_ms"],
                        "hold_ms": n.get("hold_ms"), "px": n.get("px"), "py": n.get("py")})
    return {"tag": tag, "n_notes": len(notes), "n_intervals": len(ivs),
            "n_uncovered": len(bad), "uncovered": bad}


# ---------------------------------------------------------------- 位置口径
# 交付坐标（touches_*.json 的 events 用的那套，1080x2340 竖屏）：
#   x = 0.5625 * px          y = 866.25 + 0.5625 * py
# 由 tools/inject.py:ScreenMap((1920,1080),(1080,2340),(3136,1440),fit="height") 反推，
# 已用首事件核对：(1440, 415.719) → (809.99, 1100.09) 与本轮 events[0] 完全一致。
K = 1080.0 / 1920.0
YOFF = (2340.0 - 1080.0 * K) / 2.0


def to_delivery(px: float, py: float) -> tuple[float, float]:
    return px * K, YOFF + py * K


def audit_pos(tag: str) -> dict:
    """每个音符在「自己的判定时刻」到最近按下指针的距离（交付像素）。"""
    d = json.loads((ROOT / f"out/touches_{tag}.json").read_text(encoding="utf-8"))
    notes = sorted(json.loads((ROOT / f"out/notes_{tag}.json").read_text(encoding="utf-8"))["notes"],
                   key=lambda n: n["t_ms"])
    evs = sorted(d["events"], key=lambda e: e["t_ms"])
    down: dict[int, tuple[float, float]] = {}
    j = 0
    rows = []
    for i, n in enumerate(notes):
        t = float(n["t_ms"])
        while j < len(evs) and float(evs[j]["t_ms"]) <= t:
            e = evs[j]
            pid, act = e["pointer_id"], e["action"]
            if act in DOWN_ACTS or act == "MOVE":
                if act in DOWN_ACTS:
                    down[pid] = (float(e.get("x", 0.0)), float(e.get("y", 0.0)))
                elif pid in down:          # MOVE 只在已按下时算数
                    down[pid] = (float(e.get("x", 0.0)), float(e.get("y", 0.0)))
            elif act in UP_ACTS:
                down.pop(pid, None)
            j += 1
        nx, ny = to_delivery(float(n["px"]), float(n["py"]))
        best, bpid = float("inf"), None
        for pid, (x, y) in down.items():
            dd = ((x - nx) ** 2 + (y - ny) ** 2) ** 0.5
            if dd < best:
                best, bpid = dd, pid
        rows.append({"idx": i, "type": n.get("type_name"), "t_ms": t,
                     "hold_ms": n.get("hold_ms") or 0.0,
                     "d": (None if best == float("inf") else round(best, 1)),
                     "pid": bpid, "n_down": len(down)})
    ds = sorted(r["d"] for r in rows if r["d"] is not None)
    q = lambda p: ds[min(len(ds) - 1, int(len(ds) * p))] if ds else None
    return {"tag": tag, "n": len(rows), "n_never_down": sum(1 for r in rows if r["d"] is None),
            "p50": q(0.50), "p90": q(0.90), "p99": q(0.99), "max": ds[-1] if ds else None,
            "rows": rows}


# ---------------------------------------------------------------- selftest
def _ev(t, act, pid):
    return {"t_ms": float(t), "action": act, "pointer_id": pid, "x": 0.0, "y": 0.0}


def selftest() -> int:
    fails = []

    def chk(name, got, want):
        if got != want:
            fails.append(f"{name}: got={got!r} want={want!r}")

    # 区间拼接：DOWN 100 → UP 400 ⇒ 一个区间
    ivs = build_intervals([_ev(100, "DOWN", 0), _ev(400, "UP", 0)])
    chk("S1 区间数", len(ivs), 1)
    chk("S1 区间值", (ivs[0][0], ivs[0][1]), (100.0, 400.0))

    # 重复 DOWN 不丢前一段（说明缺失 UP 时必须收口）
    ivs = build_intervals([_ev(100, "DOWN", 0), _ev(300, "DOWN", 0), _ev(500, "UP", 0)])
    chk("S2 重复DOWN", [(a, b) for a, b, *_ in ivs], [(100.0, 300.0), (300.0, 500.0)])

    # 1 ms 短按（tap 的真实形态）覆盖住同刻 Tap
    ivs = build_intervals([_ev(3243, "DOWN", 0), _ev(3244, "UP", 0)])
    chk("S3 1ms短按命中", covered({"t_ms": 3243.2432, "hold_ms": 0}, ivs)[0], True)

    # 音符落在空档里 ⇒ 必须判不覆盖（这是本工具存在的理由）
    ivs = build_intervals([_ev(0, "DOWN", 0), _ev(1000, "UP", 0),
                           _ev(2000, "DOWN", 0), _ev(3000, "UP", 0)])
    chk("S4 空档不覆盖", covered({"t_ms": 1500, "hold_ms": 0}, ivs)[0], False)
    chk("S4b 空档右缘不覆盖", covered({"t_ms": 1900, "hold_ms": 0}, ivs)[0], False)
    chk("S5 区间内覆盖", covered({"t_ms": 2500, "hold_ms": 0}, ivs)[0], True)

    # Hold：手指中途抬起 ⇒ 未覆盖；撑到底 ⇒ 覆盖
    ivs_mid = build_intervals([_ev(1000, "DOWN", 0), _ev(1500, "UP", 0)])
    chk("S6 Hold中途抬", covered({"t_ms": 1000, "hold_ms": 2000}, ivs_mid)[0], False)
    chk("S7 Hold撑到尾", covered({"t_ms": 1000, "hold_ms": 400}, ivs_mid)[0], True)

    # 反向：若把窗口收成 0，压边 1 ms 的短按仍应命中（防止靠宽窗蒙对）
    ivs_edge = build_intervals([_ev(1080, "DOWN", 0), _ev(1081, "UP", 0)])
    chk("S8 窗内变形", covered({"t_ms": 1000, "hold_ms": 0}, ivs_edge, win=80)[0], True)

    # 多指针：A 已抬、B 仍按着 ⇒ 覆盖（不能只看第一个区间）
    ivs_multi = build_intervals([_ev(0, "DOWN", 0), _ev(900, "UP", 0),
                                 _ev(0, "DOWN", 1), _ev(2000, "UP", 1)])
    chk("S9 多指针兜底", covered({"t_ms": 1500, "hold_ms": 0}, ivs_multi)[0], True)

    # vacuous 防护：全空事件流必须把所有音符判成未覆盖
    chk("S10 空流全不覆盖", covered({"t_ms": 500, "hold_ms": 0}, [])[0], False)

    # 坐标映射：必须与本轮 events[0] 的真实坐标一致（否则位置口径全错）
    x, y = to_delivery(1440.0, 415.719)
    chk("S11 映射x", round(x, 2), 810.00)
    chk("S11 映射y", round(y, 2), 1100.09)

    if fails:
        print("✗ selftest 失败：")
        for f in fails:
            print("   ", f)
        return 1
    print(f"✓ selftest 10 项全过（WIN_MS={WIN_MS}）")
    return 0


# ---------------------------------------------------------------- 近邻口径
def audit_near(tag: str, win: float = WIN_MS) -> dict:
    """每个音符在 ±win 内**最近的一条事件**到它的距离（交付像素）。

    ★ 这是「静态可判」的唯一口径：手指按下只有 1 ms（Tap）或由 MOVE 携带，
      所以不能用「t 时刻手指是否按着」去判（那样会把 583 个 Tap 全判成无人）。
      真正能静态判的是：**判定窗内有没有任何一条事件落在这个音符的位置附近**。
      距离大 ⇒ 端内不管怎么发都打不到 ⇒ 必定 Miss；距离小 ⇒ 静态没问题，
      丢的那几个只能是运行时（端内抽稀 / 游戏侧）造成的。
    """
    d = json.loads((ROOT / f"out/touches_{tag}.json").read_text(encoding="utf-8"))
    notes = sorted(json.loads((ROOT / f"out/notes_{tag}.json").read_text(encoding="utf-8"))["notes"],
                   key=lambda n: n["t_ms"])
    evs = [e for e in d["events"] if e["action"] != "UP" and e["action"] != "POINTER_UP"]
    evs.sort(key=lambda e: e["t_ms"])
    ts = [float(e["t_ms"]) for e in evs]
    # ⛔ 事件坐标**已经是**交付坐标（1080x2340），不能再套 to_delivery —— 只有音符
    #   （px/py 在 1920x1080 画布）需要换算。这里曾双套一次，导致 707/707 全判 d>20。
    pos = [(float(e.get("x", 0.0)), float(e.get("y", 0.0))) for e in evs]
    import bisect
    rows = []
    for i, n in enumerate(notes):
        t = float(n["t_ms"])
        lo = bisect.bisect_left(ts, t - win)
        hi = bisect.bisect_right(ts, t + win)
        nx, ny = to_delivery(float(n["px"]), float(n["py"]))
        best, nev = float("inf"), hi - lo
        for k in range(lo, hi):
            x, y = pos[k]
            dd = ((x - nx) ** 2 + (y - ny) ** 2) ** 0.5
            if dd < best:
                best = dd
        rows.append({"idx": i, "type": n.get("type_name"), "t_ms": t,
                     "hold_ms": n.get("hold_ms") or 0.0,
                     "d": (None if best == float("inf") else round(best, 1)),
                     "n_ev": nev})
    return {"tag": tag, "n": len(rows), "rows": rows}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag")
    ap.add_argument("--dump")
    ap.add_argument("--pos", action="store_true",
                    help="按位置口径审计（⚠ 对 Tap 无意义：1ms 按压在 t 时刻手上没有手指）")
    ap.add_argument("--near", action="store_true",
                    help="按近邻口径审计（推荐：判定窗内最近一条事件到音符的距离）")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest or not a.tag:
        return selftest()
    if a.near:
        r = audit_near(a.tag)
        from collections import Counter
        rows = r["rows"]
        print(f"{a.tag}: 音符 {r['n']}，判定窗内无任何事件 "
              f"{sum(1 for x in rows if x['d'] is None)}")
        for thr in (20, 50, 100, 200, 400):
            far = [x for x in rows if x["d"] is None or x["d"] > thr]
            print("   d > %4d : %3d 个  %s" % (thr, len(far), dict(Counter(x["type"] for x in far))))
        if a.dump:
            Path(a.dump).write_text(json.dumps(r, ensure_ascii=False, indent=1), encoding="utf-8")
            print("   →", a.dump)
        return 0
    if a.pos:
        print("⚠ --pos 用「t 时刻按着的手指」算距离；Tap 的按压只有 1 ms，t 时刻手上可能没手指，"
              "\n   所以大量 Tap 会显示 d=None。**判覆盖请用 --near**。")
        r = audit_pos(a.tag)
        print(f"{a.tag}: 音符 {r['n']}，无任何指针 {r['n_never_down']}；"
              f"最近指针距离 p50={r['p50']} p90={r['p90']} p99={r['p99']} max={r['max']}（交付像素）")
        for thr in (50, 100, 150, 200, 300, 500, 800):
            far = [x for x in r["rows"] if x["d"] is None or x["d"] > thr]
            from collections import Counter
            print("   d > %4d : %3d 个  %s" %
                  (thr, len(far), dict(Counter(x["type"] for x in far))))
        far = sorted([x for x in r["rows"] if x["d"] is None or x["d"] > 200],
                     key=lambda x: -1e9 if x["d"] is None else -x["d"])
        print("   —— 最远的 15 个 ——")
        for x in far[:15]:
            print("   #%-4d %-5s t=%9.1f hold=%6.1f d=%s pid=%s n_down=%d" %
                  (x["idx"], x["type"], x["t_ms"], x["hold_ms"], x["d"], x["pid"], x["n_down"]))
        if a.dump:
            Path(a.dump).write_text(json.dumps(r, ensure_ascii=False, indent=1), encoding="utf-8")
            print("   →", a.dump)
        return 0
    r = audit(a.tag)
    print(f"{r['tag']}: 音符 {r['n_notes']}，指针区间 {r['n_intervals']}，"
          f"**未覆盖 {r['n_uncovered']}**")
    from collections import Counter
    print("  类型分布：", dict(Counter(x["type"] for x in r["uncovered"])))
    for x in r["uncovered"][:20]:
        print("   #%-4d %-5s t=%9.1f ms hold=%6.1f  px=(%.0f,%.0f)" %
              (x["idx"], x["type"], x["t_ms"], x["hold_ms"] or 0, x["px"], x["py"]))
    if a.dump:
        Path(a.dump).write_text(json.dumps(r, ensure_ascii=False, indent=1), encoding="utf-8")
        print("   →", a.dump)
    return 0


if __name__ == "__main__":
    sys.exit(main())


