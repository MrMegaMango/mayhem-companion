"""On-demand Arena statistics with a champion-specific Lolalytics fallback.

The source's own ``Win Rate`` label is preserved: it does not define its finish
cutoff on these pages. Items are individual observations, never joint builds.
Global augment observations provide names only, never substitute champion rates.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
from html.parser import HTMLParser
import json
import math
import os
from pathlib import Path
import re
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .data import DataUnavailableError
from .matching import normalize_title


BASE = "https://winrate.lol"
INDEX_URL = BASE + "/en/arena"
VERSIONS_URL = "https://ddragon.leagueoflegends.com/api/versions.json"
MAX_BYTES = 3_000_000
TTL = 3600
RETRY_DELAY = 60
_VOID = frozenset(("area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"))


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = build_opener(_NoRedirect())


def _reference_names():
    """Bundled Riot English names only; never rates or availability predictions."""
    try:
        path = Path(__file__).with_name("arena_augments.json")
        if path.stat().st_size > 200_000:
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("reference_only") is not True or not isinstance(data.get("augments"), dict):
            return {}
        result, names = {}, set()
        for ident, row in data["augments"].items():
            ident = _id(ident)
            name = row.get("name")
            if (row.get("id") != ident or not isinstance(name, str) or not 1 <= len(name) <= 120
                    or normalize_title(name) in names):
                return {}
            names.add(normalize_title(name))
            result[ident] = {"id": ident, "name": name, "rarity": row.get("rarity", ""),
                             "description": "", "icon_url": None}
        return result
    except (OSError, ValueError, TypeError, AttributeError, RecursionError):
        return {}


def _merge_names(observed, reference):
    # Source IDs/names win. Do not introduce two competing OCR IDs for a title.
    observed_names = {normalize_title(row["name"]) for row in observed.values()}
    merged = {key: row for key, row in reference.items()
              if key not in observed and normalize_title(row["name"]) not in observed_names}
    for key, row in observed.items():
        ref = reference.get(key, {})
        if (row.get("rarity") not in ("silver", "gold", "prismatic")
                and ref.get("rarity") in ("silver", "gold", "prismatic")
                and normalize_title(row["name"]) == normalize_title(ref.get("name", ""))):
            # An observed name can omit rarity. Preserve independently verified
            # reference metadata only when both identity and name agree.
            row = {**row, "rarity": ref["rarity"]}
        merged[key] = row
    return merged


def _error(message):
    return DataUnavailableError("Arena source: " + message)


def _id(value):
    value = str(value) if type(value) is int else value
    if not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]{0,9}", value):
        raise ValueError("Invalid Arena identifier")
    return value


class _Node:
    def __init__(self, tag="", attrs=()):
        self.tag, self.attrs, self.children = tag, dict(attrs), []

    def has(self, cls):
        return cls in self.attrs.get("class", "").split()

    def text(self):
        return " ".join("".join(x.text() if isinstance(x, _Node) else x for x in self.children).split())

    def nodes(self, *, cls=None, tag=None):
        result = []
        for child in self.children:
            if isinstance(child, _Node):
                if (cls is None or child.has(cls)) and (tag is None or child.tag == tag):
                    result.append(child)
                result.extend(child.nodes(cls=cls, tag=tag))
        return result

    def direct(self, cls):
        return [x for x in self.children if isinstance(x, _Node) and x.has(cls)]


class _Document(HTMLParser):
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.root = _Node()
        self.stack = [self.root]
        self.count = 0
        try:
            self.feed(html)
            self.close()
        except (RecursionError, AssertionError) as exc:
            raise _error("invalid page structure") from exc

    def handle_starttag(self, tag, attrs):
        self.count += 1
        if self.count > 30000 or len(self.stack) > 100:
            raise _error("page structure exceeds limits")
        node = _Node(tag, attrs)
        self.stack[-1].children.append(node)
        if tag not in _VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, value):
        if self.stack[-1].tag not in ("script", "style"):
            self.stack[-1].children.append(value)


def _one(nodes, message):
    if len(nodes) != 1:
        raise _error(message)
    return nodes[0]


def _number(value, *, rate=False, count=False):
    value = value.strip().replace(",", "")
    pattern = r"[0-9]+" if count else r"[0-9]+(?:\.[0-9]+)?"
    if rate:
        if not value.endswith("%"):
            raise _error("invalid percentage")
        value = value[:-1]
    if not re.fullmatch(pattern, value):
        raise _error("invalid numeric statistic")
    result = int(value) if count else float(value)
    if (not math.isfinite(result) or (rate and result > 100)
            or (count and result > 1_000_000_000)):
        raise _error("statistic out of range")
    return result / 100 if rate else result


def _icon(path, kind):
    patterns = {
        "champion": r"/img/w64/champion/[A-Za-z0-9]+\.jpg",
        "augment": r"/img/augment/[1-9][0-9]{0,9}\.png",
        "item": r"/img/w128/item/[1-9][0-9]{0,9}\.jpg",
    }
    if not isinstance(path, str) or not re.fullmatch(patterns[kind], path):
        raise _error("invalid image resource")
    return BASE + path


def _page(html, url):
    if not isinstance(html, str) or len(html) > MAX_BYTES:
        raise _error("invalid HTML response")
    root = _Document(html).root
    canonical = [n.attrs.get("href") for n in root.nodes(tag="link") if n.attrs.get("rel") == "canonical"]
    if canonical != [url]:
        raise _error("page belongs to another mode or champion")
    title = _one(root.nodes(tag="title"), "missing page title").text()
    match = re.search(r"\bArena\b.*\bPatch ([0-9]{1,3}\.[0-9]{1,2})$", title)
    if not match:
        raise _error("missing Arena patch")
    return root, match[1]


def _metadata(version, url):
    return {
        "source": "winrate.lol", "source_url": url, "game_mode": "Arena",
        "version": version, "data_date": None, "stale": False,
        "win_rate_label": "Win rate",
        "rate_note": "Source win rate; finish cutoff not specified. Items are individual, not full builds.",
        "sample_label": "Arena · source sample (rank/region unspecified)",
    }


def _ddragon(data):
    if not isinstance(data, dict) or not isinstance(data.get("data"), dict):
        raise _error("invalid champion catalog")
    result = {}
    for row in data["data"].values():
        if not isinstance(row, dict):
            raise _error("invalid champion entry")
        try:
            ident = _id(row.get("key"))
        except ValueError as exc:
            raise _error("invalid champion ID") from exc
        name, raw = row.get("name"), row.get("id")
        if (not isinstance(name, str) or not 1 <= len(name) <= 100
                or not isinstance(raw, str) or not re.fullmatch(r"[A-Za-z0-9]{1,40}", raw)
                or ident in result):
            raise _error("invalid champion identity")
        result[ident] = {"id": ident, "name": name, "raw_name": raw, "riotId": raw,
                         "icon_url": _icon(f"/img/w64/champion/{raw}.jpg", "champion")}
    if not result or len(result) > 1000:
        raise _error("empty champion catalog")
    return result


def _observations(root, kind):
    rows = []
    for node in root.nodes(cls="ar-aug-row"):
        image = _one(node.nodes(tag="img"), "ambiguous observation identity")
        path = image.attrs.get("src", "")
        expected = "/img/augment/" if kind == "augment" else "/img/w128/item/"
        if not path.startswith(expected):
            raise _error("observation belongs to another category")
        icon = _icon(path, kind)
        ident = _id(path.rsplit("/", 1)[1].split(".", 1)[0])
        name = image.attrs.get("alt")
        if not isinstance(name, str) or not 1 <= len(name) <= 120:
            raise _error("missing observation name")
        values = node.direct("tl-num")
        if len(values) != 3:
            raise _error("changed observation columns")
        avg = _number(values[0].text())
        rate = _number(values[1].text(), rate=True)
        count = _number(values[2].text(), count=True)
        if not 1 <= avg <= 16:
            raise _error("invalid average placement")
        description = node.nodes(cls="itm-desc")
        rows.append({"id": ident, "name": name, "icon_url": icon,
                     "description": description[0].text()[:4000] if description else "",
                     "win_rate": rate if count else None, "avg_placement": avg if count else None,
                     "sample_count": count, "low_sample": count < 200,
                     "rarity": next((x[4:] for x in image.attrs.get("class", "").split()
                                     if x in ("aug-silver", "aug-gold", "aug-prismatic")), "")})
    if len({row["id"] for row in rows}) != len(rows):
        raise _error("duplicate observation")
    return rows


def parse_catalog(html, champions):
    root, version = _page(html, INDEX_URL)
    by_raw = {row["raw_name"]: key for key, row in champions.items()}
    champions = copy.deepcopy(champions)
    rows = []
    for node in root.nodes(cls="tl-row"):
        if not node.has("ar-head"):
            continue
        href = node.attrs.get("href", "")
        if not re.fullmatch(r"/en/arena/[a-z0-9-]{1,50}", href):
            raise _error("invalid champion link")
        image = _one(node.nodes(tag="img"), "ambiguous champion image")
        path = image.attrs.get("src", "")
        _icon(path, "champion")
        raw = path.rsplit("/", 1)[1][:-4]
        ident = by_raw.get(raw)
        if ident is None:
            raise _error("champion source does not match catalog")
        values = node.direct("tl-num")
        if len(values) != 5:
            raise _error("changed champion columns")
        avg = _number(values[0].text())
        wr, first = (_number(n.text(), rate=True) for n in values[1:3])
        _number(values[3].text(), rate=True)
        count = _number(values[4].text(), count=True)
        if not 1 <= avg <= 16:
            raise _error("invalid champion placement")
        champions[ident]["slug"] = href.rsplit("/", 1)[1]
        rows.append({**champions[ident], "champion_id": ident, "win_rate": wr if count else None,
                     "first_rate": first if count else None, "avg_placement": avg if count else None,
                     "sample_count": count, "low_sample": count < 200, "missing": not bool(count)})
    if not rows or len({row["id"] for row in rows}) != len(rows):
        raise _error("missing or duplicate champion observations")
    # Global rates are deliberately discarded. This dictionary is for OCR names.
    augment_section = _one([node for node in root.nodes(cls="ar-augs")
                            if [h.text() for h in node.nodes(tag="h2")] == ["Augments"]],
                           "missing Arena augment section")
    augments = {row["id"]: {k: row[k] for k in ("id", "name", "description", "icon_url", "rarity")}
                for row in _observations(augment_section, "augment")}
    if not augments:
        raise _error("missing Arena augment names")
    return {**_metadata(version, INDEX_URL), "champions": champions, "augments": augments, "rows": rows,
            "catalog_note": "Observed Arena augment names; unlisted choices remain unrecognized."}


def parse_champion(html, champion, url):
    root, version = _page(html, url)
    name = _one(root.nodes(tag="h1"), "missing champion heading").text()
    if name != champion["name"]:
        raise _error("wrong champion response")
    summary = {}
    labels = {"Avg. Place": "avg_placement", "Win Rate": "win_rate", "1st Place": "first_rate", "Games": "sample_count"}
    for node in root.nodes(cls="ar-stat"):
        label = _one(node.nodes(cls="stat-lbl"), "missing summary label").text()
        value = _one(node.nodes(cls="stat-val"), "missing summary value").text()
        if label not in labels or labels[label] in summary:
            raise _error("changed summary columns")
        key = labels[label]
        summary[key] = _number(value, rate=key.endswith("rate"), count=key == "sample_count")
    if set(summary) != set(labels.values()) or not 1 <= summary["avg_placement"] <= 16:
        raise _error("incomplete champion summary")
    categories = {}
    for node in root.nodes(cls="ar-augs--focus"):
        title = _one(node.nodes(tag="h2"), "missing observation heading").text()
        expected = {f"Augments taken with {name}": "augments", f"Items taken with {name}": "items"}
        if title not in expected or expected[title] in categories:
            raise _error("unexpected observation section")
        key = expected[title]
        categories[key] = _observations(node, "augment" if key == "augments" else "item")
        # An empty current-source section is valid; never backfill old/global rows.
    if set(categories) != {"augments", "items"}:
        raise _error("missing champion-specific sections")
    summary["low_sample"] = summary["sample_count"] < 200
    if not summary["sample_count"]:
        summary.update(win_rate=None, first_rate=None, avg_placement=None)
    return {**_metadata(version, url), "champion_id": champion["id"], "champion_name": name,
            "summary": summary, **categories}


class ArenaProvider:
    """One-thread owner; bounded downloads, one-hour snapshots and short retries."""
    def __init__(self, cache_dir):
        self.cache_dir = Path(cache_dir) / "arena"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._memory = {}
        self._catalog = None

    def _path(self, url):
        return self.cache_dir / (hashlib.sha256(url.encode()).hexdigest() + ".json")

    def _read(self, url):
        try:
            path = self._path(url)
            if path.stat().st_size > MAX_BYTES * 2:
                return None
            value = json.loads(path.read_text(encoding="utf-8"))
            stamp = value["fetched_at"]
            if (value.get("url") != url or type(stamp) not in (int, float)
                    or not math.isfinite(stamp) or not 0 <= stamp <= time.time() + 300):
                return None
            return value
        except (OSError, ValueError, KeyError, TypeError, RecursionError):
            return None

    def _write(self, url, value):
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.cache_dir,
                                             prefix=".arena-", suffix=".tmp", delete=False) as handle:
                temporary = Path(handle.name)
                json.dump(value, handle, allow_nan=False, separators=(",", ":"))
            os.replace(temporary, self._path(url))
        except OSError:
            pass
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def _download(self, url):
        request = Request(url, headers={"User-Agent": "MayhemCompanion/0.5", "Accept": "text/html,application/json"})
        try:
            with _OPENER.open(request, timeout=12) as response:
                if response.geturl() != url:
                    raise _error("unexpected redirect")
                content = response.read(MAX_BYTES + 1)
        except HTTPError as exc:
            raise DataUnavailableError(f"Arena source request failed (HTTP {exc.code})",
                                       transient=exc.code in (408, 425, 429) or exc.code >= 500) from exc
        except (URLError, OSError, TimeoutError) as exc:
            raise DataUnavailableError("Arena source is temporarily unreachable", transient=True) from exc
        if len(content) > MAX_BYTES:
            raise _error("response exceeds size limit")
        try:
            return content.decode("utf-8")
        except UnicodeError as exc:
            raise _error("invalid response encoding") from exc

    def _get(self, url, parse, *, force=False, ttl=TTL):
        now = time.time()
        memory = self._memory.get(url)
        if not force and memory and memory["until"] > now:
            if "error" in memory:
                raise memory["error"]
            return copy.deepcopy(memory["value"])
        cached = self._read(url)
        data = None
        if cached:
            try:
                data = parse(cached.get("content"))
            except (ValueError, TypeError, DataUnavailableError):
                cached = None
        stale = False
        if force or not cached or now - cached["fetched_at"] >= ttl:
            try:
                content = self._download(url)
                data = parse(content)
            except DataUnavailableError as exc:
                if not (exc.transient and cached):
                    self._memory[url] = {"error": exc, "until": now + RETRY_DELAY}
                    raise
                stale = True
            else:
                cached = {"url": url, "fetched_at": time.time(), "content": content}
                self._write(url, cached)
        if isinstance(data, dict) and "source" in data:
            data["stale"] = stale
            data["fetched_at"] = datetime.fromtimestamp(cached["fetched_at"], timezone.utc).isoformat(timespec="seconds")
        self._memory[url] = {"value": data, "until": now + RETRY_DELAY if stale else cached["fetched_at"] + ttl}
        return copy.deepcopy(data)

    @staticmethod
    def _versions(content):
        try:
            versions = json.loads(content)
        except (ValueError, TypeError) as exc:
            raise _error("invalid Data Dragon version response") from exc
        if (not isinstance(versions, list) or not versions or not isinstance(versions[0], str)
                or not re.fullmatch(r"[0-9]{1,3}\.[0-9]{1,2}\.[0-9]{1,3}", versions[0])):
            raise _error("invalid Data Dragon version")
        return versions[0]

    def load(self, force=False):
        version = self._get(VERSIONS_URL, self._versions, force=force, ttl=86400)
        resource_url = f"https://ddragon.leagueoflegends.com/cdn/{version}/data/en_US/champion.json"
        def champions(content):
            try:
                return _ddragon(json.loads(content))
            except (ValueError, TypeError) as exc:
                raise _error("invalid Data Dragon champions") from exc
        names = self._get(resource_url, champions, ttl=86400)
        self._catalog = self._get(INDEX_URL, lambda html: parse_catalog(html, names), force=force)
        reference = _reference_names()
        if reference:
            self._catalog["augments"] = _merge_names(self._catalog["augments"], reference)
            self._catalog["catalog_note"] = "Riot 16.21 English reference names; champion rates come only from the Arena source."
        return copy.deepcopy(self._catalog)

    def champion_for(self, champion_id, force=False):
        ident = _id(champion_id)
        catalog = self.load()
        champion = catalog["champions"].get(ident)
        if champion is None:
            raise _error("unknown champion")
        slug = champion.get("slug")
        if not slug:
            # The index omits some low-sample champions even when their detail
            # pages have observations. Source routes use Data Dragon's internal
            # champion name (e.g. MonkeyKing), not the display name. The fetched
            # page still has to pass canonical URL, Arena mode and champion checks.
            raw = champion.get("raw_name", "")
            if not isinstance(raw, str) or not re.fullmatch(r"[A-Za-z0-9]{1,40}", raw):
                raise _error("invalid champion route")
            slug = raw.lower()
        url = INDEX_URL + "/" + slug
        try:
            data = self._get(url, lambda html: parse_champion(html, champion, url), force=force)
        except DataUnavailableError:
            fallback = self._fallback_champion(champion, catalog, force=force)
            if fallback is not None:
                return fallback
            raise
        if not data.get("augments"):
            fallback = self._fallback_champion(champion, catalog, force=force)
            if fallback is not None and (fallback.get("augments") or not data.get("items")):
                return fallback
        return data

    def _fallback_champion(self, champion, catalog, *, force=False):
        # A complete snapshot keeps each row and its metadata on one source.
        # Do not fill gaps with global augment rates or another champion's data.
        from .arena_lolalytics import champion_url, parse_champion as parse_fallback
        try:
            url = champion_url(champion, catalog["version"])
            return self._get(
                url, lambda html: parse_fallback(html, champion, url, catalog["augments"],
                                                expected_patch=catalog["version"]),
                force=force,
            )
        except DataUnavailableError:
            # A denied/changed fallback must not erase valid primary item data.
            return None
