package io.pgauto.play;

/**
 * `LeadAudit` 的自测（纯 JVM）。
 *
 * ★ 这三条分支必须在真机上"各自可区分"，所以断言写成决策表：
 *   本轮样本 vs S 不符 ⇒ ROLLBACK（不能污染均值）；
 *   样本相符但均值不符 ⇒ RESET（均值才是坏的，例：bounded_in 那条 7926）；
 *   都对得上 ⇒ OK（绝大多数轮次走这里，不许乱动）。
 */
public final class LeadAuditSelfTest {

    private static int pass = 0, fail = 0;

    public static void main(String[] args) {
        consistent_is_ok();
        sample_off_is_rollback();
        stale_mean_is_reset();
        missing_info_is_noop();
        boundary();

        System.out.println("LeadAuditSelfTest: " + pass + " 通过, " + fail + " 失败");
        if (fail > 0) throw new AssertionError("LeadAudit 自测失败 " + fail + " 项");
    }

    private static void consistent_is_ok() {
        ok(LeadAudit.decide(7423.5, 7421.9, 7425.0, 300) == LeadAudit.OK,
                "样本与均值都和 S 一致 ⇒ OK");
        ok(LeadAudit.decide(7423.5, 7421.9, null, 300) == LeadAudit.OK,
                "还没有均值时不重置");
    }

    private static void sample_off_is_rollback() {
        // 样本比 S 大 500 ms（tap 记录时刻偏早那种）⇒ 回滚样本
        ok(LeadAudit.decide(7926.3, 7421.9, 7425.0, 300) == LeadAudit.ROLLBACK,
                "样本比 S 大 504 ms ⇒ ROLLBACK");
        ok(LeadAudit.decide(6900.0, 7421.9, 7425.0, 300) == LeadAudit.ROLLBACK,
                "样本比 S 小 522 ms ⇒ ROLLBACK");
    }

    private static void stale_mean_is_reset() {
        // 真机现场：本轮样本 7423.5 与 S 7421.9 只差 1.6 ms，但均值是 7926.3（坏首样本）
        ok(LeadAudit.decide(7423.5, 7421.9, 7926.3, 300) == LeadAudit.RESET,
                "样本对、均值错 ⇒ RESET（别把好样本丢掉）");
    }

    private static void missing_info_is_noop() {
        ok(LeadAudit.decide(Double.NaN, 7421.9, 7926.3, 300) == LeadAudit.OK,
                "没有本轮样本 ⇒ 不动（别拿 NaN 去改均值）");
        ok(LeadAudit.decide(7423.5, Double.NaN, 7926.3, 300) == LeadAudit.OK,
                "没有 S ⇒ 不动");
        ok(LeadAudit.decide(7423.5, 7421.9, Double.NaN, 300) == LeadAudit.OK,
                "均值是 NaN ⇒ 视为没有均值");
    }

    private static void boundary() {
        ok(LeadAudit.decide(7721.9, 7421.9, 7421.9, 300) == LeadAudit.OK,
                "偏差正好 300 ms 应放行（判据 >300）");
        ok(LeadAudit.decide(7722.0, 7421.9, 7421.9, 300) == LeadAudit.ROLLBACK,
                "偏差 300.1 ms 应回滚");
        ok("rollback".equals(LeadAudit.tag(LeadAudit.ROLLBACK))
                        && "reset_mean".equals(LeadAudit.tag(LeadAudit.RESET))
                        && "ok".equals(LeadAudit.tag(LeadAudit.OK)),
                "tag 必须无空格（要拼进 trace 的 k=v 行）");
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
