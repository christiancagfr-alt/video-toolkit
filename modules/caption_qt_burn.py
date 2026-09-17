"""Qt caption burn-in: same paint engine as live preview → WYSIWYG export.

libass glyph widths ≠ Qt metrics caused huge export word gaps and made the
word-spacing spinner feel ineffective. Export now paints ARGB frames with Qt
and overlays them in FFmpeg (ASS kept only as fallback).
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import time
from pathlib import Path

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import (
    QBrush, QColor, QFont, QFontInfo, QImage, QPainter, QPainterPath, QPen,
)

from .language_style import effective_letter_spacing
from .platform_utils import instance_temp_dir
from .settings_page import hidden_kwargs


def normalize_caption_paint_settings(settings: dict, sample_text: str = "") -> dict:
    """Resolve font family + letter spacing the same way live preview does."""
    from .dynamic_caption_page import (
        caption_layout_context, ensure_font_in_render_dir, resolve_caption_preset,
    )

    out = dict(settings or {})
    out["letter_spacing"] = effective_letter_spacing(out, sample_text or "")
    try:
        probe = caption_layout_context(out)[0]
        resolved = QFontInfo(probe).family()
        if resolved:
            out["font"] = resolved
            ensure_font_in_render_dir(resolved)
    except Exception:
        pass
    # Ensure preset effect fields are present for paint
    try:
        preset = resolve_caption_preset(out)
        if preset.get("effect") and not out.get("effect"):
            out["effect"] = preset.get("effect")
        for key in (
            "semantic_large_ratio", "semantic_small_ratio", "semantic_lead_ms",
            "semantic_max_lines", "semantic_small_words",
        ):
            if out.get(key) in (None, "") and key in preset:
                out[key] = preset[key]
    except Exception:
        pass
    return out


def _safe_float(value, default=0.0):
    if value is None or value == "":
        return float(default)
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def karaoke_lead_seconds(settings=None) -> float:
    """可选跟读提前量。默认 0：严格跟词级 ASR 对口型（不靠百分比/抢拍）。

    仅当 settings 显式提供 karaoke_lead_ms 时才提前。
    """
    if isinstance(settings, dict):
        raw = settings.get("karaoke_lead_ms", None)
        if raw is not None and raw != "":
            try:
                return max(0.0, min(0.35, float(raw) / 1000.0))
            except (TypeError, ValueError):
                pass
    return 0.0


def _token_core(text: str) -> str:
    return "".join(
        ch for ch in str(text or "").strip().lower()
        if ch.isalnum() or "\u3400" <= ch <= "\u9fff"
    )


def fit_phrase_word_timings(start, end, n, phrase_words):
    """句内词级 → 显示 token 时间窗（始终锚定 ASR，不对整句做假均分）。

    - 数量一致：原样用每个 ASR 词的起止
    - 数量不一致：按序把 ASR 词摊到显示 token 上，保留真实开口时间
    - 完全无词级：才退回句内均分
    """
    start_f, end_f = float(start), float(end)
    n = int(n or 0)
    if n <= 0:
        return []
    words = list(phrase_words or [])
    m = len(words)
    if m == n and m > 0:
        return [(float(words[i][0]), float(words[i][1])) for i in range(n)]
    if m <= 0:
        dur = max(0.08, (end_f - start_f) / n)
        return [(start_f + dur * i, min(end_f, start_f + dur * (i + 1))) for i in range(n)]
    result = []
    for i in range(n):
        a = int(i * m / n)
        b = max(a + 1, int((i + 1) * m / n))
        b = min(m, b)
        a = min(max(0, a), m - 1)
        ts = float(words[a][0])
        te = float(words[b - 1][1])
        # 多个 token 分到同一词：切开该词时间窗，避免永远只亮第一个
        if b == a + 1:
            owners = [
                j for j in range(n)
                if int(j * m / n) == a
            ]
            if len(owners) > 1:
                w_ts, w_te = float(words[a][0]), float(words[a][1])
                if w_te <= w_ts:
                    w_te = w_ts + 0.04
                slot = (w_te - w_ts) / len(owners)
                k = owners.index(i) if i in owners else 0
                ts = w_ts + slot * k
                te = w_ts + slot * (k + 1)
        if te <= ts:
            te = min(end_f, ts + 0.04)
        result.append((ts, te))
    return result


def cut_from_word_timings(t_sec, timings, tokens, lead_sec=0.0):
    """按真实词窗推进跟读。优先落在 [ts, te]，间隙粘在已开始的一词。"""
    tokens = list(tokens or [])
    n = len(tokens)
    if n <= 0:
        return 0, ""
    if not timings:
        return 1, tokens[0]
    t_eff = float(t_sec or 0.0) + max(0.0, float(lead_sec or 0.0))
    for i, (ts, te) in enumerate(timings):
        if float(ts) <= t_eff <= float(te):
            return i + 1, tokens[i]
    cut = 0
    for i, (ts, _te) in enumerate(timings):
        if t_eff >= float(ts):
            cut = i + 1
    if cut <= 0:
        cut = 1
    cut = min(cut, n)
    return cut, tokens[cut - 1]


def _active_asr_word_index(t_sec, phrase_words) -> int:
    """当前时刻对应的句内 ASR 词下标；间隙粘在已开始的一词。"""
    words = list(phrase_words or [])
    if not words:
        return -1
    t = float(t_sec or 0.0)
    for i, w in enumerate(words):
        if float(w[0]) <= t <= float(w[1]):
            return i
    wi = -1
    for i, w in enumerate(words):
        if t >= float(w[0]):
            wi = i
    return wi


def resolve_lip_sync_cut(t_sec, tokens, phrase_words, start, end, settings=None):
    """对口型 cut：永远先看词级 ASR，再映射到当前显示 token。

    换预设/改描边只影响画法，不应改口型钟。词数不一致时也按 ASR 词序映射，
    禁止「句长÷词数」假均分（那会造成有的句不跟、有的句飞快）。
    """
    tokens = list(tokens or [])
    n = len(tokens)
    if n <= 0:
        return 0, "", []
    words = list(phrase_words or [])
    timings = fit_phrase_word_timings(start, end, n, words)
    lead = karaoke_lead_seconds(settings)
    t = float(t_sec or 0.0) + lead
    m = len(words)

    if m > 0:
        wi = _active_asr_word_index(t, words)
        if wi >= 0:
            if m == n:
                return wi + 1, tokens[wi], timings
            # 文本对齐（校对后用词）
            wcore = _token_core(words[wi][2] if len(words[wi]) > 2 else "")
            if wcore:
                for ti, tok in enumerate(tokens):
                    if _token_core(tok) == wcore:
                        return ti + 1, tokens[ti], timings
            # 序映射：跟 ASR 词进度，不跟句时长百分比
            ti = int((wi + 1e-6) * n / m)
            ti = max(0, min(n - 1, ti))
            return ti + 1, tokens[ti], timings

    cut, active = cut_from_word_timings(t_sec, timings, tokens, lead)
    return cut, active, timings


def caption_content_at(t_sec: float, phrase_srt: str, word_srt: str, settings: dict):
    """Return (phrase_text, tokens, cut_1based, active_word) at time t."""
    from .dynamic_caption_page import parse_srt, tokens_for, group_word_srt

    t = max(0.0, float(t_sec or 0.0))
    phrases = parse_srt(phrase_srt or "")
    words = parse_srt(word_srt or "")
    if not phrases and words:
        max_chars = max(18, int(settings.get("line_length") or 18) * 2)
        max_words = int(settings.get("max_words") or 7)
        phrases = parse_srt(group_word_srt(word_srt, max_chars=max_chars, max_words=max_words))
    if not phrases:
        return "", [], 0, ""

    event = next((item for item in phrases if item[0] <= t < item[1]), None)
    if event is None:
        return "", [], 0, ""

    start, end, text = float(event[0]), float(event[1]), str(event[2] or "")
    tokens = tokens_for(text)
    n = len(tokens)
    if n <= 0:
        return text, [], 0, ""

    free_static = (
        settings.get("caption_mode") == "自由文案动画（不对口型）"
        and settings.get("free_animation") == "整段固定"
    )
    if free_static:
        return text, tokens, 0, ""

    phrase_words = [
        w for w in words
        if start - 0.01 <= (w[0] + w[1]) / 2 <= end + 0.01
    ]
    cut, active, _timings = resolve_lip_sync_cut(t, tokens, phrase_words, start, end, settings)
    return text, tokens, cut, active


def caption_visual_key(t_sec: float, phrase_srt: str, word_srt: str, settings: dict) -> tuple:
    text, tokens, cut, _active = caption_content_at(t_sec, phrase_srt, word_srt, settings)
    effect = str((resolve_effect(settings) or ""))
    return (text, cut, effect, int(settings.get("word_spacing") or 0), int(settings.get("letter_spacing") or 0))


def resolve_effect(settings: dict) -> str:
    from .dynamic_caption_page import resolve_caption_preset
    preset = resolve_caption_preset(settings)
    return str(preset.get("effect") or settings.get("effect") or "word_color")


def paint_caption_overlay_image(
    settings: dict,
    phrase_srt: str,
    word_srt: str,
    t_sec: float,
    size=(1080, 1920),
) -> QImage:
    """Paint captions onto a transparent 1080×1920 ARGB image (preview==export)."""
    from .dynamic_caption_page import (
        SEMANTIC_LAYOUT_EFFECTS, caption_layout_context, caption_page_geometry,
        caption_uses_bold_face, caption_wrapped_lines, resolve_caption_preset,
        select_emphasis_words, semantic_stack_geometry, semantic_stack_layout,
    )

    w, h = int(size[0] or 1080), int(size[1] or 1920)
    image = QImage(w, h, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)

    sample = ""
    try:
        sample = str(parse_first_text(phrase_srt, word_srt) or "")
    except Exception:
        sample = ""
    settings = normalize_caption_paint_settings(settings, sample)
    preset = resolve_caption_preset(settings)
    context = caption_layout_context(settings)
    font, metrics, _gap, _line_gap, _max_w = context

    text, tokens, cut, active_word = caption_content_at(t_sec, phrase_srt, word_srt, settings)
    if not tokens:
        return image

    fixed_all = (
        settings.get("caption_mode") == "自由文案动画（不对口型）"
        and settings.get("free_animation") == "整段固定"
    )
    free_static = fixed_all
    effect = preset.get("effect") or "word_color"
    if free_static:
        effect = "plain"
        cut = 0

    base_color = QColor(settings.get("text_color") or "#FFFFFF")
    outline = QColor(settings.get("outline_color") or "#111827")
    highlight = QColor(settings.get("highlight_color") or "#FF2D2D")
    background_color = QColor(settings.get("background_color") or "#168AAD")
    active_text_color = QColor(settings.get("active_text_color") or "#FFFFFF")
    pen_width = max(1.0, float(settings.get("outline_width") or 3))

    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)

    try:
        if preset.get("effect") == "three_bands":
            paint_three_bands(painter, text, settings)
        elif effect in SEMANTIC_LAYOUT_EFFECTS:
            _paint_semantic(
                painter, settings, preset, tokens, cut, effect,
                base_color, outline, highlight, pen_width, fixed_all,
            )
        else:
            _paint_standard(
                painter, settings, context, font, metrics, tokens, cut, effect,
                base_color, outline, highlight, background_color, active_text_color,
                pen_width, fixed_all,
            )
    finally:
        painter.end()
    return image


def three_band_geometry(text, settings):
    """Shared 1080x1920 geometry for preview, Qt burn and ASS fallback."""
    import textwrap
    from PySide6.QtGui import QFontMetricsF
    lines = [line.strip() for line in str(text).splitlines() if line.strip()]
    if len(lines) == 1:
        lines = textwrap.wrap(lines[0], width=max(8, int(settings.get("line_length", 42))), break_long_words=False)
    if not lines:
        return QFont(), []
    font = QFont(str(settings.get("font") or "Roboto Condensed"))
    font.setBold(True)
    font.setPixelSize(max(20, int(settings.get("font_size") or 64)))
    metrics = QFontMetricsF(font)
    pad_x = max(0, int(settings.get("highlight_padding", 28)))
    pad_y = max(0, int(settings.get("highlight_padding_y", 18)))
    max_width = 1080 * max(20, min(100, float(settings.get("line_width", 94)))) / 100
    widest = max(metrics.horizontalAdvance(line) for line in lines)
    if widest + 2 * pad_x > max_width:
        font.setPixelSize(max(20, int(font.pixelSize() * max(1, max_width - 2 * pad_x) / max(1, widest))))
        metrics = QFontMetricsF(font)
        # Font fallback and pixel rounding need not scale metrics linearly.
        while font.pixelSize() > 20 and max(metrics.horizontalAdvance(line) for line in lines) + 2 * pad_x > max_width:
            font.setPixelSize(font.pixelSize() - 1)
            metrics = QFontMetricsF(font)
    row_h = metrics.height() + 2 * pad_y
    gap = max(0., row_h * (float(settings.get("line_spacing", 100)) / 100 - 1))
    total = len(lines) * row_h + (len(lines) - 1) * gap
    position = settings.get("position", "画面中间")
    margin = float(settings.get("margin_v", 500))
    y = (1920 - total) / 2 if position == "画面中间" else (margin if position == "顶部" else 1920 - margin - total)
    foreground = [settings.get("text_color") or "#FFFFFF", settings.get("active_text_color") or "#111111", settings.get("outline_color") or "#FFFFFF"]
    backgrounds = [settings.get("background_color") or "#00A526", settings.get("highlight_color") or "#FFFFFF", settings.get("band_last_background") or "#000000"]
    rows = []
    for index, line in enumerate(lines):
        width = metrics.horizontalAdvance(line) + 2 * pad_x
        rect = QRectF((1080 - width) / 2, y, width, row_h)
        rows.append(dict(text=line, rect=rect, baseline=y + pad_y + metrics.ascent(),
                         x=rect.x() + pad_x, foreground=foreground[min(index, 2)], background=backgrounds[min(index, 2)]))
        y += row_h + gap
    return font, rows


def paint_three_bands(painter, text, settings):
    font, rows = three_band_geometry(text, settings)
    painter.save()
    painter.setFont(font)
    for row in rows:
        painter.fillRect(row["rect"], QColor(row["background"]))
        path = QPainterPath()
        path.addText(row["x"], row["baseline"], font, row["text"])
        painter.fillPath(path, QColor(row["foreground"]))
    painter.restore()


def parse_first_text(phrase_srt: str, word_srt: str) -> str:
    from .dynamic_caption_page import parse_srt
    for block in (phrase_srt, word_srt):
        for _s, _e, text in parse_srt(block or "")[:3]:
            if text:
                return text
    return ""


def _paint_semantic(
    painter, settings, preset, tokens, cut, effect,
    base_color, outline, highlight, pen_width, fixed_all,
):
    from .dynamic_caption_page import (
        caption_uses_bold_face, select_emphasis_words,
        semantic_stack_geometry, semantic_stack_layout,
    )

    geo_settings = dict(settings)
    geo_settings["position"] = "画面中间"
    preset_data = preset if isinstance(preset, dict) else {}
    for key in ("semantic_large_ratio", "semantic_small_ratio", "semantic_max_lines"):
        if geo_settings.get(key) in (None, "") and key in preset_data:
            geo_settings[key] = preset_data[key]
    geo_settings.setdefault("semantic_large_ratio", 1.18)
    geo_settings.setdefault("semantic_small_ratio", 0.78)
    geo_settings.setdefault("semantic_max_lines", 5)

    emphasized = select_emphasis_words(tokens)
    full_lines = semantic_stack_layout(tokens, emphasized, geo_settings)
    max_stack_lines = max(3, min(6, int(geo_settings.get("semantic_max_lines") or 5)))
    full_pages = (
        [full_lines]
        if fixed_all or len(full_lines) <= max_stack_lines
        else [full_lines[i:i + max_stack_lines] for i in range(0, len(full_lines), max_stack_lines)]
    ) or [[]]
    spoken_cut = max(1, min(int(cut or len(tokens)), len(tokens))) if cut else len(tokens)
    if effect == "semantic_stack" and cut <= 0:
        spoken_cut = len(tokens)
    spoken = set(range(spoken_cut))
    active_i = spoken_cut - 1 if effect == "semantic_karaoke" else -1

    page_index = 0
    cursor = 0
    for pi, page in enumerate(full_pages):
        count = sum(len(line) for line in page)
        if spoken_cut - 1 < cursor + count:
            page_index = pi
            break
        cursor += count
    page_lines = full_pages[page_index]
    page_token_offset = sum(sum(len(line) for line in full_pages[i]) for i in range(page_index))
    geometry = semantic_stack_geometry(page_lines, geo_settings)
    family = str(settings.get("font") or "Arial")
    bold = caption_uses_bold_face(settings)
    letter = _safe_float(settings.get("letter_spacing"), 0)
    flat_i = 0
    for line, line_geo in zip(page_lines, geometry):
        for item, geo in zip(line, line_geo):
            global_i = page_token_offset + flat_i
            flat_i += 1
            if global_i not in spoken:
                continue
            size = int(item.get("size") or settings.get("font_size") or 86)
            is_active = global_i == active_i
            draw_size = int(round(size * 1.18)) if is_active and effect == "semantic_karaoke" else size
            word_font = QFont(family)
            word_font.setPixelSize(draw_size)
            word_font.setBold(bold)
            word_font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, letter)
            path = QPainterPath()
            path.addText(0, 0, word_font, item["token"])
            fill = highlight if is_active else base_color
            painter.save()
            if is_active and effect == "semantic_karaoke":
                cx = geo["left"] + _safe_float(item.get("width"), size) / 2.0
                cy = geo["baseline"]
                painter.translate(cx, cy)
                painter.scale(1.18, 1.18)
                painter.translate(-cx, -cy)
            painter.translate(geo["left"], geo["baseline"])
            if is_active and effect == "semantic_karaoke":
                glow = QPen(
                    highlight, max(pen_width * 2 + 4, 8),
                    Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin,
                )
                painter.setPen(glow)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawPath(path)
            painter.setPen(QPen(outline, pen_width * 2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(path)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(fill)
            painter.drawPath(path)
            painter.restore()


def _paint_standard(
    painter, settings, context, font, metrics, tokens, cut, effect,
    base_color, outline, highlight, background_color, active_text_color,
    pen_width, fixed_all,
):
    from .dynamic_caption_page import caption_page_geometry, caption_wrapped_lines

    text = " ".join(tokens) if not any(
        "\u3400" <= c <= "\u9fff" for c in "".join(tokens)
    ) else "".join(tokens)
    # Prefer original token joining for CJK already handled by caller text;
    # caption_wrapped_lines wants the phrase string — rebuild carefully
    phrase = "".join(tokens) if any("\u3400" <= c <= "\u9fff" for tok in tokens for c in tok) else " ".join(tokens)
    lines = caption_wrapped_lines(phrase, settings, fixed_all, context)
    max_lines = max(1, min(6, int(settings.get("max_lines") or 2)))
    pages = (
        [lines] if fixed_all
        else [lines[index:index + max_lines] for index in range(0, len(lines), max_lines)]
    ) or [[]]
    active_page = 0
    cursor = 0
    for page_index, page in enumerate(pages):
        count = sum(len(line) for line in page)
        if cut > 0 and cut - 1 < cursor + count:
            active_page = page_index
            break
        cursor += count
    page_lines = pages[active_page]
    geometry = caption_page_geometry(page_lines, settings, context)
    page_token_offset = sum(sum(len(line) for line in pages[i]) for i in range(active_page))
    token_i = page_token_offset
    path_cache = {}
    for line, line_geometry in zip(page_lines, geometry):
        for token, item in zip(line, line_geometry):
            width = item["width"]
            left = item["left"]
            baseline = item["baseline"]
            is_active = cut > 0 and token_i == cut - 1
            # word_color_persist：已读保持跟读色；word_color（经典黄）：仅当前词，读完回原色
            is_spoken = cut > 0 and token_i < cut
            token_i += 1
            if effect == "dual_box":
                pad_x = max(0, int(settings.get("highlight_padding") or 0))
                pad_y = max(0, int(settings.get("highlight_padding_y") or 0))
                box_width = width + pad_x * 2
                box_height = max(float(settings.get("font_size") or 76) * 1.12, metrics.height()) + pad_y * 2
                radius = max(0, min(14.0, box_height * 0.20))
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(highlight if is_active else background_color)
                painter.drawRoundedRect(
                    QRectF(item["x"] - box_width / 2, item["y"] - box_height / 2, box_width, box_height),
                    radius, radius,
                )
            elif is_active and effect in ("descript", "heygen", "highlight"):
                pad_x = max(0, int(settings.get("highlight_padding") or 0))
                pad_y = max(0, int(settings.get("highlight_padding_y") or 0))
                box_width = width + pad_x * 2
                box_height = max(float(settings.get("font_size") or 76) * 1.12, metrics.height()) + pad_y * 2
                radius = max(0, min(18.0, box_height * 0.24))
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(highlight)
                painter.drawRoundedRect(
                    QRectF(item["x"] - box_width / 2, item["y"] - box_height / 2, box_width, box_height),
                    radius, radius,
                )
            path = path_cache.get(token)
            if path is None:
                path = QPainterPath()
                path.addText(0, 0, font, token)
                path_cache[token] = path
            painter.save()
            painter.translate(left, baseline)
            # 仅弹出类效果放大；word_color（经典黄等）只变色跟读，不缩放
            if is_active and effect in ("word_pop_color", "pop"):
                painter.translate(width / 2.0, -metrics.ascent() / 3.0)
                painter.scale(1.12, 1.12)
                painter.translate(-width / 2.0, metrics.ascent() / 3.0)
            if effect == "double_outline":
                painter.setPen(QPen(highlight, (pen_width + 3) * 2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawPath(path)
                painter.setPen(QPen(outline, pen_width * 2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
                painter.drawPath(path)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(base_color)
                painter.drawPath(path)
            else:
                # 弹出类可加跟读色微光；经典黄 word_color 不加，保持原跟读观感
                if is_active and effect in ("word_pop_color", "pop"):
                    glow = QPen(
                        highlight, max(pen_width * 2 + 4, 8),
                        Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin,
                    )
                    painter.setPen(glow)
                    painter.setBrush(Qt.BrushStyle.NoBrush)
                    painter.drawPath(path)
                painter.setPen(QPen(outline, pen_width * 2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawPath(path)
                if effect == "dual_box" and is_active:
                    fill = active_text_color
                elif effect in ("descript", "heygen", "highlight") and is_active:
                    fill = active_text_color
                elif effect == "word_color_persist":
                    fill = highlight if is_spoken else base_color
                elif effect == "word_color":
                    # 经典黄：只有正在读的词变黄，读完立刻回到普通文字色
                    fill = highlight if is_active else base_color
                elif is_active and effect in ("word_pop_color", "pop", "underline"):
                    fill = highlight
                else:
                    fill = base_color
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(fill)
                painter.drawPath(path)
            painter.restore()
            if is_active and effect == "underline":
                painter.setPen(QPen(highlight, max(2, pen_width)))
                painter.drawLine(
                    int(left), int(baseline + metrics.descent() + 3),
                    int(left + width), int(baseline + metrics.descent() + 3),
                )


def collect_caption_cut_times(
    phrase_srt: str, word_srt: str, duration: float, settings: dict | None = None,
) -> list[float]:
    from .dynamic_caption_page import parse_srt

    duration = max(0.05, float(duration or 0.05))
    lead = karaoke_lead_seconds(settings)  # 默认 0；仅显式 karaoke_lead_ms 时加提前切点
    times = {0.0, duration}
    for s, e, _ in parse_srt(phrase_srt or ""):
        times.add(max(0.0, min(duration, float(s))))
        times.add(max(0.0, min(duration, float(e))))
    for s, e, _ in parse_srt(word_srt or ""):
        ws, we = float(s), float(e)
        times.add(max(0.0, min(duration, ws)))
        times.add(max(0.0, min(duration, we)))
        if lead > 0.001:
            times.add(max(0.0, min(duration, ws - lead)))
    # Slight mid-samples so karaoke doesn't miss boundaries
    ordered = sorted(times)
    denser = set(ordered)
    for a, b in zip(ordered, ordered[1:]):
        if b - a > 0.55:
            denser.add(round((a + b) / 2, 3))
    return sorted(denser)


def bake_qt_caption_overlay_mov(
    ffmpeg: str,
    *,
    duration: float,
    settings: dict,
    phrase_srt: str,
    word_srt: str,
    target_w: int,
    target_h: int,
    log=None,
) -> Path:
    """Paint Qt caption segments → concat → transparent MOV (yuva420p/png)."""
    def _log(msg: str):
        if callable(log):
            try:
                log(msg)
            except Exception:
                pass

    duration = max(0.05, float(duration or 0.05))
    tw = max(2, int(target_w or 1080))
    th = max(2, int(target_h or 1920))
    tw -= tw % 2
    th -= th % 2

    work = instance_temp_dir("qt_cap") / hashlib.md5(
        f"{time.time_ns()}|{tw}x{th}|{duration:.3f}".encode()
    ).hexdigest()[:12]
    work.mkdir(parents=True, exist_ok=True)

    settings = normalize_caption_paint_settings(
        settings, parse_first_text(phrase_srt, word_srt)
    )
    cuts = collect_caption_cut_times(phrase_srt, word_srt, duration, settings)
    # Build segments [t0,t1) with stable visual key
    segments = []
    last_key = None
    seg_start = 0.0
    for i, t in enumerate(cuts):
        key = caption_visual_key(min(t + 0.001, duration), phrase_srt, word_srt, settings)
        if last_key is None:
            last_key = key
            seg_start = 0.0
            continue
        if key != last_key:
            segments.append((seg_start, t, last_key))
            seg_start = t
            last_key = key
    segments.append((seg_start, duration, last_key))
    # Blank states are real time: removing them advances all later captions.
    segments = [(a, b, k) for a, b, k in segments if b > a]

    # 禁止「合并不同跟读状态」：用中间段的 cut 覆盖整段会让高亮提前（导出字幕偏快）。
    # 段数过多时直接回退 ASS，而不是错合状态。
    cue_count = str(phrase_srt or "").count("-->") + str(word_srt or "").count("-->")
    max_segments = 360
    if len(segments) > max_segments or cue_count > 900 or duration > 900:
        raise RuntimeError(
            f"字幕过密/过长（cue≈{cue_count}, 段={len(segments)}, {duration:.0f}s），"
            f"改用 ASS 烧录以保证口型不被错误合并"
        )

    # 几何按 1080×1920 排版；勿缩画布（会画到画外）。靠限段数 + 及时释放控内存。
    paint_w, paint_h = 1080, 1920

    if not segments:
        # empty transparent still
        blank = QImage(paint_w, paint_h, QImage.Format.Format_ARGB32_Premultiplied)
        blank.fill(Qt.GlobalColor.transparent)
        png = work / "blank.png"
        blank.save(str(png), "PNG")
        segments = [(0.0, duration, ("", 0, "", 0, 0))]
        png_paths = [png]
    else:
        png_paths = []
        _log(f"Qt 字幕烧录：{len(segments)} 个画面状态…")
        for idx, (t0, t1, _key) in enumerate(segments):
            mid = (t0 + t1) / 2.0
            img = paint_caption_overlay_image(
                settings, phrase_srt, word_srt, mid, size=(paint_w, paint_h),
            )
            png = work / f"c{idx:04d}.png"
            if not img.save(str(png), "PNG"):
                raise RuntimeError(f"无法写入字幕帧：{png.name}")
            png_paths.append(png)
            # 尽快释放大图，避免批量合成时内存堆积崩溃
            del img
            if idx and idx % 30 == 0:
                _log(f"  · Qt 字幕已绘制 {idx}/{len(segments)} …")
                try:
                    import gc
                    gc.collect()
                except Exception:
                    pass
    # concat demuxer list
    list_path = work / "list.txt"
    lines = []
    for png, (t0, t1, _) in zip(png_paths, segments):
        # concat paths: escape single quotes
        p = str(png.resolve()).replace("\\", "/").replace("'", r"'\''")
        lines.append(f"file '{p}'")
        lines.append("option framerate 1000")
        lines.append(f"duration {t1 - t0:.6f}")
    # last file must repeat without duration
    last = str(png_paths[-1].resolve()).replace("\\", "/").replace("'", r"'\''")
    lines.append(f"file '{last}'")
    lines.append("option framerate 1000")
    list_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    out_mov = work / "caption_overlay.mov"
    # PNG codec keeps alpha; then scale to target
    command = [
        str(ffmpeg), "-y", "-hide_banner", "-loglevel", "error",
        "-f", "concat", "-safe", "0", "-i", str(list_path),
        "-vf", f"scale={tw}:{th}:flags=lanczos,format=rgba",
        "-c:v", "png", "-fps_mode", "vfr", "-an",
        "-t", f"{duration:.4f}",
        str(out_mov),
    ]
    result = subprocess.run(
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace", **hidden_kwargs(),
    )
    if result.returncode != 0 or not out_mov.is_file() or out_mov.stat().st_size < 64:
        err = (result.stderr or "")[-800:]
        raise RuntimeError(f"Qt 字幕透明轨合成失败：{err}")
    _log(f"Qt 字幕透明轨就绪：{len(segments)} 段 → {out_mov.name}")
    # Persist segment meta for debug
    try:
        (work / "meta.json").write_text(
            json.dumps({"segments": len(segments), "duration": duration, "size": [tw, th]}, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:
        pass
    return out_mov


def compose_vf_with_qt_overlay(v_filter_str: str, caption_input_index: int) -> str:
    """scale/crop chain on [0:v], then overlay Qt caption from input N."""
    parts = []
    if v_filter_str:
        parts.append(str(v_filter_str).strip().strip(","))
    base = ",".join(p for p in parts if p)
    if base:
        # [0:v]base[vbase];[N:v]format=rgba[cap];[vbase][cap]overlay...
        # When using -vf only (single stream), we need filter_complex instead.
        return base  # caller should use filter_complex helper
    return ""


def build_qt_caption_filter_complex(
    v_filter_str: str,
    caption_input_index: int,
    *,
    out_label: str = "outv",
) -> str:
    """filter_complex: process main video then overlay Qt RGBA caption track."""
    if v_filter_str:
        chain = str(v_filter_str).strip().strip(",")
        prefix = f"[0:v]{chain}[vbase]"
    else:
        prefix = "[0:v]setpts=PTS-STARTPTS[vbase]"
    cap = f"[{caption_input_index}:v]format=rgba,setpts=PTS-STARTPTS[cap]"
    overlay = f"[vbase][cap]overlay=0:0:format=auto:eof_action=pass[{out_label}]"
    return ";".join([prefix, cap, overlay])
