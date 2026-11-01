package io.pgauto.play;

import android.content.Context;
import android.hardware.display.DisplayManager;
import android.view.Display;
import android.view.Surface;

import java.util.Locale;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;

import io.pgauto.inject.ShizukuProbe;

/**
 * 「学习落点」：开一个窗口，等用户自己在游戏里点一下 ▶，把**第一次**按下的位置记下来。
 *
 * <h3>口径（2026-10-31 用户定，别再改）</h3>
 * <ul>
 *   <li>15 秒窗口内，**第一次** DOWN 即有效点，之后所有点击忽略（{@link FirstDown}）；</li>
 *   <li>不判"有没有听到音乐"（选曲页本身有 BGM，判据不可靠；用户明确要求去掉）；</li>
 *   <li>没等到点击 ⇒ **不落盘**，只提示。</li>
 * </ul>
 *
 * <h3>坐标怎么换算（这是唯一需要"事实"的地方）</h3>
 * 触摸设备报的是**面板自然方向**的坐标（本机 0..719 × 0..1609），游戏横屏 1610×720。
 * 旋转角取系统给的值（`Display.getRotation()`），镜像**假定不翻转**
 * —— 正面触摸屏的驱动已按面板方向处理过。这个假定不是靠嘴保证的：
 * 换算出来的点必须落在**屏幕右下角**（▶ 按钮的实际位置），否则 {@link #run} 会在结果里带警告，
 * 让我们在第一次真机测试时立刻看出映射错了（`AxisMapSelfTest.t02` 用同一组数钉住了 rot90 的公式）。
 *
 * <h3>为什么不做"注入已知点反解映射"</h3>
 * 那样会**真的点一次屏幕**：如果点在 ▶ 上，游戏就直接开始播放了，用户后面点的那一下会落在游玩界面。
 * 代价大于收益。改用系统旋转 + 右下角校验，零副作用。
 */
public final class LearnOnce {

    /** 窗口长度：15 s（用户口径）。够切 App + 找到 ▶。 */
    public static final int WINDOW_MS = 15_000;

    /** 学到的点离出厂比例点超过这么多像素，就在结果里带警告（很可能是映射错了或用户点错地方）。 */
    private static final int FAR_PX = 300;

    public static final class Result {
        public final boolean ok;
        public final String msg;

        Result(boolean ok, String msg) {
            this.ok = ok;
            this.msg = msg;
        }
    }

    /**
     * 准备好了（设备 + 轴映射）。**必须在按按钮那一刻算**，不能等 lead 结束再算。
     *
     * ★ 为什么拆两段（2026-10-31 第一次真机实测后改）：
     *   原来 run() 里是「sleep 4 s → getevent -p → 起监听」。用户看到提示"4 秒后开始监听"，
     *   自然就在第 4 秒点 —— 那正好撞上 `-p` 那次进程启动（几十~一百毫秒）。
     *   点击落在这一段里就永远收不到，而界面只会说"没等到点击"。
     *   现在把 `-p` 提到按按钮那一刻，第 4 秒一到就直接进监听循环。
     */
    public static final class Prep {
        final TouchDevices.Dev dev;
        /** 仅用于日志：`Display.getRotation()`。**不再拿它算坐标**（见 AxisMap.pickForStartButton）。 */
        final int displayRotation;
        final String err;

        Prep(TouchDevices.Dev dev, int displayRotation, String err) {
            this.dev = dev;
            this.displayRotation = displayRotation;
            this.err = err;
        }
    }

    private LearnOnce() {
    }

    /** 解析触摸设备 + 建轴映射。失败时 {@link Prep#err} 非 null。 */
    public static Prep prepare(Context ctx, int screenW, int screenH) {
        if (screenW <= 0 || screenH <= 0) {
            return new Prep(null, 0, "拿不到屏幕尺寸，未记录（重启 App 再试）");
        }
        try {
            String dump = TouchSniff.execReadAll(TouchSniff.service(),
                    new String[]{"/system/bin/getevent", "-p"});
            TouchDevices.Dev dev = TouchDevices.pick(dump);
            int rotation = rotationDeg(ctx);
            // ★ 这里只**记录**显示旋转，不再用它算坐标：那一刻前台是我们自己的 App，
            //   拿到的方向是 App 的方向而不是游戏的方向（10-31 真机：拿到 0，实际要 90）。
            RunLog.line("学习落点：设备 " + dev + " · 显示旋转=" + rotation
                    + "°（仅参考，映射由点击位置反推）· 屏幕 " + screenW + "x" + screenH);
            return new Prep(dev, rotation, null);
        } catch (Throwable t) {
            String why = ShizukuProbe.brief(t);
            RunLog.line("学习落点失败（准备）：" + why);
            return new Prep(null, 0, "学习落点失败：" + why);
        }
    }

    /**
     * 开窗监听（阻塞，最长 `windowMs`），**必须在后台线程调用**。
     *
     * @param screenW 横屏宽（与 `landscapeSize()` 同口径）
     * @param screenH 横屏高
     */
    public static Result listen(Context ctx, Prep prep, int screenW, int screenH, long windowMs) {
        if (prep.err != null) return new Result(false, prep.err);
        final CountDownLatch latch = new CountDownLatch(1);
        final int[] hit = new int[]{-1, -1};      // rawX, rawY
        TouchSniff sniff = new TouchSniff();
        try {
            sniff.start(prep.dev.path, ev -> {
                if (ev[0] == FirstDown.ACT_DOWN && latch.getCount() > 0) {
                    hit[0] = ev[1];
                    hit[1] = ev[2];
                    latch.countDown();            // 只认第一次：后续点击 latch 已归零，不会再改 hit
                }
            });
        } catch (Throwable t) {
            sniff.stop();
            String why = ShizukuProbe.brief(t);
            RunLog.line("学习落点失败（起监听）：" + why);
            return new Result(false, "学习落点失败：" + why);
        }

        boolean got;
        try {
            got = latch.await(windowMs, TimeUnit.MILLISECONDS);
        } catch (InterruptedException ie) {
            Thread.currentThread().interrupt();
            got = false;
        } finally {
            sniff.stop();                       // ⛔ 窗口结束必须停，别常驻
        }

        if (!got) {
            // ★ 把「收到多少」写进结论：rawLines=0 ⇒ 管道里根本没数据（起不来 / 被 stdio 缓冲）；
            //   rawLines>0 但 events=0 ⇒ 解析不出（设备选错 / 格式不认识）。两种修法完全不同。
            RunLog.line("学习落点：窗口内没等到点击 ⇒ 未记录（rawLines=" + sniff.rawLines()
                    + " events=" + sniff.events() + "）");
            return new Result(false, (windowMs / 1000) + " 秒内没等到点击，未记录（收到 "
                    + sniff.rawLines() + " 行触摸输出："
                    + hint(sniff.rawLines(), sniff.events()) + "）");
        }

        int rawX = hit[0];
        int rawY = hit[1];
        // ★ 用点击本身反推定死旋转：只有能让该点落进「开始按钮区」（右下角）的旋转才算候选，
        //   且必须恰好一个。定不下来就**不落盘**，并把原因原样告诉用户（不猜一个糊过去）。
        AxisMap.Pick pick = AxisMap.pickForStartButton(rawX, rawY,
                prep.dev.maxX, prep.dev.maxY, screenW, screenH);
        if (pick.map == null) {
            RunLog.line("学习落点：raw(" + rawX + "," + rawY + ") 定不下映射 ⇒ 未记录（"
                    + pick.why + "）");
            return new Result(false, "未记录：" + pick.why);
        }
        AxisMap map = pick.map;
        if (!map.inRange(rawX, rawY)) {
            RunLog.line("⚠ 学习落点：raw 越界（" + rawX + "," + rawY + "）⇒ 量程可疑");
        }
        int[] s = map.rawToScreen(rawX, rawY);
        RunLog.line(String.format(Locale.ROOT,
                "学习落点：第一次点击 raw(%d,%d) ⇒ 屏幕(%d,%d) · %s"
                        + "（rawLines=%d events=%d 显示旋转=%d°）",
                rawX, rawY, s[0], s[1], map.describe(), sniff.rawLines(), sniff.events(),
                prep.displayRotation));

        String err = StartPointPrefs.save(ctx, (float) s[0] / screenW, (float) s[1] / screenH,
                screenW, screenH);
        if (err != null) {
            RunLog.line("学习落点：落盘失败 " + err);
            return new Result(false, "落盘失败：" + err + "（未生效）");
        }

        // ★ 自检：换算结果必须落在右下角（▶ 就在那里）。不满足只警告不拦 ——
        //   因为"用户点错地方"和"映射错了"从数据上分不开，而用户已明确接受前者。
        StringBuilder msg = new StringBuilder();
        msg.append("已记录 ").append(s[0]).append(',').append(s[1]);
        // ★ 右下角这件事已由 AxisMap.pickForStartButton 保证（不满足就不落盘），这里只留距离提醒。
        int fx = (int) Math.round(screenW * PlayService.START_REL_X);
        int fy = (int) Math.round(screenH * PlayService.START_REL_Y);
        if (Math.abs(s[0] - fx) > FAR_PX || Math.abs(s[1] - fy) > FAR_PX) {
            msg.append("（⚠ 离已知按钮位置较远：出厂点 ").append(fx).append(',').append(fy).append("）");
        }
        RunLog.line("学习落点：" + msg);
        return new Result(true, msg.toString());
    }

    /** 一句话判据，别让用户（和下一次的我）从数字里猜。 */
    static String hint(int rawLines, int events) {
        if (rawLines == 0) return "设备没吐出任何一行，多半是 getevent 没起来或输出被缓冲";
        if (events == 0) return "有输出但没解析出触摸事件，可能是设备选错或格式不认识";
        return "有事件但没等到第一次按下";
    }

    /** `Display.getRotation()` → 0/90/180/270。 */
    static int rotationDeg(Context ctx) {
        DisplayManager dm = (DisplayManager) ctx.getSystemService(Context.DISPLAY_SERVICE);
        if (dm == null) throw new IllegalStateException("拿不到 DisplayManager");
        Display d = dm.getDisplay(Display.DEFAULT_DISPLAY);
        if (d == null) throw new IllegalStateException("拿不到默认 Display");
        switch (d.getRotation()) {
            case Surface.ROTATION_90:
                return 90;
            case Surface.ROTATION_180:
                return 180;
            case Surface.ROTATION_270:
                return 270;
            case Surface.ROTATION_0:
            default:
                return 0;
        }
    }
}
