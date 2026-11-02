"""`pgauto.catalog.derive_key` 的回归测试 —— 专防「中文曲名共用同一个 key」。

背景（2026-10-18 实测）：旧实现末尾是 `return k or "song"`，非 ASCII 曲名被正则删空后
**所有中文歌都映射到 `"song"`** ⇒ 共用 `out/touches_song.json` / `songs.song` ⇒ 静默打错谱面。
同一天还发现 `tmp/build_ap_pool.py` 有一份手抄副本，且用 `ch.isalnum()`（对中文返回 True），
两份规则对同一首中文歌给出**不同 key** ⇒ pool 归属静默错配。

★ 2026-11-02 搬进 `pgauto/catalog.py`：它决定 `out/touches_<key>.json` 的文件名，
属于生产逻辑，不该只活在 `tmp/`（公开仓没有 `tmp/`）。搬迁是**逐字复制**，断言一条不改。

本文件锁四件事：
1. 中文曲名不再退化成 `"song"`，且**互不相同**；
2. 纯 ASCII 曲名的 key **逐位不变**（不能为了修中文而改动现有几百份载荷的名字）；
3. 边界输入（空串 / 纯符号）不得抛异常，且必须能当文件名；
4. 规则只有一份：`build_ap_pool._derive_key` 委托到本实现（该条只在主仓可跑）。
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from pgauto.catalog import ALIAS, derive_key

ROOT = Path(__file__).resolve().parents[1]
TMP = ROOT / "tmp"

#: ⑤⑥ 依赖 `tmp/` 里的开发脚本：公开仓按设计不导出 `tmp/`（内部工具不在分发范围内），
#: 那几条只能跳过；但 ①~④ 直接测生产实现，**公开仓照样跑**。
needs_tmp = pytest.mark.skipif(
    not (TMP / "play.py").exists(),
    reason="公开仓不导出 tmp/（⑤⑥ 测的是开发脚本间的委托关系）",
)


def _load(name: str, filename: str):
    """按路径加载脚本（`tmp/` 不是包，也不该为测试改 sys.path）。"""
    spec = importlib.util.spec_from_file_location(name, TMP / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def play():
    return _load("_pg_play_under_test", "play.py")


@pytest.fixture(scope="module")
def pool():
    return _load("_pg_pool_under_test", "build_ap_pool.py")


# ── ① ASCII 曲名：逐位不变（这是「不能回归」的底线）──────────────────────────
@pytest.mark.parametrize("song,want", [
    ("Burn.NceS.0", "burn"),
    ("Cleyera.Riya.0", "cleyera"),          # ALIAS 命中
    ("csqn.Aoi.0", "csqn"),                 # 真实目录名（不是 C.s.q.n.）
    ("Der Schneid.0", "derschneid"),        # 不在 ALIAS ⇒ 由 ASCII 提取得到
    ("Random.SobremSilentroom.0", "random"),
])
def test_ascii_names_unchanged(song, want):
    assert derive_key(song) == want


# ── ② 中文曲名：不再是 "song"，且彼此不同 ──────────────────────────────────
CN = [
    "今天不是明天.PIKASONICfeat兰音Reine.0",
    "盏茗.茶鸣拾贰律.0",
    "雪降り雪が降っている.AiSSw夜輪ft結月ゆかり.0",
]


def test_chinese_names_are_not_collapsed_to_song():
    for s in CN:
        k = derive_key(s)
        assert k != "song", f"{s} 仍退化成 'song'"
        assert k.startswith("x") and len(k) == 9, f"期望 x+8hex，实际 {k}"


def test_chinese_names_are_distinct():
    keys = [derive_key(s) for s in CN]
    assert len(set(keys)) == len(keys), f"中文曲名撞 key：{keys}"


def test_chinese_key_is_stable():
    s = CN[0]
    assert derive_key(s) == derive_key(s)


# ── ③ 边界：空串 / 纯符号 / 带空格，不得抛异常且必须可作文件名 ───────────────
@pytest.mark.parametrize("song", ["", "。", " ... ", ".hidden.0"])
def test_degenerate_inputs_still_return_safe_key(song):
    k = derive_key(song)
    assert k, "不能返回空 key"
    assert all(c.isalnum() or c == "_" for c in k), f"key 含不安全字符：{k!r}"
    assert k != "song" or song == "", f"{song!r} 落进了 'song'"


# ── ④ 别名优先 ────────────────────────────────────────────────────────────
def test_alias_still_wins():
    alias_keys = list(ALIAS)[:3]
    assert alias_keys, "ALIAS 不该为空"
    for src in alias_keys:
        assert derive_key(src) == ALIAS[src]


# ── ⑤ 规则只有一份：build_ap_pool 与 play 都必须与生产实现逐项一致 ──────────
@needs_tmp
@pytest.mark.parametrize("song", [
    "Burn.NceS.0", "C.s.q.n..Aoi.0", "Der Schneid.0",
    *CN, "", "。",
])
def test_build_ap_pool_delegates_to_play(play, pool, song):
    assert pool._derive_key(song) == play.derive_key(song), (
        f"{song!r}: pool={pool._derive_key(song)!r} vs play={play.derive_key(song)!r}"
    )


@needs_tmp
@pytest.mark.parametrize("song", ["Burn.NceS.0", "C.s.q.n..Aoi.0", "Der Schneid.0", *CN])
def test_play_and_pool_both_match_production(play, pool, song):
    """★ 防「搬完之后 tmp 里还留着一份旧规则」：两边都必须等于 pgauto 的实现。"""
    assert play.derive_key(song) == derive_key(song)
    assert pool._derive_key(song) == derive_key(song)


# ── ⑥ key 撞车必须被拦住（难度检查拦不住：同难度、同 offset 时全过）───────────
def _fake_notes(root: Path, key: str, song_dir: str | None) -> None:
    out = root / "out"
    out.mkdir(exist_ok=True)
    payload = {} if song_dir is None else {"song_dir": song_dir}
    (out / f"notes_{key}.json").write_text(json.dumps(payload), encoding="utf-8")


@needs_tmp
def test_collision_same_song_passes(play, tmp_path, monkeypatch):
    _fake_notes(tmp_path, "random", "Random.SobremSilentroom.0")
    monkeypatch.setattr(play, "ROOT", tmp_path)
    play.guard_key_collision("Random.SobremSilentroom.0", "random")   # 不抛


@needs_tmp
def test_collision_different_song_blocked(play, tmp_path, monkeypatch):
    """库里真实存在这组：`Random.SobremSilentroom.0/1/2` 都导出 key `random`。"""
    _fake_notes(tmp_path, "random", "Random.SobremSilentroom.0")
    monkeypatch.setattr(play, "ROOT", tmp_path)
    with pytest.raises(SystemExit) as e:
        play.guard_key_collision("Random.SobremSilentroom.1", "random")
    assert "random" in str(e.value)


@needs_tmp
def test_collision_no_notes_file_passes(play, tmp_path, monkeypatch):
    monkeypatch.setattr(play, "ROOT", tmp_path)
    play.guard_key_collision("Whatever.0", "whatever")                # 不抛


@needs_tmp
def test_collision_legacy_notes_without_song_dir_passes(play, tmp_path, monkeypatch):
    """旧格式 notes 没有 song_dir ⇒ 判据未知 ⇒ 放行（不误伤历史计划）。"""
    _fake_notes(tmp_path, "cleyera", None)
    monkeypatch.setattr(play, "ROOT", tmp_path)
    play.guard_key_collision("SomethingElse.0", "cleyera")            # 不抛
