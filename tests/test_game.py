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



class SummonerSpellTests(unittest.TestCase):
    @staticmethod
    def spell(name, token):
        return {"displayName": name,
                "rawDisplayName": f"GeneratedTip_SummonerSpell_{token}_DisplayName"}

    def poll_with(self, spells):
        player = {"riotId": "PrivatePlayer#Secret", "championName": "Annie",
                  "summonerSpells": spells}
        with patch.object(game, "_get_json", side_effect=["PrivatePlayer#Secret", [player], {}]) as fetch:
            result = game.poll_active_player()
        self.assertEqual(result["status"], "live")
        self.assertEqual(result["champion_name"], "Annie")
        self.assertEqual(fetch.call_count, 3)
        self.assertEqual([call.args[0] for call in fetch.call_args_list], [
            "/liveclientdata/activeplayername", "/liveclientdata/playerlist", "/liveclientdata/gamestats"])
        return result["summoner_spells"]

    def test_official_flash_ignite_schema_and_slot_order(self):
        spells = {"summonerSpellTwo": self.spell("Ignite", "SummonerDot"),
                  "summonerSpellOne": self.spell("Flash", "SummonerFlash")}
        self.assertEqual(self.poll_with(spells), [
            {"id": "4", "name": "Flash", "raw_name": "GeneratedTip_SummonerSpell_SummonerFlash_DisplayName"},
            {"id": "14", "name": "Ignite", "raw_name": "GeneratedTip_SummonerSpell_SummonerDot_DisplayName"},
        ])

    def test_mark_flash_preserves_player_slot_order(self):
        result = self.poll_with({"summonerSpellOne": self.spell("Mark", "SummonerSnowball"),
                                 "summonerSpellTwo": self.spell("Flash", "SummonerFlash")})
        self.assertEqual([spell["id"] for spell in result], ["32", "4"])

    def test_raw_identifier_recognizes_localized_name(self):
        result = self.poll_with({"summonerSpellOne": self.spell("闪现", "SummonerFlash")})
        self.assertEqual(result[0]["id"], "4")
        self.assertEqual(result[0]["name"], "闪现")

    def test_known_raw_identifier_without_display_name_uses_canonical_name(self):
        result = self.poll_with({"summonerSpellOne": self.spell("", "SummonerHaste")})
        self.assertEqual(result[0]["name"], "Ghost")
        self.assertEqual(result[0]["id"], "6")

    def test_known_raw_token_without_template_is_recognized(self):
        result = self.poll_with({"summonerSpellOne": {"rawDisplayName": "SummonerBoost"}})
        self.assertEqual(result, [{"id": "1", "name": "Cleanse", "raw_name": "SummonerBoost"}])

    def test_missing_raw_name_uses_exact_normalized_canonical_name(self):
        result = self.poll_with({"summonerSpellOne": {"displayName": "  FLASH  "},
                                 "summonerSpellTwo": {"displayName": "mArK"}})
        self.assertEqual([spell["id"] for spell in result], ["4", "32"])
        self.assertTrue(all(spell["raw_name"] is None for spell in result))

    def test_unknown_name_never_fuzzily_matches_a_real_spell(self):
        result = self.poll_with({"summonerSpellOne": {"displayName": "Flazh"}})
        self.assertEqual(result, [{"id": None, "name": "Flazh", "raw_name": None}])

    def test_unknown_raw_variant_is_not_guessed_from_display_name(self):
        result = self.poll_with({"summonerSpellOne": self.spell("Flash", "SummonerFlashUnknownVariant")})
        self.assertIsNone(result[0]["id"])

    def test_known_mode_variants_are_not_collapsed_to_ordinary_ids(self):
        result = self.poll_with({"summonerSpellOne": self.spell("Flash", "SummonerCherryFlash"),
                                 "summonerSpellTwo": self.spell("Mark", "SummonerSnowURFSnowball_Mark")})
        self.assertEqual([spell["id"] for spell in result], ["2202", "39"])

    def test_malformed_or_missing_container_does_not_invalidate_champion(self):
        for value in (None, [], "Flash", 4, {}, {"summonerSpellOne": None, "summonerSpellTwo": []},
                      {"summonerSpellOne": {"displayName": 4, "rawDisplayName": False}}):
            with self.subTest(value=value):
                self.assertEqual(self.poll_with(value), [])
        self.assertEqual(game._summoner_spells({}), [])

    def test_partial_pair_skips_malformed_entry(self):
        result = self.poll_with({"summonerSpellOne": "malformed", "summonerSpellTwo": self.spell("Heal", "SummonerHeal")})
        self.assertEqual([spell["id"] for spell in result], ["7"])

    def test_only_active_player_spells_and_allowed_fields_are_returned(self):
        player = {"riotId": "PrivatePlayer#Secret", "championName": "Annie", "summonerSpells": {
            "summonerSpellOne": {**self.spell("Flash", "SummonerFlash"), "riotId": "PrivatePlayer#Secret",
                                 "rawDescription": "private-description", "privateField": "private-value"},
            "summonerSpellTwo": self.spell("Mark", "SummonerSnowball"),
            "summonerSpellThree": self.spell("Ignite", "SummonerDot"),
            "riotId": "PrivatePlayer#Secret",
        }}
        other = {"riotId": "Other#Secret", "championName": "Ashe", "summonerSpells": {
            "summonerSpellOne": self.spell("Heal", "SummonerHeal")}}
        with patch.object(game, "_get_json", side_effect=["PrivatePlayer#Secret", [other, player], {}]):
            result = game.poll_active_player()
        self.assertEqual([spell["id"] for spell in result["summoner_spells"]], ["4", "32"])
        serialized = json.dumps(result)
        for private_value in ("PrivatePlayer", "Other", "Secret", "private-description", "private-value", "Ignite", "Heal"):
            self.assertNotIn(private_value, serialized)
        self.assertEqual(set(result["summoner_spells"][0]), {"id", "name", "raw_name"})

    def test_nonlive_states_never_reuse_a_previous_spell_pair(self):
        with patch.object(game, "_get_json", side_effect=URLError("offline")):
            self.assertEqual(game.poll_active_player()["summoner_spells"], [])
        with patch.object(game, "_get_json", side_effect=ValueError("bad JSON")):
            self.assertEqual(game.poll_active_player()["summoner_spells"], [])
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
