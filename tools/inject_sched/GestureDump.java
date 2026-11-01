package io.pgauto.inject.sched;

import java.nio.file.Path;
import java.util.List;
import java.util.Locale;

/**
 * PC 侧对拍工具：把「载荷 → 指令 → 帧」整条链的产物打成 TSV，与
 * {@code tools/inject_sched/gesture_ref.py} 的独立实现逐行 diff。
 *
 * 用法（cwd = 仓库根）：
 *   javac -encoding UTF-8 -d tmp/_s1_verify/classes \
 *       android/injector/app/src/main/java/io/pgauto/inject/sched/Scheduler.java \
 *       android/injector/app/src/main/java/io/pgauto/inject/sched/Gesture.java \
 *       tools/inject_sched/GestureDump.java
 *   java -cp tmp/_s1_verify/classes io.pgauto.inject.sched.GestureDump out/touches_demiurge.json \
 *       > java.tsv
 *   diff <(python tools/inject_sched/gesture_ref.py out/touches_demiurge.json) java.tsv
 */
public final class GestureDump {

    public static void main(String[] args) throws Exception {
        if (args.length < 1) {
            System.err.println("用法: GestureDump <touches.json> [dstW=1610] [dstH=720] [fit=height]");
            System.exit(2);
        }
        double w = args.length > 1 ? Double.parseDouble(args[1]) : 1610;
        double h = args.length > 2 ? Double.parseDouble(args[2]) : 720;
        boolean fitHeight = args.length <= 3 || "height".equals(args[3]);

        List<Scheduler.Cmd> cmds = Scheduler.build(Path.of(args[0]), w, h, fitHeight);
        Gesture g = new Gesture();
        StringBuilder sb = new StringBuilder(cmds.size() * 48);
        sb.append("seq\tt_ms\taction\tcount\tids\txs\tys\n");
        int seq = 0;
        for (Scheduler.Cmd c : cmds) {
            Gesture.Frame f = g.step(c);
            if (f == null) continue;
            sb.append(seq++).append('\t')
              .append(String.format(Locale.ROOT, "%.6f", f.tMs())).append('\t')
              .append(f.action()).append('\t')
              .append(f.count()).append('\t')
              .append(join(f.ids())).append('\t')
              .append(join(f.xs())).append('\t')
              .append(join(f.ys())).append('\n');
        }
        // 结尾状态必须为空（真机会卡指针）；非空就把剩余帧 dump 出来，让 diff 直接暴露
        if (!g.isEmpty()) {
            System.err.println("⚠ 结尾仍有 " + g.activeCount() + " 个指针未抬起");
            for (Gesture.Frame f : g.releaseAll(0)) {
                sb.append(seq++).append('\t').append("0.000000").append('\t')
                  .append(f.action()).append('\t').append(f.count()).append('\t')
                  .append(join(f.ids())).append('\t').append(join(f.xs())).append('\t')
                  .append(join(f.ys())).append('\n');
            }
        }
        System.out.print(sb);
        System.err.println("帧数 " + seq + "，孤儿 MOVE " + g.orphanMove
                + " / UP " + g.orphanUp + "，重复 DOWN " + g.dupDown);
    }

    private static String join(int[] a) {
        StringBuilder sb = new StringBuilder(a.length * 5);
        for (int i = 0; i < a.length; i++) {
            if (i > 0) sb.append(',');
            sb.append(a[i]);
        }
        return sb.toString();
    }
}
