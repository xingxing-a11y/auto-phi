package io.pgauto.play;

/**
 * 标定参数的**融合规则**（纯逻辑，零 Android 依赖，可 JVM 自测）。
 *
 * ★ 为什么单独一个类：`K`（每曲检测偏置）与 `lead`（点 ▶→t=0）都是"越测越准、但也越测越容易被
 *   一轮异常带歪"的量。规则错了不会报错，只会让 `L_dev` 慢慢偏几十毫秒，然后 φ 变成一堆 Good
 *   —— 这种错只能靠**可测的纯函数**防住（本类 + `CalibMergeSelfTest`）。
 *
 * 规则（按顺序）：
 * 1. **绝对范围**：样本超出 `[absMin, absMax]` 直接丢（物理上不可能）；
 * 2. **第一条样本**：没有基线时也要过 `firstAbsMax`（否则一次异常就成基线，离群剔除永远失效）；
 * 3. **离群**：与均值差 > `rejectAbs` 判为离群，**不并入**（但要计数）；
 * 4. **环境变更**：连续 `sideLimit` 次**同侧**大偏离（> `sameSideAbs`）⇒ 判环境真的变了，
 *    **重置为新值**（n=1），而不是被旧均值一直拖着；
 * 5. **融合**：n<10 用算术平均，之后转指数式（权重 1/10），保留一点适应性。
 *
 * ⛔ 别把"离群剔除"当成"越多越好"：阈值太紧会把真实漂移当噪声（lead 实测跨度可达 166 ms）。
 */
public final class CalibMerge {

    /** 一次融合的结果。 */
    public static final class Out {
        public final float value;
        public final int n;
        public final boolean accepted;
        /** 连续同侧偏离的计数（调用方要存起来，下一轮继续用）。 */
        public final int sideCount;
        /** 最近一次大偏离的方向：+1 = 样本比均值大，−1 = 小，0 = 无。 */
        public final int sideSign;
        public final boolean reset;
        public final String why;

        Out(float value, int n, boolean accepted, int sideCount, int sideSign,
            boolean reset, String why) {
            this.value = value;
            this.n = n;
            this.accepted = accepted;
            this.sideCount = sideCount;
            this.sideSign = sideSign;
            this.reset = reset;
            this.why = why;
        }

        @Override
        public String toString() {
            return String.format(java.util.Locale.ROOT,
                    "value=%.2f n=%d accepted=%s sideCount=%d sideSign=%d reset=%s %s",
                    value, n, accepted, sideCount, sideSign, reset, why);
        }
    }

    /** 阈值集合（K 与 lead 各一套，单位都是 ms）。 */
    public static final class Policy {
        public final double absMin, absMax;
        public final double firstAbsMax;
        public final double rejectAbs;
        public final double sameSideAbs;
        public final int sideLimit;

        public Policy(double absMin, double absMax, double firstAbsMax,
                      double rejectAbs, double sameSideAbs, int sideLimit) {
            this.absMin = absMin;
            this.absMax = absMax;
            this.firstAbsMax = firstAbsMax;
            this.rejectAbs = rejectAbs;
            this.sameSideAbs = sameSideAbs;
            this.sideLimit = sideLimit;
        }

        /**
         * K：实测 −7.3~+53.2；第一条允许到 **130**。
         *
         * ★ 2026-10-26 修：原来第一条卡 **80**，而 `destruction321_at` 实测 `k_meas=-92.2 ms`
         *   （cc=0.861 / quality=1.08，已按"弱峰档"采纳）⇒ 被这条**融合闸**拒掉，`k_n` 永远是 0，
         *   用户看到的就是"这首永远测不出 K"。上游已经有 `MAX_ABS_K_MS=150` 的物理闸，
         *   这里不该比它更严；第一样本放宽到 130 留一点余量，后续样本仍按 ±30 离群剔除，
         *   连续 3 次同侧偏差会自动重置基线（`sideLimit`）。
         */
        public static Policy forK() {
            return new Policy(-150, 150, 130, 30, 35, 3);
        }

        /** lead：实测 7.19~7.56 s；换设备/低端机可能 5~15 s。 */
        public static Policy forLead() {
            return new Policy(3000, 25000, 25000, 30, 35, 3);
        }
    }

    private CalibMerge() {}

    /**
     * @param old       已有均值（n==0 时无意义）
     * @param n         已有样本数
     * @param sample    本轮测得值
     * @param sideCount 已有的"连续同侧大偏离"计数
     */
    public static Out merge(float old, int n, double sample,
                            int sideCount, int prevSign, Policy p) {
        if (Double.isNaN(sample)) {
            return new Out(old, n, false, sideCount, prevSign, false, "样本是 NaN");
        }
        if (sample < p.absMin || sample > p.absMax) {
            return new Out(old, n, false, sideCount, prevSign, false,
                    String.format(java.util.Locale.ROOT, "超出绝对范围 [%.0f, %.0f]",
                            p.absMin, p.absMax));
        }
        if (n <= 0) {
            if (Math.abs(sample) > p.firstAbsMax) {
                return new Out(old, n, false, sideCount, 0, false,
                        String.format(java.util.Locale.ROOT, "第一条样本 |%.1f| > %.0f",
                                sample, p.firstAbsMax));
            }
            return new Out((float) sample, 1, true, 0, 0, false, "首条样本");
        }

        double dev = sample - old;
        if (Math.abs(dev) > p.rejectAbs) {
            int sign = dev > 0 ? 1 : -1;
            int sc = (sign == prevSign) ? sideCount + 1 : 1;
            if (sc >= p.sideLimit) {
                // 连续同侧 ⇒ 判环境变了，重置为新值（而不是被旧均值拖着）
                return new Out((float) sample, 1, true, 0, 0, true,
                        String.format(java.util.Locale.ROOT,
                                "连续 %d 次同侧偏离（本次 %+.1f）⇒ 重置为新值", sc, dev));
            }
            return new Out(old, n, false, sc, sign, false,
                    String.format(java.util.Locale.ROOT, "离群（%+.1f ms > %.0f）", dev, p.rejectAbs));
        }

        // 正常样本：清掉同侧计数
        float merged = n < 10 ? (float) ((old * n + sample) / (n + 1))
                : (float) (old + (sample - old) / 10.0);
        return new Out(merged, Math.min(10, n + 1), true, 0, 0, false,
                String.format(java.util.Locale.ROOT, "并入（%+.1f ms）", dev));
    }
}
