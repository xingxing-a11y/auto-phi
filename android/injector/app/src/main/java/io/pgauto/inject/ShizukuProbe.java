package io.pgauto.inject;

import android.os.Build;
import android.os.IBinder;
import android.util.Log;

import java.lang.reflect.Field;
import java.lang.reflect.Method;
import java.util.ArrayList;
import java.util.List;

import rikka.shizuku.Shizuku;
import rikka.shizuku.ShizukuBinderWrapper;
import rikka.shizuku.SystemServiceHelper;

/**
 * S0 门禁：验证「Shizuku 代理的系统服务调用」这条路能不能走通。
 *
 * 要回答的问题，一个都不能跳过（缺读数 ≠ 读数正常）：
 *   ① Shizuku 的 binder 收到了吗             → {@code Shizuku.pingBinder()}
 *   ② 拿到 API 权限了吗                      → {@code Shizuku.checkSelfPermission()}
 *   ③ 代理调用是不是真的以 shell 身份执行     → {@code Shizuku.getUid() == 2000}
 *   ④ 代理拿到的 input 服务真的存在吗         → {@code SystemServiceHelper.getSystemService("input")}
 *   ⑤ hidden API 反射有没有被挡               → 反射 {@code IInputManager$Stub.asInterface}
 *   ⑥ 能不能**看到** injectInputEvent 这个方法 → 见下（这是本文件的重点）
 *
 * ★ ⑥ 为什么要单独诊断：
 *   Android 9+ 的 hiddenapi 会对 {@code getDeclaredMethods()} / {@code getDeclaredFields()}
 *   的返回值做**裁剪**——被 blocklist 的成员直接从数组里消失，不抛异常。
 *   所以「找不到 injectInputEvent」有两种完全不同的含义：
 *     (a) 真的不存在（API 变了）
 *     (b) 存在但被隐藏（反射拿不到，但界面上/跨进程调用照常工作）
 *   不区分就会得出「方案不可行」的错误结论。诊断办法是**同时看三处**：
 *     · Stub 的 TRANSACTION_* 字段能看到几个      （字段被裁 = 整类受限）
 *     · 接口 IInputManager 的方法列表             （接口方法与实现方法可被标不同级别）
 *     · Proxy 的 declaredMethods                  （AIDL 生成的实现）
 *   2026-10-21 实测（Android 14 / ALT-AN00）：asInterface 可反射，但上面三处的
 *   injectInputEvent 全部看不见 ⇒ 走 {@code Shizuku.transactRemote} 手工 Parcel（见 S3）。
 *
 * ★ 为什么不用 UserService：
 *   {@code ShizukuBinderWrapper} 能把**任意** binder 调用转发给 shell uid 执行
 *   （它的 transact 内部走 {@code Shizuku.transactRemote}），所以只需要把 Parcel 构造成
 *   IInputManager.injectInputEvent 要的形状即可，不必自己起一个 uid=2000 的进程。
 */
public final class ShizukuProbe {

    public static final String TAG = "PgInject";

    /** 注入一个事件用的 flag：0 = FLAG_SYNC（同步等待注入完成），与 10-20 实测 600/600 一致。 */
    public static final int INJECT_FLAG_DEFAULT = 0;

    private ShizukuProbe() {}

    /** 一步的结论。fail 不为 null 表示抛异常（必须和「结果为 false」区分开）。 */
    public static final class Step {
        public final String name;
        public final boolean ok;
        public final String detail;
        public final String fail;
        /** 是否参与门禁判定。诊断项（统计类）传 false——它们只提供信息，不该让门禁失败。 */
        public final boolean gate;

        Step(String name, boolean ok, String detail, String fail) {
            this(name, ok, detail, fail, true);
        }

        Step(String name, boolean ok, String detail, String fail, boolean gate) {
            this.name = name;
            this.ok = ok;
            this.detail = detail;
            this.fail = fail;
            this.gate = gate;
        }

        @Override public String toString() {
            String head = (fail != null ? "[ERR ] " : (ok ? "[ OK ] " : "[FAIL] ")) + name;
            if (fail != null) return head + " ⇒ 异常: " + fail;
            return head + (detail == null || detail.isEmpty() ? "" : " ⇒ " + detail);
        }
    }

    public static final class Report {
        public final List<Step> steps = new ArrayList<>();
        public final List<String> notes = new ArrayList<>();
        /** ⑥ 成功时留下 Proxy，S3 的注入直接用它。 */
        public Object inputManagerProxy;
        /** 从**接口**拿到的 injectInputEvent（优先用它，因为接口方法与实现方法级别可能不同）。 */
        public Method injectMethod;

        public boolean allOk() {
            // 只统计 gate=true 的项。诊断项即便「没找到」也不代表门禁失败。
            boolean any = false;
            for (Step s : steps) {
                if (!s.gate) continue;
                any = true;
                if (!s.ok) return false;
            }
            return any && inputManagerProxy != null;
        }

        public String toText() {
            StringBuilder sb = new StringBuilder();
            for (Step s : steps) sb.append(s).append(s.gate ? "" : "   (诊断)").append('\n');
            for (String n : notes) sb.append("· ").append(n).append('\n');
            sb.append(allOk() ? "⇒ 门禁通过：Shizuku 代理路径可用" : "⇒ 门禁未过，见上");
            return sb.toString();
        }
    }

    /** 跑完整检测。**不注入任何事件**——这一步只查能力，不动屏幕。 */
    public static Report runAll() {
        Report r = new Report();

        r.notes.add("app uid=" + android.os.Process.myUid()
                + "  sdk=" + Build.VERSION.SDK_INT
                + "  " + Build.MANUFACTURER + " " + Build.MODEL);

        // ---- ① binder ----
        boolean ping = false;
        try {
            ping = Shizuku.pingBinder();
            r.steps.add(new Step("Shizuku pingBinder", ping, String.valueOf(ping), null));
        } catch (Throwable t) {
            r.steps.add(new Step("Shizuku pingBinder", false, null, brief(t)));
        }
        if (!ping) {
            // 没有 binder 就什么都验不了。宁可停在这里，也不要输出一堆 false
            // 让人误以为「反射失败」——那是把两种完全不同的故障混成一个。
            r.notes.add("binder 未收到 ⇒ 后面各项无从验证，已跳过（先确认 shizuku_server 在跑且已授权）");
            emit(r);
            return r;
        }

        // ---- ② 权限 ----
        try {
            int perm = Shizuku.checkSelfPermission();
            boolean granted = perm == android.content.pm.PackageManager.PERMISSION_GRANTED;
            r.steps.add(new Step("checkSelfPermission", granted, "perm=" + perm, null));
            if (!granted) {
                r.notes.add("权限未授予 ⇒ 点「申请权限」后在手机上允许，再点「重新检测」");
                emit(r);
                return r;
            }
        } catch (Throwable t) {
            r.steps.add(new Step("checkSelfPermission", false, null, brief(t)));
            emit(r);
            return r;
        }

        // ---- ③ 证明 binder 背后确实是 shell 身份 ----
        try {
            int uid = Shizuku.getUid();
            r.steps.add(new Step("Shizuku.getUid", uid == 2000, "uid=" + uid + "（2000=shell）", null));
        } catch (Throwable t) {
            r.steps.add(new Step("Shizuku.getUid", false, null, brief(t)));
        }

        // ---- ④ input 服务的 binder ----
        IBinder inputBinder = null;
        try {
            inputBinder = SystemServiceHelper.getSystemService("input");
            r.steps.add(new Step("getSystemService(\"input\")", inputBinder != null,
                    inputBinder == null ? "null" : inputBinder.toString(), null));
        } catch (Throwable t) {
            r.steps.add(new Step("getSystemService(\"input\")", false, null, brief(t)));
        }
        if (inputBinder == null) {
            emit(r);
            return r;
        }

        // ---- ⑤ asInterface 反射（门禁项：这条过不了，整条路就废）----
        Class<?> iface = null, stub = null;
        try {
            iface = Class.forName("android.hardware.input.IInputManager");
            stub = Class.forName("android.hardware.input.IInputManager$Stub");
            Method asInterface = stub.getDeclaredMethod("asInterface", IBinder.class);
            asInterface.setAccessible(true);
            Object proxy = asInterface.invoke(null, new ShizukuBinderWrapper(inputBinder));
            r.inputManagerProxy = proxy;
            r.steps.add(new Step("reflect IInputManager$Stub.asInterface", proxy != null,
                    proxy == null ? "null" : proxy.getClass().getName(), null));
        } catch (Throwable t) {
            r.steps.add(new Step("reflect IInputManager$Stub.asInterface", false, null, brief(t)));
            r.notes.add("★ asInterface 失败 ⇒ ShizukuBinderWrapper 路线存疑，"
                    + "需评估退回 UserService（独立进程，不受 app 的 hiddenapi 限制）");
        }

        // ---- ⑥ 诊断：injectInputEvent 到底是「不存在」还是「被 hiddenapi 裁剪」----
        //
        // 这三项全部 gate=false：它们是**信息**，不是门禁条件。
        // 理由：方法看不见 ≠ 不能调用（跨进程 transact 不依赖 Java 方法可见性）。
        r.steps.add(scanStubFields(stub));
        r.steps.add(scanIfaceMethods(iface, r));
        r.steps.add(scanProxyMethods(r.inputManagerProxy));

        // ---- ⑦ Shizuku 自带的 transact code 解析（它会反射 TRANSACTION_* 字段，同样受裁剪影响）----
        try {
            Integer code = SystemServiceHelper.getTransactionCode("input", "injectInputEvent");
            r.steps.add(new Step("getTransactionCode(injectInputEvent)", code != null,
                    code == null ? "null（TRANSACTION_* 字段不可见或服务名对不上）" : "code=" + code,
                    null, false));
        } catch (Throwable t) {
            r.steps.add(new Step("getTransactionCode(injectInputEvent)", false, null, brief(t), false));
        }

        emit(r);
        return r;
    }

    /** Stub 的 TRANSACTION_* 字段。若一个都看不见 ⇒ 整类被 hiddenapi 限制。 */
    private static Step scanStubFields(Class<?> stub) {
        if (stub == null) return new Step("Stub 可见字段", false, "类没拿到", null, false);
        int total = 0;
        List<String> hits = new ArrayList<>();
        try {
            for (Field f : stub.getDeclaredFields()) {
                total++;
                if (f.getName().contains("inject")) hits.add(f.getName());
            }
        } catch (Throwable t) {
            return new Step("Stub 可见字段", false, null, brief(t), false);
        }
        return new Step("Stub 可见字段 contains(inject)", !hits.isEmpty(),
                "共 " + total + " 个；命中 " + hits.size() + (hits.isEmpty() ? "" : ": " + hits),
                null, false);
    }

    /**
     * ★ 接口方法优先。AIDL 的接口声明在 IInputManager 上，实现类 Stub$Proxy 只是 override；
     * hiddenapi 对这两者的分级不一定相同，所以两边都要试。
     */
    private static Step scanIfaceMethods(Class<?> iface, Report r) {
        if (iface == null) return new Step("IInputManager 接口方法", false, "类没拿到", null, false);
        int total = 0;
        List<String> hits = new ArrayList<>();
        Method found = null;
        try {
            for (Method m : iface.getMethods()) {
                total++;
                if (m.getName().equals("injectInputEvent")) {
                    found = m;
                    hits.add(sig(m));
                }
            }
        } catch (Throwable t) {
            return new Step("IInputManager 接口方法", false, null, brief(t), false);
        }
        if (found != null) {
            try { found.setAccessible(true); } catch (Throwable ignore) { /* 接口方法本来就可访问 */ }
            r.injectMethod = found;
        }
        return new Step("IInputManager 接口可见 injectInputEvent", found != null,
                "共 " + total + " 个方法；" + (found == null ? "未命中" : "命中: " + hits),
                null, false);
    }

    /** Proxy 的 declaredMethods（AIDL 生成的实现）。三项交叉对比才能定性。 */
    private static Step scanProxyMethods(Object proxy) {
        if (proxy == null) return new Step("Proxy 可见方法", false, "Proxy 没拿到", null, false);
        int total = 0;
        List<String> hits = new ArrayList<>();
        try {
            for (Method m : proxy.getClass().getDeclaredMethods()) {
                total++;
                if (m.getName().contains("inject")) hits.add(sig(m));
            }
        } catch (Throwable t) {
            return new Step("Proxy 可见方法", false, null, brief(t), false);
        }
        return new Step("Proxy 可见方法 contains(inject)", !hits.isEmpty(),
                "共 " + total + " 个；命中 " + hits.size() + (hits.isEmpty() ? "" : ": " + hits),
                null, false);
    }

    /** 方法签名简写，仅用于日志可读性。 */
    private static String sig(Method m) {
        StringBuilder sb = new StringBuilder(m.getName()).append('(');
        Class<?>[] ps = m.getParameterTypes();
        for (int i = 0; i < ps.length; i++) {
            if (i > 0) sb.append(',');
            sb.append(ps[i].getSimpleName());
        }
        return sb.append(')').toString();
    }

    /** 异常信息要带上**类名**，因为 ClassNotFoundException 与 SecurityException 的含义完全不同。 */
    public static String brief(Throwable t) {
        String msg = t.getMessage();
        return t.getClass().getSimpleName() + (msg == null ? "" : ": " + msg);
    }

    private static void emit(Report r) {
        for (Step s : r.steps) Log.i(TAG, s.toString() + (s.gate ? "" : "   (诊断)"));
        for (String n : r.notes) Log.i(TAG, "  · " + n);
        Log.i(TAG, "  ⇒ " + (r.allOk() ? "GATE_PASS" : "GATE_FAIL"));
    }
}
