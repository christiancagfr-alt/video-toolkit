"""Regression tests for preview/export parity, ASR boundaries and watermark size.

Run with: python -m unittest test_reels_render_stability -v
Uses synthetic media and mocked recognition results; no model download/API calls.
"""
import json
import os
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPainter
from PySide6.QtWidgets import QApplication
from PIL import Image, ImageDraw
import numpy as np

from modules import asr_timing as timing
from modules import caption_qt_burn as burn
from modules import dynamic_caption_page as d
from modules import metadata_page as meta

ROOT = Path(__file__).resolve().parent
FFMPEG = str(ROOT / "tools/ffmpeg/bin/ffmpeg.exe")
FFPROBE = str(ROOT / "tools/ffmpeg/bin/ffprobe.exe")
APP = QApplication.instance() or QApplication([])
GREEK = "Ποια είναι τα πέντε πιο επικίνδυνα αμαρτήματα"
STYLE = dict(preset="双描边大字幕", effect="double_outline", font="Arial", font_size=82,
             caption_mode="语音同步字幕", letter_spacing=-4, word_spacing=-12,
             line_spacing=100, line_width=86, max_lines=2, line_length=28,
             outline_width=5, margin_v=310, position="底部", text_color="#FFCC00",
             outline_color="#111111", highlight_color="#FF3300", layers=[],
             encode_preset="fast", encoder_backend="cpu", provider="本地 Whisper（无需密钥）")


def ffmpeg(*args):
    return subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", *map(str, args)],
                          check=True, capture_output=True)


def probe(path):
    return json.loads(subprocess.check_output(
        [FFPROBE, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)]))


def pixels(image):
    image = image.convertToFormat(QImage.Format.Format_RGBA8888)
    return np.frombuffer(image.constBits(), dtype=np.uint8).reshape(image.height(), image.width(), 4).copy()


class AsrTimingTests(unittest.TestCase):
    def test_local_worker_retries_missing_window_tail(self):
        import app as main
        calls = []
        class FakeModel:
            def transcribe(self, path, **kwargs):
                with wave.open(str(path)) as wav:
                    duration = wav.getnframes() / wav.getframerate()
                calls.append((Path(path).name, kwargs["vad_filter"], duration))
                # Model simulation: VAD returns only the first half of each
                # window; retry returns the actual remaining words.
                limit = duration / 2 if kwargs["vad_filter"] else duration
                words = [SimpleNamespace(start=i * .5, end=min(i * .5 + .3, duration), word=f" w{i}")
                         for i in range(int(limit * 2))]
                segment = SimpleNamespace(start=0., end=duration, text="words", words=words)
                return iter([segment]), SimpleNamespace(language="el")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "audio.wav"
            ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000:duration=54", source)
            worker = main.TranscribeWorker(None, main.LOCAL_PROVIDER, "small", [], str(root),
                                          "el", False, FFMPEG, translate_result=False)
            fake = FakeModel()
            with patch.dict("sys.modules", {
                "ctranslate2": SimpleNamespace(StorageView=object, get_cuda_device_count=lambda: 0),
                "faster_whisper": SimpleNamespace(WhisperModel=lambda *a, **k: fake),
                "onnxruntime": SimpleNamespace(),
            }), patch.object(main.TranscribeWorker, "_shared_local_model", None), \
                 patch.object(main.TranscribeWorker, "_shared_local_model_name", None), \
                 patch.object(main.TranscribeWorker, "_shared_local_device", None), \
                 patch.object(main, "write_app_log"):
                _srt, _plain, raw = worker._local_whisper(source)
            self.assertTrue(any(not vad for _name, vad, _dur in calls), calls)
            self.assertTrue(all(dur <= 30.01 for _name, _vad, dur in calls), calls)
            words = raw["words"]
            self.assertGreater(words[-1]["end"], 53.5)
            self.assertLessEqual(words[-1]["end"], 54)
            self.assertEqual([w["start"] for w in words], sorted(set(w["start"] for w in words)))

    def test_context_is_owned_once_and_zero_timestamp_survives(self):
        windows = timing.recognition_windows(52.1)
        self.assertEqual([(w["start"], w["end"]) for w in windows], [(0, 26), (26, 52), (52, 52.1)])
        words = [dict(start=0., end=.2, text="zero"),
                 dict(start=25.9, end=26.1, text="boundary"),
                 dict(start=51.9, end=52.2, text="tail")]
        segments = [dict(start=0, end=52.2, text="zero boundary tail", words=words)]
        joined = [s for win in windows for s in timing.owned_asr_segments(segments, win["start"], win["end"], 52.1)]
        actual = [w for s in joined for w in s["words"]]
        self.assertEqual([w["text"] for w in actual], ["zero", "boundary", "tail"])
        self.assertEqual(actual[0]["start"], 0.)
        self.assertEqual(actual[-1]["end"], 52.1)
        self.assertEqual(timing.owned_asr_segments(segments, 30, 40, 52.1), [])

    def test_short_missing_tail_triggers_retry_despite_long_segment(self):
        words = [dict(start=i * .5, end=i * .5 + .3, text="word") for i in range(300)]
        segments = [dict(start=0, end=159, words=words)]
        self.assertTrue(timing.asr_needs_retry(segments, 159))
        self.assertAlmostEqual(timing.asr_coverage(segments, 159)[1], 149.8)
        # No stretching or synthetic tail words in the ownership operation.
        clipped = timing.owned_asr_segments(segments, 0, 159, 159)
        self.assertEqual(clipped[0]["end"], 149.8)


class RenderTests(unittest.TestCase):
    def test_batch_export_uses_qt_and_preserves_output_clock(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(burn, "instance_temp_dir", return_value=Path(temp) / "qt"):
            root = Path(temp)
            source = root / "source.mp4"
            ffmpeg("-f", "lavfi", "-i", "color=black:s=108x192:r=10:d=181",
                   "-c:v", "libx264", source)
            phrase = d.events_to_srt([(0.5, 2., GREEK), (179., 180.5, "τέλος")])
            settings = {**STYLE, "word_timelines": {str(source.resolve()): phrase},
                        "timeline_overrides": {str(source.resolve()): phrase}}
            results, outcomes, logs, commands = [], [], [], []
            worker = d.CaptionWorker([source], [], root / "out", FFMPEG,
                                     lambda *a: self.fail("Unexpected ASR"), settings)
            worker._encoder_diag_done = True
            worker.result.connect(lambda path, *_args: results.append(Path(path)))
            worker.finished.connect(lambda ok, msg: outcomes.append((ok, msg)))
            worker.log.connect(logs.append)
            original_render = worker._run_render
            def capture(command, *args):
                commands.append(list(command))
                return original_render(command, *args)
            worker._run_render = capture
            worker._run_all_failfast()
            self.assertTrue(outcomes[-1][0], outcomes)
            self.assertTrue(any("Qt" in line for line in logs), logs)
            self.assertAlmostEqual(float(probe(results[0])["format"]["duration"]), 181, delta=.05)
            command = commands[0]
            self.assertGreater(command.index("-t"), max(i for i, arg in enumerate(command) if arg == "-i"))
            tail = ffmpeg("-ss", "180", "-i", results[0], "-frames:v", 1,
                         "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1").stdout
            self.assertGreater(np.frombuffer(tail, dtype=np.uint8).max(), 100)

    def test_live_and_export_same_pixels(self):
        phrase = d.events_to_srt([(0, 3, GREEK)])
        tokens = d.tokens_for(GREEK)
        words = d.events_to_srt([(i * .35, (i + 1) * .35, word) for i, word in enumerate(tokens)])
        for effect in ("double_outline", "word_color", "dual_box", "semantic_karaoke"):
            for spacing in (-12, 0, 18):
                settings = {**STYLE, "effect": effect, "word_spacing": spacing}
                class Harness:
                    _paint_live_caption = d.DynamicCaptionPage._paint_live_caption
                    _live_caption_style_cache = None
                    def _current_settings(self):
                        return settings
                    def _live_caption_data(self, t):
                        text, _, _, active = burn.caption_content_at(t, phrase, words, settings)
                        return text, active
                    def _resolve_karaoke_cut(self, t, *_args):
                        _, _, cut, active = burn.caption_content_at(t, phrase, words, settings)
                        return cut, active
                live = QImage(1080, 1920, QImage.Format.Format_ARGB32_Premultiplied)
                live.fill(Qt.GlobalColor.transparent)
                painter = QPainter(live)
                painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
                painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
                Harness()._paint_live_caption(painter, live, 1.1)
                painter.end()
                export = burn.paint_caption_overlay_image(settings, phrase, words, 1.1)
                self.assertTrue(np.array_equal(pixels(live), pixels(export)), (effect, spacing))

    def test_dense_long_overlay_keeps_engine_and_tail(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(burn, "instance_temp_dir", return_value=Path(temp)):
            # Both old density limits (360 states / 900 cues) are exceeded.
            phrase = d.events_to_srt([(i * .4 + .03, i * .4 + .33, "λέξη") for i in range(501)])
            mov = burn.bake_qt_caption_overlay_mov(FFMPEG, duration=201, settings=STYLE,
                    phrase_srt=phrase, word_srt=phrase, target_w=108, target_h=192)
            data = probe(mov)
            self.assertAlmostEqual(float(data["format"]["duration"]), 201, delta=.01)
            manifest = mov.with_name("list.txt").read_text()
            self.assertAlmostEqual(sum(float(line.split()[1]) for line in manifest.splitlines()
                                       if line.startswith("duration ")), 201, places=4)
            self.assertFalse(list(mov.parent.glob("c*.png")))
            tail = ffmpeg("-i", mov, "-vf", "select=between(t\\,200.029\\,200.031)",
                          "-frames:v", 1, "-f", "rawvideo", "-pix_fmt", "rgba", "pipe:1").stdout
            self.assertGreater(np.frombuffer(tail, dtype=np.uint8).reshape(-1, 4)[:, 3].sum(), 0)
            burn.cleanup_qt_caption_overlay(mov)
            self.assertFalse(mov.exists())

    def test_precise_preview_pixels_duration_and_no_silent_fallback(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(burn, "instance_temp_dir", return_value=Path(temp) / "qt"):
            root = Path(temp)
            source, out = root / "source.mp4", root / "preview.mp4"
            ffmpeg("-f", "lavfi", "-i", "color=black:s=270x480:r=30:d=5",
                   "-c:v", "libx264", source)
            phrase = d.events_to_srt([(.5, 2.5, GREEK)])
            settings = {**STYLE, "preview_max_seconds": 3}
            outcomes = []
            worker = d.PreviewWorker(FFMPEG, source, out, phrase, settings)
            worker.finished.connect(lambda ok, msg: outcomes.append((ok, msg)))
            worker.run()
            self.assertTrue(outcomes[-1][0], outcomes)
            self.assertAlmostEqual(float(probe(out)["format"]["duration"]), 3, delta=.05)
            raw = ffmpeg("-ss", "1.0", "-i", out, "-frames:v", 1,
                         "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1").stdout
            frame = np.frombuffer(raw, dtype=np.uint8).reshape(480, 270, 3)
            expected = pixels(burn.paint_caption_overlay_image(settings, phrase, "", 1, size=(270, 480)))
            mask_a = frame.max(axis=2) > 55
            mask_b = (expected[:, :, :3] * (expected[:, :, 3:4] / 255)).max(axis=2) > 55
            self.assertGreater((mask_a & mask_b).sum() / (mask_a | mask_b).sum(), .90)
            with patch.object(d, "bake_qt_caption_overlay_mov", side_effect=RuntimeError("test render failure")):
                failed = []
                worker = d.PreviewWorker(FFMPEG, source, root / "failed.mp4", phrase, settings)
                worker.finished.connect(lambda ok, msg: failed.append((ok, msg)))
                worker.run()
                self.assertFalse(failed[-1][0])
                self.assertFalse((root / "failed.mp4").exists())


class MetadataTests(unittest.TestCase):
    def test_source_bitrate_budget_for_all_encoders(self):
        for backend in ("cpu", "nvenc", "qsv", "mf", "amf"):
            with patch.object(meta, "resolve_encoder", return_value=backend):
                args, _ = meta._hq_video_encode_args(FFMPEG, 1080, 1920, 700_000)
                self.assertLessEqual(int(args[args.index("-maxrate") + 1]), 805_000)

    def test_real_watermark_reencode_size_and_copy_only_cleanup(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(meta, "resolve_encoder", return_value="cpu"):
            root = Path(temp)
            source, out, logo = root / "source.mp4", root / "marked.mp4", root / "logo.png"
            ffmpeg("-f", "lavfi", "-i", "testsrc2=s=360x640:r=25:d=8",
                   "-f", "lavfi", "-i", "sine=frequency=440:duration=8",
                   "-c:v", "libx264", "-crf", "34", "-preset", "medium", "-c:a", "aac",
                   "-metadata", "comment=test private metadata", source)
            im = Image.new("RGBA", (120, 60))
            ImageDraw.Draw(im).rectangle((5, 5, 110, 50), fill=(255, 120, 0, 200))
            im.save(logo)
            worker = meta.MetadataWorker([source], root, watermark=dict(enabled=True, path=str(logo),
                                         width_pct=18, opacity=90, position="右下"))
            worker._av_reencode(FFMPEG, source, out)
            self.assertLess(out.stat().st_size / source.stat().st_size, 1.5)
            self.assertAlmostEqual(float(probe(out)["format"]["duration"]), 8, delta=.15)
            self.assertNotIn("comment", probe(out)["format"].get("tags", {}))
            clean = root / "clean.mp4"
            worker._av_copy_clean(FFMPEG, source, clean)
            self.assertAlmostEqual(clean.stat().st_size / source.stat().st_size, 1, delta=.03)
            print(f"Watermark size ratio: {out.stat().st_size / source.stat().st_size:.3f}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
