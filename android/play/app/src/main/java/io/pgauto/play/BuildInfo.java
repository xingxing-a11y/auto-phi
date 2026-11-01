package io.pgauto.play;

/**
 * 构建自证（G13）—— **纯函数，零 Android 依赖**。
 *
 * ★ 为什么需要它（2026-10-25 19:2x 的事故）：为了排除变量连着出了好几个安装包，最后"变量都不是原因"、
 *   把包回退了 —— 但 `build.sh` 里的 `versionName` 是**写死的 `0.1-m1s1d`**，
 *   装到手机上之后 `dumpsys`、界面、trace **全都认不出这是哪一版代码**，
 *   而 `lastUpdateTime` 只记"安装时刻"、不记"构建时刻"。⇒ 只能靠回忆，正是本项目最贵的 bug 类型。
 *
 * ★ 为什么是 `key=value` 行而不是 JSON：端内解析 JSON 要 `org.json`（Android 自带，但那样这个类
 *   就不能进"无 android.jar"的 JVM 自测）。构建信息本来就只是几个标量，行格式足够且可重放。
 *
 * ★ 缺失时**必须**回 `unknown`，不许伪装成"旧版本号"：界面上要能一眼看出"这包装的时候没带指纹"。
 */
public final class BuildInfo {

    /** 读不到 `build_info.txt` 时的取值（手搓的包、旧包、selftest 里都会走到）。 */
    public static final BuildInfo UNKNOWN = new BuildInfo("", "", "", "", false);

    public final String version;      // 例：0.1-m1s1d
    public final String rev;          // 例：0d8f527
    public final String versionFull;  // 例：0.1-m1s1d+g0d8f527（写进 AndroidManifest 的那个）
    public final String built;        // 例：2026-10-25T19:40:00+0800
    public final boolean dirty;       // 构建时工作树有已跟踪改动

    private BuildInfo(String version, String rev, String versionFull, String built,
                      boolean dirty) {
        this.version = version == null ? "" : version;
        this.rev = rev == null ? "" : rev;
        this.versionFull = versionFull == null ? "" : versionFull;
        this.built = built == null ? "" : built;
        this.dirty = dirty;
    }

    /**
     * 解析 `key=value` 行（`#` 开头与空行忽略，未知键忽略，绝不抛）。
     *
     * @param text 文件内容；null/空 ⇒ {@link #UNKNOWN}
     */
    public static BuildInfo parse(String text) {
        if (text == null || text.trim().isEmpty()) return UNKNOWN;
        String version = "", rev = "", full = "", built = "";
        boolean dirty = false;
        for (String raw : text.split("\n")) {
            String line = raw.trim();
            if (line.isEmpty() || line.startsWith("#")) continue;
            int eq = line.indexOf('=');
            if (eq <= 0) continue;
            String k = line.substring(0, eq).trim();
            String v = line.substring(eq + 1).trim();
            switch (k) {
                case "version": version = v; break;
                case "rev": rev = v; break;
                case "version_full": full = v; break;
                case "built": built = v; break;
                case "dirty": dirty = "1".equals(v) || "true".equalsIgnoreCase(v); break;
                default: break;   // 未知键忽略：以后加字段不会让老解析器炸
            }
        }
        if (version.isEmpty() && full.isEmpty() && rev.isEmpty()) return UNKNOWN;
        return new BuildInfo(version, rev, full, built, dirty);
    }

    /**
     * 从流读并解析（端内 `assets/build_info.txt` 与 selftest 共用**同一份**实现）。
     *
     * ⛔ 读不到 / 读一半就抛 ⇒ 一律 {@link #UNKNOWN}：构建信息缺失是"这包没带指纹"，
     *   不是"算出来的版本号为空"，两者必须在界面上长得不一样。
     */
    public static BuildInfo read(java.io.InputStream in) {
        if (in == null) return UNKNOWN;
        try (java.io.InputStream i = in) {
            java.io.ByteArrayOutputStream bo = new java.io.ByteArrayOutputStream(256);
            byte[] buf = new byte[512];
            int r;
            while ((r = i.read(buf)) > 0) bo.write(buf, 0, r);
            return parse(new String(bo.toByteArray(), java.nio.charset.StandardCharsets.UTF_8));
        } catch (Throwable t) {
            return UNKNOWN;
        }
    }

    /** 一行短串，给横幅 / trace 头用。⛔ 不许带空格（trace 头是 `k=v k=v` 空格分隔）。 */
    public String brief() {
        if (!versionFull.isEmpty()) return versionFull;
        if (version.isEmpty() && rev.isEmpty()) return "unknown";
        StringBuilder b = new StringBuilder(version.isEmpty() ? "unknown" : version);
        if (!rev.isEmpty()) b.append("+g").append(rev);
        if (dirty) b.append("+dirty");
        return b.toString();
    }

    public boolean known() {
        return !(versionFull.isEmpty() && version.isEmpty() && rev.isEmpty());
    }

    @Override public String toString() {
        return "BuildInfo{" + brief() + (built.isEmpty() ? "" : " built=" + built) + "}";
    }
}
