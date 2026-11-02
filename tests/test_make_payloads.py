"""`tools/make_payloads.py` 的纯函数层测试（不跑子进程、不写 out/）。

本文件锁四件事：
1. 表解析：`"曲目录|难度"` 必须拆成两个分量（带 `|` 的目录名不存在，但仍要能报错）；
2. 取名：表里有就用表、没有才 fallback，且 fallback 会被点名（不许静默）；
3. 过滤：非正式难度（`LEGACY` / `*_ERROR`）整类不进载荷；`--only` 精确匹配；
4. 排序：按 key 升序 —— `--limit` 截断必须可复现，否则每次跑的是不同的曲。

★ 防「空转断言」：每条断言前先确认夹具非空；变异（把表里 key 改一个字母）必须让第 2 条红。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import make_payloads as mp  # noqa: E402


def _mkcharts(root: Path, spec: dict[str, list[str]]) -> Path:
    charts = root / "charts"
    for song_dir, diffs in spec.items():
        d = charts / song_dir
        d.mkdir(parents=True, exist_ok=True)
        for diff in diffs:
            (d / f"{diff}.json").write_text("{}", encoding="utf-8")
    return charts


# ── ① 表解析 ────────────────────────────────────────────────────────────────
def test_load_table_splits_on_last_pipe(tmp_path):
    p = tmp_path / "t.json"
    p.write_text(json.dumps({"A.b.0|IN": "a_in", "A.b.0|HD": "a_hd"}, ensure_ascii=False),
                 encoding="utf-8")
    table = mp.load_table(p)
    assert len(table) == 2
    assert table[("A.b.0", "IN")] == "a_in"


def test_load_table_rejects_two_charts_sharing_a_key(tmp_path):
    """两个谱面映射到同一个 key ⇒ 后写的覆盖前一个，等于静默少打一首。"""
    p = tmp_path / "t.json"
    p.write_text(json.dumps({"A.0|IN": "same", "B.0|IN": "same"}), encoding="utf-8")
    with pytest.raises(AssertionError, match="同一个 key"):
        mp.load_table(p)


def test_real_table_is_not_empty_and_covers_songs():
    table = mp.load_table()
    assert len(table) == 1056, f"真实表条目 {len(table)}，预期 1056"
    assert ("BoundedQuietude.FiniteLimitvsSiLiS.0", "IN") in table


# ── ② 取名：表优先，缺失才 fallback，并点名 ─────────────────────────────────
def test_named_from_table(tmp_path):
    charts = _mkcharts(tmp_path, {"Song.One.0": ["EZ", "IN"]})
    table = {("Song.One.0", "EZ"): "song", ("Song.One.0", "IN"): "song_in"}
    items, nonformal, fallback = mp.plan_items(table, charts_root=charts)
    assert [k for _, _, k in items] == ["song", "song_in"]      # 按 key 升序
    assert not nonformal and not fallback


def test_unknown_chart_falls_back_and_is_reported(tmp_path):
    charts = _mkcharts(tmp_path, {"New.Song.0": ["AT"]})
    items, _, fallback = mp.plan_items({}, charts_root=charts)
    assert items and items[0][2] == "new_at", f"fallback 名字不对：{items}"
    assert fallback == [("New.Song.0", "AT")], "走了 fallback 却没被点名"


def test_chinese_name_falls_back_to_stable_hash(tmp_path):
    charts = _mkcharts(tmp_path, {"盏茗.茶鸣拾贰律.0": ["IN"]})
    items, _, _ = mp.plan_items({}, charts_root=charts)
    key = items[0][2]
    assert key.startswith("x") and key.endswith("_in") and len(key.split("_")[0]) == 9


# ── ③ 过滤 ──────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("diff", ["LEGACY", "EZ_ERROR", "Legacy"])
def test_nonformal_difficulties_are_excluded(tmp_path, diff):
    charts = _mkcharts(tmp_path, {"Song.One.0": [diff, "IN"]})
    table = {("Song.One.0", "IN"): "song_in"}
    items, nonformal, _ = mp.plan_items(table, charts_root=charts)
    assert [k for _, _, k in items] == ["song_in"]
    # 难度码统一大写（磁盘文件名 `Legacy.json` 也要归到这一类）
    assert nonformal == [("Song.One.0", diff.upper())]


def test_only_filter_is_exact(tmp_path):
    charts = _mkcharts(tmp_path, {"A.0": ["EZ"], "B.0": ["EZ"]})
    table = {("A.0", "EZ"): "a_ez", ("B.0", "EZ"): "b_ez"}
    items, _, _ = mp.plan_items(table, charts_root=charts, only={"b_ez"})
    assert [k for _, _, k in items] == ["b_ez"]


# ── ④ 排序与可复现 ──────────────────────────────────────────────────────────
def test_items_sorted_by_key(tmp_path):
    charts = _mkcharts(tmp_path, {"Z.0": ["EZ"], "A.0": ["EZ"], "M.0": ["EZ"]})
    table = {("Z.0", "EZ"): "zzz", ("A.0", "EZ"): "aaa", ("M.0", "EZ"): "mmm"}
    items, _, _ = mp.plan_items(table, charts_root=charts)
    assert [k for _, _, k in items] == ["aaa", "mmm", "zzz"], "没按 key 排序 ⇒ --limit 不可复现"


# ── ⑤ 变异：表里改一个字母，取名必须跟着变（证明「表真的在被读」）────────────
def test_mutated_table_changes_key(tmp_path):
    charts = _mkcharts(tmp_path, {"Song.One.0": ["IN"]})
    base, _, _ = mp.plan_items({("Song.One.0", "IN"): "song_in"}, charts_root=charts)
    mut, _, _ = mp.plan_items({("Song.One.0", "IN"): "song_IN_typo"}, charts_root=charts)
    assert base[0][2] == "song_in" and mut[0][2] == "song_IN_typo"
    assert base[0][2] != mut[0][2], "改表不影响取名 ⇒ 断言空转"


# ── ⑥ 孤儿载荷：只报告，不删 ────────────────────────────────────────────────
def test_orphans_detected_but_nothing_deleted(tmp_path, monkeypatch):
    out = tmp_path / "out"
    out.mkdir()
    for name in ("touches_known.json", "touches_stray.json"):
        (out / name).write_text("{}", encoding="utf-8")
    monkeypatch.setattr(mp, "OUT", out)
    orphans = mp.find_orphans({("Song.0", "IN"): "known"})
    assert orphans == ["stray"]
    assert (out / "touches_stray.json").is_file(), "只报告，绝不能删"


# ── ⑦ 手工修的载荷不许被 --force 冲掉 ───────────────────────────────────────
def test_hand_tuned_keys_exist_in_real_table():
    """保护名单必须落在真实表里，否则那条保护是死的（永远不会命中）。"""
    assert mp.HAND_TUNED, "保护名单不该为空"
    keys = set(mp.load_table().values())
    missing = sorted(set(mp.HAND_TUNED) - keys)
    assert not missing, f"保护名单里的 key 不在表中：{missing}"


def test_select_todo_holds_hand_tuned_under_force(tmp_path):
    k = sorted(mp.HAND_TUNED)[0]
    items = [("Song.0", "IN", k), ("Other.0", "EZ", "other_ez")]
    todo, held = mp.select_todo(items, force=True, force_hand_tuned=False, out_dir=tmp_path)
    assert [x[2] for x in held] == [k], "手工修的没被保护"
    assert [x[2] for x in todo] == ["other_ez"]


def test_select_todo_rebuilds_when_explicitly_allowed(tmp_path):
    k = sorted(mp.HAND_TUNED)[0]
    todo, held = mp.select_todo([("Song.0", "IN", k)], force=True,
                                force_hand_tuned=True, out_dir=tmp_path)
    assert not held and [x[2] for x in todo] == [k]


def test_select_todo_skips_existing_without_force(tmp_path):
    (tmp_path / "touches_other_ez.json").write_text("{}", encoding="utf-8")
    todo, held = mp.select_todo([("Other.0", "EZ", "other_ez")], force=False,
                                force_hand_tuned=False, out_dir=tmp_path)
    assert not todo and not held
