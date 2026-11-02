"""Honest sample-window and freshness labels for source metadata.

A dataset update or retrieval time never establishes when its matches occurred.
This module formats only supplied metadata and performs no clock or network work.
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, timezone
import math
import re


_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_TIMESTAMP = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}"
    r"(?::[0-9]{2}(?:\.[0-9]{1,6})?)?"
    r"(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])"
)


def _utc(value):
    """Return comparable UTC time and a minute-precision display label."""
    value = value.astimezone(timezone.utc)
    return value, f"{value.date().isoformat()} {value:%H:%M} UTC"


def _iso(value):
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        if _DATE.fullmatch(value):
            parsed = date.fromisoformat(value)
            return parsed, parsed.isoformat()
        if _TIMESTAMP.fullmatch(value):
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is not None and parsed.utcoffset() is not None:
                return _utc(parsed)
    except (ValueError, OverflowError):
        pass
    return None


def _epoch(value):
    if type(value) not in (int, float):
        return None
    try:
        if not math.isfinite(value) or value < 0:
            return None
        return _utc(datetime.fromtimestamp(value, timezone.utc))
    except (ValueError, OverflowError, OSError):
        return None


def _sample(meta):
    start, end = _iso(meta.get("sample_start")), _iso(meta.get("sample_end"))
    if start and end:
        # A date has day precision; a timestamp has instant precision. Mixed
        # endpoints cannot establish a precise, ordered source window.
        if type(start[0]) is not type(end[0]) or start[0] > end[0]:
            return "Match dates not published"
        return f"Matches {start[1]} – {end[1]}"
    if start:
        return f"Matches {start[1]} – unknown"
    if end:
        return f"Matches unknown – {end[1]}"
    return "Match dates not published"


def data_timing(meta, *, editorial=False) -> str:
    """Separate an explicit match window from source updates or retrievals.

    ``updated`` and ``data_date`` are source freshness, never sample bounds.
    Retrieval uses an ISO ``fetched_at`` or seconds-since-epoch ``_fetched_at``.
    Editorial sources have an edit date, not a statistical match window.
    """
    meta = meta if isinstance(meta, Mapping) else {}
    updated = _iso(meta.get("updated"))
    if editorial:
        return f"Edited {updated[1]}" if updated else "Update date not published"
    parts = [_sample(meta)]
    if updated:
        prefix = "Dataset updated" if meta.get("updated_scope") == "dataset" else "Updated"
        parts.append(f"{prefix} {updated[1]}")
    else:
        source_date = _iso(meta.get("data_date"))
        retrieved = _iso(meta.get("fetched_at")) or _epoch(meta.get("_fetched_at"))
        if source_date:
            parts.append(f"Updated {source_date[1]}")
        elif retrieved:
            parts.append(f"Retrieved {retrieved[1]}")
    return " · ".join(parts)
