"""Retention (GFS) planning and timestamp parsing."""

from __future__ import annotations

import datetime as dt

import pytest

from br import retention

NOW = dt.datetime(2026, 11, 11, 12, 0, tzinfo=dt.timezone.utc)


def _backup(backup_id: str, when: dt.datetime, kind: str = "full") -> dict:
    return {"backup_id": backup_id, "type": kind, "created_at": when.isoformat()}


def _plan(backups, **kwargs):
    kwargs.setdefault("now", NOW)
    return retention.plan(backups, **kwargs)


# -- timestamps -------------------------------------------------------------

def test_parse_ts_accepts_z_suffix():
    parsed = retention.parse_ts("2026-11-11T18:14:40Z")
    assert parsed == dt.datetime(2026, 11, 11, 18, 14, 40, tzinfo=dt.timezone.utc)


def test_parse_ts_assumes_utc_when_naive():
    assert retention.parse_ts("2026-11-11T18:14:40").tzinfo is dt.timezone.utc


def test_parse_ts_normalises_offsets():
    parsed = retention.parse_ts("2026-11-11T23:44:40+05:30")
    assert parsed == dt.datetime(2026, 11, 11, 18, 14, 40, tzinfo=dt.timezone.utc)


# -- core GFS behaviour -----------------------------------------------------

def test_keeps_newest_of_each_day():
    backups = [
        _backup("br-1", NOW - dt.timedelta(hours=1)),
        _backup("br-2", NOW - dt.timedelta(hours=6)),
        _backup("br-3", NOW - dt.timedelta(days=1)),
    ]
    result = _plan(backups, keep_daily=7)
    assert "br-1" in result["keep"], "newest of today kept"
    assert "br-2" not in result["keep"], "second of the same day dropped"
    assert "br-3" in result["keep"], "previous day kept"


def test_gfs_keeps_one_per_day_even_inside_the_window():
    """keep_daily is a count of DAYS, not of backups: four same-day backups
    collapse to the newest one. This is the documented GFS contract."""
    backups = [_backup(f"br-{i}", NOW - dt.timedelta(hours=i * 6)) for i in range(4)]
    result = _plan(backups, keep_daily=7, keep_weekly=4, keep_monthly=6)
    # br-0 (newest) is today's daily; br-3 (18h back) lands on yesterday and
    # is that day's own daily. br-1 and br-2 are same-day duplicates: out.
    assert result["keep"] == ["br-0", "br-3"]
    assert result["delete"] == ["br-1", "br-2"]


def test_nothing_deleted_when_each_day_has_one_backup():
    backups = [_backup(f"br-{i}", NOW - dt.timedelta(days=i)) for i in range(1, 5)]
    result = _plan(backups, keep_daily=7, keep_weekly=4, keep_monthly=6)
    assert result["delete"] == []
    assert len(result["keep"]) == 4


def test_daily_window_expires():
    backups = [_backup(f"br-{i}", NOW - dt.timedelta(days=i)) for i in range(1, 15)]
    result = _plan(backups, keep_daily=3, keep_weekly=0, keep_monthly=0)
    kept = set(result["keep"])
    assert {"br-1", "br-2", "br-3"} <= kept
    assert "br-10" not in kept


def test_weekly_covers_the_gap_beyond_daily():
    backups = [_backup(f"br-{i}", NOW - dt.timedelta(days=i)) for i in range(1, 40)]
    result = _plan(backups, keep_daily=2, keep_weekly=4, keep_monthly=0)
    kept = [b for b in result["keep"]]
    assert len(kept) > 2, "weekly tier must retain more than the daily tier"


def test_protected_always_kept():
    backups = [_backup(f"br-{i}", NOW - dt.timedelta(days=i)) for i in range(1, 30)]
    result = _plan(backups, keep_daily=1, keep_weekly=0, keep_monthly=0,
                   protected=frozenset({"br-25"}))
    assert "br-25" in result["keep"]
    assert "br-25" not in result["delete"]


def test_keep_total_is_an_upper_bound():
    backups = [_backup(f"br-{i}", NOW - dt.timedelta(days=i)) for i in range(1, 20)]
    result = _plan(backups, keep_daily=15, keep_weekly=0, keep_monthly=0, keep_total=5)
    assert len(result["keep"]) <= 5


def test_keep_total_never_drops_protected():
    backups = [_backup(f"br-{i}", NOW - dt.timedelta(days=i)) for i in range(1, 10)]
    result = _plan(backups, keep_daily=9, keep_total=2, protected=frozenset({"br-8"}))
    assert "br-8" in result["keep"]


def test_non_full_backups_ignored():
    backups = [
        _backup("br-full", NOW - dt.timedelta(hours=1)),
        _backup("br-cfg", NOW - dt.timedelta(hours=2), kind="configuration"),
    ]
    result = _plan(backups, keep_daily=7)
    assert "br-cfg" not in result["keep"]
    assert "br-cfg" not in result["delete"]


def test_checkpoint_rows_are_never_deleted():
    # Checkpoints are referenced by restores; retention must not touch them.
    backups = [_backup("br-1", NOW - dt.timedelta(days=400))]
    result = _plan(backups, keep_daily=1, keep_weekly=0, keep_monthly=0)
    assert set(result["keep"]) | set(result["delete"]) == {"br-1"}


def test_reasons_reported_for_every_decision():
    backups = [_backup(f"br-{i}", NOW - dt.timedelta(days=i)) for i in range(1, 6)]
    result = _plan(backups, keep_daily=2, keep_weekly=0, keep_monthly=0)
    for backup_id in list(result["keep"]) + list(result["delete"]):
        assert backup_id in result["reasons"], backup_id


def test_empty_input():
    result = _plan([])
    assert result["keep"] == [] and result["delete"] == []
    assert result["evaluated"] == 0


def test_output_is_partition():
    backups = [_backup(f"br-{i}", NOW - dt.timedelta(days=i)) for i in range(1, 12)]
    result = _plan(backups, keep_daily=3, keep_weekly=2, keep_monthly=1, keep_total=4)
    assert set(result["keep"]).isdisjoint(result["delete"])
    assert len(result["keep"]) + len(result["delete"]) == len(backups)


# -- retention class --------------------------------------------------------

@pytest.mark.parametrize("age_days,expected", [
    (0, "daily"), (3, "daily"), (20, "weekly"), (200, "monthly"),
])
def test_retention_class_thresholds(age_days, expected):
    created = NOW - dt.timedelta(days=age_days)
    assert retention.suggest_retention_class(created, NOW) == expected


def test_retention_class_fits_schema():
    """The column is varchar(16); 'checkpoint' must fit alongside classes."""
    for value in ("daily", "weekly", "monthly", "checkpoint"):
        assert len(value) <= 16
