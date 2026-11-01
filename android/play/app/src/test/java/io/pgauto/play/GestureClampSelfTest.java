package io.pgauto.play;

import io.pgauto.inject.sched.Scheduler;

/**
 * `Scheduler.toDevice` 的**系统手势区夹取**自测（纯 JVM，不需要设备）。
 *
 * ★ 背景（2026-10-26 真机）：`玩具狂奏曲終焉 IN/AT` 有 13 / 551 个事件落在顶部 y≤80，
 *   注入的"连续下滑"会把通知栏拉下来（sticky immersive 只挡第一次）⇒ 整轮作废。
 *   修法是在 `toDevice` 里把触点夹出顶部/底部手势区.
 *
 * ⛔ 这里断的是**边界行为**：顶部必须 ≥92、底部必须 ≤628、左右各留 12；
 *   正常构图（判定线 y=360 一带）**一个像素都不许动**。
 */
public final class GestureClampSelfTest {

    private static int pass = 0, fail = 0;

    // 与生产一致：画布 1920x1080 → 源 1080x2340（源上下各 866.25 黑边）→ 设备 1610x720（fitHeight）
    private static int[] dev(double cx, double cy) {
        double srcScale = 1080.0 / 1920.0;                 // = 0.5625
        double srcYOff = (2340.0 - 1080.0 * srcScale) / 2; // = 866.25
        return Scheduler.toDevice(cx * srcScale, cy * srcScale + srcYOff,
                1920, 1080, 1080, 2340, 1610, 720, true);
    }

    public static void main(String[] args) {
        top_is_clamped();
        bottom_is_clamped();
        middle_is_untouched();
        sides_are_clamped();
        base_resolution_is_unchanged();
        scales_with_resolution();
        tiny_screen_is_left_alone();

        System.out.println("GestureClampSelfTest: " + pass + " 通过, " + fail + " 失败");
        if (fail > 0) throw new AssertionError("手势区夹取自测失败 " + fail + " 项");
    }

    /**
     * ★ 2026-10-30：**旧设备逐像素不变**——这是本次「绝对像素 → 按比例」改动的核心不变量。
     *
     * 比例写的是 `92.0/720` 与 `12.0/1610`，都由下面这四个数反推而来；
     * 哪天有人把比例改成「实测值反推」（74/720、30/1610），余量就没了，这里立刻红。
     */
    private static void base_resolution_is_unchanged() {
        int[] t = Scheduler.clampOutOfGestureZone(new int[]{800, 0}, 1610, 720);
        ok(t[1] == 92, "1610×720 顶部必须精确是 92（比例由此反推），实得 " + t[1]);
        int[] b = Scheduler.clampOutOfGestureZone(new int[]{800, 720}, 1610, 720);
        ok(b[1] == 628, "1610×720 底部必须精确是 628，实得 " + b[1]);
        int[] l = Scheduler.clampOutOfGestureZone(new int[]{0, 360}, 1610, 720);
        ok(l[0] == 12, "1610×720 最左必须精确是 12（比例由此反推），实得 " + l[0]);
        int[] r = Scheduler.clampOutOfGestureZone(new int[]{1610, 360}, 1610, 720);
        ok(r[0] == 1598, "1610×720 最右必须精确是 1598，实得 " + r[0]);
    }

    /**
     * ★ 换到 3136×1440（RMX5200 / realme / Android 17）后必须按比例放大，而不是还按 92 夹。
     *
     * 算式：top = round(1440 × 92/720) = 184、bottom 边 = 1440−184 = 1256、
     *       side = round(3136 × 12/1610) = 23、右边 = 3136−23 = 3113。
     * 变异：把比例改回固定像素（或把 92/720 写成 0.5）⇒ 本条红。
     */
    private static void scales_with_resolution() {
        int[] t = Scheduler.clampOutOfGestureZone(new int[]{1568, 0}, 3136, 1440);
        ok(t[1] == 184, "3136×1440 顶部应夹到 184，实得 " + t[1]);
        int[] b = Scheduler.clampOutOfGestureZone(new int[]{1568, 1440}, 3136, 1440);
        ok(b[1] == 1256, "3136×1440 底部应夹到 1256，实得 " + b[1]);
        int[] l = Scheduler.clampOutOfGestureZone(new int[]{0, 720}, 3136, 1440);
        ok(l[0] == 23, "3136×1440 最左应夹到 23，实得 " + l[0]);
        int[] r = Scheduler.clampOutOfGestureZone(new int[]{3136, 720}, 3136, 1440);
        ok(r[0] == 3113, "3136×1440 最右应夹到 3113，实得 " + r[0]);
        // 判定线一带（屏幕中线）一个像素都不许动
        int[] mid = Scheduler.clampOutOfGestureZone(new int[]{1568, 720}, 3136, 1440);
        ok(mid[1] == 720 && mid[0] == 1568, "3136×1440 中线不许被夹，实得 "
                + mid[0] + "," + mid[1]);
    }

    /**
     * 屏幕太小就不夹 —— 沿用旧语义（旧代码判 `dstH > 2×92`）。
     *
     * 为什么要钉：那道「不夹」是留给 `requireGameCanvas` 报错的，在这里夹了等于**把闸门测成绿**。
     * 变异：删掉 `GESTURE_MIN_H_PX` 那道判断 ⇒ 本条红（300×150 会被夹到 y=19）。
     */
    private static void tiny_screen_is_left_alone() {
        int[] d = Scheduler.clampOutOfGestureZone(new int[]{150, 10}, 300, 150);
        ok(d[1] == 10, "300×150 属于「太小不夹」，y 必须原样返回，实得 " + d[1]);
        int[] e = Scheduler.clampOutOfGestureZone(new int[]{5, 75}, 300, 150);
        ok(e[0] == 5, "300×150 属于「太小不夹」，x 也必须原样返回，实得 " + e[0]);
    }

    private static void top_is_clamped() {
        int[] raw = dev(960, 0);                       // 画布最顶端
        int[] d = Scheduler.clampOutOfGestureZone(raw, 1610, 720);   // 只对「按下」调用
        ok(d[1] >= 92, "按下点 y=0 必须被夹到 ≥92，实得 " + d[1]);
        ok(d[1] <= 120, "夹取幅度不该超过 ~100 px，实得 " + d[1]);
        ok(raw[1] < 92, "toDevice 本身**不许**夹（MOVE 要保留真实轨迹），实得 " + raw[1]);
    }

    private static void bottom_is_clamped() {
        int[] d = Scheduler.clampOutOfGestureZone(dev(960, 1080), 1610, 720);
        ok(d[1] <= 628, "按下点 y=1080 必须被夹到 ≤628，实得 " + d[1]);
    }

    private static void middle_is_untouched() {
        int[] d = Scheduler.clampOutOfGestureZone(dev(960, 540), 1610, 720);
        ok(Math.abs(d[1] - 360) <= 1, "判定线一带不许被夹，实得 " + d[1]);
        int[] e = Scheduler.clampOutOfGestureZone(dev(960, 300), 1610, 720);
        ok(e[1] < 360 && e[1] > 92, "画布 y=300 应落在无夹取区间，实得 " + e[1]);
    }

    private static void sides_are_clamped() {
        int[] l = Scheduler.clampOutOfGestureZone(dev(0, 540), 1610, 720);
        ok(l[0] >= 12, "最左必须 ≥12，实得 " + l[0]);
        int[] r = Scheduler.clampOutOfGestureZone(dev(1920, 540), 1610, 720);
        ok(r[0] <= 1598, "最右必须 ≤1598，实得 " + r[0]);
    }

    private static void ok(boolean cond, String msg) {
        if (cond) {
            pass++;
        } else {
            fail++;
            System.out.println("  ✗ " + msg);
        }
    }
}
