"""Plain data structures passed between the clients, matcher and sync engine."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field


class MatchTier(enum.Enum):
    HIGH = "high"        # auto-add
    MEDIUM = "medium"    # review required, do not auto-add
    LOW = "low"          # reject
    OVERRIDE = "override"  # forced by a manual mapping
    CACHED = "cached"    # reused from a previously accepted mapping


@dataclass(frozen=True)
class SpotifyArtist:
    id: str | None
    name: str


@dataclass(frozen=True)
class SpotifyTrack:
    """A single track *occurrence* in the source playlist.

    ``playlist_position`` and ``occurrence`` together identify a specific slot,
    which is what lets us handle a playlist that contains the same track twice.
    """

    track_id: str
    name: str
    artists: tuple[SpotifyArtist, ...]
    album: str
    duration_ms: int
    isrc: str | None
    explicit: bool = False
    is_remix: bool = False
    is_live: bool = False
    playlist_position: int = 0
    occurrence: int = 0  # 0-based index among duplicates of this track_id
    added_at: str | None = None

    @property
    def primary_artist(self) -> str:
        return self.artists[0].name if self.artists else ""

    @property
    def all_artist_names(self) -> tuple[str, ...]:
        return tuple(a.name for a in self.artists)

    @property
    def duration_s(self) -> float:
        return self.duration_ms / 1000.0

    @property
    def key(self) -> tuple[str, int]:
        """Stable identity of this occurrence within a desired-state list."""
        return (self.track_id, self.occurrence)


@dataclass(frozen=True)
class YouTubeVideo:
    video_id: str
    title: str
    channel_title: str
    channel_id: str | None
    description: str
    duration_s: float | None
    published_at: str | None = None
    is_available: bool = True

    @property
    def url(self) -> str:
        return f"https://www.youtube.com/watch?v={self.video_id}"


@dataclass
class ScoredCandidate:
    video: YouTubeVideo
    score: float
    tier: MatchTier
    reasons: list[str] = field(default_factory=list)
    penalties: list[str] = field(default_factory=list)

    def summary(self) -> str:
        bits = [f"{self.score:.2f}"]
        if self.reasons:
            bits.append("+[" + ", ".join(self.reasons) + "]")
        if self.penalties:
            bits.append("-[" + ", ".join(self.penalties) + "]")
        return " ".join(bits)


@dataclass
class MatchResult:
    """Outcome of resolving one Spotify track to a YouTube video."""

    track: SpotifyTrack
    accepted: ScoredCandidate | None
    best_rejected: ScoredCandidate | None
    tier: MatchTier
    searched: bool
    # Human-readable explanation, used for logs and the unmatched store.
    note: str = ""

    @property
    def video_id(self) -> str | None:
        return self.accepted.video.video_id if self.accepted else None


class PlanAction(enum.Enum):
    ADD = "add"
    REMOVE = "remove"
    REORDER = "reorder"


@dataclass
class PlannedAdd:
    track: SpotifyTrack
    video: YouTubeVideo
    score: float
    tier: MatchTier
    target_position: int


@dataclass
class PlannedRemove:
    spotify_track_id: str
    occurrence: int
    youtube_video_id: str
    playlist_item_id: str
    reason: str


@dataclass
class PlannedReorder:
    playlist_item_id: str
    youtube_video_id: str
    from_position: int
    to_position: int


@dataclass
class SyncPlan:
    adds: list[PlannedAdd] = field(default_factory=list)
    removes: list[PlannedRemove] = field(default_factory=list)
    reorders: list[PlannedReorder] = field(default_factory=list)

    # Counters for the summary log line.
    spotify_tracks: int = 0
    managed_before: int = 0
    unchanged: int = 0
    unmatched: list[MatchResult] = field(default_factory=list)
    skipped_non_tracks: int = 0

    # Safety flags.
    source_validated: bool = False
    large_removal_blocked: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def is_noop(self) -> bool:
        return not self.adds and not self.removes and not self.reorders

    def describe(self) -> str:
        lines = [
            "Sync plan:",
            f"  Spotify tracks (playable): {self.spotify_tracks}",
            f"  YouTube managed items before: {self.managed_before}",
            f"  Unchanged: {self.unchanged}",
            f"  To add: {len(self.adds)}",
            f"  To remove: {len(self.removes)}",
            f"  To reorder: {len(self.reorders)}",
            f"  Unmatched / review: {len(self.unmatched)}",
        ]
        if self.skipped_non_tracks:
            lines.append(f"  Skipped (episodes/local files): {self.skipped_non_tracks}")
        for a in self.adds:
            lines.append(
                f"    + {a.track.primary_artist} - {a.track.name}  ->  "
                f"{a.video.video_id} [{a.video.title}] ({a.score:.2f})"
            )
        for r in self.removes:
            lines.append(
                f"    - {r.youtube_video_id} (was Spotify {r.spotify_track_id}"
                f"#{r.occurrence}) : {r.reason}"
            )
        for ro in self.reorders:
            lines.append(
                f"    ~ {ro.youtube_video_id}: pos {ro.from_position} -> {ro.to_position}"
            )
        for note in self.notes:
            lines.append(f"  note: {note}")
        return "\n".join(lines)


@dataclass
class SyncOutcome:
    status: str  # success | partial | failed | skipped | dry_run
    started_at: str
    finished_at: str
    spotify_tracks: int = 0
    managed_before: int = 0
    unchanged: int = 0
    added: int = 0
    removed: int = 0
    reordered: int = 0
    unmatched: int = 0
    quota_spent: int = 0
    errors: list[str] = field(default_factory=list)
    message: str = ""
