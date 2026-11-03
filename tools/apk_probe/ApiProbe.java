/**
 * 能力探针：端内方案依赖的几条 API 到底能不能在 shell(uid 2000) 身份下碰到。
 *
 * 为什么不用「文档上写着 API 24+ 可用」就下结论：
 *   ① `GestureDescription.getMaxStrokeCount()` 是 @hide，普通应用受 hidden API 限制；
 *   ② 网上大量代码把上限硬编码成 10 / 60000，但这两个值随系统实现变化；
 *   ③ Shizuku UserService 跑的是 app_process，和本探针同一启动方式 ⇒ 本探针的结论
 *      对 Shizuku 档直接适用，不需要先装 Shizuku 才能验证。
 */
public final class ApiProbe {

    public static void main(String[] argv) {
        System.out.println("uid=" + android.os.Process.myUid()
                + " (2000=shell) sdk=" + android.os.Build.VERSION.SDK_INT);

        probeGesture();
        probeInput();

        System.out.println("-- 硬编码对照 --");
        System.out.println("常见硬编码值：maxStroke=10, maxGestureDuration=60000ms —— 与本机真值是否一致见上");
    }

    private static void probeGesture() {
        try {
            Class<?> gd = Class.forName("android.accessibilityservice.GestureDescription");
            for (String name : new String[]{"getMaxStrokeCount", "getMaxGestureDuration"}) {
                try {
                    java.lang.reflect.Method m = gd.getMethod(name);
                    Object v = m.invoke(null);
                    System.out.println("GestureDescription." + name + " = " + v);
                } catch (Throwable t) {
                    System.out.println("GestureDescription." + name + " FAIL: " + t);
                }
            }
        } catch (Throwable t) {
            System.out.println("GestureDescription 类加载 FAIL: " + t);
        }
    }

    private static void probeInput() {
        try {
            Class<?> imc = Class.forName("android.hardware.input.InputManager");
            Object im = imc.getMethod("getInstance").invoke(null);
            System.out.println("InputManager.getInstance() = " + (im != null ? "OK" : "null"));
            boolean found = false;
            for (java.lang.reflect.Method m : imc.getMethods()) {
                if (m.getName().toLowerCase().contains("inject")) {
                    System.out.println("  inject 方法: " + m);
                    found = true;
                }
            }
            if (!found) {
                System.out.println("  ! 无 public inject* 方法（可能只暴露在 IInputManager 接口）");
            }
        } catch (Throwable t) {
            System.out.println("InputManager FAIL: " + t);
        }

        // 第二条路：直接拿 IInputManager 这个系统服务的 binder
        try {
            Class<?> sm = Class.forName("android.os.ServiceManager");
            Object binder = sm.getMethod("getService", String.class).invoke(null, "input");
            System.out.println("ServiceManager.getService(\"input\") = " + (binder != null ? "OK" : "null"));
        } catch (Throwable t) {
            System.out.println("ServiceManager FAIL: " + t);
        }
    }
}
