package io.pgauto.play;

import android.util.Log;

import java.text.SimpleDateFormat;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Date;
import java.util.List;
import java.util.Locale;

/**
 * 进程内运行日志 + 阶段状态。**只在内存里，不落盘。**
 *
 * ★ 为什么不用文件通道（`InjectService` 那套 arm/run/result.json）：
 *   那是 PC ↔ App 之间的通道，跨进程才需要落盘。M1 端内闭环里「谁点火」和「谁注入」
 *   在**同一个进程**，用静态对象传递就够了，而且额外拿到两个好处：
 *     ① 零残留 —— 没有文件就没有「异常退出没删掉」这种形态；
 *     ② 没有轮询延迟 —— 界面 400 ms 刷一次，日志实时可见。
 *
 * ★ 为什么不落盘也不可惜：需要事后分析的是**注入结果**（条数 / lag 分位 / 中止原因），
 *   而这些在 `DevRun.summary()` 里，PC 侧同一套口径已在 `history` 里记了 190+ 轮。
 *   端内版一旦要长期归档，M2 会走「结算页读分」那条路，不是靠日志文件。
 *
 * ⚠ 本类所有方法都是线程安全的（注入线程写、界面线程读），但**不保证顺序跨线程可见**：
 *   `tail()` 拿到的是一致性快照，不承诺"绝对最新"。对一个给人看的日志足够了。
 */
public final class RunLog {

    public static final String TAG = "PgPlay";

    /** 环形容量。够装完整一轮（bounded_in 全量约 40 行，stop 时最多再多几十行）。 */
    private static final int CAP = 600;

    private static final ArrayDeque<String> LINES = new ArrayDeque<>(CAP);
    private static volatile String phase = "空闲";
    private static volatile boolean running;
    private static volatile PhaseListener listener;

    /** 阶段变化回调（服务用它刷新通知栏，因为 `ZeroSource` 也会改阶段）。 */
    public interface PhaseListener { void onPhase(String p); }

    private RunLog() {}

    public static void setListener(PhaseListener l) { listener = l; }

    /** 一条普通日志：带毫秒时间戳写进环里，同时进 logcat（`adb logcat -s PgPlay`）。 */
    public static void line(String s) {
        String ts = new SimpleDateFormat("HH:mm:ss.SSS", Locale.ROOT).format(new Date());
        synchronized (LINES) {
            if (LINES.size() >= CAP) LINES.removeFirst();
            LINES.addLast(ts + "  " + s);
        }
        Log.i(TAG, s);
    }

    /** 阶段切换：既改状态（界面顶部大字用），也留一行痕迹（便于数「待命→零点→注入」三段）。 */
    public static void phase(String p) {
        phase = p;
        line("=== " + p + " ===");
        notifyPhase(p);
    }

    /**
     * 只改阶段（顶部大字 + 通知栏），**不往环形缓冲写行**。
     *
     * ★ 2026-11-01：专给倒计时用。以前倒计时每秒写一行，界面只显示最后 16 行 ⇒
     *   一轮开头 9 行里 8 行是「倒计时 7 s / 6 s / …」，把「收到零点」「K 已并入平均」
     *   这些真正要看的挤出去了。而顶部大字本来就是逐秒跳的，日志里是纯重复。
     * ⚠ 通知栏**要跟着刷**：切到游戏里时那是唯一能看到的进度，别为了省行数把它停了。
     */
    public static void phaseQuiet(String p) {
        phase = p;
        notifyPhase(p);
    }

    private static void notifyPhase(String p) {
        PhaseListener l = listener;
        if (l != null) {
            try {
                l.onPhase(p);
            } catch (Throwable ignore) {
                // 通知栏刷新失败不能影响注入；日志里已经有这一行了
            }
        }
    }

    public static String phase() { return phase; }

    /** 环里当前有多少行（界面显示「共 N 行」用）。 */
    public static int size() {
        synchronized (LINES) { return LINES.size(); }
    }

    public static void setRunning(boolean b) { running = b; }

    public static boolean running() { return running; }

    /** 清空（每次开火前调，避免上一轮的日志把这一轮的判据淹掉）。 */
    public static void clear() {
        synchronized (LINES) { LINES.clear(); }
        phase = "空闲";
    }

    /** 最近 `n` 行（时间正序）。 */
    public static List<String> tail(int n) {
        synchronized (LINES) {
            int skip = Math.max(0, LINES.size() - Math.max(1, n));
            List<String> out = new ArrayList<>(LINES.size() - skip);
            int i = 0;
            for (String s : LINES) {
                if (i++ < skip) continue;
                out.add(s);
            }
            return out;
        }
    }

    /**
     * 全部行（时间正序）。
     *
     * ★ 2026-11-01：日志面板改成「固定高度 + 内部滑动看全这一轮」，所以不能再按行数截断
     *   （旧的 `tail(2)` / `tail(16)` 已删）。环形容量 {@link #CAP} 仍是内存护栏，不是显示上限
     *   —— 一轮实际约 40 行，碰不到 600。
     * ⚠ 与 {@link #size()} 分开取会拿到两个时刻的快照；界面要「行数 + 内容」一起用就调这个，
     *   别写成 `tail(size())`。
     */
    public static List<String> all() {
        synchronized (LINES) {
            return new ArrayList<>(LINES);
        }
    }
}
