package io.pgauto.play;

import java.io.ByteArrayInputStream;
import java.nio.charset.StandardCharsets;

/**
 * `DevFp`（G2 设备指纹）与 `BuildInfo`（G13 构建自证）的自测（纯 JVM）。
 *
 * ★ 这两条都属于"显示/判定用的元信息"，写错了不会让任何东西崩，只会静默带偏：
 *   · 指纹把「首次运行」判成「换了设备」⇒ 每台新机第一轮都白重标；
 *   · 指纹把「换了设备」判成「同机」⇒ 拿旧设备的 K/c 打歌，整曲偏几百毫秒；
 *   · BuildInfo 拼错 ⇒ trace 里那行 `ver=` 变成假证据（比没有更坏）。
 *   ⇒ 所以断言盯的是**三种指纹状态的区分**与**brief() 的逐字符形态**。
 */
public final class GuardSelfTest {

    private static int pass = 0, fail = 0;

    public static void main(String[] args) {
        fp_shape();
        fp_compare_three_states();
        build_parse_full();
        build_parse_partial_and_junk();
        build_unknown();

        System.out.println("GuardSelfTest: " + pass + " 通过, " + fail + " 失败");
        if (fail > 0) throw new AssertionError("DevFp/BuildInfo 自测失败 " + fail + " 项");
    }

    // ─────────────────────────── G2 ───────────────────────────

    private static void fp_shape() {
        String s = DevFp.of("HONOR", "ALT-AN00", "HNALT", 1610, 720);
        ok("honor|alt-an00|hnalt|1610x720".equals(s), "指纹串形态不对，实得 " + s);
        // ★ 旋转/方向变化不得改变指纹：否则横竖屏切换就会被判成"换了设备"
        ok(s.equals(DevFp.of("HONOR", "ALT-AN00", "HNALT", 720, 1610)),
                "分辨率必须按 max×min 归一（换方向不能改指纹）");
        // 分隔符/空白必须被换掉，否则会造成"看起来同机、其实不同"的歧义
        ok("a_b_c".equals(DevFp.clean("A|B C")), "clean 没处理 | 与空格，实得 " + DevFp.clean("A|B C"));
        ok("-".equals(DevFp.clean(null)), "null 字段应写成 -");
        ok("-".equals(DevFp.clean("   ")), "空白字段应写成 -（不能给空串，否则 || 会连在一起）");
    }

    private static void fp_compare_three_states() {
        String cur = DevFp.of("HONOR", "ALT-AN00", "HNALT", 1610, 720);
        ok(DevFp.compare(null, cur) == DevFp.NONE, "没存过必须是 NONE");
        ok(DevFp.compare("", cur) == DevFp.NONE, "空串必须是 NONE（不能当 DIFF）");
        ok(DevFp.compare(cur, cur) == DevFp.SAME, "同机必须 SAME");
        ok(DevFp.compare(cur.toUpperCase(java.util.Locale.ROOT), cur) == DevFp.SAME,
                "比较必须忽略大小写");
        String other = DevFp.of("HONOR", "ALT-AN00", "HNALT", 1080, 2340);
        ok(DevFp.compare(cur, other) == DevFp.DIFF, "分辨率变了必须 DIFF");
        ok(DevFp.compare(cur, "") == DevFp.NONE, "当前拿不到 ⇒ 不得冤判成 DIFF");
    }

    // ─────────────────────────── G13 ───────────────────────────

    private static void build_parse_full() {
        String txt = "# 由 build.sh 生成\n"
                + "version=0.1-m1s1d\n"
                + "rev=0d8f527\n"
                + "dirty=0\n"
                + "version_full=0.1-m1s1d+g0d8f527\n"
                + "built=2026-10-25T19:40:00+0800\n";
        BuildInfo b = BuildInfo.parse(txt);
        ok(b.known(), "完整文件必须 known()");
        ok("0.1-m1s1d".equals(b.version), "version 解析错，实得 " + b.version);
        ok("0d8f527".equals(b.rev), "rev 解析错，实得 " + b.rev);
        ok(!b.dirty, "dirty=0 应解析成 false");
        ok("0.1-m1s1d+g0d8f527".equals(b.brief()), "brief 应优先用 version_full，实得 " + b.brief());
        ok(!b.brief().contains(" "), "brief 里不许有空格（trace 头是空格分隔的 k=v）");
    }

    private static void build_parse_partial_and_junk() {
        // 老包可能只有 version/rev（没有 version_full）：brief 要能自己拼出来
        BuildInfo b = BuildInfo.parse("version=0.1-m1s1d\nrev=abc1234\nunknown_key=x\n裸行没有等号\n");
        ok("0.1-m1s1d+gabc1234".equals(b.brief()), "缺 version_full 时应自己拼，实得 " + b.brief());
        ok(!b.dirty, "没写 dirty 就是 false");

        BuildInfo d = BuildInfo.parse("version=1.0\nrev=abc\ndirty=1\n");
        ok(d.dirty, "dirty=1 应解析成 true");
        ok("1.0+gabc+dirty".equals(d.brief()), "两个都没有 version_full 时要拼完整，实得 " + d.brief());

        // Windows 行尾（CRLF）也必须能读：build_info.txt 是构建机写的，换行不受控
        BuildInfo crlf = BuildInfo.parse("version=1.0\r\nrev=abc\r\n");
        ok("1.0+gabc".equals(crlf.brief()), "CRLF 行尾解析失败，实得 " + crlf.brief());
    }

    private static void build_unknown() {
        ok(!BuildInfo.parse(null).known(), "null ⇒ UNKNOWN");
        ok(!BuildInfo.parse("").known(), "空串 ⇒ UNKNOWN");
        ok(!BuildInfo.parse("# 只有注释\n\n").known(), "只有注释 ⇒ UNKNOWN");
        ok("unknown".equals(BuildInfo.UNKNOWN.brief()), "UNKNOWN.brief 必须是 unknown");
        ok("unknown".equals(BuildInfo.parse("# 只有注释\n").brief()), "解析不出内容时 brief 必须是 unknown（不许伪装成版本号）");

        BuildInfo viaRead = BuildInfo.read(new ByteArrayInputStream(
                "version=0.1-m1s1d\nrev=deadbee\n".getBytes(StandardCharsets.UTF_8)));
        ok("0.1-m1s1d+gdeadbee".equals(viaRead.brief()), "read(流) 应等价于 parse，实得 " + viaRead.brief());
        ok(!BuildInfo.read(null).known(), "read(null) ⇒ UNKNOWN，不许抛");
    }

    private static void ok(boolean cond, String msg) {
        if (cond) {
            pass++;
        } else {
            fail++;
            System.out.println("  ✗ " + msg);
        }
    }
}
