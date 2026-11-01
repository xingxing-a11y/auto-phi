package io.pgauto.probe;

/**
 * 标定用脉冲串的生成器。**纯函数、零 Android 依赖** —— 这样能被普通 JVM 直接自测
 * （`PulseSelfTest`），不必上真机就能验证「脉冲在哪个帧、多长、多响」。
 *
 * <p>用途：把「播放提交 → 音频被采集到」的延迟 `d1+d2` 变成可测量的量。
 * 上一格（FACTS §87）留下 APK 链路 **+129 ~ +190 ms** 的偏置拆不开，缺的正是这一块。
 *
 * <p>★ 为什么不用「host 发 adb 命令的时刻」当已知时刻：
 * host → adbd → 设备进程 fork → 执行，实测 30~100 ms 且抖动，与被测量同量级
 * ⇒ 等于用一把会抖的尺子量另一个会抖的量。改用设备自己的
 * {@code AudioTrack.getTimestamp()}（与录音侧同一套机制），两端都落到 elapsedRealtime 轴。
 *
 * <p>★ 为什么是 1050 Hz 而不是 1 kHz：44100/1050 = **42 整**，2 个周期 = 84 样本，
 * 首尾天然为 0（sin 的整数周期端点），不会在起止处产生宽带咔哒声。1 kHz 的周期是
 * 44.1 样本，取整后首尾有 0.17 的残差 ⇒ 会多出一层瞬态干扰 onset 定位。
 */
public final class PulseGen {

    public static final int RATE = 44100;

    /** 脉冲频率。选它的唯一原因是 44100/1050 = 42 整除（见类注释）。 */
    public static final int PULSE_HZ = 1050;

    /** 单个脉冲样本数 = 2 个整周期。 */
    public static final int PULSE_LEN = 84;

    /** 幅度 0.8 满幅。留 20% 余量，避免经过系统音效/EQ 后削波。 */
    public static final int AMP = 26214;

    /** 第一个脉冲的起点（帧）。留 0.5 s 前置静音，让播放启动瞬态过去。 */
    public static final int FIRST_OFF = 22050;

    /** 脉冲间隔（帧）= 100 ms。必须远大于 getTimestamp 的 1024 帧粒度。 */
    public static final int GAP = 4410;

    /** 脉冲个数。5 个才有离散度可看；单个无法判断结果是否可信。 */
    public static final int COUNT = 5;

    /** 整段长度（帧）= 1.5 s。 */
    public static final int TOTAL = 66150;

    private PulseGen() {
    }

    /** 第 {@code k} 个脉冲的**起点**帧偏移。 */
    public static int startOf(int k) {
        return startOf(k, FIRST_OFF, GAP);
    }

    /**
     * 参数化版本 —— 只为让自测能验证公式本身（而不是验证常量碰巧对）。
     *
     * <p>★ 变异测试要靠它：把 {@code gap} 改 1 帧，算出的偏移必须跟着变；
     * 若断言用的是常量而不是这个函数的结果，两种实现都会「通过」⇒ 断言无效。
     */
    public static int startOf(int k, int firstOff, int gap) {
        if (k < 0 || k >= COUNT) throw new IllegalArgumentException("k 越界：" + k);
        return firstOff + k * gap;
    }

    /**
     * 第 {@code k} 个脉冲的**中心**帧偏移。
     *
     * <p>PC 端在录音里定位脉冲时会拿「能量重心」当中心 ⇒ 两端用同一个定义，才能直接相减。
     */
    public static int centerOf(int k) {
        return startOf(k) + PULSE_LEN / 2;
    }

    /** 全部脉冲中心偏移（升序）。 */
    public static int[] centers() {
        int[] out = new int[COUNT];
        for (int k = 0; k < COUNT; k++) out[k] = centerOf(k);
        return out;
    }

    /**
     * 单声道脉冲串。脉冲之外严格为 0 —— 这样录音里的背景干净，PC 端找峰值不会被干扰。
     */
    public static short[] mono() {
        short[] s = new short[TOTAL];
        for (int k = 0; k < COUNT; k++) {
            int off = startOf(k);
            for (int i = 0; i < PULSE_LEN; i++) {
                double w = 2.0 * Math.PI * PULSE_HZ * i / RATE;
                s[off + i] = (short) Math.round(AMP * Math.sin(w));
            }
        }
        return s;
    }

    /** 立体声交织（左右相同）。AudioTrack 用 CHANNEL_OUT_STEREO。 */
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
        return TOTAL * 2 * 2;
    }

    /**
     * 「已经没必要再等」的帧位置 = 最后一个脉冲结束 + 100 ms 余量。
     *
     * <p>不必等到 {@link #TOTAL}（1.5 s）：脉冲都在前 0.91 s 内，多等 0.5 s 只是让
     * 标定实验变慢，不增加任何信息。
     */
    public static int endFrames() {
        return startOf(COUNT - 1) + PULSE_LEN + GAP;
    }
}
