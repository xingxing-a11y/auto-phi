package io.pgauto.inject;

import android.os.SystemClock;
import android.view.InputDevice;
import android.view.MotionEvent;

import java.lang.reflect.Method;
import java.util.Arrays;

/**
 * 「手」的体检：注入成本 + 多指能力。
 *
 * 为什么要单独测，而不直接搬 10-20 的数字：
 *   10-20 那次是 {@code app_process} 进程**直连** system_server（uid 2000），
 *   实测单次 p50 0.51~0.63 ms / p99 1.87~3.27 ms。
 *   现在走 {@code ShizukuBinderWrapper}，多了一跳：
 *     App(uid 10188) → shizuku_server(uid 2000) → system_server
 *   每次调用是**两次同步 binder 往返**，成本结构完全不同，不能假设同值。
 *   S1 的两级调度（关键事件忙等、MOVE 尽力而为）预算全押在这个数上，
 *   拿错数就会得到「以为够用、实际 5.5 ms」的排程。
 *
 * 为什么要测多指：单指 tap 能通，不代表多指事件能被接受。
 *   Phigros 常态同时按 2~5 个键。多指走的是 {@code MotionEvent.obtain} 的另一组重载
 *   （要 PointerProperties[] / PointerCoords[]），构造错会得到 pointerCount=1 的事件，
 *   或者干脆被 InputDispatcher 丢弃 —— 两种都是「看起来注入成功、实际没按」。
 */
public final class InjectBench {

    private InjectBench() {}

    /** 一次统计的摘要。样本数、分位、极值都要留，只报均值会掩盖长尾。 */
    public static final class Stat {
        public final int n;
        public final double mean;
        public final double p50;
        public final double p99;
        public final double max;

        Stat(int n, double mean, double p50, double p99, double max) {
            this.n = n;
            this.mean = mean;
            this.p50 = p50;
            this.p99 = p99;
            this.max = max;
        }

        public String fmt(String label) {
            return String.format(java.util.Locale.ROOT,
                    "%s n=%d mean=%.3f p50=%.3f p99=%.3f max=%.3f ms",
                    label, n, mean, p50, p99, max);
        }
    }

    /**
     * @param nanos 单次耗时（纳秒），**必须非空**
     * @return 摘要；样本为空时返回 n=0 的零值（调用方自己判断）
     */
    public static Stat summarize(long[] nanos) {
        if (nanos == null || nanos.length == 0) {
            return new Stat(0, 0, 0, 0, 0);
        }
        long[] sorted = nanos.clone();
        Arrays.sort(sorted);
        double sum = 0;
        for (long v : sorted) sum += v;
        return new Stat(
                sorted.length,
                sum / sorted.length / 1e6,
                sorted[(int) Math.min(sorted.length - 1, Math.round(0.50 * (sorted.length - 1)))] / 1e6,
                sorted[(int) Math.min(sorted.length - 1, Math.round(0.99 * (sorted.length - 1)))] / 1e6,
                sorted[sorted.length - 1] / 1e6);
    }

    /**
     * 测单指点击的**调用成本**（不含事件分发到 App 的时间）。
     *
     * ★ 只测 {@code mode=0}（ASYNC）：这是生产要用的模式。
     *   {@code mode=2} 已被实测全拒（只返回 false 不抛异常），{@code mode=1} 会等事件真正
     *   分发完，阻塞时长取决于 UI 线程，跟排程器无关，测它没有指导意义。
     *
     * @param warmup 预热次数，不计入统计（JIT 未编译时头几次会是稳态的数倍）
     * @param n      统计样本数
     */
    public static String tapCost(Object proxy, Method inject, float x, float y, int warmup, int n) {
        if (proxy == null || inject == null) return "成本测试跳过：proxy / method 为 null";

        long[] downNs = new long[n];
        long[] upNs = new long[n];
        int failDown = 0, failUp = 0;

        for (int i = 0; i < warmup + n; i++) {
            boolean stat = i >= warmup;
            long t0 = SystemClock.uptimeMillis();
            MotionEvent d = MotionEvent.obtain(t0, t0, MotionEvent.ACTION_DOWN, x, y, 0);
            MotionEvent u = MotionEvent.obtain(t0, t0 + 10, MotionEvent.ACTION_UP, x, y, 0);
            try {
                long a = System.nanoTime();
                Object r1 = inject.invoke(proxy, d, Injector.MODE_ASYNC);
                long b = System.nanoTime();
                Object r2 = inject.invoke(proxy, u, Injector.MODE_ASYNC);
                long c = System.nanoTime();
                if (stat) {
                    downNs[i - warmup] = b - a;
                    upNs[i - warmup] = c - b;
                    if (!Boolean.TRUE.equals(r1)) failDown++;
                    if (!Boolean.TRUE.equals(r2)) failUp++;
                }
            } catch (Throwable t) {
                return "成本测试中断: " + ShizukuProbe.brief(t);
            } finally {
                d.recycle();
                u.recycle();
            }
        }
        return summarize(downNs).fmt("DOWN") + "  |  " + summarize(upNs).fmt("UP")
                + "  |  返回 false: down=" + failDown + " up=" + failUp;
    }

    /** 多指一次完整序列的结果。 */
    public static final class MultiResult {
        public final int pointers;
        public final int sent;
        public final int rejected;
        public final String error;   // null = 没抛异常
        public final double totalMs;

        MultiResult(int pointers, int sent, int rejected, String error, double totalMs) {
            this.pointers = pointers;
            this.sent = sent;
            this.rejected = rejected;
            this.error = error;
            this.totalMs = totalMs;
        }

        public String fmt() {
            return pointers + " 指：发 " + sent + " 条事件，返回 false " + rejected
                    + "（" + String.format(java.util.Locale.ROOT, "%.3f", totalMs) + " ms，总耗时）"
                    + (error == null ? "" : "，异常: " + error);
        }
    }

    /**
     * 注入一组真正的多指事件序列：DOWN → POINTER_DOWN×(k−1) → MOVE → POINTER_UP×(k−1) → UP。
     *
     * ★ 必须发完整序列，不能只发一条 pointerCount=3 的 ACTION_DOWN：
     *   InputDispatcher 要求指针的 down/up 成对出现（每个指针 id 先出现在
     *   POINTER_DOWN 里才能出现在 MOVE 里），凭空出现在 MOVE 里的 id 会被丢弃。
     *
     * @param pts 每个指针的 (x, y)，长度即手指数
     */
    public static MultiResult multiTouch(Object proxy, Method inject, float[][] pts) {
        if (proxy == null || inject == null) {
            return new MultiResult(pts == null ? 0 : pts.length, 0, 0, "proxy / method 为 null", 0);
        }
        int k = pts.length;
        MotionEvent.PointerProperties[] props = new MotionEvent.PointerProperties[k];
        for (int i = 0; i < k; i++) {
            props[i] = new MotionEvent.PointerProperties();
            props[i].id = i;
            props[i].toolType = MotionEvent.TOOL_TYPE_FINGER;
        }

        int sent = 0, rejected = 0;
        String error = null;
        long t0 = SystemClock.uptimeMillis();
        long start = System.nanoTime();

        try {
            // —— 按下：第一个用 ACTION_DOWN，其余用 ACTION_POINTER_DOWN 并带上「是第几个指针」
            for (int i = 0; i < k; i++) {
                int action = (i == 0)
                        ? MotionEvent.ACTION_DOWN
                        : MotionEvent.ACTION_POINTER_DOWN | (i << MotionEvent.ACTION_POINTER_INDEX_SHIFT);
                MotionEvent e = build(t0, t0 + i, action, i + 1, props, pts);
                try {
                    if (!Boolean.TRUE.equals(inject.invoke(proxy, e, Injector.MODE_ASYNC))) rejected++;
                    sent++;
                } finally {
                    e.recycle();
                }
            }
            // —— 按住期间给一条 MOVE：真实的 Phigros Hold 音符就是这个形态，
            //    也顺带验证「多指 MOVE 不会被丢」
            for (int rep = 0; rep < 3; rep++) {
                MotionEvent e = build(t0, t0 + k + rep, MotionEvent.ACTION_MOVE, k, props, pts);
                try {
                    if (!Boolean.TRUE.equals(inject.invoke(proxy, e, Injector.MODE_ASYNC))) rejected++;
                    sent++;
                } finally {
                    e.recycle();
                }
            }
            // —— 抬起：必须逆序，且最后一个用 ACTION_UP
            for (int i = k - 1; i >= 0; i--) {
                int action = (i == 0)
                        ? MotionEvent.ACTION_UP
                        : MotionEvent.ACTION_POINTER_UP | (i << MotionEvent.ACTION_POINTER_INDEX_SHIFT);
                MotionEvent e = build(t0, t0 + k + 3 + (k - 1 - i), action, i + 1, props, pts);
                try {
                    if (!Boolean.TRUE.equals(inject.invoke(proxy, e, Injector.MODE_ASYNC))) rejected++;
                    sent++;
                } finally {
                    e.recycle();
                }
            }
        } catch (java.lang.reflect.InvocationTargetException e) {
            error = ShizukuProbe.brief(e.getTargetException());
        } catch (Throwable t) {
            error = ShizukuProbe.brief(t);
        }
        double totalMs = (System.nanoTime() - start) / 1e6;
        return new MultiResult(k, sent, rejected, error, totalMs);
    }

    /**
     * 构造一条 pointerCount = count 的触摸事件。
     *
     * ⚠ 只能用这个 14 参数的重载；{@code obtain(..., x, y, ...)} 那一组永远只有一个指针，
     *   拿它做多指会得到 pointerCount=1 的事件（静默降级，不报错）。
     */
    private static MotionEvent build(long downTime, long eventTime, int action, int count,
                                     MotionEvent.PointerProperties[] props,
                                     float[][] pts) {
        MotionEvent.PointerCoords[] coords = new MotionEvent.PointerCoords[count];
        for (int i = 0; i < count; i++) {
            coords[i] = new MotionEvent.PointerCoords();
            coords[i].x = pts[i][0];
            coords[i].y = pts[i][1];
            coords[i].pressure = 1f;
            coords[i].size = 1f;
        }
        MotionEvent e = MotionEvent.obtain(
                downTime, eventTime, action, count,
                props, coords,
                0,                  // metaState
                0,                  // buttonState
                1f, 1f,             // xPrecision, yPrecision
                0,                  // deviceId
                0,                  // edgeFlags
                InputDevice.SOURCE_TOUCHSCREEN,
                0);                 // flags
        // obtain() 已经带了 source，但显式再设一次：injectInputEvent 在 mode=0 下
        // 会按 source 决定注入目标，source 为 0 的事件可能被静默丢弃。
        e.setSource(InputDevice.SOURCE_TOUCHSCREEN);
        return e;
    }
}
