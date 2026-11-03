#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""S1 参考实现：touches_<key>.json → 端内注入序列（规范化 TSV）。

用途
  S2 的 Java 实现（纯逻辑，端内跑）必须与本文件**逐行一致**。
  这就是项目规矩「同一个量两处实现 ⇒ 交叉断言」的落地：
  两份独立实现产出同一个黄金序列，任一方算错都会被 diff 抓到。

为什么坐标变换复用 tools/inject.ScreenMap 而不另写一份
  换机时这里最容易出错（设备绝对像素是换机头号杀手）。
  复用生产函数 = 只有一处真值；另写一份 = 引入第二真值，迟早不一致。

载荷契约（现查 out/touches_demiurge.json）
  events[] 的 x/y 是 **1080x2340 竖屏交付坐标**（不是画布、不是屏幕）。
  canvas = 1920x1080（谱面画布），screen = 1080x2340，dst = 真机横屏 1610x720。
  变换：交付坐标 --to_canvas--> 画布 --from_canvas(fit=height)--> 真机像素。

用法
  .venv/Scripts/python.exe tools/inject_sched/payload_ref.py out/touches_demiurge.json
  .venv/Scripts/python.exe tools/inject_sched/payload_ref.py --all      # 三首基准各跑一遍
  .venv/Scripts/python.exe tools/inject_sched/payload_ref.py --check    # 自测断言
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

import inject  # noqa: E402  生产 ScreenMap

# 关键事件：决定「手指何时落到判定区」⇒ 直接影响 Tap/Flick 判定时刻
KEY_ACTIONS = ("DOWN", "POINTER_DOWN", "POINTER_UP", "UP")

# 端内真机横屏尺寸（HONOR ALT-AN00：wm size 720x1610 ⇒ 横屏 1610x720）
DST_DEFAULT = (1610, 720)
FIT_DEFAULT = "height"


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def build(path: Path, dst=DST_DEFAULT, fit=FIT_DEFAULT, densify: bool = True,
          stats: dict | None = None) -> list[dict]:
    """产出一条注入指令序列（已排序 → 可选 MOVE 加密 → 变换坐标 → 标关键性）。

    ★ 顺序不能换：densify.py 的 Drag 判定带（150 canvas px）是拿 **screen 坐标**
      乘 1920/1080 判的。先变换到设备像素再加密，就得多一次反推 + 两次取整，
      与端内 Java 实现分叉。
    ★ 加密后**不重排**：densify 就地追加插值点，重排会改同一时刻多指针的相对顺序。
    """
    d = load(path)
    canvas = (float(d["canvas"]["width"]), float(d["canvas"]["height"]))
    src = (float(d["screen"]["width"]), float(d["screen"]["height"]))
    sm = inject.ScreenMap(canvas, src, (float(dst[0]), float(dst[1])), fit=fit)

    raw = d["events"]
    # ★ 稳定排序：只按 t_ms，同毫秒保持原文件顺序。
    #   Java 侧必须用同样的「稳定 + 只比 t_ms」才可能逐行一致（TimSort / sorted 都稳定）。
    order = sorted(range(len(raw)), key=lambda i: raw[i]["t_ms"])
    seq_events = [raw[i] for i in order]

    if densify:
        sys.path.insert(0, str(ROOT / "tmp"))
        import densify as _dens       # 生产实现，别再抄一份
        ident = {id(e): si for e, si in zip(seq_events, order)}
        dense, st = _dens.densify(seq_events)
        if stats is not None:
            stats.update(st)
        # 插值点没有源下标 ⇒ -1（Java 侧 Densify.Raw 用同一个约定）
        pairs = [(ident.get(id(e), -1), e) for e in dense]
    else:
        pairs = list(zip(order, seq_events))

    out = []
    clamp_n = 0
    clamp_max = 0
    bottom_n = 0
    h_px = int(dst[1])
    for seq, (src_idx, e) in enumerate(pairs):
        t = e["t_ms"]
        act = e["action"]
        # ★ 2026-10-22 21:2x：`point_ex()` 的夹取口径 = x 夹进 [0,w-1]、顶部夹到 0、
        #   **底部屏外（y > h-1）收到 `inject.SAFE_BOTTOM_Y = 639`**，`640~719`
        #   原样保留（那一段是 mandatory 手势区，但本来就能正常工作，动了只吃容差；
        #   收进手势区会触发 GestureNavBottom 抢流，见 FACTS §126）。
        #   端内 Java 侧（Scheduler/Injector）必须做**同一件事**，否则 PC 参考实现与
        #   端内会对不上 —— 这是「同一个量两处实现」的既有隐患，用 clamp_n / bottom_n 显式暴露。
        dx, dy, nx, ny = sm.point_ex(float(e["x"]), float(e["y"]))
        if nx or ny:
            clamp_n += 1
            clamp_max = max(clamp_max, abs(nx), abs(ny))
        # ★ 判 `ny > 0` 而**不是** `dy > h-1`：底部夹取后 `dy` 恒 ≤ 639，用夹后坐标判
        #   会让这个计数**静默变 0**。`ny = iy - py`，底部屏外为正、顶部屏外为负。
        if ny > 0:
            bottom_n += 1
        out.append({
            "seq": seq,
            "src_idx": src_idx,
            "t_ms": t,
            "action": act,
            "pid": int(e["pointer_id"]),
            "dev_x": dx,
            "dev_y": dy,
            "is_key": 1 if act in KEY_ACTIONS else 0,
        })
    if stats is not None:
        stats["clamp_n"] = clamp_n
        stats["clamp_max_px"] = clamp_max
        stats["bottom_n"] = bottom_n
        stats["dst"] = [int(dst[0]), int(dst[1])]
    return out


def emit_tsv(rows: list[dict], fp=sys.stdout) -> None:
    # ★ t_ms 用固定 6 位小数：JSON 里可能是 25946 或 25946.0，
    #   Python 会分别解析成 int / float ⇒ 直接 str() 两边对不上。
    #   Java 侧必须用同样的 "%.6f" + Locale.ROOT（否则某些 locale 用逗号做小数点）。
    fp.write("seq\tsrc_idx\tt_ms\taction\tpid\tdev_x\tdev_y\tis_key\n")
    for r in rows:
        fp.write(f"{r['seq']}\t{r['src_idx']}\t{r['t_ms']:.6f}\t{r['action']}\t"
                 f"{r['pid']}\t{r['dev_x']}\t{r['dev_y']}\t{r['is_key']}\n")


# ─────────────────────────── 自测 ───────────────────────────

def _check() -> int:
    fails = []

    def chk(cond, msg):
        print(("  OK   " if cond else "  FAIL ") + msg)
        if not cond:
            fails.append(msg)

    print("=== 1. 坐标变换与 plan 对照（demiurge 首条事件）===")
    d = load(ROOT / "out" / "touches_demiurge.json")
    rows = build(ROOT / "out" / "touches_demiurge.json")
    # plan 里首条 = [1, 0, 25946.0, 0, 1500.0, 864.006]（画布坐标）
    plan0 = d["plan"][0]
    canvas_x, canvas_y = plan0[4], plan0[5]
    sm = inject.ScreenMap((1920.0, 1080.0), (1080.0, 2340.0), (1610.0, 720.0), fit="height")
    exp_x, exp_y = sm.from_canvas(canvas_x, canvas_y)
    chk(abs(rows[0]["dev_x"] - round(exp_x)) <= 1,
        f"dev_x {rows[0]['dev_x']} ≈ from_canvas(1500) = {exp_x:.3f}")
    chk(abs(rows[0]["dev_y"] - round(exp_y)) <= 1,
        f"dev_y {rows[0]['dev_y']} ≈ from_canvas(864.006) = {exp_y:.3f}")
    # 黑边 165：画布 x=0 应落到真机 x=165
    edge_x, _ = sm.from_canvas(0.0, 0.0)
    chk(abs(edge_x - 165.0) < 1e-6, f"左黑边 = {edge_x}（实测常量应为 165）")

    print("\n=== 2. 坐标方向性检查（demiurge；合法域 x∈[0,1609]、y∈[0,639] + 手势区原样点）===")
    # ⚠ 2026-10-22 一天内三次修正，前两次都是「断言把 bug 锁住」的同族错误：
    #   ① 原断言写 `dev_y <= 720`，自称「落在屏幕内」却把 y=720 这个**不存在的像素**
    #      （屏高 720，合法行 0..719，Rect 半开）当合法 —— 真机实测 y=720 的
    #      ACTION_DOWN 找不到触摸窗口。
    #   ② 改成「四边全夹、越界必须 0」后，实测夹到 719 会落进**系统底部手势区**
    #      （`mandatorySystemGestures [640,720)`），Honor GestureNavBottom 抢流把游戏
    #      踢到桌面（FACTS §126）⇒ 当时改成「底部不夹」，于是断言 `dev_y > 719` 合法。
    #      **那一版把「已知缺陷」写成了契约**，是错的。
    #   ③ 现行：屏外（>719）收到 `SAFE_BOTTOM_Y=639`，`640~719` 原样。所以合法域
    #      收窄到 **y ∈ [0,639]**（639 之外只允许「原样保留的手势区点」，见 2b 的分项）。
    #      注意这里断言的是**上界 639**，不是 719 —— 719 落在手势区里。
    oob_x = [r for r in rows if not (0 <= r["dev_x"] <= 1609)]
    oob_top = [r for r in rows if r["dev_y"] < 0]
    # ⚠ 这里**只**断言「不超出屏内最后一行 719」：`640~719` 是「手势区内但原样保留」
    #   的合法点（全库 29,406 条，本来就能正常工作），不能在这里判成越界。
    #   精确口径（屏外必须收到 639、手势区内必须原样）留给 2b 分项验。
    oob_bottom = [r for r in rows if r["dev_y"] > 719]
    chk(not oob_x, f"x 域越界 {len(oob_x)} 条（应为 0，合法 x∈[0,1609]）")
    chk(not oob_top, f"顶部越界 {len(oob_top)} 条（应为 0：顶部夹到 0，避免屏外 POINTER_UP 丢失）")
    chk(not oob_bottom, f"超出屏内最后一行 {len(oob_bottom)} 条（应为 0，合法 y≤719）")
    # ⚠ 口径：`n_safe` 是**结果侧**的条数（落点恰为 639），含本来就落在 639 的点，
    #   **不等于夹取量**。夹取量看 2b 的 `st["bottom_n"]`（那里用裸映射独立复算）。
    #   上一版把 `n_safe` 的括号写成「（屏外）」⇒ 读者会以为它就是屏外条数，
    #   而 2b 同屏打出 73 ⇒ 看起来像自检自相矛盾（实际是两首不同的曲子）。
    n_safe = sum(1 for r in rows if r["dev_y"] == inject.SAFE_BOTTOM_Y)
    n_gest = sum(1 for r in rows if inject.GEST_BOTTOM_Y <= r["dev_y"] <= 719)
    # 同曲裸映射独立复算：屏外原始条数。方向性断言 —— 屏外点**全部**被收到 639，
    # 而 639 上还可能另有本来就落在该行的点 ⇒ `n_safe >= n_raw_oob`（只可能更大）。
    raw_d = [sm.unclamped_point(float(e["x"]), float(e["y"])) for e in d["events"]]
    n_raw_oob = sum(1 for _x, uy in raw_d if uy > 719)
    chk(n_safe >= n_raw_oob,
        f"落点为 {inject.SAFE_BOTTOM_Y} 的 {n_safe} 条 >= 裸映射屏外 {n_raw_oob} 条"
        f"（屏外点必须全部被收，故只可能更大）")
    print(f"     结果侧：落点恰为 {inject.SAFE_BOTTOM_Y} 的 {n_safe} 条"
          f"（含原本就在该行的点）｜手势区内原样（640~719）：{n_gest} 条"
          f"｜裸映射屏外：{n_raw_oob} 条")
    for r in oob_x[:5]:
        print("     x-oob", r)

    print("\n=== 2b. 夹取口径（wanju_in）：顶部夹到 0、屏外收到 639、手势区原样；其余分毫不动 ===")
    pw = ROOT / "out" / "touches_wanju_in.json"
    if not pw.exists():
        chk(False, f"缺 {pw}（无法验证夹取口径）")
    else:
        st: dict = {}
        rows_w = build(pw, densify=False, stats=st)
        sm2 = inject.ScreenMap((1920.0, 1080.0), (1080.0, 2340.0), (1610.0, 720.0), fit="height")
        raw = load(pw)["events"]
        # 独立复算：拿裸映射自己数，两条独立路径必须一致（别信 build 的自报数）
        raw_pts = [sm2.unclamped_point(float(e["x"]), float(e["y"])) for e in raw]
        n_top_or_side = sum(1 for ux, uy in raw_pts
                            if not (0 <= ux <= 1609) or uy < 0)
        n_bottom = sum(1 for _ux, uy in raw_pts if uy > 719)
        # `clamp_n` 现在**包含**底部夹取（屏外点 ny>0）⇒ 与「顶部/左右 + 屏外」对账
        chk(st["clamp_n"] == n_top_or_side + n_bottom,
            f"夹取条数 {st['clamp_n']} == 顶部/左右 {n_top_or_side} + 屏外 {n_bottom}")
        chk(st["bottom_n"] == n_bottom,
            f"屏外条数 {st['bottom_n']} == 裸映射折算 {n_bottom}")
        chk(n_bottom > 0, f"样本曲确实存在屏外点（{n_bottom} 条）⇒ 这条判据不是空转")
        by_idx = {r["src_idx"]: r for r in rows_w if r["src_idx"] >= 0}

        # ① 屏外点（原始 >719）必须**全部**收到 SAFE_BOTTOM_Y
        bad_safe = [(i, raw_pts[i][1], by_idx[i]["dev_y"]) for i in by_idx
                    if raw_pts[i][1] > 719 and by_idx[i]["dev_y"] != inject.SAFE_BOTTOM_Y]
        chk(not bad_safe,
            f"屏外点未收到 {inject.SAFE_BOTTOM_Y} 的 {len(bad_safe)} 条（应为 0）")
        # ② 屏外点**一个都不许**落回手势区（640~719 会被 GestureNavBottom 抢流）
        got_gest = [(i, by_idx[i]["dev_y"]) for i in by_idx
                    if raw_pts[i][1] > 719
                    and inject.GEST_BOTTOM_Y <= by_idx[i]["dev_y"] <= 719]
        chk(not got_gest,
            f"屏外点落回手势区的 {len(got_gest)} 条（应为 0，§126 事故形态）")
        # ③ ★ 手势区内的点（640~719）必须**原样**：这是「别无差别上移」的守门断言
        moved_gest = 0
        for i in by_idx:
            ux, uy = raw_pts[i]
            if inject.GEST_BOTTOM_Y <= uy <= 719:
                if (ux, uy) != (by_idx[i]["dev_x"], by_idx[i]["dev_y"]):
                    moved_gest += 1
        chk(moved_gest == 0, f"手势区内事件被改动 {moved_gest} 条（应为 0）")
        # ④ 安全区内的点也必须分毫不动
        moved_safe = 0
        for i in by_idx:
            ux, uy = raw_pts[i]
            if 0 <= ux <= 1609 and 0 <= uy < inject.GEST_BOTTOM_Y:
                if (ux, uy) != (by_idx[i]["dev_x"], by_idx[i]["dev_y"]):
                    moved_safe += 1
        chk(moved_safe == 0, f"安全区内事件被改动 {moved_safe} 条（应为 0）")

    print("\n=== 3. 排序稳定 + 事件数守恒 ===")
    # ⚠ 2026-10-22 修正：本节原来断言 `len(rows) == len(d["events"])` 且
    #   `src_idx` 逐项等于稳定排序结果 —— 但 `build()` 默认 densify=True 会**插入
    #   加密点**（`src_idx = -1`），于是这两条一直 FAIL。真机侧没错，是自检的算式
    #   忘了加插入量 ⇒ 改成「源事件 + 插入量」守恒、并在比顺序时剔除 -1。
    st3: dict = {}
    rows = build(ROOT / "out" / "touches_demiurge.json", stats=st3)
    n_ins = st3.get("n_added", 0) + st3.get("n_tail_added", 0)
    chk(len(rows) == len(d["events"]) + n_ins,
        f"条数 {len(rows)} == 源 {len(d['events'])} + 加密插入 {n_ins}")
    src_order = sorted(range(len(d["events"])), key=lambda i: d["events"][i]["t_ms"])
    got_order = [r["src_idx"] for r in rows if r["src_idx"] >= 0]
    chk(got_order == src_order,
        f"src_idx（剔除 {len(rows) - len(got_order)} 个加密点）顺序 = 稳定排序结果")
    chk([r["seq"] for r in rows] == list(range(len(rows))), "seq 连续 0..n-1")
    chk(all(rows[i]["t_ms"] <= rows[i + 1]["t_ms"] for i in range(len(rows) - 1)),
        "t_ms 单调不减")

    print("\n=== 4. 关键事件计数与 action 分布一致 ===")
    import collections
    dist = collections.Counter(e["action"] for e in d["events"])
    nkey = sum(1 for r in rows if r["is_key"])
    exp_key = sum(v for k, v in dist.items() if k in KEY_ACTIONS)
    chk(nkey == exp_key, f"关键事件 {nkey} == 分布折算 {exp_key}")
    chk(nkey + sum(1 for r in rows if not r["is_key"]) == len(rows), "关键+非关键 = 总数")
    print(f"     action 分布: {dict(dist)}")

    print("\n=== 5. ★ 变异测试：改错一处，判据必须能发现 ===")
    # (a) 把 fit 换成 width ⇒ 黑边/坐标都应变化，用同一断言必须判失败
    rows_w = build(ROOT / "out" / "touches_demiurge.json", fit="width")
    sm_w = inject.ScreenMap((1920.0, 1080.0), (1080.0, 2340.0), (1610.0, 720.0), fit="width")
    edge_w, _ = sm_w.from_canvas(0.0, 0.0)
    chk(abs(edge_w - 165.0) > 1e-6, f"fit=width 时黑边 {edge_w} ≠ 165 ⇒ 判据能区分两种 fit")
    chk(rows_w[0]["dev_y"] != rows[0]["dev_y"],
        f"两种 fit 下首条 dev_y 不同（{rows_w[0]['dev_y']} vs {rows[0]['dev_y']}）")
    # (b) 若把 MOVE 也标成关键 ⇒ 关键数必须变
    fake = [dict(r) for r in build(ROOT / "out" / "touches_demiurge.json")]
    for r in fake:
        r["is_key"] = 1
    chk(sum(x["is_key"] for x in fake) != nkey, "把 MOVE 也算关键 ⇒ 关键数变化 ⇒ 判据有区分力")

    print("\n" + ("全部通过" if not fails else f"失败 {len(fails)} 项"))
    return 1 if fails else 0


def main() -> int:
    # ★ Windows 下 sys.stdout 默认把 "\n" 转成 "\r\n"，而 Java 的 System.out 输出裸 "\n"
    #   ⇒ 两边 diff 会「每一行都不同」。这是交叉验证实抓到的坑，不是排版问题。
    try:
        sys.stdout.reconfigure(newline="\n")
    except (AttributeError, ValueError):
        pass  # 非文本流（如已被替换）时跳过

    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    if args[0] == "--check":
        return _check()
    if args[0] == "--all":
        for k in ("demiurge", "kuangxi", "lacamp"):
            p = ROOT / "out" / f"touches_{k}.json"
            if not p.exists():
                print(f"skip {k}（缺 {p}）", file=sys.stderr)
                continue
            rows = build(p)
            nkey = sum(r["is_key"] for r in rows)
            buckets = len({r["t_ms"] for r in rows})
            print(f"# {k}: {len(rows)} 条 / 关键 {nkey} / 不同时刻 {buckets} "
                  f"⇒ 同毫秒可合并省 {len(rows) - buckets}", file=sys.stderr)
            emit_tsv(rows)
        return 0
    p = Path(args[0])
    if not p.is_absolute():
        p = ROOT / p
    emit_tsv(build(p))
    return 0


if __name__ == "__main__":
    sys.exit(main())
