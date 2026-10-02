"""Shared URL allowlists for downloads and API follow-ups."""
from __future__ import annotations

from urllib.parse import urlparse

SUPPORTED_VIDEO_DOMAINS = (
    "youtube.com",
    "youtu.be",
    "facebook.com",
    "fb.watch",
    "instagram.com",
    "tiktok.com",
)

GITHUB_ASSET_HOSTS = frozenset(
    {
        "github.com",
        "objects.githubusercontent.com",
        "release-assets.githubusercontent.com",
    }
)

GITHUB_MIRROR_PREFIXES = (
    "https://ghproxy.net/",
    "https://gh.llkk.cc/",
    "https://mirror.ghproxy.com/",
)


def is_supported_video_url(value: str) -> bool:
    try:
        parsed = urlparse(str(value or "").strip())
        host = (parsed.hostname or "").lower()
        return parsed.scheme in ("http", "https") and any(
            host == domain or host.endswith("." + domain) for domain in SUPPORTED_VIDEO_DOMAINS
        )
    except Exception:
        return False


def allow_https_host(url: str, allowed_hosts: set[str] | frozenset[str]) -> str:
    """Reject non-HTTPS or unexpected hosts before following server-supplied URLs."""
    value = str(url or "").strip()
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not host:
        raise ValueError(f"拒绝非 HTTPS 回调地址：{value[:120]!r}")
    ok = any(host == h or host.endswith("." + h) for h in allowed_hosts)
    if not ok:
        raise ValueError(f"拒绝非预期主机：{host}")
    return value


def is_allowed_github_asset_url(url: str) -> bool:
    try:
        parsed = urlparse(str(url or "").strip())
        host = (parsed.hostname or "").lower()
        return parsed.scheme == "https" and host in GITHUB_ASSET_HOSTS
    except Exception:
        return False
