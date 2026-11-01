#!/usr/bin/env bash
# 下载构建所需的 Shizuku 依赖（4 个 aar）到 android/vendor/。
#
# 为什么入库：这些是构建的硬依赖，仓库里没有它们就编不出来。4 个合计约 60 KB，
# 比让每个新会话重新摸索 artifact 名字便宜得多（api / aidl / shared / provider 四个，
# 少一个都是「找不到符号」，而官网文档只提 api 和 provider）。
#
# 版本锁 13.1.5：手机上装的是 13.6.0，Shizuku 的 client 库向后兼容，
# 13.1.5 是与 13.x 服务端配套的最后一个稳定 API 版本。
#
# 用法：bash android/injector/fetch_deps.sh
set -euo pipefail

V="${SHIZUKU_API_VERSION:-13.1.5}"
BASE="https://repo1.maven.org/maven2/dev/rikka/shizuku"
HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="$(cd "$HERE/../.." && pwd)/android/vendor"
mkdir -p "$OUT"

for a in api aidl shared provider; do
  f="$OUT/shizuku-$a-$V.aar"
  if [ -f "$f" ]; then
    echo "  已存在 $f"
    continue
  fi
  code=$(curl -sS -m 60 -o "$f" -w '%{http_code}' "$BASE/$a/$V/$a-$V.aar")
  if [ "$code" != "200" ]; then
    rm -f "$f"
    echo "✗ $a 下载失败 (http=$code)" >&2
    exit 1
  fi
  echo "  ✓ $a  $(stat -c%s "$f") B"
done

echo
echo "校验（每个 aar 必须含 classes.jar）："
"$(cd "$HERE/../.." && pwd)/.venv/Scripts/python.exe" - "$OUT" "$V" <<'PY'
import sys, zipfile, pathlib
out, ver = pathlib.Path(sys.argv[1]), sys.argv[2]
bad = 0
for a in ('api', 'aidl', 'shared', 'provider'):
    p = out / f'shizuku-{a}-{ver}.aar'
    try:
        with zipfile.ZipFile(p) as z:
            n = len([x for x in z.namelist() if x.endswith('.class')]) or None
            inner = zipfile.ZipFile(__import__('io').BytesIO(z.read('classes.jar')))
            n = len([x for x in inner.namelist() if x.endswith('.class')])
        print(f'  {p.name}: {n} 个 class')
    except Exception as e:
        print(f'  ✗ {p.name}: {e}')
        bad += 1
sys.exit(1 if bad else 0)
PY
