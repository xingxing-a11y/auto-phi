package io.pgauto.probe;

import android.content.Context;
import android.media.AudioAttributes;
import android.media.AudioFormat;
import android.media.AudioManager;
import android.media.AudioTimestamp;
import android.media.AudioTrack;
import android.os.SystemClock;
import android.util.Log;

/**
 * 播放标定脉冲串，并把播放侧的硬件时间戳打进 sidecar。
 *
 * <p>★ 本类是整个 `d1` 标定的关键：**播放时刻不用 host 给**，而用
 * {@code AudioTrack.getTimestamp()}（与录音侧 {@code AudioRecord.getTimestamp()} 同一套机制）。
 * 两端都在 elapsedRealtime 轴上，才能真正相减。
 *
 * <p>★ 缓冲必须一次装下整段：`MODE_STREAM` 下 `write()` 分段进入会让「第 k 个脉冲在第几帧」
 * 仍然正确（帧偏移是相对 AudioTrack 的），但会让 `play()` 与数据就位之间的时序不可控。
 * 这里申请 `byteCount() + 8 KB`，一次写完再 play。
 *
 * <p>★ 写入与播放都放后台线程：整段 1.5 s，阻塞主线程会让 UI 假死、也会抢走
 * 录制线程的调度时间（录制侧每 46 ms 一块，被抢会直接推高积压）。
 */
public final class PulsePlayer {

    public static final String TAG = "pgprobe";

    /** 时间戳轮询间隔。5 ms × 1 s ≈ 200 行，够拟合直线又不至于把 sidecar 撑爆。 */
    private static final int POLL_MS = 5;

    /** 兜底超时：正常 1 s 内结束，8 s 还没结束一定是 HAL 卡了。 */
    private static final int MAX_WAIT_MS = 8000;

    /** sidecar `P` 行的接收器（由 RecorderService 提供，最终落到 .tsv）。 */
    public interface Sink {
        void accept(String line);
    }

    private PulsePlayer() {
    }

    /**
     * 阻塞播放一遍脉冲串。调用方负责放到后台线程。
     *
     * <p>★ 返回人类可读的结果串（而不是 boolean）：失败有 3 条互不相同的路径
     * （缓冲参数不支持 / AudioTrack 没初始化 / write 失败），boolean 会把它们糊成一个
     * 「没播成」，真机上一旦出问题就分不清该改代码还是改机型设置。
     *
     * @return 非空的结果摘要；以「失败」开头即真的没播出去
     */
    public static String play(Context ctx, Sink sink) {
        short[] pcm = PulseGen.stereo();
        int pcmBytes = pcm.length * 2;

        AudioManager am = (AudioManager) ctx.getSystemService(Context.AUDIO_SERVICE);
        final int vol0 = am == null ? -1 : am.getStreamVolume(AudioManager.STREAM_MUSIC);
        final int volMax = am == null ? -1 : am.getStreamMaxVolume(AudioManager.STREAM_MUSIC);

        // ★★ 2026-10-19 真机实测：媒体音量 0 时 `AudioPlaybackCapture` 抓到的**也是 0**
        //   （整轮 wav 峰值 = 0、非零样本 0 个），而 `AudioTrack.write/play` 全程正常、
        //   sidecar 里 P 行 227 条、时间戳 100% 成功 —— 也就是说**不报任何错**。
        //   ⇒ 不主动提音量就会「看起来跑成功了、数据全废」，且要等到 PC 端分析才发现。
        //   提到 3/4 而不是满档：留点余量，避免后续任何增益级（系统音效/EQ）削波。
        int vol = vol0;
        boolean bumped = false;
        if (am != null && volMax > 0 && vol0 < (volMax * 3) / 4) {
            try {
                am.setStreamVolume(AudioManager.STREAM_MUSIC, (volMax * 3) / 4, 0);
                bumped = true;
            } catch (Throwable ignored) {
                // 免打扰模式可能拒绝；下面读回真值，按真值记录
            }
            int after = am.getStreamVolume(AudioManager.STREAM_MUSIC);
            if (after > vol) vol = after;
        }

        int minBuf = AudioTrack.getMinBufferSize(PulseGen.RATE,
                AudioFormat.CHANNEL_OUT_STEREO, AudioFormat.ENCODING_PCM_16BIT);
        if (minBuf <= 0) {
            // 失败也写一行 S：字段数与成功路径一致（前 7 个字段可解析），末尾 kv 加 err
            sink.accept(PulseLines.start(-1, 0, vol, volMax, vol0, -1, -1) + ",err=minBuf" + minBuf);
            return "失败：getMinBufferSize=" + minBuf;
        }
        // 一次装下整段（+8 KB 余量），保证 write 不阻塞、play 时数据已在缓冲里
        int bufBytes = Math.max(minBuf * 2, pcmBytes + 8192);

        AudioTrack tr = null;
        try {
            tr = new AudioTrack.Builder()
                    .setAudioAttributes(new AudioAttributes.Builder()
                            // ★ USAGE_MEDIA 必须落在 AudioPlaybackCaptureConfiguration
                            //   的 addMatchingUsage 名单里，否则自己播的东西录不到。
                            .setUsage(AudioAttributes.USAGE_MEDIA)
                            .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION)
                            .build())
                    .setAudioFormat(new AudioFormat.Builder()
                            .setSampleRate(PulseGen.RATE)
                            .setChannelMask(AudioFormat.CHANNEL_OUT_STEREO)
                            .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                            .build())
                    .setBufferSizeInBytes(bufBytes)
                    .setTransferMode(AudioTrack.MODE_STREAM)
                    .build();
            if (tr.getState() != AudioTrack.STATE_INITIALIZED) {
                sink.accept(PulseLines.start(-1, 0, vol, volMax, vol0, -1, -1)
                        + ",err=state" + tr.getState());
                return "失败：AudioTrack state=" + tr.getState();
            }

            long tBeforeWrite = SystemClock.elapsedRealtimeNanos();
            int written = 0;
            while (written < pcm.length) {
                int n = tr.write(pcm, written, pcm.length - written);
                if (n <= 0) {
                    sink.accept(PulseLines.start(tBeforeWrite, written / 2, vol, volMax, vol0, -1, -1)
                            + ",err=write" + n);
                    return "失败：write 返回 " + n + "（已写 " + written + "/" + pcm.length + "）";
                }
                written += n;
            }
            long tAfterWrite = SystemClock.elapsedRealtimeNanos();

            long tPlay = SystemClock.elapsedRealtimeNanos();
            tr.play();
            // S 行：seq=0；nFrames=实际写入帧数；vol/volMax 用于判「录不到是不是因为静音」；
            // tBeforeWrite/tAfterWrite/tPlay 三个时刻把「提交 → 真正开始播」的过程留证。
            sink.accept(PulseLines.start(tPlay, written / 2, vol, volMax, vol0,
                    (tPlay - tBeforeWrite) / 1_000_000L,
                    (tAfterWrite - tBeforeWrite) / 1_000_000L));

            AudioTimestamp ts = new AudioTimestamp();
            long deadline = SystemClock.elapsedRealtimeNanos() + MAX_WAIT_MS * 1_000_000L;
            long maxFrame = 0;
            int tried = 0, ok = 0;
            long prevFrame = -1;
            int stalls = 0;
            while (SystemClock.elapsedRealtimeNanos() < deadline) {
                long now = SystemClock.elapsedRealtimeNanos();
                tried++;
                boolean got = false;
                long f = -1, ns = -1;
                try {
                    // ★ AudioTrack 与 AudioRecord 的 getTimestamp **两处都不一样**（javap android-34 实测）：
                    //     AudioRecord.getTimestamp(AudioTimestamp, int timebase) → int（SUCCESS = 0）
                    //     AudioTrack .getTimestamp(AudioTimestamp)              → boolean
                    //   AudioTrack 的时基固定 MONOTONIC，与录音侧显式传 TIMEBASE_MONOTONIC 等价。
                    //   ⛔ 照抄录音侧那一行会连续编译失败两次（先参数个数、再返回类型）。
                    if (tr.getTimestamp(ts)) {
                        got = true;
                        f = ts.framePosition;
                        ns = ts.nanoTime;
                        ok++;
                        if (f > maxFrame) maxFrame = f;
                        if (f == prevFrame) stalls++; else stalls = 0;
                        prevFrame = f;
                    }
                } catch (Throwable ignored) {
                    // 某些机型不支持；退化路径，不致命
                }
                sink.accept(PulseLines.tick(tried, now, got, f, ns));
                if (maxFrame >= PulseGen.endFrames()) break;
                if (tr.getPlayState() != AudioTrack.PLAYSTATE_PLAYING) break;
                // framePosition 连续 200 次（1 s）不动 = HAL 不再推进，别无限等
                if (stalls > 200) break;
                SystemClock.sleep(POLL_MS);
            }

            sink.accept(PulseLines.end(maxFrame, SystemClock.elapsedRealtimeNanos(),
                    tried, ok, vol, volMax));
            return ok > 0
                    ? "已播放（播放侧时间戳 " + ok + "/" + tried + "，音量 " + vol + "/" + volMax + "）"
                    : "已播放但时间戳全失败（0/" + tried + "，音量 " + vol + "/" + volMax + "）";
        } catch (Throwable t) {
            Log.e(TAG, "pulse play", t);
            sink.accept(PulseLines.start(-1, 0, vol, volMax, vol0, -1, -1) + ",ex=" + t);
            return "异常：" + t;
        } finally {
            if (tr != null) {
                try {
                    tr.stop();
                } catch (Throwable ignored) {
                }
                tr.release();
            }
            // ★ 一定恢复原音量（哪怕是失败路径）：这是我们临时改的系统设置，
            //   不恢复就是「为了做实验改了用户设备状态」——违反「设备侧零残留」。
            if (bumped && am != null) {
                try {
                    am.setStreamVolume(AudioManager.STREAM_MUSIC, vol0, 0);
                } catch (Throwable ignored) {
                }
            }
        }
    }
}
