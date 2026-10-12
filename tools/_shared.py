"""共享的小工具。

只有一件事：把标准输出钉在 UTF-8。

## 为什么需要它

Windows 控制台默认编码是 cp936（GBK）之类，而本项目所有界面文本与工具输出
都是中文。不在程序里显式重配的话，用户看到的是乱码——「程序明明能用却像是
坏了」。这比一个明确的报错更糟，因为它看起来像功能故障。

环境变量 `PYTHONIOENCODING=utf-8` 也能解决，但要求每个用户都记得设它，
不能算修好。

这是**零依赖**项目里唯一的共享工具模块，刻意保持极小。
"""

from __future__ import annotations

import sys


def force_utf8_output() -> None:
    """把 stdin / stdout / stderr 重配为 UTF-8。

    三个流都要，缺一不可：

      - 只改 stdout → 输出乱码。
      - 只改 stdout+stderr → **管道输入**按 cp936 解码，中文变成代理字符，
        随后写回输出时抛 `UnicodeEncodeError: surrogates not allowed`。

    在无法重配时静默跳过（例如流被替换成了不支持 `reconfigure` 的对象，
    或已被 pytest 之类接管）。`errors="replace"` 保证即便出现无法编码的
    字符也只是显示成替代符，而不会中断程序。
    """
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass


__all__ = ["force_utf8_output"]
