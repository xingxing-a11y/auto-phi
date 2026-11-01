package io.pgauto.play;

import java.lang.reflect.Method;
import java.util.Locale;
import java.util.function.BooleanSupplier;

import io.pgauto.inject.Injector;

/**
 * 零点来源（`zero = 谱面 t=0 对应的设备墙钟时刻`，epoch ms）。
 *
 * M1 分两步走，接口不变：
 *   · **S1-c（本步）**：{@link CountdownTap} —— 倒计时 → 自拍 ▶ → `zero = 点击墙钟 + lead`。
 *     lead 是本曲实测常数（见 `PlayService.DEFAULT_LEAD_MS`），跨曲能差 300+ ms。
 *     这一步的判据是**链路**（待命 → 收到零点 → 注入 N 条），不是成绩。
 *   · **S1-d**：新增 `OnsetZero`（`io.pgauto.probe` 的 AudioPlaybackCapture + OnsetDetector），
 *     `zero = onset − sil_ref − L_dev`，lead 这个常数就不再参与计算。
 *
 * ★ 拆成接口而不是「在 Service 里写死一条路径」，是为了让 S1-d 只换一个实现、
 *   不动 `PlayService` 一行（零点来源可替换：onset 自听 / 点击 ▶ 时刻）。
 */
public interface ZeroSource {

    /**
     * 等零点。
     *
     * @return 零点（设备墙钟 epoch ms，double 保留小数）
     * @throws Stopped 用户在倒计时期间中止（**不是错误**，调用方按"已停止"处理）
     * @throws Exception 自拍失败等真错误
     */
    double await() throws Exception;

    /** 来源标记，写进日志（S1-d 会变成 `onset`）。**必须能区分 estimate 与实测**。 */
    String src();

    /**
     * 本轮的零点参数，一行 `k=v k=v`，由 `PlayService` 写进注入 trace 的 `# run` 行。
     *
     * ★ 为什么放在接口上（2026-10-25）：打完一枪之后，「这枪到底用哪个 `L_dev`/判据跑的」
     *   在 logcat（被游戏冲掉）与界面日志（只显示最后 16 行）里都取不到 ⇒ 只能靠进程启动
     *   参数推断。**产物必须自带参数**（§167 同一条规矩，此前只补了 inject_mode 一半）。
     * 默认空串 = 该实现没有参数可报（不写这一行）。
     */
    default String meta() {
        return "";
    }

    /** 用户中止（倒计时期间点了「停止」）。 */
    final class Stopped extends Exception {
        public Stopped(String why) { super(why); }
    }

    /** 点击器：返回 null = 成功，否则返回人类可读的失败原因。 */
    interface Tapper {
        String tap(float x, float y);
    }

    /** 真点：走 Shizuku 代理（`Injector.tap`）。 */
    final class ShizukuTap implements Tapper {

        private final Object proxy;
        private final Method inject;

        public ShizukuTap(Object proxy, Method inject) {
            this.proxy = proxy;
            this.inject = inject;
        }

        @Override
        public String tap(float x, float y) {
            String r = Injector.tap(proxy, inject, x, y);
            if (r == null) return "点击失败：Injector.tap 返回 null";
            // ★ 必须看返回里的两个布尔：mode 写错时 invoke **返回 false 而不抛异常**
            //   （10-20 实测 600/600 全 false）。只看"没抛异常"就会以为点成功了。
            if (r.startsWith("注入完成")) {
                if (r.contains("down=false") || r.contains("up=false")) {
                    return "点击被拒（injectInputEvent 返回 false ⇒ mode 可能写错）: " + r;
                }
                RunLog.line("点击 ▶ 成功：" + r);
                return null;
            }
            return r;
        }
    }

    /**
     * 倒计时 → 自拍 ▶ → `zero = 点击墙钟 + leadMs`。
     *
     * ★ 为什么必须留倒计时：本 App 的界面在打歌时是后台，用户要手动切到游戏并停在
     *   就绪界面。没有倒计时就没法把「切过去」和「按下 ▶」对齐。
     * ★ 点击墙钟取在**发起注入之前**：注入本身要 2~7 ms，取之后会把这段时间算进 lead。
     */
    final class CountdownTap implements ZeroSource {

        private final Tapper tapper;
        private final int countdownS;
        private final double leadMs;
        private final float x;
        private final float y;
        private final BooleanSupplier stop;

        public CountdownTap(Tapper tapper, int countdownS, double leadMs,
                            float x, float y, BooleanSupplier stop) {
            this.tapper = tapper;
            this.countdownS = Math.max(0, countdownS);
            this.leadMs = leadMs;
            this.x = x;
            this.y = y;
            this.stop = stop == null ? () -> false : stop;
        }

        @Override
        public double await() throws Exception {
            RunLog.phase("倒计时 " + countdownS + " s");
            long end = System.currentTimeMillis() + countdownS * 1000L;
            int last = Integer.MIN_VALUE;
            while (true) {
                if (stop.getAsBoolean()) throw new Stopped("倒计时期间中止");
                long left = end - System.currentTimeMillis();
                if (left <= 0) break;
                int s = (int) Math.ceil(left / 1000.0);
                if (s != last) {
                    last = s;
                    RunLog.line("倒计时 " + s + " s");
                }
                Thread.sleep(Math.min(200L, Math.max(1L, left)));
            }
            if (stop.getAsBoolean()) throw new Stopped("点击前中止");
            double tapWall = System.currentTimeMillis();
            String err = tapper.tap(x, y);
            if (err != null) throw new IllegalStateException("自拍失败：" + err);
            return tapWall + leadMs;
        }

        @Override
        public String src() {
            return "tap+" + String.format(Locale.ROOT, "%.0f", leadMs) + "ms";
        }

        @Override
        public String meta() {
            return String.format(Locale.ROOT, "src=tap lead_ms=%.3f", leadMs);
        }
    }
}
