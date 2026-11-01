package io.pgauto.play;

/**
 * {@link StartButtonBox} 的离线自测。
 *
 * ★ `selftest.sh` 用**没有 android.jar** 的 classpath 编它并运行 ——
 *   所以这里只能碰纯 Java，不许出现 SystemClock / Log。
 *
 * 钉住三件事：
 *   ① 框的四个边界必须能追溯到「1610×720 上的实测像素」（1465 / 558 / 665 / 1610）；
 *   ② 现有落点在两台真机上都在框内（HONOR 已打出 φ、RMX5200 已打成功）；
 *   ③ 框外 1px 就必须判外 —— 否则「在内 ✓」这句话没有分辨力，等于没测。
 */
public final class StartButtonBoxSelfTest {

    private static int pass = 0;
    private static int fail = 0;

    /**
     * 无偏移时的比例落点 —— 与 `PlayService.START_REL_X/Y` **必须一致**。
     *
     * ⚠ 这里只能是副本：`PlayService` 依赖 android.jar，而本自测链刻意**不带** android.jar。
     *   两边相等由 pytest 契约钉住（`test_start_rel_single_source`），不是靠注释声明。
     */
    private static final double REL_X = 0.945791;
    private static final double REL_Y = 0.854861;

    private static void ok(boolean cond, String msg) {
        if (cond) {
            pass++;
        } else {
            fail++;
            System.out.println("  ✗ " + msg);
        }
    }

    public static void main(String[] args) {
        t01_constantsTraceable();
        t02_honorInside();
        t03_rmxInside();
        t04_boundaryIsExact();
        t05_degenerateDoesNotThrow();
        t06_verdictText();
        t07_targetMatchesCenter();

        System.out.println("StartButtonBoxSelfTest: " + pass + " 通过, " + fail + " 失败");
        if (fail > 0) throw new AssertionError("按钮框自测失败 " + fail + " 项");
    }

    /** 常量必须写成「实测像素 / 屏幕尺寸」的形式，可核对来源。 */
    private static void t01_constantsTraceable() {
        ok(near(StartButtonBox.X0, 1465.0 / 1610.0), "t01 X0 应 = 1465/1610");
        ok(near(StartButtonBox.X1, 1.0), "t01 X1 应 = 1.0（按钮贴右边缘）");
        ok(near(StartButtonBox.Y0, 558.0 / 720.0), "t01 Y0 应 = 558/720");
        ok(near(StartButtonBox.Y1, 665.0 / 720.0), "t01 Y1 应 = 665/720");
        ok(StartButtonBox.Y0 < StartButtonBox.Y1, "t01 Y0 必须小于 Y1");
        ok(StartButtonBox.X0 < StartButtonBox.X1, "t01 X0 必须小于 X1");
    }

    /** HONOR 1610×720：落点 (1522.7, 615.5)，实测能打出 φ。 */
    private static void t02_honorInside() {
        double x = 1610 * 0.945791;
        double y = 720 * 0.854861;
        ok(near(x, 1522.72, 0.02), "t02 落点 x 应 ≈ 1522.72，得到 " + x);
        ok(near(y, 615.50, 0.02), "t02 落点 y 应 ≈ 615.50，得到 " + y);
        ok(StartButtonBox.inside(x, y, 1610, 720), "t02 HONOR 落点应在框内");
        // 余量要写清：左 57.7 / 右 87.3 / 上 57.5 / 下 49.5
        ok(near(x - 1610 * StartButtonBox.X0, 57.7, 0.1), "t02 左余量应 ≈ 57.7 px");
        ok(near(1610 * StartButtonBox.X1 - x, 87.3, 0.1), "t02 右余量应 ≈ 87.3 px");
        ok(near(y - 720 * StartButtonBox.Y0, 57.5, 0.1), "t02 上余量应 ≈ 57.5 px");
        ok(near(720 * StartButtonBox.Y1 - y, 49.5, 0.1), "t02 下余量应 ≈ 49.5 px");
    }

    /** RMX5200 3136×1440：13:42 那轮 onset 正常、n_emit=3264 ⇒ 按钮点中了。 */
    private static void t03_rmxInside() {
        double x = 3136 * 0.945791;
        double y = 1440 * 0.854861;
        ok(StartButtonBox.inside(x, y, 3136, 1440),
                "t03 RMX5200 落点 (" + x + "," + y + ") 应在框内 —— 该机实测已点中");
    }

    /** 框外 **1 px** 就必须判外：否则「在内 ✓」没有分辨力。 */
    private static void t04_boundaryIsExact() {
        ok(StartButtonBox.inside(1465, 558, 1610, 720), "t04 左上角 (1465,558) 应在内（闭区间）");
        ok(!StartButtonBox.inside(1464, 558, 1610, 720), "t04 左出 1 px 应判外");
        ok(!StartButtonBox.inside(1465, 557, 1610, 720), "t04 上出 1 px 应判外");
        ok(!StartButtonBox.inside(1465, 666, 1610, 720), "t04 下出 1 px 应判外");
        ok(StartButtonBox.inside(1610, 665, 1610, 720), "t04 右下角 (1610,665) 应在内（贴边算内）");
    }

    /** 非法尺寸不许抛（这条会在真机异常时把日志打断）。 */
    private static void t05_degenerateDoesNotThrow() {
        try {
            ok(!StartButtonBox.inside(0, 0, 0, 0), "t05 w=h=0 应判外");
            ok(!StartButtonBox.inside(10, 10, -1, 720), "t05 负宽应判外");
            ok(!StartButtonBox.inside(Double.NaN, 0, 1610, 720), "t05 NaN 应判外");
            String s = StartButtonBox.verdict(0, 0, 0, 0, REL_X, REL_Y);
            ok(s != null && s.contains("非法"), "t05 非法尺寸的 verdict 应明说，得到：" + s);
        } catch (Throwable t) {
            ok(false, "t05 非法尺寸抛了异常：" + t);
        }
    }

    /**
     * 判读文本：三档 + **每一档都要给"照着填什么"**。
     *
     * ★★ 2026-10-30 第二版。第一版只做「在不在框内」，用户实测被它带偏两次：
     * <pre>
     *   dx −58 / dy −57 ⇒ 落点 1464.72, 558.50 ⇒ 旧文案「在内 ✓（与边界差 &lt;1px，可视为对准）」
     *   实际 x 差 0.28px **落在左边界外**，正压按钮左上角顶点 ⇒ 点不中，游戏一动不动
     * </pre>
     * 那次"可视为对准"来自 0.5px 显示宽容，把**唯一的警告吞掉了**。
     * ⇒ 现在：二元判断换成「距中心多远」+「dx/dy 该填多少（绝对值）」，
     *   并且**贴边（余量 &lt; 10px）单独一档报警**。
     */
    private static void t06_verdictText() {
        // ── ① 默认偏移：落在框中心偏左 15 / 下 4，四边余量都够 ⇒ ✓ 对准
        String in = StartButtonBox.verdict(1522.72, 615.5, 1610, 720, REL_X, REL_Y);
        ok(in.contains("✓") && in.contains("在框内"), "t06 框内应写「✓ 在框内」，得到：" + in);
        ok(!in.contains("对准"), "t06 不许再写「对准」——框本身量错 26px，它不配下这个结论");
        ok(in.contains("距中心 左15 下4"), "t06 应给出「距中心 左15 下4」，得到：" + in);
        ok(in.contains("左58") && in.contains("右87"), "t06 应给出四边余量，得到：" + in);
        ok(in.contains("非物理真值"), "t06 必须标注这不是物理真值，得到：" + in);
        ok(in.contains("学习落点") && !in.contains("dx 填"),
                "t06 每一档都要指向「学习落点」，且不许再让人填 dx/dy，得到：" + in);

        // ── ② 用户今天的真实落点（dx −58 / dy −57）：出界 0.28px
        //    这条是本次改版的**回归守卫**：旧版在这里说"可视为对准"。
        String out = StartButtonBox.verdict(1464.72, 558.5, 1610, 720, REL_X, REL_Y);
        ok(out.contains("✗") && out.contains("出界"), "t06 该落点必须判「✗ 出界」，得到：" + out);
        ok(out.contains("左边界外 0.3 px"),
                "t06 差 0.28px 必须显示「0.3 px」，不许写成 0 ⇒ 得到：" + out);
        ok(out.contains("距中心 左73 上53"), "t06 应给出距中心多远，得到：" + out);
        ok(out.contains("学习落点") && !out.contains("dx 填"), "t06 出界也要指向「学习落点」，得到：" + out);
        ok(!out.contains("可视为对准"), "t06 不许再出现「可视为对准」—— 正是它把用户骗了");
        ok(!out.contains("在内"), "t06 不许再出现「在内」这种二元判断");

        // ── ③ 框内但贴边（余量 3px）⇒ 必须报警
        String edge = StartButtonBox.verdict(1468, 615.5, 1610, 720, REL_X, REL_Y);
        ok(edge.contains("⚠") && edge.contains("贴边"), "t06 余量 3px 应判「⚠ 贴边」，得到：" + edge);
        ok(edge.contains("只剩 3 px"), "t06 贴边档要写清最近边剩多少，得到：" + edge);
        ok(edge.contains("学习落点"), "t06 贴边档也要指向「学习落点」，得到：" + edge);

        // ── ④ 远距出界：逐条列出每条出界的边（差 0.3px 与差 1365px 读起来必须不同）
        String far = StartButtonBox.verdict(100, 100, 1610, 720, REL_X, REL_Y);
        ok(far.contains("左边界外 1365 px"), "t06 应写「左边界外 1365 px」，得到：" + far);
        ok(far.contains("上边界外 458 px"), "t06 应写「上边界外 458 px」，得到：" + far);

        // ── ⑤ 正好在中心：不许写出「左0 / 上0」这种像 bug 的东西
        String mid = StartButtonBox.verdict(1537.5, 611.5, 1610, 720, REL_X, REL_Y);
        ok(mid.contains("水平居中") && mid.contains("垂直居中"),
                "t06 落在中心应写「水平居中 垂直居中」，得到：" + mid);
        ok(!mid.contains("左0") && !mid.contains("上0"), "t06 不许出现「左0/上0」，得到：" + mid);
    }

    /**
     * `targetDx/targetDy` = 「⇢ 对准按钮中心」按钮填的值 = 判读文案给的「dx 填 …」。
     * **三处必须是同一个数**（本项目教训：同一个量两处实现必然漂移）。
     */
    private static void t07_targetMatchesCenter() {
        ok(near(StartButtonBox.targetDx(1610, REL_X), 14.78, 0.05),
                "t07 HONOR 的 targetDx 应 ≈ +14.78");
        ok(near(StartButtonBox.targetDy(720, REL_Y), -4.0, 0.05),
                "t07 HONOR 的 targetDy 应 ≈ -4.0");
        // 填了这两个偏移之后，落点必须正好落在框中心
        double x = 1610 * REL_X + StartButtonBox.targetDx(1610, REL_X);
        double y = 720 * REL_Y + StartButtonBox.targetDy(720, REL_Y);
        ok(near(x, StartButtonBox.centerX(1610), 1e-9), "t07 填完 dx 后 x 应等于框中心");
        ok(near(y, StartButtonBox.centerY(720), 1e-9), "t07 填完 dy 后 y 应等于框中心");
        // 中心本身必须落在框内、且离四边都够远
        ok(StartButtonBox.inside(x, y, 1610, 720), "t07 框中心必须在框内");
        String v = StartButtonBox.verdict(x, y, 1610, 720, REL_X, REL_Y);
        ok(v.contains("✓"), "t07 落到中心后判读必须是 ✓，得到：" + v);
    }

    private static boolean near(double a, double b) {
        return Math.abs(a - b) < 1e-9;
    }

    private static boolean near(double a, double b, double tol) {
        return Math.abs(a - b) < tol;
    }
}
