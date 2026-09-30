"""Minimal ctranslate2 package init for frozen VideoToolkit builds.

PyInstaller --collect-binaries left this folder with only native libs, which
shadowed the archived package and produced: module has no attribute StorageView.
Loading _ext here restores Whisper/faster-whisper.
"""
from __future__ import annotations

import ctypes
import glob
import os
import sys

package_dir = os.path.dirname(os.path.abspath(__file__))

if sys.platform == "win32":
    try:
        os.add_dll_directory(package_dir)
    except (FileNotFoundError, OSError):
        pass
    for library in glob.glob(os.path.join(package_dir, "*.dll")):
        try:
            ctypes.CDLL(library)
        except OSError:
            pass

from ctranslate2._ext import (  # noqa: E402
    AsyncGenerationResult,
    AsyncScoringResult,
    AsyncTranslationResult,
    DataType,
    Device,
    Encoder,
    EncoderForwardOutput,
    ExecutionStats,
    GenerationResult,
    GenerationStepResult,
    Generator,
    MpiInfo,
    ScoringResult,
    StorageView,
    TranslationResult,
    Translator,
    contains_model,
    get_cuda_device_count,
    get_supported_compute_types,
    set_random_seed,
)

try:
    from ctranslate2.version import __version__
except Exception:
    __version__ = "bundled"
