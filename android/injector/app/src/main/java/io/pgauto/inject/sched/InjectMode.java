package io.pgauto.inject.sched;

/**
 * 注入模式常量（0 / 1）+ 合法性与日志名判定。**纯 Java，零 Android 依赖**（PC 上可单测）。
 *
 * ★ 为什么单独一个类，而不是放在 `Injector` 里：`Injector` 为了造 `MotionEvent` 必须
 *   引 `android.view.*` ⇒ 它进不了 `android/play/selftest.sh` 那条「不带 android.jar」的
 *   编译链。而这一层恰恰是**值得单测的部分**：
 *   「传给 `injectInputEvent` 的那个 int 到底是什么」决定了一枪 A/B 是否成立，
 *   写错的表现是「日志写着 wait、实际还是 async」—— 成绩会一模一样，白打一枪。
 *
 * ★ 2026-11-01：原先还有 `of(boolean)` 把「界面上的一个开关」映射成模式号，随 dev/user
 *   双形态一起删除（生产现在固定 `ASYNC`，写死在 `PlayService`）。
 *
 * ★ 数值来源是 Android 的 `InputManager.INJECT_INPUT_EVENT_MODE_*`（0 / 1），
 *   与 `Injector` 注释里 10-20 实测的 600/600 成功一致。**不要照抄猜别的值。**
 */
public final class InjectMode {

    /** `INJECT_INPUT_EVENT_MODE_ASYNC`：事件入队即返回（生产默认，10-20 实测 600/600）。 */
    public static final int ASYNC = 0;

    /**
     * `INJECT_INPUT_EVENT_MODE_WAIT_FOR_FINISH`：等到事件被派发完成后返回。
     *
     * 用途（FACTS §165 的 E2）：ASYNC 下 `DevRun` 记的 `late_ms` 只反映「塞进队列」的成本，
     * 测不到游戏何时消费 ⇒ 端内 522 Miss 无法归因。同步模式下 `late_ms` 变成「投递完成」的成本。
     */
    public static final int WAIT_FOR_FINISH = 1;

    private InjectMode() {}

    /** 只认 0 / 1。 */
    public static boolean isValid(int mode) {
        return mode == ASYNC || mode == WAIT_FOR_FINISH;
    }

    /**
     * 给日志用的名字。**未知值不静默当 async** —— 日志里必须能看出「这个数不认识」，
     * 否则一个错字面量（比如 2）会以 async 的身份混过整轮验收。
     */
    public static String name(int mode) {
        if (mode == WAIT_FOR_FINISH) return "wait";
        if (mode == ASYNC) return "async";
        return "?" + mode;
    }
}
