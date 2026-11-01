package io.pgauto.play;

import android.os.ParcelFileDescriptor;

import java.io.BufferedReader;
import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;

import io.pgauto.inject.ShizukuProbe;

import moe.shizuku.server.IRemoteProcess;
import moe.shizuku.server.IShizukuService;
import rikka.shizuku.Shizuku;

/**
 * 通过 **Shizuku** 起 shell 进程跑 `getevent`，把用户手指落在屏幕上的位置读出来。
 *
 * <h3>为什么这条路可行（2026-10-31 实测，不是推断）</h3>
 * <pre>
 *   ps -A -Z   → shizuku_server  context=u:r:shell:s0  uid shell
 *   id         → shell 的组含 1004(input)
 *   ls -Z      → /dev/input/event5  u:object_r:input_device:s0
 * </pre>
 * 也就是说 Shizuku 服务与 `adb shell` **同身份同域**，而 shell 属于 `input` 组 ⇒ 能读触摸设备。
 * 端内起进程用 `IShizukuService.newProcess`（`shizuku-aidl-13_1_5.jar` 里就有）。
 *
 * <h3>为什么要先 `getevent -p` 再监听</h3>
 * `eventN` 是内核枚举顺序，换机/插拔/重启都会变。直接把 `/dev/input/event5` 写死，
 * 在别人机器上可能读到指纹、电源键，甚至读不到 ⇒ 永远学不到落点。
 * 所以每次先跑一次 `-p`，用 {@link TouchDevices#pick} 挑唯一触摸屏（挑不出就报错，不猜）。
 *
 * <h3>边界</h3>
 * - **只在「学习落点」窗口期间存在**，用完立刻 {@link #stop()}；不许常驻（占一个 shell 进程）。
 * - 本类**不做坐标换算**（那是 {@link AxisMap} 的事）、**不做事件判读**（{@link GetEventParser}），
 *   只负责"把行喂进去、把事件交出去"。
 */
public final class TouchSniff {

    /** 事件回调（在读取线程上调用，回调里别做重活）。 */
    public interface Sink {
        void onTouch(int[] ev);
    }

    /**
     * ⛔ 用绝对路径：`newProcess` 起进程时的 PATH 取决于 `shizuku_server` 的环境，
     * 不能假设一定含 `/system/bin`（起不来时 stderr 只有一行 "not found"，很容易被当成"读不到触摸"）。
     */
    private static final String GETEVENT = "/system/bin/getevent";

    private IRemoteProcess proc;
    private Thread reader;
    private volatile boolean running;

    /** 收到了多少行原始输出、解析出多少事件 —— 用来区分「管道没数据」和「解析不出来」。 */
    private volatile int rawLines;
    private volatile int events;
    private volatile int logged;

    /** 头多少行原文进日志（一次点击约 15 行；够看清整帧又不至于刷屏）。 */
    private static final int RAW_DUMP_MAX = 24;

    /** 取 Shizuku 服务代理。失败抛异常，由调用方转成用户看得懂的提示。 */
    public static IShizukuService service() throws Exception {
        if (!Shizuku.pingBinder()) {
            throw new IllegalStateException("Shizuku 未运行（先启动 Shizuku 并授权本 App）");
        }
        if (Shizuku.checkSelfPermission() != android.content.pm.PackageManager.PERMISSION_GRANTED) {
            throw new IllegalStateException("Shizuku 未授权本 App");
        }
        return IShizukuService.Stub.asInterface(Shizuku.getBinder());
    }

    /** 跑 `getevent -p`，挑出唯一触摸屏的路径。挑不出（0 个/多个）抛异常，不猜。 */
    public static String devicePath() throws Exception {
        String text = execReadAll(service(), new String[]{GETEVENT, "-p"});
        return TouchDevices.pick(text).path;
    }

    /** 跑一条命令并把 stdout 全读回来（用于 `-p` 这种短命令）。 */
    public static String execReadAll(IShizukuService svc, String[] cmd) throws Exception {
        IRemoteProcess p = svc.newProcess(cmd, null, null);
        ByteArrayOutputStream bos = new ByteArrayOutputStream();
        try (InputStream in = open(p)) {
            byte[] buf = new byte[8192];
            int n;
            while ((n = in.read(buf)) > 0) {
                bos.write(buf, 0, n);
                if (bos.size() > 1 << 20) break;   // 防御：`-p` 不该超过 1 MB
            }
        } finally {
            try {
                p.destroy();
            } catch (Throwable ignore) {
                // 已经退出就无所谓
            }
        }
        return new String(bos.toByteArray(), StandardCharsets.UTF_8);
    }

    /**
     * 开始监听。
     *
     * @param devPath `devicePath()` 返回的路径
     * @param sink    每解析出一个 DOWN/UP/MOVE 回调一次
     * @throws Exception Shizuku 不可用 / 起进程失败
     */
    public void start(String devPath, Sink sink) throws Exception {
        stop();
        IShizukuService svc = service();
        proc = svc.newProcess(new String[]{GETEVENT, devPath}, null, null);
        final InputStream in;
        try {
            in = open(proc);
        } catch (Throwable t) {
            // ★ 自审补的：进程已经起来了但拿不到 stdout ⇒ 必须销毁，否则留下一个
            //   常驻的 getevent 进程（本项目对"设备侧零残留"是有硬要求的）。
            try {
                proc.destroy();
            } catch (Throwable ignore) {
                // 已经退出就算了
            }
            proc = null;
            throw t instanceof Exception ? (Exception) t : new IllegalStateException(t);
        }
        final InputStream err;
        try {
            err = new ParcelFileDescriptor.AutoCloseInputStream(proc.getErrorStream());
        } catch (Throwable t) {
            // 拿不到 stderr 不影响主流程；只是丢一条诊断
            RunLog.line("触摸监听：拿不到 stderr（" + ShizukuProbe.brief(t) + "）");
            startReaderOnly(in, devPath, sink);
            return;
        }
        // ★ stderr 必须读：起不来时唯一线索就这一行（"not found" / "Permission denied"）。
        //   不读它，用户只看到"15 秒内没等到点击"，我们会去查方向、查解析，全是死路。
        Thread errThread = new Thread(() -> {
            try (BufferedReader br = new BufferedReader(
                    new InputStreamReader(err, StandardCharsets.UTF_8))) {
                String line;
                int n = 0;
                while ((line = br.readLine()) != null) {
                    if (n++ < 8) RunLog.line("触摸监听 stderr：" + line);
                }
            } catch (Throwable ignore) {
                // 进程被杀就会抛，无所谓
            }
        }, "pg-touchsniff-err");
        errThread.setDaemon(true);
        errThread.start();

        running = true;
        rawLines = 0;
        events = 0;
        logged = 0;
        final GetEventParser parser = new GetEventParser();
        reader = new Thread(() -> {
            try (BufferedReader br = new BufferedReader(
                    new InputStreamReader(in, StandardCharsets.UTF_8))) {
                String line;
                boolean first = true;
                while (running && (line = br.readLine()) != null) {
                    rawLines++;
                    if (logged < RAW_DUMP_MAX) {
                        // ★ 头 N 行原文都打出来：一次点击约 15 行，够看完整一帧。
                        //   只有首行不够 —— 10-31 那次就是靠这行才发现"没有设备前缀"；
                        //   再多几行就能直接看出按下标志用的是 BTN_TOUCH 还是 TRACKING_ID。
                        RunLog.line("触摸原始[" + rawLines + "]：" + line);
                        logged++;
                        first = false;
                    }
                    int[] ev = parser.feed(line, devPath);
                    if (ev != null) {
                        events++;
                        if (sink != null) sink.onTouch(ev);
                    }
                }
                if (running) RunLog.line("触摸监听流已结束（rawLines=" + rawLines + "）");
            } catch (Throwable t) {
                if (running) {
                    RunLog.line("触摸监听中断：" + ShizukuProbe.brief(t)
                            + "（rawLines=" + rawLines + "）");
                }
            }
        }, "pg-touchsniff");
        reader.setDaemon(true);
        reader.start();
        RunLog.line("触摸监听已启动：" + GETEVENT + " " + devPath);
    }

    /** stderr 拿不到时的退化路径（只起 stdout 读取）。 */
    private void startReaderOnly(InputStream in, String devPath, Sink sink) {
        running = true;
        rawLines = 0;
        events = 0;
        logged = 0;
        final GetEventParser parser = new GetEventParser();
        reader = new Thread(() -> {
            try (BufferedReader br = new BufferedReader(
                    new InputStreamReader(in, StandardCharsets.UTF_8))) {
                String line;
                boolean first = true;
                while (running && (line = br.readLine()) != null) {
                    rawLines++;
                    if (first) {
                        RunLog.line("触摸监听首行：" + line);
                        first = false;
                    }
                    int[] ev = parser.feed(line, devPath);
                    if (ev != null) {
                        events++;
                        if (sink != null) sink.onTouch(ev);
                    }
                }
                if (running) RunLog.line("触摸监听流已结束（rawLines=" + rawLines + "）");
            } catch (Throwable t) {
                if (running) RunLog.line("触摸监听中断：" + ShizukuProbe.brief(t));
            }
        }, "pg-touchsniff");
        reader.setDaemon(true);
        reader.start();
        RunLog.line("触摸监听已启动（无 stderr）：" + GETEVENT + " " + devPath);
    }

    /** 到目前为止收到多少行原始输出（0 ⇒ 管道里根本没数据，与"解析不出来"是两回事）。 */
    public int rawLines() {
        return rawLines;
    }

    /** 到目前为止解析出多少事件。 */
    public int events() {
        return events;
    }

    /** 停掉监听（幂等）。⛔ 窗口结束必须调，别让它常驻。 */
    public void stop() {
        running = false;
        if (reader != null) {
            reader.interrupt();
            reader = null;
        }
        if (proc != null) {
            try {
                proc.destroy();
            } catch (Throwable ignore) {
                // 进程可能已自行退出
            }
            proc = null;
        }
    }

    public boolean isRunning() {
        return running;
    }

    private static InputStream open(IRemoteProcess p) throws Exception {
        ParcelFileDescriptor pfd = p.getInputStream();
        return new ParcelFileDescriptor.AutoCloseInputStream(pfd);
    }
}
