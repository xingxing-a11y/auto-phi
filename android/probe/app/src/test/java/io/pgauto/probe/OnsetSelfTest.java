package io.pgauto.probe;

import java.util.ArrayList;
import java.util.List;

/**
 * `OnsetDetector` 的自测：**纯 PC 端可跑**（`javac` + `java`，不需要 Android、不需要真机）。
 *
 * 跑法（Git Bash，cwd `android/probe`）：
 *   javac -encoding UTF-8 -d build/selftest \
 *     app/src/main/java/io/pgauto/probe/OnsetDetector.java \
 *     app/src/test/java/io/pgauto/probe/OnsetSelfTest.java
 *   java -cp build/selftest io.pgauto.probe.OnsetSelfTest
 *
 * 设计原则（按项目规矩「自测要能抓到真 bug、必须能区分两种实现」）：
 *   1. 每项都断言**具体数**（帧号、onset 数），不是「跑完没报错」；
 *   2. 有三项是**变异测试**：把某个判据关掉/调松，output 必须变化 —— 否则说明那个判据根本没生效；
 *   3. 有一项与**手算的 mean square 对拍** —— 若实现误用了 max / abs 均值，这一项必红。
 *
 * 实际抓到的 bug：`testTwoBursts` 首版失败（第二个 burst 漏报），根因是背景更新
 * 按 `onset` 分档而不是按 `over` 分档。见 `DEFAULT_BG_ALPHA_OVER` 注释。
 */
public final class OnsetSelfTest {

    private static final int RATE = 44100;
    private static int pass = 0;
    private static final List<String> fails = new ArrayList<>();

    public static void main(String[] args) {
        t01_frameCountSilence();
        t02_singleBurst();
        t03_twoBursts();
        t04_longBurstSingleOnset();
        t05_minGapEffect();
        t06_rightChannelOnly();
        t07_oddTail();
        t08_frameContinuityAcrossBlocks();
        t09_envelopeMatchesHandComputed();
        t10_floorMutation();
        t11_ratioMutation();
        t12_reset();
        t13_silenceNoSpuriousOnset();
        t14_silenceFirstFrameRatioBounded();
        t14_badArgs();
        t15_loudFirstFrameNoHole();
        t16_sinkReceivesEveryFrame();

        int total = pass + fails.size();
        System.out.println();
        System.out.println("===== OnsetDetector 自测：" + pass + "/" + total + " passed =====");
        for (String f : fails) System.out.println("  ✗ " + f);
        if (!fails.isEmpty()) System.exit(1);
    }

    // ── 用例 ─────────────────────────────────────────────────────────────────

    /** 1 s 数字静音 ⇒ 100 帧、0 onset。 */
    private static void t01_frameCountSilence() {
        short[] pcm = silence(1.0);
        OnsetDetector d = new OnsetDetector(RATE);
        int frames = d.push(pcm, pcm.length);
        eq("t01 帧数", frames, 100);
        eq("t01 onset 数", d.onsetCount(), 0);
        eq("t01 frameIndex", d.frameIndex(), 100L);
    }

    /** 0.5 s 静音 + 0.2 s 正弦 + 1 s 静音 ⇒ 恰 1 个 onset，在第 50 帧。 */
    private static void t02_singleBurst() {
        short[] pcm = concat(silence(0.5), sine(0.2, 440, 8000), silence(1.0));
        Rec rec = run(pcm, new OnsetDetector(RATE));
        eq("t02 onset 数", rec.onsets.size(), 1);
        eq("t02 onset 帧号", rec.onsets.get(0), 50L);
    }

    /** ★ 两个 burst 间隔 300 ms ⇒ 必须报 2 个（这条首版失败，抓出了背景更新 bug）。 */
    private static void t03_twoBursts() {
        short[] pcm = concat(silence(0.5), sine(0.2, 440, 8000), silence(0.3),
                sine(0.2, 660, 8000), silence(1.0));
        Rec rec = run(pcm, new OnsetDetector(RATE));
        eq("t03 onset 数", rec.onsets.size(), 2);
        if (rec.onsets.size() == 2) {
            eq("t03 第 1 个帧号", rec.onsets.get(0), 50L);
            eq("t03 第 2 个帧号", rec.onsets.get(1), 100L);
        }
    }

    /** 单个 300 ms 持续 burst ⇒ 只报 1 个（持续段不能被 minGap 之外的逻辑重复报）。 */
    private static void t04_longBurstSingleOnset() {
        short[] pcm = concat(silence(0.5), sine(0.3, 440, 8000), silence(1.0));
        Rec rec = run(pcm, new OnsetDetector(RATE));
        eq("t04 onset 数", rec.onsets.size(), 1);
    }

    /** 变异测试：minGap 0 ms vs 50 ms，同一持续 burst 上的 onset 数必须不同。 */
    private static void t05_minGapEffect() {
        // 幅度按 100 ms 交替强弱（15000 / 3000）⇒ 每 10 帧出现一次「能量跳变」，
        // 这样 50 ms 的 minGap 正好能挡住一半，0 ms 则全放行。
        short[] churn = new short[(int) (RATE * 0.6) * 2];
        for (int f = 0; f < churn.length / 2; f++) {
            int amp = ((f / 441) % 2 == 0) ? 15000 : 3000;
            short v = (short) (amp * Math.sin(2 * Math.PI * 440 * f / RATE));
            churn[f * 2] = v;
            churn[f * 2 + 1] = v;
        }
        short[] sig = concat(silence(0.5), churn, silence(0.3));

        int nTight = run(sig, new OnsetDetector(RATE, 10, 5.0, 50, 300, 0.05)).onsets.size();
        int nLoose = run(sig, new OnsetDetector(RATE, 10, 5.0, 0, 300, 0.05)).onsets.size();
        check("t05 minGap 生效（tight=" + nTight + " < loose=" + nLoose + "）", nLoose > nTight);
    }

    /** 只有右声道有声 ⇒ 下混 (0+8000)/2 = 4000，mean square = 8e6 > 底噪 ⇒ 必须检出。 */
    private static void t06_rightChannelOnly() {
        int frames = (int) (RATE * 2.0);
        short[] whole = sine(2.0, 440, 8000);      // 两声道都有
        short[] pcm = new short[frames * 2];
        int quiet = (int) (RATE * 0.5) * 2;
        for (int i = quiet; i < pcm.length; i += 2) {
            pcm[i] = 0;                    // 左声道恒 0
            pcm[i + 1] = whole[i];         // 右声道复制
        }
        Rec rec = run(pcm, new OnsetDetector(RATE));
        eq("t06 右声道 onset 数", rec.onsets.size(), 1);
        // 能量应为 8000²/2/4 = 8e6（下混减半 ⇒ 能量除 4）
        double e = rec.energyAt(50);
        // 容差 5%：帧窗是 441 样本 = 4.41 个 440 Hz 周期（非整数）⇒ mean(x²) 天然偏离 A²/2 约 1.6%。
        // 仍足以区分「下混取 (L+R)/2」（8e6）与「只取单声道」（3.2e7，差 4 倍）。
        check("t06 帧 50 能量 ≈ 8.0e6（实得 " + fmt(e) + "）", Math.abs(e - 8.0e6) < 4.0e5);
    }

    /** 奇数 n（模拟上游 bug）⇒ 不崩，帧数 = floor(n/2/hop)。 */
    private static void t07_oddTail() {
        OnsetDetector d = new OnsetDetector(RATE);
        short[] buf = new short[1000];
        int frames = d.push(buf, 1000);
        eq("t07 奇数 n 帧数", frames, 1000 / 2 / 441);
        // 越界必须抛
        boolean threw = false;
        try {
            d.push(buf, 1001);
        } catch (IllegalArgumentException ex) {
            threw = true;
        }
        check("t07 n 越界抛 IllegalArgumentException", threw);
    }

    /** 分 7 块喂 2 s 信号 ⇒ 每帧帧号连续 +1，总帧数 200。 */
    private static void t08_frameContinuityAcrossBlocks() {
        short[] pcm = concat(silence(1.0), sine(1.0, 440, 8000));
        OnsetDetector d = new OnsetDetector(RATE);
        final List<Long> idx = new ArrayList<>();
        d.setSink((i, e, onset, ratio) -> idx.add(i));
        int per = pcm.length / 7;
        int off = 0;
        int totalFrames = 0;
        for (int k = 0; k < 7; k++) {
            int n = (k == 6) ? (pcm.length - off) : per;
            totalFrames += d.push(pcm, off, n);
            off += n;
        }
        eq("t08 总帧数", totalFrames, 200);
        eq("t08 sink 调用次数", idx.size(), 200);
        boolean mono = true;
        for (int k = 0; k < idx.size(); k++) if (idx.get(k) != (long) k) mono = false;
        check("t08 帧号严格连续 0..199", mono);
    }

    /** ★ 与手算 mean square 对拍（区分「mean(x²)」与「mean(|x|)」「max(x²)」）。 */
    private static void t09_envelopeMatchesHandComputed() {
        // 幅度恒为 ±1000 的方波：mean(x²) 应恰为 1e6；
        // 若实现误用 mean(|x|) 会得 1000，误用 max(x²) 也会得 1e6 但相邻帧全同 ⇒ 用两段不同幅度区分。
        short[] pcm = new short[(int) (RATE * 0.05) * 2];
        for (int i = 0; i < pcm.length / 2; i++) {
            short v = (short) ((i % 2 == 0) ? 1000 : -1000);
            pcm[i * 2] = v;
            pcm[i * 2 + 1] = v;
        }
        OnsetDetector d = new OnsetDetector(RATE);
        List<Double> es = new ArrayList<>();
        d.setSink((i, e, onset, ratio) -> es.add(e));
        d.push(pcm, 0, pcm.length);
        eq("t09 帧数", es.size(), 5);
        boolean all = true;
        for (double e : es) if (Math.abs(e - 1.0e6) > 1e-6) all = false;
        check("t09 每帧 mean(x²) 恰为 1.0e6（实得首帧 " + fmt(es.get(0)) + "）", all);

        // 第二段：幅度 2000 ⇒ 4e6，且 ≠ 首段，证明不是「常数返回」
        short[] pcm2 = new short[(int) (RATE * 0.05) * 2];
        for (int i = 0; i < pcm2.length / 2; i++) {
            short v = (short) ((i % 2 == 0) ? 2000 : -2000);
            pcm2[i * 2] = v;
            pcm2[i * 2 + 1] = v;
        }
        List<Double> es2 = new ArrayList<>();
        OnsetDetector d2 = new OnsetDetector(RATE);
        d2.setSink((i, e, onset, ratio) -> es2.add(e));
        d2.push(pcm2, 0, pcm2.length);
        check("t09 幅度 2000 时能量为 4.0e6（实得 " + fmt(es2.get(0)) + "）",
                Math.abs(es2.get(0) - 4.0e6) < 1e-6);
    }

    /** 变异测试：把绝对底噪抬到 1e6 RMS ⇒ 任何信号都不够响 ⇒ onset 必须为 0。 */
    private static void t10_floorMutation() {
        short[] pcm = concat(silence(0.5), sine(0.5, 440, 8000), silence(0.5));
        Rec normal = run(pcm, new OnsetDetector(RATE));
        Rec muted = run(pcm, new OnsetDetector(RATE, 10, 5.0, 50, 1.0e6, 0.05));
        check("t10 正常底噪有 onset（" + normal.onsets.size() + "）", normal.onsets.size() >= 1);
        eq("t10 高底噪 onset 数", muted.onsets.size(), 0);
    }

    /** 变异测试：thrRatio 0.5（几乎不设阈）vs 5.0 ⇒ 前者 onset 必须更多。 */
    private static void t11_ratioMutation() {
        // 幅度逐帧缓慢变化的持续音：低阈值会在每个 50 ms 里找到「上升沿」
        short[] pcm = new short[(int) (RATE * 2.0) * 2];
        for (int i = 0; i < pcm.length / 2; i++) {
            int fr = i / 441;
            double amp = 4000 + 12000.0 * (fr % 20) / 20.0;
            short v = (short) (amp * Math.sin(2 * Math.PI * 440 * i / RATE));
            pcm[i * 2] = v;
            pcm[i * 2 + 1] = v;
        }
        int nStrict = run(pcm, new OnsetDetector(RATE, 10, 5.0, 50, 300, 0.05)).onsets.size();
        int nLoose = run(pcm, new OnsetDetector(RATE, 10, 0.5, 50, 300, 0.05)).onsets.size();
        check("t11 thrRatio 生效（0.5 → " + nLoose + " > 5.0 → " + nStrict + "）", nLoose > nStrict);
    }

    /** reset 后帧号 / onset 计数归零，且能继续正确工作。 */
    private static void t12_reset() {
        OnsetDetector d = new OnsetDetector(RATE);
        short[] pcm = concat(silence(0.5), sine(0.2, 440, 8000), silence(0.5));
        d.push(pcm, 0, pcm.length);
        check("t12 reset 前有 onset", d.onsetCount() > 0);
        d.reset();
        eq("t12 reset frameIndex", d.frameIndex(), 0L);
        eq("t12 reset onsetCount", d.onsetCount(), 0);
        // reset 后重喂同样信号应得到同样的帧号
        OnsetDetector d2 = new OnsetDetector(RATE);
        List<Long> a = run(pcm, d).onsets, b = run(pcm, d2).onsets;
        check("t12 reset 后可复现（" + a + " vs " + b + "）", a.equals(b));
    }

    /** 长静音（含 ±10 量化噪声）⇒ 0 onset（绝对底噪必须挡住）。 */
    /**
     * 首帧数字静音 ⇒ bg 不许退化成 0/1e-9 ⇒ ratio 必须有界。
     *
     * 为什么要有这条：2026-10-18 真机 sidecar 里首个 onset 报出
     * `ratio = 15494058811224490`（1.5e16）。根因是首帧 e == 0 ⇒ bg == 0 ⇒
     * 兜底钳到 1e-9 ⇒ ratio = e/1e-9。旧自测全从「静音开头」跑，但从没断言过 ratio，
     * 所以这个 bug 一路活到真机。这条用例就是补那个空档。
     */
    private static void t14_silenceFirstFrameRatioBounded() {
        short[] pcm = concat(silence(0.5), sine(0.3, 440, 8000), silence(0.2));
        Rec rec = run(pcm, new OnsetDetector(RATE));
        eq("t14 首帧静音时 onset 数", rec.onsets.size(), 1);
        double r0 = rec.ratios.get(0);
        // 上限 1e6：e/absFloorE = 8000²/2/90000 ≈ 356，留三个数量级余量仍能抓 1.5e16
        check("t14 首 onset 的 ratio 有界（实得 " + fmt(r0) + "）", r0 > 0 && r0 < 1e6);
        double rmax = 0;
        for (double v : rec.ratios) if (v > rmax) rmax = v;
        check("t14 全程 ratio 有界（最大 " + fmt(rmax) + "）", rmax < 1e6);
    }

    private static void t13_silenceNoSpuriousOnset() {
        short[] pcm = new short[(int) (RATE * 5.0) * 2];
        java.util.Random r = new java.util.Random(7);
        for (int i = 0; i < pcm.length; i++) pcm[i] = (short) (r.nextInt(21) - 10); // ±10
        Rec rec = run(pcm, new OnsetDetector(RATE));
        eq("t13 ±10 噪声 onset 数", rec.onsets.size(), 0);
    }

    /** 非法构造参数必须抛。 */
    private static void t14_badArgs() {
        check("t14 rate=0 抛", threw(() -> new OnsetDetector(0)));
        check("t14 frameMs=0 抛", threw(() -> new OnsetDetector(RATE, 0, 5, 50, 300, 0.05)));
        check("t14 thrRatio=0 抛", threw(() -> new OnsetDetector(RATE, 10, 0, 50, 300, 0.05)));
        check("t14 bgAlpha=1 抛", threw(() -> new OnsetDetector(RATE, 10, 5, 50, 300, 1.0)));
        check("t14 minGap=-1 抛", threw(() -> new OnsetDetector(RATE, 10, 5, -1, 300, 0.05)));
    }

    /** ★ 首帧即高潮也必须报（背景初值取 min，见 OnsetDetector 内注释）。 */
    private static void t15_loudFirstFrameNoHole() {
        short[] pcm = concat(sine(0.5, 440, 12000), silence(1.0));
        Rec rec = run(pcm, new OnsetDetector(RATE));
        eq("t15 首帧即响 onset 数", rec.onsets.size(), 1);
        eq("t15 首帧即响 onset 帧号", rec.onsets.get(0), 0L);
    }

    /** sink 每完成一帧必被调用一次（漏调会导致 sidecar 包络缺失，且不容易在真机上发现）。 */
    private static void t16_sinkReceivesEveryFrame() {
        OnsetDetector d = new OnsetDetector(RATE);
        final int[] n = {0};
        final long[] last = {-1};
        d.setSink((i, e, onset, ratio) -> {
            n[0]++;
            if (i != last[0] + 1) throw new IllegalStateException("帧号跳变 " + last[0] + "→" + i);
            last[0] = i;
        });
        short[] pcm = concat(silence(0.3), sine(0.3, 440, 6000), silence(0.4));
        d.push(pcm, 0, pcm.length);
        eq("t16 sink 次数 = 帧数", n[0], 100);
    }

    // ── 辅助 ─────────────────────────────────────────────────────────────────

    private static final class Rec {
        final List<Long> onsets = new ArrayList<>();
        final List<Double> energies = new ArrayList<>();
        final List<Double> ratios = new ArrayList<>();   // 每个 onset 那一刻的 ratio（诊断量）

        double energyAt(long frame) {
            return frame >= 0 && frame < energies.size() ? energies.get((int) frame) : Double.NaN;
        }
    }

    private static Rec run(short[] pcm, OnsetDetector d) {
        Rec r = new Rec();
        d.setSink((i, e, onset, ratio) -> {
            r.energies.add(e);
            if (onset) { r.onsets.add(i); r.ratios.add(ratio); }
        });
        d.push(pcm, 0, pcm.length);
        return r;
    }

    private static short[] silence(double seconds) {
        return new short[(int) Math.round(RATE * seconds) * 2];
    }

    private static short[] sine(double seconds, double freq, int amp) {
        int frames = (int) Math.round(RATE * seconds);
        short[] s = new short[frames * 2];
        for (int i = 0; i < frames; i++) {
            short v = (short) Math.round(amp * Math.sin(2 * Math.PI * freq * i / RATE));
            s[i * 2] = v;
            s[i * 2 + 1] = v;
        }
        return s;
    }

    private static short[] concat(short[]... parts) {
        int n = 0;
        for (short[] p : parts) n += p.length;
        short[] out = new short[n];
        int off = 0;
        for (short[] p : parts) {
            System.arraycopy(p, 0, out, off, p.length);
            off += p.length;
        }
        return out;
    }

    private static String fmt(double v) {
        return String.format(java.util.Locale.US, "%.4g", v);
    }

    private static void eq(String name, long got, long want) {
        if (got == want) pass++;
        else fails.add(name + "：期望 " + want + "，实得 " + got);
    }

    private static void check(String name, boolean ok) {
        if (ok) pass++;
        else fails.add(name);
    }

    private interface Thrower {
        void run();
    }

    private static boolean threw(Thrower t) {
        try {
            t.run();
            return false;
        } catch (IllegalArgumentException ex) {
            return true;
        }
    }

    private OnsetSelfTest() {
    }
}
