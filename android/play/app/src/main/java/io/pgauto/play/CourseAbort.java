package io.pgauto.play;

/**
 * 课题模式「中止 / 停止」的**落盘行与 Toast 文案**（纯逻辑，2026-10-28）。
 *
 * 为什么单独成类：`PlayService` 是 Android Service，不在 `selftest.sh` 的 JVM 编译集里
 * （故意不带 android.jar）⇒ 文案/格式写在 Service 里就永远拿不到离线单测。
 *
 * ★ 存在的理由（2026-10-28 诊断轮实测暴露的两个缺口）：
 *   1. **中止原因不落盘**：端内日志只有 16 行、下一轮就被覆盖 ⇒ 事后只能靠"少一份 trace"
 *      推断，判不出是超时、Shizuku 掉了、还是用户按了停止；
 *   2. **中止时人在游戏里看不到**：本次用户就以为"没生效"。
 *   ⇒ 原因写进 `course_*.csv`（`#` 行按注释跳过，与 trace 的 header 约定一致），
 *     同时在游戏画面上弹一条 Toast。
 *
 * ⚠ CSV 行**必须单行**：原因里若带换行会把 sidecar 的结构弄坏（分析脚本按行读）。
 */
public final class CourseAbort {

    /** sidecar 里中止行的前缀（`#` = 注释，现有分析脚本都跳过 `#` 行）。 */
    public static final String CSV_PREFIX = "# aborted: ";

    /** Toast 正文上限：超过就截断，一条 Toast 不该占满屏。 */
    public static final int TOAST_MAX = 110;

    /** sidecar 的一行：`# aborted: <原因>`（带结尾换行；保证单行）。 */
    public static String csvLine(String reason) {
        return CSV_PREFIX + stripClassPrefix(oneLine(reason)) + "\n";
    }

    /** Toast 文案：`课题模式中止（已完成 N 首）：<原因>`，超长截断。 */
    public static String toastText(String reason, int done) {
        String head = "课题模式中止（已完成 " + Math.max(0, done) + " 首）：";
        String s = head + stripClassPrefix(oneLine(reason));
        if (s.length() <= TOAST_MAX) {
            return s;
        }
        return s.substring(0, TOAST_MAX - 1) + "…";
    }

    /**
     * 剥掉 `ShizukuProbe.brief` 加的 `<异常类名>: ` 前缀
     * （`IllegalStateException: 第 2 首…` → `第 2 首…`）。
     *
     * ★ 为什么剥、且**只在**这里剥（2026-10-29 用户要求）：端内日志面板需要类名
     *   （异常类型是第一诊断信息）⇒ `RunLog.line("!! 课题模式中止：" + why)` 用原串不动；
     *   而 CSV 行与 Toast 是给人读的 —— `IllegalStateException: ` 占 22 个字符，
     *   在 Toast 的 {@link #TOAST_MAX} 上限里会挤掉真正的解释。
     *
     * ★ 只认「标识符序列 + ': '」这一种形态，且**简单名**（最后一个 `.` 之后）必须以大写字母开头：
     *   · `java.lang.IllegalStateException: …` ⇒ 简单名大写 ⇒ 剥（首字符是小写 'j'，所以不能拿首字符判）；
     *   · `NullPointerException`（无 message，无冒号）⇒ 原样保留，剥完是空串等于丢信息；
     *   · `IllegalStateException: `（冒号后没内容）⇒ 原样保留，同理；
     *   · `零点是 NaN: …` 这类中文原因 ⇒ 标识符序列在第 0 个字符就断了 ⇒ 不动。
     */
    static String stripClassPrefix(String s) {
        if (s == null || s.isEmpty()) {
            return "";
        }
        int i = 0;
        while (i < s.length()) {
            char c = s.charAt(i);
            boolean ident = (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z')
                    || (c >= '0' && c <= '9') || c == '_' || c == '$' || c == '.';
            if (!ident) {
                break;
            }
            i++;
        }
        if (i == 0 || i + 1 >= s.length() || s.charAt(i) != ':' || s.charAt(i + 1) != ' ') {
            return s;
        }
        String head = s.substring(0, i);
        int dot = head.lastIndexOf('.');
        String simple = dot >= 0 ? head.substring(dot + 1) : head;
        if (simple.isEmpty() || simple.charAt(0) < 'A' || simple.charAt(0) > 'Z') {
            return s;
        }
        String rest = s.substring(i + 2).trim();
        return rest.isEmpty() ? s : rest;
    }

    /** 换行 / 回车 / 制表一律压成空格，再去首尾空白；null → 空串。 */
    static String oneLine(String s) {
        if (s == null) {
            return "";
        }
        StringBuilder b = new StringBuilder(s.length());
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            b.append((c == '\n' || c == '\r' || c == '\t') ? ' ' : c);
        }
        return b.toString().trim();
    }

    private CourseAbort() {
    }
}
