"""Build a health/status snapshot from the database.

Consumed by the ingress status page (`/`), the JSON endpoint (`/health`) and
`python -m app status`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from .config import Config
from .database import Database
from .quota import QuotaTracker, pacific_day


def _iso_to_human(value: str | None) -> str | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    delta = datetime.now(UTC) - dt
    secs = int(delta.total_seconds())
    if secs < 0:
        secs = abs(secs)
        rel = f"in {_dur(secs)}"
    else:
        rel = f"{_dur(secs)} ago"
    return f"{dt.isoformat(timespec='seconds')} ({rel})"


def _dur(secs: int) -> str:
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if secs >= size:
            return f"{secs // size}{unit}"
    return f"{secs}s"


def build_status(
    config: Config,
    db: Database,
    quota: QuotaTracker | None,
    *,
    configured: bool,
    tokens_present: dict[str, bool],
    scheduler_running: bool,
    next_run: str | None,
) -> dict[str, Any]:
    last = db.last_sync()
    last_ok = db.last_successful_sync()
    unmatched = db.all_unmatched()
    overrides = db.all_overrides()

    day = pacific_day()
    quota_spent = db.quota_spent_on(day)

    healthy = configured and all(tokens_present.values())
    if last and last["status"] == "failed":
        healthy = False

    return {
        "healthy": healthy,
        "configured": configured,
        "dry_run": config.dry_run,
        "strict_mirror": config.strict_mirror,
        "sync_interval_hours": config.sync_interval_hours,
        "tokens_present": tokens_present,
        "scheduler_running": scheduler_running,
        "last_attempt_at": _iso_to_human(db.get_meta("last_attempt_at")),
        "last_success_at": _iso_to_human(db.get_meta("last_success_at")),
        "last_finished_at": _iso_to_human(db.get_meta("last_finished_at")),
        "next_sync_at": _iso_to_human(next_run),
        "last_result": db.get_meta("last_result"),
        "last_sync": _sync_row(last),
        "last_successful_sync": _sync_row(last_ok),
        "managed_items_active": db.count_active_managed(),
        "cached_mappings": _count(db, "track_mapping"),
        "unmatched_count": len(unmatched),
        "unmatched": [
            {
                "spotify_track_id": r["spotify_track_id"],
                "title": r["spotify_title"],
                "artist": r["spotify_artist"],
                "reason": r["reason"],
                "best_candidate_id": r["best_candidate_id"],
                "best_score": r["best_score"],
                "tier": r["tier"],
                "retry_count": r["retry_count"],
                "last_attempt_at": r["last_attempt_at"],
            }
            for r in unmatched[:100]
        ],
        "overrides": overrides,
        "quota": {
            "pacific_day": day,
            "spent_today": quota_spent,
            "daily_budget": config.daily_quota_budget,
            "remaining": max(0, config.daily_quota_budget - quota_spent),
        },
        "recent_syncs": [_sync_row(r) for r in db.recent_syncs(10)],
    }


def _sync_row(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "status": row["status"],
        "tracks_evaluated": row["tracks_evaluated"],
        "additions": row["additions"],
        "removals": row["removals"],
        "reorders": row["reorders"],
        "unmatched": row["unmatched"],
        "quota_spent": row["quota_spent"],
        "message": row["message"],
    }


def _count(db: Database, table: str) -> int:
    with db._lock:  # noqa: SLF001 - internal helper
        row = db._conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()
    return int(row["c"])
