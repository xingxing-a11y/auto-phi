package io.pgauto.play;

import android.graphics.Point;
import android.hardware.display.DisplayManager;
import android.os.Build;
import android.view.Display;

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.Locale;

/**
 * 三树同编的**运行期**自检。
 *
 * 为什么需要它（这是 M1 架构最大的单点风险）：
 *   本 app 不复刻 probe / injector 的源码，而是 `build.sh` 里把三棵树一起 `javac` + `d8`。
 *   好处是 `Scheduler` 只有一份、与 `payload_ref.py` 的逐位对拍不会失效（教训 9）；
 *   代价是「编译通过 ≠ 打进 dex ≠ 运行期能加载」。三种失败形态都**不会在编译期报错**：
 *     ① manifest 里写了 `.<name>` 简写 ⇒ 组件解析成本包名 ⇒ 运行时 ServiceNotFound；
 *     ② 只编了一棵树 ⇒ 本类编译就过不去（这个会报，最轻）；
 *     ③ d8 漏了某个 class 或 Shizuku jar ⇒ 运行期 NoClassDefFoundError。
 *   所以本类不做「打印类名」这种空转，而是**真的跑一次 onset 检出**：
 *   用合成 PCM（100 帧静音 + 50 帧 1 kHz 正弦）喂 `io.pgauto.probe.OnsetDetector`，
 *   断言恰好检出 1 个 onset。这同时验证了 S1-d 的核心零件在本 APK 里可用。
 */
public final class Components {

    /** 一行自检结果。`ok` 为假时 `detail` 必须说明**为什么**，不许只写"失败"。 */
    public static final class Item {
        public final boolean ok;
        public final String name;
        public final String detail;
        /**
         * true = **本 app 自己的接线**（失败就是缺陷，必须修）；
         * false = **外部环境状态**（Shizuku 有没有在跑、屏幕多大 —— 失败只是提示，
         * 不是这个 APK 的问题）。不区分的话，「Shizuku 没启动」会让界面显示
         * 「5/6 通过」看起来像一个 bug，而它其实完全正常。
         */
        public final boolean required;

        Item(boolean ok, String name, String detail) {
            this(ok, name, detail, true);
        }

        Item(boolean ok, String name, String detail, boolean required) {
            this.ok = ok;
            this.name = name;
            this.detail = detail;
            this.required = required;
        }

        @Override
        public String toString() {
            String mark = ok ? "✓ " : (required ? "✗ " : "· ");
            return mark + name + "：" + detail;
        }
    }

    private Components() {}

    // ★ 接线项（失败=缺陷）直接用 `new Item(ok, name, detail)` 三参构造 —— 那是 required=true。
    //   原来还有个 `chk()` 工厂方法，2026-11-01 扫死代码时删掉了（所有项都改走构造器后没人调它）。

    /** 环境项：Shizuku 有没有在跑、屏幕多大 —— 失败只是提示，不是本 APK 的缺陷。 */
    private static Item env(boolean ok, String name, String detail) {
        return new Item(ok, name, detail, false);
    }

    /**
     * 跑全部自检。**不要在主线程调用**（合成 PCM 那项约 150×480 次循环，虽小但没必要）。
     * 任何一项抛异常都被兜住并记成该项失败 —— 自检自己崩掉比自检不跑更坑。
     */
    public static List<Item> runAll(android.content.Context ctx) {
        List<Item> out = new ArrayList<>();
        out.add(buildInfo());
        out.add(deviceSize(ctx));
        out.add(actionCodes());
        out.add(onsetCheck());
        out.add(schedulerPresent());
        out.add(shizukuState(ctx));
        return Collections.unmodifiableList(out);
    }

    /** 一行汇总：`3/3 接线通过 · 环境 2/3`。界面直接显示。 */
    public static String headline(List<Item> items) {
        int reqOk = 0, reqN = 0, envOk = 0, envN = 0;
        for (Item i : items) {
            if (i.required) {
                reqN++;
                if (i.ok) reqOk++;
            } else {
                envN++;
                if (i.ok) envOk++;
            }
        }
        String s = reqN == 0 ? "无接线项" : (reqOk + "/" + reqN
                + (reqOk == reqN ? " 接线通过" : " 接线有失败"));
        if (envN > 0) s += " · 环境 " + envOk + "/" + envN;
        return s;
    }

    // ─────────────────────────── 各项 ───────────────────────────

    private static Item buildInfo() {
        return env(true, "构建",
                "SDK " + Build.VERSION.SDK_INT + "（" + Build.VERSION.RELEASE + "）· "
                        + Build.MANUFACTURER + " " + Build.MODEL);
    }

    /**
     * 显示尺寸。**换机头号杀手**（教训 2），所以只显示、不做判断：
     * 判定「载荷目标 vs 设备尺寸」要等 S1-c 真正调 `Scheduler.build(dstW, dstH, fit)`
     * 时才知道口径（`fit=height` 用长边还是短边），此处猜一个就是制造 bug。
     */
    private static Item deviceSize(android.content.Context ctx) {
        try {
            DisplayManager dm = (DisplayManager) ctx.getSystemService(android.content.Context.DISPLAY_SERVICE);
            if (dm == null) return env(false, "显示尺寸", "拿不到 DisplayManager");
            Display d = dm.getDisplay(Display.DEFAULT_DISPLAY);
            if (d == null) return env(false, "显示尺寸", "拿不到默认 Display");
            Point p = new Point();
            d.getRealSize(p);
            if (p.x <= 0 || p.y <= 0) {
                return env(false, "显示尺寸", "读到非法值 " + p.x + "x" + p.y);
            }
            int longSide = Math.max(p.x, p.y), shortSide = Math.min(p.x, p.y);
            return env(true, "显示尺寸",
                    "竖屏 " + p.x + "x" + p.y + " ⇒ 横屏 " + longSide + "x" + shortSide
                            + " · densityDpi " + ctx.getResources().getDisplayMetrics().densityDpi);
        } catch (Throwable t) {
            return env(false, "显示尺寸", brief(t));
        }
    }

    /** 注入用的 6 个动作码必须与 `MotionEvent` 一致；不一致整条手势流会被系统当非法事件。 */
    private static Item actionCodes() {
        try {
            String err = io.pgauto.inject.DevRun.checkConstants();
            return err == null
                    ? new Item(true, "注入动作码", "6 个 Gesture 常量与 MotionEvent 一致")
                    : new Item(false, "注入动作码", err);
        } catch (Throwable t) {
            return new Item(false, "注入动作码", "DevRun 加载失败：" + brief(t));
        }
    }

    /**
     * 真的喂一段合成 PCM 给 `OnsetDetector`：100 帧数字静音 + 50 帧 1 kHz 满幅正弦。
     * 期望**恰好 1 个** onset。多于 1 说明持续段被反复触发（`riseRatio` 失效），
     * 0 个说明漏检 —— 两种都会让 S1-d 的零点定不出来，必须在装机第一次就知道。
     */
    private static Item onsetCheck() {
        final int rate = 48000;
        final int frameMs = 10;
        final int frameSamples = rate * frameMs / 1000;      // 480
        final int silentFrames = 100;                        // 1.0 s 静音
        final int loudFrames = 50;                           // 0.5 s 正弦
        try {
            io.pgauto.probe.OnsetDetector od = new io.pgauto.probe.OnsetDetector(rate);
            if (od.hopSamples() != frameSamples) {
                return new Item(false, "onset 检出",
                        "hopSamples=" + od.hopSamples() + "，期望 " + frameSamples
                                + "（" + rate + " Hz / " + frameMs + " ms）");
            }
            short[] buf = new short[frameSamples * 2];       // 交错立体声
            for (int f = 0; f < silentFrames + loudFrames; f++) {
                boolean loud = f >= silentFrames;
                for (int i = 0; i < frameSamples; i++) {
                    short v = 0;
                    if (loud) {
                        v = (short) Math.round(12000.0
                                * Math.sin(2.0 * Math.PI * 1000.0 * i / rate));
                    }
                    buf[2 * i] = v;
                    buf[2 * i + 1] = v;
                }
                od.push(buf, buf.length);
            }
            int n = od.onsetCount();
            long frames = od.frameIndex();
            if (frames != silentFrames + loudFrames) {
                return new Item(false, "onset 检出",
                        "消费帧数 " + frames + "，期望 " + (silentFrames + loudFrames));
            }
            if (n != 1) {
                return new Item(false, "onset 检出",
                        "合成信号检出 " + n + " 个 onset，期望 1（0=漏检，>1=持续段重复触发）");
            }
            return new Item(true, "onset 检出",
                    "合成信号（1.0 s 静音 + 0.5 s 1 kHz）恰好 1 个 onset，"
                            + frames + " 帧 / hop " + od.hopSamples() + " 样本");
        } catch (Throwable t) {
            return new Item(false, "onset 检出", brief(t));
        }
    }

    /** 排程器与加密层必须在 dex 里（`Scheduler.build` 是 S1-c 的主要入口）。 */
    private static Item schedulerPresent() {
        try {
            Class<?> sched = io.pgauto.inject.sched.Scheduler.class;
            Class<?> dens = io.pgauto.inject.sched.Densify.class;
            Class<?> gest = io.pgauto.inject.sched.Gesture.class;
            return new Item(true, "排程器",
                    sched.getSimpleName() + " / " + dens.getSimpleName() + " / "
                            + gest.getSimpleName() + " 均在 dex 内");
        } catch (Throwable t) {
            return new Item(false, "排程器", "类加载失败：" + brief(t));
        }
    }

    /**
     * Shizuku 状态。这里**只查不说**（不 requestPermission）：授权流程属于 S1-c 的界面，
     * S1-b 只需要让「装完打开就能看到有没有手」这件事成立。
     */
    private static Item shizukuState(android.content.Context ctx) {
        try {
            boolean binder = rikka.shizuku.Shizuku.pingBinder();
            if (!binder) {
                return env(false, "Shizuku",
                        "binder 未就绪（Shizuku 服务没跑，或本 app 未启动时 Shizuku 在停止状态）");
            }
            int perm = rikka.shizuku.Shizuku.checkSelfPermission();
            if (perm != android.content.pm.PackageManager.PERMISSION_GRANTED) {
                return env(false, "Shizuku",
                        "binder 就绪，但 API 权限未授予（S1-c 会弹授权）");
            }
            // 读一个 probe 的 volatile 字段：常量会被 d8 内联，读 volatile 才能证明
            // "probe 那棵树真的进了 dex 且字段可访问"，而不是编译器把字面量抄过来了。
            boolean recRunning = io.pgauto.probe.RecorderService.sRunning;
            return env(true, "Shizuku",
                    "binder 就绪、权限已授予（录音服务 sRunning=" + recRunning + "）");
        } catch (Throwable t) {
            return env(false, "Shizuku", brief(t));
        }
    }

    private static String brief(Throwable t) {
        String m = t.getMessage();
        return t.getClass().getSimpleName() + (m == null ? "" : ": " + m);
    }

    /** 供界面显示的一行摘要：只列**接线项**失败（环境项另有一行）。 */
    public static String oneLine(List<Item> items) {
        StringBuilder bad = new StringBuilder();
        StringBuilder env = new StringBuilder();
        for (Item i : items) {
            if (i.ok) continue;
            StringBuilder t = i.required ? bad : env;
            t.append(t.length() == 0 ? "" : "、").append(i.name);
        }
        if (bad.length() > 0) return "接线失败：" + bad;
        if (env.length() > 0) return "接线全部通过；环境待就绪：" + env;
        return String.format(Locale.ROOT, "接线与环境全部就绪（%d 项）", items.size());
    }
}
