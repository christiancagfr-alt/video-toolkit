"""ASR clock, recovery and cache regressions; synthetic media, no network/models."""
import os
import shutil
import tempfile
import unittest
import wave
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from test_reels_render_stability import APP, FFMPEG, STYLE, ffmpeg, probe
from modules import asr_timing as timing
from modules import dynamic_caption_page as d
import app as main


class ProviderPhraseGapTests(unittest.TestCase):
    def test_gladia_keeps_nested_word_times_and_subtitles(self):
        payload={"status":"done","result":{"transcription":{
            "full_transcript":"hello world", "utterances":[{"start":1,"end":5,"text":"hello world",
            "words":[{"start":1,"end":1.2,"word":"hello"},{"start":4.8,"end":5,"word":"world"}]}],
            "subtitles":[{"format":"srt","subtitles":"1\n00:00:01,000 --> 00:00:05,000\nhello world\n"}]}}}
        response=lambda data:SimpleNamespace(status_code=200,json=lambda:data)
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/"audio.wav";path.write_bytes(b"fake")
            worker=main.TranscribeWorker(None,"Gladia","default",[],td,"en",False,FFMPEG,translate_result=False)
            with patch.object(main.requests,"post",side_effect=[response({"audio_url":"https://api.gladia.io/audio"}),response({"id":"test"})]), \
                 patch.object(main.requests,"get",return_value=response(payload)):
                srt,_,raw=worker._gladia(path,"test-key")
            self.assertEqual([(w["start"],w["end"]) for w in raw["words"]],[(1,1.2),(4.8,5)])
            self.assertIn("00:00:05,000",srt)

    def test_sparse_words_do_not_bridge_ninety_second_gap(self):
        rows = main.words_to_segments([
            dict(start=260,end=260.4,text="hello"),
            dict(start=350,end=350.5,text="world")])
        self.assertEqual([(r["start"],r["end"]) for r in rows],[(260,260.4),(350,350.5)])

    def test_adjacent_words_preserve_original_clock(self):
        rows=main.words_to_segments([dict(start=1,end=1.3,text="hello"),
                                    dict(start=1.4,end=1.9,text="world.")])
        self.assertEqual(rows,[dict(start=1,end=1.9,text="hello world.")])


def segment(words):
    return dict(start=words[0]["start"], end=words[-1]["end"],
                text=" ".join(w["text"] for w in words), words=words)


def word(start, end, text="word"):
    return dict(start=start, end=end, text=text)


class TimingIntegrityTests(unittest.TestCase):
    def test_provider_token_count_difference_does_not_shift_later_phrase(self):
        words=d.events_to_srt([(0,.3,"ice"),(.3,.6,"cream"),(.7,1,"here"),
                              (5,5.3,"next"),(5.4,5.8,"sentence")])
        old=d.events_to_srt([(0,1,"ice-cream here"),(5,6,"next sentence")])
        corrected=d.events_to_srt([(0,1,"ice cream here"),(5,6,"next sentence")])
        rows=d.parse_srt(d.align_word_srt_to_phrase_srt(words,corrected,old))
        self.assertEqual(rows[-2:],[(5,5.3,"next"),(5.4,5.8,"sentence")])

    def test_corrected_overlapping_words_reserve_nonzero_millisecond_slots(self):
        words = d.events_to_srt([(255,256.225,"one"),(256.2,256.225,"two"),(256.21,256.225,"three")])
        phrases = d.events_to_srt([(255,256.225,"one two three")])
        result = d.align_word_srt_to_phrase_srt(words,phrases)
        timing.validate_srt_timing(result,257)
        rows = d.parse_srt(result)
        self.assertEqual([r[2] for r in rows],["one","two","three"])
        self.assertEqual(rows[-1][1],256.225)
        self.assertTrue(all(a[1]<=b[0] for a,b in zip(rows,rows[1:])))

    def test_phrase_too_short_for_individual_words_preserves_all_text(self):
        phrases=d.events_to_srt([(1,1.002,"one two three")])
        words=d.events_to_srt([(1,1.002,"one"),(1,1.002,"two"),(1,1.002,"three")])
        result=d.align_word_srt_to_phrase_srt(words,phrases)
        timing.validate_srt_timing(result,2)
        self.assertEqual(d.parse_srt(result),[(1,1.002,"one two three")])

    def test_zero_length_context_word_keeps_text_without_extending_clock(self):
        original = [segment([word(27.5, 27.96, "hello"), word(27.96, 27.96, "world")])]
        repaired = timing.validate_asr_segments(timing.coalesce_zero_length_words(original), 28)
        self.assertEqual(repaired[0]["words"], [word(27.5, 27.96, "hello world")])
        self.assertEqual(len(original[0]["words"]), 2)
        self.assertEqual(original[0]["words"][0]["text"], "hello")

    def test_zero_length_leading_word_keeps_text_on_touching_next_word(self):
        rows = [segment([word(0, 0, "hello"), word(0, .3, "world")])]
        repaired = timing.validate_asr_segments(timing.coalesce_zero_length_words(rows), 1)
        self.assertEqual(repaired[0]["words"], [word(0, .3, "hello world")])

    def test_zero_word_cannot_bridge_silence_or_hide_invalid_clock(self):
        for bad in (word(2, 2), word(2, 1), word(float("nan"), 2)):
            rows = [segment([word(0, .3), bad])]
            with self.assertRaises(timing.CaptionTimingError):
                timing.validate_asr_segments(timing.coalesce_zero_length_words(rows), 3)

    def test_middle_gap_with_valid_tail_is_recovered_without_replacing_anchors(self):
        original = [word(i * .5, i * .5 + .3, f"old{i}") for i in range(30)
                    if not 8 <= i < 14]
        retry = [word(i * .5, i * .5 + .3, f"retry{i}") for i in range(28)]
        self.assertTrue(timing.asr_needs_retry([segment(original)], 15))
        recovered = timing.merge_asr_recovery([segment(original)], [segment(retry)], 15)
        words = [w for s in recovered for w in s["words"]]
        self.assertEqual([w["start"] for w in words], [i * .5 for i in range(30)])
        self.assertEqual([w for w in words if w["text"].startswith("old")], original)
        self.assertEqual([w["text"] for w in words[8:14]], [f"retry{i}" for i in range(8, 14)])
        self.assertFalse(timing.asr_needs_retry(recovered, 15))

    def test_silence_is_not_filled_by_stretching_anchors(self):
        original = [segment([word(0, .4), word(9.5, 10)])]
        self.assertEqual(timing.merge_asr_recovery(original, [], 10), original)
        self.assertEqual(timing.asr_gaps(original, 10), [(.4, 9.5)])

    def test_invalid_word_clocks_rejected_before_offsets(self):
        for start, end in ((0, 300), (2, 3), (-1, 1), (1, 1), (2, 1), (float("nan"), 1), (0, float("inf"))):
            with self.subTest(start=start, end=end), self.assertRaises(timing.CaptionTimingError):
                timing.validate_asr_segments([segment([word(start, end)])], 2)
        clipped = timing.validate_asr_segments([segment([word(0, 2.03)])], 2)
        self.assertEqual(clipped[0]["words"][0]["end"], 2)

    def test_invalid_srt_and_plain_text_never_get_invented_duration(self):
        for stamp in ("00:00:00,000 --> 00:03:00,000", "00:00:00,000 --> 00:61:00,000",
                      "-00:00:01,000 --> 00:00:02,000", "00:00:01,000 --> 00:00:01,000"):
            with self.subTest(stamp=stamp), self.assertRaises(timing.CaptionTimingError):
                timing.validate_srt_timing(f"1\n{stamp}\nhello\n", 2)
        with self.assertRaises(timing.CaptionTimingError):
            main.clean_model_srt("This service returned text without timestamps.")

    def test_fast_word_cues_and_overlap_fix_do_not_grow_the_timeline(self):
        words = [word(i * .04, i * .04 + .04, f"w{i}") for i in range(100)]
        srt = main.segments_to_srt(words)
        parsed = d.parse_srt(srt)
        self.assertEqual(len(parsed), 100)
        self.assertAlmostEqual(parsed[-1][1], 4)
        self.assertAlmostEqual(parsed[0][1] - parsed[0][0], .04)
        overlap = d.events_to_srt([(i * .04, i * .04 + .2, f"w{i}") for i in range(100)])
        fixed, count = d.fix_srt_overlaps(overlap)
        actual = d.parse_srt(fixed)
        self.assertGreater(count, 0)
        self.assertEqual([e[0] for e in actual], [e[0] for e in d.parse_srt(overlap)])
        self.assertAlmostEqual(actual[-1][1], 4.16)
        self.assertTrue(all(a[1] <= b[0] for a, b in zip(actual, actual[1:])))

    def test_grouping_zero_start_respects_max_duration(self):
        srt = d.events_to_srt([(i * .5, i * .5 + .3, "hello") for i in range(20)])
        phrases = d.parse_srt(d.group_word_srt(srt, max_chars=1000, max_duration=2))
        self.assertEqual(phrases[0][0], 0)
        self.assertTrue(all(end - start <= 2 for start, end, _ in phrases))
        tiny = d.expand_phrase_srt_to_estimated_words(d.events_to_srt([(0, .04, "a b c")]))
        self.assertEqual(len(d.parse_srt(tiny)), 3)
        timing.validate_srt_timing(tiny, .04)

    def test_cut_and_speed_keep_short_words_on_the_actual_clock(self):
        words = d.events_to_srt([(1, 1.02, "fast"), (1.97, 2., "tail")])
        cut = [{"start": 0, "end": 500, "source_start": 1000, "source_end": 2000}]
        actual = d.parse_srt(d.retime_srt_for_video_segments(words, cut))
        self.assertEqual(actual, [(0, .01, "fast"), (.485, .5, "tail")])
        scaled = d.parse_srt(d.scale_srt_timestamps(words, .5))
        self.assertEqual(scaled, [(.5, .51, "fast"), (.985, 1., "tail")])


class MediaIntegrityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.short = cls.root / "short.mp4"
        ffmpeg("-f", "lavfi", "-i", "color=black:s=108x192:r=10:d=2",
               "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000:duration=6",
               "-c:v", "libx264", "-c:a", "aac", cls.short)
        cls.speech = cls.root / "speech.wav"
        ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000:duration=15", cls.speech)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def run_fake_local(self, source, responder):
        calls, logs = [], []
        class FakeModel:
            def transcribe(self, path, **kwargs):
                duration = len(path) / 16000
                calls.append((duration, kwargs["vad_filter"]))
                words = responder(duration, kwargs["vad_filter"])
                result = SimpleNamespace(start=0., end=duration, text=" ".join(w.text for w in words),
                                         words=[SimpleNamespace(start=w.start, end=w.end, word=w.text) for w in words])
                return iter([result]), SimpleNamespace(language="en")
        worker = main.TranscribeWorker(None, main.LOCAL_PROVIDER, "small", [], str(self.root),
                                      "en", False, FFMPEG, translate_result=False)
        worker.log.connect(logs.append)
        with ExitStack() as stack:
            stack.enter_context(patch.dict("sys.modules", {
                "ctranslate2": SimpleNamespace(StorageView=object, get_cuda_device_count=lambda: 0),
                "faster_whisper": SimpleNamespace(WhisperModel=lambda *a, **k: FakeModel()),
                "onnxruntime": SimpleNamespace(),
            }))
            for attr in ("_shared_local_model", "_shared_local_model_name", "_shared_local_device"):
                stack.enter_context(patch.object(main.TranscribeWorker, attr, None))
            stack.enter_context(patch.object(main, "write_app_log"))
            result = worker._local_whisper(source)
        return result, calls, logs

    def test_video_clock_limits_audio_extraction(self):
        self.assertGreater(float(probe(self.short)["format"]["duration"]), 5.9)
        self.assertAlmostEqual(d.caption_media_duration(FFMPEG, self.short), 2)
        def response(duration, vad):
            return [SimpleNamespace(**word(i * .5, i * .5 + .3)) for i in range(int(duration * 2))]
        result, calls, _ = self.run_fake_local(self.short, response)
        self.assertTrue(all(abs(duration - 2) < .01 for duration, _ in calls), calls)
        self.assertLessEqual(result[2]["words"][-1]["end"], 2)
        with self.assertRaises(timing.CaptionTimingError):
            d.validate_srt_for_media(d.events_to_srt([(0, 5, "too long")]), self.short, FFMPEG)

    def test_worker_merges_middle_retry_even_when_retry_tail_is_shorter(self):
        def response(duration, vad):
            return [SimpleNamespace(**word(i * .5, i * .5 + .3, f"{'old' if vad else 'new'}{i}"))
                    for i in range(30 if vad else 28) if not (vad and 8 <= i < 14)]
        result, calls, logs = self.run_fake_local(self.speech, response)
        words = result[2]["words"]
        self.assertEqual([w["start"] for w in words], [i * .5 for i in range(30)])
        self.assertEqual(words[-1]["text"], "old29")
        self.assertEqual(words[8]["text"], "new8")
        self.assertEqual([vad for _, vad in calls], [True, False])
        self.assertFalse(any("覆盖 100%" in line for line in logs))

    def test_invalid_short_result_retries_then_fails_explicitly(self):
        def response(duration, vad):
            return [SimpleNamespace(**word(0, 120))]
        with self.assertRaisesRegex(timing.CaptionTimingError, "越界"):
            self.run_fake_local(self.short, response)

    def test_provider_result_is_validated_before_success_signal(self):
        worker = main.TranscribeWorker(None, main.LOCAL_PROVIDER, "small", [], str(self.root),
                                      "en", False, FFMPEG, translate_result=False)
        signals = []
        worker.result_ready.connect(lambda *args: signals.append(args))
        with patch.object(worker, "_call_provider", return_value=(d.events_to_srt([(0, 120, "bad")]), "bad", {})):
            with self.assertRaises(timing.CaptionTimingError):
                worker._process_one(str(self.short))
        with patch.object(worker, "_call_provider", return_value=(
                d.events_to_srt([(0, 1, "valid phrase")]), "valid phrase", {"words": [word(0, 120)]})):
            with self.assertRaises(timing.CaptionTimingError):
                worker._process_one(str(self.short))
        self.assertEqual(signals, [])

    def test_groq_checks_chunk_clock_and_uses_picture_duration(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            worker = main.TranscribeWorker(None, "Groq", "whisper-large-v3", [], str(root),
                                          "en", False, FFMPEG, translate_result=False)
            self.assertIn("越界", worker._groq_payload_is_suspicious(
                {"text": "bad", "segments": [word(0, 120)]}, 2))
            self.assertIn("缺少", worker._groq_payload_is_suspicious({"text": "bad"}, 2))
            durations = []
            def transcribe(chunk, key, model):
                duration = d.caption_media_duration(FFMPEG, chunk)
                durations.append(duration)
                return {"text": "hello world", "words": [word(0, .4, "hello"), word(.8, 1.5, "world")]}
            with patch.object(worker, "_groq_transcribe_file", side_effect=transcribe):
                srt, _, raw = worker._groq(self.short, "test", root)
            self.assertEqual(durations, [2.])
            self.assertEqual(d.parse_srt(srt), [(0, .4, "hello"), (.8, 1.5, "world")])
            self.assertLessEqual(raw["words"][-1]["end"], 2)

    def test_real_export_manual_trim_and_fresh_post_cut_asr(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.mp4"
            ffmpeg("-f", "lavfi", "-i", "color=black:s=108x192:r=10:d=6",
                   "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000:duration=6",
                   "-c:v", "libx264", "-c:a", "aac", source)
            key = str(source.resolve())
            original = d.events_to_srt([(.2, .8, "before"), (2.2, 3.8, "inside"), (4.2, 5.8, "after")])
            for fresh in (False, True):
                settings = dict(STYLE)
                if fresh:
                    cut = [{"start": 0, "end": 2000, "source_start": 2000, "source_end": 4000,
                            "media_type": "video"}]
                    settings["timeline_edits"] = {key: {"tracks": {"video": cut, "original_audio": cut}}}
                else:
                    settings.update(word_timelines={key: original},
                                    timeline_overrides={key: "[2-4]\n" + original})
                transcriptions = []
                def transcribe(path):
                    self.assertTrue(fresh)
                    transcriptions.append(path)
                    self.assertAlmostEqual(d.caption_media_duration(FFMPEG, path), 2, delta=.05)
                    return "inside", "", d.events_to_srt([(.2, 1.8, "inside")])
                worker = d.CaptionWorker([source], [], root / str(fresh), FFMPEG, transcribe, settings)
                worker._encoder_diag_done = True
                results, outcomes, timelines = [], [], []
                worker.result.connect(lambda path, *_args: results.append(Path(path)))
                worker.finished.connect(lambda ok, msg: outcomes.append((ok, msg)))
                worker.timeline_ready.connect(lambda key, words, phrase: timelines.append(phrase))
                # Keep this case independent of sidecars created by the prior
                # iteration: fresh means recognize the edited audio itself.
                with patch.object(d, "_load_word_sidecar", return_value=""):
                    worker._run_all_failfast()
                self.assertTrue(outcomes[-1][0], outcomes)
                self.assertEqual(d.parse_srt(timelines[-1]), [(.2, 1.8, "inside")])
                self.assertAlmostEqual(float(probe(results[-1])["format"]["duration"]), 2, delta=.05)
                self.assertEqual(len(transcriptions), int(fresh))

    def test_sidecar_is_bound_to_media_and_text(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            media = root / "clip.mp4"
            shutil.copyfile(self.short, media)
            srt = d.events_to_srt([(0, .4, "hello"), (.6, 1.2, "there")])
            side = d._write_word_sidecar(media, srt)
            self.assertEqual(side.name, "clip.mp4.words.srt")
            self.assertEqual(d.parse_srt(d._load_word_sidecar(media)), d.parse_srt(srt))
            moved = root / "moved"
            moved.mkdir()
            for path in (media, side, side.with_suffix(".json")):
                shutil.copyfile(path, moved / path.name)
            self.assertEqual(d.parse_srt(d._load_word_sidecar(moved / media.name)), d.parse_srt(srt))
            side.write_text(srt.replace("hello", "modified"), encoding="utf-8-sig")
            self.assertEqual(d._load_word_sidecar(media), "")
            d._write_word_sidecar(media, srt)
            old = media.stat()
            content = bytearray(media.read_bytes())
            content[-20] ^= 1
            media.write_bytes(content)
            os.utime(media, ns=(old.st_atime_ns, old.st_mtime_ns))
            self.assertEqual(d._load_word_sidecar(media), "")

    def test_same_name_cannot_reuse_other_media_maps_or_rebind_stale_path(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            a, b = root / "a" / "clip.wav", root / "b" / "clip.wav"
            a.parent.mkdir(); b.parent.mkdir()
            shutil.copyfile(self.speech, a)
            shutil.copyfile(self.speech, b)
            mapping = {}
            d.DynamicCaptionPage._caption_map_put(None, mapping, str(a), "valid")
            self.assertEqual(d._lookup_settings_map(mapping, b), "valid")
            b.write_bytes(b.read_bytes() + b"different")
            mapping["name:clip.wav"] = "wrong legacy"
            self.assertEqual(d._lookup_settings_map(mapping, b), "")
            a.write_bytes(a.read_bytes() + b"replacement")
            class Harness:
                timeline_words = mapping
                _timeline_key = staticmethod(lambda path: str(Path(path).resolve()))
                _caption_map_get = d.DynamicCaptionPage._caption_map_get
                _caption_map_put = d.DynamicCaptionPage._caption_map_put
            d.DynamicCaptionPage._rebind_caption_maps_for_path(Harness(), str(a))
            self.assertEqual(d._lookup_settings_map(mapping, a), "")

    def test_legacy_and_out_of_range_cache_are_not_accepted(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            media = root / "clip.mp4"
            shutil.copyfile(self.short, media)
            bad = d.events_to_srt([(0, 60, "bad")])
            media.with_suffix(".words.srt").write_text(bad)
            self.assertEqual(d._load_word_sidecar(media), "")
            cache = d._timeline_cache_path(root, media)
            cache.parent.mkdir(parents=True)
            cache.write_text(bad)
            self.assertEqual(d._load_timeline_cache(root, media), "")
            with self.assertRaises(timing.CaptionTimingError):
                d._save_timeline_cache(root, media, bad)


if __name__ == "__main__":
    unittest.main()
