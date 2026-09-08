from __future__ import annotations

import pytest

from app.errors import QuotaExceededError
from app.quota import QuotaTracker, pacific_day


def test_pacific_day_format():
    assert len(pacific_day()) == 10


def test_budget_enforced(db):
    qt = QuotaTracker(db, daily_budget=250)
    assert qt.can_afford("search.list") is True   # 100
    qt.spend("search.list")
    qt.spend("videos.list")                       # 1
    assert qt.spent_today == 101
    assert qt.can_afford("search.list") is True   # 201 <= 250
    qt.spend("search.list")
    assert qt.spent_today == 201
    assert qt.can_afford("search.list") is False  # would be 301
    with pytest.raises(QuotaExceededError):
        qt.require("search.list")


def test_charge_persists_across_instances(db):
    QuotaTracker(db, 9000).spend("playlistItems.insert")  # 50
    assert QuotaTracker(db, 9000).spent_today == 50


def test_session_spent_tracks_only_this_run(db):
    db.add_quota(pacific_day(), 500)  # "yesterday's" leftover in the ledger
    qt = QuotaTracker(db, 9000)
    qt.spend("videos.list")
    assert qt.session_spent == 1
    assert qt.spent_today == 501
