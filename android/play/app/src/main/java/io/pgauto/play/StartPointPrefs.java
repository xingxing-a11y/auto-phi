package io.pgauto.play;

import android.content.Context;
import android.content.SharedPreferences;
import android.os.Build;

import java.util.Locale;

/**
 * 「用户点一次学到的 ▶ 落点」的唯一读写者。
 *
 * <h3>为什么单独成类</h3>
 * 这个量有**两个消费者**：打歌时的 {@link PlayService#startTapX}/{@link PlayService#startTapY}，
 * 与界面上的落点显示。本项目已经因为"同一个量两处实现"踩过两次
 * （`START_REL_X` 三个直乘点、`calibK` 与 `kStored` 读的 key 不同）
 * ⇒ 这里把「learned 还是出厂比例」这个判断**只写一份**，两边都调 {@link #tapX}/{@link #tapY}。
 *
 * <h3>存的是比例，不是像素</h3>
 * 存像素跨分辨率必错。存比例后按当前屏幕缩放，至少在同一宽高比下是对的。
 * 但**比例不等于可靠**：Phigros 的按钮是斜切形状、且不同宽高比会重排
 * ⇒ 只要设备指纹（含分辨率）变了，就把这个值判定为**失效**，不硬用。
 *
 * <h3>与 dx/dy 的关系（2026-10-31 定）</h3>
 * 手填偏移**已废弃**：{@link #tapX} 不再读 `start_dx_px/dy_px`。
 * 因此设备里残留 `+15/-4` 也**不生效**（等价 0/0）；{@link #clear} 会顺手删掉它们。
 * ⛔ 不许再让 learned 与 dx/dy 叠加 —— 那就是"两套来源同时生效"。
 */
public final class StartPointPrefs {

    static final String K_X_REL = "start_learn_x_rel";
    static final String K_Y_REL = "start_learn_y_rel";
    static final String K_W = "start_learn_w";
    static final String K_H = "start_learn_h";
    static final String K_FP = "start_learn_fp";
    static final String K_TS = "start_learn_ts";

    /** 学到的一个落点。 */
    public static final class Learned {
        /** 屏幕比例（0..1）。 */
        public final float xRel;
        public final float yRel;
        /** 学到时候的屏幕尺寸与设备指纹。 */
        public final int w;
        public final int h;
        public final String fp;
        public final long ts;
        /** 设备指纹与当前不一致 ⇒ 换机或分辨率变了，**不许用**。 */
        public final boolean stale;

        Learned(float xRel, float yRel, int w, int h, String fp, long ts, boolean stale) {
            this.xRel = xRel;
            this.yRel = yRel;
            this.w = w;
            this.h = h;
            this.fp = fp;
            this.ts = ts;
            this.stale = stale;
        }

        public boolean usable() {
            return !stale
                    && !Float.isNaN(xRel) && !Float.isInfinite(xRel)
                    && !Float.isNaN(yRel) && !Float.isInfinite(yRel)
                    && xRel > 0f && xRel < 1f && yRel > 0f && yRel < 1f;
        }
    }

    private StartPointPrefs() {
    }

    static SharedPreferences p(Context c) {
        return c.getSharedPreferences(PlayService.PREF, Context.MODE_PRIVATE);
    }

    /** 当前设备指纹（与 `PlayService.fp()` 同一口径：含分辨率）。 */
    static String fp(int screenW, int screenH) {
        return DevFp.of(Build.MANUFACTURER, Build.MODEL, Build.DEVICE, screenW, screenH);
    }

    /**
     * 读学到的落点；没学过返回 null。
     *
     * ⛔ 返回 null 而不是"默认 0,0"：没学过就走出厂比例，两条路必须能区分。
     */
    public static Learned load(Context c, int screenW, int screenH) {
        SharedPreferences sp = p(c);
        float xr = sp.getFloat(K_X_REL, Float.NaN);
        if (Float.isNaN(xr)) return null;
        float yr = sp.getFloat(K_Y_REL, Float.NaN);
        int w = sp.getInt(K_W, 0);
        int h = sp.getInt(K_H, 0);
        String storedFp = sp.getString(K_FP, "");
        // ts 用「秒级 epoch 存 int」：PrefGuard 只有 putFloat/putInt/putString 三条"写必回读"通道，
        // 不为一个时间戳新开一条没有回读校验的写入路径（2038 年前够用）。
        long ts = sp.getInt(K_TS, 0) * 1000L;
        boolean stale = !fp(screenW, screenH).equals(storedFp);
        return new Learned(xr, yr, w, h, storedFp, ts, stale);
    }

    /**
     * 落点 x（屏幕像素）。
     *
     * 优先级（写死）：学到的（且未失效） > 出厂比例。**没有 dx/dy 这一档。**
     */
    public static float tapX(Context c, double dstW, int screenW, int screenH) {
        Learned l = load(c, screenW, screenH);
        if (l != null && l.usable()) return (float) (l.xRel * dstW);
        return (float) (dstW * PlayService.START_REL_X);
    }

    /** 落点 y（屏幕像素）。 */
    public static float tapY(Context c, double dstH, int screenW, int screenH) {
        Learned l = load(c, screenW, screenH);
        if (l != null && l.usable()) return (float) (l.yRel * dstH);
        return (float) (dstH * PlayService.START_REL_Y);
    }

    /**
     * 落点来源，给界面显示用（**界面上必须一眼看出用的是哪一档**）。
     */
    public static String sourceOf(Context c, int screenW, int screenH) {
        Learned l = load(c, screenW, screenH);
        if (l == null) return "默认 0,0";
        if (l.stale) return String.format(Locale.ROOT, "学到的已失效（%dx%d 学于 %s）", l.w, l.h, ts(l.ts));
        return String.format(Locale.ROOT, "已学到 %s", ts(l.ts));
    }

    /**
     * 保存学到的落点。
     *
     * @return null = 成功；非 null = 失败原因（`PrefGuard` 是"写必回读"，
     *         失败必须让用户看见，不许假装成功 —— 否则界面显示"已记录"但下次仍走旧值）
     */
    public static String save(Context c, float xRel, float yRel, int w, int h) {
        if (!(xRel > 0f && xRel < 1f) || !(yRel > 0f && yRel < 1f)) {
            return String.format(Locale.ROOT, "比例越界（%.4f, %.4f）", xRel, yRel);
        }
        SharedPreferences sp = p(c);
        String e;
        if ((e = PrefGuard.putFloat(sp, K_X_REL, xRel)) != null) return e;
        if ((e = PrefGuard.putFloat(sp, K_Y_REL, yRel)) != null) return e;
        if ((e = PrefGuard.putInt(sp, K_W, w)) != null) return e;
        if ((e = PrefGuard.putInt(sp, K_H, h)) != null) return e;
        if ((e = PrefGuard.putInt(sp, K_TS, (int) (System.currentTimeMillis() / 1000))) != null) return e;
        if ((e = PrefGuard.putString(sp, K_FP, fp(w, h))) != null) return e;
        return null;
    }

    /**
     * 「恢复默认」：删掉学到的落点 + 删掉遗留的 `dx/dy` ⇒ 落点回出厂比例 0/0。
     *
     * ★ 这是**唯一**的重置入口（手填已废弃），所以两件事必须一起做：
     *   只删 learned 会留下老偏移语义，只删 dx/dy 会留下学到的点。
     *
     * @return null = 成功；非 null = 失败原因
     */
    public static String clear(Context c) {
        return PrefGuard.remove(p(c),
                K_X_REL, K_Y_REL, K_W, K_H, K_FP, K_TS,
                PlayService.KEY_START_DX, PlayService.KEY_START_DY);
    }

    static String ts(long ms) {
        if (ms <= 0) return "?";
        java.text.SimpleDateFormat f =
                new java.text.SimpleDateFormat("MM-dd HH:mm", Locale.ROOT);
        return f.format(new java.util.Date(ms));
    }
}
