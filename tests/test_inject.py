"""`tools/inject.py` 的离线测试 —— 不连手机、不碰设备。

覆盖三件事，每件都对着 scrcpy v5.0.1 的源码条款：

    [1] 32 字节 INJECT_TOUCH_EVENT 的**逐字节**布局（用硬编码的期望字节串比对，
        而不是拿 struct 再解一遍 —— 否则改错格式串两边一起错）
    [2] touches.json 的竖屏坐标 → 真机横屏坐标的映射（关键点是手算值）
    [3] 绝对时基 Clock / wait_until 的语义
"""

from __future__ import annotations

import json
import socket as _socket
import struct
import time

import pytest

import inject


# ---------------------------------------------------------------- [1] 编码


def test_touch_message_is_32_bytes():
    msg = inject.build_touch(inject.ACTION_DOWN, 0, 0, 0, 1, 1, 0)
    assert len(msg) == 32
    assert struct.calcsize(inject.TOUCH_FMT) == 32


def test_touch_message_byte_layout():
    """一条已知消息必须**逐字节**等于手写的期望串。

    字段表见 tools/inject.py 模块 docstring（抄自 v5.0.1 的
    ControlMessageReader.parseInjectTouchEvent 与 control_msg.c）。
    """
    msg = inject.build_touch(
        action=inject.ACTION_DOWN,      # 0
        pointer_id=0,
        x=1568,
        y=720,
        screen_w=3136,
        screen_h=1440,
        pressure=inject.PRESSURE_MAX,   # 0xFFFF
    )
    want = (
        "02"                  # type = 2
        "00"                  # action = DOWN
        "0000000000000000"    # pointer_id
        "00000620"            # x = 1568
        "000002D0"            # y = 720
        "0C40"                # screen_width = 3136
        "05A0"                # screen_height = 1440
        "FFFF"                # pressure = 1.0（u16 定点）
        "00000000"            # action_button
        "00000000"            # buttons
    )
    assert msg.hex().upper() == want


def test_touch_message_handles_negative_and_out_of_screen_values():
    """屏幕外的点也要能编码（server 直通坐标，Android 自己会丢）。"""
    msg = inject.build_touch(inject.ACTION_MOVE, -2, -162, 1702, 3136, 1440, 0xFFFF)
    _, _, pid, x, y, *_ = struct.unpack(inject.TOUCH_FMT, msg)
    assert pid == -2
    assert x == -162
    assert y == 1702


def test_pointer_id_is_big_endian_64bit():
    msg = inject.build_touch(inject.ACTION_DOWN, 10, 0, 0, 1, 1, 0)
    assert msg[2:10] == (10).to_bytes(8, "big")


def test_action_name_mapping_collapses_pointer_variants():
    """POINTER_DOWN / POINTER_UP 是语义标注，发出去必须压回裸 DOWN / UP。

    scrcpy server 会按当前指针数自己补 ACTION_POINTER_* 前缀
    （Controller.injectTouch → PointersState），客户端补了反而错。
    """
    assert inject.ACTION_NAMES["DOWN"] == 0
    assert inject.ACTION_NAMES["POINTER_DOWN"] == 0
    assert inject.ACTION_NAMES["MOVE"] == 2
    assert inject.ACTION_NAMES["UP"] == 1
    assert inject.ACTION_NAMES["POINTER_UP"] == 1


@pytest.mark.parametrize("name,action", [("DOWN", 0), ("POINTER_DOWN", 0),
                                         ("MOVE", 2), ("UP", 1), ("POINTER_UP", 1)])
def test_encode_from_event_picks_pressure_by_action(name, action):
    ev = {"action": name, "pointer_id": 3, "x": 540.0, "y": 1170.0}
    msg = inject.encode_from_event(ev, 3136, 1440, lambda x, y: (int(x), int(y)))
    got_action = msg[1]
    pressure = int.from_bytes(msg[22:24], "big")
    assert got_action == action
    # Controller 对非鼠标 source 会强制 buttons=0；压力只有 UP 才是 0
    assert pressure == (0 if action == inject.ACTION_UP else 0xFFFF)


def test_encode_from_event_rejects_unknown_action():
    with pytest.raises(ValueError):
        inject.encode_from_event(
            {"action": "CLICK", "pointer_id": 0, "x": 0.0, "y": 0.0}, 1, 1, lambda x, y: (0, 0)
        )


# ---------------------------------------------------------------- [2] 坐标映射

CANVAS = (1920.0, 1080.0)
SRC = (1080.0, 2340.0)      # touches.json 里的竖屏
DST = (3136.0, 1440.0)      # 真机横屏


@pytest.mark.parametrize("canvas_pt,want", [
    ((960.0, 540.0), (1568.0, 720.0)),
    ((0.0, 0.0), (0.0, -162.0)),
    ((1920.0, 0.0), (3136.0, -162.0)),
    ((0.0, 1080.0), (0.0, 1602.0)),
    ((1920.0, 1080.0), (3136.0, 1602.0)),
])
def test_fit_width_matches_hand_computed_values(canvas_pt, want):
    """按宽度适配：k = 3136/1920 = 1.63333，y_off = (1440 - 1764)/2 = -162。"""
    sm = inject.ScreenMap(CANVAS, SRC, DST, fit="width")
    got = sm.from_canvas(*canvas_pt)
    assert got == pytest.approx(want, abs=0.01)


@pytest.mark.parametrize("canvas_pt,want", [
    ((960.0, 540.0), (1568.0, 720.0)),
    ((0.0, 0.0), (288.0, 0.0)),
    ((1920.0, 0.0), (2848.0, 0.0)),
    ((0.0, 1080.0), (288.0, 1440.0)),
    ((1920.0, 1080.0), (2848.0, 1440.0)),
])
def test_fit_height_matches_hand_computed_values(canvas_pt, want):
    """按高度适配：k = 1440/1080 = 1.33333，x_off = (3136 - 2560)/2 = 288。"""
    sm = inject.ScreenMap(CANVAS, SRC, DST, fit="height")
    got = sm.from_canvas(*canvas_pt)
    assert got == pytest.approx(want, abs=0.01)


def test_default_fit_is_height():
    """★ 默认值钉死为 height（2026-10-09 真机实测结论）。

    这条是**回归护栏**：默认值一旦被改回 width，正式打歌时整条判定区会按
    1.6333 倍错位（上下各裁 162px），而离线测试可能全绿、看不出来。
    """
    assert inject.ScreenMap(CANVAS, SRC, DST).fit == "height"


def test_default_fit_behaves_exactly_like_explicit_height():
    """不传 fit 构造时，行为必须与显式 fit="height" 逐点一致。"""
    default = inject.ScreenMap(CANVAS, SRC, DST)
    height = inject.ScreenMap(CANVAS, SRC, DST, fit="height")
    for pt in ((0.0, 0.0), (960.0, 540.0), (1920.0, 1080.0)):
        assert default.from_canvas(*pt) == pytest.approx(height.from_canvas(*pt), abs=1e-9)


@pytest.mark.parametrize("argv", [
    ["play"],
    ["tap", "--x", "0", "--y", "0"],
])
def test_cli_fit_defaults_to_height(argv):
    """命令行的 `--fit` 默认值也必须是 height，否则 play/tap 会静默用错映射。"""
    assert inject.build_parser().parse_args(argv).fit == "height"


def test_cli_fit_can_still_be_overridden():
    """width / stretch 仍可显式指定，用于复现历史判断与做对照实验。"""
    assert inject.build_parser().parse_args(["play", "--fit", "width"]).fit == "width"


# ---------------------------------------------------- [4] 横屏守卫（play）

_TOUCHES_DOC = {
    "canvas": {"width": 1920, "height": 1080},
    "screen": {"width": 1080, "height": 2340},
    "events": [
        {"action": "DOWN", "pointer_id": 0, "x": 100.0, "y": 200.0, "t_ms": 0.0},
    ],
}


def _fake_adb(monkeypatch, size):
    """把 cmd_play 用的 Adb 换成一个只会报尺寸的替身。"""
    fake = inject.Adb(inject.DEFAULT_ADB)
    monkeypatch.setattr(fake, "resolve_serial", lambda: "FAKE0000")
    monkeypatch.setattr(fake, "screen_size", lambda: size)
    monkeypatch.setattr(inject, "Adb", lambda *a, **k: fake)
    return fake


def _write_touches(tmp_path):
    p = tmp_path / "t.json"
    p.write_text(json.dumps(_TOUCHES_DOC), encoding="utf-8")
    return p


def test_play_refuses_portrait_screen(monkeypatch, tmp_path):
    """★ 竖屏时必须硬拦。

    `screen_size()` 读的是**当前方向**尺寸。游戏不在前台时设备是竖屏
    1440x3136，坐标会整体按竖屏映射 → 全错，而这种错**静默发生**：
    打完整首才发现全 Miss，还往账号里留了一条成绩。
    """
    _fake_adb(monkeypatch, (1440, 3136, 0))
    args = inject.build_parser().parse_args(
        ["play", "--touches", str(_write_touches(tmp_path))]
    )
    with pytest.raises(SystemExit, match="竖屏"):
        inject.cmd_play(args)


def test_play_accepts_landscape_screen(monkeypatch, tmp_path):
    """横屏（游戏在前台）时守卫不误伤，能正常走到 dry-run。"""
    _fake_adb(monkeypatch, (3136, 1440, 0))
    args = inject.build_parser().parse_args(
        ["play", "--touches", str(_write_touches(tmp_path)), "--dry-run"]
    )
    assert inject.cmd_play(args) == 0


def test_two_fits_agree_only_at_canvas_center():
    """两种适配只在画布中心重合 —— 中心永远是安全的注入点。"""
    w = inject.ScreenMap(CANVAS, SRC, DST, fit="width")
    h = inject.ScreenMap(CANVAS, SRC, DST, fit="height")
    assert w.from_canvas(960.0, 540.0) == pytest.approx(h.from_canvas(960.0, 540.0))


def test_screen_center_round_trips_to_canvas_center():
    """交付物的屏幕中点（竖屏 1080x2340 的 (540,1170)）必须反解回画布中心。"""
    sm = inject.ScreenMap(CANVAS, SRC, DST, fit="width")
    assert sm.to_canvas(540.0, 1170.0) == pytest.approx((960.0, 540.0), abs=0.01)


def test_end_to_end_vertical_screen_center_lands_on_device_center():
    sm = inject.ScreenMap(CANVAS, SRC, DST, fit="width")
    assert sm.point(540.0, 1170.0) == (1568, 720)


def test_vertical_screen_edges_map_inside_canvas():
    """竖屏（2340 高）的上下边缘对应的正是画布上下边缘。"""
    sm = inject.ScreenMap(CANVAS, SRC, DST, fit="width")
    assert sm.to_canvas(0.0, 866.25) == pytest.approx((0.0, 0.0), abs=0.01)
    assert sm.to_canvas(0.0, 1473.75) == pytest.approx((0.0, 1080.0), abs=0.01)


def test_describe_reports_the_crop_for_width_fit():
    """按宽度适配必然上下裁 —— 这句描述要写在报告里，别让它悄悄过去。"""
    text = inject.ScreenMap(CANVAS, SRC, DST, fit="width").describe()
    assert "上下各裁 162px" in text


def test_stretch_is_not_equal_to_width_in_y():
    sm = inject.ScreenMap(CANVAS, SRC, DST, fit="stretch")
    # 非等比：y 单独按 1440/1080 缩放
    assert sm.from_canvas(0.0, 1080.0) == pytest.approx((0.0, 1440.0), abs=0.01)


# ---------------------------------------------------------------- [2b] 方向性夹取
#
# 2026-10-22 一天两改，两次都有真机证据（FACTS §125 / §126）：
#   上午：`point()` 从「原样直通」改成「四边全夹」，起因是画布底边 y=1080 → 真机
#         y=720 = 屏高，是个不存在的像素（Rect 半开），24 条 DOWN 打在这，Miss=41。
#   晚上：**底部改回不夹**。同一首、同一份谱、只切「夹不夹」的对照实验：
#         不夹 0 个底部上滑手势，夹取 7 个 ⇒ Honor GestureNavBottom 抢流，
#         `input channel stole pointer stream` ⇒ 游戏被踢到桌面。
#         代价：贴底音符继续 Miss（已知缺陷，不是修好）。
#   顶部**必须夹**：qzw_ez 有一条 ptr=1 的轨迹 MOVE 到 y=-22 后在屏外 POINTER_UP，
#         屏外 UP 会被系统丢弃 ⇒ 指针永远抬不起。

DEV = (1610.0, 720.0)       # ALT-AN00 横屏（MEMORY §3）
# 交付物竖屏 1080x2340 的底边中点 = 画布 (960, 1080)
BOTTOM_CENTER = (540.0, 1473.75)
# 画布 y=-117.8（画面上方屏外）⇒ 真机 y=-79（被夹到 0）
TOP_OUT = (540.0, 800.0)


def test_top_overshoot_is_clamped_so_the_pointer_can_be_released():
    """★ 顶部**必须夹到 0**：屏外 `POINTER_UP` 会被系统丢弃 ⇒ 指针永远抬不起。

    真机证据（2026-10-22，FACTS §125）：`qzw_ez` 有一条 ptr=1 的轨迹 MOVE 到
    `y=-22` 之后在屏外发 `POINTER_UP`，那一根手指就卡死了；后续所有落在它身上的
    MOVE 都跟着偏。

    夹取后 `ny` 为**负**（−79），与底部夹取的正值可区分 —— 这个符号是排查用的抓手，
    别改成 `abs()`。
    """
    sm = inject.ScreenMap(CANVAS, SRC, DEV, fit="height")
    assert sm.point(*TOP_OUT) == (805, 0)
    # 反 vacuous：裸映射确实是负的 ⇒ 上面那条真的说明「夹了」
    assert sm.unclamped_point(*TOP_OUT) == (805, -79)
    # 顶部夹取要留痕，且符号为负（底部屏外为正 +81）
    assert sm.point_ex(*TOP_OUT) == (805, 0, 0, -79)


def test_point_leaves_every_in_screen_point_untouched():
    """★ 手势区内 / 屏内最后一行的点**分毫不动** —— 这是「别无差别上移」的底线。

    `640~719` 落在 `mandatorySystemGestures [640,720)` 里，看起来「危险」，但
    `home_gesture_scan --gest 640` 全库扫出 **0 段**「手势区内按下 + 竖直上滑」⇒
    它们本来就能正常工作。把它们统一挪到 639 只会白白吃掉切向容差
    （判定区切向半宽 100 真机 px，挪 81px 只剩 19px）。

    ⛔ 2026-10-22 21:0x 的第四版就犯过这个方向的反错：把 `640~719` 也夹了，
    结果那 29,406 条事件的落点全变、Early/Late 抬高。这里先红。
    """
    sm = inject.ScreenMap(CANVAS, SRC, DEV, fit="height")
    # (交付物 y, 期望真机 y)：k_src=0.5625 / y_off=866.25 / k_dst=2/3，手算值
    for dev_y in (0, 360, 640, 719):
        y = 866.25 + dev_y * 0.5625 / (2.0 / 3.0)       # 反解成交付物竖屏 y
        px, py = sm.point(540.0, y)
        assert (px, py) == (805, dev_y), (dev_y, y, px, py)
        # 「不动」的严格口径：与完全不做夹取的映射逐项相等
        assert sm.unclamped_point(540.0, y) == (px, py)
        assert sm.point_ex(540.0, y)[2:] == (0, 0)
    # 反 vacuous：同一函数在屏外确实会改值 ⇒ 上面四条不是「永远相等」
    out = 866.25 + 720 * 0.5625 / (2.0 / 3.0)           # 画布底边 ⇒ 真机 720
    assert sm.point(540.0, out) == (805, 639) != sm.unclamped_point(540.0, out)


def test_point_clamps_horizontal_overflow_too():
    """左右越界夹进 `[0, w-1]`，且**没越界的点不许被夹**（防过度夹取）。

    左右黑边 165 是 `fit=height` 的产物：画布 x∈[0,1920] → 真机 x∈[165,1445]。
    所以正常谱面根本到不了 0 / 1609，只有 Flick 的扫动端点和 `clamp_into_screen`
    的残余才会越界。
    """
    sm = inject.ScreenMap(CANVAS, SRC, DEV, fit="height")
    # 左越界：交付物 x=-200 ⇒ 画布 x≈-355.6 ⇒ 真机 x=-72
    assert sm.point(-200.0, 1170.0) == (0, 360)
    assert sm.unclamped_point(-200.0, 1170.0) == (-72, 360)
    assert sm.point_ex(-200.0, 1170.0) == (0, 360, -72, 0)
    # 右越界：交付物 x=1300 ⇒ 画布 x≈2311 ⇒ 真机 x=1706（屏宽 1610）
    assert sm.point(1300.0, 1170.0) == (1609, 360)
    assert sm.unclamped_point(1300.0, 1170.0) == (1706, 360)
    assert sm.point_ex(1300.0, 1170.0) == (1609, 360, 97, 0)
    # 两边边界内侧必须原样：交付物 x=0/1080 ⇒ 真机 165/1445
    assert sm.point(0.0, 1170.0) == (165, 360)
    assert sm.point(1080.0, 1170.0) == (1445, 360)
    # 反 vacuous：交付物 x=1200（画布 2133）虽已超出交付域但真机仍在屏内 ⇒ 不许夹
    assert sm.point(1200.0, 1170.0) == (1587, 360)
    assert sm.point_ex(1200.0, 1170.0)[2:] == (0, 0)


def test_tap_point_from_canvas_does_not_go_through_the_portrait_domain():
    """★ `tap --from-canvas` 必须走 `point_canvas()`，不许走 `point()`。

    两个函数的入参是**不同的坐标域**：`point()` 期望交付物竖屏 1080×2340，
    `point_canvas()` 期望画布 1920×1080。2026-10-22 审查发现原实现走的是
    `point()` —— 传画布中心 `(960, 540)` 会得到 `(1303, 0)`（真实 y 是 -387，
    被顶部夹成 0），正确值 `(805, 360)`，偏了 498px 且整根手指落在屏幕顶上。

    这条用例要能**区分两种实现**，所以刻意断言错误路径的值不相等。
    """
    sm = inject.ScreenMap(CANVAS, SRC, DEV, fit="height")
    assert inject.tap_point(sm, 960.0, 540.0, from_canvas=True) == (805, 360)
    # 错误实现（走 point）会得到 x=1303、y=-387（夹取后 0）⇒ 两条路径确实可分辨。
    # ⚠ 只断言 x 与「不等于正确值」，**不**断言被夹后的 y=0 —— 否则这条用例会跟
    #   顶部夹取的实现对赌，把变异测试的信号弄浑（实测：MUT=top 时它假红）。
    wrong = sm.point(960.0, 540.0)
    assert wrong[0] == 1303 and wrong != (805, 360)
    assert sm.unclamped_point(960.0, 540.0) == (1303, -387)
    # from_canvas=False 时是真机像素直通，不经过任何映射
    assert inject.tap_point(sm, 805.0, 360.0, from_canvas=False) == (805, 360)
    assert inject.tap_point(sm, 800.6, 359.4, from_canvas=False) == (801, 359)


def test_bottom_overshoot_is_clamped_to_the_safe_edge_not_the_last_row():
    """★ 事故回归：底部屏外**收到 639**，不是最后一行 719。

    719 落在 `mandatorySystemGestures [640,720)` 里，2026-10-22 19:5x 就是这么把游戏
    踢回桌面的（FACTS §126）。639 在手势区之外，而竖直偏移又不参与判定（判定区沿
    法向无限长）⇒ 既能救回贴底音符，又不会跨过系统的判据阈值。

    断言要能抓到三种改坏方式，否则等于没测：
      ① 底部改回不夹（`CLAMP_BOTTOM_EDGE=False`）⇒ 720
      ② 夹到屏内最后一行（`SAFE_BOTTOM_Y = h-1`）⇒ 719（护栏会先抛，见下一条用例）
      ③ 夹进手势区任意位置（如 660）⇒ 同上
    """
    sm = inject.ScreenMap(CANVAS, SRC, DEV, fit="height")
    assert sm.point(*BOTTOM_CENTER) == (805, 639)
    # 反 vacuous：裸映射仍是 720 ⇒ 上面那条真的说明「夹了」，不是碰巧相等
    assert sm.unclamped_point(*BOTTOM_CENTER) == (805, 720)
    assert inject.CLAMP_BOTTOM_EDGE is True
    # 上沿必须严格小于 mandatory 手势区上沿
    assert 0 < inject.SAFE_BOTTOM_Y < inject.GEST_BOTTOM_Y == 640


def test_gesture_zone_pixels_are_left_untouched():
    """★ 守门断言：`640~719`（mandatory 手势区）**原样保留**。

    全库 29,406 条事件落在这段里（占 8.7%），而 `home_gesture_scan --gest 640` 全库
    扫出 **0 段**「手势区按下 + 竖直上滑」⇒ 它们本来就能正常工作。无差别上移只会白白
    吃掉切向容差（判定区切向半宽 150 画布 px = 100 真机 px，挪 81px 只剩 19px）。

    ⛔ 哪天有人把 `_clamp` 改成「凡 `>= GEST_BOTTOM_Y` 就收到 SAFE_BOTTOM_Y」，这里先红。
    """
    sm = inject.ScreenMap(CANVAS, SRC, DEV, fit="height")
    # 画布 y → 真机 y = canvas_y * 720/1080
    for canvas_y, want in ((958.5, 639),      # 恰好在安全上沿
                           (960.0, 640),      # 手势区第一行
                           (990.0, 660),      # 典型「贴底判定线」的推中心结果
                           (1078.0, 719)):    # 屏内最后一行
        px, py, nx, ny = sm._clamp(*sm.from_canvas(960.0, canvas_y))
        assert py == round(want), (canvas_y, py, want)
        assert ny == 0, (canvas_y, ny)        # 没被夹


def test_point_ex_reports_how_many_pixels_were_clamped():
    sm = inject.ScreenMap(CANVAS, SRC, DEV, fit="height")
    # 底部屏外 ⇒ 夹 81px（720 → 639）
    assert sm.point_ex(*BOTTOM_CENTER) == (805, 639, 0, 81)
    # 顶部越界会夹，并且报出来（负值 ⇒ 与底部夹取可区分）
    assert sm.point_ex(*TOP_OUT) == (805, 0, 0, -79)
    # 屏内点不报夹取
    assert sm.point_ex(540.0, 1170.0) == (805, 360, 0, 0)


def test_safe_bottom_y_guard_rejects_the_gesture_zone(monkeypatch):
    """★ 护栏：`SAFE_BOTTOM_Y` 设进手势区（或超出屏高）必须**抛**，不许静默降级。

    静默降级是最坏的形态：配置错了还照常跑、看着「成功」，实际整轮被踢回桌面。
    """
    sm = inject.ScreenMap(CANVAS, SRC, DEV, fit="height")
    for bad in (640, 660, 719):                # 手势区内
        monkeypatch.setattr(inject, "SAFE_BOTTOM_Y", bad)
        with pytest.raises(ValueError):
            sm.point(*BOTTOM_CENTER)
    monkeypatch.setattr(inject, "SAFE_BOTTOM_Y", 720)      # 超出可触摸域
    with pytest.raises(ValueError):
        sm.point(*BOTTOM_CENTER)
    # 反 vacuous：合法值必须能过 —— 否则上面四条可能只是「总是抛」
    monkeypatch.setattr(inject, "SAFE_BOTTOM_Y", 600)
    assert sm.point(*BOTTOM_CENTER) == (805, 600)


def test_point_canvas_maps_canvas_center_to_device_center():
    """画布中心 (960,540) → 真机 (805,360)；底边 → 639（屏外收到手势区之外）。"""
    sm = inject.ScreenMap(CANVAS, SRC, DEV, fit="height")
    assert sm.point_canvas(960.0, 540.0) == (805, 360)
    assert sm.point_canvas(960.0, 1080.0) == (805, 639)


def test_mutation_last_row_clamp_must_break_the_bottom_regression_assertion(monkeypatch):
    """变异测试：把上沿调回「屏内最后一行」（2026-10-22 事故版），回归断言必须红。

    防「断言与实现一起漂移」：哪天有人把 `SAFE_BOTTOM_Y` 调回 719，这个用例先失败。
    两条腿都要:① 719 被护栏拦下；② 合法但不同（620）能改出不同的值 ⇒ 判据可分辨。
    """
    sm = inject.ScreenMap(CANVAS, SRC, DEV, fit="height")
    assert sm.point(*BOTTOM_CENTER) == (805, 639)
    monkeypatch.setattr(inject, "SAFE_BOTTOM_Y", 719)
    with pytest.raises(ValueError):
        sm.point(*BOTTOM_CENTER)
    monkeypatch.setattr(inject, "SAFE_BOTTOM_Y", 620)
    assert sm.point(*BOTTOM_CENTER) == (805, 620) != (805, 639)


# ---------------------------------------------------------------- [3] 时基


def test_clock_offsets_from_anchor():
    t0 = time.perf_counter()
    clock = inject.Clock(t0)
    assert clock.at(0.0) == pytest.approx(t0)
    assert clock.at(1500.0) == pytest.approx(t0 + 1.5)


def test_wait_until_returns_immediately_when_already_past():
    late = time.perf_counter() - 1.0
    overshoot = inject.wait_until(late)
    assert overshoot >= 0.0


def test_wait_until_lands_close_to_target():
    """1ms 间隔连发 50 次（Hold 持续段的密度），中位偏差应远小于判定窗 ±80ms。"""
    t0 = time.perf_counter() + 0.05
    errs = []
    for i in range(50):
        errs.append(inject.wait_until(t0 + i * 0.001))
    errs.sort()
    assert errs[len(errs) // 2] < 0.005


def test_wait_until_can_hold_a_long_gap():
    """远距离也要准 —— busy-wait 只在最后 1.5ms 才自旋，前面靠 sleep。

    ⚠ 这是**全库唯一**依赖真实时钟的用例：Windows 调度器偶发把唤醒推迟 >10ms
    （实测约 6 次跑里出现 1 次），那不是回归、是 OS 抢占。所以取 3 次里**最好**的一次 ——
    判据 `0.0 <= err < 0.01` **不放宽**：实现真坏（比如去掉自旋窗口）时 3 次都会超。
    """
    best = min(inject.wait_until(time.perf_counter() + 0.2) for _ in range(3))
    assert 0.0 <= best < 0.01


# ------------------------------------------------- [4] 两个只有真机才会暴露的坑


def test_device_meta_len_matches_scrcpy_constant():
    """就绪握手依赖 DesktopConnection.DEVICE_NAME_FIELD_LENGTH = 64。"""
    assert inject.DEVICE_META_LEN == 64


class _FakeSock:
    """假 socket：按脚本逐次返回 recv 结果；sendall 只记录不发送。"""

    def __init__(self, chunks):
        self._chunks = list(chunks)
        self.closed = False
        self.opts = []          # 记录 setsockopt 调用，供护栏断言
        self.sent = []          # 记录 sendall 出去的字节串，供发送侧断言

    def settimeout(self, _t):
        pass

    def setsockopt(self, *a):
        self.opts.append(a)

    def recv(self, _n):
        return self._chunks.pop(0) if self._chunks else b""

    def sendall(self, data):
        self.sent.append(data)

    def close(self):
        self.closed = True

    def shutdown(self, _how):
        pass


def test_wait_ready_skips_dead_connection(monkeypatch):
    """TCP 连得上 != server 就绪。

    真机实测：adb forward 的 PC 侧监听一直都在，设备侧 LocalServerSocket 还没建好时
    adb 会接受 TCP 连接后**立刻关掉它**，客户端拿到一条死连接（recv 立刻返回 0 字节），
    而 sendall 还能「成功」一小会儿。若把这种连接当成就绪，就等于往空气里发指令。
    """
    dead = _FakeSock([b""])                          # 死连接
    good = _FakeSock([b"RMX5200".ljust(64, b"\0")])  # 真就绪：64 字节设备名
    seq = [dead, good]

    monkeypatch.setattr(inject.socket, "create_connection", lambda *a, **k: seq.pop(0))
    monkeypatch.setattr(inject.time, "sleep", lambda _s: None)

    adb = inject.Adb(inject.DEFAULT_ADB)
    server = inject.ScrcpyServer(adb, inject.DEFAULT_SERVER_JAR)
    server.port = 12345

    sock = server.wait_ready(timeout=5.0)
    assert sock is good
    assert server.device_name == "RMX5200"
    assert dead.closed, "死连接必须被丢弃并换一条重试"


def test_wait_ready_nodelay_off_by_default(monkeypatch):
    """默认**不**设 TCP_NODELAY（Nagle 生效）。

    r3 实测（2026-10-09，70MF IN）：开着 NODELAY 打出 33 Late Good + 3 Miss
    （S / 99.07%），而 Nagle 生效的 r2 是 0 Good + 2 Miss（V / 99.87%）。
    解释：NODELAY 让 8995 个 32B 小包逐个过 USB，逐包开销反而比攒包更抖。
    ⇒ 这条 localhost→adb→USB 链路上 Nagle 是盟友，默认关闭 NODELAY。
    """
    good = _FakeSock([b"RMX5200".ljust(64, b"\0")])
    monkeypatch.setattr(inject.socket, "create_connection", lambda *a, **k: good)
    monkeypatch.setattr(inject.time, "sleep", lambda _s: None)
    monkeypatch.delenv("PHGA_NODELAY", raising=False)

    adb = inject.Adb(inject.DEFAULT_ADB)
    server = inject.ScrcpyServer(adb, inject.DEFAULT_SERVER_JAR)
    server.port = 12345
    server.wait_ready(timeout=5.0)

    nodelay_opts = [o for o in good.opts if o[1] == _socket.TCP_NODELAY]
    assert nodelay_opts == [], "默认必须保持 Nagle（不设 TCP_NODELAY）"


def test_wait_ready_sets_tcp_nodelay_when_env_set(monkeypatch):
    """PHGA_NODELAY=1 时设 TCP_NODELAY=1（保留实验开关，便于复查）。"""
    good = _FakeSock([b"RMX5200".ljust(64, b"\0")])
    monkeypatch.setattr(inject.socket, "create_connection", lambda *a, **k: good)
    monkeypatch.setattr(inject.time, "sleep", lambda _s: None)
    monkeypatch.setenv("PHGA_NODELAY", "1")

    adb = inject.Adb(inject.DEFAULT_ADB)
    server = inject.ScrcpyServer(adb, inject.DEFAULT_SERVER_JAR)
    server.port = 12345
    server.wait_ready(timeout=5.0)

    assert (_socket.IPPROTO_TCP, _socket.TCP_NODELAY, 1) in good.opts, \
        "PHGA_NODELAY=1 时必须设置 TCP_NODELAY=1"


def test_wait_ready_times_out_when_never_ready(monkeypatch):
    monkeypatch.setattr(inject.socket, "create_connection",
                        lambda *a, **k: _FakeSock([b""]))
    monkeypatch.setattr(inject.time, "sleep", lambda _s: None)
    adb = inject.Adb(inject.DEFAULT_ADB)
    server = inject.ScrcpyServer(adb, inject.DEFAULT_SERVER_JAR)
    server.port = 12345
    with pytest.raises(RuntimeError, match="连不上 server"):
        server.wait_ready(timeout=0.0)


def test_server_start_gives_stdin_a_pipe(monkeypatch, tmp_path):
    """Popen 必须给 stdin 一个不会 EOF 的管道。

    真机实测：stdin 若是 /dev/null（继承自后台 shell），adb shell 会立刻 EOF 并结束
    会话，把设备侧 server 一起杀掉 —— 症状是 server 启动 ~0.86s 后连接突然断开，
    日志里只有 "Device:" 和 "Cleaning up"，没有 "Controller stopped"。
    """
    seen = {}

    class FakeProc:
        def __init__(self, args, **kw):
            seen["args"] = args
            seen["kw"] = kw
            self.stdin = None
            self.returncode = None

        def poll(self):
            return 0

    monkeypatch.setattr(inject.subprocess, "Popen", FakeProc)

    jar = tmp_path / "scrcpy-server"
    jar.write_bytes(b"x")
    adb = inject.Adb(inject.DEFAULT_ADB)
    monkeypatch.setattr(adb, "kill_server", lambda: 0)
    monkeypatch.setattr(adb, "run", lambda *a, **k: "")

    server = inject.ScrcpyServer(adb, jar)
    monkeypatch.setattr(server, "_pick_port", lambda: 12345)
    monkeypatch.setattr(server, "wait_ready", lambda timeout=25.0: "sock")

    server.start(timeout=1.0)
    assert seen["kw"].get("stdin") is inject.subprocess.PIPE


def test_server_log_level_is_a_proper_key_value_pair(monkeypatch, tmp_path):
    """`log_level` 必须以 `key=value` 形式传给设备侧 server —— **两种 verbose 都要**。

    2026-10-16 真机踩坑：原写法

        "log_level=info" if self.verbose else "warn",

    三元表达式的 else 分支少了 `log_level=` 前缀 ⇒ `verbose=False` 时 server 收到一个
    **裸 token** `warn`，`Options.parse` 抛 `Invalid key=value pair: "warn"` ⇒ server
    exit 1（日志落在 `tmp/inject_server.log`）。

    生产点火路径（`tmp/full_run.py`）一直传 `verbose=True` 所以从没暴露，但**默认值
    `verbose=False` 一直是坏的** —— `tmp/tap_once.py`、`pointer_guard.reset(verbose=False)`
    都会中招。
    """
    def argv_for(verbose: bool) -> list:
        seen = {}

        class FakeProc:
            def __init__(self, args, **kw):
                seen["args"] = args
                self.stdin = None
                self.returncode = None

            def poll(self):
                return 0

        monkeypatch.setattr(inject.subprocess, "Popen", FakeProc)
        jar = tmp_path / "scrcpy-server"
        jar.write_bytes(b"x")
        adb = inject.Adb(inject.DEFAULT_ADB)
        monkeypatch.setattr(adb, "kill_server", lambda: 0)
        monkeypatch.setattr(adb, "run", lambda *a, **k: "")
        server = inject.ScrcpyServer(adb, jar, verbose=verbose)
        monkeypatch.setattr(server, "_pick_port", lambda: 12345)
        monkeypatch.setattr(server, "wait_ready", lambda timeout=25.0: "sock")
        server.start(timeout=1.0)
        return list(seen["args"])

    for verbose, want in ((True, "log_level=info"), (False, "log_level=warn")):
        argv = argv_for(verbose)
        assert want in argv, f"verbose={verbose} 时应传 {want}，实得 {argv}"
        levelish = [a for a in argv if "log_level" in a or a in ("warn", "info")]
        assert levelish == [want], f"不许出现不带 key 的裸 token：{argv}"


# ---------------------------------------------------------------- [4] 卡指针兜底
#
# 2026-10-16 定案：注入中途被杀会在真机留下**永久按下的虚拟指针**，此后
# InputDispatcher 吞掉所有 DOWN（外部表现「怎么点都没反应」），且**只有
# adb reboot 能清**。Injector 是全项目唯一的发送出口，按下状态的记账放在这里，
# 退出前由 release_all() / close() 兜底抬指。


def _mk_injector():
    sock = _FakeSock([])
    return sock, inject.Injector(sock, 3136, 1440)


def _actions(sock):
    """解出已发送消息的 (action, pointer_id, x, y, pressure)。"""
    out = []
    for b in sock.sent:
        f = struct.unpack(inject.TOUCH_FMT, b)
        out.append((f[1], f[2], f[3], f[4], f[7]))
    return out


def test_injector_records_pressed_pointers_on_down_and_clears_on_up():
    _, inj = _mk_injector()
    inj.send(inject.ACTION_DOWN, 0, 100, 200)
    inj.send(inject.ACTION_DOWN, 3, 300, 400)
    assert inj.pressed == {0: (100, 200), 3: (300, 400)}

    inj.send(inject.ACTION_UP, 0, 100, 200)
    assert inj.pressed == {3: (300, 400)}


def test_injector_move_updates_last_known_position_without_changing_the_set():
    _, inj = _mk_injector()
    inj.send(inject.ACTION_DOWN, 2, 10, 20)
    inj.send(inject.ACTION_MOVE, 2, 11, 21)
    # MOVE 刷新「最后一次已知坐标」，但按下集合不变
    assert inj.pressed == {2: (11, 21)}


def test_release_all_lifts_every_stuck_pointer_at_its_last_position():
    sock, inj = _mk_injector()
    inj.send(inject.ACTION_DOWN, 0, 100, 200)
    inj.send(inject.ACTION_MOVE, 0, 150, 250)
    inj.send(inject.ACTION_DOWN, 3, 300, 400)
    del sock.sent[:]

    assert inj.release_all(settle_s=0) == 2
    assert inj.pressed == {}
    assert _actions(sock) == [
        (inject.ACTION_UP, 0, 150, 250, 0),   # 用 MOVE 之后的最后位置
        (inject.ACTION_UP, 3, 300, 400, 0),
    ]


def test_release_all_is_a_noop_when_nothing_is_pressed():
    sock, inj = _mk_injector()
    inj.send(inject.ACTION_DOWN, 0, 1, 2)
    inj.send(inject.ACTION_UP, 0, 1, 2)
    del sock.sent[:]

    assert inj.release_all(settle_s=0) == 0
    assert sock.sent == []


def test_release_all_survives_a_broken_socket():
    """兜底逻辑本身绝不能把收尾搞砸 —— socket 断了也要正常返回。"""

    class _Flaky(_FakeSock):
        def __init__(self):
            super().__init__([])
            self.broken = False

        def sendall(self, data):
            if self.broken:
                raise OSError("broken pipe")
            super().sendall(data)

    sock = _Flaky()
    inj = inject.Injector(sock, 3136, 1440)
    inj.send(inject.ACTION_DOWN, 1, 5, 6)
    assert inj.pressed == {1: (5, 6)}

    sock.broken = True
    assert inj.release_all(settle_s=0) == 1     # 不抛异常
    assert inj.pressed == {}


def test_close_releases_stuck_pointers_by_default():
    sock, inj = _mk_injector()
    inj.send(inject.ACTION_DOWN, 4, 7, 8)
    del sock.sent[:]

    inj.close()
    assert _actions(sock) == [(inject.ACTION_UP, 4, 7, 8, 0)]
    assert sock.closed


def test_close_can_skip_the_release():
    sock, inj = _mk_injector()
    inj.send(inject.ACTION_DOWN, 4, 7, 8)
    del sock.sent[:]

    inj.close(release=False)
    assert sock.sent == []
    assert sock.closed


def test_tap_leaves_nothing_pressed():
    sock, inj = _mk_injector()
    inj.tap(10, 20, hold_ms=0)
    assert inj.pressed == {}
    assert [a[0] for a in _actions(sock)] == [inject.ACTION_DOWN, inject.ACTION_UP]
