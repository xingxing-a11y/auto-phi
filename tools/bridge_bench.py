#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""时钟桥标尺：量化 `dev_brain.clock_offset_s` 的**加性偏差**与**重复性**。

为什么要有这个脚本
------------------
dp3 轮 `μ = +32.1 ms`（整体偏晚）。嫌疑之一是「时钟桥」：devrun 是**唯一**把零点
经 PC 墙钟换算成设备墙钟的路径，而它用的估计式

    offset_est = dev_ms − (t0_pc + t1_pc)/2      # 取最小 RTT 的样本

只有在「PC→设备」与「设备→PC」延时对称、且**设备侧取钟时刻恰在 RTT 中点**时才无偏。
`adb shell date` 每次都要在设备上 **fork 一个 shell + exec toybox**，这段时间全在
「设备→PC」那一半里 ⇒ 取钟时刻远晚于 RTT 中点 ⇒ **offset 估大**（零点是设备墙钟，
估大 ⇒ 零点被推晚 ⇒ 音符整体偏晚）。这个偏置的方向与 dp3 的 μ 同号。

三条量：
  A. 现行法（每条一次 `adb shell date +%s%3N`）—— 重复 20 组 × 7 条，看组间散布
  B. 常驻 shell（`adb shell -T` + `while read` 循环）—— 设备侧只剩 `date` 一次 exec
  C. 地板：`adb shell true` 的 RTT（= 传输 + shell 启动）
A 与 B 之差 ≈ 「设备侧 fork/exec 造成的加性偏差」。若这个差值 ≳10 ms，就说明桥
**不能**按文档串写的「误差 1~3 ms」来用。

自测（`--selftest`，不碰设备）
-----------------------------
用一个「假设备」子进程（时钟 = PC + 已知偏移，回复前先 sleep 已知时长，模拟
fork/exec 发生在取钟之前），验证：
  S1 对称、无设备侧延时时，估计式能还原偏移（误差 < 3 ms）
  S2 设备侧延迟 30 ms 时，估计式**必然**偏 +15 ms（= s' − s/2，s'≈s）
  S3 把同一延迟改成「取钟之后再延」，偏差符号翻成 −15 ms
  S4 变异：把估计式的 /2 去掉（改成 t1）⇒ S1 必须失败（否则说明自测是空转）
⇒ S2/S3 是「这个标尺真的能抓到加性偏差」的证据，不是「跑完没报错」。

用法（Git Bash，cwd = D:/code/phigros-auto）
    .venv/Scripts/python.exe tools/bridge_bench.py --selftest
    .venv/Scripts/python.exe tools/bridge_bench.py            # 真机，约 40 s
    .venv/Scripts/python.exe tools/bridge_bench.py --rounds 40 --n 9
"""
from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path

ADB = r"D:\platform-tools\adb.exe"
DATE = "date +%s%3N"


# ─────────────────────────── 估计式（与 dev_brain 逐式一致） ───────────────────────────
def estimate(samples: list[tuple[float, float]]) -> dict:
    """samples = [(rtt_ms, dev_ms)]；取最小 RTT 的样本，用往返中点。

    返回 offset_ms = dev − (t0+t1)/2。★ 与 dev_brain.clock_offset_s 同一算法，
    改这里必须同步改那边（否则标尺量到的不是生产用的那个数）。
    """
    samples = sorted(samples, key=lambda x: x[0])
    rtt, off = samples[0]
    return {"offset_ms": off, "rtt_ms": rtt,
            "offs_all": [round(o, 2) for _, o in samples],
            "rtts_all": [round(r, 2) for r, _ in samples]}


# ─────────────────────────── 两种设备侧取钟法 ───────────────────────────
class PerCall:
    """A：每条命令一次 `adb shell`（现行 dev_brain 实现）。"""

    name = "A 逐条 adb shell date"

    def __init__(self, adb: str = ADB):
        self.adb = adb

    def sample(self, n: int) -> list[tuple[float, float]]:
        out = []
        for _ in range(n):
            t0 = time.time()
            r = subprocess.run([self.adb, "shell", DATE], capture_output=True, timeout=30)
            t1 = time.time()
            s = r.stdout.decode("utf-8", "replace").strip()
            if not s.isdigit():
                continue
            out.append(((t1 - t0) * 1000.0, int(s) - (t0 + t1) / 2.0 * 1000.0))
        return out


class Persistent:
    """B：常驻 `adb shell -T` + 设备侧 `while read` 循环 ⇒ 每个请求只 exec 一次 date。"""

    name = "B 常驻 shell 循环"
    LOOP = f'while IFS= read -r _l; do {DATE}; done'

    def __init__(self, adb: str = ADB):
        self.adb = adb
        self.proc = None

    def __enter__(self):
        self.proc = subprocess.Popen(
            [self.adb, "shell", "-T", self.LOOP],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            bufsize=0)
        # 预热一次：把 shell 启动 / 第一个 date 的 exec 抖掉
        self.sample(2)
        return self

    def __exit__(self, *a):
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()

    def sample(self, n: int) -> list[tuple[float, float]]:
        out = []
        for _ in range(n):
            t0 = time.time()
            self.proc.stdin.write(b"x\n")
            self.proc.stdin.flush()
            line = self.proc.stdout.readline()
            t1 = time.time()
            s = line.decode("utf-8", "replace").strip()
            if not s.isdigit():
                continue
            out.append(((t1 - t0) * 1000.0, int(s) - (t0 + t1) / 2.0 * 1000.0))
        return out


def floor_rtt(adb: str = ADB, n: int = 10) -> dict:
    """C：`adb shell true` 的 RTT —— 传输 + shell 启动的地板。"""
    xs = []
    for _ in range(n):
        t0 = time.time()
        subprocess.run([adb, "shell", "true"], capture_output=True, timeout=30)
        xs.append((time.time() - t0) * 1000.0)
    xs.sort()
    return {"min_ms": round(xs[0], 2), "median_ms": round(statistics.median(xs), 2)}


# ─────────────────────────── 自测：假设备 ───────────────────────────
FAKE = r'''
import sys, time
OFF = float(sys.argv[1]); S = float(sys.argv[2]); MODE = sys.argv[3]
for line in sys.stdin:
    if MODE == "pre":
        time.sleep(S)
        v = int((time.time() + OFF) * 1000)
        time.sleep(0.0002)
    else:
        v = int((time.time() + OFF) * 1000)
        time.sleep(S)
    sys.stdout.write(str(v) + "\n"); sys.stdout.flush()
'''


class FakeDev:
    """假设备：时钟 = PC + OFF，每次回复前/后 sleep S 秒（模拟 fork/exec 位置）。"""

    def __init__(self, off_s: float, s_s: float, mode: str):
        self.proc = subprocess.Popen(
            [sys.executable, "-c", FAKE, str(off_s), str(s_s), mode],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, bufsize=0)

    def sample(self, n: int) -> list[tuple[float, float]]:
        out = []
        for _ in range(n):
            t0 = time.time()
            self.proc.stdin.write(b"x\n")
            self.proc.stdin.flush()
            line = self.proc.stdout.readline()
            t1 = time.time()
            v = int(line)
            out.append(((t1 - t0) * 1000.0, v - (t0 + t1) / 2.0 * 1000.0))
        return out

    def close(self):
        self.proc.stdin.close()
        self.proc.wait(timeout=5)


def selftest() -> int:
    ok = True

    def chk(name, cond, detail=""):
        nonlocal ok
        print(f"  {'✓' if cond else '✗'} {name}{('  ' + detail) if detail else ''}")
        ok = ok and bool(cond)

    print("S1 对称、无设备侧延时 ⇒ 估计式应还原 +500 ms")
    d = FakeDev(0.5, 0.0, "pre")
    est = estimate(d.sample(15))
    d.close()
    chk("S1 offset≈+500", abs(est["offset_ms"] - 500.0) < 3.0, f"{est['offset_ms']:.1f} ms")

    print("S2 设备侧取钟前 sleep 30 ms ⇒ 模型预测偏 +15 ms")
    d = FakeDev(0.5, 0.030, "pre")
    est2 = estimate(d.sample(15))
    d.close()
    bias2 = est2["offset_ms"] - 500.0
    chk("S2 偏 +15±4 ms", 11.0 < bias2 < 19.0, f"实测偏 {bias2:+.1f} ms")

    print("S3 取钟后 sleep 30 ms ⇒ 符号应翻成 −15 ms")
    d = FakeDev(0.5, 0.030, "post")
    est3 = estimate(d.sample(15))
    d.close()
    bias3 = est3["offset_ms"] - 500.0
    chk("S3 偏 −15±4 ms", -19.0 < bias3 < -11.0, f"实测偏 {bias3:+.1f} ms")

    print("S4 变异：估计式把中点换成 t1 ⇒ 必须偏移 −RTT/2，且要被抓到")
    # ★ 不能用 S1（假设备 RTT 只有几 ms，−RTT/2 落在噪声里 ⇒ 变异测不出来 = 空转）。
    #   用 S3 的「取钟后 sleep 30 ms」：RTT ~30 ms ⇒ 变异位移 ~15 ms，必定可见。
    d = FakeDev(0.5, 0.030, "post")
    samples = d.sample(15)
    d.close()
    base = estimate(samples)["offset_ms"]
    bad = [(r, o - r / 2.0) for r, o in samples]   # 用 t1 当取钟时刻 = 偏 −RTT/2
    est4 = estimate(bad)
    chk("S4 变异被抓到", abs(est4["offset_ms"] - base) > 8.0,
        f"原 {base:.1f} → 变异 {est4['offset_ms']:.1f} ms（位移 {est4['offset_ms'] - base:+.1f}）")

    print("S5 真设备上会看到的现象：B 法的 RTT 应显著小于 A 法")
    print("   （真机数据见 main 输出；此处只断言自测本身的性质）")
    chk("S5 假设备的 RTT ≥ sleep 值（说明 RTT 确实包含设备侧耗时）",
        est2["rtt_ms"] > 25.0, f"RTT {est2['rtt_ms']:.1f} ms")
    print("SELFTEST", "PASS" if ok else "FAIL")
    return 0 if ok else 1


# ─────────────────────────── 真机 ───────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--rounds", type=int, default=20, help="A 法重复组数")
    ap.add_argument("--n", type=int, default=7, help="每组样本数（生产用 7）")
    ap.add_argument("--json", default="tmp/_bridge_bench.json")
    a = ap.parse_args()
    if a.selftest:
        return selftest()

    A = PerCall()
    print(f"== {A.name}：{a.rounds} 组 × {a.n} 条 ==")
    groups = []
    for k in range(a.rounds):
        est = estimate(A.sample(a.n))
        groups.append(est)
        if k % 4 == 0:
            print(f"  [{k:02d}] offset {est['offset_ms']:+9.1f} ms  minRTT {est['rtt_ms']:6.2f}")
    offs = [g["offset_ms"] for g in groups]
    rtts = [g["rtt_ms"] for g in groups]
    a_med, a_min, a_max = statistics.median(offs), min(offs), max(offs)
    a_sd = statistics.pstdev(offs)
    print(f"  → offset 组间：中位 {a_med:+.1f} / 最小 {a_min:+.1f} / 最大 {a_max:+.1f}"
          f" / σ {a_sd:.1f} ms；minRTT 中位 {statistics.median(rtts):.1f} ms")

    print(f"== {Persistent.name}：{a.rounds} 组 × {a.n} 条 ==")
    b_groups = []
    with Persistent() as B:
        for k in range(a.rounds):
            est = estimate(B.sample(a.n))
            b_groups.append(est)
            if k % 4 == 0:
                print(f"  [{k:02d}] offset {est['offset_ms']:+9.1f} ms  minRTT {est['rtt_ms']:6.2f}")
    b_offs = [g["offset_ms"] for g in b_groups]
    b_rtts = [g["rtt_ms"] for g in b_groups]
    b_med = statistics.median(b_offs)
    b_sd = statistics.pstdev(b_offs)
    print(f"  → offset 组间：中位 {b_med:+.1f} / 最小 {min(b_offs):+.1f}"
          f" / 最大 {max(b_offs):+.1f} / σ {b_sd:.1f} ms；minRTT 中位 "
          f"{statistics.median(b_rtts):.1f} ms")

    fl = floor_rtt()
    print(f"== C 地板（adb shell true）==  min {fl['min_ms']} / 中位 {fl['median_ms']} ms")

    # 组间同刻比较：A 与 B 交替测，避免「双钟漂移」把差值算进去
    print("== 交替测（A/B 各 1 组，重复 8 次）⇒ 差值剔除漂移 ==")
    pairs = []
    with Persistent() as B:
        for _ in range(8):
            ea = estimate(A.sample(3))
            eb = estimate(B.sample(3))
            pairs.append(ea["offset_ms"] - eb["offset_ms"])
    d_med = statistics.median(pairs)
    print(f"  A−B 中位 {d_med:+.1f} ms（8 组：{', '.join(f'{x:+.1f}' for x in pairs)}）")

    doc = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "rounds": a.rounds, "n": a.n,
        "A": {"name": A.name, "offset_median_ms": a_med, "offset_min_ms": a_min,
              "offset_max_ms": a_max, "sd_ms": a_sd,
              "rtt_median_ms": statistics.median(rtts), "groups": groups},
        "B": {"name": Persistent.name, "offset_median_ms": b_med,
              "offset_min_ms": min(b_offs), "offset_max_ms": max(b_offs),
              "sd_ms": b_sd, "rtt_median_ms": statistics.median(b_rtts),
              "groups": b_groups},
        "C_floor": fl,
        "AB_diff_median_ms": d_med, "AB_diff_pairs_ms": pairs,
    }
    Path(a.json).parent.mkdir(parents=True, exist_ok=True)
    Path(a.json).write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n落盘 {a.json}")
    print(f"★ 结论读法：|A−B| 就是「设备侧 fork/exec 造成的加性偏差」。若 ≳10 ms，"
          f"则 dev_brain 文档串写的「误差 1~3 ms」不成立；偏置方向按模型应为正（零点被推晚）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
