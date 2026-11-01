package io.pgauto.play;

import android.content.Context;
import android.view.MotionEvent;
import android.widget.ScrollView;

/**
 * 日志面板的滚动容器：**自己独占触摸**，滚到边界再把事件交还外层页面。
 *
 * ★ 为什么 `onInterceptTouchEvent` 直接返回 true：里面的 `TextView` 一旦拿到触摸
 *   （尤其可选中文本），就会把 MOVE 吃掉 ⇒ 这里收不到拖动，表现为"日志块滑不动"。
 *   日志块是只读的，不需要子 View 参与触摸，所以这里整个接管。
 *   （2026-11-01 真机实测：加了 `setTextIsSelectable(true)` 的日志块完全滑不动。）
 *
 * ★ 为什么要 disallow 父级：外层是页面级 `ScrollView`，普通 `ScrollView` 的
 *   `onInterceptTouchEvent` 只看 touchSlop、并不知道子 View 自己也能滚
 *   ⇒ 不 disallow 的话手指一移动父级就把 MOVE 截走。
 *
 * ⚠ 交还时机只能落在 MOVE 上：父级一旦 intercept，本次手势余下的事件都不再发给子 View，
 *   直到下一次 DOWN。所以「还回去」必须发生在自己还收得到事件的时候。
 */
class LogScroll extends ScrollView {

    LogScroll(Context c) {
        super(c);
    }

    /** 独占触摸：子 View（TextView）不参与，避免它吃掉 MOVE 导致滑不动。 */
    @Override
    public boolean onInterceptTouchEvent(MotionEvent e) {
        return true;
    }

    @Override
    public boolean onTouchEvent(MotionEvent e) {
        final android.view.ViewParent p = getParent();
        switch (e.getActionMasked()) {
            case MotionEvent.ACTION_DOWN:
                // 先由我处理：否则外层页面会在 MOVE 时抢先 intercept
                if (p != null) p.requestDisallowInterceptTouchEvent(true);
                break;
            case MotionEvent.ACTION_MOVE:
                // 已经到顶/到底（或内容不足一屏）⇒ 把后续 MOVE 让给页面
                if (!canScrollVertically(1) && !canScrollVertically(-1) && p != null) {
                    p.requestDisallowInterceptTouchEvent(false);
                }
                break;
            case MotionEvent.ACTION_UP:
            case MotionEvent.ACTION_CANCEL:
                // 收尾复位：漏了这行，页面会从此滚不动（下一次 DOWN 才恢复）
                if (p != null) p.requestDisallowInterceptTouchEvent(false);
                break;
            default:
                break;
        }
        return super.onTouchEvent(e);
    }
}
