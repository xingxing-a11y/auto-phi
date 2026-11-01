package io.pgauto.inject.sched;

/**
 * {@link InjectMode} 的自测（纯 JVM，无 Android 依赖）。
 *
 * ★ 为什么值得写：这一层决定的不是「行为好不好」，而是**这一枪 A/B 是否成立**。
 *   两种典型失败都不会报错：
 *     · `ASYNC` 写成 1 ⇒ 生产路径悄悄变同步，成绩与 lag 全不可比却没人发现；
 *     · `name(2)` 返回 "async" ⇒ 一个写错的字面量以 async 的身份混过整轮验收。
 *   ⇒ 断言必须是**具体数值**（0 / 1 / "?2"），不能只断言「两者不相等」。
 */
public final class InjectModeSelfTest {

    private static int pass, fail;

    public static void main(String[] args) {
        t01_constantsMatchAndroid();
        t03_validityBoundary();
        t04_unknownNameIsNotAsync();

        System.out.println("InjectModeSelfTest: " + pass + " 通过, " + fail + " 失败");
        if (fail > 0) throw new AssertionError("InjectMode 自测失败 " + fail + " 项");
    }

    /** 数值来源是 Android 的 `INJECT_INPUT_EVENT_MODE_*`：0=ASYNC / 1=WAIT_FOR_FINISH。 */
    private static void t01_constantsMatchAndroid() {
        eq(0, InjectMode.ASYNC, "ASYNC 必须是 0（INJECT_INPUT_EVENT_MODE_ASYNC）");
        eq(1, InjectMode.WAIT_FOR_FINISH, "WAIT_FOR_FINISH 必须是 1");
        ok(InjectMode.ASYNC != InjectMode.WAIT_FOR_FINISH, "两个模式号不能相同");
    }

    private static void t03_validityBoundary() {
        ok(InjectMode.isValid(0), "0 合法");
        ok(InjectMode.isValid(1), "1 合法");
        ok(!InjectMode.isValid(2), "2 必须非法（injectInputEvent 对非法 mode 无文档保证）");
        ok(!InjectMode.isValid(-1), "-1 必须非法");
    }

    /**
     * 未知值不能伪造身份：`name(2)` 必须带出那个数字。
     * 变异：把兜底改成 `return "async"` ⇒ 本项红。
     */
    private static void t04_unknownNameIsNotAsync() {
        eq("async", InjectMode.name(InjectMode.ASYNC), "name(0)");
        eq("wait", InjectMode.name(InjectMode.WAIT_FOR_FINISH), "name(1)");
        eq("?2", InjectMode.name(2), "name(2) 必须显形，不许当 async");
    }

    // ─────────────────────────────── 断言 ───────────────────────────────

    private static void ok(boolean cond, String msg) {
        if (cond) {
            pass++;
        } else {
            fail++;
            System.out.println("  ✗ " + msg);
        }
    }

    private static void eq(int expected, int actual, String msg) {
        ok(expected == actual, msg + "：期望 " + expected + "，实际 " + actual);
    }

    private static void eq(String expected, String actual, String msg) {
        ok(expected.equals(actual), msg + "：期望「" + expected + "」，实际「" + actual + "」");
    }
}
