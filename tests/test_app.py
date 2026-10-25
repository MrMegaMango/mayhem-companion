"""State regression tests use a fake view; no game interaction or desktop input."""
import queue
import unittest
from unittest.mock import Mock

from mayhem.app import Companion


class StateTests(unittest.TestCase):
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
        app.game_live = True
        app.token = 5
        app.is_auto_offer = True
        app.offer_ids = ["1", "2", "3"]
        app.cards = [{"var": Mock(), "value": Mock(), "meta": Mock()} for _ in range(3)]
        return app

    def test_game_updates_resolve_locally_without_http(self):
        app = self.make_view()
        app.handle_game({"status": "live", "raw_champion": "game_character_displayname_Yasuo"})
        self.assertEqual(app.champion, "157")
        self.assertFalse(app.worker.provider.mock_calls)

    def test_disconnected_game_clears_previous_champion_and_rates(self):
        app = self.make_view()
        app.handle_game({"status": "waiting"})
        self.assertIsNone(app.champion)
        self.assertEqual(app.offer_ids, [None, None, None])
        for card in app.cards:
            card["value"].configure.assert_called_with(text="—", fg="#edf4ff")

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


if __name__ == "__main__":
    unittest.main()
