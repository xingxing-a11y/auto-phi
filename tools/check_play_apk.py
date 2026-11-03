#!/usr/bin/env python
"""核对 play APK 的内容清单 —— 装到设备之前能查的全查掉。

为什么需要它（三种「构建成功、装机失败」的形态，`build.sh` 自己都不会报）：
  ① **assets 没进去**：`aapt2 link` 少了 `-A` ⇒ APK 里没有 manifest.json，
     装完进界面才「曲库读取失败」。aapt2 全程静默。
  ② **跨包类没进 dex**：三树同编时只要漏一个源文件，`javac` 未必报错
     （没被引用就不编），但 `Components` 会在真机 NoClassDefFoundError。
  ③ **manifest 被改写**：Shizuku 的 provider authority 必须是
     `<package>.shizuku`，写错 = `pingBinder()` 永远 false 且**零报错**。
  ④ **图标没生效 / 被改坏**：manifest 缺 `android:icon`、adaptive 两层指向同一个
     资源、或 aapt2 的 PNG crunch 把图有损量化 —— 三者都是「装上去能跑，只是桌面
     图标不对」，前面八组的判据全绿。

判据全部是「逐字节」或「字符串存在」，没有抽样。

用法：
  .venv/Scripts/python.exe tools/check_play_apk.py [apk 路径]
默认 apk = android/play/build/pgplay.apk
"""
from __future__ import annotations

import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_APK = ROOT / "android/play/build/pgplay.apk"
ASSETS_DIR = ROOT / "android/play/app/src/main/assets"

#: 必须出现在 classes.dex 里的类（每个都对应 M1 某一步，少一个就那一步跑不了）。
REQUIRED_CLASSES = (
    "io/pgauto/play/MainActivity",       # 屏 1：曲库页
    "io/pgauto/play/SongLib",            # 读 assets/manifest.json
    "io/pgauto/play/Components",         # 三树自检
    "io/pgauto/play/PlayService",        # S1-c 驱动层（待命→零点→注入）
    "io/pgauto/play/RunLog",             # 进程内日志（界面靠它显示进度）
    "io/pgauto/play/LogScroll",          # 日志面板容器：内层滚到边界时把触摸交还页面（11-01）
    "io/pgauto/play/ZeroSource",         # 零点来源（S1-c 估计 / S1-d onset）
    "io/pgauto/play/OnsetZero",          # S1-d 零点 = onset − sil_ref − L_dev
    "io/pgauto/play/OnsetWait",          # S1-d 等待决策（纯函数；含「已锁到却读到未锁到」的复查）
    "io/pgauto/play/TapLead",            # tap 零点的 lead 解析（按曲查表，查不到就拒绝）
    "io/pgauto/play/OnsetTap",           # S1-d 端内采集（AudioPlaybackCapture）
    "io/pgauto/play/OnsetGate",          # S1-d 曲首 onset 选点（纯 Java 状态机）
    "io/pgauto/play/FrameClock",         # S1-d 帧 → 挂钟换算（纯算术）
    "io/pgauto/play/PayloadCache",       # 载荷缓存判据（内容比对；10-24 坐标事故的根因层）
    "io/pgauto/play/EnvCodec",           # 标定：包络 u8 编解码（S1，互相关的唯一失真源）
    "io/pgauto/play/EnvMatcher",         # 标定：端内互相关（S2，算每曲的 K）
    "io/pgauto/play/CalibMerge",         # 参数融合：K/lead 的首样本闸/离群/重置（纯逻辑，可自测）
    "io/pgauto/play/CalibGate",          # G4/G5：标定连续性 + 交叉校验（纯逻辑，可自测）
    "io/pgauto/play/DevFp",              # G2：设备指纹比较（纯逻辑，可自测）
    "io/pgauto/play/BuildInfo",          # G13：构建自证（versionName 带 rev，界面/trace 都读它）
    "io/pgauto/play/PrefGuard",          # G1：写盘 commit + 回读校验（Service 与 Activity 共用唯一实现）
    "io/pgauto/play/LeadAudit",          # lead 事后对账：样本坏回滚 / 均值坏重置（纯逻辑，可自测）
    "io/pgauto/play/CourseStart",        # 课题模式：第 2/3 首的曲首定位（互相关，不靠"第一个响点"）
    "io/pgauto/play/RezeroGate",         # 课题模式 rezero 的采纳闸门（±MAX_DELTA_MS 之外一律不采纳；纯逻辑）
    "io/pgauto/play/CourseBound",        # 课题模式失败路径的曲间下界判据（无零点 ⇒ 中止；纯逻辑，可自测）
    "io/pgauto/play/CourseAbort",        # 课题模式中止的 sidecar 行 + Toast 文案（纯逻辑，可自测）
    "io/pgauto/play/StartButtonBox",     # ▶ 落点是否在按钮预期框内（换机自检用，纯逻辑）
    "io/pgauto/play/CalibKeys",          # K 的 prefs key 契约：删除集必须覆盖读取集（10-30 清除 bug）
    "io/pgauto/play/AxisMap",            # 触摸 raw ↔ 屏幕坐标（用户点一次学落点，10-31）
    "io/pgauto/play/FirstDown",          # 多点击只认第一次（用户 10-31 12:12 口径）
    "io/pgauto/play/TouchDevices",       # getevent -p → 挑唯一触摸屏（不写死 eventN）
    "io/pgauto/play/GetEventParser",     # getevent 逐行 → DOWN/UP/MOVE（SYN_REPORT 聚合）
    "io/pgauto/play/TouchSniff",         # Shizuku 起 getevent 读触摸（同 shell 身份，零新权限）
    "io/pgauto/play/StartPointPrefs",    # 学到的落点：唯一读写者（learned 优先，否则出厂比例）
    "io/pgauto/play/LearnOnce",          # 学习窗口：15 s 内第一次点击 → 记录
    "io/pgauto/inject/DevRun",           # 注入引擎（S1-c）
    "io/pgauto/inject/sched/Scheduler",  # 排程/坐标变换（唯一副本）
    "io/pgauto/inject/sched/Densify",    # MOVE 加密（dp1 漏这层 ⇒ 15 Miss）
    "io/pgauto/inject/sched/InjectTrace",  # 逐帧注入追踪（多指丢帧的唯一证据来源）
    "io/pgauto/inject/sched/RecvTrace",    # 收货侧逐帧记录（对账的另一半，缺它就只有一半证据）
    "io/pgauto/play/TouchProbeActivity",   # S1-e 收货端对账靶子（重放载荷给自身）
    "io/pgauto/inject/ShizukuProbe",     # 门禁
    "io/pgauto/probe/OnsetDetector",     # 端内零点（S1-d）
    "io/pgauto/probe/RecorderService",   # 音频捕获（S1-c）
    "rikka/shizuku/Shizuku",             # 授权状态
    "rikka/shizuku/ShizukuProvider",     # binder 接收（缺 = 静默失败）
)

#: 前台服务类型位：specialUse。Android 14 上 manifest 的 type 与
#: `startForeground(..., type)` 不一致会抛 SecurityException —— 而这两种错法
#: 都不会在构建期报出来，只会表现为"点开始没反应"。
FGS_SPECIAL_USE = 0x40000000
#: 前台服务类型位：mediaProjection。Android 14 起 `MediaProjectionManager.getMediaProjection()`
#: 要求**调用之前**已有带这个类型的 FGS 在跑，否则抛 SecurityException —— 这条查漏了，
#: 症状是「点开始 → 弹了录屏授权 → 然后服务静默失败」。
FGS_MEDIA_PROJECTION = 0x00000020


def fail(msg: str) -> None:
    print(f"✗ {msg}")
    sys.exit(1)


def build_tool(name: str) -> Path:
    """定位 build-tools 里的工具；找不到就**失败**，不静默跳过检查。"""
    import os
    sdk = Path(os.environ.get("ANDROID_SDK", "D:/android-sdk"))
    suffix = ".bat" if name in ("d8", "apksigner") else ".exe"
    p = sdk / f"build-tools/34.0.0/{name}{suffix}"
    if not p.exists():
        fail(f"找不到 {p}（设 ANDROID_SDK 环境变量）")
    return p


def aapt2() -> Path:
    return build_tool("aapt2")


def xmltree(apk: Path) -> str:
    """aapt2 dump xmltree 的文本（编译后的 manifest 只有它能给出带边界的属性值）。"""
    import subprocess
    p = subprocess.run([str(aapt2()), "dump", "xmltree",
                        "--file", "AndroidManifest.xml", str(apk)],
                       capture_output=True)
    txt = p.stdout.decode("utf-8", "replace")
    if p.returncode != 0 or "E: manifest" not in txt:
        fail("aapt2 dump xmltree 失败：" + p.stderr.decode("utf-8", "replace")[:200])
    return txt


def elements(txt: str, tag: str) -> list[list[str]]:
    """按缩进切出每个 `<tag>` 元素的行块（含其属性与子元素）。

    aapt2 的 dump 是缩进树：`E: xxx` 的行首空格数就是深度，属性行 `A: ...` 比所属
    元素深 2。所以「同级下一个 E:」即为块结束 —— 不依赖具体标签内容。
    """
    import re
    lines = txt.splitlines()
    out, i = [], 0
    while i < len(lines):
        m = re.match(r"^(\s*)E: " + re.escape(tag) + r"( |$)", lines[i])
        if not m:
            i += 1
            continue
        d = len(m.group(1))
        block = [lines[i]]
        j = i + 1
        while j < len(lines):
            m2 = re.match(r"^(\s*)E: ", lines[j])
            if m2 and len(m2.group(1)) <= d:
                break
            if lines[j].strip():
                block.append(lines[j])
            j += 1
        out.append(block)
        i = j
    return out


def attr(block: list[str], name: str):
    """取块内某属性的值（字符串带引号，数值/布尔是裸的）。没有返回 None。"""
    import re
    pat = re.compile(r":" + re.escape(name) + r"\(0x[0-9a-f]+\)=(?:\"([^\"]*)\"|(0x[0-9a-fA-F]+)|(\S+))")
    for line in block:
        m = pat.search(line)
        if m:
            if m.group(1) is not None:
                return m.group(1)
            return m.group(2) if m.group(2) is not None else m.group(3)
    return None


def manifest_provider(apk: Path) -> tuple[str, str]:
    """用 aapt2 解析编译后的 AndroidManifest，取出 Shizuku provider 的 (authority, class)。

    为什么非要用 aapt2 而不是在 AXML 字节里搜索：属性值在字符串池里**没有边界信息**，
    子串搜索无法区分 `...shizuku` 与 `...shizuku_`，会把真实错误判成通过。
    """
    import re
    txt = xmltree(apk)
    # 取形如 `A: http://.../android:authorities(0x01010018)="值" (Raw: "值")`
    m_auth = re.search(r':authorities\(0x[0-9a-f]+\)="([^"]*)"', txt)
    # provider 的 class 只在 provider 那个元素里出现，取它后面最近的 android:name
    m_prov = re.search(r"E: provider.*?\n\s*A: http://[^:]+/android:name\(0x[0-9a-f]+\)=\"([^\"]*)\"",
                       txt, re.S)
    return (m_auth.group(1) if m_auth else "<缺 authorities>",
            m_prov.group(1) if m_prov else "<缺 provider name>")


def main() -> int:
    apk = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_APK
    if not apk.exists():
        fail(f"APK 不存在：{apk}（先跑 bash android/play/build.sh）")
    z = zipfile.ZipFile(apk)
    names = set(z.namelist())
    ok = 0

    # ── ① dex 与 manifest ────────────────────────────────────────────
    for need in ("classes.dex", "AndroidManifest.xml"):
        if need not in names:
            fail(f"APK 缺 {need}")
        ok += 1
    print(f"  ✓ classes.dex + AndroidManifest.xml 存在")

    # ── ② assets 与磁盘逐字节一致（不是"数量一样"）───────────────────
    disk = sorted(p for p in ASSETS_DIR.rglob("*") if p.is_file())
    if not disk:
        fail(f"磁盘上 assets 是空的：{ASSETS_DIR}")
    got = sorted(n for n in names if n.startswith("assets/"))
    want = sorted(f"assets/{p.relative_to(ASSETS_DIR).as_posix()}" for p in disk)
    if got != want:
        miss = [w for w in want if w not in got]
        extra = [g for g in got if g not in want]
        fail(f"assets 清单不一致：缺 {miss} 多 {extra}")
    total = 0
    for w, p in zip(want, disk):
        a = z.read(w)
        b = p.read_bytes()
        if a != b:
            fail(f"{w} 与磁盘不一致（APK {len(a)} B / 磁盘 {len(b)} B）")
        total += len(b)
    print(f"  ✓ assets {len(want)} 个 / {total} B 与磁盘逐字节一致")
    ok += 1

    # manifest.json 必须真的可解析，且曲目数与 payload 文件数对得上
    import json
    man = json.loads(z.read("assets/manifest.json").decode("utf-8"))
    songs = man["songs"]
    for s in songs:
        # manifest 里的 payload 是**相对 assets 根**的路径（`payload/x.json`），
        # 而 zip 内是 `assets/payload/x.json` —— 少这个前缀就会在自测里假报失败。
        if f"assets/{s['payload']}" not in got:
            fail(f"manifest 指向的载荷 {s['payload']} 不在 APK 里")

    # ★★ 2026-10-28 补：**反向**检查 —— APK 里的每个 payload 都必须被 manifest 提到。
    #   修前只查了一个方向（manifest → APK），于是「`pack_assets --keys <一首>` 把 manifest
    #   覆盖成只剩那一首」这种事故**照样能过这道闸**：manifest 里那 1 条指向的载荷确实在包里，
    #   另外 1055 个载荷则成了没人引用的孤儿。实测就是这样漏过去的（FACTS §189.15）。
    claimed = {f"assets/{s['payload']}" for s in songs}
    in_apk = {n for n in got if n.startswith("assets/payload/") and n.endswith(".json")}
    orphan = sorted(in_apk - claimed)
    if orphan:
        fail(f"发现 {len(orphan)} 个 payload 不在 manifest 里（manifest 被截断过？）"
             f" 例：{', '.join(x.split('/')[-1] for x in orphan[:3])}"
             f" ⇒ manifest {len(songs)} 首 / APK 内 {len(in_apk)} 个载荷")
    print(f"  ✓ manifest.json 可解析：{len(songs)} 首（APK 内 {len(in_apk)} 个载荷，无孤儿）"
          f"  例：" + ", ".join(f"{s['key']}({s['note_count']})" for s in songs[:3]) + " …")
    ok += 1

    # ── ③ dex 里的类名（字符串表是明文，漏一个立刻抓到）────────────────
    dex = z.read("classes.dex")
    missing = [c for c in REQUIRED_CLASSES if c.encode() not in dex]
    if missing:
        fail("dex 里缺这些类（三树同编漏了源文件？）：" + ", ".join(missing))
    print(f"  ✓ dex {len(dex)} B，{len(REQUIRED_CLASSES)} 个关键类全部在册")
    ok += 1

    # ── ④ manifest 里的 provider（authority 写错 = 静默失败）─────────────
    # ★ 必须用 aapt2 **解析**出属性值再精确比较，不能用 `b"io.pgauto.play.shizuku" in axml`
    #   这种子串搜索 —— 把 authority 改成 `...shizuku_` 时原串仍是前缀，搜索恒真，
    #   变异测试会 GREEN（本脚本第一版就是这么错的，M4 抓到）。
    auth, pname = manifest_provider(apk)
    want_auth = "io.pgauto.play.shizuku"
    if pname != "rikka.shizuku.ShizukuProvider":
        fail(f"provider class 是 `{pname}`，应为 `rikka.shizuku.ShizukuProvider`")
    if auth != want_auth:
        fail(f"provider authority 是 `{auth}`，应为 `{want_auth}`"
             "（写错 = binder 永远不送达，且没有任何报错）")
    print(f"  ✓ Shizuku provider = {pname} / authority {auth}")
    ok += 1

    # ── ⑤ 前台服务声明（S1-c）──────────────────────────────────────────
    # 三种「点开始没反应」的错法，构建期全都不报：
    #   ① 没声明 <service> ⇒ ActivityNotFound 类异常照样是运行期的；
    #   ② foregroundServiceType 与 startForeground(...,type) 不符 ⇒ Android 14 抛 SecurityException；
    #   ③ specialUse 少了 <property> 说明 ⇒ 上架被拒（这里先按协议要求锁住）。
    import re
    txt = xmltree(apk)
    m = re.search(r'^\s*A: package="([^"]*)"', txt, re.M)
    pkg = m.group(1) if m else "<缺 package>"
    want_svc = f"{pkg}.PlayService"

    def _resolve(n: str) -> str:
        if n.startswith("."):
            return pkg + n
        if "." not in n:
            return pkg + "." + n
        return n

    svc = None
    for b in elements(txt, "service"):
        if _resolve(attr(b, "name") or "") == want_svc:
            svc = b
            break
    if svc is None:
        fail(f"manifest 里没有 <service android:name=\"{want_svc}\">"
             "（PlayService 起不来，点「开始」等于什么也没做）")
    if _resolve(attr(svc, "name") or "") != want_svc:
        fail(f"service 名解析成 `{_resolve(attr(svc, 'name') or '')}`，应为 `{want_svc}`")
    fgs_raw = attr(svc, "foregroundServiceType")
    if fgs_raw is None:
        fail("service 缺 android:foregroundServiceType"
             "（Android 14 上 startForeground 会抛 SecurityException）")
    if not (int(fgs_raw, 0) & FGS_SPECIAL_USE):
        fail(f"foregroundServiceType={fgs_raw} 不含 specialUse(0x{FGS_SPECIAL_USE:x})"
             "（与本工程 startForeground 传的 type 不符）")
    if "android.app.PROPERTY_SPECIAL_USE_FGS_SUBTYPE" not in "\n".join(svc):
        fail("specialUse 服务缺 <property name=\"android.app.PROPERTY_SPECIAL_USE_FGS_SUBTYPE\">")
    perms = set()
    for b in elements(txt, "uses-permission"):
        n = attr(b, "name")
        if n:
            perms.add(n)
    need_perm = "android.permission.FOREGROUND_SERVICE_SPECIAL_USE"
    if need_perm not in perms:
        fail(f"缺权限 {need_perm}（Android 14 起 specialUse 的 FGS 必须声明它）")
    if "android.permission.WAKE_LOCK" not in perms:
        fail("缺权限 android.permission.WAKE_LOCK（160 s 的注入期间 CPU 可能被降频）")
    print(f"  ✓ 前台服务 {want_svc} · type=specialUse · property+权限齐")
    ok += 1

    # ── ⑥ 端内 onset 零点的授权（S1-d）───────────────────────────────
    # 三条都是「构建期不报、运行期才炸」的：
    #   ① 缺 mediaProjection 类型位 ⇒ getMediaProjection 抛 SecurityException；
    #   ② 缺 FOREGROUND_SERVICE_MEDIA_PROJECTION ⇒ A14 上 startForeground 直接拒；
    #   ③ 缺 RECORD_AUDIO ⇒ AudioRecord 建不起来（且 getState 才是判据，构建期无声）。
    # ★ 判据用**位与**，不是字符串包含：`specialUse|mediaProjection` 编译后是
    #   数值 `0x40000020`，用文本搜 "mediaProjection" 恒假（会误报）。
    if not (int(fgs_raw, 0) & FGS_MEDIA_PROJECTION):
        fail(f"foregroundServiceType={fgs_raw} 不含 mediaProjection"
             f"(0x{FGS_MEDIA_PROJECTION:x})（A14 上 getMediaProjection 会抛 SecurityException）")
    for need_perm in ("android.permission.FOREGROUND_SERVICE_MEDIA_PROJECTION",
                      "android.permission.RECORD_AUDIO"):
        if need_perm not in perms:
            fail(f"缺权限 {need_perm}（onset 零点链路建不起来）")
    print(f"  ✓ 端内零点：type 含 mediaProjection + 权限齐（RECORD_AUDIO / FGS_MP）")
    ok += 1

    # ── ⑦ 4 字节对齐（Android 11+ 必须；不齐的 APK 装得上但读 assets 会崩）──
    # ★ 由 zipalign 自己判，而不是看它构建时的输出：构建日志里那 4 行
    #   `WARNING: header mismatch` 是「输入不齐、我在修」，看了会误以为失败。
    import subprocess
    p = subprocess.run([str(build_tool("zipalign")), "-c", "-v", "4", str(apk)],
                       capture_output=True)
    out = (p.stdout + p.stderr).decode("utf-8", "replace")
    if p.returncode != 0 or "Verification succesful" not in out:
        tail = [l for l in out.splitlines() if l.strip()][-3:]
        fail("4 字节对齐未通过（zipalign -c）：" + " | ".join(tail))
    print(f"  ✓ 4 字节对齐（zipalign -c -v 4）")
    ok += 1

    # ── ⑧ 签名（未签名的 APK adb install 会直接拒）──────────────────────
    p = subprocess.run([str(build_tool("apksigner")), "verify", "--print-certs", str(apk)],
                       capture_output=True)
    out = (p.stdout + p.stderr).decode("utf-8", "replace")
    if p.returncode != 0:
        fail("apksigner verify 失败：" + " | ".join(
            [l for l in out.splitlines() if l.strip()][-2:]))
    dn = next((l.split(":", 1)[1].strip() for l in out.splitlines()
               if "certificate DN" in l), "?")
    print(f"  ✓ 签名有效（{dn}）")
    ok += 1

    # ── ⑨ 应用图标（2026-10-26 接入）───────────────────────────────────
    # 三种「装上去还是绿机器人」的错法，①~⑧ 全抓不到：
    #   ① manifest 缺 android:icon ⇒ 系统渲染默认图标，构建/安装/启动一切正常；
    #   ② adaptive-icon 的 background 与 foreground 指向同一个资源 ⇒ 图形被叠一层、
    #      启动器做视差时两层一起动 ⇒ 抖动，而且背景被裁成图形形状；
    #   ③ aapt2 的 PNG crunch 把图**有损量化**：2026-10-26 实测 xxxhdpi 前景层
    #      最大通道差 112、15.5 万像素被改（其余 14 张为 0）⇒ φ 的模糊光晕被啃。
    #      所以判据必须是**逐字节**，只看"res 里有没有 png"抓不到。
    # ⛔ 别改成「res/ 里 png 数量 == 15」这种计数断言：图被换掉、被 crunch、
    #    张冠李戴（foreground 塞了 background）都能过。
    RES_DIR = ROOT / "android/play/app/src/main/res"

    def png_size(data: bytes) -> tuple[int, int]:
        """从 PNG 的 IHDR 读 (宽, 高)。不依赖 Pillow —— 校验脚本不能因为
        换了个没装 Pillow 的 python 就整条构建崩掉。"""
        import struct
        if not data.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("不是 PNG（缺 magic）")
        return struct.unpack(">II", data[16:24])

    # 期望的 15 张：5 档传统图标 + 5 档 × 2 层 adaptive 图层。
    # ⚠ 图层尺寸是 108dp 而非图标尺寸 ⇒ 换算系数 108/48（与 build.sh / 导出脚本一致）。
    #   写死成"和 ic_launcher 一样大"会漏掉「图层没按 108dp 导」这个真错。
    expect_png: dict[str, int] = {}
    for _dpi, _px in (("mdpi", 48), ("hdpi", 72), ("xhdpi", 96),
                      ("xxhdpi", 144), ("xxxhdpi", 192)):
        expect_png[f"mipmap-{_dpi}/ic_launcher.png"] = _px
        for _layer in ("ic_launcher_foreground.png", "ic_launcher_background.png"):
            expect_png[f"mipmap-{_dpi}/{_layer}"] = round(_px * 108 / 48)

    # ① manifest 声明了图标，且 aapt2 能把它解析到 APK 里真实存在的一条
    #   ★ 只能用 badging：编译后的 manifest 里 icon 是 `@0x7f010000` 这种资源 ID，
    #     在 xmltree 里看不到"它到底是哪个文件"，也就验不出指向对不对。
    p = subprocess.run([str(aapt2()), "dump", "badging", str(apk)],
                       capture_output=True)
    badging = p.stdout.decode("utf-8", "replace")
    m = re.search(r"^application:.*?\bicon='([^']+)'", badging, re.M)
    if not m:
        fail("manifest 没有 android:icon（桌面会显示系统默认图标；"
             "构建、安装、启动全都不报错，所以只能在这里拦）")
    icon_entry = m.group(1)
    if icon_entry not in names:
        fail(f"badging 报的 icon `{icon_entry}` 不在 APK 里（引用了没编进去的资源）")

    # ② adaptive icon：两层必须都存在且**不是同一个资源**
    adap = [n for n in names if n.startswith("res/mipmap-anydpi") and n.endswith("/ic_launcher.xml")]
    if not adap:
        fail("APK 里没有 mipmap-anydpi-*/ic_launcher.xml —— API 26+ 会退回位图并"
             "套方形遮罩（圆角桌面会出现一块方底）")
    ax = subprocess.run([str(aapt2()), "dump", "xmltree", "--file", adap[0], str(apk)],
                        capture_output=True).stdout.decode("utf-8", "replace")

    def _layer(tag: str) -> str | None:
        for blk in elements(ax, tag):
            v = attr(blk, "drawable")
            if v:
                return v
        return None

    bg_id, fg_id = _layer("background"), _layer("foreground")
    if not bg_id or not fg_id:
        fail(f"adaptive-icon 缺背景层或前景层（background={bg_id} foreground={fg_id}）"
             "：缺前景 = 只剩一块纯色，缺背景 = 启动器可能画成黑块")
    if bg_id == fg_id:
        fail(f"adaptive-icon 的 background 与 foreground 都指向 {bg_id}"
             "（图形被重复叠一层，且启动器视差会让两层一起抖）")

    # ③ 15 张位图逐字节与磁盘一致 + 尺寸符合 108dp 换算
    #   ★ 目录名不要写死 `-v4`：那是 aapt2 按 minSdk 自动加的限定符，换 build-tools
    #     版本可能变。按 `res/mipmap-<dpi>` 前缀 + 文件名精确匹配。
    checked = 0
    for rel, want_px in sorted(expect_png.items()):
        dpi, base = rel.split("/")
        cands = [n for n in names
                 if n.startswith(f"res/{dpi}") and n.endswith("/" + base)]
        if len(cands) != 1:
            fail(f"{rel} 在 APK 里命中 {len(cands)} 条（应为 1）")
        disk = RES_DIR / rel
        if not disk.exists():
            fail(f"磁盘缺 {rel}（源稿在 android/play/icon/，拷回 res/ 即可）")
        a, b = z.read(cands[0]), disk.read_bytes()
        if a != b:
            fail(f"{cands[0]} 与磁盘不一致（APK {len(a)} B / 磁盘 {len(b)} B）"
                 "—— 高度怀疑 aapt2 的 PNG crunch 改写了图：给 aapt2 compile 加 --no-crunch")
        w, h = png_size(b)
        if (w, h) != (want_px, want_px):
            fail(f"{rel} 是 {w}x{h}，应为 {want_px}x{want_px}"
                 "（adaptive 图层必须按 108dp 导出，否则会被拉伸出边）")
        checked += 1
    print(f"  ✓ 图标：icon={icon_entry} · adaptive 两层有别 · "
          f"{checked} 张位图逐字节一致且尺寸合规")
    ok += 1

    print(f"\n⇒ APK 校验通过（{ok} 组）：{apk.name} {apk.stat().st_size} B")
    return 0


if __name__ == "__main__":
    sys.exit(main())
