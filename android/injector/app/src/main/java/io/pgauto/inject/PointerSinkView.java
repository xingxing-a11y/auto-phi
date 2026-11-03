package io.pgauto.inject;

import android.content.Context;
import android.os.SystemClock;
import android.view.MotionEvent;
import android.view.View;

/**
 * 接住注入事件的「靶子」。
 *
 * ★ 为什么需要它：{@code injectInputEvent} 返回 true **只代表事件成功入队**，
 *   不代表被任何一个 App 收到。本项目已经踩过「看起来成功、数据全废」的坑
 *   （10-19 音量 0 时录音峰值恒 0、播放却不报错）。
 *   所以要有一个真的 View 报出「我收到了几个指针」。
 *
 * ★ 它还顺带测出了**端到端分发延迟**：事件里的 eventTime 是注入时写进去的，
 *   收到时的 uptimeMillis 减去它，就是「注入 → App 收到」的真实耗时。
 *   这个数才是判读精度真正吃的延迟；调用成本只是它的一部分。
 */
public class PointerSinkView extends View {

    private int maxPointers = 0;
    private int eventCount = 0;
    private long maxDispatchMs = 0;
    private long sumDispatchMs = 0;
    private int dispatchSamples = 0;
    private final StringBuilder seq = new StringBuilder();

    public PointerSinkView(Context c) {
        super(c);
        // 不 clickable 的 View 不会收到 onTouchEvent：事件会被父容器接管或丢弃。
        setClickable(true);
        setFocusable(true);
    }

    public void reset() {
        maxPointers = 0;
        eventCount = 0;
        maxDispatchMs = 0;
        sumDispatchMs = 0;
        dispatchSamples = 0;
        seq.setLength(0);
    }

    @Override
    public boolean onTouchEvent(MotionEvent e) {
        int n = e.getPointerCount();
        eventCount++;
        if (n > maxPointers) maxPointers = n;

        // 用 eventTime（注入时写入的时刻）与此刻比较 ⇒ 端到端分发延迟
        long delay = SystemClock.uptimeMillis() - e.getEventTime();
        if (delay >= 0 && delay < 5000) {   // 负数/荒谬值说明时钟基准不同，不计入
            dispatchSamples++;
            sumDispatchMs += delay;
            if (delay > maxDispatchMs) maxDispatchMs = delay;
        }

        if (seq.length() < 220) {
            seq.append(actionName(e.getActionMasked())).append('(').append(n).append(") ");
        }
        return true;
    }

    public String report() {
        if (eventCount == 0) {
            return "靶子收到 0 条事件（注入入队但没有任何 View 收到）";
        }
        String avg = dispatchSamples == 0 ? "n/a"
                : String.format(java.util.Locale.ROOT, "%.1f", (double) sumDispatchMs / dispatchSamples);
        return "靶子收到 " + eventCount + " 条事件，最大指针数 " + maxPointers
                + "；端到端分发延迟 avg=" + avg + " ms max=" + maxDispatchMs + " ms"
                + "；序列: " + seq;
    }

    public int getMaxPointers() { return maxPointers; }

    private static String actionName(int a) {
        switch (a) {
            case MotionEvent.ACTION_DOWN: return "DOWN";
            case MotionEvent.ACTION_UP: return "UP";
            case MotionEvent.ACTION_MOVE: return "MOVE";
            case MotionEvent.ACTION_CANCEL: return "CANCEL";
            case MotionEvent.ACTION_POINTER_DOWN: return "PDOWN";
            case MotionEvent.ACTION_POINTER_UP: return "PUP";
            default: return "A" + a;
        }
    }
}
