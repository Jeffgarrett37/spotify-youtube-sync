"""Resolve a single Spotify track to a YouTube video id.

Order of precedence (this is where quota is saved):

1. **Manual override** - always wins, never triggers a search.
2. **Cached mapping** - a previously accepted match is reused as-is; no search.
3. **Sticky "unmatched"** - if we already failed recently and the track's
   metadata has not changed, do not search again until the retry window passes.
4. **Search** - only now do we spend the 100-unit ``search.list`` quota, and
   only if the daily budget can afford it.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC

from .config import YT_QUOTA_COST, Config
from .database import Database
from .errors import QuotaExceededError
from .logging_setup import get_logger
from .matcher import match_track
from .models import MatchResult, MatchTier, SpotifyTrack, YouTubeVideo
from .normalize import normalize_artist, normalize_text, normalize_title
from .quota import QuotaTracker
from .youtube_client import YouTubeClient

log = get_logger("resolver")

MAX_SEARCHES_PER_TRACK = 2
CANDIDATES_PER_SEARCH = 8

# Only start a search if we can also afford to hydrate candidates and insert the
# winner - otherwise the search quota is wasted and the add happens anyway next
# run from the cache. Keeps a big initial migration progressing every day.
_SEARCH_PLUS_ADD_COST = (
    YT_QUOTA_COST["search.list"]
    + YT_QUOTA_COST["videos.list"]
    + YT_QUOTA_COST["playlistItems.insert"]
)


def track_fingerprint(track: SpotifyTrack) -> str:
    basis = "|".join(
        [
            normalize_title(track.name),
            ",".join(sorted(normalize_artist(a) for a in track.all_artist_names)),
            str(round(track.duration_s / 3)),  # 3s buckets
            normalize_text(track.album),
        ]
    )
    return hashlib.sha1(basis.encode()).hexdigest()[:16]


def build_queries(track: SpotifyTrack) -> list[str]:
    artist = track.primary_artist
    title = track.name
    all_artists = " ".join(track.all_artist_names[:2])
    queries = [
        f"{artist} {title} official audio",
        f"{all_artists} {title}",
        f"{artist} {title} audio",
        f"{artist} {title} official video",
    ]
    # De-dup while preserving order.
    seen: list[str] = []
    for q in queries:
        q = " ".join(q.split())
        if q and q.lower() not in {s.lower() for s in seen}:
            seen.append(q)
    return seen


@dataclass
class ResolveOutcome:
    track: SpotifyTrack
    video: YouTubeVideo | None
    tier: MatchTier
    score: float
    source: str            # override | cache | search | none
    searched: bool
    note: str = ""
    quota_blocked: bool = False
    match_result: MatchResult | None = None
    yt_title: str = ""
    yt_channel: str = ""

    @property
    def video_id(self) -> str | None:
        if self.video:
            return self.video.video_id
        return None


class TrackResolver:
    def __init__(
        self,
        db: Database,
        youtube: YouTubeClient,
        quota: QuotaTracker,
        config: Config,
        persist: bool = True,
    ) -> None:
        self.db = db
        self.yt = youtube
        self.quota = quota
        self.config = config
        # When False (dry-run) the resolver computes matches but writes nothing
        # to the mapping cache / unmatched store.
        self.persist = persist

    # ------------------------------------------------------------------ #
    def resolve(self, track: SpotifyTrack) -> ResolveOutcome:
        override_id = self.db.get_override(track.track_id)
        if override_id:
            return self._from_override(track, override_id)

        cached = self.db.get_mapping(track.track_id)
        if cached and cached["youtube_video_id"] and cached["source"] != "stale":
            return ResolveOutcome(
                track=track,
                video=YouTubeVideo(
                    video_id=cached["youtube_video_id"],
                    title=cached["youtube_title"] or "",
                    channel_title=cached["youtube_channel"] or "",
                    channel_id=None,
                    description="",
                    duration_s=None,
                ),
                tier=MatchTier.CACHED,
                score=float(cached["match_score"] or 0.0),
                source="cache",
                searched=False,
                yt_title=cached["youtube_title"] or "",
                yt_channel=cached["youtube_channel"] or "",
                note="reused cached mapping",
            )

        if self._unmatched_is_sticky(track):
            row = self.db.get_unmatched(track.track_id)
            return ResolveOutcome(
                track=track,
                video=None,
                tier=MatchTier(row["tier"]) if row and row["tier"] in MatchTier._value2member_map_ else MatchTier.LOW,
                score=float(row["best_score"] or 0.0) if row else 0.0,
                source="none",
                searched=False,
                note=f"still unmatched (retry after {self.config.unmatched_retry_hours}h)",
            )

        return self._search_and_match(track)

    # ------------------------------------------------------------------ #
    def _from_override(self, track: SpotifyTrack, video_id: str) -> ResolveOutcome:
        video: YouTubeVideo | None = None
        if self.quota.can_afford("videos.list"):
            try:
                video = self.yt.get_videos([video_id]).get(video_id)
            except QuotaExceededError:
                video = None
        if video is None:
            video = YouTubeVideo(
                video_id=video_id,
                title="(override)",
                channel_title="",
                channel_id=None,
                description="",
                duration_s=None,
            )
        if self.persist:
            self.db.upsert_mapping(
                track, video_id, video.title, video.channel_title, 1.0,
                "override", "override",
            )
            self.db.clear_unmatched(track.track_id)
        return ResolveOutcome(
            track=track,
            video=video,
            tier=MatchTier.OVERRIDE,
            score=1.0,
            source="override",
            searched=False,
            yt_title=video.title,
            yt_channel=video.channel_title,
            note=f"manual override -> {video_id}",
        )

    def _unmatched_is_sticky(self, track: SpotifyTrack) -> bool:
        row = self.db.get_unmatched(track.track_id)
        if not row:
            return False
        if row["metadata_fingerprint"] != track_fingerprint(track):
            return False  # metadata changed -> allow a fresh attempt
        from datetime import datetime

        try:
            last = datetime.fromisoformat(row["last_attempt_at"])
        except ValueError:
            return False
        if last.tzinfo is None:
            last = last.replace(tzinfo=UTC)
        age_h = (datetime.now(UTC) - last).total_seconds() / 3600
        return age_h < self.config.unmatched_retry_hours

    def _search_and_match(self, track: SpotifyTrack) -> ResolveOutcome:
        if self.quota.remaining < _SEARCH_PLUS_ADD_COST:
            return ResolveOutcome(
                track=track,
                video=None,
                tier=MatchTier.LOW,
                score=0.0,
                source="none",
                searched=False,
                quota_blocked=True,
                note=(
                    "search skipped: not enough YouTube quota left today to "
                    "search and add this track (will retry next sync)"
                ),
            )

        queries = build_queries(track)[:MAX_SEARCHES_PER_TRACK]
        seen_ids: list[str] = []
        result: MatchResult | None = None
        for i, query in enumerate(queries):
            if not self.quota.can_afford("search.list"):
                break
            try:
                ids = self.yt.search_videos(query, CANDIDATES_PER_SEARCH)
            except QuotaExceededError:
                if result is None:
                    return ResolveOutcome(
                        track=track, video=None, tier=MatchTier.LOW, score=0.0,
                        source="none", searched=True, quota_blocked=True,
                        note="quota exhausted mid-search",
                    )
                break
            for vid in ids:
                if vid not in seen_ids:
                    seen_ids.append(vid)
            if not seen_ids:
                continue
            videos = list(self.yt.get_videos(seen_ids).values())
            result = match_track(track, videos, self.config)
            log.debug(
                "query %d/%d %r -> %d candidates, best tier %s",
                i + 1, len(queries), query, len(videos), result.tier.value,
            )
            if result.accepted is not None:
                break
            # A second search only helps if we are close but not there.
            if result.tier is not MatchTier.MEDIUM:
                break

        if result is None:
            result = MatchResult(
                track=track, accepted=None, best_rejected=None, tier=MatchTier.LOW,
                searched=True, note="no YouTube candidates found",
            )

        if result.accepted is not None:
            cand = result.accepted
            if self.persist:
                self.db.upsert_mapping(
                    track, cand.video.video_id, cand.video.title,
                    cand.video.channel_title, cand.score, cand.tier.value, "auto",
                )
                self.db.clear_unmatched(track.track_id)
            return ResolveOutcome(
                track=track, video=cand.video, tier=MatchTier.HIGH,
                score=cand.score, source="search", searched=True,
                yt_title=cand.video.title, yt_channel=cand.video.channel_title,
                note=result.note, match_result=result,
            )

        best = result.best_rejected
        if self.persist:
            self.db.upsert_unmatched(
                spotify_track_id=track.track_id,
                title=track.name,
                artist=track.primary_artist,
                reason=result.note,
                best_candidate_id=best.video.video_id if best else None,
                best_candidate_title=best.video.title if best else None,
                best_score=best.score if best else None,
                tier=result.tier.value,
                fingerprint=track_fingerprint(track),
            )
        return ResolveOutcome(
            track=track, video=None, tier=result.tier,
            score=best.score if best else 0.0, source="none", searched=True,
            note=result.note, match_result=result,
        )
