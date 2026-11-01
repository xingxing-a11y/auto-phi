package io.pgauto.inject;

import android.os.SystemClock;
import android.view.InputDevice;
import android.view.MotionEvent;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.File;
import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Method;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.concurrent.locks.LockSupport;

import io.pgauto.inject.sched.Gesture;
import io.pgauto.inject.sched.InjectTrace;
import io.pgauto.inject.sched.Scheduler;

/**
 * 端内「脑」：把载荷变成**按时注入的 MotionEvent 流**。
 *
 * 与 PC 侧（tmp/full_run.py 的注入循环）的关系
 *   PC：`for ev: wait_until(zero + t_ms); send()` —— 单次 0.5 ms，全事件串行也跟得上。
 *   端内：单次经 Shizuku 代理 **p50 3.2 ms / p99 7.2 ms**（10-21 实测）⇒ 全事件串行
 *         会累积（lacamp 模拟 p99 38 ms）。所以这里用**两级调度**：
 *           · 关键事件（DOWN/POINTER_DOWN/POINTER_UP/UP）：绝对时刻忙等，**必发**；
 *           · MOVE：落后（now > due）就丢 —— 判定只认手指何时落在判定区，MOVE 只维持位置。
 *
 * ★ 三个必须做对、否则「看起来成功、实际全崩」的点：
 *   1. 每条注入**断言返回值**：`mode=2` 会全部返回 false 且不抛异常（10-20 实测 600/600）。
 *   2. 结束/中止时**必抬所有仍按下的指针**：残留虚拟指针会让输入子系统永久吞 DOWN。
 *   3. MOVE 被丢弃时**状态照样推进**：否则下一个关键事件会把旧坐标带给游戏。
 */
public final class DevRun {

    /** 记事用回调（服务里写 status.txt + logcat）。 */
    public interface Log { void line(String s); }

    /**
     * MOVE 允许的迟到上限（ms）。超过它说明调度已经跟不上，这条 MOVE 的位置
     * 已经没有意义（后面还有更新的位置），丢弃比迟到更安全。
     * ★ 必须给容差：改成「等到 due 再发」之后，迟到 3~7 ms 是注入本身的成本，
     *   用布尔 late 判定会把每一条 MOVE 都丢掉。
     */
    private static final double MOVE_LATE_TOL_MS = 20.0;
    /**
     * 迟到超过这个量的 **MOVE 直接丢掉**（止损，2026-10-26）。
     *
     * 理由：40 ms 之后这条 MOVE 的位置信息已经过期（Perfect 窗 ±80 ms 都过去一半），
     * 继续发只会让队列越拖越长（`玩具狂奏曲終焉 AT` 的 106 个 Late-Good 就是这么来的）。
     * 关键事件（DOWN/UP/POINTER_*）**绝不丢**。
     */
    private static final double MOVE_LATE_DROP_MS = 40.0;

    /**
     * 相邻 MOVE 的最小间隔 = **实测注入成本的 EWMA + 0.8 ms**，夹在 [3, 8]。
     *
     * ★ 为什么必须抽稀：本载荷的 flick 扫动是 **1 ms 一条 × 60 条**（19 个 flick 全部如此），
     *   而端内单条注入 p50 3.2 ms / p99 7.2 ms ⇒ 60 条要 192 ms，**物理上发不完**。
     *   旧实现「走到就发」于是把这 60 条在判定窗之前一口气刷完，中位提前 118 ms、最大 1.6 s
     *   ⇒ 判定窗内手指早已不动 ⇒ flick 判 Miss。这是 10-21 两轮 15 Miss 的根因。
     * ★ 也不能不抽稀：固定 4 ms 在 p99 上会累积落后（关键事件被拖到 25 ms）。
     */
    private static final double MOVE_GAP_MIN_MS = 3.0;
    private static final double MOVE_GAP_MAX_MS = 8.0;
    private static final double MOVE_GAP_MARGIN_MS = 0.8;

    private final Object proxy;
    private final Method inject;
    private final boolean useDueTime;
    private final boolean dropMoves;
    private final int maxFalse;
    private final Log log;
    /**
     * 注入模式（`Injector.MODE_*`）。**默认 ASYNC**，只有显式传进来才会变。
     *
     * ★ 为什么必须有这个字段（FACTS §165）：ASYNC 下 `late_ms` 只测「塞进队列」的成本，
     *   测不到游戏何时消费 ⇒ 端内 522 Miss 无法归因。把它做成参数而不是改常量，
     *   是为了能「一枪只改一处」地做 A/B，且老调用方逐位不变。
     */
    private final int injectMode;

    /**
     * 可选的中止钩子：返回 true 就在**下一条事件之前**跳出主循环。
     *
     * ★ 为什么需要它：主循环里 `LockSupport.parkNanos` 只是让出时间片，线程被
     *   `interrupt()` 也**不会抛异常**（parkNanos 不检查中断标志），所以「停」这个动作
     *   必须由外部谓词轮询。默认实现恒为 false ⇒ 老调用方行为逐位不变。
     * ★ 跳出后仍然走 `finally` 的 `releaseAll`：残留虚拟指针会让输入子系统永久吞 DOWN，
     *   这条不变量不能因为「中止路径」而破例。
     */
    private final java.util.function.BooleanSupplier stop;

    /**
     * 可选的逐帧追踪（null = 不追踪）。默认不建，由调用方注入。
     *
     * ★ 为什么用 setter 而不是构造参数：四个构造重载已经够多了，而追踪是**可选**
     *   的诊断旁路；加参数会让"不开追踪"的调用方被迫传 null，反而更容易漏。
     */
    private InjectTrace trace;

    /** 装上逐帧追踪（null = 卸掉）。必须在 {@link #run} 之前调用。 */
    public void setTrace(InjectTrace t) { this.trace = t; }

    /**
     * 追踪已经出过错 ⇒ 停用它。
     *
     * ★ 为什么要有这个开关：追踪是**诊断旁路**，绝不允许它把被测对象弄坏。
     *   但它也不能静默失败（那就是「跑了但没留下证据」）⇒ 第一次失败时记一行日志
     *   并永久停用，之后注入照常跑完 —— 既保住了这一枪的成绩，也让白跑可见。
     */
    private boolean traceBroken;

    public DevRun(Object proxy, Method inject, boolean useDueTime, boolean dropMoves,
                  int maxFalse, Log log) {
        this(proxy, inject, useDueTime, dropMoves, maxFalse, log, () -> false);
    }

    // 统计
    private int nEmit, nDropMove, nFalse, nOrphan, nSkipPast, nUnknown;
    /** MOVE 的提前量分布（正数 = 提前 ms）。端内 15 Miss 那次是 p50 +118 ms。 */
    private final List<Double> moveLeads = new ArrayList<>();
    private int nMoveEarly50;
    /** 抽稀掉的密集 MOVE（1 ms 采样里发不完的那些）。位置由后续事件承担，不算丢信息。 */
    private int nSkipDense;
    /** 单条注入成本的 EWMA（ms），决定 MOVE 抽稀间隔。 */
    private double costEwmaMs = 3.2;
    /** 静默保活间隔（ms）：距上一帧超过它就在空隙里补一帧同位置 MOVE（见 Gesture.keepalive）。 */
    private static final double KEEPALIVE_MS = 120.0;
    /** 上一帧真正发出的计划时刻（ms）；-1 = 还没发过。 */
    private double lastEmitT = -1.0;
    /** 补出来的保活帧数（进 trace 摘要，便于事后区分"真事件"与"保活"）。 */
    private int nKeepalive;
    /** 上一条**真正发出**的 MOVE 的计划时刻；NaN = 还没发过。 */
    private double lastMoveDueMs = Double.NaN;
    /**
     * **按指针**记的上一条真正发出的 MOVE 计划时刻（键 = pid）。
     *
     * ★★★ 2026-10-25 20:5x 修（moonlight IN 丢 3 个黄键的根因之一）：
     *   抽稀原来只看**全局** `lastMoveDueMs` ⇒ 两只手**同一毫秒**各要动一次时，
     *   第二只手那条 MOVE 被判"太密"直接丢掉，而它的位置要等**下一帧**才被带出去
     *   （实测下一帧在 1350 ms 之后）⇒ 那 1~2 个 Drag 必然 Miss。
     *   证据：`out/inject_trace_205001.csv` 在 t=140517 / 142069 两处，
     *   计划里 pid0 与 pid1 各有一条 MOVE，实际帧里只有 pid0 动了。
     *   ⇒ 抽稀必须**每指针独立**，且**只对 MOVE** 生效（DOWN/UP 绝不能被抽掉）。
     */
    private final java.util.HashMap<Integer, Double> lastMoveByPid = new java.util.HashMap<>();
    private long[] keyLagsUs = new long[1024];
    private int nLag;
    private final List<double[]> slowLags = new ArrayList<>();   // {t_ms, lag_ms}，>2 ms
    private String abortWhy;
    private long downTime;

    public DevRun(Object proxy, Method inject, boolean useDueTime, boolean dropMoves,
                  int maxFalse, Log log, java.util.function.BooleanSupplier stop) {
        // ★ 老签名一律走 ASYNC：这一行是「默认值 = 生产值」的落点，
        //   自测里有一条断言专盯它（改这里会让所有老调用方悄悄换模式）。
        this(proxy, inject, useDueTime, dropMoves, maxFalse, log, stop, Injector.MODE_ASYNC);
    }

    /**
     * @param injectMode {@link Injector#MODE_ASYNC}（生产固定用这个）或
     *                   {@link Injector#MODE_WAIT_FOR_FINISH}（等派发完成；11-01 起端内
     *                   已无入口触发它，常量保留是因为它同时是 Android 那两个 int 的映射）
     */
    public DevRun(Object proxy, Method inject, boolean useDueTime, boolean dropMoves,
                  int maxFalse, Log log, java.util.function.BooleanSupplier stop,
                  int injectMode) {
        if (!io.pgauto.inject.sched.InjectMode.isValid(injectMode)) {
            // 不认识的模式号必须炸：injectInputEvent 对非法 mode 的返回值没有文档保证，
            // 静默接受会变成「日志写着 wait、实际按别的语义跑」——比崩溃更难查。
            throw new IllegalArgumentException("injectMode 只能是 0/1，得到 " + injectMode);
        }
        this.proxy = proxy;
        this.inject = inject;
        this.useDueTime = useDueTime;
        this.dropMoves = dropMoves;
        this.maxFalse = maxFalse;
        this.log = log;
        this.injectMode = injectMode;

        this.stop = stop == null ? () -> false : stop;
    }

    /**
     * 常量对齐断言：{@link Gesture} 的动作码是字面量（为了 PC 上能编译），
     * 必须与真机 MotionEvent 一致，否则整条手势流会被系统当成非法事件。
     * **不一致就拒绝开火** —— 不猜、不降级。
     */
    public static String checkConstants() {
        int[][] pairs = {
                {Gesture.ACTION_DOWN, MotionEvent.ACTION_DOWN},
                {Gesture.ACTION_UP, MotionEvent.ACTION_UP},
                {Gesture.ACTION_MOVE, MotionEvent.ACTION_MOVE},
                {Gesture.ACTION_POINTER_DOWN, MotionEvent.ACTION_POINTER_DOWN},
                {Gesture.ACTION_POINTER_UP, MotionEvent.ACTION_POINTER_UP},
                {Gesture.ACTION_POINTER_INDEX_SHIFT, MotionEvent.ACTION_POINTER_INDEX_SHIFT},
        };
        String[] names = {"DOWN", "UP", "MOVE", "POINTER_DOWN", "POINTER_UP", "INDEX_SHIFT"};
        for (int i = 0; i < pairs.length; i++) {
            if (pairs[i][0] != pairs[i][1]) {
                return "动作码不一致: " + names[i] + " Gesture=" + pairs[i][0]
                        + " MotionEvent=" + pairs[i][1];
            }
        }
        return null;
    }

    /** 注入结果摘要（写给 result.json）。 */
    public JSONObject summary() {
        JSONObject o = new JSONObject();
        try {
            o.put("n_emit", nEmit);
            o.put("n_drop_move", nDropMove);
            o.put("n_false", nFalse);
            o.put("n_orphan", nOrphan);
            o.put("n_skip_past", nSkipPast);
            o.put("n_unknown_action", nUnknown);
            o.put("abort", abortWhy == null ? JSONObject.NULL : abortWhy);
            long[] s = java.util.Arrays.copyOf(keyLagsUs, nLag);
            java.util.Arrays.sort(s);
            o.put("n_key", nLag);
            o.put("lag_p50_ms", round3(pct(s, 0.50)));
            o.put("lag_p90_ms", round3(pct(s, 0.90)));
            o.put("lag_p99_ms", round3(pct(s, 0.99)));
            o.put("lag_max_ms", round3(nLag == 0 ? 0 : s[nLag - 1] / 1000.0));
            JSONArray slow = new JSONArray();
            for (double[] p : slowLags) {
                JSONObject e = new JSONObject();
                e.put("t_ms", p[0]);
                e.put("lag_ms", round3(p[1]));
                slow.put(e);
            }
            o.put("slow", slow);
            o.put("n_move", moveLeads.size());
            o.put("n_move_early_gt50", nMoveEarly50);
            o.put("move_late_tol_ms", MOVE_LATE_TOL_MS);
            o.put("n_skip_dense", nSkipDense);
            o.put("cost_ewma_ms", round3(costEwmaMs));
            o.put("move_gap_ms", round3(moveGapMs()));
            if (!moveLeads.isEmpty()) {
                double[] ls = new double[moveLeads.size()];
                for (int k = 0; k < ls.length; k++) ls[k] = moveLeads.get(k);
                java.util.Arrays.sort(ls);
                o.put("move_lead_p50_ms", round3(ls[ls.length / 2]));
                o.put("move_lead_p99_ms", round3(ls[(int) Math.min(ls.length - 1,
                        Math.round(0.99 * (ls.length - 1)))]));
                o.put("move_lead_max_ms", round3(ls[ls.length - 1]));
            }
        } catch (Exception ignore) {
            // JSONObject 的写入在本机不会失败；真失败也没有更好的兜底
        }
        return o;
    }

    private static double pct(long[] sortedUs, double q) {
        if (sortedUs.length == 0) return 0;
        int i = (int) Math.min(sortedUs.length - 1, Math.round(q * (sortedUs.length - 1)));
        return sortedUs[i] / 1000.0;
    }

    /** 墙钟→nanoTime 的换算常量（run 里算一次）。 */
    private long wallToNano;
    /** 零点（nanoTime 基准）。**可变**：两段锚会在注入途中把它换成实测值。 */
    private volatile long zeroNano;
    /** 注入线程句柄：rezero 时 unpark 它，让 sleepUntilT 立刻重算。 */
    private volatile Thread runner;
    private volatile double rezeroDeltaMs;
    private volatile int nRezero;
    /** 第一次 rezero 之前派发的事件数（= 两段锚里"用预测零点打出去"的条数，trace 要记）。 */
    private volatile int nBeforeRezero;
    private int nLateMove;
    /** 两段锚里"零点被挪到过去"的容忍：晚了 ≤60 ms 仍立即发（P 窗 ±80 ms）。 */
    private static final long PREROLL_LATE_TOL_MS = 60L;

    private static double round3(double v) { return Math.rint(v * 1000.0) / 1000.0; }

    /**
     * 主循环。
     *
     * @param cmds      已排序、坐标已变换的指令序列
     * @param zeroDevMs 音乐零点的**设备墙钟**（epoch ms，double 保留小数）
     * @return 实际开始注入时已跳过多少条（追赶式启动），由调用方写日志
     */
    public void run(List<Scheduler.Cmd> cmds, double zeroDevMs) {
        // 墙钟 → nanoTime 只在这里换一次；之后全部用单调时钟（墙钟可能被 NTP 拉走）
        wallToNano = System.currentTimeMillis() * 1_000_000L - System.nanoTime();
        zeroNano = (long) Math.rint(zeroDevMs * 1_000_000.0) - wallToNano;
        runner = Thread.currentThread();
        final long nanoBase = System.nanoTime();
        final long upBaseMs = SystemClock.uptimeMillis();

        Gesture g = new Gesture();
        int prevPrio = 0;
        try {
            prevPrio = android.os.Process.getThreadPriority(android.os.Process.myTid());
            android.os.Process.setThreadPriority(android.os.Process.THREAD_PRIORITY_URGENT_AUDIO);
        } catch (Throwable ignore) {
            // 提不上去只是抖动变大，不是致命错误（日志里有 prevPrio 可对照）
        }
        log.line("脑启动: 指令 " + cmds.size() + " 条，zero_dev_ms="
                + String.format(Locale.ROOT, "%.3f", zeroDevMs)
                + "，线程优先级 " + prevPrio + "→"
                + android.os.Process.THREAD_PRIORITY_URGENT_AUDIO
                + "，drop_moves=" + dropMoves + "，due_time=" + useDueTime
                + "，inject_mode=" + io.pgauto.inject.sched.InjectMode.name(injectMode)
                + "(" + injectMode + ")");

        long startNano = System.nanoTime();
        double firstPlayedT = -1;
        int i = 0;
        try {
            for (Scheduler.Cmd c : cmds) {
                if (stop.getAsBoolean()) {
                    abortWhy = "用户中止（在 t=" + String.format(Locale.ROOT, "%.0f", c.tMs())
                            + " ms 处）";
                    log.line("!! " + abortWhy + " ⇒ 停止注入并抬起手指");
                    break;
                }
                long due = dueOf(c.tMs());
                if (due < startNano) {
                    // ★ 追赶式启动：零点已过去（PC 在 zero+2.4s 才算准零点）⇒ 跳过来不及的
                    nSkipPast++;
                    i++;
                    continue;
                }
                // ★ B3：两段锚 rezero 可能把 due 挪到"刚刚过去"。晚了 ≤60 ms 立刻发（还在
                //   P 窗内），超过 60 ms 才跳过 —— 否则会把整段事件晚派成一片 Good/Miss。
                long lateByMs = (System.nanoTime() - due) / 1_000_000L;
                if (lateByMs > PREROLL_LATE_TOL_MS) {
                    nSkipPast++;
                    i++;
                    continue;
                }
                if (firstPlayedT < 0) firstPlayedT = c.tMs();
                // ★ 静默保活：谱面里常有"上千米秒没有任何事件"的空档，而音符可能正落在里面。
                //   真机证据（2026-10-25）：moonlight IN #1193 在 139.310 s，前后 1350 ms 无事件，
                //   手指位置完全正确却判 Miss ⇒ 在空隙里每 KEEPALIVE_MS 补一帧同位置 MOVE。
                if (lastEmitT >= 0 && g.activeCount() > 0) {
                    for (double t = lastEmitT + KEEPALIVE_MS; t < c.tMs() - 1.0; t += KEEPALIVE_MS) {
                        sleepUntilT(t);
                        Gesture.Frame kf = g.keepalive(t);
                        if (kf == null) break;
                        if (!emit(kf, dueOf(t), nanoBase, upBaseMs)) break;
                        lastEmitT = t;
                        nKeepalive++;
                    }
                }
                if (c.key()) {
                    sleepUntilT(c.tMs());
                    long nowN = System.nanoTime();
                    double lagMs = (nowN - due) / 1e6;
                    recordLag(c.tMs(), lagMs);
                    Gesture.Frame f = g.step(c);
                    if (f == null) { nOrphan++; i++; continue; }
                    if (!emit(f, due, nanoBase, upBaseMs)) break;
                    lastEmitT = c.tMs();
                } else {
                    // ★★ MOVE 也必须等到 due —— 这是 10-21 端内 15 Miss 的根因。
                    //   原实现「走到就发」：一组 MOVE 会在它前面那个关键事件之后**立刻**
                    //   全部发出，实测中位提前 118 ms、最大 1.6 s（离线复刻本载荷）。
                    //   Flick 靠「窗口 [t-30,t+30] 内的滑动」判定，手指提前跳到终点后再不动
                    //   ⇒ 窗口内没有位移 ⇒ 判 Miss。而 Tap/Hold 都是关键事件（按时）⇒ 全 Perfect。
                    //   症状因此极其迷惑：Early 0 / Late 0、lag p99 11.7 ms 全过、却 15 Miss。
                    //   ⛔ 别再退回「只在关键事件上等待」：PC 侧 full_run.py 对**每一条**事件
                    //      都 wait_until，两侧必须一致。
                    sleepUntilT(c.tMs());
                    long nowN = System.nanoTime();
                    double lateMs = (nowN - due) / 1e6;
                    Gesture.Frame f = g.step(c);      // 状态总是推进（见类注释 3）
                    if (f == null) { nOrphan++; i++; continue; }
                    // ① 太密 ⇒ 抽稀（1 ms 采样本来就发不完；位置由后续事件承担）
                    //    ⚠ 只抽 **MOVE**，且**按指针**判：否则"两只手同毫秒各动一次"会丢掉第二只，
                    //      它的位置要等下一帧才带出去（可能上千米秒后）⇒ 那一两个 Drag 必 Miss。
                    if (Gesture.masked(f.action()) == Gesture.ACTION_MOVE) {
                        Double lastSamePid = lastMoveByPid.get(c.pid());
                        if (lastSamePid != null && c.tMs() - lastSamePid < moveGapMs()) {
                            nSkipDense++;
                            i++;
                            continue;
                        }
                    }
                    // ② 迟到太多 ⇒ 丢（说明调度已经跟不上，这条位置已无意义）
                    //    ★ 2026-10-25：丢 MOVE 有代价 —— 指针位置会**停在上一次成功的位置**上，
                    //    Drag（黄键）是按"音符时刻手指在不在音符上"判的，位置陈旧就可能判不到
                    //    ⇒ 症状正是「0 Good / 0 Bad 的纯 Miss」（近 8 轮出现 3 次）。
                    //    所以这里**先计数**，丢不丢由 dropMoves 决定（默认已关闭，见 PlayService）。
                    if (lateMs > MOVE_LATE_TOL_MS) {
                        nLateMove++;
                        if (dropMoves) { nDropMove++; i++; continue; }
                    }
                    // ③ **止损**：迟到 > MOVE_LATE_DROP_MS 的 MOVE 直接丢（2026-10-26）。
                    //   真机证据：`玩具狂奏曲終焉 AT` 30~40 s 出现 100 ms 桶 54 个事件的 burst，
                    //   单条注入 ≈3 ms ⇒ 一桶 160 ms ⇒ 之后 30~60 ms 全晚（106 个音符判 Late-Good）。
                    //   迟到 40 ms 的 MOVE 里，位置信息已经过期（音符早判完或已过点）——继续发只会
                    //   把队列越拖越长。丢掉它，下一条（更新的位置）才有机会按时发出去。
                    //   ⛔ 只丢 MOVE：DOWN/UP/POINTER_* 是关键事件，迟到也必须发（否则整颗丢）。
                    if (lateMs > MOVE_LATE_DROP_MS
                            && Gesture.masked(f.action()) == Gesture.ACTION_MOVE) {
                        nDropMove++;
                        i++;
                        continue;
                    }
                    recordMoveLead(-lateMs);          // 负 = 迟到；不变量要求 ≥ -50
                    lastMoveDueMs = c.tMs();
                    if (Gesture.masked(f.action()) == Gesture.ACTION_MOVE) {
                        lastMoveByPid.put(c.pid(), c.tMs());
                    }
                    if (!emit(f, due, nanoBase, upBaseMs)) break;
                    lastEmitT = c.tMs();
                }
                i++;
                if (i % 200 == 0) {
                    log.line(String.format(Locale.ROOT,
                            "  · %d/%d 条，t=%.0fms，发 %d 丢 %d，false %d",
                            i, cmds.size(), c.tMs(), nEmit, nDropMove, nFalse));
                }
            }
        } finally {
            // ★ 无论正常结束、false 超阈、异常还是被 stop：都必须把手指抬起来
            int released = releaseAll(g, nanoBase, upBaseMs);
            try {
                android.os.Process.setThreadPriority(prevPrio);
            } catch (Throwable ignore) { /* 线程即将结束 */ }
            log.line("脑收尾: 共 " + i + " 条，发 " + nEmit + "，丢 MOVE " + nDropMove
                    + "，跳过(零点前) " + nSkipPast + "，孤儿 " + nOrphan
                    + "，false " + nFalse + "，补抬指针 " + released
                    + "，首条播放 t_ms=" + (firstPlayedT < 0 ? "-"
                    : String.format(Locale.ROOT, "%.0f", firstPlayedT)));
        }
    }

    /** 抬掉所有仍按下的指针；返回成功注入的抬指数。 */
    private int releaseAll(Gesture g, long nanoBase, long upBaseMs) {
        if (g.isEmpty()) return 0;
        int ok = 0;
        long due = System.nanoTime();
        for (Gesture.Frame f : g.releaseAll(0)) {
            if (emit(f, due, nanoBase, upBaseMs)) ok++;
        }
        return ok;
    }

    /**
     * 记一条 MOVE 的提前量。
     *
     * ★ 为什么必须单独记：key 事件的 lag 只看得到关键事件，**MOVE 完全不在其中** ——
     *   15 Miss 那一轮的 p99 11.7 ms「全过」正是因为量的是另一批事件。
     *   凡是「不变量」都要有对应的落盘指标，否则它只存在于注释里。
     */
    private void recordMoveLead(double leadMs) {
        if (moveLeads.size() < 20000) moveLeads.add(leadMs);
        if (leadMs > 50.0) nMoveEarly50++;
    }

    private double moveGapMs() {
        return Math.min(MOVE_GAP_MAX_MS,
                Math.max(MOVE_GAP_MIN_MS, costEwmaMs + MOVE_GAP_MARGIN_MS));
    }

    private void recordLag(double tMs, double lagMs) {
        if (nLag == keyLagsUs.length) keyLagsUs = java.util.Arrays.copyOf(keyLagsUs, nLag * 2);
        keyLagsUs[nLag++] = (long) Math.rint(lagMs * 1000.0);
        if (lagMs > 2.0 && slowLags.size() < 4000) {
            slowLags.add(new double[]{tMs, lagMs});
        }
    }

    /**
     * 睡到「这条指令的应发时刻」。
     *
     * ★ 与旧版（`static sleepUntil(long)`）的差别：**每一轮都重读 `zeroNano`**。
     *   两段锚里 `rezero()` 会在等待途中改零点，旧版会一直按旧目标睡到点 ⇒ 晚派 delta。
     *   同时 `rezero()` 会 `unpark` 本线程，`parkNanos` 立即返回后重新算剩余时间。
     * ★ 最后 2 ms 忙等保留（Thread.sleep(1) 实测尾部迟到 3~6 ms，P 窗只有 ±80 ms）。
     */
    private void sleepUntilT(double tMs) {
        while (true) {
            long target = dueOf(tMs);
            long now = System.nanoTime();
            if (now >= target) return;
            long rem = target - now;
            if (rem <= 2_000_000L) continue;      // 忙等（下一轮会重读零点）
            LockSupport.parkNanos(rem - 2_000_000L);
        }
    }

    private long dueOf(double tMs) {
        return zeroNano + (long) Math.rint(tMs * 1_000_000.0);
    }

    /**
     * 两段锚：用**实测零点**替换预测零点（可在注入进行中调用，线程安全）。
     *
     * ★ 设计：只换"之后要派发的事件"的时间基准；已经派发的（盲区那 1~2 个）保持预测时刻。
     * ⛔ 不要在这里动 `Gesture` 状态：手指位置与时间无关，换零点只影响调度。
     */
    public void rezero(double zeroDevMs) {
        double delta = zeroDevMs - (zeroNano + wallToNano) / 1_000_000.0;
        zeroNano = (long) Math.rint(zeroDevMs * 1_000_000.0) - wallToNano;
        rezeroDeltaMs = delta;
        nRezero++;
        Thread t = runner;
        if (t != null) LockSupport.unpark(t);
    }

    /** 零点被改过几次、最后一次的 delta（ms，正 = 实测比预测晚）。 */
    public int rezeroCount() { return nRezero; }

    public double rezeroDeltaMs() { return rezeroDeltaMs; }

    /** 用预测零点派发出的条数（0 = 这轮没预派）。 */
    public int beforeRezero() { return nBeforeRezero; }

    /**
     * 把一帧注入进去。
     *
     * @param dueNano 该事件的**计划时刻**（nanoTime 基准）。useDueTime 时拿它当
     *                MotionEvent.eventTime —— 游戏按事件时间戳判定，这样 3~7 ms 的
     *                注入耗时不会进判定（PC 侧因 send 只要 0.3 ms，不设此差别）。
     * @return false = 已经因为「返回值 false 超阈」中止，调用方必须停止循环
     */
    private boolean emit(Gesture.Frame f, long dueNano, long nanoBase, long upBaseMs) {
        long nowMs = SystemClock.uptimeMillis();
        long et = nowMs;
        if (useDueTime) {
            long want = upBaseMs + (dueNano - nanoBase) / 1_000_000L;
            et = Math.min(want, nowMs);      // 绝不发未来时间戳
        }
        if (nRezero == 0) nBeforeRezero++;
        if (Gesture.masked(f.action()) == Gesture.ACTION_DOWN) downTime = et;
        if (downTime > et) downTime = et;

        int n = f.count();
        MotionEvent.PointerProperties[] props = new MotionEvent.PointerProperties[n];
        MotionEvent.PointerCoords[] coords = new MotionEvent.PointerCoords[n];
        for (int k = 0; k < n; k++) {
            props[k] = new MotionEvent.PointerProperties();
            props[k].id = f.ids()[k];
            props[k].toolType = MotionEvent.TOOL_TYPE_FINGER;
            coords[k] = new MotionEvent.PointerCoords();
            coords[k].x = f.xs()[k];
            coords[k].y = f.ys()[k];
            coords[k].pressure = 1f;
            coords[k].size = 1f;
        }
        MotionEvent ev = MotionEvent.obtain(downTime, et, f.action(), n, props, coords,
                0, 0, 1f, 1f, 0, 0, InputDevice.SOURCE_TOUCHSCREEN, 0);
        ev.setSource(InputDevice.SOURCE_TOUCHSCREEN);
        long tInject = System.nanoTime();
        // ★ 落后量在这里自己算（而不是复用 run() 里的 lagMs）：releaseAll 收尾抬指也走
        //   emit，那条路径没有 run() 的 lagMs 可比，自己算才能让两类帧口径一致。
        double lateMs = (tInject - dueNano) / 1e6;
        try {
            Object r = inject.invoke(proxy, ev, injectMode);
            // 注入成本决定 MOVE 抽稀间隔（设备负载会变，不能写死）
            long costUs = (System.nanoTime() - tInject) / 1000L;
            double dtMs = costUs / 1000.0;
            costEwmaMs = 0.9 * costEwmaMs + 0.1 * dtMs;
            nEmit++;
            boolean injected = Boolean.TRUE.equals(r);
            if (trace != null && !traceBroken) {
                try {
                    trace.record(f, injected, costUs, lateMs, et, nowMs);
                } catch (Throwable t) {
                    traceBroken = true;
                    log.line("!! 帧追踪记录失败（已停用追踪，注入继续）："
                            + ShizukuProbe.brief(t));
                }
            }
            if (!injected) {
                nFalse++;
                // ★ 不是「偶尔失败」：mode 写错时会 100% 返回 false，且没有任何异常。
                //   阈值给得很低（默认 max(3, n/200)）就是为了在头几条就发现它。
                if (nFalse > maxFalse) {
                    abortWhy = "注入返回 false " + nFalse + " 次（阈 " + maxFalse + "）";
                    log.line("!! " + abortWhy + " ⇒ 立即中止（继续发只会整轮静默 0 分）");
                    return false;
                }
            }
            return true;
        } catch (InvocationTargetException e) {
            abortWhy = "注入抛异常: " + ShizukuProbe.brief(e.getTargetException());
            log.line("!! " + abortWhy + " ⇒ 中止");
            return false;
        } catch (Throwable t) {
            abortWhy = "注入失败: " + ShizukuProbe.brief(t);
            log.line("!! " + abortWhy + " ⇒ 中止");
            return false;
        } finally {
            ev.recycle();
        }
    }

    public static String describe(File payload, List<Scheduler.Cmd> cmds) {
        int nKey = 0;
        for (Scheduler.Cmd c : cmds) if (c.key()) nKey++;
        return payload.getName() + " → " + cmds.size() + " 条（关键 " + nKey + "），t_ms "
                + (cmds.isEmpty() ? "-" : String.format(Locale.ROOT, "%.0f ~ %.0f",
                cmds.get(0).tMs(), cmds.get(cmds.size() - 1).tMs()));
    }

    public int emitted() { return nEmit; }
    public int dropped() { return nDropMove; }
    public int falses() { return nFalse; }
    public int skippedPast() { return nSkipPast; }

    /** 状态机异常（子帧拼不出合法手势）的条数；非 0 说明载荷/手势逻辑有问题。 */
    public int orphans() { return nOrphan; }

    /** 迟到超过容忍阈的 MOVE 条数（= 若开 dropMoves 会被丢掉的量）。 */
    public int lateMoves() { return nLateMove; }

    /** 因"太密"被抽稀掉的 MOVE 条数（不影响关键事件）。 */
    public int denseSkipped() { return nSkipDense; }

    /** 静默空隙里补出来的保活 MOVE 条数（见 Gesture#keepalive）。 */
    public int keepalives() { return nKeepalive; }

    public String abortReason() { return abortWhy; }
}
