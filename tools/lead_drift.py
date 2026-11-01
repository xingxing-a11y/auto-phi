"""从已有 trace 头部里抽出每轮的 `zero_rel_ms`（= 点 ▶ 到谱面 t=0 的时间 = lead），
统计同一首曲跨轮的漂移 —— 这是 B 方案（两段锚）能不能成立的关键量。

为什么写它：lead 漂移原本打算再做几轮真机实验去测，但 trace 头里**已经记着**这个值
（`zero_rel_ms = onset_ms − sil_ref_ms − l_dev_ms`，而 `onset_ms` 本来就是相对点 ▶ 的时刻）。
那就直接挖历史数据，不用再打枪。
"""
from __future__ import annotations

import collections
import glob
import re
import statistics as st
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    rows = []
    for p in sorted(glob.glob(str(ROOT / "out" / "*trace*.csv"))):
        # 头部有两行注释：# inject_mode=... 与 # run ...（后者才有 zero_rel_ms）⇒ 两行都收
        with open(p, encoding="utf-8", errors="replace") as f:
            head = ""
            for _ in range(4):
                line = f.readline()
                if not line.startswith("#"):
                    break
                head += " " + line.strip()
        m = dict(re.findall(r"(\w+)=([^\s]+)", head))
        if "zero_rel_ms" not in m:
            continue
        rows.append((Path(p).name, m.get("song"), float(m["zero_rel_ms"]),
                     m.get("inject_mode"), m.get("l_dev_ms")))
    by = collections.defaultdict(list)
    for name, song, z, mode, l in rows:
        by[song].append((name, z, mode, l))
    for song, vs in by.items():
        zs = [v[1] for v in vs]
        print(f"{song}: {len(zs)} 轮 | lead 均值 {st.mean(zs):.0f} ms | "
              f"跨度 {max(zs) - min(zs):.0f} ms | sd {st.pstdev(zs) if len(zs) > 1 else 0:.0f} ms")
        for n, z, mode, l in vs:
            print(f"    {z:8.1f}  {str(mode):14s} l_dev={l}  {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
