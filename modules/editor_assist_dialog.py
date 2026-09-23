"""Optional editor tools, with explicit review before any data is changed."""
from __future__ import annotations

import copy
import json
import tempfile
from pathlib import Path

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QColorDialog, QComboBox, QDialog, QDoubleSpinBox,
    QFileDialog, QFontComboBox, QGridLayout, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QListWidget, QMessageBox, QPlainTextEdit, QPushButton, QSpinBox,
    QTabWidget, QTableWidget, QTableWidgetItem, QTextEdit, QVBoxLayout, QWidget,
)

from . import editor_assist as core
from .caption_rich import codepoint_offset


class AssistWorker(QThread):
    progress = Signal(str)
    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, job, parent=None):
        super().__init__(parent)
        self.job = job

    def run(self):
        try:
            result = self.job(self.progress.emit, self.isInterruptionRequested)
            self.completed.emit(result)
        except Exception as exc:
            self.failed.emit(str(exc))


def button(text, callback, layout):
    widget = QPushButton(text)
    widget.clicked.connect(callback)
    layout.addWidget(widget)
    return widget


def table(headers):
    widget = QTableWidget(0, len(headers))
    widget.setHorizontalHeaderLabels(headers)
    widget.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
    widget.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    widget.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
    widget.horizontalHeader().setStretchLastSection(True)
    widget.verticalHeader().setVisible(False)
    return widget


def fill(widget, rows):
    widget.setRowCount(len(rows))
    for i, row in enumerate(rows):
        for j, value in enumerate(row):
            item = QTableWidgetItem(str(value))
            item.setToolTip(str(value))
            widget.setItem(i, j, item)


class EditorAssistDialog(QDialog):
    def __init__(self, page, initial_tab=0):
        super().__init__(page)
        self.page = page
        self.worker = None
        self._closing = False
        self.setWindowTitle("Reels 辅助工具")
        self.setMinimumSize(580, 400)
        screen = self.screen().availableGeometry()
        self.resize(min(1080, screen.width()-60), min(760, screen.height()-80))
        root = QVBoxLayout(self)
        self.status = QLabel("检查结果是风险提示，不代表绝对准确；不会自动修改字幕或移动素材。")
        self.status.setWordWrap(True)
        root.addWidget(self.status)
        self.tabs = QTabWidget()
        root.addWidget(self.tabs)
        self.rows = []
        for i in range(page.videos.count()):
            path = page.videos.item(i).text()
            key = page._timeline_key(path)
            self.rows.append(dict(path=path, key=key))
        self._review_tab()
        self._rich_tab()
        self._bilingual_tab()
        self._similar_tab()
        self._export_tab()
        self.tabs.setCurrentIndex(initial_tab)
        close = QPushButton("关闭")
        close.clicked.connect(self.reject)
        root.addWidget(close)

    def _tab(self, name):
        widget = QWidget()
        layout = QVBoxLayout(widget)
        self.tabs.addTab(widget, name)
        return layout

    def _media_combo(self, layout):
        combo = QComboBox()
        combo.setMinimumWidth(0)
        for row in self.rows:
            combo.addItem(Path(row["path"]).name, row)
            combo.setItemData(combo.count()-1, row["path"], Qt.ItemDataRole.ToolTipRole)
        index = next((i for i, row in enumerate(self.rows) if row["key"] == self.page._current_video_key()), 0)
        combo.setCurrentIndex(index)
        layout.addWidget(combo)
        return combo

    def _snapshot(self, row):
        key = row["key"]
        source = self.page._timeline_key(self.page._caption_source_for_video(row["path"]))
        srt = self.page.timeline_overrides.get(key) or self.page.timeline_overrides.get(source, "")
        words = self.page.timeline_words.get(key) or self.page.timeline_words.get(source, "")
        if not srt and words:
            srt = self.page._group_words_for_current_layout(words)
        state = copy.deepcopy(self.page.timeline_edit_states.get(key, {}))
        return dict(**row, srt=srt, words=words, state=state,
                    fingerprint=core.fingerprint(srt, words, state))

    def _valid_snapshot(self, snapshot):
        if self._snapshot(snapshot_row(snapshot))["fingerprint"] != snapshot["fingerprint"]:
            raise ValueError("字幕/剪辑已发生变化，请重新检查，避免覆盖新结果")

    def _guard(self, function):
        try:
            return function()
        except Exception as exc:
            QMessageBox.warning(self, "未完成", str(exc))

    def _run(self, job, finished):
        if self.worker and self.worker.isRunning():
            QMessageBox.information(self, "请稍候", "已有任务正在运行")
            return
        self.worker = AssistWorker(job, self)
        self.tabs.setEnabled(False)
        self.worker.progress.connect(self.status.setText)
        self.worker.completed.connect(lambda result: self._guard(lambda: finished(result)))
        self.worker.failed.connect(lambda message: QMessageBox.warning(self, "未完成", message))
        self.worker.finished.connect(self._worker_finished)
        self.worker.start()

    def _worker_finished(self):
        self.tabs.setEnabled(True)
        self.worker.deleteLater()
        self.worker = None
        if self._closing:
            super().reject()

    def reject(self):
        if self.worker and self.worker.isRunning():
            self._closing = True
            self.worker.requestInterruption()
            self.status.setText("正在停止；等待当前文件操作安全结束后关闭…")
            return
        self.page.player.pause()
        super().reject()

    def _review_tab(self):
        layout = self._tab("字幕检查")
        self.review_media = self._media_combo(layout)
        row = QHBoxLayout()
        button("检查当前", lambda: self._guard(self._audit), row)
        button("检查全部", lambda: self._guard(self._audit_all), row)
        button("定位/试听", lambda: self._guard(self._audition), row)
        layout.addLayout(row)
        self.review = table(["序号", "开始(s)", "结束(s)", "当前文字", "词轴文字", "风险提示"])
        self.review.setColumnWidth(3, 230)
        self.review.setColumnWidth(4, 220)
        self.review.itemSelectionChanged.connect(self._select_review)
        layout.addWidget(self.review)
        timing = QHBoxLayout()
        self.start, self.end = QDoubleSpinBox(), QDoubleSpinBox()
        for label, control in (("开始", self.start), ("结束", self.end)):
            control.setRange(0, 86400)
            control.setDecimals(3)
            timing.addWidget(QLabel(label))
            timing.addWidget(control)
        button("保存所选时间", lambda: self._guard(self._apply_time), timing)
        layout.addLayout(timing)
        self.review_media.currentIndexChanged.connect(lambda: self.review.setRowCount(0))
        self.audit_snapshot = None
        self.audit_items = []

    def _audit(self):
        row = self.review_media.currentData()
        if not row:
            raise ValueError("请先添加视频")
        self.audit_snapshot = self._snapshot(row)
        snap = self.audit_snapshot
        if not core.parse_srt(snap["srt"]):
            raise ValueError("此视频没有字幕，请先提取")
        from .dynamic_caption_page import caption_clock_segments, retime_srt_for_video_segments
        baseline = self.page.timeline_overrides_source.get(snap["key"], "")
        if snap["state"].get("captions_timeline_aligned") and baseline:
            external = self.page._timeline_key(self.page._caption_source_for_video(snap["path"])) != snap["key"]
            baseline = retime_srt_for_video_segments(baseline, caption_clock_segments(snap["state"], external))
        duration = max((int(c["end"]) for c in snap["state"].get("tracks", {}).get("video", [])), default=0)
        self.audit_items = core.audit_captions(snap["srt"], snap["words"], baseline, duration)
        fill(self.review, [(r["index"]+1, f'{r["start"]/1000:.3f}', f'{r["end"]/1000:.3f}', r["text"], r["recognized"],
                            "；".join(r["reasons"]) or "未发现结构风险，仍需试听") for r in self.audit_items])
        self.status.setText(f"检查完成：{sum(bool(r['reasons']) for r in self.audit_items)} / {len(self.audit_items)} 句需关注。文字一致也不能证明口型一定同步。")

    def _audit_all(self):
        messages = []
        for row in self.rows:
            snapshot = self._snapshot(row)
            results = core.audit_captions(snapshot["srt"], snapshot["words"])
            messages.append(f"{Path(row['path']).name}：" + (f"{sum(bool(r['reasons']) for r in results)}/{len(results)} 句风险" if results else "尚无字幕"))
        dialog = QDialog(self)
        dialog.setWindowTitle("批量检查汇总（选择视频后可逐句调整）")
        layout = QVBoxLayout(dialog)
        text = QPlainTextEdit("\n".join(messages))
        text.setReadOnly(True)
        layout.addWidget(text)
        dialog.resize(680, 460)
        dialog.exec()

    def _select_review(self):
        index = self.review.currentRow()
        if 0 <= index < len(self.audit_items):
            item = self.audit_items[index]
            self.start.setValue(item["start"]/1000)
            self.end.setValue(item["end"]/1000)

    def _audition(self):
        if not self.audit_snapshot or self.review.currentRow() < 0:
            raise ValueError("先检查并选中一句字幕")
        self._valid_snapshot(self.audit_snapshot)
        if self.audit_snapshot["key"] != self.page._current_video_key():
            raise ValueError("请先关闭工具，在视频列表选择此视频，再打开检查试听")
        if self.page._timeline_edits_active() and not getattr(self.page, "_precise_preview_active", False):
            raise ValueError("当前有剪辑操作，请先生成「轨道预览」再试听，避免将源视频时钟误当成片时钟")
        self.page.player.setPosition(round(self.start.value()*1000))
        self.page.player.play()
        QTimer.singleShot(max(100, round((self.end.value()-self.start.value())*1000)), self.page.player.pause)

    def _apply_time(self):
        index = self.review.currentRow()
        if not self.audit_snapshot or index < 0:
            raise ValueError("请先检查并选择字幕")
        self._valid_snapshot(self.audit_snapshot)
        srt, words = core.adjust_cue_times(self.audit_snapshot["srt"], self.audit_snapshot["words"],
                                          {index: (round(self.start.value()*1000), round(self.end.value()*1000))})
        self.page._apply_assist_caption_times(self.audit_snapshot["path"], srt, words)
        self._audit()

    def _rich_tab(self):
        layout = self._tab("局部样式")
        hint = QLabel("选择一句，再选中文字设置样式。规则随字幕样式预设保存；局部范围绑定完整句子，改字后需重新设置。\n命中样式的句子采用统一富文本排版＋可选逐词色块，不叠加原预设的弹跳/滚动动画；未命中的句子不变。")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.rich_config = copy.deepcopy(getattr(self.page, "_rich_caption_config", {}))
        self.rich_config.setdefault("rules", [])
        self.rich_config.setdefault("ranges", [])
        self.rich_cue = QComboBox()
        self.rich_cue.setMinimumWidth(0)
        for cue in core.parse_srt(self.page._current_timeline_srt_text()):
            self.rich_cue.addItem(cue.text[:90], cue.text)
        layout.addWidget(self.rich_cue)
        self.rich_text = QTextEdit()
        self.rich_text.setReadOnly(True)
        self.rich_text.setPlainText(self.rich_cue.currentData() or "")
        self.rich_text.setMaximumHeight(120)
        self.rich_cue.currentIndexChanged.connect(lambda: self.rich_text.setPlainText(self.rich_cue.currentData() or ""))
        layout.addWidget(self.rich_text)
        form = QHBoxLayout()
        self.rich_font = QFontComboBox()
        self.rich_font.setMinimumWidth(80)
        self.rich_size = QSpinBox()
        self.rich_size.setRange(20, 220)
        self.rich_size.setValue(60)
        self.rich_bold = QCheckBox("粗体")
        self.rich_italic = QCheckBox("斜体")
        for w in (self.rich_font, self.rich_size, self.rich_bold, self.rich_italic):
            form.addWidget(w)
        layout.addLayout(form)
        colors = QHBoxLayout()
        self.rich_color = QLineEdit("#FFFF00")
        self.rich_background = QLineEdit("")
        self.rich_background.setPlaceholderText("留空无背景")
        for name, field in (("文字色", self.rich_color), ("背景色", self.rich_background)):
            button(name, lambda _checked=False, f=field: self._choose_color(f), colors)
            colors.addWidget(field)
        button("应用到所选文字", lambda: self._guard(self._add_range), colors)
        layout.addLayout(colors)
        rule = QHBoxLayout()
        self.rule_kind = QComboBox()
        self.rule_kind.addItem("关键词", "keyword")
        self.rule_kind.addItem("数字/金额", "number")
        self.rule_keyword = QLineEdit()
        self.rule_keyword.setPlaceholderText("关键词（按原文匹配，不执行正则表达式）")
        rule.addWidget(self.rule_kind)
        rule.addWidget(self.rule_keyword)
        button("添加规则", lambda: self._guard(self._add_rule), rule)
        layout.addLayout(rule)
        self.rich_list = QListWidget()
        layout.addWidget(self.rich_list)
        actions = QHBoxLayout()
        self.rich_karaoke = QCheckBox("保留逐词跟读色块")
        self.rich_karaoke.setChecked(self.rich_config.get("karaoke", True))
        actions.addWidget(self.rich_karaoke)
        button("删除所选", self._delete_rich, actions)
        button("保存并应用", lambda: self._guard(self._save_rich), actions)
        layout.addLayout(actions)
        self._refresh_rich()

    def _choose_color(self, field):
        color = QColorDialog.getColor(parent=self)
        if color.isValid():
            field.setText(color.name())

    def _rich_style(self):
        from .caption_rich import clean_style
        style = dict(font=self.rich_font.currentFont().family(), size=self.rich_size.value(),
                     bold=self.rich_bold.isChecked(), italic=self.rich_italic.isChecked(),
                     color=self.rich_color.text().strip(), background=self.rich_background.text().strip())
        cleaned = clean_style(style)
        if "color" not in cleaned or (style["background"] and "background" not in cleaned):
            raise ValueError("颜色请使用 #RRGGBB 格式")
        return cleaned

    def _add_range(self):
        cursor = self.rich_text.textCursor()
        text = self.rich_text.toPlainText()
        if not cursor.hasSelection():
            raise ValueError("请在上方文字中选中几个字")
        self.rich_config["ranges"].append(dict(text=text, start=codepoint_offset(text, cursor.selectionStart()),
            end=codepoint_offset(text, cursor.selectionEnd()), style=self._rich_style()))
        self._refresh_rich()

    def _add_rule(self):
        kind, keyword = self.rule_kind.currentData(), self.rule_keyword.text().strip()
        if kind == "keyword" and not keyword:
            raise ValueError("请输入关键词")
        self.rich_config["rules"].append(dict(kind=kind, keyword=keyword, style=self._rich_style()))
        self._refresh_rich()

    def _refresh_rich(self):
        self.rich_list.clear()
        for item in self.rich_config["rules"]:
            self.rich_list.addItem("规则："+(item["keyword"] if item["kind"] == "keyword" else "数字/金额")+"  "+str(item["style"]))
        for item in self.rich_config["ranges"]:
            self.rich_list.addItem("局部："+item["text"][item["start"]:item["end"]]+"  "+str(item["style"]))

    def _delete_rich(self):
        index = self.rich_list.currentRow()
        if index < 0:
            return
        count = len(self.rich_config["rules"])
        if index < count:
            del self.rich_config["rules"][index]
        else:
            del self.rich_config["ranges"][index-count]
        self._refresh_rich()

    def _save_rich(self):
        self.rich_config["karaoke"] = self.rich_karaoke.isChecked()
        self.page._rich_caption_config = copy.deepcopy(self.rich_config)
        self.page._remember_batch_style_snapshot()
        self.page._refresh_live_preview()
        self.page._save_style_preferences()
        self.status.setText("局部样式已应用；可在原字幕样式入口保存/导出预设，不会改变文案。")

    def _bilingual_tab(self):
        layout = self._tab("双语对齐")
        hint = QLabel("复制三列：视频文件名｜原文｜译文。每个视频一行，单元格内每行一句，原文与译文行数一致。\n只对齐你提供的译文，不自动翻译或改动原字幕；未匹配句子需手动确认时间。")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        action = QHBoxLayout()
        button("粘贴表格", lambda: self._guard(self._paste_bilingual), action)
        button("对齐全部", lambda: self._guard(self._align_all), action)
        button("导出全部 SRT", lambda: self._guard(self._export_bilingual), action)
        layout.addLayout(action)
        self.bilingual_inputs = table(["视频文件名", "原文（每行一句）", "译文（每行一句）"])
        self.bilingual_inputs.setEditTriggers(QTableWidget.EditTrigger.DoubleClicked | QTableWidget.EditTrigger.EditKeyPressed)
        self.bilingual_inputs.setMaximumHeight(170)
        layout.addWidget(self.bilingual_inputs)
        self.bilingual_results = table(["视频", "原文", "译文", "开始(s)", "结束(s)", "状态"])
        layout.addWidget(self.bilingual_results)
        edit = QHBoxLayout()
        self.bi_start, self.bi_end = QDoubleSpinBox(), QDoubleSpinBox()
        for label, control in (("开始", self.bi_start), ("结束", self.bi_end)):
            control.setRange(0, 86400)
            control.setDecimals(3)
            edit.addWidget(QLabel(label))
            edit.addWidget(control)
        button("确认所选时间", lambda: self._guard(self._confirm_bilingual), edit)
        layout.addLayout(edit)
        self.bilingual_results.itemSelectionChanged.connect(self._select_bilingual)
        self.bi_jobs = []
        self.bi_flat = []
        self.bilingual_inputs.itemChanged.connect(self._invalidate_bilingual)

    def _invalidate_bilingual(self, *_):
        self.bi_jobs = []
        self.bi_flat = []
        self.bilingual_results.setRowCount(0)

    def _paste_bilingual(self):
        rows = core.parse_bilingual_tsv(QApplication.clipboard().text())
        fill(self.bilingual_inputs, rows)

    def _align_all(self):
        jobs = []
        seen = set()
        for i in range(self.bilingual_inputs.rowCount()):
            values = [self.bilingual_inputs.item(i, j).text() if self.bilingual_inputs.item(i, j) else "" for j in range(3)]
            matches = [r for r in self.rows if values[0].strip() in (r["path"], Path(r["path"]).name)]
            if len(matches) != 1:
                raise ValueError(f"第 {i+1} 行视频不存在或同名不唯一，请填写完整路径")
            snap = self._snapshot(matches[0])
            if snap["key"] in seen:
                raise ValueError("同一视频重复出现，请合并到一个项目")
            seen.add(snap["key"])
            rows = core.align_bilingual(snap["words"], values[1].strip().splitlines(), values[2].strip().splitlines())
            jobs.append(dict(snapshot=snap, rows=rows))
        if not jobs:
            raise ValueError("请先粘贴表格")
        self.bi_jobs = jobs
        self._show_bilingual()

    def _show_bilingual(self):
        self.bi_flat = [(job, row) for job in self.bi_jobs for row in job["rows"]]
        fill(self.bilingual_results, [(Path(job["snapshot"]["path"]).name, row["original"], row["translation"],
            "" if row["start"] is None else f'{row["start"]/1000:.3f}', "" if row["end"] is None else f'{row["end"]/1000:.3f}',
            "；".join(row["reasons"]) or ("人工确认" if row.get("manual") else "文字完全匹配，建议试听")) for job, row in self.bi_flat])

    def _select_bilingual(self):
        i = self.bilingual_results.currentRow()
        if 0 <= i < len(self.bi_flat):
            row = self.bi_flat[i][1]
            self.bi_start.setValue((row["start"] or 0)/1000)
            self.bi_end.setValue((row["end"] or 0)/1000)

    def _confirm_bilingual(self):
        i = self.bilingual_results.currentRow()
        if i < 0:
            raise ValueError("请选择一句字幕")
        start, end = round(self.bi_start.value()*1000), round(self.bi_end.value()*1000)
        if end <= start:
            raise ValueError("结束必须晚于开始")
        self.bi_flat[i][1].update(start=start, end=end, reasons=[], manual=True)
        self._show_bilingual()

    def _export_bilingual(self):
        if not self.bi_jobs:
            raise ValueError("请先对齐")
        exports = []
        for job in self.bi_jobs:
            self._valid_snapshot(job["snapshot"])
            exports.append(core.bilingual_srts(job["rows"]))
        folder = QFileDialog.getExistingDirectory(self, "选择双语字幕输出目录")
        if not folder:
            return
        dest = Path(tempfile.mkdtemp(prefix="bilingual_", dir=folder))
        for i, (job, srts) in enumerate(zip(self.bi_jobs, exports), 1):
            for suffix, srt in zip(("source", "translation", "bilingual"), srts):
                (dest/f"{i:03d}_{Path(job['snapshot']['path']).stem}_{suffix}.srt").write_text(srt, encoding="utf-8-sig")
        self.status.setText(f"双语字幕已导出：{dest}（原字幕未改动）")

    def _similar_tab(self):
        layout = self._tab("相似素材")
        hint = QLabel("图像按画面相似度分组；视频比较 10%、50%、90% 三处画面。不是内容理解，也不是重复文件证明。\n扫描不移动文件；查看结果后可复制分组，原素材不删除。")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        controls = QHBoxLayout()
        button("选择素材", self._choose_similar, controls)
        self.sim_threshold = QSpinBox()
        self.sim_threshold.setRange(0, 16)
        self.sim_threshold.setValue(5)
        self.sim_threshold.setToolTip("哈希差异阈值：越小越严格；平色画面可能误判，需人工核对")
        controls.addWidget(QLabel("相似容差"))
        controls.addWidget(self.sim_threshold)
        button("扫描", lambda: self._guard(self._scan_similar), controls)
        button("复制分组", lambda: self._guard(self._copy_similar), controls)
        layout.addLayout(controls)
        self.sim_table = table(["组", "文件", "完整路径"])
        layout.addWidget(self.sim_table)
        self.sim_files, self.sim_groups = [], []
        self.sim_threshold.valueChanged.connect(lambda: setattr(self, "sim_groups", []))

    def _choose_similar(self):
        files, _ = QFileDialog.getOpenFileNames(self, "选择图片或视频", "", "素材 (*.png *.jpg *.jpeg *.webp *.bmp *.tif *.tiff *.heif *.heic *.mp4 *.mov *.mkv *.avi)")
        if files:
            self.sim_files = list(dict.fromkeys(files))
            self.sim_groups = []
            fill(self.sim_table, [("待扫描", Path(p).name, p) for p in self.sim_files])

    def _scan_similar(self):
        if len(self.sim_files) < 2:
            raise ValueError("请至少选择两个素材")
        ffmpeg = self.page._resolve_ffmpeg()
        files, threshold = list(self.sim_files), self.sim_threshold.value()
        self.sim_groups = []
        def job(progress, canceled):
            items, failures = [], []
            for i, path in enumerate(files):
                if canceled():
                    return [], ["扫描已取消"]
                progress(f"扫描素材 {i+1}/{len(files)}：{Path(path).name}")
                try:
                    signature = core.media_signature(path)
                    kind, hashes = core.visual_hashes(ffmpeg, path)
                    if core.media_signature(path) != signature:
                        raise ValueError("扫描时素材发生变化")
                    items.append(dict(path=path, signature=signature, kind=kind, hashes=hashes))
                except Exception as exc:
                    failures.append(f"{Path(path).name}：{exc}")
            return core.group_similar(items, threshold), failures
        def done(result):
            self.sim_groups, failures = result
            fill(self.sim_table, [(i, Path(item["path"]).name, item["path"]) for i, group in enumerate(self.sim_groups, 1) for item in group])
            self.status.setText(f"发现 {len(self.sim_groups)} 组相似素材；未分组的素材保持原样。"+"；".join(failures))
        self._run(job, done)

    def _copy_similar(self):
        if not self.sim_groups:
            raise ValueError("请先扫描并确认分组")
        folder = QFileDialog.getExistingDirectory(self, "选择分组复制目录")
        if folder:
            dest = Path(tempfile.mkdtemp(prefix="similar_", dir=folder))
            groups = copy.deepcopy(self.sim_groups)
            self._run(lambda progress, canceled: core.copy_group_files(groups, dest),
                      lambda _: self.status.setText(f"已复制到 {dest}；原文件全部保留。"))

    def _export_tab(self):
        layout = self._tab("剪辑工程")
        self.export_media = self._media_combo(layout)
        hint = QLabel("导出 FCPXML ＋ SRT，可交给支持该格式的剪辑软件继续编辑。\n包含基础画面切点、独立音轨和字幕文字。蒙版、动态跟读、调色、转场和音量包络不保证还原；变速片段会拒绝导出。\n工程引用本机源文件，不打包素材。不同剪辑软件对字幕支持不同，可单独导入 SRT。导出不等于成品渲染。")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        form = QHBoxLayout()
        self.export_fps = QComboBox()
        self.export_fps.addItems(["30", "25", "24", "30000/1001", "60"])
        form.addWidget(QLabel("工程帧率"))
        form.addWidget(self.export_fps)
        button("导出当前工程", lambda: self._guard(self._export_project), form)
        layout.addLayout(form)
        layout.addStretch()

    def _export_project(self):
        row = self.export_media.currentData()
        if not row:
            raise ValueError("请先添加视频")
        snapshot = self._snapshot(row)
        folder = QFileDialog.getExistingDirectory(self, "选择工程输出目录")
        if not folder:
            return
        ffmpeg, fps = self.page._resolve_ffmpeg(), self.export_fps.currentText()
        def job(progress, canceled):
            paths = {snapshot["path"]}
            for name in ("video", "original_audio", "bgm", "tts"):
                paths.update(str(c.get("path") or snapshot["path"]) for c in snapshot["state"].get("tracks", {}).get(name, []))
            info = {}
            for path in paths:
                if canceled():
                    raise ValueError("工程导出已取消")
                progress(f"读取素材信息：{Path(path).name}")
                info[path] = core.probe_media(ffmpeg, path)
            xml, warnings = core.build_fcpxml(snapshot["path"], snapshot["state"], snapshot["srt"], info, fps)
            return xml, warnings
        def done(result):
            self._valid_snapshot(snapshot)
            dest = Path(tempfile.mkdtemp(prefix="edit_project_", dir=folder))
            (dest/"timeline.fcpxml").write_text(result[0], encoding="utf-8")
            (dest/"captions.srt").write_text(snapshot["srt"], encoding="utf-8-sig")
            (dest/"说明.txt").write_text("\n".join(result[1]), encoding="utf-8-sig")
            self.status.setText(f"工程已导出：{dest}。请在目标剪辑软件中检查导入效果。")
        self._run(job, done)


def snapshot_row(snapshot):
    return dict(path=snapshot["path"], key=snapshot["key"])
