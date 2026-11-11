"""测试包的初始化：把 CoreCode 的家目录钉在仓库内。

**这一步必须在任何 `corecode` 子模块被导入之前完成。**

隔离之所以放在 `tests/__init__.py`，是因为 `unittest` 在导入任何测试模块之前
一定会先导入本包。测试模块自己写的 `sys.path.insert` / 环境变量设置要到它们
被导入时才执行——那时可能已经有别的模块先导入了配置。

（早先这里写的是「`corecode.config` 在模块加载期就求值了 `CORECODE_HOME`
等路径常量」，那是当时的事实。现在那些路径常量已经**全部删掉**，配置只通过
`corecode_home()` 这类动态访问器求值——见 config.py 的说明。隔离仍然必要：
动态访问器读的是环境变量，而环境变量必须在第一次调用前设好。）

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

os.environ["CORECODE_HOME"] = str(_ISOLATED_HOME)
# 清掉这些变量：它们会覆盖 settings.json，让依赖配置的测试结果随环境变化。
for _key in (
    "CORECODE_MODEL",
    "ANTHROPIC_MODEL",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_BASE_URL",
    "CORECODE_MAX_OUTPUT_TOKENS",
):
    os.environ.pop(_key, None)

# 触发一次路径求值并断言隔离生效。
from corecode import config as _config  # noqa: E402

# 用**动态访问器**而不是模块级常量：后者已不存在，而且它本来就是这套隔离要
# 防的东西（导入期快照在环境变量变化后不会跟着变）。
_resolved = Path(str(_config.corecode_home())).resolve()
if _TESTS_DIR.resolve() not in _resolved.parents:
    raise RuntimeError(
        "测试隔离失败：CORECODE_HOME 未落在仓库内 "
        f"({_resolved})。拒绝继续，以免读写真实用户目录。"
    )
if _resolved == Path.home() / ".corecode":
    raise RuntimeError("测试隔离失败：CORECODE_HOME 指向真实用户目录。")
