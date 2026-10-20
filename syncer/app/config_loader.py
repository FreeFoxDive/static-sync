"""Load multi-file YAML config into job groups."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

SOURCE_KEYS = {
    "id",
    "enabled",
    "type",
    "dest",
    "url",
    "repo",
    "ref",
    "path",
    "include",
    "base_url",
    "paths",
}
GROUP_KEYS = {"enabled", "output_dir", "schedule", "sources"}
SCHEDULE_KEYS = {"cron", "timezone", "run_on_startup"}


class ConfigError(ValueError):
    pass


@dataclass
class Schedule:
    cron: str
    timezone: str
    run_on_startup: bool


@dataclass
class ServerSettings:
    bind: str
    port: int


@dataclass
class SyncSettings:
    data_dir: Path
    state_dir: Path
    timeout_seconds: float
    max_bytes: int
    max_redirects: int
    user_agent: str


@dataclass
class Source:
    id: str
    enabled: bool
    type: str
    dest: str
    url: str | None = None
    repo: str | None = None
    ref: str = "main"
    path: str = ""
    include: list[str] = field(default_factory=list)
    base_url: str | None = None
    paths: list[str] = field(default_factory=list)


@dataclass
class JobGroup:
    name: str
    enabled: bool
    output_dir: str
    schedule: Schedule
    sources: list[Source]


@dataclass
class AppConfig:
    server: ServerSettings
    sync: SyncSettings
    groups: list[JobGroup]


def load_config(config_dir: Path) -> AppConfig:
    if not config_dir.is_dir():
        raise ConfigError(f"config dir not found: {config_dir}")

    defaults = _read_yaml(config_dir / "defaults.yaml") if (config_dir / "defaults.yaml").is_file() else {}
    if not isinstance(defaults, dict):
        raise ConfigError("defaults.yaml must be a mapping")

    schedule = _parse_schedule(defaults.get("schedule") or {}, where="defaults.yaml")
    server = _parse_server(defaults.get("server") or {})
    sync = _parse_sync(defaults.get("sync") or {})
    sync = _apply_env_overrides(sync, server)

    groups: list[JobGroup] = []
    paths = sorted(p for p in config_dir.glob("*.yaml") if p.name != "defaults.yaml")
    paths += sorted(p for p in config_dir.glob("*.yml") if p.stem != "defaults")
    if not paths:
        raise ConfigError(f"no job files in {config_dir}")

    seen: set[str] = set()
    for path in paths:
        if path.stem in seen:
            raise ConfigError(f"duplicate job name: {path.stem}")
        seen.add(path.stem)
        raw = _read_yaml(path)
        if not isinstance(raw, dict):
            raise ConfigError(f"{path.name} must be a mapping")
        groups.append(_parse_group(path.stem, raw, schedule))

    _assert_state_not_public(sync)
    return AppConfig(server=server, sync=sync, groups=groups)


def _assert_state_not_public(sync: SyncSettings) -> None:
    data = sync.data_dir.resolve()
    state = sync.state_dir.resolve()
    if data == state or data in state.parents:
        raise ConfigError("state_dir must not be inside data_dir")


def _repo_part(value: str) -> bool:
    return bool(value) and all(ch.isalnum() or ch in "._-" for ch in value)


def _apply_env_overrides(sync: SyncSettings, server: ServerSettings) -> SyncSettings:
    data = os.environ.get("DATA_DIR")
    state = os.environ.get("STATE_DIR")
    if data:
        sync.data_dir = Path(data)
    if state:
        sync.state_dir = Path(state)
    bind = os.environ.get("BIND")
    port = os.environ.get("PORT")
    if bind:
        server.bind = bind
    if port:
        try:
            server.port = int(port)
        except ValueError as exc:
            raise ConfigError(f"invalid PORT: {port}") from exc
        if not 1 <= server.port <= 65535:
            raise ConfigError(f"invalid PORT: {port}")
    return sync


def _read_yaml(path: Path) -> object:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read {path.name}: {exc}") from exc
    try:
        loaded = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path.name}: {exc}") from exc
    return {} if loaded is None else loaded


def _parse_schedule(raw: object, where: str, base: Schedule | None = None) -> Schedule:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{where} schedule must be a mapping")
    unknown = set(raw) - SCHEDULE_KEYS
    if unknown:
        raise ConfigError(f"{where} schedule has unknown keys: {sorted(unknown)}")
    cron = str(raw.get("cron", base.cron if base else "0 5 * * *"))
    timezone = str(raw.get("timezone", base.timezone if base else "Asia/Shanghai"))
    if "run_on_startup" in raw:
        run_on_startup = bool(raw["run_on_startup"])
    else:
        run_on_startup = base.run_on_startup if base else True
    if len(cron.split()) != 5:
        raise ConfigError(f"{where} cron must have 5 fields, got {cron!r}")
    if not timezone:
        raise ConfigError(f"{where} timezone is empty")
    return Schedule(cron=cron, timezone=timezone, run_on_startup=run_on_startup)


def _parse_server(raw: object) -> ServerSettings:
    if not isinstance(raw, dict):
        raise ConfigError("server must be a mapping")
    bind = str(raw.get("bind", "0.0.0.0"))
    port = int(raw.get("port", 9999))
    if not 1 <= port <= 65535:
        raise ConfigError(f"invalid port: {port}")
    return ServerSettings(bind=bind, port=port)


def _parse_sync(raw: object) -> SyncSettings:
    if not isinstance(raw, dict):
        raise ConfigError("sync must be a mapping")
    data_dir = Path(str(raw.get("data_dir", "/data/public")))
    state_dir = Path(str(raw.get("state_dir", "/data/state")))
    timeout = float(raw.get("timeout_seconds", 60))
    max_bytes = int(raw.get("max_bytes", 104857600))
    max_redirects = int(raw.get("max_redirects", 5))
    user_agent = str(raw.get("user_agent", "static-sync/1.0"))
    if timeout <= 0 or max_bytes <= 0 or max_redirects < 0:
        raise ConfigError("sync limits must be positive")
    return SyncSettings(
        data_dir=data_dir,
        state_dir=state_dir,
        timeout_seconds=timeout,
        max_bytes=max_bytes,
        max_redirects=max_redirects,
        user_agent=user_agent,
    )


def _parse_group(name: str, raw: dict, default_schedule: Schedule) -> JobGroup:
    unknown = set(raw) - GROUP_KEYS
    if unknown:
        raise ConfigError(f"{name}: unknown keys: {sorted(unknown)}")
    enabled = bool(raw.get("enabled", True))
    output_dir = _relative_path(str(raw.get("output_dir", name)), f"{name} output_dir")
    schedule = _parse_schedule(raw.get("schedule"), where=name, base=default_schedule)
    sources_raw = raw.get("sources")
    if not isinstance(sources_raw, list) or not sources_raw:
        raise ConfigError(f"{name}: sources must be a non-empty list")
    sources: list[Source] = []
    seen: set[str] = set()
    for item in sources_raw:
        source = _parse_source(name, item)
        if source.id in seen:
            raise ConfigError(f"{name}: duplicate source id {source.id}")
        seen.add(source.id)
        sources.append(source)
    return JobGroup(
        name=name,
        enabled=enabled,
        output_dir=output_dir,
        schedule=schedule,
        sources=sources,
    )


def _parse_source(group: str, raw: object) -> Source:
    if not isinstance(raw, dict):
        raise ConfigError(f"{group}: each source must be a mapping")
    unknown = set(raw) - SOURCE_KEYS
    if unknown:
        raise ConfigError(f"{group}: source has unknown keys: {sorted(unknown)}")
    source_id = str(raw.get("id") or "").strip()
    if not source_id or not source_id.replace(".", "").replace("_", "").replace("-", "").isalnum():
        raise ConfigError(f"{group}: source id must be a simple name")
    source_type = str(raw.get("type") or "").strip()
    enabled = bool(raw.get("enabled", True))
    where = f"{group}/{source_id}"
    if source_type == "url":
        url = _required_str(raw, "url", where)
        dest = _relative_path(_required_str(raw, "dest", where), f"{where} dest")
        return Source(id=source_id, enabled=enabled, type="url", dest=dest, url=url)
    if source_type == "github":
        repo = _required_str(raw, "repo", where)
        owner, _, name = repo.partition("/")
        if not name or "/" in name or not _repo_part(owner) or not _repo_part(name):
            raise ConfigError(f"{where}: repo must look like owner/name")
        ref = str(raw.get("ref") or "main")
        if not ref or ".." in ref or ref.startswith("/") or "\\" in ref:
            raise ConfigError(f"{where}: invalid ref")
        path = str(raw.get("path") or "").strip().lstrip("/")
        if ".." in Path(path).parts or "\\" in path:
            raise ConfigError(f"{where}: invalid github path")
        include = _string_list(raw.get("include"), f"{where} include")
        dest = _relative_dir(str(raw.get("dest") or "."), f"{where} dest")
        return Source(
            id=source_id,
            enabled=enabled,
            type="github",
            dest=dest,
            repo=repo,
            ref=ref,
            path=path,
            include=include,
        )
    if source_type == "site":
        base_url = _required_str(raw, "base_url", where)
        paths = [_relative_path(item, f"{where} paths") for item in _string_list(raw.get("paths"), f"{where} paths")]
        if not paths:
            raise ConfigError(f"{where}: site.paths must be a non-empty list")
        dest = _relative_dir(str(raw.get("dest") or "."), f"{where} dest")
        return Source(
            id=source_id,
            enabled=enabled,
            type="site",
            dest=dest,
            base_url=base_url,
            paths=paths,
        )
    raise ConfigError(f"{where}: unsupported type {source_type!r}")


def _required_str(raw: dict, key: str, where: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{where}: {key} is required")
    return value.strip()


def _string_list(value: object, where: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise ConfigError(f"{where} must be a list of strings")
    return [item.strip() for item in value]


def _relative_path(value: str, where: str) -> str:
    rel = value.replace("\\", "/").strip()
    if not rel or rel.startswith("/") or rel.endswith("/"):
        raise ConfigError(f"{where} must be a relative file path")
    parts = [part for part in rel.split("/") if part not in ("", ".")]
    if not parts or any(part == ".." or "\x00" in part for part in parts):
        raise ConfigError(f"{where} must be a relative file path")
    return "/".join(parts)


def _relative_dir(value: str, where: str) -> str:
    rel = value.replace("\\", "/").strip()
    if rel in ("", "."):
        return "."
    rel = rel.rstrip("/")
    return _relative_path(rel, where)
