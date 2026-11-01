#!/usr/bin/env bash
# APK 端内注入可行性探针（**不用 Gradle**，与 android/probe/build.sh 同一套工具链）。
#
# 用途：在真机上、以 shell(uid 2000) 身份量出端内方案赖以成立的四件事。
#   为什么能在 shell 身份下量：Shizuku UserService 也是 app_process 起的，
#   所以本探针的结论对 Shizuku 档直接适用 —— 不需要先装 Shizuku 才能验证。
#
# 用法（Git Bash，cwd = 仓库根）：
#   bash tools/apk_probe/build.sh ticker [n]     # 1 ms tick 精度（n 默认 4000）
#   bash tools/apk_probe/build.sh api            # GestureDescription 真值 + InputManager 可达性
#   bash tools/apk_probe/build.sh api2           # InputManager 失败根因 + IInputManager 备路
#   bash tools/apk_probe/build.sh inject [n]     # 真实注入耗时（n 默认 300）
#
# 安全：inject 只注 ACTION_MOVE 且不带前序 DOWN，不会生成点击、不会误触按钮。
# 清理：跑完自动删设备上的 jar（trap EXIT）；失败也会删。
#
# 依赖：JDK 17、Android SDK（platforms/android-34 + build-tools/34.0.0）、adb
set -euo pipefail

SDK="${ANDROID_SDK:-D:/android-sdk}"
BT="$SDK/build-tools/34.0.0"
PLAT="$SDK/platforms/android-34/android.jar"
JDK="${JDK_HOME:-D:/OpenJDK17_17.0.19_10}"
ADB="${ADB:-D:/platform-tools/adb.exe}"

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
cd "$HERE"

TARGET="${1:-}"
case "$TARGET" in
  ticker) CLS=TickerProbe; ARG="${2:-4000}" ;;
  api)    CLS=ApiProbe;    ARG="" ;;
  api2)   CLS=ApiProbe2;   ARG="" ;;
  inject) CLS=InjectProbe; ARG="${2:-300}" ;;
  *) sed -n '2,20p' "$0"; exit 2 ;;
esac

for f in "$BT/d8.bat" "$PLAT" "$JDK/bin/javac.exe" "$ADB"; do
  [ -e "$f" ] || { echo "✗ 缺少 $f" >&2; exit 1; }
done

OUT="$ROOT/tmp/_apkprobe_$CLS"
DEV_JAR="/data/local/tmp/pgprobe_$CLS.jar"
cleanup() { "$ADB" shell "rm -f $DEV_JAR" >/dev/null 2>&1 || true; }
trap cleanup EXIT

rm -rf "$OUT"; mkdir -p "$OUT/classes" "$OUT/dex"

echo "[1/4] javac $CLS.java"
"$JDK/bin/javac.exe" -source 17 -target 17 -nowarn -encoding UTF-8 \
  -classpath "$PLAT" -d "$OUT/classes" "$CLS.java"

echo "[2/4] d8 → classes.dex"
"$BT/d8.bat" --release --min-api 29 --lib "$PLAT" \
  --output "$OUT/dex" "$OUT/classes/$CLS.class" >/dev/null

echo "[3/4] 打包 + push"
(/d/code/phigros-auto/.venv/Scripts/python.exe - "$OUT/dex/classes.dex" "$OUT/$CLS.jar" <<'PY'
import sys, zipfile
dex, jar = sys.argv[1], sys.argv[2]
with zipfile.ZipFile(jar, "w", zipfile.ZIP_DEFLATED) as z:
    z.write(dex, "classes.dex")
PY
) || {
  # 没装 venv 时退回系统 python
  python - "$OUT/dex/classes.dex" "$OUT/$CLS.jar" <<'PY'
import sys, zipfile
dex, jar = sys.argv[1], sys.argv[2]
with zipfile.ZipFile(jar, "w", zipfile.ZIP_DEFLATED) as z:
    z.write(dex, "classes.dex")
PY
}
# ★ push 必须带 MSYS_NO_PATHCONV=1：否则 Git Bash 会把 /data/local/tmp/...
#   改写成 D:/WorkBuddy/resources/vendor/PortableGit/data/local/tmp/...
#   （2026-10-20 自测实踩，报 "remote secure_mkdirs failed"）
# ★ 但同一个开关会连**源路径**一起禁用转换 ⇒ 源必须自己转成 Windows 形式，
#   否则 adb 拿到 "/d/code/..." 这种 MSYS 路径会 "cannot stat"（第二次实踩）。
MSYS_NO_PATHCONV=1 "$ADB" push "$(cygpath -w "$OUT/$CLS.jar")" "$DEV_JAR" >/dev/null

echo "[4/4] app_process $CLS $ARG（uid 2000）"
# shellcheck disable=SC2086
MSYS_NO_PATHCONV=1 "$ADB" shell "CLASSPATH=$DEV_JAR app_process /system/bin $CLS $ARG"

echo "（设备侧 jar 已清理）"
