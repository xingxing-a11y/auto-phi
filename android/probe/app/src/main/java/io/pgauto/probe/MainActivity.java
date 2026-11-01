package io.pgauto.probe;

import android.Manifest;
import android.app.Activity;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.graphics.Typeface;
import android.media.projection.MediaProjectionManager;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.text.InputType;
import android.view.Gravity;
import android.view.ViewGroup;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;

/**
 * 探针界面。三件事：
 *   ① 一键开始（自动串起 录音权限 → 投屏授权 → 前台服务录制）
 *   ② 录制中**实时显示峰值幅度** —— 这是本探针的核心，用户不必等分析，
 *      一眼就能看出「有没有收到声音」（全 0 = 静音 = 捕获失败）
 *   ③ 显示落盘路径，方便 `adb pull`
 *
 * 不用 AndroidX（省依赖、省下载），所以走已废弃但可用的 startActivityForResult。
 */
public class MainActivity extends Activity {

    private static final int REQ_AUDIO = 101;
    private static final int REQ_PROJ = 102;

    private TextView tvState, tvPeak, tvMeter, tvPath, tvErr, tvOnset, tvLat, tvNotice, tvPulse;
    private Button btnStart, btnStop, btnPulse;
    private final Handler h = new Handler(Looper.getMainLooper());

    private final Runnable tick = new Runnable() {
        @Override
        public void run() {
            refresh();
            h.postDelayed(this, 200);
        }
    };

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(buildUi());
        h.post(tick);
    }

    @Override
    protected void onDestroy() {
        h.removeCallbacks(tick);
        super.onDestroy();
    }

    private LinearLayout row() {
        LinearLayout l = new LinearLayout(this);
        l.setOrientation(LinearLayout.HORIZONTAL);
        l.setPadding(0, 8, 0, 8);
        return l;
    }

    private TextView label(String s, float sp, boolean bold) {
        TextView t = new TextView(this);
        t.setText(s);
        t.setTextSize(sp);
        if (bold) t.setTypeface(Typeface.DEFAULT_BOLD);
        return t;
    }

    private ScrollView buildUi() {
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setPadding(40, 40, 40, 40);

        TextView title = label("Phigros 音频捕获探针", 22, true);
        root.addView(title);

        TextView desc = label(
                "用法：\n"
                        + "1) 点「开始录制」，系统会问是否允许录制/投放 → 允许\n"
                        + "2) 切到 Phigros，选歌并让它响起来（不用打完，几秒就行）\n"
                        + "3) 回到本应用点「停止录制」\n"
                        + "4) 电脑上 adb pull 下面那个路径\n\n"
                        + "★ 判据看「本轮最大」：录完回来它应停在几千~几万。\n"
                        + "   （瞬时值此时必然为 0 —— 切回本应用后游戏已暂停，属正常）\n"
                        + "   若本轮最大也是 0，才说明捕获被系统/机型拦掉了。\n\n"
                        + "★ 同时会落一个 .tsv（时间戳 + 10ms 能量包络 + onset）：\n"
                        + "   · 积压 = 硬件已采集帧 − 应用已读帧 → 真正可解释的延迟量\n"
                        + "     （2026-10-18 实测：中位 70~83 ms，P5~P95 47~93 ms）\n"
                        + "   · 硬件时间戳成功次数 / 总块数 → 本机 getTimestamp 能不能用\n"
                        + "     （实测 100% 成功；但它的钟与 elapsedRealtime 差一个常数\n"
                        + "      65569.5 s，绝对值当延迟看会得到 18 小时这种荒谬数）\n"
                        + "   · onset 计数 → 实时检出是否在工作（不是最终判决，最终看 PC 端抓包络）\n\n"
                        + "★ 「播放测试脉冲」只在录制中可用，用途是量出「播放 → 采集」的延迟 d1：\n"
                        + "   播 5 个 1050 Hz 短脉冲（间隔 100 ms、前置 0.5 s 静音），同时把播放侧\n"
                        + "   的 AudioTrack.getTimestamp 写进 sidecar 的 P 行，与录音侧的 B 行对称。\n"
                        + "   ⛔ 必须在安静环境跑（先把游戏停掉）：有音乐时积压 70 ms，会把不确定度\n"
                        + "      放大到静音时的 6 倍。",
                14, false);
        desc.setPadding(0, 16, 0, 16);
        root.addView(desc);

        LinearLayout btns = row();
        btnStart = new Button(this);
        btnStart.setText("开始录制");
        btnStart.setOnClickListener(v -> askAudioThenProjection());
        btns.addView(btnStart, new LinearLayout.LayoutParams(0,
                ViewGroup.LayoutParams.WRAP_CONTENT, 1f));

        btnStop = new Button(this);
        btnStop.setText("停止录制");
        btnStop.setEnabled(false);
        btnStop.setOnClickListener(v -> {
            stopService(new Intent(this, RecorderService.class));
            tvState.setText("状态：已请求停止…");
        });
        btns.addView(btnStop, new LinearLayout.LayoutParams(0,
                ViewGroup.LayoutParams.WRAP_CONTENT, 1f));
        root.addView(btns);

        // 标定脉冲：只在录制中可用 —— 脉冲必须落在同一份录音里才能和 B 行对上
        btnPulse = new Button(this);
        btnPulse.setText("播放测试脉冲（标定播放→采集延迟）");
        btnPulse.setEnabled(false);
        btnPulse.setOnClickListener(v -> {
            RecorderService.playPulse(this);
            tvPulse.setText("脉冲：已请求…");
        });
        root.addView(btnPulse);

        tvPulse = label("脉冲：—", 14, false);
        tvPulse.setPadding(0, 4, 0, 8);
        root.addView(tvPulse);

        tvState = label("状态：空闲", 16, true);
        tvState.setPadding(0, 16, 0, 4);
        root.addView(tvState);

        tvPeak = label("峰值：—", 30, true);
        tvPeak.setPadding(0, 8, 0, 4);
        root.addView(tvPeak);

        tvMeter = label("", 16, false);   // 用字符条模拟电平表
        tvMeter.setTypeface(Typeface.MONOSPACE);
        tvMeter.setPadding(0, 0, 0, 8);
        root.addView(tvMeter);

        tvOnset = label("onset：—", 16, false);
        tvOnset.setPadding(0, 0, 0, 4);
        root.addView(tvOnset);

        tvLat = label("硬件时间戳：—", 14, false);
        tvLat.setPadding(0, 0, 0, 8);
        root.addView(tvLat);

        tvPath = label("文件：—", 12, false);
        tvPath.setPadding(0, 8, 0, 4);
        tvPath.setTextIsSelectable(true);
        tvPath.setInputType(InputType.TYPE_CLASS_TEXT);
        root.addView(tvPath);

        tvErr = label("", 13, false);
        tvErr.setTextColor(Color.parseColor("#B00020"));
        tvErr.setPadding(0, 8, 0, 0);
        root.addView(tvErr);

        // 非错误提示单独一行、不用红色：撞 300 s 上限属于设计行为，数据是完整的
        tvNotice = label("", 13, false);
        tvNotice.setTextColor(Color.parseColor("#1B5E20"));
        tvNotice.setPadding(0, 4, 0, 0);
        root.addView(tvNotice);

        ScrollView sv = new ScrollView(this);
        sv.addView(root);
        return sv;
    }

    // ── 流程 ────────────────────────────────────────────────────────────────
    private void askAudioThenProjection() {
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO)
                != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.RECORD_AUDIO}, REQ_AUDIO);
            return;
        }
        askProjection();
    }

    @Override
    public void onRequestPermissionsResult(int req, String[] perms, int[] res) {
        super.onRequestPermissionsResult(req, perms, res);
        if (req == REQ_AUDIO) {
            if (res.length > 0 && res[0] == PackageManager.PERMISSION_GRANTED) {
                askProjection();
            } else {
                tvErr.setText("✗ 没有录音权限，无法建采集流");
            }
        }
    }

    private void askProjection() {
        MediaProjectionManager mpm =
                (MediaProjectionManager) getSystemService(MEDIA_PROJECTION_SERVICE);
        startActivityForResult(mpm.createScreenCaptureIntent(), REQ_PROJ);
    }

    @Override
    protected void onActivityResult(int req, int resultCode, Intent data) {
        super.onActivityResult(req, resultCode, data);
        if (req != REQ_PROJ) return;
        if (resultCode != RESULT_OK || data == null) {
            tvErr.setText("✗ 投屏授权被拒绝（没有授权就无法捕获音频）");
            return;
        }
        Intent svc = new Intent(this, RecorderService.class)
                .putExtra(RecorderService.EXTRA_RESULT_CODE, resultCode)
                .putExtra(RecorderService.EXTRA_RESULT_DATA, data);
        // ★ 2026-10-19：把 Activity 收到的运行期参数原样转给 Service。
        //   `adb shell am start -n io.pgauto.probe/.MainActivity --ei bufBytes 8192 --ei maxSeconds 20`
        //   —— 这样「缓冲容量」与「录制上限」都能在**同一次安装**里换，
        //     不必重编译重装（重装会换掉整个 HAL 会话，混进新变量）。
        int rb = getIntent().getIntExtra(RecorderService.EXTRA_BUF_BYTES, 0);
        if (rb > 0) svc.putExtra(RecorderService.EXTRA_BUF_BYTES, rb);
        int ms = getIntent().getIntExtra(RecorderService.EXTRA_MAX_SECONDS, 0);
        if (ms > 0) svc.putExtra(RecorderService.EXTRA_MAX_SECONDS, ms);
        // 采样率同理：设备混音是 48000 Hz，我们历来按 44100 申请 ⇒ 采集侧过 48k→44.1k
        // 重采样。要判它是不是 d_total 的一项，必须在同一次安装里换。
        int rt = getIntent().getIntExtra(RecorderService.EXTRA_RATE, 0);
        if (rt > 0) svc.putExtra(RecorderService.EXTRA_RATE, rt);
        int sa = getIntent().getIntExtra(RecorderService.EXTRA_STALL_AT, 0);
        if (sa > 0) svc.putExtra(RecorderService.EXTRA_STALL_AT, sa);
        int sp = getIntent().getIntExtra(RecorderService.EXTRA_STALL_PROBES, 0);
        if (sp > 0) svc.putExtra(RecorderService.EXTRA_STALL_PROBES, sp);
        startForegroundService(svc);
        tvState.setText("状态：启动中…");
    }

    // ── 刷新 ────────────────────────────────────────────────────────────────
    private void refresh() {
        boolean running = RecorderService.sRunning;
        btnStart.setEnabled(!running);
        btnStop.setEnabled(running);
        btnPulse.setEnabled(running);
        tvPulse.setText(RecorderService.sPulseState.isEmpty()
                ? "脉冲：—"
                : "脉冲：" + RecorderService.sPulseCount + " 次 ｜ " + RecorderService.sPulseState);

        if (running) {
            long sec = RecorderService.sFrames / 44100L;
            tvState.setText("状态：录制中  " + sec + " s");
        } else if (RecorderService.sError.isEmpty()) {
            tvState.setText("状态：空闲");
        } else {
            tvState.setText("状态：已停止");
        }

        int peak = RecorderService.sPeak;
        int peakMax = RecorderService.sPeakMax;
        // ★ 整轮最大值必须一直显示（哪怕已停止）：切回本应用时游戏已暂停，
        //   瞬时值必然归零，只看它会得出「捕获被拦掉」的假阴性（2026-10-18 实踩）。
        tvPeak.setText(peakMax > 0
                ? "峰值：本轮最大 " + peakMax + " ｜ 瞬时 " + peak
                : "峰值：" + (running ? "等待信号…" : "—"));
        if (running || peakMax > 0) {
            int bars = (int) Math.round(Math.log10(Math.max(peakMax, 1)) / Math.log10(32768) * 30);
            StringBuilder sb = new StringBuilder();
            for (int i = 0; i < 30; i++) sb.append(i < bars ? '█' : '·');
            tvMeter.setText("[" + sb + "]");
        }

        // onset：整轮累计 + 最近一次的时刻（相对开录）
        int ons = RecorderService.sOnsets;
        long lastMs = RecorderService.sLastOnsetMs;
        tvOnset.setText(ons > 0
                ? "onset：" + ons + " 个（最近 " + String.format(java.util.Locale.US, "%.2f", lastMs / 1000.0) + " s）"
                : (RecorderService.sRunning ? "onset：等待…" : "onset：0"));

        long tried = RecorderService.sTsTried;
        long ok = RecorderService.sTsOk;
        long backlog = RecorderService.sBacklogMs;
        if (tried == 0) {
            tvLat.setText("硬件时间戳：—（缓冲 " + RecorderService.sBufBytes + " B）");
        } else {
            // ★ 别再显示 `tRead − hwNs`：它的中位数是两钟基准差（实测 65569507 ms），
            //   不是延迟。改显示「积压」——硬件已采集领先应用已读多少，这才是可解释的延迟量。
            tvLat.setText("硬件时间戳：" + ok + "/" + tried
                    + " ｜ 积压 " + (backlog < 0 ? "—" : backlog + " ms")
                    + " ｜ 缓冲 " + RecorderService.sBufBytes + " B");
        }

        String p = RecorderService.sPath;
        if (!p.isEmpty()) tvPath.setText("文件：" + p + "\n时序：" + RecorderService.sTsPath);
        tvErr.setText(RecorderService.sError.isEmpty() ? "" : "✗ " + RecorderService.sError);
        tvNotice.setText(RecorderService.sNotice.isEmpty() ? "" : "⏹ " + RecorderService.sNotice);
    }
}
