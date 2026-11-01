package io.pgauto.inject.sched;

/**
 * {@link RecvTrace} 的自测（纯 JVM，无 Android 依赖）。
 *
 * ★ 为什么这些断言值得写：收货侧 CSV 是「派发层有没有罪」的唯一证据。
 *   它自己错了不会报错，只会让 780 个 Miss 永远停留在"大概不是派发问题"。
 *   所以这里盯的都是会让结论失真的具体错法：
 *   ① 存了 MotionEvent 引用而不是拷值（真机上事件被池化复用 ⇒ 全成垃圾）
 *   ② POINTER_UP 后没把指从按下集合里删 ⇒ 后续全被误报孤儿
 *   ③ 截断把统计一起短路 ⇒ "收到条数"看似对上、实际是抽样
 *   ④ 坐标用截断而不是四舍五入 ⇒ 与发送侧 int 坐标系统性偏 1 px
 *   ⑤ idx 对非 POINTER_* 动作也记 ⇒ 对账时把 MOVE 当多指动作
 */
public final class RecvTraceSelfTest {

    private static int pass, fail;

    public static void main(String[] args) {
        t01HeaderAndColumns();
        t02ActionIndexEncoding();
        t03PointerSetRoundTrip();
        t04Counters();
        t05MaxPointers();
        t06DelayNotStoredWhenNegative();
        t07OrphanUp();
        t08DownWhileHeld();
        t09TruncationKeepsStats();
        t10PointerUpShrinksSet();
        t11RemainingPointerTrackedAfterPointerUp();
        t12CancelClearsSet();
        t13EmptyTrace();
        t14CoordRounding();

        System.out.println("RecvTraceSelfTest: " + pass + " 通过, " + fail + " 失败");
        if (fail > 0) throw new AssertionError("RecvTrace 自测失败 " + fail + " 项");
    }

    // ─────────────────────────── 用例 ───────────────────────────

    private static void t01HeaderAndColumns() {
        RecvTrace t = new RecvTrace();
        t.record(1000, 995, Gesture.ACTION_DOWN, 1, ids(0), xs(100), ys(200));
        String[] l = t.csv().split("\n");
        ok(RecvTrace.HEADER.equals(l[0]), "表头不符：" + l[0]);
        ok(cols(l[1]).length == 8, "明细应 8 列，实得 " + cols(l[1]).length);
        ok("1".equals(cols(l[1])[0]), "seq 应 1");
        ok("1000".equals(cols(l[1])[1]), "recv_ms 应 1000");
        ok("5".equals(cols(l[1])[7]), "dly 应 5");
    }

    private static void t02ActionIndexEncoding() {
        RecvTrace t = new RecvTrace();
        int pd = Gesture.ACTION_POINTER_DOWN | (1 << Gesture.ACTION_POINTER_INDEX_SHIFT);
        int pu = Gesture.ACTION_POINTER_UP | (0 << Gesture.ACTION_POINTER_INDEX_SHIFT);
        t.record(1, 1, Gesture.ACTION_DOWN, 1, ids(0), xs(10), ys(10));
        t.record(2, 2, pd, 2, ids(0, 1), xs(10, 20), ys(10, 20));
        t.record(3, 3, Gesture.ACTION_MOVE, 2, ids(0, 1), xs(11, 21), ys(10, 20));
        t.record(4, 4, pu, 2, ids(0, 1), xs(11, 21), ys(10, 20));
        String[] l = t.csv().split("\n");
        ok("1".equals(cols(l[2])[4]), "POINTER_DOWN 的 idx 应 1，实得 " + cols(l[2])[4]);
        ok("-1".equals(cols(l[3])[4]), "MOVE 的 idx 应 -1，实得 " + cols(l[3])[4]);
        ok("0".equals(cols(l[4])[4]), "POINTER_UP 的 idx 应 0，实得 " + cols(l[4])[4]);
    }

    private static void t03PointerSetRoundTrip() {
        RecvTrace t = new RecvTrace();
        t.record(1, 1, Gesture.ACTION_DOWN, 1, ids(0), xs(100), ys(200));
        t.record(2, 2, Gesture.ACTION_POINTER_DOWN, 2, ids(0, 3), xs(100, 300), ys(200, 400));
        String[] l = t.csv().split("\n");
        // 与 InjectTrace 同格式：id:x:y|id:x:y（用 | 与 : 分隔，绝不含逗号）
        ok("0:100:200".equals(cols(l[1])[6]), "单指 ptrs 应 0:100:200，实得 " + cols(l[1])[6]);
        ok("0:100:200|3:300:400".equals(cols(l[2])[6]),
                "双指 ptrs 应 0:100:200|3:300:400，实得 " + cols(l[2])[6]);
    }

    private static void t04Counters() {
        RecvTrace t = new RecvTrace();
        t.record(1, 1, Gesture.ACTION_DOWN, 1, ids(0), xs(1), ys(1));
        t.record(2, 2, Gesture.ACTION_MOVE, 1, ids(0), xs(2), ys(2));
        t.record(3, 3, Gesture.ACTION_MOVE, 1, ids(0), xs(3), ys(3));
        t.record(4, 4, Gesture.ACTION_UP, 1, ids(0), xs(4), ys(4));
        ok(t.downCount() == 1, "DOWN 应 1，实得 " + t.downCount());
        ok(t.moveCount() == 2, "MOVE 应 2，实得 " + t.moveCount());
        ok(t.upCount() == 1, "UP 应 1，实得 " + t.upCount());
        ok(t.events() == 4, "总条数 4，实得 " + t.events());
        ok(t.stillDown() == 0, "UP 后按下集合应空，实得 " + t.stillDown());
    }

    private static void t05MaxPointers() {
        RecvTrace t = new RecvTrace();
        t.record(1, 1, Gesture.ACTION_DOWN, 1, ids(0), xs(1), ys(1));
        t.record(2, 2, Gesture.ACTION_POINTER_DOWN, 2, ids(0, 1), xs(1, 2), ys(1, 2));
        t.record(3, 3, Gesture.ACTION_POINTER_DOWN, 3, ids(0, 1, 2), xs(1, 2, 3), ys(1, 2, 3));
        t.record(4, 4, Gesture.ACTION_MOVE, 3, ids(0, 1, 2), xs(1, 2, 3), ys(1, 2, 3));
        ok(t.maxPointers() == 3, "最大指针数应 3，实得 " + t.maxPointers());
    }

    private static void t06DelayNotStoredWhenNegative() {
        RecvTrace t = new RecvTrace();
        t.record(1000, 1500, Gesture.ACTION_DOWN, 1, ids(0), xs(1), ys(1)); // et 在未来 ⇒ 负延迟
        ok(t.dlySamples() == 0, "负延迟不该进样本，实得 " + t.dlySamples());
        String[] l = t.csv().split("\n");
        ok("-500".equals(cols(l[1])[7]), "负延迟仍要照记明细，实得 " + cols(l[1])[7]);
    }

    private static void t07OrphanUp() {
        RecvTrace t = new RecvTrace();
        t.record(1, 1, Gesture.ACTION_DOWN, 1, ids(0), xs(1), ys(1));
        t.record(2, 2, Gesture.ACTION_UP, 1, ids(7), xs(1), ys(1));  // 7 从没按下
        ok(t.orphanUp() == 1, "孤儿抬指应 1，实得 " + t.orphanUp());
    }

    private static void t08DownWhileHeld() {
        RecvTrace t = new RecvTrace();
        t.record(1, 1, Gesture.ACTION_DOWN, 1, ids(0), xs(1), ys(1));
        t.record(2, 2, Gesture.ACTION_DOWN, 1, ids(1), xs(2), ys(2)); // 已有指按着
        ok(t.dupDown() == 1, "按下中又来 DOWN 应记 1，实得 " + t.dupDown());
    }

    private static void t09TruncationKeepsStats() {
        RecvTrace t = new RecvTrace();
        int total = RecvTrace.CAP + 7;
        for (int i = 0; i < total; i++) {
            t.record(i, i, Gesture.ACTION_MOVE, 1, ids(0), xs(i), ys(i));
        }
        ok(t.events() == total, "总条数应含被截断的，" + total + " 实得 " + t.events());
        ok(t.truncated() == 7, "截断明细应 7，实得 " + t.truncated());
        ok(t.moveCount() == total, "统计不许被截断短路：MOVE 应 " + total + "，实得 " + t.moveCount());
        ok(t.written() == RecvTrace.CAP, "明细应写 " + RecvTrace.CAP + "，实得 " + t.written());
        ok(t.csv().contains("# truncated 7"), "CSV 尾部必须有截断说明");
    }

    private static void t10PointerUpShrinksSet() {
        RecvTrace t = new RecvTrace();
        int pu = Gesture.ACTION_POINTER_UP | (1 << Gesture.ACTION_POINTER_INDEX_SHIFT);
        t.record(1, 1, Gesture.ACTION_DOWN, 1, ids(0), xs(1), ys(1));
        t.record(2, 2, Gesture.ACTION_POINTER_DOWN, 2, ids(0, 1), xs(1, 2), ys(1, 2));
        t.record(3, 3, pu, 2, ids(0, 1), xs(1, 2), ys(1, 2));   // 抬起 idx=1（id 1）
        ok(t.stillDown() == 1, "抬一指后应剩 1 指，实得 " + t.stillDown());
        ok(t.orphanUp() == 0, "合法抬指不该记孤儿，实得 " + t.orphanUp());
    }

    /**
     * ★ 这条来自 10-24 的真教训：Python 分析器的「抬指后不收缩按下集合」变异
     *   第一轮**没被杀**（用例在抬指后立刻 UP，看不出差别）。收货侧是同一类状态机，
     *   所以这里必须显式验「抬指后剩下的那根指仍被跟踪」——否则 PUP 之后来的
     *   MOVE/UP 会被误报成孤儿，把「流合法」冤枉成「流有问题」。
     */
    private static void t11RemainingPointerTrackedAfterPointerUp() {
        RecvTrace t = new RecvTrace();
        int pu = Gesture.ACTION_POINTER_UP | (1 << Gesture.ACTION_POINTER_INDEX_SHIFT);
        t.record(1, 1, Gesture.ACTION_DOWN, 1, ids(0), xs(1), ys(1));
        t.record(2, 2, Gesture.ACTION_POINTER_DOWN, 2, ids(0, 1), xs(1, 2), ys(1, 2));
        t.record(3, 3, pu, 2, ids(0, 1), xs(1, 2), ys(1, 2));      // 抬 id 1，剩 id 0
        t.record(4, 4, Gesture.ACTION_MOVE, 1, ids(0), xs(5), ys(5));
        t.record(5, 5, Gesture.ACTION_UP, 1, ids(0), xs(5), ys(5));
        ok(t.orphanMove() == 0, "抬指后剩下的 MOVE 不该是孤儿，实得 " + t.orphanMove());
        ok(t.orphanUp() == 0, "抬指后剩下的 UP 不该是孤儿，实得 " + t.orphanUp());
        ok(t.stillDown() == 0, "最终应全抬起，实得 " + t.stillDown());
    }

    private static void t12CancelClearsSet() {
        RecvTrace t = new RecvTrace();
        t.record(1, 1, Gesture.ACTION_DOWN, 1, ids(0), xs(1), ys(1));
        t.record(2, 2, Gesture.ACTION_POINTER_DOWN, 2, ids(0, 1), xs(1, 2), ys(1, 2));
        t.record(3, 3, Gesture.ACTION_CANCEL, 2, ids(0, 1), xs(1, 2), ys(1, 2));
        ok(t.cancelCount() == 1, "CANCEL 应 1，实得 " + t.cancelCount());
        ok(t.stillDown() == 0, "CANCEL 必须清空按下集合，实得 " + t.stillDown());
    }

    private static void t13EmptyTrace() {
        RecvTrace t = new RecvTrace();
        String[] l = t.csv().split("\n");
        ok(l.length == 1 && RecvTrace.HEADER.equals(l[0]), "空 trace 只该有表头，实得 " + l.length + " 行");
        ok(t.events() == 0, "空 trace 事件数应 0");
        ok(t.brief().contains("收到 0 条"), "brief 应明说 0 条：" + t.brief());
    }

    /** 坐标必须四舍五入（与发送侧 int 对齐），截断会在 0.5 上系统性偏 1 px。 */
    private static void t14CoordRounding() {
        RecvTrace t = new RecvTrace();
        t.record(1, 1, Gesture.ACTION_DOWN, 1, ids(0),
                new float[]{100.6f}, new float[]{200.4f});
        String[] l = t.csv().split("\n");
        ok("0:101:200".equals(cols(l[1])[6]), "四舍五入应得 101:200，实得 " + cols(l[1])[6]);
    }

    // ─────────────────────────── 工具 ───────────────────────────

    private static int[] ids(int... v) { return v; }

    private static float[] xs(float... v) { return v; }

    private static float[] ys(float... v) { return v; }

    /** ★ 必须用 split(",", -1)：默认 split 会吃掉行尾的空字段，"缺列"就查不出来了。 */
    private static String[] cols(String line) { return line.split(",", -1); }

    private static void ok(boolean cond, String msg) {
        if (cond) {
            pass++;
        } else {
            fail++;
            System.out.println("  ✗ " + msg);
        }
    }
}
