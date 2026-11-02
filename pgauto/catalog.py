"""A 段中间层：解 Unity Addressables 目录表（`assets/aa/catalog.json`）。

Phigros 的谱面不在 `assets/bin/Data` 里，而是走 **Addressables** 分发：
`catalog.json` 是一张「资源键 → 所在 bundle」的表，真正的资源在
`assets/aa/Android/<hash>.bundle`（UnityFS 容器）。

表的结构（键为 Unity 序列化字段名，值是 base64 编码的自定义二进制）::

    m_KeyDataString     键序列：   count(u32) + N × 自描述对象
    m_BucketDataString  桶序列：   每桶一个 key + 该 key 的 entry 下标列表
    m_EntryDataString   条目序列： 每条 28 B，字段全是下标
    m_ExtraDataString   额外数据： 条目里 dataIndex 指向这里
    m_InternalIds       字符串池： 资源内部 ID（bundle 路径就在这里）
    m_ProviderIds       提供者池
    m_resourceTypes     资源类型池

条目 28 字节的字段顺序（全部 i32）::

    internalId | provider | dependencyKey | depHash | dataIndex | primaryKey | resourceType

**谱面的识别**：桶里的键形如 `Assets/Tracks/<曲名>/Chart_<难度>.json`。
这类条目的 `provider` 是 `BundledAssetProvider`、`resourceType` 是
`UnityEngine.TextAsset`，而它的 `dependencyKey` 指向真正要下载的 bundle。

**是否加密**：谱面 bundle 全部走 `AssetBundleProvider`（明文）。厂商另有
`C9SecretAssetBundleProvider`，但只用在 8 个与谱面无关的核心 bundle 上
（键形为纯 `<hash>.bundle`，不带 `<md5>_` 前缀）。实测谱面字节不需解密。
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

__all__ = [
    "read_object",
    "CatalogEntry",
    "AddressablesCatalog",
    "ChartRef",
    "CHART_KEY_RE",
    "ANDROID_PREFIX",
    "ALIAS",
    "derive_key",
]

#: 谱面资源键。难度码在 4.0.1 起出现 `SP` / `Legacy` / `EZ_Error` 等形态，
#: 所以用「非斜杠字符」而不是固定宽度。
CHART_KEY_RE = re.compile(
    r"^Assets/Tracks/(?P<song>[^/]+)/Chart_(?P<diff>[A-Za-z0-9_]+)\.json$"
)

#: bundle 在 APK 内的目录
ANDROID_PREFIX = "assets/aa/Android/"


class _Reader:
    """小端顺序读取器。"""

    def __init__(self, buf: bytes):
        self.buf = buf
        self.pos = 0

    def u8(self) -> int:
        v = self.buf[self.pos]
        self.pos += 1
        return v

    def u16(self) -> int:
        v = struct.unpack_from("<H", self.buf, self.pos)[0]
        self.pos += 2
        return v

    def u32(self) -> int:
        v = struct.unpack_from("<I", self.buf, self.pos)[0]
        self.pos += 4
        return v

    def i32(self) -> int:
        v = struct.unpack_from("<i", self.buf, self.pos)[0]
        self.pos += 4
        return v

    def take(self, n: int) -> bytes:
        v = self.buf[self.pos : self.pos + n]
        self.pos += n
        return v

    def text(self, n: int, enc: str = "utf-8") -> str:
        return self.take(n).decode(enc, "replace")


def read_object(r: _Reader) -> Any:
    """读一个 Addressables 自描述对象（类型字节 + 载荷）。"""
    t = r.u8()
    if t == 0:  # ascii 字符串
        return r.text(r.u32())
    if t == 1:  # utf-16 字符串（长度按**字节**计）
        return r.text(r.u32(), "utf-16")
    if t == 2:
        return r.u16()
    if t == 3:
        return r.u32()
    if t == 4:
        return r.i32()
    if t == 7:  # json 对象（内嵌类型信息）
        asm = r.text(r.u8())
        cls = r.text(r.u8())
        return {
            "assembly_name": asm,
            "class_name": cls,
            "json": json.loads(r.text(r.i32(), "utf-16")),
        }
    raise RuntimeError(f"未知的 catalog 对象类型 {t}")


@dataclass
class CatalogEntry:
    """catalog 里的一条记录。"""

    internal_id: str
    provider: str
    dependency_key: Any
    dep_hash: int
    primary_key: Any
    resource_type: Any
    data: Any
    keys: list = field(default_factory=list)

    @property
    def provider_short(self) -> str:
        return str(self.provider).rsplit(".", 1)[-1]

    @property
    def type_short(self) -> str:
        rt = self.resource_type
        if isinstance(rt, dict):
            return str(rt.get("m_ClassName", "")).rsplit(".", 1)[-1]
        return str(rt)


@dataclass
class ChartRef:
    """一个谱面：曲名 + 难度 + 所在 bundle。"""

    song: str
    difficulty: str
    key: str
    bundle_name: str
    #: APK 内路径（`assets/aa/Android/<name>`）
    apk_path: str

    def __str__(self) -> str:
        return f"{self.song} / {self.difficulty}  ->  {self.bundle_name}"


class AddressablesCatalog:
    """解出的 Addressables 目录表。"""

    def __init__(self, data: dict):
        self.raw = data
        self.keys: list = []
        self.buckets: list[dict] = []
        self.entries: list[CatalogEntry] = []
        self._parse(data)

    # -- 解析 -------------------------------------------------------------- #

    def _parse(self, data: dict) -> None:
        r = _Reader(base64.b64decode(data["m_BucketDataString"]))
        for _ in range(r.u32()):
            offset = r.i32()
            self.buckets.append(
                {"offset": offset, "entries": [r.i32() for _ in range(r.i32())]}
            )

        r = _Reader(base64.b64decode(data["m_KeyDataString"]))
        for _ in range(r.u32()):
            self.keys.append(read_object(r))

        eds = _Reader(base64.b64decode(data["m_EntryDataString"]))
        xds = _Reader(base64.b64decode(data.get("m_ExtraDataString", "") or ""))
        iids = data["m_InternalIds"]
        pids = data["m_ProviderIds"]
        rtps = data["m_resourceTypes"]

        for _ in range(eds.u32()):
            iid, pv, dk, dh, di, pk, rt = (eds.i32() for _ in range(7))
            obj = None
            if di >= 0 and xds.buf:
                xds.pos = di
                obj = read_object(xds)
            self.entries.append(
                CatalogEntry(
                    internal_id=iids[iid],
                    provider=pids[pv],
                    dependency_key=None if dk < 0 else self.keys[dk],
                    dep_hash=dh,
                    primary_key=self.keys[pk],
                    resource_type=rtps[rt],
                    data=obj,
                )
            )

        for b, k in zip(self.buckets, self.keys):
            for idx in b["entries"]:
                if 0 <= idx < len(self.entries):
                    self.entries[idx].keys.append(k)

        self.by_key: dict[Any, list[CatalogEntry]] = {}
        for e in self.entries:
            for k in e.keys:
                self.by_key.setdefault(k, []).append(e)

    @classmethod
    def from_bytes(cls, raw: bytes) -> "AddressablesCatalog":
        return cls(json.loads(raw.decode("utf-8")))

    @classmethod
    def from_file(cls, path: Path | str) -> "AddressablesCatalog":
        with open(path, encoding="utf-8") as f:
            return cls(json.load(f))

    # -- 查询 -------------------------------------------------------------- #

    def provider_histogram(self) -> dict[str, int]:
        h: dict[str, int] = {}
        for e in self.entries:
            h[e.provider_short] = h.get(e.provider_short, 0) + 1
        return dict(sorted(h.items(), key=lambda x: -x[1]))

    def charts(self) -> list[ChartRef]:
        """列出所有谱面，按（曲名, 难度）稳定排序。"""
        found: dict[tuple[str, str], ChartRef] = {}
        for e in self.entries:
            for k in e.keys:
                if not isinstance(k, str):
                    continue
                m = CHART_KEY_RE.match(k)
                if not m:
                    continue
                song, diff = m.group("song"), m.group("diff")
                found.setdefault(
                    (song, diff),
                    ChartRef(
                        song=song,
                        difficulty=diff,
                        key=k,
                        bundle_name=self._bundle_of(e, k),
                        apk_path="",  # 稍后统一填
                    ),
                )
        out = [found[kk] for kk in sorted(found)]
        for ref in out:
            ref.apk_path = ANDROID_PREFIX + ref.bundle_name
        return out

    def _bundle_of(self, chart_entry: CatalogEntry, key: str) -> str:
        """求某个谱面条目所在 bundle 的文件名。

        正路：`dependencyKey` → 该 bundle 条目的 `internalId`
        （形如 `{RuntimePath}/Android/<hash>.bundle`）。这样拿到的是
        Addressables 认定的权威路径，不依赖键名约定。

        退路：`dependencyKey` 形如 `<md5>_<hash>.bundle`，取最后一段。
        """
        dk = chart_entry.dependency_key
        if dk is not None:
            for dep in self.by_key.get(dk, ()):
                n = _basename_of(dep.internal_id)
                if n.endswith(".bundle"):
                    return n
            if isinstance(dk, str) and dk.endswith(".bundle"):
                return dk.rsplit("_", 1)[-1]
        raise RuntimeError(f"{key}: 无法定位所在 bundle（dependencyKey={dk!r}）")


def _basename_of(internal_id: Any) -> str:
    """`{RuntimePath}/Android/<hash>.bundle` → `<hash>.bundle`。"""
    s = str(internal_id)
    return s.rsplit("/", 1)[-1]


def diff_order(d: str) -> int:
    """难度的习惯排序（显示用）。"""
    order = {"EZ": 0, "HD": 1, "IN": 2, "AT": 3, "SP": 4, "Legacy": 5}
    return order.get(d, 9)


# --------------------------------------------------------------------------- #
# 曲目录名 → 短名 key
# --------------------------------------------------------------------------- #
#: 手工别名：目录名首段太长或与别的曲撞车时用它（由作者机器上的历史约定固化）。
ALIAS = {
    "70MinutesFighters.かたぎり.0": "70mf",
    "GungnirFracture.Kryexe.0": "gungnir",
    "000AinSophAur.Yumeji.0": "yume",
    "DerSchneid.Ωμεγα.0": "schneid",
    "Cleyera.Riya.0": "cleyera",
    "CROSSSOUL.HyuNfeatSyepias.0": "cross",
    "DistortedFate.Sakuzyo.0": "distort",
    "AfterZABANIYAMUGEdit.Daily天利.0": "zaban",
    "Ark.kanoryo.0": "ark",
    "Shelter.MeatyRabbitNceS.0": "shelter",
    "SIGMA.Kryexe.0": "sigma",
    "Verruckt.Raimukun.0": "verruckt",
    "星拂云锦featkoi.S9ryne.0": "xfyj",
    "Initialize.iF.0": "init",
}


def derive_key(song: str) -> str:
    """曲目录名 → 短名 key（有别名用别名，否则取句点前首段并只留 ASCII 字母数字）。

    ★ 2026-10-18 修（真事故预防）：旧版末尾是 `return k or "song"` —— 非 ASCII 曲名
    （中文 / 日文 / 假名）被正则删空后退化成**同一个固定字符串 `"song"`**。
    后果不是「名字不好看」，而是：
      `out/touches_song.json` / `out/notes_song.json` / `songs.song` 缓存被**所有中文曲共用**
      ⇒ 打第二首中文歌时**静默复用上一首的谱面**（与「同曲多难度按错谱面」同类事故）。
    现在退化为 `x` + 目录名的 md5 前 8 位：稳定（同名必同 key）、唯一、且 `x` 前缀
    不会与「纯 ASCII 提取」的真实曲名撞车（真曲名不可能以 x 开头且恰好 9 位十六进制）。

    ⚠ 这条规则决定 `out/touches_<key>.json` 的文件名，**两条不变式不能动**：
    纯 ASCII 曲名的 key 逐位不变（否则现存的几百份载荷全部改名）、中文 key 互不相同。
    `tests/test_derive_key.py` 锁着这两条；搬进本模块时是**逐字复制**，没有「顺手优化」。
    """
    if song in ALIAS:
        return ALIAS[song]
    head = song.split(".")[0]
    k = re.sub(r"[^0-9A-Za-z]", "", head).lower()
    if k:
        return k
    return "x" + hashlib.md5(song.encode("utf-8")).hexdigest()[:8]
