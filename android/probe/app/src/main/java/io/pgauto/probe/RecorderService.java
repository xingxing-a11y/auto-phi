package io.pgauto.probe;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.media.AudioAttributes;
import android.media.AudioFormat;
import android.media.AudioPlaybackCaptureConfiguration;
import android.media.AudioRecord;
import android.media.AudioTimestamp;
import android.media.projection.MediaProjection;
import android.media.projection.MediaProjectionManager;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.os.SystemClock;
import android.util.Log;

import java.io.BufferedWriter;
import java.io.File;
import java.io.FileOutputStream;
import java.io.OutputStreamWriter;
import java.io.RandomAccessFile;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.nio.charset.StandardCharsets;
import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.Locale;
import java.util.concurrent.ConcurrentLinkedQueue;

/**
 * 前台服务：用 AudioPlaybackCapture 抓其它应用（Phigros）的播放音频。
 *
 * 为什么要 Service 而不是在 Activity 里录：
 *   Phigros 一启动，本 app 就退到后台；Activity 后台的 AudioRecord 会被加严限制，
 *   而且 Android 14+ 规定 MediaProjection 必须在**已启动的前台服务**里用。
 *
 * 关键顺序（Android 14+ 强制，写反就抛异常）：
 *   1. startForeground(type = FOREGROUND_SERVICE_TYPE_MEDIA_PROJECTION)
 *   2. MediaProjectionManager.getMediaProjection(resultCode, data)
 *   3. projection.registerCallback(...)   ← 不注册会在开始采集时抛 IllegalStateException
 *   4. 建 AudioRecord（带 AudioPlaybackCaptureConfiguration）
 *
 * ★ 2026-10-18 第二版：除 wav 外**再落一个 sidecar `.tsv`**（时间戳 + 10 ms 能量包络 + onset）。
 *   动机见 `FACTS §82 §三`：「PCM 可用」已证，但**延迟未测**、**曲线内有近乎逐样本相同的段落
 *   ⇒ 全局 argmax 会锁错 43.1 s**。仅凭 wav 事后做互相关，解不了这两个问题（wav 里没有钟）。
 *
 *   sidecar 行的语义（制表符分隔，`#` 开头为注释）：
 *     M   元信息（一次）：rate / ch / hop / blockFrames / bufBytes / mono0 / epoch0 / ver
 *     B   每块一行：块号 / 累计样本 / 读返回时刻 / getTimestamp 是否成功 / 硬件帧位置 / 硬件时刻
 *     E   每 10 ms 一行：帧号 / 推算的帧末时刻 / 能量（rms×10，整数）
 *     O   每个 onset 一行：帧号 / 帧末时刻 / 能量 / 能量比
 *     P   标定脉冲（v2 新增）：
 *           `P S 0 <帧数> <音量> <音量上限> <tPlay> [pre=..,wr=..]`      播放提交
 *           `P T <轮询序> <tReadPlay> <hwOk> <framePos> <hwNs>`           每 5 ms 一条
 *           `P E 0 <末帧位置> <tReadPlay> tried=..,ok=..,vol=../..`       结束
 *         用途：`AudioTrack.getTimestamp()` 给出**播放侧**的 (帧, 时刻)，
 *         与录音侧的 `B` 行对称 ⇒ 两端都在 elapsedRealtime 轴上，可直接相减得到 `d1+d2`。
 *         ⛔ 不要用 host 发 adb 的时刻当「已知播放时刻」：那条路 30~100 ms 且抖动，
 *            与被测量同量级（`.workbuddy/plans/2026-10-18-d1-pulse-probe.md` §一）。
 *
 *   ★ 为什么 E 行的时间戳是**从块末往回推算**而不是「回调那一刻取 now」：
 *     一个 2048 帧的块里有 4~5 个 10 ms 帧，它们在 push() 内部被连续 flush，
 *     若在回调里取 `elapsedRealtimeNanos()`，5 个帧会拿到几乎同一时刻 ⇒ 时间戳分辨率
 *     退化成 46 ms 的块粒度，包络对齐就白做了。正确做法：
 *       frameEndTs = tRead − (块末累计样本 − (frameIdx+1)·hop) / rate × 1e9
 */
public class RecorderService extends Service {

    public static final String TAG = "pgprobe";
    public static final String EXTRA_RESULT_CODE = "resultCode";
    public static final String EXTRA_RESULT_DATA = "resultData";

    /** 覆盖 AudioRecord 申请的缓冲字节数（0 = 用默认 `max(minBuf*2, 16384)`）。
     *
     *  ★ 2026-10-19 新加：`d_total ≈ 200 ms` 里「AudioRecord 申请了 16384 B」是最可疑、
     *    也最容易实验的一项（`out/d1_chirp_probe_report.md §四`）。做成**运行期参数**
     *    而不是改常量重编译，是为了让「同一份 APK、同一次会话」里能把 16384 / 8192 / 4096
     *    连着跑完 —— 否则每次改值都要重装，而重装本身会换掉 HAL 会话，混进新变量。
     */
    public static final String EXTRA_BUF_BYTES = "bufBytes";

    /** 覆盖单次录制上限（秒，0 = 用 300）。标定轮只需 12~20 s，自动停可省一次 UI 点击，
     *  也避免「忘记点停止 ⇒ 录到 300 s 才发现」。
     */
    public static final String EXTRA_MAX_SECONDS = "maxSeconds";
    /** 采样率（44100 / 48000）。0 = 默认 44100。 */
    public static final String EXTRA_RATE = "rate";

    /**
     * ★ 诊断：在第 N 块**之前**人为停顿 `read()`，停顿期间反复取 `getTimestamp`。
     *
     * 目的 = 判定 `AudioTimestamp.framePosition` 的语义（这是 FACTS 里挂了很久的待查项）：
     *   · 若停顿期间 `framePosition` **继续按 rate 增长** ⇒ 它是**硬件采集位置**（独立推进）
     *   · 若停顿期间 **不动** ⇒ 它是「已交付给 App 的位置」（跟 read 绑定）
     *
     * 为什么必须这样做：`hwFrame` 与 `sCum` 的差（积压）是 `d_total` 不确定度的唯一来源；
     *   只有确认它是不是采集位置，才知道 `t0_B = tRead − hwFrame/rate` 能不能当真值。
     * 为什么不用播放声音：语义判定与「有没有声音」无关（§88 已证积压与内容无关），
     *   静音采集照样能测，且不打扰用户。
     */
    public static final String EXTRA_STALL_AT = "stallAtBlock";
    /** 停顿期间的探针次数（每 200 ms 一次）。0 = 不停顿。 */
    public static final String EXTRA_STALL_PROBES = "stallProbes";

    private static final String CH_ID = "pgprobe_rec";
    private static final int NOTIF_ID = 0x1001;

    /** 采样参数：与 Phigros 的音乐文件常见格式一致，便于与 samples/…/music.wav 直接互相关
     *
     *  ★ 2026-10-19 19:3x：`static final` → **运行期可改**（`EXTRA_RATE`）。
     *    理由是查到了静态事实：`dumpsys media.audio_flinger` 里所有 MIXER 输出线程
     *    都是 **48000 Hz**（`AudioOut_1D` HAL frame count 1920 = 40 ms，
     *    `AudioIn_1B6` = 940 帧），而本服务历来按 44100 申请 ⇒ **采集侧必过一次
     *    48k→44.1k 重采样**。要判「重采样是不是 d_total 的一项」，必须在同一次安装里
     *    换 rate（重装会换掉整个 HAL 会话，把重装效应混进来）。
     */
    private int rate = 44100;
    private static final int CHANNEL_MASK = AudioFormat.CHANNEL_IN_STEREO;
    private static final int ENCODING = AudioFormat.ENCODING_PCM_16BIT;
    private static final int CHANNELS = 2;

    /** 单次录制上限（秒），防用户忘记点停止把手机存满。
     *
     *  ★ 2026-10-18 21:2x：180 → 300。理由（实测预算，不是猜）：
     *  「双链对拍」轮要手动开录 → 切游戏 → 停在就绪界面 → 等人核对 → 开火 → 打 139 s
     *  → 等 full_run 判读完。开录到音乐零点这段人为等待不可压缩（授权弹窗必须手点）。
     *  按 180 s 算，留给「开录→开火」的窗口只剩 ~25 s，一个授权弹窗就吃光 ⇒
     *  录制会在打歌中途自动停，整轮白费。
     *  代价：满 300 s 的 wav = 44100×2ch×2B×300 ≈ 52.9 MB，可接受（PC 端拉完即清设备）。
     */
    private static final int MAX_SECONDS = 300;

    /** sidecar 格式版本；PC 端解析时校验，改了格式就必须加一。
     *
     *  v2（2026-10-18 22:2x）：新增 `P` 行（标定脉冲的播放侧时间戳）。
     *  ⛔ 改了格式却不加版本号，PC 端解析器会「按旧格式读新文件」——不会报错，只会静默丢字段。
     */
    public static final int SIDECAR_VERSION = 2;

    // ── 供 Activity 轮询的状态（探针求简单，不用广播 / 不用 Bind）────────────
    public static volatile boolean sRunning = false;
    public static volatile int sPeak = 0;          // 最近 100 ms 的峰值幅度（0~32767）
    /**
     * 整轮录制期间的最大峰值。★ 2026-10-18 首次真机跑暴露的缺陷：
     * 只看 sPeak 会得到「峰值 0」的假阴性 —— 它是 100 ms 滑窗值，而切回本应用时
     * 游戏已经暂停（后台不出声）⇒ 回来读到的必然是 0。判读必须用这个整轮最大值。
     */
    public static volatile int sPeakMax = 0;
    public static volatile long sFrames = 0;       // 已写入的帧数
    public static volatile String sPath = "";      // 输出文件绝对路径
    public static volatile String sTsPath = "";    // sidecar 绝对路径
    public static volatile String sError = "";     // 失败原因（含 AudioRecord 状态码）
    /**
     * 非错误类提示（例如「已满 300 s 上限自动停止」）。
     * <p>与 sError 分开的理由：2026-10-18 真机跑满 300 s 时，界面把自动停止显示成
     * 「✗ 投影授权被系统/用户停止」—— 数据其实完整，却让人以为是链路崩了。
     * 撞上限是设计行为，不该占用错误通道。
     */
    public static volatile String sNotice = "";
    /**
     * 主动停止中。★ 2026-10-18 实测补：`projection.stop()` 会**同步回调** onStop()，
     * 于是「用户自己点停止」也被记成「授权被系统/用户停止」——截图里那行红字就是误报。
     * 主动停时不设错误，只有外部撤销（快捷设置面板里撤权）才设。
     */
    private static volatile boolean selfStopping = false;

    // ── 第二版新增：时间戳 / onset 观测 ──────────────────────────────────────
    /** 累计 onset 数（整轮，停止后不清零 —— 判读靠它，同 sPeakMax 的理由）。 */
    public static volatile int sOnsets = 0;
    /** 最近一次 onset 相对开录的毫秒数；-1 = 还没有。 */
    public static volatile long sLastOnsetMs = -1;
    /** `getTimestamp` 成功次数 / 总块数 —— 硬件时间戳在本机到底能不能用，这是唯一判据。 */
    public static volatile long sTsOk = 0;
    public static volatile long sTsTried = 0;
    /** 最近一次「应用可读时刻 − 硬件采集时刻」，微秒；-1 = 无效。这是**采集链路延迟的直接测量**。 */
    public static volatile long sReadLatencyUs = -1;
    /**
     * 缓冲积压（毫秒）= 硬件已采集总帧数 − 应用已读帧数。
     * ★ 2026-10-18 实测：`tRead − hwNs` 的**中位数**是「两钟基准差」（跨两轮只差 14 μs），
     * 不是延迟 —— 直接显示会得到 65569507 ms 这种荒谬值（见 sReadLatencyUs）。
     * 真正可解释的延迟量是这个积压：实测中位 69.8~83.1 ms、P5~P95 46.6~93.1 ms。
     */
    public static volatile long sBacklogMs = -1;
    /** 实际申请的 AudioRecord 缓冲字节数（延迟的头号来源，必须显示出来）。 */
    public static volatile int sBufBytes = 0;
    /** `AudioRecord.getMinBufferSize` 的返回值（本轮臂是否已低于系统下限的证据）。 */
    public static volatile int sMinBuf = 0;

    // ── v2：标定脉冲 ────────────────────────────────────────────────────────
    /** 本轮播放过的脉冲串个数。 */
    public static volatile int sPulseCount = 0;
    /** 最近一次播放的结果串（成功摘要或失败原因），直接给 UI 显示。 */
    public static volatile String sPulseState = "";

    /**
     * 脉冲 sidecar 行队列：生产者 = 播放线程，消费者 = 录制线程。
     *
     * <p>★ 为什么过队列而不是让播放线程自己写文件：`tsOut`（BufferedWriter）由录制线程独占，
     * 两个线程往同一个 writer 写会交错出半行 —— 解析器会直接崩，而且崩在「数据看起来都在」
     * 的时候最难查。队列让「谁写文件」保持唯一。
     */
    private static final ConcurrentLinkedQueue<String> pulseQueue = new ConcurrentLinkedQueue<>();

    private final Handler ui = new Handler(Looper.getMainLooper());
    private volatile boolean stopFlag = false;
    /** 本轮申请的 AudioRecord 缓冲字节（0 = 用默认口径）。写入 M 行 `bufBytes=`，PC 端据此分臂。 */
    private int reqBufBytes = 0;
    /** 本轮单次录制上限（秒）。写入 M 行 `maxS=`。 */
    private int maxSeconds = MAX_SECONDS;
    /** 诊断：在第几块之前停顿（0 = 不停顿）。写入 M 行 `stallAt=`。 */
    private int stallAt = 0;
    /** 诊断：停顿期间取几次 getTimestamp（每 200 ms 一次）。写入 M 行 `stallN=`。 */
    private int stallProbes = 0;
    private Thread worker;
    private AudioRecord record;
    private MediaProjection projection;
    private File outFile;
    private File tsFile;

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent == null) {
            stopSelf();
            return START_NOT_STICKY;
        }
        sError = "";
        sNotice = "";
        // ★ 每一轮开始都要把 selfStopping 清掉。它是 static：上一轮 onDestroy() 把它置 true，
        //   若不复位，下一次「用户在快捷设置面板里撤销授权」就静默不报了（反向漏报）。
        selfStopping = false;

        // 运行期参数（0 = 用默认）。夹在合理区间：bufBytes 太小必然欠载，
        //   maxSeconds 太大只会占手机存储。
        int rb = intent.getIntExtra(EXTRA_BUF_BYTES, 0);
        reqBufBytes = rb >= 4096 && rb <= 262144 ? rb : 0;
        int ms = intent.getIntExtra(EXTRA_MAX_SECONDS, 0);
        maxSeconds = ms >= 5 && ms <= MAX_SECONDS ? ms : MAX_SECONDS;
        // ★ 只允许设备真实支持的值。乱传一个 96000 会被 HAL 静默重采样，
        //   那这一臂就变成「测了另一个未知配置」，比不复核更危险。
        int rq = intent.getIntExtra(EXTRA_RATE, 0);
        rate = (rq == 48000 || rq == 44100 || rq == 32000) ? rq : 44100;
        // 诊断停顿：块号夹在 1~10000，探针数夹在 0~40（40 × 200 ms = 8 s，够看趋势）
        int sa = intent.getIntExtra(EXTRA_STALL_AT, 0);
        stallAt = sa >= 1 && sa <= 10000 ? sa : 0;
        int sp = intent.getIntExtra(EXTRA_STALL_PROBES, 0);
        stallProbes = stallAt > 0 && sp >= 0 && sp <= 40 ? sp : 0;
        Log.i(TAG, "reqBufBytes=" + reqBufBytes + " maxSeconds=" + maxSeconds
                + " rate=" + rate + " stallAt=" + stallAt + " stallProbes=" + stallProbes);
        sPeak = 0;
        sPeakMax = 0;
        sFrames = 0;
        sOnsets = 0;
        sLastOnsetMs = -1;
        sTsOk = 0;
        sTsTried = 0;
        sReadLatencyUs = -1;
        sBacklogMs = -1;
        sPulseCount = 0;
        sPulseState = "";
        pulseQueue.clear();          // 上一轮没写完的行不能串到这一轮

        // ① 先起前台服务（必须先于 getMediaProjection）
        startForegroundCompat();

        int code = intent.getIntExtra(EXTRA_RESULT_CODE, 0);
        // ★ 不能用 API 33 的 getParcelableExtra(name, Class)：minSdk 是 29
        @SuppressWarnings("deprecation")
        Intent data = intent.getParcelableExtra(EXTRA_RESULT_DATA);
        if (data == null) {
            fail("拿不到 MediaProjection 授权数据（data == null）");
            return START_NOT_STICKY;
        }

        // ② 取 MediaProjection 并注册回调
        MediaProjectionManager mpm =
                (MediaProjectionManager) getSystemService(Context.MEDIA_PROJECTION_SERVICE);
        try {
            projection = mpm.getMediaProjection(code, data);
        } catch (Throwable t) {
            fail("getMediaProjection 抛异常：" + t);
            return START_NOT_STICKY;
        }
        if (projection == null) {
            fail("getMediaProjection 返回 null（授权被撤销？）");
            return START_NOT_STICKY;
        }
        projection.registerCallback(new MediaProjection.Callback() {
            @Override
            public void onStop() {
                // 主动 stop() 触发的回调不算错（见 selfStopping 注释）
                if (selfStopping) {
                    Log.i(TAG, "MediaProjection stopped (self-initiated)");
                    return;
                }
                // 用户在快捷设置里撤了授权 ⇒ 立刻收尾，别继续写静音
                Log.w(TAG, "MediaProjection stopped by user/system");
                stopFlag = true;
                sError = "投影授权被系统/用户停止";
            }
        }, ui);

        // ③ 建 AudioRecord
        AudioPlaybackCaptureConfiguration cfg;
        try {
            cfg = new AudioPlaybackCaptureConfiguration.Builder(projection)
                    .addMatchingUsage(AudioAttributes.USAGE_MEDIA)
                    .addMatchingUsage(AudioAttributes.USAGE_GAME)
                    .addMatchingUsage(AudioAttributes.USAGE_UNKNOWN)
                    .build();
        } catch (Throwable t) {
            fail("构造 AudioPlaybackCaptureConfiguration 失败：" + t);
            return START_NOT_STICKY;
        }

        int minBuf = AudioRecord.getMinBufferSize(rate, CHANNEL_MASK, ENCODING);
        if (minBuf <= 0) {
            fail("getMinBufferSize 返回 " + minBuf + "（参数不被支持）");
            return START_NOT_STICKY;
        }
        // ★ 缓冲从 `max(minBuf*4, 32768)` 改成 `max(minBuf*2, 16384)`。
        //   原来 32768 B = 8192 帧 = **186 ms 的积压上限**，是采集延迟的头号嫌疑（FACTS §82 §三）。
        //   现在 16384 B = 4096 帧 = 93 ms；每块仍读 2048 帧（46 ms），留 2 块余量防欠载。
        //   ⚠ 欠载会让 samplesCum 与帧号脱节 —— 所以 sidecar 每块都记累计样本数，
        //     PC 端可据此检测丢帧（`E` 帧间隔 ≠ 10 ms 就是欠载证据）。
        // ★ 2026-10-19：默认口径不变，但允许 `EXTRA_BUF_BYTES` 覆盖 —— 只为做
        //   「缓冲容量是不是 d_total 的一部分」这一个单变量实验。下限 4096 B
        //   （= 1024 帧 = 23 ms，仍 ≥ minBuf）防止把缓冲设到必然欠载的值。
        int bufBytes = reqBufBytes > 0 ? reqBufBytes : Math.max(minBuf * 2, 16384);
        sBufBytes = bufBytes;
        // ★ 把 minBuf 也记下来：若某臂把缓冲压到 minBuf 以下，欠载是**预期**的，
        //   不记这个值就没法区分「缓冲容量无关」和「缓冲太小已经坏了」。
        sMinBuf = minBuf;
        Log.i(TAG, "minBuf=" + minBuf + " bufBytes=" + bufBytes);
        try {
            record = new AudioRecord.Builder()
                    .setAudioFormat(new AudioFormat.Builder()
                            .setSampleRate(rate)
                            .setChannelMask(CHANNEL_MASK)
                            .setEncoding(ENCODING)
                            .build())
                    .setBufferSizeInBytes(bufBytes)
                    .setAudioPlaybackCaptureConfig(cfg)
                    .build();
        } catch (Throwable t) {
            fail("AudioRecord.Builder 抛异常：" + t);
            return START_NOT_STICKY;
        }
        if (record.getState() != AudioRecord.STATE_INITIALIZED) {
            // ★ 这一条就是「能不能捕获」的直接证据：STATE_UNINITIALIZED = 系统拒绝了
            fail("AudioRecord 未初始化，state=" + record.getState()
                    + "（STATE_UNINITIALIZED 说明系统拒绝为该来源建采集流）");
            return START_NOT_STICKY;
        }

        // ④ 输出文件
        File dir = getExternalFilesDir(null);
        if (dir == null) dir = getFilesDir();
        //noinspection ResultOfMethodCallIgnored
        dir.mkdirs();
        String stamp = new SimpleDateFormat("yyyyMMdd_HHmmss", Locale.US).format(new Date());
        outFile = new File(dir, "rec_" + stamp + ".wav");
        tsFile = new File(dir, "rec_" + stamp + ".tsv");
        sPath = outFile.getAbsolutePath();
        sTsPath = tsFile.getAbsolutePath();

        stopFlag = false;
        sRunning = true;
        worker = new Thread(this::loop, "pgprobe-rec");
        worker.start();
        return START_NOT_STICKY;
    }

    /**
     * 播放一遍标定脉冲串（异步，立即返回）。只在录制中才有意义 ——
     * 脉冲必须落在同一份录音里，才能和录音侧的 `B` 行对上。
     */
    public static void playPulse(Context ctx) {
        if (!sRunning) {
            sPulseState = "未在录制 —— 脉冲必须与录音同轮才有意义";
            return;
        }
        sPulseCount++;
        sPulseState = "播放中…";
        final Context app = ctx.getApplicationContext();
        new Thread(() -> {
            String r = PulsePlayer.play(app, pulseQueue::add);
            sPulseState = r;
            Log.i(TAG, "playPulse: " + r);
        }, "pgprobe-pulse").start();
    }

    private void loop() {
        final int blockFrames = 2048;
        final int blockSamples = blockFrames * CHANNELS;   // 每次读 2048 帧 = 46.4 ms
        short[] buf = new short[blockSamples];
        RandomAccessFile raf = null;
        BufferedWriter tsOut = null;
        long dataBytes = 0;
        int lastPeak = 0;
        long lastUi = 0;
        long lastHeader = 0;      // 上次回填 wav 头的时刻

        // ── 每块待贴时间戳的帧（一个块最多 5 帧，16 够）──────────────────────
        final int CAP = 32;
        final long[] pendFrame = new long[CAP];
        final double[] pendE = new double[CAP];
        final boolean[] pendOn = new boolean[CAP];
        final double[] pendRatio = new double[CAP];
        final int[] pendN = {0};

        OnsetDetector det = new OnsetDetector(rate);
        det.setSink((frameIdx, e, onset, ratio) -> {
            int k = pendN[0];
            if (k < CAP) {
                pendFrame[k] = frameIdx;
                pendE[k] = e;
                pendOn[k] = onset;
                pendRatio[k] = ratio;
                pendN[0] = k + 1;
            }
        });
        final int hop = det.hopSamples();

        AudioTimestamp hwTs = new AudioTimestamp();
        long sCum = 0;              // 已读样本数（单声道等效）
        long blk = 0;
        long blockedWrites = 0;     // sidecar 写失败计数（不能静默）
        // ★ 停顿只做一次。`blk` 在写 B 行时才自增，所以必须在停顿后自己置位，
        //   否则 `blk == stallAt` 会连续为真、每块都停 200 ms（把整轮数据毁掉）。
        boolean stalled = false;

        try {
            raf = new RandomAccessFile(outFile, "rw");
            raf.write(wavHeader(0, rate));                   // 先占位，结束后回填

            tsOut = new BufferedWriter(new OutputStreamWriter(
                    new FileOutputStream(tsFile), StandardCharsets.UTF_8), 1 << 16);
            tsOut.write("#pgts\tv" + SIDECAR_VERSION + "\n");

            record.startRecording();
            if (record.getRecordingState() != AudioRecord.RECORDSTATE_RECORDING) {
                fail("startRecording 后状态 = " + record.getRecordingState() + "（未在录制）");
                return;
            }

            long mono0 = SystemClock.elapsedRealtimeNanos();
            long epoch0 = System.currentTimeMillis();
            tsOut.write("M\trate=" + rate + "\tch=" + CHANNELS + "\thop=" + hop
                    + "\tblockFrames=" + blockFrames + "\tbufBytes=" + sBufBytes
                    + "\tminBuf=" + sMinBuf + "\tmaxS=" + maxSeconds
                    + "\tstallAt=" + stallAt + "\tstallN=" + stallProbes
                    + "\tmono0=" + mono0 + "\tepoch0=" + epoch0 + "\n");
            tsOut.flush();

            while (!stopFlag) {
                // ★★ 诊断停顿（只在 `--ei stallAtBlock N` 时触发，生产路径 blk/stallAt 不匹配 ⇒ 零影响）。
                //   关键设计：**停顿期间不 read**，但**每 200 ms 仍取一次 getTimestamp**。
                //   于是「framePosition 会不会自己往前跑」成为判定语义的唯一判据：
                //     跑 ⇒ 硬件在采集（位置与 read 无关）
                //     不跑 ⇒ 只是「已交付」位置（与 read 绑定）
                //   ⚠ `blk` 是本方法里已有的循环计数器（在写 B 行时 `blk++`），不要重复定义。
                if (stallAt > 0 && !stalled && blk == stallAt) {
                    stalled = true;
                    long tStall0 = SystemClock.elapsedRealtimeNanos();
                    for (int q = 0; q < stallProbes && !stopFlag; q++) {
                        long tNow = SystemClock.elapsedRealtimeNanos();
                        boolean okP = false;
                        long fp = -1, ns = -1;
                        try {
                            okP = record.getTimestamp(hwTs, AudioTimestamp.TIMEBASE_MONOTONIC)
                                    == AudioRecord.SUCCESS;
                            if (okP) {
                                fp = hwTs.framePosition;
                                ns = hwTs.nanoTime;
                            }
                        } catch (Throwable ignored) {
                        }
                        // P 行：块号 / 探针时刻(mono) / ok / framePosition / nanoTime / sCum / 停顿内偏移µs
                        tsOut.write("P\t" + blk + "\t" + tNow + "\t" + (okP ? 1 : 0)
                                + "\t" + fp + "\t" + ns + "\t" + sCum
                                + "\t" + (tNow - tStall0) / 1000 + "\n");
                        tsOut.flush();
                        try {
                            Thread.sleep(200);
                        } catch (InterruptedException ie) {
                            break;
                        }
                    }
                }
                int n = record.read(buf, 0, buf.length);
                if (n <= 0) {
                    if (n == AudioRecord.ERROR_DEAD_OBJECT || n == AudioRecord.ERROR_INVALID_OPERATION) {
                        sError = "read 返回错误码 " + n;
                        break;
                    }
                    continue;
                }
                // ★ 只能在 read 返回后才取时刻：read 是阻塞的，返回时刻 ≈ 这 n 个样本已进应用缓冲
                long tRead = SystemClock.elapsedRealtimeNanos();

                // ① 硬件时间戳（能不能用，本机只有真机跑一次才知道）
                boolean hwOk = false;
                long hwFrame = -1, hwNs = -1;
                sTsTried++;
                try {
                    if (record.getTimestamp(hwTs, AudioTimestamp.TIMEBASE_MONOTONIC)
                            == AudioRecord.SUCCESS) {
                        hwOk = true;
                        hwFrame = hwTs.framePosition;
                        hwNs = hwTs.nanoTime;
                        sTsOk++;
                        sReadLatencyUs = (tRead - hwNs) / 1000;
                    }
                } catch (Throwable ignored) {
                    // 某些机型 / 来源不支持；不致命，退化到「块末推算」
                }

                // ② 下混 + 包络 + onset
                sCum += n / CHANNELS;
                if (hwOk) sBacklogMs = (hwFrame - sCum) * 1000L / rate;
                pendN[0] = 0;
                det.push(buf, n);
                int done = pendN[0];
                for (int k = 0; k < done; k++) {
                    long f = pendFrame[k];
                    long frameEndSample = (f + 1) * hop;
                    long back = sCum - frameEndSample;
                    if (back < 0) back = 0;               // 理论不会发生；发生即上游帧计数错
                    long tsNs = tRead - back * 1_000_000_000L / rate;
                    long rms10 = Math.round(Math.sqrt(Math.max(pendE[k], 0)) * 10);
                    tsOut.write("E\t");
                    tsOut.write(Long.toString(f));
                    tsOut.write('\t');
                    tsOut.write(Long.toString(tsNs));
                    tsOut.write('\t');
                    tsOut.write(Long.toString(rms10));
                    tsOut.write('\n');
                    if (pendOn[k]) {
                        sOnsets++;
                        sLastOnsetMs = (tsNs - mono0) / 1_000_000L;
                        tsOut.write("O\t");
                        tsOut.write(Long.toString(f));
                        tsOut.write('\t');
                        tsOut.write(Long.toString(tsNs));
                        tsOut.write('\t');
                        tsOut.write(Long.toString(Math.round(pendE[k])));
                        tsOut.write('\t');
                        tsOut.write(String.format(Locale.US, "%.3f", pendRatio[k]));
                        tsOut.write('\n');
                    }
                }
                tsOut.write("B\t");
                tsOut.write(Long.toString(blk++));
                tsOut.write('\t');
                tsOut.write(Long.toString(sCum));
                tsOut.write('\t');
                tsOut.write(Long.toString(tRead));
                tsOut.write('\t');
                tsOut.write(hwOk ? '1' : '0');
                tsOut.write('\t');
                tsOut.write(Long.toString(hwFrame));
                tsOut.write('\t');
                tsOut.write(Long.toString(hwNs));
                tsOut.write('\n');

                // P 行（标定脉冲）：播放线程生产、本线程消费。每块 drain 一次，
                // 最多延迟一块（46 ms）落盘 —— 行内容自带时刻，落盘延迟不影响判读。
                for (String pl = pulseQueue.poll(); pl != null; pl = pulseQueue.poll()) {
                    tsOut.write(pl);
                    tsOut.write('\n');
                }

                // ③ PCM 落 wav
                byte[] bytes = new byte[n * 2];
                int peak = 0;
                for (int i = 0; i < n; i++) {
                    short v = buf[i];
                    bytes[i * 2] = (byte) (v & 0xFF);
                    bytes[i * 2 + 1] = (byte) ((v >> 8) & 0xFF);
                    int a = Math.abs(v);
                    if (a > peak) peak = a;
                }
                raf.write(bytes);
                dataBytes += bytes.length;
                sFrames += n / CHANNELS;
                if (peak > lastPeak) lastPeak = peak;
                if (peak > sPeakMax) sPeakMax = peak;

                long now = System.currentTimeMillis();
                // ★ 每 2 s 把 data 长度回填进 wav 头。
                //   为什么必须定期回填：进程被系统回收时 finally 不会执行，
                //   头里留着占位的 0 ⇒ 文件虽然有几 MB 却是「0 帧」的废 wav。
                //   （2026-10-17 自测实际踩到：文件 9 MB 但 wave 读出来 frames=0。）
                if (now - lastHeader >= 2000) {
                    raf.seek(0);
                    raf.write(wavHeader(dataBytes, rate));
                    raf.seek(44 + dataBytes);
                    tsOut.flush();      // 与 wav 头同频刷盘：进程被杀也留得下
                    lastHeader = now;
                }
                if (now - lastUi >= 100) {
                    sPeak = lastPeak;
                    lastPeak = 0;
                    lastUi = now;
                }
                if (sFrames / rate >= maxSeconds) {
                    // ★ 撞上限是设计行为，不是故障：
                    //   ① 先置 selfStopping，否则下面 projection.stop() 同步回调 onStop()
                    //      会把 sError 写成「投影授权被系统/用户停止」（2026-10-18 实测踩到）；
                    //   ② 用 sNotice 而非 sError —— 数据是完整的，不该打红叉吓人。
                    selfStopping = true;
                    sNotice = "已满 " + maxSeconds + " s 上限自动停止（数据完整）";
                    break;
                }
            }
        } catch (Throwable t) {
            sError = "录制线程异常：" + t;
            Log.e(TAG, "loop", t);
        } finally {
            try {
                if (record != null) {
                    record.stop();
                    record.release();
                }
            } catch (Throwable ignored) { }
            record = null;
            try {
                if (tsOut != null) {
                    // 停止那一刻队列里可能还剩几条 P 行（播放线程刚 push 完、本线程已退出循环）
                    // —— 不在这里 drain 就会静默丢掉「播放结束」那几行。
                    for (String pl = pulseQueue.poll(); pl != null; pl = pulseQueue.poll()) {
                        tsOut.write(pl);
                        tsOut.write('\n');
                    }
                    tsOut.write("Z\tsamples=" + sCum + "\tframes=" + det.frameIndex()
                            + "\tonsets=" + sOnsets + "\tblocks=" + blk
                            + "\thwOk=" + sTsOk + "/" + sTsTried
                            + "\tpulses=" + sPulseCount + "\n");
                    tsOut.flush();
                    tsOut.close();
                }
            } catch (Throwable t) {
                blockedWrites++;
                sError = (sError.isEmpty() ? "" : sError + "; ") + "收尾 sidecar 失败：" + t;
            }
            try {
                if (raf != null) {
                    raf.seek(0);
                    raf.write(wavHeader(dataBytes, rate));
                    raf.close();
                }
            } catch (Throwable t) {
                sError = (sError.isEmpty() ? "" : sError + "; ") + "回填 wav 头失败：" + t;
            }
            if (blockedWrites > 0) {
                Log.w(TAG, "sidecar 写失败 " + blockedWrites + " 次");
            }
            try {
                if (projection != null) projection.stop();
            } catch (Throwable ignored) { }
            projection = null;
            sRunning = false;
            sPeak = 0;
            ui.post(() -> {
                stopForeground(STOP_FOREGROUND_REMOVE);
                stopSelf();
            });
        }
    }

    /** 44 字节标准 PCM wav 头。dataLen 传 0 时先占位，结束后回填。
     *
     *  ★ rate 必须由调用方给：本类改为运行期可换采样率后，静态方法拿不到实例字段，
     *    而 WAV 头的 `fmt ` 段写错会让下游把 48k 当 44.1k 解析 ⇒ 时间轴整体偏 8.8%。
     */
    private static byte[] wavHeader(long dataLen, int rate) {
        int blockAlign = CHANNELS * 2;
        long byteRate = (long) rate * blockAlign;
        ByteBuffer b = ByteBuffer.allocate(44).order(ByteOrder.LITTLE_ENDIAN);
        b.put("RIFF".getBytes());
        b.putInt((int) (36 + dataLen));
        b.put("WAVE".getBytes());
        b.put("fmt ".getBytes());
        b.putInt(16);
        b.putShort((short) 1);              // PCM
        b.putShort((short) CHANNELS);
        b.putInt(rate);
        b.putInt((int) byteRate);
        b.putShort((short) blockAlign);
        b.putShort((short) 16);
        b.put("data".getBytes());
        b.putInt((int) dataLen);
        return b.array();
    }

    private void fail(String msg) {
        Log.e(TAG, msg);
        sError = msg;
        sRunning = false;
        ui.post(() -> {
            stopForeground(STOP_FOREGROUND_REMOVE);
            stopSelf();
        });
    }

    private void startForegroundCompat() {
        NotificationManager nm = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
        NotificationChannel ch = new NotificationChannel(
                CH_ID, "音频探针录制", NotificationManager.IMPORTANCE_LOW);
        nm.createNotificationChannel(ch);

        PendingIntent pi = PendingIntent.getActivity(
                this, 0, new Intent(this, MainActivity.class),
                PendingIntent.FLAG_IMMUTABLE);

        Notification n = new Notification.Builder(this, CH_ID)
                .setContentTitle("正在捕获播放音频")
                .setContentText("切到 Phigros 播放；返回本应用可停止")
                .setSmallIcon(android.R.drawable.ic_btn_speak_now)
                .setOngoing(true)
                .setContentIntent(pi)
                .build();

        startForeground(NOTIF_ID, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PROJECTION);
    }

    @Override
    public void onDestroy() {
        // ★★ 2026-10-18 实测补的第三个入口：用户点「停止录制」走的是
        //    stopService → onDestroy，**不经过** MAX_SECONDS 那个分支。
        //    这里不置 selfStopping 的话，录制线程退出后 finally 里的
        //    projection.stop() 会同步回调 onStop()，把 sError 写成
        //    「投影授权被系统/用户停止」—— 主动停止被当成故障（截图为证）。
        //    比「撞上限」那条更常走：正常用完就是点停止，所以它才是最该修的路径。
        selfStopping = true;
        stopFlag = true;
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }
}
