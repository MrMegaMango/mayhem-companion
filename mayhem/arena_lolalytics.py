"""Bounded parser for Lolalytics' public Arena page snapshot.

Only the page's selected champion, aggregate augments, and item observations are
read. Qwik's object references are data, never code. Augment IDs are translated
by unambiguous English names, or by reference IDs only after validating the
source's entire named ID namespace against the caller's Arena reference catalog.
"""
from __future__ import annotations

from html.parser import HTMLParser
import json
import math
import re
from urllib.parse import parse_qs, urlsplit

from .data import DataUnavailableError
from .matching import normalize_title

MAX_BYTES = 3_000_000
MAX_OBJECTS = 50_000
MAX_ROWS = 2_000
MAX_DECODED = 100_000
MIN_ID_ANCHORS = 100


def _error(message):
    return DataUnavailableError("Arena Lolalytics source: " + message)


class _Page(HTMLParser):
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.scripts, self.canonicals = [], []
        self.capture = None
        self.feed(html)
        self.close()
        if self.capture is not None:
            raise _error("incomplete page snapshot")

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "script" and attrs.get("type") == "qwik/json":
            if self.capture is not None:
                raise _error("nested page snapshot")
            self.capture = []
        elif tag == "link" and attrs.get("rel") == "canonical":
            self.canonicals.append(attrs.get("href"))

    def handle_endtag(self, tag):
        if tag == "script" and self.capture is not None:
            self.scripts.append("".join(self.capture))
            self.capture = None

    def handle_data(self, data):
        if self.capture is not None:
            self.capture.append(data)


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


class _Graph:
    def __init__(self, objects):
        if not isinstance(objects, list) or not 1 <= len(objects) <= MAX_OBJECTS:
            raise _error("invalid snapshot object count")
        self.objects = objects
        self.decoded = {}
        self.visits = 0

    def one(self, keys):
        matches = [obj for obj in self.objects if isinstance(obj, dict) and keys <= obj.keys()]
        if len(matches) != 1:
            raise _error("missing or ambiguous snapshot section")
        return matches[0]

    def read(self, ref, active=frozenset(), depth=0):
        self.visits += 1
        if self.visits > MAX_DECODED or depth > 14:
            raise _error("snapshot reference limits exceeded")
        if not isinstance(ref, str) or not re.fullmatch(r"[0-9a-z]{1,6}", ref):
            raise _error("invalid snapshot reference")
        index = int(ref, 36)
        if index >= len(self.objects) or index in active:
            raise _error("missing or circular snapshot reference")
        if index in self.decoded:
            return self.decoded[index]
        value = self.objects[index]
        active = active | {index}
        if isinstance(value, dict):
            value = {key: self.read(child, active, depth + 1) for key, child in value.items()}
        elif isinstance(value, list):
            value = [self.read(child, active, depth + 1) for child in value]
        elif type(value) not in (str, int, float, bool, type(None)):
            raise _error("invalid snapshot value")
        self.decoded[index] = value
        return value


def _patch(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,3}\.[0-9]{1,2}", value):
        raise _error("invalid patch")
    major, minor = map(int, value.split("."))
    # Riot uses 16.x internally for the externally named 26.x season.
    return (26 if major == 16 else major, minor)


def _number(value, *, count=False, percent=False):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise _error("invalid numeric statistic")
    if count:
        if type(value) is not int or value > 1_000_000_000:
            raise _error("invalid sample count")
        return value
    if percent and value > 100:
        raise _error("percentage out of range")
    return value / 100 if percent else value


def _name(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 120 or any(ord(c) < 32 for c in value):
        raise _error("invalid name")
    return value


def _identifier(value, *, zero=False):
    if type(value) is not int or not (0 if zero else 1) <= value <= 2**31 - 1:
        raise _error("invalid observation identity")
    return str(value)


def _rows(value, length):
    if not isinstance(value, list) or len(value) > MAX_ROWS:
        raise _error("invalid observations")
    result, seen = [], set()
    for row in value:
        if not isinstance(row, list) or len(row) != length:
            raise _error("changed observation columns")
        ident = _identifier(row[0], zero=True)
        if ident in seen:
            raise _error("duplicate observation")
        seen.add(ident)
        rate = _number(row[1], percent=True)
        popularity = _number(row[2])
        if popularity > 10_000:
            raise _error("pick percentage out of range")
        count = _number(row[3], count=True)
        if length == 5:
            _number(row[4])  # Source's item acquisition time, not a build slot.
        result.append((ident, rate if count else None, count))
    return result


def _verified_reference_entries(source_augments, references, reference_ids):
    """Recover stale labels only after the full named source proves ID alignment.

    In the verified 16.21 snapshot all 225 source names independently resolve to
    the identical Riot ID. A partial, remapped or conflicting catalog must never
    enable an ID-only join. Unnamed observations still require a unique reference
    name, a unique ID entry, and an explicit Arena rarity.
    """
    if len(source_augments) < MIN_ID_ANCHORS:
        return {}
    seen_names = set()
    # The label catalog also includes crafting choices with rarity 4. Their
    # verified names/IDs remain anchors; only recovered playable rows need a
    # silver/gold/prismatic rarity below.
    for source_id, entry in source_augments.items():
        if (not isinstance(source_id, str) or not re.fullmatch(r"[1-9][0-9]{0,9}", source_id)
                or not isinstance(entry, list) or len(entry) != 2):
            return {}
        try:
            normalized = normalize_title(_name(entry[0]))
        except DataUnavailableError:
            return {}
        candidates = references.get(normalized, [])
        if (normalized in seen_names or len(candidates) != 1 or candidates[0][0] != source_id
                or len(reference_ids.get(source_id, [])) != 1):
            return {}
        seen_names.add(normalized)
    recovered = {}
    for ident, entries in reference_ids.items():
        if len(entries) != 1:
            continue
        name, rarity = entries[0]
        if rarity not in ("silver", "gold", "prismatic"):
            continue
        candidates = references.get(normalize_title(name), [])
        if len(candidates) == 1 and candidates[0][0] == ident:
            recovered[ident] = (name, ("silver", "gold", "prismatic").index(rarity))
    return recovered


def _route(url):
    if not isinstance(url, str):
        raise _error("invalid source URL")
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.netloc != "lolalytics.com" or parsed.fragment
            or not re.fullmatch(r"/lol/[a-z0-9]{1,40}/arena/build/", parsed.path)):
        raise _error("invalid Arena source URL")
    query = parse_qs(parsed.query, keep_blank_values=True)
    if (query.get("tier") != ["all"] or any(key not in ("tier", "patch") for key in query)
            or ("patch" in query and len(query["patch"]) != 1)):
        raise _error("unexpected source filters")
    if "patch" in query:
        _patch(query["patch"][0])
    return parsed.path, query.get("patch", [None])[0]


def champion_url(champion, patch):
    """Build a fixed-origin all-ranks page URL from Riot's internal name."""
    if not isinstance(champion, dict):
        raise _error("invalid champion")
    ident = str(champion.get("id", ""))
    raw = champion.get("raw_name", champion.get("riotId"))
    if (not re.fullmatch(r"[1-9][0-9]{0,9}", ident) or not isinstance(raw, str)
            or not re.fullmatch(r"[A-Za-z0-9]{1,40}", raw)):
        raise _error("invalid champion route")
    route = "wukong" if ident == "62" and raw == "MonkeyKing" else raw.lower()
    major, minor = _patch(patch)
    internal = 16 if major == 26 else major
    return f"https://lolalytics.com/lol/{route}/arena/build/?tier=all&patch={internal}.{minor}"


def parse_champion(html, champion, url, augment_catalog, *, expected_patch=None):
    """Return the ArenaProvider champion schema from one public HTML response.

    ``augment_catalog`` is the caller's ID-to-name Arena reference dictionary.
    Unsupported names are omitted, never matched through numeric coincidences.
    A source patch expressed as 16.x is equivalent only to public patch 26.x.
    """
    if not isinstance(html, str) or len(html.encode("utf-8")) > MAX_BYTES:
        raise _error("invalid HTML response")
    path, requested_patch = _route(url)
    try:
        page = _Page(html)
        if len(page.scripts) != 1 or len(page.canonicals) != 1:
            raise _error("missing or ambiguous page snapshot")
        canonical_path, canonical_patch = _route(page.canonicals[0])
        if canonical_path != path:
            raise _error("canonical belongs to another champion or mode")
        snapshot = json.loads(page.scripts[0], object_pairs_hook=_pairs,
                              parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite JSON")))
        graph = _Graph(snapshot.get("objs") if isinstance(snapshot, dict) else None)
        nav_raw = graph.one({"path", "cid", "champion", "champName", "lang", "page", "mode",
                             "patch", "tier", "region", "status"})
        nav = {key: graph.read(nav_raw[key]) for key in
               ("path", "cid", "champName", "lang", "page", "mode", "patch", "tier", "region", "status")}
        if (nav["path"] != path or nav["mode"] != "arena" or nav["page"] != "arena"
                or nav["lang"] != "en" or nav["tier"] != "all" or nav["region"] != "all"
                or type(nav["status"]) is not int or nav["status"] != 200):
            raise _error("wrong Arena mode, language or sample filters")
        if (str(nav["cid"]) != str(champion["id"]) or type(nav["cid"]) is not int
                or _name(nav["champName"]) != champion["name"]):
            raise _error("wrong champion response")
        source_patch = _patch(nav["patch"])
        for expected in (expected_patch, requested_patch, canonical_patch):
            if expected is not None and _patch(expected) != source_patch:
                raise _error("wrong patch response")
        catalog_raw = graph.one({"patch", "champions", "champPath", "augments", "items"})
        if _patch(graph.read(catalog_raw["patch"])) != source_patch:
            raise _error("catalog patch mismatch")
        paths = graph.read(catalog_raw["champPath"])
        if (not isinstance(paths, dict) or paths.get(str(champion["id"])) != path.split("/")[2]):
            raise _error("champion route mismatch")
        source_augments = graph.read(catalog_raw["augments"])
        item_names = graph.read(catalog_raw["items"])
        if not isinstance(source_augments, dict) or not isinstance(item_names, dict):
            raise _error("invalid source names")
        stats_raw = graph.one({"header", "augment", "prismatic", "item", "response"})
        response = graph.read(stats_raw["response"])
        if not isinstance(response, dict) or response.get("valid") is not True:
            raise _error("invalid statistics response")
        header = graph.read(stats_raw["header"])
        if (not isinstance(header, dict) or type(header.get("cid")) is not int
                or header.get("cid") != nav["cid"]):
            raise _error("statistics belong to another champion")
        count = _number(header["n"], count=True)
        win_rate = _number(header["wr"], percent=True)
        summary = {"sample_count": count, "win_rate": win_rate if count else None,
                   "first_rate": None, "avg_placement": None, "low_sample": count < 200}
        # First-place and average placement remain unknown here: no inferred
        # cutoff, weighted histogram, or rounded derived metric is presented.
        references, reference_ids = {}, {}
        if not isinstance(augment_catalog, dict) or len(augment_catalog) > MAX_ROWS:
            raise _error("invalid reference catalog")
        for key, reference in augment_catalog.items():
            if not isinstance(reference, dict):
                raise _error("invalid reference entry")
            name = _name(reference.get("name"))
            ident = str(key)
            if not re.fullmatch(r"[1-9][0-9]{0,9}", ident) or str(reference.get("id", ident)) != ident:
                raise _error("invalid reference identity")
            references.setdefault(normalize_title(name), []).append((ident, name))
            reference_ids.setdefault(ident, []).append((name, reference.get("rarity")))
        recovered = _verified_reference_entries(source_augments, references, reference_ids)
        aggregate = graph.read(stats_raw["augment"])
        if not isinstance(aggregate, dict) or "augment0" not in aggregate:
            raise _error("missing aggregate augment observations")
        augments, mapped = [], set()
        for ident, rate, samples in _rows(aggregate["augment0"], 4):
            if ident == "0":
                continue
            entry = source_augments.get(ident)
            if ident not in source_augments:
                reference = recovered.get(ident)
                if reference is None:
                    continue
                entry = list(reference)
            if (not isinstance(entry, list) or len(entry) != 2 or type(entry[1]) is not int
                    or entry[1] not in (0, 1, 2)):
                raise _error("invalid augment identity")
            candidates = references.get(normalize_title(_name(entry[0])), [])
            if len(candidates) != 1:
                continue
            ref_id, name = candidates[0]
            if ref_id in mapped:
                raise _error("duplicate mapped augment identity")
            mapped.add(ref_id)
            augments.append({"id": ref_id, "name": name, "icon_url": None, "description": "",
                             "rarity": ("silver", "gold", "prismatic")[entry[1]],
                             "win_rate": rate, "sample_count": samples, "low_sample": samples < 200})
        # Prismatic selections and later purchased items are distinct source
        # tables. Keep the prismatic observation for overlapping IDs; never mix
        # their rates or add their samples together.
        items, item_ids = [], set()
        for category, field, width in (("prismatic", "prismatic", 4), ("item", "item", 5)):
            for ident, rate, samples in _rows(graph.read(stats_raw[field]), width):
                if ident == "0" or ident in item_ids or ident not in item_names:
                    continue
                name = _name(item_names[ident])
                item_ids.add(ident)
                # All source item IDs use this verified 64px CDN route. Most
                # rows are hydrated in the browser, so their images need not be
                # present in the server-rendered HTML. Never use page-supplied
                # image origins or paths to construct the download URL.
                icon_url = f"https://cdn5.lolalytics.com/item64/{ident}.webp"
                items.append({"id": ident, "name": name, "icon_url": icon_url, "description": "",
                              "category": category, "win_rate": rate, "sample_count": samples,
                              "low_sample": samples < 200})
        return {"source": "Lolalytics", "source_url": url, "game_mode": "Arena",
                "version": nav["patch"], "source_patch": nav["patch"], "data_date": None,
                "stale": False, "win_rate_label": "Win rate",
                "rate_note": "Source win rate; finish cutoff not specified. Items and prismatic selections are individual observations, not full builds.",
                "sample_label": "Arena · all ranks · all regions · queue unspecified",
                "champion_id": str(champion["id"]), "champion_name": champion["name"],
                "summary": summary, "augments": augments, "items": items}
    except DataUnavailableError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError, OverflowError) as exc:
        raise _error("invalid page snapshot") from exc
