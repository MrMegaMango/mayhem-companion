import json
import ssl
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

from mayhem import game


class ResolveActivePlayerTests(unittest.TestCase):
    def test_prefers_full_id_and_handles_case(self):
        players = [{"riotId": "Player#NA1", "championName": "Annie"},
                   {"summonerName": "player#na1", "championName": "Ashe"}]
        self.assertIs(game.resolve_active_player("PLAYER#NA1", players), players[0])

    def test_composes_id_when_endpoint_uses_split_fields(self):
        player = {"riotIdGameName": "Player", "riotIdTagLine": "NA1"}
        self.assertIs(game.resolve_active_player("Player#NA1", [player]), player)

    def test_unique_legacy_name_fallback(self):
        player = {"summonerName": "Player", "championName": "Ashe"}
        self.assertIs(game.resolve_active_player("Player#NA1", [player]), player)

    def test_different_tag_is_not_legacy_fallback(self):
        player = {"riotId": "Player#EUW", "summonerName": "Player"}
        self.assertIsNone(game.resolve_active_player("Player#NA1", [player]))

    def test_ambiguous_short_name_is_not_guessed(self):
        players = [{"summonerName": "Player", "riotId": "Player#NA1"},
                   {"riotId": "Player#EUW"}]
        self.assertIsNone(game.resolve_active_player("Player", players))

    def test_duplicate_full_ids_are_not_guessed(self):
        self.assertIsNone(game.resolve_active_player("Player#NA1", [{"riotId": "Player#NA1"}] * 2))

    def test_malformed_records_are_ignored(self):
        self.assertIsNone(game.resolve_active_player("", [None, {}]))
        self.assertIsNone(game.resolve_active_player("missing", [None, {}]))


class PollTests(unittest.TestCase):
    def test_returns_champion_context_without_player_identity(self):
        player = {"riotId": "PrivatePlayer#Secret", "summonerName": "PrivatePlayer",
                  "championName": "Annie", "rawChampionName": "game_character_displayname_Annie",
                  "level": 7, "scores": {"kills": 10}, "team": "ORDER"}
        with patch.object(game, "_get_json", side_effect=["PrivatePlayer#Secret", [player],
                          {"gameMode": "ARAM", "gameTime": 421.5}]):
            state = game.poll_active_player()
        self.assertEqual(state["status"], "live")
        self.assertEqual(state["champion_name"], "Annie")
        self.assertEqual(state["level"], 7)
        self.assertEqual(state["game_time"], 421.5)
        self.assertIsNone(state["champion_id"])
        encoded = json.dumps(state)
        for private_value in ("PrivatePlayer", "Secret", "scores", "ORDER", "riotId"):
            self.assertNotIn(private_value, encoded)

    def test_game_stats_failure_does_not_discard_champion(self):
        with patch.object(game, "_get_json", side_effect=["P", [{"summonerName": "P", "championName": "Ashe"}], URLError("secret")]):
            state = game.poll_active_player()
        self.assertEqual(state["status"], "live")
        self.assertEqual(state["champion_name"], "Ashe")
        self.assertIsNone(state["game_time"])
        self.assertNotIn("secret", json.dumps(state))

    def test_raw_champion_fallback(self):
        with patch.object(game, "_get_json", side_effect=["P", [{"summonerName": "P", "rawChampionName": "game_character_displayname_AurelionSol"}], {}]):
            self.assertEqual(game.poll_active_player()["champion_name"], "AurelionSol")

    def test_offline_waits_without_leaking_exception(self):
        with patch.object(game, "_get_json", side_effect=URLError("PrivatePlayer: unavailable")):
            state = game.poll_active_player()
        self.assertEqual(state["status"], "waiting")
        self.assertNotIn("PrivatePlayer", json.dumps(state))

    def test_invalid_json_reports_sanitized_error(self):
        with patch.object(game, "_get_json", side_effect=ValueError("private response")):
            state = game.poll_active_player()
        self.assertEqual(state["status"], "error")
        self.assertNotIn("private response", json.dumps(state))

    def test_invalid_shapes_are_errors(self):
        with patch.object(game, "_get_json", side_effect=[{}, []]):
            self.assertEqual(game.poll_active_player()["status"], "error")

    def test_no_matching_player_waits(self):
        with patch.object(game, "_get_json", side_effect=["P", []]):
            self.assertEqual(game.poll_active_player()["status"], "waiting")

    def test_invalid_numbers_are_not_exposed(self):
        with patch.object(game, "_get_json", side_effect=["P", [{"summonerName": "P", "championName": "Ashe", "level": True}], {"gameTime": float("nan")}]):
            state = game.poll_active_player()
        self.assertIsNone(state["level"])
        self.assertIsNone(state["game_time"])

    def test_timeout_is_bounded(self):
        for value in (0, -1, 100, float("nan")):
            with self.assertRaises(ValueError):
                game.poll_active_player(value)


class LocalTransportTests(unittest.TestCase):
    def test_only_known_loopback_paths_accepted(self):
        with patch.object(game, "build_opener") as builder:
            for path in ("https://external.test", "//external.test", "/unknown"):
                with self.assertRaises(ValueError):
                    game._get_json(path, 1)
            builder.assert_not_called()

    def test_redirects_are_refused(self):
        self.assertIsNone(game._NoRedirect().redirect_request(None, None, 302, "", {}, "https://external.test"))

    def test_no_global_ssl_modification_and_fixed_origin(self):
        original = ssl._create_default_https_context
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'"P#NA1"'
        opener = MagicMock()
        opener.open.return_value = response
        with patch.object(game, "build_opener", return_value=opener) as builder:
            self.assertEqual(game._get_json("/liveclientdata/activeplayername", .5), "P#NA1")
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://127.0.0.1:2999/liveclientdata/activeplayername")
        self.assertEqual(opener.open.call_args.kwargs["timeout"], .5)
        self.assertIs(ssl._create_default_https_context, original)
        self.assertTrue(any(isinstance(handler, game._NoRedirect) for handler in builder.call_args.args))


if __name__ == "__main__":
    unittest.main()
