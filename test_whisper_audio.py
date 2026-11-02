import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import numpy as np
from modules.whisper_audio import load_whisper_audio
from test_smartcut_precision import FFMPEG, ffmpeg


class WhisperAudioTests(unittest.TestCase):
    def test_stereo_48k_becomes_finite_mono_16k_without_pyav_decoder(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "română 中文.wav"
            ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=1", "-ac", "2", path)
            with patch("faster_whisper.audio.decode_audio", side_effect=TypeError("metadata_errors")):
                samples = load_whisper_audio(FFMPEG, path)
            self.assertEqual(samples.shape, (16000,))
            self.assertEqual(samples.dtype, np.float32)
            self.assertTrue(np.isfinite(samples).all())
            self.assertGreater(float(np.abs(samples).max()), .05)

    def test_no_audio_gives_actionable_error(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "silent.mp4"
            ffmpeg("-f", "lavfi", "-i", "color=s=160x90:d=0.2", "-an", path)
            with self.assertRaisesRegex(RuntimeError, "没有.*音轨"):
                load_whisper_audio(FFMPEG, path)

    def test_cancel_stops_before_processing(self):
        with self.assertRaisesRegex(RuntimeError, "取消"):
            load_whisper_audio(FFMPEG, "missing.wav", lambda: True)

    def test_romanian_is_supported(self):
        from faster_whisper.tokenizer import _LANGUAGE_CODES
        self.assertIn("ro", _LANGUAGE_CODES)


if __name__ == "__main__":
    unittest.main()
