package io.pgauto.pulse;

import android.app.Activity;
import android.content.Context;
import android.graphics.Typeface;
import android.media.AudioAttributes;
import android.media.AudioFormat;
import android.media.AudioManager;
import android.media.AudioTrack;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.SystemClock;
import android.util.Log;
import android.widget.ScrollView;
import android.widget.TextView;

import java.io.BufferedWriter;
import java.io.File;
import java.io.FileOutputStream;
import java.io.OutputStreamWriter;
import java.nio.charset.StandardCharsets;
import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.Locale;

/**
 * 标定用播放器：**启动即播一段 5 s 线性扫频**，并把 `tPlay` 写进 sidecar。
 *
 * <p>为什么要单独一个 APK（而不是在录制探针里播）：
 * Android 的 playback capture 按 **uid** 分流 —— 捕获方自己的播放被路由到 SPEAKER、
 * 不进 REMOTE_SUBMIX，所以「自播自录」录到的是恒 0（FACTS §89 三轮实测）。
 * 换一个包 ⇒ 换一个 uid ⇒ 它的播放才会进 REMOTE_SUBMIX 被录到。
 *
 * <p>★ 只记 `tPlay = elapsedRealtimeNanos()`（`play()` 调用那一刻），**不依赖
 * `AudioTrack.getTimestamp()`** —— 那台机器上它恒返回 0（202/222「成功」但值无意义）。
 *
 * <p>★ 缓冲策略：只申请约 1 秒的 buffer，先写 1 秒、`play()`、再边播边写。
 * 一次性申请 5 秒（882 KB）的 buffer 风险太大（可能被 HAL 拒绝或退回小 buffer 导致
 * `write` 长时间阻塞，把 `tPlay` 与真正出声的时刻拉开）。
 */
public class MainActivity extends Activity {

    public static final String TAG = "pgpulse";

    /** 分块写入的样本数（含双声道）⇒ 4096 帧 ≈ 93 ms。 */
    private static final int CHUNK = 8192;

    /** AudioTrack buffer 目标 ≈ 1 秒。 */
    private static final int BUF_TARGET_BYTES = 176400;

    // ── 运行期参数（0 / 默认 = 保持原口径）─────────────────────────────────
    // ★ 为什么必须可运行期改：本格的四个臂（44.1k/48k × 1s buffer/minBuf×2）
    //   要在**同一次安装**里跑完。每换一个值就重装，会换掉整个 HAL 会话，
    //   臂效应与重装效应混在一起，得到的「差异」无法归因。
    private int reqRate = ChirpGen.RATE;
    private int reqBufBytes = 0;
    /** >0 时按 `minBuf × mul / 10` 申请 —— 因为「绝对字节数」在不同采样率下
     *  与 minBuf 的关系完全不同（48k 的 minBuf 是 15376，44.1k 是 14144），
     *  写死一个 8192 会直接被系统抬到 minBuf，实验就变成了「测 minBuf」。 */
    private int reqBufMulX10 = 0;
    /**
     * 播放时的媒体音量目标（百分比，1~100）。默认 **25%**。
     *
     * ★ 2026-10-19 20:1x 用户要求降到 25%（原为 3/4 = 75%，扫频很吵）。
     *   下限不能是 0：音量 0 ⇒ 捕获到的样本全 0 且不报错（§89），整轮白跑。
     *   上限放到 100 是为了「安静环境想提高信噪比」时还能用。
     */
    private int reqVolPct = 25;
    private int reqPerfMode = 0;   // 0=不设 1=LOW_LATENCY 2=POWER_SAVING
    private int reqSegs = 2;
    private double reqGapS = 2.0;

    private TextView tv;
    private final Handler ui = new Handler(Looper.getMainLooper());
    private final StringBuilder logBuf = new StringBuilder();

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        ScrollView sv = new ScrollView(this);
        tv = new TextView(this);
        tv.setPadding(40, 40, 40, 40);
        tv.setTextSize(13);
        tv.setTypeface(Typeface.MONOSPACE);
        tv.setText("准备中…");
        sv.addView(tv);
        setContentView(sv);
        readExtras();
        new Thread(this::run, "pulse-play").start();
    }

    /**
     * 读运行期参数。**每个值都夹在合法区间**：乱传一个不支持的 rate 会被 HAL 静默重采样，
     * 那一臂就变成「测了另一个未知配置」，比不复核更危险。
     */
    private void readExtras() {
        Bundle ex = getIntent() == null ? null : getIntent().getExtras();
        if (ex == null) return;
        int r = ex.getInt("rate", 0);
        if (r == 44100 || r == 48000 || r == 32000) reqRate = r;
        int bb = ex.getInt("bufBytes", 0);
        if (bb >= 4096 && bb <= (1 << 20)) reqBufBytes = bb;
        int mul = ex.getInt("bufMulX10", 0);
        if (mul >= 10 && mul <= 100) reqBufMulX10 = mul;
        // 音量百分比：夹在 1~100。0 是禁区（会让捕获全 0）。
        int vp = ex.getInt("volPct", 0);
        if (vp >= 1 && vp <= 100) reqVolPct = vp;
        int pm = ex.getInt("perfMode", 0);
        if (pm >= 0 && pm <= 2) reqPerfMode = pm;
        int sg = ex.getInt("segs", reqSegs);
        if (sg >= 1 && sg <= 4) reqSegs = sg;
        int gm = ex.getInt("gapMs", (int) Math.round(reqGapS * 1000));
        if (gm >= 0 && gm <= 10000) reqGapS = gm / 1000.0;
    }

    private void log(String s) {
        Log.i(TAG, s);
        ui.post(() -> {
            logBuf.append(s).append('\n');
            tv.setText(logBuf.toString());
        });
    }

    private void run() {
        AudioManager am = (AudioManager) getSystemService(Context.AUDIO_SERVICE);
        final int volPre = am == null ? -1 : am.getStreamVolume(AudioManager.STREAM_MUSIC);
        final int volMax = am == null ? -1 : am.getStreamMaxVolume(AudioManager.STREAM_MUSIC);

        // ★ 媒体音量 0 时 AudioPlaybackCapture 抓到的也是 0（FACTS §89 实测），
        //   且播放不报任何错 ⇒ 必须先提起来，否则整轮白跑。
        //
        // ★★ 2026-10-19 20:1x（用户要求）：原来是「提到 3/4（12/16）」—— 扫频是 300~6000 Hz
        //   的连续音，3/4 音量在安静房间里很吵。改成**默认 25%（4/16）**，并做成运行期参数
        //   `--ei volPct N`，以后不用重编译就能调。
        //   为什么不能降到 0：0 会让捕获到的样本全 0（且不报错），整轮无效。
        //   为什么 25% 仍能测：互相关是**归一化**的（NCC），幅度只影响峰值不影响定位；
        //   只要不削顶、不为 0，降到 1/4 对 `d_total` 没有任何影响。
        //
        // 三档逻辑（都不违背用户设置）：
        //   volPre == 0        → 提到 target（否则抓不到）
        //   volPre >  target   → 压到 target（用户嫌吵）
        //   0 < volPre ≤ target → 保持不动（用户本来就开得低，尊重）
        int volTarget = volMax > 0 ? Math.max(1, volMax * reqVolPct / 100) : -1;
        int vol = volPre;
        boolean bumped = false;
        if (am != null && volMax > 0) {
            int want;
            if (volPre <= 0) want = volTarget;
            else if (volPre > volTarget) want = volTarget;
            else want = volPre;
            if (want != volPre) {
                try {
                    am.setStreamVolume(AudioManager.STREAM_MUSIC, want, 0);
                    bumped = true;
                } catch (Throwable ignored) {
                }
                int after = am.getStreamVolume(AudioManager.STREAM_MUSIC);
                if (after > 0) vol = after;
            }
        }
        log("音量 " + vol + "/" + volMax + "（播放前 " + volPre + "，目标 "
                + reqVolPct + "%）");

        File dir = getExternalFilesDir(null);
        if (dir == null) dir = getFilesDir();
        //noinspection ResultOfMethodCallIgnored
        dir.mkdirs();
        String stamp = new SimpleDateFormat("yyyyMMdd_HHmmss", Locale.US).format(new Date());
        File out = new File(dir, "pulse_" + stamp + ".tsv");

        AudioTrack tr = null;
        BufferedWriter w = null;
        try {
            int[] starts = new int[reqSegs];
            short[] pcm = ChirpGen.stereoWithGap(reqRate, reqSegs, reqGapS, starts);
            final int totalFrames = pcm.length / 2;
            int minBuf = AudioTrack.getMinBufferSize(reqRate,
                    AudioFormat.CHANNEL_OUT_STEREO, AudioFormat.ENCODING_PCM_16BIT);
            int bufBytes;
            if (reqBufBytes > 0) {
                bufBytes = reqBufBytes;
            } else if (reqBufMulX10 > 0) {
                bufBytes = Math.max(minBuf * reqBufMulX10 / 10, 4096);
            } else {
                bufBytes = Math.max(minBuf * 2, BUF_TARGET_BYTES);
            }

            AudioTrack.Builder tb = new AudioTrack.Builder()
                    .setAudioAttributes(new AudioAttributes.Builder()
                            // USAGE_MEDIA 必须落在录制方 AudioPlaybackCaptureConfiguration
                            // 的 addMatchingUsage 名单里，否则录不到。
                            .setUsage(AudioAttributes.USAGE_MEDIA)
                            .setContentType(AudioAttributes.CONTENT_TYPE_MUSIC)
                            .build())
                    .setAudioFormat(new AudioFormat.Builder()
                            .setSampleRate(reqRate)
                            .setChannelMask(AudioFormat.CHANNEL_OUT_STEREO)
                            .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                            .build())
                    .setBufferSizeInBytes(bufBytes)
                    .setTransferMode(AudioTrack.MODE_STREAM);
            if (reqPerfMode == 1) {
                tb.setPerformanceMode(AudioTrack.PERFORMANCE_MODE_LOW_LATENCY);
            } else if (reqPerfMode == 2) {
                tb.setPerformanceMode(AudioTrack.PERFORMANCE_MODE_POWER_SAVING);
            }
            tr = tb.build();
            if (tr.getState() != AudioTrack.STATE_INITIALIZED) {
                log("✗ AudioTrack state=" + tr.getState());
                writeErr(out, volPre, vol, volMax, "AudioTrack state=" + tr.getState());
                return;
            }

            // ★ 反向校验要用的实际值：请求的 bufBytes 不一定是系统给的。
            //   `getBufferSizeInFrames()` 返回**真正生效**的容量，两边都记下来，
            //   否则「压 buffer 没效果」可能只是「请求被忽略了」。
            int bufFrames = -1;
            try {
                bufFrames = tr.getBufferSizeInFrames();
            } catch (Throwable ignored) {
            }
            String psr = am == null ? "?" : am.getProperty(AudioManager.PROPERTY_OUTPUT_SAMPLE_RATE);
            String pfb = am == null ? "?" : am.getProperty(AudioManager.PROPERTY_OUTPUT_FRAMES_PER_BUFFER);

            w = new BufferedWriter(new OutputStreamWriter(
                    new FileOutputStream(out), StandardCharsets.UTF_8), 1 << 14);
            StringBuilder sb = new StringBuilder("M");
            sb.append("\trate=").append(reqRate).append("\tch=2");
            sb.append("\tframes=").append(totalFrames);
            sb.append("\tsegFrames=").append(ChirpGen.framesFor(reqRate));
            sb.append("\tf0=").append((int) ChirpGen.F0).append("\tf1=").append((int) ChirpGen.F1);
            sb.append("\ts=").append(ChirpGen.SECONDS);
            sb.append("\tbuf=").append(bufBytes).append("\tbufFrames=").append(bufFrames);
            sb.append("\tbufMulX10=").append(reqBufMulX10);
            sb.append("\tminBuf=").append(minBuf);
            sb.append("\tperf=").append(reqPerfMode);
            sb.append("\tsegs=").append(reqSegs).append("\tgapMs=").append(Math.round(reqGapS * 1000));
            sb.append("\tpropRate=").append(psr).append("\tpropFpb=").append(pfb);
            sb.append("\tvolPre=").append(volPre).append("\tvol=").append(vol);
            sb.append("\tvolPct=").append(reqVolPct);
            sb.append("\tvolMax=").append(volMax).append("\n");
            w.write(sb.toString());
            log("rate=" + reqRate + " buf=" + bufBytes + "→" + bufFrames + " 帧"
                    + " minBuf=" + minBuf + " perf=" + reqPerfMode
                    + " segs=" + reqSegs + " ｜ 设备 " + psr + " Hz/" + pfb + " 帧每缓冲");

            // ① 预写。★★ 预写量必须**严格小于实际 buffer 容量**，否则 `write()` 会在
            //   `play()` 之前一直阻塞 —— 因为 play 还没发生，buffer 不会被消费 ⇒ 死锁。
            //   这是本格真抓到的 bug：原来写死「预写 1 秒」，在 44100 + 176400 B 时
            //   恰好 == buffer 容量（踩在边界上侥幸没卡），换成 48000（1 s = 192000 B
            //   > 176400）或把小 buffer 传进来，直接卡死。症状是**录音峰值 0**，
            //   而 sidecar 里连一条错误都看不到（写在 M/S 之间静默停住）。
            //   现在按**实际生效容量**取一半，并为极小容量兜底 50 ms。
            int preFrames = Math.min(totalFrames,
                    Math.max(reqRate / 20, Math.max(1, bufFrames) / 2));
            int pre = preFrames * 2;   // short 个数（双声道）
            int written = 0;
            while (written < pre) {
                int n = tr.write(pcm, written, pre - written);
                if (n <= 0) {
                    log("✗ 预写失败 n=" + n);
                    writeErr(out, volPre, vol, volMax, "prewrite=" + n);
                    return;
                }
                written += n;
            }
            log("预写 " + (written / 2) + " 帧（buffer " + bufFrames + " 帧）");

            // ② 关键：记下 play() 的前一刻，然后立刻 play
            long tPlay = SystemClock.elapsedRealtimeNanos();
            tr.play();
            // S 行：tPlay + 预写帧数 + 每段首帧号（逗号分隔）。
            // ★ 段首帧号是「第 k 段应该出现在录音的哪个位置」的硬依据：
            //   第 2 段的播放时刻 = tPlay + (首帧号/rate)，不需要第二次 play()。
            StringBuilder st = new StringBuilder("S\t").append(tPlay).append('\t').append(written / 2);
            for (int k = 0; k < reqSegs; k++) st.append('\t').append(starts[k]);
            st.append('\n');
            w.write(st.toString());
            log("play() 已调用，tPlay=" + tPlay + " 段首帧=" + java.util.Arrays.toString(starts));

            // ③ 边播边写剩余。write 在 buffer 满时阻塞 ⇒ 天然节流，不会欠载
            int maxHead = 0;
            int seq = 0;
            long tFirstHead = -1;
            while (written < pcm.length) {
                int n = tr.write(pcm, written, Math.min(CHUNK, pcm.length - written));
                if (n <= 0) {
                    log("✗ 写入中断 n=" + n);
                    break;
                }
                written += n;
                long now = SystemClock.elapsedRealtimeNanos();
                int head = tr.getPlaybackHeadPosition();
                if (head > maxHead) maxHead = head;
                if (head > 0 && tFirstHead < 0) tFirstHead = now;
                if (seq < 400) {
                    w.write("H\t" + seq + "\t" + now + "\t" + head + "\t"
                            + tr.getPlayState() + "\n");
                }
                seq++;
            }
            log("已写入 " + (written / 2) + "/" + totalFrames + " 帧");

            // ④ 等播放真正走完（headPosition 追上总帧数，或超时）
            long deadline = SystemClock.elapsedRealtimeNanos()
                    + (long) ((reqSegs * ChirpGen.SECONDS + (reqSegs - 1) * reqGapS + 3.0) * 1e9);
            while (SystemClock.elapsedRealtimeNanos() < deadline) {
                int head = tr.getPlaybackHeadPosition();
                if (head > maxHead) maxHead = head;
                if (head > 0 && tFirstHead < 0) tFirstHead = SystemClock.elapsedRealtimeNanos();
                if (head >= totalFrames - 1) break;
                SystemClock.sleep(10);
                long now = SystemClock.elapsedRealtimeNanos();
                if (seq < 400) {
                    w.write("H\t" + seq + "\t" + now + "\t" + head + "\t"
                            + tr.getPlayState() + "\n");
                }
                seq++;
            }

            long tEnd = SystemClock.elapsedRealtimeNanos();
            int underruns = -1;
            try {
                underruns = tr.getUnderrunCount();
            } catch (Throwable ignored) {
            }
            w.write("E\t" + tEnd + "\t" + maxHead + "\t" + underruns
                    + "\tfirstHead=" + tFirstHead + "\tseq=" + seq + "\n");
            log("完成：maxHead=" + maxHead + "/" + totalFrames
                    + "  欠载=" + underruns
                    + "  播放跨度=" + (maxHead > 0 && tFirstHead > 0
                    ? (tEnd - tPlay) / 1_000_000L + " ms" : "—"));
            log("文件：" + out.getAbsolutePath());
        } catch (Throwable t) {
            Log.e(TAG, "run", t);
            log("✗ 异常：" + t);
            writeErr(out, volPre, vol, volMax, "ex=" + t);
        } finally {
            if (tr != null) {
                try {
                    tr.stop();
                } catch (Throwable ignored) {
                }
                tr.release();
            }
            if (w != null) {
                try {
                    w.close();
                } catch (Throwable ignored) {
                }
            }
            // 恢复原音量：这是我们临时改的系统设置，不恢复就违反「设备侧零残留」
            if (bumped && am != null) {
                try {
                    am.setStreamVolume(AudioManager.STREAM_MUSIC, volPre, 0);
                } catch (Throwable ignored) {
                }
                log("音量已恢复为 " + volPre);
            }
        }
    }

    private void writeErr(File out, int volPre, int vol, int volMax, String why) {
        try (BufferedWriter w = new BufferedWriter(new OutputStreamWriter(
                new FileOutputStream(out), StandardCharsets.UTF_8))) {
            w.write("M\trate=" + reqRate + "\tvolPre=" + volPre + "\tvol=" + vol
                    + "\tvolMax=" + volMax + "\n");
            w.write("X\t" + why + "\n");
        } catch (Throwable ignored) {
        }
    }
}
