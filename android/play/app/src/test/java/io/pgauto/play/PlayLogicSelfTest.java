package io.pgauto.play;

import java.util.concurrent.atomic.AtomicInteger;
import java.util.function.BooleanSupplier;

/**
 * `OnsetWait` / `TapLead` / `OnsetGate` 的离线单测 —— **纯 JVM，不需要 Android、不需要真机**。
 *
 * 三个类各自对应一个真实的坑，所以用例必须**能抓到那个坑本身**：
 *   · `OnsetWait`：采集线程「写 fired → break → 写 recording=false」不是原子的。
 *     旧代码 `fired()=false` + `alive()=false` ⇒ 抛「没锁到 onset」，实际已经锁到
 *     ⇒ **把成功的一轮判成失败**，且日志把原因指反。t01 用一个「第一次读 false、
 *     第二次读 true」的 supplier 精确重放那个交错；把复查删掉，t01 立刻红。
 *   · `TapLead`：`zero=tap` 曾恒用 bounded_in 的常数、且不校验曲目 ⇒ 换曲静默偏
 *     300+ ms。t13 钉住「曲目没登记就必须拒绝」，t14 钉住「旧常数不是合法来源」。
 *   · `OnsetGate`：曾经用 `getStreamVolume()==0` 当硬闸门拒绝开跑，而实测音量 0 下
 *     录到满幅并打出 φ（10-24）。t17~t20 钉住新判据 —— **只有一帧非零样本都没有
 *     才算真静音**，且被忽略的帧同样算「有信号」（否则日志会把人引向"调音量"）。
 *
 * 运行：`bash android/play/selftest.sh`
 */
public final class PlayLogicSelfTest {

    private static int pass = 0;
    private static int fail = 0;

    public static void main(String[] args) {
        // ── OnsetWait：等待决策 ──
        t01_lockedButRecordingAlreadyFalseIsNotFailure();
        t02_reallyDeadIsFailure();
        t03_normalWaitDoesNotFire();
        t04_timeoutWithoutLockIsFailure();
        t05_lockedAtTimeoutBoundaryIsSuccess();
        t06_errorBeatsAlive();
        t07_lockedBeatsError();
        t08_stopBeatsEverything();
        t09_nullSupplierRejected();
        t10_deadlineBoundaryIsStrict();
        // ── TapLead：lead 解析 ──
        t11_overrideWins();
        t12_songLeadUsedWhenNoOverride();
        t13_missingLeadIsRejected();
        t14_illegalValuesRejected();
        t15_oldConstantIsNotASource();
        t16_realMeasuredValueAccepted();
        // ── OnsetGate：真实信号统计（10-24 删掉音量闸门后的唯一判据） ──
        t17_allZeroFramesMeanReallySilent();
        t18_oneNonZeroFrameBreaksSilence();
        t19_noFramesIsNotAllZero();
        t20_ignoredFramesStillCountAsSignal();

        System.out.println();
        System.out.println("== OnsetWait/TapLead/OnsetGate 自测：" + pass + " 通过，" + fail + " 失败 ==");
        if (fail > 0) System.exit(1);
    }

    // ─────────────────────── OnsetWait ───────────────────────

    /**
     * ★ 竞态复现：第一次读 `fired=false`，第二次读 `true`，而 `alive` 已经是 false。
     * 这是采集线程「先写 fired、后写 recording」的窗口。**必须判 FIRED**。
     * 变异：删掉 `if (!alive)` 分支里的复查 ⇒ 变成 DIED ⇒ 本用例红。
     */
    private static void t01_lockedButRecordingAlreadyFalseIsNotFailure() {
        AtomicInteger calls = new AtomicInteger();
        BooleanSupplier fired = () -> calls.incrementAndGet() >= 2;   // false, true
        OnsetWait.Step s = OnsetWait.step(fired, null, false, false, 5_000, 20_000);
        ok(s == OnsetWait.Step.FIRED, "t01 已锁到但 recording 已 false ⇒ FIRED（实际 "
                + s + "）");
        ok(calls.get() >= 2, "t01 必须复查 fired（实际只读了 " + calls.get() + " 次）");
    }

    /** 采集线程真死了（一次也没锁到）⇒ DIED。防止「无条件 FIRED」的变异。 */
    private static void t02_reallyDeadIsFailure() {
        OnsetWait.Step s = OnsetWait.step(() -> false, null, false, false, 5_000, 20_000);
        ok(s == OnsetWait.Step.DIED, "t02 采集退出且未锁到 ⇒ DIED（实际 " + s + "）");
    }

    /** 一切正常、还没到 deadline ⇒ WAIT（不许提前宣告成功）。 */
    private static void t03_normalWaitDoesNotFire() {
        OnsetWait.Step s = OnsetWait.step(() -> false, null, true, false, 100, 20_000);
        ok(s == OnsetWait.Step.WAIT, "t03 未超时未锁到 ⇒ WAIT（实际 " + s + "）");
    }

    /** 超时且确实没锁到 ⇒ TIMEOUT。 */
    private static void t04_timeoutWithoutLockIsFailure() {
        OnsetWait.Step s = OnsetWait.step(() -> false, null, true, false, 20_001, 20_000);
        ok(s == OnsetWait.Step.TIMEOUT, "t04 超时未锁到 ⇒ TIMEOUT（实际 " + s + "）");
    }

    /**
     * ★ 超时那一刻恰好锁到 ⇒ FIRED（同一个竞态的另一种表现：慢机器上尤其容易撞）。
     * 变异：删掉超时分支里的复查 ⇒ TIMEOUT ⇒ 本用例红。
     */
    private static void t05_lockedAtTimeoutBoundaryIsSuccess() {
        AtomicInteger calls = new AtomicInteger();
        BooleanSupplier fired = () -> calls.incrementAndGet() >= 2;
        OnsetWait.Step s = OnsetWait.step(fired, null, true, false, 20_001, 20_000);
        ok(s == OnsetWait.Step.FIRED, "t05 超时瞬间已锁到 ⇒ FIRED（实际 " + s + "）");
        ok(calls.get() >= 2, "t05 超时分支必须复查 fired（实际读 " + calls.get() + " 次）");
    }

    /** 采集硬错误优先于「线程还活着」——不然后者会把前者吞掉，等到超时才报。 */
    private static void t06_errorBeatsAlive() {
        OnsetWait.Step s = OnsetWait.step(() -> false, "read 返回 -3", true, false, 100, 20_000);
        ok(s == OnsetWait.Step.ERROR, "t06 有错误文本 ⇒ ERROR（实际 " + s + "）");
    }

    /**
     * 已锁到之后采集报错 ⇒ 仍算 FIRED。
     * 理由：采集的唯一职责就是吐出曲首 onset，拿到之后它怎么退都不影响零点。
     * 变异：把 ERROR 判断挪到 fired 之前 ⇒ 本用例红（真机上会白丢一轮）。
     */
    private static void t07_lockedBeatsError() {
        OnsetWait.Step s = OnsetWait.step(() -> true, "投影被撤销", false, false, 100, 20_000);
        ok(s == OnsetWait.Step.FIRED, "t07 已锁到 ⇒ FIRED（即使采集此刻报错，实际 " + s + "）");
    }

    /** 用户点了停止 ⇒ 一律 STOPPED（不该被报成「采集故障」）。 */
    private static void t08_stopBeatsEverything() {
        OnsetWait.Step s = OnsetWait.step(() -> true, "x", false, true, 99_999, 20_000);
        ok(s == OnsetWait.Step.STOPPED, "t08 stop ⇒ STOPPED（实际 " + s + "）");
    }

    private static void t09_nullSupplierRejected() {
        try {
            OnsetWait.step(null, null, true, false, 0, 20_000);
            ok(false, "t09 fired==null 应当抛");
        } catch (IllegalArgumentException e) {
            ok(true, "t09 fired==null ⇒ IllegalArgumentException");
        }
    }

    /** 边界口径与旧实现一致：`waitedMs > deadlineMs` 才算超时（等于不算）。 */
    private static void t10_deadlineBoundaryIsStrict() {
        OnsetWait.Step s = OnsetWait.step(() -> false, null, true, false, 20_000, 20_000);
        ok(s == OnsetWait.Step.WAIT, "t10 waited==deadline 仍 WAIT（实际 " + s + "）");
    }

    // ─────────────────────── TapLead ───────────────────────

    private static void t11_overrideWins() {
        double v = TapLead.resolve(7000.0, 7520.086);
        ok(v == 7000.0, "t11 命令行 lead 优先（实际 " + v + "）");
    }

    private static void t12_songLeadUsedWhenNoOverride() {
        double v = TapLead.resolve(null, 7520.086);
        ok(Math.abs(v - 7520.086) < 1e-9, "t12 无覆盖 ⇒ 用曲目自带（实际 " + v + "）");
    }

    /** ★ 回归钉：曲目没登记 lead、也没有覆盖 ⇒ 必须抛，不许回退到任何常数。 */
    private static void t13_missingLeadIsRejected() {
        try {
            double v = TapLead.resolve(null, 0.0);
            ok(false, "t13 缺 lead 应当抛，实际返回 " + v);
        } catch (IllegalArgumentException e) {
            ok(e.getMessage().contains("onset"), "t13 缺 lead ⇒ 抛且提示改用 onset（"
                    + e.getMessage() + "）");
        }
    }

    private static void t14_illegalValuesRejected() {
        double[] bad = {0.0, -1.0, Double.NaN, 60_001.0, Double.POSITIVE_INFINITY};
        for (double b : bad) {
            try {
                double v = TapLead.resolve(b, 0.0);
                ok(false, "t14 lead 覆盖 = " + b + " 应当抛，实际返回 " + v);
            } catch (IllegalArgumentException e) {
                ok(true, "t14 lead 覆盖 = " + b + " ⇒ 拒绝");
            }
        }
        // 边界值本身合法（[MIN, MAX] 闭区间）
        ok(TapLead.resolve(TapLead.MIN_MS, 0.0) == TapLead.MIN_MS, "t14 MIN_MS 合法");
        ok(TapLead.resolve(TapLead.MAX_MS, 0.0) == TapLead.MAX_MS, "t14 MAX_MS 合法");
    }

    /**
     * ★ 旧 bug 的回归钉：删除前的 `DEFAULT_LEAD_MS = 7479.88` 不属于任何一轮实测，
     * 而界面从不传 lead ⇒ 它在**任何曲**上都被用。现在「没登记 ⇒ 拒绝」，
     * 且 7479.88 只能作为**显式覆盖**出现（那是调试者的选择，不是代码的默认）。
     */
    private static void t15_oldConstantIsNotASource() {
        double old = 7479.88;
        ok(TapLead.resolve(old, 0.0) == old,
                "t15 显式覆盖仍可用（调试自由），但它不是默认值");
        try {
            double v = TapLead.resolve(null, 0.0);
            ok(false, "t15 无覆盖 + 无曲目值 必须拒绝，实际返回 " + v);
        } catch (IllegalArgumentException e) {
            ok(true, "t15 无覆盖 + 无曲目值 ⇒ 拒绝（旧实现会静默用 " + old + "）");
        }
    }

    /** bounded_in 的真实值（timeline r3 实测）必须能用。 */
    private static void t16_realMeasuredValueAccepted() {
        double v = TapLead.resolve(null, 7520.086);
        ok(v > 7000 && v < 8000, "t16 bounded_in 实测 lead 7520.086 通过（实际 " + v + "）");
    }

    // ─────────── OnsetGate：真实信号统计（替代「媒体音量 == 0 就拒绝」） ───────────

    /**
     * 真静音：每一帧能量都精确为 0 ⇒ `allZero()` 为真，且结论文本必须说「真静音」。
     *
     * 这是唯一允许提示「调音量/看看是不是没在播」的条件 —— 而不是开跑前读一个回读值。
     */
    private static void t17_allZeroFramesMeanReallySilent() {
        OnsetGate g = OnsetGate.withDefaults(10, 0L);
        for (int i = 0; i < 30; i++) g.onFrame(i, 0.0, false, i * 10_000_000L);
        ok(g.frames() == 30, "t17 帧数应为 30（实际 " + g.frames() + "）");
        ok(g.zeroFrames() == 30, "t17 全 0 帧应为 30（实际 " + g.zeroFrames() + "）");
        ok(g.peakMeanSquare() == 0.0, "t17 峰值能量应为 0（实际 " + g.peakMeanSquare() + "）");
        ok(g.allZero(), "t17 allZero() 必须为真（真静音）");
        ok(g.signalVerdict().contains("真静音"),
                "t17 结论要明说「真静音」（实际：" + g.signalVerdict() + "）");
    }

    /**
     * 只要**有一帧**非零 ⇒ 不是静音。变异：把 `allZero` 写成恒真 ⇒ 本用例红；
     * 把 `zeroFrames == frames` 写成 `zeroFrames > 0` ⇒ 本用例红。
     */
    private static void t18_oneNonZeroFrameBreaksSilence() {
        OnsetGate g = OnsetGate.withDefaults(10, 0L);
        for (int i = 0; i < 30; i++) g.onFrame(i, 0.0, false, i * 10_000_000L);
        g.onFrame(30, 0.5, false, 300_000_000L);
        ok(g.frames() == 31, "t18 帧数应为 31（实际 " + g.frames() + "）");
        ok(g.zeroFrames() == 30, "t18 全 0 帧应为 30（实际 " + g.zeroFrames() + "）");
        ok(Math.abs(g.peakMeanSquare() - 0.5) < 1e-12,
                "t18 峰值能量应为 0.5（实际 " + g.peakMeanSquare() + "）");
        ok(!g.allZero(), "t18 有一帧非零 ⇒ allZero() 必须为假");
        ok(!g.signalVerdict().contains("真静音"),
                "t18 有信号时不许说「真静音」（实际：" + g.signalVerdict() + "）");
        ok(g.signalVerdict().contains("有信号"),
                "t18 结论要说「有信号」（实际：" + g.signalVerdict() + "）");
    }

    /**
     * 一帧都没喂 ⇒ **不能**当成「全 0」。两者性质不同：
     * 全 0 = 「设备没声」；零帧 = 「采集线程根本没跑起来/投影立刻被撤」，修法不同。
     */
    private static void t19_noFramesIsNotAllZero() {
        OnsetGate g = OnsetGate.withDefaults(10, 0L);
        ok(g.frames() == 0, "t19 未喂帧时 frames == 0（实际 " + g.frames() + "）");
        ok(!g.allZero(), "t19 零帧不许算「全 0」");
        ok(g.signalVerdict().contains("一帧都没采到"),
                "t19 结论要区分「一帧都没采到」（实际：" + g.signalVerdict() + "）");
    }

    /**
     * ★ 关键：**被忽略的帧也必须算进统计**。
     *
     * 场景：游戏还在放前奏/加载音（非 onset、窗外、没 ARMED）—— 这些帧一帧都不会
     * 进入「锁定」路径，但它们证明**采集通道是通的**。若统计写在 `if (!onset) return;`
     * 之后、或只在锁定那一刻统计，就会把「有声音但没认」误报成「真静音」，
     * 于是日志又把人引向「调音量」这个错误方向。
     * 变异：把 `frames++` 那三行挪到 `firedAtNs = frameMidNs;` 之前 ⇒ 本用例红。
     */
    private static void t20_ignoredFramesStillCountAsSignal() {
        OnsetGate g = OnsetGate.withDefaults(10, 0L);
        // 全部落在窗外（frameMidNs 远小于 gateStart + 1500 ms）且 onset=false ⇒ 一帧都不会锁定
        for (int i = 0; i < 40; i++) g.onFrame(i, 1.0e6, false, i * 10_000_000L);
        ok(g.frames() == 40, "t20 被忽略的帧也要计数（实际 " + g.frames() + "）");
        ok(!g.allZero(), "t20 40 帧响度 1e6 ⇒ 绝不是静音");
        ok(g.peakMeanSquare() == 1.0e6, "t20 峰值能量应为 1e6（实际 " + g.peakMeanSquare() + "）");
        ok(!g.fired(), "t20 前提：这些帧不该锁定");
        ok(g.diag().contains("全 0 0"), "t20 diag 要带全 0 帧计数（实际：" + g.diag() + "）");
    }

    // ─────────────────────── 助手 ───────────────────────

    private static void ok(boolean cond, String what) {
        if (cond) {
            pass++;
            System.out.println("  ✓ " + what);
        } else {
            fail++;
            System.out.println("  ✗ " + what);
        }
    }

    private PlayLogicSelfTest() {}
}
