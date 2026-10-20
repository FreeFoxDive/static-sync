"""Run one job group and record per-source results."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

import httpx

from app.config_loader import JobGroup, SyncSettings
from app.security.path import directory_under
from app.status import StatusStore
from app.sync.download import DownloadError
from app.sync.github import sync_github
from app.sync.site import sync_site
from app.sync.url import sync_url

log = logging.getLogger(__name__)


def run_group(group: JobGroup, settings: SyncSettings, status: StatusStore) -> None:
    output_root = directory_under(settings.data_dir, group.output_dir)
    started = _now()
    status.mark_started(group.name, started)
    timeout = httpx.Timeout(settings.timeout_seconds)
    limits = httpx.Limits(max_connections=8, max_keepalive_connections=4)
    ok = True
    with httpx.Client(timeout=timeout, limits=limits, follow_redirects=False, trust_env=False) as client:
        for source in group.sources:
            public_dest = group.output_dir if source.dest == "." else f"{group.output_dir}/{source.dest}"
            if not source.enabled:
                status.mark_source(
                    group.name,
                    source.id,
                    ok=True,
                    dest=public_dest,
                    bytes_written=0,
                    error=None,
                    skipped=True,
                    finished_at=_now(),
                )
                continue
            try:
                dest, size = _sync_one(client, source, output_root, settings)
                public = group.output_dir if dest in ("", ".") else f"{group.output_dir}/{dest}"
                status.mark_source(
                    group.name,
                    source.id,
                    ok=True,
                    dest=public,
                    bytes_written=size,
                    error=None,
                    skipped=False,
                    finished_at=_now(),
                )
                log.info("synced %s/%s -> %s (%s bytes)", group.name, source.id, public, size)
            except (DownloadError, OSError, ValueError) as exc:
                ok = False
                message = str(exc).splitlines()[0][:300]
                status.mark_source(
                    group.name,
                    source.id,
                    ok=False,
                    dest=public_dest,
                    bytes_written=0,
                    error=message,
                    skipped=False,
                    finished_at=_now(),
                )
                log.warning("sync failed %s/%s: %s", group.name, source.id, message)
    status.mark_finished(group.name, _now(), ok)


def _sync_one(client: httpx.Client, source, output_root: Path, settings: SyncSettings):
    if source.type == "url":
        return sync_url(client, source, output_root, settings)
    if source.type == "github":
        return sync_github(client, source, output_root, settings)
    if source.type == "site":
        return sync_site(client, source, output_root, settings)
    raise DownloadError(f"unsupported type {source.type}")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
