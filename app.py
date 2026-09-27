from __future__ import annotations

import json
import hashlib
import asyncio
import base64
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import wave
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse


def _startup_trace(message):
    path = os.environ.get("VIDEO_TOOLKIT_STARTUP_TRACE", "")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(f"{time.strftime('%H:%M:%S')} {message}\n")
    except Exception:
        pass


_startup_trace("standard imports ready")
import requests
_startup_trace("requests ready")
from PySide6.QtCore import QEvent, QObject, QThread, QTimer, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView, QAbstractSpinBox, QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QFileDialog, QFormLayout, QFrame, QInputDialog,
    QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget, QMainWindow,
    QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QSlider, QSpinBox, QToolTip, QTextBrowser,
    QScrollArea, QSplitter, QStackedWidget, QTabWidget, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)
_startup_trace("PySide6 ready")

from modules.rename_page import RenamePage, RenameTask, natural_key as rename_natural_key
from modules.screenshot_page import VideoTool as ScreenshotPage
from modules.settings_page import SettingsPage, component_bin, hidden_kwargs
from modules.smartcut_page import SmartCutPage, video_duration
from modules.watermark_page import MainWindow as WatermarkPage
from modules.dynamic_caption_page import DynamicCaptionPage, group_word_srt, write_ass
from modules.tts_page import TtsPage
from modules.text_rules import (
    filter_asr_junk_srt,
    is_asr_junk_caption,
    normalize_required_capitalization,
    normalize_subtitle_text,
)
from modules.metadata_page import MetadataPage
from modules.platform_utils import (
    app_data_dir,
    bundled_media_tool,
    exclusive_file_lock,
    instance_id,
    media_tool_name,
    validate_media_tool,
)
from modules.path_picker import default_output_path
from modules.app_logging import app_log_path, read_app_log, write_app_log
from modules import elevenlabs_web_auth as el_web
from modules.help_content import FAQ_JUMP, HELP_CSS, HELP_FAQ_TAB_INDEX, HELP_TABS, SETTINGS_NAV
from modules.language_style import (
    fill_writing_language_combo, import_language_pack_file, reload_language_packs,
    user_language_packs_dir, writing_language_from_ui,
)
_startup_trace("tool modules ready")


APP_NAME = "视频工具合集"
APP_VERSION = os.environ.get("VIDEO_TOOLKIT_VERSION", "1.7.65").strip().lstrip("v") or "1.7.65"
APP_DISPLAY_NAME = f"{APP_NAME}  v{APP_VERSION}"
_SINGLE_INSTANCE_MUTEX = None
ALL_RESULTS_LABEL = "【全部结果】"
ASR_PROVIDERS = ["Groq", "Gemini", "ElevenLabs", "Gladia"]
PROVIDERS = ASR_PROVIDERS + ["Luma", "Kling"]
LOCAL_PROVIDER = "本地 Whisper（无需密钥）"
AUTO_PROVIDER = "自动选择（按优先级）"
TRANSCRIPTION_PROVIDERS = [AUTO_PROVIDER, LOCAL_PROVIDER] + ASR_PROVIDERS
DEFAULT_MODELS = {
    # 本地默认 medium：比 small 更懂语境/词形，比 large-v3 更快
    LOCAL_PROVIDER: "medium",
    # turbo 在部分希腊语/宗教口播上会幻觉成「Υπότιτλοι AUTHORWAVE」；large-v3 更稳
    "Groq": "whisper-large-v3",
    # gemini-2.0-flash 已于 2026-06-01 关停；字幕多模态用 3.6 Flash（稳定）
    "Gemini": "gemini-3.6-flash",
    "ElevenLabs": "scribe_v2",
    "Gladia": "default",
    "Luma": "default",
    "Kling": "default",
}
# 本地 Whisper 可选体积（faster-whisper 模型名）
LOCAL_WHISPER_MODEL_OPTIONS = [
    ("base", "base · 最快（口型可用，语义略弱）"),
    ("small", "small · 快 / 准确度中上（推荐提速）"),
    ("medium", "medium · 语义更稳 / 更慢"),
    ("large-v3", "large-v3 · 最准 / 很慢很吃显存"),
]
# 自动模式下的服务优先级：Groq 快 → Gemini → Gladia → 本地 → 其它
DEFAULT_PROVIDER_PRIORITY = [
    "Groq", "Gemini", "Gladia", LOCAL_PROVIDER, "ElevenLabs", "Luma", "Kling",
]
# 已安装用户配置里若仍是旧默认模型，启动时迁移到可用值
_MODEL_MIGRATIONS = {
    "Groq": {
        "whisper-large-v3-turbo": "whisper-large-v3",
        "whisper-large-v3-turbo-latest": "whisper-large-v3",
    },
    "Gemini": {
        # 1.x / 2.0 系列已关停 → 3.6 Flash
        "gemini-1.5-flash": "gemini-3.6-flash",
        "gemini-1.5-flash-latest": "gemini-3.6-flash",
        "gemini-1.5-pro": "gemini-3.6-flash",
        "gemini-2.0-flash": "gemini-3.6-flash",
        "gemini-2.0-flash-001": "gemini-3.6-flash",
        "gemini-2.0-flash-lite": "gemini-3.1-flash-lite",
        "gemini-2.0-flash-lite-001": "gemini-3.1-flash-lite",
        "gemini-2.5-flash": "gemini-3.6-flash",
        "gemini-2.5-flash-lite": "gemini-3.1-flash-lite",
        "gemini-3.5-flash": "gemini-3.6-flash",  # 旧默认别名，升到现行稳定 Flash
    },
    "ElevenLabs": {
        "scribe_v1": "scribe_v2",
        "scribe_v1_experimental": "scribe_v2",
    },
}
DEFAULT_SHEET_MAPPINGS = [
    {"field": "日期", "column": "A", "source": "date", "value": ""},
    {"field": "文件名/链接", "column": "B", "source": "file", "value": ""},
    {"field": "中文字幕", "column": "K", "source": "chinese", "value": ""},
    {"field": "原文/葡语", "column": "L", "source": "original", "value": ""},
    {"field": "云端文件夹", "column": "W", "source": "folder", "value": ""},
]
DEFAULT_VARIABLE_FIELDS = [
    {"field": "组别", "column": "C", "options": [], "selected": ""},
    {"field": "分类", "column": "D", "options": [], "selected": ""},
    {"field": "难易程度", "column": "E", "options": [], "selected": ""},
    {"field": "素材来源", "column": "F", "options": [], "selected": ""},
    {"field": "使用软件", "column": "G", "options": [], "selected": ""},
    {"field": "制作人1", "column": "H", "options": [], "selected": ""},
    {"field": "字幕审核", "column": "I", "options": [], "selected": ""},
    {"field": "版权审核", "column": "J", "options": [], "selected": ""},
]


def app_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def resource_path(name: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", app_root()))
    return base / name


def config_dir() -> Path:
    return app_data_dir()


def atomic_write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def read_json_file(path: Path, default=None):
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        return loaded
    except (OSError, ValueError, TypeError):
        return {} if default is None else default


def source_signature(source: str):
    if is_supported_video_url(source):
        return {"kind": "url", "value": source.strip()}
    path = Path(source).expanduser()
    try:
        resolved = path.resolve()
        stat = resolved.stat()
        return {"kind": "file", "path": str(resolved), "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns}
    except OSError:
        return {"kind": "file", "path": str(path.absolute()), "missing": True}


def stable_key(payload) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class ConfigStore:
    def __init__(self):
        self.path = config_dir() / "config.json"
        self.lock = threading.RLock()
        self.data = self._load()

    def _default(self):
        return {
            "providers": {p: [] for p in PROVIDERS},
            "round_robin": {p: 0 for p in PROVIDERS},
            "models": dict(DEFAULT_MODELS),
            "provider_priority": list(DEFAULT_PROVIDER_PRIORITY),
            "google_sync": {
                "enabled": False, "json_path": "", "parent_folder": "",
                "folder_mode": "视频名称", "custom_folder_name": "", "public_link": False,
                "write_sheet": False, "spreadsheet_id": "", "sheet_name": "",
                "available_sheet_names": [], "option_sheet_name": "", "option_start_row": 2,
                "insert_row": 4, "date_column": "A", "file_column": "B",
                "chinese_column": "K", "original_column": "L", "folder_column": "W",
                "static_columns": "C=\nD=\nE=\nF=\nG=\nH=\nI=\nJ=",
                "sheet_mappings": [dict(item) for item in DEFAULT_SHEET_MAPPINGS],
                "sheet_profiles": {}, "active_sheet_profile": "",
                "sync_profiles": {}, "active_sync_profile": "",
                "auth_ok": False, "auth_identity": "", "auth_checked": "",
                "variable_fields": [dict(item) for item in DEFAULT_VARIABLE_FIELDS],
                "mapping_ui_version": 3,
            },
        }

    def _load(self):
        default = self._default()
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            # API keys live in encrypted secrets.vault, never in config.json clear-text.
            try:
                from modules.secret_crypto import restore_keys_from_vault
                loaded = restore_keys_from_vault(loaded)
            except Exception:
                pass
            for key in default:
                if key not in loaded:
                    loaded[key] = default[key]
            for provider in PROVIDERS:
                loaded["providers"].setdefault(provider, [])
                loaded["round_robin"].setdefault(provider, 0)
                loaded["models"].setdefault(provider, DEFAULT_MODELS[provider])
            loaded["models"].setdefault(LOCAL_PROVIDER, DEFAULT_MODELS[LOCAL_PROVIDER])
            # 迁移已知会出错/幻觉的旧模型名
            for provider, mapping in _MODEL_MIGRATIONS.items():
                current = str(loaded["models"].get(provider, "") or "")
                if current in mapping:
                    loaded["models"][provider] = mapping[current]
            # 识别优先级：Groq → Gemini → Gladia → 本地 → 其它（可在「调整顺序」里改）
            preferred = list(DEFAULT_PROVIDER_PRIORITY)
            old_pri = [p for p in (loaded.get("provider_priority") or []) if p in preferred]
            # 旧默认序统一迁到新推荐序（保留用户在「调整顺序」里手动改过的非旧默认）
            legacy_prefixes = {
                tuple([LOCAL_PROVIDER, "Gladia", "Groq", "ElevenLabs"]),
                tuple(["Gemini", "Gladia", LOCAL_PROVIDER, "Groq"]),
                tuple(["Gemini", "Gladia", "Groq", LOCAL_PROVIDER]),
                tuple(["Gladia", "Gemini", LOCAL_PROVIDER, "Groq"]),
            }
            old_head4 = tuple(old_pri[:4]) if len(old_pri) >= 4 else tuple(old_pri)
            force_new = (
                not old_pri
                or old_head4 in legacy_prefixes
                or (old_pri and old_pri[0] == LOCAL_PROVIDER)
                or (old_pri and old_pri[0] == "Gemini" and "Groq" in old_pri
                    and old_pri.index("Groq") >= 2)
            )
            if force_new:
                loaded["provider_priority"] = list(preferred)
            else:
                pri = list(old_pri)
                for p in preferred:
                    if p not in pri:
                        pri.append(p)
                loaded["provider_priority"] = pri
            # 本地模型若还是默认 small，升到 medium（用户已选手动 medium/large 则保留）
            if str(loaded["models"].get(LOCAL_PROVIDER, "")).strip() in ("", "small"):
                # 仅当从未显式改过：若用户故意 small，可在界面再改回
                # 用配置标记避免反复强制
                if not loaded.get("_local_model_user_set"):
                    loaded["models"][LOCAL_PROVIDER] = DEFAULT_MODELS[LOCAL_PROVIDER]
            old_google = loaded.get("google_sync", {})
            had_mappings = bool(old_google.get("sheet_mappings"))
            old_mapping_version = int(old_google.get("mapping_ui_version", 1))
            loaded["google_sync"] = {**default["google_sync"], **old_google}
            if not had_mappings:
                legacy_values = {}
                for line in old_google.get("static_columns", "").splitlines():
                    if "=" in line:
                        column, value = line.split("=", 1); legacy_values[column.strip().upper()] = value.strip()
                mappings = [dict(item) for item in DEFAULT_SHEET_MAPPINGS]
                legacy_columns = {"date": "date_column", "file": "file_column", "chinese": "chinese_column",
                                  "original": "original_column", "folder": "folder_column"}
                for item in mappings:
                    if item["source"] in legacy_columns:
                        item["column"] = old_google.get(legacy_columns[item["source"]], item["column"])
                    elif item["source"] == "static":
                        item["value"] = legacy_values.get(item["column"], "")
                loaded["google_sync"]["sheet_mappings"] = mappings
            if old_mapping_version < 2:
                variable_columns = {item["column"] for item in DEFAULT_VARIABLE_FIELDS}
                old_mappings = loaded["google_sync"].get("sheet_mappings", [])
                loaded["google_sync"]["sheet_mappings"] = [item for item in old_mappings
                    if not (item.get("source") == "static" and item.get("column") in variable_columns
                            and not str(item.get("value", "")).strip())]
                loaded["google_sync"]["variable_fields"] = [dict(item) for item in DEFAULT_VARIABLE_FIELDS]
                loaded["google_sync"]["mapping_ui_version"] = 2
            priority = loaded.get("provider_priority", [])
            allowed = PROVIDERS + [LOCAL_PROVIDER]
            loaded["provider_priority"] = [p for p in priority if p in allowed]
            loaded["provider_priority"] += [p for p in allowed if p not in loaded["provider_priority"]]
            return loaded
        except Exception:
            return default

    def save(self):
        # Cross-process lock so multi-open instances do not corrupt config.json.
        # CodeQL CWE-312: API keys never enter the JSON document written below.
        # They are sealed into secrets.vault (Fernet ciphertext) separately.
        with self.lock, exclusive_file_lock(self.path.with_suffix(".lock"), timeout=12.0):
            from modules.secret_crypto import extract_and_strip_keys, save_vault
            safe_payload, vault = extract_and_strip_keys(self.data)
            save_vault(vault)  # ciphertext only; may be empty when no keys
            # Serialize only the secret-free payload (keys are literal "").
            document = json.dumps(safe_payload, ensure_ascii=False, indent=2)
            temp = self.path.with_suffix(".tmp")
            temp.write_text(document, encoding="utf-8")
            temp.replace(self.path)


    def add_key(self, provider: str, key: str, **meta):
        # 网页会话 JSON 不能走 normalize（会破坏内容）；API Key 仍规范化
        if provider == "ElevenLabs" and el_web.is_web_secret(key):
            raw_key = str(key).strip()
        else:
            raw_key = normalize_api_key(key)
        if not raw_key:
            raise ValueError("密钥不能为空")
        with self.lock:
            if any(item["key"] == raw_key for item in self.data["providers"][provider]):
                raise ValueError("该密钥已存在")
            row = {
                "id": uuid.uuid4().hex,
                "key": raw_key,
                "enabled": True,
                "status": "未检测",
                "last_checked": "",
                "last_error": "",
                "uses": 0,
            }
            if meta.get("auth_kind"):
                row["auth_kind"] = str(meta["auth_kind"])
            if meta.get("label"):
                row["label"] = str(meta["label"])
            self.data["providers"][provider].append(row)
            self.save()

    def update_key(self, provider: str, key_id: str, **changes):
        with self.lock:
            for item in self.data["providers"][provider]:
                if item["id"] == key_id:
                    item.update(changes)
                    self.save()
                    return

    def remove_key(self, provider: str, key_id: str):
        with self.lock:
            self.data["providers"][provider] = [
                x for x in self.data["providers"][provider] if x["id"] != key_id
            ]
            self.save()

    def candidates(self, provider: str):
        with self.lock:
            keys = [x.copy() for x in self.data["providers"][provider]
                    if x.get("enabled", True) and x.get("status") not in ("失效", "格式错误")]
            if not keys:
                return []
            index = self.data["round_robin"].get(provider, 0) % len(keys)
            ordered = keys[index:] + keys[:index]
            self.data["round_robin"][provider] = (index + 1) % len(keys)
            self.save()
            return ordered

    def has_candidates(self, provider: str):
        if provider == LOCAL_PROVIDER:
            return True
        with self.lock:
            return any(x.get("enabled", True) and x.get("status") != "失效"
                       and x.get("status") != "格式错误"
                       for x in self.data["providers"].get(provider, []))

    def mark_use(self, provider: str, key_id: str, status="有效", error=""):
        with self.lock:
            for item in self.data["providers"][provider]:
                if item["id"] == key_id:
                    item["uses"] = item.get("uses", 0) + 1
                    item["status"] = status
                    item["last_error"] = error[:300]
                    item["last_checked"] = datetime.now().strftime("%Y-%m-%d %H:%M")
                    break
            self.save()


def masked_key(key: str) -> str:
    # ElevenLabs 网页会话：显示标签，不泄露完整 Cookie
    try:
        if el_web.is_web_secret(key):
            return el_web.display_secret(key)
    except Exception:
        pass
    if len(key) <= 9:
        return "•" * len(key)
    return f"{key[:4]}…{key[-4:]}"


def response_error(resp: requests.Response) -> str:
    try:
        payload = resp.json()
        return json.dumps(payload, ensure_ascii=False)[:500]
    except Exception:
        return resp.text[:500] or f"HTTP {resp.status_code}"


def probe_audio_layout(ffmpeg_path: str, media_path: str):
    creation = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    result = subprocess.run([ffmpeg_path, "-hide_banner", "-i", str(media_path)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                            creationflags=creation, text=True, encoding="utf-8", errors="replace")
    match = re.search(r"Audio:.*?,\s*(\d+)\s*Hz,\s*([^,\r\n]+)", result.stderr)
    return (int(match.group(1)), match.group(2).strip()) if match else None


def normalize_api_key(key: str) -> str:
    """清洗粘贴噪声：BOM、零宽字符、首尾引号/空白、行内空白。"""
    value = str(key or "")
    # 去掉常见不可见字符
    for ch in ("\ufeff", "\u200b", "\u200c", "\u200d", "\u2060", "\xa0"):
        value = value.replace(ch, "")
    value = value.strip().strip("\"'“”‘’`").strip()
    # 密钥不应含空白；若用户从表格粘出带空格，去掉所有空白
    if any(c.isspace() for c in value):
        compact = "".join(value.split())
        # 仅当去掉空白后仍像密钥（无中文）时采用
        if compact and all(ord(c) < 128 for c in compact):
            value = compact
    return value


def _looks_like_elevenlabs_key_id(value: str) -> bool:
    """ElevenLabs 控制台里的「Key ID」是 32/64 位 hex，不是可调用的 secret。

    官方错误：API key ID used as API key — only the full secret (sk_…) works.
    """
    v = str(value or "").strip()
    if re.fullmatch(r"[0-9a-fA-F]{32}", v):
        return True
    if re.fullmatch(r"[0-9a-fA-F]{64}", v):
        return True
    return False


def detect_api_provider(key: str) -> str | None:
    """根据密钥前缀/形态猜测所属服务；无法判断时返回 None。

    规则按「强特征优先」：
    - Gladia 新版 sk_gla… 必须先于通用 sk_
    - ElevenLabs 真密钥是 sk_…（不是 sk-，也不是 Key ID 的 hex）
    """
    value = normalize_api_key(key)
    if not value:
        return None
    # 网页会话包
    try:
        if el_web.is_web_secret(value) or value.startswith(el_web.WEB_KEY_PREFIX):
            return "ElevenLabs"
    except Exception:
        pass
    lower = value.casefold()

    # —— 强前缀（几乎可确定）——
    if value.startswith("gsk_") or lower.startswith("gsk-"):
        return "Groq"
    # Gladia 新版密钥：sk_gla… / sk_gladia…（必须在通用 sk_ 之前）
    if lower.startswith("sk_gla") or lower.startswith("sk_gladia"):
        return "Gladia"
    # Google AI Studio / Gemini
    if lower.startswith("aiza") or value.startswith(("AIza", "AIZa", "aiza", "AIZA")):
        return "Gemini"
    if lower.startswith("aq.") and len(value) >= 20:
        return "Gemini"
    if re.match(r"^aq\.[A-Za-z0-9_\-]{16,}$", value, flags=re.IGNORECASE):
        return "Gemini"
    if lower.startswith("ai") and 35 <= len(value) <= 64 and re.fullmatch(r"[A-Za-z0-9_\-]+", value):
        if not lower.startswith(("airtable", "aidrive")):
            return "Gemini"
    # ElevenLabs 真·API secret（下划线 sk_，且不是 sk_gla）
    if value.startswith("sk_") and len(value) >= 20:
        return "ElevenLabs"
    if value.startswith("xi_") and len(value) >= 16:
        return "ElevenLabs"
    # JWT / Firebase idToken（网页会话用）→ ElevenLabs，勿当 API Key
    if value.count(".") >= 2 and len(value) > 80 and value.startswith("eyJ"):
        return "ElevenLabs"

    # 名称写在密钥里
    if "gladia" in lower:
        return "Gladia"
    if "groq" in lower and ("gsk" in lower or len(value) > 20):
        return "Groq"
    if "gemini" in lower or "generativelanguage" in lower or "googleapis" in lower:
        return "Gemini"
    if "eleven" in lower or "11labs" in lower:
        return "ElevenLabs"
    if "luma" in lower:
        return "Luma"
    if "kling" in lower:
        return "Kling"

    # Gladia：标准 UUID（带连字符）
    if re.fullmatch(
        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
        value,
    ):
        return "Gladia"
    # 32 位 hex：更像 Gladia 旧 key / 通用 id；64 位 hex 多为 ElevenLabs「Key ID」误贴
    # → 不在纯规则里强判，交给联网探测
    if re.fullmatch(r"[0-9a-fA-F]{32}", value):
        return "Gladia"

    # OpenAI 风格 sk- 不是本软件 ASR 服务
    if value.startswith("sk-"):
        return None

    return None


def detect_api_provider_with_probe(key: str, timeout: float = 8.0) -> tuple[str | None, str]:
    """先规则识别；失败则对常见服务短超时探测，返回 (provider, 说明)。"""
    value = normalize_api_key(key)
    if not value:
        return None, "空密钥"

    # 明确误贴了 ElevenLabs 的 Key ID（不是 sk_ secret）
    if _looks_like_elevenlabs_key_id(value) and not value.startswith("sk_"):
        # 先试 Gladia（32 hex 可能是 Gladia）；64 hex 几乎一定是 EL Key ID
        if len(value) == 64:
            return None, (
                "这是 ElevenLabs 的「Key ID」（64 位十六进制），不是可调用的密钥。"
                "请到 elevenlabs.io → API Keys 复制完整 secret（以 sk_ 开头），"
                "或使用「添加网页会话」粘贴 Authorization Bearer。"
            )
        ok_g, msg_g = check_api_key("Gladia", value, timeout=timeout)
        if ok_g:
            return "Gladia", "联网探测确认为 Gladia"
        return None, (
            "无法确认：若是 ElevenLabs，请粘贴 sk_ 开头的完整密钥（不是 Key ID）；"
            f"若是 Gladia：{msg_g[:80]}"
        )

    guessed = detect_api_provider(value)
    if guessed:
        # 对 sk_ 可能仍歧义：sk_gla 已归 Gladia；纯 sk_ 再弱探测一次 Gladia？不必
        return guessed, f"按格式识别为 {guessed}"

    # 探测顺序：特征冲突少、响应快的优先；Gladia 放 Eleven 前以免 64hex 误走 EL
    probe_order = ["Groq", "Gemini", "Gladia", "ElevenLabs"]
    errors = []
    for provider in probe_order:
        ok, message = check_api_key(provider, value, timeout=timeout)
        if ok:
            return provider, f"联网探测确认为 {provider}"
        if "key ID" in message or "Key ID" in message:
            errors.append(f"{provider}:{message[:100]}")
            continue
        if "HTTP 401" in message or "HTTP 403" in message:
            errors.append(f"{provider}:密钥形态匹配但未通过")
        else:
            errors.append(f"{provider}:{message[:60]}")
    detail = "；".join(errors[:4])
    return None, f"无法识别（{detail}）"


def check_api_key(provider: str, key: str, timeout: float = 20.0) -> tuple[bool, str]:
    # HTTP headers must be ASCII/Latin-1 encodable; APP_NAME contains Chinese.
    headers = {"User-Agent": "VideoToolkit/1.0"}
    key = normalize_api_key(key)
    try:
        if not key or any(ord(char) < 33 or ord(char) > 126 for char in key):
            return False, "密钥格式异常：含有空格、中文、全角字符或其他非法字符"
        if provider == "ElevenLabs" and _looks_like_elevenlabs_key_id(key) and not key.startswith("sk_"):
            return False, (
                "格式错误：粘贴的是 ElevenLabs「Key ID」而不是密钥。"
                "请复制以 sk_ 开头的完整 API secret，或改用「添加网页会话」。"
            )
        if provider == "Groq":
            resp = requests.get("https://api.groq.com/openai/v1/models",
                                headers={**headers, "Authorization": f"Bearer {key}"}, timeout=timeout)
        elif provider == "Gemini":
            resp = requests.get("https://generativelanguage.googleapis.com/v1beta/models",
                                headers={**headers, "x-goog-api-key": key}, timeout=timeout)
        elif provider == "ElevenLabs":
            # 纯 JWT → 临时包成网页会话再验
            if key.count(".") >= 2 and key.startswith("eyJ") and not el_web.is_web_secret(key):
                try:
                    packed = el_web.pack_web_session(
                        cookie="", authorization=f"Bearer {key}", xi_api_key="", label="JWT",
                    )
                    ok, message, _q = el_web.verify_session(packed, timeout=max(timeout, 45.0))
                    return ok, message
                except Exception as exc:
                    return False, f"JWT 会话验证失败：{exc}"
            ok, message, _quota = el_web.verify_session(key, timeout=max(timeout, 45.0))
            # 把官方「key ID」错误翻译成人话
            if (not ok) and ("invalid_api_key" in (message or "") or "API key ID" in (message or "")):
                return False, (
                    "ElevenLabs 拒绝：当前字符串不是有效 API secret（常见原因：只复制了 Key ID）。"
                    "请到 https://elevenlabs.io/app/settings/api-keys 创建/复制 sk_… 密钥，"
                    "或使用「添加 ElevenLabs 网页会话」。原文：" + message[:160]
                )
            return ok, message
        elif provider == "Luma":
            return True, "密钥格式有效，免联机检测"
        elif provider == "Kling":
            return True, "密钥格式有效，免联机检测"
        else:
            # Gladia
            resp = requests.get("https://api.gladia.io/v2/pre-recorded?limit=1",
                                headers={**headers, "x-gladia-key": key}, timeout=timeout)
        if resp.status_code < 300:
            return True, "验证通过"
        body = response_error(resp)
        if provider == "ElevenLabs" or "invalid_api_key" in body or "API key ID" in body:
            return False, (
                f"HTTP {resp.status_code}: {body}\n"
                "提示：ElevenLabs 必须使用 sk_ 完整密钥，不能用控制台里的 Key ID。"
            )
        return False, f"HTTP {resp.status_code}: {body}"
    except Exception as exc:
        return False, f"网络检测失败：{exc}"


def reclassify_misplaced_keys(store: "ConfigStore") -> list[str]:
    """修正历史误归类：如 sk_gla 进了 ElevenLabs、纯 Key ID 标错等。返回操作说明。"""
    notes = []
    # 1) Gladia 的 sk_gla 若在 ElevenLabs → 挪回 Gladia
    move_pairs = []
    for provider in list(PROVIDERS):
        for item in list(store.data["providers"].get(provider) or []):
            key = item.get("key") or ""
            if el_web.is_web_secret(key):
                continue
            right = detect_api_provider(key)
            if not right or right == provider:
                # 标出 Key ID 误用
                if provider == "ElevenLabs" and _looks_like_elevenlabs_key_id(key) and not key.startswith("sk_"):
                    store.update_key(
                        provider, item["id"],
                        status="格式错误",
                        last_error=(
                            "这是 ElevenLabs Key ID，不是 sk_ 密钥。请删除后重新添加 sk_… 或网页会话。"
                        ),
                    )
                    notes.append(f"标记 {provider}/{masked_key(key)} 为格式错误（Key ID）")
                continue
            move_pairs.append((provider, right, item))
    for src, dst, item in move_pairs:
        key = item["key"]
        # 目标是否已有相同 key
        exists = any(x.get("key") == key for x in store.data["providers"].get(dst) or [])
        store.remove_key(src, item["id"])
        if not exists:
            store.add_key(dst, key)
            notes.append(f"已迁移 {masked_key(key)}：{src} → {dst}")
        else:
            notes.append(f"已从 {src} 删除重复 {masked_key(key)}（{dst} 已有）")
    return notes


class KeyCheckWorker(QObject):
    progress = Signal(str, str, bool, str)
    finished = Signal()

    def __init__(self, jobs):
        super().__init__()
        self.jobs = jobs

    def run(self):
        for provider, item in self.jobs:
            ok, message = check_api_key(provider, item["key"])
            self.progress.emit(provider, item["id"], ok, message)
        self.finished.emit()


def timestamp_srt(seconds: float) -> str:
    millis = max(0, round(float(seconds) * 1000))
    hours, rem = divmod(millis, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, ms = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"


def segments_to_srt(segments, language=None) -> str:
    blocks = []
    for i, seg in enumerate(segments, 1):
        text = normalize_subtitle_text(
            re.sub(r"\s+", " ", str(seg.get("text", ""))).strip(), language=language)
        if not text or is_asr_junk_caption(text):
            continue
        start = seg.get("start", 0)
        end = max(float(seg.get("end", start + 2)), float(start) + 0.2)
        blocks.append(f"{len(blocks)+1}\n{timestamp_srt(start)} --> {timestamp_srt(end)}\n{text}")
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def words_to_segments(words):
    segments, current, start, end = [], [], None, None
    for word in words or []:
        if word.get("type") not in (None, "word"):
            continue
        text = str(word.get("text", "")).strip()
        if not text:
            continue
        w_start = float(word.get("start") or end or 0)
        w_end = float(word.get("end") or (w_start + 0.3))
        if start is None:
            start = w_start
        current.append(text)
        end = w_end
        joined = "".join(current) if any("\u4e00" <= c <= "\u9fff" for c in text) else " ".join(current)
        if end - start >= 6 or len(joined) >= 34 or re.search(r"[。！？.!?]$", text):
            segments.append({"start": start, "end": end, "text": joined})
            current, start, end = [], None, None
    if current:
        joined = "".join(current) if any(any("\u4e00" <= c <= "\u9fff" for c in x) for x in current) else " ".join(current)
        segments.append({"start": start or 0, "end": end or (start or 0) + 2, "text": joined})
    return segments


def clean_model_srt(text: str, language=None) -> str:
    text = re.sub(r"^```(?:srt)?\s*", "", text.strip(), flags=re.I)
    text = re.sub(r"\s*```$", "", text)
    text = text.replace("\r\n", "\n")
    text = normalize_subtitle_text(text, language=language)
    if "-->" not in text:
        body = normalize_subtitle_text(text.strip(), language=language)
        if is_asr_junk_caption(body):
            return ""
        return f"1\n00:00:00,000 --> 99:59:59,000\n{body}\n"
    # 去掉 Whisper 幻觉水印行（如「Υπότιτλοι AUTHORWAVE」）
    return filter_asr_junk_srt(text.strip() + "\n")


SUPPORTED_VIDEO_DOMAINS = (
    "youtube.com", "youtu.be", "facebook.com", "fb.watch",
    "instagram.com", "tiktok.com",
)
MEDIA_EXTENSIONS = {
    ".mp4", ".mov", ".mkv", ".avi", ".wmv", ".webm", ".m4v", ".ts",
    ".mp3", ".wav", ".m4a", ".flac", ".aac", ".ogg", ".opus", ".wma",
}


def is_supported_video_url(value: str) -> bool:
    try:
        parsed = urlparse(value.strip())
        host = (parsed.hostname or "").lower()
        return parsed.scheme in ("http", "https") and any(
            host == domain or host.endswith("." + domain) for domain in SUPPORTED_VIDEO_DOMAINS
        )
    except Exception:
        return False


def natural_path_key(value: str):
    return [int(part) if part.isdigit() else part.casefold()
            for part in re.split(r"(\d+)", value)]


class MediaDropList(QListWidget):
    paths_dropped = Signal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DropOnly)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls() and any(url.isLocalFile() for url in event.mimeData().urls()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        paths = [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
        if paths:
            self.paths_dropped.emit(paths)
            event.acceptProposedAction()
        else:
            event.ignore()


class ApiFailure(RuntimeError):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class TranscribeWorker(QObject):
    log = Signal(str)
    progress = Signal(int)
    result_ready = Signal(str, str, str, str)
    finished = Signal(bool, str)

    # 图文成片/批量识别每次新建 Worker 时复用同一 Whisper，避免 12 条任务重复加载 medium 卡死感
    _shared_local_lock = threading.Lock()
    _shared_local_model = None
    _shared_local_model_name = None
    _shared_local_device = None

    def __init__(self, store: ConfigStore, provider: str, model: str, files: list[str],
                 output_dir: str, language: str, diarize: bool, ffmpeg_path: str,
                 resume_existing: bool = True, allow_provider_fallback: bool = True,
                 translate_result: bool = True):
        super().__init__()
        self.store = store
        self.provider = provider
        self.model = model
        self.files = files
        self.language = language.strip()
        self.diarize = diarize
        self.ffmpeg_path = ffmpeg_path
        self.cancelled = False
        self._local_model = None
        self._local_device = None
        self.resume_existing = resume_existing
        self.allow_provider_fallback = allow_provider_fallback
        # Reels only needs the original transcript and word timestamps.  Translating
        # every result here can take longer than ASR itself when the free translator
        # is rate-limited, so callers that only need a timeline may opt out.
        self.translate_result = bool(translate_result)
        task_payload = {
            "version": 2, "provider": provider, "model": model,
            "language": self.language, "diarize": bool(diarize),
            "sources": [source_signature(source) for source in files],
        }
        self.task_id = stable_key(task_payload)
        self.output_dir = (Path(output_dir) if output_dir
                           else app_data_dir() / "subtitle_tasks" / self.task_id)
        self.checkpoint_path = self.output_dir / "checkpoint.json"
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def cancel(self):
        self.cancelled = True

    def run(self):
        try:
            if not self.resume_existing:
                shutil.rmtree(self.output_dir / ".work", ignore_errors=True)
            state = read_json_file(self.checkpoint_path, {}) if self.resume_existing else {}
            if state.get("task_id") != self.task_id:
                state = {}
            state.setdefault("task_id", self.task_id)
            state.setdefault("results", {})
            state["status"] = "running"
            atomic_write_json(self.checkpoint_path, state)
            failures = []
            for index, source in enumerate(self.files):
                if self.cancelled:
                    raise RuntimeError("任务已取消")
                source_key = stable_key(source_signature(source))
                cached = state["results"].get(source_key)
                if cached:
                    cached_original = normalize_required_capitalization(cached.get("original", ""))
                    cached_srt = normalize_required_capitalization(cached.get("srt", ""))
                    self.log.emit(f"断点续接：跳过已完成字幕 {index + 1}/{len(self.files)}：{cached['name']}")
                    self.result_ready.emit(cached["name"], cached_original,
                                           cached.get("chinese", ""), cached_srt)
                    self.progress.emit(round((index + 1) / len(self.files) * 100))
                    continue
                display = source if is_supported_video_url(source) else Path(source).name
                self.log.emit(f"正在处理 {index + 1}/{len(self.files)}：{display}")
                try:
                    result = self._process_one(source)
                    state["results"][source_key] = {
                        "source": source, "name": result["name"], "original": result["original"],
                        "chinese": result["chinese"], "srt": result["srt"],
                        "completed_at": datetime.now().isoformat(timespec="seconds"),
                    }
                    atomic_write_json(self.checkpoint_path, state)
                    self._cleanup_source_work(source)
                except Exception as exc:
                    message=f"{display}：{exc}"
                    failures.append(message); self.log.emit(f"当前素材失败，已记录并继续下一项：{message}")
                    write_app_log(message,"ERROR","字幕批处理")
                self.progress.emit(round((index + 1) / len(self.files) * 100))
            state["status"] = "completed_with_errors" if failures else "completed"
            atomic_write_json(self.checkpoint_path, state)
            succeeded=len(self.files)-len(failures)
            self.finished.emit(bool(succeeded),
                               f"批量字幕完成：成功 {succeeded} 个，失败 {len(failures)} 个；失败项已写入软件日志。"
                               if failures else "完成，字幕与中文对照已显示在当前窗口")
        except Exception as exc:
            try:
                state["status"] = "failed"
                state["last_error"] = str(exc)
                atomic_write_json(self.checkpoint_path, state)
            except Exception:
                pass
            self.finished.emit(False, str(exc))

    def _source_work_dir(self, source_value: str) -> Path:
        path = self.output_dir / ".work" / self._source_work_key(source_value)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _source_work_key(self, source_value: str) -> str:
        return stable_key({"source": source_signature(source_value), "provider": self.provider,
                           "model": self.model, "language": self.language})[:20]

    def _cleanup_source_work(self, source_value: str):
        work = self.output_dir / ".work" / self._source_work_key(source_value)
        if work.exists():
            shutil.rmtree(work, ignore_errors=True)

    def _media_has_audio(self, media_path: Path) -> bool:
        """探测文件是否含音轨（TikTok 等有时只下到静音画面）。"""
        ffprobe = str(Path(self.ffmpeg_path).with_name(
            "ffprobe.exe" if os.name == "nt" else "ffprobe"))
        if not Path(ffprobe).is_file():
            ffprobe = self.ffmpeg_path.replace("ffmpeg", "ffprobe")
        run_kw: dict = {
            "stdout": subprocess.PIPE,
            "stderr": subprocess.DEVNULL,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "timeout": 30,
        }
        if os.name == "nt":
            run_kw["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            result = subprocess.run(
                [
                    ffprobe, "-v", "error", "-select_streams", "a",
                    "-show_entries", "stream=index",
                    "-of", "csv=p=0", str(media_path),
                ],
                **run_kw,
            )
            return bool((result.stdout or "").strip())
        except Exception:
            return False

    def _download_online_media(self, url: str, temp: Path):
        from modules.ytdlp_utils import download_media, ytdlp_status

        ok, detail = ytdlp_status()
        if not ok:
            raise RuntimeError("缺少网络视频解析组件 yt-dlp，请到“组件管理”点击「一键更新 yt-dlp」。")

        self.log.emit(f"正在解析并静默下载网络视频音轨 …（{detail}）")
        last_percent = {"value": ""}

        def download_hook(data):
            if self.cancelled:
                raise RuntimeError("任务已取消")
            if data.get("status") == "downloading":
                percent = re.sub(r"\x1b\[[0-9;]*m", "", data.get("_percent_str", "")).strip()
                if percent and percent != last_percent["value"]:
                    last_percent["value"] = percent
                    self.log.emit(f"网络视频下载中：{percent}")

        # TikTok 等：bestaudio 不可用时 best 可能是「仅画面」；优先带音轨的组合
        format_attempts = [
            (
                "bestaudio/bestvideo*+bestaudio/best",
                {"restrictfilenames": True, "merge_output_format": "mp4"},
            ),
            (
                "bv*+ba/b",
                {"restrictfilenames": True, "merge_output_format": "mp4"},
            ),
            (
                "bestaudio/best",
                {"restrictfilenames": True},
            ),
        ]
        source = None
        info = None
        last_exc: Exception | None = None
        for fmt, extra in format_attempts:
            try:
                # 清掉上一轮无音轨的残片，避免误复用
                for old in temp.glob("online_source.*"):
                    if old.suffix.lower() in (".part", ".ytdl", ".json"):
                        continue
                    try:
                        if not self._media_has_audio(old):
                            old.unlink(missing_ok=True)
                    except Exception:
                        pass
                prepared_str, info = download_media(
                    url,
                    str(temp / "online_source.%(ext)s"),
                    format_spec=fmt,
                    progress_hooks=[download_hook],
                    extra_opts=extra,
                    log=self.log.emit,
                )
                prepared = Path(prepared_str)
                candidates = [prepared] if prepared.exists() else []
                candidates += [
                    p for p in temp.glob("online_source.*")
                    if p.suffix.lower() not in (".part", ".ytdl", ".json")
                ]
                candidate = next((p for p in candidates if p.exists() and p.is_file()), None)
                if not candidate:
                    continue
                if self._media_has_audio(candidate):
                    source = candidate
                    self.log.emit(f"已下载含音轨媒体：{candidate.name}（格式 {fmt}）")
                    break
                self.log.emit(
                    f"下载文件无音轨（{candidate.name}，格式 {fmt}），尝试其它格式…"
                )
                last_exc = RuntimeError("下载结果无音轨")
            except Exception as exc:
                last_exc = exc
                self.log.emit(f"下载尝试失败（{fmt}）：{exc}")
                continue

        if not source:
            hint = (
                "网络视频下载完成，但没有可用音轨。"
                "TikTok/抖音等有时只下到静音画面。"
                "请：① 组件管理更新 yt-dlp；② 换浏览器可播的完整链接；"
                "③ 或先本地下载含声音的 mp4 再识别。"
            )
            if last_exc:
                raise RuntimeError(f"{hint}\n技术详情：{last_exc}") from last_exc
            raise RuntimeError(hint)
        title = re.sub(r"[\\/:*?\"<>|]+", "_", str((info or {}).get("title") or "网络视频")).strip()
        return source, (title[:100] or "网络视频")

    def _process_one(self, source_value: str):
        candidates = ([{"id": "local", "key": ""}] if self.provider == LOCAL_PROVIDER
                      else self.store.candidates(self.provider))
        if not candidates:
            raise RuntimeError(f"{self.provider} 没有可用密钥，请先到“密钥管理”添加并检测。")
        # 非断点模式：清掉该素材工作目录，避免复用 Groq 错误分段缓存
        if not self.resume_existing:
            work = self.output_dir / ".work" / self._source_work_key(source_value)
            if work.exists():
                shutil.rmtree(work, ignore_errors=True)
        temp = self._source_work_dir(source_value)
        if is_supported_video_url(source_value):
            metadata_path = temp / "online_source.json"
            metadata = read_json_file(metadata_path, {})
            saved_path = Path(metadata.get("path", "")) if metadata.get("path") else None
            if saved_path and saved_path.exists() and self._media_has_audio(saved_path):
                source = saved_path
                result_name = metadata.get("title") or source.name
                self.log.emit("断点续接：复用已下载的网络媒体。")
            else:
                if saved_path and saved_path.exists() and not self._media_has_audio(saved_path):
                    self.log.emit("缓存媒体无音轨，重新下载…")
                    try:
                        saved_path.unlink(missing_ok=True)
                    except Exception:
                        pass
                source, result_name = self._download_online_media(source_value, temp)
                atomic_write_json(metadata_path, {"path": str(source), "title": result_name})
        else:
            source = Path(source_value)
            result_name = source.name

        if not self._media_has_audio(source):
            raise RuntimeError(
                f"媒体没有音轨，无法识别字幕：{result_name}。"
                "（TikTok/抖音链接有时只下到静音画面；请更新 yt-dlp 或改用含声音的本地文件。）"
            )

        if self.provider == LOCAL_PROVIDER:
            recognition_input = source
            self.log.emit("本地 Whisper 直接流式读取媒体，不创建整段 PCM 副本。")
        elif self.provider == "Groq":
            recognition_input = source
            self.log.emit("Groq 将直接从媒体生成 90 秒分段，不创建整段 PCM 副本。")
        else:
            audio = temp / "audio.wav"
            if not audio.exists() or audio.stat().st_size == 0:
                self.log.emit("创建临时 PCM 无损识别副本（保留原声道；不会修改视频音轨）…")
                cmd = [self.ffmpeg_path, "-y", "-i", str(source), "-map", "0:a:0", "-vn",
                       "-c:a", "pcm_s16le", str(audio)]
                creation = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
                proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                      creationflags=creation, text=True, encoding="utf-8", errors="replace")
                if proc.returncode != 0 or not audio.exists():
                    raise RuntimeError(
                        "无法提取音频，请确认视频包含音轨。\n" + (proc.stderr or "")[-800:]
                    )
            else:
                self.log.emit("断点续接：复用已提取的识别音频。")
            recognition_input = audio

        last_error = ""
        for item in candidates:
            if self.cancelled:
                raise RuntimeError("任务已取消")
            if self.provider == LOCAL_PROVIDER:
                self.log.emit("使用本地 Whisper 模型，无需上传媒体或 API 密钥 …")
            else:
                self.log.emit(f"使用 {self.provider} 密钥 {masked_key(item['key'])} …")
            try:
                srt, plain, raw = self._call_provider(recognition_input, item["key"], temp)
                srt = normalize_required_capitalization(srt)
                plain = normalize_required_capitalization(plain)
                chinese = self._translate_chinese(plain) if self.translate_result else ""
                if not self.translate_result:
                    self.log.emit("已生成精确时间轴；Reels 快速模式跳过中文对照翻译。")
                self.result_ready.emit(result_name, plain, chinese, srt)
                if self.provider != LOCAL_PROVIDER:
                    self.store.mark_use(self.provider, item["id"], "有效", "")
                self.log.emit(f"已在当前窗口生成中外文对照：{result_name}")
                return {"name": result_name, "original": plain, "chinese": chinese,
                        "srt": srt, "raw": raw}
            except ApiFailure as exc:
                last_error = str(exc)
                if exc.status in (401, 403):
                    status = "失效"
                elif exc.status == 429:
                    status = "额度受限"
                else:
                    status = "异常"
                self.store.mark_use(self.provider, item["id"], status, last_error)
                self.log.emit(f"密钥 {masked_key(item['key'])} 失败（{status}），自动轮换下一枚。")
            except requests.RequestException as exc:
                last_error = f"网络请求失败：{exc}"
                if self.provider != LOCAL_PROVIDER:
                    self.store.mark_use(self.provider, item["id"], "异常", last_error)
                    self.log.emit(f"密钥 {masked_key(item['key'])} 网络失败，保留分段进度并轮换下一枚。")
        primary_error=f"{self.provider} 的可用密钥均调用失败。最后错误：{last_error}"
        if self.allow_provider_fallback:
            fallbacks=list(self.store.data.get("provider_priority") or [])+[LOCAL_PROVIDER]
            for provider in fallbacks:
                if provider==self.provider: continue
                if provider!=LOCAL_PROVIDER and not self.store.has_candidates(provider): continue
                self.log.emit(f"{primary_error}；自动切换到 {provider} 继续识别。")
                write_app_log(f"{primary_error}；切换到 {provider}","WARNING","字幕识别")
                child=TranscribeWorker(self.store,provider,self.store.data["models"].get(provider,DEFAULT_MODELS[provider]),[],
                                       str(self.output_dir),self.language,self.diarize,self.ffmpeg_path,
                                       self.resume_existing,False,self.translate_result)
                child.log.connect(self.log.emit); child.result_ready.connect(self.result_ready.emit)
                try: return child._process_one(source_value)
                except Exception as exc:
                    self.log.emit(f"备用识别服务 {provider} 失败，继续切换：{exc}")
                    write_app_log(f"{provider} 备用识别失败：{exc}","WARNING","字幕识别")
        raise RuntimeError(primary_error)

    def _translate_chinese(self, text: str) -> str:
        text = text.strip()
        if not text:
            return ""
        visible = [c for c in text if c.isalpha() or "\u4e00" <= c <= "\u9fff"]
        chinese_count = sum("\u4e00" <= c <= "\u9fff" for c in visible)
        if visible and chinese_count / len(visible) > 0.45:
            return text
        self.log.emit("正在生成中文字幕对照 …")
        for item in self.store.candidates("Gemini"):
            try:
                prompt = ("把下面字幕准确翻译成简体中文。保留原有换行和段落顺序，只输出译文，"
                          "不要解释，不要 Markdown：\n\n" + text)
                model = self.store.data["models"].get("Gemini", DEFAULT_MODELS["Gemini"])
                response = requests.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                    headers={"x-goog-api-key": item["key"], "Content-Type": "application/json"},
                    json={"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"temperature": 0.1}},
                    timeout=600)
                if response.status_code >= 300:
                    raise ApiFailure(response_error(response), response.status_code)
                payload = response.json()
                translated = "\n".join(part.get("text", "") for candidate in payload.get("candidates", [])
                                       for part in candidate.get("content", {}).get("parts", [])).strip()
                if translated:
                    self.store.mark_use("Gemini", item["id"], "有效", "")
                    return translated
            except Exception as exc:
                self.store.mark_use("Gemini", item["id"], "异常", str(exc))
        try:
            from deep_translator import GoogleTranslator
            translator = GoogleTranslator(source="auto", target="zh-CN")
            chunks, current = [], ""
            for line in text.splitlines() or [text]:
                if len(current) + len(line) + 1 > 3500 and current:
                    chunks.append(current); current = ""
                current += ("\n" if current else "") + line
            if current: chunks.append(current)
            return "\n".join(translator.translate(chunk) or "" for chunk in chunks).strip()
        except Exception as exc:
            self.log.emit(f"无密钥翻译暂不可用：{exc}")
            return "【自动翻译失败；可在密钥管理添加 Gemini 密钥后重试】\n" + text

    def _call_provider(self, audio: Path, key: str, temp: Path):
        if self.provider == LOCAL_PROVIDER:
            return self._local_whisper(audio)
        if self.provider == "Groq":
            return self._groq(audio, key, temp)
        if self.provider == "Gemini":
            return self._gemini(audio, key)
        if self.provider == "ElevenLabs":
            return self._elevenlabs(audio, key)
        return self._gladia(audio, key)

    def _local_whisper(self, audio: Path):
        try:
            from faster_whisper import WhisperModel
            import ctranslate2
        except ImportError as exc:
            raise RuntimeError("缺少本地字幕组件，请运行：pip install faster-whisper") from exc
        model_name = str(self.model or DEFAULT_MODELS[LOCAL_PROVIDER]).strip() or "medium"
        # 兼容界面展示名
        for code, label in LOCAL_WHISPER_MODEL_OPTIONS:
            if model_name == label or model_name.startswith(code):
                model_name = code
                break
        if model_name not in {code for code, _ in LOCAL_WHISPER_MODEL_OPTIONS}:
            self.log.emit(f"未知本地模型「{model_name}」，回退 medium")
            write_app_log(f"未知本地模型「{model_name}」，回退 medium", "WARNING", "字幕识别")
            model_name = "medium"

        def _emit(msg: str) -> None:
            try:
                self.log.emit(msg)
            except Exception:
                pass
            write_app_log(msg, "INFO", "字幕识别")

        # 复用跨 Worker 的共享模型（图文成片每条任务都会新建 Worker）
        with TranscribeWorker._shared_local_lock:
            shared_ok = (
                TranscribeWorker._shared_local_model is not None
                and TranscribeWorker._shared_local_model_name == model_name
            )
            if shared_ok:
                self._local_model = TranscribeWorker._shared_local_model
                self._local_device = TranscribeWorker._shared_local_device or "cpu"
                self._local_model_name = model_name
                _emit(f"复用已加载的本地 Whisper 模型：{model_name}（{self._local_device}）")
            else:
                _emit(
                    f"正在加载本地 Whisper 模型：{model_name}"
                    f"（首次使用会下载；medium/large 在 CPU 上可能需数分钟，并非卡死）…"
                )
                has_cuda = ctranslate2.get_cuda_device_count() > 0
                # CPU 多线程；small 用 int8 更快。不影响词级时间戳准确度的前提下提速加载。
                cpu_threads = max(2, min(16, (os.cpu_count() or 4)))
                try:
                    self._local_model = WhisperModel(
                        model_name,
                        device="cuda" if has_cuda else "cpu",
                        compute_type="float16" if has_cuda else "int8",
                        cpu_threads=cpu_threads,
                        num_workers=1,
                    )
                    self._local_device = "cuda" if has_cuda else "cpu"
                    self._local_model_name = model_name
                except (ValueError, RuntimeError) as exc:
                    if not has_cuda:
                        raise
                    _emit(f"当前 GPU 模式不可用，自动切换 CPU INT8：{exc}")
                    self._local_model = WhisperModel(
                        model_name, device="cpu", compute_type="int8",
                        cpu_threads=cpu_threads, num_workers=1,
                    )
                    self._local_device = "cpu"
                    self._local_model_name = model_name
                TranscribeWorker._shared_local_model = self._local_model
                TranscribeWorker._shared_local_model_name = model_name
                TranscribeWorker._shared_local_device = self._local_device
                _emit(f"本地 Whisper 模型已就绪：{model_name}（{self._local_device}）")

        language = None if not self.language or self.language == "auto" else self.language

        # 先抽 mono 16k WAV：本地 Whisper 解码更快、时间戳更稳（避免直接喂整段 mp4 又慢又容易第一次对不齐）
        asr_audio = Path(audio)
        prep_wav = None
        try:
            import tempfile
            creation = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            prep_dir = Path(tempfile.mkdtemp(prefix="vt_whisper_"))
            prep_wav = prep_dir / "asr_16k.wav"
            _emit(f"本地识别：抽取音轨 {audio.name} → 16k mono …")
            prep = subprocess.run(
                [
                    self.ffmpeg_path, "-y", "-hide_banner", "-loglevel", "error",
                    "-i", str(audio), "-map", "0:a:0", "-vn",
                    "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
                    str(prep_wav),
                ],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                creationflags=creation, text=True, encoding="utf-8", errors="replace",
            )
            if prep.returncode == 0 and prep_wav.is_file() and prep_wav.stat().st_size > 1000:
                asr_audio = prep_wav
            else:
                err = (prep.stderr or "")[-400:]
                _emit(f"16k 抽取未成功，回退源文件识别：{err or 'unknown'}")
                prep_wav = None
        except Exception as prep_exc:
            _emit(f"16k 抽取异常，回退源文件：{prep_exc}")
            prep_wav = None

        audio_dur = 0.0
        try:
            audio_dur = float(video_duration(self.ffmpeg_path, str(asr_audio)) or 0)
        except Exception:
            audio_dur = 0.0
        _emit(f"开始本地识别：{asr_audio.name}（约 {audio_dur:.0f}s）…")

        def collect_segments(stream, info, *, offset=0.0, label=""):
            segments = []
            for item in stream:
                if self.cancelled:
                    raise RuntimeError("任务已取消")
                words = [
                    {
                        "start": float(word.start) + offset,
                        "end": float(word.end) + offset,
                        "text": word.word.strip(),
                    }
                    for word in (getattr(item, "words", None) or []) if word.word.strip()
                ]
                segments.append({
                    "start": float(item.start) + offset,
                    "end": float(item.end) + offset,
                    "text": item.text.strip(),
                    "words": words,
                })
                if len(segments) % 5 == 0 or len(segments) == 1:
                    _emit(f"本地识别中{label}：已生成 {len(segments)} 条字幕 …")
            return segments, info

        def transcribe_file(model, path, *, offset=0.0, beam_size=None, use_vad=None, label=""):
            try:
                import onnxruntime  # noqa: F401
                vad_default = True
            except ImportError:
                vad_default = False
                if use_vad is None:
                    _emit("未检测到 ONNX Runtime，已自动关闭 VAD 静音过滤并继续识别。")
            if use_vad is None:
                use_vad = vad_default
            if beam_size is None:
                name = str(getattr(self, "_local_model_name", "") or "").lower()
                if self._local_device == "cuda" and "large" in name:
                    beam_size = 2
                else:
                    beam_size = 1
            try:
                stream, info = model.transcribe(
                    str(path),
                    language=language,
                    beam_size=beam_size,
                    best_of=1,
                    patience=1.0,
                    temperature=0.0,
                    vad_filter=use_vad,
                    word_timestamps=True,
                    condition_on_previous_text=False,
                    without_speech_threshold=0.6,
                    vad_parameters={
                        "min_silence_duration_ms": 350,
                        "speech_pad_ms": 120,
                    } if use_vad else None,
                )
                return collect_segments(stream, info, offset=offset, label=label)
            except RuntimeError as exc:
                if not use_vad or "onnxruntime" not in str(exc).lower():
                    raise
                _emit("VAD 组件不可用，已关闭静音过滤并自动重试当前片段。")
                stream, info = model.transcribe(
                    str(path), language=language, beam_size=1, best_of=1,
                    temperature=0.0, vad_filter=False, word_timestamps=True,
                    condition_on_previous_text=False,
                )
                return collect_segments(stream, info, offset=offset, label=label)

        def _word_density_ok(segs, expect_dur=0.0) -> bool:
            if not segs:
                return False
            n_words = sum(len(s.get("words") or []) for s in segs)
            span = max(0.5, float(segs[-1].get("end") or 0) - float(segs[0].get("start") or 0))
            if n_words < max(3, int(span / 1.6)):
                return False
            # 长视频：词轴覆盖率过低 = 第一次常对不上口型
            if expect_dur >= 90 and n_words > 0:
                last_end = max(float(s.get("end") or 0) for s in segs)
                if last_end < expect_dur * 0.72:
                    return False
            return True

        def _transcribe_long_chunked(model, wav_path, total_dur):
            """≥90s：按段识别再拼时间戳，避免一次跑完整长片词轴漂移/截断。"""
            import tempfile
            creation = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            chunk_len, overlap = 75.0, 1.5
            chunk_dir = Path(tempfile.mkdtemp(prefix="vt_whisper_chunks_"))
            pattern = chunk_dir / "c_%04d.wav"
            cmd = [
                self.ffmpeg_path, "-y", "-hide_banner", "-loglevel", "error",
                "-i", str(wav_path), "-map", "0:a:0",
                "-f", "segment", "-segment_time", str(int(chunk_len)),
                "-reset_timestamps", "1", "-ac", "1", "-ar", "16000",
                "-c:a", "pcm_s16le", str(pattern),
            ]
            _emit(f"长音频（{total_dur:.0f}s）分段识别：每段约 {int(chunk_len)}s …")
            proc = subprocess.run(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                creationflags=creation, text=True, encoding="utf-8", errors="replace",
            )
            chunks = sorted(chunk_dir.glob("c_*.wav"))
            if proc.returncode != 0 or not chunks:
                err = (proc.stderr or "")[-300:]
                _emit(f"分段失败，回退整段识别：{err or 'unknown'}")
                try:
                    shutil.rmtree(chunk_dir, ignore_errors=True)
                except Exception:
                    pass
                return None
            all_segs = []
            info = None
            for i, chunk in enumerate(chunks):
                if self.cancelled:
                    raise RuntimeError("任务已取消")
                offset = i * chunk_len
                _emit(f"本地识别分段 {i + 1}/{len(chunks)}（起点 {offset:.0f}s）…")
                segs, info = transcribe_file(
                    model, chunk, offset=offset, label=f"[{i + 1}/{len(chunks)}]",
                )
                # 与上一段重叠区：丢掉本段 offset 附近 overlap 内的重复词
                if all_segs and overlap > 0:
                    cut = offset + overlap * 0.35
                    segs = [s for s in segs if float(s.get("start") or 0) >= cut]
                    for s in segs:
                        s["words"] = [
                            w for w in (s.get("words") or [])
                            if float(w.get("start") or 0) >= cut
                        ]
                all_segs.extend(segs)
            try:
                shutil.rmtree(chunk_dir, ignore_errors=True)
            except Exception:
                pass
            return all_segs, info

        try:
            import shutil
            _emit(
                f"本地识别参数：beam=1、VAD 跳过静音、词级时间戳开启"
                f"（设备 {self._local_device}，模型 {getattr(self, '_local_model_name', '?')}）"
            )
            segments, info = None, None
            if audio_dur >= 90 and prep_wav is not None:
                chunked = _transcribe_long_chunked(self._local_model, asr_audio, audio_dur)
                if chunked is not None:
                    segments, info = chunked
            if segments is None:
                segments, info = transcribe_file(self._local_model, asr_audio)
            # 覆盖率/密度不足：关 VAD 整段补跑（解决「第一次偏、再提取才准」）
            if not _word_density_ok(segments, audio_dur):
                n_words = sum(len(s.get("words") or []) for s in segments)
                last_end = max((float(s.get("end") or 0) for s in segments), default=0)
                _emit(
                    f"本地识别覆盖不足（词 {n_words}，末词 {last_end:.1f}s / 音频 {audio_dur:.1f}s），"
                    f"关闭 VAD 整段补跑一次…"
                )
                segments2, info2 = transcribe_file(
                    self._local_model, asr_audio, use_vad=False, label="[补跑]",
                )
                if _word_density_ok(segments2, audio_dur) or len(segments2) >= len(segments):
                    segments, info = segments2, info2
        except RuntimeError as exc:
            if self._local_device != "cuda" or self.cancelled:
                raise
            _emit(f"GPU 长视频识别中断，自动改用 CPU INT8 从当前视频重试：{exc}")
            with TranscribeWorker._shared_local_lock:
                self._local_model = WhisperModel(
                    model_name, device="cpu", compute_type="int8",
                    cpu_threads=max(1, min(8, os.cpu_count() or 4)),
                )
                self._local_device = "cpu"
                self._local_model_name = model_name
                TranscribeWorker._shared_local_model = self._local_model
                TranscribeWorker._shared_local_model_name = model_name
                TranscribeWorker._shared_local_device = "cpu"
            segments, info = transcribe_file(self._local_model, asr_audio)
        detected = getattr(info, "language", None) or language
        plain = "\n".join(
            normalize_subtitle_text(x["text"], language=detected) for x in segments)
        raw = {"provider": "Local Whisper", "model": self.model,
               "language": detected, "segments": segments,
               "words": [word for segment in segments for word in segment.get("words", [])]}
        _emit(f"本地识别完成：{audio.name}（{len(segments)} 段，{len(raw['words'])} 词）")
        try:
            if prep_wav is not None:
                shutil.rmtree(prep_wav.parent, ignore_errors=True)
        except Exception:
            pass
        return segments_to_srt(segments, language=detected), plain, raw

    def _groq_payload_is_suspicious(self, payload: dict, duration: float) -> str:
        """识别 turbo 等模型在希腊语等语种上的典型幻觉（极短文本 + 水印词）。"""
        text = str(payload.get("text") or "").strip()
        words = payload.get("words") or []
        segments = payload.get("segments") or []
        lower = text.casefold()
        # 已知幻觉：把整段口播压成水印/「字幕」二字
        if "authorwave" in lower or "υπότιτλοι" in lower or "υποτιτλοι" in lower:
            if len(text) < 80:
                return f"疑似幻觉文本：{text[:60]}"
        # 时长 > 12s 却文本/词数明显过少（turbo 幻觉或 large-v3 偶发截断）
        if duration >= 12:
            min_chars = max(60, int(duration * 2.5))
            min_words = max(8, int(duration * 0.6))
            if len(text) < min_chars or (words and len(words) < min_words and len(segments) <= 2):
                return f"结果过稀（{len(text)} 字/{len(words)} 词/{duration:.0f}s）：{text[:60]}"
        return ""

    def _groq_transcribe_file(self, chunk: Path, key: str, model: str) -> dict:
        data = {
            "model": model,
            "response_format": "verbose_json",
            "timestamp_granularities[]": ["word", "segment"],
            "temperature": "0",
        }
        if self.language and self.language != "auto":
            data["language"] = self.language
        with chunk.open("rb") as handle:
            resp = requests.post(
                "https://api.groq.com/openai/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {key}"},
                data=data,
                files={"file": (chunk.name, handle, "audio/wav")},
                timeout=900,
            )
        if resp.status_code >= 300:
            raise ApiFailure(response_error(resp), resp.status_code)
        return resp.json()

    def _groq(self, audio: Path, key: str, temp: Path):
        chunks_dir = temp / "chunks"
        chunks_dir.mkdir(parents=True, exist_ok=True)
        pattern = chunks_dir / "chunk_%03d.wav"
        creation = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        segment_seconds = 90
        # 统一抽 mono 16k 再分段，体积更小、Groq 更稳
        # 注意：不要用 0:a:0?（可选映射）——无音轨时会生成 0 流输出却仍 return 0，导致「无 stream」
        prepared = temp / "groq_source.wav"
        if not prepared.exists() or prepared.stat().st_size < 1000:
            prep = subprocess.run(
                [self.ffmpeg_path, "-y", "-i", str(audio), "-map", "0:a:0", "-vn",
                 "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(prepared)],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                creationflags=creation, text=True, encoding="utf-8", errors="replace",
            )
            if prep.returncode != 0 or not prepared.exists() or prepared.stat().st_size < 1000:
                err = (prep.stderr or "")[-600:]
                if "Stream map" in err or "does not contain" in err or "matches no streams" in err:
                    raise RuntimeError(
                        "媒体没有可识别的音轨（常见于 TikTok/抖音只下到静音画面）。"
                        "请更新 yt-dlp 后重试链接，或改用含声音的本地视频。\n" + err
                    )
                # 其它错误：回退对源文件分段（仍要求有音轨）
                prepared = audio
        chunks = sorted(chunks_dir.glob("chunk_*.wav"), key=lambda path: rename_natural_key(path.name))
        if not chunks:
            self.log.emit("正在把长音频切成 90 秒无损识别分段 …")
            cmd = [self.ffmpeg_path, "-y", "-i", str(prepared), "-map", "0:a:0", "-vn", "-f", "segment",
                   "-segment_time", str(segment_seconds), "-reset_timestamps", "1",
                   "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(pattern)]
            result = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                    creationflags=creation, text=True, encoding="utf-8", errors="replace")
            chunks = sorted(chunks_dir.glob("chunk_*.wav"), key=lambda path: rename_natural_key(path.name))
            if result.returncode != 0 or not chunks:
                err = (result.stderr or "")[-800:]
                if "does not contain any stream" in err or "matches no streams" in err:
                    raise RuntimeError(
                        "无法切分识别音频：文件没有音轨。"
                        "TikTok 等链接请确保下载含声音的版本，或使用本地 mp4。\n" + err
                    )
                raise RuntimeError("Groq 长音频分段失败。\n" + err)
        else:
            self.log.emit(f"断点续接：复用 {len(chunks)} 个长音频分段。")
        cache_path = temp / "groq_chunk_results.json"
        # 模型名变了就丢弃旧缓存（避免 turbo 幻觉结果永久复用）
        cache = read_json_file(cache_path, {})
        if cache.get("_model") != self.model:
            cache = {"_model": self.model}
            atomic_write_json(cache_path, cache)
        all_segments, all_words, texts, raw_items, offset = [], [], [], [], 0.0
        primary_model = self.model or DEFAULT_MODELS["Groq"]
        fallback_models = []
        if "turbo" in primary_model.casefold():
            fallback_models.append("whisper-large-v3")
        elif primary_model != "whisper-large-v3":
            fallback_models.append("whisper-large-v3")
        for number, chunk in enumerate(chunks, 1):
            payload = cache.get(chunk.name)
            try:
                chunk_dur = float(video_duration(self.ffmpeg_path, str(chunk)))
            except Exception:
                chunk_dur = float(segment_seconds)
            if payload and not self._groq_payload_is_suspicious(payload, chunk_dur):
                self.log.emit(f"Groq 断点续接：跳过已完成分段 {number}/{len(chunks)}")
            else:
                if payload:
                    self.log.emit(f"Groq 分段 {number} 缓存结果异常，重新请求 …")
                used_model = primary_model
                self.log.emit(f"Groq 转写分段 {number}/{len(chunks)}（模型 {used_model}）…")
                payload = self._groq_transcribe_file(chunk, key, used_model)
                reason = self._groq_payload_is_suspicious(payload, chunk_dur)
                if reason:
                    for alt in fallback_models:
                        if alt == used_model:
                            continue
                        self.log.emit(f"Groq {used_model} 结果异常（{reason}），改用 {alt} 重试 …")
                        try:
                            payload = self._groq_transcribe_file(chunk, key, alt)
                            used_model = alt
                            if not self._groq_payload_is_suspicious(payload, chunk_dur):
                                break
                        except Exception as exc:
                            self.log.emit(f"Groq 备用模型 {alt} 失败：{exc}")
                    still_bad = self._groq_payload_is_suspicious(payload, chunk_dur)
                    if still_bad:
                        raise RuntimeError(
                            f"Groq 识别结果异常（{still_bad}）。"
                            "建议：设置里把 Groq 模型改为 whisper-large-v3，或改用本地 Whisper。"
                        )
                cache[chunk.name] = payload
                cache["_model"] = used_model
                atomic_write_json(cache_path, cache)
            raw_items.append(payload)
            text = payload.get("text", "").strip()
            texts.append(text)
            local = payload.get("segments") or []
            for seg in local:
                all_segments.append({"start": float(seg.get("start", 0)) + offset,
                                     "end": float(seg.get("end", 0)) + offset,
                                     "text": seg.get("text", "")})
            for word in payload.get("words") or []:
                word_text = str(word.get("word") or word.get("text") or "").strip()
                if word_text:
                    all_words.append({"start": float(word.get("start", 0)) + offset,
                                      "end": float(word.get("end", 0)) + offset, "text": word_text})
            offset += chunk_dur
        if not all_segments:
            all_segments = [{"start": 0, "end": max(2, offset), "text": "\n".join(texts)}]
        lang = None if not self.language or self.language == "auto" else self.language
        plain = "\n".join(normalize_subtitle_text(t, language=lang) for t in texts if t)
        return segments_to_srt(all_segments, language=lang), plain or "\n".join(texts), {
            "provider": "Groq", "chunks": raw_items, "words": all_words, "language": lang}

    def _gemini(self, audio: Path, key: str):
        size = audio.stat().st_size
        mime = "audio/wav"
        headers = {
            "x-goog-api-key": key,
            "X-Goog-Upload-Protocol": "resumable",
            "X-Goog-Upload-Command": "start",
            "X-Goog-Upload-Header-Content-Length": str(size),
            "X-Goog-Upload-Header-Content-Type": mime,
            "Content-Type": "application/json",
        }
        start = requests.post("https://generativelanguage.googleapis.com/upload/v1beta/files",
                              headers=headers, json={"file": {"display_name": audio.name}}, timeout=60)
        if start.status_code >= 300:
            msg = response_error(start)
            if start.status_code == 429:
                msg = "Gemini 配额已用尽（429）。请到 Google AI Studio 检查额度/账单，或改用 Groq / 本地 Whisper。\n" + msg
            raise ApiFailure(msg, start.status_code)
        upload_url = start.headers.get("x-goog-upload-url")
        if not upload_url:
            raise ApiFailure("Gemini 未返回上传地址")
        self.log.emit("上传音频到 Gemini Files API …")
        with audio.open("rb") as handle:
            uploaded = requests.post(upload_url, headers={
                "Content-Length": str(size), "X-Goog-Upload-Offset": "0",
                "X-Goog-Upload-Command": "upload, finalize",
            }, data=handle, timeout=900)
        if uploaded.status_code >= 300:
            msg = response_error(uploaded)
            if uploaded.status_code == 429:
                msg = "Gemini 上传配额已用尽（429）。请检查额度或改用其它识别服务。\n" + msg
            raise ApiFailure(msg, uploaded.status_code)
        file_info = uploaded.json().get("file", {})
        file_uri = file_info.get("uri")
        file_name = file_info.get("name")
        if not file_uri:
            raise ApiFailure("Gemini 文件上传响应缺少 URI")
        # 等待文件进入 ACTIVE，避免刚上传就 generate 失败
        for _ in range(30):
            if self.cancelled:
                raise RuntimeError("任务已取消")
            state = str(file_info.get("state") or "").upper()
            if state in ("ACTIVE", "STATE_ACTIVE", ""):
                if state.startswith("ACTIVE") or state == "STATE_ACTIVE":
                    break
                # 部分响应无 state 字段，直接继续
                if not state:
                    break
            if state in ("FAILED", "STATE_FAILED"):
                raise ApiFailure(f"Gemini 文件处理失败：{file_info}")
            time.sleep(1)
            try:
                meta = requests.get(
                    f"https://generativelanguage.googleapis.com/v1beta/{file_name}",
                    headers={"x-goog-api-key": key}, timeout=30,
                )
                if meta.status_code < 300:
                    file_info = meta.json()
                    file_uri = file_info.get("uri") or file_uri
                    if str(file_info.get("state") or "").upper() in ("ACTIVE", "STATE_ACTIVE"):
                        break
            except Exception:
                break
        prompt = (
            "请准确转写这段音频，并只输出标准 SRT 字幕（不要 Markdown、不要解释）。\n"
            "硬性要求：\n"
            "1) 保留原语言；\n"
            "2) 必须是【词级】时间轴：每一条 SRT 只写一个词（或一个很短的语气词），"
            "时间码必须对齐该词真实开口与收口，禁止把整句塞进一条；\n"
            "3) 格式：序号 / HH:MM:SS,mmm --> HH:MM:SS,mmm / 正文；\n"
            "4) 词与词之间时间连续、不重叠；静音处不要编造词。\n"
            "若无法做到词级，也请尽量短切（每条≤2个词），时间码仍须贴合口播节奏。"
        )
        if self.language and self.language != "auto":
            prompt += f"\n音频语言代码提示：{self.language}。"
        body = {"contents": [{"parts": [{"text": prompt}, {"file_data": {
            "mime_type": mime, "file_uri": file_uri}}]}],
                "generationConfig": {"temperature": 0.1}}
        try:
            model_name = self.model or DEFAULT_MODELS["Gemini"]
            # 旧模型名热迁移（即使用户配置未重启）
            model_name = _MODEL_MIGRATIONS.get("Gemini", {}).get(model_name, model_name)
            fallbacks = [model_name]
            for alt in ("gemini-3.6-flash", "gemini-3.8-flash", "gemini-2.5-flash"):
                if alt not in fallbacks:
                    fallbacks.append(alt)
            payload = None
            used_model = model_name
            last_err = ""
            for try_model in fallbacks:
                self.log.emit(f"Gemini 正在生成带时间码字幕（{try_model}）…")
                resp = requests.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{try_model}:generateContent",
                    headers={"x-goog-api-key": key, "Content-Type": "application/json"},
                    json=body, timeout=1200,
                )
                if resp.status_code < 300:
                    payload = resp.json()
                    used_model = try_model
                    break
                msg = response_error(resp)
                last_err = msg
                # 模型关停 / 不存在：换下一个
                if resp.status_code in (404, 400) and (
                    "not found" in msg.casefold()
                    or "not supported" in msg.casefold()
                    or "is not found" in msg.casefold()
                    or "no longer available" in msg.casefold()
                ):
                    self.log.emit(f"Gemini 模型「{try_model}」不可用，尝试下一型号…")
                    continue
                if resp.status_code == 429:
                    msg = (
                        "Gemini 配额已用尽（429）。免费额度用完后需开通计费，"
                        "或暂时改用「本地 Whisper」/「Groq」。\n" + msg
                    )
                raise ApiFailure(msg, resp.status_code)
            if payload is None:
                raise ApiFailure(
                    f"Gemini 模型均不可用（已试 {', '.join(fallbacks)}）。\n{last_err}",
                    404,
                )
            text = "\n".join(part.get("text", "") for cand in payload.get("candidates", [])
                              for part in cand.get("content", {}).get("parts", []))
            lang = None if not self.language or self.language == "auto" else self.language
            srt = clean_model_srt(text, language=lang)
            plain = re.sub(r"(?m)^\d+\s*$|^\d{2}:\d{2}:\d{2},\d{3} --> .*?$", "", srt)
            plain = re.sub(r"\n{2,}", "\n", plain).strip()
            return srt, plain, {
                "provider": "Gemini", "response": payload, "language": lang, "model": used_model,
            }
        finally:
            if file_name:
                try:
                    requests.delete(f"https://generativelanguage.googleapis.com/v1beta/{file_name}",
                                    headers={"x-goog-api-key": key}, timeout=20)
                except Exception:
                    pass

    def _elevenlabs(self, audio: Path, key: str):
        model_id = str(self.model or DEFAULT_MODELS["ElevenLabs"] or "scribe_v2").strip()
        model_id = _MODEL_MIGRATIONS.get("ElevenLabs", {}).get(model_id, model_id) or "scribe_v2"
        if not model_id.startswith("scribe"):
            model_id = "scribe_v2"
        data = {"model_id": model_id, "tag_audio_events": "true",
                "diarize": "true" if self.diarize else "false"}
        if self.language and self.language != "auto":
            data["language_code"] = self.language
        self.log.emit(f"ElevenLabs Scribe 正在转写（{model_id}）…")
        with audio.open("rb") as handle:
            resp = requests.post("https://api.elevenlabs.io/v1/speech-to-text",
                                 headers={"xi-api-key": key}, data=data,
                                 files={"file": (audio.name, handle, "audio/wav")}, timeout=1800)
        if resp.status_code >= 300:
            # scribe_v1 已退役时自动改 scribe_v2 再试一次
            err = response_error(resp)
            if model_id != "scribe_v2" and resp.status_code in (400, 404, 422):
                self.log.emit(f"ElevenLabs 模型「{model_id}」失败，改用 scribe_v2 重试…")
                data["model_id"] = "scribe_v2"
                with audio.open("rb") as handle2:
                    resp = requests.post(
                        "https://api.elevenlabs.io/v1/speech-to-text",
                        headers={"xi-api-key": key}, data=data,
                        files={"file": (audio.name, handle2, "audio/wav")}, timeout=1800,
                    )
            if resp.status_code >= 300:
                raise ApiFailure(response_error(resp) if resp.status_code >= 300 else err, resp.status_code)
        payload = resp.json()
        segments = words_to_segments(payload.get("words", []))
        text = payload.get("text", "").strip()
        if not segments:
            segments = [{"start": 0, "end": 5, "text": text}]
        lang = None if not self.language or self.language == "auto" else self.language
        plain = normalize_subtitle_text(text, language=lang)
        return segments_to_srt(segments, language=lang), plain, {
            "provider": "ElevenLabs", "response": payload, "language": lang}

    def _gladia(self, audio: Path, key: str):
        headers = {"x-gladia-key": key}
        self.log.emit("上传音频到 Gladia …")
        with audio.open("rb") as handle:
            uploaded = requests.post("https://api.gladia.io/v2/upload", headers=headers,
                                     files={"audio": (audio.name, handle, "audio/wav")}, timeout=1800)
        if uploaded.status_code >= 300:
            raise ApiFailure(response_error(uploaded), uploaded.status_code)
        audio_url = uploaded.json().get("audio_url")
        body = {"audio_url": audio_url, "subtitles": True,
                "subtitles_config": {"formats": ["srt"]}, "diarization": self.diarize}
        if self.language and self.language != "auto":
            body["language_config"] = {"languages": [self.language], "code_switching": False}
        init = requests.post("https://api.gladia.io/v2/pre-recorded",
                             headers={**headers, "Content-Type": "application/json"}, json=body, timeout=60)
        if init.status_code >= 300:
            raise ApiFailure(response_error(init), init.status_code)
        job = init.json()
        job_id = job.get("id")
        # 新版返回 result_url（常为 /v2/transcription/{id}）；旧轮询 /v2/pre-recorded/{id} 会接口对不上
        result_url = str(job.get("result_url") or "").strip()
        if not result_url and job_id:
            result_url = f"https://api.gladia.io/v2/transcription/{job_id}"
        self.log.emit(f"Gladia 任务已提交：{job_id}")
        for _ in range(720):
            if self.cancelled:
                raise RuntimeError("任务已取消")
            result = requests.get(result_url, headers=headers, timeout=30)
            if result.status_code == 404 and job_id and "/transcription/" in result_url:
                # 兼容旧账号仍用 pre-recorded 路径
                result_url = f"https://api.gladia.io/v2/pre-recorded/{job_id}"
                result = requests.get(result_url, headers=headers, timeout=30)
            if result.status_code >= 300:
                raise ApiFailure(response_error(result), result.status_code)
            payload = result.json()
            status = payload.get("status")
            if status == "done":
                data = payload.get("result") or {}
                transcription = data.get("transcription") or {}
                plain = transcription.get("full_transcript", "") if isinstance(transcription, dict) else str(transcription)
                subtitles = data.get("subtitles") or []
                srt = ""
                if isinstance(subtitles, list):
                    for sub in subtitles:
                        if isinstance(sub, dict) and sub.get("format") == "srt":
                            srt = sub.get("subtitles") or sub.get("content") or ""
                            break
                        if isinstance(sub, str) and "-->" in sub:
                            srt = sub
                            break
                elif isinstance(subtitles, dict):
                    srt = subtitles.get("srt", "")
                lang = None if not self.language or self.language == "auto" else self.language
                if not srt:
                    utterances = transcription.get("utterances", []) if isinstance(transcription, dict) else []
                    srt = (segments_to_srt(utterances, language=lang) if utterances
                           else clean_model_srt(plain, language=lang))
                return clean_model_srt(srt, language=lang), plain, {
                    "provider": "Gladia", "response": payload, "language": lang}
            if status == "error":
                raise ApiFailure(json.dumps(payload, ensure_ascii=False)[:800])
            time.sleep(5)
        raise ApiFailure("Gladia 任务等待超时")


def extract_google_id(value: str) -> str:
    match = re.search(r"[-\w]{20,}", (value or "").strip())
    return match.group(0) if match else ""


def column_to_index(column: str) -> int:
    column = (column or "").strip().upper()
    if not re.fullmatch(r"[A-Z]{1,3}", column):
        raise ValueError(f"无效的表格列：{column or '空'}")
    result = 0
    for char in column: result = result * 26 + ord(char) - 64
    return result - 1


def index_to_column(index: int) -> str:
    value = index + 1; result = ""
    while value:
        value, remainder = divmod(value - 1, 26); result = chr(65 + remainder) + result
    return result


GOOGLE_SCOPES = ["https://www.googleapis.com/auth/drive", "https://www.googleapis.com/auth/spreadsheets"]


def load_google_credentials(config, interactive=False):
    json_path = Path(config.get("json_path", ""))
    if not json_path.is_file(): raise RuntimeError("Google 授权 JSON 文件不存在。")
    try: payload = json.loads(json_path.read_text(encoding="utf-8-sig"))
    except Exception as exc: raise RuntimeError(f"Google JSON 无法读取：{exc}") from exc
    if payload.get("type") == "service_account":
        required = [name for name in ("client_email", "token_uri", "private_key") if not payload.get(name)]
        if required:
            raise RuntimeError("服务账号 JSON 不完整，缺少：" + "、".join(required))
        from google.oauth2 import service_account
        credentials = service_account.Credentials.from_service_account_info(payload, scopes=GOOGLE_SCOPES)
        return credentials, f"服务账号：{payload.get('client_email', '')}"
    client = payload.get("installed") or payload.get("web")
    if not client:
        raise RuntimeError("无法识别该 JSON。请选择服务账号密钥，或 OAuth 桌面客户端 JSON。")
    if not client.get("client_id") or not client.get("client_secret") or not client.get("token_uri"):
        raise RuntimeError("OAuth 客户端 JSON 不完整，缺少 client_id、client_secret 或 token_uri。")
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as exc:
        raise RuntimeError("缺少 Google OAuth 授权组件，请到“设置与组件”一键安装。") from exc
    token_path = config_dir() / "google_oauth_token.json"
    credentials = None
    if token_path.is_file():
        try: credentials = Credentials.from_authorized_user_file(str(token_path), GOOGLE_SCOPES)
        except Exception: credentials = None
    if credentials and credentials.expired and credentials.refresh_token:
        try: credentials.refresh(Request())
        except Exception: credentials = None
    if not credentials or not credentials.valid:
        if not interactive:
            raise RuntimeError("OAuth 尚未授权，请打开 Google 配置并点击“授权/检查权限”。")
        flow = InstalledAppFlow.from_client_config(payload, GOOGLE_SCOPES)
        credentials = flow.run_local_server(port=0, open_browser=True,
                                            success_message="视频工具合集 Google 授权成功，可以关闭此页面。")
        token_path.write_text(credentials.to_json(), encoding="utf-8")
    return credentials, "OAuth 用户授权"


def test_google_authorization(config, interactive=True):
    try:
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise RuntimeError("缺少 Google API 组件，请到“设置与组件”一键安装。") from exc
    credentials, identity = load_google_credentials(config, interactive=interactive)
    drive = build("drive", "v3", credentials=credentials, cache_discovery=False)
    drive.about().get(fields="user").execute()
    parent_id = extract_google_id(config.get("parent_folder", ""))
    if parent_id:
        drive.files().get(fileId=parent_id, fields="id,name", supportsAllDrives=True).execute()
    return identity


class SheetWritePendingError(RuntimeError):
    def __init__(self, folder_url, uploaded, cause):
        super().__init__(f"视频已上传成功，但写入表格失败：{cause}")
        self.folder_url=folder_url
        self.uploaded=[{**dict(item),"path":str(item.get("path",""))} for item in uploaded]


class GoogleCloudSync:
    def __init__(self, config, log_callback=None, cancel_callback=None):
        self.config = config
        self.log = log_callback or (lambda text: None)
        self.cancelled = cancel_callback or (lambda: False)

    def _services(self):
        try:
            from googleapiclient.discovery import build
        except ImportError as exc:
            raise RuntimeError("缺少 Google 云同步组件，请到“设置与组件”一键安装。") from exc
        credentials, identity = load_google_credentials(self.config, interactive=False)
        drive = build("drive", "v3", credentials=credentials, cache_discovery=False)
        sheets = build("sheets", "v4", credentials=credentials, cache_discovery=False)
        return drive, sheets, getattr(credentials, "service_account_email", "") or identity

    def _find_or_create_folder(self, drive, name, parent_id):
        escaped = name.replace("'", "\\'")
        query = (f"name = '{escaped}' and mimeType = 'application/vnd.google-apps.folder' "
                 f"and '{parent_id}' in parents and trashed = false")
        response = drive.files().list(q=query, spaces="drive", fields="files(id,name)",
                                      pageSize=10, supportsAllDrives=True,
                                      includeItemsFromAllDrives=True).execute()
        items = response.get("files", [])
        if items: return items[0]["id"]
        body = {"name": name, "mimeType": "application/vnd.google-apps.folder", "parents": [parent_id]}
        return drive.files().create(body=body, fields="id", supportsAllDrives=True).execute()["id"]

    def _upload_file(self, drive, path, parent_id):
        from googleapiclient.http import MediaFileUpload
        media = MediaFileUpload(str(path), mimetype=mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                                chunksize=8 * 1024 * 1024, resumable=True)
        escaped = path.name.replace("'", "\\'")
        query = f"name = '{escaped}' and '{parent_id}' in parents and trashed = false"
        found = drive.files().list(q=query, spaces="drive", fields="files(id,name,webViewLink)", pageSize=10,
                                   supportsAllDrives=True, includeItemsFromAllDrives=True).execute().get("files", [])
        if found:
            # 断点续传：同一个任务文件夹中已经存在该成品时直接复用云端文件，
            # 不重新传输，也不会生成重名副本。表格仍使用真实 Drive 链接去重。
            existing = dict(found[0]); existing["_reused"] = True
            return existing
        request = drive.files().create(body={"name": path.name, "parents": [parent_id]}, media_body=media,
                                       fields="id,name,webViewLink", supportsAllDrives=True)
        response = None
        while response is None:
            if self.cancelled(): raise RuntimeError("云端上传已停止；已上传的文件会保留，可稍后继续上传。")
            status, response = request.next_chunk()
            if status: self.log(f"上传 {path.name}：{round(status.progress() * 100)}%")
        return response

    def _parse_static_columns(self):
        mappings = {}
        for line in self.config.get("static_columns", "").splitlines():
            if "=" not in line: continue
            column, value = line.split("=", 1); column = column.strip().upper()
            if column: mappings[column] = value.strip()
        return mappings

    def _write_sheet(self, sheets, uploaded, folder_url):
        spreadsheet_id = extract_google_id(self.config.get("spreadsheet_id", ""))
        sheet_name = self.config.get("sheet_name", "").strip()
        if not spreadsheet_id or not sheet_name:
            raise RuntimeError("已开启表格写入，但表格 ID 或 Sheet 名称为空。")
        insert_row = max(1, int(self.config.get("insert_row", 4)))
        mappings = [dict(item) for item in self.config.get("sheet_mappings", DEFAULT_SHEET_MAPPINGS)
                    if str(item.get("column", "")).strip()]
        mappings += [{"field": item.get("field", "下拉字段"), "column": item.get("column", ""),
                      "source": "static", "value": item.get("selected", "")}
                     for item in self.config.get("variable_fields", []) if str(item.get("column", "")).strip()]
        if not mappings: raise RuntimeError("表格列映射为空。")
        for item in mappings: item["column"] = item["column"].strip().upper()
        max_index = max(column_to_index(item["column"]) for item in mappings)
        file_mapping = next((item for item in mappings if item.get("source") == "file"), None)
        if not file_mapping: raise RuntimeError("列映射中必须保留“文件名/链接”自动字段。")
        file_col = file_mapping["column"]
        folder_mapping = next((item for item in mappings if item.get("source") == "folder"), None)
        quoted_sheet = "'" + sheet_name.replace("'", "''") + "'"
        existing_response = sheets.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id, range=f"{quoted_sheet}!{file_col}{insert_row}:{file_col}",
            valueRenderOption="FORMULA").execute()
        # 文件链接是唯一值；同名文件可以存在，但同一个云端链接不会重复写入。
        existing = {}
        for offset, values in enumerate(existing_response.get("values", [])):
            value = str(values[0]) if values else ""
            match = re.search(r'HYPERLINK\(\s*"([^"]+)"\s*[,;]\s*"[^"]*"', value, re.I)
            link = match.group(1) if match else (value if value.lower().startswith(("http://","https://")) else "")
            if link: existing[link.strip().casefold()] = insert_row + offset

        # Google Sheets 的 B 列（或用户配置的文件链接列）也可能是富文本超链接，
        # Values API 只返回显示文字。额外读取 CellData，提取 hyperlink / textFormatRuns
        # 中的真实 Drive URL，确保不会因为同名文件或显示文字而误判。
        try:
            rich_response = sheets.spreadsheets().get(
                spreadsheetId=spreadsheet_id,
                ranges=[f"{quoted_sheet}!{file_col}{insert_row}:{file_col}"],
                includeGridData=True,
                fields="sheets(data(rowData(values)))",
            ).execute()
            for sheet in rich_response.get("sheets", []):
                for data in sheet.get("data", []):
                    for row in data.get("rowData", []):
                        for cell in row.get("values", []):
                            candidates = [str(cell.get("hyperlink", ""))]
                            effective = cell.get("effectiveValue", {})
                            candidates.append(str(effective.get("formulaValue", "")))
                            candidates.append(str(cell.get("formattedValue", "")))
                            candidates.extend(
                                str(run.get("format", {}).get("link", {}).get("uri", ""))
                                for run in cell.get("textFormatRuns", [])
                            )
                            for value in candidates:
                                match = re.search(r'HYPERLINK\(\s*"([^"]+)"', value, re.I)
                                link = match.group(1) if match else value.strip()
                                if link.lower().startswith(("http://", "https://")):
                                    existing[link.casefold()] = True
        except Exception as exc:
            self.log(f"读取 {file_col} 列富文本链接失败，已继续使用公式链接检查：{exc}")

        new_rows, update_data = [], []
        for item in uploaded:
            path, url = item["path"], item["url"]
            values = [""] * (max_index + 1)
            context = {"date": datetime.now().strftime("%Y-%m-%d"), "folder_url": folder_url,
                       "file_name": path.name, "file_url": url, "zh": item.get("chinese", ""),
                       "original": item.get("original", ""), "language": item.get("language", "")}
            for mapping in mappings:
                column_index = column_to_index(mapping["column"]); source = mapping.get("source", "static")
                if source == "date": cell_value = context["date"]
                elif source == "file": cell_value = f'=HYPERLINK("{url}","{path.name.replace(chr(34), chr(34)*2)}")'
                elif source == "chinese": cell_value = context["zh"]
                elif source == "original": cell_value = context["original"]
                elif source == "language": cell_value = context["language"]
                elif source == "folder": cell_value = folder_url
                else:
                    template = str(mapping.get("value", ""))
                    try: cell_value = template.format(**context)
                    except (KeyError, ValueError): cell_value = template
                values[column_index] = cell_value
            key = url.strip().casefold()
            if key in existing:
                self.log(f"表格已存在该文件链接，跳过：{path.name}")
            else:
                new_rows.append(values)
        if update_data:
            sheets.spreadsheets().values().batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={"valueInputOption": "USER_ENTERED", "data": update_data}).execute()
        if new_rows:
            metadata = sheets.spreadsheets().get(spreadsheetId=spreadsheet_id, fields="sheets.properties").execute()
            target = next((item["properties"] for item in metadata.get("sheets", [])
                           if item["properties"]["title"] == sheet_name), None)
            if not target: raise RuntimeError(f"表格中没有找到 Sheet：{sheet_name}")
            sheets.spreadsheets().batchUpdate(spreadsheetId=spreadsheet_id, body={"requests": [{
                "insertDimension": {"range": {"sheetId": target["sheetId"], "dimension": "ROWS",
                                                "startIndex": insert_row - 1,
                                                "endIndex": insert_row - 1 + len(new_rows)},
                                    "inheritFromBefore": False}}]}).execute()
            end_col = index_to_column(max_index)
            sheets.spreadsheets().values().update(
                spreadsheetId=spreadsheet_id,
                range=f"{quoted_sheet}!A{insert_row}:{end_col}{insert_row + len(new_rows) - 1}",
                valueInputOption="USER_ENTERED", body={"values": new_rows}).execute()
        return len(new_rows), len(uploaded) - len(new_rows)

    def run(self, final_dir: Path, records, source_paths, selected_files=None):
        drive, sheets, email = self._services()
        parent_id = extract_google_id(self.config.get("parent_folder", ""))
        if not parent_id: raise RuntimeError("Google Drive 父文件夹 ID 或链接无效。")
        self.log(f"Google JSON 授权成功：{email}")
        date_folder = self._find_or_create_folder(drive, datetime.now().strftime("%Y-%m-%d"), parent_id)
        if self.config.get("folder_mode") == "自定义名称":
            task_name = self.config.get("custom_folder_name", "").strip()
        else:
            first = Path(source_paths[0]).stem if source_paths else final_dir.name
            task_name = first if len(source_paths) <= 1 else f"{first}_等{len(source_paths)}个视频"
        task_name = re.sub(r'[\\/:*?"<>|]+', "_", task_name).strip() or final_dir.name
        task_folder = self._find_or_create_folder(drive, task_name, date_folder)
        if self.config.get("public_link"):
            drive.permissions().create(fileId=task_folder, body={"type": "anyone", "role": "reader"},
                                       supportsAllDrives=True).execute()
        folder_url = f"https://drive.google.com/drive/folders/{task_folder}"
        record_map = {Path(item["path"]).name: item for item in records}
        uploaded = []; reused_count = 0; uploaded_count = 0
        final_files = sorted((Path(path) for path in selected_files), key=lambda path: rename_natural_key(path.name)) if selected_files else sorted((path for path in final_dir.iterdir() if path.is_file()),
                             key=lambda path: rename_natural_key(path.name))
        self.log(f"只上传重命名成品：共 {len(final_files)} 个文件")
        for number, path in enumerate(final_files, 1):
            if self.cancelled(): raise RuntimeError("云端上传已停止；可以稍后点击继续上传。")
            response = self._upload_file(drive, path, task_folder)
            if response.get("_reused"):
                reused_count += 1
                self.log(f"云端已存在，断点跳过上传 {number}/{len(final_files)}：{path.name}")
            else:
                uploaded_count += 1
            source_record = record_map.get(path.name, {})
            uploaded.append({"path": path, "url": response.get("webViewLink") or
                             f"https://drive.google.com/file/d/{response['id']}/view",
                             "chinese": source_record.get("chinese", ""),
                             "original": source_record.get("original", "")})
            if not response.get("_reused"):
                self.log(f"云端上传完成 {number}/{len(final_files)}：{path.name}")
        sheet_note = "未开启表格写入"
        if self.config.get("write_sheet"):
            try:
                added, updated = self._write_sheet(sheets, uploaded, folder_url)
                sheet_note = f"表格新增 {added} 行，跳过已存在链接 {updated} 行"
            except Exception as exc:
                raise SheetWritePendingError(folder_url, uploaded, exc) from exc
        return folder_url, f"新上传 {uploaded_count} 个，复用云端已有 {reused_count} 个；{sheet_note}"

    def write_sheet_only(self, uploaded, folder_url):
        _drive, sheets, email = self._services()
        self.log(f"复用已上传文件，不重新上传视频（{email}）")
        normalized=[]
        for item in uploaded:
            value=dict(item); value["path"]=Path(value.get("path","")); normalized.append(value)
        added, skipped=self._write_sheet(sheets,normalized,folder_url)
        return f"继续填表完成：新增 {added} 行，跳过已存在链接 {skipped} 行"


class PipelineWorker(QObject):
    log = Signal(str)
    progress = Signal(int)
    result_ready = Signal(str, str, str, str)
    titles_ready = Signal(str, list)
    cloud_ready = Signal(str, str)
    cloud_failed = Signal(str, str)
    cloud_sheet_pending = Signal(str, object, str)
    finished = Signal(bool, str)

    def __init__(self, store, sources, output, threshold, provider, model, language,
                 ffmpeg, prefix, date_text, suffix, start_index, padding, cloud_config=None,
                 resume_existing=True, extras=None):
        super().__init__()
        self.store = store; self.sources = sources; self.output = Path(output)
        self.threshold = threshold; self.provider = provider; self.model = model
        self.language = language; self.ffmpeg = ffmpeg; self.prefix = prefix
        self.date_text = date_text; self.suffix = suffix
        self.start_index = start_index; self.padding = padding; self.cancelled = False
        self.cloud_config = cloud_config or {}
        self.resume_existing = resume_existing
        self.extras = extras or {}  # bgm_path, bgm_volume, watermark_path, wm_width, wm_opacity
        self.state = {}
        self.checkpoint_path = None

    def cancel(self):
        self.cancelled = True

    def _polish_finals(self, final_dir: Path, final_records: list):
        """后处理与 Reels 一致：9:16 全屏水印；声音=仅原声 或 原声+BGM（支持曲库随机截取）。"""
        audio_mode = str(self.extras.get("audio_mode") or "仅视频原声")
        bgm_root = Path(str(self.extras.get("bgm_path") or ""))
        wm = Path(str(self.extras.get("watermark_path") or ""))
        want_bgm = "背景" in audio_mode or "BGM" in audio_mode.upper()
        use_wm = bool(self.extras.get("watermark_enabled")) and wm.is_file()
        randomize_bgm = bool(self.extras.get("bgm_random", True))
        # 解析曲库：单文件 / 文件夹
        try:
            from modules.dynamic_caption_page import find_bgm_file, random_bgm_start_ms, media_duration
        except Exception:
            find_bgm_file = None
            random_bgm_start_ms = None
            media_duration = None

        def resolve_bgm(index, video_path):
            if not want_bgm or not bgm_root:
                return None, 0
            if find_bgm_file is None:
                return (bgm_root if bgm_root.is_file() else None), 0
            picked = find_bgm_file(str(bgm_root), index - 1, video_path, randomize=randomize_bgm)
            if not picked or not Path(picked).is_file():
                return None, 0
            offset_ms = 0
            if randomize_bgm and random_bgm_start_ms is not None:
                offset_ms = int(random_bgm_start_ms(
                    self.ffmpeg, picked, video_path, index - 1, "pipeline_bgm"
                ))
            return Path(picked), offset_ms

        sample_bgm, _ = resolve_bgm(1, final_records[0]["path"] if final_records else "")
        use_bgm_any = want_bgm and (sample_bgm is not None or bgm_root.is_file() or bgm_root.is_dir())
        if not use_bgm_any and not use_wm:
            if want_bgm:
                self.log.emit("提醒：已选「原声＋背景音乐」但未找到可用 BGM 文件/文件夹，按仅原声输出。")
            return
        if want_bgm and not sample_bgm and not bgm_root.is_file():
            self.log.emit(f"提醒：BGM 路径无效：{bgm_root}，跳过混音（仍可烧水印）。")
            use_bgm_any = False
        if not use_bgm_any and not use_wm:
            return

        vol = max(0.01, min(1.0, float(self.extras.get("bgm_volume", 25)) / 100.0))
        # 默认 100% 不透明全屏覆盖
        wm_op = max(0.1, min(1.0, float(self.extras.get("wm_opacity", 100)) / 100.0))
        creation = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        for index, rec in enumerate(final_records, 1):
            if self.cancelled:
                raise RuntimeError("任务已取消")
            src = Path(rec["path"])
            if not src.is_file():
                continue
            bgm_file, bgm_offset_ms = resolve_bgm(index, str(src))
            use_bgm = bool(bgm_file and Path(bgm_file).is_file())
            if want_bgm and not use_bgm:
                self.log.emit(f"提醒：{src.name} 未匹配到 BGM，本条仅原声"
                              + ("＋水印" if use_wm else "") + "。")
            if not use_bgm and not use_wm:
                continue
            tmp = src.with_name(src.stem + "._polish_tmp.mp4")
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass
            cmd = [self.ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(src)]
            filter_parts = []
            next_in = 1
            v_label = "0:v"
            if use_wm:
                cmd += ["-i", str(wm)]
                # 固定 9:16 全屏覆盖：水印强制对齐主画面尺寸后 0,0 叠加
                filter_parts.append(
                    f"[0:v]setpts=PTS-STARTPTS[base];"
                    f"[{next_in}:v]format=rgba,colorchannelmixer=aa={wm_op:.3f}[wmraw];"
                    f"[wmraw][base]scale2ref=w=iw:h=ih[wm][base2];"
                    f"[base2][wm]overlay=0:0[vout]"
                )
                v_label = "vout"
                next_in += 1
            if use_bgm:
                # 随机起点截取后循环铺满视频时长（loop 在 -i 前；-ss 跟在 loop 后）
                cmd += ["-stream_loop", "-1"]
                if bgm_offset_ms > 0:
                    cmd += ["-ss", f"{bgm_offset_ms / 1000:.3f}"]
                cmd += ["-i", str(bgm_file)]
                filter_parts.append(
                    f"[0:a]aformat=channel_layouts=stereo,volume=1.0[a0];"
                    f"[{next_in}:a]aformat=channel_layouts=stereo,volume={vol:.3f},"
                    f"afade=t=in:st=0:d=0.3[a1];"
                    f"[a0][a1]amix=inputs=2:duration=first:dropout_transition=2,"
                    f"aformat=channel_layouts=stereo[aout]"
                )
                next_in += 1
            if filter_parts:
                cmd += ["-filter_complex", ";".join(filter_parts)]
            if use_wm:
                cmd += ["-map", f"[{v_label}]"]
            else:
                cmd += ["-map", "0:v:0"]
            if use_bgm:
                cmd += ["-map", "[aout]"]
            else:
                cmd += ["-map", "0:a?"]
            cmd += [
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                "-c:a", "aac", "-b:a", "192k", "-shortest",
                "-movflags", "+faststart", str(tmp),
            ]
            bgm_note = ""
            if use_bgm:
                bgm_note = f"｜BGM={Path(bgm_file).name}"
                if bgm_offset_ms > 0:
                    bgm_note += f"@{bgm_offset_ms/1000:.1f}s"
            self.log.emit(
                f"成品润色 {index}/{len(final_records)}：{src.name}"
                f"｜声音={audio_mode}"
                + bgm_note
                + (f"｜全屏水印{int(wm_op*100)}%" if use_wm else "")
            )
            result = subprocess.run(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                creationflags=creation, text=True, encoding="utf-8", errors="replace",
            )
            if result.returncode == 0 and tmp.is_file() and tmp.stat().st_size > 1024:
                try:
                    src.unlink(missing_ok=True)
                    tmp.replace(src)
                except OSError as exc:
                    self.log.emit(f"提醒：替换成品失败 {src.name}：{exc}")
            else:
                err = (result.stdout or "")[-500:]
                self.log.emit(f"提醒：润色跳过 {src.name}：{err or '编码失败'}")
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass

    def _pipeline_key(self):
        return stable_key({
            "version": 2,
            "sources": [source_signature(source) for source in self.sources],
            "threshold": self.threshold,
            "rename": {"prefix": self.prefix, "date": self.date_text, "suffix": self.suffix,
                       "start": self.start_index, "padding": self.padding},
        })

    def _open_run(self):
        pipeline_key = self._pipeline_key()
        if self.resume_existing and self.output.exists():
            candidates = sorted((path for path in self.output.glob("流水线_*") if path.is_dir()),
                                key=lambda path: path.stat().st_mtime, reverse=True)
            for candidate in candidates:
                checkpoint = candidate / "pipeline_checkpoint.json"
                state = read_json_file(checkpoint, {})
                if state.get("pipeline_key") == pipeline_key and state.get("status") != "completed":
                    self.state = state
                    self.checkpoint_path = checkpoint
                    self.log.emit(f"发现未完成任务，自动断点续接：{candidate.name}")
                    return candidate
        run_root = self.output / f"流水线_{datetime.now():%Y%m%d_%H%M%S}"
        suffix = 1
        while run_root.exists():
            run_root = self.output / f"流水线_{datetime.now():%Y%m%d_%H%M%S}_{suffix}"
            suffix += 1
        run_root.mkdir(parents=True, exist_ok=False)
        self.checkpoint_path = run_root / "pipeline_checkpoint.json"
        self.state = {
            "version": 2, "pipeline_key": pipeline_key, "status": "running",
            "sources": self.sources, "clips": [], "transcripts": {}, "renamed": {},
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
        self._save_checkpoint()
        return run_root

    def _save_checkpoint(self):
        self.state["updated_at"] = datetime.now().isoformat(timespec="seconds")
        atomic_write_json(self.checkpoint_path, self.state)

    def _cut_sources(self, clips_dir):
        try:
            from scenedetect import ContentDetector, detect
        except ImportError as exc:
            raise RuntimeError("缺少智能场景检测组件 scenedetect，请到“设置与组件”一键安装。") from exc
        clips = []
        creation = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        for source_number, source_text in enumerate(self.sources, 1):
            source = Path(source_text)
            if self.cancelled: raise RuntimeError("任务已取消")
            source_key = stable_key(source_signature(source_text))
            completed_names = self.state.setdefault("cut_completed", {}).get(source_key, [])
            completed_paths = [clips_dir / name for name in completed_names]
            if completed_paths and all(path.exists() for path in completed_paths):
                clips.extend(completed_paths)
                self.log.emit(f"断点续接：跳过已完成剪辑 {source_number}/{len(self.sources)}：{source.name}")
                self.progress.emit(round(source_number / max(1, len(self.sources)) * 30))
                continue
            for partial in clips_dir.glob(f"{source_number:03d}_*.mp4"):
                try:
                    partial.unlink()
                except OSError:
                    pass
            self.log.emit(f"分析画面切换：{source.name}")
            source_audio = probe_audio_layout(self.ffmpeg, str(source))
            scenes = detect(str(source), ContentDetector(threshold=self.threshold), show_progress=False)
            if not scenes:
                duration = video_duration(self.ffmpeg, str(source))
                scenes = [(0.0, duration)]
            for scene_number, (start, end) in enumerate(scenes, 1):
                start_seconds = start.get_seconds() if hasattr(start, "get_seconds") else float(start)
                end_seconds = end.get_seconds() if hasattr(end, "get_seconds") else float(end)
                duration = max(0.1, end_seconds - start_seconds)
                destination = clips_dir / f"{source_number:03d}_{scene_number:03d}.mp4"
                cmd = [self.ffmpeg, "-y", "-ss", f"{start_seconds:.3f}", "-t", f"{duration:.3f}",
                       "-i", str(source), "-map", "0", "-c", "copy", "-avoid_negative_ts", "make_zero", str(destination)]
                result = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                        creationflags=creation, text=True, encoding="utf-8", errors="replace")
                if result.returncode != 0:
                    raise RuntimeError(f"智能剪辑失败：{source.name}\n{result.stderr[-500:]}")
                output_audio = probe_audio_layout(self.ffmpeg, str(destination))
                if source_audio and output_audio != source_audio:
                    raise RuntimeError(
                        f"音轨校验失败：{source.name} 为 {source_audio[1]}，"
                        f"但片段 {destination.name} 为 {output_audio[1]}。已停止以避免改变声道。")
                clips.append(destination)
                audio_note = f"；音轨保持 {output_audio[1]} / {output_audio[0]}Hz" if output_audio else ""
                self.log.emit(f"已生成片段：{destination.name}{audio_note}")
            source_clips = [path.name for path in clips if path.name.startswith(f"{source_number:03d}_")]
            self.state["cut_completed"][source_key] = source_clips
            self._save_checkpoint()
            self.progress.emit(round(source_number / max(1, len(self.sources)) * 30))
        return clips

    def run(self):
        try:
            self.output.mkdir(parents=True, exist_ok=True)
            run_root = self._open_run()
            clips_dir = run_root / "01_智能剪辑片段"; clips_dir.mkdir(exist_ok=True)
            subtitles_dir = run_root / "02_字幕"; subtitles_dir.mkdir(exist_ok=True)
            saved_clips = [clips_dir / name for name in self.state.get("clips", [])]
            if saved_clips and all(path.exists() for path in saved_clips):
                clips = saved_clips
                self.log.emit(f"断点续接：剪辑阶段已完成，直接使用 {len(clips)} 个片段。")
                self.progress.emit(30)
            else:
                clips = self._cut_sources(clips_dir)
                self.state["clips"] = [path.name for path in clips]
                self.state["stage"] = "subtitles"
                self._save_checkpoint()
            if not clips: raise RuntimeError("没有生成任何视频片段。")
            transcriber = TranscribeWorker(self.store, self.provider, self.model, [],
                                           str(run_root / ".transcription_work"),
                                           self.language, False, self.ffmpeg, True)
            transcriber.log.connect(self.log.emit)
            captured = {}
            def capture(name, original, chinese, srt):
                captured.clear(); captured.update(name=name, original=original, chinese=chinese, srt=srt)
            transcriber.result_ready.connect(capture)
            titles, transcript_records = [], []
            for index, clip in enumerate(clips, 1):
                if self.cancelled: raise RuntimeError("任务已取消")
                cached = self.state.setdefault("transcripts", {}).get(clip.name)
                if cached:
                    captured.clear(); captured.update(name=clip.name, original=cached.get("original", ""),
                                                      chinese=cached.get("chinese", ""), srt=cached.get("srt", ""))
                    self.log.emit(f"断点续接：跳过已完成字幕 {index}/{len(clips)}：{clip.name}")
                else:
                    captured.clear(); self.log.emit(f"提取字幕 {index}/{len(clips)}：{clip.name}")
                    result = transcriber._process_one(str(clip))
                    if result and not captured:
                        captured.update(name=result["name"], original=result["original"],
                                        chinese=result["chinese"], srt=result["srt"])
                    if not captured: raise RuntimeError(f"未收到字幕结果：{clip.name}")
                    self.state["transcripts"][clip.name] = {
                        "original": captured["original"], "chinese": captured["chinese"],
                        "srt": captured["srt"], "provider": self.provider,
                    }
                    self._save_checkpoint()
                    transcriber._cleanup_source_work(str(clip))
                title = re.sub(r"\s+", " ", captured.get("chinese") or captured.get("original") or clip.stem).strip()
                titles.append(title)
                transcript_records.append({"clip_name": clip.name, "original": captured["original"],
                                           "chinese": captured["chinese"], "srt": captured["srt"]})
                (subtitles_dir / f"{clip.stem}.srt").write_text(captured["srt"], encoding="utf-8-sig")
                bilingual = f"【原文】\n{captured['original']}\n\n【简体中文】\n{captured['chinese']}"
                (subtitles_dir / f"{clip.stem}_中外文对照.txt").write_text(bilingual, encoding="utf-8-sig")
                self.result_ready.emit(clip.name, captured["original"], captured["chinese"], captured["srt"])
                self.progress.emit(30 + round(index / len(clips) * 60))

            self.state["stage"] = "rename"
            self._save_checkpoint()

            task = RenameTask(str(clips_dir), str(run_root), "03_重命名成品", self.prefix,
                              "\n".join(titles), self.date_text, self.suffix,
                              self.start_index, self.padding, True)
            final_dir = task.output_folder(); final_dir.mkdir(parents=True, exist_ok=True)
            ordered = sorted((path for path in clips_dir.iterdir() if path.is_file()),
                             key=lambda path: rename_natural_key(path.name))
            final_records = []
            for offset, source in enumerate(ordered):
                destination = final_dir / task.render_name(source.name, self.start_index + offset)
                saved_destination = self.state.setdefault("renamed", {}).get(source.name)
                if saved_destination and (final_dir / saved_destination).exists():
                    destination = final_dir / saved_destination
                    self.log.emit(f"断点续接：跳过已完成重命名 {offset + 1}/{len(ordered)}：{destination.name}")
                elif destination.exists():
                    self.log.emit(f"断点续接：检测到已复制成品，登记并跳过：{destination.name}")
                    self.state["renamed"][source.name] = destination.name
                    self._save_checkpoint()
                else:
                    shutil.copy2(source, destination)
                    self.state["renamed"][source.name] = destination.name
                    self._save_checkpoint()
                transcript = self.state["transcripts"].get(source.name, {})
                final_records.append({"path": str(destination), "original": transcript.get("original", ""),
                                      "chinese": transcript.get("chinese", "")})
            # 可选：成品叠加 BGM / 水印（不影响剪辑与字幕识别核心）
            if self.extras.get("bgm_path") or self.extras.get("watermark_path"):
                self.state["stage"] = "polish"
                self._save_checkpoint()
                self.progress.emit(88)
                self._polish_finals(final_dir, final_records)
            self.state["stage"] = "cloud" if self.cloud_config.get("enabled") else "completed"
            self._save_checkpoint()
            self.titles_ready.emit(str(clips_dir), titles)
            if self.cloud_config.get("enabled"):
                self.progress.emit(92); self.log.emit("开始 Google 云端同步（仅重命名成品）…")
                try:
                    folder_url, cloud_summary = GoogleCloudSync(
                        self.cloud_config, self.log.emit, lambda: self.cancelled).run(
                        final_dir, final_records, self.sources)
                    self.cloud_ready.emit(folder_url, cloud_summary)
                    self.state["status"] = "completed"
                    self.state["cloud_url"] = folder_url
                    self._save_checkpoint()
                except SheetWritePendingError as cloud_exc:
                    folder_url = cloud_exc.folder_url
                    cloud_summary = str(cloud_exc)
                    self.log.emit(cloud_summary); self.cloud_sheet_pending.emit(folder_url, cloud_exc.uploaded, str(cloud_exc))
                    self.state["status"] = "sheet_pending"; self.state["cloud_url"] = folder_url
                    self.state["pending_sheet_uploads"] = cloud_exc.uploaded; self.state["last_error"] = str(cloud_exc)
                    self._save_checkpoint()
                except Exception as cloud_exc:
                    folder_url = ""
                    cloud_summary = f"本地视频已全部处理；云同步失败：{cloud_exc}"
                    self.log.emit(cloud_summary); self.cloud_failed.emit(str(final_dir), str(cloud_exc))
                    self.state["status"] = "cloud_failed"
                    self.state["last_error"] = str(cloud_exc)
                    self._save_checkpoint()
            else:
                folder_url = ""; cloud_summary = "云端同步已关闭"
                self.state["status"] = "completed"
                self._save_checkpoint()
            self.progress.emit(100)
            message = f"流水线完成：生成 {len(clips)} 个片段和成品\n{final_dir}\n{cloud_summary}"
            if folder_url: message += f"\n{folder_url}"
            self.finished.emit(True, message)
        except Exception as exc:
            if self.checkpoint_path:
                try:
                    self.state["status"] = "failed"
                    self.state["last_error"] = str(exc)
                    self._save_checkpoint()
                except Exception:
                    pass
            self.finished.emit(False, str(exc))


class CloudUploadWorker(QObject):
    log = Signal(str)
    finished = Signal(bool, str, str)
    sheet_pending = Signal(str, object, str)

    def __init__(self, config, files, records=None, source_paths=None):
        super().__init__(); self.config = config; self.files = [Path(path) for path in files]
        self.records = records or []; self.source_paths = source_paths or [str(self.files[0])] if self.files else []
        self.cancelled = False

    def cancel(self): self.cancelled = True

    def run(self):
        try:
            if not self.files: raise RuntimeError("没有选择需要上传的成品文件。")
            folder_url, summary = GoogleCloudSync(
                self.config, self.log.emit, lambda: self.cancelled).run(
                self.files[0].parent, self.records, self.source_paths, self.files)
            self.finished.emit(True, folder_url, summary)
        except SheetWritePendingError as exc:
            self.sheet_pending.emit(exc.folder_url,exc.uploaded,str(exc))
            self.finished.emit(False,exc.folder_url,str(exc))
        except Exception as exc:
            self.finished.emit(False, "", str(exc))


class SheetFillWorker(QObject):
    log=Signal(str); finished=Signal(bool,str,str)

    def __init__(self,config,uploaded,folder_url):
        super().__init__(); self.config=config; self.uploaded=uploaded; self.folder_url=folder_url

    def run(self):
        try:
            summary=GoogleCloudSync(self.config,self.log.emit).write_sheet_only(self.uploaded,self.folder_url)
            self.finished.emit(True,self.folder_url,summary)
        except Exception as exc:
            self.finished.emit(False,self.folder_url,str(exc))


class ToolCard(QFrame):
    clicked = Signal(str)

    def __init__(self, icon_text, title, description, accent, path):
        super().__init__()
        self.path = path
        self.setObjectName("toolCard")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(6)
        header = QHBoxLayout()
        header.setSpacing(10)
        icon = QLabel(icon_text)
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon.setFixedSize(38, 38)
        icon.setStyleSheet(
            f"background:{accent}22;border:1px solid {accent}66;border-radius:9px;"
            f"color:{accent};font-size:20px;font-weight:800;")
        title_label = QLabel(title)
        title_label.setStyleSheet(f"font-size:18px;font-weight:800;color:{accent};")
        header.addWidget(icon)
        header.addWidget(title_label, 1)
        desc = QLabel(description)
        desc.setWordWrap(True)
        desc.setStyleSheet("color:#a9b8cb;line-height:1.45;")
        button = QPushButton("进入  →")
        button.clicked.connect(lambda: self.clicked.emit(self.path))
        layout.addLayout(header)
        layout.addWidget(desc)
        layout.addStretch()
        layout.addWidget(button)


class GoogleAuthWorker(QObject):
    finished = Signal(bool, str)

    def __init__(self, config, interactive=True):
        super().__init__(); self.config = config; self.interactive = interactive

    def run(self):
        try:
            self.finished.emit(True, test_google_authorization(self.config, interactive=self.interactive))
        except Exception as exc:
            self.finished.emit(False, str(exc))


class GoogleSheetReadWorker(QObject):
    """读取工作表名称，或按配置表列读取去重后的下拉选项。"""
    finished = Signal(bool, object, str)

    def __init__(self, config, mode):
        super().__init__(); self.config = config; self.mode = mode

    def run(self):
        try:
            from googleapiclient.discovery import build
            credentials, identity = load_google_credentials(self.config, interactive=False)
            service = build("sheets", "v4", credentials=credentials, cache_discovery=False)
            spreadsheet_id = extract_google_id(self.config.get("spreadsheet_id", ""))
            if not spreadsheet_id: raise RuntimeError("请先填写有效的 Google 表格 ID 或链接。")
            metadata = service.spreadsheets().get(
                spreadsheetId=spreadsheet_id, fields="sheets.properties.title").execute()
            sheet_names = [item.get("properties", {}).get("title", "") for item in metadata.get("sheets", [])]
            sheet_names = [name for name in sheet_names if name]
            if self.mode == "sheets":
                self.finished.emit(True, sheet_names, f"已读取 {len(sheet_names)} 个 Sheet（{identity}）")
                return
            source_sheet = self.config.get("option_sheet_name", "").strip()
            if not source_sheet: raise RuntimeError("请选择用于读取下拉选项的配置 Sheet。")
            if source_sheet not in sheet_names: raise RuntimeError(f"表格中没有找到配置 Sheet：{source_sheet}")
            start_row = max(1, int(self.config.get("option_start_row", 2)))
            quoted = "'" + source_sheet.replace("'", "''") + "'"
            result = {}
            for item in self.config.get("variable_fields", []):
                source_column = str(item.get("source_column", "")).strip().upper()
                if not source_column: continue
                column_to_index(source_column)
                response = service.spreadsheets().values().get(
                    spreadsheetId=spreadsheet_id,
                    range=f"{quoted}!{source_column}{start_row}:{source_column}").execute()
                values=[]; seen=set()
                for row in response.get("values", []):
                    value=str(row[0]).strip() if row else ""
                    key=value.casefold()
                    if value and key not in seen: values.append(value); seen.add(key)
                result[item.get("field", source_column)] = values
            self.finished.emit(True, result, f"已从“{source_sheet}”读取 {sum(len(v) for v in result.values())} 个去重选项")
        except Exception as exc:
            self.finished.emit(False, {}, str(exc))


class PasteOptionsTable(QTableWidget):
    """支持把 Excel/Google Sheets 的多行、多列内容直接粘贴进选项网格。"""
    def keyPressEvent(self, event):
        if event.matches(QKeySequence.StandardKey.Paste):
            text=QApplication.clipboard().text().replace("\r\n","\n").replace("\r","\n")
            rows=[line.split("\t") for line in text.split("\n") if line or "\t" in line]
            if not rows: return
            start_row=max(0,self.currentRow()); start_col=max(0,self.currentColumn())
            needed_rows=start_row+len(rows)
            if needed_rows>self.rowCount(): self.setRowCount(needed_rows)
            for row_offset,values in enumerate(rows):
                for col_offset,value in enumerate(values):
                    column=start_col+col_offset
                    if column>=self.columnCount(): break
                    self.setItem(start_row+row_offset,column,QTableWidgetItem(value.strip()))
            return
        super().keyPressEvent(event)


class NoWheelComboBox(QComboBox):
    """与全局策略一致：未获焦点时滚轮不改值，便于页面滚动。"""

    def wheelEvent(self, event):
        if self.hasFocus():
            super().wheelEvent(event)
        else:
            event.ignore()


class FocusOnlyWheelFilter(QObject):
    """全局防误触：下拉框 / 数字框 / 滑条仅在点击获得焦点后才响应滚轮。

    与 Reels 编辑器一致；未聚焦时把滚轮交给外层滚动区域，避免滚动页面时改参数。

    注意：不可对正在处理的同一 QWheelEvent 再 sendEvent 转发——Win11/Qt6 会报
    CE_INVALIDATED，严重时卡死；应直接改 QScrollBar 数值。
    """

    _CONTROL_TYPES = (QComboBox, QAbstractSpinBox, QSlider)

    def eventFilter(self, obj, event):
        try:
            if event is None or event.type() != QEvent.Type.Wheel:
                return False
            control = self._find_control(obj)
            if control is None:
                return False
            try:
                if control.focusPolicy() != Qt.FocusPolicy.ClickFocus:
                    control.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
            except Exception:
                pass
            if control.hasFocus():
                return False  # 已聚焦：允许改值
            # 未聚焦：拦截改值，并手动滚动外层区域
            self._scroll_enclosing(control, event)
            return True
        except Exception:
            # eventFilter 里绝不能抛到 Qt（会刷 Error calling Python override）
            return False

    @classmethod
    def _find_control(cls, obj):
        widget = obj
        while widget is not None:
            if isinstance(widget, cls._CONTROL_TYPES):
                return widget
            widget = widget.parentWidget() if hasattr(widget, "parentWidget") else None
        return None

    @staticmethod
    def _enclosing_scroll(widget):
        parent = widget.parentWidget() if widget else None
        while parent is not None:
            if isinstance(parent, QScrollArea):
                return parent
            parent = parent.parentWidget()
        return None

    @classmethod
    def _scroll_enclosing(cls, widget, event):
        """用滚轮 delta 直接驱动外层 QScrollArea，避免 sendEvent 重入。"""
        scroll = cls._enclosing_scroll(widget)
        if scroll is None:
            return
        try:
            angle = event.angleDelta()
            pixel = event.pixelDelta()
        except Exception:
            return
        dy = int(angle.y()) if angle is not None else 0
        dx = int(angle.x()) if angle is not None else 0
        if dy == 0 and pixel is not None:
            dy = int(pixel.y())
        if dx == 0 and pixel is not None:
            dx = int(pixel.x())
        # 竖向优先；Shift+滚轮常见为横向
        def _apply(bar, delta):
            if bar is None or not bar.isEnabled() or not delta:
                return
            steps = max(1, int(bar.singleStep() or 1) * 3)
            if abs(delta) >= 15:
                bar.setValue(bar.value() - int(delta / 120.0 * steps))
            else:
                bar.setValue(bar.value() - (steps if delta > 0 else -steps))

        _apply(scroll.verticalScrollBar(), dy)
        _apply(scroll.horizontalScrollBar(), dx)


def apply_click_focus_to_wheel_controls(root: QWidget) -> None:
    """把已有控件设为点击后才聚焦，配合 FocusOnlyWheelFilter。"""
    for cls in (QComboBox, QAbstractSpinBox, QSlider):
        for widget in root.findChildren(cls):
            widget.setFocusPolicy(Qt.FocusPolicy.ClickFocus)


class VariableOptionsDialog(QDialog):
    """以“字段为列、选项为行”的方式批量维护本地上传选项。"""
    def __init__(self, fields, parent=None):
        super().__init__(parent); self.fields=[dict(item) for item in fields]
        self.setWindowTitle("配置下拉字段和选项"); self.resize(1040,620)
        root=QVBoxLayout(self); root.setContentsMargins(12,12,12,10); root.setSpacing(8)
        hint=QLabel("每一列对应一个上传字段。选中列下方的第一个空格后，可直接粘贴多行数据；也支持一次粘贴多列。保存时会自动删除空白和重复项。")
        hint.setWordWrap(True); hint.setStyleSheet("color:#7dd3fc;"); root.addWidget(hint)
        max_rows=max([len(item.get("options",[])) for item in self.fields]+[8])
        self.table=PasteOptionsTable(max_rows+3,len(self.fields))
        self.table.setHorizontalHeaderLabels([f"{item.get('field','字段')}\n（写入 {item.get('column','')} 列）" for item in self.fields])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setDefaultSectionSize(30)
        self.table.setAlternatingRowColors(False); self.table.setSelectionMode(QAbstractItemView.SelectionMode.ContiguousSelection)
        self.table.setStyleSheet("""
            QTableWidget { background:#0b1424; color:#f1f5f9; gridline-color:#334155; border:1px solid #334155; }
            QTableWidget::item { background:#0b1424; color:#f1f5f9; padding:4px; }
            QTableWidget::item:selected { background:#2563eb; color:#ffffff; }
            QHeaderView::section { background:#1b2a41; color:#e2e8f0; border:1px solid #334155; padding:5px; font-weight:700; }
        """)
        for column,item in enumerate(self.fields):
            for row,value in enumerate(item.get("options",[])):
                self.table.setItem(row,column,QTableWidgetItem(str(value)))
        root.addWidget(self.table,1)
        actions=QHBoxLayout(); add_rows=QPushButton("增加 10 行"); add_rows.clicked.connect(lambda:self.table.setRowCount(self.table.rowCount()+10))
        clear_column=QPushButton("清空选中列"); clear_column.clicked.connect(self._clear_selected_columns)
        actions.addWidget(add_rows); actions.addWidget(clear_column); actions.addStretch(); root.addLayout(actions)
        buttons=QDialogButtonBox(QDialogButtonBox.StandardButton.Save|QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存选项")
        buttons.accepted.connect(self.accept); buttons.rejected.connect(self.reject); root.addWidget(buttons)

    def _clear_selected_columns(self):
        columns={index.column() for index in self.table.selectedIndexes()}
        for column in columns:
            for row in range(self.table.rowCount()): self.table.takeItem(row,column)

    def result_fields(self):
        result=[]
        for column,item in enumerate(self.fields):
            values=[]; seen=set()
            for row in range(self.table.rowCount()):
                cell=self.table.item(row,column); value=cell.text().strip() if cell else ""; key=value.casefold()
                if value and key not in seen: values.append(value); seen.add(key)
            updated=dict(item); updated["options"]=values
            if updated.get("selected") not in values: updated["selected"]=""
            result.append(updated)
        return result


class GoogleSettingsPanel(QWidget):
    """常驻的 Google Drive / Sheets 多方案编辑器。"""
    profiles_changed = Signal()

    def __init__(self, store):
        super().__init__(); self.store = store; self.auth_thread = None; self.auth_worker = None
        self.sheet_thread = None; self.sheet_worker = None; self._loaded_options = {}; self._variable_selected = {}; self._available_sheet_names = []
        self._build(); self.load_current()
        # 授权成功后直接复用本地 OAuth token / 服务账号，不要求每次启动重新点击检查。

    def _build(self):
        # 与「组件 / 字体 / 密钥」分区统一：外边距、标题、副标题风格
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 16, 24, 16)
        root.setSpacing(8)
        top = QHBoxLayout()
        title = QLabel("☁ Google Drive / Sheets 授权与同步方案")
        title.setObjectName("heading")
        top.addWidget(title)
        top.addStretch()
        self.profile = QComboBox()
        self.profile.setEditable(True)
        self.profile.setMinimumWidth(210)
        self.profile.currentTextChanged.connect(self.load_profile)
        save = QPushButton("保存为当前方案")
        save.setObjectName("primary")
        save.clicked.connect(self.save_profile)
        delete = QPushButton("删除方案")
        delete.clicked.connect(self.delete_profile)
        top.addWidget(QLabel("方案"))
        top.addWidget(self.profile)
        top.addWidget(save)
        top.addWidget(delete)
        root.addLayout(top)
        hint = QLabel(
            "把授权、Drive 文件夹、表格、Sheet、固定列和上传时选择项保存在同一方案。"
            "流水线 / Reels 开始前只需选择方案。"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#94a3b8;font-size:13px;")
        root.addWidget(hint)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet("QScrollArea{background:transparent;border:none;}")
        body = QWidget()
        body.setStyleSheet("background:transparent;")
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 4, 8, 8)
        body_layout.setSpacing(10)
        auth = QGroupBox("🔐 授权与 Drive"); auth_form = QFormLayout(auth)
        auth_form.setContentsMargins(12, 12, 12, 12); auth_form.setSpacing(8)
        json_row = QHBoxLayout(); self.json_path = QLineEdit(); json_row.addWidget(self.json_path); browse = QPushButton("选择 JSON…")
        browse.clicked.connect(self.choose_json); json_row.addWidget(browse); auth_form.addRow("服务账号 / OAuth JSON", json_row)
        self.parent_folder = QLineEdit(); self.parent_folder.setPlaceholderText("Drive 父文件夹 ID 或链接"); auth_form.addRow("父文件夹", self.parent_folder)
        self.folder_mode = QComboBox(); self.folder_mode.addItems(["视频名称", "自定义名称"]); self.custom_folder = QLineEdit()
        mode_row = QHBoxLayout(); mode_row.addWidget(self.folder_mode); mode_row.addWidget(self.custom_folder, 1); auth_form.addRow("云端目录命名", mode_row)
        auth_row = QHBoxLayout(); self.auth_status = QLabel("尚未检查"); self.auth_status.setWordWrap(True); self.auth_button = QPushButton("授权 / 重新检查")
        self.auth_button.clicked.connect(lambda: self.check_auth(True)); auth_row.addWidget(self.auth_status, 1); auth_row.addWidget(self.auth_button); auth_form.addRow("权限状态", auth_row)
        self.public_link = QCheckBox("允许知道链接的用户查看任务文件夹"); auth_form.addRow("共享", self.public_link); body_layout.addWidget(auth)
        sheet = QGroupBox("📊 Google Sheets 写入"); sheet_form = QFormLayout(sheet)
        sheet_form.setContentsMargins(12, 12, 12, 12); sheet_form.setSpacing(8)
        self.write_sheet = QCheckBox("上传完成后写入表格"); sheet_form.addRow("启用", self.write_sheet)
        self.spreadsheet = QLineEdit(); self.spreadsheet.setPlaceholderText("表格 ID 或完整链接")
        spreadsheet_row = QHBoxLayout(); spreadsheet_row.addWidget(self.spreadsheet, 1)
        self.read_sheets_button = QPushButton("读取 Sheet 名称"); self.read_sheets_button.clicked.connect(self.read_sheet_names); spreadsheet_row.addWidget(self.read_sheets_button)
        self.sheet_name = QComboBox(); self.sheet_name.setEditable(True); self.sheet_name.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.sheet_name.lineEdit().setPlaceholderText("选择或直接输入写入目标 Sheet")
        self.insert_row = QSpinBox(); self.insert_row.setRange(1,100000); self.insert_row.setValue(4)
        sheet_form.addRow("表格 ID", spreadsheet_row); sheet_form.addRow("写入 Sheet", self.sheet_name); sheet_form.addRow("数据起始行", self.insert_row)
        body_layout.addWidget(sheet)
        mapping_group = QGroupBox("🗂 固定字段与列映射"); mapping_layout = QVBoxLayout(mapping_group)
        mapping_layout.setContentsMargins(12, 12, 12, 12)
        self.mapping_table = QTableWidget(0, 4); self.mapping_table.setHorizontalHeaderLabels(["字段名称", "写入列", "类型", "固定内容"])
        self.mapping_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.mapping_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.mapping_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.mapping_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch); self.mapping_table.setMinimumHeight(210)
        mapping_layout.addWidget(self.mapping_table); mbuttons = QHBoxLayout(); madd = QPushButton("新增固定字段"); madd.clicked.connect(self.add_mapping)
        mdel = QPushButton("删除选中"); mdel.clicked.connect(lambda: self.remove_rows(self.mapping_table)); mbuttons.addWidget(madd); mbuttons.addWidget(mdel); mbuttons.addStretch(); mapping_layout.addLayout(mbuttons)
        body_layout.addWidget(mapping_group)
        variable_group = QGroupBox("☑ 本次上传可选择字段"); variable_layout = QVBoxLayout(variable_group)
        variable_layout.setContentsMargins(12, 12, 12, 12)
        variable_hint=QLabel("字段名称和写入列在下表维护；具体选项在独立窗口中按列批量粘贴。")
        variable_hint.setStyleSheet("color:#94a3b8;font-size:12px;"); variable_layout.addWidget(variable_hint)
        self.variable_table = QTableWidget(0, 3); self.variable_table.setHorizontalHeaderLabels(["字段名称", "写入列", "可选项数量"])
        self.variable_table.horizontalHeader().setSectionResizeMode(0,QHeaderView.ResizeMode.Stretch)
        self.variable_table.horizontalHeader().setSectionResizeMode(1,QHeaderView.ResizeMode.ResizeToContents)
        self.variable_table.horizontalHeader().setSectionResizeMode(2,QHeaderView.ResizeMode.ResizeToContents); self.variable_table.setMinimumHeight(180)
        variable_layout.addWidget(self.variable_table); vbuttons = QHBoxLayout(); configure=QPushButton("配置下拉字段和选项"); configure.setObjectName("primary"); configure.clicked.connect(self.configure_variable_options)
        vadd = QPushButton("新增字段"); vadd.clicked.connect(self.add_variable)
        vdel = QPushButton("删除选中"); vdel.clicked.connect(lambda: self.remove_rows(self.variable_table)); vbuttons.addWidget(configure); vbuttons.addWidget(vadd); vbuttons.addWidget(vdel); vbuttons.addStretch(); variable_layout.addLayout(vbuttons)
        body_layout.addWidget(variable_group); body_layout.addStretch(); scroll.setWidget(body); root.addWidget(scroll, 1)
        bottom = QHBoxLayout(); self.enabled = QCheckBox("允许流水线使用此方案"); bottom.addWidget(self.enabled); bottom.addStretch()
        apply = QPushButton("保存当前修改"); apply.clicked.connect(self.save_current); bottom.addWidget(apply); root.addLayout(bottom)

    def choose_json(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择 Google 授权 JSON", "", "JSON (*.json)")
        if path: self.json_path.setText(path); self.auth_status.setText("授权文件已变更，等待检查")

    def read_sheet_names(self):
        self._start_sheet_read("sheets")

    def _start_sheet_read(self, mode="sheets", config=None):
        if self.sheet_thread and self.sheet_thread.isRunning(): return
        config=config or self.read_ui(); self._sheet_read_mode=mode
        self.read_sheets_button.setEnabled(False); self.read_sheets_button.setText("正在读取…")
        self.sheet_thread=QThread(self); self.sheet_worker=GoogleSheetReadWorker(config,mode); self.sheet_worker.moveToThread(self.sheet_thread)
        self.sheet_thread.started.connect(self.sheet_worker.run)
        self.sheet_worker.finished.connect(self._sheet_read_done, Qt.ConnectionType.QueuedConnection)
        self.sheet_worker.finished.connect(self.sheet_thread.quit)
        self.sheet_thread.finished.connect(self._sheet_read_ended)
        self.sheet_thread.finished.connect(self.sheet_thread.deleteLater)
        self.sheet_thread.start()

    def _sheet_read_done(self, ok, data, message):
        self.read_sheets_button.setEnabled(True); self.read_sheets_button.setText("读取 Sheet 名称")
        if not ok:
            QMessageBox.warning(self,"读取 Google 表格失败",message); return
        target=self.sheet_name.currentText(); self.sheet_name.blockSignals(True); self.sheet_name.clear(); self.sheet_name.addItems(data)
        self.sheet_name.setCurrentText(target or (data[0] if data else "")); self.sheet_name.blockSignals(False)
        self._available_sheet_names=list(data); self.save_current(silent=True)
        QMessageBox.information(self,"读取完成",message)

    def _sheet_read_ended(self):
        self.sheet_worker=None; self.sheet_thread=None

    def add_mapping(self, item=None):
        item = item or {"field":"自定义字段","column":"","source":"static","value":""}; row = self.mapping_table.rowCount(); self.mapping_table.insertRow(row)
        for col, value in enumerate((item.get("field",""), item.get("column",""), item.get("source","static"), item.get("value",""))): self.mapping_table.setItem(row,col,QTableWidgetItem(str(value)))

    def add_variable(self, item=None):
        item = item or {"field":"选择项","column":"","options":[],"selected":""}; row = self.variable_table.rowCount(); self.variable_table.insertRow(row)
        field=item.get("field",""); options=list(item.get("options",[])); self._loaded_options[field]=options; self._variable_selected[field]=item.get("selected","")
        self.variable_table.setItem(row,0,QTableWidgetItem(str(field))); self.variable_table.setItem(row,1,QTableWidgetItem(str(item.get("column",""))))
        count=QTableWidgetItem(str(len(options))); count.setFlags(count.flags()&~Qt.ItemFlag.ItemIsEditable); self.variable_table.setItem(row,2,count)

    def configure_variable_options(self):
        fields=[]
        for row in range(self.variable_table.rowCount()):
            field=self.variable_table.item(row,0).text().strip() if self.variable_table.item(row,0) else ""
            column=self.variable_table.item(row,1).text().strip().upper() if self.variable_table.item(row,1) else ""
            if field and column:
                fields.append({"field":field,"column":column,"options":list(self._loaded_options.get(field,[])),"selected":self._variable_selected.get(field,"")})
        if not fields:
            QMessageBox.information(self,"没有字段","请先添加字段名称和写入列。")
            return
        dialog=VariableOptionsDialog(fields,self)
        if dialog.exec()!=QDialog.DialogCode.Accepted: return
        updated=dialog.result_fields(); self.variable_table.setRowCount(0); self._loaded_options={}; self._variable_selected={}
        for item in updated: self.add_variable(item)
        self.save_current(silent=True)

    def remove_rows(self, table):
        for row in sorted({idx.row() for idx in table.selectedIndexes()}, reverse=True): table.removeRow(row)

    def read_ui(self):
        mappings=[]
        for row in range(self.mapping_table.rowCount()):
            values=[self.mapping_table.item(row,c).text().strip() if self.mapping_table.item(row,c) else "" for c in range(4)]
            if values[1]: mappings.append({"field":values[0] or "自定义字段","column":values[1].upper(),"source":values[2] or "static","value":values[3]})
        variables=[]
        for row in range(self.variable_table.rowCount()):
            values=[self.variable_table.item(row,c).text().strip() if self.variable_table.item(row,c) else "" for c in range(2)]
            if values[0] and values[1]: variables.append({"field":values[0],"column":values[1].upper(),"options":list(self._loaded_options.get(values[0],[])),"selected":self._variable_selected.get(values[0],"")})
        base = dict(self.store.data.get("google_sync", {}))
        base.update({"enabled":self.enabled.isChecked(),"json_path":self.json_path.text().strip(),"parent_folder":self.parent_folder.text().strip(),
                     "folder_mode":self.folder_mode.currentText(),"custom_folder_name":self.custom_folder.text().strip(),"public_link":self.public_link.isChecked(),
                     "write_sheet":self.write_sheet.isChecked(),"spreadsheet_id":self.spreadsheet.text().strip(),"sheet_name":self.sheet_name.currentText().strip(),
                     "available_sheet_names":list(self._available_sheet_names),"insert_row":self.insert_row.value(),"sheet_mappings":mappings,"variable_fields":variables,"mapping_ui_version":3})
        return base

    def apply(self, config):
        self.json_path.setText(config.get("json_path","")); self.parent_folder.setText(config.get("parent_folder","")); self.folder_mode.setCurrentText(config.get("folder_mode","视频名称"))
        self.custom_folder.setText(config.get("custom_folder_name","")); self.public_link.setChecked(config.get("public_link",False)); self.write_sheet.setChecked(config.get("write_sheet",False))
        self.spreadsheet.setText(config.get("spreadsheet_id","")); self._available_sheet_names=list(config.get("available_sheet_names",[]))
        current_sheet=config.get("sheet_name",""); self.sheet_name.blockSignals(True); self.sheet_name.clear(); self.sheet_name.addItems(self._available_sheet_names); self.sheet_name.setCurrentText(current_sheet); self.sheet_name.blockSignals(False)
        self.insert_row.setValue(int(config.get("insert_row",4))); self.enabled.setChecked(config.get("enabled",False))
        self.mapping_table.setRowCount(0)
        for item in config.get("sheet_mappings",DEFAULT_SHEET_MAPPINGS): self.add_mapping(item)
        self.variable_table.setRowCount(0); self._loaded_options={}; self._variable_selected={}
        for item in config.get("variable_fields",DEFAULT_VARIABLE_FIELDS): self.add_variable(item)
        if config.get("auth_ok"):
            self.auth_status.setText(f"已授权：{config.get('auth_identity','Google 账号')}（启动后自动复用）"); self.auth_status.setStyleSheet("color:#86efac;")
        else: self.auth_status.setText("尚未授权或需要重新检查"); self.auth_status.setStyleSheet("color:#fbbf24;")

    def load_current(self):
        config = self.store.data["google_sync"]; profiles = config.get("sync_profiles",{})
        self.profile.blockSignals(True); self.profile.clear(); self.profile.addItems(profiles.keys()); self.profile.blockSignals(False)
        active = config.get("active_sync_profile","")
        if active in profiles: self.profile.setCurrentText(active); self.apply(profiles[active])
        else: self.apply(config)

    def load_profile(self, name):
        profile = self.store.data["google_sync"].get("sync_profiles",{}).get(name)
        if profile: self.apply(profile)

    def save_current(self, silent=False):
        config = self.read_ui(); profiles = dict(self.store.data["google_sync"].get("sync_profiles",{}))
        profile_name=self.profile.currentText().strip()
        if profile_name in profiles:
            profile_data=dict(config); profile_data.pop("sync_profiles",None); profile_data.pop("active_sync_profile",None); profiles[profile_name]=profile_data
        config["sync_profiles"] = profiles; config["active_sync_profile"] = profile_name if profile_name in profiles else ""
        self.store.data["google_sync"] = config; self.store.save(); self.profiles_changed.emit()
        if not silent: QMessageBox.information(self,"配置已保存","Google 同步配置已保存。")

    def save_profile(self):
        name = self.profile.currentText().strip()
        if not name:
            name, ok = QInputDialog.getText(self,"保存同步方案","方案名称（例如：方案1）：")
            if not ok or not name.strip(): return
            name = name.strip()
        config = self.read_ui(); profiles = dict(self.store.data["google_sync"].get("sync_profiles",{})); config.pop("sync_profiles",None)
        profiles[name] = config; current = dict(config); current["sync_profiles"] = profiles; current["active_sync_profile"] = name
        self.store.data["google_sync"] = current; self.store.save(); self.load_current(); self.profile.setCurrentText(name); self.profiles_changed.emit()
        QMessageBox.information(self,"方案已保存",f"同步方案“{name}”已保存并设为当前方案。")

    def delete_profile(self):
        name=self.profile.currentText().strip(); profiles=dict(self.store.data["google_sync"].get("sync_profiles",{}))
        if name not in profiles: return
        del profiles[name]; self.store.data["google_sync"]["sync_profiles"]=profiles; self.store.data["google_sync"]["active_sync_profile"]=""; self.store.save(); self.load_current(); self.profiles_changed.emit()

    def check_auth(self, interactive=True):
        if self.auth_thread and self.auth_thread.isRunning(): return
        config=self.read_ui(); self.auth_status.setText("正在检查 Google 权限…"); self.auth_button.setEnabled(False)
        self.auth_thread=QThread(self); self.auth_worker=GoogleAuthWorker(config, interactive); self.auth_worker.moveToThread(self.auth_thread)
        self.auth_thread.started.connect(self.auth_worker.run)
        # 强制主线程处理 UI / 弹窗，避免跨线程崩溃
        self.auth_worker.finished.connect(self.auth_done, Qt.ConnectionType.QueuedConnection)
        self.auth_worker.finished.connect(self.auth_thread.quit)
        self.auth_thread.finished.connect(self.auth_ended)
        self.auth_thread.finished.connect(self.auth_thread.deleteLater)
        self.auth_thread.start()

    def auth_done(self, ok, message):
        self.auth_button.setEnabled(True); config=self.read_ui(); config["auth_ok"]=ok; config["auth_identity"]=message if ok else ""; config["auth_checked"]=datetime.now().isoformat(timespec="seconds")
        profiles=dict(self.store.data["google_sync"].get("sync_profiles",{})); profile_name=self.profile.currentText().strip()
        if profile_name in profiles:
            profile_data=dict(config); profile_data.pop("sync_profiles",None); profile_data.pop("active_sync_profile",None); profiles[profile_name]=profile_data
        config["sync_profiles"]=profiles; config["active_sync_profile"]=profile_name if profile_name in profiles else ""
        self.store.data["google_sync"]=config; self.store.save(); self.profiles_changed.emit()
        self.auth_status.setText(("授权成功：" if ok else "授权失败：")+message); self.auth_status.setStyleSheet("color:#86efac;" if ok else "color:#fca5a5;")
        if not ok: QMessageBox.warning(self,"Google 授权失败",message)

    def auth_ended(self):
        worker = self.auth_worker
        self.auth_worker = None
        self.auth_thread = None
        if worker is not None:
            worker.deleteLater()


class FullTextToolTipFilter(QObject):
    """Show unabridged text for compact combo boxes and item views across the app."""

    def eventFilter(self, watched, event):
        try:
            if event is None:
                return False
            if event.type() == QEvent.Type.ToolTip:
                if isinstance(watched, QComboBox):
                    text = watched.currentText().strip()
                    if text:
                        QToolTip.showText(event.globalPos(), text, watched)
                        return True
                view = watched if isinstance(watched, QAbstractItemView) else None
                parent = watched.parentWidget() if hasattr(watched, "parentWidget") else None
                while view is None and parent is not None:
                    if isinstance(parent, QAbstractItemView):
                        view = parent
                        break
                    parent = parent.parentWidget()
                if view is not None:
                    point = view.viewport().mapFromGlobal(event.globalPos())
                    index = view.indexAt(point)
                    if index.isValid():
                        text = str(index.data(Qt.ItemDataRole.DisplayRole) or "").strip()
                        if text:
                            QToolTip.showText(event.globalPos(), text, view)
                            return True
            return super().eventFilter(watched, event)
        except Exception:
            return False



def _parse_version_parts(version):
    """Parse '1.7.14' / 'v1.7.14-beta' into comparable int tuples."""
    text = str(version or "").strip().lstrip("vV")
    # Drop pre-release suffix: 1.7.14-beta -> 1.7.14
    text = text.split("-", 1)[0].split("+", 1)[0]
    parts = []
    for chunk in text.split("."):
        digits = "".join(ch for ch in chunk if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts or (0,))


def _select_release_asset(assets, *, is_win=True, is_mac=False, machine=""):
    """Pick best download for this OS. Prefer Windows Setup .exe over green zip.

    Returns (download_url, filename) or ("", "").
    """
    assets = list(assets or [])
    machine_l = str(machine or "").lower()
    mac_arch = "arm64" if ("arm" in machine_l or "aarch64" in machine_l) else "x64"

    def score(asset):
        name = str(asset.get("name") or "")
        name_l = name.lower()
        url = str(asset.get("browser_download_url") or "").strip()
        if not url or not name:
            return -1
        if is_win:
            # VideoToolkit_Setup_vX.Y.Z.exe  (no "windows" in name — previous bug)
            if name_l.endswith(".exe") and "setup" in name_l:
                return 100
            if name_l.endswith(".exe") and ("windows" in name_l or "win64" in name_l or "win32" in name_l):
                return 80
            if name_l.endswith(".zip") and "windows" in name_l:
                return 60
            if name_l.endswith(".exe"):
                return 40
            if name_l.endswith(".zip"):
                return 20
            return -1
        if is_mac:
            if mac_arch not in name_l and "universal" not in name_l:
                return -1
            if name_l.endswith(".zip") and ("macos" in name_l or "darwin" in name_l or "osx" in name_l):
                return 80
            if name_l.endswith(".zip"):
                return 40
            if name_l.endswith(".dmg"):
                return 70
            return -1
        # Linux / other
        if "linux" in name_l and name_l.endswith(".zip"):
            return 80
        if name_l.endswith((".zip", ".tar.gz", ".appimage")):
            return 30
        return -1

    ranked = sorted(assets, key=score, reverse=True)
    if not ranked or score(ranked[0]) < 0:
        return "", ""
    best = ranked[0]
    return str(best.get("browser_download_url") or ""), str(best.get("name") or "")


def _github_download_candidates(url: str) -> list[str]:
    """Official URL first, then common release proxies (helps when GitHub CDN is blocked)."""
    url = str(url or "").strip()
    if not url:
        return []
    candidates = [url]
    if url.startswith("https://github.com/") or url.startswith("https://objects.githubusercontent.com/"):
        for prefix in (
            "https://ghproxy.net/",
            "https://gh.llkk.cc/",
            "https://mirror.ghproxy.com/",
        ):
            proxied = prefix + url
            if proxied not in candidates:
                candidates.append(proxied)
    return candidates


class UpdateCheckWorker(QObject):
    """从 GitHub releases/latest 检查新版本（Setup .exe 优先，其次为 .zip 绿色包）。"""
    # has_new, latest_version, download_url, filename, error
    finished = Signal(bool, str, str, str, str)

    def __init__(self, current_version):
        super().__init__()
        self.current_version = str(current_version or "").strip().lstrip("vV")

    def run(self):
        try:
            import platform
            import sys

            import requests

            headers = {
                "User-Agent": f"VideoToolkit-UpdateCheck/{self.current_version or '1.0'}",
                "Accept": "application/vnd.github+json",
            }
            response = requests.get(
                "https://api.github.com/repos/secure-artifacts/video-toolkit/releases/latest",
                headers=headers,
                timeout=15,
            )
            if response.status_code != 200:
                body = (response.text or "")[:180].replace("\n", " ")
                self.finished.emit(
                    False, "", "", "",
                    f"GitHub 返回 HTTP {response.status_code}" + (f"：{body}" if body else ""),
                )
                return

            data = response.json()
            tag_name = str(data.get("tag_name") or "").strip()
            latest_version = tag_name.lstrip("vV")
            if not latest_version:
                self.finished.emit(False, "", "", "", "无法从 GitHub 获取最新版本号")
                return

            has_new = _parse_version_parts(latest_version) > _parse_version_parts(self.current_version)
            # 已是最新：不必解析安装包，避免把文件名误当成错误信息
            if not has_new:
                self.finished.emit(False, latest_version, "", "", "")
                return

            is_win = sys.platform.startswith("win")
            is_mac = sys.platform.startswith("dar")
            download_url, filename = _select_release_asset(
                data.get("assets") or [],
                is_win=is_win,
                is_mac=is_mac,
                machine=platform.machine(),
            )
            if not download_url:
                self.finished.emit(
                    True, latest_version, "", "",
                    f"发现新版本 v{latest_version}，但未找到当前系统可用的安装包。\n"
                    "请到 GitHub Releases 页面手动下载。",
                )
                return

            self.finished.emit(True, latest_version, download_url, filename, "")
        except Exception as exc:
            self.finished.emit(False, "", "", "", f"{type(exc).__name__}: {exc}")


class DownloadWorker(QObject):
    progress = Signal(int)
    finished = Signal(bool, str, str)  # success, file_path, error

    def __init__(self, url, version, filename=""):
        super().__init__()
        self.url = url
        self.version = version
        self.filename = filename
        self.cancelled = False

    def run(self):
        try:
            import tempfile

            import requests

            if not str(self.url or "").strip():
                self.finished.emit(False, "", "下载地址为空")
                return

            ext = ".exe"
            if self.filename:
                ext = Path(self.filename).suffix or ext
            else:
                url_path = self.url.split("?")[0]
                if url_path.lower().endswith(".zip"):
                    ext = ".zip"
            dest = Path(tempfile.gettempdir()) / f"VideoToolkit_v{self.version}{ext}"
            headers = {
                "User-Agent": f"VideoToolkit-Updater/{self.version or APP_VERSION}",
                "Accept": "*/*",
            }
            # connect 15s；单次读块 180s（大安装包/弱网）；官方失败再试镜像
            timeouts = (15, 180)
            last_error = ""
            for attempt, candidate in enumerate(_github_download_candidates(self.url), 1):
                if self.cancelled:
                    self.finished.emit(False, "", "下载已取消")
                    return
                try:
                    with requests.get(
                        candidate,
                        stream=True,
                        timeout=timeouts,
                        headers=headers,
                        allow_redirects=True,
                    ) as response:
                        response.raise_for_status()
                        total = int(response.headers.get("content-length", 0) or 0)
                        downloaded = 0
                        with open(dest, "wb") as handle:
                            for chunk in response.iter_content(chunk_size=256 * 1024):
                                if self.cancelled:
                                    try:
                                        handle.close()
                                        dest.unlink(missing_ok=True)
                                    except Exception:
                                        pass
                                    self.finished.emit(False, "", "下载已取消")
                                    return
                                if not chunk:
                                    continue
                                handle.write(chunk)
                                downloaded += len(chunk)
                                if total > 0:
                                    self.progress.emit(min(99, int(downloaded / total * 100)))
                        size = dest.stat().st_size if dest.exists() else 0
                        if size < 1024 * 100:
                            raise RuntimeError(f"下载文件过小（{size} 字节），可能被拦截或不完整")
                        if total > 0 and size < int(total * 0.98):
                            raise RuntimeError(
                                f"下载不完整：已下 {size // (1024 * 1024)} MB /"
                                f" 预期 {total // (1024 * 1024)} MB"
                            )
                        self.progress.emit(100)
                        self.finished.emit(True, str(dest), "")
                        return
                except Exception as exc:
                    last_error = f"{type(exc).__name__}: {exc}"
                    try:
                        if dest.exists():
                            dest.unlink(missing_ok=True)
                    except Exception:
                        pass
                    # 下一个候选镜像
                    continue

            tip = (
                f"下载失败（已尝试官方与镜像通道）。\n{last_error or '未知错误'}\n\n"
                "可到 GitHub Releases 页面用浏览器手动下载：\n"
                "https://github.com/secure-artifacts/video-toolkit/releases/latest"
            )
            self.finished.emit(False, "", tip)
        except Exception as exc:
            self.finished.emit(False, "", f"{type(exc).__name__}: {exc}")


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        _startup_trace("MainWindow init")
        # 打包版的 FFmpeg / FFprobe 位于 PyInstaller 解包目录；将它与用户组件目录
        # 一并加入 PATH，确保截图、剪辑、水印、字幕和组件检测使用同一套工具。
        media_paths = [str(component_bin())]
        bundled_media = str(bundled_media_tool("ffmpeg").parent)
        if bundled_media_tool("ffmpeg").exists():
            media_paths.insert(0, bundled_media)
        current_path = os.environ.get("PATH", "")
        current_parts = current_path.split(os.pathsep)
        prepend = [path for path in media_paths if path not in current_parts]
        if prepend:
            os.environ["PATH"] = os.pathsep.join(prepend + [current_path])
        self.store = ConfigStore()
        self.thread = None
        self.worker = None
        self.cloud_thread = None
        self.cloud_worker = None
        self.pending_upload_files = []
        self.pending_upload_records = []
        self.pending_sheet_uploads = []; self.pending_sheet_folder_url = ""
        self.setWindowTitle(APP_DISPLAY_NAME)
        self.resize(1380, 820)
        self.setMinimumSize(1080, 680)
        icon = resource_path("logo.ico")
        if icon.exists():
            self.setWindowIcon(QIcon(str(icon)))
        _startup_trace("building UI")
        self._build_ui()
        self.full_text_tooltips = FullTextToolTipFilter(self)
        QApplication.instance().installEventFilter(self.full_text_tooltips)
        # 全应用：下拉/数字/滑条需点击聚焦后才响应滚轮（同 Reels 逻辑）
        self._focus_wheel_filter = FocusOnlyWheelFilter(self)
        QApplication.instance().installEventFilter(self._focus_wheel_filter)
        apply_click_focus_to_wheel_controls(self)
        _startup_trace("UI built")
        self._refresh_keys()
        _startup_trace("keys refreshed")
        # 启动后自动在后台静默检查更新（延迟3秒，不阻塞主UI展示）
        QTimer.singleShot(3000, lambda: self._check_update(manual=False))
    def _build_ui(self):
        root = QWidget()
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        nav = QFrame()
        nav.setObjectName("nav")
        nav.setFixedHeight(62)
        nav_layout = QHBoxLayout(nav)
        nav_layout.setContentsMargins(18, 10, 18, 10)
        nav_layout.setSpacing(5)
        brand = QLabel(f"▶  {APP_DISPLAY_NAME}")
        brand.setObjectName("brand")
        nav_layout.addWidget(brand)
        nav_layout.addSpacing(16)
        self.nav_buttons = []
        # 索引与 self.pages 顺序一致：0 首页 … 10 元数据 … 11 文字转语音
        nav_items = (
            ("首页", 0),
            ("格式转换", 1),
            ("智能剪辑", 2),
            ("Reels 编辑器", 3),
            ("文字转语音", 11),
            ("批量重命名", 4),
            ("清除元数据", 10),
            ("字幕提取", 5),
            ("自动流水线", 8),
            ("设置与组件", 7),
            ("帮助", 9),
        )
        for text, page_index in nav_items:
            btn = QPushButton(text)
            btn.setCheckable(True)
            btn.setObjectName("navButton")
            btn.setProperty("pageIndex", page_index)
            btn.clicked.connect(lambda checked=False, idx=page_index: self._show_page(idx))
            nav_layout.addWidget(btn)
            self.nav_buttons.append(btn)

        # 帮助右侧：检查更新 → 查看软件日志（均不参与页面切换）
        self.update_btn = QPushButton("检查更新")
        self.update_btn.setObjectName("updateNavButton")
        self.update_btn.setToolTip("检查是否有新版本；启动后也会在后台静默检查")
        self.update_btn.clicked.connect(lambda: self._check_update(manual=True))
        nav_layout.addWidget(self.update_btn)

        self.log_nav_btn = QPushButton("查看软件日志")
        self.log_nav_btn.setObjectName("logNavButton")
        self.log_nav_btn.setToolTip("打开全局运行日志，排查批处理与 API 报错")
        self.log_nav_btn.clicked.connect(self._show_app_log)
        nav_layout.addWidget(self.log_nav_btn)

        self.merge_report_nav_btn = QPushButton("合成报表")
        self.merge_report_nav_btn.setObjectName("logNavButton")
        self.merge_report_nav_btn.setToolTip("查看 Reels 分组合成统计与成品记录")
        self.merge_report_nav_btn.clicked.connect(self._show_reels_merge_report)
        nav_layout.addWidget(self.merge_report_nav_btn)

        nav_layout.addStretch()
        privacy = QLabel("密钥仅存本机")
        privacy.setStyleSheet("color:#64748b;font-size:11px;")
        nav_layout.addWidget(privacy)
        outer.addWidget(nav)

        self.pages = QStackedWidget()
        self.pages.addWidget(self._home_page())
        _startup_trace("home page ready")
        self.screenshot_page = ScreenshotPage()
        try:
            self.screenshot_page.set_ffmpeg_finder(self._find_ffmpeg)
        except Exception:
            pass
        _startup_trace("screenshot page ready")
        self.smartcut_page = SmartCutPage()
        _startup_trace("smartcut page ready")
        self.dynamic_caption_page = DynamicCaptionPage(
            self._caption_transcribe, self._text_to_speech, self._find_ffmpeg,
            TRANSCRIPTION_PROVIDERS, AUTO_PROVIDER,
            self._reels_sync_profiles, self._start_reels_cloud_sync, self._open_google_settings,
            store=self.store)
        self.dynamic_caption_page.rename_folder_requested.connect(self._open_folder_in_batch_rename)
        self.dynamic_caption_page.navigate_requested.connect(self._show_page)
        _startup_trace("watermark page ready")
        self.rename_page = RenamePage(self._rename_title_transcribe)
        _startup_trace("rename page ready")
        self.pages.addWidget(self.screenshot_page)
        self.pages.addWidget(self.smartcut_page)
        self.pages.addWidget(self.dynamic_caption_page)
        self.pages.addWidget(self.rename_page)
        self.pages.addWidget(self._subtitle_page())
        _startup_trace("subtitle page ready")
        # 保留原页面索引 6 作为兼容入口；_show_page(6) 会转到设置中的密钥分区。
        self.pages.addWidget(QWidget())
        _startup_trace("keys page ready")
        self.key_settings_page = self._keys_page()
        self.component_settings_page = SettingsPage()
        self.font_settings_page = self._font_settings_page()
        self.google_settings_page = GoogleSettingsPanel(self.store)
        self.settings_page = self._build_settings_shell()
        _startup_trace("settings page ready")
        self.pages.addWidget(self.settings_page)
        self.pages.addWidget(self._pipeline_page())
        self.google_settings_page.profiles_changed.connect(self._refresh_pipeline_profiles)
        self.google_settings_page.profiles_changed.connect(self.dynamic_caption_page.refresh_sync_profiles)
        _startup_trace("pipeline page ready")
        self.pages.addWidget(self._help_page())
        _startup_trace("help page ready")
        self.metadata_page = MetadataPage()
        self.pages.addWidget(self.metadata_page)
        _startup_trace("metadata page ready")
        self.tts_page = TtsPage(
            text_to_speech_fn=self._text_to_speech,
            store=self.store,
        )
        self.tts_page.navigate_requested.connect(self._show_page)
        self.pages.addWidget(self.tts_page)
        _startup_trace("tts page ready")
        outer.addWidget(self.pages, 1)
        self._show_page(0)

    def _show_reels_merge_report(self):
        if hasattr(self, "dynamic_caption_page"):
            self.dynamic_caption_page._show_group_merge_report()

    def _side_nav_button_style(self):
        return (
            "QPushButton{text-align:left;padding:11px 12px;border:1px solid transparent;"
            "border-radius:8px;color:#cbd5e1;font-size:13px;font-weight:600;background:transparent;}"
            "QPushButton:hover{background:#1e293b;color:#f1f5f9;}"
            "QPushButton:checked{background:#1d4ed8;color:white;border-color:#60a5fa;}"
        )

    def _build_settings_shell(self):
        """设置页：左侧导航 + 右侧内容，与帮助页统一。"""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(28, 18, 28, 18)
        layout.setSpacing(10)
        heading = QLabel("设置与组件")
        heading.setObjectName("heading")
        layout.addWidget(heading)
        sub = QLabel("左侧切换分区：组件 · 字体与语言包 · Google · 密钥。布局与帮助页一致。")
        sub.setWordWrap(True)
        sub.setStyleSheet("color:#94a3b8;font-size:14px;")
        layout.addWidget(sub)

        body = QHBoxLayout()
        body.setSpacing(14)
        nav_frame = QFrame()
        nav_frame.setObjectName("helpSideNav")
        nav_frame.setFixedWidth(220)
        nav_frame.setStyleSheet(
            "#helpSideNav{background:#0f172a;border:1px solid #334155;border-radius:10px;}"
        )
        nav_col = QVBoxLayout(nav_frame)
        nav_col.setContentsMargins(10, 12, 10, 12)
        nav_col.setSpacing(6)
        nav_title = QLabel("分区")
        nav_title.setStyleSheet("color:#94a3b8;font-size:12px;font-weight:700;padding:0 4px 6px 4px;")
        nav_col.addWidget(nav_title)

        content_frame = QFrame()
        content_frame.setObjectName("helpContentFrame")
        content_frame.setStyleSheet(
            "#helpContentFrame{background:#0b1220;border:1px solid #334155;border-radius:10px;}"
        )
        content_col = QVBoxLayout(content_frame)
        content_col.setContentsMargins(0, 0, 0, 0)
        self.settings_stack = QStackedWidget()
        self.settings_stack.addWidget(self.component_settings_page)
        self.settings_stack.addWidget(self.font_settings_page)
        self.settings_stack.addWidget(self.google_settings_page)
        self.settings_stack.addWidget(self.key_settings_page)
        content_col.addWidget(self.settings_stack)

        self._settings_nav_buttons = []
        self._settings_section_widgets = [
            self.component_settings_page,
            self.font_settings_page,
            self.google_settings_page,
            self.key_settings_page,
        ]
        for index, item in enumerate(SETTINGS_NAV):
            btn = QPushButton(item["title"])
            btn.setCheckable(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setStyleSheet(self._side_nav_button_style())
            btn.clicked.connect(lambda checked=False, i=index: self._show_settings_section(i))
            nav_col.addWidget(btn)
            self._settings_nav_buttons.append(btn)
        nav_col.addStretch(1)

        body.addWidget(nav_frame)
        body.addWidget(content_frame, 1)
        layout.addLayout(body, 1)

        # 兼容旧代码：setCurrentWidget / count / tabText / currentWidget
        page.setCurrentWidget = self._settings_set_current_widget  # type: ignore[attr-defined]
        page.currentWidget = lambda: self.settings_stack.currentWidget()  # type: ignore[attr-defined]
        page.count = lambda: self.settings_stack.count()  # type: ignore[attr-defined]
        page.tabText = self._settings_tab_text  # type: ignore[attr-defined]
        page.widget = lambda i: self.settings_stack.widget(i)  # type: ignore[attr-defined]

        self._show_settings_section(0)
        return page

    def _settings_tab_text(self, index: int) -> str:
        labels = [x["title"] for x in SETTINGS_NAV]
        if 0 <= index < len(labels):
            return labels[index]
        return ""

    def _settings_set_current_widget(self, widget):
        try:
            index = self._settings_section_widgets.index(widget)
        except (ValueError, AttributeError):
            return
        self._show_settings_section(index)

    def _show_settings_section(self, index: int):
        if not hasattr(self, "settings_stack"):
            return
        if index < 0 or index >= self.settings_stack.count():
            return
        self.settings_stack.setCurrentIndex(index)
        for i, btn in enumerate(getattr(self, "_settings_nav_buttons", [])):
            btn.setChecked(i == index)

    def _font_settings_page(self):
        page, layout = self._page_shell(
            "🔤 字体与语言包",
            "字体用于 Reels 预览与烧录；语言包控制字幕引号/RTL 等书写规范（无需系统语言包）。",
        )
        group = QGroupBox("🔤 本地与开源字体")
        group_layout = QVBoxLayout(group)
        group_layout.setContentsMargins(14, 14, 14, 14)
        group_layout.setSpacing(10)
        folder = QLineEdit(str(app_data_dir() / "fonts"))
        folder.setReadOnly(True)
        folder_row = QHBoxLayout()
        folder_row.addWidget(QLabel("字体保存目录"))
        folder_row.addWidget(folder, 1)
        group_layout.addLayout(folder_row)
        buttons = QHBoxLayout()
        import_button = QPushButton("导入本地字体…")
        import_button.setObjectName("primary")
        import_button.setToolTip("支持 TTF、OTF、TTC")
        import_button.clicked.connect(self.dynamic_caption_page._import_local_fonts)
        open_button = QPushButton("下载开源字体…")
        open_button.setToolTip("从 Google Fonts 官方仓库下载，安装一次后可离线使用")
        open_button.clicked.connect(self.dynamic_caption_page._open_source_font_library)
        buttons.addWidget(import_button)
        buttons.addWidget(open_button)
        buttons.addStretch()
        group_layout.addLayout(buttons)
        note = QLabel("导入后无需重启；回到 Reels「字体」下拉即可选择。阿拉伯/希伯来请选用支持该文种的字体。")
        note.setWordWrap(True)
        note.setStyleSheet("color:#7dd3fc;background:#0b1830;padding:8px;border-radius:5px;")
        group_layout.addWidget(note)
        layout.addWidget(group)

        lang_group = QGroupBox("🌐 字幕书写语言包")
        lang_layout = QVBoxLayout(lang_group)
        lang_layout.setContentsMargins(14, 14, 14, 14)
        lang_layout.setSpacing(10)
        lang_layout.addWidget(QLabel(
            "内置：en / pt / es / fr / de / it / el / ru / tr / zh / ar / he。\n"
            "导入 JSON 可扩展或覆盖（需含 code 字段，如 \"code\": \"my\"）。"
        ))
        pack_dir = QLineEdit(str(user_language_packs_dir()))
        pack_dir.setReadOnly(True)
        dir_row = QHBoxLayout()
        dir_row.addWidget(QLabel("用户语言包目录"))
        dir_row.addWidget(pack_dir, 1)
        lang_layout.addLayout(dir_row)
        lang_btns = QHBoxLayout()
        import_pack = QPushButton("导入语言包 JSON…")
        import_pack.setObjectName("primary")
        import_pack.setToolTip("选择 .json 语言包文件，复制到用户目录并立即生效")
        import_pack.clicked.connect(self._import_language_pack)
        open_pack_dir = QPushButton("打开语言包目录")
        open_pack_dir.clicked.connect(lambda: self._open_path(str(user_language_packs_dir())))
        reload_pack = QPushButton("重新加载语言包")
        reload_pack.clicked.connect(self._reload_language_packs)
        lang_btns.addWidget(import_pack)
        lang_btns.addWidget(open_pack_dir)
        lang_btns.addWidget(reload_pack)
        lang_btns.addStretch()
        lang_layout.addLayout(lang_btns)
        sample = QLabel(
            "JSON 示例：\n"
            '{ "code": "nl", "name": "Nederlands", "quote_open": "“", "quote_close": "”", "rtl": false }'
        )
        sample.setWordWrap(True)
        sample.setStyleSheet(
            "color:#94a3b8;background:#0c1424;padding:10px;border-radius:6px;"
            "font-family:Consolas,'Microsoft YaHei UI';font-size:12px;"
        )
        lang_layout.addWidget(sample)
        layout.addWidget(lang_group)
        layout.addStretch()
        return page

    def _import_language_pack(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "导入语言包", "", "语言包 JSON (*.json);;所有文件 (*.*)")
        if not path:
            return
        ok, message = import_language_pack_file(path)
        if ok:
            QMessageBox.information(self, "导入成功", message)
            write_app_log(message, "INFO", "语言包")
            # 刷新 Reels / 字幕语言下拉
            if hasattr(self, "dynamic_caption_page") and hasattr(self.dynamic_caption_page, "writing_language"):
                cur = writing_language_from_ui(self.dynamic_caption_page.writing_language.currentText())
                fill_writing_language_combo(self.dynamic_caption_page.writing_language, cur)
            if hasattr(self, "language_edit"):
                cur = writing_language_from_ui(self.language_edit.currentText())
                fill_writing_language_combo(self.language_edit, cur)
        else:
            QMessageBox.warning(self, "导入失败", message)

    def _reload_language_packs(self):
        reload_language_packs()
        QMessageBox.information(self, "已重新加载", "语言包缓存已刷新。新规则将在下次格式化字幕时生效。")
        write_app_log("用户触发重新加载语言包", "INFO", "语言包")

    def _open_path(self, path: str):
        from modules.platform_utils import open_local_path
        open_local_path(path)

    def _page_shell(self, title, subtitle):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(24, 16, 24, 16)
        layout.setSpacing(7)
        heading = QLabel(title)
        heading.setObjectName("heading")
        sub = QLabel(subtitle)
        sub.setStyleSheet("color:#94a3b8;")
        layout.addWidget(heading)
        layout.addWidget(sub)
        layout.addSpacing(6)
        return page, layout

    def _home_page(self):
        page, layout = self._page_shell("一站式视频工作台", "选择需要的业务功能；文件、文件夹和网络链接均可按模块批量处理。")
        tools = [
            ("▣", "格式转换",
             "• 批量截图（网络/本地视频取帧）\n• 图片格式转换（含 HEIF/HEIC）\n• 视频压缩/转格式：优先转封装保画质（如 4K MOV→4K MP4）\n• 不兼容时自动高质量重编码减小体积",
             "#38bdf8", "page:1"),
            ("✂", "智能剪辑",
             "• 根据画面变化自动检测视频场景\n• 支持自定义片段时长和批量切分\n• 多视频、文件夹拖拽和任务队列\n• 输出成品并保留视频原有立体声音频",
             "#a78bfa", "page:2"),
            ("▶", "Reels 编辑器",
             "• 分组合成、批量配音与字幕智能识别\n• 字幕样式、字幕校对、视频预览和公司水印\n• 每个视频对应自己的音频与文案并批量生成\n• 可选生成后上传云端并按方案填写 Google Sheets",
             "#34d399", "page:3"),
            ("🎤", "文字转语音",
             "• 独立批量配音（微软 / ElevenLabs / Gemini）\n• ElevenLabs 网页会话扣点数（同浏览器插件 stream API）\n• 多卡文案、Excel 粘贴、本地缓存避免重复扣点\n• 音色/模型、混响、试听与导出目录",
             "#f472b6", "page:11"),
            ("A↔", "视频 / 文件重命名",
             "• 文件自然排序及 Windows 安全名称处理\n• 标题、日期、前后缀和连续编号组合\n• 执行前完整预览新旧文件名\n• 多套前缀与后缀方案保存和快速切换",
             "#fbbf24", "page:4"),
            ("CC", "智能字幕提取",
             "• 本地 Whisper 无需密钥即可识别\n• 在线服务支持多密钥检测与轮询\n• 批量处理网络链接、本地视频或音频\n• 中外文对照、全部复制及批量导出字幕",
             "#fb7185", "page:5"),
            ("⇢", "自动流水线",
             "• 智能剪辑 → 字幕提取 → 标题生成 → 批量重命名\n• 批量上传重命名成品并填写 Google Sheets\n• 上传成功、填表失败时可单独继续填表\n• 支持断点续接、方案保存和重复链接跳过",
             "#22d3ee", "page:8"),
            ("⌫", "批量清除素材元数据",
             "• 无损清除视频/音频的标题、作者、设备和章节信息\n• 清除图片 EXIF、XMP、拍摄时间和位置数据\n• 文件与文件夹拖拽、父目录和子目录批量选择\n• 可作为自动流水线的素材预处理步骤",
             "#60a5fa", "page:10"),
        ]
        rows = [QHBoxLayout() for _ in range((len(tools) + 1) // 2)]
        for idx, item in enumerate(tools):
            card = ToolCard(*item)
            card.clicked.connect(self._launch_tool)
            rows[idx // 2].addWidget(card)
        for row in rows:
            layout.addLayout(row, 1)
        return page

    def _help_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(28, 18, 28, 18)
        layout.setSpacing(10)

        heading = QLabel("帮助与使用说明")
        heading.setObjectName("heading")
        layout.addWidget(heading)
        sub = QLabel("最上方「更新日志」看新功能；「快速上手」入门；「常见问题」带图标分类，右侧可快速跳转。")
        sub.setWordWrap(True)
        sub.setStyleSheet("color:#94a3b8;font-size:14px;")
        layout.addWidget(sub)

        body = QHBoxLayout()
        body.setSpacing(14)

        nav_frame = QFrame()
        nav_frame.setObjectName("helpSideNav")
        nav_frame.setFixedWidth(220)
        nav_frame.setStyleSheet(
            "#helpSideNav{background:#0f172a;border:1px solid #334155;border-radius:10px;}"
        )
        nav_col = QVBoxLayout(nav_frame)
        nav_col.setContentsMargins(10, 12, 10, 12)
        nav_col.setSpacing(6)
        nav_title = QLabel("目录")
        nav_title.setStyleSheet("color:#94a3b8;font-size:12px;font-weight:700;padding:0 4px 6px 4px;")
        nav_col.addWidget(nav_title)

        content_frame = QFrame()
        content_frame.setObjectName("helpContentFrame")
        content_frame.setStyleSheet(
            "#helpContentFrame{background:#0b1220;border:1px solid #334155;border-radius:10px;}"
        )
        content_col = QVBoxLayout(content_frame)
        content_col.setContentsMargins(8, 8, 8, 8)
        content_col.setSpacing(8)

        # 右侧顶部：常见问题板块快速跳转（仅在「⑦ 常见问题」显示）
        self._help_jump_bar = QFrame()
        self._help_jump_bar.setObjectName("helpJumpBar")
        self._help_jump_bar.setStyleSheet(
            "#helpJumpBar{background:#111827;border:1px solid #334155;border-radius:8px;}"
        )
        jump_layout = QHBoxLayout(self._help_jump_bar)
        jump_layout.setContentsMargins(10, 8, 10, 8)
        jump_layout.setSpacing(6)
        jump_label = QLabel("快速跳转")
        jump_label.setStyleSheet("color:#94a3b8;font-size:12px;font-weight:700;")
        jump_layout.addWidget(jump_label)
        self._help_jump_buttons = []
        for anchor, label in FAQ_JUMP:
            jbtn = QPushButton(label)
            jbtn.setCursor(Qt.CursorShape.PointingHandCursor)
            jbtn.setStyleSheet(
                "QPushButton{background:#1e293b;border:1px solid #475569;border-radius:6px;"
                "padding:6px 10px;color:#e2e8f0;font-size:12px;font-weight:600;}"
                "QPushButton:hover{background:#2563eb;border-color:#60a5fa;color:white;}"
            )
            jbtn.clicked.connect(lambda checked=False, a=anchor: self._jump_help_faq(a))
            jump_layout.addWidget(jbtn)
            self._help_jump_buttons.append(jbtn)
        jump_layout.addStretch(1)
        self._help_jump_bar.setVisible(False)
        content_col.addWidget(self._help_jump_bar)

        self._help_browser = QTextBrowser()
        self._help_browser.setOpenExternalLinks(False)
        self._help_browser.setStyleSheet(
            "QTextBrowser{background:transparent;border:none;padding:4px 8px;"
            "font-size:15px;color:#e2e8f0;}"
        )
        self._help_browser.document().setDefaultStyleSheet(HELP_CSS)
        content_col.addWidget(self._help_browser, 1)

        self._help_nav_buttons = []
        for index, tab in enumerate(HELP_TABS):
            btn = QPushButton(tab["title"])
            btn.setCheckable(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setStyleSheet(self._side_nav_button_style())
            btn.clicked.connect(lambda checked=False, i=index: self._show_help_tab(i))
            nav_col.addWidget(btn)
            self._help_nav_buttons.append(btn)
            
        # Separator line
        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine); sep.setFrameShadow(QFrame.Shadow.Sunken); sep.setStyleSheet("background:#334155;max-height:1px;margin:6px 0;")
        nav_col.addWidget(sep)
        
        # Tools title
        tools_title = QLabel("实用辅助")
        tools_title.setStyleSheet("color:#94a3b8;font-size:12px;font-weight:700;padding:4px 4px 6px 4px;")
        nav_col.addWidget(tools_title)
        
        # Time calc button
        calc_btn = QPushButton("🧮 时间换算工具")
        calc_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        calc_btn.setStyleSheet(self._side_nav_button_style())
        calc_btn.clicked.connect(self._open_help_time_calc)
        nav_col.addWidget(calc_btn)
        
        # Feedback button
        feedback_btn = QPushButton("💬 问题反馈表单")
        feedback_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        feedback_btn.setStyleSheet(self._side_nav_button_style())
        feedback_btn.clicked.connect(self._open_feedback_form)
        nav_col.addWidget(feedback_btn)
        
        nav_col.addStretch(1)

        body.addWidget(nav_frame)
        body.addWidget(content_frame, 1)
        layout.addLayout(body, 1)

        self._show_help_tab(0)
        return page

    def _open_help_time_calc(self):
        from modules.dynamic_caption_page import TimeCalculatorDialog
        dialog = TimeCalculatorDialog(self)
        dialog.exec()
        
    def _open_feedback_form(self):
        dialog = FeedbackDialog(self)
        dialog.exec()

    def _show_help_tab(self, index: int, anchor: str | None = None):
        if index < 0 or index >= len(HELP_TABS):
            return
        for i, btn in enumerate(getattr(self, "_help_nav_buttons", [])):
            btn.setChecked(i == index)
        tab = HELP_TABS[index]
        html = f"<html><head></head><body>{tab['html']}</body></html>"
        if hasattr(self, "_help_browser"):
            self._help_browser.setHtml(html)
            if hasattr(self, "_help_jump_bar"):
                self._help_jump_bar.setVisible(index == HELP_FAQ_TAB_INDEX)
            if anchor and index == HELP_FAQ_TAB_INDEX:
                # 等文档布局完成后再滚动，避免锚点尚未就绪
                QTimer.singleShot(30, lambda a=anchor: self._help_browser.scrollToAnchor(a))
            else:
                self._help_browser.verticalScrollBar().setValue(0)

    def _jump_help_faq(self, anchor: str):
        """右侧顶部导航：进入常见问题并滚动到对应板块。"""
        self._show_help_tab(HELP_FAQ_TAB_INDEX, anchor=anchor)

    def _show_app_log(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("视频工具合集 · 软件运行日志")
        dialog.resize(920, 600)
        box = QVBoxLayout(dialog)
        hint = QLabel(
            "记录批处理进度、API 配额/密钥异常、自动切换和无法继续的错误，方便后续排查。"
        )
        hint.setWordWrap(True); hint.setStyleSheet("color:#7dd3fc;")
        box.addWidget(hint)
        viewer = QPlainTextEdit(); viewer.setReadOnly(True); viewer.setPlainText(read_app_log())
        from PySide6.QtGui import QTextCursor
        viewer.moveCursor(QTextCursor.MoveOperation.End)
        viewer.setStyleSheet("font-family:Consolas,'Microsoft YaHei UI';font-size:12px;")
        box.addWidget(viewer, 1)
        path = QLabel(f"日志位置：{app_log_path()}")
        path.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        path.setStyleSheet("color:#94a3b8;"); box.addWidget(path)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dialog.reject); box.addWidget(buttons)
        dialog.exec()

    def _subtitle_page(self):
        page, layout = self._page_shell("智能提取视频字幕", "结果直接显示在当前窗口；支持原文与简体中文对照、一键复制。")
        self.subtitle_results = {}

        main_split = QSplitter(Qt.Orientation.Horizontal)
        main_split.setChildrenCollapsible(False)
        control_panel = QFrame(); control_panel.setObjectName("panel")
        control_panel.setMinimumWidth(480)
        control_layout = QVBoxLayout(control_panel)
        control_layout.setContentsMargins(12, 10, 12, 10); control_layout.setSpacing(8)

        local_header = QHBoxLayout(); local_header.addWidget(QLabel("本地媒体（可拖入文件或文件夹）")); local_header.addStretch()
        self.media_source_hint = QLabel("尚未添加")
        self.media_source_hint.setStyleSheet("color:#7dd3fc;")
        local_header.addWidget(self.media_source_hint); control_layout.addLayout(local_header)
        self.file_list = MediaDropList()
        self.file_list.paths_dropped.connect(self._add_media_paths)
        self.file_list.setMinimumHeight(120); self.file_list.setMaximumHeight(185)
        file_buttons = QHBoxLayout()
        add = QPushButton("添加视频 / 音频")
        add.clicked.connect(self._add_media)
        add_folder = QPushButton("添加文件夹"); add_folder.clicked.connect(self._add_media_folder)
        remove = QPushButton("移除选中")
        remove.clicked.connect(self._remove_selected_media)
        file_buttons.addWidget(add)
        file_buttons.addWidget(add_folder)
        file_buttons.addWidget(remove)
        control_layout.addWidget(self.file_list)
        control_layout.addLayout(file_buttons)

        url_header = QHBoxLayout(); url_header.addWidget(QLabel("网络视频链接（每行一个）")); url_header.addStretch()
        paste_urls = QPushButton("粘贴"); paste_urls.clicked.connect(
            lambda: self.url_input.setPlainText(QApplication.clipboard().text()))
        clear_urls = QPushButton("清空"); clear_urls.clicked.connect(lambda: self.url_input.clear())
        url_header.addWidget(paste_urls); url_header.addWidget(clear_urls)
        control_layout.addLayout(url_header)
        self.url_input = QPlainTextEdit()
        self.url_input.setPlaceholderText(
            "支持 YouTube、Facebook、Instagram、TikTok；可一次粘贴多个链接，每行一个")
        self.url_input.setMinimumHeight(64); self.url_input.setMaximumHeight(88)
        control_layout.addWidget(self.url_input)

        settings_group = QGroupBox("识别设置")
        settings_group.setMinimumHeight(190)
        form = QFormLayout(settings_group)
        form.setContentsMargins(10, 10, 10, 8); form.setSpacing(6)
        self.provider_combo = QComboBox(); self.provider_combo.addItems(TRANSCRIPTION_PROVIDERS)
        self.provider_combo.currentTextChanged.connect(self._provider_changed)
        form.addRow("识别服务", self.provider_combo)
        # 可编辑下拉：本地模型三选一；云端可填自定义模型名
        self.model_edit = QComboBox()
        self.model_edit.setEditable(True)
        self.model_edit.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.model_edit.setToolTip(
            "本地 Whisper：small 快 / medium 推荐（语义更稳）/ large-v3 最准。\n"
            "Groq 建议 whisper-large-v3；Gemini 建议 gemini-3.6-flash（2.0 已关停）。\n"
            "自动模式显示「按优先级自动匹配」，本地体积在选中「本地 Whisper」时设置。"
        )
        form.addRow("模型", self.model_edit)
        self.language_edit = QComboBox()
        self.language_edit.setEditable(True)
        fill_writing_language_combo(self.language_edit, "")
        # 第一项「自动检测」对识别也表示 auto
        self.language_edit.setToolTip(
            "识别语言提示（Whisper/云服务）与书写规范共用。\n"
            "选「自动检测」时由模型判断；马达加斯加语请选「Malagasy 马达加斯加语」（码 mg），"
            "勿用自动——拉丁字母易被误判为英语/法语。\n"
            "也可选希腊/阿拉伯/西语等，或直接输入 el/ar/pt/mg。"
        )
        form.addRow("语言 / 书写规范", self.language_edit)
        self.diarize_check = QCheckBox("区分说话人（服务支持时启用）")
        form.addRow("说话人", self.diarize_check)
        priority_widget = QWidget(); priority_row = QHBoxLayout(priority_widget)
        priority_row.setContentsMargins(0, 0, 0, 0); priority_row.setSpacing(5)
        self.priority_label = QLabel(); self.priority_label.setWordWrap(True)
        priority_btn = QPushButton("调整顺序"); priority_btn.clicked.connect(self._open_priority_dialog)
        priority_row.addWidget(self.priority_label, 1); priority_row.addWidget(priority_btn)
        form.addRow("自动优先级", priority_widget)
        control_layout.addWidget(settings_group)

        self.transcribe_progress = QProgressBar(); self.transcribe_progress.setValue(0)
        control_layout.addWidget(self.transcribe_progress)
        actions = QHBoxLayout()
        self.subtitle_resume_check = QCheckBox("自动续接上次进度")
        self.subtitle_resume_check.setChecked(True)
        self.subtitle_resume_check.setToolTip("同一批素材和识别设置再次执行时，自动跳过已成功的视频")
        self.start_btn = QPushButton("开始提取字幕"); self.start_btn.setObjectName("primary")
        self.start_btn.clicked.connect(self._start_transcription)
        self.cancel_btn = QPushButton("取消"); self.cancel_btn.setEnabled(False); self.cancel_btn.clicked.connect(self._cancel_transcription)
        actions.addWidget(self.subtitle_resume_check); actions.addStretch()
        actions.addWidget(self.cancel_btn); actions.addWidget(self.start_btn)
        control_layout.addLayout(actions)
        control_layout.addWidget(QLabel("运行日志"))
        self.log_box = QPlainTextEdit(); self.log_box.setReadOnly(True)
        control_layout.addWidget(self.log_box, 1)

        result_panel = QFrame(); result_panel.setObjectName("panel")
        result_layout = QVBoxLayout(result_panel)
        result_layout.setContentsMargins(12, 10, 12, 10); result_layout.setSpacing(7)

        result_bar = QHBoxLayout(); result_bar.addWidget(QLabel("查看结果"))
        self.result_combo = QComboBox(); self.result_combo.addItem(ALL_RESULTS_LABEL)
        self.result_combo.currentTextChanged.connect(self._show_subtitle_result)
        copy_original = QPushButton("复制当前原文"); copy_original.clicked.connect(self._copy_current_original)
        copy_bilingual = QPushButton("复制当前对照"); copy_bilingual.setToolTip(
            "复制为 CSV 两列（原文\\t中文），可直接粘贴到 Google 表格左右并排"
        )
        copy_bilingual.clicked.connect(self._copy_bilingual)
        copy_all_original = QPushButton("复制全部原文"); copy_all_original.clicked.connect(self._copy_all_original)
        copy_all_bilingual = QPushButton("复制全部对照"); copy_all_bilingual.setToolTip(
            "复制全部结果为 CSV 两列（原文\\t中文），可直接粘贴到 Google 表格"
        )
        copy_all_bilingual.clicked.connect(self._copy_all_bilingual)
        export_all = QPushButton("批量导出字幕"); export_all.setObjectName("primary"); export_all.clicked.connect(self._export_all_subtitles)
        result_bar.addWidget(self.result_combo, 1); result_bar.addWidget(copy_original); result_bar.addWidget(copy_bilingual)
        result_bar.addWidget(copy_all_original); result_bar.addWidget(copy_all_bilingual); result_bar.addWidget(export_all)
        self.copy_status = QLabel("")
        self.copy_status.setStyleSheet(
            "color:#4ade80;font-size:12px;font-weight:700;padding:2px 8px;"
            "background:#052e16;border-radius:4px;"
        )
        self.copy_status.setVisible(False)
        result_bar.addWidget(self.copy_status)
        result_layout.addLayout(result_bar)
        result_split = QSplitter(Qt.Orientation.Vertical); result_split.setChildrenCollapsible(False)
        original_group = QGroupBox("识别原文"); original_layout = QVBoxLayout(original_group)
        self.original_result = QPlainTextEdit(); self.original_result.setReadOnly(True); original_layout.addWidget(self.original_result)
        chinese_group = QGroupBox("简体中文对照"); chinese_layout = QVBoxLayout(chinese_group)
        self.chinese_result = QPlainTextEdit(); self.chinese_result.setReadOnly(True); chinese_layout.addWidget(self.chinese_result)
        result_split.addWidget(original_group); result_split.addWidget(chinese_group); result_split.setSizes([360, 360])
        result_layout.addWidget(result_split, 1)

        control_scroll = QScrollArea(); control_scroll.setWidgetResizable(True); control_scroll.setWidget(control_panel)
        main_split.addWidget(control_scroll); main_split.addWidget(result_panel)
        main_split.setStretchFactor(0, 0); main_split.setStretchFactor(1, 1)
        main_split.setSizes([520, 1000])
        layout.addWidget(main_split, 1)
        self._refresh_priority_label()
        self._provider_changed(AUTO_PROVIDER)
        return page

    def _pipeline_page(self):
        page, layout = self._page_shell(
            "批量自动流水线",
            "一次完成：智能画面剪辑 → 批量字幕 → 字幕作为标题 → 按规则重命名；中间结果全部保留。")
        split = QSplitter(Qt.Orientation.Horizontal); split.setChildrenCollapsible(False)
        left = QFrame(); left.setObjectName("panel"); left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(12, 10, 12, 10); left_layout.setSpacing(7)
        left_layout.addWidget(QLabel("1. 原始视频（可拖入多个视频或文件夹）"))
        self.pipeline_files = MediaDropList(); self.pipeline_files.setMinimumHeight(145)
        self.pipeline_files.paths_dropped.connect(self._pipeline_add_paths); left_layout.addWidget(self.pipeline_files)
        source_buttons = QHBoxLayout()
        add_files = QPushButton("添加视频"); add_files.clicked.connect(self._pipeline_choose_files)
        add_folder = QPushButton("添加文件夹"); add_folder.clicked.connect(self._pipeline_choose_folder)
        clear = QPushButton("清空"); clear.clicked.connect(self.pipeline_files.clear)
        for button in (add_files, add_folder, clear): source_buttons.addWidget(button)
        left_layout.addLayout(source_buttons)

        settings = QGroupBox("2. 流程设置"); form = QFormLayout(settings)
        output_row = QHBoxLayout(); self.pipeline_output = QLineEdit(str(default_output_path("流水线输出")))
        choose_output = QPushButton("选择…"); choose_output.clicked.connect(self._pipeline_choose_output)
        output_row.addWidget(self.pipeline_output); output_row.addWidget(choose_output)
        output_widget = QWidget(); output_widget.setLayout(output_row); form.addRow("输出目录", output_widget)
        self.pipeline_threshold = QSpinBox(); self.pipeline_threshold.setRange(1, 100); self.pipeline_threshold.setValue(27)
        form.addRow("画面阈值", self.pipeline_threshold)
        self.pipeline_provider = QComboBox(); self.pipeline_provider.addItems(TRANSCRIPTION_PROVIDERS)
        form.addRow("字幕服务", self.pipeline_provider)
        self.pipeline_language = QComboBox(); self.pipeline_language.setEditable(True)
        fill_writing_language_combo(self.pipeline_language, "")
        self.pipeline_language.setToolTip(
            "识别语言与书写规范。马达加斯加语请选 Malagasy（mg）；"
            "自动检测易把马拉加斯语判成英语/法语。"
        )
        form.addRow("语言 / 书写规范", self.pipeline_language)
        rename_line = QHBoxLayout()
        self.pipeline_prefix = QLineEdit(); self.pipeline_prefix.setPlaceholderText("前缀")
        self.pipeline_date = QLineEdit(datetime.now().strftime("%Y%m%d"))
        self.pipeline_suffix = QLineEdit("FF-PT")
        rename_line.addWidget(self.pipeline_prefix); rename_line.addWidget(self.pipeline_date); rename_line.addWidget(self.pipeline_suffix)
        rename_widget = QWidget(); rename_widget.setLayout(rename_line); form.addRow("前缀/日期/后缀", rename_widget)
        number_line = QHBoxLayout(); self.pipeline_start = QSpinBox(); self.pipeline_start.setRange(0, 999999); self.pipeline_start.setValue(1)
        self.pipeline_padding = QSpinBox(); self.pipeline_padding.setRange(1, 12); self.pipeline_padding.setValue(3)
        number_line.addWidget(QLabel("起始编号")); number_line.addWidget(self.pipeline_start)
        number_line.addWidget(QLabel("位数")); number_line.addWidget(self.pipeline_padding); number_line.addStretch()
        number_widget = QWidget(); number_widget.setLayout(number_line); form.addRow("编号", number_widget)
        # 声音 / 水印：与 Reels 编辑器一致
        self.pipeline_audio_mode = QComboBox()
        self.pipeline_audio_mode.addItems([
            "仅视频原声",
            "视频原声＋背景音乐",
        ])
        self.pipeline_audio_mode.setToolTip(
            "与 Reels 一致：要么只保留原声，要么原声与 BGM 混合（不替换掉原声）。"
        )
        form.addRow("声音模式", self.pipeline_audio_mode)
        self.pipeline_bgm_path = QLineEdit()
        self.pipeline_bgm_path.setPlaceholderText("BGM 文件 或 文件夹（多曲库随机匹配）")
        self.pipeline_bgm_path.setToolTip(
            "可填单个音频文件，或填含多首 BGM 的文件夹。\n"
            "勾选「随机选曲并随机截取」后，每个成品从库中稳定随机一首，并从随机起点截取匹配时长。"
        )
        bgm_file_btn = QPushButton("文件…")
        bgm_folder_btn = QPushButton("文件夹…")
        def _pick_bgm_file():
            path, _ = QFileDialog.getOpenFileName(
                self, "选择背景音乐文件", "",
                "音频 (*.mp3 *.wav *.m4a *.aac *.flac *.ogg);;所有文件 (*.*)",
            )
            if path:
                self.pipeline_bgm_path.setText(path)
                self.pipeline_audio_mode.setCurrentText("视频原声＋背景音乐")
        def _pick_bgm_folder():
            path = QFileDialog.getExistingDirectory(self, "选择背景音乐文件夹")
            if path:
                self.pipeline_bgm_path.setText(path)
                self.pipeline_audio_mode.setCurrentText("视频原声＋背景音乐")
                self.pipeline_bgm_random.setChecked(True)
        bgm_file_btn.clicked.connect(_pick_bgm_file)
        bgm_folder_btn.clicked.connect(_pick_bgm_folder)
        bgm_row = QHBoxLayout()
        bgm_row.addWidget(self.pipeline_bgm_path, 1)
        bgm_row.addWidget(bgm_file_btn)
        bgm_row.addWidget(bgm_folder_btn)
        bgm_widget = QWidget(); bgm_widget.setLayout(bgm_row)
        form.addRow("背景音乐", bgm_widget)
        self.pipeline_bgm_random = QCheckBox("随机选曲并随机截取匹配（推荐文件夹曲库）")
        self.pipeline_bgm_random.setChecked(True)
        self.pipeline_bgm_random.setToolTip(
            "开启：每个视频按路径哈希稳定随机一首 BGM，并从随机起点截取（同 Reels）。\n"
            "关闭：固定使用选中文件，或文件夹内按队列顺序轮换，均从 0 秒起。"
        )
        form.addRow("", self.pipeline_bgm_random)
        self.pipeline_bgm_volume = QSpinBox()
        self.pipeline_bgm_volume.setRange(1, 100)
        self.pipeline_bgm_volume.setValue(25)
        self.pipeline_bgm_volume.setSuffix(" %")
        self.pipeline_bgm_volume.setToolTip("BGM 相对音量（原声保持 100%）")
        form.addRow("BGM 音量", self.pipeline_bgm_volume)
        self.pipeline_wm_enable = QCheckBox("成品叠加水印（默认 9:16 全屏覆盖 · 不透明度 100%）")
        self.pipeline_wm_enable.setToolTip(
            "与 Reels 一致：水印图强制缩放到画面尺寸后全屏覆盖（9:16 竖屏同样铺满）。"
        )
        self.pipeline_wm_path = QLineEdit()
        self.pipeline_wm_path.setPlaceholderText("水印图 PNG/JPG（9:16 全屏覆盖，铺满画面）")
        wm_browse = QPushButton("浏览…")
        def _pick_wm():
            path, _ = QFileDialog.getOpenFileName(
                self, "选择水印图片", "", "图片 (*.png *.jpg *.jpeg *.webp);;所有文件 (*.*)"
            )
            if path:
                self.pipeline_wm_path.setText(path)
                self.pipeline_wm_enable.setChecked(True)
        wm_browse.clicked.connect(_pick_wm)
        wm_row = QHBoxLayout()
        wm_row.addWidget(self.pipeline_wm_path, 1)
        wm_row.addWidget(wm_browse)
        wm_widget = QWidget(); wm_widget.setLayout(wm_row)
        form.addRow(self.pipeline_wm_enable, wm_widget)
        self.pipeline_wm_opacity = QSpinBox()
        self.pipeline_wm_opacity.setRange(10, 100)
        self.pipeline_wm_opacity.setValue(100)
        self.pipeline_wm_opacity.setSuffix(" %")
        self.pipeline_wm_opacity.setToolTip("默认 100% 不透明全屏覆盖；可按需调低")
        form.addRow("水印不透明度", self.pipeline_wm_opacity)
        wm_mode_hint = QLabel("水印模式：固定 9:16 全屏覆盖（scale2ref 铺满，非角落小标）")
        wm_mode_hint.setStyleSheet("color:#7dd3fc;")
        wm_mode_hint.setWordWrap(True)
        form.addRow("", wm_mode_hint)
        left_layout.addWidget(settings)
        cloud_group = QGroupBox("3. Google 云端同步（只上传重命名成品）")
        cloud_layout = QVBoxLayout(cloud_group); cloud_layout.setContentsMargins(10, 9, 10, 9)
        cloud_top = QHBoxLayout()
        self.pipeline_cloud_check = QCheckBox("流水线完成后自动上传并写入表格")
        self.pipeline_cloud_check.setChecked(self.store.data["google_sync"].get("enabled", False))
        self.pipeline_cloud_check.toggled.connect(self._pipeline_cloud_toggled)
        cloud_config = QPushButton("打开设置与组件")
        cloud_config.clicked.connect(self._open_google_settings)
        cloud_top.addWidget(self.pipeline_cloud_check); cloud_top.addStretch(); cloud_top.addWidget(cloud_config); cloud_layout.addLayout(cloud_top)
        profile_row = QHBoxLayout(); profile_row.addWidget(QLabel("同步方案")); self.pipeline_sync_profile = NoWheelComboBox()
        self.pipeline_sync_profile.currentTextChanged.connect(self._pipeline_profile_changed)
        self.pipeline_save_profile=QPushButton("保存方案"); self.pipeline_save_profile.clicked.connect(self._save_pipeline_sync_profile)
        profile_row.addWidget(self.pipeline_sync_profile, 1); profile_row.addWidget(self.pipeline_save_profile); cloud_layout.addLayout(profile_row)
        self.pipeline_profile_hint = QLabel("未选择同步方案"); self.pipeline_profile_hint.setWordWrap(True); self.pipeline_profile_hint.setStyleSheet("color:#94a3b8;")
        self.pipeline_profile_hint.setVisible(False)
        self.pipeline_variable_group = QGroupBox("本次上传选择（每次可重新选择）")
        self.pipeline_variable_form = QFormLayout(self.pipeline_variable_group); self.pipeline_variable_form.setVerticalSpacing(6)
        self.pipeline_runtime_values = {}; self.pipeline_runtime_sheet = ""; self._pipeline_runtime_profile = None
        cloud_layout.addWidget(self.pipeline_variable_group)
        left_layout.addWidget(cloud_group)
        self.pipeline_resume_check = QCheckBox("自动续接未完成任务（跳过已完成的剪辑、字幕和重命名）")
        self.pipeline_resume_check.setChecked(True)
        self.pipeline_resume_check.setToolTip("取消勾选后会创建一个全新的流水线任务")
        left_layout.addWidget(self.pipeline_resume_check)
        self.pipeline_progress = QProgressBar(); left_layout.addWidget(self.pipeline_progress)
        actions = QHBoxLayout(); actions.addStretch()
        self.pipeline_stop = QPushButton("停止"); self.pipeline_stop.setEnabled(False); self.pipeline_stop.clicked.connect(self._pipeline_cancel)
        self.pipeline_start_btn = QPushButton("开始自动流水线"); self.pipeline_start_btn.setObjectName("primary")
        self.pipeline_start_btn.clicked.connect(self._pipeline_start)
        actions.addWidget(self.pipeline_stop); actions.addWidget(self.pipeline_start_btn); left_layout.addLayout(actions)

        right = QFrame(); right.setObjectName("panel"); right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(12, 10, 12, 10); right_layout.setSpacing(7)
        step_text = QLabel("① 智能剪辑   →   ② 提取字幕   →   ③ 字幕生成标题   →   ④ 批量重命名成品   →   ⑤ 批量上传   →   ⑥ 批量填表")
        step_text.setWordWrap(True)
        step_text.setStyleSheet("color:#7dd3fc;font-size:14px;font-weight:700;padding:8px;")
        right_layout.addWidget(step_text)
        self.pipeline_cloud_result = QLabel("云端同步：等待执行")
        self.pipeline_cloud_result.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        self.pipeline_cloud_result.setOpenExternalLinks(True); self.pipeline_cloud_result.setWordWrap(True)
        self.pipeline_cloud_result.setStyleSheet("color:#86efac;padding:4px;")
        right_layout.addWidget(self.pipeline_cloud_result)
        right_layout.addWidget(QLabel("自动引用到批量重命名的标题列表"))
        self.pipeline_titles = QPlainTextEdit(); self.pipeline_titles.setReadOnly(True); self.pipeline_titles.setMinimumHeight(170)
        right_layout.addWidget(self.pipeline_titles, 1)
        right_layout.addWidget(QLabel("运行日志"))
        self.pipeline_log = QPlainTextEdit(); self.pipeline_log.setReadOnly(True)
        right_layout.addWidget(self.pipeline_log, 2)
        upload_actions = QHBoxLayout()
        upload_files = QPushButton("选择成品文件上传"); upload_files.clicked.connect(self._manual_upload_files)
        upload_folder = QPushButton("选择成品目录上传"); upload_folder.clicked.connect(self._manual_upload_folder)
        self.pipeline_retry_upload = QPushButton("继续上传"); self.pipeline_retry_upload.setEnabled(False)
        self.pipeline_retry_upload.clicked.connect(self._retry_cloud_upload)
        self.pipeline_continue_sheet = QPushButton("继续填表"); self.pipeline_continue_sheet.setEnabled(False)
        self.pipeline_continue_sheet.clicked.connect(self._continue_sheet_write)
        self.pipeline_stop_upload = QPushButton("停止上传"); self.pipeline_stop_upload.setEnabled(False)
        self.pipeline_stop_upload.clicked.connect(self._stop_cloud_upload)
        upload_actions.addWidget(upload_files); upload_actions.addWidget(upload_folder)
        upload_actions.addWidget(self.pipeline_retry_upload); upload_actions.addWidget(self.pipeline_continue_sheet); upload_actions.addWidget(self.pipeline_stop_upload)
        upload_actions.addStretch(); right_layout.addLayout(upload_actions)
        handoff = QHBoxLayout(); handoff.addStretch()
        to_subtitle = QPushButton("查看全部字幕"); to_subtitle.clicked.connect(lambda: self._show_page(5))
        to_rename = QPushButton("到批量重命名继续调整"); to_rename.clicked.connect(lambda: self._show_page(4))
        handoff.addWidget(to_subtitle); handoff.addWidget(to_rename); right_layout.addLayout(handoff)
        left_scroll = QScrollArea(); left_scroll.setWidgetResizable(True); left_scroll.setWidget(left)
        split.addWidget(left_scroll); split.addWidget(right); split.setSizes([620, 850])
        layout.addWidget(split, 1)
        self._refresh_pipeline_profiles()
        QTimer.singleShot(0,self._restore_pending_sheet_checkpoint)
        return page

    def _restore_pending_sheet_checkpoint(self):
        try:
            resume=dict(self.store.data.get("cloud_resume",{}))
            if resume.get("status")=="sheet_pending" and resume.get("uploads") and resume.get("folder_url"):
                self.pending_sheet_uploads=list(resume["uploads"]); self.pending_sheet_folder_url=resume["folder_url"]
                self.pipeline_continue_sheet.setEnabled(True)
                self.pipeline_cloud_result.setStyleSheet("color:#fbbf24;padding:4px;")
                self.pipeline_cloud_result.setText("检测到上次视频已上传但表格未完成，可直接点击“继续填表”。")
                return
            if resume.get("status")=="upload_pending" and resume.get("files"):
                self.pending_upload_files=list(resume["files"]); self.pending_upload_records=list(resume.get("records",[]))
                self.pipeline_retry_upload.setEnabled(True)
                self.pipeline_cloud_result.setStyleSheet("color:#fbbf24;padding:4px;")
                self.pipeline_cloud_result.setText("检测到上次上传未完成；点击“继续上传”将匹配云端已有文件并从缺少项继续。")
                return
            root=Path(self.pipeline_output.text())
            checkpoints=sorted(root.rglob("pipeline_checkpoint.json"),key=lambda path:path.stat().st_mtime,reverse=True) if root.is_dir() else []
            for checkpoint in checkpoints:
                state=read_json_file(checkpoint,{})
                uploads=state.get("pending_sheet_uploads",[]); folder_url=state.get("cloud_url","")
                if state.get("status")=="sheet_pending" and uploads and folder_url:
                    self.pending_sheet_uploads=list(uploads); self.pending_sheet_folder_url=folder_url; self.pipeline_continue_sheet.setEnabled(True)
                    self.pipeline_cloud_result.setStyleSheet("color:#fbbf24;padding:4px;")
                    self.pipeline_cloud_result.setText("检测到上次视频已上传但表格未完成，可直接点击“继续填表”。")
                    break
        except Exception:
            pass

    def _keys_page(self):
        page, layout = self._page_shell(
            "🔑 API 密钥管理",
            "粘贴即可自动识别（Gemini：AIza / AQ.）；也可强制指定。调用时轮询，失效自动切换。",
        )
        # 上方：左添加密钥 | 右申请入口（不抢下方列表高度）
        top_split = QSplitter(Qt.Orientation.Horizontal)
        top_split.setChildrenCollapsible(False)
        top_split.setHandleWidth(6)

        # —— 左侧：添加密钥（较窄即可）——
        add_group = QGroupBox("添加密钥")
        add_layout = QVBoxLayout(add_group)
        add_layout.setContentsMargins(10, 10, 10, 10)
        add_layout.setSpacing(6)
        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("归类"))
        self.key_assign_mode = QComboBox()
        self.key_assign_mode.addItem("自动识别（推荐）", "auto")
        for provider in PROVIDERS:
            self.key_assign_mode.addItem(f"强制 {provider}", provider)
        self.key_assign_mode.setToolTip(
            "自动：gsk_→Groq，AIza/AQ.→Gemini，sk_→ElevenLabs，UUID→Gladia。\n"
            "认不出会短时联网探测；仍失败请强制指定。"
        )
        mode_row.addWidget(self.key_assign_mode, 1)
        add_layout.addLayout(mode_row)
        self.key_bulk_input = QPlainTextEdit()
        self.key_bulk_input.setPlaceholderText(
            "每行一枚密钥，可混合服务：\n"
            "gsk_… → Groq\n"
            "AIza… / AQ.… → Gemini\n"
            "sk_… → ElevenLabs\n"
            "UUID → Gladia"
        )
        self.key_bulk_input.setMinimumHeight(120)
        add_layout.addWidget(self.key_bulk_input, 1)
        add_btn = QPushButton("添加密钥（自动检测）")
        add_btn.setObjectName("primary")
        add_btn.setMinimumHeight(34)
        add_btn.clicked.connect(self._add_keys_unified)
        add_layout.addWidget(add_btn)
        el_web_btn = QPushButton("添加 ElevenLabs 网页会话（Cookie）")
        el_web_btn.setToolTip(
            "用浏览器登录 ElevenLabs 后粘贴 Cookie，可扣该账号免费点数转语音。\n"
            "支持多个账户轮询；凭证加密保存，之后无需每次开浏览器。"
        )
        el_web_btn.setMinimumHeight(32)
        el_web_btn.clicked.connect(self._add_elevenlabs_web_session)
        add_layout.addWidget(el_web_btn)
        hint = QLabel(
            "gsk_→Groq · AIza/AQ.→Gemini · sk_→ElevenLabs API · UUID→Gladia\n"
            "ElevenLabs 推荐：网页会话 Cookie（多账户点数）或 sk_ API Key"
        )
        hint.setStyleSheet("color:#7dd3fc;font-size:11px;")
        hint.setWordWrap(True)
        add_layout.addWidget(hint)
        self.provider_inputs = {p: self.key_bulk_input for p in PROVIDERS}
        top_split.addWidget(add_group)

        # —— 右侧：申请入口与说明（可滚动）——
        links_group = QGroupBox("申请入口与说明")
        links_outer = QVBoxLayout(links_group)
        links_outer.setContentsMargins(8, 8, 8, 8)
        links_scroll = QScrollArea()
        links_scroll.setWidgetResizable(True)
        links_scroll.setFrameShape(QFrame.Shape.NoFrame)
        links_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        links_body = QWidget()
        links_body_layout = QVBoxLayout(links_body)
        links_body_layout.setContentsMargins(4, 2, 4, 4)
        links_label = QLabel(
            "<div style='line-height:1.5;font-size:12px'>"
            "<b>一、语音识别 ASR（密钥加在左侧）</b><br/>"
            "• <b>Groq</b> "
            "<a href='https://console.groq.com/keys' style='color:#60a5fa;'>申请密钥</a> · "
            "<a href='https://console.groq.com' style='color:#93c5fd;'>控制台</a><br/>"
            "• <b>Gemini</b>（识别+配音同一 Key）"
            "<a href='https://aistudio.google.com/api-keys' style='color:#60a5fa;'>API Keys</a> · "
            "<a href='https://aistudio.google.com/' style='color:#93c5fd;'>AI Studio</a><br/>"
            "• <b>ElevenLabs</b>（识别+多语 TTS）"
            "<a href='https://elevenlabs.io/app/settings/api-keys' style='color:#60a5fa;'>API Keys</a> · "
            "<a href='https://elevenlabs.io/app/voice-library' style='color:#93c5fd;'>音色库</a> · "
            "<a href='https://elevenlabs.io/' style='color:#93c5fd;'>官网</a><br/>"
            "• <b>Gladia</b> "
            "<a href='https://app.gladia.io/account' style='color:#60a5fa;'>账户/API</a> · "
            "<a href='https://docs.gladia.io/' style='color:#93c5fd;'>文档</a><br/><br/>"
            "<b>二、文字转语音 TTS</b><br/>"
            "• <b>微软 edge-tts</b>（免费、无需密钥；Reels 选「微软文字转语音」）"
            "<a href='https://speech.microsoft.com/portal' style='color:#60a5fa;'>试听</a> · "
            "<a href='https://learn.microsoft.com/azure/ai-services/speech-service/language-support' style='color:#93c5fd;'>语言列表</a><br/>"
            "• <b>Gemini 自然语音</b> "
            "<a href='https://ai.google.dev/gemini-api/docs/speech-generation' style='color:#60a5fa;'>文档</a><br/>"
            "• <b>ElevenLabs TTS</b> "
            "<a href='https://elevenlabs.io/docs/api-reference/text-to-speech' style='color:#60a5fa;'>TTS API</a> · "
            "也可用左侧「网页会话 Cookie」扣账号点数（多账户）<br/>"
            "• <b>Azure Speech</b>（商用可选）"
            "<a href='https://portal.azure.com/#create/Microsoft.CognitiveServicesSpeechServices' style='color:#60a5fa;'>创建资源</a> · "
            "<a href='https://speech.microsoft.com/' style='color:#93c5fd;'>官网</a><br/><br/>"
            "<b>三、本地识别</b>：设置与组件安装 Whisper，无需密钥。"
            "</div>"
        )
        links_label.setOpenExternalLinks(True)
        links_label.setWordWrap(True)
        links_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextBrowserInteraction | Qt.TextInteractionFlag.LinksAccessibleByMouse
        )
        links_label.setStyleSheet(
            "QLabel { background:#0b1830; color:#e2e8f0; padding:8px 10px; border-radius:6px; }"
        )
        links_label.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        links_body_layout.addWidget(links_label)
        links_body_layout.addStretch(1)
        links_scroll.setWidget(links_body)
        links_outer.addWidget(links_scroll)
        top_split.addWidget(links_group)

        # 左约 38% 添加区，右约 62% 说明
        top_split.setStretchFactor(0, 2)
        top_split.setStretchFactor(1, 3)
        top_split.setSizes([360, 560])
        # 限制上半区高度，把空间留给密钥列表
        top_wrap = QWidget()
        top_wrap_layout = QVBoxLayout(top_wrap)
        top_wrap_layout.setContentsMargins(0, 0, 0, 0)
        top_wrap_layout.addWidget(top_split)
        top_wrap.setMaximumHeight(260)
        top_wrap.setMinimumHeight(200)
        layout.addWidget(top_wrap, 0)

        # —— 下方：密钥列表（主要区域）——
        panel = QFrame()
        panel.setObjectName("panel")
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(10, 10, 10, 10)
        panel_layout.setSpacing(6)
        list_title = QLabel("已添加密钥列表")
        list_title.setStyleSheet("font-weight:700;color:#f1f5f9;")
        panel_layout.addWidget(list_title)
        self.key_table = QTableWidget(0, 7)
        self.key_table.setHorizontalHeaderLabels(
            ["服务", "密钥", "状态", "上次检测", "使用次数", "异常原因", "ID"]
        )
        header = self.key_table.horizontalHeader()
        for column in range(5):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self.key_table.setColumnHidden(6, True)
        self.key_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.key_table.setMinimumHeight(180)
        self.key_table.cellDoubleClicked.connect(lambda row, column: self._show_key_error(row))
        panel_layout.addWidget(self.key_table, 1)
        buttons = QHBoxLayout()
        check_selected = QPushButton("检测选中")
        check_selected.clicked.connect(self._check_selected_keys)
        check_all = QPushButton("检测全部")
        check_all.clicked.connect(self._check_all_keys)
        details = QPushButton("查看异常详情")
        details.clicked.connect(self._show_selected_key_error)
        toggle = QPushButton("启用 / 停用")
        toggle.clicked.connect(self._toggle_key)
        remove = QPushButton("删除选中")
        remove.clicked.connect(self._remove_key)
        buttons.addWidget(check_selected)
        buttons.addWidget(check_all)
        buttons.addWidget(details)
        buttons.addWidget(toggle)
        buttons.addStretch()
        buttons.addWidget(remove)
        panel_layout.addLayout(buttons)
        layout.addWidget(panel, 1)

        note = QLabel("安全提示：配置文件为本机明文保存，请勿共享该文件或整个用户配置目录。")
        note.setStyleSheet("color:#f59e0b;font-size:11px;")
        layout.addWidget(note)
        return page

    def _show_page(self, index):
        requested_index = index
        nav_index = index
        if index == 6 and hasattr(self, "settings_page"):
            # 旧的“密钥管理”入口统一落到设置页中的独立标签，避免重复顶栏入口。
            index = 7
            nav_index = 7
            self.settings_page.setCurrentWidget(self.key_settings_page)
        self.pages.setCurrentIndex(index)
        page_names={0:"首页",1:"格式转换",2:"智能剪辑",3:"Reels 编辑器",4:"批量重命名",5:"字幕提取",
                    6:"密钥管理",7:"设置与组件",8:"自动流水线",9:"帮助",10:"清除元数据",11:"文字转语音"}
        write_app_log(f"切换页面：{page_names.get(requested_index,requested_index)}","INFO","界面")
        for btn in self.nav_buttons:
            btn.setChecked(int(btn.property("pageIndex")) == nav_index)

    def _open_folder_in_batch_rename(self, folder):
        path=Path(str(folder)).expanduser()
        if not path.is_dir():
            QMessageBox.information(self,"文件夹不存在",f"无法加入批量重命名：\n{path}")
            return
        self.rename_page.set_input_folder(str(path.resolve()))
        # Copy the custom titles from dynamic_caption_page if they exist
        custom_titles = self.dynamic_caption_page.rename_custom_titles.toPlainText().strip()
        if custom_titles:
            self.rename_page.titles.setPlainText(custom_titles)
        else:
            self.rename_page.titles.clear()
        self._show_page(4)
        self.rename_page.input.setFocus()
        write_app_log(f"Reels 成品已加入批量重命名：{path.resolve()}","INFO","批量重命名")

    def _launch_tool(self, relative):
        if relative.startswith("page:"):
            self._show_page(int(relative.split(":", 1)[1]))

    def _add_media(self):
        files, _ = QFileDialog.getOpenFileNames(self, "选择视频或音频", "",
            "媒体文件 (*.mp4 *.mov *.mkv *.avi *.wmv *.webm *.m4v *.mp3 *.wav *.m4a *.flac *.aac *.ogg);;所有文件 (*.*)")
        self._add_media_paths(files)

    def _add_media_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "选择包含媒体的文件夹")
        if folder:
            self._add_media_paths([folder])

    def _media_files_in_folder(self, folder: Path):
        found = []
        try:
            for root, directories, files in os.walk(folder):
                directories.sort(key=natural_path_key)
                for name in sorted(files, key=natural_path_key):
                    path = Path(root) / name
                    if path.suffix.lower() in MEDIA_EXTENSIONS:
                        found.append(str(path.resolve()))
        except (OSError, PermissionError) as exc:
            self.media_source_hint.setText(f"部分路径无法读取：{exc}")
        return found

    def _add_media_paths(self, paths):
        candidates = []
        folder_count = 0
        for raw_path in paths:
            path = Path(raw_path)
            if path.is_dir():
                folder_count += 1
                candidates.extend(self._media_files_in_folder(path))
            elif path.is_file() and path.suffix.lower() in MEDIA_EXTENSIONS:
                candidates.append(str(path.resolve()))
        candidates.sort(key=natural_path_key)
        existing = {self.file_list.item(i).text() for i in range(self.file_list.count())}
        added = 0
        for path in candidates:
            if path not in existing:
                self.file_list.addItem(path); existing.add(path); added += 1
        if added:
            source = f"，来自 {folder_count} 个文件夹" if folder_count else ""
            self.media_source_hint.setText(f"新增 {added} 个{source}；共 {self.file_list.count()} 个")
        elif paths:
            self.media_source_hint.setText("没有发现新媒体（可能重复或格式不支持）")

    def _remove_selected_media(self):
        for index in self.file_list.selectedIndexes()[::-1]:
            self.file_list.takeItem(index.row())
        self.media_source_hint.setText(f"共 {self.file_list.count()} 个")



    def _pipeline_choose_files(self):
        files, _ = QFileDialog.getOpenFileNames(
            self, "选择原始视频", "", "视频 (*.mp4 *.mov *.mkv *.avi *.wmv *.webm *.m4v *.flv *.ts)")
        self._pipeline_add_paths(files)

    def _pipeline_choose_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "选择原始视频文件夹")
        if folder: self._pipeline_add_paths([folder])

    def _pipeline_add_paths(self, paths):
        video_extensions = {".mp4", ".mov", ".mkv", ".avi", ".wmv", ".webm", ".m4v", ".flv", ".ts"}
        candidates = []
        for raw in paths:
            path = Path(raw)
            if path.is_dir():
                candidates.extend(str(item.resolve()) for item in path.rglob("*")
                                  if item.is_file() and item.suffix.lower() in video_extensions)
            elif path.is_file() and path.suffix.lower() in video_extensions:
                candidates.append(str(path.resolve()))
        candidates.sort(key=natural_path_key)
        existing = {self.pipeline_files.item(i).text() for i in range(self.pipeline_files.count())}
        for path in candidates:
            if path not in existing: self.pipeline_files.addItem(path); existing.add(path)



    def _pipeline_choose_output(self):
        folder = QFileDialog.getExistingDirectory(self, "选择流水线输出目录", self.pipeline_output.text())
        if folder: self.pipeline_output.setText(folder)

    def _pipeline_cloud_toggled(self, checked):
        self.store.data["google_sync"]["enabled"] = bool(checked)
        self.store.save()
        self.pipeline_cloud_result.setText("云端同步：已开启" if checked else "云端同步：已关闭")

    def _save_pipeline_sync_profile(self):
        current_name=self.pipeline_sync_profile.currentData()
        if current_name:
            name=current_name
        else:
            name,ok=QInputDialog.getText(self,"保存同步方案","方案名称：",text="方案1")
            if not ok or not name.strip(): return
            name=name.strip()
        config=self._selected_sync_config(); config["enabled"]=self.pipeline_cloud_check.isChecked()
        config.pop("sync_profiles",None); config.pop("active_sync_profile",None)
        root=self.store.data.setdefault("google_sync",{}); profiles=dict(root.get("sync_profiles",{})); profiles[name]=config
        root["sync_profiles"]=profiles; root["active_sync_profile"]=name; self.store.save()
        if hasattr(self,"google_settings_page"): self.google_settings_page.load_current()
        self._refresh_pipeline_profiles(); index=self.pipeline_sync_profile.findData(name)
        if index>=0: self.pipeline_sync_profile.setCurrentIndex(index)
        QMessageBox.information(self,"方案已保存",f"当前写入 Sheet 和本次选择已同步保存到方案“{name}”。")

    def _open_google_settings(self):
        self._show_page(7)
        self.settings_page.setCurrentWidget(self.google_settings_page)

    def _refresh_pipeline_profiles(self):
        if not hasattr(self, "pipeline_sync_profile"): return
        config = self.store.data.get("google_sync", {}); profiles = config.get("sync_profiles", {})
        current = self.pipeline_sync_profile.currentText() or config.get("active_sync_profile", "")
        self.pipeline_sync_profile.blockSignals(True); self.pipeline_sync_profile.clear()
        self.pipeline_sync_profile.addItem("使用当前设置", "")
        for name in profiles: self.pipeline_sync_profile.addItem(name, name)
        index = self.pipeline_sync_profile.findData(current)
        if index < 0: index = self.pipeline_sync_profile.findData(config.get("active_sync_profile", ""))
        self.pipeline_sync_profile.setCurrentIndex(max(0, index)); self.pipeline_sync_profile.blockSignals(False)
        self._pipeline_profile_changed(self.pipeline_sync_profile.currentText())

    def _selected_sync_config(self):
        base = dict(self.store.data.get("google_sync", {})); name = self.pipeline_sync_profile.currentData() if hasattr(self, "pipeline_sync_profile") else ""
        profile = base.get("sync_profiles", {}).get(name)
        if profile:
            preserved_profiles = base.get("sync_profiles", {}); base.update(dict(profile)); base["sync_profiles"] = preserved_profiles; base["active_sync_profile"] = name
        profile_key=name or "__current__"
        if getattr(self,"_pipeline_runtime_profile",None)==profile_key:
            if getattr(self,"pipeline_runtime_sheet",""): base["sheet_name"]=self.pipeline_runtime_sheet
            fields=[]
            for item in base.get("variable_fields",[]):
                updated=dict(item); updated["selected"]=self.pipeline_runtime_values.get(item.get("field",""),item.get("selected","")); fields.append(updated)
            base["variable_fields"]=fields
        return base

    def _reels_sync_profiles(self):
        config = self.store.data.get("google_sync", {})
        return list(config.get("sync_profiles", {}).keys()), config.get("active_sync_profile", "")

    def _start_reels_cloud_sync(self, files, records, profile_name=""):
        if profile_name and hasattr(self, "pipeline_sync_profile"):
            index = self.pipeline_sync_profile.findData(profile_name)
            if index >= 0: self.pipeline_sync_profile.setCurrentIndex(index)
        self._start_cloud_upload(files, records=records)

    def _pipeline_profile_changed(self, _text):
        if not hasattr(self, "pipeline_profile_hint"): return
        name=self.pipeline_sync_profile.currentData(); profile_key=name or "__current__"
        base=dict(self.store.data.get("google_sync",{})); profile_config=base.get("sync_profiles",{}).get(name)
        if profile_config: base.update(dict(profile_config))
        self._pipeline_runtime_profile=profile_key
        self.pipeline_runtime_sheet=base.get("sheet_name","")
        self.pipeline_runtime_values={item.get("field",""):item.get("selected","") for item in base.get("variable_fields",[])}
        while self.pipeline_variable_form.count():
            item=self.pipeline_variable_form.takeAt(0)
            if item.widget(): item.widget().deleteLater()
        self.pipeline_target_sheet=NoWheelComboBox(); self.pipeline_target_sheet.setEditable(True); self.pipeline_target_sheet.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        sheet_names=[str(value) for value in base.get("available_sheet_names",[]) if str(value).strip()]
        current_sheet=base.get("sheet_name","")
        if current_sheet and current_sheet not in sheet_names: sheet_names.insert(0,current_sheet)
        self.pipeline_target_sheet.addItems(sheet_names); self.pipeline_target_sheet.setCurrentText(current_sheet)
        self.pipeline_target_sheet.lineEdit().setPlaceholderText("选择或输入写入 Sheet 名称")
        self.pipeline_target_sheet.currentTextChanged.connect(self._pipeline_sheet_changed)
        self.pipeline_variable_form.addRow("写入 Sheet",self.pipeline_target_sheet)
        for item in base.get("variable_fields",[]):
            field=item.get("field","选择项"); options=[str(v) for v in item.get("options",[]) if str(v).strip()]
            combo=NoWheelComboBox(); combo.setEditable(False); combo.addItem("请选择…",""); combo.addItems(options)
            selected=item.get("selected",""); index=combo.findText(selected); combo.setCurrentIndex(index if index>=0 else 0)
            combo.currentTextChanged.connect(lambda value,f=field:self._pipeline_variable_changed(f,value))
            combo.setEnabled(bool(options)); combo.setToolTip("选项来自 Google 配置 Sheet" if options else "请先在设置与组件中从配置 Sheet 刷新选项")
            self.pipeline_variable_form.addRow(f"{field}（{item.get('column','')}列）",combo)
        self.pipeline_variable_group.setVisible(bool(base.get("write_sheet") or base.get("variable_fields",[])))
        self._update_pipeline_profile_hint()

    def _pipeline_variable_changed(self, field, value):
        self.pipeline_runtime_values[field]="" if value=="请选择…" else value
        self._update_pipeline_profile_hint()

    def _pipeline_sheet_changed(self, value):
        self.pipeline_runtime_sheet=value.strip()
        self._update_pipeline_profile_hint()

    def _update_pipeline_profile_hint(self):
        config = self._selected_sync_config(); profile = self.pipeline_sync_profile.currentData() or "当前设置"
        sheet = config.get("sheet_name", "") or "未填写 Sheet"
        table_id = extract_google_id(config.get("spreadsheet_id", "")) or "未填写表格"
        selected = [f"{item.get('field')}={item.get('selected')}" for item in config.get("variable_fields", []) if item.get("selected")]
        extra = "；" + "，".join(selected) if selected else ""
        self.pipeline_profile_hint.setText(f"{profile} → 表格 {table_id} / Sheet：{sheet}{extra}")

    def _open_google_sync_dialog(self):
        config = dict(self.store.data["google_sync"])
        dialog = QDialog(self); dialog.setWindowTitle("Google 云端同步配置"); dialog.resize(680, 760)
        root = QVBoxLayout(dialog)
        note = QLabel("使用服务账号 JSON。请先把 Drive 父文件夹和 Google 表格共享给 JSON 中的 client_email。\n"
                      "流水线只上传“03_重命名成品”中的最终视频，中间片段和字幕不会上传。")
        note.setWordWrap(True); note.setStyleSheet("color:#7dd3fc;"); root.addWidget(note)
        scroll = QScrollArea(); scroll.setWidgetResizable(True); container = QWidget(); form = QFormLayout(container)
        json_edit = QLineEdit(config.get("json_path", "")); json_row = QHBoxLayout(); json_row.addWidget(json_edit)
        browse_json = QPushButton("选择 JSON…")
        browse_json.clicked.connect(lambda: json_edit.setText(QFileDialog.getOpenFileName(
            dialog, "选择 Google 服务账号 JSON", "", "JSON (*.json)")[0] or json_edit.text()))
        json_row.addWidget(browse_json); json_widget = QWidget(); json_widget.setLayout(json_row)
        form.addRow("服务账号 JSON", json_widget)
        parent_edit = QLineEdit(config.get("parent_folder", "")); parent_edit.setPlaceholderText("Drive 父文件夹 ID 或链接")
        form.addRow("云端父文件夹", parent_edit)
        auth_row = QHBoxLayout(); auth_status = QLabel("尚未检查授权"); auth_status.setWordWrap(True)
        authorize = QPushButton("授权 / 检查权限"); auth_row.addWidget(auth_status, 1); auth_row.addWidget(authorize)
        auth_widget = QWidget(); auth_widget.setLayout(auth_row); form.addRow("Google 权限", auth_widget)
        def authorize_google():
            temporary = dict(config); temporary.update({"json_path": json_edit.text().strip(),
                                                         "parent_folder": parent_edit.text().strip()})
            authorize.setEnabled(False); auth_status.setText("正在授权并检查权限…")
            QApplication.processEvents()
            try:
                identity = test_google_authorization(temporary, interactive=True)
                auth_status.setText(f"授权成功：{identity}"); auth_status.setStyleSheet("color:#86efac;")
            except Exception as exc:
                auth_status.setText(f"授权失败：{exc}"); auth_status.setStyleSheet("color:#fca5a5;")
                QMessageBox.warning(dialog, "Google 授权失败", str(exc))
            finally: authorize.setEnabled(True)
        authorize.clicked.connect(authorize_google)
        mode_combo = QComboBox(); mode_combo.addItems(["视频名称", "自定义名称"])
        mode_combo.setCurrentText(config.get("folder_mode", "视频名称")); form.addRow("任务文件夹命名", mode_combo)
        custom_name = QLineEdit(config.get("custom_folder_name", "")); custom_name.setPlaceholderText("选择自定义名称时使用")
        form.addRow("自定义名称", custom_name)
        public_check = QCheckBox("允许知道链接的用户查看云端任务文件夹")
        public_check.setChecked(config.get("public_link", False)); form.addRow("共享权限", public_check)
        sheet_check = QCheckBox("上传完成后写入 Google Sheets")
        sheet_check.setChecked(config.get("write_sheet", False)); form.addRow("表格同步", sheet_check)
        profiles = {name: dict(value) for name, value in config.get("sheet_profiles", {}).items()}
        profile_row = QHBoxLayout(); profile_combo = QComboBox(); profile_combo.addItem("选择已保存表格方案…")
        profile_combo.addItems(profiles.keys())
        save_profile = QPushButton("保存当前方案"); delete_profile = QPushButton("删除方案")
        profile_row.addWidget(profile_combo, 1); profile_row.addWidget(save_profile); profile_row.addWidget(delete_profile)
        profile_widget = QWidget(); profile_widget.setLayout(profile_row); form.addRow("表格方案", profile_widget)
        spreadsheet = QLineEdit(config.get("spreadsheet_id", "")); spreadsheet.setPlaceholderText("表格 ID 或完整链接")
        sheet_name = QLineEdit(config.get("sheet_name", "")); sheet_name.setPlaceholderText("例如：AS-批量视频版权表")
        insert_row = QSpinBox(); insert_row.setRange(1, 100000); insert_row.setValue(int(config.get("insert_row", 4)))
        form.addRow("表格 ID", spreadsheet); form.addRow("Sheet 名称", sheet_name); form.addRow("数据插入行", insert_row)

        variable_fields = [dict(item) for item in config.get("variable_fields", [])]
        variable_group = QGroupBox("本次上传选择（每次上传可重新选择）")
        variable_layout = QVBoxLayout(variable_group); variable_rows = QFormLayout(); variable_layout.addLayout(variable_rows)
        variable_combos = []
        def clear_form_layout(layout_to_clear):
            while layout_to_clear.rowCount(): layout_to_clear.removeRow(0)
        def read_variable_fields():
            result = []
            for index, item in enumerate(variable_fields):
                updated_item = dict(item)
                if index < len(variable_combos): updated_item["selected"] = variable_combos[index].currentText()
                result.append(updated_item)
            return result
        def rebuild_variable_rows():
            clear_form_layout(variable_rows); variable_combos.clear()
            for item in variable_fields:
                combo = QComboBox(); options = [str(value) for value in item.get("options", []) if str(value).strip()]
                combo.addItems(options); combo.setEditable(True)
                combo.setCurrentText(str(item.get("selected", options[0] if options else "")))
                variable_combos.append(combo)
                variable_rows.addRow(f"{item.get('field', '选择项')}（{item.get('column', '')}列）", combo)
            if not variable_fields:
                variable_rows.addRow(QLabel("尚未配置非固定字段；点击右侧按钮添加。"))
        configure_variables = QPushButton("配置下拉字段和选项")
        variable_layout.addWidget(configure_variables, 0, Qt.AlignmentFlag.AlignRight)
        form.addRow(variable_group)
        def configure_variable_fields():
            nonlocal variable_fields
            editor = QDialog(dialog); editor.setWindowTitle("配置每次上传需要选择的字段"); editor.resize(650, 460)
            editor_layout = QVBoxLayout(editor)
            hint = QLabel("这些字段不会固定在表格方案中；每次上传前从下拉框选择。选项用 | 分隔。")
            hint.setWordWrap(True); editor_layout.addWidget(hint)
            table = QTableWidget(0, 3); table.setHorizontalHeaderLabels(["字段名称", "写入列", "下拉选项（用 | 分隔）"])
            table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
            table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
            table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
            for item in read_variable_fields():
                row = table.rowCount(); table.insertRow(row)
                table.setItem(row, 0, QTableWidgetItem(str(item.get("field", "选择项"))))
                table.setItem(row, 1, QTableWidgetItem(str(item.get("column", ""))))
                table.setItem(row, 2, QTableWidgetItem(" | ".join(item.get("options", []))))
            editor_layout.addWidget(table, 1)
            edit_buttons = QHBoxLayout(); add_row = QPushButton("新增字段"); remove_row = QPushButton("删除选中")
            add_row.clicked.connect(lambda: (table.insertRow(table.rowCount()),
                                              table.setItem(table.rowCount()-1, 0, QTableWidgetItem("选择项")),
                                              table.setItem(table.rowCount()-1, 1, QTableWidgetItem("")),
                                              table.setItem(table.rowCount()-1, 2, QTableWidgetItem(""))))
            def remove_rows():
                for row in sorted({index.row() for index in table.selectedIndexes()}, reverse=True): table.removeRow(row)
            remove_row.clicked.connect(remove_rows); edit_buttons.addWidget(add_row); edit_buttons.addWidget(remove_row); edit_buttons.addStretch()
            editor_layout.addLayout(edit_buttons)
            editor_buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
            editor_buttons.accepted.connect(editor.accept); editor_buttons.rejected.connect(editor.reject)
            editor_layout.addWidget(editor_buttons)
            if editor.exec() != QDialog.DialogCode.Accepted: return
            updated_fields = []
            for row in range(table.rowCount()):
                field = table.item(row, 0).text().strip() if table.item(row, 0) else ""
                column = table.item(row, 1).text().strip().upper() if table.item(row, 1) else ""
                options_text = table.item(row, 2).text() if table.item(row, 2) else ""
                options = [value.strip() for value in options_text.split("|") if value.strip()]
                if field and column: updated_fields.append({"field": field, "column": column,
                                                             "options": options, "selected": options[0] if options else ""})
            variable_fields = updated_fields; rebuild_variable_rows()
        configure_variables.clicked.connect(configure_variable_fields); rebuild_variable_rows()

        columns_group = QGroupBox("字段与列映射（名称、列、填写内容合并配置）")
        columns_layout = QVBoxLayout(columns_group)
        mapping_table = QTableWidget(0, 3)
        mapping_table.setHorizontalHeaderLabels(["字段名称", "写入列", "固定内容 / 自动来源"])
        mapping_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        mapping_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        mapping_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        mapping_table.setMinimumHeight(330); mapping_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        source_labels = {"date": "自动：当天日期", "file": "自动：文件名与云端链接",
                         "chinese": "自动：中文字幕", "original": "自动：识别原文/葡语",
                         "folder": "自动：云端任务文件夹链接"}
        def load_mapping_table(mappings):
            mapping_table.setRowCount(0)
            for mapping in mappings:
                row = mapping_table.rowCount(); mapping_table.insertRow(row)
                field_item = QTableWidgetItem(str(mapping.get("field", "自定义字段")))
                source = mapping.get("source", "static"); field_item.setData(Qt.ItemDataRole.UserRole, source)
                column_item = QTableWidgetItem(str(mapping.get("column", "")))
                value_item = QTableWidgetItem(source_labels.get(source, str(mapping.get("value", ""))))
                if source != "static":
                    value_item.setFlags(value_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                    value_item.setForeground(QColor("#7dd3fc"))
                mapping_table.setItem(row, 0, field_item); mapping_table.setItem(row, 1, column_item)
                mapping_table.setItem(row, 2, value_item)
        def read_mapping_table():
            mappings = []
            for row in range(mapping_table.rowCount()):
                field_item = mapping_table.item(row, 0); column_item = mapping_table.item(row, 1)
                value_item = mapping_table.item(row, 2); source = field_item.data(Qt.ItemDataRole.UserRole) or "static"
                column = column_item.text().strip().upper() if column_item else ""
                if not column: continue
                mappings.append({"field": field_item.text().strip() or "自定义字段", "column": column,
                                 "source": source, "value": value_item.text() if source == "static" and value_item else ""})
            return mappings
        load_mapping_table(config.get("sheet_mappings", DEFAULT_SHEET_MAPPINGS))
        mapping_buttons = QHBoxLayout()
        add_mapping = QPushButton("新增固定字段"); delete_mapping = QPushButton("删除选中字段")
        reset_mapping = QPushButton("恢复默认映射")
        def add_mapping_row():
            row = mapping_table.rowCount(); mapping_table.insertRow(row)
            field = QTableWidgetItem("自定义字段"); field.setData(Qt.ItemDataRole.UserRole, "static")
            mapping_table.setItem(row, 0, field); mapping_table.setItem(row, 1, QTableWidgetItem(""))
            mapping_table.setItem(row, 2, QTableWidgetItem("")); mapping_table.setCurrentCell(row, 0)
        def delete_mapping_rows():
            for row in sorted({index.row() for index in mapping_table.selectedIndexes()}, reverse=True):
                mapping_table.removeRow(row)
        add_mapping.clicked.connect(add_mapping_row); delete_mapping.clicked.connect(delete_mapping_rows)
        reset_mapping.clicked.connect(lambda: load_mapping_table(DEFAULT_SHEET_MAPPINGS))
        mapping_buttons.addWidget(add_mapping); mapping_buttons.addWidget(delete_mapping); mapping_buttons.addStretch()
        mapping_buttons.addWidget(reset_mapping); columns_layout.addWidget(mapping_table); columns_layout.addLayout(mapping_buttons)
        form.addRow(columns_group)

        def current_sheet_profile():
            return {"spreadsheet_id": spreadsheet.text().strip(), "sheet_name": sheet_name.text().strip(),
                    "insert_row": insert_row.value(), "sheet_mappings": read_mapping_table(),
                    "variable_fields": read_variable_fields()}
        def apply_sheet_profile(name):
            nonlocal variable_fields
            profile = profiles.get(name)
            if not profile: return
            spreadsheet.setText(profile.get("spreadsheet_id", "")); sheet_name.setText(profile.get("sheet_name", ""))
            insert_row.setValue(int(profile.get("insert_row", 4)))
            load_mapping_table(profile.get("sheet_mappings", DEFAULT_SHEET_MAPPINGS))
            variable_fields = [dict(item) for item in profile.get("variable_fields", [])]
            rebuild_variable_rows()
        def save_current_profile():
            default_name = sheet_name.text().strip() or "表格方案"
            name, ok = QInputDialog.getText(dialog, "保存表格方案", "方案名称：", text=default_name)
            if not ok: return
            name = name.strip()
            if not name:
                QMessageBox.information(dialog, "无法保存", "请输入方案名称。")
                return
            try:
                profiles[name] = current_sheet_profile()
                # “保存当前方案”应立即持久化，不要求用户再点击弹窗底部的 Save。
                google_config = self.store.data.setdefault("google_sync", {})
                google_config["sheet_profiles"] = {key: dict(value) for key, value in profiles.items()}
                google_config["active_sheet_profile"] = name
                self.store.save()
                if profile_combo.findText(name) < 0: profile_combo.addItem(name)
                profile_combo.setCurrentText(name)
                QMessageBox.information(dialog, "保存成功", f"表格方案“{name}”已保存。")
            except Exception as exc:
                QMessageBox.critical(dialog, "保存方案失败", f"无法写入配置：\n{exc}")
        def delete_current_profile():
            name = profile_combo.currentText()
            if name in profiles:
                try:
                    del profiles[name]
                    google_config = self.store.data.setdefault("google_sync", {})
                    google_config["sheet_profiles"] = {key: dict(value) for key, value in profiles.items()}
                    if google_config.get("active_sheet_profile") == name:
                        google_config["active_sheet_profile"] = ""
                    self.store.save()
                    profile_combo.removeItem(profile_combo.currentIndex())
                    QMessageBox.information(dialog, "删除成功", f"表格方案“{name}”已删除。")
                except Exception as exc:
                    QMessageBox.critical(dialog, "删除方案失败", f"无法写入配置：\n{exc}")
        profile_combo.currentTextChanged.connect(apply_sheet_profile)
        save_profile.clicked.connect(save_current_profile); delete_profile.clicked.connect(delete_current_profile)
        active_profile = config.get("active_sheet_profile", "")
        if active_profile in profiles: profile_combo.setCurrentText(active_profile)
        scroll.setWidget(container); root.addWidget(scroll, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept); buttons.rejected.connect(dialog.reject); root.addWidget(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted: return False
        updated = {
            "enabled": self.pipeline_cloud_check.isChecked(), "json_path": json_edit.text().strip(),
            "parent_folder": parent_edit.text().strip(), "folder_mode": mode_combo.currentText(),
            "custom_folder_name": custom_name.text().strip(), "public_link": public_check.isChecked(),
            "write_sheet": sheet_check.isChecked(), "spreadsheet_id": spreadsheet.text().strip(),
            "sheet_name": sheet_name.text().strip(), "insert_row": insert_row.value(),
            "sheet_mappings": read_mapping_table(), "sheet_profiles": profiles,
            "variable_fields": read_variable_fields(),
            "active_sheet_profile": profile_combo.currentText() if profile_combo.currentText() in profiles else "",
            "mapping_ui_version": 2,
        }
        self.store.data["google_sync"] = updated; self.store.save()
        return True

    def _pipeline_start(self):
        sources = [self.pipeline_files.item(i).text() for i in range(self.pipeline_files.count())]
        if not sources:
            QMessageBox.information(self, "没有视频", "请先添加 Canva、HeyGen 或其他来源的视频。")
            return
        if self.thread:
            try:
                if self.thread.isRunning():
                    QMessageBox.information(self, "任务进行中", "请等待当前任务结束。")
                    return
            except RuntimeError:
                self.thread = None
        selected = self.pipeline_provider.currentText()
        provider = self._resolve_provider() if selected == AUTO_PROVIDER else selected
        if provider != LOCAL_PROVIDER and not self.store.has_candidates(provider):
            QMessageBox.information(self, "缺少密钥", f"{provider} 没有可用密钥，请先添加并检测。")
            self._show_page(6); return
        try: ffmpeg = self._find_ffmpeg()
        except Exception as exc: QMessageBox.critical(self, "缺少组件", str(exc)); return
        cloud_config = self._selected_sync_config()
        cloud_config["enabled"] = self.pipeline_cloud_check.isChecked()
        if cloud_config["enabled"]:
            if not Path(cloud_config.get("json_path", "")).is_file() or not extract_google_id(cloud_config.get("parent_folder", "")):
                QMessageBox.warning(self, "Google 同步配置不完整",
                                    "请配置有效的服务账号 JSON 和 Drive 父文件夹 ID/链接。")
                self._open_google_settings(); return
            if cloud_config.get("write_sheet") and (not extract_google_id(cloud_config.get("spreadsheet_id", ""))
                                                     or not cloud_config.get("sheet_name", "").strip()):
                QMessageBox.warning(self, "表格配置不完整", "请填写 Google 表格 ID 和 Sheet 名称。")
                self._open_google_settings(); return
        model = self.store.data["models"].get(provider, DEFAULT_MODELS[provider])
        self.subtitle_results.clear(); self.result_combo.clear(); self.result_combo.addItem(ALL_RESULTS_LABEL)
        self.pipeline_titles.clear(); self.pipeline_log.clear(); self.pipeline_progress.setValue(0)
        self.thread = QThread(self)
        audio_mode = (
            self.pipeline_audio_mode.currentText()
            if hasattr(self, "pipeline_audio_mode") else "仅视频原声"
        )
        extras = {
            "audio_mode": audio_mode,
            "bgm_path": self.pipeline_bgm_path.text().strip() if hasattr(self, "pipeline_bgm_path") else "",
            "bgm_volume": int(self.pipeline_bgm_volume.value()) if hasattr(self, "pipeline_bgm_volume") else 25,
            "bgm_random": bool(
                hasattr(self, "pipeline_bgm_random") and self.pipeline_bgm_random.isChecked()
            ),
            "watermark_enabled": bool(
                hasattr(self, "pipeline_wm_enable") and self.pipeline_wm_enable.isChecked()
            ),
            "watermark_path": self.pipeline_wm_path.text().strip() if hasattr(self, "pipeline_wm_path") else "",
            "wm_opacity": int(self.pipeline_wm_opacity.value()) if hasattr(self, "pipeline_wm_opacity") else 100,
            "wm_mode": "9:16 全屏覆盖",
        }
        self.worker = PipelineWorker(
            self.store, sources, self.pipeline_output.text(), self.pipeline_threshold.value(),
            provider, model, self._pipeline_language_code(), ffmpeg,
            self.pipeline_prefix.text(), self.pipeline_date.text(), self.pipeline_suffix.text(),
            self.pipeline_start.value(), self.pipeline_padding.value(), cloud_config,
            self.pipeline_resume_check.isChecked(), extras=extras)
        self.worker.moveToThread(self.thread); self.thread.started.connect(self.worker.run)
        self.worker.log.connect(self.pipeline_log.appendPlainText)
        self.worker.progress.connect(self.pipeline_progress.setValue)
        self.worker.result_ready.connect(self._subtitle_result_ready)
        self.worker.titles_ready.connect(self._pipeline_titles_ready)
        self.worker.cloud_ready.connect(self._pipeline_cloud_ready)
        self.worker.cloud_failed.connect(self._pipeline_cloud_failed)
        self.worker.cloud_sheet_pending.connect(self._pipeline_sheet_pending)
        self.worker.finished.connect(self._pipeline_done); self.worker.finished.connect(self.thread.quit)
        self.thread.finished.connect(self._thread_ended); self.thread.finished.connect(self.thread.deleteLater)
        self.pipeline_start_btn.setEnabled(False); self.pipeline_stop.setEnabled(True); self.thread.start()

    def _pipeline_cancel(self):
        if self.worker and hasattr(self.worker, "cancel"): self.worker.cancel()

    def _pipeline_titles_ready(self, clips_dir, titles):
        self.pipeline_titles.setPlainText("\n".join(titles))
        self.rename_page.input.setText(clips_dir)
        self.rename_page.output.setText(str(Path(clips_dir).parent))
        self.rename_page.task_name.setText("04_手动调整成品")
        self.rename_page.titles.setPlainText("\n".join(titles))
        self.rename_page.update_preview()

    def _pipeline_cloud_ready(self, folder_url, summary):
        self.pending_upload_files = []; self.pipeline_retry_upload.setEnabled(False)
        self.pending_upload_records=[]
        self.pending_sheet_uploads=[]; self.pending_sheet_folder_url=""; self.pipeline_continue_sheet.setEnabled(False)
        self.store.data.pop("cloud_resume",None); self.store.save()
        self.pipeline_cloud_result.setText(
            f'云端同步完成：{summary}<br><a href="{folder_url}">打开 Google Drive 文件夹</a>')
        if hasattr(self, "dynamic_caption_page"):
            sheet_url = f"https://docs.google.com/spreadsheets/d/{extract_google_id(self._selected_sync_config().get('spreadsheet_id', ''))}"
            self.dynamic_caption_page._append_run_log(
                f"[云端同步成功] {summary}\n"
                f"Google Drive 文件夹链接: {folder_url}\n"
                f"Google Sheets 表格链接: {sheet_url}"
            )
            self.dynamic_caption_page.cloud_sync_hint.setText(
                f'云端同步完成：{summary}<br>'
                f'<a href="{folder_url}">[打开 Google Drive]</a> | '
                f'<a href="{sheet_url}">[打开 Google Sheets]</a>'
            )

    def _pipeline_sheet_pending(self, folder_url, uploaded, error):
        self.pending_upload_files=[]; self.pipeline_retry_upload.setEnabled(False)
        self.pending_upload_records=[]
        self.pending_sheet_uploads=list(uploaded); self.pending_sheet_folder_url=folder_url
        self.store.data["cloud_resume"]={"status":"sheet_pending","folder_url":folder_url,"uploads":list(uploaded)}
        self.store.save()
        self.pipeline_continue_sheet.setEnabled(bool(self.pending_sheet_uploads))
        self.pipeline_cloud_result.setStyleSheet("color:#fbbf24;padding:4px;")
        self.pipeline_cloud_result.setText(
            f'视频已上传成功，但写入表格失败：{error}<br><a href="{folder_url}">打开 Google Drive 文件夹</a><br>修正配置后点击“继续填表”，不会重新上传视频。')
        if hasattr(self, "dynamic_caption_page"):
            self.dynamic_caption_page._append_run_log(
                f"[云端同步未完全成功] 视频已全部上传成功，但写入 Google Sheets 发生错误：{error}\n"
                f"Google Drive 文件夹链接: {folder_url}"
            )
            self.dynamic_caption_page.cloud_sync_hint.setText(
                f'已上传但写入表格失败：{error}<br>'
                f'<a href="{folder_url}">[打开 Google Drive]</a>'
            )

    def _pipeline_cloud_failed(self, final_dir, error):
        if hasattr(self, "dynamic_caption_page"):
            self.dynamic_caption_page._append_run_log(f"[云端同步失败] 同步发生错误：{error}")
            self.dynamic_caption_page.cloud_sync_hint.setText(f"云端同步失败：{error}")
        self.pending_sheet_uploads=[]; self.pending_sheet_folder_url=""; self.pipeline_continue_sheet.setEnabled(False)
        video_extensions = {".mp4", ".mov", ".mkv", ".avi", ".wmv", ".webm", ".m4v", ".flv", ".ts"}
        self.pending_upload_files = [str(path) for path in sorted(Path(final_dir).iterdir(),
                                      key=lambda path: natural_path_key(path.name))
                                     if path.is_file() and path.suffix.lower() in video_extensions]
        self.pipeline_retry_upload.setEnabled(bool(self.pending_upload_files))
        self.store.data["cloud_resume"]={"status":"upload_pending","files":list(self.pending_upload_files),
                                         "records":list(self.pending_upload_records)}
        self.store.save()
        self.pipeline_cloud_result.setText(f"云端同步失败，但本地视频已处理完成：{error}<br>可点击“继续上传”。")
        self.pipeline_cloud_result.setStyleSheet("color:#fca5a5;padding:4px;")

    def _manual_upload_files(self):
        files, _ = QFileDialog.getOpenFileNames(self, "选择需要上传的重命名成品", "",
                                                 "视频 (*.mp4 *.mov *.mkv *.avi *.wmv *.webm *.m4v *.flv *.ts)")
        if files: self._start_cloud_upload(files)

    def _manual_upload_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "选择重命名成品目录")
        if not folder: return
        selected_folder = Path(folder)
        if selected_folder.name != "03_重命名成品":
            product_folders = sorted((path for path in selected_folder.rglob("03_重命名成品") if path.is_dir()),
                                     key=lambda path: path.stat().st_mtime, reverse=True)
            if product_folders:
                selected_folder = product_folders[0]
                self.pipeline_log.appendPlainText(f"已自动限定为重命名成品目录：{selected_folder}")
        extensions = {".mp4", ".mov", ".mkv", ".avi", ".wmv", ".webm", ".m4v", ".flv", ".ts"}
        files = [str(path) for path in sorted(selected_folder.rglob("*"), key=lambda path: natural_path_key(path.name))
                 if path.is_file() and path.suffix.lower() in extensions]
        if not files: QMessageBox.information(self, "没有成品", "所选目录中没有找到视频文件。")
        else: self._start_cloud_upload(files)

    def _retry_cloud_upload(self):
        if self.pending_upload_files: self._start_cloud_upload(self.pending_upload_files,self.pending_upload_records or None)

    def _continue_sheet_write(self):
        if not self.pending_sheet_uploads or not self.pending_sheet_folder_url: return
        if self.cloud_thread:
            try:
                if self.cloud_thread.isRunning():
                    QMessageBox.information(self,"任务进行中","请等待当前云端任务结束。")
                    return
            except RuntimeError: self.cloud_thread=None
        config=self._selected_sync_config(); config["enabled"]=True; config["write_sheet"]=True
        if not extract_google_id(config.get("spreadsheet_id","")) or not config.get("sheet_name","").strip():
            QMessageBox.warning(self,"表格配置不完整","请选择写入 Sheet 并确认表格 ID。")
            return
        self.cloud_thread=QThread(self); self.cloud_worker=SheetFillWorker(config,list(self.pending_sheet_uploads),self.pending_sheet_folder_url)
        self.cloud_worker.moveToThread(self.cloud_thread); self.cloud_thread.started.connect(self.cloud_worker.run)
        self.cloud_worker.log.connect(self.pipeline_log.appendPlainText)
        if hasattr(self, "dynamic_caption_page"):
            self.cloud_worker.log.connect(lambda msg: self.dynamic_caption_page._append_run_log(f"[云端同步] {msg}"))
        self.cloud_worker.finished.connect(self._sheet_fill_done)
        self.cloud_worker.finished.connect(self.cloud_thread.quit); self.cloud_thread.finished.connect(self._cloud_thread_ended); self.cloud_thread.finished.connect(self.cloud_thread.deleteLater)
        self.pipeline_continue_sheet.setEnabled(False); self.pipeline_stop_upload.setEnabled(False)
        self.pipeline_cloud_result.setStyleSheet("color:#7dd3fc;padding:4px;"); self.pipeline_cloud_result.setText("正在继续填写 Google Sheets，不会重新上传视频…")
        self.cloud_thread.start()

    def _sheet_fill_done(self, ok, folder_url, message):
        if ok:
            self._mark_pending_sheet_complete(folder_url)
            self.pending_sheet_uploads=[]; self.pending_sheet_folder_url=""; self.pipeline_continue_sheet.setEnabled(False)
            self.store.data.pop("cloud_resume",None); self.store.save()
            self.pipeline_cloud_result.setStyleSheet("color:#86efac;padding:4px;")
            self.pipeline_cloud_result.setText(f'{message}<br><a href="{folder_url}">打开 Google Drive 文件夹</a>')
            if hasattr(self, "dynamic_caption_page"):
                sheet_url = f"https://docs.google.com/spreadsheets/d/{extract_google_id(self._selected_sync_config().get('spreadsheet_id', ''))}"
                self.dynamic_caption_page._append_run_log(
                    f"[云端同步成功] {message}\n"
                    f"Google Drive 文件夹链接: {folder_url}\n"
                    f"Google Sheets 表格链接: {sheet_url}"
                )
                self.dynamic_caption_page.cloud_sync_hint.setText(
                    f'云端同步完成：{message}<br>'
                    f'<a href="{folder_url}">[打开 Google Drive]</a> | '
                    f'<a href="{sheet_url}">[打开 Google Sheets]</a>'
                )
        else:
            self.pipeline_continue_sheet.setEnabled(bool(self.pending_sheet_uploads))
            self.pipeline_cloud_result.setStyleSheet("color:#fca5a5;padding:4px;")
            self.pipeline_cloud_result.setText(f"继续填表失败：{message}<br>修正配置后可以再次点击继续填表。")
            if hasattr(self, "dynamic_caption_page"):
                self.dynamic_caption_page._append_run_log(f"[云端同步失败] 填表失败：{message}")

    def _mark_pending_sheet_complete(self, folder_url):
        try:
            root=Path(self.pipeline_output.text())
            for checkpoint in root.rglob("pipeline_checkpoint.json") if root.is_dir() else []:
                state=read_json_file(checkpoint,{})
                if state.get("status")=="sheet_pending" and state.get("cloud_url")==folder_url:
                    state["status"]="completed"; state.pop("pending_sheet_uploads",None); state.pop("last_error",None); atomic_write_json(checkpoint,state)
        except Exception:
            pass

    def _start_cloud_upload(self, files, records=None):
        if self.cloud_thread:
            try:
                if self.cloud_thread.isRunning():
                    QMessageBox.information(self, "正在上传", "请等待当前上传结束，或点击停止上传。")
                    return
            except RuntimeError: self.cloud_thread = None
        config = self._selected_sync_config(); config["enabled"] = True
        if not Path(config.get("json_path", "")).is_file() or not extract_google_id(config.get("parent_folder", "")):
            QMessageBox.warning(self, "Google 配置不完整", "请先配置授权 JSON 和 Drive 父文件夹。")
            self._open_google_settings(); return
        if records is None:
            results = list(self.subtitle_results.values())
            records = []
            for index, path in enumerate(files):
                result = results[index] if index < len(results) else {}
                records.append({"path": path, "original": result.get("original", ""),
                                "chinese": result.get("chinese", ""),
                                "language": self.dynamic_caption_page.writing_language.currentText()})
        else:
            by_path = {str(item.get("path", "")): item for item in records}
            records = [{"path": path,
                        "original": by_path.get(str(path), {}).get("original", ""),
                        "chinese": by_path.get(str(path), {}).get("chinese", ""),
                        "language": by_path.get(str(path), {}).get("language", "") or self.dynamic_caption_page.writing_language.currentText()}
                       for path in files]
        self.pending_upload_files = list(files)
        self.pending_upload_records = list(records)
        self.store.data["cloud_resume"]={"status":"upload_pending","files":[str(path) for path in files],
                                         "records":[{**dict(item),"path":str(item.get("path",""))} for item in records]}
        self.store.save()
        self.cloud_thread = QThread(self); self.cloud_worker = CloudUploadWorker(config, files, records, files)
        self.cloud_thread.started.connect(self.cloud_worker.run)
        self.cloud_worker.log.connect(self.pipeline_log.appendPlainText)
        if hasattr(self, "dynamic_caption_page"):
            self.cloud_worker.log.connect(lambda msg: self.dynamic_caption_page._append_run_log(f"[云端同步] {msg}"))
        self.cloud_worker.sheet_pending.connect(self._pipeline_sheet_pending)
        self.cloud_worker.finished.connect(self._cloud_upload_done); self.cloud_worker.finished.connect(self.cloud_thread.quit)
        self.cloud_thread.finished.connect(self._cloud_thread_ended); self.cloud_thread.finished.connect(self.cloud_thread.deleteLater)
        self.pipeline_stop_upload.setEnabled(True); self.pipeline_retry_upload.setEnabled(False)
        self.pipeline_cloud_result.setStyleSheet("color:#7dd3fc;padding:4px;")
        self.pipeline_cloud_result.setText(f"正在上传 {len(files)} 个重命名成品…")
        self.cloud_thread.start()

    def _stop_cloud_upload(self):
        if self.cloud_worker: self.cloud_worker.cancel()

    def _cloud_upload_done(self, ok, folder_url, message):
        self.pipeline_stop_upload.setEnabled(False)
        if ok:
            self.pending_upload_files = []; self.pipeline_retry_upload.setEnabled(False)
            self._pipeline_cloud_ready(folder_url, message)
        elif self.pending_sheet_uploads:
            self.pipeline_retry_upload.setEnabled(False)
        else:
            self.pipeline_retry_upload.setEnabled(bool(self.pending_upload_files))
            self.pipeline_cloud_result.setStyleSheet("color:#fca5a5;padding:4px;")
            self.pipeline_cloud_result.setText(f"上传失败/已停止：{message}<br>可以修复授权后继续上传。")

    def _cloud_thread_ended(self):
        self.cloud_worker = None; self.cloud_thread = None

    def _pipeline_done(self, ok, message):
        self.pipeline_start_btn.setEnabled(True); self.pipeline_stop.setEnabled(False)
        self.pipeline_log.appendPlainText(message)
        (QMessageBox.information if ok else QMessageBox.critical)(
            self, "流水线完成" if ok else "流水线失败", message)

    def _choose_output(self):
        path = QFileDialog.getExistingDirectory(self, "选择字幕输出目录", self.output_edit.text())
        if path:
            self.output_edit.setText(path)

    def _provider_changed(self, provider):
        automatic = provider == AUTO_PROVIDER
        self.model_edit.blockSignals(True)
        self.model_edit.clear()
        if automatic:
            self.model_edit.setEditable(False)
            self.model_edit.addItem("按优先级自动匹配")
            self.model_edit.setCurrentIndex(0)
            self.diarize_check.setEnabled(True)
        elif provider == LOCAL_PROVIDER:
            self.model_edit.setEditable(False)
            current = str(self.store.data["models"].get(provider, DEFAULT_MODELS[provider]) or "medium")
            select = 1  # medium
            for index, (code, label) in enumerate(LOCAL_WHISPER_MODEL_OPTIONS):
                self.model_edit.addItem(label, code)
                if current == code or current.startswith(code) or current == label:
                    select = index
            self.model_edit.setCurrentIndex(select)
            self.diarize_check.setEnabled(False)
        else:
            self.model_edit.setEditable(True)
            current = str(self.store.data["models"].get(provider, DEFAULT_MODELS[provider]) or "")
            presets = {
                "Groq": ["whisper-large-v3", "whisper-large-v3-turbo"],
                "Gemini": [
                    "gemini-3.6-flash",
                    "gemini-3.8-flash",
                    "gemini-3.5-flash",
                    "gemini-2.5-flash",
                ],
                "ElevenLabs": ["scribe_v2", "scribe_v1"],
                "ElevenLabs": ["scribe_v2"],
                "Gladia": ["default"],
            }.get(provider, [current or "default"])
            for item in presets:
                self.model_edit.addItem(item)
            if current and self.model_edit.findText(current) < 0:
                self.model_edit.addItem(current)
            self.model_edit.setCurrentText(current or presets[0])
            self.diarize_check.setEnabled(provider in ("ElevenLabs", "Gladia"))
        self.model_edit.blockSignals(False)

    def _current_model_for_provider(self, provider: str) -> str:
        """从模型下拉/配置解析实际模型名。"""
        if provider == AUTO_PROVIDER:
            return ""
        if provider == LOCAL_PROVIDER:
            data = self.model_edit.currentData()
            if data:
                return str(data)
            text = self.model_edit.currentText().strip()
            for code, label in LOCAL_WHISPER_MODEL_OPTIONS:
                if text == code or text == label or text.startswith(code):
                    return code
            return str(self.store.data["models"].get(provider, DEFAULT_MODELS[provider]) or "medium")
        text = self.model_edit.currentText().strip()
        if text and text != "按优先级自动匹配":
            return text
        return str(self.store.data["models"].get(provider, DEFAULT_MODELS[provider]) or "")

    def _refresh_priority_label(self):
        if hasattr(self, "priority_label"):
            self.priority_label.setText("  ›  ".join(self.store.data["provider_priority"]))

    def _open_priority_dialog(self):
        dialog = QDialog(self); dialog.setWindowTitle("调整字幕服务优先级"); dialog.resize(470, 390)
        box = QVBoxLayout(dialog)
        note = QLabel("自动模式会从上到下查找可用服务；拖动项目，或用右侧按钮调整。")
        note.setWordWrap(True); box.addWidget(note)
        row = QHBoxLayout(); priority_list = QListWidget()
        priority_list.addItems(self.store.data["provider_priority"])
        priority_list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        priority_list.setDefaultDropAction(Qt.DropAction.MoveAction)
        moves = QVBoxLayout()
        up = QPushButton("上移"); down = QPushButton("下移")
        def move_item(delta):
            current = priority_list.currentRow()
            target = current + delta
            if current < 0 or target < 0 or target >= priority_list.count():
                return
            item = priority_list.takeItem(current); priority_list.insertItem(target, item); priority_list.setCurrentRow(target)
        up.clicked.connect(lambda: move_item(-1)); down.clicked.connect(lambda: move_item(1))
        moves.addWidget(up); moves.addWidget(down); moves.addStretch()
        row.addWidget(priority_list, 1); row.addLayout(moves); box.addLayout(row, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept); buttons.rejected.connect(dialog.reject); box.addWidget(buttons)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.store.data["provider_priority"] = [priority_list.item(i).text() for i in range(priority_list.count())]
            self.store.save(); self._refresh_priority_label()

    def _resolve_provider(self):
        for provider in self.store.data["provider_priority"]:
            if provider == LOCAL_PROVIDER:
                return provider
            keys = self.store.data["providers"].get(provider, [])
            if any(x.get("enabled", True) and x.get("status", "未检测") in ("未检测", "有效") for x in keys):
                return provider
        return LOCAL_PROVIDER

    def _caption_asr_language(self) -> str:
        """Reels 识别语言 → Whisper/云识别 language 码；自动则 auto。

        优先「字幕识别」页的识别语言（可固定马达加斯加语等）；
        其次书写语言；再次字幕提取页语言框。
        """
        try:
            page = getattr(self, "dynamic_caption_page", None)
            if page is not None and hasattr(page, "asr_language_code"):
                code = page.asr_language_code()
                if code:
                    return code
            if page is not None and hasattr(page, "asr_language"):
                code = writing_language_from_ui(page.asr_language.currentText())
                if code:
                    return code
            if page is not None and hasattr(page, "writing_language"):
                code = writing_language_from_ui(page.writing_language.currentText())
                if code:
                    return code
        except Exception:
            pass
        try:
            if hasattr(self, "language_edit"):
                code = writing_language_from_ui(self.language_edit.currentText())
                if code:
                    return code
        except Exception:
            pass
        return "auto"

    def _caption_transcribe(self, media_path, selected_provider, cancel_flag=None, prefer_fast=False):
        """在动态文案工作线程中复用同一套识别、翻译和密钥轮询逻辑。

        cancel_flag: 可选 callable() -> bool，为 True 时尽快中止（图文成片点停止）。
        prefer_fast: 图文成片场景优先用更轻的本地模型，避免 medium 卡死感。
        """
        priority = list(self.store.data.get("provider_priority") or PROVIDERS + [LOCAL_PROVIDER])
        # 界面旧文案 / 别名 → 正式 provider 名
        alias = {
            "Whisper (本地/较慢)": LOCAL_PROVIDER,
            "Whisper": LOCAL_PROVIDER,
            "本地 Whisper": LOCAL_PROVIDER,
            "Local Whisper": LOCAL_PROVIDER,
        }
        selected_provider = alias.get(str(selected_provider or "").strip(), selected_provider)
        candidates = ([selected_provider] if selected_provider != AUTO_PROVIDER else []) + priority + [LOCAL_PROVIDER]
        ordered = []
        for provider in candidates:
            provider = alias.get(str(provider or "").strip(), provider)
            if provider in TRANSCRIPTION_PROVIDERS and provider != AUTO_PROVIDER and provider not in ordered:
                ordered.append(provider)
        # 图文成片 + 长音频：云端偶发「整段 1 条字幕」且很慢；把本地 Whisper 提前，避免干等 Gemini 数分钟
        if prefer_fast:
            media_sec = 0.0
            try:
                from modules.dynamic_caption_page import media_duration
                media_sec = float(
                    media_duration(self._find_ffmpeg(), str(media_path), fallback=0) or 0
                )
            except Exception:
                media_sec = 0.0
            if media_sec >= 90 and LOCAL_PROVIDER in ordered:
                ordered = [LOCAL_PROVIDER] + [p for p in ordered if p != LOCAL_PROVIDER]
        errors = []
        asr_language = self._caption_asr_language()
        write_app_log(
            f"图文/字幕识别排队：{Path(media_path).name}｜首选={selected_provider}｜候选={','.join(ordered)}"
            f"{'｜prefer_fast' if prefer_fast else ''}",
            "INFO", "字幕识别",
        )
        for provider in ordered:
            if cancel_flag and cancel_flag():
                raise RuntimeError("任务已取消")
            if provider != LOCAL_PROVIDER and not self.store.has_candidates(provider):
                message = f"{provider} 没有可用密钥，自动尝试下一种识别服务"
                errors.append(message); write_app_log(message, "WARNING", "字幕识别")
                continue
            model = self.store.data["models"].get(provider, DEFAULT_MODELS[provider])
            if prefer_fast and provider == LOCAL_PROVIDER:
                # 图文成片只需时间轴，small 足够且远快于 medium
                cur = str(model or "").strip().lower()
                if cur in ("", "medium", "large-v3", "large-v2", "large"):
                    model = "small"
            try:
                # resume_existing=False：避免误复用 subtitle_tasks 断点里的坏结果
                worker = TranscribeWorker(
                    self.store, provider, model, [media_path], "", asr_language, False,
                    self._find_ffmpeg(), False, True, False,
                )
                # 同步调用时 Signal 可能无接收端：双写到软件日志
                worker.log.connect(lambda m: write_app_log(m, "INFO", "字幕识别"))
                # 轮询取消标志，写入 TranscribeWorker.cancelled（Whisper 段落间会检查）
                stop_poll = threading.Event()
                poller = None
                if cancel_flag:
                    def _poll_cancel(w=worker, flag=cancel_flag, stop=stop_poll):
                        while not stop.wait(0.2):
                            try:
                                if flag():
                                    w.cancel()
                                    return
                            except Exception:
                                return
                    poller = threading.Thread(target=_poll_cancel, name="asr-cancel-poll", daemon=True)
                    poller.start()
                write_app_log(
                    f"字幕识别：{Path(media_path).name}｜服务={provider}｜模型={model}｜语言={asr_language or 'auto'}",
                    "INFO", "字幕识别",
                )
                try:
                    if cancel_flag and cancel_flag():
                        raise RuntimeError("任务已取消")
                    result = worker._process_one(media_path)
                finally:
                    stop_poll.set()
                raw = result.get("raw") or {}
                words = raw.get("words") or (raw.get("response") or {}).get("words") or []
                timed_words = []
                for word in words:
                    text = str(word.get("text") or word.get("word") or "").strip()
                    if text and word.get("start") is not None:
                        timed_words.append({"start": float(word.get("start", 0)),
                                            "end": float(word.get("end", word.get("start", 0) + .25)), "text": text})
                precise_srt = segments_to_srt(timed_words) if timed_words else result["srt"]
                # 质量护栏：词级结果若明显少于句级，优先句级（防坏 words 列表）
                phrase_count = max(0, str(result.get("srt") or "").count("-->"))
                word_count = max(0, precise_srt.count("-->"))
                if phrase_count >= 3 and word_count > 0 and word_count < max(2, phrase_count // 3):
                    write_app_log(
                        f"词级时间轴异常稀疏（词段 {word_count} / 句段 {phrase_count}），改用句级 SRT",
                        "WARNING", "字幕识别",
                    )
                    precise_srt = result["srt"]
                # 仍是句级（Gemini 常见）：按字长占比估词级，避免跟读「一词一分」假匀速
                try:
                    from modules.dynamic_caption_page import ensure_word_level_srt
                    precise_srt, estimated = ensure_word_level_srt(
                        precise_srt, str(result.get("srt") or ""),
                    )
                    if estimated:
                        write_app_log(
                            f"识别结果无真词级时间戳，已按字长占比估时（{precise_srt.count('-->')} 词段）；"
                            f"对口型请优先 Groq / 本地 Whisper",
                            "WARNING", "字幕识别",
                        )
                except Exception as expand_exc:
                    write_app_log(f"词级估时跳过：{expand_exc}", "WARNING", "字幕识别")
                # 长音频护栏：整段只出 1～2 条（Gemini 常见）对跟读/语义几乎无用，强制换下一方案
                media_dur = 0.0
                try:
                    from modules.dynamic_caption_page import media_duration
                    media_dur = float(
                        media_duration(self._find_ffmpeg(), str(media_path), fallback=0) or 0
                    )
                except Exception:
                    media_dur = 0.0
                if media_dur <= 0:
                    # 从字幕末尾估时长
                    try:
                        ends = re.findall(
                            r"-->\s*(\d+):(\d+):(\d+)[,.](\d+)",
                            str(precise_srt or result.get("srt") or ""),
                        )
                        if ends:
                            h, m, s, ms = ends[-1]
                            media_dur = (
                                int(h) * 3600 + int(m) * 60 + int(s)
                                + int(ms.ljust(3, "0")[:3]) / 1000.0
                            )
                    except Exception:
                        media_dur = 0.0
                cue_n = max(phrase_count, word_count, precise_srt.count("-->"))
                plain_len = len(str(result.get("original") or "").strip())
                if media_dur >= 60:
                    min_cues = max(3, int(media_dur / 28))
                    min_chars = max(100, int(media_dur * 1.8))
                    if cue_n < min_cues or plain_len < min_chars:
                        raise RuntimeError(
                            f"结果过稀（{cue_n} 条/{plain_len} 字/{media_dur:.0f}s，"
                            f"期望≥{min_cues} 条）：长配音需更细时间轴，改试下一识别服务"
                        )
                if errors:
                    write_app_log(f"已自动切换到 {provider} 并继续：{Path(media_path).name}", "INFO", "字幕识别")
                # 供 Reels 界面日志显示「真正用了谁」
                self._last_caption_asr = {
                    "provider": provider,
                    "model": model,
                    "language": asr_language or "auto",
                    "cues": max(0, precise_srt.count("-->")),
                }
                return result["original"], result["chinese"], precise_srt
            except Exception as exc:
                if (cancel_flag and cancel_flag()) or "取消" in str(exc):
                    raise RuntimeError("任务已取消") from exc
                message = f"{provider} 调用失败（可能是配额、密钥或网络问题）：{exc}；自动切换下一方案"
                errors.append(message); write_app_log(message, "WARNING", "字幕识别")
        if cancel_flag and cancel_flag():
            raise RuntimeError("任务已取消")
        final = "所有字幕识别方案均不可用：" + "｜".join(errors[-5:])
        write_app_log(final, "ERROR", "字幕识别")
        raise RuntimeError(final)

    def _rename_title_transcribe(self, media_path):
        """Reuse subtitle-page results first; transcribe only videos that have no cached content."""
        path = Path(media_path)
        cached = self.subtitle_results.get(path.name) or self.subtitle_results.get(path.stem)
        if cached and (cached.get("chinese") or cached.get("original")):
            return cached.get("original", ""), cached.get("chinese", ""), cached.get("srt", "")
        return self._caption_transcribe(str(path), AUTO_PROVIDER)

    @staticmethod
    def _elevenlabs_alignment_to_srt(alignment, srt_path):
        """VideoKit 同款：把 with-timestamps 的 character 对齐写成简易 SRT。"""
        chars = alignment.get("characters") or []
        starts = alignment.get("character_start_times_seconds") or []
        ends = alignment.get("character_end_times_seconds") or []
        if not chars or not starts or not ends:
            return
        n = min(len(chars), len(starts), len(ends))
        if n <= 0:
            return

        def fmt(sec):
            sec = max(0.0, float(sec))
            h = int(sec // 3600)
            m = int((sec % 3600) // 60)
            s = int(sec % 60)
            ms = int(round((sec - int(sec)) * 1000))
            if ms >= 1000:
                s += 1
                ms = 0
            return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

        # 按停顿/标点拆成字幕块
        delimiters = set(" \t\n，。！？,.!?;；:：\"'“”")
        sentence_end = set("。！？.!?")
        blocks = []
        buf, b_start, b_end = "", None, 0.0
        for i in range(n):
            ch = chars[i]
            st, en = float(starts[i]), float(ends[i])
            if b_start is None:
                b_start = st
            buf += ch
            b_end = en
            pause = (float(starts[i + 1]) - en) if i + 1 < n else 0.0
            end_here = (
                i == n - 1
                or ch in sentence_end
                or pause >= 0.35
                or (ch in delimiters and len(buf.strip()) >= 28)
            )
            if end_here:
                clean = buf.strip()
                if clean:
                    blocks.append((b_start, max(b_start + 0.08, b_end), clean))
                buf, b_start = "", None
        if not blocks:
            return
        lines = []
        for idx, (st, en, txt) in enumerate(blocks, 1):
            lines.append(f"{idx}\n{fmt(st)} --> {fmt(en)}\n{txt}\n")
        Path(srt_path).write_text("\n".join(lines), encoding="utf-8")

    def _text_to_speech(self, text, service, voice, destination):
        """生成配音；ElevenLabs 失败时自动轮换下一枚可用密钥。"""
        target = Path(destination); target.parent.mkdir(parents=True, exist_ok=True)
        if service == "微软文字转语音":
            try:
                import edge_tts
            except ImportError as exc:
                raise RuntimeError("缺少微软语音组件 edge-tts，请到“设置与组件”点击一键安装。") from exc
            clean_text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", " ", str(text)).strip()
            if not clean_text:
                raise RuntimeError("文案为空，无法生成语音。")

            # 下拉项可能是「ShortName｜说明」
            selected_voice = (str(voice or "").split("｜", 1)[0].strip() or "zh-CN-XiaoxiaoNeural")
            locale_m = re.match(r"^([a-z]{2}-[A-Z]{2})", selected_voice)
            locale = locale_m.group(1) if locale_m else selected_voice[:5]
            # 多语言备用音色（同语种失败时自动换）
            voice_fallbacks = {
                "pt-PT": ["pt-PT-RaquelNeural", "pt-PT-DuarteNeural"],
                "pt-BR": ["pt-BR-FranciscaNeural", "pt-BR-AntonioNeural"],
                "zh-CN": ["zh-CN-XiaoxiaoNeural", "zh-CN-YunxiNeural", "zh-CN-XiaoyiNeural"],
                "zh-TW": ["zh-TW-HsiaoChenNeural", "zh-TW-YunJheNeural"],
                "en-US": ["en-US-JennyNeural", "en-US-GuyNeural", "en-US-AriaNeural"],
                "en-GB": ["en-GB-SoniaNeural", "en-GB-RyanNeural"],
                "es-ES": ["es-ES-ElviraNeural", "es-ES-AlvaroNeural"],
                "es-MX": ["es-MX-DaliaNeural", "es-MX-JorgeNeural"],
                "fr-FR": ["fr-FR-DeniseNeural", "fr-FR-HenriNeural"],
                "de-DE": ["de-DE-KatjaNeural", "de-DE-ConradNeural"],
                "it-IT": ["it-IT-ElsaNeural", "it-IT-DiegoNeural"],
                "el-GR": ["el-GR-AthinaNeural", "el-GR-NestorasNeural"],
                "ru-RU": ["ru-RU-SvetlanaNeural", "ru-RU-DmitryNeural"],
                "tr-TR": ["tr-TR-EmelNeural", "tr-TR-AhmetNeural"],
                "ar-SA": ["ar-SA-ZariyahNeural", "ar-SA-HamedNeural"],
                "ar-EG": ["ar-EG-SalmaNeural", "ar-EG-ShakirNeural"],
                "he-IL": ["he-IL-HilaNeural", "he-IL-AvriNeural"],
                "ja-JP": ["ja-JP-NanamiNeural", "ja-JP-KeitaNeural"],
                "ko-KR": ["ko-KR-SunHiNeural", "ko-KR-InJoonNeural"],
                "hi-IN": ["hi-IN-SwaraNeural", "hi-IN-MadhurNeural"],
                "id-ID": ["id-ID-GadisNeural", "id-ID-ArdiNeural"],
                "nl-NL": ["nl-NL-FennaNeural", "nl-NL-MaartenNeural"],
                "pl-PL": ["pl-PL-AgnieszkaNeural", "pl-PL-MarekNeural"],
                "vi-VN": ["vi-VN-HoaiMyNeural", "vi-VN-NamMinhNeural"],
                "th-TH": ["th-TH-PremwadeeNeural", "th-TH-NiwatNeural"],
            }
            voices = list(dict.fromkeys([selected_voice] + voice_fallbacks.get(locale, [])))

            # Edge 的免费接口在长段落或网络短暂波动时偶尔只返回元数据、不返回音频。
            # 按句拆成适中的请求，并对当前音色及同语种备用音色自动重试。
            # 注意：不要用 strip("。！？…") 之类会吃掉首尾标点/句子的写法。
            pieces = []
            pending = ""
            # 保留首句：用换行/句末标点切分，但不过滤无标点的开头段落
            raw_parts = re.split(r"(?<=[。！？.!?；;])\s+|\n+", clean_text)
            for sentence in raw_parts:
                sentence = sentence.strip()
                if not sentence:
                    continue
                if pending and len(pending) + len(sentence) + 1 > 1400:
                    pieces.append(pending)
                    pending = sentence
                else:
                    pending = f"{pending} {sentence}".strip() if pending else sentence
            if pending:
                pieces.append(pending)
            if not pieces:
                pieces = [clean_text]
            # 保险：若首段过短（纯标点/序号），合并到下一段，避免「第一句没声」
            if len(pieces) >= 2 and len(re.sub(r"[\s\W_]+", "", pieces[0], flags=re.UNICODE)) < 2:
                pieces[1] = f"{pieces[0]} {pieces[1]}".strip()
                pieces = pieces[1:]

            async def generate_part(part_text, part_path, part_voice):
                boundaries = []
                wrote = 0
                with part_path.open("wb") as audio_handle:
                    communicate = edge_tts.Communicate(part_text, part_voice)
                    async for chunk in communicate.stream():
                        if chunk.get("type") == "audio" and chunk.get("data"):
                            audio_handle.write(chunk["data"])
                            wrote += len(chunk["data"])
                        elif chunk.get("type") == "WordBoundary":
                            start = float(chunk.get("offset", 0)) / 10_000_000
                            duration = float(chunk.get("duration", 0)) / 10_000_000
                            boundaries.append({"start": start, "end": start + max(.08, duration),
                                               "text": str(chunk.get("text", "")).strip()})
                if wrote < 256:
                    raise RuntimeError("流式接口未写入有效音频数据")
                return boundaries

            def _probe_audio_seconds(path: Path) -> float:
                try:
                    ffprobe = self._find_ffmpeg().replace("ffmpeg", "ffprobe")
                    if not Path(ffprobe).is_file():
                        ffprobe = self._find_ffmpeg()
                    # prefer sibling ffprobe next to ffmpeg
                    ffmpeg_bin = Path(self._find_ffmpeg())
                    candidate = ffmpeg_bin.with_name(
                        "ffprobe.exe" if os.name == "nt" else "ffprobe")
                    if candidate.is_file():
                        ffprobe = str(candidate)
                    result = subprocess.run(
                        [ffprobe, "-v", "error", "-show_entries", "format=duration",
                         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                        encoding="utf-8", errors="replace", **hidden_kwargs())
                    return max(0.0, float((result.stdout or "").strip() or 0))
                except Exception:
                    return 0.0

            last_error = "服务没有返回音频"
            with tempfile.TemporaryDirectory(prefix="video_toolkit_tts_") as temp_name:
                temp_dir = Path(temp_name)
                for selected in voices:
                    for attempt in range(1, 3):
                        part_paths = []; all_boundaries = []; time_offset = 0.0
                        try:
                            for index, piece in enumerate(pieces):
                                part_path = temp_dir / f"part_{index:03d}.mp3"
                                if part_path.exists():
                                    part_path.unlink()
                                boundaries = asyncio.run(generate_part(piece, part_path, selected))
                                if not part_path.exists() or part_path.stat().st_size < 256:
                                    raise RuntimeError(f"第 {index + 1} 段没有收到音频（可能是首句被跳过）")
                                part_paths.append(part_path)
                                part_dur = _probe_audio_seconds(part_path)
                                for entry in boundaries:
                                    all_boundaries.append({**entry,
                                                           "start": entry["start"] + time_offset,
                                                           "end": entry["end"] + time_offset})
                                # 无 WordBoundary 时也必须推进时间轴，避免后段字幕盖住首句
                                if boundaries:
                                    time_offset += max(item["end"] for item in boundaries) + .06
                                elif part_dur > 0.05:
                                    time_offset += part_dur + .06
                                else:
                                    # 粗估：每 4 字约 0.35s（中文）/ 每词 0.28s
                                    est = max(0.4, len(piece) * 0.08)
                                    time_offset += est

                            if target.exists():
                                target.unlink()
                            ffmpeg = self._find_ffmpeg()
                            if len(part_paths) == 1:
                                # 重编码一次，消除部分 edge-tts MP3 片头丢帧导致「第一句听不见」
                                result = subprocess.run(
                                    [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                                     "-i", str(part_paths[0]),
                                     "-af", "aresample=48000,aformat=channel_layouts=stereo,asetpts=PTS-STARTPTS",
                                     "-c:a", "libmp3lame", "-b:a", "192k", str(target)],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                    encoding="utf-8", errors="replace", **hidden_kwargs())
                                if result.returncode or not target.exists() or target.stat().st_size < 256:
                                    shutil.copyfile(part_paths[0], target)
                            else:
                                # 禁止 -c copy 拼接 MP3：常见丢首包/首句
                                filter_inputs = "".join(f"[{i}:a:0]" for i in range(len(part_paths)))
                                filter_complex = (
                                    f"{filter_inputs}concat=n={len(part_paths)}:v=0:a=1,"
                                    f"aresample=48000,aformat=channel_layouts=stereo,"
                                    f"asetpts=PTS-STARTPTS[aout]"
                                )
                                cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y"]
                                for path in part_paths:
                                    cmd += ["-i", str(path)]
                                cmd += [
                                    "-filter_complex", filter_complex, "-map", "[aout]",
                                    "-c:a", "libmp3lame", "-b:a", "192k", str(target),
                                ]
                                result = subprocess.run(
                                    cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                    encoding="utf-8", errors="replace", **hidden_kwargs())
                                if result.returncode:
                                    raise RuntimeError(result.stderr.strip() or "分段音频合并失败")
                            if not target.exists() or target.stat().st_size < 256:
                                raise RuntimeError("合并后音频为空")
                            # 时长异常偏短时视为首句丢失，触发重试
                            total_dur = _probe_audio_seconds(target)
                            min_expect = max(0.35, min(8.0, len(clean_text) * 0.035))
                            if total_dur > 0 and total_dur < min_expect * 0.45:
                                raise RuntimeError(
                                    f"生成音频过短（{total_dur:.2f}s），疑似首句未写入，将重试")
                            if all_boundaries:
                                target.with_suffix(".srt").write_text(
                                    segments_to_srt(all_boundaries), encoding="utf-8-sig")
                            return target
                        except Exception as exc:
                            last_error = f"{selected}（第 {attempt} 次）：{exc}"
                            for part_path in part_paths:
                                try:
                                    part_path.unlink()
                                except OSError:
                                    pass
            raise RuntimeError(
                "微软文字转语音连续重试后仍未收到音频。"
                f"\n最后错误：{last_error}"
                "\n请检查网络，或切换同语种音色；追求自然度可改用 ElevenLabs。")

        if service == "Gemini 自然语音":
            candidates = self.store.candidates("Gemini")
            if not candidates:
                raise RuntimeError("没有可用的 Gemini 密钥，请先到密钥管理添加并检测。")
            voice_name = (voice.split("｜", 1)[0].strip() if voice else "Kore") or "Kore"
            last_error = ""
            tts_models = (
                "gemini-3.1-flash-tts-preview",
                "gemini-2.5-flash-preview-tts",
                "gemini-2.5-pro-preview-tts",
            )
            for item in candidates:
                try:
                    payload = None
                    last_error = ""
                    for tts_model in tts_models:
                        response = requests.post(
                            "https://generativelanguage.googleapis.com/v1beta/models/"
                            f"{tts_model}:generateContent",
                            params={"key": item["key"]},
                            headers={"Content-Type": "application/json"},
                            json={
                                "contents": [{"parts": [{"text": str(text).strip()}]}],
                                "generationConfig": {
                                    "responseModalities": ["AUDIO"],
                                    "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {
                                        "voiceName": voice_name}}},
                                },
                            }, timeout=240)
                        if response.status_code < 400:
                            payload = response.json()
                            break
                        last_error = response_error(response)
                        if response.status_code in (404, 400) and "not found" in last_error.casefold():
                            continue
                        break
                    if payload is None:
                        self.store.mark_use("Gemini", item["id"],
                                            "失效" if "401" in last_error or "403" in last_error else
                                            "额度受限" if "429" in last_error else "异常", last_error)
                        continue
                    parts = (((payload.get("candidates") or [{}])[0].get("content") or {}).get("parts") or [])
                    inline = next((part.get("inlineData") or part.get("inline_data")
                                   for part in parts if part.get("inlineData") or part.get("inline_data")), None)
                    if not inline or not inline.get("data"):
                        last_error = "Gemini 没有返回音频数据，请重试或更换音色。"
                        self.store.mark_use("Gemini", item["id"], "异常", last_error)
                        continue
                    audio = base64.b64decode(inline["data"])
                    mime = str(inline.get("mimeType") or inline.get("mime_type") or "audio/L16;rate=24000").lower()
                    with tempfile.TemporaryDirectory(prefix="video_toolkit_gemini_tts_") as temp_name:
                        temp_dir = Path(temp_name)
                        if "wav" in mime:
                            source = temp_dir / "voice.wav"; source.write_bytes(audio)
                        else:
                            rate_match = re.search(r"rate=(\d+)", mime)
                            rate = int(rate_match.group(1)) if rate_match else 24000
                            source = temp_dir / "voice.wav"
                            with wave.open(str(source), "wb") as wav_file:
                                wav_file.setnchannels(1); wav_file.setsampwidth(2); wav_file.setframerate(rate)
                                wav_file.writeframes(audio)
                        result = subprocess.run(
                            [self._find_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
                             "-i", str(source), "-c:a", "libmp3lame", "-b:a", "192k", str(target)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            encoding="utf-8", errors="replace", **hidden_kwargs())
                        if result.returncode or not target.exists() or target.stat().st_size < 256:
                            last_error = result.stderr.strip() or "Gemini 音频转换失败"
                            self.store.mark_use("Gemini", item["id"], "异常", last_error)
                            continue
                    self.store.mark_use("Gemini", item["id"], "有效", "")
                    return target
                except (requests.RequestException, ValueError, KeyError) as exc:
                    last_error = f"Gemini 语音请求失败：{exc}"
                    self.store.mark_use("Gemini", item["id"], "异常", last_error)
            raise RuntimeError(f"Gemini 可用密钥均生成失败。最后错误：{last_error}")

        voice_id = voice.strip().split("｜", 1)[0].strip()
        if not voice_id or voice_id.endswith("Neural"):
            raise RuntimeError(
                "使用 ElevenLabs 时，请在音色框输入 ElevenLabs Voice ID"
                "（在 elevenlabs.io 音色库复制，不是微软 Neural 名称）。"
            )
        candidates = self.store.candidates("ElevenLabs")
        if not candidates:
            raise RuntimeError(
                "没有可用的 ElevenLabs 凭证。\n"
                "请到「设置与组件 → 密钥」任选其一：\n"
                "• 添加 sk_ API Key；或\n"
                "• 添加「网页会话（Cookie）」——与浏览器插件相同，用 Bearer 调官方 TTS 扣点数。\n"
                "也可在「文字转语音」独立板块批量生成。"
            )
        el_model = (
            os.environ.get("VIDEO_TOOLKIT_EL_MODEL")
            or "eleven_flash_v2_5"
        ).strip() or "eleven_flash_v2_5"
        # Reels/字幕需要时间轴时用 timestamps；独立批量板块默认 stream（与插件一致）
        want_ts = os.environ.get("VIDEO_TOOLKIT_EL_TIMESTAMPS", "").strip() in ("1", "true", "yes")
        last_error = ""
        for item in candidates:
            secret = item.get("key") or ""
            try:
                audio_bytes, alignment = el_web.tts_request(
                    secret, voice_id, text, timeout=180,
                    model_id=el_model,
                    want_timestamps=want_ts,
                )
                if alignment:
                    try:
                        self._elevenlabs_alignment_to_srt(
                            alignment, target.with_suffix(".srt"))
                    except Exception:
                        pass
                if not audio_bytes or len(audio_bytes) < 256:
                    last_error = "接口未返回有效音频"
                    self.store.mark_use("ElevenLabs", item["id"], "异常", last_error)
                    continue
                target.write_bytes(audio_bytes)
                self.store.mark_use("ElevenLabs", item["id"], "有效", "")
                return target
            except RuntimeError as exc:
                err = str(exc)
                last_error = err
                status = "异常"
                if "401" in err or "403" in err:
                    status = "失效"
                elif "429" in err or "quota" in err.lower() or "limit" in err.lower():
                    status = "额度受限"
                self.store.mark_use("ElevenLabs", item["id"], status, last_error)
            except requests.RequestException as exc:
                last_error = f"网络请求失败：{exc}"
                self.store.mark_use("ElevenLabs", item["id"], "异常", last_error)
            except Exception as exc:
                last_error = str(exc)
                self.store.mark_use("ElevenLabs", item["id"], "异常", last_error)
        raise RuntimeError(
            f"ElevenLabs 可用凭证均生成失败。最后错误：{last_error}\n"
            "排查：\n"
            "① Voice ID 是否为 elevenlabs 音色库 ID（非微软 Neural 名称）\n"
            "② sk_ 密钥或网页会话 Authorization/Cookie 是否过期（JWT 约 1 小时）\n"
            "③ 免费档是否被 unusual_activity 关掉 API（网页仍可能显示点数）\n"
            "④ 可添加多个网页会话/密钥轮询；或改用「微软文字转语音」"
        )

    def _find_ffmpeg(self):
        executable = media_tool_name("ffmpeg")
        candidates = [bundled_media_tool("ffmpeg"), app_root() / executable, component_bin() / executable]
        for path in candidates:
            if validate_media_tool(path,"ffmpeg"):
                return str(path)
        found = shutil.which("ffmpeg")
        if found and validate_media_tool(found,"ffmpeg"):
            return found
        raise RuntimeError(f"未找到 {executable}")

    def _start_transcription(self):
        local_files = [self.file_list.item(i).text() for i in range(self.file_list.count())]
        urls = [line.strip() for line in self.url_input.toPlainText().splitlines() if line.strip()]
        invalid_urls = [url for url in urls if not is_supported_video_url(url)]
        if invalid_urls:
            QMessageBox.warning(self, "链接格式不支持",
                                "以下内容不是受支持的视频链接：\n" + "\n".join(invalid_urls[:5]))
            return
        files = local_files + urls
        if not files:
            QMessageBox.information(self, "请选择来源", "请添加本地视频/音频，或粘贴网络视频链接。")
            return
        selected_provider = self.provider_combo.currentText()
        provider = self._resolve_provider() if selected_provider == AUTO_PROVIDER else selected_provider
        if provider != LOCAL_PROVIDER and not self.store.has_candidates(provider):
            QMessageBox.information(self, "缺少密钥", f"请先在“API 密钥管理”中添加 {provider} 密钥。")
            self._show_page(6)
            return
        try:
            ffmpeg = self._find_ffmpeg()
        except Exception as exc:
            QMessageBox.critical(self, "缺少组件", str(exc)); return
        if selected_provider == AUTO_PROVIDER:
            model = self.store.data["models"].get(provider, DEFAULT_MODELS[provider])
        else:
            model = self._current_model_for_provider(selected_provider) or DEFAULT_MODELS[provider]
            self.store.data["models"][provider] = model
            if provider == LOCAL_PROVIDER:
                self.store.data["_local_model_user_set"] = True
            self.store.save()
        self.log_box.clear(); self.transcribe_progress.setValue(0)
        if selected_provider == AUTO_PROVIDER:
            self._append_log(f"自动选择：{provider}（模型：{model}）")
        else:
            self._append_log(f"使用 {provider}（模型：{model}）")
        self.subtitle_results.clear(); self.result_combo.clear(); self.result_combo.addItem(ALL_RESULTS_LABEL)
        self.original_result.clear(); self.chinese_result.clear()
        self.thread = QThread(self)
        self.worker = TranscribeWorker(self.store, provider, model, files, "",
                                       self._subtitle_language_code(), self.diarize_check.isChecked(), ffmpeg,
                                       self.subtitle_resume_check.isChecked())
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.log.connect(self._append_log)
        self.worker.progress.connect(self.transcribe_progress.setValue)
        self.worker.result_ready.connect(self._subtitle_result_ready)
        self.worker.finished.connect(self._transcription_done)
        self.worker.finished.connect(self.thread.quit)
        self.thread.finished.connect(self._thread_ended)
        self.thread.finished.connect(self.thread.deleteLater)
        self.start_btn.setEnabled(False); self.cancel_btn.setEnabled(True)
        self.thread.start()

    def _subtitle_language_code(self) -> str:
        """UI 语言下拉 → whisper/书写用码；空则 auto。"""
        code = writing_language_from_ui(self.language_edit.currentText())
        return code or "auto"

    def _pipeline_language_code(self) -> str:
        code = writing_language_from_ui(self.pipeline_language.currentText())
        return code or "auto"

    def _cancel_transcription(self):
        if self.worker:
            self.worker.cancel(); self._append_log("正在取消（当前网络请求结束后生效）…")

    def _append_log(self, text):
        self.log_box.appendPlainText(f"[{datetime.now():%H:%M:%S}] {text}")

    def _transcription_done(self, ok, message):
        self.start_btn.setEnabled(True); self.cancel_btn.setEnabled(False)
        self._append_log(message); self._refresh_keys()
        (QMessageBox.information if ok else QMessageBox.critical)(self, "任务完成" if ok else "任务失败", message)

    def _subtitle_result_ready(self, name, original, chinese, srt):
        self.subtitle_results[name] = {"original": original, "chinese": chinese, "srt": srt}
        if self.result_combo.findText(name) < 0:
            self.result_combo.addItem(name)
        self.result_combo.setCurrentText(ALL_RESULTS_LABEL)
        self._show_subtitle_result(ALL_RESULTS_LABEL)

    def _show_subtitle_result(self, name):
        if name == ALL_RESULTS_LABEL:
            originals, translations = [], []
            for result_name, result in self.subtitle_results.items():
                originals.append(f"【{result_name}】\n{result.get('original', '')}")
                translations.append(f"【{result_name}】\n{result.get('chinese', '')}")
            self.original_result.setPlainText("\n\n".join(originals))
            self.chinese_result.setPlainText("\n\n".join(translations))
            return
        result = self.subtitle_results.get(name, {})
        self.original_result.setPlainText(result.get("original", ""))
        self.chinese_result.setPlainText(result.get("chinese", ""))

    def _flash_copied(self, message="✓ 已复制"):
        """短暂显示绿色「已复制」提示，复制反馈更明显。"""
        label = getattr(self, "copy_status", None)
        if label is None:
            return
        label.setText(message)
        label.setVisible(True)
        QTimer.singleShot(2200, lambda: label.setVisible(False) if label is not None else None)

    @staticmethod
    def _bilingual_to_csv(original: str, chinese: str) -> str:
        """原文/中文按行配对为 TSV 两列，粘贴到 Google 表格时自动左右并排。"""
        def _lines(text: str):
            text = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
            if not text:
                return []
            return [line.strip() for line in text.split("\n")]

        def _cell(value: str) -> str:
            # 单元格内换行/制表符压成空格，避免破坏两列结构
            return (value or "").replace("\t", " ").replace("\r", " ").replace("\n", " ").strip()

        orig_lines = _lines(original)
        zh_lines = _lines(chinese)
        # 若行数接近（差 ≤2），按行配对；否则各压成单格（两列一行）
        if orig_lines and zh_lines and abs(len(orig_lines) - len(zh_lines)) <= 2:
            n = max(len(orig_lines), len(zh_lines))
            rows = []
            for i in range(n):
                o = orig_lines[i] if i < len(orig_lines) else ""
                z = zh_lines[i] if i < len(zh_lines) else ""
                rows.append(f"{_cell(o)}\t{_cell(z)}")
            return "\n".join(rows)
        return f"{_cell(original)}\t{_cell(chinese)}"

    def _copy_current_original(self):
        text = self.original_result.toPlainText()
        if not text.strip():
            return
        QApplication.clipboard().setText(text)
        self._flash_copied("✓ 已复制原文")

    def _copy_bilingual(self):
        name = self.result_combo.currentText()
        if name == ALL_RESULTS_LABEL:
            self._copy_all_bilingual()
            return
        result = self.subtitle_results.get(name)
        if not result:
            return
        text = self._bilingual_to_csv(result.get("original", ""), result.get("chinese", ""))
        QApplication.clipboard().setText(text)
        self._flash_copied("✓ 已复制对照（CSV 两列）")

    def _copy_all_original(self):
        text = "\n\n".join(f"【{name}】\n{result['original']}"
                            for name, result in self.subtitle_results.items())
        if not text.strip():
            return
        QApplication.clipboard().setText(text)
        self._flash_copied("✓ 已复制全部原文")

    def _copy_all_bilingual(self):
        parts = []
        for name, result in self.subtitle_results.items():
            block = self._bilingual_to_csv(result.get("original", ""), result.get("chinese", ""))
            if block.strip():
                parts.append(block)
        if not parts:
            return
        QApplication.clipboard().setText("\n".join(parts))
        self._flash_copied("✓ 已复制全部对照（CSV 两列）")

    def _export_all_subtitles(self):
        if not self.subtitle_results:
            QMessageBox.information(self, "没有结果", "请先完成字幕提取。")
            return
        folder = QFileDialog.getExistingDirectory(self, "选择字幕导出目录")
        if not folder:
            return
        output = Path(folder)
        for number, (name, result) in enumerate(self.subtitle_results.items(), 1):
            base = re.sub(r'[\\/:*?"<>|]+', "_", Path(name).stem).strip(" .") or f"字幕_{number:03d}"
            (output / f"{base}.srt").write_text(result.get("srt", ""), encoding="utf-8-sig")
            (output / f"{base}_原文.txt").write_text(result.get("original", ""), encoding="utf-8-sig")
            bilingual = f"【原文】\n{result.get('original', '')}\n\n【简体中文】\n{result.get('chinese', '')}"
            (output / f"{base}_中外文对照.txt").write_text(bilingual, encoding="utf-8-sig")
        QMessageBox.information(self, "导出完成", f"已导出 {len(self.subtitle_results)} 组字幕到：\n{output}")

    def _add_keys_for_provider(self, provider):
        """兼容旧入口：按指定服务添加（使用统一输入框）。"""
        if hasattr(self, "key_assign_mode"):
            index = self.key_assign_mode.findData(provider)
            if index >= 0:
                self.key_assign_mode.setCurrentIndex(index)
        self._add_keys_unified(force_provider=provider)

    def _add_keys_unified(self, force_provider=None):
        edit = getattr(self, "key_bulk_input", None)
        if edit is None and getattr(self, "provider_inputs", None):
            # 极旧布局回退
            edit = next(iter(self.provider_inputs.values()), None)
        if edit is None:
            return
        keys = [line.strip() for line in edit.toPlainText().splitlines() if line.strip()]
        if not keys:
            QMessageBox.information(self, "没有密钥", "请粘贴至少一枚密钥，每行一个。")
            return
        mode = "auto"
        if force_provider in PROVIDERS:
            mode = force_provider
        elif hasattr(self, "key_assign_mode"):
            mode = self.key_assign_mode.currentData() or "auto"

        counts = {p: 0 for p in PROVIDERS}
        skipped, unknown = [], []
        probed_notes = []
        for raw_key in keys:
            key = normalize_api_key(raw_key)
            if not key:
                continue
            if mode == "auto":
                provider = detect_api_provider(key)
                how = "格式"
                if not provider:
                    # 规则认不出时：短超时联网探测，减少「找不到对应服务」
                    provider, how = detect_api_provider_with_probe(key, timeout=8.0)
                    if provider:
                        probed_notes.append(f"{masked_key(key)}→{provider}（{how}）")
                if not provider:
                    unknown.append(masked_key(key))
                    continue
            else:
                provider = mode
            try:
                self.store.add_key(provider, key)
                counts[provider] = counts.get(provider, 0) + 1
            except Exception:
                skipped.append(masked_key(key))

        edit.clear()
        self._refresh_keys()
        parts = [f"{p} {n} 枚" for p, n in counts.items() if n]
        message = "已添加：" + ("、".join(parts) if parts else "0 枚") + "。"
        if probed_notes:
            message += "\n联网辅助识别：" + "；".join(probed_notes[:6])
            if len(probed_notes) > 6:
                message += "…"
        if unknown:
            message += (
                f"\n仍未能识别 {len(unknown)} 枚（请在上方下拉框「强制归入 xxx」后重试）："
                + "、".join(unknown[:5])
                + ("…" if len(unknown) > 5 else "")
            )
        if skipped:
            message += f"\n跳过 {len(skipped)} 枚重复或无效内容。"
        QMessageBox.information(self, "添加完成", message)

    def _add_elevenlabs_web_session(self):
        """添加 ElevenLabs 网页会话（Cookie），支持多账户，扣各自免费点数。"""
        dialog = QDialog(self)
        dialog.setWindowTitle("添加 ElevenLabs 网页会话")
        dialog.resize(560, 520)
        box = QVBoxLayout(dialog)
        tip = QLabel(
            "<b>用途</b>：用自己账号登录态在软件内转语音，扣该账号点数；可多账户轮询。<br/><br/>"
            "<b style='color:#fbbf24'>重要：不要复制 Application→Cookies 的 JSON 列表</b>"
            "（那是统计 Cookie，会报 401）。请按下面做：<br/>"
            "1. 浏览器打开并登录 "
            "<a href='https://elevenlabs.io/app/home'>elevenlabs.io/app/home</a><br/>"
            "2. F12 → <b>Network（网络）</b> → 刷新页面<br/>"
            "3. 过滤 <code>api.elevenlabs.io</code>，点开任意成功请求<br/>"
            "4. Request Headers 里复制（任选其一，推荐从上到下）：<br/>"
            "　• <b>xi-api-key</b>（最稳，贴到下方「xi-api-key」框）<br/>"
            "　• <b>Authorization: Bearer …</b>（贴到 Authorization 框）<br/>"
            "　• 整行 <b>Cookie:</b>（需含 <code>fern_token</code>，贴到 Cookie 框）<br/>"
            "5. 也可把整段 Request Headers 文本直接贴进 Cookie 大框，软件会自动识别。<br/>"
            "6. <b>更简单</b>：用仓库 <code>tools/elevenlabs_capture.user.js</code> "
            "（Tampermonkey 或 F12 控制台粘贴），登录后右下角一键复制。<br/><br/>"
            "<b style='color:#f87171'>为何网页有点数却 TTS 失败？</b><br/>"
            "官方会把「异常活动」账号的<strong>免费档 API</strong>关掉；"
            "Balance 仍可能显示 10000，但本软件与 VideoKit 一样调官方 TTS 接口，会被 401。"
            "请升级付费、换号，或改用微软/Gemini 语音。"
        )
        tip.setWordWrap(True)
        tip.setOpenExternalLinks(True)
        tip.setStyleSheet("color:#cbd5e1;background:#0b1830;padding:10px;border-radius:6px;")
        box.addWidget(tip)

        form = QFormLayout()
        label_edit = QLineEdit()
        label_edit.setPlaceholderText("例如：账号A / 工作号 / 1000点号1")
        cookie_edit = QPlainTextEdit()
        cookie_edit.setPlaceholderText(
            "推荐：直接粘贴 Network 请求头整段，或 Cookie: 那一行\n"
            "格式示例：fern_token=eyJ...; 其它=...\n"
            "不要粘贴 Application 里导出的 {\"name\":\"_ga\",...} JSON 列表"
        )
        cookie_edit.setMinimumHeight(120)
        auth_edit = QLineEdit()
        auth_edit.setPlaceholderText("推荐：Bearer eyJ...（Network 里的 Authorization）")
        xi_edit = QLineEdit()
        xi_edit.setPlaceholderText("最推荐：Network 里的 xi-api-key: sk_… 或网页密钥")
        form.addRow("账户备注", label_edit)
        form.addRow("Cookie / 请求头", cookie_edit)
        form.addRow("Authorization", auth_edit)
        form.addRow("xi-api-key", xi_edit)
        box.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        box.addWidget(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            packed = el_web.pack_web_session(
                cookie=cookie_edit.toPlainText(),
                authorization=auth_edit.text(),
                xi_api_key=xi_edit.text(),
                label=label_edit.text().strip() or "网页会话",
            )
        except ValueError as exc:
            QMessageBox.warning(self, "无法保存", str(exc))
            return
        # 先检测
        ok, message, quota = el_web.verify_session(packed)
        if not ok:
            reply = QMessageBox.question(
                self, "验证失败",
                f"当前会话验证未通过：\n{message}\n\n仍要保存吗？（可稍后「检测选中」）",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return
        try:
            self.store.add_key(
                "ElevenLabs", packed,
                auth_kind="web",
                label=label_edit.text().strip() or "网页会话",
            )
            # 写入检测状态
            items = self.store.data["providers"]["ElevenLabs"]
            if items:
                last = items[-1]
                if ok:
                    detail = message
                    if quota:
                        rem = quota.get("remaining", "?")
                        lim = quota.get("limit", "?")
                        detail = f"剩余 {rem}/{lim} 点"
                        if quota.get("tts_ok") is False:
                            detail += "｜TTS被风控禁用(免费API)"
                        elif quota.get("tts_ok") is True:
                            detail += "｜TTS可用"
                    self.store.mark_use("ElevenLabs", last["id"], "有效", detail)
                else:
                    self.store.mark_use("ElevenLabs", last["id"], "异常", message)
        except Exception as exc:
            QMessageBox.warning(self, "添加失败", str(exc))
            return
        self._refresh_keys()
        extra = ""
        if ok and quota and quota.get("tts_ok") is False:
            extra = (
                "\n\n⚠️ 余额查询成功，但 TTS 探测失败：该账号免费档 API 可能被风控"
                "（网页仍显示 credits）。\n"
                "请关 VPN、换网络重登后重新粘贴 xi-api-key，或升级付费/换号。"
            )
        elif ok and quota and quota.get("tts_ok") is True:
            extra = "\n\n✓ 已通过极短文本 TTS 探测，可以试听/合成。"
        QMessageBox.information(
            self, "已添加网页会话",
            (f"已保存：{label_edit.text().strip() or '网页会话'}\n{message}{extra}\n\n"
             "到「文字转语音」板块选 ElevenLabs + Voice ID 即可批量转语音。\n"
             "可继续添加更多账户实现轮询。")
        )
        write_app_log(f"添加 ElevenLabs 网页会话：{label_edit.text().strip() or '网页会话'}", "INFO", "密钥")

    def _refresh_keys(self):
        if not hasattr(self, "key_table"):
            return
        # 启动/刷新时修正历史误归类（sk_gla→Gladia、Key ID 标错等）
        if not getattr(self, "_keys_reclassified", False):
            try:
                notes = reclassify_misplaced_keys(self.store)
                self._keys_reclassified = True
                if notes:
                    write_app_log("密钥归类修正：" + "；".join(notes[:8]), "INFO", "密钥")
            except Exception as exc:
                write_app_log(f"密钥归类修正跳过：{exc}", "WARN", "密钥")
        self.key_table.setRowCount(0)
        status_colors = {"有效": "#22c55e", "失效": "#ef4444", "格式错误": "#ef4444",
                         "额度受限": "#f59e0b", "异常": "#f97316"}
        for provider in PROVIDERS:
            for item in self.store.data["providers"][provider]:
                row = self.key_table.rowCount(); self.key_table.insertRow(row)
                reason = item.get("last_error", "") or "—"
                compact_reason = " ".join(reason.split())
                key_display = masked_key(item["key"])
                if item.get("auth_kind") == "web" and item.get("label"):
                    key_display = f"网页·{item['label']}"
                elif el_web.is_web_secret(item.get("key") or ""):
                    key_display = el_web.display_secret(item.get("key") or "")
                svc = provider
                if item.get("auth_kind") == "web" or el_web.is_web_secret(item.get("key") or ""):
                    svc = f"{provider}·网页"
                values = [svc, key_display, item.get("status", "未检测"),
                          item.get("last_checked", ""), str(item.get("uses", 0)), compact_reason, item["id"]]
                for col, value in enumerate(values):
                    cell = QTableWidgetItem(value)
                    if col == 5:
                        cell.setToolTip(reason)
                    if not item.get("enabled", True):
                        cell.setForeground(QColor("#64748b"))
                    elif col == 2 and value in status_colors:
                        cell.setForeground(QColor(status_colors[value]))
                    self.key_table.setItem(row, col, cell)

    def _selected_key_jobs(self):
        jobs = []
        for index in self.key_table.selectionModel().selectedRows():
            provider = self.key_table.item(index.row(), 0).text()
            # 显示名可能是「ElevenLabs·网页」
            provider = provider.split("·", 1)[0].strip()
            key_id = self.key_table.item(index.row(), 6).text()
            item = next((x for x in self.store.data["providers"].get(provider, [])
                         if x["id"] == key_id), None)
            if item:
                jobs.append((provider, item.copy()))
        return jobs

    def _check_selected_keys(self):
        jobs = self._selected_key_jobs()
        if not jobs:
            QMessageBox.information(self, "未选择", "请选择要检测的密钥行。")
            return
        self._run_key_check(jobs)

    def _check_all_keys(self):
        jobs = [(p, x.copy()) for p in PROVIDERS for x in self.store.data["providers"][p]]
        if jobs: self._run_key_check(jobs)

    def _run_key_check(self, jobs):
        # 使用独立线程，避免与字幕/流水线共用 self.thread 互相踩踏
        if getattr(self, "_key_check_thread", None):
            try:
                if self._key_check_thread.isRunning():
                    QMessageBox.information(self, "任务进行中", "请等待当前密钥检测结束。")
                    return
            except RuntimeError:
                self._key_check_thread = None
        # 兼容：字幕任务占用 self.thread 时也提示
        if self.thread:
            try:
                if self.thread.isRunning():
                    QMessageBox.information(self, "任务进行中", "请等待当前任务结束。")
                    return
            except RuntimeError:
                self.thread = None

        self._key_check_thread = QThread(self)
        self._key_check_worker = KeyCheckWorker(jobs)
        self._key_check_worker.moveToThread(self._key_check_thread)
        self._key_check_thread.started.connect(self._key_check_worker.run)
        # 显式排队到主线程，禁止在工作线程弹窗/改 UI
        self._key_check_worker.progress.connect(
            self._key_check_result, Qt.ConnectionType.QueuedConnection)
        self._key_check_worker.finished.connect(
            self._key_check_done, Qt.ConnectionType.QueuedConnection)
        self._key_check_worker.finished.connect(self._key_check_thread.quit)
        self._key_check_thread.finished.connect(self._key_check_cleanup)
        self._key_check_thread.start()

    def _key_check_result(self, provider, key_id, ok, message):
        if ok:
            status = "有效"
        elif "HTTP 429" in message:
            status = "额度受限"
        elif message.startswith("密钥格式异常"):
            status = "格式错误"
        elif "HTTP 401" in message or "HTTP 403" in message:
            status = "失效"
        else:
            status = "异常"
        self.store.update_key(provider, key_id, status=status,
                              last_checked=datetime.now().strftime("%Y-%m-%d %H:%M"), last_error="" if ok else message)
        # progress 经 QueuedConnection 回到主线程，可安全刷新表格
        self._refresh_keys()

    def _key_check_done(self):
        """必须在主线程执行：最终刷新并提示（禁止在工作线程弹窗）。"""
        self._refresh_keys()
        QMessageBox.information(self, "检测完成", "密钥检测已完成。")

    def _key_check_cleanup(self):
        worker = getattr(self, "_key_check_worker", None)
        thread = getattr(self, "_key_check_thread", None)
        self._key_check_worker = None
        self._key_check_thread = None
        if worker is not None:
            worker.deleteLater()
        if thread is not None:
            thread.deleteLater()

    def _show_selected_key_error(self):
        rows = self.key_table.selectionModel().selectedRows()
        if not rows:
            QMessageBox.information(self, "未选择", "请先选择一行密钥。")
            return
        self._show_key_error(rows[0].row())

    def _show_key_error(self, row):
        provider_item = self.key_table.item(row, 0)
        key_id_item = self.key_table.item(row, 6)
        if not provider_item or not key_id_item:
            return
        provider = provider_item.text(); key_id = key_id_item.text()
        item = next((x for x in self.store.data["providers"][provider] if x["id"] == key_id), None)
        if not item:
            return
        reason = item.get("last_error") or "没有错误记录。该密钥尚未检测，或最近一次检测通过。"
        detail = (f"服务：{provider}\n密钥：{masked_key(item['key'])}\n"
                  f"状态：{item.get('status', '未检测')}\n"
                  f"检测时间：{item.get('last_checked') or '尚未检测'}\n\n"
                  f"检测详情：\n{reason}")
        box = QMessageBox(self); box.setWindowTitle("密钥检测详情"); box.setIcon(QMessageBox.Icon.Information)
        box.setText("密钥状态诊断"); box.setDetailedText(detail); box.setInformativeText(reason)
        box.exec()

    def _thread_ended(self):
        self.worker = None
        self.thread = None

    def _toggle_key(self):
        jobs = self._selected_key_jobs()
        for provider, item in jobs:
            self.store.update_key(provider, item["id"], enabled=not item.get("enabled", True))
        self._refresh_keys()

    def _remove_key(self):
        jobs = self._selected_key_jobs()
        if not jobs: return
        if QMessageBox.question(self, "确认删除", f"确定删除选中的 {len(jobs)} 枚密钥？") != QMessageBox.StandardButton.Yes:
            return
        for provider, item in jobs:
            self.store.remove_key(provider, item["id"])
        self._refresh_keys()



    def _check_update(self, manual=False):
        thread = getattr(self, "_update_thread", None)
        if thread is not None:
            try:
                if thread.isRunning():
                    if manual:
                        QMessageBox.information(self, "检查更新", "正在检查中，请稍候...")
                    return
            except RuntimeError:
                self._update_thread = None

        self._update_manual_check = manual
        self._update_thread = QThread(self)
        self._update_worker = UpdateCheckWorker(APP_VERSION)
        self._update_worker.moveToThread(self._update_thread)
        self._update_thread.started.connect(self._update_worker.run)
        # 禁止在 finished 槽里 wait 自己的线程（会死锁/跨线程弹窗崩溃）
        self._update_worker.finished.connect(
            self._on_update_finished, Qt.ConnectionType.QueuedConnection)
        # quit 不接收参数，用 lambda 吞掉 signal 的 5 个参数，避免槽签名错位
        self._update_worker.finished.connect(lambda *_args: self._update_thread and self._update_thread.quit())
        self._update_thread.finished.connect(self._update_thread_cleanup)
        self._update_thread.start()

    def _on_update_finished(self, has_new, latest_version, download_url, filename, error):
        manual = getattr(self, "_update_manual_check", False)
        error_text = str(error or "").strip()
        # 防御：历史上若槽参数错位，会把 zip/exe 文件名当成 error 弹出「检测失败」
        if error_text and error_text.lower().endswith((".zip", ".exe", ".msi", ".dmg", ".pkg")):
            if " " not in error_text and "://" not in error_text and len(error_text) < 160:
                if latest_version and not has_new:
                    error_text = ""
                elif has_new and download_url:
                    error_text = ""
                else:
                    error_text = (
                        f"更新检查结果异常，请重试或到 GitHub Releases 手动下载。\n（内部信息：{error_text}）"
                    )

        if error_text:
            if manual:
                QMessageBox.warning(self, "检查更新失败", f"检测失败，错误原因：\n{error_text}")
            return

        if has_new:
            if not str(download_url or "").strip():
                if manual:
                    QMessageBox.warning(
                        self, "检查更新",
                        f"发现新版本 v{latest_version}，但没有可用的下载地址。\n"
                        "请到 GitHub Releases 页面手动下载。",
                    )
                return
            package = str(filename or Path(str(download_url).split("?")[0]).name or "安装包")
            is_setup = package.lower().endswith(".exe")
            prompt = (
                f"发现新版本 v{latest_version}（当前版本 v{APP_VERSION}）。\n"
                f"安装包：{package}\n\n"
                + ("是否立即下载并运行升级安装程序？" if is_setup else
                   "是否立即下载绿色免安装包？（下载后请解压覆盖使用）")
            )
            reply = QMessageBox.question(
                self, "检测到新版本", prompt,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if reply == QMessageBox.StandardButton.Yes:
                self._start_update_download(latest_version, download_url, filename)
        else:
            if manual:
                shown = latest_version or APP_VERSION
                QMessageBox.information(
                    self, "已经是最新版本",
                    f"当前版本 v{APP_VERSION} 已经是最新版本"
                    + (f"（远程 v{shown}）" if shown and shown != APP_VERSION else "")
                    + "！",
                )

    def _update_thread_cleanup(self):
        worker = getattr(self, "_update_worker", None)
        thread = getattr(self, "_update_thread", None)
        self._update_worker = None
        self._update_thread = None
        if worker is not None:
            worker.deleteLater()
        if thread is not None:
            thread.deleteLater()

    def _start_update_download(self, version, url, filename):
        thread = getattr(self, "_download_thread", None)
        if thread is not None:
            try:
                if thread.isRunning():
                    QMessageBox.information(self, "下载进行中", "已有更新包正在下载，请稍候。")
                    return
            except RuntimeError:
                self._download_thread = None

        QMessageBox.information(
            self, "开始下载",
            "最新版更新包已在后台开始静默下载。下载期间您可以继续正常使用软件，下载完成后将会自动提示您安装。")

        if not str(url or "").strip():
            QMessageBox.warning(self, "无法下载", "下载地址为空，请到 GitHub Releases 手动下载。")
            return

        self._download_thread = QThread(self)
        self._download_worker = DownloadWorker(url, version, filename)
        self._download_worker.moveToThread(self._download_thread)
        self._download_thread.started.connect(self._download_worker.run)
        self._download_worker.progress.connect(self._on_download_progress)
        self._download_worker.finished.connect(
            self._on_download_finished, Qt.ConnectionType.QueuedConnection)
        self._download_worker.finished.connect(lambda *_args: self._download_thread and self._download_thread.quit())
        self._download_thread.finished.connect(self._download_thread_cleanup)
        self._download_thread.start()

    def _on_download_progress(self, val):
        pass

    def _on_download_cancelled(self):
        if getattr(self, "_download_worker", None):
            self._download_worker.cancelled = True

    def _on_download_finished(self, success, file_path, error):
        if success:
            is_exe = str(file_path).lower().endswith(".exe")
            if is_exe:
                reply = QMessageBox.question(
                    self, "新版本下载完成",
                    "最新版本的升级安装包已在后台下载完成！\n是否现在退出本软件并启动升级安装？",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.Yes
                )
                if reply == QMessageBox.StandardButton.Yes:
                    try:
                        import subprocess
                        subprocess.Popen([file_path], shell=True)
                        self.close()
                    except Exception as e:
                        QMessageBox.warning(
                            self, "运行安装包失败",
                            f"启动升级安装程序失败，请手动打开文件安装：\n{file_path}\n错误信息: {e}")
            else:
                QMessageBox.information(
                    self, "绿色免安装版下载完成",
                    f"最新版本的绿色免安装压缩包已在后台下载完成！\n\n存储路径：\n{file_path}\n\n请解压该文件后使用新版。"
                )
                try:
                    import os
                    os.startfile(os.path.dirname(file_path))
                except Exception:
                    pass
        else:
            is_cancelled = bool(
                getattr(self, "_download_worker", None)
                and self._download_worker.cancelled)
            if not is_cancelled:
                releases = "https://github.com/secure-artifacts/video-toolkit/releases/latest"
                box = QMessageBox(self)
                box.setIcon(QMessageBox.Icon.Warning)
                box.setWindowTitle("下载失败")
                box.setText("后台下载升级安装包失败。")
                box.setInformativeText(
                    f"{error}\n\n若网络访问 GitHub 不稳定，请用浏览器打开 Releases 手动下载。"
                )
                open_btn = box.addButton("打开下载页", QMessageBox.ButtonRole.AcceptRole)
                box.addButton("关闭", QMessageBox.ButtonRole.RejectRole)
                box.exec()
                if box.clickedButton() is open_btn:
                    try:
                        from PySide6.QtCore import QUrl
                        from PySide6.QtGui import QDesktopServices
                        QDesktopServices.openUrl(QUrl(releases))
                    except Exception:
                        try:
                            import webbrowser
                            webbrowser.open(releases)
                        except Exception:
                            pass

    def _download_thread_cleanup(self):
        worker = getattr(self, "_download_worker", None)
        thread = getattr(self, "_download_thread", None)
        self._download_worker = None
        self._download_thread = None
        if worker is not None:
            worker.deleteLater()
        if thread is not None:
            thread.deleteLater()

STYLE = """
QWidget { background:#080d19; color:#e5edf9; font-family:'Segoe UI','Microsoft YaHei UI','Microsoft YaHei',sans-serif; font-size:12px; }
#nav { background:qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 #101a35,stop:.55 #111b2e,stop:1 #0b1325); border-bottom:1px solid #263655; }
#brand { font-size:18px; font-weight:800; color:#f8fbff; padding-right:8px; }
#navButton { padding:8px 10px; border:1px solid transparent; border-radius:7px; color:#9cacbf; }
#navButton:hover { background:#192844; color:#f3f7ff; border-color:#2b4268; }
#navButton:checked { background:qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 #2563eb,stop:1 #7c3aed); color:white; font-weight:700; }
#updateNavButton { padding:8px 12px; border:1px solid #3b82f6; border-radius:7px; color:#bfdbfe; background:#13233f; font-weight:700; }
#updateNavButton:hover { background:#1d4ed8; color:white; border-color:#60a5fa; }
#logNavButton { padding:8px 12px; border:1px solid #475569; border-radius:7px; color:#e2e8f0; background:#172033; font-weight:600; }
#logNavButton:hover { background:#334155; color:white; border-color:#94a3b8; }
#heading { font-size:24px; font-weight:800; color:#f8fbff; }
#toolCard, #panel { background:qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 #121d31,stop:1 #0d1627); border:1px solid #263957; border-radius:12px; }
#toolCard:hover { border-color:#3b82f6; }
QPushButton { background:#17243a; border:1px solid #30445f; border-radius:6px; padding:6px 11px; min-height:18px; }
QPushButton:hover { background:#223654; border-color:#4d6d97; }
QPushButton:disabled { color:#64748b; background:#172033; }
#primary { background:qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 #0ea5e9,stop:1 #6366f1); border-color:#60a5fa; color:white; font-weight:700; padding:7px 15px; }
#primary:hover { background:qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 #38bdf8,stop:1 #818cf8); }
QLineEdit, QComboBox, QListWidget, QPlainTextEdit, QTextEdit, QTableWidget { background:#0c1424; border:1px solid #2b3d58; border-radius:5px; padding:4px; selection-background-color:#2563eb; }
/*
 * SpinBox 数字区：Win11 + 样式表时若宽度不足或按钮 subcontrol 未固定，
 * 数字与后缀会被按钮盖住/裁切，残成 I/O/x/± 等“乱码”。必须：
 * 1) 足够 min-width 容纳值+后缀；2) 明确上下按钮占位，把文字挤在左侧。
 */
QSpinBox, QDoubleSpinBox {
  background:#0c1424; border:1px solid #2b3d58; border-radius:5px;
  padding: 3px 22px 3px 6px;
  min-height: 28px;
  min-width: 96px;
  color: #f1f5f9;
  selection-background-color: #2563eb;
  font-family: 'Segoe UI', 'Microsoft YaHei UI', 'Microsoft YaHei', Arial, sans-serif;
  font-size: 12px;
}
QSpinBox::up-button, QDoubleSpinBox::up-button {
  subcontrol-origin: border;
  subcontrol-position: top right;
  width: 18px;
  border-left: 1px solid #2b3d58;
  border-bottom: 1px solid #2b3d58;
  border-top-right-radius: 4px;
  background: #17243a;
}
QSpinBox::down-button, QDoubleSpinBox::down-button {
  subcontrol-origin: border;
  subcontrol-position: bottom right;
  width: 18px;
  border-left: 1px solid #2b3d58;
  border-bottom-right-radius: 4px;
  background: #17243a;
}
QSpinBox::up-button:hover, QDoubleSpinBox::up-button:hover,
QSpinBox::down-button:hover, QDoubleSpinBox::down-button:hover {
  background: #223654;
}
/* 不重绘箭头，保留系统/Qt 默认三角，避免 Win11 上箭头样式把文字区挤坏 */
QGroupBox { background:#101a2b; border:1px solid #293d5c; border-radius:8px; margin-top:8px; padding-top:7px; font-weight:700; }
QGroupBox::title { subcontrol-origin:margin; left:9px; padding:0 4px; color:#b8c8dc; }
QHeaderView::section { background:#17243a; color:#cbd5e1; border:none; padding:6px; }
QProgressBar { background:#17243a; border:none; border-radius:5px; text-align:center; min-height:16px; }
QProgressBar::chunk { background:qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 #06b6d4,stop:1 #6366f1); border-radius:5px; }
QScrollArea { border:none; }
QTabWidget::pane { border:1px solid #2b3d58; background:#0b1322; top:-1px; }
QTabBar::tab { background:#17243a; color:#cbd5e1; border:1px solid #30445f; padding:8px 16px; min-width:90px; }
QTabBar::tab:hover { background:#223654; color:white; }
QTabBar::tab:selected { background:#2563eb; color:white; border-color:#60a5fa; font-weight:700; }
QToolBar { background:#0d1627; border-bottom:1px solid #263957; spacing:6px; padding:4px; }
QScrollBar:vertical { background:#091221; width:14px; margin:2px; border-radius:7px; }
QScrollBar::handle:vertical { background:#46658d; min-height:34px; margin:1px; border-radius:6px; }
QScrollBar::handle:vertical:hover { background:#60a5fa; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height:0px; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background:transparent; }
QScrollBar:horizontal { background:#091221; height:14px; margin:2px; border-radius:7px; }
QScrollBar::handle:horizontal { background:#46658d; min-width:34px; margin:1px; border-radius:6px; }
QScrollBar::handle:horizontal:hover { background:#60a5fa; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width:0px; }
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal { background:transparent; }
QSlider::groove:horizontal { height:8px; background:#1c2d45; border-radius:4px; }
QSlider::sub-page:horizontal { background:qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 #06b6d4,stop:1 #6366f1); border-radius:4px; }
QSlider::handle:horizontal { background:#e0f2fe; border:2px solid #38bdf8; width:18px; margin:-6px 0; border-radius:9px; }
QSlider::handle:horizontal:hover { background:white; border-color:#818cf8; }
QSplitter::handle { background:#263957; }
QSplitter::handle:hover { background:#3b82f6; }
"""


def _acquire_single_instance() -> bool:
    """Keep one VideoToolkit window on Windows; retain the mutex for process lifetime."""
    global _SINGLE_INSTANCE_MUTEX
    if os.name != "nt":
        return True
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        handle = kernel32.CreateMutexW(None, False, "Local\\VideoToolkit_SingleInstance")
        if not handle:
            return True
        if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            kernel32.CloseHandle(handle)
            return False
        _SINGLE_INSTANCE_MUTEX = handle
    except Exception:
        return True
    return True


def _focus_existing_instance() -> None:
    """Best-effort: bring an already-running VideoToolkit window to the front."""
    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        found = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        def _enum(hwnd, _lparam):
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value or ""
            if "视频工具合集" in title or "VideoToolkit" in title:
                found.append(hwnd)
            return True

        user32.EnumWindows(_enum, 0)
        if not found:
            return
        hwnd = found[0]
        # Restore if minimized, then foreground
        SW_RESTORE = 9
        user32.ShowWindow(hwnd, SW_RESTORE)
        user32.SetForegroundWindow(hwnd)
    except Exception:
        pass


def main():
    # A damaged macOS bundle once resolved "ffmpeg" to the app bootloader.
    # Media probes must never construct a second application window.
    if os.environ.get("VIDEO_TOOLKIT_MEDIA_PROBE") == "1":
        return
    if not _acquire_single_instance():
        msg = (
            "VideoToolkit 已在运行（单实例），本次启动已退出。\n"
            "请到任务栏点「视频工具合集」窗口；若看不到，可在任务管理器结束 "
            "VideoToolkit.exe / python.exe 后再启动。\n"
            "开发时若要多开，可先关掉已安装版或旧进程。"
        )
        write_app_log("程序已在运行，本次重复启动已退出。", "INFO", "应用")
        try:
            print(msg, flush=True)
        except Exception:
            pass
        _focus_existing_instance()
        # 尽量弹窗提醒（无事件循环时 MessageBox 仍可用）
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                0, msg, "VideoToolkit 已在运行", 0x40,
            )
        except Exception:
            pass
        return
    _startup_trace("main entered")
    write_app_log(
        f"启动 {APP_DISPLAY_NAME} | instance={instance_id()}",
        "INFO",
        "应用",
    )
    original_hook=sys.excepthook
    def log_unhandled(exc_type,exc_value,exc_traceback):
        import traceback
        detail="".join(traceback.format_exception(exc_type,exc_value,exc_traceback))
        write_app_log(f"未处理异常：{exc_type.__name__}: {exc_value}\n{detail}","ERROR","应用")
        original_hook(exc_type,exc_value,exc_traceback)
    sys.excepthook=log_unhandled
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    try:
        from modules.settings_page import ensure_toolkit_packages_on_path
        ensure_toolkit_packages_on_path()
        _startup_trace("toolkit python_packages on path")
    except Exception:
        pass
    app = QApplication(sys.argv)
    _startup_trace("QApplication ready")
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    app.setStyleSheet(STYLE)
    icon = resource_path("logo.ico")
    if icon.exists(): app.setWindowIcon(QIcon(str(icon)))
    window = MainWindow()
    window.showMaximized()
    window.raise_()
    window.activateWindow()
    _startup_trace(
        f"window shown platform={app.platformName()} visible={window.isVisible()} "
        f"winId={int(window.winId())} geometry={window.geometry().getRect()} "
        f"instance={instance_id()}"
    )
    QTimer.singleShot(350, lambda: (window.raise_(), window.activateWindow()))
    # 打包后自动化启动检查使用；普通用户启动时不会触发。
    if os.environ.get("VIDEO_TOOLKIT_SMOKE_TEST", "").strip() == "1":
        QTimer.singleShot(1800, app.quit)
    sys.exit(app.exec())




class FeedbackSubmitWorker(QObject):
    log = Signal(str)
    finished = Signal(bool, str)
    
    def __init__(self, settings, title, content, attachments):
        super().__init__()
        self.settings = settings
        self.title = title
        self.content = content
        self.attachments = attachments
        
    def run(self):
        try:
            self.log.emit("正在准备反馈数据与附件...")
            import base64
            import mimetypes
            import requests
            
            # Load credentials to get the user's identity
            identity = "Anonymous"
            try:
                credentials, identity = load_google_credentials(self.settings, interactive=False)
            except Exception:
                pass
                
            attachments_payload = []
            for idx, path in enumerate(self.attachments, 1):
                p = Path(path)
                if p.is_file():
                    self.log.emit(f"正在读取并转换附件 ({idx}/{len(self.attachments)}): {p.name}...")
                    file_bytes = p.read_bytes()
                    base64_str = base64.b64encode(file_bytes).decode("utf-8")
                    mimetype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
                    attachments_payload.append({
                        "name": p.name,
                        "mimeType": mimetype,
                        "base64": base64_str
                    })
                    
            payload = {
                "user": identity,
                "title": self.title,
                "content": self.content,
                "attachments": attachments_payload
            }
            
            self.log.emit("正在通过 Web App 提交反馈（正在上传附件，这可能需要几分钟，请耐心等待）...")
            url = "https://script.google.com/macros/s/AKfycbw43iki16bBIfruuF_9YrbrZplvKQgGyYExEtweoDMv7fCQtlMgjqlr9uyCNCapeN_o/exec"
            
            response = requests.post(url, json=payload, timeout=600)
            if response.status_code == 200:
                res_data = response.json()
                if res_data.get("success"):
                    self.finished.emit(True, "反馈已成功通过 Web App 提交！感谢您的反馈。")
                else:
                    error_msg = res_data.get("error", "未知错误")
                    raise RuntimeError(error_msg)
            else:
                raise RuntimeError(f"HTTP 请求失败：状态码 {response.status_code}")
        except Exception as exc:
            self.finished.emit(False, str(exc))


class FeedbackDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("💬 问题反馈与建议")
        self.resize(500, 450)
        self.settings = parent.settings if parent and hasattr(parent, "settings") else {}
        
        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        
        info = QLabel(
            "<b>填写问题反馈：</b><br/>"
            "支持上传截图与问题视频。<br/>"
            "⚠️ <b>注意</b>：Google 限制单次上传最大 50MB。国内网络上传视频较慢，请优先使用图片截图；若上传视频，提交时请耐心等待几分钟。"
        )
        info.setWordWrap(True)
        info.setStyleSheet("color: #93c5fd; background: #1e293b; padding: 10px; border-radius: 5px;")
        layout.addWidget(info)
        
        form = QFormLayout()
        self.title_input = QLineEdit()
        self.title_input.setPlaceholderText("一句话简述您的问题")
        
        self.content_input = QPlainTextEdit()
        self.content_input.setPlaceholderText("请详细描述您遇到的问题、操作步骤或建议...")
        
        form.addRow("反馈标题:", self.title_input)
        form.addRow("问题描述:", self.content_input)
        layout.addLayout(form)
        
        att_label = QLabel("附件列表 (支持图片、视频):")
        layout.addWidget(att_label)
        
        self.att_list = QListWidget()
        self.att_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        layout.addWidget(self.att_list)
        
        btn_layout = QHBoxLayout()
        add_btn = QPushButton("添加图片/视频")
        add_btn.clicked.connect(self.add_attachment)
        btn_layout.addWidget(add_btn)
        
        self.remove_btn = QPushButton("删除选中")
        self.remove_btn.clicked.connect(self.remove_attachment)
        self.remove_btn.setEnabled(False)
        self.att_list.itemSelectionChanged.connect(lambda: self.remove_btn.setEnabled(bool(self.att_list.selectedItems())))
        btn_layout.addWidget(self.remove_btn)
        btn_layout.addStretch()
        layout.addLayout(btn_layout)
        
        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet("color: #bae6fd;")
        layout.addWidget(self.status_label)
        
        actions = QHBoxLayout()
        self.submit_btn = QPushButton("提交反馈")
        self.submit_btn.setObjectName("primary")
        self.submit_btn.setFixedSize(120, 36)
        self.submit_btn.clicked.connect(self.submit_feedback)
        
        cancel_btn = QPushButton("取消")
        cancel_btn.setFixedSize(80, 36)
        cancel_btn.clicked.connect(self.reject)
        
        actions.addStretch()
        actions.addWidget(self.submit_btn)
        actions.addWidget(cancel_btn)
        layout.addLayout(actions)
        
        self.attachments = []
        self.worker = None
        self.thread = None
        
    def add_attachment(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "选择反馈附件", "",
            "媒体文件 (*.png *.jpg *.jpeg *.mp4 *.mov *.avi *.mkv);;所有文件 (*.*)"
        )
        if paths:
            for p in paths:
                if p not in self.attachments:
                    self.attachments.append(p)
                    self.att_list.addItem(Path(p).name)
                    
    def remove_attachment(self):
        selected = self.att_list.currentRow()
        if selected != -1:
            self.att_list.takeItem(selected)
            self.attachments.pop(selected)
            
    def submit_feedback(self):
        title = self.title_input.text().strip()
        content = self.content_input.toPlainText().strip()
        
        if not title:
            QMessageBox.warning(self, "信息不全", "请填写反馈标题！")
            return
            
        self.submit_btn.setEnabled(False)
        self.title_input.setEnabled(False)
        self.content_input.setEnabled(False)
        
        self.thread = QThread()
        self.worker = FeedbackSubmitWorker(self.settings, title, content, self.attachments)
        self.worker.moveToThread(self.thread)
        
        self.thread.started.connect(self.worker.run)
        self.worker.log.connect(self.status_label.setText)
        self.worker.finished.connect(self.on_finished)
        
        self.thread.start()
        
    def on_finished(self, ok, msg):
        # 勿在 UI 线程长时间 wait 工作线程，避免界面卡住
        thread = getattr(self, "thread", None)
        if thread is not None:
            try:
                thread.quit()
                if not thread.wait(3000):
                    thread.terminate()
            except RuntimeError:
                pass

        if ok:
            QMessageBox.information(self, "提交成功", msg)
            self.accept()
        else:
            QMessageBox.critical(self, "提交失败", f"发生错误：\n{msg}")
            self.submit_btn.setEnabled(True)
            self.title_input.setEnabled(True)
            self.content_input.setEnabled(True)
            self.status_label.setText("")


if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    main()
