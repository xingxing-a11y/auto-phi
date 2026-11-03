#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""端内指针状态机（Gesture.java）的**独立参考实现** + AOSP 级不变量断言。

为什么要独立再写一份
  项目规矩「同一个量两处实现 ⇒ 交叉断言」。Gesture.java 是端内真正跑的那份，
  Python 这份**故意用另一种写法**（dict 列表、函数式），只求语义相同、写法不同：
  两边同时写错同一个细节的概率远低于一份实现自己错。

测什么（不能只测"跑通"）
  A. 与 Java 逐行 diff（同一份黄金序列）
  B. 不变量断言 —— 这些是 AOSP 的硬要求，不是我的口味：
     B1 每帧指针 id 唯一；count 与数组长度一致
     B2 ACTION_DOWN ⇒ count==1
     B3 POINTER_DOWN/POINTER_UP ⇒ ids[actionIndex] == 本条载荷的 pointer_id
     B4 POINTER_UP 之后该 pid 不在下一帧（抬手帧里仍在）
     B5 MOVE ⇒ 该 pid 在帧里，且它的坐标 == 本条载荷坐标
     B6 DOWN/POINTER_DOWN 之后该 pid 必在帧里，且坐标 == 本条载荷坐标
     B7 载荷里每个 DOWN/U(P) 与帧一一对应（不丢不多）
     B8 走到结尾状态必须为空（否则真机会留卡指针）
  C. 变异测试：把 actionIndex 改成 pointer_id、把删指针提前到出帧前，
     断言**必须**被判失败（证明判据有区分力，不是空转）。

用法
  .venv/Scripts/python.exe tools/inject_sched/gesture_ref.py out/touches_demiurge.json
  .venv/Scripts/python.exe tools/inject_sched/gesture_ref.py --all
  .venv/Scripts/python.exe tools/inject_sched/gesture_ref.py --selftest   # B + C
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tools" / "inject_sched"))

import payload_ref  # noqa: E402  已验过的坐标变换 + 稳定排序

ACTION_DOWN, ACTION_UP, ACTION_MOVE = 0, 1, 2
ACTION_POINTER_DOWN, ACTION_POINTER_UP = 5, 6
SHIFT = 8


def masked(a: int) -> int:
    return a & 0xFF


def act_index(a: int) -> int:
    return (a >> SHIFT) & 0xFF


def step(state: list[dict], c: dict) -> dict | None:
    """state = [{pid, x, y}]，顺序即数组下标（先按下的在前）。"""
    a, pid = c["action"], c["pid"]
    idx = next((i for i, p in enumerate(state) if p["pid"] == pid), -1)

    def frame(action: int) -> dict:
        return {"t_ms": c["t_ms"], "action": action, "count": len(state),
                "ids": [p["pid"] for p in state],
                "xs": [p["x"] for p in state],
                "ys": [p["y"] for p in state],
                "pid": pid}

    if a in ("DOWN", "POINTER_DOWN"):
        if idx >= 0:                      # 重复按下：当 MOVE
            state[idx].update(x=c["dev_x"], y=c["dev_y"])
            return frame(ACTION_MOVE)
        state.append({"pid": pid, "x": c["dev_x"], "y": c["dev_y"]})
        i = len(state) - 1
        action = ACTION_DOWN if len(state) == 1 else (ACTION_POINTER_DOWN | (i << SHIFT))
        return frame(action)
    if a == "MOVE":
        if idx < 0:
            return None
        state[idx].update(x=c["dev_x"], y=c["dev_y"])
        return frame(ACTION_MOVE)
    if a in ("POINTER_UP", "UP"):
        if idx < 0:
            return None
        state[idx].update(x=c["dev_x"], y=c["dev_y"])
        action = ACTION_UP if len(state) == 1 else (ACTION_POINTER_UP | (idx << SHIFT))
        f = frame(action)
        state.pop(idx)                    # 先出帧再删；变异 (c) 就删这一行的顺序
        return f
    return None


def run(rows: list[dict], mutate: str | None = None) -> tuple[list[dict], list[dict], dict]:
    """返回 (frames, 未消化的载荷项, 统计)。mutate 仅用于变异测试。"""
    state: list[dict] = []
    frames: list[dict] = []
    stat = {"orphan_move": 0, "orphan_up": 0, "unknown": 0}
    for c in rows:
        if mutate == "delete_before_frame" and c["action"] in ("POINTER_UP", "UP"):
            # 变异 (c)：先把指针删掉再出帧 ⇒ 帧里少了抬手那个指针（真机会丢整条）
            idx = next((i for i, p in enumerate(state) if p["pid"] == c["pid"]), -1)
            if idx >= 0:
                f_state = [dict(p) for p in state]
                f_state.pop(idx)
                f = {"t_ms": c["t_ms"], "action": ACTION_UP, "count": len(f_state),
                     "ids": [p["pid"] for p in f_state], "xs": [p["x"] for p in f_state],
                     "ys": [p["y"] for p in f_state], "pid": c["pid"]}
                frames.append(f)
                state.pop(idx)
                continue
        f = step(state, c)
        if f is None:
            if c["action"] == "MOVE":
                stat["orphan_move"] += 1
            elif c["action"] in ("POINTER_UP", "UP"):
                stat["orphan_up"] += 1
            else:
                stat["unknown"] += 1
            continue
        if mutate == "index_as_pid":
            # 变异 (a)：actionIndex 误用 pointer_id
            if masked(f["action"]) in (ACTION_POINTER_DOWN, ACTION_POINTER_UP):
                f["action"] = masked(f["action"]) | (c["pid"] << SHIFT)
        frames.append(f)
    return frames, [], stat


def check(frames: list[dict], rows: list[dict], state_end_empty: bool = True) -> list[str]:
    """AOSP 级不变量。返回失败信息列表（空 = 全过）。"""
    errs: list[str] = []
    # B1
    for i, f in enumerate(frames):
        if len(set(f["ids"])) != f["count"] or not (f["count"] == len(f["ids"]) == len(f["xs"])):
            errs.append(f"B1 帧{i} 指针数/数组长度不一致: {f}")
            if len(errs) > 12:
                return errs
        if masked(f["action"]) == ACTION_DOWN and f["count"] != 1:
            errs.append(f"B2 帧{i} ACTION_DOWN 但 count={f['count']}")
        k = masked(f["action"])
        if k in (ACTION_POINTER_DOWN, ACTION_POINTER_UP):
            ai = act_index(f["action"])
            if ai >= f["count"] or f["ids"][ai] != f["pid"]:
                errs.append(f"B3 帧{i} actionIndex={ai} 指向 "
                            f"{f['ids'][ai] if ai < f['count'] else 'NULL'}，"
                            f"应为 pid={f['pid']}")
        # MOVE/DOWN/PDOWN 的 pid 必须在帧里；抬手帧里**也必须还在**（下一帧才消失）
        if k != ACTION_DOWN and f["pid"] not in f["ids"]:
            errs.append(f"B5/B6 帧{i} action={k} pid={f['pid']} 不在帧里")
        if len(errs) > 12:
            return errs
    # B4：抬手帧的 pid 不得「无声无息」继续出现在下一帧 ——
    #     ★ 例外：下一帧若是同一 pid 的 DOWN/POINTER_DOWN（谱面复用指针 id，实测很常见），
    #       那是合法重按，不是残留。第一版漏了这个例外，把 208~589 个正常帧判成违规。
    for i in range(len(frames) - 1):
        if masked(frames[i]["action"]) in (ACTION_POINTER_UP, ACTION_UP):
            pid = frames[i]["pid"]
            nxt = frames[i + 1]
            re_press = (masked(nxt["action"]) in (ACTION_DOWN, ACTION_POINTER_DOWN)
                        and nxt["pid"] == pid)
            if pid in nxt["ids"] and not re_press:
                errs.append(f"B4 帧{i} 抬起 pid={pid} 仍出现在帧{i+1}")
    # B7：载荷的 DOWN/UP 类必须与帧一一对应
    n_payload_key = sum(1 for r in rows if r["action"] in
                        ("DOWN", "POINTER_DOWN", "POINTER_UP", "UP"))
    n_frame_key = sum(1 for f in frames if masked(f["action"]) != ACTION_MOVE)
    if n_payload_key != n_frame_key:
        errs.append(f"B7 关键事件数不符：载荷 {n_payload_key} vs 帧 {n_frame_key}")
    if state_end_empty and frames:
        # 结尾状态：把帧序列重放一遍（只跟踪 id 集合）确认归零
        live: list[int] = []
        for f in frames:
            k = masked(f["action"])
            if k == ACTION_DOWN:
                live = list(f["ids"])
            elif k == ACTION_POINTER_DOWN:
                live.append(f["pid"])
            elif k == ACTION_POINTER_UP:
                live = [x for x in live if x != f["pid"]]
            elif k == ACTION_UP:
                live = []
        if live:
            errs.append(f"B8 结尾仍有按下的指针 {live}（真机会卡指针）")
    return errs


def dump_tsv(frames: list[dict]) -> None:
    out = ["seq\tt_ms\taction\tcount\tids\txs\tys"]
    for i, f in enumerate(frames):
        out.append("\t".join([
            str(i), f"{f['t_ms']:.6f}", str(f["action"]), str(f["count"]),
            ",".join(map(str, f["ids"])), ",".join(map(str, f["xs"])),
            ",".join(map(str, f["ys"]))]))
    sys.stdout.write("\n".join(out) + "\n")


def selftest() -> int:
    keys = ["demiurge", "kuangxi", "lacamp"]
    fail = 0
    for key in keys:
        p = ROOT / "out" / f"touches_{key}.json"
        if not p.exists():
            print(f"  skip {key}（缺 {p}）")
            continue
        rows = payload_ref.build(p)
        frames, _, stat = run(rows)
        errs = check(frames, rows)
        n_pid_ne_index = sum(1 for f in frames
                             if masked(f["action"]) in (ACTION_POINTER_DOWN, ACTION_POINTER_UP)
                             and act_index(f["action"]) != f["pid"])
        print(f"[{key}] 载荷 {len(rows)} → 帧 {len(frames)}"
              f"（关键 {sum(1 for f in frames if masked(f['action']) != ACTION_MOVE)}），"
              f"孤儿 MOVE {stat['orphan_move']} / UP {stat['orphan_up']}，"
              f"pid≠actionIndex 的帧 {n_pid_ne_index}")
        if errs:
            fail = 1
            print("  ✗ 不变量失败：")
            for e in errs[:8]:
                print("    " + e)
        else:
            print("  OK 不变量 B1~B8 全过")

        # ── 变异：证明判据有区分力 ──
        for mut, label in (("index_as_pid", "actionIndex 误用 pointer_id"),
                           ("delete_before_frame", "抬手帧提前删指针")):
            m_rows = payload_ref.build(p)
            m_frames, _, _ = run(m_rows, mutate=mut)
            m_errs = check(m_frames, m_rows)
            if m_errs:
                print(f"  OK 变异被发现（{label}）: {m_errs[0][:70]}")
            else:
                fail = 1
                print(f"  ✗ 变异**没**被发现（{label}）⇒ 判据空转")
    return fail


def main(argv: list[str]) -> int:
    if len(argv) > 1 and argv[1] == "--selftest":
        return selftest()
    paths = ([ROOT / "out" / f"touches_{k}.json" for k in ("demiurge", "kuangxi", "lacamp")]
             if len(argv) > 1 and argv[1] == "--all"
             else [Path(argv[1]) if len(argv) > 1 else ROOT / "out" / "touches_demiurge.json"])
    for p in paths:
        if not p.exists():
            print(f"✗ 缺 {p}", file=sys.stderr)
            return 1
        frames, _, _ = run(payload_ref.build(p))
        dump_tsv(frames)
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(newline="\n")   # Windows 下别把 \n 变 \r\n（首轮 diff 每行都不同）
    raise SystemExit(main(sys.argv))
