"""Updater download/launch regressions. Never launches a real installer."""
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import app as toolkit

URL = ("https://github.com/christiancagfr-alt/video-toolkit/"
       "releases/download/v1.7.77/VideoToolkit_Setup_v1.7.77.exe")


class InstallerLaunchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="update_install_")
        self.path = Path(self.temp.name) / "setup & 空格.exe"
        self.path.write_bytes(b"MZ" + b"\0" * 120000)
        self.window = SimpleNamespace(close=Mock(), _download_thread=None)

    def tearDown(self):
        self.temp.cleanup()

    def invoke(self, launcher, answer=None):
        if answer is None:
            answer = toolkit.QMessageBox.StandardButton.Yes
        with patch.object(toolkit.QMessageBox, "question", return_value=answer), \
             patch.object(toolkit.QMessageBox, "warning") as warning, \
             patch.object(toolkit.sys, "platform", "win32"), \
             patch.object(toolkit.os, "startfile", launcher, create=True), \
             patch.object(toolkit.QApplication, "quit") as quit_app:
            toolkit.MainWindow._on_download_finished(self.window, True, str(self.path), "")
        return warning, quit_app

    def test_installer_launches_then_exits_including_floating_windows(self):
        launcher = Mock()
        warning, quit_app = self.invoke(launcher)
        launcher.assert_called_once_with(str(self.path.resolve()))
        warning.assert_not_called()
        self.window.close.assert_called_once()
        quit_app.assert_called_once()

    def test_waits_for_download_thread_before_launch(self):
        thread = Mock()
        thread.isRunning.return_value = True
        thread.wait.return_value = True
        self.window._download_thread = thread
        launcher = Mock(side_effect=lambda _path: thread.wait.assert_called_once_with(5000))
        warning, _quit = self.invoke(launcher)
        thread.quit.assert_called_once()
        warning.assert_not_called()

    def test_download_thread_timeout_does_not_launch_or_close(self):
        thread = Mock()
        thread.isRunning.return_value = True
        thread.wait.return_value = False
        self.window._download_thread = thread
        launcher = Mock()
        warning, quit_app = self.invoke(launcher)
        launcher.assert_not_called()
        quit_app.assert_not_called()
        self.window.close.assert_not_called()
        warning.assert_called_once()

    def test_declined_install_does_not_launch_or_exit(self):
        launcher = Mock()
        warning, quit_app = self.invoke(launcher, toolkit.QMessageBox.StandardButton.No)
        launcher.assert_not_called()
        self.window.close.assert_not_called()
        quit_app.assert_not_called()
        warning.assert_not_called()

    def test_failed_launch_keeps_application_open_and_shows_path(self):
        warning, quit_app = self.invoke(Mock(side_effect=OSError("launch denied")))
        self.window.close.assert_not_called()
        quit_app.assert_not_called()
        self.assertIn(str(self.path), warning.call_args.args[2])
        self.assertIn("launch denied", warning.call_args.args[2])

    def test_corrupt_installer_is_not_launched(self):
        self.path.write_bytes(b"<html>download error</html>")
        launcher = Mock()
        warning, quit_app = self.invoke(launcher)
        launcher.assert_not_called()
        quit_app.assert_not_called()
        warning.assert_called_once()


class FakeResponse:
    url = URL

    def __init__(self, payload, content_length):
        self.payload = payload
        self.headers = {"content-length": str(content_length)}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size):
        for start in range(0, len(self.payload), chunk_size):
            yield self.payload[start:start + chunk_size]


class DownloadIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="update_download_")
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def download(self, payload, content_length, response=None):
        worker = toolkit.DownloadWorker(URL, "1.7.77", "VideoToolkit_Setup_v1.7.77.exe")
        results = []
        worker.finished.connect(lambda *args: results.append(args))
        with patch("tempfile.gettempdir", return_value=str(self.root)), \
             patch.object(toolkit.requests, "get",
                          return_value=response or FakeResponse(payload, content_length)):
            worker.run()
        self.assertEqual(len(results), 1)
        return results[0]

    def test_complete_download_is_published_atomically_with_unique_name(self):
        payload = b"MZ" + b"\0" * 120000
        first = self.download(payload, len(payload))
        second = self.download(payload, len(payload))
        self.assertTrue(first[0], first)
        self.assertTrue(second[0], second)
        self.assertNotEqual(first[1], second[1])
        self.assertEqual(Path(first[1]).read_bytes(), payload)
        self.assertFalse(list(self.root.glob("*.part")))

    def test_one_percent_truncation_no_longer_counts_as_success(self):
        payload = b"MZ" + b"\0" * 118798  # 99% of advertised size.
        result = self.download(payload, 120000)
        self.assertFalse(result[0], result)
        self.assertIn("不完整", result[2])
        self.assertFalse(list(self.root.iterdir()))

    def test_html_error_body_is_not_saved_as_installer(self):
        payload = b"<html>" + b"x" * 120000
        result = self.download(payload, len(payload))
        self.assertFalse(result[0], result)
        self.assertFalse(list(self.root.iterdir()))

    def test_interrupted_transfer_removes_partial_file(self):
        response = FakeResponse(b"", 120000)

        def broken_stream(chunk_size):
            yield b"MZ" + b"\0" * 110000
            raise OSError("connection lost")

        response.iter_content = broken_stream
        result = self.download(b"", 120000, response)
        self.assertFalse(result[0])
        self.assertIn("connection lost", result[2])
        self.assertFalse(list(self.root.iterdir()))

    def test_cancelled_download_never_exposes_executable(self):
        worker = toolkit.DownloadWorker(URL, "1.7.77", "Setup.exe")
        results = []
        worker.finished.connect(lambda *args: results.append(args))
        response = FakeResponse(b"", 220000)

        def cancelled_stream(chunk_size):
            yield b"MZ" + b"\0" * 110000
            worker.cancelled = True
            yield b"\0" * 110000

        response.iter_content = cancelled_stream
        with patch("tempfile.gettempdir", return_value=str(self.root)), \
             patch.object(toolkit.requests, "get", return_value=response):
            worker.run()
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0][0])
        self.assertIn("取消", results[0][2])
        self.assertFalse(list(self.root.iterdir()))


if __name__ == "__main__":
    unittest.main()
