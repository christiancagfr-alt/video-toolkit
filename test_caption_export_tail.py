import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from modules.asr_timing import CaptionTimingError, reconcile_export_tail, validate_srt_timing
from test_reels_render_stability import APP, FFMPEG, STYLE, ffmpeg, d, probe


class ExportTailTests(unittest.TestCase):
    def test_reported_bounds_preserve_earlier_cues(self):
        for duration, start, end in [(17.497,17.626,17.895), (27.399,27.438,28.160),
                                     (120,119.539,120.436), (127.067,127.020,127.390)]:
            with self.subTest(duration=duration):
                srt = d.events_to_srt([(1,2,"keep"),(start,end,"tail")])
                messages = []
                fixed = reconcile_export_tail(srt,duration,messages.append)
                cues = d.parse_srt(fixed)
                self.assertEqual(cues[0],(1,2,"keep"))
                if start >= duration:
                    self.assertEqual(len(cues),1)
                else:
                    self.assertEqual(cues[1],(start,duration,"tail"))
                self.assertTrue(messages)
                validate_srt_timing(fixed,duration)

    def test_large_excess_and_wholly_outside_timeline_still_fail(self):
        for cues,duration in [([(0,120,"bad")],2), ([(1,2,"keep"),(20,22,"bad")],20),
                               ([(17.626,17.895,"bad")],17.497),
                               ([(0,.2,"keep"),(1.1,1.4,"bad")],1),
                               ([(0,1,"keep"),(20.3,20.5,"bad")],20)]:
            with self.subTest(cues=cues),self.assertRaises(CaptionTimingError):
                reconcile_export_tail(d.events_to_srt(cues),duration)

    def test_recognition_validation_is_not_relaxed(self):
        with self.assertRaises(CaptionTimingError):
            validate_srt_timing(d.events_to_srt([(119.539,120.436,"tail")]),120)

    def test_normal_timeline_is_unchanged(self):
        srt=d.events_to_srt([(0,1,"a"),(3,4,"b")])
        self.assertEqual(reconcile_export_tail(srt,5),srt)

    def test_real_export_fixes_tail_and_uses_source_clock_for_sidecar(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            source=root/"source.mp4"
            ffmpeg("-f","lavfi","-i","color=s=108x192:r=10:d=6","-c:v","libx264",source)
            key=str(source.resolve())
            original=d.events_to_srt([(.2,.8,"before"),(2.2,3.8,"inside"),(4.2,5.8,"after")])
            cut=[dict(start=0,end=2000,source_start=2000,source_end=4000,media_type="video")]
            settings=dict(STYLE, timeline_edits={key:dict(captions_timeline_aligned=True,
                                                         tracks={"video":cut})},
                          timeline_overrides={key:d.events_to_srt([(.2,1.8,"corrected")])})
            worker=d.CaptionWorker([source],[],root/"cut",FFMPEG,None,settings)
            worker._encoder_diag_done=True
            outcomes=[]; timelines=[]; files=[]
            worker.finished.connect(lambda *args:outcomes.append(args))
            worker.timeline_ready.connect(lambda _,words,phrases:timelines.append((words,phrases)))
            worker.result.connect(lambda path,*_:files.append(path))
            with patch.object(d,"_load_word_sidecar",return_value=original):
                worker._run_all_failfast()
            self.assertTrue(outcomes[-1][0],outcomes)
            self.assertEqual(d.parse_srt(timelines[-1][0]),[(.2,1.8,"inside")])
            self.assertEqual(d.parse_srt(timelines[-1][1]),[(.2,1.8,"corrected")])
            self.assertAlmostEqual(float(probe(files[-1])["format"]["duration"]),2,delta=.1)

            # Entire worker path: cached word and manual sentence both cross EOF.
            srt=d.events_to_srt([(1,2,"keep"),(5.8,6.25,"tail")])
            worker=d.CaptionWorker([source],[],root/"tail",FFMPEG,None,
                                   dict(STYLE,word_timelines={key:srt},timeline_overrides={key:srt}))
            worker._encoder_diag_done=True
            outcomes=[]; timelines=[]; logs=[]
            worker.finished.connect(lambda *args:outcomes.append(args))
            worker.timeline_ready.connect(lambda _,words,phrases:timelines.append((words,phrases)))
            worker.log.connect(logs.append)
            worker._run_all_failfast()
            self.assertTrue(outcomes[-1][0],outcomes)
            self.assertEqual(d.parse_srt(timelines[-1][0])[-1],(5.8,6.,"tail"))
            self.assertTrue(any("片尾边界修正" in line for line in logs))


if __name__ == "__main__":
    unittest.main()
