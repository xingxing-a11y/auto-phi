package io.pgauto.probe;

import java.util.ArrayList;
import java.util.List;

/**
 * `PulseGen` / `PulseLines` 的自测：**纯 PC 端可跑**（`javac` + `java`，不需要 Android、不需要真机）。
 *
 * <p>跑法（Git Bash，cwd `android/probe`）：
 * <pre>
 *   JDK=D:/OpenJDK17_17.0.19_10
 *   "$JDK/bin/javac.exe" -encoding UTF-8 -d build/selftest \
 *     app/src/main/java/io/pgauto/probe/PulseGen.java \
 *     app/src/main/java/io/pgauto/probe/PulseLines.java \
 *     app/src/test/java/io/pgauto/probe/PulseSelfTest.java
 *   "$JDK/bin/java.exe" -cp build/selftest io.pgauto.probe.PulseSelfTest
 * </pre>
 *
 * <p>★ 设计原则（项目规矩：自测要能抓到真 bug、必须能区分两种实现）：
 * <ol>
 *   <li>采样值用**独立算出来的参考数**硬编码（Python 侧用同一公式算，见提交说明），
 *       而不是「用实现算一遍再比一遍」—— 后者恒真，永远抓不到频率写错。</li>
 *   <li>有三项是**变异测试**：把参数挪 1，断言必须能察觉。</li>
 *   <li>P 行的字段数逐个断言 —— 少一个制表符在真机上不报错，只让 PC 端算出「延迟 47 s」。</li>
 * </ol>
 */
public final class PulseSelfTest {

    private static int pass = 0;
    private static final List<String> fails = new ArrayList<>();

    public static void main(String[] args) {
        t01_offsetsFollowFormula();
        t02_parametrizedOffset();
        t03_gapMutation();
        t04_centerInsidePulse();
        t05_centersAscending();
        t06_outOfRange();
        t07_silenceOutsidePulses();
        t08_nonZeroCount();
        t09_peak();
        t10_sampleAgainstReference();
        t11_frequencyMutation();
        t12_stereoLayout();
        t13_byteCount();
        t14_endFrames();
        t15_lastPulseFits();
        t16_leadSilence();
        t17_gapAboveTimestampGranularity();
        t18_startLineFields();
        t19_tickLineFields();
        t20_tickLineFailure();
        t21_endLineFields();
        t22_kindIsSecondField();
        t23_timestampParsable();
        report();
    }

    // ── 波形几何 ────────────────────────────────────────────────────────────

    private static void t01_offsetsFollowFormula() {
        boolean ok = true;
        StringBuilder sb = new StringBuilder();
        for (int k = 0; k < PulseGen.COUNT; k++) {
            int want = PulseGen.FIRST_OFF + k * PulseGen.GAP;
            int got = PulseGen.startOf(k);
            sb.append(k).append(':').append(got).append(' ');
            if (got != want) ok = false;
        }
        chk("t01 起点 = FIRST_OFF + k*GAP", ok, sb.toString().trim());
        chk("t01b 起点序列", PulseGen.startOf(0) == 22050
                        && PulseGen.startOf(4) == 39690,
                PulseGen.startOf(0) + "/" + PulseGen.startOf(4));
    }

    private static void t02_parametrizedOffset() {
        chk("t02 参数化 startOf(2,100,50) = 200",
                PulseGen.startOf(2, 100, 50) == 200, "" + PulseGen.startOf(2, 100, 50));
    }

    private static void t03_gapMutation() {
        // ★ 变异：gap 挪 1 帧，结果必须跟着变。若公式没用到 gap，这里两者会相等 ⇒ 红。
        int a = PulseGen.startOf(2, 0, 4410);
        int b = PulseGen.startOf(2, 0, 4411);
        chk("t03 gap 4410 vs 4411 必须不同", a != b && b - a == 2, a + " vs " + b);
    }

    private static void t04_centerInsidePulse() {
        boolean ok = true;
        StringBuilder sb = new StringBuilder();
        for (int k = 0; k < PulseGen.COUNT; k++) {
            int c = PulseGen.centerOf(k);
            int s = PulseGen.startOf(k);
            sb.append(c).append(' ');
            if (c != s + PulseGen.PULSE_LEN / 2) ok = false;
            if (c < s || c >= s + PulseGen.PULSE_LEN) ok = false;
        }
        chk("t04 中心 = 起点+42 且落在脉冲内", ok, sb.toString().trim());
    }

    private static void t05_centersAscending() {
        int[] c = PulseGen.centers();
        boolean ok = c.length == PulseGen.COUNT;
        for (int i = 1; i < c.length; i++) if (c[i] <= c[i - 1]) ok = false;
        chk("t05 centers 升序且长度 = 5", ok,
                c.length + " 个，首 " + c[0] + " 末 " + c[c.length - 1]);
    }

    private static void t06_outOfRange() {
        chk("t06 k=-1 抛 IAE", throwsIae(() -> PulseGen.startOf(-1)), "未抛");
        chk("t06b k=COUNT 抛 IAE", throwsIae(() -> PulseGen.startOf(PulseGen.COUNT)), "未抛");
    }

    private static void t07_silenceOutsidePulses() {
        short[] s = PulseGen.mono();
        int bad = 0;
        for (int i = 0; i < s.length; i++) {
            boolean inPulse = false;
            for (int k = 0; k < PulseGen.COUNT; k++) {
                int st = PulseGen.startOf(k);
                if (i >= st && i < st + PulseGen.PULSE_LEN) {
                    inPulse = true;
                    break;
                }
            }
            if (!inPulse && s[i] != 0) bad++;
        }
        chk("t07 脉冲之外严格为 0", bad == 0, bad + " 处非零");
    }

    private static void t08_nonZeroCount() {
        short[] s = PulseGen.mono();
        int total = 0;
        for (short v : s) if (v != 0) total++;
        // 独立算出来：84 样本里有 4 个零点（i=0/21/42/63，都是 sin 的整数半周期端点）
        // ⇒ 每脉冲非零 80，5 个脉冲共 400
        chk("t08 全串非零样本 = 400（每脉冲 80）", total == 400, "" + total);

        // 逐个脉冲核一遍，避免两个脉冲互相抵消恰好凑出 400
        boolean ok = true;
        StringBuilder sb = new StringBuilder();
        for (int k = 0; k < PulseGen.COUNT; k++) {
            int st = PulseGen.startOf(k), n = 0;
            for (int i = 0; i < PulseGen.PULSE_LEN; i++) if (s[st + i] != 0) n++;
            sb.append(n).append(' ');
            if (n != 80) ok = false;
        }
        chk("t08b 每个脉冲都是 80", ok, sb.toString().trim());
    }

    private static void t09_peak() {
        short[] s = PulseGen.mono();
        int mx = 0;
        for (short v : s) {
            int a = Math.abs(v);
            if (a > mx) mx = a;
        }
        // 参考值 26141（= round(26214 × sin(2π×1050×10/44100))），不是满幅 26214
        chk("t09 峰值 = 26141", mx == 26141, "" + mx);
    }

    private static void t10_sampleAgainstReference() {
        short[] s = PulseGen.mono();
        int st = PulseGen.startOf(0);
        int[] idx = {0, 1, 5, 10, 11, 21, 31, 42, 52, 63, 83};
        int[] ref = {0, 3907, 17830, 26141, 26141, 0, -26141, 0, 26141, 0, -3907};
        boolean ok = true;
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < idx.length; i++) {
            int got = s[st + idx[i]];
            sb.append(idx[i]).append('=').append(got).append(' ');
            if (got != ref[i]) ok = false;
        }
        chk("t10 与独立参考值逐点对拍（11 点）", ok, sb.toString().trim());
    }

    private static void t11_frequencyMutation() {
        // ★ 变异：若频率写成 1000 Hz，第 10 样本应为 round(26214*sin(2π*1000*10/44100)) = 26095，
        //   比参考值 26141 小 46 ⇒ 差异可见。这里断言「两者确实不同」，
        //   证明 t10 的参考值真的在约束频率，而不是随便一个正弦都能过。
        int at1050 = PulseGen.mono()[PulseGen.startOf(0) + 10];
        int at1000 = (int) Math.round(PulseGen.AMP
                * Math.sin(2 * Math.PI * 1000 * 10.0 / PulseGen.RATE));
        chk("t11 1050 Hz 与 1000 Hz 的第 10 样本必须不同", at1050 != at1000,
                at1050 + " vs " + at1000);
    }

    private static void t12_stereoLayout() {
        short[] m = PulseGen.mono();
        short[] st = PulseGen.stereo();
        boolean ok = st.length == m.length * 2;
        int bad = 0;
        for (int i = 0; i < m.length; i++) {
            if (st[i * 2] != m[i] || st[i * 2 + 1] != m[i]) bad++;
        }
        chk("t12 立体声长度 = 2×单声道", ok, st.length + " vs " + m.length);
        chk("t12b 左右完全相同", bad == 0, bad + " 处不同");
    }

    private static void t13_byteCount() {
        chk("t13 byteCount = TOTAL×4", PulseGen.byteCount() == PulseGen.TOTAL * 4,
                "" + PulseGen.byteCount());
    }

    private static void t14_endFrames() {
        int want = PulseGen.startOf(PulseGen.COUNT - 1) + PulseGen.PULSE_LEN + PulseGen.GAP;
        chk("t14 endFrames = 末脉冲末 + 间隔", PulseGen.endFrames() == want,
                PulseGen.endFrames() + " vs " + want);
        chk("t14b endFrames < TOTAL（不必等整段）",
                PulseGen.endFrames() < PulseGen.TOTAL,
                PulseGen.endFrames() + " < " + PulseGen.TOTAL);
    }

    private static void t15_lastPulseFits() {
        int lastEnd = PulseGen.startOf(PulseGen.COUNT - 1) + PulseGen.PULSE_LEN;
        chk("t15 最后一个脉冲不越界", lastEnd <= PulseGen.TOTAL,
                lastEnd + " <= " + PulseGen.TOTAL);
    }

    private static void t16_leadSilence() {
        double s = PulseGen.FIRST_OFF / (double) PulseGen.RATE;
        chk("t16 前置静音 ≥ 0.4 s（播放启动瞬态）", s >= 0.4, String.format("%.3f s", s));
    }

    private static void t17_gapAboveTimestampGranularity() {
        // getTimestamp 的 framePosition 粒度是 1024 帧（实测）。间隔必须远大于它，
        // 否则相邻脉冲的播放时刻在时间戳上无法区分。
        chk("t17 间隔 > 1024 帧（时间戳粒度）", PulseGen.GAP > 1024,
                PulseGen.GAP + " 帧 = " + String.format("%.1f ms", PulseGen.GAP * 1000.0 / PulseGen.RATE));
    }

    // ── P 行格式 ────────────────────────────────────────────────────────────

    private static void t18_startLineFields() {
        // 7 参版本：实际音量 12、原始音量 0（= 本轮真机遇到的「媒体音量 0」情形）
        String ln = PulseLines.start(1792331808000000000L, 66150, 12, 15, 0, 3, 7);
        String[] p = ln.split("\t", -1);
        chk("t18 S 行字段数 = 8", p.length == 8, p.length + "：" + ln);
        chk("t18b S 行 2..6 字段可解析", parses(p, 2, 7), ln);
        chk("t18c S 行末字段是 kv 串（pre/wr/volPre）",
                p[7].contains("pre=") && p[7].contains("wr=") && p[7].contains("volPre="), p[7]);
        chk("t18d 7 参版本区分 used/orig",
                "12".equals(p[4]) && "15".equals(p[5]) && p[7].contains("volPre=0"), ln);
        // 6 参版本（兜底重载）必须把 volPre 记成与 vol 相同，不能留 0 占位
        String ln6 = PulseLines.start(1, 1, 9, 15, 0, 0);
        chk("t18e 6 参重载 vol0 = vol", ln6.split("\t", -1)[7].contains("volPre=9"), ln6);
    }

    private static void t19_tickLineFields() {
        String ln = PulseLines.tick(42, 1792331808000000000L, true, 12345, 65569500000000L);
        String[] p = ln.split("\t", -1);
        chk("t19 T 行字段数 = 7", p.length == 7, p.length + "：" + ln);
        chk("t19b T 行 2..6 字段可解析", parses(p, 2, 7), ln);
        chk("t19c hwOk = 1", "1".equals(p[4]), p[4]);
    }

    private static void t20_tickLineFailure() {
        String ln = PulseLines.tick(7, 1792331808000000000L, false, 999, 888);
        String[] p = ln.split("\t", -1);
        chk("t20 ok=false 时 frame/ns 写 -1（不留空字段）",
                "-1".equals(p[5]) && "-1".equals(p[6]), ln);
        chk("t20b 字段数仍是 7", p.length == 7, p.length + "：" + ln);
    }

    private static void t21_endLineFields() {
        String ln = PulseLines.end(44226, 1792331808000000000L, 200, 198, 12, 15);
        String[] p = ln.split("\t", -1);
        chk("t21 E 行字段数 = 6", p.length == 6, p.length + "：" + ln);
        chk("t21b E 行 2..4 字段可解析", parses(p, 2, 5), ln);
        chk("t21c E 行末字段含 tried/ok/vol",
                p[5].contains("tried=") && p[5].contains("ok=") && p[5].contains("vol="), p[5]);
    }

    private static void t22_kindIsSecondField() {
        String s = PulseLines.start(1, 1, 1, 1, 0, 0);
        String t = PulseLines.tick(1, 1, true, 1, 1);
        String e = PulseLines.end(1, 1, 1, 1, 1, 1);
        boolean ok = "P".equals(s.split("\t")[0]) && "S".equals(s.split("\t")[1])
                && "P".equals(t.split("\t")[0]) && "T".equals(t.split("\t")[1])
                && "P".equals(e.split("\t")[0]) && "E".equals(e.split("\t")[1]);
        chk("t22 首字段恒为 P，第二字段是 S/T/E", ok, "");
    }

    private static void t23_timestampParsable() {
        long base = 1792331808000000000L;
        long t0 = Long.parseLong(PulseLines.tick(1, base, true, 0, base).split("\t")[3]);
        long t1 = Long.parseLong(PulseLines.tick(2, base + 5_000_000L, true, 220, base)
                .split("\t")[3]);
        chk("t23 tReadPlay 可解析且递增（5 ms 轮询）",
                t1 - t0 == 5_000_000L, (t1 - t0) + " ns");
    }

    // ── 工具 ────────────────────────────────────────────────────────────────

    /**
     * 断言 p[from] ~ p[to-1] 全是可解析的整数。
     *
     * <p>★ 必须给 from：P 行的前两个字段是字面量 `P` / `S|T|E`，从 0 开始 parse
     * 一定会失败 —— 首版就是这么写的，三条断言全红，但代码是对的（错在断言）。
     */
    private static boolean parses(String[] p, int from, int to) {
        for (int i = from; i < to && i < p.length; i++) {
            try {
                Long.parseLong(p[i]);
            } catch (NumberFormatException ex) {
                return false;
            }
        }
        return to <= p.length;
    }

    private static boolean throwsIae(Runnable r) {
        try {
            r.run();
            return false;
        } catch (IllegalArgumentException ex) {
            return true;
        }
    }

    private static void chk(String name, boolean ok, String detail) {
        if (ok) {
            pass++;
        } else {
            fails.add(name + "  [" + detail + "]");
        }
    }

    private static void report() {
        if (fails.isEmpty()) {
            System.out.println(pass + " passed, 0 failed");
            return;
        }
        for (String f : fails) System.out.println("✗ " + f);
        System.out.println(pass + " passed, " + fails.size() + " failed");
        System.exit(1);
    }

    private PulseSelfTest() {
    }
}
