import json
from pathlib import Path
import ssl
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError
from urllib.request import HTTPSHandler, ProxyHandler

from mayhem import client


def session(champion=897, **own_fields):
    own = {"cellId": 2, "championId": champion, "championPickIntent": 0,
           "spell1Id": 6, "spell2Id": 4, "puuid": "PRIVATE-PUUID",
           "gameName": "PRIVATE-PLAYER", "summonerId": 987654321}
    own.update(own_fields)
    return {"localPlayerCellId": 2, "myTeam": [
        {"cellId": 0, "championId": 1, "gameName": "PRIVATE-TEAMMATE"}, own],
        "theirTeam": [{"cellId": 2, "championId": 99}],
        "benchChampions": [{"championId": 22}], "gameId": 123456789}


def gameflow(mode="ARAM", queue_id=450, phase="ChampSelect"):
    return {"phase": phase, "gameData": {"queue": {"id": queue_id, "gameMode": mode},
            "gameId": 123456789, "password": "PRIVATE-PASSWORD",
            "teamOne": [{"puuid": "PRIVATE-PUUID"}]}}


class SessionTests(unittest.TestCase):
    def test_matches_own_cell_and_returns_only_champion_and_spell_context(self):
        result = client._session_state(session())
        self.assertEqual(result["status"], "champ_select")
        self.assertEqual(result["phase"], "ChampSelect")
        self.assertEqual(result["champion_id"], "897")
        self.assertIsNone(result["champion_name"])
        self.assertFalse(result["champion_preview"])
        self.assertEqual(result["summoner_spells"], [
            {"id": "6", "name": "Ghost", "raw_name": None},
            {"id": "4", "name": "Flash", "raw_name": None},
        ])
        encoded = json.dumps(result)
        for private in ("PRIVATE", "puuid", "summonerId", "myTeam", "theirTeam",
                        "987654321", "123456789", "localPlayerCellId"):
            self.assertNotIn(private, encoded)

    def test_pick_intent_is_preview_only_when_no_selected_champion(self):
        preview = client._session_state(session(0, championPickIntent=22))
        self.assertEqual(preview["champion_id"], "22")
        self.assertTrue(preview["champion_preview"])
        confirmed = client._session_state(session(897, championPickIntent=22))
        self.assertEqual(confirmed["champion_id"], "897")
        self.assertFalse(confirmed["champion_preview"])

    def test_unselected_champion_retains_champ_select_state(self):
        result = client._session_state(session(0))
        self.assertEqual(result["status"], "champ_select")
        self.assertIsNone(result["champion_id"])
        self.assertFalse(result["champion_preview"])
        self.assertEqual(len(result["summoner_spells"]), 2)

    def test_zero_is_a_valid_local_cell(self):
        data = session()
        data["localPlayerCellId"] = 0
        self.assertEqual(client._session_state(data)["champion_id"], "1")

    def test_missing_ambiguous_and_wrong_type_cells_do_not_guess(self):
        cases = [None, [], {}, {"localPlayerCellId": 2, "myTeam": []}]
        for local in (True, False, "2", 2.0, -1, None):
            data = session()
            data["localPlayerCellId"] = local
            cases.append(data)
        duplicate = session()
        duplicate["myTeam"].append(dict(duplicate["myTeam"][1]))
        cases.append(duplicate)
        wrong = session()
        wrong["myTeam"][1]["cellId"] = "2"
        cases.append(wrong)
        for data in cases:
            with self.subTest(data=data):
                result = client._session_state(data)
                self.assertEqual(result["status"], "champ_select")
                self.assertIsNone(result["champion_id"])
                self.assertEqual(result["summoner_spells"], [])

    def test_invalid_champion_values_do_not_use_intent(self):
        for value in (True, False, -1, 0.0, 1.5, "Ashe", "-1", {}, [], 2**40):
            with self.subTest(value=value):
                result = client._session_state(session(value, championPickIntent=22))
                self.assertIsNone(result["champion_id"])
                self.assertFalse(result["champion_preview"])

    def test_spectator_does_not_claim_a_team_champion(self):
        data = session()
        data["isSpectating"] = True
        self.assertIsNone(client._session_state(data)["champion_id"])

    def test_spells_keep_slot_order_and_variant_ids(self):
        result = client._session_state(session(spell1Id=32, spell2Id=2202))
        self.assertEqual([(s["id"], s["name"]) for s in result["summoner_spells"]],
                         [("32", "Mark"), ("2202", "Flash")])

    def test_missing_and_malformed_spells_preserve_champion(self):
        for value in (None, 0, True, "Flash", {}, [], -1, 4.5):
            result = client._session_state(session(spell1Id=value, spell2Id=None))
            self.assertEqual(result["champion_id"], "897")
            self.assertEqual(result["summoner_spells"], [])

    def test_unknown_numeric_spell_can_match_by_id_without_inventing_name(self):
        result = client._session_state(session(spell1Id=9999, spell2Id="4"))
        self.assertEqual(result["summoner_spells"][0],
                         {"id": "9999", "name": "Spell 9999", "raw_name": None})
        self.assertEqual(result["summoner_spells"][1]["id"], "4")


class ReaderTests(unittest.TestCase):
    def setUp(self):
        self.discovery = patch.object(client, "_discover_lockfiles", return_value=[Path("fixture-lockfile")]).start()
        self.lockfile = patch.object(client, "_read_lockfile", return_value=client._Credentials(54321, "fixture-token")).start()
        self.http = patch.object(client, "_get_json").start()
        self.addCleanup(patch.stopall)
        self.reader = client.ClientReader()

    def test_only_three_read_only_endpoints_in_champ_select(self):
        self.http.side_effect = ["ChampSelect", gameflow(), session()]
        result = self.reader.poll()
        self.assertEqual(result["champion_id"], "897")
        self.assertTrue(result["is_aram"])
        self.assertEqual(result["game_mode"], "ARAM")
        self.assertNotIn("PRIVATE", json.dumps(result))
        self.assertEqual([c.args[2] for c in self.http.call_args_list],
                         [client._PHASE_PATH, client._GAMEFLOW_PATH, client._SESSION_PATH])
        self.assertTrue(all(c.args[3] == 0.5 for c in self.http.call_args_list))

    def test_no_session_read_outside_champ_select(self):
        self.http.return_value = "Lobby"
        result = self.reader.poll()
        self.assertEqual((result["status"], result["phase"]), ("waiting", "Lobby"))
        self.http.assert_called_once()

    def test_changes_trades_bench_swaps_and_empty_selection_replace_previous_data(self):
        self.http.side_effect = ["ChampSelect", gameflow(), session(897), "ChampSelect", gameflow(), session(22),
                                 "ChampSelect", gameflow(), session(0), "ChampSelect", gameflow(), session(1, spell1Id=7)]
        results = [self.reader.poll() for _ in range(4)]
        self.assertEqual([s["champion_id"] for s in results], ["897", "22", None, "1"])
        self.assertEqual(results[-1]["summoner_spells"][0]["id"], "7")
        self.discovery.assert_called_once()
        self.assertEqual(self.lockfile.call_count, 4)

    def test_client_restart_refreshes_credentials_without_rediscovering(self):
        self.lockfile.side_effect = [client._Credentials(54321, "old-token"),
                                    client._Credentials(54322, "new-token")]
        self.http.side_effect = ["Lobby", "ChampSelect", gameflow(), session(22)]
        self.reader.poll()
        result = self.reader.poll()
        self.assertEqual(result["champion_id"], "22")
        self.assertEqual(self.http.call_args_list[0].args[1].port, 54321)
        self.assertEqual(self.http.call_args_list[1].args[1].port, 54322)
        self.discovery.assert_called_once()

    def test_offline_discovery_is_throttled_and_retried(self):
        self.lockfile.return_value = None
        with patch.object(client.time, "monotonic", side_effect=[0, 0.5, 10.5]):
            results = [self.reader.poll() for _ in range(3)]
        self.assertEqual(self.discovery.call_count, 2)
        self.assertTrue(all(s["status"] == "waiting" for s in results))
        self.http.assert_not_called()

    def test_connection_failure_allows_new_installation_discovery(self):
        self.http.side_effect = [URLError("fixture-token private details"), "Lobby"]
        with patch.object(client.time, "monotonic", side_effect=[0, 11]):
            failed = self.reader.poll()
            recovered = self.reader.poll()
        self.assertEqual(self.discovery.call_count, 2)
        self.assertEqual(recovered["phase"], "Lobby")
        self.assertNotIn("fixture-token", json.dumps(failed))

    def test_session_transition_404_still_clears_champion(self):
        self.http.side_effect = ["ChampSelect", gameflow(), HTTPError("private", 404, "private", {}, None)]
        result = self.reader.poll()
        self.assertEqual(result["status"], "champ_select")
        self.assertIsNone(result["champion_id"])

    def test_active_game_phases_return_mode_without_champion_selection_read(self):
        for phase in ("GameStart", "InProgress", "Reconnect"):
            self.http.reset_mock()
            self.http.side_effect = [phase, gameflow("KIWI", 2400, phase)]
            result = self.reader.poll()
            self.assertEqual(result["status"], "waiting")
            self.assertEqual(result["phase"], phase)
            self.assertTrue(result["is_aram"])
            self.assertEqual(result["game_mode"], "KIWI")
            self.assertEqual(self.http.call_count, 2)

    def test_inactive_phase_reads_no_session_and_forgets_previous_mode(self):
        self.http.side_effect = ["ChampSelect", gameflow(), session(), "EndOfGame", "None", "Lobby"]
        self.assertTrue(self.reader.poll()["is_aram"])
        for phase in ("EndOfGame", "None", "Lobby"):
            result = self.reader.poll()
            self.assertEqual(result["phase"], phase)
            self.assertIsNone(result["is_aram"])
            self.assertIsNone(result["game_mode"])
        self.assertEqual(self.http.call_count, 6)

    def test_metadata_failure_preserves_phase_and_champion_but_not_old_mode(self):
        self.http.side_effect = ["ChampSelect", gameflow(), session(),
                                 "ChampSelect", URLError("PRIVATE-TOKEN"), session(22)]
        self.assertTrue(self.reader.poll()["is_aram"])
        result = self.reader.poll()
        self.assertEqual(result["phase"], "ChampSelect")
        self.assertEqual(result["champion_id"], "22")
        self.assertIsNone(result["is_aram"])
        self.assertIsNone(result["game_mode"])
        self.assertNotIn("PRIVATE", json.dumps(result))

    def test_other_mode_still_returns_own_selection_without_marking_aram(self):
        self.http.side_effect = ["ChampSelect", gameflow("CLASSIC", 420), session()]
        result = self.reader.poll()
        self.assertEqual(result["champion_id"], "897")
        self.assertFalse(result["is_aram"])

    def test_phase_transition_in_newer_gameflow_response_wins(self):
        self.http.side_effect = ["ChampSelect", gameflow(phase="EndOfGame")]
        result = self.reader.poll()
        self.assertEqual(result["phase"], "EndOfGame")
        self.assertEqual(result["status"], "waiting")
        self.assertIsNone(result["is_aram"])
        self.assertEqual(self.http.call_count, 2)

    def test_http_and_parse_errors_are_sanitized(self):
        for error, status in ((HTTPError("private", 401, "fixture-token", {}, None), "waiting"),
                              (HTTPError("private", 500, "fixture-token", {}, None), "error"),
                              (ValueError("fixture-token"), "error"),
                              (TimeoutError("fixture-token"), "waiting")):
            self.http.side_effect = error
            result = self.reader.poll()
            self.assertEqual(result["status"], status)
            self.assertNotIn("fixture-token", json.dumps(result))

    def test_unknown_or_malformed_phase_never_exposes_raw_data(self):
        for phase in ("PRIVATE-PLAYER", {}, ["PRIVATE-PLAYER"]):
            self.http.return_value = phase
            result = self.reader.poll()
            self.assertIsNone(result["phase"])
            self.assertNotIn("PRIVATE-PLAYER", json.dumps(result))

    def test_invalid_timeout_fails_before_discovery_or_network(self):
        for timeout in (0, -1, True, None, "0.5", 11, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                self.reader.poll(timeout)
        self.http.assert_not_called()
        self.discovery.assert_not_called()


class GameflowContextTests(unittest.TestCase):
    def test_mode_helper_accepts_only_verified_exact_modes(self):
        for mode in ("ARAM", "aram", " KIWI "):
            self.assertTrue(client.is_aram_mode(mode))
        for mode in (None, {}, True, "CLASSIC", "ARAM_RANKED", "ARAM Mayhem", ""):
            self.assertFalse(client.is_aram_mode(mode))

    def test_official_aram_queue_fallback_includes_mayhem_and_clash(self):
        for queue_id in (65, 67, 100, 450, 720, 2400):
            context = client._gameflow_context(gameflow(None, queue_id))
            self.assertTrue(context["is_aram"])
            self.assertIsNone(context["game_mode"])

    def test_custom_queue_requires_actual_mode(self):
        for mode in ("ARAM", "KIWI"):
            self.assertTrue(client._gameflow_context(gameflow(mode, 0))["is_aram"])
        self.assertFalse(client._gameflow_context(gameflow("CLASSIC", 0))["is_aram"])
        custom = gameflow(None, 0)
        custom["gameData"]["queue"]["mapId"] = 12
        self.assertIsNone(client._gameflow_context(custom)["is_aram"])

    def test_custom_selected_map_mode_takes_priority_over_generic_queue(self):
        custom = gameflow("CLASSIC", 0)
        custom["map"] = {"id": 12, "gameMode": "ARAM"}
        self.assertEqual(client._gameflow_context(custom), {"is_aram": True, "game_mode": "ARAM"})
        self.assertTrue(client._gameflow_context({"map": {"gameMode": "KIWI"}})["is_aram"])

    def test_known_other_mode_wins_over_conflicting_queue_id(self):
        self.assertEqual(client._gameflow_context(gameflow("CLASSIC", 450)),
                         {"is_aram": False, "game_mode": "CLASSIC"})

    def test_unknown_and_malformed_metadata_is_not_assumed_aram(self):
        for data in (None, [], {}, {"gameData": []}, {"gameData": {"queue": []}},
                     gameflow("PRIVATE-MODE", 99999), gameflow(None, True),
                     gameflow({}, 0)):
            result = client._gameflow_context(data)
            self.assertIsNone(result["is_aram"])
            self.assertIsNone(result["game_mode"])
            self.assertNotIn("PRIVATE", json.dumps(result))


class LockfileAndTransportTests(unittest.TestCase):
    def test_lockfile_validation_and_hidden_credential_repr(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "lockfile"
            path.write_text("LeagueClient:123:54321:fixture-token:https", encoding="utf-8")
            credentials = client._read_lockfile(path)
            self.assertEqual(credentials.port, 54321)
            self.assertEqual(credentials.password, "fixture-token")
            self.assertNotIn("fixture-token", repr(credentials))
            for malformed in ("LeagueClient:123:54321:fixture-token:http",
                              "LeagueClient:123:65536:fixture-token:https",
                              "LeagueClient:0:54321:fixture-token:https",
                              "LeagueClient:123:54321::https", "partial",
                              "LeagueClient:123:54321:token\nextra:https", "x" * 4097):
                path.write_text(malformed, encoding="utf-8")
                self.assertIsNone(client._read_lockfile(path))
            self.assertIsNone(client._read_lockfile(Path(directory) / "missing"))

    def test_process_discovery_reads_only_executable_path_and_is_bounded(self):
        completed = subprocess.CompletedProcess([], 0, "D:\\Games\\League\\LeagueClientUx.exe\n")
        with patch.object(client.sys, "platform", "win32"), patch.object(client.subprocess, "run", return_value=completed) as run:
            paths = client._discover_lockfiles()
        self.assertEqual(str(paths[0]), r"D:\Games\League\lockfile")
        command = run.call_args.args[0]
        self.assertIn("ExecutablePath", command[-1])
        self.assertNotIn("CommandLine", command[-1])
        self.assertEqual(run.call_args.kwargs["timeout"], 1.5)
        self.assertIs(run.call_args.kwargs["stderr"], subprocess.DEVNULL)
        self.assertIn(client._DEFAULT_LOCKFILE, paths)

    def test_discovery_failure_uses_default_path(self):
        with patch.object(client.sys, "platform", "win32"), patch.object(client.subprocess, "run", side_effect=OSError("private")):
            self.assertEqual(client._discover_lockfiles(), [client._DEFAULT_LOCKFILE])

    def test_transport_is_get_loopback_only_with_scoped_auth(self):
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value.read.return_value = b'"ChampSelect"'
        result = client._get_json(opener, client._Credentials(54321, "fixture-token"), client._PHASE_PATH, 0.5)
        request = opener.open.call_args.args[0]
        self.assertEqual(result, "ChampSelect")
        self.assertEqual(request.full_url, "https://127.0.0.1:54321" + client._PHASE_PATH)
        self.assertEqual(request.get_method(), "GET")
        self.assertTrue(request.get_header("Authorization").startswith("Basic "))
        for path in ("https://example.com/", "/lol-champ-select/v1/session/my-selection", "//example.com"):
            with self.assertRaises(ValueError):
                client._get_json(opener, client._Credentials(54321, "fixture-token"), path, 0.5)
        self.assertEqual(opener.open.call_count, 1)

    def test_response_size_is_bounded(self):
        opener = MagicMock()
        response = opener.open.return_value.__enter__.return_value
        response.read.return_value = b"x" * (client._MAX_RESPONSE_BYTES + 1)
        with self.assertRaises(ValueError):
            client._get_json(opener, client._Credentials(54321, "fixture-token"), client._PHASE_PATH, 0.5)
        response.read.assert_called_once_with(client._MAX_RESPONSE_BYTES + 1)

    def test_opener_disables_proxy_and_redirects_with_private_tls_context(self):
        with patch.object(client, "build_opener") as build:
            client._local_opener()
        handlers = build.call_args.args
        proxy = next(h for h in handlers if isinstance(h, ProxyHandler))
        redirect = next(h for h in handlers if isinstance(h, client._NoRedirect))
        https = next(h for h in handlers if isinstance(h, HTTPSHandler))
        self.assertEqual(proxy.proxies, {})
        self.assertIsNone(redirect.redirect_request(None, None, 302, "", {}, "https://example.com"))
        self.assertFalse(https._context.check_hostname)
        self.assertEqual(https._context.verify_mode, ssl.CERT_NONE)


if __name__ == "__main__":
    unittest.main()
