"""`tmp/regen_touches.py` 的四态判定与落盘路径。

背景（2026-10-23）：原实现只有「旧口径复刻不出 ⇒ 拒绝写入」两态，
把**已经是新口径**的 `wanju_at`（磁盘 y=1405.406，旧口径复刻 1433.25）
误报成「口径漂移」⇒ 批量分类时已修好的文件全被判成异常。
改成 `old / new / same / drift` 四态后必须能分开，且**只有 `old` 落盘**。

本文件锁四件事：
1. `classify` 纯函数的四态**互斥且可区分**（变异测试：把 new 判成 old 必须红）；
2. 两套口径产物相同时不误报成 `old`（否则会做一次内容相同的无意义重写）；
3. 落盘路径：`old` 文件跑完变成 `new`，事件条数不变、底部被夹到安全上沿；
4. 幂等 + `drift` 拒写：第二次跑不改字节，漂移文件一个字节都不许动。
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
TMP = ROOT / "tmp"

#: 公开仓按设计不导出 `tmp/`（内部工具不在分发范围内）⇒ 这里只能在主仓跑。
pytestmark = pytest.mark.skipif(
    not (TMP / "regen_touches.py").exists(),
    reason="公开仓不导出 tmp/（载荷重生成工具只在主仓）",
)


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, TMP / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def rg():
    return _load("_pg_regen_under_test", "regen_touches.py")


def _sig(y: float, n: int = 1) -> list:
    """造 n 条签名，y 用来区分口径。"""
    return [(100 * i, "MOVE", 0, 500.0, y) for i in range(n)]


# ---------------------------------------------------------------- classify 四态

def test_classify_old(rg):
    assert rg.classify(_sig(1433.25), _sig(1433.25), _sig(1405.406))[0] == rg.STATE_OLD


def test_classify_new(rg):
    """磁盘已是新口径 ⇒ new，且绝不能是 old（否则会被无意义重写）。"""
    st, _ = rg.classify(_sig(1405.406), _sig(1433.25), _sig(1405.406))
    assert st == rg.STATE_NEW
    assert st != rg.STATE_OLD


def test_classify_same_when_two_ratios_agree(rg):
    """该谱没有 Flick 触底 ⇒ 两套口径产物相同 ⇒ same，不是 old。"""
    st, _ = rg.classify(_sig(900.0), _sig(900.0), _sig(900.0))
    assert st == rg.STATE_SAME
    assert st != rg.STATE_OLD


def test_classify_same_but_real_differs_is_drift(rg):
    """两口径相同却复刻不出磁盘 ⇒ 别的参数漂了，不能报 same。"""
    st, note = rg.classify(_sig(910.0), _sig(900.0), _sig(900.0))
    assert st == rg.STATE_DRIFT
    assert "首处差异" in note


def test_classify_drift_reports_first_diff(rg):
    st, note = rg.classify(_sig(1234.0), _sig(1433.25), _sig(1405.406))
    assert st == rg.STATE_DRIFT
    assert "idx=0" in note


def test_classify_length_mismatch_is_drift(rg):
    st, note = rg.classify(_sig(1405.406, 2), _sig(1433.25, 1), _sig(1405.406, 1))
    assert st == rg.STATE_DRIFT
    assert "条数不同" in note


def test_classify_empty_is_same_not_crash(rg):
    assert rg.classify([], [], [])[0] == rg.STATE_SAME


def test_sig_distinguishes_pure_y_shift(rg):
    """签名必须带 y：否则「只有 y 变了」的口径差异会被判成相同。

    这张谱的扫动法向多带 x 分量，所以就算签名丢了 y，逐事件比对仍能分出旧/新口径
    （实测真签名 8780 处不同，y 归零后还剩 7904 处）—— 端到端测试**抓不到**这条。
    用纯函数钉住它，别让 `_sig` 将来被简化成只比 `(t_ms, action, pointer_id, x)`。
    """
    a = {"events": [{"t_ms": 0, "action": "MOVE", "pointer_id": 0, "x": 100.0, "y": 1405.4}]}
    b = {"events": [{"t_ms": 0, "action": "MOVE", "pointer_id": 0, "x": 100.0, "y": 1433.2}]}
    assert rg._sig(a) != rg._sig(b)


def test_classify_states_are_pairwise_distinct(rg):
    """四个状态字符串必须互不相同 —— 否则「判成 A 还是 B」的断言会空转。"""
    sts = {rg.STATE_OLD, rg.STATE_NEW, rg.STATE_SAME, rg.STATE_DRIFT}
    assert len(sts) == 4


# ------------------------------------------------- 坐标映射（独立公式对照）

def test_touches_doc_uses_independent_formula(rg):
    """y 走 `canvas_y*scale + y_off`，x 走 `canvas_x*scale`。

    期望值用**独立算式**给出（不调用被测函数），否则两边同错看不出来。
    """
    doc = {"canvas": {"width": 1920, "height": 1080},
           "screen": {"width": 1080, "height": 2340}}
    sched = SimpleNamespace(events=[
        SimpleNamespace(t_ms=0, action="MOVE", pointer_id=0, x=960.0, y=1080.0),
        SimpleNamespace(t_ms=10, action="PRESS", pointer_id=0, x=0.0, y=0.0),
    ])
    out = rg.touches_doc(doc, sched)
    scale = 1080 / 1920
    y_off = (2340 - 1080 * scale) / 2
    assert out["events"][0]["x"] == round(960.0 * scale, 3)
    assert out["events"][0]["y"] == round(1080.0 * scale + y_off, 3)
    assert out["events"][1]["y"] == round(0.0 * scale + y_off, 3)
    assert out["event_count"] == 2
    # 输入文档不许被就地改写
    assert "events" not in doc


def test_oob_stats_counts_only_move(rg):
    """只有 MOVE（Flick 扫动）受底部安全上沿约束；同高度的 PRESS 不算超界。

    判据必须盯「功能赖以成立的那个量」——约束本来只加在扫动上。
    """
    safe = rg.sch.FLICK_BOTTOM_SAFE_RATIO * 1080 * (1080 / 1920) + (2340 - 1080 * (1080 / 1920)) / 2
    doc = {"canvas": {"width": 1920, "height": 1080},
           "screen": {"width": 1080, "height": 2340},
           "events": [
               {"action": "MOVE", "y": safe + 10},
               {"action": "PRESS", "y": safe + 10},
               {"action": "MOVE", "y": safe - 1},
           ]}
    st = rg.oob_stats(doc)
    assert st["move_over"] == 1


# ------------------------------------------------------- 落盘路径（E2E，临时根）

@pytest.fixture(scope="module")
def e2e(rg, tmp_path_factory):
    """在临时根里复现一份**旧口径**文件，返回 (rg, root, key)。"""
    src_notes = ROOT / "out" / "notes_wanju_at.json"
    src_touches = ROOT / "out" / "touches_wanju_at.json"
    if not (src_notes.is_file() and src_touches.is_file()):
        pytest.skip("缺 wanju_at 素材")

    root = tmp_path_factory.mktemp("regen")
    (root / "out").mkdir()
    key = "e2e"
    (root / "out" / f"notes_{key}.json").write_bytes(src_notes.read_bytes())

    template = json.loads(src_touches.read_text(encoding="utf-8"))
    notes_doc = rg.sch.load_notes(root / "out" / f"notes_{key}.json")
    kw = dict(geometry=notes_doc.geometry, canvas=notes_doc.canvas,
              presence_reuse_ratio=20.0 / notes_doc.canvas[0])
    old = rg.touches_doc(template, rg.sch.build(notes_doc.notes,
                                                flick_bottom_safe_ratio=rg.OLD_RATIO, **kw))
    (root / "out" / f"touches_{key}.json").write_text(
        json.dumps(old, ensure_ascii=False), encoding="utf-8")
    return rg, root, key


def test_e2e_old_file_rewritten_to_new(e2e):
    rg, root, key = e2e
    path = root / "out" / f"touches_{key}.json"
    before = json.loads(path.read_text(encoding="utf-8"))

    code, seen = rg.run(key, dry_run=False, root=root)

    assert code == 0
    assert seen[key] == rg.STATE_OLD
    after = json.loads(path.read_text(encoding="utf-8"))
    assert len(after["events"]) == len(before["events"]), "重生成不许改变事件条数"

    # 判据 1：扫动被夹 ⇒ 越安全上沿的 MOVE 必须**显著下降**（实测 2646 → 60）。
    # 不能要求降到 0：音符本体位置（`_move` 归位 / `Hold.keep`）本来就在手势区，
    # 夹了会打不中 —— 见 `oob_stats` 的告警段。
    b, a = rg.oob_stats(before), rg.oob_stats(after)
    assert a["move_over"] < b["move_over"] / 10
    # 判据 2：**不许过度夹取**。`max_y` 由音符位置决定，必须原样保留。
    assert a["max_y"] == b["max_y"], "音符本体的归位/保持位置不许被夹"
    # 判据 3：真改了几何（不是原样写回）
    assert [e for e in after["events"]] != [e for e in before["events"]]


def test_e2e_second_run_is_idempotent(e2e):
    rg, root, key = e2e
    path = root / "out" / f"touches_{key}.json"
    # 先把文件改写成「语义相同、字节不同」的形式（紧凑分隔符）。
    # ⚠️ 不这么做的话，这条测试会**空转**：重写一遍产出的字节与原来完全相同，
    #    字节比对照样通过 —— 实测「非 old 也写盘」的变异就是这么漏过去的。
    doc = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps(doc, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    b1 = path.read_bytes()

    code, seen = rg.run(key, dry_run=False, root=root)

    assert code == 0
    assert seen[key] == rg.STATE_NEW
    assert path.read_bytes() == b1, "已是新口径时不许重写文件（字节必须一致）"


def test_e2e_drift_file_untouched(e2e):
    rg, root, key = e2e
    path = root / "out" / f"touches_{key}.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["events"][5]["t_ms"] += 7          # 制造「两套口径都复刻不出」
    path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    b0 = path.read_bytes()

    code, seen = rg.run(key, dry_run=False, root=root)

    assert code == 1
    assert seen[key] == rg.STATE_DRIFT
    assert path.read_bytes() == b0, "drift 必须一个字节都不动"


def test_e2e_missing_notes_reported(rg, tmp_path):
    (tmp_path / "out").mkdir()
    code, seen = rg.run("nope", dry_run=False, root=tmp_path)
    assert code == 1
    assert seen["nope"] == "missing"
