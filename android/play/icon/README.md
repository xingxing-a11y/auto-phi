# 应用图标源稿

**这里是源稿，构建读的是 `../app/src/main/res/`。** 两边内容必须一致，但不要手动改 res/。

## 文件

| 文件 | 用途 |
|---|---|
| `ic_launcher_1024.png` / `ic_launcher_512.png` | 母版（512 那份给商店/文档用） |
| `mipmap-{m,h,x,xx,xxx}dpi/ic_launcher.png` | 传统位图图标（老桌面 / 第三方桌面读它） |
| `mipmap-*/ic_launcher_foreground.png` | adaptive 前景层，透明底，108dp（432px @xxxhdpi） |
| `mipmap-*/ic_launcher_background.png` | adaptive 背景层，全不透明，同为 108dp |
| `icon_spec.png` | 配色规格板（值全部取自 `MainActivity` 的 BG / ACCENT / OK 常量） |
| `icon_sizes_preview.png` | 48/72/96/144/192 五档在深浅底下的可读性对照 |
| `icon_launcher_preview.png` | 圆形 / 方圆 / 圆角方 三种遮罩下的效果 |

## 改图流程

1. 在 `PG自动演奏-应用图标` 画布上改，重新导出 PNG 覆盖本目录（保 `mipmap-*/` 的目录结构与文件名）
2. 同步进构建：
   ```bash
   .venv/Scripts/python.exe -c "
   import shutil, pathlib
   for d in ['mdpi','hdpi','xhdpi','xxhdpi','xxxhdpi']:
       src = pathlib.Path('android/play/icon')/f'mipmap-{d}'
       dst = pathlib.Path('android/play/app/src/main/res')/f'mipmap-{d}'
       dst.mkdir(parents=True, exist_ok=True)
       for f in src.glob('*.png'): shutil.copyfile(f, dst/f.name)
   "
   ```
3. `bash android/play/build.sh` —— 第 ⑨ 组会逐字节核对 res/ 与 APK 内是否一致

## 三条硬约束

- **图形必须落在 66dp 安全圆内**（108dp 画布的直径 66/108 内切圆）。超了在圆形遮罩的桌面上会切边。
  实测余量：α≥128 的最远像素 r=124.5px，上限 132px。
- **前景层必须透明底、只放图形**。背景烘进前景会让启动器的视差露出生硬色块边缘。
- **别删 `mipmap-*dpi/ic_launcher.png`**。只留 adaptive 的话，部分第三方桌面会显示空白图标。

## 已知坑

- `aapt2 compile` **默认会 crunch PNG**，其中 `ic_launcher_foreground`(xxxhdpi) 那张会被**有损量化**
  （实测最大通道差 112 / 15.5 万像素）。`build.sh` 因此带 `--no-crunch`，**别去掉**。
- 换图标后 `versionCode` 必须递增（当前 3），否则部分桌面会沿用旧图标缓存。
