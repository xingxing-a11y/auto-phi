"""从设备录音里定位「曲首静音 → 有声」边沿（= 音乐零点 + ≈2.05 s）。

为什么是它
----------
70MF 的参考音频 samples/70mf/music.wav（从 APK 抽出，已核实）：
  * 44100 Hz / 2ch / 16-bit / 146.375 s
  * **前 ≈2.05 s 近似静音**（100 ms RMS = 0.1），2.1 s 处 RMS 陡升到 3412
  * 谱面 offset = 0.0  ⇒ 谱面 t_ms=0 与音频第 0 帧重合
  * 谱面**第一个音符 t_ms = 4843**  ⇒ 边沿之后还有 **2.79 s** 才需要动第一根手指

    音乐零点(host) = 边沿(host) − silence_end_s

★ 边沿判据（2026-10-15，格 C）：候选点（5 ms 帧首超 thr）**不能单独认边沿** ——
  录音里一个瞬时爆音（击打音效/采集毛刺）就能骗过它。实测「明鏡烈火」IN 被
  10.56 / 10.65 s 两个孤立尖峰骗早 **169 ms**（真音起点 10.81 s，互相关独立印证），
  整轮由 φ 砸成 F；历史 90 轮里 |边沿−互相关| > 60 ms 的有 4 轮（r38 第 8 枪的
  −109 ms 也是这个原因，当时误归因为「D_true 漂移」）。
  ⇒ 新增 **`sustain_ms`（默认 `SUSTAIN_MS`）**：候选点后必须再有一段 `sustain_ms`
  的 RMS ≥ thr 才确认，否则否决该候选、往后重找。**ref 侧与录音侧必须同值**（同口径）。

本文件**只做算法**（读 wav / 找边沿），不碰设备。

★ 2026-11-02 从 `tmp/tb_audio_edge.py` 搬进 `pgauto/`：`tools/pack_assets.py` 用它算 1056 首的
  `sil_ref`，那是**生产链路**；而 `export_public.sh` 按设计不导出 `tmp/` ⇒ 公开仓里
  `pack_assets → audio_round → tb_audio_edge` 会在第一首歌就 ImportError（干净环境实测确认）。
  `tmp/tb_audio_edge.py` 现在只剩一个兼容层，供 `tmp/` 下那批审计脚本继续用。

用法::

    ./.venv/Scripts/python.exe pgauto/silref.py --selftest
    ./.venv/Scripts/python.exe pgauto/silref.py --info    # 打印参考音频的静音边界
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
REF70 = ROOT / "samples" / "70mf" / "music.wav"

# 兜底值（实测 2.05 s）；**只在"整首都没有可辨的起音"时**才该用。
# ⚠ 2026-10-26 修：以前只要 `find_silence_end` 返回 None 就套 2.05 s —— 但"返回 None"最常见的
#   原因是**这首歌开头就是响的（没有静音→有声边沿）**，那种情况正确答案是 **0 s**。
#   真机后果：`Retribution Cycleof Redemption` IN 的零点偏 2.05 s、`K` 恒等于 −sil_ref 被 range 闸门拒掉
#   （用户报"K 和 lead 都测不出"），全库共 **60 条**中招。
SILENCE_S_FALLBACK = 2.05


def gate_edge_s(x: np.ndarray, sr: int, thr: float, *, sustain_ms: float = 30.0) -> float:
    """按**端内 OnsetGate 的判据**给出边沿（秒）：首个「连续 `sustain_ms` 帧 ≥ thr」的起点。

    ★ 为什么不要"前置静音"条件：录音里「点 ▶ → 歌曲 t=0」之间必然有 ≥300 ms 静音
      （闸门就是靠它 ARMED 的），所以判**母带自身**时这段静音恒成立 —— 把它写进循环反而会
      让"开头有零散尖峰、随后才持续超阈"的曲子永远触发不了（Chronomia 就是这么被误判成 2.05 s 的）。
    """
    t, rms = rms_windows(x, sr, 10.0)
    if len(rms) == 0:
        return 0.0
    need = max(1, int(round(sustain_ms / 10.0)))
    over = rms >= thr
    # 连续 need 帧都超阈
    run = np.convolve(over.astype(np.int32), np.ones(need, dtype=np.int32), "valid")
    hit = np.where(run >= need)[0]
    return float(t[hit[0]]) if hit.size else SILENCE_S_FALLBACK


def leading_silence_s(x: np.ndarray, sr: int) -> float:
    """这首歌**开头**的静音时长（秒）。0 = 一上来就响。

    ★ 判据必须与**端内相对闸门**同源，否则 `sil_ref` 与"闸门实际触发点"错位 ⇒
      `K = E − S` 恒偏一个 sil_ref 的差量、被 range 闸门永久拒收（2026-10-26 那 60 条的根因）。
      端内 `OnsetGate` 的相对模式：**RMS < `DEFAULT_QUIET_RMS`(300) 算安静**、
      前置 300 ms 安静后，首个 RMS ≥ 300 的帧即边沿 ⇒ 这里就按 300 判（10 ms 窗）。
    """
    t, rms = rms_windows(x, sr, 10.0)
    if len(rms) == 0:
        return 0.0
    for i, v in enumerate(rms):
        if v >= 300.0:
            return float(t[i])
    # 整首都没有 ≥300 的帧：极轻/全静音 ⇒ 退回经验值
    return SILENCE_S_FALLBACK

# ★ 边沿判据的「持续超阈确认窗」（2026-10-15 格 C，见文件头）：候选点后必须有这么长的
#   一段 RMS ≥ thr 才算边沿，用以否决瞬时尖峰。0 = 关闭（退回旧判据）。
#   取值 30 ms：实测 20/30/40/60 ms 在 meikyo 上结论一致；30 对第二尖峰的否决余量约 19%。
SUSTAIN_MS = 30.0
# ★ `tail_only`（在线到点兜底）下允许的最小可用窗：只在候选点后已有这么多音频时
#   才敢做「不完整窗」判断，并要求该窗**末尾 5 ms 仍 ≥ thr**（能量没有在衰减
#   ⇒ 不是尖峰尾巴）。8 ms 的选择：低于一个 20 ms 包络窗的粒度，只作为「有没有料」的门。
TAIL_MIN_MS = 8.0
TAIL_EDGE_MS = 5.0


# --------------------------------------------------------------------------- #
# 读 wav（兼容 scrcpy 边写边读的半截文件）
# --------------------------------------------------------------------------- #
def load_pcm(path: Path | str) -> tuple[np.ndarray, int, dict]:
    """读 wav → (float32 单声道, 采样率, 头信息)。

    正在写入的 scrcpy 录音有两种形态：头已写（RIFF/`data` 齐全，但尺寸是占位值）
    或头还没写（整个文件就是裸 PCM）。两种都认。
    """
    raw = Path(path).read_bytes()
    sr, nch, width, off = 48000, 2, 2, 0
    if raw[:4] == b"RIFF":
        i = raw.find(b"fmt ", 12)
        if i != -1 and len(raw) >= i + 24:
            nch = int.from_bytes(raw[i + 10:i + 12], "little") or 2
            sr = int.from_bytes(raw[i + 12:i + 14], "little") or 48000
            width = (int.from_bytes(raw[i + 22:i + 24], "little") or 16) // 8
        j = raw.find(b"data", 12)
        if j != -1:
            off = j + 8
    info = {"sr": sr, "nch": nch, "width": width, "data_off": off,
            "bytes": len(raw), "header": raw[:4] == b"RIFF"}

    pcm = raw[off:]
    if width == 2:
        pcm = pcm[: len(pcm) // 2 * 2]
        a = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    elif width == 4:
        pcm = pcm[: len(pcm) // 4 * 4]
        a = np.frombuffer(pcm, dtype=np.int32).astype(np.float32) / 65536.0
    else:
        raise ValueError(f"不支持的位宽 {width * 8} bit（{path}）")

    if nch > 1:
        a = a[: len(a) // nch * nch].reshape(-1, nch).mean(axis=1)
    return a, sr, info


# --------------------------------------------------------------------------- #
# 包络与边沿
# --------------------------------------------------------------------------- #
def rms_windows(x: np.ndarray, sr: int, win_ms: float = 20.0) -> tuple[np.ndarray, np.ndarray]:
    """返回 (窗起点秒, 窗 RMS)；整段降成 hop=win_ms 的窗。"""
    hop = max(1, int(round(sr * win_ms / 1000.0)))
    n = len(x) // hop
    if n == 0:
        return np.zeros(0), np.zeros(0)
    y = x[: n * hop].astype(np.float64).reshape(n, hop)
    return np.arange(n) * (hop / sr), np.sqrt((y * y).mean(axis=1))


def _first_loud_sample(x: np.ndarray, sr: int, from_sample: int, thr: float,
                       hop_ms: float = 5.0, max_s: float = 3.0) -> int | None:
    """从 from_sample 起按 hop 找第一个「5 ms RMS > thr」的样本位置。"""
    hop = max(1, int(round(sr * hop_ms / 1000.0)))
    p = max(0, from_sample)
    stop = p + int(max_s * sr)
    while p < min(len(x), stop):
        seg = x[p:p + hop].astype(np.float64)
        if len(seg) and float(np.sqrt((seg * seg).mean())) > thr:
            return p
        p += hop
    return None


def find_silence_end(x: np.ndarray, sr: int, *, after_s: float = 0.0,
                     floor_rel: float = 0.02, abs_floor: float = 80.0,
                     min_silence_s: float = 1.0, win_ms: float = 20.0,
                     refine_ms: float = 5.0, thr: float | None = None,
                     sustain_ms: float = SUSTAIN_MS,
                     tail_only: bool = False) -> dict | None:
    """找 after_s 之后**第一个**「够长的静音段」的结束点（秒）。

    口径（对 70MF 这类「数字静音 → 硬阶跃」的曲首很稳，阈值取 100 或 1000 结果只差 ~5 ms）：

    * `level_ref` = 被分析那一段 20 ms RMS 的 90 分位，代表「有声」电平；
    * 判定阈值 `thr = max(abs_floor, floor_rel × level_ref)`，**静音段与边沿共用同一个 thr**；
    * 静音段必须在 `after_s` 之后**开始**、长度 ≥ `min_silence_s`、且后面确实又有声
      （末尾那段静音不算 —— 歌曲尾部也会静音）；
    * 结束点精修到 `refine_ms`：取静音段后第一个 5 ms RMS > thr 的样本位置。
    * ★ **持续确认（`sustain_ms`，2026-10-15 格 C）**：候选点后必须再有一整段
      `sustain_ms` 的 RMS ≥ thr 才认；否则否决该候选、从「候选点 + refine_ms」重找。
      这是为了抗**瞬时尖峰**（击打音效/采集毛刺）—— 见文件头。`sustain_ms=0` 关闭。
      前瞻样本不足时：
        * `tail_only=False`（默认，在线主判据）⇒ 返回 `None`（= 「还判不了」，
          调用方下一轮再试 / 到点走兜底）；
        * `tail_only=True`（在线**到点**兜底）⇒ 允许用「已有的那一段」（≥ `TAIL_MIN_MS`）
          判定，但额外要求该段**末尾 `TAIL_EDGE_MS` 仍 ≥ thr**（能量未在衰减 ⇒ 不是尖峰尾巴）。

    ★ `thr` 显式给定（≥0）时**直接使用**，跳过 `level_ref` 推算。
    这是修一个真实 bug 的关键：`level_ref` 是「被分析段」的统计量 ——
    参考音频按整首算、录音按窗口算，两段的 90 分位天然不同 ⇒ 同一首歌的
    「边沿」在两侧被定义在不同位置。对突跳歌（70MF）无害（阈值不敏感），
    对渐入歌（ZABANIYA 159→175→…）会放大成 ~230 ms 系统偏差。
    修法：让参考侧与录音侧**共用同一个 thr**（由 `reference_silence()` 给出）。
    """
    t, r = rms_windows(x, sr, win_ms)
    if len(r) < 3:
        return None
    level = float(np.percentile(r, 90))
    thr = float(thr) if thr is not None else max(abs_floor, floor_rel * level)

    win_s = win_ms / 1000.0
    need = max(1, int(round(min_silence_s / win_s)))
    quiet = r < thr
    n_sus = max(0, int(round(sustain_ms / 1000.0 * sr)))
    n_min = max(1, int(round(TAIL_MIN_MS / 1000.0 * sr))) if tail_only else 0
    n_tail = max(1, int(round(TAIL_EDGE_MS / 1000.0 * sr)))
    n_hop = max(1, int(round(refine_ms / 1000.0 * sr)))
    i = max(0, int(after_s / win_s))
    while i < len(quiet):
        if not quiet[i]:
            i += 1
            continue
        j = i
        while j < len(quiet) and quiet[j]:
            j += 1
        if j - i >= need and j < len(quiet):
            start_sample = int(round(i * win_s * sr))
            cursor = int(round(j * win_s * sr))
            # ★ 候选点须经「持续超阈」确认（sustain_ms），否则否决、往后重找 ——
            #   旧版在这里直接返回，于是被单个瞬时尖峰骗走（格 C 根因）。
            while True:
                edge = _first_loud_sample(x, sr, cursor, thr, hop_ms=refine_ms)
                if edge is None:
                    break
                if n_sus <= 0:
                    n_use, conf = 0, "off"
                else:
                    avail = len(x) - edge
                    if avail >= n_sus:
                        n_use, conf = n_sus, "full"
                    elif tail_only and avail >= n_min:
                        n_use, conf = avail, "partial"
                    else:
                        # 前瞻样本不够 ⇒ 判不了（在线语义：下轮再试 / 到点落兜底）
                        return None
                ok = True
                if n_use > 0:
                    seg_use = x[edge:edge + n_use].astype(np.float64)
                    ok = float(np.sqrt((seg_use * seg_use).mean())) >= thr
                    if ok and conf == "partial":
                        seg_t = x[edge + n_use - n_tail:edge + n_use].astype(np.float64)
                        ok = float(np.sqrt((seg_t * seg_t).mean())) >= thr
                if ok:
                    return {
                        "edge_s": edge / sr,
                        "run_start_s": start_sample / sr,
                        "run_dur_s": (edge / sr) - (start_sample / sr),
                        "level_ref": level,
                        "thr": thr,
                        "win_ms": win_ms,
                        "refine_ms": refine_ms,
                        "sustain_ms": sustain_ms,
                        "confirmed": conf,
                    }
                cursor = edge + n_hop
            i = j
            continue
        i = j
    return None


_REF_CACHE: dict[str, dict] = {}


def reference_silence(ref: Path | str = REF70, *,
                      sustain_ms: float = SUSTAIN_MS) -> dict:
    """参考音频自身的「静音 → 有声」边界，**连同判定阈值一起**返回。

    返回 ``{"sil_ref_s": float, "thr": float | None, "level_ref": float | None,
    "sustain_ms": float}``。

    ★ `thr` 必须被录音侧复用（``find_silence_end(..., thr=thr)``）：
    否则录音侧会用它**自己窗口**的 `level_ref` 现算阈值，两侧口径不一致 ——
    对突跳歌曲（70MF）无所谓，对渐入歌曲（ZABANIYA 159→175→…）会产生
    ~230 ms 的系统偏差，且逐轮抖动（窗口电平随 t0 与前导内容变）。

    ★ `sustain_ms` 同理必须两侧同值（2026-10-15 格 C）：它是判据的一部分，
    参考侧与录音侧口径不一致 = 边沿被定义在不同位置。默认两侧都取 `SUSTAIN_MS`。
    """
    key = f"{ref}|{sustain_ms}"
    if key in _REF_CACHE:
        return _REF_CACHE[key]
    x, sr, _ = load_pcm(ref)
    r = find_silence_end(x, sr, min_silence_s=0.5, sustain_ms=sustain_ms)
    if r:
        thr = float(r["thr"])
        # ★★ 2026-10-26：**sil_ref 必须等于"端内闸门实际会触发的那一帧"**，否则
        #    `K = E − S` 会恒差一个 (真边沿 − sil_ref)，表现为 K 被 range 闸门拒收。
        #    实测差异来源：打包时 `find_silence_end(min_silence_s=0.5)` 要求**前面已有 0.5 s 静音**，
        #    而端内 `OnsetGate` 只要求 **300 ms**（`DEFAULT_QUIET_MS`）⇒ 开头有"高于阈值的前奏"时，
        #    打包端会挑一个更晚的边沿（例：introduction 报 1.305 s，闸门其实在 0.43 s 就触发 ⇒ K 偏 −875 ms）。
        #    所以这里按闸门判据重算一次边沿（同阈值、300 ms 静音、30 ms 持续）。
        edge = gate_edge_s(x, sr, thr, sustain_ms=sustain_ms)
        # ★ 只有**两者差得远**（>200 ms）才改用闸门口径：`find_silence_end(min_silence_s=0.5)`
        #   在绝大多数曲子上与闸门一致（差几毫秒，且与真机记录吻合），此时必须保留原值
        #   —— 否则会把 `bounded_in` 这类有真机真值的曲子改动 5 ms，打包自测直接红。
        if abs(edge - float(r["edge_s"])) <= 0.1:
            edge = float(r["edge_s"])
        d = {"sil_ref_s": float(edge), "thr": thr,
             "level_ref": float(r["level_ref"]), "sustain_ms": float(sustain_ms),
             "confirmed": r.get("confirmed")}
    else:
        # ★ 2026-10-26：没有"静音→有声"边沿 ⇒ 最常见的原因是**开头就是响的**（sil_ref 应为 0），
        #   不是"测不出来"。用 leading_silence_s() 给出真实值；只有"整首都没有可辨起音"才用经验值。
        lead_s = leading_silence_s(x, sr)
        d = {"sil_ref_s": float(lead_s), "thr": None, "level_ref": None,
             "sustain_ms": float(sustain_ms), "confirmed": False}
    _REF_CACHE[key] = d
    return d


def silence_end_of_reference(ref: Path | str = REF70, *,
                             sustain_ms: float = SUSTAIN_MS) -> float:
    """参考音频自身的「静音 → 有声」边界（秒）。这就是要实现场测的常量。"""
    return reference_silence(ref, sustain_ms=sustain_ms)["sil_ref_s"]


# --------------------------------------------------------------------------- #
# 自测
# --------------------------------------------------------------------------- #
def selftest(ref: Path | str = REF70, lead: Path | str | None = None) -> int:
    from tb_audio_cap import read_wav_mono, resample_linear, _impulses

    ref_x, sr_ref = read_wav_mono(ref)
    sr = 48000
    body = resample_linear(ref_x, sr_ref, sr)[: int(60 * sr)]
    sil = silence_end_of_reference(ref)
    mus_loud = float(np.percentile(rms_windows(body, sr)[1], 90))
    print(f"[selftest] 参考 {Path(ref).name}  静音边界 = {sil:.4f} s"
          f"  （45000 窗 90 分位电平 {mus_loud:.0f}）")

    lead_src = None
    lp = Path(lead or (ROOT / "tmp" / "_rrhar_48k.wav"))
    if lp.exists():
        lx, lsr = read_wav_mono(lp)
        lead_src = resample_linear(lx, lsr, sr)
        print(f"[selftest] 前导 BGM = {lp.name}（另一首歌，真实干扰）")
    else:
        print("[selftest] 无前导 BGM 文件 —— 用低通白噪代替")

    # 真实录音的构造：前导 BGM 播一段时间 → 游戏切到歌曲（歌曲自带 2.15 s 静音）→ 有声
    cases = [
        ("① 基线",            {}),
        ("② 前导很响 ×1.6",    {"lead_gain": 1.6}),
        ("③ 前导很轻 ×0.25",   {"lead_gain": 0.25}),
        ("④ 底噪 50 dB",       {"snr_db": 50, "dither": True}),
        ("⑤ 击打音效",         {"hits": True}),
        ("⑥ 全叠加 40 dB",     {"snr_db": 40, "hits": True, "lead_gain": 1.4, "dither": True}),
        ("⑦ 前导 8 s",         {"lead_s": 8.0}),
        ("⑧ 极低音量 ×0.08",   {"gain": 0.08, "snr_db": 50, "dither": True}),
    ]

    print(f"\n{'场景':<20}{'真边沿':>9}{'测得':>9}{'误差ms':>9}{'静音段':>9}{'thr':>8}")
    print("-" * 66)
    errs = []
    for name, kw in cases:
        kw = dict(kw)
        lead_gain = kw.pop("lead_gain", 1.0)
        lead_s = kw.pop("lead_s", 3.0)
        rng = np.random.default_rng(abs(hash(name)) & 0xFFFF)
        true_edge = lead_s + sil

        n_lead = int(lead_s * sr)
        if lead_src is not None and len(lead_src) > n_lead:
            st = int(rng.integers(0, len(lead_src) - n_lead))
            lead = lead_src[st:st + n_lead].copy() * lead_gain
        else:
            lead = rng.normal(0, 3000.0, n_lead).astype(np.float32)
            lead = lead.reshape(-1, 32).mean(axis=1).repeat(32)[:n_lead]

        rec = np.concatenate([lead, body]).astype(np.float32)
        if kw.get("hits"):
            rec = rec + _impulses(len(rec), sr, rng)
        if kw.get("gain"):
            rec = rec * kw["gain"]
        if kw.get("dither"):      # 静音段也带 ±3 LSB 抖动（真实采集不会正好是 0）
            rec = rec + rng.integers(-3, 4, len(rec)).astype(np.float32)
        if kw.get("snr_db"):
            amp = mus_loud * (10 ** (-kw["snr_db"] / 20.0))
            rec = rec + rng.normal(0, amp, len(rec)).astype(np.float32)

        got = find_silence_end(rec, sr)
        if not got:
            print(f"{name:<20}{true_edge:9.3f}   FAIL（未检出）")
            errs.append(float("nan"))
            continue
        e = (got["edge_s"] - true_edge) * 1000.0
        errs.append(e)
        print(f"{name:<20}{true_edge:9.3f}{got['edge_s']:9.3f}{e:+9.1f}"
              f"{got['run_dur_s']:8.2f}s{got['thr']:8.0f}")

    ok = [e for e in errs if np.isfinite(e)]
    print("-" * 66)
    if not ok:
        return 1
    mx = max(abs(e) for e in ok)
    print(f"max |误差| = {mx:.1f} ms（Perfect 窗 ±80 ms；边沿到首音符还有 2.69 s 余量）")
    print("判定: " + ("✅ 通过" if mx < 20.0 else "❌ 未达标"))
    return 0 if mx < 20.0 else 1


def selftest_gradin(sr: int = 48000) -> int:
    """回归测试：**渐入**歌曲上「参考/录音必须共用同一阈值」的必要性。

    背景（2026-10-12 真机事故根因）：
      `find_silence_end` 的判定阈值是 `max(abs_floor, floor_rel × level_ref)`，
      而 `level_ref` 是**被分析那一段**的 90 分位 RMS。参考音频按整首算、录音
      按窗口算 ⇒ 两段电平不同 ⇒ 同一首歌的「边沿」被定义在不同位置。
      对突跳歌曲（70MF）阈值不敏感，对**渐入**歌曲（ZABANIYA 159→175→192→…）
      会放大成 ~230 ms 系统偏差，且随窗口电平逐轮抖动。

    本测试构造与 ZABANIYA 同型的信号：长数字静音 → 缓慢渐入（1.3 s 才到满幅）。
      * ref  = 整首（含后面 12 s 响段）⇒ level_ref 高 ⇒ thr 高
      * rec  = 只录到开头（没录到响段）+ 一段前置响声 ⇒ 窗口 level_ref 低 ⇒ 旧逻辑落到地板 80
    断言：同阈值口径下 Δ 误差 < 35 ms；并打印旧口径的偏差作对照。
    """
    rng = np.random.default_rng(3)
    n_sil, n_ramp, n_loud = int(3.0 * sr), int(1.6 * sr), int(12.0 * sr)
    ramp = (rng.normal(0, 1, n_ramp) * np.linspace(0.0, 1200.0, n_ramp)).astype(np.float32)
    loud = rng.normal(0, 9000.0, n_loud).astype(np.float32)     # 「副歌」远晚于开头
    ref = np.concatenate([np.zeros(n_sil, np.float32), ramp, loud])
    sil_true = float(find_silence_end(ref, sr, min_silence_s=0.5)["edge_s"])

    pre = rng.normal(0, 3000.0, int(2.0 * sr)).astype(np.float32)   # 前置 BGM
    gap = np.zeros(int(4.0 * sr), np.float32)                       # 曲前数值静音
    body = ref[: int(n_sil + n_ramp)]                               # ★ 只录到渐入结束（不含副歌）
    rec = np.concatenate([pre, gap, body])
    delta_true = (len(pre) + len(gap)) / sr      # 录音里「音乐第 0 样本」的位置

    rs = find_silence_end(ref, sr, min_silence_s=0.5)
    sil_ref, thr_ref = float(rs["edge_s"]), float(rs["thr"])
    after_s = 5.5                        # 模拟 after_s（跳过前置响声）

    def edge_with(thr):
        seg = rec[int(after_s * sr):]
        g = find_silence_end(seg, sr, min_silence_s=1.0, thr=thr)
        return (after_s + g["edge_s"]) if g else float("nan")

    e_bug = edge_with(None)                 # 旧逻辑：录音窗口自算 level_ref
    e_fix = edge_with(thr_ref)              # 修法：复用参考侧阈值
    d_bug = (e_bug - sil_ref - delta_true) * 1000.0
    d_fix = (e_fix - sil_ref - delta_true) * 1000.0

    print(f"[gradin] 参考 sil_ref = {sil_ref:.4f}s (thr={thr_ref:.1f})   "
          f"录音真值 Δ = {delta_true:.4f}s   after_s = {after_s}s")
    print(f"[gradin] 旧口径（各自算 level_ref）edge = {e_bug:.4f}  ⇒ Δ 误差 {d_bug:+7.1f} ms")
    print(f"[gradin] 同阈值（复用 ref thr）  edge = {e_fix:.4f}  ⇒ Δ 误差 {d_fix:+7.1f} ms")
    if abs(d_fix) < 35.0:
        print("[gradin] ✅ 通过（同阈值口径把渐入偏差压进 Perfect 窗；旧口径偏差见上）")
        return 0
    print("[gradin] ❌ 未达标：同阈值口径误差仍 > 35 ms")
    return 1


def selftest_spike(sr: int = 48000) -> int:
    """回归测试：**孤立尖峰不得骗走边沿**（2026-10-15 格 C 的判据验收）。

    构造与「明鏡烈火」实测同型的信号：曲前数字静音里塞两个**孤立击打尖峰**
    （各 ~8 ms，随后立刻回落），真实音乐起点在更后面。
      * 旧判据（`sustain_ms=0`）⇒ 锁在最前面的尖峰上，**系统性偏早**；
      * 新判据（`sustain_ms=SUSTAIN_MS`）⇒ 否决尖峰、落到真实音乐起点；
      * `tail_only=True`（在线到点兜底）⇒ 即使只录到音乐起点后十几 ms，
        也能确认（"partial"），但不会把尖峰尾巴当成起点。
    """
    rng = np.random.default_rng(20261015)
    n_mus = int(10.0 * sr)
    music = rng.normal(0, 3000.0, n_mus).astype(np.float32)
    # 参考侧 = [2.0 s 静音][音乐]（生产里 ref 就是整首歌；thr 由它给出）
    ref = np.concatenate([np.zeros(int(2.0 * sr), np.float32), music])
    ref_hit = find_silence_end(ref, sr, min_silence_s=0.5)
    thr = float(ref_hit["thr"])
    sil_ref = float(ref_hit["edge_s"])
    # ★ 尖峰的振幅按 thr 定，必须同时满足三条（才等价于实测里骗过旧判据的那种尖峰）：
    #     · 20 ms 窗 > thr（幅 0.316A）⇒ **打断静音段**，旧判据的「段尾」就落在尖峰上；
    #     · 5 ms 窗  > thr（幅 0.632A）⇒ 精修也锁在尖峰上；
    #     · 30 ms 窗 < thr（幅 0.258A）⇒ 新判据能把它否决掉。
    #   ⇒ A ∈ [3.16, 3.87)·thr，取 A = 3.5·thr（两条余量各 ~10%）。
    #   （meikyo 实测：thr 298.9 / 尖峰 5 ms 311.7 / 其后 30 ms 188.5 —— 同型。）
    amp = 3.5 * thr
    true_edge = 3.0
    rec = np.concatenate([np.zeros(int(true_edge * sr), np.float32), music])
    rec = rec + rng.normal(0, 0.15 * thr, len(rec)).astype(np.float32)   # 录音底噪（仍 < thr）
    spike_at = [int((true_edge - 0.30) * sr), int((true_edge - 0.15) * sr)]
    # 突发段的 RMS **精确归一化**（否则随机实现会让「20 ms 窗 > thr」与「30 ms 窗 < thr」
    # 这两条互斥条件同时落空 —— 这俩的比值恒为 sqrt(30/20)=1.22，余量本来就窄）。
    burst = rng.normal(0, 1.0, 96)
    burst = (burst / float(np.sqrt((burst * burst).mean())) * amp).astype(np.float32)
    for p in spike_at:
        rec[p:p + 96] += burst
    rec = rec + rng.integers(-3, 4, len(rec)).astype(np.float32)   # ±3 LSB 抖动
    print(f"[spike] ref sil_ref = {sil_ref:.3f}s  thr = {thr:.1f}  尖峰振幅 = {amp:.0f}"
          f"（20 ms 窗 ≈ {0.316 * amp:.0f} / 5 ms 窗 ≈ {0.632 * amp:.0f} > thr；"
          f"30 ms 窗 ≈ {0.258 * amp:.0f} < thr）")
    print(f"[spike] 尖峰位 {[round(p / sr, 3) for p in spike_at]}（皆为孤立、后接静音）；"
          f"真起点 {true_edge:.3f}s")

    seg = rec[int(1.0 * sr):]                               # 模拟 after_s 之后的窗口
    after = 1.0
    ok = True
    rows = [("v1（sustain=0，旧判据）", dict(sustain_ms=0.0)),
            (f"v2（sustain={SUSTAIN_MS:g}ms，新判据）", dict(sustain_ms=SUSTAIN_MS))]
    print(f"\n{'判据':<32}{'edge(s)':>10}{'误差ms':>9}{'确认档':>9}")
    print("-" * 62)
    for name, kw in rows:
        g = find_silence_end(seg, sr, min_silence_s=1.0, thr=thr, **kw)
        if not g:
            print(f"{name:<32}   FAIL（未检出）")
            ok = False
            continue
        e = g["edge_s"] + after - true_edge
        print(f"{name:<32}{g['edge_s'] + after:10.3f}{e * 1000:+9.1f}"
              f"{g.get('confirmed', '-'):>9}")
        if name.startswith("v1") and e > -0.10:
            ok = False                     # v1 必须被尖峰骗早 ≥100 ms（证伪该 bug 存在）
        if name.startswith("v2") and abs(e) > 0.020:
            ok = False                     # v2 必须落在真起点 ±20 ms 内

    # 在线到点兜底：只录到「真起点 + 12 ms」
    cut = int((true_edge + 0.012) * sr) - int(1.0 * sr)
    short = seg[:cut]
    g_strict = find_silence_end(short, sr, min_silence_s=1.0, thr=thr)
    g_tail = find_silence_end(short, sr, min_silence_s=1.0, thr=thr, tail_only=True)
    es = None if not g_strict else g_strict["edge_s"] + after
    et = None if not g_tail else g_tail["edge_s"] + after
    print(f"\n[到点兜底] 只录到真起点+12ms："
          f"严格判据 ⇒ {es if es is None else f'{es:.3f}'}"
          f"（{'None ⇒ 等到点' if es is None else '不该有值'}）｜"
          f"tail_only ⇒ {et if et is None else f'{et:.3f}'}"
          f"（{'-' if not g_tail else g_tail.get('confirmed')}）")
    if g_strict is not None or not g_tail or abs(et - true_edge) > 0.020:
        ok = False

    print("判定: " + ("✅ 通过" if ok else "❌ 未达标"))
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="设备录音的「曲首静音→有声」边沿检测")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--selftest-gradin", action="store_true",
                    help="渐入歌曲的阈值口径回归测试")
    ap.add_argument("--selftest-spike", action="store_true",
                    help="孤立尖峰不得骗走边沿（格 C 判据验收）")
    ap.add_argument("--info", action="store_true", help="打印参考音频的静音边界")
    ap.add_argument("--edge", type=Path, help="对某个 wav 直接跑边沿检测")
    ap.add_argument("--ref", type=Path, default=REF70)
    ap.add_argument("--lead", type=Path, default=None)
    args = ap.parse_args()

    if args.selftest_gradin:
        return selftest_gradin()
    if args.selftest_spike:
        return selftest_spike()
    if args.info:
        print(json.dumps({
            "ref": str(args.ref),
            "silence_end_s": round(silence_end_of_reference(args.ref), 4),
        }, ensure_ascii=False, indent=2))
        return 0
    if args.edge:
        x, sr, info = load_pcm(args.edge)
        got = find_silence_end(x, sr, min_silence_s=0.8)
        print(json.dumps({"file": str(args.edge), "samples": len(x), "info": info,
                          "edge": got}, ensure_ascii=False, indent=2))
        return 0 if got else 1
    return selftest(args.ref, args.lead)


if __name__ == "__main__":
    sys.exit(main())
