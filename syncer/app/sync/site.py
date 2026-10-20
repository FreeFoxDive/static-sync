"""Fetch an explicit list of paths from one site. No crawling."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx

from app.config_loader import Source, SyncSettings
from app.security.path import safe_join
from app.security.ssrf import SsrfError, assert_public_http_url
from app.sync.download import DownloadError, download_to_file


def sync_site(
    client: httpx.Client,
    source: Source,
    output_root: Path,
    settings: SyncSettings,
) -> tuple[str, int]:
    if not source.base_url or not source.paths:
        raise RuntimeError("site source missing base_url or paths")
    total = 0
    for rel in source.paths:
        url = _join_same_origin(source.base_url, rel)
        relative_dest = rel if source.dest == "." else f"{source.dest}/{rel}"
        dest = safe_join(output_root, relative_dest)
        total += download_to_file(
            client,
            url,
            dest,
            max_bytes=settings.max_bytes,
            max_redirects=settings.max_redirects,
            user_agent=settings.user_agent,
        )
    return source.dest, total


def _join_same_origin(base_url: str, relative: str) -> str:
    lowered = relative.lower()
    if (
        relative.startswith(("/", "\\"))
        or "\\" in relative
        or ".." in relative.split("/")
        or "?" in relative
        or "#" in relative
        or "%2e" in lowered
        or "%2f" in lowered
        or "%5c" in lowered
    ):
        raise DownloadError("site path escapes base_url")
    base = urlsplit(base_url)
    if base.scheme not in ("http", "https") or not base.hostname:
        raise DownloadError("site base_url must be http(s)")
    root = base_url if base_url.endswith("/") else base_url + "/"
    joined = urljoin(root, relative)
    target = urlsplit(joined)
    if (target.scheme, (target.hostname or "").lower(), target.port) != (
        base.scheme,
        base.hostname.lower(),
        base.port,
    ):
        raise DownloadError("site path leaves the base origin")
    base_path = base.path if base.path.endswith("/") else base.path.rsplit("/", 1)[0] + "/"
    if not target.path.startswith(base_path):
        raise DownloadError("site path leaves the base path")
    try:
        assert_public_http_url(joined)
    except SsrfError as exc:
        raise DownloadError(str(exc)) from exc
    return joined
