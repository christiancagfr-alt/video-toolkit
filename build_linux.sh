#!/usr/bin/env bash
# Build portable Linux x64 onedir package with bundled FFmpeg / FFprobe.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MEDIA_BIN="${VIDEO_TOOLKIT_MEDIA_BIN:-}"
APP_VERSION="${VIDEO_TOOLKIT_VERSION:-${GITHUB_REF_NAME:-}}"
if [[ -z "$APP_VERSION" ]]; then
  APP_VERSION="$(git -C "$ROOT_DIR" describe --tags --abbrev=0 2>/dev/null || true)"
fi
APP_VERSION="${APP_VERSION#v}"
if [[ -z "$APP_VERSION" && -f "$ROOT_DIR/VERSION" ]]; then
  APP_VERSION="$(tr -d '[:space:]' < "$ROOT_DIR/VERSION")"
fi
APP_VERSION="${APP_VERSION:-1.7.70}"
if [[ ! "$APP_VERSION" =~ ^[0-9A-Za-z._-]+$ ]]; then
  echo "Invalid application version: $APP_VERSION" >&2
  exit 1
fi

VERSION_HOOK="$(mktemp "${TMPDIR:-/tmp}/video_toolkit_version.XXXXXX.py")"
trap 'rm -f "$VERSION_HOOK"' EXIT
printf "import os\nos.environ['VIDEO_TOOLKIT_VERSION'] = '%s'\n" "$APP_VERSION" > "$VERSION_HOOK"
echo "Embedding application version: $APP_VERSION"

if [[ -z "$MEDIA_BIN" ]]; then
  if command -v ffmpeg >/dev/null 2>&1 && command -v ffprobe >/dev/null 2>&1; then
    MEDIA_BIN="$(dirname "$(command -v ffmpeg)")"
  fi
fi
if [[ -z "$MEDIA_BIN" || ! -x "$MEDIA_BIN/ffmpeg" || ! -x "$MEDIA_BIN/ffprobe" ]]; then
  echo "VIDEO_TOOLKIT_MEDIA_BIN must contain executable ffmpeg and ffprobe." >&2
  echo "Example: export VIDEO_TOOLKIT_MEDIA_BIN=/usr/bin" >&2
  exit 1
fi

DIST_DIR="$ROOT_DIR/dist_linux"
WORK_DIR="$ROOT_DIR/build_linux"
rm -rf "$DIST_DIR" "$WORK_DIR"

# Linux: use onedir + console=false via --windowed (no terminal flash).
# --contents-directory keeps layout similar to Windows portable build.
python -m PyInstaller \
  --noconfirm \
  --clean \
  --windowed \
  --onedir \
  --contents-directory internal \
  --name VideoToolkit \
  --icon "$ROOT_DIR/logo.ico" \
  --add-data "$ROOT_DIR/logo.ico:." \
  --add-data "$ROOT_DIR/VERSION:." \
  --add-data "$ROOT_DIR/resources/fonts:resources/fonts" \
  --add-data "$ROOT_DIR/resources/language_packs:resources/language_packs" \
  --add-binary "$MEDIA_BIN/ffmpeg:." \
  --add-binary "$MEDIA_BIN/ffprobe:." \
  --collect-data faster_whisper \
  --collect-all ctranslate2 \
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
  --exclude-module torchvision \
  --exclude-module torchaudio \
  --exclude-module tensorflow \
  --exclude-module matplotlib \
  --exclude-module IPython \
  --exclude-module jupyter \
  --noupx \
  --distpath "$DIST_DIR" \
  --workpath "$WORK_DIR" \
  --specpath "$ROOT_DIR" \
  "$ROOT_DIR/app.py"

APP_DIR="$DIST_DIR/VideoToolkit"
if [[ ! -x "$APP_DIR/VideoToolkit" ]]; then
  echo "Build failed: $APP_DIR/VideoToolkit not found" >&2
  exit 1
fi

# Launcher: set library path hints for common distros (Wayland/X11)
cat > "$APP_DIR/run-videotoolkit.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PATH="$HERE/internal${PATH:+:$PATH}"
# Prefer bundled internal libs when present
if [[ -d "$HERE/internal" ]]; then
  export LD_LIBRARY_PATH="$HERE/internal${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
fi
# Qt platform plugins
if [[ -d "$HERE/internal/PySide6/Qt/plugins" ]]; then
  export QT_PLUGIN_PATH="$HERE/internal/PySide6/Qt/plugins${QT_PLUGIN_PATH:+:$QT_PLUGIN_PATH}"
fi
exec "$HERE/VideoToolkit" "$@"
EOF
chmod +x "$APP_DIR/run-videotoolkit.sh" "$APP_DIR/VideoToolkit"

cat > "$APP_DIR/README-Linux.txt" <<EOF
VideoToolkit Linux portable package (v$APP_VERSION)

Run:
  ./run-videotoolkit.sh
  # or
  ./VideoToolkit

Requires a desktop environment with OpenGL/X11 or Wayland.
On Debian/Ubuntu, if the GUI fails to start, install:
  sudo apt-get install -y libxcb-cursor0 libxkbcommon-x11-0 libegl1 libgl1

FFmpeg and FFprobe are bundled next to the app binary.
EOF

echo "Linux build complete: $APP_DIR"
echo "Start with: $APP_DIR/run-videotoolkit.sh"

# Ensure ctranslate2 package init next to native libs (StorageView for faster-whisper)
INIT_SRC="$ROOT_DIR/tools/packaging/ctranslate2_frozen_init.py"
while IFS= read -r -d '' ct2_dir; do
  if [[ -f "$INIT_SRC" ]]; then
    cp -f "$INIT_SRC" "$ct2_dir/__init__.py"
    [[ -f "$ct2_dir/version.py" ]] || printf '%s\n' '__version__ = "bundled"' > "$ct2_dir/version.py"
    echo "ctranslate2 frozen package init ensured at $ct2_dir"
  fi
done < <(find "$ROOT_DIR" -type d -name ctranslate2 \( -path '*/_internal/ctranslate2' -o -path '*/Contents/Frameworks/ctranslate2' \) 2>/dev/null | tr '\n' '\0')

