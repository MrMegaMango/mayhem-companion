"""Read-only game-mode and champion selection from the local League client (LCU).

The client supplies the schema through ``/help?format=Full``: a session's
``localPlayerCellId`` identifies one ``myTeam`` entry, whose ``championId``,
``championPickIntent``, and ``spell1Id``/``spell2Id`` are numeric IDs. The LCU
is an unsupported Riot interface, so missing or changed data fails closed.

Only three GET endpoints are used at runtime. Lockfile authentication remains in
memory for the current poll; no credentials, player identifiers, or rosters
are returned or logged. A pick intent is a preview, not a confirmed champion.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
import json
import math
from pathlib import Path, PureWindowsPath
import ssl
import subprocess
import sys
import time
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import HTTPSHandler, ProxyHandler, Request, build_opener

from .game import _NoRedirect, _SPELLS_BY_RAW_ID, _state


_PHASE_PATH = "/lol-gameflow/v1/gameflow-phase"
_GAMEFLOW_PATH = "/lol-gameflow/v1/session"
_SESSION_PATH = "/lol-champ-select/v1/session"
_ALLOWED_PATHS = frozenset((_PHASE_PATH, _GAMEFLOW_PATH, _SESSION_PATH))
_ACTIVE_PHASES = frozenset(("ChampSelect", "GameStart", "InProgress", "Reconnect"))
_KNOWN_PHASES = frozenset((
    "None", "Lobby", "Matchmaking", "ReadyCheck", "ChampSelect", "GameStart",
    "InProgress", "Reconnect", "WaitingForStats", "PreEndOfGame", "EndOfGame",
    "TerminatedInError",
))
_DEFAULT_LOCKFILE = Path(r"C:\Riot Games\League of Legends\lockfile")
_DISCOVERY_INTERVAL = 10.0
_MAX_RESPONSE_BYTES = 2_000_000
_SPELL_NAMES = {identifier: name for identifier, name in _SPELLS_BY_RAW_ID.values()}
# Riot's queues.json identifies these ARAM queues, including Mayhem (2400).
# A custom queue (0), or Howling Abyss map alone, does not identify ARAM.
_ARAM_QUEUE_IDS = frozenset((65, 67, 100, 450, 720, 2400))
# Riot's TFT Data Dragon 16.21.1 tft-queues.json plus public queues.json
# identify these TFT queues. Never infer TFT from a numeric range or map ID.
_TFT_QUEUE_IDS = frozenset((
    1090, 1100, 1110, 1111, 1130, 1160, 1170, 1210, 1220,
    6000, 6100, 6120, 6130,
))
# Verified in the local /lol-game-queues/v1/queues response. The public
# gameModes.json omits TFT, so it is not treated as a League "other" mode.
_TFT_MODES = frozenset(("TFT",))
# KIWI was verified in the local Live Client API during a Mayhem custom game.
_ARAM_MODES = frozenset(("ARAM", "KIWI"))
# Public gameModes.json provides these non-ARAM mode tokens. Unknown future
# modes remain unknown rather than accidentally inheriting an earlier game.
_OTHER_MODES = frozenset((
    "CLASSIC", "ODIN", "TUTORIAL", "URF", "DOOMBOTSTEEMO", "ONEFORALL",
    "ASCENSION", "FIRSTBLOOD", "KINGPORO", "SIEGE", "ASSASSINATE", "ARSR",
    "DARKSTAR", "STARGUARDIAN", "PROJECT", "GAMEMODEX", "ODYSSEY",
    "NEXUSBLITZ", "ULTBOOK", "SWIFTPLAY", "BRAWL",
))


def is_aram_mode(mode: Any) -> bool:
    """Recognize verified ARAM/Mayhem modes; never use partial name matches."""
    return isinstance(mode, str) and mode.strip().upper() in _ARAM_MODES


def is_tft_mode(mode: Any) -> bool:
    """Recognize the verified TFT token; never use partial name matches."""
    return isinstance(mode, str) and mode.strip().upper() in _TFT_MODES


def _gameflow_context(session: Any) -> dict[str, Any]:
    result = {"is_aram": None, "is_tft": None, "game_mode": None}
    if not isinstance(session, Mapping):
        return result
    game = session.get("gameData")
    queue = game.get("queue") if isinstance(game, Mapping) else None
    queue = queue if isinstance(queue, Mapping) else {}
    # The selected map's gameMode is also in the local /help schema. It is
    # especially useful for custom games, whose generic queue can be empty.
    for source in (session.get("map"), queue):
        if not isinstance(source, Mapping):
            continue
        mode = source.get("gameMode")
        mode = mode.strip().upper() if isinstance(mode, str) else None
        if mode in _ARAM_MODES or mode in _TFT_MODES or mode in _OTHER_MODES:
            result.update(is_aram=is_aram_mode(mode), is_tft=is_tft_mode(mode), game_mode=mode)
            return result
    if type(queue.get("id")) is int:
        if queue["id"] in _ARAM_QUEUE_IDS:
            result.update(is_aram=True, is_tft=False)
        elif queue["id"] in _TFT_QUEUE_IDS:
            result.update(is_aram=False, is_tft=True)
    return result


@dataclass(frozen=True, repr=False)
class _Credentials:
    port: int
    password: str


def _read_lockfile(path: Path) -> _Credentials | None:
    """Read a bounded lockfile, never expose its contents in an exception."""
    try:
        with path.open("rb") as stream:
            data = stream.read(4097)
        if len(data) > 4096:
            return None
        fields = data.decode("utf-8").strip().split(":")
        if len(fields) != 5:
            return None
        name, pid, port, password, protocol = fields
        if (name not in ("LeagueClient", "LeagueClientUx") or protocol != "https"
                or not pid.isascii() or not pid.isdecimal() or int(pid) <= 0
                or not port.isascii() or not port.isdecimal()
                or not 1 <= int(port) <= 65535
                or not password or len(password) > 2048
                or any(ord(char) < 33 or ord(char) > 126 for char in password)):
            return None
        return _Credentials(int(port), password)
    except (OSError, ValueError, UnicodeError):
        return None


def _discover_lockfiles() -> list[Path]:
    """Find install locations without inspecting process command-line secrets."""
    paths: list[Path] = []
    if sys.platform == "win32":
        # Fixed read-only query. Its only output is ExecutablePath; never ask
        # for CommandLine, which contains the client's authentication token.
        command = (
            "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new(); "
            "Get-CimInstance Win32_Process -Filter \"Name='LeagueClientUx.exe'\" "
            "| Select-Object -ExpandProperty ExecutablePath"
        )
        try:
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                encoding="utf-8", errors="replace", timeout=1.5, check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if result.returncode == 0:
                for line in result.stdout.splitlines():
                    executable = PureWindowsPath(line.strip())
                    if (executable.is_absolute()
                            and len(executable.drive) == 2
                            and executable.drive[1] == ":"
                            and executable.name.casefold() == "leagueclientux.exe"):
                        paths.append(Path(str(executable.parent / "lockfile")))
        except (OSError, subprocess.SubprocessError):
            pass
    paths.append(_DEFAULT_LOCKFILE)
    return list(dict.fromkeys(paths))


def _local_opener():
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    # This opener is private to the reader and can only be used below with a
    # validated lockfile port on HTTPS IPv4 loopback. Do not install globally.
    return build_opener(ProxyHandler({}), _NoRedirect(), HTTPSHandler(context=context))


def _get_json(opener, credentials: _Credentials, path: str, timeout: float) -> Any:
    if (path not in _ALLOWED_PATHS or type(credentials.port) is not int
            or not 1 <= credentials.port <= 65535):
        raise ValueError("Unsupported local client API request")
    authentication = base64.b64encode(("riot:" + credentials.password).encode("utf-8")).decode("ascii")
    request = Request(
        f"https://127.0.0.1:{credentials.port}{path}",
        headers={"Accept": "application/json", "Authorization": "Basic " + authentication},
        method="GET",
    )
    with opener.open(request, timeout=timeout) as response:
        payload = response.read(_MAX_RESPONSE_BYTES + 1)
        if len(payload) > _MAX_RESPONSE_BYTES:
            raise ValueError("Local client response exceeds expected size")
        return json.loads(payload.decode("utf-8"))


def _client_state(status: str, message: str, phase: str | None = None) -> dict[str, Any]:
    result = _state(status, message)
    result.update(phase=phase if phase in _KNOWN_PHASES else None,
                  champion_preview=False, is_aram=None, is_tft=None)
    return result


def _positive_id(value: Any) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        if not value.isascii() or not value.isdecimal() or len(value) > 10:
            return None
        value = int(value)
    if isinstance(value, int) and 0 < value <= 2**31 - 1:
        return str(value)
    return None


def _session_state(session: Any) -> dict[str, Any]:
    result = _client_state("champ_select", "Waiting for your champion selection.", "ChampSelect")
    if not isinstance(session, Mapping) or session.get("isSpectating") is True:
        return result
    local_cell = session.get("localPlayerCellId")
    team = session.get("myTeam")
    if (isinstance(local_cell, bool) or not isinstance(local_cell, int)
            or local_cell < 0 or not isinstance(team, list)):
        return result
    matches = [player for player in team if isinstance(player, Mapping)
               and type(player.get("cellId")) is int and player["cellId"] == local_cell]
    if len(matches) != 1:
        return result
    player = matches[0]
    raw_champion = player.get("championId")
    champion = _positive_id(raw_champion)
    no_selection = (raw_champion is None or raw_champion == "0"
                    or (type(raw_champion) is int and raw_champion == 0))
    if champion is None and no_selection:
        champion = _positive_id(player.get("championPickIntent"))
        result["champion_preview"] = champion is not None
    result["champion_id"] = champion
    for slot in ("spell1Id", "spell2Id"):
        identifier = _positive_id(player.get(slot))
        if identifier is not None:
            result["summoner_spells"].append({
                "id": identifier,
                "name": _SPELL_NAMES.get(identifier, f"Spell {identifier}"),
                "raw_name": None,
            })
    if champion is not None:
        result["message"] = ("Champion preview detected." if result["champion_preview"]
                             else "Champion detected in champion select.")
    return result


class ClientReader:
    """Persistent reader; discovery is cached, lockfile auth is refreshed per poll.

    Poll from one background worker. ``timeout`` bounds each HTTP request;
    initial/offline install discovery can additionally take up to 1.5 seconds.
    A restart in the same installation is detected on the next lockfile read.
    Alternative installation discovery retries at most once every ten seconds.
    """

    def __init__(self):
        self._lockfiles: list[Path] = []
        self._next_discovery = 0.0
        self._needs_discovery = True
        self._opener = _local_opener()

    def _credentials(self) -> _Credentials | None:
        now = time.monotonic()
        if self._needs_discovery and now >= self._next_discovery:
            self._next_discovery = now + _DISCOVERY_INTERVAL
            discovered = _discover_lockfiles()
            self._lockfiles = list(dict.fromkeys([*discovered, *self._lockfiles]))
        for path in self._lockfiles:
            credentials = _read_lockfile(path)
            if credentials is not None:
                return credentials
        return None

    def poll(self, timeout: float = 0.5) -> dict[str, Any]:
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout) or not 0 < timeout <= 10):
            raise ValueError("timeout must be greater than zero and at most 10 seconds")
        credentials = self._credentials()
        if credentials is None:
            self._needs_discovery = True
            return _client_state("waiting", "Waiting for the League client.")
        phase = None
        context = {"is_aram": None, "is_tft": None, "game_mode": None}

        def finish(result):
            result.update(context)
            return result

        try:
            phase = _get_json(self._opener, credentials, _PHASE_PATH, timeout)
            if not isinstance(phase, str):
                return _client_state("error", "The local client returned an unexpected phase.")
            self._needs_discovery = False
            if phase not in _ACTIVE_PHASES:
                return _client_state("waiting", "Waiting for champion select.", phase)
            # This resource supplies queue.gameMode for regular and custom
            # games. Read it only while a game is starting or active, and do
            # not discard champion selection if supplementary metadata fails.
            try:
                gameflow = _get_json(self._opener, credentials, _GAMEFLOW_PATH, timeout)
                context = _gameflow_context(gameflow)
                current_phase = gameflow.get("phase") if isinstance(gameflow, Mapping) else None
                if isinstance(current_phase, str) and current_phase in _KNOWN_PHASES:
                    phase = current_phase
                if phase not in _ACTIVE_PHASES:
                    return _client_state("waiting", "Waiting for champion select.", phase)
            except (HTTPError, URLError, OSError, ValueError, TypeError, UnicodeError):
                pass
            # TFT uses the same League client phases, but has no League
            # champion selection or guaranteed Live Client board API.
            if context["is_tft"] is True:
                return finish(_client_state("tft", "TFT session detected.", phase))
            if phase != "ChampSelect":
                return finish(_client_state("waiting", "League game active.", phase))
            return finish(_session_state(_get_json(self._opener, credentials, _SESSION_PATH, timeout)))
        except HTTPError as error:
            if error.code in (401, 403):
                self._needs_discovery = True
                return finish(_client_state("waiting", "Waiting for the League client connection.", phase))
            if error.code in (404, 503):
                if phase == "ChampSelect":
                    return finish(_client_state("champ_select", "Waiting for your champion selection.", phase))
                return finish(_client_state("waiting", "Waiting for champion select.", phase))
            return finish(_client_state("error", "The local client API is temporarily unavailable.", phase))
        except (URLError, OSError):
            self._needs_discovery = True
            return finish(_client_state("waiting", "Waiting for the League client connection.", phase))
        except (ValueError, TypeError, UnicodeError):
            return finish(_client_state("error", "The local client returned an unreadable response.", phase))
