#!/usr/bin/env bash
# 探针 APK 的最小构建链（**不用 Gradle**）。
#
# 为什么不用 Gradle：
#   这个 app 纯 Java、零依赖、无 res 目录（只有 manifest），Gradle 反而要下 ~130 MB
#   的发行版 + 一堆插件依赖。build-tools 里的 aapt2 / d8 / zipalign / apksigner 足够。
#   （2026-10-17 实测 services.gradle.org 在本机 SSL 握手失败，也是促成因素。）
#
# 用法：
#   bash android/probe/build.sh            # 产出 android/probe/build/pgprobe.apk
#   bash android/probe/build.sh install    # 构建 + adb install -r
#
# 依赖：JDK 17、Android SDK（platforms/android-34 + build-tools/34.0.0）
set -euo pipefail

SDK="${ANDROID_SDK:-D:/android-sdk}"
BT="$SDK/build-tools/34.0.0"
PLAT="$SDK/platforms/android-34/android.jar"
JDK="${JDK_HOME:-D:/OpenJDK17_17.0.19_10}"

HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"

for f in "$BT/aapt2.exe" "$BT/d8.bat" "$BT/zipalign.exe" "$BT/apksigner.bat" \
         "$PLAT" "$JDK/bin/javac.exe" "$JDK/bin/keytool.exe"; do
  [ -e "$f" ] || { echo "✗ 缺少 $f" >&2; exit 1; }
done

echo "[1/6] aapt2 link（manifest → base.apk）"
rm -rf build
mkdir -p build/classes build/apk
"$BT/aapt2.exe" link \
  -o build/base.apk \
  -I "$PLAT" \
  --manifest app/src/main/AndroidManifest.xml \
  --min-sdk-version 29 --target-sdk-version 34 \
  --version-code 1 --version-name 1.0

echo "[2/6] javac（→ build/classes）"
"$JDK/bin/javac.exe" -source 17 -target 17 -nowarn -encoding UTF-8 \
  -classpath "$PLAT" -d build/classes \
  $(find app/src/main/java -name '*.java')

echo "[3/6] d8（→ classes.dex）"
"$BT/d8.bat" --release --min-api 29 --lib "$PLAT" --output build/apk \
  $(find build/classes -name '*.class')

echo "[4/6] 把 dex 打进 apk"
PY="$HERE/../../.venv/Scripts/python.exe"
[ -x "$PY" ] || PY="$(command -v python)"
"$PY" - <<'PY'
import shutil, zipfile
shutil.copy('build/base.apk', 'build/unsigned.apk')
with zipfile.ZipFile('build/unsigned.apk', 'a', zipfile.ZIP_DEFLATED) as z:
    z.write('build/apk/classes.dex', 'classes.dex')
PY

echo "[5/6] zipalign（4 字节对齐，Android 11+ 必须）"
"$BT/zipalign.exe" -f -p 4 build/unsigned.apk build/aligned.apk

echo "[6/6] 签名"
if [ ! -f debug.keystore ]; then
  "$JDK/bin/keytool.exe" -genkeypair -keystore debug.keystore \
    -storepass android -keypass android -alias androiddebugkey \
    -keyalg RSA -keysize 2048 -validity 10000 \
    -dname "CN=Android Debug,O=Android,C=US" >/dev/null 2>&1
  echo "  （已生成 debug.keystore）"
fi
"$BT/apksigner.bat" sign --ks debug.keystore --ks-pass pass:android \
  --key-pass pass:android --out build/pgprobe.apk build/aligned.apk

"$BT/apksigner.bat" verify --print-certs build/pgprobe.apk 2>/dev/null | head -2
echo "✓ build/pgprobe.apk  $(stat -c%s build/pgprobe.apk) B"

if [ "${1:-}" = "install" ]; then
  ADB="${ADB:-D:/platform-tools/adb.exe}"
  MSYS_NO_PATHCONV=1 "$ADB" install -r -g build/pgprobe.apk
  echo "✓ 已安装（-g 自动授予 RECORD_AUDIO）"
fi
