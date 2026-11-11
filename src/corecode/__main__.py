"""支持 `python -m corecode`。

包如果只有 `__init__.py` 而没有 `__main__.py`，Python 会报
「'corecode' is a package and cannot be directly executed」。加上这个文件后，
三种启动方式都可用：

    corecode                     控制台脚本（需安装）
    python -m corecode           可编辑安装后，不需要 PYTHONPATH
    python -m corecode.cli       从源码直接跑（需 PYTHONPATH=src）

刻意只做转发，不含任何逻辑——逻辑在 `cli.main` 里，只有一份。
"""

from __future__ import annotations

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
