package io.pgauto.play;

/**
 * {@link GetEventParser} 的离线自测。
 *
 * 行格式取自 ABI 文档与本机 `getevent` 的实际输出形状（数值型与 `-l` 命名型都覆盖）。
 * 这里钉的四条都是"错了不会报错、只会静默给出错坐标"的地方：
 *   ① **必须在 SYN_REPORT 才出事件**（X 一到就出 ⇒ 拿到"新 X + 旧 Y"）；
 *   ② 十六进制解析（`000005f1` = 1521，不是 5f1=1521 的十进制误读）；
 *   ③ `BTN_TOUCH` 与 `TRACKING_ID` 两种按下标志都要认；
 *   ④ 别的设备/垃圾行不能中断监听。
 */
public final class GetEventParserSelfTest {

    private static int pass = 0;
    private static int fail = 0;

    private static final String DEV = "/dev/input/event5";

    private static void ok(boolean cond, String msg) {
        if (cond) {
            pass++;
        } else {
            fail++;
            System.out.println("  ✗ " + msg);
        }
    }

    public static void main(String[] args) {
        t01_downOnlyAfterSyn();
        t02_hexCoordinates();
        t03_incompleteFrameNoEvent();
        t04_trackingIdAsDown();
        t05_upOnTrackingMinusOne();
        t06_moveWhileDown();
        t07_otherDeviceIgnored();
        t08_garbageSafe();
        t09_namedFormat();
        t10_resetStartsFresh();
        t11_addDeviceLineIgnored();
        t12_singleDeviceNoPrefix();
        t13_noPrefixWithoutDeviceDropped();
        t14_downFlagBeforeCoordsNotLost();
        t15_firstEventOfGestureIsNeverMove();

        System.out.println("GetEventParserSelfTest: " + pass + " 通过, " + fail + " 失败");
        if (fail > 0) throw new AssertionError("GetEventParser 自测失败 " + fail + " 项");
    }

    private static String line(String payload) {
        return DEV + ": " + payload;
    }

    /** ★ 核心：X/Y/BTN 三行之后还没 SYN ⇒ 绝不能出事件。 */
    private static void t01_downOnlyAfterSyn() {
        GetEventParser p = new GetEventParser();
        ok(p.feed(line("0003 0035 00000261"), DEV) == null, "t01 X 单独到达时不应出事件");
        ok(p.feed(line("0003 0036 000005f1"), DEV) == null, "t01 Y 单独到达时不应出事件");
        ok(p.feed(line("0001 014a 00000001"), DEV) == null, "t01 BTN_TOUCH 单独到达时不应出事件");
        int[] e = p.feed(line("0000 0000 00000000"), DEV);
        ok(e != null, "t01 SYN_REPORT 后必须出 DOWN");
        if (e != null) {
            ok(e[0] == FirstDown.ACT_DOWN, "t01 动作应是 DOWN，实得 " + e[0]);
            ok(e[1] == 0x261, "t01 rawX 应 0x261=609，实得 " + e[1]);
            ok(e[2] == 0x5f1, "t01 rawY 应 0x5f1=1521，实得 " + e[2]);
        }
    }

    /** 十六进制 ⇒ 若误按十进制解析，000005f1 会变成 5f1→非法或 1521 的巧合，必须靠数值断言区分。 */
    private static void t02_hexCoordinates() {
        GetEventParser p = new GetEventParser();
        p.feed(line("0003 0035 0000000a"), DEV);      // 0x0a = 10
        p.feed(line("0003 0036 00000100"), DEV);      // 0x100 = 256
        p.feed(line("0001 014a 00000001"), DEV);
        int[] e = p.feed(line("0000 0000 00000000"), DEV);
        ok(e != null, "t02 应出 DOWN");
        if (e != null) {
            ok(e[1] == 10, "t02 rawX 应 10（十六进制 0x0a），实得 " + e[1]);
            ok(e[2] == 256, "t02 rawY 应 256（十六进制 0x100），实得 " + e[2]);
        }
    }

    /** 只有 X、没有 Y ⇒ 坐标不完整，不许拿旧值凑一个点。 */
    private static void t03_incompleteFrameNoEvent() {
        GetEventParser p = new GetEventParser();
        p.feed(line("0003 0035 00000261"), DEV);
        p.feed(line("0001 014a 00000001"), DEV);
        int[] e = p.feed(line("0000 0000 00000000"), DEV);
        ok(e == null, "t03 缺 Y 时不许出事件，实得 " + str(e));
    }

    /** 只有 TRACKING_ID（没有 BTN_TOUCH）也要能判出按下。 */
    private static void t04_trackingIdAsDown() {
        GetEventParser p = new GetEventParser();
        p.feed(line("0003 0035 00000261"), DEV);
        p.feed(line("0003 0036 000005f1"), DEV);
        p.feed(line("0003 0039 00000000"), DEV);      // tracking id 0 = 有手指
        int[] e = p.feed(line("0000 0000 00000000"), DEV);
        ok(e != null && e[0] == FirstDown.ACT_DOWN, "t04 仅靠 TRACKING_ID 也必须判出 DOWN");
    }

    /** TRACKING_ID = 0xffffffff（即 -1）⇒ 抬起。 */
    private static void t05_upOnTrackingMinusOne() {
        GetEventParser p = new GetEventParser();
        p.feed(line("0003 0035 00000261"), DEV);
        p.feed(line("0003 0036 000005f1"), DEV);
        p.feed(line("0003 0039 00000005"), DEV);
        int[] d = p.feed(line("0000 0000 00000000"), DEV);
        ok(d != null && d[0] == FirstDown.ACT_DOWN, "t05 前置：应先出 DOWN");
        p.feed(line("0003 0039 ffffffff"), DEV);
        int[] u = p.feed(line("0000 0000 00000000"), DEV);
        ok(u != null && u[0] == FirstDown.ACT_UP, "t05 TRACKING_ID=-1 必须出 UP，实得 " + str(u));
    }

    private static void t06_moveWhileDown() {
        GetEventParser p = new GetEventParser();
        p.feed(line("0003 0035 00000064"), DEV);
        p.feed(line("0003 0036 000000c8"), DEV);
        p.feed(line("0001 014a 00000001"), DEV);
        ok(p.feed(line("0000 0000 00000000"), DEV) != null, "t06 前置：应出 DOWN");
        p.feed(line("0003 0035 000000c8"), DEV);
        int[] m = p.feed(line("0000 0000 00000000"), DEV);
        ok(m != null && m[0] == FirstDown.ACT_MOVE, "t06 按下期间坐标变化应出 MOVE，实得 " + str(m));
        if (m != null) ok(m[1] == 0xc8, "t06 MOVE 的 rawX 应 0xc8，实得 " + m[1]);
    }

    private static void t07_otherDeviceIgnored() {
        GetEventParser p = new GetEventParser();
        ok(p.feed("/dev/input/event0: 0003 0035 00000261", DEV) == null,
                "t07 别的设备必须被过滤掉");
        ok(!p.isDown(), "t07 过滤后不应进入按下状态");
    }

    private static void t08_garbageSafe() {
        GetEventParser p = new GetEventParser();
        ok(p.feed(null, DEV) == null, "t08 null 行应返回 null");
        ok(p.feed("", DEV) == null, "t08 空行应返回 null");
        ok(p.feed("garbage without colon", DEV) == null, "t08 无冒号行应返回 null");
        ok(p.feed(DEV + ": zz zz zz", DEV) == null, "t08 非法十六进制应返回 null 而不抛");
    }

    /** `-l` 命名格式也必须认（否则有人加了 -l 就整段静默失效）。 */
    private static void t09_namedFormat() {
        GetEventParser p = new GetEventParser();
        p.feed("[   12345.678901] " + DEV + ": EV_ABS ABS_MT_POSITION_X 00000261", DEV);
        p.feed("[   12345.678902] " + DEV + ": EV_ABS ABS_MT_POSITION_Y 000005f1", DEV);
        p.feed("[   12345.678903] " + DEV + ": EV_KEY BTN_TOUCH 00000001", DEV);
        int[] e = p.feed("[   12345.678904] " + DEV + ": EV_SYN SYN_REPORT 00000000", DEV);
        ok(e != null && e[0] == FirstDown.ACT_DOWN, "t09 命名格式也应判出 DOWN，实得 " + str(e));
        if (e != null) {
            ok(e[1] == 0x261, "t09 命名格式 rawX 应 609，实得 " + e[1]);
            ok(e[2] == 0x5f1, "t09 命名格式 rawY 应 1521，实得 " + e[2]);
        }
    }

    /** reset 后必须能重新出 DOWN（否则第二次学习时永远学不到）。 */
    private static void t10_resetStartsFresh() {
        GetEventParser p = new GetEventParser();
        p.feed(line("0003 0035 00000064"), DEV);
        p.feed(line("0003 0036 000000c8"), DEV);
        p.feed(line("0001 014a 00000001"), DEV);
        ok(p.feed(line("0000 0000 00000000"), DEV) != null, "t10 前置：应出 DOWN");
        p.reset();
        ok(!p.isDown(), "t10 reset 后不应仍是按下状态");
        p.feed(line("0003 0035 00000064"), DEV);
        p.feed(line("0003 0036 000000c8"), DEV);
        p.feed(line("0001 014a 00000001"), DEV);
        int[] e = p.feed(line("0000 0000 00000000"), DEV);
        ok(e != null && e[0] == FirstDown.ACT_DOWN, "t10 reset 后必须能再出 DOWN");
    }

    private static void t11_addDeviceLineIgnored() {
        GetEventParser p = new GetEventParser();
        ok(p.feed("add device 3: /dev/input/event5", DEV) == null,
                "t11 add device 行应返回 null（它冒号后面不是事件）");
    }

    /**
     * ★★★ 生产格式：`getevent /dev/input/event5` 的输出**不带设备前缀**。
     *
     * 2026-10-31 第一次真机实测：rawLines=15、events=0，日志第一行是
     * <pre>
     *   0003 003a 000001fc        ← 真实抓到的（EV_ABS / ABS_MT_PRESSURE / 508）
     * </pre>
     * 原因就是旧解析器硬要 `": "` 前缀 —— 而我们正是用「指定设备」的方式起 getevent 的
     * ⇒ 每一行都在入口被丢掉，用户点得再准也只报"没等到点击"。
     *
     * ⚠ 这组用例之前不存在，所以 t01~t11 全绿而真机必败：**测试用的格式和生产的格式不是一个**。
     *   凡是"解析外部输出"的类，测试必须至少有一条用**生产实际调用方式**产出的形状。
     */
    private static void t12_singleDeviceNoPrefix() {
        GetEventParser p = new GetEventParser();
        // 真实抓到的第一行（压力）—— 顺序与驱动有关，解析器靠 SYN 聚合，不该挑顺序
        ok(p.feed("0003 003a 000001fc", DEV) == null, "t12 无前缀的压力行不应出事件");
        ok(p.feed("0003 0039 00000000", DEV) == null, "t12 无前缀的 TRACKING_ID 不应出事件");
        ok(p.feed("0003 0035 00000261", DEV) == null, "t12 无前缀的 X 不应出事件");
        ok(p.feed("0003 0036 000005f1", DEV) == null, "t12 无前缀的 Y 不应出事件");
        ok(p.feed("0001 014a 00000001", DEV) == null, "t12 无前缀的 BTN_TOUCH 不应出事件");
        int[] e = p.feed("0000 0000 00000000", DEV);
        ok(e != null, "t12 无前缀格式的 SYN_REPORT 后必须出 DOWN（这是真机实际格式）");
        if (e != null) {
            ok(e[0] == FirstDown.ACT_DOWN, "t12 动作应是 DOWN，实得 " + e[0]);
            ok(e[1] == 0x261 && e[2] == 0x5f1,
                    "t12 raw 应是 (609,1521)，实得 " + str(e));
        }
        int[] up = p.feed("0003 0039 ffffffff", DEV);
        ok(up == null, "t12 抬起的 TRACKING_ID 单独到达不应出事件");
        // 真机抬起时 BTN_TOUCH 也会一起给 0；只给 TRACKING_ID=-1 而 BTN 仍为 1 时，
        // 判据 `btn || tracking>=0` 仍算按下 —— 那是对的，因为这正是"还有一根手指按着"。
        ok(p.feed("0001 014a 00000000", DEV) == null, "t12 BTN_TOUCH=0 单独到达不应出事件");
        int[] up2 = p.feed("0000 0000 00000000", DEV);
        ok(up2 != null && up2[0] == FirstDown.ACT_UP, "t12 抬起帧应出 UP，实得 " + str(up2));
    }

    /**
     * 无前缀又没告诉我是哪个设备 ⇒ **必须丢弃**，不许瞎认。
     * （否则读多设备时会把别人的事件当成触摸）
     */
    private static void t13_noPrefixWithoutDeviceDropped() {
        GetEventParser p = new GetEventParser();
        ok(p.feed("0003 0035 00000261", null) == null, "t13 无前缀 + 未知设备 ⇒ 丢弃");
        ok(p.feed("0001 014a 00000001", null) == null, "t13 同上");
        ok(p.feed("0000 0000 00000000", null) == null, "t13 同上（不许凭 SYN 就出一个点）");
    }

    /**
     * ★★★ 2026-10-31 自审发现的真 bug：「按下标志先到、X/Y 晚一帧」时 DOWN 会被吞。
     *
     * 旧实现先把 `wasDown = true` 再判坐标 ⇒ 这一帧返回 null，但状态已经"按着"，
     * 下一帧坐标到齐时走的是 MOVE 分支 ⇒ **DOWN 永远不发**。
     * 而 FirstDown 只认 DOWN ⇒ 用户那一下彻底丢失（界面只会说"没等到点击"）。
     * 本机驱动恰好是坐标先到，所以真机三轮都没暴露；换 MT 顺序不同的机型必现。
     */
    private static void t14_downFlagBeforeCoordsNotLost() {
        GetEventParser p = new GetEventParser();
        // 帧1：只有 BTN_TOUCH=1（此时还没有 X/Y）
        ok(p.feed("0001 014a 00000001", DEV) == null, "t14 BTN_TOUCH 单独到达不应出事件");
        ok(p.feed("0000 0000 00000000", DEV) == null,
                "t14 坐标还没到时不许出事件，但也**不许把状态改成已按下**");
        // 帧2：坐标到齐 + SYN ⇒ 必须补发 DOWN，坐标是这一帧的
        ok(p.feed("0003 0035 00000261", DEV) == null, "t14 X 单独到达不应出事件");
        ok(p.feed("0003 0036 000005f1", DEV) == null, "t14 Y 单独到达不应出事件");
        int[] e = p.feed("0000 0000 00000000", DEV);
        ok(e != null && e[0] == FirstDown.ACT_DOWN,
                "t14 坐标到齐后必须补发 DOWN（旧实现这里发的是 MOVE ⇒ 这一下丢失），实得 " + str(e));
        if (e != null && e[0] == FirstDown.ACT_DOWN) {
            ok(e[1] == 0x261 && e[2] == 0x5f1, "t14 DOWN 的坐标应是这一帧的 (609,1521)，实得 " + str(e));
        }
    }

    /**
     * 不变量：**一次手势的第一个事件只能是 DOWN**，绝不能是 MOVE。
     * 这条比 t14 更宽 —— 任何输入顺序都不许让"第一帧是 MOVE"。
     */
    private static void t15_firstEventOfGestureIsNeverMove() {
        String[][] orders = {
                {"0001 014a 00000001", "0003 0035 00000261", "0003 0036 000005f1"},
                {"0003 0039 00000000", "0003 0035 00000261", "0003 0036 000005f1"},
                {"0003 0035 00000261", "0003 0036 000005f1", "0001 014a 00000001"},
        };
        for (int i = 0; i < orders.length; i++) {
            GetEventParser p = new GetEventParser();
            for (String l : orders[i]) p.feed(l, DEV);
            int[] e = p.feed("0000 0000 00000000", DEV);
            ok(e != null && e[0] == FirstDown.ACT_DOWN,
                    "t15 顺序" + i + " 的第一帧事件必须是 DOWN，实得 " + str(e));
        }
    }

    private static String str(int[] e) {
        return e == null ? "null" : ("{" + e[0] + "," + e[1] + "," + e[2] + "}");
    }
}
