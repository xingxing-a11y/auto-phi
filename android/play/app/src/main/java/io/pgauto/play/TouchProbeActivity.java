package io.pgauto.play;

import android.app.Activity;
import android.content.Intent;
import android.graphics.Color;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.util.DisplayMetrics;
import android.util.Log;
import android.util.TypedValue;
import android.view.MotionEvent;
import android.view.ViewGroup;
import android.view.WindowManager;
import android.widget.FrameLayout;
import android.widget.TextView;

import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;

import io.pgauto.inject.DevRun;
import io.pgauto.inject.ShizukuProbe;
import io.pgauto.inject.sched.Densify;
import io.pgauto.inject.sched.InjectTrace;
import io.pgauto.inject.sched.RecvTrace;
import io.pgauto.inject.sched.Scheduler;

/**
 * 端内注入「收货端对账」靶子：把一份真实载荷按原样重放给**自己**，双侧落盘。
 *
 * ★ 为什么需要这个实验（10-24 第六枪之后唯一还站得住的缺口）：
 *   第六枪有 2775 帧的**发送侧**记录（InjectTrace），判定是「H1/H2/H4 否、
 *   H3 仅 5 帧、跨帧指针流 0 违规」⇒ 只能推到「发送侧清白」。
 *   但从没人证明过这 2775 帧**被任何 App 收到**——`injectInputEvent` 返回 true
 *   只代表入队成功（本项目已经在 §7 和 §101 各踩过一次同类坑）。
 *   于是「40.73% / 31.47%」到底该怪派发还是怪游戏判定，一直悬着。
 *
 * ★ 为什么不能一边让 Phigros 打、一边在这里收：
 *   触摸事件按窗口派发，一次只给最顶层的可触摸窗口。想同时拿到「游戏成绩」和
 *   「收到几条事件」是不可能的。所以这是一次**可分离实验**：
 *     - 靶子收全 ⇒ 派发层清白 ⇒ Miss 只能出自游戏判定那一侧；
 *     - 靶子也丢 ⇒ 派发层有罪 ⇒ 别再去调 G/L/坐标。
 *   两种结果都把搜索空间砍一半，所以它比再盲打一轮值。
 *
 * ★ 为什么不复用 10-21 的靶子测试（`InjectBench.multiTouch` + `PointerSinkView`）：
 *   那次是手工构造的 5 帧序列（1/3/5 指各一次），结论「多指全部收到」已记在
 *   FACTS §2472。它证明的是**通道支持多指**。本 Activity 要答的是另一个问题：
 *   **生产路径（Scheduler 坐标变换 + Gesture 状态机 + MOVE 抽稀 + 两级调度）
 *   重放真实载荷时，到了多少**。两者没有一行共用代码，通道能过 ≠ 生产路径能过。
 *
 * ★ 为什么用 {@code dispatchTouchEvent} 直接记录，而不是放一个 View 接 onTouchEvent：
 *   界面上要显示进度文字，文字层若参与触摸分发就会把 DOWN 吃掉（一旦某个 View
 *   消费了 DOWN，后续整串都给它）⇒ 那等于自己造出一批"丢帧"。
 *   在根容器覆写 dispatchTouchEvent 并直接记录，绕开整条 View 分发链，
 *   不受任何子 View 的 clickable/可聚焦性影响。
 *
 * 用法（必须先让本页面在前台，倒计时结束自动开始）：
 *
 *   adb shell am start -n io.pgauto.play/.TouchProbeActivity \
 *       --es song bounded_in [--ei maxMs 20000] [--ei countdown 3]
 *
 *   adb pull /sdcard/Android/data/io.pgauto.play/files/touch_send.csv
 *   adb pull /sdcard/Android/data/io.pgauto.play/files/touch_recv.csv
 *   python tools/compare_touch.py touch_send.csv touch_recv.csv --notes 1286
 */
public class TouchProbeActivity extends Activity {

    private static final String TAG = "PgTouch";

    private final RecvTrace recv = new RecvTrace();
    private final InjectTrace send = new InjectTrace();
    private final StringBuilder logBuf = new StringBuilder();

    private TextView tvStatus;
    private FrameLayout root;
    private final Handler h = new Handler(Looper.getMainLooper());

    private String songKey = "bounded_in";
    /** >0 ⇒ 只重放前这么多毫秒（小样本用；0 = 整首）。 */
    private long maxMs;
    private int countdownS = 3;

    private volatile boolean busy;

    @Override
    protected void onCreate(Bundle b) {
        super.onCreate(b);
        readIntent();
        setContentView(buildUi());
        enterFullscreen();
        log("就绪：曲目 " + songKey + (maxMs > 0 ? "，只重放前 " + maxMs + " ms" : "，整首")
                + "；倒计时 " + countdownS + " s（务必保持本页在前台）");
        tick(countdownS);
    }

    private void readIntent() {
        Intent it = getIntent();
        if (it == null) return;
        String s = it.getStringExtra("song");
        if (s != null && !s.isEmpty()) songKey = s;
        long m = it.getIntExtra("maxMs", 0);
        if (m > 0) maxMs = m;
        int c = it.getIntExtra("countdown", 3);
        if (c >= 0 && c <= 60) countdownS = c;
    }

    // ─────────────────────────── 界面 ───────────────────────────

    private FrameLayout buildUi() {
        root = new FrameLayout(this) {
            /**
             * ★ 所有事件在这里直接记录，绕开整条 View 分发链。
             *
             * 为什么不在子 View 上 onTouchEvent：文字层会先收到事件，一旦它消费
             * DOWN，后面整串都给文字层，靶子一个都收不到 —— 症状与「多指被丢弃」
             * 一模一样，会得出完全相反的结论（10-21 已经踩过同形态的坑：
             * 靶子没布局 ⇒ 收到 0 条 ⇒ 误判成多指不支持）。
             */
            @Override
            public boolean dispatchTouchEvent(MotionEvent ev) {
                final int n = ev.getPointerCount();
                final int[] ids = new int[n];
                final float[] xs = new float[n];
                final float[] ys = new float[n];
                // ★ 必须立刻拷值：MotionEvent 被系统池化复用，本方法返回后
                //   getX/getPointerId 拿到的可能就是别人的了。
                for (int i = 0; i < n; i++) {
                    ids[i] = ev.getPointerId(i);
                    xs[i] = ev.getX(i);
                    ys[i] = ev.getY(i);
                }
                // ★ 用 getAction()（带 actionIndex 编码），不是 getActionMasked()：
                //   对账要能看出 POINTER_UP 抬的是第几根手指。
                recv.record(android.os.SystemClock.uptimeMillis(), ev.getEventTime(),
                        ev.getAction(), n, ids, xs, ys);
                return true;
            }
        };
        root.setBackgroundColor(Color.BLACK);

        tvStatus = new TextView(this);
        tvStatus.setTextColor(Color.parseColor("#B0FFB0"));
        tvStatus.setTextSize(TypedValue.COMPLEX_UNIT_SP, 13);
        tvStatus.setTypeface(android.graphics.Typeface.MONOSPACE);
        tvStatus.setPadding(24, 24, 24, 24);
        // 不参与触摸（虽然 dispatch 已被覆写，这里仍显式声明意图）
        tvStatus.setClickable(false);
        tvStatus.setFocusable(false);
        tvStatus.setTextIsSelectable(false);
        root.addView(tvStatus, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
        return root;
    }

    private void enterFullscreen() {
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        getWindow().getDecorView().setSystemUiVisibility(
                android.view.View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY
                        | android.view.View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
                        | android.view.View.SYSTEM_UI_FLAG_FULLSCREEN
                        | android.view.View.SYSTEM_UI_FLAG_LAYOUT_STABLE
                        | android.view.View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION
                        | android.view.View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN);
    }

    private void tick(int left) {
        if (left <= 0) {
            start();
            return;
        }
        tvStatus.setText(tail(logText(), 30) + "\n\n== " + left + " s 后开始（保持前台）==");
        h.postDelayed(() -> tick(left - 1), 1000);
    }

    private void start() {
        if (busy) return;
        busy = true;
        new Thread(this::runOnce, "pg-touch").start();
    }

    // ─────────────────────────── 主流程 ───────────────────────────

    private void runOnce() {
        try {
            // ① 尺寸核对。
            //
            // ★ 这里**不能**要求「窗口 == 屏幕」。实测（HONOR ALT-AN00，横屏）窗口是
            //   1536x720，屏幕 1610x720 —— 差的 74 px 是挖孔/系统栏占用的那条边。
            //   如果强行按屏幕尺寸生成坐标，那 74 px 里的事件会落在窗口外、被系统吃掉，
            //   然后被我们算成「派发丢帧」⇒ 得出与事实相反的结论。
            //   正确做法：坐标按**窗口**尺寸生成，再把窗口在屏幕上的偏移加到每个点上。
            //   于是所有事件都落在窗口内，而"屏幕坐标 → 窗口坐标"只差一个常量平移
            //   （tools/compare_touch.py 会把恒定平移识别为良性，不误报坐标错位）。
            int[] real = realSize();
            int w = root.getWidth(), hh = root.getHeight();
            int[] loc = new int[2];
            root.getLocationOnScreen(loc);
            log("窗口 " + w + "x" + hh + " @(" + loc[0] + "," + loc[1] + ")"
                    + " / 屏幕 " + real[0] + "x" + real[1]);
            if (w <= 0 || hh <= 0) {
                throw new IllegalStateException("窗口未布局（宽高 0）—— 倒计时太短或页面没到前台");
            }
            if (w != real[0] || hh != real[1]) {
                log("⚠ 窗口比屏幕小 " + (real[0] - w) + "x" + (real[1] - hh)
                        + "（挖孔/系统栏）⇒ 按窗口尺寸生成坐标并加偏移；"
                        + "帧级对账不受影响，坐标与真实打歌（" + real[0] + "x" + real[1] + "）差一个平移");
            }

            // ② Shizuku 门禁（与 PlayService 同一条路径）
            ShizukuProbe.Report r = ShizukuProbe.runAll();
            if (!r.allOk()) {
                for (ShizukuProbe.Step s : r.steps) {
                    if (s.gate && !s.ok) log("✗ " + s);
                }
                throw new IllegalStateException("Shizuku 门禁未过");
            }
            log("Shizuku 门禁通过");

            // ③ 载荷 → 指令
            SongLib.Song song = findSong(songKey);
            File payload = cachePayload(song);
            Densify.Stats ds = new Densify.Stats();
            long t0 = System.nanoTime();
            Scheduler.Meta meta = new Scheduler.Meta();
            List<Scheduler.Cmd> cmds = Scheduler.build(payload.toPath(), w, hh, true, true, ds, meta);
            double buildMs = (System.nanoTime() - t0) / 1e6;
            if (cmds.isEmpty()) throw new IllegalStateException("载荷解析出 0 条指令");
            log("载荷口径 " + meta);
            log("排程 " + cmds.size() + " 条（曲 = " + song.name + " " + song.diff
                    + "，音符 " + song.noteCount + "，解析 " + String.format(Locale.ROOT, "%.0f", buildMs) + " ms）");

            if (maxMs > 0) {
                List<Scheduler.Cmd> cut = new ArrayList<>();
                for (Scheduler.Cmd c : cmds) {
                    if (c.tMs() <= maxMs) cut.add(c);
                }
                log("只重放前 " + maxMs + " ms ⇒ 保留 " + cut.size() + " / " + cmds.size() + " 条");
                cmds = cut;
            }
            // 窗口坐标 → 屏幕坐标：注入用的是屏幕绝对坐标，而上面的坐标是按窗口尺寸
            // 算出来的。两者只差窗口原点（多数机型是 0，挖孔机型可能不是）。
            if (loc[0] != 0 || loc[1] != 0) {
                List<Scheduler.Cmd> shifted = new ArrayList<>(cmds.size());
                for (Scheduler.Cmd c : cmds) {
                    shifted.add(new Scheduler.Cmd(c.seq(), c.srcIdx(), c.tMs(), c.action(),
                            c.pid(), c.devX() + loc[0], c.devY() + loc[1], c.key()));
                }
                cmds = shifted;
                log("坐标已平移 +(" + loc[0] + "," + loc[1] + ") 对齐屏幕坐标系");
            }
            int nKey = 0;
            for (Scheduler.Cmd c : cmds) if (c.key()) nKey++;
            log("关键事件 " + nKey + " 条，MOVE " + (cmds.size() - nKey) + " 条");

            // ④ 注入（与 PlayService 完全同一套参数：dueTime + dropMoves）
            DevRun brain = new DevRun(r.inputManagerProxy, r.injectMethod, true, true,
                    Math.max(3, cmds.size() / 200), this::log);
            brain.setTrace(send);
            double zero = System.currentTimeMillis() + 1500;
            log("开始注入（zero = now+1.5s）");
            brain.run(cmds, zero);
            log("注入结束：" + send.brief());
            log("注入结束，收货快照：" + recv.brief());

        } catch (Throwable t) {
            log("!! 中止：" + ShizukuProbe.brief(t));
        } finally {
            // ★ 注入是 ASYNC：「脑收尾」返回 ≠ 事件已派发完。整首实测就是卡在这里——
            //   CSV 里缺的恰好是**最后一条 UP**（t=149273 ms），看起来像「丢了 1 条关键帧」，
            //   实际是落盘比队列快。300 ms 是实测分发延迟 max（33 ms）的 9 倍，足够清空。
            //   ⛔ 别用「反正只差一条」搪塞：对账的结论是"100% 还是 99.94%"，只差这一条。
            try {
                Thread.sleep(300);
            } catch (InterruptedException ignore) {
                // 被中断也要落盘：已经收到的数据比"完整收尾"更重要
            }
            dump();
            // ★ 落盘之后再汇报一次：界面与日志要反映**文件里真正有多少**。
            log("落盘后最终：" + recv.brief());
            rewriteLog();
            busy = false;
        }
    }

    // ─────────────────────────── 落盘 ───────────────────────────

    private void dump() {
        File dir = getExternalFilesDir(null);
        if (dir == null) dir = getFilesDir();
        if (!dir.isDirectory() && !dir.mkdirs()) {
            log("!! 建不了目录 " + dir);
            return;
        }
        writeAndVerify(new File(dir, "touch_send.csv"), send.csv(), "发送侧");
        writeAndVerify(new File(dir, "touch_recv.csv"), recv.csv(), "收货侧");
        writeAndVerify(new File(dir, "touch_probe.log"), logText(), "日志");
        log("落盘完成：" + dir.getAbsolutePath());
    }

    /** 写完立刻回读长度 —— 空文件/半截文件拉回来才发现，等于白跑一轮真机。 */
    private void writeAndVerify(File f, String text, String what) {
        try {
            byte[] raw = text.getBytes(java.nio.charset.StandardCharsets.UTF_8);
            try (FileOutputStream fo = new FileOutputStream(f)) {
                fo.write(raw);
            }
            long len = f.length();
            if (len != raw.length) {
                log("!! " + what + "落盘长度不符：" + len + " vs " + raw.length);
            } else {
                log("✓ " + what + " " + f.getName() + " " + len + " B");
            }
        } catch (Throwable t) {
            log("!! " + what + "落盘失败：" + ShizukuProbe.brief(t));
        }
    }

    /** 只重写日志文件（把 dump 之后才产生的那几行补进去，保持界面与文件一致）。 */
    private void rewriteLog() {
        File dir = getExternalFilesDir(null);
        if (dir == null) dir = getFilesDir();
        File f = new File(dir, "touch_probe.log");
        try (FileOutputStream fo = new FileOutputStream(f)) {
            fo.write(logText().getBytes(java.nio.charset.StandardCharsets.UTF_8));
        } catch (Throwable t) {
            Log.w(TAG, "重写日志失败: " + ShizukuProbe.brief(t));
        }
    }

    // ─────────────────────────── 工具 ───────────────────────────

    private SongLib.Song findSong(String key) throws Exception {
        for (SongLib.Song s : SongLib.load(this)) {
            if (key.equals(s.key)) return s;
        }
        throw new IllegalArgumentException("manifest 里没有曲目 " + key);
    }

    /**
     * 载荷缓存。★ 判据与落盘全部交给 {@link PayloadCache}（唯一实现）。
     *
     * 这里原本是 `PlayService.cachePayload` 的**逐字副本**（`f.length() != raw.length`）——
     * 2026-10-24 的坐标事故正是从这条判据来的：`canvas` 换了口径但字节数没变 ⇒ 旧缓存
     * 永远命中。两处实现只改一处就等于没修（10-24 的对账轮走的正是本类）。
     */
    private File cachePayload(SongLib.Song s) throws Exception {
        File dir = new File(getFilesDir(), "payload");
        try (InputStream in = getAssets().open(s.payload)) {
            return PayloadCache.ensure(dir, s.key, s.payload, in, this::log);
        }
    }

    @SuppressWarnings("deprecation")
    private int[] realSize() {
        DisplayMetrics dm = new DisplayMetrics();
        getWindowManager().getDefaultDisplay().getRealMetrics(dm);
        return new int[]{dm.widthPixels, dm.heightPixels};
    }

    private void log(String s) {
        Log.i(TAG, s);
        String text;
        synchronized (logBuf) {
            logBuf.append(s).append('\n');
            text = logBuf.toString();
        }
        final String show = tail(text, 30);
        h.post(() -> {
            if (tvStatus != null) tvStatus.setText(show);
        });
    }

    private String logText() {
        synchronized (logBuf) {
            return logBuf.toString();
        }
    }

    /** 只保留最后 n 行（界面显示用；落盘仍是全文）。 */
    private static String tail(String s, int n) {
        String[] parts = s.split("\n");
        int from = Math.max(0, parts.length - n);
        StringBuilder sb = new StringBuilder();
        for (int i = from; i < parts.length; i++) sb.append(parts[i]).append('\n');
        return sb.toString();
    }

    @Override
    protected void onDestroy() {
        h.removeCallbacksAndMessages(null);
        super.onDestroy();
    }
}
