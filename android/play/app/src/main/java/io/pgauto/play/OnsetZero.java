package io.pgauto.play;

import android.os.SystemClock;

import java.util.Locale;
import java.util.function.BooleanSupplier;

/**
 * S1-d：**端内 onset 定零点** —— `zero = onset − sil_ref − L_dev`。
 *
 * 与 S1-c 的 {@link ZeroSource.CountdownTap} 的差别只有「零点从哪来」：
 * ```text
 *   S1-c：zero = 点击墙钟 + 7479.88 ms   ← lead 是本曲实测常数，跨曲差 300+ ms
 *   S1-d：zero = 曲首第一个音的挂钟 − 母带静音长 − 端内滞后
 * ```
 * ★ **顺序不可换**：必须先 `tap.start()`（开采集）再点 ▶。
 *   反过来会漏掉点击之后的静音段 —— 静音段正是 {@link OnsetGate} 判「哪个 onset 算数」的依据。
 * ★ **失败不降级**：超时/采集报错一律抛。绝不回退到 `tap + 固定 lead` ——
 *   lead 是**曲目属性**（bounded_in 7.52 s vs 其余 7.13~7.34 s，跨曲差 300+ ms），
 *   回退等于「用别的曲的常数打分」，症状是整轮偏而日志写着「成功」。宁可当场失败让人看见。
 * ★ 时钟口径：gate 全程用 `elapsedRealtimeNanos`（单调），转 epoch 只在**点击那一刻**
 *   取两个钟配对一次（`tapEpoch − tapElapsed`）。不要用「等待结束时」的配对 ——
 *   中间隔了十几秒，NTP 校时会把它变成常数偏移。
 */
public final class OnsetZero implements ZeroSource {

    /**
     * 端内滞后首版 0.050 s。**这是唯一一个还没标定过的常数**，其余量都有出处：
     *   · `sil_ref` 来自 assets（母带属性，换设备不变）；
     *   · `onset` 由 AudioPlaybackCapture 实测。
     *
     * 它吸收的是「从混音输出到判定被处理」这一段：采集链路的缓冲积压、`read()` 的
     * 阻塞粒度（10 ms 帧量化的一半 ≈ 5 ms）、注入侧固定滞后。
     * 按 PC 侧的账（`L` 按运输路径分账，apk 路径 0.2337 s 含 PC→adb 那一跳）
     * 端内应该**明显小于**它，但具体值只能真机标定。
     * ⇒ 启动参数可覆盖（`PlayService.EXTRA_L_DEV_S`），标定后不必重装 APK。
     */
    public static final double DEFAULT_L_DEV_S = 0.050;

    /** 点 ▶ 之后最多等这么久。曲首第一个音在 8 s 上下（d_ideal 7.1~7.5 + sil_ref 0.86）。 */
    public static final int DEFAULT_DEADLINE_MS = 20_000;

    private final OnsetTap tap;
    private final Tapper tapper;
    private final int countdownS;
    private final double silRefS;
    private final double lDevS;
    private final float x;
    private final float y;
    private final BooleanSupplier stop;
    private final int deadlineMs;
    /** 标定尾：锁定后继续采这么多帧（0 = 生产行为，锁定即返回）。 */
    private final int calibFrames;
    /**
     * 锁定后**不**关闭采集（由调用方负责 stop）。
     *
     * ★ 用途（自动标定 A 方案）：普通打歌轮让采集继续跑 30 s，跑完在端内算 K 存给下一轮，
     *   用户就再也不用手动标定。风险是采集与注入并行 —— 必须用 trace 的 lag 分布验证
     *   没有扰动注入（判据：关键 lag mean/sd 与基准 7.07/7.28 ms 一致）。
     */
    private boolean keepTapAlive;
    // ── begin()/finish() 之间要留着的状态（两段锚用）──
    private boolean started;
    private long tapElapsedNs;
    private long tapEpochMs;
    private OnsetGate gate;
    /** 实测零点相对点击的偏移（= lead）。finish() 算完后写这里。 */
    private double zeroRel = Double.NaN;
    /**
     * 本轮实测出来的零点参数（写进注入 trace 的 `# run` 行）。
     *
     * ★ 打完之后 `L_dev` / `onset` / 判据在 logcat（被游戏冲掉）与界面日志（只 16 行）里
     *   都取不到 ⇒ 产物必须自带参数（FACTS §171.2 的取证缺口）。
     */
    private volatile String meta = "";

    public OnsetZero(OnsetTap tap, Tapper tapper, int countdownS,
                     double silRefS, double lDevS, float x, float y,
                     BooleanSupplier stop) {
        this(tap, tapper, countdownS, silRefS, lDevS, x, y, stop, DEFAULT_DEADLINE_MS);
    }

    public OnsetZero(OnsetTap tap, Tapper tapper, int countdownS,
                     double silRefS, double lDevS, float x, float y,
                     BooleanSupplier stop, int deadlineMs) {
        this(tap, tapper, countdownS, silRefS, lDevS, x, y, stop, deadlineMs, 0);
    }

    /**
     * @param calibFrames &gt;0 = **标定尾**：锁定 onset 后继续采到这么多帧再返回（0 = 生产行为不变）。
     *
     * ★ 为什么标定必须走这条链（2026-10-25 第二次踩）：标定的第一次实现让**用户自己点 ▶**，
     *   结果门的 1.5 s minLead 抓到了游戏的 UI 音（捕获 1.77 s 处），而不是曲首 —— `K` 直接变成
     *   −8.9 s 的垃圾。而生产路径是 App 自己点 ▶，UI 音落在 minLead 之前被挡掉。
     *   ⇒ 标定必须**复用生产路径去点 ▶**，只是点完之后不注入。
     */
    public OnsetZero(OnsetTap tap, Tapper tapper, int countdownS,
                     double silRefS, double lDevS, float x, float y,
                     BooleanSupplier stop, int deadlineMs, int calibFrames) {
        if (tap == null) throw new IllegalArgumentException("tap == null");
        if (tapper == null) throw new IllegalArgumentException("tapper == null");
        if (!(silRefS >= 0) || silRefS > 100) {
            // 单位写错（把 ms 当 s 传）会让零点偏 1000 倍，且不报错 ⇒ 构造期就炸
            throw new IllegalArgumentException("silRefS 应在 [0,100] 秒，得到 " + silRefS);
        }
        if (!(Math.abs(lDevS) <= 5)) {
            throw new IllegalArgumentException("lDevS 应在 ±5 秒内，得到 " + lDevS);
        }
        if (deadlineMs <= 0) throw new IllegalArgumentException("deadlineMs 必须 > 0");
        this.tap = tap;
        this.tapper = tapper;
        this.countdownS = Math.max(0, countdownS);
        this.silRefS = silRefS;
        this.lDevS = lDevS;
        this.x = x;
        this.y = y;
        this.stop = stop == null ? () -> false : stop;
        this.deadlineMs = deadlineMs;
        if (calibFrames < 0) throw new IllegalArgumentException("calibFrames 不能为负：" + calibFrames);
        this.calibFrames = calibFrames;
    }

    /**
     * 点 ▶ 并把「点击时刻 → 谱面 t=0」的两种估计都准备好，**立即返回**（不等 onset）。
     *
     * ★ 为什么要把 await 拆开（B 方案）：两段锚必须在点 ▶ 之后**立刻**开始派发盲区事件，
     *   而实测零点要等 ~0.35 s 的 onset。绑在一起的 await() 只能二选一。
     *
     * @return 点击时刻（墙钟 epoch ms，与 {@link #finish()} 返回的零点同一基准）
     */
    public double begin() throws Exception {
        countdown();
        tap.start();
        started = true;
        RunLog.line("onset 采集已就绪：" + tap.rateNote() + " · 边沿=" + tap.edgeNote());

        gate = tap.gate();
        // ★ 两个钟只在**这一刻**配对：之后的换算全走单调时钟。
        tapElapsedNs = SystemClock.elapsedRealtimeNanos();
        tapEpochMs = System.currentTimeMillis();
        gate.start(tapElapsedNs);

        long t0 = SystemClock.elapsedRealtimeNanos();
        String err = tapper.tap(x, y);
        if (err != null) throw new IllegalStateException("自拍失败：" + err);
        double tapCostMs = (SystemClock.elapsedRealtimeNanos() - t0) / 1e6;
        RunLog.line(String.format(Locale.ROOT,
                "已点 ▶（注入耗时 %.1f ms）⇒ 等曲首 onset（窗 ≥ +%d ms，超时 %d ms）",
                tapCostMs, OnsetGate.DEFAULT_MIN_LEAD_MS, deadlineMs));
        return tapEpochMs;
    }

    /** 等 onset 并算出实测零点（epoch ms）。{@link #begin()} 之后调用。 */
    public double finish() throws Exception {
        if (!started) throw new IllegalStateException("finish() 之前必须先 begin()");
        try {
            // ★ 判定逻辑全在 OnsetWait（纯函数）：它会在「据 alive/超时下结论」之前
            //   **复查 fired** —— 采集线程「写 fired → break → 写 recording=false」不是原子的，
            //   在两步之间读到就会把已锁到的一轮判成失败（10-24 修，见 OnsetWait 类注释）。
            BooleanSupplier fired = gate::fired;
            waitLoop:
            while (true) {
                String tapErr = tap.error();
                long waitedMs = (SystemClock.elapsedRealtimeNanos() - tapElapsedNs) / 1_000_000L;
                switch (OnsetWait.step(fired, tapErr, tap.alive(),
                        stop.getAsBoolean(), waitedMs, deadlineMs)) {
                    case FIRED:
                        break waitLoop;
                    case STOPPED:
                        throw new Stopped("等待 onset 期间中止");
                    case ERROR:
                        throw new IllegalStateException(tapErr);
                    case DIED:
                        // ★ 2026-11-01：首句改成「发生了什么 + 该怎么办」，技术诊断接在后面。
                        //   diag() 已打印静音帧数/窗外忽略等计数，故不再复述那两句解释。
                        throw new IllegalStateException("录屏被系统中断了（一帧都没采到 = 录屏授权没生效，"
                                + "或被别的录屏/投屏抢走）⇒ 重新点「开始演奏」，弹窗里允许录屏"
                                + "｜实测：" + gate.signalVerdict() + "｜诊断：" + gate.diag());
                    case TIMEOUT:
                        // ★ 先给「到底有没有声音」的实测结论，再给模式诊断 —— 两种情况修法完全不同：
                        //   真静音 = 环境问题；有信号但没认 = 判据/曲目问题。
                        // ★ 2026-11-01：首句改成人话（原来说的是「等待曲首 onset 超时」，普通用户不知道
                        //   onset 是什么）；diag() 已含「静音需要 / 最长静音 / 窗外忽略」，不再另写解释。
                        throw new IllegalStateException("没听到曲首的琴声（等满 " + (deadlineMs / 1000)
                                + " 秒）⇒ 最常见是倒计时里没切回游戏、那一下 ▶ 点空了，回 App 重新点「开始演奏」"
                                + "｜实测：" + gate.signalVerdict() + "｜诊断：" + gate.diag());
                    case WAIT:
                    default:
                        SystemClock.sleep(5);
                }
            }

            double onsetMsFromTap = (gate.firedAtNs() - tapElapsedNs) / 1e6;
            double silRefMs = silRefS * 1000.0;
            double lDevMs = lDevS * 1000.0;
            double zeroRelMs = onsetMsFromTap - silRefMs - lDevMs;
            // 零点必须落在点击之后：落在之前说明 onset 是点击音/过场音，或者 sil_ref 用错曲子
            if (zeroRelMs < 0) {
                throw new IllegalStateException(String.format(Locale.ROOT,
                        "零点算到点击之前（%.1f ms = onset %.1f − sil_ref %.1f − L_dev %.1f）"
                                + "⇒ onset 不是曲首第一个音，拒绝开火",
                        zeroRelMs, onsetMsFromTap, silRefMs, lDevMs));
            }
            this.zeroRel = zeroRelMs;
            double zeroEpochMs = tapEpochMs + zeroRelMs;

            RunLog.line(String.format(Locale.ROOT,
                    "曲首 onset：点击后 +%.1f ms（帧 %d，E=%.3g，判据=%s），%s",
                    onsetMsFromTap, gate.firedFrame(), gate.firedE(), gate.firedBy(), gate.diag()));
            RunLog.line(String.format(Locale.ROOT,
                    "零点 = onset %.1f − sil_ref %.1f − L_dev %.1f = 点击后 %.1f ms"
                            + "（src=onset）",
                    onsetMsFromTap, silRefMs, lDevMs, zeroRelMs));
            meta = String.format(Locale.ROOT,
                    "src=onset onset_ms=%.1f sil_ref_ms=%.1f l_dev_ms=%.1f zero_rel_ms=%.1f"
                            + " fired_by=%s captured_rate=%d",
                    onsetMsFromTap, silRefMs, lDevMs, zeroRelMs,
                    gate.firedBy(), tap.rate());
            if (calibFrames > 0) {
                // ★ 标定尾：锁定后继续采（此时 tap 的 keepFrames 也设了同值，不会提前 break）。
                //   不注入、不点屏 —— 这段采到的音频就是离线互相关的输入。
                RunLog.line("标定：继续采到 " + calibFrames + " 帧（当前 " + tap.frames()
                        + "，还差约 " + Math.max(0, (calibFrames - tap.frames()) / 100) + " s）");
                int got = tap.awaitFrames(calibFrames, 40_000);
                RunLog.line("标定：采集结束，共 " + got + " 帧（目标 " + calibFrames + "）");
            }
            return zeroEpochMs;
        } finally {
            if (!keepTapAlive) tap.stop();
        }
    }

    /** 兼容旧调用：begin + finish。 */
    public double await() throws Exception {
        begin();
        return finish();
    }


    /** 锁定后保留采集（调用方随后要读包络算 K）。 */
    public void keepTapAlive(boolean keep) {
        this.keepTapAlive = keep;
    }

    @Override
    public String src() {
        return "onset";
    }

    /** 实测 lead（点 ▶ → 谱面 t=0，ms）；没成功拿到零点时 NaN。 */
    public double zeroRelMs() {
        return zeroRel;
    }

    public String meta() {
        return meta;
    }

    /** 与 {@link ZeroSource.CountdownTap} 同一套交互（留时间切到游戏并就绪）。 */
    private void countdown() throws Stopped {
        RunLog.phase("倒计时 " + countdownS + " s");
        long end = System.currentTimeMillis() + countdownS * 1000L;
        int last = Integer.MIN_VALUE;
        while (true) {
            if (stop.getAsBoolean()) throw new Stopped("倒计时期间中止");
            long left = end - System.currentTimeMillis();
            if (left <= 0) break;
            int s = (int) Math.ceil(left / 1000.0);
            if (s != last) {
                last = s;
                RunLog.line("倒计时 " + s + " s");
            }
            SystemClock.sleep(Math.min(200L, Math.max(1L, left)));
        }
        if (stop.getAsBoolean()) throw new Stopped("点击前中止");
    }
}
