"""交互式本地安装器。

## 它做什么

1. 依次问三项：模型名、`ANTHROPIC_BASE_URL`、`ANTHROPIC_AUTH_TOKEN`
2. 写进 `<CORECODE_HOME>/settings.json`
3. 在 `<CORECODE_HOME 之外的可执行目录>` 放一个启动器
4. 如果那个目录不在 `PATH` 里，告诉用户怎么加

第 1 步是它的主要价值：**不用手写 JSON**。键名（`ANTHROPIC_BASE_URL` 而不是
`baseUrl`）、嵌套结构（都在 `env` 里）、以及「模型名要同时写 `model` 和
`env.ANTHROPIC_MODEL` 两处」这些细节，靠记忆写很容易漏一处——而漏了的症状是
「配置看起来对了但没生效」。

## 运行方式

    python -m corecode.install

"""

from __future__ import annotations

import os
import platform
import sys
from pathlib import Path
from typing import Any, Callable, TextIO

from .config import load_effective_settings, save_settings, settings_path

__all__ = [
    "ask_required",
    "default_bin_dir",
    "has_path_entry",
    "install_local",
    "launcher_scripts",
]

#: 默认的启动器目录（相对家目录）。
#:
#: `bin`（与家目录同级更浅、更容易被用户接受加进 PATH）。
_POSIX_BIN_DIRNAME = ".local/bin"
_WINDOWS_BIN_DIRNAME = "bin"

def default_bin_dir() -> Path:
    """启动器目录。可用 `CORECODE_BIN_DIR` / `CORECODE_BIN_DIR` 覆盖。"""
    from .config import env_value

    override = env_value("BIN_DIR")
    if override:
        return Path(override).resolve()
    if platform.system() == "Windows":
        return Path.home() / _WINDOWS_BIN_DIRNAME
    return Path.home() / _POSIX_BIN_DIRNAME

def has_path_entry(target: Path) -> bool:
    """目录是否已在 `PATH` 里。

    """
    entries = (os.environ.get("PATH") or "").split(os.pathsep)
    normalized = {os.path.normcase(os.path.normpath(item.strip())) for item in entries if item.strip()}
    return os.path.normcase(os.path.normpath(str(target))) in normalized

def launcher_scripts(repo_root: Path) -> dict[str, str]:
    """生成启动器脚本。返回 `{文件名: 内容}`。

    ## 启动器必须自己设 `PYTHONPATH`

    本项目是 `src/` 布局，只有两种情况下 `-m corecode.cli` 能找到包：
    ① `pip install` 过；② 调用时 `PYTHONPATH` 里有 `src/`。

    早先生成的脚本**两样都没做**——`repo_root` 参数收下了却**从未使用**。
    于是从源码使用时，启动器一执行就是：

        ModuleNotFoundError: No module named 'corecode'

    这个错误的可诊断性极差：用户会以为「没装」，实际装的是**启动器有问题**。

    现在三个脚本都显式把 `<repo_root>/src` 放到 `PYTHONPATH` **最前面**
    ——前面而不是后面：仓库里的代码应当优先于任何已安装的同名包。

    Windows 上同时给 `.cmd` 与 `.ps1`：`cmd.exe` 只认 `.cmd`，PowerShell
    更愿意执行 `.ps1`，两者都放才能「在任意终端输入 corecode 启动」。
    """
    src_dir = repo_root / "src"
    python = f'"{sys.executable}"'

    # 三个脚本的路径分隔符不同：Windows 用 `;`，POSIX 用 `:`。
    posix = (
        "#!/usr/bin/env sh\n"
        "set -eu\n"
        f'PYTHONPATH="{src_dir}${{PYTHONPATH:+:$PYTHONPATH}}"\n'
        "export PYTHONPATH\n"
        f'exec {python} -m corecode.cli "$@"\n'
    )
    # `setlocal` 让 PYTHONPATH 只在这次调用里生效，不污染用户的终端。
    cmd = (
        "@echo off\r\n"
        "setlocal\r\n"
        f'set "PYTHONPATH={src_dir};%PYTHONPATH%"\r\n'
        f"{python} -m corecode.cli %*\r\n"
    )
    # **注释刻意用 ASCII。**
    #
    # Windows PowerShell 5.1 读取**无 BOM** 的 UTF-8 文件时按 ANSI 解码，
    # 非 ASCII 字符会变成乱码。注释乱码本身不影响执行，但一旦有人把非 ASCII
    # 写进**代码**（比如路径），就会真的出问题。launcher 保持纯 ASCII 最稳。
    ps1 = (
        "# CoreCode launcher (PowerShell)\n"
        f'$env:PYTHONPATH = "{src_dir};" + $env:PYTHONPATH\n'
        f"& {python} -m corecode.cli @args\n"
        "$code = $LASTEXITCODE\n"
        # **显式传递退出码。** PowerShell 里 `&` 之后脚本会继续往下走；
        # 若最后不是 `exit`，`$LASTEXITCODE` 会被后续语句吞掉，于是
        # `corecode --help; echo $LASTEXITCODE` 恒为 0，脚本无法判断成败。
        "exit $code\n"
    )
    return {"corecode": posix, "corecode.cmd": cmd, "corecode.ps1": ps1}

def ask_required(
    read_line: Callable[[], str | None],
    label: str,
    default: str = "",
    *,
    stream: TextIO | None = None,
) -> str:
    """问一个必填项，直到拿到非空答案。

    """
    out = stream or sys.stdout
    while True:
        suffix = f" [{default}]" if default else ""
        out.write(f"{label}{suffix}: ")
        out.flush()
        incoming = read_line()
        if incoming is None:
            # EOF：不再重问（否则管道输入会死循环）。
            return default
        answer = incoming.strip()
        value = answer or default
        if value:
            return value
        print("该项不能为空，请重新输入。")

def _secret_prompt_suffix(secret: str) -> str:
    return " [saved]" if secret else " [not set]"

def install_local(
    *,
    read_line: Callable[[], str | None] | None = None,
    stream: TextIO | None = None,
    repo_root: Path | None = None,
    bin_dir: Path | None = None,
    write_launcher: bool = True,
) -> dict[str, Any]:
    """跑一次安装。返回一份结果摘要（便于测试断言，也便于调用方复用）。

    `read_line` 注入是为了可测：交互式函数最难测的部分就是「输入从哪来」。
    """
    out = stream or sys.stdout
    if read_line is None:
        read_line = lambda: sys.stdin.readline()  # noqa: E731

    root = repo_root or Path(__file__).resolve().parent.parent.parent
    target_bin = bin_dir or default_bin_dir()

    settings = load_effective_settings()
    current_env = settings.get("env") or {}
    assert isinstance(current_env, dict)

    out.write("corecode installer\n")
    out.write(f"配置会写入 {settings_path()}\n")
    out.write("配置保存在独立目录中，不会影响其它本地工具配置。\n")
    out.write("\n")

    model = ask_required(
        read_line,
        "Model name",
        str(settings.get("model") or current_env.get("ANTHROPIC_MODEL") or ""),
        stream=out,
    )
    base_url = ask_required(
        read_line,
        "ANTHROPIC_BASE_URL",
        str(current_env.get("ANTHROPIC_BASE_URL") or "https://api.anthropic.com"),
        stream=out,
    )

    saved_token = str(current_env.get("ANTHROPIC_AUTH_TOKEN") or "")
    out.write(f"ANTHROPIC_AUTH_TOKEN{_secret_prompt_suffix(saved_token)}: ")
    out.flush()
    incoming = read_line()
    token_input = (incoming or "").strip()
    auth_token = token_input or saved_token

    if not auth_token:
        raise ValueError(
            "ANTHROPIC_AUTH_TOKEN 不能为空。"
            "（已有配置里也没有可复用的 token，请重新运行并填入。）"
        )

    written = save_settings(
        {
            "model": model,
            "env": {
                "ANTHROPIC_BASE_URL": base_url,
                "ANTHROPIC_AUTH_TOKEN": auth_token,
                "ANTHROPIC_MODEL": model,
            },
        }
    )

    launchers: list[Path] = []
    if write_launcher:
        target_bin.mkdir(parents=True, exist_ok=True)
        for name, content in launcher_scripts(root).items():
            path = target_bin / name
            path.write_text(content, encoding="utf-8", newline="")
            if not name.endswith((".cmd", ".ps1")):
                path.chmod(0o755)
            launchers.append(path)

    on_path = has_path_entry(target_bin)

    out.write("\n")
    out.write("安装完成。\n")
    out.write(f"配置文件: {written}\n")
    if launchers:
        out.write(f"启动命令: {target_bin / 'corecode'}\n")
    out.write("\n")
    if on_path:
        out.write("现在你可以在任意终端输入 `corecode` 启动。\n")
    else:
        out.write(f"你的 PATH 里还没有 {target_bin}\n")
        out.write(_path_hint(target_bin))

    return {
        "settings_path": str(written),
        "model": model,
        "base_url": base_url,
        "bin_dir": str(target_bin),
        "launchers": [str(item) for item in launchers],
        "on_path": on_path,
    }

def _path_hint(bin_dir: Path) -> str:
    """按平台给出「怎么把这个目录加进 PATH」的说明。"""
    if platform.system() == "Windows":
        return (
            "可以把下面这行加入 PowerShell 的 $PROFILE，或"
            "在「系统属性 → 环境变量」里追加：\n"
            f'$env:Path = "{bin_dir};" + $env:Path\n'
        )
    return (
        "可以把下面这行加入 ~/.bashrc 或 ~/.zshrc:\n"
        f'export PATH="{bin_dir}:$PATH"\n'
    )

def main() -> int:
    try:
        install_local()
    except (ValueError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
