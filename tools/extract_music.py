#!/usr/bin/env python
"""A 段（音频）：从 Phigros 安装包里按需解出 `samples/<曲>/music.wav` + `music.info.json`。

数据流与 `tools/extract_chart.py` 完全同路（都是 Addressables）：

    APK(ZIP)
      └─ assets/aa/catalog.json                 ← Addressables 目录表（明文 JSON）
           └─ 键 `Assets/Tracks/<曲>/music.wav`
                └─ dependencyKey → 所在 bundle
                     └─ assets/aa/Android/<hash>.bundle   ← UnityFS 容器
                          └─ UnityPy → AudioClip.samples ← 导出 WAV

**为什么要它**：端内定零点的公式是 `zero = onset − sil_ref − L_dev`，其中 `sil_ref`（母带静音长度）
和标定用的 30 s 包络指纹**都必须从母带算**。所以 `pack_assets.py` 要求
`samples/<曲目录>/music.wav` 在场；没有它打不出可用的 assets。

用法（Git Bash，项目根；必须用 .venv 的 python）：

    # 列出 catalog 里能取到音频的曲目（不下载任何 bundle）
    .venv/Scripts/python.exe tools/extract_music.py --list

    # 取一首（按曲目录名精确匹配，或子串匹配）
    .venv/Scripts/python.exe tools/extract_music.py --song "996.李化禹.0"

    # 取全部（约 3~8 GB 流量/磁盘；已存在的会跳过，可中断后续跑）
    .venv/Scripts/python.exe tools/extract_music.py --all

    # 用本地 APK（离线、可重复；不给 --apk 时走发行方的公开直链 + HTTP Range）
    .venv/Scripts/python.exe tools/extract_music.py --all --apk D:/Phigros.apk

⚠ 导出的音频是**游戏资产**，仅供本机自用；不要随仓库或安装包分发。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import UnityPy                                      # noqa: E402
from UnityPy.enums import ClassIDType               # noqa: E402

from pgauto.apk import open_apk                     # noqa: E402
from pgauto.catalog import ANDROID_PREFIX, AddressablesCatalog  # noqa: E402

TRACK_PREFIX = "Assets/Tracks/"
MUSIC_SUFFIX = "/music.wav"


def all_songs(cat) -> list[str]:
    """catalog 里所有能取到音频的曲目录名。"""
    out = []
    for k in cat.by_key:
        if isinstance(k, str) and k.startswith(TRACK_PREFIX) and k.endswith(MUSIC_SUFFIX):
            out.append(k[len(TRACK_PREFIX):-len(MUSIC_SUFFIX)])
    return sorted(set(out))


def pick_songs(cat, want: list[str]) -> tuple[list[str], list[str]]:
    """按「精确 → 子串」匹配；返回 (命中的曲目录, 未命中的关键词)。"""
    songs = all_songs(cat)
    hit, miss = [], []
    for w in want:
        if w in songs:
            hit.append(w)
            continue
        sub = [s for s in songs if w in s]
        if sub:
            hit.extend(sub)
        else:
            miss.append(w)
    # 去重保序
    seen, uniq = set(), []
    for s in hit:
        if s not in seen:
            seen.add(s)
            uniq.append(s)
    return uniq, miss


def extract_one(z, cat, song: str, out_root: Path, force: bool = False) -> dict:
    """解一首的 music.wav + music.info.json。

    默认**已存在就跳过**（增量补缺，适合「只新增了几首歌」的版本更新）。
    `force=True` 时强制重解 —— 游戏大版本更新可能悄悄换掉老歌的母带/谱面，
    继续沿用旧文件会让 `sil_ref` 与音频指纹对不上，症状是"整首都偏/全 Good"。
    """
    out_dir = out_root / song
    wav = out_dir / "music.wav"
    if not force and wav.exists() and (out_dir / "music.info.json").exists():
        return {"song": song, "skipped": True, "wav_mib": wav.stat().st_size / 2 ** 20}

    key = f"Assets/Tracks/{song}/music.wav"
    entries = cat.by_key.get(key)
    if not entries:
        raise RuntimeError(f"catalog 无键 {key!r}")

    bundle_name = cat._bundle_of(entries[0], key)
    raw = z.read(ANDROID_PREFIX + bundle_name)
    env = UnityPy.load(raw)
    bf = env.files[next(iter(env.files))]
    clips = [o.read() for o in bf.get_filtered_objects([ClassIDType.AudioClip])]
    if not clips:
        raise RuntimeError("bundle 内没有 AudioClip")
    clip = next((c for c in clips if "music" in (c.m_Name or "").lower()), clips[0])

    data = clip.samples
    if isinstance(data, dict):
        data = next(iter(data.values()))
    if not data:
        raise RuntimeError("AudioClip.samples 为空（格式可能不是可解码的 PCM/Vorbis）")

    out_dir.mkdir(parents=True, exist_ok=True)
    wav.write_bytes(data)
    info = {
        "song": song,
        "target_key": key,
        "bundle": bundle_name,
        "name": clip.m_Name,
        "channels": clip.m_Channels,
        "frequency": clip.m_Frequency,
        "length_s": clip.m_Length,
        "load_type": str(clip.m_LoadType),
        "format": str(clip.m_CompressionFormat),
        "source_bundle_sha256": hashlib.sha256(raw).hexdigest(),
        "wav_mib": round(len(data) / 2 ** 20, 3),
        "bundle_mib": round(len(raw) / 2 ** 20, 3),
        "files": ["music.wav"],
    }
    (out_dir / "music.info.json").write_text(
        json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"song": song, "skipped": False, "wav_mib": info["wav_mib"],
            "bundle_mib": info["bundle_mib"], "format": info["format"]}


def main() -> int:
    ap = argparse.ArgumentParser(description="从 Phigros 安装包解出 samples/<曲>/music.wav")
    ap.add_argument("--song", action="append", default=[], help="曲目录名或子串，可重复")
    ap.add_argument("--all", action="store_true", help="解全部曲目（已存在的跳过）")
    ap.add_argument("--list", action="store_true", help="只列出可取的曲目，不下载")
    ap.add_argument("--apk", type=Path, default=None, help="本地 APK；省略则走公开直链 + Range")
    ap.add_argument("--out", type=Path, default=ROOT / "samples", help="输出根目录（默认 samples/）")
    ap.add_argument("--limit", type=int, default=0, help="最多解几首（冒烟用）")
    ap.add_argument("--force", action="store_true",
                    help="已存在也重解（游戏更新后老歌的母带可能变了）")
    args = ap.parse_args()

    cache = ROOT / ".cache" / "apk"
    _apk = {"z": None, "cat": None}

    def ensure_apk():
        """惰性打开安装包 —— 所有文件都已存在时**一个请求都不发**（断网也能续跑）。"""
        if _apk["z"] is None:
            z, desc = open_apk(args.apk, cache=cache)
            print(f"[apk] {desc}")
            _apk["z"] = z
            _apk["cat"] = AddressablesCatalog.from_bytes(z.read("assets/aa/catalog.json"))
        return _apk["z"], _apk["cat"]

    if not (args.list or args.all or args.song):
        ap.print_help()
        print("\n✗ 至少给 --song / --all / --list 之一", file=sys.stderr)
        return 2

    # ★ 快速通道：`--song` 给的正好是本地已有的曲目录（且没要 --force）⇒ 直接返回，
    #   **一个网络请求都不发**。断网时也能安全地重复跑同一条命令（幂等）。
    if args.song and not args.list and not args.all and not args.force:
        if all((args.out / w / "music.wav").exists() for w in args.song):
            print(f"[skip] 本地已存在，无需打开安装包：{list(args.song)}")
            return 0

    _, cat = ensure_apk()
    songs = all_songs(cat)
    if args.list:
        print(f"[list] catalog 里可取音频的曲目共 {len(songs)} 首")
        for s in songs[:40]:
            print("   ", s)
        if len(songs) > 40:
            print(f"    …… 其余 {len(songs) - 40} 首省略")
        return 0

    if args.all:
        targets, miss = songs, []
    else:
        targets, miss = pick_songs(cat, args.song)
    if miss:
        print(f"✗ 这些关键词在 catalog 里没有对上的曲目：{miss}", file=sys.stderr)
        return 2
    if args.limit:
        targets = targets[: args.limit]

    ok = skipped = 0
    failures = []
    t0 = time.perf_counter()
    for i, song in enumerate(targets, 1):
        try:
            zz, cc = ensure_apk()
            r = extract_one(zz, cc, song, args.out, force=args.force)
            if r["skipped"]:
                skipped += 1
                continue
            ok += 1
            print(f"  [{i:4d}/{len(targets)}] ✓ {song[:44]:46s} {r['wav_mib']:6.2f} MiB "
                  f"(bundle {r['bundle_mib']:5.2f})  fmt={r['format']}", flush=True)
        except Exception as e:                                   # noqa: BLE001
            failures.append({"song": song, "error": f"{type(e).__name__}: {e}"})
            print(f"  [{i:4d}/{len(targets)}] ✗ {song[:44]:46s} {type(e).__name__}: {e}",
                  flush=True)

    print(f"\n[done] 新解 {ok} ｜跳过（已存在）{skipped} ｜失败 {len(failures)} ｜"
          f"用时 {(time.perf_counter() - t0) / 60:.1f} min")
    if failures:
        print("失败前 10 条：")
        for f in failures[:10]:
            print("   -", f["song"], "→", f["error"])
        print("\n提示：失败通常是格式不可解码或网络中断；重跑会跳过已成功的，只补缺口。")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
