"""yt-dlp helpers: module import, standalone binary, and download entry points.

Packaged (frozen) builds cannot reliably use system pip. We install the official
standalone binary into the app data bin folder (same pattern as FFmpeg).
"""
from __future__ import annotations

import importlib.metadata
import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable

import requests

from .platform_utils import app_data_dir


def _component_bin() -> Path:
    path = app_data_dir() / "bin"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _hidden_kwargs() -> dict:
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


LogFn = Callable[[str], None]


def ytdlp_asset_name() -> str:
    if sys.platform == "win32":
        return "yt-dlp.exe"
    if sys.platform == "darwin":
        return "yt-dlp_macos"
    return "yt-dlp_linux"


def ytdlp_binary_path() -> Path:
    return _component_bin() / ytdlp_asset_name()


def find_ytdlp_binary() -> str | None:
    """Return path to a usable yt-dlp executable, or None."""
    candidates = [
        ytdlp_binary_path(),
        _component_bin() / "yt-dlp.exe",
        _component_bin() / "yt-dlp-new.exe",
        _component_bin() / "yt-dlp",
        _component_bin() / "yt-dlp-new",
    ]
    for path in candidates:
        try:
            if path.is_file() and path.stat().st_size > 50_000:
                return str(path)
        except OSError:
            continue
    which = shutil.which("yt-dlp") or shutil.which("yt-dlp.exe")
    if which:
        try:
            if Path(which).stat().st_size > 50_000:
                return which
        except OSError:
            return which
    return None


def module_available() -> bool:
    try:
        return importlib.util.find_spec("yt_dlp") is not None
    except Exception:
        return False


def module_version() -> str:
    try:
        return importlib.metadata.version("yt-dlp")
    except Exception:
        return ""


def binary_version(binary: str | None = None) -> str:
    path = binary or find_ytdlp_binary()
    if not path:
        return ""
    try:
        result = subprocess.run(
            [path, "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            **_hidden_kwargs(),
        )
        if result.returncode == 0:
            return (result.stdout or "").strip().splitlines()[0].strip()
    except Exception:
        pass
    return ""


def ytdlp_status() -> tuple[bool, str]:
    """(ok, detail) for component table."""
    if module_available():
        ver = module_version() or "已内置"
        return True, ver
    binary = find_ytdlp_binary()
    if binary:
        ver = binary_version(binary) or "独立程序"
        return True, f"{ver}  ({binary})"
    return False, "缺少：yt-dlp（模块或独立程序）"


def ytdlp_download_urls(*, use_mirrors: bool = False) -> list[str]:
    """Official GitHub asset first; optional third-party mirrors (off by default)."""
    asset = ytdlp_asset_name()
    official = f"https://github.com/yt-dlp/yt-dlp/releases/latest/download/{asset}"
    urls = [official]
    if use_mirrors:
        urls.extend(
            [
                f"https://ghproxy.net/{official}",
                f"https://mirror.ghproxy.com/{official}",
            ]
        )
    return urls


def install_ytdlp_binary(
    log: LogFn | None = None,
    progress: Callable[[int], None] | None = None,
) -> str:
    """Download official standalone yt-dlp into app bin. Returns installed path."""
    import tempfile

    def _log(msg: str) -> None:
        if log:
            log(msg)

    # 已有可用版本时先提示（失败时可继续用旧版）
    existing = find_ytdlp_binary()
    existing_ver = binary_version(existing) if existing else ""
    if not existing_ver and module_available():
        existing_ver = module_version()
        if existing_ver:
            existing = existing or "(Python 模块)"

    target = ytdlp_binary_path()
    _component_bin().mkdir(parents=True, exist_ok=True)
    urls = ytdlp_download_urls()
    last_error: Exception | None = None

    for url in urls:
        # 先下到系统临时目录，避免边下边写 AppData 被杀软/权限拦截
        tmp_fd = None
        tmp_path: Path | None = None
        try:
            _log("正在下载 yt-dlp 独立程序…")
            _log(f"地址：{url}")
            tmp_fd, tmp_name = tempfile.mkstemp(
                prefix="yt_dlp_", suffix=".download", dir=tempfile.gettempdir()
            )
            os.close(tmp_fd)
            tmp_fd = None
            tmp_path = Path(tmp_name)

            with requests.get(url, stream=True, timeout=120) as response:
                response.raise_for_status()
                total = int(response.headers.get("content-length", 0) or 0)
                if total:
                    _log(f"文件大小约 {total / (1024 * 1024):.1f} MB")
                received = 0
                last_pct = -1
                with tmp_path.open("wb") as handle:
                    for chunk in response.iter_content(256 * 1024):
                        if not chunk:
                            continue
                        handle.write(chunk)
                        received += len(chunk)
                        if total and progress:
                            pct = min(95, round(received / total * 95))
                            progress(pct)
                            if pct // 10 > last_pct // 10:
                                last_pct = pct
                                _log(
                                    f"  下载进度 {pct}%（"
                                    f"{received / (1024 * 1024):.1f} / "
                                    f"{total / (1024 * 1024):.1f} MB）"
                                )
            size = tmp_path.stat().st_size
            if size < 50_000:
                raise RuntimeError(f"下载文件过小（{size} 字节），可能不是有效程序")

            # 覆盖安装：旧文件可能被占用 → 改名备份后写入
            installed = _install_binary_file(tmp_path, target, _log)
            if sys.platform != "win32":
                try:
                    installed.chmod(installed.stat().st_mode | 0o111)
                except OSError:
                    pass
            ver = binary_version(str(installed)) or "未知"
            _log(f"yt-dlp 独立程序已安装：版本 {ver}")
            _log(f"路径：{installed}")
            if progress:
                progress(100)
            bin_text = str(_component_bin())
            path_env = os.environ.get("PATH", "")
            if bin_text not in path_env.split(os.pathsep):
                os.environ["PATH"] = bin_text + os.pathsep + path_env
            return str(installed)
        except Exception as exc:
            last_error = exc
            err_text = str(exc)
            if "Permission" in err_text or "权限" in err_text or "13" in err_text:
                _log(
                    "  此地址失败：权限被拒绝（Permission denied）。"
                    "常见原因：杀毒/防火墙拦截下载、目录无写权限、旧 yt-dlp.exe 被占用。"
                )
            else:
                _log(f"  此地址失败：{exc}")
            continue
        finally:
            if tmp_fd is not None:
                try:
                    os.close(tmp_fd)
                except OSError:
                    pass
            if tmp_path is not None:
                try:
                    if tmp_path.exists():
                        tmp_path.unlink()
                except OSError:
                    pass

    hint = ""
    if existing_ver:
        hint = (
            f"\n你本机已有可用 yt-dlp（{existing_ver}），"
            "可先继续使用；若网络解析正常不必强行覆盖更新。"
        )
    raise RuntimeError(
        "下载 yt-dlp 独立程序失败。\n"
        "请依次尝试：\n"
        "1）暂时关闭杀毒/防火墙对 VideoToolkit 与下载的拦截；\n"
        "2）检查是否能打开 GitHub，或配置系统代理后重试；\n"
        "3）确认 %AppData%\\VideoToolkit\\bin 可写，并关闭正在占用 yt-dlp.exe 的进程；\n"
        "4）以当前用户重开软件后再点「一键更新 yt-dlp」。"
        + hint
        + (f"\n最后错误：{last_error}" if last_error else "")
    )


def _install_binary_file(src: Path, target: Path, log: LogFn | None = None) -> Path:
    """把下载好的文件装到 target；若目标被占用则写入备用文件名。"""
    def _log(msg: str) -> None:
        if log:
            log(msg)

    target.parent.mkdir(parents=True, exist_ok=True)
    # 1) 尝试替换
    if target.exists():
        try:
            target.unlink()
        except OSError:
            backup = target.with_name(target.stem + ".old" + target.suffix)
            try:
                if backup.exists():
                    backup.unlink()
                target.rename(backup)
                _log(f"  旧文件已改名备份：{backup.name}")
            except OSError as exc:
                _log(f"  无法删除/改名旧 yt-dlp（可能被占用）：{exc}")
                # 写入新文件名，避免 Permission denied
                alt = target.with_name(target.stem + "-new" + target.suffix)
                try:
                    if alt.exists():
                        alt.unlink()
                except OSError:
                    pass
                shutil.copy2(src, alt)
                if sys.platform != "win32":
                    try:
                        alt.chmod(alt.stat().st_mode | 0o111)
                    except OSError:
                        pass
                _log(f"  已安装为备用文件：{alt.name}（旧文件仍被占用）")
                return alt
    try:
        shutil.copy2(src, target)
    except OSError as exc:
        # 最后手段：备用名
        alt = target.with_name(target.stem + "-new" + target.suffix)
        shutil.copy2(src, alt)
        _log(f"  主文件写入失败（{exc}），已装到：{alt.name}")
        return alt
    return target


def get_youtube_dl_class():
    """Import YoutubeDL or raise ImportError if only binary is available."""
    from yt_dlp import YoutubeDL  # type: ignore
    return YoutubeDL


def _youtube_friendly_opts() -> dict:
    """缓解 YouTube 403 / SABR / player 限制（Shorts、地区网络常见）。"""
    return {
        # android/web 组合在多数环境可拿到可下的 https 格式；避免仅用 web 被 403
        "extractor_args": {
            "youtube": {
                "player_client": ["android", "ios", "web", "mweb"],
            }
        },
        # 部分网络下证书/重试更稳
        "retries": 5,
        "fragment_retries": 5,
        "file_access_retries": 3,
    }


def _friendly_download_error(exc: BaseException) -> str:
    text = str(exc or "")
    low = text.lower()
    tips = []
    if "403" in text or "forbidden" in low:
        tips.append("YouTube 拒绝了直链（403），请更新 yt-dlp 或稍后重试/换网络。")
    if "unable to handle request" in low or "unexpected error" in low:
        tips.append(
            "解析请求失败（多为 YouTube 接口变更或网络拦截）。"
            "请到「设置与组件」一键更新 yt-dlp，或开系统代理后重试。"
        )
    if "429" in text or "too many" in low:
        tips.append("请求过于频繁，请稍等几分钟再试。")
    if "private" in low or "login" in low or "sign in" in low:
        tips.append("视频可能是私密/需登录，软件无法直接抓取。")
    if not tips:
        tips.append("请检查链接是否可在浏览器打开，并尝试更新 yt-dlp 或使用代理。")
    return "网络视频下载失败：" + " ".join(tips) + f"\n技术详情：{text[:500]}"


def download_media(
    url: str,
    outtmpl: str,
    *,
    format_spec: str = "mp4/best",
    quiet: bool = True,
    no_warnings: bool = True,
    proxy: str | None = None,
    extra_opts: dict | None = None,
    progress_hooks: list | None = None,
    log: LogFn | None = None,
) -> tuple[str, dict | None]:
    """Download with Python module if available, else standalone binary.

    Returns (output_path, info_dict_or_None).
    """
    def _log(msg: str) -> None:
        if log:
            log(msg)

    # 自动带上系统代理（若调用方未指定）
    if proxy is None:
        try:
            import urllib.request
            proxy = urllib.request.getproxies().get("https") or urllib.request.getproxies().get("http")
        except Exception:
            proxy = None

    last_exc: BaseException | None = None
    if module_available():
        from yt_dlp import YoutubeDL  # type: ignore

        options: dict = {
            "outtmpl": outtmpl,
            "format": format_spec,
            "quiet": quiet,
            "no_warnings": no_warnings,
            "nocheckcertificate": False,
            "noplaylist": True,
            "overwrites": True,
        }
        options.update(_youtube_friendly_opts())
        if proxy:
            options["proxy"] = proxy
            _log(f"使用代理：{proxy}")
        if progress_hooks:
            options["progress_hooks"] = progress_hooks
        if extra_opts:
            # 合并 extractor_args，避免 extra 整表覆盖掉 youtube 客户端
            extra = dict(extra_opts)
            if "extractor_args" in extra and "extractor_args" in options:
                base_ea = dict(options["extractor_args"])
                for site, args in (extra.pop("extractor_args") or {}).items():
                    merged = dict(base_ea.get(site) or {})
                    merged.update(args or {})
                    base_ea[site] = merged
                options["extractor_args"] = base_ea
            options.update(extra)
        try:
            with YoutubeDL(options) as ydl:
                info = ydl.extract_info(url, download=True)
                path = ydl.prepare_filename(info)
                return path, info
        except Exception as exc:
            last_exc = exc
            _log(f"模块下载失败，尝试独立程序：{exc}")

    binary = find_ytdlp_binary()
    if not binary:
        if last_exc is not None:
            raise RuntimeError(_friendly_download_error(last_exc)) from last_exc
        raise RuntimeError(
            "缺少网络视频解析组件 yt-dlp。请到「设置与组件」点击「一键更新 yt-dlp」。"
        )

    _log(f"使用独立 yt-dlp 程序：{binary}")
    out_dir = Path(outtmpl).parent if "%(" in outtmpl else Path(outtmpl).parent
    out_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        binary,
        "--no-playlist",
        "--force-overwrites",
        "-f", format_spec,
        "-o", outtmpl,
        "--newline",
        "--retries", "5",
        # 与模块侧一致的客户端回退
        "--extractor-args",
        "youtube:player_client=android,ios,web,mweb",
    ]
    if quiet:
        cmd.append("--quiet")
    if no_warnings:
        cmd.append("--no-warnings")
    if proxy:
        cmd.extend(["--proxy", proxy])
    cmd.append(url)

    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=3600,
        **_hidden_kwargs(),
    )
    if result.returncode != 0:
        tail = (result.stdout or "")[-2000:]
        detail = tail or (str(last_exc) if last_exc else f"退出码 {result.returncode}")
        raise RuntimeError(_friendly_download_error(RuntimeError(detail)))

    import glob as _glob
    import re as _re

    pattern = outtmpl
    if "%(" in pattern:
        pattern = _re.sub(r"%\([^)]+\)s", "*", pattern)
        pattern = _re.sub(r"%\([^)]+\)\d*d", "*", pattern)
    matches = sorted(
        (Path(p) for p in _glob.glob(pattern) if Path(p).is_file()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not matches:
        files = [p for p in out_dir.iterdir() if p.is_file()]
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        if not files:
            raise RuntimeError("yt-dlp 已退出但未找到输出文件")
        return str(files[0]), None
    return str(matches[0]), None
