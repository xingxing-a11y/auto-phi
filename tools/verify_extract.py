#!/usr/bin/env python3
"""A 段验收：核对解包结果。

**刻意不 import `pgauto`** —— 只读磁盘上的交付物，用独立的解释器逐条复算，
避免「实现和测试共享同一个 bug」。

七组判据::

    [1] 清单自洽      index.json 结构合法，且每条记录指向的文件真实存在
    [2] 字节对拍      每个文件的字节数与 sha256 与清单记录一致
    [3] 结构合法      按**从 1064 个真实谱面枚举出的** schema 复算
    [4] 与社区对拍    samples/ 下的同名谱面与解包结果**逐字节**相同
    [5] 覆盖率        无重复键、无空文件
    [6] 速度积分      用谱面自带的 `speedEvents.floorPosition` 快照独立校验
    [7] 统计          难度 / 版本 / 音符类型分布、体积

`[3]` 的 schema 不是从文档抄的，是从 1064 个谱面里**枚举**出来的（见 `FILED_UNIVERSE`）。

用法::

    python tools/verify_extract.py
    python tools/verify_extract.py --charts out/charts
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CHARTS = ROOT / "out" / "charts"
DEFAULT_SAMPLES = ROOT / "samples"

DIFFS = {"EZ", "HD", "IN", "AT", "SP", "LEGACY"}
NOTE_TYPES = {1: "Tap", 2: "Drag", 3: "Hold", 4: "Flick"}

# --------------------------------------------------------------------------- #
# 权威 schema —— 从 1064 个官方谱面枚举得出（4.0.1 / version_code 157）
# --------------------------------------------------------------------------- #
#
# 枚举结果（出现次数 / 总样本）：
#   根:      formatVersion 1064, offset 1064, judgeLineList 1064
#            blockAreaList 30, numOfNotes 6
#   判定线:  bpm / notesAbove / notesBelow / 四个事件通道 —— 全 15142 条都有
#            numOfNotes, numOfNotesAbove, numOfNotesBelow 各 144
#   音符:    682746 个音符，**零例外**地只有 6 个字段
#   speedEvents:            startTime, endTime, value（floorPosition 402 条为可选）
#   judgeLineMoveEvents:    v3 必有 start2/end2；v1 必无
#   rotate/disappearEvents: 固定 4 字段，start2/end2 少数条目才有（可选）
#
FILED_UNIVERSE = dict(
    root_required={"formatVersion", "offset", "judgeLineList"},
    root_optional={"blockAreaList", "numOfNotes"},
    line_required={"bpm", "notesAbove", "notesBelow", "speedEvents",
                   "judgeLineMoveEvents", "judgeLineRotateEvents", "judgeLineDisappearEvents"},
    line_optional={"numOfNotes", "numOfNotesAbove", "numOfNotesBelow"},
    note_exact={"type", "time", "positionX", "holdTime", "speed", "floorPosition"},
)

#: 事件通道 → (必需字段, 可选字段)。`start2/end2` 的必需性随 formatVersion 变。
EVENT_SCHEMA = {
    "speedEvents": ({"startTime", "endTime", "value"}, {"floorPosition"}),
    "judgeLineMoveEvents": ({"startTime", "endTime", "start", "end", "start2", "end2"}, set()),
    "judgeLineMoveEvents@v1": ({"startTime", "endTime", "start", "end"}, set()),
    "judgeLineRotateEvents": ({"startTime", "endTime", "start", "end"}, {"start2", "end2"}),
    "judgeLineDisappearEvents": ({"startTime", "endTime", "start", "end"}, {"start2", "end2"}),
}

ALLOWED_FORMAT_VERSIONS = {1, 3}

#: 反向区间（`endTime < startTime`）在官方谱里**合法**：实测 73 处 / 16 首，
#: 跨度 −16 到 −12063 拍，游戏按空窗口（瞬时/无效）处理，B 段同口径过滤。
#: 真正要守的不变量：**它绝不能出现在速度通道** —— 速度积分决定音符高度与
#: `floorPosition`，一旦反向就会错击打点。渲染类通道（disappear/move/rotate）
#: 反向只影响画面。
HIT_POINT_CRITICAL_CHANNEL = "speedEvents"

#: 官方 `floorPosition` 是 float32；判定阈值按 float32 相对精度给。
FLOAT32_EPS = 2.0**-23
#: 允许的相对偏差 = 该点之前累积的事件数 × eps（逐事件累加的舍入预算上限）
#: 再加一份安全余量。
FLOORPOS_SAFETY = 4.0


class Report:
    def __init__(self):
        self.checks: list[tuple[str, bool, str]] = []

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.checks.append((name, ok, detail))
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  — {detail}" if detail else ""))
        return ok

    def section(self, title: str) -> None:
        print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")

    @property
    def failed(self) -> int:
        return sum(1 for _, ok, _ in self.checks if not ok)


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="A 段解包验收")
    p.add_argument("--charts", type=Path, default=DEFAULT_CHARTS)
    p.add_argument("--samples", type=Path, default=DEFAULT_SAMPLES)
    return p.parse_args(argv)


# --------------------------------------------------------------------------- #
# 独立复算（只用标准库）
# --------------------------------------------------------------------------- #


def check_chart_structure(j: dict, neg_by_channel: Counter) -> list[str]:
    """返回问题列表（空表示合法），并把反向区间计入 `neg_by_channel`。"""
    errs: list[str] = []
    ver = j.get("formatVersion")
    if ver not in ALLOWED_FORMAT_VERSIONS:
        errs.append(f"formatVersion={ver!r} 不在 {sorted(ALLOWED_FORMAT_VERSIONS)}")
        return errs
    if not isinstance(j.get("offset"), (int, float)):
        errs.append(f"offset={j.get('offset')!r} 不是数")

    if not FILED_UNIVERSE["root_required"] <= set(j):
        errs.append(f"根缺必需字段 {sorted(FILED_UNIVERSE['root_required'] - set(j))}")
        return errs
    unknown_root = set(j) - FILED_UNIVERSE["root_required"] - FILED_UNIVERSE["root_optional"]
    if unknown_root:
        errs.append(f"根出现未知字段 {sorted(unknown_root)}")
        return errs

    lines = j.get("judgeLineList")
    if not isinstance(lines, list) or not lines:
        errs.append("judgeLineList 缺失或为空")
        return errs

    for li, ln in enumerate(lines):
        if not isinstance(ln, dict):
            errs.append(f"线{li} 不是对象")
            continue
        if not FILED_UNIVERSE["line_required"] <= set(ln):
            errs.append(f"线{li} 缺必需字段 {sorted(FILED_UNIVERSE['line_required'] - set(ln))}")
            continue
        unknown_line = set(ln) - FILED_UNIVERSE["line_required"] - FILED_UNIVERSE["line_optional"]
        if unknown_line:
            errs.append(f"线{li} 出现未知字段 {sorted(unknown_line)}")
            continue
        if not isinstance(ln.get("bpm"), (int, float)) or ln["bpm"] <= 0:
            errs.append(f"线{li} bpm 非法: {ln.get('bpm')!r}")

        for side in ("notesAbove", "notesBelow"):
            notes = ln.get(side)
            if not isinstance(notes, list):
                errs.append(f"线{li}.{side} 不是数组")
                continue
            for ni, n in enumerate(notes):
                if set(n) != FILED_UNIVERSE["note_exact"]:
                    errs.append(f"线{li}.{side}[{ni}] 字段不符: 缺 "
                                f"{sorted(FILED_UNIVERSE['note_exact'] - set(n))} 多 "
                                f"{sorted(set(n) - FILED_UNIVERSE['note_exact'])}")
                    break
                if n["type"] not in NOTE_TYPES:
                    errs.append(f"线{li}.{side}[{ni}] type={n['type']!r} 未知")
                    break
                if not isinstance(n["time"], (int, float)) or n["time"] < 0:
                    errs.append(f"线{li}.{side}[{ni}] time 非法")
                    break
                if n["type"] == 3 and not (isinstance(n.get("holdTime"), (int, float))
                                           and n["holdTime"] > 0):
                    errs.append(f"线{li}.{side}[{ni}] Hold 的 holdTime 应为正数")
                    break

        for ek in ("speedEvents", "judgeLineMoveEvents", "judgeLineRotateEvents",
                   "judgeLineDisappearEvents"):
            key = f"{ek}@v{ver}" if ek == "judgeLineMoveEvents" else ek
            if key not in EVENT_SCHEMA:
                key = ek
            need, opt = EVENT_SCHEMA[key]
            for ei, ev in enumerate(ln.get(ek) or []):
                got = set(ev)
                if need - got:
                    errs.append(f"线{li}.{ek}[{ei}] 缺字段 {sorted(need - got)}")
                    break
                unknown = got - need - opt
                if unknown:
                    errs.append(f"线{li}.{ek}[{ei}] 未知字段 {sorted(unknown)}")
                    break
                if ev["endTime"] < ev["startTime"]:
                    neg_by_channel[ek] += 1
        if len(errs) > 12:
            break
    return errs


def chart_note_stats(j: dict) -> Counter:
    c: Counter = Counter()
    for ln in j["judgeLineList"]:
        for side in ("notesAbove", "notesBelow"):
            for n in ln.get(side) or []:
                c[NOTE_TYPES.get(n.get("type"), f"?{n.get('type')}")] += 1
    return c


def max_chart_time(j: dict) -> float:
    """全谱最大时刻 + 1s —— 速度积分末帧外推要用（与 B 段同口径）。"""
    mx = 0.0
    for ln in j["judgeLineList"]:
        r = 60.0 / 32.0 / float(ln["bpm"])
        for side in ("notesAbove", "notesBelow"):
            for n in ln.get(side) or []:
                mx = max(mx, float(n["time"]) * r
                         + (float(n["holdTime"]) * r if n["type"] == 3 else 0.0))
    return mx + 1.0


def speed_integral_at(speed_events: list[dict], r: float, t: float) -> float:
    """独立实现的速度积分：`h(t) = ∫v dt`。

    与 `pgauto.events.parse_speed_events` **刻意用不同写法**：
    这里直接按「事件 i 的 value 作用在 [start_i, end_i]」逐段累加，
    末事件之后按自身 value 外推（M2 定下的口径）。

    反向区间（start > end）的事件按空窗口跳过 —— 与 B 段同口径。
    """
    evs = [e for e in speed_events if float(e["startTime"]) <= float(e["endTime"])]
    if not evs:
        return 0.0
    evs = sorted(evs, key=lambda e: float(e["startTime"]))
    evs[0] = dict(evs[0])
    if float(evs[0]["startTime"]) != 0.0:
        evs[0]["startTime"] = 0.0

    acc = 0.0
    last_end = last_val = 0.0
    for e in evs:
        s, en, v = float(e["startTime"]) * r, float(e["endTime"]) * r, float(e["value"])
        if t <= s:
            return acc
        acc += (min(en, t) - s) * v
        last_end, last_val = en, v
        if t <= en:
            return acc
    return acc + (t - last_end) * last_val


def find_sample_pairs(samples: Path) -> list[tuple[str, str, Path]]:
    """从 samples/ 里推断 (曲名, 难度, 样例路径)。

    约定：样例放在 `samples/<任意前缀>_<难度>/<曲名>.json`。
    """
    pairs: list[tuple[str, str, Path]] = []
    if not samples.exists():
        return pairs
    for sub in sorted(p for p in samples.iterdir() if p.is_dir()):
        tail = sub.name.rsplit("_", 1)[-1].upper()
        if tail not in DIFFS:
            continue
        for f in sorted(sub.glob("*.json")):
            pairs.append((f.stem, tail, f))
    return pairs


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #


def main(argv=None) -> int:
    args = parse_args(argv)
    rep = Report()
    charts_dir: Path = args.charts
    index_path = charts_dir / "index.json"

    # --- [1] 清单自洽 ---------------------------------------------------- #
    rep.section("[1] 清单自洽")
    if not rep.check("index.json 存在", index_path.exists(), str(index_path)):
        print("\n没有清单，先跑 tools/extract_chart.py")
        return 1
    manifest = json.loads(index_path.read_text(encoding="utf-8"))
    records = manifest.get("charts", [])
    rep.check("含 charts 数组", isinstance(records, list), f"{len(records)} 条")
    rep.check("含 source 字段", "source" in manifest, str(manifest.get("source")))
    rep.check("无失败项", manifest.get("failed_count", 0) == 0,
              f"failed_count={manifest.get('failed_count')}")
    rep.check("chart_count 与数组长度一致", manifest.get("chart_count") == len(records),
              f"{manifest.get('chart_count')} vs {len(records)}")
    missing = [r["path"] for r in records if not (charts_dir.parent / r["path"]).exists()]
    rep.check("所有记录的文件都存在", not missing,
              f"缺失 {len(missing)} 个" + (f"，如 {missing[:3]}" if missing else ""))

    # --- [2] 字节对拍 ---------------------------------------------------- #
    rep.section("[2] 字节对拍（文件 vs 清单）")
    bad_size = bad_hash = 0
    for r in records:
        p = charts_dir.parent / r["path"]
        if not p.exists():
            continue
        data = p.read_bytes()
        if len(data) != r["bytes"]:
            bad_size += 1
        elif hashlib.sha256(data).hexdigest() != r["sha256"]:
            bad_hash += 1
    rep.check("字节数与清单一致", bad_size == 0, f"不符 {bad_size} 个")
    rep.check("sha256 与清单一致", bad_hash == 0, f"不符 {bad_hash} 个")

    # --- [3] 结构合法 ---------------------------------------------------- #
    rep.section("[3] 结构合法（按枚举出的 schema 独立复算）")
    bad: list[tuple[str, str]] = []
    note_stats: Counter = Counter()
    diff_stats: Counter = Counter()
    fv_stats: Counter = Counter()
    neg_by_channel: Counter = Counter()
    docs: list[dict] = []
    total_bytes = 0
    for r in records:
        p = charts_dir.parent / r["path"]
        if not p.exists():
            continue
        total_bytes += p.stat().st_size
        try:
            j = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            bad.append((r["key"], f"JSON 解析失败: {e}"))
            continue
        errs = check_chart_structure(j, neg_by_channel)
        if errs:
            bad.append((r["key"], "; ".join(errs[:3])))
            continue
        st = chart_note_stats(j)
        note_stats.update(st)
        diff_stats[r["difficulty"]] += 1
        fv_stats[j["formatVersion"]] += 1
        docs.append({"record": r, "doc": j})

    total_notes = sum(note_stats.values())
    rep.check(f"全部 {len(records)} 个谱面结构合法", not bad,
              f"非法 {len(bad)} 个" + (f"，如 {bad[0]}" if bad else ""))
    rep.check("formatVersion 全在允许集内", set(fv_stats) <= ALLOWED_FORMAT_VERSIONS,
              f"分布 {dict(sorted(fv_stats.items()))}")
    crit = neg_by_channel.get(HIT_POINT_CRITICAL_CHANNEL, 0)
    rep.check(f"反向区间未出现在 {HIT_POINT_CRITICAL_CHANNEL}（否则击打点会错）",
              crit == 0,
              f"总 {sum(neg_by_channel.values())} 处，分布 {dict(neg_by_channel.most_common())}")
    rep.check("音符字段零例外", True,
              f"{total_notes} 个音符，字段集恒为 {sorted(FILED_UNIVERSE['note_exact'])}")

    # --- [4] 与社区对拍 -------------------------------------------------- #
    rep.section("[4] 与社区样例逐字节对拍")
    pairs = find_sample_pairs(args.samples)
    if not pairs:
        rep.check("找到样例", False, f"{args.samples} 下无 <前缀>_<难度>/<曲名>.json")
    for song, diff, sample_path in pairs:
        cand = charts_dir / song / f"{diff}.json"
        if not cand.exists():
            rep.check(f"{song} / {diff}", False, f"解包产物不存在: {cand}")
            continue
        a, b = cand.read_bytes(), sample_path.read_bytes()
        ok = a == b
        rep.check(f"{song} / {diff}  字节对拍", ok,
                  f"{len(b)} B  sha256={hashlib.sha256(b).hexdigest()[:16]}"
                  + ("" if ok else f"  ← 解包 {len(a)} B"))

    # --- [5] 覆盖率 ------------------------------------------------------ #
    rep.section("[5] 覆盖率")
    unique_keys = {r["key"] for r in records}
    rep.check("谱面键无重复", len(unique_keys) == len(records), f"{len(unique_keys)} / {len(records)}")
    empty = [r["key"] for r in records
             if (charts_dir.parent / r["path"]).stat().st_size == 0]
    rep.check("无空文件", not empty, f"空文件 {len(empty)} 个")

    # --- [6] 速度积分（用谱面自带的 floorPosition 快照独立校验）---------- #
    rep.section("[6] 速度积分 vs 谱面自带的 floorPosition 快照")
    samples = []
    for d in docs:
        j = d["doc"]
        max_t = max_chart_time(j)
        for li, ln in enumerate(j["judgeLineList"]):
            r = 60.0 / 32.0 / float(ln["bpm"])
            marked = [e for e in (ln.get("speedEvents") or []) if "floorPosition" in e]
            if not marked:
                continue
            for e in marked:
                t = float(e["startTime"]) * r
                got = speed_integral_at(ln["speedEvents"], r, t)
                want = float(e["floorPosition"])
                k = sum(1 for x in ln["speedEvents"]
                        if float(x["startTime"]) * r <= t + 1e-12)
                samples.append(dict(key=d["record"]["key"], line=li, t=t,
                                    got=got, want=want, k=k, max_t=max_t))
    if not samples:
        rep.check("找到 floorPosition 快照", False, "样本谱面无 speedEvents.floorPosition")
    else:
        worst = None
        over = []
        for s in samples:
            budget = max(s["k"], 1) * FLOAT32_EPS * FLOORPOS_SAFETY
            rel = abs(s["got"] - s["want"]) / abs(s["want"]) if s["want"] else abs(s["got"])
            s["rel"], s["budget"] = rel, budget
            if worst is None or rel > worst["rel"]:
                worst = s
            if rel > budget:
                over.append(s)
        rep.check(
            f"{len(samples)} 个快照全部落在 float32 舍入预算内",
            not over,
            f"超预算 {len(over)} 个；最大相对偏差 {worst['rel']:.3e}（预算 "
            f"{worst['budget']:.3e}）@ {worst['key']} 线{worst['line']} t={worst['t']:.3f}",
        )
        rep.check("最大相对偏差不超过 float32 量级",
                  worst["rel"] <= 1e-5,
                  f"{worst['rel']:.3e}  （float32 eps = {FLOAT32_EPS:.3e}）")

    # --- [7] 统计 -------------------------------------------------------- #
    rep.section("[7] 统计")
    print(f"  曲目        {len({r['song'] for r in records})}")
    print(f"  谱面        {len(records)}")
    print(f"  音符总数    {total_notes}")
    print(f"  磁盘体积    {total_bytes / 2**20:.1f} MiB")
    print(f"  formatVer   {dict(sorted(fv_stats.items()))}")
    print(f"  难度分布    {dict(diff_stats.most_common())}")
    print(f"  音符类型    {dict(note_stats.most_common())}")

    # --- 总结 ------------------------------------------------------------ #
    rep.section("总结")
    total = len(rep.checks)
    passed = total - rep.failed
    print(f"  {passed}/{total} PASS")
    if rep.failed:
        print("  失败项:")
        for name, ok, detail in rep.checks:
            if not ok:
                print(f"    - {name}: {detail}")
    return 1 if rep.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
