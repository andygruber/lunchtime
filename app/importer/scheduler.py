"""Periodic imports with APScheduler (in-process; run a single app worker)."""

from __future__ import annotations

import datetime as dt
import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from sqlalchemy import select

from .. import db
from .pipeline import Importer

log = logging.getLogger(__name__)


class ImportScheduler:
    def __init__(self, importer: Importer, default_schedule: str, timezone: str):
        self.importer = importer
        self.default_schedule = default_schedule
        self.timezone = timezone
        self.scheduler = BackgroundScheduler(
            timezone=timezone, job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 3600}
        )

    def start(self) -> None:
        self.reschedule()
        # catch up after a deploy/restart if the last run is older than an hour
        self.scheduler.add_job(self._catch_up, DateTrigger(dt.datetime.now() + dt.timedelta(seconds=15)),
                               id="catch-up")
        self.scheduler.start()

    def shutdown(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)

    def reschedule(self) -> None:
        """(Re)create cron jobs from the current profiles — called after profile edits."""
        for job in self.scheduler.get_jobs():
            if job.id.startswith("source:"):
                job.remove()
        for source in self.importer.store.sources.values():
            if not source.enabled:
                continue
            for n, expr in enumerate(e.strip() for e in (source.schedule or self.default_schedule).split(";")):
                if not expr:
                    continue
                try:
                    trigger = CronTrigger.from_crontab(expr, timezone=self.timezone)
                except ValueError as e:
                    log.error("invalid schedule %r for %s: %s", expr, source.id, e)
                    continue
                self.scheduler.add_job(self.importer.run_source, trigger, args=[source.id], id=f"source:{source.id}:{n}",
                                       replace_existing=True)

    def next_runs(self) -> dict[str, dt.datetime | None]:
        out: dict[str, dt.datetime | None] = {}
        for job in self.scheduler.get_jobs():
            if job.id.startswith("source:"):
                source_id = job.id.split(":")[1]
                nxt = job.next_run_time
                if nxt and (out.get(source_id) is None or nxt < out[source_id]):
                    out[source_id] = nxt
        return out

    def _catch_up(self) -> None:
        cutoff = db.utcnow() - dt.timedelta(hours=1)
        for source_id in list(self.importer.store.sources):
            with db.session() as s:
                last = s.scalars(
                    select(db.ImportRun.started_at).where(db.ImportRun.source_id == source_id)
                    .order_by(db.ImportRun.started_at.desc()).limit(1)
                ).first()
            if last is None or last < cutoff:
                self.importer.run_source(source_id, trigger="startup")
