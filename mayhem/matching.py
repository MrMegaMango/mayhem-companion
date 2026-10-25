"""Conservative OCR title matching, independent of any game statistics.

Pass title-region OCR lines when available. Whole-line comparison deliberately
avoids treating a short augment name inside a longer title or description as a
match. Uncertain matches are omitted so callers can offer manual lookup.
"""
from __future__ import annotations

from difflib import SequenceMatcher
import math
import re
import unicodedata
from typing import Any, Iterable, Mapping


def normalize_title(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).casefold()
    return " ".join(re.sub(r"[^\w]+", " ", text, flags=re.UNICODE).split())


def _number(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return float(value)
    return None


def _line(line: Any) -> dict[str, Any] | None:
    if isinstance(line, str):
        return {"text": line, "x": None, "y": None, "width": None, "height": None}
    if not isinstance(line, Mapping) or not isinstance(line.get("text"), str):
        return None
    box = line.get("bounds", line.get("bounding_box", {}))
    box = box if isinstance(box, Mapping) else {}
    return {
        "text": line["text"],
        "x": _number(line.get("x", box.get("x"))),
        "y": _number(line.get("y", box.get("y"))),
        "width": _number(line.get("width", box.get("width"))),
        "height": _number(line.get("height", box.get("height"))),
    }


def _catalog_entries(catalog: Mapping[Any, Any]) -> list[tuple[Any, str, list[str]]]:
    entries = []
    for identifier, entry in catalog.items():
        if isinstance(entry, str):
            name, aliases = entry, []
        elif isinstance(entry, Mapping):
            name = entry.get("name", "")
            aliases = entry.get("aliases", [])
            aliases = aliases if isinstance(aliases, (list, tuple)) else []
        else:
            continue
        if not isinstance(name, str) or not name.strip():
            continue
        variants = [normalize_title(v) for v in [name, *aliases] if isinstance(v, str) and v.strip()]
        entries.append((identifier, name, variants))
    return entries


def _similarity(observed: str, target: str) -> float:
    if observed == target:
        return 1.0
    # Short titles are especially susceptible to accidental HUD matches.
    if min(len(observed.replace(" ", "")), len(target.replace(" ", ""))) <= 6:
        return 0.0
    # Whole title only: do not let descriptions or title variants match a
    # substring. A missing OCR word is intentionally left for manual lookup.
    if observed.startswith(target + " ") or target.startswith(observed + " "):
        return 0.0
    return SequenceMatcher(None, observed, target, autojunk=False).ratio()


def _horizontal_alignment(upper: Mapping[str, Any], lower: Mapping[str, Any]) -> bool:
    """Require substantial overlap within one card, allowing centered titles."""
    if any(line.get(key) is None or line[key] <= 0
           for line in (upper, lower) for key in ("width", "height")):
        return False
    if any(line.get(key) is None for line in (upper, lower) for key in ("x", "y")):
        return False
    left = max(upper["x"], lower["x"])
    right = min(upper["x"] + upper["width"], lower["x"] + lower["width"])
    narrow = min(upper["width"], lower["width"])
    if right - left < narrow * 0.75:
        return False
    center_delta = abs(upper["x"] + upper["width"] / 2 -
                       lower["x"] - lower["width"] / 2)
    left_delta = abs(upper["x"] - lower["x"])
    return (center_delta <= max(upper["width"], lower["width"]) * 0.20 or
            left_delta <= narrow * 0.15)


def _can_wrap(upper: Mapping[str, Any], lower: Mapping[str, Any]) -> bool:
    if not _horizontal_alignment(upper, lower):
        return False
    tall = max(upper["height"], lower["height"])
    if min(upper["height"], lower["height"]) < tall * 0.65:
        return False  # A smaller description beneath a heading is not its title.
    gap = lower["y"] - (upper["y"] + upper["height"])
    return lower["y"] > upper["y"] and -tall * 0.12 <= gap <= tall * 0.65


def _joined_title_lines(lines: list[dict[str, Any]], entries: list[tuple[Any, str, list[str]]]) -> list[dict[str, Any]]:
    """Join up to three adjacent title fragments only into known exact names.

    A confirmed longer title consumes its fragments, so a wrapped "Stats on
    Stats!" does not also produce "Stats!". Unrelated complete titles retain
    their normal exact/fuzzy matching behavior. Geometry uses ratios, working
    with both pixels and normalized coordinates.
    """
    known: dict[str, set[Any]] = {}
    for identifier, _, variants in entries:
        for variant in variants:
            known.setdefault(variant, set()).add(identifier)
    joined = []
    for index, first in enumerate(lines):
        chain = [index]
        for _ in range(2):
            upper = lines[chain[-1]]
            candidates = []
            for next_index, lower in enumerate(lines):
                if next_index in chain or not _can_wrap(upper, lower):
                    continue
                # Do not jump over an intervening line, even when its font is
                # smaller and it cannot itself participate in a title join.
                if any(other_index not in (*chain, next_index) and
                       other.get("y") is not None and
                       upper["y"] < other["y"] < lower["y"] and
                       _horizontal_alignment(upper, other)
                       for other_index, other in enumerate(lines)):
                    continue
                candidates.append(next_index)
            if len(candidates) != 1:
                break
            chain.append(candidates[0])
            pieces = [lines[part] for part in chain]
            text = " ".join(piece["text"].strip() for piece in pieces)
            if len(known.get(normalize_title(text), ())) != 1:
                continue
            left = min(piece["x"] for piece in pieces)
            top = min(piece["y"] for piece in pieces)
            right = max(piece["x"] + piece["width"] for piece in pieces)
            bottom = max(piece["y"] + piece["height"] for piece in pieces)
            joined.append({"text": text, "x": left, "y": top,
                           "width": right - left, "height": bottom - top,
                           "_sources": tuple(chain), "_order": min(chain)})
    # Prefer the complete longest known title over a valid shorter prefix.
    joined.sort(key=lambda line: (-len(line["_sources"]), -len(line["text"])))
    consumed: set[int] = set()
    selected = []
    for line in joined:
        if not consumed.intersection(line["_sources"]):
            selected.append(line)
            consumed.update(line["_sources"])
    selected.extend({**line, "_order": index} for index, line in enumerate(lines)
                    if index not in consumed)
    selected.sort(key=lambda line: line["_order"])
    return selected


def _match_candidates(lines: Iterable[Any], catalog: Mapping[Any, Any], *, threshold: float = 0.87) -> list[dict[str, Any]]:
    """Keep every confident spatial occurrence until its offer row is known.

    A tooltip may contain a clearer or earlier occurrence of the same name as
    a card. Deduplicating by augment ID here would discard the card's geometry.
    """
    if not 0.5 <= threshold <= 1:
        raise ValueError("threshold must be between 0.5 and 1")
    entries = _catalog_entries(catalog)
    prepared = [parsed for raw_line in lines if (parsed := _line(raw_line)) is not None]
    candidates = []
    for order, line in enumerate(_joined_title_lines(prepared, entries)):
        observed = normalize_title(line["text"])
        if not observed or len(observed) > 100:
            continue
        scores = [(max((_similarity(observed, variant) for variant in variants), default=0.0), identifier, name)
                  for identifier, name, variants in entries]
        scores.sort(key=lambda item: item[0], reverse=True)
        if not scores or scores[0][0] < threshold:
            continue
        score, identifier, name = scores[0]
        # Exact unique matches are reliable. Similar competing titles require
        # a sufficient margin; never guess between Scoped/Scopier/Scopiest.
        if len(scores) > 1 and (scores[1][0] == score or
                              (score < 1 and score - scores[1][0] < 0.055)):
            continue
        candidates.append({"id": identifier, "name": name, "similarity": round(score, 4),
                           "x": line["x"], "y": line["y"], "width": line["width"],
                           "height": line["height"], "_order": order})
    return candidates


def _sorted_matches(candidates: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered = sorted(candidates, key=lambda item: (
        item["y"] if item["y"] is not None else float("inf"),
        item["x"] if item["x"] is not None else float("inf"), item["_order"]))
    return [{key: value for key, value in item.items() if key != "_order"} for item in ordered]


def match_augments(lines: Iterable[Any], catalog: Mapping[Any, Any], *, threshold: float = 0.87) -> list[dict[str, Any]]:
    """Return confident, unique title candidates with optional OCR geometry.

    Lines may be strings or dictionaries with text/x/y/width/height (or a
    bounds dictionary). Catalog values may be names or records with name and
    optional aliases. IDs retain their original type. Results are sorted by
    y then x when geometry is present, otherwise by input order. Each augment
    retains its highest-confidence occurrence; ties retain the input order.
    """
    selected: dict[Any, dict[str, Any]] = {}
    for candidate in _match_candidates(lines, catalog, threshold=threshold):
        existing = selected.get(candidate["id"])
        if existing is None or candidate["similarity"] > existing["similarity"]:
            selected[candidate["id"]] = candidate
    return _sorted_matches(selected.values())

def detect_offers(lines: Iterable[Any], catalog: Mapping[Any, Any], *, min_count: int = 2) -> list[dict[str, Any]]:
    """Return 2-3 confident distinct titles on one horizontal row, left to right.

    Geometry is required for automatic offers. A single title or scattered
    matches can still be displayed through match_augments for manual review.
    Screenshots containing more than one plausible offer row are ambiguous and
    produce no automatic result. Pixel and 0-1 normalized coordinates work.
    """
    if min_count not in (2, 3):
        raise ValueError("min_count must be 2 or 3")
    matches = _sorted_matches(m for m in _match_candidates(lines, catalog)
                              if m["x"] is not None and m["y"] is not None)
    if len(matches) < min_count:
        return []
    normalized = all(0 <= m["x"] <= 1 and 0 <= m["y"] <= 1 for m in matches)
    floor_tolerance = 0.025 if normalized else 18.0
    rows: list[list[dict[str, Any]]] = []
    for item in matches:
        center = item["y"] + (item["height"] or 0) / 2
        matching_row = None
        for row in rows:
            row_center = sum(m["y"] + (m["height"] or 0) / 2 for m in row) / len(row)
            tolerance = max(floor_tolerance, max((m["height"] or 0 for m in [*row, item]), default=0) * 0.9)
            if abs(center - row_center) <= tolerance:
                matching_row = row
                break
        if matching_row is None:
            rows.append([item])
        else:
            matching_row.append(item)
    plausible = []
    for row in rows:
        if not min_count <= len(row) <= 3:
            continue
        # The same augment in two separated card positions is not a valid
        # offer. Do not turn that impossible row into a plausible partial pair.
        if len({item["id"] for item in row}) != len(row):
            continue
        row = sorted(row, key=lambda m: m["x"])
        min_gap = 0.05 if normalized else 30.0
        if all(right["x"] - left["x"] >= min_gap for left, right in zip(row, row[1:])):
            plausible.append(row)
    return plausible[0] if len(plausible) == 1 else []
