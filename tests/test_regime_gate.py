"""F2 锚定模式闸门（tmp/regime_check.py）的回归测试。

背景（FACTS §138）：2026-10-22 wanju_at AT（margin=6.043，A 档，应走 l4c+pipe）
被连打 4 轮纯 G。full_run 每轮第一行就打印了「⚠ 可在线锚却没带 l4c」，
但那是 warn（告警），不拦 ⇒ 知道却不做，4 轮全废。

修法：可锚却纯 G 由 warn 升级为 block，仅 `nogate` 显式放行。
这些用例的价值不在「跑通」，而在**锁住级别**——把 block 改回 warn 立刻红。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tmp"))

import regime_check  # noqa: E402

# 真实谱面文件：用来驱动 resolve_song / resolve_diff
WANJU_T = ROOT / "out" / "touches_wanju_at.json"
WANJU_M = ROOT / "samples" / "玩具狂奏曲終焉.きくお.0" / "music.wav"
INIT_T = ROOT / "out" / "touches_init.json"
INIT_M = ROOT / "samples" / "Initialize.iF.0" / "music.wav"
ARK_T = ROOT / "out" / "touches_ark.json"
ARK_M = ROOT / "samples" / "Ark.kanoryo.0" / "music.wav"

pytestmark = pytest.mark.skipif(
    not (WANJU_T.exists() and WANJU_M.exists()),
    reason="缺 wanju_at 谱面/音频，闸门用例依赖真实对照表条目",
)


def _wanju(**kw):
    return regime_check.check(music_ref=str(WANJU_M), touches=str(WANJU_T), **kw)


def test_incident_scene_pure_g_is_blocked():
    """事故现场：wanju_at AT 可在线锚却纯 G ⇒ 必须 block（曾经是 warn）。"""
    lvl, msg = _wanju(use_l4c=False, use_pipe=True)
    assert lvl == "block", f"可锚却纯 G 必须拦断，实际 {lvl}：{msg}"
    assert "拒绝点火" in msg
    # 消息要给出「为什么」（原则 + 量化后果），否则下次还会被无视
    assert "兜底" in msg


def test_incident_scene_with_nogate_downgrades_to_warn():
    """显式 nogate（对照实验）⇒ 放行但保留告警。"""
    lvl, msg = _wanju(use_l4c=False, use_pipe=True, allow_pure_g=True)
    assert lvl == "warn", f"nogate 应降为 warn，实际 {lvl}：{msg}"
    assert "nogate" in msg


def test_wanju_l4c_pipe_is_ok():
    """正确姿势不被误拦。"""
    lvl, msg = _wanju(use_l4c=True, use_pipe=True)
    assert lvl == "ok", msg


def test_allow_pure_g_does_not_disable_other_blocks():
    """★ nogate 只能放行「纯 G」这一条，不能连带放过 nopipe 那条。

    这是闸门最容易被写坏的地方：在函数开头 `if allow_pure_g: return ok`
    会让 nopipe 的随机 timeout 拦断一起失效。
    """
    lvl, msg = _wanju(use_l4c=True, use_pipe=False, allow_pure_g=True)
    assert lvl == "block", f"nogate 不该放行 nopipe，实际 {lvl}：{msg}"
    assert "nopipe" in msg


def test_pure_g_renders_ok_for_unanchorable_songs():
    """不可在线锚的曲目（ark）纯 G 才是正道，不能被拦。"""
    if not (ARK_T.exists() and ARK_M.exists()):
        pytest.skip("缺 ark 谱面/音频")
    lvl, msg = regime_check.check(
        music_ref=str(ARK_M), touches=str(ARK_T), use_l4c=False, use_pipe=False)
    assert lvl == "ok", msg


def test_unknown_song_fails_open():
    """查不到曲目 ⇒ fail-open（不能因为对照表缺条目就拦住所有新歌）。"""
    lvl, _ = regime_check.check(
        music_ref="samples/NoSuchSong.zzz.0/music.wav",
        touches="out/touches_nope.json", use_l4c=False, use_pipe=True)
    assert lvl == "ok"


# ── 接线：full_run 必须把 nogate 真正传进 check ──────────────────────────────
def test_full_run_wires_nogate_flag():
    """闸门再对，只要 full_run 不传 allow_pure_g=NOGATE 就等于没接线。

    ★ 这类「实现对了但没接上」的 bug 无法用 check() 的行为测出来，
      只能对源码做契约断言（教训：判据要能脱离 I/O 重放，这里退化为源码断言）。
    """
    src = (ROOT / "tmp" / "full_run.py").read_text(encoding="utf-8")
    assert 'NOGATE = "nogate" in sys.argv[1:]' in src, "full_run 未定义 nogate 旗标"
    assert "allow_pure_g=NOGATE" in src, "full_run 调 check() 时没传 allow_pure_g"


def test_full_run_blocks_before_any_device_io():
    """block 分支必须出现在 main() 的设备操作之前。

    否则闸门会在「已经推了 scrcpy-server / 起了 screenrecord」之后才返回，
    用户看到「拒绝点火」但设备已经被动过 —— 比不拦更糟。
    """
    src = (ROOT / "tmp" / "full_run.py").read_text(encoding="utf-8")
    main_at = src.index("def main()")
    gate_at = src.index('if _lvl == "block"', main_at)
    # main() 之后的第一次真实设备动作
    io_marks = []
    for pat in ("screenrecord", "push scrcpy-server", "inject.point", "启动（限时"):
        i = src.find(pat, main_at)
        if i != -1:
            io_marks.append((pat, i))
    assert io_marks, "没找到设备动作标记 —— 断言本身失效，需更新标记列表"
    first_io = min(i for _p, i in io_marks)
    assert gate_at < first_io, "F2 闸门位于设备操作之后 ⇒ 拦断时设备已被动过"
