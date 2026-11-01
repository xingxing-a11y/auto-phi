package io.pgauto.play;

import java.util.Locale;

/**
 * 包络（10 ms 帧 RMS）的**编解码 + 落盘格式** —— 纯 Java，零 Android 依赖（PC 上 JVM 可测）。
 *
 * 为什么要有它（2026-10-25，方案 `plans/2026-10-25-env-correlation.md` 的 S1）：
 * 端内每轮的捕获包络只在内存里存在几百毫秒，打完就没了；而「每曲的 `L_dev`」只能靠
 * 拿这段包络与母带做互相关才能**量**出来。所以标定模式要把包络落到一个文件里带回来。
 *
 * ★ 量化用 **对数刻度 u8**，不用线性：
 *   · 线性 u8 在 0~30000 的动态范围里，低电平只剩一两级，而渐入曲的有效信息恰好在低电平；
 *   · 对数刻度每级 ≈4.6% 幅度，够互相关用。
 *   · ⛔ 编码用 `log`（**不是 log1p**）：`log1p` 不是增益不变，下游归一化互相关会让峰跑飞
 *     （`tmp/env_corr.py` 自测实测过：cc 0.94 → 0.06）。
 *
 * ★ 落盘格式（一行注释 + 一行数据，逗号分隔，CSV 安全）：
 * ```text
 * # env v=1 hop_ms=10 frames=<n> sr=<rate> song=<key> [onset_frame=… sil_ref_ms=… fired_by=…]
 * 12,45,120,…
 * ```
 * 下游 `tmp/env_corr.py --device` 读它；`#` 行会被跳过。
 */
public final class EnvCodec {

    /** 编码上限：16 bit 有符号样本的满幅 RMS。 */
    public static final double MAX_RMS = 32768.0;
    /** 对数刻度的分母（固定，不随曲变，否则下游解不回来）。 */
    private static final double LOG_MAX = Math.log(MAX_RMS);

    private EnvCodec() {}

    /**
     * `meanSquare[]`（`OnsetDetector` 每帧给的那个 e = mean(x²)）→ u8 包络。
     *
     * ★ 入参用 meanSquare 而不是 RMS：端内采集线程手里就是 e，转换只在这里做一次，
     *   避免两处各写一次 `sqrt` 然后口径漂了（项目教训 9：同一个量两处实现必然分叉）。
     */
    public static byte[] encodeMeanSquare(double[] meanSquare) {
        if (meanSquare == null) throw new IllegalArgumentException("meanSquare == null");
        byte[] out = new byte[meanSquare.length];
        for (int i = 0; i < meanSquare.length; i++) {
            double e = meanSquare[i];
            double rms = (e > 0) ? Math.sqrt(e) : 0.0;
            out[i] = (byte) quantizeRms(rms);
        }
        return out;
    }

    /**
     * RMS → u8。**0 只留给「数字静音」**；1~255 覆盖 1.0~32768 的对数区间。
     *
     * ★ 为什么不直接 `q = round(log(rms)/log(MAX)*255)`（第一版就是这么写的，自测当场红）：
     *   那样 `rms=1` 会得到 0，而 0 在解码侧代表「静音」⇒ **往返误差 100%**。
     *   「0 = 静音」与「0 = 很小的声音」必须分开，否则包络开头的静音段会被解成 1.0 而不是 0。
     */
    public static int quantizeRms(double rms) {
        if (!(rms > 0)) return 0;                 // NaN / 0 / 负数 全归 0，别让 NaN 传播
        if (rms >= MAX_RMS) return 255;
        double v = Math.log(rms) / LOG_MAX;       // ∈ (0,1)
        int q = 1 + (int) Math.round(v * 254.0);  // ∈ [1,255]
        return Math.max(1, Math.min(255, q));
    }

    /** u8 → RMS（`quantizeRms` 的逆；q=0 ⇒ 0 = 数字静音）。 */
    public static double rmsOf(int q) {
        if (q <= 0) return 0.0;
        if (q >= 255) return MAX_RMS;
        return Math.exp((q - 1) * LOG_MAX / 254.0);
    }

    /** u8 包络 → RMS 数组（下游互相关直接用）。 */
    public static double[] decode(byte[] u8) {
        if (u8 == null) throw new IllegalArgumentException("u8 == null");
        double[] out = new double[u8.length];
        for (int i = 0; i < u8.length; i++) out[i] = rmsOf(u8[i] & 0xFF);
        return out;
    }

    /** base64 → u8 → RMS（母带指纹 `env_b64` 走这条路；与 pack_assets 同源）。 */
    public static double[] rmsFromBase64(String b64) {
        if (b64 == null || b64.isEmpty()) {
            throw new IllegalArgumentException("env_b64 为空（这张曲没有母带指纹）");
        }
        return decode(java.util.Base64.getDecoder().decode(b64));
    }

    /** 注释行（含全部解码所需的元信息；缺一项下游就解不回来）。 */
    public static String header(int hopMs, int frames, int rate, String songKey) {
        return header(hopMs, frames, rate, songKey, "");
    }

    /**
     * 带**额外元信息**的头（`key=value` 用空格分隔，如 `onset_frame=1070 sil_ref_ms=865.0`）。
     *
     * ★ 为什么需要（2026-10-25 标定第一轮暴露）：只有包络时能算出 `S`（母带 0 在捕获里的位置），
     *   但要算 `K = E − S` 还需要**同一次捕获里**的 onset 帧号 —— 否则 E 与 S 分别在两次运行的
     *   时钟里，根本不能相减。所以标定必须把门锁定的帧号一起写进头。
     */
    public static String header(int hopMs, int frames, int rate, String songKey, String extra) {
        if (hopMs <= 0) throw new IllegalArgumentException("hopMs 必须 > 0：" + hopMs);
        if (frames < 0) throw new IllegalArgumentException("frames 不能为负：" + frames);
        if (rate <= 0) throw new IllegalArgumentException("rate 必须 > 0：" + rate);
        if (songKey == null || songKey.isEmpty()) {
            throw new IllegalArgumentException("songKey 为空（下游不知道这份包络是哪首）");
        }
        if (songKey.indexOf(',') >= 0 || songKey.indexOf('\n') >= 0) {
            throw new IllegalArgumentException("songKey 含逗号/换行：" + songKey);
        }
        String x = oneLine(extra);
        return String.format(Locale.ROOT,
                "# env v=1 hop_ms=%d frames=%d sr=%d song=%s%s",
                hopMs, frames, rate, songKey, x.isEmpty() ? "" : " " + x);
    }

    /** 把任意字符串压成**一行**（换行→空格、去首尾空白）。空 ⇒ `""`。 */
    static String oneLine(String s) {
        if (s == null) return "";
        StringBuilder b = new StringBuilder(s.length());
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            b.append(c == '\r' || c == '\n' ? ' ' : c);
        }
        return b.toString().trim();
    }

    /** 完整文件内容（注释行 + 一行逗号分隔的 u8）。 */
    public static String toFile(int hopMs, int rate, String songKey, byte[] u8) {
        return toFile(hopMs, rate, songKey, u8, "");
    }

    /** 同上，但头里带额外元信息（标定要 `onset_frame` / `sil_ref_ms`）。 */
    public static String toFile(int hopMs, int rate, String songKey, byte[] u8, String extra) {
        StringBuilder sb = new StringBuilder(u8.length * 4 + 160);
        sb.append(header(hopMs, u8.length, rate, songKey, extra)).append('\n');
        for (int i = 0; i < u8.length; i++) {
            if (i > 0) sb.append(',');
            sb.append(u8[i] & 0xFF);
        }
        sb.append('\n');
        return sb.toString();
    }
}
