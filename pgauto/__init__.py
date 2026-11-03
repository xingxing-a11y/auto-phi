"""Phigros 官方谱（PGR）工具链 —— A 段解包（M4）/ B 段解析（M1–M2）/ C 段调度（M5）。

A 段 · 解包（`apk.py` + `catalog.py`）
======================================

Phigros **不用**传统的 `assets/bin/Data` 打包，而是走 Unity **Addressables**：

    APK(ZIP，明文)
      └─ assets/aa/catalog.json              ← 目录表（明文 JSON 4.1 MiB）
           └─ 谱面键 Assets/Tracks/<曲名>/Chart_<难度>.json
                └─ dependencyKey → 所在 bundle
                     └─ assets/aa/Android/<md5>_<hash>.bundle   ← UnityFS 容器
                          └─ UnityPy → TextAsset.m_Script      ← 谱面 JSON，**明文**

- **不下载整个 APK**：`apk.RemoteSource` 走 HTTP Range 只取需要的字节。
  4.0.1 的包是 3.104 GiB，全量取 1064 个谱面只花 **451 MiB**（省 7.4 倍）。
- **不加密**：谱面 bundle 全部走 `AssetBundleProvider`。厂商自有
  `C9SecretAssetBundleProvider` 只用在 8 个与谱面无关的核心 bundle 上。
- **可复现**：`--apk <本地文件>` 走 `LocalSource`，离线也能跑。

B 段 · 解析（`events.py` + `chart.py`，M1 击打点 / M2 轨迹）
===========================================================

坐标、时间与运动语义对齐两套**独立**参考实现，互相印证：

1. `TeamFlos/phira` 的 `prpr` 核心库（Rust）
2. `lchzh3473/sim-phi`（TypeScript）—— 用于交叉验证，不依赖同一份代码

逐项对应关系：

    参考实现位置                                     | 本包实现
    ------------------------------------------------|----------------------------
    prpr/src/parse/pgr.rs::parse_float_events        | events.parse_float_events
    prpr/src/parse/pgr.rs::parse_move_events         | events.parse_move_events
    prpr/src/parse/pgr.rs::parse_move_events_fv1     | events.parse_move_events_fv1
    prpr/src/parse/pgr.rs::parse_speed_events        | events.parse_speed_events
    prpr/src/parse/pgr.rs::parse_notes               | chart.X_UNIT / Chart._hit_point
    prpr/src/core/object.rs::now_translation         | chart.Chart.line_state（y / aspect_ratio）
    prpr/src/core/note.rs::now_transform             | chart.Chart.note_travel（dy）
    prpr/src/core/note.rs::render                    | chart.Chart.note_pos（base 沿局部 y）
    prpr/src/core.rs::HEIGHT_RATIO                   | chart.HEIGHT_RATIO
    sim-phi src/core.ts:239 / :463-467 / :447-455   | chart.Chart.note_pos（X_UNIT、
                                                      合成顺序、dy 尺度）

⚠️ **一处有意偏离**：判定线的旋转中心取「判定线自身」而非「屏幕中心」。
    `prpr/src/core/line.rs::now_transform` 的代数读法与 sim-phi 的实现不一致，
    本包按后者 + 官谱「音符必须在屏幕内」的外部约束裁定，依据见 `chart.py` 模块文档。
    该裁定的最终裁决点是真实游戏画面的逐帧比对（M2，需外部素材）。

✅ **速度积分的四重独立验证**（互不共享代码）：

1. 对拍官谱每个音符自带的 `floorPosition` —— 样本谱 **1300/1300**（`tools/verify_timeline.py`）
2. 对拍官谱 `speedEvents` 自带的 `floorPosition` 快照 —— **402/402** 落在 float32
   舍入预算内（`tools/verify_extract.py` 组 [6]）
3. 两套独立参考实现（Phira `prpr` + sim-phi）的公式互证
4. 官谱自身的结构不变量：时间轴**重叠 0 / 空隙 0**（35148 个事件）

✅ **A 段验收取最强形态**：从官方 APK 解出的 Rrhar'il AT 与社区同源样例**逐字节相同**
    （`sha256=27a9f73a65d155daa53147dda55da7544b8e4812db71684b9d80b9c722fd9042`）。
    全量 1064 个谱面结构合法、682746 个音符字段零例外。

📌 **从 1064 个真实谱面枚举出的 schema**（不是从文档抄的，见 `tools/verify_extract.py`）：

    formatVersion          1 (29 个) / 3 (1035 个) —— **不是全 3**
    根必需                  formatVersion, offset, judgeLineList
    根可选                  blockAreaList (30), numOfNotes (6)
    判定线必需              bpm, notesAbove, notesBelow, 四个事件通道
    判定线可选              numOfNotes / numOfNotesAbove / numOfNotesBelow (144 条线)
    音符（零例外）          type, time, positionX, holdTime, speed, floorPosition
    speedEvents             startTime, endTime, value（+ floorPosition 402 条可选）
    judgeLineMoveEvents     v3 必需 start2/end2；**v1 没有**
    rotate/disappearEvents  start2/end2 为可选（少数条目才有）
    **全套事件都没有 `easingType`** —— 官方谱不做缓动插值，缓动靠线性事件叠加
    反向区间                `endTime < startTime` 合法（73 处 / 16 首），
                            但**绝不出现在 speedEvents**（否则击打点会错）

C 段 · 调度（`schedule.py`，M5）没有可直接对拍的参考实现，改用**独立验收**。
模型从 M3 的「区间图着色」整体换成 **指针池 + 切向投影复用**（phisap `algo/algo2.py` 谱系）：

    - **判定区是矩形**：法向**无限长**、切向宽度 = 音符宽
      ⇒ 手指只要沿判定线方向的投影够近即可，法向离多远都行。这是「8 个 hold +
      一堆 drag 只要 2 个指针」的原理，也是全量 **98.44%** 的服务不需要发任何事件的原因。
    - **Drag / Hold 持续段不需要 DOWN**，只需判定区内有手指；Tap / Hold 起点 / Flick
      才需要 DOWN。所以指针**不逐个任务抬起**，D 段必须按整条流回放。
    - **但一次点击只能判定一个 Tap / Hold**（Phigros 真实规则，优先判最早的那个）。
      所以 Tap / Hold 起点**不能靠「判定区内已有手指」顶替**：同帧两个 Tap 必须各按一次。
      `Incyde`、`QZKagoRequiem/AT` 这类「同刻同位、不同判定线」的双押，玩家在真机上
      就是两根手指按两次。`_Pointer.click_claimed` 记录「这一次按下有没有被吃掉」；
      Drag / Hold 持续段产生的按下保持未认领，谁都可以拿去判一个 Tap（那是真实点击）。
      全量共 **774 处同帧双押，分布在 117 个谱面**。
    - Hold 被拆成「1 个 TAP 起手 + 每毫秒 1 个 DRAG」，跟随路径取**判定线上的判定点**
      （随判定线移动 / 旋转而变），不是「按住不动」。
    - **M3 的 `move_speed` 参数被彻底删除**：注入的触点位置由事件直接设定，
      不存在「手指要花时间飞过去」。M3 那 27 个「超限谱面」是建模误差，不是谱面难。

⚠️ **几何一律取「事件被盖的那个整数毫秒」的判定点，不取音符的精确时刻。**
    `notes.json` 的 `t_ms` 是 4 位小数，事件却只能盖在整数毫秒上。判定线会在事件边界
    **瞬变**（`Incyde/IN` 的 5 号线在 t=47333.333 处从 `(595.5, 864.0)@0°` 跳到
    `(788.0, 844.9)@-3°`，差 **192.5 像素**），取错时刻会让手指落在判定区外 —— 真机上
    必 Miss，而「判定区内有手指」的宽松模型量不出来（两边用的是同一个错坐标，一起错）。
    下发时刻本身在**最近的整数毫秒**里挑「判定点可达」的那个（`pick_head_time`），
    两个候选都距音符时刻不到 1ms，远在判定窗口内。

**屏幕外音符的三级兜底**（分层与 phisap `algo/algo1.py` 一致）：

    1. **时间微调**（只对 Flick，默认开）。判定线会**闪烁**到另一个位置，同一个 Flick
       因此在两个位置都判得到 —— phisap 正是靠这条救回了 `DESTRUCTION 3,2,1` 的屏外 flick。
       在判定窗口 `t ± 30ms` 内取距 `t0` 最近的入屏时刻。全量命中 **1 处**（DiamondDust/IN）。
    2. **沿法线投影**（默认开）。与判定点同法线的一切点等效，取屏幕矩形上的交点中点。
    3. **拉进屏幕**（`--clamp-offscreen`，**默认关**）。前两级失败才用，只保证事件流
       完整可注入、**不保证命中**，所以默认宁可把它如实报成 `unassigned`。

⚠️ **一处与参考实现的有意分歧**：phisap 的 `recalc_pos` 在「法线与屏幕不相交」时，
    `div()` 的除零得到 nan 并污染两条候选线，`sumx/2` 与 `sumy/2` 双双落到 0 ——
    坐标被**静默摆到屏幕角上**。本包把这种情况显式化：返回 `None` 并记入 `unassigned`。

**验收（`tools/verify_schedule.py`）仍**刻意不 import 本包**，只读交付物，独立解码几何、
用指针状态机反推。**22 条判据全部通过**（Tap/Drag/Hold/Flick 四类判定仿真 + 事件流合法性
+ 规划对拍 + 规模统计）。判据之一「判定区离开屏幕的检查点」只要求手指留在屏幕上 ——
这些时刻判定区与屏幕无交集，数学上不可命中，如实单列而不是算作缺口。

**全量普查（1064 个官方谱面，`tools/sweep_charts.py`）**：

    通过 1062 / 未分配 2 / 异常 0        最大指针数 **9** / 最大峰值 9
    音符 682746   服务 35483510   复用 34930117 = **98.44%**
    指针数分布 {1:11, 2:624, 3:163, 4:223, 5:20, 6:4, 7:4, 8:13, 9:2}
    同帧双押 774 处（117 个谱面）
    `--clamp-offscreen` 后：**1064 / 1064 通过**

    剩下 2 个谱面共 2 个音符够不着，都是 **Drag**，原因是判定线整体移到屏幕外
    （如 `Message/EZ` 的判定线归一化 x = 3.0，即 3 倍屏宽处）。

**端到端离线判分（`tools/replay_score.py`，G 段）**：把 `touches.json` 灌进一个
    **独立重写的判定器**，按 Phigros 真实规则逐音符判分（Perfect ≤80ms / Good ≤160ms /
    Bad ≤180ms 仅 Tap / Hold 可提前 80ms 松手 / Drag 与 Flick 只有 Perfect 与 Miss），
    再算 `900000×ACC + 100000×(最大连击/物量)`：

    **满分（理论 AP）1044 / 1064 = 98.1%**（修复前 894 = 84.0%）
    平均得分 999420.5
    **「严格模型」与「宽松模型」的差：0 个谱面**（修复前 117 个）

    「严格 = Tap/Hold 起手必须独占一次按下」「宽松 = 判定区内有手指即可」两套模型并排跑，
    两者的差就是复用模型在真机上的**风险敞口**。敞口从 117 个谱面清零，是这一轮的核心成果。

⚠️ **事件边界容差（`schedule._LOCATE_EPS`）**：音符时刻与判定线事件边界重合是常态
    （都由整数单位表示），而 `notes.json` 把 `t_ms` 序列化成 4 位小数，反推回单位带
    约 5e-6 误差；判定线又会在边界处**瞬变**（Rrhar'il AT #57 的 rotate 在 u=1408
    从 -10° 直接跳到 0°），这点误差足以取到瞬变**之前**的值 —— 实测击打点偏 **87.5px**。
    加 1e-4 单位容差（把贴左边界的点归给右侧新事件）后偏差降到 **0.0005px**。

⚠️ **交付给 D 段的硬约束：指针不逐个任务抬起。**
    一根手指会连续服务一串音符（Drag / Hold 持续段不需要 DOWN），事件流表现为
    「DOWN 少、MOVE 多」（Rrhar'il AT：DOWN 140 / MOVE 10620 / UP 140）。
    **D 段必须按整条流回放，不能按 DOWN/UP 配对切分任务。**
    Flick 的滑动固定在 60ms 内完成（`FLICK_SPAN_MS`），沿判定线法向来回 ±45px（1920 画布）。
"""

__all__ = ["anim", "events", "chart", "schedule", "apk", "catalog"]

__version__ = "0.4.0"
