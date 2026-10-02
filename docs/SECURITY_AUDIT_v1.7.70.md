# VideoToolkit 安全审核报告（v1.7.70）

- **审核日期**：2026-10-01
- **审核范围**：`E:\new-chat\work\video_toolkit` 活动源码（`app.py`、`modules/*.py`、`.github/workflows/*`、`requirements.txt`、`.gitignore`）
- **排除**：`backup_*`、`dist_*`、`*.bak`、本地媒体与安装包产物
- **方法**：结构通读 + 依赖清单 + `pip-audit` + OSV/NVD/GitHub Advisory 交叉核对 + 代码模式扫描（注入 / TLS / 更新供应链 / 密钥 / CI）

## 1. 项目概况

| 项 | 说明 |
|----|------|
| 语言 / UI | Python 3.12 · PySide6 桌面应用 |
| 包管理 | `requirements.txt`（无 lock 文件；间接依赖以本机 / CI 解析结果为准） |
| 直接依赖 | 约 18 项（见 `requirements.txt`） |
| 密钥存储 | AppData `secrets.vault`（Fernet）；Windows 上主密钥 DPAPI 包装 |
| 发布渠道 | GitHub Releases（org + fork CI） |

## 2. 问题汇总表

| 编号 | 类别 | 严重级别 | 位置 | 状态 |
|------|------|----------|------|------|
| S-01 | 代码 | High | `app.py` 更新安装 `shell=True` + 未净化版本号写入路径 | **已修复** |
| S-02 | 代码 | High | 更新下载默认走第三方 GitHub 镜像 | **已修复**（默认关，可选手动开） |
| S-03 | 代码 | High | `ytdlp_utils` `nocheckcertificate` / `--no-check-certificates` | **已修复** |
| S-04 | 依赖 | High | `yt-dlp` &lt; 2026.7.4 系列 CVE（cookie 泄漏 / aria2c / shortcut / write-link） | **已修复**（`>=2026.7.4`） |
| S-05 | 依赖 | High | `Pillow` 11.x 多项图像炸弹 / OOB（pip-audit PYSEC-2026-*） | **已修复**（`>=12.3,<13`；移除阻塞的 `moviepy`） |
| S-06 | 依赖 | High | `cryptography` 46.x 需升至 48+/49+/50+ | **已修复**（`>=48,<51`） |
| S-07 | CI | High | Actions 仅钉 tag、未钉 commit SHA | **已修复** |
| S-08 | CI | Medium | `workflow_dispatch` tag 直接拼进 `run:`（脚本注入） | **已修复** |
| S-09 | CI | Medium | 工作流顶层 `contents: write` 过宽 | **已修复** |
| S-10 | 代码 | Medium | 截图页任意 URL 进 yt-dlp | **已修复**（与 ASR 同域名白名单） |
| S-11 | 代码 | Medium | Gladia / Gemini 跟随服务端绝对 URL | **已修复**（HTTPS + host 白名单） |
| S-12 | 密钥 | Medium | `.gitignore` 缺 `.env` / vault / `temp_proj_*` / `dist_final` 等 | **已修复** |
| S-13 | 代码 | Medium | FFmpeg zip 成员路径未校验 | **已修复** |
| S-14 | 依赖 | Medium | `deep-translator` PYSEC-2022-252（历史 PyPI 账号劫持） | **建议关注**（钉 `>=1.11.4,<2`；无后续补丁版本） |
| S-15 | 密钥 | Medium | 非 Windows `vtplain:` 明文 Fernet 主密钥 | **待开发者决定**（Keychain/libsecret） |
| S-16 | 密钥 | Medium | Google OAuth token 明文落盘 | **待开发者决定**（迁入 vault） |
| S-17 | CI | Medium | CI 下载 FFmpeg `latest` 无校验和 | **待开发者决定**（钉版本 + SHA256） |
| S-18 | 代码 | Low | 重命名 `exists`→写 TOCTOU | **建议关注** |

不确定项均标 **待确认**，未编造额外 CVE。

## 3. 已修复问题与改动

1. **更新安装**：去掉 `shell=True`；版本号白名单净化；`.exe` 校验 `MZ` PE 头；Windows 用 `os.startfile`。
2. **更新镜像**：默认仅官方 `github.com` / `objects.githubusercontent.com`；顶栏「更新走镜像」写入 `config.updater.use_github_mirrors`。
3. **yt-dlp TLS**：恢复证书校验；组件下载镜像同样默认关闭。
4. **依赖**：Pillow / cryptography / yt-dlp 升级；**删除未使用的 moviepy**（其 `Pillow<12` 约束阻塞安全升级）。
5. **URL 策略**：新增 `modules/url_policy.py`；截图页与 ASR 共用域名白名单；Gladia/Gemini 回调 host 校验。
6. **CI**：Actions 钉 SHA；tag 经 env 传入并校验；最小权限。
7. **仓库卫生**：扩展 `.gitignore`；新增 `.env.example`（仅变量名）。

## 4. 需要开发者决定

| 项 | 可选方案 | 建议 |
|----|----------|------|
| macOS/Linux 主密钥 `vtplain:` | A) Keychain/libsecret B) 维持并文档警示 | **A** |
| Google OAuth token 加密 | A) 写入 Fernet vault B) 仅 chmod 600 | **A** |
| CI FFmpeg 完整性 | A) 钉版本 URL + SHA256 B) 维持多镜像 | **A** |
| `deep-translator` | A) 保留并监控 PyPI B) 换官方翻译 API | **A**（功能仍用到时） |

**提醒**：若历史提交或聊天中曾出现真实 API Key / Cookie，仅删代码不够，须到对应平台**作废并轮换**。

## 5. 剩余风险与后续建议

- 桌面应用信任模型：用户粘贴的 ElevenLabs Cookie/JWT 等价于会话接管；UI 粘贴框仍为明文，需物理与本地访问控制。
- 无 `requirements.lock` / hash pinning：建议后续引入 `pip-tools` 或 `uv lock` + CI `--require-hashes`。
- 自动更新仍依赖 GitHub API 资产列表；长期可增加 release asset digest 校验。
- 定期（发版前）跑 `pip-audit` 与本报告对照。

## 6. 验证

- 语法 / `url_policy` 单测逻辑：通过
- 本地已安装：`cryptography 50.x`、`Pillow 12.3`、`yt-dlp 2026.08.19`
- `pip install -r requirements.txt`：在移除 moviepy 后应可解析（以本机/CI 实测为准）
