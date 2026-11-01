# APK 端内注入可行性探针

> 目的：在**真机上**证实/否证「手机端自己注入触摸」这件事到底成不成立，
> 以及成立之后精度够不够。所有结论都来自实测，不看文档下结论。
>
> 背景：这件事最早是为了回答「能不能把打歌能力整个搬进 APK、不再依赖电脑」而做的可行性验证。

## 为什么能在 shell 身份下量

`app_process` 起的进程是 `uid=2000 (shell)`，与 Shizuku 的 `UserService` 同等权限级别，
也与 scrcpy-server 同源。所以本探针的结论对 Shizuku 档**直接适用**，
不需要先装 Shizuku 才能验证。

## 用法（Git Bash，cwd = 仓库根）

```bash
bash tools/apk_probe/build.sh api            # GestureDescription 真值 + InputManager 可达性
bash tools/apk_probe/build.sh api2           # InputManager 失败根因 + IInputManager 备路
bash tools/apk_probe/build.sh ticker 4000    # 1 ms tick 精度（忙等 vs sleep）
bash tools/apk_probe/build.sh inject 300     # 真实注入耗时（只注 MOVE，不会误触）
.venv/Scripts/python.exe tools/apk_probe/sched_sim.py   # 排程可行性模拟（纯离线）
```

产物写 `tmp/_apkprobe_<类名>/`（`tmp/_*` 不入库）；设备侧 jar 由 `trap` 自动删。

## 关键结论（2026-10-20，HONOR ALT-AN00 / A14 / arm64）

| 项 | 结果 |
|---|---|
| `injectInputEvent(event, 0)` | **600/600 成功**（两轮各 300） |
| `injectInputEvent(event, 2)` | **0/600，全部 refuse 且不抛异常** ← 最容易踩的沉默失败 |
| 单次注入耗时 | p50 0.51~0.63 ms，p99 1.87~3.27 ms，max 3.82 ms |
| 1 ms tick（nanoTime 忙等） | p90 0.5 ms，p99 1.8~2.1 ms，max 3.2 ms |
| 1 ms tick（`Thread.sleep(1)`） | 平均迟到 0.32 ms，max 3~6 ms（不够用） |
| `getMaxStrokeCount()` | **20**（不是常见硬编码的 10） |
| `getMaxGestureDuration()` | 60000 ms |
| `InputManager.getInstance()` | **NPE**，`prepareMainLooper()` 也救不了 |
| `ServiceManager → IInputManager$Stub` | OK（唯一的可用道路） |

## 三个「照直觉写会挂」的坑

1. **`mode` 只能填 0**。填 2（`WAIT_FOR_FINISH`）会全被拒，但**只返回 `false`、不抛异常** ⇒
   整轮 0 注入、程序"正常结束"、结算 0 分。每个事件都要断言返回值。
2. **`IInputManager` / `ServiceManager` 是 @hide**，公开 `android.jar` 里没有
   ⇒ 只能全反射，直接 `import` 编译不过。
3. **`adb push` 的路径双向陷阱**：不加 `MSYS_NO_PATHCONV=1`，`/data/local/tmp` 会被 Git Bash
   改写成 Git 安装目录；加了之后**源路径**也不再转换 ⇒ 源要 `cygpath -w`。

## 排程模拟（`sched_sim.py`）

用实测的抖动分布重放真实谱面，比较两种调度：

| 曲目 | 全事件串行（悲观） | 关键事件优先 + MOVE 可丢 |
|---|---|---|
| demiurge 2125 | p99 3.36 / max 6.97 ms | p99 3.10 / max 5.56 ms |
| kuangxi 2444 | p99 3.93 / max **44.22** ms | p99 3.28 / max 6.38 ms |
| lacamp 6239 | p99 4.54 / max **47.31** ms | p99 3.29 / max 6.37 ms |

⇒ 端内必须做**两级调度**（`DOWN/UP` 保证准时，`MOVE` 落后就丢），
否则密集谱面会整体偏晚 40+ ms 且不报错。
