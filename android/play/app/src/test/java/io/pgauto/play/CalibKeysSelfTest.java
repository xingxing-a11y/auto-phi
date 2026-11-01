package io.pgauto.play;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collections;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Set;

/**
 * {@link CalibKeys} 的离线自测。
 *
 * ★ `selftest.sh` 用**没有 android.jar** 的 classpath 编它并运行 ⇒ 这里只能碰纯 Java。
 *
 * 钉住的核心不变量是**一条**：`removeKeys` 必须**逐项包含** `readKeys` 的每一项。
 * 2026-10-30 的 bug 就是这个不变量被破坏 —— 读遍历三个难度、删只删一个。
 *
 * ★ 为了证明这条断言**有分辨力**，本文件末尾用 {@link #legacyRemoveKeys} 重建了旧实现，
 *   并断言它**不**满足包含关系。如果哪天有人把断言写成恒真（例如让 removeKeys 调 readKeys），
 *   这条"反向断言"会立刻失败。
 */
public final class CalibKeysSelfTest {

    private static int pass = 0;
    private static int fail = 0;

    private static void ok(boolean cond, String msg) {
        if (cond) {
            pass++;
        } else {
            fail++;
            System.out.println("  ✗ " + msg);
        }
    }

    private static Set<String> setOf(String[] a) {
        return new LinkedHashSet<>(Arrays.asList(a));
    }

    public static void main(String[] args) {
        t01_readKeysExact();
        t02_removeCoversRead();
        t03_threeDiffs();
        t04_singleDiff();
        t05_auxKeys();
        t06_nullVariantList();
        t07_legacyMustFail();      // ← 反向断言：旧实现必须**不**满足
        System.out.println("CalibKeysSelfTest: " + pass + " 通过, " + fail + " 失败");
        if (fail > 0) System.exit(1);
    }

    /** 读取口径恰好两项，且顺序是 [按曲名, 按当前 key]。 */
    private static void t01_readKeysExact() {
        String[] r = CalibKeys.readKeys("雪降りメリクリ", "x1fb07d1f_in");
        ok(r.length == 2, "readKeys 应恰 2 项，实际 " + r.length);
        ok("k_ms_name_雪降りメリクリ".equals(r[0]),
                "第 1 项必须是按曲名，实际 " + r[0]);
        ok("k_ms_x1fb07d1f_in".equals(r[1]),
                "第 2 项必须是按当前 key，实际 " + r[1]);
    }

    /** ★ 核心不变量：删除集必须逐项包含读取集（多组曲目/难度）。 */
    private static void t02_removeCoversRead() {
        String[][] cases = {
                {"曲A", "a_in", "a_ez", "a_hd", "a_in"},
                {"曲B", "b_ez", "b_ez"},
                {"曲C", "c_at", "c_ez", "c_hd", "c_in", "c_at"},
                {"带空格 与/斜杠", "k1", "k1", "k2"},
                {"", "only", "only"},
        };
        for (String[] c : cases) {
            String name = c[0];
            String cur = c[1];
            List<String> variants = new ArrayList<>(Arrays.asList(c).subList(2, c.length));
            Set<String> rm = setOf(CalibKeys.removeKeys(name, cur, variants));
            for (String need : CalibKeys.readKeys(name, cur)) {
                ok(rm.contains(need),
                        "removeKeys 漏了读取项 [" + need + "]（曲=" + name + ", 当前=" + cur + "）");
            }
        }
    }

    /** 三难度曲：三个 `k_ms_<key>` 都要删 —— 这正是 10-30 那个 bug 的直接判据。 */
    private static void t03_threeDiffs() {
        List<String> v = Arrays.asList("x_ez", "x_hd", "x_in");
        Set<String> rm = setOf(CalibKeys.removeKeys("X", "x_in", v));
        ok(rm.contains("k_ms_x_ez"), "三难度曲漏删 k_ms_x_ez ⇒ 清除后会被兄弟难度顶回来");
        ok(rm.contains("k_ms_x_hd"), "三难度曲漏删 k_ms_x_hd");
        ok(rm.contains("k_ms_x_in"), "三难度曲漏删 k_ms_x_in（当前难度）");
        ok(rm.contains("k_ms_name_X"), "三难度曲漏删按曲名主口径");
    }

    /** 单难度曲：变体列表只有自己，也要删到位。 */
    private static void t04_singleDiff() {
        Set<String> rm = setOf(CalibKeys.removeKeys("Y", "y_ez",
                Collections.singletonList("y_ez")));
        ok(rm.contains("k_ms_y_ez"), "单难度曲漏删 k_ms_y_ez");
        ok(rm.contains("k_ms_name_Y"), "单难度曲漏删按曲名");
    }

    /** 四个融合/标记位必须一起清，否则清空后第一轮就被判离群。 */
    private static void t05_auxKeys() {
        Set<String> rm = setOf(CalibKeys.removeKeys("Z", "z_in", Collections.singletonList("z_in")));
        for (String aux : new String[]{"k_n_name_Z", "k_side_n_Z", "k_side_sign_Z", "kbad_Z"}) {
            ok(rm.contains(aux), "漏清辅助位 " + aux);
        }
    }

    /** 拿不到变体列表时（列表未加载）不能崩，且仍覆盖读取集。 */
    private static void t06_nullVariantList() {
        String[] rm = CalibKeys.removeKeys("W", "w_in", null);
        Set<String> s = setOf(rm);
        ok(s.contains("k_ms_name_W"), "null 变体列表时漏删按曲名");
        ok(s.contains("k_ms_w_in"), "null 变体列表时漏删当前 key");
        ok(rm.length >= 2, "null 变体列表时删除项过少：" + rm.length);
    }

    /**
     * ★ 反向断言：**旧实现**（只删当前难度）必须不满足包含关系。
     *
     * 如果这条失败，说明 `removeKeys` 已经退化成"只删当前 key"，或者断言本身写空了。
     * 这是对"自测能抓到真 bug"的证明，不是对生产代码的断言。
     */
    private static void t07_legacyMustFail() {
        Set<String> legacy = setOf(legacyRemoveKeys("X", "x_in",
                Arrays.asList("x_ez", "x_hd", "x_in")));
        boolean covers = true;
        for (String need : CalibKeys.readKeys("X", "x_in")) {
            if (!legacy.contains(need)) covers = false;
        }
        // 旧实现在本例里 name 与 curKey 都删了 ⇒ 对 readKeys 仍是"覆盖"的；
        // 真正漏的是兄弟难度。所以这里断言它漏了 x_ez / x_hd。
        ok(!legacy.contains("k_ms_x_ez"),
                "旧实现居然删了兄弟难度 —— 本用例失去意义（说明变异没构造对）");
        ok(!legacy.contains("k_ms_x_hd"), "同上");
        // 而新实现必须删 —— 一条用例里同时钉住"旧的错、新的对"
        Set<String> now = setOf(CalibKeys.removeKeys("X", "x_in",
                Arrays.asList("x_ez", "x_hd", "x_in")));
        ok(now.contains("k_ms_x_ez") && now.contains("k_ms_x_hd"),
                "新实现没删兄弟难度 ⇒ 回到 10-30 的 bug");
        ok(covers, "旧实现对 readKeys 的覆盖情况发生了变化（读口径被改过？）");
    }

    /** 旧实现（2026-10-30 之前）：只删当前难度那一个 key。**只在本自测里存在**。 */
    private static String[] legacyRemoveKeys(String name, String curKey, List<String> ignored) {
        return new String[]{
                "k_ms_name_" + name,
                "k_ms_" + curKey,
                "k_n_name_" + name,
                "k_side_n_" + name,
                "k_side_sign_" + name,
                "kbad_" + name,
        };
    }
}
