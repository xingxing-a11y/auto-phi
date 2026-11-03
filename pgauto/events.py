"""官方谱事件 → 关键帧动画。

逐函数对齐 `prpr/src/parse/pgr.rs`，包括其中几处反直觉但有意的处理：

- 负哨兵：首事件的 `startTime` 常为 `-999999`，被 `.max(0.)` 夹到 0；
- 末哨兵：末事件的 `endTime` 常为 `1e9`，其产生的结束帧被 `pop()` 丢弃。
  该帧的 start 与 end 值通常相同，故丢弃不影响取值；
- 去重：若上一帧的值已等于本事件的 `start`，则不再插入重复帧。

三类解析器对应的事件：

    parse_float_events   旋转角（judgeLineRotateEvents）、透明度（judgeLineDisappearEvents）
    parse_move_events    位置（judgeLineMoveEvents），formatVersion 3
    parse_move_events_fv1 同上，formatVersion 1
    parse_speed_events   流速（speedEvents）→ 判定线「高度」的累计曲线
"""

from __future__ import annotations

from typing import Any, Dict, List, Sequence, Tuple

from .anim import AnimFloat, AnimVec

Events = Sequence[Dict[str, Any]]
Keyframes = List[Tuple[float, float]]


def parse_float_events(r: float, events: Events) -> AnimFloat:
    """标量事件（旋转角、透明度）。"""
    kfs: Keyframes = []

    for e in events:
        start = float(e["start"])
        if not (kfs and kfs[-1][1] == start):
            kfs.append((max(float(e["startTime"]) * r, 0.0), start))
        kfs.append((float(e["endTime"]) * r, float(e["end"])))

    if kfs:
        kfs.pop()

    return AnimFloat(kfs)


def parse_move_events(r: float, events: Events) -> AnimVec:
    """formatVersion 3 的移动事件：x / y 已拆成 `start/end` 与 `start2/end2`。"""
    kf_x: Keyframes = []
    kf_y: Keyframes = []

    for e in events:
        st = max(float(e["startTime"]) * r, 0.0)
        en = float(e["endTime"]) * r
        start_x, start_y = float(e["start"]), float(e.get("start2", 0.0))
        end_x, end_y = float(e["end"]), float(e.get("end2", 0.0))

        if not (kf_x and kf_x[-1][1] == start_x):
            kf_x.append((st, start_x))
        if not (kf_y and kf_y[-1][1] == start_y):
            kf_y.append((st, start_y))

        kf_x.append((en, end_x))
        kf_y.append((en, end_y))

    if kf_x:
        kf_x.pop()
    if kf_y:
        kf_y.pop()

    # [0, 1] 归一化 → 世界坐标 [-1, 1]
    kf_x = [(t, -1.0 + v * 2.0) for t, v in kf_x]
    kf_y = [(t, -1.0 + v * 2.0) for t, v in kf_y]

    return AnimVec(AnimFloat(kf_x), AnimFloat(kf_y))


def parse_move_events_fv1(r: float, events: Events) -> AnimVec:
    """formatVersion 1 的移动事件：x / y 压成 `1000 * x + y`。

    x 取值域 0..880、y 取值域 0..520（官谱 v1 的像素坐标系）。
    注意 `kf_y` 的去重比较用 `start2`，而写入用 `start % 1000` —— 与 Phira 原样一致。
    """
    kf_x: Keyframes = []
    kf_y: Keyframes = []

    for e in events:
        st = max(float(e["startTime"]) * r, 0.0)
        en = float(e["endTime"]) * r
        start, start2 = float(e["start"]), float(e.get("start2", 0.0))
        end, end2 = float(e["end"]), float(e.get("end2", 0.0))

        if not (kf_x and kf_x[-1][1] == start):
            kf_x.append((st, (start - start % 1000.0) / 1000.0))
        if not (kf_y and kf_y[-1][1] == start2):
            kf_y.append((st, start % 1000.0))

        kf_x.append((en, (end - end % 1000.0) / 1000.0))
        kf_y.append((en, end % 1000.0))

    if kf_x:
        kf_x.pop()
    if kf_y:
        kf_y.pop()

    kf_x = [(t, (-880.0 + v * 2.0) / 880.0) for t, v in kf_x]
    kf_y = [(t, (-520.0 + v * 2.0) / 520.0) for t, v in kf_y]

    return AnimVec(AnimFloat(kf_x), AnimFloat(kf_y))


def parse_speed_events(r: float, events: Events, max_time: float) -> AnimFloat:
    """流速事件 → 判定线「高度」对时间的累计积分。

    这是音符进场的动力来源：判定线高度 h(t) = ∫ v(t) dt，音符在 h(note.time)
    处、判定线在 h(t) 处，两者的差决定音符离判定线多远。

    与 Phira `parse_speed_events` 的两处细节：

    1. ``pos`` 是**逐事件累加** ``(endTime - startTime) * r * value``，与事件之间
       是否有空隙/重叠无关；因为官谱把事件首尾相接铺满整条时间轴，
       等价于对 v(t) 的严格积分。
    2. 末事件之后要按它自己的 ``value`` 继续外推，直到 ``max_time``。**这一帧不能省**
       —— 省掉之后所有位于末事件之后的音符高度会偏小，最坏偏差 94（见 `tools/verify_timeline.py`）。

    **返回的是未做任何缩放的原值。** 官谱每个音符都带一个 `floorPosition` 字段，
    它就是本条曲线的取值；样本谱 1300/1300 逐音符精确吻合（最大偏差 1e-4 = float32 舍入）。
    因此这里不做 ``/ HEIGHT_RATIO`` —— 那个除数是**渲染**用的尺度，放在 `chart.note_pos`
    里显式出现，好处是 ``note_h == floorPosition`` 可以直接断言，不被魔数遮住。
    """
    ev: List[Dict[str, Any]] = [
        dict(e) for e in events if float(e["startTime"]) <= float(e["endTime"])
    ]
    if not ev:
        return AnimFloat([])

    # Phira 会把首事件的起点强制拉到 0（并 `assert_eq!(pgr[0].start_time, 0.0)` 的注释
    # 说明这属于「厂商偶尔不守规矩」的兜底）
    if float(ev[0]["startTime"]) != 0.0:
        ev[0]["startTime"] = 0.0

    kfs: Keyframes = []
    pos = 0.0
    for e in ev[:-1]:
        kfs.append((float(e["startTime"]) * r, pos))
        pos += (float(e["endTime"]) - float(e["startTime"])) * r * float(e["value"])

    last = ev[-1]
    last_start = float(last["startTime"]) * r
    kfs.append((last_start, pos))
    kfs.append((max_time, pos + (max_time - last_start) * float(last["value"])))

    return AnimFloat(kfs)
