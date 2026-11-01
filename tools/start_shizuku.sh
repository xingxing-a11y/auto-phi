#!/usr/bin/env bash
# 启动 Shizuku（**无需 root**；手机每次重启后必须重跑一次 —— Shizuku 官方明示）。
#
# 原理（Shizuku 13.x）：官方"通过连接电脑启动（使用 adb）"给的命令是
#     adb shell /data/app/~~<hash>/moe.shizuku.privileged.api-<hash>/lib/arm64/libshizuku.so
# 那个 .so 就是它的 starter（直接可执行），跑完会 fork 出 `shizuku_server`（uid=shell）。
# 路径里的 hash 随安装/升级变化 ⇒ 不要写死，用 `pm path` 现取再替换 `base.apk`。
#
# 用法：  bash tools/start_shizuku.sh
#        ADB=/path/to/adb bash tools/start_shizuku.sh
set -euo pipefail

ADB="${ADB:-D:/platform-tools/adb.exe}"
export MSYS_NO_PATHCONV=1

if ! "$ADB" get-state >/dev/null 2>&1; then
  echo "✗ adb 没连上设备（先插 USB / 开 USB 调试）" >&2
  exit 2
fi

apk=$("$ADB" shell pm path moe.shizuku.privileged.api | tr -d '\r' | head -1 | sed 's/^package://')
if [ -z "$apk" ]; then
  echo "✗ 设备上没装 Shizuku（moe.shizuku.privileged.api）" >&2
  exit 2
fi
starter=$(printf '%s' "$apk" | sed 's#/base\.apk$#/lib/arm64/libshizuku.so#')
echo "starter: $starter"

if "$ADB" shell ps -A | tr -d '\r' | grep -q shizuku_server; then
  echo "✓ shizuku_server 已经在跑，无需重复启动"
  exit 0
fi

"$ADB" shell "$starter" | tr -d '\r' | tail -3
sleep 1
if "$ADB" shell ps -A | tr -d '\r' | grep -q shizuku_server; then
  echo "✓ shizuku_server 已启动"
else
  echo "✗ 没起来：看 Shizuku App 首页是否显示「Shizuku 正在运行」" >&2
  exit 1
fi
