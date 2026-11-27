"""Arena routing never reuses ARAM statistics or stale selection replies."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from mayhem.app import Companion
from tests import test_app


class ArenaAppTests(unittest.TestCase):
    setUp = test_app.StateTests.setUp
    make_view = test_app.StateTests.make_view

    def arena_game(self, **values):
        return {"status": "champ_select", "phase": "ChampSelect", "is_arena": True,
                "is_aram": False, "is_tft": False, "champion_id": "157", **values}

    def test_auto_opens_arena_during_selection_without_aram_lookups(self):
        app = self.make_view()
        game = self.arena_game()
        app.handle_game(game)
        self.assertEqual(app.display_mode, "arena")
        self.assertFalse(app.hidden)
        self.assertEqual(list(app.requests.queue), [("arena", (1, False, "current")), ("scan", None)])
        app.arena_panel.handle_game.assert_called_with(game)
        app.brand.configure.assert_called_with(text="ARENA")

    def test_arena_loading_remains_visible_then_lobby_hides(self):
        app = self.make_view()
        app.handle_game(self.arena_game())
        app.handle_game(self.arena_game(status="waiting", phase="GameStart"))
        self.assertFalse(app.hidden)
        app.handle_game({"status": "waiting", "phase": "Lobby", "is_arena": None})
        self.assertTrue(app.hidden)

    def test_manual_arena_mode_does_not_follow_aram(self):
        app = self.make_view()
        app.mode_choice.get.return_value = "Arena"
        app.last_game_context = {"status": "live", "is_aram": True, "is_tft": False}
        app.change_game_mode()
        self.assertEqual(app.display_mode, "arena")
        self.assertTrue(app.visibility_override)

    def test_leaving_arena_resets_panel_context(self):
        app = self.make_view()
        app.handle_game(self.arena_game())
        app.arena_panel.handle_game.reset_mock()
        app.handle_game({"status": "tft", "is_tft": True, "is_arena": False})
        self.assertEqual(app.display_mode, "tft")
        app.arena_panel.handle_game.assert_called_once_with({})

    def test_stale_champion_result_and_error_cannot_update_new_champion(self):
        app = self.make_view()
        app.load_arena_champion("157")
        old = app.arena_champion_token
        app.load_arena_champion("99")
        app.handle_event("arena_champion", (old, "157", {"champion_id": "157"}))
        app.handle_event("arena_champion_error", (old, "157", "old error"))
        app.handle_event("arena_champion", (app.arena_champion_token, "99", {"champion_id": "157"}))
        app.arena_panel.set_champion_data.assert_not_called()
        app.arena_panel.set_champion_error.assert_not_called()
        data = {"champion_id": "99", "items": []}
        app.handle_event("arena_champion", (app.arena_champion_token, "99", data))
        app.arena_panel.set_champion_data.assert_called_once_with("99", data)

    def test_cleared_champion_invalidates_pending_result(self):
        app = self.make_view()
        app.load_arena_champion("157")
        token = app.arena_champion_token
        app.load_arena_champion(None)
        app.handle_event("arena_champion", (token, "157", {"champion_id": "157"}))
        app.arena_panel.set_champion_data.assert_not_called()

    def test_offers_are_separated_by_mode_and_catalog_generation(self):
        app = self.make_view()
        app.display_mode = "arena"
        app.arena_token = 4
        offers = [{"id": "41", "name": "Goliath"}]
        app.handle_event("offers", offers)
        app.handle_event("arena_offers", (3, offers))
        app.arena_panel.set_offers.assert_not_called()
        self.assertEqual(app.offer_ids, ["1", "2", "3"])
        app.handle_event("arena_offers", (4, offers))
        app.arena_panel.set_offers.assert_called_once_with(offers)
        app.display_mode = "aram"
        app.handle_event("arena_offers", (4, offers))
        self.assertEqual(app.arena_panel.set_offers.call_count, 1)

    def test_late_aram_catalog_does_not_trigger_aram_data_in_arena(self):
        app = self.make_view()
        app.handle_game(self.arena_game())
        app.requests.queue.clear()
        app.champ_box = test_app.MagicMock()
        for card in app.cards:
            card["box"] = test_app.MagicMock()
        app.handle_event("catalog", {"champions": {"157": {"name": "Yasuo"}}, "augments": {}})
        self.assertEqual(list(app.requests.queue), [])

    def test_catalog_failure_releases_retry_and_stale_reply_is_ignored(self):
        app = self.make_view()
        app.load_arena()
        app.handle_event("arena_error", (1, "temporary error"))
        self.assertFalse(app.arena_pending)
        app.load_arena(force=True)
        app.handle_event("arena", (1, {"champions": {}}))
        app.arena_panel.set_catalog.assert_not_called()
        app.handle_event("arena", (2, {"champions": {}}))
        app.arena_panel.set_catalog.assert_called_once()
        self.assertFalse(app.arena_pending)


    def test_saved_window_preference_and_first_run_default(self):
        class StopAfterSettings(Exception):
            pass
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for value, expected in ((None, "30"), ("current", "current"), ("30", "30"), ("invalid", "30")):
                with self.subTest(value=value):
                    (root / "settings.json").write_text(json.dumps({"arena_window": value}), encoding="utf-8")
                    app = object.__new__(Companion)
                    with patch("mayhem.app.app_dir", return_value=root), \
                            patch.object(Companion, "build", side_effect=StopAfterSettings), \
                            self.assertRaises(StopAfterSettings):
                        app.__init__(Mock())
                    self.assertEqual(app.arena_window, expected)

    def test_window_switch_clears_data_and_rejects_previous_window_replies(self):
        app = self.make_view()
        app.settings, app.settings_path = {}, Mock()
        app.arena_window = "current"
        app.load_arena()
        app.load_arena_champion("157")
        old_catalog, old_champion = app.arena_token, app.arena_champion_token
        app.requests.queue.clear()
        app.change_arena_window("30")
        self.assertEqual(app.arena_window, "30")
        self.assertEqual(app.settings["arena_window"], "30")
        self.assertEqual(json.loads(app.settings_path.write_text.call_args.args[0])["arena_window"], "30")
        app.arena_panel.set_window.assert_called_once_with("30")
        self.assertEqual(list(app.requests.queue), [
            ("arena", (old_catalog + 1, False, "30")),
            ("arena_champion", (old_champion + 1, "157", False, "30")),
        ])
        app.handle_event("arena", (old_catalog, {"data_window": "current", "champions": {}}))
        app.handle_event("arena_error", (old_catalog, "old failure"))
        app.handle_event("arena_champion", (old_champion, "157", {"data_window": "current", "champion_id": "157"}))
        app.handle_event("arena_champion_error", (old_champion, "157", "old failure"))
        app.arena_panel.set_catalog.assert_not_called()
        app.arena_panel.set_champion_data.assert_not_called()
        app.arena_panel.set_error.assert_not_called()
        app.arena_panel.set_champion_error.assert_not_called()
        self.assertTrue(app.arena_pending)
        catalog = {"data_window": "30", "champions": {}}
        champion = {"data_window": "30", "champion_id": "157", "items": []}
        app.handle_event("arena", (app.arena_token, catalog))
        app.handle_event("arena_champion", (app.arena_champion_token, "157", champion))
        app.arena_panel.set_catalog.assert_called_once_with(catalog)
        app.arena_panel.set_champion_data.assert_called_once_with("157", champion)
        self.assertFalse(app.arena_pending)

    def test_quick_window_switch_back_invalidates_intermediate_window(self):
        app = self.make_view()
        app.settings, app.settings_path = {}, Mock()
        app.arena_window = "current"
        app.arena_champion = "157"
        app.change_arena_window("30")
        token, champion_token = app.arena_token, app.arena_champion_token
        app.change_arena_window("current")
        app.handle_event("arena", (token, {"data_window": "30"}))
        app.handle_event("arena_champion", (champion_token, "157", {"data_window": "30", "champion_id": "157"}))
        app.arena_panel.set_catalog.assert_not_called()
        app.arena_panel.set_champion_data.assert_not_called()
        self.assertEqual(app.arena_window, "current")
        self.assertEqual(list(app.requests.queue)[-1],
                         ("arena_champion", (champion_token + 1, "157", False, "current")))

    def test_wrong_period_reply_cannot_paint_and_catalog_retry_is_released(self):
        app = self.make_view()
        app.arena_window = "30"
        app.load_arena()
        app.load_arena_champion("157")
        app.handle_event("arena", (app.arena_token, {"data_window": "current"}))
        app.handle_event("arena_champion", (app.arena_champion_token, "157", {"data_window": "current", "champion_id": "157"}))
        app.arena_panel.set_catalog.assert_not_called()
        app.arena_panel.set_champion_data.assert_not_called()
        self.assertFalse(app.arena_pending)
        app.arena_panel.set_error.assert_called_once()

    def test_window_change_without_champion_and_preference_write_failure(self):
        app = self.make_view()
        app.arena_window = "current"
        app.settings, app.settings_path = {}, Mock()
        app.settings_path.write_text.side_effect = OSError("Read only")
        app.change_arena_window("30")
        self.assertEqual(list(app.requests.queue), [("arena", (1, False, "30"))])
        self.assertEqual(app.arena_champion_token, 1)
        app.requests.queue.clear()
        app.change_arena_window("30")
        app.change_arena_window("invalid")
        self.assertEqual(list(app.requests.queue), [])
