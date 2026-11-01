package io.pgauto.inject;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.graphics.Point;
import android.hardware.display.DisplayManager;
import android.os.Build;
import android.os.IBinder;
import android.util.Log;
import android.view.Display;

import org.json.JSONObject;

import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.security.MessageDigest;
import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.List;
import java.util.Locale;

import io.pgauto.inject.sched.Densify;
import io.pgauto.inject.sched.Scheduler;

/**
 * 端内「脑」的服务壳：待命 → 收零点 → 注入 → 落盘结果。
 *
 * 为什么必须是**前台服务**而不是 Activity 里跑：
 *   注入期间游戏在前台，本 App 的 Activity 必然 onStop ⇒ 进程降级成 cached，
 *   会被 HONOR 的省电策略限频甚至冻结。前台服务是唯一能保证「160 秒不被打断」的位置。
 *
 * ★ 三份文件都在 App 的外部私有目录（`/sdcard/Android/data/<pkg>/files/`）：
 *   `arm.json`    PC → App：本次跑什么（载荷名 / 目标尺寸 / 开关）
 *   `run.json`    PC → App：零点（设备墙钟 epoch ms），**必须 id 与 arm 一致**
 *   `armed.json`  App → PC：待命结果（门禁/载荷条数/显示尺寸/哈希）⇒ PC 的点火前校验
 *   `result.json` App → PC：注入结果（条数 / lag 分位 / 中止原因）
 *   用外部私有目录的理由：`adb push` 直接可写，不需要 `run-as`、不需要存储权限，
 *   而它仍在 App 私有路径下、跑完即删（设备零残留）。
 */
public class InjectService extends Service {

    public static final String ACTION_ARM = "io.pgauto.inject.ARM";
    public static final String ACTION_STOP = "io.pgauto.inject.STOP";
    private static final String TAG = ShizukuProbe.TAG;
    private static final String CH = "pginject";
    private static final int NOTIF_ID = 4211;
    /** 待命后等 run.json 的上限：够走完「起录音 → 点火 → 侧台锚」还有余量。 */
    private static final long ARM_TIMEOUT_MS = 600_000L;

    private volatile boolean stopFlag;
    private Thread worker;
    private File dir;
    private NotificationManager nm;

    @Override
    public void onCreate() {
        super.onCreate();
        nm = (NotificationManager) getSystemService(Context.NOTIFICATION_SERVICE);
        if (nm != null) {
            NotificationChannel ch = new NotificationChannel(
                    CH, "端内注入", NotificationManager.IMPORTANCE_LOW);
            // LOW = 不出横幅、不出声：避免通知盖在游戏画面上或引入额外帧开销
            ch.setShowBadge(false);
            nm.createNotificationChannel(ch);
        }
    }

    @Override
    public IBinder onBind(Intent intent) { return null; }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        String act = intent == null || intent.getAction() == null ? ACTION_ARM : intent.getAction();
        if (ACTION_STOP.equals(act)) {
            stopFlag = true;
            say("收到停止指令 ⇒ 下一帧起停止注入并抬起手指");
            return START_NOT_STICKY;
        }
        startForeground(NOTIF_ID, notif("待命中…"));
        if (worker == null || !worker.isAlive()) {
            stopFlag = false;
            worker = new Thread(this::armLoop, "pg-brain");
            worker.start();
        }
        return START_NOT_STICKY;
    }

    @Override
    public void onDestroy() {
        stopFlag = true;
        super.onDestroy();
    }

    private Notification notif(String text) {
        Notification.Builder b = Build.VERSION.SDK_INT >= 26
                ? new Notification.Builder(this, CH)
                : new Notification.Builder(this);
        return b.setContentTitle("PG 注入器 · 端内")
                .setContentText(text)
                .setSmallIcon(android.R.drawable.ic_menu_compass)
                .setOngoing(true)
                .setShowWhen(false)
                .build();
    }

    private void phase(String s) {
        if (nm != null) nm.notify(NOTIF_ID, notif(s));
        say(s);
    }

    /** 同时写 logcat 与 status.txt（PC 侧可直接 pull 看进度）。 */
    private void say(String s) {
        Log.i(TAG, "[svc] " + s);
        if (dir == null) return;
        try (FileOutputStream fo = new FileOutputStream(new File(dir, "status.txt"), true)) {
            String ts = new SimpleDateFormat("HH:mm:ss.SSS", Locale.ROOT).format(new Date());
            fo.write((ts + "  " + s + "\n").getBytes(StandardCharsets.UTF_8));
        } catch (IOException ignore) {
            // 状态文件写不进去不影响注入；logcat 还有一份
        }
    }

    // ─────────────────────────── 主流程 ───────────────────────────

    private void armLoop() {
        dir = getExternalFilesDir(null);
        if (dir == null) dir = getFilesDir();
        say("==== 待命开始 dir=" + dir);

        // 清掉上一轮的产物：否则 PC 会读到旧 result.json / 旧的零点
        for (String n : new String[]{"armed.json", "result.json", "run.json"}) {
            File f = new File(dir, n);
            if (f.exists() && !f.delete()) say("⚠ 删不掉旧文件 " + n);
        }

        try {
            doArm();
        } catch (Throwable t) {
            say("!! 待命/注入异常中止: " + ShizukuProbe.brief(t));
            writeArmed(false, "异常: " + ShizukuProbe.brief(t), null);
            finish(0);
        }
    }

    private void doArm() throws Exception {
        // ① 门禁：没有 Shizuku 代理就没有「手」，后面的检查全是空转
        ShizukuProbe.Report r = ShizukuProbe.runAll();
        if (!r.allOk()) {
            say("✗ 门禁未过 ⇒ 不待命（先在界面上授权/重检）");
            writeArmed(false, "Shizuku 门禁未过", null);
            finish(0);
            return;
        }
        say("✓ 门禁通过（proxy=" + r.inputManagerProxy.getClass().getName() + "）");

        // ② 常量对齐（Gesture 的动作码 vs MotionEvent）
        String cErr = DevRun.checkConstants();
        if (cErr != null) {
            say("✗ " + cErr + " ⇒ 拒绝开火");
            writeArmed(false, cErr, null);
            finish(0);
            return;
        }

        // ③ arm.json（缺了必须显式失败，不能"当成 0 条事件正常跑"）
        File armF = new File(dir, "arm.json");
        if (!armF.exists()) {
            say("✗ 缺 arm.json（PC 侧没准备好本次参数）⇒ 不待命");
            writeArmed(false, "缺 arm.json", null);
            finish(0);
            return;
        }
        JSONObject arm = new JSONObject(new String(Files.readAllBytes(armF.toPath()),
                StandardCharsets.UTF_8));
        String id = arm.getString("id");
        String payloadName = arm.getString("payload");
        double dstW = arm.getDouble("dst_w");
        double dstH = arm.getDouble("dst_h");
        String fit = arm.optString("fit", "height");
        boolean dropMoves = arm.optBoolean("drop_moves", true);
        boolean useDue = arm.optBoolean("use_due_time", true);
        // ★ 默认开：densify 是「20px 一跳 → 60px 一跳」的保护层，PC 生产路径一直开着。
        //   dp1 轮（2026-10-21）端内漏了这一层 ⇒ 505 P / 15 Miss / MaxCombo 197。
        boolean densify = arm.optBoolean("densify", true);
        int maxFalse = arm.optInt("max_false", -1);

        // ④ 载荷
        File pf = new File(dir, payloadName);
        if (!pf.exists()) {
            say("✗ 缺载荷 " + payloadName + " ⇒ 不待命");
            writeArmed(false, "缺载荷 " + payloadName, null);
            finish(0);
            return;
        }
        long t0 = System.nanoTime();
        Densify.Stats dst = new Densify.Stats();
        List<Scheduler.Cmd> cmds = Scheduler.build(pf.toPath(), dstW, dstH,
                "height".equals(fit), densify, dst);
        double buildMs = (System.nanoTime() - t0) / 1e6;
        if (cmds.isEmpty()) {
            say("✗ 载荷解析出 0 条指令 ⇒ 不待命（不降级成「打空」）");
            writeArmed(false, "载荷 0 条指令", null);
            finish(0);
            return;
        }
        String sha = sha256(pf);
        int nKey = 0;
        for (Scheduler.Cmd c : cmds) if (c.key()) nKey++;
        double firstT = cmds.get(0).tMs();
        double lastT = cmds.get(cmds.size() - 1).tMs();
        say("✓ " + DevRun.describe(pf, cmds) + String.format(Locale.ROOT,
                "（解析 %.1f ms，sha %s…）", buildMs, sha.substring(0, 12)));
        say("✓ MOVE 加密：" + (densify ? dst.toString() : "关（A/B）"));

        // ⑤ 显示尺寸：查不到就拒绝（换机头号杀手，绝不猜）
        String disp = displaySize();
        String want = (int) Math.rint(dstW) + "x" + (int) Math.rint(dstH);
        if (disp == null) {
            say("✗ 读不到显示尺寸 ⇒ 不待命（拒绝猜测）");
            writeArmed(false, "读不到显示尺寸", null);
            finish(0);
            return;
        }
        if (!disp.equals(want)) {
            say("✗ 显示尺寸 " + disp + " ≠ 载荷目标 " + want
                    + " ⇒ 不待命（坐标会整体错位）");
            writeArmed(false, "显示尺寸 " + disp + " ≠ " + want, null);
            finish(0);
            return;
        }
        say("✓ 显示尺寸 " + disp + " == 目标 " + want + "（fit=" + fit + "）");

        // ⑥ armed.json：这就是 PC 的点火前校验依据
        JSONObject a = new JSONObject();
        a.put("ok", true);
        a.put("id", id);
        a.put("payload", payloadName);
        a.put("sha256", sha);
        a.put("n_cmd", cmds.size());
        a.put("n_key", nKey);
        a.put("first_t_ms", firstT);
        a.put("last_t_ms", lastT);
        a.put("dst", want);
        a.put("display", disp);
        a.put("fit", fit);
        a.put("drop_moves", dropMoves);
        a.put("use_due_time", useDue);
        a.put("densify", densify);
        a.put("n_densify_added", dst.nAdded + dst.nTailAdded);
        a.put("build_ms", Math.rint(buildMs * 10) / 10.0);
        a.put("armed_dev_ms", (double) System.currentTimeMillis());
        writeJson("armed.json", a);
        phase("待命中（" + cmds.size() + " 条），等零点…");

        // ⑦ 等 run.json
        double zeroDevMs = Double.NaN;
        long deadline = System.currentTimeMillis() + ARM_TIMEOUT_MS;
        File runF = new File(dir, "run.json");
        while (!stopFlag && System.currentTimeMillis() < deadline) {
            if (runF.exists()) {
                try {
                    JSONObject run = new JSONObject(new String(
                            Files.readAllBytes(runF.toPath()), StandardCharsets.UTF_8));
                    if (id.equals(run.optString("id")) && run.has("zero_dev_ms")) {
                        zeroDevMs = run.getDouble("zero_dev_ms");
                        break;
                    }
                    say("⚠ run.json 的 id 不匹配（期望 " + id + "，收到 "
                            + run.optString("id") + "）⇒ 忽略");
                    // 改名避免反复刷同一行日志
                    File bad = new File(dir, "run.json.bad");
                    if (!runF.renameTo(bad)) runF.delete();
                } catch (Exception e) {
                    say("⚠ run.json 解析失败（可能还在写）: " + ShizukuProbe.brief(e));
                }
            }
            Thread.sleep(20);
        }
        if (Double.isNaN(zeroDevMs)) {
            say(stopFlag ? "已停止（未收到零点）" : "✗ 等零点超时 ⇒ 未注入任何事件");
            writeArmed(false, stopFlag ? "用户停止" : "等 run.json 超时", null);
            finish(0);
            return;
        }

        // ⑧ 注入
        double leadMs = zeroDevMs - System.currentTimeMillis();
        phase("收到零点（" + String.format(Locale.ROOT, "%+.0f ms", leadMs) + "），注入中…");
        if (leadMs < -30_000) {
            say("⚠ 零点已在 " + String.format(Locale.ROOT, "%.0f", -leadMs)
                    + " ms 前 ⇒ 追赶式启动，会跳过这之前的事件");
        }
        if (maxFalse < 0) maxFalse = Math.max(3, cmds.size() / 200);
        DevRun brain = new DevRun(r.inputManagerProxy, r.injectMethod, useDue, dropMoves, maxFalse,
                this::say);
        long tStart = System.nanoTime();
        brain.run(cmds, zeroDevMs);
        double runS = (System.nanoTime() - tStart) / 1e9;

        // ⑨ result.json
        JSONObject res = brain.summary();
        res.put("id", id);
        res.put("payload", payloadName);
        res.put("sha256", sha);
        res.put("n_cmd", cmds.size());
        res.put("n_key", nKey);
        res.put("dst", want);
        res.put("display", disp);
        res.put("zero_dev_ms", zeroDevMs);
        res.put("lead_ms", Math.rint(leadMs * 10) / 10.0);
        res.put("run_s", Math.rint(runS * 100) / 100.0);
        res.put("result_dev_ms", (double) System.currentTimeMillis());
        writeJson("result.json", res);
        phase("完成：发 " + brain.emitted() + "，抽稀 MOVE " + res.opt("n_skip_dense")
                + "，丢 MOVE " + brain.dropped() + "，false " + brain.falses());
        // ★ MOVE 提前量必须当场打出来：15 Miss 那一轮就是因为「只看关键事件 lag」而全绿。
        Object mvP50 = res.opt("move_lead_p50_ms");
        say("✓ MOVE 提前量 p50 " + mvP50 + " / p99 " + res.opt("move_lead_p99_ms")
                + " / max " + res.opt("move_lead_max_ms") + " ms，>50ms 提前 "
                + res.opt("n_move_early_gt50") + " 条（不变量：应为 0）");
        finish(brain.emitted());
    }

    private void finish(int emitted) {
        try {
            stopForeground(true);
        } catch (Throwable ignore) {
            // 低版本/异常状态下 stopForeground 可能抛，不影响结果
        }
        stopSelf();
    }

    // ─────────────────────────── 工具 ───────────────────────────

    private void writeArmed(boolean ok, String why, JSONObject extra) {
        try {
            JSONObject a = extra != null ? extra : new JSONObject();
            a.put("ok", ok);
            if (!ok) a.put("why", why);
            a.put("armed_dev_ms", (double) System.currentTimeMillis());
            writeJson("armed.json", a);
        } catch (Exception e) {
            say("⚠ 写 armed.json 失败: " + ShizukuProbe.brief(e));
        }
    }

    private void writeJson(String name, JSONObject o) {
        try {
            File f = new File(dir, name);
            Files.write(f.toPath(), o.toString(1).getBytes(StandardCharsets.UTF_8));
            say("写出 " + name + "（" + f.length() + " B）");
        } catch (Exception e) {
            say("!! 写 " + name + " 失败: " + ShizukuProbe.brief(e));
        }
    }

    /** 归一化成横屏宽x高（与 PC 侧 `wm size` 的口径一致：720x1610 ⇒ "1610x720"）。 */
    private String displaySize() {
        try {
            DisplayManager dm = (DisplayManager) getSystemService(Context.DISPLAY_SERVICE);
            if (dm == null) return null;
            Display d = dm.getDisplay(Display.DEFAULT_DISPLAY);
            if (d == null) return null;
            Point p = new Point();
            d.getRealSize(p);
            if (p.x <= 0 || p.y <= 0) return null;
            return Math.max(p.x, p.y) + "x" + Math.min(p.x, p.y);
        } catch (Throwable t) {
            say("⚠ 查显示尺寸异常: " + ShizukuProbe.brief(t));
            return null;
        }
    }

    private static String sha256(File f) throws Exception {
        MessageDigest md = MessageDigest.getInstance("SHA-256");
        byte[] d = md.digest(Files.readAllBytes(f.toPath()));
        StringBuilder sb = new StringBuilder(d.length * 2);
        for (byte b : d) sb.append(String.format(Locale.ROOT, "%02x", b));
        return sb.toString();
    }
}
