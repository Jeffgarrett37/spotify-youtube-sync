"""YouTube Data API quota accounting.

Google resets the 10,000-unit daily quota at midnight US Pacific time. We keep
our own running total per Pacific day in the database and refuse to start an
operation we cannot afford, so a sync always stops *cleanly* rather than dying
half-way with a ``quotaExceeded`` error.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from .config import QUOTA_TIMEZONE, YT_QUOTA_COST
from .database import Database
from .errors import QuotaExceededError
from .logging_setup import get_logger

log = get_logger("quota")

_PACIFIC = ZoneInfo(QUOTA_TIMEZONE)


def pacific_day(now: datetime | None = None) -> str:
    now = now or datetime.now(tz=_PACIFIC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=_PACIFIC)
    return now.astimezone(_PACIFIC).strftime("%Y-%m-%d")


class QuotaTracker:
    def __init__(self, db: Database, daily_budget: int) -> None:
        self.db = db
        self.daily_budget = max(0, daily_budget)
        self.day = pacific_day()
        self.session_spent = 0

    def _roll_day(self) -> None:
        today = pacific_day()
        if today != self.day:
            self.day = today
            self.session_spent = 0

    @property
    def spent_today(self) -> int:
        self._roll_day()
        return self.db.quota_spent_on(self.day)

    @property
    def remaining(self) -> int:
        return max(0, self.daily_budget - self.spent_today)

    def cost(self, method: str, *, units: int | None = None) -> int:
        if units is not None:
            return units
        try:
            return YT_QUOTA_COST[method]
        except KeyError as exc:  # pragma: no cover - programmer error
            raise KeyError(f"unknown YouTube method {method!r}") from exc

    def can_afford(self, method: str, *, units: int | None = None) -> bool:
        return self.cost(method, units=units) <= self.remaining

    def require(self, method: str, *, units: int | None = None) -> None:
        need = self.cost(method, units=units)
        if need > self.remaining:
            raise QuotaExceededError(
                f"YouTube quota budget reached: need {need} units for {method}, "
                f"only {self.remaining} of {self.daily_budget} left for {self.day} (PT)"
            )

    def charge(self, method: str, *, units: int | None = None) -> None:
        self._roll_day()
        spent = self.cost(method, units=units)
        self.session_spent += spent
        total = self.db.add_quota(self.day, spent)
        log.debug(
            "quota +%d (%s) -> %d/%d today", spent, method, total, self.daily_budget
        )

    def spend(self, method: str, *, units: int | None = None) -> None:
        """require + charge in one call, for use right before an API call."""
        self.require(method, units=units)
        self.charge(method, units=units)
