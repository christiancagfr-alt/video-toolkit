"""Window ownership and validation for local ASR, in media seconds."""
from __future__ import annotations

import math
import re


class CaptionTimingError(RuntimeError):
    """Recognition/cached timestamps are not on the media's clock."""


def checked_span(start, end, duration=None, tolerance=0.12):
    try:
        start, end = float(start), float(end)
    except (TypeError, ValueError) as exc:
        raise CaptionTimingError("字幕时间戳缺失或不是数字") from exc
    if not math.isfinite(start) or not math.isfinite(end) or start < -tolerance or end <= start:
        raise CaptionTimingError(f"无效字幕时间段：{start} → {end}")
    start = max(0.0, start)
    if duration is not None:
        duration = float(duration)
        if not math.isfinite(duration) or duration <= 0:
            raise CaptionTimingError("无法确认媒体真实时长，不能校验字幕时间轴")
        if start >= duration or end > duration + tolerance:
            raise CaptionTimingError(f"字幕时间戳越界：{start:.3f}–{end:.3f}s，媒体仅 {duration:.3f}s")
        end = min(end, duration)
    if end <= start:
        raise CaptionTimingError("字幕时间段没有有效时长")
    return start, end


def validate_asr_segments(segments, duration):
    """Validate words before adding offsets. Real word bounds own the sentence.

    Only sub-frame/codec rounding at EOF is clipped; seconds/minutes of excess
    must cause a retry or an explicit error, never an apparently valid result.
    """
    result = []
    for segment in segments or []:
        words = []
        for word in segment.get("words") or []:
            start, end = checked_span(word.get("start"), word.get("end"), duration)
            words.append({**word, "start": start, "end": end})
        if words:
            words.sort(key=lambda w: (w["start"], w["end"]))
            start, end = words[0]["start"], max(w["end"] for w in words)
        else:
            start, end = checked_span(segment.get("start"), segment.get("end"), duration)
        result.append({**segment, "start": start, "end": end, "words": words})
    return sorted(result, key=lambda s: (s["start"], s["end"]))


def validate_srt_timing(srt, duration):
    """Reject malformed/out-of-media SRT before caching, estimating or rendering."""
    pattern = re.compile(r"^[ \t]*(\d+):(\d{2}):(\d{2})[,.](\d+)[ \t]*-->[ \t]*(\d+):(\d{2}):(\d{2})[,.](\d+)[ \t]*\r?$", re.M)
    count = 0
    def stamp(t):
        ms = round(t * 1000)
        h, ms = divmod(ms, 3600000)
        m, ms = divmod(ms, 60000)
        sec, ms = divmod(ms, 1000)
        return f"{h:02d}:{m:02d}:{sec:02d},{ms:03d}"
    def replace(match):
        nonlocal count
        values = match.groups()
        for pos in (0, 4):
            if int(values[pos + 1]) >= 60 or int(values[pos + 2]) >= 60:
                raise CaptionTimingError("字幕时间戳的分/秒字段超出范围")
        def seconds(pos):
            return (int(values[pos]) * 3600 + int(values[pos + 1]) * 60
                    + int(values[pos + 2]) + int(values[pos + 3]) / 10 ** len(values[pos + 3]))
        start, end = checked_span(seconds(0), seconds(4), duration)
        if round(end * 1000) <= round(start * 1000):
            raise CaptionTimingError("字幕时间段短于 SRT 毫秒精度")
        count += 1
        return f"{stamp(start)} --> {stamp(end)}"
    text = str(srt or "")
    result = pattern.sub(replace, text)
    if not count or count != text.count("-->"):
        raise CaptionTimingError("识别结果没有完整、有效的字幕时间戳")
    return result


def asr_gaps(segments, duration, min_gap=1.5):
    """Uncovered intervals, including the middle; gaps are NOT proof of speech."""
    spans = []
    for segment in segments or []:
        for item in segment.get("words") or [segment]:
            start, end = checked_span(item.get("start"), item.get("end"), duration)
            spans.append((start, end))
    gaps, cursor = [], 0.0
    for start, end in sorted(spans):
        if start - cursor >= min_gap:
            gaps.append((cursor, start))
        cursor = max(cursor, end)
    if duration - cursor >= min_gap:
        gaps.append((cursor, duration))
    return gaps


def merge_asr_recovery(current, retry, duration):
    """Fill uncovered time only; never replace good anchors with a longer guess."""
    current = validate_asr_segments(current, duration)
    retry = validate_asr_segments(retry, duration)
    if not current:
        return retry
    gaps = asr_gaps(current, duration, min_gap=0.08)
    added = []
    for segment in retry:
        for word in segment.get("words") or []:
            start, end = word["start"], word["end"]
            for a, b in gaps:
                if a <= (start + end) / 2 < b and min(end, b) - max(start, a) >= .04:
                    w = {**word, "start": max(start, a), "end": min(end, b)}
                    added.append({"start": w["start"], "end": w["end"],
                                  "text": w.get("text") or w.get("word") or "", "words": [w]})
                    break
    if not added:
        return current
    # A recovered middle word must interleave with the original sentence's
    # words. Sorting whole sentences would append it after that sentence's tail.
    anchors = []
    for segment in current:
        if not segment["words"]:
            anchors.append(segment)
        for word in segment["words"]:
            anchors.append({"start": word["start"], "end": word["end"],
                            "text": word.get("text") or word.get("word") or "", "words": [word]})
    return sorted(anchors + added, key=lambda s: (s["start"], s["end"]))


def recognition_windows(duration: float, core_seconds: float = 26.0) -> list[dict]:
    """Disjoint ownership intervals; callers add 2s context on either side.

    A 26s core plus context fits Whisper's 30s window. Context is decoded but
    must never be appended twice. Include even a short final interval.
    """
    duration = max(0.0, float(duration))
    if core_seconds <= 0:
        raise ValueError("core_seconds must be positive")
    windows = []
    start = 0.0
    while start < duration:
        end = min(duration, start + core_seconds)
        windows.append({"start": start, "end": end, "label": f"{start:.0f}s"})
        start = end
    return windows


def owned_asr_segments(segments, start: float, end: float, duration: float):
    """Assign context words by midpoint without shifting their timestamps.

    Word-bearing segments with no owned words are discarded, never resurrected
    as a whole sentence (which used to reintroduce overlap words).
    """
    result = []
    for segment in segments or []:
        original_words = segment.get("words") or []
        if original_words:
            words = []
            for word in original_words:
                ws, we = float(word["start"]), float(word["end"])
                if we <= ws or not start <= (ws + we) / 2 < end:
                    continue
                ws, we = max(0.0, ws), min(duration, we)
                if we > ws:
                    words.append({**word, "start": ws, "end": we})
            if not words:
                continue
            words.sort(key=lambda w: (w["start"], w["end"]))
            result.append({
                **segment, "words": words,
                "start": words[0]["start"], "end": words[-1]["end"],
                "text": " ".join(str(w.get("text") or w.get("word") or "").strip()
                                 for w in words).strip(),
            })
        else:
            ss, se = float(segment["start"]), float(segment["end"])
            if not start <= (ss + se) / 2 < end:
                continue
            ss, se = max(start, ss), min(end, duration, se)
            if se > ss:
                result.append({**segment, "start": ss, "end": se})
    return sorted(result, key=lambda s: (s["start"], s["end"]))


def asr_coverage(segments, duration=0.0):
    words = [w for s in (segments or []) for w in (s.get("words") or [])]
    # Segment ends can reach EOF even when their word timestamps stop early.
    timed = words or list(segments or [])
    last = max((float(w.get("end") or 0) for w in timed), default=0.0)
    return len(words), last, last / duration if duration > 0 else 0.0


def asr_needs_retry(segments, duration: float) -> bool:
    """A suspicion check, not proof of missing speech; never stretch timestamps.

    Trailing silence can trigger one retry with VAD disabled. A short unvoiced
    tail must remain blank if the retry also finds no words.
    """
    count, last, _ratio = asr_coverage(segments, duration)
    if not segments or count == 0:
        return True
    timed = [w for s in segments for w in (s.get("words") or [])]
    first = min(float(w.get("start") or 0) for w in timed)
    span = max(0.5, last - first)
    return (count < max(3, int(span / 1.6)) or bool(asr_gaps(segments, duration)))
