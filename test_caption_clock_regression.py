"""Source-only regressions: speech clock remapping and actual alpha MOV timing."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import subprocess
from pathlib import Path
from types import SimpleNamespace
from PySide6.QtWidgets import QApplication, QPlainTextEdit
from PySide6.QtCore import QSettings, Qt
from modules import dynamic_caption_page as d
from modules import caption_qt_burn as burn

SOURCE = d.events_to_srt([(0.5, 1.2, "AAA"), (2.5, 3.2, "BBB"), (4.5, 5.2, "CCC")])

def segment(start, end, source_start, source_end):
    return dict(start=start, end=end, source_start=source_start, source_end=source_end, media_type="video")

def state(segments):
    return {"tracks": {"video": segments, "original_audio": segments}}

class PageHarness:
    _ensure_caption_source_snapshot = d.DynamicCaptionPage._ensure_caption_source_snapshot
    _sync_captions_after_video_edit = d.DynamicCaptionPage._sync_captions_after_video_edit
    _timeline_track_srt_changed = d.DynamicCaptionPage._timeline_track_srt_changed
    _timeline_key = staticmethod(str)
    _current_video_key = lambda self: "clip"
    _timeline_source = lambda self: "clip"
    _caption_source_for_video = lambda self, key: key
    _invalidate_preview_caption_overlay = lambda self: None

    def __init__(self):
        self.timeline_words = {"clip": SOURCE}
        self.timeline_overrides = {"clip": SOURCE}
        self.timeline_words_source = {"clip": SOURCE}
        self.timeline_overrides_source = {"clip": SOURCE}
        self.timeline_edit_states = {}

def test_repeated_delete_restore_and_silent_image():
    page = PageHarness()
    cut = state([segment(0, 2000, 0, 2000), segment(2000, 4000, 4000, 6000)])
    page._sync_captions_after_video_edit("clip", cut)
    first = page.timeline_words["clip"]
    assert [c[2] for c in d.parse_srt(first)] == ["AAA", "CCC"]
    assert d.parse_srt(first)[1][0] == 2.5
    page._sync_captions_after_video_edit("clip", cut)
    assert page.timeline_words["clip"] == first, "second notification shifted captions again"
    page._sync_captions_after_video_edit("clip", state([segment(0, 2000, 4000, 6000)]))
    assert d.parse_srt(page.timeline_words["clip"]) == [(0.5, 1.2, "CCC")]
    original = state([segment(0, 6000, 0, 6000)])
    page._sync_captions_after_video_edit("clip", original)
    assert page.timeline_words["clip"] == SOURCE, "undo did not restore source clock"
    original["tracks"]["video"] = [dict(start=0, end=6000, media_type="image")]
    page._sync_captions_after_video_edit("clip", original)
    assert page.timeline_words["clip"] == SOURCE, "silent image moved dialogue"
    page._sync_captions_after_video_edit("clip", state([segment(0, 2000, 0, 2000)]))
    assert [c[2] for c in d.parse_srt(page.timeline_words["clip"])] == ["AAA"]
    page._sync_captions_after_video_edit("clip", state([]))
    assert page.timeline_words["clip"] == "" and page.timeline_overrides["clip"] == ""
    print("PASS repeated cut / undo / image-only / tail trim / delete all")

def test_structural_srt_signal_is_not_manual_edit():
    page = PageHarness()
    page.timeline_edit_states["clip"] = state([segment(0, 6000, 0, 6000)])
    edited = state([segment(0, 2000, 4000, 6000)])
    page.canva_timeline = SimpleNamespace(canvas=SimpleNamespace(current_state=lambda: edited))
    page.override_text = SimpleNamespace(setPlainText=lambda text: (_ for _ in ()).throw(AssertionError("structural signal treated as manual edit")))
    page._timeline_track_srt_changed("changed")
    print("PASS structural SRT signal suppression")

def test_reextracted_source_clock_survives_next_cut():
    segments = [segment(0, 2000, 0, 2000), segment(2000, 4000, 4000, 6000)]
    corrected = d.events_to_srt([(0.5, 1.2, "NEW_A"), (2.5, 3.2, "NEW_C")])
    lifted = d.restore_source_caption_clock(corrected, SOURCE, segments)
    assert d.parse_srt(lifted) == [(0.5, 1.2, "NEW_A"), (2.5, 3.2, "BBB"), (4.5, 5.2, "NEW_C")]
    assert d.retime_srt_for_video_segments(lifted, segments) == corrected
    sped = [segment(0, 1000, 4000, 6000)]
    assert d.parse_srt(d.retime_srt_for_video_segments(lifted, sped)) == [(0.25, 0.6, "NEW_C")]
    print("PASS post-cut ASR source clock / undo retention / speed mapping")

def test_real_overlay_keeps_blank_time():
    root = Path(__file__).resolve().parent
    ffmpeg = str(root / "tools/ffmpeg/bin/ffmpeg.exe")
    ffprobe = str(root / "tools/ffmpeg/bin/ffprobe.exe")
    output = root / "_smoke_caption_clock"
    output.mkdir(exist_ok=True)
    burn.instance_temp_dir = lambda name: output
    phrase = d.events_to_srt([(0.507, 0.912, "FIRST"), (2.013, 2.619, "SECOND")])
    mov = burn.bake_qt_caption_overlay_mov(ffmpeg, duration=3, settings={}, phrase_srt=phrase, word_srt="", target_w=108, target_h=192)
    manifest = mov.with_name("list.txt").read_text(encoding="utf-8")
    durations = [float(line.split()[1]) for line in manifest.splitlines() if line.startswith("duration ")]
    assert abs(sum(durations) - 3) < .001, durations
    stamps = subprocess.check_output([ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries", "frame=pts_time", "-of", "csv=p=0", str(mov)], text=True)
    pts = [float(line.strip().split(',')[0]) for line in stamps.splitlines() if line.strip()]
    for boundary in (0, .507, .912, 2.013, 2.619):
        assert any(abs(t - boundary) < .002 for t in pts), (boundary, pts)
    assert len(pts) < 30, "VFR unexpectedly duplicated hundreds of frames"
    print("PASS real FFmpeg alpha MOV: millisecond boundaries and silence preserved", pts)

def test_word_correction_preserves_anchors_and_next_phrase():
    words = d.events_to_srt([(0., .3, "hello"), (1., 1.3, "world"), (2., 2.3, "second"), (2.4, 2.8, "phrase")])
    old = d.events_to_srt([(0., 1.5, "hello world"), (2., 3., "second phrase")])
    corrected = d.events_to_srt([(0., 1.5, "hello dear world"), (2., 3., "second phrase")])
    result = d.parse_srt(d.align_word_srt_to_phrase_srt(words, corrected, old))
    assert result[0] == (0., .3, "hello") and result[2] == (1., 1.3, "world"), result
    assert result[3:] == [(2., 2.3, "second"), (2.4, 2.8, "phrase")], result
    print("PASS diff anchors / estimated insertion / next phrase word cursor")

def test_batch_dialog_projects_are_independent():
    rows = [dict(path="one.mp4", key="one", srt=SOURCE, words=SOURCE),
            dict(path="two.mp4", key="two", srt=SOURCE, words=SOURCE)]
    dialog = d.BatchScriptProofreadDialog(None, rows)
    dialog.table.cellWidget(0, 2).setPlainText("AAA NEW CCC")
    dialog.table.cellWidget(1, 2).setPlainText("AAA OTHER CCC")
    dialog.compare_all()
    assert dialog.results[0]["corrected"] != dialog.results[1]["corrected"]
    for row in (0, 1):
        assert [(s, e) for s, e, _ in d.parse_srt(dialog.results[row]["corrected"])] == [(s, e) for s, e, _ in d.parse_srt(SOURCE)]
        assert dialog.table.item(row, 0).checkState() == Qt.CheckState.Unchecked
    dialog.table.item(0, 0).setCheckState(Qt.CheckState.Checked)
    dialog.apply_checked()
    assert [row["key"] for row in dialog.selected_results] == ["one"]
    dialog.table.cellWidget(0, 2).setPlainText("changed again")
    assert 0 not in dialog.results
    dialog.close()
    print("PASS batch per-project isolation / opt-in replacement / edit invalidation")

def test_cache_exact_paths_precede_portable_name():
    mapping = {"name:same.mp4": "wrong cached caption", "E:/two/same.mp4": "correct"}
    assert d._lookup_settings_map(mapping, "E:/missing/same.mp4", "E:/two/same.mp4") == "correct"
    mapping["E:/two/same.mp4"] = ""
    assert d._lookup_settings_map(mapping, "E:/two/same.mp4") == ""
    print("PASS exact cache path precedence / intentional cleared captions")

def test_source_page_startup():
    root = Path(__file__).resolve().parent
    profile = root / "_smoke_caption_clock/profile"
    profile.mkdir(parents=True, exist_ok=True)
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(profile))
    os.environ["VIDEO_TOOLKIT_DISABLE_STYLE_MEMORY"] = "1"
    page = d.DynamicCaptionPage(lambda *a: "", lambda *a: "", lambda: str(root / "tools/ffmpeg/bin/ffmpeg.exe"), ["本地"], "本地")
    page.resize(1366, 768)
    page.show()
    QApplication.processEvents()
    assert page.batch_proofread_btn.text() == "批量校对"
    assert page.vertical_stack_btn.text() == "上下拼接"
    assert not page.auto_reextract_after_cut.isChecked()
    assert page.free_animation.findText("持续向上滚动") >= 0
    from modules.dynamic_caption_page import ALLOWED_VIDEO_INPUTS
    source = root / "_smoke_vertical_stack/top.mp4"
    stacked = root / "_smoke_vertical_stack/result_top.mp4"
    if source.exists() and stacked.exists():
        page._add(page.videos, [str(source)], ALLOWED_VIDEO_INPUTS)
        count_before = page.videos.count()
        page._add_stacked_caption_sources([str(stacked)])
        assert page.videos.count() == count_before + 1
        assert page._current_video_key() == page._timeline_key(stacked)
        assert page._active_source_tool_index == 1
        print("PASS stacked source added to real caption queue without clearing existing media")
    assert page.grab().save(str(root / "_smoke_caption_clock/source_page.png"))
    page.close()
    print("PASS source Reels page startup / batch button / scroll option / screenshot")

def test_real_canvas_delete_undo_redo_signals():
    from modules.canva_timeline import TimelineCanvas
    page = PageHarness()
    canvas = TimelineCanvas()
    pieces = [segment(0, 2000, 0, 2000), segment(2000, 4000, 2000, 4000), segment(4000, 6000, 4000, 6000)]
    canvas.set_project(6000, "clip", SOURCE, edit_state=state(pieces))
    page.timeline_edit_states["clip"] = canvas.current_state()
    page.canva_timeline = SimpleNamespace(canvas=canvas, set_srt=canvas.set_srt)
    page.override_text = QPlainTextEdit()
    page._append_run_log = lambda text: None
    canvas.srtChanged.connect(page._timeline_track_srt_changed)
    canvas.timelineEdited.connect(lambda edited: page._sync_captions_after_video_edit("clip", edited))
    canvas.selected = ("video", 1)
    canvas.delete_selected()
    assert [c[2] for c in d.parse_srt(page.timeline_words["clip"])] == ["AAA", "CCC"]
    assert d.parse_srt(page.timeline_words["clip"])[1][0] == 2.5
    assert canvas.undo()
    assert page.timeline_words["clip"] == SOURCE
    assert canvas.redo()
    assert d.parse_srt(page.timeline_words["clip"])[1][0] == 2.5
    canvas.close()
    print("PASS real timeline canvas delete / undo / redo signal sequence")

def test_scrolling_caption_actual_render():
    import cv2
    import numpy as np
    root = Path(__file__).resolve().parent
    output = root / "_smoke_caption_clock"
    output.mkdir(exist_ok=True)
    settings = dict(preset="滚动字幕 · 上浮", font="Arial", font_size=72,
                    caption_mode="自由文案动画（不对口型）", free_animation="持续向上滚动",
                    line_length=30, outline_width=2, margin_v=100, layers=[])
    srt = d.free_caption_srt("SCROLL TEST", 6, settings)
    assert len(d.parse_srt(srt)) == 1
    ass = output / "scroll.ass"
    d.write_ass(ass, srt, settings, "")
    assert "\\move(" in ass.read_text(encoding="utf-8-sig")
    video = output / "scroll.mp4"
    command = [str(root / "tools/ffmpeg/bin/ffmpeg.exe"), "-y", "-v", "error", "-f", "lavfi", "-i",
               "color=c=black:s=180x320:r=10:d=6", "-vf", d.ass_filter_expression(ass),
               "-c:v", "libx264", "-pix_fmt", "yuv420p", str(video)]
    subprocess.run(command, check=True, capture_output=True)
    cap = cv2.VideoCapture(str(video))
    centers = []
    for second in (1.5, 3., 4.5):
        cap.set(cv2.CAP_PROP_POS_MSEC, second * 1000)
        ok, frame = cap.read()
        assert ok
        ys, xs = np.where(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) > 150)
        assert len(ys) > 5
        centers.append(float(ys.mean()))
    cap.release()
    assert centers[0] > centers[1] > centers[2], centers
    print("PASS actual scrolling render: text moves upward", centers)

if __name__ == "__main__":
    app = QApplication.instance() or QApplication([])
    test_repeated_delete_restore_and_silent_image()
    test_structural_srt_signal_is_not_manual_edit()
    test_reextracted_source_clock_survives_next_cut()
    test_word_correction_preserves_anchors_and_next_phrase()
    test_batch_dialog_projects_are_independent()
    test_cache_exact_paths_precede_portable_name()
    test_real_overlay_keeps_blank_time()
    test_source_page_startup()
    test_real_canvas_delete_undo_redo_signals()
    test_scrolling_caption_actual_render()
