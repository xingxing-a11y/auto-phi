#!/usr/bin/env bash
# 抢 USB 调试的「时间窗口」—— HONOR(MagicOS) 的 USB 调试会自动关闭。
#
# 官方答复（荣耀俱乐部多条一致）：
#   「在开发人员选项中开启 USB 调试开关后，此功能会自动关闭，是设计如此，
#     为保证系统运行流畅，减少功耗，在隔一段时间不使用或退出开发人员选项后会自动关闭。
#     若要让此功能不自动关闭，请开启「仅充电模式下允许 ADB 调试」的开关，
#     再开启「USB 调试」。 」
#   ⇒ 正确顺序 = 先开「仅充电模式下允许 ADB 调试」，再开「USB 调试」。
#
# 为什么要这个脚本：
#   用户开完 USB 调试几秒内就被系统关掉，2 秒轮询抓不住。这里用 1 秒轮询，
#   一看到设备就立刻 (1) 固化 adb_enabled (2) 拉 Shizuku —— 这两步都在窗口里做。
#
# 用法: bash tools/adb_window.sh [等待秒数, 默认300]

set -u
ADB="${ADB:-D:/platform-tools/adb.exe}"
LIMIT="${1:-300}"
export MSYS_NO_PATHCONV=1
PKG="moe.shizuku.privileged.api"

[ -x "$ADB" ] || { echo "ERROR: adb 不存在: $ADB" >&2; exit 1; }

now() { date +%H:%M:%S; }
dev() { "$ADB" devices 2>/dev/null | awk 'NR>1 && $2=="device" {print $1; exit}'; }

"$ADB" start-server >/dev/null 2>&1

echo "[$(now)] 开始抢窗口，最多等 ${LIMIT}s"
FOUND=""
# 用 `adb wait-for-device` 阻塞等，而不是轮询：
#   ① adb server 监听 USB 是内核事件驱动，插上即刻返回，检测延迟 <100ms；
#   ② 轮询版每次 `adb devices` 本身要 ~1s，粒度退化成 1.2s，抓不住 2~3 秒的窗口（自测实测）。
# 另外 LIMIT 按真实时间算、不按迭代次数（轮询版「3 秒」实际跑 6 秒，已修）。
START_TS=$(date +%s)
while :; do
    REMAIN=$(( LIMIT - ($(date +%s) - START_TS) ))
    [ "$REMAIN" -le 0 ] && break
    timeout "$REMAIN" "$ADB" wait-for-device >/dev/null 2>&1
    FOUND="$(dev)"
    [ -n "$FOUND" ] && break
    [ "$(($(date +%s) - START_TS))" -ge "$LIMIT" ] && break
    sleep 0.2
done
i=$(( $(date +%s) - START_TS ))

if [ -z "$FOUND" ]; then
    echo "[$(now)] 超时 ${LIMIT}s，设备始终没上线（USB 调试没开 / 线没插 / 窗口太短）"
    exit 2
fi

echo "[$(now)] ★ 抓到设备: $FOUND（等了 ${i}s）"

# --- 窗口内第一件事：把 adb_enabled 写死 ---
echo "  adb_enabled(改前) = $("$ADB" shell settings get global adb_enabled 2>&1 | tr -d '\r')"
"$ADB" shell settings put global adb_enabled 1 >/dev/null 2>&1
echo "  adb_enabled(改后) = $("$ADB" shell settings get global adb_enabled 2>&1 | tr -d '\r')"

# --- 顺手把之前已设的省电/后台白名单再确一遍（幂等，失败不致命）---
"$ADB" shell "cmd appops set $PKG RUN_IN_BACKGROUND allow" >/dev/null 2>&1
"$ADB" shell "cmd appops set $PKG RUN_ANY_IN_BACKGROUND allow" >/dev/null 2>&1
echo "  appops RUN_* = $("$ADB" shell "cmd appops get $PKG" 2>/dev/null | grep -ciE 'RUN_.*allow') 项 allow"

# --- 窗口内第二件事：拉 Shizuku ---
echo "=== 拉起 Shizuku ==="
bash "$(dirname "$0")/shizuku_start.sh" 2>&1 | sed 's/^/  /'
