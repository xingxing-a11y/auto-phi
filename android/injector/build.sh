#!/usr/bin/env bash
# 注入器 APK 的最小构建链（**不用 Gradle**）。
#
# 与 android/probe/build.sh 同构，多了三件事：
#   ① 从 4 个 Shizuku aar 里抽 classes.jar 当编译依赖
#   ② 把它们一起交给 d8，打进同一个 classes.dex
#   ③ manifest 里手写 ShizukuProvider（provider aar 自己不带这条，见该文件注释）
#
# 为什么不用 Gradle：见 android/probe/build.sh 顶部注释（同样的原因，外加
# build-tools 里的 aapt2/d8/zipalign/apksigner 已经够用）。
#
# 用法：
#   bash android/injector/build.sh            # 产出 build/pginject.apk
#   bash android/injector/build.sh install    # 构建 + adb install -r
#
# 依赖：JDK 17、Android SDK（platforms/android-34 + build-tools/34.0.0）、
#       android/vendor/ 下 4 个 Shizuku aar（fetch_deps.sh 可重新下载）
set -euo pipefail

SDK="${ANDROID_SDK:-D:/android-sdk}"
BT="$SDK/build-tools/34.0.0"
PLAT="$SDK/platforms/android-34/android.jar"
JDK="${JDK_HOME:-D:/OpenJDK17_17.0.19_10}"

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
VENDOR="$ROOT/android/vendor"
cd "$HERE"

AARS=(shizuku-api-13.1.5.aar shizuku-aidl-13.1.5.aar \
      shizuku-shared-13.1.5.aar shizuku-provider-13.1.5.aar)

for f in "$BT/aapt2.exe" "$BT/d8.bat" "$BT/zipalign.exe" "$BT/apksigner.bat" \
         "$PLAT" "$JDK/bin/javac.exe" "$JDK/bin/keytool.exe"; do
  [ -e "$f" ] || { echo "✗ 缺少 $f" >&2; exit 1; }
done
for a in "${AARS[@]}"; do
  [ -f "$VENDOR/$a" ] || { echo "✗ 缺少 $VENDOR/$a（先跑 fetch_deps.sh）" >&2; exit 1; }
done

echo "[1/7] 从 aar 抽 classes.jar"
rm -rf build
mkdir -p build/classes build/apk build/libs
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

echo "[2/7] aapt2 link（manifest → base.apk）"
"$BT/aapt2.exe" link \
  -o build/base.apk \
  -I "$PLAT" \
  --manifest app/src/main/AndroidManifest.xml \
  --min-sdk-version 29 --target-sdk-version 34 \
  --version-code 5 --version-name 0.4-s4

echo "[3/7] javac"
# classpath 必须包含 4 个 jar：api 依赖 shared 的常量、aidl 的 IShizukuService，
# 少了任何一个都会在编译期报「找不到符号」——但运行时才发现就更难查。
CP="$PLAT"
for j in build/libs/*.jar; do CP="$CP;$(cygpath -w "$j" 2>/dev/null || echo "$j")"; done
"$JDK/bin/javac.exe" -source 17 -target 17 -nowarn -encoding UTF-8 \
  -classpath "$CP" -d build/classes \
  $(find app/src/main/java -name '*.java')

echo "[4/7] d8（本工程 class + 4 个 jar → classes.dex）"
"$BT/d8.bat" --release --min-api 29 --lib "$(cygpath -w "$PLAT" 2>/dev/null || echo "$PLAT")" \
  --output build/apk \
  $(find build/classes -name '*.class') build/libs/*.jar

echo "[5/7] 把 dex 打进 apk"
"$PY" - <<'PY'
import shutil, zipfile
shutil.copy('build/base.apk', 'build/unsigned.apk')
with zipfile.ZipFile('build/unsigned.apk', 'a', zipfile.ZIP_DEFLATED) as z:
    z.write('build/apk/classes.dex', 'classes.dex')
PY

echo "[6/7] zipalign"
"$BT/zipalign.exe" -f -p 4 build/unsigned.apk build/aligned.apk

echo "[7/7] 签名"
if [ ! -f debug.keystore ]; then
  "$JDK/bin/keytool.exe" -genkeypair -keystore debug.keystore \
    -storepass android -keypass android -alias androiddebugkey \
    -keyalg RSA -keysize 2048 -validity 10000 \
    -dname "CN=Android Debug,O=Android,C=US" >/dev/null 2>&1
  echo "  （已生成 debug.keystore）"
fi
"$BT/apksigner.bat" sign --ks debug.keystore --ks-pass pass:android \
  --key-pass pass:android --out build/pginject.apk build/aligned.apk
"$BT/apksigner.bat" verify --print-certs build/pginject.apk 2>/dev/null | head -2

echo "✓ build/pginject.apk  $(stat -c%s build/pginject.apk) B"

if [ "${1:-}" = "install" ]; then
  ADB="${ADB:-D:/platform-tools/adb.exe}"
  MSYS_NO_PATHCONV=1 "$ADB" install -r build/pginject.apk
fi
