"""Decode model input with our bundled FFmpeg, independent of PyAV API changes."""
from pathlib import Path

import numpy as np

from .precise_cut import compatible_audio_streams, run_media


def load_whisper_audio(ffmpeg, path, cancelled=lambda: False):
    streams = compatible_audio_streams(ffmpeg, path, cancelled)
    if not streams:
        raise RuntimeError("素材没有可用于字幕识别的音轨")
    raw = run_media([
        str(ffmpeg), "-hide_banner", "-loglevel", "error", "-nostdin", "-xerror",
        "-i", str(Path(path)), "-map", f"0:{streams[0]}", "-vn",
        "-ac", "1", "-ar", "16000", "-c:a", "pcm_f32le", "-f", "f32le", "pipe:1",
    ], cancelled)
    audio = np.frombuffer(raw, dtype="<f4").copy()
    if not audio.size or not np.isfinite(audio).all():
        raise RuntimeError("音轨解码结果为空或包含无效采样，无法识别字幕")
    return audio
