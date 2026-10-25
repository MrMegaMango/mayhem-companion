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


def _state(status: str, message: str) -> dict[str, Any]:
    return {
        "status": status, "champion_id": None, "champion_name": None,
        "raw_champion": None, "level": None, "game_mode": None,
        "game_time": None, "message": message,
    }


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def poll_active_player(timeout: float = 1.0) -> dict[str, Any]:
    """Return current champion context, or a sanitized waiting/error state.

    Numeric champion IDs are not supplied by this API, so champion_id is None.
    The caller may resolve champion_name/raw_champion using its static catalog.
    No names, Riot IDs, team rosters, or raw API errors appear in this result.
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
