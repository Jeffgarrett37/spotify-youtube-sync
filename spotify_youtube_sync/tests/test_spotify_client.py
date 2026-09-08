from __future__ import annotations

import json

import pytest

from app.errors import (
    IncompleteSpotifyFetchError,
    PlaylistUnavailableError,
    SourceStateError,
)
from app.spotify_client import SpotifyClient


class FakeResp:
    def __init__(self, status=200, payload=None, headers=None):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(f"{self.status_code}")


class FakeSession:
    """Serves a 3-track playlist across two pages."""

    def __init__(self, *, total=3, pages=None, snapshots=None):
        self.total = total
        self.snapshots = list(snapshots or ["s1", "s1", "s1", "s1", "s1", "s1"])
        self._snap_i = 0
        self.pages = pages or [
            {
                "total": total,
                "next": "https://api.spotify.com/v1/playlists/PL/items?offset=2&limit=50",
                "items": [_track_item("a"), _track_item("b")],
            },
            {"total": total, "next": None, "items": [_track_item("c")]},
        ]
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append(url)
        if "/items" not in url and "/tracks" not in url:
            snap = self.snapshots[min(self._snap_i, len(self.snapshots) - 1)]
            self._snap_i += 1
            return FakeResp(200, {
                "snapshot_id": snap, "name": "P", "owner": {"id": "me"},
                "public": True, "tracks": {"total": self.total},
            })
        if "offset=2" in url or (params and params.get("offset") == 2):
            return FakeResp(200, self.pages[1])
        return FakeResp(200, self.pages[0])

    def post(self, *a, **k):  # token refresh - not exercised (token not expired)
        return FakeResp(200, {"access_token": "x", "expires_in": 3600})


def _track_item(tid):
    return {
        "added_at": "2026-01-01T00:00:00Z",
        "is_local": False,
        "track": {
            "id": tid, "name": f"Song {tid}", "type": "track",
            "duration_ms": 200000, "explicit": False,
            "external_ids": {"isrc": f"US{tid.upper()}0000000"},
            "album": {"name": "Al", "album_type": "album"},
            "artists": [{"id": "1", "name": "Artist"}],
        },
    }


@pytest.fixture
def token_file(tmp_path):
    p = tmp_path / "spotify_token.json"
    p.write_text(json.dumps({
        "access_token": "tok", "refresh_token": "ref",
        "expires_at": 9999999999.0,
    }))
    return p


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr("app.spotify_client.time.sleep", lambda *_: None)


def test_full_paginated_fetch(token_file):
    c = SpotifyClient("id", "secret", token_file, session=FakeSession())
    snapshot, tracks = c.get_playlist_tracks("PL")
    assert snapshot == "s1"
    assert [t.track_id for t in tracks] == ["a", "b", "c"]
    assert tracks[0].isrc == "USA0000000"


def test_incomplete_page_count_raises(token_file):
    sess = FakeSession(total=5)  # claims 5, only serves 3
    c = SpotifyClient("id", "secret", token_file, session=sess)
    with pytest.raises(IncompleteSpotifyFetchError):
        c.get_playlist_tracks("PL")


def test_snapshot_change_mid_read_raises(token_file):
    # before/after snapshots differ on every attempt
    sess = FakeSession(snapshots=["s1", "s2", "s3", "s4", "s5", "s6", "s7", "s8"])
    c = SpotifyClient("id", "secret", token_file, session=sess)
    with pytest.raises(SourceStateError):
        c.get_playlist_tracks("PL")


def test_playlist_unavailable(token_file):
    class Gone(FakeSession):
        def get(self, url, **k):
            return FakeResp(404, {"error": {"status": 404}})

    c = SpotifyClient("id", "secret", token_file, session=Gone())
    with pytest.raises(PlaylistUnavailableError):
        c.get_playlist_tracks("PL")
