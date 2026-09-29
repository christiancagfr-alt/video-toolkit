from __future__ import annotations

import math
import re
import struct
import subprocess
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QEvent, QMimeData, QPoint, QPointF, QProcess, Qt, Signal
from PySide6.QtGui import QColor, QDrag, QFont, QPainter, QPainterPath, QPen, QWheelEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

# Timeline zoom: px per second. High end ≈ frame-level editing at 30fps
# (e.g. 900 px/s ≈ 30 px/frame). Low end is overview.
ZOOM_MIN_PPS = 12
ZOOM_MAX_PPS = 1800
ZOOM_DEFAULT_PPS = 42


_TIME_RE = re.compile(
    r"(?P<sh>\d{1,2}):(?P<sm>\d{2}):(?P<ss>\d{2})[,.](?P<sms>\d{3})"
    r"\s*-->\s*"
    r"(?P<eh>\d{1,2}):(?P<em>\d{2}):(?P<es>\d{2})[,.](?P<ems>\d{3})"
)


def _milliseconds(hours: str, minutes: str, seconds: str, millis: str) -> int:
    return (((int(hours) * 60 + int(minutes)) * 60) + int(seconds)) * 1000 + int(millis)


def _timestamp(milliseconds: int) -> str:
    milliseconds = max(0, int(milliseconds))
    seconds, millis = divmod(milliseconds, 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


@dataclass
class CaptionClip:
    start: int
    end: int
    text: str


@dataclass
class MediaClip:
    start: int
    end: int
    source_start: int
    source_end: int
    name: str = ""
    # Full source media length in ms; allows edge-drag to restore trimmed content.
    source_duration: int = 0
    media_type: str = "video"
    path: str = ""
    # Playback speed: 1.0 normal, 2.0 = 2× fast (shorter on timeline), 0.5 = slow-mo.
    speed: float = 1.0

    def as_dict(self) -> dict:
        return {
            "start": self.start,
            "end": self.end,
            "source_start": self.source_start,
            "source_end": self.source_end,
            "name": self.name,
            "source_duration": self.source_duration or max(self.source_end, 0),
            "media_type": self.media_type,
            "path": self.path,
            "speed": float(self.speed or 1.0),
        }

    def source_span_ms(self) -> int:
        return max(80, int(self.source_end) - int(self.source_start))

    def timeline_span_for_speed(self, speed: float | None = None) -> int:
        spd = max(0.25, min(4.0, float(speed if speed is not None else self.speed or 1.0)))
        return max(80, int(round(self.source_span_ms() / spd)))

    def resolved_source_duration(self, fallback: int = 0) -> int:
        """Full source file length; never shrink just because the clip was trimmed."""
        candidates = [
            int(self.source_duration or 0),
            int(fallback or 0),
            int(self.source_end or 0),
            int(self.source_start or 0) + max(80, int(self.end - self.start)),
        ]
        return max(candidates)


class TransitionPresetButton(QPushButton):
    """A normal preset button that can also be dragged onto the timeline."""

    MIME_TYPE = "application/x-video-transition"

    def __init__(self, text: str, transition_name: str, parent=None):
        super().__init__(text, parent)
        self.transition_name = str(transition_name)
        self._press_position = QPoint()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._press_position = event.position().toPoint()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if not (event.buttons() & Qt.MouseButton.LeftButton):
            return super().mouseMoveEvent(event)
        if (event.position().toPoint() - self._press_position).manhattanLength() < 8:
            return super().mouseMoveEvent(event)
        mime = QMimeData()
        mime.setData(self.MIME_TYPE, self.transition_name.encode("utf-8"))
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.exec(Qt.DropAction.CopyAction)


def parse_srt(text: str) -> list[CaptionClip]:
    clips: list[CaptionClip] = []
    for block in re.split(r"\r?\n\s*\r?\n", str(text or "").strip()):
        lines = block.splitlines()
        timing_index = next((i for i, line in enumerate(lines) if "-->" in line), -1)
        if timing_index < 0:
            continue
        match = _TIME_RE.search(lines[timing_index])
        if not match:
            continue
        values = match.groupdict()
        start = _milliseconds(values["sh"], values["sm"], values["ss"], values["sms"])
        end = _milliseconds(values["eh"], values["em"], values["es"], values["ems"])
        copy = "\n".join(lines[timing_index + 1 :]).strip()
        if end > start:
            clips.append(CaptionClip(start, end, copy))
    return clips


def write_srt(clips: list[CaptionClip]) -> str:
    blocks = []
    for index, clip in enumerate(clips, 1):
        blocks.append(
            f"{index}\n{_timestamp(clip.start)} --> {_timestamp(clip.end)}\n{clip.text}".rstrip()
        )
    return "\n\n".join(blocks) + ("\n" if blocks else "")


class TimelineCanvas(QWidget):
    srtChanged = Signal(str)
    seekRequested = Signal(int)
    # 涟漪删除区间（毫秒）：上层用于同步词级 SRT
    rangeRippled = Signal(int, int)
    timelineEdited = Signal(dict)
    # deltaY from wheel, x on canvas (for zoom-to-cursor)
    zoomWheel = Signal(int, int)

    LABEL_WIDTH = 112
    RULER_HEIGHT = 26
    TRACK_HEIGHT = 38

    def __init__(self, parent=None):
        super().__init__(parent)
        self.duration_ms = 10_000
        self.position_ms = 0
        self.pixels_per_second = ZOOM_DEFAULT_PPS
        # Full length of the loaded media file (ms). Used so edge-drag can restore
        # trimmed audio/video even if a clip forgot source_duration after edits.
        self.media_source_duration_ms = 10_000
        self.clips: list[CaptionClip] = []
        self.video_waveform: list[float] = []
        self.bgm_waveform: list[float] = []
        self.ambient_waveform: list[float] = []
        self.tts_waveform: list[float] = []
        self.video_name = ""
        self.bgm_name = ""
        self.ambient_name = ""
        self.tts_name = ""
        self.original_audio_enabled = True
        self.transitions: list[dict] = []
        # Timed declaration layers. Each entry contains start/end and a
        # serializable text or mask layer payload used by the final ASS render.
        self.overlays: list[dict] = []
        self.transition_names: list[str] = []
        self.transition_duration_ms = 500
        self.image_overwrite_duration_ms = 1000
        self.media_clips: dict[str, list[MediaClip]] = {
            "video": [],
            "original_audio": [],
            "bgm": [],
            "ambient": [],
            "tts": [],
        }
        self.selected: tuple[str, int] | None = None
        # Drag payload: kind, index, edge, originals..., linked, ripple snapshot, ...
        self._drag: tuple | None = None
        self._scrubbing = False
        self._project_key = ""
        self._undo_stack: list[dict] = []
        self._redo_stack: list[dict] = []
        self._max_history = 40
        self._history_locked = False
        self._drag_snapshot_pushed = False
        self.setMinimumHeight(self.RULER_HEIGHT + self.TRACK_HEIGHT * 7 + 8)
        self.setMouseTracking(True)
        self.setAcceptDrops(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._update_width()

    def _update_width(self):
        # Content-only width (track name column is a fixed rail outside the scroll area).
        content = math.ceil(self.duration_ms / 1000 * self.pixels_per_second)
        self.setMinimumWidth(max(640, content + 48))
        self.updateGeometry()
        self.update()
        rail = getattr(self, "label_rail", None)
        if rail is not None:
            rail.update()

    def set_zoom(self, value: int):
        self.pixels_per_second = max(ZOOM_MIN_PPS, min(ZOOM_MAX_PPS, int(value)))
        self._update_width()

    def wheelEvent(self, event: QWheelEvent):
        """Mouse wheel zooms the timeline (frame-level when zoomed in)."""
        delta = int(event.angleDelta().y())
        if delta == 0:
            # Some devices send pixelDelta only
            delta = int(event.pixelDelta().y())
        if delta == 0:
            super().wheelEvent(event)
            return
        self.zoomWheel.emit(delta, int(event.position().x()))
        event.accept()

    def _apply_tracks_from_state(self, tracks_state: dict, full_ms: int):
        """Restore media bars from edit_state tracks (always, not only on project change)."""
        if not tracks_state:
            return
        full_ms = max(1000, int(full_ms or 0))
        for kind in self.media_clips:
            restored = []
            for item in tracks_state.get(kind, []) or []:
                try:
                    src_end = int(item["source_end"])
                    item_path=str(item.get("path", "") or "")
                    if item_path:
                        src_dur=max(int(item.get("source_duration") or 0),src_end)
                    else:
                        src_dur = max(
                            int(item.get("source_duration") or 0),
                            src_end,
                            full_ms,
                            self.media_source_duration_ms,
                        )
                    try:
                        spd = float(item.get("speed", 1.0) or 1.0)
                    except (TypeError, ValueError):
                        spd = 1.0
                    restored.append(
                        MediaClip(
                            int(item["start"]), int(item["end"]),
                            int(item["source_start"]), src_end,
                            str(item.get("name", "")),
                            source_duration=src_dur,
                            media_type=str(item.get("media_type", "video") or "video"),
                            path=item_path,
                            speed=max(0.25, min(4.0, spd)),
                        )
                    )
                except (KeyError, TypeError, ValueError):
                    continue
            for clip in restored:
                if clip.path:
                    clip.source_duration=max(clip.source_duration or 0,clip.source_end)
                else:
                    self.media_source_duration_ms = max(
                        self.media_source_duration_ms,
                        clip.resolved_source_duration(self.media_source_duration_ms),
                    )
                    clip.source_duration = max(
                        clip.source_duration or 0, self.media_source_duration_ms
                    )
            # Only replace when payload has bars for this kind (empty list clears intentionally)
            if kind in tracks_state:
                self.media_clips[kind] = restored

    def set_project(
        self,
        duration_ms: int,
        video_name: str,
        srt: str,
        bgm_name: str = "",
        tts_name: str = "",
        original_audio_enabled: bool = True,
        edit_state: dict | None = None,
        ambient_name: str = "",
    ):
        self.duration_ms = max(1000, int(duration_ms or 0))
        self.media_source_duration_ms = max(self.duration_ms, int(duration_ms or 0), 1000)
        self.video_name = str(video_name or "")
        self.bgm_name = str(bgm_name or "")
        self.ambient_name = str(ambient_name or "")
        self.tts_name = str(tts_name or "")
        self.original_audio_enabled = bool(original_audio_enabled)
        self.clips = parse_srt(srt)
        cue_end = max((clip.end for clip in self.clips), default=0)
        self.duration_ms = max(self.duration_ms, cue_end, 1000)
        self.media_source_duration_ms = max(self.media_source_duration_ms, self.duration_ms)
        project_key = str(video_name or "")
        tracks_state = (edit_state or {}).get("tracks") or {}
        is_new_project = project_key != self._project_key
        if is_new_project:
            self._project_key = project_key
            base_name = Path(project_key).name if project_key else "视频"
            full = self.media_source_duration_ms
            self.media_clips["video"] = [
                MediaClip(0, full, 0, full, base_name, source_duration=full)
            ]
            self.media_clips["original_audio"] = [
                MediaClip(0, full, 0, full, "视频原声", source_duration=full)
            ]
            self.media_clips["bgm"] = (
                [MediaClip(0, full, 0, full, Path(bgm_name).name, source_duration=full,
                           media_type="bgm", path=str(bgm_name or ""))]
                if bgm_name
                else []
            )
            self.media_clips["ambient"] = (
                [MediaClip(0, full, 0, full, Path(ambient_name).name, source_duration=full,
                           media_type="ambient", path=str(ambient_name or ""))]
                if ambient_name
                else []
            )
            self.media_clips["tts"] = (
                [MediaClip(0, full, 0, full, Path(tts_name).name, source_duration=full)]
                if tts_name
                else []
            )
            self.selected = None
            self.transitions = []
            self.overlays = []
        # 关键：同项目第二次刷新（播放器时长就绪 / 分段元数据就绪）也必须套用 tracks。
        # 以前仅在 project_key 变化时应用，导致要来回切换几次才出现分段轨。
        if tracks_state:
            self._apply_tracks_from_state(tracks_state, self.media_source_duration_ms)
            self.transitions = [
                {
                    "position": max(0, int(item.get("position", 0))),
                    "name": str(item.get("name", "")),
                    "duration_ms": max(100, int(item.get("duration_ms", 500))),
                }
                for item in (edit_state or {}).get("transitions", [])
                if item.get("name")
            ]
            self.overlays = [
                {
                    "start": max(0, int(item.get("start", 0))),
                    "end": max(80, int(item.get("end", 3000))),
                    "name": str(item.get("name", "声明叠加")),
                    "layer": dict(item.get("layer") or {}),
                }
                for item in (edit_state or {}).get("overlays", [])
                if isinstance(item, dict) and isinstance(item.get("layer"), dict)
            ]
            # 轨道状态里没有环境音时，用当前选择补一条
            if "ambient" not in tracks_state:
                self._ensure_optional_track("ambient", ambient_name)
            if "bgm" not in tracks_state:
                self._ensure_optional_track("bgm", bgm_name)
            if "tts" not in tracks_state:
                self._ensure_optional_track("tts", tts_name)
        elif not is_new_project:
            if "bgm" not in tracks_state:
                self._ensure_optional_track("bgm", bgm_name)
            if "ambient" not in tracks_state:
                self._ensure_optional_track("ambient", ambient_name)
            if "tts" not in tracks_state:
                self._ensure_optional_track("tts", tts_name)
        self.position_ms = min(self.position_ms, self.duration_ms)
        # 仅换片时清撤销栈；同项目刷新保留撤销（避免一切片就被 duration 回调冲掉）
        if is_new_project:
            self.clear_history()
        self._update_width()
        self.update()

    def set_transition_catalog(self, names: list[str], duration_ms: int = 500):
        self.transition_names = [str(name) for name in names if str(name).strip()]
        self.transition_duration_ms = max(100, int(duration_ms))

    def set_transition_duration(self, duration_ms: int):
        self.transition_duration_ms = max(100, int(duration_ms))

    def set_image_overwrite_duration(self, duration_ms: int):
        self.image_overwrite_duration_ms = max(100, int(duration_ms))

    def add_overlay(self, layer: dict, start_ms: int, end_ms: int):
        """Add a time-limited declaration overlay without touching A/V tracks."""
        if not isinstance(layer, dict) or layer.get("type") not in ("text", "mask", "image"):
            return False
        start = max(0, min(int(start_ms), self.duration_ms - 80))
        end = max(start + 80, min(int(end_ms), self.duration_ms))
        self.push_undo()
        payload = dict(layer)
        payload["enabled"] = True
        self.overlays.append(
            {
                "start": start,
                "end": end,
                "name": str(payload.get("name") or {
                    "text": "声明文字",
                    "mask": "声明蒙版",
                    "image": "PNG 声明图",
                }.get(payload.get("type"), "声明叠加")),
                "layer": payload,
            }
        )
        self.overlays.sort(key=lambda item: (int(item["start"]), int(item["end"])))
        self.selected = ("overlay", self.overlays.index(next(
            item for item in self.overlays
            if item["start"] == start and item["end"] == end and item["layer"] is payload
        )))
        self._emit_timeline_state()
        self.update()
        return True

    def update_overlay_template(self, layer: dict) -> int:
        """Synchronize geometry/style edits to clips created from this template."""
        if not isinstance(layer,dict):
            return 0
        template_id=str(layer.get("template_id",""))
        changed=0
        for item in self.overlays:
            current=dict(item.get("layer") or {})
            same_id=bool(template_id and current.get("template_id")==template_id)
            same_legacy=(
                not template_id
                and current.get("type")==layer.get("type")
                and current.get("name")==layer.get("name")
            )
            if not (same_id or same_legacy):
                continue
            # Clip timing/fades stay clip-specific; visual template fields update.
            preserved={
                key:current[key] for key in ("fade_in_ms","fade_out_ms")
                if key in current
            }
            updated={
                key:value for key,value in layer.items()
                if key!="timeline_template_only"
            }
            updated.update(preserved)
            updated["enabled"]=True
            item["layer"]=updated
            item["name"]=str(updated.get("name") or item.get("name") or "声明叠加")
            changed+=1
        if changed:
            self._emit_timeline_state()
            self.update()
        return changed

    @staticmethod
    def _is_image_path(path: str) -> bool:
        return Path(path).suffix.lower() in {
            ".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff",
        }

    @staticmethod
    def _is_video_path(path: str) -> bool:
        return Path(path).suffix.lower() in {
            ".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".mts", ".m2ts",
        }

    def _probe_media(self, path: str) -> tuple[int, bool]:
        """Return (duration_ms, has_audio) using the configured FFmpeg bundle."""
        source=Path(path)
        ffmpeg=Path(str(getattr(self,"ffmpeg_path","ffmpeg") or "ffmpeg"))
        ffprobe=ffmpeg.with_name("ffprobe"+ffmpeg.suffix)
        probe_cmd=str(ffprobe) if ffprobe.is_file() else "ffprobe"
        try:
            result=subprocess.run(
                [probe_cmd,"-v","error","-show_entries","format=duration",
                 "-show_entries","stream=codec_type","-of","json",str(source)],
                stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,
                encoding="utf-8",errors="replace",
                creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess,"CREATE_NO_WINDOW") else 0,
            )
            if result.returncode==0:
                import json
                data=json.loads(result.stdout or "{}")
                duration=max(80,round(float((data.get("format") or {}).get("duration") or 0)*1000))
                has_audio=any(item.get("codec_type")=="audio" for item in data.get("streams",[]))
                return duration,has_audio
        except Exception:
            pass
        try:
            result=subprocess.run(
                [str(ffmpeg),"-hide_banner","-i",str(source)],
                stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,
                encoding="utf-8",errors="replace",
                creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess,"CREATE_NO_WINDOW") else 0,
            )
            text=result.stderr or ""
            match=re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)",text)
            if match:
                seconds=int(match.group(1))*3600+int(match.group(2))*60+float(match.group(3))
                return max(80,round(seconds*1000)),bool(re.search(r"Stream\s+#.*Audio:",text))
        except Exception:
            pass
        return 0,False

    def insert_video_clip(self, video_path: str, position: int):
        """Insert external video and its own audio as a locked A/V pair."""
        source=Path(str(video_path or ""))
        if not source.is_file() or not self._is_video_path(str(source)):
            return False
        source_duration,has_audio=self._probe_media(str(source))
        if source_duration<80:
            return False
        position=max(0,min(int(position),self.duration_ms))
        self.push_undo()
        # Split the main picture and its linked original audio at the insertion point.
        for index,clip in enumerate(list(self.media_clips.get("video",[]))):
            if clip.start+80<position<clip.end-80:
                self._split_clip("video",index,position)
                break
        # Ripple only the video and original-audio pair. Captions/BGM/TTS remain
        # independently editable, matching the existing track-lock semantics.
        for kind in ("video","original_audio"):
            for clip in self.media_clips.get(kind,[]):
                if clip.start>=position-2:
                    clip.start+=source_duration
                    clip.end+=source_duration
        inserted=MediaClip(
            position,position+source_duration,0,source_duration,source.name,
            source_duration,"external_video",str(source.resolve()),
        )
        self.media_clips["video"].append(inserted)
        self.media_clips["video"].sort(key=lambda clip:(clip.start,clip.end))
        if has_audio:
            self.media_clips["original_audio"].append(
                MediaClip(
                    position,position+source_duration,0,source_duration,
                    f"{source.name} · 音频",source_duration,
                    "external_audio",str(source.resolve()),
                )
            )
            self.media_clips["original_audio"].sort(key=lambda clip:(clip.start,clip.end))
        self.duration_ms+=source_duration
        self.selected=("video",self.media_clips["video"].index(inserted))
        self._emit_timeline_state()
        self._update_width()
        self.update()
        return True

    def overwrite_cut_with_image(self, image_path: str, position: int):
        """Overwrite existing picture time around an edit point without growing the timeline."""
        image = Path(str(image_path or ""))
        if not image.is_file() or not self._is_image_path(str(image)):
            return False
        video_clips = self.media_clips.get("video", [])
        if not video_clips:
            return False
        boundaries = sorted(
            {int(clip.end) for clip in video_clips[:-1]}
            | {int(clip.start) for clip in video_clips[1:]}
        )
        position = max(0, min(int(position), self.duration_ms))
        if boundaries:
            position = min(boundaries, key=lambda value: abs(value - position))
        duration = min(
            max(100, int(self.image_overwrite_duration_ms)),
            max(100, self.duration_ms),
        )
        start = max(0, position - duration // 2)
        end = min(self.duration_ms, start + duration)
        start = max(0, end - duration)
        if end - start < 80:
            return False

        self.push_undo()
        kept: list[MediaClip] = []
        for clip in video_clips:
            src_dur = clip.resolved_source_duration()
            if clip.end <= start or clip.start >= end:
                kept.append(clip)
                continue
            if clip.start < start:
                left_source_end = clip.source_start + (start - clip.start)
                kept.append(
                    MediaClip(
                        clip.start, start, clip.source_start, left_source_end,
                        clip.name, src_dur, clip.media_type, clip.path,
                    )
                )
            if clip.end > end:
                right_source_start = clip.source_start + (end - clip.start)
                kept.append(
                    MediaClip(
                        end, clip.end, right_source_start, clip.source_end,
                        clip.name, src_dur, clip.media_type, clip.path,
                    )
                )
        kept.append(
            MediaClip(
                start, end, 0, end - start, image.name, end - start,
                "image", str(image.resolve()),
            )
        )
        kept.sort(key=lambda clip: (clip.start, clip.end))
        self.media_clips["video"] = kept
        # 覆盖编辑只替换画面。视频原声音轨保持原始起止和连续声音，
        # 不切开、不静音，也不随着图片片段向后移动。
        self.selected = (
            "video",
            next(index for index, clip in enumerate(kept) if clip.media_type == "image"
                 and clip.start == start and clip.end == end),
        )
        self._emit_timeline_state()
        self.update()
        return True

    def add_transition(self, name: str, position: int):
        name = str(name or "").strip()
        if not name:
            return
        position = max(80, min(int(position), self.duration_ms - 80))
        boundaries = sorted(
            {clip.end for clip in self.media_clips["video"][:-1]}
            | {clip.start for clip in self.media_clips["video"][1:]}
        )
        if boundaries:
            position = min(boundaries, key=lambda value: abs(value - position))
        else:
            for index, clip in enumerate(self.media_clips["video"]):
                if clip.start + 80 < position < clip.end - 80:
                    self._split_clip("video", index, position)
                    break
        marker = {
            "position": position,
            "name": name,
            "duration_ms": self.transition_duration_ms,
        }
        self.transitions = [
            item for item in self.transitions
            if abs(int(item.get("position", 0)) - position) > 40
        ]
        self.transitions.append(marker)
        self.transitions.sort(key=lambda item: int(item["position"]))
        self._emit_timeline_state()
        self.update()

    def _split_clip(self, kind: str, index: int, cut: int) -> bool:
        tracks = self.media_clips.get(kind, [])
        if not (0 <= index < len(tracks)):
            return False
        clip = tracks[index]
        # 允许更靠近两端（约 1 帧～50ms），旧版 80ms 在短段上几乎切不动
        edge = 50
        if cut <= clip.start + edge or cut >= clip.end - edge:
            return False
        spd = max(0.25, min(4.0, float(getattr(clip, "speed", 1.0) or 1.0)))
        source_cut = clip.source_start + int(round((cut - clip.start) * spd))
        source_cut = max(clip.source_start + 40, min(clip.source_end - 40, source_cut))
        src_dur = clip.resolved_source_duration()
        tracks[index:index + 1] = [
            MediaClip(
                clip.start, cut, clip.source_start, source_cut, clip.name, src_dur,
                clip.media_type, clip.path, speed=spd,
            ),
            MediaClip(
                cut, clip.end, source_cut, clip.source_end, clip.name, src_dur,
                clip.media_type, clip.path, speed=spd,
            ),
        ]
        if kind == "video":
            self._split_linked_original_audio(cut)
        return True

    def set_selected_video_speed(self, speed: float) -> bool:
        """Set playback speed on the selected video clip; timeline length = source/speed."""
        if not self.selected or self.selected[0] != "video":
            return False
        kind, index = self.selected
        tracks = self.media_clips.get("video", [])
        if not (0 <= index < len(tracks)):
            return False
        clip = tracks[index]
        if str(clip.media_type or "video") == "image":
            return False
        new_speed = max(0.25, min(4.0, float(speed or 1.0)))
        old_speed = max(0.25, min(4.0, float(getattr(clip, "speed", 1.0) or 1.0)))
        if abs(new_speed - old_speed) < 0.001:
            return False
        self.push_undo()
        old_end = int(clip.end)
        new_span = clip.timeline_span_for_speed(new_speed)
        new_end = int(clip.start) + new_span
        delta = new_end - old_end
        clip.speed = new_speed
        clip.end = new_end
        # Ripple following video clips (and linked original audio loosely via duration)
        if abs(delta) >= 1:
            for other in tracks:
                if other is clip:
                    continue
                if int(other.start) >= old_end - 5:
                    other.start = int(other.start) + delta
                    other.end = int(other.end) + delta
            # Keep original_audio clip bounds aligned if 1:1 with video segments when possible
            for ac in self.media_clips.get("original_audio", []):
                if int(ac.start) >= old_end - 5:
                    ac.start = int(ac.start) + delta
                    ac.end = int(ac.end) + delta
            self._grow_timeline_if_needed(max(
                (int(c.end) for c in tracks),
                default=self.duration_ms,
            ))
            if delta < 0:
                # Shrink duration_ms if trailing empty
                max_end = max((int(c.end) for c in tracks), default=1000)
                self.duration_ms = max(1000, max(self.duration_ms + delta, max_end + 200))
                self._update_width()
        self._emit_timeline_state()
        self.update()
        return True

    def _ensure_optional_track(self, kind: str, name: str):
        if name and not self.media_clips[kind]:
            full = self.duration_ms
            self.media_clips[kind] = [
                MediaClip(0, full, 0, full, Path(name).name, source_duration=full)
            ]
        elif not name:
            self.media_clips[kind] = []

    def set_srt(self, srt: str):
        self.clips = parse_srt(srt)
        cue_end = max((clip.end for clip in self.clips), default=0)
        # 标尺对齐素材时长：短字幕轴不缩短时间线；仅当字幕超出片长时才拉长
        base = max(int(self.media_source_duration_ms or 0), int(self.duration_ms or 0), 1000)
        self.duration_ms = max(base, cue_end, 1000)
        self.media_source_duration_ms = max(int(self.media_source_duration_ms or 0), self.duration_ms)
        self._update_width()
        self.update()

    def set_position(self, milliseconds: int):
        value = max(0, min(int(milliseconds), self.duration_ms))
        if value != self.position_ms:
            self.position_ms = value
            self.update()

    def set_waveform(self, kind: str, values: list[float]):
        if kind == "bgm":
            self.bgm_waveform = values
        elif kind == "ambient":
            self.ambient_waveform = values
        elif kind == "tts":
            self.tts_waveform = values
        else:
            self.video_waveform = values
        self.update()

    def set_original_audio_enabled(self, enabled: bool):
        self.original_audio_enabled = bool(enabled)
        self._emit_timeline_state()
        self.update()
        rail = getattr(self, "label_rail", None)
        if rail is not None:
            rail.update()

    def _x(self, milliseconds: int) -> float:
        # Timeline content starts at x=0; track names live in the fixed left rail.
        return max(0.0, float(milliseconds)) / 1000.0 * self.pixels_per_second

    def _ms(self, x: float) -> int:
        return max(
            0,
            min(
                self.duration_ms,
                round(float(x) / max(1e-6, self.pixels_per_second) * 1000),
            ),
        )

    def track_label_rows(self):
        """Rows for the fixed left name rail: (title, detail)."""
        return (
            ("视频轨道", self.video_name),
            ("视频原声", "已静音（独立）" if not self.original_audio_enabled else "原片声音·独立"),
            ("字幕时间块", ""),
            ("声明叠加", "文字 / 蒙版 / PNG"),
            ("BGM 伴奏", self.bgm_name or "背景音乐"),
            ("环境音", self.ambient_name or "氛围音·独立于原声"),
            ("文字配音", self.tts_name),
        )

    def _caption_y(self) -> int:
        return self.RULER_HEIGHT + self.TRACK_HEIGHT * 2

    @staticmethod
    def _track_index(kind: str) -> int:
        return {
            "video": 0, "original_audio": 1, "caption": 2,
            "overlay": 3, "bgm": 4, "ambient": 5, "tts": 6,
        }[kind]

    def _track_y(self, kind: str) -> int:
        return self.RULER_HEIGHT + self._track_index(kind) * self.TRACK_HEIGHT

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor("#111318"))

        # Track row backgrounds (full content width; names are on the fixed left rail).
        for index in range(7):
            y = self.RULER_HEIGHT + index * self.TRACK_HEIGHT
            painter.fillRect(
                0,
                y,
                self.width(),
                self.TRACK_HEIGHT - 1,
                QColor("#161922") if index % 2 == 0 else QColor("#13161d"),
            )
        # Ruler band
        painter.fillRect(0, 0, self.width(), self.RULER_HEIGHT, QColor("#14171e"))

        painter.setPen(QColor("#87909f"))
        painter.setFont(QFont("Consolas", 8))
        pps = self.pixels_per_second
        # Finer ticks when zoomed in so frame-level edits are readable.
        if pps >= 900:
            step_ms = 40          # ~1 frame @ 25fps
        elif pps >= 450:
            step_ms = 100         # 0.1s
        elif pps >= 200:
            step_ms = 200
        elif pps >= 100:
            step_ms = 500
        elif pps >= 52:
            step_ms = 1000
        elif pps >= 28:
            step_ms = 2000
        else:
            step_ms = 5000
        total_ms = max(step_ms, self.duration_ms)
        for ms in range(0, total_ms + step_ms, step_ms):
            x = self._x(ms)
            major = (ms % max(1000, step_ms * 5) == 0) or step_ms >= 1000
            painter.drawLine(QPointF(x, 14 if major else 20), QPointF(x, self.height()))
            if major or step_ms <= 100:
                seconds, millis = divmod(ms, 1000)
                minutes, seconds = divmod(seconds, 60)
                if step_ms < 1000:
                    label = f"{minutes}:{seconds:02d}.{millis:03d}"
                else:
                    label = f"{minutes}:{seconds:02d}"
                painter.drawText(int(x + 4), 17, label)

        self._draw_media_track(painter, "video", QColor("#334b73"), QColor("#73a7f5"), [])
        for transition in self.transitions:
            x = self._x(int(transition.get("position", 0)))
            y = self._track_y("video") + 22
            painter.setBrush(QColor("#f59e0b"))
            painter.setPen(QPen(QColor("#fff7d6"), 1))
            painter.drawPolygon(
                [
                    QPointF(x, y - 10), QPointF(x + 10, y),
                    QPointF(x, y + 10), QPointF(x - 10, y),
                ]
            )
            painter.setPen(QColor("#fef3c7"))
            painter.drawText(
                int(x + 12), int(y + 4),
                painter.fontMetrics().elidedText(
                    str(transition.get("name", "")),
                    Qt.TextElideMode.ElideRight, 90,
                ),
            )
        self._draw_media_track(
            painter,
            "original_audio",
            QColor("#30445f") if self.original_audio_enabled else QColor("#3f3237"),
            QColor("#7eb4e8") if self.original_audio_enabled else QColor("#8b5a66"),
            self.video_waveform if self.original_audio_enabled else [],
        )

        caption_y = self._caption_y() + 4
        caption_height = self.TRACK_HEIGHT - 8
        for index, clip in enumerate(self.clips):
            left, right = self._x(clip.start), self._x(clip.end)
            width = max(4.0, right - left)
            selected = self.selected == ("caption", index)
            painter.setPen(QPen(QColor("#e2dcff") if selected else QColor("#b7a7ff"), 2 if selected else 1))
            painter.setBrush(QColor("#765fd1"))
            painter.drawRoundedRect(int(left), caption_y, int(width), caption_height, 5, 5)
            # Edge handles for trim; middle drag moves the whole subtitle block
            handle_w = max(5, min(10, int(width // 5)))
            painter.fillRect(int(left), caption_y, handle_w, caption_height, QColor("#e2dcff"))
            painter.fillRect(int(right - handle_w), caption_y, handle_w, caption_height, QColor("#e2dcff"))
            painter.setPen(QColor("#ffffff"))
            painter.setFont(QFont("Microsoft YaHei UI", 8))
            text = " ".join(clip.text.splitlines())
            painter.drawText(
                int(left + handle_w + 4),
                caption_y + 22,
                painter.fontMetrics().elidedText(
                    text, Qt.TextElideMode.ElideRight, max(0, int(width - handle_w * 2 - 8))
                ),
            )

        overlay_y = self._track_y("overlay") + 4
        for index, item in enumerate(self.overlays):
            left, right = self._x(int(item.get("start", 0))), self._x(int(item.get("end", 0)))
            width = max(4, int(right - left))
            selected = self.selected == ("overlay", index)
            layer_type = str((item.get("layer") or {}).get("type", "text"))
            fill = QColor({
                "text": "#0f766e",
                "mask": "#7c2d92",
                "image": "#b45309",
            }.get(layer_type, "#475569"))
            painter.setPen(QPen(QColor("#f8fafc") if selected else fill.lighter(145), 2 if selected else 1))
            painter.setBrush(fill)
            painter.drawRoundedRect(int(left), overlay_y, width, self.TRACK_HEIGHT - 8, 5, 5)
            if selected and width >= 16:
                handle_w = max(5, min(9, width // 6))
                painter.fillRect(int(left), overlay_y, handle_w, self.TRACK_HEIGHT - 8, QColor("#f8fafc"))
                painter.fillRect(int(right - handle_w), overlay_y, handle_w, self.TRACK_HEIGHT - 8, QColor("#f8fafc"))
            painter.setPen(QColor("#ffffff"))
            title = str(item.get("name") or {
                "text": "声明文字",
                "mask": "声明蒙版",
                "image": "PNG 声明图",
            }.get(layer_type, "声明叠加"))
            painter.drawText(
                int(left + 8), overlay_y + 23,
                painter.fontMetrics().elidedText(title, Qt.TextElideMode.ElideRight, max(0, width - 16)),
            )

        self._draw_media_track(painter, "bgm", QColor("#275f50"), QColor("#6ee7b7"), self.bgm_waveform)
        self._draw_media_track(painter, "ambient", QColor("#1e3a5f"), QColor("#7dd3fc"), self.ambient_waveform)
        self._draw_media_track(painter, "tts", QColor("#68461f"), QColor("#f6b95f"), self.tts_waveform)

        playhead_x = self._x(self.position_ms)
        painter.setPen(QPen(QColor("#f43f5e"), 2))
        painter.drawLine(QPointF(playhead_x, 0), QPointF(playhead_x, self.height()))
        painter.setBrush(QColor("#f43f5e"))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawPolygon(
            [QPointF(playhead_x - 6, 0), QPointF(playhead_x + 6, 0), QPointF(playhead_x, 9)]
        )

    def _draw_media_track(
        self,
        painter: QPainter,
        kind: str,
        fill: QColor,
        waveform_color: QColor,
        waveform: list[float],
    ):
        y = self._track_y(kind) + 4
        for index, clip in enumerate(self.media_clips.get(kind, [])):
            left, right = self._x(clip.start), self._x(clip.end)
            width = max(4, int(right - left))
            selected = self.selected == (kind, index)
            clip_fill = (
                QColor("#7c3aed") if kind == "video" and clip.media_type == "image"
                else QColor("#0369a1") if clip.media_type in ("external_video","external_audio")
                else fill
            )
            painter.setPen(QPen(QColor("#f8fafc") if selected else clip_fill.lighter(135), 2 if selected else 1))
            painter.setBrush(clip_fill)
            painter.drawRoundedRect(int(left), y, width, self.TRACK_HEIGHT - 8, 5, 5)
            painter.setPen(QColor("#eef2ff"))
            painter.drawText(
                int(left + 8),
                y + 14,
                painter.fontMetrics().elidedText(
                    (
                        f"图片覆盖 · {clip.name}" if clip.media_type == "image"
                        else f"插入视频 · {clip.name}" if clip.media_type == "external_video"
                        else f"视频音频 · {clip.name}" if clip.media_type == "external_audio"
                        else (clip.name or kind)
                    ),
                    Qt.TextElideMode.ElideRight, max(0, width - 16)
                ),
            )
            # Edge handles: drag left/right edges to trim or restore source content
            if selected and width >= 16:
                handle_w = max(4, min(8, width // 6))
                painter.fillRect(int(left), y, handle_w, self.TRACK_HEIGHT - 8, QColor("#f8fafc"))
                painter.fillRect(int(right - handle_w), y, handle_w, self.TRACK_HEIGHT - 8, QColor("#f8fafc"))
            if waveform:
                self._draw_waveform_segment(
                    painter, waveform, clip, y + 25, waveform_color
                )

    def _draw_waveform_segment(
        self, painter: QPainter, values: list[float], clip: MediaClip, center_y: int, color: QColor
    ):
        if not values:
            return
        left, right = self._x(clip.start), self._x(clip.end)
        path = QPainterPath(QPointF(left, center_y))
        sample_count = max(1, len(values) - 1)
        for index, value in enumerate(values):
            x = left + (right - left) * index / sample_count
            amplitude = min(1.0, abs(float(value))) * 9
            path.lineTo(x, center_y - amplitude)
            path.lineTo(x, center_y + amplitude)
        painter.setPen(QPen(color, 1))
        painter.drawPath(path)

    def _draw_waveform(self, painter: QPainter, values: list[float], center_y: int, color: QColor):
        if not values:
            painter.setPen(QPen(color.darker(150), 1))
            painter.drawLine(
                QPointF(self._x(0), center_y), QPointF(self._x(self.duration_ms), center_y)
            )
            return
        left, right = self._x(0), self._x(self.duration_ms)
        path = QPainterPath(QPointF(left, center_y))
        for index, value in enumerate(values):
            x = left + (right - left) * index / max(1, len(values) - 1)
            amplitude = min(1.0, abs(float(value))) * 16
            path.lineTo(x, center_y - amplitude)
            path.lineTo(x, center_y + amplitude)
        painter.setPen(QPen(color, 1))
        painter.drawPath(path)

    def _edge_hit_px(self) -> float:
        """Wider edge handles so trim is easy even when zoomed out."""
        return float(max(12, min(22, int(self.pixels_per_second * 0.12))))

    def _hit_media_edge(self, x: float, left: float, right: float) -> str:
        edge = self._edge_hit_px()
        width = right - left
        # Tiny clips: prefer move unless very close to an edge.
        if width <= edge * 2.5:
            if abs(x - left) <= edge * 0.6:
                return "start"
            if abs(x - right) <= edge * 0.6:
                return "end"
            if left - 2 <= x <= right + 2:
                return "move"
            return ""
        if abs(x - left) <= edge:
            return "start"
        if abs(x - right) <= edge:
            return "end"
        if left <= x <= right:
            return "move"
        return ""

    def _grow_timeline_if_needed(self, end_ms: int):
        end_ms = max(0, int(end_ms))
        if end_ms > self.duration_ms:
            self.duration_ms = end_ms + 200  # small tail room
            self._update_width()

    def _collect_ripple_after(
        self,
        after_ms: int,
        exclude: set[tuple[str, int]] | None = None,
        scope: str = "media",
    ) -> list[tuple[str, int, int, int]]:
        """Snapshot clips at/after after_ms for ripple push.

        scope:
          - \"media\": video / original_audio / bgm / tts only（不碰字幕）
          - \"caption\": 字幕轨 internally only（不碰音视频）
        专业剪辑里字幕与 A/V 默认解绑：改字幕时长不会推动配音，改画面也不会推字幕。
        """
        exclude = exclude or set()
        items: list[tuple[str, int, int, int]] = []
        threshold = int(after_ms) - 2  # tiny tolerance for float/int rounding
        scope = str(scope or "media").lower()
        if scope == "media":
            for kind in ("video", "original_audio", "bgm", "ambient", "tts"):
                for index, clip in enumerate(self.media_clips.get(kind, [])):
                    if (kind, index) in exclude:
                        continue
                    if int(clip.start) >= threshold:
                        items.append((kind, index, int(clip.start), int(clip.end)))
        elif scope == "caption":
            for index, clip in enumerate(self.clips):
                if ("caption", index) in exclude:
                    continue
                if int(clip.start) >= threshold:
                    items.append(("caption", index, int(clip.start), int(clip.end)))
        return items

    def _apply_ripple_shift(
        self,
        ripple_items: list[tuple[str, int, int, int]],
        delta_ms: int,
    ):
        """Shift snapshotted clips by delta_ms (timeline only; source in/out unchanged)."""
        if not ripple_items or not delta_ms:
            return
        max_end = 0
        for kind, index, o_start, o_end in ripple_items:
            new_start = max(0, int(o_start) + int(delta_ms))
            new_end = max(new_start + 80, int(o_end) + int(delta_ms))
            if kind == "caption":
                if 0 <= index < len(self.clips):
                    self.clips[index].start = new_start
                    self.clips[index].end = new_end
                    max_end = max(max_end, new_end)
            else:
                tracks = self.media_clips.get(kind, [])
                if 0 <= index < len(tracks):
                    tracks[index].start = new_start
                    tracks[index].end = new_end
                    max_end = max(max_end, new_end)
        if max_end:
            self._grow_timeline_if_needed(max_end)

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        x, y = event.position().x(), event.position().y()
        ms = self._ms(x)
        caption_top = self._caption_y()
        self._drag_snapshot_pushed = False
        if caption_top <= y <= caption_top + self.TRACK_HEIGHT:
            for index, clip in enumerate(self.clips):
                left, right = self._x(clip.start), self._x(clip.end)
                edge = self._hit_media_edge(x, left, right)
                if not edge:
                    continue
                self.push_undo()
                self._drag_snapshot_pushed = True
                self.selected = ("caption", index)
                grab = ms - clip.start
                # 仅涟漪后续字幕，绝不推动音视频（否则对白对不齐）
                ripple = ()
                if edge == "end":
                    ripple = tuple(
                        self._collect_ripple_after(
                            clip.end,
                            exclude={("caption", index)},
                            scope="caption",
                        )
                    )
                self._drag = (
                    "caption", index, edge, clip.start, clip.end, 0, 0, grab, 0,
                    (), ripple,
                )
                if edge == "move":
                    self.seekRequested.emit(clip.start)
                self.update()
                return
        overlay_top = self._track_y("overlay")
        if overlay_top <= y <= overlay_top + self.TRACK_HEIGHT:
            for index, item in enumerate(self.overlays):
                left = self._x(int(item.get("start", 0)))
                right = self._x(int(item.get("end", 0)))
                edge = self._hit_media_edge(x, left, right)
                if not edge:
                    continue
                self.push_undo()
                self._drag_snapshot_pushed = True
                self.selected = ("overlay", index)
                start = int(item.get("start", 0)); end = int(item.get("end", start + 80))
                self._drag = (
                    "overlay", index, edge, start, end, 0, 0, ms - start, 0, (), (),
                )
                self.seekRequested.emit(ms)
                self.update()
                return
        for kind in ("video", "original_audio", "bgm", "ambient", "tts"):
            top = self._track_y(kind)
            if not (top <= y <= top + self.TRACK_HEIGHT):
                continue
            for index, clip in enumerate(self.media_clips.get(kind, [])):
                left, right = self._x(clip.start), self._x(clip.end)
                edge = self._hit_media_edge(x, left, right)
                if not edge:
                    continue
                self.push_undo()
                self._drag_snapshot_pushed = True
                self.selected = (kind, index)
                grab = ms - clip.start
                # Always use full media length so over-trimmed audio/video can be dragged back.
                if clip.path:
                    src_dur=max(clip.resolved_source_duration(),clip.source_end)
                else:
                    src_dur = max(
                        clip.resolved_source_duration(self.media_source_duration_ms),
                        self.media_source_duration_ms,
                        clip.source_end,
                    )
                clip.source_duration = src_dur
                # Video edge drag also drives co-aligned original_audio (same start/end).
                linked_audio = []
                linked_video = []
                if kind == "video":
                    for ai, aclip in enumerate(self.media_clips.get("original_audio", [])):
                        if aclip.start == clip.start and aclip.end == clip.end:
                            aclip.source_duration = max(
                                aclip.resolved_source_duration(src_dur), src_dur
                            )
                            linked_audio.append(ai)
                elif kind == "original_audio":
                    # Keep paired video bar in lockstep when present
                    for vi, vclip in enumerate(self.media_clips.get("video", [])):
                        if vclip.start == clip.start and vclip.end == clip.end:
                            vclip.source_duration = max(
                                vclip.resolved_source_duration(src_dur), src_dur
                            )
                            linked_video.append(vi)
                exclude = {(kind, index)}
                for ai in linked_audio:
                    exclude.add(("original_audio", ai))
                for vi in linked_video:
                    exclude.add(("video", vi))
                # 拉长/缩短右边缘：只推动后续音视频轨，字幕独立（方便单独对齐语音）
                ripple = ()
                if edge == "end":
                    ripple = tuple(
                        self._collect_ripple_after(
                            clip.end, exclude=exclude, scope="media",
                        )
                    )
                self._drag = (
                    kind, index, edge, clip.start, clip.end,
                    clip.source_start, clip.source_end, grab, src_dur,
                    tuple(linked_audio), ripple, tuple(linked_video),
                )
                self.seekRequested.emit(ms)
                self.update()
                return
        if x >= 0:
            self._scrubbing = True
            self.seekRequested.emit(ms)

    def mouseMoveEvent(self, event):
        x = event.position().x()
        y = event.position().y()
        if self._scrubbing:
            self.seekRequested.emit(self._ms(x))
            return
        if not self._drag:
            # Hover cursor feedback
            self._update_hover_cursor(x, y)
            return
        # Support caption / media tuples with optional ripple snapshot
        drag = self._drag
        kind = drag[0]
        index = drag[1]
        edge = drag[2]
        original_start = drag[3]
        original_end = drag[4]
        original_source_start = drag[5]
        original_source_end = drag[6]
        grab_ms = drag[7]
        original_source_dur = drag[8]
        linked_audio = drag[9] if len(drag) > 9 else ()
        ripple_items = list(drag[10]) if len(drag) > 10 else []
        linked_video = drag[11] if len(drag) > 11 else ()
        value = self._ms(x)
        min_len = 80
        if kind in ("caption", "overlay"):
            target_items = self.clips if kind == "caption" else self.overlays
            if not (0 <= index < len(target_items)):
                return
            clip = target_items[index]
            clip_start = int(clip.start if kind == "caption" else clip.get("start", 0))
            clip_end = int(clip.end if kind == "caption" else clip.get("end", clip_start + min_len))
            if edge == "start":
                lower = 0
                new_start = max(lower, min(value, clip_end - min_len))
                if kind == "caption":
                    clip.start = new_start
                else:
                    clip["start"] = new_start
            elif edge == "end":
                new_end = max(value, clip_start + min_len)
                if kind == "caption":
                    clip.end = new_end
                    ripple_delta = clip.end - original_end
                    self._apply_ripple_shift(ripple_items, ripple_delta)
                else:
                    clip["end"] = new_end
                self._grow_timeline_if_needed(new_end)
            else:  # move whole subtitle / declaration block
                length = original_end - original_start
                new_start = max(0, value - int(grab_ms))
                if kind == "caption":
                    # Soft neighbor clamps (allow slight overlap prevention)
                    if index > 0:
                        new_start = max(new_start, self.clips[index - 1].end)
                    if index + 1 < len(self.clips):
                        new_start = min(new_start, self.clips[index + 1].start - length)
                    clip.start = max(0, new_start)
                    clip.end = clip.start + length
                    new_end = clip.end
                else:
                    clip["start"] = max(0, new_start)
                    clip["end"] = clip["start"] + length
                    new_end = clip["end"]
                self._grow_timeline_if_needed(new_end)
        else:
            tracks = self.media_clips.get(kind, [])
            if not (0 <= index < len(tracks)):
                return
            clip = tracks[index]
            src_dur = max(
                int(original_source_dur or 0),
                int(self.media_source_duration_ms or 0),
                original_source_end,
                80,
            )
            clip.source_duration = max(clip.source_duration or 0, src_dur)

            def apply_edge(target: MediaClip, o_start, o_end, o_ss, o_se):
                if edge == "start":
                    # Pull left to restore (source_start ↓) or trim in (source_start ↑).
                    delta = value - o_start
                    min_delta = -o_ss
                    max_delta = o_end - o_start - min_len
                    if o_start + delta < 0:
                        delta = -o_start
                    delta = max(min_delta, min(delta, max_delta))
                    target.start = o_start + delta
                    target.source_start = o_ss + delta
                elif edge == "end":
                    # Pull right to restore up to full source file length.
                    delta = value - o_end
                    remaining = max(0, src_dur - o_se)
                    min_delta = -(o_end - o_start - min_len)
                    delta = max(min_delta, min(delta, remaining))
                    target.end = o_end + delta
                    target.source_end = o_se + delta
                    target.source_duration = max(target.source_duration or 0, src_dur)
                    self._grow_timeline_if_needed(target.end)
                else:
                    length = o_end - o_start
                    new_start = max(0, value - int(grab_ms))
                    target.start = new_start
                    target.end = new_start + length
                    self._grow_timeline_if_needed(target.end)
                target.source_duration = max(target.source_duration or 0, src_dur)

            apply_edge(
                clip, original_start, original_end,
                original_source_start, original_source_end,
            )
            # Keep video + 视频原声 locked together when they started aligned.
            if kind == "video" and linked_audio:
                audio_tracks = self.media_clips.get("original_audio", [])
                for ai in linked_audio:
                    if 0 <= ai < len(audio_tracks):
                        aclip = audio_tracks[ai]
                        apply_edge(
                            aclip, original_start, original_end,
                            original_source_start, original_source_end,
                        )
            elif kind == "original_audio" and linked_video:
                video_tracks = self.media_clips.get("video", [])
                for vi in linked_video:
                    if 0 <= vi < len(video_tracks):
                        vclip = video_tracks[vi]
                        apply_edge(
                            vclip, original_start, original_end,
                            original_source_start, original_source_end,
                        )
            elif kind == "original_audio":
                clip.source_duration = max(clip.source_duration or 0, src_dur)
            # 拉长/缩短右边缘：后续音视频与字幕整体推移，不重叠
            if edge == "end":
                ripple_delta = int(clip.end) - int(original_end)
                self._apply_ripple_shift(ripple_items, ripple_delta)
        self.update()

    def _update_hover_cursor(self, x: float, y: float):
        caption_top = self._caption_y()
        if caption_top <= y <= caption_top + self.TRACK_HEIGHT:
            for clip in self.clips:
                left, right = self._x(clip.start), self._x(clip.end)
                edge = self._hit_media_edge(x, left, right)
                if edge in ("start", "end"):
                    self.setCursor(Qt.CursorShape.SizeHorCursor)
                    return
                if edge == "move":
                    self.setCursor(Qt.CursorShape.SizeAllCursor)
                    return
        overlay_top = self._track_y("overlay")
        if overlay_top <= y <= overlay_top + self.TRACK_HEIGHT:
            for item in self.overlays:
                edge = self._hit_media_edge(
                    x, self._x(int(item.get("start", 0))), self._x(int(item.get("end", 0)))
                )
                if edge in ("start", "end"):
                    self.setCursor(Qt.CursorShape.SizeHorCursor)
                    return
                if edge == "move":
                    self.setCursor(Qt.CursorShape.SizeAllCursor)
                    return
        for kind in ("video", "original_audio", "bgm", "ambient", "tts"):
            top = self._track_y(kind)
            if not (top <= y <= top + self.TRACK_HEIGHT):
                continue
            for clip in self.media_clips.get(kind, []):
                left, right = self._x(clip.start), self._x(clip.end)
                edge = self._hit_media_edge(x, left, right)
                if edge in ("start", "end"):
                    self.setCursor(Qt.CursorShape.SizeHorCursor)
                    return
                if edge == "move":
                    self.setCursor(Qt.CursorShape.SizeAllCursor)
                    return
        self.unsetCursor()

    def mouseReleaseEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        self._scrubbing = False
        if self._drag:
            kind = self._drag[0]
            self._drag = None
            if kind == "caption":
                self.srtChanged.emit(write_srt(self.clips))
            else:
                self._emit_timeline_state()
            self._update_width()
            self.update()

    def dragEnterEvent(self, event):
        has_image = any(
            url.isLocalFile() and self._is_image_path(url.toLocalFile())
            for url in event.mimeData().urls()
        )
        has_video = any(
            url.isLocalFile() and self._is_video_path(url.toLocalFile())
            for url in event.mimeData().urls()
        )
        if event.mimeData().hasFormat(TransitionPresetButton.MIME_TYPE) or has_image or has_video:
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        has_image = any(
            url.isLocalFile() and self._is_image_path(url.toLocalFile())
            for url in event.mimeData().urls()
        )
        has_video = any(
            url.isLocalFile() and self._is_video_path(url.toLocalFile())
            for url in event.mimeData().urls()
        )
        if event.mimeData().hasFormat(TransitionPresetButton.MIME_TYPE) or has_image or has_video:
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        video_path = next(
            (
                url.toLocalFile() for url in event.mimeData().urls()
                if url.isLocalFile() and self._is_video_path(url.toLocalFile())
            ),
            "",
        )
        if video_path:
            if self.insert_video_clip(video_path,self._ms(event.position().x())):
                event.acceptProposedAction()
            else:
                event.ignore()
            return
        image_path = next(
            (
                url.toLocalFile() for url in event.mimeData().urls()
                if url.isLocalFile() and self._is_image_path(url.toLocalFile())
            ),
            "",
        )
        if image_path:
            if self.overwrite_cut_with_image(image_path, self._ms(event.position().x())):
                event.acceptProposedAction()
            else:
                event.ignore()
            return
        if not event.mimeData().hasFormat(TransitionPresetButton.MIME_TYPE):
            return event.ignore()
        name = bytes(event.mimeData().data(TransitionPresetButton.MIME_TYPE)).decode(
            "utf-8", "replace"
        )
        self.push_undo()
        self.add_transition(name, self._ms(event.position().x()))
        event.acceptProposedAction()

    def contextMenuEvent(self, event):
        position = self._ms(event.pos().x())
        y = event.pos().y()
        menu = QMenu(self)
        # 声明叠加轨：右键删除 PNG/文字/蒙版块
        overlay_top = self._track_y("overlay")
        hit_overlay = None
        hit_overlay_index = -1
        if overlay_top <= y <= overlay_top + self.TRACK_HEIGHT:
            for index, item in enumerate(self.overlays):
                left = self._x(int(item.get("start", 0)))
                right = self._x(int(item.get("end", 0)))
                if left - 2 <= event.pos().x() <= right + 2:
                    hit_overlay = item
                    hit_overlay_index = index
                    break
        if hit_overlay is not None:
            self.selected = ("overlay", hit_overlay_index)
            self.update()
            name = str(hit_overlay.get("name") or "声明叠加")
            delete_overlay = menu.addAction(f"删除「{name}」")
            delete_overlay.triggered.connect(self.delete_selected)
            menu.addSeparator()
        if self.transition_names:
            submenu = menu.addMenu("添加转场")
            for name in self.transition_names:
                action = submenu.addAction(name)
                action.triggered.connect(
                    lambda checked=False, value=name, at=position: (
                        self.push_undo(), self.add_transition(value, at)
                    )
                )
            nearby = next(
                (
                    item for item in self.transitions
                    if abs(int(item.get("position", 0)) - position) <= 300
                ),
                None,
            )
            if nearby:
                menu.addSeparator()
                remove = menu.addAction("删除此处转场")
                remove.triggered.connect(lambda checked=False, item=nearby: self._remove_transition(item))
        if menu.actions():
            menu.exec(event.globalPos())
        else:
            super().contextMenuEvent(event)

    def _remove_transition(self, item: dict):
        if item in self.transitions:
            self.push_undo()
            self.transitions.remove(item)
            self._emit_timeline_state()
            self.update()

    def split_at_playhead(self) -> bool:
        """Split the clip under the playhead. Returns True if a cut was made."""
        cut = int(self.position_ms)
        selected = self.selected
        # 优先：当前选中的可切轨；否则找播放头所在的视频段；再否则原声/BGM/TTS
        if selected and selected[0] in ("video", "original_audio", "bgm", "ambient", "tts"):
            kind, index = selected
            tracks = self.media_clips.get(kind, [])
            if not (0 <= index < len(tracks)):
                selected = None
            else:
                clip = tracks[index]
                # 选中段不包含播放头时，改找播放头下的视频
                if not (clip.start <= cut <= clip.end):
                    selected = None
        if not selected or selected[0] == "caption" or selected[0] == "overlay":
            selected = next(
                (
                    ("video", i)
                    for i, clip in enumerate(self.media_clips.get("video") or [])
                    if clip.start <= cut <= clip.end
                ),
                None,
            )
        if not selected:
            # 再试其它轨（用户点在原声/BGM 上时）
            for kind in ("original_audio", "bgm", "ambient", "tts"):
                selected = next(
                    (
                        (kind, i)
                        for i, clip in enumerate(self.media_clips.get(kind) or [])
                        if clip.start <= cut <= clip.end
                    ),
                    None,
                )
                if selected:
                    break
        if not selected:
            return False
        kind, index = selected
        tracks = self.media_clips.get(kind, [])
        if not (0 <= index < len(tracks)):
            return False
        self.push_undo()
        if not self._split_clip(kind, index, cut):
            if self._undo_stack:
                self._undo_stack.pop()
            return False
        self.selected = (kind, index + 1)
        self._emit_timeline_state()
        self.update()
        return True

    def _split_linked_original_audio(self, cut: int):
        for index, clip in enumerate(self.media_clips["original_audio"]):
            if clip.start < cut < clip.end:
                source_cut = clip.source_start + (cut - clip.start)
                src_dur = clip.resolved_source_duration()
                self.media_clips["original_audio"][index:index+1] = [
                    MediaClip(clip.start, cut, clip.source_start, source_cut, clip.name, src_dur),
                    MediaClip(cut, clip.end, source_cut, clip.source_end, clip.name, src_dur),
                ]
                return

    def delete_selected(self):
        if not self.selected:
            return
        kind, index = self.selected
        if kind == "caption":
            if 0 <= index < len(self.clips):
                self.push_undo()
                self.clips.pop(index)
                self.srtChanged.emit(write_srt(self.clips))
        elif kind == "overlay":
            if 0 <= index < len(self.overlays):
                self.push_undo()
                self.overlays.pop(index)
                # 由 timelineEdited → 父页刷新预览；此处保证选中态清空
                self.selected = None
                self._emit_timeline_state()
                self.update()
                return
        else:
            tracks = self.media_clips.get(kind, [])
            if 0 <= index < len(tracks):
                if kind == "video" and len(tracks) == 1:
                    return
                self.push_undo()
                removed = tracks.pop(index)
                if kind == "video":
                    self._ripple_delete_range(removed.start, removed.end)
        self.selected = None
        self._emit_timeline_state()
        self.update()

    def keyPressEvent(self, event):
        mods = event.modifiers()
        key = event.key()
        if mods & Qt.KeyboardModifier.ControlModifier:
            if key == Qt.Key.Key_Z and not (mods & Qt.KeyboardModifier.ShiftModifier):
                if self.undo():
                    event.accept()
                    return
            if key == Qt.Key.Key_Y or (
                key == Qt.Key.Key_Z and (mods & Qt.KeyboardModifier.ShiftModifier)
            ):
                if self.redo():
                    event.accept()
                    return
        if key in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            self.delete_selected()
            event.accept()
            return
        super().keyPressEvent(event)

    def _ripple_delete_range(self, start: int, end: int):
        delta = max(0, end - start)
        self.media_clips["video"] = self._delete_range_from_track(
            self.media_clips["video"], start, end, delta
        )
        for kind in ("original_audio", "bgm", "ambient", "tts"):
            self.media_clips[kind] = self._delete_range_from_track(
                self.media_clips[kind], start, end, delta
            )
        kept: list[CaptionClip] = []
        for cue in self.clips:
            if cue.end <= start:
                kept.append(cue)
            elif cue.start >= end:
                kept.append(CaptionClip(cue.start-delta, cue.end-delta, cue.text))
            elif cue.start < start:
                kept.append(CaptionClip(cue.start, max(cue.start+80, start), cue.text))
        self.clips = kept
        # 声明叠加层与媒体同步涟漪，避免删段后文字/蒙版/PNG 仍停在旧时间
        if hasattr(self, "overlays") and self.overlays:
            kept_overlays = []
            for item in self.overlays:
                try:
                    o_start = int(item.get("start", 0) or 0)
                    o_end = int(item.get("end", o_start + 1000) or (o_start + 1000))
                except Exception:
                    kept_overlays.append(item)
                    continue
                if o_end <= start:
                    kept_overlays.append(item)
                elif o_start >= end:
                    copy = dict(item)
                    copy["start"] = o_start - delta
                    copy["end"] = o_end - delta
                    kept_overlays.append(copy)
                elif o_start < start < o_end:
                    copy = dict(item)
                    copy["end"] = max(copy.get("start", start) + 80, start)
                    kept_overlays.append(copy)
                # 完全落在删除区间内的叠加层丢弃
            self.overlays = kept_overlays
        adjusted_transitions = []
        for item in self.transitions:
            position = int(item.get("position", 0))
            if start <= position <= end:
                continue
            copy = dict(item)
            if position > end:
                copy["position"] = position - delta
            adjusted_transitions.append(copy)
        self.transitions = adjusted_transitions
        self.duration_ms = max(1000, self.duration_ms - delta)
        self.srtChanged.emit(write_srt(self.clips))
        # 通知上层同步词级时间轴（毫秒）
        if hasattr(self, "rangeRippled"):
            try:
                self.rangeRippled.emit(int(start), int(end))
            except Exception:
                pass
        self._update_width()
        self._emit_timeline_state()

    @staticmethod
    def _delete_range_from_track(
        clips: list[MediaClip], start: int, end: int, delta: int
    ) -> list[MediaClip]:
        result = []
        for clip in clips:
            src_dur = clip.resolved_source_duration()
            if clip.end <= start:
                result.append(clip)
            elif clip.start >= end:
                result.append(
                    MediaClip(
                        clip.start-delta, clip.end-delta,
                        clip.source_start, clip.source_end, clip.name, src_dur,
                        clip.media_type, clip.path,
                    )
                )
            else:
                if clip.start < start:
                    left_source_end = clip.source_start + (start - clip.start)
                    result.append(
                        MediaClip(
                            clip.start, start, clip.source_start, left_source_end,
                            clip.name, src_dur, clip.media_type, clip.path,
                        )
                    )
                if clip.end > end:
                    right_source_start = clip.source_start + (end - clip.start)
                    result.append(
                        MediaClip(
                            start, clip.end-delta,
                            right_source_start, clip.source_end, clip.name, src_dur,
                            clip.media_type, clip.path,
                        )
                    )
        return result

    def current_state(self) -> dict:
        """Return a detached snapshot so callers can persist edits immediately."""
        return {
            "duration_ms": self.duration_ms,
            "original_audio_enabled": self.original_audio_enabled,
            # 用户切片/拖动后禁止被「按时长重算分段」覆盖
            "user_edited": True,
            "transitions": [dict(item) for item in self.transitions],
            "overlays": [
                {
                    "start": int(item.get("start", 0)),
                    "end": int(item.get("end", 0)),
                    "name": str(item.get("name", "")),
                    "layer": dict(item.get("layer") or {}),
                }
                for item in self.overlays
            ],
            "tracks": {
                kind: [clip.as_dict() for clip in clips]
                for kind, clips in self.media_clips.items()
            },
        }

    def _emit_timeline_state(self):
        self.timelineEdited.emit(self.current_state())

    def _history_snapshot(self) -> dict:
        return {
            "duration_ms": self.duration_ms,
            "original_audio_enabled": self.original_audio_enabled,
            "position_ms": self.position_ms,
            "clips": [(c.start, c.end, c.text) for c in self.clips],
            "transitions": [dict(item) for item in self.transitions],
            "overlays": [
                {
                    "start": int(item.get("start", 0)),
                    "end": int(item.get("end", 0)),
                    "name": str(item.get("name", "")),
                    "layer": dict(item.get("layer") or {}),
                }
                for item in self.overlays
            ],
            "tracks": {
                kind: [clip.as_dict() for clip in clips]
                for kind, clips in self.media_clips.items()
            },
            "selected": self.selected,
        }

    def _restore_history_snapshot(self, snap: dict):
        self._history_locked = True
        try:
            self.duration_ms = max(1000, int(snap.get("duration_ms") or 1000))
            self.original_audio_enabled = bool(snap.get("original_audio_enabled", True))
            self.position_ms = max(0, min(int(snap.get("position_ms") or 0), self.duration_ms))
            self.clips = [
                CaptionClip(int(s), int(e), str(t))
                for s, e, t in (snap.get("clips") or [])
            ]
            self.transitions = [dict(item) for item in (snap.get("transitions") or [])]
            self.overlays = [
                {
                    "start": int(item.get("start", 0)),
                    "end": int(item.get("end", 0)),
                    "name": str(item.get("name", "")),
                    "layer": dict(item.get("layer") or {}),
                }
                for item in (snap.get("overlays") or [])
            ]
            tracks = snap.get("tracks") or {}
            for kind in self.media_clips:
                restored = []
                for item in tracks.get(kind, []):
                    try:
                        src_end = int(item["source_end"])
                        src_dur = int(item.get("source_duration") or 0) or src_end
                        try:
                            spd = float(item.get("speed", 1.0) or 1.0)
                        except (TypeError, ValueError):
                            spd = 1.0
                        restored.append(
                            MediaClip(
                                int(item["start"]), int(item["end"]),
                                int(item["source_start"]), src_end,
                                str(item.get("name", "")),
                                source_duration=src_dur,
                                media_type=str(item.get("media_type", "video") or "video"),
                                path=str(item.get("path", "") or ""),
                                speed=max(0.25, min(4.0, spd)),
                            )
                        )
                    except (KeyError, TypeError, ValueError):
                        continue
                self.media_clips[kind] = restored
            sel = snap.get("selected")
            self.selected = tuple(sel) if isinstance(sel, (list, tuple)) and len(sel) == 2 else None
            self._update_width()
            self.srtChanged.emit(write_srt(self.clips))
            self._emit_timeline_state()
            self.update()
        finally:
            self._history_locked = False

    def push_undo(self):
        """Save current state so the next edit can be undone."""
        if self._history_locked:
            return
        self._undo_stack.append(self._history_snapshot())
        if len(self._undo_stack) > self._max_history:
            self._undo_stack.pop(0)
        self._redo_stack.clear()

    def undo(self) -> bool:
        if not self._undo_stack:
            return False
        self._redo_stack.append(self._history_snapshot())
        self._restore_history_snapshot(self._undo_stack.pop())
        return True

    def redo(self) -> bool:
        if not self._redo_stack:
            return False
        self._undo_stack.append(self._history_snapshot())
        self._restore_history_snapshot(self._redo_stack.pop())
        return True

    def can_undo(self) -> bool:
        return bool(self._undo_stack)

    def can_redo(self) -> bool:
        return bool(self._redo_stack)

    def clear_history(self):
        self._undo_stack.clear()
        self._redo_stack.clear()


class TrackLabelRail(QWidget):
    """Fixed left column for track names — does not scroll or zoom with the timeline."""

    def __init__(self, canvas: "TimelineCanvas", parent=None):
        super().__init__(parent)
        self.canvas = canvas
        self.setFixedWidth(TimelineCanvas.LABEL_WIDTH)
        self.setMinimumHeight(
            TimelineCanvas.RULER_HEIGHT + TimelineCanvas.TRACK_HEIGHT * 7 + 8
        )
        self.setStyleSheet("background:#181b22;border-right:1px solid #2a2f3a;")

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor("#181b22"))
        painter.fillRect(0, 0, self.width(), TimelineCanvas.RULER_HEIGHT, QColor("#14171e"))
        painter.setPen(QColor("#64748b"))
        painter.setFont(QFont("Microsoft YaHei UI", 8))
        painter.drawText(8, 18, "轨道")

        rows = self.canvas.track_label_rows() if self.canvas else ()
        for index, (name, detail) in enumerate(rows):
            y = TimelineCanvas.RULER_HEIGHT + index * TimelineCanvas.TRACK_HEIGHT
            painter.fillRect(
                0,
                y,
                self.width(),
                TimelineCanvas.TRACK_HEIGHT - 1,
                QColor("#1a1e28") if index % 2 == 0 else QColor("#161a22"),
            )
            painter.setPen(QColor("#d8dbe5"))
            painter.setFont(QFont("Microsoft YaHei UI", 9, QFont.Weight.DemiBold))
            painter.drawText(10, y + 18, name)
            if detail:
                painter.setPen(QColor("#818898"))
                painter.setFont(QFont("Microsoft YaHei UI", 8))
                elided = painter.fontMetrics().elidedText(
                    Path(detail).name,
                    Qt.TextElideMode.ElideMiddle,
                    self.width() - 18,
                )
                painter.drawText(10, y + 34, elided)
        painter.setPen(QPen(QColor("#2a2f3a"), 1))
        painter.drawLine(self.width() - 1, 0, self.width() - 1, self.height())


class CanvaTimelinePanel(QWidget):
    srtChanged = Signal(str)
    seekRequested = Signal(int)
    rangeRippled = Signal(int, int)
    bgmVolumeChanged = Signal(int)
    ambientVolumeChanged = Signal(int)
    timelineEdited = Signal(dict)
    originalAudioChanged = Signal(bool)

    def __init__(self, ffmpeg: str = "ffmpeg", parent=None):
        super().__init__(parent)
        self.ffmpeg = str(ffmpeg or "ffmpeg")
        self._processes: dict[str, QProcess] = {}
        self._paths: dict[str, str] = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 6, 8, 6)
        root.setSpacing(5)
        toolbar = QVBoxLayout()
        toolbar.setSpacing(4)
        title_row = QHBoxLayout()
        title = QLabel("多轨时间轴")
        title.setStyleSheet("font-weight:800;color:#f4f4f5;font-size:13px;")
        hint = QLabel("图片＝覆盖画面；视频＝插入编辑并锁定自带音频")
        hint.setToolTip(
            "图片占用原时间且不改原声；视频会插入到放置点、推后后续原画面与原声音频，"
            "插入视频自己的画面和声音保持成对移动。"
        )
        hint.setWordWrap(False)
        hint.setStyleSheet("color:#8b93a5;font-size:11px;")
        title_row.addWidget(title)
        title_row.addWidget(hint, 1)
        toolbar.addLayout(title_row)
        edit_row = QHBoxLayout()
        self.cut_button = QPushButton("✂ 切片")
        self.cut_button.setToolTip(
            "把播放头拖到要切开的位置，点此切片。\n"
            "优先切中视频轨；也可先点选某段再切。\n"
            "切点需距片段两端至少约 0.05 秒。"
        )
        self.delete_button = QPushButton("删除")
        self.delete_button.setToolTip(
            "删除当前选中的时间轴片段。\n"
            "声明叠加轨（PNG/文字/蒙版）也可先点选色块再删，或右键删除。"
        )
        self.undo_button = QPushButton("撤销")
        self.undo_button.setToolTip("撤销上一步时间轴操作（Ctrl+Z）")
        self.redo_button = QPushButton("重做")
        self.redo_button.setToolTip("重做（Ctrl+Y / Ctrl+Shift+Z）")
        edit_row.addWidget(self.cut_button)
        edit_row.addWidget(self.delete_button)
        edit_row.addWidget(self.undo_button)
        edit_row.addWidget(self.redo_button)
        edit_row.addWidget(QLabel("片段速度"))
        self.clip_speed = QDoubleSpinBox()
        self.clip_speed.setRange(0.25, 4.0)
        self.clip_speed.setDecimals(2)
        self.clip_speed.setSingleStep(0.05)
        self.clip_speed.setValue(1.0)
        self.clip_speed.setSuffix(" ×")
        self.clip_speed.setToolTip(
            "先点选视频轨片段，再输入自定义倍数（0.25～4.00）。\n"
            "例如 1.25×、2.50×、0.60× 均可。\n"
            "加速：无声过场更快；慢放：好画面拉长。\n"
            "会按源时长÷速度重算该段长度，并后移后续片段。"
        )
        self.clip_speed.setMinimumWidth(88)
        self.clip_speed.setMaximumWidth(110)
        edit_row.addWidget(self.clip_speed)
        self.clip_speed_apply = QPushButton("应用")
        self.clip_speed_apply.setToolTip("把当前倍数应用到选中的视频片段")
        self.clip_speed_apply.setMaximumWidth(48)
        edit_row.addWidget(self.clip_speed_apply)
        # 常用快捷
        self.clip_speed_presets = QComboBox()
        self.clip_speed_presets.addItem("快捷…", None)
        for label, value in (
            ("0.5× 慢放", 0.5),
            ("0.75×", 0.75),
            ("1× 正常", 1.0),
            ("1.25×", 1.25),
            ("1.5×", 1.5),
            ("2×", 2.0),
            ("3×", 3.0),
        ):
            self.clip_speed_presets.addItem(label, value)
        self.clip_speed_presets.setToolTip("选快捷倍数会填入左边输入框，再点「应用」生效")
        self.clip_speed_presets.setMaximumWidth(96)
        edit_row.addWidget(self.clip_speed_presets)
        edit_row.addStretch()
        edit_row.addWidget(QLabel("图片时长"))
        self.image_overwrite_duration = QSpinBox()
        self.image_overwrite_duration.setRange(100, 10_000)
        self.image_overwrite_duration.setValue(1000)
        self.image_overwrite_duration.setSingleStep(100)
        self.image_overwrite_duration.setSuffix(" ms")
        self.image_overwrite_duration.setToolTip("图片覆盖画面的时长；不会增加总时长，也不会移动视频原声音轨")
        edit_row.addWidget(self.image_overwrite_duration)
        toolbar.addLayout(edit_row)
        view_row = QHBoxLayout()
        self.original_audio = QCheckBox("保留视频原声")
        self.original_audio.setChecked(True)
        self.original_audio.setToolTip(
            "只控制「视频原声」轨道（原片自带声音）。\n"
            "关闭后导出/预览不含原片声音，但环境音、BGM、配音仍独立存在。"
        )
        view_row.addWidget(self.original_audio)
        view_row.addWidget(QLabel("BGM"))
        self.volume = QSlider(Qt.Orientation.Horizontal)
        self.volume.setRange(0, 200)
        self.volume.setValue(25)
        self.volume.setMinimumWidth(56)
        self.volume.setMaximumWidth(110)
        self.volume.setToolTip("背景音乐音量（不影响环境音与视频原声）")
        self.volume.valueChanged.connect(self.bgmVolumeChanged)
        view_row.addWidget(self.volume, 1)
        view_row.addWidget(QLabel("环境音"))
        self.ambient_volume = QSlider(Qt.Orientation.Horizontal)
        self.ambient_volume.setRange(0, 200)
        self.ambient_volume.setValue(20)
        self.ambient_volume.setMinimumWidth(56)
        self.ambient_volume.setMaximumWidth(110)
        self.ambient_volume.setToolTip(
            "环境音音量（青色轨道，与视频原声完全独立）。\n"
            "在「背景音乐」页添加环境音文件后，会出现在此轨道上可单独裁剪。"
        )
        self.ambient_volume.valueChanged.connect(self.ambientVolumeChanged)
        view_row.addWidget(self.ambient_volume, 1)
        view_row.addWidget(QLabel("缩放"))
        self.zoom = QSlider(Qt.Orientation.Horizontal)
        self.zoom.setRange(ZOOM_MIN_PPS, ZOOM_MAX_PPS)
        self.zoom.setValue(ZOOM_DEFAULT_PPS)
        self.zoom.setSingleStep(8)
        self.zoom.setPageStep(80)
        self.zoom.setMinimumWidth(90)
        self.zoom.setMaximumWidth(180)
        self.zoom.setToolTip(
            "时间轴缩放（也可在轨道上滚轮缩放）\n"
            f"最小 {ZOOM_MIN_PPS}px/s · 最大 {ZOOM_MAX_PPS}px/s（可到帧级）\n"
            "左侧轨道名称固定不动"
        )
        view_row.addWidget(self.zoom, 1)
        self.zoom_value = QLabel(f"{ZOOM_DEFAULT_PPS}")
        self.zoom_value.setFixedWidth(36)
        self.zoom_value.setStyleSheet("color:#94a3b8;font-size:11px;")
        self.zoom_value.setToolTip("当前像素/秒")
        view_row.addWidget(self.zoom_value)
        fit = QPushButton("适合窗口")
        fit.clicked.connect(self._fit)
        view_row.addWidget(fit)
        toolbar.addLayout(view_row)
        root.addLayout(toolbar)

        # Fixed track-name rail + scrollable timeline content
        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        self.canvas = TimelineCanvas()
        self.canvas.ffmpeg_path = self.ffmpeg
        self.label_rail = TrackLabelRail(self.canvas)
        self.canvas.label_rail = self.label_rail
        body.addWidget(self.label_rail, 0)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.scroll.setStyleSheet("QScrollArea{border:none;background:#111318;}")
        self.scroll.setWidget(self.canvas)
        body.addWidget(self.scroll, 1)
        root.addLayout(body, 1)

        self.zoom.valueChanged.connect(self._on_zoom_slider)
        self.image_overwrite_duration.valueChanged.connect(
            self.canvas.set_image_overwrite_duration
        )
        self.canvas.zoomWheel.connect(self._wheel_zoom)
        self.canvas.srtChanged.connect(self.srtChanged)
        self.canvas.seekRequested.connect(self.seekRequested)
        self.canvas.rangeRippled.connect(self.rangeRippled)
        self.canvas.timelineEdited.connect(self.timelineEdited)
        self.canvas.timelineEdited.connect(lambda *_: self._refresh_undo_buttons())
        self.canvas.srtChanged.connect(lambda *_: self._refresh_undo_buttons())
        # 直接连接切片（勿再用 connect(lambda:None)+disconnect 占位，部分环境下会丢信号）
        self.cut_button.clicked.connect(self._on_cut_clicked)
        self.delete_button.clicked.connect(self.canvas.delete_selected)
        self.clip_speed_apply.clicked.connect(self._apply_clip_speed)
        self.clip_speed.editingFinished.connect(self._apply_clip_speed)
        self.clip_speed_presets.activated.connect(self._fill_speed_from_preset)
        self.undo_button.clicked.connect(self._undo_clicked)
        self.redo_button.clicked.connect(self._redo_clicked)
        self._refresh_undo_buttons()
        self.original_audio.toggled.connect(self.canvas.set_original_audio_enabled)
        self.original_audio.toggled.connect(self.originalAudioChanged)
        self.setMinimumHeight(262)
        self.setStyleSheet(
            "CanvaTimelinePanel{background:#101218;border:1px solid #30343f;border-radius:8px;}"
        )
        # Let the scroll viewport also zoom with the wheel (when not over canvas chrome).
        self.scroll.viewport().installEventFilter(self)

    def eventFilter(self, obj, event):
        if obj is self.scroll.viewport() and event.type() == QEvent.Type.Wheel:
            # Zoom when wheel is used over empty viewport margins.
            delta = int(event.angleDelta().y()) or int(event.pixelDelta().y())
            if delta:
                # Map x into canvas coordinates
                canvas_pos = self.canvas.mapFrom(self.scroll.viewport(), event.position().toPoint())
                self._wheel_zoom(delta, int(canvas_pos.x()))
                return True
        return super().eventFilter(obj, event)

    def _on_zoom_slider(self, value: int):
        self.canvas.set_zoom(value)
        if hasattr(self, "zoom_value"):
            self.zoom_value.setText(str(int(value)))

    def _fill_speed_from_preset(self, index: int):
        if not hasattr(self, "clip_speed_presets") or not hasattr(self, "clip_speed"):
            return
        data = self.clip_speed_presets.itemData(index)
        if data is None:
            return
        try:
            speed = float(data)
        except (TypeError, ValueError):
            return
        self.clip_speed.blockSignals(True)
        self.clip_speed.setValue(speed)
        self.clip_speed.blockSignals(False)
        self._apply_clip_speed()
        self.clip_speed_presets.blockSignals(True)
        self.clip_speed_presets.setCurrentIndex(0)
        self.clip_speed_presets.blockSignals(False)

    def _apply_clip_speed(self, *_args):
        if not hasattr(self, "clip_speed"):
            return
        try:
            speed = float(self.clip_speed.value())
        except (TypeError, ValueError):
            return
        if self.canvas.set_selected_video_speed(speed):
            self._refresh_undo_buttons()
        else:
            # 未选中视频片段：把输入框同步为当前选中段速度（若有）
            sel = self.canvas.selected
            if sel and sel[0] == "video":
                tracks = self.canvas.media_clips.get("video", [])
                idx = sel[1]
                if 0 <= idx < len(tracks):
                    cur = float(getattr(tracks[idx], "speed", 1.0) or 1.0)
                    self.clip_speed.blockSignals(True)
                    self.clip_speed.setValue(cur)
                    self.clip_speed.blockSignals(False)

    def _refresh_undo_buttons(self):
        if hasattr(self, "undo_button"):
            self.undo_button.setEnabled(self.canvas.can_undo())
        if hasattr(self, "redo_button"):
            self.redo_button.setEnabled(self.canvas.can_redo())

    def _undo_clicked(self):
        if self.canvas.undo():
            self._refresh_undo_buttons()

    def _redo_clicked(self):
        if self.canvas.redo():
            self._refresh_undo_buttons()

    def keyPressEvent(self, event):
        # Allow undo/redo when focus is on panel chrome (not only canvas).
        mods = event.modifiers()
        key = event.key()
        if mods & Qt.KeyboardModifier.ControlModifier:
            if key == Qt.Key.Key_Z and not (mods & Qt.KeyboardModifier.ShiftModifier):
                self._undo_clicked()
                event.accept()
                return
            if key == Qt.Key.Key_Y or (
                key == Qt.Key.Key_Z and (mods & Qt.KeyboardModifier.ShiftModifier)
            ):
                self._redo_clicked()
                event.accept()
                return
        super().keyPressEvent(event)

    def _wheel_zoom(self, delta: int, canvas_x: int):
        """Zoom toward the cursor so frame-level edits stay under the pointer."""
        if not delta:
            return
        old = max(ZOOM_MIN_PPS, int(self.canvas.pixels_per_second))
        steps = max(1.0, abs(delta) / 120.0)
        factor = (1.22 ** steps) if delta > 0 else (1.0 / (1.22 ** steps))
        new = int(round(old * factor))
        new = max(ZOOM_MIN_PPS, min(ZOOM_MAX_PPS, new))
        if new == old:
            return
        time_ms = self.canvas._ms(canvas_x)
        # Keep the same media time under the cursor after width changes.
        global_pt = self.canvas.mapToGlobal(QPoint(int(canvas_x), 1))
        view_x = self.scroll.viewport().mapFromGlobal(global_pt).x()
        self.zoom.blockSignals(True)
        self.zoom.setValue(new)
        self.zoom.blockSignals(False)
        self._on_zoom_slider(new)
        new_x = self.canvas._x(time_ms)
        self.scroll.horizontalScrollBar().setValue(max(0, int(new_x - view_x)))

    def _fit(self):
        # Labels are outside the scroll area — use full viewport width for content.
        available = max(400, self.scroll.viewport().width() - 28)
        seconds = max(1.0, self.canvas.duration_ms / 1000)
        self.zoom.setValue(max(self.zoom.minimum(), min(self.zoom.maximum(), int(available / seconds))))

    def _on_cut_clicked(self):
        """Toolbar cut: split at playhead and surface why it no-ops."""
        ok = self.canvas.split_at_playhead()
        if not ok:
            # 轻量状态：不弹窗打断，用 tooltip 闪一下说明
            tip = (
                "无法切片：请把播放头移到某段视频中间（距两端至少约 0.05 秒），"
                "或先点选要切开的片段。"
            )
            self.cut_button.setToolTip(tip)
            # 恢复默认说明
            from PySide6.QtCore import QTimer
            QTimer.singleShot(
                4000,
                lambda: self.cut_button.setToolTip(
                    "把播放头拖到要切开的位置，点此切片。\n"
                    "优先切中视频轨；也可先点选某段再切。\n"
                    "切点需距片段两端至少约 0.05 秒。"
                ),
            )
        self._refresh_undo_buttons()

    def set_position(self, milliseconds: int):
        self.canvas.set_position(milliseconds)

    def set_transition_catalog(self, names: list[str], duration_ms: int = 500):
        self.canvas.set_transition_catalog(names, duration_ms)

    def set_transition_duration(self, duration_ms: int):
        self.canvas.set_transition_duration(duration_ms)

    def set_srt(self, srt: str):
        self.canvas.set_srt(srt)

    def add_overlay(self, layer: dict, start_ms: int, end_ms: int):
        return self.canvas.add_overlay(layer, start_ms, end_ms)

    def update_overlay_template(self, layer: dict) -> int:
        return self.canvas.update_overlay_template(layer)

    def current_state(self) -> dict:
        return self.canvas.current_state()

    def ensure_time_visible(self, milliseconds: int):
        """Scroll a newly inserted overlay into view without moving the playhead."""
        target_x = int(self.canvas._x(max(0, int(milliseconds))))
        bar = self.scroll.horizontalScrollBar()
        viewport_width = max(1, self.scroll.viewport().width())
        if target_x < bar.value() + 24 or target_x > bar.value() + viewport_width - 90:
            bar.setValue(max(0, target_x - viewport_width // 3))
        self.canvas.update()
        self.label_rail.update()

    def set_project(
        self,
        video_path: str,
        duration_ms: int,
        srt: str,
        bgm_path: str = "",
        tts_path: str = "",
        original_audio_enabled: bool = True,
        edit_state: dict | None = None,
        ambient_path: str = "",
    ):
        self.original_audio.blockSignals(True)
        self.original_audio.setChecked(bool(original_audio_enabled))
        self.original_audio.blockSignals(False)
        self.canvas.set_project(
            duration_ms, video_path, srt, bgm_path, tts_path, original_audio_enabled, edit_state,
            ambient_name=ambient_path,
        )
        self.canvas.set_waveform("video", [])
        self.canvas.set_waveform("bgm", [])
        self.canvas.set_waveform("ambient", [])
        self.canvas.set_waveform("tts", [])
        self._load_waveform("video", video_path)
        if bgm_path and Path(bgm_path).is_file():
            self._load_waveform("bgm", bgm_path)
        if ambient_path and Path(ambient_path).is_file():
            self._load_waveform("ambient", ambient_path)
        if tts_path and Path(tts_path).is_file():
            self._load_waveform("tts", tts_path)
        if hasattr(self, "label_rail"):
            self.label_rail.update()
        self._fit()

    def _load_waveform(self, kind: str, path: str):
        if not path or not Path(path).is_file():
            return
        previous = self._processes.pop(kind, None)
        if previous and previous.state() != QProcess.ProcessState.NotRunning:
            previous.kill()
        process = QProcess(self)
        process.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)
        self._processes[kind] = process
        self._paths[kind] = str(path)

        def finished(*_args):
            if self._processes.get(kind) is not process:
                process.deleteLater()
                return
            raw = bytes(process.readAllStandardOutput())
            values: list[float] = []
            if len(raw) >= 4:
                count = len(raw) // 4
                samples = struct.unpack(f"<{count}f", raw[: count * 4])
                bucket = max(1, count // 650)
                for offset in range(0, count, bucket):
                    chunk = samples[offset : offset + bucket]
                    values.append(min(1.0, max((abs(value) for value in chunk), default=0.0)))
            self.canvas.set_waveform(kind, values)
            self._processes.pop(kind, None)
            process.deleteLater()

        process.finished.connect(finished)
        process.start(
            self.ffmpeg,
            [
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(path),
                "-map",
                "0:a:0?",
                "-ac",
                "1",
                "-ar",
                "200",
                "-f",
                "f32le",
                "pipe:1",
            ],
        )
