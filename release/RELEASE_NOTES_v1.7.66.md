## v1.7.66

### Windows
- [VideoToolkit_Setup_v1.7.66.exe](https://github.com/secure-artifacts/video-toolkit/releases/download/v1.7.66/VideoToolkit_Setup_v1.7.66.exe)（安装包）
- [video-toolkit-windows-x64-v1.7.66.zip](https://github.com/secure-artifacts/video-toolkit/releases/download/v1.7.66/video-toolkit-windows-x64-v1.7.66.zip)（绿色版）

### macOS
- [video-toolkit-macos-arm64-v1.7.66.zip](https://github.com/secure-artifacts/video-toolkit/releases/download/v1.7.66/video-toolkit-macos-arm64-v1.7.66.zip)（Apple Silicon）
- [video-toolkit-macos-x64-v1.7.66.zip](https://github.com/secure-artifacts/video-toolkit/releases/download/v1.7.66/video-toolkit-macos-x64-v1.7.66.zip)（Intel）

### Linux
- [video-toolkit-linux-x64-v1.7.66.zip](https://github.com/secure-artifacts/video-toolkit/releases/download/v1.7.66/video-toolkit-linux-x64-v1.7.66.zip)

### Changes
- **字幕时长对齐**：本地 Whisper 长音频覆盖率门槛约 90%；分段按真实时长累加偏移；不足时关 VAD 补跑后再强制分段；提取后对比视频时长并警告。
- **时间轴标尺**：短字幕轴不再缩短时间线，标尺锁定视频时长。
- **识别设置记忆**：字幕识别服务 / 本地模型 / 识别语言写入偏好，重启后恢复。
- **水印只烧一次**：仅「合成时启用水印」勾选时在合成阶段烧录；批量导出不再强制加水印库内容。
