from __future__ import annotations

from app.database import CURRENT_SCHEMA_VERSION, Database

from .fakes import make_track


def test_schema_initialises_and_is_idempotent(tmp_path):
    path = tmp_path / "s.sqlite"
    db1 = Database(path)
    with db1._lock:
        v = db1._conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()["v"]
    assert v == CURRENT_SCHEMA_VERSION
    db1.close()
    # reopening must not wipe or re-migrate
    db2 = Database(path)
    db2.set_meta("k", "v")
    db2.close()
    db3 = Database(path)
    assert db3.get_meta("k") == "v"
    db3.close()


def test_overrides_roundtrip(db):
    db.put_override("spot1", "yt1", "note")
    assert db.get_override("spot1") == "yt1"
    assert db.all_overrides() == {"spot1": "yt1"}
    db.put_override("spot1", "yt2")
    assert db.get_override("spot1") == "yt2"
    assert db.delete_override("spot1") is True
    assert db.get_override("spot1") is None


def test_mapping_cache_keeps_first_matched_date(db):
    t = make_track("s1", "Song", "Artist")
    db.upsert_mapping(t, "v1", "YT Song", "Artist - Topic", 0.95, "high")
    first = db.get_mapping("s1")["first_matched_at"]
    db.upsert_mapping(t, "v2", "YT Song 2", "Artist", 0.91, "high")
    row = db.get_mapping("s1")
    assert row["youtube_video_id"] == "v2"
    assert row["first_matched_at"] == first  # unchanged


def test_unmatched_retry_count_and_fingerprint_reset(db):
    db.upsert_unmatched(
        spotify_track_id="s1", title="T", artist="A", reason="r",
        best_candidate_id="c", best_candidate_title="ct", best_score=0.6,
        tier="medium", fingerprint="fp1",
    )
    db.upsert_unmatched(
        spotify_track_id="s1", title="T", artist="A", reason="r",
        best_candidate_id="c", best_candidate_title="ct", best_score=0.6,
        tier="medium", fingerprint="fp1",
    )
    assert db.get_unmatched("s1")["retry_count"] == 2
    # metadata changed -> counter resets
    db.upsert_unmatched(
        spotify_track_id="s1", title="T2", artist="A", reason="r",
        best_candidate_id="c", best_candidate_title="ct", best_score=0.6,
        tier="medium", fingerprint="fp2",
    )
    assert db.get_unmatched("s1")["retry_count"] == 1
    assert db.clear_unmatched("s1") == 1


def test_quota_ledger_accumulates(db):
    assert db.quota_spent_on("2026-09-08") == 0
    assert db.add_quota("2026-09-08", 100) == 100
    assert db.add_quota("2026-09-08", 50) == 150
    assert db.quota_spent_on("2026-09-08") == 150
    assert db.quota_spent_on("2026-09-09") == 0


def test_managed_item_lifecycle(db):
    item_id = db.record_managed_add("s1", 0, "v1", "pli1", 0, "high match")
    assert db.count_active_managed() == 1
    assert db.managed_item_for("s1", 0)["youtube_video_id"] == "v1"
    db.deactivate_managed_item(item_id, "removed from spotify")
    assert db.count_active_managed() == 0
    assert db.managed_item_for("s1", 0) is None


def test_sync_history_records(db):
    sid = db.start_sync("2026-09-08T00:00:00+00:00")
    db.finish_sync(
        sid, status="success", finished_at="2026-09-08T00:01:00+00:00",
        tracks_evaluated=10, additions=2, removals=1, reorders=0, unmatched=0,
        quota_spent=201, errors=[], message="ok",
    )
    last = db.last_sync()
    assert last["status"] == "success"
    assert last["additions"] == 2
    assert db.last_successful_sync()["id"] == sid
