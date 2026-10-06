"""Checks. Every check is a plain sync function returning a dict.

Checks may include an `events` list: notable things with a timestamp, which
diagnose() merges into one cross-system timeline.
"""

from __future__ import annotations

import datetime as dt


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def to_utc(ts: dt.datetime | float | None) -> dt.datetime | None:
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        return dt.datetime.fromtimestamp(ts, dt.timezone.utc)
    if ts.tzinfo is None:
        ts = ts.astimezone()  # naive = local time
    return ts.astimezone(dt.timezone.utc)


def event(ts, source: str, kind: str, detail: str, severity: str = "info", **extra) -> dict:
    """A timeline event. `kind` is a stable keyword used by the cause → effect rules."""
    return {"ts": to_utc(ts) or utcnow(), "source": source, "kind": kind,
            "severity": severity, "detail": detail, **extra}
