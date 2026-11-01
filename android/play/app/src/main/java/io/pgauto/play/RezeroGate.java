package io.pgauto.play;

/**
 * 课题模式 rezero 的**采纳闸门**（纯逻辑，可离线单测）。
 *
 * 背景（2026-10-27/28 实测）：
 *  · gate 定的零点有 **±25 ms 轮间抖动** ⇒ 同一首曲的 Good 数在 1~115 之间跳；
 *  · 互相关（`CourseStart`）在真实数据上精度 **+4.2 ms**，但**会自信地锁错**：
 *    实测 `retributioncycleofredemption_in` 锁到偏 2 秒的位置，`cc` 仍有 0.913。
 * ⇒ 互相关只能做**小范围修正**：与 gate 零点相差超过 {@link #MAX_DELTA_MS} 就一律不采纳。
 *    这样最坏情况只是"没修"（回到今天的行为），而不是"把整轮改坏"。
 */
public final class RezeroGate {

    /** 允许的最大修正量（ms）。超过即判为"锁错"，忽略。 */
    public static final double MAX_DELTA_MS = 200.0;

    /** 是否采纳这次互相关的结果。 */
    public static boolean accept(double zeroGateMs, double zeroCorrMs) {
        if (Double.isNaN(zeroGateMs) || Double.isNaN(zeroCorrMs)) {
            return false;
        }
        return Math.abs(zeroCorrMs - zeroGateMs) <= MAX_DELTA_MS;
    }

    /** 可读的拒绝原因（进日志用）；null = 采纳。 */
    public static String why(double zeroGateMs, double zeroCorrMs) {
        if (Double.isNaN(zeroGateMs) || Double.isNaN(zeroCorrMs)) {
            return "零点为空";
        }
        double d = zeroCorrMs - zeroGateMs;
        if (Math.abs(d) <= MAX_DELTA_MS) {
            return null;
        }
        return String.format(java.util.Locale.ROOT,
                "互相关与 gate 差 %+.0f ms（> %.0f）⇒ 判为锁错，不纠正", d, MAX_DELTA_MS);
    }

    private RezeroGate() {
    }
}
