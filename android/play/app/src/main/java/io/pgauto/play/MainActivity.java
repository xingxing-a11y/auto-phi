package io.pgauto.play;

import android.Manifest;
import android.app.Activity;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.graphics.Typeface;
import android.graphics.drawable.Drawable;
import android.graphics.drawable.GradientDrawable;
import android.graphics.drawable.StateListDrawable;
import android.media.projection.MediaProjectionManager;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.text.Editable;
import android.text.TextWatcher;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.widget.CheckBox;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;
import android.widget.Toast;

import java.util.ArrayList;
import java.util.List;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;

import org.json.JSONArray;
import org.json.JSONObject;

import rikka.shizuku.Shizuku;

/**
 * 三步流程：**① 选曲 → ② 选难度 → ③ 开火**。
 *
 * 为什么拆成三步而不是一屏全给：
 *   · 曲目会越来越多（`out/ap_pool.csv` 里 291 首可打）；一屏"歌 + 难度 + 参数 + 日志"
 *     在 4 首时能看，到 40 首就变成滚不到底的列表。
 *   · 难度是**歌曲的属性**，选完歌才知道有哪些难度可选（assets 里目前每首只打包了
 *     1 个难度，所以第 ② 步经常只有一个选项 —— 这是数据的真实状态，不是界面冗余）。
 *   · 三步状态都在同一个 Activity 里，返回键逐级回退，不需要 Activity 栈。
 *
 * 数据全部来自 `assets/manifest.json`（`SongLib`）：界面里没有一行硬编码的曲名/等级。
 * 运行日志来自同进程静态 {@link RunLog}，**不落盘、不走 IPC**（见该类注释）。
 *
 * 不用的东西：AndroidX、任何 `res/`。UI 全在代码里搭 —— 好处是 `aapt2 link`
 * 只吃 manifest + assets，坏处是下边的 `round()/pressable()` 这些样式助手要自己写。
 */
public class MainActivity extends Activity {

    /** 日志刷新间隔。注入主循环内部 20 ms 级的抖动看 logcat，界面只要"看得到进度"。 */
    private static final long LOG_POLL_MS = 400L;

    // ── 配色：深色底 + 单一强调色，避免默认控件那种灰底方块 ──────────────────
    private static final int BG = 0xFF0E1116;
    private static final int HEAD_TOP = 0xFF18233A;
    private static final int CARD = 0xFF171B22;
    private static final int CARD_PRESSED = 0xFF20262F;
    private static final int STROKE = 0xFF262C36;
    private static final int TEXT = 0xFFE6E9EF;
    private static final int DIM = 0xFF9AA3AF;
    private static final int ACCENT = 0xFF5B8DEF;
    private static final int ACCENT_DARK = 0xFF3568D4;
    private static final int OK = 0xFF4ADE80;
    private static final int ERR = 0xFFF87171;

    private final Handler h = new Handler(Looper.getMainLooper());
    private final TextView[] chips = new TextView[3];

    /**
     * 「旧口径 K 已迁移」的一次性标记（2026-10-30）。
     *
     * ⛔ 别删也别改名：没有它，`migrateCalibKeys()` 每次冷启动都会把用户刚清掉的 K 从
     *   `k_ms_<key>` 搬回 `k_ms_name_<曲名>`，表现为「点了清除、过一会儿 K 又回来了」。
     */
    private static final String MIGRATE_K_FLAG = "migrate_k_v2";

    private List<SongLib.Group> groups = new ArrayList<>();
    /** 顶部右侧那个「c=177 ms」小字（点它才弹窗改；放开火页太显眼、易误触）。 */
    private TextView tvC;
    /** 顶部 c 右侧的 `lead=…`（点它改当前选中曲的 lead；没有选中曲时提示先选歌）。 */
    private TextView tvLead;
    /**
     * 顶部 **c 那一行的下一行**的 `K=…`（2026-10-30 用户要求：K 也要有人工入口）。
     *
     * ★ 为什么需要：K 以前**只有标定/自动标定能写**，界面一个写入口都没有 ⇒ 测错了
     *   （例如标定时游戏里放的是另一首）就永远清不掉，只能连打 3 轮用"连续 3 次同侧重置"
     *   硬顶掉。用户 2026-10-30 明确要求补这个入口。
     * ★ 按**曲名**存，与单曲页/课题页/服务侧完全共用同一个键。
     */
    private TextView tvK;
    /** ▶ 落点的手工偏移（`+dx/+dy px`）。点开可改 —— 2026-10-30 用户要求「能自己调」。 */
    private TextView tvStart;
    /** 曲库搜索词（跨 render 保留；只重建列表、不重建搜索框，否则每敲一个字就丢焦点）。 */
    private String songQuery = "";
    /** 一次最多画多少张曲卡：291 首全画会拖慢首屏，搜索能把范围缩小。 */
    private static final int SONG_LIST_CAP = 60;
    /** 当前这次渲染最多画多少张卡（「显示更多」会把它加 120）。 */
    private int songListLimit = SONG_LIST_CAP;
    /** 可滚动区（buildRoot 里建）；返回上一页时要恢复位置，否则一退回就跳到最顶上。 */
    private ScrollView scroll;
    /** 「回到最上方」悬浮按钮（只在选曲/难度页显示）。 */
    private TextView topBtn;
    /** ≥0 ⇒ 下一次 render 之后滚到这个位置；-1 = 不恢复。 */
    private int restoreScrollY = -1;
    private int step = 1;
    /**
     * 「教程/帮助」是**独立页面**，不占 1/2/3 三个步骤编号（2026-10-25 用户要求：
     * 开火页只留操作，教程统一收进左侧导航栏里的这一页）。
     */
    private static final int STEP_HELP = 100;
    /** 课题模式独立页（2026-10-27）。放在抽屉里，**不碰选曲/难度/开火三页**。 */
    private static final int STEP_COURSE = 101;
    // ★ 2026-11-01 用户报 bug 后改口径：这两个独立页**返回一律回选歌页**（step=1）。
    //   旧实现各自记「从哪一步进来的」（两个 `*Return` 字段），而两者会互相记录：
    //   课题 → 抽屉点帮助（记下 101）→ 抽屉点课题（记下 100）之后，
    //   返回键就在 100 / 101 之间来回跳，永远回不到选歌页。
    //   ⛔ 别再引入"记住来路"的字段：抽屉是全局入口，来路不可枚举（开火页/难度页/独立页都能进）。
    //   ⚠ 也正因为如此，`onBackPressed` 里**不能**让它们落到 `step--`：100-1=99、101-1=100，
    //     后者正好是帮助页 —— 那会把"从课题页按系统返回"变成"跳进帮助页"。
    /** 左侧导航栏与遮罩（`buildRoot` 建；默认 GONE）。 */
    private LinearLayout drawer;
    private View drawerScrim;
    private boolean drawerOpen;
    private SongLib.Group selGroup;
    private SongLib.Song selSong;

    /** 用端内标定值自动定 L_dev（K + c）。默认开：产品态就该这样。 */
    private boolean autoLDev = true;
    private String selfTestText = "未运行";
    /**
     * 三树自检结果那个 TextView。只改它的文本，**绝不整页 render()**：
     * 重建视图会让外层 ScrollView 的滚动位置重算（2026-11-01 真机反馈「点自检页面就动」）。
     */
    private TextView tvSelfTest;

    /** 授权链的 request code。1 已被通知权限占用（见 {@link #askNotificationPermission}）。 */
    private static final int REQ_AUDIO = 2;
    private static final int REQ_PROJ = 3;
    /**
     * Shizuku 的授权请求码。★ 它**不走** {@link #onRequestPermissionsResult}：
     * Shizuku 有自己的 listener（{@link #permListener}），因为授权表是它自己维护的。
     */
    private static final int REQ_SHIZUKU = 201;

    /**
     * Shizuku 授权结果回调。**必须注册**：`Shizuku.requestPermission()` 是异步的，
     * 结果只从这里回来；不注册就等于「弹了窗但永远不知道用户点了什么」。
     *
     * ★ 为什么非要 app 内申请（2026-10-24 两枪的教训）：
     *   `adb shell pm grant io.pgauto.play moe.shizuku.manager.permission.API_V23`
     *   会返回成功、`dumpsys package` 也显示 `granted=true`，但
     *   `Shizuku.checkSelfPermission()` **仍然返回 -1**（force-stop 重启进程后也一样）。
     *   ⇒ Shizuku 的授权表与 Android 的 permission 状态是两套账，pm grant 只是假绿。
     *   唯一合法路径是本 Activity 内 `Shizuku.requestPermission()` 弹它自己的确认框。
     */
    private final Shizuku.OnRequestPermissionResultListener permListener =
            (requestCode, grantResult) -> {
                if (requestCode != REQ_SHIZUKU) return;
                boolean ok = grantResult == PackageManager.PERMISSION_GRANTED;
                Toast.makeText(this, ok ? "Shizuku 已授权 ⇒ 可以开火"
                                : "Shizuku 授权被拒绝（code=" + grantResult + "）⇒ 真注入会拒绝开火",
                        Toast.LENGTH_LONG).show();
                render();
            };

    /**
     * 等授权的曲目：系统弹框是异步的，回来的那一刻才知道要打哪首。
     * ★ 用字段而不是 `startActivityForResult` 的 requestCode 编码曲目 —— 后者会把
     *   「第几个难度」这种业务信息塞进框架常量，一旦曲库变长就不可读。
     */
    private SongLib.Song pendingSong;

    /**
     * 零点来源与端内滞后：**从启动 Intent 透传**，这样真机标定改常数不必重装 APK：
     * ```text
     *   adb shell am start -n io.pgauto.play/.MainActivity --es zero tap
     *   adb shell am start -n io.pgauto.play/.MainActivity --ef l_dev_s 0.0812
     * ```
     * 与 probe 的 `EXTRA_BUF_BYTES` 是同一套路子（那里已验证可用）。
     */
    /**
     * 课题模式：逗号分隔的曲目 key（**游戏里选歌的顺序**）。空 = 普通单曲。
     * ★ 2026-10-27 从启动 Intent 透传（`am start … --es course "k1,k2,k3"`），
     *   这样 S1 可以不做 UI 就先验证会话骨架。UI 入口后补。
     */
    private String courseKeys = "";

    private String zeroMode = PlayService.ZERO_ONSET;
    private double lDevS = OnsetZero.DEFAULT_L_DEV_S;
    private LinearLayout content;
    private TextView tvPhase;
    private TextView tvLog;
    /** 日志容器（固定高度、内部可滑动）；收起时整块 GONE。 */
    private LogScroll logScroll;
    /** 日志块的「▸ 展开 / ▾ 收起」那行小字（点它或点日志块本身都能切）。 */
    private TextView tvLogToggle;
    /**
     * 日志是否展开。默认**收起**（整块不占位，只剩一行标题）。
     *
     * ★ 2026-11-01 用户反馈"日志影响观感"：本机横屏 1610x720 @density320 ⇒ 屏幕只有 360 dp 高，
     *   内容区约 230 dp，而 16 行 11sp 等宽文本约 210 dp（长行还会折行）⇒ 日志块几乎吃满一屏，
     *   按钮全被挤到下面。
     * ★★ 2026-11-01 晚用户再定：**收起 = 完全隐藏，展开 = 固定高度、内部滑动看全部**。
     *   ⇒ 既不砍行数（端内 APK 不可调试、run-as 被拒，日志是手机上唯一的观测通道），
     *     也不让它撑爆首屏 —— 两个目标靠「折叠 + 内部滚动」同时满足。
     * ⚠ 状态存字段（不是局部变量）：`render()` 每次都重建视图，存局部会在每次重绘后弹回去。
     */
    private boolean logExpanded = false;
    /**
     * 展开后日志面板的固定高度（dp）。11sp 等宽约 13 dp/行 + 上下内边距 ⇒ 约 18~20 行可见。
     * ★ 旧的「收起 2 行 / 展开 16 行」两个行数上限已删，内容改为 {@code RunLog.all()}。
     * ★ 2026-11-01 真机反馈后由 132 调到 264（用户要"现在的两倍左右"）。
     */
    private static final int LOG_BOX_DP = 264;
    /** 下一次刷新是否强制滚到底：刚展开时置 true；平时只有「原本就贴底」才滚。 */
    private boolean logStickBottom = true;
    /** 首页副标题：把当前的 `zero` / `注入` 口径显示出来，打之前就能发现开关丢了。 */
    private TextView tvModeLabel;
    /** 上一次写进视图的文本：内容没变就**不碰**视图。 */
    private String lastPhaseText = "";
    private String lastLogText = "";
    private String lastLogToggleText = "";

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(buildRoot());
        // ★ 必须在这里注册、在 onDestroy 注销：Shizuku 的授权结果是异步回调过来的，
        //   与 Activity 的 onRequestPermissionsResult 是两条完全不同的通道。
        Shizuku.addRequestPermissionResultListener(permListener);
        readLaunchOverrides();
        refreshModeLabel();
        loadSongs();
        migrateCalibKeys();
        askNotificationPermission();
        render();
        h.postDelayed(this::refreshLog, LOG_POLL_MS);
    }

    /** 读 `adb am start` 传进来的覆盖项。非法值只提示、不静默采用（否则会「看起来在跑」）。 */
    private void readLaunchOverrides() {
        Intent it = getIntent();
        if (it == null) return;
        String z = it.getStringExtra(PlayService.EXTRA_ZERO);
        if (z != null && !z.isEmpty()) {
            if (PlayService.ZERO_ONSET.equals(z) || PlayService.ZERO_TAP.equals(z)) {
                zeroMode = z;
            } else {
                Toast.makeText(this, "忽略非法 zero=" + z + "（只认 onset/tap）",
                        Toast.LENGTH_LONG).show();
            }
        }
        if (it.hasExtra(PlayService.EXTRA_L_DEV_S)) {
            double v = it.getDoubleExtra(PlayService.EXTRA_L_DEV_S, lDevS);
            if (Math.abs(v) <= 5) {
                lDevS = v;
            } else {
                Toast.makeText(this, "忽略非法 l_dev_s=" + v + "（|值| 应 ≤ 5 秒）",
                        Toast.LENGTH_LONG).show();
            }
        }
    }

    /**
     * `am start` 在**本 Activity 已在栈顶**时不会新建实例，而是把 intent 投给现有实例
     * （`am start` 打印 `Warning: Activity not started, intent has been delivered to
     * currently running top-most instance.`）。
     *
     * ★ 不实现这个方法 ⇒ `--ez sync true` 被**静默丢弃**：界面和实际注入都还是旧值，
     *   而 am start 的返回看着完全正常。
     * ★★ 但**实现了也不够**：10-24 真机实测（Android 14 / HONOR ALT-AN00）—— 在栈顶时
     *   `am start` 连这个方法都**不会调用**（这里打的 `onNewIntent extras=` 在 logcat 里
     *   一行都没有），intent 在 AMS 那层就被丢了。所以**改参数必须 `am force-stop` 后再
     *   `am start`**。保留本方法是因为通知点击、`singleTop` 前进等其他路径会走它，
     *   而"收到 intent 却不读"是最不该留的形态。
     */
    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        // ★ 现场记录：`am start` 的 warning 只说明 intent 被投递了，**不说明 extra 到了**。
        //   上一版就是靠这一行才分清「没调用 onNewIntent」与「调了但 hasExtra=false」。
        android.util.Log.i("PgPlay", "onNewIntent extras=" + (intent == null ? "null" : intent.getExtras()));
        readLaunchOverrides();
        refreshModeLabel();
    }

    /** 把当前口径刷到副标题上（`onCreate` 与 `onNewIntent` 都要走这里）。 */
    private void refreshModeLabel() {
        if (tvModeLabel != null) tvModeLabel.setText(subtitleText());
    }

    /**
     * 副标题：首页回答「补偿是多少、这是哪个包」。
     *
     * ★ G13：版本号自证。以前 `versionName` 写死 `0.1-m1s1d`，出了几个实验包之后
     *   再也没法确认手机上装的是哪一版（19:0x 回退事故）⇒ 构建信息必须上界面。
     * ★ 2026-11-01 用户定的最终口径：只留 **L_dev 与构建号**，删掉「端内闭环」那句
     *   与「注入模式 / 零点来源」两段 —— 后面这些与选了哪首歌无关，是全局运行模式，
     *   摆在首页会被当成当前曲的信息。
     * ⚠ 这里显示的 L_dev 是**本机默认值**（`lDevS`，初值取 `OnsetZero.DEFAULT_L_DEV_S`，
     *   只有启动参数能覆盖）。勾了「自动补偿」且该曲存过 K 时，实际生效的是
     *   `(K + c)/1000` ⇒ 那种情况下以难度卡上的 `K=… ⇒ L_dev=… s` 为准。
     * ⚠ selftest.sh 有断言钉着这行（旧串不许回来 + 新串必须在），改注释时别复写旧串原文。
     */
    private String subtitleText() {
        return "L_dev=" + String.format(java.util.Locale.ROOT, "%.3f", lDevS)
                + " · 构建 " + buildInfo().brief();
    }

    @Override
    protected void onDestroy() {
        try {
            Shizuku.removeRequestPermissionResultListener(permListener);
        } catch (Throwable ignored) {
            // 解注册失败不影响退出；进程马上就要走了，泄漏一个静态 list 里的回调也无害
        }
        h.removeCallbacksAndMessages(null);
        super.onDestroy();
    }

    @Override
    public void onBackPressed() {
        // 抽屉开着 ⇒ 先关抽屉（这是覆盖层，不该"退一步"）
        if (drawerOpen) {
            toggleDrawer(false);
            return;
        }
        // ★ 独立页（教程 / 课题）返回一律回选歌页，见字段区 2026-11-01 的注释。
        //   ⛔ 这两页绝不能落到下面的 `step--`：STEP_COURSE-1 就是 STEP_HELP。
        if (step == STEP_HELP || step == STEP_COURSE) {
            step = 1;
            render();
            return;
        }
        if (step > 1) {
            // ⛔ 这里**不能** saveScroll()：此刻滚动的是难度/开火页，会把列表位置覆盖成 0
            step--;
            render();
            return;
        }
        super.onBackPressed();
    }

    // ─────────────────────────── 数据 ───────────────────────────

    private void loadSongs() {
        try {
            groups = SongLib.group(SongLib.load(this));
        } catch (Throwable t) {
            groups = new ArrayList<>();
            Toast.makeText(this, "曲库读取失败：" + brief(t), Toast.LENGTH_LONG).show();
        }
    }

    private static String brief(Throwable t) {
        String m = t.getMessage();
        return t.getClass().getSimpleName() + (m == null || m.isEmpty() ? "" : " · " + m);
    }

    /**
     * Android 13+ 前台服务的通知需要用户授权；不给也能跑，但**注入期间看不到进度**
     * （通知是唯一能看到进度的地方）。所以请求，但不阻塞开火。
     */
    private void askNotificationPermission() {
        if (Build.VERSION.SDK_INT < 33) return;
        if (checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS)
                == PackageManager.PERMISSION_GRANTED) return;
        requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, 1);
    }

    // ─────────────────────────── 三步渲染 ───────────────────────────

    private void render() {
        if (content == null) return;
        content.removeAllViews();
        tvPhase = null;
        tvLog = null;
        logScroll = null;
        tvLogToggle = null;
        tvSelfTest = null;
        // 视图是新建的，上一轮的"已写入文本"必须清掉，否则新视图会一直是空的
        lastPhaseText = "";
        lastLogText = "";
        lastLogToggleText = "";
        updateChips();
        refreshCLabel();
        // 「回到最上方」只在长列表页有用：选曲(1)/难度(2)。开火页/帮助页隐藏，免得挡按钮。
        if (topBtn != null) {
            topBtn.setVisibility((step == 1 || step == 2) ? View.VISIBLE : View.GONE);
        }
        switch (step) {
            case 1: renderSongs(); break;
            case 2: renderDiffs(); break;
            case STEP_HELP: renderHelp(); break;
            case STEP_COURSE: renderCourse(); break;
            default: renderRun(); break;
        }
        refreshLogNow();
        // ★ 只在**回到曲库页**时恢复；且在布局完成后再滚（post 一帧不够，scrollTo 会被布局重置为 0）
        if (step == 1 && restoreScrollY >= 0 && scroll != null) {
            final int y = restoreScrollY;
            restoreScrollY = -1;
            scroll.postDelayed(() -> scroll.scrollTo(0, y), 60);
        }
    }

    private void updateChips() {
        String[] names = {"1 选曲", "2 难度", "3 开火"};
        // 帮助页不属于任何一步：三枚 chip 全不高亮（否则"全亮"会被读成"三步都完成了"）
        final boolean help = step == STEP_HELP;
        for (int i = 0; i < chips.length; i++) {
            boolean on = !help && (i + 1) <= step;
            boolean cur = !help && (i + 1) == step;
            chips[i].setText(names[i]);
            chips[i].setTextColor(cur ? 0xFF0E1116 : (on ? 0xFFDCE6F7 : 0xFF6B7684));
            chips[i].setBackground(round(cur ? ACCENT : (on ? 0xFF2A3B5C : 0xFF1B202A), 20));
            chips[i].setTypeface(Typeface.DEFAULT_BOLD);
        }
    }

    /** ① 曲目列表：按曲名排序（`SongLib.group` 已排好）。 */
    /** 记住当前滚动位置（下一次 render 后恢复）。 */
    private void saveScroll() {
        if (scroll != null) restoreScrollY = scroll.getScrollY();
    }

    private void renderSongs() {
        if (groups.isEmpty()) {
            content.addView(tip("assets 里没有可用曲目 —— 先跑 tools/pack_assets.py 重新打包。", ERR));
            return;
        }
        int notes = 0;
        for (SongLib.Group g : groups) {
            for (SongLib.Song s : g.variants()) notes += s.noteCount;
        }
        content.addView(section(String.format(Locale.ROOT,
                "选择歌曲 · %d 首 / %d 个难度 / %d 音符", groups.size(), countVariants(), notes)));

        // 搜索框：曲库到 291 首之后，靠滑动找歌不现实。只重建下面的列表，避免输入时丢焦点。
        EditText et = new EditText(this);
        et.setHint("搜索曲名（忽略大小写）");
        et.setTextSize(15);
        et.setTextColor(TEXT);
        et.setHintTextColor(DIM);
        et.setSingleLine(true);
        et.setText(songQuery);
        et.setSelection(songQuery.length());
        content.addView(wrapTop(et, 8));

        LinearLayout listBox = new LinearLayout(this);
        listBox.setOrientation(LinearLayout.VERTICAL);
        content.addView(listBox);
        et.addTextChangedListener(new TextWatcher() {
            @Override public void beforeTextChanged(CharSequence c, int a, int b, int d) { }
            @Override public void onTextChanged(CharSequence c, int a, int b, int d) { }
            @Override public void afterTextChanged(Editable e) {
                songQuery = e.toString();
                songListLimit = SONG_LIST_CAP;   // 换关键词 ⇒ 重新从 60 张开始
                fillSongList(listBox);
            }
        });
        fillSongList(listBox);
        content.addView(footnote("列表按曲名 A→Z 排序并可滑动；点一首歌进入难度选择。"));
    }

    private int countVariants() {
        int n = 0;
        for (SongLib.Group g : groups) n += g.variants().size();
        return n;
    }

    /**
     * 搜索归一化：**忽略大小写与所有空白**。
     *
     * ★ 为什么必须忽略空白（2026-10-26 用户报"那首备选歌怎么搜不到"）：曲库里确实存在名字带空格
     *   （甚至断词出错）的条目，例如 `Whatdoyouwantmorethana Happyending` —— "thana" 后面多一个空格。
     *   原来用 `name.contains(query)` 整串匹配 ⇒ 用户照 key 打成 `whatdoyouwantmorethanahappyending`
     *   就永远搜不到，第一反应是"曲库里没这首歌"。
     *   ⚠ 只去空白、不做模糊匹配 —— 否则搜索结果的顺序会变得不可预期。
     */
    private static String normForSearch(String s) {
        if (s == null) return "";
        StringBuilder b = new StringBuilder(s.length());
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            if (Character.isWhitespace(c) || c == '\u3000') continue;
            b.append(Character.toLowerCase(c));
        }
        return b.toString();
    }

    /** 按搜索词过滤后填列表（只动这个容器，搜索框保持焦点/光标）。 */
    private void fillSongList(LinearLayout box) {
        box.removeAllViews();
        String q = normForSearch(songQuery);
        int matched = 0, shown = 0;
        for (SongLib.Group g : groups) {
            if (!q.isEmpty() && !normForSearch(g.name).contains(q)) continue;
            matched++;
            if (shown >= songListLimit) continue;
            box.addView(songCard(g));
            shown++;
        }
        if (matched == 0) {
            box.addView(tip("没有匹配「" + songQuery.trim() + "」的曲目", DIM));
        } else if (matched > shown) {
            // ★ 以前是静默截断（用户以为「曲库没到 289 首」）。现在给按钮，数字摆明。
            box.addView(tip(String.format(Locale.ROOT,
                    "已显示 %d / 共 %d 首（往下滑可继续看；也可用搜索缩小）", shown, matched), DIM));
            TextView more = subButton(String.format(Locale.ROOT,
                    "显示更多（还有 %d 首）", matched - shown),
                    () -> {
                        songListLimit += 120;
                        fillSongList(box);
                    });
            box.addView(more);
        }
    }

    /**
     * 这首曲已存的标定值 `K`（ms）；没标过返回 null。
     *
     * ★ 为什么要在界面上显示：标定是「每曲一次」的隐性状态，用户根本记不住标过哪些。
     *   以前只有打出来才知道（退回默认值 → 一堆 Good），等于用成绩试错。
     *   读取口径必须与 PlayService 一致：先按曲名，再退回按 key。
     */
    /** 本曲的 K 是否被标记为"互相关不可靠"（`kbad_` 由 PlayService 写）。 */
    private boolean kUnreliable(SongLib.Song s) {
        return getSharedPreferences(PlayService.PREF, MODE_PRIVATE)
                .getInt("kbad_" + s.name, 0) == 1;
    }

    private Float calibK(SongLib.Song s) {
        android.content.SharedPreferences p =
                getSharedPreferences(PlayService.PREF, MODE_PRIVATE);
        // ★ 2026-10-30 修：读取口径必须与 `PlayService.kStored` **逐项一致**。
        //   以前这里还会遍历整组变体（`k_ms_<每档 key>`），于是：
        //     · 清除只删当前难度 ⇒ 兄弟难度那份把值顶回来 ⇒ 用户点了清除、toast 说已清除、
        //       那一行 K 照旧显示（真机复现，2026-10-30 uiautomator 实抓）；
        //     · 更糟的是服务侧 `kStored` 从不遍历变体 ⇒ 界面显示的 K 与打歌实际用的 K
        //       根本不是同一个数。现在两边都走 `CalibKeys.readKeys`。
        for (String k : CalibKeys.readKeys(s.name, s.key)) {
            float v = p.getFloat(k, Float.NaN);
            if (!Float.isNaN(v)) return v;
        }
        return null;
    }

    private void toast(String msg) {
        android.widget.Toast.makeText(this, msg, android.widget.Toast.LENGTH_SHORT).show();
    }

    /** 刷新顶部那几个参数小字（c / lead / K 都只从弹窗改，这里只负责显示）。 */
    private void refreshCLabel() {
        if (tvC != null) tvC.setText(String.format(Locale.ROOT, "c=%.0f ms", cMs()));
        // 选中曲：先看显式选中的，再退回"当前分组的第一首"（与 tvLead/tvK 的点击逻辑必须一致）
        SongLib.Song cur = selSong != null ? selSong
                : (selGroup != null && !selGroup.variants().isEmpty()
                        ? selGroup.variants().get(0) : null);
        if (tvLead != null) {
            if (cur == null) {
                tvLead.setVisibility(View.GONE);
            } else {
                Float l = leadOf(cur);
                tvLead.setVisibility(View.VISIBLE);
                tvLead.setText(l == null ? "lead=--" : String.format(Locale.ROOT, "lead=%.0f", l));
                tvLead.setTextColor(l == null ? 0xFF6C7A8C : 0xFF7FB8D1);
            }
        }
        if (tvK != null) {
            if (cur == null) {
                tvK.setVisibility(View.GONE);
            } else {
                Float k = calibK(cur);
                tvK.setVisibility(View.VISIBLE);
                if (k != null) {
                    tvK.setText(String.format(Locale.ROOT, "K=%+.1f ms", k));
                    tvK.setTextColor(0xFF7FB8D1);
                } else if (!Double.isNaN(cur.kPresetMs)) {
                    // 预置值也是"有 K 可用"，只是不能改（改它没有意义：它来自 assets）
                    tvK.setText(String.format(Locale.ROOT, "K=%+.0f ms（预置）", cur.kPresetMs));
                    tvK.setTextColor(0xFF9FB4CC);
                } else {
                    tvK.setText("K=--（点这里手填）");
                    tvK.setTextColor(0xFF6C7A8C);
                }
            }
        }
        refreshStartLabel();
    }

    /**
     * 弹窗改「设备常数 c」。
     *
     * ★ 它是什么：`L_dev = K + c` 里的**设备项**（捕获链滞后），一台设备一个值。
     * ⛔ 别把它和 K 混为一谈：K 是每曲测的，c 是换设备时人工定的。
     */
    private void showCDialog() {
        EditText in = new EditText(this);
        in.setInputType(android.text.InputType.TYPE_CLASS_NUMBER
                | android.text.InputType.TYPE_NUMBER_FLAG_SIGNED);
        in.setSingleLine(true);
        in.setText(String.format(Locale.ROOT, "%.0f", cMs()));
        in.setSelection(in.getText().length());
        new android.app.AlertDialog.Builder(this)
                .setTitle("设备常数 c（ms）")
                .setMessage("L_dev = K + c。c 是本机的捕获链滞后，换设备要重标。\n"
                        + "微调口诀：Good 全是 Late ⇒ 调小；全是 Early ⇒ 调大。\n"
                        + "（本机实测 177，2026-10-25）")
                .setView(in)
                .setPositiveButton("保存", (d, w) -> {
                    try {
                        float v = Float.parseFloat(in.getText().toString().trim());
                        if (v >= 0 && v <= 600) {
                            // ★ G1：手改 c 也必须 commit + 回读（旧代码是 apply()，进程被
                            //   force-stop 就丢写 ⇒ 界面显示新值、服务仍读旧值）
                            String err = PrefGuard.putFloat(
                                    getSharedPreferences(PlayService.PREF, MODE_PRIVATE), "c_ms", v);
                            if (err != null) toast("c 落盘失败：" + err);
                            refreshCLabel();
                            render();      // 难度页里的 L_dev 也跟着变
                        } else {
                            toast("c 要在 0~600 ms 之间");
                        }
                    } catch (NumberFormatException e) {
                        toast("不是合法数字");
                    }
                })
                .setNegativeButton("取消", null)
                .show();
    }

    /**
     * 顶部那行「▶ 落点 x,y」。
     *
     * ★ 2026-10-31 起不再显示"偏移 ±px"：手填偏移已废弃，落点要么是**学到的**、
     *   要么是出厂比例。显示的两个数必须**就是**打歌时会用的那两个 ——
     *   所以这里调 {@link StartPointPrefs}，不自己乘比例（本项目已经因为两处实现踩过两次）。
     */
    private void refreshStartLabel() {
        if (tvStart == null) return;
        int[] wh = landscapeSize();
        if (wh[0] <= 0 || wh[1] <= 0) {
            tvStart.setText("▶ 落点 ?");
            return;
        }
        float x = StartPointPrefs.tapX(this, wh[0], wh[0], wh[1]);
        float y = StartPointPrefs.tapY(this, wh[1], wh[0], wh[1]);
        tvStart.setText(String.format(Locale.ROOT, "▶ 落点 %.0f,%.0f", x, y));
    }

    /**
     * ▶ 落点偏移对话框（像素）。
     *
     * ★ 为什么是「偏移」不是「比例」：用户看得懂「往右 10 像素」，`0.945791` 看不懂，
     *   改错一位小数就是几十像素的偏差。出厂比例仍由 `PlayService.START_REL_X/Y` 唯一决定。
     * ⛔ 别照抄 {@link #showCDialog()} 的 `0~600` 校验：dx 可以是负数，照抄会误伤。
     */
    /**
     * 横屏尺寸（宽 >= 高）。与 {@link PlayService} 里那份同逻辑：
     * ⛔ 不用 `getResources().getDisplayMetrics()` —— 它给的是**当前方向**的尺寸，
     * 而本页是竖屏（Manifest `portrait`），拿到的会是 720×1610 而不是 1610×720。
     */
    private int[] landscapeSize() {
        android.hardware.display.DisplayManager dm =
                (android.hardware.display.DisplayManager) getSystemService(DISPLAY_SERVICE);
        android.view.Display d = dm == null ? null : dm.getDisplay(android.view.Display.DEFAULT_DISPLAY);
        if (d == null) return new int[]{0, 0};
        android.graphics.Point p = new android.graphics.Point();
        d.getRealSize(p);
        return new int[]{Math.max(p.x, p.y), Math.min(p.x, p.y)};
    }

    /**
     * 当前落点 + 来源，给对话框在动手前看。
     *
     * ★ 2026-10-31 撤掉了原来的"在框内 ✓ / 贴边 ⚠ / 出界 ✗"判读，理由有实测依据：
     *   `StartButtonBox.X0` 记的是 1465/1610，而真机图上按钮左边界实际是 **1439**（斜切边，
     *   每行都不同），框把按钮宽度量小了 26 px ⇒ 它算的"中心"偏右 13 px，
     *   曾经把"压在按钮左上角顶点"的点判成「在内 ✓」把人带偏一整晚。
     *   ⇒ 只显示**坐标与来源**，不再下"对准没对准"的结论。真正的判据只有「测试 ▶」。
     */
    private String currentStartVerdict() {
        int[] wh = landscapeSize();
        if (wh[0] <= 0 || wh[1] <= 0) return "（拿不到屏幕尺寸，重启 App 再试）";
        float x = StartPointPrefs.tapX(this, wh[0], wh[0], wh[1]);
        float y = StartPointPrefs.tapY(this, wh[1], wh[0], wh[1]);
        return String.format(Locale.ROOT, "屏幕 %dx%d ⇒ 当前落点 %.0f,%.0f\n来源：%s",
                wh[0], wh[1], x, y, StartPointPrefs.sourceOf(this, wh[0], wh[1]));
    }

    private void showStartDialog() {
        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL);
        box.setPadding(dp(20), dp(8), dp(20), dp(8));

        final TextView verdictTv = tv(currentStartVerdict(), 12, 0xFFA8B6CC, false);
        verdictTv.setPadding(0, 0, 0, dp(8));
        box.addView(verdictTv);

        box.addView(tv("⚠ 只记录【第一次】点击：点下面的按钮后 15 秒内切到游戏，\n"
                + "第一次必须点在右下角「开始演奏」按钮上。\n"
                + "先点了难度 / 曲目 / 返回，那个位置会被当成按钮 ⇒ 之后打歌会点空。\n"
                + "记完用右边「测试 ▶」验证：游戏开始加载 = 位置对。", 11, 0xFF6C7A8C, false));

        // ⛔ 只用可点文本，不用 Button：Button 有 48dp 最小高度，10-30 就是它把底部按钮行挤出屏幕。
        TextView learn = tv("▸ 学习落点：点这里 → 切到游戏 → 点一下「开始演奏」", 13, 0xFF7FB2FF, true);
        learn.setPadding(dp(2), dp(14), dp(2), dp(14));
        learn.setOnClickListener(v -> {
            int[] wh = landscapeSize();
            if (wh[0] <= 0 || wh[1] <= 0) {
                toast("拿不到屏幕尺寸，重启 App 再试");
                return;
            }
            startService(new Intent(this, PlayService.class)
                    .setAction(PlayService.ACTION_LEARN_TAP));
            verdictTv.setText("准备中…\n切回游戏停在选曲页，等提示「现在开始监听」再点「开始演奏」");
            // 窗口 19 s 内一定结束 ⇒ 21 s 后把界面刷成真实值（结果同时以 toast 报出）
            h.postDelayed(() -> {
                refreshStartLabel();
                verdictTv.setText(currentStartVerdict());
            }, 23_000L);
        });
        box.addView(learn);

        ScrollView sv = new ScrollView(this);
        sv.addView(box);

        new android.app.AlertDialog.Builder(this)
                .setTitle("▶ 落点")
                .setView(sv)
                .setPositiveButton("关闭", null)
                .setNeutralButton("恢复默认（0,0）", (d, w) -> {
                    String err = StartPointPrefs.clear(this);
                    if (err != null) {
                        toast("恢复默认失败：" + err);
                    } else {
                        toast("已恢复出厂落点（0,0）");
                    }
                    refreshStartLabel();
                })
                .show();
    }

    /** 设备常数 c（ms）：换设备要重标。存在 prefs 里，只从顶部弹窗改。 */
    private float cMs() {
        return getSharedPreferences(PlayService.PREF, MODE_PRIVATE)
                .getFloat("c_ms", PlayService.DEFAULT_C_MS);
    }

    /** 该曲的 lead 均值（ms）；没攒过返回 null。两段锚能不能用就看它。 */
    private Float leadOf(SongLib.Song s) {
        float v = getSharedPreferences(PlayService.PREF, MODE_PRIVATE)
                .getFloat(PlayService.LEAD_KEY + s.name, Float.NaN);
        return Float.isNaN(v) ? null : v;
    }

    /** G13：构建自证（`assets/build_info.txt`，`build.sh` 打的包才有）。 */
    private BuildInfo buildInfo() {
        try {
            return BuildInfo.read(getAssets().open("build_info.txt"));
        } catch (Throwable t) {
            return BuildInfo.UNKNOWN;
        }
    }

    /**
     * G2：设备指纹状态（{@link DevFp#NONE} / {@link DevFp#SAME} / {@link DevFp#DIFF}）。
     *
     * ⛔ 必须保留三态：把 `NONE`（首次运行，还没记）并进"同机"会让横幅显示一个
     *   从未建立过的结论；并进"不符"又会每台新机首跑就报红。两者都是假信号。
     */
    private int fpState() {
        String stored = getSharedPreferences(PlayService.PREF, MODE_PRIVATE)
                .getString(PlayService.FP_KEY, "");
        return DevFp.compare(stored, PlayService.fpOf(this));
    }

    /** K 的样本数（融合了多少次）。横幅上必须显示 —— 单独一个值看不出可信度。 */
    private int kSamples(SongLib.Song s) {
        return getSharedPreferences(PlayService.PREF, MODE_PRIVATE)
                .getInt("k_n_name_" + s.name, 0);
    }

    /** lead 的样本数。 */
    private int leadSamples(SongLib.Song s) {
        return getSharedPreferences(PlayService.PREF, MODE_PRIVATE)
                .getInt(PlayService.LEAD_N_KEY + s.name, 0);
    }

    /**
     * G11：开火前的**参数体检横幅**。
     *
     * ★ 为什么要有它：历史上白打的那几枪，参数都是"界面上看起来有"——
     *   实际是没落盘 / 被指纹判废 / 根本没标定。把这些隐性状态摊在「开始演奏」**上面**，
     *   一眼就能看出该不该开火。
     * ★ 三档颜色各有含义：绿 = 齐全；橙 = 能用但有已知欠缺（未标定/无 lead）；红 = 参数不可信。
     *   ⛔ 别把"橙"合并进"绿"：那等于把"渐入曲会偏几十毫秒"藏起来。
     * ★★ 2026-10-30 用户要求：**这一块只读，不可点**。它以前点了会打印全部 prefs 键值，
     *   而那行字很宽、误触概率高。⛔ 别把点击加回来（selftest 有反向断言钉着）。
     *   （2026-11-01：那个打印函数本身也删了，见下方按钮处的说明。）
     */
    private View paramBanner(SongLib.Song s) {
        final int fp = fpState();
        final boolean invalid = fp == DevFp.DIFF;          // 指纹变了 ⇒ 旧参数一律不算数
        final Float kRaw = calibK(s);
        final Float leadRaw = leadOf(s);
        final Float k = invalid ? null : kRaw;             // 显示口径必须与"能不能用"一致
        final Float lead = invalid ? null : leadRaw;
        final float c = cMs();
        final BuildInfo bi = buildInfo();

        StringBuilder b = new StringBuilder("参数体检  ");
        if (invalid) {
            b.append("K（作废）｜lead（作废）");
        } else {
            boolean kbad = kUnreliable(s);
            if (k != null) {
                b.append(String.format(Locale.ROOT, "K=%+.1f ms (n=%d)", k, kSamples(s)));
            } else if (!Double.isNaN(s.kPresetMs)) {
                b.append(String.format(Locale.ROOT, "K=%+.0f ms（预置，本曲自动标定测不出）", s.kPresetMs));
            } else {
                b.append(kbad ? "K=?（本曲互相关不可靠 ⇒ K 按 0 处理）" : "K=?（未标定）");
            }
            b.append("｜").append(lead == null ? "lead=?（无历史）"
                    : String.format(Locale.ROOT, "lead=%.0f ms (n=%d)", lead, leadSamples(s)));
        }
        b.append(String.format(Locale.ROOT, "｜c=%.0f ms%s", c, invalid ? "（作废，用默认 177）" : ""));
        b.append("\n");

        if (fp == DevFp.DIFF) {
            b.append("⚠ 设备指纹不符：旧 K/lead/c 一律不用");
        } else if (fp == DevFp.NONE) {
            b.append("设备指纹：未记录（首轮会自动记下）");
        } else {
            b.append("设备指纹 ✓ 同机");
        }
        b.append("｜构建 ").append(bi.brief());
        if (!bi.known()) b.append("（这包没带构建信息）");

        String fix = null;
        if (invalid) {
            fix = "⇒ 换过设备/系统：在这台机器上打一轮，成功后自动写回指纹";
        } else if (k == null) {
            boolean kbad2 = kUnreliable(s);
            fix = kbad2
                    ? "⇒ 本曲互相关不可靠（30 s 指纹太平）：K 按 0 处理，与「K=0」完全等价；"
                    + "L_dev 用 c，判定窗 ±80 ms 不受影响，仍可出 AP"
                    // ★ 2026-11-01：原来这里写「建议先点下面的「标定」」，而手动标定入口
                    //   已随开发版功能一起删除（runCalib 整段没了）⇒ 指向一个不存在的按钮。
                    //   自动标定仍在（打一轮就测 K），所以改成直接说这条路径。
                    : "⇒ 本曲还没测过 K：本轮 L_dev 退到 c；渐入曲会偏几十 ms。打完一轮会自动测出 K。";
        } else if (s.blindLost > 0 && lead == null) {
            fix = String.format(Locale.ROOT,
                    "⇒ Ⅱ 类但还没有 lead：两段锚不可用，开头 %d 个音符本轮会丢"
                            + "（打完这轮就有 lead，下一轮起会自动预派）", s.blindLost);
        }
        if (fix != null) b.append("\n").append(fix);

        final boolean red = invalid;
        TextView t = tv(b.toString(), 12, red ? ERR : (fix == null ? OK : 0xFFE0A96D), false);
        t.setLineSpacing(dp(3), 1f);
        t.setPadding(dp(12), dp(11), dp(12), dp(11));
        t.setBackground(roundStroke(0xFF12161D, 10, red ? ERR : STROKE));
        // ⛔ 不挂点击（2026-10-30 用户要求）：这行字很宽，误触概率高，而且点了只是把
        //   同样的值再打一份到日志里（那个函数 2026-11-01 已删）。
        return wrapTop(t, 10);
    }


    /**
     * 手填/修正某曲的 lead（ms）。
     *
     * ★ 为什么需要：预派要求在**开打之前**就知道 lead，而 lead 只能从"打过一轮"里量出来
     *   ⇒ 严格来说每首 Ⅱ 曲的第 1 轮永远补不上盲区。手动入口让"已经量到值"的场合直接开打
     *   （值可以从前一轮的 trace 头 `zero_rel_ms` 读）。
     */
    private void showLeadDialog(SongLib.Song s) {
        Float cur = leadOf(s);
        EditText in = new EditText(this);
        in.setInputType(android.text.InputType.TYPE_CLASS_NUMBER);
        in.setSingleLine(true);
        if (cur != null) in.setText(String.format(Locale.ROOT, "%.0f", cur));
        new android.app.AlertDialog.Builder(this)
                .setTitle("lead（ms）— " + s.name)
                .setMessage("lead = 点 ▶ → 谱面 t=0 的时间。预派用它预测零点。\n"
                        + "可以从前一轮 trace 头的 zero_rel_ms 读到（例：996 IN ≈ 7190）。\n"
                        + "留空 = 清除（回到「下一轮自动记」）。")
                .setView(in)
                .setNeutralButton("清除", (d, w) -> {
                    // ★ G1：删除同样要校验（丢写会造成"界面说已清除、预派仍用旧 lead"）
                    String err = PrefGuard.remove(
                            getSharedPreferences(PlayService.PREF, MODE_PRIVATE),
                            PlayService.LEAD_KEY + s.name, PlayService.LEAD_N_KEY + s.name);
                    toast(err == null ? "已清除 lead" : "清除失败：" + err);
                    render();
                })
                .setPositiveButton("保存", (d, w) -> {
                    String txt = in.getText().toString().trim();
                    if (txt.isEmpty()) {
                        // ⛔ 空值 = 什么都不改。以前空值=清除，用户误触后一点保存就把 lead 抹了。
                        toast("未修改（要清除请点「清除」）");
                    } else {
                        float v;
                        try {
                            v = Float.parseFloat(txt);
                        } catch (NumberFormatException e) {
                            toast("不是合法数字");
                            return;
                        }
                        if (v < 1000 || v > 120_000) {
                            toast("lead 应在 1000~120000 ms");
                            return;
                        }
                        // ★ G1：两个键都要 commit + 回读；任何一个失败就不能报"已存"
                        android.content.SharedPreferences p =
                                getSharedPreferences(PlayService.PREF, MODE_PRIVATE);
                        String e1 = PrefGuard.putFloat(p, PlayService.LEAD_KEY + s.name, v);
                        String e2 = e1 != null ? null
                                : PrefGuard.putInt(p, PlayService.LEAD_N_KEY + s.name, 1);
                        String err = e1 != null ? e1 : e2;
                        toast(err == null ? "已存 lead=" + (int) v + " ms" : "lead 落盘失败：" + err);
                    }
                    render();
                })
                .setNegativeButton("取消", null)
                .show();
    }

    /**
     * 写一条**人工 K**（ms）。返回 null = 全部写成功；否则是错误串。
     *
     * ★ 为什么要写这么多键（2026-10-30 用户要求补 K 的人工入口）：
     *   · `k_ms_name_<曲名>` 是主口径（服务侧 `kStored` 先读它）；
     *   · `k_ms_<key>` 是旧口径，`kStored` / `calibK` 都还有兜底分支读它 ⇒
     *     不写就会「界面有新值、老路径读旧值」；
     *   · `k_n_name_` 写 **1**：把人工值当**一条样本**参与 `CalibMerge`。写成 0 的话，
     *     下一次自动标定会当"没有基线"直接覆盖成首个样本 ⇒ 人工值静默失效；
     *   · `k_side_n_` / `k_side_sign_` 清 0：那是旧样本的"连续同侧"计数，留着会把新值判离群；
     *   · `kbad_` 清 0：否则横幅一直写「本曲互相关不可靠 ⇒ K 按 0 处理」，与刚存的值自相矛盾。
     */
    private String saveK(SongLib.Song s, float v) {
        android.content.SharedPreferences p = getSharedPreferences(PlayService.PREF, MODE_PRIVATE);
        String e = PrefGuard.putFloat(p, "k_ms_name_" + s.name, v);
        if (e == null) e = PrefGuard.putFloat(p, "k_ms_" + s.key, v);
        if (e == null) e = PrefGuard.putInt(p, "k_n_name_" + s.name, 1);
        if (e == null) e = PrefGuard.putInt(p, "k_side_n_" + s.name, 0);
        if (e == null) e = PrefGuard.putInt(p, "k_side_sign_" + s.name, 0);
        if (e == null) e = PrefGuard.putInt(p, "kbad_" + s.name, 0);
        return e;
    }

    /**
     * 清掉该曲的 K（含**整组难度**的旧口径 key、融合状态与"不可靠"标记）。返回 null = 成功。
     *
     * ★ 2026-10-30 真机 bug：原来只删 `k_ms_<当前难度>`，而同一首曲的另一个难度可能还留着
     *   `k_ms_<那一档>`。清除后 `calibK` 会遍历变体把它读回来 ⇒ 「toast 说已清除、那一行 K 还在」。
     *   ⇒ 必须删整组；`CalibKeys.removeKeys` 保证「删除集 ⊇ 读取集」。
     * ⛔ 变体列表兜底用 `Collections.singletonList`，**不是** `List.of` —— 构建链没开 desugaring。
     */
    private String clearK(SongLib.Song s) {
        java.util.List<String> keys = new java.util.ArrayList<>();
        SongLib.Group g = groupOf(s.name);
        if (g != null) {
            for (SongLib.Song v : g.variants()) keys.add(v.key);
        }
        if (keys.isEmpty()) keys.add(s.key);      // 列表未加载/曲名对不上时的兜底
        return PrefGuard.remove(getSharedPreferences(PlayService.PREF, MODE_PRIVATE),
                CalibKeys.removeKeys(s.name, s.key, keys));
    }

    /**
     * 顶部 `K=…` 小字的弹窗：手填/修正/清除**当前选中曲**的 K。
     *
     * ⛔ 与前两天那个坑直接相关：标定时如果游戏里放的是**另一首**，互相关是错配的，
     *   而错配值一旦被采纳就会写到「App 里选的那首」名下。以前没有任何清除入口 ⇒
     *   只能连打 3 轮（`CalibMerge` 连续 3 次同侧才重置）硬顶。这个入口就是那个补救手段。
     */
    private void showKDialog(SongLib.Song s) {
        Float cur = calibK(s);
        EditText in = new EditText(this);
        // ★ 2026-10-30：K 是**浮点**量（自动标定会写 +0.1 这种值），键盘必须带小数点。
        //   之前只给了 SIGNED ⇒ 用户反馈"小数点打不出来、只能填整数"，想手填 0.1 做不到。
        //   ⛔ 别只加在其中一个弹窗：这里和 showKLeadDialog 的 kIn 是两处，都得有。
        in.setInputType(android.text.InputType.TYPE_CLASS_NUMBER
                | android.text.InputType.TYPE_NUMBER_FLAG_SIGNED
                | android.text.InputType.TYPE_NUMBER_FLAG_DECIMAL);
        in.setSingleLine(true);
        if (cur != null) {
            in.setText(String.format(Locale.ROOT, "%.1f", cur));
            in.setSelection(in.getText().length());
        }
        new android.app.AlertDialog.Builder(this)
                .setTitle("K（ms）— " + s.name)
                .setMessage("L_dev = K + c。K 是曲目属性（由起音形状决定），按曲名存"
                        + " ⇒ 改这一次，该曲所有难度都变。\n"
                        + "实测值域 −7~+53 ms；方向：Good 全 Late ⇒ K 调小，全 Early ⇒ 调大。\n"
                        + "留空 = 不改；要清空请点「清除」（清完 L_dev 退回用 c 兜底）。")
                .setView(in)
                .setNeutralButton("清除", (d, w) -> {
                    String err = clearK(s);
                    toast(err == null ? "已清除 K（下一轮 L_dev 退到 c）" : "清除失败：" + err);
                    render();
                })
                .setPositiveButton("保存", (d, w) -> {
                    String txt = in.getText().toString().trim();
                    if (txt.isEmpty()) {
                        // ⛔ 空值**不**当清除：lead 弹窗历史上就是这么误抹的（见 showLeadDialog 注释）
                        toast("未修改（要清除请点「清除」）");
                    } else {
                        float v;
                        try {
                            v = Float.parseFloat(txt);
                        } catch (NumberFormatException ex) {
                            toast("不是合法数字");
                            return;
                        }
                        if (v < -150 || v > 150) {
                            toast("K 应在 −150~150 ms（超范围只可能是互相关错配）");
                            return;
                        }
                        String err = saveK(s, v);
                        toast(err == null ? String.format(Locale.ROOT, "已存 K=%+.1f ms", v)
                                : "K 落盘失败：" + err);
                    }
                    render();
                })
                .setNegativeButton("取消", null)
                .show();
    }

    /**
     * 课题页每首歌的「改」：**同时**改 K 和 lead（2026-10-30 用户要求）。
     *
     * ★ 两行各带一个「清」而不是靠"空值=清除"：那是 lead 老弹窗踩过的坑
     *   （用户误触保存就把值抹了）⇒ 清除必须是**显式**动作。
     * ★ 先全部校验、再统一写：否则会出现"K 存进去了、lead 因为非法没存"的半写状态。
     */
    private void showKLeadDialog(SongLib.Song s) {
        final android.content.SharedPreferences p =
                getSharedPreferences(PlayService.PREF, MODE_PRIVATE);
        final Float k0 = calibK(s);
        final Float l0 = leadOf(s);

        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL);
        box.setPadding(dp(20), dp(10), dp(20), 0);

        final EditText kIn = new EditText(this);
        // ★ 2026-10-30：同 showKDialog —— K 必须能输入小数（+0.1 这种），键盘要带 DECIMAL。
        kIn.setInputType(android.text.InputType.TYPE_CLASS_NUMBER
                | android.text.InputType.TYPE_NUMBER_FLAG_SIGNED
                | android.text.InputType.TYPE_NUMBER_FLAG_DECIMAL);
        kIn.setSingleLine(true);
        if (k0 != null) kIn.setText(String.format(Locale.ROOT, "%.1f", k0));

        final EditText lIn = new EditText(this);
        lIn.setInputType(android.text.InputType.TYPE_CLASS_NUMBER);
        lIn.setSingleLine(true);
        if (l0 != null) lIn.setText(String.format(Locale.ROOT, "%.0f", l0));

        box.addView(tv("K（ms）— 曲目属性，该曲所有难度共用", 11, 0xFF9FB4CC, false));
        LinearLayout r1 = new LinearLayout(this);
        r1.setOrientation(LinearLayout.HORIZONTAL);
        r1.setGravity(Gravity.CENTER_VERTICAL);
        r1.addView(kIn, new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f));
        TextView ck = tv("清", 13, 0xFF7FA6D9, true);
        ck.setPadding(dp(12), dp(8), dp(4), dp(8));
        ck.setOnClickListener(v -> {
            String err = clearK(s);
            toast(err == null ? "已清除 K" : "清除 K 失败：" + err);
            kIn.setText("");
            render();
        });
        r1.addView(ck);
        box.addView(r1);

        box.addView(tv("lead（ms）— 点 ▶ → 谱面 t=0，预派用它", 11, 0xFF9FB4CC, false));
        LinearLayout r2 = new LinearLayout(this);
        r2.setOrientation(LinearLayout.HORIZONTAL);
        r2.setGravity(Gravity.CENTER_VERTICAL);
        r2.addView(lIn, new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f));
        TextView cl = tv("清", 13, 0xFF7FA6D9, true);
        cl.setPadding(dp(12), dp(8), dp(4), dp(8));
        cl.setOnClickListener(v -> {
            String err = PrefGuard.remove(p,
                    PlayService.LEAD_KEY + s.name, PlayService.LEAD_N_KEY + s.name);
            toast(err == null ? "已清除 lead" : "清除 lead 失败：" + err);
            lIn.setText("");
            render();
        });
        r2.addView(cl);
        box.addView(r2);

        new android.app.AlertDialog.Builder(this)
                .setTitle("K / lead — " + s.name)
                .setMessage("留空 = 该项不改。K 范围 −150~150 ms，lead 范围 1000~120000 ms。")
                .setView(box)
                .setPositiveButton("保存", (d, w) -> {
                    // ① 先全部解析 + 校验（任何一项非法 ⇒ 一个都不写）
                    Float kv = null;
                    Float lv = null;
                    String kt = kIn.getText().toString().trim();
                    String lt = lIn.getText().toString().trim();
                    if (!kt.isEmpty()) {
                        try {
                            kv = Float.parseFloat(kt);
                        } catch (NumberFormatException ex) {
                            toast("K 不是合法数字（未保存任何值）");
                            return;
                        }
                        if (kv < -150 || kv > 150) {
                            toast("K 应在 −150~150 ms（未保存任何值）");
                            return;
                        }
                    }
                    if (!lt.isEmpty()) {
                        try {
                            lv = Float.parseFloat(lt);
                        } catch (NumberFormatException ex) {
                            toast("lead 不是合法数字（未保存任何值）");
                            return;
                        }
                        if (lv < 1000 || lv > 120_000) {
                            toast("lead 应在 1000~120000 ms（未保存任何值）");
                            return;
                        }
                    }
                    // ② 再统一写
                    StringBuilder done = new StringBuilder();
                    if (kv != null) {
                        String err = saveK(s, kv);
                        if (err != null) {
                            toast("K 落盘失败：" + err);
                            return;
                        }
                        done.append(String.format(Locale.ROOT, "K=%+.1f ", kv));
                    }
                    if (lv != null) {
                        String e1 = PrefGuard.putFloat(p, PlayService.LEAD_KEY + s.name, lv);
                        String e2 = e1 != null ? null
                                : PrefGuard.putInt(p, PlayService.LEAD_N_KEY + s.name, 1);
                        String err = e1 != null ? e1 : e2;
                        if (err != null) {
                            toast("lead 落盘失败：" + err);
                            return;
                        }
                        done.append(String.format(Locale.ROOT, "lead=%.0f ", lv));
                    }
                    toast(done.length() == 0 ? "未修改" : "已存 " + done.toString().trim());
                    render();
                })
                .setNegativeButton("取消", null)
                .show();
    }

    private SongLib.Group groupOf(String name) {
        for (SongLib.Group g : groups) if (g.name.equals(name)) return g;
        return null;
    }

    /**
     * 一次性迁移：把旧口径的 `k_ms_<key>`（按难度）搬成 `k_ms_name_<曲名>`（按曲）。
     *
     * ★ 为什么必须迁移：标定的设计口径是**一曲一次**（K 只与母带/首音有关），
     *   而旧数据是按 key 存的（key 里带难度）⇒ 同一首曲的 EZ/HD/IN 里只有一个认得到 K，
     *   界面上就会显示成「这首 `✓ 已标定`，但另外两个难度 `未标定`」——正好是自相矛盾。
     */
    private void migrateCalibKeys() {
        android.content.SharedPreferences p =
                getSharedPreferences(PlayService.PREF, MODE_PRIVATE);
        // ★★ 2026-10-30 真机 bug 的主因：这本来是"一次性迁移"，却挂在 `onCreate` 上
        //   **每次冷启动都跑**。用户点「清除」删掉 `k_ms_name_` 之后，下次启动又会被它
        //   从旧口径 `k_ms_<key>` 搬回来 ⇒「清除了，但 K 过一会儿又自己出现」。
        //   ⇒ 必须落标记：跑过就不再跑。（标记名带 v2，将来换口径用 v3，不要复用）
        if (p.getInt(MIGRATE_K_FLAG, 0) == 1) return;
        java.util.Map<String, Float> legacy = new java.util.HashMap<>();
        for (java.util.Map.Entry<String, ?> e : p.getAll().entrySet()) {
            String k = e.getKey();
            if (!k.startsWith("k_ms_") || k.startsWith("k_ms_name_")) continue;
            if (e.getValue() instanceof Float) legacy.put(k.substring(6), (Float) e.getValue());
        }
        if (legacy.isEmpty()) {
            // ★ 空表也要打标记：否则每次启动都白扫一遍（而且"没东西可迁"正是最该记下来的状态）
            PrefGuard.putInt(p, MIGRATE_K_FLAG, 1);
            return;
        }
        int moved = 0;
        int failed = 0;
        for (java.util.Map.Entry<String, Float> e : legacy.entrySet()) {
            SongLib.Song s = null;
            for (SongLib.Group g : groups) {
                for (SongLib.Song v : g.variants()) if (v.key.equals(e.getKey())) s = v;
            }
            if (s == null) continue;
            String nk = "k_ms_name_" + s.name;
            if (p.contains(nk)) {
                // ★ 按曲口径已经有值（另一个难度先迁过了）⇒ 这一条没人会读，删掉。
                //   留着它就是"清除后被搬回来"的种子。
                PrefGuard.remove(p, "k_ms_" + s.key);
                continue;
            }
            // ★ G1：迁移也是写参数，同样走 commit + 回读（旧代码是攒一个 Editor 再 apply()）
            if (PrefGuard.putFloat(p, nk, e.getValue()) == null) {
                moved++;
                // ★ 迁完即删旧 key —— 只复制不删除是 10-30 bug 的根源
                PrefGuard.remove(p, "k_ms_" + s.key);
            } else {
                failed++;
            }
        }
        if (moved > 0) System.out.println("[calib] 迁移了 " + moved + " 条旧 K 到按曲口径");
        if (failed > 0) {
            // 有失败就不置标记 ⇒ 下次启动只重试失败的那几条（成功的旧 key 已经删掉了）
            System.out.println("[calib] 有 " + failed + " 条旧 K 迁移落盘失败（下一轮会重试）");
        } else {
            PrefGuard.putInt(p, MIGRATE_K_FLAG, 1);
        }
    }

    /** 组内任一难度已标定 ⇒ 这首曲就算「标过」。 */
    private boolean groupCalibrated(SongLib.Group g) {
        for (SongLib.Song s : g.variants()) if (calibK(s) != null) return true;
        return false;
    }

    private View songCard(SongLib.Group g) {
        LinearLayout card = card();
        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL);
        box.addView(tv(g.name, 17, TEXT, true));
        int lost = 0;
        for (SongLib.Song v : g.variants()) lost = Math.max(lost, v.blindLost);
        String subText = g.subtitle() + (groupCalibrated(g) ? "  · ✓ 已标定" : "  · 未标定")
                + (lost > 0 ? "  · ⚠ 首音盲区" : "");
        TextView sub = tv(subText, 12, groupCalibrated(g) ? 0xFF7FD1A0 : DIM, false);
        sub.setPadding(0, dp(4), 0, 0);
        box.addView(sub);
        card.addView(box, new LinearLayout.LayoutParams(0,
                ViewGroup.LayoutParams.WRAP_CONTENT, 1f));
        card.addView(tv("›", 22, DIM, false));
        card.setOnClickListener(v -> {
            saveScroll();               // 记住列表位置：退回时回到这首歌，而不是最顶上
            selGroup = g;
            selSong = g.variants().size() == 1 ? g.variants().get(0) : null;
            step = 2;
            render();
        });
        return wrapTop(card, 10);
    }

    /** ② 难度列表：只列该曲**已打包**的难度（不虚构没打包的难度）。 */
    private void renderDiffs() {
        if (selGroup == null) {
            step = 1;
            render();
            return;
        }
        content.addView(backLink("‹ 重新选歌", () -> {
            step = 1;
            render();
        }));
        content.addView(section("《" + selGroup.name + "》选择难度"));
        for (SongLib.Song s : selGroup.variants()) {
            content.addView(diffCard(s));
        }
        if (selGroup.variants().size() == 1) {
            content.addView(footnote("该曲目前只打包了 1 个难度（assets 里就这一个）。"
                    + "要加别的难度：PC 侧用 play.py --diff 出载荷再重跑 pack_assets.py。"));
        } else {
            content.addView(footnote("难度按 EZ → HD → IN → AT 排序。"));
        }
    }

    private View diffCard(SongLib.Song s) {
        LinearLayout card = card();
        TextView badge = tv(s.diff, 13, 0xFF0E1116, true);
        badge.setGravity(Gravity.CENTER);
        badge.setBackground(round(diffColor(s.diff), 8));
        badge.setPadding(dp(9), dp(5), dp(9), dp(5));
        card.addView(badge);

        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL);
        LinearLayout.LayoutParams blp = new LinearLayout.LayoutParams(0,
                ViewGroup.LayoutParams.WRAP_CONTENT, 1f);
        blp.leftMargin = dp(12);
        // ★ 等级只有手上有真据才显示（现在只有 bounded_in=16 有截图出处）。
        //   以前未知时显示 "Lv.?"，用户一眼看不出是"没数据"还是"显示坏了" ⇒ 直接不显示。
        String lv = s.level > 0 ? "Lv." + s.level : "";
        if (!lv.isEmpty()) box.addView(tv(lv, 16, TEXT, true));
        Float k = calibK(s);
        String kText;
        if (k != null) {
            kText = String.format(Locale.ROOT, "K=%+.1f ms ⇒ L_dev=%.3f s", k, (k + cMs()) / 1000.0);
        } else if (!Double.isNaN(s.kPresetMs)) {
            // 本曲自动标定测不出 K（指纹太平），用**预置值**兜底；不需要用户填任何东西
            kText = String.format(Locale.ROOT, "K=%+.0f ms（预置）⇒ L_dev=%.3f s",
                    s.kPresetMs, (s.kPresetMs + cMs()) / 1000.0);
        } else {
            kText = "K=?（未标定：打一轮会自动补上）";
        }
        TextView sub = tv(s.noteCount + " 音符 · " + kText, 12, k == null ? 0xFFE0A96D : 0xFF7FD1A0, false);

        sub.setPadding(0, dp(3), 0, 0);
        box.addView(sub);
        if (s.blindLost > 0) {
            Float lead = leadOf(s);
            TextView pre = tv(lead == null
                            ? "⚓ 两段锚：还没 lead（打一轮，或点顶部 lead= 手填）"
                            : String.format(Locale.ROOT,
                                    "⚓ 两段锚就绪（lead 均值 %.0f ms）：盲区音符按预测先打",
                                    lead),
                    12, lead == null ? DIM : 0xFF7FB8D1, false);
            pre.setPadding(0, dp(4), 0, 0);
            // ⛔ 这里**不放**任何点击：卡片整块是"进入打歌"的区域，之前把 lead 编辑挂在这行，
            //   用户想开始打歌却点到它、又在弹窗里按了保存（空值=清除）⇒ lead 被清掉、预派失效。
            //   入口统一挪到顶部 c 旁边的小字。
            box.addView(pre);
            // ★ Ⅱ 类：首音落在起音盲区（0.342 s）里 —— **只在没有 lead 时才会丢**。
            //   ⛔ 旧文案断言「必丢、拿不到 φ」，已被真机证伪：moonlight IN 的 blind=4 仍打出 φ
            //      （两段锚预派 + rezero 覆盖了盲区）。⛔ 评论区也别复写旧文案原文：selftest 按字面串查。
            //   括号里的百分比 = 未预派时的上限（一个 Miss 只拿该音符 0.65 的分）。
            double best = 100.0 * (1.0 - 0.65 * s.blindLost / Math.max(1, s.noteCount));
            TextView warn = tv(String.format(Locale.ROOT,
                    "⚠ 首音盲区：开头 %d 个音符落在 0.342 s 内 —— 没有 lead 时会丢（最高约 %.2f%%）；"
                            + "有 lead 时两段锚会先派发它们", s.blindLost, best), 12, 0xFFD98A5A, false);
            warn.setPadding(0, dp(4), 0, 0);
            box.addView(warn);
        }
        // ★ 2026-10-26：**10 指压力**。并发到 9~10 根时，调度器会开始抢正在长按的手指，
        //   真机表现为偶发纯 Miss（0 Good / 0 Bad）——这是**结构限制**，不是能调好的 bug
        //   （全库只有 6 首，`distort_at` 到 10）。先说清楚，别把结构问题当故障追。
        // ★ 2026-10-28：已用真机证明能 AP 的谱面（manifest `ten_finger_ok`）**不再**打这条警告
        //   —— 原文案「φ 概率低 / 结构限制」对它们已不成立（`erabyeconnec10n_at`、千紫万紅 IN 均已 φ）。
        if (s.pointerPeak >= 9 && !s.tenFingerOk) {
            TextView pw = tv(String.format(Locale.ROOT,
                    "⚠ 并发 %d 指：指针会被占满，极少数段落可能被压成一次扫动", s.pointerPeak),
                    12, 0xFF9A7BD0, false);
            pw.setPadding(0, dp(4), 0, 0);
            box.addView(pw);
        }
        card.addView(box, blp);
        card.addView(tv("›", 22, DIM, false));

        card.setOnClickListener(v -> {
            selSong = s;
            step = 3;
            render();
        });
        return wrapTop(card, 10);
    }

    private static int diffColor(String diff) {
        switch (diff == null ? "" : diff.toUpperCase(Locale.ROOT)) {
            case "EZ": return 0xFF4ADE80;
            case "HD": return 0xFF5B8DEF;
            case "IN": return 0xFFA78BFA;
            case "AT": return 0xFFFB923C;
            default: return 0xFF94A3B8;
        }
    }

    /** ③ 开火：详情 + 开始/停止 + 实时日志。 */
    private void renderRun() {
        if (selSong == null) {
            step = 1;
            render();
            return;
        }
        final SongLib.Song s = selSong;
        content.addView(backLink("‹ 重新选难度", () -> {
            step = 2;
            render();
        }));
        content.addView(section("《" + s.title() + "》"
                + s.diff + (s.level > 0 ? " " + s.level : "")));

        // ★ G11：参数体检横幅必须在**「开始演奏」上面**（见 paramBanner 注释）
        content.addView(paramBanner(s));



        TextView det = tv(String.format(Locale.ROOT,
                "key      %s\n"
                        + "载荷     %s\n"
                        + "音符     %d（事件 %d，关键 %d）\n"
                        + "时间     %.3f ~ %.3f s\n"
                        + "sil_ref  %.6f s", s.key, s.payload, s.noteCount, s.nEvents, s.nKey,
                s.firstTMs / 1000.0, s.lastTMs / 1000.0, s.silRefS), 11, 0xFFC3CCDA, false);
        det.setTypeface(Typeface.MONOSPACE);
        det.setBackground(round(0xFF0B0E13, 10));
        det.setPadding(dp(12), dp(12), dp(12), dp(12));
        content.addView(wrapTop(det, 10));

        // ★ 2026-10-25 用户要求：开火页**只留操作与参数**。
        //   原来的「开火前必读三步」与橙色「会让标定作废」两段已整段搬进
        //   `renderHelp()`（左侧 ☰ → 教程/帮助）。⛔ 别在这里再加"教程式"长文本。
        //   ⚠ 连注释里也别复写它们的原文：selftest 的顺序闸门按字面串判"教程有没有搬回去"。

        CheckBox cbAuto = new CheckBox(this);
        cbAuto.setText("自动 L_dev（用端内标定的 K + c；没标定则用启动参数）");
        cbAuto.setTextSize(13);
        cbAuto.setTextColor(0xFFC9D1DD);
        cbAuto.setChecked(autoLDev);
        cbAuto.setOnCheckedChangeListener((b, checked) -> autoLDev = checked);
        content.addView(wrapTop(cbAuto, 6));

        // ★ 放在「开始演奏」**之前**：没有这支权限，真注入一定在 prepare 阶段被门禁拒绝。
        // ★ 2026-11-01 用户要求挪到「自动 L_dev」**下面**（原来在它上面）。
        content.addView(wrapTop(shizukuPermRow(), 8));

        LinearLayout row = new LinearLayout(this);
        row.setOrientation(LinearLayout.HORIZONTAL);
        row.setBaselineAligned(false);
        mainButton("开始演奏", () -> {
            if (RunLog.running()) {
                Toast.makeText(this, "已有一轮在跑（看下面的日志）", Toast.LENGTH_SHORT).show();
                return;
            }
            startRun(s);
        }, row);
        LinearLayout.LayoutParams slp = new LinearLayout.LayoutParams(0,
                ViewGroup.LayoutParams.WRAP_CONTENT, 1f);
        slp.leftMargin = dp(8);
        TextView stop = subButton("停止", () -> {
            startService(new Intent(this, PlayService.class).setAction(PlayService.ACTION_STOP));
            Toast.makeText(this, "已发送停止指令", Toast.LENGTH_SHORT).show();
        });
        row.addView(stop, slp);
        content.addView(wrapTop(row, 12));

        tvPhase = tv("阶段：空闲", 15, TEXT, true);
        content.addView(wrapTop(tvPhase, 16));

        // ★ 日志：收起时整块不占位；展开后固定高度、内部滑动看**全部**行（2026-11-01 用户定）。
        //   ⚠ 点击只挂在标题行上：日志块由 `LogScroll` 独占触摸，连单击都收不到（且不需要）。
        content.addView(logPanel(6));

        // ★ 2026-11-01 按用户要求删掉「参数诊断」与「服务侧探针」两个按钮：
        //   ① 它们打印的 K / lead / c 在曲目卡与难度卡上已经有了，且格式比 key=value 好读；
        //   ② 两者读的是**同一份数据** —— manifest 里 PlayService 没写 android:process，
        //      与 Activity 同进程，SharedPreferences 按文件名缓存是同一个实例
        //      ⇒ "服务侧"根本没有第二个视角可对照，输出的东西一字不差；
        //   ③ `dumpCalibPrefs()` 第一件事是 RunLog.clear()，手滑就把上一轮日志抹了。
        content.addView(subButton("三树自检（probe / injector / play）", this::runSelfTest));
        tvSelfTest = tv(selfTestText, 11, 0xFF8F9AAB, false);
        tvSelfTest.setTypeface(Typeface.MONOSPACE);
        tvSelfTest.setPadding(0, dp(8), 0, 0);
        // ⛔ 不许开可选中：自检结果是给人看的，长按会弹出复制/选择手柄（用户 11-01 要求去掉）
        tvSelfTest.setTextIsSelectable(false);
        tvSelfTest.setFocusable(false);
        tvSelfTest.setLongClickable(false);
        content.addView(tvSelfTest);
        // ★ 2026-11-01 按用户要求删掉页面最底部的 footnote：
        //   原文「判定线 y=360（横屏 1610x720）。真注入需要 Shizuku 已授权并运行。」
        //   ① `y=360` 与 `1610x720` 都是 ALT-AN00 的实测值，**硬编码** ⇒ 换设备就变成假参数
        //      （教训 2：设备绝对像素是换机头号杀手）；
        //   ② 后半句与上方 `shizukuPermRow()` 的三态显示重复（未连接 / 未授权 / 已授予 ✓）。
        //   ⛔ 别把"判定线 y=…"再写进界面：它随分辨率、黑边(fit=height 165 px)、密度变。
    }

    // ─────────────────────────── 教程/帮助（左侧 ☰ → 教程 / 帮助）───────────────────────────

    /**
     * 「教程/帮助」页：**开火页搬出来的说明文字都落在这里**（2026-10-25 用户要求）。
     *
     * ★ 为什么单独一页而不是折在开火页里：开火页是"操作面板"，长文会把「开始演奏」挤出首屏，
     *   而且同一段话在开火页出现时，用户每次都要滑过它才能点按钮。教程是"查一次"的东西，
     *   收进导航栏里更符合它的使用频率。
     * ⛔ 以后要补说明就写这里；开火页只准出现"当前这首曲的参数与状态"。
     */
    private void renderHelp() {
        content.addView(backLink("‹ 返回", () -> {
            step = 1;                       // 一律回选歌页（见字段区 2026-11-01 注释）
            render();
        }));
        content.addView(section("教程 / 帮助"));

        content.addView(section("① 打一局只要三步"));
        content.addView(tip("① 先在 Phigros 里把这个难度选中、停在 ▶ 就绪界面；\n"
                + "② 回到本 App 点「开始演奏」，允许一次录屏授权；\n"
                + "③ 授权一通过就立刻切回 Phigros（倒计时只有 8 秒），到点会自动点 ▶ 并按时注入。", 0xFFA8B6CC));

        content.addView(section("② 新设备第一次用：七步"));
        content.addView(tip("1 装两个 App：本 App + Shizuku。系统版本较新时请用 Shizuku 13.6.0 或更新。\n"
                + "2 起 Shizuku：手机内「无线调试 → 配对 → 启动」；有电脑时也可用 adb 启动"
                + "（tools/start_shizuku.sh）。\n"
                + "3 授权：点本页的「申请 Shizuku 权限」→ 在 Shizuku 的弹框里点允许。\n"
                + "　判据：那一行变成「Shizuku 权限：已授予 ✓」（Shizuku 首页「已授权应用」应 ≥1）。\n"
                + "4 关掉三件会让标定作废的事（见下面 ③）。\n"
                + "5 定 c（每台设备只做一次）。建议用下面两首的 IN 难度：\n"
                + "　· 首选「Cthugha」→ 难度 IN：156 秒，1333 音符里 1066 个要按时判定，并发 4；\n"
                + "　· 备选「Bounded Quietude」→ 难度 IN：1286 音符里 839 个要按时判定，并发 4。\n"
                + "　在 App 搜索框输 Cthugha / Bounded 就能找到；这两首 IN 都没有盲区。\n"
                + "　⚠ App 曲库是游戏资产的全量谱面，不代表你账号已解锁；游戏里没有的曲子打不了。\n"
                + "　做法（扫描取中点）：\n"
                + "　① 第一轮 c 不动，正常打 —— 这轮会自动测出这首的 K，之后就不用管它了；\n"
                + "　② 之后每轮只改 c，一次 +20，看结算是「全 Perfect / 一堆 Late / 一堆 Early」，late则加c，early则减c；\n"
                + "　③ 找到两端：「最后一次还出 Late」与「第一次开始出 Early」，\n"
                + "　　（这两端加起来）÷ 2 就是真实 c，精度约 ±10 ms；\n"
                + "　④ 填回中点，再打一轮确认全 Perfect。\n"
                + "　改法：首页顶部 c=… 小字点开填，下一轮生效。\n"
                + "6 正式开打（同 ①）。新曲第 1 轮用的是 c，同时自动标定 K；第 2 轮起才用 K + c。\n"
                + "7 打完看两处：结算页；曲目卡片上的 K。\n"
                + "　⚠ 若有盲区曲是在定 c 之前打的：卡片上点 lead →「清除」，再打一轮（那轮不预派）。", 0xFFA8B6CC));

        content.addView(section("③ 三件会让标定作废的事"));
        content.addView(tip("① 改过游戏内的「判定设置/延迟」⇒ 必须重标；\n"
                + "② 开了游戏空间/加速器 ⇒ 标定作废；\n"
                + "③ 别的 App 正在出声 ⇒ 内录会把它一起录进去，标定前请关掉。\n"
                + "反过来说：房间里的说话声、外放/蓝牙/有线、系统音量都不影响 —— "
                + "我们录的是系统播放流，不是麦克风。", 0xFFD98A5A));

        content.addView(section("④ 打不起来，先看日志最后一行"));
        content.addView(tip("· 「没听到曲首的琴声」⇒ 倒计时 8 秒内没切回游戏，那一下 ▶ 点空了（最常见）；\n"
                + "· 「录屏被系统中断了」⇒ 录屏授权没生效，或被别的录屏/投屏抢走；\n"
                + "· 「上一轮还没收尾」⇒ 上一轮没结束（自动标定最多再等 20 秒），等一会再点；\n"
                + "· 「Shizuku 门禁未过」⇒ 还没授权，回第 3 步；\n"
                + "· 「这首的谱面数据是空的」⇒ 素材没打包或版本不匹配，回电脑重新打包。", 0xFFA8B6CC));

        content.addView(section("⑤ 点 ▶ 没反应"));
        content.addView(tip("怎么判断：点了「开始演奏」后屏幕上一次触摸都没有、等满 20 秒就失败 ⇒ 多半是那一下 ▶ 点空了。\n"
                + "（App 不检查那一下点到哪儿，所以日志照样写「已点 ▶」，游戏却没开始。）\n\n"
                + "顶部那行小字有两个入口：\n"
                + "· 「▶ 落点 x,y」= 点开可以「学习落点」，或「恢复默认（0,0）」。\n"
                + "· 「测试 ▶」= 4 秒后自动点一次 ▶，只试落点、不碰谱面，之后不会进行注入。\n"
                + "　⚠ 那一下是从 App 发出去的，所以必须在这 4 秒内切回游戏、停在选曲页；\n"
                + "　　没切回去的话，就点在 App 自己身上了（不会误触游戏别的按钮）。\n\n"
                + "让 App 自己记落点：\n"
                + "1. 点「▶ 落点」→ 点「学习落点」；\n"
                + "2. 有 4 秒准备 + 15 秒窗口，切回游戏停在选曲页；\n"
                + "3. 在窗口内点一下右下角「开始演奏」；\n"
                + "　⚠ 只记第一次点击：先点了难度 / 曲目 / 返回，那个位置就会被当成按钮，\n"
                + "　　之后打歌就会点空 —— 这时点「恢复默认（0,0）」重学一次即可。\n"
                + "4. 记完用「测试 ▶」验证：游戏开始加载 = 位置对。\n\n"
                + "想亲眼看到落点落在哪：\n"
                + "系统设置 →「开发者选项」→ 打开「显示触摸操作」，再点一次「测试 ▶」，\n"
                + "屏幕上会留下白点/圆圈 —— 那就是我们的落点。\n\n"
                + "· 不支持手填偏移：坐标只能由「学习落点」写入，没学过就是出厂比例（0,0）。\n"
                + "· 换手机或改分辨率要重学一次（学到的点带机型与分辨率，变了就自动失效）。\n"
                + "· 别和 c / K 混：位置不对找这里；成绩不对（一堆 Good）找 c / K。", 0xFFD98A5A));

        content.addView(footnote("三句口诀：K 是曲子的、c 是这台手机的、lead 只服务盲区曲。"
                + "（盲区曲现在能 φ —— 两段锚会预派，不是结构缺陷。）"));
    }

    // ─────────────────────────── Shizuku 授权入口 ───────────────────────────

    /**
     * 「申请 Shizuku 权限」。**这是真注入的唯一前置条件**，但历史上界面里没有它
     * （injector 有，play 一直没有）⇒ 2026-10-24 两枪都在 prepare 阶段被
     * `ShizukuProbe` 的门禁挡下（日志 `checkSelfPermission ⇒ perm=-1`），
     * 而用户看到的只有一句「阶段：失败」。
     *
     * ★ 三种状态必须分开说（这是本项目教训 11「素材缺失 ≠ 读数合格」的同类）：
     *   binder 没连上 / 连上但没授权 / 已授权 —— 三种修法完全不同。
     * ⛔ 不要试图用 `adb shell pm grant <pkg> moe.shizuku.manager.permission.API_V23`
     *   替代它：那条命令会成功、`dumpsys package` 也显示 `granted=true`，但
     *   `Shizuku.checkSelfPermission()` 仍返回 -1（force-stop 重启进程后仍 -1）——
     *   Shizuku 的授权表是它自己维护的，pm grant 只是假绿。
     */
    private View shizukuPermRow() {
        final boolean bound = shizukuBound();
        final boolean granted = bound && shizukuGranted();
        String text;
        if (!bound) {
            // ★ 2026-10-26：措辞带上"被杀"这一档。真机实例：手机**没重启**（uptime 5.5 天）
            //   但 overnight 被系统清理把 shizuku_server 杀了 ⇒ 只写"重启后要拉起"会让人以为没重启就不该断。
            text = "Shizuku 未连接（点这里打开 Shizuku 启动）—— 重启或被杀后都要重新拉起";
        } else if (!granted) {
            text = "申请 Shizuku 权限（未授予 ⇒ 真注入会被门禁拒绝开火）";
        } else {
            text = "Shizuku 权限：已授予 ✓";
        }
        return subButton(text, () -> {
            if (!shizukuBound()) {
                // ★ 一键打开 Shizuku（省得每次翻设置）：它的启动页就是 MainActivity。
                //   本 app 无法替 Shizuku 起服务（那需要 shell/root），但能把人送到该按的按钮面前。
                try {
                    android.content.Intent i = getPackageManager()
                            .getLaunchIntentForPackage("moe.shizuku.privileged.api");
                    if (i == null) {
                        Toast.makeText(this, "没装 Shizuku ⇒ 先装 moe.shizuku.privileged.api",
                                Toast.LENGTH_LONG).show();
                    } else {
                        startActivity(i.addFlags(android.content.Intent.FLAG_ACTIVITY_NEW_TASK));
                        Toast.makeText(this, "在 Shizuku 里点「配对 / 启动」；没有电脑时用「通过无线调试启动」",
                                Toast.LENGTH_LONG).show();
                    }
                } catch (Throwable t) {
                    Toast.makeText(this, "打不开 Shizuku：" + brief(t), Toast.LENGTH_LONG).show();
                }
                return;
            }
            if (shizukuGranted()) {
                Toast.makeText(this, "已经授权了，直接点「开始演奏」", Toast.LENGTH_SHORT).show();
                return;
            }
            try {
                Shizuku.requestPermission(REQ_SHIZUKU);
                Toast.makeText(this, "已弹出授权请求，请在手机上点「允许」", Toast.LENGTH_LONG).show();
            } catch (Throwable t) {
                Toast.makeText(this, "申请失败：" + brief(t), Toast.LENGTH_LONG).show();
            }
        });
    }

    /** binder 是否就绪。Shizuku 的静态调用在未就绪时会抛，界面不能被它带崩。 */
    private static boolean shizukuBound() {
        try {
            return Shizuku.pingBinder();
        } catch (Throwable t) {
            return false;
        }
    }

    /** 只回答「授予了吗」。未就绪 / 抛异常一律当**未授予**（不许降级成通过）。 */
    private static boolean shizukuGranted() {
        try {
            return Shizuku.checkSelfPermission() == PackageManager.PERMISSION_GRANTED;
        } catch (Throwable t) {
            return false;
        }
    }

    // ─────────────────────────── 开火 / 停止 ───────────────────────────

    /**
     * 启动一轮。**载荷解析、门禁、排程全部在服务里复核**，这里只传"打哪首/哪个难度"。
     *
     * ★ 用 `startForegroundService`（API 26+）：普通 `startService` 从后台调会被拒；
     *   服务必须在 5 s 内 `startForeground`，`PlayService.onStartCommand` 第一件事就是它。
     */
    private void startRun(SongLib.Song s) {
        // ★ 2026-10-26 22:1x（真机日志）：真注入没有 Shizuku 授权时，服务照样启动、占 WakeLock、
        //   解析完 3989 条指令，**90 ms 后**才在 prepare 的门禁里失败（`perm=-1`）⇒ 用户看到的是
        //   「点了开始演奏但没打起来」，很容易被当成「要按两次」的怪 bug。
        //   ⇒ 真注入前先查授权：没 binder 提示去拉起，没授权当场弹授权框，**都不启动服务**。
        if (!ensureShizukuPermission()) return;
        if (PlayService.ZERO_TAP.equals(zeroMode)) {
            // S1-c 的回退路径：零点 = 点击 + 固定 lead，不需要任何采集授权。
            launchPlay(s, 0, null);
            return;
        }
        // onset 零点要 AudioPlaybackCapture ⇒ RECORD_AUDIO（运行时权限）+ MediaProjection 授权。
        pendingSong = s;
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO)
                != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.RECORD_AUDIO}, REQ_AUDIO);
            return;
        }
        askProjection();
    }

    /**
     * 真注入前的 Shizuku 授权闸门。
     *
     * ★ 为什么放在界面而不是只在服务里：服务的门禁是对的（没有它就会发一堆无效事件），但它
     *   在「准备」之后才拦 ⇒ 白白占 WakeLock、解析载荷、还让人误以为已经开始。界面先拦，
     *   用户当场看到要做什么，服务根本不用起。
     *
     * @return true = 已授权，可以继续；false = 已经给了用户下一步提示，本次不启动
     */
    private boolean ensureShizukuPermission() {
        if (!shizukuBound()) {
            Toast.makeText(this, "Shizuku 没在运行 ⇒ 先点下面那行「Shizuku 未连接」把它拉起",
                    Toast.LENGTH_LONG).show();
            return false;
        }
        if (!shizukuGranted()) {
            try {
                Shizuku.requestPermission(REQ_SHIZUKU);
                Toast.makeText(this,
                        "还没给本 App 授权：在 Shizuku 的弹框里点「允许」，然后再点一次「开始演奏」",
                        Toast.LENGTH_LONG).show();
            } catch (Throwable t) {
                Toast.makeText(this, "申请 Shizuku 权限失败：" + brief(t), Toast.LENGTH_LONG).show();
            }
            return false;
        }
        return true;
    }

    /**
     * 弹系统「录制或投射」授权框。**每个 token 只能用一次**，所以每轮都要走一遍 ——
     * 这是 Android 的设计，没有绕过的合法途径（Shizuku 也不能代点这个系统框）。
     */
    private void askProjection() {
        MediaProjectionManager mpm =
                (MediaProjectionManager) getSystemService(MEDIA_PROJECTION_SERVICE);
        if (mpm == null) {
            pendingSong = null;
            Toast.makeText(this, "拿不到 MediaProjectionManager", Toast.LENGTH_LONG).show();
            return;
        }
        Toast.makeText(this, "接下来会弹一次录屏授权（只用于端内听曲首起音）",
                Toast.LENGTH_LONG).show();
        startActivityForResult(mpm.createScreenCaptureIntent(), REQ_PROJ);
    }

    @Override
    public void onRequestPermissionsResult(int req, String[] perms, int[] res) {
        super.onRequestPermissionsResult(req, perms, res);
        if (req != REQ_AUDIO) return;
        if (res.length > 0 && res[0] == PackageManager.PERMISSION_GRANTED) {
            askProjection();
        } else {
            pendingSong = null;
            Toast.makeText(this, "没有录音权限 ⇒ 建不了采集流，这一轮不打"
                    + "（可在系统设置里授予，或把零点切回 tap）", Toast.LENGTH_LONG).show();
        }
    }

    @Override
    protected void onActivityResult(int req, int resultCode, Intent data) {
        super.onActivityResult(req, resultCode, data);
        if (req != REQ_PROJ) return;
        SongLib.Song s = pendingSong;
        pendingSong = null;
        if (s == null) return;                       // 不是我们发起的（或已被取消）
        if (resultCode != RESULT_OK || data == null) {
            Toast.makeText(this, "录屏授权被拒 ⇒ onset 零点拿不到，这一轮不打",
                    Toast.LENGTH_LONG).show();
            return;
        }
        launchPlay(s, resultCode, data);
    }

    /**
     * 真正启动服务。**载荷解析、门禁、排程全部在服务里复核**，这里只传"打哪首/怎么定零点"。
     *
     * ★ 用 `startForegroundService`（API 26+）：普通 `startService` 从后台调会被拒；
     *   服务必须在 5 s 内 `startForeground`，`PlayService.onStartCommand` 第一件事就是它。
     * ★ 授权 token 跟着 Intent 走：Service 侧 `getMediaProjection(resultCode, data)` 要用它。
     */
    private void launchPlay(SongLib.Song s, int resultCode, Intent resultData) {
        Intent it = new Intent(this, PlayService.class)
                .setAction(PlayService.ACTION_START)
                .putExtra(PlayService.EXTRA_SONG, s.key)
                .putExtra(PlayService.EXTRA_ZERO, zeroMode)
                .putExtra(PlayService.EXTRA_AUTO_L, autoLDev)
                .putExtra(PlayService.EXTRA_COURSE, courseKeys)
                .putExtra(PlayService.EXTRA_L_DEV_S, lDevS);
        if (resultData != null) {
            it.putExtra(PlayService.EXTRA_RESULT_CODE, resultCode)
                    .putExtra(PlayService.EXTRA_RESULT_DATA, resultData);
        }
        startForegroundService(it);
        // ★ 2026-11-01：原来这里尾巴上拼了一段运行模式口径，随副标题一起删了。
        //   选的是哪首/哪个难度已经写在前面，模式口径对用户没意义（理由见 subtitleText()）。
        Toast.makeText(this, "已启动：" + s.diff + (s.level > 0 ? " " + s.level : ""),
                Toast.LENGTH_SHORT).show();
    }


    // ★ 2026-11-01：`dumpCalibPrefs()`（把全部 k_ms_/c_ms/lead 键打到日志）与
    //   `probeServiceView()`（让"服务侧"再读一遍同样的 prefs）已按用户要求删除。
    //   删的两个理由都写在上方按钮处：前者输出几十行、还先 clear 日志，且值在曲目卡上已有；
    //   后者与前者读的是**同一个 SharedPreferences 实例**（同进程），没有对照价值。
    //   ⛔ 别再以"多点观测手段"为由加回来：端内真要查原始键值，`adb logcat -s PgPlay` 还在。

    private void runSelfTest() {
        // ★ 这里**不能** render()：整页重建会让外层 ScrollView 的滚动位置重算，
        //   真机表现是「点一下自检，页面动一下」（2026-11-01 用户反馈）。
        //   自检结果只影响这一个 TextView，直接改文本就够。
        selfTestText = "自检中…";
        if (tvSelfTest != null) tvSelfTest.setText(selfTestText);
        new Thread(() -> {
            final List<Components.Item> items = Components.runAll(this);
            StringBuilder sb = new StringBuilder();
            sb.append("三树自检 · ").append(Components.headline(items)).append('\n');
            for (Components.Item it : items) sb.append(it).append('\n');
            sb.append('\n').append(Components.oneLine(items));
            final String text = sb.toString().trim();
            h.post(() -> {
                selfTestText = text;
                // 视图可能已被别的页面替换（tvSelfTest 为 null）⇒ 只留文本，下次 render 自然显示
                if (tvSelfTest != null) tvSelfTest.setText(text);
            });
        }, "selftest").start();
    }

    // ─────────────────────────── 日志刷新 ───────────────────────────

    private void refreshLog() {
        refreshLogNow();
        h.postDelayed(this::refreshLog, LOG_POLL_MS);
    }

    /**
     * 从进程内 {@link RunLog} 拉最新状态（同进程，直接读静态对象）。
     *
     * ★ 只在**内容真的变了**时才 setText。无条件每 400 ms setText 会让界面一直重绘、
     *   永久进不了 idle 状态 —— 实测后果：`uiautomator dump` 直接报
     *   `ERROR: could not get idle state.`，自动化与无障碍工具全部失效，而且白耗电。
     *   这是"看起来只是刷个日志"的功能带出来的副作用，属于必须自己踩一遍才知道的坑。
     */
    private void refreshLogNow() {
        if (tvPhase != null) {
            String t = "阶段：" + RunLog.phase() + (RunLog.running() ? "（运行中）" : "");
            if (!t.equals(lastPhaseText)) {
                lastPhaseText = t;
                tvPhase.setText(t);
                tvPhase.setTextColor(RunLog.running() ? ACCENT : TEXT);
            }
        }
        if (tvLog != null && logScroll != null && logExpanded) {
            // ★ 收起态整块 GONE ⇒ 不排版：每 400 ms 给 40 行做一次 setText 只是白耗电
            //   （「共 N 行」由下面的标题行刷新，所以收起时行数照样是活的）。
            // ★ 只在「刚展开」时贴一次底，**不再按"是否贴底"跟随刷新**。
            //   原因（11-01 真机）：内容不足一屏时 `scrollY + 视口高` 恒 ≥ 内容高 ⇒ atBottom 恒真，
            //   于是任何一条新日志（典型是点「三树自检」）都会把视图从上往下拉到底。
            boolean stick = logStickBottom;
            List<String> lines = RunLog.all();
            StringBuilder sb = new StringBuilder();
            if (lines.isEmpty()) {
                sb.append(step == STEP_COURSE
                        ? "（还没有日志：点「开始课题演奏」开一场）"
                        : "（还没有日志：点「开始演奏」跑一轮）");
            } else {
                for (String l : lines) sb.append(l).append('\n');
            }
            String t = sb.toString().trim();
            if (!t.equals(lastLogText)) {
                lastLogText = t;
                tvLog.setText(t);
            }
            if (stick) {
                // setText 之后内容高度还没测出来 ⇒ 必须放到下一帧再滚，否则滚的是旧高度
                final LogScroll box = logScroll;
                box.post(() -> box.fullScroll(View.FOCUS_DOWN));
            }
            logStickBottom = false;
        }
        if (tvLogToggle != null) {
            // 带总行数：收着的时候也知道后面还有多少行（否则会以为日志只有 2 行）
            String t = "日志  " + (logExpanded
                    ? "▾ 收起"
                    : "▸ 展开（共 " + RunLog.size() + " 行）");
            if (!t.equals(lastLogToggleText)) {
                lastLogToggleText = t;
                tvLogToggle.setText(t);
            }
        }
    }

    /** 切换日志收起/展开。 */
    private void toggleLog() {
        // ★ 记住页面位置：日志块出现/消失会改变内容高度，外层 ScrollView 可能重新定位
        //   ⇒ 展开时"整页跳走"。这里在布局完成后把 scrollY 原样写回去
        //   （2026-11-01 用户要求：展开只是多出一块区域，不移动页面）。
        final ScrollView page = scroll;
        final int keepY = page == null ? -1 : page.getScrollY();

        logExpanded = !logExpanded;
        // ⛔ 必须清缓存：`refreshLogNow` 只在"内容变了"时才 setText，
        //   而收起态根本不排版 —— 不清就会出现"点了展开但新视图一直是空的"。
        lastLogText = "";
        // 刚展开 ⇒ 直接给到最新一行（否则 40 行里看到的是开头）；收起时置 false 无副作用
        logStickBottom = logExpanded;
        if (logScroll != null) {
            logScroll.setVisibility(logExpanded ? View.VISIBLE : View.GONE);
        }
        refreshLogNow();
        if (page != null && keepY >= 0) {
            // 放到下一帧：本帧里 GONE↔VISIBLE 引起的高度变化还没走完
            page.post(() -> page.scrollTo(0, keepY));
        }
    }

    /**
     * 日志面板：可点的标题行 + 固定高度、内部可滑动的深色块。开火页与课题页共用这一份
     * （2026-11-01 之前课题页是手写副本，漏了底色 / 内边距 / 可选中）。
     *
     * ★ 收起时整块 {@code GONE}：不占高度 —— 一屏才放得下「开始演奏」。
     * ★ 背景 / 圆角 / 内边距画在**容器**上：画在内层 TextView 上时，内容不足一屏会露出下半截空白。
     * ⚠ 高度不能用 {@code wrapTop()} 设：它内部写死 WRAP_CONTENT，会把固定高度冲掉。
     */
    private View logPanel(int topDp) {
        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL);
        box.addView(logHeader());

        logScroll = new LogScroll(this);
        logScroll.setBackground(round(0xFF0B0E13, 10));
        logScroll.setPadding(dp(12), dp(10), dp(12), dp(10));
        logScroll.setVisibility(logExpanded ? View.VISIBLE : View.GONE);

        tvLog = tv("", 11, 0xFFB8C2D0, false);
        tvLog.setTypeface(Typeface.MONOSPACE);
        // ⛔ 不许再开 setTextIsSelectable(true)：可选中文本会吃掉 MOVE（日志块整块滑不动），
        //   而且它拿到焦点后外层 ScrollView 会把整页滚到它上面（表现为"点展开页面乱跳"）。
        //   2026-11-01 真机反馈：用户不要长按复制，只要滚动。
        tvLog.setTextIsSelectable(false);
        tvLog.setFocusable(false);
        tvLog.setClickable(false);
        tvLog.setLongClickable(false);
        logScroll.addView(tvLog, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));

        LinearLayout.LayoutParams slp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, dp(LOG_BOX_DP));
        slp.topMargin = dp(6);
        box.addView(logScroll, slp);

        LinearLayout.LayoutParams blp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        blp.topMargin = dp(topDp);
        box.setLayoutParams(blp);
        return box;
    }

    /** 日志块上方那行可点的小字（收起态也会显示，否则没人知道日志能点开）。 */
    private View logHeader() {
        tvLogToggle = tv("", 11, 0xFF8FA2BE, false);
        tvLogToggle.setPadding(dp(4), dp(2), dp(4), dp(6));
        tvLogToggle.setOnClickListener(v -> toggleLog());
        return tvLogToggle;
    }

    // ─────────────────────────── UI 骨架 ───────────────────────────

    private View buildRoot() {
        // ★ 外层换 FrameLayout：左侧导航栏与遮罩要浮在内容之上（2026-10-25 用户要求）。
        //   里面那层 LinearLayout 结构不变 —— 少动一层，ScrollView 的滚动恢复逻辑就不用重验。
        FrameLayout frame = new FrameLayout(this);
        frame.setBackgroundColor(BG);

        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setBackgroundColor(BG);
        frame.addView(root, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
        if (Build.VERSION.SDK_INT >= 21) {
            getWindow().setStatusBarColor(BG);
        }

        // ── 顶部：标题 + 三步指示 ──
        LinearLayout head = new LinearLayout(this);
        head.setOrientation(LinearLayout.VERTICAL);
        head.setPadding(dp(20), dp(22), dp(20), dp(16));
        GradientDrawable hb = new GradientDrawable(GradientDrawable.Orientation.TOP_BOTTOM,
                new int[]{HEAD_TOP, BG});
        head.setBackground(hb);

        // ★ 标题行：☰ + 标题。☰ 是左导航的唯一入口（平时抽屉不显示，避免常驻占宽）。
        LinearLayout titleRow = new LinearLayout(this);
        titleRow.setOrientation(LinearLayout.HORIZONTAL);
        titleRow.setGravity(Gravity.CENTER_VERTICAL);
        TextView menuBtn = tv("☰", 22, TEXT, true);
        menuBtn.setPadding(dp(2), dp(2), dp(14), dp(2));
        menuBtn.setOnClickListener(v -> toggleDrawer(true));
        titleRow.addView(menuBtn);
        titleRow.addView(tv("PG 自动演奏", 23, TEXT, true));
        head.addView(titleRow);
        // ★ 首页必须把口径显示出来：端内轮的「开火」在另一个界面点，一旦开关丢失，
        //   界面与打歌结果都会静默退回 async，一模一样的成绩 —— 一整枪又白交学费（§167）。
        //   这里显示 = 打之前就能发现。⛔ 文本只能来自 subtitleText()，别在此处复写字面量。
        tvModeLabel = tv(subtitleText(), 12, 0xFF9FB0C9, false);
        tvModeLabel.setPadding(0, dp(4), 0, 0);
        head.addView(tvModeLabel);

        LinearLayout chipRow = new LinearLayout(this);
        chipRow.setOrientation(LinearLayout.HORIZONTAL);
        chipRow.setBaselineAligned(false);
        chipRow.setGravity(Gravity.CENTER_VERTICAL);
        for (int i = 0; i < chips.length; i++) {
            chips[i] = tv("", 11, DIM, true);
            chips[i].setGravity(Gravity.CENTER);
            chips[i].setPadding(dp(11), dp(5), dp(11), dp(5));
            chips[i].setBackground(round(0xFF1B202A, 20));
            LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                    ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT);
            if (i > 0) lp.leftMargin = dp(8);
            chipRow.addView(chips[i], lp);
        }
        // 右侧空白处：设备常数 c。放在这里而不是开火页 —— 它是"一台设备一次"的量，
        // 天天摆在打歌按钮旁边只会被误触（用户明确要求）。
        // ⚠ 高度必须给死 1px：WRAP_CONTENT 的空 View 在横向 LinearLayout 里会让整行
        //   按"撑满剩余高度"测量（实测把 ScrollView 挤成 0 高、整页空白）。
        View spacer = new View(this);
        chipRow.addView(spacer, new LinearLayout.LayoutParams(0, dp(1), 1f));
        tvC = tv("", 11, 0xFF6C7A8C, false);
        tvC.setPadding(dp(8), dp(5), dp(8), dp(5));
        tvC.setOnClickListener(v -> showCDialog());
        chipRow.addView(tvC);
        // lead 与 c 并列：都是"参数"而不是"操作"，放这里才不会挡住「开始打歌」。
        tvLead = tv("", 11, 0xFF6C7A8C, false);
        tvLead.setPadding(dp(6), dp(5), dp(8), dp(5));
        tvLead.setOnClickListener(v -> {
            SongLib.Song cur = selSong != null ? selSong
                    : (selGroup != null && !selGroup.variants().isEmpty()
                            ? selGroup.variants().get(0) : null);
            if (cur == null) toast("先选一首歌，再改它的 lead");
            else showLeadDialog(cur);
        });
        chipRow.addView(tvLead);
        LinearLayout.LayoutParams clp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        clp.topMargin = dp(14);
        head.addView(chipRow, clp);

        // K 单独一行、右对齐，正好落在 `c=…` 的**下方**（用户 2026-10-30 指定的位置）。
        // ★ 为什么不并进 chipRow：那一行已经放满「1选曲 / 2难度 / 3开火 + c + lead」，
        //   再挤一个在窄屏上会把步骤 chips 压成省略号；而且 K 与 c/lead 不同类（K 是**曲目**属性）。
        LinearLayout chipRowK = new LinearLayout(this);
        chipRowK.setOrientation(LinearLayout.HORIZONTAL);
        chipRowK.setBaselineAligned(false);
        chipRowK.setGravity(Gravity.CENTER_VERTICAL);
        View spacerK = new View(this);
        chipRowK.addView(spacerK, new LinearLayout.LayoutParams(0, dp(1), 1f));
        tvK = tv("", 11, 0xFF6C7A8C, false);
        tvK.setPadding(dp(8), dp(2), dp(8), dp(5));
        tvK.setOnClickListener(v -> {
            SongLib.Song cur = selSong != null ? selSong
                    : (selGroup != null && !selGroup.variants().isEmpty()
                            ? selGroup.variants().get(0) : null);
            if (cur == null) toast("先选一首歌，再改它的 K");
            else showKDialog(cur);
        });
        chipRowK.addView(tvK);
        LinearLayout.LayoutParams klp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        klp.topMargin = dp(2);
        head.addView(chipRowK, klp);

        // ▶ 落点单独一行（2026-10-30 用户要求「点不到要能自己调」）：
        // 左边可点文本显示当前偏移，右边「测试 ▶」直接发一次点击。
        // ⚠ 只在这里改**偏移**；出厂比例 START_REL_X/Y 仍由 PlayService 的常量唯一决定。
        LinearLayout chipRowStart = new LinearLayout(this);
        chipRowStart.setOrientation(LinearLayout.HORIZONTAL);
        chipRowStart.setBaselineAligned(false);
        chipRowStart.setGravity(Gravity.CENTER_VERTICAL);
        View spacerS = new View(this);
        chipRowStart.addView(spacerS, new LinearLayout.LayoutParams(0, dp(1), 1f));
        tvStart = tv("", 11, 0xFF6C7A8C, false);
        tvStart.setPadding(dp(8), dp(2), dp(6), dp(5));
        tvStart.setOnClickListener(v -> showStartDialog());
        chipRowStart.addView(tvStart);
        TextView btnTestTap = tv("测试 ▶", 11, 0xFF6C7A8C, false);
        btnTestTap.setPadding(dp(6), dp(2), dp(8), dp(5));
        btnTestTap.setOnClickListener(v -> {
            startService(new Intent(this, PlayService.class)
                    .setAction(PlayService.ACTION_TEST_TAP));
            toast("已发一次 ▶：请站在选曲页看游戏是否开始加载（没反应就去调偏移）");
        });
        chipRowStart.addView(btnTestTap);
        LinearLayout.LayoutParams slp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        slp.topMargin = dp(2);
        head.addView(chipRowStart, slp);

        root.addView(head);

        // ── 中间：可滚动的步骤内容 ──
        ScrollView sv = new ScrollView(this);
        scroll = sv;
        sv.setFillViewport(true);
        content = new LinearLayout(this);
        content.setOrientation(LinearLayout.VERTICAL);
        content.setPadding(dp(16), dp(6), dp(16), dp(28));
        sv.addView(content, new ViewGroup.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));
        root.addView(sv, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f));

        // ── 抽屉遮罩：点它关抽屉（手机上没有 ESC，得有"戳外面关"这条退路）──
        drawerScrim = new View(this);
        drawerScrim.setBackgroundColor(0x99000000);
        drawerScrim.setVisibility(View.GONE);
        drawerScrim.setOnClickListener(v -> toggleDrawer(false));
        frame.addView(drawerScrim, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));

        // ── 左侧导航栏：默认 GONE，只有点 ☰ 才出现 ──
        drawer = buildDrawer();
        drawer.setVisibility(View.GONE);
        FrameLayout.LayoutParams dlp = new FrameLayout.LayoutParams(
                Math.min(dp(300), (int) (getResources().getDisplayMetrics().widthPixels * 0.78)),
                ViewGroup.LayoutParams.MATCH_PARENT);
        dlp.gravity = Gravity.START;
        frame.addView(drawer, dlp);

        // ── 「回到最上方」悬浮按钮（2026-10-26 用户要求）──
        //    曲库 334 首，滑到一半想回顶部只能一路拖。只在选曲/难度页显示，其它步骤隐藏。
        topBtn = tv("▲", 16, 0xFFBFD3EA, true);
        topBtn.setGravity(Gravity.CENTER);
        GradientDrawable tbg = new GradientDrawable();
        tbg.setColor(0xCC1B2330);
        tbg.setCornerRadius(dp(28));
        tbg.setStroke(dp(1), STROKE);
        topBtn.setBackground(tbg);
        topBtn.setOnClickListener(v -> {
            if (scroll != null) scroll.smoothScrollTo(0, 0);
        });
        FrameLayout.LayoutParams tlp = new FrameLayout.LayoutParams(dp(52), dp(52));
        tlp.gravity = Gravity.END | Gravity.BOTTOM;
        tlp.setMargins(0, 0, dp(16), dp(26));
        topBtn.setVisibility(View.GONE);
        frame.addView(topBtn, tlp);

        return frame;
    }

    /** 左侧导航栏的内容。以后要加诊断入口就往这里塞，⛔ 别再往开火页堆。 */
    private LinearLayout buildDrawer() {
        LinearLayout d = new LinearLayout(this);
        d.setOrientation(LinearLayout.VERTICAL);
        d.setPadding(dp(14), dp(30), dp(14), dp(16));
        GradientDrawable bg = new GradientDrawable();
        bg.setColor(0xFF11161E);
        bg.setStroke(dp(1), STROKE);
        d.setBackground(bg);

        TextView title = tv("菜单", 13, 0xFF8FA2BE, true);
        title.setPadding(dp(6), 0, 0, dp(10));
        d.addView(title);

        d.addView(drawerItem("课题模式", () -> openCourse()));
        d.addView(drawerItem("教程 / 帮助", () -> openHelp()));
        // ★ 2026-11-01 按用户要求删掉「曲库（返回选曲）」：两个独立页的返回键已经一律回选歌页，
        //   这个入口就是重复的（而且它绕过返回链，是"回不去"错觉的一部分）。

        // 底部：构建自证（G13）—— 抽屉是"关于这个包"最自然的位置
        View grow = new View(this);
        d.addView(grow, new LinearLayout.LayoutParams(1, 0, 1f));
        TextView bi = tv("构建 " + buildInfo().brief(), 11, 0xFF6C7A8C, false);
        bi.setPadding(dp(6), 0, 0, 0);
        bi.setTextIsSelectable(true);
        d.addView(bi);
        return d;
    }

    private LinearLayout.LayoutParams drawerLp() {
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        lp.bottomMargin = dp(8);
        return lp;
    }

    private View drawerItem(String text, Runnable r) {
        TextView t = tv(text, 15, TEXT, false);
        t.setPadding(dp(12), dp(14), dp(12), dp(14));
        t.setBackground(pressable(0xFF161C26, 0xFF1E2634, 10, STROKE));
        // ★ 先收抽屉再跳转（2026-10-27 用户反馈：点完「课题模式」抽屉还挡在上面）
        t.setOnClickListener(v -> {
            toggleDrawer(false);
            r.run();
        });
        LinearLayout.LayoutParams lp = drawerLp();
        t.setLayoutParams(lp);
        return t;
    }

    /** 打开/关闭左侧抽屉。只做显隐 + 120 ms 小动画；⛔ 不做手势滑出（会和 ScrollView 抢事件）。 */
    private void toggleDrawer(boolean open) {
        if (drawer == null || drawerScrim == null) return;
        drawerOpen = open;
        if (open) {
            drawerScrim.setAlpha(0f);
            drawerScrim.setVisibility(View.VISIBLE);
            drawerScrim.animate().alpha(1f).setDuration(120).start();
            drawer.setTranslationX(-dp(24));
            drawer.setAlpha(0f);
            drawer.setVisibility(View.VISIBLE);
            drawer.animate().translationX(0f).alpha(1f).setDuration(120).start();
        } else {
            drawerScrim.animate().cancel();
            drawer.animate().cancel();
            drawerScrim.setVisibility(View.GONE);
            drawer.setVisibility(View.GONE);
            drawerScrim.setAlpha(1f);
            drawer.setAlpha(1f);
            drawer.setTranslationX(0f);
        }
    }

    /** 打开课题模式独立页（返回一律回选歌页，见字段区 2026-11-01 注释）。 */
    private void openCourse() {
        step = STEP_COURSE;
        render();
    }

    /** 读 assets 里的文本（失败返回 null，绝不抛）。 */
    private String readAsset(String name) {
        try (java.io.InputStream in = getAssets().open(name)) {
            java.io.ByteArrayOutputStream bo = new java.io.ByteArrayOutputStream();
            byte[] buf = new byte[4096];
            int n;
            while ((n = in.read(buf)) > 0) bo.write(buf, 0, n);
            return new String(bo.toByteArray(), java.nio.charset.StandardCharsets.UTF_8);
        } catch (Throwable t) {
            return null;
        }
    }

    /** 按 key 找曲目；找不到返回 null。 */
    private SongLib.Song songByKey(String key) {
        for (SongLib.Group g : groups) {
            for (SongLib.Song s : g.variants()) {
                if (s.key.equals(key)) return s;
            }
        }
        return null;
    }

    /**
     * 课题模式独立页（2026-10-27）。
     *
     * ★ 为什么单独一页：用户明确要求"放到抽屉里、不要碰之前的界面"。
     *   选曲/难度/开火三页保持原样；课题模式从左侧抽屉进入。
     * ★ 预设来自 `assets/courses.json`（数据而非代码）：改搭配只改那个文件 + 重打包。
     */
    /**
     * 课题模式独立页（2026-10-27）。
     *
     * ★ 为什么单独一页：用户要求"放到抽屉里、不要碰之前的界面"。
     * ★ 页面结构：教程卡（告诉用户具体要选哪几首、按什么顺序）→ 预设卡（三首 + 状态 + 开始）→ 阶段/日志。
     * ★ 预设来自 `assets/courses.json`（数据），改搭配只改那个文件 + 重打包。
     */
    private void renderCourse() {
        content.addView(backLink("‹ 返回", () -> {
            step = 1;                       // 一律回选歌页（见字段区 2026-11-01 注释）
            render();
        }));
        content.addView(section("课题模式"));

        final String json = readAsset("courses.json");
        if (json == null) {
            content.addView(tip("这个包没带 assets/courses.json ⇒ 没有课题预设。", ERR));
            return;
        }

        try {
            JSONArray presets = new JSONObject(json).optJSONArray("presets");
            if (presets == null || presets.length() == 0) {
                content.addView(tip("courses.json 里没有任何预设。", ERR));
                return;
            }

            // ★ 教程要**点名**要选哪几首（用户明确要求）：从第一组预设生成「选歌顺序」。
            content.addView(tutCard("怎么用", new String[][]{
                    {"①", "在游戏里进入「课题模式」"},
                    {"②", "按这个顺序选好三首：" + courseOrderText(presets.getJSONObject(0))},
                    {"③", "停在课题模式的开始界面（能看到「开始」按钮）"},
                    {"④", "回到本页点「开始课题演奏」→ 允许一次屏幕录制"},
                    {"⑤", "切回游戏等着（App 会替你点开始，之后三首自动接续）"},
            }, "⚠ 会话模式不做标定：哪首显示「未标定」，请先单独打一轮再进课题。"));
            for (int i = 0; i < presets.length(); i++) {
                JSONObject p = presets.getJSONObject(i);
                final String pname = p.optString("name", p.optString("id", "预设"));
                JSONArray arr = p.optJSONArray("songs");
                if (arr == null || arr.length() == 0) continue;

                LinearLayout box = vcard();
                LinearLayout head = new LinearLayout(this);
                head.setOrientation(LinearLayout.HORIZONTAL);
                head.setBaselineAligned(false);
                head.setGravity(Gravity.CENTER_VERTICAL);
                head.addView(tv(pname, 16, TEXT, true));
                TextView cnt = tv(arr.length() + " 首", 11, 0xFF9FB4CC, false);
                cnt.setGravity(Gravity.END);
                LinearLayout.LayoutParams clp = new LinearLayout.LayoutParams(
                        LinearLayout.LayoutParams.WRAP_CONTENT,
                        LinearLayout.LayoutParams.WRAP_CONTENT);
                clp.weight = 1f;
                clp.setMarginStart(dp(8));
                head.addView(cnt, clp);
                box.addView(head);

                final List<SongLib.Song> songs = new ArrayList<>();
                boolean allFound = true;
                boolean allCalibrated = true;
                for (int j = 0; j < arr.length(); j++) {
                    String key = arr.getJSONObject(j).optString("key");
                    SongLib.Song s = songByKey(key);
                    if (s == null) {
                        // ★ 缺曲目时不能走 courseRow（它要读 s.name）——单独一行提示
                    box.addView(tv((j + 1) + ". ✗ 曲库里没有 " + key, 12, ERR, false));
                        allFound = false;
                        continue;
                    }
                    songs.add(s);
                    Float k = calibK(s);
                    Float lead = leadOf(s);
                    boolean ok = (k != null && lead != null);
                    if (!ok) allCalibrated = false;
                    box.addView(courseRow(j + 1, s, k, lead, ok));
                }

                box.addView(tv("lead / K 都按曲名存，和单曲模式共用：这里改，单曲界面同一首也会变。",
                        11, 0xFF8FA2BE, false));
                if (!allCalibrated) {
                    box.addView(tv("↑ 标黄的那首还没标定 ⇒ 先单独打一轮，否则这一场拿不到满分。",
                            11, 0xFFE0A96D, false));
                }
                if (allFound && !songs.isEmpty()) {
                    LinearLayout row = new LinearLayout(this);
                    row.setOrientation(LinearLayout.HORIZONTAL);
                    row.setBaselineAligned(false);
                    final SongLib.Song first = songs.get(0);
                    mainButton("开始课题演奏", () -> startCourse(pname, songs, first), row);
                    box.addView(wrapTop(row, 12));
                }
                content.addView(wrapTop(box, 10));
            }
        } catch (Throwable t) {
            content.addView(tip("courses.json 解析失败：" + brief(t), ERR));
        }

        tvPhase = tv("阶段：空闲", 15, TEXT, true);
        content.addView(wrapTop(tvPhase, 16));

        // ★ 与开火页共用同一个面板实现：以前这里是第二份手写副本，漏了底色/内边距/可选中。
        content.addView(logPanel(10));
    }

    /** 把一组预设渲染成「歌名 难度 → 歌名 难度 → …」，给教程第②步点名用。 */
    private String courseOrderText(JSONObject preset) {
        JSONArray arr = preset.optJSONArray("songs");
        if (arr == null) return "（预设为空）";
        StringBuilder b = new StringBuilder();
        for (int i = 0; i < arr.length(); i++) {
            JSONObject o = arr.optJSONObject(i);
            if (o == null) continue;
            String key = o.optString("key");
            String diff = o.optString("diff", "AT");
            SongLib.Song s = songByKey(key);
            if (i > 0) b.append(" → ");
            b.append(s == null ? key : s.name).append(" ").append(diff);
        }
        return b.toString();
    }

    /** 竖排卡片（`card()` 是横排的，专给按钮行用；这里要竖排容器）。 */
    private LinearLayout vcard() {
        LinearLayout c = new LinearLayout(this);
        c.setOrientation(LinearLayout.VERTICAL);
        c.setPadding(dp(14), dp(13), dp(14), dp(13));
        c.setBackground(pressable(CARD, CARD_PRESSED, 14, STROKE));
        return c;
    }

    /**
     * 课题页里的一行：编号 + 曲名/难度 + `K` 与 `leadN`。
     *
     * ★ lead 是**按曲**存的（`LEAD_KEY + 曲名`），**与单曲模式共用同一份数据** ——
     *   在这里改，回到单曲界面打同一首也会用这个值；反之单曲打一轮也会写回这里。
     * ★ 点 `lead` 那一小段可以改（弹窗校验范围 1000~120000 ms，与单曲模式同一套 PrefGuard）。
     */
    private View courseRow(int idx, SongLib.Song s, Float k, Float lead, boolean ok) {
        LinearLayout r = new LinearLayout(this);
        r.setOrientation(LinearLayout.HORIZONTAL);
        r.setBaselineAligned(false);
        r.setGravity(Gravity.CENTER_VERTICAL);
        r.setPadding(0, dp(5), 0, dp(5));

        TextView n = tv(String.valueOf(idx), 15, 0xFF7FA6D9, true);
        n.setMinWidth(dp(24));
        r.addView(n);

        LinearLayout col = new LinearLayout(this);
        col.setOrientation(LinearLayout.VERTICAL);
        col.addView(tv(s.name + "  " + s.diff, 14, 0xFFE8EDF5, true));

        LinearLayout meta = new LinearLayout(this);
        meta.setOrientation(LinearLayout.HORIZONTAL);
        meta.setBaselineAligned(false);
        meta.addView(tv(k == null ? "K 未标定" : String.format(Locale.ROOT, "K %+.1f", k),
                11, 0xFF9FB4CC, false));
        TextView leadTv = tv("· lead " + (lead == null ? "—"
                        : String.format(Locale.ROOT, "%.0f", lead)),
                11, ok ? 0xFF9FB4CC : 0xFFE0A96D, false);
        meta.addView(leadTv);
        TextView edit = tv("  改", 11, 0xFF7FA6D9, true);
        edit.setPadding(dp(2), 0, dp(2), 0);
        // 2026-10-30：从「只改 lead」改成「K 和 lead 都能改」（课程里最常缺的就是这两个）。
        edit.setOnClickListener(v -> showKLeadDialog(s));
        leadTv.setOnClickListener(v -> showKLeadDialog(s));
        meta.addView(edit);
        col.addView(meta);

        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(0,
                LinearLayout.LayoutParams.WRAP_CONTENT, 1f);
        lp.setMarginStart(dp(6));
        r.addView(col, lp);
        return r;
    }

    /** 教程卡：标题 + 编号步骤 + 可选的一句提醒。 */
    private View tutCard(String title, String[][] steps, String foot) {
        LinearLayout c = vcard();
        c.addView(tv(title, 15, TEXT, true));
        for (String[] s : steps) {
            LinearLayout r = new LinearLayout(this);
            r.setOrientation(LinearLayout.HORIZONTAL);
            r.setBaselineAligned(false);
            r.setPadding(0, dp(4), 0, dp(4));
            TextView n = tv(s[0], 14, 0xFF7FA6D9, true);
            n.setMinWidth(dp(26));
            r.addView(n);
            TextView tx = tv(s[1], 13, 0xFFC9D1DD, false);
            tx.setLineSpacing(dp(2), 1f);
            LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(0,
                    LinearLayout.LayoutParams.WRAP_CONTENT, 1f);
            r.addView(tx, lp);
            c.addView(r);
        }
        if (foot != null && !foot.isEmpty()) {
            TextView f = tv(foot, 11, 0xFFE0A96D, false);
            f.setLineSpacing(dp(2), 1f);
            c.addView(wrapTop(f, 8));
        }
        return c;
    }

    /** 把预设里的曲目按顺序交给服务（一次授权跑完整场会话）。 */
    private void startCourse(String presetName, List<SongLib.Song> songs, SongLib.Song first) {
        StringBuilder b = new StringBuilder();
        for (SongLib.Song s : songs) {
            if (b.length() > 0) b.append(',');
            b.append(s.key);
        }
        courseKeys = b.toString();
        RunLog.clear();
        RunLog.line("课题模式：" + presetName + " → " + courseKeys);
        startRun(first);
    }

    /** 打开教程/帮助独立页（抽屉由 `drawerItem` 收起；返回一律回选歌页）。 */
    private void openHelp() {
        step = STEP_HELP;
        render();
    }

    private TextView section(String s) {
        TextView t = tv(s, 13, 0xFF8FA2BE, true);
        t.setPadding(dp(4), dp(10), dp(4), dp(8));
        return t;
    }

    private View backLink(String s, Runnable r) {
        TextView t = tv(s, 13, ACCENT, false);
        t.setPadding(dp(4), dp(6), dp(4), dp(6));
        t.setOnClickListener(v -> r.run());
        return t;
    }

    private TextView tip(String s, int color) {
        TextView t = tv(s, 12, color, false);
        t.setLineSpacing(dp(3), 1f);
        t.setPadding(dp(12), dp(11), dp(12), dp(11));
        t.setBackground(roundStroke(0xFF12161D, 10, STROKE));
        return wrapTop(t, 12);
    }

    private TextView footnote(String s) {
        TextView t = tv(s, 11, 0xFF6F7B8C, false);
        t.setLineSpacing(dp(2), 1f);
        t.setPadding(dp(4), dp(14), dp(4), 0);
        return t;
    }

    /** 一张卡片：圆角 + 细边 + 内边距 + 按下反馈。 */
    private LinearLayout card() {
        LinearLayout c = new LinearLayout(this);
        c.setOrientation(LinearLayout.HORIZONTAL);
        // ★ 关掉 baseline 对齐：与文本混排时按第一条基线对齐会把第二行裁掉一半
        c.setBaselineAligned(false);
        c.setGravity(Gravity.CENTER_VERTICAL);
        c.setPadding(dp(14), dp(13), dp(14), dp(13));
        c.setBackground(pressable(CARD, CARD_PRESSED, 14, STROKE));
        c.setClickable(true);
        return c;
    }

    private void mainButton(String text, Runnable r, LinearLayout row) {
        TextView b = tv(text, 15, 0xFF0E1116, true);
        b.setGravity(Gravity.CENTER);
        b.setPadding(dp(10), dp(14), dp(10), dp(14));
        b.setBackground(pressable(ACCENT, ACCENT_DARK, 12, 0));
        b.setOnClickListener(v -> r.run());
        row.addView(b, new LinearLayout.LayoutParams(0,
                ViewGroup.LayoutParams.WRAP_CONTENT, 2f));
    }

    private TextView subButton(String text, Runnable r) {
        TextView b = tv(text, 13, 0xFFC9D1DD, false);
        b.setGravity(Gravity.CENTER);
        b.setPadding(dp(10), dp(12), dp(10), dp(12));
        b.setBackground(pressable(0xFF1B202A, 0xFF262E3B, 12, STROKE));
        b.setOnClickListener(v -> r.run());
        return wrapTop(b, 12);
    }

    // ─────────────────────────── 样式助手 ───────────────────────────

    private int dp(int v) {
        return Math.round(v * getResources().getDisplayMetrics().density);
    }

    private TextView tv(String s, float sp, int color, boolean bold) {
        TextView t = new TextView(this);
        t.setText(s);
        t.setTextSize(sp);
        t.setTextColor(color);
        if (bold) t.setTypeface(Typeface.DEFAULT_BOLD);
        return t;
    }

    private GradientDrawable round(int fill, int radiusDp) {
        GradientDrawable d = new GradientDrawable();
        d.setColor(fill);
        d.setCornerRadius(dp(radiusDp));
        return d;
    }

    private GradientDrawable roundStroke(int fill, int radiusDp, int stroke) {
        GradientDrawable d = round(fill, radiusDp);
        d.setStroke(Math.max(1, dp(1)), stroke);
        return d;
    }

    /** 按下态变色的可点背景（默认 Button 的灰底方块太丑，自己搭）。 */
    private Drawable pressable(int fill, int pressed, int radiusDp, int stroke) {
        GradientDrawable a = stroke == 0 ? round(fill, radiusDp) : roundStroke(fill, radiusDp, stroke);
        GradientDrawable b = stroke == 0 ? round(pressed, radiusDp)
                : roundStroke(pressed, radiusDp, stroke);
        StateListDrawable s = new StateListDrawable();
        s.addState(new int[]{android.R.attr.state_pressed}, b);
        s.addState(new int[]{android.R.attr.state_focused}, b);
        s.addState(new int[0], a);
        return s;
    }

    /**
     * 给视图设「上方留白」，并让它撑满宽度。
     *
     * ★ 不用外面再包一层 LinearLayout：包一层会让 `setBackground` 落错对象
     *   （背景画在包装层上、内边距却在内层），第一版的第二行被裁就是这个原因。
     */
    private <T extends View> T wrapTop(T v, int topDp) {
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        lp.topMargin = dp(topDp);
        v.setLayoutParams(lp);
        return v;
    }
}
