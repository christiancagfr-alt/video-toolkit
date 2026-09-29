## v1.7.67

### Windows
- [VideoToolkit_Setup_v1.7.67.exe](https://github.com/secure-artifacts/video-toolkit/releases/download/v1.7.67/VideoToolkit_Setup_v1.7.67.exe)（安装包）
- [video-toolkit-windows-x64-v1.7.67.zip](https://github.com/secure-artifacts/video-toolkit/releases/download/v1.7.67/video-toolkit-windows-x64-v1.7.67.zip)（绿色版）

### macOS
- [video-toolkit-macos-arm64-v1.7.67.zip](https://github.com/secure-artifacts/video-toolkit/releases/download/v1.7.67/video-toolkit-macos-arm64-v1.7.67.zip)（Apple Silicon）
- [video-toolkit-macos-x64-v1.7.67.zip](https://github.com/secure-artifacts/video-toolkit/releases/download/v1.7.67/video-toolkit-macos-x64-v1.7.67.zip)（Intel）

### Linux
- [video-toolkit-linux-x64-v1.7.67.zip](https://github.com/secure-artifacts/video-toolkit/releases/download/v1.7.67/video-toolkit-linux-x64-v1.7.67.zip)

### Changes
- **分组合成后字幕对齐**：快速声音边界合成成品的多段条按成品时钟直接提取，不再误判为切片去烘焙重提；合成入库时清空时长缓存，避免串上一段短片时长。
- **时间轴标尺**：音画轨锁定素材时长；错位长字幕只拉长可滚动区域，不再把视频/原声条撑飞。
- **字幕时长告警**：字幕明显长于视频时写日志提示。
- **批量重命名**：执行成功后自动清空源文件夹、标题列表与预览任务表。
