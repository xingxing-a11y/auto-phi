"""pytest 公共配置。

把项目根与 `tools/` 加进 `sys.path` —— 这样测试既能 `import pgauto`，
也能直接加载 `tools/verify_extract.py` 这类验收脚本（它们不是包，
只能按文件路径导入）。不依赖「从哪个目录跑 pytest」。
"""

import os
import sys
from pathlib import Path

# ★★ 子进程必须用 UTF-8 写管道，否则 **7 条测试会因为控制台编码而红**（2026-10-25 实测）：
#   好几个测试 spawn `tmp/full_run.py` 并读 stdout；子进程在 Windows 上按**本地编码**
#   （中文系统 = gbk）写管道，而脚本里 `print("…⇒…")` / `print("…✓…")` 里全是箭头/勾
#   ⇒ `UnicodeEncodeError` ⇒ 测试看起来像「代码回归」，其实是环境变量没设。
#   放这里是因为 `subprocess.run(..., capture_output=True)` **默认继承 os.environ**，
#   一处设置覆盖全部用例（包括以后新写的）。⛔ 别改成在每条用例里各传一次。
#   判据：删掉这三行，`pytest tests/ -q` 应报 7 failed（test_apk_src_wiring 4 + test_edge_fast 3）。
os.environ.setdefault("PYTHONUTF8", "1")

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT, ROOT / "tools"):
    s = str(p)
    if s not in sys.path:
        sys.path.insert(0, s)
