from __future__ import annotations

import pytest

from app.normalize import (
    artist_similarity,
    normalize_artist,
    normalize_text,
    normalize_title,
    split_artists,
    title_core,
    token_set_ratio,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Beyoncé", "beyonce"),
        ("P!nk", "p nk"),
        ("AC/DC", "ac dc"),
        ("Sigur Rós", "sigur ros"),
        ("  Multiple   Spaces ", "multiple spaces"),
        ("Beyoncé & Jay-Z", "beyonce and jay z"),
    ],
)
def test_normalize_text(raw, expected):
    assert normalize_text(raw) == expected


@pytest.mark.parametrize(
    "a,b",
    [
        ("Tennessee Whiskey", "Tennessee Whiskey (Official Audio)"),
        ("Blinding Lights", "Blinding Lights (Official Video)"),
        ("Song Title", "Song Title [Official Music Video]"),
        ("Levitating", "Levitating (feat. DaBaby)"),
        ("Shape of You", "Shape of You - Official Video"),
        ("bad guy", "Billie Eilish - bad guy (Official Music Video)".split(" - ", 1)[1]),
    ],
)
def test_normalize_title_strips_decoration(a, b):
    assert normalize_title(a) == normalize_title(b)


def test_normalize_title_keeps_meaningful_tags():
    assert "live" in normalize_title("Song (Live at Wembley)")
    assert "remix" in normalize_title("Song (Kaskade Remix)")


def test_title_core_drops_all_parens():
    assert title_core("Bohemian Rhapsody (Remastered 2011)") == "bohemian rhapsody"
    assert title_core("Dreams (2004 Remaster)") == "dreams"


@pytest.mark.parametrize(
    "raw,expected_first",
    [
        ("Tyler, The Creator", "tyler"),  # comma-in-name is a known limitation
        ("Simon & Garfunkel", "simon"),
        ("Calvin Harris feat. Rihanna", "calvin harris"),
        ("Jay-Z", "jay z"),
    ],
)
def test_split_artists(raw, expected_first):
    parts = split_artists(raw)
    assert parts
    assert parts[0] == expected_first


def test_normalize_artist_removes_topic_and_vevo():
    assert normalize_artist("Taylor Swift - Topic") == "taylor swift"
    assert normalize_artist("EminemVEVO") == "eminem"


def test_artist_similarity_matches_channel():
    assert artist_similarity(["Dua Lipa"], "Dua Lipa") > 0.95
    assert artist_similarity(["Dua Lipa"], "DuaLipaVEVO") > 0.8
    assert artist_similarity(["Dua Lipa"], "Some Random Cover Channel") < 0.5


def test_token_set_ratio_order_independent():
    assert token_set_ratio("hello dark world", "world hello dark") == pytest.approx(1.0)
