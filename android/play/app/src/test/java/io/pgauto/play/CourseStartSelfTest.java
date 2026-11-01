package io.pgauto.play;

/**
 * `CourseStart` 的离线单测 —— **纯 JVM，不需要 Android、不需要真机**。
 *
 * 动机（2026-10-27）：课题模式第 2/3 首的曲首原本靠"第一个响点"（OnsetGate），
 * 但曲间过场一直在响 ⇒ 要么等不到、要么认到过场音上（零点偏 1~3 s）。
 * 改用包络互相关后，"曲首在哪"变成一个**可离线验证**的量：
 * 造一段母带指纹，把它按已知偏移塞进一段更长的捕获里，看能不能原样找回来。
 *
 * 运行：`bash android/play/selftest.sh`
 */
public final class CourseStartSelfTest {

    private static int pass = 0;
    private static int fail = 0;

    private static final int HOP = 10;              // 10 ms 一帧
    private static final int REF_FRAMES = 3000;     // 母带指纹 30 s

    public static void main(String[] args) {
        t01_findsKnownOffset();
        t02_findsWhenCaptureStartsMidSong();
        t03_partialOverlapStillWorks();
        t04_silenceRejected();
        t05_wrongSongRejected();
        t06_threeSongSequence();
        System.out.println("CourseStart: " + pass + " passed, " + fail + " failed");
        if (fail > 0) {
            throw new AssertionError("CourseStartSelfTest 失败 " + fail + " 条");
        }
    }

    /**
     * 造一段"像音乐"的包络：**随机游走的主体 + 不规则的爆发**。
     *
     * ⛔ 不要用"sin 叠加"造测试数据（第一版就是）：给一个 33 帧周期的强正弦，
     *    互相关会在别处出现同高的峰 ⇒ 定位偏好几秒，测试反而在骂实现。
     *    真实母带的包络是宽带的（随机游走 + 突发），自相关只在真位置出尖峰。
     */
    private static double[] music(int frames, int seed) {
        double[] v = new double[frames];
        java.util.Random r = new java.util.Random(seed);
        double level = 6000;
        for (int k = 0; k < frames; k++) {
            level = Math.max(0, 0.92 * level + 6000 + r.nextGaussian() * 900);
            double burst = r.nextInt(120) == 0 ? 14000 : 0;
            v[k] = level + burst + Math.abs(r.nextGaussian()) * 200;
        }
        return v;
    }

    /** 低噪底（模拟曲间过场/底噪）：非零、无结构。 */
    private static double[] lowNoise(int frames, int seed) {
        java.util.Random r = new java.util.Random(seed);
        double[] v = new double[frames];
        for (int k = 0; k < frames; k++) {
            v[k] = Math.abs(r.nextGaussian()) * 120;
        }
        return v;
    }

    /** t01：把母带按偏移 500 帧（5 s）塞进捕获，必须找回 5000 ms。 */
    private static void t01_findsKnownOffset() {
        double[] ref = music(REF_FRAMES, 7);
        double[] cap = lowNoise(4200, 3);
        System.arraycopy(ref, 0, cap, 500, REF_FRAMES);
        CourseStart.Hit h = CourseStart.locate(cap, ref, HOP, -1, 0);
        ok(h.ok, "t01 应判可信，实际 " + h);
        near(h.startMs, 5000, 30, "t01 曲首应被定位在 5000 ms");
    }

    /** t02：捕获从曲中开始（母带第 0 帧在窗口之外，startMs 为负）。 */
    private static void t02_findsWhenCaptureStartsMidSong() {
        double[] ref = music(REF_FRAMES, 11);
        int skip = 400;                       // 母带第 400 帧开始捕获
        double[] cap = new double[REF_FRAMES - skip];
        System.arraycopy(ref, skip, cap, 0, cap.length);
        CourseStart.Hit h = CourseStart.locate(cap, ref, HOP, -skip, 100);
        ok(h.ok, "t02 应判可信，实际 " + h);
        near(h.startMs, -skip * HOP, 60, "t02 startMs 应为负（曲首在窗口之前）");
    }

    /** t03：只采到 18 s（部分重叠），仍应定位正确。 */
    private static void t03_partialOverlapStillWorks() {
        double[] ref = music(REF_FRAMES, 23);
        int off = 800;                        // 曲首在 8 s 处
        double[] cap = lowNoise(off + 1800, 5);  // 只覆盖母带前 18 s
        System.arraycopy(ref, 0, cap, off, 1800);
        CourseStart.Hit h = CourseStart.locate(cap, ref, HOP, -1, 0);
        ok(h.ok, "t03 部分重叠也应可信，实际 " + h);
        near(h.startMs, off * HOP, 60, "t03 仍应定在 8000 ms");
    }

    /**
     * t06：**三首连打** —— 三段窗口各自含不同曲的指纹、偏移各不相同（模拟每首 rearm 的时机不同），
     * 必须都能定位。这是"课题模式连打三首"能成立的离线证据。
     */
    private static void t06_threeSongSequence() {
        final int[] offsets = {700, 1250, 520};
        for (int i = 0; i < offsets.length; i++) {
            double[] ref = music(REF_FRAMES, 100 + i);
            double[] cap = lowNoise(offsets[i] + REF_FRAMES + 200, 200 + i);
            System.arraycopy(ref, 0, cap, offsets[i], REF_FRAMES);
            CourseStart.Hit h = CourseStart.locate(cap, ref, HOP, -1, 0);
            ok(h.ok, "t06-" + (i + 1) + " 第 " + (i + 1) + " 首应判可信，实际 " + h);
            near(h.startMs, offsets[i] * HOP, 30,
                    "t06-" + (i + 1) + " 第 " + (i + 1) + " 首曲首位置");
        }
    }

    /** t04：全静音 ⇒ 必须拒（不能凭空给一个 start）。 */
    private static void t04_silenceRejected() {
        double[] ref = music(REF_FRAMES, 31);
        double[] cap = new double[1500];          // 全静音（数字 0）
        CourseStart.Hit h = CourseStart.locate(cap, ref, HOP, 300, 100);
        ok(!h.ok, "t04 全静音必须判不可信，实际 " + h);
    }

    /** t05：捕获是**另一首**的指纹 ⇒ cc 应偏低，不得当成可信。 */
    private static void t05_wrongSongRejected() {
        double[] ref = music(REF_FRAMES, 41);
        double[] other = music(REF_FRAMES, 97);
        double[] cap = new double[3600];
        System.arraycopy(other, 0, cap, 300, REF_FRAMES);
        CourseStart.Hit h = CourseStart.locate(cap, ref, HOP, 300, 100);
        ok(!h.ok, "t05 别的曲子的包络不该被判可信，实际 " + h);
    }

    private static void ok(boolean cond, String what) {
        if (cond) {
            pass++;
            System.out.println("  ✓ " + what);
        } else {
            fail++;
            System.out.println("  ✗ " + what);
        }
    }

    private static void near(double got, double want, double tol, String what) {
        ok(Math.abs(got - want) <= tol,
                what + "（实际 " + String.format(java.util.Locale.ROOT, "%.1f", got)
                        + "，期望 " + want + "±" + tol + "）");
    }

    private CourseStartSelfTest() {
    }
}
