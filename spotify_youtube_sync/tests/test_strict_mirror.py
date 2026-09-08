"""strict_mirror behaviour: both modes only ever delete items the add-on added,
they differ in how aggressively they clean an orphan that no longer looks like
a clean single managed copy (e.g. the user manually added a second copy).
"""

from __future__ import annotations

import dataclasses

from app.quota import QuotaTracker
from app.sync_engine import SyncEngine

from .fakes import FakeSpotifyClient, FakeYouTubeClient, make_track, make_video


def _engine(config, db, sp, yt):
    q = QuotaTracker(db, config.daily_quota_budget)
    yt.quota = q
    return SyncEngine(config, db, sp, yt, q)


def _yt():
    yt = FakeYouTubeClient()
    yt.videos = {
        "vA": make_video("vA", "Alpha - A (Official Audio)", "Alpha - Topic", duration_s=200),
        "vB": make_video("vB", "Beta - B (Official Audio)", "Beta - Topic", duration_s=200),
    }
    yt.search_map = {" a ": ["vA"], "alpha a": ["vA"], "beta b": ["vB"]}
    return yt


def _tracks():
    return [
        make_track("A", "A", "Alpha", duration_s=200, position=0),
        make_track("B", "B", "Beta", duration_s=200, position=1),
    ]


def test_strict_removes_managed_copy_but_leaves_manual_duplicate(config, db):
    cfg = dataclasses.replace(config, strict_mirror=True)
    yt = _yt()
    sp = FakeSpotifyClient(_tracks())
    _engine(cfg, db, sp, yt).run(trigger="startup")
    assert db.count_active_managed() == 2

    # user manually adds a SECOND copy of vB
    yt.playlist.append(
        {
            "playlist_item_id": "pli-manual",
            "video_id": "vB",
            "position": 99,
            "title": "Beta - B",
            "channel": "Beta",
            "privacy_status": "public",
        }
    )
    # B removed from Spotify
    sp.tracks = [_tracks()[0]]
    sp.snapshot_id = "snap-2"
    _engine(cfg, db, sp, yt).run(trigger="scheduled")

    remaining_ids = [i["playlist_item_id"] for i in yt.playlist]
    assert "pli-manual" in remaining_ids          # manual copy preserved
    assert any(i["video_id"] == "vB" for i in yt.playlist)
    # exactly one managed delete happened
    assert len(yt.deleted) == 1
    assert db.count_active_managed() == 1         # only A still managed


def test_nonstrict_keeps_orphan_when_not_a_clean_single_copy(config, db):
    cfg = dataclasses.replace(config, strict_mirror=False)
    yt = _yt()
    sp = FakeSpotifyClient(_tracks())
    _engine(cfg, db, sp, yt).run(trigger="startup")

    yt.playlist.append(
        {
            "playlist_item_id": "pli-manual",
            "video_id": "vB",
            "position": 99,
            "title": "Beta - B",
            "channel": "Beta",
            "privacy_status": "public",
        }
    )
    sp.tracks = [_tracks()[0]]
    sp.snapshot_id = "snap-2"
    _engine(cfg, db, sp, yt).run(trigger="scheduled")

    # non-strict refuses to delete because vB now appears twice
    assert yt.deleted == []
    assert sum(1 for i in yt.playlist if i["video_id"] == "vB") == 2
    # but it stops managing B
    assert db.count_active_managed() == 1


def test_nonstrict_still_removes_clean_single_managed_copy(config, db):
    cfg = dataclasses.replace(config, strict_mirror=False)
    yt = _yt()
    sp = FakeSpotifyClient(_tracks())
    _engine(cfg, db, sp, yt).run(trigger="startup")

    sp.tracks = [_tracks()[0]]
    sp.snapshot_id = "snap-2"
    _engine(cfg, db, sp, yt).run(trigger="scheduled")

    # single recorded copy, gone from Spotify -> removed even in non-strict mode
    assert len(yt.deleted) == 1
    assert all(i["video_id"] != "vB" for i in yt.playlist)
    assert db.count_active_managed() == 1
