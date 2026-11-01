package io.pgauto.play;

/**
 * `OnsetGate` / `FrameClock` 的离线单测 —— **纯 JVM 跑，不需要 Android、不需要真机**。
 *
 * 为什么这么重视：这两个类的错**全部是静默的**。
 *   · 门控错 ⇒ 拿了点击音当零点 ⇒ 整轮偏几百毫秒/几秒，结算全 Miss，日志一切正常；
 *   · 帧时钟错 ⇒ 偏固定值 ⇒ 全 Late 或全 Early，看起来跟「注入侧坏了」一模一样。
 * 真机上这两种都只能靠打一轮 + 结算页去猜，所以必须在 PC 上就用断言钉死。
 *
 * 运行：`bash android/play/selftest.sh`
 */
public final class OnsetGateSelfTest {

    private static int pass = 0;
    private static int fail = 0;

    private static final int FRAME_MS = 10;
    private static final int RATE = 48000;
    private static final int HOP = 480;              // 48000 × 10 / 1000
    private static final long FRAME_NS = 10_000_000L;
    /** 静音 / 有声的 meanSquare 代表值。阈值 RMS 300 ⇒ quietE = 9e4。 */
    private static final double QUIET = 0.0;
    private static final double LOUD = 1.0e8;

    public static void main(String[] args) {
        t01_pureSilenceNeverFires();
        t02_firesOnFirstOnsetAfterQuiet();
        t03_clickSoundIsIgnored();
        t04_shortQuietIsRejected();
        t05_beforeMinLeadNeverArms();
        t06_idempotentAfterFired();
        t07_zeroQuietMsBoundary();
        t08_quietRunMustBeContinuous();
        t09_onsetRightAfterQuietCompletion();
        t10_frameClockKnownValue();
        t11_frameClockSpacingConstant();
        t12_frameClockRejectsOverrun();
        t13_frameClockAt44k1();
        t14_windowOpensExactlyOnOnsetFrame();
        t23_forceArmBypassesQuiet();
        t24_forceArmStillNeedsSound();
        t15_absFiresAtFirstFrameOfRun();
        t16_absTwoFrameBlipRejected();
        t17_absOffStillUsesRelative();
        t18_absThresholdIsInclusive();
        t19_absRunBeforeWindowDoesNotCount();
        t20_absNeedsQuietBefore();
        t21_soundSpanningWindowIsRejected();
        t22_absEdgeNoteAndAccessors();

        System.out.println();
        System.out.println("== OnsetGate/FrameClock 自测：" + pass + " 通过，"
                + fail + " 失败 ==");
        if (fail > 0) System.exit(1);
    }

    // ─────────────────────────── 用例 ───────────────────────────

    /** 全程静音（比如采集没问题但曲子一直没开始）⇒ 永远不 FIRED，只停在 ARMED/WAIT。 */
    private static void t01_pureSilenceNeverFires() {
        OnsetGate g = gate(300, 0, 0);
        for (int f = 0; f < 200; f++) g.onFrame(f, QUIET, false, mid(f));
        ok(!g.fired(), "t01 全静音不应锁定");
        ok(g.state() == OnsetGate.ARMED, "t01 静音够长后应停在 ARMED，实际 "
                + OnsetGate.stateName(g.state()));
    }

    /** 静音够 + 窗内第一个 onset ⇒ 锁定在这一帧的中点。 */
    private static void t02_firesOnFirstOnsetAfterQuiet() {
        OnsetGate g = gate(300, 1500, 0);
        for (int f = 0; f < 160; f++) g.onFrame(f, QUIET, false, mid(f));
        g.onFrame(160, LOUD, true, mid(160));
        ok(g.fired(), "t02 应锁定");
        eq(g.firedAtNs(), mid(160), "t02 firedAtNs 必须是该帧中点");
        eq(g.firedFrame(), 160, "t02 firedFrame");
    }

    /**
     * 点击音场景：**点 ▶ 之前已经安静了 1 秒**（> quietMs），然后点击音出现。
     *
     * ★ 这是 `minLead` 唯一真正被需要的地方。第一版把点击音放在第 0 帧，
     *   结果「静音不足」这一条已经把它挡住了 ⇒ `minLead` 删掉也不会红（假保护）。
     *   真实场景恰恰是：采集启动后界面一直是静的，1 s 后才点 ▶。
     *   若 `minLead` 失效，零点会落在这个点击音上，比真零点早 1.5 s 以上 ⇒ 整轮全 Early。
     */
    private static void t03_clickSoundIsIgnored() {
        OnsetGate g = gate(300, 1500, 0);
        for (int f = 0; f < 100; f++) g.onFrame(f, QUIET, false, mid(f));  // 点前已静 1 s
        g.onFrame(100, LOUD, true, mid(100));        // 点击音（t≈1010 ms，仍在 1500 内）
        for (int f = 101; f < 200; f++) g.onFrame(f, QUIET, false, mid(f));
        g.onFrame(200, LOUD, true, mid(200));        // 曲首第一个音
        ok(g.fired(), "t03 应锁定");
        eq(g.firedAtNs(), mid(200), "t03 必须锁在曲首音（不是点击音）");
        eq(g.ignoredEarlyCount(), 1, "t03 窗外忽略应恰好 1 次（那次点击音）");
    }

    /** 只安静了 5 帧（50 ms < 300 ms）就来的 onset：不认。那是持续音里的起伏。 */
    private static void t04_shortQuietIsRejected() {
        OnsetGate g = gate(300, 0, 0);
        for (int f = 0; f < 5; f++) g.onFrame(f, QUIET, false, mid(f));
        g.onFrame(5, LOUD, true, mid(5));
        ok(!g.fired(), "t04 安静不足时不该锁定");
        ok(g.ignoredNotArmedCount() == 1, "t04 应记一次 quiet 不足忽略，实际 "
                + g.ignoredNotArmedCount());
    }

    /** 安静够了但还没过 minLead：不 ARMED，也不锁定。 */
    private static void t05_beforeMinLeadNeverArms() {
        OnsetGate g = gate(300, 1500, 0);
        for (int f = 0; f < 60; f++) g.onFrame(f, QUIET, false, mid(f));   // 600 ms
        ok(g.state() == OnsetGate.WAIT_QUIET, "t05 minLead 内不该 ARMED，实际 "
                + OnsetGate.stateName(g.state()));
        g.onFrame(60, LOUD, true, mid(60));
        ok(!g.fired(), "t05 minLead 内的 onset 不该锁定");
        ok(g.ignoredEarlyCount() == 1, "t05 应记一次窗外忽略");
    }

    /** 锁定后再喂什么都不能改结果（采集线程可能多读一块）。 */
    private static void t06_idempotentAfterFired() {
        OnsetGate g = gate(300, 0, 0);
        for (int f = 0; f < 40; f++) g.onFrame(f, QUIET, false, mid(f));
        g.onFrame(40, LOUD, true, mid(40));
        long first = g.firedAtNs();
        int ignA = g.ignoredNotArmedCount();
        int ignE = g.ignoredEarlyCount();
        g.onFrame(41, LOUD, true, mid(41));
        g.onFrame(42, LOUD * 10, true, mid(42));
        eq(g.firedAtNs(), first, "t06 锁定后 firedAtNs 不得改变");
        eq(g.firedFrame(), 40, "t06 锁定后 firedFrame 不得改变");
        // ★ 诊断计数也不能涨：否则日志会把「锁定后曲子里正常的音」记成
        //   「安静不足被忽略 N 次」，真机排查时会被这条假线索带偏。
        ok(g.ignoredNotArmedCount() == ignA && g.ignoredEarlyCount() == ignE,
                "t06 锁定后诊断计数不得再涨（实际 early " + g.ignoredEarlyCount()
                        + "/notArmed " + g.ignoredNotArmedCount() + "）");
    }

    /** quietMs = 0 ⇒ 不需要任何静音（边界必须可构造，不能除零/负帧数）。 */
    private static void t07_zeroQuietMsBoundary() {
        OnsetGate g = gate(0, 0, 0);
        g.onFrame(0, LOUD, true, mid(0));
        ok(g.fired(), "t07 quietMs=0 时首个 onset 应直接锁定");
        eq(g.quietFramesNeeded(), 0, "t07 quietFramesNeeded");
    }

    /**
     * 静音必须**连续**：20 帧静音 → 一阵声响（非 onset，比如持续音）→ 20 帧静音，
     * 累计 40 帧但每段都只有 200 ms < 300 ms ⇒ 不能 ARMED。
     */
    private static void t08_quietRunMustBeContinuous() {
        OnsetGate g = gate(300, 0, 0);
        for (int f = 0; f < 20; f++) g.onFrame(f, QUIET, false, mid(f));
        g.onFrame(20, LOUD, false, mid(20));         // 持续音：有声但不是 onset
        for (int f = 21; f < 41; f++) g.onFrame(f, QUIET, false, mid(f));
        g.onFrame(41, LOUD, true, mid(41));
        ok(!g.fired(), "t08 两段各 200 ms 的静音不该被当成 400 ms 连续静音");
        ok(g.quietRunMax() < g.quietFramesNeeded(),
                "t08 最长静音应 < 需要帧数，实际 " + g.quietRunMax());
    }

    /**
     * ★ 最险的顺序 bug：安静刚好满 300 ms 的**下一帧**就是起音。
     * 那一帧自己 `quiet=false` ⇒ 若先用清零后的计数判 ARMED，就永远 ARMED 不了，
     * 这个音被永久错过（表现为「明明有音却超时」）。
     */
    private static void t09_onsetRightAfterQuietCompletion() {
        OnsetGate g = gate(300, 0, 0);
        for (int f = 0; f < 30; f++) g.onFrame(f, QUIET, false, mid(f));   // 正好 300 ms
        g.onFrame(30, LOUD, true, mid(30));
        ok(g.fired(), "t09 静音刚满 300 ms 就来的起音必须被认");
        eq(g.firedFrame(), 30, "t09 firedFrame");
    }

    /** FrameClock 的已知值：手算 962_333_333 ns（见类注释的公式）。 */
    private static void t10_frameClockKnownValue() {
        long mid = FrameClock.frameMidNs(1_000_000_000L, 2048, 0, HOP, RATE);
        eq(mid, 962_333_333L, "t10 帧 0 中点");
        // 手算：back = 2048-480 = 1568 样本 = 32,666,666.67 ns；半帧 5,000,000 ns
        eq(FrameClock.frameNs(HOP, RATE), FRAME_NS, "t10 帧长应为 10 ms");
    }

    /** 相邻帧的中点必须恰好差一帧（差不对 ⇒ 时间轴被拉伸/压缩，全 Late 或全 Early）。 */
    private static void t11_frameClockSpacingConstant() {
        long tRead = 5_000_000_000L;
        long sAcc = 2048;
        long prev = 0;
        for (int f = 0; f < 4; f++) {
            long m = FrameClock.frameMidNs(tRead, sAcc, f, HOP, RATE);
            if (f > 0) eq(m - prev, FRAME_NS, "t11 帧 " + (f - 1) + "→" + f + " 间距");
            prev = m;
        }
        ok(prev < tRead, "t11 所有帧中点都应早于 tRead");
    }

    /** 帧末尾还没读到时必须炸 —— 否则算出负回测量，时间戳静默偏掉。 */
    private static void t12_frameClockRejectsOverrun() {
        boolean threw = false;
        try {
            FrameClock.frameMidNs(1_000_000_000L, 100, 0, HOP, RATE);
        } catch (IllegalArgumentException e) {
            threw = true;
        }
        ok(threw, "t12 sAcc 不够一帧时必须抛 IllegalArgumentException");
    }

    /** 44.1 kHz 也要是 10 ms 一帧（hop 变了，间距不能变）。 */
    private static void t13_frameClockAt44k1() {
        int rate = 44100;
        int hop = 441;
        long a = FrameClock.frameMidNs(2_000_000_000L, 1764, 0, hop, rate);
        long b = FrameClock.frameMidNs(2_000_000_000L, 1764, 1, hop, rate);
        eq(b - a, FRAME_NS, "t13 44.1k 下帧间距仍应 10 ms");
    }

    /**
     * ★「窗口恰好在起音那一帧才打开」—— `prevQuietRun` 唯一不可替代的场景。
     *
     * 构造：静音正好 30 帧（帧 0~29），minLead 取 305 ms。
     *   · 帧 29 的中点 = 300 ms < 305 ⇒ 窗外（这一帧不能 ARMED）
     *   · 帧 30 的中点 = 310 ms ≥ 305 ⇒ 窗内，而它自己就是起音（`quiet=false`）
     * 若判 ARMED 时用本帧清零后的 `quietRun`（= 0），这一帧就永远 ARM 不了，
     * 起音被永久错过 —— 症状是「明明有音却超时」。
     */
    private static void t14_windowOpensExactlyOnOnsetFrame() {
        OnsetGate g = gate(300, 305, 0);
        for (int f = 0; f < 30; f++) g.onFrame(f, QUIET, false, mid(f));
        ok(g.state() == OnsetGate.WAIT_QUIET, "t14 帧 29（300 ms）还在窗外，不该 ARMED");
        g.onFrame(30, LOUD, true, mid(30));
        ok(g.fired(), "t14 窗口恰在起音那帧打开时仍必须锁定");
        eq(g.firedFrame(), 30, "t14 firedFrame");
    }

    // ─────────────────── 绝对阈值模式（母带 sil_thr，FACTS §169）───────────────────

    /**
     * 边沿 = **连续超阈段的首帧**，不是确认时刻。
     *
     * 变异点：把回填改成 `absFirstMid → frameMidNs`（末帧）⇒ 这里右移 20 ms 而全绿？
     * 不会 —— `firedAtNs` 断言用的是 mid(160)，末帧会是 mid(162)。
     */
    private static void t15_absFiresAtFirstFrameOfRun() {
        OnsetGate g = gateAbs(300, 300, 1500, 0);
        for (int f = 0; f < 160; f++) g.onFrame(f, QUIET, false, mid(f));
        g.onFrame(160, LOUD, false, mid(160));
        g.onFrame(161, LOUD, false, mid(161));
        ok(!g.fired(), "t15 只超阈 2 帧不该锁定（sustain = 3 帧）");
        g.onFrame(162, LOUD, false, mid(162));
        ok(g.fired(), "t15 连续 3 帧后应锁定");
        eq(g.firedFrame(), 160, "t15 必须回填**首帧**（不是确认帧 162）");
        eq(g.firedAtNs(), mid(160), "t15 firedAtNs 必须是首帧中点");
        ok("abs".equals(g.firedBy()), "t15 firedBy 应为 abs，实际 " + g.firedBy());
    }

    /** 2 帧毛刺（瞬时尖峰）必须被 sustain 否决，之后的真边沿仍要认。 */
    private static void t16_absTwoFrameBlipRejected() {
        OnsetGate g = gateAbs(300, 300, 1500, 0);
        for (int f = 0; f < 160; f++) g.onFrame(f, QUIET, false, mid(f));
        g.onFrame(160, LOUD, false, mid(160));
        g.onFrame(161, LOUD, false, mid(161));
        for (int f = 162; f < 170; f++) g.onFrame(f, QUIET, false, mid(f));
        ok(!g.fired(), "t16 单尖峰（2 帧）不得锁定");
        g.onFrame(170, LOUD, false, mid(170));
        g.onFrame(171, LOUD, false, mid(171));
        g.onFrame(172, LOUD, false, mid(172));
        ok(g.fired(), "t16 尖峰之后的真边沿必须锁定");
        eq(g.firedFrame(), 170, "t16 锁在真边沿首帧");
    }

    /** 阈值 = 0（未标定）⇒ 逐位退回相对判据：`onset` 标记才是唯一依据。 */
    private static void t17_absOffStillUsesRelative() {
        OnsetGate g = gate(300, 1500, 0);
        for (int f = 0; f < 160; f++) g.onFrame(f, QUIET, false, mid(f));
        // 超阈 5 帧但**没有 onset 标记** ⇒ 相对判据必须一帧都不认
        for (int f = 160; f < 165; f++) g.onFrame(f, LOUD, false, mid(f));
        ok(!g.fired(), "t17 相对判据只认 onset 标记（没有标记不得锁定）");
        g.onFrame(165, LOUD, true, mid(165));
        ok(g.fired(), "t17 有 onset 标记才锁定");
        ok("onset".equals(g.firedBy()), "t17 firedBy 应为 onset，实际 " + g.firedBy());
    }

    /** 阈值判据是 `>=`：恰好等于阈值要认，差一点不认（边界必须与 PC 侧 `e > thr` 对齐的口径固定下来）。 */
    private static void t18_absThresholdIsInclusive() {
        double thr = 300.0;
        double e = thr * thr;
        OnsetGate g = gateAbs(thr, 300, 1500, 0);
        for (int f = 0; f < 160; f++) g.onFrame(f, QUIET, false, mid(f));
        g.onFrame(160, e * 0.999, false, mid(160));
        ok(!g.fired(), "t18 低于阈值不得计数");
        for (int f = 161; f < 164; f++) g.onFrame(f, e, false, mid(f));
        ok(g.fired(), "t18 恰好等于阈值应计入");
        eq(g.firedFrame(), 161, "t18 首帧是 161");
    }

    /** 窗外的连续超阈段不累计（否则点击音尾巴会让窗一开就立刻锁定）。 */
    private static void t19_absRunBeforeWindowDoesNotCount() {
        OnsetGate g = gateAbs(300, 300, 1500, 0);
        for (int f = 0; f < 100; f++) g.onFrame(f, QUIET, false, mid(f));
        for (int f = 100; f < 107; f++) g.onFrame(f, LOUD, false, mid(f));   // 窗外（≤1070 ms）
        ok(!g.fired(), "t19 窗外超阈段不得锁定");
        for (int f = 107; f < 155; f++) g.onFrame(f, QUIET, false, mid(f));
        g.onFrame(155, LOUD, false, mid(155));
        g.onFrame(156, LOUD, false, mid(156));
        g.onFrame(157, LOUD, false, mid(157));
        ok(g.fired(), "t19 窗内连续 3 帧应锁定");
        eq(g.firedFrame(), 155, "t19 锁在窗内首帧 155（窗外那段不算）");
    }

    /** 没有安静够（未 ARMED）时，超阈段不认，且只记一次「安静不足」。 */
    private static void t20_absNeedsQuietBefore() {
        OnsetGate g = gateAbs(300, 300, 0, 0);
        // only 5 quiet frames (50 ms < 300 ms)
        for (int f = 0; f < 5; f++) g.onFrame(f, QUIET, false, mid(f));
        for (int f = 5; f < 8; f++) g.onFrame(f, LOUD, false, mid(f));
        ok(!g.fired(), "t20 安静不足时不得锁定");
        ok(g.ignoredNotArmedCount() == 1, "t20 应记一次安静不足，实际 "
                + g.ignoredNotArmedCount());
    }

    /**
     * 跨窗边界的持续声（点击音尾巴）：窗开时它还在响 ⇒ **不算边沿**，整段被忽略；
     * 之后重新安静够久，新的边沿仍要认。
     *
     * ★ 这条钉的是「边沿必须有前提」：`ARMED` 要求**紧邻** 300 ms 安静，而窗外那段
     *   持续声已经把 `quietRun` 清零 ⇒ 它不可能在窗内凑够安静。若哪天有人为了
     *   「窗一开就锁定」把 `quietRun` 的复位删掉，这条立刻红。
     * ⚠ 说明：绝对模式下 `!inWindow ⇒ absRun=0` 是**防御性**分支（这种情形下
     *   本来就 ARMED 不了），可观测的后果由本用例覆盖。
     */
    private static void t21_soundSpanningWindowIsRejected() {
        OnsetGate g = gateAbs(300, 300, 1500, 0);
        for (int f = 0; f < 148; f++) g.onFrame(f, QUIET, false, mid(f));
        g.onFrame(148, LOUD, false, mid(148));   // 1490 ms，窗外开始
        g.onFrame(149, LOUD, false, mid(149));   // 窗内
        g.onFrame(150, LOUD, false, mid(150));
        ok(!g.fired(), "t21 跨窗边界的持续声不得锁定（前面没有安静段）");
        // ★ 窗外那帧**不计入**连续段：所以窗内只累计到 2 帧（149/150），压根没凑满 sustain。
        //   若有人把 `!inWindow ⇒ absRun=0` 删掉，148/149/150 会凑成 3 帧并记一次
        //   「安静不足」⇒ 这里立刻红。
        ok(g.ignoredNotArmedCount() == 0,
                "t21 窗外那帧不得计入连续段（凑满 3 帧就会误记安静不足），实际 "
                        + g.ignoredNotArmedCount());
        for (int f = 151; f < 185; f++) g.onFrame(f, QUIET, false, mid(f));
        g.onFrame(185, LOUD, false, mid(185));
        g.onFrame(186, LOUD, false, mid(186));
        g.onFrame(187, LOUD, false, mid(187));
        ok(g.fired(), "t21 重新安静够久之后的新边沿必须锁定");
        eq(g.firedFrame(), 185, "t21 锁在新边沿首帧 185");
    }

    /** 口径文本与访问器：日志/界面共用一处，sustain 必须换算成帧（30 ms / 10 ms = 3）。 */
    private static void t22_absEdgeNoteAndAccessors() {
        OnsetGate abs = gateAbs(283.0, 300, 1500, 0);
        ok(abs.sustainFrames() == 3, "t22 sustain 30 ms / 10 ms 应为 3 帧，实际 "
                + abs.sustainFrames());
        ok(Math.abs(abs.absThrRms() - 283.0) < 1e-9, "t22 absThrRms 应回读 283");
        ok(abs.edgeNote().contains("sil_thr 283.0"), "t22 edgeNote 应含阈值，实际 "
                + abs.edgeNote());
        ok(abs.edgeNote().contains("3 帧"), "t22 edgeNote 应含确认帧数，实际 " + abs.edgeNote());
        OnsetGate rel = gate(300, 1500, 0);
        ok(rel.absThrRms() == 0.0, "t22 相对模式的 absThrRms 必须是 0");
        ok(rel.sustainFrames() == 0, "t22 相对模式不该有确认窗");
        ok(rel.edgeNote().contains("相对"), "t22 相对模式的 edgeNote 应写明，实际 "
                + rel.edgeNote());
    }

    /**
     * t23：**连续响**（课题模式第 2/3 首的曲间过场）时，正常状态机永远不会 ARMED
     * ⇒ 永远不会触发（这就是 20:58「后两首直接不打」的根因）。
     * `forceArm()` 必须能跳过"等 300 ms 静音"这一步。
     */
    private static void t23_forceArmBypassesQuiet() {
        OnsetGate g = gateAbs(300, 300, 1500, 0);
        for (int f = 0; f < 300; f++) g.onFrame(f, LOUD, false, mid(f));
        ok(!g.fired(), "t23 连续响：正常路径不应触发");
        ok(g.ignoredNotArmedCount() > 0, "t23 应记到 ignoredNotArmed（超阈但前面没安静够）");
        ok(!g.armed(), "t23 连续响：不应 ARMED");
        g.forceArm();
        ok(g.armed(), "t23 forceArm 后应立即 ARMED");
        for (int f = 300; f < 330; f++) g.onFrame(f, LOUD, false, mid(f));
        ok(g.fired(), "t23 forceArm + 持续响 ⇒ 应触发");
        // ★ 回填的是「**当前这段连续超阈**」的首帧；forceArm 那一刻正在响的那段从 299 开始
        //   ⇒ 锁在 mid(299)。这正是会话里"过场音效"会被丢弃的原因（notBefore 把它滤掉）。
        eq(g.firedAtNs(), mid(299), "t23 应锁在当前超阈段的首帧（不是确认帧）");
    }

    /** t24：forceArm 只是跳过"等静音"，**不能**在静音里凭空触发。 */
    private static void t24_forceArmStillNeedsSound() {
        OnsetGate g = gateAbs(300, 300, 1500, 0);
        g.forceArm();
        for (int f = 0; f < 300; f++) g.onFrame(f, QUIET, false, mid(f));
        ok(!g.fired(), "t24 forceArm 之后一直静音 ⇒ 不应触发");
    }

    // ─────────────────────────── 助手 ───────────────────────────

    /** 绝对阈值模式的 gate（quiet 300 ms / minLead 可变）。 */
    private static OnsetGate gateAbs(double thrRms, int quietMs, int minLeadMs, long originNs) {
        OnsetGate g = new OnsetGate(FRAME_MS, OnsetGate.DEFAULT_QUIET_RMS, quietMs, minLeadMs,
                originNs, thrRms, OnsetGate.DEFAULT_SUSTAIN_MS);
        g.start(originNs);
        return g;
    }

    /** 默认 origin=0；`tapWall` 也取 0（`gate.start(0)` 由调用方按需覆盖）。 */
    private static OnsetGate gate(int quietMs, int minLeadMs, long originNs) {
        OnsetGate g = new OnsetGate(FRAME_MS, OnsetGate.DEFAULT_QUIET_RMS,
                quietMs, minLeadMs, originNs);
        g.start(originNs);
        return g;
    }

    /** 帧 f 的中点时刻（假定 origin=0、每帧 10 ms 且无丢块）。 */
    private static long mid(int f) {
        return (long) (f + 1) * FRAME_NS;
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

    private static void eq(long a, long b, String what) {
        ok(a == b, what + "（实际 " + a + "，期望 " + b + "）");
    }

    private OnsetGateSelfTest() {}
}
