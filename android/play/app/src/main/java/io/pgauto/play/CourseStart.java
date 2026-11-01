package io.pgauto.play;

/**
 * 课题模式：用**包络互相关**定位「下一首的曲首」，不依赖"第一个响点"。
 *
 * 为什么需要（2026-10-27 实测）：
 * 课题模式第 2/3 首是自动接续的，曲间那段过场/动画里一直有声音。于是
 * `OnsetGate` 的"第一个可信响点"要么根本等不到（没有 300 ms 静音），要么认到
 * 过场音上（零点偏 1~3 s）。而每一首的母带指纹（`manifest.env_b64`，前 30 s 的
 * 10 ms RMS 包络）本来就在包里 —— 拿它跟刚采到的包络做互相关，就能**直接量出**
 * 「母带 t=0 落在捕获窗口的哪个位置」，与"谁先响"无关。
 *
 * 与单曲模式的关系：K 标定早就在用同一套 `EnvMatcher`（见 `computeKOnDevice`），
 * 区别只在于那里 center 是"由闸门帧推出来的"，而这里 center 是"由上一首结束时间推出来的"，
 * 搜索半径也大得多（曲间最长可能差十几秒）。
 *
 * ⛔ 不要改成"取互相关最大峰"就完事：`EnvMatcher` 内部已经做了 trim + 抛物线插值 +
 *    唯一性(`quality`) 判定，直接用它的结论，别再自己写一套。
 */
public final class CourseStart {

    /** 互相关的采纳门槛：与 `computeKOnDevice` 的第二档一致（强峰 / 弱峰但 K 合理）。 */
    public static final double CC_MIN = 0.70;
    public static final double QUALITY_MIN = 1.00;

    /** 一次定位的结果。 */
    public static final class Hit {
        /** 母带 t=0 在捕获窗口里的位置（ms，可负 = 窗口开始时已经开唱）。 */
        public final double startMs;
        /** 相关系数（0~1）。 */
        public final double cc;
        /** 峰的唯一性（越大越唯一；≥1.10 视为强峰）。 */
        public final double quality;
        /** 是否可信（强峰，或弱峰但 cc/quality 过第二档门槛）。 */
        public final boolean ok;
        /** 不可信时的人类可读原因。 */
        public final String why;

        Hit(double startMs, double cc, double quality, boolean ok, String why) {
            this.startMs = startMs;
            this.cc = cc;
            this.quality = quality;
            this.ok = ok;
            this.why = why;
        }

        @Override
        public String toString() {
            return String.format(java.util.Locale.ROOT,
                    "start=%.0fms cc=%.3f quality=%.2f ok=%s%s",
                    startMs, cc, quality, ok, ok ? "" : "（" + why + "）");
        }
    }

    /**
     * @param recMs        捕获窗口的包络（逐帧 RMS），第 0 帧 = 窗口起点
     * @param ref          母带指纹（`Song.envRms()`），第 0 帧 = 母带 t=0
     * @param hopMs        每帧毫秒数（母带指纹的 `env_hop_ms`，通常 10）
     * @param centerFrames 预期的曲首帧号（由"上一首结束 + 5 s"推出）
     * @param halfFrames   搜索半径（帧）；曲间误差可能十几秒 ⇒ 给 500~1500
     */
    public static Hit locate(double[] recMs, double[] ref, int hopMs,
                            int centerFrames, int halfFrames) {
        if (recMs == null || ref == null || recMs.length == 0 || ref.length == 0) {
            return new Hit(0, 0, 0, false, "输入为空");
        }
        if (hopMs <= 0) {
            return new Hit(0, 0, 0, false, "hop 非法：" + hopMs);
        }
        EnvMatcher.Result m = EnvMatcher.match(recMs, ref, centerFrames, halfFrames);
        if (m == null) {
            return new Hit(0, 0, 0, false, "匹配器返回 null");
        }
        final boolean ok = m.ok || (m.cc >= CC_MIN && m.quality >= QUALITY_MIN);
        final String why = m.ok ? "" : (m.why == null ? "弱峰" : m.why);
        return new Hit(m.startMs(hopMs), m.cc, m.quality, ok, why);
    }

    private CourseStart() {
    }
}
