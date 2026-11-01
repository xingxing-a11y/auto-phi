package io.pgauto.play;

/**
 * `CalibGate`（G4 音频连续性 / G5 交叉校验）的自测（纯 JVM）。
 *
 * ★ 这两条闸门都属于"错了也不会报错"的类型：
 *   · G4 写松 ⇒ 中断的捕获被当成好样本，K 偏几百毫秒，整曲偏；
 *   · G4 写**紧/锚错** ⇒ 每轮都判中断，用户再也标不了定（这是更常见的坏法：
 *     窗口锚在捕获开头 ⇒ 天生 7 s 静音 ⇒ 100% 误报）。
 *   · G5 写松 ⇒ 锁到 UI 音效的 K 被写进偏好，之后一直用错值。
 *   ⇒ 所以断言必须同时覆盖「该拒的拒」和「不该拒的不拒」，还要锁住边界（79/80 帧、300.0/300.5 ms）。
 */
public final class CalibGateSelfTest {

    private static int pass = 0, fail = 0;

    public static void main(String[] args) {
        gap_detects_after_onset();
        gap_ignores_preexisting_silence();
        gap_boundary_79_vs_80();
        gap_short_window_does_not_lie();
        gap_missing_inputs();
        cross_with_lead();
        cross_without_lead();
        cross_degenerate();

        System.out.println("CalibGateSelfTest: " + pass + " 通过, " + fail + " 失败");
        if (fail > 0) throw new AssertionError("CalibGate 自测失败 " + fail + " 项");
    }

    // ─────────────────────────── G4 ───────────────────────────

    /** 起音后 1 s 处断 1 s（100 帧）⇒ 必须拒绝，且原因里带起点秒数。 */
    private static void gap_detects_after_onset() {
        double[] env = loud(600);
        silence(env, 400, 100);
        CalibGate.Verdict v = CalibGate.checkGap(env, 300);
        ok(!v.ok, "起音后 1 s 断 1 s 必须判中断，实得 " + v);
        ok(v.why.startsWith("gap@"), "原因必须是 gap@…，实得 " + v.why);
        ok(v.why.contains("4.00s"), "起点应是 4.00 s（帧 400），实得 " + v.why);
    }

    /**
     * ★ 最容易写错的一条：**点 ▶ 到起音之间的静音不算中断**。
     *   窗口若锚在捕获开头，这一条会红 —— 而它正是"每轮都误报"的根因。
     */
    private static void gap_ignores_preexisting_silence() {
        double[] env = loud(600);
        silence(env, 0, 300);                 // 起音前 3 s 全静音（真实场景就是 7 s）
        CalibGate.Verdict v = CalibGate.checkGap(env, 300);
        ok(v.ok, "起音前的静音不得判中断，实得 " + v);
        ok("gap=none".equals(v.why), "原因应为 gap=none，实得 " + v.why);
    }

    /** 边界：79 帧（0.79 s）放过，80 帧（0.80 s）必须拦。 */
    private static void gap_boundary_79_vs_80() {
        double[] a = loud(600);
        silence(a, 310, 79);
        ok(CalibGate.checkGap(a, 300).ok, "79 帧静音（0.79 s）不得判中断");

        double[] b = loud(600);
        silence(b, 310, 80);
        ok(!CalibGate.checkGap(b, 300).ok, "80 帧静音（0.80 s）必须判中断");
    }

    /**
     * 包络不够长（窗口 < 80 帧）时**不能冒充"通过"**：它必须显式说出 `n/a`，
     * 让日志/产物能区分「查过且干净」与「压根没查」。
     */
    private static void gap_short_window_does_not_lie() {
        double[] env = loud(340);              // onset 300 + 40 帧 ⇒ 窗口只有 30 帧
        CalibGate.Verdict v = CalibGate.checkGap(env, 300);
        ok(v.ok, "窗口不足时不该拒绝（没有证据说它坏了），实得 " + v);
        ok(v.why.startsWith("gap=n/a"), "必须显式标成 n/a，实得 " + v.why);
    }

    private static void gap_missing_inputs() {
        ok(!CalibGate.checkGap(null, 300).ok, "包络为 null 必须拒绝");
        ok(!CalibGate.checkGap(new double[0], 300).ok, "空包络必须拒绝");
        CalibGate.Verdict noOnset = CalibGate.checkGap(loud(600), 0);
        ok(!noOnset.ok, "门没锁定（onsetFrame=0）必须拒绝");
        ok("gap=no_onset".equals(noOnset.why), "原因要能区分 no_onset，实得 " + noOnset.why);
        CalibGate.Verdict negOnset = CalibGate.checkGap(loud(600), -1);
        ok(!negOnset.ok, "onsetFrame=-1 也必须拒绝（不能当成「从 0 开始」）");
    }

    // ─────────────────────────── G5 ───────────────────────────

    /**
     * 有 lead：`S` 必须与 `lead` 相符（±150 ms）。
     *
     * ★ 2026-10-25 20:3x 改口径：旧判据比的是 `lead + c`，等于给闸门加了一个 −c 的系统偏移
     *   （真机两轮 d=−173.3 / −153.3 就是它）。现在比的是 `lead` 本身 —— 因为 OnsetZero 的 onset
     *   来自挂钟（含 c），而零点的 L_dev 又减掉了 K+c，两条路径的 c 都正好抵消 ⇒ `lead = S`。
     */
    private static void cross_with_lead() {
        CalibGate.Verdict same = CalibGate.checkCross(7300.0, 7300.0);
        ok(same.ok, "S=lead=7300 必须通过，实得 " + same);

        // 两轮真机实测偏差：bounded_in +3.6、996 +23（修口径后都该过）
        ok(CalibGate.checkCross(7929.9, 7926.3).ok, "真机 bounded_in（+3.6）必须通过");
        ok(CalibGate.checkCross(7268.0, 7245.0).ok, "真机 996（+23）必须通过");

        CalibGate.Verdict edge = CalibGate.checkCross(7450.0, 7300.0);
        ok(edge.ok, "偏差正好 150 ms 应通过（判据是 >150），实得 " + edge);

        CalibGate.Verdict over = CalibGate.checkCross(7450.5, 7300.0);
        ok(!over.ok, "偏差 150.5 ms 必须拒绝，实得 " + over);
        ok(over.why.contains("d=+150.5"), "原因要带实际偏差，实得 " + over.why);

        // 旧口径的系统偏移量（−c=−177）现在必须被判掉 —— 这正是收紧要抓的那类样本
        ok(!CalibGate.checkCross(7123.0, 7300.0).ok,
                "按旧口径偏 −177 ms 的样本必须拒绝（修完口径它就不再是合法样本）");

        // 锁到 UI 音效的典型形态：S 早了 1.4 s
        CalibGate.Verdict early = CalibGate.checkCross(6100.0, 7300.0);
        ok(!early.ok, "S 早 1.4 s（疑似锁到 UI 音）必须拒绝，实得 " + early);
    }

    /** 没 lead 历史：退化成一个宽物理区间 [3 s, 15 s]。 */
    private static void cross_without_lead() {
        ok(CalibGate.checkCross(7000.0, null).ok, "无 lead 时 S=7000 应通过");
        ok(!CalibGate.checkCross(2000.0, null).ok, "无 lead 时 S=2000 必须拒绝（太早）");
        ok(!CalibGate.checkCross(16000.0, null).ok, "无 lead 时 S=16000 必须拒绝（太晚）");
        CalibGate.Verdict v = CalibGate.checkCross(7000.0, null);
        ok(v.why.contains("no_lead"), "原因要标明走了区间兜底，实得 " + v.why);
    }

    private static void cross_degenerate() {
        ok(!CalibGate.checkCross(Double.NaN, 7300.0).ok, "S=NaN 必须拒绝");
        // lead 是 NaN（而不是 null）时也不许当成"有 lead"去算偏差
        ok(CalibGate.checkCross(7000.0, Double.NaN).ok,
                "lead=NaN 应按「没有 lead」处理（走区间），S=7000 应通过");
    }

    // ─────────────────────────── 工具 ───────────────────────────

    /** 全"响"的包络：超过 300 RMS 的门限（比较的是**均方**，所以填 300²+1）。 */
    private static double[] loud(int n) {
        double[] env = new double[n];
        java.util.Arrays.fill(env, 300.0 * 300.0 + 1.0);
        return env;
    }

    /** 把 [from, from+len) 填成静音。 */
    private static void silence(double[] env, int from, int len) {
        for (int i = from; i < from + len && i < env.length; i++) env[i] = 0.0;
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
