package io.pgauto.play;

import java.io.ByteArrayInputStream;
import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.util.ArrayList;
import java.util.List;

/**
 * {@link PayloadCache} 的自测（纯 JVM，无 Android 依赖）。
 *
 * ★ 为什么这份自测必须存在：缓存判据错了**不会报任何错**，只会让设备静默跑旧载荷。
 *   10-24 run5/run6 的 60~69% Miss 就是这么来的（`canvas` 换了口径但字节数没变，
 *   长度判据放行旧缓存 ⇒ 每一下触摸都压进屏幕中间一块）。
 *   所以这里盯的不是"能把文件写出去"，而是三类会让这个 bug 复发的具体写法：
 *   ① 拿长度当内容（t02/t19）② 缓存坏掉时把开火搞死而不是自愈（t08/t09/t16/t17/t18）
 *   ③ 该重写时不重写、不该重写时乱写（t14 用 mtime 钉住"命中必须不落盘"）。
 */
public final class PayloadCacheSelfTest {

    private static int pass, fail;

    public static void main(String[] args) throws Exception {
        t01_sameContentIsNotStale();
        t02_sameLengthDifferentContentIsStale();
        t03_differentLengthIsStale();
        t04_nullCachedIsStale();
        t05_nullAssetIsStale();
        t06_emptyVsEmptyIsNotStale();
        t07_emptyVsNonEmptyIsStale();
        t08_readOrNullMissingFile();
        t09_readOrNullDirectory();
        t09b_readOrNullUnreadableFile();
        t10_readOrNullRoundTrip();
        t11_sha256KnownVectors();
        t12_sha256Format();
        t13_ensureWritesOnFirstRun();
        t14_ensureHitsCacheWithoutRewriting();
        t15_ensureRewritesOnSameLengthChange();
        t16_ensureRewritesTruncatedCache();
        t17_ensureRewritesEmptyCache();
        t18_ensureHealsDirectoryInTheWay();

        System.out.println("PayloadCacheSelfTest: " + pass + " 通过, " + fail + " 失败");
        if (fail > 0) throw new AssertionError("PayloadCache 自测失败 " + fail + " 项");
    }

    // ─────────────────────────── 判据 ───────────────────────────

    private static void t01_sameContentIsNotStale() {
        byte[] a = bytes("{\"canvas\":{\"width\":1920.0}}");
        ok(!PayloadCache.isStale(a.clone(), a), "内容相同 ⇒ 不得判为过期");
    }

    private static void t02_sameLengthDifferentContentIsStale() {
        // ★ 本次事故的形状：两个数值互换位置，长度一个字都不差
        byte[] good = bytes("{\"width\":1920.0,\"height\":1080.0}");
        byte[] bad = bytes("{\"width\":1080.0,\"height\":2340.0}");
        ok(good.length == bad.length, "夹具前提：两份长度必须相等，实得 " + good.length + " vs " + bad.length);
        ok(PayloadCache.isStale(bad, good), "等长但内容不同 ⇒ 必须判为过期");
        ok(PayloadCache.isStale(good, bad), "反向也必须判为过期（不能只查一侧）");
    }

    private static void t03_differentLengthIsStale() {
        ok(PayloadCache.isStale(bytes("abc"), bytes("abcd")), "长度不同 ⇒ 过期");
    }

    private static void t04_nullCachedIsStale() {
        ok(PayloadCache.isStale(null, bytes("x")), "没有缓存 ⇒ 过期（不许把 null 当相同）");
    }

    private static void t05_nullAssetIsStale() {
        ok(PayloadCache.isStale(bytes("x"), null), "asset 为空引用 ⇒ 过期（宁可重写也不要用未知内容）");
        ok(PayloadCache.isStale(null, null), "两边都 null ⇒ 过期");
    }

    private static void t06_emptyVsEmptyIsNotStale() {
        ok(!PayloadCache.isStale(new byte[0], new byte[0]), "两个空数组内容相同 ⇒ 不过期");
    }

    private static void t07_emptyVsNonEmptyIsStale() {
        // 空缓存文件是"写到一半被打断"的典型形态
        ok(PayloadCache.isStale(new byte[0], bytes("x")), "空缓存 + 非空 asset ⇒ 过期");
    }

    // ─────────────────────────── 读缓存 ───────────────────────────

    private static void t08_readOrNullMissingFile() throws Exception {
        File dir = tmpDir();
        ok(PayloadCache.readOrNull(new File(dir, "nope.json")) == null, "文件不存在 ⇒ null");
        ok(PayloadCache.readOrNull(null) == null, "null 路径 ⇒ null");
    }

    private static void t09_readOrNullDirectory() throws Exception {
        File dir = tmpDir();
        ok(PayloadCache.readOrNull(dir) == null, "路径是目录 ⇒ null（不许抛）");
    }

    private static void t10_readOrNullRoundTrip() throws Exception {
        File dir = tmpDir();
        byte[] data = bytes("hello 载荷");
        File f = new File(dir, "a.json");
        write(f, data);
        byte[] got = PayloadCache.readOrNull(f);
        ok(got != null && java.util.Arrays.equals(got, data), "读回内容必须逐字节相等");
    }

    /**
     * ★ 这条是补出来的：第一轮变异（把 `catch IOException` 改成抛异常）**逃逸**了 ——
     *   t08 是「文件不存在」、t09 是「路径是目录」，两者都在 `isFile()` 就提前返回，
     *   根本走不到 catch。也就是说：当时「读失败要自愈」这个承诺**没有任何用例在看着**。
     *
     * 造法说明：本机（管理员态）`setReadable(false)` **拦不住** FileInputStream（实测
     * `canRead=false` 却仍能读出首字节），所以没法用真实权限造。这里用 File 的匿名子类
     * 精确表达「`isFile()` 说能读、实际 open 失败」这一状态 —— 测的是 catch 分支的**契约**
     * （返回 null、不抛），不是在测文件系统。
     */
    private static void t09b_readOrNullUnreadableFile() throws Exception {
        File dir = tmpDir();
        File ghost = new File(dir, "ghost.json") {
            @Override
            public boolean isFile() {
                return true;      // 骗过前置检查，让 FileInputStream 真的去 open
            }
        };
        byte[] got;
        try {
            got = PayloadCache.readOrNull(ghost);
        } catch (RuntimeException e) {
            ok(false, "读失败的缓存不许把异常抛给开火流程，实际抛了 " + e);
            return;
        }
        ok(got == null, "读失败 ⇒ 必须返回 null（上层据此判过期并重写，自愈）");
    }


    private static void t11_sha256KnownVectors() {
        ok("e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
                .equals(PayloadCache.sha256Hex(new byte[0])), "空串的 sha256 必须等于公认值");
        ok("ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
                .equals(PayloadCache.sha256Hex(bytes("abc"))), "\"abc\" 的 sha256 必须等于公认值");
    }

    private static void t12_sha256Format() {
        String h = PayloadCache.sha256Hex(bytes("任意内容"));
        ok(h.length() == 64, "必须是全 64 位（截断后无法与装机 asset 直接比对），实得 " + h.length());
        ok(h.equals(h.toLowerCase(java.util.Locale.ROOT)), "必须全小写");
        ok(h.matches("[0-9a-f]{64}"), "必须是 hex，实得 " + h);
    }

    // ─────────────────────────── 落盘 ───────────────────────────

    private static void t13_ensureWritesOnFirstRun() throws Exception {
        File dir = tmpDir();
        List<String> logs = new ArrayList<>();
        byte[] asset = bytes("第一版");
        File f = PayloadCache.ensure(new File(dir, "payload"), "k", "payload/k.json",
                stream(asset), logs::add);
        ok(f.isFile(), "首次必须落盘");
        ok(java.util.Arrays.equals(read(f), asset), "落盘内容必须逐字节等于 asset");
        ok(logs.size() == 1 && logs.get(0).contains("source=asset"),
                "日志必须标 source=asset，实得 " + logs);
        ok(logs.get(0).contains(PayloadCache.sha256Hex(asset)), "日志指纹必须是这份内容的 sha256");
    }

    private static void t14_ensureHitsCacheWithoutRewriting() throws Exception {
        File dir = tmpDir();
        byte[] asset = bytes("同一版");
        List<String> logs = new ArrayList<>();
        File f = PayloadCache.ensure(dir, "k", "payload/k.json", stream(asset), logs::add);
        // ★ 用 mtime 当外部判据：若实现偷偷重写，mtime 会跳到 20 ms 之后。
        //   只查日志"说"source=cache 是空转 —— 那句话是实现在自证。
        Thread.sleep(20);
        long before = f.lastModified();
        logs.clear();
        PayloadCache.ensure(dir, "k", "payload/k.json", stream(asset), logs::add);
        ok(f.lastModified() == before, "内容相同 ⇒ 必须不落盘（mtime 变了就是真写了）");
        ok(logs.size() == 1 && logs.get(0).contains("source=cache"),
                "日志必须标 source=cache，实得 " + logs);
    }

    private static void t15_ensureRewritesOnSameLengthChange() throws Exception {
        // ★ 回归本次事故：等长、内容不同的新版 asset 必须覆盖旧缓存
        File dir = tmpDir();
        byte[] v1 = bytes("{\"width\":1080.0,\"height\":2340.0}");
        byte[] v2 = bytes("{\"width\":1920.0,\"height\":1080.0}");
        ok(v1.length == v2.length, "夹具前提：两份等长");
        PayloadCache.ensure(dir, "k", "payload/k.json", stream(v1), null);
        List<String> logs = new ArrayList<>();
        PayloadCache.ensure(dir, "k", "payload/k.json", stream(v2), logs::add);
        ok(java.util.Arrays.equals(read(new File(dir, "k.json")), v2),
                "等长新版必须覆盖旧缓存（这就是 run5/run6 没做到的那一步）");
        ok(logs.size() == 1 && logs.get(0).contains("source=asset"), "必须报落盘");
    }

    private static void t16_ensureRewritesTruncatedCache() throws Exception {
        File dir = tmpDir();
        byte[] asset = bytes("完整内容 1234567890");
        File f = new File(dir, "k.json");
        write(f, java.util.Arrays.copyOf(asset, 5));      // 旧缓存被截断
        PayloadCache.ensure(dir, "k", "payload/k.json", stream(asset), null);
        ok(java.util.Arrays.equals(read(f), asset), "截断的缓存必须被完整重写");
    }

    private static void t17_ensureRewritesEmptyCache() throws Exception {
        File dir = tmpDir();
        byte[] asset = bytes("x");
        File f = new File(dir, "k.json");
        write(f, new byte[0]);
        PayloadCache.ensure(dir, "k", "payload/k.json", stream(asset), null);
        ok(java.util.Arrays.equals(read(f), asset), "空缓存必须被重写");
    }

    private static void t18_ensureHealsDirectoryInTheWay() throws Exception {
        // 极端形态：同名路径是个目录（历史版本 bug / 手工折腾留下的）
        File dir = tmpDir();
        File f = new File(dir, "k.json");
        ok(f.mkdirs(), "夹具前提：先造一个同名目录");
        byte[] asset = bytes("heal");
        PayloadCache.ensure(dir, "k", "payload/k.json", stream(asset), null);
        ok(f.isFile(), "必须把挡路的目录清掉并落盘");
        ok(java.util.Arrays.equals(read(f), asset), "自愈后内容必须正确");
    }

    // ─────────────────────────── 工具 ───────────────────────────

    private static File tmpDir() throws IOException {
        File d = File.createTempFile("pgcache", "");
        if (!d.delete() || !d.mkdirs()) throw new IOException("建不了临时目录 " + d);
        d.deleteOnExit();
        return d;
    }

    private static ByteArrayInputStream stream(byte[] b) {
        return new ByteArrayInputStream(b);
    }

    private static byte[] bytes(String s) {
        return s.getBytes(java.nio.charset.StandardCharsets.UTF_8);
    }

    private static void write(File f, byte[] data) throws IOException {
        try (FileOutputStream fo = new FileOutputStream(f)) {
            fo.write(data);
        }
    }

    private static byte[] read(File f) throws IOException {
        byte[] b = PayloadCache.readOrNull(f);
        return b == null ? new byte[0] : b;
    }

    private static void ok(boolean cond, String msg) {
        if (cond) {
            pass++;
        } else {
            fail++;
            System.out.println("  ✗ " + msg);
        }
    }
}
