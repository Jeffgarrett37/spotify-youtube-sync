"""Runtime configuration.

Values come from ``SYNC_*`` environment variables which ``run.sh`` populates
from the Home Assistant add-on options. ``Config.from_env`` is the only place
that reads ``os.environ`` so tests can build a ``Config`` directly.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .errors import ConfigError

# --------------------------------------------------------------------------- #
# Match-confidence thresholds.
#
# The matcher produces a score in [0, 1].  These constants decide what happens:
#
#   score >= auto_add_threshold      -> HIGH  : add automatically
#   medium_threshold <= score < auto -> MEDIUM: do NOT add; store as "unmatched
#                                               / review required" so a human
#                                               can add a manual override
#   score < medium_threshold         -> LOW   : reject outright
#
# `auto_add_threshold` is user-facing (`match_threshold` option, default 0.85).
# The others are derived from it but clamped to sane floors.  They are defined
# here (not buried in the matcher) so they are easy to find and tune.
# --------------------------------------------------------------------------- #
DEFAULT_AUTO_ADD_THRESHOLD = 0.85
MEDIUM_THRESHOLD_DELTA = 0.20      # medium band starts this far below auto
MEDIUM_THRESHOLD_FLOOR = 0.55      # never treat anything below this as "review"
HARD_REJECT_FLOOR = 0.40          # scores under this are never surfaced at all

# Duration tolerance, in seconds, before a candidate starts losing points.
DURATION_GRACE_SECONDS = 12
# Absolute duration delta beyond which a candidate is disqualified entirely
# (protects against 10-hour loops, single vs. album-version mixups, etc.).
DURATION_HARD_LIMIT_SECONDS = 75

# YouTube Data API v3 quota unit costs (verified against Google docs, 2026).
YT_QUOTA_COST = {
    "search.list": 100,
    "videos.list": 1,
    "playlists.list": 1,
    "playlistItems.list": 1,
    "playlistItems.insert": 50,
    "playlistItems.update": 50,
    "playlistItems.delete": 50,
}

# The Pacific-time day boundary Google uses to reset YouTube quota.
QUOTA_TIMEZONE = "America/Los_Angeles"

_TRUE = {"1", "true", "yes", "on"}


def _get(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _get_bool(name: str, default: bool) -> bool:
    raw = _get(name)
    if raw == "":
        return default
    return raw.lower() in _TRUE


def _get_int(name: str, default: int) -> int:
    raw = _get(name)
    if raw == "":
        return default
    try:
        return int(float(raw))
    except ValueError as exc:  # pragma: no cover - defensive
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def _get_float(name: str, default: float) -> float:
    raw = _get(name)
    if raw == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:  # pragma: no cover - defensive
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc


@dataclass(frozen=True)
class Config:
    data_dir: Path

    spotify_client_id: str
    spotify_client_secret: str
    spotify_playlist_id: str

    youtube_client_id: str
    youtube_client_secret: str
    youtube_playlist_id: str

    sync_interval_hours: int = 6
    strict_mirror: bool = True
    dry_run: bool = False
    log_level: str = "info"

    auto_add_threshold: float = DEFAULT_AUTO_ADD_THRESHOLD
    manage_order: bool = False
    max_reorders_per_sync: int = 20
    max_removals_per_sync: int = 50
    removal_safety_ratio: float = 0.34
    confirm_large_removal: bool = False

    daily_quota_budget: int = 9000
    unmatched_retry_hours: int = 168
    region_code: str = "US"

    ingress_port: int = 8099

    # Derived, not set directly.
    medium_threshold: float = field(init=False)
    hard_reject_threshold: float = field(init=False)

    def __post_init__(self) -> None:
        medium = max(
            MEDIUM_THRESHOLD_FLOOR,
            min(self.auto_add_threshold, self.auto_add_threshold - MEDIUM_THRESHOLD_DELTA),
        )
        medium = min(medium, self.auto_add_threshold)
        object.__setattr__(self, "medium_threshold", medium)
        object.__setattr__(
            self, "hard_reject_threshold", min(HARD_REJECT_FLOOR, medium)
        )

    # --- paths ---------------------------------------------------------------
    @property
    def db_path(self) -> Path:
        return self.data_dir / "state.sqlite"

    @property
    def spotify_token_path(self) -> Path:
        return self.data_dir / "spotify_token.json"

    @property
    def youtube_token_path(self) -> Path:
        return self.data_dir / "youtube_token.json"

    # --- validation --------------------------------------------------------- #
    def require_for_sync(self) -> None:
        """Raise ConfigError if anything required for a real sync is missing."""
        missing = [
            key
            for key, value in {
                "spotify_client_id": self.spotify_client_id,
                "spotify_client_secret": self.spotify_client_secret,
                "spotify_playlist_id": self.spotify_playlist_id,
                "youtube_client_id": self.youtube_client_id,
                "youtube_client_secret": self.youtube_client_secret,
                "youtube_playlist_id": self.youtube_playlist_id,
            }.items()
            if not value
        ]
        if missing:
            raise ConfigError(
                "Missing required add-on options: " + ", ".join(sorted(missing))
            )
        if not (0.0 < self.auto_add_threshold <= 1.0):
            raise ConfigError("match_threshold must be between 0 and 1")
        if self.sync_interval_hours < 1:
            raise ConfigError("sync_interval_hours must be >= 1")

    @classmethod
    def from_env(cls) -> "Config":
        data_dir = Path(_get("SYNC_DATA_DIR", "/data"))
        return cls(
            data_dir=data_dir,
            spotify_client_id=_get("SYNC_SPOTIFY_CLIENT_ID"),
            spotify_client_secret=_get("SYNC_SPOTIFY_CLIENT_SECRET"),
            spotify_playlist_id=_get("SYNC_SPOTIFY_PLAYLIST_ID"),
            youtube_client_id=_get("SYNC_YOUTUBE_CLIENT_ID"),
            youtube_client_secret=_get("SYNC_YOUTUBE_CLIENT_SECRET"),
            youtube_playlist_id=_get("SYNC_YOUTUBE_PLAYLIST_ID"),
            sync_interval_hours=_get_int("SYNC_INTERVAL_HOURS", 6),
            strict_mirror=_get_bool("SYNC_STRICT_MIRROR", True),
            dry_run=_get_bool("SYNC_DRY_RUN", False),
            log_level=_get("SYNC_LOG_LEVEL", "info").lower() or "info",
            auto_add_threshold=_get_float(
                "SYNC_MATCH_THRESHOLD", DEFAULT_AUTO_ADD_THRESHOLD
            ),
            manage_order=_get_bool("SYNC_MANAGE_ORDER", False),
            max_reorders_per_sync=_get_int("SYNC_MAX_REORDERS_PER_SYNC", 20),
            max_removals_per_sync=_get_int("SYNC_MAX_REMOVALS_PER_SYNC", 50),
            removal_safety_ratio=_get_float("SYNC_REMOVAL_SAFETY_RATIO", 0.34),
            confirm_large_removal=_get_bool("SYNC_CONFIRM_LARGE_REMOVAL", False),
            daily_quota_budget=_get_int("SYNC_DAILY_QUOTA_BUDGET", 9000),
            unmatched_retry_hours=_get_int("SYNC_UNMATCHED_RETRY_HOURS", 168),
            region_code=_get("SYNC_REGION_CODE", "US") or "US",
            ingress_port=_get_int("SYNC_INGRESS_PORT", 8099),
        )
