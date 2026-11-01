package io.pgauto.play;

/**
 * `EnvMatcher` 的纯 JVM 自测 —— 它是「端内自己算 K」的唯一算法，错了会让 `L_dev` 悄悄偏。
 *
 * 数据用**合成信号**（确定性、不依赖 assets）：造一条有结构的"母带包络"，把某一段
 * 平移 + 改增益 + 加噪当作"捕获"，再让 matcher 找回来。
 *
 * ★ 也提供 `--replay <env.csv> <manifest.json>`：拿**真机标定文件**跑一遍，
 *   与 `tmp/env_corr.py` 的结果对拍（两处实现分叉时只有这条路抓得住）。
 *
 * 运行：`bash android/play/selftest.sh`
 */
public final class EnvMatcherSelfTest {

    private static int pass = 0, fail = 0;

    public static void main(String[] args) throws Exception {
        if (args.length >= 3 && "--replay".equals(args[0])) {
            replay(args[1], args[2]);
            return;
        }
        t01_knownShiftRecovered();
        t02_gainInvariance();
        t03_priorWindowRestricts();
        t04_wrongPriorGivesLowCc();
        t05_qualityRejectsFlatPeak();
        t06_trimHandlesLeadingSilence();
        t07_shortInputFailsLoudly();
        t08_base64RoundTripWithCodec();
        System.out.println("EnvMatcherSelfTest: " + pass + " 通过, " + fail + " 失败");
        if (fail > 0) throw new AssertionError("EnvMatcher 自测失败 " + fail + " 项");
    }

    /** 造一条有结构的包络：静音 + 若干"音头" + 衰减，长度 n 帧。 */
    private static double[] synthMaster(int n, long seed) {
        java.util.Random r = new java.util.Random(seed);
        double[] x = new double[n];
        java.util.Arrays.fill(x, 0.0);
        int i = 100;
        while (i < n - 50) {
            int len = 20 + r.nextInt(40);
            double amp = 3000 + r.nextDouble() * 12000;
            for (int k = 0; k < len && i + k < n; k++) {
                x[i + k] = amp * Math.exp(-k / 6.0) + 200 * r.nextDouble();
            }
            i += 60 + r.nextInt(60);
        }
        return x;
    }

    /** 捕获 = 前导静音 + 母带从 start 帧起切一段 + 增益 + 噪声。 */
    private static double[] synthCapture(double[] master, int start, int leadFrames,
                                         double gain, long seed) {
        java.util.Random r = new java.util.Random(seed);
        int n = Math.min(master.length - start, 1500);
        double[] y = new double[leadFrames + n];
        for (int i = 0; i < n; i++) {
            y[leadFrames + i] = Math.max(0, master[start + i] * gain + r.nextGaussian() * 20);
        }
        return y;
    }

    private static void t01_knownShiftRecovered() {
        double[] master = synthMaster(2000, 42);
        int start = 300;
        double[] cap = synthCapture(master, start, 120, 0.5, 7);
        // 期望：cap[0] ↔ master[0] 的偏移 = start − lead = 180 帧
        EnvMatcher.Result m = EnvMatcher.match(cap, master, -1, 0);
        ok(m.ok, "t01 应判 ok，实得 " + m);
        ok(Math.abs(m.lagFrames - 180) <= 1.0,
                "t01 位移应还原为 180 帧，实得 " + m.lagFrames);
    }

    private static void t02_gainInvariance() {
        double[] master = synthMaster(2000, 42);
        double[] a = synthCapture(master, 300, 120, 0.05, 7);
        double[] b = synthCapture(master, 300, 120, 3.0, 7);
        double la = EnvMatcher.match(a, master, -1, 0).lagFrames;
        double lb = EnvMatcher.match(b, master, -1, 0).lagFrames;
        ok(Math.abs(la - lb) <= 1.0,
                "t02 增益 60× 变化后 lag 必须不变：" + la + " vs " + lb);
    }

    private static void t03_priorWindowRestricts() {
        double[] master = synthMaster(2000, 42);
        double[] cap = synthCapture(master, 300, 120, 0.5, 7);
        EnvMatcher.Result good = EnvMatcher.match(cap, master, 180, 10);
        ok(good.ok && Math.abs(good.lagFrames - 180) <= 1.0,
                "t03 先验 ±10 帧内应还原 180，实得 " + good);
    }

    private static void t04_wrongPriorGivesLowCc() {
        double[] master = synthMaster(2000, 42);
        double[] cap = synthCapture(master, 300, 120, 0.5, 7);
        // 先验故意错 600 帧（远超 ±20 帧窗）⇒ cc 必须掉下来，调用方据此拒绝
        EnvMatcher.Result bad = EnvMatcher.match(cap, master, 780, 20);
        ok(!bad.ok || bad.cc < 0.8,
                "t04 先验错时不得判 ok 且 cc<0.8，实得 " + bad);
    }

    /** 一条几乎没有结构的包络（全是常数）⇒ 质量分不能假装很高。 */
    private static void t05_qualityRejectsFlatPeak() {
        double[] flat = new double[1500];
        java.util.Arrays.fill(flat, 1000.0);
        double[] rec = new double[900];
        java.util.Arrays.fill(rec, 1000.0);
        EnvMatcher.Result m = EnvMatcher.match(rec, flat, -1, 0);
        ok(!m.ok, "t05 常数包络必须判 not ok，实得 " + m);
    }

    private static void t06_trimHandlesLeadingSilence() {
        double[] master = synthMaster(2000, 42);
        // 捕获前面有 2.4 s 静音（240 帧）+ 母带从 0 起 —— 不掐静音会被带偏（Python 侧 B8）
        double[] cap = synthCapture(master, 0, 240, 0.7, 11);
        EnvMatcher.Result m = EnvMatcher.match(cap, master, -1, 0);
        // ★ 符号：lag 的定义是 rec[t] ↔ ref[t+lag]。捕获前面多 240 帧静音 ⇒ 母带 0 出现在
        //   rec 的 240 帧处 ⇒ 240+lag=0 ⇒ **lag = −240**（S = −lag×hop = +2400 ms）。
        //   第一版这里期望 +240，是测试写错了，不是实现错（对拍 t01 的 +180 可验）。
        ok(m.ok && Math.abs(m.lagFrames + 240) <= 1.0,
                "t06 带 2.4 s 静音的捕获应得 lag=−240（S=+2400 ms），实得 " + m);
        ok(m.trimRec >= 200, "t06 必须真的掐掉静音前缀，trimRec=" + m.trimRec);
    }

    private static void t07_shortInputFailsLoudly() {
        double[] tiny = new double[10];
        for (int i = 0; i < 10; i++) tiny[i] = 100 + i;
        EnvMatcher.Result m = EnvMatcher.match(tiny, tiny, -1, 0);
        ok(!m.ok, "t07 输入太短必须 not ok，实得 " + m);
    }

    /** 母带指纹走 base64（pack_assets 写）⇒ 解码后与原始 u8 逐值相同。 */
    private static void t08_base64RoundTripWithCodec() {
        byte[] u8 = new byte[64];
        for (int i = 0; i < u8.length; i++) u8[i] = (byte) (i * 3);
        String b64 = java.util.Base64.getEncoder().encodeToString(u8);
        double[] rms = EnvCodec.rmsFromBase64(b64);
        ok(rms.length == u8.length, "t08 base64 解码长度必须一致");
        boolean same = true;
        for (int i = 0; i < u8.length; i++) {
            if (Math.abs(rms[i] - EnvCodec.rmsOf(u8[i] & 0xFF)) > 1e-9) same = false;
        }
        ok(same, "t08 base64 → RMS 必须与 EnvCodec.rmsOf 逐值相同");
    }

    // ─────────────────────── 真机数据对拍（手动跑） ───────────────────────

    private static void replay(String envCsv, String manifestPath) throws Exception {
        String[] lines = new String(java.nio.file.Files.readAllBytes(
                java.nio.file.Paths.get(envCsv)), java.nio.charset.StandardCharsets.UTF_8)
                .split("\\R");
        String head = lines[0];
        int frames = 0, onsetFrame = -1;
        double silRefMs = 0;
        String song = "";
        for (String kv : head.substring(2).split(" ")) {
            String[] p = kv.split("=", 2);
            if (p.length != 2) continue;
            switch (p[0]) {
                case "frames": frames = Integer.parseInt(p[1]); break;
                case "song": song = p[1]; break;
                case "onset_frame": onsetFrame = Integer.parseInt(p[1]); break;
                case "sil_ref_ms": silRefMs = Double.parseDouble(p[1]); break;
                default: break;
            }
        }
        int[] q = new int[frames];
        String[] vals = lines[1].split(",");
        for (int i = 0; i < frames && i < vals.length; i++) q[i] = Integer.parseInt(vals[i]);
        double[] rec = new double[frames];
        for (int i = 0; i < frames; i++) rec[i] = EnvCodec.rmsOf(q[i]);

        String manifest = new String(java.nio.file.Files.readAllBytes(
                java.nio.file.Paths.get(manifestPath)), java.nio.charset.StandardCharsets.UTF_8);
        String b64 = extract(manifest, song);
        double[] ref = EnvCodec.rmsFromBase64(b64);
        // ★ 先验的**符号**（第一版写反了，replay 直接 cc=−0.48）：
        //   lag 的定义是 rec[t] ↔ ref[t+lag]，而 S（母带 0 在捕获里的位置）= −lag×hop
        //   ⇒ 期望 lag = −S/hop = −E/hop = silRefFrames − onsetFrame（负值）。
        int center = onsetFrame < 0 ? -1
                : (int) Math.round(silRefMs / 10.0) - onsetFrame;
        EnvMatcher.Result m = EnvMatcher.match(rec, ref, center, 100);
        double eMs = onsetFrame * 10.0 - silRefMs;
        double sMs = m.startMs(10);
        System.out.printf(java.util.Locale.ROOT,
                "replay %s: lag=%.1f 帧 cc=%.3f 质量=%.2f ok=%s%n  S=%.1f ms E=%.1f ms K=%.1f ms%n",
                song, m.lagFrames, m.cc, m.quality, m.ok, sMs, eMs, eMs - sMs);
    }

    /** 从 manifest 的 JSON 文本里抠出 `"env_b64": "..."`（只在 replay 工具里用，不当解析器）。 */
    private static String extract(String manifest, String song) {
        int i = manifest.indexOf("\"key\": \"" + song + "\"");
        if (i < 0) i = manifest.indexOf("\"key\":\"" + song + "\"");
        if (i < 0) throw new IllegalArgumentException("manifest 里找不到 " + song);
        int j = manifest.indexOf("\"env_b64\"", i);
        if (j < 0) throw new IllegalArgumentException(song + " 没有 env_b64");
        int q1 = manifest.indexOf('"', manifest.indexOf(':', j) + 1);
        int q2 = manifest.indexOf('"', q1 + 1);
        return manifest.substring(q1 + 1, q2);
    }

    // ─────────────────────────── 工具 ───────────────────────────

    private static void ok(boolean cond, String what) {
        if (cond) {
            pass++;
            System.out.println("  ✓ " + what);
        } else {
            fail++;
            System.out.println("  ✗ " + what);
        }
    }

    private EnvMatcherSelfTest() {}
}
