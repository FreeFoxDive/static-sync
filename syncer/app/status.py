"""In-memory sync status persisted outside the public directory."""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

from app.sync.atomic import atomic_write

log = logging.getLogger(__name__)


class StatusStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._data = self._load(path)

    def init_group(self, name: str, *, enabled: bool, output_dir: str, cron: str, timezone_name: str) -> None:
        with self._lock:
            existing = self._data["groups"].get(name)
            if not isinstance(existing, dict):
                existing = {}
            sources = existing.get("sources")
            if not isinstance(sources, dict):
                sources = {}
            self._data["groups"][name] = {
                "enabled": enabled,
                "output_dir": output_dir,
                "cron": cron,
                "timezone": timezone_name,
                "last_started_at": existing.get("last_started_at"),
                "last_finished_at": existing.get("last_finished_at"),
                "ok": existing.get("ok"),
                "sources": sources,
            }
            self._persist()

    def retain_groups(self, names: set[str]) -> None:
        with self._lock:
            before = set(self._data["groups"])
            self._data["groups"] = {name: group for name, group in self._data["groups"].items() if name in names}
            removed = before - set(self._data["groups"])
            if removed:
                log.info("dropped status for removed groups: %s", ", ".join(sorted(removed)))
            self._persist()

    def mark_started(self, name: str, started_at: str) -> None:
        with self._lock:
            group = self._data["groups"].setdefault(name, {"sources": {}})
            group["last_started_at"] = started_at
            group["ok"] = None
            self._persist()

    def mark_source(
        self,
        group: str,
        source_id: str,
        *,
        ok: bool,
        dest: str,
        bytes_written: int,
        error: str | None,
        skipped: bool,
        finished_at: str,
    ) -> None:
        with self._lock:
            entry = self._data["groups"].setdefault(group, {"sources": {}})
            sources = entry.setdefault("sources", {})
            sources[source_id] = {
                "ok": ok,
                "skipped": skipped,
                "dest": dest,
                "bytes": bytes_written,
                "error": error,
                "finished_at": finished_at,
            }
            self._persist()

    def mark_finished(self, name: str, finished_at: str, ok: bool) -> None:
        with self._lock:
            group = self._data["groups"].setdefault(name, {"sources": {}})
            group["last_finished_at"] = finished_at
            group["ok"] = ok
            self._persist()

    def snapshot(self) -> dict:
        with self._lock:
            return json.loads(json.dumps(self._data))

    def _persist(self) -> None:
        payload = json.dumps(self._data, ensure_ascii=False, indent=2).encode("utf-8")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(self.path, [payload], max_bytes=max(len(payload), 1))

    @staticmethod
    def _load(path: Path) -> dict:
        if not path.is_file():
            return {"groups": {}}
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("ignoring unreadable status file %s: %s", path, exc)
            return {"groups": {}}
        if not isinstance(loaded, dict):
            return {"groups": {}}
        groups = loaded.get("groups")
        if not isinstance(groups, dict):
            return {"groups": {}}
        return {"groups": groups}
