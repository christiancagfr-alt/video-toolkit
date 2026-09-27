## v1.7.65

### Windows
- [VideoToolkit_Setup_v1.7.65.exe](https://github.com/secure-artifacts/video-toolkit/releases/download/v1.7.65/VideoToolkit_Setup_v1.7.65.exe)（安装包）
- [video-toolkit-windows-x64-v1.7.65.zip](https://github.com/secure-artifacts/video-toolkit/releases/download/v1.7.65/video-toolkit-windows-x64-v1.7.65.zip)（绿色版）

### Changes
- **批量/长视频本地识别**：提取完成后强制刷新预览与编辑器，避免仍显示旧词轴（看起来像「要再提取一次才准」）。
- **本地 Whisper ≥90s 分段识别**：按段拼时间戳，覆盖率不足时自动关 VAD 补跑。
- **缓存策略**：不再误用句级同名 `.srt`；优先词级 `.words.srt` / 合格缓存。
- **安装包启动**：禁用 UPX、完整收集 charset_normalizer/chardet；目录固定 `_internal`（修复 python312.dll / CompatibleFamillyRange 启动失败）。
- 含 v1.7.64：本地提速、上下拼接文件夹、Gemini 3.6、口型/裁剪后重提修复。
