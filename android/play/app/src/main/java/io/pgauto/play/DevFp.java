package io.pgauto.play;

/**
 * 设备指纹（G2）—— **纯函数，零 Android 依赖**。
 *
 * ★ 防什么：换设备 / 手动搬 prefs / 云恢复之后，旧设备的 `K`（每曲）与 `c`（每设备）、
 *   `lead` 全都不成立，但界面照样会把它们当有效值用 ⇒ 打一轮"参数看着都有、判定整体偏"。
 *   `allowBackup=false` 已经挡住主路径，这一条补的是边角（手动搬目录、adb backup 恢复）。
 *
 * ★ 为什么只比 机型 + 设备名 + 分辨率，**不碰** `Build.FINGERPRINT`：
 *   系统小版本更新会让 FINGERPRINT 变，但音频链路与判定滞后并没有变 ⇒ 那会逼用户白重标一次。
 *   分辨率取 `max×min`（横竖屏无关），避免旋转/方向判断差异造成假阳性。
 */
public final class DevFp {

    /** 比较结果：`SAME` 同机；`NONE` 没存过（首次运行，直接记下）；`DIFF` 换了设备。 */
    public static final int SAME = 0;
    public static final int NONE = 1;
    public static final int DIFF = 2;

    private DevFp() {}

    /** 拼指纹串。任何字段都先规范化（小写、去空白、把分隔符换掉），保证可比较。 */
    public static String of(String manufacturer, String model, String device, int w, int h) {
        return clean(manufacturer) + '|' + clean(model) + '|' + clean(device)
                + '|' + Math.max(w, h) + 'x' + Math.min(w, h);
    }

    /** 把字段压成不会破坏 `|` 分隔、也不会带空白的形态。null/空白都写成 `-`。 */
    public static String clean(String s) {
        if (s == null) return "-";
        StringBuilder b = new StringBuilder(s.length());
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            if (c == '|' || Character.isWhitespace(c) || Character.isISOControl(c)) {
                b.append('_');
            } else {
                b.append(Character.toLowerCase(c));
            }
        }
        String out = b.toString();
        // ★ 只剩分隔符（例如字段是三个空格 ⇒ "___"）说明这个字段其实没有信息：
        //   归一成 "-"，否则指纹里会出现 `|||` 这种看不出"有没有值"的形态。
        if (out.isEmpty() || out.matches("_+")) return "-";
        return out;
    }

    /**
     * 比较存盘指纹与当前指纹。
     *
     * ⚠ 调用方必须**区分** `NONE` 与 `DIFF`：`NONE` 是正常首跑（应当写入），
     *   `DIFF` 才是"参数作废"。把两者混成一个 boolean 会退化成"首次运行就报警"。
     */
    public static int compare(String stored, String current) {
        if (stored == null || stored.trim().isEmpty()) return NONE;
        if (current == null || current.trim().isEmpty()) return NONE;   // 当前拿不到 ⇒ 不冤判
        return stored.trim().equalsIgnoreCase(current.trim()) ? SAME : DIFF;
    }
}
