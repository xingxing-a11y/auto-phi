import android.os.IBinder;

import android.os.SystemClock;
import android.view.InputEvent;
import android.view.MotionEvent;
import java.lang.reflect.Method;
import java.util.Arrays;

/**
 * 注入通道探针 v3：走通了「ServiceManager → IInputManager」之后，
 * 回答端内方案真正关心的问题 ——「一次 injectInputEvent 调用本身要多久，抖动多大」。
 *
 * 为什么必须测这个：
 *   谱面事件间隔中位 1.00 ms（demiurge 2125 条 / lacamp 6239 条实测）。
 *   如果单次注入耗时 > 1 ms，端内排程就必须做「提前量 + 队列」，而不是 sleep 到点直接发。
 *
 * 为什么全用反射：`IInputManager` / `IInputManager$Stub` 都是 @hide，
 *   公开 android.jar 里没有，直接 import 编译不过（这是 S1 实现时的第一个坑）。
 *
 * 安全性：只注 ACTION_MOVE、不注 DOWN/UP。没有前序 DOWN 的孤立 MOVE 不会生成点击，
 *   既验证 binder 调用被 system_server 接受，又不会误触按钮。
 */
public final class InjectProbe {

    public static void main(String[] argv) {
        int n = argv.length > 0 ? Integer.parseInt(argv[0]) : 200;
        int x = 800, y = 360;

        Object svc;
        Method inject;
        try {
            // ServiceManager 也是 @hide ⇒ 必须反射，不能写 ServiceManager.class
            IBinder binder = (IBinder) Class.forName("android.os.ServiceManager")
                    .getMethod("getService", String.class).invoke(null, "input");
            if (binder == null) {
                System.out.println("FATAL: input 服务为 null");
                return;
            }
            Class<?> stub = Class.forName("android.hardware.input.IInputManager$Stub");
            svc = stub.getMethod("asInterface", IBinder.class).invoke(null, binder);
            System.out.println("asInterface=" + (svc != null ? "OK" : "null"));
            inject = svc.getClass().getMethod("injectInputEvent", InputEvent.class, int.class);
        } catch (Throwable t) {
            System.out.println("FATAL: " + t + " cause=" + t.getCause());
            return;
        }
        System.out.println("method=" + inject.getName()
                + " uid=" + android.os.Process.myUid()
                + " sdk=" + android.os.Build.VERSION.SDK_INT);

        // mode: 0=AS_IS（异步立即返回） 2=WAIT_FOR_FINISH（等分发完成）
        for (int mode : new int[]{0, 2}) {
            long[] us = new long[n];
            int okCount = 0, exCount = 0, falseCount = 0;
            long t0 = SystemClock.uptimeMillis();
            for (int i = 0; i < n; i++) {
                MotionEvent ev = null;
                long a = System.nanoTime();
                try {
                    ev = MotionEvent.obtain(t0, t0 + i, MotionEvent.ACTION_MOVE,
                            x + (i & 3), y + (i & 3), 0);
                    Object r = inject.invoke(svc, ev, mode);
                    if (Boolean.TRUE.equals(r)) {
                        okCount++;
                    } else {
                        falseCount++;
                    }
                } catch (Throwable e) {
                    exCount++;
                    if (exCount == 1) {
                        System.out.println("  首次异常: " + e + " cause=" + e.getCause());
                    }
                } finally {
                    if (ev != null) {
                        ev.recycle();
                    }
                }
                us[i] = (System.nanoTime() - a) / 1000L;
            }
            report("mode=" + mode, us, okCount, falseCount, exCount);
        }
    }

    private static void report(String tag, long[] v, int ok, int fls, int ex) {
        long[] c = v.clone();
        Arrays.sort(c);
        long sum = 0;
        for (long z : c) {
            sum += z;
        }
        System.out.printf(
                "%s n=%d ok=%d refuse=%d ex=%d mean=%.1fus p50=%d p90=%d p99=%d max=%dus over1ms=%d%n",
                tag, c.length, ok, fls, ex, (double) sum / c.length,
                c[c.length / 2], c[(int) (c.length * 0.9)], c[(int) (c.length * 0.99)],
                c[c.length - 1], countOver1ms(c));
    }

    private static int countOver1ms(long[] c) {
        int k = 0;
        for (long x : c) {
            if (x > 1000L) {
                k++;
            }
        }
        return k;
    }
}
