package io.pgauto.play;

import android.content.Context;
import android.content.Intent;
import android.media.AudioAttributes;
import android.media.AudioFormat;
import android.media.AudioManager;
import android.media.AudioPlaybackCaptureConfiguration;
import android.media.AudioRecord;
import android.media.projection.MediaProjection;
import android.media.projection.MediaProjectionManager;
import android.os.Handler;
import android.os.Looper;
import android.os.SystemClock;

import java.util.Locale;

import io.pgauto.probe.OnsetDetector;

/**
 * 端内音频采集（`AudioPlaybackCapture`）—— 只在**本 app 进程**里跑，不落 wav、不写 sidecar。
 *
 * 与 `probe/RecorderService` 的区别（那段代码是为离线标定写的，39 KB，含 wav/sidecar/stall 探针）：
 *   · 不写文件（数据只留内存，符合「数据只留 PC 端」的约定）；
 *   · 不测 loopback 延迟（那是 Ⅱ-b 的事）；
 *   · 唯一职责：把包络喂给 {@link OnsetGate}，尽快吐出「曲首第一个音的时刻」。
 *
 * ★ 调用顺序是硬约束（Android 14 起）：**必须先 `startForeground(TYPE_MEDIA_PROJECTION)`，
 *   再 `getMediaProjection`**，否则抛 SecurityException。本类假定调用方（`PlayService`）
 *   已经做过，`start()` 里只把异常翻成人话。
 * ★ 每个授权 token 只能用一次 ⇒ 每轮都要用户重新点一次系统弹框（无法绕过，这是系统设计）。
 * ★★ **不拿媒体音量当闸门**（10-24 改，推翻原实现）：旧版在 `start()` 里硬检查
 *   `getStreamVolume(STREAM_MUSIC) == 0` 就抛，依据是「音量 0 时 capture 录到的也是 0」。
 *   那个结论来自一次**混杂实验**：10-19 三轮录的是 probe **自己**播的脉冲，而 Android
 *   设计上本 app 的播放永远不进 capture（同批数据里 vol=12 的两轮同样峰值 0）
 *   ⇒ 音量从来不是被隔离出来的变量。
 *   实测反证（10-24）：`bounded_in_r4` 在音量 0 下录到 **peak 32768 / −32768**、11 个台阶
 *   清晰并打出 φ；抽查 18 份历史录音**全部**满幅 ⇒ 本机 capture 取**音量缩放前**的渲染侧。
 *   ⇒ 现在只在**采集之后**按实测说话（`OnsetGate.allZero()`）：一帧非零样本都没有才算真静音，
 *     真静音才提示环境问题。音量值本身只写一行日志，**不参与任何判定**。
 * ★ `read()` 返回负数不许当「这轮没声音」跳过 —— 那是硬错误（-3 无效参数 / -6 已死），
 *   跳过就会静默等满超时，症状与「曲目没有音频」完全一样。
 */
public final class OnsetTap {

    /** 首选 48 kHz：设备混音就是 48000，绕开 48k→44.1k 重采样（那是一段不可控的延迟）。 */
    private static final int[] CANDIDATE_RATES = {48000, 44100};
    private static final int CHANNELS = 2;
    private static final int CHANNEL_MASK = AudioFormat.CHANNEL_IN_STEREO;
    private static final int ENCODING = AudioFormat.ENCODING_PCM_16BIT;
    /** 每块读 2048 帧（46.4 ms）。与 probe 同口径 —— 那里已验证不欠载。 */
    private static final int BLOCK_FRAMES = 2048;

    private final Context app;
    private final int resultCode;
    private final Intent resultData;
    /**
     * 母带的边沿阈值（RMS）。**≤0 = 未标定 ⇒ 相对判据**（旧行为，逐位不变）。
     *
     * ★ 为什么把阈值从这里传进去而不是让 Gate 自己猜：阈值是**曲目属性**（`sil_thr` 随母带走），
     *   而 Gate 只该回答「哪一刻算边沿」。两者分开才能分别单测（与 OnsetDetector 的分层一致）。
     */
    private final double silThrRms;

    private MediaProjection projection;
    private AudioRecord record;
    private Thread worker;
    private volatile OnsetGate gate;
    private volatile int rate;
    /**
     * 会话模式（2026-10-27，课题模式）：gate 触发后**不退出**采集线程，可连续待命多首。
     * ⚠ 生产/标定路径下必须为 false —— 那时"锁定即停"是省电且避免与注入并行的关键。
     */
    private volatile boolean sessionMode;
    /** 当前这一首的母带阈值（`rearm` 会改；`awaitFire` 丢弃过早触发时用它重建 gate）。 */
    private volatile double curSilThrRms;
    /** 每次 rearm 自增，供日志/自检区分"这一首的 gate"。 */
    private volatile int rearmGen;
    /**
     * 本次 rearm 之后**第一帧**的中点时刻（`elapsedRealtimeNanos`）；0 = 还没收到帧。
     *
     * ★ 用途（课题模式互相关定曲首）：`envelope()` 的第 0 帧对应哪个墙钟时刻，
     *   只有知道它才能把"互相关算出的 startMs"换算成绝对零点。
     */
    private volatile long firstFrameMidNs;

    private volatile boolean stopping;
    private volatile String error;
    private volatile boolean recording;

    /**
     * 标定模式：**至少采到这么多帧才停**（0 = 旧行为：门一锁定就停）。
     *
     * ★ 为什么要它（2026-10-25，方案 S1）：曲首 onset 在 `sil_ref` 处就锁定了，可那只是
     *   开唱后的第 0.9 s —— 这点音频做互相关远远不够（峰不唯一）。而普通轮次又不能让采集
     *   继续跑（会和注入并行，污染 `late_ms`）。⇒ 单独一条「只听不打」的标定路径。
     */
    private final int keepFrames;

    /** 每帧的 meanSquare（`OnsetDetector` 的 e）。只在采集线程写，读方等线程结束/帧数够了再读。 */
    private double[] envMs = new double[2048];
    private volatile int envN;

    /** 旧签名：不带母带阈值 ⇒ 相对判据（行为与 10-24 版逐位相同）。 */
    public OnsetTap(Context ctx, int resultCode, Intent resultData) {
        this(ctx, resultCode, resultData, 0.0);
    }

    /**
     * @param silThrRms 母带边沿阈值（`manifest.sil_thr`）。≤0 = 未标定 ⇒ 相对判据
     */
    public OnsetTap(Context ctx, int resultCode, Intent resultData, double silThrRms) {
        this(ctx, resultCode, resultData, silThrRms, 0);
    }

    /**
     * @param keepFrames 标定模式至少采多少帧（0 = 门锁定即停，生产行为不变）
     */
    public OnsetTap(Context ctx, int resultCode, Intent resultData, double silThrRms,
                    int keepFrames) {
        this.app = ctx.getApplicationContext();
        this.resultCode = resultCode;
        this.resultData = resultData;
        if (Double.isNaN(silThrRms) || silThrRms < 0) {
            throw new IllegalArgumentException("silThrRms 非法：" + silThrRms
                    + "（0 = 未标定，正数 = 母带阈值）");
        }
        if (keepFrames < 0) {
            throw new IllegalArgumentException("keepFrames 不能为负：" + keepFrames);
        }
        this.silThrRms = silThrRms;
        this.curSilThrRms = silThrRms;
        this.keepFrames = keepFrames;
    }

    /**
     * 会话模式开关（**必须在 start() 之前调用**）。
     *
     * ★ 为什么需要（课题模式三曲连打）：`loop()` 原来在 gate 触发时 `break`，
     *   采集线程就此结束 —— 第 2 首就没有数据源了；而每首重新 `new OnsetTap` 会再要一次
     *   屏幕录制授权（系统不允许复用 token）。⇒ 会话模式让线程持续待命，用 {@link #rearm}
     *   换阈值，而不是重建采集链。
     */
    public void setSessionMode(boolean on) {
        this.sessionMode = on;
    }

    public boolean sessionMode() {
        return sessionMode;
    }

    /**
     * 换下一首：**重建 gate**（阈值与原点都换）并清零帧计数，采集线程/投影不动。
     *
     * ⚠ 必须在「上一首结束、下一首开唱之前」调用（即 5 秒休息期里）：
     *   采集链有 `c ≈ 177 ms` 滞后，rearm 刚完成时到的帧其实还是上一首的尾巴；
     *   留出这 5 秒，gate 才能先攒够"静音帧"，把真正的曲首边沿认出来。
     *
     * @param silThrRms 这一首的母带阈值（`manifest.sil_thr`）；≤0 = 相对判据
     */
    public void rearm(double silThrRms) {
        rearm(silThrRms, false);
    }

    /**
     * @param armNow true = 建好就强制 ARMED（曲间休息一直在响、等不到静音时用；
     *               误触发由调用方的 `notBefore` 过滤）。正常模式/第 1 首必须传 false。
     */
    public void rearm(double silThrRms, boolean armNow) {
        if (!(silThrRms >= 0)) {
            throw new IllegalArgumentException("rearm 的阈值非法：" + silThrRms);
        }
        this.curSilThrRms = silThrRms;
        final long origin = SystemClock.elapsedRealtimeNanos();
        gate = silThrRms > 0
                ? OnsetGate.withEdge(OnsetDetector.DEFAULT_FRAME_MS, silThrRms, origin)
                : OnsetGate.withDefaults(OnsetDetector.DEFAULT_FRAME_MS, origin);
        if (armNow) {
            gate.forceArm();
        }
        firstFrameMidNs = 0;
        envN = 0;                 // 帧计数/包络按首清零，否则第 2 首的标定窗口混进第 1 首
        rearmGen++;
    }

    /**
     * 等「这一首」的 gate 触发。
     *
     * @param notBeforeNs 早于这个时刻（`elapsedRealtimeNanos`）的触发**不可信**（通常是休息期的
     *                    过场音效）⇒ 丢弃、重新 rearm、继续等。≤0 表示不限。
     * @return true = 采到可信的起音；false = 出错或超时
     */
    public boolean awaitFire(long notBeforeNs, long timeoutMs) {
        return awaitFire(notBeforeNs, timeoutMs, false);
    }

    /**
     * @param armOnDiscard true = 每次重新待命都强制 ARMED（曲间休息一直在响时用）。
     *                     ⚠ 第 1 首**必须传 false**：点 ▶ 之后有一声点击音，
     *                     强制待命会把它当成起音（零点早一整段）。
     */
    public boolean awaitFire(long notBeforeNs, long timeoutMs, boolean armOnDiscard) {
        final long end = System.currentTimeMillis() + timeoutMs;
        long armedSince = System.currentTimeMillis();
        long lastDiag = 0;
        int discards = 0;
        boolean forced = false;
        while (true) {
            if (error != null) return false;
            OnsetGate g = gate;
            final long now = System.currentTimeMillis();
            if (g != null && g.fired()) {
                if (notBeforeNs <= 0 || g.firedAtNs() >= notBeforeNs) return true;
                discards++;
                if (discards <= 3 || discards % 20 == 0) {
                    RunLog.line("会话：丢弃过早触发 #" + discards + "（休息期音效）⇒ 重新待命（gen="
                            + rearmGen + "）");
                }
                rearm(curSilThrRms, armOnDiscard);
                armedSince = now;
                forced = false;
                continue;
            }
            // ★ 等不到静音（过场一直响）时，1.5 s 后强制待命，靠 notBefore 过滤误触发
            if (armOnDiscard && !forced && g != null && !g.armed()
                    && now - armedSince > 1500) {
                g.forceArm();
                forced = true;
                RunLog.line("会话：等不到 300 ms 静音 ⇒ 强制待命（gen=" + rearmGen
                        + "，quietRun=" + g.quietRunNow() + "）");
            }
            if (now - lastDiag > 5000) {
                lastDiag = now;
                RunLog.line(String.format(Locale.ROOT,
                        "会话诊断：armed=%s quietRun=%d frames=%d peak=%.0f 剩余=%.1fs",
                        g != null && g.armed(), g == null ? -1 : g.quietRunNow(), frames(),
                        g == null ? -1 : g.peakMeanSquare(),
                        (end - now) / 1000.0));
            }
            if (now >= end) return false;
            try {
                Thread.sleep(5);
            } catch (InterruptedException ie) {
                Thread.currentThread().interrupt();
                return false;
            }
        }
    }

    /**
     * 建投影 + 建采集流 + 起读线程。**必须在点 ▶ 之前调用**（否则曲首静音段缺失）。
     *
     * @throws IllegalStateException 任何一步不成立（消息已是人话，直接进日志/界面）
     */
    public void start() throws Exception {
        if (projection != null || record != null) {
            throw new IllegalStateException("OnsetTap 已经在跑（不要重复 start）");
        }
        // ★ 只记不拦：音量是诊断量，判据是采集到的样本（见类注释与 OnsetGate.allZero）。
        RunLog.line("媒体音量 " + mediaVolumeNote()
                + "（仅诊断，不参与判定：本机 capture 取音量缩放前，0 档也能录到）");

        // ★ 半途失败必须自己收尸：`OnsetZero.await()` 的 finally 只在 start() **正常返回**
        //   之后才调 stop()，所以这里抛出的任何异常都会让已建好的 MediaProjection /
        //   AudioRecord 泄漏。系统的投影是**独占**的（同一时刻只允许一个），
        //   泄漏一次 ⇒ 下一轮 getMediaProjection 拿不到，且错误信息是
        //   「返回 null / 已被撤销」，完全指不到真正的原因。
        try {
            startInner();
        } catch (Throwable t) {
            try {
                stop();
            } catch (Throwable ignore) {
                // 收尸失败不能盖掉真正的错误
            }
            throw t;
        }
    }

    private void startInner() throws Exception {
        MediaProjectionManager mpm =
                (MediaProjectionManager) app.getSystemService(Context.MEDIA_PROJECTION_SERVICE);
        if (mpm == null) throw new IllegalStateException("拿不到 MediaProjectionManager");
        try {
            projection = mpm.getMediaProjection(resultCode, resultData);
        } catch (SecurityException se) {
            throw new IllegalStateException("getMediaProjection 被拒：" + se.getMessage()
                    + "（Android 14 要求先 startForeground 带 mediaProjection 类型）", se);
        } catch (Throwable t) {
            throw new IllegalStateException("getMediaProjection 抛异常：" + t, t);
        }
        if (projection == null) {
            throw new IllegalStateException("getMediaProjection 返回 null"
                    + "（授权过期/已被撤销 ⇒ 重新点「开始演奏」拿新 token）");
        }
        // ★ handler 必须**显式**给主线程 Looper，不能传 null：
        //   `registerCallback(cb, null)` 会让 MediaProjection 用**当前线程**的 Looper 建 Handler，
        //   而 startInner() 跑在 `pg-play` 线程上（服务里 new Thread 起的），那个线程没有 Looper
        //   ⇒ 抛 `RuntimeException: Can't create handler inside thread ... that has not called
        //   Looper.prepare()`。10-24 第三枪就是在「倒计时结束、采集刚要起」这一刻炸的，
        //   前面 8 s 倒计时全部白等。回调只做一件事（记 error 置 stopping），主线程完全够用。
        projection.registerCallback(new MediaProjection.Callback() {
            @Override
            public void onStop() {
                if (stopping) return;             // 自己停的不算错
                error = "投影授权被系统/用户撤销";
                stopping = true;
            }
        }, new Handler(Looper.getMainLooper()));

        openRecord();

        // ★ gate 必须在 start() 里就建好：等待方可能在读线程起跑之前就调 gate()，
        //   在 loop() 里懒建会让它拿到 null（空指针，且报在看不出原因的时机）。
        // ★ 有母带阈值 ⇒ 用绝对判据（与 `sil_ref` 同口径，FACTS §169）；没有 ⇒ 旧相对判据。
        final long origin = SystemClock.elapsedRealtimeNanos();
        gate = silThrRms > 0
                ? OnsetGate.withEdge(OnsetDetector.DEFAULT_FRAME_MS, silThrRms, origin)
                : OnsetGate.withDefaults(OnsetDetector.DEFAULT_FRAME_MS, origin);

        stopping = false;
        recording = true;
        worker = new Thread(this::loop, "pg-onset");
        worker.start();
    }

    /** 采集是否还在推进（线程活着且没出错）。 */
    public boolean alive() {
        return recording && error == null;
    }

    public OnsetGate gate() {
        OnsetGate g = gate;
        if (g == null) throw new IllegalStateException("还没 start()");
        return g;
    }

    public int rate() {
        return rate;
    }

    /** 供日志：本机采样率档位是否用了 48k（false = 落到 44.1k，多了重采样）。 */
    /** 把 MediaProjection 借给诊断用的抓帧器（同一投影可挂多个 VirtualDisplay）。 */
    public MediaProjection projection() {
        return projection;
    }

    public String rateNote() {
        return rate == 48000 ? "48k（无重采样）" : rate + " Hz（含重采样）";
    }

    /** 供日志：这一轮用哪条边沿判据（口径只允许 `OnsetGate.edgeNote()` 一处产出）。 */
    public String edgeNote() {
        OnsetGate g = gate;
        return g == null ? "未起采集" : g.edgeNote();
    }

    /** 采集线程/投影的失败原因；null = 正常。 */
    public String error() {
        return error;
    }

    /** 停录 + 释放。**幂等**，可从任意线程调（`await()` 的 finally 里调）。 */
    public void stop() {
        stopping = true;
        AudioRecord r = record;
        if (r != null) {
            try {
                if (r.getRecordingState() == AudioRecord.RECORDSTATE_RECORDING) r.stop();
            } catch (Throwable ignore) {
                // 已停/已释放
            }
        }
        Thread w = worker;
        if (w != null && w != Thread.currentThread()) {
            try {
                w.join(600);
            } catch (InterruptedException ie) {
                Thread.currentThread().interrupt();
            }
        }
        if (r != null) {
            try {
                r.release();
            } catch (Throwable ignore) {
                // 释放失败只会占着 HAL，下一轮 start() 会报出来
            }
        }
        record = null;
        MediaProjection p = projection;
        projection = null;
        if (p != null) {
            try {
                p.stop();
            } catch (Throwable ignore) {
                // 已被系统停
            }
        }
        recording = false;
    }

    // ─────────────────────────── 内部 ───────────────────────────

    private void openRecord() {
        AudioPlaybackCaptureConfiguration cfg;
        try {
            cfg = new AudioPlaybackCaptureConfiguration.Builder(projection)
                    .addMatchingUsage(AudioAttributes.USAGE_MEDIA)
                    .addMatchingUsage(AudioAttributes.USAGE_GAME)
                    .addMatchingUsage(AudioAttributes.USAGE_UNKNOWN)
                    .build();
        } catch (Throwable t) {
            throw new IllegalStateException("构造 AudioPlaybackCaptureConfiguration 失败：" + t, t);
        }

        int chosen = -1;
        int minBuf = -1;
        for (int r : CANDIDATE_RATES) {
            int mb = AudioRecord.getMinBufferSize(r, CHANNEL_MASK, ENCODING);
            if (mb > 0) {
                chosen = r;
                minBuf = mb;
                break;
            }
        }
        if (chosen < 0) {
            throw new IllegalStateException("48000/44100 立体声 16bit 都不被支持"
                    + "（getMinBufferSize ≤ 0）");
        }
        rate = chosen;
        // 与 probe 同口径：16384 B = 4096 帧 = 85 ms 上限，每块 2048 帧 ⇒ 留 2 块余量防欠载。
        int bufBytes = Math.max(minBuf * 2, 16384);

        AudioRecord rec;
        try {
            rec = new AudioRecord.Builder()
                    .setAudioFormat(new AudioFormat.Builder()
                            .setSampleRate(chosen)
                            .setChannelMask(CHANNEL_MASK)
                            .setEncoding(ENCODING)
                            .build())
                    .setBufferSizeInBytes(bufBytes)
                    .setAudioPlaybackCaptureConfig(cfg)
                    .build();
        } catch (Throwable t) {
            throw new IllegalStateException("AudioRecord.Builder 抛异常：" + t
                    + "（Android 13 及以下还需要 RECORD_AUDIO 已授予）", t);
        }
        if (rec.getState() != AudioRecord.STATE_INITIALIZED) {
            int st = rec.getState();
            try { rec.release(); } catch (Throwable ignore) { /* 尚未初始化 */ }
            throw new IllegalStateException("AudioRecord 未初始化，state=" + st
                    + "（STATE_UNINITIALIZED = 系统拒绝为该来源建采集流）");
        }
        record = rec;
    }

    /**
     * 媒体音量的诊断文本（如 `0/16`）。**不参与任何判定** —— 只为了让日志能回答
     * 「当时音量是多少」，而不是用它推断「能不能录到」。
     *
     * ⚠ 这个值本身也不可靠（同机同时刻三个口径会打架：`settings get system volume_music`
     * 报 5、`dumpsys audio` 的 `streamVolume` 报 1、logcat 的 `preVol` 报 5）
     * ⇒ 更不该拿它当闸门。
     */
    public String mediaVolumeNote() {
        AudioManager am = (AudioManager) app.getSystemService(Context.AUDIO_SERVICE);
        if (am == null) return "未知（拿不到 AudioManager）";
        try {
            int v = am.getStreamVolume(AudioManager.STREAM_MUSIC);
            int max = am.getStreamMaxVolume(AudioManager.STREAM_MUSIC);
            return v + "/" + max;
        } catch (Throwable t) {
            return "读不出（" + t + "）";
        }
    }

    private void loop() {
        final int blockSamples = BLOCK_FRAMES * CHANNELS;
        final short[] buf = new short[blockSamples];
        final OnsetDetector det = new OnsetDetector(rate);
        final int hop = det.hopSamples();

        // sink 在 push() 内部**同步**回调 ⇒ 用字段把「本块的时间基准」传进去，避免每帧分配。
        final long[] blk = new long[2];   // [0]=tReadNs  [1]=sAccMono（本块读完后）
        det.setSink((frameIdx, e, onset, ratio) -> {
            long mid = FrameClock.frameMidNs(blk[0], blk[1], frameIdx, hop, rate);
            if (firstFrameMidNs == 0) {
                firstFrameMidNs = mid;   // 本段包络的第 0 帧时刻（rearm 后第一帧）
            }
            // ★ 每帧重新读 volatile：会话模式下 `rearm()` 会换 gate，抓局部变量会让新阈值失效。
            OnsetGate g = gate;
            if (g != null) g.onFrame(frameIdx, e, onset, mid);
            recordEnv(e);                 // 标定用：逐帧留底（生产路径只是多一次数组写，可忽略）
        });

        long sAcc = 0;
        try {
            AudioRecord r = record;
            r.startRecording();
            if (r.getRecordingState() != AudioRecord.RECORDSTATE_RECORDING) {
                throw new IllegalStateException("startRecording 后状态 = "
                        + r.getRecordingState());
            }
            while (!stopping) {
                int n = r.read(buf, 0, blockSamples);
                if (n < 0) {
                    throw new IllegalStateException("AudioRecord.read 返回 " + n);
                }
                if (n == 0) continue;
                if (n > blockSamples) {      // 防御：API 语义变了才会发生
                    throw new IllegalStateException("read 返回 " + n + " > 请求 " + blockSamples);
                }
                long tRead = SystemClock.elapsedRealtimeNanos();
                sAcc += n / 2;               // 交错立体声：2 个 short = 1 个单声道样本
                blk[0] = tRead;
                blk[1] = sAcc;
                det.push(buf, 0, n);
                // ★ 标定模式不因 FIRED 而停（要攒够 keepFrames）；生产模式逐位不变。
                if (keepFrames > 0) {
                    if (envN >= keepFrames) break;
                } else if (!sessionMode) {
                    OnsetGate g = gate;
                    if (g != null && g.fired()) {
                        break;               // 已锁定 ⇒ 不再占用采集（省电、早释放）
                    }
                }
            }
        } catch (Throwable t) {
            if (!stopping) error = "采集线程失败：" + t;
        } finally {
            recording = false;
        }
    }

    /** 采集线程专用：逐帧留底（容量翻倍扩容；标定 20 s = 2000 帧，几 KB）。 */
    private void recordEnv(double e) {
        int n = envN;
        if (n == envMs.length) {
            double[] bigger = new double[Math.max(256, n * 2)];
            System.arraycopy(envMs, 0, bigger, 0, n);
            envMs = bigger;
        }
        envMs[n] = e;
        envN = n + 1;
    }

    /** 已采到的帧数（标定路径用它判断「攒够了没」）。 */
    /**
     * 本次 rearm 之后第一帧的中点时刻（ns，单调钟）；0 = 还没收到帧。
     * 与 `envelope()` 的第 0 帧对齐 —— 互相关定曲首要拿它换算绝对零点。
     */
    public long firstFrameMidNs() {
        return firstFrameMidNs;
    }

    public int frames() {
        return envN;
    }

    /**
     * 本轮采到的 meanSquare 包络副本（前 {@link #frames()} 帧）。
     *
     * ⚠ 只在采集停止后（或 {@link #frames()} 已达标）调用；采集线程还在写时读到的可能是半截。
     */
    public double[] envelope() {
        int n = envN;
        double[] out = new double[n];
        System.arraycopy(envMs, 0, out, 0, n);
        return out;
    }

    /**
     * 等到采够 {@code n} 帧（或超时/出错）。返回**实际**帧数。标定路径在 onset 锁定后用。
     *
     * ★ 不抛异常：调用方拿到帧数自己判（标定缺帧仍可落盘，只是要如实报告采到多少）。
     */
    public int awaitFrames(int n, long timeoutMs) {
        long end = System.currentTimeMillis() + timeoutMs;
        while (envN < n) {
            if (error != null) break;
            if (System.currentTimeMillis() >= end) break;
            try {
                Thread.sleep(20);
            } catch (InterruptedException ie) {
                Thread.currentThread().interrupt();
                break;
            }
        }
        return envN;
    }
}
