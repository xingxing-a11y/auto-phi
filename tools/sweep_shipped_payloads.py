#!/usr/bin/env python
"""全库离线普查：对**已出货**的 `out/touches_<key>.json` 逐个跑 `replay_score`，汇总任何非 Perfect。

为什么不用 `sweep_charts --score`：那个是"用调度器现算"，验的是调度器；
本脚本验的是**真正打进 APK 的载荷**（含本轮新补的 36 条），这正是用户要的"A"。

用法：PYTHONUTF8=1 .venv/Scripts/python.exe tmp/_sweep_shipped.py [--workers 6] [--limit N]
输出：`tmp/_sweep_shipped.json`（逐条 strict/lenient 汇总）+ 控制台只打"有问题"的条目。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def keys() -> list[str]:
    man = json.loads((ROOT / "android/play/app/src/main/assets/manifest.json")
                     .read_text(encoding="utf-8"))["songs"]
    return [e["key"] for e in man]


_TL = threading.local()


def _tmp_slot() -> Path:
    """每个线程一个固定的临时文件（覆盖写，不删除）—— 避开"批量删文件被拦截"的环境。"""
    p = getattr(_TL, "slot", None)
    if p is None:
        p = ROOT / "tmp" / f"_rs_worker_{threading.get_ident()}.json"
        _TL.slot = p
    return p


def one(key: str) -> tuple[str, dict | None, str]:
    notes = ROOT / "out" / f"notes_{key}.json"
    touches = ROOT / "out" / f"touches_{key}.json"
    if not (notes.is_file() and touches.is_file()):
        return key, None, "缺 notes/touches"
    # ⚠ 临时文件按**线程**分配、且**不删除**（2026-10-28 改）：
    #    修前是「每个 key 一个文件 + finally 里 unlink」⇒ 1056 条要删 1056 次，
    #    在受限环境（删文件被安全策略拦截）下整个普查直接被杀：exit=1，只剩一行日志、
    #    1007 条 `[safe-delete]` 拦截记录，**一条结果都没有**，而表面看像"跑完了"。
    #    现在：每个 worker 复用一个固定路径（并发安全），只覆盖不删除。
    out = _tmp_slot()
    r = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "replay_score.py"),
         "--notes", str(notes), "--touches", str(touches), "--json", str(out)],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        return key, None, (r.stderr or "")[-160:]
    try:
        d = json.loads(out.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return key, None, f"读结果失败 {exc}"
    return key, d, ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()
    ks = keys()
    if a.limit:
        ks = ks[: a.limit]
    print(f"普查 {len(ks)} 条已出货载荷（workers={a.workers}）…", flush=True)
    t0 = time.time()
    rows, bad, done = [], [], 0
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        futs = {ex.submit(one, k): k for k in ks}
        for f in as_completed(futs):
            key, d, err = f.result()
            done += 1
            if d is None:
                bad.append({"key": key, "err": err})
            else:
                rows.append({"key": key, "notes": d["note_count"],
                             "strict": d["strict"], "lenient": d["lenient"]})
            if done % 100 == 0:
                print(f"  {done}/{len(ks)} 用时 {time.time()-t0:.0f}s 失败 {len(bad)}", flush=True)

    def nonperfect(r: dict) -> int:
        m = r["strict"]
        return (m.get("miss", 0) + m.get("good", 0) + m.get("bad", 0))

    problem = [r for r in rows if nonperfect(r) > 0]
    disagree = [r for r in rows if r["strict"].get("miss", 0) != r["lenient"].get("miss", 0)]
    (ROOT / "tmp" / "_sweep_shipped.json").write_text(
        json.dumps({"rows": rows, "bad": bad}, ensure_ascii=False), encoding="utf-8")
    print(f"\n完成 {len(rows)} 条，失败 {len(bad)} 条，用时 {time.time()-t0:.0f}s")
    print(f"严格模型下**非全 P** 的条目：{len(problem)}")
    for r in sorted(problem, key=lambda x: -(x["strict"].get("miss", 0)
                                             + x["strict"].get("good", 0)))[:15]:
        m = r["strict"]
        print(f"  {r['key']:24s} notes={r['notes']:4d} P={m.get('perfect')} "
              f"G={m.get('good')} B={m.get('bad')} M={m.get('miss')}")
    print(f"两套模型判分不一致的条目：{len(disagree)}")
    if bad:
        print("跑不起来的：" + ", ".join(b["key"] for b in bad[:10]))
    return 0 if not problem and not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
