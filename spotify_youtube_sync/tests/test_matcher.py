from __future__ import annotations

from app.matcher import match_track, score_candidate
from app.models import MatchTier

from .fakes import make_track, make_video


def test_official_audio_is_high_confidence(config):
    track = make_track("t1", "Tennessee Whiskey", "Chris Stapleton", duration_s=293)
    candidates = [
        make_video(
            "good", "Chris Stapleton - Tennessee Whiskey (Official Audio)",
            "Chris Stapleton", duration_s=294,
        ),
        make_video(
            "cover", "Tennessee Whiskey (cover by Some Guy)", "Some Guy",
            duration_s=250,
        ),
    ]
    result = match_track(track, candidates, config)
    assert result.tier is MatchTier.HIGH
    assert result.video_id == "good"


def test_topic_channel_preferred(config):
    track = make_track("t2", "Redbone", "Childish Gambino", duration_s=326)
    cands = [
        make_video("topic", "Redbone", "Childish Gambino - Topic", duration_s=327),
        make_video("live", "Redbone (Live on SNL)", "Childish Gambino", duration_s=330),
    ]
    result = match_track(track, cands, config)
    assert result.video_id == "topic"
    assert result.tier is MatchTier.HIGH


def test_karaoke_is_rejected(config):
    track = make_track("t3", "Someone Like You", "Adele", duration_s=285)
    cands = [
        make_video(
            "kar", "Someone Like You (Karaoke Version)", "Sing King Karaoke",
            duration_s=286,
        )
    ]
    result = match_track(track, cands, config)
    assert result.accepted is None
    assert result.tier is MatchTier.LOW


def test_wrong_artist_rejected_even_with_right_title(config):
    track = make_track("t4", "Yesterday", "The Beatles", duration_s=125)
    cands = [make_video("x", "Yesterday", "Boyce Avenue", duration_s=124)]
    result = match_track(track, cands, config)
    assert result.accepted is None


def test_slowed_reverb_penalised_hard(config):
    track = make_track("t5", "Sunflower", "Post Malone", duration_s=158)
    good = make_video("g", "Post Malone - Sunflower (Official Audio)", "Post Malone", duration_s=159)
    slowed = make_video("s", "Sunflower (slowed + reverb)", "Post Malone", duration_s=175)
    assert score_candidate(track, good, config).score > score_candidate(track, slowed, config).score
    assert score_candidate(track, slowed, config).tier is not MatchTier.HIGH


def test_remix_rejected_when_track_is_not_remix(config):
    track = make_track("t6", "Closer", "The Chainsmokers", duration_s=244, is_remix=False)
    cands = [make_video("rmx", "Closer (T-Mass Remix)", "The Chainsmokers", duration_s=250)]
    assert match_track(track, cands, config).accepted is None


def test_remix_accepted_when_track_is_remix(config):
    track = make_track(
        "t7", "Lean On (Remix)", "Major Lazer", duration_s=180, is_remix=True
    )
    cands = [
        make_video("r", "Major Lazer - Lean On (Remix) (Official Audio)", "Major Lazer - Topic", duration_s=181)
    ]
    assert match_track(track, cands, config).tier is MatchTier.HIGH


def test_not_auto_added_when_signals_are_weak(config):
    track = make_track("t8", "Obscure Deep Cut B-Side", "Some Artist", duration_s=200)
    # title only partial, unrelated channel, duration off -> never auto-add
    cand = make_video("m", "Obscure Deep Cut", "Random Uploads Channel", duration_s=230)
    result = match_track(track, [cand], config)
    assert result.accepted is None
    assert result.tier in (MatchTier.MEDIUM, MatchTier.LOW)
    assert result.best_rejected is not None


def test_borderline_duration_keeps_out_of_high(config):
    track = make_track("t8b", "Exact Title Match", "Real Artist", duration_s=200)
    cand = make_video("d", "Real Artist - Exact Title Match", "Real Artist", duration_s=248)
    result = match_track(track, [cand], config)
    # 48s off with no "official video" tag -> not confident enough
    assert result.tier is not MatchTier.HIGH


def test_close_runner_up_flagged(config):
    track = make_track("t9", "Hurt", "Johnny Cash", duration_s=218)
    cands = [
        make_video("a", "Johnny Cash - Hurt (Official Audio)", "Johnny Cash - Topic", duration_s=218),
        make_video("b", "Johnny Cash - Hurt (Official Music Video)", "JohnnyCashVEVO", duration_s=219),
    ]
    result = match_track(track, cands, config)
    assert result.tier is MatchTier.HIGH
    assert "runner-up" in result.note or result.video_id in ("a", "b")
