"""Shared, timestamp-based cutting for Smart Cut and the automatic pipeline."""
from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import uuid
from bisect import bisect_left
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path


def run_media(command, cancelled=lambda: False):
    if cancelled():
        raise RuntimeError("用户已取消任务")
    kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs)
    try:
        while True:
            try:
                stdout, stderr = process.communicate(timeout=0.2)
                break
            except subprocess.TimeoutExpired:
                if cancelled():
                    raise RuntimeError("用户已取消任务")
        if cancelled():
            raise RuntimeError("用户已取消任务")
        if process.returncode:
            raise RuntimeError(stderr.decode("utf-8", "replace")[-2000:] or "媒体处理失败")
        return stdout
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()


def _number(value, default=0.0):
    try:
        value = float(value)
        return value if math.isfinite(value) else default
    except (ValueError, TypeError):
        return default


@dataclass(frozen=True)
class VideoClock:
    duration: float
    start: float
    fps: float


def _probe_command(ffmpeg):
    sibling = Path(ffmpeg).with_name("ffprobe.exe" if os.name == "nt" else "ffprobe")
    ffprobe = str(sibling) if sibling.is_file() else shutil.which("ffprobe")
    if not ffprobe:
        raise RuntimeError("缺少 FFprobe，无法校验精确剪辑时间")
    return [ffprobe, "-v", "error", "-select_streams", "V:0"]


def _video_packets(ffmpeg, path, cancelled):
    data = json.loads(run_media(_probe_command(ffmpeg) + [
        "-show_packets", "-show_entries", "packet=pts_time,duration_time",
        "-of", "json", str(path)], cancelled))
    times = [(_number(p.get("pts_time"), math.nan),
              _number(p.get("duration_time"))) for p in data.get("packets", [])]
    return [(pts, length) for pts, length in times if math.isfinite(pts)]


def probe_video(ffmpeg, path, cancelled=lambda: False):
    command = _probe_command(ffmpeg)
    data = json.loads(run_media(command + [
        "-show_entries", "stream=start_time,duration,avg_frame_rate,r_frame_rate",
        "-of", "json", str(path)], cancelled))
    streams = data.get("streams", [])
    if not streams:
        raise RuntimeError("文件没有可剪辑的视频画面")
    stream = streams[0]
    start = _number(stream.get("start_time"))
    duration = _number(stream.get("duration"))
    try:
        fps = float(Fraction(stream.get("avg_frame_rate", "0/1")))
    except (ValueError, ZeroDivisionError):
        fps = 0.0
    if fps <= 0:
        try:
            fps = float(Fraction(stream.get("r_frame_rate", "0/1")))
        except (ValueError, ZeroDivisionError):
            fps = 0.0
    if duration <= 0:
        # Matroska and transport streams often omit stream duration. Container
        # duration can belong to longer audio, so measure video packets only.
        times = _video_packets(ffmpeg, path, cancelled)
        if times:
            start = min(pts for pts, _ in times)
            duration = max(pts + (length if length > 0 else 1 / (fps or 25))
                           for pts, length in times) - start
    if not math.isfinite(duration) or duration <= 0:
        raise RuntimeError("无法读取有效的视频画面时长")
    return VideoClock(duration, start, fps or 25.0)


def parse_lengths(sequence, loop_length):
    try:
        lengths = [float(item.strip()) for item in str(sequence).replace("，", ",").split(",")
                   if item.strip()]
        loop = float(loop_length)
    except (TypeError, ValueError) as exc:
        raise ValueError("时长序列和循环时长必须是数字") from exc
    if any(not math.isfinite(length) or length <= 0 for length in [*lengths, loop]):
        raise ValueError("每段时长和循环时长必须是大于 0 的有限数字")
    return lengths, loop


def fixed_ranges(duration, sequence, loop_length):
    lengths, loop = parse_lengths(sequence, loop_length)
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("视频时长无效")
    start, index = 0.0, 0
    while start < duration - 1e-8:
        end = min(duration, start + (lengths[index] if index < len(lengths) else loop))
        if end <= start:
            raise ValueError("切片时长过小")
        yield start, end
        start, index = end, index + 1


def frame_aligned_ranges(ffmpeg, path, ranges, clock, cancelled=lambda: False):
    # A timestamp between two frames cannot be a physical video cut. Assign
    # each boundary to the next presentation frame once for both neighbours.
    # A sub-frame tail then belongs to the last real frame instead of producing
    # an empty MP4 (common with 29.97 fps material cut at integer seconds).
    points = sorted(set(pts - clock.start for pts, _ in _video_packets(ffmpeg, path, cancelled)
                        if -1e-7 <= pts - clock.start < clock.duration - 1e-7))
    if not points:
        raise RuntimeError("无法读取画面时间戳，不能生成精确剪辑")
    previous = 0.0
    for _start, end in ranges:
        if cancelled():
            raise RuntimeError("用户已取消任务")
        index = bisect_left(points, end - 1e-7)
        boundary = points[index] if index < len(points) else clock.duration
        if boundary > previous + 1e-8:
            yield previous, boundary
            previous = boundary


class _CancellableVideo:
    def __init__(self, video, cancelled):
        self.video, self.cancelled = video, cancelled

    def __getattr__(self, name):
        return getattr(self.video, name)

    def read(self, *args, **kwargs):
        return False if self.cancelled() else self.video.read(*args, **kwargs)


def scene_ranges(path, clock, threshold, cancelled=lambda: False):
    from scenedetect import ContentDetector, SceneManager, __version__
    from scenedetect.backends.pyav import VideoStreamAv

    if tuple(int(part) for part in __version__.split(".")[:2]) < (0, 7):
        raise RuntimeError("精确场景剪辑需要 scenedetect 0.7 或更新版本，请在设置中更新组件")
    if not math.isfinite(threshold) or not 0 < threshold <= 255:
        raise ValueError("场景阈值必须大于 0 且不超过 255")
    if cancelled():
        raise RuntimeError("用户已取消任务")
    # PyAV uses actual presentation timestamps, including variable frame rates.
    # OpenCV/older frame-count timecodes can drift away from the video clock.
    manager = SceneManager()
    manager.add_detector(ContentDetector(threshold=threshold))
    # Own the input handle: VideoStreamAv currently has no public close() and
    # its finalizer closes the decoder/container but not its path-opened file.
    with open(path, "rb") as handle:
        video = VideoStreamAv(handle, name=Path(path).name)
        manager.detect_scenes(video=_CancellableVideo(video, cancelled), show_progress=False)
    if cancelled():
        raise RuntimeError("用户已取消任务")
    scenes = manager.get_scene_list(start_in_scene=True)
    if not scenes:
        raise RuntimeError("未能解码视频画面，不能生成剪辑片段")
    boundaries = [0.0]
    for start, _end in scenes[1:]:
        position = float(start.seconds)
        if not math.isfinite(position) or position <= boundaries[-1]:
            raise RuntimeError("场景检测返回了无效或倒退的切点")
        if position < clock.duration - 1e-8:
            boundaries.append(position)
    boundaries.append(clock.duration)
    return list(zip(boundaries, boundaries[1:]))


def cut_range(ffmpeg, source, destination, start, end, clock=None, cancelled=lambda: False):
    clock = clock or probe_video(ffmpeg, source, cancelled)
    if not all(math.isfinite(value) for value in (start, end)):
        raise ValueError("剪辑时间必须是有限数字")
    if start < 0 or end <= start or end > clock.duration + 1e-6:
        raise ValueError("剪辑区间超出视频画面范围")
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.stem}.{uuid.uuid4().hex}.partial.mp4")
    first, last = clock.start + start, clock.start + min(end, clock.duration)
    # Seek to the preceding keyframe, decode/discard the preroll, and trim both
    # tracks on one absolute clock. Resetting each track with STARTPTS separately
    # would remove real audio offsets. Stream copy cannot enforce these bounds.
    video_filter = (f"trim=start={first:.9f}:end={last:.9f},setpts=PTS-({first:.9f})/TB,"
                    "pad=ceil(iw/2)*2:ceil(ih/2)*2")
    audio_filter = f"atrim=start={first:.9f}:end={last:.9f},asetpts=PTS-({first:.9f})/TB"
    command = [
        str(ffmpeg), "-hide_banner", "-loglevel", "error", "-nostdin", "-xerror", "-y",
        "-copyts", "-seek_timestamp", "1", "-ss", f"{first:.9f}", "-noaccurate_seek",
        "-i", str(source),
        "-map", "0:V:0", "-map", "0:a?", "-map_metadata", "0", "-map_chapters", "-1",
        "-vf", video_filter, "-af", audio_filter, "-t", f"{last - first:.9f}",
        "-c:v", "libx264", "-threads:v", "2", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
        # Use the named source clock: FFmpeg 9 removed the legacy -1 alias.
        "-fps_mode", "vfr", "-enc_time_base:v", "demux",
        "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
        str(temporary),
    ]
    try:
        run_media(command, cancelled)
        actual = probe_video(ffmpeg, temporary, cancelled)
        if abs(actual.start + actual.duration - (end - start)) > max(0.1, 2 / clock.fps):
            raise RuntimeError("片段画面时长与指定区间不符，已停止保存")
        if cancelled():
            raise RuntimeError("用户已取消任务")
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
