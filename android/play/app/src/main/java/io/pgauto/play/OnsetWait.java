package io.pgauto.play;

import java.util.function.BooleanSupplier;

/**
 * S1-d 等待循环的**决策函数**（纯 Java，零 Android 依赖）。
 *
 * 为什么单独一个类：这里曾经藏着一个**只在几毫秒窗口**里出现的竞态（10-24 遍历发现）——
 * `OnsetZero.await()` 先读 `gate.fired()` 再读 `tap.alive()`，而采集侧写这两个量的顺序
 * **不是原子的**：
 * ```text
 *   采集线程（OnsetTap.loop）            等待线程（OnsetZero.await）
 *   g.onFrame(...) ⇒ fired = true
 *   if (g.fired()) break;
 *   finally ⇒ recording = false
 *                                        fired() → false   ← 恰好在两步之间读到
 *                                        alive() → false   ⇒ 抛「采集线程已退出但没锁到 onset」
 * ```
 * 后果是**把已经成功的一轮判成失败**，而且日志把原因指反（说没听见，其实听见了）。
 *
 * 抽成纯函数是为了能在 PC 上**精确重放这个交错**：测试传一个「第一次读 false、第二次读
 * true」的 {@link BooleanSupplier} 即可，不需要 Android、不需要真机、不需要线程。
 *
 * ★ 因此有两条不变量，改这个类必须保住：
 *   ① 任何「据 alive / 超时下结论」的分支，**下结论之前必须复查 fired**；
 *   ② `stop` 优先级最高（用户点了停止就别再报采集故障），`error` 其次于 `fired`
 *      （已锁到就不算错 —— 锁到之后采集线程退出是**正常提前收工**）。
 */
public final class OnsetWait {

    /** 一次判定的结论（语义见 {@link #step} 的注释）。 */
    public enum Step {
        /** 已锁到曲首 onset。 */
        FIRED,
        /** 用户中止。 */
        STOPPED,
        /** 采集侧硬错误（`read` 返回负值 / 投影被撤销 等）。 */
        ERROR,
        /** 采集线程退出，且**复查确认**没锁到。 */
        DIED,
        /** 等过 deadline，且**复查确认**没锁到。 */
        TIMEOUT,
        /** 继续等。 */
        WAIT
    }

    /**
     * 根据当前状态决定下一步。
     *
     * @param fired      `gate.fired`（**会被多次调用**，这是复查机制本身，不要换成预先读好的 boolean）
     * @param error      采集侧错误文本；null = 正常
     * @param alive      采集线程是否还在推进（`OnsetTap.alive()`）
     * @param stop       用户是否点了停止
     * @param waitedMs   从点 ▶ 到现在过了多少毫秒（单调时钟）
     * @param deadlineMs 超时上限
     * @return 下一步动作；`WAIT` 时调用方应短暂 sleep 后重新调本函数
     */
    public static Step step(BooleanSupplier fired, String error, boolean alive,
                            boolean stop, long waitedMs, int deadlineMs) {
        if (fired == null) throw new IllegalArgumentException("fired == null");
        if (stop) return Step.STOPPED;
        if (fired.getAsBoolean()) return Step.FIRED;
        if (error != null) return Step.ERROR;
        if (!alive) {
            // ★ 竞态兜底：`recording=false` 可能是「已锁到 ⇒ break」的正常收工。
            //   不看这一眼就会把成功判成失败（这类错只在毫秒窗口里出现，真机上极难复现）。
            return fired.getAsBoolean() ? Step.FIRED : Step.DIED;
        }
        if (waitedMs > deadlineMs) {
            // 同一个道理：超时与锁到可能只差一次调度。
            return fired.getAsBoolean() ? Step.FIRED : Step.TIMEOUT;
        }
        return Step.WAIT;
    }

    private OnsetWait() {}
}
