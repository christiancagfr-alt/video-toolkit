import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication
from modules.rename_page import (RenamePage, RenameTask, RenameWorker, safe_filename,
                                 unique_destination, filename_component_limit, _filename_units)


class FilenameLimitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_default_uses_filesystem_limit_and_keeps_extension(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            name, changed = safe_filename("a" * 400 + ".mp4", root)
            self.assertTrue(changed)
            self.assertEqual(_filename_units(name), filename_component_limit(root))
            self.assertTrue(name.endswith(".mp4"))
            (root / name).write_bytes(b"valid filename")

    def test_unicode_stays_inside_filesystem_units(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for title in ["中文" * 200, "😀" * 200]:
                name, _ = safe_filename(title + ".mp4", root)
                self.assertLessEqual(_filename_units(name), filename_component_limit(root))
                (root / name).write_bytes(b"valid Unicode")

    def test_custom_limit_excludes_extension_and_sanitizes_reserved_name(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.assertEqual(safe_filename("一二三四五六.mp4", root, 4)[0], "一二三四.mp4")
            self.assertEqual(safe_filename("CONgratulations.mp4", root, 3)[0], "_ON.mp4")
            self.assertEqual(safe_filename("CON.title.mp4", root)[0], "_ON.title.mp4")

    def test_collisions_keep_limit_and_existing_data(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            first = root / "abcdefgh.mp4"
            first.write_bytes(b"keep")
            second = unique_destination(first, 8)
            self.assertEqual(second.name, "abcdef_1.mp4")
            second.write_bytes(b"second")
            self.assertEqual(unique_destination(first, 8).name, "abcdef_2.mp4")
            self.assertEqual(first.read_bytes(), b"keep")
            self.assertEqual(len(unique_destination(root / "abcdefgh.mp4", 1).stem), 1)

    def test_maximum_length_collision_preserves_number(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            name, _ = safe_filename("a" * 400 + ".mp4", root)
            first = root / name
            first.touch()
            second = unique_destination(first)
            self.assertTrue(second.stem.endswith("_1"))
            self.assertEqual(_filename_units(second.name), filename_component_limit(root))

    def test_preview_and_actual_rename_use_same_limit_and_collision_rule(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source"; source.mkdir()
            (source / "1.mp4").write_bytes(b"one")
            (source / "2.mp4").write_bytes(b"two")
            page = RenamePage()
            page.set_input_folder(str(source)); page.set_output_folder(str(root / "out"))
            page.direct_replace.setChecked(True)
            page.titles.setPlainText("identical first\nidentical second")
            self.assertFalse(page.name_limit_enabled.isChecked())
            page.name_limit.setValue(8); page.name_limit_enabled.setChecked(True)
            task = page.task_from_form()
            expected = [page.preview.item(i, 3).text().split(" (")[0] for i in range(2)]
            worker = RenameWorker([task]); results = []
            worker.finished.connect(lambda ok, message: results.append((ok, message)))
            worker.run()
            self.assertTrue(results[-1][0], results)
            self.assertEqual(sorted(p.name for p in task.output_folder().iterdir()), sorted(expected))
            self.assertTrue(all(len(Path(name).stem) <= 8 for name in expected))
            self.assertEqual(len(list(source.iterdir())), 2)
            page.close()


class ReelsFilenameIntegrationTests(unittest.TestCase):
    def test_real_export_uses_custom_limit_and_default_system_limit(self):
        from modules.dynamic_caption_page import CaptionWorker
        app = QApplication.instance() or QApplication([])
        root = Path(__file__).resolve().parent
        ffmpeg = (str(Path(os.environ.get("VIDEO_TOOLKIT_MEDIA_BIN", root / "tools/ffmpeg/bin")) / "ffmpeg.exe")
                  if os.name == "nt" else shutil.which("ffmpeg"))
        if not ffmpeg or not Path(ffmpeg).is_file():
            self.skipTest("FFmpeg required")
        with tempfile.TemporaryDirectory() as folder:
            work = Path(folder); source = work / "input_name_for_export.mp4"
            subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                            "-i", "color=red:s=160x90:r=30:d=0.3", "-c:v", "libx264", str(source)],
                           check=True, capture_output=True)
            for enabled, renamed in ((True, True), (False, True), (True, False), (False, False)):
                settings = {"rename_enabled": renamed, "rename_titles": ["a" * 350],
                            "rename_name_limit_enabled": enabled, "rename_name_limit": 12,
                            "rename_date_enabled": False, "rename_suffix_enabled": False,
                            "skip_captions": True,
                            "encoder_backend": "cpu", "encode_preset": "ultrafast", "aspect_ratio": "原始比例"}
                output = work / f"{enabled}_{renamed}"
                worker = CaptionWorker([source], [], output, ffmpeg, None, settings)
                results = []; files = []
                worker.finished.connect(lambda ok, message: results.append((ok, message)))
                worker.result.connect(lambda name, *_: files.append(Path(name)))
                with patch("modules.dynamic_caption_page.encoder_probe_detail", return_value=(False, "test")):
                    worker.run()
                self.assertTrue(results[-1][0], results)
                self.assertEqual(len(files), 1)
                self.assertTrue(files[0].is_file())
                if enabled:
                    self.assertEqual(len(files[0].stem), 12)
                elif renamed:
                    self.assertEqual(_filename_units(files[0].name), filename_component_limit(output))
                else:
                    self.assertEqual(files[0].stem, source.stem + "_动态文案")


if __name__ == "__main__":
    unittest.main()
