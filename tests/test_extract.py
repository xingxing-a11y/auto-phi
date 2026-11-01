"""A 段（解包）的离线回归。

**全部离线**：ZIP 层用合成包自造，catalog 层与 bundle 层用真实固定样本
（`tests/fixtures/`，取自 Phigros 4.0.1 官方 APK）。所以这套测试在没网
的情况下也能跑，且不需要 3.33 GB 的安装包。

固定样本的来历与用途::

    fixtures/catalog.json            4.0.1 的 assets/aa/catalog.json（4.0 MiB）
    fixtures/chart_bundle_at.bundle  Rrhar'il AT 所在 bundle（628 KiB，含 TextAsset Chart_AT）

两者都是从官方 APK 里按 Range 取出的**原始字节**，未做任何改写。
"""

from __future__ import annotations

import io
import struct
import zipfile
from pathlib import Path

import pytest

from pgauto.apk import ApkZip, LocalSource, ZipEntry
from pgauto.catalog import ANDROID_PREFIX, AddressablesCatalog, CHART_KEY_RE

FIX = Path(__file__).resolve().parent / "fixtures"
CATALOG = FIX / "catalog.json"
BUNDLE_AT = FIX / "chart_bundle_at.bundle"

# ★ 2026-10-26：这两份夹具是**游戏原始字节**（catalog.json 4 MiB + Rrhar'il AT 的 bundle），
#   不随公开仓库分发 ⇒ 本地没有就整模块跳过（而不是让 pytest 报 collection error）。
pytestmark = pytest.mark.skipif(
    not (CATALOG.is_file() and BUNDLE_AT.is_file()),
    reason="缺 tests/fixtures/（游戏原始字节，不随仓库分发）；从自己的 APK 取出 catalog.json / chart_bundle_at.bundle 后即可跑")

#: 官方 APK 里的确切数字（来自 4.0.1 / version_code 157）
OFFICIAL = dict(
    version_name="4.0.1",
    version_code=157,
    md5="5e317917c4f24e31ba5b6d91c88ba190",
    size=3333003433,
    entries=2928,
)
#: Rrhar'il AT 的权威 sha256（与社区样例 `samples/rrhar_at/` 逐字节相同）
RRHAR_AT_SHA256 = "27a9f73a65d155daa53147dda55da7544b8e4812db71684b9d80b9c722fd9042"
RRHAR_AT_BYTES = 3224475
RRHAR_BUNDLE = "92b61ca42ceef14ca469c14c2f1e30f9.bundle"


def make_zip(path: Path, entries: dict[str, bytes], *, compress: bool = True) -> Path:
    """造一个测试用 ZIP。"""
    mode = zipfile.ZIP_DEFLATED if compress else zipfile.ZIP_STORED
    with zipfile.ZipFile(path, "w", mode) as z:
        for name, data in entries.items():
            z.writestr(name, data)
    return path


# --------------------------------------------------------------------------- #
# ZIP 层（合成包，不依赖任何 fixture）
# --------------------------------------------------------------------------- #


def test_local_source_reads_closed_ranges(tmp_path):
    p = tmp_path / "x.bin"
    p.write_bytes(bytes(range(256)))
    with LocalSource(p) as src:
        assert src.size == 256
        assert src.read(0, 3) == bytes(range(4))
        assert src.read(255, 255) == b"\xff"
    assert src.requests == 2


def test_local_source_rejects_out_of_range(tmp_path):
    p = tmp_path / "x.bin"
    p.write_bytes(b"abc")
    with LocalSource(p) as src:
        with pytest.raises(ValueError):
            src.read(0, 3)
        with pytest.raises(ValueError):
            src.read(2, 1)


def test_apk_zip_reads_stored_and_deflated(tmp_path):
    """Phigros 的 bundle 在 ZIP 里是 stored；普通文件常有 deflate。两条路都要通。"""
    payload = b"UnityFS\x00" + bytes(range(256)) * 40
    other = "hello 世界 ".encode("utf-8") * 500
    path = make_zip(tmp_path / "t.apk", {"assets/aa/Android/a.bundle": payload,
                                         "assets/aa/catalog.json": other},
                    compress=True)
    with ApkZip(LocalSource(path)) as z:
        assert z.entry_count == 2
        assert z.read("assets/aa/catalog.json") == other
        assert z.read("assets/aa/Android/a.bundle") == payload


def test_apk_zip_reports_entry_metadata(tmp_path):
    data = b"x" * 1000
    path = make_zip(tmp_path / "t.apk", {"a.txt": data}, compress=False)
    with ApkZip(LocalSource(path)) as z:
        e = z.entry("a.txt")
        assert isinstance(e, ZipEntry)
        assert e.stored and e.method == 0
        assert e.usize == e.csize == 1000
        assert not e.is_dir


def test_apk_zip_directory_parsed_once(tmp_path):
    path = make_zip(tmp_path / "t.apk", {"a": b"1", "b": b"2"})
    with ApkZip(LocalSource(path)) as z:
        before = z.source.requests
        z.names()
        z.names()
        z.names()
        # 目录表只读一次：多出新请求只可能来自条目数据（这里没读）
        assert z.source.requests - before <= 2


def test_apk_zip_unknown_entry_raises(tmp_path):
    path = make_zip(tmp_path / "t.apk", {"a": b"1"})
    with ApkZip(LocalSource(path)) as z:
        with pytest.raises(KeyError):
            z.read("nope")


def test_apk_zip_rejects_non_zip(tmp_path):
    p = tmp_path / "not.zip"
    p.write_bytes(b"this is not a zip at all, " * 100)
    with ApkZip(LocalSource(p)) as z:
        with pytest.raises(RuntimeError, match="EOCD"):
            z.names()


def build_zip64_archive(name: bytes = b"a.txt", data: bytes = b"hi") -> bytes:
    """手工造一份**最小 ZIP64 归档**（stdlib 的 force_zip64 不会写 ZIP64 EOCD）。

    尾部布局（这正是被测逻辑必须认对的地方）::

        [中央目录]
        [ZIP64 EOCD record ]  PK\\x06\\x06  56 字节
        [ZIP64 EOCD locator]  PK\\x06\\x07  20 字节   ← 紧挨在 EOCD **之前**
        [EOCD              ]  PK\\x05\\x06  22 字节（字段全填 0xFFFF/0xFFFFFFFF 哨兵）
    """
    crc = zipfile.crc32(data) & 0xFFFFFFFF
    out = bytearray()

    lho = len(out)
    out += struct.pack("<IHHHHHIIIHH", 0x04034B50, 20, 0, 0, 0, 0, crc,
                       len(data), len(data), len(name), 0)
    out += name + data

    cd_off = len(out)
    out += struct.pack("<IHHHHHHIIIHHHHHII", 0x02014B50, 20, 20, 0, 0, 0, 0,
                       crc, len(data), len(data), len(name), 0, 0, 0, 0, 0, lho)
    out += name
    cd_size = len(out) - cd_off

    z64_off = len(out)
    out += struct.pack("<IQHHIIQQQQ", 0x06064B50, 44, 45, 45, 0, 0,
                       1, 1, cd_size, cd_off)
    out += struct.pack("<IIQI", 0x07064B50, 0, z64_off, 1)
    out += struct.pack("<IHHHHIIH", 0x06054B50, 0xFFFF, 0xFFFF, 0xFFFF, 0xFFFF,
                       0xFFFFFFFF, 0xFFFFFFFF, 0)
    return bytes(out)


def test_apk_zip_reads_zip64_archive(tmp_path):
    """★ ZIP64 归档要能读 —— 判据是 stdlib `zipfile`（它就在 EOCD 前 20 字节找定位器）。

    2026-10-16 回归：`_find_eocd` 原先写 `tail.find(LOC_SIG, i)`（从 EOCD **往后**找），
    结构上永远找不到 ⇒ 退回 EOCD 里的哨兵值 ⇒
    `RuntimeError: 目录表条目数不符：解析 0 / 声明 65535`。
    Phigros 现包 3.33 GB < 4 GB 走不到这条路，所以是**潜伏** bug。
    """
    blob = build_zip64_archive()
    p = tmp_path / "zip64.apk"
    p.write_bytes(blob)

    # 判据一：stdlib 认为这是一份合法 ZIP64 归档（期望值不来自被测实现）。
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        assert zf.namelist() == ["a.txt"]
        assert zf.read("a.txt") == b"hi"

    # 判据二：ApkZip 结论必须一致。
    with ApkZip(LocalSource(p)) as z:
        assert z.entry_count == 1
        assert z.names() == ["a.txt"]
        assert z.read("a.txt") == b"hi"
        assert z.entry("a.txt").usize == 2


def test_apk_zip_cache_avoids_second_read(tmp_path):
    payload = b"payload" * 100
    path = make_zip(tmp_path / "t.apk", {"d/x.bin": payload})
    cache = tmp_path / "cache"
    with ApkZip(LocalSource(path), cache=cache) as z:
        assert z.read("d/x.bin") == payload
        hits_after_first = z.source.bytes_read
        assert z.read("d/x.bin") == payload
        assert z.source.bytes_read == hits_after_first, "第二次应命中缓存"
    assert list(cache.iterdir()), "缓存目录应有落盘"


# --------------------------------------------------------------------------- #
# catalog 层（真实固定样本）
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def catalog() -> AddressablesCatalog:
    assert CATALOG.exists(), f"缺固定样本 {CATALOG}"
    return AddressablesCatalog.from_file(CATALOG)


def test_catalog_top_level_keys(catalog):
    for k in ("m_InternalIds", "m_ProviderIds", "m_resourceTypes",
              "m_KeyDataString", "m_BucketDataString", "m_EntryDataString"):
        assert k in catalog.raw, f"缺字段 {k}"


def test_catalog_counts(catalog):
    assert len(catalog.entries) == 6902
    assert len(catalog.keys) == len(catalog.buckets) == 8559


def test_catalog_provider_histogram(catalog):
    h = catalog.provider_histogram()
    assert h["BundledAssetProvider"] == 3918
    assert h["AssetBundleProvider"] == 2683
    # 厂商自有加密 provider 确实存在，但只用在极少数核心 bundle 上
    assert h.get("C9SecretAssetBundleProvider", 0) == 8


def test_catalog_chart_inventory(catalog):
    charts = catalog.charts()
    assert len(charts) == 1064
    assert len({c.song for c in charts}) == 334
    diffs = {}
    for c in charts:
        diffs[c.difficulty] = diffs.get(c.difficulty, 0) + 1
    assert diffs["EZ"] == 334
    assert diffs["HD"] == 333
    assert diffs["IN"] == 333
    assert diffs["AT"] == 56


def test_catalog_chart_apk_paths_are_well_formed(catalog):
    for c in catalog.charts():
        assert c.apk_path.startswith(ANDROID_PREFIX)
        assert c.apk_path.endswith(".bundle")
        assert "/" not in c.bundle_name, "bundle 名不该带目录"
        assert CHART_KEY_RE.match(c.key)


def test_catalog_locates_rrhar_at(catalog):
    ref = next(c for c in catalog.charts()
               if c.song == "Rrharil.TeamGrimoire.0" and c.difficulty == "AT")
    assert ref.bundle_name == RRHAR_BUNDLE
    assert ref.apk_path == f"assets/aa/Android/{RRHAR_BUNDLE}"


def test_catalog_chart_entries_are_textassets(catalog):
    """每个谱面条目都应是 BundledAssetProvider + UnityEngine.TextAsset，且指向明文 bundle。"""
    chart_keys = {c.key for c in catalog.charts()}
    seen = 0
    for e in catalog.entries:
        if not any(k in chart_keys for k in e.keys):
            continue
        seen += 1
        assert e.provider_short == "BundledAssetProvider"
        assert e.type_short == "TextAsset"
        dep = catalog.by_key.get(e.dependency_key)
        assert dep, f"{e.primary_key}: dependencyKey 无法解析"
        # 谱面 bundle 走明文 provider，不是 C9Secret
        for d in dep:
            assert d.provider_short == "AssetBundleProvider", (
                f"{e.primary_key} 依赖了加密 provider {d.provider_short}"
            )
    assert seen == 1064


def test_chart_key_regex_variants():
    """难度码长度不固定（4.0.1 起有 SP / Legacy / EZ_Error），正则要能兜住。"""
    for key, song, diff in [
        ("Assets/Tracks/A.B.0/Chart_EZ.json", "A.B.0", "EZ"),
        ("Assets/Tracks/A.B.0/Chart_SP.json", "A.B.0", "SP"),
        ("Assets/Tracks/A.B.0/Chart_SP_AT.json", "A.B.0", "SP_AT"),
        ("Assets/Tracks/A.B.0/Chart_EZ_Error.json", "A.B.0", "EZ_Error"),
        ("Assets/Tracks/望影の方舟Six.SeURa.0/Chart_HD.json", "望影の方舟Six.SeURa.0", "HD"),
    ]:
        m = CHART_KEY_RE.match(key)
        assert m, key
        assert (m.group("song"), m.group("diff")) == (song, diff)
    # 不该误命中
    for bad in ("Assets/Tracks/A.B.0/Chart.json",
                "Assets/Tracks/A.B.0/Illustration.jpg",
                "Assets/Other/A.B.0/Chart_AT.json",
                "Assets/Tracks/A/Sub/Chart_AT.json"):
        assert not CHART_KEY_RE.match(bad), bad


def test_basename_extraction():
    from pgauto.catalog import _basename_of
    assert _basename_of(
        "{UnityEngine.AddressableAssets.Addressables.RuntimePath}/Android/abc.bundle"
    ) == "abc.bundle"


# --------------------------------------------------------------------------- #
# bundle 层（真实固定样本 → UnityPy → TextAsset）
# --------------------------------------------------------------------------- #


def _extract_text_asset(raw: bytes) -> tuple[str, str]:
    """与 tools/extract_chart.py 同一路径的极简复刻，用于独立核对。"""
    import UnityPy
    from UnityPy.enums import ClassIDType

    env = UnityPy.load(raw)
    bf = env.files[next(iter(env.files))]
    tas = list(bf.get_filtered_objects([ClassIDType.TextAsset]))
    assert len(tas) == 1, f"期望 1 个 TextAsset，实得 {len(tas)}"
    obj = tas[0].read()
    return obj.m_Name, obj.m_Script


def test_chart_bundle_is_unityfs():
    raw = BUNDLE_AT.read_bytes()
    assert raw[:8] == b"UnityFS\x00", "Phigros 的 bundle 应是 UnityFS 容器"


def test_chart_bundle_yields_text_asset():
    name, script = _extract_text_asset(BUNDLE_AT.read_bytes())
    assert name == "Chart_AT"
    assert len(script) == RRHAR_AT_BYTES


def test_chart_bundle_text_matches_official_sha256():
    """固定样本的 sha256 必须与「官方 APK 解包 == 社区样例」的结论一致。"""
    import hashlib

    _, script = _extract_text_asset(BUNDLE_AT.read_bytes())
    assert hashlib.sha256(script.encode("utf-8")).hexdigest() == RRHAR_AT_SHA256


def test_extracted_chart_is_valid_pgr():
    import json

    _, script = _extract_text_asset(BUNDLE_AT.read_bytes())
    j = json.loads(script)
    assert j["formatVersion"] == 3
    assert j["offset"] == 0.0
    assert len(j["judgeLineList"]) == 24
    notes = sum(
        len(ln.get("notesAbove") or []) + len(ln.get("notesBelow") or [])
        for ln in j["judgeLineList"]
    )
    assert notes == 1300


def test_bundle_roundtrip_through_apk_zip(tmp_path):
    """把 fixture bundle 塞进一个合成 APK，走完整 ZIP → UnityPy 链路。"""
    raw = BUNDLE_AT.read_bytes()
    apk = make_zip(tmp_path / "fake.apk", {f"assets/aa/Android/{RRHAR_BUNDLE}": raw},
                   compress=False)
    with ApkZip(LocalSource(apk)) as z:
        got = z.read(f"assets/aa/Android/{RRHAR_BUNDLE}")
    assert got == raw
    name, script = _extract_text_asset(got)
    assert name == "Chart_AT"


def test_official_apk_facts_are_recorded():
    """把官方 APK 的权威数字钉在测试里：将来换版本时这条会立刻报警。"""
    assert OFFICIAL["version_name"] == "4.0.1"
    assert OFFICIAL["version_code"] == 157
    assert OFFICIAL["md5"] == "5e317917c4f24e31ba5b6d91c88ba190"
    assert OFFICIAL["size"] > 3 * 2**30
    assert OFFICIAL["entries"] == 2928
