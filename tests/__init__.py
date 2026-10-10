"""测试包的初始化：把 MiniCode 的家目录钉在仓库内。

**这一步必须在任何 `minicode` 子模块被导入之前完成。**

`minicode.config` 在模块加载期就求值了 `MINICODE_HOME` 等路径常量；一旦
`config` 先被导入，之后再设环境变量就没有任何效果——测试会去读写真实用户目录
（`~/.mini-code`）。这不是假设，是本项目踩过的真实事故，所以把隔离放在
`tests/__init__.py` 里：`unittest` 在导入任何测试模块之前一定会先导入本包。

同时这里做一次 **fail-closed 断言**：如果路径没有落在仓库内，宁可直接报错，
也不要继续跑下去污染用户目录。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _TESTS_DIR.parent

# 本包会在任何测试模块之前被导入，因此 `src` 的路径注入必须放在这里：
# 测试模块自己写的 sys.path.insert 要到它们被导入时才执行，那时已经太晚。
_SRC = str(_REPO_ROOT / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

_ISOLATED_HOME = _TESTS_DIR / "_tmp" / "mini-home"

os.environ["MINICODE_HOME"] = str(_ISOLATED_HOME)
# 清掉这些变量：它们会覆盖 settings.json，让依赖配置的测试结果随环境变化。
for _key in (
    "MINICODE_MODEL",
    "ANTHROPIC_MODEL",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_BASE_URL",
    "MINICODE_MAX_OUTPUT_TOKENS",
):
    os.environ.pop(_key, None)

# 触发一次路径求值并断言隔离生效。
from minicode import config as _config  # noqa: E402

_resolved = Path(str(_config.MINICODE_HOME)).resolve()
if _TESTS_DIR.resolve() not in _resolved.parents:
    raise RuntimeError(
        "测试隔离失败：MINICODE_HOME 未落在仓库内 "
        f"({_resolved})。拒绝继续，以免读写真实用户目录。"
    )
if _resolved == Path.home() / ".mini-code":
    raise RuntimeError("测试隔离失败：MINICODE_HOME 指向真实用户目录。")
