$ErrorActionPreference = 'Stop'
# Qt uses Windows ICU. Unrelated Codex/Poppler ICU on PATH has incompatible
# versioned exports and must not be collected into the application bundle.
$env:PATH = (($env:PATH -split ';') | Where-Object { $_ -notmatch 'codex-runtimes|[\\/]poppler[\\/]' }) -join ';'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$dist = Join-Path $root 'dist_folder'
$version = $env:VIDEO_TOOLKIT_VERSION
if ([string]::IsNullOrWhiteSpace($version)) { $version = $env:GITHUB_REF_NAME }
if ([string]::IsNullOrWhiteSpace($version)) {
  try { $version = (& git -C $root describe --tags --abbrev=0 2>$null).Trim() } catch { $version = '' }
}
if ([string]::IsNullOrWhiteSpace($version)) {
  $versionFile = Join-Path $root 'VERSION'
  if (Test-Path $versionFile) { $version = (Get-Content -Raw -LiteralPath $versionFile).Trim() }
}
if ([string]::IsNullOrWhiteSpace($version)) { $version = '1.7.61' }
$version = $version.Trim().TrimStart('v')
if ($version -notmatch '^[0-9A-Za-z._-]+$') { throw "Invalid application version: $version" }
$versionHook = Join-Path $env:TEMP ("video_toolkit_version_" + [guid]::NewGuid().ToString('N') + '.py')
Set-Content -LiteralPath $versionHook -Encoding UTF8 -Value @(
  'import os',
  "os.environ['VIDEO_TOOLKIT_VERSION'] = '$version'"
)
Write-Host "Embedding application version: $version"
$mediaBin = if ($env:VIDEO_TOOLKIT_MEDIA_BIN) {
  $env:VIDEO_TOOLKIT_MEDIA_BIN
} else {
  Join-Path $root 'tools\ffmpeg\bin'
}
if (-not (Test-Path (Join-Path $mediaBin 'ffmpeg.exe')) -or -not (Test-Path (Join-Path $mediaBin 'ffprobe.exe'))) {
  throw 'FFmpeg and FFprobe were not found. Set VIDEO_TOOLKIT_MEDIA_BIN or place them in tools\ffmpeg\bin.'
}
python -m PyInstaller --clean --noconfirm --windowed --onedir --contents-directory 'internal' `
  --name 'VideoToolkit' `
  --icon (Join-Path $root 'logo.ico') `
  --add-data ((Join-Path $root 'logo.ico') + ';.') `
  --add-data ((Join-Path $root 'VERSION') + ';.') `
  --add-data ((Join-Path $root 'resources\fonts') + ';resources\fonts') `
  --add-data ((Join-Path $root 'resources\language_packs') + ';resources\language_packs') `
  --add-binary ((Join-Path $mediaBin 'ffmpeg.exe') + ';.') `
  --add-binary ((Join-Path $mediaBin 'ffprobe.exe') + ';.') `
  --collect-data 'faster_whisper' `
  --collect-binaries 'ctranslate2' `
  --collect-data 'onnxruntime' `
  --collect-binaries 'onnxruntime' `
  --hidden-import 'faster_whisper' `
  --hidden-import 'onnxruntime' `
  --hidden-import 'yt_dlp' `
  --hidden-import 'modules.caption_qt_burn' `
  --hidden-import 'modules.dynamic_caption_page' `
  --collect-submodules 'yt_dlp' `
  --collect-data 'yt_dlp' `
  --runtime-hook $versionHook `
  --exclude-module 'torch' `
  --exclude-module 'torchvision' `
  --exclude-module 'torchaudio' `
  --exclude-module 'tensorflow' `
  --exclude-module 'numba' `
  --exclude-module 'llvmlite' `
  --exclude-module 'IPython' `
  --exclude-module 'jupyter' `
  --distpath $dist `
  --workpath (Join-Path $root 'build') `
  --specpath $root `
  (Join-Path $root 'app.py')
