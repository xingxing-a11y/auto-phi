package io.pgauto.inject;

import android.app.Activity;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.Context;
import android.content.Intent;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.util.Log;
import android.util.TypedValue;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.widget.Button;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;
import android.widget.Toast;

import java.io.File;
import java.io.RandomAccessFile;
import java.nio.charset.StandardCharsets;

import rikka.shizuku.Shizuku;

/**
 * S0 门禁界面。三个按钮，别的都不做：
 *   ① 申请权限 ② 重新检测 ③ 复制报告
 *
 * ★ 本界面**不注入任何事件**。它只回答「能不能注入」这个前提问题。
 *   真正的注入在 S3，且必须由显式操作触发——这个项目已经踩过「脚本一跑就在设备上点开始」的坑。
 *
 * ★ 不用 AndroidX（省依赖、省下载），所以走已废弃但可用的 Activity + 手写 View，
 *   与 android/probe 保持一致。
 */
public class MainActivity extends Activity {

    private static final int REQ_SHIZUKU = 201;

    private TextView tvOut;
    private Button btnPerm, btnRun, btnCopy, btnInject, btnCost, btnMulti, btnArm, btnStop;
    /** 端内服务的进度（服务写 status.txt，这里每秒读一次尾部）。 */
    private TextView tvSvc;
    private File svcDir;
    private final Handler h = new Handler(Looper.getMainLooper());
    private String lastReport = "";
    /** 最近一次门禁报告，注入自检要用里面的 proxy 与 injectInputEvent。 */
    private ShizukuProbe.Report last;
    /** runOnce 的调用次数。用于证明「注入的事件真的被本 App 消费了」（端到端，而非只看返回值）。 */
    private int runCount = 0;
    /** 多指测试用的靶子：铺在最上层，只在测试期间可见（可见时会挡住按钮，这是有意的）。 */
    private PointerSinkView sink;

    private final Shizuku.OnRequestPermissionResultListener permListener =
            (requestCode, grantResult) -> {
                if (requestCode != REQ_SHIZUKU) return;
                boolean ok = grantResult == android.content.pm.PackageManager.PERMISSION_GRANTED;
                toast(ok ? "已授权，正在重新检测" : "授权被拒绝（code=" + grantResult + "）");
                h.post(this::runOnce);
            };

    private final Shizuku.OnBinderReceivedListener binderListener = () -> h.post(this::runOnce);
    private final Shizuku.OnBinderDeadListener deadListener = () -> h.post(() -> render("Shizuku 服务已断开（重新拉起后再检测）\n" + lastReport));

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(buildUi());

        // sticky 版本：如果 binder 早于本 Activity 就绪，注册时会立刻回调一次。
        // 不用 sticky 的话，冷启动会一直显示「未连接」，直到服务重启——这个坑很隐蔽。
        Shizuku.addBinderReceivedListenerSticky(binderListener);
        Shizuku.addBinderDeadListener(deadListener);
        Shizuku.addRequestPermissionResultListener(permListener);
        h.post(this::runOnce);
        svcDir = getExternalFilesDir(null) != null ? getExternalFilesDir(null) : getFilesDir();
        h.postDelayed(svcPoller, 500);
    }

    @Override
    protected void onDestroy() {
        Shizuku.removeBinderReceivedListener(binderListener);
        Shizuku.removeBinderDeadListener(deadListener);
        Shizuku.removeRequestPermissionResultListener(permListener);
        h.removeCallbacksAndMessages(null);
        super.onDestroy();
    }

    private void runOnce() {
        runCount++;
        String text;
        try {
            ShizukuProbe.Report r = ShizukuProbe.runAll();
            last = r;
            text = r.toText();
            btnPerm.setEnabled(!r.allOk());
            btnInject.setEnabled(r.allOk());
            btnCost.setEnabled(r.allOk());
            btnMulti.setEnabled(r.allOk());
            btnArm.setEnabled(r.allOk());
        } catch (Throwable t) {
            // 兜底：runAll 内部每步都 try 了，走到这里说明是意料外的错误（比如类加载失败）。
            text = "检测整体失败（不是单步失败）: " + ShizukuProbe.brief(t);
        }
        lastReport = text;
        render(text);
    }

    /**
     * 注入自检：**真的**点一下「② 重新检测」按钮，然后用 runCount 是否递增来证明
     * 事件被 App 消费了。
     *
     * 为什么不只看 injectInputEvent 的返回值：它返回 true 只代表「事件成功入队」，
     * 不代表有任何一个 App 收到。本项目踩过「看起来成功、数据全废」的坑
     * （10-19 音量 0 时录音全 0 但播放不报错），所以这里要端到端证据。
     */
    private void doInjectSelfTest() {
        if (last == null || last.inputManagerProxy == null || last.injectMethod == null) {
            toast("门禁还没过：需要 getSystemService(input) 与接口方法都拿到");
            return;
        }
        int[] loc = new int[2];
        btnRun.getLocationOnScreen(loc);
        float cx = loc[0] + btnRun.getWidth() / 2f;
        float cy = loc[1] + btnRun.getHeight() / 2f;

        int before = runCount;
        String head = "=== 注入自检：目标=「② 重新检测」按钮中心 (" + cx + "," + cy + ") ===";
        Log.i(ShizukuProbe.TAG, head);

        String res = Injector.tap(last.inputManagerProxy, last.injectMethod, cx, cy);
        Log.i(ShizukuProbe.TAG, res);
        toast(res);

        // 事件是异步投递的，给它 900 ms 走到输入分发器再进 App。
        h.postDelayed(() -> {
            int after = runCount;
            String verdict = after > before
                    ? "  ⇒ 事件被 App 真实消费（runCount " + before + "→" + after + "，端到端成功）"
                    : "  ⇒ 未被消费（runCount 停在 " + before + "；注入入队了但没人收到）";
            Log.i(ShizukuProbe.TAG, "=== 注入自检结论:" + verdict + " ===");
            Toast.makeText(this, verdict.trim(), Toast.LENGTH_LONG).show();
        }, 900);
    }

    /**
     * 注入成本：经 ShizukuBinderWrapper 的**每次调用**耗时分布。
     *
     * ★ 这个数不能拿 10-20 的直连数据代替：那时是 app_process 直连 system_server，
     *   现在多了一跳（App → shizuku_server → system_server），每次是两次同步 binder 往返。
     *   S1 的两级调度预算全押在这个数上。
     * ★ 放后台线程：200 次 × 2 次往返 ≈ 数百毫秒，在主线程会卡出 ANR 风险。
     */
    private void doCostTest() {
        if (last == null || last.inputManagerProxy == null || last.injectMethod == null) {
            toast("门禁还没过");
            return;
        }
        final Object proxy = last.inputManagerProxy;
        final java.lang.reflect.Method m = last.injectMethod;
        btnCost.setEnabled(false);
        Log.i(ShizukuProbe.TAG, "=== 成本测试开始：预热 20 + 统计 200 次 tap ===");
        new Thread(() -> {
            String res;
            try {
                res = InjectBench.tapCost(proxy, m, 360f, 1200f, 20, 200);
            } catch (Throwable t) {
                res = "成本测试失败: " + ShizukuProbe.brief(t);
            }
            final String out = res;
            h.post(() -> {
                Log.i(ShizukuProbe.TAG, "=== 成本结论: " + out + " ===");
                toast(out);
                btnCost.setEnabled(true);
            });
        }, "pg-cost").start();
    }

    /**
     * 多指注入：发一套完整的 3 指序列，看靶子报出「最大指针数」。
     *
     * ★ 判定必须用「靶子收到几个指针」，不能用 injectInputEvent 的返回值：
     *   返回 true 只是入队成功。多指构造错（比如用了单指针的 obtain 重载）会得到
     *   pointerCount=1 的事件，**不报错**，只是静默降级成单指 —— 那在 Phigros 里
     *   就是「按了但只算一个键」。
     *
     * ★★ 必须先跑**单指对照**：靶子从 GONE 转 VISIBLE 后如果还没布局（宽高 0），
     *   事件落不到它身上，会得到「0 条事件」——这跟「多指被 InputDispatcher 丢弃」
     *   症状完全一样。不先做对照就会把布局问题误判成多指不支持。
     */
    private void doMultiTouchTest() {
        if (last == null || last.inputManagerProxy == null || last.injectMethod == null) {
            toast("门禁还没过");
            return;
        }
        sink.reset();
        sink.setVisibility(View.VISIBLE);
        Log.i(ShizukuProbe.TAG, "=== 多指测试：依次 1 / 3 / 5 指（1 指兼作对照）===");
        // 等一帧以上的布局：setVisibility 只是标记，宽高要等 measure/layout 走完
        h.postDelayed(() -> {
            Log.i(ShizukuProbe.TAG, "靶子尺寸 " + sink.getWidth() + "x" + sink.getHeight()
                    + "（为 0 说明还没布局，本次测试无效）");
            if (sink.getWidth() == 0) {
                toast("靶子未布局，测试作废");
                sink.setVisibility(View.GONE);
                return;
            }
            runPointerCount(new int[]{1, 3, 5}, 0);
        }, 300);
    }

    /** 依次对每种手指数跑一次，全部结束后隐藏靶子。 */
    private void runPointerCount(int[] counts, int idx) {
        if (idx >= counts.length) {
            Log.i(ShizukuProbe.TAG, "=== 多指测试结束 ===");
            toast("多指测试结束，结果见日志");
            sink.setVisibility(View.GONE);
            return;
        }
        final int k = counts[idx];
        sink.reset();
        int w = sink.getWidth();
        int hh = sink.getHeight();
        float y = hh * 0.75f;
        float[][] pts = new float[k][2];
        for (int i = 0; i < k; i++) {
            // 横向均匀铺开，避免两点重合被 InputDispatcher 当成同一指
            pts[i][0] = w * (i + 1f) / (k + 1f);
            pts[i][1] = y;
        }

        InjectBench.MultiResult r = InjectBench.multiTouch(
                last.inputManagerProxy, last.injectMethod, pts);
        Log.i(ShizukuProbe.TAG, "=== " + k + " 指: " + r.fmt() + " ===");

        // 事件是异步投递，给 700 ms 走到分发器
        h.postDelayed(() -> {
            String rep = sink.report();
            boolean ok = sink.getMaxPointers() >= k;
            Log.i(ShizukuProbe.TAG, "=== " + k + " 指结论: " + rep + "  ⇒ "
                    + (ok ? k + " 指全部被消费" : "指针数不足（只有 " + sink.getMaxPointers() + "），多指未生效")
                    + " ===");
            runPointerCount(counts, idx + 1);
        }, 700);
    }

    /**
     * 端内服务的进度轮询。
     *
     * ★ 为什么读文件而不是 bind/广播：服务是**故意**独立于 Activity 的（注入时 Activity
     *   已经 onStop）。用文件当唯一的跨进程状态通道，PC 侧 `adb pull status.txt` 也能看，
     *   调试时少一层。
     */
    private final Runnable svcPoller = new Runnable() {
        @Override public void run() {
            if (tvSvc != null && svcDir != null) {
                tvSvc.setText(tail(new File(svcDir, "status.txt"), 12));
            }
            h.postDelayed(this, 1000);
        }
    };

    private static String tail(File f, int lines) {
        if (!f.exists()) return "（端内服务未待命）";
        try (RandomAccessFile raf = new RandomAccessFile(f, "r")) {
            long len = raf.length();
            int want = Math.min((int) Math.max(0, len), 6000);
            raf.seek(Math.max(0, len - want));
            byte[] buf = new byte[want];
            raf.readFully(buf);
            String all = new String(buf, StandardCharsets.UTF_8);
            String[] parts = all.split("\n");
            StringBuilder sb = new StringBuilder();
            int from = Math.max(0, parts.length - lines);
            for (int i = from; i < parts.length; i++) sb.append(parts[i]).append('\n');
            return sb.toString();
        } catch (Exception e) {
            return "（读 status.txt 失败: " + e + "）";
        }
    }

    private void armService() {
        if (last == null || !last.allOk()) {
            toast("门禁没过 ⇒ 不待命");
            return;
        }
        try {
            Intent it = new Intent(this, InjectService.class).setAction(InjectService.ACTION_ARM);
            startForegroundService(it);
            toast("已待命：切到 Phigros 就绪界面等我点火");
        } catch (Throwable t) {
            toast("待命失败: " + ShizukuProbe.brief(t));
        }
    }

    private void render(String text) {
        tvOut.setText(text);
    }

    private void toast(String s) {
        Toast.makeText(this, s, Toast.LENGTH_SHORT).show();
    }

    private View buildUi() {
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        int pad = dp(12);
        root.setPadding(pad, pad, pad, pad);

        TextView title = new TextView(this);
        title.setText("PG 注入器 · S0 门禁");
        title.setTextSize(TypedValue.COMPLEX_UNIT_SP, 18);
        title.setPadding(0, 0, 0, dp(8));
        root.addView(title);

        btnPerm = mkButton("① 申请权限");
        btnRun = mkButton("② 重新检测");
        btnCopy = mkButton("③ 复制报告");
        btnInject = mkButton("④ 注入自检（真的点一下「②」）");
        btnCost = mkButton("⑤ 注入成本（200 次，报 p50/p99/max）");
        btnMulti = mkButton("⑥ 多指注入（3 指，看靶子收几个）");
        btnArm = mkButton("⑦ 待命（端内「脑」：等 PC 给零点后自己打）");
        btnStop = mkButton("⑧ 停止端内注入");
        // 门禁没过之前禁用：S0 还没跑通就注入，只会得到一个含义不明的失败。
        btnInject.setEnabled(false);
        btnCost.setEnabled(false);
        btnMulti.setEnabled(false);
        btnArm.setEnabled(false);
        btnStop.setEnabled(true);
        btnArm.setOnClickListener(v -> armService());
        btnStop.setOnClickListener(v -> {
            try {
                startService(new Intent(this, InjectService.class)
                        .setAction(InjectService.ACTION_STOP));
                toast("已发停止指令");
            } catch (Throwable t) {
                toast("停止失败: " + ShizukuProbe.brief(t));
            }
        });
        btnInject.setOnClickListener(v -> doInjectSelfTest());
        btnCost.setOnClickListener(v -> doCostTest());
        btnMulti.setOnClickListener(v -> doMultiTouchTest());

        btnPerm.setOnClickListener(v -> {
            try {
                Shizuku.requestPermission(REQ_SHIZUKU);
                toast("已弹出授权请求，请在手机上确认");
            } catch (Throwable t) {
                toast("申请失败: " + ShizukuProbe.brief(t));
            }
        });
        btnRun.setOnClickListener(v -> runOnce());
        btnCopy.setOnClickListener(v -> {
            ClipboardManager cm = (ClipboardManager) getSystemService(Context.CLIPBOARD_SERVICE);
            if (cm != null) {
                cm.setPrimaryClip(ClipData.newPlainText("pg-inject", lastReport));
                toast("已复制");
            }
        });

        root.addView(btnPerm);
        root.addView(btnRun);
        root.addView(btnCopy);
        root.addView(btnInject);
        root.addView(btnCost);
        root.addView(btnMulti);
        root.addView(btnArm);
        root.addView(btnStop);

        tvSvc = new TextView(this);
        tvSvc.setTextSize(TypedValue.COMPLEX_UNIT_SP, 10);
        tvSvc.setTypeface(android.graphics.Typeface.MONOSPACE);
        tvSvc.setTextIsSelectable(true);
        tvSvc.setPadding(0, dp(6), 0, dp(6));
        root.addView(tvSvc);

        tvOut = new TextView(this);
        tvOut.setTextSize(TypedValue.COMPLEX_UNIT_SP, 12);
        // 等宽字体：报告是逐行对齐的，比例字体读起来会错行
        tvOut.setTypeface(android.graphics.Typeface.MONOSPACE);
        tvOut.setGravity(Gravity.START);
        tvOut.setTextIsSelectable(true);
        tvOut.setPadding(0, dp(8), 0, 0);

        ScrollView sc = new ScrollView(this);
        sc.addView(tvOut, new ViewGroup.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));
        root.addView(sc, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f));

        // 靶子必须**晚于**内容加入，才能盖在最上层接到注入事件。
        // 平时 GONE：它可见时会吃掉所有触摸，按钮就点不动了。
        FrameLayout frame = new FrameLayout(this);
        frame.addView(root, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
        sink = new PointerSinkView(this);
        sink.setVisibility(View.GONE);
        frame.addView(sink, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
        return frame;
    }

    private Button mkButton(String label) {
        Button b = new Button(this);
        b.setText(label);
        b.setAllCaps(false);
        b.setLayoutParams(new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));
        return b;
    }

    private int dp(int v) {
        return Math.round(v * getResources().getDisplayMetrics().density);
    }
}
