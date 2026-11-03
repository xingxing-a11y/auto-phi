#!/usr/bin/env python3
"""A 段：从 Phigros 安装包里解出官方谱面 JSON。

数据流::

    APK(ZIP)
      └─ assets/aa/catalog.json      ← Addressables 目录表（明文 JSON）
           └─ 谱面键 Assets/Tracks/<曲名>/Chart_<难度>.json
                └─ dependencyKey → 所在 bundle
                     └─ assets/aa/Android/<hash>.bundle  ← UnityFS 容器
                          └─ UnityPy → TextAsset.m_Script ← 谱面 JSON

**不下载整个 APK**：默认走 TapTap 官方直链 + HTTP Range，只按需读 ZIP 目录表
与该谱面的 bundle（实测单谱面约 1 MiB 流量，全量 1064 个谱面约 465 MiB）。

用法::

    # 列清单（不下载任何 bundle）
    python tools/extract_chart.py --list

    # 取一首（匹配曲名子串），全部难度
    python tools/extract_chart.py --song Rrharil

    # 取指定难度
    python tools/extract_chart.py --song Rrharil --diff AT,IN

    # 全量（约 465 MiB，带缓存可断点续传）
    python tools/extract_chart.py --all --jobs 16

    # 用本地 APK（离线可重复）
    python tools/extract_chart.py --apk D:/Phigros.apk --all
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import UnityPy
from UnityPy.enums import ClassIDType

from pgauto.apk import ApkZip, LocalSource, RemoteSource, open_apk, resolve_taptap_apk
from pgauto.catalog import ANDROID_PREFIX, AddressablesCatalog, ChartRef, diff_order

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "out" / "charts"
DEFAULT_CACHE = ROOT / ".cache" / "apk"

ILLEGAL = set('\\/:*?"<>|')


def safe_name(s: str) -> str:
    """把曲名压成合法文件名（只替换 Windows 禁止字符，其余原样保留）。"""
    return "".join("_" if c in ILLEGAL else c for c in s)


def load_catalog(z: ApkZip, verbose: bool = True) -> AddressablesCatalog:
    if verbose:
        print(f"[catalog] 读取 {ANDROID_PREFIX.replace('Android/', '')}catalog.json ...")
    raw = z.read("assets/aa/catalog.json")
    if verbose:
        print(f"[catalog] {len(raw) / 2**20:.2f} MiB，解析中 ...")
    return AddressablesCatalog.from_bytes(raw)


def extract_one(z: ApkZip, ref: ChartRef, out_root: Path) -> dict:
    """解一个谱面，返回该谱面的记录（含 sha256 与字节数）。"""
    bundle = z.read(ref.apk_path)
    env = UnityPy.load(bundle)
    if not env.files:
        raise RuntimeError(f"{ref.key}: bundle 内没有可读文件")
    bf = env.files[next(iter(env.files))]
    tas = list(bf.get_filtered_objects([ClassIDType.TextAsset]))
    if not tas:
        raise RuntimeError(f"{ref.key}: bundle 内没有 TextAsset")
    if len(tas) > 1:
        raise RuntimeError(f"{ref.key}: bundle 内有 {len(tas)} 个 TextAsset，无法确定取哪个")
    obj = tas[0].read()
    script = obj.m_Script
    data = script.encode("utf-8") if isinstance(script, str) else bytes(script)

    # 结构自检：必须是合法 PGR 谱面
    parsed = json.loads(data.decode("utf-8"))
    if "formatVersion" not in parsed or "judgeLineList" not in parsed:
        raise RuntimeError(f"{ref.key}: 解出的内容不是 PGR 谱面")

    dst = out_root / safe_name(ref.song) / f"{ref.difficulty}.json"
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(data)

    lines = parsed["judgeLineList"]
    notes = 0
    for ln in lines:
        notes += len(ln.get("notesAbove", []) or []) + len(ln.get("notesBelow", []) or [])
    return {
        "song": ref.song,
        "difficulty": ref.difficulty,
        "key": ref.key,
        "bundle": ref.bundle_name,
        "text_asset": obj.m_Name,
        "format_version": parsed["formatVersion"],
        "offset": parsed["offset"],
        "judge_lines": len(lines),
        "notes": notes,
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "path": str(dst.relative_to(out_root.parent)),
    }


def load_previous(out_root: Path) -> dict[str, dict]:
    """读上一轮的清单，筛出「文件还在且大小对得上」的记录。

    这是续传的依据。**只比大小不比 sha256** —— 对 2.5 GB 的产物集，
    每次全量重算哈希的代价不值当；真要强校验请跑 `tools/verify_extract.py`。
    """
    idx = out_root / "index.json"
    if not idx.exists():
        return {}
    try:
        prev_manifest = json.loads(idx.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    prev: dict[str, dict] = {}
    for r in prev_manifest.get("charts", []):
        p = out_root.parent / r["path"]
        try:
            if p.exists() and p.stat().st_size == r.get("bytes"):
                prev[r["key"]] = r
        except OSError:
            continue
    return prev


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="从 Phigros APK 解出官方谱面 JSON",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--apk", type=Path, default=None, help="本地 APK 路径；省略则走 TapTap 官方直链")
    p.add_argument("-o", "--out", type=Path, default=DEFAULT_OUT, help=f"输出目录（默认 {DEFAULT_OUT}）")
    p.add_argument("--cache", type=Path, default=DEFAULT_CACHE, help="bundle 落盘缓存目录（默认 .cache/apk）")
    p.add_argument("--no-cache", action="store_true", help="禁用缓存")
    p.add_argument("--no-resume", action="store_true", help="忽略上一轮清单，全部重解")
    p.add_argument("--song", action="append", default=[], help="曲名子串过滤，可重复")
    p.add_argument("--diff", default="", help="难度过滤，逗号分隔，如 AT,IN")
    p.add_argument("--all", action="store_true", help="取全部谱面（约 465 MiB）")
    p.add_argument("--limit", type=int, default=0, help="最多解几个（调试用）")
    p.add_argument("--jobs", type=int, default=8, help="并发下载/解析线程数（默认 8）")
    p.add_argument("--list", action="store_true", help="只列出谱面清单，不下载 bundle")
    p.add_argument("-q", "--quiet", action="store_true")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    t0 = time.time()

    if not args.all and not args.song and not args.list:
        print("请指定 --song / --all，或先用 --list 看清单。", file=sys.stderr)
        return 2

    z, desc = open_apk(args.apk, cache=None if args.no_cache else args.cache, verbose=not args.quiet)
    try:
        if not args.quiet:
            print(f"[apk] {desc}  条目 {z.entry_count}")
        cat = load_catalog(z, verbose=not args.quiet)

        charts = cat.charts()
        if not args.quiet:
            h = cat.provider_histogram()
            print(f"[catalog] entry {len(cat.entries)}  谱面 {len(charts)}")
            print(f"[catalog] provider 分布: {h}")

        if args.list:
            by_song: dict[str, list[str]] = {}
            for c in charts:
                by_song.setdefault(c.song, []).append(c.difficulty)
            print(f"\n{'曲名':<46} 难度")
            print("-" * 76)
            for song in sorted(by_song):
                diffs = sorted(by_song[song], key=diff_order)
                print(f"{song:<46} {','.join(diffs)}")
            print(f"\n共 {len(by_song)} 首 / {len(charts)} 个谱面")
            return 0

        sel = list(charts)
        if args.song:
            needles = [s.lower() for s in args.song]
            sel = [c for c in sel if any(n in c.song.lower() for n in needles)]
        if args.diff:
            want = {d.strip() for d in args.diff.split(",") if d.strip()}
            sel = [c for c in sel if c.difficulty in want]
        if args.limit:
            sel = sel[: args.limit]

        if not sel:
            print("没有匹配的谱面。用 --list 看清单。", file=sys.stderr)
            return 1

        args.out.mkdir(parents=True, exist_ok=True)
        prev = {} if args.no_resume else load_previous(args.out)
        sel_keys = {c.key for c in sel}
        reused = [prev[c.key] for c in sel if c.key in prev]
        todo = [c for c in sel if c.key not in prev]

        need_bytes = sum(z.entry(c.apk_path).csize for c in todo if z.entry(c.apk_path))
        if not args.quiet:
            print(f"\n[计划] 目标 {len(sel)} 个谱面：已完成 {len(reused)}，待解 {len(todo)}，"
                  f"bundle 约 {need_bytes / 2**20:.1f} MiB，并发 {args.jobs}")

        records: list[dict] = list(reused)
        failed: list[tuple[str, str]] = []

        def work(ref: ChartRef) -> dict:
            if args.apk is None:
                # 远程源：多线程各自持 HTTP 连接，共用一个 ApkZip 读目录表没问题
                return extract_one(z, ref, args.out)
            # 本地源：file 句柄需串行
            with LocalSource(args.apk) as src:
                return extract_one(ApkZip(src), ref, args.out)

        done = 0
        if todo:
            with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
                for ref, res in zip(todo, pool.map(_guard(work), todo)):
                    done += 1
                    if isinstance(res, Exception):
                        failed.append((ref.key, repr(res)))
                        print(f"  [失败] {ref.key}: {res}", file=sys.stderr)
                    else:
                        records.append(res)
                    if not args.quiet and (done % 25 == 0 or done == len(todo)):
                        print(f"  {done}/{len(todo)} ... 用时 {time.time() - t0:.1f}s")

        # 清单 = 本目录的**存盘点**：本轮选择之外的旧记录也保留
        kept = [v for k, v in prev.items() if k not in sel_keys]
        manifest = {
            "source": desc if args.apk else "TapTap CDN (Range)",
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "chart_count": len(records) + len(kept),
            "failed_count": len(failed),
            "failed": [{"key": k, "error": e} for k, e in failed],
            "charts": sorted(
                kept + records, key=lambda r: (r["song"], diff_order(r["difficulty"]))
            ),
        }
        (args.out / "index.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8"
        )

        print(f"\n[完成] 新解 {len(records) - len(reused)}  复用 {len(reused)}  "
              f"失败 {len(failed)}  用时 {time.time() - t0:.1f}s")
        print(f"[存盘] {manifest['chart_count']} 个谱面")
        print(f"[输出] {args.out}")
        print(f"[清单] {args.out / 'index.json'}")
        if isinstance(z.source, RemoteSource):
            print(f"[流量] {z.source.stat()}")
        return 0 if not failed else 1
    finally:
        z.close()


def _guard(fn):
    def wrapped(x):
        try:
            return fn(x)
        except Exception as e:  # noqa: BLE001 - 单点失败不应中断整批
            return e

    return wrapped


if __name__ == "__main__":
    raise SystemExit(main())
