package io.pgauto.play;

/**
 * 从一次「学习落点」窗口里挑出**第一次按下**的那个点。
 *
 * <h3>为什么"第一次"</h3>
 * 用户 2026-10-31 12:12 定的口径：**只认第一次点击**。
 * 窗口里可能先点了难度、曲目、返回，再点 ▶；按"最后一个"会记错，
 * 按"第一次"则由用户自己保证（界面文案已写明）。所以这里**不做任何猜测**，
 * 只负责把第一个 DOWN 挑出来。
 *
 * <h3>为什么单独成类</h3>
 * `selftest.sh` 用**没有 android.jar** 的 classpath 编它 ⇒ 必须纯 Java。
 * 「多点击取第一个」这条规则是用户明确指定的，必须能被离线断言钉住，
 * 否则以后有人"顺手"改成取最后一个，没有任何测试会红。
 */
public final class FirstDown {

    /** 没有按下事件。 */
    public static final int NONE = -1;

    /**
     * 动作编码：**0 = 按下**。
     *
     * ⚠ 这三个常量是**全项目动作编码的唯一来源**：`GetEventParser` 产出它们、`FirstDown` 消费它们。
     *   不要在各处另写 0/1/2 —— 本项目最贵的一类 bug 就是"同一个量两处定义"。
     *   也**不要**和 `InjectTrace` 的 act 混为一谈（那边 0=DOWN 是巧合相同，5/6 是 POINTER_DOWN/UP，语义不同源）。
     */
    public static final int ACT_DOWN = 0;
    /** 抬起。 */
    public static final int ACT_UP = 1;
    /** 移动（手指按住时坐标变化）。 */
    public static final int ACT_MOVE = 2;

    private FirstDown() {
    }

    /**
     * 返回第一个按下事件的下标；没有则 {@link #NONE}。
     *
     * @throws IllegalArgumentException 序列为 null，或长度不一致（宁可炸，不要静默错位）
     */
    public static int pick(int[] act, int[] rawX, int[] rawY) {
        check(act, rawX, rawY);
        for (int i = 0; i < act.length; i++) {
            if (act[i] == ACT_DOWN) return i;
        }
        return NONE;
    }

    /**
     * 第一个按下点的**原始坐标** `{rawX, rawY}`；没有按下返回 `null`。
     *
     * ⛔ 返回 null 而不是 `{0,0}`：`{0,0}` 是一个合法坐标（屏幕左上角），
     *   混用会让"没点"和"点了左上角"无法区分 —— 正是本项目最贵的 bug 形状。
     */
    public static int[] firstDownPoint(int[] act, int[] rawX, int[] rawY) {
        int i = pick(act, rawX, rawY);
        if (i == NONE) return null;
        return new int[]{rawX[i], rawY[i]};
    }

    private static void check(int[] act, int[] rawX, int[] rawY) {
        if (act == null || rawX == null || rawY == null) {
            throw new IllegalArgumentException("act/rawX/rawY 都不能为 null");
        }
        if (act.length != rawX.length || act.length != rawY.length) {
            throw new IllegalArgumentException("三个序列长度必须一致：act=" + act.length
                    + " rawX=" + rawX.length + " rawY=" + rawY.length
                    + "（长度不一致说明解析层已经错位，继续算只会得到错的坐标）");
        }
    }

    /** 调试用：把序列打印成一行，便于日志里看"到底收到了什么"。 */
    public static String dump(int[] act, int[] rawX, int[] rawY) {
        check(act, rawX, rawY);
        StringBuilder b = new StringBuilder("n=").append(act.length).append(" [");
        for (int i = 0; i < act.length; i++) {
            if (i > 0) b.append(", ");
            b.append(act[i]).append(':').append(rawX[i]).append('x').append(rawY[i]);
        }
        return b.append(']').toString();
    }
}
