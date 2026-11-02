"""APK 音频源的生产接线（`full_run.py src=apk`）—— 离线测试，不连手机。

覆盖四层，每层都有**能单独否决**的判据：

    [1] `apk_prod` 的时序胶水：前台判据 / 画面判据 / 录音时长分账
        （自测 23 项，见 `tmp/apk_prod.py --selftest`）。
    [2] `full_run.py` 的**命令行 → 生效配置**：`src=apk` 必须换 L 池、换时长，
        且非法组合（没带 l4c、与 pas 互斥、拼错 src）必须**早退**而不是跑起来。
        这一层只能靠 `dry` 早退点验 —— 否则就只能真机点火来验，代价太高。
    [3] `audio_round.apply_lag_update` 的新参数：`default`（APK 初值 0.070，
        不能沿用 scrcpy 的 0.1858）与 `lock_key`（scrcpy 池已冻结 0.1858，
        APK 池要能收敛）。**不传时行为必须与旧版逐字节一致**。
    [4] `apk_audio.ApkStream.fix_header`：提前释放时设备头停在第一块，
        本地镜像要能补成真实长度（否则「留档自证」是假的）。
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

apk_audio = pytest.importorskip("apk_audio")  # tmp/ 下的审计工具，公开仓不导出 tmp ⇒ 跳过
apk_prod = pytest.importorskip("apk_prod")
AR = pytest.importorskip("audio_round")


# ------------------------------------------------------------------ [1] 胶水层

def test_apk_prod_selftest_passes(capsys):
    """胶水层的 23 项自测必须全绿（它们自己能抓到阈值失效/口径错/空判据）。"""
    assert apk_prod._selftest() == 0


def test_apk_dur_is_independent_of_song_length():
    """非 norelease 轮：起点在**点火前**，所以时长不该跟着歌长走。

    变异（把 `apk_dur` 写成 `rec+4`，即 scrcpy 口径）会让 30 秒的歌只录 34 秒 ——
    点火前就吃掉 12 秒，边沿必然被切掉。
    """
    assert apk_prod.apk_dur(30, False) == apk_prod.apk_dur(300, False)
    assert apk_prod.apk_dur(30, False) == apk_prod.APK_DUR_L4C
    # norelease 要覆盖全曲，且夹到设备侧硬上限
    assert apk_prod.apk_dur(154, True) == 174
    assert apk_prod.apk_dur(400, True) == apk_prod.MAX_SECONDS_CAP


def test_lag_pool_is_separate_from_scrcpy():
    assert apk_prod.lag_key() == "anchor_lag_apk_s"
    assert apk_prod.LAG_LOCK_KEY_APK != "lag_lock"
    # 三个池互不相同（scrcpy 文件档 / scrcpy 管道档 / APK）
    assert len({apk_prod.lag_key(), "anchor_lag_s", "anchor_lag_pipe_s"}) == 3


def test_screenrec_dur_adds_lead_only_for_apk():
    """`screenrecord --time-limit` 从**它自己启动**算，APK 档点火晚 ~15 s。

    不补的后果是具体的：Cleyera 最后一个注入事件 131.7 s、录屏 154 s，
    余量 22 s；APK 那 15 s 落差吃掉之后只剩 7 s，且任何抖动都会切尾。
    """
    assert apk_prod.screenrec_dur(154, "scrcpy") == 154
    assert apk_prod.screenrec_dur(154, "apk") == 154 + apk_prod.APK_LEAD_S
    assert apk_prod.APK_LEAD_S >= 12, "落差实测 ~13 s，给少了会切尾"


# -------------------------------------------------------- [2] 命令行 → 配置

def _dry(*args: str) -> subprocess.CompletedProcess:
    """跑 `full_run.py ... dry`，返回进程结果（`dry` 会早退，不碰设备）。"""
    base = ["7.07551", "1e9", "height", "154", "cleyera",
            str(ROOT / "out" / "touches_cleyera.json")]
    return subprocess.run([sys.executable, str(TMP / "full_run.py"), *base,
                           *args, "dry"],
                          # ★ 显式 UTF-8：与子进程同编码（conftest 设了 PYTHONUTF8）。
                          #   `text=True` 默认按本地编码解码 ⇒ 中文断言在 gbk 机器上必红。
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=120)


def test_dry_scrcpy_keeps_old_pool():
    r = _dry("l4c")
    assert r.returncode == 0, r.stderr[-800:]
    assert "src=scrcpy" in r.stdout
    assert "LAG_KEY=anchor_lag_pipe_s" in r.stdout


def test_dry_nopipe_switches_pool():
    r = _dry("l4c", "nopipe")
    assert r.returncode == 0, r.stderr[-800:]
    assert "LAG_KEY=anchor_lag_s" in r.stdout


def test_dry_apk_switches_pool_and_duration():
    r = _dry("l4c", "src=apk")
    assert r.returncode == 0, r.stderr[-800:]
    assert "src=apk" in r.stdout
    # ★ 换池是这一格的核心：用 scrcpy 的 0.1858 当 APK 初值会直接多算 115 ms
    assert "LAG_KEY=anchor_lag_apk_s" in r.stdout
    assert f"录音时长 = {apk_prod.APK_DUR_L4C} s" in r.stdout
    # ★ 2026-10-19：dry 的 L 原来打印代码常量（0.070），而真实生效值是
    #   `audio_calib.json[anchor_lag_apk_s]`（cl7=0.1709 / cl8=0.1994）⇒ 假验证。
    #   判据改成「等于池值（无该键才等于 0.070）」，对池值本身不写死。
    calib_p = TMP / "audio_calib.json"
    pool = json.loads(calib_p.read_text(encoding="utf-8")) if calib_p.exists() else {}
    want = float(pool.get("anchor_lag_apk_s", 0.070))
    assert f"L 生效值 = {want:.4f}" in r.stdout, r.stdout[-400:]
    assert "L 初值" not in r.stdout                       # 旧文案不许回来
    assert "anchor_lag_apk_s" in r.stdout                 # 标出用的是哪本账
    # 录屏时长也必须补上「起探针 → 点火」的落差
    assert f"录屏时长 = {154 + apk_prod.APK_LEAD_S} s" in r.stdout


def test_dry_apk_norelease_covers_song():
    r = _dry("l4c", "norelease", "src=apk")
    assert r.returncode == 0, r.stderr[-800:]
    assert "录音时长 = 174 s" in r.stdout


@pytest.mark.parametrize("args,needle", [
    (("src=apk",), "必须同时带 l4c"),        # 没有 l4c ⇒ 录音没人消费
    (("l4c", "src=apk", "pas"), "互斥"),      # pas 用的是 scrcpy 常驻流
    (("l4c", "src=foo"), "只接受 scrcpy / apk"),
])
def test_dry_rejects_bad_combinations(args, needle):
    r = _dry(*args)
    assert r.returncode != 0, r.stdout[-500:]
    assert needle in (r.stdout + r.stderr)


# --------------------------------------------- [3] L 池的 default / lock_key

def test_anchor_lag_of_separates_pools():
    st = {"anchor_lag_s": 0.1858, "anchor_lag_pipe_s": 0.1858}
    # 池里没有 APK 键 ⇒ 用 APK 自己的默认值，绝不能被 scrcpy 的 0.1858 顶掉
    assert AR.anchor_lag_of(st, default=AR.ANCHOR_LAG_APK_DEFAULT,
                            key="anchor_lag_apk_s") == pytest.approx(0.070)
    assert AR.anchor_lag_of({"anchor_lag_apk_s": 0.12},
                            default=AR.ANCHOR_LAG_APK_DEFAULT,
                            key="anchor_lag_apk_s") == pytest.approx(0.12)


def test_apply_lag_update_default_applies_when_pool_empty():
    """池空时不能回落到 0.1858 —— 那会让首条 APK 样本被拖慢 115 ms。"""
    st = {"anchor_lag_s": 0.1858, "lag_lock": True}
    new = AR.apply_lag_update(st, 0.0, "t", kappa=1.0,
                              key="anchor_lag_apk_s",
                              default=AR.ANCHOR_LAG_APK_DEFAULT,
                              lock_key="lag_lock_apk")
    assert new == pytest.approx(AR.ANCHOR_LAG_APK_DEFAULT)


def test_apply_lag_update_apk_pool_ignores_scrcpy_lock():
    """`lag_lock=true` 冻结的是 scrcpy 池；APK 池必须能收敛。"""
    st = {"anchor_lag_s": 0.1858, "lag_lock": True}
    new = AR.apply_lag_update(st, 120.0, "t", kappa=1.0,
                              key="anchor_lag_apk_s",
                              default=AR.ANCHOR_LAG_APK_DEFAULT,
                              lock_key="lag_lock_apk")
    # κ=1 ⇒ 直接采信 μ 反解：0.070 + 0.120
    assert new == pytest.approx(0.190, abs=1e-6)
    assert st["anchor_lag_apk_s"] == pytest.approx(0.190)
    # scrcpy 池一格没动
    assert st["anchor_lag_s"] == pytest.approx(0.1858)


def test_apply_lag_update_apk_lock_works():
    st = {"lag_lock_apk": True, "anchor_lag_apk_s": 0.10}
    new = AR.apply_lag_update(st, 200.0, "t", kappa=1.0,
                              key="anchor_lag_apk_s",
                              default=AR.ANCHOR_LAG_APK_DEFAULT,
                              lock_key="lag_lock_apk")
    assert new == pytest.approx(0.10)
    assert st["anchor_lag_apk_s"] == pytest.approx(0.10)


def test_apply_lag_update_legacy_behaviour_unchanged():
    """不传新参数时：沿用 0.1858 默认 + 受全局 lag_lock 管（旧行为）。"""
    st = {"lag_lock": True}
    assert AR.apply_lag_update(st, 100.0, "t") == pytest.approx(
        AR.ANCHOR_LAG_DEFAULT)

    st2: dict = {}
    got = AR.apply_lag_update(st2, 100.0, "t", kappa=0.5)
    assert got == pytest.approx(AR.ANCHOR_LAG_DEFAULT + 0.5 * 0.100)
    assert st2["anchor_lag_s"] == pytest.approx(got)


def test_apply_lag_update_kappa_one_is_full_step():
    st: dict = {"anchor_lag_apk_s": 0.070}
    got = AR.apply_lag_update(st, -40.0, "t", kappa=1.0,
                              key="anchor_lag_apk_s",
                              default=AR.ANCHOR_LAG_APK_DEFAULT,
                              lock_key="lag_lock_apk")
    assert got == pytest.approx(0.030)


# ------------------------------------------------------------ [4] wav 头补全

def test_fix_header_rewrites_real_length(tmp_path):
    p = tmp_path / "m.wav"
    # 先造一个**合法**的 48k/16bit/立体声 wav（0.1 s），再把长度字段打回
    # 「设备提前释放时的样子」：RIFF/data 都只写第一块（8192 B 帧）。
    # ★ 不能拿全零字节冒充 wav 头 —— `wave` 会先检查 'WAVE' 与 fmt 块，
    #   那样测出来的是「我的假头不合法」，不是「fix_header 对不对」。
    nframes = 4800
    with wave.open(str(p), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(48000)
        w.writeframes(b"\x01\x02" * 2 * nframes)
    raw = bytearray(p.read_bytes())
    assert len(raw) == apk_audio.WAV_HEADER + nframes * 4
    raw[4:8] = (8192).to_bytes(4, "little")      # 假装头停在第一块
    raw[40:44] = (8192).to_bytes(4, "little")
    p.write_bytes(bytes(raw))
    body = bytes(raw[apk_audio.WAV_HEADER:])
    size = p.stat().st_size

    st = apk_audio.ApkStream(p, prestart=False)
    r = st.fix_header()
    assert r["ok"] and r["changed"]

    fixed = p.read_bytes()
    assert int.from_bytes(fixed[4:8], "little") == size - 8
    assert int.from_bytes(fixed[40:44], "little") == size - 44
    assert fixed[apk_audio.WAV_HEADER:] == body      # PCM 体一字节没动
    # 补完之后 wave 模块必须能整段读出来（这是「留档自证」的最低要求）
    with wave.open(str(p), "rb") as w:
        assert w.getnframes() == nframes
        assert w.readframes(nframes) == body

    # 幂等
    assert st.fix_header()["changed"] is False


def test_fix_header_refuses_non_riff(tmp_path):
    p = tmp_path / "x.wav"
    p.write_bytes(b"XXXX" + b"\x00" * 60)
    st = apk_audio.ApkStream(p, prestart=False)
    assert st.fix_header()["ok"] is False
    assert p.read_bytes() == b"XXXX" + b"\x00" * 60
