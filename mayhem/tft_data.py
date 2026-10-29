"""Public TFT statistics, with set-aware snapshots and on-demand unit builds.

Tactics.tools comp outcomes are counts; its unit/build outcomes are percentages.
These formats are deliberately converted separately. MetaTFT augment tiers are
editorial guidance and never converted into placement or win-rate estimates.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
from html import unescape
from html.parser import HTMLParser
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
import threading
import time
import uuid
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .data import DataUnavailableError


SITE = "https://tactics.tools"
UNITS_URL = SITE + "/units"
AUGMENT_TIERS_URL = "https://api-hc.metatft.com/tft-stat-api/augments_tiers"
CACHE_TTL = 3600
REQUEST_TIMEOUT = 8
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_ID = re.compile(r"[A-Za-z0-9_]{1,100}\Z")
_ICON = re.compile(
    r"https://ap\.tft\.tools/img/(?:gg[0-9]+/face|items_s[0-9]+|augments)/"
    r"[A-Za-z0-9_-]+\.(?:png|jpg|webp)(?:\?w=[0-9]{1,3})?\Z"
)


def _fail(message):
    raise DataUnavailableError("TFT: " + message)


def _identifier(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        _fail("invalid source identifier")
    return value


def _number(value, low, high):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or not low <= value <= high):
        _fail("invalid source statistic")
    return float(value)


def _count(value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        _fail("invalid source sample count")
    return value


def _stats(row, *, percentages):
    if not isinstance(row, dict):
        _fail("missing source statistics")
    count = _count(row.get("count"))
    place = _number(row.get("place"), 1, 8)
    if percentages:
        top4 = _number(row.get("top4"), 0, 100) / 100
        first = _number(row.get("won"), 0, 100) / 100
    else:
        top4_count = _count(row.get("top4"))
        first_count = _count(row.get("win", row.get("won")))
        if not first_count <= top4_count <= count:
            _fail("inconsistent placement counts")
        top4 = top4_count / count if count else None
        first = first_count / count if count else None
    if first is not None and top4 is not None and first > top4:
        _fail("inconsistent placement percentages")
    return {
        "games": count, "sample_count": count,
        "avg_place": place if count else None,
        "avg_placement": place if count else None,
        "top4_rate": top4 if count else None,
        "first_rate": first if count else None,
        "win_rate": first if count else None,
        "low_sample": count < 200,
    }


def _json(text):
    try:
        return json.loads(text, parse_constant=lambda _: _fail("non-finite JSON"))
    except (ValueError, TypeError) as error:
        raise DataUnavailableError("TFT: malformed source JSON") from error


def _page_data(text):
    match = re.search(
        r'<script\b[^>]*\bid=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
        text, re.S,
    )
    if not match:
        _fail("source page no longer contains its data")
    try:
        value = _json(match.group(1))["props"]["pageProps"]
    except (KeyError, TypeError) as error:
        raise DataUnavailableError("TFT: source page schema changed") from error
    if not isinstance(value, dict):
        _fail("invalid source page")
    return value


def _locale(text, set_id):
    """Read JSON literals from the public locale asset; never execute JavaScript."""
    result = {}
    decoder = json.JSONDecoder()
    for kind in ("Units", "Items", "Traits", "Augments"):
        match = re.search(r"window\.s" + re.escape(set_id) + kind + r"i18n\s*=\s*", text)
        if not match:
            _fail("set catalog does not match the statistics")
        try:
            values, _ = decoder.raw_decode(text[match.end():])
        except ValueError as error:
            raise DataUnavailableError("TFT: invalid catalog") from error
        if not isinstance(values, dict) or any(not isinstance(v, str) for v in values.values()):
            _fail("invalid catalog names")
        result[kind.lower()] = values
    return result


def _plain(text):
    text = re.sub(r"<br\s*/?>", "\n", text or "", flags=re.I)
    text = re.sub(r"<[^>]*>", "", text)
    text = re.sub(r"%i:([^%]+)%", lambda m: " " + m.group(1).replace("scale", "") + " ", text)
    return unescape(text).replace("\r", "").strip()


def _date(timestamp):
    try:
        return datetime.fromtimestamp(_number(timestamp, 0, 10**11), timezone.utc).isoformat()
    except (ValueError, OverflowError, OSError) as error:
        raise DataUnavailableError("TFT: invalid source update time") from error


def _icon(url):
    return url.split("?", 1)[0] + "?w=64" if isinstance(url, str) and _ICON.fullmatch(url) else None


class _AugmentIcons(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack = []
        self.icons = {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "div":
            self.stack.append(attrs.get("id"))
        elif tag == "img":
            identifier = next((value for value in reversed(self.stack) if value), None)
            url = _icon(attrs.get("src"))
            if identifier and _ID.fullmatch(identifier) and url:
                self.icons[identifier] = url

    def handle_endtag(self, tag):
        if tag == "div" and self.stack:
            self.stack.pop()


def _catalog(locale, page):
    urls = re.findall(r'https://ap\.tft\.tools/img/[^"\s<>]+', page)
    unit_icons, item_icons = {}, {}
    item_prefix = None
    for url in urls:
        url = unescape(url)
        normalized = _icon(url)
        if not normalized:
            continue
        key = url.rsplit("/", 1)[-1].split(".", 1)[0]
        if "/face/" in url:
            unit_icons[key.casefold()] = normalized
        elif "/items_s" in url:
            item_icons[key] = normalized
            item_prefix = url.rsplit("/", 1)[0] + "/"
    units = {}
    for identifier, name in locale["units"].items():
        if identifier.endswith(("_desc", "_ability")) or "_details_" in identifier:
            continue
        _identifier(identifier)
        units[identifier] = {
            "id": identifier, "name": name,
            "icon_url": unit_icons.get(identifier.casefold()),
            "description": _plain(locale["units"].get(identifier + "_desc", "")),
        }
    items = {}
    for identifier, name in locale["items"].items():
        if identifier.endswith(("_desc", "_statLine")):
            continue
        _identifier(identifier)
        items[identifier] = {
            "id": identifier, "name": name,
            "icon_url": item_icons.get(identifier) or (_icon(item_prefix + identifier + ".png") if item_prefix else None),
            "description": _plain(locale["items"].get(identifier + "_desc", "")),
        }
    return units, items


def _comps(payload, units, locale, set_id):
    if not isinstance(payload, dict) or not isinstance(payload.get("groups"), list) or not payload["groups"]:
        _fail("composition statistics are unavailable")
    result = []
    seen = set()
    for group in payload["groups"]:
        if not isinstance(group, dict) or not isinstance(group.get("full"), dict):
            _fail("invalid composition group")
        full = group["full"]
        code = full.get("code")
        if not isinstance(code, str) or not code.endswith("TFTSet" + set_id):
            _fail("composition set differs from the catalog")
        if code in seen:
            _fail("duplicate composition group")
        seen.add(code)
        variations = full.get("comps")
        if not isinstance(variations, list) or not variations:
            continue
        board = max(variations, key=lambda row: _count(row.get("count")))
        board_ids = board.get("units")
        if not isinstance(board_ids, list) or not board_ids or any(key not in units for key in board_ids):
            _fail("composition units do not match the catalog")
        stats = _stats(full, percentages=False)
        carry_candidates = full.get("carryUnits", [])
        carry_id = carry_candidates[0][0] if carry_candidates else board_ids[-1]
        carry = units.get(carry_id, units[board_ids[-1]])["name"]
        trait_rows = [row for row in full.get("traits", []) if isinstance(row, list) and len(row) >= 3
                      and row[0] in locale["traits"] and row[2] >= stats["games"] * .4]
        trait_rows.sort(key=lambda row: (row[1], row[2]), reverse=True)
        traits = list(dict.fromkeys(locale["traits"][row[0]] for row in trait_rows))[:4]
        name = carry + (" · " + traits[0] if traits else "")
        result.append({
            **stats, "id": hashlib.sha256(code.encode()).hexdigest()[:16],
            "name": name, "carry": carry, "traits": traits,
            "units": [copy.deepcopy(units[key]) for key in board_ids],
            "icon_url": units.get(carry_id, units[board_ids[-1]]).get("icon_url"),
            "scope": "comp_archetype", "source_url": SITE + "/team-compositions",
            "description": "Archetype statistics. The board shown is one common variation.",
            "board_games": _count(board.get("count")),
        })
    if not result:
        _fail("composition statistics are empty")
    return sorted(result, key=lambda row: (row["avg_place"] is None, row["avg_place"] or 8))


def _augments(locale, tiers, set_id, icons):
    tier_by_id = {}
    author, updated = "", ""
    if tiers is not None:
        if not isinstance(tiers, dict) or tiers.get("tft_set") != "TFTSet" + set_id or tiers.get("tier_list_set") != "TFTSet" + set_id:
            _fail("augment tiers refer to a different set")
        content = tiers.get("content", {})
        author = content.get("author", {}).get("gameName", "")
        updated = content.get("updated_at", "")
        for tier in content.get("content", {}).get("tierList", []):
            label = tier.get("label")
            if label not in ("S", "A", "B", "C", "D"):
                continue
            for entry in tier.get("content", []):
                if entry.get("type") == "augment":
                    tier_by_id[entry.get("id")] = label
    result = []
    for identifier, name in locale["augments"].items():
        if identifier.endswith("_desc"):
            continue
        _identifier(identifier)
        tier = tier_by_id.get(identifier)
        result.append({
            "id": identifier, "name": name, "icon_url": icons.get(identifier),
            "description": _plain(locale["augments"].get(identifier + "_desc", "")),
            "tier": tier, "scope": "expert_tier" if tier else "reference",
            "source_url": "https://www.metatft.com/augments" if tier else SITE + "/info/augments",
            "tier_author": author if tier else "",
        })
    return sorted(result, key=lambda row: ("SABCD".find(row["tier"]) if row["tier"] else 9, row["name"])), {
        "source": "MetaTFT tiers · Tactics.tools catalog" if tier_by_id else "Tactics.tools",
        "tier_author": author,
        "source_url": "https://www.metatft.com/augments" if tier_by_id else SITE + "/info/augments",
        "patch": "", "set": "Set " + set_id, "updated": updated,
        "rank": "", "queue": "Ranked", "scope": "Expert tiers · no placement statistics" if tier_by_id else "Augment reference",
    }


class TftProvider:
    def __init__(self, cache_dir):
        self.cache_dir = Path(cache_dir)
        self._snapshot = None
        self._lock = threading.RLock()

    def _get_text(self, url):
        request = Request(url, headers={"User-Agent": "MayhemCompanion/0.1", "Accept": "application/json,text/html,*/*"})
        try:
            with urlopen(request, timeout=REQUEST_TIMEOUT) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                _fail("source response is too large")
            return raw.decode("utf-8")
        except HTTPError as error:
            raise DataUnavailableError("TFT source request failed", transient=error.code in (408, 429) or error.code >= 500) from error
        except (URLError, TimeoutError, OSError) as error:
            raise DataUnavailableError("TFT data is temporarily unavailable", transient=True) from error
        except UnicodeError as error:
            raise DataUnavailableError("TFT: invalid response encoding") from error

    def _read(self, filename):
        try:
            with (self.cache_dir / filename).open("rb") as stream:
                raw = stream.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                return None
            return _json(raw)
        except (OSError, DataUnavailableError):
            return None

    def _write(self, filename, value):
        temporary = None
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.cache_dir,
                                             prefix=".tft-", suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(value, stream, ensure_ascii=False, allow_nan=False)
            os.replace(temporary, self.cache_dir / filename)
        except OSError:
            pass
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    @staticmethod
    def _valid_snapshot(value):
        if not (isinstance(value, dict) and value.get("_schema") == 1
                and TftProvider._valid_time(value.get("_fetched_at"))
                and type(value.get("_patch_id")) is int and 1 <= value["_patch_id"] <= 999999
                and isinstance(value.get("_set_id"), str) and re.fullmatch(r"[0-9]{1,3}", value["_set_id"])
                and value.get("_rank_id") == 1
                and isinstance(value.get("comps"), list) and bool(value["comps"])
                and isinstance(value.get("units"), list) and bool(value["units"])
                and isinstance(value.get("item_catalog"), dict)
                and isinstance(value.get("augments"), list)):
            return False
        try:
            for row in value["units"] + value["comps"]:
                if not isinstance(row.get("name"), str) or not row["name"]:
                    return False
                count = _count(row.get("games"))
                if count:
                    _number(row.get("avg_place"), 1, 8)
                    top4 = _number(row.get("top4_rate"), 0, 1)
                    if _number(row.get("first_rate"), 0, 1) > top4:
                        return False
            for row in value["units"]:
                _identifier(row.get("id"))
            for key, row in value["item_catalog"].items():
                _identifier(key)
                if row.get("id") != key or not isinstance(row.get("name"), str):
                    return False
        except (DataUnavailableError, TypeError, AttributeError):
            return False
        return True

    @staticmethod
    def _valid_time(value):
        return (type(value) in (int, float) and math.isfinite(value)
                and 0 <= value <= time.time() + 300)

    @staticmethod
    def _valid_build_cache(value, snapshot, unit_id):
        if not (isinstance(value, dict) and value.get("_schema") == 1
                and isinstance(value.get("unit"), dict) and value["unit"].get("id") == unit_id
                and value.get("_patch_id") == snapshot["_patch_id"]
                and value.get("_set_id") == snapshot["_set_id"]
                and isinstance(value.get("items"), list)
                and TftProvider._valid_time(value.get("_fetched_at"))):
            return False
        try:
            for row in value["items"]:
                count = _count(row.get("games"))
                if count:
                    _number(row.get("avg_place"), 1, 8)
                    top4 = _number(row.get("top4_rate"), 0, 1)
                    if _number(row.get("first_rate"), 0, 1) > top4:
                        return False
                items = row.get("items")
                if not isinstance(items, list) or len(items) != 3:
                    return False
                if any(item.get("id") not in snapshot["item_catalog"] for item in items):
                    return False
        except (DataUnavailableError, AttributeError, TypeError):
            return False
        return True

    def _fetch_snapshot(self):
        page = self._get_text(UNITS_URL)
        data = _page_data(page)
        aperture = data.get("aperture", {})
        patch_id = aperture.get("patch", {}).get("_0")
        if (isinstance(patch_id, bool) or not isinstance(patch_id, int) or not 1 <= patch_id <= 999999
                or aperture.get("rankGroup") != 1 or aperture.get("queue") != 1100):
            _fail("source statistics scope changed")
        title = re.search(r"<title>[^<]*for Set ([0-9]+) Patch ([0-9]+\.[0-9]+[a-z]?)</title>", page)
        if not title:
            _fail("source set metadata is missing")
        set_id, patch = title.groups()
        locale_url = "https://ap.tft.tools/static/s" + set_id + "/en.js"
        if locale_url not in page:
            _fail("source set catalog is missing")
        locale = _locale(self._get_text(locale_url), set_id)
        units, items = _catalog(locale, page)
        stats = data.get("statsData", {})
        unit_stats = stats.get("units")
        if not isinstance(unit_stats, dict) or not unit_stats:
            _fail("unit statistics are missing")
        unit_rows = []
        for key, row in unit_stats.items():
            if key not in units:
                _fail("unit statistics differ from the catalog")
            unit_rows.append({**units[key], **_stats(row, percentages=True),
                              "top_items": [copy.deepcopy(items[i]) for i in row.get("topItems", []) if i in items]})
        unit_rows.sort(key=lambda row: row["games"], reverse=True)
        comp_data = _json(self._get_text(f"https://api.tft.tools/team-compositions/1/{patch_id}"))
        comp_rows = _comps(comp_data, units, locale, set_id)
        tiers, icons = None, {}
        # Optional guidance cannot prevent browsing independently valid stats.
        try:
            candidate = _json(self._get_text(AUGMENT_TIERS_URL))
            if candidate.get("tft_set") == "TFTSet" + set_id and candidate.get("tier_list_set") == "TFTSet" + set_id:
                tiers = candidate
        except (DataUnavailableError, AttributeError):
            pass
        try:
            parser = _AugmentIcons()
            parser.feed(self._get_text(SITE + "/info/augments"))
            icons = parser.icons
        except DataUnavailableError:
            pass
        try:
            augments, augment_meta = _augments(locale, tiers, set_id, icons)
        except (DataUnavailableError, AttributeError, TypeError):
            augments, augment_meta = _augments(locale, None, set_id, icons)
        return {
            "_schema": 1, "_patch_id": patch_id, "_set_id": set_id, "_rank_id": 1,
            "_snapshot_revision": uuid.uuid4().hex,
            "_fetched_at": time.time(), "source": "Tactics.tools",
            "source_url": SITE + "/team-compositions", "patch": patch, "set": "Set " + set_id,
            "updated": _date(stats.get("lastUpdated")), "updated_scope": "dataset", "rank": "Diamond+", "queue": "Ranked",
            "stale": False, "comps": comp_rows, "units": unit_rows, "items": [],
            "item_catalog": items, "augments": augments,
            "view_meta": {"augments": augment_meta},
        }

    def load(self, force=False):
        with self._lock:
            previous = self._snapshot or self._read("snapshot.json")
            if not self._valid_snapshot(previous):
                previous = None
            if previous and not force and 0 <= time.time() - previous["_fetched_at"] < CACHE_TTL:
                self._snapshot = previous
                return copy.deepcopy(previous)
            try:
                result = self._fetch_snapshot()
            except DataUnavailableError as error:
                if previous is None or not error.transient:
                    raise
                result = {**previous, "stale": True}
            else:
                self._write("snapshot.json", result)
            self._snapshot = result
            return copy.deepcopy(result)

    def items_for(self, unit_id, force=False):
        """Fetch exact three-item observations for one unit, never a unit proxy."""
        _identifier(unit_id)
        with self._lock:
            snapshot = copy.deepcopy(self._snapshot)
        if not self._valid_snapshot(snapshot):
            snapshot = self.load()
        unit = next((row for row in snapshot["units"] if row["id"] == unit_id), None)
        if unit is None:
            raise ValueError("Unit does not belong to the loaded TFT set")
        filename = f'builds-{snapshot["_patch_id"]}-{unit_id}.json'
        previous = self._read(filename)
        if not self._valid_build_cache(previous, snapshot, unit_id):
            previous = None
        # Refreshes can share a wall-clock tick. Tie builds to the exact snapshot
        # instead of treating equal fetch timestamps as proof they are current.
        revision = snapshot.get("_snapshot_revision")
        same_snapshot = previous and (
            previous.get("_snapshot_revision") == revision if revision is not None
            else previous["_fetched_at"] >= snapshot["_fetched_at"])
        if (previous and not force and same_snapshot
                and 0 <= time.time() - previous["_fetched_at"] < CACHE_TTL):
            return copy.deepcopy({**previous, "stale": snapshot["stale"] or previous.get("stale", False)})
        try:
            payload = _json(self._get_text(
                f'https://d3.tft.tools/stats2/unit/1100/{unit_id}/{snapshot["_patch_id"]}/1'))
            if (not isinstance(payload, dict) or payload.get("unitId") != unit_id
                    or payload.get("starLevel") != 0 or not isinstance(payload.get("itemTrios"), list)):
                _fail("build statistics do not match the selected unit")
            builds, seen = [], set()
            for row in payload["itemTrios"]:
                ids = row.get("items")
                if not isinstance(ids, list) or len(ids) != 3 or any(i not in snapshot["item_catalog"] for i in ids):
                    _fail("build items do not match the loaded catalog")
                key = tuple(sorted(ids))
                if key in seen:
                    _fail("duplicate item build")
                seen.add(key)
                named = [copy.deepcopy(snapshot["item_catalog"][i]) for i in ids]
                builds.append({
                    **_stats(row, percentages=True), "id": unit_id + ":" + "+".join(key),
                    "name": " + ".join(item["name"] for item in named),
                    "unit": copy.deepcopy(unit), "items": named,
                    "icon_url": unit.get("icon_url"), "scope": "unit_three_item_build",
                    "description": "Three-item build on " + unit["name"] + ". All star levels; item order is not implied.",
                })
            builds.sort(key=lambda row: row["games"], reverse=True)
            result = {key: copy.deepcopy(snapshot[key]) for key in (
                "_schema", "_patch_id", "_set_id", "patch", "set", "source", "updated", "rank", "queue", "stale")}
            result.update({"_snapshot_revision": revision,
                           "_fetched_at": time.time(), "fetched_at": _date(time.time()), "updated_scope": "dataset", "source_url": SITE + "/units/" + unit_id.lower(),
                           "unit": copy.deepcopy(unit), "items": builds, "scope": "unit_three_item_build"})
        except DataUnavailableError as error:
            if previous is None or not error.transient:
                raise
            return copy.deepcopy({**previous, "stale": True})
        self._write(filename, result)
        return copy.deepcopy(result)
