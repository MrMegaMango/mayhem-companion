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


class StatsProvider:
    """Public ARAMKit data with daily discovery and immutable snapshot caching.

    Fetches only catalog resources and requested champions. A stale fallback is
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
