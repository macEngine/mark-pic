#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
md2xhs.py — 纯命令行：把一个 Markdown 文件渲染成适合小红书的图片（3:4 卡片，自动分页）。

不打开浏览器，纯 Pillow 排版。

用法:
    python3 md2xhs.py <markdown 文件路径>
    python3 md2xhs.py <md> -o 输出目录 --theme indigo --width 1080

默认: 输出到 ./output/，3:4（1080x1440），主题 indigo，每个 ## 段落开新卡片，
内容过长自动续页，目标 3~4 张。
"""

import argparse
import datetime
import os
import re
import sys
from PIL import Image, ImageDraw, ImageFont, ImageFilter

# ----------------------------------------------------------------------------
# 字体路径（macOS）
# ----------------------------------------------------------------------------
F_CJK = "/System/Library/Fonts/Hiragino Sans GB.ttc"          # idx0 W3 / idx2 W6
F_SERIF = "/System/Library/Fonts/Supplemental/Songti.ttc"      # idx6 Regular / idx1 Bold
F_MONO = "/System/Library/Fonts/Menlo.ttc"                     # idx0 Regular / idx1 Bold
F_IPA = "/Library/Fonts/Arial Unicode.ttf"                     # 覆盖 IPA 音标
F_EMOJI = "/System/Library/Fonts/Apple Color Emoji.ttc"
EMOJI_STRIKE = 96  # Apple Color Emoji 在 Pillow 中可用的位图字号


# ----------------------------------------------------------------------------
# 主题（背景渐变 + 强调色）
# ----------------------------------------------------------------------------
THEMES = {
    "mono":    {"from": (245, 245, 247), "to": (228, 228, 232), "accent": (28, 28, 30)},
    "indigo":  {"from": (102, 126, 234), "to": (118, 75, 162),  "accent": (99, 102, 241)},
    "sunset":  {"from": (255, 154, 158), "to": (250, 208, 196), "accent": (236, 100, 75)},
    "ocean":   {"from": (33, 147, 176),  "to": (109, 213, 237), "accent": (14, 116, 144)},
    "forest":  {"from": (56, 142, 60),   "to": (165, 214, 167), "accent": (27, 94, 32)},
    "ink":     {"from": (44, 62, 80),    "to": (76, 92, 116),   "accent": (52, 73, 94)},
    "peach":   {"from": (255, 175, 189), "to": (255, 195, 160), "accent": (217, 91, 96)},
}


# ----------------------------------------------------------------------------
# 字符分类
# ----------------------------------------------------------------------------
def is_cjk(ch: str) -> bool:
    o = ord(ch)
    return (
        0x3400 <= o <= 0x9FFF or 0xF900 <= o <= 0xFAFF or
        0xFF00 <= o <= 0xFFEF or 0x3000 <= o <= 0x303F or
        o in (0x2014, 0x2026, 0x00B7) or 0x2018 <= o <= 0x201F
    )


def is_ipa(ch: str) -> bool:
    o = ord(ch)
    return 0x0250 <= o <= 0x02FF or 0x0300 <= o <= 0x036F or 0x1D00 <= o <= 0x1D7F


def is_emoji(ch: str) -> bool:
    o = ord(ch)
    return (
        o >= 0x1F000 or 0x2600 <= o <= 0x27BF or 0x2B00 <= o <= 0x2BFF or
        0x2190 <= o <= 0x21FF or o in (0x2122, 0x2139) or 0xFE00 <= o <= 0xFE0F
    )


# ----------------------------------------------------------------------------
# 字体缓存
# ----------------------------------------------------------------------------
class FontBook:
    def __init__(self):
        self._cache = {}

    def get(self, face: str, size: int) -> ImageFont.FreeTypeFont:
        key = (face, size)
        if key in self._cache:
            return self._cache[key]
        if face == "cjk":
            f = ImageFont.truetype(F_CJK, size, index=0)
        elif face == "cjk_bold":
            f = ImageFont.truetype(F_CJK, size, index=2)
        elif face == "serif":
            f = ImageFont.truetype(F_SERIF, size, index=6)
        elif face == "serif_bold":
            f = ImageFont.truetype(F_SERIF, size, index=1)
        elif face == "mono":
            f = ImageFont.truetype(F_MONO, size, index=0)
        elif face == "mono_bold":
            f = ImageFont.truetype(F_MONO, size, index=1)
        elif face == "ipa":
            f = ImageFont.truetype(F_IPA, size)
        else:
            f = ImageFont.truetype(F_CJK, size, index=0)
        self._cache[key] = f
        return f


# ----------------------------------------------------------------------------
# Emoji 渲染缓存
# ----------------------------------------------------------------------------
_emoji_font = None
_emoji_cache = {}


def emoji_image(ch: str, target_h: int):
    global _emoji_font
    key = (ch, target_h)
    if key in _emoji_cache:
        return _emoji_cache[key]
    try:
        if _emoji_font is None:
            _emoji_font = ImageFont.truetype(F_EMOJI, EMOJI_STRIKE)
        canvas = Image.new("RGBA", (EMOJI_STRIKE + 24, EMOJI_STRIKE + 24), (0, 0, 0, 0))
        d = ImageDraw.Draw(canvas)
        d.text((6, 6), ch, font=_emoji_font, embedded_color=True)
        bbox = canvas.getbbox()
        if bbox is None:
            _emoji_cache[key] = None
            return None
        glyph = canvas.crop(bbox)
        w, h = glyph.size
        nw = max(1, int(w * target_h / h))
        glyph = glyph.resize((nw, target_h), Image.LANCZOS)
        _emoji_cache[key] = glyph
        return glyph
    except Exception:
        _emoji_cache[key] = None
        return None


# ----------------------------------------------------------------------------
# Markdown -> blocks
# ----------------------------------------------------------------------------
def parse_markdown(text: str):
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocks = []
    i = 0
    n = len(lines)

    def is_table_row(s):
        return s.strip().startswith("|") and s.strip().endswith("|") and "|" in s.strip()[1:-1] + "|"

    while i < n:
        line = lines[i]
        s = line.strip()

        if s == "":
            i += 1
            continue

        # 水平线 / 分页提示
        if re.fullmatch(r"(-{3,}|\*{3,}|_{3,})", s) or s.lower() in ("<!--more-->", "<!-- more -->"):
            blocks.append({"type": "pagebreak"})
            i += 1
            continue

        # 标题
        m = re.match(r"^(#{1,6})\s+(.*)$", s)
        if m:
            level = len(m.group(1))
            blocks.append({"type": "heading", "level": level, "text": m.group(2).strip()})
            i += 1
            continue

        # 图片（独占一行）
        m = re.match(r"^!\[[^\]]*\]\(([^)\s]+)(?:\s+[^)]*)?\)\s*$", s)
        if m:
            blocks.append({"type": "image", "url": m.group(1)})
            i += 1
            continue

        # 引用块
        if s.startswith(">"):
            buf = []
            while i < n and lines[i].strip().startswith(">"):
                buf.append(re.sub(r"^\s*>\s?", "", lines[i]))
                i += 1
            # 去掉空行分段
            paras = []
            cur = []
            for b in buf:
                if b.strip() == "":
                    if cur:
                        paras.append(" ".join(cur).strip())
                        cur = []
                else:
                    cur.append(b.strip())
            if cur:
                paras.append(" ".join(cur).strip())
            blocks.append({"type": "quote", "paras": [p for p in paras if p]})
            continue

        # 表格
        if is_table_row(line) and i + 1 < n and re.search(r"^\s*\|?[\s:\-\|]+\|?\s*$", lines[i + 1]):
            header = [c.strip() for c in line.strip().strip("|").split("|")]
            i += 2  # 跳过表头分隔行
            rows = []
            while i < n and is_table_row(lines[i]):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                rows.append(cells)
                i += 1
            blocks.append({"type": "table", "header": header, "rows": rows})
            continue

        # 列表
        if re.match(r"^[-*+]\s+", s) or re.match(r"^\d+[.)]\s+", s):
            items = []
            ordered = bool(re.match(r"^\d+[.)]\s+", s))
            while i < n:
                t = lines[i].strip()
                mm = re.match(r"^[-*+]\s+(.*)$", t) or re.match(r"^\d+[.)]\s+(.*)$", t)
                if not mm:
                    if t == "":
                        # 允许列表项后空行结束
                        nxt = lines[i + 1].strip() if i + 1 < n else ""
                        if re.match(r"^[-*+]\s+", nxt) or re.match(r"^\d+[.)]\s+", nxt):
                            i += 1
                            continue
                    break
                items.append(mm.group(1).strip())
                i += 1
            blocks.append({"type": "list", "ordered": ordered, "items": items})
            continue

        # 段落（连续非空、非块起始行）
        buf = []
        while i < n:
            t = lines[i]
            ts = t.strip()
            if ts == "":
                break
            if (re.match(r"^#{1,6}\s+", ts) or ts.startswith(">") or
                    re.match(r"^[-*+]\s+", ts) or re.match(r"^\d+[.)]\s+", ts) or
                    is_table_row(t) or re.match(r"^!\[[^\]]*\]\([^)]+\)\s*$", ts) or
                    re.fullmatch(r"(-{3,}|\*{3,}|_{3,})", ts)):
                break
            buf.append(ts)
            i += 1
        blocks.append({"type": "para", "text": " ".join(buf).strip()})

    return blocks


# ----------------------------------------------------------------------------
# 行内解析: text -> runs [(text, style)]  style in normal/bold/code
# ----------------------------------------------------------------------------
def parse_inline(text: str):
    # 先去掉链接，保留文字
    def strip_links(s):
        return re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", s)

    runs = []
    parts = re.split(r"(`[^`]+`)", text)
    for part in parts:
        if not part:
            continue
        if part.startswith("`") and part.endswith("`") and len(part) >= 2:
            runs.append((part[1:-1], "code"))
            continue
        part = strip_links(part)
        segs = re.split(r"(\*\*[^*]+\*\*|__[^_]+__)", part)
        for seg in segs:
            if not seg:
                continue
            if (seg.startswith("**") and seg.endswith("**") and len(seg) >= 4) or \
               (seg.startswith("__") and seg.endswith("__") and len(seg) >= 4):
                runs.append((seg[2:-2], "bold"))
            else:
                # 去掉残留的单星号 *italic* -> 文本
                seg = re.sub(r"\*([^*]+)\*", r"\1", seg)
                runs.append((seg, "normal"))
    return runs


def split_tag_tail(runs):
    """若段落末尾是一串行内代码标签（如 `六级` `托福` `GRE`），把它们整体拆出来单独成行。
    返回 (head_runs, tag_runs)；若不适用则 (runs, None)。
    """
    has_code = False
    start = len(runs)
    idx = len(runs) - 1
    while idx >= 0:
        t, s = runs[idx]
        if s == "code" or (s == "normal" and t.strip() == ""):
            if s == "code":
                has_code = True
            start = idx
            idx -= 1
        else:
            break
    # 需要存在标签、且前面还有其它内容（音标/释义），才拆行
    if has_code and start > 0:
        head = runs[:start]
        # head 去掉尾部纯空白 run
        while head and head[-1][1] == "normal" and head[-1][0].strip() == "":
            head = head[:-1]
        return head, runs[start:]
    return runs, None


# ----------------------------------------------------------------------------
# 原子化 + 换行
# ----------------------------------------------------------------------------
class Atom:
    __slots__ = ("text", "kind", "style", "font", "width", "img")

    def __init__(self, text, kind, style, font, width, img=None):
        self.text = text
        self.kind = kind          # cjk / word / space / emoji
        self.style = style        # normal / bold / code
        self.font = font
        self.width = width
        self.img = img


_glyph_cache = {}
_notdef_cache = {}
_MISSING_REF = "\U000FFFFD"  # 私用区码点，几乎所有字体都没有 → 用作 .notdef 参考形状


def font_has_glyph(font, ch):
    """检测 font 是否真的有 ch 的字形（通过与 .notdef 形状比对）。无 fontTools 依赖。"""
    key = (id(font), ch)
    cached = _glyph_cache.get(key)
    if cached is not None:
        return cached
    res = True
    try:
        m = font.getmask(ch)
        if m.size[0] == 0 or m.size[1] == 0:
            res = True  # 零宽（组合符等）当作有
        else:
            ref = _notdef_cache.get(id(font))
            if ref is None:
                ref = font.getmask(_MISSING_REF)
                _notdef_cache[id(font)] = bytes(ref) if ref.size[0] else b""
                ref = _notdef_cache[id(font)]
            if m.size == font.getmask(_MISSING_REF).size and bytes(m) == ref:
                res = False
    except Exception:
        res = True
    _glyph_cache[key] = res
    return res


def pick_font(fb: FontBook, style: str, ch: str, size: int, serif: bool):
    # 选主字体
    if style == "code":
        base = "cjk" if is_cjk(ch) else "mono"
    elif serif:
        base = "serif_bold" if style == "bold" else "serif"
    else:
        base = "cjk_bold" if style == "bold" else "cjk"
    f = fb.get(base, size)
    # 中文/空格主字体必有，直接返回
    if is_cjk(ch) or ch == " ":
        return f
    # 其它字符（拉丁/IPA/符号）：主字体缺字形则回退到 Arial Unicode
    if font_has_glyph(f, ch):
        return f
    alt = fb.get("ipa", size)
    if font_has_glyph(alt, ch):
        return alt
    return f


def atomize(runs, fb, size, serif=False):
    atoms = []
    for text, style in runs:
        j = 0
        L = len(text)
        while j < L:
            ch = text[j]
            if ch == "\n":
                j += 1
                continue
            if is_emoji(ch):
                # 处理可能跟随的变体选择符
                em = ch
                k = j + 1
                while k < L and 0xFE00 <= ord(text[k]) <= 0xFE0F:
                    em += text[k]
                    k += 1
                img = emoji_image(em, size)
                w = img.width if img else size
                atoms.append(Atom(em, "emoji", style, None, w, img))
                j = k
                continue
            if ch == " ":
                f = pick_font(fb, style, "a", size, serif)
                atoms.append(Atom(" ", "space", style, f, f.getlength(" ")))
                j += 1
                continue
            if is_cjk(ch):
                f = pick_font(fb, style, ch, size, serif)
                atoms.append(Atom(ch, "cjk", style, f, f.getlength(ch)))
                j += 1
                continue
            # 普通词：按“所选字体相同”累积，遇字体变化即切分（处理缺字回退，如 æ/ŋ/ð 等）
            f0 = pick_font(fb, style, ch, size, serif)
            buf = ch
            k = j + 1
            while k < L:
                c2 = text[k]
                if c2 == " " or is_cjk(c2) or is_emoji(c2) or c2 == "\n":
                    break
                if pick_font(fb, style, c2, size, serif) is not f0:
                    break
                buf += c2
                k += 1
            atoms.append(Atom(buf, "word", style, f0, f0.getlength(buf)))
            j = k
    return atoms


def wrap_atoms(atoms, max_w):
    """按“分组”换行：分组之间才允许断行。
    - 空格 = 分隔
    - 每个中文字 = 一组（可在字间断行）
    - emoji = 一组
    - 连续 code 原子（如 `六级` 标签）= 一组（整体不断行）
    - 连续普通词/IPA 原子（如音标 /dɪˌlɪbəˈreɪʃ(ə)n/）= 一组（整体不断行）
    返回 lines, 每个 line 是 atom 列表（已去除行尾空格）。
    """
    groups = []
    cur_kind = None

    def new_group(space=False):
        groups.append({"atoms": [], "space": space, "w": 0.0})

    for a in atoms:
        if a.kind == "space":
            new_group(space=True)
            groups[-1]["atoms"].append(a)
            groups[-1]["w"] = a.width
            cur_kind = "space"
        elif a.style == "code":
            if cur_kind == "code":
                groups[-1]["atoms"].append(a)
                groups[-1]["w"] += a.width
            else:
                new_group()
                groups[-1]["atoms"].append(a)
                groups[-1]["w"] = a.width
                cur_kind = "code"
        elif a.kind == "emoji":
            new_group()
            groups[-1]["atoms"].append(a)
            groups[-1]["w"] = a.width
            cur_kind = "emoji"
        elif a.kind == "cjk":
            new_group()
            groups[-1]["atoms"].append(a)
            groups[-1]["w"] = a.width
            cur_kind = "cjk"
        else:  # word / ipa（普通样式）
            if cur_kind == "word":
                groups[-1]["atoms"].append(a)
                groups[-1]["w"] += a.width
            else:
                new_group()
                groups[-1]["atoms"].append(a)
                groups[-1]["w"] = a.width
                cur_kind = "word"

    lines = []
    cur = []
    curw = 0.0
    for g in groups:
        if g["space"]:
            if not cur:
                continue
            if curw + g["w"] > max_w:
                lines.append(cur)
                cur = []
                curw = 0
                continue
            cur.extend(g["atoms"])
            curw += g["w"]
            continue
        # 单组超宽 -> 逐原子/逐字符硬断（极少见）
        if g["w"] > max_w:
            if cur:
                lines.append(cur)
                cur = []
                curw = 0
            for a in g["atoms"]:
                if cur and curw + a.width > max_w:
                    lines.append(cur)
                    cur = []
                    curw = 0
                if a.width > max_w and a.kind == "word":
                    piece = ""
                    f = a.font
                    for ch in a.text:
                        w = f.getlength(piece + ch)
                        if w > max_w and piece:
                            if cur:
                                lines.append(cur)
                                cur = []
                                curw = 0
                            lines.append([Atom(piece, "word", a.style, f, f.getlength(piece))])
                            piece = ch
                        else:
                            piece += ch
                    if piece:
                        cur.append(Atom(piece, "word", a.style, f, f.getlength(piece)))
                        curw += f.getlength(piece)
                else:
                    cur.append(a)
                    curw += a.width
            continue
        if cur and curw + g["w"] > max_w:
            lines.append(cur)
            cur = []
            curw = 0
        cur.extend(g["atoms"])
        curw += g["w"]

    if cur:
        lines.append(cur)
    for ln in lines:
        while ln and ln[-1].kind == "space":
            ln.pop()
    return lines


# ----------------------------------------------------------------------------
# 绘制一行（混合字体，基线对齐，代码 chip）
# ----------------------------------------------------------------------------
_RENDER = {"code_border": None, "ss": 2}   # 渲染期开关（chip 边框等），main 中按 cfg 设置


def draw_line(img, draw, atoms, x, y, primary_font, color, accent, code_bg, code_fg):
    asc, desc = primary_font.getmetrics()
    baseline = y + asc
    # 先画代码 chip 背景（连续 code 原子）
    cx = x
    seg_start = None
    seg_x0 = 0
    positions = []
    for a in atoms:
        positions.append((a, cx))
        cx += a.width
    # chips
    k = 0
    while k < len(positions):
        a, ax = positions[k]
        if a.style == "code" and a.kind != "space":
            x0 = ax
            j = k
            while j < len(positions) and positions[j][0].style == "code":
                j += 1
            last_a, last_x = positions[j - 1]
            x1 = last_x + last_a.width
            pad = max(4, asc // 6)
            rect = [x0 - pad, baseline - asc + pad // 2, x1 + pad, baseline + desc - pad // 2]
            rad = max(6, asc // 4)
            draw.rounded_rectangle(rect, radius=rad, fill=code_bg)
            cb = _RENDER.get("code_border")
            if cb:
                draw.rounded_rectangle(rect, radius=rad, outline=cb, width=max(1, _RENDER.get("ss", 2)))
            k = j
        else:
            k += 1
    # 再画文字 / emoji
    for a, ax in positions:
        if a.kind == "space":
            continue
        if a.kind == "emoji":
            if a.img is not None:
                img.paste(a.img, (int(ax), int(baseline - int(a.img.height * 0.82))), a.img)
            continue
        fill = code_fg if a.style == "code" else color
        draw.text((ax, baseline), a.text, font=a.font, fill=fill, anchor="ls")


# ----------------------------------------------------------------------------
# 布局：blocks -> rows（可分页的流式行）
# ----------------------------------------------------------------------------
class Ctx:
    pass


def build_tag_pills(tags, cfg, fb):
    """把标签渲染成 Dribbble 风格的彩色胶囊（pill），自动折行。返回 rows。"""
    cw = cfg["content_w"]
    tsize = int(cfg["body"] * 0.80)
    f = fb.get("cjk", tsize)
    asc, desc = f.getmetrics()
    txt_h = asc + desc
    pad_x = int(tsize * 0.70)
    pad_y = int(tsize * 0.42)
    pill_h = txt_h + pad_y * 2
    gap = int(tsize * 0.5)
    line_gap = int(tsize * 0.5)

    pills = [(t, int(f.getlength(t)) + pad_x * 2) for t in tags]
    lines = []
    cur = []
    curw = 0
    for t, w in pills:
        add = w + (gap if cur else 0)
        if cur and curw + add > cw:
            lines.append(cur)
            cur = []
            curw = 0
            add = w
        cur.append((t, w))
        curw += add
    if cur:
        lines.append(cur)

    rows = []
    n = len(lines)
    for li, ln in enumerate(lines):
        def make(ln):
            def _d(img, draw, x, y):
                cx = x
                for t, w in ln:
                    draw.rounded_rectangle([cx, y, cx + w, y + pill_h],
                                           radius=pill_h // 2, fill=cfg["tag_bg"])
                    draw.text((cx + w / 2, y + pill_h / 2), t, font=f,
                              fill=cfg["tag_fg"], anchor="mm")
                    cx += w + gap
            return _d
        rows.append({"h": pill_h + (line_gap if li < n - 1 else 0), "draw": make(ln)})
    return rows


def build_rows(blocks, cfg, fb):
    rows = []
    cw = cfg["content_w"]

    def add_spacer(h):
        rows.append({"h": h, "draw": None, "spacer": True})

    BODY = cfg["body"]
    body_lh = cfg["body_lh"]
    text_color = cfg["text_color"]
    sub_color = cfg["sub_color"]
    accent = cfg["accent"]
    code_bg = cfg["code_bg"]
    code_fg = cfg["code_fg"]

    first_heading_seen = [False]

    for bi, blk in enumerate(blocks):
        bt = blk["type"]

        if bt == "pagebreak":
            rows.append({"h": 0, "draw": None, "spacer": True, "breakbefore": True})
            continue

        if bt == "heading":
            level = blk["level"]
            size = {1: cfg["h1"], 2: cfg["h2"], 3: cfg["h3"]}.get(level, cfg["h3"])
            lh = int(size * 1.35)
            runs = parse_inline(blk["text"])
            atoms = atomize(runs, fb, size, serif=False)
            # 强制加粗
            for a in atoms:
                if a.kind in ("cjk", "word") and a.style != "code":
                    a.style = "bold"
                    a.font = pick_font(fb, "bold", a.text[0], size, False)
                    a.width = a.font.getlength(a.text)
            lines = wrap_atoms(atoms, cw - (cfg["bar_w"] + cfg["bar_gap"] if level == 2 else 0))
            pf = fb.get("cjk_bold", size)
            total_h = lh * len(lines)
            top_pad = cfg["block_gap"] if first_heading_seen[0] else 0
            # 流式排版：不在 ## 处强制翻页，让词汇卡片与今日例句等自然连排，仅用 --- 强制分页
            breakbefore = False
            first_heading_seen[0] = True

            indent = (cfg["bar_w"] + cfg["bar_gap"]) if (cfg.get("heading_bar", True) and level == 2) else 0
            hcolor = cfg.get("heading_color", accent) if level == 2 else text_color

            def make_heading_draw(lines, pf, lh, level, indent, hcolor, size, total_h):
                def _d(img, draw, x, y):
                    if cfg.get("heading_bar", True) and level == 2:
                        bar_h = total_h
                        draw.rounded_rectangle(
                            [x, y + int(size * 0.08), x + cfg["bar_w"], y + bar_h - int(size * 0.12)],
                            radius=cfg["bar_w"] // 2, fill=accent)
                    yy = y
                    for ln in lines:
                        draw_line(img, draw, ln, x + indent, yy, pf, hcolor, accent, code_bg, code_fg)
                        yy += lh
                return _d

            if top_pad:
                add_spacer(top_pad)
            rows.append({
                "h": total_h,
                "draw": make_heading_draw(lines, pf, lh, level, indent, hcolor, size, total_h),
                "breakbefore": breakbefore,
                "keepnext": True,
            })
            add_spacer(int(cfg["block_gap"] * 0.55))
            continue

        if bt == "para":
            runs = parse_inline(blk["text"])
            head_runs, tag_runs = split_tag_tail(runs)
            pf = fb.get("cjk", BODY)
            if bi > 0:
                add_spacer(cfg["block_gap"])

            def emit_lines(rns, size, font, color, serif=False, center=False):
                atoms = atomize(rns, fb, size, serif=serif)
                lh = int(size * (1.8 if cfg.get("wechat") else 1.6))
                for ln in wrap_atoms(atoms, cw):
                    lw = sum(a.width for a in ln)
                    off = int((cw - lw) / 2) if center else 0
                    rows.append({
                        "h": lh,
                        "draw": (lambda ln, font, color, off: (lambda img, draw, x, y: draw_line(
                            img, draw, ln, x + off, y, font, color, accent, code_bg, code_fg)))(ln, font, color, off),
                    })

            is_vocab = bool(head_runs) and head_runs[0][1] == "bold" and any("/" in t for t, s in head_runs)
            if cfg.get("hero_enable", True) and is_vocab:
                # Dribbble Hero 词卡：词放大成 hero，音标行浅灰
                headword = head_runs[0][0].strip()
                rest = head_runs[1:]
                while rest and rest[0][1] == "normal" and rest[0][0].strip() == "":
                    rest = rest[1:]
                hsize = cfg["hero"]
                emit_lines([(headword, "bold")], hsize, fb.get("cjk_bold", hsize), text_color)
                if rest:
                    add_spacer(int(body_lh * 0.30))
                    emit_lines(rest, BODY, pf, cfg["sub_color"])
            elif cfg.get("wechat") and is_vocab:
                # 公众号：词 + 音标 居中一行
                emit_lines(head_runs, BODY, pf, text_color, center=True)
            else:
                emit_lines(head_runs, BODY, pf, text_color)

            if tag_runs:
                tags = [t for t, s in tag_runs if s == "code"]
                if tags:
                    add_spacer(int(body_lh * 0.30))
                    if cfg.get("tag_style") == "pill":
                        for r in build_tag_pills(tags, cfg, fb):
                            rows.append(r)
                    else:
                        emit_lines(tag_runs, BODY, pf, text_color, center=True)
            continue

        if bt == "list":
            pf = fb.get("cjk", BODY)
            if bi > 0:
                add_spacer(cfg["block_gap"])
            bullet_indent = int(BODY * 1.3)
            for idx, item in enumerate(blk["items"]):
                runs = parse_inline(item)
                atoms = atomize(runs, fb, BODY, serif=False)
                lines = wrap_atoms(atoms, cw - bullet_indent)
                marker = f"{idx + 1}." if blk["ordered"] else "•"
                for li, ln in enumerate(lines):
                    def make_list_draw(ln, li, marker, pf):
                        def _d(img, draw, x, y):
                            if li == 0:
                                draw.text((x + 2, y + pf.getmetrics()[0]), marker,
                                          font=fb.get("cjk_bold", BODY), fill=accent, anchor="ls")
                            draw_line(img, draw, ln, x + bullet_indent, y, pf,
                                      text_color, accent, code_bg, code_fg)
                        return _d
                    rows.append({"h": body_lh, "draw": make_list_draw(ln, li, marker, pf)})
                if idx < len(blk["items"]) - 1:
                    add_spacer(int(body_lh * 0.18))
            continue

        if bt == "quote":
            qsize = cfg["quote"]
            serif = cfg.get("quote_serif", True)
            qlh = int(qsize * (1.8 if cfg.get("wechat") else 1.62))
            pf = fb.get("serif" if serif else "cjk", qsize)
            if bi > 0:
                add_spacer(cfg["block_gap"])
            qpad = cfg["quote_pad"]
            bar = cfg["bar_w"]
            text_x = bar + cfg["bar_gap"] + qpad
            inner_w = cw - text_x - qpad
            all_lines = []
            for pidx, p in enumerate(blk["paras"]):
                runs = parse_inline(p)
                atoms = atomize(runs, fb, qsize, serif=serif)
                plines = wrap_atoms(atoms, inner_w)
                all_lines.extend(plines)
                if pidx < len(blk["paras"]) - 1:
                    all_lines.append(None)  # 段间空行
            total_h = qpad * 2 + qlh * len(all_lines)
            qbg = cfg["quote_bg"]

            def make_quote_draw(all_lines, total_h, qlh, pf, qpad, bar, text_x, qbg):
                def _d(img, draw, x, y):
                    if qbg is not None:
                        draw.rounded_rectangle([x, y, x + cw, y + total_h],
                                               radius=cfg["quote_radius"], fill=qbg)
                        draw.rounded_rectangle([x, y + bar, x + bar * 2, y + total_h - bar],
                                               radius=bar, fill=cfg["quote_bar"])
                    else:
                        # 公众号风：仅左侧灰色竖线，无底色
                        draw.rectangle([x, y, x + bar, y + total_h], fill=cfg["quote_bar"])
                    ty = y + qpad
                    for ln in all_lines:
                        if ln is not None:
                            draw_line(img, draw, ln, x + text_x, ty, pf,
                                      cfg["quote_text"], cfg["accent"], cfg["code_bg"], cfg["code_fg"])
                        ty += qlh
                return _d

            # 整块不可分页，保证引用不被拆到两页
            rows.append({
                "h": total_h,
                "draw": make_quote_draw(all_lines, total_h, qlh, pf, qpad, bar, text_x, qbg),
                "keepnext": False,
            })
            continue

        if bt == "table":
            if bi > 0:
                add_spacer(cfg["block_gap"])
            tsize = cfg["table"]
            tlh = int(tsize * 1.5)
            cellpad = cfg["cell_pad"]
            ncol = len(blk["header"])
            ff = fb.get("cjk", tsize)

            # 每列：min_w = 最长“不可断词”宽度（英文单词不断行）；pref_w = 整段内容宽度
            min_w = []
            pref_w = []
            for c in range(ncol):
                texts = [blk["header"][c]] + [r[c] if c < len(r) else "" for r in blk["rows"]]
                longest_word = tsize * 1.0
                total_pref = tsize * 2.0
                for t in texts:
                    runs = parse_inline(t)
                    atoms = atomize(runs, fb, tsize, serif=False)
                    line_w = sum(a.width for a in atoms)
                    total_pref = max(total_pref, line_w)
                    for a in atoms:
                        if a.kind == "word":
                            longest_word = max(longest_word, a.width)
                min_w.append(longest_word + cellpad * 2)
                pref_w.append(min(total_pref + cellpad * 2, cw * 0.62))

            base = sum(min_w)
            if base <= cw:
                extra = cw - base
                pref_extra = [max(0, pref_w[c] - min_w[c]) for c in range(ncol)]
                tot_pe = sum(pref_extra) or 1
                colw = [int(min_w[c] + extra * pref_extra[c] / tot_pe) for c in range(ncol)]
            else:
                colw = [int(w / base * cw) for w in min_w]
            colw[-1] = cw - sum(colw[:-1])

            def cell_lines(text, w, bold=False):
                runs = parse_inline(text)
                if bold:
                    runs = [(t, "bold" if s == "normal" else s) for t, s in runs]
                atoms = atomize(runs, fb, tsize, serif=False)
                return wrap_atoms(atoms, w - cellpad * 2)

            # 预排每一行（整张表作为不可分页的整块）
            tround = cfg.get("table_round", True)
            R = cfg["quote_radius"] if tround else 0
            hfill = cfg.get("table_header_fill", cfg["accent"])
            hfg = cfg.get("table_header_fg", (255, 255, 255))

            def measure(cells, bold):
                per_cell = []
                maxlines = 1
                for c in range(ncol):
                    txt = cells[c] if c < len(cells) else ""
                    ls = cell_lines(txt, colw[c], bold=bold)
                    per_cell.append(ls)
                    maxlines = max(maxlines, len(ls))
                return maxlines * tlh + cellpad * 2, per_cell

            rowinfos = []
            hh, hc = measure(blk["header"], True)
            rowinfos.append((hh, hc, True))
            for r in blk["rows"]:
                rh, pc = measure(r, False)
                rowinfos.append((rh, pc, False))
            total_h = sum(ri[0] for ri in rowinfos)

            def make_table_draw(rowinfos, total_h, colw, R):
                def _d(img, draw, x, y):
                    pf = fb.get("cjk", tsize)
                    header_h = rowinfos[0][0]
                    draw.rounded_rectangle([x, y, x + cw, y + total_h], radius=R, fill=(255, 255, 255))
                    draw.rounded_rectangle([x, y, x + cw, y + header_h], radius=R,
                                           corners=(True, True, False, False), fill=hfill)
                    # 行分隔线（表头之后）
                    yy = 0
                    for i, (rh, pc, is_h) in enumerate(rowinfos):
                        if i > 0:
                            draw.line([x, y + yy, x + cw, y + yy], fill=cfg["table_border"], width=2)
                        yy += rh
                    # 列分隔线
                    v_top = y if not tround else y + header_h
                    v_bot = y + total_h if not tround else y + total_h - R // 2
                    cx = x
                    for c in range(ncol - 1):
                        cx += colw[c]
                        draw.line([cx, v_top, cx, v_bot], fill=cfg["table_border"], width=2)
                    # 文本
                    yy = y
                    for (rh, pc, is_h) in rowinfos:
                        cx = x
                        for c in range(ncol):
                            col = hfg if is_h else cfg["text_color"]
                            ty = yy + cellpad
                            for ln in pc[c]:
                                draw_line(img, draw, ln, cx + cellpad, ty, pf, col,
                                          cfg["accent"], cfg["code_bg"], cfg["code_fg"])
                                ty += tlh
                            cx += colw[c]
                        yy += rh
                    draw.rounded_rectangle([x, y, x + cw, y + total_h], radius=R,
                                           outline=cfg["table_border"], width=2)
                return _d

            rows.append({"h": total_h, "draw": make_table_draw(rowinfos, total_h, colw, R),
                         "keepnext": False})
            continue

        if bt == "image":
            if bi > 0:
                add_spacer(cfg["block_gap"])
            # 图片填满“正文宽度”（与文字左右对齐），圆角 + 边框；高度自由但不超过页面
            pim = load_remote_image(blk["url"], cw, cfg["content_h"], fill=True)
            if pim is None:
                continue

            def make_img_draw(pim):
                def _d(img, draw, x, y):
                    ox = int(x + (cw - pim.width) // 2)   # x 即正文左边缘，与文字对齐
                    rad = cfg["img_radius"]
                    mask = Image.new("L", pim.size, 0)
                    md = ImageDraw.Draw(mask)
                    md.rounded_rectangle([0, 0, pim.width, pim.height], radius=rad, fill=255)
                    img.paste(pim, (ox, int(y)), mask)
                    ib = cfg.get("img_border")
                    if ib:
                        draw.rounded_rectangle([ox, int(y), ox + pim.width - 1, int(y) + pim.height - 1],
                                               radius=rad, outline=ib, width=max(1, cfg["SS"]))
                return _d
            rows.append({"h": pim.height, "draw": make_img_draw(pim), "keepnext": False})
            continue

    return rows


# ----------------------------------------------------------------------------
# 远程/本地图片加载
# ----------------------------------------------------------------------------
def load_remote_image(url, max_w, max_h, fill=False):
    try:
        data = None
        if url.startswith("http://") or url.startswith("https://"):
            import urllib.request
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": ""})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = resp.read()
            from io import BytesIO
            im = Image.open(BytesIO(data)).convert("RGBA")
        else:
            im = Image.open(url).convert("RGBA")
        w, h = im.size
        if fill:
            # 满宽：强制缩放到 max_w（高度自由，仅做一个很宽松的上限保护）
            scale = max_w / w
            if h * scale > max_h:
                scale = max_h / w * (max_w / max_w)  # 不再额外压缩，保持满宽
                scale = max_w / w
        else:
            scale = min(max_w / w, max_h / h)
        nw, nh = max(1, int(w * scale)), max(1, int(h * scale))
        return im.resize((nw, nh), Image.LANCZOS)
    except Exception as e:
        sys.stderr.write(f"[warn] 图片加载失败，已跳过: {url} ({e})\n")
        return None


# ----------------------------------------------------------------------------
# 分页
# ----------------------------------------------------------------------------
def paginate(rows, content_h):
    pages = []
    cur = []
    curh = 0
    i = 0
    N = len(rows)
    while i < N:
        r = rows[i]
        if r.get("spacer") and not cur:
            i += 1
            continue
        if r.get("breakbefore") and cur:
            pages.append(cur)
            cur = []
            curh = 0
            continue
        need = r["h"]
        extra = 0
        if r.get("keepnext"):
            j = i + 1
            acc = 0
            while j < N and rows[j].get("spacer"):
                acc += rows[j]["h"]
                j += 1
            if j < N:
                extra = acc + rows[j]["h"]
        if cur and curh + need + extra > content_h:
            pages.append(cur)
            cur = []
            curh = 0
            continue
        cur.append(r)
        curh += need
        i += 1
    if cur:
        # 去掉尾部 spacer
        while cur and cur[-1].get("spacer"):
            cur.pop()
        pages.append(cur)
    return pages


# ----------------------------------------------------------------------------
# 背景渐变
# ----------------------------------------------------------------------------
def gradient_bg(w, h, c1, c2):
    base = Image.new("RGB", (1, h))
    px = base.load()
    for y in range(h):
        t = y / max(1, h - 1)
        px[0, y] = (
            int(c1[0] + (c2[0] - c1[0]) * t),
            int(c1[1] + (c2[1] - c1[1]) * t),
            int(c1[2] + (c2[2] - c1[2]) * t),
        )
    return base.resize((w, h), Image.BILINEAR)


# ----------------------------------------------------------------------------
# 渲染一张卡片
# ----------------------------------------------------------------------------
def render_card(page_rows, cfg, fb, page_no, total, footer_left):
    W, H = cfg["W"], cfg["H"]
    M = cfg["margin"]
    PAD = cfg["pad"]
    theme = cfg["theme"]

    if M <= 0:
        # 满版：去掉最外面的渐变边框，白底铺满整张图
        img = Image.new("RGBA", (W, H), (255, 255, 255, 255))
        draw = ImageDraw.Draw(img)
    else:
        img = gradient_bg(W, H, theme["from"], theme["to"]).convert("RGBA")
        # 面板阴影
        panel_box = [M, M, W - M, H - M]
        radius = cfg["panel_radius"]
        shadow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        sd = ImageDraw.Draw(shadow)
        sd.rounded_rectangle([M + 6, M + 14, W - M + 6, H - M + 14], radius=radius, fill=(0, 0, 0, 70))
        shadow = shadow.filter(ImageFilter.GaussianBlur(cfg["shadow_blur"]))
        img = Image.alpha_composite(img, shadow)
        draw = ImageDraw.Draw(img)
        draw.rounded_rectangle(panel_box, radius=radius, fill=(255, 255, 255, 255))

    # 内容区起点
    x0 = M + PAD
    y0 = M + PAD
    content_h = cfg["content_h"]
    used = sum(r["h"] for r in page_rows)
    # 短卡片轻度垂直居中（如封面卡），更美观
    y = y0
    if used < content_h * 0.62:
        y = y0 + min((content_h - used) // 2, int(content_h * 0.22))

    for r in page_rows:
        if r.get("draw"):
            r["draw"](img, draw, x0, y)
        y += r["h"]

    # 页脚
    fcolor = (170, 175, 185, 255)
    fsize = cfg["footer"]
    ff = fb.get("cjk", fsize)
    fy = H - M - int(PAD * 0.5)
    if footer_left:
        draw.text((x0, fy), footer_left, font=ff, fill=fcolor, anchor="lm")
    page_txt = f"{page_no} / {total}"
    draw.text((W - M - PAD, fy), page_txt, font=fb.get("cjk_bold", fsize),
              fill=cfg["accent"] + (255,), anchor="rm")

    return img.convert("RGB")


def render_total(rows, cfg, fb, footer_left):
    """把所有内容渲染成一张长图（不分页）。满版白底。"""
    W = cfg["W"]
    M = cfg["margin"]
    PAD = cfg["pad"]

    rr = [r for r in rows]
    while rr and rr[0].get("spacer"):
        rr.pop(0)
    while rr and rr[-1].get("spacer"):
        rr.pop()
    content_h = sum(r["h"] for r in rr)
    footer_band = int(PAD * 1.0) + cfg["footer"]
    H = M * 2 + PAD * 2 + content_h + footer_band

    img = Image.new("RGBA", (W, H), (255, 255, 255, 255))
    draw = ImageDraw.Draw(img)

    x0 = M + PAD
    y = M + PAD
    for r in rr:
        if r.get("draw"):
            r["draw"](img, draw, x0, y)
        y += r["h"]

    fsize = cfg["footer"]
    fy = H - M - int(PAD * 0.5)
    if footer_left:
        draw.text((x0, fy), footer_left, font=fb.get("cjk", fsize),
                  fill=(170, 175, 185, 255), anchor="lm")
    draw.text((W - M - PAD, fy), "全文", font=fb.get("cjk_bold", fsize),
              fill=cfg["accent"] + (255,), anchor="rm")

    return img.convert("RGB"), H


# ----------------------------------------------------------------------------
# 配置
# ----------------------------------------------------------------------------
def build_cfg(args, theme, fscale=1.0):
    SS = args.scale

    def s(v):                       # 固定盒子尺寸（图片/边距/内边距，不随字号缩放）
        return int(round(v * SS))

    def fs(v):                      # 字号/间距，随 fscale 缩放以适配页数上限
        return int(round(v * SS * fscale))

    W = s(args.width)
    H = s(int(args.width * args.ratio_h / args.ratio_w))
    M = s(args.margin)
    PAD = s(64)
    content_w = W - 2 * M - 2 * PAD
    footer_h = s(58)
    content_h = H - 2 * M - 2 * PAD - footer_h

    common = {
        "SS": SS, "W": W, "H": H, "margin": M, "pad": PAD,
        "content_w": content_w, "content_h": content_h,
        "panel_radius": s(40) if M > 0 else 0, "shadow_blur": s(18),
        "theme": theme,
    }

    if args.style == "wechat":
        # 样式来自 md-to-wechat.html（微信公众号文章风格）
        body = fs(29)
        cfg = dict(common)
        cfg.update({
            "style": "wechat", "wechat": True,
            "hero_enable": False, "heading_bar": False,
            "hero": fs(40), "h1": fs(34), "h2": fs(38), "h3": fs(29),
            "body": body, "body_lh": int(body * 1.8),
            "quote": fs(26), "quote_pad": fs(18), "quote_radius": 0,
            "quote_serif": False, "quote_bg": None,
            "quote_bar": (210, 210, 212), "quote_text": (26, 26, 26),
            "table": fs(26), "cell_pad": fs(15),
            "table_border": (221, 221, 221), "table_round": False,
            "table_header_fill": (245, 245, 245), "table_header_fg": (0, 0, 0),
            "footer": fs(24), "block_gap": fs(28),
            "bar_w": fs(6), "bar_gap": fs(16),
            "img_max_h": fs(440), "img_radius": s(8), "img_border": (221, 221, 221),
            "text_color": (26, 26, 26), "sub_color": (115, 115, 115),
            "accent": (26, 26, 26),
            "code_bg": (235, 235, 235), "code_fg": (26, 26, 26), "code_border": (204, 204, 204),
            "tag_style": "chip",
            "heading_color": (0, 0, 0),
        })
        return cfg

    # 默认/dribbble：大字号 hero + 彩色胶囊 + 圆角强调表格
    ax = theme["accent"]
    tag_bg = tuple(int(c + (255 - c) * 0.85) for c in ax)
    tag_fg = tuple(int(c * 0.70) for c in ax)
    body = fs(37)
    cfg = dict(common)
    cfg.update({
        "style": "dribbble", "wechat": False,
        "hero_enable": True, "heading_bar": True,
        "hero": fs(76), "h1": fs(60), "h2": fs(56), "h3": fs(46),
        "body": body, "body_lh": int(body * 1.6),
        "quote": fs(36), "quote_pad": fs(28), "quote_radius": fs(18),
        "quote_serif": True, "quote_bg": (246, 248, 252),
        "quote_bar": theme["accent"], "quote_text": (70, 80, 96),
        "table": fs(33), "cell_pad": fs(18),
        "table_border": (226, 230, 238), "table_round": True,
        "table_header_fill": theme["accent"], "table_header_fg": (255, 255, 255),
        "footer": fs(25), "block_gap": fs(32),
        "bar_w": fs(9), "bar_gap": fs(20),
        "img_max_h": fs(400), "img_radius": fs(20), "img_border": None,
        "text_color": (28, 31, 42), "sub_color": (120, 126, 140),
        "accent": theme["accent"],
        "code_bg": tag_bg, "code_fg": tag_fg, "code_border": None,
        "tag_bg": tag_bg, "tag_fg": tag_fg, "tag_style": "pill",
        "heading_color": theme["accent"],
    })
    return cfg



# ----------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="把 Markdown 渲染成小红书图片（纯命令行，无浏览器）")
    ap.add_argument("md", help="Markdown 文件路径")
    ap.add_argument("-o", "--out", default=os.path.expanduser("~/Downloads/a01_output"),
                    help="输出目录（默认 ~/Downloads/a01_output，在 git 仓库之外）")
    ap.add_argument("--theme", default="mono", choices=list(THEMES.keys()), help="配色主题（默认 mono 黑白）")
    ap.add_argument("--style", default="wechat", choices=["wechat", "dribbble"],
                    help="排版风格：wechat=公众号文章风（默认）；dribbble=大字号卡片风")
    ap.add_argument("--width", type=int, default=1080, help="图片宽度像素（默认 1080）")
    ap.add_argument("--margin", type=int, default=0,
                    help="外边框宽度px（默认 0=满版铺满；>0 时显示渐变边框+圆角白卡）")
    ap.add_argument("--ratio-w", type=float, default=3, help="宽比（默认 3）")
    ap.add_argument("--ratio-h", type=float, default=4, help="高比（默认 4）")
    ap.add_argument("--scale", type=int, default=2, help="超采样倍数，越大越清晰（默认 2）")
    ap.add_argument("--format", default="png", choices=["png", "jpg"], help="输出格式")
    ap.add_argument("--max-pages", type=int, default=4,
                    help="卡片张数上限（默认 4，适配 Twitter 单条 4 图）；超出则自动微缩字号重排。设 0 不限制")
    args = ap.parse_args()

    if not os.path.isfile(args.md):
        sys.stderr.write(f"找不到文件: {args.md}\n")
        sys.exit(1)

    with open(args.md, "r", encoding="utf-8") as f:
        text = f.read()

    theme = THEMES[args.theme]
    fb = FontBook()
    blocks = parse_markdown(text)

    # 自动适配页数上限：超过 max_pages 则按比例微缩字号重排（设下限避免过小）
    fscale = 1.0
    while True:
        cfg = build_cfg(args, theme, fscale)
        rows = build_rows(blocks, cfg, fb)
        pages = paginate(rows, cfg["content_h"])
        if args.max_pages <= 0 or len(pages) <= args.max_pages or fscale <= 0.66:
            break
        fscale -= 0.05

    _RENDER["code_border"] = cfg.get("code_border")
    _RENDER["ss"] = cfg["SS"]

    stem = os.path.splitext(os.path.basename(args.md))[0]
    word = stem.split("：")[-1].split(":")[-1].strip()
    word = re.sub(r"[\\/:*?\"<>|\s]", "_", word) or "word"
    footer_left = word[:24]

    # 子文件夹：日期_单词；同一文件夹内每次成功生成 version 自增
    date = datetime.date.today().strftime("%Y%m%d")
    out_dir = os.path.join(args.out, f"{date}_{word}")
    os.makedirs(out_dir, exist_ok=True)

    ext = "jpg" if args.format == "jpg" else "png"
    ver_re = re.compile(rf"^{re.escape(word)}_v(\d+)_\d+\.{ext}$")
    prev = [int(m.group(1)) for f in os.listdir(out_dir)
            for m in [ver_re.match(f)] if m]
    version = (max(prev) + 1) if prev else 1

    total = len(pages)
    out_paths = []
    for idx, pg in enumerate(pages, 1):
        card = render_card(pg, cfg, fb, idx, total, footer_left)
        target = (args.width, int(args.width * args.ratio_h / args.ratio_w))
        if cfg["SS"] != 1:
            card = card.resize(target, Image.LANCZOS)
        name = f"{word}_v{version}_{idx:02d}.{ext}"
        path = os.path.abspath(os.path.join(out_dir, name))
        if ext == "jpg":
            card.save(path, "JPEG", quality=92)
        else:
            card.save(path, "PNG")
        out_paths.append(path)

    # total 长图（所有内容拼成一张）
    total_img, th = render_total(rows, cfg, fb, footer_left)
    if cfg["SS"] != 1:
        total_img = total_img.resize((args.width, int(round(th / cfg["SS"]))), Image.LANCZOS)
    total_name = f"{word}_v{version}_total.{ext}"
    total_path = os.path.abspath(os.path.join(out_dir, total_name))
    if ext == "jpg":
        total_img.save(total_path, "JPEG", quality=92)
    else:
        total_img.save(total_path, "PNG")

    print(f"✅ 生成 {total} 张图片 ({target[0]}x{target[1]}, 主题 {args.theme}, 版本 v{version}):")
    print(f"  目录: {os.path.abspath(out_dir)}")
    for p in out_paths:
        print("  " + p)
    print(f"  [total] {total_path}")


if __name__ == "__main__":
    main()
