# VideoToolkit v1.7.75

- **字幕识别**：分组合成成品按 sidecar 每段小视频（约 8–10s）识别后再拼成片时钟；其它长片改为约 30s 短窗、4s 重叠
- **文案校对**：粘贴自动清洗 BOM / UTF-8 乱码；界面字体改用 Segoe UI / Arial，避免希腊语等缺字形串格
- **本地 Whisper**：`no_speech_threshold` 参数名修正，兼容当前 faster-whisper

使用提示：识别语言/书写语言请选对（如 Ελληνικά）；长片漏句或口型不准时对合成成品点「重新提取」。个别词听错仍可用「文案校对」只改文字、保留时间戳。

详见 `docs/FEATURES.md`
