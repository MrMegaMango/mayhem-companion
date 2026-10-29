"""Arena routing never reuses ARAM statistics or stale selection replies."""
import unittest
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
        self.assertEqual(list(app.requests.queue), [("arena", (1, False)), ("scan", None)])
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
