package io.pgauto.play;

/**
 * {@link FirstDown} 的离线自测。`selftest.sh` 用**没有 android.jar** 的 classpath 编它 ⇒ 只能纯 Java。
 *
 * 钉住的是**一条用户规则**（2026-10-31 12:12）：多次点击只认**第一次**。
 * 这条规则一旦被"顺手改成最后一个"，界面上没法察觉，但用户会把难度按钮记成 ▶ 的位置 ⇒
 * 每轮打歌都点空。所以必须有测试能红。
 */
public final class FirstDownSelfTest {

    private static int pass = 0;
    private static int fail = 0;

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

    public static void main(String[] args) {
        t01_firstOfSeveral();
        t02_moveOnlyGivesNone();
        t03_emptyGivesNone();
        t04_multipleDownsTakeFirst();
        t05_pointIsNullWhenNone();
        t06_pointZeroZeroIsValid();
        t07_lengthMismatchThrows();
        t08_nullThrows();
        t09_dumpFormat();

        System.out.println("FirstDownSelfTest: " + pass + " 通过, " + fail + " 失败");
        if (fail > 0) throw new AssertionError("FirstDown 自测失败 " + fail + " 项");
    }

    /** MOVE、DOWN、MOVE、UP ⇒ 取那个 DOWN。 */
    private static void t01_firstOfSeveral() {
        int[] act = {2, 0, 2, 1};
        int[] rx = {10, 20, 30, 40};
        int[] ry = {11, 21, 31, 41};
        eq(FirstDown.pick(act, rx, ry), 1, "t01 应取下标 1 的 DOWN");
        int[] p = FirstDown.firstDownPoint(act, rx, ry);
        ok(p != null && p[0] == 20 && p[1] == 21, "t01 坐标应是 (20,21)");
    }

    private static void t02_moveOnlyGivesNone() {
        int[] act = {2, 2, 2};
        int[] rx = {1, 2, 3};
        int[] ry = {1, 2, 3};
        eq(FirstDown.pick(act, rx, ry), FirstDown.NONE, "t02 只有 MOVE 应返回 NONE");
        ok(FirstDown.firstDownPoint(act, rx, ry) == null, "t02 只有 MOVE 应返回 null");
    }

    private static void t03_emptyGivesNone() {
        eq(FirstDown.pick(new int[0], new int[0], new int[0]), FirstDown.NONE,
                "t03 空序列应返回 NONE");
    }

    /** ★ 用户规则：多个 DOWN 取**第一个**。这条变了必红。 */
    private static void t04_multipleDownsTakeFirst() {
        int[] act = {0, 0, 0};
        int[] rx = {100, 200, 300};
        int[] ry = {110, 210, 310};
        eq(FirstDown.pick(act, rx, ry), 0, "t04 多个 DOWN 必须取第一个");
        int[] p = FirstDown.firstDownPoint(act, rx, ry);
        ok(p != null && p[0] == 100 && p[1] == 110, "t04 坐标必须是第一次那个 (100,110)");
        // 反向断言：第二个点绝不能被返回
        ok(p != null && p[0] != 200, "t04 绝不能返回第二次点击的坐标");
    }

    /** ★ (0,0) 是合法坐标 ⇒ 没有点击必须返回 null，不能拿 0,0 冒充。 */
    private static void t06_pointZeroZeroIsValid() {
        int[] act = {0};
        int[] rx = {0};
        int[] ry = {0};
        int[] p = FirstDown.firstDownPoint(act, rx, ry);
        ok(p != null, "t06 点在 (0,0) 必须返回非 null（它与「没点」是两回事）");
        int[] none = FirstDown.firstDownPoint(new int[]{}, new int[]{}, new int[]{});
        ok(none == null, "t06 没点时必须是 null，不许用 {0,0} 顶替");
    }

    private static void t05_pointIsNullWhenNone() {
        int[] act = {1, 2};
        int[] rx = {5, 6};
        int[] ry = {7, 8};
        ok(FirstDown.firstDownPoint(act, rx, ry) == null, "t05 无 DOWN 时 firstDownPoint 必须 null");
    }

    private static void t07_lengthMismatchThrows() {
        ok(throwsIAE(() -> FirstDown.pick(new int[]{0, 0}, new int[]{1}, new int[]{1, 2})),
                "t07 长度不一致必须抛（错位继续算只会得到错坐标）");
    }

    private static void t08_nullThrows() {
        ok(throwsIAE(() -> FirstDown.pick(null, new int[0], new int[0])),
                "t08 act 为 null 必须抛");
        ok(throwsIAE(() -> FirstDown.pick(new int[0], null, new int[0])),
                "t08 rawX 为 null 必须抛");
    }

    private static void t09_dumpFormat() {
        String s = FirstDown.dump(new int[]{0, 2}, new int[]{10, 20}, new int[]{11, 21});
        ok(s.startsWith("n=2 ["), "t09 dump 应含条数，实得 " + s);
        ok(s.contains("0:10x11"), "t09 dump 应含第一个事件，实得 " + s);
        ok(s.contains("2:20x21"), "t09 dump 应含第二个事件，实得 " + s);
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
