package io.pgauto.play;

/**
 * 包络互相关：给定「捕获包络」与「母带指纹」，求母带 t=0 出现在捕获时间轴的哪一帧。
 *
 * ★ 它是 `tmp/env_corr.py` 的 Java 版，**必须与它逐字同口径**（2026-10-25 方案 S2/S3）：
 *   · 特征 = `log(max(rms, 0.005·max))` 去均值（对增益严格不变；⛔ 不是 log1p，那条路 cc 会从 0.94 掉到 0.06）；
 *   · 两边都掐掉首尾静音（不掐会出现「捕获静音 vs 母带正在响」的结构性错配）；
 *   · 归一化互相关，抛物线插值取亚帧峰；
 *   · 质量分 = 峰值 /（±0.5 s 之外的次峰）。
 *   两处实现分叉不会报错，只会让 K 偏几十毫秒 —— 所以只能在**同一份数据**上对拍（见 selftest 的 --replay）。
 *
 * ★ 为什么必须有**先验窗**：真实录音上全局搜索的次峰/主峰 = 1.02（几乎一样高），
 *   峰不唯一；而 onset 已经给出 ±80 ms 内的粗位置 ⇒ 只在它附近搜。先验以外的搜索只在诊断时用。
 *
 * 线程模型：纯函数、无状态、可在任意线程跑（标定结束后的后台线程）。
 */
public final class EnvMatcher {

    /** 至少重叠这么多帧（0.5 s）才允许作为候选，否则两端几个点的伪峰会把结果带走。 */
    private static final int MIN_OVERLAP_FRAMES = 50;
    /** 质量分里「次峰」的排除半径（帧）= ±0.5 s。 */
    private static final int GUARD_FRAMES = 50;

    private EnvMatcher() {}

    /** 一次匹配的结果。`ok=false` 时**必须**由调用方回退（不许静默采用垃圾值）。 */
    public static final class Result {
        /** 母带第 0 帧在**捕获原始帧坐标**里的位置（可以是负的：捕获从曲中开始）。 */
        public final double lagFrames;
        public final double cc;
        /** 峰值 / 次峰。≈1 ⇒ 峰不唯一，结果不可信。 */
        public final double quality;
        public final int trimRec;
        public final int trimRef;
        public final boolean ok;
        public final String why;

        Result(double lagFrames, double cc, double quality, int trimRec, int trimRef,
               boolean ok, String why) {
            this.lagFrames = lagFrames;
            this.cc = cc;
            this.quality = quality;
            this.trimRec = trimRec;
            this.trimRef = trimRef;
            this.ok = ok;
            this.why = why;
        }

        /** 母带 t=0 在捕获里的毫秒数（= −lag×hop；正 = 捕获开始后这么久开唱）。 */
        public double startMs(int hopMs) {
            return -lagFrames * hopMs;
        }

        @Override
        public String toString() {
            return String.format(java.util.Locale.ROOT,
                    "lag=%.1f 帧 cc=%.3f 质量=%.2f trim(rec=%d, ref=%d) ok=%s %s",
                    lagFrames, cc, quality, trimRec, trimRef, ok, why);
        }
    }

    /**
     * @param recRms      捕获包络（RMS，10 ms 一帧）
     * @param refRms      母带指纹（RMS，同 hop）
     * @param centerFrames 先验中心（**未掐坐标**，帧）：通常是 onset 帧号 − sil_ref/10；&lt;0 表示不加先验
     * @param halfFrames   先验半宽（帧）
     */
    public static Result match(double[] recRms, double[] refRms,
                               int centerFrames, int halfFrames) {
        if (recRms == null || refRms == null) {
            return new Result(0, 0, 0, 0, 0, false, "输入为空");
        }
        int[] tr = trim(recRms, 0.01);
        int[] tf = trim(refRms, 0.005);
        int a0 = tr[0], b0 = tr[1], r0 = tf[0], r1 = tf[1];
        if (b0 - a0 < MIN_OVERLAP_FRAMES || r1 - r0 < MIN_OVERLAP_FRAMES) {
            return new Result(0, 0, 0, a0, r0, false, "有效帧太少(rec=" + (b0 - a0)
                    + ", ref=" + (r1 - r0) + ")");
        }
        double[] L = feat(java.util.Arrays.copyOfRange(recRms, a0, b0));
        double[] R = feat(java.util.Arrays.copyOfRange(refRms, r0, r1));
        int n = L.length, m = R.length;

        int minOverlap = Math.max(MIN_OVERLAP_FRAMES, n / 4);
        int lo = -(n - minOverlap), hi = m - minOverlap;
        if (centerFrames >= 0) {
            // ★ 先验是「未掐坐标」，搜索跑在掐过的帧上 ⇒ 必须换算，否则整体偏 (r0−a0) 帧
            int c = centerFrames - (r0 - a0);
            int w = Math.max(1, halfFrames);
            lo = Math.max(lo, c - w);
            hi = Math.min(hi, c + w);
        }
        if (hi < lo) return new Result(0, 0, 0, a0, r0, false, "先验窗越界");

        double best = -2, bestLag = 0;
        int bestI = -1;
        double[] ccs = new double[hi - lo + 1];
        for (int lag = lo, k = 0; lag <= hi; lag++, k++) {
            double cc = ccAt(L, R, lag, n, m);
            ccs[k] = cc;
            if (cc > best) {
                best = cc;
                bestLag = lag;
                bestI = k;
            }
        }
        if (bestI < 0) return new Result(0, 0, 0, a0, r0, false, "无有效重叠");

        // 抛物线插值（边界不插）
        double lag = bestLag;
        if (bestI > 0 && bestI < ccs.length - 1
                && !Double.isNaN(ccs[bestI - 1]) && !Double.isNaN(ccs[bestI + 1])) {
            double y0 = ccs[bestI - 1], y1 = ccs[bestI], y2 = ccs[bestI + 1];
            double den = y0 - 2 * y1 + y2;
            if (Math.abs(den) > 1e-12) lag += 0.5 * (y0 - y2) / den;
        }

        double second = -1;
        for (int k = 0; k < ccs.length; k++) {
            if (Math.abs(k - bestI) <= GUARD_FRAMES) continue;
            if (ccs[k] > second) second = ccs[k];
        }
        double quality = second > 1e-6 ? best / second : Double.POSITIVE_INFINITY;

        // 换算回「捕获原始帧坐标」
        double outLag = lag + r0 - a0;
        boolean ok = best >= 0.80 && quality >= 1.10;
        String why = ok ? "ok" : "质量不足(cc=" + fmt(best) + ", 质量=" + fmt(quality) + ")";
        return new Result(outLag, best, quality, a0, r0, ok, why);
    }

    /** 归一化互相关：把 rec 整体平移 lag 帧后与 ref 重叠部分的相关系数。 */
    static double ccAt(double[] L, double[] R, int lag, int n, int m) {
        int lo = Math.max(0, lag), hi = Math.min(m, lag + n);
        int a = lo - lag, b = hi - lag;
        if (b - a < 2) return Double.NaN;
        double sL = 0, sR = 0;
        for (int i = 0; i < b - a; i++) {
            sL += L[a + i];
            sR += R[lo + i];
        }
        double mL = sL / (b - a), mR = sR / (b - a);
        double vL = 0, vR = 0, cov = 0;
        for (int i = 0; i < b - a; i++) {
            double x = L[a + i] - mL, y = R[lo + i] - mR;
            vL += x * x;
            vR += y * y;
            cov += x * y;
        }
        if (vL <= 1e-12 || vR <= 1e-12) return Double.NaN;
        return cov / Math.sqrt(vL * vR);
    }

    /** 特征：`log(max(x, 0.005·max))` 去均值 —— 对增益严格不变。 */
    static double[] feat(double[] x) {
        double mx = 0;
        for (double v : x) if (v > mx) mx = v;
        double floor = Math.max(mx * 0.005, 1e-9);
        double[] y = new double[x.length];
        double sum = 0;
        for (int i = 0; i < x.length; i++) {
            y[i] = Math.log(Math.max(x[i], floor));
            sum += y[i];
        }
        double mean = sum / x.length;
        for (int i = 0; i < y.length; i++) y[i] -= mean;
        return y;
    }

    /** 掐掉首尾静音，返回 [start, end)（end 独占）。 */
    static int[] trim(double[] x, double rel) {
        double mx = 0;
        for (double v : x) if (v > mx) mx = v;
        double thr = Math.max(mx * rel, 1.0);
        int s = -1, e = -1;
        for (int i = 0; i < x.length; i++) {
            if (x[i] > thr) {
                if (s < 0) s = i;
                e = i + 1;
            }
        }
        if (s < 0) return new int[]{0, x.length};
        return new int[]{s, e};
    }

    private static String fmt(double v) {
        return String.format(java.util.Locale.ROOT, "%.3f", v);
    }
}
