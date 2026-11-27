"""Champion-wide Arena observations from one bounded Lolalytics snapshot."""
from __future__ import annotations

import copy
import json
import re
from urllib.parse import parse_qs, urlsplit

from .arena_lolalytics import MAX_BYTES, _Graph, _Page, _name, _number, _pairs, _patch
from .data import DataUnavailableError

BASE_URL = "https://lolalytics.com/lol/tierlist/arena/"
MAX_CHAMPIONS = 1000
SOURCE_QUEUE = 1750


def _error(message):
    return DataUnavailableError("Arena rankings: " + message)


def _window(window):
    if window not in ("current", "30"):
        raise _error("unsupported data window")
    return window


def ranking_url(patch, *, window="current"):
    """Use only the fixed all-ranks/global Arena route and supported windows."""
    if _window(window) == "30":
        selected = "30"
    else:
        major, minor = _patch(patch)
        selected = f"{16 if major == 26 else major}.{minor}"
    return BASE_URL + "?tier=all&patch=" + selected


def _route(url, *, window, canonical=False):
    if not isinstance(url, str):
        raise _error("invalid source URL")
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.netloc != "lolalytics.com"
            or parsed.path != "/lol/tierlist/arena/" or parsed.fragment):
        raise _error("invalid Arena rankings URL")
    query = parse_qs(parsed.query, keep_blank_values=True)
    if (query.get("tier") != ["all"] or any(key not in ("tier", "patch") for key in query)
            or ("patch" in query and len(query["patch"]) != 1)):
        raise _error("unexpected source filters")
    selected = query.get("patch", [None])[0]
    if selected is None and canonical and window == "current":
        return None
    if window == "30":
        if selected != "30":
            raise _error("wrong data window")
    else:
        _patch(selected)
    return selected


def parse_rankings(html, champions, url, *, expected_patch=None, window="current"):
    """Return all champion observations; identity resources contain no rates.

    Rolling-window statistics retain their own window and do not inherit the
    asset catalog's patch as their match period.
    """
    window = _window(window)
    requested_patch = _route(url, window=window)
    if not isinstance(html, str) or len(html.encode("utf-8")) > MAX_BYTES:
        raise _error("invalid HTML response")
    if not isinstance(champions, dict) or not 1 <= len(champions) <= MAX_CHAMPIONS:
        raise _error("invalid champion catalog")
    try:
        page = _Page(html)
        if len(page.scripts) != 1 or len(page.canonicals) != 1:
            raise _error("missing or ambiguous page snapshot")
        canonical_patch = _route(page.canonicals[0], window=window, canonical=True)
        snapshot = json.loads(page.scripts[0], object_pairs_hook=_pairs,
                              parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite JSON")))
        graph = _Graph(snapshot.get("objs") if isinstance(snapshot, dict) else None)
        nav_raw = graph.one({"path", "lang", "page", "mode", "patch", "tier", "region", "view", "status"})
        nav = {key: graph.read(nav_raw[key]) for key in
               ("path", "lang", "page", "mode", "patch", "tier", "region", "view", "status")}
        if (nav["path"] != "/lol/tierlist/arena/" or nav["page"] != "tierlist"
                or nav["mode"] != "arena" or nav["lang"] != "en" or nav["tier"] != "all"
                or nav["region"] != "all" or nav["view"] != "list"
                or type(nav["status"]) is not int or nav["status"] != 200):
            raise _error("wrong Arena mode, language or sample filters")
        catalog_raw = graph.one({"patch", "champions", "champPath", "champId"})
        asset_patch = graph.read(catalog_raw["patch"])
        _patch(asset_patch)
        if window == "30":
            if nav["patch"] != "30":
                raise _error("wrong rolling-window response")
        else:
            selected_patch = _patch(nav["patch"])
            for expected in (expected_patch, requested_patch, canonical_patch):
                if expected is not None and _patch(expected) != selected_patch:
                    raise _error("wrong patch response")
        names, paths, source_ids = (graph.read(catalog_raw[key]) for key in ("champions", "champPath", "champId"))
        if any(not isinstance(value, dict) or not 1 <= len(value) <= MAX_CHAMPIONS
               for value in (names, paths, source_ids)):
            raise _error("invalid source identities")
        stats_raw = graph.one({"queue", "view", "fields", "avgWr", "analysed", "cid", "response"})
        queue, view = graph.read(stats_raw["queue"]), graph.read(stats_raw["view"])
        if type(queue) is not int or queue != SOURCE_QUEUE or view != "list":
            raise _error("wrong source queue or view")
        response = graph.read(stats_raw["response"])
        if not isinstance(response, dict) or response.get("valid") is not True:
            raise _error("invalid statistics response")
        total = _number(graph.read(stats_raw["analysed"]), count=True)
        _number(graph.read(stats_raw["avgWr"]), percent=True)
        observations = graph.read(stats_raw["cid"])
        if not isinstance(observations, dict) or not 1 <= len(observations) <= MAX_CHAMPIONS:
            raise _error("missing or oversized champion observations")
        rows = []
        for ident, row in observations.items():
            if (not isinstance(ident, str) or not re.fullmatch(r"[1-9][0-9]{0,9}", ident)
                    or ident not in champions or not isinstance(row, dict)):
                raise _error("unknown or invalid champion observation")
            champion = champions[ident]
            if not isinstance(champion, dict) or str(champion.get("id")) != ident:
                raise _error("invalid champion identity")
            name, raw = _name(champion.get("name")), champion.get("raw_name", champion.get("riotId"))
            if not isinstance(raw, str) or not re.fullmatch(r"[A-Za-z0-9]{1,40}", raw):
                raise _error("invalid champion route")
            route = "wukong" if ident == "62" and raw == "MonkeyKing" else raw.lower()
            if (paths.get(ident) != route or names.get(route) != name
                    or type(source_ids.get(route)) is not int or str(source_ids[route]) != ident):
                raise _error("champion catalog identity mismatch")
            count = _number(row["games"], count=True)
            rate = _number(row["wr"], percent=True)
            rows.append({**champion, "id": ident, "champion_id": ident, "name": name,
                         "win_rate": rate if count else None, "sample_count": count,
                         "low_sample": count < 200, "missing": not bool(count),
                         "first_rate": None, "avg_placement": None})
        rolling = window == "30"
        return {"source": "Lolalytics", "source_url": url, "game_mode": "Arena",
                "version": None if rolling else nav["patch"],
                "source_patch": None if rolling else nav["patch"], "asset_patch": asset_patch,
                "data_window": window, **({"window_days": 30} if rolling else {}),
                "data_date": None, "stale": False, "win_rate_label": "Win rate",
                "rate_note": "Source win rate; finish cutoff not specified."
                             + (" Last 30 days may span balance patches." if rolling else ""),
                "sample_label": "Arena · all ranks · all regions · source queue 1750",
                "champion_observations": total, "champions": copy.deepcopy(champions), "rows": rows}
    except DataUnavailableError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError, OverflowError) as exc:
        raise _error("invalid page snapshot") from exc
