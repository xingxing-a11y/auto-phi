"""A 段底层：按需读取 APK 的字节（不下载整个包）。

设计目标
--------
1. **不下载整个 APK。** Phigros 4.0.1 的包是 3.33 GB，而谱面相关字节只有
   几百 MB。`RemoteSource` 只按字节区间读 ZIP 的目录表与目标条目，
   实测取一个谱面约 1 MiB 流量。
2. **不依赖 UnityPy。** 这里只负责「从 ZIP 里取出某个条目的字节」；
   解析 Unity 资源是 `pgauto.catalog` + UnityPy 的事。
3. **数据源可换。** `RemoteSource`（HTTP Range）与 `LocalSource`（本地 APK 文件）
   实现同一个 `ByteSource` 接口，上层逻辑不用改，也便于离线回归。

对照的参考实现（只读、只作事实核对，未复制代码）
------------------------------------------------
- `9RayAlkalin/Resource_Auto` 的 `taptap.py`：证明 TapTap 官方 CDN 的 APK 直链
  可查、且支持 HTTP Range。
- `kvarenzn/phisap` 的 `extract.py`：自写的 Unity 资源读取器（本模块不需要）。
- `9RayAlkalin/Resource_Auto` 的 `getResource.py`：证明 APK 是**明文 ZIP**、
  谱面走 Unity Addressables、谱面字节**不经解密**。

ZIP 结构备忘
------------
- EOCD 位于文件末尾 ≤ `22 + 65535` 字节内，签名 `PK\\x05\\x06`。
- 中央目录条目签名 `PK\\x01\\x02`，给出每个条目的本地头偏移与压缩大小。
- 本地头签名 `PK\\x03\\x04`，其文件名/扩展域长度才能定位真实数据起点
  （所以读条目要分两次：先读 30 字节头，再读数据）。
- 当通用位标记 bit3（数据描述符）置位时，本地头里的 size 字段为 0，
  **必须**以中央目录的值为准 —— 本模块统一用中央目录。
"""

from __future__ import annotations

import hashlib
import json
import random
import struct
import string
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zlib
from dataclasses import dataclass, field
from pathlib import Path

USER_AGENT = {"User-Agent": "okhttp/3.12.1"}

__all__ = [
    "ByteSource",
    "RemoteSource",
    "LocalSource",
    "ZipEntry",
    "ApkZip",
    "TapTapApkInfo",
    "resolve_taptap_apk",
    "PHIGROS_APP_ID",
]

# TapTap 上的 Phigros（鸽子游戏 / PigeonGames）
PHIGROS_APP_ID = 165287


# --------------------------------------------------------------------------- #
# 数据源
# --------------------------------------------------------------------------- #


class ByteSource:
    """可随机读取的字节源。区间语义为**闭区间** `[start, end]`。"""

    size: int = 0
    name: str = ""
    #: 累计发出的读取请求数 / 字节数（用于汇报流量）
    requests: int = 0
    bytes_read: int = 0

    def read(self, start: int, end: int) -> bytes:  # pragma: no cover - 抽象
        raise NotImplementedError

    def stat(self) -> str:
        return f"{self.requests} 次请求 / {self.bytes_read / 2**20:.2f} MiB"

    def close(self) -> None:
        """默认无资源可释放；需要释放的源覆盖它。"""

    def __enter__(self) -> "ByteSource":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class RemoteSource(ByteSource):
    """HTTP Range 只读源，带退避重试。

    实测教训：全量取 1064 个 bundle 时，本机透明代理偶发 `502 Bad Gateway`，
    一次性失败会让 20 个谱面凭空消失。所以**必须**在这里做重试——上层不该
    为「网络抖了一下」写补偿逻辑。
    """

    def __init__(
        self,
        url: str,
        size: int,
        name: str = "",
        timeout: float = 60.0,
        retries: int = 5,
        backoff: float = 0.8,
    ):
        self.url = url
        self.size = size
        self.name = name or url.rsplit("/", 1)[-1]
        self.timeout = timeout
        self.retries = max(1, retries)
        self.backoff = backoff
        self.requests = 0
        self.bytes_read = 0
        self.retried = 0
        self.last_error: Exception | None = None

    def read(self, start: int, end: int) -> bytes:
        if start < 0 or end >= self.size or start > end:
            raise ValueError(f"区间越界: [{start}, {end}] 文件大小 {self.size}")
        req = urllib.request.Request(
            self.url, headers={**USER_AGENT, "Range": f"bytes={start}-{end}"}
        )
        for attempt in range(self.retries):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    data = resp.read()
                if len(data) != end - start + 1:
                    raise OSError(f"长度不符：期望 {end - start + 1}，实得 {len(data)}")
                self.requests += 1
                self.bytes_read += len(data)
                return data
            except urllib.error.HTTPError as e:
                self.last_error = e
                # 4xx（除 429）是永久性错误，重试没意义
                if e.code < 500 and e.code != 429:
                    raise
            except (urllib.error.URLError, OSError, TimeoutError) as e:
                self.last_error = e
            if attempt + 1 < self.retries:
                self.retried += 1
                time.sleep(self.backoff * (2**attempt))
        assert self.last_error is not None
        raise RuntimeError(
            f"读取 [{start}, {end}] 重试 {self.retries} 次仍失败: {self.last_error!r}"
        ) from self.last_error

    def stat(self) -> str:
        s = f"{self.requests} 次请求 / {self.bytes_read / 2**20:.2f} MiB"
        if self.retried:
            s += f"（重试 {self.retried} 次）"
        return s


class LocalSource(ByteSource):
    """本地文件源（用于离线的 APK 文件，或已落盘的缓存）。"""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.size = self.path.stat().st_size
        self.name = self.path.name
        self.requests = 0
        self.bytes_read = 0
        self._fh = None

    def read(self, start: int, end: int) -> bytes:
        if start < 0 or end >= self.size or start > end:
            raise ValueError(f"区间越界: [{start}, {end}] 文件大小 {self.size}")
        if self._fh is None:
            self._fh = open(self.path, "rb")
        self._fh.seek(start)
        data = self._fh.read(end - start + 1)
        self.requests += 1
        self.bytes_read += len(data)
        return data

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None


# --------------------------------------------------------------------------- #
# ZIP 解析
# --------------------------------------------------------------------------- #

_EOCD_SIG = b"PK\x05\x06"
_EOCD64_LOC_SIG = b"PK\x06\x07"
_EOCD64_SIG = b"PK\x06\x06"
_CEN_SIG = b"PK\x01\x02"
_LOC_SIG = b"PK\x03\x04"


@dataclass
class ZipEntry:
    name: str
    method: int
    csize: int
    usize: int
    local_header_offset: int
    flags: int = 0

    @property
    def is_dir(self) -> bool:
        return self.name.endswith("/")

    @property
    def stored(self) -> bool:
        """ZIP 里是否原样存放（Phigros 的 UnityFS bundle 都是 stored）。"""
        return self.method == 0


class ApkZip:
    """只读 ZIP 访问器：解析目录表，按需取单个条目。

    用法::

        with ApkZip(LocalSource("Phigros.apk")) as z:
            data = z.read("assets/aa/catalog.json")

    目录表只读一次并缓存；条目数据每次现取（可选 `cache` 落盘复用）。
    """

    def __init__(self, source: ByteSource, cache: Path | None = None):
        self.source = source
        self.cache = Path(cache) if cache else None
        if self.cache:
            self.cache.mkdir(parents=True, exist_ok=True)
        self._entries: dict[str, ZipEntry] | None = None
        self._cd_offset = 0
        self._cd_size = 0
        self._declared_count = 0

    # -- 生命周期 ---------------------------------------------------------- #

    def __enter__(self) -> "ApkZip":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        if isinstance(self.source, LocalSource):
            self.source.close()

    # -- 目录表 ------------------------------------------------------------ #

    def _find_eocd(self) -> bytes:
        window = min(self.source.size, 22 + 65535)
        tail = self.source.read(self.source.size - window, self.source.size - 1)
        i = tail.rfind(_EOCD_SIG)
        if i < 0:
            raise RuntimeError("不是有效 ZIP：找不到 EOCD 签名")
        end = i + 22
        # 优先看 ZIP64 定位器。★ 结构上它紧挨在 EOCD **之前** 20 字节
        # （[ZIP64 EOCD record][ZIP64 EOCD locator][EOCD]），不是之后。
        # 2026-10-16 修复：原先写的是 `tail.find(sig, i)`（从 EOCD 往后找），
        # 永远命中不了 ⇒ 一旦遇到真 ZIP64 归档就退回用 EOCD 里的 0xFFFFFFFF 哨兵，
        # 解析必失败（实测 `RuntimeError: 目录表条目数不符：解析 0 / 声明 65535`）。
        j = tail.rfind(_EOCD64_LOC_SIG, 0, i)
        if j >= 0 and j == i - 20:
            off64 = struct.unpack_from("<Q", tail, j + 8)[0]
            rec = self.source.read(off64, off64 + 55)
            if rec[:4] == _EOCD64_SIG:
                self._declared_count = struct.unpack_from("<Q", rec, 32)[0]
                self._cd_size = struct.unpack_from("<Q", rec, 40)[0]
                self._cd_offset = struct.unpack_from("<Q", rec, 48)[0]
                return tail[i:end]
        self._declared_count, self._cd_size, self._cd_offset = struct.unpack_from(
            "<HII", tail, i + 10
        )
        return tail[i:end]

    def _load_directory(self) -> dict[str, ZipEntry]:
        if self._entries is not None:
            return self._entries
        self._find_eocd()
        raw = self.source.read(self._cd_offset, self._cd_offset + self._cd_size - 1)
        entries: dict[str, ZipEntry] = {}
        pos = 0
        while pos + 46 <= len(raw) and raw[pos : pos + 4] == _CEN_SIG:
            (
                _vmade, _vneed, flags, method, _mt, _md, _crc, csize, usize,
                nlen, elen, clen, _disk, _iattr, _eattr, lho,
            ) = struct.unpack_from("<HHHHHHIIIHHHHHII", raw, pos + 4)
            name = raw[pos + 46 : pos + 46 + nlen].decode("utf-8", "replace")
            # ZIP64 扩展域：当字段被写成 0xFFFFFFFF 时，真实值在扩展域里
            if csize == 0xFFFFFFFF or usize == 0xFFFFFFFF or lho == 0xFFFFFFFF:
                csize, usize, lho = self._zip64_extra(
                    raw[pos + 46 + nlen : pos + 46 + nlen + elen], csize, usize, lho
                )
            entries[name] = ZipEntry(name, method, csize, usize, lho, flags)
            pos += 46 + nlen + elen + clen
        if len(entries) != self._declared_count:
            raise RuntimeError(
                f"目录表条目数不符：解析 {len(entries)} / 声明 {self._declared_count}"
            )
        self._entries = entries
        return entries

    @staticmethod
    def _zip64_extra(extra: bytes, csize: int, usize: int, lho: int) -> tuple[int, int, int]:
        p = 0
        while p + 4 <= len(extra):
            hid, hsize = struct.unpack_from("<HH", extra, p)
            body = extra[p + 4 : p + 4 + hsize]
            if hid == 0x0001:
                q = 0
                if usize == 0xFFFFFFFF:
                    usize = struct.unpack_from("<Q", body, q)[0]
                    q += 8
                if csize == 0xFFFFFFFF:
                    csize = struct.unpack_from("<Q", body, q)[0]
                    q += 8
                if lho == 0xFFFFFFFF:
                    lho = struct.unpack_from("<Q", body, q)[0]
                break
            p += 4 + hsize
        return csize, usize, lho

    # -- 查询 -------------------------------------------------------------- #

    @property
    def entry_count(self) -> int:
        return len(self._load_directory())

    def names(self) -> list[str]:
        return list(self._load_directory())

    def entry(self, name: str) -> ZipEntry | None:
        return self._load_directory().get(name)

    # -- 读取 -------------------------------------------------------------- #

    def read(self, name: str) -> bytes:
        """取出一个条目的**解压后**字节。"""
        e = self.entry(name)
        if e is None:
            raise KeyError(f"APK 中不存在条目 {name!r}")
        if e.is_dir:
            return b""
        return self.read_entry(e)

    def read_entry(self, e: ZipEntry) -> bytes:
        if self.cache is not None:
            hit = self._cache_path(e)
            if hit.exists():
                return hit.read_bytes()
        head = self.source.read(e.local_header_offset, e.local_header_offset + 29)
        if head[:4] != _LOC_SIG:
            raise RuntimeError(f"{e.name}: 本地头签名异常 {head[:4]!r}")
        nlen, elen = struct.unpack_from("<HH", head, 26)
        start = e.local_header_offset + 30 + nlen + elen
        raw = self.source.read(start, start + e.csize - 1) if e.csize else b""
        if e.method == 0:
            data = raw
        elif e.method == 8:
            data = zlib.decompress(raw, -15)
        else:
            raise RuntimeError(f"{e.name}: 不支持的压缩方式 {e.method}")
        if len(data) != e.usize:
            raise RuntimeError(f"{e.name}: 解出 {len(data)} B，声明 {e.usize} B")
        if self.cache is not None:
            self._cache_path(e).write_bytes(data)
        return data

    def _cache_path(self, e: ZipEntry) -> Path:
        assert self.cache is not None
        safe = e.name.replace("/", "__")
        return self.cache / safe


# --------------------------------------------------------------------------- #
# TapTap 官方 APK 直链
# --------------------------------------------------------------------------- #


@dataclass
class TapTapApkInfo:
    app_id: int
    title: str
    package: str
    version_name: str
    version_code: int
    size: int
    md5: str
    download: str

    def __str__(self) -> str:
        return (
            f"{self.package} v{self.version_name} ({self.version_code}) "
            f"{self.size / 2**30:.3f} GiB md5={self.md5}"
        )


_TOKEN_SECRET = "PeCkE6Fu0B10Vm9BKfPfANwCUAn5POcs"


def resolve_taptap_apk(app_id: int = PHIGROS_APP_ID, timeout: float = 30.0) -> TapTapApkInfo:
    """走 TapTap 官方 API 拿到当前有效的 APK 直链与元信息。

    两跳：
      1. ``/app/v2/detail-by-id/<app_id>`` → ``data.download.apk_id``
      2. ``/apk/v1/detail``（带 MD5 签名）→ ``data.apk``（含直链 / 大小 / md5）

    直链带时效签名，**用前现取**，不要缓存复用。
    """
    uid = uuid.uuid4()
    xua = f"V=1&PN=TapTap&VN_CODE=281001004&LOC=CN&LANG=zh_CN&CH=default&UID={uid}"

    req = urllib.request.Request(
        "https://api.taptapdada.com/app/v2/detail-by-id/%d?X-UA=%s"
        % (app_id, urllib.parse.quote(xua)),
        headers=USER_AGENT,
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        detail = json.load(resp)
    if "data" not in detail:
        raise RuntimeError(f"TapTap 未返回应用详情: {json.dumps(detail)[:200]}")
    apk_id = detail["data"]["download"]["apk_id"]

    nonce = "".join(random.sample(string.ascii_lowercase + string.digits, 5))
    ts = int(time.time())
    raw = (
        f"X-UA={xua}&end_point=d1&id={apk_id}&node={uid}&nonce={nonce}"
        f"&time={ts}{_TOKEN_SECRET}"
    )
    body = (
        f"sign={hashlib.md5(raw.encode()).hexdigest()}&node={uid}&time={ts}"
        f"&id={apk_id}&nonce={nonce}&end_point=d1"
    )
    req = urllib.request.Request(
        "https://api.taptapdada.com/apk/v1/detail?X-UA=" + urllib.parse.quote(xua),
        body.encode(),
        USER_AGENT,
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        apk = json.load(resp)["data"]["apk"]

    name = apk.get("name", "")
    return TapTapApkInfo(
        app_id=app_id,
        title=detail["data"].get("title", ""),
        package=name.split("-")[0] if name else "",
        version_name=apk.get("version_name", ""),
        version_code=int(apk.get("version_code", 0)),
        size=int(apk.get("size", 0)),
        md5=apk.get("md5", ""),
        download=apk["download"],
    )


def open_apk(
    apk: str | Path | None = None,
    *,
    app_id: int = PHIGROS_APP_ID,
    cache: Path | None = None,
    verbose: bool = True,
) -> tuple[ApkZip, str]:
    """打开一个 APK。`apk` 为 None 时走 TapTap 官方直链远程读取。

    返回 `(ApkZip, 描述串)`。
    """
    if apk is not None:
        p = Path(apk)
        if not p.exists():
            raise FileNotFoundError(f"APK 不存在: {p}")
        src: ByteSource = LocalSource(p)
        desc = f"本地 {p.name} ({src.size / 2**30:.3f} GiB)"
    else:
        info = resolve_taptap_apk(app_id)
        src = RemoteSource(info.download, info.size, name=info.package)
        desc = f"远程 {info}"
        if verbose:
            print(f"[apk] {info}")
    return ApkZip(src, cache=cache), desc
