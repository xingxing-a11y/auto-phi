package io.pgauto.probe;

/**
 * 实时 onset 检出 + 10 ms 能量包络。**纯 Java，零 Android 依赖** —— 这样 PC 端
 * `javac` 就能单测（见 `OnsetSelfTest`），不必把逻辑塞进 Service 里靠真机试。
 *
 * 为什么要有它（两个已知缺口，都写在 `FACTS §82`）：
 *   ① **延迟未测**：`AudioRecord.read()` 是阻塞的，32768 B 缓冲 ≈ 186 ms、每块 2048 帧 ≈ 46 ms
 *      ⇒ 事后拿 wav 做互相关只能得到「样本序号 ↔ 母带时刻」，得不出「样本序号 ↔ 手机挂钟」。
 *   ② **相关误锁**：曲内有近乎逐样本相同的段落（实测两个 0.999 差 43.1 s），全局 argmax 会锁错。
 *
 * 本类的角色：把「每 10 ms 一个能量值 + 帧号」吐出来，调用方（Service）负责给它贴上
 * 单调时钟时间戳。于是 PC 端拿到的是**带绝对时间的包络流**：
 *   - 用包络与母带包络做粗对齐（100 Hz，比 44.1 kHz 波形快 400 倍）⇒ 得到粗位置；
 *   - 用粗位置把精细波形 NCC 的搜索窗掐死在 ±0.5 s 内 ⇒ **误锁在结构上不可能**（单调搜索）；
 *   - 包络帧的挂钟时间戳 − 母带时刻 = **端到端延迟**，这就是 Ⅱ-b 要的那个数。
 *
 * 能量口径：`mean(x^2)`（下混成单声道后），单位与幅度平方同量纲。
 *   Phigros 实测 RMS 9929 ⇒ 有声段 meanSquare ≈ 9.9e7；静音段应 < 1e3。
 */
public final class OnsetDetector {

    /** 每完成一帧回调一次。`frameIdx` 从 0 严格递增 1。 */
    public interface FrameSink {
        /**
         * @param frameIdx   帧号（= 已消费的 hop 数）
         * @param meanSquare 该帧 mean(x^2)
         * @param onset      本帧是否判为 onset
         * @param ratio      meanSquare / bg（bg 为背景能量；bg 未建立时为 +Inf）
         */
        void onFrame(long frameIdx, double meanSquare, boolean onset, double ratio);
    }

    // ── 默认参数（都做成实例字段，便于自测构造边界）──────────────────────────
    /** 帧长 = 10 ms ⇒ 100 Hz 包络。Ⅱ-b 需要曲首 1~3 s 内定零点，10 ms 粒度够（判定窗 P 是 ±80 ms）。 */
    public static final int DEFAULT_FRAME_MS = 10;
    /** 能量比阈值 5.0 ≈ +7 dB。低于 4 会把持续音的起伏当 onset；只影响 `onset` 标记，**不影响包络落盘**。 */
    public static final double DEFAULT_THR_RATIO = 5.0;
    /** 最小 onset 间隔 50 ms —— 低于此的两声在 Phigros 里通常是同一个音的包络起伏。 */
    public static final int DEFAULT_MIN_GAP_MS = 50;
    /** 绝对能量底噪：RMS 300（≈ −41 dBFS）。防止数字静音（全 0）被 EMA 抖出 onset。 */
    public static final double DEFAULT_ABS_FLOOR_RMS = 300.0;
    /** 背景 EMA 系数（安静段），200 ms 时间常数。 */
    public static final double DEFAULT_BG_ALPHA = 0.05;
    /**
     * ★ 超阈段的背景跟进系数（500 ms 时间常数）。
     *
     * 为什么需要两档：一开始写成「`onset` 为真才慢跟进，否则快速 EMA」是个**真 bug** ——
     * 持续音里第 2 帧起被 `minGap` 挡掉（onset=false）却仍 `over`，于是走快速 EMA，
     * 背景在 200 ms 内被顶到信号量级 ⇒ **同一个 burst 之后紧跟的下一个 burst 必然漏报**
     * （`OnsetSelfTest.testTwoBursts` 抓到的就是这个）。
     * 正解：按 **`over`**（是否超阈）分档，而不是按 `onset`。
     * 取 0.02 而非 0.05：太快会吞掉 1 s 后的下一个音，太慢会让背景停留低位导致持续段反复误报。
     */
    public static final double DEFAULT_BG_ALPHA_OVER = 0.02;
    /**
     * ★ 「显著上升」判据：本帧能量必须超过上一帧的这么多倍才算上升沿。
     *
     * 为什么不能用 `e >= prevE`：自测 `t02/t03/t04/t06/t15` 首版全红，每个 burst 都报 **2** 个
     * onset —— 持续段（正弦稳定）每帧能量只抖 ±1%，`>=` 恒真，于是 `minGap` 一到就再报一次。
     * 取 1.15（≈ +0.6 dB / 10 ms）：打击瞬态的能量跳变远大于它，而持续音的逐帧起伏远小于它。
     */
    public static final double DEFAULT_RISE_RATIO = 1.15;

    private final int rate;
    private final int hop;
    private final int minGapFrames;
    private final double thrRatio;
    private final double absFloorE;
    private final double bgAlpha;
    private final double bgAlphaOver;
    private final double riseRatio;

    private final double[] acc;
    private int accN = 0;

    private long frame = 0;
    private double bg = 0;
    private boolean primed = false;   // bg 是否已由第一帧初始化
    private double prevE = 0;
    private long lastOnsetFrame = Long.MIN_VALUE / 4;
    private int onsetCount = 0;

    private FrameSink sink;

    public OnsetDetector(int rate) {
        this(rate, DEFAULT_FRAME_MS, DEFAULT_THR_RATIO, DEFAULT_MIN_GAP_MS,
                DEFAULT_ABS_FLOOR_RMS, DEFAULT_BG_ALPHA);
    }

    /**
     * @param frameMs      帧长（毫秒）
     * @param thrRatio     能量比阈值
     * @param minGapMs     最小 onset 间隔（毫秒）
     * @param floorRms     绝对底噪（RMS 幅度，0~32767）
     * @param bgAlpha      安静段背景 EMA 系数（0~1）
     */
    public OnsetDetector(int rate, int frameMs, double thrRatio, int minGapMs,
                         double floorRms, double bgAlpha) {
        this(rate, frameMs, thrRatio, minGapMs, floorRms, bgAlpha, DEFAULT_BG_ALPHA_OVER);
    }

    public OnsetDetector(int rate, int frameMs, double thrRatio, int minGapMs,
                         double floorRms, double bgAlpha, double bgAlphaOver) {
        if (rate <= 0) throw new IllegalArgumentException("rate 必须 > 0，得到 " + rate);
        if (frameMs <= 0) throw new IllegalArgumentException("frameMs 必须 > 0，得到 " + frameMs);
        if (thrRatio <= 0) throw new IllegalArgumentException("thrRatio 必须 > 0，得到 " + thrRatio);
        if (minGapMs < 0) throw new IllegalArgumentException("minGapMs 必须 ≥ 0，得到 " + minGapMs);
        if (bgAlpha <= 0 || bgAlpha >= 1) throw new IllegalArgumentException("bgAlpha 必须在 (0,1)，得到 " + bgAlpha);
        if (bgAlphaOver <= 0 || bgAlphaOver >= 1) throw new IllegalArgumentException("bgAlphaOver 必须在 (0,1)，得到 " + bgAlphaOver);

        this.rate = rate;
        this.hop = Math.max(1, rate * frameMs / 1000);
        this.thrRatio = thrRatio;
        this.minGapFrames = (int) Math.max(0L, (long) minGapMs * rate / 1000 / this.hop);
        this.absFloorE = floorRms * floorRms;
        this.bgAlpha = bgAlpha;
        this.bgAlphaOver = bgAlphaOver;
        this.riseRatio = DEFAULT_RISE_RATIO;
        this.acc = new double[this.hop];
    }

    public void setSink(FrameSink s) {
        this.sink = s;
    }

    public int hopSamples() {
        return hop;
    }

    public int onsetCount() {
        return onsetCount;
    }

    /**
     * 喂一块交错立体声 PCM（与 `AudioRecord` 的 `read(short[], …)` 完全同格式）。
     *
     * @param buf short 数组
     * @param n   有效 short 个数（**必须是偶数**；奇数尾巴会被忽略，便于发现上游 bug）
     * @return 本块内完成的帧数
     */
    public int push(short[] buf, int n) {
        return push(buf, 0, n);
    }

    /**
     * 同上，但支持从 `buf[off]` 开始的子区间（自测分块喂数据用；Service 走两参数版本）。
     */
    public int push(short[] buf, int off, int n) {
        if (buf == null) throw new IllegalArgumentException("buf == null");
        if (off < 0) throw new IllegalArgumentException("off 不能为负：" + off);
        if (n < 0) throw new IllegalArgumentException("n 不能为负：" + n);
        if ((long) off + n > buf.length) {
            throw new IllegalArgumentException("off+n 越界：" + off + "+" + n + " > " + buf.length);
        }
        int done = 0;
        int i = off;
        // 立体声下混（(L+R)/2）。注意用 double 累加：mean 平方后单样本最大 1.07e9，
        // 累加 441 个仍在 double 精度内，不需要 long。
        final int end = off + n - (n & 1);      // 丢掉奇数尾样本
        while (i < end) {
            double m = (buf[i] + buf[i + 1]) * 0.5;
            acc[accN++] = m * m;
            i += 2;
            if (accN == hop) {
                flushFrame();
                accN = 0;
                done++;
            }
        }
        return done;
    }

    private void flushFrame() {
        double sum = 0;
        for (int k = 0; k < hop; k++) sum += acc[k];
        double e = sum / hop;

        if (!primed) {
            // ★ 背景初值：下界 floor×1e-3、上界 floor。
            //   上界（min 的作用）：首帧恰好就是曲首第一个音时，若取 bg == e ⇒ over 永远为假
            //     ⇒ **最该报的那一个 onset 被吃掉**（Ⅱ-b 的 onset 就在曲首 1~3 s）。
            //   下界（2026-10-18 真机实测补的）：首帧是数字静音时 e == 0 ⇒ bg == 0 ⇒
            //     下面 `bg < 1e-9` 兜底成 1e-9 ⇒ 之后任何声音都算出 1e16 量级的 ratio。
            //     实测 rec_20261018_205750 首 onset 报 ratio=15494058811224490。
            //   注意 onset **判定**不受这两个界影响：loud 要求 e > absFloorE，
            //   而 bg×thrRatio ≤ absFloorE×thrRatio，两者从不冲突。
            bg = Math.max(Math.min(e, absFloorE), absFloorE * 1e-3);
            primed = true;
        }

        boolean loud = e > absFloorE;
        boolean over = e > bg * thrRatio;
        boolean rising = e > prevE * riseRatio;
        boolean spaced = (frame - lastOnsetFrame) >= minGapFrames;
        boolean onset = loud && over && rising && spaced;

        // ratio 的分母不低于绝对底噪：bg 趋 0 时「倍数」在物理上没有意义（见上），
        // 用 max(bg, absFloorE) 让 ratio 统一口径为「相对底噪的倍数」，跨轮可比。
        double ratio = e / Math.max(bg, absFloorE);

        if (onset) {
            lastOnsetFrame = frame;
            onsetCount++;
        }
        // ★ 按 `over`（是否超阈）分档更新背景，不按 `onset` —— 见 DEFAULT_BG_ALPHA_OVER 的注释。
        //   超阈段慢跟进（不让本段把背景顶上去），安静段快跟进（快速回到真实底噪）。
        bg = over
                ? bg * (1 - bgAlphaOver) + e * bgAlphaOver
                : bg * (1 - bgAlpha) + e * bgAlpha;
        if (bg < 1e-9) bg = 1e-9;      // 防 0 除

        prevE = e;
        if (sink != null) sink.onFrame(frame, e, onset, ratio);
        frame++;
    }

    /** 清空内部状态（换曲 / 重录时调用）。 */
    public void reset() {
        for (int k = 0; k < acc.length; k++) acc[k] = 0;
        accN = 0;
        frame = 0;
        bg = 0;
        primed = false;
        prevE = 0;
        lastOnsetFrame = Long.MIN_VALUE / 4;
        onsetCount = 0;
    }

    public long frameIndex() {
        return frame;
    }

    public int rate() {
        return rate;
    }
}
