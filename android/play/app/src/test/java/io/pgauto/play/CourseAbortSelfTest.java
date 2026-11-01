package io.pgauto.play;

/**
 * {@link CourseAbort} 的离线单测 —— **纯 JVM，不需要 Android、不需要真机**。
 *
 * 每个用例都对应"中止取证"里的一个真实坏法，删掉实现里的判据必须立刻红：
 *   · t01：CSV 行必须带 `# aborted: ` 前缀 + 结尾换行（否则分析脚本读不到）；
 *   · t02：原因里的换行必须被压成空格 —— 否则一行变两行，sidecar 结构直接坏掉；
 *   · t03：null / 空白不能抛、也不能写出 `null`；
 *   · t04：Toast 必须带"已完成 N 首"（判断打到第几首才断的关键）；
 *   · t05：超长原因必须截断到 {@link CourseAbort#TOAST_MAX}；
 *   · t06：负数次数夹到 0（防御）；
 *   · t07：`IllegalStateException: …` 的类名前缀必须剥掉（CSV 与 Toast 都剥，日志不剥）；
 *   · t08：只有类名、没有 message（`NullPointerException`）⇒ **不许**剥成空串；
 *   · t09：冒号后只有空白 ⇒ 同上，保留原样；
 *   · t10：中文原因（`零点是 NaN: …`）不许被误剥；
 *   · t11：带包名的全限定类名（`java.lang.IllegalStateException: …`）也要剥干净。
 *
 * 运行：`bash android/play/selftest.sh`
 */
public final class CourseAbortSelfTest {

    private static int pass = 0;
    private static int fail = 0;

    public static void main(String[] args) {
        t01_csvLineHasPrefixAndNewline();
        t02_newlinesAreCollapsed();
        t03_nullIsSafe();
        t04_toastCarriesDoneCount();
        t05_toastIsTruncated();
        t06_negativeDoneClamped();
        t07_classPrefixStripped();
        t08_classOnlyKept();
        t09_blankAfterColonKept();
        t10_chineseReasonUntouched();
        t11_fullyQualifiedStripped();
        System.out.println("\nCourseAbortSelfTest: " + pass + " passed, " + fail + " failed");
        if (fail > 0) {
            System.exit(1);
        }
    }

    private static void t01_csvLineHasPrefixAndNewline() {
        String l = CourseAbort.csvLine("第 2 首连零点都没建立");
        ok(l.equals("# aborted: 第 2 首连零点都没建立\n"),
                "t01 CSV 行 = 前缀 + 原因 + 换行（实际 " + esc(l) + "）");
    }

    /** ★ 关键：换行必须压平，否则一行变两行、sidecar 结构坏掉。 */
    private static void t02_newlinesAreCollapsed() {
        String l = CourseAbort.csvLine("第 2 首\n连零点\r都没\t建立");
        ok(l.indexOf('\n') == l.length() - 1, "t02 只能有结尾那一个换行（实际 " + esc(l) + "）");
        ok(l.contains("第 2 首 连零点 都没 建立"), "t02 换行/制表要变成空格（实际 " + esc(l) + "）");
    }

    private static void t03_nullIsSafe() {
        ok("# aborted: \n".equals(CourseAbort.csvLine(null)), "t03 null ⇒ 空原因，不写 \"null\"");
        ok("# aborted: \n".equals(CourseAbort.csvLine("   ")), "t03 纯空白 ⇒ 空原因");
    }

    private static void t04_toastCarriesDoneCount() {
        String t = CourseAbort.toastText("第 2 首超时", 1);
        ok(t.startsWith("课题模式中止（已完成 1 首）："), "t04 Toast 要带已完成首数（实际 " + esc(t) + "）");
        ok(t.contains("第 2 首超时"), "t04 Toast 要带原因（实际 " + esc(t) + "）");
    }

    private static void t05_toastIsTruncated() {
        StringBuilder b = new StringBuilder();
        for (int i = 0; i < 300; i++) {
            b.append('长');
        }
        String t = CourseAbort.toastText(b.toString(), 2);
        ok(t.length() == CourseAbort.TOAST_MAX,
                "t05 超长原因截到 " + CourseAbort.TOAST_MAX + " 字（实际 " + t.length() + "）");
        ok(t.endsWith("…"), "t05 截断要有省略号");
    }

    private static void t06_negativeDoneClamped() {
        ok(CourseAbort.toastText("x", -3).startsWith("课题模式中止（已完成 0 首）："),
                "t06 负数次数夹到 0");
    }

    /** ★ 2026-10-29 真机 sidecar 实测：`# aborted: 中止：IllegalStateException: 第 2 首…`。 */
    private static void t07_classPrefixStripped() {
        String l = CourseAbort.csvLine("IllegalStateException: 第 2 首连零点都没建立");
        ok(l.equals("# aborted: 第 2 首连零点都没建立\n"),
                "t07 CSV 剥掉类名前缀（实际 " + esc(l) + "）");
        String t = CourseAbort.toastText("IllegalStateException: 第 2 首连零点都没建立", 1);
        ok(!t.contains("IllegalStateException"), "t07 Toast 也不许带类名（实际 " + esc(t) + "）");
        ok(t.contains("第 2 首连零点都没建立"), "t07 剥完原因必须还在（实际 " + esc(t) + "）");
    }

    /** 没有 message 时剥完是空串 —— 那等于丢信息，必须原样保留。 */
    private static void t08_classOnlyKept() {
        String l = CourseAbort.csvLine("NullPointerException");
        ok(l.equals("# aborted: NullPointerException\n"),
                "t08 只有类名时不许剥空（实际 " + esc(l) + "）");
    }

    private static void t09_blankAfterColonKept() {
        // ⚠ 期望值里**不能**带那 4 个尾部空格：oneLine 先 trim，剥之前串已经是
        //   "IllegalStateException:"（冒号后为空）。第一版期望写了带空格的原文 ⇒ 必红。
        String l = CourseAbort.csvLine("IllegalStateException:    ");
        ok(l.equals("# aborted: IllegalStateException:\n"),
                "t09 冒号后只有空白时保留原样（实际 " + esc(l) + "）");
    }

    /** 中文原因首字符不是 ASCII 字母 ⇒ 一个字符都不许动。 */
    private static void t10_chineseReasonUntouched() {
        String l = CourseAbort.csvLine("零点是 NaN: 第 2 首");
        ok(l.equals("# aborted: 零点是 NaN: 第 2 首\n"),
                "t10 中文原因不许被误剥（实际 " + esc(l) + "）");
    }

    private static void t11_fullyQualifiedStripped() {
        String l = CourseAbort.csvLine("java.lang.IllegalStateException: 第 2 首");
        ok(l.equals("# aborted: 第 2 首\n"),
                "t11 带包名的类名也要剥干净（实际 " + esc(l) + "）");
    }

    private static String esc(String s) {
        return s == null ? "null" : s.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t");
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

    private CourseAbortSelfTest() {
    }
}
