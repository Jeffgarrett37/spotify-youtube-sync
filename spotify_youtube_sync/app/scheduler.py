"""Runs a sync on startup and then every ``sync_interval_hours``.

The loop never dies on a sync error: it logs, records the failure, schedules
the next attempt and carries on. A manual trigger (from the ingress status
page or ``python -m app sync``) wakes it early.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from .database import Database
from .logging_setup import get_logger
from .models import SyncOutcome

log = get_logger("scheduler")

SyncCallable = Callable[[str], SyncOutcome]


class Scheduler:
    def __init__(
        self,
        run_sync: SyncCallable,
        db: Database,
        interval_hours: float,
    ) -> None:
        self._run_sync = run_sync
        self._db = db
        self._interval = timedelta(hours=max(0.05, interval_hours))
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._busy = threading.Lock()
        self._thread: threading.Thread | None = None
        self._trigger_reason = "manual"

    # --- lifecycle ----------------------------------------------------- #
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._loop, name="sync-scheduler", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=10)

    # --- manual trigger --------------------------------------------- #
    def trigger(self, reason: str = "manual") -> bool:
        """Ask for an out-of-band sync. Returns False if one is already running."""
        if self._busy.locked():
            return False
        self._trigger_reason = reason
        self._wake.set()
        return True

    @property
    def is_running(self) -> bool:
        return self._busy.locked()

    def next_run_at(self) -> datetime | None:
        raw = self._db.get_meta("next_sync_at")
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            return None

    # --- internals ------------------------------------------------- #
    def _loop(self) -> None:
        first = True
        while not self._stop.is_set():
            reason = "startup" if first else self._trigger_reason
            first = False
            self._safe_run(reason)
            self._schedule_next()
            # Sleep until the interval elapses or someone triggers us.
            woke = self._wake.wait(timeout=self._interval.total_seconds())
            self._wake.clear()
            if self._stop.is_set():
                break
            self._trigger_reason = "manual" if woke else "scheduled"

    def _safe_run(self, reason: str) -> None:
        with self._busy:
            try:
                self._run_sync(reason)
            except Exception:  # noqa: BLE001 - loop must survive anything
                log.exception("sync raised an unhandled exception; will retry next cycle")

    def _schedule_next(self) -> None:
        nxt = datetime.now(UTC) + self._interval
        self._db.set_meta("next_sync_at", nxt.isoformat(timespec="seconds"))
        hours = self._interval.total_seconds() / 3600
        log.info("Next sync in %s", _humanize(hours))


def _humanize(hours: float) -> str:
    if abs(hours - round(hours)) < 1e-6:
        h = round(hours)
        return f"{h} hour" if h == 1 else f"{h} hours"
    return f"{hours:.1f} hours"
