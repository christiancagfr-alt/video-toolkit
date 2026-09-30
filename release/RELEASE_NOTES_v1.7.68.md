## v1.7.68

### Windows
- [VideoToolkit_Setup_v1.7.68.exe](https://github.com/secure-artifacts/video-toolkit/releases/download/v1.7.68/VideoToolkit_Setup_v1.7.68.exe)（安装包）
- [video-toolkit-windows-x64-v1.7.68.zip](https://github.com/secure-artifacts/video-toolkit/releases/download/v1.7.68/video-toolkit-windows-x64-v1.7.68.zip)（绿色版）

### macOS
- [video-toolkit-macos-arm64-v1.7.68.zip](https://github.com/secure-artifacts/video-toolkit/releases/download/v1.7.68/video-toolkit-macos-arm64-v1.7.68.zip)（Apple Silicon）
- [video-toolkit-macos-x64-v1.7.68.zip](https://github.com/secure-artifacts/video-toolkit/releases/download/v1.7.68/video-toolkit-macos-x64-v1.7.68.zip)（Intel）

### Linux
- [video-toolkit-linux-x64-v1.7.68.zip](https://github.com/secure-artifacts/video-toolkit/releases/download/v1.7.68/video-toolkit-linux-x64-v1.7.68.zip)

### Changes
- **本地 Whisper 安装包修复**：PyInstaller 改为 `--collect-all ctranslate2`，并在打包后确保 `ctranslate2/__init__.py` 与原生库同目录，避免空目录遮蔽导致 `module 'ctranslate2' has no attribute 'StorageView'`，批量提取字幕可正常使用本地模型。
