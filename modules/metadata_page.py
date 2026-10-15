from __future__ import annotations

import os
import json
import subprocess
import threading
import time
from pathlib import Path

from PIL import ExifTags, Image
from PySide6.QtCore import QObject, Qt, QThread, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFileDialog, QGroupBox, QHBoxLayout,
    QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
    QScrollArea, QSizePolicy, QSpinBox, QSplitter, QVBoxLayout, QWidget,
)

from .path_picker import (AUDIO_EXTENSIONS, IMAGE_EXTENSIONS, VIDEO_EXTENSIONS,
                          DropListWidget, collect_files, default_output_path)
from .settings_page import find_media_tool, hidden_kwargs
from .video_encoding import encoder_args, resolve_encoder

MEDIA_EXTENSIONS = VIDEO_EXTENSIONS | AUDIO_EXTENSIONS | IMAGE_EXTENSIONS
_META_STRIP = [
    "-map_metadata", "-1", "-map_metadata:s", "-1", "-map_metadata:p", "-1",
    "-map_metadata:c", "-1", "-map_chapters", "-1", "-fflags", "+bitexact",
    "-metadata", "creation_time=", "-metadata", "date=", "-metadata", "location=",
    "-metadata", "title=", "-metadata", "artist=", "-metadata", "author=",
    "-metadata", "copyright=", "-metadata", "comment=", "-metadata", "description=",
    "-metadata", "encoder=",
]
# 9:16 竖屏；表达式保证偶数宽高（yuv420p 兼容）
_CROP_916_VF = (
    "crop="
    "trunc(min(iw\\,ih*9/16)/2)*2:"
    "trunc(min(ih\\,iw*16/9)/2)*2:"
    "(iw-ow)/2:(ih-oh)/2"
)
_CROP_916_MODES = {
    "keep": None,  # 仅居中裁切，不缩放
    "1080x1920": (1080, 1920),
    "720x1280": (720, 1280),
    "1440x2560": (1440, 2560),
}


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    for index in range(2, 10000):
        candidate = path.with_name(f"{path.stem}_{index}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"无法生成不重复的输出文件名：{path.name}")


def _logo_has_useful_alpha(logo: Image.Image) -> bool:
    """True if the image already carries real transparency (skip hard knockout)."""
    if logo.mode != "RGBA":
        return False
    alpha = logo.getchannel("A")
    # Sample a grid; >2% non-opaque → treat as designed transparency
    w, h = logo.size
    step_x = max(1, w // 48)
    step_y = max(1, h // 48)
    transparent = total = 0
    px = alpha.load()
    for y in range(0, h, step_y):
        for x in range(0, w, step_x):
            total += 1
            if px[x, y] < 240:
                transparent += 1
    return total > 0 and (transparent / total) > 0.02


def _prepare_logo_rgba(logo_path: Path, opacity: float = 1.0) -> Image.Image:
    """Load logo as RGBA; match Reels watermark prep (opacity once, smart knockout).

    JPG/无透明通道时：仅当画面大面积近黑或近白才抠底，避免把白色 Logo 文字误抠成透明。
    """
    with Image.open(logo_path) as im:
        logo = im.convert("RGBA")
    if not _logo_has_useful_alpha(logo):
        pixels = logo.load()
        w, h = logo.size
        dark = bright = sampled = 0
        step_x = max(1, w // 64)
        step_y = max(1, h // 64)
        for y in range(0, h, step_y):
            for x in range(0, w, step_x):
                r, g, b, _a = pixels[x, y]
                lum = (r + g + b) // 3
                sampled += 1
                if lum <= 28:
                    dark += 1
                elif lum >= 250:
                    bright += 1
        knock_dark = sampled > 0 and (dark / sampled) >= 0.35
        knock_bright = (not knock_dark) and sampled > 0 and (bright / sampled) >= 0.45
        if knock_dark or knock_bright:
            for y in range(h):
                for x in range(w):
                    r, g, b, a = pixels[x, y]
                    if a < 8:
                        continue
                    lum = (r + g + b) // 3
                    if knock_dark and lum <= 28:
                        pixels[x, y] = (r, g, b, 0)
                    elif knock_bright and lum >= 250:
                        pixels[x, y] = (r, g, b, 0)
    if opacity < 0.999:
        r, g, b, a = logo.split()
        a = a.point(lambda v: int(v * max(0.0, min(1.0, opacity))))
        logo = Image.merge("RGBA", (r, g, b, a))
    return logo


def _corner_xy(position: str, base_w: int, base_h: int, mark_w: int, mark_h: int, margin: int):
    m = max(0, int(margin))
    mapping = {
        "左上": (m, m),
        "顶部居中": ((base_w - mark_w) // 2, m),
        "右上": (base_w - mark_w - m, m),
        "居中": ((base_w - mark_w) // 2, (base_h - mark_h) // 2),
        "左下": (m, base_h - mark_h - m),
        "底部居中": ((base_w - mark_w) // 2, base_h - mark_h - m),
        "右下": (base_w - mark_w - m, base_h - mark_h - m),
    }
    return mapping.get(position, mapping["右下"])


def _overlay_logo_on_image(base: Image.Image, logo_path: Path, cfg: dict) -> Image.Image:
    canvas = base.convert("RGBA")
    # 图片导出路径只在 Pillow 侧应用一次不透明度（与 Reels/视频滤镜「预乘后不再二次 aa」对齐）
    opacity = max(0.05, min(1.0, float(cfg.get("opacity", 100)) / 100.0))
    logo = _prepare_logo_rgba(logo_path, opacity)
    mode = str(cfg.get("mode") or "小 Logo 角标")
    if "全屏" in mode:
        logo = logo.resize(canvas.size, Image.Resampling.LANCZOS)
        x, y = 0, 0
    else:
        width_pct = max(4.0, min(80.0, float(cfg.get("width_pct", 18))))
        target_w = max(16, int(canvas.width * width_pct / 100.0))
        scale = target_w / max(1, logo.width)
        target_h = max(16, int(logo.height * scale))
        logo = logo.resize((target_w, target_h), Image.Resampling.LANCZOS)
        x, y = _corner_xy(
            str(cfg.get("position") or "右下"),
            canvas.width, canvas.height, logo.width, logo.height,
            int(cfg.get("margin", 28) or 28),
        )
    canvas.alpha_composite(logo, (max(0, x), max(0, y)))
    return canvas


def crop_image_to_916(image: Image.Image, target_size=None) -> Image.Image:
    """Center-crop to 9:16 without stretching. Optional high-quality resize after crop."""
    w, h = image.size
    if w < 2 or h < 2:
        return image
    target_ratio = 9.0 / 16.0
    current = w / float(h)
    if abs(current - target_ratio) > 0.004:
        if current > target_ratio:
            new_w = max(2, int(round(h * target_ratio)))
            left = max(0, (w - new_w) // 2)
            image = image.crop((left, 0, min(w, left + new_w), h))
        else:
            new_h = max(2, int(round(w / target_ratio)))
            top = max(0, (h - new_h) // 2)
            image = image.crop((0, top, w, min(h, top + new_h)))
    if target_size and len(target_size) == 2:
        tw, th = int(target_size[0]), int(target_size[1])
        if tw > 0 and th > 0 and (image.width != tw or image.height != th):
            image = image.resize((tw, th), Image.Resampling.LANCZOS)
    return image


def _probe_video_meta(ffmpeg, path: Path):
    """Return width, height, fps, video_bit_rate (bps, 0 if unknown)."""
    ffprobe = find_media_tool("ffprobe")
    if not ffprobe:
        return 0, 0, 0.0, 0
    command = [
        ffprobe, "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height,r_frame_rate,avg_frame_rate,bit_rate",
        "-show_entries", "format=bit_rate",
        "-of", "json", str(path),
    ]
    result = subprocess.run(
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace", **hidden_kwargs())
    if result.returncode:
        return 0, 0, 0.0, 0
    try:
        payload = json.loads(result.stdout or "{}")
        stream = (payload.get("streams") or [{}])[0]
        fmt = payload.get("format") or {}
        w = int(stream.get("width") or 0)
        h = int(stream.get("height") or 0)
        rate = str(stream.get("avg_frame_rate") or stream.get("r_frame_rate") or "0/1")
        fps = 0.0
        if "/" in rate:
            num, den = rate.split("/", 1)
            den_f = float(den) if float(den) else 1.0
            fps = float(num) / den_f if den_f else 0.0
        else:
            fps = float(rate or 0)
        br = 0
        try:
            br = int(stream.get("bit_rate") or 0)
        except (TypeError, ValueError):
            br = 0
        if br <= 0:
            try:
                br = int(fmt.get("bit_rate") or 0)
            except (TypeError, ValueError):
                br = 0
        return w, h, fps, max(0, br)
    except Exception:
        return 0, 0, 0.0, 0


def _probe_video_size(ffmpeg, path: Path):
    w, h, fps, _br = _probe_video_meta(ffmpeg, path)
    return w, h, fps


def _bitrate_floor_for_size(width: int, height: int) -> int:
    """Minimum video bitrate (bps) for clean 9:16 delivery at given pixels."""
    pixels = max(1, int(width) * int(height))
    # ~0.12 bit/pixel/frame @ 30fps ≈ 3.6 bpp-ish overall; clamp to sensible range
    # 1080x1920 → ~8 Mbps floor; 720x1280 → ~4.5; tiny 270x480 → ~1.2
    floor = int(pixels * 30 * 0.12)
    return max(1_200_000, min(16_000_000, floor))


def _hq_video_encode_args(ffmpeg, width: int = 0, height: int = 0, source_bitrate: int = 0) -> tuple[list, str]:
    """成品级 H.264：在肉眼难辨的前提下优先速度。

    策略（相对 1.7.45 偏快档略回调画质）：
    - CPU：medium + CRF14（接近旧 slow/CRF14 观感，仍比 slow 快）
    - NVENC：p5 + CQ15
    - QSV/AMF：质量档 + 码率下限
    返回 (ffmpeg_args, 简短说明)
    """
    encoder = resolve_encoder(ffmpeg, "auto")
    w = max(0, int(width or 0))
    h = max(0, int(height or 0))
    floor = _bitrate_floor_for_size(w or 1080, h or 1920)
    # 源码率更高时尽量贴近源，减少二次压缩损失
    target_br = max(floor, int(source_bitrate * 0.95) if source_bitrate > 500_000 else floor)
    maxrate = int(target_br * 1.5)
    bufsize = int(target_br * 2)

    if encoder == "nvenc":
        return [
            "-c:v", "h264_nvenc", "-preset", "p5", "-tune", "hq",
            "-rc", "vbr", "-cq", "15", "-b:v", str(target_br),
            "-maxrate", str(maxrate), "-bufsize", str(bufsize),
            "-spatial-aq", "1", "-temporal-aq", "1",
            "-profile:v", "high", "-bf", "0", "-pix_fmt", "yuv420p",
        ], "NVIDIA 硬编 p5/CQ15（高画质）"
    if encoder == "qsv":
        return [
            "-c:v", "h264_qsv", "-preset", "slow", "-global_quality", "16",
            "-look_ahead", "1", "-b:v", str(target_br),
            "-maxrate", str(maxrate), "-bufsize", str(bufsize),
            "-bf", "0", "-pix_fmt", "nv12",
        ], "Intel QSV slow（高画质硬编）"
    if encoder == "mf":
        # Windows MF：质量档拉满 + 码率下限
        return [
            "-c:v", "h264_mf", "-rate_control", "quality", "-quality", "100",
            "-b:v", str(target_br), "-maxrate", str(maxrate),
            "-pix_fmt", "yuv420p",
        ], "Windows 硬编 h264_mf（最高质量档）"
    if encoder == "amf":
        return [
            "-c:v", "h264_amf", "-quality", "quality",
            "-rc", "vbr_peak", "-b:v", str(target_br),
            "-maxrate", str(maxrate),
            "-qp_i", "15", "-qp_p", "17", "-bf_delta_qp", "0",
            "-pix_fmt", "yuv420p",
        ], "AMD AMF quality（高画质硬编）"
    # CPU libx264：medium+14 兼顾速度与接近旧版 slow/14 的清晰度
    return [
        "-c:v", "libx264", "-preset", "medium", "-crf", "14",
        "-profile:v", "high", "-level", "4.2",
        "-aq-mode", "1", "-bf", "0", "-pix_fmt", "yuv420p", "-threads", "0",
        "-maxrate", str(maxrate), "-bufsize", str(bufsize),
    ], "CPU libx264 medium/CRF14（高画质）"


def _effective_crop_target(source_w: int, source_h: int, mode_key: str):
    """计算裁切后是否缩放，以及目标尺寸。

    规则（避免糊）：
    - keep：只居中裁 9:16，绝不缩放
    - 指定 720/1080/1440：仅当裁后像素 **大于** 目标时缩小；源更小则保持源像素（绝不放大）
    返回 (target_wh_or_None, note)
    """
    mode = str(mode_key or "keep")
    wanted = _CROP_916_MODES.get(mode)
    if not wanted:
        return None, "仅居中裁切（不缩放，画质最佳）"
    tw, th = int(wanted[0]), int(wanted[1])
    # 裁后近似尺寸（与 FFmpeg crop 表达式一致）
    if source_w > 0 and source_h > 0:
        crop_w = min(source_w, int(source_h * 9 / 16))
        crop_h = min(source_h, int(source_w * 16 / 9))
        crop_w = max(2, (crop_w // 2) * 2)
        crop_h = max(2, (crop_h // 2) * 2)
    else:
        crop_w, crop_h = tw, th
    # 源/裁后任一边小于目标 → 放大会糊，跳过缩放
    if crop_w < tw or crop_h < th:
        return None, (
            f"目标 {tw}×{th} 大于裁后约 {crop_w}×{crop_h}，"
            f"已禁止放大（保持源像素，避免发糊）"
        )
    return (tw, th), f"缩小到 {tw}×{th}（Lanczos，不变形）"


def _is_nearly_916(width: int, height: int, tol: float = 0.012) -> bool:
    if width < 2 or height < 2:
        return False
    return abs((width / float(height)) - (9.0 / 16.0)) <= tol


# 仅在正常打开失败后使用；完好文件仍走纯流复制，不预探测、不伤画质
_RECOVERY_INPUT_FLAGS = [
    "-err_detect", "ignore_err",
    "-fflags", "+genpts+igndts+discardcorrupt",
]


def _is_container_corrupt_error(text: str) -> bool:
    low = (text or "").lower()
    needles = (
        "stsc",
        "stco",
        "contradict",
        "error reading header",
        "invalid data found",
        "moov atom not found",
        "error opening input",
        "could not find codec parameters",
        "partial file",
    )
    return any(n in low for n in needles)


def _friendly_ffmpeg_error(stderr: str, *, stage: str = "") -> str:
    """把常见 FFmpeg 英文错误翻成用户可读中文。"""
    text = (stderr or "").strip()
    prefix = f"【{stage}】" if stage else ""
    if _is_container_corrupt_error(text):
        return (
            f"{prefix}源视频容器索引损坏（常见：STSC/STCO 矛盾 / Invalid data / 读头失败）。"
            "已尝试自动修复仍无法打开。"
            "建议：用能正常播放的播放器「另存为/重新导出」，或换一份完整源文件后再清理元数据。"
        )
    low = text.lower()
    if "no such file" in low:
        return f"{prefix}找不到输入文件。"
    if "permission denied" in low or "拒绝访问" in text:
        return f"{prefix}没有写入权限，请换输出目录或关闭占用该文件的程序。"
    if "disk" in low and ("space" in low or "full" in low):
        return f"{prefix}磁盘空间不足。"
    tail = text[-320:] if len(text) > 320 else text
    if not tail:
        return f"{prefix}FFmpeg 处理失败。"
    return f"{prefix}处理失败：{tail}"


def _output_looks_valid(path: Path, *, min_bytes: int = 1024) -> bool:
    try:
        return path.is_file() and path.stat().st_size >= min_bytes
    except OSError:
        return False


class MetadataWorker(QObject):
    log = Signal(str)
    progress = Signal(int)
    finished = Signal(bool, str)
    file_done = Signal(str, str)

    def __init__(
        self,
        files,
        output,
        keep_structure=True,
        preserve_time=False,
        watermark=None,
        crop_916=False,
        crop_916_mode="keep",
        rotate_mode="none",
    ):
        super().__init__()
        self.files = [Path(value) for value in files]
        self.output = Path(output)
        self.keep_structure = keep_structure
        self.preserve_time = preserve_time
        self.watermark = watermark if isinstance(watermark, dict) else None
        self.crop_916 = bool(crop_916)
        self.crop_916_mode = str(crop_916_mode or "keep")
        # none | 180 | 90cw | 90ccw
        self.rotate_mode = str(rotate_mode or "none").strip().lower()
        self.cancelled = False
        self._proc: subprocess.Popen | None = None
        self._proc_lock = threading.Lock()
        self._repair_paths: list[Path] = []

    def _rotate_enabled(self) -> bool:
        return self.rotate_mode in ("180", "90cw", "90ccw")

    def _rotate_vf(self) -> str:
        """FFmpeg 画面旋转（真正改像素，不是只写旋转元数据）。"""
        return {
            "180": "hflip,vflip",
            "90cw": "transpose=1",
            "90ccw": "transpose=2",
        }.get(self.rotate_mode, "")

    def _rotate_label(self) -> str:
        return {
            "180": "旋转 180°（倒着拍放正）",
            "90cw": "顺时针 90°",
            "90ccw": "逆时针 90°",
        }.get(self.rotate_mode, "")

    def cancel(self):
        self.cancelled = True
        with self._proc_lock:
            proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass
            try:
                proc.kill()
            except Exception:
                pass

    def _probe_duration_sec(self, ffmpeg: str, source: Path) -> float:
        ffprobe = str(Path(ffmpeg).with_name("ffprobe" + Path(ffmpeg).suffix))
        if not Path(ffprobe).is_file():
            ffprobe = find_media_tool("ffprobe") or "ffprobe"
        try:
            result = subprocess.run(
                [
                    ffprobe, "-v", "error", "-show_entries", "format=duration",
                    "-of", "default=noprint_wrappers=1:nokey=1", str(source),
                ],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, encoding="utf-8", errors="replace",
                timeout=30, **hidden_kwargs(),
            )
            return max(0.0, float((result.stdout or "").strip() or 0))
        except Exception:
            return 0.0

    def _run_ffmpeg(
        self,
        command: list,
        *,
        source: Path | None = None,
        label: str = "FFmpeg",
        duration_hint: float = 0.0,
    ) -> subprocess.CompletedProcess:
        """运行 FFmpeg：实时读进度、心跳日志、可停止；避免 PIPE 死锁。"""
        if self.cancelled:
            raise RuntimeError("任务已停止；已完成的文件仍保留在输出目录。")

        duration = duration_hint
        if duration <= 0.1 and source is not None:
            duration = self._probe_duration_sec(command[0], source)

        # -progress pipe:1 输出 key=value；-nostats 减少 stderr 噪音
        cmd = list(command)
        # 在输出路径前插入 progress（最后一个非 flag 参数前）
        insert_at = len(cmd) - 1
        cmd[insert_at:insert_at] = ["-progress", "pipe:1", "-nostats"]

        creation = hidden_kwargs()
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                **creation,
            )
        except Exception as exc:
            raise RuntimeError(f"无法启动 FFmpeg：{exc}") from exc

        with self._proc_lock:
            self._proc = proc

        err_chunks: list[str] = []
        last_out_ms = 0
        last_beat = time.monotonic()
        started = last_beat

        def _drain_stderr():
            try:
                assert proc.stderr is not None
                for line in proc.stderr:
                    if line:
                        err_chunks.append(line)
                        if len(err_chunks) > 200:
                            del err_chunks[:50]
            except Exception:
                pass

        err_thread = threading.Thread(target=_drain_stderr, name="meta-ff-err", daemon=True)
        err_thread.start()

        try:
            assert proc.stdout is not None
            for raw in proc.stdout:
                if self.cancelled:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    raise RuntimeError("任务已停止；已完成的文件仍保留在输出目录。")
                line = (raw or "").strip()
                if not line or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip()
                if key == "out_time_ms":
                    try:
                        last_out_ms = int(float(val))
                    except ValueError:
                        pass
                elif key == "out_time":
                    # 00:00:12.345678
                    try:
                        parts = val.split(":")
                        if len(parts) == 3:
                            last_out_ms = int(
                                (float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2]))
                                * 1000
                            )
                    except ValueError:
                        pass
                now = time.monotonic()
                if now - last_beat >= 8.0:
                    last_beat = now
                    elapsed = now - started
                    if duration > 0.5 and last_out_ms > 0:
                        pct = min(99.0, last_out_ms / (duration * 1000.0) * 100.0)
                        self.log.emit(
                            f"  · {label}进行中：约 {pct:.0f}% "
                            f"（已编码 {last_out_ms / 1000:.1f}s / 片长 {duration:.1f}s，"
                            f"耗时 {elapsed:.0f}s）"
                        )
                    else:
                        self.log.emit(
                            f"  · {label}进行中：已运行 {elapsed:.0f}s"
                            f"{f'，已输出约 {last_out_ms / 1000:.1f}s' if last_out_ms else ''}"
                        )
            rc = proc.wait(timeout=6 * 3600)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except Exception:
                pass
            raise RuntimeError(f"{label}超时（超过 6 小时），已终止")
        finally:
            with self._proc_lock:
                if self._proc is proc:
                    self._proc = None
            err_thread.join(timeout=2.0)

        stderr = "".join(err_chunks)
        return subprocess.CompletedProcess(cmd, rc, stdout="", stderr=stderr)

    def _wm_enabled(self) -> bool:
        if not self.watermark or not self.watermark.get("enabled"):
            return False
        path = Path(str(self.watermark.get("path") or ""))
        return path.is_file()

    def _crop_target_size(self, source_w: int = 0, source_h: int = 0):
        """兼容旧调用；真正策略见 _effective_crop_target。"""
        if not self.crop_916:
            return None
        target, _note = _effective_crop_target(source_w, source_h, self.crop_916_mode)
        return target

    def _image(self, source: Path, destination: Path):
        with Image.open(source) as image:
            clean = Image.new(image.mode, image.size)
            clean.putdata(list(image.getdata()))
            if self._rotate_enabled():
                before = (clean.width, clean.height)
                if self.rotate_mode == "180":
                    clean = clean.rotate(180, expand=True)
                elif self.rotate_mode == "90cw":
                    clean = clean.transpose(Image.Transpose.ROTATE_270)  # PIL: 90 CW
                elif self.rotate_mode == "90ccw":
                    clean = clean.transpose(Image.Transpose.ROTATE_90)
                self.log.emit(
                    f"  · {self._rotate_label()}：{before[0]}×{before[1]} → "
                    f"{clean.width}×{clean.height}"
                )
            if self.crop_916:
                before = (clean.width, clean.height)
                target, note = _effective_crop_target(before[0], before[1], self.crop_916_mode)
                clean = crop_image_to_916(clean, target)
                self.log.emit(
                    f"  · 9:16 居中裁切：{before[0]}×{before[1]} → {clean.width}×{clean.height}"
                    f"（不拉伸；{note}）"
                )
            if self._wm_enabled():
                self.log.emit(f"  · 叠加水印：{Path(self.watermark['path']).name}")
                composed = _overlay_logo_on_image(
                    clean.convert("RGBA"), Path(self.watermark["path"]), self.watermark)
                if source.suffix.lower() in {".jpg", ".jpeg"}:
                    clean = composed.convert("RGB")
                else:
                    clean = composed
            options = {}
            suffix = source.suffix.lower()
            if suffix in {".jpg", ".jpeg"}:
                if clean.mode not in {"RGB", "L"}:
                    clean = clean.convert("RGB")
                options = {"quality": 95, "optimize": True, "subsampling": 0}
            elif suffix == ".png":
                if clean.mode not in {"RGB", "RGBA", "L", "LA", "P"}:
                    clean = clean.convert("RGBA")
                options = {"optimize": True}
            clean.save(destination, **options)

    def _run_simple_ffmpeg(self, command: list) -> subprocess.CompletedProcess:
        return subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            **hidden_kwargs(),
        )

    def _cleanup_repairs(self):
        for path in list(self._repair_paths):
            try:
                if path.is_file():
                    path.unlink()
            except OSError:
                pass
        self._repair_paths.clear()
        cache = self.output / ".repair_cache"
        try:
            if cache.is_dir() and not any(cache.iterdir()):
                cache.rmdir()
        except OSError:
            pass

    def _try_repair_container(self, ffmpeg, source: Path) -> Path | None:
        """损坏容器抢救：优先流复制（零画质损失），最后才对坏片重编码。

        完好文件不会走到这里。返回可读临时片路径；无法修复则返回 None。
        """
        if self.cancelled:
            raise RuntimeError("任务已停止；已完成的文件仍保留在输出目录。")

        cache = self.output / ".repair_cache"
        cache.mkdir(parents=True, exist_ok=True)
        stem = source.stem[:48] or "repaired"
        attempts: list[tuple[str, list]] = [
            (
                "容错流复制（画质不变）",
                [
                    ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                    *_RECOVERY_INPUT_FLAGS, "-i", str(source),
                    "-map", "0:V?", "-map", "0:a?",
                    "-c", "copy", "-movflags", "+faststart",
                ],
            ),
            (
                "容错流复制（仅主音视频轨）",
                [
                    ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                    *_RECOVERY_INPUT_FLAGS, "-i", str(source),
                    "-map", "0:v:0?", "-map", "0:a:0?",
                    "-c", "copy", "-movflags", "+faststart",
                ],
            ),
            (
                "容错重编码抢救（仅坏片最后手段）",
                [
                    ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                    *_RECOVERY_INPUT_FLAGS, "-i", str(source),
                    "-map", "0:v:0?", "-map", "0:a:0?",
                    "-c:v", "libx264", "-preset", "medium", "-crf", "18",
                    "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-b:a", "192k",
                    "-movflags", "+faststart",
                ],
            ),
        ]
        last_err = ""
        for label, base_cmd in attempts:
            if self.cancelled:
                raise RuntimeError("任务已停止；已完成的文件仍保留在输出目录。")
            out = unique_path(cache / f"{stem}_fix{source.suffix.lower() or '.mp4'}")
            if out.suffix.lower() not in {".mp4", ".mov", ".m4v", ".mkv"}:
                out = out.with_suffix(".mp4")
            self.log.emit(f"  · 自动修复：{label}…")
            result = self._run_simple_ffmpeg([*base_cmd, str(out)])
            last_err = (result.stderr or "").strip()
            if result.returncode == 0 and _output_looks_valid(out):
                # 音频-only 或视频轨：至少能被 probe 打开
                if source.suffix.lower() in AUDIO_EXTENSIONS:
                    self._repair_paths.append(out)
                    self.log.emit(f"  · 自动修复成功（{label}），继续清理元数据")
                    return out
                w, h, _fps = _probe_video_size(ffmpeg, out)
                if w > 0 and h > 0:
                    self._repair_paths.append(out)
                    self.log.emit(f"  · 自动修复成功（{label}），继续清理元数据")
                    return out
                # 纯音频伪装扩展名
                if source.suffix.lower() in AUDIO_EXTENSIONS or w == 0:
                    # 再确认文件非空即可
                    if _output_looks_valid(out, min_bytes=256):
                        self._repair_paths.append(out)
                        self.log.emit(f"  · 自动修复成功（{label}），继续清理元数据")
                        return out
            try:
                if out.exists():
                    out.unlink()
            except OSError:
                pass
            if last_err:
                self.log.emit(f"  · {label}未成功")
        self.log.emit(
            "  · 自动修复未能打开该文件（容器索引可能已彻底损坏）"
            + (f"：{last_err[-180:]}" if last_err else "")
        )
        return None

    def _ensure_readable_av(self, ffmpeg, source: Path, *, err_hint: str = "") -> Path:
        """正常路径失败后才抢救；成功返回可用源（原片或临时修复片）。"""
        hint = err_hint or ""
        if hint and not _is_container_corrupt_error(hint):
            # 非容器损坏类错误：仍尝试一轮容错 copy 修复（少数 Invalid data 变体）
            pass
        self.log.emit("  · 检测到可能的容器异常，尝试自动修复（优先流复制，不伤画质）…")
        repaired = self._try_repair_container(ffmpeg, source)
        if repaired is not None:
            return repaired
        raise RuntimeError(_friendly_ffmpeg_error(hint or "Invalid data found when processing input", stage="清理元数据"))

    def _maybe_repair_and_retry(self, ffmpeg, source: Path, exc: BaseException):
        """仅容器损坏类错误才抢救；其它错误原样抛出。"""
        msg = str(exc)
        if not _is_container_corrupt_error(msg):
            raise
        return self._ensure_readable_av(ffmpeg, source, err_hint=msg)

    def _av(self, source: Path, destination: Path):
        ffmpeg = find_media_tool("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("未找到 FFmpeg，请先到“设置与组件”一键安装。")
        work = source
        # 纯音频：只清元数据
        if source.suffix.lower() in AUDIO_EXTENSIONS:
            try:
                self._av_copy_clean(ffmpeg, work, destination)
            except RuntimeError as exc:
                work = self._maybe_repair_and_retry(ffmpeg, source, exc)
                self._av_copy_clean(ffmpeg, work, destination)
            return
        need_reencode = self._wm_enabled() or self.crop_916 or self._rotate_enabled()
        if not need_reencode:
            try:
                self._av_copy_clean(ffmpeg, work, destination)
            except RuntimeError as exc:
                work = self._maybe_repair_and_retry(ffmpeg, source, exc)
                self._av_copy_clean(ffmpeg, work, destination)
            return
        # 已是 9:16 且仅裁切、无水印：可走流复制
        if (
            self.crop_916
            and not self._wm_enabled()
            and not self._rotate_enabled()
            and self.crop_916_mode == "keep"
        ):
            w, h, _fps = _probe_video_size(ffmpeg, work)
            if _is_nearly_916(w, h):
                self.log.emit(
                    f"  · 画面已是 9:16（{w}×{h}），跳过裁切，仅清除元数据（流复制、零画质损失）。"
                )
                try:
                    self._av_copy_clean(ffmpeg, work, destination)
                except RuntimeError as exc:
                    work = self._maybe_repair_and_retry(ffmpeg, source, exc)
                    self._av_copy_clean(ffmpeg, work, destination)
                return
        try:
            self._av_reencode(ffmpeg, work, destination)
        except RuntimeError as exc:
            work = self._maybe_repair_and_retry(ffmpeg, source, exc)
            self._av_reencode(ffmpeg, work, destination)

    def _av_copy_clean(self, ffmpeg, source: Path, destination: Path):
        attempts = [
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
             "-map", "0:V?", "-map", "0:a?", "-map", "0:s?",
             *_META_STRIP, "-c", "copy", str(destination)],
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
             "-map", "0:V?", "-map", "0:a?",
             *_META_STRIP, "-c", "copy", str(destination)],
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
             "-map", "0:V?", "-map", "0:a?", "-map_metadata", "-1",
             "-map_metadata:s", "-1", "-map_chapters", "-1", "-metadata", "creation_time=",
             "-metadata", "location=", "-metadata", "title=", "-metadata", "artist=",
             "-metadata", "copyright=", "-metadata", "comment=", "-c", "copy", str(destination)],
            # 容错输入 + 流复制（仍零画质损失；仅前面失败才执行）
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
             *_RECOVERY_INPUT_FLAGS, "-i", str(source),
             "-map", "0:V?", "-map", "0:a?",
             *_META_STRIP, "-c", "copy", str(destination)],
        ]
        result = None
        for command in attempts:
            if self.cancelled:
                raise RuntimeError("任务已停止；已完成的文件仍保留在输出目录。")
            result = self._run_simple_ffmpeg(command)
            if result.returncode == 0 and _output_looks_valid(destination, min_bytes=256):
                return
            try:
                if destination.exists() and destination.stat().st_size < 256:
                    destination.unlink()
            except OSError:
                pass
        err = (result.stderr if result else "") or "FFmpeg 清除元数据失败"
        raise RuntimeError(_friendly_ffmpeg_error(err, stage="清除元数据"))

    def _build_video_chain(self, source_w: int = 0, source_h: int = 0) -> tuple[str, tuple | None, str]:
        """Build video filter chain. Returns (vf, out_size_or_None, scale_note).

        - 居中裁 9:16，不拉伸
        - 目标分辨率仅允许缩小，禁止放大（小图强制 1080p 会发糊）
        - 不改 fps
        """
        parts = ["setpts=PTS-STARTPTS"]
        out_w, out_h = int(source_w or 0), int(source_h or 0)
        scale_note = ""
        rot = self._rotate_vf()
        if rot:
            parts.append(rot)
            # 90° 交换宽高供后续裁切估算
            if self.rotate_mode in ("90cw", "90ccw") and out_w and out_h:
                out_w, out_h = out_h, out_w
                source_w, source_h = out_w, out_h
        parts.append("format=yuv420p")
        if self.crop_916:
            parts.append(_CROP_916_VF)
            # 估算裁后尺寸
            if source_w > 0 and source_h > 0:
                out_w = min(source_w, int(source_h * 9 / 16))
                out_h = min(source_h, int(source_w * 16 / 9))
                out_w = max(2, (out_w // 2) * 2)
                out_h = max(2, (out_h // 2) * 2)
            target, scale_note = _effective_crop_target(source_w, source_h, self.crop_916_mode)
            if target:
                tw, th = int(target[0]), int(target[1])
                # Lanczos 缩小；accurate_rnd + full_chroma 更锐利
                parts.append(
                    f"scale={tw}:{th}:flags=lanczos+accurate_rnd+full_chroma_int"
                )
                parts.append("setsar=1")
                out_w, out_h = tw, th
        # yuv420p 偶数边（裁切表达式已保证偶数，这里兜底）
        parts.append("scale=trunc(iw/2)*2:trunc(ih/2)*2")
        return ",".join(parts), ((out_w, out_h) if out_w and out_h else None), scale_note

    def _av_reencode(self, ffmpeg, source: Path, destination: Path):
        """Re-encode with optional 9:16 crop and/or watermark; strip metadata.

        Quality rules:
        - Center crop only (no stretch)
        - Never upscale to “1080×1920” when source is smaller (that causes blur)
        - No fps filter (preserve source frame rate)
        - Delivery-grade H.264（medium/CRF15 或硬编高画质，兼顾速度）
        """
        w, h, fps, src_br = _probe_video_meta(ffmpeg, source)
        notes = []
        v_prep, out_size, scale_note = self._build_video_chain(w, h)
        if self._rotate_enabled():
            notes.append(self._rotate_label())
            if w and h and self.rotate_mode in ("90cw", "90ccw"):
                notes.append(f"源 {w}×{h} → 旋转后约 {h}×{w}")
        if self.crop_916:
            if w and h and not self._rotate_enabled():
                notes.append(f"源 {w}×{h}")
            if scale_note:
                notes.append(scale_note)
            else:
                notes.append("9:16 居中裁切（不缩放）")
            if out_size:
                notes.append(f"输出约 {out_size[0]}×{out_size[1]}")
        if self._wm_enabled():
            notes.append(f"水印 {Path(self.watermark['path']).name}")
        if fps > 0.1:
            notes.append(f"保留约 {fps:.3g} fps")
        if src_br > 0:
            notes.append(f"源码率约 {src_br / 1_000_000:.2f} Mbps")
        self.log.emit("  · " + "；".join(notes) + "（高画质重编码，不变形、不放大）")

        ow = int(out_size[0]) if out_size else w
        oh = int(out_size[1]) if out_size else h
        encode, enc_note = _hq_video_encode_args(ffmpeg, ow, oh, src_br)
        self.log.emit(
            f"  · 编码器：{enc_note}；进度会周期性更新，可点「停止」"
        )
        # 不强制 -r，避免改帧率导致重复/丢帧；passthrough 尽量按源包时间戳
        rate_args = ["-fps_mode", "passthrough"]
        duration = self._probe_duration_sec(ffmpeg, source)

        if self._wm_enabled():
            logo = Path(self.watermark["path"])
            mode = str(self.watermark.get("mode") or "小 Logo 角标")
            opacity = max(0.05, min(1.0, float(self.watermark.get("opacity", 100)) / 100.0))
            width_pct = max(0.04, min(0.80, float(self.watermark.get("width_pct", 18)) / 100.0))
            margin = max(0, int(self.watermark.get("margin", 28) or 28))
            position = str(self.watermark.get("position") or "右下")
            pos_map = {
                "左上": (str(margin), str(margin)),
                "顶部居中": ("(main_w-overlay_w)/2", str(margin)),
                "右上": (f"main_w-overlay_w-{margin}", str(margin)),
                "居中": ("(main_w-overlay_w)/2", "(main_h-overlay_h)/2"),
                "左下": (str(margin), f"main_h-overlay_h-{margin}"),
                "底部居中": ("(main_w-overlay_w)/2", f"main_h-overlay_h-{margin}"),
                "右下": (f"main_w-overlay_w-{margin}", f"main_h-overlay_h-{margin}"),
            }
            ox, oy = pos_map.get(position, pos_map["右下"])

            cache = self.output / ".watermark_cache"
            cache.mkdir(parents=True, exist_ok=True)
            prepared = cache / f"{logo.stem}_o{int(opacity * 100)}.png"
            try:
                prepared_im = _prepare_logo_rgba(logo, opacity)
                # 角标模式：先缩小到合理像素，避免每帧 scale2ref 超大 PNG
                if "全屏" not in mode and prepared_im.width > 1200:
                    tw = 1200
                    th = max(16, int(prepared_im.height * (tw / prepared_im.width)))
                    prepared_im = prepared_im.resize((tw, th), Image.Resampling.LANCZOS)
                prepared_im.save(prepared, "PNG")
                logo_input = prepared
            except Exception:
                logo_input = logo

            # 与 Reels 一致：若 PNG 已在 _prepare_logo_rgba 里写好透明度，
            # 滤镜里不要再 colorchannelmixer 叠一次（否则 90%×90% 看起来发灰发透）。
            prepared_has_alpha = logo_input == prepared
            if prepared_has_alpha:
                wm_in = "[1:v]format=rgba[wmraw]"
            else:
                wm_in = f"[1:v]format=rgba,colorchannelmixer=aa={opacity:.3f}[wmraw]"
            if "全屏" in mode:
                fc = (
                    f"[0:v]{v_prep}[base];"
                    f"{wm_in};"
                    f"[wmraw][base]scale2ref=w=iw:h=ih[wm][base2];"
                    f"[base2][wm]overlay=0:0:format=auto:eof_action=repeat[vout]"
                )
            else:
                fc = (
                    f"[0:v]{v_prep}[base];"
                    f"{wm_in};"
                    f"[wmraw][base]scale2ref=w=main_w*{width_pct:.4f}:h=ow/mdar[wm][base2];"
                    f"[base2][wm]overlay={ox}:{oy}:format=auto:eof_action=repeat[vout]"
                )

            # -t 限制片长，防止 loop 水印在个别环境下拖成无限编码
            t_args = ["-t", f"{duration:.3f}"] if duration > 0.1 else []
            command = [
                ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(source),
                "-loop", "1", *t_args, "-i", str(logo_input),
                "-filter_complex", fc,
                "-map", "[vout]", "-map", "0:a?",
                *encode,
                "-c:a", "copy",
                *rate_args,
                *_META_STRIP,
                "-shortest", "-movflags", "+faststart",
                str(destination),
            ]
            result = self._run_ffmpeg(
                command, source=source, label="9:16/水印重编码", duration_hint=duration,
            )
            # 音频 copy 失败时回退 AAC
            if result.returncode:
                if self.cancelled:
                    raise RuntimeError("任务已停止；已完成的文件仍保留在输出目录。")
                self.log.emit("  · 音轨复制失败，改为 AAC 重试…")
                command = [
                    ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                    "-i", str(source),
                    "-loop", "1", *t_args, "-i", str(logo_input),
                    "-filter_complex", fc,
                    "-map", "[vout]", "-map", "0:a?",
                    *encode,
                    "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
                    *rate_args,
                    *_META_STRIP,
                    "-shortest", "-movflags", "+faststart",
                    str(destination),
                ]
                result = self._run_ffmpeg(
                    command, source=source, label="9:16/水印重编码(AAC)", duration_hint=duration,
                )
            if result.returncode or not destination.is_file() or destination.stat().st_size < 1024:
                raise RuntimeError(
                    _friendly_ffmpeg_error(
                        (result.stderr or "").strip() or "FFmpeg 处理失败（9:16/水印）",
                        stage="9:16/水印",
                    )
                )
            return

        # 仅裁切 + 清元数据
        command = [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(source),
            "-vf", v_prep,
            "-map", "0:v:0", "-map", "0:a?",
            *encode,
            "-c:a", "copy",
            *rate_args,
            *_META_STRIP,
            "-movflags", "+faststart",
            str(destination),
        ]
        result = self._run_ffmpeg(
            command, source=source, label="9:16 裁切重编码", duration_hint=duration,
        )
        if result.returncode:
            if self.cancelled:
                raise RuntimeError("任务已停止；已完成的文件仍保留在输出目录。")
            # 部分容器/编码无法 copy 音轨：重编码音频
            command = [
                ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(source),
                "-vf", v_prep,
                "-map", "0:v:0", "-map", "0:a?",
                *encode,
                "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
                *rate_args,
                *_META_STRIP,
                "-movflags", "+faststart",
                str(destination),
            ]
            result = self._run_ffmpeg(
                command, source=source, label="9:16 裁切重编码(AAC)", duration_hint=duration,
            )
        # 旧版 FFmpeg 可能不认 fps_mode=passthrough
        if result.returncode and "fps_mode" in (result.stderr or ""):
            if self.cancelled:
                raise RuntimeError("任务已停止；已完成的文件仍保留在输出目录。")
            command = [
                ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(source),
                "-vf", v_prep,
                "-map", "0:v:0", "-map", "0:a?",
                *encode,
                "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
                *_META_STRIP,
                "-movflags", "+faststart",
                str(destination),
            ]
            result = self._run_ffmpeg(
                command, source=source, label="9:16 裁切重编码(兼容)", duration_hint=duration,
            )
        if result.returncode or not destination.is_file() or destination.stat().st_size < 1024:
            raise RuntimeError(
                _friendly_ffmpeg_error(
                    (result.stderr or "").strip() or "FFmpeg 9:16 裁切失败",
                    stage="9:16裁切",
                )
            )

    def run(self):
        failed_names: list[str] = []
        try:
            self.output.mkdir(parents=True, exist_ok=True)
            common = None
            if self.keep_structure and self.files:
                try:
                    common = Path(os.path.commonpath([str(path.parent) for path in self.files]))
                except ValueError:
                    common = None
            completed = 0
            failed = 0
            total = max(1, len(self.files))
            extras = []
            if self._rotate_enabled():
                extras.append(self._rotate_label())
            if self.crop_916:
                extras.append("9:16裁切")
            if self._wm_enabled():
                extras.append("水印")
            note = f"（清理+{'+'.join(extras)}）" if extras else "（清理元数据）"
            for index, source in enumerate(self.files, 1):
                if self.cancelled:
                    raise RuntimeError("任务已停止；已完成的文件仍保留在输出目录。")
                relative_parent = Path()
                if common:
                    try:
                        relative_parent = source.parent.relative_to(common)
                    except ValueError:
                        pass
                destination_dir = self.output / relative_parent
                destination_dir.mkdir(parents=True, exist_ok=True)
                destination = unique_path(destination_dir / source.name)
                self.log.emit(f"正在处理{note} [{index}/{len(self.files)}]：{source.name}")
                try:
                    if source.suffix.lower() in IMAGE_EXTENSIONS:
                        self._image(source, destination)
                    else:
                        self._av(source, destination)
                    if self.preserve_time and destination.is_file():
                        stat = source.stat()
                        os.utime(destination, (stat.st_atime, stat.st_mtime))
                    completed += 1
                    self.log.emit(f"完成：{destination}")
                    self.file_done.emit(str(source), str(destination))
                except RuntimeError as exc:
                    if self.cancelled or "任务已停止" in str(exc):
                        raise
                    failed += 1
                    failed_names.append(source.name)
                    self.log.emit(f"跳过：{source.name} — {exc}")
                    try:
                        if destination.exists() and destination.stat().st_size < 1024:
                            destination.unlink()
                    except OSError:
                        pass
                except Exception as exc:
                    failed += 1
                    failed_names.append(source.name)
                    self.log.emit(f"跳过：{source.name} — {exc}")
                    try:
                        if destination.exists() and destination.stat().st_size < 1024:
                            destination.unlink()
                    except OSError:
                        pass
                self.progress.emit(round(index / total * 100))
            self._cleanup_repairs()
            ops = "清除元数据"
            if self._rotate_enabled():
                ops += f" + {self._rotate_label()}"
            if self.crop_916:
                ops += " + 9:16 居中裁切"
            if self._wm_enabled():
                ops += " + 水印合成"
            if completed == 0 and failed > 0:
                detail = "；".join(failed_names[:5])
                more = f" 等 {failed} 个" if failed > 5 else ""
                self.finished.emit(
                    False,
                    f"全部失败（{failed} 个）。{ops}未产出可用文件。\n{detail}{more}\n{self.output}",
                )
                return
            msg = f"已处理 {completed} 个素材（{ops}）"
            if failed:
                msg += f"，跳过 {failed} 个失败文件"
            msg += f"。\n{self.output}"
            self.finished.emit(True, msg)
        except Exception as exc:
            self._cleanup_repairs()
            self.finished.emit(False, str(exc))


class MetadataPage(QWidget):
    def __init__(self):
        super().__init__()
        self.thread = None
        self.worker = None
        self.files = []
        self.cleaned_files = {}
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 14, 18, 14)
        root.setSpacing(8)
        title = QLabel("批量清除素材元数据")
        title.setObjectName("heading")
        root.addWidget(title)
        note = QLabel(
            "隐私清理会强制删除 GPS、拍摄时间、设备/序列号、作者版权、唯一标识、标题描述、软件来源、"
            "章节、附件和封面图；图片重建像素并清除 EXIF/XMP/IPTC。原文件不会被修改。"
            "可选「旋转校正」：倒着拍的视频/图片转正（真正改像素，不是只改元数据）。"
            "可选「9:16 裁切」：所有视频/图片居中裁成竖屏，不拉伸变形，不强制改帧率，高质量编码。"
            "可选「水印合成」：清理同时把 Logo 烧进画面（视频会重编码）。"
            "注意：文件名以及画面、声音中直接出现的隐私内容需要另行处理。"
        )
        note.setWordWrap(True)
        note.setStyleSheet("color:#94a3b8;")
        root.addWidget(note)
        split = QSplitter()
        source = QGroupBox("素材队列（支持拖入文件或文件夹）")
        source_layout = QVBoxLayout(source)
        self.list = DropListWidget()
        self.list.paths_dropped.connect(self.add_paths)
        self.list.currentTextChanged.connect(self.inspect_selected)
        source_layout.addWidget(self.list, 1)
        buttons = QHBoxLayout()
        add_files = QPushButton("添加文件")
        add_files.clicked.connect(self.choose_files)
        add_folder = QPushButton("添加文件夹")
        add_folder.clicked.connect(self.choose_folder)
        remove = QPushButton("移除选中")
        remove.clicked.connect(lambda: [self.list.takeItem(i.row()) for i in reversed(self.list.selectedIndexes())])
        clear = QPushButton("清空")
        clear.clicked.connect(self.list.clear)
        for button in (add_files, add_folder, remove, clear):
            buttons.addWidget(button)
        source_layout.addLayout(buttons)

        # 右侧：内容固定最小宽度 + 纵向滚动。
        # 禁止把表单压成「一字竖排」（窄宽度时 QGrid 双列会把中文挤成竖排乱码）。
        LABEL_W = 88
        CTRL_H = 34
        CONTENT_MIN_W = 400

        def _row_label(text: str) -> QLabel:
            lab = QLabel(text)
            lab.setFixedWidth(LABEL_W)
            lab.setMinimumHeight(CTRL_H)
            lab.setAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight)
            lab.setStyleSheet(
                "QLabel{font-size:13px;font-weight:600;color:#e2e8f0;"
                "padding-right:8px;background:transparent;}"
            )
            # 禁止在极窄宽度下自动换行成竖排
            lab.setWordWrap(False)
            lab.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
            return lab

        def _labeled_row(label: str, *widgets) -> QHBoxLayout:
            row = QHBoxLayout()
            row.setSpacing(8)
            row.setContentsMargins(0, 0, 0, 0)
            row.addWidget(_row_label(label), 0)
            for i, w in enumerate(widgets):
                if hasattr(w, "setMinimumHeight"):
                    w.setMinimumHeight(CTRL_H)
                stretch = 1 if i == 0 and len(widgets) == 1 else (1 if i == 0 else 0)
                row.addWidget(w, stretch)
            return row

        options_host = QWidget()
        options_host.setMinimumWidth(CONTENT_MIN_W)
        options_host.setSizePolicy(
            QSizePolicy.Policy.MinimumExpanding, QSizePolicy.Policy.Preferred
        )
        options_layout = QVBoxLayout(options_host)
        options_layout.setContentsMargins(10, 8, 14, 12)
        options_layout.setSpacing(12)

        out_box = QGroupBox("输出与执行")
        out_box_layout = QVBoxLayout(out_box)
        out_box_layout.setSpacing(10)
        self.output = QLineEdit(str(default_output_path("metadata_clean_outputs")))
        self.output.setMinimumHeight(CTRL_H)
        self.output.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        choose = QPushButton("选择…")
        choose.setFixedWidth(84)
        choose.setMinimumHeight(CTRL_H)
        choose.clicked.connect(self.choose_output)
        out_dir_row = _labeled_row("输出目录", self.output, choose)
        out_box_layout.addLayout(out_dir_row)
        self.keep_structure = QCheckBox("保留输入文件夹层级")
        self.keep_structure.setChecked(True)
        self.keep_structure.setMinimumHeight(28)
        self.preserve_time = QCheckBox("保留文件系统修改时间（隐私清理模式下禁用）")
        self.preserve_time.setChecked(False)
        self.preserve_time.setEnabled(False)
        # QCheckBox 无 setWordWrap；长说明放 tooltip 即可
        self.preserve_time.setToolTip("拍摄/修改时间可能用于推断活动轨迹，因此隐私清理固定使用新的输出时间。")
        self.preserve_time.setMinimumHeight(28)
        out_box_layout.addWidget(self.keep_structure)
        out_box_layout.addWidget(self.preserve_time)
        options_layout.addWidget(out_box)

        # —— 9:16 竖屏裁切（可选）——
        crop_box = QGroupBox("9:16 竖屏裁切（可选）")
        crop_layout = QVBoxLayout(crop_box)
        crop_layout.setSpacing(10)
        self.crop_916 = QCheckBox("所有视频/图片裁剪为 9:16")
        self.crop_916.setMinimumHeight(28)
        self.crop_916.setToolTip(
            "居中裁切为竖屏 9:16：\n"
            "· 不拉伸、不变形（只裁掉左右或上下多余部分）\n"
            "· 绝不把小分辨率强行放大到 1080p（会发糊）\n"
            "· 不强制改帧率；高画质重编码（medium/CRF15 或硬编，兼顾速度）\n"
            "· 已是 9:16 且选「仅裁切」时走流复制\n"
            "· 可与水印同时开启（先裁切再叠水印）"
        )
        self.crop_916_mode = QComboBox()
        self.crop_916_mode.addItem("仅居中裁切（推荐，不缩放）", "keep")
        self.crop_916_mode.addItem("裁切后缩小到 1080×1920（不放大）", "1080x1920")
        self.crop_916_mode.addItem("裁切后缩小到 720×1280（不放大）", "720x1280")
        self.crop_916_mode.addItem("裁切后缩小到 1440×2560（不放大）", "1440x2560")
        self.crop_916_mode.setMinimumHeight(CTRL_H)
        self.crop_916_mode.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.crop_916_mode.setToolTip(
            "「仅居中裁切」：只裁 9:16，不做缩放，画质最佳。\n"
            "「缩小到 xxx」：源比目标大时才缩小；源更小则保持源像素，"
            "绝不上采样放大（例如 270×480 不会被拉成 1080×1920 变糊）。"
        )
        crop_layout.addWidget(self.crop_916)
        crop_layout.addLayout(_labeled_row("输出尺寸", self.crop_916_mode))

        def _sync_crop_enabled(on: bool):
            self.crop_916_mode.setEnabled(bool(on))

        self.crop_916.toggled.connect(_sync_crop_enabled)
        _sync_crop_enabled(False)
        options_layout.addWidget(crop_box)

        # —— 旋转校正（倒着拍放正）——
        rot_box = QGroupBox("旋转校正（可选）")
        rot_layout = QVBoxLayout(rot_box)
        rot_layout.setSpacing(10)
        self.rotate_mode = QComboBox()
        self.rotate_mode.addItem("不旋转", "none")
        self.rotate_mode.addItem("旋转 180°（倒着拍放正，推荐）", "180")
        self.rotate_mode.addItem("顺时针 90°", "90cw")
        self.rotate_mode.addItem("逆时针 90°", "90ccw")
        self.rotate_mode.setMinimumHeight(CTRL_H)
        self.rotate_mode.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.rotate_mode.setToolTip(
            "用于手机倒着拍、横竖拿反的素材：\n"
            "· 180°：整段倒置放正（最常见）\n"
            "· 90°：横拍竖看或竖拍横看\n"
            "会重编码并真正改画面像素（不是只写旋转标记），"
            "可与 9:16 裁切/水印同时开启（先旋转再裁切）。"
        )
        rot_layout.addLayout(_labeled_row("旋转方式", self.rotate_mode))
        options_layout.addWidget(rot_box)

        # —— 水印合成：严格单列（标签在左固定宽，控件在右），永不挤成竖排 ——
        wm_box = QGroupBox("水印合成（可选）")
        wm_outer = QVBoxLayout(wm_box)
        wm_outer.setSpacing(10)
        self.wm_enable = QCheckBox("清理时同时烧录水印")
        self.wm_enable.setToolTip("开启后图片用 PIL 叠图；视频重编码并 overlay Logo，同时清除元数据。")
        self.wm_enable.setMinimumHeight(28)
        wm_outer.addWidget(self.wm_enable)

        self.wm_path = QLineEdit()
        self.wm_path.setPlaceholderText("选择 PNG / JPG Logo（推荐透明 PNG）")
        self.wm_path.setMinimumHeight(CTRL_H)
        self.wm_path.setMinimumWidth(160)
        self.wm_path.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.wm_path.textChanged.connect(
            lambda t: self.wm_path.setToolTip(
                t if t.strip() else "选择 PNG / JPG Logo（推荐透明 PNG）"
            )
        )
        wm_browse = QPushButton("选择…")
        wm_browse.setFixedWidth(84)
        wm_browse.setMinimumHeight(CTRL_H)
        wm_browse.clicked.connect(self.choose_watermark)
        wm_outer.addLayout(_labeled_row("Logo 文件", self.wm_path, wm_browse))

        self.wm_mode = QComboBox()
        self.wm_mode.addItems(["小 Logo 角标", "9:16 全屏覆盖"])
        self.wm_mode.setMinimumHeight(CTRL_H)
        self.wm_mode.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.wm_position = QComboBox()
        self.wm_position.addItems(["右下", "右上", "左下", "左上", "顶部居中", "底部居中", "居中"])
        self.wm_position.setMinimumHeight(CTRL_H)
        self.wm_position.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.wm_width = QSpinBox()
        self.wm_width.setRange(4, 80)
        self.wm_width.setValue(18)
        self.wm_width.setSuffix(" %")
        self.wm_width.setMinimumHeight(CTRL_H)
        self.wm_width.setMinimumWidth(100)
        self.wm_width.setToolTip("角标模式：相对画面宽度的 Logo 宽度")
        self.wm_opacity = QSpinBox()
        self.wm_opacity.setRange(5, 100)
        self.wm_opacity.setValue(90)
        self.wm_opacity.setSuffix(" %")
        self.wm_opacity.setMinimumHeight(CTRL_H)
        self.wm_opacity.setMinimumWidth(100)
        self.wm_margin = QSpinBox()
        self.wm_margin.setRange(0, 200)
        self.wm_margin.setValue(28)
        self.wm_margin.setSuffix(" px")
        self.wm_margin.setMinimumHeight(CTRL_H)
        self.wm_margin.setMinimumWidth(100)

        # 每一项独占一行 → 小屏也不会把「模式/位置」挤成竖排
        wm_outer.addLayout(_labeled_row("模式", self.wm_mode))
        wm_outer.addLayout(_labeled_row("位置", self.wm_position))
        wm_outer.addLayout(_labeled_row("宽度", self.wm_width))
        wm_outer.addLayout(_labeled_row("不透明度", self.wm_opacity))
        wm_outer.addLayout(_labeled_row("边距", self.wm_margin))

        wm_hint = QLabel("角标模式可调位置 / 宽度 / 边距；全屏覆盖只使用不透明度。")
        wm_hint.setWordWrap(True)
        wm_hint.setStyleSheet("color:#94a3b8;font-size:12px;padding:2px 0 0 0;")
        wm_outer.addWidget(wm_hint)

        def _sync_wm_enabled(on: bool):
            corner = bool(on) and "全屏" not in self.wm_mode.currentText()
            for w in (self.wm_path, wm_browse, self.wm_mode, self.wm_opacity):
                w.setEnabled(bool(on))
            for w in (self.wm_position, self.wm_width, self.wm_margin):
                w.setEnabled(corner)

        self.wm_enable.toggled.connect(_sync_wm_enabled)
        self.wm_mode.currentTextChanged.connect(
            lambda _t: _sync_wm_enabled(self.wm_enable.isChecked()))
        _sync_wm_enabled(False)
        options_layout.addWidget(wm_box)

        inspection = QGroupBox("元数据检查（选中左侧素材自动读取）")
        inspection_layout = QVBoxLayout(inspection)
        inspection_layout.setSpacing(8)
        self.inspect_status = QLabel(
            "请选择一个素材查看清理前信息；完成清理后会自动显示前后对比。"
        )
        self.inspect_status.setWordWrap(True)
        self.inspect_status.setStyleSheet("color:#7dd3fc;font-size:13px;")
        inspection_layout.addWidget(self.inspect_status)
        compare = QHBoxLayout()
        compare.setSpacing(10)
        before_box = QVBoxLayout()
        after_box = QVBoxLayout()
        before_box.addWidget(QLabel("清理前 · 原素材"))
        after_box.addWidget(QLabel("清理后 · 输出成品"))
        self.before_metadata = QPlainTextEdit()
        self.before_metadata.setReadOnly(True)
        self.before_metadata.setMinimumHeight(140)
        self.after_metadata = QPlainTextEdit()
        self.after_metadata.setReadOnly(True)
        self.after_metadata.setMinimumHeight(140)
        metadata_style = "font-family:Consolas,'Microsoft YaHei UI';font-size:12px;"
        self.before_metadata.setStyleSheet(metadata_style)
        self.after_metadata.setStyleSheet(metadata_style)
        before_box.addWidget(self.before_metadata)
        after_box.addWidget(self.after_metadata)
        compare.addLayout(before_box, 1)
        compare.addLayout(after_box, 1)
        inspection_layout.addLayout(compare)
        options_layout.addWidget(inspection)
        self.progress = QProgressBar()
        self.progress.setMinimumHeight(18)
        options_layout.addWidget(self.progress)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMinimumHeight(100)
        options_layout.addWidget(self.log, 1)
        actions = QHBoxLayout()
        actions.addStretch()
        self.stop = QPushButton("停止")
        self.stop.setEnabled(False)
        self.stop.setMinimumHeight(36)
        self.stop.setMinimumWidth(88)
        self.stop.clicked.connect(self.cancel)
        self.start = QPushButton("开始批量清除")
        self.start.setObjectName("primary")
        self.start.setMinimumHeight(36)
        self.start.setMinimumWidth(120)
        self.start.clicked.connect(self.run)
        actions.addWidget(self.stop)
        actions.addWidget(self.start)
        options_layout.addLayout(actions)
        options_layout.addStretch(0)

        options_scroll = QScrollArea()
        options_scroll.setWidgetResizable(True)
        options_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        options_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        options_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        options_scroll.setWidget(options_host)
        options_scroll.setMinimumWidth(360)
        options_scroll.setStyleSheet(
            "QScrollArea{background:transparent;border:none;}"
            "QScrollBar:vertical{width:12px;background:#0f172a;margin:0;}"
            "QScrollBar::handle:vertical{background:#334155;border-radius:5px;min-height:28px;}"
            "QScrollBar:horizontal{height:12px;background:#0f172a;margin:0;}"
            "QScrollBar::handle:horizontal{background:#334155;border-radius:5px;min-width:28px;}"
        )

        split.addWidget(source)
        split.addWidget(options_scroll)
        split.setStretchFactor(0, 2)
        split.setStretchFactor(1, 3)
        split.setSizes([320, 680])
        split.setChildrenCollapsible(False)
        root.addWidget(split, 1)

    def add_paths(self, paths):
        found = collect_files(paths, MEDIA_EXTENSIONS)
        existing = {self.list.item(i).text() for i in range(self.list.count())}
        for path in found:
            if path not in existing:
                self.list.addItem(path)
                existing.add(path)
        if self.list.count() and self.list.currentRow() < 0:
            self.list.setCurrentRow(0)

    @staticmethod
    def _privacy_risks(tag_items):
        rules = [
            ("位置/GPS（必须清理）", ("gps", "location", "latitude", "longitude", "altitude", "iso6709", "geotag")),
            ("拍摄与创建时间（必须清理）", ("datetime", "creation_time", "creationdate", "createdate", "modifydate", "timestamp", "date_time")),
            ("设备与序列号（必须清理）", ("make", "model", "serial", "cameraowner", "lensmake", "lensmodel", "hostcomputer", "device")),
            ("作者/版权/联系方式（必须清理）", ("artist", "author", "copyright", "creator", "owner", "byline", "credit", "contact", "email", "publisher", "rights")),
            ("唯一标识符（必须清理）", ("documentid", "instanceid", "uniqueid", "uuid", "identifier", "assetid", "contentid", "mediaid")),
            ("标题/描述/关键词（建议清理）", ("title", "comment", "description", "subject", "keyword", "category", "caption", "synopsis", "lyrics")),
            ("软件与处理来源（建议清理）", ("software", "encoder", "encoded_by", "application", "processingsoftware", "tool")),
            ("人物/人脸区域信息（必须清理）", ("personinimage", "mwg-rs", "regioninfo", "faceregion", "people")),
        ]
        found = {}
        for key, value in tag_items:
            haystack = (str(key) + " " + str(value)[:300]).casefold().replace(" ", "").replace("_", "").replace("-", "")
            for category, patterns in rules:
                if any(pattern.replace("_", "").replace("-", "") in haystack for pattern in patterns):
                    found.setdefault(category, []).append(str(key))
                    break
        return found

    def _append_risk_scan(self, lines, tag_items):
        risks = self._privacy_risks(tag_items)
        lines += ["", "【隐私风险扫描】"]
        if risks:
            for category, keys in risks.items():
                lines.append(f"⚠ {category}：{', '.join(dict.fromkeys(keys))}")
        else:
            lines.append("✓ 未检测到已知的高风险隐私字段")
        return sum(len(values) for values in risks.values())

    def _metadata_details(self, value):
        path = Path(value)
        if not path.is_file():
            return "文件不存在。", 0
        lines = [f"文件：{path.name}", f"大小：{path.stat().st_size:,} 字节", f"扩展名：{path.suffix.lower()}"]
        metadata_count = 0
        tag_items = []
        if path.suffix.lower() in IMAGE_EXTENSIONS:
            try:
                with Image.open(path) as image:
                    lines += [f"格式：{image.format}", f"尺寸：{image.width} × {image.height}",
                              f"色彩模式：{image.mode}", "", "【EXIF / 图片元数据】"]
                    exif = image.getexif()
                    for key, val in exif.items():
                        name = ExifTags.TAGS.get(key, str(key))
                        lines.append(f"{name}: {str(val)[:500]}")
                        tag_items.append((name, val))
                        metadata_count += 1
                    for key, val in image.info.items():
                        if key.lower() not in {"exif"}:
                            lines.append(f"{key}: {str(val)[:500]}")
                            tag_items.append((key, val))
                            metadata_count += 1
                    if metadata_count == 0:
                        lines.append("未检测到 EXIF/XMP 等附加信息")
            except Exception as exc:
                lines.append(f"读取图片信息失败：{exc}")
            self._append_risk_scan(lines, tag_items)
            return "\n".join(lines), metadata_count
        ffprobe = find_media_tool("ffprobe")
        if not ffprobe:
            return "\n".join(lines + ["", "未找到 FFprobe，无法读取音视频元数据。"]), 0
        command = [ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)]
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                encoding="utf-8", errors="replace", **hidden_kwargs())
        if result.returncode:
            return "\n".join(lines + ["", result.stderr.strip() or "FFprobe 读取失败"]), 0
        payload = json.loads(result.stdout or "{}")
        fmt = payload.get("format", {})
        lines += [f"容器：{fmt.get('format_long_name') or fmt.get('format_name', '')}",
                  f"时长：{fmt.get('duration', '')} 秒", f"码率：{fmt.get('bit_rate', '')}", "", "【容器元数据】"]
        tags = fmt.get("tags", {}) or {}
        for key, val in tags.items():
            lines.append(f"{key}: {val}")
            tag_items.append((key, val))
            metadata_count += 1
        if not tags:
            lines.append("未检测到容器附加信息")
        for index, stream in enumerate(payload.get("streams", []), 1):
            lines += ["", f"【轨道 {index} · {stream.get('codec_type', 'unknown')}】",
                      f"编码：{stream.get('codec_long_name') or stream.get('codec_name', '')}"]
            for key in ("profile", "width", "height", "r_frame_rate", "sample_rate", "channels",
                        "channel_layout", "bit_rate"):
                if stream.get(key) not in (None, ""):
                    lines.append(f"{key}: {stream[key]}")
            stream_tags = stream.get("tags", {}) or {}
            for key, val in stream_tags.items():
                lines.append(f"tag.{key}: {val}")
                tag_items.append((key, val))
                metadata_count += 1
        self._append_risk_scan(lines, tag_items)
        return "\n".join(lines), metadata_count

    def inspect_selected(self, path):
        if not path:
            self.before_metadata.clear()
            self.after_metadata.clear()
            return
        before, before_count = self._metadata_details(path)
        self.before_metadata.setPlainText(before)
        cleaned = self.cleaned_files.get(str(Path(path)))
        if cleaned and Path(cleaned).is_file():
            after, after_count = self._metadata_details(cleaned)
            self.after_metadata.setPlainText(after)
            removed = max(0, before_count - after_count)
            self.inspect_status.setText(
                f"检测完成：清理前 {before_count} 项附加信息，清理后 {after_count} 项，已减少 {removed} 项。"
                "编码、尺寸、时长等技术参数会保留。")
            self.inspect_status.setStyleSheet("color:#86efac;")
        else:
            self.after_metadata.setPlainText("尚未生成对应的清理成品。")
            self.inspect_status.setText(
                f"原素材检测到 {before_count} 项附加信息；执行清理后将在右侧自动显示结果。")
            self.inspect_status.setStyleSheet("color:#7dd3fc;")

    def _file_cleaned(self, source, destination):
        self.cleaned_files[str(Path(source))] = destination
        if self.list.currentItem() and self.list.currentItem().text() == source:
            self.inspect_selected(source)

    def choose_files(self):
        files, _ = QFileDialog.getOpenFileNames(
            self, "选择素材", "",
            "媒体文件 (*.mp4 *.mov *.mkv *.avi *.webm *.m4v *.mp3 *.wav *.m4a *.flac *.aac "
            "*.ogg *.opus *.jpg *.jpeg *.png *.bmp *.webp *.tif *.tiff)")
        self.add_paths(files)

    def choose_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "选择素材文件夹")
        if folder:
            self.add_paths([folder])

    def choose_output(self):
        folder = QFileDialog.getExistingDirectory(self, "选择输出目录", self.output.text())
        if folder:
            self.output.setText(folder)

    def choose_watermark(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择水印图片", "",
            "图片 (*.png *.jpg *.jpeg *.webp *.bmp *.tif *.tiff)")
        if path:
            self.wm_path.setText(path)
            self.wm_enable.setChecked(True)

    def _watermark_cfg(self):
        if not self.wm_enable.isChecked():
            return None
        path = self.wm_path.text().strip()
        if not path or not Path(path).is_file():
            return None
        return {
            "enabled": True,
            "path": path,
            "mode": self.wm_mode.currentText(),
            "position": self.wm_position.currentText(),
            "width_pct": int(self.wm_width.value()),
            "opacity": int(self.wm_opacity.value()),
            "margin": int(self.wm_margin.value()),
        }

    def _crop_916_mode_key(self) -> str:
        if not hasattr(self, "crop_916_mode"):
            return "keep"
        data = self.crop_916_mode.currentData()
        if data:
            return str(data)
        text = self.crop_916_mode.currentText()
        if "1080" in text:
            return "1080x1920"
        if "720" in text:
            return "720x1280"
        if "1440" in text:
            return "1440x2560"
        return "keep"

    def run(self):
        files = [self.list.item(i).text() for i in range(self.list.count())]
        if not files:
            QMessageBox.information(self, "没有素材", "请先添加视频、音频或图片。")
            return
        if self.wm_enable.isChecked() and not (self.wm_path.text().strip() and Path(self.wm_path.text().strip()).is_file()):
            QMessageBox.warning(self, "水印", "已勾选水印合成，请先选择有效的 Logo 图片。")
            return
        if getattr(self, "thread", None):
            try:
                if self.thread.isRunning():
                    QMessageBox.information(self, "任务进行中", "请等待当前清理结束。")
                    return
            except RuntimeError:
                self.thread = None
        self.log.clear()
        self.progress.setValue(0)
        self.thread = QThread(self)
        crop_on = bool(getattr(self, "crop_916", None) and self.crop_916.isChecked())
        rotate_key = "none"
        if hasattr(self, "rotate_mode"):
            rotate_key = str(self.rotate_mode.currentData() or "none")
        self.worker = MetadataWorker(
            files, self.output.text(), self.keep_structure.isChecked(),
            self.preserve_time.isChecked(),
            watermark=self._watermark_cfg(),
            crop_916=crop_on,
            crop_916_mode=self._crop_916_mode_key() if crop_on else "keep",
            rotate_mode=rotate_key,
        )
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.log.connect(self.log.appendPlainText)
        self.worker.progress.connect(self.progress.setValue)
        self.worker.file_done.connect(self._file_cleaned)
        self.worker.finished.connect(self.done)
        self.worker.finished.connect(self.thread.quit)
        self.thread.finished.connect(self._ended)
        self.thread.finished.connect(self.thread.deleteLater)
        self.start.setEnabled(False)
        self.stop.setEnabled(True)
        if rotate_key and rotate_key != "none":
            self.log.appendPlainText(
                f"已开启旋转校正：{self.rotate_mode.currentText()}（重编码改像素，放正后再进 Reels）。"
            )
        if crop_on:
            self.log.appendPlainText(
                f"已开启 9:16 居中裁切（模式：{self.crop_916_mode.currentText()}）；"
                "不拉伸、不强制改帧率，使用高质量编码。"
            )
        self.thread.start()

    def cancel(self):
        if self.worker:
            self.worker.cancel()

    def done(self, ok, message):
        self.start.setEnabled(True)
        self.stop.setEnabled(False)
        self.log.appendPlainText(message)
        (QMessageBox.information if ok else QMessageBox.critical)(
            self, "元数据清除" if ok else "处理失败", message)

    def _ended(self):
        self.worker = None
        self.thread = None
