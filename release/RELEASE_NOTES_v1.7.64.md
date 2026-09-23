## v1.7.64

### Windows
- [VideoToolkit_Setup_v1.7.64.exe](https://github.com/secure-artifacts/video-toolkit/releases/download/v1.7.64/VideoToolkit_Setup_v1.7.64.exe)（安装包）
- [video-toolkit-windows-x64-v1.7.64.zip](https://github.com/secure-artifacts/video-toolkit/releases/download/v1.7.64/video-toolkit-windows-x64-v1.7.64.zip)（绿色版）

### macOS
- [video-toolkit-macos-arm64-v1.7.64.zip](https://github.com/secure-artifacts/video-toolkit/releases/download/v1.7.64/video-toolkit-macos-arm64-v1.7.64.zip)（Apple Silicon）
- [video-toolkit-macos-x64-v1.7.64.zip](https://github.com/secure-artifacts/video-toolkit/releases/download/v1.7.64/video-toolkit-macos-x64-v1.7.64.zip)（Intel）

### Linux
- [video-toolkit-linux-x64-v1.7.64.zip](https://github.com/secure-artifacts/video-toolkit/releases/download/v1.7.64/video-toolkit-linux-x64-v1.7.64.zip)

### Changes
- **本地 Whisper 提速**：beam=1 / best_of=1、更积极 VAD、CPU 多线程；新增 `base` 最快选项；减少无谓 double 重跑。
- **长视频导出更稳**：≥3 分钟自动 ASS 烧录，避免 Qt 逐状态 PNG 卡住。
- **混合素材不卡 UI**：文件夹/大量文件加入队列时分批让出事件循环。
- **上下拼接**：支持添加文件夹；可选「先 Reels 合成（带字幕）再拼接」，上方成品字幕落在上半画面。
- **API 模型**：Gemini 默认升到 `gemini-3.6-flash`（2.0 已关停）；Gladia 用 `result_url`；ElevenLabs 回退 `scribe_v2`。
- **口型/裁剪后重提**：修复找不到 FFmpeg；导出禁止错误合并跟读状态；视频减速时字幕时钟同步拉长。
- 含辅助工具：字幕校准检查、富文本关键词、双语对齐、相似素材、FCPXML 导出。
