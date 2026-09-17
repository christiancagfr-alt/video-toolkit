"""Real FFmpeg split-screen tests; all generated files stay in a test directory."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import subprocess
import threading
from types import SimpleNamespace
from pathlib import Path
import cv2
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QFont
from modules.vertical_stack import make_pairs, probe_video, stack_command, StackWorker, VerticalStackDialog

ROOT = Path(__file__).resolve().parent
FFMPEG = str(ROOT / "tools/ffmpeg/bin/ffmpeg.exe")
OUT = ROOT / "_smoke_vertical_stack"

def run(command):
    result = subprocess.run(command, capture_output=True)
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")

def test_stack():
    OUT.mkdir(exist_ok=True)
    top, bottom = OUT / "top.mp4", OUT / "bottom.mp4"
    for path, color, size, duration, audio in ((top, "red", "320x180", "2", True), (bottom, "blue", "180x320", "1", False)):
        command = [FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i", f"color={color}:s={size}:r=30:d={duration}"]
        if audio:
            command += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}", "-c:a", "aac"]
        run(command + ["-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)])
    a, b = probe_video(FFMPEG, top), probe_video(FFMPEG, bottom)
    assert make_pairs(["a", "b"], ["c"]) == [("a", "c"), ("b", "c")]
    try:
        make_pairs(["a", "b"], ["c", "d", "e"])
        raise AssertionError("mismatched list accepted")
    except ValueError:
        pass
    for mode, fit, duration, expected_audio, expected_duration in (
        ("top", "cover", "top", True, 2.),
        ("bottom", "contain", "shortest", False, 1.),
        ("mix", "cover", "longest", True, 2.),
        ("mute", "cover", "top", False, 2.),
    ):
        target = OUT / f"result_{mode}.mp4"
        settings = dict(width=180, height=320, ratio=64, audio=mode, fit=fit, duration=duration)
        run(stack_command(FFMPEG, top, bottom, target, settings, a, b))
        info = probe_video(FFMPEG, target)
        assert info["audio"] == expected_audio and abs(info["duration"] - expected_duration) < .08, info
        capture = cv2.VideoCapture(str(target))
        capture.set(cv2.CAP_PROP_POS_MSEC, 700)
        ok, frame = capture.read(); capture.release()
        assert ok and frame.shape[:2] == (320, 180)
        # BGR: each panel must show the correct clip, not sequential playback.
        assert frame[102, 90, 2] > 200 and frame[260, 90, 0] > 200
        if mode == "top":
            assert frame[203, 90, 2] > 200 and frame[206, 90, 0] > 200, "split ratio incorrect"
            capture = cv2.VideoCapture(str(target)); capture.set(cv2.CAP_PROP_POS_MSEC, 1600)
            ok, late = capture.read(); capture.release()
            assert ok and late[260, 90, 0] > 200, "short bottom video did not loop"
    with_audio = OUT / "bottom_audio.mp4"
    run([FFMPEG, "-y", "-v", "error", "-i", str(bottom), "-f", "lavfi", "-i", "sine=frequency=880:duration=1",
         "-c:v", "copy", "-c:a", "aac", "-shortest", str(with_audio)])
    mixed = OUT / "mix_both.mp4"
    run(stack_command(FFMPEG, top, with_audio, mixed, dict(width=180, height=320, audio="mix"), a, probe_video(FFMPEG, with_audio)))
    assert probe_video(FFMPEG, mixed)["audio"]
    portrait = OUT / "portrait_markers.mp4"
    run([FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i", "color=green:s=120x360:r=30:d=1",
         "-vf", "drawbox=x=0:y=0:w=iw:h=100:color=red:t=fill,drawbox=x=0:y=260:w=iw:h=100:color=blue:t=fill",
         "-c:v", "libx264", str(portrait)])
    for position, channel in ((0, 2), (100, 0)):
        target = OUT / f"custom_{position}.mp4"
        settings = dict(width=180, height=320, fit="custom", bottom_x=50, bottom_y=position, duration="shortest")
        run(stack_command(FFMPEG, top, portrait, target, settings, a, probe_video(FFMPEG, portrait)))
        capture = cv2.VideoCapture(str(target)); ok, frame = capture.read(); capture.release()
        assert ok and frame[260, 90, channel] > 200, "custom vertical crop did not move"
    products = []
    worker = StackWorker(FFMPEG, [(str(top), str(bottom))], OUT / "batch", dict(width=180, height=320))
    worker.completed.connect(lambda files, error: products.append((files, error)))
    worker.run()
    assert products and len(products[0][0]) == 1 and not products[0][1], products
    assert not list((OUT / "batch").glob(".stack_*")), "temporary render directory leaked"
    cancelled = StackWorker(FFMPEG, [(str(top), str(bottom))], OUT / "cancel", {})
    cancelled.cancel(); cancelled.run()
    assert not list((OUT / "cancel").glob("*.mp4"))
    dialog = VerticalStackDialog(FFMPEG, str(OUT))
    assert dialog.ratio.value() == 64
    assert dialog.fit.currentData() == "contain"
    dialog.show(); QApplication.processEvents()
    dialog.fit.setCurrentIndex(dialog.fit.findData("custom"))
    assert dialog.crop_controls.isVisible()
    assert dialog.crop_positions["bottom_y"].value() == 0
    dialog.grab().save(str(OUT / "dialog.png"))
    received = []
    dialog.caption_sources_ready.connect(lambda paths: received.extend(paths))
    dialog.worker = SimpleNamespace(warnings=[], cancelled=threading.Event(), isRunning=lambda: False)
    dialog.done_job([str(mixed)], "", False, True)
    assert not received, "queue insertion must wait for worker to finish"
    dialog.job_finished()
    assert received == [str(mixed)]
    print("PASS: pairing, 64/36 split, crop/contain, duration, loop, audio modes, worker cleanup, cancellation, UI")
    print("PASS: independent custom crop / no-crop default / caption queue handoff after completion")

if __name__ == "__main__":
    app = QApplication([])
    app.setFont(QFont("Microsoft YaHei", 10))
    test_stack()
