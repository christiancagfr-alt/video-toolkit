"""Non-destructive editor assistance. Times are integer milliseconds throughout.

No ASR guesses are presented as measured timing. Translation is supplied by the
user; alignment only maps source words onto an existing word clock.
"""
from __future__ import annotations

import csv
import difflib
import hashlib
import io
import json
import re
import shutil
import subprocess
import unicodedata
import xml.etree.ElementTree as ET
from fractions import Fraction
from pathlib import Path

from .canva_timeline import CaptionClip, parse_srt, write_srt
from .settings_page import hidden_kwargs


def normalized_tokens(text):
    text = unicodedata.normalize("NFKC", str(text)).casefold()
    return re.findall(r"[\u3400-\u9fff]|[^\W_]+", text, re.UNICODE)


def fingerprint(*values):
    return hashlib.sha256(json.dumps(values, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def audit_captions(srt, word_srt, expected_srt="", duration_ms=0):
    """Return heuristics, NOT recognition confidence or guaranteed lip sync."""
    cues, words, expected = parse_srt(srt), parse_srt(word_srt), parse_srt(expected_srt)
    result = []
    previous_end = 0
    for i, cue in enumerate(cues):
        reasons = []
        nearby = [w for w in words if w.end > cue.start and w.start < cue.end]
        a = normalized_tokens(cue.text)
        b = normalized_tokens(" ".join(w.text for w in nearby))
        match = difflib.SequenceMatcher(None, a, b, autojunk=False)
        coverage = sum(m.size for m in match.get_matching_blocks()) / max(1, len(a))
        if not words:
            reasons.append("无逐词时间轴，无法核验口型")
        elif coverage < .8:
            reasons.append("文字与词轴不一致：可能改字估时/漏字")
        if cue.start < previous_end:
            reasons.append("与上一句重叠")
        if cue.end - cue.start < 180:
            reasons.append("显示时间过短")
        if len(re.sub(r"\s", "", cue.text)) / max(.001, (cue.end-cue.start)/1000) > 30:
            reasons.append("阅读速度偏快")
        if duration_ms and cue.end > duration_ms + 50:
            reasons.append("超过视频时长")
        if expected and i < len(expected):
            old = expected[i]
            if old.text != cue.text or abs(old.start-cue.start) > 80 or abs(old.end-cue.end) > 80:
                reasons.append("与源轴重映射结果不同（也可能是人工修改）")
        elif expected:
            reasons.append("比源轴重映射结果多出字幕")
        result.append(dict(index=i, start=cue.start, end=cue.end, text=cue.text,
                           recognized=" ".join(w.text for w in nearby), coverage=coverage,
                           reasons=reasons, expected=expected[i].text if i < len(expected) else ""))
        previous_end = max(previous_end, cue.end)
    return result


def adjust_cue_times(srt, word_srt, edits):
    """Move/resize sentence clocks only after explicit review; transform its words.

    Ambiguous overlapping ownership is rejected instead of retiming a word twice.
    """
    cues, words = parse_srt(srt), parse_srt(word_srt)
    changed = {}
    for index, (start, end) in edits.items():
        if not 0 <= index < len(cues) or start < 0 or end <= start:
            raise ValueError("起止时间无效")
        old = cues[index]
        changed[index] = old
        cues[index] = CaptionClip(int(start), int(end), old.text)
    if any(b.start < a.end for a, b in zip(cues, cues[1:])):
        raise ValueError("修改后字幕相互重叠，请调整起止时间")
    mapped = []
    for word in words:
        owners = [(i, old) for i, old in changed.items()
                  if word.start < old.end and word.end > old.start]
        if len(owners) > 1:
            raise ValueError("词跨越多个待修改字幕，需先重新提取或修正重叠")
        if owners:
            i, old = owners[0]
            new = cues[i]
            ratio = (new.end-new.start) / (old.end-old.start)
            start = new.start + round((max(word.start, old.start)-old.start)*ratio)
            end = new.start + round((min(word.end, old.end)-old.start)*ratio)
            if end > start:
                mapped.append(CaptionClip(start, end, word.text))
        else:
            mapped.append(word)
    return write_srt(cues), write_srt(mapped)


def align_bilingual(word_srt, originals, translations):
    """Monotonic lexical alignment; unmatched sentences require manual review.

    Each input line is a sentence, not a word. Translations inherit sentence
    boundaries, never fabricated word-by-word timing in the other language.
    """
    if len(originals) != len(translations) or not originals:
        raise ValueError("原文与译文必须逐行对应，且行数相同")
    words = parse_srt(word_srt)
    if not words:
        raise ValueError("缺少逐词字幕，请先提取字幕")
    clock, owners = [], []
    for word in words:
        for token in normalized_tokens(word.text):
            clock.append(token)
            owners.append(word)
    script, spans = [], []
    for line in originals:
        begin = len(script)
        script.extend(normalized_tokens(line))
        spans.append((begin, len(script)))
    mapping = {}
    for a, b, count in difflib.SequenceMatcher(None, script, clock, autojunk=False).get_matching_blocks():
        for offset in range(count):
            mapping[a+offset] = b+offset
    result = []
    last_end = -1
    for original, translation, (a, b) in zip(originals, translations, spans):
        indices = [mapping[n] for n in range(a, b) if n in mapping]
        coverage = len(indices)/max(1, b-a)
        start = owners[indices[0]].start if indices else None
        end = owners[indices[-1]].end if indices else None
        reasons = []
        if not original.strip() or not translation.strip():
            reasons.append("原文或译文为空")
        if coverage < 1:
            reasons.append("存在未匹配文字，禁止自动估时导出")
        if indices and indices != list(range(indices[0], indices[-1]+1)):
            reasons.append("原音中夹有其他文字，需确认区间")
        if start is not None and start < last_end:
            reasons.append("时间区间重叠")
        if start is not None:
            last_end = end
        # Repeated identical target phrases are safe only when occurrence counts agree.
        target = script[a:b]
        occurrences = sum(clock[j:j+len(target)] == target for j in range(len(clock)-len(target)+1)) if target else 0
        requested = sum(script[x:y] == target for x, y in spans)
        if occurrences > requested:
            reasons.append("原音中有多个相同句子，需要手动确认")
        result.append(dict(original=original, translation=translation, start=start, end=end,
                           coverage=coverage, reasons=reasons))
    return result


def bilingual_srts(rows):
    if not rows or any(r.get("reasons") for r in rows):
        raise ValueError("仍有待核对的句子，不能导出。请修正文案或手动确认时间")
    original, translated, both = [], [], []
    last_end = -1
    for row in rows:
        start, end = row["start"], row["end"]
        if start is None or end is None or start < 0 or end <= start or start < last_end:
            raise ValueError("时间无效或字幕相互重叠")
        if not row["original"].strip() or not row["translation"].strip():
            raise ValueError("原文和译文均不能为空")
        original.append(CaptionClip(start, end, row["original"]))
        translated.append(CaptionClip(start, end, row["translation"]))
        both.append(CaptionClip(start, end, row["original"]+"\n"+row["translation"]))
        last_end = end
    return tuple(write_srt(items) for items in (original, translated, both))


def parse_bilingual_tsv(text):
    rows = list(csv.reader(io.StringIO(text), delimiter="\t"))
    if rows and rows[0][:3] in (["视频", "原文", "译文"], ["filename", "source", "translation"]):
        rows = rows[1:]
    if not rows or any(len(r) != 3 for r in rows):
        raise ValueError("请复制三列：视频文件名、原文、译文。单元格内可换行")
    return rows


def media_signature(path):
    path = Path(path).resolve()
    stat = path.stat()
    return str(path), stat.st_size, stat.st_mtime_ns


def probe_media(ffmpeg, path):
    probe = Path(ffmpeg).with_name("ffprobe.exe" if str(ffmpeg).lower().endswith(".exe") else "ffprobe")
    result = subprocess.run([str(probe), "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
                            capture_output=True, timeout=30, **hidden_kwargs())
    if result.returncode:
        raise ValueError(f"无法读取媒体：{Path(path).name}")
    return json.loads(result.stdout)


def visual_hashes(ffmpeg, path):
    """Images: one dHash; videos: three samples. Same type only is compared."""
    image_exts = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".heif", ".heic"}
    if Path(path).suffix.lower() in image_exts:
        from PIL import Image, ImageOps
        if Path(path).suffix.lower() in {".heif", ".heic"}:
            import pillow_heif
            pillow_heif.register_heif_opener()
        with Image.open(path) as image:
            pixels = list(ImageOps.exif_transpose(image).convert("L").resize((9, 8)).getdata())
        return "image", (_dhash(pixels),)
    info = probe_media(ffmpeg, path)
    duration = float(info.get("format", {}).get("duration", 0))
    if duration <= 0:
        raise ValueError("无法读取视频时长")
    hashes = []
    for fraction in (.1, .5, .9):
        result = subprocess.run([str(ffmpeg), "-v", "error", "-ss", str(duration*fraction), "-i", str(path),
                                 "-frames:v", "1", "-vf", "scale=9:8,format=gray", "-f", "rawvideo", "pipe:1"],
                                capture_output=True, timeout=30, **hidden_kwargs())
        if result.returncode or len(result.stdout) != 72:
            raise ValueError("视频抽帧失败")
        hashes.append(_dhash(result.stdout))
    return "video", tuple(hashes)


def _dhash(pixels):
    value = 0
    for y in range(8):
        for x in range(8):
            value = (value << 1) | int(pixels[y*9+x] > pixels[y*9+x+1])
    return value


def group_similar(items, threshold=5):
    """Bound distance to every member, avoiding A~B~C transitive false groups."""
    groups = []
    for item in items:
        for group in groups:
            if all(item["kind"] == other["kind"] and
                   len(item["hashes"]) == len(other["hashes"]) and
                   all((a ^ b).bit_count() <= threshold for a, b in zip(item["hashes"], other["hashes"]))
                   for other in group):
                group.append(item)
                break
        else:
            groups.append([item])
    return [group for group in groups if len(group) > 1]


def copy_group_files(groups, output):
    """Copy only. Exclusive writes and source signatures protect existing files."""
    output = Path(output).resolve()
    for group in groups:
        for item in group:
            if tuple(item["signature"]) != media_signature(item["path"]):
                raise ValueError(f"素材已改变，请重新扫描：{item['path']}")
    output.mkdir(parents=True, exist_ok=True)
    for index, group in enumerate(groups, 1):
        folder = output / f"group_{index:03d}"
        folder.mkdir(exist_ok=True)
        for item in group:
            source = Path(item["path"])
            target = folder / source.name
            if target.exists():
                target = folder / f"{source.stem}_{fingerprint(str(source))[:10]}{source.suffix}"
            with source.open("rb") as incoming, target.open("xb") as outgoing:
                shutil.copyfileobj(incoming, outgoing)


def build_fcpxml(source, state, srt, metadata, fps="30", size=(1080, 1920)):
    """Editable straight-cut picture/audio timeline + native caption lane.

    Unsupported effects are explicitly reported. XML timing uses rational seconds;
    source start offsets are kept separate from timeline offsets (no cumulative drift).
    """
    rate = Fraction(str(fps))
    if not 1 <= rate <= 240:
        raise ValueError("帧率无效")
    def stamp(ms):
        frames = round(Fraction(str(ms))*rate/1000)
        value = Fraction(frames, 1)/rate
        return f"{value.numerator}/{value.denominator}s"
    root = ET.Element("fcpxml", version="1.10")
    resources = ET.SubElement(root, "resources")
    ET.SubElement(resources, "format", id="r1", frameDuration=f"{rate.denominator}/{rate.numerator}s",
                  width=str(size[0]), height=str(size[1]), colorSpace="1-1-1 (Rec. 709)")
    tracks = state.get("tracks", {})
    duration = int(float(metadata[str(source)].get("format", {}).get("duration", 0))*1000)
    video = tracks.get("video") if "video" in tracks else [dict(start=0, end=duration, source_start=0, source_end=duration, path=source)]
    if not video:
        raise ValueError("没有视频片段可以导出")
    all_tracks = [(0, video)]
    for lane, name in ((-1, "original_audio"), (-2, "bgm"), (-3, "tts")):
        if name == "original_audio" and not state.get("original_audio_enabled", True):
            continue
        if tracks.get(name):
            all_tracks.append((lane, tracks[name]))
    paths = list(dict.fromkeys(str(c.get("path") or source) for _, clips in all_tracks for c in clips))
    refs = {}
    for path in paths:
        info = metadata[path]
        streams = info.get("streams", [])
        has_video = any(s.get("codec_type") == "video" for s in streams)
        audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
        ref = f"r{len(refs)+2}"
        refs[path] = ref
        attrs = dict(id=ref, name=Path(path).name, start="0s", duration=stamp(float(info.get("format", {}).get("duration", duration/1000))*1000),
                     hasVideo="1" if has_video else "0", hasAudio="1" if audio else "0")
        if has_video:
            stream = next(s for s in streams if s.get("codec_type") == "video")
            fid = f"f{len(refs)}"
            fr = Fraction(stream.get("avg_frame_rate") or "30")
            if fr <= 0:
                fr = rate
            ET.SubElement(resources, "format", id=fid, width=str(stream.get("width", size[0])),
                          height=str(stream.get("height", size[1])), frameDuration=f"{fr.denominator}/{fr.numerator}s")
            attrs["format"] = fid
        if audio:
            attrs.update(audioSources="1", audioChannels=str(audio.get("channels", 2)), audioRate=str(audio.get("sample_rate", 48000)))
        asset = ET.SubElement(resources, "asset", **attrs)
        ET.SubElement(asset, "media-rep", kind="original-media", src=Path(path).resolve().as_uri())
    event = ET.SubElement(ET.SubElement(root, "library"), "event", name="VideoToolkit")
    project = ET.SubElement(event, "project", name=Path(source).stem)
    total = max(int(c["end"]) for _, clips in all_tracks for c in clips)
    seq = ET.SubElement(project, "sequence", format="r1", duration=stamp(total), tcStart="0s", tcFormat="NDF", audioLayout="stereo", audioRate="48k")
    spine = ET.SubElement(seq, "spine")
    # A single gap provides a stable time origin for independent A/V tracks/captions.
    anchor = ET.SubElement(spine, "gap", name="Timeline", offset="0s", start="0s", duration=stamp(total))
    warnings = []
    for lane, clips in all_tracks:
        for clip in clips:
            start, end = int(clip["start"]), int(clip["end"])
            if start < 0 or end <= start:
                raise ValueError("片段时间无效")
            path = str(clip.get("path") or source)
            source_start = int(clip.get("source_start", 0))
            source_end = int(clip.get("source_end", source_start+end-start))
            if abs((source_end-source_start)-(end-start)) > 2 or abs(float(clip.get("speed", 1))-1) > .001:
                raise ValueError("当前工程含变速片段，此版工程导出不支持变速；请先恢复 1× 或导出成品")
            enable = "video" if lane == 0 and ("original_audio" in tracks or not state.get("original_audio_enabled", True)) else ("all" if lane == 0 else "audio")
            ET.SubElement(anchor, "asset-clip", ref=refs[path], name=Path(path).name, lane=str(1 if lane == 0 else lane),
                          offset=stamp(start), start=stamp(source_start), duration=stamp(end-start), srcEnable=enable)
    for i, cue in enumerate(parse_srt(srt)):
        if cue.end > total:
            warnings.append("超过工程时长的字幕已保留在 SRT，但未放入工程")
            continue
        caption = ET.SubElement(anchor, "caption", lane="2", offset=stamp(cue.start), start="0s", duration=stamp(cue.end-cue.start), role="iTT.en")
        text = ET.SubElement(caption, "text")
        ET.SubElement(text, "text-style", ref=f"ts{i}").text = cue.text
        definition = ET.SubElement(caption, "text-style-def", id=f"ts{i}")
        ET.SubElement(definition, "text-style", font="Arial", fontSize="48", fontColor="1 1 1 1", alignment="center")
    warnings.append("工程交接为基础画面/音轨切点和可编辑文字；蒙版、跟读动画、调色、音量包络、转场需在目标软件重建。SRT 同时输出以便兼容导入。")
    ET.indent(root)
    return '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n'+ET.tostring(root, encoding="unicode"), list(dict.fromkeys(warnings))
