"""Honest sample-window and freshness labels for source metadata.

A dataset update or retrieval time never establishes when its matches occurred.
Calendar estimates combine supplied metadata with verified patch-release days.
This module performs no clock or network work.
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


def data_freshness(meta, *, editorial=False) -> str:
    """Format source freshness alone, without claiming match coverage."""
    meta = meta if isinstance(meta, Mapping) else {}
    updated = _iso(meta.get("updated"))
    if editorial:
        return f"Edited {updated[1]}" if updated else ""
    if updated:
        prefix = "Dataset updated" if meta.get("updated_scope") == "dataset" else "Updated"
        return f"{prefix} {updated[1]}"
    source_date = _iso(meta.get("data_date"))
    if source_date:
        return f"Updated {source_date[1]}"
    retrieved = _iso(meta.get("fetched_at")) or _epoch(meta.get("_fetched_at"))
    return f"Retrieved {retrieved[1]}" if retrieved else ""


def data_timing(meta, *, editorial=False) -> str:
    """Backward-compatible explicit match bounds plus source freshness."""
    meta = meta if isinstance(meta, Mapping) else {}
    freshness = data_freshness(meta, editorial=editorial)
    if editorial:
        return freshness or "Update date not published"
    return " · ".join(part for part in (_sample(meta), freshness) if part)


# Official scheduled release DAYS in Pacific Time, not UTC rollout instants.
# These supply calendar context only, never observed first/last match dates.
# Verified 2026-11-02 against Riot's calendars linked in period details.
_LOL_CALENDAR_URL = "https://support.riotgames.com/en-us/league-of-legends/gameplay/patch-schedule-league-of-legends"
_TFT_CALENDAR_URL = "https://support.riotgames.com/en-us/tft/events/patch-schedule-teamfight-tactics"
_LOL_2026_STARTS = {
    19: date(2026, 9, 23), 20: date(2026, 10, 7),
    21: date(2026, 10, 21), 22: date(2026, 11, 4),
    23: date(2026, 11, 18), 24: date(2026, 12, 9),
}
# TFT uses Set.Patch, independently of the League display/data version.
_TFT_STARTS = {
    "18.3": date(2026, 9, 23), "18.4": date(2026, 10, 7),
    "18.5": date(2026, 10, 21), "18.6": date(2026, 11, 4),
    "18.6b": date(2026, 11, 11), "19.1": date(2026, 12, 1),
    "19.2": date(2026, 12, 15),
}
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _day(parsed):
    value = parsed[0]
    return value.date() if isinstance(value, datetime) else value


def _day_text(value, *, year=True):
    label = f"{_MONTHS[value.month - 1]} {value.day}"
    return f"{label}, {value.year}" if year else label


def _day_range(start, end):
    return f"{_day_text(start, year=start.year != end.year)} – {_day_text(end)}"


def _patch_anchor(meta):
    if "patch_start" in meta:
        explicit = _iso(meta.get("patch_start"))
        if not explicit:
            return None
        precision = "UTC calendar date" if isinstance(explicit[0], datetime) else "source timezone not published"
        return _day(explicit), f"Source-provided patch start ({precision})", None
    source = str(meta.get("source") or "").casefold()
    mode = str(meta.get("game_mode") or "").casefold()
    is_tft = mode in ("tft", "teamfight tactics") or source.startswith("tactics.tools") or str(meta.get("set") or "").startswith("Set ")
    is_lol = source in ("aramkit", "lolalytics", "winrate.lol") or mode in ("arena", "aram", "aram mayhem", "league of legends", "lol")
    if not is_tft and not is_lol:
        return None
    patches = [meta[key] for key in ("patch", "version", "source_patch") if meta.get(key) not in (None, "")]
    if not patches:
        return None
    anchors = []
    for patch in patches:
        match = re.fullmatch(r"([0-9]{2})\.([0-9]{1,2})([a-zA-Z]?)", str(patch))
        if not match:
            return None
        major, minor, suffix = int(match[1]), int(match[2]), match[3].lower()
        if is_tft:
            base = f"{major}.{minor}"
            exact = base + suffix
            known = exact if exact in _TFT_STARTS else base
            start = _TFT_STARTS.get(known)
            if start is None:
                return None
            if suffix and known != exact:
                basis = f"Riot scheduled base TFT patch {base} date (Pacific Time); the exact {exact} mid-patch start is not established"
            else:
                basis = f"Riot scheduled TFT patch {known} date (Pacific Time)"
            anchors.append((start, basis, _TFT_CALENDAR_URL))
        else:
            # Data Dragon 16.x and public 26.x are explicit 2026 aliases.
            if major not in (16, 26) or suffix or minor not in _LOL_2026_STARTS:
                return None
            anchors.append((_LOL_2026_STARTS[minor], f"Riot scheduled patch 26.{minor} date (Pacific Time)", _LOL_CALENDAR_URL))
    if any(anchor[0] != anchors[0][0] for anchor in anchors[1:]):
        return None
    return anchors[0]


def _snapshot_anchor(meta):
    for key, basis in (("updated", "Source dataset update" if meta.get("updated_scope") == "dataset" else "Source update"), ("data_date", "Source data date")):
        parsed = _iso(meta.get(key))
        if parsed:
            precision = "UTC calendar date" if isinstance(parsed[0], datetime) else "source timezone not published"
            return _day(parsed), f"{basis} ({precision})", False, parsed[0]
    retrieved = _iso(meta.get("fetched_at")) or _epoch(meta.get("_fetched_at"))
    if retrieved:
        precision = "UTC calendar date" if isinstance(retrieved[0], datetime) else "retrieval timezone not published"
        return _day(retrieved), f"App retrieval ({precision})", True, retrieved[0]
    return None


def _estimated_period(meta):
    start, end = _patch_anchor(meta), _snapshot_anchor(meta)
    if not start or not end or start[0] > end[0]:
        return None
    if isinstance(end[3], datetime):
        explicit = _iso(meta.get("patch_start"))
        if explicit and isinstance(explicit[0], datetime):
            if explicit[0] > end[3]:
                return None
        elif start[2]:
            # The verified schedule entries are Sep-Dec 2026. Pacific midnight
            # is 07:00 UTC before Nov 1, 08:00 thereafter. This only rejects an
            # endpoint before the release DAY; it does not assert a rollout hour.
            hour = 7 if start[0] < date(2026, 11, 1) else 8
            calendar_day_begins = datetime(start[0].year, start[0].month, start[0].day, hour, tzinfo=timezone.utc)
            if end[3] < calendar_day_begins:
                return None
    return start, end


def data_period(meta, *, editorial=False) -> str:
    """Show supplied match bounds, else clearly labelled patch calendar context.

    A patch release and snapshot date are not observed match endpoints. No clock
    or next-patch boundary is used; cached data retains its own snapshot end.
    """
    meta = meta if isinstance(meta, Mapping) else {}
    if editorial:
        edited = _iso(meta.get("updated"))
        return f"Expert tiers · edited {_day_text(_day(edited))}" if edited else "Expert guidance · no match sample"
    if "sample_start" in meta or "sample_end" in meta:
        return _sample(meta)
    estimated = _estimated_period(meta)
    if not estimated:
        return "Match dates not published"
    start, end = estimated
    prefix = "Patch start → retrieval" if end[2] else "Patch period (est.)"
    return f"{prefix}: {_day_range(start[0], end[0])}"


def data_period_details(meta, *, editorial=False) -> str:
    """Explain exactly which snapshot supplied the displayed period's bounds."""
    meta = meta if isinstance(meta, Mapping) else {}
    if editorial:
        freshness = data_freshness(meta, editorial=True)
        return "Expert guidance has no statistical match sample or match-date range." + (f" {freshness}." if freshness else " The edit date is not published.")
    if "sample_start" in meta or "sample_end" in meta:
        sample = _sample(meta)
        if sample == "Match dates not published":
            return "The supplied match bounds are invalid or ambiguous; no period is inferred from them."
        return f"{sample}. These are source-provided match bounds; any missing endpoint remains unknown. Timestamps are normalized to UTC. Date-only bounds retain the source's day precision; its timezone is not published."
    estimated = _estimated_period(meta)
    if not estimated:
        return "The source does not publish first/last match dates. This snapshot also lacks a valid, ordered patch-start and snapshot-date pair for a calendar estimate."
    start, end = estimated
    explanation = "Calendar context only; this is not a measured first/last-match interval. "
    explanation += f"Start: {_day_text(start[0])} — {start[1]}. End: {_day_text(end[0])} — {end[1]}. "
    if end[2]:
        explanation += "The end is when the app retrieved this snapshot, not a source update time or the latest included match. "
    else:
        explanation += "The end is the source's snapshot date, not the latest included match. "
    explanation += "Actual match coverage may be narrower. Regional rollout times, hotfix boundaries and processing delays are not established by this range."
    if start[2]:
        explanation += f" Riot calendar (scheduled dates can change): {start[2]}"
    return explanation
