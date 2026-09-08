from __future__ import annotations

import dataclasses

import pytest

from app.config import Config
from app.errors import IncompleteSpotifyFetchError
from app.quota import QuotaTracker
from app.sync_engine import SyncEngine

from .fakes import FakeSpotifyClient, FakeYouTubeClient, make_track, make_video


def _cfg(config: Config, **over) -> Config:
    return dataclasses.replace(config, **over)


def build_engine(config, db, spotify, youtube) -> SyncEngine:
    quota = QuotaTracker(db, config.daily_quota_budget)
    youtube.quota = quota
    return SyncEngine(config, db, spotify, youtube, quota)


@pytest.fixture
def two_tracks():
    return [
        make_track("A", "Alpha Song", "Alpha", duration_s=200, position=0),
        make_track("B", "Beta Song", "Beta", duration_s=210, position=1),
    ]


@pytest.fixture
def youtube_with_matches():
    yt = FakeYouTubeClient()
    yt.videos = {
        "vA": make_video("vA", "Alpha - Alpha Song (Official Audio)", "Alpha - Topic", duration_s=200),
        "vB": make_video("vB", "Beta - Beta Song (Official Audio)", "Beta - Topic", duration_s=210),
        "vA2": make_video("vA2", "Alpha - Alpha Song (Official Video)", "AlphaVEVO", duration_s=205),
    }
    yt.search_map = {
        "alpha song": ["vA"],
        "beta song": ["vB"],
    }
    return yt


def test_initial_sync_adds_matches(config, db, two_tracks, youtube_with_matches):
    sp = FakeSpotifyClient(two_tracks)
    engine = build_engine(config, db, sp, youtube_with_matches)
    outcome = engine.run(trigger="startup")
    assert outcome.status == "success"
    assert outcome.added == 2
    assert {i["video_id"] for i in youtube_with_matches.inserted} == {"vA", "vB"}
    assert db.count_active_managed() == 2


def test_second_sync_is_noop_and_uses_cache(config, db, two_tracks, youtube_with_matches):
    sp = FakeSpotifyClient(two_tracks)
    build_engine(config, db, sp, youtube_with_matches).run(trigger="startup")
    searches_after_first = youtube_with_matches.search_calls
    inserts_after_first = len(youtube_with_matches.inserted)

    outcome = build_engine(config, db, sp, youtube_with_matches).run(trigger="scheduled")
    assert outcome.status == "success"
    assert outcome.added == 0
    assert outcome.unchanged == 2
    # snapshot unchanged -> Spotify full fetch not repeated
    assert sp.tracks_calls == 1
    # cached mappings -> no new YouTube searches, no new inserts
    assert youtube_with_matches.search_calls == searches_after_first
    assert len(youtube_with_matches.inserted) == inserts_after_first


def test_spotify_removal_removes_managed_youtube_item(config, db, two_tracks, youtube_with_matches):
    sp = FakeSpotifyClient(two_tracks)
    build_engine(config, db, sp, youtube_with_matches).run(trigger="startup")

    # B removed from Spotify; snapshot changes
    sp.tracks = [two_tracks[0]]
    sp.snapshot_id = "snap-2"
    outcome = build_engine(config, db, sp, youtube_with_matches).run(trigger="scheduled")

    assert outcome.status == "success"
    assert outcome.removed == 1
    assert db.count_active_managed() == 1
    assert youtube_with_matches.deleted  # B's playlist item was deleted
    assert all(i["video_id"] != "vB" for i in youtube_with_matches.playlist)


def test_incomplete_spotify_fetch_never_deletes(config, db, two_tracks, youtube_with_matches):
    sp = FakeSpotifyClient(two_tracks)
    build_engine(config, db, sp, youtube_with_matches).run(trigger="startup")
    assert db.count_active_managed() == 2

    sp.snapshot_id = "snap-2"  # force a fresh fetch
    sp.raise_on_tracks = IncompleteSpotifyFetchError("only got 1 of 2 pages")
    outcome = build_engine(config, db, sp, youtube_with_matches).run(trigger="scheduled")

    assert outcome.status == "failed"
    assert youtube_with_matches.deleted == []
    assert db.count_active_managed() == 2
    assert db.last_sync()["status"] == "failed"


def test_dry_run_makes_no_changes(config, db, two_tracks, youtube_with_matches):
    sp = FakeSpotifyClient(two_tracks)
    engine = build_engine(_cfg(config, dry_run=True), db, sp, youtube_with_matches)
    outcome = engine.run(trigger="startup")

    assert outcome.status == "dry_run"
    assert youtube_with_matches.inserted == []
    assert db.count_active_managed() == 0
    assert db.last_sync()["status"] == "dry_run"


def test_unavailable_cached_video_is_replaced(config, db, youtube_with_matches):
    track = [make_track("A", "Alpha Song", "Alpha", duration_s=200)]
    sp = FakeSpotifyClient(track)
    build_engine(config, db, sp, youtube_with_matches).run(trigger="startup")
    assert db.get_mapping("A")["youtube_video_id"] == "vA"

    # vA becomes unavailable; a fresh search should find vA2
    youtube_with_matches.videos["vA"] = make_video(
        "vA", "gone", "gone", duration_s=200, available=False
    )
    youtube_with_matches.search_map = {"alpha song": ["vA2"]}
    outcome = build_engine(config, db, sp, youtube_with_matches).run(trigger="scheduled")

    assert outcome.status == "success"
    assert db.get_mapping("A")["youtube_video_id"] == "vA2"
    assert db.count_active_managed() == 1


def test_override_takes_precedence_and_skips_search(config, db, youtube_with_matches):
    track = [make_track("A", "Alpha Song", "Alpha", duration_s=200)]
    sp = FakeSpotifyClient(track)
    youtube_with_matches.videos["forced"] = make_video("forced", "whatever", "whoever", duration_s=999)
    db.put_override("A", "forced", "manual test")

    outcome = build_engine(config, db, sp, youtube_with_matches).run(trigger="startup")

    assert outcome.status == "success"
    assert youtube_with_matches.search_calls == 0
    assert [i["video_id"] for i in youtube_with_matches.inserted] == ["forced"]
    assert db.get_mapping("A")["source"] == "override"


def _playlist_of(n, artist="Vera"):
    tracks = [make_track(f"T{i}", f"Track Number {i}", artist, duration_s=200, position=i) for i in range(n)]
    yt = FakeYouTubeClient()
    yt.videos = {
        f"v{i}": make_video(
            f"v{i}", f"{artist} - Track Number {i} (Official Audio)", f"{artist} - Topic",
            duration_s=200,
        )
        for i in range(n)
    }
    yt.search_map = {f"track number {i}": [f"v{i}"] for i in range(n)}
    return tracks, yt


def test_large_removal_is_blocked_without_confirmation(config, db):
    tracks, youtube_with_matches = _playlist_of(6)
    sp = FakeSpotifyClient(tracks)
    build_engine(config, db, sp, youtube_with_matches).run(trigger="startup")
    assert db.count_active_managed() == 6

    # Spotify now "empty" (e.g. suspicious) - snapshot changes
    sp.tracks = []
    sp.snapshot_id = "snap-empty"
    outcome = build_engine(config, db, sp, youtube_with_matches).run(trigger="scheduled")
    assert outcome.removed == 0
    assert youtube_with_matches.deleted == []
    assert db.count_active_managed() == 6  # nothing removed

    # With explicit confirmation the removals go through
    out2 = build_engine(
        _cfg(config, confirm_large_removal=True), db, sp, youtube_with_matches
    ).run(trigger="scheduled")
    assert out2.removed == 6
    assert db.count_active_managed() == 0


def test_quota_exhaustion_stops_cleanly_and_resumes(config, db):
    tracks, youtube_with_matches = _playlist_of(4)
    sp = FakeSpotifyClient(tracks)

    # Budget only enough for ~2 tracks: 2 * (search 100 + videos.list 1 + insert 50) = 302
    tight = _cfg(config, daily_quota_budget=320)
    outcome = build_engine(tight, db, sp, youtube_with_matches).run(trigger="startup")
    assert outcome.status == "partial"
    added_first = db.count_active_managed()
    assert 0 < added_first < 4

    # Next day / fresh budget: remaining tracks added, no duplicates
    db.clear_unmatched()
    build_engine(_cfg(config, daily_quota_budget=9000), db, sp, youtube_with_matches).run(
        trigger="scheduled"
    )
    assert db.count_active_managed() == 4
    assert len({i["video_id"] for i in youtube_with_matches.inserted}) == 4
