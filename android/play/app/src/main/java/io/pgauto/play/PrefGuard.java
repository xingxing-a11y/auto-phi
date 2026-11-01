package io.pgauto.play;

import android.content.SharedPreferences;

/**
 * G1 的**唯一实现**：写盘必须 `commit()` + **精确回读**。
 *
 * ★ 为什么抽这一个类（2026-10-25 补）：G1 先前只在 `PlayService` 里落实，
 *   而**用户手改参数的路径全在 Activity**（`c`、手填 `lead`、清除 `lead`、旧 K 迁移）⇒
 *   那 4 处仍是 `apply()`。这正是 G1 最初要防的形状：16:14 那次"界面有值、服务读到 NaN"
 *   就是异步落盘被 force-stop 打断。同一个判据必须只有一份实现（MEMORY 教训 9），
 *   所以 PlayService 与 MainActivity 现在都走这里，各自只负责"失败怎么响"。
 *
 * ★ 为什么回读用**精确比较**而不是 epsilon：float 经 SharedPreferences 往返是精确的，
 *   用 epsilon 会把"写成另一个很接近的值"当成成功 —— 那正是"静默写坏"。
 *
 * ★ 为什么不用 `apply()`：它把 fsync 交给后台线程，进程被杀就丢；参数一轮只写几个值，
 *   `commit()` 的代价可以忽略。
 *
 * 返回值约定：`null` = 成功；否则返回**短原因**（不含换行），调用方负责落日志/提示。
 */
public final class PrefGuard {

    private PrefGuard() {}

    /** 写 float 并回读校验。 */
    public static String putFloat(SharedPreferences p, String key, float v) {
        try {
            boolean ok;
            try {
                ok = p.edit().putFloat(key, v).commit();
            } catch (Throwable t) {
                return "commit 异常：" + brief(t);
            }
            float back = p.getFloat(key, Float.NaN);
            if (!ok) return "commit=false（写 " + v + "，读 " + back + "）";
            if (Float.compare(back, v) != 0) return "回读不一致（写 " + v + "，读 " + back + "）";
            return null;
        } catch (Throwable t) {
            return "回读异常：" + brief(t);
        }
    }

    /** 写 int 并回读校验（`k_n` / `lead_n` / 连续同侧计数都不能静默丢）。 */
    public static String putInt(SharedPreferences p, String key, int v) {
        try {
            boolean ok;
            try {
                ok = p.edit().putInt(key, v).commit();
            } catch (Throwable t) {
                return "commit 异常：" + brief(t);
            }
            int back = p.getInt(key, Integer.MIN_VALUE);
            if (!ok) return "commit=false（写 " + v + "，读 " + back + "）";
            if (back != v) return "回读不一致（写 " + v + "，读 " + back + "）";
            return null;
        } catch (Throwable t) {
            return "回读异常：" + brief(t);
        }
    }

    /** 写字符串并回读校验（设备指纹：写丢了会退化成"首跑"而不是"变了"）。 */
    public static String putString(SharedPreferences p, String key, String v) {
        try {
            boolean ok;
            try {
                ok = p.edit().putString(key, v).commit();
            } catch (Throwable t) {
                return "commit 异常：" + brief(t);
            }
            String back = p.getString(key, null);
            if (!ok) return "commit=false";
            if (back == null || !back.equals(v)) return "回读不一致（写 " + v + "，读 " + back + "）";
            return null;
        } catch (Throwable t) {
            return "回读异常：" + brief(t);
        }
    }

    /**
     * 删除若干键并校验"确实不在了"。
     *
     * ★ 删除也要校验：`remove()` 丢写会造成"界面说已清除、服务仍读到旧值"，
     *   而那个旧值会继续参与预派 —— 比写错更难发现。
     */
    public static String remove(SharedPreferences p, String... keys) {
        if (keys == null || keys.length == 0) return null;
        try {
            SharedPreferences.Editor ed = p.edit();
            for (String k : keys) ed.remove(k);
            boolean ok;
            try {
                ok = ed.commit();
            } catch (Throwable t) {
                return "commit 异常：" + brief(t);
            }
            if (!ok) return "commit=false";
            for (String k : keys) {
                if (p.contains(k)) return "回读仍存在：" + k;
            }
            return null;
        } catch (Throwable t) {
            return "回读异常：" + brief(t);
        }
    }

    private static String brief(Throwable t) {
        String m = t.getMessage();
        return t.getClass().getSimpleName() + (m == null || m.isEmpty() ? "" : "：" + m);
    }
}
