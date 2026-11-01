package io.pgauto.play;

/**
 * {@link CourseBound} 的离线单测 —— **纯 JVM，不需要 Android、不需要真机**。
 *
 * 每个用例都对应 2026-10-28 复盘里那条真 bug 的一个分支，删掉实现里的判据必须立刻红：
 *   · t01：零点没建立（NaN）⇒ 必须返回 NaN（调用方据此中止）——
 *          **这条就是原 bug 的守卫**：旧实现根本不看零点，直接拿上上首的下界继续打。
 *   · t02：长度未知（0 / 负 / NaN）⇒ 同样返回 NaN（用"上一首末事件"会早退）。
 *   · t03：两者都有 ⇒ 逐位等于 zero+len（不许有 ±1 ms 的花活）。
 *   · t04：正常路径 baseMs 的两条分支。
 *
 * 运行：`bash android/play/selftest.sh`
 */
public final class CourseBoundSelfTest {

    private static int pass = 0;
    private static int fail = 0;

    public static void main(String[] args) {
        t01_noZeroMeansUnknown();
        t02_noLengthMeansUnknown();
        t03_knownZeroAndLengthIsExactSum();
        t04_normalPathBase();
        t05_infiniteZeroRejected();
        System.out.println("\nCourseBoundSelfTest: " + pass + " passed, " + fail + " failed");
        if (fail > 0) {
            System.exit(1);
        }
    }

    /**
     * 契约：零点没建立（NaN）⇒ **必须**返回 NaN，调用方据此中止。
     *
     * ⚠ 变异力说明（2026-10-28 实测，别自欺）：这条**抓不到**「删掉 NaN 判据」——
     *   NaN 参与加法本身就是 NaN，删掉判据 t01 照样绿。真正能红的是：
     *   · **t02**（长度判据：删掉 `musicLenMs > 0` ⇒ 返回 1e6，t02 红）；
     *   · **t05**（±∞ 判据：删掉有限性检查 ⇒ 返回 ±∞，t05 红）。
     *   t01 守的是另一种退化：**没零点时退回某个默认基线**（例如拿上一首的末事件顶上），
     *   那才是原 bug 的形态 —— 那时这里会拿到一个有限的 ms 值，立刻红。
     */
    private static void t01_noZeroMeansUnknown() {
        double r = CourseBound.nextBaseAfterFailure(Double.NaN, 168_000.0);
        ok(Double.isNaN(r), "t01 零点未建立 ⇒ 下界不可知（NaN），实际 " + r);
    }

    /** 长度未知 ⇒ 同样不可知（否则会用"上一首末事件"，早退到上一首的尾巴）。 */
    private static void t02_noLengthMeansUnknown() {
        ok(Double.isNaN(CourseBound.nextBaseAfterFailure(1_000_000.0, 0.0)),
                "t02 length=0 ⇒ NaN");
        ok(Double.isNaN(CourseBound.nextBaseAfterFailure(1_000_000.0, -5.0)),
                "t02 length<0 ⇒ NaN");
        ok(Double.isNaN(CourseBound.nextBaseAfterFailure(1_000_000.0, Double.NaN)),
                "t02 length=NaN ⇒ NaN");
    }

    /** 两者都有 ⇒ 就是加法，精确到 1 ms。 */
    private static void t03_knownZeroAndLengthIsExactSum() {
        double r = CourseBound.nextBaseAfterFailure(1_000_000.0, 168_020.0);
        ok(r == 1_168_020.0, "t03 zero+len 必须逐位相等（实际 " + r + "）");
    }

    /** 正常路径：有长度用 zero+len；没长度退回末事件（且不许比末事件更早）。 */
    private static void t04_normalPathBase() {
        ok(CourseBound.baseMs(1_000_000.0, 168_020.0, 9_999_999.0) == 1_168_020.0,
                "t04 有 length ⇒ zero+length");
        ok(CourseBound.baseMs(1_000_000.0, 0.0, 1_160_800.0) == 1_160_800.0,
                "t04 没 length ⇒ 退回上一首末事件");
    }

    /** 防御：无穷大的零点（除零/算飞）同样不能当"已知"。 */
    private static void t05_infiniteZeroRejected() {
        ok(Double.isNaN(CourseBound.nextBaseAfterFailure(Double.POSITIVE_INFINITY, 168_000.0)),
                "t05 零点 = +∞ ⇒ NaN");
        ok(Double.isNaN(CourseBound.nextBaseAfterFailure(Double.NEGATIVE_INFINITY, 168_000.0)),
                "t05 零点 = -∞ ⇒ NaN");
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

    private CourseBoundSelfTest() {
    }
}
