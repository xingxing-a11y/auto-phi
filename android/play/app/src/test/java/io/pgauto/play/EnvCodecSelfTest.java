package io.pgauto.play;

/**
 * `EnvCodec` 的纯 JVM 自测 —— 包络编解码是**标定链路唯一的失真源**，错了整条互相关都白算。
 *
 * 为什么值得单测（2026-10-25 方案 S1）：
 *   · 编码用 `log` 而不是 `log1p` —— 后者不是增益不变，下游归一化互相关会让峰跑飞
 *     （`tmp/env_corr.py` 的自测实测过 cc 0.94 → 0.06）；
 *   · 入参是 **meanSquare**（采集线程手里的 e），不是 RMS —— 少做/多做一次 sqrt
 *     会让阈值整体差 3 dB，而下游只看相对形状、**不会报错**，只会让解出来的 `K` 偏几十毫秒。
 *
 * 运行：`bash android/play/selftest.sh`
 */
public final class EnvCodecSelfTest {

    private static int pass = 0;
    private static int fail = 0;

    public static void main(String[] args) {
        t01_roundTripWithinTolerance();
        t02_inputIsMeanSquareNotRms();
        t03_monotonicAndClamped();
        t04_badValuesGoToZero();
        t05_headerRejectsBadInputs();
        t06_fileShape();
        t07_extraMetaInHeader();
        System.out.println("EnvCodecSelfTest: " + pass + " 通过, " + fail + " 失败");
        if (fail > 0) throw new AssertionError("EnvCodec 自测失败 " + fail + " 项");
    }

    /** 编→解 的相对误差 ≤5%（对数 u8 每级 ≈4.6%）。 */
    private static void t01_roundTripWithinTolerance() {
        double[] rms = {1, 10, 100, 300, 1000, 5000, 16000, 30000, 32768};
        for (double r : rms) {
            int q = EnvCodec.quantizeRms(r);
            double back = EnvCodec.rmsOf(q);
            double rel = Math.abs(back - r) / r;
            ok(rel <= 0.05, "往返误差 ≤5%：rms=" + r + " → q=" + q + " → " + back
                    + "（相对误差 " + String.format(java.util.Locale.ROOT, "%.3f", rel) + "）");
        }
    }

    /**
     * ★ 入参是 meanSquare：传 `{9e4}`（= RMS 300）必须解回 ≈300，而不是 90000。
     *
     * 变异点：把 `encodeMeanSquare` 里的 `sqrt` 删掉 ⇒ 这里解出 32768（被夹满）⇒ 红。
     */
    private static void t02_inputIsMeanSquareNotRms() {
        double[] ms = {9.0e4};                       // RMS 300
        double[] back = EnvCodec.decode(EnvCodec.encodeMeanSquare(ms));
        ok(Math.abs(back[0] - 300.0) / 300.0 <= 0.05,
                "meanSquare 9e4 必须解回 ≈300 RMS，实得 " + back[0]);
    }

    /** 单调 + 夹取：0 < 小 < 中 < 大；超满幅夹到 255；0 ⇒ 0。 */
    private static void t03_monotonicAndClamped() {
        int a = EnvCodec.quantizeRms(1), b = EnvCodec.quantizeRms(100), c = EnvCodec.quantizeRms(3000);
        ok(a < b && b < c, "必须单调：1→" + a + " < 100→" + b + " < 3000→" + c);
        ok(EnvCodec.quantizeRms(1e9) == 255, "超满幅必须夹到 255");
        ok(EnvCodec.quantizeRms(32768) == 255, "满幅 = 255");
        ok(EnvCodec.quantizeRms(0) == 0, "0 RMS ⇒ 0");
    }

    /** NaN / 负数 必须归 0，不许把 NaN 传下去（下游一乘就是 NaN，峰全变 NaN）。 */
    private static void t04_badValuesGoToZero() {
        ok(EnvCodec.quantizeRms(Double.NaN) == 0, "NaN ⇒ 0");
        ok(EnvCodec.quantizeRms(-5) == 0, "负数 ⇒ 0");
        double[] ms = {Double.NaN, -1, 0};
        byte[] enc = EnvCodec.encodeMeanSquare(ms);
        ok(enc[0] == 0 && enc[1] == 0 && enc[2] == 0, "非法 meanSquare 一律编成 0");
    }

    /** 头信息缺一项下游就解不回来 ⇒ 非法输入必须炸，不许拼一行看起来正常的注释。 */
    private static void t05_headerRejectsBadInputs() {
        mustThrow(() -> EnvCodec.header(0, 10, 48000, "k"), "hopMs=0");
        mustThrow(() -> EnvCodec.header(10, -1, 48000, "k"), "frames<0");
        mustThrow(() -> EnvCodec.header(10, 10, 0, "k"), "rate=0");
        mustThrow(() -> EnvCodec.header(10, 10, 48000, ""), "空 songKey");
        mustThrow(() -> EnvCodec.header(10, 10, 48000, "a,b"), "songKey 含逗号");
        mustThrow(() -> EnvCodec.header(10, 10, 48000, "a\nb"), "songKey 含换行");
        ok(EnvCodec.header(10, 2, 48000, "bounded_in")
                        .equals("# env v=1 hop_ms=10 frames=2 sr=48000 song=bounded_in"),
                "正常头必须逐字稳定（下游按它解析）");
    }

    /** 文件形状：1 行注释 + 1 行逗号分隔、个数对齐、无尾逗号。 */
    private static void t06_fileShape() {
        byte[] u8 = {0, 1, 127, (byte) 255};
        String f = EnvCodec.toFile(10, 48000, "credits_ez", u8);
        String[] lines = f.split("\n");
        ok(lines.length == 2, "必须正好两行（注释 + 数据），实得 " + lines.length);
        ok(lines[0].startsWith("# env v=1 hop_ms=10 frames=4"), "注释行要带 frames，实得 " + lines[0]);
        ok(lines[1].equals("0,1,127,255"), "数据行要无尾逗号，实得 " + lines[1]);
        ok(!lines[1].contains("-"), "u8 必须按无符号打印（不能出现负号），实得 " + lines[1]);
    }

    /** 标定头必须能带 `onset_frame` 等额外元信息 —— 缺了它 K 就算不出来（第一轮标定的教训）。 */
    private static void t07_extraMetaInHeader() {
        String h = EnvCodec.header(10, 3003, 48000, "bounded_in",
                "onset_frame=1070 sil_ref_ms=865.0 fired_by=abs");
        ok(h.endsWith("onset_frame=1070 sil_ref_ms=865.0 fired_by=abs"),
                "额外元信息必须原样附在头尾，实得 " + h);
        ok(h.startsWith("# env v=1 hop_ms=10 frames=3003 sr=48000 song=bounded_in "),
                "原有字段不得被挤位，实得 " + h);
        String h2 = EnvCodec.header(10, 2, 48000, "k", "  ");
        ok(!h2.endsWith(" "), "空白 extra 不得留尾空格，实得 «" + h2 + "»");
        String h3 = EnvCodec.header(10, 2, 48000, "k", "a=1\nb=2");
        ok(!h3.contains("\n"), "extra 里的换行必须压平（否则一条注释会断成两行）");
    }

    // ─────────────────────────── 工具 ───────────────────────────

    private static void mustThrow(Runnable r, String what) {
        try {
            r.run();
            fail++;
            System.out.println("  ✗ " + what + " 应抛而未抛");
        } catch (RuntimeException e) {
            pass++;
            System.out.println("  ✓ " + what + " ⇒ " + e.getClass().getSimpleName());
        }
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

    private EnvCodecSelfTest() {}
}
