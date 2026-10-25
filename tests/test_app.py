"""State regression tests use a fake view; no game interaction or desktop input."""
import queue
import unittest
from unittest.mock import Mock, patch

from mayhem.app import Companion


class StateTests(unittest.TestCase):
    def setUp(self):
        style = patch("mayhem.app.set_window_style")
        style.start()
        self.addCleanup(style.stop)

    def make_view(self):
        app = object.__new__(Companion)
        app.catalog = {"champions": {"157": {"name": "Yasuo", "riotId": "Yasuo", "slug": "yasuo"}}}
        app.worker = Mock()
        app.requests = queue.Queue()
        app.events = queue.Queue()
        app.champ_var, app.badge, app.stage = Mock(), Mock(), Mock()
        app.status, app.source, app.root = Mock(), Mock(), Mock()
        app.follow = True
        app.champion = "157"
        app.live_champion = "157"
        app.locked = False
        app.hidden = True
        app.auto_visible = app.browse_mode = False
        app.visibility_override = None
        app.hotkeys = Mock()
        app.hotkeys.registered = [101, 102, 103]
        app.game_live = True
        app.game_status = "live"
        app.champ_select = app.champion_preview = False
        app.switch_view = Mock()
        app.token = 5
        app.is_auto_offer = True
        app.offer_ids = ["1", "2", "3"]
        app.cards = [{"var": Mock(), "value": Mock(), "meta": Mock()} for _ in range(3)]
        app.spell_token = 7
        app.spell_champion = "157"
        app.spell_data = None
        app.live_spells = []
        app.spell_tree = Mock()
        app.spell_tree.get_children.return_value = []
        app.spell_summary, app.spell_note = Mock(), Mock()
        app.spell_sort = Mock()
        app.spell_sort.get.return_value = "Win rate"
        app.view = Mock()
        app.view.get.return_value = "augments"
        app.auto = Mock()
        app.auto.get.return_value = True
        return app

    def test_game_updates_resolve_locally_without_http(self):
        app = self.make_view()
        app.handle_game({"status": "live", "raw_champion": "game_character_displayname_Yasuo"})
        self.assertEqual(app.champion, "157")
        self.assertFalse(app.worker.provider.mock_calls)

    def test_hidden_on_desktop_lobby_other_modes_and_unknown_mode(self):
        app = self.make_view()
        for game in ({"status": "waiting"},
                     {"status": "waiting", "phase": "Lobby", "is_aram": True},
                     {"status": "champ_select", "is_aram": False},
                     {"status": "live", "is_aram": False},
                     {"status": "live", "is_aram": None}):
            app.update_visibility(game)
            self.assertTrue(app.hidden)
        app.root.deiconify.assert_not_called()

    def test_shows_at_aram_select_stays_through_loading_and_hides_at_end(self):
        app = self.make_view()
        for game in ({"status": "champ_select", "phase": "ChampSelect", "is_aram": True},
                     {"status": "waiting", "phase": "GameStart", "is_aram": True},
                     {"status": "live", "phase": "InProgress", "is_aram": True},
                     {"status": "waiting", "phase": "Reconnect", "is_aram": True}):
            app.update_visibility(game)
            self.assertFalse(app.hidden)
        app.root.deiconify.assert_called_once()
        app.update_visibility({"status": "waiting", "phase": "EndOfGame", "is_aram": False})
        self.assertTrue(app.hidden)
        app.root.withdraw.assert_called_once()

    def test_manual_hide_survives_polls_and_resets_next_match(self):
        app = self.make_view()
        selected = {"status": "champ_select", "is_aram": True}
        app.update_visibility(selected)
        app.toggle_visible()
        app.update_visibility(selected)
        app.update_visibility({"status": "waiting", "phase": "GameStart", "is_aram": True})
        app.update_visibility({"status": "live", "is_aram": True})
        self.assertTrue(app.hidden)
        app.update_visibility({"status": "waiting", "phase": "Lobby", "is_aram": False})
        app.update_visibility(selected)
        self.assertFalse(app.hidden)

    def test_manual_open_outside_match_is_preserved_until_next_match_ends(self):
        app = self.make_view()
        app.toggle_visible()
        app.update_visibility({"status": "waiting", "phase": "None", "is_aram": False})
        self.assertFalse(app.hidden)
        app.update_visibility({"status": "champ_select", "is_aram": True})
        app.update_visibility({"status": "waiting", "phase": "Lobby", "is_aram": False})
        self.assertTrue(app.hidden)

    def test_metadata_failure_does_not_cancel_manual_hide_within_a_match(self):
        app = self.make_view()
        selection = {"status": "champ_select", "phase": "ChampSelect", "is_aram": True}
        app.update_visibility(selection)
        app.toggle_visible()
        app.update_visibility({"status": "champ_select", "phase": "ChampSelect", "is_aram": None})
        app.update_visibility({"status": "error", "phase": None, "is_aram": None})
        app.update_visibility(selection)
        self.assertTrue(app.hidden)
        app.root.deiconify.assert_called_once()
        # An authoritative idle phase, even without mode metadata, ends it.
        app.update_visibility({"status": "waiting", "phase": "None", "is_aram": None})
        app.update_visibility(selection)
        self.assertFalse(app.hidden)

    def test_explicit_browse_mode_remains_available_and_can_be_hidden(self):
        app = self.make_view()
        app.browse_mode = True
        app.update_visibility({"status": "waiting", "is_aram": False})
        self.assertFalse(app.hidden)
        app.toggle_visible()
        app.update_visibility({"status": "waiting", "is_aram": False})
        self.assertTrue(app.hidden)

    def test_disconnected_game_clears_previous_champion_and_rates(self):
        app = self.make_view()
        app.handle_game({"status": "waiting"})
        self.assertIsNone(app.champion)
        self.assertEqual(app.offer_ids, [None, None, None])
        for card in app.cards:
            card["value"].configure.assert_called_with(text="—", fg="#edf4ff")

    def test_champion_select_resolves_id_opens_spells_and_fetches_before_game(self):
        app = self.make_view()
        app.champion = app.live_champion = app.spell_champion = None
        app.handle_game({"status": "champ_select", "champion_id": "157",
                         "summoner_spells": [{"id": "4", "name": "Flash"}, {"id": "32", "name": "Mark"}]})
        self.assertEqual(app.champion, "157")
        self.assertFalse(app.game_live)
        self.assertTrue(app.champ_select)
        self.assertEqual(app.live_spells[0]["id"], "4")
        app.switch_view.assert_called_once_with("spells")
        self.assertEqual(app.badge.configure.call_args.kwargs["text"], "CHAMP SELECT")
        self.assertEqual(app.requests.get_nowait(), ("spells", (8, "157")))

    def test_champion_trade_invalidates_old_pair_data_and_fetches_new_champion(self):
        app = self.make_view()
        app.catalog["champions"]["22"] = {"name": "Ashe", "riotId": "Ashe", "slug": "ashe"}
        app.game_status = "champ_select"
        app.handle_game({"status": "champ_select", "champion_id": "22"})
        self.assertEqual(app.champion, "22")
        self.assertEqual(app.requests.get_nowait(), ("spells", (8, "22")))
        app.handle_event("spells", (7, {"champion_id": "157", "rows": []}))
        self.assertIsNone(app.spell_data)

    def test_champion_select_without_pick_clears_previous_match(self):
        app = self.make_view()
        app.handle_game({"status": "champ_select", "champion_id": None})
        self.assertIsNone(app.champion)
        self.assertEqual(app.offer_ids, [None, None, None])
        app.switch_view.assert_called_once_with("spells")

    def test_unknown_champion_id_does_not_reuse_last_champion(self):
        app = self.make_view()
        app.handle_game({"status": "champ_select", "champion_id": "99999"})
        self.assertIsNone(app.champion)

    def test_tab_switch_happens_once_per_phase_and_manual_browse_is_preserved(self):
        app = self.make_view()
        selection = {"status": "champ_select", "champion_id": "157"}
        app.handle_game(selection)
        app.handle_game(selection)
        app.switch_view.assert_called_once_with("spells")
        app.handle_game({"status": "live", "champion_name": "Yasuo"})
        self.assertEqual(app.switch_view.call_args.args, ("augments",))
        app.follow = False
        app.switch_view.reset_mock()
        app.handle_game({"status": "champ_select", "champion_id": None})
        self.assertEqual(app.champion, "157")
        app.switch_view.assert_not_called()

    def test_spell_change_during_select_updates_current_pair_without_refetch(self):
        app = self.make_view()
        app.game_status = "champ_select"
        app.view.get.return_value = "spells"
        app.render_spells = Mock()
        app.handle_game({"status": "champ_select", "champion_id": "157",
                         "summoner_spells": [{"id": "4", "name": "Flash"}, {"id": "6", "name": "Ghost"}]})
        app.render_spells.assert_called_once()
        self.assertTrue(app.requests.empty())

    def test_return_to_auto_discards_manual_choices_and_requests_new_scan(self):
        app = self.make_view()
        app.follow = False
        app.auto_champion()
        self.assertTrue(app.follow)
        self.assertEqual(app.offer_ids, [None, None, None])
        self.assertEqual(app.requests.get_nowait(), ("scan", None))

    def test_stale_lookup_cannot_replace_current_selection(self):
        app = self.make_view()
        app.render_rates = Mock()
        app.handle_event("rates", (4, [{"win_rate": 1.0}]))
        app.render_rates.assert_not_called()

    def test_event_failure_still_schedules_next_drain(self):
        app = self.make_view()
        app.events.put(("game", {}))
        app.handle_event = Mock(side_effect=ValueError("bad event"))
        with self.assertLogs(level="ERROR"):
            app.drain()
        app.root.after.assert_called_once_with(100, app.drain)

    def test_missing_data_stays_blank_and_zero_rate_is_valid(self):
        app = self.make_view()
        app.render_rates([{"win_rate": None, "sample_count": 0},
                          {"win_rate": 0.0, "sample_count": 20}, None])
        app.cards[0]["value"].configure.assert_called_with(text="—", fg="#9aacc4")
        app.cards[1]["value"].configure.assert_called_with(text="0.0%", fg="#edf4ff")

    def test_champion_change_fetches_spells_without_any_augment_selected(self):
        app = self.make_view()
        app.offer_ids = [None] * 3
        app.champion = "41"
        app.lookup()
        self.assertEqual(app.requests.get_nowait(), ("spells", (8, "41")))
        app.lookup()
        self.assertTrue(app.requests.empty())

    def test_champion_edit_clears_spells_and_invalidates_inflight_result(self):
        app = self.make_view()
        app.champion = None
        app.lookup_spells()
        self.assertEqual(app.spell_token, 8)
        app.handle_event("spells", (7, {"champion_id": "157", "rows": []}))
        self.assertIsNone(app.spell_data)
        app.spell_tree.delete.assert_called_once()

    def test_spell_response_must_match_champion_and_request(self):
        app = self.make_view()
        app.render_spells = Mock()
        app.view.get.return_value = "spells"
        app.handle_event("spells", (7, {"champion_id": "41", "rows": []}))
        app.render_spells.assert_not_called()
        app.handle_event("spells", (7, {"champion_id": "157", "rows": []}))
        app.render_spells.assert_called_once()

    def test_spell_pairs_highlight_order_independently_and_mark_low_sample(self):
        app = self.make_view()
        app.live_spells = [{"id": "32"}, {"id": "4"}]
        app.spell_data = {"rows": [
            {"spell_ids": ["4", "32"], "spell_names": ["Flash", "Mark"], "win_rate": .58, "sample_count": 1000, "pick_rate": .9},
            {"spell_ids": ["4", "6"], "spell_names": ["Flash", "Ghost"], "win_rate": .75, "sample_count": 4, "pick_rate": .001},
        ]}
        app.render_spells()
        inserts = app.spell_tree.insert.call_args_list
        self.assertEqual(inserts[0].kwargs["values"][0], "Flash + Ghost")
        self.assertEqual(inserts[0].kwargs["values"][2], "4*")
        self.assertEqual(inserts[1].kwargs["tags"], ("current",))
        self.assertIn("Your current pair", app.spell_summary.set.call_args.args[0])

    def test_spell_zero_samples_are_blank_and_unknown_pair_does_not_highlight(self):
        app = self.make_view()
        app.live_spells = [{"id": None}, {"id": "4"}]
        app.spell_data = {"rows": [
            {"spell_ids": ["4", "6"], "spell_names": ["Flash", "Ghost"], "win_rate": None, "sample_count": 0, "pick_rate": None},
        ]}
        app.render_spells()
        result = app.spell_tree.insert.call_args.kwargs
        self.assertEqual(result["values"][1], "—")
        self.assertNotIn("current", result["tags"])

    def test_spell_pair_highlights_during_champion_select(self):
        app = self.make_view()
        app.game_live = False
        app.champ_select = True
        app.live_spells = [{"id": "32", "name": "Mark"}, {"id": "4", "name": "Flash"}]
        app.spell_data = {"rows": [
            {"spell_ids": ["4", "32"], "spell_names": ["Flash", "Mark"], "win_rate": .58,
             "sample_count": 1000, "pick_rate": .9}]}
        app.render_spells()
        self.assertEqual(app.spell_tree.insert.call_args.kwargs["tags"], ("current",))
        self.assertEqual(app.status.set.call_args.args[0], "Champion select · Mark + Flash")

    def test_background_augment_response_does_not_replace_spells_source_date(self):
        app = self.make_view()
        app.view.get.return_value = "spells"
        app.render_rates([{"win_rate": .5, "sample_count": 200, "version": "old"}])
        app.source.set.assert_not_called()

    def test_auto_scan_still_populates_choices_for_manual_champion(self):
        app = self.make_view()
        app.follow = False
        app.catalog["augments"] = {"1": {"name": "Goliath"}, "2": {"name": "Tank Engine"}}
        app.lookup = Mock()
        app.handle_event("offers", [{"id": "1"}, {"id": "2"}])
        self.assertEqual(app.offer_ids, ["1", "2", None])
        app.lookup.assert_called_once()

    def test_scan_paused_message_is_visible_with_retained_choices(self):
        app = self.make_view()
        app.handle_event("scan_status", "Scan paused · Showing last recognized choices.")
        app.status.set.assert_called_once()
        self.assertEqual(app.offer_ids, ["1", "2", "3"])

    def test_locked_panel_still_discloses_paused_scan(self):
        app = self.make_view()
        app.locked = True
        app.handle_event("scan_status", "Scan paused · Showing last recognized choices.")
        message = app.status.set.call_args.args[0]
        self.assertIn("Scan paused", message)
        self.assertIn("Ctrl+Alt+L to unlock", message)
        self.assertEqual(app.offer_ids, ["1", "2", "3"])


if __name__ == "__main__":
    unittest.main()
