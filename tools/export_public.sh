#!/usr/bin/env bash
# 把「可以公开的代码与文档」导出成一个干净目录 —— **不改本仓库历史、不删本地文件**。
#
# 为什么需要它（2026-10-26）：
#   本仓库里有三类东西不能进公开仓库：
#     ① 游戏派生数据：`android/play/app/src/main/assets/{manifest.json,payload/*.json}`（1056 条）、
#        `samples/**`（曲目清单/母带，母带 wav 本来就被 gitignore）；
#     ② 实验中间产物：`tmp/**` 里成堆的 MB 级 JSON；
#     ③ 项目记忆与报告：`.workbuddy/**`、`out/**`（含大量证据截图）。
#   公开仓库只放「别人用来复现/构建/测试」所需的那部分；曲库素材由使用者按 README
#   从自己的游戏安装里生成（`tools/pack_assets.py`）。
#
# 用法：
#   bash tools/export_public.sh                 # 导出到 dist/public（默认，不含游戏素材）
#   bash tools/export_public.sh /tmp/pub        # 指定目录
#   bash tools/export_public.sh --with-assets   # 连同 manifest/payload 一起导出（⚠ 版权自负）
#   bash tools/export_public.sh --force         # 覆盖已有目录：清空内容但**保留 .git**（增量提交）
#   bash tools/export_public.sh --fresh         # 彻底重置：连 .git 一起删，历史重建
#
# 导出后会：① 打印文件数与体积 ② 做一次「泄漏检查」（不该出现的路径出现就报错）
#   ③ 把变化提交进目标目录的 git 仓库（不配 remote，不推送）。
#
# ★ 增量语义（2026-11-01）：`--force` 只清空文件、保留 `.git`，所以每次导出是一笔**正常提交**，
#   历史连续、push 永远不需要 `--force`；内容没变化时不产生空提交。
#   （文件仍是全量重写，但 git 按内容哈希判断，未变的文件不会进提交。）
# ⚠ 目标目录是**产物**，主仓才是唯一真源：直接在目标目录里手改的东西会被下次导出覆盖。
# ⚠ `--fresh` 之后历史归零；若远端已有内容，push 会被拒，只能用 `git push --force`。
# 需要 git ≥ 2.28（用到 `git init -b`）。
set -euo pipefail

# ★ 中文/✓ 输出在「中文 Windows + Git Bash」的管道里会按 GBK 编码炸掉（build.sh 踩过同一个坑）。
#   统一让本脚本起的 python 子进程用 UTF-8，否则一次导出会在打印统计时静默中断。
export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

OUT="dist/public"
WITH_ASSETS=0
FORCE=0
FRESH=0
for a in "$@"; do
  case "$a" in
    --with-assets) WITH_ASSETS=1 ;;
    --force)       FORCE=1 ;;
    --fresh)       FORCE=1; FRESH=1 ;;   # --fresh 隐含 --force
    -*)            echo "✗ 未知选项：$a" >&2; exit 2 ;;
    *)             OUT="$a" ;;
  esac
done
case "$OUT" in /*) ;; *) OUT="$ROOT/$OUT" ;; esac

if [ -e "$OUT" ]; then
  if [ "$FORCE" = "1" ]; then
    if [ "$FRESH" = "1" ]; then
      KEEP_GIT=0
      echo "· 目标已存在，按 --fresh 彻底重置（含 .git，历史将重建）：$OUT"
    else
      KEEP_GIT=1
      echo "· 目标已存在，按 --force 清空内容（保留 .git，增量提交）：$OUT"
    fi
    "$ROOT/.venv/Scripts/python.exe" - "$OUT" "$KEEP_GIT" <<'PY'
import shutil, sys, pathlib
p = pathlib.Path(sys.argv[1])
# ★ 保留 .git 是「增量提交」的全部关键：文件全量重写，但 git 历史接着往下长。
keep_git = sys.argv[2] == "1"
def _force_rm(x):
    # ★ Windows 上 git 会把 .git/objects 下的文件设成只读 ⇒ 直接 unlink 会 WinError 5。
    try:
        x.chmod(0o777)
    except Exception:
        pass
    try:
        x.unlink()
    except PermissionError:
        x.chmod(0o666); x.unlink()

for c in sorted(p.iterdir(), reverse=True):
    if keep_git and c.name == '.git':
        continue
    if c.is_dir() and not c.is_symlink():
        shutil.rmtree(c, onerror=lambda f, path, exc: (_force_rm(pathlib.Path(path)) if pathlib.Path(path).is_file() else None))
    else:
        _force_rm(c)
PY
  else
    echo "✗ 目标已存在：$OUT（要覆盖请加 --force）" >&2
    exit 1
  fi
fi
mkdir -p "$OUT"

# ── 白名单：只导出「被 git 跟踪」的文件，再按下面规则剔除 ──────────────────────
n=0
git ls-files -z | while IFS= read -r -d '' f; do
  keep=1
  case "$f" in
    # ① 游戏派生数据：assets 整目录（manifest.json + payload/*.json）
    android/*/app/src/main/assets/*) keep=0 ;;
    # ② 本地生成的曲库素材（清单/母带/谱面）
    samples/*) keep=0 ;;
    # ③ 中间产物 / 记忆 / 报告 / 快照
    tmp/*|out/*|snapshots/*|.workbuddy/*|dist/*) keep=0 ;;
    # 测试夹具是**游戏原始字节**（官方 APK 的 catalog.json / chart bundle），不能公开分发
    tests/fixtures/*) keep=0 ;;
    # check.bat 调用 tmp/migrate_check.py，而 tmp/ 不导出 ⇒ 公开仓库里会是个坏入口
    check.bat) keep=0 ;;
    # ④ 第三方二进制（用 fetch_deps.sh 拉，避免仓库里塞 AAR）
    android/vendor/*) keep=0 ;;
    # ⑤ 构建产物 / 缓存 / 机器相关配置
    android/*/build/*|*/__pycache__/*|*.pyc|.idea/*|.cache/*|.pytest_cache/*) keep=0 ;;
    android/*/local.properties|android/*/*.keystore|android/*/*.jks) keep=0 ;;
    # ⑥ 运行期状态（每轮都变）
    pgauto/g_state.json*) keep=0 ;;
  esac
  if [ "$WITH_ASSETS" = "1" ]; then
    case "$f" in
      android/play/app/src/main/assets/manifest.json|android/play/app/src/main/assets/payload/*) keep=1 ;;
    esac
  fi
  [ "$keep" = "1" ] && cp --parents "$f" "$OUT/"
done

# ── 统计 + 泄漏检查 ──────────────────────────────────────────────────────────
echo
echo "· 导出目录：$OUT"
"$ROOT/.venv/Scripts/python.exe" - "$OUT" "$WITH_ASSETS" <<'PY'
import pathlib, sys, subprocess
out = pathlib.Path(sys.argv[1])
with_assets = sys.argv[2] == "1"
# 兜底脱敏：把形如设备序列号的 16 位大写串换成占位符。
# 本仓库目前只出现机型名（公开信息），但导出是给外人看的最后一道闸。
import re as _re
TEXT_EXT = {'.py', '.java', '.sh', '.bat', '.md', '.txt', '.xml', '.json', '.gradle', '.properties'}


def _iter_files():
    # ★ 必须排除 `.git/`：`--force` 现在保留 .git，而 rglob('*') 会一路扫进 objects/
    #   ⇒ 统计出「534 文件 / 5.44 MB」这种假数字，泄漏检查也跟着扫无用文件。
    for _p in out.rglob('*'):
        if not _p.is_file():
            continue
        if '.git' in _p.relative_to(out).parts:
            continue
        yield _p


for _p in _iter_files():
    if _p.suffix.lower() in TEXT_EXT:
        try:
            _t = _p.read_text(encoding='utf-8', errors='surrogateescape')
            _n = _re.sub(r'\bAK3' + 'SBB[0-9A-Z]{10}\b', 'DEVICE_SERIAL', _t)
            if _n != _t:
                _p.write_text(_n, encoding='utf-8', errors='surrogateescape')
        except Exception:
            pass
files = list(_iter_files())
total = sum(p.stat().st_size for p in files)
print(f"· 文件数 {len(files)} ｜体积 {total/1048576:.2f} MB")

bad = []
for p in files:
    rel = p.relative_to(out).as_posix()
    if rel.startswith(('samples/', 'tmp/', 'out/', 'snapshots/', '.workbuddy/', 'dist/', 'android/vendor/')):
        bad.append(rel)
    if '/assets/' in rel and not with_assets:
        bad.append(rel)
    if rel.endswith(('.wav', '.mp3', '.mp4', '.h264', '.keystore', '.jks')):
        bad.append(rel)
if bad:
    print(f"✗ 泄漏检查失败：{len(bad)} 个不该导出的文件，例如 {bad[:5]}")
    sys.exit(1)
print("✓ 泄漏检查通过（无曲库素材 / 无 tmp / 无 out / 无 keystore / 无音视频）")

# ⚠ 不能用 `git rev-parse --show-toplevel` 判：导出目录在本仓库的 dist/ 下，
#   那条命令会一路找到**父仓库**并误报「已是 git 仓库」。只看自己有没有 .git。
print("· 目标已是 git 仓库（.git 存在），跳过 git init" if (out / '.git').exists()
      else "· 目标还没有 .git，稍后会 git init")
PY

# ── 把变化提交进目标目录的 git 仓库（不配 remote、不推送）──────────────────────
# ★ 身份取本机 git 配置：写死 `phigros-auto <noreply@example.com>` 会让 GitHub 无法把提交
#   关联到账户（显示成灰头像、点不动）。只有本机没配过才回退到占位身份。
# ★ 身份**优先读目标仓库自己的配置**。脚本开头 `cd "$ROOT"` 了，直接 `git config` 读到的是
#   主仓身份（本地开发用的真实邮箱）⇒ 推 GitHub 会被 GH007 拒（私密邮箱保护），
#   哪怕目标仓库里已经配好了 noreply 地址也没用。所以先 `git -C "$OUT" config`。
#   ⚠ 不能用裸 `git -C "$OUT" config`：目标目录在父仓库之内且自身还不是仓库时，
#   它会向上找到**父仓库**并返回主仓身份 —— 所以先判 `$OUT/.git` 是否存在。
GIT_NAME=""
GIT_EMAIL=""
if [ -d "$OUT/.git" ]; then
  GIT_NAME="$(git -C "$OUT" config user.name 2>/dev/null || true)"
  GIT_EMAIL="$(git -C "$OUT" config user.email 2>/dev/null || true)"
fi
if [ -z "$GIT_NAME" ]; then GIT_NAME="$(git config user.name 2>/dev/null || true)"; fi
if [ -z "$GIT_EMAIL" ]; then GIT_EMAIL="$(git config user.email 2>/dev/null || true)"; fi
if [ -z "$GIT_NAME" ]; then GIT_NAME="phigros-auto"; fi
if [ -z "$GIT_EMAIL" ]; then GIT_EMAIL="noreply@example.com"; fi

if [ ! -d "$OUT/.git" ]; then
  # ★ 分支名固定 main：GitHub 2020-10 起新建仓库默认 main，两边一致才不会出现
  #   「远端在 main、本地推 master」两个不相干分支的局面。本机 git 未配置
  #   init.defaultBranch（仍默认 master），所以这里必须显式写。
  ( cd "$OUT" && git init -q -b main && git add -A && \
    git -c user.name="$GIT_NAME" -c user.email="$GIT_EMAIL" commit -q -m "initial public export" ) || {
    echo "⚠ git init 失败（不影响文件导出）" >&2
  }
  echo "· 已建仓库并完成首次提交（分支 main）"
else
  (
    cd "$OUT"
    git add -A
    # ⛔ `git diff --cached --quiet` 有差异时返回 1。必须写在 if 条件位置 —— 直接
    #   `git diff --cached --quiet; commit` 会在「刚 add 完、正要提交」处被 set -e 静默终止。
    if git diff --cached --quiet; then
      echo "· 内容无变化，跳过提交"
    else
      _n=$(git diff --cached --name-only | wc -l | tr -d ' ')
      git -c user.name="$GIT_NAME" -c user.email="$GIT_EMAIL" commit -q \
        -m "sync: $(date +%F) 更新 ${_n} 个文件"
      echo "· 已提交 ${_n} 个变化文件（累计第 $(git rev-list --count HEAD) 笔）"
    fi
    # 兜底：目标目录里带着主仓的 .gitignore，可能静默吞掉本该提交的文件
    _tracked=$(git ls-files | wc -l | tr -d ' ')
    _onfs=$(find . -type f -not -path './.git/*' | wc -l | tr -d ' ')
    if [ "$_tracked" != "$_onfs" ]; then
      echo "⚠ 跟踪 $_tracked 个 / 磁盘 $_onfs 个（不一致，可能有文件被 .gitignore 吞掉）：" >&2
      git status --short --untracked-files=all 2>/dev/null | grep '^??' | head -5 >&2 || true
    fi
  )
fi

echo
echo "下一步（手动，只需第一次）："
echo "  1) 看一眼导出内容：  ls \"$OUT\""
echo "  2) 建空仓库并推送：  cd \"$OUT\" && git remote add origin <你的仓库地址> && git push -u origin HEAD"
echo "  3) 别人 clone 后按 README「你需要的素材」生成 assets，再 bash android/play/build.sh"
echo
echo "以后每次：改主仓 → 重跑本脚本 → cd \"$OUT\" && git push（历史连续，不需要 --force）"
if [ "$FRESH" = "1" ]; then
  echo
  echo "⚠ 本次用了 --fresh：历史已重建。若远端已有内容，push 会被拒，只能用 git push --force 覆盖。" >&2
fi
