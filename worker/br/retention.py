"""Retention policy evaluation.

Pure functions over a list of backup descriptors so the policy can be unit
tested without touching the filesystem:

    {backup_id, created_at (ISO8601 Z), size_bytes, type, retention_keep}

The worker maps DB rows into this shape, calls :func:`plan`, then deletes the
archives it is told to delete.  ``protected`` ids (referenced by recovery
checkpoints, or the newest N) are never returned for deletion.
"""

from __future__ import annotations

import datetime as dt
import typing as t


def parse_ts(value: str) -> dt.datetime:
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    parsed = dt.datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def plan(
    backups: t.Sequence[dict],
    *,
    keep_daily: int = 7,
    keep_weekly: int = 4,
    keep_monthly: int = 6,
    keep_total: int = 0,
    protected: t.Container[str] = frozenset(),
    now: dt.datetime | None = None,
) -> dict:
    """Return ``{"keep": [ids], "delete": [ids], "reasons": {id: str}}``.

    GFS (grandfather-father-son) logic:

    * keep the newest backup of every day for ``keep_daily`` days
    * keep the newest backup of every ISO week for ``keep_weekly`` weeks
    * keep the newest backup of every month for ``keep_monthly`` months
    * always keep ``protected`` ids
    * apply ``keep_total`` as an absolute cap afterwards (protected wins)
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    items = [dict(b) for b in backups if b.get("type") in (None, "full")]
    items.sort(key=lambda b: parse_ts(b["created_at"]), reverse=True)

    keep: set[str] = set()
    reasons: dict[str, str] = {}

    daily_seen: set[dt.date] = set()
    weekly_seen: set[tuple[int, int]] = set()
    monthly_seen: set[tuple[int, int]] = set()

    for item in items:
        created = parse_ts(item["created_at"])
        created_date = created.date()
        iso = created.isocalendar()
        week_key = (iso[0], iso[1])
        month_key = (created.year, created.month)

        # Each tier is evaluated independently against the newest-first order,
        # so exactly one backup per day/week/month is kept per window. The
        # tiers must NOT be chained with `continue`: doing so let an older
        # backup from an already-seen day fall through into the weekly tier
        # and be retained as a "weekly", which silently exceeded keep_total
        # and defeated the daily de-duplication.
        matched = False
        if len(daily_seen) < keep_daily and created_date not in daily_seen:
            daily_seen.add(created_date)
            keep.add(item["backup_id"])
            reasons[item["backup_id"]] = f"daily:{created_date.isoformat()}"
            matched = True
        if len(weekly_seen) < keep_weekly and week_key not in weekly_seen:
            weekly_seen.add(week_key)
            keep.add(item["backup_id"])
            reasons.setdefault(
                item["backup_id"], f"weekly:{week_key[0]}-W{week_key[1]:02d}"
            )
            matched = True
        if len(monthly_seen) < keep_monthly and month_key not in monthly_seen:
            monthly_seen.add(month_key)
            keep.add(item["backup_id"])
            reasons.setdefault(
                item["backup_id"], f"monthly:{created.year}-{created.month:02d}"
            )
            matched = True
        del matched

    for backup_id in protected:
        if any(b["backup_id"] == backup_id for b in items):
            keep.add(backup_id)
            reasons.setdefault(backup_id, "protected")

    delete = []
    if keep_total and len(items) > keep_total:
        # Newest first; anything beyond the cap is a deletion candidate
        # unless protected. Everything INSIDE the cap is kept, even if it
        # matched no GFS tier - otherwise such a backup would appear in
        # neither list and a caller could neither report it nor delete it.
        for item in items[:keep_total]:
            keep.add(item["backup_id"])
            reasons.setdefault(item["backup_id"], f"within_total_cap:{keep_total}")
        for item in items[keep_total:]:
            if item["backup_id"] in protected:
                keep.add(item["backup_id"])
                continue
            delete.append(item["backup_id"])
            reasons[item["backup_id"]] = f"total_cap:{keep_total}"
        keep -= set(delete)
    else:
        delete = [
            b["backup_id"] for b in items
            if b["backup_id"] not in keep and b["backup_id"] not in protected
        ]
        for backup_id in delete:
            reasons.setdefault(backup_id, "outside_retention")

    return {
        "keep": sorted(keep),
        "delete": sorted(delete),
        "reasons": reasons,
        "evaluated": len(items),
    }


def suggest_retention_class(created_at: dt.datetime, now: dt.datetime | None = None) -> str:
    """Classify a backup as daily/weekly/monthly for display purposes."""
    now = now or dt.datetime.now(dt.timezone.utc)
    age_days = (now - created_at).days
    if age_days <= 7:
        return "daily"
    if age_days <= 31:
        return "weekly"
    return "monthly"
