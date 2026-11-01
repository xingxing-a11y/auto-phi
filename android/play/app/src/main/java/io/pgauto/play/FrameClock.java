package io.pgauto.play;

/**
 * 包络帧 → 挂钟时刻的换算。**纯算术，零 Android 依赖**（PC 上 javac + java 可单测）。
 *
 * 为什么需要它：`OnsetDetector` 只吐出 `frameIdx`，不给时间。而零点的精度要求是
 * 毫秒级（判定窗 P 是 ±80 ms），所以必须把「第几帧」换成「设备挂钟的哪一刻」。
 *
 * 口径（与 `probe/RecorderService` 的 sidecar 一致）：
 * ```text
 *   read() 返回的时刻 tRead  ≈ **本块最后一个样本已被采集**的时刻
 *   帧 f 覆盖单声道样本 [f*hop, (f+1)*hop)
 *   frameEndNs = tReadNs − (sAccMono − (f+1)*hop) × 1e9 / rate
 * ```
 *
 * ★ 为什么取**帧中点**而不是帧末尾：一帧 10 ms，真正的起音落在帧内某处。取末尾恒偏晚
 *   最多 10 ms，取中点把量化误差压成 ±5 ms。剩下的常模误差（含 `read()` 缓冲积压、
 *   混音链路）由 `L_dev` 一次性吸收 —— **这正是 L_dev 存在的意义**，不要指望这里算准。
 * ★ 这里算错的表现不是崩溃，而是「整轮偏几十毫秒、全 Late 或全 Early」，症状和注入侧
 *   的 bug 一模一样 ⇒ 所以它是纯函数，必须被单测钉住（`OnsetGateSelfTest` #9~#12）。
 */
public final class FrameClock {

    private FrameClock() {}

    /**
     * @param tReadNs      `AudioRecord.read()` 返回后立刻取的 `SystemClock.elapsedRealtimeNanos()`
     * @param sAccMono     读完本块后**累计**的单声道样本数（= 历次 read 返回的 short 数 / 2）
     * @param frameIdx     帧号（`OnsetDetector` 从 0 严格递增）
     * @param hop          每帧单声道样本数（`OnsetDetector.hopSamples()`）
     * @param rate         采样率
     * @return 该帧**中点**的挂钟时刻（ns，与 elapsedRealtimeNanos 同基准）
     */
    public static long frameMidNs(long tReadNs, long sAccMono, long frameIdx,
                                  int hop, int rate) {
        if (rate <= 0) throw new IllegalArgumentException("rate 必须 > 0，得到 " + rate);
        if (hop <= 0) throw new IllegalArgumentException("hop 必须 > 0，得到 " + hop);
        if (frameIdx < 0) throw new IllegalArgumentException("frameIdx 不能为负：" + frameIdx);
        if (sAccMono < 0) throw new IllegalArgumentException("sAccMono 不能为负：" + sAccMono);
        // 帧的末尾样本序号 = (frameIdx+1)*hop；它必须已经在本块里读过。
        // 违反 ⇒ 上游把「块内帧」当成「全局帧」或用错了 sAcc，**必须炸**而不是算个负的回溯量。
        long endSample = (frameIdx + 1) * hop;
        long back = sAccMono - endSample;
        if (back < 0) {
            throw new IllegalArgumentException("sAccMono=" + sAccMono
                    + " 还没吃到帧 " + frameIdx + " 的末尾（需要 ≥ " + endSample + "）");
        }
        long backNs = Math.round(back * 1e9 / rate);
        long halfNs = Math.round(hop * 0.5 * 1e9 / rate);
        return tReadNs - backNs - halfNs;
    }

    /** 一帧的时长（ns），给日志/容差用。 */
    public static long frameNs(int hop, int rate) {
        if (rate <= 0) throw new IllegalArgumentException("rate 必须 > 0，得到 " + rate);
        return Math.round(hop * 1e9 / (double) rate);
    }
}
