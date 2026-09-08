"""Score YouTube candidates against a Spotify track and pick a safe match.

Design bias: **prefer missing a song over inserting the wrong one.** A
candidate must clear artist, title and duration floors *and* a weighted score
threshold before it can be auto-added. Anything in between is recorded as
"review required" rather than guessed.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import (
    DURATION_GRACE_SECONDS,
    DURATION_HARD_LIMIT_SECONDS,
    Config,
)
from .models import MatchResult, MatchTier, ScoredCandidate, SpotifyTrack, YouTubeVideo
from .normalize import (
    artist_similarity,
    contains_any,
    normalize_artist,
    normalize_text,
    normalize_title,
    similarity,
    split_artists,
    title_core,
    token_set_ratio,
)

# Component weights for the base score (sum to 1.0).
W_TITLE = 0.42
W_ARTIST = 0.34
W_DURATION = 0.24

# Acceptance floors — independent of the weighted score.
ARTIST_FLOOR_REJECT = 0.45   # below this: disqualified (wrong performer)
ARTIST_FLOOR_HIGH = 0.60     # below this: cannot be HIGH confidence
TITLE_FLOOR_REJECT = 0.50

# Phrase groups.
_DISQUALIFY_PHRASES = [
    "karaoke", "reaction", "reacts to", "reacts", "review", "reviewed",
    "how to play", "guitar lesson", "piano tutorial", "drum tutorial",
    "tutorial", "cover by", "cover version", "in the style of",
    "made famous by", "tribute to", "full album", "greatest hits",
    "mixtape", "type beat", "1 hour", "1hour", "10 hours", "loop",
    "sleep music", "study music",
]
_COVER_PHRASES = ["cover", "acoustic cover", "vocal cover", "piano cover"]
_KARAOKE_PHRASES = ["karaoke", "instrumental version", "backing track", "sing along", "singalong"]
_LIVE_PHRASES = ["live at", "live from", "live in", "(live)", "[live]", " live ", "live performance", "tiny desk", "concert", "unplugged"]
_REMIX_PHRASES = ["remix", "bootleg", "flip", "vip mix", "rework", "re-work", "re work"]
_EDIT_PHRASES = ["extended mix", "extended version", "radio edit"]
_MANIP_PHRASES = ["slowed", "reverb", "sped up", "speed up", "nightcore", "8d audio", "bass boosted", "daycore", "pitched"]
_LYRIC_PHRASES = ["lyric video", "lyrics video", "with lyrics", "(lyrics)", "[lyrics]"]
_OFFICIAL_AUDIO_PHRASES = ["official audio", "official hd audio", "audio"]
_OFFICIAL_VIDEO_PHRASES = ["official video", "official music video", "official m/v", "official mv"]


@dataclass
class _Signals:
    title_sim: float
    artist_sim: float
    duration_delta: float | None
    duration_score: float


def _yt_title_variants(video: YouTubeVideo) -> list[str]:
    """Return plausible 'song title' substrings from a YouTube video title."""
    raw = video.title
    variants = [raw]
    if " - " in raw:
        # "Artist - Title (Official Video)" -> "Title (Official Video)"
        variants.append(raw.split(" - ", 1)[1])
        # also the reversed layout "Title - Artist"
        variants.append(raw.rsplit(" - ", 1)[0])
    if "|" in raw:
        variants.extend(part.strip() for part in raw.split("|"))
    return [v for v in variants if v.strip()]


def _title_similarity(track: SpotifyTrack, video: YouTubeVideo) -> float:
    sp_full = normalize_title(track.name)
    sp_core = title_core(track.name)
    best = 0.0
    for variant in _yt_title_variants(video):
        yt_full = normalize_title(variant)
        yt_core = title_core(variant)
        best = max(
            best,
            similarity(sp_full, yt_full),
            token_set_ratio(sp_full, yt_full),
            similarity(sp_core, yt_core),
            token_set_ratio(sp_core, yt_core),
        )
        # Containment: Spotify title fully inside the YouTube title is strong.
        if sp_core and sp_core in yt_full:
            best = max(best, 0.93)
    return best


def _artist_similarity(track: SpotifyTrack, video: YouTubeVideo) -> float:
    sp_artists = [a for a in track.all_artist_names if a]
    haystacks = [video.channel_title or ""]
    if " - " in video.title:
        haystacks.append(video.title.split(" - ", 1)[0])
    haystacks.append(video.title)
    best = 0.0
    for h in haystacks:
        best = max(best, artist_similarity(sp_artists, h))
    return best


def _duration_score(track: SpotifyTrack, video: YouTubeVideo) -> tuple[float, float | None]:
    if not video.duration_s or video.duration_s <= 0:
        # Unknown duration: neutral-ish, cannot help but should not fully sink.
        return 0.5, None
    delta = video.duration_s - track.duration_s  # +ve => YouTube is longer
    adelta = abs(delta)
    if adelta <= DURATION_GRACE_SECONDS:
        return 1.0, delta
    if adelta >= DURATION_HARD_LIMIT_SECONDS:
        return 0.0, delta
    # Linear decay between grace and hard limit; a longer YouTube video (music
    # video intro/outro) is penalised a little less than a shorter one.
    span = DURATION_HARD_LIMIT_SECONDS - DURATION_GRACE_SECONDS
    over = adelta - DURATION_GRACE_SECONDS
    decayed = 1.0 - (over / span)
    if delta > 0:
        decayed = min(1.0, decayed + 0.15)
    return max(0.0, decayed), delta


def _channel_is_topic(video: YouTubeVideo) -> bool:
    return video.channel_title.strip().lower().endswith("- topic")


def _channel_is_official_artist(track: SpotifyTrack, video: YouTubeVideo) -> bool:
    chan = normalize_artist(video.channel_title)
    if not chan:
        return False
    if "vevo" in video.channel_title.lower():
        base = normalize_artist(video.channel_title.lower().replace("vevo", ""))
        for a in track.all_artist_names:
            if normalize_artist(a) and normalize_artist(a) in base:
                return True
    for a in track.all_artist_names:
        na = normalize_artist(a)
        if na and (na == chan or na in chan.split()):
            return True
        if na and na in chan and abs(len(na) - len(chan)) <= 6:
            return True
    return False


def score_candidate(
    track: SpotifyTrack, video: YouTubeVideo, config: Config
) -> ScoredCandidate:
    reasons: list[str] = []
    penalties: list[str] = []

    title_sim = _title_similarity(track, video)
    artist_sim = _artist_similarity(track, video)
    duration_score, delta = _duration_score(track, video)

    hay = normalize_text(f"{video.title} {video.channel_title} {video.description[:400]}")
    raw_hay = f"{video.title} {video.channel_title} {video.description[:400]}".lower()

    disqualified: str | None = None

    # --- hard floors -------------------------------------------------------- #
    if title_sim < TITLE_FLOOR_REJECT:
        disqualified = f"title mismatch ({title_sim:.2f})"
    if artist_sim < ARTIST_FLOOR_REJECT:
        disqualified = disqualified or f"artist mismatch ({artist_sim:.2f})"
    if delta is not None and abs(delta) >= DURATION_HARD_LIMIT_SECONDS:
        disqualified = disqualified or f"duration off by {abs(delta):.0f}s"

    for phrase in _DISQUALIFY_PHRASES:
        if phrase in raw_hay:
            # a couple of these are legitimate if the Spotify track itself says so
            if phrase in ("live", "loop") and (track.is_live):
                continue
            disqualified = disqualified or f"contains '{phrase}'"
            break

    # --- weighted base ---------------------------------------------------- #
    base = W_TITLE * title_sim + W_ARTIST * artist_sim + W_DURATION * duration_score

    if title_sim >= 0.92:
        reasons.append("title~exact")
    if artist_sim >= 0.9:
        reasons.append("artist~exact")
    if duration_score >= 1.0:
        reasons.append("duration~exact")
    elif delta is not None:
        reasons.append(f"dur{delta:+.0f}s")

    bonus = 0.0
    if contains_any(raw_hay, _OFFICIAL_AUDIO_PHRASES):
        if "official audio" in raw_hay:
            bonus += 0.08
            reasons.append("official-audio")
    if contains_any(raw_hay, _OFFICIAL_VIDEO_PHRASES):
        bonus += 0.05
        reasons.append("official-video")
        if not track.is_live and delta is not None and delta > 0:
            # music videos legitimately run long; soften duration hit
            bonus += 0.02
    if _channel_is_topic(video):
        bonus += 0.11
        reasons.append("topic-channel")
    if _channel_is_official_artist(track, video):
        bonus += 0.10
        reasons.append("artist-channel")
    if track.album and normalize_text(track.album) and normalize_text(track.album) in hay:
        bonus += 0.04
        reasons.append("album-match")
    if track.isrc and track.isrc.lower() in raw_hay:
        bonus += 0.05
        reasons.append("isrc-in-desc")

    # --- penalties ------------------------------------------------------- #
    pen = 0.0
    if not track.is_remix and contains_any(raw_hay, _REMIX_PHRASES):
        pen += 0.30
        penalties.append("remix")
    if not track.is_live and contains_any(raw_hay, _LIVE_PHRASES):
        pen += 0.25
        penalties.append("live")
    if contains_any(raw_hay, _MANIP_PHRASES):
        pen += 0.50
        penalties.append("slowed/spedup")
    if contains_any(raw_hay, _COVER_PHRASES):
        pen += 0.40
        penalties.append("cover")
    if contains_any(raw_hay, _KARAOKE_PHRASES):
        pen += 0.55
        penalties.append("karaoke/instrumental")
    if contains_any(raw_hay, _EDIT_PHRASES) and not track.is_remix:
        pen += 0.12
        penalties.append("alt-edit")
    if contains_any(raw_hay, _LYRIC_PHRASES):
        pen += 0.03
        penalties.append("lyric-video")
    if artist_sim < ARTIST_FLOOR_HIGH:
        pen += 0.15
        penalties.append(f"weak-artist({artist_sim:.2f})")

    score = max(0.0, min(1.0, base + bonus - pen))

    # --- tier ----------------------------------------------------------- #
    if disqualified:
        score = min(score, 0.30)
        penalties.append(disqualified)
        tier = MatchTier.LOW
    elif (
        score >= config.auto_add_threshold
        and artist_sim >= ARTIST_FLOOR_HIGH
        and title_sim >= 0.70
        and duration_score > 0.0
    ):
        tier = MatchTier.HIGH
    elif score >= config.medium_threshold:
        tier = MatchTier.MEDIUM
    else:
        tier = MatchTier.LOW

    return ScoredCandidate(
        video=video, score=round(score, 4), tier=tier, reasons=reasons, penalties=penalties
    )


def match_track(
    track: SpotifyTrack, candidates: list[YouTubeVideo], config: Config
) -> MatchResult:
    scored = [score_candidate(track, v, config) for v in candidates]
    scored.sort(key=lambda c: c.score, reverse=True)

    high = [c for c in scored if c.tier is MatchTier.HIGH]
    if high:
        best = high[0]
        runner_up = high[1].score if len(high) > 1 else None
        note = f"auto-added at {best.score:.2f}"
        if runner_up is not None and best.score - runner_up < 0.03:
            note += f" (close runner-up {runner_up:.2f})"
        return MatchResult(
            track=track,
            accepted=best,
            best_rejected=scored[1] if len(scored) > 1 else None,
            tier=MatchTier.HIGH,
            searched=True,
            note=note,
        )

    best_any = scored[0] if scored else None
    if best_any and best_any.tier is MatchTier.MEDIUM:
        note = (
            f"best candidate {best_any.video.video_id} scored {best_any.score:.2f} "
            f"(needs >= {config.auto_add_threshold:.2f}); "
            + best_any.summary()
        )
        tier = MatchTier.MEDIUM
    elif best_any:
        note = (
            f"no acceptable candidate; best {best_any.video.video_id} "
            f"scored {best_any.score:.2f}: " + best_any.summary()
        )
        tier = MatchTier.LOW
    else:
        note = "YouTube search returned no candidates"
        tier = MatchTier.LOW

    return MatchResult(
        track=track,
        accepted=None,
        best_rejected=best_any,
        tier=tier,
        searched=True,
        note=note,
    )
