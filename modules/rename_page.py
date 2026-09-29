from __future__ import annotations

import datetime
import json
import os
import re
import shutil
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFileDialog, QFormLayout, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
    QGroupBox, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QScrollArea,
    QSpinBox, QSplitter, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget, QHeaderView,
    QAbstractItemView,
)
from .path_picker import DropFolderLineEdit
from .platform_utils import app_data_dir


MAX_FILENAME_CHARS = 230
INVALID_FILENAME_CHARS = '<>:"/\\|?*'
INVALID_FILENAME_TRANS = str.maketrans("", "", INVALID_FILENAME_CHARS)
WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"}


def clean_filename_part(value, fallback="未命名", max_chars=None):
    original = str(value)
    cleaned = original.translate(INVALID_FILENAME_TRANS)
    cleaned = re.sub(r"[\x00-\x1f]", "", cleaned)
    cleaned = " ".join(cleaned.split()).strip(" .")
    if max_chars is not None:
        cleaned = cleaned[:max_chars].rstrip(" .")
    if not cleaned:
        cleaned = fallback
    if cleaned.upper() in WINDOWS_RESERVED_NAMES:
        cleaned += "_"
    return cleaned


def filename_part_changed(value, max_chars=None):
    raw = str(value).strip()
    return clean_filename_part(raw, fallback="", max_chars=max_chars) != raw


def safe_filename(filename, parent: Path):
    source = Path(filename)
    extension = source.suffix
    stem = clean_filename_part(source.stem)
    # Modern Python/Qt use Unicode long-path capable file APIs.  Limiting the
    # title again by the full parent path made perfectly valid titles become
    # much shorter merely because the selected output folder was deeply nested.
    # Keep only the per-file safety limit here.
    maximum = MAX_FILENAME_CHARS - len(extension)
    if maximum < 12:
        raise ValueError(f"输出路径过长，请选择更短的输出目录：{parent}")
    truncated = len(stem) > maximum
    stem = stem[:maximum].rstrip(" .") or "未命名"
    if stem.upper() in WINDOWS_RESERVED_NAMES:
        stem += "_"
    return stem + extension, truncated


class RenameTask:
    def __init__(self, input_dir, output_parent, task_name, prefix, titles, date_str,
                 suffix, start_index, padding, copy_files, direct_replace=False):
        self.input_dir = Path(input_dir)
        self.output_parent = Path(output_parent)
        raw_task_name = task_name.strip() or self.input_dir.name
        self.task_name = clean_filename_part(raw_task_name, max_chars=80)
        self.task_name_changed = filename_part_changed(raw_task_name, 80)
        self.prefix = clean_filename_part(prefix.strip(), fallback="", max_chars=60) if prefix.strip() else ""
        self.prefix_changed = filename_part_changed(prefix, 60) if prefix.strip() else False
        self.direct_replace = bool(direct_replace)
        raw_titles = [x.strip() for x in titles.splitlines() if x.strip()]
        title_limit = None
        self.titles = [clean_filename_part(x, fallback="", max_chars=title_limit) for x in raw_titles]
        self.title_changed = [filename_part_changed(x, title_limit) for x in raw_titles]
        self.date_str = clean_filename_part(date_str.strip(), fallback="", max_chars=30) if date_str.strip() else ""
        self.date_changed = filename_part_changed(date_str, 30) if date_str.strip() else False
        self.suffix = clean_filename_part(suffix.strip(), fallback="", max_chars=60) if suffix.strip() else ""
        self.suffix_changed = filename_part_changed(suffix, 60) if suffix.strip() else False
        self.start_index = int(start_index)
        self.padding = int(padding)
        self.copy_files = copy_files

    def output_folder(self):
        return self.output_parent / "_".join(clean_filename_part(self.task_name).split())

    def render_name(self, original, index):
        return self.render_name_info(original, index)[0]

    def render_name_info(self, original, index):
        source = Path(original)
        title_index = index - self.start_index
        changed = self.task_name_changed or self.prefix_changed or self.date_changed or self.suffix_changed
        if self.direct_replace:
            raw_title = self.titles[title_index] if 0 <= title_index < len(self.titles) else source.stem
            # 标题列表约定为“不含扩展名的文件名”；若用户恰好粘贴了相同扩展名，避免重复追加。
            base = raw_title[:-len(source.suffix)] if source.suffix and raw_title.casefold().endswith(source.suffix.casefold()) else raw_title
            result, truncated = safe_filename(base + source.suffix, self.output_folder())
            title_changed = self.title_changed[title_index] if 0 <= title_index < len(self.title_changed) else False
            return result, title_changed or truncated
        if self.titles:
            title = self.titles[title_index] if 0 <= title_index < len(self.titles) else ""
            if 0 <= title_index < len(self.title_changed):
                changed = changed or self.title_changed[title_index]
        else:
            title = clean_filename_part(source.stem, fallback="")
            changed = changed or filename_part_changed(source.stem)
        parts = [str(index).zfill(self.padding)]
        for part in (self.prefix, title, self.date_str):
            if part: parts.append(part)
        base = "-".join(parts)
        if self.suffix: base += self.suffix if self.suffix.startswith("-") else "-" + self.suffix
        result, truncated = safe_filename(base + source.suffix, self.output_folder())
        return result, changed or truncated


class RenameWorker(QObject):
    log = Signal(str); progress = Signal(int); finished = Signal(bool, str)

    def __init__(self, tasks):
        super().__init__(); self.tasks = tasks

    def run(self):
        try:
            for task_no, task in enumerate(self.tasks):
                if not task.input_dir.is_dir():
                    raise FileNotFoundError(f"源文件夹已不存在：{task.input_dir}")
                if not task.output_parent.is_dir():
                    raise FileNotFoundError(f"输出父目录已不存在：{task.output_parent}")
                output = task.output_folder(); output.mkdir(parents=True, exist_ok=True)
                files = sorted((x for x in task.input_dir.iterdir() if x.is_file()), key=lambda x: natural_key(x.name))
                for offset, source in enumerate(files):
                    if not source.exists():
                        raise FileNotFoundError(f"源文件已不存在：{source}")
                    new_name, adjusted = task.render_name_info(source.name, task.start_index + offset)
                    destination = unique_destination(output / new_name)
                    (shutil.copy2 if task.copy_files else shutil.move)(str(source), str(destination))
                    note = "（已清洗/截断）" if adjusted else ""
                    self.log.emit(f"{source.name}  →  {destination.name}{note}")
                self.progress.emit(round((task_no + 1) / len(self.tasks) * 100))
            self.finished.emit(True, "全部重命名任务已完成")
        except Exception as exc:
            self.finished.emit(False, str(exc))


class SmartTitleWorker(QObject):
    log = Signal(str); progress = Signal(int); finished = Signal(bool, str, list)

    def __init__(self, files, transcribe_callable):
        super().__init__(); self.files = list(files); self.transcribe_callable = transcribe_callable

    def run(self):
        titles = []; errors = []
        try:
            for index, source in enumerate(self.files):
                self.log.emit(f"[{index + 1}/{len(self.files)}] 正在识别视频内容：{source.name}")
                try:
                    original, chinese, _srt = self.transcribe_callable(str(source))
                    chinese = re.sub(r"\s+", " ", str(chinese or "")).strip()
                    chinese_chars=sum("\u4e00"<=char<="\u9fff" for char in chinese)
                    title = chinese if chinese_chars and not chinese.startswith("【自动翻译失败") else ""
                    if not title:
                        errors.append(f"{source.name}: 未生成有效中文字幕")
                        self.log.emit(f"当前视频只识别到外文，未把外文写入标题；暂用原文件名：{source.name}")
                except Exception as exc:
                    title = ""; errors.append(f"{source.name}: {exc}")
                    self.log.emit(f"识别失败，暂用原文件名，可稍后重试或手动修改：{source.name}（{exc}）")
                if not title:
                    title = source.stem
                    self.log.emit(f"未识别到有效文案，保留原文件名：{source.name}")
                titles.append(title)
                self.progress.emit(round((index + 1) / max(1, len(self.files)) * 100))
            if errors:
                self.finished.emit(False, f"已生成 {len(titles)} 行标题，其中 {len(errors)} 个视频识别失败并暂用原文件名。", titles)
            else:
                self.finished.emit(True, f"已读取 {len(titles)} 个视频内容并写入标题列表。", titles)
        except Exception as exc:
            self.finished.emit(False, str(exc), titles)


def natural_key(text):
    return [int(x) if x.isdigit() else x.lower() for x in re.split(r"(\d+)", text)]


def unique_destination(path: Path):
    if not path.exists(): return path
    number = 1
    while True:
        tail = f"_{number}"
        available = MAX_FILENAME_CHARS - len(path.suffix) - len(tail)
        if available < 8:
            raise ValueError(f"输出路径过长，请选择更短的输出目录：{path.parent}")
        candidate = path.with_name(f"{path.stem[:available].rstrip(' .')}{tail}{path.suffix}")
        if not candidate.exists(): return candidate
        number += 1


class RenamePage(QWidget):
    def __init__(self, transcribe_callable=None):
        super().__init__(); self.tasks = []; self.thread = None; self.worker = None
        self.transcribe_callable = transcribe_callable; self.title_thread = None; self.title_worker = None
        self.preset_path = app_data_dir() / "rename_presets.json"
        self.presets = self.load_presets(); self.build_ui(); self.refresh_presets()

    def build_ui(self):
        layout = QVBoxLayout(self); layout.setContentsMargins(18, 12, 18, 12); layout.setSpacing(8)
        header = QHBoxLayout()
        title_box = QVBoxLayout(); title_box.setSpacing(1)
        title = QLabel("视频 / 文件批量重命名"); title.setStyleSheet("font-size:24px;font-weight:800;")
        title_box.addWidget(title); title_box.addWidget(QLabel("选择文件夹、配置命名规则、一键执行重命名"))
        header.addLayout(title_box); header.addStretch()
        top_preview = QPushButton("刷新预览"); top_preview.clicked.connect(self.update_preview)
        self.run_btn = QPushButton("开始重命名"); self.run_btn.setObjectName("primary"); self.run_btn.clicked.connect(self.run_tasks)
        header.addWidget(top_preview); header.addWidget(self.run_btn)
        layout.addLayout(header)

        content = QSplitter(Qt.Orientation.Horizontal); content.setChildrenCollapsible(False)
        left_scroll = QScrollArea(); left_scroll.setWidgetResizable(True)
        left = QWidget(); left_layout = QVBoxLayout(left); left_layout.setContentsMargins(10, 8, 10, 8); left_layout.setSpacing(7)
        form_group = QGroupBox("1. 文件夹与命名规则")
        form = QFormLayout(form_group); form.setContentsMargins(10, 10, 10, 10); form.setSpacing(6)
        self.input = DropFolderLineEdit(); self.input.setPlaceholderText("可把文件夹拖到这里")
        self.input.folder_dropped.connect(self.set_input_folder)
        form.addRow("源文件夹", self.path_row(self.input, self.choose_input))
        self.output = DropFolderLineEdit(); self.output.setPlaceholderText("可把输出文件夹拖到这里（留空默认在源文件夹同级生成成品目录）")
        self.output.folder_dropped.connect(self.set_output_folder)
        form.addRow("输出文件夹", self.path_row(self.output, self.choose_output))
        preset_row = QHBoxLayout()
        self.preset_combo = QComboBox(); self.preset_combo.currentTextChanged.connect(self.apply_preset)
        save_preset = QPushButton("保存当前方案"); save_preset.clicked.connect(self.save_preset)
        delete_preset = QPushButton("删除方案"); delete_preset.clicked.connect(self.delete_preset)
        preset_row.addWidget(self.preset_combo, 1); preset_row.addWidget(save_preset); preset_row.addWidget(delete_preset)
        preset_widget = QWidget(); preset_widget.setLayout(preset_row); form.addRow("前后缀方案", preset_widget)
        self.prefix = QLineEdit(); form.addRow("前缀", self.prefix)
        line = QHBoxLayout()
        self.date_enabled = QCheckBox("日期"); self.date_enabled.setChecked(True)
        self.date = QLineEdit(datetime.date.today().strftime("%Y%m%d")); self.suffix = QLineEdit("FF-PT")
        self.suffix_enabled = QCheckBox("后缀"); self.suffix_enabled.setChecked(True)
        self.start_index = QSpinBox(); self.start_index.setRange(0, 999999); self.start_index.setValue(1)
        self.padding = QSpinBox(); self.padding.setRange(1, 12); self.padding.setValue(3)
        line.addWidget(self.date_enabled); line.addWidget(self.date); line.addWidget(self.suffix_enabled); line.addWidget(self.suffix)
        line.addWidget(QLabel("起始编号")); line.addWidget(self.start_index); line.addWidget(QLabel("位数")); line.addWidget(self.padding)
        line_widget = QWidget(); line_widget.setLayout(line); form.addRow("命名规则", line_widget)
        self.direct_replace = QCheckBox("标题原样替换文件名（不添加编号、前缀、日期和后缀）")
        self.direct_replace.setToolTip("标题列表每一行直接作为对应文件名；只保留原文件扩展名。Windows 不允许的字符仍会自动清理。")
        form.addRow("替换方式", self.direct_replace)
        self.date_enabled.toggled.connect(self.date.setEnabled)
        self.suffix_enabled.toggled.connect(self.suffix.setEnabled)
        self.direct_replace.toggled.connect(self._direct_replace_changed)
        self.copy = QCheckBox("复制到输出目录，保留原文件"); self.copy.setChecked(True); form.addRow("处理方式", self.copy)
        left_layout.addWidget(form_group)

        title_group = QGroupBox("2. 标题列表（每行一个；空行会被忽略，请用编号表核对对应关系）")
        title_layout = QVBoxLayout(title_group); title_layout.setContentsMargins(10, 10, 10, 10)
        self.titles = QPlainTextEdit(); self.titles.setMinimumHeight(150)
        self.titles.setPlaceholderText(
            "第1行 → 自然排序后的第1个文件\n"
            "第2行 → 第2个文件\n"
            "（空行不计入；首尾空格会清理，请看右侧编号表）"
        )
        self.titles.textChanged.connect(self.update_preview)
        title_layout.addWidget(self.titles)
        self.title_match_hint = QLabel("有效标题 0 行 · 源文件 0 个")
        self.title_match_hint.setWordWrap(True)
        self.title_match_hint.setStyleSheet(
            "color:#7dd3fc;background:#0b1830;padding:6px 8px;border-radius:5px;"
        )
        title_layout.addWidget(self.title_match_hint)
        buttons = QHBoxLayout()
        preview = QPushButton("刷新预览"); preview.clicked.connect(self.update_preview)
        load = QPushButton("读取文件名为标题"); load.clicked.connect(self.load_titles)
        self.smart_titles_btn = QPushButton("智能读取视频内容为标题")
        self.smart_titles_btn.setObjectName("primary")
        self.smart_titles_btn.setToolTip("按自然排序批量识别视频内容，每个视频生成一行标题；识别后仍可手动修改")
        self.smart_titles_btn.clicked.connect(self.load_smart_titles)
        self.smart_titles_btn.setEnabled(callable(self.transcribe_callable))
        buttons.addWidget(load); buttons.addWidget(self.smart_titles_btn); buttons.addWidget(preview); buttons.addStretch()
        title_layout.addLayout(buttons); left_layout.addWidget(title_group, 1)
        left_scroll.setWidget(left); content.addWidget(left_scroll)

        right = QWidget(); right_layout = QVBoxLayout(right); right_layout.setContentsMargins(10, 8, 10, 8); right_layout.setSpacing(8)
        preview_group = QGroupBox("编号核对表（序号 · 源视频 · 对应标题 · 新文件名）")
        preview_layout = QVBoxLayout(preview_group); preview_layout.setContentsMargins(10, 10, 10, 10)
        self.preview_title = QLabel("待处理文件总数：0 个")
        self.preview_title.setStyleSheet("font-weight: bold; color: #3b82f6;")
        preview_layout.addWidget(self.preview_title)
        
        self.preview = QTableWidget(0, 4)
        self.preview.setHorizontalHeaderLabels(["序号", "源文件名", "对应标题（第N行）", "新文件名"])
        self.preview.verticalHeader().setVisible(False)
        self.preview.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self.preview.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.preview.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.preview.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.preview.setColumnWidth(0, 46)
        self.preview.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.preview.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.preview.setAlternatingRowColors(True)
        self.preview.setMinimumHeight(350)
        self.preview.setStyleSheet(
            "QTableWidget{background:#0b1424;alternate-background-color:#13223d;gridline-color:#1e293b;}"
            "QTableWidget::item{background:transparent;color:#e5edf8;padding:4px;}"
            "QTableWidget::item:selected{background:#2563eb;color:#ffffff;}"
        )
        preview_layout.addWidget(self.preview); right_layout.addWidget(preview_group, 1)
        content.addWidget(right); content.setSizes([610, 720]); content.setStretchFactor(0, 5); content.setStretchFactor(1, 6)
        layout.addWidget(content, 1)

        log_row = QHBoxLayout(); log_row.addWidget(QLabel("执行日志")); log_row.addStretch()
        layout.addLayout(log_row)
        self.progress = QProgressBar(); self.log = QPlainTextEdit(); self.log.setReadOnly(True); self.log.setMaximumHeight(90)
        layout.addWidget(self.progress); layout.addWidget(self.log)

    def load_presets(self):
        try:
            return json.loads(self.preset_path.read_text(encoding="utf-8"))
        except Exception:
            return {
                "常用-FF-PT": {"prefix": "", "suffix": "FF-PT"},
                "仅编号": {"prefix": "", "suffix": ""},
            }

    def persist_presets(self):
        self.preset_path.parent.mkdir(parents=True, exist_ok=True)
        self.preset_path.write_text(json.dumps(self.presets, ensure_ascii=False, indent=2), encoding="utf-8")

    def refresh_presets(self, selected=""):
        self.preset_combo.blockSignals(True)
        self.preset_combo.clear(); self.preset_combo.addItem("选择已保存方案…")
        self.preset_combo.addItems(self.presets.keys())
        if selected:
            self.preset_combo.setCurrentText(selected)
        self.preset_combo.blockSignals(False)

    def save_preset(self):
        name, ok = QInputDialog.getText(self, "保存前后缀方案", "方案名称：")
        if not ok: return
        name = name.strip()
        if not name:
            QMessageBox.information(self, "无法保存", "请输入方案名称。")
            return
        try:
            self.presets[name] = {"prefix": self.prefix.text(), "suffix": self.suffix.text(),
                                  "suffix_enabled": self.suffix_enabled.isChecked()}
            self.persist_presets(); self.refresh_presets(name)
            QMessageBox.information(self, "保存成功", f"前后缀方案“{name}”已保存。")
        except Exception as exc:
            QMessageBox.critical(self, "保存方案失败", f"无法写入配置：\n{exc}")

    def apply_preset(self, name):
        data = self.presets.get(name)
        if data:
            self.prefix.setText(data.get("prefix", "")); self.suffix.setText(data.get("suffix", ""))
            self.suffix_enabled.setChecked(bool(data.get("suffix_enabled", True))); self.update_preview()

    def delete_preset(self):
        name = self.preset_combo.currentText()
        if name in self.presets:
            try:
                del self.presets[name]; self.persist_presets(); self.refresh_presets()
                QMessageBox.information(self, "删除成功", f"前后缀方案“{name}”已删除。")
            except Exception as exc:
                QMessageBox.critical(self, "删除方案失败", f"无法写入配置：\n{exc}")

    def path_row(self, edit, callback):
        row = QHBoxLayout(); button = QPushButton("浏览…"); button.clicked.connect(callback); row.addWidget(edit); row.addWidget(button)
        widget = QWidget(); widget.setLayout(row); return widget

    def choose_input(self):
        path = QFileDialog.getExistingDirectory(self, "选择源文件夹")
        if path: self.set_input_folder(path)

    def set_input_folder(self, path):
        self.input.setText(path)
        if hasattr(self, "output") and not self.output.text():
            self.output.setText(str(Path(path).parent / (Path(path).name + "_成品")))
        self.update_preview()

    def set_output_folder(self, path):
        self.output.setText(path)
        self.update_preview()

    def choose_output(self):
        path = QFileDialog.getExistingDirectory(self, "选择输出文件夹")
        if path: self.set_output_folder(path)

    def _direct_replace_changed(self, enabled):
        for control in (self.prefix, self.date_enabled, self.suffix_enabled, self.start_index, self.padding):
            control.setEnabled(not enabled)
        self.date.setEnabled(not enabled and self.date_enabled.isChecked())
        self.suffix.setEnabled(not enabled and self.suffix_enabled.isChecked())
        self.update_preview()

    def task_from_form(self):
        input_dir = Path(self.input.text())
        if not input_dir.is_dir():
            raise ValueError("请选择有效的源文件夹")
        output_dir = self.output.text().strip()
        if not output_dir:
            output_parent = input_dir.parent
            task_name = input_dir.name + "_成品"
        else:
            out_path = Path(output_dir)
            output_parent = out_path.parent
            task_name = out_path.name
        return RenameTask(str(input_dir), str(output_parent), task_name, self.prefix.text(),
                          self.titles.toPlainText(), self.date.text() if self.date_enabled.isChecked() else "",
                          self.suffix.text() if self.suffix_enabled.isChecked() else "",
                          self.start_index.value(), self.padding.value(), self.copy.isChecked(),
                          direct_replace=self.direct_replace.isChecked())

    def update_preview(self):
        try:
            task = self.task_from_form()
            files = sorted((x for x in task.input_dir.iterdir() if x.is_file()), key=lambda x: natural_key(x.name))
            raw_lines = self.titles.toPlainText().splitlines()
            blank_lines = sum(1 for line in raw_lines if not str(line).strip())
            title_count = len(task.titles)
            file_count = len(files)

            # Update file count + match hint
            self.preview_title.setText(
                f"待处理文件：{file_count} 个｜有效标题：{title_count} 行"
                + (f"（已忽略空行 {blank_lines}）" if blank_lines else "")
            )
            if hasattr(self, "title_match_hint"):
                if file_count == 0:
                    hint = "请先选择源文件夹。"
                    style = "color:#94a3b8;background:#0b1424;padding:6px 8px;border-radius:5px;"
                elif title_count == 0:
                    hint = f"尚未填写标题：{file_count} 个文件将按规则使用原文件名。"
                    style = "color:#fde68a;background:#422006;padding:6px 8px;border-radius:5px;"
                elif title_count == file_count:
                    hint = (
                        f"✓ 一一对应：序号 01…{file_count:02d} 的源文件 ↔ 标题第 1…{title_count} 行。"
                        "可在右侧表核对，避免空格/空行错位。"
                    )
                    style = "color:#86efac;background:#052e1a;padding:6px 8px;border-radius:5px;"
                elif title_count < file_count:
                    hint = (
                        f"⚠ 标题只有 {title_count} 行，文件有 {file_count} 个："
                        f"第 {title_count + 1:02d}…{file_count:02d} 将用原文件名。"
                        + (f" 另有 {blank_lines} 个空行已被忽略。" if blank_lines else "")
                    )
                    style = "color:#fde68a;background:#422006;padding:6px 8px;border-radius:5px;"
                else:
                    hint = (
                        f"⚠ 标题 {title_count} 行多于文件 {file_count} 个："
                        f"多出的第 {file_count + 1}…{title_count} 行不会用到。"
                    )
                    style = "color:#fca5a5;background:#450a0a;padding:6px 8px;border-radius:5px;"
                self.title_match_hint.setText(hint)
                self.title_match_hint.setStyleSheet(style)

            self.preview.setRowCount(0)
            self.preview.setRowCount(file_count)

            for offset, item in enumerate(files):
                name, adjusted = task.render_name_info(item.name, task.start_index + offset)
                note = " (已自动清洗/截断)" if adjusted else ""
                seq = offset + 1
                title_index = offset  # titles 已去掉空行后的第 N 条对应第 N 个文件
                if 0 <= title_index < len(task.titles):
                    title_text = task.titles[title_index]
                    title_cell = f"#{title_index + 1} {title_text}"
                else:
                    title_cell = "（无标题 → 用原名）"

                idx_item = QTableWidgetItem(f"{seq:02d}")
                idx_item.setFlags(idx_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.preview.setItem(offset, 0, idx_item)

                src_item = QTableWidgetItem(item.name)
                src_item.setFlags(src_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.preview.setItem(offset, 1, src_item)

                title_item = QTableWidgetItem(title_cell)
                title_item.setFlags(title_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                title_item.setToolTip(
                    "标题列表去掉空行后的第 N 行对应自然排序后的第 N 个文件。\n"
                    "若文案里有多余空行/空格，请看本列是否对得上。"
                )
                self.preview.setItem(offset, 2, title_item)

                dst_item = QTableWidgetItem(f"{name}{note}")
                dst_item.setFlags(dst_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.preview.setItem(offset, 3, dst_item)

        except Exception as exc:
            self.preview_title.setText("待处理文件总数：0 个")
            self.preview.setRowCount(0)
            if hasattr(self, "title_match_hint"):
                self.title_match_hint.setText(f"预览无法刷新：{exc}")
            if hasattr(self, "log"):
                self.log.appendPlainText(f"预览刷新错误: {exc}")

    def load_titles(self):
        folder = Path(self.input.text())
        if folder.is_dir():
            self.titles.setPlainText("\n".join(x.stem for x in sorted(folder.iterdir(), key=lambda x: natural_key(x.name)) if x.is_file()))
            self.update_preview()

    def load_smart_titles(self):
        folder = Path(self.input.text())
        if not folder.is_dir():
            QMessageBox.information(self, "没有源文件夹", "请先选择包含视频的源文件夹。")
            return
        files = sorted(
            (item for item in folder.iterdir() if item.is_file() and item.suffix.lower() in VIDEO_EXTENSIONS),
            key=lambda item: natural_key(item.name),
        )
        if not files:
            QMessageBox.information(self, "没有视频", "当前文件夹没有可识别的视频文件。")
            return
        if self.title_thread and self.title_thread.isRunning():
            QMessageBox.information(self, "正在识别", "请等待当前标题识别任务完成。")
            return
        self.smart_titles_btn.setEnabled(False); self.smart_titles_btn.setText(f"正在识别 0/{len(files)}")
        self.progress.setValue(0); self.log.appendPlainText(f"开始按自然排序读取 {len(files)} 个视频内容……")
        self.title_thread = QThread(self); self.title_worker = SmartTitleWorker(files, self.transcribe_callable)
        self.title_worker.moveToThread(self.title_thread); self.title_thread.started.connect(self.title_worker.run)
        self.title_worker.log.connect(self.log.appendPlainText)
        self.title_worker.progress.connect(self._smart_title_progress)
        self.title_worker.finished.connect(self._smart_titles_done)
        self.title_worker.finished.connect(self.title_thread.quit)
        self.title_thread.finished.connect(self._smart_title_ended)
        self.title_thread.finished.connect(self.title_thread.deleteLater)
        self.title_thread.start()

    def _smart_title_progress(self, value):
        self.progress.setValue(value)
        folder = Path(self.input.text())
        count = len([item for item in folder.iterdir() if item.is_file() and item.suffix.lower() in VIDEO_EXTENSIONS]) if folder.is_dir() else 0
        current = min(count, max(0, round(value * count / 100)))
        self.smart_titles_btn.setText(f"正在识别 {current}/{count}")

    def _smart_titles_done(self, ok, message, titles):
        if titles:
            self.titles.setPlainText("\n".join(titles)); self.update_preview()
        self.log.appendPlainText(message)
        if not ok:
            self.log.appendPlainText("失败项未写入外文标题，已安全回退为原文件名；可补充 Gemini 密钥后重试。")

    def _smart_title_ended(self):
        self.smart_titles_btn.setEnabled(callable(self.transcribe_callable))
        self.smart_titles_btn.setText("智能读取视频内容为标题")
        self.title_worker = None; self.title_thread = None

    def run_tasks(self):
        try:
            task = self.task_from_form()
            count = sum(1 for x in task.input_dir.iterdir() if x.is_file())
            if not count:
                raise ValueError("源文件夹中没有任何文件")
        except Exception as exc:
            QMessageBox.warning(self, "无法开始重命名", str(exc))
            return
        if getattr(self, "thread", None):
            try:
                if self.thread.isRunning():
                    QMessageBox.information(self, "任务进行中", "请等待当前重命名结束。")
                    return
            except RuntimeError:
                self.thread = None
        self.thread = QThread(self)
        self.worker = RenameWorker([task])
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.log.connect(self.log.appendPlainText)
        self.worker.progress.connect(self.progress.setValue)
        self.worker.finished.connect(self.done)
        self.worker.finished.connect(self.thread.quit)
        self.thread.finished.connect(self._ended)
        self.thread.finished.connect(self.thread.deleteLater)
        self.run_btn.setEnabled(False)
        self.thread.start()

    def done(self, ok, message):
        self.run_btn.setEnabled(True)
        self.log.appendPlainText(message)
        (QMessageBox.information if ok else QMessageBox.critical)(self, "执行完成" if ok else "执行失败", message)
        if ok:
            self._clear_task_list_after_success()

    def _clear_task_list_after_success(self):
        """执行成功后清空本页任务相关输入，避免误对同一批再跑一次。"""
        self.tasks = []
        try:
            self.titles.blockSignals(True)
            self.titles.clear()
        finally:
            self.titles.blockSignals(False)
        try:
            self.input.blockSignals(True)
            self.input.clear()
        finally:
            self.input.blockSignals(False)
        try:
            self.output.blockSignals(True)
            self.output.clear()
        finally:
            self.output.blockSignals(False)
        self.progress.setValue(0)
        self.update_preview()
        self.log.appendPlainText("已清空任务列表（源文件夹 / 标题 / 预览），可继续添加下一批。")

    def _ended(self):
        self.worker = None
        self.thread = None
