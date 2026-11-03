/**
 * 注入通道探针 v2：为什么 v1 的 InputManager.getInstance() 抛 InvocationTargetException？
 *
 * 猜测（要在设备上证实/否证）：
 *   app_process 直接起进程时没有初始化 ActivityThread/Looper，
 *   而 InputManager 的内部实现依赖主线程 Looper ⇒ 先 prepareMainLooper 再取。
 *   这条如果成立，就必须写进方案：端内注入进程的初始化顺序是硬约束。
 *
 * 同时验证第二条路：绕过 InputManager，直接拿 ServiceManager 的 input binder
 * 走 IInputManager.Stub.asInterface —— scrcpy 的部分版本就是这么做的。
 */
public final class ApiProbe2 {

    public static void main(String[] argv) {
        System.out.println("uid=" + android.os.Process.myUid());

        // ── 路 1a：不初始化 Looper，直接取（复现 v1 的失败）──
        try {
            System.out.println("[1a] getInstance 前无 Looper: " + getInputManager());
        } catch (Throwable t) {
            System.out.println("[1a] FAIL " + t + " cause=" + t.getCause());
        }

        // ── 初始化主线程 Looper ──
        try {
            android.os.Looper.prepareMainLooper();
            System.out.println("[init] Looper.prepareMainLooper() OK, 主线程=" + android.os.Looper.getMainLooper());
        } catch (Throwable t) {
            System.out.println("[init] FAIL " + t);
        }

        // ── 路 1b：有了 Looper 再取 ──
        try {
            Object im = getInputManager();
            System.out.println("[1b] getInstance 后: " + im);
            dumpInjectMethods(im.getClass());
        } catch (Throwable t) {
            System.out.println("[1b] FAIL " + t + " cause=" + t.getCause());
        }

        // ── 路 2：ServiceManager → IInputManager.Stub.asInterface ──
        try {
            Class<?> sm = Class.forName("android.os.ServiceManager");
            Object binder = sm.getMethod("getService", String.class).invoke(null, "input");
            Class<?> iim = Class.forName("android.hardware.input.IInputManager$Stub");
            Object svc = iim.getMethod("asInterface", Class.forName("android.os.IBinder")).invoke(null, binder);
            System.out.println("[2] IInputManager.asInterface = " + (svc != null ? "OK" : "null"));
            if (svc != null) {
                dumpInjectMethods(svc.getClass());
            }
        } catch (Throwable t) {
            System.out.println("[2] FAIL " + t + " cause=" + t.getCause());
        }
    }

    private static Object getInputManager() throws Exception {
        Class<?> imc = Class.forName("android.hardware.input.InputManager");
        return imc.getMethod("getInstance").invoke(null);
    }

    private static void dumpInjectMethods(Class<?> c) {
        int n = 0;
        for (java.lang.reflect.Method m : c.getMethods()) {
            if (m.getName().toLowerCase().contains("inject")) {
                System.out.println("     " + m);
                n++;
            }
        }
        if (n == 0) {
            System.out.println("     ! 该实现上没暴露 inject*（可能需要 getDeclaredMethod 走接口）");
        }
    }
}
