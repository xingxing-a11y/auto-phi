import android.os.SystemClock;
import java.util.Arrays;

/**
 * 端内排程精度探针：在 shell(uid 2000) 身份下，用 app_process 起，
 * 量「1 ms tick」能不能稳定跑出来。
 *
 * 为什么必须在设备上实测而不是查文档：
 *   端内方案的全部收益来自「去掉 PC→adb→server 的链路」，
 *   剩下的抖动只有调度器给的。这个抖动量级决定要不要 busy-wait，
 *   也决定 1 ms 间隔的谱面到底能不能端内复现 —— 查不到，只能测。
 *
 * 三种实现对比：
 *   sleep1        Thread.sleep(1)                —— 最省 CPU，但 Linux 睡眠粒度 ~1~10 ms
 *   spin-uptime   SystemClock.uptimeMillis() 忙等 —— 1 ms 量化，理论上刚好
 *   spin-nano     System.nanoTime() 忙等         —— 亚毫秒，能看真实尾部
 */
public final class TickerProbe {

    public static void main(String[] argv) {
        int n = argv.length > 0 ? Integer.parseInt(argv[0]) : 4000;
        System.out.println("sdk=" + android.os.Build.VERSION.SDK_INT
                + " abi=" + Arrays.toString(android.os.Build.SUPPORTED_ABIS)
                + " n=" + n);

        // 预热：让 JIT / 页表热起来，否则第一轮全是冷启动噪声
        runSleep(n / 10);
        runSpin(n / 10);

        System.out.println("-- A: Thread.sleep(1) --");
        runSleep(n);
        System.out.println("-- B: busy-wait uptimeMillis --");
        runSpin(n);
        System.out.println("-- C: busy-wait nanoTime --");
        runNano(n);
    }

    private static void runSleep(int n) {
        long[] late = new long[n];
        for (int i = 0; i < n; i++) {
            long t0 = SystemClock.uptimeMillis();
            try {
                Thread.sleep(1);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
            }
            late[i] = SystemClock.uptimeMillis() - t0 - 1;
        }
        report("sleep1", late);
    }

    private static void runSpin(int n) {
        long[] late = new long[n];
        long target = SystemClock.uptimeMillis();
        for (int i = 0; i < n; i++) {
            target += 1;
            while (SystemClock.uptimeMillis() < target) {
                Thread.onSpinWait();
            }
            late[i] = SystemClock.uptimeMillis() - target;
        }
        report("spin-uptime_ms", late);
    }

    private static void runNano(int n) {
        long[] late = new long[n];
        long target = System.nanoTime();
        for (int i = 0; i < n; i++) {
            target += 1_000_000L;
            while (System.nanoTime() < target) {
                Thread.onSpinWait();
            }
            late[i] = (System.nanoTime() - target) / 1000L;   // us
        }
        report("spin-nano_us", late);
    }

    private static void report(String tag, long[] v) {
        long[] c = v.clone();
        Arrays.sort(c);
        long sum = 0;
        for (long x : c) {
            sum += x;
        }
        System.out.printf(
                "%s n=%d mean=%.2f p50=%d p90=%d p99=%d p999=%d max=%d over1ms=%d%n",
                tag, c.length, (double) sum / c.length,
                at(c, 0.50), at(c, 0.90), at(c, 0.99), at(c, 0.999), c[c.length - 1],
                countOver(c, tag.endsWith("_us") ? 1000L : 1L));
    }

    private static long at(long[] c, double q) {
        int i = (int) (c.length * q);
        return c[Math.min(i, c.length - 1)];
    }

    private static int countOver(long[] c, long thr) {
        int k = 0;
        for (long x : c) {
            if (x > thr) {
                k++;
            }
        }
        return k;
    }
}
