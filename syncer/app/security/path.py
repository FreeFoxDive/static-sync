"""Keep synced files inside the configured output directory."""

from __future__ import annotations

from pathlib import Path


class PathEscapeError(ValueError):
    pass


def directory_under(root: Path, relative: str) -> Path:
    """Return a directory that stays inside root. '.' means root itself."""
    if relative in ("", "."):
        root.mkdir(parents=True, exist_ok=True)
        return root.resolve()
    directory = safe_join(root, f"{relative}/.keep").parent
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def safe_join(root: Path, relative: str) -> Path:
    rel = relative.replace("\\", "/").strip()
    if rel in ("", "."):
        raise PathEscapeError("relative path is empty")
    if rel.startswith("/") or Path(relative).is_absolute():
        raise PathEscapeError("absolute path is not allowed")
    parts = [part for part in rel.split("/") if part not in ("", ".")]
    if not parts or any(part == ".." or "\x00" in part or part.endswith(":") for part in parts):
        raise PathEscapeError("path escapes output directory")
    base = root.resolve()
    candidate = base.joinpath(*parts)
    resolved = candidate.resolve()
    if resolved != base and base not in resolved.parents:
        raise PathEscapeError("path escapes output directory")
    return candidate
