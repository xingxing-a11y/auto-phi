package io.pgauto.inject.sched;

// S1 端内排程的**纯逻辑**实现（零 Android 依赖，可用 javac 直接编译、PC 上跑）。
//
// ★ 2026-10-21：本文件从 tools/inject_sched/Scheduler.java **搬进端内源码树**
//   （唯一副本，不再两处维护 —— 两份迟早不一致）。端内 `DevRun` 直接调 build()。
//   PC 侧对拍仍跑 `bash tools/inject_sched/verify.sh`（它编译的就是这个路径）。
//
// 为什么是纯逻辑：S2 要把它原样搬进端内 APK；若这里就绑定 Android API，
// 就只能靠真机验证，回归成本极高。纯逻辑可用 Python 参考实现逐行对拍。
//
// 与 payload_ref.py 的关系：**两份独立实现、同一个黄金序列**。
//   payload_ref.py = 复用生产 tools/inject.ScreenMap
//   Scheduler.java = 独立复现同一套变换数学
//   任一方算错，diff 都会抓到（项目规矩：同一个量两处实现 ⇒ 交叉断言）。
//
// ★ 三个必须对齐的细节（写错就 diff 不上，且都是静默错）：
//   1. 取整用 Math.rint()（= round-half-even），**不是** Math.round()（half-up）。
//      Python 的 round() 是 half-even；用 Math.round() 会在 .5 边界上差 1 像素。
//   2. t_ms 输出 "%6f" + Locale.ROOT（默认 locale 可能用逗号做小数点）。
//   3. 排序必须**稳定**且只比较 t_ms：Java List.sort 是 TimSort（稳定），
//      与 Python sorted 一致；若改成"t_ms 相同再比别的"就会与参考实现分叉。
//
// 用法（PC 上自测；与 Python 参考实现逐行 diff）：
//   javac -encoding UTF-8 -d <out> android/injector/app/src/main/java/io/pgauto/inject/sched/Scheduler.java
//   java -cp <out> io.pgauto.inject.sched.Scheduler out/touches_demiurge.json 1610 720 height > java.tsv
//   diff <(python tools/inject_sched/payload_ref.py out/touches_demiurge.json) java.tsv
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;

public final class Scheduler {

    /** 关键事件：决定「手指何时落到判定区」，直接决定 Tap/Flick 判定时刻。 */
    public static boolean isKey(String action) {
        return switch (action) {
            case "DOWN", "POINTER_DOWN", "POINTER_UP", "UP" -> true;
            default -> false;
        };
    }

    /** 一条待注入指令（坐标已变换到真机像素）。 */
    public record Cmd(int seq, int srcIdx, double tMs, String action, int pid,
                      int devX, int devY, boolean key) { }

    /**
     * 载荷口径摘要 —— **只供日志**，不参与任何判定。
     *
     * ★ 为什么要有：10-24 那两轮事故里，日志只有「排程 N 条」，没有任何一处能看出
     *   「设备用的是哪份载荷」。有了这个，下一轮跑完直接把 `sha256 / canvas / screen`
     *   与装机 asset 一比，就能把「跑的是旧缓存」从推断变成实测。
     */
    public static final class Meta {
        public double canvasW, canvasH, srcW, srcH;
        public int nEvents, nCmds, nKey;

        @Override
        public String toString() {
            return String.format(Locale.ROOT,
                    "canvas=%.0fx%.0f screen=%.0fx%.0f events=%d cmds=%d key=%d",
                    canvasW, canvasH, srcW, srcH, nEvents, nCmds, nKey);
        }
    }

    /**
     * 交付坐标 → 画布 → 真机像素。与 tools/inject.ScreenMap.point() 逐式同序，
     * 保证浮点运算顺序一致（否则末位差异会在 .5 边界上放大成 1 像素）。
     */
    public static int[] toDevice(double x, double y,
                                 double canvasW, double canvasH,
                                 double srcW, double srcH,
                                 double dstW, double dstH, boolean fitHeight) {
        // to_canvas: k = srcW/canvasW; yOff = (srcH - canvasH*k)/2; (x/k, (y-yOff)/k)
        double srcScale = srcW / canvasW;
        double srcYOff = (srcH - canvasH * srcScale) / 2.0;
        double cx = x / srcScale;
        double cy = (y - srcYOff) / srcScale;

        // from_canvas(fit=height): k = dstH/canvasH; xOff = (dstW - canvasW*k)/2; (xOff+cx*k, cy*k)
        double dstScale = fitHeight ? (dstH / canvasH) : (dstW / canvasW);
        double xOff = fitHeight ? (dstW - canvasW * dstScale) / 2.0 : 0.0;
        double yOff = fitHeight ? 0.0 : (dstH - canvasH * dstScale) / 2.0;
        double dx = xOff + cx * dstScale;
        double dy = yOff + cy * dstScale;

        // ★ rint = round-half-even，与 Python round() 一致
        // ⚠ 这里**不做**手势区夹取：只有「按下」那一条需要夹（见 `clampOutOfGestureZone`）。
        return new int[]{(int) Math.rint(dx), (int) Math.rint(dy)};
    }

    // ★ 2026-10-30：从「绝对像素」改成「按屏幕比例」。
    //   原来写死 92 / 92 / 12 px，全是 HONOR ALT-AN00（1610×720，density 320）上的实测值；
    //   换到 3136×1440 的机器上仍按 92 夹 ⇒ 顶部/底部各有 92 px 没了保护，落在里面的
    //   **按下点**可能触发系统手势（上滑回桌面 / 下拉通知栏）。
    //
    //   ⛔ 比例必须从**当前常量**反推（92/720、12/1610），不能从实测值（74/80）反推 ——
    //      那样会丢掉 18 px / 12 px 的安全余量，等于偷偷改了 1610×720 上的行为。
    //      现在的约束是：1610×720 上算出来必须仍是 92 / 92 / 12（GestureClampSelfTest 钉住）。
    //
    //   ⛔ 不能用 dp 换算（`DisplayMetrics.densityDpi`）：本类在 selftest.sh 的
    //      「无 android.jar」清单里，必须保持纯 Java（现状 import 只有 java.*）。
    private static final double GESTURE_TOP_REL = 92.0 / 720.0;      // ≈ 0.127778
    private static final double GESTURE_BOTTOM_REL = 92.0 / 720.0;   // ≈ 0.127778
    private static final double GESTURE_SIDE_REL = 12.0 / 1610.0;    // ≈ 0.007453

    /**
     * 「屏幕太小就别夹」的**绝对**下限（沿用旧代码 `dstH > 2×92 && dstW > 2×12` 的语义）。
     *
     * ⛔ 这两个值故意**不做比例化**：它们的职责不是"手势区多大"，而是"换个太小的屏幕时
     *   别在这里夹，留给 `requireGameCanvas` 去报错"（注释原话：否则会把那道闸门测成绿）。
     *   而 `requireGameCanvas` 也是一道绝对闸门，两者口径一致。
     */
    private static final int GESTURE_MIN_H_PX = 184;
    private static final int GESTURE_MIN_W_PX = 24;

    /** 比例 → 像素。`Math.round` 而**不是** `rint`：这里要最近的整数，不是银行家舍入。 */
    private static int insetPx(double dim, double rel, int cap) {
        long v = Math.round(dim * rel);
        return (int) Math.max(0L, Math.min(cap, v));
    }

    /**
     * 把**按下点**夹出系统手势区（顶部 y≤74 / 底部 y≥640 / 左 x≤104 / 右 x≥1580，均为
     * HONOR 1610×720 的实测值；实际内缩按屏幕比例缩放，见 `GESTURE_*_REL`）。
     *
     * ★ 2026-10-26：`玩具狂奏曲終焉 IN/AT` 的下滑会把通知栏拉下来（sticky immersive 只挡第一次，
     *   连续两次仍会拉开）⇒ 整轮作废。关键认识：**通知栏只在"触摸起手点"落在顶部手势区时才会被拉下**；
     *   手指一旦在安全区按下，之后滑到哪都不会拉。
     *   ⇒ 只夹 `DOWN/POINTER_DOWN`，MOVE 保持真实轨迹（AT 有 593 条贴顶 MOVE，全夹会把轨迹压平 ⇒ 大量 Good/Miss）。
     * ⚠ 设备太小时不夹，留给 `requireGameCanvas` 报错，否则会把那道闸门测成绿。
     */
    public static int[] clampOutOfGestureZone(int[] dev, double dstW, double dstH) {
        // ① 旧语义：屏幕太小就不夹（绝对下限，见 GESTURE_MIN_*_PX 的注释）。
        if (dstH < GESTURE_MIN_H_PX || dstW < GESTURE_MIN_W_PX) return dev;

        // 兜底上限，只为挡住"比例被写错成 0.5"这类事故；正常参数（92/184）远小于它，不生效。
        // ⛔ 它不是调参旋钮 —— 别拿它当设计参数。
        final int cap = (int) Math.max(dstW, dstH) / 4;
        final int top = insetPx(dstH, GESTURE_TOP_REL, cap);
        final int bottomIn = insetPx(dstH, GESTURE_BOTTOM_REL, cap);
        final int side = insetPx(dstW, GESTURE_SIDE_REL, cap);
        // ② 防荒谬比例：夹完得还剩空间。
        if (top + bottomIn >= dstH - 1 || 2 * side >= dstW) return dev;

        int x = dev[0], y = dev[1];
        final int bottomEdge = (int) dstH - bottomIn;
        final int rightEdge = (int) dstW - side;
        if (y < top) y = top;
        if (y > bottomEdge) y = bottomEdge;
        if (x < side) x = side;
        if (x > rightEdge) x = rightEdge;
        return new int[]{x, y};
    }

    /** 默认开启 MOVE 加密（与 PC 生产路径一致：2125 → 2188 事件）。 */
    public static List<Cmd> build(Path payload, double dstW, double dstH, boolean fitHeight)
            throws IOException {
        return build(payload, dstW, dstH, fitHeight, true, null, null);
    }

    public static List<Cmd> build(Path payload, double dstW, double dstH, boolean fitHeight,
                                  boolean densify, Densify.Stats st) throws IOException {
        return build(payload, dstW, dstH, fitHeight, densify, st, null);
    }

    /** 解析载荷并产出指令序列（稳定排序：只按 t_ms）。`meta` 可为 null（仅日志用）。 */
    public static List<Cmd> build(Path payload, double dstW, double dstH, boolean fitHeight,
                                  boolean densify, Densify.Stats st, Meta meta) throws IOException {
        // ★ 端内用 parsePayload（只物化三个键）：demiurge 载荷 1.8 MB，其中 `plan`
        //   有 5 万条明细，全物化会在手机上白吃几十 MB 堆 + 秒级解析 —— 而注入只
        //   需要 canvas / screen / events。PC 侧自测仍可用 parse() 全解。
        // ★ 必须用 readAllBytes + new String，**不能**用 Files.readString：
        //   readString 是 Java 11 API，Android 的 java.nio.file 是裁剪实现，
        //   API 34 上依然 NoSuchMethodError（2026-10-21 真机实测炸在待命第一步）。
        //   javac 用 JDK 17 编得过、PC 上跑得过 ⇒ 这个错误**离线抓不到**，
        //   只能靠 verify_apk.sh 的禁用符号扫描挡回归。
        Object root = Json.parsePayload(
                new String(Files.readAllBytes(payload), StandardCharsets.UTF_8));
        Map<?, ?> obj = (Map<?, ?>) root;
        Map<?, ?> canvas = (Map<?, ?>) obj.get("canvas");
        Map<?, ?> screen = (Map<?, ?>) obj.get("screen");
        List<?> events = (List<?>) obj.get("events");
        if (canvas == null || screen == null || events == null) {
            throw new IllegalStateException("载荷缺 canvas / screen / events");
        }
        double canvasW = ((Number) canvas.get("width")).doubleValue();
        double canvasH = ((Number) canvas.get("height")).doubleValue();
        double srcW = ((Number) screen.get("width")).doubleValue();
        double srcH = ((Number) screen.get("height")).doubleValue();

        // ★★★ 载荷自洽闸门 A：canvas 必须是 16:9 的游戏画布。
        //   2026-10-24 事故：载荷的 canvas 被填成了 screen（1080x2340），toDevice 于是把
        //   每一个点压进屏幕中间一条竖带里 —— 事件照发、日志照常、只有成绩是 40%/31%。
        //   宁可在这里拒绝开火，也不要打一局坐标全错的（项目规矩：缺素材一律拒绝，不许降级）。
        requireGameCanvas(canvasW, canvasH, srcW, srcH);

        int n = events.size();
        // 记录原始下标后稳定排序：等价于 Python sorted(range(n), key=lambda i: t_ms[i])
        List<Integer> order = new ArrayList<>(n);
        for (int i = 0; i < n; i++) order.add(i);
        order.sort((a, b) -> Double.compare(
                ((Number) ((Map<?, ?>) events.get(a)).get("t_ms")).doubleValue(),
                ((Number) ((Map<?, ?>) events.get(b)).get("t_ms")).doubleValue()));

        // ① 排好序的原始事件（仍是**屏幕坐标**）—— 加密必须在变换之前做，
        //    因为 densify.py 的 Drag 判定带（150 canvas px）是拿 screen 坐标乘
        //    1920/1080 判的；换到设备像素上算就得多一次反推+两次取整。
        List<Densify.Raw> raws = new ArrayList<>(n);
        for (int i = 0; i < n; i++) {
            int srcIdx = order.get(i);
            Map<?, ?> e = (Map<?, ?>) events.get(srcIdx);
            double ex = ((Number) e.get("x")).doubleValue();
            double ey = ((Number) e.get("y")).doubleValue();
            // ★ 闸门 B：事件坐标必须落在 screen 口径内。
            //   它**拦不住** canvas 写错那种形态（事件本来就在 screen 空间里），
            //   挡的是另一类错：生成端把 canvas 坐标当 screen 坐标塞了进来。
            if (!inRange(ex, srcW) || !inRange(ey, srcH)) {
                throw new IllegalStateException(String.format(Locale.ROOT,
                        "载荷事件坐标越界（第 %d 条 %s）：(%.1f,%.1f) 不在 screen %.0fx%.0f 内"
                                + " ⇒ 载荷口径不对，不是时基问题",
                        srcIdx, e.get("action"), ex, ey, srcW, srcH));
            }
            raws.add(new Densify.Raw(srcIdx,
                    ((Number) e.get("t_ms")).doubleValue(),
                    (String) e.get("action"),
                    ((Number) e.get("pointer_id")).intValue(),
                    ex, ey));
        }

        // ② MOVE 加密（可关，A/B 用）。插值点 srcIdx = -1，且**不重排** ——
        //    与 densify.py 一样就地追加，重排会改同一时刻多指针的相对顺序。
        if (st != null) st.nIn = raws.size();
        if (densify) {
            raws = Densify.densify(raws, st != null ? st : new Densify.Stats());
        } else if (st != null) {
            st.nOut = raws.size();
        }

        // ③ 逐条变换成设备像素
        List<Cmd> out = new ArrayList<>(raws.size());
        int nKey = 0;
        // ★ 闸门 C：变换后的坐标必须落在设备屏幕内。
        //   教训 15：判据要盯「功能赖以成立的那个量」—— 坐标出界必 Miss，这条与设备无关。
        //   ⚠ 但它**拦不住** 10-24 那次 canvas 写错：错误载荷只是把点压进屏幕中间一块，
        //     仍在屏内（正因如此成绩还有 31%）。拦那次的是闸门 A。三条各挡一类，别指望一条全包。
        int oob = 0;
        String oobFirst = null;
        for (int seq = 0; seq < raws.size(); seq++) {
            Densify.Raw r = raws.get(seq);
            int[] dev = toDevice(r.x(), r.y(),
                    canvasW, canvasH, srcW, srcH, dstW, dstH, fitHeight);
            // ★★ 2026-10-26 改为**只夹「按下」**：通知栏只在"触摸**起手**位于顶部手势区"时才会被拉下，
            //   手指一旦在安全区按下，之后无论怎样下滑都不会拉下通知栏。
            //   为什么不能全夹：`玩具狂奏曲終焉 AT` 有 593 条 MOVE 贴在 y=16~88（跟着判定线走），
            //   全夹到 92 会把整条轨迹压平 ⇒ 用户实测「很多 good/miss」。只夹 8 条 POINTER_DOWN
            //   就把"起手点"挪出危险区，轨迹仍按真实位置走。
            if ("DOWN".equals(r.action()) || "POINTER_DOWN".equals(r.action())) {
                dev = clampOutOfGestureZone(dev, dstW, dstH);
            }
            boolean key = isKey(r.action());
            if (key) nKey++;
            if (oob < 1_000_000 && (!inRange(dev[0], dstW) || !inRange(dev[1], dstH))) {
                if (oob == 0) {
                    oobFirst = String.format(Locale.ROOT, "seq=%d %s (%.1f,%.1f)->(%d,%d)",
                            seq, r.action(), r.x(), r.y(), dev[0], dev[1]);
                }
                oob++;
            }
            out.add(new Cmd(seq, r.srcIdx(), r.tMs(), r.action(), r.pid(),
                    dev[0], dev[1], key));
        }
        if (oob > 0) {
            throw new IllegalStateException(String.format(Locale.ROOT,
                    "排程坐标越界：%d/%d 条落在设备 %.0fx%.0f 之外（首个 %s）"
                            + " ⇒ 变换参数或载荷口径不对，不是时基问题",
                    oob, raws.size(), dstW, dstH, oobFirst));
        }
        if (meta != null) {
            meta.canvasW = canvasW;
            meta.canvasH = canvasH;
            meta.srcW = srcW;
            meta.srcH = srcH;
            meta.nEvents = n;
            meta.nCmds = out.size();
            meta.nKey = nKey;
        }
        return out;
    }

    /** 闭区间：`toDevice` 的 rint 与上游 clamp 都可能正好落在边界上，不等于出错。 */
    private static boolean inRange(double v, double dim) {
        return v >= 0 && v <= dim;
    }

    /**
     * 闸门 A：游戏画布必须是 **16:9 横屏**。
     *
     * ★ 为什么只盯宽高比：`to_canvas` 的 `yOff = (srcH − canvasH·srcW/canvasW)/2` 只有在
     *   `canvas` 是**游戏渲染画布**时才有意义。它若被写成 `screen`（1080x2340，宽高比 0.46），
     *   `to_device` 会把所有坐标压进屏幕中间一条竖带 —— 事件一条不少地发出去，成绩却是 40%。
     *
     * ★ 为什么**不能**写成「canvas 必须 ≠ screen」：`out/touches_schneid.json` 与
     *   `touches_yume.json` 的 `canvas == screen == 1920x1080` 是**合法退化**（游戏分辨率
     *   恰好等于画布，`to_canvas` 成恒等）。那条判据会把这两首直接判死。
     *
     * ★ 2026-10-24 实测：`out/` 71 份 touches + 4 份 assets **全部**是 16:9 ⇒ 本闸门零误伤。
     */
    private static void requireGameCanvas(double cw, double ch, double sw, double sh) {
        if (!(cw > 0) || !(ch > 0)) {
            throw new IllegalStateException("载荷 canvas 尺寸非法：" + cw + "x" + ch);
        }
        double lhs = cw * 9.0;
        double rhs = ch * 16.0;
        double tol = 1e-3 * Math.max(lhs, rhs);      // 相对容差 ≈0.1%（1920 宽约 ±2 px）
        if (Math.abs(lhs - rhs) > tol) {
            throw new IllegalStateException(String.format(Locale.ROOT,
                    "载荷 canvas 口径不对：canvas=%.0fx%.0f（宽高比 %.4f ≠ 16:9），screen=%.0fx%.0f。"
                            + "游戏画布固定 16:9（如 1920x1080），出现非 16:9 几乎一定是 canvas 被填成了 screen"
                            + " ⇒ 这不是时基问题，查载荷生成或缓存（PlayService/TouchProbe 的 PayloadCache）",
                    cw, ch, cw / ch, sw, sh));
        }
    }

    public static void emitTsv(List<Cmd> rows) {
        StringBuilder sb = new StringBuilder(rows.size() * 48);
        sb.append("seq\tsrc_idx\tt_ms\taction\tpid\tdev_x\tdev_y\tis_key\n");
        for (Cmd c : rows) {
            sb.append(c.seq()).append('\t')
              .append(c.srcIdx()).append('\t')
              .append(String.format(Locale.ROOT, "%.6f", c.tMs())).append('\t')
              .append(c.action()).append('\t')
              .append(c.pid()).append('\t')
              .append(c.devX()).append('\t')
              .append(c.devY()).append('\t')
              .append(c.key() ? 1 : 0).append('\n');
        }
        System.out.print(sb);
    }

    /** 一条调度决策（与 sched_ref.py 的 schedule() 逐条对应）。 */
    public record Decision(int seq, String action, boolean key, boolean emit,
                          double startMs, double devMs) { }

    /**
     * 两级调度决策规则（与 tools/apk_probe/sched_sim.py 的 prio 分支、sched_ref.py 逐字一致）。
     *
     * 关键事件：start = max(due + tick, finish); finish = start + cost —— 必发。
     * MOVE    ：finish <= due 才发（"现在有空、不拖累后面"），否则丢。
     *
     * ⚠ 改这条规则必须同时改 sched_ref.py 与 sched_sim.py，否则端内行为与
     *   验证过的模型不是一回事（三处实现，任一处漂移都会静默改变精度）。
     */
    public static List<Decision> schedule(List<Cmd> cmds, double costMs, double tickMs) {
        List<Decision> out = new ArrayList<>(cmds.size());
        double finish = Double.NEGATIVE_INFINITY;   // 上一条注入完成时刻
        for (Cmd c : cmds) {
            double due = c.tMs();
            double wake = due + tickMs;
            if (c.key()) {
                double start = Math.max(wake, finish);
                finish = start + costMs;
                out.add(new Decision(c.seq(), c.action(), true, true, start, start - due));
            } else {
                if (finish <= due) {
                    double start = Math.max(wake, finish);
                    finish = start + costMs;
                    out.add(new Decision(c.seq(), c.action(), false, true, start, start - due));
                } else {
                    out.add(new Decision(c.seq(), c.action(), false, false,
                            Double.NaN, Double.NaN));
                }
            }
        }
        return out;
    }

    public static void emitSchedTsv(List<Decision> plan) {
        StringBuilder sb = new StringBuilder(plan.size() * 40);
        sb.append("seq\taction\tis_key\tdecision\tstart_ms\tdev_ms\n");
        for (Decision d : plan) {
            sb.append(d.seq()).append('\t')
              .append(d.action()).append('\t')
              .append(d.key() ? 1 : 0).append('\t')
              .append(d.emit() ? "emit" : "drop").append('\t')
              // NaN 打印成 "-"，与 Python 侧 None 的分支一致
              .append(Double.isNaN(d.startMs()) ? "-"
                      : String.format(Locale.ROOT, "%.6f", d.startMs())).append('\t')
              .append(Double.isNaN(d.devMs()) ? "-"
                      : String.format(Locale.ROOT, "%.6f", d.devMs())).append('\n');
        }
        System.out.print(sb);
    }

    public static void main(String[] args) throws Exception {
        if (args.length < 1) {
            System.err.println("用法:");
            System.err.println("  Scheduler <touches.json> [dstW=1610] [dstH=720] [fit=height]");
            System.err.println("  Scheduler <touches.json> [dst...] --sched <costMs> [tickMs=0]");
            System.err.println("  Scheduler <touches.json> [dst...] --nodensify   # A/B：关掉 MOVE 加密");
            System.exit(2);
        }
        // Path.of 同为 Java 11 API（端内不会走 main，但别留一颗定时炸弹）
        Path p = Paths.get(args[0]);

        // 提取 --sched / --tick（位置无关），其余按位置解析
        Double schedCost = null;
        double tickMs = 0.0;
        boolean densify = true;
        List<String> pos = new ArrayList<>();
        for (int i = 1; i < args.length; i++) {
            if ("--nodensify".equals(args[i])) {
                densify = false;
            } else if ("--sched".equals(args[i])) {
                schedCost = Double.parseDouble(args[++i]);
            } else if ("--tick".equals(args[i])) {
                tickMs = Double.parseDouble(args[++i]);
            } else {
                pos.add(args[i]);
            }
        }
        double dstW = pos.size() > 0 ? Double.parseDouble(pos.get(0)) : 1610;
        double dstH = pos.size() > 1 ? Double.parseDouble(pos.get(1)) : 720;
        boolean fitHeight = pos.size() <= 2 || "height".equals(pos.get(2));

        Densify.Stats st = new Densify.Stats();
        List<Cmd> cmds = build(p, dstW, dstH, fitHeight, densify, st);
        if (densify) {
            System.err.println("MOVE 加密：" + st);
        } else {
            System.err.println("MOVE 加密：关（--nodensify），" + st.nIn + " 条");
        }
        if (schedCost == null) {
            emitTsv(cmds);
        } else {
            emitSchedTsv(schedule(cmds, schedCost, tickMs));
        }
    }

    /**
     * 极简 JSON 解析器（仅够读本项目的 touches 载荷）。
     *
     * 为什么不引 gson/jackson：端内 APK 是免 Gradle 构建（aapt2/javac/d8），
     * 加第三方 jar 会把构建链复杂化；而载荷 schema 固定，200 行足够。
     * 只在 S1/S2 内部使用，不做通用性承诺。
     */
    static final class Json {
        private final String s;
        private int i;

        private Json(String s) { this.s = s; }

        static Object parse(String s) {
            Json j = new Json(s);
            j.ws();
            Object v = j.value();
            j.ws();
            if (j.i != s.length()) throw new IllegalStateException("JSON 尾部多余内容 @" + j.i);
            return v;
        }

        /** 只保留 canvas / screen / events，其余顶层键用 {@link #skipValue()} 跳过（不分配）。 */
        static Object parsePayload(String s) {
            Json j = new Json(s);
            j.ws();
            if (s.charAt(j.i) != '{') throw new IllegalStateException("载荷顶层必须是对象 @" + j.i);
            j.i++;
            Map<String, Object> m = new LinkedHashMap<>();
            j.ws();
            if (s.charAt(j.i) == '}') { j.i++; return m; }
            while (true) {
                j.ws();
                String k = j.string();
                j.ws();
                if (s.charAt(j.i) != ':') throw new IllegalStateException("期望 : @" + j.i);
                j.i++;
                j.ws();
                switch (k) {
                    case "canvas", "screen", "events" -> m.put(k, j.value());
                    default -> j.skipValue();
                }
                j.ws();
                char c = s.charAt(j.i++);
                if (c == '}') break;
                if (c != ',') throw new IllegalStateException("期望 , 或 } @" + (j.i - 1));
            }
            j.ws();
            if (j.i != s.length()) throw new IllegalStateException("JSON 尾部多余内容 @" + j.i);
            return m;
        }

        /** 跳过一个值：只走结构、不分配对象（用于 `plan` 那种 5 万条的明细数组）。 */
        private void skipValue() {
            char c = s.charAt(i);
            switch (c) {
                case '{', '[' -> {
                    int depth = 0;
                    while (i < s.length()) {
                        char d = s.charAt(i);
                        if (d == '"') { skipString(); continue; }
                        if (d == '{' || d == '[') {
                            depth++;
                        } else if (d == '}' || d == ']') {
                            depth--;
                            i++;
                            if (depth == 0) return;
                            continue;
                        }
                        i++;
                    }
                    throw new IllegalStateException("跳过一个值时到了文件尾 @" + i);
                }
                case '"' -> skipString();
                case 't' -> expect("true");
                case 'f' -> expect("false");
                case 'n' -> expect("null");
                default -> number();
            }
        }

        private void skipString() {
            if (s.charAt(i) != '"') throw new IllegalStateException("期望 \" @" + i);
            i++;
            while (true) {
                char c = s.charAt(i++);
                if (c == '\\') { i++; continue; }
                if (c == '"') return;
            }
        }

        private void ws() {
            while (i < s.length() && Character.isWhitespace(s.charAt(i))) i++;
        }

        private Object value() {
            char c = s.charAt(i);
            switch (c) {
                case '{': return object();
                case '[': return array();
                case '"': return string();
                case 't': expect("true"); return Boolean.TRUE;
                case 'f': expect("false"); return Boolean.FALSE;
                case 'n': expect("null"); return null;
                default: return number();
            }
        }

        private void expect(String lit) {
            if (!s.startsWith(lit, i)) throw new IllegalStateException("期望 " + lit + " @" + i);
            i += lit.length();
        }

        private Map<String, Object> object() {
            Map<String, Object> m = new LinkedHashMap<>();
            i++; // {
            ws();
            if (s.charAt(i) == '}') { i++; return m; }
            while (true) {
                ws();
                String k = string();
                ws();
                if (s.charAt(i) != ':') throw new IllegalStateException("期望 : @" + i);
                i++;
                ws();
                m.put(k, value());
                ws();
                char c = s.charAt(i++);
                if (c == '}') return m;
                if (c != ',') throw new IllegalStateException("期望 , 或 } @" + (i - 1));
            }
        }

        private List<Object> array() {
            List<Object> a = new ArrayList<>();
            i++; // [
            ws();
            if (s.charAt(i) == ']') { i++; return a; }
            while (true) {
                ws();
                a.add(value());
                ws();
                char c = s.charAt(i++);
                if (c == ']') return a;
                if (c != ',') throw new IllegalStateException("期望 , 或 ] @" + (i - 1));
            }
        }

        private String string() {
            if (s.charAt(i) != '"') throw new IllegalStateException("期望 \" @" + i);
            i++;
            StringBuilder sb = new StringBuilder();
            while (true) {
                char c = s.charAt(i++);
                if (c == '"') return sb.toString();
                if (c == '\\') {
                    char e = s.charAt(i++);
                    switch (e) {
                        case '"' -> sb.append('"');
                        case '\\' -> sb.append('\\');
                        case '/' -> sb.append('/');
                        case 'b' -> sb.append('\b');
                        case 'f' -> sb.append('\f');
                        case 'n' -> sb.append('\n');
                        case 'r' -> sb.append('\r');
                        case 't' -> sb.append('\t');
                        case 'u' -> {
                            sb.append((char) Integer.parseInt(s.substring(i, i + 4), 16));
                            i += 4;
                        }
                        default -> throw new IllegalStateException("非法转义 \\" + e);
                    }
                } else {
                    sb.append(c);
                }
            }
        }

        private Double number() {
            int st = i;
            while (i < s.length() && "+-0123456789.eE".indexOf(s.charAt(i)) >= 0) i++;
            if (st == i) throw new IllegalStateException("期望数字 @" + st);
            return Double.valueOf(s.substring(st, i));
        }
    }
}
