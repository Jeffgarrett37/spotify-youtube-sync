from __future__ import annotations

import pytest

from app.matcher import _duration_score, score_candidate
from app.models import MatchTier
from app.youtube_client import parse_iso8601_duration

from .fakes import make_track, make_video


@pytest.mark.parametrize(
    "text,seconds",
    [
        ("PT3M33S", 213),
        ("PT1H2M10S", 3730),
        ("PT45S", 45),
        ("PT4M", 240),
        ("P1DT1H", 90000),
        ("", 0),
        ("garbage", 0),
    ],
)
def test_parse_iso8601_duration(text, seconds):
    assert parse_iso8601_duration(text) == seconds


def test_within_grace_is_full_score():
    track = make_track("t", "x", "y", duration_s=200)
    vid = make_video("v", "x", "y", duration_s=209)
    s, delta = _duration_score(track, vid)
    assert s == 1.0
    assert delta == pytest.approx(9)


def test_far_off_is_zero_and_disqualifies():
    track = make_track("t", "Exact Title", "Real Artist", duration_s=200)
    vid = make_video("v", "Real Artist - Exact Title", "Real Artist", duration_s=320)
    s, _ = _duration_score(track, vid)
    assert s == 0.0
    result = score_candidate(track, vid, _cfg())
    assert result.tier is MatchTier.LOW


def test_unknown_duration_is_neutral():
    track = make_track("t", "x", "y", duration_s=200)
    vid = make_video("v", "x", "y", duration_s=None)
    s, delta = _duration_score(track, vid)
    assert s == 0.5
    assert delta is None


def test_longer_youtube_is_softer_than_shorter():
    track = make_track("t", "x", "y", duration_s=200)
    longer = make_video("l", "x", "y", duration_s=230)
    shorter = make_video("s", "x", "y", duration_s=170)
    assert _duration_score(track, longer)[0] >= _duration_score(track, shorter)[0]


def _cfg():
    from pathlib import Path

    from app.config import Config

    return Config(
        data_dir=Path("."), spotify_client_id="a", spotify_client_secret="b",
        spotify_playlist_id="c", youtube_client_id="d", youtube_client_secret="e",
        youtube_playlist_id="f",
    )
