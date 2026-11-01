package io.pgauto.inject.sched;

import java.util.ArrayList;
import java.util.List;

/**
 * 指针状态机：把载荷的「单指针动作流」还原成 InputDispatcher 要求的**完整 MotionEvent 帧**。
 *
 * ★ 为什么必须有这一层（本项目最容易静默出错的一块）：
 *   载荷里每条事件只描述**一个**指针的动作（t_ms, action, pointer_id, x, y），
 *   但 Android 的 MotionEvent 携带的是**当时所有按下中的指针**。少一个指针，
 *   InputDispatcher 会认为状态不一致并丢弃整条事件 —— 不报错、不抛异常，
 *   表现只是"游戏里那个键没按到"。
 *
 * ★ 三条 AOSP 级规则（写错任何一条都是静默失败）：
 *   1. {@code ACTION_POINTER_DOWN/UP} 的 actionIndex 是**数组下标**，不是指针 id；
 *   2. {@code POINTER_UP} 抬手的那一帧，该指针**仍在数组里**（下一帧才消失）；
 *   3. 只有 count==1 时才能用 {@code ACTION_DOWN}/{@code ACTION_UP}。
 *
 * ★ 本类**零 Android 依赖**（动作码用字面量），这样 PC 上能直接 javac + 断言不变量：
 *   端内只负责「Frame → MotionEvent」的搬运。{@link #ACTION_* } 与
 *   {@code android.view.MotionEvent} 的一致性由 DevRun 在启动时**运行时断言**，
 *   防 API 漂移（断言失败即拒绝开火，不猜）。
 *
 * ⚠ 丢弃 MOVE 时**状态照样要推进**：状态记的是「载荷真值」（最新位置），
 *   否则下一个关键事件会把旧坐标带给游戏（Drag 会滑出判定区）。
 */
public final class Gesture {

    // 值必须等于 android.view.MotionEvent.ACTION_*（DevRun 启动时断言）
    public static final int ACTION_DOWN = 0;
    public static final int ACTION_UP = 1;
    public static final int ACTION_MOVE = 2;
    public static final int ACTION_CANCEL = 3;
    public static final int ACTION_POINTER_DOWN = 5;
    public static final int ACTION_POINTER_UP = 6;
    public static final int ACTION_POINTER_INDEX_SHIFT = 8;

    /** 取动作类型（低 8 位），等价 MotionEvent.getActionMasked()。 */
    public static int masked(int action) { return action & 0xff; }

    /** 取 actionIndex，等价 MotionEvent.getActionIndex()。 */
    public static int actionIndex(int action) { return (action >> ACTION_POINTER_INDEX_SHIFT) & 0xff; }

    /** 一帧待注入事件。坐标已是真机像素。 */
    public record Frame(double tMs, int action, int count, int[] ids, int[] xs, int[] ys) {
        public String fmt() {
            StringBuilder sb = new StringBuilder();
            sb.append(masked(action));
            if (masked(action) == ACTION_POINTER_DOWN || masked(action) == ACTION_POINTER_UP) {
                sb.append("@idx=").append(actionIndex(action));
            }
            sb.append(" n=").append(count).append(" [");
            for (int i = 0; i < count; i++) {
                if (i > 0) sb.append(' ');
                sb.append(ids[i]).append(':').append(xs[i]).append(',').append(ys[i]);
            }
            return sb.append(']').toString();
        }
    }

    private final List<Integer> ids = new ArrayList<>();
    private final List<int[]> pos = new ArrayList<>();

    /** 孤儿计数：MOVE/UP 的 pid 从没 DOWN 过（追赶式启动时会正常出现，要计数不要慌）。 */
    public int orphanMove, orphanUp, dupDown, unknownAction;

    public int activeCount() { return ids.size(); }

    public boolean isEmpty() { return ids.isEmpty(); }

    /**
     * **静默保活帧**：不改任何指针位置，只把当前状态重发一遍。
     *
     * ★ 为什么需要（2026-10-25 20:5x，moonlight IN 第 1193 个音符）：那处谱面有 **1.35 s 一条事件都没有**
     *   （没有音符需要移动手指），而音符正好落在静默中间。真机上那个 Drag 判 Miss，
     *   而离线回放认为"手指就在音符上"——差别只能是：**游戏侧长时间收不到 MotionEvent 时
     *   不认为那些手指还在按**。⇒ 静默期间定期重发一次同位置 MOVE（不改变判定）。
     * ⚠ 只填空隙，不是持续轮询：调用方只在"距上一帧超过阈值"时补，避免事件量膨胀。
     */
    public Frame keepalive(double tMs) {
        if (ids.isEmpty()) return null;
        int n = ids.size();
        int[] ida = new int[n], xs = new int[n], ys = new int[n];
        for (int i = 0; i < n; i++) {
            ida[i] = ids.get(i);
            xs[i] = pos.get(i)[0];
            ys[i] = pos.get(i)[1];
        }
        return new Frame(tMs, ACTION_MOVE, n, ida, xs, ys);
    }

    private int indexOf(int pid) {
        for (int i = 0; i < ids.size(); i++) if (ids.get(i) == pid) return i;
        return -1;
    }

    private void set(int idx, Scheduler.Cmd c) {
        pos.get(idx)[0] = c.devX();
        pos.get(idx)[1] = c.devY();
    }

    /**
     * 推进一条指令。
     *
     * @return 要注入的帧；null = 本条不产生帧（孤儿 MOVE / 孤儿 UP / 未知动作）
     */
    public Frame step(Scheduler.Cmd c) {
        String a = c.action();
        int pid = c.pid();
        int idx = indexOf(pid);
        switch (a) {
            case "DOWN", "POINTER_DOWN" -> {
                if (idx >= 0) {
                    // 重复按下同一个 pid：载荷不该这么给，但真机上"当成 MOVE"比"再按一次"安全
                    dupDown++;
                    set(idx, c);
                    return build(c, ACTION_MOVE);
                }
                int[] p = new int[]{c.devX(), c.devY()};
                ids.add(pid);
                pos.add(p);
                int i = ids.size() - 1;
                int action = (ids.size() == 1)
                        ? ACTION_DOWN
                        : (ACTION_POINTER_DOWN | (i << ACTION_POINTER_INDEX_SHIFT));
                return build(c, action);
            }
            case "MOVE" -> {
                if (idx < 0) { orphanMove++; return null; }
                set(idx, c);
                return build(c, ACTION_MOVE);
            }
            case "POINTER_UP", "UP" -> {
                if (idx < 0) { orphanUp++; return null; }
                set(idx, c);
                int action = (ids.size() == 1)
                        ? ACTION_UP
                        : (ACTION_POINTER_UP | (idx << ACTION_POINTER_INDEX_SHIFT));
                Frame f = build(c, action);
                ids.remove(idx);      // ★ 先出帧再删：抬手那一帧指针仍在数组里
                pos.remove(idx);
                return f;
            }
            default -> {
                unknownAction++;
                return null;
            }
        }
    }

    /**
     * 收尾：把仍按下的指针**逐个抬起**（逆序）。
     *
     * ★ 必须做，且必须真的注入成功：残留的虚拟指针会让 InputDispatcher 从此吞掉所有
     *   DOWN，外部表现是「怎么点都没反应」，只有重启能清（10-16 实测）。
     */
    public List<Frame> releaseAll(long tMs) {
        List<Frame> out = new ArrayList<>();
        while (!ids.isEmpty()) {
            int idx = ids.size() - 1;
            int action = (ids.size() == 1)
                    ? ACTION_UP
                    : (ACTION_POINTER_UP | (idx << ACTION_POINTER_INDEX_SHIFT));
            out.add(new Frame(tMs, action, ids.size(),
                    toArr(0), toArr(1), toArr(2)));
            ids.remove(idx);
            pos.remove(idx);
        }
        return out;
    }

    /** kind: 0=ids 1=xs 2=ys */
    private int[] toArr(int kind) {
        int[] a = new int[ids.size()];
        for (int i = 0; i < a.length; i++) {
            a[i] = switch (kind) {
                case 0 -> ids.get(i);
                case 1 -> pos.get(i)[0];
                default -> pos.get(i)[1];
            };
        }
        return a;
    }

    private Frame build(Scheduler.Cmd c, int action) {
        return new Frame(c.tMs(), action, ids.size(), toArr(0), toArr(1), toArr(2));
    }
}
