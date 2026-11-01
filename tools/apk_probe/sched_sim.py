"""S1 验收判据的可行性自测 v2（纯离线，不碰设备）。

v1 的结论：把 events 里**所有**条目串行发出去，lacamp 的关键时刻误差 p99 会到 38 ms —— 判据不成立。
v1 的错误在于把 MOVE 和 DOWN/UP 当成同一类：

    DOWN / UP / POINTER_DOWN / POINTER_UP  → 决定「手指何时落到判定区」⇒ 直接影响 Tap/Flick 判定
    MOVE                                   → 只维持「手指还在判定区内」⇒ 时刻不影响判定

所以正确设计是 **关键事件优先、MOVE 可丢**。本脚本量化这个设计能不能把关键事件拉回 3 ms 内，
并回答「同毫秒事件合并成多指针 MotionEvent」能省多少。

用法：
    .venv/Scripts/python.exe tmp/_sched_sim.py
"""
from __future__ import annotations

import json
import math
import random
import sys
from pathlib import Path

# ★ 不能用 "/d/code/..." 这种 MSYS 路径：Python 在 Windows 下不认（自测实踩）
ROOT = Path(__file__).resolve().parents[2]   # tools/apk_probe/ → 仓库根

KEY_ACTIONS = {"DOWN", "UP", "POINTER_DOWN", "POINTER_UP"}


def lognorm_sampler(p50: float, p99: float):
    """按 p50 / p99 两点拟合对数正态。"""
    mu = math.log(p50)
    sigma = (math.log(p99) - mu) / 2.326347874
    return lambda: random.lognormvariate(mu, sigma)


def tick_sampler():
    """busy-wait 唤醒偏差：本轮实测 p50≈0 / p90 0.5 / p99 2.0 / max 3.2 ms。"""
    r = random.random()
    if r < 0.90:
        return random.uniform(0.0, 0.5)
    if r < 0.99:
        return random.uniform(0.5, 2.0)
    return random.uniform(2.0, 3.2)


def pct(v, q):
    v = sorted(v)
    return v[min(int(len(v) * q), len(v) - 1)]


def run(events, cost_fn, mode: str, runs: int = 120):
    """返回 (关键事件 p50/p99/max, 丢弃的 MOVE 比例)。"""
    key_t = [e["t_ms"] for e in events if e["action"] in KEY_ACTIONS]
    p50s, p99s, maxs, drops = [], [], [], []
    for _ in range(runs):
        finish = 0.0
        devs = []
        dropped = 0
        for e in events:
            t = e["t_ms"]
            is_key = e["action"] in KEY_ACTIONS
            wake = t + tick_sampler()
            if is_key:
                start = max(wake, finish)
                devs.append(start - t)
                finish = start + cost_fn()
            else:
                if mode == "full":
                    start = max(wake, finish)
                    finish = start + cost_fn()
                else:  # prio：只在「现在有空」时发 MOVE，否则丢弃
                    if finish <= t:
                        finish = max(wake, finish) + cost_fn()
                    else:
                        dropped += 1
        p50s.append(pct(devs, 0.50))
        p99s.append(pct(devs, 0.99))
        maxs.append(max(devs))
        drops.append(dropped / max(1, len(events) - len(key_t)))
    n = len(p50s)
    return (sum(p50s) / n, sum(p99s) / n, sum(maxs) / n, sum(drops) / n, len(key_t), len(events))


def main() -> int:
    scenarios = {
        "乐观(cost p99=1.87)": lognorm_sampler(0.51, 1.87),
        "悲观(cost p99=3.27)": lognorm_sampler(0.63, 3.27),
    }
    for key in ["demiurge", "kuangxi", "lacamp"]:
        raw = json.load(open(ROOT / "out" / f"touches_{key}.json", encoding="utf-8"))["events"]
        events = sorted(raw, key=lambda e: e["t_ms"])
        # 同毫秒可合并成多指针 MotionEvent
        n_buckets = len({e["t_ms"] for e in events})
        print(f"\n=== {key}: {len(events)} 事件 / {n_buckets} 个不同时刻 "
              f"⇒ 合并后省 {len(events) - n_buckets} 条 ({(1 - n_buckets / len(events)) * 100:.1f}%) ===")
        for name, fn in scenarios.items():
            for mode in ("full", "prio"):
                p50, p99, mx, drop, nk, ne = run(events, fn, mode)
                verdict = "PASS" if p99 < 3.0 else "FAIL"
                print(f"  {name:20} {mode:4} 关键{nk:5d}/{ne:5d} "
                      f"p50={p50:6.2f} p99={p99:6.2f} max={mx:6.2f} 丢MOVE={drop * 100:5.1f}%  {verdict}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
