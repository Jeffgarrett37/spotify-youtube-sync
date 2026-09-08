from __future__ import annotations

from app.spotify_client import SpotifyClient


def _item(track_id, name="Song", *, is_local=False, ttype="track", track=True):
    if not track:
        return {"added_at": "2026-01-01T00:00:00Z", "is_local": is_local, "track": None}
    return {
        "added_at": "2026-01-01T00:00:00Z",
        "is_local": is_local,
        "track": {
            "id": track_id,
            "name": name,
            "type": ttype,
            "duration_ms": 200000,
            "explicit": False,
            "external_ids": {"isrc": "USABC1234567"},
            "album": {"name": "Album", "album_type": "album"},
            "artists": [{"id": "a1", "name": "The Artist"}],
        },
    }


def test_duplicate_tracks_get_distinct_occurrences():
    raw = [_item("dup"), _item("other"), _item("dup")]
    tracks = SpotifyClient._build_tracks(raw)
    assert [t.track_id for t in tracks] == ["dup", "other", "dup"]
    assert tracks[0].occurrence == 0
    assert tracks[2].occurrence == 1
    assert tracks[0].key == ("dup", 0)
    assert tracks[2].key == ("dup", 1)
    # positions preserve playlist order
    assert tracks[0].playlist_position == 0
    assert tracks[2].playlist_position == 2


def test_non_tracks_and_locals_are_skipped():
    raw = [
        _item("keep"),
        _item("local", is_local=True),
        _item("ep", ttype="episode"),
        _item("none", track=False),
        {"is_local": False, "track": {"id": None, "name": "unavailable"}},
    ]
    tracks = SpotifyClient._build_tracks(raw)
    assert [t.track_id for t in tracks] == ["keep"]


def test_isrc_uppercased_and_metadata_captured():
    (t,) = SpotifyClient._build_tracks([_item("x", "My Song")])
    assert t.isrc == "USABC1234567"
    assert t.primary_artist == "The Artist"
    assert t.duration_s == 200.0
    assert t.album == "Album"
