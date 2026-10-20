"""static-sync entrypoint."""

from __future__ import annotations

import logging
import os
import signal
import threading
from pathlib import Path

from app.config_loader import AppConfig, ConfigError, load_config
from app.http_server import serve
from app.scheduler import build_scheduler, group_locks
from app.status import StatusStore
from app.sync.runner import run_group

log = logging.getLogger("static-sync")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config_dir = Path(os.environ.get("CONFIG_DIR", "/config"))
    try:
        config = load_config(config_dir)
    except ConfigError as exc:
        log.error("config error: %s", exc)
        raise SystemExit(1) from exc

    _prepare_dirs(config)
    status = StatusStore(config.sync.state_dir / "status.json")
    groups = {group.name: group for group in config.groups}
    for group in config.groups:
        status.init_group(
            group.name,
            enabled=group.enabled,
            output_dir=group.output_dir,
            cron=group.schedule.cron,
            timezone_name=group.schedule.timezone,
        )
    status.retain_groups({group.name for group in config.groups})

    locks = group_locks(list(groups))

    def run_named(name: str) -> None:
        group = groups[name]
        if not group.enabled:
            return
        lock = locks[name]
        if not lock.acquire(blocking=False):
            log.info("skip overlapping run for %s", name)
            return
        try:
            run_group(group, config.sync, status)
        finally:
            lock.release()

    httpd = serve(config.server.bind, config.server.port, status)
    thread = threading.Thread(target=httpd.serve_forever, name="http", daemon=True)
    thread.start()
    log.info("status server on %s:%s", config.server.bind, config.server.port)

    try:
        scheduler = build_scheduler(config, run_named)
    except ConfigError as exc:
        log.error("config error: %s", exc)
        raise SystemExit(1) from exc
    scheduler.start()

    def startup() -> None:
        for group in config.groups:
            if group.enabled and group.schedule.run_on_startup:
                run_named(group.name)

    threading.Thread(target=startup, name="startup-sync", daemon=True).start()

    stop = threading.Event()

    def _stop(signum, _frame) -> None:
        log.info("received signal %s", signum)
        stop.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    stop.wait()
    scheduler.shutdown(wait=False)
    httpd.shutdown()


def _prepare_dirs(config: AppConfig) -> None:
    paths = [config.sync.data_dir, config.sync.state_dir]
    geteuid = getattr(os, "geteuid", None)
    if geteuid is not None and geteuid() == 0:
        import pwd

        user = pwd.getpwnam("syncer")
        for path in paths:
            path.mkdir(parents=True, exist_ok=True)
            os.chown(path, user.pw_uid, user.pw_gid)
        os.initgroups(user.pw_name, user.pw_gid)
        os.setgid(user.pw_gid)
        os.setuid(user.pw_uid)
        log.info("dropped privileges to syncer")
        return
    for path in paths:
        path.mkdir(parents=True, exist_ok=True)


if __name__ == "__main__":
    main()
