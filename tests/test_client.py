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


def choice_session(champion=897, *, phase="FINALIZATION", subset=False, bench=True):
    data = session(champion)
    data.update(isSpectating=False, benchEnabled=bench, allowSubsetChampionPicks=subset,
                timer={"phase": phase}, actions=[[{"actorCellId": 2, "type": "pick",
                                                 "completed": False, "isInProgress": True}]])
    return data


class AvailableChampionTests(unittest.TestCase):
    def read_choices(self, data, *, pickable=None, subset=None, is_aram=True):
        responses = {client._PICKABLE_PATH: pickable, client._SUBSET_PATH: subset}
        read = MagicMock(side_effect=lambda path: responses[path])
        return client._session_state(data, is_aram=is_aram, read_options=read), read

    def test_current_and_eligible_bench_only_never_whole_pickable_roster(self):
        data = choice_session()
        data["benchChampions"] = [{"championId": 22}, {"championId": 99},
                                  {"championId": 1}, {"championId": "022"}, {"championId": 897}]
        result, read = self.read_choices(data, pickable=[897, "22", 1, 157, 555])
        self.assertEqual(result["available_champion_ids"], ["897", "22"])
        self.assertEqual(result["available_champion_sources"], {"897": "current", "22": "bench"})
        self.assertFalse(result["availability_incomplete"])
        read.assert_called_once_with(client._PICKABLE_PATH)
        self.assertEqual(result["champion_id"], "897")
        self.assertEqual(len(result["summoner_spells"]), 2)
        self.assertNotIn("PRIVATE", json.dumps(result))

    def test_initial_cards_use_subset_not_full_eligible_roster(self):
        data = choice_session(0, phase="BAN_PICK", subset=True, bench=False)
        result, read = self.read_choices(data, pickable=list(range(1, 200)), subset=[22, "099", 157])
        self.assertEqual(result["available_champion_ids"], ["22", "99", "157"])
        self.assertEqual(set(result["available_champion_sources"].values()), {"card"})
        self.assertFalse(result["availability_incomplete"])
        read.assert_called_once_with(client._SUBSET_PATH)

    def test_bench_is_locked_to_own_subset_during_initial_card_phase(self):
        data = choice_session(0, phase="BAN_PICK", subset=True)
        data["benchChampions"] = [{"championId": 22}, {"championId": 99}, {"championId": 157}]
        result, read = self.read_choices(data, pickable=[22, 99, 157, 555], subset=[22, 99])
        self.assertEqual(result["available_champion_ids"], ["22", "99"])
        self.assertEqual(result["available_champion_sources"], {"22": "card", "99": "card"})
        self.assertFalse(result["availability_incomplete"])
        self.assertEqual([c.args[0] for c in read.call_args_list], [client._SUBSET_PATH, client._PICKABLE_PATH])
        data["timer"]["phase"] = "FINALIZATION"
        result, read = self.read_choices(data, pickable=[22, 99, 157], subset=[])
        self.assertEqual(result["available_champion_ids"], ["22", "99", "157"])
        read.assert_called_once_with(client._PICKABLE_PATH)

    def test_completed_local_pick_does_not_expose_initial_cards(self):
        for completed, in_progress in ((True, True), (True, False)):
            data = choice_session(0, phase="BAN_PICK", subset=True, bench=False)
            data["actions"][0][0].update(completed=completed, isInProgress=in_progress)
            result, read = self.read_choices(data, subset=[22, 99])
            self.assertEqual(result["available_champion_ids"], [])
            read.assert_not_called()

    def test_assigned_cards_are_available_during_planning_and_before_pick_active(self):
        for phase in ("PLANNING", "BAN_PICK"):
            for in_progress in (False, True):
                data = choice_session(0, phase=phase, subset=True, bench=False)
                data["actions"][0][0]["isInProgress"] = in_progress
                result, read = self.read_choices(data, subset=[22, "099", 157], pickable=list(range(1, 200)))
                self.assertEqual(result["available_champion_ids"], ["22", "99", "157"])
                self.assertEqual(set(result["available_champion_sources"].values()), {"card"})
                self.assertFalse(result["availability_incomplete"])
                read.assert_called_once_with(client._SUBSET_PATH)

    def test_confirmed_champion_does_not_reopen_initial_cards(self):
        for phase in ("PLANNING", "BAN_PICK"):
            data = choice_session(897, phase=phase, subset=True, bench=False)
            result, read = self.read_choices(data, subset=[22, 99])
            self.assertEqual(result["available_champion_ids"], ["897"])
            read.assert_not_called()

    def test_planning_cards_do_not_enable_bench_or_infer_missing_subset(self):
        data = choice_session(0, phase="PLANNING", subset=True)
        data["actions"][0][0]["isInProgress"] = False
        result, read = self.read_choices(data, subset=[99, 157], pickable=[22, 99, 157])
        self.assertEqual(result["available_champion_ids"], ["99", "157"])
        read.assert_called_once_with(client._SUBSET_PATH)
        for subset in (None, {}, [99, True], [22, 99, 157, 555]):
            result, read = self.read_choices(data, subset=subset, pickable=[22, 99, 157])
            self.assertIsNone(result["available_champion_ids"])
            self.assertTrue(result["availability_incomplete"])
            read.assert_called_once_with(client._SUBSET_PATH)

    def test_completed_card_pick_can_retake_own_eligible_subset_from_bench(self):
        data = choice_session(897, phase="BAN_PICK", subset=True)
        data["actions"][0][0]["completed"] = True
        data["benchChampions"] = [{"championId": 22}, {"championId": 99}]
        result, _ = self.read_choices(data, pickable=[22, 99], subset=[897, 22])
        self.assertEqual(result["available_champion_ids"], ["897", "22"])
        self.assertEqual(result["available_champion_sources"]["22"], "bench")

    def test_preview_is_not_current_but_verified_card_can_still_appear(self):
        data = choice_session(0, phase="BAN_PICK", subset=True, bench=False)
        data["myTeam"][1]["championPickIntent"] = 22
        result, _ = self.read_choices(data, subset=[99, 157])
        self.assertEqual(result["champion_id"], "22")
        self.assertTrue(result["champion_preview"])
        self.assertEqual(result["available_champion_ids"], ["99", "157"])
        result, _ = self.read_choices(data, subset=[22, 99])
        self.assertEqual(result["available_champion_sources"]["22"], "card")

    def test_missing_and_explicitly_empty_choices_are_distinct(self):
        data = choice_session(0)
        data["benchChampions"] = []
        result, read = self.read_choices(data)
        self.assertEqual(result["available_champion_ids"], [])
        self.assertFalse(result["availability_incomplete"])
        read.assert_not_called()
        del data["benchChampions"]
        result, read = self.read_choices(data)
        self.assertIsNone(result["available_champion_ids"])
        self.assertTrue(result["availability_incomplete"])
        read.assert_not_called()

    def test_unknown_eligibility_preserves_current_and_marks_partial(self):
        for response in (None, {}, [True], [22, "PRIVATE"], list(range(1, 1026))):
            result, _ = self.read_choices(choice_session(), pickable=response)
            self.assertEqual(result["available_champion_ids"], ["897"])
            self.assertTrue(result["availability_incomplete"])
            self.assertNotIn("PRIVATE", json.dumps(result))
        result, _ = self.read_choices(choice_session(), pickable=[])
        self.assertEqual(result["available_champion_ids"], ["897"])
        self.assertFalse(result["availability_incomplete"])

    def test_unknown_subset_never_unlocks_bench_during_initial_phase(self):
        data = choice_session(897, phase="BAN_PICK", subset=True)
        for subset in (None, {}, [22, "PRIVATE"], [22, 99, 157, 555]):
            result, _ = self.read_choices(data, pickable=[22, 99], subset=subset)
            self.assertEqual(result["available_champion_ids"], ["897"])
            self.assertTrue(result["availability_incomplete"])
            self.assertNotIn("PRIVATE", json.dumps(result))

    def test_malformed_bench_does_not_discard_valid_cards(self):
        data = choice_session(0, phase="BAN_PICK", subset=True)
        for bench in (None, {}, [22], [{"championId": True}], [{"championId": 22}, {}],
                      [{"championId": 22}] * 51):
            data["benchChampions"] = bench
            result, read = self.read_choices(data, pickable=[22], subset=[99, 157])
            self.assertEqual(result["available_champion_ids"], ["99", "157"])
            self.assertTrue(result["availability_incomplete"])
            read.assert_called_once_with(client._SUBSET_PATH)

    def test_unknown_timer_and_missing_flags_preserve_only_confirmed_context(self):
        data = choice_session()
        for timer in (None, {}, {"phase": "PRIVATE"}, {"phase": True}, []):
            data["timer"] = timer
            result, read = self.read_choices(data, pickable=[22])
            self.assertEqual(result["available_champion_ids"], ["897"])
            self.assertTrue(result["availability_incomplete"])
            read.assert_not_called()
        for key in ("benchEnabled", "allowSubsetChampionPicks"):
            data = choice_session(0, phase="BAN_PICK")
            del data[key]
            result, _ = self.read_choices(data, pickable=[22], subset=[99])
            self.assertIsNone(result["available_champion_ids"])
            self.assertTrue(result["availability_incomplete"])

    def test_irrelevant_timer_phases_and_disabled_bench_do_not_query_options(self):
        for phase in ("PLANNING", "GAME_STARTING"):
            result, read = self.read_choices(choice_session(phase=phase, subset=True), pickable=[22], subset=[99])
            self.assertEqual(result["available_champion_ids"], ["897"])
            read.assert_not_called()
        result, read = self.read_choices(choice_session(bench=False), pickable=[22])
        self.assertEqual(result["available_champion_ids"], ["897"])
        read.assert_not_called()
        result, read = self.read_choices(choice_session(phase="BAN_PICK"), pickable=[22])
        self.assertEqual(result["available_champion_ids"], ["897"])
        read.assert_not_called()

    def test_spectator_unknown_mode_and_malformed_local_identity_fail_closed(self):
        for is_aram in (False, None, "true", 1):
            result, read = self.read_choices(choice_session(), is_aram=is_aram, pickable=[22])
            self.assertIsNone(result["available_champion_ids"])
            read.assert_not_called()
        for field, value in (("isSpectating", True), ("isSpectating", "false"),
                             ("isSpectating", None), ("localPlayerCellId", "2"),
                             ("localPlayerCellId", True), ("myTeam", [])):
            data = choice_session()
            data[field] = value
            result, read = self.read_choices(data, pickable=[22])
            self.assertIsNone(result["available_champion_ids"])
            self.assertTrue(result["availability_incomplete"])
            read.assert_not_called()

    def test_missing_or_ambiguous_actions_do_not_infer_initial_cards(self):
        for actions in (None, {}, [None], [[None]], [[{}]],
                        [[{"actorCellId": "2", "type": "pick"}]],
                        [[{"actorCellId": 2, "type": None}]], [[{"actorCellId": 2, "type": "pick"}]],
                        [[{"actorCellId": 2, "type": "pick", "completed": False,
                           "isInProgress": True}]] * 2):
            data = choice_session(0, phase="BAN_PICK", subset=True, bench=False)
            data["actions"] = actions
            result, read = self.read_choices(data, subset=[22, 99])
            self.assertIsNone(result["available_champion_ids"])
            self.assertTrue(result["availability_incomplete"])
            read.assert_not_called()

    def test_optional_endpoint_failure_is_sanitized_and_preserves_selection(self):
        for error in (URLError("PRIVATE-TOKEN"), HTTPError("private", 404, "PRIVATE", {}, None),
                      ValueError("PRIVATE"), TimeoutError("PRIVATE")):
            read = MagicMock(side_effect=error)
            result = client._session_state(choice_session(), is_aram=True, read_options=read)
            self.assertEqual(result["champion_id"], "897")
            self.assertEqual(result["available_champion_ids"], ["897"])
            self.assertTrue(result["availability_incomplete"])
            self.assertEqual(len(result["summoner_spells"]), 2)
            self.assertNotIn("PRIVATE", json.dumps(result))


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

    def test_default_install_avoids_process_discovery_on_first_poll(self):
        self.http.return_value = "Lobby"
        result = self.reader.poll()
        self.assertEqual(result["phase"], "Lobby")
        self.lockfile.assert_called_once_with(client._DEFAULT_LOCKFILE)
        self.discovery.assert_not_called()

    def test_missing_default_discovers_alternate_install_then_reuses_it(self):
        alternate = Path("fixture-lockfile")
        self.lockfile.side_effect = lambda path: (client._Credentials(54322, "fixture-token")
                                                  if path == alternate else None)
        self.http.return_value = "Lobby"
        self.reader.poll()
        self.reader.poll()
        self.discovery.assert_called_once()
        self.assertEqual([call.args[0] for call in self.lockfile.call_args_list],
                         [client._DEFAULT_LOCKFILE, alternate, alternate])

    def test_connection_failure_rediscovers_despite_valid_orphaned_lockfile(self):
        alternate = Path("fixture-lockfile")
        self.lockfile.side_effect = lambda path: client._Credentials(
            54322 if path == alternate else 54321, "fixture-token")
        self.http.side_effect = [URLError("PRIVATE"), "Lobby"]
        with patch.object(client.time, "monotonic", side_effect=[0, 1]):
            self.reader.poll()
            self.reader.poll()
        self.discovery.assert_called_once()
        self.assertEqual([call.args[1].port for call in self.http.call_args_list], [54321, 54322])

    def test_disappearing_known_lockfile_triggers_install_discovery(self):
        self.http.return_value = "Lobby"
        self.reader.poll()
        alternate = Path("fixture-lockfile")
        self.lockfile.side_effect = lambda path: (client._Credentials(54322, "fixture-token")
                                                  if path == alternate else None)
        result = self.reader.poll()
        self.assertEqual(result["phase"], "Lobby")
        self.assertEqual(self.http.call_args.args[1].port, 54322)
        self.discovery.assert_called_once()

    def test_early_cards_never_carry_over_across_sessions_or_failed_reads(self):
        first = choice_session(0, phase="PLANNING", subset=True, bench=False)
        first["actions"][0][0]["isInProgress"] = False
        self.http.side_effect = ["ChampSelect", gameflow(), first, [22, 99], "Lobby",
                                 "ChampSelect", gameflow(), first, URLError("PRIVATE"),
                                 "ChampSelect", gameflow(), first, [157, 555]]
        states = [self.reader.poll() for _ in range(4)]
        self.assertEqual([state["available_champion_ids"] for state in states],
                         [["22", "99"], None, None, ["157", "555"]])
        self.assertTrue(states[2]["availability_incomplete"])
        self.assertEqual(states[2]["available_champion_sources"], {})
        self.assertNotIn("PRIVATE", json.dumps(states))

    def test_unknown_or_other_mode_does_not_read_available_options(self):
        for metadata in (gameflow("CLASSIC", 420), gameflow(None, 0), URLError("PRIVATE")):
            self.http.reset_mock()
            self.http.side_effect = ["ChampSelect", metadata,
                                     choice_session(phase="BAN_PICK", subset=True)]
            result = self.reader.poll()
            self.assertEqual(result["champion_id"], "897")
            self.assertIsNone(result["available_champion_ids"])
            self.assertEqual(result["available_champion_sources"], {})
            self.assertEqual(self.http.call_count, 3)

    def test_availability_clears_on_missing_session_exit_disconnect_and_tft(self):
        for remaining in (["ChampSelect", gameflow(), HTTPError("private", 404, "private", {}, None)],
                          ["EndOfGame"], ["None"], ["Lobby"], [URLError("PRIVATE")],
                          ["ChampSelect", gameflow("TFT", 1100)]):
            self.http.side_effect = ["ChampSelect", gameflow(), choice_session(), [22], *remaining]
            self.assertEqual(self.reader.poll()["available_champion_ids"], ["897", "22"])
            result = self.reader.poll()
            self.assertIsNone(result["available_champion_ids"])
            self.assertEqual(result["available_champion_sources"], {})
            self.assertFalse(result["availability_incomplete"])

    def test_changes_trades_bench_swaps_and_empty_selection_replace_previous_data(self):
        self.http.side_effect = ["ChampSelect", gameflow(), session(897), "ChampSelect", gameflow(), session(22),
                                 "ChampSelect", gameflow(), session(0), "ChampSelect", gameflow(), session(1, spell1Id=7)]
        results = [self.reader.poll() for _ in range(4)]
        self.assertEqual([s["champion_id"] for s in results], ["897", "22", None, "1"])
        self.assertEqual(results[-1]["summoner_spells"][0]["id"], "7")
        self.discovery.assert_not_called()
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
        self.discovery.assert_not_called()

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
        self.assertEqual(self.discovery.call_count, 1)
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

    def test_tft_active_phases_return_only_mode_without_champion_selection_read(self):
        for phase in ("ChampSelect", "GameStart", "InProgress", "Reconnect"):
            with self.subTest(phase=phase):
                self.http.reset_mock()
                self.http.side_effect = [phase, gameflow("TFT", 1100, phase)]
                result = self.reader.poll()
                self.assertEqual(result["status"], "tft")
                self.assertEqual(result["phase"], phase)
                self.assertTrue(result["is_tft"])
                self.assertFalse(result["is_aram"])
                self.assertEqual(result["game_mode"], "TFT")
                self.assertIsNone(result["champion_id"])
                self.assertEqual(result["summoner_spells"], [])
                self.assertEqual([c.args[2] for c in self.http.call_args_list],
                                 [client._PHASE_PATH, client._GAMEFLOW_PATH])
                self.assertNotIn("PRIVATE", json.dumps(result))

    def test_tft_queue_without_mode_skips_champion_selection(self):
        self.http.side_effect = ["ChampSelect", gameflow(None, 1160)]
        result = self.reader.poll()
        self.assertEqual(result["status"], "tft")
        self.assertTrue(result["is_tft"])
        self.assertFalse(result["is_aram"])
        self.assertIsNone(result["game_mode"])
        self.assertEqual(self.http.call_count, 2)

    def test_arena_select_returns_local_champion_without_aram_choice_requests(self):
        self.http.side_effect = ["ChampSelect", gameflow("CHERRY", 1700),
                                 choice_session(22, phase="BAN_PICK", subset=True)]
        result = self.reader.poll()
        self.assertEqual(result["status"], "champ_select")
        self.assertTrue(result["is_arena"])
        self.assertFalse(result["is_aram"])
        self.assertFalse(result["is_tft"])
        self.assertEqual(result["champion_id"], "22")
        self.assertEqual(result["queue_id"], 1700)
        self.assertIsNone(result["available_champion_ids"])
        self.assertFalse(result["availability_incomplete"])
        self.assertEqual([c.args[2] for c in self.http.call_args_list],
                         [client._PHASE_PATH, client._GAMEFLOW_PATH, client._SESSION_PATH])
        self.assertNotIn("PRIVATE", json.dumps(result))

    def test_arena_active_phases_preserve_mode_without_reading_selection(self):
        for phase in ("GameStart", "InProgress", "Reconnect"):
            with self.subTest(phase=phase):
                self.http.reset_mock()
                self.http.side_effect = [phase, gameflow("CHERRY", 1710, phase)]
                result = self.reader.poll()
                self.assertEqual(result["status"], "waiting")
                self.assertEqual(result["phase"], phase)
                self.assertTrue(result["is_arena"])
                self.assertFalse(result["is_aram"])
                self.assertFalse(result["is_tft"])
                self.assertEqual(result["game_mode"], "CHERRY")
                self.assertEqual(result["queue_id"], 1710)
                self.assertEqual(self.http.call_count, 2)

    def test_arena_mode_does_not_survive_inactive_phase_or_failed_metadata(self):
        self.http.side_effect = ["InProgress", gameflow("CHERRY", 1700, "InProgress"),
                                 "Lobby", "InProgress", URLError("PRIVATE-TOKEN")]
        self.assertTrue(self.reader.poll()["is_arena"])
        for _ in range(2):
            result = self.reader.poll()
            self.assertIsNone(result["is_arena"])
            self.assertIsNone(result["is_aram"])
            self.assertIsNone(result["is_tft"])
            self.assertIsNone(result["game_mode"])
            self.assertNotIn("queue_id", result)
            self.assertNotIn("PRIVATE", json.dumps(result))

    def test_arena_transition_to_aram_replaces_mode_and_champion_context(self):
        self.http.side_effect = ["InProgress", gameflow("CHERRY", 1700, "InProgress"),
                                 "ChampSelect", gameflow(), session(22)]
        self.assertTrue(self.reader.poll()["is_arena"])
        result = self.reader.poll()
        self.assertTrue(result["is_aram"])
        self.assertFalse(result["is_arena"])
        self.assertFalse(result["is_tft"])
        self.assertNotIn("queue_id", result)
        self.assertEqual(result["champion_id"], "22")

    def test_arena_newer_inactive_phase_clears_mode(self):
        self.http.side_effect = ["InProgress", gameflow("CHERRY", 1700, "EndOfGame")]
        result = self.reader.poll()
        self.assertEqual(result["phase"], "EndOfGame")
        self.assertIsNone(result["is_arena"])
        self.assertNotIn("queue_id", result)

    def test_tft_mode_does_not_survive_inactive_phase_or_failed_metadata(self):
        self.http.side_effect = ["InProgress", gameflow("TFT", 1100, "InProgress"),
                                 "Lobby", "InProgress", URLError("PRIVATE-TOKEN")]
        self.assertTrue(self.reader.poll()["is_tft"])
        for _ in range(2):
            result = self.reader.poll()
            self.assertEqual(result["status"], "waiting")
            self.assertIsNone(result["is_tft"])
            self.assertIsNone(result["is_aram"])
            self.assertIsNone(result["game_mode"])

    def test_tft_transition_to_aram_replaces_mode_and_champion_context(self):
        self.http.side_effect = ["InProgress", gameflow("TFT", 1100, "InProgress"),
                                 "ChampSelect", gameflow(), session(22)]
        self.assertTrue(self.reader.poll()["is_tft"])
        result = self.reader.poll()
        self.assertEqual(result["status"], "champ_select")
        self.assertTrue(result["is_aram"])
        self.assertFalse(result["is_tft"])
        self.assertEqual(result["champion_id"], "22")

    def test_tft_newer_inactive_phase_does_not_report_active_tft(self):
        self.http.side_effect = ["InProgress", gameflow("TFT", 1100, "EndOfGame")]
        result = self.reader.poll()
        self.assertEqual(result["status"], "waiting")
        self.assertEqual(result["phase"], "EndOfGame")
        self.assertIsNone(result["is_tft"])

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

    def test_tft_mode_helper_accepts_only_verified_exact_token(self):
        for mode in ("TFT", "tft", " TFT "):
            self.assertTrue(client.is_tft_mode(mode))
        for mode in (None, {}, True, "ARAM", "CLASSIC", "RANKED_TFT", "TFT_DOUBLE_UP", ""):
            self.assertFalse(client.is_tft_mode(mode))

    def test_tft_mode_is_distinct_from_aram_and_accepts_unknown_future_queue(self):
        for queue_id in (0, 1100, 99999):
            self.assertEqual(client._gameflow_context(gameflow("TFT", queue_id)),
                             {"is_aram": False, "is_tft": True, "is_arena": False, "game_mode": "TFT"})

    def test_official_tft_queue_fallback_does_not_infer_from_range(self):
        for queue_id in (1090, 1100, 1110, 1111, 1130, 1160, 1170, 1210, 1220,
                         6000, 6100, 6120, 6130):
            with self.subTest(queue_id=queue_id):
                self.assertEqual(client._gameflow_context(gameflow(None, queue_id)),
                                 {"is_aram": False, "is_tft": True, "is_arena": False, "game_mode": None})
        for queue_id in (1091, 1120, 1140, 1200, 1230, "1100", True, 1100.0):
            self.assertIsNone(client._gameflow_context(gameflow(None, queue_id))["is_tft"])

    def test_tft_map_mode_takes_priority_and_known_other_modes_override_queue(self):
        custom = gameflow("CLASSIC", 0)
        custom["map"] = {"id": 22, "gameMode": "TFT"}
        self.assertEqual(client._gameflow_context(custom),
                         {"is_aram": False, "is_tft": True, "is_arena": False, "game_mode": "TFT"})
        for mode in ("CLASSIC", "ARAM", "KIWI"):
            self.assertFalse(client._gameflow_context(gameflow(mode, 1100))["is_tft"])
        self.assertTrue(client._gameflow_context(gameflow("TFT", 450))["is_tft"])

    def test_tft_is_not_inferred_from_map_or_partial_mode(self):
        for mode in (None, "RANKED_TFT", "TFT_DOUBLE_UP", "PRIVATE-TFT"):
            data = gameflow(mode, 0)
            data["map"] = {"id": 22, "gameMode": mode}
            result = client._gameflow_context(data)
            self.assertIsNone(result["is_tft"])
            self.assertIsNone(result["is_aram"])
            self.assertNotIn("PRIVATE", json.dumps(result))

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
        self.assertEqual(client._gameflow_context(custom),
                         {"is_aram": True, "is_tft": False, "is_arena": False, "game_mode": "ARAM"})
        self.assertTrue(client._gameflow_context({"map": {"gameMode": "KIWI"}})["is_aram"])

    def test_known_other_mode_wins_over_conflicting_queue_id(self):
        self.assertEqual(client._gameflow_context(gameflow("CLASSIC", 450)),
                         {"is_aram": False, "is_tft": False, "is_arena": False, "game_mode": "CLASSIC"})

    def test_arena_mode_helper_accepts_only_verified_exact_token(self):
        for mode in ("CHERRY", "cherry", " CHERRY "):
            self.assertTrue(client.is_arena_mode(mode))
        for mode in (None, {}, True, "ARENA", "CHERRY_PBE", "ARAM", "TFT", ""):
            self.assertFalse(client.is_arena_mode(mode))

    def test_arena_mode_is_distinct_and_accepts_custom_or_future_queue(self):
        for queue_id in (0, 1700, 1710, 99999):
            self.assertEqual(client._gameflow_context(gameflow("CHERRY", queue_id)),
                             {"is_aram": False, "is_tft": False, "is_arena": True,
                              "game_mode": "CHERRY", "queue_id": queue_id})

    def test_verified_arena_queue_fallback_does_not_infer_from_range(self):
        for queue_id in (1700, 1704, 1710, 1740, 1750):
            with self.subTest(queue_id=queue_id):
                self.assertEqual(client._gameflow_context(gameflow(None, queue_id)),
                                 {"is_aram": False, "is_tft": False, "is_arena": True,
                                  "game_mode": None, "queue_id": queue_id})
        for queue_id in (1701, 1705, 1711, 1720, 1741, 1751, "1700", True, 1700.0):
            self.assertIsNone(client._gameflow_context(gameflow(None, queue_id))["is_arena"])

    def test_arena_map_mode_takes_priority_and_known_other_modes_override_queue(self):
        custom = gameflow("CLASSIC", 0)
        custom["map"] = {"id": 30, "gameMode": "CHERRY"}
        self.assertEqual(client._gameflow_context(custom),
                         {"is_aram": False, "is_tft": False, "is_arena": True,
                          "game_mode": "CHERRY", "queue_id": 0})
        for mode in ("CLASSIC", "ARAM", "KIWI", "TFT"):
            self.assertFalse(client._gameflow_context(gameflow(mode, 1700))["is_arena"])
        for queue_id in (450, 1100):
            self.assertTrue(client._gameflow_context(gameflow("CHERRY", queue_id))["is_arena"])

    def test_arena_is_not_inferred_from_map_or_partial_mode(self):
        for mode in (None, "ARENA", "CHERRY_PBE", "PRIVATE-CHERRY"):
            data = gameflow(mode, 0)
            data["map"] = {"id": 30, "gameMode": mode}
            result = client._gameflow_context(data)
            self.assertIsNone(result["is_arena"])
            self.assertIsNone(result["is_aram"])
            self.assertIsNone(result["is_tft"])
            self.assertNotIn("PRIVATE", json.dumps(result))

    def test_arena_mode_omits_invalid_queue_identifier(self):
        for queue_id in (None, True, "1700", 1700.0, -1, 2**31):
            result = client._gameflow_context(gameflow("CHERRY", queue_id))
            self.assertTrue(result["is_arena"])
            self.assertNotIn("queue_id", result)

    def test_unknown_and_malformed_metadata_is_not_assumed_aram(self):
        for data in (None, [], {}, {"gameData": []}, {"gameData": {"queue": []}},
                     gameflow("PRIVATE-MODE", 99999), gameflow(None, True),
                     gameflow({}, 0)):
            result = client._gameflow_context(data)
            self.assertIsNone(result["is_aram"])
            self.assertIsNone(result["game_mode"])
            self.assertNotIn("PRIVATE", json.dumps(result))


class LockfileAndTransportTests(unittest.TestCase):
    def test_reader_uses_allowed_gets_and_replaces_choices_without_stale_state(self):
        first = choice_session(0, phase="PLANNING", subset=True)
        first["actions"][0][0]["isInProgress"] = False
        second = choice_session(0, phase="BAN_PICK", subset=True)
        second["actions"][0][0]["isInProgress"] = False
        second["benchChampions"] = [{"championId": 157}]
        opener = MagicMock()
        response = opener.open.return_value.__enter__.return_value
        payloads = ["ChampSelect", gameflow(), first, [22, 99],
                    "ChampSelect", gameflow(), second, [157, 555], [157, 555],
                    "ChampSelect", gameflow(), choice_session(), [22, 99],
                    "GameStart", gameflow(phase="GameStart")]
        response.read.side_effect = [json.dumps(value).encode("utf-8") for value in payloads]
        with patch.object(client, "_discover_lockfiles", return_value=[Path("fixture-lockfile")]), \
                patch.object(client, "_read_lockfile", return_value=client._Credentials(54321, "fixture-token")), \
                patch.object(client, "_local_opener", return_value=opener):
            reader = client.ClientReader()
            states = [reader.poll(timeout=0.25) for _ in range(4)]
        self.assertEqual([state["available_champion_ids"] for state in states],
                         [["22", "99"], ["157", "555"], ["897", "22"], None])
        self.assertEqual(states[2]["available_champion_sources"], {"897": "current", "22": "bench"})
        self.assertEqual(states[3]["available_champion_sources"], {})
        self.assertTrue(all(not state["availability_incomplete"] for state in states))
        paths = [client._PHASE_PATH, client._GAMEFLOW_PATH, client._SESSION_PATH]
        expected = [*paths, client._SUBSET_PATH,
                    *paths, client._SUBSET_PATH, client._PICKABLE_PATH,
                    *paths, client._PICKABLE_PATH, client._PHASE_PATH, client._GAMEFLOW_PATH]
        self.assertEqual([call.args[0].full_url for call in opener.open.call_args_list],
                         ["https://127.0.0.1:54321" + path for path in expected])
        self.assertTrue(all(call.args[0].get_method() == "GET" and call.kwargs["timeout"] == 0.25
                            for call in opener.open.call_args_list))
        self.assertNotIn("PRIVATE", json.dumps(states))
        self.assertNotIn("fixture-token", json.dumps(states))

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
