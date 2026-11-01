#!/usr/bin/env bash
# 一键启动 Shizuku（走 USB adb，不需要无线调试配对码）
#
# 用法（Git Bash）:  bash tools/shizuku_start.sh
# 前提: 手机用 USB 连着 + 已开「USB 调试」
#
# 为什么要有这个脚本:
#   手机的「通过无线调试启动」在 HONOR 上会卡在「正在搜索配对服务」——
#   Shizuku 官方故障页第一条: 搜索配对服务要访问本地网络, 厂商在应用不可见后
#   立刻禁止访问网络。而我们本来就有 USB adb, 走官方给的「通过连接电脑启动」
#   一条命令就完事, 且**不需要配对码**。
#
# 注意: 手机重启后要再跑一次（Shizuku 官方设计, 无 root 无法开机自启）。
#      启动后 adb 线可以拔, 服务常驻（PPID=1, 已脱离 adb）。

set -u

ADB="${ADB:-D:/platform-tools/adb.exe}"
PKG="moe.shizuku.privileged.api"
export MSYS_NO_PATHCONV=1

say() { printf '%s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

# --- 0. adb 本身存在? （缺这一步会把「adb 路径错」误报成「没有在线设备」）---
[ -x "$ADB" ] || die "adb 不存在或不可执行: $ADB   （默认应为 D:/platform-tools/adb.exe，可用 ADB=<路径> 覆盖）"

# --- 1. 设备在线? ---
"$ADB" start-server >/dev/null 2>&1
DEV="$("$ADB" devices | awk 'NR>1 && $2=="device" {print $1; exit}')"
[ -n "$DEV" ] || die "没有在线设备（检查 USB 线 / USB 调试开关）"
say "设备: $DEV"

# --- 2. 已在跑就别重复启动 ---
if "$ADB" shell "ps -A | grep -q shizuku_server" >/dev/null 2>&1; then
    say "Shizuku 服务已在运行，无需重复启动"
    "$ADB" shell "ps -A | grep shizuku_server"
    exit 0
fi

# --- 3. 动态解析 libshizuku.so 路径（不能硬编码: 路径含安装 hash, 升级 APK 就变）---
BASE="$("$ADB" shell "pm path $PKG" 2>/dev/null | sed -n 's/^package://p' | head -1 | tr -d '\r')"
[ -n "$BASE" ] || die "找不到 $PKG，先安装 Shizuku"
APPDIR="$(dirname "$BASE")"
SO="$APPDIR/lib/arm64/libshizuku.so"
[ "$("$ADB" shell "test -x '$SO' && echo yes" 2>/dev/null | tr -d '\r')" = "yes" ] \
    || die "libshizuku.so 不可执行: $SO"
say "启动器: $SO"

# --- 4. 启动（pid 已 detach, 命令会自己返回）---
"$ADB" shell "'$SO'" >/dev/null 2>&1

# --- 5. 验收: 进程在 + UID 必须是 2000(shell) ---
for _ in 1 2 3 4 5 6 7 8 9 10; do
    PID="$("$ADB" shell "pidof shizuku_server" 2>/dev/null | tr -d '\r')"
    [ -n "$PID" ] && break
    sleep 0.5
done
[ -n "${PID:-}" ] || die "启动失败: 10 次轮询内没看到 shizuku_server 进程"

UIDLINE="$("$ADB" shell "grep -E '^Uid:' /proc/$PID/status" 2>/dev/null | tr -d '\r')"
case "$UIDLINE" in
    *2000*) say "OK  shizuku_server pid=$PID  $UIDLINE" ;;
    *)      die "进程起来了但身份不对（期望 2000）: $UIDLINE" ;;
esac

say "完成。Shizuku app 里应显示「正在运行」。现在可以拔 USB 线，服务常驻到下次重启。"
