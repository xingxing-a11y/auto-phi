"""`tools/song_keys.json`（曲目录 + 难度 → 载荷 key）与 `pack_assets.SONGS` 的一致性。

为什么要有这张表：载荷文件名 `out/touches_<key>.json` 里的 key **不是**一条机械规则能推出来的
——1056 个 key 里 724 个带 `_ez/_hd/_in/_at` 后缀、332 个是裸名，同一首曲还同时存在
`bounded_in` / `boundedquietude` / `boundedquietude_ez` 三套历史命名。表由 `assets/manifest.json`
一次性生成（manifest 不导出，所以必须把结果随仓库分发）。

本文件锁五件事（都是「错了会静默带偏」的）：
1. 表与 `SONGS` **双向相等** —— 少一条 ⇒ `pack_assets` 静默少打一首；多一条 ⇒ 多出一张卡；
2. key 全局唯一 —— 撞车会让两首歌抢同一个 payload 文件；
3. key 不含 `EXCLUDE_KEYS` —— 那是作者机器的开发残留，进了表就会被自动收录；
4. key 必须是安全文件名（纯 ASCII 字母数字下划线）；
5. 表里的难度只允许 EZ/HD/IN/AT —— `LEGACY` / `*_ERROR` 不是正式谱。

★ 防「空转断言」：先断言两侧数量都对（1056），再比集合；否则两张空表也「相等」。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TABLE_PATH = ROOT / "tools" / "song_keys.json"

sys.path.insert(0, str(ROOT))
from tools.pack_assets import EXCLUDE_KEYS, SONGS  # noqa: E402

FORMAL_DIFFS = {"EZ", "HD", "IN", "AT"}
EXPECTED_ENTRIES = 1056


def _table() -> dict[str, str]:
    return json.loads(TABLE_PATH.read_text(encoding="utf-8"))


def test_table_exists_and_is_not_empty():
    """先钉「有东西可比」，否则下面那些集合断言在空表上也成立。"""
    assert TABLE_PATH.is_file(), f"缺 {TABLE_PATH}"
    tbl = _table()
    assert len(tbl) == EXPECTED_ENTRIES, f"条目数 {len(tbl)}，预期 {EXPECTED_ENTRIES}"
    assert len(SONGS) == EXPECTED_ENTRIES, f"SONGS 条目数 {len(SONGS)}，预期 {EXPECTED_ENTRIES}"


def test_keys_are_unique():
    tbl = _table()
    keys = list(tbl.values())
    dup = sorted({k for k in keys if keys.count(k) > 1})
    assert not dup, f"key 撞车：{dup[:5]}"


def test_table_and_songs_are_equal_sets():
    """双向相等：少一条会静默少打，多一条会多出一张卡。"""
    keys = set(_table().values())
    missing_in_table = sorted(set(SONGS) - keys)
    extra_in_table = sorted(keys - set(SONGS))
    assert not missing_in_table, f"SONGS 里有、表里没有：{missing_in_table[:5]}"
    assert not extra_in_table, f"表里有、SONGS 里没有：{extra_in_table[:5]}"


def test_no_excluded_key_leaks_into_table():
    keys = set(_table().values())
    leaked = sorted(keys & EXCLUDE_KEYS)
    assert not leaked, f"表里出现开发残留 key：{leaked[:5]}"


def test_keys_are_filename_safe():
    bad = [k for k in _table().values() if not re.fullmatch(r"[0-9a-z_]+", k)]
    assert not bad, f"key 含不适合做文件名的字符：{bad[:5]}"


def test_only_formal_difficulties():
    diffs = {k.rsplit("|", 1)[1] for k in _table()}
    assert diffs <= FORMAL_DIFFS, f"表里出现非正式难度：{sorted(diffs - FORMAL_DIFFS)}"


def test_every_entry_has_song_dir_and_diff():
    bad = [k for k in _table() if "|" not in k or not k.rsplit("|", 1)[0] or not k.rsplit("|", 1)[1]]
    assert not bad, f"格式不对的条目：{bad[:3]}"


@pytest.mark.parametrize("song_dir,diff,key", [
    ("BoundedQuietude.FiniteLimitvsSiLiS.0", "IN", "bounded_in"),
    ("BoundedQuietude.FiniteLimitvsSiLiS.0", "HD", "boundedquietude"),
    ("BoundedQuietude.FiniteLimitvsSiLiS.0", "EZ", "boundedquietude_ez"),
    ("70MinutesFighters.かたぎり.0", "EZ", "70mf_ez"),
    ("996.李化禹.0", "EZ", "996"),
])
def test_known_entries_are_pinned(song_dir, diff, key):
    """钉几条真实条目：表一旦被重新生成走样，这几条会先红。"""
    assert _table()[f"{song_dir}|{diff}"] == key
