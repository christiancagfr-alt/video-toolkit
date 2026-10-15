from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from difflib import SequenceMatcher
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from .path_picker import VIDEO_EXTENSIONS, collect_files, natural_key
from .settings_page import hidden_kwargs
from .video_encoding import (
    ENCODER_LABELS, encoder_args, resolve_encoder, calculate_target_size,
    davinci_safe_mux_args, remux_zero_start, wrap_with_intro_outro,
)


# 合并转场预设：UI 显示名 → FFmpeg xfade 参数。
# 达芬奇「平滑剪接」依赖光流，此处用 hblur 短过渡近似；Crash Zoom 用 zoomin 近似。
MERGE_TRANSITION_PRESETS = {
    "无转场": None,
    # —— 达芬奇风格（与 Resolve 转场库对应）——
    "交叉叠化": {"xfade": "dissolve", "duration": 0.65},
    "Cross Dissolve": {"xfade": "dissolve", "duration": 0.65},
    "Crash Zoom": {"xfade": "zoomin", "duration": 0.42},
    "平滑剪接": {"xfade": "hblur", "duration": 0.28},
    # —— 常用 xfade ——
    # 口播成片默认偏短转场，避免长叠化像“卡一下再说话”
    "淡入淡出": {"xfade": "fade", "duration": 0.22},
    "溶解": {"xfade": "dissolve", "duration": 0.28},
    "淡入黑场": {"xfade": "fadeblack", "duration": 0.28},
    "淡入白场": {"xfade": "fadewhite", "duration": 0.28},
    "向左滑动": {"xfade": "slideleft", "duration": 0.28},
    "向右滑动": {"xfade": "slideright", "duration": 0.28},
    "向上滑动": {"xfade": "slideup", "duration": 0.28},
    "向下滑动": {"xfade": "slidedown", "duration": 0.28},
    "直线向左擦除": {"xfade": "wipeleft", "duration": 0.25},
    "直线向右擦除": {"xfade": "wiperight", "duration": 0.25},
    "直线向上擦除": {"xfade": "wipeup", "duration": 0.25},
    "直线向下擦除": {"xfade": "wipedown", "duration": 0.25},
    "圆形打开": {"xfade": "circleopen", "duration": 0.28},
    "圆形关闭": {"xfade": "circleclose", "duration": 0.28},
    "水平打开": {"xfade": "horzopen", "duration": 0.25},
    "垂直打开": {"xfade": "vertopen", "duration": 0.25},
    "像素化": {"xfade": "pixelize", "duration": 0.28},
    "径向模糊": {"xfade": "radial", "duration": 0.28},
}


def merge_transition_labels():
    """Ordered labels for the Reels transition combo box."""
    return list(MERGE_TRANSITION_PRESETS.keys())


def resolve_merge_transition(name):
    """Return {xfade, duration} or None for hard cut."""
    return MERGE_TRANSITION_PRESETS.get(str(name or "").strip())


def discover_groups(parent):
    """Discover groups by child folder, or by a shared filename prefix plus numeric suffix."""
    root = Path(parent)
    if not root.is_dir():
        return []
    groups = []
    for folder in sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: natural_key(p.name)):
        clips = collect_files([str(folder)], VIDEO_EXTENSIONS)
        if clips:
            groups.append((folder, [Path(p) for p in clips]))
    direct_clips=sorted(
        (path for path in root.iterdir() if path.is_file() and path.suffix.casefold() in VIDEO_EXTENSIONS),
        key=lambda path:natural_key(path.name),
    )
    if direct_clips:
        buckets={}
        for clip in direct_clips:
            # Flow 等工具常见命名：11-1.mp4、11-2.mp4，或
            # 2-1_202607211405.mp4、2-2_202607211405.mp4。前一段是组号，
            # 后一段是片段号；其后的时间戳/描述不参与分组。
            numbered = re.match(r"^(?P<group>\d+)[-_](?P<part>\d+)(?:[_\s-].*)?$", clip.stem)
            if numbered:
                key = numbered.group("group")
            else:
                key=re.sub(r"(?:[\s_.-]*(?:part|segment|clip|片段)?[\s_.-]*\d+|[\s_.-]*\(\d+\))$","",clip.stem,flags=re.I).strip(" ._-")
            buckets.setdefault(key or clip.stem,[]).append(clip)
        useful=[(name,clips) for name,clips in buckets.items() if clips]
        if len(useful)>1 and any(len(clips)>1 for _name,clips in useful):
            for name,clips in sorted(useful,key=lambda item:natural_key(item[0])):
                groups.append((root/name,sorted(clips,key=lambda path:natural_key(path.name))))
        else:
            groups.append((root,direct_clips))
    return groups


# 分段文案段首序号：1. / 1、 / 1) / （1） / (1) / 第1段
# 捕获序号数字，便于校验是否为连续 1..N（避免正文里的「2.」误切开）
_SEGMENT_PREFIX_RE = re.compile(
    r"^\s*(?:"
    r"(\d{1,3})\s*[.\u3001\uff0e\)\）:：]\s*"
    r"|\(\s*(\d{1,3})\s*\)\s*"
    r"|（\s*(\d{1,3})\s*）\s*"
    r"|第\s*(\d{1,3})\s*[段章节条款]\s*"
    r")"
)
_NUMBERED_START_RE = re.compile(
    r"(?:^|\n)\s*(?:"
    r"(\d{1,3})\s*[.\u3001\uff0e\)\）:：]\s*"
    r"|\(\s*(\d{1,3})\s*\)\s*"
    r"|（\s*(\d{1,3})\s*）\s*"
    r"|第\s*(\d{1,3})\s*[段章节条款]\s*"
    r")"
)


def _strip_segment_prefix(text: str) -> str:
    """去掉段首序号，避免「1.」「2.」干扰相似度匹配。"""
    value = str(text or "").strip()
    if not value:
        return ""
    return _SEGMENT_PREFIX_RE.sub("", value, count=1).strip() or value


def _extract_leading_number(prefix_match) -> int | None:
    if not prefix_match:
        return None
    for g in prefix_match.groups():
        if g is not None and str(g).isdigit():
            return int(g)
    return None


def _split_numbered_script(text: str) -> list[str]:
    """按「连续段首序号 1..N」切开；非连续/正文误匹配则返回空（勿用）。"""
    value = str(text or "").strip()
    if not value:
        return []
    matches = list(_NUMBERED_START_RE.finditer(value))
    if len(matches) < 2:
        return []
    numbers = []
    for match in matches:
        num = _extract_leading_number(match)
        if num is None:
            return []
        numbers.append(num)
    # 必须是从 1 或 0 起的连续序号，防止正文里的「2. xxx」误分段
    start_num = numbers[0]
    if start_num not in (0, 1):
        return []
    expected = list(range(start_num, start_num + len(numbers)))
    if numbers != expected:
        return []
    # 第一段必须紧贴全文开头（允许空白），避免标题后的「1.」才开始却丢掉标题误当段
    if matches[0].start() > 0 and value[: matches[0].start()].strip():
        # 允许很短标题行；超过 40 字则认为不是规范编号文案
        if len(value[: matches[0].start()].strip()) > 40:
            return []
    segments = []
    for i, match in enumerate(matches):
        body_start = match.end()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(value)
        cleaned = re.sub(r"\s+", " ", value[body_start:body_end].strip())
        if cleaned and cleaned != "---":
            segments.append(cleaned)
    return segments if len(segments) >= 2 else []


def split_group_script(text, expected_count=None):
    """Split pasted group script into per-clip segments.

    When expected_count is known, **only** return a candidate whose length
    exactly equals expected_count. Never return a near-miss numbered split
    (that was scrambling trim/sort since v1.7.48).
    """
    value = str(text or "").strip()
    if not value:
        return []
    value = re.sub(r"(?m)^\s*---+\s*$", "\n\n", value)
    numbered = _split_numbered_script(value)
    lines = [line.strip() for line in value.splitlines() if line.strip() and line.strip() != "---"]
    blocks = [
        re.sub(r"\s+", " ", block).strip()
        for block in re.split(r"\r?\n\s*\r?\n", value)
        if block.strip()
    ]
    if expected_count is not None:
        expected = int(expected_count)
        # 1) 严格连续序号 1..N（已防正文误切）→ 2) 空行段 → 3) 纯行
        for candidate in (numbered, blocks, lines):
            if len(candidate) == expected:
                return candidate
        # 段数对不上：优先返回空行分段（最接近真实结构），交给上层报错/回退
        ranked = sorted(
            [blocks, lines, numbered],
            key=lambda c: (
                abs(len(c) - expected) if c else 10_000,
                0 if c is blocks else 1 if c is lines else 2,
            ),
        )
        for candidate in ranked:
            if candidate:
                return candidate
        return []
    # 无期望段数：空行优先，其次严格序号，再逐行
    if len(blocks) >= 2:
        return blocks
    if numbered:
        return numbered
    return lines


def _script_key(folder):
    """Stable key for scripts dict (resolve path)."""
    try:
        return str(Path(folder).resolve())
    except Exception:
        return str(folder)


def lookup_group_script(scripts, folder):
    """Find pasted group script even when path separators/casing differ."""
    scripts = scripts or {}
    if not scripts:
        return ""
    folder = Path(folder)
    candidates = []
    try:
        resolved = folder.resolve()
        candidates.extend([str(resolved), str(resolved).replace("\\", "/"), str(resolved).replace("/", "\\")])
    except Exception:
        resolved = folder
    candidates.extend([str(folder), str(folder).replace("\\", "/"), folder.name])
    for key in candidates:
        value = scripts.get(key)
        if str(value or "").strip():
            return str(value)
    # Path-equality scan (handles soft links / case differences on Windows)
    try:
        target = folder.resolve()
    except Exception:
        target = folder
    for key, value in scripts.items():
        if not str(value or "").strip():
            continue
        try:
            if Path(key).resolve() == target:
                return str(value)
        except Exception:
            if Path(key).name == folder.name:
                return str(value)
    return ""


# 希腊语匹配：去变音、终态 sigma、常见拉丁/希腊同形混入（ASR 常混）
_GREEK_HOMOGLYPHS = str.maketrans({
    "ς": "σ",
    "Ϲ": "σ", "ϲ": "σ",
    "A": "α", "a": "α", "Á": "α", "á": "α",
    "B": "β", "b": "β",
    "E": "ε", "e": "ε", "É": "ε", "é": "ε",
    "H": "η", "h": "η",
    "I": "ι", "i": "ι", "Í": "ι", "í": "ι",
    "K": "κ", "k": "κ",
    "M": "μ", "m": "μ",
    "N": "ν", "n": "ν",
    "O": "ο", "o": "ο", "Ó": "ο", "ó": "ο",
    "P": "ρ", "p": "ρ",
    "T": "τ", "t": "τ",
    "X": "χ", "x": "χ",
    "Y": "υ", "y": "υ",
    "Z": "ζ", "z": "ζ",
})
_GREEK_STOP = {
    "και", "το", "τα", "τη", "της", "του", "των", "να", "για", "με", "σε",
    "μου", "σου", "μας", "σας", "απο", "από", "στο", "στη", "στην", "στον",
    "ειναι", "είναι", "θα", "αν", "ή", "η", "ο", "οι", "μια", "ένα", "ενά",
    # 常见虚词/指示：短祷文互相沾边时易抬高 Jaccard
    "δε", "δεν", "που", "πως", "ως", "οτι", "ότι", "αυτο", "αυτό", "αυτη", "αυτή",
    "αυτα", "αυτά", "εκει", "εκεί", "εδω", "εδώ", "τωρα", "τώρα", "μονο", "μόνο",
    "ομως", "όμως", "αλλα", "αλλά", "εσυ", "εσύ", "εγω", "εγώ", "μας", "σας",
}


def _text_has_greek(value: str) -> bool:
    return any(
        "\u0370" <= ch <= "\u03ff" or "\u1f00" <= ch <= "\u1fff"
        for ch in str(value or "")
    )


def _normalize_match_text(value: str) -> str:
    """Fold diacritics / final sigma / Latin-Greek lookalikes for matching only.

    Homoglyph folding is only applied when the string contains Greek letters,
    so English/Portuguese matching is not rewritten into Greek stopwords.
    """
    import unicodedata
    text = str(value or "")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    if _text_has_greek(text):
        text = text.translate(_GREEK_HOMOGLYPHS)
    else:
        # 无希腊字母时仍折叠终态 σ（极少见）与大小写
        text = text.replace("ς", "σ")
    return text.casefold()


def _plain_text(value):
    # Include Greek Extended (U+1F00–U+1FFF) used by polytonic / some ASR outputs.
    folded = _normalize_match_text(value)
    return re.sub(
        r"[^0-9a-z\u00c0-\u024f\u0370-\u03ff\u1f00-\u1fff\u0400-\u04ff\u3400-\u9fff]+",
        "",
        folded,
    )


def _word_tokens(value):
    folded = _normalize_match_text(value)
    tokens = set(re.findall(
        r"[0-9a-z\u00c0-\u024f\u0370-\u03ff\u1f00-\u1fff\u0400-\u04ff\u3400-\u9fff]+",
        folded,
    ))
    # 仅希腊文段去掉虚词，避免「και/το」沾边；拉丁文保留原 token
    if _text_has_greek(value):
        return {t for t in tokens if t not in _GREEK_STOP and len(t) > 1}
    return {t for t in tokens if len(t) > 1}


def _text_similarity(source, target):
    """Multi-signal similarity robust to ASR noise and long script vs short clip text.

    Containment is capped so a short shared opening (e.g. Αμήν) cannot tie
    every long script segment at ~0.95 and scramble assignment.
    """
    s_raw, t_raw = str(source or "").strip(), str(target or "").strip()
    s, t = _plain_text(s_raw), _plain_text(t_raw)
    if not s or not t:
        return 0.0
    full = SequenceMatcher(None, s, t).ratio()
    # Opening words are often the most distinctive per segment.
    plen = min(56, len(s), len(t))
    prefix = SequenceMatcher(None, s[:plen], t[:plen]).ratio() if plen >= 6 else 0.0
    # Partial / containment: shorter spoken text inside longer pasted script (or vice versa).
    shorter, longer = (s, t) if len(s) <= len(t) else (t, s)
    contain = 0.0
    if len(shorter) >= 12 and shorter in longer:
        # 长度差越大，包含分越打折，避免短句「沾边」高分
        ratio = len(shorter) / max(1, len(longer))
        contain = 0.70 + 0.25 * ratio
    elif len(shorter) >= 14 and len(longer) >= len(shorter):
        best = 0.0
        window = len(shorter)
        step = max(1, window // 5)
        for i in range(0, len(longer) - window + 1, step):
            best = max(best, SequenceMatcher(None, shorter, longer[i:i + window]).ratio())
            if best >= 0.92:
                break
        contain = best * 0.85
    ws, wt = _word_tokens(s_raw), _word_tokens(t_raw)
    jacc = (len(ws & wt) / len(ws | wt)) if ws and wt else 0.0
    # 短句：降低 Jaccard 权重，避免虚词带动错误高分
    short = min(len(s), len(t)) < 28 or (len(ws) <= 4 and len(wt) <= 4)
    jacc_w = 0.60 if short else 0.95
    contain_w = 0.55 if short else 0.75
    return max(full, 0.92 * prefix, contain_w * contain, jacc_w * jacc)


def _transcript_variants(analysis_or_text):
    """Collect all usable transcript strings for a clip (SRT preferred, then original)."""
    if analysis_or_text is None:
        return []
    if isinstance(analysis_or_text, str):
        text = analysis_or_text.strip()
        return [text] if text else []
    variants = []
    entries = parse_srt_with_text(analysis_or_text.get("srt", ""))
    srt_text = " ".join(text for _start, _end, text in entries).strip()
    if srt_text:
        variants.append(srt_text)
    original = str(analysis_or_text.get("original") or "").strip()
    if original and original not in variants:
        variants.append(original)
    return variants


def _transcript_for_match(analysis_or_text):
    """Best single transcript string (SRT first, then original)."""
    variants = _transcript_variants(analysis_or_text)
    return variants[0] if variants else ""


def _resolve_transcript(transcripts, clip):
    """Look up transcript by resolved path with a few fallback key forms."""
    path = Path(clip)
    try:
        resolved = path.resolve()
    except Exception:
        resolved = path
    keys = [
        str(resolved),
        str(path),
        str(resolved).replace("\\", "/"),
        str(path).replace("\\", "/"),
    ]
    for key in keys:
        if key in transcripts:
            return transcripts[key]
    # Windows 安装版常见：盘符大小写 / 斜杠不一致导致查不到识别稿 → 误匹配或全空
    try:
        target = resolved
        target_norm = str(target).replace("\\", "/").lower()
        for key, value in transcripts.items():
            try:
                if Path(key).resolve() == target:
                    return value
            except Exception:
                pass
            try:
                if str(key).replace("\\", "/").lower() == target_norm:
                    return value
            except Exception:
                continue
    except Exception:
        pass
    return ""


def _max_weight_assignment(score_matrix):
    """Maximize sum of scores for one-to-one clip→segment assignment.

    score_matrix[clip_i][seg_j] → float. Returns list seg_index → clip_index.
    Uses DP bitmask for n≤12 (exact); larger n uses Hungarian-style O(n³).
    """
    n = len(score_matrix)
    if n == 0:
        return []
    if n == 1:
        return [0]

    if n <= 12:
        # dp[mask] = (best_sum, prev_clip_for_this_popcount_step as packed path via parent)
        size = 1 << n
        dp = [-1e100] * size
        parent = [(-1, -1)] * size  # mask → (prev_mask, clip_used)
        dp[0] = 0.0
        for mask in range(size):
            seg = mask.bit_count()
            if seg >= n:
                continue
            base = dp[mask]
            if base < -1e99:
                continue
            for clip in range(n):
                bit = 1 << clip
                if mask & bit:
                    continue
                nxt = mask | bit
                # Tiny order prior: prefer clip index near seg index when scores tie
                # (helps when ASR of neighboring takes are similar).
                prior = 0.002 * (1.0 - abs(clip - seg) / max(1, n - 1))
                val = base + float(score_matrix[clip][seg]) + prior
                if val > dp[nxt]:
                    dp[nxt] = val
                    parent[nxt] = (mask, clip)
        full = (1 << n) - 1
        if dp[full] < -1e99:
            return list(range(n))
        seg_to_clip = [-1] * n
        mask = full
        for seg in range(n - 1, -1, -1):
            prev, clip = parent[mask]
            seg_to_clip[seg] = clip
            mask = prev
        return seg_to_clip

    # Hungarian (Kuhn-Munkres) maximizing weight via cost = M - score
    max_score = max(max(row) for row in score_matrix) if score_matrix else 0.0
    cost = [[max_score - float(score_matrix[i][j]) + 0.002 * abs(i - j) for j in range(n)] for i in range(n)]
    u = [0.0] * (n + 1)
    v = [0.0] * (n + 1)
    p = [0] * (n + 1)
    way = [0] * (n + 1)
    for i in range(1, n + 1):
        p[0] = i
        j0 = 0
        minv = [1e100] * (n + 1)
        used = [False] * (n + 1)
        while True:
            used[j0] = True
            i0 = p[j0]
            delta = 1e100
            j1 = 0
            for j in range(1, n + 1):
                if used[j]:
                    continue
                cur = cost[i0 - 1][j - 1] - u[i0] - v[j]
                if cur < minv[j]:
                    minv[j] = cur
                    way[j] = j0
                if minv[j] < delta:
                    delta = minv[j]
                    j1 = j
            for j in range(n + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while True:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
            if j0 == 0:
                break
    # p[j] = clip+1 assigned to segment j
    seg_to_clip = [-1] * n
    for j in range(1, n + 1):
        if p[j] != 0:
            seg_to_clip[j - 1] = p[j] - 1
    for i, c in enumerate(seg_to_clip):
        if c < 0:
            seg_to_clip[i] = i
    return seg_to_clip


def match_clips_to_script(clips, transcripts, script_text, minimum_score=0.55):
    """One-to-one match clips to script segments; return clips in script-segment order.

    Returns (ordered_clips_or_None, reason, details).
    Each details item includes ``segment_text`` so trim can use the *matched*
    script line instead of re-splitting by index (which caused chaos when split
    disagreed with match order).

    minimum_score default 0.55：低于此的「勉强匹配」会打乱合成顺序（例如 0.25～0.45
    实际文案与识别几乎无关，必须回退文件名排序）。安装版用户反馈弱匹配仍会排错，故略抬高门槛。
    """
    segments = split_group_script(script_text, len(clips))
    clips = list(clips)
    if len(segments) != len(clips) or not clips:
        got = len(segments)
        need = len(clips)
        return (
            None,
            f"分段文案数量与视频片段数量不一致（文案 {got} 段 / 视频 {need} 段；"
            f"请用空行分隔每段，或使用连续序号 1. 2. 3.，段数须与视频数一致）",
            [],
        )
    # 匹配用去序号正文；预览仍显示原文前缀
    match_segments = [_strip_segment_prefix(seg) or seg for seg in segments]
    empty_sources = 0
    source_previews = {}
    score_matrix = []
    for clip_index, clip in enumerate(clips):
        raw = _resolve_transcript(transcripts, clip)
        variants = _transcript_variants(raw)
        row = []
        if not variants:
            empty_sources += 1
            source_previews[clip_index] = ""
            row = [0.0] * len(match_segments)
        else:
            source_previews[clip_index] = variants[0][:80]
            for segment in match_segments:
                score = max(_text_similarity(variant, segment) for variant in variants)
                row.append(float(score))
        score_matrix.append(row)
    if empty_sources == len(clips):
        return None, "片段识别文案为空，无法按分段文案匹配排序", []

    # 若某段「第二高分」与最高分非常接近，加大顺序先验，减少互换
    for seg in range(len(match_segments)):
        col = sorted(((score_matrix[c][seg], c) for c in range(len(clips))), reverse=True)
        if len(col) >= 2 and col[0][0] - col[1][0] < 0.08:
            for c in range(len(clips)):
                # 文件名自然序接近段序时微调
                score_matrix[c][seg] += 0.01 * (1.0 - abs(c - seg) / max(1, len(clips) - 1))

    seg_to_clip = _max_weight_assignment(score_matrix)
    mapping = {}
    details = []
    for seg_index, clip_index in enumerate(seg_to_clip):
        if clip_index < 0 or clip_index >= len(clips):
            continue
        score = score_matrix[clip_index][seg_index]
        clip = clips[clip_index]
        mapping[seg_index] = (clip, score, source_previews.get(clip_index, ""))
        details.append({
            "segment_index": seg_index,
            "clip": clip,
            "score": score,
            "script_preview": segments[seg_index][:60],
            "segment_text": segments[seg_index],
            "transcript_preview": source_previews.get(clip_index, ""),
        })

    if len(mapping) != len(clips) or len(set(seg_to_clip)) != len(clips):
        return None, "文案匹配未能建立一一对应", details
    scores = [item["score"] for item in details]
    min_score = min(scores)
    mean_score = sum(scores) / max(1, len(scores))
    # 短段抬高门槛（希腊语短祷文等容易互相沾边）
    shortest = min((len(_plain_text(seg)) for seg in match_segments), default=0)
    adaptive_floor = float(minimum_score)
    if shortest < 24:
        adaptive_floor = max(adaptive_floor, 0.70)
    elif shortest < 48:
        adaptive_floor = max(adaptive_floor, 0.62)
    # 任一段空识别稿不得强行配对
    if any(item["score"] <= 0.0 for item in details):
        return (
            None,
            "部分片段识别文案为空，无法按分段文案可信匹配；已拒绝重排",
            details,
        )
    # 近并列：最高分与次高分太近则拒绝（避免希腊语相邻段互换）
    ambiguous = []
    for seg in range(len(match_segments)):
        col = sorted((score_matrix[c][seg] for c in range(len(clips))), reverse=True)
        if len(col) >= 2 and (col[0] - col[1]) < (0.12 if shortest < 28 else 0.08):
            ambiguous.append(seg + 1)
    if ambiguous:
        return (
            None,
            f"文案匹配存在近并列（第 {','.join(map(str, ambiguous[:8]))} 段与其它片段区分度不足），"
            f"已拒绝重排以免顺序错乱；请改用文件名自然排序或检查分段文案",
            details,
        )
    # 单段过低 或 整体偏低：说明粘贴文案与这组视频对不上，禁止强行重排
    if min_score < adaptive_floor or mean_score < max(0.50, adaptive_floor - 0.05):
        weak = ", ".join(
            f"第{item['segment_index'] + 1}段↔{item['clip'].name}({item['score']:.2f})"
            for item in details
            if item["score"] < adaptive_floor
        ) or ", ".join(
            f"第{item['segment_index'] + 1}段↔{item['clip'].name}({item['score']:.2f})"
            for item in details
        )
        return (
            None,
            f"文案匹配可信度不足（最低 {min_score:.2f} / 平均 {mean_score:.2f}；"
            f"需单段≥{adaptive_floor:.2f}）。弱匹配：{weak}。"
            f"请检查该组粘贴文案是否对应这些视频，或改用文件名自然排序",
            details,
        )
    ordered = [mapping[index][0] for index in range(len(segments))]
    natural = list(clips)
    if [str(p) for p in ordered] != [str(p) for p in natural]:
        return (
            ordered,
            f"已按分段文案自动匹配排序（已重排，与文件名顺序不同；"
            f"最低相似度 {min_score:.2f}，平均 {mean_score:.2f}）",
            details,
        )
    return (
        ordered,
        f"已按分段文案自动匹配排序（与文件名顺序一致；"
        f"最低相似度 {min_score:.2f}，平均 {mean_score:.2f}）",
        details,
    )


def _speech_spans(srt):
    timing = re.compile(
        r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)"
    )
    spans = []
    for match in timing.finditer(str(srt or "")):
        values = [int(value) for value in match.groups()]
        start = values[0] * 3600 + values[1] * 60 + values[2] + values[3] / (10 ** len(match.group(4)))
        end = values[4] * 3600 + values[5] * 60 + values[6] + values[7] / (10 ** len(match.group(8)))
        spans.append((start, end))
    return spans


def speech_trim_bounds(srt, duration, head_padding_ms=220, tail_padding_ms=280,
                       tail_safety_ms=280):
    """ASR 词界：从首词前 padding 到末词后 padding。

    尾部额外 +tail_safety_ms：ASR 词尾时间常偏早，防止吞掉尾音/气声。
    若末词后剩余不足 ~0.8s，直接保留到片尾。
    片头 padding 默认 220ms：避免首句辅音/轻声被当成静音切掉。
    """
    spans = _speech_spans(srt)
    duration = max(0.05, float(duration or 0.05))
    if not spans:
        return 0.0, duration, False
    # ASR 首词常晚于真实开口；padding 至少 200ms，首词特别晚时宁可不裁片头
    head_pad = max(200, int(head_padding_ms or 0)) / 1000.0
    first_word = float(spans[0][0])
    if first_word > 1.2:
        # 首词过晚：多半漏识别了第一句，整段从头保留
        start = 0.0
    else:
        start = max(0.0, first_word - head_pad)
    # 用户尾保护至少 280ms；再加 safety（ASR 常切在音节中间）
    tail = max(280, int(tail_padding_ms or 0)) + max(0, int(tail_safety_ms))
    end = min(duration, spans[-1][1] + tail / 1000.0)
    # 末词结束后不远：整段留到文件尾，宁可多留气口
    if duration - spans[-1][1] <= 0.85:
        end = duration
    return start, max(start + 0.05, end), True


def _last_voice_in_probe(events, probe_len: float) -> float:
    """在 [0, probe_len] 探针窗口内，估计最后仍有声音的相对时刻。"""
    probe_len = max(0.05, float(probe_len))
    if not events:
        return probe_len  # 未检出静音 → 可能全程有声
    voice_end = 0.0
    if events[0][0] == "start" and events[0][1] > 0.05:
        voice_end = float(events[0][1])
    for i, (kind, value) in enumerate(events):
        value = float(value)
        if kind != "end":
            continue
        next_start = None
        for j in range(i + 1, len(events)):
            if events[j][0] == "start":
                next_start = float(events[j][1])
                break
        if next_start is not None:
            voice_end = max(voice_end, next_start)
        else:
            voice_end = probe_len
    if events[-1][0] == "start":
        voice_end = max(voice_end, float(events[-1][1]))
    return min(probe_len, voice_end)


def energy_extend_end(ffmpeg, clip_path, end, duration, max_extend=1.5, threshold_db=-40):
    """ASR/静音给出 end 后，探测其后是否还有说话能量，只扩展不回缩。"""
    duration = max(0.05, float(duration or 0.05))
    end = max(0.0, min(duration, float(end)))
    remain = duration - end
    if remain < 0.10:
        return duration
    probe_len = min(max_extend, remain)
    cmd = [
        str(ffmpeg), "-hide_banner", "-nostats",
        "-ss", f"{end:.3f}", "-t", f"{probe_len:.3f}",
        "-i", str(clip_path),
        "-map", "0:a:0",
        "-af", f"silencedetect=noise={int(threshold_db)}dB:d=0.05",
        "-f", "null", "-",
    ]
    try:
        result = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            timeout=25, **hidden_kwargs(),
        )
        text = result.stdout or ""
    except Exception:
        return end
    events = [
        (kind, float(value))
        for kind, value in re.findall(r"silence_(start|end):\s*([0-9.]+)", text)
    ]
    voice_rel = _last_voice_in_probe(events, probe_len)
    if voice_rel < 0.07:
        return end
    return min(duration, end + voice_rel + 0.15)


def parse_srt_with_text(srt):
    blocks = re.split(r"\r?\n\s*\r?\n", str(srt or "").strip())
    result = []
    timing_re = re.compile(r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)")
    for block in blocks:
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        timing_index = next((i for i, line in enumerate(lines) if "-->" in line), -1)
        if timing_index < 0:
            continue
        match = timing_re.match(lines[timing_index])
        if not match:
            continue
        raw_values = match.groups()
        values = [int(value) for value in raw_values]
        start = values[0] * 3600 + values[1] * 60 + values[2] + values[3] / (10 ** len(raw_values[3]))
        end = values[4] * 3600 + values[5] * 60 + values[6] + values[7] / (10 ** len(raw_values[7]))
        text = " ".join(lines[timing_index + 1:]).strip()
        if text:
            result.append((start, max(start + 0.05, end), text))
    return result


def find_matching_srt_bounds(srt, clip_script, duration, head_padding_ms=80, tail_padding_ms=120):
    """按分段文案在本片段 SRT 中找最相似连续句，返回该句时间窗。

    匹配失败或近并列时回退到「整段 ASR 首尾词」，宁多留不漏字
    （希腊语等短句最容易误切到相邻句）。
    """
    entries = parse_srt_with_text(srt)
    duration = max(0.05, float(duration or 0.05))
    if not entries:
        return 0.0, duration, False

    clean_target = _plain_text(clip_script)
    if not clean_target:
        return speech_trim_bounds(srt, duration, head_padding_ms, tail_padding_ms)

    # 检测希腊文比例：小语种短句抬高接受门槛，避免「και/Αμήν」沾边切错窗
    raw = str(clip_script or "")
    greek_chars = sum(1 for ch in raw if "\u0370" <= ch <= "\u03ff" or "\u1f00" <= ch <= "\u1fff")
    letterish = sum(1 for ch in raw if ch.isalpha())
    is_greekish = letterish > 0 and (greek_chars / max(1, letterish)) >= 0.35

    best_score = -1.0
    second_score = -1.0
    best_range = (0, len(entries) - 1)

    for i in range(len(entries)):
        for j in range(i, len(entries)):
            subset_text = " ".join(entry[2] for entry in entries[i:j + 1])
            score = _text_similarity(subset_text, clip_script)
            if score > best_score:
                second_score = best_score
                best_score = score
                best_range = (i, j)
            elif score > second_score:
                second_score = score

    if is_greekish or len(clean_target) < 28:
        accept = 0.72 if len(clean_target) < 24 else 0.66
        margin = 0.12 if len(clean_target) < 28 else 0.09
    else:
        accept = 0.65 if len(clean_target) < 28 else 0.55
        margin = 0.08 if len(clean_target) < 28 else 0.06
    if best_score >= accept and (best_score - max(0.0, second_score)) >= margin:
        i, j = best_range
        # 覆盖面过窄（只命中 1 个短 cue）且目标文案明显更长 → 不可信，回退整段
        covered = _plain_text(" ".join(entries[k][2] for k in range(i, j + 1)))
        if len(clean_target) >= 36 and len(covered) < len(clean_target) * 0.45:
            return speech_trim_bounds(srt, duration, head_padding_ms, tail_padding_ms)
        tail = max(200, int(tail_padding_ms or 0)) + 220
        start = max(0.0, entries[i][0] - max(0, head_padding_ms) / 1000.0)
        end = min(duration, entries[j][1] + tail / 1000.0)
        if duration - entries[j][1] <= 0.55:
            end = duration
        return start, max(start + 0.05, end), True

    return speech_trim_bounds(srt, duration, head_padding_ms, tail_padding_ms)


def pair_silence_events(events):
    """silencedetect 事件 → [(start, end), ...]；无 end 的尾段用 None 表示。"""
    intervals = []
    open_start = None
    for kind, value in events or []:
        if kind == "start":
            if open_start is not None:
                intervals.append((open_start, None))
            open_start = float(value)
        elif kind == "end" and open_start is not None:
            intervals.append((open_start, float(value)))
            open_start = None
    if open_start is not None:
        intervals.append((open_start, None))
    return intervals


def safe_silence_bounds(duration, events, head_padding_ms=200, tail_padding_ms=120):
    """只裁「真·片头静音」和「真·片尾静音」。

    核心原则（高于 v1.7.17 旧逻辑）：
    - 中间停顿绝不当成片尾（旧逻辑会把 last silence_start 当 end，后半句全没）。
    - 片头 silence_end 若过晚（轻声/第一句被当成静音），宁可不裁。
    - 不确定 → (0, duration, False) 保留完整片段。
    """
    duration = max(0.05, float(duration or 0.05))
    intervals = pair_silence_events(events)
    start = 0.0
    end = duration
    detected = False

    # —— 片头：只裁很短的真静音；超过 ~0.7s 易吞第一句 ——
    max_head = min(0.70, duration * 0.18)
    for s0, s1 in intervals:
        if s0 > 0.12:
            break
        if s1 is None:
            break
        if 0.02 < s1 <= max_head:
            start = s1
            detected = True
        break

    # —— 片尾：静音必须「贴片尾」：无 end 或 end 很接近 duration，且起点足够靠后 ——
    min_tail_from = max(duration * 0.50, duration - 5.0)
    for s0, s1 in reversed(intervals):
        touches_end = s1 is None or s1 >= duration - 0.08
        if not touches_end:
            continue
        if s0 < min_tail_from:
            continue
        if s0 >= duration - 0.05:
            continue
        end = s0
        detected = True
        break

    if detected:
        start = max(0.0, start - max(0, int(head_padding_ms)) / 1000.0)
        # 片尾多留：静音起点后再加 tail padding，减轻尾音被切
        end = min(duration, end + max(0, int(tail_padding_ms)) / 1000.0 + 0.12)
    if end <= start + 0.08:
        return 0.0, duration, False
    # 裁掉大半 → 不可信（轻声/阈值不当）
    if (end - start) < duration * 0.50:
        return 0.0, duration, False
    return start, end, True


def hybrid_trim_bounds(srt, duration, audio_bounds, head_padding_ms=80, tail_padding_ms=280,
                       word_guard_ms=120):
    """智能混合：ASR 词界为硬护栏；静音只收片头，片尾不因静音提前结束（防吞尾音）。

    绝不：用中间静音切掉后半句；绝不：静音越过首/末识别词。
    """
    duration = max(0.05, float(duration or 0.05))
    spans = _speech_spans(srt)
    text_start, text_end, text_detected = speech_trim_bounds(
        srt, duration, head_padding_ms, tail_padding_ms,
    )
    if not text_detected or not spans:
        if audio_bounds and len(audio_bounds) >= 3 and bool(audio_bounds[2]):
            return float(audio_bounds[0]), float(audio_bounds[1]), True
        return 0.0, duration, False
    if not audio_bounds or len(audio_bounds) < 3 or not bool(audio_bounds[2]):
        return text_start, text_end, True

    audio_start = float(audio_bounds[0])
    guard = max(0, int(word_guard_ms)) / 1000.0
    hard_latest_start = max(0.0, spans[0][0] - guard)
    # 片尾硬底：末词结束 + 护栏，且不低于文案窗 end（已含 tail padding + safety）
    hard_earliest_end = min(duration, max(text_end, spans[-1][1] + guard + 0.15))

    start = text_start
    # 片头：静音可收，但不得越过首词护栏；过晚静音（轻声）忽略
    if audio_start <= spans[0][0] + 0.35:
        start = min(hard_latest_start, max(text_start, audio_start))
        start = min(start, hard_latest_start)
    # 片尾：不使用 audio_end 缩短！静音收尾极易吞掉尾音/气声
    end = max(text_end, hard_earliest_end)
    end = min(duration, end)

    start = max(0.0, min(start, hard_latest_start))
    if end <= start + 0.08:
        return text_start, text_end, True
    return start, end, True


def refine_text_window_with_audio(text_start, text_end, text_ok, audio_bounds, duration,
                                  edge_slack_ms=150):
    """分段文案窗 + 静音：只允许收片头；片尾保持文案窗（防吞尾）。"""
    duration = max(0.05, float(duration or 0.05))
    if not text_ok:
        if audio_bounds and len(audio_bounds) >= 3 and bool(audio_bounds[2]):
            return float(audio_bounds[0]), float(audio_bounds[1]), True
        return 0.0, duration, False
    text_start = max(0.0, float(text_start))
    text_end = max(text_start + 0.05, min(duration, float(text_end)))
    if not audio_bounds or len(audio_bounds) < 3 or not bool(audio_bounds[2]):
        return text_start, text_end, True
    audio_start = float(audio_bounds[0])
    slack = max(0, int(edge_slack_ms)) / 1000.0
    latest_safe_start = min(duration, text_start + slack)
    start = text_start
    if audio_start <= text_start + slack + 0.25:
        start = min(latest_safe_start, max(text_start, audio_start))
    # 尾部不收：保持 text_end（已含尾保护）
    end = text_end
    if end <= start + 0.08:
        return text_start, text_end, True
    return max(0.0, start), min(duration, end), True


def resolve_trim_bounds(trim_mode, srt, clip_script, audio_bounds, hybrid_bounds,
                        media_duration, head_ms, tail_ms):
    """统一三种模式的最终起止点。返回 (start, end, detected, reason)。

    优先级：宁多留气口，不丢字。
    """
    media_duration = max(0.05, float(media_duration or 0.05))
    head_ms = int(head_ms or 0)
    tail_ms = int(tail_ms or 0)
    srt = str(srt or "")
    clip_script = str(clip_script or "").strip()

    if clip_script and srt:
        text_start, text_end, text_ok = find_matching_srt_bounds(
            srt, clip_script, media_duration, head_ms, tail_ms,
        )
    elif srt:
        text_start, text_end, text_ok = speech_trim_bounds(
            srt, media_duration, head_ms, tail_ms,
        )
    else:
        text_start, text_end, text_ok = 0.0, media_duration, False

    mode = str(trim_mode or "hybrid").lower()
    if mode == "fast":
        if audio_bounds and len(audio_bounds) >= 3 and bool(audio_bounds[2]):
            return float(audio_bounds[0]), float(audio_bounds[1]), True, "快速声音边界"
        if text_ok:
            return text_start, text_end, True, "快速模式无静音边界→文案时间轴"
        return 0.0, media_duration, False, "快速模式无可用边界→完整片段"

    if mode == "text":
        if text_ok:
            return text_start, text_end, True, "文案边界"
        if audio_bounds and len(audio_bounds) >= 3 and bool(audio_bounds[2]):
            return float(audio_bounds[0]), float(audio_bounds[1]), True, "文案不可用→声音边界"
        return 0.0, media_duration, False, "文案边界失败→完整片段"

    # hybrid（默认）
    if clip_script and text_ok:
        s, e, ok = refine_text_window_with_audio(
            text_start, text_end, text_ok, audio_bounds, media_duration,
        )
        return s, e, ok, "智能混合（分段文案+声音）"
    if text_ok:
        s, e, ok = hybrid_trim_bounds(
            srt, media_duration, audio_bounds, head_ms, tail_ms,
        )
        return s, e, ok, "智能混合（ASR词界+声音）"
    if hybrid_bounds and len(hybrid_bounds) >= 3 and bool(hybrid_bounds[2]):
        return (
            float(hybrid_bounds[0]), float(hybrid_bounds[1]), True,
            "智能混合（缓存hybrid）",
        )
    if audio_bounds and len(audio_bounds) >= 3 and bool(audio_bounds[2]):
        return float(audio_bounds[0]), float(audio_bounds[1]), True, "智能混合无ASR→声音边界"
    return 0.0, media_duration, False, "智能混合无边界→完整片段"


def _safe_name(value):
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", str(value)).strip(" .")
    return cleaned or "合成视频"


def group_segments_sidecar_path(output_path) -> Path:
    """Sidecar next to a group-merge product: stem.segments.json"""
    path = Path(output_path)
    return path.with_name(path.stem + ".segments.json")


def write_group_segments_map(output_path, segments, transition_ms=0):
    """Write per-segment timing so the timeline can show split video/audio bars.

    segments: list of dicts with keys name, duration_ms, optional original/normalized paths.
    """
    output_path = Path(output_path)
    rows = []
    for index, item in enumerate(segments or [], 1):
        if not isinstance(item, dict):
            continue
        rows.append({
            "index": index,
            "name": str(item.get("name") or f"段{index:02d}"),
            "duration_ms": max(80, int(item.get("duration_ms") or 0)),
            "original": str(item.get("original") or ""),
            "normalized": str(item.get("normalized") or ""),
        })
    if not rows:
        return None
    payload = {
        "version": 1,
        "output": output_path.name,
        "transition_ms": max(0, int(transition_ms or 0)),
        "segments": rows,
    }
    sidecar = group_segments_sidecar_path(output_path)
    sidecar.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return sidecar


def load_group_segments_map(output_path):
    """Load segment map if present; return dict or None."""
    sidecar = group_segments_sidecar_path(output_path)
    if not sidecar.is_file():
        return None
    try:
        data = json.loads(sidecar.read_text(encoding="utf-8"))
    except Exception:
        return None
    segments = data.get("segments") or []
    if len(segments) < 2:
        return None
    return data


def _ffprobe_candidates(ffmpeg=None):
    """Yield possible ffprobe executables (bundled + PATH)."""
    seen = set()
    candidates = []
    if ffmpeg:
        fp = Path(ffmpeg)
        candidates.append(fp.with_name("ffprobe" + fp.suffix))
        candidates.append(fp.with_name("ffprobe.exe"))
    # Project-relative common locations (dev + packaged)
    here = Path(__file__).resolve()
    roots = [here.parents[1], Path.cwd()]
    for root in roots:
        candidates.extend([
            root / ".build_media" / "ffprobe.exe",
            root / "internal" / "ffprobe.exe",
            root / "dist_folder" / "VideoToolkit" / "internal" / "ffprobe.exe",
        ])
    candidates.extend([Path("ffprobe"), Path("ffprobe.exe")])
    for item in candidates:
        key = str(item)
        if key in seen:
            continue
        seen.add(key)
        if item.name in {"ffprobe", "ffprobe.exe"} or item.is_file():
            yield str(item)


def _probe_media_duration_ms(path, ffmpeg=None) -> int:
    """Best-effort duration in ms via ffprobe; 0 on failure."""
    path = Path(path)
    if not path.is_file():
        return 0
    creation = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    for tool in _ffprobe_candidates(ffmpeg):
        try:
            res = subprocess.run(
                [tool, "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                creationflags=creation, timeout=30,
            )
            text = (res.stdout or b"").decode("utf-8", errors="replace").strip().splitlines()
            text = (text[0] if text else "").strip()
            if text and text.upper() != "N/A":
                return max(0, int(round(float(text) * 1000)))
        except Exception:
            continue
    # Fallback: ffmpeg -i header
    ffmpeg_tools = []
    if ffmpeg:
        ffmpeg_tools.append(str(ffmpeg))
    ffmpeg_tools.extend(["ffmpeg", "ffmpeg.exe"])
    for tool in ffmpeg_tools:
        try:
            res = subprocess.run(
                [tool, "-hide_banner", "-i", str(path)],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                creationflags=creation, timeout=30,
            )
            text = (res.stdout or b"").decode("utf-8", errors="replace")
            m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
            if m:
                h, mi, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
                return max(0, int(round((h * 3600 + mi * 60 + s) * 1000)))
        except Exception:
            continue
    return 0


def _parse_concat_segment_paths(concat_file: Path):
    """Parse FFmpeg concat demuxer list → absolute Paths that still exist."""
    if not concat_file.is_file():
        return []
    rows = []
    try:
        text = concat_file.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []
    for line in text.splitlines():
        line = line.strip()
        if not line.lower().startswith("file "):
            continue
        raw = line[5:].strip()
        if raw.startswith("'") and raw.endswith("'") and len(raw) >= 2:
            raw = raw[1:-1].replace("'\\''", "'")
        elif raw.startswith('"') and raw.endswith('"') and len(raw) >= 2:
            raw = raw[1:-1]
        candidate = Path(raw)
        if candidate.is_file():
            rows.append(candidate)
    return rows


def _original_names_from_analysis(analysis_file: Path, count: int):
    """Return up to count original clip basenames from analysis.json (insertion order)."""
    if not analysis_file.is_file() or count <= 0:
        return []
    try:
        data = json.loads(analysis_file.read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(data, dict):
        return []
    names = []
    for key in data.keys():
        name = Path(str(key)).name
        if name:
            names.append(name)
        if len(names) >= count:
            break
    return names


def try_rebuild_segments_sidecar(output_path, ffmpeg=None):
    """Rebuild stem.segments.json from .group_merge_cache when missing.

    Used for products merged before segmented timeline support. Returns sidecar
    Path on success, else None.
    """
    output_path = Path(output_path)
    if not output_path.is_file():
        return None
    existing = load_group_segments_map(output_path)
    if existing:
        return group_segments_sidecar_path(output_path)

    cache_root = output_path.parent / ".group_merge_cache"
    if not cache_root.is_dir():
        return None

    # Stem like "12_去口气音合成" → group token "12"
    stem = output_path.stem
    group_token = stem
    for suffix in ("_去口气音合成", "去口气音合成", "_合成"):
        if group_token.endswith(suffix):
            group_token = group_token[: -len(suffix)]
            break
    group_token = group_token.strip("_- ")

    out_dur = _probe_media_duration_ms(output_path, ffmpeg=ffmpeg)
    best = None  # (score, rows, transition_ms)  lower score is better
    for cache_dir in sorted(cache_root.iterdir()):
        if not cache_dir.is_dir():
            continue
        final_file = cache_dir / "final.json"
        final_data = {}
        try:
            if final_file.is_file():
                final_data = json.loads(final_file.read_text(encoding="utf-8")) or {}
        except Exception:
            final_data = {}
        # Prefer exact output name match written by newer merges
        name_hit = (
            str(final_data.get("output_name") or final_data.get("output") or "")
            == output_path.name
            or str(final_data.get("group_name") or "") == group_token
        )
        segment_paths = _parse_concat_segment_paths(cache_dir / "concat.txt")
        if len(segment_paths) < 2:
            # Fall back to newest segment_NNN_* files by index
            by_index = {}
            for child in cache_dir.glob("segment_*.mp4"):
                m = re.match(r"segment_(\d+)_", child.name)
                if not m:
                    continue
                idx = int(m.group(1))
                prev = by_index.get(idx)
                if prev is None or child.stat().st_mtime >= prev.stat().st_mtime:
                    by_index[idx] = child
            segment_paths = [by_index[i] for i in sorted(by_index)]
        if len(segment_paths) < 2:
            continue

        names = _original_names_from_analysis(cache_dir / "analysis.json", len(segment_paths))
        # Strong match: originals look like 12-1.mp4 / 12_1.mp4 for product 12_去口气音合成
        prefix_hit = False
        if group_token and names:
            hits = 0
            for n in names:
                stem_n = Path(n).stem
                if (
                    stem_n == group_token
                    or stem_n.startswith(f"{group_token}-")
                    or stem_n.startswith(f"{group_token}_")
                ):
                    hits += 1
            prefix_hit = hits >= max(2, (len(names) + 1) // 2)

        durs = [_probe_media_duration_ms(p, ffmpeg=ffmpeg) for p in segment_paths]
        known = [d for d in durs if d > 0]
        if len(known) < 2 and not (name_hit or prefix_hit):
            continue
        if known:
            avg = int(round(sum(known) / len(known)))
        elif out_dur > 0:
            avg = max(80, int(round(out_dur / len(segment_paths))))
        else:
            avg = 2000
        durs = [d if d > 0 else avg for d in durs]
        total = sum(durs) or 1
        rows = []
        for i, (seg_path, dur) in enumerate(zip(segment_paths, durs)):
            rows.append({
                "name": names[i] if i < len(names) else seg_path.name,
                "duration_ms": max(80, dur),
                "original": "",
                "normalized": str(seg_path.resolve()),
            })
        transition_ms = int(final_data.get("transition_ms") or 0)
        if name_hit:
            best = (0.0, rows, transition_ms)
            break
        if prefix_hit:
            score = 0.01
            if out_dur > 0:
                score = min(0.05, abs(total - out_dur) / max(out_dur, 1))
            if best is None or score < best[0]:
                best = (score, rows, transition_ms)
            continue
        if out_dur > 0:
            score = abs(total - out_dur) / max(out_dur, 1)
            if best is None or score < best[0]:
                best = (score, rows, transition_ms)

    if not best:
        return None
    score, rows, transition_ms = best
    # Reject very poor duration matches (likely wrong cache group)
    if score > 0.22:
        return None
    return write_group_segments_map(output_path, rows, transition_ms=transition_ms)


def build_segmented_edit_state(output_path, duration_ms, original_audio_enabled=True):
    """Build timeline edit_state with one bar per merged segment (video + original_audio).

    Source ranges map into the **concatenated output file** (not originals), so edge
    drag adjusts which part of the finished file is used. Segment names help find
    the bad take; re-merge with milder trim if content was permanently cut by 去口气.

    Final export is still one continuous file; segments are timeline edit aids only.
    """
    data = load_group_segments_map(output_path)
    if not data:
        try:
            try_rebuild_segments_sidecar(output_path)
        except Exception:
            pass
        data = load_group_segments_map(output_path)
    if not data:
        return None
    segments = data.get("segments") or []
    if len(segments) < 2:
        return None
    duration_ms = max(1000, int(duration_ms or 0))
    raw = [max(80, int(s.get("duration_ms") or 0)) for s in segments]
    total_raw = sum(raw) or 1
    # Fit to real file duration (hard-cut ≈ 1.0; xfade slightly shorter)
    scale = duration_ms / total_raw
    t = 0
    video_tracks = []
    audio_tracks = []
    for i, seg in enumerate(segments):
        if i < len(segments) - 1:
            dur = max(80, int(round(raw[i] * scale)))
        else:
            dur = max(80, duration_ms - t)
        name = str(seg.get("name") or f"段{i + 1:02d}")
        label = f"{i + 1:02d}.{name}"
        video_tracks.append({
            "start": t,
            "end": t + dur,
            "source_start": t,
            "source_end": t + dur,
            "source_duration": duration_ms,
            "name": label,
        })
        audio_tracks.append({
            "start": t,
            "end": t + dur,
            "source_start": t,
            "source_end": t + dur,
            "source_duration": duration_ms,
            "name": f"{label}·音",
        })
        t += dur
    return {
        "duration_ms": duration_ms,
        "original_audio_enabled": bool(original_audio_enabled),
        "segmented": True,
        "tracks": {
            "video": video_tracks,
            "original_audio": audio_tracks,
            "bgm": [],
            "tts": [],
        },
    }


class GroupMergeWorker(QObject):
    log = Signal(str)
    progress = Signal(int)
    item_done = Signal(str, str, int, int)
    finished = Signal(bool, str)

    def __init__(self, groups, output, ffmpeg, transcribe, settings):
        super().__init__()
        self.groups = [(Path(folder), [Path(p) for p in clips]) for folder, clips in groups]
        self.output = Path(output)
        self.ffmpeg = str(ffmpeg)
        self.transcribe = transcribe
        self.settings = dict(settings)
        self.cancelled = False
        import threading
        self._active_processes = set()
        self._lock = threading.Lock()
        # 本地 Whisper 模型非线程安全；API 也可串行化避免打爆限流。
        # 分析阶段并行时：多路静音检测可重叠，ASR 经此锁排队。
        self._asr_lock = threading.Lock()
        self._cache_lock = threading.Lock()
        # Windows MF 编码器多路并行易 “Could not open encoder before EOF”
        self._mf_encode_lock = threading.Lock()
        self.encoder = resolve_encoder(self.ffmpeg, self.settings.get("encoder_backend", "auto"))

    def cancel(self):
        self.cancelled = True
        with self._lock:
            for process in list(self._active_processes):
                if process.poll() is None:
                    try:
                        process.terminate()
                    except Exception:
                        pass

    @property
    def ffprobe(self):
        candidate = Path(self.ffmpeg).with_name("ffprobe" + Path(self.ffmpeg).suffix)
        return str(candidate if candidate.exists() else "ffprobe")

    def _run(self, command, timeout=None, heartbeat_label=None, heartbeat_sec=8.0):
        """Run FFmpeg/ffprobe. timeout=None means no hard limit (still cancellable).
        heartbeat_label: emit a log line every heartbeat_sec while still running
        so the UI does not look frozen on long QSV/filter encodes.
        """
        if self.cancelled:
            raise RuntimeError("分组合成已停止；已经处理的片段会保留，下一次可断点续接。")
        process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            encoding="utf-8", errors="replace", **hidden_kwargs(),
        )
        with self._lock:
            self._active_processes.add(process)
        started = time.monotonic()
        last_beat = started
        stdout = stderr = ""
        try:
            while True:
                try:
                    stdout, stderr = process.communicate(timeout=0.15)
                    break
                except subprocess.TimeoutExpired:
                    now = time.monotonic()
                    if timeout is not None and (now - started) >= float(timeout):
                        try:
                            process.kill()
                            stdout, stderr = process.communicate(timeout=2.0)
                        except Exception:
                            try:
                                process.kill()
                            except Exception:
                                pass
                            stdout, stderr = "", ""
                        raise RuntimeError(
                            f"FFmpeg 超时（>{float(timeout):.0f}s）"
                            + (f"：{heartbeat_label}" if heartbeat_label else "")
                            + "。常见原因：Intel QSV 多路并行卡死，或滤镜图未结束。"
                            "请停止后重试（已改为硬件编码串行）。"
                        )
                    if heartbeat_label and (now - last_beat) >= float(heartbeat_sec):
                        last_beat = now
                        try:
                            self.log.emit(
                                f"编码进行中：{heartbeat_label}（已 {now - started:.0f}s，请稍候…）"
                            )
                        except Exception:
                            pass
                    if not self.cancelled:
                        continue
                    try:
                        process.terminate()
                        stdout, stderr = process.communicate(timeout=1.5)
                    except Exception:
                        process.kill()
                        stdout, stderr = process.communicate()
                    raise RuntimeError("分组合成已停止；已经处理的片段会保留，下一次可断点续接。")
        finally:
            with self._lock:
                self._active_processes.discard(process)
        if self.cancelled:
            raise RuntimeError("分组合成已停止；已经处理的片段会保留，下一次可断点续接。")
        if process.returncode:
            raise RuntimeError((stderr or "")[-1200:].strip() or "FFmpeg 处理失败")
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)

    def _write_final_output(self, command, destination: Path):
        """Write via a temp file then atomically replace, so a locked/in-use final
        path (preview player still open from a previous run) cannot leave a
        half-written mp4 that Qt later rejects as “Invalid data…”."""
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(destination.stem + f".tmp_{os.getpid()}.mp4")
        try:
            if temporary.exists():
                temporary.unlink()
        except OSError:
            pass
        # Point FFmpeg output at the temp path (last arg is always destination)
        if not command:
            raise RuntimeError("内部错误：空的合成命令")
        command = list(command[:-1]) + [str(temporary)]
        try:
            self._run(command)
            if not temporary.is_file() or temporary.stat().st_size < 1024:
                raise RuntimeError(f"合成输出无效或过小：{temporary.name}")
            # Replace may fail if the destination is still open; retry briefly.
            last_error = None
            for attempt in range(8):
                try:
                    os.replace(str(temporary), str(destination))
                    last_error = None
                    break
                except OSError as exc:
                    last_error = exc
                    time.sleep(0.12 * (attempt + 1))
            if last_error is not None:
                # Fall back to a unique name rather than leave a broken final file
                alt = destination.with_name(
                    f"{destination.stem}_{int(time.time())}{destination.suffix}"
                )
                try:
                    os.replace(str(temporary), str(alt))
                    self.log.emit(
                        f"提醒：原成品文件被占用无法覆盖，已改存为 {alt.name}。"
                        "请先停止预览后再合成，以便覆盖同名文件。"
                    )
                    return alt
                except OSError:
                    raise RuntimeError(
                        f"无法写入成品（文件可能被播放器占用）：{destination.name}\n"
                        f"请先停止预览/关闭占用该文件的程序后重试。\n{last_error}"
                    ) from last_error
            return destination
        finally:
            try:
                if temporary.exists():
                    temporary.unlink()
            except OSError:
                pass

    def _probe(self, path):
        result = self._run([
            self.ffprobe, "-v", "error", "-show_entries",
            "format=duration:stream=codec_type,width,height", "-of", "json", str(path),
        ])
        data = json.loads(result.stdout or "{}")
        video = next((stream for stream in data.get("streams", []) if stream.get("codec_type") == "video"), {})
        return {
            "duration": float(data.get("format", {}).get("duration") or 0),
            "width": int(video.get("width") or 1080),
            "height": int(video.get("height") or 1920),
            "audio": any(stream.get("codec_type") == "audio" for stream in data.get("streams", [])),
        }

    @staticmethod
    def _signature(path):
        stat = Path(path).stat()
        return {"path": str(Path(path).resolve()), "size": stat.st_size, "mtime": stat.st_mtime_ns}

    def _analysis(self, clip, cache):
        signature = self._signature(clip)
        key = str(Path(clip).resolve())
        with self._cache_lock:
            saved = dict(cache.get(key, {}) or {})
        if (self.settings.get("resume", True) and saved.get("signature") == signature
                and str(saved.get("srt") or "").strip()):
            self.log.emit(f"续接：复用语音边界 {clip.name}")
            return saved
        self.log.emit(
            f"正在识别说话边界：{clip.name}"
            f"（智能混合需语音识别，可能较慢；已勾选「不转文案」时会改用快速声音边界）"
        )
        # ASR 串行：本地 Whisper 同模型不可并发；API 亦避免瞬间打满配额
        import threading
        stop_beat = threading.Event()
        started = time.monotonic()
        original, srt = "", ""

        def _asr_heartbeat():
            while not stop_beat.wait(12.0):
                if self.cancelled:
                    return
                try:
                    self.log.emit(
                        f"说话边界识别进行中：{clip.name}"
                        f"（已 {time.monotonic() - started:.0f}s，请稍候…）"
                    )
                except Exception:
                    return

        beat = threading.Thread(target=_asr_heartbeat, name="asr-beat", daemon=True)
        beat.start()
        try:
            with self._asr_lock:
                if self.cancelled:
                    raise RuntimeError("分组合成已停止；已经处理的片段会保留，下一次可断点续接。")
                original, _translated, srt = self.transcribe(str(clip))
        finally:
            stop_beat.set()
        if not str(srt or "").strip():
            raise RuntimeError("没有识别到带时间轴的有效文案")
        with self._cache_lock:
            saved = dict(cache.get(key, {}) or {})
            info = {**saved, "signature": signature, "original": str(original or ""), "srt": str(srt or "")}
            cache[key] = info
        self.log.emit(
            f"说话边界识别完成：{clip.name}"
            f"（耗时 {time.monotonic() - started:.1f}s）"
        )
        return info

    def _silence_events_head_tail(self, clip, duration, threshold, minimum):
        """只解码片头/片尾做 silencedetect，避免整段 8s×N 全文件扫（智能混合时显著加速）。"""
        duration = max(0.05, float(duration or 0.05))
        threshold = int(threshold)
        minimum = max(0.06, float(minimum))
        af = f"silencedetect=noise={threshold}dB:d={minimum:.3f}"
        events = []

        def collect(stderr, offset=0.0):
            for kind, value in re.findall(r"silence_(start|end):\s*([0-9.]+)", stderr or ""):
                events.append((kind, float(value) + float(offset)))

        # 短片：一次全扫更省事
        if duration <= 6.5:
            result = self._run([
                self.ffmpeg, "-hide_banner", "-nostats", "-i", str(clip),
                "-map", "0:a:0", "-af", af, "-f", "null", "-",
            ])
            collect(result.stderr, 0.0)
            return events

        head_win = min(3.2, max(1.2, duration * 0.42))
        tail_win = min(5.5, max(1.5, duration * 0.55))
        # 片头窗口
        result = self._run([
            self.ffmpeg, "-hide_banner", "-nostats",
            "-t", f"{head_win:.3f}", "-i", str(clip),
            "-map", "0:a:0", "-af", af, "-f", "null", "-",
        ])
        collect(result.stderr, 0.0)
        # 片尾窗口（-ss 在 -i 前：时间戳从 0 起，需加 offset）
        tail_ss = max(0.0, duration - tail_win)
        if tail_ss > head_win * 0.85:
            result = self._run([
                self.ffmpeg, "-hide_banner", "-nostats",
                "-ss", f"{tail_ss:.3f}", "-i", str(clip),
                "-map", "0:a:0", "-af", af, "-f", "null", "-",
            ])
            collect(result.stderr, tail_ss)
        return events

    def _fast_analysis(self, clip, cache):
        """本地片头/片尾静音（安全版）：中间停顿绝不裁切。"""
        signature = self._signature(clip)
        key = str(Path(clip).resolve())
        threshold = int(self.settings.get("silence_threshold_db", -35))
        minimum = max(0.06, float(self.settings.get("silence_min_ms", 180)) / 1000.0)
        params = {"threshold": threshold, "minimum": round(minimum, 3),
                  "head": int(self.settings.get("head_padding_ms", 80)),
                  "tail": int(self.settings.get("tail_padding_ms", 120)),
                  "scan": "head_tail_v5"}
        with self._cache_lock:
            saved = dict(cache.get(key, {}) or {})
        # v5 = 首尾窗口 silencedetect + safe_silence_bounds；旧 v4 全片扫仍可续接
        if (self.settings.get("resume", True) and saved.get("signature") == signature
                and saved.get("fast_bounds_version") in (4, 5)
                and saved.get("fast_params") == params
                and saved.get("bounds")):
            self.log.emit(f"续接：复用本地声音边界 {clip.name}")
            return saved
        # 兼容旧缓存：同签名且已有 bounds，参数仅 scan 字段不同时仍可复用
        if (self.settings.get("resume", True) and saved.get("signature") == signature
                and saved.get("fast_bounds_version") in (4, 5) and saved.get("bounds")
                and isinstance(saved.get("fast_params"), dict)
                and saved["fast_params"].get("threshold") == params["threshold"]
                and saved["fast_params"].get("minimum") == params["minimum"]
                and saved["fast_params"].get("head") == params["head"]
                and saved["fast_params"].get("tail") == params["tail"]):
            self.log.emit(f"续接：复用本地声音边界 {clip.name}")
            return saved
        probe = self._probe(clip)
        duration = max(0.05, probe["duration"])
        if not probe["audio"]:
            info = {**saved, "signature": signature, "fast_bounds_version": 5,
                    "fast_params": params, "duration": duration, "bounds": [0.0, duration, False]}
            with self._cache_lock:
                prev = dict(cache.get(key, {}) or {})
                info = {**prev, **info}
                cache[key] = info
            return info
        self.log.emit(f"快速检测首尾声音：{clip.name}（仅片头/片尾，保护说话内容）")
        events = self._silence_events_head_tail(clip, duration, threshold, minimum)
        start, end, detected = safe_silence_bounds(
            duration, events,
            self.settings.get("head_padding_ms", 80),
            self.settings.get("tail_padding_ms", 120),
        )
        with self._cache_lock:
            prev = dict(cache.get(key, {}) or {})
            info = {**prev, "signature": signature, "fast_bounds_version": 5,
                    "fast_params": params, "duration": duration, "bounds": [start, end, detected]}
            cache[key] = info
        return info

    def _persist_analysis_cache(self, cache_file, analysis_cache):
        try:
            with self._cache_lock:
                payload = json.dumps(analysis_cache, ensure_ascii=False, indent=2)
            cache_file.write_text(payload, encoding="utf-8")
        except Exception:
            pass

    def _analyze_clip(self, clip, analysis_cache, *, need_asr, need_fast, trim_mode, script_mode):
        """单片段分析（可并行）：先静音后 ASR，便于多路静音与一路 ASR 重叠。"""
        if self.cancelled:
            raise RuntimeError("分组合成已停止；已经处理的片段会保留，下一次可断点续接。")
        key = str(clip.resolve())
        if trim_mode == "none" and not script_mode:
            return key, {}
        if not need_asr and not need_fast:
            return key, {}
        analysis = {}
        try:
            # 静音检测无 GPU/模型，优先跑完，再进 ASR 锁 —— 并行收益最大
            if need_fast:
                analysis = self._fast_analysis(clip, analysis_cache)
            if need_asr:
                analysis = self._analysis(clip, analysis_cache)
            if trim_mode == "hybrid":
                media_duration = float(analysis.get("duration") or self._probe(clip)["duration"])
                analysis["hybrid_bounds"] = list(hybrid_trim_bounds(
                    analysis.get("srt", ""), media_duration, analysis.get("bounds"),
                    self.settings.get("head_padding_ms", 80),
                    self.settings.get("tail_padding_ms", 120),
                ))
                with self._cache_lock:
                    prev = dict(analysis_cache.get(key, {}) or {})
                    analysis = {**prev, **analysis}
                    analysis_cache[key] = analysis
                hb = analysis["hybrid_bounds"]
                self.log.emit(
                    f"智能混合边界：{clip.name} → "
                    f"{float(hb[0]):.2f}s–{float(hb[1]):.2f}s"
                    f"{'' if hb[2] else '（回退完整）'}"
                )
            elif trim_mode == "fast":
                self.log.emit(f"快速声音边界：已完成本地首尾检测 {clip.name}")
            elif trim_mode == "none" and script_mode:
                self.log.emit(f"文案识别完成（不裁剪，仅用于按分段文案排序）：{clip.name}")
            elif need_asr:
                self.log.emit(f"智能文案边界：已按首词/末词时间定位 {clip.name}")
            return key, analysis
        except Exception as exc:
            if script_mode and need_asr:
                raise
            if need_asr:
                self.log.emit(
                    f"智能文案边界识别失败，自动改用本地声音边界继续处理：{clip.name}（{exc}）"
                )
                return key, self._fast_analysis(clip, analysis_cache)
            raise

    def _normalize(
        self, clip, index, total_count, cache_dir, analysis, target_w, target_h,
        watermark=None, group_script="", clip_scripts=None,
    ):
        self.log.emit(
            f"[{index + 1}/{total_count}] 开始裁剪/编码片段：{clip.name}"
            f"（编码器 {ENCODER_LABELS.get(self.encoder, self.encoder)}）"
        )
        probe = self._probe(clip)
        # Prefer the group-level script (virtual groups like 2-1/2-2 live under parent dir,
        # so clip.parent is NOT the group key used when saving 分段文案).
        if not str(group_script or "").strip():
            group_script = lookup_group_script(self.settings.get("scripts", {}), clip.parent)
        # 优先用匹配结果带来的「第 N 段原文」，避免再次 split 与排序不一致
        if isinstance(clip_scripts, (list, tuple)) and index < len(clip_scripts):
            clip_script = str(clip_scripts[index] or "")
        else:
            segments = split_group_script(group_script, total_count)
            clip_script = segments[index] if index < len(segments) and len(segments) == total_count else ""
            if group_script and len(segments) != total_count:
                self.log.emit(
                    f"提醒：{clip.name} 分段文案 {len(segments)} 段 ≠ 视频 {total_count} 段，"
                    f"本段裁剪不按文案窗（请用空行或 1.2.3. 保证段数一致）。"
                )
        
        manual_bounds = None
        if clip_script:
            match = re.search(r'\[\s*(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s*\]', clip_script)
            if match:
                val1, val2 = float(match.group(1)), float(match.group(2))
                if val2 <= probe["duration"]:
                    manual_bounds = (val1, val2)
                clip_script = re.sub(r'\[\s*\d+(?:\.\d+)?\s*-\s*\d+(?:\.\d+)?\s*\]', '', clip_script).strip()

        trim_mode = self.settings.get("trim_mode", "hybrid")
        head_ms = int(self.settings.get("head_padding_ms", 100) or 100)
        tail_ms = int(self.settings.get("tail_padding_ms", 280) or 280)
        srt = str(analysis.get("srt") or "")
        media_duration = probe["duration"]

        reason = ""
        if manual_bounds is not None:
            start = max(0.0, min(media_duration, manual_bounds[0]))
            end = max(start + 0.05, min(media_duration, manual_bounds[1]))
            detected = True
            reason = "手动切片"
            self.log.emit(f"切片功能：{clip.name} 已应用手动切片区间 {start:.2f}s - {end:.2f}s")
        elif trim_mode == "none":
            start, end, detected = 0.0, media_duration, True
            reason = "不裁剪"
            self.log.emit(f"不裁剪：{clip.name} 保留完整片段。")
        else:
            start, end, detected, reason = resolve_trim_bounds(
                trim_mode, srt, clip_script,
                analysis.get("bounds"), analysis.get("hybrid_bounds"),
                media_duration, head_ms, tail_ms,
            )
        if not detected:
            self.log.emit(f"提醒：{clip.name} {reason or '未识别边界'}，保留完整片段。")
            start, end = 0.0, media_duration
        else:
            spans = _speech_spans(srt)
            if spans and trim_mode != "none":
                # 尾底线：末词 + max(尾保护, 450ms)
                floor_end = min(media_duration, spans[-1][1] + max(0.45, tail_ms / 1000.0))
                if end < floor_end:
                    end = floor_end
                    reason = f"{reason}+尾底线"
                # 末词距片尾 ≤1.2s：直接用到文件尾（如 14-5 的 preocupa 贴尾）
                if media_duration - spans[-1][1] <= 1.20:
                    if end < media_duration - 0.02:
                        end = media_duration
                        reason = f"{reason}+贴尾整段"
            # 能量续尾：ASR 结束后若仍有声，继续延长
            if trim_mode != "none" and end < media_duration - 0.08:
                try:
                    extended = energy_extend_end(
                        self.ffmpeg, clip, end, media_duration, max_extend=1.8,
                    )
                    if extended > end + 0.04:
                        self.log.emit(
                            f"能量续尾：{clip.name} {end:.2f}s → {extended:.2f}s"
                            f"（+{extended - end:.2f}s 补 ASR 未标出的尾音）"
                        )
                        end = extended
                        reason = f"{reason}+能量续尾"
                except Exception as exc:
                    self.log.emit(f"提醒：能量续尾跳过 {clip.name}：{exc}")
            # 片头：start 不得晚于「首词前 250ms」；首词很晚则强制从 0 起
            if spans and trim_mode != "none":
                first = float(spans[0][0])
                if first > 1.2:
                    start = 0.0
                    reason = f"{reason}+首句保护(ASR首词过晚)"
                else:
                    start = min(start, max(0.0, first - 0.25))
            # 硬顶：自动裁剪片头最多 0.65s，防止静音检测吞掉第一句
            if trim_mode != "none" and start > 0.65 and reason != "手动切片":
                self.log.emit(
                    f"片头保护：{clip.name} 自动片头 {start:.2f}s 过大，限制为 0.15s 以防吞首句"
                )
                start = min(start, 0.15)
                reason = f"{reason}+片头上限"
            self.log.emit(
                f"去口气音：{clip.name} 保留 {start:.2f}s - {end:.2f}s"
                f"（{reason}｜首保护{head_ms}ms 尾保护{tail_ms}ms）"
            )

        # 转场预留（v14 自然接缝）：
        # 旧方案在片头/片尾「垫静音」再 acrossfade → 听感是「停一下再说话」。
        # 新方案：只在源文件里还有真实余量时多取一点（多为句末静音/气口），
        # 绝不人工垫静音；合并时用短音频交叉淡化，画面可稍长叠化。
        transition_pad = 0.0
        start_silence_pad = 0.0  # 保留字段兼容指纹/日志，v14 恒为 0
        end_silence_pad = 0.0
        if total_count > 1:
            tname = str(self.settings.get("transition_name") or "无转场")
            if tname and tname != "无转场":
                try:
                    td = float(self.settings.get("transition_duration") or 0.22)
                except (TypeError, ValueError):
                    td = 0.22
                # 接缝缓冲不必等于转场全长；0.12~0.28 足够给画面叠化
                transition_pad = max(0.12, min(0.35, td if td >= 0.10 else 0.22))
                if index < total_count - 1:
                    room = media_duration - end
                    if room > 0.02:
                        # 只吃真实片尾余量，不造静音
                        end = min(media_duration, end + min(transition_pad, room))
                if index > 0:
                    room = start
                    if room > 0.02:
                        start = max(0.0, start - min(transition_pad, room))
                if transition_pad > 0:
                    self.log.emit(
                        f"转场预留：{clip.name} 段{index + 1}/{total_count} "
                        f"真实余量缓冲≤{transition_pad:.2f}s（不垫静音，避免接缝停顿）"
                    )

        # 硬性校验：无效裁切会导致 0 帧 → “Could not open encoder before EOF / nothing written”
        media_duration = max(0.08, float(media_duration or probe.get("duration") or 0.08))
        if start >= media_duration - 0.04 or end <= start + 0.04:
            self.log.emit(
                f"提醒：{clip.name} 裁切区间无效（{float(start):.2f}–{float(end):.2f}s / 片长 {media_duration:.2f}s），"
                f"改用整段，避免空帧编码失败。"
            )
            start, end = 0.0, media_duration
            reason = f"{reason or '边界'}→整段回退"
        start = max(0.0, min(float(start), media_duration - 0.08))
        end = max(start + 0.08, min(float(end), media_duration))
        duration = max(0.08, end - start)
        # H.264/yuv420p 要求偶数分辨率
        target_w = max(2, int(target_w) // 2 * 2)
        target_h = max(2, int(target_h) // 2 * 2)
        if int(probe.get("width") or 0) < 2 or int(probe.get("height") or 0) < 2:
            raise RuntimeError(
                f"{clip.name} 没有有效视频流（{probe.get('width')}×{probe.get('height')}），无法编码。"
            )
        removed = max(0.0, float(probe.get("duration") or 0) - duration)
        ratio = (removed / probe["duration"] * 100.0) if probe.get("duration") else 0.0
        self.log.emit(
            f"时长：{clip.name} 原始 {probe['duration']:.2f}s → 取源 {duration:.2f}s"
            f"，删减 {removed:.2f}s（{ratio:.1f}%）"
        )
        if ratio > 40:
            self.log.emit(f"提醒：{clip.name} 删减超过 40%，请检查文案时间轴或适当调低静音阈值。")
        watermark = Path(watermark) if watermark and Path(watermark).is_file() else None
        # version 11：片尾不够转场时垫静音/定格，避免 acrossfade 吃真词
        fingerprint = hashlib.sha256(json.dumps({
            "source": self._signature(clip), "start": round(start, 3), "end": round(end, 3),
            "width": target_w, "height": target_h,
            "watermark": self._signature(watermark) if watermark else None,
            "clean_metadata": bool(self.settings.get("clean_metadata", True)),
            "trim_mode": str(trim_mode),
            "tail_ms": int(tail_ms),
            "tpad": round(transition_pad, 3),
            "spad": round(start_silence_pad, 3),
            "epad": round(end_silence_pad, 3),
            "index": int(index),
            "version": 15,  # clamp empty trim + accurate -ss after -i
        }, sort_keys=True).encode("utf-8")).hexdigest()[:14]
        destination = cache_dir / f"segment_{index + 1:03d}_{fingerprint}.mp4"
        if self.settings.get("resume", True) and destination.exists() and destination.stat().st_size > 1024:
            self.log.emit(f"续接：复用已处理片段 {clip.name}")
            return destination
        sp = float(start_silence_pad or 0.0)
        ep = float(end_silence_pad or 0.0)
        expect_out = duration + sp + ep
        self.log.emit(
            f"正在裁剪口气音并统一音视频参数：{clip.name}"
            f"（约 {expect_out:.1f}s，编码器 {ENCODER_LABELS.get(self.encoder, self.encoder)}）"
        )
        # 画面核心：分辨率已一致时跳过 scale/crop。
        # 时间戳归零 + 关 B 帧：避免达芬奇看到 video start_time≈1 帧的片头黑帧。
        # 中间段不强行 fps=30（源多为 24/25/30，重采样贵）；最终成片字幕烧录再统一 30fps。
        same_geometry = (
            int(probe.get("width") or 0) == int(target_w)
            and int(probe.get("height") or 0) == int(target_h)
        )
        if same_geometry:
            video_core = "setsar=1,format=yuv420p,setpts=PTS-STARTPTS"
        else:
            video_core = (
                f"scale={target_w}:{target_h}:force_original_aspect_ratio=increase,"
                f"crop={target_w}:{target_h}:(iw-ow)/2:(ih-oh)/2,"
                f"setsar=1,format=yuv420p,setpts=PTS-STARTPTS"
            )
        has_pad = sp > 0.01 or ep > 0.01
        need_complex = bool(watermark) or has_pad
        # 默认 -ss 在 -i 前（关键帧快进，快很多）。start≈0 时省略 -ss。
        # 若出现 0 帧再回退到 -ss 在 -i 后 / 整段（见下方重试逻辑）。
        command = [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y"]
        if start > 0.02:
            command += ["-ss", f"{start:.3f}"]
        command += ["-t", f"{duration:.3f}", "-i", str(clip)]
        # —— 快路径：无水印、无垫片 → 简单 -vf/-af，比 filter_complex 轻很多 ——
        if not need_complex:
            command += ["-vf", video_core, "-map", "0:v:0"]
            if probe["audio"]:
                command += [
                    "-af",
                    "aresample=48000,aformat=sample_rates=48000:channel_layouts=stereo,"
                    "asetpts=PTS-STARTPTS",
                    "-map", "0:a:0",
                ]
            else:
                command += ["-an"]
            if self.settings.get("clean_metadata", True):
                command += ["-map_metadata", "-1", "-map_metadata:s", "-1",
                            "-map_metadata:p", "-1", "-map_metadata:c", "-1",
                            "-map_chapters", "-1"]
            command += ["-sn", "-dn"]
            command += encoder_args(
                self.encoder, self.settings.get("encode_preset", "veryfast"), intermediate=True,
            )
            if probe["audio"]:
                command += ["-c:a", "aac", "-b:a", "160k", "-ac", "2", "-ar", "48000"]
            command += [
                "-t", f"{expect_out:.3f}",
                "-fps_mode", "cfr",
                "-muxdelay", "0", "-muxpreload", "0",
                "-avoid_negative_ts", "make_zero",
                "-movflags", "+faststart",
                str(destination),
            ]
        else:
            # 滤镜图：源 → 水印 → 头/尾定格；输出强制 -t 防止 loop 水印挂死
            wm_idx = None
            if watermark:
                command += [
                    "-loop", "1", "-framerate", "30",
                    "-t", f"{max(0.25, expect_out):.3f}",
                    "-i", str(watermark),
                ]
                wm_idx = 1

            fc_parts = []
            if watermark and wm_idx is not None:
                fc_parts.append(
                    f"[0:v]{video_core}[base];[{wm_idx}:v]scale={target_w}:{target_h},format=rgba[wm];"
                    f"[base][wm]overlay=0:0:shortest=1,format=yuv420p,setpts=PTS-STARTPTS[vcore]"
                )
            else:
                fc_parts.append(f"[0:v]{video_core}[vcore]")
            v_label = "vcore"
            if sp > 0.01:
                fc_parts.append(f"[{v_label}]tpad=start_mode=clone:start_duration={sp:.3f}[v1]")
                v_label = "v1"
            if ep > 0.01:
                fc_parts.append(f"[{v_label}]tpad=stop_mode=clone:stop_duration={ep:.3f}[v2]")
                v_label = "v2"
            # 垫片后再次归零，保证输出从 t=0 起
            fc_parts.append(f"[{v_label}]setpts=PTS-STARTPTS[vout]")
            v_label = "vout"

            a_label = None
            if probe["audio"]:
                fc_parts.append(
                    f"[0:a]aresample=48000,aformat=sample_rates=48000:channel_layouts=stereo,"
                    f"asetpts=PTS-STARTPTS[acore]"
                )
                a_label = "acore"
                if sp > 0.01:
                    fc_parts.append(
                        f"aevalsrc=0|0:d={sp:.3f}:s=48000,"
                        f"aformat=sample_rates=48000:channel_layouts=stereo,asetpts=PTS-STARTPTS[ah];"
                        f"[ah][{a_label}]concat=n=2:v=0:a=1[a1]"
                    )
                    a_label = "a1"
                if ep > 0.01:
                    fc_parts.append(
                        f"aevalsrc=0|0:d={ep:.3f}:s=48000,"
                        f"aformat=sample_rates=48000:channel_layouts=stereo,asetpts=PTS-STARTPTS[at];"
                        f"[{a_label}][at]concat=n=2:v=0:a=1[a2]"
                    )
                    a_label = "a2"
                fc_parts.append(
                    f"[{a_label}]apad=whole_dur={expect_out:.3f},"
                    f"atrim=0:{expect_out:.3f},asetpts=PTS-STARTPTS[aout]"
                )
                a_label = "aout"

            fc = ";".join(fc_parts)
            command += ["-filter_complex", fc, "-map", f"[{v_label}]"]
            if a_label:
                command += ["-map", f"[{a_label}]"]
            else:
                command += ["-an"]
            if self.settings.get("clean_metadata", True):
                command += ["-map_metadata", "-1", "-map_metadata:s", "-1",
                            "-map_metadata:p", "-1", "-map_metadata:c", "-1",
                            "-map_chapters", "-1"]
            command += ["-sn", "-dn"]
            command += encoder_args(
                self.encoder, self.settings.get("encode_preset", "veryfast"), intermediate=True,
            )
            if a_label:
                command += ["-c:a", "aac", "-b:a", "160k", "-ac", "2", "-ar", "48000"]
            command += [
                "-t", f"{expect_out:.3f}",
                "-fps_mode", "cfr",
                "-muxdelay", "0", "-muxpreload", "0",
                "-avoid_negative_ts", "make_zero",
                "-movflags", "+faststart",
                str(destination),
            ]
        # 8s 片段正常应 <15s；给硬件/水印余量。超时即杀，避免「一直卡在这里」
        encode_timeout = max(90.0, min(600.0, expect_out * 25.0 + 45.0))
        t0 = time.monotonic()

        def _to_cpu_command(cmd):
            """Replace hardware encoder flags with libx264."""
            retry = list(cmd)
            for i, tok in enumerate(retry):
                if tok == "-c:v" and i + 1 < len(retry):
                    retry[i + 1] = "libx264"
                    break
            drop_keys = {
                "-rate_control", "-quality", "-cq", "-b:v", "-rc",
                "-look_ahead", "-tune", "-qp_i", "-qp_p", "-bf_delta_qp",
                "-global_quality", "-preset", "-crf", "-bf",
            }
            cleaned = []
            skip_next = False
            for i, tok in enumerate(retry):
                if skip_next:
                    skip_next = False
                    continue
                if tok in drop_keys:
                    skip_next = True
                    continue
                cleaned.append(tok)
                if tok == "libx264" and i > 0 and retry[i - 1] == "-c:v":
                    cleaned.extend(["-preset", "veryfast", "-crf", "23", "-bf", "0"])
            if "-pix_fmt" not in cleaned:
                cleaned = cleaned[:-1] + ["-pix_fmt", "yuv420p"] + cleaned[-1:]
            return cleaned

        def _full_clip_command(cmd):
            """去掉裁切，整段编码（空帧/越界 seek 的最后手段）。"""
            out = list(cmd)
            # 删除 -ss / -t 参数对（紧跟在 -i 后或前）
            cleaned = []
            i = 0
            while i < len(out):
                if out[i] in ("-ss", "-t") and i + 1 < len(out):
                    i += 2
                    continue
                cleaned.append(out[i])
                i += 1
            return cleaned

        def _run_encode(cmd, label):
            # Windows MF 多实例易卡死；串行拿锁后再编
            if self.encoder == "mf":
                with self._mf_encode_lock:
                    self._run(cmd, timeout=encode_timeout, heartbeat_label=label, heartbeat_sec=6.0)
            else:
                self._run(cmd, timeout=encode_timeout, heartbeat_label=label, heartbeat_sec=6.0)

        def _accurate_ss_command(cmd):
            """把 -ss 移到 -i 之后（精确裁切，略慢；仅作空帧失败后的回退）。"""
            out = list(cmd)
            ss_val = None
            cleaned = []
            i = 0
            while i < len(out):
                if out[i] == "-ss" and i + 1 < len(out):
                    ss_val = out[i + 1]
                    i += 2
                    continue
                cleaned.append(out[i])
                i += 1
            if not ss_val:
                return cleaned
            # 在 -i <path> 之后插入 -ss
            result = []
            i = 0
            while i < len(cleaned):
                result.append(cleaned[i])
                if cleaned[i] == "-i" and i + 1 < len(cleaned):
                    result.append(cleaned[i + 1])
                    result.extend(["-ss", ss_val])
                    i += 2
                    continue
                i += 1
            return result

        def _empty_output_error(err: str) -> bool:
            low = err.lower()
            return any(
                token in low
                for token in (
                    "could not open encoder",
                    "nothing was written",
                    "received no packets",
                    "error code: -22",
                    "invalid argument",
                )
            )

        try:
            _run_encode(command, clip.name)
        except Exception as exc:
            err = str(exc)
            empty = _empty_output_error(err)
            hw = self.encoder in ("mf", "nvenc", "qsv", "amf")
            try:
                if destination.exists():
                    destination.unlink()
            except OSError:
                pass
            # 1) 空帧：先试精确 -ss（-i 后），仍失败再整段
            # 2) 硬件失败：改 CPU
            if not (hw or empty):
                raise
            retry_cmd = command
            if empty and start > 0.02:
                self.log.emit(
                    f"提醒：{clip.name} 可能关键帧 seek 空帧，改用精确裁切重试…"
                )
                retry_cmd = _accurate_ss_command(command)
            if hw:
                self.log.emit(
                    f"提醒：{clip.name} 编码失败（{self.encoder}），"
                    f"{'空帧/无效参数，' if empty else ''}"
                    f"自动改用 CPU 重试本段…"
                )
                retry_cmd = _to_cpu_command(retry_cmd)
            try:
                _run_encode(retry_cmd, f"{clip.name}·重试")
            except Exception as exc2:
                err2 = str(exc2)
                if _empty_output_error(err2) or empty:
                    self.log.emit(
                        f"提醒：{clip.name} 裁切后仍无帧，改用整段 0–{media_duration:.2f}s 再试…"
                    )
                    try:
                        if destination.exists():
                            destination.unlink()
                    except OSError:
                        pass
                    full_cmd = _full_clip_command(_to_cpu_command(command))
                    full_cmd = full_cmd[:-1] + [
                        "-t", f"{media_duration:.3f}",
                        full_cmd[-1],
                    ]
                    _run_encode(full_cmd, f"{clip.name}·整段")
                    expect_out = media_duration
                else:
                    raise RuntimeError(
                        f"片段编码失败：{clip.name} — {err2}"
                    ) from exc2
        elapsed = time.monotonic() - t0
        # 校验垫片后时长
        try:
            out_dur = float(self._probe(destination)["duration"])
            if abs(out_dur - expect_out) > 0.35:
                self.log.emit(
                    f"提醒：{clip.name} 输出时长 {out_dur:.2f}s 与预期 {expect_out:.2f}s 偏差较大，请检查垫片。"
                )
            else:
                self.log.emit(
                    f"片段校验：{clip.name} 输出 {out_dur:.2f}s（含垫片，预期≈{expect_out:.2f}s，耗时 {elapsed:.1f}s）"
                )
        except Exception:
            self.log.emit(f"片段处理完成：{clip.name}（耗时 {elapsed:.1f}s）")
        else:
            self.log.emit(f"片段处理完成：{clip.name}")
        return destination

    def run(self):
        outputs = []
        try:
            self.output.mkdir(parents=True, exist_ok=True)
            self.log.emit(f"分段统一编码：{ENCODER_LABELS[self.encoder]}")
            cache_root = self.output / ".group_merge_cache"
            cache_root.mkdir(parents=True, exist_ok=True)
            total_steps = max(1, sum(len(clips) * 2 + 1 for _folder, clips in self.groups))
            completed_steps = 0
            for group_index, (folder, incoming_clips) in enumerate(self.groups, 1):
                if self.cancelled:
                    raise RuntimeError("分组合成已停止；已经处理的片段会保留，下一次可断点续接。")
                clips = sorted(incoming_clips, key=lambda p: natural_key(p.name))
                group_id = hashlib.sha256(str(folder.resolve()).encode("utf-8")).hexdigest()[:12]
                cache_dir = cache_root / group_id
                cache_dir.mkdir(parents=True, exist_ok=True)
                cache_file = cache_dir / "analysis.json"
                try:
                    analysis_cache = json.loads(cache_file.read_text(encoding="utf-8")) if cache_file.exists() else {}
                except Exception:
                    analysis_cache = {}
                analyses = {}
                self.log.emit(f"[{group_index}/{len(self.groups)}] 开始处理文件夹：{folder.name}（{len(clips)} 段）")
                script_mode = self.settings.get("sort_mode") == "script"
                trim_mode = self.settings.get("trim_mode", "hybrid")
                group_script = lookup_group_script(self.settings.get("scripts", {}), folder)
                if script_mode and not group_script:
                    self.log.emit(
                        f"提醒：未在 scripts 中找到「{folder}」的分段文案"
                        f"（已登记 {len(self.settings.get('scripts') or {})} 组键）。"
                    )
                group_manual_bounds = None
                if group_script:
                    match = re.search(r'\[\s*(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s*\]', group_script)
                    if match:
                        val1, val2 = float(match.group(1)), float(match.group(2))
                        first_probe = self._probe(clips[0])
                        if val2 > first_probe["duration"]:
                            group_manual_bounds = (val1, val2)
                # 裁剪模式需要的分析数据：
                # - none: 不需要（但文案排序仍要 ASR）
                # - text/hybrid: 需要 ASR 时间轴
                # - fast: 需要本地静音边界
                # - 文案排序: 无论裁剪模式都要 ASR（用于 match_clips_to_script）
                need_asr = script_mode or trim_mode in ("hybrid", "text")
                need_fast = trim_mode in ("hybrid", "fast")
                from concurrent.futures import ThreadPoolExecutor, as_completed
                # 静音检测可多路并行；ASR 在内部加锁串行 → 工人数略大于 1 即可重叠「静音||ASR」
                if need_fast and not need_asr:
                    analysis_workers = min(4, max(1, len(clips)))
                elif need_asr and need_fast:
                    analysis_workers = min(3, max(1, len(clips)))
                elif need_asr:
                    analysis_workers = min(2, max(1, len(clips)))
                else:
                    analysis_workers = 1
                if len(clips) > 1 and (need_asr or need_fast):
                    self.log.emit(
                        f"边界分析并行：{analysis_workers} 路"
                        f"（{'静音可重叠 + ASR 排队' if need_asr and need_fast else '静音并行' if need_fast else 'ASR'}）"
                        f" · 本组 {len(clips)} 段"
                    )
                if trim_mode == "none" and not script_mode:
                    for clip in clips:
                        analyses[str(clip.resolve())] = {}
                        completed_steps += 1
                        self.progress.emit(round(completed_steps / total_steps * 100))
                else:
                    def _job(c):
                        return self._analyze_clip(
                            c, analysis_cache,
                            need_asr=need_asr, need_fast=need_fast,
                            trim_mode=trim_mode, script_mode=script_mode,
                        )
                    with ThreadPoolExecutor(max_workers=analysis_workers) as executor:
                        futures = {executor.submit(_job, clip): clip for clip in clips}
                        for future in as_completed(futures):
                            if self.cancelled:
                                for other in futures:
                                    other.cancel()
                                raise RuntimeError("分组合成已停止；已经处理的片段会保留，下一次可断点续接。")
                            clip = futures[future]
                            try:
                                key, analysis = future.result()
                            except Exception as exc:
                                for other in futures:
                                    other.cancel()
                                raise RuntimeError(f"边界分析失败：{clip.name} — {exc}") from exc
                            analyses[key] = analysis
                            self._persist_analysis_cache(cache_file, analysis_cache)
                            completed_steps += 1
                            self.progress.emit(round(completed_steps / total_steps * 100))
                self._persist_analysis_cache(cache_file, analysis_cache)
                self.log.emit(
                    f"边界分析完成：{folder.name} 共 {len(clips)} 段，"
                    f"开始裁剪编码（进度不会停在「智能混合边界」）。"
                )
                # 与 clips 平行的分段文案：必须按「识别↔文案」匹配绑定，禁止按文件名序号硬套。
                # 旧逻辑在「文件名自然排序 + 智能/文案边界」时把第 N 行文案套到自然序第 N 个文件，
                # 另一台电脑文件名顺序不同或片段未按 1/2/3 命名时就会裁错/像「排错序」。
                clip_scripts = None
                has_script = bool(str(group_script or "").strip())
                want_script_sort = bool(script_mode and has_script)
                want_script_trim = bool(has_script and trim_mode in ("hybrid", "text"))
                if want_script_sort or want_script_trim:
                    if not has_script:
                        self.log.emit("提醒：本组未找到分段文案，自动回退为文件名自然排序。")
                    else:
                        ordered, reason, details = match_clips_to_script(
                            clips,
                            analyses,
                            group_script,
                        )
                        if ordered and details:
                            by_clip = {}
                            for item in details:
                                try:
                                    key = str(Path(item["clip"]).resolve())
                                except Exception:
                                    key = str(item.get("clip") or "")
                                by_clip[key] = str(
                                    item.get("segment_text") or item.get("script_preview") or ""
                                )
                            for item in details:
                                self.log.emit(
                                    f"  文案第{item['segment_index'] + 1}段 ↔ {item['clip'].name}"
                                    f" (相似度 {item['score']:.2f})｜文案: {item['script_preview']!s}"
                                    f"｜识别: {item['transcript_preview']!s}"
                                )
                            if want_script_sort:
                                clips = ordered
                                clip_scripts = [
                                    str(item.get("segment_text") or item.get("script_preview") or "")
                                    for item in sorted(details, key=lambda d: d["segment_index"])
                                ]
                                self.log.emit(reason)
                                order_desc = " → ".join(
                                    f"{i + 1}.{path.name}" for i, path in enumerate(clips)
                                )
                                self.log.emit(f"合成顺序（按分段文案）：{order_desc}")
                            else:
                                # 保留文件名顺序，但裁剪窗口用「该文件真正匹配到的那段文案」
                                clip_scripts = []
                                for c in clips:
                                    try:
                                        clip_scripts.append(by_clip.get(str(c.resolve()), ""))
                                    except Exception:
                                        clip_scripts.append("")
                                self.log.emit(
                                    f"排序=文件名自然序（不重排）；已按识别把分段文案对齐到各片段，"
                                    f"供「{trim_mode}」裁剪使用。{reason}"
                                )
                                matched_order = [
                                    str(Path(item["clip"]).resolve())
                                    for item in sorted(details, key=lambda d: d["segment_index"])
                                ]
                                natural_order = []
                                for c in clips:
                                    try:
                                        natural_order.append(str(c.resolve()))
                                    except Exception:
                                        natural_order.append(str(c))
                                if matched_order != natural_order:
                                    self.log.emit(
                                        "提醒：按文案的最佳播放顺序与当前文件名顺序不一致。"
                                        "若口播先后不对，请把「排序」改为「按分段文案自动匹配」后重新合成。"
                                    )
                        else:
                            if details:
                                for item in details:
                                    self.log.emit(
                                        f"  候选 文案第{item['segment_index'] + 1}段 ↔ {item['clip'].name}"
                                        f" (相似度 {item['score']:.2f})"
                                    )
                            if want_script_sort:
                                self.log.emit(f"提醒：{reason}，本组自动回退为文件名自然排序。")
                            else:
                                self.log.emit(f"提醒：{reason}（文件名顺序不变）。")
                            # 匹配失败时禁止「按索引硬套分段文案」做智能/文案裁剪
                            clip_scripts = [""] * len(clips)
                            if want_script_trim:
                                self.log.emit(
                                    "提醒：文案匹配未通过，智能/文案边界将按识别音轴裁剪，"
                                    "不使用分段文案时间窗（避免错段裁切）。"
                                )
                first_probe = self._probe(clips[0])
                target_w, target_h = calculate_target_size(
                    first_probe["width"], first_probe["height"],
                    self.settings.get("aspect_ratio", "原始比例"),
                    self.settings.get("resolution", "默认最高")
                )
                watermark = None
                prepare_watermark = self.settings.get("watermark_prepare")
                if self.settings.get("burn_watermark") and callable(prepare_watermark):
                    watermark = Path(prepare_watermark(str(clips[0]), str(cache_dir)))
                    if watermark.is_file():
                        self.log.emit("已启用合成时烧录水印：水印将在片段统一编码时一次完成，后续导出不重复烧录。")
                    else:
                        watermark = None
                from concurrent.futures import ThreadPoolExecutor, as_completed
                import os
                # QSV / Windows MF 多开极易卡死或 “Could not open encoder”；一律串行。
                # NVENC/AMF 最多双路；CPU 最多 4 路。
                if self.encoder in ("qsv", "mf"):
                    max_workers = 1
                    self.log.emit(
                        f"编码策略：{ENCODER_LABELS.get(self.encoder, self.encoder)} 串行"
                        f"（避免多路硬件会话卡死，看起来像停在智能混合边界之后）。"
                    )
                elif self.encoder in ("nvenc", "amf"):
                    max_workers = min(2, len(clips), os.cpu_count() or 2)
                    self.log.emit(
                        f"编码策略：硬件编码并行 {max_workers}"
                        f"（{ENCODER_LABELS.get(self.encoder, self.encoder)}；"
                        f"单段失败自动 CPU 重试）。"
                    )
                else:
                    max_workers = min(4, len(clips), os.cpu_count() or 4)
                    self.log.emit(
                        f"编码策略：CPU 并行 {max_workers} 路。"
                        f" 提示：片段多时建议改「自动硬件加速」可明显提速。"
                    )
                def run_norm(args):
                    clip, clip_index, total_count = args
                    return self._normalize(
                        clip, clip_index, total_count, cache_dir, analyses[str(clip.resolve())],
                        target_w, target_h, watermark, group_script=group_script,
                        clip_scripts=clip_scripts,
                    )
                tasks = [(clip, clip_index, len(clips)) for clip_index, clip in enumerate(clips)]
                normalized = [None] * len(clips)
                self.log.emit(
                    f"开始编码 {len(tasks)} 个片段…（若长时间无新日志，请看是否出现「编码进行中」心跳）"
                )
                with ThreadPoolExecutor(max_workers=max_workers) as executor:
                    futures = {executor.submit(run_norm, task): i for i, task in enumerate(tasks)}
                    for future in as_completed(futures):
                        i = futures[future]
                        try:
                            normalized[i] = future.result()
                        except Exception as exc:
                            # 取消其余任务，避免超时后仍占 QSV/MF
                            for other in futures:
                                other.cancel()
                            raise RuntimeError(
                                f"片段编码失败：{tasks[i][0].name} — {exc}"
                            ) from exc
                        done_n = sum(1 for p in normalized if p)
                        self.log.emit(
                            f"片段编码完成 [{done_n}/{len(tasks)}]：{tasks[i][0].name}"
                        )
                        completed_steps += 1
                        self.progress.emit(round(completed_steps / total_steps * 100))
                if any(path is None or not Path(path).is_file() for path in normalized):
                    raise RuntimeError(f"{folder.name} 有片段编码失败，无法合成。")
                if any(not self._probe(path)["audio"] for path in normalized):
                    raise RuntimeError(f"{folder.name} 中存在没有音轨的片段，无法保证无缝合并声音。")
                # 明确记录拼接顺序：源文件名 → 缓存段文件（便于核对「名字对、内容错」）
                order_lines = []
                for i, (src, norm) in enumerate(zip(clips, normalized), 1):
                    order_lines.append(f"{i:02d}. {Path(src).name} → {Path(norm).name}")
                self.log.emit("拼接顺序（必须与听感一致）：\n  " + "\n  ".join(order_lines))
                concat_file = cache_dir / "concat.txt"
                concat_file.write_text("\n".join(
                    "file '" + path.resolve().as_posix().replace("'", "'\\''") + "'" for path in normalized
                ), encoding="utf-8")
                # 旁路清单，方便人工打开核对
                try:
                    (cache_dir / "concat_order.txt").write_text(
                        "\n".join(order_lines) + "\n", encoding="utf-8"
                    )
                except Exception:
                    pass
                destination = self.output / f"{_safe_name(folder.name)}_去口气音合成.mp4"
                # Resolve group-specific transition name
                group_key = folder.resolve().as_posix().lower()
                custom_trans = self.settings.get("group_custom_transitions", {}).get(group_key, "跟随全局")
                if custom_trans != "跟随全局":
                    transition_name = custom_trans
                else:
                    transition_name = self.settings.get("transition_name", "无转场")

                final_fingerprint = hashlib.sha256(json.dumps({
                    "files": [self._signature(path) for path in normalized],
                    "clean_metadata": bool(self.settings.get("clean_metadata", True)),
                    "transition_name": transition_name,
                    "transition_duration": float(self.settings.get("transition_duration") or 0),
                    "aspect_ratio": self.settings.get("aspect_ratio", "原始比例"),
                    "resolution": self.settings.get("resolution", "默认最高"),
                    "group_script": group_script,
                    "version": 8,  # v8: 低相似度禁止文案重排
                }, sort_keys=True).encode("utf-8")).hexdigest()
                state_file = cache_dir / "final.json"
                try:
                    state = json.loads(state_file.read_text(encoding="utf-8")) if state_file.exists() else {}
                except Exception:
                    state = {}
                transition_cfg = resolve_merge_transition(transition_name)
                transition_key = (transition_cfg or {}).get("xfade") if transition_cfg else None
                actual_transition_duration = 0.0
                if not (self.settings.get("resume", True) and destination.exists() and destination.stat().st_size > 1024
                        and state.get("fingerprint") == final_fingerprint):
                    self.log.emit(f"正在合并文件夹“{folder.name}”的 {len(normalized)} 个片段，请等待…")
                    
                    if transition_key and len(normalized) > 1:
                        # 优先用户在 UI 设置的时长；未设置时用该转场类型的推荐默认值
                        user_dur = self.settings.get("transition_duration")
                        try:
                            user_dur = float(user_dur) if user_dur is not None else 0.0
                        except (TypeError, ValueError):
                            user_dur = 0.0
                        preset_dur = float((transition_cfg or {}).get("duration") or 0.22)
                        transition_duration = user_dur if user_dur >= 0.10 else preset_dur
                        # 口播默认偏短：画面最长 0.35s，避免长叠化造成「停一下」感
                        transition_duration = max(0.10, min(0.35, transition_duration))
                        segment_infos = [self._probe(path) for path in normalized]
                        min_segment_dur = min(info["duration"] for info in segment_infos)
                        actual_transition_duration = min(
                            transition_duration, max(0.10, min_segment_dur * 0.35)
                        )
                        # 音画必须同长叠化，否则 A/V 漂移；但不垫静音，叠的是真实句末/句首
                        self.log.emit(
                            f"应用合并转场「{transition_name}」→ xfade={transition_key}，"
                            f"时长 {actual_transition_duration:.2f}s（设定 {transition_duration:.2f}s），"
                            f"共 {len(normalized)} 段｜自然接缝（无静音垫）。"
                        )
                        
                        concat_command = [
                            self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y"
                        ]
                        for path in normalized:
                            concat_command += ["-i", str(path)]
                            
                        self.log.emit(
                            f"合并转场画面/声音同步 {actual_transition_duration:.2f}s；"
                            f"三角淡化，依赖尾保护气口而非垫静音。"
                        )
                        # 统一时间基/帧率，避免 xfade 报 timebase 不一致（如 1/12288 vs 1/15360）
                        filter_parts = []
                        for i in range(len(normalized)):
                            filter_parts.append(
                                f"[{i}:v]fps=30,settb=AVTB,setpts=PTS-STARTPTS,format=yuv420p[v{i}]"
                            )
                            filter_parts.append(
                                f"[{i}:a]aresample=48000,aformat=channel_layouts=stereo,"
                                f"asetpts=PTS-STARTPTS[a{i}]"
                            )
                        v_in = "[v0]"
                        a_in = "[a0]"
                        current_offset = segment_infos[0]["duration"] - actual_transition_duration
                        for i in range(1, len(normalized)):
                            next_v = f"[v{i}]"
                            next_a = f"[a{i}]"
                            out_v = f"[v_out_{i}]"
                            out_a = f"[a_out_{i}]"
                            filter_parts.append(
                                f"{v_in}{next_v}xfade=transition={transition_key}:"
                                f"duration={actual_transition_duration:.3f}:offset={current_offset:.3f}{out_v}"
                            )
                            # tri 比 exp 更干净；同长保证音画同步
                            filter_parts.append(
                                f"{a_in}{next_a}acrossfade=d={actual_transition_duration:.3f}:"
                                f"c1=tri:c2=tri{out_a}"
                            )
                            v_in = out_v
                            a_in = out_a
                            current_offset = (
                                current_offset + segment_infos[i]["duration"]
                                - actual_transition_duration
                            )
                        # 最终再归零时间戳，避免 xfade 链残留 start delay
                        filter_parts.append(f"{v_in}setpts=PTS-STARTPTS[vfinal]")
                        filter_parts.append(f"{a_in}asetpts=PTS-STARTPTS[afinal]")
                        filter_complex_str = ";".join(filter_parts)
                        concat_command += [
                            "-filter_complex", filter_complex_str,
                            "-map", "[vfinal]",
                            "-map", "[afinal]",
                        ]
                        concat_command += encoder_args(
                            self.encoder, self.settings.get("encode_preset", "veryfast"),
                            intermediate=True,
                        )
                        concat_command += ["-c:a", "aac", "-b:a", "160k", "-ac", "2", "-ar", "48000"]
                        if self.settings.get("clean_metadata", True):
                            concat_command += ["-map_metadata", "-1", "-map_metadata:s", "-1",
                                               "-map_metadata:p", "-1", "-map_metadata:c", "-1",
                                               "-map_chapters", "-1"]
                        concat_command += [
                            "-fps_mode", "cfr",
                            "-muxdelay", "0", "-muxpreload", "0",
                            "-avoid_negative_ts", "make_zero",
                            "-movflags", "+faststart",
                            str(destination),
                        ]
                    else:
                        if transition_name and transition_name != "无转场" and len(normalized) <= 1:
                            self.log.emit(f"本组仅 1 个片段，跳过转场「{transition_name}」。")
                        # 轻量重封装（不重编码）归零时间戳，避免 stream copy 保留负 DTS/start delay
                        concat_command = [
                            self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0",
                            "-i", str(concat_file), "-map", "0:v:0", "-map", "0:a:0", "-c", "copy",
                            "-muxdelay", "0", "-muxpreload", "0",
                            "-avoid_negative_ts", "make_zero",
                        ]
                        if self.settings.get("clean_metadata", True):
                            concat_command += ["-map_metadata", "-1", "-map_metadata:s", "-1",
                                               "-map_metadata:p", "-1", "-map_metadata:c", "-1",
                                               "-map_chapters", "-1"]
                        concat_command += ["-movflags", "+faststart", str(destination)]
                        
                    destination = self._write_final_output(concat_command, destination)
                    if remux_zero_start(self.ffmpeg, destination):
                        self.log.emit("已归零成品时间戳（避免达芬奇/Resolve 片头黑帧）。")
                    # 可选片头/片尾（硬切接到正文前后）
                    intro_p = ""
                    outro_p = ""
                    if self.settings.get("intro_enabled"):
                        intro_p = str(self.settings.get("selected_intro_path") or "").strip()
                    if self.settings.get("outro_enabled"):
                        outro_p = str(self.settings.get("selected_outro_path") or "").strip()
                    if (intro_p and Path(intro_p).is_file()) or (outro_p and Path(outro_p).is_file()):
                        try:
                            tw = int(self.settings.get("target_w") or 0)
                            th = int(self.settings.get("target_h") or 0)
                            wrap_with_intro_outro(
                                self.ffmpeg,
                                destination,
                                intro_path=intro_p if self.settings.get("intro_enabled") else "",
                                outro_path=outro_p if self.settings.get("outro_enabled") else "",
                                work_dir=Path(self.output) / ".intro_outro_cache",
                                target_w=tw,
                                target_h=th,
                                fps=30,
                            )
                            remux_zero_start(self.ffmpeg, destination)
                            bits = []
                            if intro_p and Path(intro_p).is_file() and self.settings.get("intro_enabled"):
                                bits.append(f"片头 {Path(intro_p).name}")
                            if outro_p and Path(outro_p).is_file() and self.settings.get("outro_enabled"):
                                bits.append(f"片尾 {Path(outro_p).name}")
                            self.log.emit(f"已拼接{' + '.join(bits)}。")
                        except Exception as bumper_exc:
                            self.log.emit(f"片头/片尾拼接失败（正文仍保留）：{bumper_exc}")
                    # 已对齐则静默跳过，不刷日志（避免误以为又慢了一遍）
                    if group_manual_bounds:
                        self.log.emit(f"群组切片功能：正在对合并后的成品视频应用手动切片 {group_manual_bounds[0]:.2f}s - {group_manual_bounds[1]:.2f}s...")
                        trimmed_dest = destination.with_name(destination.stem + "_trimmed.mp4")
                        
                        total_dur = self._probe(destination)["duration"]
                        start = max(0.0, min(total_dur, group_manual_bounds[0]))
                        end = max(start + 0.05, min(total_dur, group_manual_bounds[1]))
                        dur = end - start
                        
                        trim_cmd = [
                            self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                            "-ss", f"{start:.3f}", "-i", str(destination),
                            "-t", f"{dur:.3f}", "-c", "copy"
                        ]
                        if self.settings.get("clean_metadata", True):
                            trim_cmd += ["-map_metadata", "-1", "-map_metadata:s", "-1",
                                         "-map_metadata:p", "-1", "-map_metadata:c", "-1",
                                         "-map_chapters", "-1"]
                        trim_cmd += ["-movflags", "+faststart", str(trimmed_dest)]
                        
                        creation = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
                        res = subprocess.run(trim_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, creationflags=creation)
                        if res.returncode == 0:
                            try:
                                destination.unlink()
                                trimmed_dest.rename(destination)
                                self.log.emit(f"群组切片完成：已剪切保留 {start:.2f}s - {end:.2f}s 段。")
                            except Exception as e:
                                self.log.emit(f"群组切片重命名失败：{e}，保留未切片版本。")
                        else:
                            err = (res.stdout or b"").decode("utf-8", errors="replace")
                            self.log.emit(f"群组切片失败：{err}，保留未切片版本。")
                    used_transition_ms = 0
                    if transition_key and len(normalized) > 1 and actual_transition_duration:
                        used_transition_ms = int(round(float(actual_transition_duration) * 1000))
                    state_file.write_text(json.dumps({
                        "fingerprint": final_fingerprint,
                        "output_name": destination.name,
                        "group_name": folder.name,
                        "transition_ms": used_transition_ms,
                        "segment_count": len(normalized),
                    }, ensure_ascii=False, indent=2), encoding="utf-8")
                else:
                    self.log.emit(f"续接：复用已完成合成视频 {destination.name}")
                    used_transition_ms = 0
                    if transition_key and len(normalized) > 1 and actual_transition_duration:
                        used_transition_ms = int(round(float(actual_transition_duration) * 1000))
                # Sidecar: keep per-segment bars on the timeline for fine re-timing
                try:
                    seg_rows = []
                    for orig, norm in zip(clips, normalized):
                        try:
                            dur_ms = int(round(float(self._probe(norm)["duration"]) * 1000))
                        except Exception:
                            dur_ms = 0
                        seg_rows.append({
                            "name": Path(orig).name,
                            "duration_ms": max(80, dur_ms),
                            "original": str(Path(orig).resolve()),
                            "normalized": str(Path(norm).resolve()),
                        })
                    if not used_transition_ms:
                        if transition_key and len(normalized) > 1 and actual_transition_duration:
                            used_transition_ms = int(round(float(actual_transition_duration) * 1000))
                    sidecar = write_group_segments_map(
                        destination, seg_rows, transition_ms=used_transition_ms,
                    )
                    # Keep final.json aligned even on resume path
                    try:
                        prev = {}
                        if state_file.is_file():
                            prev = json.loads(state_file.read_text(encoding="utf-8")) or {}
                        prev.update({
                            "output_name": destination.name,
                            "group_name": folder.name,
                            "transition_ms": used_transition_ms,
                            "segment_count": len(seg_rows),
                        })
                        if "fingerprint" not in prev:
                            prev["fingerprint"] = final_fingerprint
                        state_file.write_text(json.dumps(prev, ensure_ascii=False, indent=2), encoding="utf-8")
                    except Exception:
                        pass
                    if sidecar:
                        self.log.emit(
                            f"已写入分段轨道元数据（{len(seg_rows)} 段）：{sidecar.name}，"
                            "时间轴按段落显示便于微调；最终导出仍是一个完整视频。"
                        )
                except Exception as exc:
                    self.log.emit(f"提醒：分段轨道元数据写入失败（不影响成品）：{exc}")
                outputs.append(str(destination))
                completed_steps += 1
                self.progress.emit(round(completed_steps / total_steps * 100))
                self.item_done.emit(str(destination), folder.name, group_index, len(self.groups))
                self.log.emit(f"[{group_index}/{len(self.groups)}] 合成完成：{destination}")
            self.finished.emit(True, json.dumps({"outputs": outputs}, ensure_ascii=False))
        except Exception as exc:
            self.finished.emit(False, str(exc))
