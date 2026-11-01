package io.pgauto.pulse;

import java.util.ArrayList;
import java.util.List;

/**
 * `ChirpGen` 的自测：**纯 PC 端可跑**（`javac` + `java`，不需要 Android、不需要真机）。
 *
 * <p>跑法（Git Bash，cwd `android/pulse`）：
 * <pre>
 *   JDK=D:/OpenJDK17_17.0.19_10
 *   "$JDK/bin/javac.exe" -encoding UTF-8 -d build/selftest \
 *     app/src/main/java/io/pgauto/pulse/ChirpGen.java \
 *     app/src/test/java/io/pgauto/pulse/ChirpSelfTest.java
 *   "$JDK/bin/java.exe" -cp build/selftest io.pgauto.pulse.ChirpSelfTest
 * </pre>
 *
 * <p>★ 关键设计：频率用**过零率**从波形本身估，不用 `phaseAt` 反算。
 * 若用 `phaseAt` 反算，等于「公式自己算自己」，把 F0/F1 写错也测不出来。
 */
public final class ChirpSelfTest {

    private static int pass = 0;
    private static final List<String> fails = new ArrayList<>();

    public static void main(String[] args) {
        short[] s = ChirpGen.mono();

        t01_length();
        t02_firstSampleZero(s);
        t03_signalStartsImmediately(s);
        t04_peak(s);
        t05_noClipping(s);
        t06_zeroCrossStart(s);
        t07_zeroCrossEnd(s);
        t08_zeroCrossMiddle(s);
        t09_directionMutation();
        t10_phaseMonotonic();
        t11_stereoLayout();
        t12_byteCount();
        t13_framesForRate();
        t14_stereoAt48k();
        t15_instFreqIndependent();
        t16_gapGeometry();
        t17_gapSilent();
        t18_segmentsIdentical();
        report();
    }

    // ── 几何 ────────────────────────────────────────────────────────────────

    private static void t01_length() {
        chk("t01 帧数 = 220500（5 s × 44100）", ChirpGen.FRAMES == 220500
                && ChirpGen.mono().length == 220500, "" + ChirpGen.FRAMES);
        chk("t01b byteCount = 帧数×4", ChirpGen.byteCount() == ChirpGen.FRAMES * 4,
                "" + ChirpGen.byteCount());
    }

    private static void t02_firstSampleZero(short[] s) {
        // sin(0) = 0 ⇒ 起点天然为 0，所以不需要淡入（淡入会让起点能量为 0、影响定位）
        chk("t02 首样本 = 0", s[0] == 0, "" + s[0]);
    }

    private static void t03_signalStartsImmediately(short[] s) {
        // 第二个样本必须非零：若代码里偷偷加了前置静音，这里会红
        chk("t03 第 2 个样本非零（无前置静音）", s[1] != 0, "" + s[1]);
        int firstNonZero = -1;
        for (int i = 0; i < 100; i++) if (s[i] != 0) { firstNonZero = i; break; }
        chk("t03b 首个非零样本在最前 100 内", firstNonZero >= 0 && firstNonZero < 100,
                "idx=" + firstNonZero);
    }

    private static void t04_peak(short[] s) {
        int mx = 0;
        for (short v : s) {
            int a = Math.abs(v);
            if (a > mx) mx = a;
        }
        chk("t04 峰值 ∈ [26000, 26214]", mx >= 26000 && mx <= 26214, "" + mx);
    }

    private static void t05_noClipping(short[] s) {
        int clip = 0;
        for (short v : s) if (v >= 32760 || v <= -32760) clip++;
        chk("t05 无削波样本", clip == 0, clip + " 个");
    }

    // ── 频率：过零率独立估（不用相位公式）──────────────────────────────────

    /**
     * 用窗口内的过零次数估平均频率：f ≈ (零交叉数 / 2) / 窗口时长。
     *
     * <p>与 {@link ChirpGen#phaseAt} 完全独立 ⇒ 能把「扫频方向写反」「频率常量写错」都测出来。
     */
    private static double estimateHz(short[] s, int from, int len) {
        int cross = 0;
        for (int i = from + 1; i < from + len && i < s.length; i++) {
            if ((s[i - 1] < 0 && s[i] >= 0) || (s[i - 1] >= 0 && s[i] < 0)) cross++;
        }
        return (cross / 2.0) / (len / (double) ChirpGen.RATE);
    }

    private static void t06_zeroCrossStart(short[] s) {
        // t=0..0.1 s：频率从 300 Hz 升到 414 Hz，均值 ≈ 357 Hz
        double f = estimateHz(s, 0, 4410);
        chk("t06 起始段估频 ≈ 300~430 Hz", f > 290 && f < 440, String.format("%.1f Hz", f));
    }

    private static void t07_zeroCrossEnd(short[] s) {
        // t=4.9..5.0 s：5886 → 6000 Hz，均值 ≈ 5943 Hz
        double f = estimateHz(s, ChirpGen.FRAMES - 4410, 4410);
        chk("t07 末尾段估频 ≈ 5700~6100 Hz", f > 5700 && f < 6100, String.format("%.1f Hz", f));
    }

    private static void t08_zeroCrossMiddle(short[] s) {
        // t=2.45..2.55 s：≈ 300 + 5700×2.5/5 = 3150 Hz
        int from = (int) (2.45 * ChirpGen.RATE);
        double f = estimateHz(s, from, 4410);
        chk("t08 中段估频 ≈ 3000~3300 Hz", f > 3000 && f < 3300, String.format("%.1f Hz", f));
    }

    private static void t09_directionMutation() {
        // ★ 变异：把 F0/F1 交换 ⇒ 起点应变高频、末尾变低频。
        //   若 t06/t07 只是"碰巧通过"，这一项会与它们同向 ⇒ 说明断言没在约束方向。
        short[] rev = ChirpGen.mono(ChirpGen.F1, ChirpGen.F0);
        double startRev = estimateHz(rev, 0, 4410);
        double endRev = estimateHz(rev, ChirpGen.FRAMES - 4410, 4410);
        chk("t09 反向扫频：起点高频、末尾低频",
                startRev > 5700 && endRev > 290 && endRev < 440,
                String.format("start=%.0f end=%.0f", startRev, endRev));
        chk("t09b 反向与正向的起点估频相差 > 5000 Hz",
                Math.abs(startRev - estimateHz(ChirpGen.mono(), 0, 4410)) > 5000,
                String.format("%.0f vs %.0f", startRev, estimateHz(ChirpGen.mono(), 0, 4410)));
    }

    // ── 其它 ────────────────────────────────────────────────────────────────

    private static void t10_phaseMonotonic() {
        // 瞬时频率恒 > 0 ⇒ 相位严格递增。抽样检查，避免 22 万次循环
        boolean ok = true;
        double prev = ChirpGen.phaseAt(0);
        for (int i = 1; i < ChirpGen.FRAMES; i += 997) {
            double cur = ChirpGen.phaseAt(i);
            if (cur <= prev) { ok = false; break; }
            prev = cur;
        }
        chk("t10 相位严格递增（抽样 222 点）", ok, "");
        chk("t10b 总相位 ≈ 2π×15750", Math.abs(ChirpGen.phaseAt(ChirpGen.FRAMES - 1)
                        - 2 * Math.PI * 15750.0) / (2 * Math.PI * 15750.0) < 1e-4,
                String.format("%.1f", ChirpGen.phaseAt(ChirpGen.FRAMES - 1)));
    }

    private static void t11_stereoLayout() {
        short[] m = ChirpGen.mono();
        short[] st = ChirpGen.stereo();
        boolean ok = st.length == m.length * 2;
        int bad = 0;
        for (int i = 0; i < m.length; i++) {
            if (st[i * 2] != m[i] || st[i * 2 + 1] != m[i]) bad++;
        }
        chk("t11 立体声长度 = 2×单声道", ok, st.length + " vs " + m.length);
        chk("t11b 左右完全相同", bad == 0, bad + " 处不同");
    }

    private static void t12_byteCount() {
        chk("t12 byteCount = 882000", ChirpGen.byteCount() == 882000, "" + ChirpGen.byteCount());
    }

    // ── 运行期采样率 / 多段（2026-10-19）────────────────────────────────────

    private static void t13_framesForRate() {
        chk("t13 framesFor(44100) = 220500", ChirpGen.framesFor(44100) == 220500,
                "" + ChirpGen.framesFor(44100));
        chk("t13b framesFor(48000) = 240000", ChirpGen.framesFor(48000) == 240000,
                "" + ChirpGen.framesFor(48000));
        // ★ 5 s 必须是**真实时间**的 5 s，不随 rate 变。写死 220500 就会让 48k 下的
        //   扫频变成 4.59 s，而 PC 端仍按 5 s 生成模板 ⇒ 互相关直接崩。
        chk("t13c 两种 rate 的时长相等（各 5.000 s）",
                Math.abs(ChirpGen.framesFor(44100) / 44100.0 - 5.0) < 1e-9
                        && Math.abs(ChirpGen.framesFor(48000) / 48000.0 - 5.0) < 1e-9, "");
    }

    private static void t14_stereoAt48k() {
        short[] a = ChirpGen.stereoAt(44100);
        short[] b = ChirpGen.stereoAt(48000);
        chk("t14 stereoAt(44100) 长度 = 441000", a.length == 441000, "" + a.length);
        chk("t14b stereoAt(48000) 长度 = 480000", b.length == 480000, "" + b.length);
        chk("t14c 48k 首样本 = 0（起点仍天然连续）", b[0] == 0, "" + b[0]);
        // ★ 注意下标：stereo 是交织的，第 1 个**样本**是 b[2]，b[1] 是第 0 帧的右声道
        chk("t14d 48k 第 1 帧的左样本非零", b[2] != 0, "" + b[2]);
        chk("t14d2 第 0 帧左右都为 0", b[0] == 0 && b[1] == 0, b[0] + "," + b[1]);
        int bad = 0;
        for (int i = 0; i < b.length / 2; i++) {
            if (b[i * 2] != b[i * 2 + 1]) bad++;
        }
        chk("t14e 48k 左右完全相同", bad == 0, bad + " 处不同");
    }

    /**
     * ★ 用**过零间隔**独立估瞬时频率 —— 不调用 `phaseAtRate`，所以能抓到
     * 「t 用了错误 rate」这类 bug。若实现把 `t = i / 44100` 写死，48k 数组在
     * 1.0 s 处的过零间隔会对应 1440×48/44.1 Hz 而不是 1440 Hz。
     */
    private static void t15_instFreqIndependent() {
        for (int rate : new int[]{44100, 48000}) {
            short[] st = ChirpGen.stereoAt(rate);
            // 理论：f(t) = F0 + (F1−F0)·t/T
            double[][] points = {{1.0, 300.0 + 5700.0 * 1.0 / 5.0},
                                 {2.5, 300.0 + 5700.0 * 2.5 / 5.0},
                                 {4.5, 300.0 + 5700.0 * 4.5 / 5.0}};
            for (double[] pt : points) {
                int c = (int) Math.round(pt[0] * rate);
                double f = zeroCrossFreq(st, c - rate / 50, c + rate / 50, rate);
                double err = Math.abs(f - pt[1]) / pt[1];
                chk("t15 rate=" + rate + " t=" + pt[0] + "s 实测 " + Math.round(f)
                                + " Hz vs 理论 " + Math.round(pt[1]) + " Hz",
                        err < 0.02, String.format("%.2f%%", err * 100));
            }
        }
    }

    /** 用区间内过零次数估平均频率：过零数/2 = 周期数。 */
    private static double zeroCrossFreq(short[] st, int from, int to, int rate) {
        from = Math.max(1, from);
        to = Math.min(st.length / 2 - 1, to);
        int cross = 0;
        for (int i = from; i <= to; i++) {
            int a = st[(i - 1) * 2];
            int b = st[i * 2];
            if ((a < 0 && b >= 0) || (a >= 0 && b < 0)) cross++;
        }
        double secs = (to - from + 1) / (double) rate;
        return cross / 2.0 / secs;
    }

    private static void t16_gapGeometry() {
        int rate = 48000;
        int[] starts = new int[2];
        short[] pcm = ChirpGen.stereoWithGap(rate, 2, 2.0, starts);
        int seg = ChirpGen.framesFor(rate);      // 240000
        int gap = ChirpGen.gapFrames(rate, 2.0); // 96000
        chk("t16 总帧 = 2 段 + 1 间隔", pcm.length / 2 == 2 * seg + gap,
                (pcm.length / 2) + " vs " + (2 * seg + gap));
        chk("t16b starts = [0, seg+gap]", starts[0] == 0 && starts[1] == seg + gap,
                starts[0] + "," + starts[1]);
        chk("t16c 间隔帧数 = 96000（2.0 s × 48000）", gap == 96000, "" + gap);
    }

    private static void t17_gapSilent() {
        int rate = 44100;
        short[] pcm = ChirpGen.stereoWithGap(rate, 2, 1.5, new int[2]);
        int seg = ChirpGen.framesFor(rate);
        int gap = ChirpGen.gapFrames(rate, 1.5);
        int nz = 0;
        for (int i = seg; i < seg + gap; i++) {
            if (pcm[i * 2] != 0 || pcm[i * 2 + 1] != 0) nz++;
        }
        chk("t17 段间静音区全 0", nz == 0, nz + " 个非零样本");
        // 反向：静音区**必须**存在，且两端都是扫频（否则「全 0」也可能是整段静音）
        chk("t17b 第 1 段末样本非零", pcm[(seg - 1) * 2] != 0, "" + pcm[(seg - 1) * 2]);
        chk("t17c 第 2 段首样本 = 0（sin(0)）", pcm[seg * 2 + gap * 2] == 0,
                "" + pcm[seg * 2 + gap * 2]);
    }

    private static void t18_segmentsIdentical() {
        int rate = 48000;
        int[] starts = new int[2];
        short[] pcm = ChirpGen.stereoWithGap(rate, 2, 0.5, starts);
        int seg = ChirpGen.framesFor(rate);
        int diff = 0;
        for (int i = 0; i < seg; i++) {
            if (pcm[i * 2] != pcm[(starts[1] + i) * 2]) diff++;
        }
        chk("t18 两段波形逐样本相同", diff == 0, diff + " 处不同");
    }

    // ── 工具 ────────────────────────────────────────────────────────────────

    private static void chk(String name, boolean ok, String detail) {
        if (ok) pass++;
        else fails.add(name + "  [" + detail + "]");
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

    private ChirpSelfTest() {
    }
}
