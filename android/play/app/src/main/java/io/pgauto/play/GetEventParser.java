package io.pgauto.play;

/**
 * 把 `getevent` 的**逐行输出**解析成触摸事件序列（DOWN / UP / MOVE）。
 *
 * <h3>为什么单独成类、且必须纯 Java</h3>
 * 解析是"用户点了哪里"这条链上唯一有格式细节的一环，也是最容易悄悄错的地方
 * （十六进制、SYN_REPORT 聚合、BTN_TOUCH 与 TRACKING_ID 两种"按下"标志）。
 * 抽成纯类才能在 `selftest.sh`（**没有 android.jar**）里离线断言；
 * 把它塞进 `TouchSniff` 里就只能靠真机撞。
 *
 * <h3>为什么必须等 SYN_REPORT</h3>
 * 一次触摸会被内核拆成多条 `EV_ABS`（X 一条、Y 一条、TRACKING_ID 一条），
 * 直到 `SYN_REPORT` 才表示"这一帧数据凑齐了"。
 * **在 X 到达时就当坐标用 ⇒ 拿到的是"新 X + 上一帧 Y"**，落点会诡异偏移。
 *
 * <h3>两种"按下"标志都认</h3>
 * <ul>
 *   <li>`BTN_TOUCH`（EV_KEY 0x14a）1/0：老式与多数触摸屏会给；</li>
 *   <li>`ABS_MT_TRACKING_ID`（EV_ABS 0x39）：&gt;=0 表示有手指，`0xffffffff` 表示抬起。</li>
 * </ul>
 * 只认其中一种，在"另一种没上报"的设备上就永远学不到落点。
 *
 * <h3>输入格式</h3>
 * 支持三种（都在本机实测过格式）：
 * <pre>
 *   0003 0035 00000261                                     ← getevent &lt;设备&gt;（★ 我们在用的这种，无前缀）
 *   /dev/input/event5: 0003 0035 00000261                  ← 无参数 getevent（带设备前缀）
 *   [   12345.678901] /dev/input/event5: EV_ABS ABS_MT_POSITION_X 00000261   ← -l 带名字
 * </pre>
 * 其它行（`add device`、空行、别的设备）一律返回 null，不抛异常 ——
 * 流里混着别的东西是常态，解析器不该因此中断。
 */
public final class GetEventParser {

    private static final int EV_SYN = 0x00;
    private static final int EV_KEY = 0x01;
    private static final int EV_ABS = 0x03;

    private static final int ABS_MT_POSITION_X = 0x35;
    private static final int ABS_MT_POSITION_Y = 0x36;
    private static final int ABS_MT_TRACKING_ID = 0x39;
    private static final int BTN_TOUCH = 0x14a;

    private static final int SYN_REPORT = 0x00;

    // ---- 状态 ----
    private int curX = -1;
    private int curY = -1;
    private boolean btn;
    private int tracking = -1;
    private boolean wasDown;
    private boolean moved;

    /** 清空状态（每次开新监听窗口前调用，避免上一轮的残留状态污染）。 */
    public void reset() {
        curX = -1;
        curY = -1;
        btn = false;
        tracking = -1;
        wasDown = false;
        moved = false;
    }

    /** 当前是否处于"按下"状态（诊断用）。 */
    public boolean isDown() {
        return wasDown;
    }

    /**
     * 喂一行。
     *
     * @param line       一行原始输出（可含行尾 \r）
     * @param devicePath 只要这个设备的事件；传 null 表示不过滤
     * @return `{act, rawX, rawY}`（act 见 {@link FirstDown#ACT_DOWN} 等）或 null
     */
    public int[] feed(String line, String devicePath) {
        if (line == null) return null;
        String s = line.trim();
        if (s.isEmpty()) return null;

        // ★★ 两种输出格式（2026-10-31 真机实测，这是第一次真机把 rawLines 打到 15 而 events=0 的原因）：
        //   ① `getevent`（不带设备）      → `/dev/input/event5: 0003 003a 000001fc`   ← 带前缀
        //   ② `getevent /dev/input/event5` → `0003 003a 000001fc`                    ← **不带前缀**
        //   我们用的是 ②（只订阅一块屏，省掉无关设备），所以**不能要求前缀**。
        //   ⛔ 之前那版硬要 ": " 前缀 ⇒ 每一行都在这里被丢掉 ⇒ 用户点得再准也是"没等到点击"。
        int sep = s.indexOf(": ");
        String payload;
        String dev;
        if (sep < 0) {
            payload = s;
            // 没前缀时归属只能靠调用方：既然只订阅了一个设备，它就是那个设备的。
            // devicePath == null（不知道订阅了谁）⇒ 丢弃，不许瞎认。
            dev = devicePath;
        } else {
            payload = s.substring(sep + 2).trim();
            dev = deviceOf(s.substring(0, sep));
        }
        if (dev == null) return null;
        if (devicePath != null && !devicePath.equals(dev)) return null;

        int[] ev = parseEvent(payload);
        if (ev == null) return null;
        int type = ev[0];
        int code = ev[1];
        int value = ev[2];

        if (type == EV_ABS) {
            if (code == ABS_MT_POSITION_X) {
                curX = value;
                moved = true;
            } else if (code == ABS_MT_POSITION_Y) {
                curY = value;
                moved = true;
            } else if (code == ABS_MT_TRACKING_ID) {
                tracking = value;      // -1 = 抬起
            }
            return null;
        }
        if (type == EV_KEY && code == BTN_TOUCH) {
            btn = value != 0;
            return null;
        }
        if (type == EV_SYN && code == SYN_REPORT) {
            return report();
        }
        return null;
    }

    /** 一帧凑齐（SYN_REPORT）时才决定有没有事件。 */
    private int[] report() {
        boolean isDown = btn || tracking >= 0;
        if (isDown && !wasDown) {
            // ★★ 2026-10-31 自审发现的真 bug：原来先 `wasDown = true` 再判坐标，
            //   于是"BTN_TOUCH/TRACKING_ID 先到、X/Y 晚一帧"的驱动上，
            //   这一帧返回 null 但状态已变成"按着"，下一帧走 MOVE 分支
            //   ⇒ **DOWN 永远不会发出**，而 FirstDown 只认 DOWN ⇒ 这一下彻底丢了。
            //   本机 fortunate（坐标先到）掩盖了它；换个 MT 协议顺序的机型就必现。
            //   现在：坐标没到就**先不改状态**，等坐标到的那一帧再发 DOWN。
            if (curX < 0 || curY < 0) return null;
            wasDown = true;
            moved = false;
            return new int[]{FirstDown.ACT_DOWN, curX, curY};
        }
        if (!isDown && wasDown) {
            wasDown = false;
            moved = false;
            if (curX < 0 || curY < 0) return null;
            return new int[]{FirstDown.ACT_UP, curX, curY};
        }
        if (isDown && moved) {
            moved = false;
            if (curX < 0 || curY < 0) return null;
            return new int[]{FirstDown.ACT_MOVE, curX, curY};
        }
        return null;
    }

    /** 从 `/dev/input/event5` 或 `[ 1.2] /dev/input/event5` 里取设备路径；取不到返回 null。 */
    private static String deviceOf(String head) {
        String[] tok = head.trim().split("\\s+");
        for (int i = tok.length - 1; i >= 0; i--) {
            if (tok[i].startsWith("/dev/input/")) return tok[i];
        }
        return null;
    }

    /** 解析 `0003 0035 00000261` 或 `EV_ABS ABS_MT_POSITION_X 00000261`。 */
    private static int[] parseEvent(String payload) {
        String[] tok = payload.split("\\s+");
        if (tok.length < 3) return null;
        try {
            int type;
            int code;
            if (isHex(tok[0])) {
                type = (int) Long.parseLong(tok[0], 16);
                code = (int) Long.parseLong(tok[1], 16);
            } else {
                type = typeOfName(tok[0]);
                code = codeOfName(tok[1]);
                if (type < 0 || code < 0) return null;
            }
            int value = (int) Long.parseLong(tok[2], 16);
            return new int[]{type, code, value};
        } catch (NumberFormatException e) {
            return null;      // 流里混进怪行不该中断监听
        }
    }

    private static boolean isHex(String s) {
        if (s.isEmpty()) return false;
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            boolean ok = (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') || (c >= 'A' && c <= 'F');
            if (!ok) return false;
        }
        return true;
    }

    private static int typeOfName(String n) {
        if ("EV_SYN".equals(n)) return EV_SYN;
        if ("EV_KEY".equals(n)) return EV_KEY;
        if ("EV_ABS".equals(n)) return EV_ABS;
        return -1;
    }

    private static int codeOfName(String n) {
        if ("SYN_REPORT".equals(n)) return SYN_REPORT;
        if ("BTN_TOUCH".equals(n)) return BTN_TOUCH;
        if ("ABS_MT_POSITION_X".equals(n)) return ABS_MT_POSITION_X;
        if ("ABS_MT_POSITION_Y".equals(n)) return ABS_MT_POSITION_Y;
        if ("ABS_MT_TRACKING_ID".equals(n)) return ABS_MT_TRACKING_ID;
        return -1;
    }
}
