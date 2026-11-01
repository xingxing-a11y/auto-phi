"""Phigros 官方谱（PGR）解析：谱面 JSON → 击打点 + 随时间运动的轨迹。

坐标链路（对齐 Phira prpr 与 sim-phi 两套**独立**实现，两者逐项吻合）：

    note.positionX      --(* X_UNIT)--------------> 判定线局部坐标的横向偏移 local_x
    judgeLineMoveEvent  --([0,1] → [-1,1], y /a)---> 判定线世界位置 pos = (line_x, line_y / a)
    judgeLineRotateEvent--(度，逆时针)------------> 判定线倾角 theta
    speedEvents         --(对时间积分)------------> 判定线高度 h(t)（== 官谱 floorPosition）
    击打点世界坐标 = pos + R(theta) · (local_x, 0)
    世界 → 像素：1 世界单位 = 屏幕半宽，y 轴向上（屏幕 y 轴向下，故取负）

**旋转中心是判定线自身，不是屏幕中心。** 三条独立证据：

1. sim-phi（lchzh3473/sim-phi，src/core.ts:463-467）显式写成
   `offset = line.offset + R·(dx, dy)`，即先旋转局部偏移、再平移到线位置。
2. 本包把 Phira 的 `now_transform = R·T` 与 `fetch_pos` 展开成屏幕像素后，
   与 sim-phi 的 `offsetX/offsetY` 表达式**逐项相等**（含正弦项符号），
   而该表达式的形式就是「线位置 + 旋转后的局部偏移」。
3. 样本官谱 Rrhar'il AT 的外部约束「音符必须在屏幕内」：绕屏幕中心旋转会让
   line 2（line_x≈0.8、90°）的 42 个音符越界，最大越界 554.7 px（纵向可见范围
   只有 |y| <= 0.5625，线中心被甩到 y=0.8 直接飞出屏幕）；绕自身旋转则 0 越界。

音符在**任意时刻**的世界坐标（M2 补齐的那一半）：

    local_x = positionX * X_UNIT
    dy(t)   = (note_h - h(t)) / (HEIGHT_RATIO * aspect_ratio) * speed     # 上侧取 +dy，下侧取 -dy
    world   = pos(t) + R(theta(t)) · (local_x, dy)
    note_h  = h(note.t_sec) == 官谱的 floorPosition 字段

    ⇒ 击打时刻 dy == 0，音符**精确落在判定线上**（不是近似）。

`dy` 的尺度常量 `1 / HEIGHT_RATIO`：Phira 用 `1 / 0.83175 = 1.2023`，
sim-phi 用 `1.2`（`scaleY = canvasH * 0.6` 折成世界单位），两者差 0.19%。
**该常量只影响音符的接近速度观感，不影响击打点** —— 击打时刻 dy 恒为 0，
所以即便它偏了 20% 也不会让 C 段的 `touches.json` 错一个像素。

上下侧的差别只在 dy 的符号：镜像轴是判定线自身，故 `local_x` 不变、
垂直于线的分量反号。发射到局部 y != 0 时两者才分叉；击打点（dy=0）上完全一致。

时间链路：

    r = 60 / 32 / bpm                # 一个 time 单位对应的秒数
    t_sec = note.time * r            # 相对谱面时间轴原点
    t_music = t_sec + offset         # offset 为「音乐延时」，见 Phira game.rs:1018
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import events

#: positionX 的 1 个单位对应的世界坐标长度。
#: 对应 prpr/src/parse/pgr.rs：`pgr.position_x * (2. * 9. / 160.)`
X_UNIT: float = 2.0 * 9.0 / 160.0  # = 0.1125

#: 判定线「高度」→ 世界坐标的尺度除数（Phira `prpr/src/core.rs::HEIGHT_RATIO`）。
#: sim-phi 等价地用 `1.2`；两者差 0.19%，见模块文档。
HEIGHT_RATIO: float = 0.83175

#: Phigros 画面默认宽高比（16:9）。Phira 中由 config / info 提供。
DEFAULT_ASPECT_RATIO: float = 16.0 / 9.0

DEFAULT_SCREEN = (1920.0, 1080.0)

NOTE_TYPE_NAMES: Dict[int, str] = {1: "Tap", 2: "Drag", 3: "Hold", 4: "Flick"}

#: 末事件之后速度曲线外推的时长余量（对齐 Phira `parse_phigros` 里的 `+ 1.`）
MAX_TIME_MARGIN: float = 1.0

#: 重算高度与官谱 `floorPosition` 字段的允许偏差（官谱为 float32，实测最大 1e-4）
FLOOR_POSITION_TOL: float = 1e-3


@dataclass
class NoteHit:
    """一个音符的击打点，以及复算轨迹所需的原始量。"""

    type: int
    line: int
    above: bool
    t_sec: float
    hold_sec: float
    multiple: bool
    world_x: float
    world_y: float
    px: float
    py: float
    position_x: float = 0.0
    speed: float = 1.0
    floor_position: float = 0.0
    note_h: float = 0.0

    @property
    def type_name(self) -> str:
        return NOTE_TYPE_NAMES.get(self.type, "?")

    @property
    def t_ms(self) -> float:
        return self.t_sec * 1000.0


@dataclass
class _Line:
    id: int
    bpm: float
    r: float
    move: Any
    rotate: Any
    alpha: Any
    height: Any
    raw: Dict[str, Any]


def load(path: str | Path) -> Dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as fh:
        return json.load(fh)


class Chart:
    """解析后的谱面。

    :param raw: 谱面 JSON
    :param aspect_ratio: 画面宽高比，进入世界坐标前 y 需除以它
    :param screen: 屏幕像素尺寸 (宽, 高)，用于输出像素坐标
    """

    def __init__(
        self,
        raw: Dict[str, Any],
        aspect_ratio: float = DEFAULT_ASPECT_RATIO,
        screen: Sequence[float] = DEFAULT_SCREEN,
    ):
        self.format_version = int(raw["formatVersion"])
        if self.format_version not in (1, 3):
            raise ValueError(f"unsupported formatVersion: {self.format_version}")

        self.offset = float(raw.get("offset", 0.0))
        self.aspect_ratio = aspect_ratio
        self.screen_w, self.screen_h = float(screen[0]), float(screen[1])

        raw_lines = raw.get("judgeLineList", [])
        self.max_time = self._compute_max_time(raw_lines)
        self.lines: List[_Line] = [
            self._build_line(i, ln) for i, ln in enumerate(raw_lines)
        ]

        #: `notes()` 调用后填入：(line, t_sec, 重算高度, 官谱 floorPosition) 分歧清单
        self.note_h_mismatch: List[Tuple[int, float, float, float]] = []

    def _compute_max_time(self, raw_lines: Sequence[Dict[str, Any]]) -> float:
        """速度曲线外推的终点，对齐 Phira `parse_phigros`。

        取「所有音符时刻（各自按所属判定线的 bpm 换算成秒）的最大值 + 1 秒」。
        末尾的 +1 是给末事件之后留出外推区间。
        """
        best = 0.0
        for ln in raw_lines:
            r = 60.0 / 32.0 / float(ln["bpm"])
            for bucket in ("notesAbove", "notesBelow"):
                for note in ln.get(bucket, []):
                    t = float(note["time"])
                    if t < 1e8:  # 官方谱可能塞入 1e9 之类的时间哨兵，排除
                        best = max(best, t * r)
        return best + MAX_TIME_MARGIN

    def _build_line(self, index: int, raw: Dict[str, Any]) -> _Line:
        bpm = float(raw["bpm"])
        r = 60.0 / 32.0 / bpm

        move_events = raw.get("judgeLineMoveEvents", [])
        if self.format_version == 1:
            move = events.parse_move_events_fv1(r, move_events)
        else:
            move = events.parse_move_events(r, move_events)

        rotate = events.parse_float_events(r, raw.get("judgeLineRotateEvents", []))
        alpha = events.parse_float_events(r, raw.get("judgeLineDisappearEvents", []))
        height = events.parse_speed_events(r, raw.get("speedEvents", []), self.max_time)

        return _Line(index, bpm, r, move, rotate, alpha, height, raw)

    # ---------------------------------------------------------------- 查询

    def line_state(self, line_id: int, t: float) -> Tuple[float, float, float, float]:
        """t 时刻判定线的状态：(世界 x, 世界 y, 角度 度, 透明度)。"""
        line = self.lines[line_id]
        lx, ly = line.move.at(t)
        return lx, ly / self.aspect_ratio, line.rotate.at(t), line.alpha.at(t)

    def line_height(self, line_id: int, t: float) -> float:
        """t 时刻判定线的累计高度（== 官谱 floorPosition 曲线在该时刻的取值）。"""
        return self.lines[line_id].height.at(t)

    def note_travel(self, hit: NoteHit, t: float) -> float:
        """音符相对判定线的局部纵向偏移 dy（世界坐标长度）。

        正值 = 音符在判定线「上方」（对应 notesAbove），负值 = 下方。
        `t == hit.t_sec` 时恒为 0。

        用重算的 `note_h` 而非官谱的 `floorPosition` 字段，与 Phira 一致：
        前者由 `note_travel` 自己从速度曲线算出，能**保证** dy(t_hit) == 0，
        即音符必然落在判定线上。官谱两者相等（样本谱 1300/1300），
        若第三方谱出现分歧，`Chart.note_h_mismatch` 会记录而不是静默吞掉。
        """
        line = self.lines[hit.line]
        # Hold（type 3）在 Phira 里被强制成 speed 1.0，见 pgr.rs::parse_notes
        speed = 1.0 if hit.type == 3 else hit.speed
        dy = (hit.note_h - line.height.at(t)) / (HEIGHT_RATIO * self.aspect_ratio) * speed
        return dy if hit.above else -dy

    def note_pos(self, hit: NoteHit, t: float) -> Tuple[float, float]:
        """t 时刻音符的世界坐标。"""
        lx, ly, angle_deg, _ = self.line_state(hit.line, t)
        base_x = hit.position_x * X_UNIT
        dy = self.note_travel(hit, t)

        rad = math.radians(angle_deg)
        cos_a, sin_a = math.cos(rad), math.sin(rad)
        return lx + base_x * cos_a - dy * sin_a, ly + base_x * sin_a + dy * cos_a

    def line_events_payload(self) -> List[Dict[str, Any]]:
        """判定线的几何事件（**原样**，未解码）。

        判定区跟着判定线移动 / 旋转，所以 C 段规划长按的跟随路径必须知道它。
        原样带出（而不是先解码成坐标）是为了让下游能**独立解码**并与本包对拍：
        formatVersion 1 的 `1000·x + y` 打包由消费方按 `format_version` 自行还原。
        """
        out: List[Dict[str, Any]] = []
        for ln in self.lines:
            raw = ln.raw
            out.append({
                "bpm": ln.bpm,
                "move_events": [
                    [e["startTime"], e["endTime"], e["start"], e["end"],
                     e.get("start2", 0.0), e.get("end2", 0.0)]
                    for e in raw.get("judgeLineMoveEvents", [])
                ],
                "rotate_events": [
                    [e["startTime"], e["endTime"], e["start"], e["end"]]
                    for e in raw.get("judgeLineRotateEvents", [])
                ],
            })
        return out

    def line_point(self, line_id: int, position_x: float, t: float) -> Tuple[float, float]:
        """判定线上 `positionX` 处的点在 t 时刻的像素坐标。

        这就是 `note_pos` 取 **dy == 0**（音符正落在判定线上）时的值，也就是该音符的
        **判定位置**。它与「音符渲染位置」不是一回事：渲染位置在接近判定线的过程中
        会随 `dy` 漂移，而音频判定用的矩形**长在判定线上**，跟着判定线移动 / 旋转。

        C 段规划长按的跟随路径时必须用这一支：`t == 击打时刻` 时两者相等，
        但 `t > 击打时刻` 之后只有本式仍然代表「手指该待在哪」。
        """
        lx, ly, angle_deg, _ = self.line_state(line_id, t)
        base_x = position_x * X_UNIT
        rad = math.radians(angle_deg)
        return self.to_px(lx + base_x * math.cos(rad), ly + base_x * math.sin(rad))

    def to_px(self, world_x: float, world_y: float) -> Tuple[float, float]:
        """世界坐标 → 像素（原点左上、y 向下）。1 世界单位 = 屏幕半宽，各向同性。"""
        half = self.screen_w / 2.0
        return half + world_x * half, self.screen_h / 2.0 - world_y * half

    def _hit_point(self, line: _Line, raw_note: Dict[str, Any]) -> NoteHit:
        t = float(raw_note["time"]) * line.r

        # 击打时刻 dy == 0，音符落在判定线上，故直接走 note_pos 的同一套公式
        position_x = float(raw_note["positionX"])
        speed = float(raw_note.get("speed", 1.0))
        note_h = line.height.at(t)
        hit = NoteHit(
            type=int(raw_note["type"]),
            line=line.id,
            above=bool(raw_note.get("_above", True)),
            t_sec=t,
            hold_sec=float(raw_note.get("holdTime", 0.0)) * line.r,
            multiple=False,
            world_x=0.0,
            world_y=0.0,
            px=0.0,
            py=0.0,
            position_x=position_x,
            speed=speed,
            floor_position=float(raw_note.get("floorPosition", note_h)),
            note_h=note_h,
        )
        hit.world_x, hit.world_y = self.note_pos(hit, t)
        hit.px, hit.py = self.to_px(hit.world_x, hit.world_y)
        return hit

    def notes(self) -> List[NoteHit]:
        """展开全部音符为击打点列表，按时刻升序。"""
        out: List[NoteHit] = []

        for line in self.lines:
            for bucket, above in (("notesAbove", True), ("notesBelow", False)):
                for raw_note in line.raw.get(bucket, []):
                    note = dict(raw_note)
                    note["_above"] = above
                    out.append(self._hit_point(line, note))

        out.sort(key=lambda n: n.t_sec)
        self._mark_multiple(out)

        # 记录重算高度与官谱 floorPosition 字段的分歧（不打断解析，只暴露）
        self.note_h_mismatch: List[Tuple[int, float, float, float]] = [
            (n.line, n.t_sec, n.note_h, n.floor_position)
            for n in out
            if abs(n.note_h - n.floor_position) > FLOOR_POSITION_TOL
        ]
        return out

    @staticmethod
    def _mark_multiple(hits: List[NoteHit]) -> None:
        """标记「同一时刻需要多指同时击打」的音符。

        简化实现：按时刻精确相等分组（官方谱时刻由同一公式生成，
        直接比较浮点相等即可），组内多于一个音符则全部标记。
        Phira 的 `process_lines` 跨判定线做同样的事。
        """
        if not hits:
            return

        start = 0
        for i in range(1, len(hits) + 1):
            if i < len(hits) and hits[i].t_sec == hits[start].t_sec:
                continue
            if i - start > 1:
                for j in range(start, i):
                    hits[j].multiple = True
            start = i
