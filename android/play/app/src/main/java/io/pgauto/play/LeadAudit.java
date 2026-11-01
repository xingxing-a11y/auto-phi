package io.pgauto.play;

/**
 * lead 的**事后对账**（2026-10-25 21:4x）：一轮打完之后，用同一轮算出的 `S` 反查这条 lead 样本可不可信。
 *
 * ★ 为什么需要（真机证据）：bounded_in 两轮**都 φ**，但 `zero_rel`（= lead 的量）差了 **503 ms**
 *   （19:56 7926.3 / 21:36 7423.5），而同一轮标定算出的 `S=7421.9` 与**后一轮**的 lead 只差 1.6 ms。
 *   ⇒ 19:56 那条"首样本"才是坏的那个，它被 `forLead()` 的首样本闸（只要求落在 3~25 s）放进来了。
 *   对 Ⅱ 类曲来说这是致命的：预派零点会偏半秒，盲区音符全丢。
 *
 * ★ 两件事必须分开：
 *   · 本轮样本与 `S` 不符（差 > 容差）⇒ **回滚本轮样本**，别污染均值；
 *   · 本轮样本与 `S` 相符、但**存盘均值**与 `S` 不符 ⇒ 均值才是坏的 ⇒ **用本轮样本重置均值**（n=1）。
 *   旧逻辑只看"样本 vs 均值"，两种情况都表现为"离群"，于是既救不回均值、也分不清谁坏。
 *
 * 纯逻辑，无 Android 依赖 ⇒ 可在 PC 上跑 JVM 自测（`LeadAuditSelfTest`）。
 */
public final class LeadAudit {

    /** 结论。 */
    public static final int OK = 0;        // 都对得上（或信息不足，不动）
    public static final int ROLLBACK = 1;  // 本轮样本不可信 ⇒ 回滚
    public static final int RESET = 2;     // 均值不可信 ⇒ 用本轮样本重置

    private LeadAudit() {}

    /**
     * @param leadMeas 本轮实测 lead（ms）；NaN = 没测到
     * @param sMs      同轮标定算出的 `S`（ms）；NaN = 没算出来
     * @param meanMs   存盘均值；null/NaN = 还没有历史
     * @param tolMs    容差（ms）
     */
    public static int decide(double leadMeas, double sMs, Double meanMs, double tolMs) {
        if (Double.isNaN(leadMeas) || Double.isNaN(sMs)) return OK;   // 信息不足：不动
        if (Math.abs(leadMeas - sMs) > tolMs) return ROLLBACK;
        if (meanMs != null && !meanMs.isNaN() && Math.abs(meanMs - sMs) > tolMs) return RESET;
        return OK;
    }

    /** 给日志/产物用的短标签（无空格，直接拼进 trace 的 `k=v` 行）。 */
    public static String tag(int verdict) {
        switch (verdict) {
            case ROLLBACK: return "rollback";
            case RESET: return "reset_mean";
            default: return "ok";
        }
    }
}
