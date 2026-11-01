"""关键帧动画。

语义对齐 TeamFlos/phira 的 `prpr::core::anim::Anim<T>`：

- 关键帧按时间非降序排列；
- 定位用「最后一个 time <= t 的帧」；
- 取值 = 相邻两帧之间的线性插值（Phira 侧 tween id = 2，即 linear）；
- 定位到末帧时直接取末值。

与 Phira 的两处**有意偏离**（均为健壮性考虑，不影响官方谱数据）：

1. 空关键帧列表返回常量 0 的动画，而 Phira 会在 `Anim::new` 的
   `assert!(!keyframes.is_empty())` 处 panic。官方谱四类事件非空，
   但第三方转换谱可能缺失，故选择容错。
2. `t` 早于首帧时返回首帧值（不外推），Phira 会按 `(t-t0)/(t1-t0)`
   外推。官方谱事件首帧已被 `.max(0.)` 夹到 0，音符时间恒 >= 0，
   该分支实际不会触发。
"""

from __future__ import annotations

from bisect import bisect_right
from typing import List, Sequence, Tuple

Keyframes = Sequence[Tuple[float, float]]


class AnimFloat:
    """单通道关键帧动画。"""

    __slots__ = ("_times", "_values")

    def __init__(self, keyframes: Keyframes):
        if not keyframes:
            self._times: List[float] = [0.0]
            self._values: List[float] = [0.0]
            return

        # Python 的 sorted 是稳定排序：同一时刻的多帧保持原有先后，
        # 从而与 Phira「游标停在最后一个 time <= t 的帧」的取法一致。
        ordered = sorted(keyframes, key=lambda kf: kf[0])
        self._times = [float(t) for t, _ in ordered]
        self._values = [float(v) for _, v in ordered]

    def __len__(self) -> int:
        return len(self._times)

    def at(self, t: float) -> float:
        """取 t 时刻的值。"""
        i = bisect_right(self._times, t) - 1

        if i < 0:
            return self._values[0]
        if i >= len(self._times) - 1:
            return self._values[-1]

        t0, t1 = self._times[i], self._times[i + 1]
        v0, v1 = self._values[i], self._values[i + 1]
        if t1 == t0:
            return v1

        return v0 + (v1 - v0) * (t - t0) / (t1 - t0)


class AnimVec:
    """双通道关键帧动画（判定线的 x / y）。"""

    __slots__ = ("x", "y")

    def __init__(self, x: AnimFloat, y: AnimFloat):
        self.x = x
        self.y = y

    def at(self, t: float) -> Tuple[float, float]:
        return self.x.at(t), self.y.at(t)
