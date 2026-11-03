#!/usr/bin/env bash
# 端内自动演奏 APK（io.pgauto.play）的最小构建链 —— **三树同编，源码不复制**。
#
# 与 android/probe|injector/build.sh 同构，多出两件事：
#   ① `javac` 同时编 probe + injector + play 三棵树（所以 play 能直接 new
#      io.pgauto.probe.OnsetDetector / io.pgauto.inject.DevRun，而不必拷一份代码；
#      `Scheduler` 只有一份 ⇒ 与 payload_ref.py 的逐位对拍不会失效）
#   ② `aapt2 link` 带 `-A app/src/main/assets`，把 pack_assets.py 的产物打进 APK
#   ③ 多一步 `aapt2 compile --dir app/src/main/res`（2026-10-26 接入图标）：
#      aapt2 link **不吃资源目录**，要先把 res 编成 res.zip 再作为位置参数交给 link。
#      少了这一步，manifest 的 @mipmap/ic_launcher 解析不到 ⇒ link 直接失败（不会静默）。
#
# 为什么不用 Gradle：见 android/probe/build.sh 顶部注释。
#
# 用法：
#   bash android/play/build.sh            # 产出 android/play/build/pgplay.apk
#   bash android/play/build.sh install    # 构建 + adb install -r
#   bash android/play/build.sh assets     # 先重新生成 assets 再构建
#
# 依赖：JDK 17、Android SDK（platforms/android-34 + build-tools/34.0.0）、
#       android/vendor/ 下 4 个 Shizuku aar、.venv（pack_assets 用）
set -euo pipefail

SDK="${ANDROID_SDK:-D:/android-sdk}"
BT="$SDK/build-tools/34.0.0"
PLAT="$SDK/platforms/android-34/android.jar"
JDK="${JDK_HOME:-D:/OpenJDK17_17.0.19_10}"

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
VENDOR="$ROOT/android/vendor"
SRC_PROBE="$ROOT/android/probe/app/src/main/java"
SRC_INJECT="$ROOT/android/injector/app/src/main/java"
SRC_PLAY="$HERE/app/src/main/java"
ASSETS="$HERE/app/src/main/assets"
RES="$HERE/app/src/main/res"
cd "$HERE"

AARS=(shizuku-api-13.1.5.aar shizuku-aidl-13.1.5.aar \
      shizuku-shared-13.1.5.aar shizuku-provider-13.1.5.aar)

for f in "$BT/aapt2.exe" "$BT/d8.bat" "$BT/zipalign.exe" "$BT/apksigner.bat" \
         "$PLAT" "$JDK/bin/javac.exe" "$JDK/bin/keytool.exe"; do
  [ -e "$f" ] || { echo "✗ 缺少 $f" >&2; exit 1; }
done
for a in "${AARS[@]}"; do
  [ -f "$VENDOR/$a" ] || { echo "✗ 缺少 $VENDOR/$a（先跑 android/injector/fetch_deps.sh）" >&2; exit 1; }
done
for d in "$SRC_PROBE" "$SRC_INJECT" "$SRC_PLAY"; do
  [ -d "$d" ] || { echo "✗ 缺少源码树 $d" >&2; exit 1; }
done

# 构建产物：只有一个形态（诊断开关已随 2026-11-01 的重构删除）
APK="build/pgplay.apk"

if [ "${1:-}" = "assets" ]; then
  echo "[0/8] 重新生成 assets"
  PY="$ROOT/.venv/Scripts/python.exe"
  [ -x "$PY" ] || PY="$(command -v python)"
  # ★ 必须检查退出码：pack_assets 在「有曲目失败」时返回 4（产物不完整）。
  #   这里原来不看退出码 ⇒ 会拿缺了几首的 manifest 继续出包，而且全程没有任何提示。
  ( cd "$ROOT" && "$PY" tools/pack_assets.py ) || {
    echo "✗ assets 打包失败（见上面的 ✗ 行）⇒ 中止构建，不要用不完整的 manifest 出包" >&2
    exit 1
  }
  shift || true
fi

[ -f "$ASSETS/manifest.json" ] || {
  echo "✗ 缺 $ASSETS/manifest.json（先跑：.venv/Scripts/python.exe tools/pack_assets.py）" >&2
  exit 1
}

# ── 图标资源（2026-10-26 接入）────────────────────────────────────────────
# manifest 已声明 android:icon="@mipmap/ic_launcher"。缺任一文件时 aapt2 link 会报
# `resource mipmap/ic_launcher not found`，但那行错误不提"图标"两个字，容易误判成
# 资源目录整体没生效 ⇒ 这里先用文件存在性给出人话提示。
# ⛔ 源稿在 android/play/icon/（含安全圆/尺寸/遮罩预览），res/ 是它的构建拷贝；
#    改图要先改 icon/ 再重新导，别只改 res/（否则图标无法复现）。
for f in "$RES/mipmap-anydpi-v26/ic_launcher.xml" \
         "$RES/mipmap-xxxhdpi/ic_launcher_foreground.png" \
         "$RES/mipmap-xxxhdpi/ic_launcher_background.png" \
         "$RES/mipmap-mdpi/ic_launcher.png"; do
  [ -f "$f" ] || { echo "✗ 缺图标资源 $f（源稿见 android/play/icon/）" >&2; exit 1; }
done

echo "[1/8] 从 aar 抽 classes.jar"
# ★ 不用 `rm -rf build`：那会一次性删 68 个文件、触发工具的「批量删除确认」，构建直接断在半路
#   （10-24 复现两次）。移到 tmp/ 下 —— 现场可事后核对，清理统一走 tmp/ 的批次确认。
if [ -d build ]; then
  mkdir -p "$ROOT/tmp"
  mv build "$ROOT/tmp/_build_$(date +%Y%m%d_%H%M%S)"
fi
mkdir -p build/classes build/apk build/libs
# ★ 所有 python 子步骤统一 UTF-8 输出（2026-10-25）：`check_play_apk.py` / `pack_assets.py`
#   会打印 ✓/⇒ 这类字符，在中文 Windows 的管道里按 gbk 编码直接抛 UnicodeEncodeError
#   ⇒ **构建最后一道闸门自己崩掉**，看起来像「APK 不合格」，其实只是编码。
#   ⛔ 别只在一处 export：这里作用于本脚本起的全部 python 子进程。
export PYTHONUTF8=1
PY="$ROOT/.venv/Scripts/python.exe"
[ -x "$PY" ] || PY="$(command -v python)"
"$PY" - "$VENDOR" <<'PY'
import sys, zipfile, pathlib
vendor = pathlib.Path(sys.argv[1])
for aar in sorted(vendor.glob('shizuku-*-13.1.5.aar')):
    with zipfile.ZipFile(aar) as z:
        data = z.read('classes.jar')
    out = pathlib.Path('build/libs') / (aar.stem.replace('.', '_') + '.jar')
    out.write_bytes(data)
    print(f'  {aar.name} → {out.name} ({len(data)} B)')
PY

echo "[2/8] 构建指纹（G13）+ aapt2 compile/link（res + manifest + assets → base.apk）"
# ★ 为什么版本号必须带 git rev（2026-10-25 19:0x 的回退事故）：
#   为了排除变量连着出了几个安装包，`versionName` 却是写死的 `0.1-m1s1d` ⇒ 装到手机上之后
#   `dumpsys`、界面、trace 全都认不出这是哪一版；`lastUpdateTime` 只记"安装时刻"。
#   现在：versionName = `0.1-m1s1d+g<rev>[+dirty]`，并把同一份信息写进 assets/build_info.txt
#   （界面横幅与 trace 头都读它）。
# ⚠ `dirty` 只看**已跟踪改动**（--untracked-files=no）：否则 tmp/_* 与 out/*.png 会让每个包都带
#   dirty，这个标记就失去意义了。
REV="$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo nogit)"
DIRTY=0
if [ -n "$(git -C "$ROOT" status --porcelain --untracked-files=no 2>/dev/null)" ]; then
  DIRTY=1
fi
BASE_VERSION="0.1-m1s1d"
VERSION="$BASE_VERSION+g$REV"
if [ "$DIRTY" = "1" ]; then VERSION="$VERSION+dirty"; fi
BUILT="$(date +%Y-%m-%dT%H:%M:%S%z)"
# assets/build_info.txt 是**构建产物**，已进 .gitignore（别提交它，否则每次构建都把工作树弄脏）
{
  echo "# 由 android/play/build.sh 生成，不入库"
  echo "version=$BASE_VERSION"
  echo "rev=$REV"
  echo "dirty=$DIRTY"
  echo "version_full=$VERSION"
  echo "built=$BUILT"
} > "$ASSETS/build_info.txt"
echo "  versionName=$VERSION（rev=$REV dirty=$DIRTY built=$BUILT）"
# ★ -A 是 assets 根目录：少了它 APK 里就没有 manifest.json，装完进界面直接
#   「曲库读取失败」，而 aapt2 一个字都不会报。
# ★ 图标（2026-10-26）：aapt2 link **不接受资源目录** —— `-R` 是 overlay 语义
#   （同名资源后者覆盖前者），不是"把这个目录编进去"。正确顺序是先
#   `aapt2 compile --dir res -o res.zip`，再把 res.zip 当**位置参数**交给 link。
#   ⛔ 别把 res.zip 写成 `-R build/res.zip`：那样能过，但语义是 overlay，
#      以后加任何资源都会静默走错覆盖关系。
# ★ `--no-crunch` 是**必须的**（2026-10-26 实测，不是保守起见）：
#   aapt2 默认会对 res 里的 PNG 做 crunch，实测两种后果并存 ——
#     ① 多数图是**无损重压缩**（字节变了、像素一字不差），例如 hdpi 的 ic_launcher
#        磁盘 5839 B → 包内 5063 B；
#     ② 但 `ic_launcher_foreground`(xxxhdpi) 那一张是**有损量化**：逐像素比对
#        最大通道差 112、15.5 万像素被改（其余 14 张像素差为 0）。
#        图标里 φ 的模糊光晕正是被啃的对象。
#   复核方式：`aapt2 dump` 不出结论，得解出 APK 内的 PNG 与磁盘 `res/` 逐像素比
#   （本轮就是这么发现 ② 的）。所以结论是「不能靠 crunch 省空间」。
#   代价：图标总大小从 ~250 KB 回到原样，占 15 MB 的包 1.6%，完全不值得省。
#   ★ 关了它之后 APK 内 res/ 与磁盘**逐字节一致**，check_play_apk.py 第 ⑨ 组
#     就是靠这点断言 —— 注意字节断言比像素断言更早撞上 ①（无损重压缩也会红），
#     这正是我们要的：任何改写都必须显式，不许静默。
"$BT/aapt2.exe" compile --no-crunch --dir "$RES" -o build/res.zip
# versionCode 2 → 3（2026-10-26）：换图标必须让包管理器认为这是一次真实升级，
# 否则部分桌面会沿用旧的图标缓存（`adb install -r` 能装上，图标却还是绿的机器人）。
"$BT/aapt2.exe" link \
  -o build/base.apk \
  -I "$PLAT" \
  --manifest app/src/main/AndroidManifest.xml \
  -A "$ASSETS" \
  --min-sdk-version 29 --target-sdk-version 34 \
  --version-code 3 --version-name "$VERSION" \
  build/res.zip

echo "[3/8] javac（三树同编：probe + injector + play）"
CP="$PLAT"
for j in build/libs/*.jar; do CP="$CP;$(cygpath -w "$j" 2>/dev/null || echo "$j")"; done
SRCS=$(find "$SRC_PROBE" "$SRC_INJECT" "$SRC_PLAY" -name '*.java')
N=$(echo "$SRCS" | wc -l)
echo "  共 $N 个源文件"
NX=
# javac 在中文 Windows 上用 GBK 输出提示，而这里的终端是 UTF-8 ⇒ 全是乱码。
# 强制英文提示（比 `-Xlint:none` 更彻底：`-nowarn` 关不掉「注: 使用了过时 API」这类 note）。
NX="-J-Duser.language=en -J-Duser.country=US"
"$JDK/bin/javac.exe" -source 17 -target 17 -nowarn $NX -Xlint:none -encoding UTF-8 \
  -classpath "$CP" -d build/classes $SRCS

echo "[4/8] d8（本工程 class + 4 个 Shizuku jar → classes.dex）"
"$BT/d8.bat" --release --min-api 29 --lib "$(cygpath -w "$PLAT" 2>/dev/null || echo "$PLAT")" \
  --output build/apk \
  $(find build/classes -name '*.class') build/libs/*.jar

echo "[5/8] 把 dex 打进 apk"
"$PY" - <<'PY'
import shutil, zipfile
shutil.copy('build/base.apk', 'build/unsigned.apk')
with zipfile.ZipFile('build/unsigned.apk', 'a', zipfile.ZIP_DEFLATED) as z:
    z.write('build/apk/classes.dex', 'classes.dex')
PY

echo "[6/8] zipalign"
# ⚠ 这里会打 4 行 `zip W ... WARNING: header mismatch`：aapt2 写的条目带对齐用的
#   extra field，第 5 步用 Python 追加 classes.dex 时重写了中央目录，两者长度对不上。
#   zipalign 会按 -p 4 重写全部条目，**产物是对的**（由 check 的第 5 组用
#   `zipalign -c` 复核）。所以这里收掉噪音、只在真失败时报错 —— 不隐藏失败，
#   只是不让"它正在修"看起来像"它失败了"。
if ! ZAL_OUT="$("$BT/zipalign.exe" -f -p 4 build/unsigned.apk build/aligned.apk 2>&1)"; then
  echo "$ZAL_OUT" >&2
  echo "✗ zipalign 失败" >&2
  exit 1
fi
if echo "$ZAL_OUT" | grep -q "header mismatch"; then
  echo "  （header mismatch ×$(echo "$ZAL_OUT" | grep -c "header mismatch")："
  echo "    aapt2 extra field 与追加 dex 后的中央目录不一致，本步已按 -p 4 重写修正）"
fi

echo "[7/8] 签名"
if [ ! -f debug.keystore ]; then
  "$JDK/bin/keytool.exe" -genkeypair -keystore debug.keystore \
    -storepass android -keypass android -alias androiddebugkey \
    -keyalg RSA -keysize 2048 -validity 10000 \
    -dname "CN=Android Debug,O=Android,C=US" >/dev/null 2>&1
  echo "  （已生成 debug.keystore）"
fi
"$BT/apksigner.bat" sign --ks debug.keystore --ks-pass pass:android \
  --key-pass pass:android --out "$APK" build/aligned.apk
"$BT/apksigner.bat" verify --print-certs "$APK" 2>/dev/null | head -2

echo "[8/8] 核对 APK 内容（assets / dex 类名 / Shizuku authority）"
# 校验只有一份实现（tools/check_play_apk.py），build.sh 不再自己抄一遍 ——
# 两处实现必然分叉（教训 9），而且那份抄的要能抓到「authority 少一位」这种错，
# 得用 aapt2 解析属性值，不是 `in` 子串搜索。
( cd "$ROOT" && "$PY" tools/check_play_apk.py "$HERE/$APK" )
echo "✓ $APK  $(stat -c%s "$APK") B"

# ★ 2026-10-30：以前这里判的是 `$1`，于是「变体名 + install」那种写法 **静默不装**
#   （脚本跑完 exit 0、APK 也生成了，设备上还是旧版 ⇒ 只能靠 lastUpdateTime 才发现）。
#   改成扫描全部参数：`install` 出现在哪一位都算。
DO_INSTALL=0
for _a in "$@"; do
  case "$_a" in install) DO_INSTALL=1 ;; esac
done
if [ "$DO_INSTALL" = "1" ]; then
  ADB="${ADB:-D:/platform-tools/adb.exe}"
  _pkg=io.pgauto.play
  # 2026-11-03：签名不一致（典型：换目录重新 clone ⇒ debug.keystore 被现场重新生成，
  # 公开仓按设计不导出 keystore）时，install -r 会被系统拒绝
  # （INSTALL_FAILED_UPDATE_INCOMPATIBLE）。真正的新用户手机上没有旧包，遇不到；
  # 遇到的是已有旧安装的人 —— 对他们「卸载重装」有代价，必须打印清楚再动手。
  # 默认自动重装；打印警告后留 5 秒可 Ctrl-C 中止（不做成 y/N 问答，免得卡住整段粘贴的用法）。
  if _out="$(MSYS_NO_PATHCONV=1 "$ADB" install -r "$APK" 2>&1)"; then
    if [ -n "$_out" ]; then printf '%s\n' "$_out"; fi
  else
    printf '%s\n' "$_out"
    if printf '%s' "$_out" | grep -q "INSTALL_FAILED_UPDATE_INCOMPATIBLE"; then
      echo "  ⚠ 手机上已装的 $_pkg 是另一把签名钥匙签的 ⇒ 覆盖安装被系统拒绝。"
      echo "  ⚠ 降级为「卸载重装」：端内标定数据（▶ 落点 / 各曲 K / 设备 c）与打歌日志会随卸载清零，"
      echo "    重装后 Shizuku 授权也要重新点一次。想保数据就现在 Ctrl-C 中止，"
      echo "    把旧的 debug.keystore 拷到本仓 android/play/ 下再重跑。5 秒后开始卸载……"
      sleep 5
      MSYS_NO_PATHCONV=1 "$ADB" uninstall "$_pkg"
      MSYS_NO_PATHCONV=1 "$ADB" install -r "$APK"
    else
      exit 1
    fi
  fi
  echo "  装机后核对（⛔ 看 lastUpdateTime，别看 versionCode）："
  MSYS_NO_PATHCONV=1 "$ADB" shell dumpsys package io.pgauto.play \
    | grep -E "versionName|lastUpdateTime" | head -2
fi
