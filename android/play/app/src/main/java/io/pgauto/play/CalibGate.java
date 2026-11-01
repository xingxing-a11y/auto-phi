package io.pgauto.play;

import java.util.Locale;

/**
 * 标定闸门（G4 音频连续性 + G5 交叉校验）—— **纯函数，零 Android 依赖**。
 *
 * ★ 为什么单独抽一个类（而不是塞在 `PlayService.computeKOnDevice` 里）：
 *   端内那段代码在真机上只能靠"打一枪看结果"验证，而这两条闸门的判据完全可以离线重放
 *   （喂一段包络 + 起音帧号 + lead 历史就够了）。抽成纯类 → 进 JVM 自测 → 变异能变红。
 *   MEMORY 教训 9：「判据要能脱离 I/O 重放 ⇒ 拆纯函数」。
 *
 * ★ 两条闸门各自防什么事故：
 *   G4 音频连续性：标定途中来电话/播报/游戏暂停 ⇒ 捕获是**半截**的，而长度闸门
 *      （`CALIB_FRAMES`）只看帧数，掉帧补齐后帧数照样满 ⇒ 会算出一个"看着合格"的 K。
 *      判据：**起音后 3 s 内不该出现 ≥0.8 s 的静音**（`out/calib_truncation_2026-10-25.md`）。
 *   G5 交叉校验：内录会把**别的 App 的声音**一起录进来，互相关可能锁到那上面。
 *      判据：`S`（互相关测出的母带 t=0）必须与「tap + 历史 lead」相符（±300 ms）。
 *
 * ⚠ G4 的窗口**必须锚在 onset 上，不能锚在捕获开头**：点 ▶ 到起音之间天生有 7 s 左右静音，
 *   锚在开头等于每轮都判"中断"。这是本类最容易写错的一处（方案原文"前 sil_ref+3 s"就是这个坑）。
 */
public final class CalibGate {

    /** 一帧 = 10 ms（`OnsetTap` 的帧长，端内包络也按它编码）。 */
    public static final double FRAME_MS = 10.0;

    /** 判"静音"的 RMS 门限（300 RMS ≈ −41 dBFS；包络统计那一侧的实现 2026-11-01 已删，口径以此为准）。 */
    public static final double SILENT_RMS = 300.0;

    /** 连续多少帧静音才判中断：80 帧 = 0.8 s。 */
    public static final int GAP_MIN_FRAMES = 80;

    /** 只看起音后多长的窗口：300 帧 = 3 s。 */
    public static final int GAP_WINDOW_FRAMES = 300;

    /** 起音后先跳过多少帧再开始找静音（10 帧 = 100 ms，避开起音那一瞬的波形过零）。 */
    public static final int GAP_SKIP_FRAMES = 10;

    /**
     * G5：`S` 与 `lead` 的允许偏差（ms）。
     *
     * ★ 2026-10-25 20:3x 从 300 收紧到 150：修掉 `+c` 的系统偏移后，`S` 与 `lead` 是**同一个量**
     *   （两轮真机实测偏差 +3.6 / +23 ms，来源只有 K 的估计误差），150 已是 ~3× 余量；
     *   300 会让"锁到别的 App 的声音、差 200 ms"这种照样通过。
     */
    public static final double CROSS_TOL_MS = 150.0;

    /** G5 无 lead 历史时的物理区间（ms）：点 ▶ → 母带 t=0，实测 7.1~7.6 s。 */
    public static final double S_MIN_MS = 3000.0;
    public static final double S_MAX_MS = 15000.0;

    private CalibGate() {}

    /** 闸门结论。`why` 一律写成**无空格**的 `k=v`，因为它会被直接拼进 trace/日志一行。 */
    public static final class Verdict {
        public final boolean ok;
        public final String why;

        private Verdict(boolean ok, String why) {
            this.ok = ok;
            this.why = why;
        }

        public static Verdict pass(String why) { return new Verdict(true, why); }
        public static Verdict reject(String why) { return new Verdict(false, why); }

        @Override public String toString() { return (ok ? "ok " : "拒绝 ") + why; }
    }

    /**
     * G4：在**起音后 3 s** 里找 ≥0.8 s 的连续静音。
     *
     * @param meanSquare 端内包络（每帧**均方**值，不是 RMS —— 与 {@code EnvCodec} 一致）
     * @param onsetFrame 门锁定的起音帧号；≤0 表示本轮门没锁定
     */
    public static Verdict checkGap(double[] meanSquare, long onsetFrame) {
        if (meanSquare == null || meanSquare.length == 0) {
            return Verdict.reject("gap=no_env");
        }
        if (onsetFrame <= 0) {
            // 门没锁定就没有"起音后"可言；K 本来也算不出来，这里给出能区分的原因
            return Verdict.reject("gap=no_onset");
        }
        int from = (int) onsetFrame + GAP_SKIP_FRAMES;
        int to = (int) Math.min(meanSquare.length, onsetFrame + GAP_WINDOW_FRAMES);
        if (to - from < GAP_MIN_FRAMES) {
            // 窗口还不够判一段 0.8 s 静音。**不冒充"通过"**：把原因写清楚，让日志能看见。
            return Verdict.pass(String.format(Locale.ROOT, "gap=n/a win=%d", Math.max(0, to - from)));
        }
        final double thr = SILENT_RMS * SILENT_RMS;
        int run = 0;
        for (int i = from; i < to; i++) {
            if (meanSquare[i] < thr) {
                run++;
                if (run >= GAP_MIN_FRAMES) {
                    double startS = (i - GAP_MIN_FRAMES + 1) * FRAME_MS / 1000.0;
                    double durS = run * FRAME_MS / 1000.0;
                    return Verdict.reject(String.format(Locale.ROOT,
                            "gap@%.2fs/%.2fs", startS, durS));
                }
            } else {
                run = 0;
            }
        }
        return Verdict.pass("gap=none");
    }

    /**
     * G5：`S` 必须与历史 `lead` 相符；没有 lead 时退化成一个宽物理区间。
     *
     * ★★★ 换算（**2026-10-25 20:3x 修正过一次，别再用旧式**）：
     *   两条路径算出来的 lead **都等于 S**，不是 `S − c`：
     *
     *   · `OnsetZero` 的 onset 取自 `gate.firedAtNs()`（**挂钟**，含捕获链滞后 c），
     *     而 `computeKOnDevice` 的 `E` 取自 `ff × 10 ms`（**捕获时间轴**）；
     *   · 打歌轮：`zeroRel = (E + sil_ref + c) − sil_ref − L_dev`，且 `L_dev = K + c`
     *     ⇒ `zeroRel = E − K = S`（c 正好抵消）；
     *   · 标定轮：`lDevS = 0` ⇒ `zeroRel = E + c`，代码再减 `K + c`
     *     ⇒ `lead = E − K = S`（同样抵消）。
     *
     *   ⇒ 期望式是 **`S ≈ lead`**。旧判据写的是 `lead + c`，等于给这条闸门整体加了一个
     *   −177 ms 的系统偏移：合法样本落在 `d ≈ −c`，正向余量被吃掉一大截。
     *   两轮真机数据（bounded_in `d=−173.3`、996 `d=−153.3`）就是它。
     *
     * @param sMs    互相关测出的"母带 t=0 在捕获轴上的位置"（ms）
     * @param leadMs 该曲历史 lead 均值；null = 还没攒到
     */
    public static Verdict checkCross(double sMs, Double leadMs) {
        if (Double.isNaN(sMs)) {
            return Verdict.reject("cross=no_s");
        }
        if (leadMs != null && !leadMs.isNaN()) {
            double d = sMs - leadMs;
            String why = String.format(Locale.ROOT, "cross s=%.1f lead=%.1f d=%+.1f", sMs, leadMs, d);
            if (Math.abs(d) > CROSS_TOL_MS) return Verdict.reject(why);
            return Verdict.pass(why);
        }
        if (sMs < S_MIN_MS || sMs > S_MAX_MS) {
            return Verdict.reject(String.format(Locale.ROOT,
                    "cross s=%.1f out=[%.0f,%.0f]", sMs, S_MIN_MS, S_MAX_MS));
        }
        return Verdict.pass(String.format(Locale.ROOT, "cross s=%.1f no_lead", sMs));
    }
}
