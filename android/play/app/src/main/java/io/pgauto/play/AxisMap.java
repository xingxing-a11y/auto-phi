package io.pgauto.play;

import java.util.ArrayList;
import java.util.List;
import java.util.Locale;

/**
 * 触摸设备的 <b>原始坐标</b>（面板口径）↔ <b>屏幕坐标</b>（当前方向口径）的映射。
 *
 * <h3>为什么需要它</h3>
 * `getevent` 报的是**面板自然方向**的坐标，与当前屏幕方向无关。本机实测：
 * <pre>
 *   /dev/input/event5  name="honor,ts_kit"
 *     ABS_MT_POSITION_X : 0..719        ← 面板宽（竖屏）
 *     ABS_MT_POSITION_Y : 0..1609       ← 面板高
 *   游戏横屏 1610×720
 * </pre>
 * 也就是说 raw 的 **X 轴对应屏幕的 Y 轴**、raw 的 Y 轴对应屏幕的 X 轴，还可能带镜像。
 * 直接把 raw 当屏幕坐标用 ⇒ 落点整体错位，不是偏几像素那种错。
 *
 * <h3>怎么定死（不猜）</h3>
 * {@link #solve} 的用法：注入一个**已知屏幕点**，读回 `getevent` 报的 raw，
 * 让 solve 反解出唯一一组 (rotation, flipX, flipY)。
 * <ul>
 *   <li>解出 <b>0 组</b> ⇒ 标定数据自相矛盾（量程/尺寸填错了），必须报错；</li>
 *   <li>解出 <b>1 组</b> ⇒ 定死，写进 prefs 或常量；</li>
 *   <li>解出 <b>多组</b> ⇒ 该标定点区分不出来（例如正好在屏幕正中），必须换点重标。</li>
 * </ul>
 * ⛔ 不许"取第一个候选"糊过去 —— 那等于把歧义当成答案。
 *
 * <h3>纯 Java</h3>
 * `selftest.sh` 用**没有 android.jar** 的 classpath 编它，所以这里只能 import java.*。
 * rotation 由调用方从 `Display.getRotation()` 取来传进构造函数。
 */
public final class AxisMap {

    /** 屏幕相对面板自然方向的旋转角（度）。只接受 0/90/180/270。 */
    public final int rotation;
    /** 在旋转之后再做的镜像（对应某些面板装反/驱动翻转）。 */
    public final boolean flipX;
    public final boolean flipY;
    /** 触摸设备量程（`getevent -p` 里的 `min 0, max N`）。 */
    public final int rawMaxX;
    public final int rawMaxY;
    /** 当前屏幕尺寸（横屏口径，与 {@code PlayService.landscapeSize()} 一致）。 */
    public final int screenW;
    public final int screenH;

    public AxisMap(int rotation, boolean flipX, boolean flipY,
                   int rawMaxX, int rawMaxY, int screenW, int screenH) {
        if (rotation != 0 && rotation != 90 && rotation != 180 && rotation != 270) {
            throw new IllegalArgumentException("rotation 只能是 0/90/180/270，实得 " + rotation);
        }
        if (rawMaxX <= 0 || rawMaxY <= 0) {
            throw new IllegalArgumentException("触摸量程必须 >0，实得 " + rawMaxX + "x" + rawMaxY);
        }
        if (screenW <= 0 || screenH <= 0) {
            throw new IllegalArgumentException("屏幕尺寸必须 >0，实得 " + screenW + "x" + screenH);
        }
        this.rotation = rotation;
        this.flipX = flipX;
        this.flipY = flipY;
        this.rawMaxX = rawMaxX;
        this.rawMaxY = rawMaxY;
        this.screenW = screenW;
        this.screenH = screenH;
    }

    /** raw 是否在量程内。⛔ 越界不静默：调用方必须据此打日志（见 {@link #rawToScreen}）。 */
    public boolean inRange(int rawX, int rawY) {
        return rawX >= 0 && rawX <= rawMaxX && rawY >= 0 && rawY <= rawMaxY;
    }

    /**
     * raw → 屏幕像素。
     *
     * ★ 越界 raw 会被**夹到量程边界**（不让运行时因为一个坏值崩掉），
     *   但越界这件事本身要由调用方通过 {@link #inRange} 发现并记日志 ——
     *   本项目踩过"静默把缺失当 0"的坑，这里不许重演。
     */
    public int[] rawToScreen(int rawX, int rawY) {
        double nx = clamp01(rawX, rawMaxX);
        double ny = clamp01(rawY, rawMaxY);

        double ux;
        double uy;
        switch (rotation) {
            case 90:
                ux = ny;
                uy = 1.0 - nx;
                break;
            case 180:
                ux = 1.0 - nx;
                uy = 1.0 - ny;
                break;
            case 270:
                ux = 1.0 - ny;
                uy = nx;
                break;
            case 0:
            default:
                ux = nx;
                uy = ny;
                break;
        }
        if (flipX) ux = 1.0 - ux;
        if (flipY) uy = 1.0 - uy;

        int sx = (int) Math.round(ux * screenW);
        int sy = (int) Math.round(uy * screenH);
        // 归一化后乘回来可能因舍入越界 1 px，夹住（否则注入会报越界）
        return new int[]{clampInt(sx, 0, screenW - 1), clampInt(sy, 0, screenH - 1)};
    }

    /** 「开始按钮区」的判定：右下角这块。**只此一处**，别在别处再写一遍比例。 */
    public static final double START_ZONE_X_MIN = 0.60;
    public static final double START_ZONE_Y_MIN = 0.50;

    /** {@link #pickForStartButton} 的结果：{@code map == null} 表示定不下来。 */
    public static final class Pick {
        public final AxisMap map;
        public final String why;

        Pick(AxisMap map, String why) {
            this.map = map;
            this.why = why;
        }
    }

    /** 归一化点是否落在「开始按钮区」（右下角）。 */
    public static boolean inStartZone(int sx, int sy, int screenW, int screenH) {
        return sx >= screenW * START_ZONE_X_MIN && sy >= screenH * START_ZONE_Y_MIN;
    }

    /**
     * 用**用户自己点的那一下**反推定死映射：只有能让该点落进「开始按钮区」的旋转才算候选，
     * 必须**恰好一个**。
     *
     * <h3>为什么不能直接用 {@code Display.getRotation()}</h3>
     * 2026-10-31 真机实测：raw(83,1534) / 量程 719×1609 / 屏幕 1610×720。
     * 用 `Display.getRotation()` 拿到的 0（那一刻前台是**我们自己的 App**，它是竖屏）
     * ⇒ 换算出 (186,686)，落在**左下角**。而用户点的是右下角 ▶。
     * 游戏横屏时真实旋转是 90：同一组数换算出 (1535,637) —— 正好压在 buttons 上。
     * ⇒ 读屏幕旋转这件事，在"此刻前台是谁"上会骗人；用点击本身反推才不依赖前台。
     *
     * <h3>先验收窄（不是猜，是几何必然）</h3>
     * 触摸面板是竖的（maxY &gt; maxX）而屏幕是横的（screenW &gt; screenH）
     * ⇒ 二者坐标系差 90°，候选只剩 {90, 270}；再用「必须落进右下角」二选一。
     *
     * @return map 非 null ⇒ 定死；null ⇒ {@link Pick#why} 说明为什么定不下来（**不许猜一个**）
     */
    public static Pick pickForStartButton(int rawX, int rawY,
                                          int rawMaxX, int rawMaxY,
                                          int screenW, int screenH) {
        boolean panelPortrait = rawMaxY > rawMaxX;
        boolean screenLandscape = screenW > screenH;
        // 两者"朝向一致"（都竖 或 都横）⇒ 差 0° 或 180°；朝向不一致 ⇒ 差 90° 或 270°。
        // ⛔ 第一版把这里写成 `panelPortrait == screenLandscape`（那其实是"不一致"），
        //    结果真机那一下去试 0/180，白跑一轮。t09 就是钉这个的。
        boolean sameOrientation = (panelPortrait != screenLandscape);
        int[] candidates;
        if (sameOrientation) {
            candidates = new int[]{0, 180};
        } else {
            candidates = new int[]{90, 270};
        }
        List<AxisMap> hit = new ArrayList<>();
        for (int r : candidates) {
            AxisMap m = new AxisMap(r, false, false, rawMaxX, rawMaxY, screenW, screenH);
            int[] s = m.rawToScreen(rawX, rawY);
            if (inStartZone(s[0], s[1], screenW, screenH)) hit.add(m);
        }
        if (hit.size() == 1) return new Pick(hit.get(0), null);
        if (hit.isEmpty()) {
            return new Pick(null, "这一点换算后不落在右下角（开始按钮区）⇒ 不是映射问题就是点错了");
        }
        return new Pick(null, "这一点区分不出旋转（" + hit.size()
                + " 个候选都落在右下角）⇒ 换靠近角落的位置再点一次");
    }

    /**
     * 屏幕像素 → raw（{@link #rawToScreen} 的逆）。
     *
     * ★ 存在它的唯一理由：标定完必须**反算一次再注入**，两边对得上才算定死
     *   （见方案用例 #12）。没有逆函数就只能"看起来对"。
     */
    public int[] screenToRaw(int sx, int sy) {
        double ux = screenW <= 1 ? 0 : (double) sx / (screenW - 1);
        double uy = screenH <= 1 ? 0 : (double) sy / (screenH - 1);
        if (flipX) ux = 1.0 - ux;
        if (flipY) uy = 1.0 - uy;

        double nx;
        double ny;
        switch (rotation) {
            case 90:
                nx = 1.0 - uy;
                ny = ux;
                break;
            case 180:
                nx = 1.0 - ux;
                ny = 1.0 - uy;
                break;
            case 270:
                nx = uy;
                ny = 1.0 - ux;
                break;
            case 0:
            default:
                nx = ux;
                ny = uy;
                break;
        }
        int rx = (int) Math.round(nx * rawMaxX);
        int ry = (int) Math.round(ny * rawMaxY);
        return new int[]{clampInt(rx, 0, rawMaxX), clampInt(ry, 0, rawMaxY)};
    }

    /**
     * 用一个「已知屏幕点 ↔ 实测 raw」反解所有自洽的映射。
     *
     * @return 候选列表。**调用方必须要求 size()==1**：0 表示标定数据矛盾，
     *         &gt;1 表示这个标定点区分不出来（换一个离屏幕中心远的位置重标）。
     */
    public static List<AxisMap> solve(int rawX, int rawY, int screenX, int screenY,
                                      int rawMaxX, int rawMaxY, int screenW, int screenH,
                                      int tolPx) {
        List<AxisMap> candidates = new ArrayList<>();
        int[] rots = {0, 90, 180, 270};
        for (int r : rots) {
            for (int fx = 0; fx < 2; fx++) {
                for (int fy = 0; fy < 2; fy++) {
                    AxisMap m = new AxisMap(r, fx == 1, fy == 1,
                            rawMaxX, rawMaxY, screenW, screenH);
                    int[] s = m.rawToScreen(rawX, rawY);
                    if (Math.abs(s[0] - screenX) > tolPx) continue;
                    if (Math.abs(s[1] - screenY) > tolPx) continue;
                    candidates.add(m);
                }
            }
        }
        return dedupe(candidates);
    }

    /**
     * 候选去重：同一个映射可能被 (rotation, flip) 的不同组合表达出来
     * （例如 180+flipX 与 180+flipY 在**某一点**上恰好都成立）。
     * 用两个对角探针把它们分开 —— 只在一个点上比对，等于没比。
     */
    private static List<AxisMap> dedupe(List<AxisMap> in) {
        List<AxisMap> out = new ArrayList<>();
        for (AxisMap m : in) {
            boolean dup = false;
            for (AxisMap k : out) {
                if (sameMapping(k, m)) {
                    dup = true;
                    break;
                }
            }
            if (!dup) out.add(m);
        }
        return out;
    }

    /** 两个映射在三个探针上给出相同结果 ⇒ 视为同一个映射。 */
    private static boolean sameMapping(AxisMap a, AxisMap b) {
        int[][] probes = {{0, 0}, {a.rawMaxX, a.rawMaxY}, {a.rawMaxX, 0}};
        for (int[] p : probes) {
            int[] sa = a.rawToScreen(p[0], p[1]);
            int[] sb = b.rawToScreen(p[0], p[1]);
            if (sa[0] != sb[0] || sa[1] != sb[1]) return false;
        }
        return true;
    }

    public String describe() {
        return String.format(Locale.ROOT,
                "rotation=%d flip=%s/%s raw=0..%d,0..%d screen=%dx%d",
                rotation, flipX ? "X" : "-", flipY ? "Y" : "-",
                rawMaxX, rawMaxY, screenW, screenH);
    }

    private static double clamp01(int v, int max) {
        if (v <= 0) return 0.0;
        if (v >= max) return 1.0;
        return (double) v / max;
    }

    private static int clampInt(int v, int lo, int hi) {
        if (v < lo) return lo;
        if (v > hi) return hi;
        return v;
    }
}
