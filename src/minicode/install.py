"""交互式本地安装器。

对应原版 `MiniCode/src/install.ts`（126 行），通过 `npm run install-local` 运行。

## 它做什么

1. 依次问三项：模型名、`ANTHROPIC_BASE_URL`、`ANTHROPIC_AUTH_TOKEN`
2. 写进 `<MINICODE_HOME>/settings.json`
3. 在 `<MINICODE_HOME 之外的可执行目录>` 放一个启动器
4. 如果那个目录不在 `PATH` 里，告诉用户怎么加

第 1 步是它的主要价值：**不用手写 JSON**。键名（`ANTHROPIC_BASE_URL` 而不是
`baseUrl`）、嵌套结构（都在 `env` 里）、以及「模型名要同时写 `model` 和
`env.ANTHROPIC_MODEL` 两处」这些细节，靠记忆写很容易漏一处——而漏了的症状是
「配置看起来对了但没生效」。

## 与原版的两处差异（D39）

**① 启动器的形态随平台。** 原版写死 `#!/usr/bin/env bash`，在 Windows 上无效。
本实现按平台生成：POSIX 写 `sh` 脚本，Windows 写 `.cmd` 与 `.ps1`。

**② 默认安装目录。** 原版用 `~/.local/bin`（POSIX 惯例）。Windows 上这个目录
通常不在 `PATH` 里，因此改用一个与 `PATH` 无关的默认值，并把「怎么加进 PATH」
的说明按平台区分。

另外原版用 `process.env.PATH.split(':')` 判断目录是否已在 `PATH` 里——在
Windows 上 `PATH` 用 `;` 分隔，那个判断会永远失败。这里用 `os.pathsep`。

## 运行方式

    python -m minicode.install

对应原版的 `npm run install-local`。
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
#: 原版用 `.local/bin`。Windows 上这个目录通常不在 `PATH` 里，改用
#: `bin`（与家目录同级更浅、更容易被用户接受加进 PATH）。
_POSIX_BIN_DIRNAME = ".local/bin"
_WINDOWS_BIN_DIRNAME = "bin"


def default_bin_dir() -> Path:
    """启动器目录。可用 `MINI_CODE_BIN_DIR` / `MINICODE_BIN_DIR` 覆盖。"""
    from .config import env_value

    override = env_value("BIN_DIR")
    if override:
        return Path(override).resolve()
    if platform.system() == "Windows":
        return Path.home() / _WINDOWS_BIN_DIRNAME
    return Path.home() / _POSIX_BIN_DIRNAME


def has_path_entry(target: Path) -> bool:
    """目录是否已在 `PATH` 里。

    **用 `os.pathsep` 而不是写死 `:`。** 原版是 `PATH.split(':')`——在 Windows 上
    `PATH` 用 `;` 分隔，那个判断会永远返回 false，于是每次都提示「还没加进 PATH」，
    而用户明明加过了。
    """
    entries = (os.environ.get("PATH") or "").split(os.pathsep)
    normalized = {os.path.normcase(os.path.normpath(item.strip())) for item in entries if item.strip()}
    return os.path.normcase(os.path.normpath(str(target))) in normalized


def launcher_scripts(repo_root: Path) -> dict[str, str]:
    """生成启动器脚本。返回 `{文件名: 内容}`。

    ## 启动器指向哪里

    原版指向仓库里的 `bin/minicode`（一个 bash 入口）。本项目没有那个文件
    ——入口由 `pyproject.toml` 的 `[project.scripts]` 声明
    （`minicode = "minicode.cli:main"`），`pip install` 后由 setuptools 生成。

    因此这里用 `python -m minicode.cli`：它在「已安装」与「设了 PYTHONPATH」
    两种情况下都能跑，而直接指向某个 `.py` 文件只在其中一种情况下成立。

    Windows 上同时给 `.cmd` 与 `.ps1`：`cmd.exe` 只认 `.cmd`，PowerShell
    更愿意执行 `.ps1`，两者都放才能「在任意终端输入 minicode 启动」。
    """
    entry = f'"{sys.executable}" -m minicode.cli'

    posix = "#!/usr/bin/env sh\nset -eu\n" + f'exec "{sys.executable}" -m minicode.cli "$@"\n'
    cmd = f"@echo off\r\n{entry} %*\r\n"
    # **注释刻意用 ASCII。**
    #
    # Windows PowerShell 5.1 读取**无 BOM** 的 UTF-8 文件时按 ANSI 解码，
    # 非 ASCII 字符会变成乱码。注释乱码本身不影响执行，但一旦有人把非 ASCII
    # 写进**代码**（比如路径），就会真的出问题。launcher 保持纯 ASCII 最稳。
    ps1 = f"# MiniCode launcher (PowerShell)\n& {entry} @args\n"
    return {"minicode": posix, "minicode.cmd": cmd, "minicode.ps1": ps1}


def ask_required(
    read_line: Callable[[], str | None],
    label: str,
    default: str = "",
    *,
    stream: TextIO | None = None,
) -> str:
    """问一个必填项，直到拿到非空答案。

    **空白答案回退到默认值**（若有），都没有就重问。原版如此。
    直接接受空值会让 `ANTHROPIC_BASE_URL` 变成空字符串，而后续的报错
    会指向「请求失败」而不是「你没填这一项」。
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

    out.write("mini-code installer\n")
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
        out.write(f"启动命令: {target_bin / 'minicode'}\n")
    out.write("\n")
    if on_path:
        out.write("现在你可以在任意终端输入 `minicode` 启动。\n")
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
