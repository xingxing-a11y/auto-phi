"""课题模式轮次对照表 —— 把「结算页的 Good 数」与「磁盘上的零点实测」配到一起。

为什么需要（2026-10-29 的教训）
================================
δ（本轮零点实际偏了多少）**只能**从结算页的 Good 数反推，而 `trim / fired_rel / zero_rel`
只在 `course_*.csv` 里。两者分处屏幕与磁盘 ⇒ 之前 12 轮的统计全靠人肉手抄，
一旦抄错就再也回不去（本项目最贵的 bug 类型 = 文档落后磁盘）。

    δ 的反推模型（双侧判定窗，**不能**用单侧近似）：
        expected_good(δ) = n · [ Φ((−80−δ)/σ) + 1 − Φ((80−δ)/σ) ] ,  σ = 24 ms
        δ = 解 expected_good(δ) = good        （单调 ⇒ 二分）
    good = 0 时无法反推，只给**上界**：δ < 「刚好 1 个 Good」对应的 δ。

⚠ 判定窗口有两个互相冲突的口径：`pgauto`（`replay_score`）用 **±80 ms / σ≈24 ms**，
  `PlayService.COURSE_TRIM_BY_KEY` 的注释用 **±40 ms / σ=5 ms**。本表按前者算
  （与真机 σ≈24 的实测一致），另一口径会把 δ 放大约 6 倍 ⇒ **不要混用**。

用法
====
    # 打完后补一轮（goods 从结算页读，顺序 = course csv 的行序）
    python tools/course_rounds.py add --at 195456 --ver 94b5f4b-user \
        --goods 1 13 1 --scores 999778 996454 999745 --total 2995977

    python tools/course_rounds.py show        # 看汇总
    python tools/course_rounds.py selftest    # 内置不变量检查（改公式后必跑）

⛔ 不写设备、不联网、不改 `out/course_*.csv`：只读它们 + 追加一个 `out/course_rounds.csv`。
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from statistics import NormalDist

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "out"
MANIFEST = ROOT / "android" / "play" / "app" / "src" / "main" / "assets" / "manifest.json"
TABLE = OUT / "course_rounds.csv"

#: Perfect 判定窗（ms）与逐音符抖动（ms）。见模块文档的口径说明。
PERFECT_MS = 80.0
SIGMA_MS = 24.0

FIELDS = [
    "round_at", "ver", "song", "trim_ms", "fired_rel_ms", "zero_rel_ms",
    "n_note", "good", "floor_good", "delta_ms", "score", "note",
]


def delta_of(good: int, n_note: int):
    """反推本轮零点偏移。δ 只在 `good > floor_good(n)` 时才可测，否则给 None。

    ★★ **`floor_good(n)` 是被长期忽略的关键量**：即使 δ=0（零点完全准），
       σ=24 ms 的高斯尾也会让 n 个音符里出现 `n·2·Φ(−80/24)` 个 Good。
       n=1578 ⇒ **1.35 个**；n=1270 ⇒ 1.09；n=1373 ⇒ 1.18。
       ⇒ 「1 个 Good」根本不是偏移，「0 个 Good」只是运气好。

    ⇒ 因此：
      · good ≤ floor_good ⇒ δ **不可测**（与 0 无法区分），返回 None；
      · good >  floor_good ⇒ 在 δ∈(0, 500] 上二分（该区间 expected_good 单调增）。

    ⚠ 不能用 `δ = 80 − σ·Φ⁻¹(1−p)`：那既漏了下侧（Early）也忽略了 V 形
      （δ 很负时所有音符都 Early，expected_good → n）。第一版就是这么错的，
      `selftest` 的往返断言当场抓到。
    """
    if n_note <= 0 or good < 0:
        return None
    if good <= floor_good(n_note) or good >= n_note:
        return None
    lo, hi = 0.0, 500.0
    for _ in range(80):
        mid = (lo + hi) / 2.0
        if expected_good(mid, n_note) < good:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def floor_good(n_note: int) -> float:
    """δ=0、σ=24 时的**理论下限**：这一首最少也会有几个 Good。"""
    return expected_good(0.0, n_note)


def expected_good(delta: float, n_note: int) -> float:
    """模型正向：给定 δ 与 σ，期望有多少音符落在判定窗外（双侧）。"""
    nd = NormalDist(mu=delta, sigma=SIGMA_MS)
    return n_note * (nd.cdf(-PERFECT_MS) + (1.0 - nd.cdf(PERFECT_MS)))


def load_note_counts() -> dict:
    if not MANIFEST.exists():
        sys.exit(f"✗ 缺 {MANIFEST}（先跑 tools/pack_assets.py）")
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    return {s["key"]: int(s["note_count"]) for s in data["songs"]}


def read_course(path: Path) -> list:
    if not path.exists():
        sys.exit(f"✗ 找不到 {path}（--at 应与 out/course_<HHmmss>.csv 的时间一致）")
    rows = []
    with path.open(encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            if (r.get("song") or "").startswith("#"):
                continue
            rows.append(r)
    if not rows:
        sys.exit(f"✗ {path.name} 里没有任何曲目行（只有中止行？）")
    return rows


def read_table() -> list:
    if not TABLE.exists():
        return []
    with TABLE.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def write_table(rows: list) -> None:
    TABLE.parent.mkdir(parents=True, exist_ok=True)
    with TABLE.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, lineterminator="\n")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in FIELDS})


def cmd_add(args) -> int:
    course = read_course(OUT / f"course_{args.at}.csv")
    notes = load_note_counts()
    if len(args.goods) != len(course):
        sys.exit(f"✗ --goods 给了 {len(args.goods)} 个，但 {course[0].get('song')} 那轮有 "
                 f"{len(course)} 首（goods 顺序必须与 course csv 的行序一致）")
    scores = args.scores or []
    if scores and len(scores) != len(course):
        sys.exit(f"✗ --scores 给了 {len(scores)} 个，但本轮有 {len(course)} 首")

    # 同一轮重复 add ⇒ 覆盖而不是追加（幂等）。判据用 round_at（文件名隐含的时刻）。
    rows = [r for r in read_table() if r.get("round_at") != args.at]
    new_rows = []
    for i, cr in enumerate(course):
        song = cr["song"]
        n = notes.get(song)
        if n is None:
            sys.exit(f"✗ manifest 里没有 {song}（曲库与设备不同步？）")
        good = args.goods[i]
        if good > n:
            sys.exit(f"✗ {song} 只有 {n} 个音符，Good={good} 不可能")
        delta = delta_of(good, n)
        new_rows.append({
            "round_at": args.at,
            "ver": args.ver,
            "song": song,
            "trim_ms": cr["trim_ms"],
            "fired_rel_ms": cr["fired_rel_ms"],
            "zero_rel_ms": cr["zero_rel_ms"],
            "n_note": n,
            "good": good,
            "floor_good": f"{floor_good(n):.2f}",
            "delta_ms": "" if delta is None else f"{delta:.1f}",
            "score": scores[i] if scores else "",
            "note": args.note,
        })
    rows.extend(new_rows)
    # 只按 round_at 排序：Python 的 sort 是稳定的 ⇒ 同轮内保持 course csv 的行序
    # （erabyeconnec → distort → xd8e302e4），而不是按曲名字母序（那样读起来像打乱过）。
    rows.sort(key=lambda r: r["round_at"])
    write_table(rows)

    print(f"✓ 已写入 {TABLE.relative_to(ROOT)}（本轮 {len(new_rows)} 行，表内共 {len(rows)} 行）")
    for r in new_rows:
        d = r["delta_ms"] or "≈0"
        flag = "" if r["delta_ms"] else "  ← 已到理论下限，δ 不可测"
        print(f"  {r['song']:24} trim={float(r['trim_ms']):5.1f}  good={r['good']:4}"
              f"  floor={r['floor_good']:>5}  δ={d:>6} ms"
              + (f"  score={r['score']}" if r["score"] else "") + flag)

    if args.total is not None and scores:
        s = sum(scores)
        if abs(s - args.total) > 3:
            print(f"⚠ 总分 {args.total} 与三首之和 {s} 差 {args.total - s}——"
                  f"是抄错了，还是标题界面与结算不同源？", file=sys.stderr)
        else:
            print(f"  ✓ 总分自洽：{args.total}")
    return 0


def cmd_show(args) -> int:
    rows = read_table()
    if not rows:
        print(f"（{TABLE.name} 还是空的：先 add 一轮）")
        return 0
    cur = None
    for r in rows:
        if r["round_at"] != cur:
            cur = r["round_at"]
            print(f"\n== {cur}  ver={r.get('ver') or '?'}")
            print(f"  {'song':24}{'trim':>6}{'good':>6}{'floor':>7}{'δ(ms)':>8}{'score':>9}")
        d = r["delta_ms"] or "≈0"
        print(f"  {r['song']:24}{float(r['trim_ms']):6.0f}{r['good']:>6}"
              f"{r.get('floor_good') or '':>7}{d:>8}{r.get('score') or '':>9}")
    return 0


def cmd_selftest(args) -> int:
    """不变量：正向/反推互逆、δ 单调、理论下限的行为正确。"""
    bad = 0

    def ok(cond, what):
        nonlocal bad
        print(("  ✓ " if cond else "  ✗ ") + what)
        if not cond:
            bad += 1

    # ① 理论下限：δ=0 时本来就有的 Good 数（σ=24 的高斯尾）
    ok(abs(floor_good(1578) - 1.35) < 0.02, f"floor(1578) ≈ 1.35（实际 {floor_good(1578):.3f}）")
    ok(abs(floor_good(1270) - 1.09) < 0.02, f"floor(1270) ≈ 1.09（实际 {floor_good(1270):.3f}）")
    ok(floor_good(2400) > floor_good(1200), "音符越多，理论下限越高（线性）")

    # ② 往返：good 明显高于下限时，δ 代回模型必须还原 good
    n = 1373
    for good in (20, 50, 200):
        delta = delta_of(good, n)
        ok(delta is not None and abs(expected_good(delta, n) - good) < 1e-6,
           f"往返一致 good={good} → δ={delta:.2f} → {expected_good(delta, n):.6f}")

    # ③ good ≤ 下限 ⇒ δ 不可测（这正是「1 个 Good 不代表偏移」的判据）
    ok(delta_of(0, n) is None, "good=0 ⇒ δ 不可测")
    ok(delta_of(1, n) is None, "good=1 ⇒ δ 不可测（1 < floor 1.18）")
    ok(delta_of(int(floor_good(n)) , n) is None, "good=floor ⇒ δ 不可测")

    # ④ good > 下限 ⇒ δ 落在正区间且随 good 单调增
    d1, d2 = delta_of(20, n), delta_of(200, n)
    ok(d1 is not None and d1 > 0, f"good>floor ⇒ δ>0（{d1:.2f}）")
    ok(d2 > d1, f"good 越多 δ 越大（{d1:.1f} < {d2:.1f}）")

    # ⑤ 全 Good 是数据异常，不硬算
    ok(delta_of(n, n) is None, "good=n_note ⇒ 不硬算")

    print(f"\ncourse_rounds selftest: {'OK' if bad == 0 else f'{bad} failed'}")
    return 1 if bad else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="课题模式轮次对照表")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add", help="补一轮（goods 从结算页读）")
    a.add_argument("--at", required=True, help="course_<HHmmss>.csv 里的 HHmmss")
    a.add_argument("--ver", default="", help="build 版本，如 94b5f4b-user")
    a.add_argument("--goods", nargs="+", type=int, required=True)
    a.add_argument("--scores", nargs="+", type=int, default=None)
    a.add_argument("--total", type=int, default=None)
    a.add_argument("--note", default="")
    a.set_defaults(func=cmd_add)
    s = sub.add_parser("show", help="打印汇总")
    s.set_defaults(func=cmd_show)
    t = sub.add_parser("selftest", help="内置不变量检查")
    t.set_defaults(func=cmd_selftest)
    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
