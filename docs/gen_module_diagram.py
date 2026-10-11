# -*- coding: utf-8 -*-
"""Render the minicode-py module dependency diagram to PNG (Pillow).

Layered by dependency depth: everything points downward, nothing points up.

NOTE: always edit this file with a UTF-8 aware tool. A previous version was
mangled by round-tripping it through PowerShell `Get-Content`/`Set-Content`,
which decoded UTF-8 as the system ANSI codepage and destroyed the CJK text.
"""
import os
from PIL import Image, ImageDraw, ImageFont

S = 2
W, H = 1220, 1260
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "minicode-py-modules.png")

FONT_CJK = r"C:\Windows\Fonts\msyh.ttc"
FONT_CJK_B = r"C:\Windows\Fonts\msyhbd.ttc"
FONT_MONO = r"C:\Windows\Fonts\consola.ttf"

INK = (38, 36, 34)
MUTED = (118, 112, 106)
BG = (252, 250, 247)
LINE = (188, 181, 172)
GREEN = (58, 138, 96)
GREY = (170, 164, 156)

img = Image.new("RGB", (W * S, H * S), BG)
d = ImageDraw.Draw(img)


def f(path, size):
    return ImageFont.truetype(path, size * S)


F = {
    "h1": f(FONT_CJK_B, 24),
    "h2": f(FONT_CJK_B, 13),
    "b": f(FONT_CJK, 11),
    "bs": f(FONT_CJK, 10),
    "mono": f(FONT_MONO, 11),
    "mono_b": f(FONT_CJK_B, 12),
    "tag": f(FONT_CJK_B, 10),
}


def px(v):
    return int(round(v * S))


def tw(s, font):
    return d.textlength(s, font=font) / S


def has_cjk(text):
    return any("\u4e00" <= ch <= "\u9fff" or "\u3000" <= ch <= "\u303f" for ch in text)


def title_font(text):
    """Consolas has no CJK glyphs; a missing glyph renders as garbage, silently.

    So any box title containing Chinese must use the CJK face instead.
    """
    return F["mono"] if not has_cjk(text) else F["mono_b"]


def box(x, y, w, h, title, lines, fill, edge, tested=None):
    d.rounded_rectangle(
        [px(x), px(y), px(x + w), px(y + h)], radius=px(7), fill=fill,
        outline=edge, width=max(1, px(0.9)),
    )
    d.text((px(x + 10), px(y + 7)), title, font=title_font(title), fill=INK)
    if tested:
        label = f"{tested} 测试"
        d.text((px(x + w - 10 - tw(label, F["tag"])), px(y + 8)), label,
               font=F["tag"], fill=GREEN)
    for i, line in enumerate(lines):
        d.text((px(x + 10), px(y + 25 + i * 14)), line, font=F["bs"], fill=MUTED)


def arrow(x1, y1, x2, y2, color=GREY, dashed=False, label=None, lw=1.3):
    import math
    if dashed:
        dx, dy = x2 - x1, y2 - y1
        dist = math.hypot(dx, dy)
        if dist < 1:
            return
        ux, uy = dx / dist, dy / dist
        t = 0.0
        while t < dist - 5:
            t2 = min(t + 3.5, dist - 5)
            d.line([px(x1 + ux * t), px(y1 + uy * t), px(x1 + ux * t2), px(y1 + uy * t2)],
                   fill=color, width=max(1, px(lw)))
            t += 6.5
    else:
        d.line([px(x1), px(y1), px(x2), px(y2)], fill=color, width=max(1, px(lw)))
    ang = math.atan2(y2 - y1, x2 - x1)
    for s in (0.9, -0.9):
        d.line([px(x2), px(y2),
                px(x2 - 5 * math.cos(ang - s * 0.38)),
                px(y2 - 5 * math.sin(ang - s * 0.38))],
               fill=color, width=max(1, px(lw)))
    if label:
        w = tw(label, F["bs"])
        lx, ly = (x1 + x2) / 2 - w / 2, (y1 + y2) / 2 - 13
        d.rectangle([px(lx - 3), px(ly - 1), px(lx + w + 3), px(ly + 12)], fill=BG)
        d.text((px(lx), px(ly)), label, font=F["bs"], fill=MUTED)


# ---------------- header ----------------
d.text((px(30), px(18)), "minicode-py 模块依赖图", font=F["h1"], fill=INK)
d.text((px(30), px(50)),
       "箭头指向被依赖方（全部向下）· 零第三方运行时依赖 · 绿色数字为该模块的测试数",
       font=F["b"], fill=MUTED)

# ---------------- L0 foundations ----------------
d.text((px(30), px(84)), "L0  基础层", font=F["h2"], fill=MUTED)
box(30, 106, 190, 66, "types.py", ["消息联合类型", "AgentStep / 回合结局"], (243, 238, 230), LINE)
box(232, 106, 200, 66, "abort.py", ["贯穿式取消信号", "AbortSignal / AbortError"], (243, 238, 230), LINE)

# ---------------- L1 contract ----------------
d.text((px(30), px(190)), "L1  契约层", font=F["h2"], fill=MUTED)
box(30, 212, 402, 80, "tooling.py",
    ["ToolDefinition（name / description / input_schema / validator / run）",
     "ToolRegistry（list / subset / add_tools / execute / dispose）",
     "ToolResult 四个控制位：await_user · stop · fatal · background_task"],
    (233, 242, 236), (150, 186, 162), tested=6)

# ---------------- L2 core ----------------
d.text((px(30), px(310)), "L2  核心引擎（本模块不依赖任何上层）", font=F["h2"], fill=MUTED)
box(30, 332, 402, 96, "agent_loop.py",
    ["run_agent_turn —— model → tool → model 主循环",
     "只 import：types · tooling · abort（无 UI / 无会话 / 无权限提示）",
     "TurnHooks 回调面 + ContextPipeline 压缩接缝",
     "五种响应分类：progress / thinking 截断 / 空响应 / 重试耗尽 / 完成"],
    (245, 227, 218), (196, 106, 74), tested=16)

# ---------------- L3 host-facing ----------------
d.text((px(30), px(446)), "L3  宿主与能力层", font=F["h2"], fill=MUTED)
box(30, 468, 194, 78, "adapters/", ["anthropic.py  模型后端", "mock.py  离线演示"],
    (228, 235, 243), (150, 176, 204), tested=22)
box(234, 468, 198, 78, "permissions.py", ["路径 / 命令 / 编辑三入口", "提示函数注入 · 四级授权"],
    (247, 234, 231), (206, 150, 140), tested=25)
box(442, 468, 190, 78, "file_review.py", ["写前 unified diff", "无变化则跳过落盘"],
    (236, 240, 234), (166, 190, 160), tested=0)

# ---------------- L4 context ----------------
d.text((px(30), px(562)), "L4  上下文工程（四级压缩 + 计量）", font=F["h2"], fill=MUTED)
box(30, 584, 402, 104, "context/",
    ["pipeline.py    编排四级顺序 · 唯一接缝",
     "tokens.py      四级阈值 · provider-usage 优先计量",
     "microcompact / snip / collapse / compact 四级机制",
     "tool_result_storage  大结果落盘 + 批预算"],
    (245, 238, 226), (198, 172, 132), tested=107)

# ---------------- L5 session ----------------
d.text((px(30), px(704)), "L5  会话持久化", font=F["h2"], fill=MUTED)
box(30, 726, 402, 62, "session/",
    ["store.py   append-only JSONL · 边界事件 · snip 回放 · fork/resume",
     "恢复只读最后一个压缩边界之后；transcript 保留压缩前历史"],
    (232, 238, 244), (150, 176, 204), tested=55)

# ---------------- L6 runtime ----------------
d.text((px(30), px(804)), "L6  运行时编排", font=F["h2"], fill=MUTED)
box(30, 826, 402, 104, "runtime/",
    ["session.py   四道互斥闸门 · 宿主唯一门面",
     "turn.py      单飞闸门（50 行，Goal 与 Loop 复用它）",
     "goal.py / goal_controller.py   目标状态机 + 跨回合推进",
     "loop.py      固定延迟重复任务 · plan.py  内存 Todo 清单"],
    (238, 234, 244), (168, 154, 200), tested=79)

# ---------------- L7 agents ----------------
d.text((px(30), px(940)), "L7  子 Agent", font=F["h2"], fill=MUTED)
box(30, 962, 402, 78, "agents/",
    ["manager.py   隔离上下文 · 受限工具 · ≤3 并发 · 生命周期",
     "worker_prompt.py   只读系统提示（明说不能改代码）",
     "tools/subagents.py  spawn / list / wait / close（仅根可见）"],
    (244, 236, 228), (204, 158, 120), tested=24)

# ---------------- right column ----------------
box(680, 106, 240, 74, "config.py", ["模型 / 凭据 / 上限解析", "settings.json 合并"],
    (243, 238, 230), LINE, tested=6)
box(680, 196, 240, 74, "workspace.py", ["路径围栏", "越界即抛或征询"],
    (243, 238, 230), LINE, tested=0)
box(680, 286, 240, 74, "tools/", ["files · grep_files（rg + 回退）", "web_fetch / web_search · load_skill"],
    (236, 242, 236), (150, 186, 162), tested=24)
box(680, 376, 240, 74, "skills.py",
    ["SKILL.md 四层发现 · 优先级去重", "load_skill 按需加载 · 名字校验"],
    (236, 242, 236), (150, 186, 162), tested=52)
box(680, 466, 240, 100, "mcp/",
    ["jsonrpc.py     两种分帧状态机",
     "stdio_client   子进程 + 帧协商 + 缓存",
     "http_client    streamable HTTP",
     "bridge.py      远端工具 → 本地工具"],
    (236, 240, 244), (140, 170, 200), tested=53)
box(680, 586, 240, 112, "tui/  差分帧渲染",
    ["frames.py    只重绘变化的行（纯函数）",
     "width.py     CJK/Emoji 占 2 列",
     "panels.py    面板 · 徽章 · 横幅",
     "app.py       状态 + 按键（纯函数）",
     "terminal.py  终端读写（薄层）"],
    (240, 236, 244), (160, 140, 190), tested=115)
box(680, 716, 240, 66, "docs/ 与 tools/",
    ["divergences.md   22 条有意差异",
     "check_imports.py  导入静态检查",
     "mutation_check.py 自愈式变异检查"],
    (240, 238, 232), LINE)

# ---------------- dependency arrows ----------------
arrow(125, 172, 125, 212, dashed=True)                     # L0 -> L1
arrow(300, 172, 300, 332, dashed=True)                     # L0 -> L2
arrow(16, 244, 16, 328, label="ToolRegistry", dashed=True)  # L1 -> L2
arrow(120, 428, 120, 468, dashed=True)                     # L2 -> L3
arrow(320, 428, 320, 468, dashed=True)
arrow(300, 428, 300, 584, color=(196, 150, 112), dashed=True)  # L2 -> L4
arrow(200, 688, 200, 726, color=(150, 176, 204), dashed=True)  # L4 -> L5
arrow(120, 788, 120, 826, color=(168, 154, 200), dashed=True)  # L5 -> L6
arrow(320, 428, 320, 826, color=(168, 154, 200), dashed=True)  # L2 -> L6（runtime 调用回合）
arrow(200, 930, 200, 962, color=(204, 158, 120), dashed=True)  # L6 -> L7
arrow(320, 428, 380, 962, color=(204, 158, 120), dashed=True)  # L2 -> L7（worker 也走同一个循环）
arrow(224, 507, 234, 507)                                  # L3 内部
arrow(432, 507, 442, 507)
arrow(680, 300, 640, 300, dashed=True)                     # 右列依赖
arrow(680, 210, 640, 210, dashed=True)

# ---------------- footer ----------------
d.line([px(30), px(1096), px(1150), px(1096)], fill=(224, 218, 208), width=max(1, px(0.9)))
d.text((px(30), px(1108)), "边界契约（三条不可动摇的规则）", font=F["h2"], fill=INK)
notes = [
    "1. agent_loop 不知道 UI、会话与权限提示的存在 —— 宿主通过 TurnHooks 注入全部输出行为。",
    "2. 工具通过 ToolResult 的四个控制位反向影响控制流，而不是靠循环里的 if tool_name == ...。",
    "3. 所有写操作收敛到 file_review.apply_reviewed_file_change()：读旧 → 比对 → diff → 批准 → 落盘。",
]
for i, note in enumerate(notes):
    d.text((px(30), px(1132 + i * 19)), note, font=F["b"], fill=MUTED)
d.text((px(30), px(1196)),
       "已实现 64 个模块 · 595 项测试通过 · 导入检查 0 问题 · 变异检查 29/29 · divergences.md 24 条",
       font=F["bs"], fill=(150, 142, 132))

img = img.crop((0, 0, W * S, 1224 * S)).resize((W, 1224), Image.LANCZOS)
img.save(OUT, "PNG")
print("wrote", OUT, img.size)
