package io.pgauto.play;

import java.util.Locale;

/**
 * 「开始演奏」按钮的**预期框**（相对屏幕的比例）—— 只用于**判读**，不参与任何点击决策。
 *
 * ★ 为什么单独成一个类：`selftest.sh` 用**没有 android.jar** 的 classpath 编它，
 *   这样框的边界和文案能被纯逻辑钉死。`PlayService` 依赖 Android，测不了这个。
 *
 * <h3>数据来源（唯一一次像素级测量）</h3>
 * `out/run_chronostasis_in_ap_2026-10-26_1924.png`（HONOR ALT-AN00，1610×720 真机选曲页）：
 * <pre>
 *   白底板（按钮整块）  x 1465..1610（贴右边缘）   y 558..665
 *   内部 ▶ 图标         x 1515..1540            y 588..638
 * </pre>
 * 可点区取**白底板**（Phigros 这个按钮整块响应），图标只是视觉中心。
 *
 * <h3>★★ 2026-10-30 改版：从「在不在框内」改成「离中心多远 + 照着填多少」</h3>
 * 旧版只做一次矩形包含判断，落在框内就报 `在内 ✓`。用户实测被它带偏两次：
 * <pre>
 *   dx −58 / dy −57  ⇒  落点 1464.72, 558.50  ⇒  旧判读「在内 ✓（与边界差 &lt;1px，可视为对准）」
 *   实际：x 差 0.28px **落在左边界外**，正压按钮左上角顶点 ⇒ 点不中
 * </pre>
 * 那次"可视为对准"来自 0.5px 的显示宽容（防止把 0.3px 写成「左侧 0px」），
 * 结果把**唯一的警告吞掉了**。教训：**二元判断（在内/在外）表达不了"离可点区域多近"**，
 * 而用户真正需要的是「往哪挪多少」。
 * ⇒ 现在三种档位，任何一档都**同时给方向和距离**：
 * <pre>
 *   ✓ 在框内 四边余量都 ≥ {@link #EDGE_WARN_PX}
 *   ⚠ 贴边   在框内但最近边不足阈值（0.3px 那种必须抓到）
 *   ✗ 出界   列出每条出界的边
 * </pre>
 * ⛔ 2026-10-31 起**不再给 dx/dy 该填多少**：手填偏移已废弃，坐标只能由
 * 「学习落点」（用户自己点一次）写入。三档只作参考，不再叫「对准」——
 * 这个框的 X0 实测比真实左边界小 26 px（1439 vs 1465），它不配下一个"对/不对"的结论。
 *
 * <h3>★ 边界（别把这条当物理真值）</h3>
 * 这是**一台机器**的实测比例。它回答的是「落点离 HONOR 实测那块矩形的中心多远」，
 * **超出不代表一定点不中**（不同宽高比下游戏 UI 可能重排），落在中心也**不等于**一定点中。
 * 真正能定论的只有「点完有没有起音」。
 */
public final class StartButtonBox {

    /** 白底板左边界：1465 / 1610 ≈ 0.90994。 */
    public static final double X0 = 1465.0 / 1610.0;
    /** 白底板右边界 = 屏幕右边缘。 */
    public static final double X1 = 1.0;
    /** 白底板上边界：558 / 720 ≈ 0.775。 */
    public static final double Y0 = 558.0 / 720.0;
    /** 白底板下边界：665 / 720 ≈ 0.92361。 */
    public static final double Y1 = 665.0 / 720.0;

    /**
     * 在框内、但最近一条边的余量小于它 ⇒ 判「贴边」并直接给警告。
     *
     * ★ 10 px 的来历：按钮 145×107 px，10 px ≈ 7% 短边；小于它时，
     *   触摸坐标抖动 + 游戏侧可能的圆角/内边距就足以让这一下落到可点区之外。
     *   2026-10-30 用户踩的是 **0.28 px**（比任何合理阈值都小），所以阈值只要 &gt;1 就能抓到。
     */
    public static final double EDGE_WARN_PX = 10.0;

    private StartButtonBox() {
    }

    /**
     * 落点是否在预期框内（闭区间）。
     *
     * ⚠ 这只是几何包含，**不是"点中了没有"**。要判断"离中心多远"用 {@link #verdict}。
     */
    public static boolean inside(double x, double y, double w, double h) {
        if (!(w > 0) || !(h > 0)) return false;
        return x >= w * X0 && x <= w * X1 && y >= h * Y0 && y <= h * Y1;
    }

    /** 框中心的 x（设备横屏像素）—— 也是「⇢ 对准按钮中心」要落到的位置。 */
    public static double centerX(double w) {
        return w * (X0 + X1) / 2.0;
    }

    /** 框中心的 y。 */
    public static double centerY(double h) {
        return h * (Y0 + Y1) / 2.0;
    }

    /**
     * 让落点落到框中心所需的 **dx 绝对偏移**（不是增量）。
     *
     * ★ 单一来源：`MainActivity` 的「⇢ 对准按钮中心」按钮也调这里，
     *   避免"两处各算一遍"—— 那种重复本项目的教训是**必然漂移**。
     *
     * @param w    屏幕横屏宽
     * @param relX `PlayService.START_REL_X`（无偏移时的比例落点）
     */
    public static double targetDx(double w, double relX) {
        return centerX(w) - w * relX;
    }

    /** 让落点落到框中心所需的 **dy 绝对偏移**。 */
    public static double targetDy(double h, double relY) {
        return centerY(h) - h * relY;
    }

    /**
     * 一行判读文本（进日志 / 真机「测试 ▶」）。
     * ⚠ 纯诊断，调用方**不许**据此改坐标或中止。
     *
     * <pre>
     * ▶ 落点 ✓ 在框内（距中心 左15 下4 px；余量 左58 右87 上58 下50）⇒ 落在框内≠点得中…
     * ▶ 落点 ⚠ 贴边（距中心 左73 下4 px；最近边只剩 3 px）⇒ 很可能点不中；用「学习落点」重记…
     * ▶ 落点 ✗ 出界（左边界外 0.3 px；距中心 左73 上53 px）⇒ 用「学习落点」重记…
     * </pre>
     *
     * @param relX 无偏移时的比例落点 x（`PlayService.START_REL_X`）
     * @param relY 同上 y
     */
    public static String verdict(double x, double y, double w, double h, double relX, double relY) {
        if (!(w > 0) || !(h > 0)) {
            return "▶ 落点：屏幕尺寸非法（" + w + "x" + h + "）⇒ 跳过判读";
        }
        double cx = centerX(w);
        double cy = centerY(h);
        double left = x - w * X0;
        double right = w * X1 - x;
        double top = y - h * Y0;
        double bottom = h * Y1 - y;
        boolean in = left >= 0 && right >= 0 && top >= 0 && bottom >= 0;

        String sx = dir(x - cx, "左", "右");
        String sy = dir(y - cy, "上", "下");
        String off = "距中心 " + (sx.isEmpty() ? "水平居中" : sx)
                + " " + (sy.isEmpty() ? "垂直居中" : sy);
        String fix = "用「▶ 落点」→「学习落点」重记一次（手填偏移已于 10-31 取消）";
        String box = "（框=1610×720 实测反推，非物理真值）";

        if (!in) {
            // 出界：逐条列出**每条出界的边**。差 0.3px 与差 1365px 读起来必须不一样，
            // 否则用户会以为"完全没救"。
            StringBuilder where = new StringBuilder();
            if (left < 0) where.append("左边界外 ").append(nEdge(-left)).append(" px");
            if (right < 0) where.append(where.length() > 0 ? "、" : "").append("右边界外 ").append(nEdge(-right)).append(" px");
            if (top < 0) where.append(where.length() > 0 ? "、" : "").append("上边界外 ").append(nEdge(-top)).append(" px");
            if (bottom < 0) where.append(where.length() > 0 ? "、" : "").append("下边界外 ").append(nEdge(-bottom)).append(" px");
            return "▶ 落点 ✗ 出界（" + where + "；" + off + " px）⇒ " + fix + box;
        }
        double margin = Math.min(Math.min(left, right), Math.min(top, bottom));
        if (margin < EDGE_WARN_PX) {
            // ★ 这一档是 2026-10-30 新增的核心：旧版把 0.3px 的余量说成"可视为对准"。
            return "▶ 落点 ⚠ 贴边（" + off + " px；最近边只剩 " + nEdge(margin)
                    + " px）⇒ 很可能点不中；" + fix + box;
        }
        // ⛔ 不再写「✓ 对准」：框是 10-26 一台机反推的，X0 实测比真实左边界小 26 px，
        //   "对准"这个词会让人以为位置已被验证 —— 真实判据只有「游戏有没有开始」。
        return "▶ 落点 ✓ 在框内（" + off + " px；余量 左" + n(left) + " 右" + n(right)
                + " 上" + n(top) + " 下" + n(bottom) + "）⇒ 落在框内≠点得中；" + fix + box;
    }

    /**
     * 方向 + 取整后的距离；不到半个像素返回空串（调用方改写成「水平/垂直居中」），
     * 免得写出「左0」那种读起来像 bug 的东西。
     */
    private static String dir(double d, String neg, String pos) {
        if (Math.abs(d) < 0.5) return "";
        return (d < 0 ? neg : pos) + n(Math.abs(d));
    }

    private static String n(double v) {
        return String.format(Locale.ROOT, "%.0f", v);
    }

    /**
     * 出界量 / 余量专用：小于 1 px 时保留一位小数。
     *
     * ★ 2026-10-30：差 0.28px 被 `%.0f` 写成「左边界外 0 px」——
     *   读起来像 bug，而它恰恰是**这次点不中的真正原因**，必须显示出来。
     */
    private static String nEdge(double v) {
        return v > 0 && v < 1 ? String.format(Locale.ROOT, "%.1f", v) : n(v);
    }
}
