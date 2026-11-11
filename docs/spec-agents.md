# 中心化多 Agent 协作

## 原则

这是一个**中心化星形拓扑**，不是 Agent 社会：

```text
                 user
                  │
                  ▼
             root Agent
        plan / spawn / wait /
        inspect / approve / apply
          │       │       │
          ▼       ▼       ▼
       worker   worker   worker
```

- 根 Agent 是唯一计划者、审批者、质量门禁和代码合并者。
- 子 Agent 只能通过根 Agent 的 Tool Call 创建。
- 子 Agent 之间没有消息、共享黑板、投票或共识协议。
- 控制权从不移交给子 Agent；根 Agent 只等待最小报告。
- Team 是扁平分组，不是独立调度器。

## 三种 worker 模式

### `research`

只读调研。工具注册表只有：

```text
list_files  grep_files  read_file  load_skill  web_fetch  web_search
```

worker 没有 `write_file`、`edit_file`、`run_command`、`ask_user` 或任何
Agent 管理工具。工作区越界路径直接拒绝。

### `fork`

主 Agent 通过 `set_fork_context_provider()` 注入当前对话快照。spawn 时只复制
最近最多 16K 字符的用户/assistant 文本，不复制系统提示、工具消息或实时状态。
之后 worker 与根会话完全独立，只交回最后一条 assistant 报告。

### `worktree`

从干净 Git 仓库的 `HEAD` 创建 detached worktree：

```text
<MINICODE_HOME>/agent-worktrees/<project-key>/<agent-id>-<nonce>/
```

根工作区有未提交改动时拒绝创建，避免 worker 基于过期 HEAD 产出错误 patch。
worker 可以读取、写文件、编辑和运行受限验证命令，但：

- 所有路径必须位于该 worktree；
- 命令参数中的绝对路径和 `..` 逃逸也会被拒绝；
- 不提供 `ask_user` 或 Agent 管理工具；
- 不使用交互审批，改为保守命令白名单；
- 不允许 commit、push、reset、clean、worktree 操作；
- 不直接把改动写入根工作区。

允许的命令以测试、构建、类型检查和只读 Git 为主，例如 `pytest`、
`python -m pytest`、`npm test`、`cargo test`、`go test`、
`git status/diff/log`。`npm run` 只允许 `test/build/lint/typecheck/check`
脚本；shell 片段、任意解释器脚本和其他生命周期脚本被拒绝。

## Team 模式

`spawn_team` 一次创建 1–3 个成员，并为它们附加同一个 `team_id` 和
`team_name`。每个成员仍是独立的：

- 消息历史；
- 工具注册表；
- worktree（若使用 worktree 模式）；
- 状态与报告。

团队没有共享可变状态。主 Agent 通过 `list_agents(team=...)` 查看成员，
通过 `wait_agent` 收集报告，再逐项审阅和合并。需要依赖关系时应由主 Agent
分阶段启动团队，而不是让成员互相协商。

## 质量门禁

Worktree worker 在 spawn 时必须提供非空 `acceptance`：

```text
spawn_agent(mode="worktree", acceptance="tests X pass and file Y exists")
```

完成后的合并链路是严格分步的：

1. `wait_agent` 取得最小报告、改动文件列表和 patch 统计；
2. `inspect_agent_changes` 读取完整 patch，并记录 `inspected=true`；
3. `apply_agent_changes` 校验状态、acceptance 与审阅标记；
4. 根权限层请求用户批准；
5. 在根工作区执行 `git apply --check`；
6. check 通过后才执行 `git apply`；
7. 根 Agent 在根工作区运行最终验证。

合并失败不会留下半应用状态。重复合并同一 Agent 是幂等的。

## 生命周期

每个 worker 的状态为：

```text
running → completed / failed / closed
```

- `wait_agent` 超时只返回状态，不关闭 worker；
- `close_agent` 可中断 worker；
- `delete_worktree=true` 仅在 worker 线程已停止后删除隔离树；
- 父回合取消会传播到 worker；
- 并发上限为 3，团队一次最多 3 人；
- worker 不能创建更多 worker。

## Tool Call 接口

根 Agent 可见 7 个管理工具：

| 工具 | 作用 |
|---|---|
| `spawn_agent` | 创建 research/fork/worktree worker |
| `spawn_team` | 创建扁平 Team |
| `list_agents` | 查看状态，可按 team 过滤 |
| `wait_agent` | 收集最小报告 |
| `close_agent` | 中断并可删除 worktree |
| `inspect_agent_changes` | 审阅 worktree patch |
| `apply_agent_changes` | 审批后合并 patch |

这些工具不会进入调研 worker 或 coder worker 的注册表。

## 与 Session Fork 的区别

`/fork` 与 `--fork` 仍然表示**会话**分叉：复制 JSONL 事件日志，便于从历史
状态继续。Agent `fork` 模式表示**一次子任务**接收主对话快照，执行结束后只把
短报告交回主 Agent。两者互补，但不应混为一谈。

## 测试

`tests/test_agent_collaboration.py` 覆盖：

- worktree 改动在合并前不进入根工作区；
- 必须先 inspect 才能 apply；
- 用户审批拒绝时不落盘；
- 根工作区在审阅后发生冲突修改时，`git apply --check` 失败且不半应用；
- worktree 路径越界和任意 shell 被拒绝；
- 脏根工作区拒绝启动 worktree；
- Fork 只复制文本快照；
- Team 成员共享 team 元数据但没有消息通道；
- 两个 worktree 成员可并行修改不同文件，并由主 Agent 逐个审阅合并；
- 合并后删除 worktree。
