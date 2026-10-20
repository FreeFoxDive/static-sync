"""HTTP download with redirect and size limits."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from urllib.parse import urljoin

import httpx

from app.security.ssrf import ResolvedUrl, SsrfError, resolve_public_http_url
from app.sync.atomic import atomic_write

REDIRECTS = {301, 302, 303, 307, 308}


class DownloadError(RuntimeError):
    pass


def download_to_file(
    client: httpx.Client,
    url: str,
    dest: Path,
    *,
    max_bytes: int,
    max_redirects: int,
    user_agent: str,
    resolve_url: Callable[[str], ResolvedUrl] = resolve_public_http_url,
) -> int:
    current = url
    headers = {"User-Agent": user_agent, "Accept": "*/*"}
    for _ in range(max_redirects + 1):
        try:
            resolved = resolve_url(current)
        except SsrfError as exc:
            raise DownloadError(str(exc)) from exc
        req_headers = {**headers, "Host": resolved.host_header}
        extensions = {}
        if resolved.sni_hostname:
            extensions["sni_hostname"] = resolved.sni_hostname
        try:
            with client.stream(
                "GET",
                resolved.pinned_url,
                headers=req_headers,
                follow_redirects=False,
                extensions=extensions,
            ) as response:
                if response.status_code in REDIRECTS:
                    location = response.headers.get("location")
                    if not location:
                        raise DownloadError("redirect without location")
                    # Resolve relative redirects against the original logical URL, not the IP URL.
                    current = urljoin(current, location)
                    continue
                if response.status_code != 200:
                    raise DownloadError(f"HTTP {response.status_code}")
                length = response.headers.get("content-length")
                if length and length.isdigit() and int(length) > max_bytes:
                    raise DownloadError(f"object exceeds {max_bytes} bytes")
                return atomic_write(dest, response.iter_bytes(chunk_size=65536), max_bytes)
        except DownloadError:
            raise
        except httpx.HTTPError as exc:
            raise DownloadError(f"download failed: {exc.__class__.__name__}") from exc
    raise DownloadError("too many redirects")
