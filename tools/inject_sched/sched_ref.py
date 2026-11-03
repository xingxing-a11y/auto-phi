#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""S1-b 参考实现：两级调度的**决策规则**（纯逻辑，确定性，可逐条对拍）。

为什么要单独做「决策规则」而不是直接跑随机模拟
  `tools/apk_probe/sched_sim.py` 用随机抖动分布证明「两级调度能把 p99 收回 5 ms 内」——
  那是**可行性**。但端内真正要落地的是**决策规则本身**（发还是丢、什么时候开始忙等）。
  规则一旦两端实现不一致，端内行为就和 PC 侧验证过的模型不是一回事。
  所以把规则抽成确定性函数：给定 (事件序列, 固定 cost, 固定 tick 偏差)
  两端必须产出**完全相同的 emit/drop 序列与时刻**。

规则（与 sched_sim.py 的 prio 分支逐字一致，改这里必须同步改那边）
  关键事件（DOWN/UP/POINTER_DOWN/POINTER_UP）：
      start = max(due + tick, finish);  finish = start + cost      —— 必须发
  非关键（MOVE）：
      若 finish <= due 才发（说明"现在有空，不会拖累后面的关键事件"），否则丢
      start = max(due + tick, finish);  finish = start + cost

为什么 MOVE 的判据是 `finish <= due` 而不是 `finish <= due + slack`
  这里刻意取最保守形式：只要上一条还没做完就丢 MOVE。
  放宽 slack 会让 MOVE 挤占关键事件的时间预算 —— sched_sim 的 full 模式
  （不区分对待）正是这样在 lacamp 上把 max 拖到 47 ms 的。

用法
  .venv/Scripts/python.exe tools/inject_sched/sched_ref.py out/touches_demiurge.json --cost 0.5
  .venv/Scripts/python.exe tools/inject_sched/sched_ref.py --all --cost 3.27
  .venv/Scripts/python.exe tools/inject_sched/sched_ref.py --check    # 自测
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))

import payload_ref  # noqa: E402  复用同一份解析+坐标变换


def schedule(cmds: list[dict], cost_ms: float, tick_ms: float = 0.0) -> list[dict]:
    """返回每条事件的决策：emit/drop + start_ms（相对零点）+ dev_ms（滞后）。"""
    out = []
    finish = float("-inf")          # 上一条注入完成的时刻
    for c in cmds:
        due = c["t_ms"]
        wake = due + tick_ms
        if c["is_key"]:
            start = max(wake, finish)
            finish = start + cost_ms
            out.append({"seq": c["seq"], "action": c["action"], "key": 1,
                        "decision": "emit", "start_ms": start,
                        "dev_ms": start - due})
        else:
            if finish <= due:
                start = max(wake, finish)
                finish = start + cost_ms
                out.append({"seq": c["seq"], "action": c["action"], "key": 0,
                            "decision": "emit", "start_ms": start,
                            "dev_ms": start - due})
            else:
                out.append({"seq": c["seq"], "action": c["action"], "key": 0,
                            "decision": "drop", "start_ms": None, "dev_ms": None})
    return out


def stats(plan: list[dict]) -> dict:
    """关键事件滞后分布 + 丢弃率。"""
    devs = [p["dev_ms"] for p in plan if p["key"] == 1]
    n_move = sum(1 for p in plan if p["key"] == 0)
    n_drop = sum(1 for p in plan if p["decision"] == "drop")
    devs_sorted = sorted(devs)

    def pct(q):
        if not devs_sorted:
            return 0.0
        return devs_sorted[min(int(len(devs_sorted) * q), len(devs_sorted) - 1)]

    return {
        "n_key": len(devs),
        "n_move": n_move,
        "n_drop": n_drop,
        "drop_rate": (n_drop / n_move) if n_move else 0.0,
        "p50": pct(0.50),
        "p99": pct(0.99),
        "max": max(devs) if devs else 0.0,
    }


def emit_tsv(plan: list[dict], fp=sys.stdout) -> None:
    # 与 Java 侧逐字节对齐：LF 换行 + 固定 6 位小数
    fp.write("seq\taction\tis_key\tdecision\tstart_ms\tdev_ms\n")
    for p in plan:
        s = "%.6f" % p["start_ms"] if p["start_ms"] is not None else "-"
        d = "%.6f" % p["dev_ms"] if p["dev_ms"] is not None else "-"
        fp.write(f"{p['seq']}\t{p['action']}\t{p['key']}\t{p['decision']}\t{s}\t{d}\n")


# ─────────────────────────── 自测 ───────────────────────────

def _check() -> int:
    fails = []

    def chk(cond, msg):
        print(("  OK   " if cond else "  FAIL ") + msg)
        if not cond:
            fails.append(msg)

    print("=== 1. 合成用例：手算可验（间隔 1 ms、cost 3 ms ⇒ 密集段必须丢 MOVE）===")
    # t: 0 DOWN(key) / 1 MOVE / 2 MOVE / 5 MOVE / 6 UP(key)
    syn = [
        {"seq": 0, "t_ms": 0.0, "action": "DOWN", "is_key": 1},
        {"seq": 1, "t_ms": 1.0, "action": "MOVE", "is_key": 0},
        {"seq": 2, "t_ms": 2.0, "action": "MOVE", "is_key": 0},
        {"seq": 3, "t_ms": 5.0, "action": "MOVE", "is_key": 0},
        {"seq": 4, "t_ms": 6.0, "action": "UP",   "is_key": 1},
    ]
    p = schedule(syn, cost_ms=3.0)
    # 手算：DOWN start=0 finish=3
    #  MOVE@1: finish(3) <= 1? 否 ⇒ drop
    #  MOVE@2: 3 <= 2? 否 ⇒ drop
    #  MOVE@5: 3 <= 5? 是 ⇒ start=5 finish=8
    #  UP@6: start=max(6,8)=8 finish=11 ⇒ dev=2
    got = [(x["seq"], x["decision"]) for x in p]
    exp = [(0, "emit"), (1, "drop"), (2, "drop"), (3, "emit"), (4, "emit")]
    chk(got == exp, f"决策序列 {got} == 手算 {exp}")
    chk(abs(p[3]["start_ms"] - 5.0) < 1e-9, f"MOVE@5 的 start={p[3]['start_ms']}（应为 5，不早于 due）")
    chk(abs(p[4]["start_ms"] - 8.0) < 1e-9, f"UP@6 的 start={p[4]['start_ms']}（应为 8 = 上条完成时刻）")
    chk(abs(p[4]["dev_ms"] - 2.0) < 1e-9, f"UP 滞后 {p[4]['dev_ms']}（应为 2）")

    print("\n=== 2. 边界：单条事件 / 全 MOVE / 全关键 ===")
    chk(schedule([], 1.0) == [], "空输入 ⇒ 空输出")
    one = schedule([{"seq": 0, "t_ms": 10.0, "action": "DOWN", "is_key": 1}], 1.0)
    chk(len(one) == 1 and one[0]["decision"] == "emit" and one[0]["start_ms"] == 10.0,
        "单条关键事件 ⇒ 正好在 due 上发，滞后 0")
    allmove = schedule([{"seq": 0, "t_ms": 0.0, "action": "MOVE", "is_key": 0},
                        {"seq": 1, "t_ms": 1.0, "action": "MOVE", "is_key": 0}], 5.0)
    chk(allmove[0]["decision"] == "emit", "首条 MOVE：finish=-inf ⇒ 必发")
    chk(allmove[1]["decision"] == "drop", "第二条 MOVE：上条未完成 ⇒ 丢")

    print("\n=== 3. ★ cost=0 的退化：不该丢任何 MOVE（判据要能区分「规则」与「cost」）===")
    real = payload_ref.build(ROOT / "out" / "touches_demiurge.json")
    z = schedule(real, cost_ms=0.0)
    chk(all(x["decision"] == "emit" for x in z), "cost=0 ⇒ 全部 emit（丢弃只由积压引起）")
    chk(all(abs(x["dev_ms"]) < 1e-9 for x in z), "cost=0 ⇒ 所有事件滞后 0")

    print("\n=== 4. 真实曲目：cost 越大丢得越多（单调性）===")
    prev = -1
    for cost in (0.5, 1.0, 3.27):
        st = stats(schedule(real, cost_ms=cost))
        chk(st["n_drop"] >= prev, f"cost={cost}: 丢弃 {st['n_drop']} ≥ 上一档 {prev}")
        prev = st["n_drop"]
        print(f"     cost={cost:5}: 关键 {st['n_key']} p50={st['p50']:.2f} "
              f"p99={st['p99']:.2f} max={st['max']:.2f} 丢 {st['drop_rate']*100:.1f}%")

    print("\n=== 5. 关键事件永不丢（安全不变量）===")
    for cost in (0.5, 3.27, 50.0):
        st = stats(schedule(real, cost_ms=cost))
        n_emit_key = sum(1 for x in schedule(real, cost_ms=cost)
                         if x["key"] == 1 and x["decision"] == "emit")
        chk(n_emit_key == st["n_key"], f"cost={cost}: 关键事件全部 emit（{n_emit_key}/{st['n_key']}）")

    print("\n" + ("全部通过" if not fails else f"失败 {len(fails)} 项"))
    return 1 if fails else 0


def main() -> int:
    try:
        sys.stdout.reconfigure(newline="\n")
    except (AttributeError, ValueError):
        pass

    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    if args[0] == "--check":
        return _check()

    cost = 0.5
    if "--cost" in args:
        cost = float(args[args.index("--cost") + 1])

    if args[0] == "--all":
        for k in ("demiurge", "kuangxi", "lacamp"):
            p = ROOT / "out" / f"touches_{k}.json"
            if not p.exists():
                continue
            st = stats(schedule(payload_ref.build(p), cost_ms=cost))
            print(f"# {k}: 关键 {st['n_key']} p50={st['p50']:.2f} p99={st['p99']:.2f} "
                  f"max={st['max']:.2f} 丢MOVE {st['drop_rate']*100:.1f}%", file=sys.stderr)
        return 0

    p = Path(args[0])
    if not p.is_absolute():
        p = ROOT / p
    emit_tsv(schedule(payload_ref.build(p), cost_ms=cost))
    return 0


if __name__ == "__main__":
    sys.exit(main())
