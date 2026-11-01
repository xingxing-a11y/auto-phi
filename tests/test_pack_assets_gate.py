"""`tools/pack_assets` 的载荷口径闸门（PC 侧孪生）。

为什么单独立一份测试：端内 `Scheduler.requireGameCanvas` 只能**拒绝开火**，
而那时已经装完机、停好歌了。10-24 的 run5/run6 就是这么白打的两轮 ——
载荷错在 PC 生成端，直到真机跑完才从成绩上看出来。

★ 这里既测"该红的红"，也测"该绿的绿"：
  `out/touches_schneid.json` / `touches_yume.json` 的 `canvas == screen`
  是**合法退化**（1920x1080 恰等于画布），任何"canvas 不许等于 screen"的
  判据都会把它们判死 —— 那正是方案初稿被自审拦下的那条。
"""

import json
from pathlib import Path

import pytest

from tools.pack_assets import (
    CANVAS_ASPECT_TOL,
    require_events_in_screen,
    require_game_canvas,
)

ROOT = Path(__file__).resolve().parent.parent


def cv(w, h):
    return {"width": float(w), "height": float(h)}


# ─────────────────────────── canvas 16:9 ───────────────────────────

def test_normal_payload_passes():
    require_game_canvas(cv(1920, 1080), cv(1080, 2340))


def test_canvas_written_as_screen_is_rejected():
    """本次事故的形状：canvas 被填成 screen（1080x2340，竖屏宽高比 0.46）。"""
    with pytest.raises(ValueError) as e:
        require_game_canvas(cv(1080, 2340), cv(1080, 2340))
    assert "16:9" in str(e.value)
    assert "1920" in str(e.value), "文案要给出正确口径的示例，否则读者还得自己猜"


def test_canvas_equals_screen_is_legal():
    """★ 防误伤：游戏分辨率恰等于画布时 canvas == screen 完全合法。"""
    require_game_canvas(cv(1920, 1080), cv(1920, 1080))


@pytest.mark.parametrize("w,h,ok", [
    (1920, 1080, True),
    (1280, 720, True),
    (2560, 1440, True),
    (1920, 1200, False),   # 16:10
    (1080, 2340, False),   # 竖屏（本次事故）
    (1920, 1081, True),    # 差 1 px，(1920*9=17280 vs 1081*16=17296) 差 0.09% < 0.1%
    (1920, 1085, False),   # 差 5 px，0.46% > 0.1% ⇒ 必须红
])
def test_aspect_boundaries(w, h, ok):
    if ok:
        require_game_canvas(cv(w, h), cv(1080, 2340))
    else:
        with pytest.raises(ValueError):
            require_game_canvas(cv(w, h), cv(1080, 2340))


def test_zero_canvas_is_rejected():
    with pytest.raises(ValueError) as e:
        require_game_canvas(cv(0, 0), cv(1080, 2340))
    assert "非法" in str(e.value)


# ─────────────────────────── 事件坐标 ───────────────────────────

def test_events_inside_screen_pass():
    require_events_in_screen(
        [{"x": 0, "y": 0}, {"x": 1080, "y": 2340}, {"x": 540, "y": 1170}], cv(1080, 2340)
    )


def test_event_outside_screen_is_rejected():
    with pytest.raises(ValueError) as e:
        require_events_in_screen([{"x": 2000, "y": 100}], cv(1080, 2340))
    assert "不在 screen" in str(e.value)
    assert "events[0]" in str(e.value), "文案要能定位到第几条"


# ─────────────────────────── 真实数据零误伤 ───────────────────────────

def _all_payloads():
    paths = sorted((ROOT / "out").glob("touches_*.json"))
    paths += sorted((ROOT / "android/play/app/src/main/assets/payload").glob("*.json"))
    return paths


def test_all_real_payloads_pass_the_gate():
    """★ 零误伤的硬证据：磁盘上**全部**真实载荷都必须通过（不是抽检）。"""
    paths = _all_payloads()
    assert len(paths) >= 70, f"只找到 {len(paths)} 份载荷，样本不足"
    bad = []
    for p in paths:
        d = json.loads(p.read_text(encoding="utf-8"))
        try:
            require_game_canvas(d["canvas"], d["screen"])
            require_events_in_screen(d["events"], d["screen"])
        except (ValueError, KeyError) as ex:
            bad.append((p.name, str(ex)))
    assert not bad, f"真实载荷被误伤 {len(bad)} 份：{bad[:5]}"


def test_tolerance_is_actually_wired(monkeypatch):
    """变异防护：容差若被放宽成"什么都不管"，1920x1200 就必须红。"""
    import tools.pack_assets as P

    monkeypatch.setattr(P, "CANVAS_ASPECT_TOL", 1.0)
    P.require_game_canvas(cv(1920, 1200), cv(1080, 2340))    # 放宽后放过
    monkeypatch.setattr(P, "CANVAS_ASPECT_TOL", 1e-6)
    with pytest.raises(ValueError):
        P.require_game_canvas(cv(1920, 1081), cv(1080, 2340))  # 收紧后 1 px 也红
    assert CANVAS_ASPECT_TOL == 1e-3, "生产默认值必须是 1e-3"
