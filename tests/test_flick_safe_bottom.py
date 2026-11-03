#!/usr/bin/env python
"""谱面层「Flick 底部安全上沿」测试（plan `2026-10-22-safe-bottom` §四 第一层）。

## 为什么需要这一层

真机屏高 720，画布高 1080 ⇒ 画布 `y = 1080` 对应真机 `y = 720`，而 `Rect` 是半开区间
`[0,720)` ⇒ **画布底边本身就在屏外**。`fit_flick_sweep` 按 `h = 1080` 推中心，就会把扫动
末端正好压在 1080 上。

更糟：再往里 80px（真机 `640~719`）是 `mandatorySystemGestures`
（`dumpsys window` 2026-10-22 21:2x 现读 `frame=[0,640][1610,720]`，是 **mandatory**，
游戏不能用 `setSystemGestureExclusionRects` 排除）⇒ 安全上沿是 **639**。

## 判据为什么盯「跨度」而不是「不出界」

Flick 靠「判定区内快速位移」识别。逐点夹取能做到「完全不出界」，代价是把越界的几毫秒
压成同一条水平线 ⇒ 位移归零 ⇒ **必漏**（教训 15：判据要盯功能赖以成立的那个量）。
所以本文件的每条断言都同时检查**跨度**。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pgauto import schedule as sch  # noqa: E402

CANVAS_W, CANVAS_H = sch.CANVAS          # 1920 x 1080
#: 真机 1610x720 的「手势区外上沿」在画布上的高度：639/720 × 1080 = 958.5
SAFE_RATIO = 639.0 / 720.0
SAFE_CANVAS_H = CANVAS_H * SAFE_RATIO

#: 扫动半幅（画布 px）。`FLICK_RADIUS_RATIO = 60/1280` ⇒ 1920 宽画布下 = 90。
FLICK_R = sch.FLICK_RADIUS_RATIO * CANVAS_W


def mk_flick(py: float = 1080.0, px: float = 960.0, t: float = 1000.0,
             angle: float = 0.0, index: int = 0) -> sch.Note:
    return sch.Note(index=index, type=sch.NOTE_FLICK, t_ms=t, px=px, py=py,
                    hold_ms=0.0, angle_deg=angle, line=-1, position_x=0.0)


def sweep_ys(sched) -> list:
    """取该 Flick 的**扫动**段 y 列表（MOVE）。

    `DOWN` 也要收进来 —— 它虽然由注入层负责收进屏内，但它是同一段连续位移的起点，
    漏掉它就会把「跨度」算小。这里返回全部事件的 y，由调用方按需筛。
    """
    return [e.y for e in sched.events if e.action == "MOVE"]


# ---------------------------------------------------------------- 纯函数

def test_radius_is_never_shrunk_only_center_is_pushed():
    """★ 核心契约：上沿变紧时**推更多中心**，**不缩半径**。

    缩半径会把「部分越界但有完整位移」变成「不出界但零位移」⇒ Flick 必漏。
    """
    px, py, nx, ny = 960.0, 1080.0, 0.0, 1.0
    cx0, cy0, r0 = sch.fit_flick_sweep(px, py, nx, ny, FLICK_R, CANVAS_W, CANVAS_H)
    assert (cx0, cy0, r0) == (pytest.approx(px), pytest.approx(CANVAS_H - FLICK_R),
                              pytest.approx(FLICK_R))

    cx1, cy1, r1 = sch.fit_flick_sweep(px, py, nx, ny, FLICK_R, CANVAS_W, SAFE_CANVAS_H)
    assert r1 == pytest.approx(FLICK_R), "半径必须保持满幅"
    assert cy1 < cy0, "上沿变紧 ⇒ 中心必须更高"
    # 扫动跨度 = 2R，两种边界下都成立 ⇒ 位移不受影响
    assert (cy0 + r0) - (cy0 - r0) == pytest.approx(2 * FLICK_R)
    assert (cy1 + r1) - (cy1 - r1) == pytest.approx(2 * FLICK_R)
    # 上端正好压在各自的上沿
    assert cy0 + r0 == pytest.approx(CANVAS_H)
    assert cy1 + r1 == pytest.approx(SAFE_CANVAS_H)


def test_safe_top_lands_on_639_device_px():
    """958.5 画布 × 720/1080 = 639 真机 px —— 与 `inject.SAFE_BOTTOM_Y` 同一个数。"""
    assert SAFE_CANVAS_H * 720.0 / 1080.0 == pytest.approx(639.0)


def test_float_residue_in_the_normal_must_not_blow_up_the_shift():
    """★ 回归（2026-10-22 实测爆炸过一次）：法向里的浮点残差必须被清掉。

    `tangent_dir(90°)` 返回 `(6.12e-17, -1.0)` —— 那个 6e-17 不是真实几何分量，
    但 `room()` 见到 `uy != 0` 就除以它，得到 **-6.8e17**；于是 `shift` 也是 6.8e17，
    扫动坐标变成 `x = -3.39e17`。

    旧版 `h = 1080` 时「余量都够 ⇒ 提前返回」把它掩盖了；把底部上沿改成 958.5 之后
    `room_neg` 变负、进入推中心分支 ⇒ 立刻暴露。所以这条断言必须留。
    """
    assert abs(sch.tangent_dir(90.0)[0]) < 1e-15      # 确实是浮点残差，不是 0

    cx, cy, r = sch.fit_flick_sweep(960.0, 1000.0, 1.0, 6.12e-17,
                                    FLICK_R, CANVAS_W, SAFE_CANVAS_H)
    assert abs(cx) < 1e4 and abs(cy) < 1e4, (cx, cy)
    assert r == pytest.approx(FLICK_R)
    # 法向水平 ⇒ 底部上沿与该 Flick 无关，中心不该被推
    assert (cx, cy) == (pytest.approx(960.0), pytest.approx(1000.0))


def test_absurd_shift_raises_instead_of_emitting_garbage_coordinates():
    """兜底护栏：推移量超过画布对角线 ⇒ **抛**，不许静默产出天文坐标。

    静默产出的后果是真机把指针甩到几百个屏宽之外，而症状要到打歌时才看得见。
    """
    # ① 画布尺寸非法（`h` 荒谬）：必须先被尺寸校验拦住，不能拿它去算余量
    with pytest.raises(ValueError, match="画布尺寸非法"):
        sch.fit_flick_sweep(960.0, 1000.0, 0.0, 1.0, FLICK_R, CANVAS_W, -1e18)
    # ② 尺寸合法但内部算出荒谬 shift：由推移量护栏兜住
    with pytest.raises(ValueError, match="荒谬的推移量"):
        sch.fit_flick_sweep(1e-3, 1000.0, 0.0, 1.0, FLICK_R, 2.0, 1.0)
    # 反 vacuous：正常参数不该抛
    assert sch.fit_flick_sweep(960.0, 1000.0, 0.0, 1.0, FLICK_R,
                               CANVAS_W, SAFE_CANVAS_H)[2] == pytest.approx(FLICK_R)


def test_vertical_judge_line_still_inside_tangent_half_width():
    """★★ 方案成立的几何前提：竖直判定线的切向容差还剩多少。

    判定区 = 沿判定线法向无限长、切向半宽 `TANGENT_THRESHOLD_RATIO × 画布宽`
    = 150 画布 px。竖直判定线（`angle=90`）时切向就是竖直方向，于是「手指在 958.5、
    判定点在 1080」会产生 **121.5 画布 px** 的切向偏移 —— 仍在区内，余量 28.5。

    ⛔ 这条最坏情况才是最紧的约束。若哪天把安全上沿再压低（ratio 更小），这里会先红。
    """
    half = sch.TANGENT_THRESHOLD_RATIO * CANVAS_W
    assert half == pytest.approx(150.0)          # 100/1280 × 1920

    finger_y = SAFE_CANVAS_H                     # 958.5
    judge_y = float(CANVAS_H)                    # 1080
    off = sch.tangent_offset((960.0, finger_y), (960.0, judge_y), 90.0)
    assert off == pytest.approx(121.5, abs=0.01)
    assert off < half, "超出切向半宽就无法判定"
    assert half - off == pytest.approx(28.5, abs=0.01)

    # 反 vacuous：水平判定线（angle=0）时竖直偏移**完全不参与**判定
    assert sch.tangent_offset((960.0, finger_y), (960.0, judge_y), 0.0) == pytest.approx(0.0)


# ---------------------------------------------------------------- 端到端

def test_build_keeps_sweep_span_while_clearing_the_bottom():
    """★ 端到端：开安全上沿后，扫动段不越界，且**跨度逐点不变**。"""
    notes = [mk_flick(py=1080.0)]
    old = sch.build(notes, flick_bottom_safe_ratio=1.0)
    new = sch.build(notes, flick_bottom_safe_ratio=SAFE_RATIO)

    ys_old, ys_new = sweep_ys(old), sweep_ys(new)
    assert max(ys_old) == pytest.approx(1080.0, abs=0.5)          # 旧：压在屏外
    assert max(ys_new) == pytest.approx(SAFE_CANVAS_H, abs=0.5)   # 新：压在 958.5
    assert max(ys_new) <= SAFE_CANVAS_H + 0.5

    span_old = max(ys_old) - min(ys_old)
    span_new = max(ys_new) - min(ys_new)
    assert span_old == pytest.approx(span_new, abs=0.5), "★ 跨度必须保持"
    assert span_new > 0.8 * 2 * FLICK_R, "★ 位移不能缩水（>80% 满幅）"


def test_down_still_lands_on_the_note_body():
    """`DOWN` 仍打在本体（画布 1080 = 真机 720，屏外）—— 这是**有意**的。

    这一层只管扫动；本体那个按点由注入层 `SAFE_BOTTOM_Y` 收进 639。
    两层分工写清楚，免得有人以为改了这一层就万事大吉。
    """
    notes = [mk_flick(py=1080.0)]
    new = sch.build(notes, flick_bottom_safe_ratio=SAFE_RATIO)
    downs = [(e.action, e.y) for e in new.events
             if e.action in ("DOWN", "POINTER_DOWN")]
    assert downs == [("DOWN", pytest.approx(1080.0, abs=0.5))]


def test_default_is_the_production_value_and_ratio_one_still_reproduces_the_old_behaviour():
    """★ 两条契约一起钉住：

    ① **不传参数 = 生产值**（`FLICK_BOTTOM_SAFE_RATIO = 639/720`），不是旧行为。
       2026-10-22 21:5x 这一版之前默认值是 `1.0`，而注释写着「生产值 0.8875」
       —— 谁照文档以为已修、跑 `play.py` 走默认路径，就**一点效果都没有**。
       变异：把常量改回 `1.0`，本用例第一条断言先红。
    ② 显式传 `1.0` 仍能**逐点**复现旧产物（`tmp/score_diff.py` 的三路对照靠它）。
       判据用「同参数确定性 + 与生产值必须不同 + 旧签名（扫动压到 1080）」。
    """
    notes = [mk_flick(py=1080.0, t=1000.0), mk_flick(py=540.0, px=400.0, t=2000.0, index=1)]
    default = sch.build(notes)
    explicit_safe = sch.build(notes, flick_bottom_safe_ratio=sch.FLICK_BOTTOM_SAFE_RATIO)
    sig = lambda s: [(e.t_ms, e.action, e.pointer_id, e.x, e.y) for e in s.events]

    # ① 默认值就是生产值
    assert sch.FLICK_BOTTOM_SAFE_RATIO == pytest.approx(639.0 / 720.0)
    assert sig(default) == sig(explicit_safe)
    # ② 显式 1.0 = 旧行为：自洽（两次跑一样）且与生产值**确实不同**
    old_a = sch.build(notes, flick_bottom_safe_ratio=1.0)
    old_b = sch.build(notes, flick_bottom_safe_ratio=1.0)
    assert sig(old_a) == sig(old_b), "同参数必须确定性"
    assert sig(old_a) != sig(default), "★ ratio=1.0 与生产值必须不同（否则 A/B 是空转）"
    # 旧签名：贴底那根 Flick 的扫动末端压在画布底边 1080（= 真机屏外 720）
    assert max(sweep_ys(old_a)) == pytest.approx(1080.0, abs=0.5)
    assert max(sweep_ys(default)) == pytest.approx(SAFE_CANVAS_H, abs=0.5)


def test_safe_ratio_does_not_touch_in_screen_flicks():
    """屏内 Flick（中心 540）两种口径下必须完全一致 —— 改动只该影响贴底的。"""
    notes = [mk_flick(py=540.0)]
    old = sch.build(notes, flick_bottom_safe_ratio=1.0)
    new = sch.build(notes, flick_bottom_safe_ratio=SAFE_RATIO)
    assert [(e.t_ms, e.action, e.x, e.y) for e in old.events] == \
           [(e.t_ms, e.action, e.x, e.y) for e in new.events]


def test_mutation_ratio_one_must_leave_the_sweep_out_of_bounds():
    """变异：把 ratio 设回 1.0，扫动**必须**重新越界（否则上面的判据是空转）。"""
    notes = [mk_flick(py=1080.0)]
    for ratio, should_exceed in ((1.0, True), (SAFE_RATIO, False)):
        s = sch.build(notes, flick_bottom_safe_ratio=ratio)
        over = max(sweep_ys(s)) > SAFE_CANVAS_H + 0.5
        assert over is should_exceed, (ratio, max(sweep_ys(s)))


def test_horizontal_judge_line_flick_is_unaffected_by_the_bottom_inset():
    """水平判定线（angle=0）的 Flick 法向是竖直，仍受上沿约束；而**竖直**判定线
    （angle=90）的法向是水平 ⇒ 底部上沿与它无关，产物必须逐点不变。"""
    notes = [mk_flick(py=1000.0, px=960.0, angle=90.0)]
    old = sch.build(notes, flick_bottom_safe_ratio=1.0)
    new = sch.build(notes, flick_bottom_safe_ratio=SAFE_RATIO)
    # 法向水平 ⇒ 底部余量根本不参与，中心不该被推
    assert [(e.t_ms, e.action, e.x, e.y) for e in old.events] == \
           [(e.t_ms, e.action, e.x, e.y) for e in new.events]
