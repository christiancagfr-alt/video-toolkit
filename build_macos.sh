#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MEDIA_BIN="${VIDEO_TOOLKIT_MEDIA_BIN:-}"
APP_VERSION="${VIDEO_TOOLKIT_VERSION:-${GITHUB_REF_NAME:-}}"
if [[ -z "$APP_VERSION" ]]; then
  APP_VERSION="$(git -C "$ROOT_DIR" describe --tags --abbrev=0 2>/dev/null || true)"
fi
APP_VERSION="${APP_VERSION#v}"
APP_VERSION="${APP_VERSION:-1.7.65}"
if [[ ! "$APP_VERSION" =~ ^[0-9A-Za-z._-]+$ ]]; then
  echo "Invalid application version: $APP_VERSION" >&2
  exit 1
fi
VERSION_HOOK="$(mktemp "${TMPDIR:-/tmp}/video_toolkit_version.XXXXXX.py")"
trap 'rm -f "$VERSION_HOOK"' EXIT
printf "import os\nos.environ['VIDEO_TOOLKIT_VERSION'] = '%s'\n" "$APP_VERSION" > "$VERSION_HOOK"
echo "Embedding application version: $APP_VERSION"

if [[ -z "$MEDIA_BIN" || ! -x "$MEDIA_BIN/ffmpeg" || ! -x "$MEDIA_BIN/ffprobe" ]]; then
  echo "VIDEO_TOOLKIT_MEDIA_BIN must contain executable ffmpeg and ffprobe files." >&2
  exit 1
fi

python -m PyInstaller \
  --noconfirm \
  --clean \
  --windowed \
  --onedir \
  --name VideoToolkit \
  --osx-bundle-identifier com.secureartifacts.videotoolkit \
  --icon "$ROOT_DIR/logo.icns" \
  --add-data "$ROOT_DIR/logo.ico:." \
  --add-data "$ROOT_DIR/VERSION:." \
  --add-data "$ROOT_DIR/resources/fonts:resources/fonts" \
  --add-data "$ROOT_DIR/resources/language_packs:resources/language_packs" \
  --add-binary "$MEDIA_BIN/ffmpeg:." \
  --add-binary "$MEDIA_BIN/ffprobe:." \
  --collect-data faster_whisper \
  --collect-binaries ctranslate2 \
  --collect-data onnxruntime \
  --collect-binaries onnxruntime \
  --hidden-import faster_whisper \
  --hidden-import onnxruntime \
  --hidden-import yt_dlp \
  --hidden-import modules.caption_qt_burn \
  --hidden-import modules.dynamic_caption_page \
  --hidden-import modules.vertical_stack \
  --hidden-import modules.editor_assist \
  --hidden-import modules.editor_assist_dialog \
  --hidden-import modules.caption_rich \
  --collect-submodules yt_dlp \
  --collect-data yt_dlp \
  --hidden-import google_auth_oauthlib \
  --hidden-import googleapiclient.discovery \
  --runtime-hook "$VERSION_HOOK" \
  --exclude-module torch \
  --exclude-module tensorflow \
  --exclude-module matplotlib \
  --noupx \
  --distpath "$ROOT_DIR/dist_macos" \
  --workpath "$ROOT_DIR/build_macos" \
  --specpath "$ROOT_DIR" \
  "$ROOT_DIR/app.py"
