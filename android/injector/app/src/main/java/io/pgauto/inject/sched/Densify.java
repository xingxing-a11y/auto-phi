package io.pgauto.inject.sched;

// MOVE 加密的**端内**实现（与 tmp/densify.py 逐式对应，纯逻辑、零 Android 依赖）。
//
// ★ 为什么端内必须自己做（2026-10-21 dp1 轮实锤）：
//   PC 注入路径默认开加密（2125 → 2188 事件），端内**没搬**这一层 ⇒ 端内只发 2125 条
//   ⇒ 同一份载荷、同一台机，PC 打 φ 而端内打出 505 P / 15 Miss（Max Combo 197）。
//   densify.py 的注释本来就写着「入口 MOVE 一丢必 Miss」—— 这条规则在端内缺了一整层。
//
// ★ 为什么作用在**屏幕坐标**而不是设备像素（顺序很要紧）：
//   densify.py 用 `dist_cv = hypot(dx,dy) × 1920/1080` 判 Drag 判定带（150 canvas px）。
//   若在设备像素上算，就得反推 canvas（`(dev-165)/0.6667`），既多一次浮点又引入取整误差。
//   ⇒ 正确顺序与会话无关：**先排序 → 加密 → 再变换坐标**。这也是 PC 的顺序。
//
// 与 Python 的三个对齐点（写错就对拍不上，且都是静默错）：
//   1. 取整用 Math.rint()（half-even），与 Python round() 一致；
//   2. 插值点必须严格落在 (t0, t1) 开区间内（`<= t0` 或 `>= t1` 一律跳过）；
//   3. 插值点**就地追加在当前事件之前**，插完不再重排 —— 原实现就是这样，
//      重新排序会改变同一时刻多指针事件的相对顺序。
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

public final class Densify {

    /** 子 MOVE 间隔 ≈ 1/16 拍 @BPM223（与 densify.py 的 STEP_MS 同值）。 */
    public static final double STEP_MS = 16.8;
    /** 超过此间隙视为「折返/维持段」，不整段加密，只做尾部渐进。 */
    public static final double MAX_GAP_MS = 50.0;
    /** 长间隙渐进窗口（恒定，不随 step 变）。 */
    public static final double TAIL_WINDOW_MS = 25.5;
    /** Drag 判定带（canvas px）。 */
    public static final double THRESH_PX = 150.0;
    /** 事件坐标(screen) → 画布(canvas) 的放大倍数。 */
    public static final double CANVAS_PER_SCREEN = 1920.0 / 1080.0;

    /** 与 Cmd 同形但保留**未变换**的屏幕坐标；srcIdx < 0 表示加密时新插的点。 */
    public record Raw(int srcIdx, double tMs, String action, int pid, double x, double y) { }

    private static boolean posAction(String a) {
        return switch (a) {
            case "DOWN", "POINTER_DOWN", "MOVE", "UP", "POINTER_UP" -> true;
            default -> false;
        };
    }

    private static boolean driveAction(String a) {
        return switch (a) {
            case "DOWN", "POINTER_DOWN", "MOVE" -> true;
            default -> false;
        };
    }

    /** Python round(x, 2)：half-even。x*100 的浮点误差与 CPython 走同一条路径。 */
    private static double r2(double v) {
        return Math.rint(v * 100.0) / 100.0;
    }

    public static final class Stats {
        public int nIn, nOut, nAdded, nSegments, nTailAdded, nTailSegments;
        @Override public String toString() {
            return nIn + " → " + nOut + "（规则1 插值 " + nAdded + "/" + nSegments
                    + " 段；规则2 渐进 " + nTailAdded + "/" + nTailSegments + " 段）";
        }
    }

    /** 长间隙里该插几个点（与 densify.py 规则2 同式）。 */
    private static int tailCount(double dx, double dy, double dt) {
        double distCv = Math.hypot(dx, dy) * CANVAS_PER_SCREEN;
        int nNeed = distCv > THRESH_PX
                ? (int) Math.ceil((1.0 - THRESH_PX / distCv) * dt / STEP_MS)
                : 1;
        int nByWindow = Math.max(1, (int) Math.ceil(Math.min(TAIL_WINDOW_MS, dt * 0.5) / STEP_MS));
        return Math.max(1, Math.min(nNeed, nByWindow));
    }

    /**
     * 返回加密后的新列表（输入不被修改）。**调用方必须已经按 t_ms 稳定排序**。
     *
     * @param events 排好序的原始事件（屏幕坐标）
     */
    public static List<Raw> densify(List<Raw> events, Stats st) {
        List<Raw> out = new ArrayList<>(events.size() + events.size() / 10 + 8);
        Map<Integer, Raw> last = new HashMap<>();
        for (Raw e : events) {
            String a = e.action();
            int pid = e.pid();
            if ("MOVE".equals(a)) {
                Raw p = last.get(pid);
                if (p != null && driveAction(p.action())) {
                    double t1 = e.tMs();
                    double dt = t1 - p.tMs();
                    if (dt > STEP_MS && dt <= MAX_GAP_MS) {
                        st.nSegments++;
                        int k = (int) Math.floor(dt / STEP_MS);
                        for (int j = 1; j <= k; j++) {
                            st.nAdded += addPoint(out, p, e, p.tMs() + j * STEP_MS);
                        }
                    } else if (dt > MAX_GAP_MS) {
                        int n = tailCount(e.x() - p.x(), e.y() - p.y(), dt);
                        int added = 0;
                        for (int k = n; k >= 1; k--) {
                            added += addPoint(out, p, e, t1 - k * STEP_MS);
                        }
                        if (added > 0) {
                            st.nTailSegments++;
                            st.nTailAdded += added;
                        }
                    }
                }
            }
            if (posAction(a)) {
                last.put(pid, e);
            }
            out.add(e);
        }
        st.nOut = out.size();
        return out;
    }

    /** 单个插值点：严格落在 (t0, t1) 内才加。返回 1/0。 */
    private static int addPoint(List<Raw> out, Raw p, Raw e, double tt) {
        double t0 = p.tMs(), t1 = e.tMs();
        if (tt <= t0 + 1e-6 || tt >= t1 - 1e-6) {
            return 0;
        }
        double f = (tt - t0) / (t1 - t0);
        out.add(new Raw(-1, r2(tt), "MOVE", e.pid(),
                r2(p.x() + (e.x() - p.x()) * f),
                r2(p.y() + (e.y() - p.y()) * f)));
        return 1;
    }

    private Densify() { }
}
