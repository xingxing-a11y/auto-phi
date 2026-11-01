package io.pgauto.play;

import java.util.List;

/**
 * {@link TouchDevices} 的离线自测。
 *
 * ★ 样本是**真机原文**（`out/getevent_p_2026-10-31.txt`，HONOR ALT-AN00，8 个设备里的 3 个），
 *   不是我自己编的格式 —— 编格式等于拿假设测假设。为避免判据依赖磁盘 I/O，原文以字符串常量内嵌。
 *
 * 钉住：① 能在杂项设备里挑出触摸屏；② 量程取的是 0x35/0x36（MT），不是 0x00/0x01（单点）；
 *      ③ 0 个/多个候选都必须抛，不许"取第一个"。
 */
public final class TouchDevicesSelfTest {

    private static int pass = 0;
    private static int fail = 0;

    /** 真机原文（节选，未改动任何字符）。 */
    private static final String REAL = ""
            + "add device 1: /dev/input/event7\n"
            + "  name:     \"ravelin-qrd-snd-card Button Jack\"\n"
            + "  events:\n"
            + "    KEY (0001): 0072  0073  00e2  0104  0105  0246 \n"
            + "  input props:\n"
            + "    <none>\n"
            + "add device 3: /dev/input/event5\n"
            + "  name:     \"honor,ts_kit\"\n"
            + "  events:\n"
            + "    KEY (0001): 003b  0042  0043  0044  0057  0058  00c4  00fb \n"
            + "                0145  014a  0264 \n"
            + "    ABS (0003): 0000  : value 0, min 0, max 719, fuzz 0, flat 0, resolution 0\n"
            + "                0001  : value 0, min 0, max 1609, fuzz 0, flat 0, resolution 0\n"
            + "                0018  : value 0, min 0, max 255, fuzz 0, flat 0, resolution 0\n"
            + "                0030  : value 0, min 0, max 255, fuzz 0, flat 0, resolution 0\n"
            + "                0031  : value 0, min 0, max 255, fuzz 0, flat 0, resolution 0\n"
            + "                0035  : value 0, min 0, max 719, fuzz 0, flat 0, resolution 0\n"
            + "                0036  : value 0, min 0, max 1609, fuzz 0, flat 0, resolution 0\n"
            + "                0039  : value 0, min 0, max 10, fuzz 0, flat 0, resolution 0\n"
            + "                003a  : value 0, min 0, max 255, fuzz 0, flat 0, resolution 0\n"
            + "  input props:\n"
            + "    INPUT_PROP_DIRECT\n"
            + "add device 6: /dev/input/event2\n"
            + "  name:     \"fingersense\"\n"
            + "  events:\n"
            + "    KEY (0001): 003b  0042  0043  0044  0057  0058  00c4  00fb \n"
            + "  input props:\n"
            + "    <none>\n";

    private static void ok(boolean cond, String msg) {
        if (cond) {
            pass++;
        } else {
            fail++;
            System.out.println("  ✗ " + msg);
        }
    }

    public static void main(String[] args) {
        t01_parsesThreeDevices();
        t02_picksTouchscreen();
        t03_rangesComeFromMtNotSingleTouch();
        t04_noTouchscreenThrows();
        t05_ambiguousThrows();
        t06_directPreferredOverTouchpad();
        t07_emptyIsSafe();

        System.out.println("TouchDevicesSelfTest: " + pass + " 通过, " + fail + " 失败");
        if (fail > 0) throw new AssertionError("TouchDevices 自测失败 " + fail + " 项");
    }

    private static void t01_parsesThreeDevices() {
        List<TouchDevices.Dev> all = TouchDevices.parse(REAL);
        ok(all.size() == 3, "t01 应解析出 3 个设备，实得 " + all.size());
        boolean hasEvent5 = false;
        for (TouchDevices.Dev d : all) {
            if ("/dev/input/event5".equals(d.path)) hasEvent5 = true;
        }
        ok(hasEvent5, "t01 应含 /dev/input/event5");
    }

    private static void t02_picksTouchscreen() {
        TouchDevices.Dev d = TouchDevices.pick(REAL);
        ok("/dev/input/event5".equals(d.path), "t02 应挑中 event5，实得 " + d.path);
        ok("honor,ts_kit".equals(d.name), "t02 设备名应是 honor,ts_kit，实得 " + d.name);
        ok(d.direct, "t02 应带 INPUT_PROP_DIRECT");
    }

    /**
     * ★ 量程必须取 MT 轴（0x35/0x36）。
     * 本机 0x00/0x01 的 max 也是 719/1609，两者数值相同 ⇒ **这条断言单独看没有分辨力**，
     * 所以还要断言"去找一个只有单点轴、没有 MT 轴的设备时必须挑不中"（t04 里用改造样本覆盖）。
     */
    private static void t03_rangesComeFromMtNotSingleTouch() {
        TouchDevices.Dev d = TouchDevices.pick(REAL);
        ok(d.maxX == 719, "t03 maxX 应 719，实得 " + d.maxX);
        ok(d.maxY == 1609, "t03 maxY 应 1609，实得 " + d.maxY);
    }

    /** 只有单点轴、没有 MT 轴的设备不能当触摸屏（否则会用错量程）。 */
    private static void t04_noTouchscreenThrows() {
        String singleTouchOnly = ""
                + "add device 1: /dev/input/event9\n"
                + "  name:     \"single-only\"\n"
                + "  events:\n"
                + "    ABS (0003): 0000  : value 0, min 0, max 719, fuzz 0, flat 0, resolution 0\n"
                + "                0001  : value 0, min 0, max 1609, fuzz 0, flat 0, resolution 0\n"
                + "  input props:\n"
                + "    INPUT_PROP_DIRECT\n";
        ok(throwsISE(() -> TouchDevices.pick(singleTouchOnly)),
                "t04 只有单点轴（无 0x35/0x36）必须抛，不能当成触摸屏");
        ok(throwsISE(() -> TouchDevices.pick("")), "t04 空文本必须抛");
    }

    /** 两个 MT 设备 ⇒ 必须抛并列出候选（不许取第一个）。 */
    private static void t05_ambiguousThrows() {
        String twice = REAL + ""
                + "add device 9: /dev/input/event9\n"
                + "  name:     \"second-touch\"\n"
                + "  events:\n"
                + "    ABS (0003): 0035  : value 0, min 0, max 1079, fuzz 0, flat 0, resolution 0\n"
                + "                0036  : value 0, min 0, max 2399, fuzz 0, flat 0, resolution 0\n"
                + "  input props:\n"
                + "    INPUT_PROP_DIRECT\n";
        boolean threw;
        try {
            TouchDevices.pick(twice);
            threw = false;
        } catch (IllegalStateException e) {
            threw = true;
            ok(e.getMessage().contains("不唯一"), "t05 报错文本应说明不唯一，实得：" + e.getMessage());
            ok(e.getMessage().contains("event5") && e.getMessage().contains("event9"),
                    "t05 报错文本应列出两个候选");
        }
        ok(threw, "t05 两个 MT 设备必须抛");
    }

    /** 有 DIRECT 的优先于触摸板（无 DIRECT 的同量程设备）。 */
    private static void t06_directPreferredOverTouchpad() {
        String pad = REAL + ""
                + "add device 9: /dev/input/event9\n"
                + "  name:     \"touchpad\"\n"
                + "  events:\n"
                + "    ABS (0003): 0035  : value 0, min 0, max 719, fuzz 0, flat 0, resolution 0\n"
                + "                0036  : value 0, min 0, max 1609, fuzz 0, flat 0, resolution 0\n"
                + "  input props:\n"
                + "    <none>\n";
        TouchDevices.Dev d = TouchDevices.pick(pad);
        ok(d.direct, "t06 应优先选 DIRECT 设备，实得 " + d);
        ok("/dev/input/event5".equals(d.path), "t06 应仍选 event5，实得 " + d.path);
    }

    private static void t07_emptyIsSafe() {
        ok(TouchDevices.parse(null).isEmpty(), "t07 parse(null) 应返回空列表");
        ok(TouchDevices.parse("").isEmpty(), "t07 parse(\"\") 应返回空列表");
    }

    private static boolean throwsISE(Runnable r) {
        try {
            r.run();
            return false;
        } catch (IllegalStateException e) {
            return true;
        }
    }
}
