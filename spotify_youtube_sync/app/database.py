"""SQLite persistence layer.

One file, ``/data/state.sqlite``. All access goes through the ``Database``
class which serialises writes with a lock (the scheduler thread and the tiny
status HTTP server share one connection).

Schema changes are done with forward-only numbered migrations so upgrading the
add-on never destroys existing state.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .errors import DatabaseError
from .logging_setup import get_logger
from .models import SpotifyTrack

log = get_logger("db")


def _utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# Migrations. Each entry is (target_version, sql_or_callable). Applied in order
# for any database whose recorded version is lower.
# --------------------------------------------------------------------------- #
_MIGRATIONS: list[tuple[int, str]] = [
    (
        1,
        """
        CREATE TABLE IF NOT EXISTS track_mapping (
            spotify_track_id   TEXT PRIMARY KEY,
            spotify_artist     TEXT NOT NULL,
            spotify_title      TEXT NOT NULL,
            spotify_album      TEXT,
            spotify_duration_ms INTEGER,
            spotify_isrc       TEXT,
            youtube_video_id   TEXT NOT NULL,
            youtube_title      TEXT,
            youtube_channel    TEXT,
            match_score        REAL,
            match_tier         TEXT,
            source             TEXT NOT NULL DEFAULT 'auto',  -- auto | override
            first_matched_at   TEXT NOT NULL,
            last_verified_at   TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS managed_items (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            spotify_track_id   TEXT NOT NULL,
            occurrence         INTEGER NOT NULL DEFAULT 0,
            youtube_video_id   TEXT NOT NULL,
            playlist_item_id   TEXT,
            position           INTEGER,
            active             INTEGER NOT NULL DEFAULT 1,
            reason             TEXT,
            added_at           TEXT NOT NULL,
            removed_at         TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_managed_active
            ON managed_items (active, spotify_track_id, occurrence);
        CREATE INDEX IF NOT EXISTS idx_managed_video
            ON managed_items (youtube_video_id);

        CREATE TABLE IF NOT EXISTS overrides (
            spotify_track_id   TEXT PRIMARY KEY,
            youtube_video_id   TEXT NOT NULL,
            note               TEXT,
            created_at         TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS unmatched_tracks (
            spotify_track_id     TEXT PRIMARY KEY,
            spotify_title        TEXT,
            spotify_artist       TEXT,
            reason               TEXT,
            best_candidate_id    TEXT,
            best_candidate_title TEXT,
            best_score           REAL,
            tier                 TEXT,
            metadata_fingerprint TEXT,
            first_seen_at        TEXT NOT NULL,
            last_attempt_at      TEXT NOT NULL,
            retry_count          INTEGER NOT NULL DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS sync_history (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at         TEXT NOT NULL,
            finished_at        TEXT,
            status             TEXT NOT NULL,
            tracks_evaluated   INTEGER DEFAULT 0,
            additions          INTEGER DEFAULT 0,
            removals           INTEGER DEFAULT 0,
            reorders           INTEGER DEFAULT 0,
            unmatched          INTEGER DEFAULT 0,
            quota_spent        INTEGER DEFAULT 0,
            errors             TEXT,
            message            TEXT
        );

        CREATE TABLE IF NOT EXISTS quota_usage (
            day          TEXT PRIMARY KEY,   -- YYYY-MM-DD in America/Los_Angeles
            units_spent  INTEGER NOT NULL DEFAULT 0,
            updated_at   TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS kv (
            key    TEXT PRIMARY KEY,
            value  TEXT
        );
        """,
    ),
]

CURRENT_SCHEMA_VERSION = _MIGRATIONS[-1][0]


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        try:
            self._conn = sqlite3.connect(
                str(path), check_same_thread=False, timeout=30.0
            )
        except sqlite3.Error as exc:  # pragma: no cover - defensive
            raise DatabaseError(f"cannot open database {path}: {exc}") from exc
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._migrate()

    # --- lifecycle --------------------------------------------------------- #
    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def _migrate(self) -> None:
        with self._tx() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)"
            )
            row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
            current = row["v"] if row and row["v"] is not None else 0
            for target, script in _MIGRATIONS:
                if target > current:
                    log.info("Applying database migration -> v%s", target)
                    conn.executescript(script)
                    conn.execute(
                        "INSERT INTO schema_version (version) VALUES (?)", (target,)
                    )
            if current == 0:
                log.info("Initialised database schema at v%s", CURRENT_SCHEMA_VERSION)

    # --- key/value ------------------------------------------------------- #
    def get_meta(self, key: str, default: str | None = None) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM kv WHERE key = ?", (key,)
            ).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO kv (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def get_meta_json(self, key: str, default: Any = None) -> Any:
        raw = self.get_meta(key)
        if raw is None:
            return default
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return default

    def set_meta_json(self, key: str, value: Any) -> None:
        self.set_meta(key, json.dumps(value, separators=(",", ":")))

    # --- overrides ------------------------------------------------------- #
    def get_override(self, spotify_track_id: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT youtube_video_id FROM overrides WHERE spotify_track_id = ?",
                (spotify_track_id,),
            ).fetchone()
        return row["youtube_video_id"] if row else None

    def all_overrides(self) -> dict[str, str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT spotify_track_id, youtube_video_id FROM overrides"
            ).fetchall()
        return {r["spotify_track_id"]: r["youtube_video_id"] for r in rows}

    def put_override(self, spotify_track_id: str, youtube_video_id: str, note: str = "") -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO overrides (spotify_track_id, youtube_video_id, note, created_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(spotify_track_id) DO UPDATE SET "
                "  youtube_video_id = excluded.youtube_video_id, "
                "  note = excluded.note, created_at = excluded.created_at",
                (spotify_track_id, youtube_video_id, note, _utcnow()),
            )

    def delete_override(self, spotify_track_id: str) -> bool:
        with self._tx() as conn:
            cur = conn.execute(
                "DELETE FROM overrides WHERE spotify_track_id = ?", (spotify_track_id,)
            )
        return cur.rowcount > 0

    # --- track mapping cache ------------------------------------------- #
    def get_mapping(self, spotify_track_id: str) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM track_mapping WHERE spotify_track_id = ?",
                (spotify_track_id,),
            ).fetchone()

    def upsert_mapping(
        self,
        track: SpotifyTrack,
        youtube_video_id: str,
        youtube_title: str,
        youtube_channel: str,
        score: float,
        tier: str,
        source: str = "auto",
    ) -> None:
        now = _utcnow()
        with self._tx() as conn:
            existing = conn.execute(
                "SELECT first_matched_at FROM track_mapping WHERE spotify_track_id = ?",
                (track.track_id,),
            ).fetchone()
            first = existing["first_matched_at"] if existing else now
            conn.execute(
                """
                INSERT INTO track_mapping (
                    spotify_track_id, spotify_artist, spotify_title, spotify_album,
                    spotify_duration_ms, spotify_isrc, youtube_video_id, youtube_title,
                    youtube_channel, match_score, match_tier, source,
                    first_matched_at, last_verified_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(spotify_track_id) DO UPDATE SET
                    spotify_artist = excluded.spotify_artist,
                    spotify_title = excluded.spotify_title,
                    spotify_album = excluded.spotify_album,
                    spotify_duration_ms = excluded.spotify_duration_ms,
                    spotify_isrc = excluded.spotify_isrc,
                    youtube_video_id = excluded.youtube_video_id,
                    youtube_title = excluded.youtube_title,
                    youtube_channel = excluded.youtube_channel,
                    match_score = excluded.match_score,
                    match_tier = excluded.match_tier,
                    source = excluded.source,
                    last_verified_at = excluded.last_verified_at
                """,
                (
                    track.track_id, track.primary_artist, track.name, track.album,
                    track.duration_ms, track.isrc, youtube_video_id, youtube_title,
                    youtube_channel, score, tier, source, first, now,
                ),
            )

    def touch_mapping_verified(self, spotify_track_id: str) -> None:
        with self._tx() as conn:
            conn.execute(
                "UPDATE track_mapping SET last_verified_at = ? WHERE spotify_track_id = ?",
                (_utcnow(), spotify_track_id),
            )

    def delete_mapping(self, spotify_track_id: str) -> None:
        with self._tx() as conn:
            conn.execute(
                "DELETE FROM track_mapping WHERE spotify_track_id = ?",
                (spotify_track_id,),
            )

    # --- managed items ------------------------------------------------- #
    def active_managed_items(self) -> list[sqlite3.Row]:
        with self._lock:
            return list(
                self._conn.execute(
                    "SELECT * FROM managed_items WHERE active = 1 "
                    "ORDER BY position IS NULL, position, id"
                ).fetchall()
            )

    def managed_item_for(
        self, spotify_track_id: str, occurrence: int
    ) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM managed_items WHERE active = 1 AND spotify_track_id = ? "
                "AND occurrence = ? ORDER BY id LIMIT 1",
                (spotify_track_id, occurrence),
            ).fetchone()

    def record_managed_add(
        self,
        spotify_track_id: str,
        occurrence: int,
        youtube_video_id: str,
        playlist_item_id: str | None,
        position: int | None,
        reason: str = "",
    ) -> int:
        with self._tx() as conn:
            cur = conn.execute(
                "INSERT INTO managed_items (spotify_track_id, occurrence, youtube_video_id, "
                "playlist_item_id, position, active, reason, added_at) "
                "VALUES (?,?,?,?,?,1,?,?)",
                (
                    spotify_track_id, occurrence, youtube_video_id,
                    playlist_item_id, position, reason, _utcnow(),
                ),
            )
            return int(cur.lastrowid)

    def update_managed_item(
        self,
        item_id: int,
        *,
        playlist_item_id: str | None = None,
        position: int | None = None,
    ) -> None:
        sets, params = [], []
        if playlist_item_id is not None:
            sets.append("playlist_item_id = ?")
            params.append(playlist_item_id)
        if position is not None:
            sets.append("position = ?")
            params.append(position)
        if not sets:
            return
        params.append(item_id)
        with self._tx() as conn:
            conn.execute(
                f"UPDATE managed_items SET {', '.join(sets)} WHERE id = ?", params
            )

    def deactivate_managed_item(self, item_id: int, reason: str) -> None:
        with self._tx() as conn:
            conn.execute(
                "UPDATE managed_items SET active = 0, removed_at = ?, reason = ? WHERE id = ?",
                (_utcnow(), reason, item_id),
            )

    def count_active_managed(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS c FROM managed_items WHERE active = 1"
            ).fetchone()
        return int(row["c"])

    # --- unmatched ---------------------------------------------------- #
    def get_unmatched(self, spotify_track_id: str) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM unmatched_tracks WHERE spotify_track_id = ?",
                (spotify_track_id,),
            ).fetchone()

    def all_unmatched(self) -> list[sqlite3.Row]:
        with self._lock:
            return list(
                self._conn.execute(
                    "SELECT * FROM unmatched_tracks ORDER BY last_attempt_at DESC"
                ).fetchall()
            )

    def upsert_unmatched(
        self,
        *,
        spotify_track_id: str,
        title: str,
        artist: str,
        reason: str,
        best_candidate_id: str | None,
        best_candidate_title: str | None,
        best_score: float | None,
        tier: str,
        fingerprint: str,
    ) -> None:
        now = _utcnow()
        with self._tx() as conn:
            existing = conn.execute(
                "SELECT retry_count, first_seen_at, metadata_fingerprint "
                "FROM unmatched_tracks WHERE spotify_track_id = ?",
                (spotify_track_id,),
            ).fetchone()
            if existing:
                # Reset the retry count if the Spotify metadata changed.
                changed = existing["metadata_fingerprint"] != fingerprint
                retry = 1 if changed else existing["retry_count"] + 1
                first_seen = existing["first_seen_at"]
            else:
                retry = 1
                first_seen = now
            conn.execute(
                """
                INSERT INTO unmatched_tracks (
                    spotify_track_id, spotify_title, spotify_artist, reason,
                    best_candidate_id, best_candidate_title, best_score, tier,
                    metadata_fingerprint, first_seen_at, last_attempt_at, retry_count
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(spotify_track_id) DO UPDATE SET
                    spotify_title = excluded.spotify_title,
                    spotify_artist = excluded.spotify_artist,
                    reason = excluded.reason,
                    best_candidate_id = excluded.best_candidate_id,
                    best_candidate_title = excluded.best_candidate_title,
                    best_score = excluded.best_score,
                    tier = excluded.tier,
                    metadata_fingerprint = excluded.metadata_fingerprint,
                    last_attempt_at = excluded.last_attempt_at,
                    retry_count = excluded.retry_count
                """,
                (
                    spotify_track_id, title, artist, reason, best_candidate_id,
                    best_candidate_title, best_score, tier, fingerprint,
                    first_seen, now, retry,
                ),
            )

    def clear_unmatched(self, spotify_track_id: str | None = None) -> int:
        with self._tx() as conn:
            if spotify_track_id:
                cur = conn.execute(
                    "DELETE FROM unmatched_tracks WHERE spotify_track_id = ?",
                    (spotify_track_id,),
                )
            else:
                cur = conn.execute("DELETE FROM unmatched_tracks")
        return cur.rowcount

    # --- sync history ------------------------------------------------ #
    def start_sync(self, started_at: str) -> int:
        with self._tx() as conn:
            cur = conn.execute(
                "INSERT INTO sync_history (started_at, status) VALUES (?, 'running')",
                (started_at,),
            )
            return int(cur.lastrowid)

    def finish_sync(
        self,
        sync_id: int,
        *,
        status: str,
        finished_at: str,
        tracks_evaluated: int,
        additions: int,
        removals: int,
        reorders: int,
        unmatched: int,
        quota_spent: int,
        errors: Iterable[str],
        message: str,
    ) -> None:
        with self._tx() as conn:
            conn.execute(
                """
                UPDATE sync_history SET
                    finished_at = ?, status = ?, tracks_evaluated = ?, additions = ?,
                    removals = ?, reorders = ?, unmatched = ?, quota_spent = ?,
                    errors = ?, message = ?
                WHERE id = ?
                """,
                (
                    finished_at, status, tracks_evaluated, additions, removals,
                    reorders, unmatched, quota_spent,
                    json.dumps(list(errors)), message, sync_id,
                ),
            )

    def recent_syncs(self, limit: int = 10) -> list[sqlite3.Row]:
        with self._lock:
            return list(
                self._conn.execute(
                    "SELECT * FROM sync_history ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
            )

    def last_sync(self) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM sync_history ORDER BY id DESC LIMIT 1"
            ).fetchone()

    def last_successful_sync(self) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM sync_history WHERE status IN ('success','dry_run') "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()

    # --- quota ledger ---------------------------------------------- #
    def quota_spent_on(self, day: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT units_spent FROM quota_usage WHERE day = ?", (day,)
            ).fetchone()
        return int(row["units_spent"]) if row else 0

    def add_quota(self, day: str, units: int) -> int:
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO quota_usage (day, units_spent, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(day) DO UPDATE SET "
                "  units_spent = units_spent + excluded.units_spent, "
                "  updated_at = excluded.updated_at",
                (day, units, _utcnow()),
            )
            row = conn.execute(
                "SELECT units_spent FROM quota_usage WHERE day = ?", (day,)
            ).fetchone()
        return int(row["units_spent"])
