## v1.7.69

### Windows
- [VideoToolkit_Setup_v1.7.69.exe](https://github.com/secure-artifacts/video-toolkit/releases/download/v1.7.69/VideoToolkit_Setup_v1.7.69.exe)（安装包）
- [video-toolkit-windows-x64-v1.7.69.zip](https://github.com/secure-artifacts/video-toolkit/releases/download/v1.7.69/video-toolkit-windows-x64-v1.7.69.zip)（绿色版）

### macOS
- [video-toolkit-macos-arm64-v1.7.69.zip](https://github.com/secure-artifacts/video-toolkit/releases/download/v1.7.69/video-toolkit-macos-arm64-v1.7.69.zip)（Apple Silicon）
- [video-toolkit-macos-x64-v1.7.69.zip](https://github.com/secure-artifacts/video-toolkit/releases/download/v1.7.69/video-toolkit-macos-x64-v1.7.69.zip)（Intel）

### Linux
- [video-toolkit-linux-x64-v1.7.69.zip](https://github.com/secure-artifacts/video-toolkit/releases/download/v1.7.69/video-toolkit-linux-x64-v1.7.69.zip)

### Changes
- **合成后字幕时间轴对齐**：提取完成后清时长缓存并用 ffprobe 重建音视频/分段条；避免字幕块与音视频条视觉错位。长音频本地 Whisper 改为重叠分段识别。
- **导出独立选项**：顶栏「烧录字幕 / 加水印」与分组合成解耦。成品直接加入视频字幕队列时可只加水印或只烧字幕导出，缺时间轴时会提示，避免误触发长时间识别像卡住。
