package io.pgauto.play;

import java.util.LinkedHashSet;
import java.util.List;

/**
 * 标定值 `K` 在 SharedPreferences 里的 **key 契约**（纯逻辑，零 Android 依赖）。
 *
 * ★ 为什么单独成类（2026-10-30 真机 bug）：
 *   用户点「清除」，toast 说「已清除」，但界面上 K 还在。根因是**读的 key 集合比删的大**：
 *     · 界面 {@link #readKeys} 的旧口径会遍历整组变体（`k_ms_<每档 key>`）；
 *     · 而 `clearK` 只删了**当前难度**那一个 ⇒ 兄弟难度的值把清除效果盖住了。
 *   这个类把「读哪些」「删哪些」放在一处，并用 {@link #removeKeys} 的**逐项包含**做断言 ——
 *   将来谁给读取加一级、忘了给删除加，自测立刻红。
 *
 * ⛔ `removeKeys` **故意不调用 `readKeys`**：那样"删除集 ⊇ 读取集"恒真，等于空转。
 *   两边各自列一遍，重复是刻意的（本项目被"委托函数测旧行为"坑过）。
 *
 * ⛔ 只能用 `java.*`：`android/play/selftest.sh` 的编译集**不带 android.jar**，
 *   而且构建链（aapt2 + javac + d8）没开 desugaring ⇒ `List.of` 会编译过、运行期崩。
 */
public final class CalibKeys {

    private CalibKeys() {
    }

    /** 按曲名（主口径）。 */
    public static String nameKey(String name) {
        return "k_ms_name_" + name;
    }

    /** 按 (曲,难度)（旧口径，也是 `k_ms_name_` 丢失时的兜底）。 */
    public static String keyKey(String key) {
        return "k_ms_" + key;
    }

    /**
     * 读 `K` 要看的 key，**按优先级**。
     *
     * ★ 必须与 `PlayService.kStored` 逐项一致 —— 否则会出现"界面显示 A、打歌用 B"
     *   （2026-10-30 之前就是这个状态：界面会遍历兄弟变体，服务不会）。
     */
    public static String[] readKeys(String name, String curKey) {
        return new String[]{nameKey(name), keyKey(curKey)};
    }

    /**
     * 清除该曲 `K` 要删的 key。
     *
     * ★ 除了 {@link #readKeys} 的两项，还要删**整组变体**的 `k_ms_<key>` ——
     *   否则兄弟难度那一份会在下次读取时把值顶回来。
     * ★ 另删四个融合/标记位：`k_n_`（样本数）、`k_side_n_`/`k_side_sign_`（连续同侧计数）、
     *   `kbad_`（互相关不可靠标记）。留着它们会让"清空"后第一轮就被判离群。
     *
     * @param variantKeys 该曲**所有**难度的 key（`SongLib.Group.variants()` 的 key 列表）；
     *                    调用方拿不到时传 `Collections.singletonList(curKey)`。
     */
    public static String[] removeKeys(String name, String curKey, List<String> variantKeys) {
        LinkedHashSet<String> s = new LinkedHashSet<>();
        s.add("k_ms_name_" + name);        // ← 与 readKeys 第 1 项重复列，刻意
        s.add("k_ms_" + curKey);           // ← 与 readKeys 第 2 项重复列，刻意
        if (variantKeys != null) {
            for (String v : variantKeys) {
                if (v != null && !v.isEmpty()) s.add("k_ms_" + v);
            }
        }
        s.add("k_n_name_" + name);
        s.add("k_side_n_" + name);
        s.add("k_side_sign_" + name);
        s.add("kbad_" + name);
        return s.toArray(new String[0]);
    }
}
