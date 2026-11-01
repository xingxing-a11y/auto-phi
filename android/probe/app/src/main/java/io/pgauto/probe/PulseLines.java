package io.pgauto.probe;

/**
 * sidecar `P` 行的格式化。**纯函数、零 Android 依赖** —— 目的是让「P 行长什么样」
 * 能被纯 JVM 自测，而不是只能上真机跑一遍再肉眼数字段。
 *
 * <p>★ 为什么把格式化单独抽出来：格式错（少一个制表符、字段顺序反）在真机上不会报错，
 * 只会让 PC 端解析器得到错的时间戳 —— 那种错误的表现是「延迟算出来 47 s」，
 * 排查成本极高。格式化集中在一处、可自测，是最便宜的对策。
 *
 * <p>格式（与 `RecorderService` 头注释必须一致）：
 * <pre>
 *   P S 0 &lt;帧数&gt; &lt;音量&gt; &lt;音量上限&gt; &lt;tPlay&gt;      pre=..ms,wr=..ms
 *   P T &lt;轮询序&gt; &lt;tReadPlay&gt; &lt;hwOk&gt; &lt;framePos&gt; &lt;hwNs&gt;
 *   P E 0 &lt;末帧位置&gt; &lt;tRead&gt;  tried=..,ok=..,vol=../..
 * </pre>
 */
public final class PulseLines {

    /** 字段分隔符。用制表符与既有 B/E/O 行保持一致。 */
    public static final char SEP = '\t';

    private PulseLines() {
    }

    /** 播放提交（`S` 行）。时间单位一律纳秒。 */
    public static String start(long tPlay, int frames, int vol, int volMax,
                              long preMs, long writeMs) {
        return start(tPlay, frames, vol, volMax, vol, preMs, writeMs);
    }

    /**
     * 播放提交（`S` 行），带「原本音量」。
     *
     * <p>★ 为什么必须记原值：2026-10-19 实测媒体音量 0 时录音**整轮全 0**，而播放不报错。
     * 播放器会把音量临时提起来（见 {@code PulsePlayer}），若不在行里留 `volPre`，
     * 事后就分不清「本来就 0（已临时提过）」和「真的全程 0 音量」。
     */
    public static String start(long tPlay, int frames, int volUsed, int volMax,
                              int volOrig, long preMs, long writeMs) {
        return "P" + SEP + "S" + SEP + "0" + SEP + frames + SEP + volUsed + SEP + volMax
                + SEP + tPlay + SEP + "pre=" + preMs + "ms,wr=" + writeMs + "ms,volPre=" + volOrig;
    }

    /** 一次时间戳轮询（`T` 行）。`ok=false` 时 frame / ns 写 -1，不留空字段（空字段最容易解析出错）。 */
    public static String tick(int seq, long tReadPlay, boolean ok, long frame, long ns) {
        return "P" + SEP + "T" + SEP + seq + SEP + tReadPlay + SEP + (ok ? 1 : 0)
                + SEP + (ok ? frame : -1) + SEP + (ok ? ns : -1);
    }

    /** 播放结束（`E` 行）。 */
    public static String end(long maxFrame, long tRead, int tried, int ok, int vol, int volMax) {
        return "P" + SEP + "E" + SEP + "0" + SEP + maxFrame + SEP + tRead
                + SEP + "tried=" + tried + ",ok=" + ok + ",vol=" + vol + "/" + volMax;
    }
}
