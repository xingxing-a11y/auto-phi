#!/usr/bin/env bash
# 端内**纯 JVM** 自测 —— 不需要 Android SDK、不需要真机。
#
# 六组：
#   · `OnsetGate` / `FrameClock`：曲首 onset 选点 + 帧→挂钟换算
#   · `OnsetWait` / `TapLead`：等待决策（含 10-24 修的竞态）+ tap 的 lead 解析
#   · `InjectTrace` / `RecvTrace`：注入**发送侧**与靶子**收货侧**的逐帧记录
#     （两侧格式必须能逐帧对账，见 tools/compare_touch.py）
#   · `PayloadCache`：载荷缓存判据（10-24 事故：等长修正被长度判据吞掉）
#   · `Scheduler` 闸门：canvas 16:9 / 事件在 screen 内 / 排程坐标在设备内
#
# 为什么单独一个脚本：这些类被刻意写成零 Android 依赖，所以能在这台 PC 上
# 用**没有 android.jar 的 classpath** 编译。classpath 一旦漏加就编不过 ⇒
# 这本身就是「它们确实没偷偷依赖 Android」的证明。
#
# 用法：
#   bash android/play/selftest.sh
set -euo pipefail

JDK="${JDK_HOME:-D:/OpenJDK17_17.0.19_10}"
HERE="$(cd "$(dirname "$0")" && pwd)"
SRC_MAIN="$HERE/app/src/main/java/io/pgauto/play"
SRC_TEST="$HERE/app/src/test/java/io/pgauto/play"
INJ_MAIN="$HERE/../injector/app/src/main/java/io/pgauto/inject"
INJ_TEST="$HERE/../injector/app/src/test/java/io/pgauto/inject"
OUT="$HERE/build/selftest-classes"

[ -x "$JDK/bin/javac.exe" ] || { echo "✗ 缺少 $JDK/bin/javac.exe" >&2; exit 1; }

rm -rf "$OUT"
mkdir -p "$OUT"

echo "[1/9] javac（**无 android.jar**：这几个类必须是纯 Java）"
# ★ 只编这几个文件，故意不带 -classpath android.jar。
#   一旦哪天有人在里面写了 Log.i/SystemClock，这里立刻编不过。
"$JDK/bin/javac.exe" -source 17 -target 17 -nowarn -encoding UTF-8 \
  -d "$OUT" \
  "$SRC_MAIN/OnsetGate.java" \
  "$SRC_MAIN/FrameClock.java" \
  "$SRC_MAIN/OnsetWait.java" \
  "$SRC_MAIN/TapLead.java" \
  "$SRC_MAIN/PayloadCache.java" \
  "$SRC_MAIN/EnvCodec.java" \
  "$SRC_MAIN/EnvMatcher.java" \
  "$SRC_MAIN/CalibMerge.java" \
  "$SRC_MAIN/CalibGate.java" \
  "$SRC_MAIN/DevFp.java" \
  "$SRC_MAIN/BuildInfo.java" \
  "$SRC_MAIN/LeadAudit.java" \
  "$SRC_MAIN/CourseStart.java" \
  "$SRC_MAIN/RezeroGate.java" \
  "$SRC_MAIN/CourseBound.java" \
  "$SRC_MAIN/CourseAbort.java" \
  "$SRC_MAIN/StartButtonBox.java" \
  "$SRC_MAIN/CalibKeys.java" \
  "$SRC_MAIN/AxisMap.java" \
  "$SRC_MAIN/FirstDown.java" \
  "$SRC_MAIN/TouchDevices.java" \
  "$SRC_MAIN/GetEventParser.java" \
  "$SRC_TEST/OnsetGateSelfTest.java" \
  "$SRC_TEST/PlayLogicSelfTest.java" \
  "$SRC_TEST/PayloadCacheSelfTest.java" \
  "$SRC_TEST/EnvCodecSelfTest.java" \
  "$SRC_TEST/EnvMatcherSelfTest.java" \
  "$SRC_TEST/CalibMergeSelfTest.java" \
  "$SRC_TEST/CalibGateSelfTest.java" \
  "$SRC_TEST/GuardSelfTest.java" \
  "$SRC_TEST/LeadAuditSelfTest.java" \
  "$SRC_TEST/CourseStartSelfTest.java" \
  "$SRC_TEST/RezeroGateSelfTest.java" \
  "$SRC_TEST/CourseBoundSelfTest.java" \
  "$SRC_TEST/CourseAbortSelfTest.java" \
  "$SRC_TEST/GestureClampSelfTest.java" \
  "$SRC_TEST/StartButtonBoxSelfTest.java" \
  "$SRC_TEST/CalibKeysSelfTest.java" \
  "$SRC_TEST/AxisMapSelfTest.java" \
  "$SRC_TEST/FirstDownSelfTest.java" \
  "$SRC_TEST/TouchDevicesSelfTest.java" \
  "$SRC_TEST/GetEventParserSelfTest.java" \
  "$INJ_MAIN/sched/Gesture.java" \
  "$INJ_MAIN/sched/Scheduler.java" \
  "$INJ_MAIN/sched/Densify.java" \
  "$INJ_MAIN/sched/InjectMode.java" \
  "$INJ_MAIN/sched/InjectTrace.java" \
  "$INJ_MAIN/sched/RecvTrace.java" \
  "$INJ_TEST/sched/InjectTraceSelfTest.java" \
  "$INJ_TEST/sched/RecvTraceSelfTest.java" \
  "$INJ_TEST/sched/InjectModeSelfTest.java" \
  "$INJ_TEST/sched/SchedulerGateSelfTest.java"

echo "[2/9] java io.pgauto.play.OnsetGateSelfTest"
"$JDK/bin/java.exe" -Dfile.encoding=UTF-8 -cp "$OUT" io.pgauto.play.OnsetGateSelfTest

echo "[3/9] java io.pgauto.play.PlayLogicSelfTest"
"$JDK/bin/java.exe" -Dfile.encoding=UTF-8 -cp "$OUT" io.pgauto.play.PlayLogicSelfTest

echo "[4/9] java io.pgauto.play.PayloadCacheSelfTest"
"$JDK/bin/java.exe" -Dfile.encoding=UTF-8 -cp "$OUT" io.pgauto.play.PayloadCacheSelfTest

echo "[10/10] java io.pgauto.play.EnvCodecSelfTest（包络编解码 = 标定链路唯一失真源）"
"$JDK/bin/java.exe" -Dfile.encoding=UTF-8 -cp "$OUT" io.pgauto.play.EnvCodecSelfTest

echo "[9b] java io.pgauto.play.CourseStartSelfTest（课题模式：互相关定曲首）"

echo "[9c] java io.pgauto.play.RezeroGateSelfTest（rezero 采纳闸门）"
"$JDK/bin/java.exe" -Dfile.encoding=UTF-8 -cp "$OUT" io.pgauto.play.RezeroGateSelfTest

echo "[9d] java io.pgauto.play.CourseBoundSelfTest（课题模式：失败路径的下界判据）"
"$JDK/bin/java.exe" -Dfile.encoding=UTF-8 -cp "$OUT" io.pgauto.play.CourseBoundSelfTest

echo "[9e] java io.pgauto.play.CourseAbortSelfTest（课题模式：中止原因落盘 + Toast 文案）"
"$JDK/bin/java.exe" -Dfile.encoding=UTF-8 -cp "$OUT" io.pgauto.play.CourseAbortSelfTest

# 源码闸门：rezero 必须走采纳闸门（2026-10-28）
if grep -qF 'startRezeroWatcher(job, tap, elapsed0, epoch0, zeroEpoch, trim, i);' "$SRC_MAIN/PlayService.java" \
   && grep -qF 'RezeroGate.why(zeroGateMs, zeroCorr)' "$SRC_MAIN/PlayService.java" \
   && grep -qF 'job.brain.rezero(zeroCorr)' "$SRC_MAIN/PlayService.java"; then
  echo "  ✓ rezero 走 RezeroGate 闸门（互相关锁错时只记日志、不纠正）"
else
  echo "  ✗ rezero 没接闸门 ⇒ 互相关一旦锁错就会把整轮改坏"; gate_fail=1
fi
"$JDK/bin/java.exe" -Dfile.encoding=UTF-8 -cp "$OUT" io.pgauto.play.CourseStartSelfTest

echo "[11/11] java io.pgauto.play.EnvMatcherSelfTest（端内互相关：位移还原/增益不变/先验/质量）"
"$JDK/bin/java.exe" -Dfile.encoding=UTF-8 -cp "$OUT" io.pgauto.play.EnvMatcherSelfTest

# ★ 12：CalibMerge 的规则自测**以前根本没被这个脚本跑过**（文件在、但 javac 清单与运行清单都没有它）
#   ⇒ G6/G8/G9 那套融合规则等于零回归保护。现在补进来，并与后面的源码闸门双保险。
echo "[12/15] java io.pgauto.play.CalibMergeSelfTest（K/lead 融合：首样本闸/离群/连续同侧重置）"
"$JDK/bin/java.exe" -Dfile.encoding=UTF-8 -cp "$OUT" io.pgauto.play.CalibMergeSelfTest

echo "[13/15] java io.pgauto.play.CalibGateSelfTest（G4 标定连续性 / G5 交叉校验）"
"$JDK/bin/java.exe" -Dfile.encoding=UTF-8 -cp "$OUT" io.pgauto.play.CalibGateSelfTest

echo "[14/15] java io.pgauto.play.GuardSelfTest（G2 设备指纹 / G13 构建自证）"
"$JDK/bin/java.exe" -Dfile.encoding=UTF-8 -cp "$OUT" io.pgauto.play.GuardSelfTest

echo "[15/16] java io.pgauto.play.LeadAuditSelfTest（lead 事后对账：样本坏回滚 / 均值坏重置）"
"$JDK/bin/java.exe" -Dfile.encoding=UTF-8 -cp "$OUT" io.pgauto.play.LeadAuditSelfTest

echo "[16/17] java io.pgauto.play.GestureClampSelfTest（触点在系统手势区外）"
"$JDK/bin/java.exe" -Dfile.encoding=UTF-8 -cp "$OUT" io.pgauto.play.GestureClampSelfTest

echo "[17/17] java io.pgauto.play.StartButtonBoxSelfTest（▶ 落点与按钮预期框）"
"$JDK/bin/java.exe" -Dfile.encoding=UTF-8 -cp "$OUT" io.pgauto.play.StartButtonBoxSelfTest

echo "[18/18] java io.pgauto.play.CalibKeysSelfTest（K 的读取集必须被删除集覆盖）"
"$JDK/bin/java.exe" -Dfile.encoding=UTF-8 -cp "$OUT" io.pgauto.play.CalibKeysSelfTest

echo "[19/19] java io.pgauto.play.AxisMapSelfTest（触摸 raw ↔ 屏幕坐标：往返闭合 / 标定唯一性）"
"$JDK/bin/java.exe" -Dfile.encoding=UTF-8 -cp "$OUT" io.pgauto.play.AxisMapSelfTest

echo "[20/20] java io.pgauto.play.FirstDownSelfTest（多点击只认第一次 —— 用户 12:12 口径）"
"$JDK/bin/java.exe" -Dfile.encoding=UTF-8 -cp "$OUT" io.pgauto.play.FirstDownSelfTest

echo "[21/21] java io.pgauto.play.TouchDevicesSelfTest（getevent -p → 挑唯一触摸屏 + 量程）"
"$JDK/bin/java.exe" -Dfile.encoding=UTF-8 -cp "$OUT" io.pgauto.play.TouchDevicesSelfTest

echo "[22/22] java io.pgauto.play.GetEventParserSelfTest（getevent 逐行 → DOWN/UP/MOVE）"
"$JDK/bin/java.exe" -Dfile.encoding=UTF-8 -cp "$OUT" io.pgauto.play.GetEventParserSelfTest

echo "[5/9] java io.pgauto.inject.sched.InjectTraceSelfTest"
"$JDK/bin/java.exe" -Dfile.encoding=UTF-8 -cp "$OUT" io.pgauto.inject.sched.InjectTraceSelfTest

echo "[6/9] java io.pgauto.inject.sched.RecvTraceSelfTest"
"$JDK/bin/java.exe" -Dfile.encoding=UTF-8 -cp "$OUT" io.pgauto.inject.sched.RecvTraceSelfTest

echo "[7/9] java io.pgauto.inject.sched.SchedulerGateSelfTest"
"$JDK/bin/java.exe" -Dfile.encoding=UTF-8 -cp "$OUT" io.pgauto.inject.sched.SchedulerGateSelfTest

echo "[8/9] java io.pgauto.inject.sched.InjectModeSelfTest"
"$JDK/bin/java.exe" -Dfile.encoding=UTF-8 -cp "$OUT" io.pgauto.inject.sched.InjectModeSelfTest

# ─────────────────────────────────────────────────────────────────────────────
# [9/9] 源码闸门：A/B 开关必须**真的**接到 inject.invoke 上
#
# 为什么需要它（这一条是纯 JVM 自测补不上的洞）：`DevRun` 引 android.view.MotionEvent，
# 进不了上面那条「不带 android.jar」的编译链 ⇒ 「加了 injectMode 参数却仍在 emit 里写
# 字面量」这种空转，编译和单测**都不会红**，而它会让这一枪 A/B 完全无效（成绩一模一样）。
# 局限要说清：这是文本断言，不是语义证明；它能抓「忘了改」和「改回去了」，
# 抓不到「改了但传错变量名」—— 后者靠真机日志的 `inject_mode=` + 注入耗时跳变兜底。
# ─────────────────────────────────────────────────────────────────────────────
echo "[9/9] 源码闸门：E2 的注入模式开关是否接到 inject.invoke"
gate_fail=0
if grep -qF "inject.invoke(proxy, ev, injectMode)" "$INJ_MAIN/DevRun.java"; then
  echo "  ✓ DevRun.emit 用的是 injectMode 字段"
else
  echo "  ✗ DevRun.emit 没有把 injectMode 交给 inject.invoke（开关会空转）"; gate_fail=1
fi
if grep -qF "inject.invoke(proxy, ev, Injector.MODE_" "$INJ_MAIN/DevRun.java"; then
  echo "  ✗ DevRun.emit 仍在用模式**字面量**（A/B 开关空转）"; gate_fail=1
fi
if grep -qF "log, stop, Injector.MODE_ASYNC);" "$INJ_MAIN/DevRun.java"; then
  echo "  ✓ 旧构造仍委托 ASYNC（默认值 = 生产值）"
else
  echo "  ✗ 旧构造的默认模式被改了（所有老调用方会悄悄换模式）"; gate_fail=1
fi
# ── onNewIntent 这条是 10-24 真机实测出来的空转机制 ───────────────────────────
# `am start` 在本 Activity 已在栈顶时**不新建实例**，只把 intent 投给现有实例
# （am start 打印 "Warning: Activity not started, intent has been delivered to
# currently running top-most instance."）。MainActivity 原先没实现 onNewIntent
# ⇒ 就算 intent 真被投递，extra 也会被静默丢弃。
# ⚠ 但这几条 grep **不能**证明「栈顶改参数可用」：真机实测（A14/ALT-AN00）连
#   onNewIntent 都不触发（诊断日志一行没有），intent 在 AMS 层就丢了。
#   ⇒ 操作硬规矩：**改参数一律 `am force-stop` 后再 `am start`**，并在首页副标题上
#   确认 `注入=wait/async` 与预期一致再开火。
if grep -qF "protected void onNewIntent(Intent intent)" "$SRC_MAIN/MainActivity.java"; then
  echo "  ✓ MainActivity 实现了 onNewIntent（否则 am start 的 extra 会被静默丢弃）"
else
  echo "  ✗ MainActivity 缺 onNewIntent ⇒ 栈顶时 am start --ez sync true 静默失效"; gate_fail=1
fi
if grep -A12 "protected void onNewIntent(Intent intent)" "$SRC_MAIN/MainActivity.java" \
     | grep -qF "readLaunchOverrides();"; then
  echo "  ✓ onNewIntent 重新解析了覆盖项"
else
  echo "  ✗ onNewIntent 没有调用 readLaunchOverrides（收到 intent 却不读，等于没实现）"; gate_fail=1
fi
if grep -qF "tvModeLabel = tv(subtitleText()" "$SRC_MAIN/MainActivity.java"; then
  echo "  ✓ 首页副标题来自 subtitleText() 唯一来源"
else
  echo "  ✗ 副标题没有走 subtitleText()（会与实际注入模式漂移）"; gate_fail=1
fi
# 2026-11-01 用户定的口径：顶部 =「L_dev 默认值 + 构建号」，运行模式口径整段下线。
# 两个方向都钉：旧串不许回来（否则又会被当成"当前曲的信息"），新串必须在（否则改参数没证据）。
if ! grep -qF '注入=async · 零点=' "$SRC_MAIN/MainActivity.java" \
   && grep -qF 'return "L_dev=" + String.format(java.util.Locale.ROOT, "%.3f", lDevS)' \
          "$SRC_MAIN/MainActivity.java" \
   && grep -qF '" · 构建 " + buildInfo().brief()' "$SRC_MAIN/MainActivity.java"; then
  echo "  ✓ 顶部副标题 = L_dev 默认值 + 构建号（运行模式口径已下线）"
else
  echo "  ✗ 顶部副标题口径不对（运行模式又回来了，或 L_dev / 构建号丢了）"; gate_fail=1
fi
# 2026-11-01：倒计时逐秒只更新阶段（顶部大字 + 通知栏），**不再往日志环里写行**。
# 界面只显示最后 N 行，而倒计时 8 s 会占掉其中 8 行 ⇒「收到零点」「K 已并入平均」被挤出可视区。
if ! grep -qF 'RunLog.line("倒计时 "' "$SRC_MAIN/PlayService.java" \
   && grep -qF 'RunLog.phaseQuiet("倒计时 "' "$SRC_MAIN/PlayService.java" \
   && grep -qF 'public static void phaseQuiet(String p)' "$SRC_MAIN/RunLog.java"; then
  echo "  ✓ 倒计时只更新阶段（顶部/通知栏），不逐秒挤占日志"
else
  echo "  ✗ 倒计时又逐秒写日志了 ⇒ 8 行倒计时会把关键行挤出可视区"; gate_fail=1
fi
# ── 2026-11-01 用户报的 bug：课题→帮助→课题 之后返回键在两个页面之间来回跳 ──────
# 根因是两个独立页各记一个"来路"字段、互相记录；修法 = 返回一律回选歌页、字段全删。
# 断言盯两件事：① 来路字段不许回来；② onBackPressed 必须显式接住 100/101。
# ⛔ 不能只靠 `step--`：STEP_COURSE-1 正好等于 STEP_HELP，减 1 会掉进另一个独立页。
if ! grep -qE 'helpReturn|courseReturn' "$SRC_MAIN/MainActivity.java" \
   && grep -qF 'if (step == STEP_HELP || step == STEP_COURSE) {' "$SRC_MAIN/MainActivity.java" \
   && grep -qF 'content.addView(section("课题模式"));' "$SRC_MAIN/MainActivity.java"; then
  echo "  ✓ 教程/课题页返回一律回选歌页（不再记忆来路）"
else
  echo "  ✗ 独立页又开始记来路了 ⇒ 课题↔帮助会互相记录，返回键来回跳（2026-11-01 真机 bug）"; gate_fail=1
fi
if grep -qF "trace.setInjectMode(injectMode" "$SRC_MAIN/PlayService.java"; then
  echo "  ✓ 注入模式写进了 trace（打完拉文件即可证明跑了哪个模式）"
else
  echo "  ✗ trace 里没有模式标记 ⇒ 又只能靠 logcat 取证（§167）"; gate_fail=1
fi
# ── §169 边沿口径：`sil_thr` 必须从 manifest **一路接到采集层** ────────────────
# 这一组防的是「数据打包了但没人读」：manifest 里有 sil_thr，SongLib 不读、或读了却
# 没往 OnsetTap 传 ⇒ 端内静默退回相对判据，日志照样写「零点=onset」，
# 而渐入曲的边沿会比母带晚几百毫秒（§169）。纯 JVM 单测抓不到「接线断了」。
if grep -qF 'o.optDouble("sil_thr"' "$SRC_MAIN/SongLib.java"; then
  echo "  ✓ SongLib 解析了 sil_thr"
else
  echo "  ✗ SongLib 没解析 sil_thr ⇒ 字段白写，边沿退回相对判据"; gate_fail=1
fi
if grep -qF "job.song.silThr" "$SRC_MAIN/PlayService.java" \
   && grep -qF "OnsetGate.withEdge(OnsetDetector.DEFAULT_FRAME_MS, silThrRms, origin)" "$SRC_MAIN/OnsetTap.java"; then
  echo "  ✓ sil_thr 经 PlayService → OnsetTap → OnsetGate.withEdge 接通"
else
  echo "  ✗ sil_thr 在 PlayService→OnsetTap→OnsetGate 之间断了（判据会静默退回相对）"; gate_fail=1
fi
if grep -qF "OnsetGate.withDefaults(OnsetDetector.DEFAULT_FRAME_MS, origin)" "$SRC_MAIN/OnsetTap.java"; then
  echo "  ✓ 未标定（sil_thr=0）时仍走 withDefaults（旧行为不变）"
else
  echo "  ✗ 未标定时没有回退分支 ⇒ 没有 sil_thr 的曲目会打不了"; gate_fail=1
fi
if grep -qF '边沿口径' "$SRC_MAIN/PlayService.java"; then
  echo "  ✓ prepare 阶段把边沿口径打进日志（开火前可核）"
else
  echo "  ✗ 日志里看不出这轮用的哪条边沿判据（又只能靠成绩反推）"; gate_fail=1
fi
# ── 本轮参数必须落进**产物**（FACTS §171.2 的取证缺口）─────────────────────────
# 打完之后 logcat 被游戏冲掉、界面日志只留最后 16 行 ⇒ 「这枪用的是哪个 L_dev」
# 只能靠进程启动参数推断。规矩（§167）：凡开关/参数，必须跟着数据走。
if grep -qF "trace.setRunMeta(" "$SRC_MAIN/PlayService.java"; then
  echo "  ✓ trace header 带了本轮参数（song/sil_thr/l_dev/零点）"
else
  echo "  ✗ trace header 只有 inject_mode ⇒ L_dev 事后无法直证"; gate_fail=1
fi
if grep -qF "job.zeroSrc.meta()" "$SRC_MAIN/PlayService.java" \
   && grep -qF "public String meta()" "$SRC_MAIN/OnsetZero.java"; then
  echo "  ✓ 零点参数来自 ZeroSource.meta()（单一来源，OnsetZero 提供实测值）"
else
  echo "  ✗ 零点参数没有从 ZeroSource 取（会与真实零点漂移）"; gate_fail=1
fi
# ★ 2026-11-01：副标题不再显示 L_dev（它与歌曲无关，且显示的是启动参数值，
#   勾了自动补偿时真正生效的是 (K + c)/1000）⇒ "改参数后要有界面证据"这条改由
#   难度卡承担：那里按曲显示 `K=… ms ⇒ L_dev=… s`。
if grep -qF '⇒ L_dev=%.3f s' "$SRC_MAIN/MainActivity.java" \
   && grep -qF 'String.format(Locale.ROOT, "K=%+.1f ms ⇒ L_dev=%.3f s"' "$SRC_MAIN/MainActivity.java"; then
  echo "  ✓ L_dev 按曲显示在难度卡上（副标题不再显示它）"
else
  echo "  ✗ 界面上没有任何地方显示 L_dev ⇒ 改参数后没有证据"; gate_fail=1
fi
if grep -qF "keepFrames" "$SRC_MAIN/OnsetTap.java" \
   && grep -qF "envN >= keepFrames" "$SRC_MAIN/OnsetTap.java"; then
  echo "  ✓ 标定模式会采够帧数才停（生产模式仍 FIRED 即停）"
else
  echo "  ✗ OnsetTap 没有 keepFrames ⇒ 标定只能采到曲首 0.9 s"; gate_fail=1
fi
# ── S2/S3：端内算 K 的链路（assets 指纹 → matcher → 存盘 → 自动 L_dev）──────────
if grep -qF "env_b64" "$SRC_MAIN/SongLib.java" && grep -qF "env_hop_ms" "$SRC_MAIN/SongLib.java"; then
  echo "  ✓ SongLib 读母带指纹 env_b64/env_hop_ms"
else
  echo "  ✗ SongLib 没读指纹 ⇒ 端内永远算不出 K"; gate_fail=1
fi
if grep -qF "EnvMatcher.match(" "$SRC_MAIN/PlayService.java" \
   && grep -qF 'putVerified("k_ms_' "$SRC_MAIN/PlayService.java"; then
  echo "  ✓ 标定结束时端内算 K 并存盘（k_ms_<曲>，走 G1 校验写盘）"
else
  echo "  ✗ 标定没有端内算 K（只能回 PC 算，M2 不成立）"; gate_fail=1
fi
if grep -qF "effectiveLDev(" "$SRC_MAIN/PlayService.java" \
   && grep -qF "EXTRA_AUTO_L, autoLDev" "$SRC_MAIN/MainActivity.java"; then
  echo "  ✓ 「自动 L_dev」开关接到 prepare（K + c）"
else
  echo "  ✗ 自动 L_dev 没接线（标定了也不会用）"; gate_fail=1
fi
# ── B（两段锚）源码闸门：这几条一旦破，Ⅱ 类的补音就会静默失效 ──────
DEV="$(cd "$(dirname "$0")" && pwd)/../injector/app/src/main/java/io/pgauto/inject/DevRun.java"
if grep -q "volatile long zeroNano" "$DEV" && grep -q "public void rezero(" "$DEV"; then
  echo "  ✓ DevRun 零点可变且有 rezero()"
else
  echo "  ✗ DevRun 零点仍是 final / 没有 rezero() ⇒ 两段锚不成立"; gate_fail=1
fi
if grep -q "sleepUntilT(" "$DEV" && ! grep -q "static void sleepUntil(long" "$DEV"; then
  echo "  ✓ 睡眠每轮重读零点（旧 static sleepUntil 已移除）"
else
  echo "  ✗ 还在用不可打断的 static sleepUntil ⇒ rezero 后会晚派"; gate_fail=1
fi
if grep -q "public double begin()" "$SRC_MAIN/OnsetZero.java"    && grep -q "public double finish()" "$SRC_MAIN/OnsetZero.java"; then
  echo "  ✓ OnsetZero 拆成 begin()/finish()"
else
  echo "  ✗ OnsetZero 没拆 ⇒ 点 ▶ 后无法立刻开打"; gate_fail=1
fi
if grep -q "runPreRoll(" "$SRC_MAIN/PlayService.java"    && grep -q "brain.rezero(" "$SRC_MAIN/PlayService.java"    && grep -q "只.*有 lead 历史.*⇒ 本轮不预派\|没有 lead 历史" "$SRC_MAIN/PlayService.java"; then
  echo "  ✓ 预派路径在，且没 lead 时明确不预派"
else
  echo "  ✗ 预派缺 lead 判空（会拿 NaN 当零点）"; gate_fail=1
fi
if grep -q "pre=1" "$SRC_MAIN/PlayService.java" && grep -q "rezero_delta_ms" "$SRC_MAIN/PlayService.java"; then
  echo "  ✓ trace 头记录 pre/rezero_delta（事后可自证）"
else
  echo "  ✗ trace 没记两段锚证据 ⇒ 分不清\"预派了\"和\"没预派\""; gate_fail=1
fi
if grep -q "song.blindLost > 0" "$SRC_MAIN/PlayService.java"; then
  echo "  ✓ 预派只对 Ⅱ 类（blindLost>0）开"
else
  echo "  ✗ 预派条件缺 blindLost ⇒ Ⅰ 类也会被预测零点污染"; gate_fail=1
fi

if grep -q "MIN_K_FRAMES" "$SRC_MAIN/PlayService.java"    && grep -q "MAX_ABS_K_MS" "$SRC_MAIN/PlayService.java"; then
  echo "  ✓ 算 K 有长度下限与 |K| 上限闸门（截断捕获不会写出离谱 K）"
else
  echo "  ✗ 缺 K 的合理性闸门 ⇒ 截断的标定会写出几百毫秒的 K"; gate_fail=1
fi

# ★ 两段锚的死分支复发检查：runOnce 必须先 buildZeroSource，再判断 instanceof OnsetZero。
#   曾经把 zeroSrc 建在 zeroOf() 内部 ⇒ 判断时恒为 null ⇒ 预派分支三轮都没进去。
_line_src=$(grep -n "buildZeroSource(job, countdownS);" "$SRC_MAIN/PlayService.java" | head -1 | cut -d: -f1)
_line_chk=$(grep -n "job.zeroSrc instanceof OnsetZero) {" "$SRC_MAIN/PlayService.java" | head -1 | cut -d: -f1)
if [ -n "$_line_src" ] && [ -n "$_line_chk" ] && [ "$_line_src" -lt "$_line_chk" ]; then
  echo "  ✓ 先建零点源再判 instanceof（预派分支不会恒为假）"
else
  echo "  ✗ buildZeroSource 不在 instanceof 之前 ⇒ 两段锚又会退化成死分支"; gate_fail=1
fi

# ★ G13：MOVE 不许丢（丢 MOVE 会让 Drag 判不到，症状=0 Good/0 Bad 的纯 Miss）
if grep -q "new DevRun(job.proxy, job.inject, true, false" "$SRC_MAIN/PlayService.java"; then
  echo "  ✓ 注入器不再丢迟到的 MOVE（G13）"
else
  echo "  ✗ DevRun 的 dropMoves 又变回 true ⇒ Drag 会偶发整批评 Miss"; gate_fail=1
fi

# ── 数据完整性防护第二批（2026-10-25 19:2x）：G10 / G11 / G4 / G5 / G2 / G13(构建自证) ──
# ★ 这些闸门全是文本断言，只能抓"忘了接/接回去了"，抓不到"接了但传错变量"。
#   语义部分由上面三个纯 JVM 自测（CalibMerge / CalibGate / GuardSelfTest）承担。
if grep -qF "k_used=" "$SRC_MAIN/PlayService.java" \
   && grep -qF "lead_used=" "$SRC_MAIN/PlayService.java" \
   && grep -qF "ver=" "$SRC_MAIN/PlayService.java"; then
  echo "  ✓ G10：trace 头写明本轮用的 k/lead/c/构建"
else
  echo "  ✗ G10：trace 头缺 k_used/lead_used/ver ⇒ 事后仍要回界面日志猜参数"; gate_fail=1
fi
if grep -qF "appendTraceNote(job" "$SRC_MAIN/PlayService.java" \
   && grep -qF "appendTraceNote(job, \"calib \"" "$SRC_MAIN/PlayService.java" \
   && grep -qF "appendTraceNote(job, String.format" "$SRC_MAIN/PlayService.java"; then
  echo "  ✓ G10：注入后才算出的 K/lead 走 trace 尾部追加（不伪造头部时序）"
else
  echo "  ✗ G10：K/lead 的实测值没进 trace 尾部 ⇒ 又只能靠界面截图取证"; gate_fail=1
fi
if grep -qF "tailNotes" "$INJ_MAIN/sched/InjectTrace.java" \
   && grep -qF "public void addTailNote(" "$INJ_MAIN/sched/InjectTrace.java"; then
  echo "  ✓ G10：InjectTrace 支持收尾注释行（明细之后、以 # 开头）"
else
  echo "  ✗ G10：InjectTrace 没有 addTailNote ⇒ 收尾值无处可落"; gate_fail=1
fi

if grep -qF "private View paramBanner(" "$SRC_MAIN/MainActivity.java" \
   && grep -qF "content.addView(paramBanner(s));" "$SRC_MAIN/MainActivity.java"; then
  echo "  ✓ G11：开火页有参数体检横幅"
else
  echo "  ✗ G11：体检横幅没挂到开火页（或只写了函数没 insert）"; gate_fail=1
fi
# 横幅必须真的显示样本数与指纹结论，否则"有值但不可信"还是看不出来
if grep -qF "n=%d" "$SRC_MAIN/MainActivity.java" \
   && grep -qF "设备指纹不符" "$SRC_MAIN/MainActivity.java"; then
  echo "  ✓ G11：横幅带样本数与指纹不符提示"
else
  echo "  ✗ G11：横幅缺样本数/指纹提示 ⇒ 横幅会变成「看着平安」的假绿"; gate_fail=1
fi

# ── K 的人工入口（2026-10-30 用户要求）──
#   K 以前只有标定/自动标定能写，界面一个写入口都没有 ⇒ 标定时游戏里放错歌、
#   测出一个错配的 K，就**永远清不掉**（只能连打 3 轮撞 CalibMerge 的"连续同侧重置"）。
if grep -qF 'private void showKDialog(SongLib.Song s)' "$SRC_MAIN/MainActivity.java" \
   && grep -qF 'private void showKLeadDialog(SongLib.Song s)' "$SRC_MAIN/MainActivity.java" \
   && grep -qF '        tvK.setOnClickListener(v -> {' "$SRC_MAIN/MainActivity.java" \
   && grep -qF 'edit.setOnClickListener(v -> showKLeadDialog(s));' "$SRC_MAIN/MainActivity.java"; then
  echo "  ✓ K 值人工入口：顶部 K 可改/可清 + 课题页「改」同时改 K 与 lead"
else
  echo "  ✗ K 的人工入口缺失 ⇒ 测错的 K 又清不掉（只能连打 3 轮硬顶）"; gate_fail=1
fi
# ⚠ 反向断言：参数体检那块**只读**（用户 2026-10-30 明确要求去掉点击）。
# 2026-11-01：那个打印函数本身也删了（连同「服务侧探针」按钮），所以这里改成双钉：
#   ① 参数体检不许再挂点击；② 那个函数不许回来（回来了就等于把一堆 keyboard=value 塞回日志）。
if grep -qF 't.setOnClickListener(v -> dumpCalibPrefs())' "$SRC_MAIN/MainActivity.java" \
   || grep -qF 'private void dumpCalibPrefs()' "$SRC_MAIN/MainActivity.java"; then
  echo "  ✗ 参数体检又变回可点 / 或 dumpCalibPrefs 又回来了 ⇒ 几十行 key=value 会挤爆日志"; gate_fail=1
else
  echo "  ✓ 参数体检只读，且不再有「把全部 prefs 打出来」的入口"
fi

if grep -qF "CalibGate.checkGap(ms, ff)" "$SRC_MAIN/PlayService.java" \
   && grep -qF "CalibGate.checkCross(sMs, leadOrNull(song))" "$SRC_MAIN/PlayService.java"; then
  echo "  ✓ G4/G5：标定连续性 + 交叉校验都接在 computeKOnDevice 里"
else
  echo "  ✗ G4/G5：标定闸门没接线（或只写了常量）⇒ 中断捕获/错锁样本照样写 K"; gate_fail=1
fi
# 阈值是这个项目最容易"顺手放宽"的东西：80 帧 / 150 ms（+c 偏移修掉后从 300 收紧）/ 3 s 窗
if grep -qF "GAP_MIN_FRAMES = 80" "$SRC_MAIN/CalibGate.java" \
   && grep -qF "CROSS_TOL_MS = 150.0" "$SRC_MAIN/CalibGate.java" \
   && grep -qF "GAP_WINDOW_FRAMES = 300" "$SRC_MAIN/CalibGate.java"; then
  echo "  ✓ G4/G5：阈值仍是 0.8 s（80 帧）/ 3 s 窗 / 150 ms（S≈lead，不带 +c）"
else
  echo "  ✗ G4/G5：阈值被改了（应为 80 帧 / 300 帧窗 / 150 ms）"; gate_fail=1
fi

if grep -qF "checkFingerprint();" "$SRC_MAIN/PlayService.java" \
   && grep -qF "private float kStored(" "$SRC_MAIN/PlayService.java" \
   && grep -qF "private float cUsed(" "$SRC_MAIN/PlayService.java"; then
  echo "  ✓ G2：设备指纹在 prepare 早期核对，K/c 都走作废后的取值"
else
  echo "  ✗ G2：指纹核对接线缺失（旧设备的 K/c 会被继续用）"; gate_fail=1
fi
if grep -qF "if (fpMismatch) return Double.NaN;" "$SRC_MAIN/PlayService.java" \
   && grep -qF "if (fpMismatch) return Float.NaN;" "$SRC_MAIN/PlayService.java"; then
  echo "  ✓ G2：指纹不符时 lead/K 一律作废（不许拿旧设备的量去预派）"
else
  echo "  ✗ G2：指纹不符仍会返回旧 K/lead ⇒ 换了设备被打成整曲偏"; gate_fail=1
fi

if grep -qF 'version-name "$VERSION"' "$HERE/build.sh" \
   && grep -qF 'build_info.txt' "$HERE/build.sh" \
   && grep -qF 'rev-parse --short HEAD' "$HERE/build.sh"; then
  echo "  ✓ G13：versionName 带 git rev，并写 assets/build_info.txt"
else
  echo "  ✗ G13：版本号仍是写死的 ⇒ 装到手机上认不出是哪一版（19:0x 回退事故）"; gate_fail=1
fi
if grep -qF 'getAssets().open("build_info.txt")' "$SRC_MAIN/MainActivity.java" \
   && grep -qF 'getAssets().open("build_info.txt")' "$SRC_MAIN/PlayService.java"; then
  echo "  ✓ G13：界面与 trace 都读同一份构建信息"
else
  echo "  ✗ G13：构建信息没接到界面/trace（写了文件也没人看）"; gate_fail=1
fi

# ── UI 结构（2026-10-25 19:5x 用户要求：开火页只留操作，教程收进左侧抽屉）──
if grep -qF "private void renderHelp()" "$SRC_MAIN/MainActivity.java" \
   && grep -qF "case STEP_HELP: renderHelp();" "$SRC_MAIN/MainActivity.java"; then
  echo "  ✓ 教程/帮助是独立页（STEP_HELP 走 renderHelp）"
else
  echo "  ✗ 教程/帮助没做成独立页（又塞回某一步里了）"; gate_fail=1
fi
if grep -qF "private LinearLayout buildDrawer()" "$SRC_MAIN/MainActivity.java" \
   && grep -qF "private void toggleDrawer(boolean open)" "$SRC_MAIN/MainActivity.java" \
   && grep -qF 'tv("☰"' "$SRC_MAIN/MainActivity.java"; then
  echo "  ✓ 左侧抽屉 + ☰ 入口都在"
else
  echo "  ✗ 左侧抽屉/☰ 入口缺失（教程页就没有入口了）"; gate_fail=1
fi
if grep -qF 'drawerItem("教程 / 帮助"' "$SRC_MAIN/MainActivity.java"; then
  echo "  ✓ 抽屉里有「教程 / 帮助」入口"
else
  echo "  ✗ 抽屉里没有教程入口"; gate_fail=1
fi
# ★ 顺序闸门：这两段教程文字必须**只出现在 renderHelp 定义之后**（搬回开火页就会红）
# ⚠ 每个 grep 都要 `|| true`：本脚本是 `set -euo pipefail`，grep 找不到词会返回 1，
#   直接赋值就会把整个脚本静默打断（2026-10-25 自己踩了一次：之后 5 条闸门全没跑）。
_help_line=$(grep -n "private void renderHelp()" "$SRC_MAIN/MainActivity.java" | head -1 | cut -d: -f1 || true)
_tut_line=$(grep -n "① 先在 Phigros" "$SRC_MAIN/MainActivity.java" | head -1 | cut -d: -f1 || true)
_tut_n=$(grep -cF "① 先在 Phigros" "$SRC_MAIN/MainActivity.java" || true)
if [ -n "$_help_line" ] && [ "$_tut_n" = "1" ] && [ -n "$_tut_line" ] && [ "$_tut_line" -gt "$_help_line" ]; then
  echo "  ✓ ①②③ 教程只在帮助页（不在开火页）"
else
  echo "  ✗ ①②③ 教程回到了开火页或被复制成两份（判定性：行号 $_tut_line vs 帮助页 $_help_line，份数 $_tut_n）"
  gate_fail=1
fi
# 开火页正文（renderRun 到 renderHelp 之间）不许再出现 tip( 说明块
if [ -n "$_help_line" ]; then
  _run_tips=$(awk '/private void renderRun\(/,/private void renderHelp\(/' "$SRC_MAIN/MainActivity.java" | grep -c 'addView(tip(' || true)
  if [ "$_run_tips" = "0" ]; then
    echo "  ✓ 开火页不再堆教程式长文本（addView(tip( 计数 0）"
  else
    echo "  ✗ 开火页又堆了 $_run_tips 段 tip(...) 长文本"; gate_fail=1
  fi
fi

# ── G1 全覆盖（2026-10-25 20:1x 用户指定「按建议修」）：写盘只许走 PrefGuard ──
# 背景：G1 先前只落在 PlayService，界面侧 4 处仍是 apply()（c / 手填 lead / 清除 lead / 旧 K 迁移），
#       恰好是「用户手改参数」这条最容易丢写的路径。
if ! grep -q '\.apply()' "$SRC_MAIN/PlayService.java" && ! grep -q '\.apply()' "$SRC_MAIN/MainActivity.java"; then
  echo "  ✓ G1：Service 与 Activity 都没有 apply()（写盘一律 commit + 回读）"
else
  echo "  ✗ G1：又出现 apply() ⇒ 这些写点会被 force-stop 静默丢写："
  grep -n '\.apply()' "$SRC_MAIN/PlayService.java" "$SRC_MAIN/MainActivity.java" | sed 's/^/      /'
  gate_fail=1
fi
if grep -q 'PrefGuard\.putFloat' "$SRC_MAIN/PlayService.java" \
   && grep -q 'PrefGuard\.putFloat' "$SRC_MAIN/MainActivity.java"; then
  echo "  ✓ G1：PrefGuard 是唯一实现，Service 与 Activity 都走它"
else
  echo "  ✗ G1：有一侧没走 PrefGuard ⇒ 两处口径会分叉"; gate_fail=1
fi


# ── G10 补口：零点参数必须在 finish() 之后追加进 trace 尾部（预派路径尤其）──
if grep -qF "private void appendZeroMeta(" "$SRC_MAIN/PlayService.java" \
   && [ "$(grep -cF 'appendZeroMeta(job);' "$SRC_MAIN/PlayService.java")" -ge 3 ]; then
  echo "  ✓ G10：零点参数走尾部追加（普通路径 + 预派两条分支都接了）"
else
  echo "  ✗ G10：零点参数没在三条路径上全接（预派轮的 trace 头又会缺 onset/sil_ref）"; gate_fail=1
fi
if grep -qF 'appendTraceNote(job, "zero " + m)' "$SRC_MAIN/PlayService.java"; then
  echo "  ✓ G10：零点尾部行的格式是 '# zero src=… onset_ms=…'"
else
  echo "  ✗ G10：零点尾部行格式不对（下游按 '# zero' 认）"; gate_fail=1
fi

# ── MOVE 抽稀必须**按指针**、且只抽 MOVE（2026-10-25 20:5x：moonlight 丢 3 黄键的根因之一）──
if grep -qF "lastMoveByPid" "$INJ_MAIN/DevRun.java" \
   && grep -qF "lastMoveByPid.put(c.pid(), c.tMs())" "$INJ_MAIN/DevRun.java" \
   && ! grep -qF "c.tMs() - lastMoveDueMs < moveGapMs()" "$INJ_MAIN/DevRun.java"; then
  echo "  ✓ 抽稀按指针独立（两只手同毫秒各动一次时，第二只不会再被吞掉）"
else
  echo "  ✗ 抽稀又用回全局时间戳 ⇒ 多指同刻的 MOVE 会被丢，Drag 会偶发整批 Miss"; gate_fail=1
fi
if grep -qF "if (Gesture.masked(f.action()) == Gesture.ACTION_MOVE) {" "$INJ_MAIN/DevRun.java"; then
  echo "  ✓ 抽稀只作用于 MOVE（DOWN/UP/POINTER_* 绝不会被抽掉）"
else
  echo "  ✗ 抽稀没有限定 MOVE ⇒ 极端情况下会吞掉按下/抬起"; gate_fail=1
fi

if grep -qF "public Frame keepalive(double tMs)" "$INJ_MAIN/sched/Gesture.java" \
   && grep -qF "Gesture.Frame kf = g.keepalive(t);" "$INJ_MAIN/DevRun.java" \
   && grep -qF "n_keepalive=%d" "$SRC_MAIN/PlayService.java"; then
  echo "  ✓ 静默保活：空隙里补同位置 MOVE，且条数进 trace（n_keepalive）"
else
  echo "  ✗ 静默保活没接全（长静默里的音符会继续丢）"; gate_fail=1
fi

# ── 盲区文案不许再写「必丢 / 打不出 φ」（2026-10-25 真机证伪：moonlight blind=4 仍 φ）──
if grep -qF '没有 lead 时会丢' "$SRC_MAIN/MainActivity.java" \
   && ! grep -qF '打不出 φ' "$SRC_MAIN/MainActivity.java"; then
  echo "  ✓ 首音盲区文案已改成「没有 lead 时才会丢」"
else
  echo "  ✗ 首音盲区文案还在说「必丢/打不出 φ」⇒ 与 moonlight 的 φ 矛盾"; gate_fail=1
fi

# ── lead 事后对账必须真的接在标定流程里（2026-10-25 真机：同曲两轮 lead 差 503 ms）──
if grep -qF 'LeadAudit.decide(' "$SRC_MAIN/PlayService.java" \
   && grep -qF 'auditLead(song, sMs)' "$SRC_MAIN/PlayService.java" \
   && grep -qF 'lead_audit=' "$SRC_MAIN/PlayService.java"; then
  echo "  ✓ lead 事后对账已接线（样本坏回滚 / 均值坏重置，结果进 trace）"
else
  echo "  ✗ lead 事后对账没接线 ⇒ 500 ms 级别的坏 lead 样本仍会进均值"; gate_fail=1
fi

# ── 10 指压力标记必须打通「打包 → manifest → SongLib → UI」（2026-10-26）──
if grep -qF '"pointer_peak": pointer_peak(' tools/pack_assets.py \
   && grep -qF 'pointerPeak' "$SRC_MAIN/SongLib.java" \
   && grep -qF 's.pointerPeak >= 9' "$SRC_MAIN/MainActivity.java"; then
  echo "  ✓ 10 指压力标记已贯通（pack_assets → manifest → SongLib → UI）"
else
  echo "  ✗ 10 指压力标记断链 ⇒ 高压谱面不会给出结构限制提示"; gate_fail=1
fi

# ── 「已验 φ」的 ≥9 指谱面必须免打结构限制警告（2026-10-28）──
#    三处缺一不可：打包端清单 → manifest 字段 → 端内解析 → UI 判据取反
if grep -qF 'TEN_FINGER_VERIFIED' tools/pack_assets.py \
   && grep -qF '"ten_finger_ok": True' tools/pack_assets.py \
   && grep -qF 'ten_finger_ok' "$SRC_MAIN/SongLib.java" \
   && grep -qF '!s.tenFingerOk' "$SRC_MAIN/MainActivity.java"; then
  echo "  ✓ 已验 φ 的 10 指谱面免警告已贯通（TEN_FINGER_VERIFIED → manifest → SongLib → UI）"
else
  echo "  ✗ 免警告链路断链 ⇒ 已 AP 的曲子还会被标「结构限制」"; gate_fail=1
fi

# ── Shizuku 未连接时必须能一键跳到 Shizuku（2026-10-26：被杀后要手动拉起，别再翻设置）──
if grep -qF 'moe.shizuku.privileged.api' "$SRC_MAIN/MainActivity.java"    && grep -qF 'getLaunchIntentForPackage' "$SRC_MAIN/MainActivity.java"    && grep -qF '重启或被杀后都要重新拉起' "$SRC_MAIN/MainActivity.java"; then
  echo "  ✓ Shizuku 未连接时可一键跳转（文案也说明了「被杀」这一档）"
else
  echo "  ✗ Shizuku 断线时没有跳转入口 ⇒ 用户只能自己翻设置"; gate_fail=1
fi

# ── 真注入前必须先查 Shizuku 授权（2026-10-26 真机：没这层检查 ⇒ 服务白起一轮）──
# 证据：`PgPlay` 里「==== 本轮开始」→ 90 ms 后「Shizuku 门禁未过：perm=-1」，
#   用户看到的是"点了开始演奏但没打起来"，容易被当成"要按两次"的怪 bug。
if grep -qF 'if (!ensureShizukuPermission()) return;' "$SRC_MAIN/MainActivity.java" \
   && grep -qF 'private boolean ensureShizukuPermission()' "$SRC_MAIN/MainActivity.java"; then
  echo "  ✓ 真注入前先查 Shizuku 授权（未授权不启动服务）"
else
  echo "  ✗ 真注入前没查 Shizuku 授权 ⇒ 服务会白起一轮"; gate_fail=1
fi

# ── 课题模式会话：入口/复用 + 唤醒锁（2026-10-27）──
if grep -qF 'public static final String EXTRA_COURSE' "$SRC_MAIN/PlayService.java" \
   && grep -qF 'private void runCourse(' "$SRC_MAIN/PlayService.java" \
   && grep -qF 'tap.setSessionMode(true)' "$SRC_MAIN/PlayService.java" \
   && grep -qF 'COURSE_WAKE_MS' "$SRC_MAIN/PlayService.java"; then
  echo "  ✓ 课题模式已接线（EXTRA_COURSE → runCourse → 会话复用）+ 900 s WakeLock"
else
  echo "  ✗ 课题模式没接上（入口/会话/唤醒锁 任一缺失）"; gate_fail=1
fi

# ── 课题模式失败路径：没有零点必须中止（2026-10-28 复盘出的真 bug）──
#    判据 = 「本曲零点到底有没有建立」：没建立 ⇒ 下一首下界不可知 ⇒ 中止整场；
#    已建立（注入中途失败）⇒ zero+音乐总时长仍然成立 ⇒ 继续。
#    ⛔ 别改回「失败一律 continue」：那会让闸门锁到本曲尾音，把下一首整首打废。
if grep -qF 'CourseBound.nextBaseAfterFailure' "$SRC_MAIN/PlayService.java" \
   && grep -qF 'double zeroEpoch = Double.NaN;' "$SRC_MAIN/PlayService.java" \
   && grep -qF 'Double.isNaN(nextBase)' "$SRC_MAIN/PlayService.java" \
   && grep -qF 'io/pgauto/play/CourseBound' tools/check_play_apk.py; then
  echo "  ✓ 课题模式失败路径按「零点是否建立」分流（没锚点必须中止，有锚点才能继续）"
else
  echo "  ✗ 课题模式失败路径又变成静默继续 ⇒ 跳曲后会把下一首打废"; gate_fail=1
fi

# ── 课题模式中止：原因必须落盘 + 弹 Toast（2026-10-28 诊断轮暴露的两个缺口）──
#    落盘 = 事后查得到（course sidecar 的 `# aborted:` 行）；Toast = 当时看得见。
# ★ 末两条是「首数只许补一次」的成对断言。2026-10-29 真机 sidecar 实测出现过
#   「（已完成 1 首）」重复两次 —— 异常消息里写了一遍、外层 appendCourseAbort 又补一遍。
#   ⚠ 负向那条**必须**用一个在源码文本里**连续存在**的串：异常消息是跨行的字符串拼接
#     （`+ " ⇒ 中止整场"` / `+ "（游戏不会停…"`），所以「中止整场（已完成」这种
#     "拼起来才成立"的串在源码里根本不存在 ⇒ grep 恒不命中 ⇒ 断言空转（第一版就是这么错的）。
#     用 `首；游戏不会停` —— 它只在「已完成 N 首；游戏不会停…」那一行里连续出现。
if grep -qF 'appendCourseAbort("中止："' "$SRC_MAIN/PlayService.java" \
   && grep -qF 'CourseAbort.csvLine(' "$SRC_MAIN/PlayService.java" \
   && grep -qF 'CourseAbort.toastText(' "$SRC_MAIN/PlayService.java" \
   && grep -qF 'io/pgauto/play/CourseAbort' tools/check_play_apk.py \
   && grep -qF '（已完成 " + done + " 首）' "$SRC_MAIN/PlayService.java" \
   && ! grep -qF '首；游戏不会停' "$SRC_MAIN/PlayService.java"; then
  echo "  ✓ 课题模式中止：原因写进 course sidecar（# aborted:）+ 弹 Toast（首数只补一次）"
else
  echo "  ✗ 课题模式中止又变回「只有端内日志」或首数重复 ⇒ 事后判不出原因、当时也看不见"; gate_fail=1
fi

# ── 课题模式 K 事后审计（2026-10-29）：δ ≡ K_stored − K_round，每首必须落盘 ──
#   为什么：课题模式的 Good **全部**来自 δ（整首零点偏移），而 `onset = sMs + K`、
#   `K = eMs − sMs` ⇒ δ ≡ K_stored − K_round（代数恒等）。课题模式从不调 computeKOnDevice
#   ⇒ 连续 5 轮 k_*.txt 一个都没有，δ 抖动来源完全无记录。
#   ⚠ 只断言"存在 auditKRound 调用"是**空转**：把它放在 inject 之前就等于用错的起音帧号。
#     所以这里额外断言**行号顺序** ff → inject → audit（这才是真正的契约）。
LINE_FF=$(grep -n 'final long ffThisSong' "$SRC_MAIN/PlayService.java" | head -1 | cut -d: -f1)
LINE_INJ=$(grep -n 'inject(job, zeroEpoch);' "$SRC_MAIN/PlayService.java" | head -1 | cut -d: -f1)
LINE_AUD=$(grep -nF 'auditKRound(job.song, tap, i, ffThisSong, ffAtNs)' "$SRC_MAIN/PlayService.java" | head -1 | cut -d: -f1)
if grep -qF 'kaudit_' "$SRC_MAIN/PlayService.java" \
   && grep -qF 'delta_pred' "$SRC_MAIN/PlayService.java" \
   && grep -qF 'EnvMatcher.match(rec, ref, -1, 100)' "$SRC_MAIN/PlayService.java" \
   && grep -qF 'final int ffRel = ' "$SRC_MAIN/PlayService.java" \
   && grep -qF 'copyOfRange(recFull, start, end)' "$SRC_MAIN/PlayService.java" \
   && grep -qF 'writeKauditSkip(' "$SRC_MAIN/PlayService.java" \
   && [ -n "$LINE_FF" ] && [ -n "$LINE_INJ" ] && [ -n "$LINE_AUD" ] \
   && [ "$LINE_FF" -lt "$LINE_INJ" ] && [ "$LINE_INJ" -lt "$LINE_AUD" ]; then
  echo "  ✓ 课题模式 K 事后审计：每首落盘 kaudit（帧号在 inject 前取、审计在 inject 后、帧号用 ffRel、rec 截 30s、不传先验）"
else
  echo "  ✗ 课题模式 K 审计缺失/顺序错（ff/inject/audit = ${LINE_FF:-无}/${LINE_INJ:-无}/${LINE_AUD:-无}）"; gate_fail=1
fi
# ⛔ 三条**反向**断言（每一条都对应一次真机翻车，别再犯）：
#   ① `center = ff − silRef`（正值）—— 2026-10-29 我"修"过一次，离线实测 s_ms 错 14.5 s、cc 0.782。
#      EnvMatcher 期望的 lag 是负的，而它只在 center ≥ 0 时启用先验 ⇒ 这个参数表达不了负 lag。
#      正解是**不传先验**（-1）+ 截短 rec。标定那处的 `silRef − ff` 保持原样（它等于放弃先验）。
#   ② 直接用 `gate().firedFrame()` 当本首帧号 —— 它是检测器累计值，不随 rearm 清零
#      （第 2 首实测 ff=19538 > frames=16913）。
#   ③ 把整首 envelope 直接喂 match —— 母带指纹只有 3000 帧，n/4 会超过它 ⇒ 必报"先验窗越界"。
if grep -qF 'final int center = (int) ff - silRefFrames;' "$SRC_MAIN/PlayService.java"; then
  echo "  ✗ audit 又用了 center = ff − silRef（正值）⇒ EnvMatcher 表达不了负 lag，实测错 14.5 s"; gate_fail=1
fi
if grep -qF 'EnvMatcher.match(recFull, ref,' "$SRC_MAIN/PlayService.java"; then
  echo "  ✗ audit 又把整首包络喂进 match ⇒ n/4 > 母带 3000 帧 ⇒ 必报先验窗越界"; gate_fail=1
fi
if grep -qF 'auditKRound(job.song, tap, i, ffThisSong);' "$SRC_MAIN/PlayService.java"; then
  echo "  ✗ auditKRound 少了 ffAtNs 参数 ⇒ 会退回用累计帧号"; gate_fail=1
fi

# ── 课题模式必须是**独立页**（2026-10-27 用户要求：放抽屉里，不碰选曲/难度/开火）──
if grep -qF 'drawerItem("课题模式"' "$SRC_MAIN/MainActivity.java"    && grep -qF 'case STEP_COURSE: renderCourse(); break;' "$SRC_MAIN/MainActivity.java"    && grep -qF 'private void renderCourse()' "$SRC_MAIN/MainActivity.java"; then
  echo "  ✓ 课题模式是独立页（抽屉进入；选曲/难度/开火三页未动）"
else
  echo "  ✗ 课题模式没做成独立页（或又塞回某个已有页面）"; gate_fail=1
fi
if grep -qF 'readAsset("courses.json")' "$SRC_MAIN/MainActivity.java"    && ! grep -qF '"schneid"' "$SRC_MAIN/MainActivity.java"; then
  echo "  ✓ 课题预设来自 assets/courses.json（数据），没有写死在代码里"
else
  echo "  ✗ 预设被硬编码进代码，或没读 assets"; gate_fail=1
fi

# ── 课题会话的两个实测 bug 不许重演（2026-10-27 20:58 事故）──
# 零点来源：gate 负责"毫秒级"，互相关只做事后审计（2026-10-27 两次真机的结论）
if grep -qF 'tap.awaitFire(notBeforeNs, (int) remain, i > 0)' "$SRC_MAIN/PlayService.java"    && grep -qF '起音在点后 %.0f ms（单曲 lead=%.0f 仅供参考）' "$SRC_MAIN/PlayService.java"    && ! grep -qF 'Math.abs(fireMs - want) <= 2500' "$SRC_MAIN/PlayService.java"; then
  echo "  ✓ 会话零点用 gate（毫秒级），且**不再用 lead 判断曲首**"
else
  echo "  ✗ 会话零点路径退化（又用 lead 猜 / 改回慢路径）"; gate_fail=1
fi
if grep -qF 'public static Hit locate(' "$SRC_MAIN/CourseStart.java"    && grep -qF 't06_threeSongSequence' "$SRC_TEST/CourseStartSelfTest.java"; then
  echo "  ✓ 互相关定曲首作为**离线可测**的审计工具保留（t01~t06）"
else
  echo "  ✗ CourseStart/其自测丢了"; gate_fail=1
fi
# forceArm 现在是"原语"，产品路径不再依赖它（第 2/3 首改走互相关）——
# 但类里必须保留它，且离线自测钉住它的语义（t23/t24）。
if grep -qF 'public void forceArm()' "$SRC_MAIN/OnsetGate.java"    && grep -qF 't23_forceArmBypassesQuiet' "$SRC_TEST/OnsetGateSelfTest.java"    && ! grep -qF '强制待命再等一次' "$SRC_MAIN/PlayService.java"; then
  echo "  ✓ forceArm 只是原语（产品不再依赖它；语义由 t23/t24 钉住）"
else
  echo "  ✗ forceArm 的使用关系退化（产品或自测有一边不对）"; gate_fail=1
fi

# ── 曲名搜索必须忽略空白（2026-10-26：曲库里真有名字带空格的，导致"搜不到这首歌"）──
if grep -qF 'private static String normForSearch(String s)' "$SRC_MAIN/MainActivity.java" \
   && grep -qF 'normForSearch(g.name).contains(q)' "$SRC_MAIN/MainActivity.java"; then
  echo "  ✓ 曲名搜索忽略空白/大小写（名字带空格的曲子也能按 key 搜到）"
else
  echo "  ✗ 搜索退回整串 contains ⇒ 名字里带空格的曲子永远搜不到"; gate_fail=1
fi

# ── 选曲页「回到最上方」按钮（2026-10-26 用户要求，334 首靠滑太累）──
if grep -qF 'topBtn' "$SRC_MAIN/MainActivity.java"    && grep -qF 'smoothScrollTo(0, 0)' "$SRC_MAIN/MainActivity.java"; then
  echo "  ✓ 选曲/难度页有「回到最上方」悬浮按钮"
else
  echo "  ✗ 缺「回到最上方」按钮"; gate_fail=1
fi

# ── G5 交叉校验放宽：|k| 在物理范围内就采纳（并打 cross_relaxed=1）；xcorr 拒绝要带 cc/质量 ──
if grep -qF 'crossRelaxed' "$SRC_MAIN/PlayService.java"    && grep -qF 'cross_relaxed=1' "$SRC_MAIN/PlayService.java"    && grep -qF 'gate=xcorr cc=%.3f quality=%.2f' "$SRC_MAIN/PlayService.java"; then
  echo "  ✓ G5 允许「音频自洽但 lead 不符」的样本 + xcorr 拒绝带 cc/质量"
else
  echo "  ✗ G5 放宽或 xcorr 诊断信息缺失"; gate_fail=1
fi

# ── 互相关的「弱峰第二档」必须存在（2026-10-26：两首卡在 quality/cc 差一点）──
if grep -qF 'boolean weak = !m.ok' "$SRC_MAIN/PlayService.java"    && grep -qF 'weak=1' "$SRC_MAIN/PlayService.java"    && grep -qF 'cc >= 0.70 && m.quality >= 1.00' "$SRC_MAIN/PlayService.java"; then
  echo "  ✓ 互相关有「弱峰但 K 合理就采纳」的第二档（并标 weak=1）"
else
  echo "  ✗ 缺弱峰第二档 ⇒ cc/quality 差一点点的曲子永远标不上 K"; gate_fail=1
fi

# ── 迟到 >40 ms 的 MOVE 必须止损（且关键事件绝不许丢）──
if grep -qF 'MOVE_LATE_DROP_MS' "$INJ_MAIN/DevRun.java"    && grep -qF 'Gesture.masked(f.action()) == Gesture.ACTION_MOVE) {' "$INJ_MAIN/DevRun.java"; then
  echo "  ✓ 迟到 >40 ms 的 MOVE 会止损（burst 饱和时不让队列越拖越长）"
else
  echo "  ✗ 缺 MOVE 迟到止损 ⇒ 密集段会一路晚下去"; gate_fail=1
fi

# ── 单曲预置 K（只给"自动标定测不出"的那首歌兜底；不许做成"每曲都要填"）──
if grep -qF 'K_PRESET_MS' tools/pack_assets.py    && grep -qF 'k_preset_ms' tools/pack_assets.py    && grep -qF 'kPresetMs' "$SRC_MAIN/SongLib.java"    && grep -qF '预置 K=' "$SRC_MAIN/PlayService.java"; then
  echo "  ✓ 单曲预置 K 已贯通（manifest → SongLib → L_dev 兜底）"
else
  echo "  ✗ 单曲预置 K 断链"; gate_fail=1
fi

[ "$gate_fail" -eq 0 ] || { echo "源码闸门失败 $gate_fail 项" >&2; exit 1; }

# 源码闸门：互相关零点必须与 gate 用同一套 trim（2026-10-28 12:4x 实测：漏了就把全曲推后 35~54 ms）
if grep -qF 'startRezeroWatcher(job, tap, elapsed0, epoch0, zeroEpoch, trim, i);' "$SRC_MAIN/PlayService.java"    && grep -qF -- '- job.lDevS * 1000.0 - trimMs - REZERO_CORR_BIAS_MS;' "$SRC_MAIN/PlayService.java"; then
  echo "  ✓ rezero 的相关零点也减 trim（只修本轮抖动，不动标定基线）"
else
  echo "  ✗ 相关零点没减 trim ⇒ rezero 会把全曲推后 35~54 ms（实测会退化成全 Late）"; gate_fail=1
fi

# 源码闸门：每条"跑"的路径都必须清 running 标记（2026-10-28 15:1x：课程漏清 ⇒ 界面永远"运行中"）
# 2026-11-01：探针路径删了（`EXTRA_PROBE` 去掉了），只剩单曲 / 课程两条 ⇒ 阈值 3 改 2。
_n=$(grep -c "RunLog.setRunning(false)" "$SRC_MAIN/PlayService.java")
if [ "$_n" -ge 2 ] && grep -qF 'RunLog.setRunning(false);' "$SRC_MAIN/PlayService.java"; then
  echo "  ✓ 两条运行路径（单曲/课程）都清 running 标记（_n=$_n）"
else
  echo "  ✗ 有运行路径没清 running 标记（_n=$_n，应 >=2）⇒ 界面会永远显示「运行中」"; gate_fail=1
fi
