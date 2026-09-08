"""In-memory fakes for the Spotify and YouTube clients.

Tests never touch the network or real credentials.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field

from app.errors import PlaylistUnavailableError
from app.models import SpotifyArtist, SpotifyTrack, YouTubeVideo


# --------------------------------------------------------------------------- #
# builders
# --------------------------------------------------------------------------- #
def make_track(
    track_id: str,
    name: str,
    artist: str | list[str],
    *,
    duration_s: float = 200.0,
    album: str = "Some Album",
    isrc: str | None = None,
    occurrence: int = 0,
    position: int = 0,
    is_remix: bool = False,
    is_live: bool = False,
) -> SpotifyTrack:
    names = [artist] if isinstance(artist, str) else artist
    return SpotifyTrack(
        track_id=track_id,
        name=name,
        artists=tuple(SpotifyArtist(id=None, name=n) for n in names),
        album=album,
        duration_ms=int(duration_s * 1000),
        isrc=isrc,
        is_remix=is_remix,
        is_live=is_live,
        playlist_position=position,
        occurrence=occurrence,
    )


def make_video(
    video_id: str,
    title: str,
    channel: str,
    *,
    duration_s: float | None = 200.0,
    description: str = "",
    available: bool = True,
) -> YouTubeVideo:
    return YouTubeVideo(
        video_id=video_id,
        title=title,
        channel_title=channel,
        channel_id="UC" + video_id,
        description=description,
        duration_s=duration_s,
        is_available=available,
    )


# --------------------------------------------------------------------------- #
# fake Spotify
# --------------------------------------------------------------------------- #
@dataclass
class FakeSpotifyClient:
    tracks: list[SpotifyTrack]
    snapshot_id: str = "snap-1"
    name: str = "Test Playlist"
    raise_on_tracks: Exception | None = None
    raise_on_meta: Exception | None = None
    meta_calls: int = 0
    tracks_calls: int = 0

    def get_playlist_meta(self, playlist_id: str) -> dict:
        self.meta_calls += 1
        if self.raise_on_meta:
            raise self.raise_on_meta
        return {
            "snapshot_id": self.snapshot_id,
            "name": self.name,
            "owner": "me",
            "total": len(self.tracks),
        }

    def get_playlist_tracks(self, playlist_id: str) -> tuple[str, list[SpotifyTrack]]:
        self.tracks_calls += 1
        if self.raise_on_tracks:
            raise self.raise_on_tracks
        return self.snapshot_id, list(self.tracks)


# --------------------------------------------------------------------------- #
# fake YouTube
# --------------------------------------------------------------------------- #
@dataclass
class FakeYouTubeClient:
    videos: dict[str, YouTubeVideo] = field(default_factory=dict)
    # query substring (lowercased) -> ordered candidate video ids
    search_map: dict[str, list[str]] = field(default_factory=dict)
    default_search: list[str] = field(default_factory=list)
    playlist: list[dict] = field(default_factory=list)
    playlist_missing: bool = False

    inserted: list[dict] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    moved: list[tuple[str, int]] = field(default_factory=list)
    search_calls: int = 0
    quota: object | None = None  # optional QuotaTracker, to exercise quota logic
    _ids = itertools.count(1)

    def _meter(self, method: str) -> None:
        if self.quota is not None:
            self.quota.require(method)
            self.quota.charge(method)

    # reads
    def list_playlist_items(self, playlist_id: str) -> list[dict]:
        if self.playlist_missing:
            raise PlaylistUnavailableError("no such playlist")
        self._meter("playlistItems.list")
        return [dict(x) for x in self.playlist]

    def search_videos(self, query: str, max_results: int = 8) -> list[str]:
        self.search_calls += 1
        self._meter("search.list")
        q = query.lower()
        for key, ids in self.search_map.items():
            if key in q:
                return list(ids)
        return list(self.default_search)

    def get_videos(self, video_ids: list[str]) -> dict[str, YouTubeVideo]:
        if video_ids:
            self._meter("videos.list")
        return {v: self.videos[v] for v in video_ids if v in self.videos}

    def video_exists(self, video_id: str) -> bool:
        return video_id in self.videos

    def check_playlist_writable(self, playlist_id: str) -> None:
        if self.playlist_missing:
            raise PlaylistUnavailableError("no such playlist")

    # writes
    def insert_playlist_item(self, playlist_id: str, video_id: str, position=None) -> dict:
        self._meter("playlistItems.insert")
        item_id = f"pli-{next(self._ids)}"
        pos = position if position is not None else len(self.playlist)
        video = self.videos.get(video_id)
        entry = {
            "playlist_item_id": item_id,
            "video_id": video_id,
            "position": pos,
            "title": video.title if video else "",
            "channel": video.channel_title if video else "",
            "privacy_status": "public",
        }
        self.playlist.insert(min(pos, len(self.playlist)), entry)
        self.inserted.append(entry)
        return {"playlist_item_id": item_id, "position": pos}

    def delete_playlist_item(self, playlist_item_id: str) -> None:
        self._meter("playlistItems.delete")
        self.deleted.append(playlist_item_id)
        self.playlist = [
            x for x in self.playlist if x["playlist_item_id"] != playlist_item_id
        ]

    def move_playlist_item(self, playlist_item_id, playlist_id, video_id, position) -> None:
        self._meter("playlistItems.update")
        self.moved.append((playlist_item_id, position))
