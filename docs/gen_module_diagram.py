# -*- coding: utf-8 -*-
"""Render the minicode-py module dependency diagram to PNG (Pillow).

Layered by dependency depth: everything points downward, nothing points up.
"""
import os
from PIL import Image, ImageDraw, ImageFont

S = 2
W, H = 1220, 960
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "minicode-py-modules.png")

FONT_CJK = r"C:\Windows\Fonts\msyh.ttc"
FONT_CJK_B = r"C:\Windows\Fonts\msyhbd.ttc"
FONT_MONO = r"C:\Windows\Fonts\consola.ttf"
FONT_MONO_B = r"C:\Windows\Fonts\consolab.ttf"

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
    "mono_s": f(FONT_MONO, 10),
    "tag": f(FONT_CJK_B, 10),
}


def px(v):
    return int(round(v * S))


def tw(s, font):
    return d.textlength(s, font=font) / S


def has_cjk(text):
    return any("\u4e00" <= ch <= "\u9fff" or "\u3000" <= ch <= "\u303f" for ch in text)


def title_font(text):
    """绛夊瀛椾綋锛圕onsolas锛夋病鏈変腑鏂囧瓧褰紝鍚腑鏂囨椂蹇呴』鎹㈢敤闆呴粦銆?
    杩欐鏄箣鍓嶆爣棰樻覆鏌撴垚涔辩爜鐨勫師鍥狅細瀛椾綋缂哄瓧褰笉浼氭姤閿欙紝鍙細鐢诲嚭閿欏瓧銆?    """
    return F["mono"] if not has_cjk(text) else F["mono_b"]


def box(x, y, w, h, title, lines, fill, edge, tested=None):
    d.rounded_rectangle(
        [px(x), px(y), px(x + w), px(y + h)], radius=px(7), fill=fill,
        outline=edge, width=max(1, px(0.9)),
    )
    font = title_font(title)
    d.text((px(x + 10), px(y + 7)), title, font=font, fill=INK)
    if tested:
        label = f"{tested} 娴嬭瘯"
        d.text((px(x + w - 10 - tw(label, F["tag"])), px(y + 8)), label,
               font=F["tag"], fill=GREEN)
    for i, line in enumerate(lines):
        d.text((px(x + 10), px(y + 25 + i * 14)), line, font=F["bs"], fill=MUTED)


def arrow(x1, y1, x2, y2, color=GREY, dashed=False, label=None, lw=1.3):
    if dashed:
        import math
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
    import math
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
d.text((px(30), px(18)), "minicode-py 妯″潡渚濊禆鍥?, font=F["h1"], fill=INK)
d.text((px(30), px(50)),
       "绠ご鎸囧悜琚緷璧栨柟锛堝叏閮ㄥ悜涓嬶級路 闆剁涓夋柟杩愯鏃朵緷璧?路 缁胯壊鏁板瓧涓鸿妯″潡鐨勬祴璇曟暟",
       font=F["b"], fill=MUTED)

# ---------------- L0 foundations ----------------
d.text((px(30), px(84)), "L0  鍩虹灞?, font=F["h2"], fill=MUTED)
box(30, 106, 190, 66, "types.py", ["娑堟伅鑱斿悎绫诲瀷", "AgentStep / 鍥炲悎缁撳眬"], (243, 238, 230), LINE)
box(232, 106, 200, 66, "abort.py", ["璐┛寮忓彇娑堜俊鍙?, "AbortSignal / AbortError"], (243, 238, 230), LINE)

# ---------------- L1 contract ----------------
d.text((px(30), px(190)), "L1  濂戠害灞?, font=F["h2"], fill=MUTED)
box(30, 212, 402, 80, "tooling.py",
    ["ToolDefinition锛坣ame / description / input_schema / validator / run锛?,
     "ToolRegistry锛坙ist / subset / add_tools / execute / dispose锛?,
     "ToolResult 鍥涗釜鎺у埗浣嶏細await_user 路 stop 路 fatal 路 background_task"],
    (233, 242, 236), (150, 186, 162), tested=6)

# ---------------- L2 core ----------------
d.text((px(30), px(310)), "L2  鏍稿績寮曟搸锛堟湰妯″潡涓嶄緷璧栦换浣曚笂灞傦級", font=F["h2"], fill=MUTED)
box(30, 332, 402, 96, "agent_loop.py",
    ["run_agent_turn 鈥斺€?model 鈫?tool 鈫?model 涓诲惊鐜?,
     "鍙?import锛歵ypes 路 tooling 路 abort锛堟棤 UI / 鏃犱細璇?/ 鏃犳潈闄愭彁绀猴級",
     "TurnHooks 鍥炶皟闈?+ ContextPipeline 鍘嬬缉鎺ョ紳",
     "浜旂鍝嶅簲鍒嗙被锛歱rogress / thinking 鎴柇 / 绌哄搷搴?/ 閲嶈瘯鑰楀敖 / 瀹屾垚"],
    (245, 227, 218), (196, 106, 74), tested=16)

# ---------------- L3 host-facing ----------------
d.text((px(30), px(446)), "L3  瀹夸富涓庤兘鍔涘眰", font=F["h2"], fill=MUTED)
box(30, 468, 194, 78, "adapters/", ["anthropic.py  妯″瀷鍚庣", "mock.py  绂荤嚎婕旂ず"],
    (228, 235, 243), (150, 176, 204), tested=22)
box(234, 468, 198, 78, "permissions.py", ["璺緞 / 鍛戒护 / 缂栬緫涓夊叆鍙?, "鎻愮ず鍑芥暟娉ㄥ叆 路 鍥涚骇鎺堟潈"],
    (247, 234, 231), (206, 150, 140), tested=25)
box(442, 468, 190, 78, "file_review.py", ["鍐欏墠 unified diff", "鏃犲彉鍖栧垯璺宠繃钀界洏"],
    (236, 240, 234), (166, 190, 160), tested=0)

# ---------------- L4 context ----------------
d.text((px(30), px(562)), "L4  涓婁笅鏂囧伐绋嬶紙鍥涚骇鍘嬬缉 + 璁￠噺锛?, font=F["h2"], fill=MUTED)
box(30, 584, 402, 104, "context/",
    ["pipeline.py    缂栨帓鍥涚骇椤哄簭 路 鍞竴鎺ョ紳",
     "tokens.py      鍥涚骇闃堝€?路 provider-usage 浼樺厛璁￠噺",
     "microcompact / snip / collapse / compact 鍥涚骇鏈哄埗",
     "tool_result_storage  澶х粨鏋滆惤鐩?+ 鎵归绠?],
    (245, 238, 226), (198, 172, 132), tested=107)

# ---------------- L5 session ----------------
d.text((px(30), px(704)), "L5  浼氳瘽鎸佷箙鍖?, font=F["h2"], fill=MUTED)
box(30, 726, 402, 62, "session/",
    ["store.py   append-only JSONL 路 杈圭晫浜嬩欢 路 snip 鍥炴斁 路 fork/resume",
     "鎭㈠鍙鏈€鍚庝竴涓帇缂╄竟鐣屼箣鍚庯紱transcript 淇濈暀鍘嬬缉鍓嶅巻鍙?],
    (232, 238, 244), (150, 176, 204), tested=55)

# ---------------- right column: consumers ----------------
box(680, 106, 240, 74, "config.py", ["妯″瀷 / 鍑嵁 / 涓婇檺瑙ｆ瀽", "settings.json 鍚堝苟"], (243, 238, 230), LINE, tested=6)
box(680, 196, 240, 74, "workspace.py", ["璺緞鍥存爮", "瓒婄晫鍗虫姏鎴栧緛璇?], (243, 238, 230), LINE, tested=0)
box(680, 286, 240, 74, "tools/", ["files.py  list/read/write", "ask_user.py  鎸傝捣绛夊洖绛?], (236, 242, 236), (150, 186, 162), tested=6)

box(680, 376, 240, 106, "寰呭疄鐜版ā鍧?,
    ["runtime/       鍗曢 路 Goal 路 Loop", "tui/           宸垎甯ф覆鏌?,
     "skills.py / mcp.py", "瀛?Agent 鐢熷懡鍛ㄦ湡", "CLI 鍏ュ彛涓庨厤缃杞?], (240, 238, 232), LINE)

# ---------------- dependency arrows ----------------
# L0 -> L1
arrow(125, 172, 125, 212, dashed=True)
# L0 -> L2
arrow(300, 172, 300, 332, dashed=True)
# L1 -> L2锛堟爣娉ㄦ斁鍦ㄥ乏渚х┖鐧斤紝閬垮厤鍘嬩綇濂戠害灞傛鏂囷級
arrow(16, 244, 16, 328, label="ToolRegistry", dashed=True)
# L2 -> L3
arrow(120, 428, 120, 468, dashed=True)
arrow(320, 428, 320, 468, dashed=True)
# L2 -> L4锛坅gent_loop 閫氳繃 ContextPipeline 鎺ョ紳璋冪敤 context锛?arrow(300, 428, 300, 584, color=(196, 150, 112), dashed=True)
# L4 -> L5锛坈ontext 鐨勮竟鐣屼簨浠剁敱 session 钀界洏锛?arrow(200, 688, 200, 726, color=(150, 176, 204), dashed=True)
# L3 internal
arrow(224, 507, 234, 507)
arrow(432, 507, 442, 507)
# right column deps
arrow(680, 300, 640, 300, dashed=True)
arrow(680, 210, 640, 210, dashed=True)

# ---------------- footer note ----------------
d.line([px(30), px(806), px(1150), px(806)], fill=(224, 218, 208), width=max(1, px(0.9)))
d.text((px(30), px(818)), "杈圭晫濂戠害锛堜笁鏉′笉鍙姩鎽囩殑瑙勫垯锛?, font=F["h2"], fill=INK)
notes = [
    "1. agent_loop 涓嶇煡閬?UI銆佷細璇濅笌鏉冮檺鎻愮ず鐨勫瓨鍦?鈥斺€?瀹夸富閫氳繃 TurnHooks 娉ㄥ叆鍏ㄩ儴杈撳嚭琛屼负銆?,
    "2. 宸ュ叿閫氳繃 ToolResult 鐨勫洓涓帶鍒朵綅鍙嶅悜褰卞搷鎺у埗娴侊紝鑰屼笉鏄潬寰幆閲岀殑 if tool_name == ...銆?,
    "3. 鎵€鏈夊啓鎿嶄綔鏀舵暃鍒?file_review.apply_reviewed_file_change()锛氳鏃?鈫?姣斿 鈫?diff 鈫?鎵瑰噯 鈫?钀界洏銆?,
]
for i, note in enumerate(notes):
    d.text((px(30), px(842 + i * 19)), note, font=F["b"], fill=MUTED)
d.text((px(30), px(906)),
       "宸插疄鐜?27 涓ā鍧?路 239 椤规祴璇曞叏閮ㄩ€氳繃 路 鍦?docs/divergences.md 璁板綍浜?17 鏉′笌鍘熷疄鐜扮殑鏈夋剰宸紓",
       font=F["bs"], fill=(150, 142, 132))

img = img.crop((0, 0, W * S, 932 * S)).resize((W, 932), Image.LANCZOS)
img.save(OUT, "PNG")
print("wrote", OUT, img.size)
