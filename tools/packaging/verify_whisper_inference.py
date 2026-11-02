"""Download a public tiny test model and verify real CPU inference, without user media."""
import os
import sys
import tempfile
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from faster_whisper import WhisperModel
from modules.whisper_audio import load_whisper_audio


def verify(model_dir, ffmpeg):
    model = WhisperModel(model_dir, device="cpu", compute_type="int8", cpu_threads=2)
    with tempfile.TemporaryDirectory(prefix="whisper_inference_") as directory:
        path = Path(directory) / "romanian.wav"
        with wave.open(str(path), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(b"\0\0" * 16000)
        audio = load_whisper_audio(ffmpeg, path)
        for vad in (False, True):
            segments, info = model.transcribe(audio, language="ro", vad_filter=vad,
                                              word_timestamps=True, beam_size=1)
            list(segments)
            assert info.language == "ro"
            print(f"Whisper actual inference passed: Romanian, VAD={vad}", flush=True)


if __name__ == "__main__":
    import shutil
    from faster_whisper.utils import download_model
    model_dir = os.environ["VIDEO_TOOLKIT_WHISPER_TEST_MODEL"]
    download_model("tiny", output_dir=model_dir)
    verify(model_dir, shutil.which("ffmpeg"))
