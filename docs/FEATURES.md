# VideoToolkit（视频工具合集）功能说明

桌面端一站式视频工作台（PySide6）。当前版本见仓库根目录 `VERSION`。

## 模块一览

| 模块 | 作用 |
|------|------|
| 批量截图 | 本地视频或白名单平台链接抽帧导出 |
| 智能剪辑 | 按场景/规则切分素材 |
| Reels / 视频字幕 | 分组合成、字幕识别与烧录、多轨时间轴、样式预设、文案校对 |
| 批量重命名 | 按规则重命名媒体；成功后清空任务列表 |
| 元数据清理 | 去除或改写容器元数据 |
| 水印 | 公司/素材水印库；合成与导出可独立控制 |
| TTS / 图文配音 | Edge TTS、Gemini、ElevenLabs（API Key 或网页会话） |
| 上下拼接 | 双画面竖版拼接，可选先 Reels 再拼 |
| 设置与组件 | FFmpeg / yt-dlp 等本地组件安装与检测 |
| Google 同步 | Drive 上传与 Sheets 填表（OAuth / 服务账号） |

## Reels 与字幕要点

- **识别**：Groq / Gemini / Gladia / ElevenLabs / 本地 faster-whisper；长音频重叠分段。
- **时间轴**：Canva 风格多轨；提取后按成品真实时长重建，避免字幕块与音视频条错位。
- **导出独立选项**：顶栏「烧录字幕」「加水印」控制批量导出，与「分组合成」勾选解耦；成品可只加水印或只烧字幕。
- **辅助工具**：字幕审计、富文本/关键词样式、双语对齐、相似素材分组、FCPXML 导出。

## 安全相关行为（用户可见）

- API 密钥存本机加密库（Windows：DPAPI + Fernet），不写进 `config.json` 明文。
- 「检查更新」从个人仓库 `christiancagfr-alt/video-toolkit` 发布页拉取；默认不走第三方镜像，需要时再勾选「更新走镜像」。
- 网络截图 / 识别链接仅允许 YouTube、Facebook、Instagram、TikTok。

## 构建与发布

- Windows：`build.ps1` + Inno Setup `VideoToolkit.iss`
- macOS / Linux：`build_macos.sh` / `build_linux.sh` 或 fork Actions
- 详细安全结论见 `docs/SECURITY_AUDIT_v1.7.70.md`
