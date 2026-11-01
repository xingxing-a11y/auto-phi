package io.pgauto.inject.sched;

import java.util.ArrayList;
import java.util.List;
import java.util.Locale;

/**
 * {@link InjectTrace} 的自测（纯 JVM，无 Android 依赖）。
 *
 * ★ 为什么这些断言值得写：trace 是**下一轮真机的唯一证据来源**。它自己错了不会报错，
 *   只会让"多指帧有没有被派发"再次无法定案 —— 那正是它存在的理由。
 *   所以这里盯的不是"能生成字符串"，而是四类会让结论失真的具体错法：
 *   ① 缺值静默写成 0（首帧 et_back）② 统计被截断逻辑短路（样本缺损）
 *   ③ locale 影响小数点 ⇒ CSV 破列 ④ act/idx 编码错 ⇒ 多指帧被当单指。
 */
public final class InjectTraceSelfTest {

    private static int pass, fail;

    public static void main(String[] args) {
        t01HeaderAndColumns();
        t02FirstFrameBackIsDash();
        t03ActionAndIndex();
        t04PointerSetRoundTrip();
        t05SingleMultiSplit();
        t06EventTimeBackwards();
        t07EtLate();
        t08TruncationKeepsStats();
        t09LocaleMustNotBreakCsv();
        t10EmptyTrace();
        t11InjectModeNote();
        t12NoNoteByDefault();
        t13ModeNoteOverwrite();
        t14RunMetaBeforeHeader();
        t15RunMetaAbsentByDefault();
        t16RunMetaRejectsMultilineAndBlank();
        t17TailNoteAfterDetails();
        t18TailNoteAbsentByDefault();

        System.out.println("InjectTraceSelfTest: " + pass + " 通过, " + fail + " 失败");
        if (fail > 0) throw new AssertionError("InjectTrace 自测失败 " + fail + " 项");
    }

    // ─────────────────────────── 用例 ───────────────────────────

    private static void t01HeaderAndColumns() {
        InjectTrace t = new InjectTrace();
        t.record(f1(0, Gesture.ACTION_DOWN, 3, 100, 200), true, 3000, 0.5, 1000L, 1001L);
        String[] lines = t.csv().split("\n");
        ok(InjectTrace.HEADER.equals(lines[0]), "表头必须逐字等于 HEADER，实得 " + lines[0]);
        ok(lines.length == 2, "1 帧 ⇒ 表头 + 1 行，实得 " + lines.length + " 行");
        ok(cols(lines[1]).length == 11, "列数必须 11，实得 " + cols(lines[1]).length);
    }

    private static void t02FirstFrameBackIsDash() {
        InjectTrace t = new InjectTrace();
        t.record(f1(0, Gesture.ACTION_DOWN, 1, 10, 20), true, 100, 0.0, 500L, 500L);
        String back = cols(t.csv().split("\n")[1])[10];
        ok("-".equals(back), "首帧 et_back_ms 必须是 '-'（缺值），实得 '" + back + "'");
    }

    private static void t03ActionAndIndex() {
        InjectTrace t = new InjectTrace();
        t.record(f1(0, Gesture.ACTION_DOWN, 1, 1, 1), true, 100, 0, 1L, 1L);
        t.record(f2(10, Gesture.ACTION_POINTER_DOWN | (2 << Gesture.ACTION_POINTER_INDEX_SHIFT),
                new int[]{4, 5, 6}, new int[]{1, 2, 3}, new int[]{1, 2, 3}), true, 100, 0, 2L, 2L);
        t.record(f2(20, Gesture.ACTION_POINTER_UP | (1 << Gesture.ACTION_POINTER_INDEX_SHIFT),
                new int[]{4, 5}, new int[]{1, 2}, new int[]{1, 2}), true, 100, 0, 3L, 3L);
        t.record(f1(30, Gesture.ACTION_MOVE, 4, 9, 9), true, 100, 0, 4L, 4L);
        String[] l = t.csv().split("\n");
        ok("0".equals(cols(l[1])[2]) && "-1".equals(cols(l[1])[3]),
                "DOWN 必须 act=0 idx=-1，实得 " + cols(l[1])[2] + "/" + cols(l[1])[3]);
        ok("5".equals(cols(l[2])[2]) && "2".equals(cols(l[2])[3]),
                "POINTER_DOWN|2<<8 必须 act=5 idx=2，实得 " + cols(l[2])[2] + "/" + cols(l[2])[3]);
        ok("6".equals(cols(l[3])[2]) && "1".equals(cols(l[3])[3]),
                "POINTER_UP|1<<8 必须 act=6 idx=1，实得 " + cols(l[3])[2] + "/" + cols(l[3])[3]);
        ok("2".equals(cols(l[4])[2]) && "-1".equals(cols(l[4])[3]),
                "MOVE 必须 act=2 idx=-1");
    }

    private static void t04PointerSetRoundTrip() {
        InjectTrace t = new InjectTrace();
        t.record(f2(5, Gesture.ACTION_MOVE, new int[]{3, 7}, new int[]{12345, 20},
                new int[]{987, 40}), true, 100, 0, 1L, 1L);
        String[] c = cols(t.csv().split("\n")[1]);
        ok("3:12345:987|7:20:40".equals(c[5]),
                "指针集必须 `id:x:y|id:x:y`，实得 " + c[5]);
        ok(c.length == 11, "含 5 位坐标时列数仍必须是 11，实得 " + c.length);
        ok("2".equals(c[4]), "n 必须 2，实得 " + c[4]);
    }

    private static void t05SingleMultiSplit() {
        InjectTrace t = new InjectTrace();
        t.record(f1(0, Gesture.ACTION_DOWN, 1, 1, 1), true, 100, 0, 1L, 1L);
        t.record(f2(10, Gesture.ACTION_POINTER_DOWN | (1 << 8), new int[]{1, 2},
                new int[]{1, 2}, new int[]{1, 2}), false, 100, 0, 2L, 2L);
        t.record(f2(20, Gesture.ACTION_MOVE, new int[]{1, 2}, new int[]{1, 2},
                new int[]{1, 2}), true, 100, 0, 3L, 3L);
        ok(t.nMulti() == 2, "多指帧应 2，实得 " + t.nMulti());
        ok(t.nMultiFalse() == 1, "多指 false 应 1，实得 " + t.nMultiFalse());
        ok(t.nSingleFalse() == 0, "单指 false 应 0，实得 " + t.nSingleFalse());
    }

    private static void t06EventTimeBackwards() {
        InjectTrace t = new InjectTrace();
        t.record(f1(0, Gesture.ACTION_DOWN, 1, 1, 1), true, 100, 0, 1000L, 1000L);
        t.record(f1(5, Gesture.ACTION_MOVE, 1, 1, 1), true, 100, 0, 1005L, 1006L);
        t.record(f1(10, Gesture.ACTION_MOVE, 1, 1, 1), true, 100, 0, 1003L, 1004L);
        ok(t.nEtBack() == 1, "倒退帧应 1，实得 " + t.nEtBack());
        String[] l = t.csv().split("\n");
        // ★ 前进的帧记负值（不是 0）：下游据此能区分"没倒退"与"缺值"，两者含义不同
        ok("-5".equals(cols(l[2])[10]), "第 2 帧 eventTime 前进 5 ms ⇒ -5，实得 " + cols(l[2])[10]);
        ok("2".equals(cols(l[3])[10]), "第 3 帧倒退 2 ms，实得 " + cols(l[3])[10]);
    }

    private static void t07EtLate() {
        InjectTrace t = new InjectTrace();
        t.record(f1(0, Gesture.ACTION_DOWN, 1, 1, 1), true, 100, 0, 1000L, 1140L);
        String c = cols(t.csv().split("\n")[1])[9];
        ok("140".equals(c), "et_late_ms 必须 140，实得 " + c);
    }

    private static void t08TruncationKeepsStats() {
        InjectTrace t = new InjectTrace();
        int total = InjectTrace.CAP + 5;
        for (int i = 0; i < total; i++) {
            boolean tail = i >= InjectTrace.CAP;
            Gesture.Frame f = tail
                    ? f2(i, Gesture.ACTION_MOVE, new int[]{1, 2}, new int[]{1, 2}, new int[]{1, 2})
                    : f1(i, Gesture.ACTION_MOVE, 1, 1, 1);
            t.record(f, !tail, 100, 0, 1000L + i, 1000L + i);   // 尾部 5 帧多指且 ret=false
        }
        ok(t.lines() == total, "总帧必须 " + total + "，实得 " + t.lines());
        ok(t.written() == InjectTrace.CAP, "明细必须截到 " + InjectTrace.CAP + "，实得 " + t.written());
        ok(t.truncated() == 5, "截断数必须 5，实得 " + t.truncated());
        // ★ 核心不变量：统计不能被截断短路 —— 否则"多指 false 为 0"是假的
        ok(t.nMultiFalse() == 5, "被截断的 5 帧多指 false 仍必须计入，实得 " + t.nMultiFalse());
        String csv = t.csv();
        int detail = 0;
        boolean sawTruncNote = false;
        for (String line : csv.split("\n")) {
            if (line.startsWith("#")) { sawTruncNote = line.contains("truncated 5"); continue; }
            if (!line.isEmpty()) detail++;
        }
        ok(detail == InjectTrace.CAP + 1, "CSV 明细行必须 CAP+表头，实得 " + detail);
        ok(sawTruncNote, "CSV 尾部必须写明截断条数（否则下游会当成'那段时间没发事件'）");
    }

    /**
     * ★ locale 陷阱：`String.format("%.3f")` 不指定 locale 时，德语区会输出 `1,235`，
     *   CSV 立刻多一列 ⇒ 下游把 cost_us 读成 late_ms，全部结论作废且不报错。
     */
    private static void t09LocaleMustNotBreakCsv() {
        Locale old = Locale.getDefault();
        try {
            Locale.setDefault(Locale.GERMANY);
            InjectTrace t = new InjectTrace();
            t.record(f1(5, Gesture.ACTION_DOWN, 1, 1, 1), true, 100, 1.5, 1000L, 1000L);
            String[] c = cols(t.csv().split("\n")[1]);
            ok(c.length == 11, "德语默认 locale 下列数仍必须 11，实得 " + c.length);
            ok("5.000".equals(c[1]), "t_ms 必须用点号小数，实得 " + c[1]);
        } finally {
            Locale.setDefault(old);
        }
    }

    private static void t10EmptyTrace() {
        InjectTrace t = new InjectTrace();
        String[] lines = t.csv().split("\n");
        ok(t.lines() == 0 && lines.length == 1, "空 trace 只应有表头，实得 " + lines.length + " 行");
        ok(!t.brief().isEmpty(), "空 trace 的 brief 不该炸");
    }

    /**
     * 注入模式必须跟着产物走（FACTS §167）。
     *
     * ★ 这条防的是「一整枪无法归因」：E2 想验证同步注入是否生效，唯一的事后证据是
     *   logcat 与 intent extra，两者都会消失。所以模式必须写在 CSV 里，且必须让
     *   下游解析器照常读到全部明细 —— 注释行放错位置就会把表头吃掉。
     */
    private static void t11InjectModeNote() {
        InjectTrace t = new InjectTrace();
        t.setInjectMode(1, "wait");
        t.record(f1(0, Gesture.ACTION_DOWN, 3, 100, 200), true, 3000, 0.5, 1000L, 1001L);
        t.record(f1(10, Gesture.ACTION_UP, 3, 100, 200), true, 2000, 0.0, 1010L, 1010L);
        String[] lines = t.csv().split("\n");
        ok("# inject_mode=wait(1)".equals(lines[0]),
                "首行必须是模式注释，实得 '" + lines[0] + "'");
        ok(InjectTrace.HEADER.equals(lines[1]),
                "第二行必须是表头（下游靠它定位 COLS），实得 '" + lines[1] + "'");
        ok(lines.length == 4, "注释 + 表头 + 2 帧，实得 " + lines.length + " 行");
        ok(cols(lines[2]).length == 11 && cols(lines[3]).length == 11,
                "模式注释不得影响明细列数，实得 " + cols(lines[2]).length + "/" + cols(lines[3]).length);
        ok(t.brief().startsWith("inject_mode=wait(1)"),
                "brief 必须自带模式口径，实得 '" + t.brief() + "'");
    }

    /** 不设模式时行为必须逐位不变：否则「默认 async」这句承诺在产物层面就假了。 */
    private static void t12NoNoteByDefault() {
        InjectTrace t = new InjectTrace();
        t.record(f1(0, Gesture.ACTION_DOWN, 1, 10, 20), true, 100, 0.0, 500L, 500L);
        String[] lines = t.csv().split("\n");
        ok(InjectTrace.HEADER.equals(lines[0]), "未设模式时首行必须仍是表头");
        ok(!t.brief().contains("inject_mode"),
                "未设模式时 brief 不该凭空出现模式字样，实得 '" + t.brief() + "'");
    }

    /** 重复设置以最后一次为准（同一 Job 不应出现两个模式，但接口不该留下不确定行为）。 */
    private static void t13ModeNoteOverwrite() {
        InjectTrace t = new InjectTrace();
        t.setInjectMode(1, "wait");
        t.setInjectMode(0, "async");
        String[] lines = t.csv().split("\n");
        ok("# inject_mode=async(0)".equals(lines[0]),
                "后设的模式必须覆盖先前值，实得 '" + lines[0] + "'");
        ok(lines.length == 2, "只能有一行模式注释，实得 " + lines.length + " 行");
    }

    /**
     * 本轮参数行必须落在**表头之前**，且与模式行并存 —— 下游靠 `#` 前缀跳过注释，
     * 一旦顺序反了（参数行在表头之后），`analyze_inject_trace.py` 会把它当数据行。
     */
    private static void t14RunMetaBeforeHeader() {
        InjectTrace t = new InjectTrace();
        t.setInjectMode(1, "wait");
        t.setRunMeta("song=bounded_in sil_thr=280.739 l_dev=0.180 src=onset zero_rel_ms=7071.9");
        t.record(f1(0, Gesture.ACTION_DOWN, 1, 10, 20), true, 100, 0.0, 500L, 500L);
        String[] lines = t.csv().split("\n");
        ok("# inject_mode=wait(1)".equals(lines[0]), "第 0 行仍是模式，实得 '" + lines[0] + "'");
        ok(lines[1].startsWith("# run "), "第 1 行必须是参数行，实得 '" + lines[1] + "'");
        ok(lines[1].contains("l_dev=0.180") && lines[1].contains("song=bounded_in"),
                "参数行必须带上这枪真正用的值，实得 '" + lines[1] + "'");
        ok(InjectTrace.HEADER.equals(lines[2]), "表头必须紧跟注释，实得 '" + lines[2] + "'");
        ok(cols(lines[3]).length == 11, "参数行不得影响明细列数，实得 " + cols(lines[3]).length);
    }

    /** 不设参数 ⇒ 逐位回到「只有表头」的形态（老调用方不受影响）。 */
    private static void t15RunMetaAbsentByDefault() {
        InjectTrace t = new InjectTrace();
        t.record(f1(0, Gesture.ACTION_DOWN, 1, 10, 20), true, 100, 0.0, 500L, 500L);
        String[] lines = t.csv().split("\n");
        ok(InjectTrace.HEADER.equals(lines[0]), "未设参数时首行必须仍是表头");
        for (String l : lines) {
            ok(!l.startsWith("# run"), "未设参数时不得出现参数行，实得 '" + l + "'");
            break;
        }
    }

    /**
     * 参数里出现换行 ⇒ 必须压成空格（否则一条参数能把表头挤到第二行以外）；
     * 空串 / 全空白 ⇒ 当没设（不写空注释行）。
     */
    private static void t16RunMetaRejectsMultilineAndBlank() {
        InjectTrace t = new InjectTrace();
        t.setRunMeta("a=1\r\nb=2\nc=3");
        String[] lines = t.csv().split("\n");
        ok("# run a=1  b=2 c=3".equals(lines[0]),
                "换行必须压成空格且只有一行注释，实得 '" + lines[0] + "'");
        ok(InjectTrace.HEADER.equals(lines[1]), "表头必须紧跟注释，实得 '" + lines[1] + "'");

        InjectTrace t2 = new InjectTrace();
        t2.setRunMeta("   ");
        ok(InjectTrace.HEADER.equals(t2.csv().split("\n")[0]),
                "空白参数不得写出一行空注释");
        t2.setRunMeta("x=1");
        t2.setRunMeta(null);
        ok(InjectTrace.HEADER.equals(t2.csv().split("\n")[0]),
                "setRunMeta(null) 必须撤掉参数行");
    }

    /**
     * G10 收尾注释：注入结束**之后**才算出来的参数（lead 测量值 / K 测量值）必须
     * 落在明细之后、以 `#` 开头，且不影响表头与明细列数。
     *
     * ★ 这条盯的是一个具体的伪造风险：把"测到的值"写进表头那一行，看起来更整齐，
     *   但时序是假的（那时候还没测出来）。所以顺序本身就是判据。
     */
    private static void t17TailNoteAfterDetails() {
        InjectTrace t = new InjectTrace();
        t.setInjectMode(1, "wait");
        t.setRunMeta("song=bounded_in k_used=47.8");
        t.record(f1(0, Gesture.ACTION_DOWN, 1, 10, 20), true, 100, 0.0, 500L, 500L);
        t.record(f1(16.7, Gesture.ACTION_UP, 1, 10, 20), true, 90, 0.0, 516L, 516L);
        t.addTailNote("lead lead_meas=7201.0 lead_after=7224.5 lead_n=3");
        t.addTailNote("calib k_meas=49.5 k_after=47.8 k_n=4");
        String[] lines = t.csv().split("\n");
        ok(InjectTrace.HEADER.equals(lines[2]), "表头不受收尾注释影响，实得 '" + lines[2] + "'");
        ok(cols(lines[3]).length == 11, "明细行仍是 11 列，实得 " + cols(lines[3]).length);
        ok(cols(lines[4]).length == 11, "第二条明细仍是 11 列，实得 " + cols(lines[4]).length);
        ok(lines[5].startsWith("# lead "), "收尾注释必须紧跟明细，实得 '" + lines[5] + "'");
        ok(lines[5].contains("lead_meas=7201.0"), "lead 收尾行必须带测量值，实得 '" + lines[5] + "'");
        ok(lines[6].startsWith("# calib "), "第二条收尾注释也要写，实得 '" + lines[6] + "'");
        ok(!lines[0].contains("lead_meas"), "测量值不得出现在表头前（时序造假）");
    }

    /** 不设收尾注释 ⇒ 逐位回到旧形态（老调用方与老解析器不受影响）。 */
    private static void t18TailNoteAbsentByDefault() {
        InjectTrace t = new InjectTrace();
        t.record(f1(0, Gesture.ACTION_DOWN, 1, 10, 20), true, 100, 0.0, 500L, 500L);
        String csv = t.csv();
        ok(csv.equals(InjectTrace.HEADER + "\n"
                        + "1,0.000,0,-1,1,1:10:20,1,100,0.000,0,-" + "\n"),
                "未设收尾注释时 csv 必须逐位等于旧形态，实得:\n" + csv);
        InjectTrace t2 = new InjectTrace();
        t2.addTailNote("   ");
        t2.addTailNote(null);
        ok(t2.csv().equals(InjectTrace.HEADER + "\n"), "空白/null 收尾注释不得写出空注释行");
        InjectTrace t3 = new InjectTrace();
        t3.addTailNote("a=1\nb=2");
        ok(t3.csv().contains("# a=1 b=2\n"), "收尾注释里的换行也必须压成空格，实得:\n" + t3.csv());
        ok(!t3.csv().contains("\nb=2"), "换行没压住会把一行注释拆成两行");
    }

    // ─────────────────────────── 工具 ───────────────────────────
    private static Gesture.Frame f1(double t, int action, int id, int x, int y) {
        return new Gesture.Frame(t, action, 1, new int[]{id}, new int[]{x}, new int[]{y});
    }

    private static Gesture.Frame f2(double t, int action, int[] ids, int[] xs, int[] ys) {
        return new Gesture.Frame(t, action, ids.length, ids, xs, ys);
    }

    /** ★ 必须用 split(",", -1)：默认 split 会吃掉行尾的空字段，"缺列"就查不出来了。 */
    private static String[] cols(String line) {
        return line.split(",", -1);
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
