package io.pgauto.play;

/**
 * 「曲首第一个音」选点状态机 —— **纯 Java，零 Android 依赖**（PC 上可 javac + java 单测）。
 *
 * 要解决的问题：`AudioPlaybackCapture` 抓到的包络里，曲首第一个音之前还有**不是它**的声：
 *   · 用户点 ▶ 的点击音（Phigros 有 UI 音效）；
 *   · 游戏内过场/加载的提示音。
 * 直接把「第一个 onset」当零点，会偏几十毫秒到几秒，而且**不报错** ——
 * 症状是整轮全 Miss，看起来像「注入坏了」。
 *
 * 判据（两条同时成立才认）：
 *   ① **安静够久**：连续 {@code quietMs} 毫秒的帧能量低于 {@code quietRms}
 *      ⇒ 前面那段声已经结束，接下来出现的才是新的起音；
 *   ② **已过静默期**：帧时刻 ≥ `gateStart + minLeadMs` ⇒ 挡掉点击音那一类。
 * 满足①②之后出现的**第一个 onset** 就是曲首第一个音。
 *
 * ★ 为什么不用「点击后固定延迟 ± 窗口」：那等于把 `PlayService.DEFAULT_LEAD_MS` 换个写法
 *   继续用，跨曲能差 300+ ms —— 那正是本步要拆掉的东西。
 * ★ 为什么不在这里再套一层幅度/比值阈值：`OnsetDetector` 已经做了
 *   （loud / over / rising / spaced）。本类只回答「**哪个 ** onset 算数」，
 *   两件事分开才能分别单测。
 * ★ 线程模型：帧由采集线程喂（`onFrame`），`start(...)` 由等待方在点 ▶ 那一刻调
 *   ⇒ `gateStartNs` 必须是 volatile，否则等待方读到的可能是旧值（表现为「窗永远没开」）。
 */
public final class OnsetGate {

    /** 还在等「安静够久」。 */
    public static final int WAIT_QUIET = 0;
    /** 安静够了，正在等那个起音。 */
    public static final int ARMED = 1;
    /** 已锁定。 */
    public static final int FIRED = 2;

    /** 需要连续多长的静音才认「前面那段声结束了」。300 ms 远小于曲首静音（≥0.8 s）。 */
    public static final int DEFAULT_QUIET_MS = 300;
    /** 静音判据的 RMS 幅度。与 `OnsetDetector.DEFAULT_ABS_FLOOR_RMS` 同口径（300 ⇒ −41 dBFS）。 */
    public static final double DEFAULT_QUIET_RMS = 300.0;
    /**
     * 点 ▶ 之后多久才接受 onset。
     *
     * 来源：Phigros 的 UI 点击音落在 0~几百毫秒内；而本工程的曲目从点 ▶ 到第一个音
     * 都在 8 s 上下（`d_ideal` 7.13~7.48 s + `sil_ref` 0.86 s）。取 1.5 s 给点击音留足
     * 余量，又离第一个音远得多 —— 两端都不紧张。
     */
    public static final int DEFAULT_MIN_LEAD_MS = 1500;

    /**
     * 绝对阈值模式的持续确认窗（毫秒）—— 与 PC 侧 `tb_audio_edge.SUSTAIN_MS` **同值**。
     *
     * 为什么是 30 ms：PC 侧的边沿判据是「首帧超阈 + 其后 30 ms 仍超阈」，用来否决瞬时尖峰
     * （击打音效/采集毛刺，实测曾把边沿骗早 169 ms）。两侧窗长必须相同，否则边沿又被定义在
     * 不同位置 —— 那正是本类要消灭的那类偏差。
     */
    public static final int DEFAULT_SUSTAIN_MS = 30;

    private final int frameMs;
    private final double quietE;
    private final int quietFrames;
    private final long minLeadNs;
    /**
     * 绝对阈值（RMS）。**0 = 关闭** ⇒ 退回相对判据（`OnsetDetector` 的 onset 标记）。
     *
     * ★ 存在的理由（2026-10-25，FACTS §169）：`sil_ref` 是**按母带阈值**定义的边沿，
     *   而相对判据（5×背景）在渐入曲上会晚几百毫秒才触发。母带的阈值 `sil_thr` 由
     *   `pack_assets.py` 写进 manifest，本类用它就能把两侧的边沿定义**对齐**。
     *   ⛔ 未标定（0）时**必须**退回相对判据，不许编一个默认阈值 —— 那是拿别的曲的常数打分。
     */
    private final double absThrE;
    private final double absThrRms;
    /** 连续超阈多少帧才认（`ceil(sustainMs / frameMs)`，至少 1）。 */
    private final int sustainFrames;

    /** 采集启动时刻（ns）。`start(...)` 没调用时用它当基准。 */
    private final long originNs;
    private volatile long gateStartNs;

    private volatile int state = WAIT_QUIET;

    /**
     * 强制进入 ARMED（2026-10-27，课题模式的曲间休息用）。
     *
     * ★ 为什么需要：正常模式里采集从"点 ▶ 之后"开始，暂停界面必然有 300 ms 静音，
     *   所以 WAIT_QUIET → ARMED 总能发生。但课题模式的第 2/3 首是在**过场音乐/动画**里
     *   重新待命的 —— 过场一直在响，"连续 300 ms 静音"这个条件可能永远不成立
     *   ⇒ gate 永远不 ARMED ⇒ 曲首都认不出来（实测症状：后两首直接不打）。
     *   强制待命的代价是"可能被过场音效误触发"，这由调用方的 `notBefore` 下界过滤。
     */
    public void forceArm() {
        if (!fired) {
            state = ARMED;
        }
    }

    /** 是否已进入 ARMED（诊断/日志用）。 */
    public boolean armed() {
        return state == ARMED;
    }

    /** 当前连续静音帧数（诊断用）。 */
    public long quietRunNow() {
        return quietRun;
    }

    private volatile long quietRun = 0;
    /**
     * ★ 必须是 volatile，而且**必须是 {@link #onFrame} 里最后一个写的字段**。
     *
     * 为什么（这一条是自测抓不到的 bug，靠读代码才发现）：`onFrame` 在采集线程跑，
     * `OnsetZero.await()` 在等待线程里 `while (true) { if (gate.fired()) break; ... }`。
     * 非 volatile 的 boolean 读可以被 JIT 提到循环外 ⇒ 采集侧明明检出了 onset，
     * 等待侧永远读到 false，最后以「等待超时」失败 —— 而超时日志里的 `diag()` 会写着
     * `firedFrame=-1`（等待线程自己也读不到），**看起来像「根本没听见音」**。
     *
     * 为什么写序也重要：读者看到 `fired == true` 之后立刻读 `firedAtNs`。若 `fired = true`
     * 先写、`firedAtNs` 后写，volatile 只保证「写 fired 之前的操作可见」，
     * 读者可能拿到 0 ⇒ 零点落在 1970 年（`zeroRelMs` 会是巨大的负数，虽有守卫拦截，
     * 但那是把一个本不该发生的错误当成正常路径）。
     * ⇒ 先算局部量、一次写完所有结果字段，最后才置 `fired = true`。
     */
    private volatile boolean fired = false;
    private volatile long firedAtNs = 0;
    private volatile long firedFrame = -1;
    private volatile double firedE = 0;
    private volatile int ignoredEarly = 0;      // 窗外的 onset（点击音等）
    private volatile int ignoredNotArmed = 0;   // 窗内但安静不够（持续音里的起伏）
    private volatile long quietRunMax = 0;

    // ── 绝对阈值模式的运行时状态 ────────────────────────────────────────────
    /** 当前这一段「连续超阈」已累计的帧数（0 = 没在段内）。 */
    private volatile int absRun = 0;
    /** 这一段的首帧号/首帧中点/首帧能量 —— 锁定时要**回填首帧**，不是最后一帧。 */
    private volatile long absFirstFrame = -1;
    private volatile long absFirstMid = 0;
    private volatile double absFirstE = 0;
    /** 这一轮是被哪条判据锁定的：`"onset"`（相对）/ `"abs"`（母带阈值）/ `""`（还没锁）。 */
    private volatile String firedBy = "";

    /**
     * ★★ 2026-10-24 新增：**真实信号**的三个统计量。
     *
     * 为什么需要它们：旧实现用 `AudioManager.getStreamVolume()` 当闸门（音量 0 就拒绝开跑），
     * 那是拿 proxy 判（项目教训 11 的第二次犯）。实测反证：`bounded_in_r4` 在音量 0 下
     * 录到 **peak 32768 / −32768**（满幅）并打出 φ；抽查 18 份历史录音**全部**满幅。
     * ⇒ 本机 capture 取的是**音量缩放前**的渲染侧，那 0 档也能用。
     *
     * 现在的判据：不预判，**事后按实测说话** —— 一帧非零样本都没有才算真静音。
     */
    private volatile long frames = 0;            // 收到的帧数
    private volatile double peakMeanSquare = 0;  // 见过的最大帧能量（0 ⇒ 从没有过非零样本）
    private volatile long zeroFrames = 0;        // 能量**严格等于 0** 的帧数

    public OnsetGate(int frameMs, double quietRms, int quietMs, int minLeadMs, long originNs) {
        this(frameMs, quietRms, quietMs, minLeadMs, originNs, 0.0, 0);
    }

    /**
     * @param absThrRms  绝对阈值（RMS）。**≤0 = 相对判据**（旧行为，逐位不变）
     * @param sustainMs  绝对模式下连续超阈的确认窗（毫秒）；≤0 时退回 1 帧
     */
    public OnsetGate(int frameMs, double quietRms, int quietMs, int minLeadMs, long originNs,
                     double absThrRms, int sustainMs) {
        if (frameMs <= 0) throw new IllegalArgumentException("frameMs 必须 > 0，得到 " + frameMs);
        if (quietRms < 0) throw new IllegalArgumentException("quietRms 不能为负：" + quietRms);
        if (quietMs < 0) throw new IllegalArgumentException("quietMs 不能为负：" + quietMs);
        if (minLeadMs < 0) throw new IllegalArgumentException("minLeadMs 不能为负：" + minLeadMs);
        // ★ 阈值只允许「关」或「正数」：NaN/负数都是上游算错（单位/字段名），构造期就炸，
        //   别让它变成「永远不触发」或「一帧就触发」这种静默失效。
        if (Double.isNaN(absThrRms)) {
            throw new IllegalArgumentException("absThrRms 是 NaN（上游阈值算错）");
        }
        if (absThrRms < 0) {
            throw new IllegalArgumentException("absThrRms 不能为负：" + absThrRms
                    + "（0 表示关闭 / 走相对判据）");
        }
        this.frameMs = frameMs;
        this.absThrRms = absThrRms;
        this.absThrE = absThrRms * absThrRms;
        // ★ 绝对模式下「安静」必须用**母带阈值**定义（与 PC 侧 `quiet = r < thr` 同口径）：
        //   否则阈值低的曲目（thr=80 那类）会把「80~300 RMS」的真实起音当成静音，
        //   一直等到更响的地方才 ARMED ⇒ 边沿又被定义在另一个位置。
        //   相对模式仍用 `quietRms`（300 RMS，旧行为逐位不变）。
        this.quietE = absThrRms > 0 ? absThrE : quietRms * quietRms;
        // 向上取整：quietMs=300 / frameMs=10 ⇒ 30 帧；quietMs=305 ⇒ 31 帧（宁严不松）
        this.quietFrames = (quietMs + frameMs - 1) / frameMs;
        this.minLeadNs = minLeadMs * 1_000_000L;
        this.originNs = originNs;
        this.gateStartNs = originNs;
        this.sustainFrames = absThrRms > 0
                ? Math.max(1, (Math.max(sustainMs, 0) + frameMs - 1) / frameMs)
                : 0;
    }

    /** 用全部默认参数（{@code DEFAULT_*}）构造 —— 相对判据。 */
    public static OnsetGate withDefaults(int frameMs, long originNs) {
        return new OnsetGate(frameMs, DEFAULT_QUIET_RMS, DEFAULT_QUIET_MS,
                DEFAULT_MIN_LEAD_MS, originNs);
    }

    /**
     * 绝对阈值模式：边沿 = 首帧 RMS ≥ `thrRms` 且其后连续 {@link #DEFAULT_SUSTAIN_MS} 仍 ≥ 它。
     *
     * ★ 与 PC 侧 `tb_audio_edge.find_silence_end(thr=…)` 同口径（同阈值、同 30 ms 持续窗），
     *   差别只有帧网：端内 10 ms 帧、PC 5 ms 候选点 ⇒ 允许 ±10 ms 的量化差。
     */
    public static OnsetGate withEdge(int frameMs, double thrRms, long originNs) {
        return new OnsetGate(frameMs, DEFAULT_QUIET_RMS, DEFAULT_QUIET_MS,
                DEFAULT_MIN_LEAD_MS, originNs, thrRms, DEFAULT_SUSTAIN_MS);
    }

    /**
     * 点 ▶ 的那一刻调用。**不重置静音累计** —— 点之前的静音同样算「安静够久」的证据
     * （点击音若真的响，会自己把累计清零，见 {@link #onFrame}）。
     */
    public void start(long tapWallNs) {
        this.gateStartNs = tapWallNs;
    }

    /**
     * 喂一帧。**必须按 frameIdx 递增顺序调用**（采集线程单线程）。
     *
     * @param frameIdx  帧号（仅诊断用）
     * @param meanSquare 该帧 mean(x^2)
     * @param onset      `OnsetDetector` 给的 onset 标记
     * @param frameMidNs 该帧中点的挂钟时刻（用 {@link FrameClock#frameMidNs} 算）
     */
    public void onFrame(long frameIdx, double meanSquare, boolean onset, long frameMidNs) {
        if (fired) return;                       // 幂等：锁定后不再改

        // ★ 先记统计量再判状态：即使这一帧注定被忽略（窗外/没 ARMED），
        //   它也证明「采集到过声音」—— 这正是区分「真静音」与「听见了但没认」的唯一依据。
        frames++;
        if (meanSquare > peakMeanSquare) peakMeanSquare = meanSquare;
        if (meanSquare == 0.0) zeroFrames++;     // 精确 0 = 该帧每一个样本都是 0

        final long gs = gateStartNs;
        final boolean quiet = meanSquare < quietE;
        final long prevQuietRun = quietRun;

        if (quiet) {
            quietRun++;
            if (quietRun > quietRunMax) quietRunMax = quietRun;
        } else {
            quietRun = 0;
        }

        final boolean inWindow = frameMidNs >= gs + minLeadNs;

        // ① 先尝试升级到 ARMED —— 用**上一帧为止**的静音累计。
        //    ★ 顺序很重要：若「安静满 300 ms 的下一帧」正好就是起音，本帧 quiet=false
        //      已经把自己清零了；用清零后的值判会永远 ARMED 不了 ⇒ 那个音被永久错过。
        if (state == WAIT_QUIET && prevQuietRun >= quietFrames && inWindow) {
            state = ARMED;
        }

        // ② 绝对阈值模式（有母带阈值时）：连续超阈够久就锁定，`onset` 标记完全不用。
        //    ★ 窗外不累计：一个跨窗边界的持续声（点击音尾巴）不该被算成半个 run。
        if (absThrRms > 0) {
            if (!inWindow) {
                absRun = 0;
                return;
            }
            if (meanSquare < absThrE) {
                absRun = 0;
                return;
            }
            if (absRun == 0) {
                absFirstFrame = frameIdx;
                absFirstMid = frameMidNs;
                absFirstE = meanSquare;
            }
            absRun++;
            if (absRun < sustainFrames) return;
            if (state != ARMED) {
                // 有超阈段但前面没安静够 ⇒ 不认（与相对判据同一条前提）
                ignoredNotArmed++;
                absRun = 0;
                return;
            }
            // ★ 回填**首帧**：边沿是这一段的起点，不是确认时刻。
            //   回填末帧会让零点恒定偏后 (sustainFrames-1)×frameMs —— 静默偏差，最难查。
            firedAtNs = absFirstMid;
            firedFrame = absFirstFrame;
            firedE = absFirstE;
            firedBy = "abs";
            state = FIRED;
            fired = true;
            return;
        }

        if (!onset) return;

        // ② 窗外（点击音/过场音）：不算零点，而且**把「安静」前提作废** ——
        //    它打破了静音段，之后必须重新安静够久才配认下一个音。
        if (!inWindow) {
            ignoredEarly++;
            quietRun = 0;
            if (state == ARMED) state = WAIT_QUIET;
            return;
        }

        if (state != ARMED) {
            ignoredNotArmed++;
            return;
        }

        // ★ 先把全部结果写完，**最后**才置 `fired`（见字段注释里的写序要求）。
        //   任何「先 fired=true 再补字段」的写法都会让读者拿到 firedAtNs=0。
        firedAtNs = frameMidNs;
        firedFrame = frameIdx;
        firedE = meanSquare;
        firedBy = "onset";
        state = FIRED;
        fired = true;
    }

    public int state() { return state; }
    public boolean fired() { return fired; }
    public long firedAtNs() { return firedAtNs; }
    public long firedFrame() { return firedFrame; }
    public double firedE() { return firedE; }
    /** 走的是哪条判据：`"onset"` = 相对（旧），`"abs"` = 母带阈值，`""` = 还没锁定。 */
    public String firedBy() { return firedBy; }
    /** 绝对阈值（RMS）；0 = 本门用的是相对判据。 */
    public double absThrRms() { return absThrRms; }
    /** 绝对模式的持续确认窗（帧数）；相对模式为 0。 */
    public int sustainFrames() { return sustainFrames; }
    /** 边沿口径一行文本（日志/界面共用，⛔ 别在别处再拼一遍）。 */
    public String edgeNote() {
        if (absThrRms <= 0) return "相对（5×背景，无母带阈值）";
        return String.format(java.util.Locale.ROOT,
                "sil_thr %.1f RMS / 连续 %d 帧（%d ms）", absThrRms, sustainFrames,
                sustainFrames * frameMs);
    }
    /** 被当成「窗外」忽略的 onset 数（>0 通常意味着点 ▶ 的点击音被正确挡掉了）。 */
    public int ignoredEarlyCount() { return ignoredEarly; }
    /** 窗内但安静不够而放过的 onset 数（持续音起伏）。 */
    public int ignoredNotArmedCount() { return ignoredNotArmed; }
    public long quietRunMax() { return quietRunMax; }
    public int quietFramesNeeded() { return quietFrames; }
    /** 收到过多少帧（0 ⇒ 采集线程一帧都没喂进来）。 */
    public long frames() { return frames; }
    /** 见过的最大帧能量（0 ⇒ 一帧非零样本都没有）。 */
    public double peakMeanSquare() { return peakMeanSquare; }
    /** 能量严格为 0 的帧数。 */
    public long zeroFrames() { return zeroFrames; }

    /**
     * 这一轮**到底有没有采到声音**。`false` ⇒ 每一帧能量都精确为 0 ⇒ 真静音。
     *
     * ★ 这是替代「媒体音量 == 0 就拒绝」的唯一判据：不预判、按实测。
     * 变异点：把 `zeroFrames == frames` 改成 `frames > 0`（恒真）⇒ t18/t19 红。
     */
    public boolean allZero() { return frames > 0 && zeroFrames == frames; }

    /**
     * 给异常文本的一句人话结论。**纯字符串**，单测可以断言关键词。
     *
     * 用途：TIMEOUT / DIED 时把「真静音」与「有信号但没满足静音+起音模式」分开 ——
     * 前者是环境问题（音量/被采集应用没在播），后者是判据/曲目问题，修法完全不同。
     */
    public String signalVerdict() {
        if (frames == 0) {
            return "一帧都没采到（采集线程没跑起来 / 投影立刻被撤）";
        }
        if (allZero()) {
            return "整轮全 0：采到 " + frames + " 帧，能量全部精确为 0 ⇒ 真静音"
                    + "（被采集应用是否真在播？本机实测音量 0 也能录到满幅，别急着调音量）";
        }
        return "有信号：最大帧能量 " + fmtE(peakMeanSquare)
                + "、全 0 帧 " + zeroFrames + "/" + frames
                + "（有声音，但没检出符合「静音 + 起音」模式的 onset）";
    }

    private static String fmtE(double e) {
        return String.format(java.util.Locale.ROOT, "%.4g", e);
    }

    /** 给日志的一行诊断（真机标定时靠它区分「没听见音」和「听见了但没认」）。 */
    public String diag() {
        return "gate(state=" + stateName(state)
                + ", 边沿=" + edgeNote() + (fired ? "，锁定于 " + firedBy : "")
                + ", 静音需要 " + quietFrames + " 帧, 最长静音 " + quietRunMax + " 帧"
                + ", 窗外忽略 " + ignoredEarly
                + ", 安静不足忽略 " + ignoredNotArmed
                + ", 帧 " + frames + "（全 0 " + zeroFrames + "，峰值能量 " + fmtE(peakMeanSquare) + "）"
                + ", firedFrame=" + firedFrame + ")";
    }

    public static String stateName(int s) {
        switch (s) {
            case WAIT_QUIET: return "WAIT_QUIET";
            case ARMED: return "ARMED";
            case FIRED: return "FIRED";
            default: return "?" + s;
        }
    }
}
