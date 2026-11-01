package io.pgauto.inject.sched;

import java.util.Locale;

/**
 * 帧级注入追踪：把「这一帧发了什么」与「系统怎么答」逐帧落到一张 CSV。
 *
 * ★ 为什么必须有它（10-24 S1-d 端内首跑 40.73%、701 Miss）：
 *   当时手上只有汇总数——发 2769 条、false 0、关键事件 lag p50 2.3 ms。这三个数
 *   **全部为真，却一个字也说明不了"多指帧有没有被派发"**：它们只证明事件进了队列。
 *   而载荷里多指占 47.09%、Miss 占 54.5%，吻合到不像巧合，正因为缺逐帧数据而定不了案。
 *   ⇒ 汇总指标能证伪「完全没发」，定位必须靠逐帧。这是在补"量错了对象"的课。
 *
 * ★ 三个设计约束（每条都对应一次踩坑）：
 *   1. **不碰实时路径**：只 append 到内存 StringBuilder，落盘由调用方在注入结束后做。
 *      边注入边写文件会把注入 p99 从 7 ms 抬到几十 ms —— 用测量工具毁掉被测对象，
 *      而这轮的被测对象恰恰就是注入时延。
 *   2. **缺值写 `-`，绝不写 0**：`et_back_ms` 在第一帧没有"上一帧"可言。写 0 会被下游
 *      读成"零倒退"，是静默造数据的经典写法（MEMORY 教训 11）。
 *   3. **容量有上限且丢弃要计数**：超限后只丢**明细行**，统计仍覆盖全部帧；丢弃条数写进
 *      摘要与 CSV 尾部。否则"少了几百行"会被误读成"那段时间没发事件"。
 *
 * ★ 列的用途（下游 {@code tools/analyze_inject_trace.py} 按这个契约读）：
 *   seq / t_ms     帧序号与该帧计划时刻（都能直接对上载荷里的 t_ms）
 *   act / idx      masked action 与 actionIndex（-1 = 非 POINTER_*）
 *   n / ptrs       指针数与指针集 `id:x:y|id:x:y`（用 `|` `:` 分隔，不含逗号，CSV 安全）
 *   ret            注入调用返回值（1/0）。⛔ true 只代表**入队成功**，不代表被派发
 *   cost_us        注入调用本身耗时（微秒）
 *   late_ms        进入注入时距计划时刻的落后（ms，正 = 迟到）
 *   et_late_ms     eventTime 落后于当前 uptimeMillis（ms，≥0；越大越像 stale 事件）
 *   et_back_ms     eventTime 相对上一帧的**倒退量**（正 = 倒退，非法；首帧为 `-`）
 */
public final class InjectTrace {

    /** 列契约。改这里必须同步改 `tools/analyze_inject_trace.py` 的 COLS。 */
    public static final String HEADER =
            "seq,t_ms,act,idx,n,ptrs,ret,cost_us,late_ms,et_late_ms,et_back_ms";

    /** 明细行上限。长曲 1286 音约 2800 帧，20000 行留足余量（≈2 MB 内存）。 */
    public static final int CAP = 20000;

    /**
     * 本轮注入模式的落盘标记，形如 `# inject_mode=wait(1)`；未设置则不写。
     *
     * ★ 为什么必须落进产物文件（FACTS §167）：E2 那一枪想验证「同步注入是否生效」，
     *   判据只能靠 intent extra + logcat 两者事后取证 —— 而 logcat 主缓冲会被游戏日志
     *   冲掉、intent 打完就取不到，结果一整枪无法归因。**开关的实际取值必须跟着数据走**，
     *   否则没有任何独立证据能证明这枪到底跑的是哪个模式。
     * ★ 以 `#` 开头放在表头**之前**是安全的：下游 `tools/analyze_inject_trace.py:93`
     *   已跳过 `#` 行再去找 COLS 表头。⛔ 别改成不带 `#` 的裸行，那会被当成表头吃掉。
     */
    private String injectModeNote;

    public void setInjectMode(int mode, String name) {
        injectModeNote = "# inject_mode=" + name + "(" + mode + ")\n";
    }

    /**
     * 本轮的**运行参数**落盘标记，形如 `# run song=bounded_in l_dev=0.180 …`；未设置则不写。
     *
     * ★ 为什么要它（2026-10-25 的取证缺口）：bounded_in 那枪改的正是 `L_dev`，但打完之后
     *   ① `RunLog.tail(16)` 只显示最后 16 行（prepare 那行早被顶掉）、② logcat 被游戏日志
     *   冲干净、③ trace header 只有 `inject_mode` ⇒ **无法直证这枪跑的是哪个 `L_dev`**，
     *   只能靠「进程是那次 am start 起的」去推断。同一课 §167 已经上过一次（开关没落进产物）。
     *   ⇒ 凡是「这一枪用什么参数跑的」都必须跟着数据走：模式、L_dev、sil_thr、零点、判据。
     * ★ 同样以 `#` 开头放在表头之前；下游已跳过 `#` 行。值里不许有换行（构造时替换掉）。
     */
    private String runNote;

    /** 注入结束后追加的收尾注释行（见 {@link #addTailNote}）。 */
    private final StringBuilder tailNotes = new StringBuilder();

    /**
     * 记本轮的运行参数（调用方拼好 `k=v k=v`）。空串/全空白 ⇒ 不写这一行。
     *
     * ★ 只允许**一行**：换行替换成空格，否则一条参数能把 CSV 表头挤到第二行以外。
     */
    public void setRunMeta(String meta) {
        if (meta == null) {
            runNote = null;
            return;
        }
        String one = meta.replace('\r', ' ').replace('\n', ' ').trim();
        runNote = one.isEmpty() ? null : "# run " + one + "\n";
    }

    /**
     * 本轮的**收尾参数**（注入结束后才算得出来的量），形如 `# lead lead_meas=…` / `# calib k_meas=…`。
     *
     * ★ 为什么不能塞进 {@link #setRunMeta}：头部那一行是在**注入开始前**拼的，
     *   而 K 的测量值要等 30 s 捕获采完 + 互相关算完才存在（`finishAutoCalib` 在注入之后跑）。
     *   把后来的值塞进头部 = 伪造证据；正确做法是**如实标注时间**：头部写"用的"，尾部写"测到的"。
     * ★ 允许追加多行（lead 与 calib 是两条独立的收尾信息）；每行仍是单行、仍以 `#` 开头 ⇒
     *   下游（`tools/analyze_inject_trace.py`、`tmp/*.py`）一律先滤 `#` 行，位置安全。
     */
    public void addTailNote(String meta) {
        if (meta == null) return;
        String one = meta.replace('\r', ' ').replace('\n', ' ').trim();
        if (one.isEmpty()) return;
        tailNotes.append("# ").append(one).append('\n');
    }

    private final StringBuilder sb = new StringBuilder(1 << 18);
    private final StringBuilder ptr = new StringBuilder(64);

    private int n;                 // 总帧数（含被截断的明细）
    private int nTruncated;        // 明细被丢弃的行数
    private long prevEt = Long.MIN_VALUE;

    // 单指 / 多指分账统计（多指 = 该帧携带 ≥2 个指针）
    private int nSingle, nMulti;
    private int nSingleFalse, nMultiFalse;
    private int nEtBack, nEtBackMulti;
    private int nMultiLate20;
    private long maxCostSingleUs, maxCostMultiUs;

    /**
     * 记一帧。
     *
     * @param ret     注入调用返回值
     * @param costUs  注入调用耗时（微秒）
     * @param lateMs  进入注入时距计划时刻的落后（ms）
     * @param et      本帧 MotionEvent.eventTime（uptimeMillis 基准）
     * @param nowMs   构造该事件时的 SystemClock.uptimeMillis()
     */
    public void record(Gesture.Frame f, boolean ret, long costUs, double lateMs,
                       long et, long nowMs) {
        n++;
        final int act = Gesture.masked(f.action());
        final boolean ptrAct = act == Gesture.ACTION_POINTER_DOWN
                || act == Gesture.ACTION_POINTER_UP;
        final int idx = ptrAct ? Gesture.actionIndex(f.action()) : -1;
        final int cnt = f.count();
        final boolean multi = cnt >= 2;

        final long etLate = nowMs - et;
        final long etBack = (prevEt == Long.MIN_VALUE) ? Long.MIN_VALUE : prevEt - et;
        prevEt = et;

        // ★ 统计必须在截断判断**之前**做完：明细可以丢，结论不能缺样本。
        if (multi) {
            nMulti++;
            if (!ret) nMultiFalse++;
            if (etBack > 0) nEtBackMulti++;
            if (lateMs > 20.0) nMultiLate20++;
            if (costUs > maxCostMultiUs) maxCostMultiUs = costUs;
        } else {
            nSingle++;
            if (!ret) nSingleFalse++;
            if (costUs > maxCostSingleUs) maxCostSingleUs = costUs;
        }
        if (etBack > 0) nEtBack++;

        if (n > CAP) { nTruncated++; return; }

        sb.append(n).append(',')
                .append(fmt3(f.tMs())).append(',')
                .append(act).append(',')
                .append(idx).append(',')
                .append(cnt).append(',');
        ptr.setLength(0);
        for (int i = 0; i < cnt; i++) {
            if (i > 0) ptr.append('|');
            ptr.append(f.ids()[i]).append(':').append(f.xs()[i]).append(':').append(f.ys()[i]);
        }
        sb.append(ptr).append(',')
                .append(ret ? 1 : 0).append(',')
                .append(costUs).append(',')
                .append(fmt3(lateMs)).append(',')
                .append(etLate).append(',')
                .append(etBack == Long.MIN_VALUE ? "-" : Long.toString(etBack))
                .append('\n');
    }

    private static String fmt3(double v) {
        return String.format(Locale.ROOT, "%.3f", v);
    }

    /** 完整 CSV（表头 + 明细 + 截断说明）。设备上落盘的就是它。 */
    public String csv() {
        StringBuilder out = new StringBuilder(sb.length() + 200);
        if (injectModeNote != null) out.append(injectModeNote);
        if (runNote != null) out.append(runNote);
        out.append(HEADER).append('\n').append(sb);
        // ★ 收尾注释行放在明细之后、截断说明之前：两者都以 `#` 开头，下游一律跳过。
        //   ⛔ 别挪到表头之前 —— 那样会伪造"参数在注入前就已知"的时序。
        if (tailNotes.length() > 0) out.append(tailNotes);
        if (nTruncated > 0) {
            out.append("# truncated ").append(nTruncated)
                    .append(" lines (cap ").append(CAP).append(")\n");
        }
        return out.toString();
    }

    /** 一行摘要，直接进 RunLog（不拉文件也能在界面上看到关键结论）。 */
    public String brief() {
        StringBuilder b = new StringBuilder(176);
        // 摘要自带模式口径：没有它，界面上一行「帧 2804 / false 0 / 峰值 18.7 ms」
        // 在 async 与 wait 两种模式下长得一模一样，靠人眼分不出这枪跑的是哪个。
        if (injectModeNote != null) {
            b.append(injectModeNote.trim().substring(2)).append("；");
        }
        b.append("帧 ").append(n)
                .append("（单指 ").append(nSingle).append(" / 多指 ").append(nMulti).append("）");
        b.append("；false 单指 ").append(nSingleFalse).append(" 多指 ").append(nMultiFalse);
        b.append("；eventTime 倒退 ").append(nEtBack)
                .append("（多指 ").append(nEtBackMulti).append("）");
        b.append("；多指 late>20ms ").append(nMultiLate20);
        b.append("；注入耗时峰值 单指 ").append(maxCostSingleUs / 1000.0)
                .append(" / 多指 ").append(maxCostMultiUs / 1000.0).append(" ms");
        if (nTruncated > 0) b.append("；明细截断 ").append(nTruncated).append(" 行");
        return b.toString();
    }

    public int lines() { return n; }
    public int written() { return n - nTruncated; }
    public int truncated() { return nTruncated; }
    public int nMulti() { return nMulti; }
    public int nMultiFalse() { return nMultiFalse; }
    public int nSingleFalse() { return nSingleFalse; }
    public int nEtBack() { return nEtBack; }
}
