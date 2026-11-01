#!/usr/bin/env bash
# 注入器 APK 的离线验收（不碰设备）。
#
# 查的都是「构建成功但运行时必然失败」的那类问题：
#   ① d8 有没有把 4 个 vendor jar 真的 dex 进去   → 漏了就是运行时 NoClassDefFoundError
#   ② manifest 里的 ShizukuProvider 还在不在      → 删了就是 pingBinder 永远 false 且无报错
#   ③ authority 是否符合 "<package>.shizuku"       → 不符则 binder 广播收不到
#   ④ 签名有效
#   ⑤ ★ 对照：从真 APK 派生一个「类名被改掉」的坏 APK，同一套检查必须报失败。
#      没有 ⑤，前四组即便全绿也可能只是检查根本没生效（vacuous）。
#
# 用法：bash android/injector/verify_apk.sh [apk路径]
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
APK="${1:-$HERE/build/pginject.apk}"
PY="$ROOT/.venv/Scripts/python.exe"
[ -x "$PY" ] || PY="$(command -v python)"
BT="${ANDROID_SDK:-D:/android-sdk}/build-tools/34.0.0"

[ -f "$APK" ] || { echo "✗ 找不到 $APK（先跑 build.sh）" >&2; exit 1; }

FAIL=0
ok()  { echo "  ✓ $*"; }
bad() { echo "  ✗ $*"; FAIL=$((FAIL+1)); }

# ---------- 对任意 APK 跑 1~4 组检查，返回失败项数 ----------
check_apk() {
  local apk="$1" quiet="${2:-}"
  FAIL=0

  echo "[1/4] dex 是否包含 Shizuku 全链路"
  local rows
  rows=$("$PY" - "$apk" <<'PY'
import sys, zipfile
with zipfile.ZipFile(sys.argv[1]) as z:
    if 'classes.dex' not in z.namelist():
        print('NODEX\t-\tAPK 里没有 classes.dex'); raise SystemExit(0)
    dex = z.read('classes.dex').decode('latin-1')
must = [
    ('rikka/shizuku/Shizuku',              'Shizuku 主类'),
    ('rikka/shizuku/ShizukuBinderWrapper', 'binder 代理（本方案核心）'),
    ('rikka/shizuku/SystemServiceHelper',  '系统服务查询'),
    ('rikka/shizuku/ShizukuProvider',      'binder 接收 provider'),
    ('moe/shizuku/server/IShizukuService', 'aidl 接口（api 依赖它）'),
    ('io/pgauto/inject/ShizukuProbe',      'S0 门禁逻辑'),
    ('io/pgauto/inject/MainActivity',      '界面'),
    # S2「脑」四件（2026-10-21）：少了任何一个都是运行时才炸
    ('io/pgauto/inject/DevRun',                    '端内注入驱动'),
    ('io/pgauto/inject/InjectService',             '前台服务壳'),
    ('io/pgauto/inject/sched/Scheduler',           '载荷解析 + 坐标变换 + 两级调度'),
    ('io/pgauto/inject/sched/Gesture',             '指针状态机（MotionEvent 帧构造）'),
    ('io/pgauto/inject/sched/Densify',             'MOVE 加密（漏了 = dp1 轮 15 Miss）'),
]
for cls, why in must:
    print(('OK' if cls in dex else 'MISS') + '\t' + cls + '\t' + why)
PY
)
  local st cls why
  while IFS=$'\t' read -r st cls why; do
    [ -z "${st:-}" ] && continue
    case "$st" in
      OK)    [ -n "$quiet" ] || ok "$cls  ($why)" ;;
      MISS)  bad "$cls  ($why) —— dex 里没有，运行时会 NoClassDefFoundError" ;;
      NODEX) bad "APK 里没有 classes.dex" ;;
    esac
  done <<< "$rows"

  echo "[2/4] manifest 声明"
  local xml
  xml=$("$BT/aapt2.exe" dump xmltree --file AndroidManifest.xml "$apk" 2>&1)
  chk() { if echo "$xml" | grep -qE "$1"; then [ -n "$quiet" ] || ok "$2"; else bad "$2 —— 没找到"; fi; }
  chk 'package="io\.pgauto\.inject"'                     'package=io.pgauto.inject'
  chk 'rikka\.shizuku\.ShizukuProvider'                   'provider 声明（删了 = pingBinder 恒 false）'
  chk 'moe\.shizuku\.manager\.permission\.API_V23'        'uses-permission API_V23'
  chk 'moe\.shizuku\.client\.V3_SUPPORT'                  'meta-data V3_SUPPORT'
  # aapt2 dump 里相对类名不会展开成全限定名（实测写成 ".InjectService"），
  # 所以这里按相对名匹配 —— 写成全限定名会永远匹配不到（第一版就是这么错的）。
  chk 'name\(0x01010003\)="\.InjectService"'              'service .InjectService（端内「脑」）'
  chk 'foregroundServiceType'                             'foregroundServiceType（A14 起没它 startForeground 会崩）'
  chk 'FOREGROUND_SERVICE_SPECIAL_USE'                    'uses-permission 专用前台类型'
  chk 'PROPERTY_SPECIAL_USE_FGS_SUBTYPE'                  'specialUse 子类型说明（商店/系统审计要求）'

  echo "[3/4] authority 与 package 是否配套"
  local pkg auth
  pkg=$(echo "$xml" | grep -oE 'package="[^"]+"' | head -1 | sed 's/package="//;s/"//')
  auth=$(echo "$xml" | grep -oE 'authorities[^=]*="[^"]+"' | head -1 | sed 's/.*="//;s/"//')
  if [ "$auth" = "$pkg.shizuku" ]; then
    [ -n "$quiet" ] || ok "$auth = $pkg.shizuku"
  else
    bad "authority='$auth'，应为 '<package>.shizuku'（ShizukuProvider 内部依赖该后缀）"
  fi

  echo "[4/4] 签名"
  if "$BT/apksigner.bat" verify "$apk" 2>/dev/null; then
    [ -n "$quiet" ] || ok "apksigner verify 通过"
  else
    bad "签名校验失败"
  fi

  return "$FAIL"
}

echo "== 主对象: $APK ($(stat -c%s "$APK") B) =="
check_apk "$APK"
MAIN_RC=$?
echo

# ---------- ⑤ 端内禁用 API 扫描 ----------
# ★ 为什么必须有这一组：2026-10-21 真机第一次待命就炸
#   `NoSuchMethodError: Files.readString`。javac 用 JDK 17 编得过、PC 上跑得过、
#   d8 也不报 —— Android 的 java.nio.file / String 是**裁剪实现**，Java 9/11 的
#   新方法大量缺失。这类错只有端内运行时才现形，所以只能靠源码级黑名单挡。
scan_forbidden() {  # $1 = 源码根；命中则打印并返回 1
  "$PY" - "$1" <<'PY'
import pathlib, sys
root = pathlib.Path(sys.argv[1])
# (符号, 替代/原因)。匹配的是**剥掉注释后**的代码，注释里写这些词不算。
BAN = [
    ('Files.readString',  'Java 11；用 new String(Files.readAllBytes(p)," + "UTF_8)'),
    ('Files.writeString', 'Java 11；用 Files.write(p, s.getBytes(UTF_8))'),
    ('Files.readAllLines','Android 裁剪实现未必有；自己按行切'),
    ('Path.of(',          'Java 11；用 Paths.get(...)'),
    ('List.of(',          'Java 9；用 Arrays.asList / new ArrayList<>'),
    ('Set.of(',           'Java 9；不支持'),
    ('Map.of(',           'Java 9；不支持'),
    ('.isBlank()',        'Java 11 String；用 trim().isEmpty()'),
    ('.strip()',          'Java 11 String；用 trim()'),
    ('.repeat(',          'Java 11 String；自己拼'),
    ('.toList()',         'Java 16 Stream；用 collect(Collectors.toList())'),
    ('Objects.requireNonNullElse', 'Java 9'),
]

def strip_comments(s):
    """剥 // 行注释与 /* */ 块注释，保留字符串字面量（里面出现禁用词要报）。"""
    out, i, n = [], 0, len(s)
    while i < n:
        c = s[i]
        if c == '"' or c == "'":
            q = c
            out.append(c); i += 1
            while i < n:
                if s[i] == '\\' and i + 1 < n:
                    out.append(s[i:i+2]); i += 2; continue
                out.append(s[i])
                if s[i] == q:
                    i += 1; break
                i += 1
            continue
        if c == '/' and i + 1 < n and s[i+1] == '/':
            while i < n and s[i] != '\n':
                i += 1
            continue
        if c == '/' and i + 1 < n and s[i+1] == '*':
            i += 2
            while i + 1 < n and not (s[i] == '*' and s[i+1] == '/'):
                if s[i] == '\n':
                    out.append('\n')
                i += 1
            i += 2
            continue
        out.append(c); i += 1
    return ''.join(out)

bad = 0
files = sorted(root.rglob('*.java'))
if not files:
    print(f'NOSRC\t{root}\t源码目录为空 ⇒ 扫描没跑成（不算通过）'); raise SystemExit(1)
for f in files:
    code = strip_comments(f.read_text(encoding='utf-8', errors='replace'))
    for ln, line in enumerate(code.splitlines(), 1):
        # 跳过 import 行（import 本身不调用方法；Path.of 之类只会在调用点出现）
        if line.lstrip().startswith('import '):
            continue
        for sym, why in BAN:
            if sym in line:
                print(f'HIT\t{f.relative_to(root)}\t{ln}\t{sym}\t{why}\t{line.strip()[:90]}')
                bad += 1
print(f'SCANNED\t{len(files)} 个源文件')
raise SystemExit(1 if bad else 0)
PY
}

echo "== ⑤ 端内禁用 API 扫描（Android 裁剪库没有 Java 9/11 新符号）=="
SRC="$HERE/app/src/main/java"
if scan_forbidden "$SRC"; then
  ok "源码未使用已知缺失 API"
  API_OK=1
else
  bad "命中禁用 API —— 端内会 NoSuchMethodError（见上面 HIT 行）"
  API_OK=0
fi

# 真对照：造一个含禁用符号的临时源目录，同一函数必须报命中。
# 没有这一步，上面「未命中」也可能只是扫描根本没生效（空转）。
TMPD="$HERE/build/_apiguard_probe"
rm -rf "$TMPD"; mkdir -p "$TMPD"
printf 'class Probe {\n  // Files.readString 只出现在注释里 —— 不该命中\n  String go(java.nio.file.Path p) throws Exception {\n    return java.nio.file.Files.readString(p);\n  }\n}\n' > "$TMPD/Probe.java"
if scan_forbidden "$TMPD" >/dev/null 2>&1; then
  bad "对照没被抓到 ⇒ 禁用 API 扫描失效（前面「未命中」不算数）"
  API_MUT=0
else
  ok "对照被抓到（注释里的同名词未误报）⇒ 扫描不是恒真"
  API_MUT=1
fi
rm -rf "$TMPD"
echo

# ---------- ⑥ 对照：派生坏 APK ----------
echo "== ⑥ 对照：同一套检查跑在一个「类名被改掉」的坏 APK 上 =="
BAD="$HERE/build/_mutant.apk"
"$PY" - "$APK" "$BAD" <<'PY'
import sys, zipfile
src, dst = sys.argv[1], sys.argv[2]
# 等长替换：dex 里的字符串是 UTF-8，长度不变就不会破坏偏移表。
# 只改这两个名字：第 1 组的 MISS 一定会被触发；testzip 仍可读。
SUBS = {b'ShizukuProvider': b'XhizukuProvider',
        b'io/pgauto/inject/MainActivity': b'io/pgauto/inject/XainActivity'}
with zipfile.ZipFile(src) as z:
    items = [(i, z.read(i.filename)) for i in z.infolist()]
with zipfile.ZipFile(dst, 'w', zipfile.ZIP_DEFLATED) as z:
    for info, data in items:
        if info.filename == 'classes.dex':
            for a, b in SUBS.items():
                assert len(a) == len(b), '替换必须等长'
                data = data.replace(a, b)
        z.writestr(info.filename, data)
PY
MUT_OK=0
if [ ! -f "$BAD" ]; then
  bad "无法派生坏 APK ⇒ 第 ⑤ 组没跑成（不算通过）"
else
  check_apk "$BAD" quiet >/dev/null 2>&1
  MUT_RC=$?
  rm -f "$BAD"
  if [ "$MUT_RC" -gt 0 ]; then
    MUT_OK=1
    ok "坏 APK 被抓到 $MUT_RC 项失败（实测: 类名缺失必须被抓）⇒ 检查不是恒真"
  else
    bad "坏 APK 竟然全通过 ⇒ 检查逻辑失效，前面全绿不算数"
  fi
fi

echo
if [ "$MAIN_RC" -eq 0 ] && [ "$MUT_OK" -eq 1 ] && [ "$API_OK" -eq 1 ] && [ "$API_MUT" -eq 1 ]; then
  echo "全部通过"
  exit 0
fi
echo "未通过（APK $MAIN_RC 项失败；禁用API扫描 $([ "$API_OK" -eq 1 ] && echo 通过 || echo 命中)；"\
     "坏APK对照 $([ "$MUT_OK" -eq 1 ] && echo 有效 || echo 失效)；"\
     "API对照 $([ "$API_MUT" -eq 1 ] && echo 有效 || echo 失效)）"
exit 1
