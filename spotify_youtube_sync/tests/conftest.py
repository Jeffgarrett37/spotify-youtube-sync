from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Config
from app.database import Database
from app.quota import QuotaTracker


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(
        data_dir=tmp_path,
        spotify_client_id="sid",
        spotify_client_secret="ssecret",
        spotify_playlist_id="SP_PLAYLIST",
        youtube_client_id="yid",
        youtube_client_secret="ysecret",
        youtube_playlist_id="YT_PLAYLIST",
        sync_interval_hours=6,
        strict_mirror=True,
        dry_run=False,
        auto_add_threshold=0.85,
        daily_quota_budget=9000,
        unmatched_retry_hours=168,
    )


@pytest.fixture
def db(tmp_path: Path) -> Database:
    d = Database(tmp_path / "state.sqlite")
    yield d
    d.close()


@pytest.fixture
def quota(db: Database, config: Config) -> QuotaTracker:
    return QuotaTracker(db, config.daily_quota_budget)
