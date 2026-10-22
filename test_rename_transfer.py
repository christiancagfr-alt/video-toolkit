import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from modules.file_transfer import filesystem_path, transfer_file
from modules.rename_page import RenameTask, RenameWorker


class RenameTransferTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "源视频.mp4"
        self.source.write_bytes(b"video data" * 200000)
        self.target = self.root / "新标题.mp4"

    def tearDown(self):
        self.temp.cleanup()

    def test_copy_never_uses_copyfile2_and_preserves_content_and_timestamp(self):
        before = self.source.stat()
        with patch("shutil.copy2", side_effect=OSError("[WinError 3]")):
            transfer_file(self.source, self.target)
        self.assertEqual(self.source.read_bytes(), self.target.read_bytes())
        self.assertEqual(before.st_mtime_ns, self.target.stat().st_mtime_ns)

    def test_move_falls_back_when_native_volume_rename_fails(self):
        expected = self.source.read_bytes()
        with patch("modules.file_transfer.os.rename", side_effect=OSError("[WinError 3]")):
            transfer_file(self.source, self.target, move=True)
        self.assertFalse(self.source.exists())
        self.assertEqual(expected, self.target.read_bytes())

    def test_partial_copy_is_removed_and_move_source_retained(self):
        def interrupted(reader, writer, **kwargs):
            writer.write(reader.read(32))
            raise OSError("volume disconnected")
        with patch("modules.file_transfer.os.rename", side_effect=OSError("cross-volume")), \
                patch("modules.file_transfer.shutil.copyfileobj", side_effect=interrupted):
            with self.assertRaisesRegex(OSError, "disconnected"):
                transfer_file(self.source, self.target, move=True)
        self.assertTrue(self.source.is_file())
        self.assertFalse(self.target.exists())

    def test_existing_destination_is_not_overwritten(self):
        self.target.write_bytes(b"existing")
        with self.assertRaises(FileExistsError):
            transfer_file(self.source, self.target)
        self.assertEqual(self.target.read_bytes(), b"existing")

    def test_attribute_failure_is_logged_without_losing_copied_content(self):
        warnings = []
        with patch("modules.file_transfer.shutil.copystat", side_effect=OSError("unsupported attributes")):
            transfer_file(self.source, self.target, warn=warnings.append)
        self.assertEqual(self.source.read_bytes(), self.target.read_bytes())
        self.assertEqual(len(warnings), 1)

    @unittest.skipUnless(os.name == "nt", "Windows extended paths")
    def test_long_paths_use_extended_windows_path(self):
        folder = filesystem_path(self.root / ("深" * 120) / ("层" * 120))
        folder.mkdir(parents=True)
        target = folder / ("标题" * 110 + ".mp4")
        transfer_file(self.source, target)
        self.assertEqual(self.source.read_bytes(), target.read_bytes())

    def test_worker_reports_failed_operation_and_both_paths(self):
        source_dir = self.root / "source"; source_dir.mkdir()
        (source_dir / "input.mp4").write_bytes(b"video")
        task = RenameTask(source_dir, self.root, "out", "", "title", "", "", 1, 3, True)
        results = []
        worker = RenameWorker([task])
        worker.finished.connect(lambda ok, message: results.append((ok, message)))
        with patch("modules.rename_page.transfer_file", side_effect=OSError("[WinError 3]")):
            worker.run()
        self.assertFalse(results[-1][0])
        message = results[-1][1]
        for part in ("复制文件失败", "input.mp4", "001-title.mp4", "源路径", "目标路径"):
            self.assertIn(part, message)


if __name__ == "__main__":
    unittest.main()
