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


def pick_font(fb: FontBook, style: str, ch: str, size: int, serif: bool):
    if is_ipa(ch):
        return fb.get("ipa", size)
    if style == "code":
        # 代码字体不含中文，遇中文用 cjk
        if is_cjk(ch):
            return fb.get("cjk", size)
        return fb.get("mono", size)
    if serif:
        return fb.get("serif_bold" if style == "bold" else "serif", size)
    return fb.get("cjk_bold" if style == "bold" else "cjk", size)


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
            # 普通词：累积到下一个空格/中文/emoji；并在 IPA 边界切分（保证音标走 Arial Unicode 回退）
            buf = ch
            ipa0 = is_ipa(ch)
            k = j + 1
            while k < L and not (text[k] == " " or is_cjk(text[k]) or is_emoji(text[k]) or text[k] == "\n") \
                    and is_ipa(text[k]) == ipa0:
                buf += text[k]
                k += 1
            f = pick_font(fb, style, ch, size, serif)
            atoms.append(Atom(buf, "word", style, f, f.getlength(buf)))
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
            draw.rounded_rectangle(
                [x0 - pad, baseline - asc + pad // 2, x1 + pad, baseline + desc - pad // 2],
                radius=max(6, asc // 4), fill=code_bg)
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

            indent = (cfg["bar_w"] + cfg["bar_gap"]) if level == 2 else 0
            hcolor = accent if level == 2 else text_color

            def make_heading_draw(lines, pf, lh, level, indent, hcolor, size, total_h):
                def _d(img, draw, x, y):
                    if level == 2:
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

            def emit_para_lines(rns):
                atoms = atomize(rns, fb, BODY, serif=False)
                for ln in wrap_atoms(atoms, cw):
                    rows.append({
                        "h": body_lh,
                        "draw": (lambda ln: (lambda img, draw, x, y: draw_line(
                            img, draw, ln, x, y, pf, text_color, accent, code_bg, code_fg)))(ln),
                    })

            emit_para_lines(head_runs)
            if tag_runs:
                add_spacer(int(body_lh * 0.22))  # 音标与标签之间留白
                emit_para_lines(tag_runs)
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
            qlh = int(qsize * 1.62)
            pf = fb.get("serif", qsize)
            if bi > 0:
                add_spacer(cfg["block_gap"])
            qpad = cfg["quote_pad"]
            bar = cfg["bar_w"]
            text_x = bar + cfg["bar_gap"] + qpad
            inner_w = cw - text_x - qpad
            all_lines = []
            for pidx, p in enumerate(blk["paras"]):
                runs = parse_inline(p)
                atoms = atomize(runs, fb, qsize, serif=True)
                plines = wrap_atoms(atoms, inner_w)
                all_lines.extend(plines)
                if pidx < len(blk["paras"]) - 1:
                    all_lines.append(None)  # 段间空行
            total_h = qpad * 2 + qlh * len(all_lines)
            qbg = cfg["quote_bg"]

            def make_quote_draw(all_lines, total_h, qlh, pf, qpad, bar, text_x, qbg):
                def _d(img, draw, x, y):
                    draw.rounded_rectangle([x, y, x + cw, y + total_h],
                                           radius=cfg["quote_radius"], fill=qbg)
                    draw.rounded_rectangle([x, y + bar, x + bar * 2, y + total_h - bar],
                                           radius=bar, fill=cfg["quote_bar"])
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

            # 预排每一行高度
            def row_render(cells, bold, header):
                per_cell = []
                maxlines = 1
                for c in range(ncol):
                    txt = cells[c] if c < len(cells) else ""
                    ls = cell_lines(txt, colw[c], bold=bold)
                    per_cell.append(ls)
                    maxlines = max(maxlines, len(ls))
                h = maxlines * tlh + cellpad * 2

                def _d(img, draw, x, y, _per=per_cell, _h=h, _header=header):
                    cx = x
                    if _header:
                        draw.rectangle([x, y, x + cw, y + _h], fill=cfg["accent"])
                    for c in range(ncol):
                        # 边框
                        draw.rectangle([cx, y, cx + colw[c], y + _h], outline=cfg["table_border"], width=1)
                        pf = fb.get("cjk", tsize)
                        ty = y + cellpad
                        for ln in _per[c]:
                            col = (255, 255, 255) if _header else cfg["text_color"]
                            draw_line(img, draw, ln, cx + cellpad, ty, pf, col,
                                      cfg["accent"], cfg["code_bg"], cfg["code_fg"])
                            ty += tlh
                        cx += colw[c]
                return h, _d

            h, d = row_render(blk["header"], True, True)
            rows.append({"h": h, "draw": d, "keepnext": True})
            for r in blk["rows"]:
                h, d = row_render(r, False, False)
                rows.append({"h": h, "draw": d})
            continue

        if bt == "image":
            if bi > 0:
                add_spacer(cfg["block_gap"])
            pim = load_remote_image(blk["url"], cw, cfg["img_max_h"])
            if pim is None:
                continue

            def make_img_draw(pim):
                def _d(img, draw, x, y):
                    ox = x + (cw - pim.width) // 2
                    # 圆角
                    rad = cfg["img_radius"]
                    mask = Image.new("L", pim.size, 0)
                    md = ImageDraw.Draw(mask)
                    md.rounded_rectangle([0, 0, pim.width, pim.height], radius=rad, fill=255)
                    img.paste(pim, (int(ox), int(y)), mask)
                return _d
            rows.append({"h": pim.height, "draw": make_img_draw(pim), "keepnext": False})
            continue

    return rows


# ----------------------------------------------------------------------------
# 远程/本地图片加载
# ----------------------------------------------------------------------------
def load_remote_image(url, max_w, max_h):
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
        scale = min(max_w / w, max_h / h, 1.0) if (w > max_w or h > max_h) else min(max_w / w, 1.0)
        # 宽度优先填满到 max_w，但限制高度
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


# ----------------------------------------------------------------------------
# 配置
# ----------------------------------------------------------------------------
def build_cfg(args, theme):
    SS = args.scale
    def s(v):
        return int(round(v * SS))

    W = s(args.width)
    H = s(int(args.width * args.ratio_h / args.ratio_w))
    M = s(args.margin)
    PAD = s(60)
    content_w = W - 2 * M - 2 * PAD
    footer_h = s(56)
    content_h = H - 2 * M - 2 * PAD - footer_h

    body = s(33)
    cfg = {
        "SS": SS, "W": W, "H": H, "margin": M, "pad": PAD,
        "content_w": content_w, "content_h": content_h,
        "panel_radius": s(40) if M > 0 else 0, "shadow_blur": s(18),
        "h1": s(56), "h2": s(48), "h3": s(40),
        "body": body, "body_lh": int(body * 1.68),
        "quote": s(32), "quote_pad": s(24), "quote_radius": s(16),
        "table": s(29), "cell_pad": s(16),
        "footer": s(24),
        "block_gap": s(26),
        "bar_w": s(8), "bar_gap": s(18),
        "img_max_h": s(390), "img_radius": s(18),
        "text_color": (33, 37, 48),
        "sub_color": (90, 96, 110),
        "accent": theme["accent"],
        "code_bg": (238, 240, 248),
        "code_fg": (90, 80, 170),
        "quote_bg": (246, 248, 252),
        "quote_bar": theme["accent"],
        "quote_text": (70, 80, 96),
        "table_border": (224, 228, 236),
        "theme": theme,
    }
    return cfg


# ----------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="把 Markdown 渲染成小红书图片（纯命令行，无浏览器）")
    ap.add_argument("md", help="Markdown 文件路径")
    ap.add_argument("-o", "--out", default="output", help="输出目录（默认 ./output）")
    ap.add_argument("--theme", default="indigo", choices=list(THEMES.keys()), help="配色主题")
    ap.add_argument("--width", type=int, default=1080, help="图片宽度像素（默认 1080）")
    ap.add_argument("--margin", type=int, default=0,
                    help="外边框宽度px（默认 0=满版铺满；>0 时显示渐变边框+圆角白卡）")
    ap.add_argument("--ratio-w", type=float, default=3, help="宽比（默认 3）")
    ap.add_argument("--ratio-h", type=float, default=4, help="高比（默认 4）")
    ap.add_argument("--scale", type=int, default=2, help="超采样倍数，越大越清晰（默认 2）")
    ap.add_argument("--format", default="png", choices=["png", "jpg"], help="输出格式")
    args = ap.parse_args()

    if not os.path.isfile(args.md):
        sys.stderr.write(f"找不到文件: {args.md}\n")
        sys.exit(1)

    with open(args.md, "r", encoding="utf-8") as f:
        text = f.read()

    theme = THEMES[args.theme]
    cfg = build_cfg(args, theme)
    fb = FontBook()

    blocks = parse_markdown(text)
    rows = build_rows(blocks, cfg, fb)
    pages = paginate(rows, cfg["content_h"])

    stem = os.path.splitext(os.path.basename(args.md))[0]
    footer_left = stem.split("：")[-1].split(":")[-1].strip()
    if len(footer_left) > 24:
        footer_left = footer_left[:24]

    os.makedirs(args.out, exist_ok=True)
    total = len(pages)
    out_paths = []
    for idx, pg in enumerate(pages, 1):
        card = render_card(pg, cfg, fb, idx, total, footer_left)
        # 缩放回目标尺寸
        target = (args.width, int(args.width * args.ratio_h / args.ratio_w))
        if cfg["SS"] != 1:
            card = card.resize(target, Image.LANCZOS)
        ext = "jpg" if args.format == "jpg" else "png"
        name = f"{stem}_{idx:02d}.{ext}"
        # 文件名安全化
        name = re.sub(r"[\\/:*?\"<>|]", "_", name)
        path = os.path.abspath(os.path.join(args.out, name))
        if ext == "jpg":
            card.save(path, "JPEG", quality=92)
        else:
            card.save(path, "PNG")
        out_paths.append(path)

    print(f"✅ 生成 {total} 张图片 ({target[0]}x{target[1]}, 主题 {args.theme}):")
    for p in out_paths:
        print("  " + p)


if __name__ == "__main__":
    main()
