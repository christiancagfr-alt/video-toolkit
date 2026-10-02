# VideoToolkit v1.7.70

安全加固与依赖升级（在 v1.7.69 功能之上）。

## 安全

- 自动更新：禁止 `shell=True` 启动安装包；版本号净化；PE `MZ` 校验；默认仅官方 GitHub 资源；顶栏可选「更新走镜像」。
- yt-dlp：默认校验证书；组件下载镜像默认关闭。
- 截图网络链接：与 ASR 相同平台白名单。
- Gladia / Gemini：跟随 URL 限制为 HTTPS + 官方主机。
- CI：第三方 Action 钉 SHA；dispatch tag 经环境变量传入并校验；最小权限。

## 依赖

- `Pillow>=12.3,<13`、`cryptography>=48,<51`、`yt-dlp>=2026.7.4`
- 移除未使用的 `moviepy`（其 Pillow&lt;12 约束阻塞安全升级）

## 文档

- `docs/SECURITY_AUDIT_v1.7.70.md`
- `docs/FEATURES.md`
