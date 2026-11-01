package io.pgauto.play;

import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * 从 `getevent -p` 的输出里挑出**触摸屏**设备，拿到它的路径与坐标量程。
 *
 * <h3>为什么不能写死 `/dev/input/event5`</h3>
 * `eventN` 是内核枚举顺序，换机、插拔外设、重启都可能变。本机 2026-10-31 实测
 * （`out/getevent_p_2026-10-31.txt`，8 个设备）触摸屏是 `event5`，
 * 但同型号另一台未必；写死会让"学习落点"在别人机器上读到一个不存在/不相干的设备。
 *
 * <h3>选择判据（三条同时成立）</h3>
 * <ol>
 *   <li>有 {@code ABS_MT_POSITION_X}（0x35）**且** {@code ABS_MT_POSITION_Y}（0x36）；</li>
 *   <li>量程 &gt; 0（拿不到量程就没法做坐标换算）；</li>
 *   <li>带 {@code INPUT_PROP_DIRECT}（直接触摸屏，而不是触摸板/鼠标）。</li>
 * </ol>
 * 命中 0 个 ⇒ 抛（本机不支持）；命中 &gt;1 个 ⇒ **抛并列出候选**（不许"取第一个"，
 * 那等于把歧义当答案 —— 本项目在 `AxisMap.solve` 里也定了同一条规矩）。
 *
 * <h3>纯 Java</h3>
 * `selftest.sh` 用**没有 android.jar** 的 classpath 编它 ⇒ 只 import java.*，
 * 且**不读文件**（真样本以字符串常量嵌进自测，避免判据依赖 I/O）。
 */
public final class TouchDevices {

    /** 一个 input 设备。只有 MT 触摸屏才会被 {@link #pick} 返回。 */
    public static final class Dev {
        public final String path;
        public final String name;
        public final int maxX;
        public final int maxY;
        public final boolean direct;

        Dev(String path, String name, int maxX, int maxY, boolean direct) {
            this.path = path;
            this.name = name;
            this.maxX = maxX;
            this.maxY = maxY;
            this.direct = direct;
        }

        public boolean isTouchscreen() {
            return maxX > 0 && maxY > 0;
        }

        @Override
        public String toString() {
            return String.format(Locale.ROOT, "%s(%s) 0..%d x 0..%d%s",
                    path, name, maxX, maxY, direct ? " DIRECT" : "");
        }
    }

    /** 匹配 `0035  : value 0, min 0, max 719, fuzz 0, flat 0, resolution 0`。 */
    private static Pattern absMax(String code) {
        return Pattern.compile("\\b" + code
                + "\\s*:\\s*value\\s+(-?\\d+),\\s*min\\s+(-?\\d+),\\s*max\\s+(-?\\d+)");
    }

    private static final Pattern DEV_LINE = Pattern.compile("^add device \\d+: (\\S+)\\s*$");
    private static final Pattern NAME_LINE = Pattern.compile("^\\s*name:\\s+\"([^\"]*)\"");

    private static final Pattern AX = absMax("0035");
    private static final Pattern AY = absMax("0036");

    private TouchDevices() {
    }

    /** 解析全部设备（含非触摸设备，便于日志里看清楚挑中谁、为什么不挑别人）。 */
    public static List<Dev> parse(String text) {
        List<Dev> out = new ArrayList<>();
        if (text == null || text.isEmpty()) return out;

        String[] lines = text.split("\r?\n");
        // 按 "add device N: /path" 切块
        String path = null;
        StringBuilder body = new StringBuilder();
        for (int i = 0; i <= lines.length; i++) {
            String line = i < lines.length ? lines[i] : null;
            Matcher md = line == null ? null : DEV_LINE.matcher(line.trim());
            boolean newDev = md != null && md.matches();
            if (newDev || i == lines.length) {
                if (path != null) out.add(build(path, body.toString()));
                path = newDev ? md.group(1) : null;
                body.setLength(0);
            }
            if (line != null && !newDev) body.append(line).append('\n');
        }
        return out;
    }

    private static Dev build(String path, String body) {
        String name = "?";
        Matcher mn = NAME_LINE.matcher(body);
        while (mn.find()) {          // 取第一个 name: 行
            name = mn.group(1);
            break;
        }
        int[] ax = maxOf(AX, body);
        int[] ay = maxOf(AY, body);
        boolean direct = body.contains("INPUT_PROP_DIRECT");
        // 量程可能是负数（少见），统一取 max 且要求 >0 才算可用
        int mx = ax == null ? 0 : ax[2];
        int my = ay == null ? 0 : ay[2];
        return new Dev(path, name, mx, my, direct);
    }

    /** 返回 {value, min, max}；没找到返回 null。 */
    private static int[] maxOf(Pattern p, String body) {
        Matcher m = p.matcher(body);
        if (!m.find()) return null;
        return new int[]{Integer.parseInt(m.group(1)),
                Integer.parseInt(m.group(2)), Integer.parseInt(m.group(3))};
    }

    /**
     * 挑出唯一的触摸屏。
     *
     * @throws IllegalStateException 0 个（没有触摸屏）或 &gt;1 个（歧义，列出候选让人看日志）
     */
    public static Dev pick(String text) {
        List<Dev> all = parse(text);
        List<Dev> touch = new ArrayList<>();
        for (Dev d : all) {
            if (d.isTouchscreen()) touch.add(d);
        }
        if (touch.isEmpty()) {
            throw new IllegalStateException("没找到支持多点触摸的设备（需要 ABS_MT_POSITION_X/Y 且量程>0）"
                    + "；共解析到 " + all.size() + " 个设备");
        }
        // 优先直接触摸屏（排除触摸板）
        List<Dev> direct = new ArrayList<>();
        for (Dev d : touch) {
            if (d.direct) direct.add(d);
        }
        List<Dev> use = direct.isEmpty() ? touch : direct;
        if (use.size() != 1) {
            StringBuilder b = new StringBuilder("触摸设备不唯一（" + use.size() + " 个候选），"
                    + "不允许取第一个（会把别的设备坐标当成落点）：");
            for (Dev d : use) b.append("\n  - ").append(d);
            throw new IllegalStateException(b.toString());
        }
        return use.get(0);
    }
}
