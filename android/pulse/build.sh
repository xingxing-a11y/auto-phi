#!/usr/bin/env bash
# 「标定脉冲播放器」的最小构建链（**不用 Gradle**，与 android/probe/build.sh 同一套）。
#
# 为什么单独一个 APK：Android 的 playback capture 按 **uid** 分流 ——
# 捕获方自己的播放被路由到 SPEAKER、不进 REMOTE_SUBMIX，所以同一 app 自播自录
# 录到的恒是 0（FACTS §89）。换包名 = 换 uid，播放才会进 REMOTE_SUBMIX。
#
# 用法：
#   bash android/pulse/build.sh            # 产出 android/pulse/build/pgpulse.apk
#   bash android/pulse/build.sh install    # 构建 + adb install -r
set -euo pipefail

SDK="${ANDROID_SDK:-D:/android-sdk}"
BT="$SDK/build-tools/34.0.0"
PLAT="$SDK/platforms/android-34/android.jar"
JDK="${JDK_HOME:-D:/OpenJDK17_17.0.19_10}"

HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"

# 复用录制探针的 debug 签名（同一把钥匙，省一次 keytool；两个 app 互不影响）
KS="../probe/debug.keystore"

for f in "$BT/aapt2.exe" "$BT/d8.bat" "$BT/zipalign.exe" "$BT/apksigner.bat" \
         "$PLAT" "$JDK/bin/javac.exe" "$KS"; do
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
"$BT/apksigner.bat" sign --ks "$KS" --ks-pass pass:android \
  --key-pass pass:android --out build/pgpulse.apk build/aligned.apk

"$BT/apksigner.bat" verify --print-certs build/pgpulse.apk 2>/dev/null | head -2
echo "✓ build/pgpulse.apk  $(stat -c%s build/pgpulse.apk) B"

if [ "${1:-}" = "install" ]; then
  ADB="${ADB:-D:/platform-tools/adb.exe}"
  MSYS_NO_PATHCONV=1 "$ADB" install -r build/pgpulse.apk
  echo "✓ 已安装"
fi
