# Skill 分层路由

## 目标

当 Skill 从几十个增长到几百个时，不能继续把全部名称和描述塞进系统提示，也
不能让模型只靠记忆猜技能名。路由系统把空间分成三层，并在每次模型调用前
只注入与当前任务相关的少数 Skill 卡片。

```text
原子 Tool
  read_file / grep_files / edit_file / run_command / ...
        │
        ▼
高级 Skill
  SKILL.md + category/tags/intents/boundaries/examples
        │
        ▼
Skill 目录
  category / subcategory 的数量摘要
```

磁盘上同时支持 `skills/<name>/SKILL.md` 和
`skills/<category>/<name>/SKILL.md`；后者父目录名作为默认分类。Subcategory
可通过 front matter 进一步细分。

## 元信息

Skill 可以采用兼容的可选 YAML front matter：

```markdown
---
description: Review Python changes for security
category: quality
subcategory: review
tags: [python, security, review]
intents: [code_review, security]
when_to_use: Reviewing Python pull requests
when_not_to_use: Production deployment
examples: ["审查这个 Python 补丁", "check security of this PR"]
priority: 0.9
capabilities: [read_file, grep_files]
---
```

解析器只实现 YAML 的保守子集，零第三方依赖，支持：

- 标量和数字；
- `[a, b]` JSON/逗号列表；
- `- item` 块列表；
- `|` / `>` 多行文本。

旧 Skill 不写 front matter 也能使用：描述从正文提取，标签从名称/描述推导，
意图按关键词识别，适用边界从 `## When to use` / `## When not to use` /
中文对应标题中读取，示例从 `## Examples` 读取。

## 两阶段路由

### 第一阶段：粗召回

对全部 Skill 计算轻量分数：

```text
lexical overlap + intent match + tag overlap + priority - boundary penalty
```

按分数保留最多 32 个候选。候选空间从此不再等于全部 Skill 数量。

### 第二阶段：精排

只对粗召回候选计算更细的加权分：

```text
lexical 30%
tag     23%
intent  22%
example 13%
priority 12%
boundary penalty -45%
```

随后做重叠消除：标签与描述相似度超过阈值的两个 Skill 不会同时进入最终结果，
高分者保留。最终默认只返回 3 个 Skill。

## 原子 Tool 建议

路由器根据意图和 Skill 的 `capabilities` 建议相关原子工具：

- code review → `read_file` / `grep_files` / `list_files`
- implementation → 读取、搜索、编辑、验证
- verification → `run_command` / `read_file`
- documentation → 读取与写入
- security → 搜索、读取、验证

Tool 的完整 schema 仍由注册表提供；这里只增加任务相关的工具提示，不改变权限
边界。

## Token 预算

系统提示不再列出所有技能，只显示：

```text
Available skill catalog: 120 skills in 8 categories (quality=20, ...).
Relevant skill cards are routed from the current task.
```

每次模型调用前动态注入 `<skill_routing>`：

- 识别出的意图；
- 目录总数、分类数、粗候选数和重叠丢弃数；
- top-k Skill 的分类、标签、适用边界和第一条示例；
- 建议的原子工具；
- `load_skill(name="...")` 的加载提示。

Skill 正文仍然不预加载，只有模型明确调用 `load_skill` 时才读取。
路由上下文默认硬上限 2,000 字符；front matter 在加载正文时会被剥离，避免
元信息重复占用 Token。

## Skill 增长

自进化生成的新 Skill 现在会写入 front matter，包括：

- `category: evolved`
- 从程序经验推导的 tags/intents/capabilities
- `when_to_use` / `when_not_to_use`
- 实际任务示例
- 随出现次数增加的 priority

宿主刷新 Skill 后同步刷新路由器，因此新 Skill 不需要重启即可参与路由。
根 Agent、调研/Fork worker、Worktree coder worker 共用同一份路由缓存。

## 兼容性

- `load_skill(name)` 的精确名称加载保持不变；
- 技能发现位置和项目 > 用户 > Claude 兼容目录的优先级不变；
- 没有 front matter 的旧 Skill 仍可被发现、加载和路由；
- 路由失败不会中断回合，只会退化为目录摘要。

## 测试

`tests/test_skill_routing.py` 覆盖：

- front matter 与旧 Skill 回退解析；
- 中英混合意图识别；
- 相关 Skill 精排；
- 边界惩罚；
- 功能重叠去重；
- 45 个 Skill 下的阶段一上限、top-k 与 Token 预算；
- 系统提示只显示目录摘要；
- 宿主与子 Agent 的动态 Skill 注入；
- 自进化 Skill 元信息可被重新路由。
