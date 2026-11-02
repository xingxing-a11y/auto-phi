"""`edge=fast`（检测节拍加密 + 滞后分解）—— 离线测试，不连手机。

背景（2026-10-23，Ⅱ-b 专项）：
    「首音能否 Perfect」的判据不是某种感觉，而是可算的：

        裕度 = margin + 80ms（P 窗） − 检测延迟
        检测延迟 = 数据供给滞后 + 算法/轮询开销

    137 轮 dmeas 实测：当前 APK 边沿法的「算法+轮询」只占 **0~30 ms**，
    而「供给」是 63~259 ms ⇒ **瓶颈在数据供给**。所以 `edge=fast` 的定位是
    「把能砍的 20 ms 砍掉 + 把两个分量测出来」，而不是「让 Ⅱ-b 一起变好」。

本文件覆盖五层，每层都能**单独否决**：

    [1] `split_detect_lag` 纯函数：精确值 / 守恒 / 负供给不夹 / 符号不许反。
    [2] 节拍常量：钉住生产值（0.02 / 0.005），且 fast 必须真的更快。
    [3] **判据不变**：`edge_fast=True` 不许动 sustain / sil_ref / sil_thr ——
        动 sustain 会让 2 ms 尖峰在 20 ms 窗里变成 1.107×thr 而否决失败（能算出真值）。
    [4] `_try_detect` 的产物：fast 才写 lag_*，normal 一个都不许多；
        且必须把 **edge/avail 的正确入参**传给分解函数（搞反会静默给出错数）。
    [5] `full_run.py` 的**命令行 → 生效配置**（dry 早退点）：默认 normal、
        fast 生效、非法值与不兼容组合要早退而不是跑起来。
"""

from __future__ import annotations

import json
import subprocess
import sys
import wave
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TMP = ROOT / "tmp"
if str(TMP) not in sys.path:
    sys.path.insert(0, str(TMP))

AR = pytest.importorskip("audio_round")  # tmp/ 下的跑分工具，公开仓不导出 tmp ⇒ 跳过

REF70 = ROOT / "samples" / "70mf" / "music.wav"


# ------------------------------------------------ [1] 纯函数：分解与守恒

def test_split_lag_exact_values():
    """手算一组（注意 t_detect 与 edge_s 同轴，都是相对 t_launch）：
    t_launch=100、t_detect=110.180、edge=10.0、avail=10.060。

        total  = (110.180 − 100 − 10.0) × 1000 = 180.0 ms
        supply = (10.060 − 10.0)        × 1000 =  60.0 ms
        algo   = 180.0 − 60.0                  = 120.0 ms
    """
    got = AR.split_detect_lag(t_detect=110.180, t_launch=100.0,
                              edge_s=10.0, avail_s=10.060)
    assert got["lag_total_ms"] == pytest.approx(180.0, abs=1e-6)
    assert got["lag_supply_ms"] == pytest.approx(60.0, abs=1e-6)
    assert got["lag_algo_ms"] == pytest.approx(120.0, abs=1e-6)


def test_split_lag_is_additive():
    """不变量：total ≡ supply + algo。任一项定义写错都会破这条。"""
    for td, tl, e, av in [
        (53.05, 50.0, 3.0, 3.05),
        (58.5, 50.0, 2.5, 7.5),
        (50.2, 50.0, 0.0, 0.2),
    ]:
        got = AR.split_detect_lag(td, tl, e, av)
        assert got["lag_total_ms"] == pytest.approx(
            got["lag_supply_ms"] + got["lag_algo_ms"], abs=0.2)


def test_split_lag_algo_is_not_total_plus_supply():
    """★ 变异防线：把 algo 写成 `total + supply` 的符号错必须被抓住。

    正确值 120；写成加法会得 240 —— 这条断言在错实现下直接红。
    """
    got = AR.split_detect_lag(110.180, 100.0, 10.0, 10.060)
    assert got["lag_algo_ms"] == pytest.approx(120.0, abs=1e-6)
    assert got["lag_algo_ms"] != pytest.approx(
        got["lag_total_ms"] + got["lag_supply_ms"], abs=1e-6)


def test_split_lag_supply_zero_when_avail_equals_edge():
    """数据刚好到 edge ⇒ 供给滞后为 0，全部滞后都算算法。"""
    got = AR.split_detect_lag(110.030, 100.0, 10.0, 10.0)
    assert got["lag_supply_ms"] == pytest.approx(0.0, abs=1e-6)
    assert got["lag_algo_ms"] == pytest.approx(30.0, abs=1e-6)


def test_split_lag_keeps_negative_supply():
    """`avail < edge` 只可能来自上层传参错误 —— 必须留着真值让人看见，不许夹成 0。

    夹成 0 的后果：`algo` 会被静默放大成 total，把「传参写反」伪装成
    「算法很慢」，于是又去加密轮询（白干）。
    """
    got = AR.split_detect_lag(110.030, 100.0, 10.0, 9.98)
    assert got["lag_supply_ms"] == pytest.approx(-20.0, abs=1e-6)
    assert got["lag_total_ms"] == pytest.approx(30.0, abs=1e-6)
    assert got["lag_algo_ms"] == pytest.approx(50.0, abs=1e-6)


def test_split_lag_matches_real_dmeas():
    """与真机实测对拍（现读磁盘，不写死数）：hckwaya 那轮应是 125.7 / 71.6 / 54.1。

    这条同时钉住「dmeas 的 live.edge_s 与 samples_avail_s 同轴」这个前提 ——
    若哪天有人把 `edge_s` 改成相对窗口而不是相对 t_launch，这里会红。
    """
    p = TMP / "dmeas_hckwaya.json"
    if not p.exists():
        pytest.skip("缺 tmp/dmeas_hckwaya.json（真机留档）")
    d = json.loads(p.read_text(encoding="utf-8"))
    lv = d["live"]
    got = AR.split_detect_lag(lv["t_detect"], d["t_launch"],
                              lv["edge_s"], lv["samples_avail_s"])
    # 与 dmeas 里独立落盘的 edge 对拍（防止两边口径漂移）
    assert lv["edge_s"] == pytest.approx(d["edge_s"], abs=1e-3)
    # 实测值（2026-10-23 现算；容差 1 ms 因为 json 只存 4 位小数）
    assert got["lag_total_ms"] == pytest.approx(125.7, abs=1.0)
    assert got["lag_supply_ms"] == pytest.approx(71.6, abs=1.0)
    assert got["lag_algo_ms"] == pytest.approx(54.1, abs=1.0)
    # 结论方向必须成立：这一轮供给 > 算法 ⇒ 该改供给，不该继续加密轮询
    assert got["lag_supply_ms"] > got["lag_algo_ms"]


# ------------------------------------------------ [2] 节拍常量

def test_detect_beats_are_pinned():
    """生产值：默认 20 ms、fast 5 ms。改数值必须同步改本测试与用法段。"""
    assert AR.DETECT_BEAT_S == pytest.approx(0.020)
    assert AR.DETECT_BEAT_FAST_S == pytest.approx(0.005)
    assert AR.DETECT_BEAT_FAST_S < AR.DETECT_BEAT_S


def test_detect_beat_honours_the_switch():
    """★ 行为钉子：`detect_beat()` 必须真的跟着开关走。

    只断言常量是不够的 —— 变异测试实测：把调用点写成 `beat = DETECT_BEAT_S`
    （等于 fast 静默失效），常量断言仍然全绿。这条才抓得住。
    """
    assert AR.detect_beat(True) == pytest.approx(AR.DETECT_BEAT_FAST_S)
    assert AR.detect_beat(False) == pytest.approx(AR.DETECT_BEAT_S)
    assert AR.detect_beat(True) < AR.detect_beat(False)


def test_poll_sleep_is_the_real_bottleneck():
    """★★ 核心：`sleep` 0.25 s 才是供给滞后的主因，不是 detect 节拍。

    默认档 12 s 后 drain 降到 4 Hz ⇒ 新字节积压最多 250 ms 才被看见；而 Ⅱ-b 的
    edge 在 ▶ 后 17~19 s，整段落在降频区（实测供给 71.6~258.7 ms vs 设备端块
    粒度 8192 B = 42.7 ms）。所以 fast 档必须把 sleep 也钉在 5 ms ——
    **只加密 detect 节拍对 Ⅱ-b 完全无用**。
    """
    assert AR.poll_sleep(False, 5.0) == pytest.approx(0.005)     # 默认档前段
    assert AR.poll_sleep(False, 18.0) == pytest.approx(0.25)     # 默认档降频（Ⅱ-b 落在这里）
    assert AR.poll_sleep(True, 18.0) == pytest.approx(0.005)     # ★ fast 不许降频
    assert AR.poll_sleep(True, 60.0) == pytest.approx(0.005)


def test_detect_beat_late_prefers_finer_fast():
    """慢档节拍：默认 50 ms，fast 20 ms（不是 5 ms —— 注入期间要留 CPU）。"""
    assert AR.detect_beat_late(False) == pytest.approx(0.05)
    assert AR.detect_beat_late(True) == pytest.approx(0.02)
    assert AR.detect_beat_late(True) < AR.detect_beat_late(False)


# ------------------------------------------------ [3] 判据一行都不许动

def _mk(tag: str, **kw) -> AR.AudioRound:
    """造一个不启动线程、不开文件的实例（live=False ⇒ 不起 _poll_loop）。"""
    return AR.AudioRound(tag, dur_s=30, live=False, ref=REF70, **kw)


def test_edge_fast_defaults_off():
    ar = _mk("t_def")
    assert ar.edge_fast is False


def test_edge_fast_does_not_touch_criteria():
    """★ 核心防线：fast 只改节拍，判据必须逐项相同。

    为什么这么严：`sustain_ms` / `win_ms` 同时决定**参考侧**的 sil_ref。
    动 sustain 到 20 ms 会让 2 ms 尖峰在 20 ms 窗里的 RMS = √(2/20)×3.5 = 1.107×thr
    ⇒ 尖峰否决失败（30 ms 窗才是 √(2/30)×3.5 = 0.904×thr）。
    这条断言能抓住「顺手把 sustain 调小」的改动。
    """
    nor = _mk("t_nor")
    fst = _mk("t_fst", edge_fast=True)
    assert fst.edge_fast is True
    # ★ 必须同时钉**绝对值**：只比「两者相等」的话，把两边的 sustain 一起改成
    #   20 ms 仍然通过（变异测试实测这就是空转）。SUSTAIN_MS 是判据的一部分。
    assert fst.sustain_ms == pytest.approx(AR.SUSTAIN_MS)
    assert nor.sustain_ms == pytest.approx(AR.SUSTAIN_MS)
    assert fst.sustain_ms == nor.sustain_ms
    assert fst.sil_ref == nor.sil_ref
    assert fst.sil_thr == nor.sil_thr


def test_sustain_20ms_would_fail_to_reject_2ms_spike():
    """把上面那句「能算出真值」落实成断言：证明 20 ms 真的挡不住 2 ms 尖峰。

    尖峰 96 样本 @48k = 2.0 ms，振幅 3.5×thr（见 tb_audio_edge.selftest_spike）。
    窗内 RMS = sqrt(2/win) × 3.5 × thr：
        30 ms ⇒ 0.904×thr < thr  ⇒ 否决成功（现行）
        20 ms ⇒ 1.107×thr > thr  ⇒ 否决失败（不许改成这个值）
    """
    import math
    amp_rel = 3.5
    spike_ms = 96 / 48000.0 * 1000.0
    rms30 = math.sqrt(spike_ms / 30.0) * amp_rel
    rms20 = math.sqrt(spike_ms / 20.0) * amp_rel
    assert rms30 < 1.0, f"30 ms 窗应能否决尖峰，实得 {rms30:.3f}×thr"
    assert rms20 > 1.0, f"20 ms 窗应否决失败（这就是不许改 sustain 的原因），实得 {rms20:.3f}×thr"
    assert AR.SUSTAIN_MS == pytest.approx(30.0)


# ------------------------------------------------ [4] _try_detect 产物

def _pad_buf(ar: AR.AudioRound, seconds: float = 8.0) -> None:
    """往 `_buf` 塞一段合法 wav（数据量足够绕开 64 KiB 与 after_s+2.0 两个门）。"""
    import io

    bio = io.BytesIO()
    with wave.open(bio, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(48000)
        w.writeframes(b"\x00\x00" * 2 * int(48000 * seconds))
    ar._buf = bytearray(bio.getvalue())
    ar.t_launch = 100.0
    ar.t0 = 100.0 + (seconds - 2.5)      # after_s = seconds − 2.8，留够测试余量


def test_try_detect_writes_lag_only_in_fast(monkeypatch):
    """fast ⇒ 必须带 lag_*；normal ⇒ 一个都不许多（保 138 份既有产物口径不变）。"""
    seen: list[tuple] = []

    def fake_split(t_detect, t_launch, edge_s, avail_s):
        seen.append((t_detect, t_launch, edge_s, avail_s))
        return {"lag_total_ms": 1.0, "lag_supply_ms": 2.0, "lag_algo_ms": 3.0}

    fake_edge = {"edge_s": 1.5, "run_dur_s": 5.0, "confirmed": "full",
                 "sustain_ms": 30.0, "thr": 100.0}
    monkeypatch.setattr(AR, "find_silence_end", lambda *a, **k: dict(fake_edge))
    monkeypatch.setattr(AR, "split_detect_lag", fake_split)

    fst = _mk("t_fast_detect", edge_fast=True)
    _pad_buf(fst)
    got = fst._try_detect()
    assert got is not None
    assert got["lag_total_ms"] == 1.0
    assert got["lag_supply_ms"] == 2.0
    assert got["lag_algo_ms"] == 3.0
    # ★ 入参方向：edge 必须传**绝对**位置（after_s + 窗口内位置），avail 传可见时长。
    #   两者搞反会让 supply 变成负数而静默通过 —— 下面这条钉死方向。
    assert len(seen) == 1
    _, tl, edge_arg, avail_arg = seen[0]
    assert tl == pytest.approx(100.0)
    assert edge_arg == pytest.approx(got["edge_s"])     # absolute
    assert avail_arg == pytest.approx(got["avail_s"])
    assert edge_arg > fst._after_s                      # 不是窗口内相对位置
    assert avail_arg > edge_arg                          # 数据确实比边沿多

    nor = _mk("t_nor_detect")
    _pad_buf(nor)
    got2 = nor._try_detect()
    assert got2 is not None
    for k in ("lag_total_ms", "lag_supply_ms", "lag_algo_ms"):
        assert k not in got2, f"normal 档不许写 {k}（会污染既有 dmeas 口径）"


def test_try_detect_returns_none_before_enough_audio(monkeypatch):
    """数据不够时 `_try_detect` 必须返回 None（不许用半截数据下结论）。"""
    monkeypatch.setattr(AR, "find_silence_end",
                        lambda *a, **k: pytest.fail("不该在数据不足时检测"))
    ar = _mk("t_short", edge_fast=True)
    _pad_buf(ar, seconds=3.0)
    ar.t0 = 100.0 + 6.0                    # after_s + 2.0 > 3.0 ⇒ 门未过
    assert ar._try_detect() is None


# ------------------------------------------------ [5] 命令行 → 配置

def _dry(*args: str) -> subprocess.CompletedProcess:
    base = ["7.07551", "1e9", "height", "154", "cleyera",
            str(ROOT / "out" / "touches_cleyera.json")]
    return subprocess.run([sys.executable, str(TMP / "full_run.py"), *base,
                           *args, "dry"],
                          # ★ 显式 UTF-8：子进程是 UTF-8（conftest 设了 PYTHONUTF8），
                          #   而 `text=True` 会按**本地**编码（中文机 = gbk）解码 ⇒ 中文断言全
                          #   变成乱码。两端必须同编码（10-25 实测：不加这两行 13 条红）。
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=120)


def test_dry_default_is_normal():
    r = _dry("l4c", "src=apk")
    assert r.returncode == 0, r.stderr[-800:]
    assert "edge_fast=False" in r.stdout
    assert "5 ms" not in r.stdout.split("edge_fast=")[1][:80]


def test_dry_edge_fast_takes_effect():
    r = _dry("l4c", "src=apk", "edge=fast")
    assert r.returncode == 0, r.stderr[-800:]
    assert "edge_fast=True" in r.stdout
    assert "判据不变" in r.stdout
    # ★ 这条依赖 `detect_beat()` 的**返回值**：把函数体改成恒返回默认值（fast 静默失效）
    #   时，这里会打印 20 ms ⇒ 红。只断言常量做不到这一点。
    assert "检测节拍 = 5 ms" in r.stdout, r.stdout[-400:]


def test_dry_default_beat_is_20ms():
    r = _dry("l4c", "src=apk")
    assert r.returncode == 0, r.stderr[-800:]
    assert "检测节拍 = 20 ms" in r.stdout, r.stdout[-400:]


@pytest.mark.parametrize("args,needle", [
    (("l4c", "src=apk", "edge=slow"), "只接受 fast / normal"),
    (("edge=fast",), "必须同时带 l4c 或 aud"),
    (("pas", "edge=fast"), "与 pas 不兼容"),
])
def test_dry_rejects_bad_edge_args(args, needle):
    r = _dry(*args)
    assert r.returncode != 0, r.stdout[-500:]
    assert needle in (r.stdout + r.stderr)
