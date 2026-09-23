"""Independent top/bottom split-screen composition; no timeline mutations."""
from __future__ import annotations

import json
import subprocess
import tempfile
import threading
import uuid
from pathlib import Path

from PySide6.QtCore import QThread, QUrl, Signal, Qt
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QFileDialog, QGridLayout, QGroupBox, QHBoxLayout,
    QLabel, QLineEdit, QListWidget, QMessageBox, QProgressBar, QPushButton,
    QSpinBox, QSlider, QVBoxLayout,
)
from .path_picker import VIDEO_EXTENSIONS, collect_files
from .settings_page import hidden_kwargs


def probe_video(ffmpeg, path):
    probe = Path(ffmpeg).with_name("ffprobe.exe" if Path(ffmpeg).suffix.lower() == ".exe" else "ffprobe")
    result = subprocess.run([str(probe), "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
                            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30, **hidden_kwargs())
    if result.returncode:
        raise ValueError(f"无法读取视频：{Path(path).name}\n{result.stderr[-600:]}")
    info = json.loads(result.stdout)
    videos = [s for s in info.get("streams", []) if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")]
    if not videos:
        raise ValueError(f"文件没有视频轨：{Path(path).name}")
    try:
        duration = float(videos[0].get("duration") or info.get("format", {}).get("duration") or 0)
    except ValueError:
        duration = float(info.get("format", {}).get("duration") or 0)
    if duration <= 0:
        raise ValueError(f"无法确定视频时长：{Path(path).name}")
    return dict(duration=duration, audio=any(s.get("codec_type") == "audio" for s in info.get("streams", [])))


def make_pairs(top, bottom):
    if not top or not bottom:
        raise ValueError("请分别添加上方和下方视频。")
    if len(top) != len(bottom) and len(top) != 1 and len(bottom) != 1:
        raise ValueError("上下数量必须相同，或其中一侧只有一个视频作为固定素材。")
    return [(top[0] if len(top) == 1 else top[i], bottom[0] if len(bottom) == 1 else bottom[i])
            for i in range(max(len(top), len(bottom)))]


def stack_command(ffmpeg, top, bottom, output, settings, info_top, info_bottom, preview=False):
    width, height = (360, 640) if preview else (int(settings.get("width", 1080)), int(settings.get("height", 1920)))
    top_h = max(2, min(height - 2, round(height * int(settings.get("ratio", 64)) / 100 / 2) * 2))
    policy = settings.get("duration", "top")
    duration = {"top": info_top["duration"], "shortest": min(info_top["duration"], info_bottom["duration"]),
                "longest": max(info_top["duration"], info_bottom["duration"])}[policy]
    if preview:
        duration = min(3., duration)
    filters = []
    for index, h in enumerate((top_h, height - top_h)):
        fit = settings.get("fit", "contain")
        if fit in ("cover", "top", "custom"):
            prefix = "top" if index == 0 else "bottom"
            x = max(0., min(1., float(settings.get(prefix + "_x", 50)) / 100)) if fit == "custom" else .5
            y = max(0., min(1., float(settings.get(prefix + "_y", 0)) / 100)) if fit == "custom" else (0. if fit == "top" else .5)
            geometry = (f"scale={width}:{h}:force_original_aspect_ratio=increase:force_divisible_by=2,"
                        f"crop={width}:{h}:(iw-ow)*{x:.6f}:(ih-oh)*{y:.6f}")
        else:
            geometry = f"scale={width}:{h}:force_original_aspect_ratio=decrease:force_divisible_by=2,pad={width}:{h}:(ow-iw)/2:(oh-ih)/2:color=black"
        filters.append(f"[{index}:v:0]setpts=PTS-STARTPTS,scale=iw*sar:ih,setsar=1,{geometry},setsar=1,fps=30,format=yuv420p[v{index}]")
    filters.append("[v0][v1]vstack=inputs=2[v]")
    mode = settings.get("audio", "top")
    selected = [i for i, info in enumerate((info_top, info_bottom))
                if info["audio"] and (mode == "mix" or mode == ("top" if i == 0 else "bottom"))]
    for index in selected:
        filters.append(f"[{index}:a:0]asetpts=PTS-STARTPTS,aresample=48000,aformat=channel_layouts=stereo,apad[a{index}]")
    if len(selected) == 2:
        filters.append("[a0][a1]amix=inputs=2:duration=longest:normalize=1[a]")
        audio = "[a]"
    else:
        audio = f"[a{selected[0]}]" if selected else None
    command = [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
               "-stream_loop", "-1", "-i", str(top), "-stream_loop", "-1", "-i", str(bottom),
               "-filter_complex", ";".join(filters), "-map", "[v]"]
    command += ["-map", audio, "-c:a", "aac", "-b:a", "192k"] if audio else ["-an"]
    command += ["-t", f"{duration:.6f}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(output)]
    return command


class StackWorker(QThread):
    progress = Signal(int, str)
    completed = Signal(list, str)

    def __init__(self, ffmpeg, pairs, output, settings, preview=False, parent=None):
        super().__init__(parent)
        self.ffmpeg, self.pairs, self.output = ffmpeg, pairs, Path(output)
        self.settings, self.preview = dict(settings), preview
        self.cancelled = threading.Event()
        self.process = None
        self.warnings = []

    def cancel(self):
        self.cancelled.set()
        process = self.process
        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass

    def run(self):
        products, error = [], ""
        try:
            self.output.mkdir(parents=True, exist_ok=True)
            for index, (top, bottom) in enumerate(self.pairs):
                if self.cancelled.is_set():
                    break
                self.progress.emit(round(index / len(self.pairs) * 100), f"正在处理 {index + 1}/{len(self.pairs)}：{Path(top).name}")
                a, b = probe_video(self.ffmpeg, top), probe_video(self.ffmpeg, bottom)
                audio_mode = self.settings.get("audio", "top")
                if (audio_mode == "top" and not a["audio"]) or (audio_mode == "bottom" and not b["audio"]) or (audio_mode == "mix" and not a["audio"] and not b["audio"]):
                    self.warnings.append(f"第 {index + 1} 组所选声音来源无音轨，输出为静音")
                if self.cancelled.is_set():
                    break
                # Only our private work directory is removed on failure/cancel.
                with tempfile.TemporaryDirectory(prefix=".stack_", dir=self.output) as work:
                    partial = Path(work) / "video.mp4"
                    command = stack_command(self.ffmpeg, top, bottom, partial, self.settings, a, b, self.preview)
                    self.process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, **hidden_kwargs())
                    if self.cancelled.is_set():
                        self.cancel()
                    _, stderr = self.process.communicate()
                    if self.cancelled.is_set():
                        break
                    if self.process.returncode:
                        raise RuntimeError(stderr.decode("utf-8", errors="replace")[-1800:])
                    probe_video(self.ffmpeg, partial)
                    target = self.output / f"{Path(top).stem[:90]}_上下拼接_{uuid.uuid4().hex[:10]}.mp4"
                    partial.replace(target)
                    products.append(str(target))
                self.progress.emit(round((index + 1) / len(self.pairs) * 100), f"完成 {index + 1}/{len(self.pairs)}")
        except Exception as exc:
            error = str(exc)
        finally:
            self.process = None
            if self.cancelled.is_set():
                error = "已停止；已完成的成品保留，未完成的临时文件已清理。"
            self.completed.emit(products, error)


class VerticalStackDialog(QDialog):
    caption_sources_ready = Signal(list)
    # top_paths, bottom_paths, settings, for_captions — 父页面先 Reels 合成再回传成品路径拼接
    synth_then_stack_requested = Signal(list, list, dict, bool)

    def __init__(self, ffmpeg, output, parent=None):
        super().__init__(parent)
        self.ffmpeg, self.worker = ffmpeg, None
        self._pending_caption_sources = []
        self.preview_dir = tempfile.TemporaryDirectory(prefix="video_stack_preview_", ignore_cleanup_errors=True)
        self.setWindowTitle("上下拼接视频")
        self.resize(840, 720)
        if self.screen():
            available = self.screen().availableGeometry()
            self.resize(min(840, available.width() - 60), min(720, available.height() - 80))
        root = QVBoxLayout(self)
        tip = QLabel(
            "上下画面同时播放｜参考比例：上 64% / 下 36%\n"
            "支持「视频文件」或「文件夹」（自动展开其中的视频）。\n"
            "批量：数量相同按列表顺序配对；任一侧只有一个则复用。\n"
            "若勾选「先 Reels 合成」：文件夹/源片会先出带字幕成品，再上下拼；"
            "上方成品字幕会落在上半画面。"
        )
        tip.setWordWrap(True)
        root.addWidget(tip)
        lists = QHBoxLayout()
        self.inputs = []
        for title in ("上方视频", "下方视频"):
            group = QGroupBox(title)
            layout = QVBoxLayout(group)
            view = QListWidget()
            view.setDragDropMode(QListWidget.DragDropMode.InternalMove)
            view.setToolTip("可拖动列表条目调整配对顺序。支持文件或文件夹路径。")
            layout.addWidget(view)
            actions = QHBoxLayout()
            add = QPushButton("添加视频")
            add_folder = QPushButton("添加文件夹")
            remove = QPushButton("移除选中")
            add.clicked.connect(lambda checked=False, target=view: self.add_files(target))
            add_folder.clicked.connect(lambda checked=False, target=view: self.add_folder(target))
            remove.clicked.connect(lambda checked=False, target=view: target.takeItem(target.currentRow()))
            actions.addWidget(add)
            actions.addWidget(add_folder)
            actions.addWidget(remove)
            layout.addLayout(actions)
            lists.addWidget(group)
            self.inputs.append(view)
        root.addLayout(lists, 1)
        self.synth_first = QCheckBox("源素材/文件夹：先走 Reels 合成（带字幕）再上下拼接")
        self.synth_first.setToolTip(
            "勾选后：列表里的源视频会先按 Reels 批量导出成带字幕成品，"
            "再按配对做上下拼接。上方成品字幕会出现在上半画面。"
            "若两侧已是成品，可取消勾选直接拼。"
        )
        root.addWidget(self.synth_first)
        options = QGridLayout()
        self.ratio = QSpinBox(); self.ratio.setRange(10, 90); self.ratio.setValue(64); self.ratio.setSuffix(" %")
        self.fit = QComboBox()
        for label, value in (("完整显示（不裁剪，推荐）", "contain"), ("顶部取景（裁满）", "top"),
                             ("自定义取景（可移动）", "custom"), ("居中裁满", "cover")):
            self.fit.addItem(label, value)
        self.audio = QComboBox()
        self.audio.setToolTip("仅保留所选来源的声音；该来源无音轨时输出静音，不会偷偷切换到另一段声音。")
        for label, value in (("仅上方声音", "top"), ("仅下方声音", "bottom"), ("上下混音", "mix"), ("静音", "mute")):
            self.audio.addItem(label, value)
        self.duration = QComboBox()
        for label, value in (("以上方时长为准", "top"), ("取较短时长", "shortest"), ("取较长时长", "longest")):
            self.duration.addItem(label, value)
        for i, (label, widget) in enumerate((("上方占比", self.ratio), ("画面适配", self.fit), ("声音", self.audio), ("成品时长", self.duration))):
            options.addWidget(QLabel(label), i // 2, (i % 2) * 2)
            options.addWidget(widget, i // 2, (i % 2) * 2 + 1)
        root.addLayout(options)
        self.crop_controls = QGroupBox("自定义取景：分别移动上下画面，调整后重新预览")
        crop_layout = QGridLayout(self.crop_controls)
        self.crop_positions = {}
        for row, (prefix, label) in enumerate((("top", "上方画面"), ("bottom", "下方画面"))):
            for column, (axis, caption, default) in enumerate((("x", "左 ↔ 右", 50), ("y", "顶部 ↔ 底部", 0))):
                key = prefix + "_" + axis
                slider = QSlider(Qt.Orientation.Horizontal); slider.setRange(0, 100); slider.setValue(default)
                spin = QSpinBox(); spin.setRange(0, 100); spin.setValue(default); spin.setSuffix(" %")
                slider.valueChanged.connect(spin.setValue); spin.valueChanged.connect(slider.setValue)
                tooltip = "取景位置：0% 保留左侧/顶部，100% 保留右侧/底部。无可裁空间的方向移动不会改变画面。"
                slider.setToolTip(tooltip); spin.setToolTip(tooltip)
                crop_layout.addWidget(QLabel(label + " " + caption), row, column * 3)
                crop_layout.addWidget(slider, row, column * 3 + 1)
                crop_layout.addWidget(spin, row, column * 3 + 2)
                self.crop_positions[key] = spin
        self.crop_controls.setVisible(False)
        self.fit.currentIndexChanged.connect(lambda: self.crop_controls.setVisible(self.fit.currentData() == "custom"))
        root.addWidget(self.crop_controls)
        destination = QHBoxLayout()
        self.output = QLineEdit(str(output))
        choose = QPushButton("输出目录")
        choose.clicked.connect(self.choose_output)
        destination.addWidget(self.output); destination.addWidget(choose); root.addLayout(destination)
        self.video = QVideoWidget(); self.video.setMinimumHeight(150)
        root.addWidget(self.video, 1)
        self.player = QMediaPlayer(self); self.audio_output = QAudioOutput(self)
        self.player.setAudioOutput(self.audio_output); self.player.setVideoOutput(self.video)
        self.status = QLabel("默认完整显示，不裁掉人头。先预览，再加入视频字幕提取/校对，或直接导出。")
        self.status.setWordWrap(True); root.addWidget(self.status)
        self.progress = QProgressBar(); root.addWidget(self.progress)
        buttons = QHBoxLayout()
        self.preview_btn, self.export_btn, self.stop_btn = QPushButton("预览首组 3 秒"), QPushButton("批量导出"), QPushButton("停止")
        self.caption_btn = QPushButton("生成并加入视频字幕")
        self.caption_btn.setToolTip("先生成拼接素材并加入 Reels 视频字幕队列，再提取、校对字幕及批量导出；不会自动识别或发布。")
        self.caption_btn.clicked.connect(lambda: self.start(False, for_captions=True))
        self.preview_btn.clicked.connect(lambda: self.start(True))
        self.export_btn.clicked.connect(lambda: self.start(False))
        self.stop_btn.clicked.connect(self.stop)
        self.stop_btn.setEnabled(False)
        for button in (self.preview_btn, self.caption_btn, self.export_btn, self.stop_btn): buttons.addWidget(button)
        root.addLayout(buttons)

    def _list_paths(self, view):
        """展开列表中的文件与文件夹为视频路径。"""
        raw = [view.item(i).text() for i in range(view.count())]
        return collect_files(raw, VIDEO_EXTENSIONS)

    def add_files(self, target):
        paths, _ = QFileDialog.getOpenFileNames(self, "添加视频", "", "视频 (*.mp4 *.mov *.mkv *.avi *.webm *.m4v)")
        for path in paths:
            target.addItem(path)
            target.item(target.count() - 1).setToolTip(path)

    def add_folder(self, target):
        folder = QFileDialog.getExistingDirectory(self, "添加视频文件夹")
        if not folder:
            return
        found = collect_files([folder], VIDEO_EXTENSIONS)
        if not found:
            QMessageBox.information(self, "没有视频", f"文件夹内未找到视频：\n{folder}")
            return
        # 列表里保留文件夹路径，导出时再展开（便于查看来源）
        target.addItem(folder)
        target.item(target.count() - 1).setToolTip(f"{folder}\n（含 {len(found)} 个视频）")
        self.status.setText(f"已添加文件夹（{len(found)} 个视频）：{Path(folder).name}")

    def choose_output(self):
        selected = QFileDialog.getExistingDirectory(self, "选择输出目录", self.output.text())
        if selected: self.output.setText(selected)

    def start_with_resolved_pairs(self, pairs, settings, preview=False, for_captions=False):
        """父页面合成完成后，用成品路径直接拼接。"""
        if self.worker and self.worker.isRunning():
            return
        if not pairs:
            QMessageBox.warning(self, "检查素材", "没有可拼接的配对。")
            return
        self.player.stop()
        self.preview_btn.setEnabled(False)
        self.export_btn.setEnabled(False)
        self.caption_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.progress.setValue(0)
        destination = self.preview_dir.name if preview else self.output.text().strip()
        if for_captions:
            destination = Path(destination) / "上下拼接素材"
        self.status.setText(f"开始拼接 {len(pairs)} 组成品…")
        self.worker = StackWorker(
            self.ffmpeg, pairs[:1] if preview else pairs, destination, settings, preview, self,
        )
        self.worker.progress.connect(self.update_progress)
        self.worker.completed.connect(lambda products, error: self.done_job(products, error, preview, for_captions))
        self.worker.finished.connect(self.job_finished)
        self.worker.start()

    def start(self, preview, for_captions=False):
        if self.worker and self.worker.isRunning(): return
        try:
            top = self._list_paths(self.inputs[0])
            bottom = self._list_paths(self.inputs[1])
            pairs = make_pairs(top, bottom)
            if not preview and not self.output.text().strip(): raise ValueError("请选择输出目录。")
        except ValueError as exc:
            QMessageBox.warning(self, "检查素材", str(exc)); return
        settings = dict(ratio=self.ratio.value(), fit=self.fit.currentData(), audio=self.audio.currentData(), duration=self.duration.currentData())
        settings.update({key: spin.value() for key, spin in self.crop_positions.items()})
        # 先合成再拼接：交给 Reels 父页面
        if (not preview) and self.synth_first.isChecked():
            self.status.setText("已请求 Reels 先合成上下素材（带字幕），完成后再拼接…")
            self.synth_then_stack_requested.emit(top, bottom, settings, bool(for_captions))
            return
        self.player.stop()
        self.preview_btn.setEnabled(False); self.export_btn.setEnabled(False); self.caption_btn.setEnabled(False); self.stop_btn.setEnabled(True)
        self.progress.setValue(0)
        destination = self.preview_dir.name if preview else self.output.text().strip()
        if for_captions:
            destination = Path(destination) / "上下拼接素材"
        self.worker = StackWorker(self.ffmpeg, pairs[:1] if preview else pairs, destination, settings, preview, self)
        self.worker.progress.connect(self.update_progress)
        self.worker.completed.connect(lambda products, error: self.done_job(products, error, preview, for_captions))
        self.worker.finished.connect(self.job_finished)
        self.worker.start()

    def update_progress(self, value, text):
        self.progress.setRange(0, 0 if text.startswith("正在处理") else 100)
        self.progress.setValue(value); self.status.setText(text)

    def done_job(self, products, error, preview, for_captions=False):
        warning = "；".join(self.worker.warnings[:3])
        self.status.setText(error or f"已生成 {len(products)} 个{'预览' if preview else '成品'}。{warning}")
        if products and preview and not error:
            self.player.setSource(QUrl.fromLocalFile(products[0])); self.player.play()
        if products and for_captions:
            self._pending_caption_sources = list(products)
        elif error and not self.worker.cancelled.is_set():
            QMessageBox.warning(self, "拼接未完成", error)

    def job_finished(self):
        self.progress.setRange(0, 100)
        self.preview_btn.setEnabled(True); self.export_btn.setEnabled(True); self.caption_btn.setEnabled(True); self.stop_btn.setEnabled(False)
        if self._pending_caption_sources:
            products, self._pending_caption_sources = self._pending_caption_sources, []
            self.caption_sources_ready.emit(products)
            self.reject()

    def stop(self):
        self.player.stop()
        if self.worker and self.worker.isRunning(): self.worker.cancel()

    def reject(self):
        if self.worker and self.worker.isRunning():
            self.stop(); self.status.setText("正在停止，请完成后关闭窗口。"); return
        self.player.stop(); self.player.setSource(QUrl())
        self.preview_dir.cleanup()
        super().reject()

    def closeEvent(self, event):
        if self.worker and self.worker.isRunning():
            self.stop(); self.status.setText("正在停止，请完成后关闭窗口。"); event.ignore(); return
        self.player.stop(); self.player.setSource(QUrl())
        self.preview_dir.cleanup()
        event.accept()
