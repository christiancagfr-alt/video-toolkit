from __future__ import annotations

import math
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Qt, Signal
from PySide6.QtWidgets import (
    QComboBox, QFileDialog, QFormLayout, QFrame, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
    QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QScrollArea, QSplitter,
    QVBoxLayout, QWidget,
)
from .precise_cut import (
    cut_range, fixed_ranges, frame_aligned_ranges, parse_lengths, probe_video, scene_ranges,
)
from .path_picker import DropListWidget, VIDEO_EXTENSIONS, collect_files, default_output_path, load_subfolders


def video_duration(ffmpeg: str, path: str) -> float:
    return probe_video(ffmpeg, path).duration


class SmartCutWorker(QObject):
    log = Signal(str)
    progress = Signal(int)
    finished = Signal(bool, str)

    def __init__(self, files, output, mode, sequence, loop_length, threshold, ffmpeg):
        super().__init__()
        self.files = files
        self.output = Path(output)
        self.mode = mode
        self.sequence = sequence
        self.loop_length = loop_length
        self.threshold = threshold
        self.ffmpeg = ffmpeg
        self.cancelled = False

    def cancel(self):
        self.cancelled = True

    def run(self):
        try:
            self.output.mkdir(parents=True, exist_ok=True)
            total = len(self.files)
            if not total:
                raise ValueError("请先选择视频文件")
            if self.mode != "智能画面识别":
                parse_lengths(self.sequence, self.loop_length)
            elif not math.isfinite(self.threshold) or not 0 < self.threshold <= 255:
                raise ValueError("场景阈值必须大于 0 且不超过 255")
            self.log.emit("════════ 智能剪辑开始 ════════")
            self.log.emit(f"文件数：{total}  |  模式：{self.mode}")
            self.log.emit(f"输出目录：{self.output}")
            if self.mode == "智能画面识别":
                self.log.emit(f"场景检测阈值：{self.threshold}")
            else:
                self.log.emit(f"时长序列：{self.sequence or '（无）'}  |  循环时长：{self.loop_length}s")
            with ThreadPoolExecutor(max_workers=min(4, total)) as pool:
                futures = {pool.submit(self.process_one, path): Path(path).name for path in self.files}
                done = 0
                ok_n, fail_n = 0, 0
                for future in as_completed(futures):
                    name = futures[future]
                    try:
                        clip_count, out_dir = future.result()
                        ok_n += 1
                        self.log.emit(f"✅ 完成 [{done + 1}/{total}] {name}  →  {clip_count} 段  |  {out_dir}")
                    except Exception as exc:
                        fail_n += 1
                        self.log.emit(f"❌ 失败 [{done + 1}/{total}] {name}：{exc}")
                        # 继续处理其余文件，最后汇总
                    done += 1
                    self.progress.emit(round(done / total * 100))
                    if self.cancelled:
                        raise RuntimeError("用户已取消任务")
            self.log.emit("════════ 智能剪辑结束 ════════")
            self.log.emit(f"成功 {ok_n} 个，失败 {fail_n} 个，输出：{self.output}")
            if fail_n and not ok_n:
                self.finished.emit(False, f"全部失败（{fail_n} 个）。请查看日志。")
            elif fail_n:
                self.finished.emit(True, f"处理完成：成功 {ok_n}，失败 {fail_n}。详见日志。")
            else:
                self.finished.emit(True, f"所有视频处理完成（{ok_n} 个），输出：{self.output}")
        except Exception as exc:
            self.finished.emit(False, str(exc))

    def process_one(self, path):
        if self.cancelled:
            raise RuntimeError("用户已取消任务")
        # Reserve a fresh directory atomically. Same-named inputs and reruns must
        # not overwrite each other or leave old numbered clips mixed with new ones.
        target_dir = self.output / Path(path).stem
        suffix = 1
        while True:
            try:
                target_dir.mkdir(parents=True, exist_ok=False)
                break
            except FileExistsError:
                suffix += 1
                target_dir = self.output / f"{Path(path).stem}_{suffix}"
        self.log.emit(f"▶ 开始：{Path(path).name}  |  输出子目录：{target_dir.name}")
        clock = probe_video(self.ffmpeg, path, lambda: self.cancelled)
        self.log.emit(f"  画面时长：{clock.duration:.3f}s；按时间戳精确重编码剪辑")
        if self.mode == "智能画面识别":
            count = self.smart_split(path, target_dir, clock=clock)
        else:
            count = self.fixed_split(path, target_dir, clock=clock)
        return count, str(target_dir)

    def _export_ranges(self, path, target_dir, ranges, clock):
        count = 0
        for count, (start, end) in enumerate(ranges, 1):
            if self.cancelled:
                raise RuntimeError("用户已取消任务")
            destination = target_dir / f"{count:03d}.mp4"
            self.log.emit(f"  [{count}] {start:.3f}s – {end:.3f}s"
                          f"（{end - start:.3f}s）→ {destination.name}")
            cut_range(self.ffmpeg, path, destination, start, end, clock, lambda: self.cancelled)
        self.log.emit(f"  {Path(path).name} 共导出 {count} 段 → {target_dir}")
        return count

    def smart_split(self, path, target_dir, clock=None):
        clock = clock or probe_video(self.ffmpeg, path, lambda: self.cancelled)
        self.log.emit(f"  分析画面切换点（阈值={self.threshold}）…")
        ranges = scene_ranges(path, clock, self.threshold, lambda: self.cancelled)
        self.log.emit(f"  检测到 {len(ranges)} 个场景，开始精确导出…")
        return self._export_ranges(path, target_dir, ranges, clock)

    def fixed_split(self, path, target_dir, total_duration=0.0, clock=None):
        # The video stream clock is authoritative; callers may pass a stale or
        # audio/container duration through the legacy total_duration argument.
        clock = clock or probe_video(self.ffmpeg, path, lambda: self.cancelled)
        ranges = fixed_ranges(clock.duration, self.sequence, self.loop_length)
        ranges = frame_aligned_ranges(self.ffmpeg, path, ranges, clock, lambda: self.cancelled)
        self.log.emit("  切点对齐到实际画面帧；不足一帧的余量并入相邻片段")
        return self._export_ranges(path, target_dir, ranges, clock)


class SmartCutPage(QWidget):
    def __init__(self):
        super().__init__()
        self.thread = None
        self.worker = None
        self.build_ui()

    def build_ui(self):
        # 与自动流水线一致：左参数配置，右输出日志
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 14, 18, 14)
        root.setSpacing(8)
        title = QLabel("✂ 智能剪辑")
        title.setObjectName("heading")
        root.addWidget(title)
        sub = QLabel("支持自定义时长序列与智能场景识别，按实际时间精确剪辑。为保证切点准确，导出会重新编码。")
        sub.setStyleSheet("color:#94a3b8;")
        sub.setWordWrap(True)
        root.addWidget(sub)

        split = QSplitter(Qt.Orientation.Horizontal)
        split.setChildrenCollapsible(False)

        left = QFrame()
        left.setObjectName("panel")
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(12, 10, 12, 10)
        left_layout.setSpacing(8)

        left_layout.addWidget(QLabel("1. 视频队列（可拖入视频或整个文件夹）"))
        self.files = DropListWidget()
        self.files.setMinimumHeight(140)
        self.files.paths_dropped.connect(self.add_paths)
        left_layout.addWidget(self.files, 1)
        row = QHBoxLayout()
        add = QPushButton("选择视频")
        add.clicked.connect(self.add_files)
        add_folder = QPushButton("选择文件夹")
        add_folder.clicked.connect(self.add_folder)
        clear = QPushButton("清空")
        clear.clicked.connect(self.files.clear)
        row.addWidget(add)
        row.addWidget(add_folder)
        row.addWidget(clear)
        row.addStretch()
        left_layout.addLayout(row)

        settings = QGroupBox("2. 剪辑参数")
        form = QFormLayout(settings)
        form.setContentsMargins(10, 12, 10, 10)
        form.setSpacing(8)
        out_row = QHBoxLayout()
        self.output = QLineEdit(str(default_output_path("智能剪辑输出")))
        choose = QPushButton("选择…")
        choose.clicked.connect(self.choose_output)
        out_row.addWidget(self.output, 1)
        out_row.addWidget(choose)
        out_widget = QWidget()
        out_widget.setLayout(out_row)
        form.addRow("输出目录", out_widget)
        self.mode = QComboBox()
        self.mode.addItems(["自定义时长序列", "智能画面识别"])
        self.mode.currentTextChanged.connect(self.mode_changed)
        form.addRow("剪辑模式", self.mode)
        self.sequence = QLineEdit("5,10,15")
        form.addRow("时长序列（秒）", self.sequence)
        self.loop = QLineEdit("30")
        form.addRow("序列用完后循环时长", self.loop)
        self.threshold = QLineEdit("27")
        self.threshold.setEnabled(False)
        form.addRow("场景检测阈值", self.threshold)
        left_layout.addWidget(settings)

        self.progress = QProgressBar()
        left_layout.addWidget(self.progress)
        actions = QHBoxLayout()
        actions.addStretch()
        self.stop = QPushButton("停止")
        self.stop.setEnabled(False)
        self.stop.clicked.connect(self.cancel)
        self.start = QPushButton("开始处理")
        self.start.setObjectName("primary")
        self.start.clicked.connect(self.start_work)
        actions.addWidget(self.stop)
        actions.addWidget(self.start)
        left_layout.addLayout(actions)

        left_scroll = QScrollArea()
        left_scroll.setWidgetResizable(True)
        left_scroll.setFrameShape(QFrame.Shape.NoFrame)
        left_scroll.setWidget(left)

        right = QFrame()
        right.setObjectName("panel")
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(12, 10, 12, 10)
        right_layout.setSpacing(8)
        right_layout.addWidget(QLabel("运行日志"))
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setPlaceholderText("任务进度与错误信息会显示在这里…")
        self.log.setStyleSheet("font-family:Consolas,'Microsoft YaHei UI';font-size:12px;")
        right_layout.addWidget(self.log, 1)

        split.addWidget(left_scroll)
        split.addWidget(right)
        split.setSizes([560, 720])
        split.setStretchFactor(0, 2)
        split.setStretchFactor(1, 3)
        root.addWidget(split, 1)

    def mode_changed(self, mode):
        smart = mode == "智能画面识别"
        self.threshold.setEnabled(smart); self.sequence.setEnabled(not smart); self.loop.setEnabled(not smart)

    def add_files(self):
        files, _ = QFileDialog.getOpenFileNames(self, "选择视频", "", "视频 (*.mp4 *.mov *.mkv *.avi *.wmv *.webm *.m4v);;所有文件 (*.*)")
        self.add_paths(files)

    def add_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "选择视频文件夹")
        if folder: self.add_paths([folder])

    def add_paths(self, paths):
        files = collect_files(paths, VIDEO_EXTENSIONS)
        current = {self.files.item(i).text() for i in range(self.files.count())}
        for path in files:
            if path not in current: self.files.addItem(path); current.add(path)



    def choose_output(self):
        path = QFileDialog.getExistingDirectory(self, "选择输出目录", self.output.text())
        if path: self.output.setText(path)

    def start_work(self):
        files = [self.files.item(i).text() for i in range(self.files.count())]
        if not files:
            QMessageBox.information(self, "请选择文件", "请先选择视频文件。")
            return
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            QMessageBox.critical(self, "缺少 FFmpeg", "当前系统未找到 ffmpeg。")
            return
        try:
            if self.mode.currentText() == "智能画面识别":
                loop, threshold = 30.0, float(self.threshold.text())
                if not math.isfinite(threshold) or not 0 < threshold <= 255:
                    raise ValueError("场景阈值必须大于 0 且不超过 255")
            else:
                _lengths, loop = parse_lengths(self.sequence.text(), self.loop.text())
                threshold = 27.0
        except ValueError as exc:
            QMessageBox.warning(self, "参数错误", str(exc))
            return
        if getattr(self, "thread", None):
            try:
                if self.thread.isRunning():
                    QMessageBox.information(self, "任务进行中", "请等待当前剪辑结束。")
                    return
            except RuntimeError:
                self.thread = None
        self.thread = QThread(self)
        self.worker = SmartCutWorker(files, self.output.text(), self.mode.currentText(), self.sequence.text(), loop, threshold, ffmpeg)
        self.worker.moveToThread(self.thread); self.thread.started.connect(self.worker.run)
        self.worker.log.connect(self.log.appendPlainText); self.worker.progress.connect(self.progress.setValue)
        self.worker.finished.connect(self.done); self.worker.finished.connect(self.thread.quit)
        self.thread.finished.connect(self._ended)
        self.thread.finished.connect(self.thread.deleteLater)
        self.start.setEnabled(False); self.stop.setEnabled(True); self.thread.start()

    def cancel(self):
        if self.worker: self.worker.cancel()

    def done(self, ok, message):
        self.start.setEnabled(True); self.stop.setEnabled(False); self.log.appendPlainText(message)
        (QMessageBox.information if ok else QMessageBox.critical)(self, "处理完成" if ok else "处理失败", message)

    def _ended(self):
        self.worker = None
        self.thread = None
