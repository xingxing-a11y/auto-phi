package io.pgauto.play;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.Arrays;
import java.util.function.Consumer;

/**
 * 载荷缓存：`assets/payload/<name>.json` → `files/payload/<key>.json`。
 *
 * ★ 为什么要有这个类（2026-10-24，FACTS §161）：
 *   原先 `PlayService` 与 `TouchProbeActivity` **各有一份** `cachePayload`，判据都是
 *   `f.length() != raw.length`。而把载荷的 `canvas` 从 `1080x2340` 改成 `1920x1080`
 *   **不改任何字节的长度**（`"1920.0"→"1080.0"`、`"1080.0"→"2340.0"` 都是 6 字符，
 *   整份仍 229222 B）⇒ 等长的载荷修正被静默吞掉，设备永远跑旧缓存。
 *   后果：run5/run6 每一下触摸都压进屏幕中间一块（1675/1682 关键帧位置错，|Δ| 中位 233 px），
 *   成绩 40.73% / 31.47%，而**所有既有判据都是绿的**。
 *   ⇒ 判据必须是「内容」，且**只能有一份实现**（项目规矩 9：同一个量两处实现必然分叉）。
 *
 * ★ 为什么用 `Arrays.equals` 而不是比 sha256：
 *   两边字节本来就已经在内存里，`equals` 在第一个差异处就返回，比走一遍 SHA-256 更快，
 *   而且没有碰撞概率。sha256 **只**用于日志指纹（人读的短标识），不参与判定。
 *
 * ★ 为什么本类零 Android 依赖：`android/play/selftest.sh` 用**没有 android.jar** 的
 *   classpath 编译它 ⇒ 一旦哪天有人在这里写 `Log.i` / `Context.getAssets` 立刻编不过。
 *   I/O 留在调用方：asset 的 `InputStream` 由调用方给，日志经 `Consumer` 回传。
 */
public final class PayloadCache {

    private PayloadCache() {
    }

    /**
     * 缓存是否需要重写。`cached == null`（没有缓存 / 读不出来 / 是目录）一律视为过期。
     *
     * ★ 唯一判据：**逐字节相等**。长度相等不算相等 —— 这正是本次事故的形态。
     */
    public static boolean isStale(byte[] cached, byte[] asset) {
        if (cached == null || asset == null) return true;
        return !Arrays.equals(cached, asset);
    }

    /**
     * 读缓存文件内容。不存在、是目录、读失败**一律返回 null（不抛）**。
     *
     * ★ 为什么不抛：一个坏掉的缓存文件若让开火直接失败，用户就只能 `pm clear`。
     *   返回 null ⇒ 上层判为过期 ⇒ 重写覆盖，自愈。
     */
    public static byte[] readOrNull(File f) {
        if (f == null || !f.isFile()) return null;
        try (InputStream in = new FileInputStream(f)) {
            return readAll(in);
        } catch (IOException e) {
            return null;
        }
    }

    /** 小写 hex、全 64 位。**仅供日志指纹**，不参与任何判定。 */
    public static String sha256Hex(byte[] data) {
        if (data == null) return "null";
        try {
            byte[] h = MessageDigest.getInstance("SHA-256").digest(data);
            StringBuilder sb = new StringBuilder(h.length * 2);
            for (byte b : h) {
                sb.append(Character.forDigit((b >> 4) & 0xF, 16));
                sb.append(Character.forDigit(b & 0xF, 16));
            }
            return sb.toString();
        } catch (NoSuchAlgorithmException e) {
            // Android 必备 SHA-256；真缺了也不该因此拒绝开火（指纹不是判据）
            return "unavailable";
        }
    }

    /** 不能用 `InputStream.readAllBytes`（API 33+）：min-sdk 29 会 NoSuchMethodError。 */
    public static byte[] readAll(InputStream in) throws IOException {
        ByteArrayOutputStream bo = new ByteArrayOutputStream(256 * 1024);
        byte[] b = new byte[16 * 1024];
        int n;
        while ((n = in.read(b)) > 0) bo.write(b, 0, n);
        return bo.toByteArray();
    }

    /**
     * 确保 `dir/<key>.json` 与 asset 内容一致，返回该文件。**唯一落盘实现**。
     *
     * @param assetName 仅用于日志（`assets/payload/xxx.json`）
     * @param assetIn   asset 的输入流，由调用方 open（本类不认识 AssetManager）
     * @param log       日志回调，可为 null；收到一行「落盘」或「命中」
     */
    public static File ensure(File dir, String key, String assetName,
                              InputStream assetIn, Consumer<String> log) throws IOException {
        if (dir == null || key == null) throw new IllegalArgumentException("dir/key 不能为空");
        if (!dir.isDirectory() && !dir.mkdirs()) {
            throw new IllegalStateException("建不了目录 " + dir);
        }
        byte[] raw = readAll(assetIn);
        File f = new File(dir, key + ".json");

        byte[] cached = readOrNull(f);
        if (cached != null && !isStale(cached, raw)) {
            if (log != null) {
                log.accept("载荷命中缓存 " + f.getName() + "（" + raw.length + " B，source=cache，sha256="
                        + sha256Hex(raw) + "）");
            }
            return f;
        }

        // 同名路径不是普通文件（目录 / 悬空符号链接）⇒ 先清掉，否则 write 必失败
        if (f.exists() && !f.isFile() && !f.delete()) {
            throw new IOException("载荷缓存路径不是普通文件且删不掉：" + f);
        }
        try (FileOutputStream fo = new FileOutputStream(f)) {
            fo.write(raw);
        }
        if (log != null) {
            log.accept("载荷落盘 " + f.getName() + "（" + raw.length + " B，source=asset，sha256="
                    + sha256Hex(raw) + "，来自 assets/" + assetName + "）");
        }
        return f;
    }
}
