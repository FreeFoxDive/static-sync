from __future__ import annotations

from pathlib import Path

import httpx

from app.config_loader import Source, SyncSettings
from app.security.path import safe_join
from app.sync.download import download_to_file


def sync_url(
    client: httpx.Client,
    source: Source,
    output_root: Path,
    settings: SyncSettings,
) -> tuple[str, int]:
    if not source.url:
        raise RuntimeError("url source missing url")
    dest = safe_join(output_root, source.dest)
    size = download_to_file(
        client,
        source.url,
        dest,
        max_bytes=settings.max_bytes,
        max_redirects=settings.max_redirects,
        user_agent=settings.user_agent,
    )
    return source.dest, size
