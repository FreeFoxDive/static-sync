"""Cron schedules per job group."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config_loader import AppConfig, ConfigError

log = logging.getLogger(__name__)


def build_scheduler(config: AppConfig, run_group: Callable[[str], None]) -> BackgroundScheduler:
    scheduler = BackgroundScheduler()
    for group in config.groups:
        if not group.enabled:
            log.info("job %s is disabled", group.name)
            continue
        try:
            trigger = CronTrigger.from_crontab(group.schedule.cron, timezone=ZoneInfo(group.schedule.timezone))
        except Exception as exc:
            raise ConfigError(f"{group.name}: invalid schedule: {exc}") from exc
        scheduler.add_job(
            run_group,
            trigger=trigger,
            args=[group.name],
            id=group.name,
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=3600,
        )
        log.info("scheduled %s at %r (%s)", group.name, group.schedule.cron, group.schedule.timezone)
    return scheduler


def group_locks(names: list[str]) -> dict[str, threading.Lock]:
    return {name: threading.Lock() for name in names}
