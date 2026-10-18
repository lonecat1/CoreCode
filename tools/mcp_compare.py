"""MCP 桥接的**跨实现对照**：同一个服务器，两边看到的工具与调用结果必须一致。

## 为什么单独做这个

MCP 是**线协议**。前面几轮的对照都在进程内比行为，而这里比的是：
同一台服务器，TS 客户端与 Python 客户端**看到的东西是否一样**。

这件事单靠 Python 的单元测试证明不了——`tests/test_mcp.py` 验的是「本实现
按自己的理解读懂了服务器」，没有第三方证人说「原版也是这么理解的」。而真实
服务器（GitHub / filesystem 之类）不会告诉我们谁对。

所以：**两者共用同一个 fake server**（`tests/fixtures/fake_mcp_server.py`，
两种分帧都实现了），比对

    · 每台服务器的摘要：status / command / toolCount / protocol /
      resourceCount / promptCount
    · 每个工具的对外名字、描述、`inputSchema`
    · 每个工具被调用后的**返回文本**

## 为什么用固定端口不现实

两台客户端各自 `spawn` 一个子进程，互不干扰。服务器用 stdio 分帧，所以
「同一个服务器」指的是**同一份服务器代码**，而不是同一个进程。

## 两种分帧都要跑

`content-length` 与 `newline-json` 是本项目支持的两种线格式。服务器都能说，
所以两边各试一次——协议协商的结果也一并比对。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TS_ROOT = Path(r"D:\CoreCode\MiniCode")
TSX_CLI = TS_ROOT / "node_modules" / "tsx" / "dist" / "cli.mjs"
PY = sys.executable
SERVER = REPO / "tests" / "fixtures" / "fake_mcp_server.py"

#: 两种线格式各跑一次。
PROTOCOLS = ["content-length", "newline-json"]

#: 要调用的工具与参数。`fail` 用来比「错误结果怎么表示」。
#:
#: 也把 4 个**通用工具**列进来——它们的输出文本格式（`server: uri (name) -
#: description`）正是最容易走样的地方，而「工具表一致」看不出来。
CALLS: list[tuple[str, dict]] = [
    ("echo", {"text": "hello"}),
    ("structured", {}),
    ("fail", {}),
    ("greet", {"who": "world"}),  # 故意给不存在的工具名，比「未知工具」的报错
    ("list_mcp_resources", {}),  # 不带 server → 遍历全部
    ("list_mcp_resources", {"server": "fake"}),
    ("list_mcp_resources", {"server": "nope"}),  # 未知服务器
    ("list_mcp_prompts", {}),
    ("list_mcp_prompts", {"server": "fake"}),
    ("read_mcp_resource", {"server": "fake", "uri": "test://readme"}),
    ("read_mcp_resource", {"server": "nope", "uri": "test://readme"}),
    ("get_mcp_prompt", {"server": "fake", "name": "greet", "arguments": {"who": "world"}}),
]


TS_PROBE = r"""
import { readFileSync, mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createMcpBackedTools }
  from "file:///D:/CoreCode/MiniCode/src/mcp.ts";

const cfg = JSON.parse(readFileSync(process.env.MCP_CFG, "utf8"));

(async () => {
  const cwd = mkdtempSync(join(tmpdir(), "mcp-ts-"));
  const bridge = await createMcpBackedTools({
    cwd,
    mcpServers: cfg.servers,
  });

  const tools = bridge.tools.map((t) => ({
    name: t.name,
    description: t.description,
    inputSchema: t.inputSchema ?? null,
  }));

  const calls = [];
  for (const c of cfg.calls) {
    const tool = bridge.tools.find((t) => t.name === c.name);
    if (!tool) {
      calls.push({ name: c.name, ok: false, output: "<not registered>" });
      continue;
    }
    try {
      const result = await tool.run(c.input ?? {});
      calls.push({
        name: c.name,
        ok: result.ok,
        output: String(result.output ?? ""),
      });
    } catch (error) {
      calls.push({ name: c.name, ok: false, output: `THREW: ${String(error)}` });
    }
  }

  const summaries = bridge.servers.map((s) => ({
    name: s.name,
    command: s.command,
    status: s.status,
    toolCount: s.toolCount,
    protocol: s.protocol ?? null,
    resourceCount: s.resourceCount ?? null,
    promptCount: s.promptCount ?? null,
    error: s.error ?? null,
  }));

  await bridge.dispose();
  console.log(JSON.stringify({ summaries, tools, calls }));
})();
"""


def config_payload(protocol: str) -> dict:
    return {
        "servers": {
            "fake": {
                "command": PY,
                "args": [str(SERVER), "--protocol", protocol],
            }
        },
        "calls": [{"name": name, "input": args} for name, args in CALLS],
    }


def run_ts(payload: dict) -> dict:
    handle, cfg_file = tempfile.mkstemp(suffix=".json", prefix="mcp-cfg-")
    os.close(handle)
    try:
        Path(cfg_file).write_text(json.dumps(payload), encoding="utf-8")
        env = {**os.environ, "MCP_CFG": cfg_file}
        proc = subprocess.run(
            ["node", str(TSX_CLI), "--eval", TS_PROBE],
            cwd=str(TS_ROOT), capture_output=True, text=True,
            encoding="utf-8", errors="replace", env=env, timeout=300,
        )
        if proc.returncode != 0:
            raise SystemExit(f"TS 侧失败：\n{proc.stdout}\n{proc.stderr}")
        for line in reversed(proc.stdout.strip().splitlines()):
            if line.startswith("{"):
                return json.loads(line)
        raise SystemExit(f"TS 侧没有输出 JSON：{proc.stdout!r}")
    finally:
        try:
            os.remove(cfg_file)
        except OSError:
            pass


def run_py(payload: dict) -> dict:
    sys.path.insert(0, str(REPO / "src"))
    from minicode.mcp import create_mcp_backed_tools

    cwd = tempfile.mkdtemp(prefix="mcp-py-")
    bridge = create_mcp_backed_tools(cwd, payload["servers"])
    try:
        tools = [
            {
                "name": tool.name,
                "description": tool.description,
                "inputSchema": tool.input_schema,
            }
            for tool in bridge.tools
        ]
        by_name = {tool.name: tool for tool in bridge.tools}
        calls = []
        for name, args in CALLS:
            tool = by_name.get(name)
            if tool is None:
                calls.append({"name": name, "ok": False, "output": "<not registered>"})
                continue
            try:
                result = tool.run(dict(args), None)
                calls.append(
                    {"name": name, "ok": result.ok, "output": str(result.output or "")}
                )
            except Exception as error:  # noqa: BLE001 - 对照要把异常也记下来
                calls.append(
                    {"name": name, "ok": False, "output": f"THREW: {error}"}
                )
        summaries = [
            {
                "name": item.name,
                "command": item.command,
                "status": item.status,
                "toolCount": item.tool_count,
                "protocol": item.protocol,
                "resourceCount": item.resource_count,
                "promptCount": item.prompt_count,
                "error": item.error,
            }
            for item in bridge.summaries
        ]
        return {"summaries": summaries, "tools": tools, "calls": calls}
    finally:
        bridge.dispose()


def normalize(command: str) -> str:
    """`command` 字段里含解释器绝对路径，两边不同（但指向同一份服务器代码）。

    只抹掉**解释器路径**这一项：它的差异是「两边用各自的 Python」，
    不是行为差异。服务器脚本路径本身保留——它是同一个文件。
    """
    return command.replace(PY, "<PY>").replace("\\", "/")


def main() -> int:
    failures: list[str] = []
    for protocol in PROTOCOLS:
        payload = config_payload(protocol)
        ts = run_ts(payload)
        py = run_py(payload)

        print(f"===== {protocol} =====")
        for side in (ts, py):
            for summary in side["summaries"]:
                summary["command"] = normalize(summary["command"])

        # --- 服务器摘要 ---
        if ts["summaries"] != py["summaries"]:
            failures.append(f"{protocol}: 服务器摘要不同")
            print("  DIFF 服务器摘要")
            print(f"    TS: {ts['summaries']}")
            print(f"    PY: {py['summaries']}")
        else:
            s = ts["summaries"][0]
            print(
                f"  OK   摘要 status={s['status']} tools={s['toolCount']} "
                f"protocol={s['protocol']} res={s['resourceCount']} "
                f"prompt={s['promptCount']}"
            )

        # --- 工具表 ---
        ts_tools = {t["name"]: t for t in ts["tools"]}
        py_tools = {t["name"]: t for t in py["tools"]}
        if set(ts_tools) != set(py_tools):
            failures.append(f"{protocol}: 工具集合不同")
            print(f"  DIFF 工具集合：仅TS={set(ts_tools) - set(py_tools)} "
                  f"仅PY={set(py_tools) - set(ts_tools)}")
        else:
            print(f"  OK   工具表 {len(ts_tools)} 个：{sorted(ts_tools)}")
            for name in sorted(ts_tools):
                a, b = ts_tools[name], py_tools[name]
                for field in ("description", "inputSchema"):
                    if a[field] != b[field]:
                        failures.append(f"{protocol}: {name}.{field} 不同")
                        print(f"  DIFF {name}.{field}")
                        print(f"    TS: {a[field]!r}")
                        print(f"    PY: {b[field]!r}")

        # --- 调用结果 ---
        if ts["calls"] != py["calls"]:
            failures.append(f"{protocol}: 调用结果不同")
            print("  DIFF 调用结果")
            for a, b in zip(ts["calls"], py["calls"]):
                if a != b:
                    print(f"    {a['name']}:")
                    print(f"      TS ok={a['ok']} output={a['output']!r}")
                    print(f"      PY ok={b['ok']} output={b['output']!r}")
        else:
            print(f"  OK   调用结果 {len(ts['calls'])} 个全部一致")
        print()

    if failures:
        print(f"不一致：{failures}")
        return 1
    print(f"{len(PROTOCOLS)} 种线格式全部一致")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
