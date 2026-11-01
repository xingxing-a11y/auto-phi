package io.pgauto.inject.sched;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.List;

/**
 * {@link Scheduler#build} 三道闸门的自测（纯 JVM，无 Android 依赖）。
 *
 * ★ 为什么值得写：这三道闸门存在的唯一理由是**拦住一局坐标全错的演奏**。
 *   而"闸门太松"不会报错（照打不误），"闸门太紧"更糟（把合法曲目判死）。
 *   10-24 的方案初稿里就有一条「canvas 必须 ≠ screen」—— 它会把
 *   `touches_schneid.json` / `touches_yume.json`（canvas==screen==1920x1080，
 *   合法的恒等变换）直接判死。所以这里必须有**双向**用例：该红的红、该绿的绿。
 */
public final class SchedulerGateSelfTest {

    private static int pass, fail;
    private static Path dir;

    public static void main(String[] args) throws Exception {
        dir = Files.createTempDirectory("schedgate");
        t01_normalPayloadPasses();
        t02_canvasWrittenAsScreenIsRejected();
        t03_canvasEqualsScreenIsLegal();
        t04_nonSixteenNineIsRejected();
        t05_eventOutOfScreenIsRejected();
        t06_deviceCoordOutOfRangeIsRejected();
        t07_metaIsOptional();
        t08_legacyOverloadsStillWork();
        t09_zeroCanvasIsRejected();
        t10_canvasGateRunsBeforeEventGate();

        System.out.println("SchedulerGateSelfTest: " + pass + " 通过, " + fail + " 失败");
        if (fail > 0) throw new AssertionError("Scheduler 闸门自测失败 " + fail + " 项");
    }

    // ─────────────────────────── 闸门 A：canvas 16:9 ───────────────────────────

    private static void t01_normalPayloadPasses() throws Exception {
        Scheduler.Meta m = new Scheduler.Meta();
        List<Scheduler.Cmd> cmds = Scheduler.build(
                write("ok.json", payload(1920, 1080, 1080, 2340,
                        ev(3000, "DOWN", 0, 570, 1050) + "," + ev(3100, "UP", 0, 570, 1050))),
                1610, 720, true, false, null, m);
        ok(cmds.size() == 2, "正常载荷应产出 2 条，实得 " + cmds.size());
        ok(m.canvasW == 1920 && m.canvasH == 1080, "meta.canvas 应回填 1920x1080，实得 "
                + m.canvasW + "x" + m.canvasH);
        ok(m.srcW == 1080 && m.srcH == 2340, "meta.screen 应回填 1080x2340");
        ok(m.nEvents == 2 && m.nCmds == 2 && m.nKey == 2,
                "meta 计数：events=2 cmds=2 key=2，实得 " + m);
        ok(m.toString().contains("canvas=1920x1080"), "meta 日志必须含 canvas 口径，实得 " + m);
    }

    /** ★ 本次事故的形状：canvas 被写成 screen（1080x2340）。 */
    private static void t02_canvasWrittenAsScreenIsRejected() throws Exception {
        String msg = buildError(payload(1080, 2340, 1080, 2340, ev(1, "DOWN", 0, 540, 1170)));
        ok(msg != null, "canvas 被写成 screen ⇒ 必须拒绝开火");
        ok(msg != null && msg.contains("16:9"), "文案必须点出 16:9，实得 " + msg);
        ok(msg != null && msg.contains("1920"), "文案必须给出正确口径的提示，实得 " + msg);
    }

    /** ★ 防误伤：canvas == screen == 1920x1080 是合法的恒等变换。 */
    private static void t03_canvasEqualsScreenIsLegal() throws Exception {
        String msg = buildError(payload(1920, 1080, 1920, 1080, ev(1, "DOWN", 0, 960, 540)));
        ok(msg == null, "canvas == screen == 1920x1080 属合法退化，不许判死，实得 " + msg);
    }

    private static void t04_nonSixteenNineIsRejected() throws Exception {
        ok(buildError(payload(1920, 1200, 1080, 2340, ev(1, "DOWN", 0, 540, 1170))) != null,
                "1920x1200（16:10）必须被拒");
        ok(buildError(payload(1280, 720, 1080, 2340, ev(1, "DOWN", 0, 540, 1170))) == null,
                "1280x720 也是 16:9，必须放行");
    }

    private static void t09_zeroCanvasIsRejected() throws Exception {
        String msg = buildError(payload(0, 0, 1080, 2340, ev(1, "DOWN", 0, 540, 1170)));
        ok(msg != null && msg.contains("非法"), "canvas 尺寸 0 ⇒ 拒绝且文案含「非法」，实得 " + msg);
    }

    // ─────────────────────────── 闸门 B/C：坐标 ───────────────────────────

    private static void t05_eventOutOfScreenIsRejected() throws Exception {
        String msg = buildError(payload(1920, 1080, 1080, 2340, ev(1, "DOWN", 0, 2000, 1170)));
        ok(msg != null, "事件 x=2000 超出 screen 宽 1080 ⇒ 必须拒绝");
        ok(msg != null && msg.contains("越界"), "文案必须含「越界」，实得 " + msg);
        // 闭区间：正好落在右边界不算越界（上游 clamp 会给出 1080）。
        // ★ 注意只取 x 的边界、y 取中部 —— screen 是 1080x2340 竖屏，它的**上下边缘**
        //   换算到 canvas 是 y≈2620 / -866，本来就落在游戏画布之外，闸门 C 会（正确地）拦下，
        //   那是另一回事，不能拿来当"边界合法"的夹具。第一版就是这么写错的。
        ok(buildError(payload(1920, 1080, 1080, 2340, ev(1, "DOWN", 0, 1080, 1170))) == null,
                "坐标正好等于 screen 右边界 1080 必须放行（clamp 的正常输出）");
    }

    private static void t06_deviceCoordOutOfRangeIsRejected() throws Exception {
        // dst 100x100：16:9 canvas 在 fit=height 下 xOff=(100-1920·100/1080)/2 = -38.9
        // ⇒ 右侧点的设备 x 会 > 100。这与"载荷口径"无关，是变换参数不对。
        String msg = buildError(payload(1920, 1080, 1080, 2340, ev(1, "DOWN", 0, 1050, 1170)),
                100, 100);
        ok(msg != null, "设备 100x100 装不下 16:9 画布 ⇒ 必须拒绝");
        ok(msg != null && msg.contains("排程坐标越界"), "文案必须点明是排程坐标，实得 " + msg);
    }

    private static void t10_canvasGateRunsBeforeEventGate() throws Exception {
        // 同时违反两条时，必须先报 canvas —— 否则人会被"事件越界"引向生成端，
        // 而真因是 canvas 口径（10-24 就是这么被带偏的）。
        String msg = buildError(payload(1080, 2340, 1080, 2340, ev(1, "DOWN", 0, 2000, 1170)));
        ok(msg != null && msg.contains("16:9"), "双违规时必须先报 canvas 口径，实得 " + msg);
    }

    // ─────────────────────────── 重载与兼容 ───────────────────────────

    private static void t07_metaIsOptional() throws Exception {
        Path p = write("meta_null.json", payload(1920, 1080, 1080, 2340, ev(1, "DOWN", 0, 570, 1050)));
        ok(Scheduler.build(p, 1610, 720, true, false, null, null).size() == 1,
                "meta=null 必须照常工作");
    }

    private static void t08_legacyOverloadsStillWork() throws Exception {
        Path p = write("legacy.json", payload(1920, 1080, 1080, 2340, ev(1, "DOWN", 0, 570, 1050)));
        ok(Scheduler.build(p, 1610, 720, true).size() == 1, "三参重载必须照常工作");
        ok(Scheduler.build(p, 1610, 720, true, false, null).size() == 1, "六参重载必须照常工作");
    }

    // ─────────────────────────── 工具 ───────────────────────────

    private static String buildError(String json) {
        return buildError(json, 1610, 720);
    }

    /** 返回异常消息；没抛就是 null。 */
    private static String buildError(String json, double dstW, double dstH) {
        try {
            Path p = write("t_" + (++n) + ".json", json);
            Scheduler.build(p, dstW, dstH, true, false, null, new Scheduler.Meta());
            return null;
        } catch (IllegalStateException e) {
            return e.getMessage();
        } catch (IOException e) {
            return "IO: " + e.getMessage();
        }
    }

    private static int n;

    private static String payload(double cw, double ch, double sw, double sh, String events) {
        return "{\"canvas\":{\"width\":" + cw + ",\"height\":" + ch + "},"
                + "\"screen\":{\"width\":" + sw + ",\"height\":" + sh + "},"
                + "\"events\":[" + events + "]}";
    }

    private static String ev(double t, String act, int pid, double x, double y) {
        return "{\"t_ms\":" + t + ",\"action\":\"" + act + "\",\"pointer_id\":" + pid
                + ",\"x\":" + x + ",\"y\":" + y + "}";
    }

    private static Path write(String name, String json) throws IOException {
        Path p = Paths.get(dir.toString(), name);
        Files.write(p, json.getBytes(StandardCharsets.UTF_8));
        return p;
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
