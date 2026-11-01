package io.pgauto.inject;

import android.os.SystemClock;
import android.view.InputDevice;
import android.view.MotionEvent;

import java.lang.reflect.Method;

/**
 * S3 的「手」：通过 Shizuku 代理执行 {@code IInputManager.injectInputEvent}。
 *
 * ★ 为什么用**接口方法**反射，而不是 {@code Stub$Proxy} 上的方法：
 *   2026-10-21 在 ALT-AN00 / Android 14 实测（PnInject 诊断日志）：
 *     · {@code IInputManager$Stub.getDeclaredFields()}       ⇒ 共 **0** 个（TRANSACTION_* 全被 hiddenapi 裁掉）
 *     · {@code IInputManager$Stub$Proxy.getDeclaredMethods()} ⇒ 共 **1** 个（override 被裁）
 *     · {@code IInputManager.getMethods()}                    ⇒ 共 2 个，**含 injectInputEvent(InputEvent,int)**
 *   接口上的方法在 unsupported 档（可见），实现类上的被裁。
 *   ⇒ 用接口 Method 做虚分派调用即可：`method.invoke(proxy, event, mode)`。
 *   跨进程的 transact code 是**编译期固化在 Proxy 字节码里**的常量，运行时不需要读那些字段，
 *   所以 {@code SystemServiceHelper.getTransactionCode()} 返回 null **不影响**注入。
 *
 * ⚠ 不要改成 `Stub$Proxy.class.getDeclaredMethod("injectInputEvent", ...)`：
 *   在 Android 14 上它被裁掉了，会抛 NoSuchMethodException —— 而这是**反射可见性**问题，
 *   不代表方法不存在，也不代表不能调用。这个坑会让人误判「方案不可行」。
 */
public final class Injector {

    /** 0 = INJECT_INPUT_EVENT_MODE_ASYNC（不等注入完成）。与 10-20 实测 600/600 成功时一致。 */
    public static final int MODE_ASYNC = io.pgauto.inject.sched.InjectMode.ASYNC;

    /**
     * 1 = INJECT_INPUT_EVENT_MODE_WAIT_FOR_FINISH（等到事件被派发/消费才返回）。
     *
     * ★ 为什么要它（FACTS §165）：ASYNC 下 `DevRun` 记的 `late_ms` 只反映「塞进
     *   InputManager 队列」的成本，**测不到游戏何时真正消费**。所以「发送侧干净」
     *   一直只是半步证据 —— 端内 522 Miss 无法归因。同步模式下 `late_ms` 变成
     *   「投递完成」的成本，同一套日志立刻能回答「队列是不是在积压」。
     * ★ 代价：每条事件要等一个派发周期（典型 5~16 ms）。44 事件/s 仍来得及，
     *   但若日志里 `late p99` 爆到 >50 ms，说明这个负载撑不住同步 ⇒ 那一枪作废，别拿去下结论。
     * ★ 生产用**不是**它：端内固定 `InjectMode.ASYNC`（诊断用的 wait 开关已随 11-01 重构删除）。
     * ★ 常量与「布尔 → 模式」的映射在 {@link io.pgauto.inject.sched.InjectMode}
     *   （纯 Java，可进无 android.jar 的自测链）。
     */
    public static final int MODE_WAIT_FOR_FINISH = io.pgauto.inject.sched.InjectMode.WAIT_FOR_FINISH;

    /** 一次点击按下到抬起的间隔。20 ms 足以让系统识别为 tap（不是长按，也不是滑动）。 */
    private static final long TAP_HOLD_MS = 20;

    private Injector() {}

    /**
     * 构造一个**单指触摸事件**（source = TOUCHSCREEN，toolType = FINGER，pressure/size = 1）。
     *
     * ★★ 为什么不能用 `MotionEvent.obtain(downTime, eventTime, action, x, y, metaState)`：
     *   那个 6 参版本造出来的事件 **source = SOURCE_UNKNOWN(0)**、toolType = UNKNOWN。
     *   `injectInputEvent` 对它**照样返回 true**（true 只代表"成功入队"），
     *   但事件不会被派发给前台窗口 ⇒ 症状是：
     *   ```
     *     点击 ▶ 成功：注入完成 (1522.7,615.5)  down=true up=true     ← 日志一片绿
     *     …然后游戏一动不动，20 s onset 超时
     *   ```
     *   10-24 第四枪就是这么白打一轮的：点 ▶ 没生效 ⇒ 游戏停在选歌界面 ⇒
     *   采集到的 20 s 全是选歌 BGM ⇒ `gate(state=WAIT_QUIET, 最长静音 10 帧 < 需要 30 帧)`
     *   ⇒ 看起来像"onset 判据不行"，实际是"根本没开始打"。
     *
     * ★ 对照（教训：对照必须自己构造）：整曲注入 `DevRun.pumpFrame` 一直是按
     *   `obtain(…, SOURCE_TOUCHSCREEN, 0)` + `setSource(...)` + `toolType=FINGER` 造的，
     *   所以它能打通整首曲子。两条路径必须一致 —— 这也是本方法存在的唯一理由。
     */
    private static MotionEvent tapEvent(long downTime, long eventTime, int action, float x, float y) {
        MotionEvent.PointerProperties props = new MotionEvent.PointerProperties();
        props.id = 0;
        props.toolType = MotionEvent.TOOL_TYPE_FINGER;
        MotionEvent.PointerCoords coords = new MotionEvent.PointerCoords();
        coords.x = x;
        coords.y = y;
        coords.pressure = 1f;
        coords.size = 1f;
        MotionEvent ev = MotionEvent.obtain(downTime, eventTime, action, 1,
                new MotionEvent.PointerProperties[]{props},
                new MotionEvent.PointerCoords[]{coords},
                0, 0, 1f, 1f, 0, 0, InputDevice.SOURCE_TOUCHSCREEN, 0);
        ev.setSource(InputDevice.SOURCE_TOUCHSCREEN);
        return ev;
    }

    /**
     * 注入一次「按下 + 抬起」。
     *
     * @return 人类可读的结果（同时用于 logcat）。**不抛异常**——异常会被转成字符串，
     *         因为调用方是要把结果打给用户看的，崩掉就没有信息了。
     */
    public static String tap(Object proxy, Method inject, float x, float y) {
        if (proxy == null) return "注入失败: proxy 为 null（先过 S0 门禁）";
        if (inject == null) return "注入失败: injectInputEvent 方法为 null（接口反射没拿到）";

        long t0 = SystemClock.uptimeMillis();
        MotionEvent down = tapEvent(t0, t0, MotionEvent.ACTION_DOWN, x, y);
        MotionEvent up = tapEvent(t0, t0 + TAP_HOLD_MS, MotionEvent.ACTION_UP, x, y);
        try {
            Object r1 = inject.invoke(proxy, down, MODE_ASYNC);
            Object r2 = inject.invoke(proxy, up, MODE_ASYNC);
            return "注入完成 (" + x + "," + y + ")  down=" + r1 + " up=" + r2;
        } catch (java.lang.reflect.InvocationTargetException e) {
            // 真正的原因在 target 里（可能是 SecurityException / IllegalArgumentException）。
            return "注入被拒: " + ShizukuProbe.brief(e.getTargetException());
        } catch (Throwable t) {
            return "注入失败: " + ShizukuProbe.brief(t);
        } finally {
            // MotionEvent 是池化对象，不 recycle 会泄漏（连续注入时会耗尽池并抛
            // "MotionEvent recycled" 一类错误）。
            down.recycle();
            up.recycle();
        }
    }
}
