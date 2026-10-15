from __future__ import annotations

import subprocess
from functools import lru_cache

from .settings_page import hidden_kwargs


ENCODER_LABELS = {
    "auto": "自动硬件加速（推荐）",
    "nvenc": "NVIDIA NVENC",
    "mf": "Windows 硬件编码 (MF)",
    "qsv": "Intel Quick Sync",
    "amf": "AMD AMF",
    "cpu": "CPU 兼容模式",
}


def encoder_key(label_or_key):
    value = str(label_or_key or "auto")
    for key, label in ENCODER_LABELS.items():
        if value == key or value == label:
            return key
    return "auto"


def _run_probe(ffmpeg, args, timeout=12):
    """Run a short encode probe; return (ok, stderr_tail)."""
    command = [
        str(ffmpeg), "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "color=black:s=640x360:d=0.20",
        *args,
    ]
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=timeout,
            text=True,
            encoding="utf-8",
            errors="replace",
            **hidden_kwargs(),
        )
        err = (result.stderr or "").strip()
        return result.returncode == 0, err[-500:] if err else ""
    except subprocess.TimeoutExpired:
        return False, "probe_timeout"
    except Exception as exc:
        return False, str(exc)


@lru_cache(maxsize=32)
def encoder_available(ffmpeg, key):
    """Test a real short encode, not just whether FFmpeg lists the encoder."""
    ok, _reason = encoder_probe_detail(str(ffmpeg), key)
    return ok


@lru_cache(maxsize=32)
def encoder_probe_detail(ffmpeg, key):
    """Return (ok, reason). reason is empty when ok, else human-readable failure."""
    key = str(key or "")
    if key == "cpu":
        return True, ""
    if key == "nvenc":
        # 多种参数：新驱动用 p-preset，旧构建用 ll/hq
        for args in (
            ["-pix_fmt", "yuv420p", "-c:v", "h264_nvenc", "-preset", "p4",
             "-cq", "28", "-frames:v", "2", "-f", "null", "-"],
            ["-pix_fmt", "yuv420p", "-c:v", "h264_nvenc", "-preset", "fast",
             "-b:v", "2M", "-frames:v", "2", "-f", "null", "-"],
            ["-pix_fmt", "yuv420p", "-c:v", "h264_nvenc", "-b:v", "2M",
             "-frames:v", "2", "-f", "null", "-"],
        ):
            ok, err = _run_probe(ffmpeg, args, timeout=10)
            if ok:
                return True, ""
            joined = (err or "").lower()
            if any(
                token in joined
                for token in (
                    "nvenc api",
                    "driver does not support",
                    "610.00",
                    "required:",
                    "function not implemented",
                    "no nvenc",
                    "cannot load nvcuda",
                )
            ):
                return False, (
                    "NVIDIA NVENC 不可用：驱动/API 与当前 FFmpeg 不匹配。"
                    "本机探测到常见情况是驱动偏旧（如 560.x）而 FFmpeg 需要更新 NVENC。"
                    "请升级 GeForce 驱动到 610+，或先用「Windows 硬件编码 (MF)」。"
                )
            last = err
        return False, last or "h264_nvenc 打开失败"
    if key == "mf":
        # Windows Media Foundation：多数机器可走硬件（含独显/核显路径），不依赖 NVENC API 版本
        for args in (
            ["-pix_fmt", "yuv420p", "-c:v", "h264_mf", "-rate_control", "quality",
             "-quality", "75", "-frames:v", "3", "-f", "null", "-"],
            ["-pix_fmt", "yuv420p", "-c:v", "h264_mf", "-frames:v", "3", "-f", "null", "-"],
        ):
            ok, err = _run_probe(ffmpeg, args, timeout=12)
            if ok:
                return True, ""
            last = err
        return False, last or "h264_mf 不可用"
    if key == "qsv":
        # QSV 冷启动可能很慢，给更长超时；并强制 nv12
        for args in (
            ["-vf", "format=nv12", "-c:v", "h264_qsv", "-global_quality", "28",
             "-look_ahead", "0", "-frames:v", "2", "-f", "null", "-"],
            ["-pix_fmt", "nv12", "-c:v", "h264_qsv", "-global_quality", "28",
             "-frames:v", "2", "-f", "null", "-"],
        ):
            ok, err = _run_probe(ffmpeg, args, timeout=25)
            if ok:
                return True, ""
            last = err
        return False, last or "h264_qsv 不可用或初始化过慢"
    if key == "amf":
        for args in (
            ["-pix_fmt", "yuv420p", "-c:v", "h264_amf", "-quality", "speed",
             "-rc", "cqp", "-qp_i", "28", "-frames:v", "2", "-f", "null", "-"],
        ):
            ok, err = _run_probe(ffmpeg, args, timeout=10)
            if ok:
                return True, ""
            last = err
        return False, last or "h264_amf 不可用"
    return False, f"未知编码器 {key}"


def resolve_encoder(ffmpeg, requested="auto"):
    requested = encoder_key(requested)
    if requested == "cpu":
        return "cpu"
    if requested != "auto":
        ok, _reason = encoder_probe_detail(str(ffmpeg), requested)
        return requested if ok else "cpu"
    # 优先顺序：NVENC（真独显）→ Windows MF（不挑 NVENC API）→ QSV → AMF
    for key in ("nvenc", "mf", "qsv", "amf"):
        ok, _reason = encoder_probe_detail(str(ffmpeg), key)
        if ok:
            return key
    return "cpu"


def diagnose_encoders(ffmpeg):
    """Return list of (key, ok, reason) for UI/log diagnostics."""
    rows = []
    for key in ("nvenc", "mf", "qsv", "amf", "cpu"):
        ok, reason = encoder_probe_detail(str(ffmpeg), key)
        rows.append((key, ok, reason))
    return rows


def encoder_args(key, cpu_preset="veryfast", preview=False, intermediate=False):
    """Return H.264 args. intermediate=True for temp segments (faster, slightly lower quality).

    成品编码默认关闭 B 帧（-bf 0）：B 帧重排会让 stream start_time≈1 帧（如 0.033s），
    达芬奇时间线从 0 起播会出现片头黑帧。
    """
    key = encoder_key(key)
    # 中间分段/时间轴缓存：优先速度（仍关 B 帧，避免拼接后残留 start delay）
    if intermediate or preview:
        if key == "qsv":
            return ["-c:v", "h264_qsv", "-preset", "veryfast", "-global_quality", "28",
                    "-look_ahead", "0", "-bf", "0", "-pix_fmt", "nv12"]
        if key == "nvenc":
            return ["-c:v", "h264_nvenc", "-preset", "p1", "-tune", "ll",
                    "-rc", "vbr", "-cq", "28", "-b:v", "0", "-bf", "0", "-pix_fmt", "yuv420p"]
        if key == "mf":
            # MF 无标准 -bf；靠滤镜 setpts + mux 参数压掉 start delay
            return ["-c:v", "h264_mf", "-rate_control", "quality", "-quality", "70",
                    "-pix_fmt", "yuv420p"]
        if key == "amf":
            return ["-c:v", "h264_amf", "-quality", "speed",
                    "-rc", "cqp", "-qp_i", "28", "-qp_p", "28", "-bf_delta_qp", "0",
                    "-pix_fmt", "yuv420p"]
        return ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
                "-bf", "0", "-pix_fmt", "yuv420p", "-threads", "0"]
    if key == "qsv":
        return ["-c:v", "h264_qsv", "-preset", "medium", "-global_quality", "19",
                "-look_ahead", "1", "-bf", "0", "-pix_fmt", "nv12"]
    if key == "nvenc":
        return ["-c:v", "h264_nvenc", "-preset", "p4", "-tune", "hq",
                "-rc", "vbr", "-cq", "19", "-b:v", "0", "-bf", "0", "-pix_fmt", "yuv420p"]
    if key == "mf":
        return ["-c:v", "h264_mf", "-rate_control", "quality", "-quality", "85",
                "-pix_fmt", "yuv420p"]
    if key == "amf":
        return ["-c:v", "h264_amf", "-quality", "quality",
                "-rc", "cqp", "-qp_i", "18", "-qp_p", "20", "-bf_delta_qp", "0",
                "-pix_fmt", "yuv420p"]
    preset = str(cpu_preset or "fast")
    if preset not in ("ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow"):
        preset = "fast"
    # 越慢 CRF 略降：成品更清晰。不再把 medium 偷偷降成 faster。
    crf_by_preset = {
        "ultrafast": 22,
        "superfast": 21,
        "veryfast": 20,
        "faster": 19,
        "fast": 18,
        "medium": 17,
        "slow": 16,
    }
    crf = crf_by_preset.get(preset, 18)
    return ["-c:v", "libx264", "-preset", preset,
            "-crf", str(crf), "-bf", "0", "-pix_fmt", "yuv420p", "-threads", "0"]


def davinci_safe_mux_args(fps=30):
    """Mux flags so Resolve/达芬奇 sees video+audio both starting at t=0, true CFR."""
    rate = str(int(fps) if float(fps).is_integer() else fps)
    timescale = str(int(float(rate) * 1000))
    return [
        "-r", rate,
        "-fps_mode", "cfr",
        "-video_track_timescale", timescale,
        "-muxdelay", "0",
        "-muxpreload", "0",
        "-avoid_negative_ts", "make_zero",
        "-movflags", "+faststart",
    ]


def _probe_video_fps(ffmpeg, path, default=30.0):
    """Best-effort average frame rate from ffprobe; falls back to default."""
    from pathlib import Path
    import json
    path = Path(path)
    ffprobe = Path(str(ffmpeg)).with_name("ffprobe" + Path(str(ffmpeg)).suffix)
    if not ffprobe.exists():
        ffprobe = "ffprobe"
    try:
        result = subprocess.run(
            [
                str(ffprobe), "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=avg_frame_rate,r_frame_rate",
                "-of", "json", str(path),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=20,
            text=True,
            encoding="utf-8",
            errors="replace",
            **hidden_kwargs(),
        )
        data = json.loads(result.stdout or "{}")
        stream = (data.get("streams") or [{}])[0]
        for key in ("avg_frame_rate", "r_frame_rate"):
            raw = str(stream.get(key) or "")
            if "/" in raw:
                num, den = raw.split("/", 1)
                try:
                    value = float(num) / float(den)
                except (TypeError, ValueError, ZeroDivisionError):
                    continue
                if 1.0 <= value <= 120.0:
                    return value
            else:
                try:
                    value = float(raw)
                except (TypeError, ValueError):
                    continue
                if 1.0 <= value <= 120.0:
                    return value
    except Exception:
        pass
    return float(default or 30.0)


def _probe_stream_start_times(ffmpeg, path):
    """Return (video_start, audio_start); missing stream → 0.0."""
    from pathlib import Path
    import json
    path = Path(path)
    ffprobe = Path(str(ffmpeg)).with_name("ffprobe" + Path(str(ffmpeg)).suffix)
    if not ffprobe.exists():
        ffprobe = "ffprobe"
    v_start = a_start = 0.0
    try:
        result = subprocess.run(
            [
                str(ffprobe), "-v", "error",
                "-show_entries", "stream=codec_type,start_time",
                "-of", "json", str(path),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=20,
            text=True,
            encoding="utf-8",
            errors="replace",
            **hidden_kwargs(),
        )
        data = json.loads(result.stdout or "{}")
        for stream in data.get("streams") or []:
            try:
                st = float(stream.get("start_time") or 0)
            except (TypeError, ValueError):
                st = 0.0
            kind = str(stream.get("codec_type") or "")
            if kind == "video" and v_start == 0.0:
                v_start = st
            elif kind == "audio" and a_start == 0.0:
                a_start = st
    except Exception:
        pass
    return v_start, a_start


def remux_zero_start(ffmpeg, path, fps=None):
    """重封装：去掉 AAC 编码延迟导致的 video start_time≈1 帧，防止达芬奇片头黑帧。

    仅 copy + 视频 bitstream 时间戳重写，不二次损画质。失败时保留原文件。
    若音画已从 ~0 起则跳过（避免每次合成多扫一遍大文件）。
    """
    from pathlib import Path
    import os
    path = Path(path)
    if not path.is_file() or path.stat().st_size < 1024:
        return False
    # 已对齐则跳过：省掉大文件二次 remux（分组合成尾声明显变快）
    try:
        v0, a0 = _probe_stream_start_times(ffmpeg, path)
        if v0 <= 0.002 and a0 <= 0.002:
            return False
    except Exception:
        pass
    temporary = path.with_name(f"{path.stem}.zts_{os.getpid()}{path.suffix}")
    rate = float(fps) if fps and float(fps) > 1 else _probe_video_fps(ffmpeg, path, 30.0)
    # 保留合理小数；常见 24/25/30/29.97
    if abs(rate - round(rate)) < 0.02:
        rate_expr = str(int(round(rate)))
    else:
        rate_expr = f"{rate:.3f}".rstrip("0").rstrip(".")
    command = [
        str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(path),
        "-map", "0:v:0?", "-map", "0:a:0?",
        "-c", "copy",
        "-bsf:v", f"setts=ts=N/{rate_expr}/TB",
        "-muxdelay", "0", "-muxpreload", "0",
        "-avoid_negative_ts", "make_zero",
        "-movflags", "+faststart",
        str(temporary),
    ]
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=max(60, min(600, path.stat().st_size // (2 * 1024 * 1024) + 30)),
            text=True,
            encoding="utf-8",
            errors="replace",
            **hidden_kwargs(),
        )
        if result.returncode != 0 or not temporary.is_file() or temporary.stat().st_size < 1024:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            return False
        os.replace(str(temporary), str(path))
        return True
    except Exception:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        return False


def wrap_with_intro_outro(
    ffmpeg,
    body_path,
    *,
    intro_path: str = "",
    outro_path: str = "",
    work_dir=None,
    target_w: int = 0,
    target_h: int = 0,
    fps: float = 30.0,
):
    """Optionally prepend/append bumper clips around a finished body video.

    Returns the final path (may replace body_path in place). Empty bumpers → no-op.
    Bumpers are normalized to body size/fps then concat-filtered (hard cut).
    """
    from pathlib import Path
    import os
    import tempfile

    body = Path(body_path)
    if not body.is_file() or body.stat().st_size < 1024:
        return str(body)
    intro = Path(intro_path) if intro_path and Path(intro_path).is_file() else None
    outro = Path(outro_path) if outro_path and Path(outro_path).is_file() else None
    if intro is None and outro is None:
        return str(body)

    # Probe body size if not provided
    tw, th = int(target_w or 0), int(target_h or 0)
    if tw < 16 or th < 16:
        try:
            ffprobe = Path(str(ffmpeg)).with_name("ffprobe" + Path(str(ffmpeg)).suffix)
            if not ffprobe.exists():
                ffprobe = Path("ffprobe")
            probe = subprocess.run(
                [
                    str(ffprobe), "-v", "error", "-select_streams", "v:0",
                    "-show_entries", "stream=width,height",
                    "-of", "csv=p=0:s=x", str(body),
                ],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                **hidden_kwargs(), timeout=20,
            )
            parts_wh = (probe.stdout or "").strip().split("x")
            tw, th = int(parts_wh[0]), int(parts_wh[1])
        except Exception:
            tw, th = 1080, 1920
    tw = max(16, tw - (tw % 2))
    th = max(16, th - (th % 2))
    rate = float(fps) if fps and float(fps) > 1 else 30.0
    if abs(rate - round(rate)) < 0.02:
        rate_expr = str(int(round(rate)))
    else:
        rate_expr = f"{rate:.3f}".rstrip("0").rstrip(".")

    cache = Path(work_dir) if work_dir else Path(tempfile.mkdtemp(prefix="vt_bumpers_"))
    cache.mkdir(parents=True, exist_ok=True)
    ffprobe_bin = Path(str(ffmpeg)).with_name("ffprobe" + Path(str(ffmpeg)).suffix)
    if not ffprobe_bin.exists():
        ffprobe_bin = Path("ffprobe")

    def _has_audio(src: Path) -> bool:
        try:
            probe = subprocess.run(
                [
                    str(ffprobe_bin), "-v", "error", "-select_streams", "a",
                    "-show_entries", "stream=index", "-of", "csv=p=0", str(src),
                ],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                **hidden_kwargs(), timeout=20,
            )
            return bool((probe.stdout or "").strip())
        except Exception:
            return True

    def _normalize(src: Path, label: str) -> Path:
        dest = cache / f"{label}_{src.stem[:40]}_{tw}x{th}.mp4"
        if dest.is_file() and dest.stat().st_size > 1024:
            try:
                if dest.stat().st_mtime >= src.stat().st_mtime:
                    return dest
            except OSError:
                pass
        vf = (
            f"scale={tw}:{th}:force_original_aspect_ratio=decrease,"
            f"pad={tw}:{th}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={rate_expr},format=yuv420p"
        )
        if _has_audio(src):
            cmd = [
                str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(src),
                "-vf", vf,
                "-af", "aresample=async=1:first_pts=0",
                "-ar", "48000", "-ac", "2",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                "-c:a", "aac", "-b:a", "192k",
                "-shortest",
                "-movflags", "+faststart",
                str(dest),
            ]
        else:
            cmd = [
                str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(src),
                "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
                "-vf", vf,
                "-map", "0:v:0", "-map", "1:a:0",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                "-c:a", "aac", "-b:a", "192k",
                "-shortest",
                "-movflags", "+faststart",
                str(dest),
            ]
        result = subprocess.run(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
            **hidden_kwargs(),
            timeout=600,
        )
        if result.returncode != 0 or not dest.is_file() or dest.stat().st_size < 1024:
            raise RuntimeError(
                f"片头/片尾规范化失败（{src.name}）：{(result.stderr or '')[-400:]}"
            )
        return dest

    parts = []
    if intro is not None:
        parts.append(_normalize(intro, "intro"))
    # Also normalize body so concat codecs match
    parts.append(_normalize(body, "body"))
    if outro is not None:
        parts.append(_normalize(outro, "outro"))

    n = len(parts)
    inputs = []
    for p in parts:
        inputs.extend(["-i", str(p)])
    fc_in = "".join(f"[{i}:v:0][{i}:a:0]" for i in range(n))
    fc = f"{fc_in}concat=n={n}:v=1:a=1[v][a]"
    out = cache / f"wrapped_{body.stem[:40]}.mp4"
    cmd = [
        str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
        *inputs,
        "-filter_complex", fc,
        "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        str(out),
    ]
    result = subprocess.run(
        cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
        **hidden_kwargs(),
        timeout=1200,
    )
    if result.returncode != 0 or not out.is_file() or out.stat().st_size < 1024:
        raise RuntimeError(f"拼接片头/片尾失败：{(result.stderr or '')[-500:]}")
    try:
        os.replace(str(out), str(body))
        return str(body)
    except OSError:
        return str(out)


def calculate_target_size(src_w, src_h, aspect_ratio_str, resolution_str):
    # Determine orientation
    is_portrait = src_h > src_w

    # Determine aspect ratio
    if aspect_ratio_str == "16:9":
        ratio = 9 / 16 if is_portrait else 16 / 9
    elif aspect_ratio_str == "3:4":
        ratio = 3 / 4 if is_portrait else 4 / 3
    elif aspect_ratio_str == "1:1":
        ratio = 1.0
    else:
        ratio = src_w / src_h if src_h else 16 / 9

    # Determine target height based on resolution selection
    if resolution_str == "720p":
        h = 720
    elif resolution_str == "1080p":
        h = 1080
    elif resolution_str == "2K":
        h = 1440
    elif resolution_str == "4K":
        h = 2160
    else:
        # "默认最高/原始" -> use the source's max dimension
        if is_portrait:
            h = src_h
        else:
            h = int(src_w / ratio) if ratio else src_h

    # Calculate target width
    if ratio == 1.0:
        if resolution_str == "720p":
            w, h = 720, 720
        elif resolution_str == "1080p":
            w, h = 1080, 1080
        elif resolution_str == "2K":
            w, h = 1440, 1440
        elif resolution_str == "4K":
            w, h = 2160, 2160
        else:
            max_dim = max(src_w, src_h)
            w, h = max_dim, max_dim
    else:
        w = int(h * ratio)

    # Ensure w and h are even numbers (FFmpeg requires even dimensions for yuv420p)
    w = (w // 2) * 2
    h = (h // 2) * 2
    return w, h
