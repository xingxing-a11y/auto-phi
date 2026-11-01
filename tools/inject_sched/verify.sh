#!/usr/bin/env bash
# 端内「脑」的交叉验证：Python 参考实现 vs Java 纯逻辑实现，逐行 diff。
#
# 为什么两处实现：项目规矩「同一个量两处实现 ⇒ 交叉断言」。
#   只跑一份实现 = 没测（它自己就是标准）。
#   Java 版就是**端内真正跑的那份**（android/injector/app/.../sched/）；
#   Python 版复用生产 ScreenMap。任一方算错，diff 必然抓到。
#
# 2026-10-21 起 Java 侧的**唯一副本**搬进端内源码树（不再 tools/ 下一份、app 里一份），
# 本脚本编译的就是端内那份 ⇒ 测的和跑的是同一个文件。
#
# 用法（Git Bash，cwd = 仓库根）：
#   bash tools/inject_sched/verify.sh
#
# 产物：tmp/_s1_verify/（tmp/_* 不入库）
set -uo pipefail

JDK="${JDK_HOME:-D:/OpenJDK17_17.0.19_10}"
PY="${PY:-.venv/Scripts/python.exe}"
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
OUT="$ROOT/tmp/_s1_verify"
SRC="$ROOT/android/injector/app/src/main/java/io/pgauto/inject/sched"
CLS="io.pgauto.inject.sched.Scheduler"

[ -x "$JDK/bin/javac.exe" ] || { echo "✗ 缺 javac: $JDK/bin/javac.exe" >&2; exit 1; }
cd "$ROOT"
rm -rf "$OUT"; mkdir -p "$OUT/classes"

echo "[1/7] javac 端内 sched/*.java + GestureDump"
# ★ 用通配而不是逐个列文件：2026-10-21 新增 Densify.java 时列表漏了它，
#   javac 报「找不到符号」才发现 —— 列表式永远会漏，通配不会。
"$JDK/bin/javac.exe" -encoding UTF-8 -d "$OUT/classes" \
  "$SRC"/*.java tools/inject_sched/GestureDump.java || exit 1

FAIL=0
for KEY in demiurge kuangxi lacamp; do
  J="out/touches_$KEY.json"
  [ -f "$J" ] || { echo "  skip $KEY（缺 $J）"; continue; }

  echo "[2/7] $KEY：坐标变换 + 排序，两边各跑一遍"
  "$PY" tools/inject_sched/payload_ref.py "$J" > "$OUT/$KEY.py.tsv" 2>"$OUT/$KEY.py.err" || {
    echo "  ✗ Python 侧失败"; cat "$OUT/$KEY.py.err"; FAIL=1; continue; }
  "$JDK/bin/java.exe" -cp "$OUT/classes" "$CLS" "$J" 1610 720 height > "$OUT/$KEY.java.tsv" 2>"$OUT/$KEY.java.err" || {
    echo "  ✗ Java 侧失败"; cat "$OUT/$KEY.java.err"; FAIL=1; continue; }

  N_PY=$(wc -l < "$OUT/$KEY.py.tsv")
  N_JV=$(wc -l < "$OUT/$KEY.java.tsv")
  N_KEY=$(awk -F'\t' 'NR>1 && $8==1' "$OUT/$KEY.py.tsv" | wc -l)

  if diff -q "$OUT/$KEY.py.tsv" "$OUT/$KEY.java.tsv" >/dev/null; then
    echo "  OK  逐行一致：$((N_PY-1)) 条（关键 $N_KEY）"
  else
    echo "  ✗ 有差异（py $((N_PY-1)) 行 / java $((N_JV-1)) 行）"
    diff "$OUT/$KEY.py.tsv" "$OUT/$KEY.java.tsv" | head -20
    FAIL=1
  fi
done

echo "[3/7] 两级调度决策：两端逐条对拍（cost=0.5 / 3.27 两档）"
for KEY in demiurge kuangxi lacamp; do
  J="out/touches_$KEY.json"
  [ -f "$J" ] || continue
  for COST in 0.5 3.27; do
    "$PY" tools/inject_sched/sched_ref.py "$J" --cost "$COST" > "$OUT/$KEY.sched$COST.py.tsv" 2>/dev/null
    "$JDK/bin/java.exe" -cp "$OUT/classes" "$CLS" "$J" 1610 720 height --sched "$COST" \
      > "$OUT/$KEY.sched$COST.java.tsv" 2>/dev/null
    if diff -q "$OUT/$KEY.sched$COST.py.tsv" "$OUT/$KEY.sched$COST.java.tsv" >/dev/null; then
      NDROP=$(awk -F'\t' 'NR>1 && $4=="drop"' "$OUT/$KEY.sched$COST.py.tsv" | wc -l)
      echo "  OK  $KEY cost=$COST 逐条一致（丢 MOVE $NDROP 条）"
    else
      echo "  ✗ $KEY cost=$COST 有差异"
      diff "$OUT/$KEY.sched$COST.py.tsv" "$OUT/$KEY.sched$COST.java.tsv" | head -10
      FAIL=1
    fi
  done
done

echo "[4/7] 指针状态机：帧序列逐行对拍（MotionEvent 的 count/ids/actionIndex）"
for KEY in demiurge kuangxi lacamp; do
  J="out/touches_$KEY.json"
  [ -f "$J" ] || continue
  "$PY" tools/inject_sched/gesture_ref.py "$J" > "$OUT/$KEY.gest.py.tsv" 2>"$OUT/$KEY.gest.py.err" || {
    echo "  ✗ Python 侧失败"; cat "$OUT/$KEY.gest.py.err"; FAIL=1; continue; }
  "$JDK/bin/java.exe" -cp "$OUT/classes" io.pgauto.inject.sched.GestureDump "$J" 1610 720 height \
    > "$OUT/$KEY.gest.java.tsv" 2>"$OUT/$KEY.gest.java.err" || {
    echo "  ✗ Java 侧失败"; cat "$OUT/$KEY.gest.java.err"; FAIL=1; continue; }
  NG=$(($(wc -l < "$OUT/$KEY.gest.py.tsv") - 1))
  if diff -q "$OUT/$KEY.gest.py.tsv" "$OUT/$KEY.gest.java.tsv" >/dev/null; then
    echo "  OK  帧逐行一致：$NG 帧（$(grep -c 'warning\|⚠' "$OUT/$KEY.gest.java.err" || true) 条 java 告警）"
  else
    echo "  ✗ 帧序列有差异"
    diff "$OUT/$KEY.gest.py.tsv" "$OUT/$KEY.gest.java.tsv" | head -10
    FAIL=1
  fi
done

echo "[5/7] 不变量 B1~B8 + 变异（actionIndex 误用 pid / 抬手帧提前删指针）"
"$PY" tools/inject_sched/gesture_ref.py --selftest || FAIL=1

echo "[6/7] ★ 变异测试：故意改坏调度输入，diff 必须能发现"
# 变异 (a)：Java 用 fit=width（模拟「把 fit 写错」）⇒ 必须与 Python(height) 不一致
"$JDK/bin/java.exe" -cp "$OUT/classes" "$CLS" out/touches_demiurge.json 1610 720 width \
  > "$OUT/mut_fit.tsv" 2>/dev/null
if diff -q "$OUT/demiurge.py.tsv" "$OUT/mut_fit.tsv" >/dev/null; then
  echo "  ✗ 变异没被发现：fit=width 的输出与 height 一模一样 ⇒ 判据失效"
  FAIL=1
else
  echo "  OK  变异(a) fit=width 被 diff 抓到（$(diff "$OUT/demiurge.py.tsv" "$OUT/mut_fit.tsv" | grep -c '^<') 行不同）"
fi

# 变异 (b)：换 dst 尺寸（模拟「端内读错屏幕尺寸」）⇒ 必须不一致
"$JDK/bin/java.exe" -cp "$OUT/classes" "$CLS" out/touches_demiurge.json 1080 2340 height \
  > "$OUT/mut_dst.tsv" 2>/dev/null
if diff -q "$OUT/demiurge.py.tsv" "$OUT/mut_dst.tsv" >/dev/null; then
  echo "  ✗ 变异没被发现：换 dst 后输出相同 ⇒ 判据失效"
  FAIL=1
else
  echo "  OK  变异(b) dst=1080x2340 被 diff 抓到"
fi

# 变异 (c)：cost=0 与 cost=3.27 的决策必须不同（否则积压规则形同虚设）
"$PY" tools/inject_sched/sched_ref.py out/touches_lacamp.json --cost 0 > "$OUT/mut_cost0.tsv" 2>/dev/null
if diff -q "$OUT/mut_cost0.tsv" "$OUT/lacamp.sched3.27.py.tsv" >/dev/null; then
  echo "  ✗ 变异没被发现：cost=0 与 cost=3.27 决策相同 ⇒ 积压规则失效"
  FAIL=1
else
  echo "  OK  变异(c) cost=0 vs 3.27 决策不同（丢弃只由积压引起，非恒丢）"
fi

# 变异 (d)：帧没做「先出帧再删指针」⇒ 与正确序列必须不同
"$PY" tools/inject_sched/gesture_ref.py out/touches_demiurge.json > "$OUT/mut_gest_ok.tsv" 2>/dev/null
"$PY" - <<'PY' > "$OUT/mut_gest_bad.tsv"
import sys
sys.path.insert(0, "tools/inject_sched"); sys.path.insert(0, "tools")
import pathlib, gesture_ref, payload_ref
frames, _, _ = gesture_ref.run(payload_ref.build(pathlib.Path("out/touches_demiurge.json")),
                               mutate="delete_before_frame")
gesture_ref.dump_tsv(frames)
PY
if diff -q "$OUT/mut_gest_ok.tsv" "$OUT/mut_gest_bad.tsv" >/dev/null; then
  echo "  ✗ 变异(d) 没被发现：删指针时序改了但帧相同 ⇒ 判据失效"
  FAIL=1
else
  echo "  OK  变异(d) 抬手帧少一个指针被 diff 抓到"
fi

# 变异 (e)：取整方式 half-up（Math.round）应与 rint 在 .5 边界上分叉
"$PY" - "$ROOT" <<'PY'
import sys, pathlib, json, math
root = pathlib.Path(sys.argv[1])
sys.path.insert(0, str(root / "tools"))
import inject
sm = inject.ScreenMap((1920.0,1080.0),(1080.0,2340.0),(1610.0,720.0), fit="height")
d = json.loads((root/"out"/"touches_demiurge.json").read_text(encoding="utf-8"))
half = 0
for e in d["events"]:
    cx, cy = sm.to_canvas(float(e["x"]), float(e["y"]))
    dx, dy = sm.from_canvas(cx, cy)
    for v in (dx, dy):
        if abs(v - math.floor(v) - 0.5) < 1e-9:
            half += 1
print(f"  INFO 落在 .5 边界的坐标数 = {half}（>0 则 rint/round 的选择真的会影响结果）")
PY

echo "[7/7] ★ MOVE 加密（densify）专项：数量守恒 + 只插不改 + 粒度变异"
# 为什么单独一组：densify 是 dp1 轮 15 Miss 的根因（PC 默认开、端内没搬）。
# 光靠 [2/7] 的逐行对拍不够 —— **两边都关掉加密也会逐行一致**，那样测不出这一层缺失。
for KEY in demiurge kuangxi lacamp; do
  J="out/touches_$KEY.json"
  [ -f "$OUT/$KEY.java.tsv" ] || continue
  "$JDK/bin/java.exe" -cp "$OUT/classes" "$CLS" "$J" 1610 720 height --nodensify \
    > "$OUT/$KEY.nodense.tsv" 2>/dev/null
  N_D=$(($(wc -l < "$OUT/$KEY.java.tsv") - 1))
  N_N=$(($(wc -l < "$OUT/$KEY.nodense.tsv") - 1))
  N_NEW=$(awk -F'\t' 'NR>1 && $2==-1' "$OUT/$KEY.java.tsv" | wc -l)
  if [ "$N_D" -gt "$N_N" ] && [ "$N_NEW" -eq $((N_D - N_N)) ]; then
    echo "  OK  $KEY 加密 $N_N → $N_D（新点 $N_NEW = 差值 ⇒ 没有丢原事件）"
  else
    echo "  ✗ $KEY 加密数量不自洽：原 $N_N / 加密后 $N_D / 新点 $N_NEW"
    FAIL=1
  fi
  # 「只插不改」：剔除新点后必须与未加密版逐列相同（seq 列除外 —— 加密后要重新编号）
  if diff -q <(awk -F'\t' 'NR>1 && $2!=-1' "$OUT/$KEY.java.tsv" | cut -f2-) \
             <(awk -F'\t' 'NR>1' "$OUT/$KEY.nodense.tsv" | cut -f2-) >/dev/null; then
    echo "  OK  $KEY 剔除新点后与未加密版逐列相同 ⇒ 原有事件一字未动"
  else
    echo "  ✗ $KEY 加密改动了原有事件（不是「只插不改」）"
    FAIL=1
  fi
done

# 变异 (f)：STEP_MS 16.8 → 8.5（加密粒度改坏）⇒ 结果必须不同
MUT="$OUT/mut_dens"; rm -rf "$MUT"; mkdir -p "$MUT/src" "$MUT/cls"
cp "$SRC"/*.java "$MUT/src/"
"$PY" -c "import pathlib,sys; p=pathlib.Path(sys.argv[1]); s=p.read_text(encoding='utf-8'); assert 'STEP_MS = 16.8' in s, '变异锚点不在'; p.write_text(s.replace('STEP_MS = 16.8','STEP_MS = 8.5'), encoding='utf-8')" "$MUT/src/Densify.java"
"$JDK/bin/javac.exe" -encoding UTF-8 -d "$MUT/cls" "$MUT"/src/*.java 2>/dev/null
"$JDK/bin/java.exe" -cp "$MUT/cls" "$CLS" out/touches_demiurge.json 1610 720 height \
  > "$OUT/mut_dens_step.tsv" 2>/dev/null
NB_MUT=$(($(wc -l < "$OUT/mut_dens_step.tsv" 2>/dev/null || echo 0) - 1))
NB_REF=$(($(wc -l < "$OUT/demiurge.java.tsv") - 1))
if [ "$NB_MUT" -gt 0 ] && [ "$NB_MUT" -ne "$NB_REF" ]; then
  echo "  OK  变异(f) STEP_MS 16.8→8.5 被抓到（$NB_REF → $NB_MUT 条）"
else
  echo "  ✗ 变异(f) 没被抓到（参考 $NB_REF / 变异 $NB_MUT）⇒ 判据失效或变异没编译成功"
  FAIL=1
fi

echo
if [ "$FAIL" -eq 0 ]; then
  echo "全部通过（产物在 tmp/_s1_verify/）"
else
  echo "有失败项，见上"
fi
exit "$FAIL"
