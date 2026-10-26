"""On-demand, cached ARAMKit statistics. No account or game data is uploaded."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
import time
import unicodedata
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


DATA_BASE = "https://data.aramkit.com"
SITE_BASE = "https://aramkit.com/en-US"
MANIFEST_URL = f"{DATA_BASE}/data/versions.json"
MANIFEST_TTL = 24 * 60 * 60
RETRY_DELAY = 60
REQUEST_TIMEOUT = 8
MAX_RESPONSE_BYTES = 16 * 1024 * 1024


class DataUnavailableError(RuntimeError):
    """A source response could not be fetched or safely interpreted."""

    def __init__(self, message: str, *, transient: bool = False):
        super().__init__(message)
        self.transient = transient


def _identifier(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError("Expected a positive numeric game ID")
    value = str(value)
    if not re.fullmatch(r"[1-9][0-9]{0,9}", value):
        raise ValueError("Expected a positive numeric game ID")
    return value


def _normal_name(value: str) -> str:
    value = re.sub(r"^game_character_displayname_", "", value, flags=re.I)
    return "".join(
        ch for ch in unicodedata.normalize("NFKD", value).casefold()
        if ch.isalnum()
    )


def resolve_champion_id(value: object, champions: dict) -> str | None:
    """Resolve an ID, display name, Riot ID, or Live Client rawChampionName."""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    text = str(value).strip()
    if text in champions:
        return text
    target = _normal_name(text)
    if not target:
        return None
    matches = {
        str(champion_id)
        for champion_id, champion in champions.items()
        for key in ("name", "riotId", "slug")
        if isinstance(champion.get(key), str)
        and _normal_name(champion[key]) == target
    }
    return next(iter(matches)) if len(matches) == 1 else None


def _reject_constant(value: str):
    raise ValueError(f"Non-finite JSON number: {value}")


def _rate(value: object, *, optional: bool = False) -> float | None:
    if optional and value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not 0 <= value <= 1
        or not math.isfinite(value)
    ):
        raise DataUnavailableError("ARAMKit returned an invalid win rate")
    return float(value)


def _count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise DataUnavailableError("ARAMKit returned an invalid sample count")
    return value


def _version_row(row: object) -> dict:
    if not isinstance(row, dict):
        raise DataUnavailableError("Invalid ARAMKit version metadata")
    for key, prefix in (("dataPath", "data/"), ("resourcePath", "resources/")):
        path = row.get(key)
        if not isinstance(path, str) or not re.fullmatch(
            re.escape(prefix) + r"[A-Za-z0-9][A-Za-z0-9._-]*", path
        ) or ".." in path:
            raise DataUnavailableError("Invalid ARAMKit data path")
    if not isinstance(row.get("version"), str) or not re.fullmatch(
        r"[0-9]+\.[0-9]+", row["version"]
    ):
        raise DataUnavailableError("Invalid ARAMKit version")
    if not isinstance(row.get("dataDate"), str) or not re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}", row["dataDate"]
    ):
        raise DataUnavailableError("Invalid ARAMKit data date")
    return row


def _manifest(data: object) -> dict:
    if not isinstance(data, dict) or not isinstance(data.get("versions"), list):
        raise DataUnavailableError("Invalid ARAMKit version manifest")
    rows = [_version_row(row) for row in data["versions"]]
    if not any(row["version"] == data.get("latest") for row in rows):
        raise DataUnavailableError("ARAMKit's latest version is missing")
    return data


def _catalog_resource(data: object) -> dict:
    if not isinstance(data, dict) or not data:
        raise DataUnavailableError("Invalid ARAMKit resource catalog")
    for key, row in data.items():
        try:
            _identifier(key)
        except ValueError as error:
            raise DataUnavailableError("Invalid ARAMKit resource ID") from error
        if not isinstance(row, dict) or not isinstance(row.get("name"), str):
            raise DataUnavailableError("Invalid ARAMKit resource name")
    return data


def _champion_resources(data: object) -> dict:
    data = _catalog_resource(data)
    if any(not row["name"].strip() for row in data.values()):
        raise DataUnavailableError("ARAMKit champion names are missing")
    return data


def _champion_rankings(data: object) -> dict:
    """Validate the single aggregate ARAM Mayhem champion dataset."""
    if not isinstance(data, dict) or not isinstance(data.get("rows"), list):
        raise DataUnavailableError("Invalid ARAMKit champion rankings")
    seen = set()
    for row in data["rows"]:
        if not isinstance(row, dict):
            raise DataUnavailableError("Invalid ARAMKit champion ranking row")
        try:
            champion_id = _identifier(row.get("id"))
        except ValueError as error:
            raise DataUnavailableError("Invalid ARAMKit champion ranking ID") from error
        if champion_id in seen:
            raise DataUnavailableError("Duplicate ARAMKit champion ranking")
        seen.add(champion_id)
        has_data = row.get("hasData", True)
        if type(has_data) is not bool:
            raise DataUnavailableError("Invalid ARAMKit champion data availability")
        _count(row.get("sampleCount", 0 if not has_data else None))
        _rate(row.get("winRate"), optional=True)
    return data


def _champion_icon_url(value: object, champion_id: str) -> str | None:
    """Accept only this champion's content-addressed ARAMKit portrait."""
    if not isinstance(value, str):
        return None
    prefix = f"{DATA_BASE}/"
    path = value[len(prefix):] if value.startswith(prefix) else value
    if not re.fullmatch(r"assets/champion/" + re.escape(champion_id) + r"-[0-9a-f]{12}\.webp", path):
        return None
    return prefix + path


def _named_champion_rates(data: dict, resources: dict) -> dict[str, dict]:
    observed = {str(row["id"]): row for row in data["rows"]}
    if any(champion_id not in resources for champion_id in observed):
        raise DataUnavailableError("ARAMKit champion names do not match the rankings")
    result = {}
    for champion_id, resource in resources.items():
        row = observed.get(champion_id)
        count = row.get("sampleCount", 0) if row else 0
        rate = row.get("winRate") if row and row.get("hasData", True) and count else None
        result[champion_id] = {
            "champion_id": champion_id, "name": resource["name"],
            "icon_url": _champion_icon_url(resource.get("icon"), champion_id),
            "win_rate": float(rate) if rate is not None else None,
            "sample_count": count, "missing": rate is None, "low_sample": count < 200,
        }
    return result


def _detail(data: object, champion_id: str) -> dict:
    if not isinstance(data, dict) or not isinstance(data.get("champion"), dict):
        raise DataUnavailableError("Invalid ARAMKit champion response")
    if str(data["champion"].get("id")) != champion_id:
        raise DataUnavailableError("ARAMKit returned a different champion")
    augments = data.get("augments")
    if not isinstance(augments, dict) or not isinstance(augments.get("all"), list):
        raise DataUnavailableError("ARAMKit champion augments are missing")
    stages = augments.get("stages", {})
    if not isinstance(stages, dict):
        raise DataUnavailableError("Invalid ARAMKit augment stages")
    for rows in [augments["all"], *stages.values()]:
        if not isinstance(rows, list):
            raise DataUnavailableError("Invalid ARAMKit augment rows")
        seen = set()
        for row in rows:
            if not isinstance(row, dict):
                raise DataUnavailableError("Invalid ARAMKit augment row")
            try:
                row_id = _identifier(row.get("id"))
            except ValueError as error:
                raise DataUnavailableError("Invalid ARAMKit augment ID") from error
            if row_id in seen:
                raise DataUnavailableError("Duplicate ARAMKit augment row")
            seen.add(row_id)
            _count(row.get("sampleCount"))
            _rate(row.get("winRate"))
            _rate(row.get("augmentWinRate"), optional=True)
    return data


def _summoner_pairs(detail: dict) -> list[dict]:
    """Validate paired observations without attributing them to either spell."""
    rows = detail.get("summoners", [])
    if not isinstance(rows, list):
        raise DataUnavailableError("Invalid ARAMKit summoner spell pairs")
    parsed, seen = [], set()
    for row in rows:
        if not isinstance(row, dict):
            raise DataUnavailableError("Invalid ARAMKit summoner spell row")
        spells = row.get("spells")
        if not isinstance(spells, list) or len(spells) != 2:
            raise DataUnavailableError("ARAMKit summoner statistics must describe a pair")
        try:
            ids = [_identifier(spell.get("id")) for spell in spells if isinstance(spell, dict)]
        except ValueError as error:
            raise DataUnavailableError("Invalid ARAMKit summoner spell ID") from error
        if len(ids) != 2 or ids[0] == ids[1]:
            raise DataUnavailableError("Invalid ARAMKit summoner spell pair")
        pair = tuple(sorted(ids))
        if pair in seen:
            raise DataUnavailableError("Duplicate ARAMKit summoner spell pair")
        seen.add(pair)
        count = _count(row.get("sampleCount"))
        win_rate = _rate(row.get("winRate"), optional=True)
        pick_rate = _rate(row.get("pickRate"), optional=True)
        parsed.append({
            "spell_ids": ids,
            "win_rate": win_rate if count else None,
            "sample_count": count,
            "pick_rate": pick_rate,
            "low_sample": count < 200,
        })
    return parsed


def _summoner_resource(data: object) -> dict:
    data = _catalog_resource(data)
    if any(not row["name"].strip() for row in data.values()):
        raise DataUnavailableError("ARAMKit summoner spell names are missing")
    return data


def _named_summoner_pairs(rows: list[dict], resources: dict) -> list[dict]:
    named = []
    for row in rows:
        if any(spell_id not in resources for spell_id in row["spell_ids"]):
            raise DataUnavailableError("ARAMKit summoner spell names do not match the statistics")
        named.append({**row, "spell_names": [resources[spell_id]["name"] for spell_id in row["spell_ids"]]})
    return named


def _build_stats(row: object) -> dict:
    if not isinstance(row, dict):
        raise DataUnavailableError("Invalid ARAMKit item build row")
    count = _count(row.get("sampleCount"))
    win_rate = _rate(row.get("winRate"), optional=True)
    rank = row.get("rank")
    if rank is not None and (isinstance(rank, bool) or not isinstance(rank, int) or rank < 1):
        raise DataUnavailableError("Invalid ARAMKit item build rank")
    return {
        "rank": rank,
        "sample_count": count,
        "win_rate": win_rate if count else None,
        "pick_rate": _rate(row.get("pickRate"), optional=True),
        "low_sample": count < 200,
    }


def _core_item_ids(items: object) -> list[str]:
    if not isinstance(items, list) or len(items) != 3:
        raise DataUnavailableError("ARAMKit core builds must contain three items")
    try:
        ids = [_identifier(item.get("id")) for item in items if isinstance(item, dict)]
    except ValueError as error:
        raise DataUnavailableError("Invalid ARAMKit item ID") from error
    if len(ids) != 3:
        raise DataUnavailableError("Invalid ARAMKit core build items")
    return ids


def _later_item_rows(route: dict) -> list[dict]:
    """Later-item observations are conditional on this exact purchase route."""
    source = route.get("laterItems", [])
    if not isinstance(source, list):
        raise DataUnavailableError("Invalid ARAMKit follow-up items")
    rows, seen = [], set()
    for row in source:
        stats = _build_stats(row)
        item = row.get("item")
        try:
            item_id = _identifier(item.get("id") if isinstance(item, dict) else None)
        except ValueError as error:
            raise DataUnavailableError("Invalid ARAMKit follow-up item ID") from error
        if item_id in seen:
            raise DataUnavailableError("Duplicate ARAMKit follow-up item")
        seen.add(item_id)
        rows.append({**stats, "item_ids": [item_id], "pick_rate_scope": "purchase_route"})
    return rows


def _item_build_groups(detail: dict) -> list[dict]:
    """Keep core-set rates separate from their ordered purchase-route rates."""
    builds = detail.get("builds", {})
    if not isinstance(builds, dict):
        raise DataUnavailableError("Invalid ARAMKit item builds")
    filtered = builds.get("filtered", {})
    if not isinstance(filtered, dict):
        raise DataUnavailableError("Invalid ARAMKit filtered item builds")
    archetypes = filtered.get("archetypes", [])
    if not isinstance(archetypes, list):
        raise DataUnavailableError("Invalid ARAMKit build groups")
    groups, seen_groups = [], set()
    for archetype in archetypes:
        stats = _build_stats(archetype)
        key = archetype.get("key")
        if not isinstance(key, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", key):
            raise DataUnavailableError("Invalid ARAMKit build group key")
        if key in seen_groups:
            raise DataUnavailableError("Duplicate ARAMKit build group")
        seen_groups.add(key)
        profiles = archetype.get("profiles", [])
        if not isinstance(profiles, list):
            raise DataUnavailableError("Invalid ARAMKit core build profiles")
        rows, seen_sets = [], set()
        for profile in profiles:
            profile_stats = _build_stats(profile)
            ids = _core_item_ids(profile.get("itemSet"))
            item_set = tuple(sorted(ids))
            if item_set in seen_sets:
                raise DataUnavailableError("Duplicate ARAMKit core build")
            seen_sets.add(item_set)
            source_routes = profile.get("routes", [])
            if not isinstance(source_routes, list):
                raise DataUnavailableError("Invalid ARAMKit purchase routes")
            routes, seen_routes = [], set()
            for route in source_routes:
                route_stats = _build_stats(route)
                route_ids = _core_item_ids(route.get("purchaseOrder"))
                if tuple(sorted(route_ids)) != item_set:
                    raise DataUnavailableError("ARAMKit purchase route differs from its core build")
                if tuple(route_ids) in seen_routes:
                    raise DataUnavailableError("Duplicate ARAMKit purchase route")
                seen_routes.add(tuple(route_ids))
                routes.append({
                    **route_stats, "item_ids": route_ids, "ordered": True,
                    "pick_rate_scope": "core_set",
                    "later_items": _later_item_rows(route),
                })
            rows.append({
                **profile_stats, "item_ids": ids, "ordered": False,
                "pick_rate_scope": "build_group", "routes": routes,
            })
        groups.append({
            **stats, "key": key,
            "label": key.upper() if key in ("ap", "ad") else key.replace("_", " ").title(),
            "rows": rows,
        })
    return groups


def _item_resource(data: object) -> dict:
    # ARAMKit includes the synthetic "0: No Boots" resource. Core IDs remain positive.
    if not isinstance(data, dict) or not data:
        raise DataUnavailableError("Invalid ARAMKit item resource catalog")
    for key, row in data.items():
        try:
            if key != "0":
                _identifier(key)
        except ValueError as error:
            raise DataUnavailableError("Invalid ARAMKit item resource ID") from error
        if not isinstance(row, dict) or not isinstance(row.get("name"), str) or not row["name"].strip():
            raise DataUnavailableError("ARAMKit item names are missing")
    return data


def _item_catalog(resources: dict) -> dict:
    """Classify explicitly final items and only reciprocal pure transforms."""
    catalog = {}
    for item_id, resource in resources.items():
        categories = resource.get("categories")
        known_categories = isinstance(categories, list) and all(isinstance(value, str) for value in categories)
        catalog[item_id] = {
            "name": resource["name"],
            "is_final": (
                resource.get("isFinal") is True and known_categories
                and not any(value in categories for value in ("Consumable", "Trinket"))
            ),
            "is_boots": known_categories and "Boots" in categories,
            "canonical_id": item_id,
        }
    for item_id, resource in resources.items():
        parents = resource.get("from")
        if (
            resource.get("acquisition") != "transform"
            or resource.get("purchasable") is not False
            or not catalog[item_id]["is_final"]
            or not isinstance(parents, list) or len(parents) != 1
        ):
            continue
        try:
            parent_id = _identifier(parents[0])
            parent = resources.get(parent_id, {})
            targets = parent.get("to")
            if not isinstance(targets, list) or item_id not in [_identifier(value) for value in targets]:
                continue
        except ValueError:
            continue
        if (
            parent_id != item_id and parent_id in catalog
            and catalog[parent_id]["is_final"]
            and parent.get("purchasable") is True
            and parent.get("acquisition") == "shop"
            and catalog[parent_id]["is_boots"] == catalog[item_id]["is_boots"]
        ):
            catalog[item_id]["canonical_id"] = parent_id
    return catalog


def _item_icon_url(value: object) -> str | None:
    """Resolve optional content-addressed icons; reject every other URL/path."""
    if not isinstance(value, str):
        return None
    prefix = f"{DATA_BASE}/"
    path = value[len(prefix):] if value.startswith(prefix) else value
    if not re.fullmatch(r"assets/item/[A-Za-z0-9][A-Za-z0-9_-]*-[0-9a-f]{12}\.webp", path):
        return None
    return prefix + path


def _named_item_builds(groups: list[dict], resources: dict) -> list[dict]:
    groups = copy.deepcopy(groups)
    for group in groups:
        for profile in group["rows"]:
            rows = [profile]
            for route in profile["routes"]:
                rows.extend([route, *route["later_items"]])
            for row in rows:
                if any(item_id not in resources for item_id in row["item_ids"]):
                    raise DataUnavailableError("ARAMKit item names do not match the statistics")
                row["item_names"] = [resources[item_id]["name"] for item_id in row["item_ids"]]
                row["item_icon_urls"] = [
                    _item_icon_url(resources[item_id].get("icon")) for item_id in row["item_ids"]
                ]
    return groups


class StatsProvider:
    """Public ARAMKit data with daily discovery and immutable snapshot caching.

    Fetches catalog resources, aggregate rankings, and requested champion details.
    A stale fallback is
    allowed only for transient transport failures, never for malformed responses
    or explicit HTTP denial/not-found responses. Missing champion statistics are
    never replaced with an augment's global win rate.
    """

    def __init__(self, cache_dir: Path):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._catalog: dict | None = None
        self._catalog_until = 0.0
        self._active_version: dict | None = None
        self._detail_meta: dict[tuple[str, str], dict] = {}
        self._details: dict[tuple[str, str], dict] = {}
        self._summoner_fallbacks: dict[tuple[str, str], dict] = {}
        self._item_fallbacks: dict[tuple[str, str], dict] = {}
        self._champion_snapshots: dict[str, dict] = {}

    def _cache_path(self, url: str) -> Path:
        return self.cache_dir / (hashlib.sha256(url.encode()).hexdigest() + ".json")

    def _read_json(self, path: Path):
        try:
            if path.stat().st_size > MAX_RESPONSE_BYTES:
                return None
            return json.loads(path.read_text(encoding="utf-8"), parse_constant=_reject_constant)
        except (OSError, ValueError, RecursionError):
            return None

    def _write_json(self, path: Path, data: object):
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.cache_dir,
                prefix=".download-", suffix=".tmp", delete=False,
            ) as handle:
                temporary = Path(handle.name)
                json.dump(data, handle, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            os.replace(temporary, path)
        except OSError:
            # Statistics remain usable if the local disk temporarily cannot cache.
            pass
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def _cached(self, url: str, validator):
        cached = self._read_json(self._cache_path(url))
        if not isinstance(cached, dict) or cached.get("url") != url:
            return None
        stamp = cached.get("fetched_at")
        if (
            isinstance(stamp, bool) or not isinstance(stamp, (int, float))
            or not 0 <= stamp <= time.time() + 300 or not math.isfinite(stamp)
        ):
            return None
        try:
            validator(cached.get("data"))
        except DataUnavailableError:
            return None
        return cached

    def _download(self, url: str):
        request = Request(url, headers={"Accept": "application/json", "User-Agent": "MayhemCompanion/0.1"})
        try:
            with urlopen(request, timeout=REQUEST_TIMEOUT) as response:
                payload = response.read(MAX_RESPONSE_BYTES + 1)
        except HTTPError as error:
            transient = error.code in (408, 425, 429) or error.code >= 500
            raise DataUnavailableError(
                f"ARAMKit request failed (HTTP {error.code})", transient=transient,
            ) from error
        except (URLError, TimeoutError, ConnectionError, OSError) as error:
            raise DataUnavailableError("ARAMKit is temporarily unreachable", transient=True) from error
        if len(payload) > MAX_RESPONSE_BYTES:
            raise DataUnavailableError("ARAMKit response exceeds the size limit")
        try:
            return json.loads(payload, parse_constant=_reject_constant)
        except (ValueError, UnicodeError, RecursionError) as error:
            raise DataUnavailableError("ARAMKit returned invalid JSON") from error

    def _fetch(self, url: str, validator, *, ttl: int | None = None):
        cached = self._cached(url, validator)
        if cached and (ttl is None or 0 <= time.time() - cached["fetched_at"] < ttl):
            return cached["data"], False
        try:
            data = validator(self._download(url))
        except DataUnavailableError as error:
            if error.transient and cached:
                return cached["data"], True
            raise
        self._write_json(self._cache_path(url), {"url": url, "fetched_at": time.time(), "data": data})
        return data, False

    @staticmethod
    def _resource_url(version: dict, kind: str) -> str:
        return f"{DATA_BASE}/{version['resourcePath']}/en-US/resources/{kind}.json"

    @staticmethod
    def _detail_url(version: dict, champion_id: str, bracket: str) -> str:
        return f"{DATA_BASE}/{version['dataPath']}/stats/{bracket}/champion-details/{champion_id}.json"

    @staticmethod
    def _champion_rankings_url(version: dict, bracket: str) -> str:
        return f"{DATA_BASE}/{version['dataPath']}/stats/{bracket}/champion-rankings.json"

    @staticmethod
    def _metadata(version: dict, stale: bool) -> dict:
        return {"source": "ARAMKit", "version": version["version"], "data_date": version["dataDate"], "stale": stale}

    def _last_catalog(self) -> tuple[dict, dict] | None:
        version = self._read_json(self.cache_dir / "last-catalog.json")
        try:
            _version_row(version)
        except DataUnavailableError:
            return None
        resources = {}
        for kind in ("champions", "augments"):
            cached = self._cached(self._resource_url(version, kind), _catalog_resource)
            if not cached:
                return None
            resources[kind] = cached["data"]
        return {**resources, **self._metadata(version, True)}, version

    def _ensure_catalog(self) -> dict:
        if self._catalog is not None and time.time() < self._catalog_until:
            return self._catalog
        try:
            manifest, stale = self._fetch(MANIFEST_URL, _manifest, ttl=MANIFEST_TTL)
            version = next(row for row in manifest["versions"] if row["version"] == manifest["latest"])
            resources = {}
            for kind in ("champions", "augments"):
                resources[kind], resource_stale = self._fetch(self._resource_url(version, kind), _catalog_resource)
                stale = stale or resource_stale
            catalog = {**resources, **self._metadata(version, stale)}
            self._write_json(self.cache_dir / "last-catalog.json", version)
        except DataUnavailableError as error:
            fallback = self._last_catalog() if error.transient else None
            if fallback is None:
                raise
            catalog, version = fallback
        self._catalog = catalog
        self._active_version = version
        if catalog["stale"]:
            self._catalog_until = time.time() + RETRY_DELAY
        else:
            cached_manifest = self._cached(MANIFEST_URL, _manifest)
            fetched_at = cached_manifest["fetched_at"] if cached_manifest else time.time()
            self._catalog_until = fetched_at + MANIFEST_TTL
        return catalog

    def load_catalog(self) -> dict:
        """Return names/IDs and source metadata; this does not download champions."""
        return copy.deepcopy(self._ensure_catalog())

    def resolve_champion_id(self, name: object) -> str | None:
        return resolve_champion_id(name, self._ensure_catalog()["champions"])

    def source_url(self, champion_id: object) -> str:
        champion_id = _identifier(champion_id)
        champion = self._ensure_catalog()["champions"].get(champion_id, {})
        slug = champion.get("slug")
        if isinstance(slug, str) and re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug):
            return f"{SITE_BASE}/champions/{quote(slug, safe='')}"
        return f"{SITE_BASE}/champions"

    def _last_champion_rates(self, bracket: str) -> tuple[dict, dict] | None:
        """Recover rates, names, and portraits from one complete older snapshot."""
        version = self._read_json(self.cache_dir / f"last-champion-rates-{bracket}.json")
        try:
            _version_row(version)
        except DataUnavailableError:
            return None
        rankings = self._cached(self._champion_rankings_url(version, bracket), _champion_rankings)
        resources = self._cached(self._resource_url(version, "champions"), _champion_resources)
        if rankings is None or resources is None:
            return None
        try:
            return _named_champion_rates(rankings["data"], resources["data"]), version
        except DataUnavailableError:
            return None

    def champion_rates_for(self, champion_ids: object, bracket: str = "all") -> dict:
        """Return overall Mayhem champion rates in deduplicated requested order.

        A single aggregate ranking file supplies every champion; changing the
        selection never downloads individual champion details. sample_count is
        champion appearances, not the total number of distinct matches. The
        source's all/high brackets remain separate, and missing observations
        never fall back to another bracket, mode, or an earlier individual row.
        Names, icons, and rates always belong to the same version snapshot.
        """
        if not isinstance(champion_ids, (list, tuple)):
            raise ValueError("Champion IDs must be a list or tuple")
        ids = list(dict.fromkeys(_identifier(value) for value in champion_ids))
        if bracket not in ("all", "high"):
            raise ValueError("Bracket must be 'all' or 'high'")
        cached = self._champion_snapshots.get(bracket)
        if cached is not None and time.time() < cached["until"]:
            if "error" in cached:
                raise DataUnavailableError(cached["error"], transient=True)
            snapshot = cached["snapshot"]
        else:
            try:
                manifest, stale = self._fetch(MANIFEST_URL, _manifest, ttl=MANIFEST_TTL)
                version = next(row for row in manifest["versions"] if row["version"] == manifest["latest"])
                rankings, rankings_stale = self._fetch(
                    self._champion_rankings_url(version, bracket), _champion_rankings,
                )
                resources, resources_stale = self._fetch(
                    self._resource_url(version, "champions"), _champion_resources,
                )
                rows = _named_champion_rates(rankings, resources)
                stale = stale or rankings_stale or resources_stale
                self._write_json(self.cache_dir / f"last-champion-rates-{bracket}.json", version)
            except DataUnavailableError as error:
                previous = self._last_champion_rates(bracket) if error.transient else None
                if previous is None:
                    if error.transient:
                        self._champion_snapshots[bracket] = {
                            "error": str(error), "until": time.time() + RETRY_DELAY,
                        }
                    raise
                rows, version = previous
                stale = True
            snapshot = {
                **self._metadata(version, stale),
                "source_url": f"{SITE_BASE}/champions" + ("?dataset=high" if bracket == "high" else ""),
                "scope": "champion", "game_mode": "ARAM Mayhem", "bracket": bracket,
                "sample_scope": "champion_appearances", "by_id": rows,
            }
            cached_manifest = self._cached(MANIFEST_URL, _manifest)
            fetched_at = cached_manifest["fetched_at"] if cached_manifest else time.time()
            self._champion_snapshots[bracket] = {
                "snapshot": snapshot,
                "until": time.time() + RETRY_DELAY if stale else fetched_at + MANIFEST_TTL,
            }
        rows = [snapshot["by_id"].get(champion_id, {
            "champion_id": champion_id, "name": f"Champion {champion_id}",
            "icon_url": None, "win_rate": None, "sample_count": 0,
            "missing": True, "low_sample": True,
        }) for champion_id in ids]
        return copy.deepcopy({**{key: value for key, value in snapshot.items() if key != "by_id"}, "rows": rows})

    def champion_detail(self, champion_id: object, bracket: str = "all") -> dict:
        """Return raw champion-specific JSON, falling back only on transport failure."""
        champion_id = _identifier(champion_id)
        if bracket not in ("all", "high"):
            raise ValueError("Bracket must be 'all' or 'high'")
        catalog = self._ensure_catalog()
        version = self._active_version
        assert version is not None
        url = self._detail_url(version, champion_id, bracket)
        key = (champion_id, bracket)
        previous_result = self._details.get(key)
        if (
            previous_result is not None and previous_result["requested_url"] == url
            and time.time() < previous_result["retry_at"]
        ):
            self._detail_meta[key] = self._metadata(
                previous_result["version"], catalog["stale"] or previous_result["stale"],
            )
            return copy.deepcopy(previous_result["data"])
        validator = lambda data: _detail(data, champion_id)
        marker_path = self.cache_dir / f"last-champion-{champion_id}-{bracket}.json"
        try:
            data, stale = self._fetch(url, validator)
            used_version = version
            self._write_json(marker_path, version)
        except DataUnavailableError as error:
            previous = self._read_json(marker_path) if error.transient else None
            try:
                _version_row(previous)
            except DataUnavailableError:
                raise error
            cached = self._cached(self._detail_url(previous, champion_id, bracket), validator)
            if cached is None:
                raise error
            data = cached["data"]
            used_version, stale = previous, True
        self._details[key] = {
            "requested_url": url, "version": used_version, "data": data,
            "stale": stale, "retry_at": time.time() + RETRY_DELAY if stale else math.inf,
        }
        self._detail_meta[key] = self._metadata(used_version, catalog["stale"] or stale)
        return copy.deepcopy(data)

    def rate_for(self, champion_id: object, augment_id: object, stage: int | None = None, *, bracket: str = "all") -> dict:
        """A selected champion's rate, or None if that precise statistic is absent."""
        champion_id, augment_id = _identifier(champion_id), _identifier(augment_id)
        if stage is not None and (isinstance(stage, bool) or not isinstance(stage, int) or stage not in (1, 2, 3, 4)):
            raise ValueError("Stage must be None or an integer from 1 to 4")
        detail = self.champion_detail(champion_id, bracket)
        rows = detail["augments"]["all"] if stage is None else detail["augments"].get("stages", {}).get(str(stage), [])
        row = next((item for item in rows if str(item["id"]) == augment_id), None)
        count = _count(row["sampleCount"]) if row else 0
        return {
            **self._detail_meta[(champion_id, bracket)],
            "champion_id": champion_id,
            "augment_id": augment_id,
            "scope": "champion",
            "stage": stage,
            "bracket": bracket,
            "win_rate": _rate(row["winRate"]) if row and count else None,
            "sample_count": count,
            "global_win_rate": _rate(row.get("augmentWinRate"), optional=True) if row else None,
            "tier": row.get("tier") if row else None,
            "rank": row.get("rank") if row else None,
            "missing": row is None or count == 0,
            "low_sample": count < 200,
            "source_url": self.source_url(champion_id),
        }

    def _last_summoner_pairs(self, champion_id: str, bracket: str) -> tuple[list[dict], dict] | None:
        """Load an entire earlier snapshot; never attach old names to new rates."""
        version = self._read_json(self.cache_dir / f"last-summoners-{champion_id}-{bracket}.json")
        try:
            _version_row(version)
        except DataUnavailableError:
            return None
        cached = self._cached(
            self._detail_url(version, champion_id, bracket),
            lambda data: _detail(data, champion_id),
        )
        if cached is None:
            return None
        try:
            rows = _summoner_pairs(cached["data"])
            if not rows:
                return [], version
            resources = self._cached(self._resource_url(version, "summoner-spells"), _summoner_resource)
            if resources is None:
                return None
            return _named_summoner_pairs(rows, resources["data"]), version
        except DataUnavailableError:
            return None

    def summoner_spells_for(self, champion_id: object, bracket: str = "all") -> dict:
        """Return champion-specific spell-pair rates and their matching names.

        Spell resources are fetched only by this method and use the exact
        resource version associated with the champion statistics. An empty list
        means no pair observations; unavailable or malformed data raises instead.
        """
        champion_id = _identifier(champion_id)
        detail = self.champion_detail(champion_id, bracket)
        key = (champion_id, bracket)
        version = self._details[key]["version"]
        requested_url = self._detail_url(version, champion_id, bracket)
        fallback = self._summoner_fallbacks.get(key)
        if (
            fallback is not None and fallback["requested_url"] == requested_url
            and time.time() < fallback["retry_at"]
        ):
            return copy.deepcopy(fallback["result"])

        rows = _summoner_pairs(detail)
        stale = self._detail_meta[key]["stale"]
        used_version = version
        try:
            if rows:
                resources, resource_stale = self._fetch(
                    self._resource_url(version, "summoner-spells"), _summoner_resource,
                )
                rows = _named_summoner_pairs(rows, resources)
                stale = stale or resource_stale
            self._write_json(self.cache_dir / f"last-summoners-{champion_id}-{bracket}.json", version)
            self._summoner_fallbacks.pop(key, None)
        except DataUnavailableError as error:
            previous = self._last_summoner_pairs(champion_id, bracket) if error.transient else None
            if previous is None:
                raise
            rows, used_version = previous
            stale = True

        result = {
            **self._metadata(used_version, stale),
            "source_url": self.source_url(champion_id),
            "champion_id": champion_id,
            "scope": "champion",
            "bracket": bracket,
            "rows": rows,
        }
        if used_version != version:
            self._summoner_fallbacks[key] = {
                "requested_url": requested_url, "retry_at": time.time() + RETRY_DELAY,
                "result": copy.deepcopy(result),
            }
        return result

    def _last_item_builds(self, champion_id: str, bracket: str) -> tuple[list[dict], dict, dict] | None:
        version = self._read_json(self.cache_dir / f"last-items-{champion_id}-{bracket}.json")
        try:
            _version_row(version)
        except DataUnavailableError:
            return None
        cached = self._cached(
            self._detail_url(version, champion_id, bracket),
            lambda data: _detail(data, champion_id),
        )
        if cached is None:
            return None
        try:
            groups = _item_build_groups(cached["data"])
            if not any(group["rows"] for group in groups):
                return groups, {}, version
            resources = self._cached(self._resource_url(version, "items"), _item_resource)
            if resources is None:
                return None
            return _named_item_builds(groups, resources["data"]), _item_catalog(resources["data"]), version
        except DataUnavailableError:
            return None

    def item_builds_for(self, champion_id: object, bracket: str = "all") -> dict:
        """Return champion-specific core sets grouped by build type.

        Core rows are unordered three-item sets; nested routes retain actual
        purchase order and their own observations. Their pick rates refer to
        the build group and core set respectively. Route later_items are
        individual follow-up rates, never joint six-item rates. Only ARAMKit's filtered
        builds are used, with matching item names from the same snapshot.
        Missing observations produce empty groups; malformed/unavailable data
        raises. Transport fallback keeps an entire older snapshot and its date.
        """
        champion_id = _identifier(champion_id)
        detail = self.champion_detail(champion_id, bracket)
        key = (champion_id, bracket)
        version = self._details[key]["version"]
        requested_url = self._detail_url(version, champion_id, bracket)
        fallback = self._item_fallbacks.get(key)
        if (
            fallback is not None and fallback["requested_url"] == requested_url
            and time.time() < fallback["retry_at"]
        ):
            return copy.deepcopy(fallback["result"])

        groups = _item_build_groups(detail)
        item_catalog = {}
        stale = self._detail_meta[key]["stale"]
        used_version = version
        try:
            if any(group["rows"] for group in groups):
                resources, resource_stale = self._fetch(
                    self._resource_url(version, "items"), _item_resource,
                )
                groups = _named_item_builds(groups, resources)
                item_catalog = _item_catalog(resources)
                stale = stale or resource_stale
            self._write_json(self.cache_dir / f"last-items-{champion_id}-{bracket}.json", version)
            self._item_fallbacks.pop(key, None)
        except DataUnavailableError as error:
            previous = self._last_item_builds(champion_id, bracket) if error.transient else None
            if previous is None:
                raise
            groups, item_catalog, used_version = previous
            stale = True

        result = {
            **self._metadata(used_version, stale),
            "source_url": self.source_url(champion_id),
            "champion_id": champion_id,
            "scope": "champion",
            "bracket": bracket,
            "build_filter": "filtered",
            "groups": groups,
            "item_catalog": item_catalog,
        }
        if used_version != version:
            self._item_fallbacks[key] = {
                "requested_url": requested_url, "retry_at": time.time() + RETRY_DELAY,
                "result": copy.deepcopy(result),
            }
        return result
