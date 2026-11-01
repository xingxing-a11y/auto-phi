package io.pgauto.inject.sched;

import java.util.Locale;

/**
 * 收货侧逐帧记录：把「靶子 View 真正收到的 MotionEvent」落到一张 CSV。
 *
 * ★ 为什么必须有它（和 {@link InjectTrace} 是一对）：
 *   InjectTrace 记的是**我们发了什么**，本类记的是**系统派发了什么**。
 *   上一轮（10-24 第六枪）只有发送侧数据，结论只能推到「发送侧清白」为止——
 *   因为 `injectInputEvent` 返回 true 从来不代表被谁收到。
 *   要切开「没送到」与「送到了但游戏不认」，必须两侧同时有逐帧数据。
 *
 * ★ 与 10-21 靶子测试的区别（别把它当重复劳动）：
 *   那次是 {@code InjectBench.multiTouch} 手工构造的 5 帧序列（1/3/5 指各一次，
 *   结论「多指全部收到」已记在 FACTS §2472）。它证明的是**通道支持多指**。
 *   本类要答的是另一个问题：**端内 DevRun 重放真实载荷的 2775 帧，到了多少**——
 *   那条路径有 Scheduler 坐标变换、Gesture 状态机、MOVE 抽稀、两级调度，
 *   与 InjectBench 无一行共用代码。通道能过 ≠ 生产路径能过。
 *
 * ★ 三个硬约束：
 *   1. **必须拷值**：{@code MotionEvent} 对象会被系统池化复用，onTouchEvent 返回后
 *      里面的坐标就是别人的了。所以这里一律把 int/float 取值后立刻存下，绝不存引用。
 *   2. **不阻塞 UI 线程**：只在内存里 append，落盘由调用方在主线程之外做。
 *      在 onTouchEvent 里写文件会拉长输入处理，反过来改变被测的时延。
 *   3. **截断要计数**：超限只丢明细行，统计仍覆盖全部帧（与 InjectTrace 同口径），
 *      否则「少了几百行」会被误读成「那段时间没收到事件」。
 *
 * ★ 为什么 MOVE 一条都不许丢：
 *   连续 MOVE 被 InputDispatcher 合并是**正常**行为（10-21 实测 5 发 3 收）。
 *   只有保留原始 MOVE 才能量出合并率、才能区分「合并」与「丢弃」。
 */
public final class RecvTrace {

    /** 列契约。改这里必须同步改 `tools/compare_touch.py` 的 RECV_COLS。 */
    public static final String HEADER = "seq,recv_ms,et_ms,act,idx,n,ptrs,dly_ms";

    /** 明细行上限（与 InjectTrace 对齐：20000 行 ≈ 2 MB）。 */
    public static final int CAP = 20000;

    private final StringBuilder sb = new StringBuilder(1 << 18);
    private final StringBuilder ptr = new StringBuilder(64);

    private int n;              // 收到的事件总数（含被截断的明细）
    private int nTruncated;

    private int nDown, nUp, nMove, nPdown, nPup, nCancel, nOther;
    private int maxPointers;

    // ── 触摸流完整性状态机（证明「收到的是不是一串合法触摸」） ──
    private final java.util.TreeSet<Integer> down = new java.util.TreeSet<>();
    private int orphanUp;       // UP/POINTER_UP 的 id 从没 DOWN 过
    private int orphanMove;     // MOVE 里出现没按下的 id
    private int dupDown;        // 已有手指按着时又收到 ACTION_DOWN（AOSP 要求 DOWN 时 count==1）
    private int badIdx;         // actionIndex 越界

    private long firstRecvMs = Long.MIN_VALUE;
    private long lastRecvMs = Long.MIN_VALUE;
    private long maxDlyMs = Long.MIN_VALUE;
    private long sumDlyMs;
    private int nDlySamples;

    /**
     * 记一条收到的事件。
     *
     * @param recvMs 收到时刻（{@code SystemClock.uptimeMillis()}）
     * @param etMs   MotionEvent 自带的 eventTime（注入侧写入，与 recvMs 同基准）
     * @param action 带 index 编码的原始 action（{@code MotionEvent.getAction()}）
     * @param count  指针数
     * @param ids    pointerId 数组（长度 ≥ count）
     * @param xs     x 坐标（长度 ≥ count）
     * @param ys     y 坐标（长度 ≥ count）
     */
    public void record(long recvMs, long etMs, int action, int count,
                       int[] ids, float[] xs, float[] ys) {
        n++;
        final int act = action & 0xff;
        final int idx = (act == Gesture.ACTION_POINTER_DOWN || act == Gesture.ACTION_POINTER_UP)
                ? ((action >> Gesture.ACTION_POINTER_INDEX_SHIFT) & 0xff) : -1;

        // ★ 统计先做完，再判截断（与 InjectTrace 同一理由：结论不能缺样本）
        switch (act) {
            case Gesture.ACTION_DOWN -> nDown++;
            case Gesture.ACTION_UP -> nUp++;
            case Gesture.ACTION_MOVE -> nMove++;
            case Gesture.ACTION_POINTER_DOWN -> nPdown++;
            case Gesture.ACTION_POINTER_UP -> nPup++;
            case Gesture.ACTION_CANCEL -> nCancel++;
            default -> nOther++;
        }
        if (count > maxPointers) maxPointers = count;

        if (firstRecvMs == Long.MIN_VALUE) firstRecvMs = recvMs;
        lastRecvMs = recvMs;
        long dly = recvMs - etMs;
        // 负数/荒谬值 = 时钟基准不同，照记不照统计（10-18 的 65569.5 s 就是这个坑）
        if (dly >= 0 && dly < 5000) {
            nDlySamples++;
            sumDlyMs += dly;
            if (dly > maxDlyMs) maxDlyMs = dly;
        }

        // 触摸流状态机：只用于「流是否合法」，不影响落盘
        if (idx >= 0 && idx >= count) badIdx++;
        switch (act) {
            case Gesture.ACTION_DOWN -> {
                if (!down.isEmpty()) dupDown++;
                if (count > 0) down.add(ids[0]);
            }
            case Gesture.ACTION_POINTER_DOWN -> {
                if (count > 0) {
                    // 新加的指针一定是数组末尾那个（AOSP 语义）；用「不在集合里的 id」更宽容
                    for (int i = 0; i < count; i++) {
                        if (!down.contains(ids[i])) { down.add(ids[i]); break; }
                    }
                }
            }
            case Gesture.ACTION_MOVE -> {
                for (int i = 0; i < count; i++) if (!down.contains(ids[i])) { orphanMove++; break; }
            }
            case Gesture.ACTION_POINTER_UP -> {
                if (idx >= 0 && idx < count) {
                    if (!down.remove(ids[idx])) orphanUp++;
                }
            }
            case Gesture.ACTION_UP -> {
                if (count > 0 && !down.remove(ids[0])) orphanUp++;
                // UP 之后不管怎样都清空：避免一处漏算把后面全污染
                down.clear();
            }
            case Gesture.ACTION_CANCEL -> down.clear();
            default -> { }
        }

        if (n > CAP) { nTruncated++; return; }

        sb.append(n).append(',')
                .append(recvMs).append(',')
                .append(etMs).append(',')
                .append(act).append(',')
                .append(idx).append(',')
                .append(count).append(',');
        ptr.setLength(0);
        for (int i = 0; i < count; i++) {
            if (i > 0) ptr.append('|');
            // 坐标取整：发送侧是 int，收货侧是 float ⇒ 对账允许 ±1 的舍入差
            ptr.append(ids[i]).append(':')
                    .append(Math.round(xs[i])).append(':').append(Math.round(ys[i]));
        }
        sb.append(ptr).append(',')
                .append(dly)
                .append('\n');
    }

    /** 完整 CSV（表头 + 明细 + 截断说明）。设备上落盘的就是它。 */
    public String csv() {
        StringBuilder out = new StringBuilder(sb.length() + 96);
        out.append(HEADER).append('\n').append(sb);
        if (nTruncated > 0) {
            out.append("# truncated ").append(nTruncated)
                    .append(" lines (cap ").append(CAP).append(")\n");
        }
        return out.toString();
    }

    /** 一行摘要（界面/日志直接看，不必拉文件）。 */
    public String brief() {
        StringBuilder b = new StringBuilder(200);
        b.append("收到 ").append(n).append(" 条")
                .append("（DOWN ").append(nDown)
                .append(" / PDOWN ").append(nPdown)
                .append(" / MOVE ").append(nMove)
                .append(" / PUP ").append(nPup)
                .append(" / UP ").append(nUp)
                .append(" / CANCEL ").append(nCancel);
        if (nOther > 0) b.append(" / 其他 ").append(nOther);
        b.append("）");
        b.append("；最大指针数 ").append(maxPointers);
        if (nDlySamples > 0) {
            b.append("；分发延迟 avg ")
                    .append(String.format(Locale.ROOT, "%.2f", (double) sumDlyMs / nDlySamples))
                    .append(" / max ").append(maxDlyMs).append(" ms");
        }
        b.append("；孤儿抬指 ").append(orphanUp)
                .append(" 孤儿MOVE ").append(orphanMove)
                .append(" 重复DOWN ").append(dupDown);
        if (badIdx > 0) b.append(" idx越界 ").append(badIdx);
        if (!down.isEmpty()) b.append("；收尾仍按下 ").append(down.size()).append(" 指");
        if (nTruncated > 0) b.append("；明细截断 ").append(nTruncated).append(" 行");
        return b.toString();
    }

    // ── 给对账/断言用 ──
    public int events() { return n; }
    public int written() { return n - nTruncated; }
    public int truncated() { return nTruncated; }
    public int maxPointers() { return maxPointers; }
    public int downCount() { return nDown; }
    public int upCount() { return nUp; }
    public int moveCount() { return nMove; }
    public int pointerDownCount() { return nPdown; }
    public int pointerUpCount() { return nPup; }
    public int cancelCount() { return nCancel; }
    public int orphanUp() { return orphanUp; }
    public int orphanMove() { return orphanMove; }
    public int dupDown() { return dupDown; }
    public int stillDown() { return down.size(); }
    public long firstRecvMs() { return firstRecvMs; }
    public long lastRecvMs() { return lastRecvMs; }
    public long maxDlyMs() { return maxDlyMs; }
    public int dlySamples() { return nDlySamples; }

    /** 清空（界面「重新开始」用）。 */
    public void reset() {
        sb.setLength(0);
        n = nTruncated = 0;
        nDown = nUp = nMove = nPdown = nPup = nCancel = nOther = 0;
        maxPointers = 0;
        down.clear();
        orphanUp = orphanMove = dupDown = badIdx = 0;
        firstRecvMs = lastRecvMs = Long.MIN_VALUE;
        maxDlyMs = Long.MIN_VALUE;
        sumDlyMs = 0;
        nDlySamples = 0;
    }
}
