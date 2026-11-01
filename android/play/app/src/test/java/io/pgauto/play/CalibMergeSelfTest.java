package io.pgauto.play;

/**
 * `CalibMerge` 的自测（纯 JVM）。
 *
 * 这些规则一旦放松，症状是"参数慢慢偏、φ 变成一堆 Good"，而且**不会报错**
 * —— 所以每条规则都必须有能红的断言，包括"该拒绝的必须拒绝"和"该重置的必须重置"。
 */
public final class CalibMergeSelfTest {

    private static int pass = 0, fail = 0;

    public static void main(String[] args) {
        first_sample_gate();
        steady_average();
        outlier_rejected();
        side_reset_after_three();
        cap_then_exponential();
        lead_policy_range();
        nan_and_range_guards();

        System.out.println("CalibMergeSelfTest: " + pass + " 通过, " + fail + " 失败");
        if (fail > 0) throw new AssertionError("CalibMerge 自测失败 " + fail + " 项");
    }

    private static void first_sample_gate() {
        CalibMerge.Policy p = CalibMerge.Policy.forK();
        CalibMerge.Out a = CalibMerge.merge(0, 0, 47.8, 0, 0, p);
        ok(a.accepted && a.n == 1 && Math.abs(a.value - 47.8) < 1e-3,
                "首条样本 47.8 应被接受且 n=1，实得 " + a);
        CalibMerge.Out b = CalibMerge.merge(0, 0, 120.0, 0, 0, p);
        ok(b.accepted, "首条样本 120（<130）应被接受 —— 2026-10-26 放宽："
                + "destruction321_at 实测 -92.2 ms 曾被旧闸（80）永久卡死，实得 " + b);
        CalibMerge.Out b2 = CalibMerge.merge(0, 0, 140.0, 0, 0, p);
        ok(!b2.accepted, "首条样本 140（>130）应被拒绝，实得 " + b2);
        CalibMerge.Out c = CalibMerge.merge(0, 0, 200.0, 0, 0, p);
        ok(!c.accepted, "首条样本 200（超绝对范围）应被拒绝，实得 " + c);
    }

    private static void steady_average() {
        CalibMerge.Policy p = CalibMerge.Policy.forK();
        CalibMerge.Out a = CalibMerge.merge(50f, 2, 53.0, 0, 0, p);
        ok(a.accepted && a.n == 3 && Math.abs(a.value - 51.0) < 1e-3,
                "均值 50(n=2) 并入 53 ⇒ 51.0(n=3)（{50,50,53} 的算术均值），实得 " + a);
    }

    private static void outlier_rejected() {
        CalibMerge.Policy p = CalibMerge.Policy.forK();
        CalibMerge.Out a = CalibMerge.merge(0.6f, 3, -48.9, 0, 0, p);
        ok(!a.accepted && Math.abs(a.value - 0.6f) < 1e-6,
                "−48.9 相对 0.6 是离群，必须拒绝且不动均值，实得 " + a);
    }

    private static void side_reset_after_three() {
        CalibMerge.Policy p = CalibMerge.Policy.forK();
        // 连续三次同侧大偏离（+40）⇒ 第三次应当重置
        CalibMerge.Out a = CalibMerge.merge(0f, 3, 40.0, 0, 0, p);
        ok(!a.accepted && a.sideCount == 1, "第 1 次同侧偏离：拒绝且 sideCount=1，实得 " + a);
        CalibMerge.Out b = CalibMerge.merge(0f, 3, 41.0, a.sideCount, a.sideSign, p);
        ok(!b.accepted && b.sideCount == 2, "第 2 次同侧：sideCount=2，实得 " + b);
        CalibMerge.Out c = CalibMerge.merge(0f, 3, 42.0, b.sideCount, b.sideSign, p);
        ok(c.accepted && c.reset && Math.abs(c.value - 42.0) < 1e-3 && c.n == 1,
                "第 3 次同侧 ⇒ 重置为新值 42、n=1，实得 " + c);
        // 反方向的偏离会清零计数
        CalibMerge.Out d = CalibMerge.merge(0f, 3, -40.0, 2, 1, p);
        ok(!d.accepted && d.sideCount == 1,
                "反方向偏离把计数重置成 1（不是累加），实得 " + d);
    }

    private static void cap_then_exponential() {
        CalibMerge.Policy p = CalibMerge.Policy.forK();
        CalibMerge.Out a = CalibMerge.merge(50f, 10, 55.0, 0, 0, p);
        ok(a.accepted && a.n == 10 && Math.abs(a.value - 50.5) < 1e-3,
                "n=10 时转指数式（50 + 5/10 = 50.5，n 保持 10），实得 " + a);
    }

    private static void lead_policy_range() {
        CalibMerge.Policy p = CalibMerge.Policy.forLead();
        CalibMerge.Out a = CalibMerge.merge(0, 0, 7195.9, 0, 0, p);
        ok(a.accepted && Math.abs(a.value - 7195.9) < 1e-2,
                "lead 7195.9 应被接受，实得 " + a);
        CalibMerge.Out b = CalibMerge.merge(0, 0, 999.0, 0, 0, p);
        ok(!b.accepted, "lead 999（<3 s）应被拒绝，实得 " + b);
        CalibMerge.Out c = CalibMerge.merge(7195f, 2, 7500.0, 0, 0, p);
        ok(!c.accepted, "lead 偏离 305 ms 应判离群（阈值 30），实得 " + c);
    }

    private static void nan_and_range_guards() {
        CalibMerge.Policy p = CalibMerge.Policy.forK();
        CalibMerge.Out a = CalibMerge.merge(10f, 2, Double.NaN, 0, 0, p);
        ok(!a.accepted && a.n == 2, "NaN 必须拒绝且不改 n，实得 " + a);
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

    private CalibMergeSelfTest() {}
}
