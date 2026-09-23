"""Shared opt-in rich caption painter for live preview and Qt export.

QTextLayout shapes whole paragraphs (including Arabic/RTL); formatting spans are
stored as Unicode code point offsets and converted to Qt UTF-16 only at painting.
Existing presets are untouched when there is no matching local style or rule.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QFont, QPen, QTextCharFormat, QTextLayout, QTextOption


def utf16_offset(text, index):
    return len(text[:index].encode("utf-16-le")) // 2


def codepoint_offset(text, qt_index):
    return len(text.encode("utf-16-le")[:qt_index*2].decode("utf-16-le", errors="ignore"))


def clean_style(style):
    result = {}
    for key in ("color", "background"):
        value = str(style.get(key, ""))
        if re.fullmatch(r"#[0-9a-fA-F]{6}", value):
            result[key] = value
    if style.get("font"):
        result["font"] = str(style["font"])[:100]
    if style.get("size"):
        result["size"] = max(20, min(220, int(style["size"])))
    for key in ("bold", "italic"):
        if key in style:
            result[key] = bool(style[key])
    return result


@lru_cache(maxsize=256)
def _resolve(text, config_json):
    config = json.loads(config_json)
    spans = []
    for rule in config.get("rules", [])[:200]:
        kind = rule.get("kind", "keyword")
        keyword = str(rule.get("keyword", ""))
        if kind == "number":
            pattern = r"\d+(?:[.,]\d+)*%?"
        elif kind == "keyword" and keyword:
            pattern = re.escape(keyword)
        else:
            continue
        for match in re.finditer(pattern, text, re.IGNORECASE):
            spans.append((match.start(), match.end(), clean_style(rule.get("style", {}))))
    for item in config.get("ranges", [])[:1000]:
        if item.get("text") == text:
            a, b = int(item.get("start", 0)), int(item.get("end", 0))
            if 0 <= a < b <= len(text):
                spans.append((a, b, clean_style(item.get("style", {}))))
    return spans


def style_spans(text, settings):
    return _resolve(text, json.dumps(settings.get("rich_caption", {}), sort_keys=True, ensure_ascii=False))


def paint_rich_caption(painter, text, settings, cut=0, tokens=None):
    spans = style_spans(text, settings)
    if not spans:
        return False
    from .dynamic_caption_page import caption_layout_context
    font, _, _, _, width = caption_layout_context(settings)
    # Qt line separators retain explicit line breaks inside a single shaped layout.
    display = text.replace("\n", "\u2028")
    layout = QTextLayout(display, font)
    option = QTextOption()
    option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
    option.setAlignment(Qt.AlignmentFlag.AlignHCenter)
    layout.setTextOption(option)
    styles = [dict() for _ in text]
    for a, b, style in spans:
        for index in range(a, b):
            styles[index].update(style)
    active_start, active_end = -1, -1
    cursor = 0
    for index, token in enumerate(tokens or []):
        found = text.find(token, cursor)
        if found < 0:
            continue
        if index == cut-1:
            active_start, active_end = found, found+len(token)
            break
        cursor = found+len(token)
    if settings.get("rich_caption", {}).get("karaoke", True) and active_start >= 0:
        for index in range(active_start, active_end):
            styles[index]["color"] = settings.get("active_text_color") or "#FFFFFF"
            styles[index]["background"] = settings.get("highlight_color") or "#0055FF"
    formats = []
    start = 0
    while start < len(styles):
        end = start+1
        while end < len(styles) and styles[end] == styles[start]:
            end += 1
        style = styles[start]
        fmt = QTextCharFormat()
        local_font = QFont(font)
        if style.get("font"):
            local_font.setFamily(style["font"])
        if style.get("size"):
            local_font.setPixelSize(style["size"])
        if "bold" in style:
            local_font.setBold(style["bold"])
        if "italic" in style:
            local_font.setItalic(style["italic"])
        fmt.setFont(local_font)
        fmt.setForeground(QColor(style.get("color", settings.get("text_color", "#FFFFFF"))))
        if style.get("background"):
            fmt.setBackground(QColor(style["background"]))
        if float(settings.get("outline_width", 0)) > 0:
            fmt.setTextOutline(QPen(QColor(settings.get("outline_color", "#111111")), float(settings["outline_width"])))
        item = QTextLayout.FormatRange()
        item.start = utf16_offset(text, start)
        item.length = utf16_offset(text, end)-item.start
        item.format = fmt
        formats.append(item)
        start = end
    layout.setFormats(formats)
    layout.beginLayout()
    lines = []
    while True:
        line = layout.createLine()
        if not line.isValid():
            break
        line.setLineWidth(width)
        lines.append(line)
    layout.endLayout()
    active_qt = utf16_offset(text, max(0, active_start))
    active_line = next((i for i, line in enumerate(lines)
                        if line.textStart() <= active_qt < line.textStart()+line.textLength()), 0)
    max_lines = max(1, int(settings.get("max_lines", 2)))
    first = (active_line//max_lines)*max_lines
    chosen = lines[first:first+max_lines]
    spacing = max(.5, float(settings.get("line_spacing", 100))/100)
    height = sum(line.height()*spacing for line in chosen)
    position = settings.get("position", "底部")
    margin = float(settings.get("margin_v", 300))
    y = margin if position == "顶部" else ((1920-height)/2 if position == "画面中间" else 1920-margin-height)
    painter.save()
    for line in chosen:
        line.draw(painter, QPointF((1080-width)/2, y))
        y += line.height()*spacing
    painter.restore()
    return True
