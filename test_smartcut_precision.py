"""Real FFmpeg regressions: long GOPs, VFR, stream offsets and short tails."""
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from modules.precise_cut import cut_range, fixed_ranges, parse_lengths, probe_video, scene_ranges
from modules.smartcut_page import SmartCutWorker

ROOT = Path(__file__).resolve().parent
FFMPEG = str(ROOT / "tools/ffmpeg/bin/ffmpeg.exe") if os.name == "nt" else shutil.which("ffmpeg")
FFPROBE = str(Path(FFMPEG).with_name("ffprobe.exe" if os.name == "nt" else "ffprobe")) if FFMPEG else ""


def ffmpeg(*args):
    return subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", *map(str, args)],
                          check=True, capture_output=True).stdout


def frames(path):
    raw = ffmpeg("-i", path, "-map", "0:v:0", "-fps_mode", "passthrough",
                 "-f", "rawvideo", "-pix_fmt", "rgb24", "-")
    return np.frombuffer(raw, dtype=np.uint8).reshape(-1, 90, 160, 3)


def frame_times(path):
    data = json.loads(subprocess.check_output([
        FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_frames",
        "-show_entries", "frame=best_effort_timestamp_time", "-of", "json", str(path)]))
    return np.array([float(frame["best_effort_timestamp_time"]) for frame in data["frames"]])


class LengthValidationTests(unittest.TestCase):
    def test_nonfinite_and_nonpositive_lengths_are_rejected_before_export(self):
        for sequence, loop in [("nan", 1), ("inf", 1), ("-1", 1), ("0", 1), ("1", 0),
                               ("1", float("nan")), ("bad", 1)]:
            with self.subTest(sequence=sequence, loop=loop), self.assertRaises(ValueError):
                parse_lengths(sequence, loop)

    def test_short_tail_is_not_discarded(self):
        self.assertEqual(list(fixed_ranges(3.64, "1.2，1.2", 1.2))[-1][1], 3.64)
        self.assertEqual(len(list(fixed_ranges(3.64, "1.2,1.2", 1.2))), 4)


@unittest.skipUnless(FFMPEG and Path(FFMPEG).is_file(), "FFmpeg required")
class PreciseCutMediaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="smartcut_precision_")
        cls.root = Path(cls.temp.name)
        cls.source = cls.root / "long-gop.mp4"
        ffmpeg("-f", "lavfi", "-i", "color=red:s=160x90:r=25:d=1.2",
               "-f", "lavfi", "-i", "color=green:s=160x90:r=25:d=1.2",
               "-f", "lavfi", "-i", "color=blue:s=160x90:r=25:d=1.24",
               "-f", "lavfi", "-i",
               "aevalsrc=if(between(t\\,1.3\\,1.4)\\,sin(2*PI*440*t)\\,0):s=48000:d=5",
               "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]",
               "-map", "[v]", "-map", "3:a", "-ac", "2",
               "-c:v", "libx264", "-g", "250", "-keyint_min", "250", "-sc_threshold", "0",
               "-c:a", "aac", cls.source)
        cls.clock = probe_video(FFMPEG, cls.source)
        cls.original = frames(cls.source)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def assertFrameColors(self, actual, expected):
        self.assertEqual(len(actual), len(expected))
        error = np.abs(actual.astype(float) - expected.astype(float)).mean(axis=(1, 2, 3))
        self.assertLess(float(error.max()), 5.0, error.tolist())

    def test_video_duration_does_not_follow_longer_audio(self):
        self.assertAlmostEqual(self.clock.duration, 3.64, places=4)

    def test_fractional_fps_subframe_tail_does_not_create_empty_output(self):
        source, out = self.root / "fractional.mp4", self.root / "fractional"
        ffmpeg("-f", "lavfi", "-i", "testsrc2=s=160x90:r=30000/1001:d=1.001",
               "-c:v", "libx264", source)
        worker = SmartCutWorker([], str(out), "自定义时长序列", "1", 30, 27, FFMPEG)
        self.assertEqual(worker.fixed_split(str(source), out), 1)
        self.assertFrameColors(frames(out / "001.mp4"), frames(source))

    def test_non_frame_aligned_intervals_assign_each_frame_once(self):
        out = self.root / "arbitrary"
        worker = SmartCutWorker([], str(out), "自定义时长序列", "", .73, 27, FFMPEG)
        self.assertEqual(worker.fixed_split(str(self.source), out), 5)
        parts = [frames(path) for path in sorted(out.glob("*.mp4"))]
        self.assertFrameColors(np.concatenate(parts), self.original)

    def test_matroska_without_stream_duration_uses_video_packets(self):
        source, dest = self.root / "packet-clock.mkv", self.root / "packet-cut.mp4"
        ffmpeg("-i", self.source, "-c", "copy", source)
        clock = probe_video(FFMPEG, source)
        self.assertAlmostEqual(clock.duration, 3.64, places=3)
        cut_range(FFMPEG, source, dest, 1.2, 2.4, clock)
        self.assertFrameColors(frames(dest), self.original[30:60])

    def test_fixed_mode_long_gop_has_no_repeated_or_missing_frames_and_keeps_tail(self):
        out = self.root / "fixed"
        out.mkdir()
        worker = SmartCutWorker([], str(out), "自定义时长序列", "1.2,1.2", 1.2, 27, FFMPEG)
        count = worker.fixed_split(str(self.source), out, total_duration=500)
        self.assertEqual(count, 4)
        parts = [frames(path) for path in sorted(out.glob("*.mp4"))]
        self.assertEqual([len(part) for part in parts], [30, 30, 30, 1])
        self.assertFrameColors(np.concatenate(parts), self.original)
        audio = np.frombuffer(ffmpeg("-i", out / "002.mp4", "-map", "0:a:0", "-ac", "1",
                                    "-ar", "48000", "-f", "f32le", "-"), dtype="<f4")
        active = np.flatnonzero(np.abs(audio) > .1)
        self.assertAlmostEqual(active[0] / 48000, .1, delta=.025)
        streams = json.loads(subprocess.check_output([
            FFPROBE, "-v", "error", "-show_streams", "-of", "json", str(out / "002.mp4")]))
        audio_stream = next(s for s in streams["streams"] if s["codec_type"] == "audio")
        self.assertEqual(audio_stream["channels"], 2)

    def test_scene_mode_preserves_exact_scene_starts_with_long_gop(self):
        out = self.root / "scenes"
        out.mkdir()
        worker = SmartCutWorker([], str(out), "智能画面识别", "", 30, 27, FFMPEG)
        self.assertEqual(worker.smart_split(str(self.source), out), 3)
        parts = [frames(path) for path in sorted(out.glob("*.mp4"))]
        self.assertEqual([len(part) for part in parts], [30, 30, 31])
        self.assertFrameColors(np.concatenate(parts), self.original)

    def test_vfr_detection_and_export_use_presentation_timestamps(self):
        source = self.root / "vfr.mp4"
        ffmpeg("-i", self.source, "-vf", "select='if(lt(t,1.2),not(mod(n,2)),1)'",
               "-fps_mode", "vfr", "-an", "-c:v", "libx264", "-g", "250",
               "-sc_threshold", "0", source)
        clock = probe_video(FFMPEG, source)
        ranges = scene_ranges(source, clock, 27)
        self.assertEqual(len(ranges), 3, ranges)
        self.assertAlmostEqual(ranges[1][0], 1.2, places=4)
        self.assertAlmostEqual(ranges[2][0], 2.4, places=4)
        actual = []
        for number, (start, end) in enumerate(ranges):
            dest = self.root / f"vfr-{number}.mp4"
            cut_range(FFMPEG, source, dest, start, end, clock)
            actual.append(frames(dest))
        self.assertFrameColors(np.concatenate(actual), frames(source))

    def test_nonzero_source_timestamp_does_not_move_cut(self):
        source, dest = self.root / "offset.mp4", self.root / "offset-cut.mp4"
        ffmpeg("-i", self.source, "-c", "copy", "-output_ts_offset", "5", source)
        clock = probe_video(FFMPEG, source)
        self.assertAlmostEqual(clock.start, 5, places=3)
        self.assertAlmostEqual(scene_ranges(source, clock, 27)[1][0], 1.2, places=4)
        cut_range(FFMPEG, source, dest, 1.2, 2.4, clock)
        self.assertFrameColors(frames(dest), self.original[30:60])

    def test_irregular_frame_timestamps_are_not_rounded_to_average_fps(self):
        source, dest = self.root / "irregular.mp4", self.root / "irregular-cut.mp4"
        ffmpeg("-i", self.source, "-vf",
               "settb=1/1000,setpts='if(lt(N,30),N*45,1350+(N-30)*37)'",
               "-fps_mode", "vfr", "-enc_time_base:v", "1/1000", "-an",
               "-c:v", "libx264", "-g", "250", "-sc_threshold", "0", source)
        clock = probe_video(FFMPEG, source)
        ranges = scene_ranges(source, clock, 27)
        self.assertAlmostEqual(ranges[1][0], 1.35, places=4)
        start, end = ranges[1]
        cut_range(FFMPEG, source, dest, start, end, clock)
        original_times = frame_times(source)
        expected = original_times[(original_times >= start) & (original_times < end)] - start
        np.testing.assert_allclose(frame_times(dest), expected, atol=.001)

    def test_audio_stream_delay_is_preserved(self):
        source, dest = self.root / "delayed.mp4", self.root / "delayed-cut.mp4"
        ffmpeg("-i", self.source, "-itsoffset", "0.36", "-i", self.source,
               "-map", "0:v:0", "-map", "1:a:0", "-c", "copy", source)
        cut_range(FFMPEG, source, dest, 1.2, 2.4)
        audio = np.frombuffer(ffmpeg("-i", dest, "-map", "0:a:0", "-ac", "1",
                                    "-ar", "48000", "-f", "f32le", "-"), dtype="<f4")
        active = np.flatnonzero(np.abs(audio) > .1)
        self.assertAlmostEqual(active[0] / 48000, .46, delta=.025)

    def test_cancelled_export_does_not_replace_existing_file(self):
        dest = self.root / "keep.mp4"
        dest.write_bytes(b"existing result")
        with self.assertRaisesRegex(RuntimeError, "取消"):
            cut_range(FFMPEG, self.source, dest, 0, 1, self.clock, lambda: True)
        self.assertEqual(dest.read_bytes(), b"existing result")
        self.assertFalse(list(self.root.glob(".*.partial.mp4")))

    def test_cancellation_terminates_running_media_process(self):
        from modules.precise_cut import run_media
        started = time.monotonic()
        with self.assertRaisesRegex(RuntimeError, "取消"):
            run_media([FFMPEG, "-hide_banner", "-loglevel", "error", "-re",
                       "-f", "lavfi", "-i", "color=s=160x90:r=25:d=20",
                       "-f", "null", "-"], lambda: time.monotonic() - started > .3)
        self.assertLess(time.monotonic() - started, 3)

    def test_cancel_during_scene_detection_releases_source(self):
        checks = 0

        def cancelled():
            nonlocal checks
            checks += 1
            return checks > 6

        source = self.root / "cancel-scene.mp4"
        shutil.copyfile(self.source, source)
        with self.assertRaisesRegex(RuntimeError, "取消"):
            scene_ranges(source, self.clock, 27, cancelled)
        source.unlink()  # Windows rejects this if the decoder still owns the file.

    def test_repeated_input_names_receive_separate_directories(self):
        out = self.root / "duplicates"
        worker = SmartCutWorker([], str(out), "自定义时长序列", "", 30, 27, FFMPEG)
        with patch.object(worker, "fixed_split", return_value=1):
            first = worker.process_one(str(self.source))[1]
            second = worker.process_one(str(self.source))[1]
        self.assertNotEqual(first, second)

    def test_pipeline_uses_same_precise_cuts_and_can_resume_them(self):
        import app as toolkit
        out = self.root / "pipeline"
        out.mkdir()
        worker = toolkit.PipelineWorker(None, [str(self.source)], str(out), 27,
                                        "", "", "", FFMPEG, "", "", "", 1, 3)
        worker.checkpoint_path = out / "checkpoint.json"
        clips = worker._cut_sources(out)
        self.assertEqual(len(clips), 3)
        self.assertFrameColors(np.concatenate([frames(path) for path in clips]), self.original)
        with patch.object(toolkit, "cut_range", side_effect=AssertionError("unexpected recut")):
            self.assertEqual(worker._cut_sources(out), clips)


if __name__ == "__main__":
    unittest.main()
