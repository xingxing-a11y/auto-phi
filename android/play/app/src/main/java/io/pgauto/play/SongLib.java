package io.pgauto.play;

import android.content.Context;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;

/**
 * 端内曲库：读 APK `assets/manifest.json`（PC 侧由 `tools/pack_assets.py` 生成）。
 *
 * ★ 为什么必需字段缺失要**抛异常**而不是 `optDouble(默认 0)`：
 *   本项目的教训 11 —— 「素材缺失 ≠ 读数合格」。`sil_ref_s` 若被静默读成 0，
 *   零点公式 `zero = onset − sil_ref − L` 会整体提前 0.86 s，1286 个音符全 Miss，
 *   而界面上一切正常、日志里没有一个"错"字。缺字段必须在**读的那一刻**就炸。
 *
 * ★ `parse(String)` 是纯函数（只依赖 org.json），`load(Context)` 才碰 assets。
 *   拆开是为了 PC 侧能对字段契约做静态核对（`tests/test_play_assets_contract.py`
 *   从本文件抽 {@link #REQUIRED} 与 {@link #OPTIONAL}，反过来断言 manifest 里有这些键）。
 */
public final class SongLib {

    public static final String ASSET = "manifest.json";

    /**
     * 缺一个就抛的字段。**加字段必须先让 `tools/pack_assets.py` 生成它**，
     * 否则装机一进界面就崩 —— 这是故意的（比静默 0 好）。
     */
    public static final String[] REQUIRED = {
            "key", "name", "diff", "payload",
            "note_count", "n_events", "n_key", "sil_ref_s", "t_ms_range",
    };

    /** 可以没有的字段（缺失时用页面上的占位符，不参与任何数值计算）。 */
    public static final String[] OPTIONAL = {
            "level", "song_dir", "chart", "bytes_slim", "sil_confirmed", "tap_lead_ms", "sil_thr",
    };

    /** 一首已打包的曲目。全部字段 final ⇒ 界面线程只读，无同步问题。 */
    public static final class Song {
        public final String key;
        public final String name;
        public final String diff;
        /** assets 内的相对路径，如 `payload/bounded_in.json`。 */
        public final String payload;
        public final int noteCount;
        public final int nEvents;
        /** 关键事件（DOWN/UP/POINTER_*）条数，端内 `Scheduler.Cmd.key()` 统计口径。 */
        public final int nKey;
        public final double silRefS;
        public final double firstTMs;
        public final double lastTMs;
        /** 难度等级；**-1 表示未知**（PC 侧无曲库索引时不编数据）。 */
        public final int level;
        /**
         * `zero=tap` 用的 lead（点击 ▶ → 谱面 t=0 的间隔，ms）。**0 表示本曲没测过**。
         *
         * ★ 允许"没有"而不是给个默认值：lead 是**曲目属性**（跨曲差 300+ ms），
         *   编一个默认值等于用别的曲的常数打分 ⇒ 整轮偏而日志全绿。
         *   0 会被 `TapLead.resolve` 当场拒绝（消息提示改用 onset）。
         */
        public final double tapLeadMs;
        /**
         * 母带的「静音 → 有声」边沿阈值（RMS 幅度，母带单位）。**0 = 未标定**。
         *
         * ★ 为什么必须有它（2026-10-25，FACTS §169）：`sil_ref_s` 是「母带上超过这个阈值」
         *   的那一刻，端内若用另一套判据（5×背景）找边沿，两侧就把边沿定义在了不同位置 ——
         *   硬起音曲看不出来，渐入曲能差几百毫秒（bounded_in：母带 0.865 s，相对判据 ~1.7 s）。
         *   ⛔ 0 时**退回相对判据**，不许编一个默认阈值（那是拿别的曲的常数打分）。
         */
        public final double silThr;
        /**
         * 母带包络指纹（base64 的 u8 序列，10 ms 一帧，前 30 s）。**"" = 没有指纹**。
         *
         * ★ 标定要用它与捕获包络做互相关，算出「每曲的 K」（FACTS §173）。
         * ⛔ 空串时标定只能落盘、不能在端内算 K —— 不许编一个默认指纹。
         */
        public final String envB64;
        /** 指纹帧长（ms）。没指纹时为 0。 */
        public final int envHopMs;
        /**
         * 首音盲区里**打不到**的关键事件数（0 = 这首能 AP）。
         *
         * ★ 来源：`pack_assets` 用「音乐起音 + 0.342 s」逐曲算出来的真实个数。
         * ⛔ 别在这里重算：盲区宽度是端内链路属性，改了这里就会和打包端不一致。
         */
        public final int blindLost;

        /**
         * 这首谱**并发最多要几根手指**（打包端从出货载荷统计的峰值）。
         *
         * ★ 2026-10-26：全库只有 6 首 ≥9（`distort_at` 到 10）。到 9~10 时调度器会开始
         *   "抢正在长按的手指" ⇒ 表现为偶发纯 Miss（0 Good/0 Bad），**这是结构限制不是 bug**。
         *   UI 据此提示"φ 概率低"，避免把结构问题当故障去追。
         */
        public final int pointerPeak;

        /**
         * 本曲的**预置 K**（ms）；NaN = 没有预置。
         *
         * 只在"自动标定测不出 K"时兜底（`chronostasis_in` 这类 30 s 指纹太平的曲子）。
         * 依据：`K` 是**曲目**属性（起音形状决定的检测器偏置），`c` 才是设备属性。
         * 自动标定一旦测出真值就覆盖它（预置值不参与 `CalibMerge` 平均）。
         */
        public final double kPresetMs;

        /**
         * 本曲虽 `pointerPeak >= 9`，但**已被真机证明能 AP** ⇒ UI 不再打「结构限制」警告。
         *
         * ★ 2026-10-28：来源是 manifest 的 `ten_finger_ok`（打包端按 `TEN_FINGER_VERIFIED` 写）。
         *   判定口径见 `tools/pack_assets.py` 的同名常量：真机 φ + trace 留档才入册。
         */
        public final boolean tenFingerOk;

        Song(String key, String name, String diff, String payload,
             int noteCount, int nEvents, int nKey, double silRefS,
             double firstTMs, double lastTMs, int level, double tapLeadMs, double silThr,
             String envB64, int envHopMs, int blindLost, int pointerPeak, double kPresetMs,
             boolean tenFingerOk) {
            this.key = key;
            this.name = name;
            this.diff = diff;
            this.payload = payload;
            this.noteCount = noteCount;
            this.nEvents = nEvents;
            this.nKey = nKey;
            this.silRefS = silRefS;
            this.firstTMs = firstTMs;
            this.lastTMs = lastTMs;
            this.level = level;
            this.tapLeadMs = tapLeadMs;
            this.silThr = silThr;
            this.envB64 = envB64 == null ? "" : envB64;
            this.envHopMs = envHopMs;
            this.blindLost = blindLost;
            this.pointerPeak = pointerPeak;
            this.kPresetMs = kPresetMs;
            this.tenFingerOk = tenFingerOk;
        }

        /** 母带指纹解成 RMS 数组（无指纹 ⇒ 抛，调用方据此回退）。 */
        public double[] envRms() {
            return EnvCodec.rmsFromBase64(envB64);
        }

        /** 列表第一行：`Bounded Quietude`。 */
        public String title() {
            return name;
        }

        /** 列表第二行：`IN 16 · 1286 音符 · 静音 0.865s`。 */
        public String subtitle() {
            StringBuilder sb = new StringBuilder();
            sb.append(diff);
            if (level > 0) sb.append(' ').append(level);
            sb.append(" · ").append(noteCount).append(" 音符")
              .append(" · ").append(String.format(java.util.Locale.ROOT, "静音 %.3fs", silRefS));
            return sb.toString();
        }

        /** 详情区多行文本（选中后显示）。 */
        public String detail() {
            return String.format(java.util.Locale.ROOT,
                    "key      %s\n"
                            + "载荷     %s\n"
                            + "事件     %d 条（关键 %d）\n"
                            + "时间     %.3f ~ %.3f s\n"
                            + "sil_ref  %.6f s\n"
                            + "边沿     %s\n"
                            + "tap lead %s\n"
                            + "零点     zero = onset − sil_ref − L_dev",
                    key, payload, nEvents, nKey,
                    firstTMs / 1000.0, lastTMs / 1000.0, silRefS,
                    silThr > 0
                            ? String.format(java.util.Locale.ROOT,
                                    "sil_thr %.1f RMS（连续 %d ms，与母带同口径）",
                                    silThr, OnsetGate.DEFAULT_SUSTAIN_MS)
                            : "未标定 ⇒ 相对判据（5×背景）",
                    tapLeadMs > 0
                            ? String.format(java.util.Locale.ROOT, "%.3f ms", tapLeadMs)
                            : "未标定（tap 零点在本题上会拒绝）");
        }
    }

    private SongLib() {}

    /**
     * 同一首歌的若干**已打包难度**。
     *
     * ★ 分组键用 `name` 而不是 `key`：`key` 是 `<短名>_<难度>`（`bounded_in`、`credits_ez`），
     *   天然把难度编进了键里；用户在界面上先选歌、再选难度，所以必须按歌聚合。
     * ★ 不用 `song_dir` 当键：它是 PC 侧的目录名（`Credits.Frums.0`），带作者后缀，
     *   同一首歌如果换了打包来源就会变成两个组。`name` 由目录名剥离作者段得到，更稳。
     */
    public static final class Group {
        public final String name;
        private final List<Song> variants = new ArrayList<>();

        Group(String name) { this.name = name; }

        public List<Song> variants() { return Collections.unmodifiableList(variants); }

        /** 列表第二行：单难度直接说清楚，多难度报个数。 */
        public String subtitle() {
            if (variants.size() == 1) {
                Song s = variants.get(0);
                return s.diff + (s.level > 0 ? " " + s.level : "") + " · " + s.noteCount + " 音符";
            }
            int notes = 0;
            for (Song s : variants) notes += s.noteCount;
            return variants.size() + " 个难度 · 共 " + notes + " 音符";
        }
    }

    /** 难度排序权重：EZ < HD < IN < AT < 其他。 */
    private static int diffRank(String diff) {
        switch (diff == null ? "" : diff.toUpperCase(java.util.Locale.ROOT)) {
            case "EZ": return 0;
            case "HD": return 1;
            case "IN": return 2;
            case "AT": return 3;
            default: return 9;
        }
    }

    /**
     * 按曲名分组：**曲名不区分大小写升序**；同曲的难度按 EZ&lt;HD&lt;IN&lt;AT 再按等级升序。
     *
     * ★ 曲名排序而不是按 key 排：assets 里的顺序是打包时的（按 key 字典序），
     *   对用户没有意义（`bounded_in` 排第一只是因为它字母序在前）。用户找歌按名字找。
     */
    public static List<Group> group(List<Song> songs) {
        java.util.Map<String, Group> byName = new java.util.LinkedHashMap<>();
        for (Song s : songs) {
            Group g = byName.get(s.name);
            if (g == null) {
                g = new Group(s.name);
                byName.put(s.name, g);
            }
            g.variants.add(s);
        }
        List<Group> out = new ArrayList<>(byName.values());
        out.sort((a, b) -> String.CASE_INSENSITIVE_ORDER.compare(a.name, b.name));
        for (Group g : out) {
            g.variants.sort((a, b) -> {
                int d = Integer.compare(diffRank(a.diff), diffRank(b.diff));
                if (d != 0) return d;
                // level 未知（-1）排在已知之后，但不影响同难度的先后
                int la = a.level < 0 ? Integer.MAX_VALUE : a.level;
                int lb = b.level < 0 ? Integer.MAX_VALUE : b.level;
                return Integer.compare(la, lb);
            });
        }
        return Collections.unmodifiableList(out);
    }

    /** 读 assets 并解析。IO/格式错误一律抛出，由界面显示红字而不是空列表。 */
    /** key → 母带总时长（秒）。课程模式算"下一首的下界"要用它（音乐结束，不是末音符）。 */
    private static final java.util.Map<String, Double> LEN = new java.util.HashMap<>();

    public static double lengthOf(String key) {
        Double v = LEN.get(key);
        return v == null ? 0.0 : v;
    }

    public static List<Song> load(Context ctx) throws Exception {
        return parse(readAsset(ctx, ASSET));
    }

    /** 纯函数：manifest.json 文本 → 曲目列表。顺序与文件里一致（PC 侧已按 key 排好）。 */
    public static List<Song> parse(String json) throws Exception {
        JSONObject root = new JSONObject(json);
        if (!root.has("version")) {
            throw new IllegalStateException("manifest.json 缺 version（不是 pack_assets.py 生成的？）");
        }
        JSONArray arr = root.optJSONArray("songs");
        if (arr == null) throw new IllegalStateException("manifest.json 缺 songs 数组");
        if (arr.length() == 0) throw new IllegalStateException("manifest.json 的 songs 为空");

        List<Song> out = new ArrayList<>(arr.length());
        for (int i = 0; i < arr.length(); i++) {
            JSONObject o = arr.getJSONObject(i);
            for (String k : REQUIRED) {
                if (!o.has(k) || o.isNull(k)) {
                    throw new IllegalStateException(
                            "manifest.json songs[" + i + "] 缺必需字段 " + k
                                    + "（key=" + o.optString("key", "?") + "）");
                }
            }
            JSONArray tr = o.getJSONArray("t_ms_range");
            if (tr.length() != 2) {
                throw new IllegalStateException(
                        "songs[" + i + "].t_ms_range 应为 2 个元素，实际 " + tr.length());
            }
            // level 允许为 null（PC 侧没有曲库索引时不编造），用 -1 表示未知
            int lv = o.isNull("level") ? -1 : o.optInt("level", -1);
            // tap_lead_ms 允许为 null（本曲没测过 tap 的 lead）⇒ 用 0 表示"无"。
            // ⛔ 这里**不许**编默认值：0 会让 TapLead.resolve 当场拒绝，那才是正确行为。
            double tapLead = o.isNull("tap_lead_ms") ? 0.0 : o.optDouble("tap_lead_ms", Double.NaN);
            if (!(tapLead >= 0)) {
                throw new IllegalStateException(
                        "songs[" + i + "].tap_lead_ms 非法：" + tapLead
                                + "（应为 null 或正数毫秒）");
            }
            // sil_thr 允许为 null（母带边沿没算出来时 pack_assets 会留空）⇒ 用 0 表示"未标定"。
            // ⛔ 出现但 ≤0 / NaN = 生成端算错，必须炸：静默当 0 会悄悄退回相对判据，
            //   而「悄悄退回」正是 §169 那个几百毫秒偏差的成因。
            double silThr = 0.0;
            if (!o.isNull("sil_thr")) {
                silThr = o.optDouble("sil_thr", Double.NaN);
                if (!(silThr > 0) || Double.isInfinite(silThr)) {
                    throw new IllegalStateException(
                            "songs[" + i + "].sil_thr 非法：" + silThr
                                    + "（应为 null 或正数 RMS）");
                }
            }
            LEN.put(o.getString("key"), o.optDouble("length_s", 0.0));
            out.add(new Song(
                    o.getString("key"),
                    o.getString("name"),
                    o.getString("diff"),
                    o.getString("payload"),
                    o.getInt("note_count"),
                    o.getInt("n_events"),
                    o.getInt("n_key"),
                    o.getDouble("sil_ref_s"),
                    tr.getDouble(0),
                    tr.getDouble(1),
                    lv,
                    tapLead,
                    silThr,
                    o.isNull("env_b64") ? "" : o.optString("env_b64", ""),
                    o.isNull("env_hop_ms") ? 0 : o.optInt("env_hop_ms", 0),
                    o.isNull("blind_lost") ? 0 : o.optInt("blind_lost", 0),
                    o.isNull("pointer_peak") ? 0 : o.optInt("pointer_peak", 0),
                    o.isNull("k_preset_ms") ? Double.NaN : o.optDouble("k_preset_ms", Double.NaN),
                    o.optBoolean("ten_finger_ok", false)));
        }
        return Collections.unmodifiableList(out);
    }

    static String readAsset(Context ctx, String name) throws Exception {
        try (InputStream in = ctx.getAssets().open(name)) {
            ByteArrayOutputStream bo = new ByteArrayOutputStream(64 * 1024);
            byte[] b = new byte[8192];
            int n;
            while ((n = in.read(b)) > 0) bo.write(b, 0, n);
            return new String(bo.toByteArray(), StandardCharsets.UTF_8);
        }
    }
}
