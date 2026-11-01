package io.pgauto.play;

/**
 * `RezeroGate` 的离线单测 —— **纯 JVM**。
 *
 * 用两个**真实数据**做锚：
 *  · 采纳侧：`destruction321_at` 的真机包络上，互相关与侧车只差 **+4.2 ms**；
 *  · 拒绝侧：`retributioncycleofredemption_in` 那轮互相关偏了 **~2014 ms**（cc 仍 0.913）——
 *    这正是必须被闸门挡住的形态，否则 rezero 会把整轮改坏。
 */
public final class RezeroGateSelfTest {

    private static int pass = 0;
    private static int fail = 0;

    public static void main(String[] args) {
        t01_acceptsSmallDelta();
        t02_rejectsBigDelta();
        t03_rejectsNaN();
        t04_boundary();
        t05_whyText();
        System.out.println("RezeroGate: " + pass + " passed, " + fail + " failed");
        if (fail > 0) {
            throw new AssertionError("RezeroGateSelfTest 失败 " + fail + " 条");
        }
    }

    /** 真实测量：+4.2 ms 必须采纳。 */
    private static void t01_acceptsSmallDelta() {
        ok(RezeroGate.accept(7509.9, 7514.1), "t01 +4.2 ms 应采纳");
        ok(RezeroGate.why(7509.9, 7514.1) == null, "t01 采纳时 why 应为 null");
    }

    /** 真实测量：偏 2014 ms（cc 0.913 的锁错）必须拒绝。 */
    private static void t02_rejectsBigDelta() {
        ok(!RezeroGate.accept(5600, 7614), "t02 偏 2014 ms 必须拒绝");
        String w = RezeroGate.why(5600, 7614);
        ok(w != null && w.contains("锁错"), "t02 why 应写明锁错，实际 " + w);
    }

    private static void t03_rejectsNaN() {
        ok(!RezeroGate.accept(Double.NaN, 1.0), "t03 gate 为 NaN 应拒绝");
        ok(!RezeroGate.accept(1.0, Double.NaN), "t03 相关为 NaN 应拒绝");
    }

    /** 边界：正好 200 采纳，200.1 拒绝（避免"差一点点就整轮改坏"）。 */
    private static void t04_boundary() {
        ok(RezeroGate.accept(1000, 1200), "t04 差 200 ms 应采纳（闭区间）");
        ok(!RezeroGate.accept(1000, 1200.1), "t04 差 200.1 ms 应拒绝");
        ok(RezeroGate.accept(1000, 800), "t04 反向差 200 ms 也应采纳");
    }

    private static void t05_whyText() {
        ok(RezeroGate.why(1000, 1035) == null, "t05 小差值不该有拒绝原因");
        String w = RezeroGate.why(1000, 600);
        ok(w != null && w.contains("-400"), "t05 应指出差值 -400，实际 " + w);
    }

    private static void ok(boolean cond, String what) {
        if (cond) {
            pass++;
            System.out.println("  ✓ " + what);
        } else {
            fail++;
            System.out.println("  ✗ " + what);
        }
    }

    private RezeroGateSelfTest() {
    }
}
