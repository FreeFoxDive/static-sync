"""Mirror selected files from a GitHub repository path."""

from __future__ import annotations

import fnmatch
import os
from pathlib import Path
from urllib.parse import quote

import httpx

from app.config_loader import Source, SyncSettings
from app.security.path import safe_join
from app.security.ssrf import assert_public_http_url
from app.sync.download import DownloadError, download_to_file

MAX_FILES = 500
MAX_DEPTH = 8
API = "https://api.github.com"


def sync_github(
    client: httpx.Client,
    source: Source,
    output_root: Path,
    settings: SyncSettings,
) -> tuple[str, int]:
    if not source.repo:
        raise RuntimeError("github source missing repo")
    assert_public_http_url(f"{API}/repos/{source.repo}")
    files = _list_files(client, source, settings)
    if not files:
        raise DownloadError("github path matched no files")
    total = 0
    prefix = "" if source.dest == "." else source.dest
    for relative, download_url, size in files:
        if size and size > settings.max_bytes:
            raise DownloadError(f"{relative} exceeds {settings.max_bytes} bytes")
        relative_dest = relative if not prefix else f"{prefix}/{relative}"
        dest = safe_join(output_root, relative_dest)
        total += download_to_file(
            client,
            download_url,
            dest,
            max_bytes=settings.max_bytes,
            max_redirects=settings.max_redirects,
            user_agent=settings.user_agent,
        )
    return source.dest, total


def _list_files(
    client: httpx.Client,
    source: Source,
    settings: SyncSettings,
) -> list[tuple[str, str, int]]:
    root = source.path.strip("/")
    found: list[tuple[str, str, int]] = []
    _walk(client, source, settings, root, "", found, 0)
    return found


def _walk(
    client: httpx.Client,
    source: Source,
    settings: SyncSettings,
    api_path: str,
    relative_prefix: str,
    found: list[tuple[str, str, int]],
    depth: int,
) -> None:
    if depth > MAX_DEPTH:
        raise DownloadError("github tree is too deep")
    if len(found) > MAX_FILES:
        raise DownloadError(f"github listing exceeds {MAX_FILES} files")
    payload = _get_json(client, source, settings, api_path)
    items = payload if isinstance(payload, list) else [payload]
    for item in items:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        name = str(item.get("name") or "")
        if not name or name in (".", "..") or "/" in name or "\\" in name:
            raise DownloadError("github returned an unsafe file name")
        relative = f"{relative_prefix}/{name}" if relative_prefix else name
        if kind == "dir":
            _walk(client, source, settings, str(item.get("path") or ""), relative, found, depth + 1)
            continue
        if kind != "file":
            continue
        if not _matches(relative, source.include):
            continue
        download_url = item.get("download_url")
        if not isinstance(download_url, str) or not download_url:
            raise DownloadError(f"github file has no download url: {relative}")
        size = int(item.get("size") or 0)
        found.append((relative, download_url, size))
        if len(found) > MAX_FILES:
            raise DownloadError(f"github listing exceeds {MAX_FILES} files")


def _matches(relative: str, patterns: list[str]) -> bool:
    if not patterns:
        return True
    name = relative.rsplit("/", 1)[-1]
    return any(fnmatch.fnmatch(relative, pattern) or fnmatch.fnmatch(name, pattern) for pattern in patterns)


def _get_json(client: httpx.Client, source: Source, settings: SyncSettings, path: str) -> object:
    quoted = "/".join(quote(part) for part in path.split("/") if part)
    url = f"{API}/repos/{source.repo}/contents/{quoted}" if quoted else f"{API}/repos/{source.repo}/contents"
    assert_public_http_url(url)
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": settings.user_agent,
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        response = client.get(url, headers=headers, params={"ref": source.ref}, follow_redirects=False)
    except httpx.HTTPError as exc:
        raise DownloadError(f"github API failed: {exc.__class__.__name__}") from exc
    if response.status_code != 200:
        raise DownloadError(f"github API HTTP {response.status_code}")
    return response.json()
