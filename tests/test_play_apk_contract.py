"""`assets/manifest.json` 的生成端（Python）与消费端（Java）之间的契约。

这一层是 M1 最容易出**静默错**的地方：Python 改个键名、Java 读旧键名，
两边都跑得好好的 —— Python 不报错，`javac` 也不报错（`org.json` 的键是字符串），
一直到真机上 `sil_ref_s` 被读成 0、1286 个音符全 Miss 才会发现。

所以这里不用"字段看起来对"当判据，全部是**跨文件取真值比对**：

    [1] Java 的 `REQUIRED` 数组 ⊆ 每首歌实际拥有的键（Java 要什么，Python 必须给）；
    [2] `manifest.json` 与 `payload/*.json` 的 note_count / n_events 必须相等
        （打包时错配 payload 会在这里被抓出来）；
    [3] `sil_ref_s` 与真机 timeline 记录一致（±1 ms）—— 预算公式若漂移，
        换设备后的零点就整体偏，而这条是唯一能在离线阶段发现的证据；
    [4] `build.sh` 必须带 `-A`（assets 根目录）且必须调 `check_play_apk.py`
        （前者少一行就 APK 里没 assets，后者是装机器前的最后一道闸）。

APK 本身（dex 类名 / authority / 逐字节）由 `tools/check_play_apk.py` 验，
需要 build-tools，不在 pytest 里跑。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "android/play/app/src/main/assets"
SONGLIB = ROOT / "android/play/app/src/main/java/io/pgauto/play/SongLib.java"
BUILDSH = ROOT / "android/play/build.sh"


def java_array(src: str, name: str) -> list[str]:
    """从 SongLib.java 抽出 `String[] NAME = { "a", "b" };` 里的字符串。"""
    m = re.search(rf"String\[\]\s+{name}\s*=\s*\{{(.*?)\}}", src, re.S)
    assert m, f"SongLib.java 里找不到 String[] {name}"
    return re.findall(r'"([^"]+)"', m.group(1))


def strip_java_comments(src: str) -> str:
    """去掉 `/* … */` 与 `// …`：判据只针对**代码**，注释里叙述旧实现不算违规。

    （这条是自测抓出来的：第一版测试把类注释里「旧版在 start() 里硬检查
      `getStreamVolume(...) == 0`」当成了违规 ⇒ 对着正确代码红。判据分开写才对：
      **"有没有这个分支"看代码，"为什么曾经这么写"看注释**。）
    """
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"//[^\n]*", "", src)


@pytest.fixture(scope="module")
def manifest() -> dict:
    p = ASSETS / "manifest.json"
    if not p.exists():
        # 曲库素材按设计不进仓库（游戏资产）；没跑 pack_assets 之前无从核对 ⇒ skip 而不是 error。
        # 主仓永远有它，改坏了照样红 —— 这条 skip 只对「刚 clone 还没生成素材」生效。
        pytest.skip(f"缺 {p}：先跑 tools/pack_assets.py（首次 clone 时本来就没有）")
    return json.loads(p.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def src() -> str:
    assert SONGLIB.exists(), f"缺 {SONGLIB}"
    return SONGLIB.read_text(encoding="utf-8")


# --------------------------------------------------------------- [1] 字段契约

def test_required_and_optional_do_not_overlap(src):
    """一个字段不能同时要求"必须有"和"可以没有"。"""
    req = set(java_array(src, "REQUIRED"))
    opt = set(java_array(src, "OPTIONAL"))
    assert req, "REQUIRED 抽成了空数组（正则失效？）"
    assert opt, "OPTIONAL 抽成了空数组（正则失效？）"
    assert not (req & opt), f"同时出现在 REQUIRED 与 OPTIONAL：{sorted(req & opt)}"


def test_every_required_field_exists_in_every_song(src, manifest):
    """Java 缺字段会抛异常（故意的），所以这条必须成立，否则装机即崩。"""
    req = java_array(src, "REQUIRED")
    for s in manifest["songs"]:
        missing = [k for k in req if k not in s]
        assert not missing, f"{s['key']} 缺 Java 必需字段 {missing}"


def test_manifest_has_no_unknown_numeric_silence_key(manifest):
    """★ 防键名漂移：`sil_ref_s` 曾被写成 `sil_ref`（少 `_s`），单位也会跟着错。

    判据只针对 `sil_ref` 这一族：**任何含 `sil_ref` 的键都必须以 `_s` 结尾**。
    不能写成「所有含 sil 的键都以 _s 结尾」—— `sil_thr`（能量阈值，无量纲）和
    `sil_confirmed`（`full`/`partial` 标签）本来就该是那个名字，那样写会误报。
    """
    for s in manifest["songs"]:
        bad = [k for k in s if "sil_ref" in k and not k.endswith("_s")]
        assert not bad, f"{s['key']} 的静音时长键不以 _s 结尾：{bad}（单位会是 ms 或裸秒）"
        assert isinstance(s.get("sil_ref_s"), (int, float)), \
            f"{s['key']}.sil_ref_s 不是数值：{s.get('sil_ref_s')!r}"


def test_level_is_null_or_positive_int(manifest):
    """等级：有真据才填正数，没有就 null —— ⛔ 不许编一个数字。"""
    for s in manifest["songs"]:
        lv = s.get("level")
        assert lv is None or (isinstance(lv, int) and lv > 0), \
            f"{s['key']}.level = {lv!r}（应为 null 或正整数）"


def test_java_knows_tap_lead_field(src, manifest):
    """Python 现在每首都写 `tap_lead_ms`，Java 必须**有人读它**。

    ★ 这条防的是「两边各写各的」：Java 不读，这个字段就是白写 —— 端内 `zero=tap`
      会退回「无 lead」而拒绝，看起来"安全"，实际是功能被静默拿掉。
      反过来若 Java 读了但 Python 不发，`SongLib.parse` 会当 null（0）⇒ 同样拒绝。
    """
    keys = set(java_array(src, "REQUIRED")) | set(java_array(src, "OPTIONAL"))
    assert "tap_lead_ms" in keys, (
        "SongLib 的 REQUIRED/OPTIONAL 里都没有 tap_lead_ms ⇒ Java 不会读它，"
        "manifest 里这个字段等于白写")
    for s in manifest["songs"]:
        assert "tap_lead_ms" in s, f"{s['key']} 缺 tap_lead_ms（pack_assets 没发？）"


def test_java_knows_sil_thr_field(src, manifest):
    """`sil_thr` 必须被 Java **真的读走**（2026-10-25，FACTS §169）。

    ★ 这条防的是一个已经发生过的空洞：`pack_assets.py` 把母带的边沿阈值写进了
      `manifest.sil_thr`，但 `SongLib` 的 REQUIRED/OPTIONAL 里没有它、端内也没有任何
      代码引用 ⇒ 字段白写，端内改用「5×背景」相对判据 ⇒ 渐入曲的边沿与 `sil_ref`
      差几百毫秒（bounded_in 实测：母带 0.865 s，相对判据 ~1.7 s）。
      **数据打包了但没人读** = 静默错，只能在契约层抓。
    """
    keys = set(java_array(src, "REQUIRED")) | set(java_array(src, "OPTIONAL"))
    assert "sil_thr" in keys, (
        "SongLib 的 REQUIRED/OPTIONAL 里都没有 sil_thr ⇒ Java 不会读它，"
        "manifest 里这个字段等于白写（边沿会退回相对判据）")
    code = strip_java_comments(src)
    assert '"sil_thr"' in code, "SongLib 没有解析 sil_thr 键"
    assert "silThr" in code, "SongLib 没把 sil_thr 放进 Song（下游拿不到）"
    for s in manifest["songs"]:
        assert "sil_thr" in s, f"{s['key']} 缺 sil_thr（pack_assets 没发？）"
        thr = s["sil_thr"]
        assert thr is None or (isinstance(thr, (int, float)) and thr > 0), \
            f"{s['key']}.sil_thr = {thr!r}（应为 null 或正数 RMS；0/负数会被 SongLib 拒收）"


def _timeline_leads() -> dict[str, list[float]]:
    """复用生成端的算式（`pack_assets.timeline_leads`），不另写一份。

    教训 9：同一个量两处实现必然分叉 —— 这里要防的恰恰是「manifest 里的数与实测
    对不上」，所以**必须**用同一个算式，否则两边漂了也测不出来。
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_pack_assets_for_test", ROOT / "tools/pack_assets.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.timeline_leads()


def test_tap_lead_matches_a_measured_timeline(manifest):
    """★ `tap_lead_ms` 必须是**某一轮真机实测**出来的值（±1 ms）。

    这条是 10-24 那个真 bug 的回归钉。端内曾写死 `DEFAULT_LEAD_MS = 7479.88`，
    注释称「bounded_in 专属」。那个数**有出处**（本曲两轮 d_ideal 的池中位
    7.479885），但：
      (a) 池中位不等于任何一轮的真实 lead —— 那两轮实测相差 73 ms，而真正打 φ 的
          第 3 轮是 7520.086（三轮实测 7471.437 / 7537.133 / 7520.086）；
      (b) `launchPlay` 从不传 `EXTRA_LEAD_MS` ⇒ `zero=tap` 在**任何曲**上都用它。
    ⇒ 现在两件事一起钉：登记了的曲必须能对上某轮实测；且**至少有一首登记**
      （否则整列被清空、端内 tap 永远拒绝，而测试会"全绿"）。
    """
    leads = _timeline_leads()
    if not leads:
        # 真机时间线（tmp/timeline_*.json）不随仓库分发 ⇒ 公开仓无从核对。
        # ⚠ 用 skip 而不是"放行"：这条测的是「登记的值必须来自某轮实测」，
        #   没数据就既不能证明也不能推翻，绝不能让它绿着蒙过去。
        pytest.skip("本机没有 tmp/timeline_*.json（真机时间线不在仓库里）⇒ 无从核对")
    registered = 0
    for s in manifest["songs"]:
        v = s.get("tap_lead_ms")
        if v is None:
            continue
        registered += 1
        assert isinstance(v, (int, float)) and 0 < v < 60_000, \
            f"{s['key']}.tap_lead_ms = {v!r} 越界（应为 1~60000 ms）"
        meas = leads.get(s["key"], [])
        assert meas, (
            f"{s['key']} 登记了 tap_lead_ms={v}，但 tmp 里没有该曲 timeline 可核对 "
            f"⇒ 无法证明它是实测值")
        d = min(abs(v - m) for m in meas)
        assert d < 1.0, (
            f"{s['key']} 的 tap_lead_ms={v} 与任何一轮实测 lead 都对不上"
            f"（最近差 {d:.2f} ms；实测 {[round(m, 3) for m in meas]}）"
            " ⇒ 它多半是池中位/跨曲借来的数，端内 tap 模式会拿它给整曲打分")
    assert registered > 0, (
        "所有曲的 tap_lead_ms 都是 null ⇒ 端内 zero=tap 变成永远拒绝。"
        "若确实要停用 tap 模式，请删掉 PlayService 里那条分支，不要留半条路")


# ------------------------------------------------------- [2] 与载荷交叉核对

def test_manifest_counts_match_payload(manifest):
    """manifest 的 note_count / n_events 必须与载荷文件里的同名字段一致。

    这是在防「打包时 payload 指错了曲」——那种错在界面上看不出来（曲名对、
    难度对），只有数字对不上。
    """
    for s in manifest["songs"]:
        pf = ASSETS / s["payload"]
        assert pf.exists(), f"{s['key']} 的载荷 {s['payload']} 不存在"
        pay = json.loads(pf.read_text(encoding="utf-8"))
        assert pay["note_count"] == s["note_count"], \
            f"{s['key']}: manifest {s['note_count']} ≠ payload {pay['note_count']}"
        assert len(pay["events"]) == s["n_events"], \
            f"{s['key']}: manifest n_events {s['n_events']} ≠ payload {len(pay['events'])}"


#: 真机横屏口径与「黑边」：`draw` 端 fit=height，16:9 内容带在 1610×720 上留 165 px 黑边。
DST_W, DST_H = 1610.0, 720.0
BLACK_BAR_PX = 165.0
#: 谱面判定线在真机上的 y（`FACTS` §3：判定线 y=360）。
JUDGE_LINE_Y = 360.0


def test_payload_canvas_is_the_16_9_content_frame(manifest):
    """★ 载荷的 `canvas` 必须是 **16:9 内容框**，事件是 `screen` 坐标。

    这是 10-24 抓到的一个**会让整曲全 Miss** 的错：HEAD 里
    `assets/payload/bounded_in.json` 的 canvas 是 `1080×2340`，而它的来源
    `out/touches_bounded_in.json` 写的是 `1920×1080` —— 两份文件的事件**逐条完全相同**
    （3104/3104），**只差 canvas 这一个标签**。判断哪边对，不能"看起来像"，要看端内几何：

      · `Scheduler.toDevice` 先 to_canvas（`k = screenW/canvasW`）再 from_canvas；
        events 实测 y 到 1416.7 > 1080 ⇒ 它们**不是** 1920×1080 画布坐标，而是
        `screen`(1080×2340) 坐标，canvas 只负责定位那条 16:9 内容带
        （y ∈ [866.25, 1473.75]，事件全部落在带内）。
      · fit=height 下 `xOff = (1610 − canvasW×720/canvasH)/2`：
        canvas=1920×1080 ⇒ **165.0**（= 真机实测黑边）；canvas=1080×2340 ⇒ 638.8
        ⇒ 全部手指挤进中间约 325 px 的竖条，1286 音全 Miss。
      · 判定线自检：canvas 中线 `ch/2` 应映射到 360 —— 1920×1080 ⇒ 540×0.6667 = **360**；
        1080×2340 ⇒ 1170×0.3077 = 720（屏幕底边）。
      · `Densify.CANVAS_PER_SCREEN` 在端内**写死 1920/1080**（拿 screen 像素换算
        canvas px 判 Drag 带）。它必须与载荷的 `canvasW/srcW` 相等，否则加密判据错。
        HEAD 那份 1080×2340 载荷会得到 1080/1080 = 1.0 ≠ 1.7778 ⇒ 本条立刻红。
    """
    densify = (ROOT / "android/injector/app/src/main/java/io/pgauto/inject/sched/Densify.java"
               ).read_text(encoding="utf-8")
    m = re.search(r"CANVAS_PER_SCREEN\s*=\s*([0-9.]+)\s*/\s*([0-9.]+)\s*;", densify)
    assert m, "Densify.java 里找不到 CANVAS_PER_SCREEN = A / B;（常量改名了？）"
    densify_k = float(m.group(1)) / float(m.group(2))
    assert densify_k > 0, f"Densify.CANVAS_PER_SCREEN 算成了 {densify_k}"

    for s in manifest["songs"]:
        pay = json.loads((ASSETS / s["payload"]).read_text(encoding="utf-8"))
        cw, ch = pay["canvas"]["width"], pay["canvas"]["height"]
        sw, sh = pay["screen"]["width"], pay["screen"]["height"]
        assert cw > 0 and ch > 0 and sw > 0 and sh > 0, f"{s['key']} canvas/screen 非正数"

        assert abs(cw / ch - 16 / 9) < 0.02, (
            f"{s['key']} canvas {cw:.0f}x{ch:.0f} 不是 16:9"
            f"（ratio {cw / ch:.4f}）—— 端内 fit=height 依赖这一点")

        x_off = (DST_W - cw * (DST_H / ch)) / 2.0
        assert abs(x_off - BLACK_BAR_PX) < 1.0, (
            f"{s['key']} canvas {cw:.0f}x{ch:.0f} ⇒ 到 1610x720 的 xOff = {x_off:.1f}px，"
            f"与真机黑边 {BLACK_BAR_PX:.0f}px 不符（画布口径错了，手指会整体错位）")

        line_y = (ch / 2.0) * (DST_H / ch)
        assert abs(line_y - JUDGE_LINE_Y) < 1.0, (
            f"{s['key']} canvas 中线映射到 y={line_y:.1f}，与真机判定线 "
            f"{JUDGE_LINE_Y:.0f} 不符")

        assert abs(densify_k - cw / sw) < 1e-9, (
            f"{s['key']} canvasW/screenW = {cw / sw:.4f}，而 Densify.CANVAS_PER_SCREEN "
            f"= {densify_k:.4f} ⇒ 端内 MOVE 加密的 Drag 判定带会错（这两者必须相等）")

        bad = [e for e in pay["events"] if not (0 <= e["x"] <= sw and 0 <= e["y"] <= sh)]
        assert not bad, (
            f"{s['key']} 有 {len(bad)} 个事件坐标落在 screen {sw:.0f}x{sh:.0f} 之外"
            f"（首个 {bad[0] if bad else None}）—— canvas 标签可能被换成了别的口径")


def test_key_events_not_more_than_all_events(manifest):
    """关键事件是事件总数的子集；多出来说明统计口径写错了。"""
    for s in manifest["songs"]:
        assert 0 < s["n_key"] <= s["n_events"], \
            f"{s['key']}: n_key={s['n_key']} 不在 (0, n_events={s['n_events']}] 内"


# --------------------------------------------------- [3] sil_ref 与真机对照

def test_sil_ref_matches_onsite_timeline(manifest):
    """离线预算的 sil_ref 必须与真机 timeline 记录一致（±1 ms）。

    `sil_ref` 是**母带属性**（`audio_round.reference_silence` 在 wav 上算），
    所以它必须与设备无关；一旦预算实现漂移，换设备后的零点会整体偏，
    而那时已经没有 PC 侧的 timeline 可以对照了。

    ★ 取值路径是 `timeline["l4c"]["sil_ref_s"]`，**不是顶层**（顶层没有这个键）。
      读错位置会让本测试静默 skip —— 那正是「判据空转」，所以这里区分两种情况：
      「没有 timeline 文件」才 skip；「有文件但读不到值」直接 fail。
    """
    tm = ROOT / "tmp"

    def sil_ref_of(path):
        t = json.loads(path.read_text(encoding="utf-8"))
        l4c = t.get("l4c") or {}
        v = l4c.get("sil_ref_s", t.get("sil_ref_s", t.get("sil_ref")))
        return v if isinstance(v, (int, float)) else None

    # ★ 先证明「字段路径还对」：tmp 里只要存在任意一个带数值 sil_ref 的 timeline 就说明没改名。
    #   否则整条断言会空转（这正是上一版要防的事），必须 fail。
    all_tl = sorted(tm.glob("timeline_*.json"))
    if not all_tl:
        pytest.skip("tmp 里没有 timeline（换机后旧记录会被清，可接受）")
    if not any(sil_ref_of(p) is not None for p in all_tl):
        pytest.fail("tmp 里有 timeline 但**没有一个**含数值 sil_ref："
                    "l4c.sil_ref_s / sil_ref_s / sil_ref 的路径变了，这条断言在空转")

    seen_files = 0
    checked = 0
    for s in manifest["songs"]:
        # ⚠ 前缀匹配会撞车（key="burn" 会带出 timeline_burn_follow.json），而且 tmp 里混着
        #   旧口径记录（那时还没有 l4c.sil_ref_s）。逐个试、取第一个有值的；全都没有就跳过本曲。
        cands = sorted(tm.glob(f"timeline_{s['key']}*.json"))
        pick = None
        for c in reversed(cands):
            v = sil_ref_of(c)
            if v is not None:
                pick = (c, v)
                break
        if pick is None:
            continue
        path, v = pick
        seen_files += 1
        d_ms = abs(v - s["sil_ref_s"]) * 1000.0
        assert d_ms < 1.0, (
            f"{s['key']}: manifest sil_ref {s['sil_ref_s']:.6f} 与 "
            f"{path.name} 的 {v:.6f} 差 {d_ms:.2f} ms（应 < 1 ms）")
        checked += 1

    assert checked == seen_files, f"只核对了 {checked}/{seen_files} 首"


# --------------------------------------------------------------- [4] 构建链

def test_build_sh_packs_assets_and_checks_apk():
    """两个单点否决：`-A` 少了 APK 里没 assets；不调 checker 就只能靠装机发现。"""
    assert BUILDSH.exists(), f"缺 {BUILDSH}"
    sh = BUILDSH.read_text(encoding="utf-8")
    assert '-A "$ASSETS"' in sh, "build.sh 的 aapt2 link 少了 -A（assets 不会进 APK）"
    assert "tools/check_play_apk.py" in sh, "build.sh 没有调用 tools/check_play_apk.py"


def test_build_sh_compiles_all_three_trees():
    """三树同编是本工程的核心决定（源码不复制）；少一棵就编译不过，但仍要钉住。"""
    sh = BUILDSH.read_text(encoding="utf-8")
    for var in ("SRC_PROBE", "SRC_INJECT", "SRC_PLAY"):
        assert re.search(rf'{var}="\$', sh), f"build.sh 里找不到 {var} 定义"
    m = re.search(r"javac\.exe.*?-d build/classes \$SRCS", sh, re.S)
    assert m, "javac 没有用三棵树合并后的 $SRCS 编译"


def test_every_play_source_is_covered_by_the_gate():
    """★ 每个 `play` 源文件都必须在 `check_play_apk.REQUIRED_CLASSES` 里。

    理由：闸门靠一张**手写清单**判断「dex 里有没有这个类」。新增一个源文件却忘了登记，
    闸门依旧全绿 —— 覆盖范围静默缩小，而这正是「构建成功、装机 NoClassDefFoundError」
    那类错误的入口（三树同编漏文件时 javac 未必报错）。
    ⇒ 清单与磁盘文件必须**双向相等**：多了是陈旧条目（改名后没人清），少了是漏登记。
    """
    gate = (ROOT / "tools/check_play_apk.py").read_text(encoding="utf-8")
    listed = set(re.findall(r'"(io/pgauto/play/[A-Za-z0-9_]+)"', gate))
    on_disk = {f"io/pgauto/play/{p.stem}"
               for p in (ROOT / "android/play/app/src/main/java/io/pgauto/play").glob("*.java")}
    assert on_disk, "play 目录里没有源文件？路径变了？"
    missing = sorted(on_disk - listed)
    stale = sorted(listed - on_disk)
    assert not missing, f"这些源文件没登记进 REQUIRED_CLASSES（闸门查不到它们）：{missing}"
    assert not stale, f"REQUIRED_CLASSES 里有已不存在的类（改名/删除后没清）：{stale}"


def test_media_volume_is_diagnostic_only():
    """★ 10-24 回归钉：媒体音量**不得**再当闸门。

    历史：`OnsetTap.start()` 曾在 `getStreamVolume(STREAM_MUSIC) == 0` 时直接抛。
    依据是一次**混杂实验** —— 10-19 那三轮录的是 probe **自己**播的脉冲，而 Android
    设计上本 app 的播放永远不进 capture（同批 `vol=12` 的两轮同样峰值 0）⇒ 音量从来
    不是被隔离出来的变量。实测反证：`bounded_in_r4` 在音量 0 下录到
    **peak 32768 / −32768**、11 个台阶清晰并打出 φ；抽查 18 份历史录音全部满幅
    ⇒ 本机 capture 取**音量缩放前**的渲染侧。

    所以现在的判据是「采集到的样本」：只有 `OnsetGate.allZero()`（一帧非零样本都没有）
    才算真静音。这条测试钉三件事：

      ① 源里不再有「音量 ≤ 0 ⇒ 抛」的分支（文案回来 = 分支回来）；
      ② 音量读数只出现在诊断函数里 —— 它**不参与判定**；
      ③ 失败文本必须引用实测结论 `signalVerdict()`，而不是让人去猜音量。

    变异验证：把旧的那句 `throw new IllegalStateException("媒体音量为 0…")` 塞回去 ⇒ 红。
    """
    play = ROOT / "android/play/app/src/main/java/io/pgauto/play"
    tap_code = strip_java_comments((play / "OnsetTap.java").read_text(encoding="utf-8"))
    zero_code = strip_java_comments((play / "OnsetZero.java").read_text(encoding="utf-8"))

    assert "媒体音量为 0" not in tap_code, "『音量 0 就中止』的文案又回来了 ⇒ 硬闸门被写回"

    parts = tap_code.split("public String mediaVolumeNote()", 1)
    assert len(parts) == 2, "OnsetTap 里找不到 mediaVolumeNote()（诊断被整个删了？）"
    head, tail = parts
    assert "getStreamVolume" not in head, "mediaVolumeNote() 之外还在读音量 ⇒ 又被当成判据"
    assert "getStreamVolume" in tail, "mediaVolumeNote() 必须真的读音量（否则日志没信息）"
    # 诊断函数里也不许抛：诊断的职责是记事，不是否决
    note_body = tail.split("\n    }", 1)[0]
    assert "throw" not in note_body, "mediaVolumeNote() 里出现了 throw ⇒ 诊断又变回闸门"

    assert "signalVerdict()" in zero_code, \
        "OnsetZero 的失败文本不再引用实测结论（signalVerdict）⇒ 会退回让人猜音量"


def test_manifest_declares_shizuku_provider_with_package_authority():
    """authority 必须是 `<package>.shizuku`；写错 = pingBinder 永远 false 且零报错。"""
    man = (ROOT / "android/play/app/src/main/AndroidManifest.xml").read_text(encoding="utf-8")
    assert re.search(r'package="(io\.pgauto\.play)"', man)
    assert 'android:authorities="io.pgauto.play.shizuku"' in man
    assert 'android:name="rikka.shizuku.ShizukuProvider"' in man


# ------------------------------------------------- [5] S1-c 前台服务（静默坑）

PLAYSVC = ROOT / "android/play/app/src/main/java/io/pgauto/play/PlayService.java"
PLAYMAN = ROOT / "android/play/app/src/main/AndroidManifest.xml"

def test_manifest_declares_special_use_foreground_service():
    """`<service>` 的三段缺一不可，而且**都不会在构建期报错**：

    ① 没声明 service ⇒ 点「开始」什么也不发生（运行期才炸）；
    ② 缺 foregroundServiceType ⇒ Android 14 上 startForeground 抛 SecurityException；
    ③ specialUse 缺 `<property>` 说明 ⇒ 上架被拒（协议要求）。

    ⚠ 这里检查**源码 manifest**；编译后的 AXML（含 type 的整数值）由
      `tools/check_play_apk.py` 验 —— 两步互补：这一步抓"源码里根本没有"，
      那一步抓"aapt2 编出来的位不对 / 被别的改动覆盖掉"。

    ★ S1-d 起 type 是**两个并存**：specialUse | mediaProjection。
      所以判据不能再用 `foregroundServiceType="specialUse"` 做精确串匹配
      （加第二位就红，而那是合法且必需的改动），改成按 `|` 切分后**集合包含**。
    """
    man = PLAYMAN.read_text(encoding="utf-8")
    assert 'android:name=".PlayService"' in man, 'manifest 里没有 <service android:name=".PlayService">'
    m = re.search(r'android:foregroundServiceType="([^"]*)"', man)
    assert m, "service 缺 foregroundServiceType（A14 上 startForeground 会抛 SecurityException）"
    types = {t.strip() for t in m.group(1).split("|")}
    assert "specialUse" in types, f"foregroundServiceType={m.group(1)} 不含 specialUse"
    assert "mediaProjection" in types, (
        f"foregroundServiceType={m.group(1)} 不含 mediaProjection"
        "（A14 上 getMediaProjection 会抛 SecurityException）")
    assert "android.app.PROPERTY_SPECIAL_USE_FGS_SUBTYPE" in man, \
        "specialUse 服务缺 <property name=PROPERTY_SPECIAL_USE_FGS_SUBTYPE>"
    assert "android.permission.FOREGROUND_SERVICE_SPECIAL_USE" in man, \
        "缺 FOREGROUND_SERVICE_SPECIAL_USE 权限（Android 14 起必须声明）"
    assert "android.permission.FOREGROUND_SERVICE_MEDIA_PROJECTION" in man, \
        "缺 FOREGROUND_SERVICE_MEDIA_PROJECTION 权限（A14 上 FGS 带 mediaProjection 类型必需）"
    assert "android.permission.RECORD_AUDIO" in man, \
        "缺 RECORD_AUDIO（AudioPlaybackCapture 的捕获方必须持有）"
    assert "android.permission.WAKE_LOCK" in man, "缺 WAKE_LOCK（160 s 注入期间 CPU 会被降频）"


def test_play_service_startforeground_type_matches_manifest():
    """★ 代码与 manifest 的 type 必须一致 —— 不一致在 Android 14 上抛 SecurityException，
    表现是「点开始没反应」，logcat 里只有一行 SecurityException，很容易被当成没接上。

    判据是代码里**真的传了** `FOREGROUND_SERVICE_TYPE_SPECIAL_USE`，而不是"manifest 写了"。
    ★ S1-d 追加：`mediaProjection` 这一位也必须**成对**存在。manifest 声明了但代码不传
      ⇒ 服务起来了、授权框也弹了，`getMediaProjection` 仍抛 SecurityException
      （A14 查的是「当前在跑的 FGS 类型」），错误落点离原因很远。
    """
    assert PLAYSVC.exists(), f"缺 {PLAYSVC}"
    src = PLAYSVC.read_text(encoding="utf-8")
    assert "ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE" in src, \
        "PlayService 没有给 startForeground 传 SPECIAL_USE 类型"
    assert "ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PROJECTION" in src, \
        "PlayService 没有给 startForeground 传 mediaProjection 类型"
    assert "startForeground(" in src, "PlayService 没有调用 startForeground（FGS 5 s 内必须调）"


def test_songs_have_unique_name_diff_pairs(manifest):
    """第 ② 步（选难度）按 (name, diff) 展示；重复会让用户看到两张一模一样的卡片。

    `name` 同时是**分组键**（先选歌、再选难度），所以它必须存在且非空 ——
    少了它所有曲目会塌成一个组，界面看起来"只有一首歌"。
    """
    seen = {}
    for s in manifest["songs"]:
        assert isinstance(s.get("name"), str) and s["name"].strip(), \
            f"{s['key']} 缺 name（分组键为空会让所有曲目塌成一组）"
        key = (s["name"], s["diff"])
        assert key not in seen, f"{s['key']} 与 {seen[key]} 同名同难度 {key}"
        seen[key] = s["key"]


def test_song_names_are_already_sortable(manifest):
    """界面按曲名 A→Z 排序；这里只钉住「排序键是纯 ASCII 曲名」这一前提。

    ★ 不重复实现 Java 的比较器（教训 9：同一个量两处实现必然分叉）。
      真正要防的是**曲名里混进控制字符/前后空格** —— 那样排序会把某首歌顶到最前或最后，
      看起来像"排序坏了"。真机顺序已用截图核对：Bounded / Credits / Cristalisia / Hardcore。
    """
    for s in manifest["songs"]:
        n = s["name"]
        assert n == n.strip(), f"{s['key']} 的曲名首尾有空白：{n!r}"
        assert all(ch.isprintable() for ch in n), f"{s['key']} 的曲名里有不可打印字符：{n!r}"


# ------------------------------------------------ [2] Shizuku 授权入口（10-24）

PLAYACT = ROOT / "android/play/app/src/main/java/io/pgauto/play/MainActivity.java"


def test_play_can_ask_shizuku_permission():
    """★ 10-24 两枪的教训：play 的界面里**没有** Shizuku 授权入口。

    injector 从 S0 起就有「① 申请权限」，play 一直没移植 ⇒ 真注入必然在
    `prepare()` 的 `gateShizuku()` 被挡下（日志：`checkSelfPermission ⇒ perm=-1`），
    而人只看到一句「阶段：失败」，看不出是缺授权。

    ⛔ `adb shell pm grant io.pgauto.play moe.shizuku.manager.permission.API_V23`
       **不是替代方案**：命令返回成功、`dumpsys package` 也显示 `granted=true`，
       但 `Shizuku.checkSelfPermission()` 仍然返回 -1（force-stop 重启进程后仍是 -1）。
       Shizuku 的授权表由它自己维护，Android 的 permission 是另一本账 —— pm grant 假绿。
       所以必须有**界面内**的 `requestPermission()`。

    钉四件事（缺一条就等于没接通）：

      ① 申请入口存在：`Shizuku.requestPermission(REQ_SHIZUKU)`；
      ② 结果回调已注册：`addRequestPermissionResultListener(permListener)`
         —— 不注册 = 弹了窗但永远不知道用户点了什么；
      ③ 对称解注册（`onDestroy` 里 remove），否则回调随静态 list 泄漏；
      ④ 入口**真的挂到界面上**：`content.addView(shizukuPermRow())`
         —— 写了方法却没 addView 等于没写（本项目教训：注释声明 ≠ 被测到）。

    变异验证：删掉 `content.addView(shizukuPermRow());` ⇒ ④ 红。
    """
    assert PLAYACT.exists(), f"缺 {PLAYACT}"
    code = strip_java_comments(PLAYACT.read_text(encoding="utf-8"))

    assert "requestPermission(REQ_SHIZUKU)" in code, "没有申请入口 ⇒ 真注入永远过不了门禁"
    assert "addRequestPermissionResultListener(permListener)" in code, \
        "没有注册授权结果回调 ⇒ 用户点了「允许」也收不到结果"
    assert "removeRequestPermissionResultListener(permListener)" in code, \
        "onDestroy 没有解注册 ⇒ 授权回调泄漏在 Shizuku 的静态 list 里"
    # ★ 2026-11-01：挂载写法多了 wrapTop(...) 包一层（要加间距），所以这里放宽到
    #   「content 直接或经 wrapTop 挂上去」，仍能抓住"写了但没挂到界面"这个真问题。
    assert re.search(r"content\.addView\((wrapTop\()?shizukuPermRow\(\)", code), \
        "授权入口写了但没挂到界面上（跟没写一样）"

    # 三种状态必须分开说：binder 未就绪 / 未授权 / 已授权，修法完全不同
    assert "pingBinder()" in code, "没有区分『Shizuku 服务没连上』和『连上但没授权』"
    assert "checkSelfPermission()" in code, "没有读授权状态 ⇒ 按钮文案会一直是初始值"
    # 未就绪时不许降级成「已授予」
    perm_fn = code.split("private static boolean shizukuGranted()", 1)
    assert len(perm_fn) == 2, "缺 shizukuGranted()（授权状态的唯一判据）"
    body = perm_fn[1].split("\n    }", 1)[0]
    assert "catch (Throwable" in body and "return false" in body, \
        "shizukuGranted() 在抛异常时必须返回 false（缺权限 ≠ 有权限）"


ONSETTAP = ROOT / "android/play/app/src/main/java/io/pgauto/play/OnsetTap.java"


def test_projection_callback_gets_an_explicit_looper_handler():
    """★ 10-24 第三枪的教训：`registerCallback(cb, null)` 在没有 Looper 的线程上必抛。

    `OnsetTap.startInner()` 跑在服务起的 `pg-play` 线程上。`MediaProjection.registerCallback`
    的 handler 传 null 时会用**调用线程**的 Looper 去 `new Handler()` ⇒
    `RuntimeException: Can't create handler inside thread Thread[pg-play,5,main]
     that has not called Looper.prepare()`。

    症状极具误导性：日志里 8 s 倒计时**一行不差**地走完，紧接在
    「媒体音量 0/16（仅诊断）」之后炸 ⇒ 看起来像音量/采集的问题，实际与音量无关。
    （这是「注释声明的契约 ≠ 被测到的契约」的又一次；也是本文件里第二条只能靠真机
    才能暴露的时序 bug，前一条是 OnsetWait 的竞态。）

    钉法：`registerCallback` 之后 300 字符内不许出现 `, null)`，且必须出现带 Looper 的 Handler。
    变异：把 `new Handler(Looper.getMainLooper())` 换回 `null` ⇒ 红。
    """
    code = strip_java_comments(ONSETTAP.read_text(encoding="utf-8"))
    assert "registerCallback" in code, "OnsetTap 没有 registerCallback ⇒ 投影被撤销时无人处理"
    seg = code.split("registerCallback", 1)[1][:300]
    assert ", null)" not in seg, \
        "registerCallback 的 handler 又传了 null ⇒ 在 pg-play 线程上必抛 RuntimeException"
    assert "Looper.getMainLooper()" in seg, "handler 必须显式绑定主线程 Looper"


INJECTOR = ROOT / "android/injector/app/src/main/java/io/pgauto/inject/Injector.java"
ZERO_SOURCE = ROOT / "android/play/app/src/main/java/io/pgauto/play/ZeroSource.java"


def test_tap_event_carries_touchscreen_source():
    """★ 10-24 第四枪的教训：`injectInputEvent` 返回 true **不等于**事件被派发。

    6 参 `MotionEvent.obtain(downTime, eventTime, action, x, y, meta)` 造出来的事件
    `source = SOURCE_UNKNOWN(0)`、toolType = UNKNOWN。`injectInputEvent` 对它照样返回 true
    —— true 只代表「成功入队」—— 但事件不会被派发给前台窗口。症状：

        点击 ▶ 成功：注入完成 (1522.7,615.5)  down=true up=true     ← 日志一片绿
        …游戏一动不动 ⇒ 采集到的 20 s 是选歌界面 BGM ⇒
        gate(state=WAIT_QUIET, 最长静音 10 帧 < 需要 30 帧) ⇒ onset 超时

    看起来像「onset 判据不行」，实际是「根本没开始打」。

    ★ 对照：整曲注入（`DevRun`）一直按 `obtain(…, SOURCE_TOUCHSCREEN, 0)` + `setSource` +
      `toolType=FINGER` 造事件，所以它能打通整首曲子（bounded_in 1286 全 P）。
      两条路径必须一致。

    变异：把 down/up 换回 6 参 `obtain` ⇒ 本条红。
    """
    assert INJECTOR.exists(), f"缺 {INJECTOR}"
    code = strip_java_comments(INJECTOR.read_text(encoding="utf-8"))

    assert "MotionEvent.obtain(t0, t0, MotionEvent.ACTION_DOWN, x, y, 0)" not in code, \
        "tap 又用回了 6 参 obtain ⇒ source=UNKNOWN，事件不会被派发给游戏"
    assert "SOURCE_TOUCHSCREEN" in code, "事件没有 TOUCHSCREEN source"
    assert "TOOL_TYPE_FINGER" in code, "事件没有 toolType=FINGER（Unity 侧可能不认）"
    assert code.count("tapEvent(") >= 3, \
        "down/up 必须都走 tapEvent（1 处定义 + 2 处调用）"
    # setSource 必须真被调（只写进 obtain 参数不够 —— 历史经验：注释声明 ≠ 实际调用）
    assert ".setSource(InputDevice.SOURCE_TOUCHSCREEN)" in code, \
        "没有显式 setSource（只信 obtain 的重载参数不够稳妥）"


def test_tap_matches_the_last_known_good_run():
    """★★ 2026-10-30 22:45 回退：单击必须与 `run_155441`（build 100a0dc，939 音符全发）时一致。

    事故经过（写给未来的自己）：
      21:5x 起"点 ▶ 没反应"，我先后给出四个假设：① 点偏 ② 4 秒没切回游戏
      ③ 游戏转场未就绪 ④ DOWN/UP 挤在同一帧 ⇒ Unity 丢点击。前三个都被数据否掉，
      第四个我照着改了代码（`MODE_WAIT_FOR_FINISH` + 真 sleep 50 ms + 真实 eventTime）。

    ★ 但假设 ④ 被**自己的历史证据**否证：`run_155441.log`（100a0dc）用 `MODE_ASYNC` +
      `eventTime` 只差 20 ms（真实间隔 1~5 ms）就**点中了** ▶、采到 onset、939 音符全发。
      同帧若真会丢点击，那一轮不可能成功。
      ⇒ 改动没有依据，改完反而"按推荐值也点不动"，已整体回退到 100a0dc。

    这条契约把「唯一有成功实绩的实现」钉死：以后想再动它，先拿真机证据。

    变异：`MODE_ASYNC` 改 `WAIT_FOR_FINISH` / 加 sleep ⇒ 红。
    """
    code = strip_java_comments(INJECTOR.read_text(encoding="utf-8"))
    tap_body = code[code.index("public static String tap("):]
    assert tap_body.count("MODE_ASYNC") == 2, \
        "tap 必须两次都用 MODE_ASYNC（与 run_155441 成功轮一致）；要改先有真机证据"
    assert "MODE_WAIT_FOR_FINISH" not in tap_body, \
        "tap 别用同步模式 —— 改成它之后出现过『按推荐值也点不动』，已回退"
    assert "SystemClock.sleep" not in tap_body, \
        "tap 里不要 sleep：成功轮就是连发的，没有证据说需要跨帧"
    assert "按住 " not in tap_body, \
        "返回串别再带『按住 N ms』—— 那是已回退版本的特征，用户会拿它判断版本"


def test_tap_result_prefix_unchanged_for_caller():
    """`Injector.tap` 的返回串加了「按住 N ms」，但**前缀没变**。

    `ZeroSource.ShizukuTap.tap` 靠 `startsWith("注入完成")` 判成功、
    靠 `down=false / up=false` 判被拒（mode 写错时 invoke 返回 false 而不抛异常，
    10-20 实测 600/600 全 false）。改返回串格式时这两条不能破 —— 破了就会
    把「失败」当成「成功」，正是本文件反复出现的那类静默错。

    变异：把前缀改成「完成」或把 down= 后缀去掉 ⇒ 本条红。
    """
    zs = ZERO_SOURCE.read_text(encoding="utf-8")
    assert 'r.startsWith("注入完成")' in zs, \
        "ShizukuTap 判成功用的前缀变了 ⇒ 注入失败会被当成成功"
    assert 'r.contains("down=false")' in zs and 'r.contains("up=false")' in zs, \
        "ShizukuTap 不再检查 down/up 两个布尔 ⇒ mode 写错时看不出失败"

    code = strip_java_comments(INJECTOR.read_text(encoding="utf-8"))
    assert '"注入完成 (" + x + "," + y + ")' in code, \
        "返回串前缀被改了（调用方按 startsWith 判成功）"
    assert '"down=" + r1 + " up=" + r2' in code or 'down=" + r1' in code, \
        "返回串里必须带 down=/up= 两个布尔"


# ══════════════════════════════════════════════════════════════════════════
# 2026-10-30 · 采集器泄漏：runOnce 的**异常路径**不收 tap
#
# 事故链（现读代码可证）：
#   buildZeroSource() 里 keepTapAlive(auto)（autoCalib 默认开）
#   ⇒ OnsetZero.finish() 的 finally 跳过 tap.stop()
#   ⇒ 全进程另一处 job.tap.stop() 在 finishAutoCalib()，写在 try 块**末行**
#   ⇒ 任何异常/中止（零点超时 / inject 抛 / Shizuku 挂 / 用户点停止）都跳过它
#   ⇒ AudioRecord 不 release（占麦克风）、MediaProjection 不 stop（状态栏一直显示录屏）
#   ⇒ 挂到进程重建 ⇒ 下一轮 new OnsetTap 可能失败 ⇒ 零注入
# ══════════════════════════════════════════════════════════════════════════

def test_capture_is_released_on_exception_path():
    """★ runOnce 的 finally 必须**无条件** stop tap。

    变异：删掉 finally 里的 `job.tap.stop()` ⇒ 本条红。
    """
    code = strip_java_comments(PLAYSVC.read_text(encoding="utf-8"))
    assert "private void runOnce(" in code, "找不到 runOnce"
    seg = code.split("private void runOnce(", 1)[1]
    assert "} finally {" in seg, "runOnce 没有 finally 块"
    head = seg.split("} finally {", 1)[1][:900]
    assert "job.tap.stop()" in head, \
        "runOnce 的 finally 没有 stop 采集器 ⇒ 异常轮会泄漏 AudioRecord / MediaProjection"
    assert "job != null && job.tap != null" in head, \
        "finally 里没先判 null：calib 分支会提前 return，那时 job 仍是 null"


def test_job_is_declared_before_try():
    """`job` 必须声明在 try 之外，否则 finally 够不到它 —— 上面那条修复会形同虚设。

    变异：把 `Job job = null;` 移回 try 内 ⇒ 本条红。
    """
    code = strip_java_comments(PLAYSVC.read_text(encoding="utf-8"))
    seg = code.split("private void runOnce(", 1)[1]
    before_try = seg.split("try {", 1)[0]
    assert "Job job = null;" in before_try, \
        "Job job 没声明在 try 之前 ⇒ finally 里拿不到 tap 引用"
    assert "Job job = prepare(" not in seg, \
        "Job job 又在 try 内重复声明了（会遮蔽外层，finally 里的仍是 null）"


# ── 2026-10-30 · 手势区内缩改成按屏幕比例 ─────────────────────────────────────
# 原来是 GESTURE_TOP_PX=92 / BOTTOM_PX=92 / SIDE_PX=12，三个**绝对像素**，全部来自
# HONOR ALT-AN00（1610×720）的实测。换到 3136×1440 上仍按 92 夹 ⇒ 顶部/底部各 92 px
# 失去保护，落在那里的**按下点**会触发系统手势（上滑回桌面 / 下拉通知栏）。

SCHEDULER = ROOT / "android/injector/app/src/main/java/io/pgauto/inject/sched/Scheduler.java"


def test_gesture_inset_is_relative_not_absolute():
    """★ 内缩必须是比例常量，不许退回绝对像素。

    变异：把 `GESTURE_TOP_REL` 改回 `GESTURE_TOP_PX` ⇒ 本条红。
    """
    code = strip_java_comments(SCHEDULER.read_text(encoding="utf-8"))
    for bad in ("GESTURE_TOP_PX", "GESTURE_BOTTOM_PX", "GESTURE_SIDE_PX"):
        assert bad not in code, \
            f"{bad} 又回来了：绝对像素在换机后会比例失真（HONOR 92px = 12.8%，1440 屏上只有 6.4%）"
    for rel in ("GESTURE_TOP_REL", "GESTURE_BOTTOM_REL", "GESTURE_SIDE_REL"):
        assert rel in code, f"缺 {rel}"


def test_gesture_rel_is_derived_from_base_resolution():
    """比例必须由 **1610×720 的旧常量** 反推，不能拿实测值（74/80）反推。

    为什么这条重要：用 74 反推会丢掉 18 px 安全余量 —— 等于悄悄改了旧设备的行为，
    而 `GestureClampSelfTest` 的 4 条精确断言（92/628/12/1598）正是钉这件事的。
    """
    code = strip_java_comments(SCHEDULER.read_text(encoding="utf-8"))
    assert "92.0 / 720.0" in code, "TOP/BOTTOM 比例必须以 92.0 / 720.0 的形式写（可核对的来源）"
    assert "12.0 / 1610.0" in code, "SIDE 比例必须以 12.0 / 1610.0 的形式写"
    assert "74.0 / 720.0" not in code, \
        "别用实测的 74 反推：会丢掉 18 px 余量，等于偷偷改旧设备行为"


# ── 2026-10-30 · ▶ 落点自检（换机用，零风险） ──────────────────────────────
# 实测：HONOR 1610×720 选曲页里，按钮白底板 x 1465..1610 / y 558..665，
# 而代码落点 (1522.7, 615.5) 距图标中心仅 4.8 / 2.5 px ⇒ 本来就点在正中。
# 所以这不是"调准坐标"，是**换到陌生机型时能提前看出会不会偏**。

STARTBOX = ROOT / "android/play/app/src/main/java/io/pgauto/play/StartButtonBox.java"
PLAYSELFTEST = ROOT / "android/play/selftest.sh"


def test_start_button_box_is_android_free():
    """必须纯 Java：`selftest.sh` 用没有 android.jar 的 classpath 编它。"""
    assert STARTBOX.exists(), f"缺 {STARTBOX}"
    code = strip_java_comments(STARTBOX.read_text(encoding="utf-8"))
    assert "import android." not in code, "StartButtonBox 引入了 android 依赖 ⇒ selftest 编不过"
    assert "SystemClock" not in code and "Log.i(" not in code, \
        "StartButtonBox 用了 Android API ⇒ 挪不动到离线自测"
    sh = PLAYSELFTEST.read_text(encoding="utf-8")
    assert "$SRC_MAIN/StartButtonBox.java" in sh, "selftest.sh 没编 StartButtonBox"
    assert "StartButtonBoxSelfTest" in sh, "selftest.sh 没跑 StartButtonBoxSelfTest"


def test_start_button_box_constants_are_traceable():
    """常量必须写成「实测像素 / 屏幕尺寸」，可核对来源。

    变异：把 1465.0/1610.0 换成裸小数 0.9099 ⇒ 本条红。
    """
    code = strip_java_comments(STARTBOX.read_text(encoding="utf-8"))
    for expr in ("1465.0 / 1610.0", "558.0 / 720.0", "665.0 / 720.0"):
        assert expr in code, f"常量必须以可核对的形式写：{expr}"


def test_verdict_reports_distance_not_binary():
    """★★ 2026-10-30 用户实测被旧判读带偏两次，这条钉住新口径。

    旧版只做一次矩形包含判断，落在框内就报 `在内 ✓`。用户填 dx −58 / dy −57：
    <pre>
      落点 1464.72, 558.50 ⇒ x 差 0.28px **在左边界外**（正压按钮左上角顶点）
      旧文案「在内 ✓（与边界差 &lt;1px，可视为对准）」
      ⇒ 唯一的警告被 0.5px 宽容吞掉 ⇒ 点不中、游戏一动不动，而用户以为位置已经对了
    </pre>
    新口径：三元档位（✓ 在框内 / ⚠ 贴边 / ✗ 出界）+「距中心多远」。

    ★ 2026-10-31 又改一次：手填偏移**已废弃**，所以判读**不许再说 dx/dy 该填多少**，
      一律指向「学习落点」。同时**不许再叫「对准」** —— 用户就是被这个词骗了一晚：
      这个框的 X0 实测 1465，而真实左边界是 1439（斜切边），框把宽度量小 26 px，
      它算的"中心"偏右 13 px，凭什么下"对/不对"的结论。

    变异（每条单独转红）：加回「可视为对准」/ 删掉贴边档 / 删掉 ⚠ / 删掉「距中心」/
      加回「dx 填」/ 把「✓ 在框内」改回「✓ 对准」。
    """
    code = strip_java_comments(STARTBOX.read_text(encoding="utf-8"))
    assert "可视为对准" not in code, \
        "又出现了「可视为对准」—— 正是它把 0.28px 的越界伪装成对准"
    assert "在内" not in code, \
        "又退回二元判断「在内」⇒ 表达不了「离中心多远」，贴边与正中心在它眼里一样"
    assert "⚠ 贴边" in code, "缺「贴边」档：在框内但余量不足时必须单独报警"
    assert "EDGE_WARN_PX" in code, "缺贴边阈值常量（写死数字以后没人知道为什么取这个值）"
    assert "距中心" in code, "判读必须给出「距中心多远」，不能只说在不在框里"
    assert "✓ 在框内" in code, "档位名应是「✓ 在框内」（描述事实），不是「✓ 对准」（下结论）"
    assert "✓ 对准" not in code, \
        "又写「✓ 对准」—— 框的 X0 量错 26px，落在框内不等于点得中"
    assert "dx 填 " not in code and "dy 填 " not in code, \
        "手填偏移已废弃 ⇒ 判读不许再让人填 dx/dy，应指向「学习落点」"
    assert "学习落点" in code, "判读必须告诉用户该怎么做：用「学习落点」重记一次"
    assert "targetDx(" in code and "targetDy(" in code, "缺 targetDx/targetDy（单一来源）"


def test_selftest_rel_copy_matches_production():
    """`StartButtonBoxSelfTest` 里的 `REL_X/REL_Y` 是 `PlayService.START_REL_X/Y` 的**副本**。

    为什么必须是副本：自测链刻意不带 android.jar，而 `PlayService` 依赖 Android ⇒ 引用不了。
    为什么必须有这条契约：副本一旦和生产值不同，自测就在验证**另一个坐标系** —— 全绿也没意义。

    变异：把副本改成 0.9458 ⇒ 红。
    """
    svc = strip_java_comments(PLAYSVC.read_text(encoding="utf-8"))
    selftest = ROOT / "android/play/app/src/test/java/io/pgauto/play/StartButtonBoxSelfTest.java"
    st = selftest.read_text(encoding="utf-8")

    def grab(text: str, name: str) -> float:
        m = re.search(name + r"\s*=\s*([0-9.]+)", text)
        assert m, f"找不到 {name}"
        return float(m.group(1))

    assert grab(svc, "START_REL_X") == grab(st, "REL_X"), \
        "自测里的 REL_X 副本与 PlayService.START_REL_X 不一致 ⇒ 自测验证的是另一个坐标系"
    assert grab(svc, "START_REL_Y") == grab(st, "REL_Y"), \
        "自测里的 REL_Y 副本与 PlayService.START_REL_Y 不一致"


def test_start_button_box_is_wired_and_advisory_only():
    """★ 必须真被调用，且**只许进日志** —— 不许据此改坐标或中止。

    P0.1 的教训：RunCore.exit() 写好了却没人调，自测全绿而产物全是 unknown。
    变异：删掉 PlayService 里那行 verdict 调用 ⇒ 本条红。
    """
    code = strip_java_comments(PLAYSVC.read_text(encoding="utf-8"))
    assert "StartButtonBox.verdict(" in code, "PlayService 没调用按钮框自检 ⇒ 换机时看不到判读"
    # 只许出现在 RunLog.line 的实参里
    for line in code.split("\n"):
        if "StartButtonBox.verdict(" in line:
            assert "RunLog.line(" in line, \
                f"verdict() 只能用于日志，不许参与决策：{line.strip()}"
    # 校验清单里也要有它
    chk = (ROOT / "tools/check_play_apk.py").read_text(encoding="utf-8")
    assert "io/pgauto/play/StartButtonBox" in chk, "check_play_apk.py 的关键类清单缺 StartButtonBox"


# ── 2026-10-30 · ▶ 位置：自测入口 + 手动微调 ────────────────────────────────
# 设计要点：偏移是**加在出厂比例之上的一层**（默认 0 = 与旧行为逐位相同），
# 而不是去改写 START_REL_X/Y —— 那两个常量必须是"出厂默认"的唯一来源。

MAINACT = ROOT / "android/play/app/src/main/java/io/pgauto/play/MainActivity.java"


def test_start_tap_has_single_source_of_truth():
    """★ 落点只许有**一个来源**：`StartPointPrefs`（学到的优先，否则出厂比例）。

    2026-10-31 改口径后，出厂比例的直乘从 `PlayService` 挪进了 `StartPointPrefs.tapX/tapY`
    —— 因为现在有"学到的 / 出厂"两档，判断必须只有一份。
    仍钉同一件事：**所有使用点都走 startTapX/Y，任何地方都不许再自己乘一次**。

    变异：把 `startTapX` 改回直接乘比例 ⇒ 本条红。
    """
    code = strip_java_comments(PLAYSVC.read_text(encoding="utf-8"))
    assert code.count("* START_REL_X") == 0, \
        "出厂比例不该再在 PlayService 里直乘（已挪进 StartPointPrefs，只留一份）"
    assert code.count("* START_REL_Y") == 0, "同上"
    assert code.count("startTapX(") >= 4, \
        "三处使用点 + 方法定义 ⇒ startTapX( 至少出现 4 次"
    assert code.count("startTapY(") >= 4, "同上"

    spp = strip_java_comments(
        (ROOT / "android/play/app/src/main/java/io/pgauto/play/StartPointPrefs.java")
        .read_text(encoding="utf-8"))
    assert spp.count("* PlayService.START_REL_X") == 1, \
        f"出厂比例必须在 StartPointPrefs 里只乘一次（当前 {spp.count('* PlayService.START_REL_X')} 次）"
    assert spp.count("* PlayService.START_REL_Y") == 1, "同上"

def test_start_offset_defaults_to_zero():
    """默认偏移必须是 0f —— 保证「不动设置 = 与改动前逐位相同」。

    ★ 2026-10-31 口径变了：手填偏移**整体废弃** —— `startTapX/Y` 只认「学到的 / 出厂比例」，
      所以 PlayService 里**一处都不许再读** `start_dx_px/dy_px`。
      这比"默认值是 0f"更强：读都不读，就谈不上"残留值偷偷生效"。

    保留的只有 prefs 里的 key 常量（`StartPointPrefs.clear` 要用它删老残留）。
    变异：在 PlayService 里加回任意一处 getFloat(KEY_START_DX, 0f) ⇒ 本条红。
    """
    code = strip_java_comments(PLAYSVC.read_text(encoding="utf-8"))
    for key in ("KEY_START_DX", "KEY_START_DY"):
        assert f"getFloat({key}" not in code, \
            f"{key} 已废弃，PlayService 不许再读（残留值会与学到的点叠加）"
    assert 'KEY_START_DX = "start_dx_px"' in code, "prefs key 名变了（老用户的设置会失效）"
    assert 'KEY_START_DY = "start_dy_px"' in code, "prefs key 名变了"


def test_test_tap_does_not_block_next_round():
    """★ 「测试点一次」绝不能占 worker —— 否则 `isAlive()` 会拒掉下一轮真打歌。

    变异：把测试点击改成赋值 worker ⇒ 本条红。
    """
    code = strip_java_comments(PLAYSVC.read_text(encoding="utf-8"))
    assert "ACTION_TEST_TAP" in code
    # 只取这个分支自己的体：从 ACTION_TEST_TAP 到它的 return 为止。
    # ⛔ 别用固定长度截取 —— 700 字符会把后面的忙碌门禁（`worker.isAlive()`）也框进来，
    #    测试会假红一次（2026-10-30 实际踩过）。
    seg = code.split("ACTION_TEST_TAP.equals(act)", 1)[1].split("return START_NOT_STICKY;", 1)[0]
    assert "new Thread" in seg, "测试点击应单独起线程"
    assert "worker =" not in seg, "测试点击不许赋值 worker ⇒ 会拒掉下一轮打歌"
    assert "setRunning" not in seg, "测试点击不许设 RunLog.setRunning（那不是一轮）"
    # 也不许写 phase
    assert "RunLog.phase(" not in seg, "测试点击不许改阶段（界面会假装在跑）"


def test_main_ui_has_calibration_entry():
    """界面上必须真的有「测试 ▶」和偏移入口，否则功能等于没做。"""
    code = strip_java_comments(MAINACT.read_text(encoding="utf-8"))
    assert "showStartDialog()" in code, "缺偏移对话框"
    assert "PlayService.ACTION_TEST_TAP" in code, "缺「测试 ▶」按钮的 intent"
    assert "恢复默认" in code, "偏移对话框缺「恢复默认」—— 换设备时必须能一键清"
    assert "±300" in code or "300" in code, "缺偏移范围校验（dx 可为负，别照抄 0~600）"
    doc = ROOT / "docs/start-button-calibration.md"
    assert doc.exists(), "缺教程 docs/start-button-calibration.md"
    assert "start-button-calibration.md" in (ROOT / "README.md").read_text(encoding="utf-8"), \
        "README 里没有指向教程的链接"


def test_test_tap_waits_before_firing():
    """★ 「测试 ▶」必须先等几秒再发点击 —— 按下的那一刻前台是**本 App**。

    这不是体贴用户，是功能能否成立的问题：立刻发 tap 会点在 App 自己的界面上
    （坐标正好在右下角），游戏毫无反应，于是「位置对不对」永远得不到答案，
    而教程还写着"点完看游戏是否加载"，等于教人一个永远失败的动作。

    变异：删掉 `Thread.sleep(TEST_TAP_LEAD_MS)`、或把常量改成 0 ⇒ 本条红。
    """
    code = strip_java_comments(PLAYSVC.read_text(encoding="utf-8"))
    assert "TEST_TAP_LEAD_MS" in code, "缺等待常量"
    m = re.search(r"TEST_TAP_LEAD_MS\s*=\s*([0-9_]+)L", code)
    assert m, "TEST_TAP_LEAD_MS 必须字面量声明（别用表达式，测试要能读到）"
    lead = int(m.group(1).replace("_", ""))
    assert lead >= 1500, f"等待太短（{lead} ms）：用户来不及切回游戏"

    seg = code.split("ACTION_TEST_TAP.equals(act)", 1)[1]
    body = seg.split("return START_NOT_STICKY", 1)[0]
    assert "Thread.sleep(TEST_TAP_LEAD_MS)" in body, \
        "测试点击没有等待期 ⇒ tap 落在本 App 界面上，游戏那边什么都没发生"
    assert "testTapOnce()" in body, "等待之后没调用真正的点击"
    # 顺序也要对：sleep 必须在 testTapOnce 之前
    assert body.index("Thread.sleep(TEST_TAP_LEAD_MS)") < body.index("testTapOnce()"), \
        "先点了再睡？顺序反了"
    # 中断要能放弃（否则一次测试点击可能变成意外连点）
    assert "InterruptedException" in body, "没处理中断 ⇒ 被中断后仍会点出去"


# ── 2026-10-30 · 教程要进 **App 内**，方向要在调之前就给 ──────────────────────
# 用户的两次反馈直接指向两个设计缺口：
#   ① 「我没教程我没看到你写啊」—— 教程只写在仓库 docs/ 里，手机上只有「教程/帮助」页；
#   ② 「点偏了我也不知道点到哪里去了，不知道往哪个方向调整」—— 判读只写日志，改数时是盲填。

def test_help_page_has_start_calibration():
    """★ 教程必须写进 **App 内的帮助页** —— 手机上打不开仓库里的 docs/。

    2026-10-31 改口径：手填偏移废弃，改成「学习落点」。帮助页必须同步，
    否则用户在 App 里看到的是"填 dx/dy"那套**已经不存在的操作**（比缺内容更糟）。
    """
    code = strip_java_comments(MAINACT.read_text(encoding="utf-8"))
    assert "点 ▶ 没反应" in code, "帮助页缺「点 ▶ 没反应」这一节"
    for frag in ("学习落点", "第一次", "测试 ▶", "显示触摸操作", "恢复默认", "4 秒内切回游戏"):
        assert frag in code, f"帮助页缺关键内容：{frag}"
    for gone in ("对准按钮中心", "dx 填", "dy 加大"):
        assert gone not in code, f"帮助页还在教已废弃的操作：{gone}"

def test_offset_dialog_shows_direction_before_edit():
    """对话框一打开就要显示**当前落点与来源**，且整块可滚。

    2026-10-31 改口径：不再"让人填数字"，改成「学习落点」+「恢复默认（0,0）」。
    仍钉两件事：① 打开就看到现在用的是哪个落点；② 内容不许溢出屏幕（10-30 事故回归守卫）。
    """
    code = strip_java_comments(MAINACT.read_text(encoding="utf-8"))
    assert "private String currentStartVerdict()" in code, "缺 currentStartVerdict()"
    seg = code.split("private void showStartDialog()", 1)[1][:2600]
    assert "currentStartVerdict()" in seg, \
        "对话框没显示当前落点 ⇒ 用户不知道现在用的是哪个"
    # ★★ 2026-10-30 事故回归守卫：setMessage + 自定义 Button 把对话框撑得比屏幕高，
    #    底部按钮行被挤出可视区（用户原话「保存按钮都没了，这个界面划不动啊」）。
    assert ".setMessage(" not in seg, \
        "对话框又用了 setMessage ⇒ 说明文字会再叠一层，底部按钮可能被挤出屏幕"
    assert "ScrollView" in seg and "sv.addView(box)" in seg, \
        "自定义视图必须包在 ScrollView 里，否则内容一多就划不动"
    assert ".setView(sv)" in seg, "setView 应传 ScrollView，不是裸 LinearLayout"
    assert "学习落点" in seg, "缺「学习落点」入口"
    assert "StartPointPrefs.clear" in seg, "缺「恢复默认（0,0）」（用户 12:20 明确要求保留）"
    assert "new android.widget.Button" not in seg, \
        "Button 有 48dp 最小高度，正是它把按钮行顶出去 ⇒ 必须是可点 TextView"

def test_landscape_size_ignores_current_orientation():
    """MainActivity 是**竖屏**：用它自己的 DisplayMetrics 会得到 720×1610。"""
    code = strip_java_comments(MAINACT.read_text(encoding="utf-8"))
    assert "private int[] landscapeSize()" in code, "MainActivity 缺 landscapeSize()"
    seg = code.split("private int[] landscapeSize()", 1)[1][:800]
    assert "getRealSize" in seg, "必须用 Display.getRealSize（与屏幕方向无关）"
    assert "getDisplayMetrics().widthPixels" not in seg, \
        "用了当前方向的 DisplayMetrics ⇒ 竖屏下宽高会反，落点算错"


def _java_method(code: str, sig: str) -> str:
    """截出 Java 方法体：从签名到下一个 4 空格缩进的右花括号（方法结束）。"""
    i = code.index(sig)
    j = code.index("\n    }", i)
    return code[i:j]


def test_k_input_accepts_decimals():
    """★ K 是浮点量（自动标定会写 +0.1），键盘必须带小数点。

    2026-10-30 用户反馈：「小数点似乎打不出来，只能设置为 1」。
    根因：`setInputType` 只给了 `TYPE_CLASS_NUMBER | TYPE_NUMBER_FLAG_SIGNED`。
    而且有两个入口（顶部 K 弹窗、课题页 K/lead 双改弹窗），**必须两处都改** ——
    只改一处的话，用户在另一个入口仍然打不出小数点。

    变异：删掉任意一处 `TYPE_NUMBER_FLAG_DECIMAL` ⇒ 本条红。
    """
    ma = strip_java_comments(MAINACT.read_text(encoding="utf-8"))
    for sig, where in (("private void showKDialog(", "顶部 K 弹窗"),
                       ("private void showKLeadDialog(", "课题页 K/lead 弹窗")):
        body = _java_method(ma, sig)
        assert "TYPE_NUMBER_FLAG_DECIMAL" in body, \
            f"{where}的 K 输入框没有 TYPE_NUMBER_FLAG_DECIMAL ⇒ 小数点打不出来"
        # 只加 DECIMAL 忘了 CLASS_NUMBER 会变成纯文本键盘（字母都能打）——一起钉住
        assert "TYPE_CLASS_NUMBER" in body, f"{where}丢了 TYPE_CLASS_NUMBER"


def test_k_clear_covers_every_key_it_reads():
    """★ 2026-10-30 真机 bug：点「清除」toast 说已清除，界面上那一行 K 还在。

    根因是**读的 key 集合比删的大**：界面 `calibK` 会遍历整组变体的 `k_ms_<key>`，
    而 `clearK` 只删了当前难度那一个 ⇒ 兄弟难度那一份把值顶回来。
    更糟的是服务侧 `kStored` 从不遍历变体 ⇒ 界面显示的 K ≠ 打歌实际用的 K。

    契约（三条缺一不可）：
      ① `clearK` 不许自己列 key（必须走 `CalibKeys.removeKeys`，它删整组）
      ② `calibK` 不许遍历 `variants()`（读取口径必须与服务一致）
      ③ `kStored` 必须走 `CalibKeys.readKeys`

    变异（都已实测转红）：把 `clearK` 改回手写 key 列表 ⇒ ① 红；
    把 `calibK` 的变体循环加回来 ⇒ ② 红；把 `kStored` 改回嵌套 getFloat ⇒ ③ 红。
    """
    ma = strip_java_comments(MAINACT.read_text(encoding="utf-8"))
    svc = strip_java_comments(PLAYSVC.read_text(encoding="utf-8"))

    clear_body = _java_method(ma, "private String clearK(")
    assert "CalibKeys.removeKeys" in clear_body, \
        "clearK 没走 CalibKeys.removeKeys ⇒ 迟早漏删某一档难度"
    assert '"k_ms_' not in clear_body, \
        "clearK 又在自己列 key 字面量 ⇒ 与读取集脱钩（10-30 那个 bug 的形状）"

    calib_body = _java_method(ma, "private Float calibK(")
    assert "CalibKeys.readKeys" in calib_body, "calibK 没走统一 key 契约"
    assert "variants()" not in calib_body, \
        "calibK 又在遍历兄弟变体 ⇒ 界面会显示一个服务根本不用的值，且清除后仍能读到"

    kstored_body = _java_method(svc, "private float kStored(")
    assert "CalibKeys.readKeys" in kstored_body, \
        "kStored 没走统一 key 契约 ⇒ 界面与实际打歌可能读不同的值"


def test_k_migration_is_one_shot_and_drops_old_keys():
    """★ 迁移必须**只跑一次**，且迁完删掉旧 key —— 否则清除效果活不过一次冷启动。

    2026-10-30 现场：用户点清除后 K 变空，重开 App 又变回 +5.5。
    因为 `migrateCalibKeys()` 挂在 `onCreate` 上每次冷启动都跑，且只复制不删旧 key。

    变异：删掉 `MIGRATE_K_FLAG` 判断 ⇒ 本条红。
    """
    ma = strip_java_comments(MAINACT.read_text(encoding="utf-8"))
    body = _java_method(ma, "private void migrateCalibKeys(")
    assert "MIGRATE_K_FLAG" in body, \
        "迁移没有一次性标记 ⇒ 每次冷启动都会把用户清掉的 K 从旧 key 搬回来"
    assert body.count("PrefGuard.remove") >= 1, "迁移没删旧 key ⇒ 它永远是复活源"
    assert "migrate_k_v2" in ma, "标记名丢了（改名会让已迁移的用户再迁一次）"


# ─────────── ▶ 落点学习（2026-10-31 用户定：用户点一次 → 记第一次点击） ───────────


def test_start_point_single_source():
    """落点只能有**一条**来源：startTapX/Y 必须走 StartPointPrefs，不许再读 dx/dy。

    为什么钉：手填偏移已废弃，但 prefs 里可能还留着 `start_dx_px=+15`。
    只要 startTapX 还读它，学到的点就会被悄悄加偏移 —— 正是 10-30 K bug 的形状
    （界面读一套、实际用一套），而且这次是**两个都能用的来源**打架，更难发现。
    """
    ps = strip_java_comments(PLAYSVC.read_text(encoding="utf-8"))
    for name in ("float startTapX(", "float startTapY("):
        body = _java_method(ps, name)
        assert "StartPointPrefs.tap" in body, f"{name} 必须走 StartPointPrefs（单一来源）"
        assert "KEY_START_DX" not in body and "KEY_START_DY" not in body, \
            f"{name} 又去读 dx/dy 了 ⇒ 学到的点会和残留偏移叠加"


def test_start_dialog_has_no_manual_input():
    """偏移对话框不许再有输入框：用户 10-31 定「暂不支持自己填写坐标」。"""
    ma = strip_java_comments(MAINACT.read_text(encoding="utf-8"))
    body = _java_method(ma, "private void showStartDialog(")
    assert "EditText" not in body, "偏移对话框不该再有输入框（手填已废弃）"
    assert "KEY_START_DX" not in body, "不许再写 dx/dy"
    assert "ACTION_LEARN_TAP" in body, "必须能触发「学习落点」"
    assert "StartPointPrefs.clear" in body, "必须保留「恢复默认」到 0,0（用户 12:20 要求）"


def test_start_verdict_does_not_rejudge_box():
    """界面不许再拿 `StartButtonBox` 下"对准没对准"的结论。

    依据（2026-10-31 实测）：`X0` 记的 1465/1610 不是按钮左边界，真实左边界是 1439（斜切边），
    框把宽度量小 26 px ⇒ 它算的"中心"偏右 13 px，曾把压在按钮左上角顶点的点判成「在内 ✓」。
    """
    ma = strip_java_comments(MAINACT.read_text(encoding="utf-8"))
    body = _java_method(ma, "private String currentStartVerdict(")
    assert "StartButtonBox" not in body, "框判读必须从界面撤掉（框本身量错了 26px）"
    assert "* PlayService.START_REL" not in body, "不许自己乘比例（落点只有一个来源）"
    assert "StartPointPrefs" in body, "必须走 StartPointPrefs"


def test_log_panel_shows_all_lines():
    """日志面板能看本轮**全部**行：取 `RunLog.all()`，且不再有 2 行 / 16 行的显示上限。

    依据（2026-11-01 用户定）：收起 = 整块不占位（只剩标题行）；展开 = 固定高度 + 内部滑动。
    旧的 `tail(16)` 会让一轮约 40 行里前 24 行在界面上**永远看不到**（只能翻 logcat），
    这是"看起来只是刷个日志"的功能盲区。
    """
    ma = strip_java_comments(MAINACT.read_text(encoding="utf-8"))
    assert "RunLog.all()" in ma, "日志面板没有取全部行（改回 tail(N) 就会红）"
    assert "LOG_TAIL_" not in ma, "2 行 / 16 行的显示上限常量还在"
    assert "RunLog.tail(" not in ma, "界面不许再按行数截断日志"
    assert "LogScroll" in ma, "缺固定高度滚动容器"
    assert "LOG_BOX_DP" in ma, "缺固定高度常量"
    # 收起必须是「整块不占位」，不是「只显示最后几行」
    body = _java_method(ma, "private View logPanel(")
    assert "GONE" in body, "收起态必须整块 GONE（否则仍占高度，一屏放不下按钮）"
    assert "LOG_BOX_DP" in body, "固定高度必须落在日志块的 LayoutParams 上"
    # 2026-11-01 真机：可选中文本会吃掉 MOVE ⇒ 日志块整块滑不动；用户也不要长按复制
    assert "setTextIsSelectable(true)" not in body, \
        "日志块不许可选中：它会吃掉 MOVE 让日志滑不动，还会抢焦点把整页滚过去"
    # 2026-11-01 真机：内容不足一屏时 atBottom 恒真 ⇒ 任何新日志都把视图拉到底
    refresh = _java_method(ma, "private void refreshLogNow(")
    assert "atBottom" not in refresh, \
        "不许再按「是否贴底」跟随刷新（内容不足一屏时恒真，点三树自检就会滚到底）"
    assert "logStickBottom" in refresh, "贴底只允许发生在「刚展开」那一刻"


def test_self_test_does_not_rebuild_page():
    """点「三树自检」不许整页 `render()`：重建会重算外层 ScrollView 的滚动位置。

    2026-11-01 真机反馈：页面滑到最底部时点自检，整页会动一下 —— 根因就是
    `runSelfTest()` 里为了显示"自检中…"调了 `render()`。自检结果只影响一个 TextView，
    直接 `setText` 就够。
    """
    ma = strip_java_comments(MAINACT.read_text(encoding="utf-8"))
    body = _java_method(ma, "private void runSelfTest(")
    assert "render()" not in body, "自检不许整页 render（页面滚动位置会被重算）"
    assert "tvSelfTest.setText" in body, "自检结果必须写进那个 TextView 而不是重建视图"
    # 2026-11-01 用户要求：自检结果也不许长按选取（会弹选择手柄）
    assert "tvSelfTest.setTextIsSelectable(true)" not in ma, "自检结果不许可选中"
