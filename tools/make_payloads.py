#!/usr/bin/env python
"""把 `out/charts/<曲>/<难度>.json` 变成 `pack_assets.py` 能吃的 `out/touches_<key>.json`。

为什么需要它：`extract_chart.py` 只解出官方谱面 JSON，`pack_assets.py` 只认
`out/touches_*.json`。中间的两步（谱面 → 音符 → 触摸事件）本来只有零散工具：

    parse_chart.py   谱面 JSON  →  out/notes_<key>.json（音符 + 判定线几何）
    schedule_chart.py  notes     →  out/touches_<key>.json（绝对时刻的触摸事件）

本脚本把这两步按曲库串起来，并且**负责起对文件名**——这一步不能靠猜：
1056 个 key 里 724 个带难度后缀（`70mf_ez`）、332 个是裸名（`996`），同一首曲还同时存在
`bounded_in` / `boundedquietude` / `boundedquietude_ez` 三套历史命名。名字由
`tools/song_keys.json` 显式给出；表里没有的谱（游戏刚出的新歌）退到 `<derive_key>_<难度>` 并警告。

用法（项目根目录，Git Bash）::

    .venv/Scripts/python.exe tools/make_payloads.py              # 增量：缺的补上
    .venv/Scripts/python.exe tools/make_payloads.py --limit 3    # 先跑 3 首看看
    .venv/Scripts/python.exe tools/make_payloads.py --force      # 全部重生成
    .venv/Scripts/python.exe tools/make_payloads.py --only 996 --only bounded_in
    .venv/Scripts/python.exe tools/make_payloads.py --prune-orphans   # 只报告没人认领的载荷

退出码：0 = 全部成功；1 = 有失败（清单写 `out/make_payloads_fail.txt`）。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHARTS = ROOT / "out" / "charts"
OUT = ROOT / "out"
TABLE = ROOT / "tools" / "song_keys.json"
FAIL_LIST = OUT / "make_payloads_fail.txt"
FALLBACK_LIST = OUT / "make_payloads_fallback.txt"

sys.path.insert(0, str(ROOT))
from pgauto.catalog import derive_key  # noqa: E402

#: 正式难度。`LEGACY` / `EZ_ERROR` 这类是游戏包里的非正式谱，不进曲库（见 plans §D3）。
FORMAL_DIFFS = {"EZ", "HD", "IN", "AT"}

#: 载荷带**手工修补**、调度器复现不出来的曲目：`--force` 也不覆盖，确实要重建得加 `--force-hand-tuned`。
#: 判据是「出货产物与重生成产物的事件序列不同」，不是凭印象：
#:   derrichter_at —— 4 个 Flick 同位 (540,1170)，一个触点最多判 3 个 ⇒ 手工改成第 2 触点提前就位、
#:                    判完即抬；出货 3706 事件 vs 调度器 3711（多出来的那 5 条会抢手指）。
#:   x47b5e6eb_at —— 10 指压力曲，手工补了 28 个事件；出货 6341 vs 调度器 6313。
HAND_TUNED = {
    "derrichter_at": "Flick 同位：第 2 触点提前就位（出货 3706 vs 调度器 3711）",
    "x47b5e6eb_at": "10 指压力：手工补 28 事件（出货 6341 vs 调度器 6313）",
}


def load_table(path: Path = TABLE) -> dict[tuple[str, str], str]:
    """`{"曲目录|难度": "key"}` → `{(曲目录, 难度): "key"}`。

    两项自检（fail-fast，都是会静默带偏的错）：
    - 同一 `(曲目录, 难度)` 出现两次 ⇒ JSON 里重复键会被 `json` 静默合并，看不出问题；
    - 两个不同的谱面映射到同一个 key ⇒ 后写的会覆盖前一个，等于少打一首。
    """
    raw = json.loads(path.read_text(encoding="utf-8"))
    table: dict[tuple[str, str], str] = {}
    for k, v in raw.items():
        song_dir, diff = k.rsplit("|", 1)
        assert (song_dir, diff) not in table, f"表里 (曲目录, 难度) 撞车：{song_dir}|{diff}"
        table[(song_dir, diff)] = v
    seen: dict[str, tuple[str, str]] = {}
    for pair, key in table.items():
        assert key not in seen, f"两个谱面映射到同一个 key {key!r}：{seen[key]} 与 {pair}"
        seen[key] = pair
    return table


def plan_items(
    table: dict[tuple[str, str], str],
    charts_root: Path = CHARTS,
    only: set[str] | None = None,
) -> tuple[list[tuple[str, str, str]], list[tuple[str, str]], list[tuple[str, str]]]:
    """扫磁盘，产出 `[(song_dir, diff, key)]` + 非正式谱 + 走 fallback 的。

    排序按 key，保证 `--limit` 每次截到的是同一批（可复现）。
    """
    items: list[tuple[str, str, str]] = []
    nonformal: list[tuple[str, str]] = []
    fallback: list[tuple[str, str]] = []
    for song_dir_path in sorted(charts_root.iterdir()) if charts_root.is_dir() else []:
        if not song_dir_path.is_dir():
            continue
        for chart in sorted(song_dir_path.glob("*.json")):
            song_dir, diff = song_dir_path.name, chart.stem.upper()
            if diff not in FORMAL_DIFFS:
                nonformal.append((song_dir, diff))
                continue
            key = table.get((song_dir, diff))
            if key is None:
                key = f"{derive_key(song_dir)}_{diff.lower()}"
                fallback.append((song_dir, diff))
            if only and key not in only:
                continue
            items.append((song_dir, diff, key))
    items.sort(key=lambda x: x[2])
    return items, nonformal, fallback


def _run(cmd: list[str]) -> tuple[int, str]:
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"          # 中文曲目录名 + JSON 读写，别赌系统代码页
    p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=env)
    tail = (p.stderr or p.stdout or "").strip().splitlines()
    return p.returncode, (tail[-1] if tail else "")


def build_one(song_dir: str, diff: str, key: str) -> tuple[str, str, str]:
    """解一首：谱面 → notes → touches。返回 `(key, 状态, 说明)`，状态 ∈ new/skip/fail。"""
    chart = CHARTS / song_dir / f"{diff}.json"
    notes = OUT / f"notes_{key}.json"
    touches = OUT / f"touches_{key}.json"
    py = sys.executable

    rc, msg = _run([py, str(ROOT / "tools" / "parse_chart.py"), str(chart), "-o", str(notes)])
    if rc != 0 or not notes.is_file():
        return key, "fail", f"parse_chart rc={rc} {msg}"
    rc, msg = _run([py, str(ROOT / "tools" / "schedule_chart.py"), str(notes), "-o", str(touches)])
    if rc != 0 or not touches.is_file():
        return key, "fail", f"schedule_chart rc={rc} {msg}"
    return key, "new", ""


def select_todo(
    items: list[tuple[str, str, str]],
    *,
    force: bool,
    force_hand_tuned: bool,
    out_dir: Path = OUT,
) -> tuple[list[tuple[str, str, str]], list[tuple[str, str, str]]]:
    """挑出这一轮要跑的，以及被手工修改保护挡下的。

    返回 `(todo, held)`。`held` 里的条目**不会**被生成 —— 它们的载荷是手工修过的，
    调度器重跑会退化（见 `HAND_TUNED` 里的实测差值）。
    """
    todo = [it for it in items if force or not (out_dir / f"touches_{it[2]}.json").is_file()]
    held = [it for it in todo if it[2] in HAND_TUNED and not force_hand_tuned]
    todo = [it for it in todo if it not in held]
    return todo, held


def find_orphans(table: dict[tuple[str, str], str]) -> list[str]:
    """`out/touches_*.json` 里没有表认领的（只报告，不删）。"""
    known = set(table.values())
    return sorted(p.stem.removeprefix("touches_") for p in OUT.glob("touches_*.json")
                  if p.stem.removeprefix("touches_") not in known)


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="谱面 → 音符 → 触摸载荷（pack_assets 的上游）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--limit", type=int, default=0, help="最多跑几首（按 key 排序后截断）")
    p.add_argument("--only", action="append", default=[], help="只跑这几首（按 key），可重复")
    p.add_argument("--force", action="store_true", help="已有 touches 也重生成")
    p.add_argument("--force-hand-tuned", action="store_true",
                   help=f"连手工修过的那 {len(HAND_TUNED)} 首也重生成（会丢掉手工修补，见 HAND_TUNED）")
    p.add_argument("--jobs", type=int, default=6, help="并发子进程数（默认 6）")
    p.add_argument("--prune-orphans", action="store_true", help="只列出没人认领的 out/touches_*.json")
    p.add_argument("-q", "--quiet", action="store_true")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    if not CHARTS.is_dir():
        print(f"✗ 缺 {CHARTS}（先跑：.venv/Scripts/python.exe tools/extract_chart.py --all）",
              file=sys.stderr)
        return 2

    table = load_table()
    if args.prune_orphans:
        orphans = find_orphans(table)
        print(f"没人认领的载荷：{len(orphans)} 个")
        for k in orphans[:20]:
            print(f"  out/touches_{k}.json")
        return 0

    items, nonformal, fallback = plan_items(table, only=set(args.only) or None)
    todo, held = select_todo(items, force=args.force, force_hand_tuned=args.force_hand_tuned)
    skipped = len(items) - len(todo) - len(held)
    if args.limit:
        todo = todo[: args.limit]

    forced_tuned = [k for _, _, k in todo if k in HAND_TUNED]
    if forced_tuned:
        print(f"⚠ --force-hand-tuned：{', '.join(forced_tuned)} 会丢掉手工修补，"
              f"重建后的版本比出货版本退化（见 HAND_TUNED 注释里的实测差值）", file=sys.stderr)

    if not args.quiet:
        print(f"谱面 {len(items)} 张｜非正式难度跳过 {len(nonformal)} 张｜待生成 {len(todo)}｜已存在 {skipped}")
        if held:
            print(f"  ⓘ 手工修过的 {len(held)} 首不覆盖（要重建加 --force-hand-tuned）："
                  + ", ".join(k for _, _, k in held))
            for k in (k for _, _, k in held):
                print(f"      {k}：{HAND_TUNED[k]}")
        if nonformal:
            sample = ", ".join(f"{s}/{d}" for s, d in nonformal[:3])
            print(f"  非正式难度（不打包）：{sample}{' …' if len(nonformal) > 3 else ''}")
        if fallback:
            print(f"  ⚠ 映射表里没有 {len(fallback)} 张，用 derive_key 兜底命名：")
            for s, d in fallback[:5]:
                print(f"      {s} / {d} ⇒ {derive_key(s)}_{d.lower()}")
        if todo:
            print("  本批：" + ", ".join(k for _, _, k in todo[:8])
                  + (" …" if len(todo) > 8 else ""))

    t0 = time.time()
    new = fail = 0
    fails: list[str] = []
    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as ex:
        futs = {ex.submit(build_one, s, d, k): k for s, d, k in todo}
        for i, fut in enumerate(as_completed(futs), 1):
            key, status, msg = fut.result()
            if status == "new":
                new += 1
            else:
                fail += 1
                fails.append(f"{key}\t{msg}")
                print(f"✗ {key}: {msg}", file=sys.stderr)
            if not args.quiet and (i % 25 == 0 or i == len(todo)):
                print(f"  [{i}/{len(todo)}] 新解 {new} 失败 {fail}")

    print(f"\n[done] 新解 {new}｜已存在跳过 {skipped}｜失败 {fail}｜用时 {(time.time() - t0) / 60:.1f} min")
    if fallback:
        FALLBACK_LIST.write_text("\n".join(f"{s}\t{d}" for s, d in fallback) + "\n",
                                 encoding="utf-8", newline="")
        print(f"· 兜底命名的清单：{FALLBACK_LIST.relative_to(ROOT)}（补进 tools/song_keys.json 后可重跑）")
    if fails:
        FAIL_LIST.write_text("\n".join(fails) + "\n", encoding="utf-8", newline="")
        print(f"✗ 失败清单：{FAIL_LIST.relative_to(ROOT)}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
