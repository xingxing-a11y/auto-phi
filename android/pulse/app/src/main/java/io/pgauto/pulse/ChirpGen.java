package io.pgauto.pulse;

/**
 * 标定用线性扫频（chirp）的生成器。**纯函数、零 Android 依赖** —— 可纯 JVM 自测。
 *
 * <p>用途：让「播放 → 被采集」这个延迟 `d_play_submit + d1 + d2` 变成可测量的量。
 * 录制方（另一个 app）录下这段扫频，PC 端用互相关在录音里定位它的起点，
 * 与播放方记下的 `tPlay` 相减即可。
 *
 * <p>★ 为什么用**线性扫频**而不是上一格的 5 个短脉冲：
 * <ol>
 *   <li>互相关的峰宽 ≈ 1/带宽 = 1/5700 Hz ≈ 0.18 ms，比 2 ms 脉冲的定位精度高一个量级；</li>
 *   <li>5 秒长、频率连续变化 ⇒ 在录音里几乎不可能被别的东西误匹配（脉冲可能撞上鼓点）；</li>
 *   <li>起止天然连续：t=0 时 sin(0)=0，不需要淡入淡出。</li>
 * </ol>
 *
 * <p>⛔ **必须由另一个 uid 的 app 播放**（本 APK 就是那个 app）。同一个 app 自播自录时，
 * 系统把它自己的播放路由到 SPEAKER、不进 REMOTE_SUBMIX ⇒ 录音里恒为 0（FACTS §89）。
 */
public final class ChirpGen {

    public static final int RATE = 44100;

    /** 起始频率。300 Hz 落在手机扬声器与混音链都能完整通过的区间。 */
    public static final double F0 = 300.0;

    /** 结束频率。6 kHz 足够高（贡献带宽），又不至于被某些设备的低通滤掉大半。 */
    public static final double F1 = 6000.0;

    /** 时长（秒）。5 s 是为了「在任意长度的录音里都极难误匹配」，不是精度需要。 */
    public static final double SECONDS = 5.0;

    /** 总帧数 = 5 × 44100。 */
    public static final int FRAMES = 220500;

    /** 幅度 = 0.8 满幅（26214）。留 20% 余量，避免过系统音效/EQ 后削波。 */
    public static final int AMP = 26214;

    private ChirpGen() {
    }

    /**
     * 第 {@code i} 个样本的相位（弧度）。
     *
     * <p>线性扫频：瞬时频率 f(t) = F0 + (F1−F0)·t/T，
     * 积分得 φ(t) = 2π·(F0·t + (F1−F0)·t²/(2T))。
     *
     * <p>★ 自测会用**过零率**独立验证这个相位真的产生了 F0→F1 的扫频，
     * 而不是只验证「公式自己算自己」（那样改错频率也测不出来）。
     */
    public static double phaseAt(int i) {
        return phaseAt(i, F0, F1, SECONDS);
    }

    /**
     * 参数化版本 —— 只为让自测能验证「扫频方向」这类性质（变异测试要用）。
     *
     * <p>★ 若断言只调用无参版本，那么把 F0/F1 交换也照样"通过" ⇒ 断言无效。
     */
    public static double phaseAt(int i, double f0, double f1, double seconds) {
        double t = i / (double) RATE;
        return 2.0 * Math.PI * (f0 * t + (f1 - f0) * t * t / (2.0 * seconds));
    }

    /** 单声道扫频。 */
    public static short[] mono() {
        return mono(F0, F1);
    }

    /** 参数化版本，供自测构造「反向扫频」的对照。 */
    public static short[] mono(double f0, double f1) {
        short[] s = new short[FRAMES];
        for (int i = 0; i < FRAMES; i++) {
            s[i] = (short) Math.round(AMP * Math.sin(phaseAt(i, f0, f1, SECONDS)));
        }
        return s;
    }

    /** 立体声交织（左右相同）。 */
    public static short[] stereo() {
        short[] m = mono();
        short[] s = new short[m.length * 2];
        for (int i = 0; i < m.length; i++) {
            s[i * 2] = m[i];
            s[i * 2 + 1] = m[i];
        }
        return s;
    }

    /** 整段字节数（16 bit 立体声），给 AudioTrack 申请 buffer 用。 */
    public static int byteCount() {
        return FRAMES * 2 * 2;
    }

    // ── 运行期采样率 / 多段（2026-10-19 19:3x）──────────────────────────────

    /**
     * 给定采样率下的扫频帧数。
     *
     * <p>★ 必须是 `round(rate × SECONDS)` 而不是常数 220500：设备混音是 48000 Hz
     * （`dumpsys media.audio_flinger`：所有 MIXER 线程 48000，`AudioOut_1D`
     * HAL frame count 1920），我们历来按 44100 申请 ⇒ 播放侧过 44.1k→48k 重采样。
     * 换 rate 时若帧数不跟着改，扫频的**真实时长**就变了（5 s × 44100/48000 = 4.59 s），
     * PC 端按 5 s 生成模板会直接对不上。
     */
    public static int framesFor(int rate) {
        return (int) Math.round(rate * SECONDS);
    }

    /** 带采样率的相位。`phaseAt(i, f0, f1, seconds)` 是自测用的旧签名，保留不动。 */
    public static double phaseAtRate(int i, int rate, double f0, double f1, double seconds) {
        double t = i / (double) rate;
        return 2.0 * Math.PI * (f0 * t + (f1 - f0) * t * t / (2.0 * seconds));
    }

    /** 单段扫频（立体声交织）。 */
    public static short[] stereoAt(int rate) {
        int n = framesFor(rate);
        short[] s = new short[n * 2];
        for (int i = 0; i < n; i++) {
            short v = (short) Math.round(AMP * Math.sin(phaseAtRate(i, rate, F0, F1, SECONDS)));
            s[i * 2] = v;
            s[i * 2 + 1] = v;
        }
        return s;
    }

    /** 段与段之间插入的静音帧数。 */
    public static int gapFrames(int rate, double gapS) {
        return (int) Math.round(Math.max(0.0, gapS) * rate);
    }

    /**
     * {@code segs} 段扫频，段间插 {@code gapS} 秒静音，一次交给 AudioTrack。
     *
     * <p>★ 为什么要一次灌多段：本格的目的是把 `d_total` 拆成「冷启动」与「稳态」。
     * 同一个 AudioTrack、同一次 `play()`，第 2 段首帧的播放时刻就是
     * `tPlay + (SECONDS + gapS)`（由帧号推算，不依赖第二次 `play()`），
     * 于是「第 2 段 d_total − 第 1 段 d_total」= **纯粹的冷启动开销**，
     * 与 buffer 长度、重采样都无关。若每段都新建 AudioTrack，两者就分不开了。
     *
     * @param outStarts 长度 ≥ segs 时，写入每段首帧在返回数组里的**帧号**
     */
    public static short[] stereoWithGap(int rate, int segs, double gapS, int[] outStarts) {
        int n = framesFor(rate);
        int gap = gapFrames(rate, gapS);
        int s = Math.max(1, segs);
        int segFrames = n * s + gap * (s - 1);
        short[] out = new short[segFrames * 2];
        for (int k = 0; k < s; k++) {
            int base = k * (n + gap);
            if (outStarts != null && k < outStarts.length) outStarts[k] = base;
            for (int i = 0; i < n; i++) {
                short v = (short) Math.round(AMP * Math.sin(phaseAtRate(i, rate, F0, F1, SECONDS)));
                out[(base + i) * 2] = v;
                out[(base + i) * 2 + 1] = v;
            }
        }
        return out;
    }
}
