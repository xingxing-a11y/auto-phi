package io.pgauto.play;

/**
 * 课题模式：**曲间下界**的纯逻辑（2026-10-28）。
 *
 * 为什么单独成类：本类被 {@code PlayService.runCourse} 使用，而 `PlayService` 是 Android
 * Service —— `selftest.sh` 的 JVM 编译集**故意不带 android.jar** ⇒ 逻辑写在 Service 里
 * 就永远拿不到离线单测。所以判据必须抽出来，做成和 `RezeroGate` / `CourseStart` 一样的纯类。
 *
 * ★ 它修的是什么 bug（2026-10-28 复盘，原样在磁盘上可查）：
 *   `runCourse` 对「第 2/3 首没打起来」只打一行日志就 `continue`，但下一首的"不早于"下界
 *   是用**上一首零点 + 音乐总时长**算的，而失败路径**从不更新**这两个量 ⇒ 被跳过的那首
 *   让下界停留在**上上首**、早退 2~4 分钟 ⇒ 闸门必然锁到"还在播放的上一首"的尾音，
 *   把本来还能救的下一首整首打废 —— 一次失败被放大成两次。
 *
 * ★ 正确判据只有一条：**这一首的零点到底有没有建立**
 *   · 没建立（等不到起音 / 连续 6 次零点异常）⇒ 下一首的下界**不可知** ⇒ 调用方必须中止；
 *   · 已建立（例如 {@code inject()} 中途抛、Shizuku 断了）⇒ 音乐时间轴与我们的注入无关，
 *     `zero + 音乐总时长` 仍然是**正确**的下界 ⇒ 更新状态、继续下一首。
 *   ⛔ 别把这条改成「失败一律中止」（会白扔一轮）或「失败一律继续」（就是上面那个 bug）。
 */
public final class CourseBound {

    /**
     * 失败路径：下一首的"不早于"基线（epoch ms）。
     *
     * @param zeroEpochMs 本曲零点（epoch ms）；{@code NaN} = 没建立
     * @param musicLenMs  本曲**音乐总时长**（manifest 的 {@code length_s}，ms）；{@code <=0} = 未知
     * @return 基线；**{@code NaN} = 不可知 ⇒ 调用方必须中止整场**
     */
    public static double nextBaseAfterFailure(double zeroEpochMs, double musicLenMs) {
        // 「有没有零点」是唯一判据：没有它，后面每首的闸门都只能盲等
        if (Double.isNaN(zeroEpochMs) || Double.isInfinite(zeroEpochMs)) {
            return Double.NaN;
        }
        // 长度未知时 base 也立不住（用"上一首末事件"会早退到上一次的尾巴）
        if (!(musicLenMs > 0)) {                  // 同一句挡住 0 / 负数 / NaN
            return Double.NaN;
        }
        return zeroEpochMs + musicLenMs;
    }

    /**
     * 正常路径的下界：优先「上一首零点 + 音乐总时长」；长度缺失时退回「上一首末事件」。
     *
     * ⚠ 退回分支只保证"不比末事件更早"，它**不含尾奏**（`length_s` 与末音符可差 7 s，
     *   见 2026-10-28 那次 959/989 Miss）⇒ 全库 1056 首都有 `length_s`，正常走不到。
     */
    public static double baseMs(double prevZeroMs, double prevLenMs, double lastEndMs) {
        return prevLenMs > 0 ? prevZeroMs + prevLenMs : lastEndMs;
    }

    private CourseBound() {
    }
}
