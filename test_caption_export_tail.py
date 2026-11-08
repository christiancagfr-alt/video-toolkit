import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from modules.asr_timing import CaptionTimingError, reconcile_export_tail, validate_srt_timing
from test_reels_render_stability import APP, FFMPEG, STYLE, ffmpeg, d, probe


class ExportTailTests(unittest.TestCase):
    def test_export_feedback_keeps_tail_words_and_marks_product_clock(self):
        from types import SimpleNamespace
        words=d.events_to_srt([(1,2,"first"),(160,161.8,"tail")])
        phrases=d.events_to_srt([(1,2,"first")])
        source=d.events_to_srt([(6,7,"first"),(165,166.8,"tail")])
        key="video.mp4"; logs=[]
        page=SimpleNamespace(_timeline_key=lambda x:x,_caption_source_for_video=lambda x:x,
            timeline_words={key:source},timeline_overrides={key:source},timeline_edit_states={},
            caption_mode=SimpleNamespace(currentText=lambda:"speech"),
            _timeline_source=lambda:"other",_append_run_log=logs.append,_refresh_task_queue=lambda:None)
        page._capture_missing_caption_sources=lambda *a:d.DynamicCaptionPage._capture_missing_caption_sources(page,*a)
        for _ in range(2):
            d.DynamicCaptionPage._worker_timeline_ready(page,key,words,phrases)
        self.assertEqual(d.parse_srt(page.timeline_words[key])[-1],(160,161.8,"tail"))
        self.assertEqual(page.timeline_words_source[key],source)
        self.assertTrue(page.timeline_edit_states[key]["captions_timeline_aligned"])
        self.assertTrue(any("提前结束" in line for line in logs))

    def test_missing_source_is_recovered_using_previous_edit_clock(self):
        from types import SimpleNamespace
        key="video.mp4"
        words=d.events_to_srt([(1,2,"first"),(9,10,"last")])
        old={"captions_timeline_aligned":True,"tracks":{"video":[dict(
            start=0,end=15000,source_start=5000,source_end=20000)]}}
        page=SimpleNamespace(timeline_words={key:words},timeline_overrides={key:words})
        d.DynamicCaptionPage._capture_missing_caption_sources(page,key,old)
        self.assertEqual(d.parse_srt(page.timeline_words_source[key]),[(6,7,"first"),(14,15,"last")])
        new=[dict(start=0,end=10000,source_start=10000,source_end=20000)]
        mapped=d.retime_srt_for_video_segments(page.timeline_words_source[key],new)
        self.assertEqual(d.parse_srt(mapped),[(4,5,"last")])

    def test_midstream_color_change_does_not_reset_cut_graph(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for i,matrix in enumerate(("bt709","smpte170m","bt709")):
                ffmpeg("-f","lavfi","-i","testsrc2=s=108x192:r=24:d=2",
                       "-f","lavfi","-i","sine=frequency=440:duration=2",
                       "-c:v","libx264","-colorspace",matrix,"-color_primaries",matrix,
                       "-color_trc",matrix,"-c:a","aac",root/f"{i}.mp4")
            listing=root/"concat.txt"
            listing.write_text("\n".join(f"file '{i}.mp4'" for i in range(3)),encoding="utf-8")
            source=root/"changing.mp4"
            ffmpeg("-f","concat","-safe","0","-i",listing,"-c","copy",source)
            state={"tracks":{"video":[dict(start=i*1500,end=(i+1)*1500,
                source_start=i*2000+100,source_end=i*2000+1600) for i in range(3)]}}
            output=d.render_timeline_edits(FFMPEG,source,state,root)
            video,audio=d.media_stream_durations(FFMPEG,output)
            self.assertAlmostEqual(video,4.5,delta=.15)
            self.assertAlmostEqual(audio,4.5,delta=.15)

    def test_readable_but_truncated_edit_cache_is_rebuilt(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); source=root/"source.mp4"
            ffmpeg("-f","lavfi","-i","color=s=108x192:r=25:d=5","-c:v","libx264",source)
            state={"tracks":{"video":[dict(start=0,end=3000,source_start=1000,source_end=4000)]}}
            output=d.render_timeline_edits(FFMPEG,source,state,root)
            ffmpeg("-f","lavfi","-i","color=s=108x192:r=25:d=0.5","-c:v","libx264",output)
            self.assertGreater(output.stat().st_size,1024)
            rebuilt=d.render_timeline_edits(FFMPEG,source,state,root)
            self.assertEqual(rebuilt,output)
            self.assertAlmostEqual(d.caption_media_duration(FFMPEG,output),3,delta=.05)

    def test_group_preferences_remember_choices_without_disabling_transcription(self):
        # Full UI initialization loads application fonts; isolate from the
        # pixel-comparison tests' font caches and from the user's QSettings.
        import os,sys,subprocess
        if not os.environ.get("VT_GROUP_PREF_TEST_CHILD"):
            result=subprocess.run([sys.executable,"-m","unittest",
                "test_caption_export_tail.ExportTailTests.test_group_preferences_remember_choices_without_disabling_transcription"],
                env={**os.environ,"VT_GROUP_PREF_TEST_CHILD":"1"},capture_output=True,timeout=60)
            self.assertEqual(result.returncode,0,result.stderr.decode("utf-8","replace"))
            return
        from PySide6.QtCore import QSettings
        with tempfile.TemporaryDirectory() as directory:
            store=QSettings(str(Path(directory)/"preferences.ini"),QSettings.Format.IniFormat)
            with patch.object(d,"QSettings",return_value=store):
                def page():
                    return d.DynamicCaptionPage(None,None,lambda:FFMPEG,["local"],"local")
                first=page()
                self.assertIn("快速",first.group_trim_mode.currentText())
                self.assertFalse(first.group_skip_transcript.isChecked())
                first.group_trim_mode.setCurrentText("仅按文案边界")
                first.group_skip_transcript.setChecked(True)
                second=page()
                self.assertEqual(second.group_trim_mode.currentText(),"仅按文案边界")
                self.assertTrue(second.group_skip_transcript.isChecked())
                second.group_auto_timeline.setChecked(True)
                second.group_trim_mode.setCurrentText("快速声音边界")
                third=page()
                self.assertFalse(third.group_skip_transcript.isChecked())
                self.assertTrue(third.group_auto_timeline.isChecked())
                for widget in (first,second,third):
                    widget.close()
                    widget.canva_timeline.scroll.viewport().removeEventFilter(widget.canva_timeline)
                    import shiboken6
                    shiboken6.delete(widget)

    def test_workspace_uses_picture_duration_when_audio_is_longer(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory)/"long_audio.mp4"
            ffmpeg("-f","lavfi","-i","color=s=108x192:r=25:d=2",
                   "-f","lavfi","-i","sine=frequency=440:duration=5",
                   "-c:v","libx264","-c:a","aac",source)
            page=SimpleNamespace(_timeline_key=lambda p:str(Path(p).resolve()),
                                 find_ffmpeg=lambda:FFMPEG,_media_duration_cache={})
            duration=d.DynamicCaptionPage._resolve_timeline_duration_ms(page,str(source))
            self.assertEqual(duration,2000)

    def test_zero_start_preserves_variable_frame_times_even_with_fps_hint(self):
        from modules import video_encoding as encoding
        import subprocess,json
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory)/"variable.mp4"
            ffmpeg("-f","lavfi","-i","testsrc2=s=108x192:r=10:d=4",
                   "-f","lavfi","-i","sine=frequency=440:duration=4",
                   "-vf","select='if(lt(t,2),1,not(mod(n,2)))'",
                   "-fps_mode","vfr","-c:v","libx264","-c:a","aac",source)
            def pts():
                raw=subprocess.check_output([str(Path(FFMPEG).with_name("ffprobe.exe")),
                    "-v","error","-select_streams","v:0","-show_packets",
                    "-show_entries","packet=pts_time","-of","json",str(source)])
                values=sorted(float(p["pts_time"]) for p in json.loads(raw)["packets"])
                return [round(t-values[0],4) for t in values]
            before=pts()
            durations_before=d.media_stream_durations(FFMPEG,source)
            with patch.object(encoding,"_probe_stream_start_times",return_value=(.1,0)):
                self.assertTrue(encoding.remux_zero_start(FFMPEG,source,fps=30))
            self.assertEqual(pts(),before)
            video,audio=d.media_stream_durations(FFMPEG,source)
            self.assertAlmostEqual(video,durations_before[0],delta=.001)
            self.assertAlmostEqual(audio,durations_before[1],delta=.001)

    def test_sixteen_cuts_preserve_workspace_gaps_with_and_without_aac(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for audio in (False,True):
                source=root/f"source_{audio}.mp4"
                args=["-f","lavfi","-i","testsrc2=s=108x192:r=25:d=22"]
                if audio:
                    args += ["-f","lavfi","-i","sine=frequency=440:sample_rate=48000:duration=22","-c:a","aac"]
                ffmpeg(*args,"-c:v","libx264",source)
                for gap in (0,50):
                    segments=[dict(start=i*(1000+gap),end=i*(1000+gap)+1000,
                                   source_start=i*1300+13,source_end=i*1300+1013)
                              for i in range(16)]
                    state={"tracks":{"video":segments}}
                    output=d.render_timeline_edits(FFMPEG,source,state,root)
                    picture,sound=d.media_stream_durations(FFMPEG,output)
                    expected=segments[-1]["end"]/1000
                    self.assertAlmostEqual(picture,expected,delta=.041)
                    if audio:
                        self.assertAlmostEqual(sound,expected,delta=.041)

    def test_new_reported_bounds_and_known_workspace_clock(self):
        for duration,start,end in [(30.629,30.783,31.203),(7.049,7.508,7.733),
                                   (15.721,15.896,16.165)]:
            srt=d.events_to_srt([(1,2,"keep"),(start,end,"tail")])
            state={"tracks":{"video":[{"start":0,"end":round(end*1000)}]}}
            with patch.object(d,"caption_media_duration",return_value=duration):
                fixed=d.validate_export_srt(srt,"baked.mp4",FFMPEG,aligned=True,state=state)
                self.assertEqual(d.parse_srt(fixed),[(1,2,"keep")])
        # Short-video excess is only accepted with evidence of the edited clock.
        srt=d.events_to_srt([(1,2,"keep"),(7.508,7.733,"tail")])
        with patch.object(d,"caption_media_duration",return_value=7.049):
            for aligned,state in [(False,state),(True,{}),(True,{"tracks":{"video":[{"end":20000}]}})]:
                with self.assertRaises(CaptionTimingError):
                    d.validate_export_srt(srt,"baked.mp4",FFMPEG,aligned=aligned,state=state)

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

            # Legacy workspace extends beyond actual source EOF: the bake is
            # shorter, but earlier corrected captions must retain their clock.
            srt=d.events_to_srt([(1,2,"keep"),(6.4,6.65,"tail")])
            state=dict(captions_timeline_aligned=True,tracks={"video":[
                dict(start=0,end=6700,source_start=0,source_end=6700)]})
            worker=d.CaptionWorker([source],[],root/"workspace",FFMPEG,None,
                dict(STYLE,word_timelines={key:srt},timeline_overrides={key:srt},
                     timeline_edits={key:state}))
            worker._encoder_diag_done=True
            outcomes=[]; timelines=[]; logs=[]
            worker.finished.connect(lambda *args:outcomes.append(args))
            worker.timeline_ready.connect(lambda _,words,phrases:timelines.append((words,phrases)))
            worker.log.connect(logs.append)
            worker._run_all_failfast()
            self.assertTrue(outcomes[-1][0],outcomes)
            self.assertEqual(d.parse_srt(timelines[-1][0]),[(1,2,"keep")])
            self.assertTrue(any("工作区字幕边界对齐" in line for line in logs))


if __name__ == "__main__":
    unittest.main()
