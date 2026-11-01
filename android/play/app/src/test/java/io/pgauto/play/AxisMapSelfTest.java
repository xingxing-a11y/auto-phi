package io.pgauto.play;

import java.util.List;

/**
 * {@link AxisMap} 的离线自测。`selftest.sh` 用**没有 android.jar** 的 classpath 编它 ⇒ 只能纯 Java。
 *
 * 钉住四件事：
 *   ① 四种旋转的公式必须是**手算能对上**的（不是"跑通就算"）；
 *   ② raw→screen→raw 往返必须闭合（否则标定"看着对"其实错位）；
 *   ③ 越界 raw 夹边界但 `inRange` 必须报 false（不许静默）；
 *   ④ `solve` 在**区分不出来**时必须给多解、在**自相矛盾**时必须给 0 解 ——
 *      这两条是"标定不许糊过去"的唯一保障。
 */
public final class AxisMapSelfTest {

    private static int pass = 0;
    private static int fail = 0;

    /** 本机实测：honor,ts_kit 量程 0..719 / 0..1609，屏幕横屏 1610×720。 */
    private static final int RAW_X = 719;
    private static final int RAW_Y = 1609;
    private static final int SW = 1610;
    private static final int SH = 720;

    private static void ok(boolean cond, String msg) {
        if (cond) {
            pass++;
        } else {
            fail++;
            System.out.println("  ✗ " + msg);
        }
    }

    private static void eq(int actual, int expected, String msg) {
        ok(actual == expected, msg + "（期望 " + expected + "，实得 " + actual + "）");
    }

    private static AxisMap m(int rot, boolean fx, boolean fy) {
        return new AxisMap(rot, fx, fy, RAW_X, RAW_Y, SW, SH);
    }

    public static void main(String[] args) {
        t01_identityOnSquare();
        t02_rot90HandComputed();
        t03_roundTrip();
        t04_outOfRangeClampsButFlagged();
        t05_invalidArgsThrow();
        t06_solveUnique();
        t07_solveAmbiguousAtCenter();
        t08_solveContradictory();
        t09_pickFromRealTap();
        t10_pickRejectsCornerTap();
        t11_pickNeverAmbiguous();

        System.out.println("AxisMapSelfTest: " + pass + " 通过, " + fail + " 失败");
        if (fail > 0) throw new AssertionError("AxisMap 自测失败 " + fail + " 项");
    }

    /** rot0 用 1:1 的量程/屏幕，等价于直通 —— 先把"没有多余换算"钉住。 */
    private static void t01_identityOnSquare() {
        AxisMap a = new AxisMap(0, false, false, 100, 100, 100, 100);
        int[] p = a.rawToScreen(0, 0);
        eq(p[0], 0, "t01 (0,0) → x");
        eq(p[1], 0, "t01 (0,0) → y");
        p = a.rawToScreen(100, 100);
        eq(p[0], 99, "t01 (max,max) → x 夹到 W-1");
        eq(p[1], 99, "t01 (max,max) → y 夹到 H-1");
        p = a.rawToScreen(50, 50);
        eq(p[0], 50, "t01 (中点) → x");
        eq(p[1], 50, "t01 (中点) → y");
    }

    /**
     * rot90 必须把 raw 的 **Y 轴变成屏幕的 X 轴**。
     *
     * 手算：raw(105,1521) ⇒ nx=105/719=0.1460、ny=1521/1609=0.9453
     *      rot90 ⇒ ux=ny=0.9453、uy=1-nx=0.8540
     *      屏幕 ⇒ (0.9453×1610, 0.8540×720) = (1522, 615)
     * 这两个数正是本机出厂落点，所以这条断言不是抽象的"公式对"，是**能对回真机**的。
     */
    private static void t02_rot90HandComputed() {
        int[] p = m(90, false, false).rawToScreen(105, 1521);
        ok(Math.abs(p[0] - 1522) <= 1, "t02 rot90 raw(105,1521) → x≈1522，实得 " + p[0]);
        ok(Math.abs(p[1] - 615) <= 1, "t02 rot90 raw(105,1521) → y≈615，实得 " + p[1]);

        // 轴必须真的交换了：raw 的 Y 变大 ⇒ 屏幕 x 变大
        int[] a = m(90, false, false).rawToScreen(105, 200);
        int[] b = m(90, false, false).rawToScreen(105, 1400);
        ok(b[0] > a[0], "t02 rot90：rawY 增大应使 screenX 增大（" + a[0] + " → " + b[0] + "）");
        // raw 的 X 变大 ⇒ 屏幕 y 变小（1-nx）
        int[] c = m(90, false, false).rawToScreen(200, 1521);
        ok(c[1] < p[1], "t02 rot90：rawX 增大应使 screenY 减小（" + p[1] + " → " + c[1] + "）");
    }

    /** 四种旋转 × 镜像，任意点往返误差 ≤ 2 px（量化误差上界）。 */
    private static void t03_roundTrip() {
        int[][] pts = {{0, 0}, {1, 1}, {359, 805}, {718, 1608}, {RAW_X, RAW_Y}, {200, 1500}};
        int[] rots = {0, 90, 180, 270};
        int worst = 0;
        int checked = 0;
        for (int r : rots) {
            for (int fx = 0; fx < 2; fx++) {
                for (int fy = 0; fy < 2; fy++) {
                    AxisMap a = m(r, fx == 1, fy == 1);
                    for (int[] p : pts) {
                        int[] s = a.rawToScreen(p[0], p[1]);
                        int[] back = a.screenToRaw(s[0], s[1]);
                        worst = Math.max(worst, Math.max(Math.abs(back[0] - p[0]),
                                Math.abs(back[1] - p[1])));
                        checked++;
                    }
                }
            }
        }
        ok(checked == 96, "t03 应检查 96 组，实得 " + checked);
        ok(worst <= 2, "t03 往返最大误差应 ≤2 px，实得 " + worst);
    }

    private static void t04_outOfRangeClampsButFlagged() {
        AxisMap a = m(90, false, false);
        ok(!a.inRange(-5, 100), "t04 负 rawX 必须判越界");
        ok(!a.inRange(100, RAW_Y + 1), "t04 rawY 超量程必须判越界");
        ok(a.inRange(0, 0), "t04 (0,0) 在量程内");
        ok(a.inRange(RAW_X, RAW_Y), "t04 (max,max) 在量程内");
        // 夹边界但不崩，且结果仍在屏幕内
        int[] p = a.rawToScreen(-9999, 99999);
        ok(p[0] >= 0 && p[0] < SW && p[1] >= 0 && p[1] < SH,
                "t04 越界 raw 必须夹出合法屏幕点，实得 " + p[0] + "," + p[1]);
    }

    private static void t05_invalidArgsThrow() {
        ok(throwsIAE(() -> new AxisMap(45, false, false, RAW_X, RAW_Y, SW, SH)),
                "t05 rotation=45 必须抛");
        ok(throwsIAE(() -> new AxisMap(90, false, false, 0, RAW_Y, SW, SH)),
                "t05 rawMaxX=0 必须抛");
        ok(throwsIAE(() -> new AxisMap(90, false, false, RAW_X, RAW_Y, 0, SH)),
                "t05 screenW=0 必须抛");
    }

    /**
     * 真机标定的那一组数：raw(105,1521) ↔ screen(1522,615) ⇒ 必须**唯一**解出 rot90 无镜像。
     * 若这里返回多解，说明容差或去重逻辑把歧义放过去了。
     */
    private static void t06_solveUnique() {
        List<AxisMap> c = AxisMap.solve(105, 1521, 1522, 615, RAW_X, RAW_Y, SW, SH, 1);
        ok(c.size() == 1, "t06 应唯一解，实得 " + c.size() + " 组");
        if (c.size() == 1) {
            AxisMap a = c.get(0);
            ok(a.rotation == 90, "t06 解出的 rotation 应为 90，实得 " + a.rotation);
            ok(!a.flipX && !a.flipY, "t06 不该需要镜像，实得 flipX=" + a.flipX + " flipY=" + a.flipY);
        }
    }

    /**
     * 正中点对**所有旋转**都等价 ⇒ 必须返回多解，让调用方拒绝。
     * （这条是"能抓到真 bug"的那条：如果 solve 只是 `return first`，它永远返回 1。）
     */
    private static void t07_solveAmbiguousAtCenter() {
        // 量程取偶数，使中点正好落在 0.5，四种旋转在中心重合
        List<AxisMap> c = AxisMap.solve(360, 805, 805, 360, 720, 1610, 1610, 720, 1);
        ok(c.size() != 1, "t07 屏幕正中必须解不唯一（实得 " + c.size() + " 组）⇒ 调用方应拒绝并要求换点重标");
        ok(c.size() >= 2, "t07 至少应有 2 组候选，实得 " + c.size());
    }

    /** 自相矛盾的标定（屏幕点根本映射不到）⇒ 0 解，必须报错而不是硬凑一个。 */
    /**
     * ★★★ 用 2026-10-31 **真机实测**的那一下反推旋转。
     *
     * <pre>
     *   触摸原始：003a(pressure) 0035=0x53(X=83) 0036=0x5fe(Y=1534) 0039 TRACKING_ID ...
     *   ⇒ raw(83, 1534) ；量程 719 × 1609 ；屏幕 1610 × 720（横屏）
     * </pre>
     * 用户点的是**右下角「开始演奏」**。旧代码用 `Display.getRotation()`（那一刻前台是我们
     * 自己的竖屏 App，拿到 0）换算出 (186,686) —— 左下角，整错了 90°。
     */
    private static void t09_pickFromRealTap() {
        AxisMap.Pick p = AxisMap.pickForStartButton(83, 1534, 719, 1609, 1610, 720);
        ok(p.map != null, "t09 真机那一下必须能定死映射，实得 why=" + p.why);
        if (p.map == null) return;
        ok(p.map.rotation == 90, "t09 旋转应是 90°（不是显示旋转报的 0），实得 " + p.map.rotation);
        int[] s = p.map.rawToScreen(83, 1534);
        ok(Math.abs(s[0] - 1535) <= 2 && Math.abs(s[1] - 637) <= 2,
                "t09 应换算到 (1535,637) 附近，实得 (" + s[0] + "," + s[1] + ")");
        // 出厂落点 1523,615 —— 用户手点必然有偏差，但必须在同一角
        ok(Math.abs(s[0] - 1523) < 200 && Math.abs(s[1] - 615) < 200,
                "t09 换算结果必须靠近出厂落点 (1523,615)，实得 (" + s[0] + "," + s[1] + ")");
        ok(AxisMap.inStartZone(s[0], s[1], 1610, 720), "t09 换算结果必须在开始按钮区");
    }

    /** 点在左上角（比如用户先点了返回/曲目）⇒ **必须定不下来**，不许硬凑一个旋转。 */
    private static void t10_pickRejectsCornerTap() {
        AxisMap.Pick p = AxisMap.pickForStartButton(60, 80, 719, 1609, 1610, 720);
        ok(p.map == null, "t10 左上角的点不该被当成开始按钮");
        ok(p.why != null && p.why.contains("右下角"), "t10 必须说清原因，实得 " + p.why);
    }

    /**
     * 不变量：**任何** raw 点都不可能同时落进两个候选的右下角区。
     * 若这条不成立，pick 就会在多候选中挑一个 —— 那正是"把歧义当答案"。
     */
    private static void t11_pickNeverAmbiguous() {
        int amb = 0;
        for (int x = 0; x <= 719; x += 7) {
            for (int y = 0; y <= 1609; y += 23) {
                int hit = 0;
                for (int r : new int[]{90, 270}) {
                    AxisMap m = new AxisMap(r, false, false, 719, 1609, 1610, 720);
                    int[] s = m.rawToScreen(x, y);
                    if (AxisMap.inStartZone(s[0], s[1], 1610, 720)) hit++;
                }
                if (hit > 1) amb++;
            }
        }
        ok(amb == 0, "t11 任何 raw 点都不该同时命中两个旋转候选，实得歧义点 " + amb + " 个");
    }

    private static void t08_solveContradictory() {
        List<AxisMap> c = AxisMap.solve(105, 1521, -50, 615, RAW_X, RAW_Y, SW, SH, 1);
        ok(c.isEmpty(), "t08 矛盾标定必须 0 解，实得 " + c.size() + " 组");
    }

    private static boolean throwsIAE(Runnable r) {
        try {
            r.run();
            return false;
        } catch (IllegalArgumentException e) {
            return true;
        }
    }
}
