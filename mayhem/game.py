"""Read-only champion detection using Riot's local Live Client Data API.

No Riot account identifiers leave this module's polling result. TLS relaxation is
restricted to the fixed loopback game service; ordinary network requests retain
normal certificate verification.
"""
from __future__ import annotations

import json
import math
import socket
import ssl
import unicodedata
from typing import Any, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request, build_opener

_BASE_URL = "https://127.0.0.1:2999"
_ALLOWED_PATHS = frozenset((
    "/liveclientdata/activeplayername",
    "/liveclientdata/playerlist",
    "/liveclientdata/gamestats",
))


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def _get_json(path: str, timeout: float) -> Any:
    if path not in _ALLOWED_PATHS:
        raise ValueError("Unsupported local game API path")
    # This context is attached only to a private opener whose only destination
    # is the fixed loopback base above. Never install it as the global default.
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    opener = build_opener(ProxyHandler({}), _NoRedirect(), HTTPSHandler(context=context))
    request = Request(_BASE_URL + path, headers={"Accept": "application/json"})
    with opener.open(request, timeout=timeout) as response:
        # A local service should be tiny; bound accidental unexpected responses.
        payload = response.read(2_000_001)
        if len(payload) > 2_000_000:
            raise ValueError("Local game response exceeds expected size")
        return json.loads(payload.decode("utf-8"))


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _riot_id(player: Mapping[str, Any]) -> str:
    explicit = _text(player.get("riotId"))
    if explicit:
        return explicit
    game_name = _text(player.get("riotIdGameName"))
    tag = _text(player.get("riotIdTagLine"))
    return f"{game_name}#{tag}" if game_name and tag else ""


def resolve_active_player(name: str, players: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    """Find one active player without guessing when legacy names are ambiguous.

    A full Riot ID wins over a coincidentally matching legacy summoner name.
    If a requested full Riot ID is absent, base-name fallback is allowed only
    for records without a full Riot ID, so a different tag cannot be selected.
    """
    sought = _text(name).casefold()
    if not sought or not isinstance(players, (list, tuple)):
        return None
    records = [p for p in players if isinstance(p, Mapping)]
    exact_riot = [p for p in records if _riot_id(p).casefold() == sought]
    if exact_riot:
        return exact_riot[0] if len(exact_riot) == 1 else None
    if "#" in sought:
        exact_legacy = [p for p in records if _text(p.get("summonerName")).casefold() == sought]
        if exact_legacy:
            return exact_legacy[0] if len(exact_legacy) == 1 else None
        base_name = sought.rsplit("#", 1)[0]
        matches = [p for p in records if not _riot_id(p) and
                   _text(p.get("summonerName")).casefold() == base_name]
    else:
        matches = [p for p in records if
                   _text(p.get("summonerName")).casefold() == sought or
                   _text(p.get("riotIdGameName")).casefold() == sought or
                   (_riot_id(p).rsplit("#", 1)[0].casefold() == sought)]
    return matches[0] if len(matches) == 1 else None


# Canonical IDs verified against Riot Data Dragon 16.21.1 summoner.json.
# Similar names do not make Arena/URF spells equivalent to ordinary spells.
_SPELLS_BY_RAW_ID = {
    "SummonerBarrier": ("21", "Barrier"),
    "SummonerBoost": ("1", "Cleanse"),
    "SummonerDot": ("14", "Ignite"),
    "SummonerExhaust": ("3", "Exhaust"),
    "SummonerFlash": ("4", "Flash"),
    "SummonerHaste": ("6", "Ghost"),
    "SummonerHeal": ("7", "Heal"),
    "SummonerMana": ("13", "Clarity"),
    "SummonerSmite": ("11", "Smite"),
    "SummonerSnowball": ("32", "Mark"),
    "SummonerTeleport": ("12", "Teleport"),
    "SummonerCherryFlash": ("2202", "Flash"),
    "SummonerSnowURFSnowball_Mark": ("39", "Mark"),
}
# Only ordinary canonical names are eligible for this Mayhem name fallback.
# Unknown raw tokens never fall back, avoiding incorrect variant equivalences.
_SPELL_IDS_BY_NAME = {
    "barrier": "21", "cleanse": "1", "ignite": "14", "exhaust": "3",
    "flash": "4", "ghost": "6", "heal": "7", "clarity": "13",
    "smite": "11", "mark": "32", "teleport": "12",
}
_RAW_SPELL_PREFIX = "GeneratedTip_SummonerSpell_"
_RAW_SPELL_SUFFIX = "_DisplayName"


def _normalized_spell_name(name: str) -> str:
    return "".join(char for char in unicodedata.normalize("NFKC", name).casefold()
                   if char.isalnum())


def _summoner_spells(player: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return at most two whitelisted spell objects in player slot order.

    The existing playerlist response contains summonerSpellOne/Two; no extra
    requests are needed. Missing entries do not invalidate champion detection.
    """
    spells = player.get("summonerSpells")
    if not isinstance(spells, Mapping):
        return []
    result = []
    for slot in ("summonerSpellOne", "summonerSpellTwo"):
        entry = spells.get(slot)
        if not isinstance(entry, Mapping):
            continue
        name = _text(entry.get("displayName"))
        raw_name = _text(entry.get("rawDisplayName"))
        if not name and not raw_name:
            continue
        raw_id = raw_name
        if raw_name.startswith(_RAW_SPELL_PREFIX) and raw_name.endswith(_RAW_SPELL_SUFFIX):
            raw_id = raw_name[len(_RAW_SPELL_PREFIX):-len(_RAW_SPELL_SUFFIX)]
        canonical = _SPELLS_BY_RAW_ID.get(raw_id)
        if canonical is not None:
            identifier, fallback_name = canonical
            name = name or fallback_name
        elif not raw_name:
            identifier = _SPELL_IDS_BY_NAME.get(_normalized_spell_name(name))
        else:
            identifier = None
        result.append({"id": identifier, "name": name or raw_name, "raw_name": raw_name or None})
    return result

def _inventory_item_ids(player: Mapping[str, Any]) -> list[str] | None:
    """Return occupied inventory slots 0-5, never an inferred purchase order.

    An explicit empty items list is known empty; missing or malformed data is
    unknown. Components and consumables remain for catalog classification.
    Trinket slot 6 is excluded, and stack counts do not create extra slots.
    """
    items = player.get("items")
    if not isinstance(items, list):
        return None
    slots = {}
    for item in items:
        if not isinstance(item, Mapping):
            return None
        slot = item.get("slot")
        if type(slot) is not int or not 0 <= slot <= 6:
            return None
        if slot == 6:
            continue
        identifier = item.get("itemID")
        if (type(identifier) is not int or not 0 < identifier <= 2**31 - 1
                or slot in slots):
            return None
        slots[slot] = str(identifier)
    return [slots[slot] for slot in sorted(slots)]


def _state(status: str, message: str) -> dict[str, Any]:
    return {
        "status": status, "champion_id": None, "champion_name": None,
        "raw_champion": None, "level": None, "game_mode": None,
        "game_time": None, "summoner_spells": [], "item_ids": None, "message": message,
    }


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def poll_active_player(timeout: float = 1.0) -> dict[str, Any]:
    """Return current champion context, or a sanitized waiting/error state.

    Numeric champion IDs are not supplied by this API, so champion_id is None.
    The caller may resolve champion_name/raw_champion using its static catalog.
    No player names, Riot IDs, team rosters, or raw API errors appear in this result.
    """
    if not isinstance(timeout, (int, float)) or not 0 < timeout <= 10:
        raise ValueError("timeout must be greater than zero and at most 10 seconds")
    try:
        active_name = _get_json("/liveclientdata/activeplayername", timeout)
        players = _get_json("/liveclientdata/playerlist", timeout)
        if not isinstance(active_name, str) or not isinstance(players, list):
            return _state("error", "The local game API returned an unexpected response.")
        player = resolve_active_player(active_name, players)
        if player is None:
            return _state("waiting", "Waiting for the active champion to become available.")
        champion_name = _text(player.get("championName"))
        raw_champion = _text(player.get("rawChampionName"))
        if not champion_name and raw_champion.startswith("game_character_displayname_"):
            champion_name = raw_champion.removeprefix("game_character_displayname_")
        if not champion_name:
            return _state("waiting", "Waiting for champion information.")
        result = _state("live", "Champion detected.")
        result["champion_name"] = champion_name
        result["raw_champion"] = raw_champion or None
        result["summoner_spells"] = _summoner_spells(player)
        result["item_ids"] = _inventory_item_ids(player)
        level = _finite_number(player.get("level"))
        result["level"] = int(level) if level is not None and 1 <= level <= 30 and level.is_integer() else None
        # Supplementary metadata failing must not discard a valid champion.
        try:
            game = _get_json("/liveclientdata/gamestats", timeout)
            if isinstance(game, Mapping):
                result["game_mode"] = _text(game.get("gameMode")) or None
                elapsed = _finite_number(game.get("gameTime"))
                result["game_time"] = elapsed if elapsed is not None and elapsed >= 0 else None
        except (HTTPError, URLError, OSError, ValueError, TypeError):
            result["message"] = "Champion detected; game details are unavailable."
        return result
    except HTTPError as error:
        if error.code in (404, 503):
            return _state("waiting", "Waiting for a League game.")
        return _state("error", "The local game API is temporarily unavailable.")
    except (URLError, ConnectionError, TimeoutError, socket.timeout, OSError):
        return _state("waiting", "Waiting for a League game.")
    except (ValueError, TypeError, UnicodeError):
        return _state("error", "The local game API returned an unreadable response.")
