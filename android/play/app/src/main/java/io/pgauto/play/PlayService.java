package io.pgauto.play;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.graphics.Point;
import android.hardware.display.DisplayManager;
import android.os.Build;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.os.SystemClock;
import android.os.PowerManager;
import android.view.Display;
import android.widget.Toast;

import org.json.JSONObject;

import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.lang.reflect.Method;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;

import io.pgauto.inject.DevRun;
import io.pgauto.inject.ShizukuProbe;
import io.pgauto.inject.sched.Densify;
import io.pgauto.inject.sched.InjectMode;
import io.pgauto.inject.sched.InjectTrace;
import io.pgauto.inject.sched.Scheduler;

/**
 * S1-c：端内闭环的驱动层 —— **进程内**跑完「待命 → 零点 → 注入」。
 *
 * 与 PC 版（`tmp/full_run.py`）的关系：算法完全一样，只是把三处外部依赖搬进进程：
 * ```text
 *   PC 版                                  端内版（本类）
 *   命令行参数 (song/G/touches/music)  →   Intent extras + assets
 *   arm.json / run.json 文件通道        →   直接方法调用（同进程）
 *   Shizuku over adb（inject.py）       →   进程内 DevRun + 同一个 IInputManager 代理
 *   零点 = 声学边沿（l4c 台阶锚）        →   ZeroSource（S1-c 是 tap+估计；S1-d 换 onset）
 * ```
 *
 * ★ 为什么必须是前台服务：注入期间游戏在前台，本 App 的 Activity 必然 onStop ⇒
 *   进程降级成 cached，会被 HONOR 的省电策略限频甚至冻结。只有 FGS 能保证
 *   160 秒不被打断（`InjectService` 在同一台机器上已经用这个形态跑过 10+ 轮）。
 *
 * ★ 三种失败形态必须显式区分（历史上都出现过）：
 *   ① Shizuku 没授权 ⇒ 没有「手」，注入必然全落空 —— 在准备阶段就拒绝，不进倒计时；
 *   ② 载荷解析出 0 条 ⇒ **不许**当成"打了个 0 音符的歌"正常结束；
 *   ③ 注入返回 false 而不抛异常（mode 写错）⇒ 由 `DevRun` 的 maxFalse 阈值兜住。
 */
public class PlayService extends Service {

    public static final String ACTION_START = "io.pgauto.play.START";
    public static final String ACTION_STOP = "io.pgauto.play.STOP";
    /**
     * 「测试点一次 ▶」：只发一个 tap，**不注入任何音符、不占 worker、不算一轮打歌**。
     *
     * ★ 用途（2026-10-30）：换新机时让用户自己验证「这个位置点得到按钮吗」。
     *   比坐标推算可靠 —— 点完看游戏有没有开始加载就知道。
     * ⛔ 三件事绝不能做：① 赋值 {@code worker}（会让 {@code isAlive()} 拒掉下一轮）；
     *   ② 设 {@code RunLog.setRunning}（那不是一轮）；③ 在没授权时静默吞掉（必须 Toast）。
     */
    public static final String ACTION_TEST_TAP = "io.pgauto.play.TEST_TAP";
    /**
     * 「测试 ▶」按下后等多久才真发点击。
     *
     * ⛔ 不能是 0：按下按钮那一刻前台是**本 App**，立刻发 tap 只会点在自己界面上
     * （坐标正好落在右下角），游戏毫无反应 ⇒ 「位置对不对」这个问题永远得不到答案。
     * 4 s 与打歌流程的 8 s 倒计时同构（用户已经习惯"倒计时内切回游戏"这个动作）。
     */
    static final long TEST_TAP_LEAD_MS = 4_000L;
    /**
     * 「学习落点」：进入监听窗口前留给用户切回游戏的时间，与 {@link #TEST_TAP_LEAD_MS} 同构。
     *
     * ⛔ 不能是 0：按下按钮那一刻前台是本 App，用户根本还没到游戏；窗口会白开 15 s。
     */
    static final long LEARN_LEAD_MS = 4_000L;

    /**
     * 学习落点是否正在跑。
     *
     * ★ 自审补的：连点两次会**同时开两个窗口**，各弹一次结果，用户分不清哪个生效；
     *   而且两个窗口都会写 prefs，后写的赢 —— 又变成"到底记了哪个"说不清。
     * 用 volatile 挡；线程任何路径结束都在 finally 里释放，**不许卡死入口**。
     */
    private static volatile boolean learnBusy;
    /** 启动「学习落点」窗口（15 s 内等用户自己点一下 ▶）。 */
    public static final String ACTION_LEARN_TAP = "io.pgauto.play.LEARN_TAP";
    /** 曲目 key（`assets/manifest.json` 里的 `key`，如 `bounded_in`）。 */
    public static final String EXTRA_SONG = "song";
    /** 倒计时秒数（留时间切到游戏并就绪）。 */
    public static final String EXTRA_COUNTDOWN_S = "countdown_s";
    /** 零点估计的 lead（ms）。**只作调试覆盖**，正常从曲目自带值来（见 `TapLead`）。 */
    public static final String EXTRA_LEAD_MS = "lead_ms";
    /** 零点来源：`onset`（默认，S1-d）= 端内实测曲首第一个音；`tap` = 点击 + 固定 lead（S1-c 回退路径）。 */
    public static final String EXTRA_ZERO = "zero";
    public static final String ZERO_ONSET = "onset";
    public static final String ZERO_TAP = "tap";
    /** MediaProjection 授权的 resultCode / data（由 Activity 的 onActivityResult 转进来）。 */
    public static final String EXTRA_RESULT_CODE = "resultCode";
    public static final String EXTRA_RESULT_DATA = "resultData";
    /** 端内滞后（秒）。首版 {@link OnsetZero#DEFAULT_L_DEV_S}，标定后由 Activity 透传覆盖。 */
    public static final String EXTRA_L_DEV_S = "l_dev_s";
    // ★ 2026-11-01：`EXTRA_PROBE`（"服务侧探针"）已删。原注释自己就写着"两个 Context 读同一份
    //   prefs 不可能不一致，除非读的键/曲名不同" —— 而本 manifest 里 PlayService 没有
    //   android:process，本来就跟 Activity 同进程，SharedPreferences 按文件名缓存是同一个实例
    //   ⇒ 那条通路能证明的只有"两边用的 key 字符串是否一样"，这靠读代码就能确认，不值得一个按钮。
    /**
     * 是否用端内标定值自动定 `L_dev`（默认 false = 只信显式参数；界面勾选后传 true）。
     *
     * ★ 打开后：若该曲存过 `K`，则 `L_dev = (K + c)/1000`，并**覆盖**启动参数（日志会写明）。
     */
    public static final String EXTRA_AUTO_L = "auto_l";
    /** 标定存盘（每曲 K + 本机 c）。名字变了就要清旧数据，否则会读到旧口径。 */
    static final String PREF = "pgauto_calib";
    /**
     * 本机常数 `c` 的出厂默认（ms）。换设备必须重标。
     *
     * ★ 2026-10-25 的定值过程（三轮实测，别凭感觉改）：
     *   · bounded_in：`L_dev=225` 打出 φ **三次**（wait×2 + async×1）⇒ `c = 225 − K`，
     *     K 四轮 47.8~53.2 ⇒ **c ≈ 172~177**。
     *   · credits：单轮拟合 `L*=180`、K=−7.3 ⇒ c=187.3（那轮只到 92%，可信度低于三次 φ）。
     *   · 曾经取 181 ⇒ bounded 的 L=228.8 ⇒ **8 个 Good（全 Late）**，说明 181 偏晚。
     *   ⇒ 取 **177**：bounded 落在 224.8（与三次 φ 的 225 一致），credits 落在 169.7（早 ~10 ms，
     *     仍在 P 窗内）。⛔ 别用 credits 的单轮拟合去把 c 抬回 187。
     */
    static final float DEFAULT_C_MS = 177f;

    /**
     * 算 K 至少要采到多少帧（10 ms 一帧）。
     *
     * ★ 实测（把 aphasia 的 30 s 标定截断后重跑互相关）：
     *   30 s ⇒ K=−3.4（cc .982）｜20 s ⇒ K=−3.4（cc .981）｜**10 s ⇒ K=+1876（cc .392）**｜4 s ⇒ K=+11703（cc .903, 质量 1.03）。
     *   10 s 那种被 cc 闸门挡住，但 4 s 只差一点就混过去 ⇒ 光靠质量分不够，必须再加**长度下限**。
     */
    static final int MIN_K_FRAMES = 1200;          // 12 s
    /**
     * lead 的存盘键前缀。**带版本号是故意的**：`lead` 的定义在 2026-10-25 修过一次
     * （标定轮把 L_dev=0 的 zero 当成 lead 记 ⇒ 多记约 177 ms）。换键 = 旧脏值自动作废，
     * 不用去清 SharedPreferences（APK 不可调试，清不了）。
     */
    static final String LEAD_KEY = "lead3_ms_name_";
    static final String LEAD_N_KEY = "lead3_n_name_";
    /** `|K|` 的物理上限：实测三首在 −7.3~+53.2 ⇒ 超过 150 ms 一定是错配，不许写盘。 */
    static final double MAX_ABS_K_MS = 150.0;

    /**
     * 标定采集帧数：**3000 帧 = 30 s**。
     *
     * ★ 为什么不是 20 s：每轮都是倒计时 8 s + 点 ▶ 后还要等曲首 lead 约 7.4 s
     *   ⇒ 20 s 的窗只剩 ~9 s 音乐，互相关在这么短的窗上峰不唯一（实测次峰/主峰 1.02）。
     *   30 s 能留 ~19 s 音乐，够用且不影响任何实时路径（自动标定只旁听、不改注入）。
     * ⛔ 以前这里写的是「用户按下『标定』后切到游戏点 ▶」—— 那个手动标定入口 2026-11-01 已删，
     *   现在只有「注入跑完顺带算 K」这一条路（{@link #finishAutoCalib}）。
     */
    public static final int CALIB_FRAMES = 3000;

    private static final String CH = "pgplay";
    private static final int NOTIF_ID = 4212;
    private static final int DEFAULT_COUNTDOWN_S = 8;

    /** WakeLock 上限：bounded_in 全长 149 s + lead 7.5 s + 余量。超时自动释放，防手滑常驻。 */
    private static final long WAKE_TIMEOUT_MS = 400_000L;

    /** ▶ 在横屏画面上的位置（**比例**，不是像素）。来源 `tmp/full_run.py` 的 `START_REL`。 */
    /**
     * 课题模式：逗号分隔的曲目 key，**按游戏里选歌的顺序**（2026-10-27）。
     * 一次会话 = 一次投影授权 + 一个采集会话 + 顺序注入 N 首。
     * ★ 课题模式是**真功能**（真注入、真判定），从来不是诊断开关 —— 2026-11-01 收掉
     *   dev/user 双形态时它原样保留，是 App 唯一形态下的正式能力。
     */
    public static final String EXTRA_COURSE = "course";
    /** 会话模式 WakeLock：三首 ≈8 分钟，比单曲的 400 s 长得多（方案 §9 B1）。 */
    private static final long COURSE_WAKE_MS = 900_000L;
    /** 曲间休息（与游戏内一致）；只用于算"不早于此"的下界，不靠它对齐。 */
    private static final int COURSE_GAP_MS = 5_000;
    /**
     * 课题模式的零点修正（ms，**正数 = 把零点提前**）。
     *
     * ★ 2026-10-27 22:2x 首次三连的实测：三首**全部只有 Late、Early 一个都没有**
     *   （Good 数 756 / 41 / 213，Perfect 797 / 1242 / 1159）。
     *   课题模式 Perfect 窗是 **±40 ms**（普通模式 ±80），所以"整体晚约 35 ms"这件事
     *   在单曲模式里完全看不出来（一直 AP），到课题模式就变成全 Late。
     *   按 Good 占比反推各首偏差：schneid≈40、玩具≈35、distort≈30 ⇒ 取 35。
     * ⚠ 这是**内部常数**，不做成用户旋钮（见规矩：内部参数不许暴露成用户开关）。
     */
    private static final double COURSE_ZERO_TRIM_MS = 35.0;
    /**
     * 开跑后多久做一次互相关 rezero（ms）。
     * 互相关要 ~6 s 音频才够；此时多数曲子的第一个音符还没到（distort 8.4 s / 玩具 8.3 s）。
     */
    private static final double REZERO_AT_MS = 6_500;
    /**
     * 课堂模式预派窗口（ms）：只提前派发"曲首这么长以内"的事件。
     * 盲区音符都落在 sil_ref 附近（本项目判据 342 ms），500 ms 留足余量。
     */
    private static final double PREROLL_WINDOW_MS = 500.0;
    /**
     * 预派窗口的**下限余量**（ms）。
     *
     * ★ 真实窗口 = `sil_ref + L_dev + 这个值`：主注入要等闸门触发才开始，
     *   而闸门触发时谱面已经走到 `sil_ref + L_dev`（首次可闻）——
     *   **这一段里的事件如果不在预派里派掉，就会被主注入当成"时刻已过"跳过**。
     *   2026-10-28 14:0x 实测：只给 500 ms 时 destruction321 有 20 条被跳、结算 5 个 Miss。
     */
    private static final double PREROLL_MARGIN_MS = 150.0;
    /**
     * 预派总开关，**默认关闭**（2026-10-28 14:4x 用户决定 A 方案）。
     *
     * 实测账（课题模式 B 组，12 轮）：
     *   · 不开预派：destruction321 的 Miss 数在 1~5 之间（那 1 个盲区音符结构性必丢）；
     *   · 开预派：有时降到 1，但 14:4x 那轮 20 条预派里 **5 条判 Miss**
     *     （预派的时间基准偏了 >80 ms），并且 14:3x 还因为"预派阻塞主流程 42 秒"
     *     直接打出 462 Miss。
     * ⇒ 预派只针对 1 个音符，却带来过 2 类新故障，**性价比是负的**。
     *   代码保留（含阻塞修复与 err 夹紧），等 lead 的轮间稳定性被量出来再考虑打开。
     */
    private static final boolean PREROLL_ENABLED = false;
    /** 存「课程 lead」（点 ▶ → 曲首）的 prefs 键；每台设备 × 课题模式各测一次。 */
    private static final String PREF_COURSE_LEAD = "course_lead_ms";
    /**
     * 预派误差的自我校正值（ms）。
     *
     * ★ 预派用"上一轮测到的 lead"猜零点，猜得准不准，只有闸门触发后才知道：
     *   `err = 实测零点 − 预派零点`。把 err 存下来、下一轮预派时补上，
     *   就形成闭环——不需要人工调参（2026-10-28：剩余的那 1 个 Miss 就是它）。
     */
    private static final String PREF_PREROLL_ERR = "course_preroll_err_ms";
    /**
     * 互相关相对"计分校准出来的零点"的**固定偏置**（ms，正 = 相关偏晚）。
     *
     * ★ 2026-10-28 13:0x 实测：带 rezero（已减 trim）的一轮 Good 是 13 / 15 / 66，**全部 Late**
     *   ⇒ 相关零点整体还晚约 20~25 ms（帧时间戳 vs 采样时刻的半块偏差那一类），
     *   所以 rezero 每次都把零点往后推，反而更差。
     *   这里把它扣掉：`zeroCorr -= trim + REZERO_CORR_BIAS_MS`，
     *   剩下的 Δ 才是**本轮真正的抖动**（gate 与相关的相对差）。
     */
    private static final double REZERO_CORR_BIAS_MS = 25.0;
    /**
     * 个别曲目在课题模式下的**额外**零点修正（ms，正 = 再提前）。
     *
     * ★ 来源（2026-10-27 两轮实测）：
     *   第一轮（trim=0）：Good 756 / 41 / 213（schneid / distort / 玩具）
     *   第二轮（trim=35）：Good   5 / **43** /   2  —— 只有 distort_at **一点没动**，
     *   说明它比另两首还晚约 35 ms，是**这首歌自己的偏差**（它存的 K=+2.8 很可能测偏了）。
     * ⚠ 这只是把已知偏差先在会话里校掉；根治要把该曲的 K 重新标一次（单曲模式打一轮）。
     */
    private static final java.util.Map<String, Double> COURSE_TRIM_BY_KEY =
            new java.util.HashMap<>();
    static {
        // ★ 2026-10-28 13:3x：把历史各轮结算反推成"零点偏移 δ"，得到**每首曲各自的最优 trim**。
        //   模型：Perfect 窗 ±40，δ = 40 − σ·Φ⁻¹(1−Good比例)，σ 取 5 ms。
        //   实测（trim=35 那轮的反推值）：
        //     schneid δ≈26 ⇒ 最优 ≈ 61
        //     distort δ≈31 ⇒ 最优 ≈ 66，但 70 时已出 Early、54 时打出 φ ⇒ 取 **54**
        //     玩具     δ≈25 ⇒ 最优 ≈ 60
        //   ⇒ 表里放**绝对值**（总 trim），未列出的曲目用 COURSE_ZERO_TRIM_MS。
        //   ⚠ 这些本质上是"课题模式版的 K 偏差"（K 是单曲模式量的），
        //     最终应该通过"课题模式下标一次 K"来消除，表只是过渡。
        COURSE_TRIM_BY_KEY.put("schneid", 61.0);
        COURSE_TRIM_BY_KEY.put("distort_at", 54.0);
        COURSE_TRIM_BY_KEY.put("xd8e302e4_at", 60.0);
        // ⛔⛔ 2026-10-29 19:50 实测：改成 80 后本曲 Good 从 **17 涨到 252**（仍全 Late、Early 0）。
        //   这是**第三次**同向证据（另两笔在下面 erabyeconnec10n_at 处：trim=60 ⇒ 23，trim=88 ⇒ 1113，
        //   当时被归因为"系统级偏移的好样本/坏样本"，其实是同一个规律）
        //   ⇒ **trim 变大 = Good 变多**，与"zero 减小 ⇒ 注入提前 ⇒ Early"的直观推导**相反**。
        //   ⚠ 注入侧解释不了：那轮 n_skip_past=0 / n_orphan=0 / n_late_move=452（trim=60 那轮是 468），
        //     事件按计划发出去了。⇒ zero 与"游戏判定时刻"之间还有没搞清的环节。
        //   ⛔ 不要再往大调。真要试只能往小调（40），一轮定性，别连续跳。
        // 2026-10-28 13:5x 实测：trim=60 时 Good 486/2330=20.9%（全 Late）
        // ⇒ δ = 40 − 5·Φ⁻¹(0.791) ≈ 36 ms ⇒ 本曲需要 ≈ 96（比另两首大得多，
        //   说明它存的 K 偏差更大；根治仍是用单曲模式重标 K）。
        COURSE_TRIM_BY_KEY.put("destruction321_at", 96.0);
        // 2026-10-28 两轮对照（同一 build 只差这一项）：
        //   trim=60 ⇒ Good 23（全 Late）
        //   trim=88 ⇒ Good **1113**（仍全 Late，且方向与"零点前移"矛盾）
        // ⇒ 88 那轮是**系统级整体偏移**的坏样本（48 ms 的参数变化不可能造成 1090 个 Good 的差），
        //   不能用它覆盖好参数 ⇒ 回到 60。per-song trim 已到极限，剩下是每轮漂移。
        COURSE_TRIM_BY_KEY.put("erabyeconnec10n_at", 60.0);
    }
    /** 第 2/3 首等起音的上限。 */
    private static final int COURSE_WAIT_MS = 25_000;

    static final double START_REL_X = 0.945791;
    static final double START_REL_Y = 0.854861;

    /**
     * ▶ 落点的**手工像素偏移**（2026-10-30）。默认 0 = 与出厂定位逐位相同。
     *
     * ★ 为什么是"偏移"而不是直接改 {@link #START_REL_X}：那两个常量是**出厂默认**的唯一来源，
     *   一旦允许覆盖，以后没人分得清「1522 是原值还是被谁改过」。
     * ⚠ 偏移是**绝对像素**，换设备必须重置（按钮位置本来就按屏幕变）。
     * ⛔ 三个使用点都必须走 {@link #startTapX}/{@link #startTapY}，否则会出现
     *   「某些轮生效、某些轮不生效」——契约测试钉住这一点。
     */
    static final String KEY_START_DX = "start_dx_px";
    static final String KEY_START_DY = "start_dy_px";

    /**
     * ▶ 落点 x。**唯一来源是 {@link StartPointPrefs#tapX}**：学到的（未失效）优先，否则出厂比例。
     *
     * ⛔ 2026-10-31 起**不再读** `KEY_START_DX`：手填偏移已废弃。
     *   设备里残留的 `+15/-4` 因此不生效（等价 0/0），由「恢复默认」顺手删掉。
     */
    float startTapX(double dstW) {
        int[] wh = sizeOrZero();
        return StartPointPrefs.tapX(this, dstW, wh[0], wh[1]);
    }

    /** ▶ 落点 y（同 {@link #startTapX} 的口径）。 */
    float startTapY(double dstH) {
        int[] wh = sizeOrZero();
        return StartPointPrefs.tapY(this, dstH, wh[0], wh[1]);
    }

    /**
     * 横屏尺寸；拿不到时返回 `{0,0}`。
     *
     * ★ 返回 0 而不是抛：`{0,0}` 会让设备指纹必然不匹配 ⇒ `StartPointPrefs` 自动退回出厂比例，
     *   等于"拿不到屏幕就用最保守的值"。**不许**在这里静默当成 1610×720。
     */
    private int[] sizeOrZero() {
        try {
            return landscapeSize();
        } catch (Throwable t) {
            return new int[]{0, 0};
        }
    }

    // ⛔ 这里**没有**全局 lead 常数（10-24 删除 `DEFAULT_LEAD_MS = 7479.88`）。
    //
    //   7479.88 本身**有出处**：它是 bounded_in 两轮 `d_ideal` 的池中位
    //   （(7.44334 + 7.51643)/2 = 7.479885，与 FACTS §155 那条日志
    //   「兜底 D = 7.4799s ← 设备池中位数 n=2，跨度 73ms」一致）。
    //   但它不能当零点的默认值，三条理由：
    //     ① 它是**统计量**：两轮真机实测相差 73.09 ms，取均不等于任何一轮的真实 lead；
    //     ② 真正打出 φ 的是第 3 轮，lead = **7520.086** ms（tmp/timeline_bounded_in_r3.json）；
    //     ③ 最要命的一条：`MainActivity.launchPlay` 从不传 `EXTRA_LEAD_MS`，于是它成了
    //        **任何曲**的默认值，且没有任何曲目校验 ⇒ 换曲静默偏 300+ ms
    //        （D 池跨曲 7.13~7.52），症状是「链路全绿、注入条数对得上、成绩全 Miss」。
    //   ⇒ 现在 lead 跟着曲子走（manifest 的 `tap_lead_ms`，值必须是**某一轮实测**），
    //     没测过就**拒绝**。规则与理由集中在 `TapLead`。`EXTRA_LEAD_MS` 只作调试覆盖。

    private volatile boolean stopFlag;
    /** 本轮是否用「标定值 K + c」自动定 L_dev（界面开关）。 */
    private volatile boolean autoLDev;
    /** G1：本轮是否出现过参数落盘失败（进 trace + 界面标红）。 */
    private volatile boolean persistFail;
    /**
     * G2：设备指纹（存盘 `dev_fp`）。指纹变了 ⇒ 旧设备的 `K` / `lead` / `c` 全都不成立，
     * 一律当"没标定过"（见 {@link #checkFingerprint()}）。
     */
    static final String FP_KEY = "dev_fp";
    /** G13：构建自证（assets/build_info.txt，build.sh 写）。懒加载一次。 */
    private BuildInfo build;
    /** G2 的结论：true ⇒ 本轮不用旧 K/lead/c。 */
    private volatile boolean fpMismatch;
    /** G10：本轮实测的 lead（ms，已按轮次口径换算过），供 trace 尾部那行用。 */
    private double lastLeadMeas = Double.NaN;
    // ── lead 事后对账（LeadAudit）用的写前快照 ──
    private SongLib.Song leadSnapSong;
    private float leadSnapValue = Float.NaN;
    private int leadSnapN, leadSnapSideN, leadSnapSideSign;
    private boolean leadSnapValid;
    private Thread worker;
    private NotificationManager nm;
    private PowerManager.WakeLock wl;

    // ─────────────────────────── 生命周期 ───────────────────────────

    @Override
    public void onCreate() {
        super.onCreate();
        nm = (NotificationManager) getSystemService(Context.NOTIFICATION_SERVICE);
        if (nm != null) {
            NotificationChannel ch = new NotificationChannel(
                    CH, "端内演奏", NotificationManager.IMPORTANCE_LOW);
            ch.setShowBadge(false);
            nm.createNotificationChannel(ch);
        }
        // 阶段一变就刷通知栏：注入期间用户看的是游戏，通知是他唯一能看到进度的地方
        RunLog.setListener(p -> {
            if (nm != null) nm.notify(NOTIF_ID, notif(p));
        });
    }

    @Override
    public IBinder onBind(Intent intent) { return null; }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        String act = intent == null || intent.getAction() == null
                ? ACTION_START : intent.getAction();
        if (ACTION_STOP.equals(act)) {
            stopFlag = true;
            RunLog.line("收到停止指令 ⇒ 下一条事件之前停止并抬起手指");
            return START_NOT_STICKY;
        }
        if (ACTION_LEARN_TAP.equals(act)) {
            // ★ 与「测试 ▶」同构：单独起线程、不赋值 worker、不 setRunning ⇒ 不影响下一轮能否启动。
            // ★ lead 的理由与 TEST_TAP_LEAD_MS 完全一样：按按钮那一刻前台是本 App。
            if (learnBusy) {
                RunLog.line("学习落点：已有窗口在进行 ⇒ 忽略这次点击");
                toast("已有一个学习窗口在进行（约 20 秒），等它结束再试");
                return START_NOT_STICKY;
            }
            learnBusy = true;
            final int[] wh = sizeOrZero();
            RunLog.line(String.format(Locale.ROOT,
                    "学习落点将在 %.1f s 后开始监听（请立刻切回游戏，第一次点击请点在开始按钮上）",
                    LEARN_LEAD_MS / 1000.0));
            Thread t = new Thread(() -> {
              try {
                // ★ 2026-10-31 第一次真机实测后改：准备（起 getevent -p、建轴映射）放在**lead 之前**。
                //   原来放在 lead 之后 ⇒ 用户看到"4 秒后开始监听"就在第 4 秒点，正好撞上这次进程启动，
                //   点击落在几十~一百毫秒的空档里 ⇒ 永远收不到，而界面只说"没等到点击"。
                LearnOnce.Prep prep = LearnOnce.prepare(this, wh[0], wh[1]);
                if (prep.err != null) {
                    toast(prep.err);
                    return;
                }
                toast(String.format(Locale.ROOT, "%.0f 秒后开始监听：请切回游戏停在选曲页",
                        LEARN_LEAD_MS / 1000.0));
                try {
                    Thread.sleep(LEARN_LEAD_MS);
                } catch (InterruptedException ie) {
                    RunLog.line("学习落点：等待被中断 ⇒ 不启动监听");
                    return;
                }
                // ★ 明确告诉用户"现在才开始听"，否则他会在 lead 期间点、正好落在盲区
                toast(String.format(Locale.ROOT, "现在开始监听 %.0f 秒：点一下「开始演奏」",
                        LearnOnce.WINDOW_MS / 1000.0));
                LearnOnce.Result r = LearnOnce.listen(this, prep, wh[0], wh[1], LearnOnce.WINDOW_MS);
                toast(r.msg);
              } finally {
                learnBusy = false;      // ⛔ 任何路径都要放行
              }
            }, "pg-learntap");
            t.setDaemon(true);
            try {
                t.start();
            } catch (Throwable err) {
                // 起线程都失败（极端情况）⇒ 立刻放行，否则入口永久卡住
                learnBusy = false;
                RunLog.line("学习落点：起线程失败 " + ShizukuProbe.brief(err));
                toast("学习落点启动失败，请重试");
            }
            return START_NOT_STICKY;
        }
        if (ACTION_TEST_TAP.equals(act)) {
            // ★ 单独起线程：不赋值 worker、不设 setRunning ⇒ 不影响下一轮能否启动。
            // ★★ 2026-10-30：必须**先等几秒再点**。原因很实在 —— 用户是在本 App 界面上
            //   按的「测试 ▶」，那一刻前台是 App 自己；立刻发 tap 只会点在自己的界面上
            //   （坐标还正好落在右下角），游戏那边什么都没发生，于是「位置对不对」这个
            //   问题永远得不到答案。留 4 s 给用户切回游戏，与打歌流程的 8 s 倒计时同构。
            RunLog.line(String.format(Locale.ROOT,
                    "测试点击将在 %.1f s 后发出（请立刻切回游戏并停在选曲页）",
                    TEST_TAP_LEAD_MS / 1000.0));
            toast("4 秒后点击 ▶：请立刻切回游戏并停在选曲页");
            Thread t = new Thread(() -> {
                try {
                    Thread.sleep(TEST_TAP_LEAD_MS);
                } catch (InterruptedException ie) {
                    return;      // 被中断就不点了（不让一次测试点击变成"意外连点"）
                }
                testTapOnce();
            }, "pg-testtap");
            t.setDaemon(true);
            t.start();
            return START_NOT_STICKY;
        }

        // ★ 零点模式要在 startForeground 之前解析：`onset` 需要 mediaProjection 这个
        //   FGS 类型位，Android 14 上类型与后面 getMediaProjection 的调用必须对得上。
        String zeroMode = ZERO_ONSET;
        String badZero = null;
        try {
            zeroMode = zeroModeOf(intent);
        } catch (IllegalArgumentException e) {
            badZero = e.getMessage();
        }
        startFg("准备中…", ZERO_ONSET.equals(zeroMode));

        if (badZero != null) {
            RunLog.phase("失败");
            RunLog.line("!! " + badZero);
            finish();
            return START_NOT_STICKY;
        }

        if (worker != null && worker.isAlive()) {
            RunLog.line("⚠ 上一轮还没收尾 ⇒ 这次点击不算（等几秒再点，别连点）");
            return START_NOT_STICKY;
        }

        final String song = intent == null ? null : intent.getStringExtra(EXTRA_SONG);
        final int countdown = intent == null ? DEFAULT_COUNTDOWN_S
                : intent.getIntExtra(EXTRA_COUNTDOWN_S, DEFAULT_COUNTDOWN_S);
        // ★ 没给就是 null（不是 7479.88 之类的默认值）：null = 「没给」，
        //   显式给了个 0/负数 = 「给了个非法值」，两者的处置不同（见 TapLead）。
        final Double leadOverride = intent != null && intent.hasExtra(EXTRA_LEAD_MS)
                ? intent.getDoubleExtra(EXTRA_LEAD_MS, 0) : null;
        final double lDev = intent != null && intent.hasExtra(EXTRA_L_DEV_S)
                ? intent.getDoubleExtra(EXTRA_L_DEV_S, OnsetZero.DEFAULT_L_DEV_S)
                : OnsetZero.DEFAULT_L_DEV_S;
        final int rc = intent == null ? 0 : intent.getIntExtra(EXTRA_RESULT_CODE, 0);
        final Intent rd = intent == null ? null
                : (Intent) intent.getParcelableExtra(EXTRA_RESULT_DATA);

        final String mode = zeroMode;
        // ★ 2026-11-01：原来的「服务侧探针」分支（读 prefs → 打 6 行 → finish）已删，
        //   理由见 `EXTRA_PROBE` 原位置那条注释。⛔ 别以"多点观测手段"为由加回来。
        autoLDev = intent != null && intent.getBooleanExtra(EXTRA_AUTO_L, false);
        // ★ 课题模式（2026-10-27）：一次授权跑 N 首，是**真功能**（真注入、真判定）。
        final String course = intent != null
                ? intent.getStringExtra(EXTRA_COURSE) : null;
        final String courseKeys = course == null ? "" : course.trim();
        stopFlag = false;
        worker = new Thread(() -> {
            if (!courseKeys.isEmpty()) {
                runCourse(courseKeys, lDev, rc, rd);
            } else {
                runOnce(song, countdown, leadOverride, mode, lDev, rc, rd);
            }
        }, "pg-play");
        worker.start();
        return START_NOT_STICKY;
    }

    /** `EXTRA_ZERO` 只认 `onset` / `tap`；缺省 onset。非法值必须炸（不许静默取默认）。 */
    private static String zeroModeOf(Intent intent) {
        String raw = intent == null ? null : intent.getStringExtra(EXTRA_ZERO);
        if (raw == null || raw.isEmpty()) return ZERO_ONSET;
        if (ZERO_ONSET.equals(raw) || ZERO_TAP.equals(raw)) return raw;
        throw new IllegalArgumentException("EXTRA_ZERO 只能是 onset/tap，得到「" + raw + "」");
    }

    @Override
    public void onDestroy() {
        stopFlag = true;
        RunLog.setListener(null);
        releaseWake();
        super.onDestroy();
    }

    // ─────────────────────────── 主流程 ───────────────────────────

    /** 本轮课程的 sidecar（每首一行：起音/零点/trim），没有就为 null。 */
    private File courseCsv;

    /**
     * 开一份课程 sidecar。2026-10-28：日志面板只留最后一轮，抓不到"第 2/3 首到底在点后几秒触发"，
     * 而这正是判断"闸门锁到过场音"的唯一证据 ⇒ 落盘。
     */
    private void openCourseCsv() {
        File dir = getExternalFilesDir(null);
        if (dir == null) {
            return;
        }
        String stamp = new java.text.SimpleDateFormat("HHmmss", Locale.ROOT)
                .format(new java.util.Date());
        courseCsv = new File(dir, "course_" + stamp + ".csv");
        try (FileOutputStream fo = new FileOutputStream(courseCsv, false)) {
            fo.write("song,fired_rel_ms,zero_rel_ms,trim_ms\n"
                    .getBytes(StandardCharsets.UTF_8));
        } catch (Throwable ignore) {
            // 落盘失败不能影响打歌
        }
    }

    private void appendCourseLine(String song, double firedRelMs, double zeroRelMs, double trimMs) {
        if (courseCsv == null) {
            return;
        }
        String line = String.format(Locale.ROOT, "%s,%.1f,%.1f,%.1f%n",
                song, firedRelMs, zeroRelMs, trimMs);
        try (FileOutputStream fo = new FileOutputStream(courseCsv, true)) {
            fo.write(line.getBytes(StandardCharsets.UTF_8));
        } catch (Throwable ignore) {
            // 同上
        }
    }

    /**
     * 中止 / 停止的**原因**也写进课程 sidecar（2026-10-28 诊断轮暴露的缺口：
     * 原因只在端内 16 行日志里，下一轮就被覆盖 ⇒ 事后只能靠"少一份 trace"推断，
     * 判不出是超时、Shizuku 掉了还是用户按了停止）。
     *
     * 写成 `# aborted: …`（`#` 行按注释跳过，与 trace 的 header 约定一致）。
     * ⚠ 必须单行：换行会把 sidecar 结构弄坏（格式由 {@link CourseAbort} 负责，有离线单测）。
     */
    private void appendCourseAbort(String reason) {
        if (courseCsv == null) {
            return;
        }
        try (FileOutputStream fo = new FileOutputStream(courseCsv, true)) {
            fo.write(CourseAbort.csvLine(reason).getBytes(StandardCharsets.UTF_8));
        } catch (Throwable ignore) {
            // 取证失败不能影响收尾
        }
    }

    /**
     * 端内 Toast：会话在后台跑、人盯着游戏画面，只有日志面板是**看不见**的
     * （2026-10-28 诊断轮：用户以为"没生效"，实际已经中止了）。
     */
    private void toast(final String msg) {
        try {
            new Handler(Looper.getMainLooper()).post(
                    () -> Toast.makeText(getApplicationContext(), msg, Toast.LENGTH_LONG).show());
        } catch (Throwable ignore) {
            // Toast 失败不能盖掉真正的错误
        }
    }

    /**
     * 课题模式：**一次投影授权、一个采集会话，按顺序连打 N 首**（2026-10-27）。
     *
     * 与单曲路径（{@link #runOnce}）的关系：载荷解析/排程/注入全部复用，区别只在「零点从哪来」——
     * 会话里没有"每首点一次 ▶"，所以：
     *   · 第 1 首：倒计时 → 自己点 ▶ → `tap.awaitFire(0)`；
     *   · 第 2/3 首：上一首结束后 `rearm(本曲 sil_thr)` → `awaitFire(上一首末事件 + 5 s)`。
     *     这个"不早于"下界是关键：休息期的过场音效会被 gate 抓成假起音，
     *     `awaitFire` 会把过早的触发丢弃并重新待命（方案 §9 B3）。
     *
     * ★ S1 明确不做（都在方案 §9 里有理由，改之前先看）：
     *   · 不做两段锚预派（第 2/3 首没有 tap 锚；第 1 首的 lead 在课题模式未必适用 §B6）
     *   · 不做自动标定（会阻塞 20 s，与 5 s 休息冲突 §B2）
     *   · 不做会话级 trace（每首仍一份，§B8）
     */
    private void runCourse(String keysCsv, double lDevS,
                           int resultCode, Intent resultData) {
        RunLog.setRunning(true);
        RunLog.clear();
        List<String> keys = new ArrayList<>();
        for (String k : keysCsv.split(",")) {
            String s = k.trim();
            if (!s.isEmpty()) keys.add(s);
        }
        RunLog.line("==== 课题模式开始：" + keys.size() + " 首 " + keys);
        RunLog.line("⚠ 会话模式不标定 K/lead；需要标定请先单独打一轮");
        openCourseCsv();
        acquireWake(COURSE_WAKE_MS);

        OnsetTap tap = null;
        // ★ 2026-10-28：**完整注入完**的曲数 —— 中止时写进 sidecar 与 Toast。
        //   "打到第几首才断"是判读的第一信息（端内日志只有 16 行，下一轮就被覆盖）。
        int done = 0;
        try {
            long elapsed0 = 0, epoch0 = 0;
            double lastEndEpochMs = 0;
            double prevZeroEpoch = 0;       // 上一首的零点（epoch ms）
            double prevLenMs = 0;           // 上一首的**音乐总时长**（ms）
            for (int i = 0; i < keys.size(); i++) {
                if (stopFlag) {
                    RunLog.line("会话：收到停止 ⇒ 中止（已完成 " + done + " 首）");
                    appendCourseAbort("用户停止（已完成 " + done + " 首）");
                    break;
                }
                final String key = keys.get(i);
                Job job = prepare(key, ZERO_ONSET, lDevS, null, resultCode, resultData);
                job.autoCalib = false;
                job.preRoll = false;
                float x = startTapX(job.dstW);
                float y = startTapY(job.dstH);
                long notBeforeNs = 0;

                double preZeroUsed = Double.NaN;
                if (i == 0) {
                    ZeroSource.Tapper tapper = new ZeroSource.ShizukuTap(job.proxy, job.inject);
                    tap = new OnsetTap(this, resultCode, resultData, job.song.silThr, 0);
                    tap.setSessionMode(true);
                    tap.start();
                    RunLog.line(String.format(Locale.ROOT,
                            "第 1 首 %s：倒计时 %d s 后点 ▶ %.1f,%.1f", key,
                            DEFAULT_COUNTDOWN_S, x, y));
                    countdown(DEFAULT_COUNTDOWN_S);
                    // ★ 点 ▶ 之前**重建 gate**：倒计时/暂停界面里的任何声音都不该被当成曲首。
                    //   （2026-10-27 实测 bug：不重建 ⇒ 倒计时期间 gate 已触发 ⇒ 零点被算到过去
                    //     ⇒ 前 288 条事件因"时刻已过"被丢弃，第一首打得一塌糊涂。）
                    // ★ 点 ▶ 之前先确认代理：Shizuku 代理必须已建好。
                    //   "整场没点下去"是最难查的失败（日志只看到倒计时），所以把结论写明确。
                    if (job.proxy == null) {
                        throw new IllegalStateException("没有 Shizuku 注入代理（授权/绑定失败）⇒"
                                + " 本轮不会点 ▶，先回单曲页确认「Shizuku 权限：已授予」");
                    }
                    RunLog.line("第 1 首：点 ▶ 用 Shizuku 代理"
                            + "，位置 " + String.format(Locale.ROOT, "%.1f,%.1f", x, y));
                    tap.rearm(job.song.silThr, false);
                    elapsed0 = SystemClock.elapsedRealtimeNanos();
                    epoch0 = System.currentTimeMillis();
                    String err = tapper.tap(x, y);
                    if (err != null) throw new IllegalStateException("点 ▶ 失败：" + err);
                    // 起音必须晚于点击时刻，否则就是点击音/残留声
                    notBeforeNs = elapsed0;
                    // ★ 预派（2026-10-28）：曲首的盲区音符在"闸门触发"时已经过去了，必丢。
                    //   上一次跑测到的"课程 lead"（点 ▶ → 曲首）用来提前派发首段；
                    //   派发完再照常等闸门，闸门到了用实测零点派发剩余（已经派过的会因为
                    //   "时刻已过"被跳过，不会重复）。
                    final float leadStored = prefs().getFloat(PREF_COURSE_LEAD, Float.NaN);
                    if (PREROLL_ENABLED && job.song.blindLost > 0 && !Float.isNaN(leadStored)
                            && leadStored > 1000) {
                        // ★ 存的 lead 是「点 ▶ → 首次可闻」；预派要的是「点 ▶ → 曲首」，
                        //   两者差 sil_ref + L_dev。漏扣这一步的后果：预派整体晚 ~2.5 s，
                        //   那些事件既打不上、又会被主注入当"已过"跳过（2026-10-28 实测 5 个 Miss）。
                        float errRaw = prefs().getFloat(PREF_PREROLL_ERR, 0f);
                        // ★ 夹紧（2026-10-28 14:3x 事故）：err 一旦变大，predZero 就会被推到很远的未来，
                        //   而预派的 DevRun 会**真的 sleep 到那个时刻** ⇒ 阻塞主流程 40 秒
                        //   ⇒ 闸门早已触发、零点落在过去、前 2283 条被跳过（结算 462 Miss）。
                        final double errStored = Math.max(-300.0, Math.min(300.0, errRaw));
                        if (Math.abs(errRaw) > 300) {
                            RunLog.line(String.format(Locale.ROOT,
                                    "⚠ 预派校正值异常（%.0f ms）⇒ 本轮按 %.0f 使用",
                                    errRaw, errStored));
                        }
                        final double preZero = epoch0 + leadStored
                                - job.song.silRefS * 1000.0 - job.lDevS * 1000.0
                                + errStored;
                        preZeroUsed = preZero;
                        final double win = job.song.silRefS * 1000.0 + job.lDevS * 1000.0
                                + PREROLL_MARGIN_MS;
                        RunLog.line(String.format(Locale.ROOT,
                                "第 1 首：本曲有 %d 个盲区音符 ⇒ 预派前 %.0f ms"
                                        + "（课程 lead %.0f − sil_ref %.0f − L_dev %.0f）",
                                job.song.blindLost, win, leadStored,
                                job.song.silRefS * 1000.0, job.lDevS * 1000.0));
                        final double fPreZero = preZero;
                        final double fWin = win;
                        Thread pr = new Thread(() -> preRollFirstEvents(job, fPreZero, fWin),
                                "pg-preroll");
                        pr.setDaemon(true);
                        pr.start();
                    } else if (job.song.blindLost > 0) {
                        RunLog.line("第 1 首：本曲有 " + job.song.blindLost
                                + " 个盲区音符，但还没有课程 lead（先跑一轮量出来）⇒ 本轮会丢");
                    }
                } else {
                    // ★ 先按**自然待命**（要 300 ms 静音）：过场/结算页通常有静音，
                    //   这样认出来的是"静音→有声"的真实边沿。
                    //   ⛔ 不用 forceArm 起步：持续音乐里强制待命 ⇒ 1.5 s 窗一到就触发，
                    //      会得到一个"看起来合理、实际晚 1.5 s"的零点（全篇 Good，最难查）。
                    //   等不到再退到 forceArm（见下面的兜底）。
                    tap.rearm(job.song.silThr, false);
                    // ★ 下界 = 上一首**零点 + 音乐总时长** + 曲间 5 s（2026-10-28 实测修正）
                    //   原来用"上一首末音符 + 5 s"：ERABYECONNEC10N 长 168.02 s、末音符只到 160.8 s
                    //   ⇒ 下界早了 7.2 s，闸门锁到上一首尾音，第 2/3 首整首错位（959/989 Miss）。
                    final double baseMs = CourseBound.baseMs(prevZeroEpoch, prevLenMs,
                            lastEndEpochMs);
                    notBeforeNs = elapsed0
                            + (long) ((baseMs - epoch0) * 1_000_000L)
                            + COURSE_GAP_MS * 1_000_000L;
                    RunLog.line(String.format(Locale.ROOT,
                            "第 %d 首 %s：rearm(sil_thr=%.3f) ⇒ 等起音（不早于上一首末 +%d ms）",
                            i + 1, key, job.song.silThr, COURSE_GAP_MS));
                }

                // ── 零点：**一律用 OnsetGate（快），不再用 lead 猜，也不再等互相关** ───────
                //
                // 2026-10-27 两次真机的教训：
                //  · 21:49：gate 在点后 16.9 s 触发 —— 那**就是**正确的曲首（课题开场比单曲长），
                //    被我用单曲 lead 做的"像不像曲首"检查丢掉了；
                //  · 22:0x：改用互相关后，第 1 首 n_skip_past=489（前 6 s 音符全丢），
                //    第 2 首 25 s 都没匹配上（cc=0.15）——**互相关要等 6 s 音频才够，太慢**。
                // ⇒ 结论：注入起点必须用 gate（毫秒级），互相关只能当事后审计（见 §9 待办）。
                // ★ 初值必须是 NaN：失败路径要读它来判断「本曲零点到底有没有建立」
                //   （Java 的 definite-assignment 也不允许留空再在 catch 里读）。
                double zeroEpoch = Double.NaN;
                double zeroRel;
                // ★ 第 2/3 首失败**不是一律跳过**（2026-10-28 复盘修正）：
                //   零点没建立 ⇒ 下一首的下界不可知 ⇒ 必须中止（见 CourseBound 类注释与 catch 里的分流）；
                //   零点已建立（注入中途失败）⇒ 音乐时间轴与我方注入无关 ⇒ 继续。
                //   ⛔ 原来这里是「失败一律 continue」，实测会把下一首整首打废（一次失败放大成两次）。
                try {
                final long deadline = System.currentTimeMillis()
                        + (i == 0 ? 30_000 : 45_000);
                int retries = 0;
                while (true) {
                    final long remain = deadline - System.currentTimeMillis();
                    if (remain <= 0) {
                        throw new IllegalStateException("第 " + (i + 1)
                                + " 首等不到起音（超时；已重试 " + retries + " 次）"
                                + (tap.error() == null ? "" : "：" + tap.error()));
                    }
                    if (!tap.awaitFire(notBeforeNs, (int) remain, i > 0)) {
                        throw new IllegalStateException("第 " + (i + 1) + " 首等不到起音（超时 "
                                + remain + " ms）"
                                + (tap.error() == null ? "" : "：" + tap.error()));
                    }
                    final long fired = tap.gate().firedAtNs();
                    final double zRel = (fired - elapsed0) / 1e6 - job.song.silRefS * 1000.0;
                    final double z = epoch0 + zRel - job.lDevS * 1000.0;
                    // 只做**物理**检查（零点不能在"现在"之后太多 = 明显算错），不再拿 lead 比
                    final double ahead = z - System.currentTimeMillis();
                    if (ahead <= 120_000) {
                        zeroRel = zRel;
                        zeroEpoch = z;
                        if (i == 0 && !Double.isNaN(preZeroUsed)) {
                            final double err = zeroEpoch - preZeroUsed;
                            final String e2 = PrefGuard.putFloat(prefs(), PREF_PREROLL_ERR,
                                    (float) err);
                            RunLog.line(String.format(Locale.ROOT,
                                    "预派校验：实测零点 − 预派零点 = %+.0f ms%s", err,
                                    e2 == null ? "（已存为下一轮的校正值）" : "（落盘失败：" + e2 + "）"));
                        }
                        if (i == 0) {
                            // 记下"课程模式"的 lead（点 ▶ → 曲首），下一轮预派用。
                            // ⚠ 必须走 PrefGuard（commit + 回读）：apply() 会被 force-stop 静默丢写，
                            //   而"丢掉 lead"的后果是下一轮预派用旧值 —— 看起来完全正常。
                            String werr = PrefGuard.putFloat(prefs(), PREF_COURSE_LEAD,
                                    (float) ((fired - elapsed0) / 1e6));
                            RunLog.line(werr == null
                                    ? String.format(Locale.ROOT, "课程 lead 已存：%.0f ms",
                                    (fired - elapsed0) / 1e6)
                                    : "课程 lead 落盘失败：" + werr);
                        }
                        RunLog.line(String.format(Locale.ROOT,
                                "第 %d 首：起音在点后 %.0f ms（单曲 lead=%.0f 仅供参考）⇒ zero 距现在 %+.0f ms",
                                i + 1, (fired - elapsed0) / 1e6, job.leadAtPrepare, ahead));
                        break;
                    }
                    retries++;
                    RunLog.line("第 " + (i + 1) + " 首：零点算出在未来 " + String.format(Locale.ROOT, "%.0f", ahead)
                            + " ms ⇒ 丢弃重等（第 " + retries + " 次）");
                    if (retries >= 6) {
                        throw new IllegalStateException("第 " + (i + 1) + " 首连续 6 次零点异常 ⇒ 放弃本场");
                    }
                    tap.rearm(job.song.silThr, i > 0);
                }
                RunLog.phase("第 " + (i + 1) + " 首注入");
                RunLog.line(String.format(Locale.ROOT,
                        "第 %d 首：zero_rel=%.1f ms · sil_ref=%.0f · L_dev=%.0f ⇒ zero 距现在 %+.0f ms",
                        i + 1, zeroRel, job.song.silRefS * 1000, job.lDevS * 1000,
                        zeroEpoch - System.currentTimeMillis()));
                // ★ 课题模式零点修正（把整体偏晚的 ~35 ms 校回来；见 COURSE_ZERO_TRIM_MS 注释）
                // ⛔⛔ 这张表**只许在 runCourse 里用**：单曲模式（runOnce）不走 trim，
                //   用户 2026-10-29 明确要求两模式互不影响 ⇒ 别把这里的三行抄进 runOnce。
                // ★ 另一个已知但无害的耦合：`prevZeroEpoch = zeroEpoch` 取的是**减过 trim** 的值，
                //   所以下一首的 notBefore 也偏早 trim ms；靠 COURSE_GAP_MS(5000) 与实测 9.7 s 之间
                //   4.7 s 的余量兜着（trim 量级 54~96 ms，淹没在余量里）。trim 若涨到几百 ms 才会咬人。
                // 本曲若在表里 ⇒ 用它（绝对值）；否则用通用值
                final double trim = COURSE_TRIM_BY_KEY.containsKey(job.song.key)
                        ? COURSE_TRIM_BY_KEY.get(job.song.key)
                        : COURSE_ZERO_TRIM_MS;
                zeroEpoch -= trim;
                zeroRel -= trim;
                appendCourseLine(job.song.key,
                        zeroRel + job.song.silRefS * 1000.0 + job.lDevS * 1000.0,
                        zeroRel, trim);
                RunLog.line(String.format(Locale.ROOT,
                        "第 %d 首：课题模式零点修正 −%.0f ms（%s；zero 距现在 %+.0f ms）",
                        i + 1, trim,
                        COURSE_TRIM_BY_KEY.containsKey(job.song.key) ? "本曲实测值" : "通用值",
                        zeroEpoch - System.currentTimeMillis()));
                // ★ 健康指标：注入即将从曲中第几秒开始。正常应 ≈ sil_ref（1~3 s）；
                //   21:54 那次是 12.07 s ⇒ 前 12 s 音符全被跳过（零点早了 12 s），
                //   而界面上只看到"打了"，看不出丢了 500 条事件。
                final double firstT = job.cmds.isEmpty() ? 0 : job.cmds.get(0).tMs();
                final double lateStartMs = (System.currentTimeMillis() - zeroEpoch) - firstT;
                if (lateStartMs > 3000) {
                    RunLog.line(String.format(Locale.ROOT,
                            "⚠ 第 %d 首零点偏早 %.0f ms ⇒ 前 %.1f s 的音符会被跳过（正常应 ≈ sil_ref %.0f ms）",
                            i + 1, lateStartMs, Math.max(0, lateStartMs) / 1000.0,
                            job.song.silRefS * 1000));
                } else {
                    RunLog.line(String.format(Locale.ROOT,
                            "第 %d 首：首事件 t=%.0f ms（落后 %.0f ms，正常 ≈ sil_ref %.0f）",
                            i + 1, firstT, lateStartMs, job.song.silRefS * 1000));
                }
                startRezeroWatcher(job, tap, elapsed0, epoch0, zeroEpoch, trim, i);
                // ★ 2026-10-29：注入前先固定"当首"的起音信息。`startRezeroWatcher` 可能改 gate，
                //   注入跑完（几十秒~几分钟）再读 `gate().*` 拿到的未必还是本首的。
                //   ⚠ `firedFrame()` 是**检测器自增的累计帧号**（不随 rearm 清零），而 `envelope()`
                //     每次 rearm 从 0 开始 ⇒ 两者坐标不同（第 2 首实测 ff=19538 > frames=16913）。
                //     所以同时取 `firedAtNs`，用「起音时刻 − 本首第 0 帧时刻」换算成**本首帧号**。
                final long ffThisSong = tap.gate().firedFrame();
                final long ffAtNs = tap.gate().firedAtNs();
                inject(job, zeroEpoch);
                // ★★ 2026-10-29 事后审计（只读）：δ ≡ K_stored − K_round，本行把 K_round 落盘。
                //   必须在**本首注入结束后、下一首 rearm 之前**（rearm 会 envN=0 清空包络）。
                auditKRound(job.song, tap, i, ffThisSong, ffAtNs);
                double lastT = job.cmds.isEmpty() ? 0 : job.cmds.get(job.cmds.size() - 1).tMs();
                lastEndEpochMs = zeroEpoch + lastT;
                prevZeroEpoch = zeroEpoch;
                prevLenMs = SongLib.lengthOf(job.song.key) * 1000.0;
                RunLog.line(String.format(Locale.ROOT, "第 %d 首注入结束（末事件 %.0f ms）", i + 1, lastT));
                done++;
                } catch (Throwable per) {
                    if (i == 0 || stopFlag) throw per;          // 第 1 首都打不起来，整场没意义
                    RunLog.line("!! 第 " + (i + 1) + " 首没打起来：" + ShizukuProbe.brief(per));
                    // ★ 2026-10-28 复盘修正（真 bug）：失败分两种，判据 = **本曲零点有没有建立**。
                    //   · 没建立（等不到起音 / 连续 6 次零点异常）⇒ 下一首的下界不可知，
                    //     闸门只能盲等 ⇒ 必然锁到"还在播放的本曲尾音" ⇒ 把下一首整首打废。
                    //     ⇒ **中止整场**。不是"跳过这一首"：没有锚，下一首的"不早于"根本算不出来。
                    //   · 已建立（inject 中途抛 / Shizuku 断连）⇒ 音乐时间轴与我方注入无关，
                    //     `zero + 音乐总时长` 仍然是**正确**的下界 ⇒ 更新状态、继续下一首。
                    //   ⛔ 别改回「失败一律 continue」（那个 bug 就是它），也别改成「一律中止」
                    //     （会白扔掉还能打的一轮）。判据在 CourseBound，有离线单测。
                    final double lenMs = SongLib.lengthOf(job.song.key) * 1000.0;
                    final double nextBase = CourseBound.nextBaseAfterFailure(zeroEpoch, lenMs);
                    if (Double.isNaN(nextBase)) {
                        // ⛔ 消息里**不要**再写「已完成 N 首」：外层 appendCourseAbort 会统一补一次
                        //   （2026-10-29 真机 sidecar 实测：两处都加 ⇒ 「（已完成 1 首）」出现两次）。
                        throw new IllegalStateException("第 " + (i + 1) + " 首连零点都没建立"
                                + " ⇒ 后面每首的下界都不可知（会锁到本曲尾音）⇒ 中止整场"
                                + "（游戏不会停，剩余曲目照常播放但不再注入）");
                    }
                    prevZeroEpoch = zeroEpoch;
                    prevLenMs = lenMs;
                    lastEndEpochMs = nextBase;
                    RunLog.line(String.format(Locale.ROOT,
                            "第 %d 首注入中途失败，但零点已知（距现在 %+.0f ms）"
                                    + " ⇒ 下一首的下界仍然有效，继续", i + 1,
                            zeroEpoch - System.currentTimeMillis()));
                }
            }
            RunLog.phase("会话结束");
        } catch (Throwable t) {
            RunLog.phase("失败");
            final String why = ShizukuProbe.brief(t);
            RunLog.line("!! 课题模式中止：" + why);
            // ★ 2026-10-28：原因落盘 + 弹 Toast（"当时看得见" + "事后查得到"，缺一不可）
            appendCourseAbort("中止：" + why + "（已完成 " + done + " 首）");
            toast(CourseAbort.toastText(why, done));
        } finally {
            if (tap != null) {
                try {
                    tap.stop();
                } catch (Throwable ignore) {
                    // 停采集失败不该盖掉真正的错误
                }
            }
            // ★ 必须清"运行中"标记（2026-10-28 15:1x 实测 bug）：
            //   课程结束只调了 finish()，漏了 running=false ⇒ 界面永远显示"运行中"、
            //   并且会拦住下一轮（用户看到的就是"一直显示有一条在跑，其实没在跑"）。
            RunLog.setRunning(false);
            releaseWake();
            finish();
        }
    }

    /**
     * 课堂模式预派：用**上一次测到的课程 lead** 提前派发曲首 {@link #PREROLL_WINDOW_MS} 内的事件。
     *
     * 为什么必须这么干（2026-10-28）：课题模式的零点只能等闸门触发才知道，
     * 而闸门触发的那一瞬间，落在 `sil_ref` 附近的**盲区音符已经过去了** ⇒ 必丢。
     * 单曲模式用"两段锚"解决同一问题；这里没有点击锚，就用**上一轮实测的 lead** 当锚。
     *
     * ⚠ 预派的时刻完全依赖存下来的 lead：lead 估计错多少，这几个音符就偏多少。
     *   本轮闸门触发后会重新校准并存回（所以第 2 轮起才准；第 1 轮只负责量）。
     */
    private void preRollFirstEvents(Job job, double predZeroMs, double windowMs) {
        if (job.cmds == null || job.cmds.isEmpty() || job.proxy == null || job.inject == null) {
            return;
        }
        List<Scheduler.Cmd> head = new ArrayList<>();
        for (Scheduler.Cmd c : job.cmds) {
            if (c.tMs() < windowMs) {
                head.add(c);
            } else {
                break;
            }
        }
        if (head.isEmpty()) {
            return;
        }
        try {
            DevRun pre = new DevRun(job.proxy, job.inject, true, false, 8,
                    RunLog::line, () -> stopFlag, InjectMode.ASYNC);
            long t0 = System.nanoTime();
            pre.run(head, predZeroMs);
            RunLog.line(String.format(Locale.ROOT,
                    "预派完成：%d 条（耗时 %.1f s，预测零点距现在 %+.0f ms）",
                    head.size(), (System.nanoTime() - t0) / 1e9,
                    predZeroMs - System.currentTimeMillis()));
        } catch (Throwable t) {
            RunLog.line("预派失败（不影响主流程）：" + ShizukuProbe.brief(t));
        }
    }

    /**
     * 开跑后 ~6.5 s 用互相关复核零点，通过 `RezeroGate` 认为可信就 `DevRun.rezero()` 纠正剩余事件。
     *
     * 为什么（2026-10-27/28）：
     *  · gate 定的零点有 **±25 ms 轮间抖动**（同曲 Good 数 1~115 跳），单靠常数 trim 治不了；
     *  · 互相关在真实数据上精度 **+4.2 ms**（真机包络验证），但**会自信地锁错**——实测有一轮
     *    `retributioncycleofredemption_in` 偏了 2 秒而 cc 仍 0.913；
     *  ⇒ 只在 `|Δ| ≤ 200 ms`（{@link RezeroGate}）时采纳：最坏情况退化成"没修"，不会改坏。
     *
     * ⚠ 已经派发的事件不受影响（`rezero` 只换后续事件的时间基准），
     *   所以第一音符早于 ~6.5 s 的曲子（如 schneid 3.2 s）开头几个音符仍按 gate 零点打。
     */
    /**
     * rezero 总开关。
     *
     * ★ 2026-10-28 13:1x 结论：**默认关闭**。
     *   `K = 闸门帧 − 互相关起点` 已经把互相关的信息用掉了；再用互相关去纠正零点
     *   等于把同一个量算两遍。而且互相关的偏置是**每首曲不同**的
     *   （实测 distort 偏 ~65 ms、玩具几乎不偏），一个全局常数永远对了这首、坏了别首。
     *   三轮实测（13:0x / 13:1x）：带 rezero 的 Good 数是 13/15/66 与 69/805/1，
     *   而**不加 rezero 的纯 gate+trim 是 3/0/1**（2986209）。
     *   代码保留，等有一个"无偏且够快"的起音估计再打开。
     */
    private static final boolean REZERO_ENABLED = false;

    private void startRezeroWatcher(Job job, OnsetTap tap, long elapsed0, long epoch0,
                                    double zeroGateMs, double trimMs, int idx) {
        if (!REZERO_ENABLED) {
            return;
        }
        if (job.song.envB64.isEmpty() || job.song.envHopMs <= 0) {
            return;
        }
        final double when = zeroGateMs + REZERO_AT_MS;
        Thread th = new Thread(() -> {
            try {
                long waitMs = (long) (when - System.currentTimeMillis());
                if (waitMs > 0) {
                    Thread.sleep(waitMs);
                }
                for (int i = 0; i < 100 && job.brain == null; i++) {
                    Thread.sleep(20);                 // inject() 里才建 brain
                }
                if (job.brain == null || stopFlag) {
                    return;
                }
                double[] rec = tap.envelope();
                if (rec.length < 600) {
                    RunLog.line("rezero：包络只有 " + rec.length + " 帧 ⇒ 跳过");
                    return;
                }
                long f0 = tap.firstFrameMidNs();
                if (f0 == 0) {
                    return;
                }
                CourseStart.Hit h = CourseStart.locate(rec, job.song.envRms(),
                        job.song.envHopMs, -1, 0);
                if (!h.ok) {
                    RunLog.line("rezero：互相关不可信（" + h + "）⇒ 跳过");
                    return;
                }
                // ★ 互相关的结果**同样**要减 trim（2026-10-28 12:4x 实测教训）：
                //   gate 零点 = 闸门值 − trim；若相关零点不减 trim，二者天然差 trim(35~54)，
                //   rezero 就会把标定好的基线整个推后 ⇒ 结算变成「全 Late、Good 数量级回到 trim=0 时代」。
                //   减掉之后，Δ 只反映"这一轮的抖动"，正是我们要修的。
                double zeroCorr = epoch0 + (f0 - elapsed0) / 1e6 + h.startMs
                        - job.lDevS * 1000.0 - trimMs - REZERO_CORR_BIAS_MS;
                String why = RezeroGate.why(zeroGateMs, zeroCorr);
                if (why != null) {
                    RunLog.line("rezero：" + why);
                    return;
                }
                job.brain.rezero(zeroCorr);
                RunLog.line(String.format(Locale.ROOT,
                        "rezero：第 %d 首按互相关修正 %+.0f ms（gate→相关，cc=%.3f 质量=%.2f）",
                        idx + 1, zeroCorr - zeroGateMs, h.cc, h.quality));
            } catch (Throwable t) {
                RunLog.line("rezero 失败：" + ShizukuProbe.brief(t));
            }
        }, "pg-rezero");
        th.setDaemon(true);
        th.start();
    }

    /**
     * 课题模式第 2/3 首的零点：**用母带指纹互相关定曲首**，不依赖"第一个响点"。
     *
     * 为什么（2026-10-27）：曲间过场一直在响 ⇒ OnsetGate 要么等不到 300 ms 静音，
     * 要么认到过场音上（零点偏 1~3 s，全篇 Good）。而每首的 `env_b64`（前 30 s 10 ms RMS）
     * 就在包里 —— 相关一下就能量出「母带 t=0 落在捕获窗口哪里」，与"谁先响"无关。
     *
     * 判据：`CourseStart` 的 cc/quality 门槛（与 K 标定第二档一致）+ 位置窗口校验。
     * 轮询 250 ms；等不到就继续等（总超时 {@link #COURSE_WAIT_MS}）。
     */
    private double zeroByCorrelation(Job job, OnsetTap tap, long elapsed0, long epoch0,
                                     long notBeforeNs, int idx) throws Exception {
        return zeroByCorrelation(job, tap, elapsed0, epoch0, notBeforeNs, idx, COURSE_WAIT_MS);
    }

    private double zeroByCorrelation(Job job, OnsetTap tap, long elapsed0, long epoch0,
                                     long notBeforeNs, int idx, int timeoutMs) throws Exception {
        final int hop = job.song.envHopMs > 0 ? job.song.envHopMs : 10;
        final double[] ref = job.song.envRms();
        final long deadline = System.currentTimeMillis() + timeoutMs;
        final double nbEpoch = epoch0 + (notBeforeNs - elapsed0) / 1e6;
        double lastCc = -1;
        int rounds = 0;
        while (System.currentTimeMillis() < deadline) {
            if (stopFlag) throw new ZeroSource.Stopped("用户中止");
            if (tap.frames() >= 600) {                 // 至少 6 s 才有意义
                double[] rec = tap.envelope();
                CourseStart.Hit h = CourseStart.locate(rec, ref, hop, -1, 0);
                lastCc = h.cc;
                rounds++;
                if (h.ok) {
                    final long f0 = tap.firstFrameMidNs();
                    if (f0 == 0) {
                        Thread.sleep(50);
                        continue;
                    }
                    final double zero = epoch0 + (f0 - elapsed0) / 1e6 + h.startMs
                            - job.lDevS * 1000.0;
                    // 窗口校验：曲首不该早于"上一首末 + 5 s"太多，也不该拖出 20 s
                    final double hi = idx == 0 ? 45_000 : 20_000;
                    if (zero >= nbEpoch - 2000 && zero <= nbEpoch + hi) {
                        RunLog.line(String.format(Locale.ROOT,
                                "第 %d 首：互相关定曲首 ⇒ start=%.0f ms（cc=%.3f 质量=%.2f）"
                                        + " ⇒ zero 距现在 %+.0f ms",
                                idx + 1, h.startMs, h.cc, h.quality,
                                zero - System.currentTimeMillis()));
                        return zero;
                    }
                    RunLog.line(String.format(Locale.ROOT,
                            "第 %d 首：互相关位置不合理（zero 距下界 %+.0f ms）⇒ 继续等",
                            idx + 1, zero - nbEpoch));
                }
            }
            Thread.sleep(250);
        }
        throw new IllegalStateException("第 " + (idx + 1) + " 首互相关定不到曲首（超时，跑了 "
                + rounds + " 轮，最后 cc=" + String.format(Locale.ROOT, "%.3f", lastCc)
                + "；帧数=" + tap.frames() + "）"
                + (tap.error() == null ? "" : "：" + tap.error()));
    }

    /** 会话用的倒计时（可被"停止"打断）。 */
    private void countdown(int seconds) throws ZeroSource.Stopped {
        RunLog.phase("倒计时 " + seconds + " s");
        long end = System.currentTimeMillis() + seconds * 1000L;
        int last = Integer.MIN_VALUE;
        while (true) {
            if (stopFlag) throw new ZeroSource.Stopped("倒计时期间中止");
            long left = end - System.currentTimeMillis();
            if (left <= 0) break;
            int s = (int) Math.ceil(left / 1000.0);
            if (s != last) {
                last = s;
                // ★ 2026-11-01：逐秒只更新阶段（顶部大字 + 通知栏），**不写日志行**。
                //   以前这里每秒 line 一次 ⇒ 界面只显示最后 16 行，等于一半被倒计时占掉。
                RunLog.phaseQuiet("倒计时 " + s + " s");
            }
            SystemClock.sleep(Math.min(200L, Math.max(1L, left)));
        }
    }

    private void runOnce(String songKey, int countdownS, Double leadOverride,
                         String zeroMode, double lDevS, int resultCode,
                         Intent resultData) {
        RunLog.setRunning(true);
        RunLog.clear();
        RunLog.line("==== 本轮开始：真注入"
                + " · 零点=" + zeroMode
                + " · 注入模式=" + InjectMode.name(InjectMode.ASYNC));
        // ★ 2026-10-30：`job` 必须声明在 try **之外** —— 否则异常跳到 catch 后，
        //   finally 里就够不到它，也就收不掉 tap（AudioRecord + MediaProjection）。
        Job job = null;
        try {
            acquireWake();
            job = prepare(songKey, zeroMode, lDevS, leadOverride,
                    resultCode, resultData);
            arm(job);
            // ★ 必须先构造零点源：预派分支要 `instanceof OnsetZero` 才成立，
            //   而原来 zeroSrc 是 zeroOf() 内部才创建的 ⇒ 判断时恒为 null（bug）。
            buildZeroSource(job, countdownS);
            if (job.preRoll && ZERO_ONSET.equals(job.zeroMode)
                    && job.zeroSrc instanceof OnsetZero) {
                runPreRoll(job, countdownS);
            } else {
                double zero = zeroOf(job, countdownS);
                RunLog.phase("收到零点");
                RunLog.line(String.format(Locale.ROOT,
                        "收到零点：src=%s，距现在 %+.0f ms（zero_dev_ms=%.3f）",
                        job.zeroSrc.src(), zero - System.currentTimeMillis(), zero));
                inject(job, zero);
                appendZeroMeta(job);
                recordLead(job);
            }
            if (job.autoCalib && job.tap != null) finishAutoCalib(job);
        } catch (ZeroSource.Stopped s) {
            RunLog.phase("已停止");
            RunLog.line("已停止（" + s.getMessage() + "）⇒ 未注入任何事件");
        } catch (Throwable t) {
            RunLog.phase("失败");
            RunLog.line("!! 中止：" + ShizukuProbe.brief(t));
        } finally {
            // ★ 2026-10-30：无条件收掉采集器（AudioRecord + MediaProjection）。
            //   为什么必须在这里：`keepTapAlive(auto)` 让 OnsetZero.finish() 的 finally **跳过** stop，
            //   而全进程另一处 `job.tap.stop()` 在 finishAutoCalib()（try 块末行）——
            //   任何异常/中止（零点超时、inject 抛、Shizuku 挂、用户点停止）都会跳过那一行。
            //   漏掉的后果：AudioRecord 不 release（占着麦克风）、MediaProjection 不 stop
            //   （状态栏一直显示录屏），一直挂到进程重建 ⇒ 下一轮 new OnsetTap 可能失败 ⇒ 零注入。
            //   `stop()` 幂等、可从任意线程调（见 OnsetTap:363），重复调用是 no-op。
            if (job != null && job.tap != null) {
                try {
                    job.tap.stop();
                } catch (Throwable ignore) {
                    // 释放失败不能盖掉真正的中止原因（上面已经打过日志）
                }
            }
            RunLog.setRunning(false);
            releaseWake();
            finish();
            RunLog.line("==== 本轮结束（进程仍在，日志留在内存里）");
        }
    }


    /**
     * 端内算 `K = E − S`（S2）：用 assets 里的母带指纹与刚采到的包络做互相关。
     *
     * ★ 先验符号：`lag` 的定义是 `rec[t] ↔ ref[t+lag]`，而 `S = −lag×hop`
     *   ⇒ 期望 `lag = −E/hop = silRefFrames − onsetFrame`（负值）。第一版写反，replay 直接 cc=−0.48。
     * ★ 不 ok 就**不写** K（宁可继续用常数，也不许把垃圾写进偏好）。
     */
    private String computeKOnDevice(SongLib.Song song, OnsetTap tap, double[] ms, byte[] u8) {
        long ff = tap.gate().firedFrame();
        if (ff <= 0) {
            RunLog.line("⚠ 本轮门没锁定（firedFrame=-1）⇒ 算不出 K，这次标定只留包络");
            return "";
        }
        if (song.envB64.isEmpty() || song.envHopMs <= 0) {
            RunLog.line("⚠ 本曲没有母带指纹（env_b64 为空）⇒ 只留包络，不算 K");
            return "";
        }
        try {
            if (u8.length < MIN_K_FRAMES) {
                RunLog.line("⚠ 算 K 的包络只有 " + u8.length + " 帧（< " + MIN_K_FRAMES
                        + "，即 <12 s）⇒ 不写 K（截断的捕获会算出离谱的 K）");
                return "k_ms=none gate=short";
            }
            // ★ G4（音频连续性）：帧数闸门只看"采够了"，看不出"中途断了又续上"。
            //   起音后 3 s 内出现 ≥0.8 s 静音 ⇒ 判中断，这一轮的 K 一律不写。
            CalibGate.Verdict gap = CalibGate.checkGap(ms, ff);
            if (!gap.ok) {
                RunLog.line("⚠ G4 标定连续性不过：" + gap.why
                        + " ⇒ 判为捕获中断（来电/暂停），本轮不写 K，请重标一次");
                return "k_ms=none gate=" + gap.why;
            }
            double[] rec = EnvCodec.decode(u8);
            double[] ref = song.envRms();
            int center = (int) Math.round(song.silRefS * 1000.0 / song.envHopMs) - (int) ff;
            EnvMatcher.Result m = EnvMatcher.match(rec, ref, center, 100);
            double eMs = ff * 10.0 - song.silRefS * 1000.0;
            double sMsRaw = m.startMs(song.envHopMs);
            double kRaw = eMs - sMsRaw;
            // ★★ 2026-10-26：**弱峰但不是垃圾** —— 分两档，而不是一刀切拒收。
            //   真机数据：`destruction321_at` cc=0.855/quality=1.07（只差 quality）；`chronostasis_in`
            //   cc=0.798/quality=1.03（两项都只差一点）。它们其实都找到了峰，只是"峰的唯一性"略低，
            //   而 K 的合理性还有下游三道闸（|K|≤150、G5 交叉、LeadAudit）兜着 ⇒ 允许第二档：
            //   `cc ≥ 0.70 && quality ≥ 1.00 && |K| ≤ 150 ms` 就采纳，并标 `weak=1` 供审计。
            boolean weak = !m.ok && m.cc >= 0.70 && m.quality >= 1.00
                    && Math.abs(kRaw) <= MAX_ABS_K_MS;
            if (!m.ok && !weak) {
                RunLog.line("⚠ 端内互相关不可信（" + m + "）⇒ 不写 K，仍用常数");
                // ★ 2026-10-26：被拒也要把 cc/质量/k 带出去 —— 否则"为什么不可信"在产物里永远查不到
                //   （用户报 `destruction321_at` / `chronostasis_in` 测不出 K，侧车只有一句 gate=xcorr）。
                markKUnreliable(song);
                return String.format(Locale.ROOT,
                        "k_ms=none gate=xcorr cc=%.3f quality=%.2f k_meas=%.1f",
                        m.cc, m.quality, kRaw);
            }
            if (weak) {
                RunLog.line(String.format(Locale.ROOT,
                        "⚠ 互相关峰偏弱（cc=%.3f quality=%.2f < 0.80/1.10），但 |k|=%.1f ms 合理 ⇒ 采纳（weak=1）",
                        m.cc, m.quality, kRaw));
            }
            double sMs = m.startMs(song.envHopMs);
            // ★ lead 事后对账（LeadAudit）：用同轮的 S 反查本轮 lead 样本；
            //   样本坏 ⇒ 回滚，均值坏 ⇒ 用它重置。（2026-10-25 真机：同曲两轮 lead 差 503 ms）
            final int leadAudit = auditLead(song, sMs);
            // ★ G5（交叉校验）：互相关可能锁到**别的 App 的声音**（内录会把它们一起录进来）。
            //   口径：`S ≈ lead`（两条路径的 c 都抵消，详见 CalibGate 注释）；没有 lead 时退化成物理区间。
            //   ⚠ 这一条专门抓"错 50 ms 以上但看起来 cc 很漂亮"的那种样本。
            // ★ 先算 K 再判闸门：拒收时也要把**原始测量值**带出去。
            //   否则"被拒的样本"在产物里连数都没有（只有一句 gate=…），
            //   A0「连打几轮量 K 的真实散布」就永远缺掉被拒那一半（2026-10-25 20:2x 发现）。
            double kMs = eMs - sMs;
            CalibGate.Verdict cross = CalibGate.checkCross(sMs, leadOrNull(song));
            boolean crossRelaxed = false;
            if (!cross.ok) {
                // ★★ 2026-10-26：**K 只由音频决定**（E 来自闸门帧号、S 来自互相关，两者都在音频时间轴上），
                //   而 `lead` 是"墙上时钟"测的弱信号（受闸门触发点影响）。真机数据里出现过
                //   `destruction321_at`：cc/quality 都很好、`k_meas=-4.9 ms` 非常合理，但 `d=+220 ms`
                //   把 K 永久拦下（用户报"无法测 K"）。⇒ 只要 **|k_meas| 落在物理范围**内，
                //   就认为音频自洽、采纳 K，并标记 `gate=ok_cross_relaxed`（可事后审计）。
                if (Math.abs(kMs) <= MAX_ABS_K_MS) {
                    crossRelaxed = true;
                    RunLog.line(String.format(Locale.ROOT,
                            "⚠ G5 交叉校验不过（%s），但 |k|=%.1f ms 在物理范围内 ⇒ 采纳本轮 K（标记 ok_cross_relaxed）",
                            cross.why, kMs));
                } else {
                    RunLog.line("⚠ G5 标定交叉校验不过：" + cross.why
                            + " ⇒ 疑似锁到别的声音（UI 音效/别的 App 出声），本轮不写 K");
                    markKUnreliable(song);
                    return String.format(Locale.ROOT, "k_ms=none k_meas=%.1f gate=%s lead_audit=%s",
                            kMs, cross.why, LeadAudit.tag(leadAudit));
                }
            }
            if (Math.abs(kMs) > MAX_ABS_K_MS) {
                // ★ 闸门：实测 K 全在 −7~+53 ms；|K|>150 只会是互相关错配（截断/错曲），
                //   而错配的 K 会让整曲偏几百毫秒 —— 宁可这次不写。
                RunLog.line(String.format(Locale.ROOT,
                        "⚠ K=%.1f ms 超出物理范围（|K|≤%.0f）⇒ 判为错配，不写 K（cc=%.3f 质量=%.2f）",
                        kMs, MAX_ABS_K_MS, m.cc, m.quality));
                markKUnreliable(song);
                return String.format(Locale.ROOT, "k_ms=none k_meas=%.1f gate=range", kMs);
            }
            // ★ 按**曲名**存，不按 key：key 里带难度（cristalisia_ez / cristalisia），
            //   但同一首曲不同难度的母带与首音是同一个 ⇒ K 只与「曲」有关。
            //   同时写一份按 key 的（兼容旧数据），读的时候两个都认。
            // ★ 再测一次**不会**覆盖旧值，而是并入滑动平均（详见 kAvgPut）。
            clearKUnreliable(song);
            CalibMerge.Out fusion = kAvgPut(song, kMs, m.cc);
            float merged = fusion.value;
            RunLog.line(String.format(Locale.ROOT, "K 已并入平均：本次 %.1f → 平均 %.1f ms", kMs, merged));
            float c = cUsed();
            RunLog.line(String.format(Locale.ROOT,
                    "端内标定：S=%.1f ms  E=%.1f ms ⇒ K=%.1f ms（cc=%.3f 质量=%.2f）",
                    sMs, eMs, kMs, m.cc, m.quality));
            RunLog.line(String.format(Locale.ROOT,
                    "已存 K；勾选「自动 L_dev」时下轮用 %.1f + %.0f = %.3f s",
                    merged, c, (merged + c) / 1000.0));
            // ★ G2 的收口：这一轮标定成功 ⇒ 当前设备就是"有效设备"，把指纹写回并解除作废状态。
            //   否则换了设备的人会永远卡在"旧 K 作废"，每次重标都被下一次启动再作废一遍。
            if (fpMismatch) {
                putVerifiedStr(FP_KEY, fpNow());
                fpMismatch = false;
                RunLog.line("设备指纹已更新为当前设备 ⇒ 之后可以用这次标定的 K");
            }
            return String.format(Locale.ROOT,
                    "k_ms=%.1f cc=%.3f quality=%.2f s_ms=%.1f e_ms=%.1f c_ms=%.0f %s k_after=%.1f k_n=%d lead_audit=%s%s",
                    kMs, m.cc, m.quality, sMs, eMs, c, cross.why, merged, fusion.n,
                    LeadAudit.tag(leadAudit),
                    (crossRelaxed ? " cross_relaxed=1" : "") + (weak ? " weak=1" : ""));
        } catch (Throwable t) {
            RunLog.line("⚠ 端内算 K 失败：" + ShizukuProbe.brief(t) + " ⇒ 不写 K");
            return "k_ms=none gate=exception";
        }
    }

    /**
     * 课题模式**事后审计**：用当轮包络做一次只读互相关，把 `K_round = E − S` 与缓存 K 的差落盘。
     *
     * ★★ 为什么需要（2026-10-29）：课题模式的 Good 部分**全部**来自 δ（整首零点偏移），
     *   而
     *   <pre>
     *   onset = sMs + K        (K = eMs − sMs)
     *   ⇒ onset − eMs = K_stored − K_round
     *   </pre>
     *   ⇒ **δ ≡ K_stored − K_round**（代数恒等，不是推测）。
     *   但课题模式**从不调用** {@link #computeKOnDevice}（那是标定流程专属）⇒ 连续 5 轮
     *   `k_*.txt` 一个都没落盘，δ 抖动的来源完全无记录。
     *   本次只补观测：**不写偏好、不改零点、不改 trim、不抛异常**。
     *
     * ⚠ **时机受限**：{@code OnsetTap.rearm()} 里有 {@code envN = 0}，下一首 rearm 会清空本首
     *   包络 ⇒ 只能在本首注入结束后、下一首 rearm 之前调用（曲间实测 9.7 s，互相关是毫秒级）。
     *
     * ⚠ **`ff` 必须由调用方在 inject 之前取好**：`startRezeroWatcher` 可能改 gate，
     *   注入跑完再读 {@code gate().firedFrame()} 拿到的未必还是本首的。
     *
     * ⚠ 不复用 `k_*.txt` 前缀：那一族被理解为「K 标定样本」，混进审计记录会让"K 标得稳不稳"
     *   这个老问题重新变得不可读。
     */
    private void auditKRound(SongLib.Song song, OnsetTap tap, int idx, long ffAbs, long ffAtNs) {
        final long t0 = System.currentTimeMillis();
        final int have = tap.frames();
        try {
            if (ffAbs <= 0 || ffAtNs <= 0) {
                RunLog.line("ⓘ 第 " + (idx + 1) + " 首审计：起音未锁定（frame=" + ffAbs
                        + ", atNs=" + ffAtNs + "）⇒ 跳过");
                writeKauditSkip(song, idx, ffAbs, have, "no_fire", t0);
                return;
            }
            if (song.envB64.isEmpty() || song.envHopMs <= 0) {
                RunLog.line("ⓘ 第 " + (idx + 1) + " 首审计：本曲无母带指纹 ⇒ 跳过");
                writeKauditSkip(song, idx, ffAbs, have, "no_ref", t0);
                return;
            }
            // ★★ 把"检测器累计帧号"换算成"本首帧号"（2026-10-29 第二轮真机 bug）。
            //   `OnsetTap` 里 `frameIdx` 由检测器自增、**不随 rearm 清零**，而 `envN`/`envelope()`
            //   每次 rearm 从 0 开始 ⇒ 第 2 首实测 `ff=19538 > frames=16913`（= 第1首16800+曲间970+1768）。
            //   `firstFrameMidNs` 在 rearm 里重置、在第一帧时赋值，正是「本首第 0 帧的时刻」。
            final long firstNs = tap.firstFrameMidNs();
            if (firstNs <= 0) {
                RunLog.line("ⓘ 第 " + (idx + 1) + " 首审计：还没收到过帧（firstFrameMidNs=0）⇒ 跳过");
                writeKauditSkip(song, idx, ffAbs, have, "no_first_frame", t0);
                return;
            }
            final int ffRel = (int) Math.round((ffAtNs - firstNs) / 1_000_000.0 / 10.0);
            if (ffRel <= 0) {
                RunLog.line("ⓘ 第 " + (idx + 1) + " 首审计：本首起音帧号异常（" + ffRel + "）⇒ 跳过");
                writeKauditSkip(song, idx, ffRel, have, "ffrel_invalid(" + ffRel + ")", t0);
                return;
            }
            if (ffRel >= have) {
                RunLog.line("ⓘ 第 " + (idx + 1) + " 首审计：起音帧 " + ffRel + " ≥ 总帧 " + have
                        + "（坐标仍不一致）⇒ 跳过");
                writeKauditSkip(song, idx, ffRel, have, "ffrel_ge_frames(" + ffRel + ">=" + have + ")",
                        t0);
                return;
            }
            final double[] ref = song.envRms();
            final int silRefFrames = (int) Math.round(song.silRefS * 1000.0 / song.envHopMs);
            // ★★ 只取「起音前 sil_ref + 30 s」——与母带指纹同长（`env_b64` 只有 3000 帧 = 30 s）。
            //   第二轮真机 bug：把整首 16934 帧喂进去 ⇒ `minOverlap = n/4 = 4233 > m = 3000`
            //   ⇒ `hi = m − minOverlap < 0` ⇒ 必报"先验窗越界"；即使不报（无先验轮），
            //   重叠要求也超过母带长度 ⇒ cc 只有 0.699。
            //   ⚠ `eMs` 用**截取坐标系**（`ffRel − start`），而 `k_round = eMs − sMs` 是差值，
            //     坐标系平移不影响它。
            final int start = Math.max(0, ffRel - silRefFrames);
            final int want = 3000 + 2 * silRefFrames + 200;   // 母带 30 s + 前置 + 余量
            final int end = Math.min(have, start + want);
            final int n = end - start;
            if (n < MIN_K_FRAMES) {
                RunLog.line("ⓘ 第 " + (idx + 1) + " 首审计：可用包络只有 " + n + " 帧（< "
                        + MIN_K_FRAMES + "）⇒ 不够算，跳过");
                writeKauditSkip(song, idx, ffRel, have, "short_window(n=" + n + ")", t0);
                return;
            }
            final double[] recFull = tap.envelope();
            final double[] rec = java.util.Arrays.copyOfRange(recFull, start, end);
            // ★★ 第三轮结论：**不传先验**（center = −1）。
            //   离线自测（tools/kaudit_offline.py，用 out/env_destruction321_at_132254.csv）实测：
            //     center = ff − silRef = +751  ⇒ s_ms = −7020（错 14.5 s），cc = 0.782 ❌
            //     center = −1（不传）          ⇒ s_ms = 7510.0（真值 7509.9），cc = 0.996 ✅
            //   `EnvMatcher` 期望的 lag 是**负数**，而它只在 centerFrames ≥ 0 时启用先验
            //   ⇒ 这个参数天生无法表达负 lag。截短 rec 后全范围搜索只要 ~4000 个 lag，够快。
            final EnvMatcher.Result m = EnvMatcher.match(rec, ref, -1, 100);
            final double eMs = (ffRel - start) * 10.0 - song.silRefS * 1000.0;
            final double sMs = m.startMs(song.envHopMs);
            final double kRound = eMs - sMs;
            final float kStored = kStored(song);
            final boolean weak = !m.ok && m.cc >= 0.70 && m.quality >= 1.00
                    && Math.abs(kRound) <= MAX_ABS_K_MS;
            final long cost = System.currentTimeMillis() - t0;
            // 诊断：`n/m/min_overlap` 三者同框才能一眼看出 B2 是否复发（n/4 > m ⇒ 必然越界）。
            final int mFrames = ref.length;

            String storedStr = Float.isNaN(kStored)
                    ? "NaN" : String.format(Locale.ROOT, "%.1f", kStored);
            String deltaStr = Float.isNaN(kStored)
                    ? "NaN" : String.format(Locale.ROOT, "%.1f", kStored - kRound);
            final String line = song.key + "," + (idx + 1) + "," + ffRel + "," + have
                    + "," + n + "," + mFrames
                    + "," + String.format(Locale.ROOT, "%.3f", m.cc)
                    + "," + String.format(Locale.ROOT, "%.2f", m.quality)
                    + "," + String.format(Locale.ROOT, "%.1f", sMs)
                    + "," + String.format(Locale.ROOT, "%.1f", eMs)
                    + "," + String.format(Locale.ROOT, "%.1f", kRound)
                    + "," + storedStr
                    + "," + deltaStr
                    + "," + (m.ok ? 1 : 0)
                    + "," + (weak ? 1 : 0)
                    + "," + (m.ok || weak ? "ok" : m.why.replace(',', ';'))
                    + "," + cost;
            writeKauditFile(song, idx, ffRel, have, line);
            RunLog.line(String.format(Locale.ROOT,
                    "审计 第 %d 首：K_round=%.1f 缓存=%s ⇒ δ_pred=%.1f ms（cc=%.3f 质量=%.2f，n=%d/%d，%d ms）",
                    idx + 1, kRound, storedStr,
                    Float.isNaN(kStored) ? Double.NaN : kStored - kRound,
                    m.cc, m.quality, n, mFrames, cost));
            if (cost > 200) {
                // 曲间富余 9.7 s，真超 200 ms 说明设备在忙 —— 值得留痕，但它不改任何时基。
                RunLog.line("⚠ 审计耗时 " + cost + " ms（>200）⇒ 曲间可能被压缩，留意下一首");
            }
        } catch (Throwable t) {
            // 审计失败绝不许影响注入主流程
            RunLog.line("⚠ 第 " + (idx + 1) + " 首审计失败：" + ShizukuProbe.brief(t) + "（不影响注入）");
            writeKauditSkip(song, idx, ffAbs, have, "exception:" + ShizukuProbe.brief(t), t0);
        }
    }

    /**
     * 审计**跳过**也要落盘（2026-10-29 第二轮真机教训）。
     *
     * 第一版只把跳过原因写进 `RunLog` —— 而 RunLog 只在界面显示、**不落文件** ⇒
     * 第 2/3 首一个 `kaudit` 都没生成时，只能从"文件不存在"反推"某处 return 了"，
     * 到底是帧号无效、没母带指纹、还是帧数不够，**完全查不到**。
     * ⇒ 规矩：任何一条"没算"的路径都必须留下一行带原因的记录。
     */
    private void writeKauditSkip(SongLib.Song song, int idx, long ff, int have,
                                 String why, long t0) {
        final long cost = System.currentTimeMillis() - t0;
        // 17 列与表头对齐；数值列一律 `-`（= 该步没算出），why 写原因
        final String line = String.format(Locale.ROOT,
                "%s,%d,%d,%d,-,-,-,-,-,-,-,-,-,-,-,%s,%d",
                song.key, idx + 1, ff, have,
                why == null ? "unknown" : why.replace(',', ';').replace('\n', ' '), cost);
        writeKauditFile(song, idx, ff, have, line);
    }

    /** 写一个 `kaudit_<key>_<stamp>.csv`（表头 + 一行）。失败只记日志，绝不抛。 */
    private void writeKauditFile(SongLib.Song song, int idx, long ff, int have, String line) {
        try {
            File dir = getExternalFilesDir(null);
            if (dir == null) {
                RunLog.line("ⓘ 第 " + (idx + 1) + " 首审计：拿不到外部目录 ⇒ 无法落盘");
                return;
            }
            String stamp = new java.text.SimpleDateFormat("HHmmss", Locale.ROOT)
                    .format(new java.util.Date());
            File f = new File(dir, "kaudit_" + song.key + "_" + stamp + ".csv");
            StringBuilder sb = new StringBuilder();
            sb.append("# kaudit v1  key=").append(song.key)
                    .append("  idx=").append(idx + 1)
                    .append("  ff_rel=").append(ff)
                    .append("  frames=").append(have).append('\n');
            sb.append("# 课题模式事后审计：当轮 gate 起音 vs 互相关起音。只读，不写偏好、不改零点。\n");
            sb.append("# delta_pred = k_stored - k_round ≈ 本曲本轮 δ（整首零点偏移，ms）；正值 = 注入偏晚\n");
            sb.append("# 数值列为 `-` = 该步没算出（why 列写原因）；这类行也必须存在，否则「为什么没数据」无从查起。\n");
            sb.append("# n_frames/ref_frames：截取后的捕获长度 vs 母带指纹长度。n/4 > ref ⇒ 必然越界（2026-10-29 实测）。\n");
            sb.append("key,idx,ff_rel,frames,n_frames,ref_frames,cc,quality,s_ms,e_ms,k_round,k_stored,delta_pred,ok,weak,why,cost_ms\n");
            sb.append(line).append('\n');
            try (FileOutputStream fo = new FileOutputStream(f)) {
                fo.write(sb.toString().getBytes(StandardCharsets.UTF_8));
                fo.flush();
            }
        } catch (Throwable t) {
            RunLog.line("⚠ kaudit 落盘失败（第 " + (idx + 1) + " 首）：" + ShizukuProbe.brief(t));
        }
    }

    /** 该曲的 lead 均值；没有（或 G2 判设备变了）返回 null —— 供 G5 的交叉校验用。 */
    /**
     * 标记「本曲的 K 互相关不可靠」（2026-10-26）。
     *
     * 用途：面板据此显示「K 未标定（互相关不可靠 ⇒ K 按 0 处理）」，而不是让用户以为坏了。
     * ⚠ **不写 K=0**：0 与"未标定"在注入上完全等价（`L_dev` 都退到 `c`），但写 0 会污染滑动平均，
     *   让后面真正测到的好样本被离群闸门拒掉。
     */
    private void markKUnreliable(SongLib.Song song) {
        try {
            putVerifiedInt("kbad_" + song.name, 1);
        } catch (Throwable ignored) {
            // 标记失败不影响标定流程本身
        }
    }

    private void clearKUnreliable(SongLib.Song song) {
        try {
            putVerifiedInt("kbad_" + song.name, 0);
        } catch (Throwable ignored) {
        }
    }

    /**
     * **lead 事后对账**（2026-10-25 21:4x，`LeadAudit`）：用同一轮标定算出的 `S` 反查本轮 lead 样本。
     *
     * ★ 真机现场（bounded_in 两轮都 φ，但 `zero_rel` 差 503 ms）：本轮样本 7423.5 与 `S=7421.9` 只差 1.6 ms，
     *   而存盘均值 7926.3 才是坏的首样本 ⇒ 旧逻辑（只看"样本 vs 均值"）会把好样本当离群拒掉、
     *   坏均值却一直留着。这里分两种情况：样本坏 ⇒ 回滚；均值坏 ⇒ 用本轮样本重置。
     */
    private int auditLead(SongLib.Song song, double sMs) {
        int verdict = LeadAudit.decide(lastLeadMeas, sMs, leadOrNull(song), CalibGate.CROSS_TOL_MS);
        if (verdict == LeadAudit.OK) return verdict;
        if (verdict == LeadAudit.ROLLBACK) {
            if (leadSnapValid && leadSnapSong == song) {
                if (Float.isNaN(leadSnapValue)) {
                    PrefGuard.remove(prefs(), LEAD_KEY + song.name, LEAD_N_KEY + song.name);
                } else {
                    PrefGuard.putFloat(prefs(), LEAD_KEY + song.name, leadSnapValue);
                    PrefGuard.putInt(prefs(), LEAD_N_KEY + song.name, leadSnapN);
                }
                PrefGuard.putInt(prefs(), "leadside_n_" + song.name, leadSnapSideN);
                PrefGuard.putInt(prefs(), "leadside_sign_" + song.name, leadSnapSideSign);
                RunLog.line(String.format(Locale.ROOT,
                        "⚠ lead 事后对账：本轮样本 %.1f 与 S %.1f 差 %+.1f ms > %.0f ⇒ 回滚该样本（均值回到 %.1f, n=%d）",
                        lastLeadMeas, sMs, lastLeadMeas - sMs, CalibGate.CROSS_TOL_MS,
                        leadSnapValue, leadSnapN));
            }
            return verdict;
        }
        // RESET：本轮样本与 S 相符，坏的是存盘均值 ⇒ 用本轮样本重置
        Double mean = leadOrNull(song);
        PrefGuard.putFloat(prefs(), LEAD_KEY + song.name, (float) lastLeadMeas);
        PrefGuard.putInt(prefs(), LEAD_N_KEY + song.name, 1);
        PrefGuard.putInt(prefs(), "leadside_n_" + song.name, 0);
        PrefGuard.putInt(prefs(), "leadside_sign_" + song.name, 0);
        RunLog.line(String.format(Locale.ROOT,
                "⚠ lead 事后对账：本轮样本 %.1f 与 S %.1f 相符，但存盘均值 %s 差 %s ⇒ 重置均值为本轮样本（n=1）",
                lastLeadMeas, sMs, mean,
                mean == null ? "n/a" : String.format(Locale.ROOT, "%+.1f ms", mean - sMs)));
        return verdict;
    }

    private Double leadOrNull(SongLib.Song song) {
        double v = leadOf(song);
        return Double.isNaN(v) ? null : Double.valueOf(v);
    }

    /**
     * 自动标定收尾：注入跑完后读这轮继续采到的包络，端内算 `K` 存起来（下一轮用）。
     *
     * ★ 为什么放在注入**之后**：包络要 30 s，而注入一开跑就是 150 s ⇒ 采完时注入早就结束了。
     *   本轮的唯一风险是「采集与注入并行会不会扰动注入」，判据在 trace 里：
     *   关键事件 `lag` mean/sd 是否仍与基准 7.07/7.28 ms 同量级。
     * ★ 结果同时写一个 sidecar（`k_<key>_<stamp>.txt`）：端内日志只留 16 行、logcat 会被冲掉、
     *   APK 不可调试 ⇒ 不留产物就等于没有证据。
     */
    private void finishAutoCalib(Job job) {
        try {
            int got = job.tap.awaitFrames(CALIB_FRAMES, 20_000);
            double[] ms = job.tap.envelope();
            if (ms.length < MIN_K_FRAMES) {
                RunLog.line("⚠ 自动标定：只采到 " + ms.length + " 帧（< "
                        + MIN_K_FRAMES + "）⇒ 本轮不算 K");
                return;
            }
            byte[] u8 = EnvCodec.encodeMeanSquare(ms);
            String meta = computeKOnDevice(job.song, job.tap, ms, u8);
            RunLog.line("自动标定：共 " + ms.length + " 帧（" + got + "），" + meta);
            // ★ G10：K 的测量值此刻才存在（注入早结束了）⇒ 只能**追加**到本轮 trace 尾部。
            //   塞进头部就是伪造时序（头部是在注入开始前拼的）。
            appendTraceNote(job, "calib " + meta);
            // ★ 落盘条件放宽（2026-10-25 20:2x）：**被闸门拒收的轮次也要留档**。
            //   原来只写 `k_ms=...` 的成功轮 ⇒ A0「连打几轮量 K 的真实散布」会缺掉被拒那一半，
            //   而"拒收率"正是要判断闸门是否过紧的第一手数据。
            if (!meta.isEmpty()) {
                File dir = getExternalFilesDir(null);
                if (dir != null) {
                    String stamp = new java.text.SimpleDateFormat("HHmmss", Locale.ROOT)
                            .format(new java.util.Date());
                    File f = new File(dir, "k_" + job.song.key + "_" + stamp + ".txt");
                    String line = "song=" + job.song.key + " name=" + job.song.name
                            + " frames=" + ms.length + " " + meta + System.lineSeparator();
                    try (FileOutputStream fo = new FileOutputStream(f)) {
                        fo.write(line.getBytes(StandardCharsets.UTF_8));
                        fo.flush();
                    }
                    RunLog.line("自动标定结果已落盘 " + f.getName());
                }
            }
        } catch (Throwable t) {
            RunLog.line("⚠ 自动标定失败：" + ShizukuProbe.brief(t) + "（不影响本轮成绩）");
        } finally {
            try { job.tap.stop(); } catch (Throwable ignore) { /* 已停 */ }
        }
    }


    /**
     * 把一次新测的 `K` 并入该曲的滑动平均。
     *
     * ★ 为什么不直接覆盖：K 的轮间抖动实测 ±3 ms（47.8~53.2），单次测量带着这个噪声。
     *   直接用最新值，等于把已有信息扔掉、把噪声全吃进去；平均后噪声按 1/√n 下降。
     * ★ 为什么不用"取最好 cc 的那次"：cc 高不代表 K 准（cc 反映形状匹配，K 反映边沿位置），
     *   而平均是唯一能把独立噪声压下去的无偏做法。
     * ★ 上限 10 次：留一点适应性（比如系统大版本升级后捕获链变了），又不至于让旧值永远压着。
     */
    private CalibMerge.Out kAvgPut(SongLib.Song song, double kMs, double cc) {
        String mk = "k_ms_name_" + song.name;
        String ck = "k_n_name_" + song.name;
        String sk = "k_side_n_" + song.name;
        String gk = "k_side_sign_" + song.name;
        float old = prefs().getFloat(mk, Float.NaN);
        int n = prefs().getInt(ck, 0);
        if (Float.isNaN(old) || n <= 0) { old = 0f; n = 0; }
        CalibMerge.Out out = CalibMerge.merge(old, n, kMs,
                prefs().getInt(sk, 0), prefs().getInt(gk, 0), CalibMerge.Policy.forK());
        RunLog.line(String.format(Locale.ROOT, "K 融合：%s（本次 %.1f, cc=%.3f）", out, kMs, cc));
        if (out.accepted) {
            // ★ G1：这几个都走 commit + 回读。`k_n` / `side_*` 漏检的话，融合状态会静默回到
            //   "n=0 首样本"，而均值仍是旧的 ⇒ 下一轮会拿一个失配的 n 算滑动平均。
            putVerified(mk, out.value);
            putVerifiedInt(ck, out.n);
            putVerifiedInt(sk, 0);
            putVerifiedInt(gk, 0);
            putVerified("k_ms_" + song.key, out.value);
        } else {
            putVerifiedInt(sk, out.sideCount);
            putVerifiedInt(gk, out.sideSign);
        }
        return out;
    }

    /**
     * 读该曲的 lead 均值（点 ▶ → 谱面 t=0，ms）。没有历史 ⇒ NaN。
     *
     * ★ 两段锚的预派就靠它：误差直接等于"本轮 lead − 这个均值"，实测 sd ≈ 30 ms。
     */
    private double leadOf(SongLib.Song song) {
        // G2：指纹变了 ⇒ 旧 lead 是别的设备上量的，预派会按错误零点派发（比不派更糟）
        if (fpMismatch) return Double.NaN;
        float v = prefs().getFloat(LEAD_KEY + song.name, Float.NaN);
        return Float.isNaN(v) ? Double.NaN : v;
    }

    /** 把一轮实测 lead 并入滑动平均（口径与 K 相同：上限 10 次后转指数式）。 */
    private CalibMerge.Out leadPut(SongLib.Song song, double leadMs) {
        String mk = LEAD_KEY + song.name;
        String ck = LEAD_N_KEY + song.name;
        String sk = "leadside_n_" + song.name;
        String gk = "leadside_sign_" + song.name;
        float old = prefs().getFloat(mk, Float.NaN);
        int n = prefs().getInt(ck, 0);
        if (Float.isNaN(old) || n <= 0) { old = 0f; n = 0; }
        // ★ 事后对账要能"回滚这一轮"（见 LeadAudit）：写之前先快照四个键
        leadSnapSong = song;
        leadSnapValue = prefs().getFloat(mk, Float.NaN);
        leadSnapN = prefs().getInt(ck, 0);
        leadSnapSideN = prefs().getInt(sk, 0);
        leadSnapSideSign = prefs().getInt(gk, 0);
        leadSnapValid = true;
        CalibMerge.Out out = CalibMerge.merge(old, n, leadMs,
                prefs().getInt(sk, 0), prefs().getInt(gk, 0), CalibMerge.Policy.forLead());
        RunLog.line(String.format(Locale.ROOT, "lead 融合：%s（本次 %.0f）", out, leadMs));
        if (out.accepted) {
            putVerified(mk, out.value);
            putVerifiedInt(ck, out.n);
            putVerifiedInt(sk, 0);
            putVerifiedInt(gk, 0);
        } else {
            putVerifiedInt(sk, out.sideCount);
            putVerifiedInt(gk, out.sideSign);
        }
        return out;
    }

    /**
     * 本轮实际用哪个 `L_dev`：开了自动且该曲存过 `K` ⇒ `(K + c)/1000`，否则用传入值。
     *
     * ★ 为什么允许覆盖启动参数：`--ed l_dev_s` 是**标定阶段**的手动旋钮；进入产品态后
     *   该由「端内标定的 K + 本机 c」决定。覆盖时日志必须写清楚（不然又是一次静默换参）。
     */
    private double effectiveLDev(SongLib.Song song, double lDevS) {
        if (!autoLDev) return lDevS;
        // ★ 先按曲名找（一次标定覆盖该曲所有难度），再退回按 key（旧数据/老版本写的）。
        //   G2：设备指纹不符时 `kStored` 直接给 NaN ⇒ 下面的"未标定"分支；这是刻意行为。
        float k = kStored(song);
        // ★ 2026-10-26：**预置 K** —— 自动标定测不出时（`chronostasis_in` 这类指纹太平的曲子）用它兜底。
        //   K 是**曲目**属性（`c` 才是设备属性），所以按曲预置是物理上成立的；
        //   只有"没测出"才用它，一旦测出真值立刻被覆盖（预置值不进 CalibMerge 平均）。
        if (Float.isNaN(k) && !Double.isNaN(song.kPresetMs) && !fpMismatch) {
            float c0 = cUsed();
            RunLog.line(String.format(Locale.ROOT,
                    "自动 L_dev：本曲未标定，但有预置 K=%+.1f ms ⇒ L_dev=%.3f s",
                    song.kPresetMs, (song.kPresetMs + c0) / 1000.0));
            return (song.kPresetMs + c0) / 1000.0;
        }
        if (Float.isNaN(k)) {
            // ★ 未标定时的兜底必须是 `c`，不能是启动参数（0.050）：13:5x 实测过一首新曲，
            //   第一遍用 0.050 ⇒ 全曲早 170 ms ⇒ **F**；第二遍有 K 就 AP。
            //   物理上：判定偏移 = K_true + (c) − L_dev；用 L_dev=c 时只剩 K_true（硬起音曲 ≈0）。
            float c0 = cUsed();
            RunLog.line(String.format(Locale.ROOT,
                    "自动 L_dev：本曲未标定%s ⇒ 用 c 兜底 L_dev=%.3f s（若这首是渐入曲会偏几十 ms，打一轮自动补 K）",
                    fpMismatch ? "（设备指纹变了，旧 K 作废）" : "", c0 / 1000.0));
            return c0 / 1000.0;
        }
        float c = cUsed();
        double l = (k + c) / 1000.0;
        RunLog.line(String.format(Locale.ROOT,
                "自动 L_dev：K %.1f + c %.0f = %.3f s（覆盖启动参数 %.3f s）", k, c, l, lDevS));
        return l;
    }

    /**
     * G1：**校验过的写盘**（commit + 回读）。
     *
     * ★ 不用 `apply()`：16:14 那次"界面有 lead、服务读到 NaN"疑似就是异步落盘被 force-stop 打断。
     *   标定参数一轮只写几个 float，commit 的 fsync 可忽略。
     * ★ 回读用**精确比较**（float 往返精确），不用 epsilon —— 否则"写成别的值但很接近"会当成成功。
     */
    private boolean putVerified(String key, float v) {
        return failIfPersist("float", key, PrefGuard.putFloat(prefs(), key, v));
    }

    /** G1 的同类：整型也要 commit + 回读（`k_n` / `lead_n` / 连续同侧计数都不能静默丢）。 */
    private boolean putVerifiedInt(String key, int v) {
        return failIfPersist("int", key, PrefGuard.putInt(prefs(), key, v));
    }

    /** G1 的同类：字符串（设备指纹）同样要回读 —— 指纹写丢了会退化成"首跑"而不是"变了"。 */
    private boolean putVerifiedStr(String key, String v) {
        return failIfPersist("string", key, PrefGuard.putString(prefs(), key, v));
    }

    /**
     * 把 `PrefGuard` 的失败变成"本轮可见"：日志 + trace 里的 `persist=fail`。
     *
     * ★ 分工：**判据只有一份**（`PrefGuard`），这里只负责"失败怎么响"。
     *   ⛔ 别再在这里重写一遍回读逻辑 —— 那会让 PlayService 与 Activity 的口径分叉。
     */
    private boolean failIfPersist(String kind, String key, String err) {
        if (err == null) return true;
        RunLog.line("⚠ 参数落盘失败（" + kind + "）：" + key + " · " + err);
        persistFail = true;
        return false;
    }

    /**
     * G13：读 `assets/build_info.txt`（build.sh 在打包时写）。
     *
     * ⛔ 读不到时**必须**回 `unknown`，不许拿 `versionName` 兜底冒充：写死的 `0.1-m1s1d`
     *   对"哪一版代码"没有任何信息量，冒充成功比显示 unknown 更糟（2026-10-25 回退事故）。
     */
    private BuildInfo build() {
        if (build == null) {
            try {
                build = BuildInfo.read(getAssets().open("build_info.txt"));
            } catch (Throwable ignore) {
                // 旧包 / 手搓包没有这个文件：如实显示 unknown
                build = BuildInfo.UNKNOWN;
            }
        }
        return build;
    }

    /** G2：当前设备指纹串（机型|设备名|横屏分辨率）。拿不到尺寸也不抛，退化成 0x0。 */
    private String fpNow() {
        return fpOf(this);
    }

    /**
     * G2 的**唯一实现**（界面横幅与自检都调这里）。
     *
     * ⛔ 别在 Activity 里用 `getResources().getDisplayMetrics()` 另算一份：两处算法一分叉，
     *   横幅就会天天报"指纹不符"，而那是最容易被当成"真的换了设备"的假警报。
     */
    static String fpOf(Context ctx) {
        int[] wh = new int[]{0, 0};
        try {
            DisplayManager dm = (DisplayManager) ctx.getSystemService(Context.DISPLAY_SERVICE);
            Display d = dm == null ? null : dm.getDisplay(Display.DEFAULT_DISPLAY);
            if (d != null) {
                Point p = new Point();
                d.getRealSize(p);
                wh[0] = p.x;
                wh[1] = p.y;
            }
        } catch (Throwable ignore) {
            // 拿不到尺寸 ⇒ 退化成 0x0：宁可指纹弱一点，也不许抛
        }
        return DevFp.of(Build.MANUFACTURER, Build.MODEL, Build.DEVICE, wh[0], wh[1]);
    }

    /**
     * G2：核对设备指纹。
     *
     * 三种状态**必须分开**（混成一个 boolean 就会"首次运行就报警"）：
     *   · 没存过 ⇒ 记下当前值（首跑，正常）；
     *   · 相同 ⇒ 无动作；
     *   · 不同 ⇒ `fpMismatch=true`：旧 K/lead/c 作废。**不自动清数据** ——
     *     标定成功后 {@link #computeKOnDevice} 会把当前指纹写回，一次重标就自动适配。
     */
    private void checkFingerprint() {
        String cur = fpNow();
        String stored = prefs().getString(FP_KEY, "");
        int cmp = DevFp.compare(stored, cur);
        fpMismatch = cmp == DevFp.DIFF;
        if (cmp == DevFp.NONE) {
            putVerifiedStr(FP_KEY, cur);
            RunLog.line("设备指纹：" + (stored == null || stored.isEmpty() ? "首次记录" : "无有效值，记下")
                    + " " + cur);
        } else if (fpMismatch) {
            RunLog.line("⚠ 设备指纹变了：存盘 " + stored + " / 当前 " + cur
                    + " ⇒ 本轮不用旧的 K / lead / c（重标一次即自动适配）");
        } else {
            RunLog.line("设备指纹 ✓ " + cur);
        }
    }

    /** 读该曲的 K（ms）。G2：指纹不符时一律当"没标定过"（返回 NaN）。 */
    private float kStored(SongLib.Song song) {
        if (fpMismatch) return Float.NaN;
        // ★ 2026-10-30：与界面 `MainActivity.calibK` 共用同一份 key 契约（`CalibKeys.readKeys`）。
        //   两边口径一旦分叉，就会出现「界面显示的 K ≠ 打歌实际用的 K」：那天界面会遍历兄弟变体、
        //   这里不会，于是用户照界面调参、实际没生效；而且点「清除」也清不干净（兄弟难度那份还在）。
        for (String k : CalibKeys.readKeys(song.name, song.key)) {
            float v = prefs().getFloat(k, Float.NaN);
            if (!Float.isNaN(v)) return v;
        }
        return Float.NaN;
    }

    /** 设备常数 c（ms）。G2：指纹不符 ⇒ 退回出厂默认（旧设备的 c 不成立）。 */
    private float cUsed() {
        return fpMismatch ? DEFAULT_C_MS : prefs().getFloat("c_ms", DEFAULT_C_MS);
    }

    /**
     * G10：把**注入结束后才算出来**的参数追加进本轮 trace（append 模式）。
     *
     * ⛔ 必须校验长度增长：trace 是下一轮唯一的证据来源，静默写短等于证据消失
     *   （与 `dumpTrace` 的落盘校验同一条规矩）。
     */
    /**
     * G10 补口：把**零点来源的实测参数**追加进 trace 尾部。
     *
     * ★ 为什么必须追加而不是写头（2026-10-25 20:3x 发现）：两段锚路径在 **onset 到达之前**就开始注入，
     *   那一刻 `job.zeroSrc.meta()` 还是空的（`OnsetZero.finish()` 才填）⇒ 头里只剩
     *   `pre=1 n_pre=… rezero_delta_ms=…`，**缺 `onset_ms/sil_ref_ms/l_dev_ms`**，
     *   事后想复核零点只能从 `# calib` 的 `s_ms/e_ms` 反推（996 那轮就是这样）。
     *   所以：头保持"注入前已知"，零点参数等 `finish()` 之后**追加**——与 K/lead 同一套规矩。
     */
    private void appendZeroMeta(Job job) {
        if (job == null || job.zeroSrc == null) return;
        String m = job.zeroSrc.meta();
        if (m == null || m.trim().isEmpty()) return;
        appendTraceNote(job, "zero " + m);
    }

    private void appendTraceNote(Job job, String note) {
        if (job == null || job.traceFile == null || note == null || note.trim().isEmpty()) return;
        String line = "# " + note.replace('\r', ' ').replace('\n', ' ').trim() + "\n";
        byte[] bytes = line.getBytes(StandardCharsets.UTF_8);
        long before = job.traceFile.length();
        try (FileOutputStream fo = new FileOutputStream(job.traceFile, true)) {
            fo.write(bytes);
            fo.flush();
        } catch (Throwable t) {
            RunLog.line("⚠ trace 追加失败：" + ShizukuProbe.brief(t) + "（" + note + "）");
            return;
        }
        long after = job.traceFile.length();
        if (after != before + bytes.length) {
            RunLog.line("⚠ trace 追加校验失败：期望 +" + bytes.length + " B，实际 +"
                    + (after - before) + " B（" + job.traceFile + "）");
            return;
        }
        RunLog.line("trace 追加 " + note);
    }

    private android.content.SharedPreferences prefs() {
        return getSharedPreferences(PREF, MODE_PRIVATE);
    }

    /** 准备：曲目 → 显示尺寸 → 载荷 → 排程 → 门禁。任何一步不成立都拒绝继续。 */
    private Job prepare(String songKey, String zeroMode, double lDevS,
                        Double leadOverride, int resultCode, Intent resultData) throws Exception {
        RunLog.phase("准备");
        if (songKey == null || songKey.isEmpty()) {
            throw new IllegalArgumentException("没有指定曲目（EXTRA_SONG 为空）");
        }
        SongLib.Song song = null;
        for (SongLib.Song s : SongLib.load(this)) {
            if (s.key.equals(songKey)) song = s;
        }
        if (song == null) {
            throw new IllegalArgumentException("assets 里没有 key=" + songKey + "（先从界面选曲）");
        }
        RunLog.line("曲目 " + song.title() + " · " + song.diff
                + (song.level > 0 ? " " + song.level : "")
                + " · " + song.noteCount + " 音符 · sil_ref "
                + String.format(Locale.ROOT, "%.6f s", song.silRefS));

        // ★ G2 必须在读 K / lead / c **之前**跑：它决定这几个值能不能用。
        checkFingerprint();

        int[] wh = landscapeSize();
        double dstW = wh[0];
        double dstH = wh[1];
        RunLog.line("显示 " + (int) dstW + "x" + (int) dstH + "（横屏口径，fit=height）");

        Job job = new Job();
        job.song = song;
        job.zeroMode = zeroMode;
        job.lDevS = effectiveLDev(song, lDevS);
        // 自动标定只在「有母带指纹 + onset 零点」时才有意义（tap 模式的零点与 K 无关）
        job.autoCalib = ZERO_ONSET.equals(zeroMode) && !song.envB64.isEmpty();
        // 两段锚：只有「Ⅱ 类（首音在盲区）+ onset 路径」才值得预派。
        // Ⅰ 类不需要（onset 比预测准）；tap 模式没有 onset 可重排。
        job.preRoll = ZERO_ONSET.equals(zeroMode) && song.blindLost > 0;
        job.leadAtPrepare = leadOf(song);
        job.resultCode = resultCode;
        job.resultData = resultData;
        job.dstW = dstW;
        job.dstH = dstH;

        // ★ tap 的 lead 必须**在曲目已知之后、排程/门禁/倒计时之前**判掉：
        //   lead 是曲目属性（跨曲差 300+ ms），没测过就当场拒绝 —— 别等倒计时结束、
        //   用户已经停在 ▶ 界面了才告诉他「这曲没有 lead」。
        if (ZERO_TAP.equals(zeroMode)) {
            try {
                job.leadMs = TapLead.resolve(leadOverride, song.tapLeadMs);
            } catch (IllegalArgumentException e) {
                throw new IllegalStateException(e.getMessage(), e);
            }
            String ov = leadOverride == null ? "未给"
                    : String.format(Locale.ROOT, "%.3f ms", leadOverride);
            RunLog.line(String.format(Locale.ROOT,
                    "零点=tap：lead %.3f ms（命令行 %s，曲目自带 %.3f ms）",
                    job.leadMs, ov, song.tapLeadMs));
        }

        File payload = cachePayload(song);
        job.payload = payload;
        long t0 = System.nanoTime();
        Densify.Stats ds = new Densify.Stats();
        Scheduler.Meta meta = new Scheduler.Meta();
        job.cmds = Scheduler.build(payload.toPath(), dstW, dstH, true, true, ds, meta);
        double buildMs = (System.nanoTime() - t0) / 1e6;
        if (job.cmds.isEmpty()) {
            // ★ 不许降级成「打了个 0 音符的歌」正常结束：那会让界面显示"完成"而实际没打
            throw new IllegalStateException("这首的谱面数据是空的（素材没打包，或游戏版本与素材不匹配）⇒ 拒绝开火");
        }
        int nKey = 0;
        for (Scheduler.Cmd c : job.cmds) if (c.key()) nKey++;
        job.nKey = nKey;
        // ★ 载荷口径单独一行（10-24 教训）：出问题时第一件事就是问"设备跑的是哪份载荷"。
        //   有这一行，跑完直接与装机 assets 的 canvas/sha256 比，不必再靠排除法推断。
        RunLog.line("载荷口径 " + meta);
        RunLog.line(String.format(Locale.ROOT,
                "排程 %d 条（关键 %d，MOVE 加密 +%d），t %.0f ~ %.0f ms，解析 %.0f ms",
                job.cmds.size(), nKey, ds.nAdded + ds.nTailAdded,
                job.cmds.get(0).tMs(), job.cmds.get(job.cmds.size() - 1).tMs(), buildMs));

        // 常量对齐：Gesture 的动作码必须等于 MotionEvent 的，否则整条流非法
        String cErr = DevRun.checkConstants();
        if (cErr != null) throw new IllegalStateException(cErr + " ⇒ 拒绝开火");

        ShizukuProbe.Report r = gateShizuku();
        job.proxy = r.inputManagerProxy;
        job.inject = r.injectMethod;
        RunLog.line("✓ Shizuku 门禁通过（proxy="
                + job.proxy.getClass().getName() + "）");

        // onset 零点的前置条件。★ 全部在**倒计时之前**判掉：缺授权却进了倒计时，
        //   用户已经把游戏停在 ▶ 界面，20 s 后才告诉他「没授权」—— 白打一轮。
        if (ZERO_ONSET.equals(zeroMode)) {
            if (resultData == null) {
                throw new IllegalStateException(
                        "零点=onset 但没有录屏授权 token（EXTRA_RESULT_DATA 为空）"
                                + " ⇒ 从界面重新点「开始演奏」走一次系统授权");
            }
            if (job.song.silRefS <= 0) {
                throw new IllegalStateException("曲目 " + songKey + " 的 sil_ref = "
                        + job.song.silRefS + " s ⇒ 算不出零点（assets 里这曲的载荷缺 sil_ref）");
            }
            RunLog.line(String.format(Locale.ROOT,
                    "零点=onset：sil_ref %.6f s，L_dev %.3f s（首版猜值，标定后改）",
                    job.song.silRefS, lDevS));
            // ★ 边沿口径单独一行：`sil_ref` 是按**母带阈值**定义的边沿，端内若用别的判据找 onset，
            //   两侧就把边沿定义在不同位置（渐入曲能差几百毫秒，FACTS §169）。
            //   开火前必须能从日志上看出这一轮用的是哪条判据。
            RunLog.line(String.format(Locale.ROOT,
                    "边沿口径 %s（sil_thr=%s）",
                    job.song.silThr > 0
                            ? String.format(Locale.ROOT, "sil_thr %.1f RMS / 连续 %d ms",
                                    job.song.silThr, OnsetGate.DEFAULT_SUSTAIN_MS)
                            : "相对（5×背景）",
                    job.song.silThr > 0 ? String.format(Locale.ROOT, "%.3f", job.song.silThr) : "未标定"));
        }
        return job;
    }

    /**
     * 「测试点一次 ▶」：只发一个 tap，**不注入音符、不占 worker、不算一轮打歌**。
     *
     * 用户操作（完整教程见 `docs/start-button-calibration.md`）：
     * 切到游戏选曲页 → 点 App 里的「测试 ▶」→ 看游戏有没有开始加载。
     * 开始加载 ⇒ 位置对了；没反应 ⇒ 用「▶ 落点」→「学习落点」重记一次（手填偏移已于 10-31 取消）。
     *
     * ⛔ 不写 `RunLog.phase`：这不是一轮打歌，改阶段会让界面假装在跑。
     * ⛔ 不赋值 `worker`：否则 `worker.isAlive()` 会拒掉下一轮真打歌。
     */
    private void testTapOnce() {
        try {
            ShizukuProbe.Report r = gateShizuku();
            int[] wh = landscapeSize();
            float x = startTapX(wh[0]);
            float y = startTapY(wh[1]);
            // ★ 2026-10-31：手填偏移已废弃 ⇒ 这里不再有"偏移"这个量，
            //   只报**落点来自哪一档**（学到的 / 出厂比例）。两套来源并存就是 K bug 的形状。
            String src = StartPointPrefs.sourceOf(this, wh[0], wh[1]);
            String err = new ZeroSource.ShizukuTap(r.inputManagerProxy, r.injectMethod).tap(x, y);
            if (err != null) {
                RunLog.line("测试点击失败：" + err);
                toast("测试点击失败：" + err);
                return;
            }
            RunLog.line(String.format(Locale.ROOT,
                    "测试点击已发出（按下后 %.1f s）：%.1f,%.1f（来源 %s；屏幕 %dx%d）"
                            + " ⇒ 游戏开始加载=位置对；没反应=用「▶ 落点」→「学习落点」重记一次",
                    TEST_TAP_LEAD_MS / 1000.0,
                    x, y, src, wh[0], wh[1]));
            RunLog.line(StartButtonBox.verdict(x, y, wh[0], wh[1], START_REL_X, START_REL_Y));
            toast("已点一次 ▶：游戏开始加载=位置对；没反应=用「学习落点」重记一次");
        } catch (Throwable t) {
            String msg = "测试点击失败：" + ShizukuProbe.brief(t)
                    + "（先确认 Shizuku 已授权并正在运行）";
            RunLog.line(msg);
            toast(msg);
        }
    }

    /** 门禁：没有 Shizuku 代理就没有「手」。返回的 Report 里有后面注入要用的 proxy + Method。 */
    private ShizukuProbe.Report gateShizuku() {
        ShizukuProbe.Report r = ShizukuProbe.runAll();
        if (r.allOk()) return r;
        StringBuilder bad = new StringBuilder();
        for (ShizukuProbe.Step s : r.steps) {
            if (s.gate && !s.ok) {
                if (bad.length() > 0) bad.append("；");
                bad.append(s.name).append(" ⇒ ")
                        .append(s.fail != null ? s.fail : (s.detail == null ? "false" : s.detail));
            }
        }
        if (bad.length() == 0) bad.append("代理为 null（授权未生效？）");
        throw new IllegalStateException("Shizuku 门禁未过：" + bad);
    }

    /** 待命：把「即将注入什么」摆出来。这一段是判据的第一行。 */
    private void arm(Job job) {
        RunLog.phase("待命");
        RunLog.line(String.format(Locale.ROOT,
                "待命：%s · 指令 %d 条（关键 %d）· t %.0f~%.0f ms · 载荷 %s（%d B）",
                job.song.key, job.cmds.size(), job.nKey,
                job.cmds.get(0).tMs(), job.cmds.get(job.cmds.size() - 1).tMs(),
                job.payload.getName(), job.payload.length()));
    }

    private double zeroOf(Job job, int countdownS) throws Exception {
        if (job.zeroSrc == null) buildZeroSource(job, countdownS);
        return job.zeroSrc.await();
    }

    /**
     * 只**构造**零点源（不点屏、不等待）。
     *
     * ★ 两段锚必须先构造、再点 ▶：`runPreRoll` 要判断 `job.zeroSrc instanceof OnsetZero`
     *   才决定要不要预派。原来 `zeroSrc` 只在 `zeroOf()` 里创建 ⇒ 判断时它还是 null，
     *   于是**预派分支永远进不去**（三轮 `pre=0`，一直被误标成 `reason=no_lead`）。这条 bug 就是它。
     */
    private void buildZeroSource(Job job, int countdownS) {
        ZeroSource.Tapper tapper = new ZeroSource.ShizukuTap(job.proxy, job.inject);
        float x = startTapX(job.dstW);
        float y = startTapY(job.dstH);
        RunLog.line(String.format(Locale.ROOT,
                "▶ 位置 %.1f,%.1f（来源 %s × %dx%d）—— 倒计时结束后真点",
                x, y, StartPointPrefs.sourceOf(this, (int) job.dstW, (int) job.dstH),
                (int) job.dstW, (int) job.dstH));
        // ★ 2026-10-30：换机自检。只判读、**不改坐标、不中止**。
        //   换到陌生机型时，这一行能提前回答「这个比例点得到按钮吗」，
        //   不用等打完发现零注入再回头查。⚠ 框是一台机的实测反推，不是物理真值。
        RunLog.line(StartButtonBox.verdict(x, y, job.dstW, job.dstH, START_REL_X, START_REL_Y));

        ZeroSource zs;
        if (ZERO_ONSET.equals(job.zeroMode)) {
            // ★ 把母带边沿阈值一路传到采集层：漏了这一跳 ⇒ 端内退回相对判据，
            //   而日志仍写着「零点=onset」，症状就是 §169 那几百毫秒的静默偏差。
            // ★ 自动标定（A 方案）：普通轮次也让采集跑满 30 s，注入跑完后端内算 K 存给下一轮。
            //   代价是采集与注入并行 —— 本轮就是来验「会不会扰动注入」的。
            boolean auto = job.autoCalib;
            OnsetTap tap = new OnsetTap(this, job.resultCode, job.resultData, job.song.silThr,
                    auto ? CALIB_FRAMES : 0);
            OnsetZero onset = new OnsetZero(tap, tapper, countdownS, job.song.silRefS, job.lDevS,
                    x, y, () -> stopFlag);
            onset.keepTapAlive(auto);
            job.tap = tap;
            zs = onset;
        } else {
            zs = new ZeroSource.CountdownTap(tapper, countdownS, job.leadMs, x, y,
                    () -> stopFlag);
        }
        job.zeroSrc = zs;
    }


    /**
     * 两段锚：**点 ▶ 后立刻用预测零点开打**，onset 到达再用实测零点重排剩余事件。
     *
     * 为什么这么做：Ⅱ 类曲的首音落在起音盲区（0.342 s）里，等 onset 就必然丢；
     * 而 lead 实测 sd ≈ 30 ms（±80 ms 的 P 窗），预测派发有 90%+ 的机会打中。
     *
     * ★ 三条必须守住的语义（对应 B 方案评审 B5/B8/B10）：
     *   ① 注入在工作线程跑，主线程等 onset ⇒ 才能 rezero；
     *   ② onset 失败**不中止**：已经用预测打出去的不能白打，继续用预测打完并标 `zero=predicted`；
     *   ③ 没 lead 历史 ⇒ 不预派（照旧走 onset），并把原因写进日志（别让用户以为"这轮预派了"）。
     */
    private void runPreRoll(Job job, int countdownS) throws Exception {
        OnsetZero onset = (OnsetZero) job.zeroSrc;
        double lead = leadOf(job.song);
        if (Double.isNaN(lead)) {
            RunLog.line("⚠ 本曲没有 lead 历史 ⇒ 本轮不预派（照旧等 onset）；"
                    + "打完这一轮就会记下 lead，下一轮起 Ⅱ 类才有机会补上首音");
            double zero = zeroOf(job, countdownS);
            inject(job, zero);
            appendZeroMeta(job);
            recordLead(job);
            return;
        }
        double tapWall = onset.begin();
        final double predZero = tapWall + lead;
        job.preRolled = true;
        job.predLeadMs = lead;
        RunLog.line(String.format(Locale.ROOT,
                "★ 两段锚：用 lead 均值 %.0f ms 预测零点，立刻开始派发（盲区事件）；"
                        + "onset 到达后重排剩余事件", lead));
        final Throwable[] injectErr = new Throwable[1];
        Thread th = new Thread(() -> {
            try {
                inject(job, predZero);
            } catch (Throwable t) {
                injectErr[0] = t;
            }
        }, "preroll-inject");
        th.start();

        double realZero = predZero;
        try {
            realZero = onset.finish();
            job.zeroRealMs = realZero;
            if (job.brain != null) {
                job.brain.rezero(realZero);
                RunLog.line(String.format(Locale.ROOT,
                        "实测零点到达：与预测差 %+.1f ms ⇒ 已重排剩余事件",
                        job.brain.rezeroDeltaMs()));
            }
        } catch (ZeroSource.Stopped stopReq) {
            // 用户按了停止：注入线程必须也停，然后原样抛出去（runOnce 的 Stopped 分支）
            stopFlag = true;
            try { th.join(3000); } catch (InterruptedException ignore) { }
            throw stopReq;
        } catch (Throwable t) {
            // ② onset 失败也不中止：已经打出去的不白打
            job.zeroFallback = true;
            RunLog.line("⚠ onset 失败（" + ShizukuProbe.brief(t) + "）⇒ 继续用预测零点打完；"
                    + "本轮标记 zero=predicted，不写 lead");
        }
        th.join();
        if (injectErr[0] != null) throw new RuntimeException(injectErr[0]);
        // ★ 预派路径的零点参数只能在这里补：头是在 onset 之前拼的（见 appendZeroMeta）
        appendZeroMeta(job);
        if (!job.zeroFallback) recordLead(job);
    }

    /** 把本轮的实测 lead 记进滑动平均（没有 onset 成功就跳过）。 */
    private void recordLead(Job job) {
        if (!(job.zeroSrc instanceof OnsetZero)) return;
        CalibMerge.Out out = recordLeadFrom((OnsetZero) job.zeroSrc, job.song, false);
        if (out == null) return;
        // ★ G10：lead 的实测值/融合结果必须跟着数据走（以前只在界面日志里，下一轮就没了）
        appendTraceNote(job, String.format(Locale.ROOT,
                "lead lead_meas=%.1f lead_after=%.1f lead_n=%d lead_acc=%d why=%s",
                lastLeadMeas, out.value, out.n, out.accepted ? 1 : 0,
                out.why.replace(' ', '_')));
    }

    /**
     * 记 lead。
     *
     * ★ 两种轮次的换算**不一样**（这里踩过一次，别再想当然）：
     *   · 打歌轮：`zeroRel = onset − sil_ref − L_dev` ⇒ 已经减过 L_dev，
     *     而 `onset = T0 + sil_ref + cap + det`、`L_dev = K + c`、`K ≈ det`
     *     ⇒ `zeroRel = (T0 − tap) + cap − c` ≈ 真 lead ± cap 抖动。**直接就是 lead，不要再减。**
     *   · 标定轮：L_dev 传的是 0 ⇒ `zeroRel = 真 lead + cap + det`
     *     ⇒ 要减掉「打歌时会用的 L_dev」（K + c）才能得到 lead。
     * ⛔ 之前给打歌轮也减了一次 L_dev ⇒ lead 少了整整 226 ms（已换 `lead3_` 键让脏值作废）。
     */
    private CalibMerge.Out recordLeadFrom(OnsetZero onset, SongLib.Song song, boolean calibration) {
        double lead = onset.zeroRelMs();
        if (Double.isNaN(lead)) return null;
        if (calibration) {
            float k = kStored(song);
            if (Float.isNaN(k)) return null;             // 没 K 就分不出 L_dev，宁可不记
            lead -= k + cUsed();
        }
        lastLeadMeas = lead;
        return leadPut(song, lead);
    }

    private void inject(Job job, double zero) {
        List<Scheduler.Cmd> cmds = job.cmds;
        int maxFalse = Math.max(3, cmds.size() / 200);
        // ★ 注入模式固定 = 生产值 ASYNC（诊断用的 wait 开关已于 2026-11-01 删除）。
        final int injectMode = InjectMode.ASYNC;
        // ★ G13（2026-10-25）：**不再丢迟到的 MOVE**（第 4 个参数 dropMoves=false）。
        //   原因：丢 MOVE 会让指针位置停在上一次成功的位置上；Drag（黄键）按"音符时刻手指在不在
        //   音符上"判定，位置陈旧就可能完全判不到 ⇒ 症状是「0 Good / 0 Bad 的纯 Miss」。
        //   代价：每轮只有 ~18 条 MOVE 会迟到 >20 ms，队列压力可忽略（实测 n_late_move 会记账）。
        DevRun brain = new DevRun(job.proxy, job.inject, true, false, maxFalse,
                RunLog::line, () -> stopFlag, injectMode);
        job.brain = brain;
        // ★ 默认开追踪：它是唯一能在「发 2769 条 / false 0 / lag p50 2.3 ms 全绿」
        //   的情况下区分「多指帧没被派发」与「坐标不对」的证据。默认关的唯一后果
        //   就是又白打一枪——上一轮已经这么白打过 4 枪了。
        InjectTrace trace = new InjectTrace();
        // 模式写进 CSV 头部：打完把文件拉回来就能证明这枪跑的是哪个模式，不依赖 logcat（§167）
        trace.setInjectMode(injectMode, InjectMode.name(injectMode));
        // ★ 本轮的**运行参数**也写进 CSV 头部（FACTS §171.2 的取证缺口）：
        //   `L_dev` 改过之后，logcat 被游戏冲干净、界面日志只留 16 行 ⇒ 事后只能靠
        //   「进程是那次 am start 起的」推断用的是哪个值。产物必须自带参数。
        String zeroMeta = job.zeroSrc == null ? "" : job.zeroSrc.meta();
        // ★ G10：把「这轮到底用了哪个 K / lead / c」也写进去。以前 trace 只有 l_dev，
        //   想知道用的是融合后的哪个值必须回界面日志（16 行，下一轮就没了）。
        //   ⚠ 这里只能写**注入前**已知的量；K 的测量值要等 30 s 采集跑完 ⇒ 走 addTailNote。
        //   `k_used` 是「存着的 K」，`l_dev` 是「实际用的零点滞后」，`auto=` 说明前者有没有生效：
        //   手动 `--ed l_dev_s` 时 K 完全不参与，写成 k_used=<K> 而不标 auto 会让人误以为它参与了。
        float kStoredMs = kStored(job.song);
        int kStoredN = fpMismatch ? 0 : prefs().getInt("k_n_name_" + job.song.name, 0);
        int leadUsedN = fpMismatch ? 0 : prefs().getInt(LEAD_N_KEY + job.song.name, 0);
        String leadUsed = (fpMismatch || Double.isNaN(job.leadAtPrepare))
                ? "none" : String.format(Locale.ROOT, "%.1f", job.leadAtPrepare);
        final String baseMeta = String.format(Locale.ROOT,
                "song=%s sil_thr=%s l_dev=%.3f auto=%d k_used=%s k_n=%d lead_used=%s lead_n=%d"
                        + " c=%.0f fp=%s ver=%s %s",
                job.song.key,
                job.song.silThr > 0
                        ? String.format(Locale.ROOT, "%.3f", job.song.silThr) : "none",
                job.lDevS, autoLDev ? 1 : 0,
                Float.isNaN(kStoredMs) ? "none" : String.format(Locale.ROOT, "%.1f", kStoredMs),
                kStoredN, leadUsed, leadUsedN, cUsed(), fpMismatch ? "mismatch" : "ok",
                build().brief(), zeroMeta).trim();
        trace.setRunMeta(baseMeta);
        brain.setTrace(trace);

        RunLog.phase("注入");
        long t0 = System.nanoTime();
        double sec;
        try {
            brain.run(cmds, zero);
        } finally {
            // ★ 放 finally：注入中止/抛异常恰恰是最需要这份 trace 的时候
            sec = (System.nanoTime() - t0) / 1e9;
            // ★ B9/B10：两段锚的凭据必须进产物 —— 否则事后无法区分「预派了」「没预派」
            //   「预派了但 onset 失败」，也没法算预测误差（rezero_delta）。
            StringBuilder x = new StringBuilder(baseMeta);
            if (job.preRolled) {
                x.append(String.format(Locale.ROOT, " pre=1 pred_lead_ms=%.1f n_pre=%d",
                        job.predLeadMs, brain.beforeRezero()));
                if (job.zeroFallback) {
                    x.append(" zero=predicted");
                } else {
                    x.append(String.format(Locale.ROOT, " rezero_delta_ms=%.1f zero=onset",
                            brain.rezeroDeltaMs()));
                }
            } else {
                // ★ 要能区分两种"没预派"：Ⅰ 类本来就不该预派 vs Ⅱ 类但缺 lead。
                //   不加这一句，看到 pre=0 只能猜（15:41 那轮就白猜了一次）。
                String why = !ZERO_ONSET.equals(job.zeroMode) ? "not_onset"
                        : !(job.zeroSrc instanceof OnsetZero) ? "no_source"
                        : job.preRoll ? "no_lead" : "not_class2";
                x.append(" pre=0 reason=" + why);
                x.append(String.format(Locale.ROOT, " lead_prepare=%.1f", job.leadAtPrepare));
            }
            // ★ 注入侧的计数也进 trace：坏轮要能回答"是不是我们丢了事件"。
            //   今天对比 8 轮时，这些量只存在于界面日志（16 行、下一轮就没了），
            //   导致"事件集合相同"这个结论只能靠 trace 反推，效率极低。
            x.append(" persist=" + (persistFail ? "fail" : "ok"));
            x.append(String.format(Locale.ROOT,
                    " n_emit=%d n_skip_past=%d n_drop_move=%d n_late_move=%d n_skip_dense=%d"
                            + " n_keepalive=%d n_orphan=%d n_false=%d drop_moves=0",
                    brain.emitted(), brain.skippedPast(), brain.dropped(), brain.lateMoves(),
                    brain.denseSkipped(), brain.keepalives(), brain.orphans(), brain.falses()));
            trace.setRunMeta(x.toString());
            dumpTrace(job, trace);
        }

        JSONObject res = brain.summary();
        String abort = brain.abortReason();
        RunLog.phase(abort == null ? "注入完成" : "注入中止");
        RunLog.line(String.format(Locale.ROOT,
                "注入 %d 条（计划 %d，跳过(零点前) %d，抽稀 MOVE %d，丢 MOVE %d，false %d），用时 %.1f s%s",
                brain.emitted(), cmds.size(), brain.skippedPast(),
                res.optInt("n_skip_dense", 0), brain.dropped(), brain.falses(), sec,
                abort == null ? "" : "，中止原因：" + abort));

        RunLog.line("关键事件 lag p50 " + res.opt("lag_p50_ms")
                + " / p90 " + res.opt("lag_p90_ms")
                + " / p99 " + res.opt("lag_p99_ms")
                + " / max " + res.opt("lag_max_ms") + " ms");
        if (res.has("move_lead_p50_ms")) {
            RunLog.line("MOVE 提前量 p50 " + res.opt("move_lead_p50_ms")
                    + " / p99 " + res.opt("move_lead_p99_ms")
                    + " / max " + res.opt("move_lead_max_ms")
                    + " ms，>50ms 提前 " + res.opt("n_move_early_gt50")
                    + " 条（不变量：应为 0）");
        }
        if (abort != null) {
            RunLog.line("!! " + abort);
        }
    }

    /**
     * 把逐帧追踪落盘。
     *
     * ★ 路径选 {@code getExternalFilesDir(null)} 而不是 {@code getFilesDir()}：
     *   前者 `adb pull` 直接可读，后者要 `run-as`（依赖 debuggable）。
     * ⛔ 拉完必须 `adb shell rm` —— 项目规矩是**设备零残留**。
     */
    private void dumpTrace(Job job, InjectTrace trace) {
        if (trace == null || trace.lines() == 0) {
            RunLog.line("注入追踪：0 帧（一条都没发出，看上面的中止原因）");
            return;
        }
        RunLog.line("注入追踪：" + trace.brief());
        byte[] bytes = trace.csv().getBytes(StandardCharsets.UTF_8);
        File dir = getExternalFilesDir(null);
        if (dir == null) dir = getFilesDir();
        // ★ 带时间戳（10-25 用户要求"设备上的日志先留着"）：固定名会被下一轮覆盖，
        //   留着也没意义。改名后每轮各一份，pull 时用 `inject_trace*.csv`。
        String stamp = new java.text.SimpleDateFormat("HHmmss", Locale.ROOT)
                .format(new java.util.Date());
        File f = new File(dir, "inject_trace_" + stamp + ".csv");
        try (FileOutputStream fo = new FileOutputStream(f)) {
            fo.write(bytes);
        } catch (Throwable t) {
            RunLog.line("!! 追踪落盘失败：" + ShizukuProbe.brief(t) + "（路径 " + f + "）");
            return;
        }
        // ★ 写完回读长度：这份 trace 是下一轮判断多指丢帧的**唯一**证据。
        //   宁可在界面上当场报错，也别等 adb pull 回来才发现是个 0 字节文件 ——
        //   那意味着又白打一枪，而且是被"落盘成功"的日志骗的白打。
        long got = f.isFile() ? f.length() : -1;
        if (got != bytes.length) {
            RunLog.line("!! 追踪落盘校验失败：期望 " + bytes.length + " B，实际 " + got + " B（" + f + "）");
        } else {
            // ★ G10：记下句柄 —— 注入结束后才算出 K/lead，要往这个文件**追加**收尾行。
            job.traceFile = f;
            RunLog.line("追踪已落盘 " + f.getAbsolutePath() + "（明细 " + trace.written()
                    + " 行 / " + got + " B，" + job.song.key + "）");
        }
    }

    /** 一轮的上下文（避免十几个参数穿来穿去）。 */
    private static final class Job {
        SongLib.Song song;
        List<Scheduler.Cmd> cmds;
        File payload;
        double dstW, dstH;
        int nKey;
        Object proxy;
        Method inject;
        ZeroSource zeroSrc;
        /** onset 采集器（自动标定要在注入跑完之后继续读它的包络）。 */
        OnsetTap tap;
        /** 本轮是否「打完顺便标定」（A 方案；判据 = 注入 lag 分布不被扰动）。 */
        boolean autoCalib;
        /** 两段锚：点 ▶ 后先用预测零点派发盲区事件，onset 到达再 rezero。 */
        boolean preRoll;
        /** 本轮真的预派了（有 lead 历史 + onset 路径）。 */
        boolean preRolled;
        /** onset 失败、改用预测零点打完（trace 必须能区分）。 */
        boolean zeroFallback;
        /** 预派用的 lead（ms）。 */
        double predLeadMs;
        /** 实测零点（epoch ms）；未预派时为 0。 */
        double zeroRealMs;
        /** prepare 时读到的 lead（NaN = 没读到）。写进 trace，用来抓"界面有值、服务说没有"。 */
        double leadAtPrepare = Double.NaN;
        DevRun brain;
        String zeroMode;
        /** G10：本轮 trace 落盘后的文件句柄（注入结束后要追加 K/lead 的实测值）。 */
        File traceFile;
        double lDevS;
        /** tap 模式的 lead（ms）。由 `TapLead.resolve` 在 prepare 里定，onset 模式不用。 */
        double leadMs;
        int resultCode;
        Intent resultData;
    }

    // ─────────────────────────── 载荷 ───────────────────────────

    /**
     * 载荷缓存：`assets/payload/<name>.json` → `files/payload/<key>.json`。
     *
     * ★★ 2026-10-24 教训（FACTS §161）：这里原本比的是 `f.length() != raw.length`。
     *   但把 `canvas` 从 `1080x2340` 改成 `1920x1080` **不改字节数** ⇒ 修好的 asset
     *   永远覆盖不掉旧缓存，run5/run6 每一下触摸都压进屏幕中间一块（成绩 40.73% / 31.47%），
     *   而所有既有判据都是绿的。判据必须是**内容**。
     * ★ 判据与落盘**只有一份实现**（{@link PayloadCache}）：本类与 `TouchProbeActivity`
     *   原先各抄一份同样的长度判据（项目规矩 9：同一个量两处实现必然分叉）。
     * ★ 为什么留在 files/ 而不每轮删：这是 App 的数据不是测试残留（计划 §5）。
     */
    private File cachePayload(SongLib.Song s) throws Exception {
        File dir = new File(getFilesDir(), "payload");
        try (InputStream in = getAssets().open(s.payload)) {
            return PayloadCache.ensure(dir, s.key, s.payload, in, RunLog::line);
        }
    }

    /** 归一化成横屏宽x高（与 PC 侧 `wm size` 口径一致：720x1610 ⇒ 1610x720）。 */
    private int[] landscapeSize() {
        DisplayManager dm = (DisplayManager) getSystemService(Context.DISPLAY_SERVICE);
        if (dm == null) throw new IllegalStateException("拿不到 DisplayManager");
        Display d = dm.getDisplay(Display.DEFAULT_DISPLAY);
        if (d == null) throw new IllegalStateException("拿不到默认 Display");
        Point p = new Point();
        d.getRealSize(p);
        if (p.x <= 0 || p.y <= 0) {
            throw new IllegalStateException("显示尺寸非法 " + p.x + "x" + p.y);
        }
        return new int[]{Math.max(p.x, p.y), Math.min(p.x, p.y)};
    }

    // ─────────────────────────── 通知 / 唤醒锁 ───────────────────────────

    private Notification notif(String text) {
        Notification.Builder b = Build.VERSION.SDK_INT >= 26
                ? new Notification.Builder(this, CH)
                : new Notification.Builder(this);
        return b.setContentTitle("PG 自动演奏 · 端内")
                .setContentText(text)
                .setSmallIcon(android.R.drawable.ic_menu_compass)
                .setOngoing(true)
                .setShowWhen(false)
                .build();
    }

    /**
     * ★ 必须给 type：Android 14 上 FGS 的 type 与 manifest 不符会抛 SecurityException。
     * ★ `withProjection`：`onset` 零点要用 `getMediaProjection`，而 Android 14 要求
     *   调用之前 FGS 已经带着 mediaProjection 这个类型在跑。**按需加位**而不是恒加 ——
     *   `tap` 路径（S1-c）不碰采集，保持与旧版逐位一致的启动参数，回退时少一个变量。
     */
    private void startFg(String text, boolean withProjection) {
        Notification n = notif(text);
        if (Build.VERSION.SDK_INT >= 34) {
            int type = ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE;
            if (withProjection) {
                type |= ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PROJECTION;
            }
            startForeground(NOTIF_ID, n, type);
        } else {
            startForeground(NOTIF_ID, n);
        }
    }

    private void finish() {
        try {
            stopForeground(true);
        } catch (Throwable ignore) {
            // 已经不在前台时可能抛，不影响结果
        }
        stopSelf();
    }

    private void acquireWake() {
        acquireWake(WAKE_TIMEOUT_MS);
    }

    /**
     * @param timeoutMs 持锁上限。单曲用 400 s；**课题模式必须给得更长**（三首 ≈8 分钟），
     *                  否则第 3 首会掉锁降频（方案 §9 B1）。
     */
    private void acquireWake(long timeoutMs) {
        try {
            PowerManager pm = (PowerManager) getSystemService(Context.POWER_SERVICE);
            if (pm == null) {
                RunLog.line("⚠ 拿不到 PowerManager ⇒ 不加 WakeLock（屏幕熄灭会拖慢注入）");
                return;
            }
            wl = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "pgplay:run");
            wl.setReferenceCounted(false);
            wl.acquire(timeoutMs);
            RunLog.line("WakeLock 已持有（PARTIAL，" + timeoutMs / 1000 + " s 上限）");
        } catch (Throwable t) {
            RunLog.line("⚠ WakeLock 获取失败：" + ShizukuProbe.brief(t));
        }
    }

    private void releaseWake() {
        try {
            if (wl != null && wl.isHeld()) wl.release();
        } catch (Throwable ignore) {
            // 释放失败只会浪费电，不会破坏正确性
        }
        wl = null;
    }
}
