package io.pgauto.play;

import java.util.Locale;

/**
 * `zero=tap` 模式的 lead 解析规则（纯 Java，零 Android 依赖）。
 *
 * ## 这个类为什么存在
 *
 * 10-24 遍历查出两个叠加的问题：
 *   ① `MainActivity.launchPlay` **从不传** `EXTRA_LEAD_MS`，所以 `zero=tap` 路径
 *      恒用 `PlayService.DEFAULT_LEAD_MS = 7479.88`；
 *   ② 那个常数写着「本曲专属」，但代码里**没有任何曲目校验** ⇒ 在任何一首曲上
 *      选 `zero=tap` 都会用 bounded_in 的数。D 池跨曲能差 **300+ ms**
 *      （bounded_in 7.48 vs 其余 7.13~7.34），症状是「链路全绿、注入条数对得上、
 *      成绩全 Miss」，日志里没有一个"错"字。
 *
 * 被删掉的 `7479.88` **有出处**，但不是"本曲的 lead"：它是 bounded_in 两轮 `d_ideal`
 * 的池中位 `(7.44334 + 7.51643)/2 = 7.479885`（FACTS §155 那条日志里的「池中位数 n=2，
 * 跨度 73ms」）。问题有三个，缺一都不至于出事、合起来必然出事：
 *   · 它是**池统计量**：两轮实测本身相差 73 ms，取均不等于任何一轮的真实 lead；
 *   · 真正打出 φ 的是第 3 轮，`zero − t0` = **7520.086 ms**；
 *   · 而 `launchPlay` 从不传 `EXTRA_LEAD_MS` ⇒ 这个数成了**任何曲**的默认值，无曲目校验。
 *
 * ⇒ 一个"既不属于本曲、又是两轮平均"的常数不该有默认值身份。现在它只能是
 *   manifest 里某曲**实测**的 `tap_lead_ms`，或调试者显式传的 `EXTRA_LEAD_MS`。
 *
 * ## 现在的规则（按优先级）
 *
 * 1. 调用方显式给的 `EXTRA_LEAD_MS`（调试 / 命令行）⇒ 用它，但必须落在合理区间；
 * 2. 曲目自带的 `manifest.tap_lead_ms`（PC 侧 `pack_assets.TAP_LEAD_MS_BY_KEY`）⇒ 用它；
 * 3. 都没有 ⇒ **抛异常**。
 *
 * ⛔ 绝不允许「回退到某个默认常数」：那等于用别的曲的常数打分。
 *   没测过 lead 的曲请改用 `zero=onset` —— onset 路径不需要 lead。
 */
public final class TapLead {

    /** lead 的物理合理区间（ms）。低于 1 ms 一定是单位写错；高于 60 s 一定是秒当毫秒。 */
    public static final double MIN_MS = 1.0;
    public static final double MAX_MS = 60_000.0;

    /**
     * 解析本轮该用的 lead（毫秒）。
     *
     * @param overrideMs 调试覆盖值；**null 表示"没给"**，0/负/NaN 都算"给了个非法值"（会抛）
     * @param songLeadMs 曲目自带值（`SongLib.Song.tapLeadMs`）；0 表示本曲没测过
     * @return 可用的 lead（ms）
     * @throws IllegalArgumentException 两个来源都拿不到合法值（消息已是人话）
     */
    public static double resolve(Double overrideMs, double songLeadMs) {
        if (overrideMs != null) {
            return checked(overrideMs, "命令行 lead_ms");
        }
        return checked(songLeadMs, "曲目自带的 tap_lead_ms（本曲没测过 ⇒ 请改用 onset）");
    }

    private static double checked(double v, String who) {
        if (Double.isNaN(v) || v < MIN_MS || v > MAX_MS) {
            throw new IllegalArgumentException(String.format(Locale.ROOT,
                    "%s = %.3f ms 不在 [%.0f, %.0f] ms 内 ⇒ 拒绝用 tap 零点"
                            + "（onset 路径不需要 lead，换过去即可）",
                    who, v, MIN_MS, MAX_MS));
        }
        return v;
    }

    private TapLead() {}
}
