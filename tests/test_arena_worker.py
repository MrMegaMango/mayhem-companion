"""Arena routing, catalog publication, and background/OCR isolation."""

import itertools
import queue
import threading
import unittest
from unittest.mock import Mock, call, patch

from mayhem.worker import Worker
from tests.test_worker_latency import EventLog


ARENA = {"status": "live", "phase": "InProgress", "game_mode": "CHERRY",
         "is_arena": True, "is_aram": False, "is_tft": False,
         "champion_id": "157", "queue_id": 1700}
CATALOG = {"champions": {"157": {"name": "Yasuo"}},
           "augments": {"41": {"name": "Goliath"}, "48": {"name": "Jeweled Gauntlet"}}}
OFFERS = [{"id": "41", "name": "Goliath"}, {"id": "48", "name": "Jeweled Gauntlet"}]


class ArenaWorkerTests(unittest.TestCase):
    def make_worker(self, *, threaded=False):
        self.aram = self.enterContext(patch("mayhem.worker.StatsProvider")).return_value
        self.arena_factory = self.enterContext(patch("mayhem.worker.ArenaProvider"))
        self.arena = self.arena_factory.return_value
        self.client = self.enterContext(patch("mayhem.worker.ClientReader")).return_value
        self.capture = self.enterContext(patch("mayhem.worker.CaptureReader")).return_value
        self.live = self.enterContext(patch("mayhem.worker.poll_active_player"))
        self.client.poll.return_value = {"status": "waiting"}
        self.live.return_value = {"status": "waiting"}
        events = EventLog() if threaded else queue.Queue()
        stop = threading.Event() if threaded else Mock()
        worker = Worker(events, queue.Queue(), stop, ".")
        worker.queue_aram_task = Mock()
        worker.prewarm_champions = Mock()
        return worker, events

    def run_steps(self, worker, steps=1):
        worker.stop.wait.side_effect = [False] * steps + [True]
        ticks = itertools.count(10, 1)
        with patch("mayhem.worker.time.monotonic", side_effect=lambda: next(ticks)):
            worker.run()

    def prepare_scan(self, worker):
        worker.arena_catalog = CATALOG
        worker.arena_catalog_token = 7
        worker.scan_mode = "arena"
        worker.arena_scan_champion = "157"
        worker.poll_game = Mock(return_value=dict(ARENA))
        self.capture.scan.return_value = {"status": "ok", "lines": []}

    def test_arena_selection_does_not_query_lingering_live_game(self):
        worker, _ = self.make_worker()
        selection = {**ARENA, "status": "champ_select", "phase": "ChampSelect"}
        self.client.poll.return_value = selection
        self.assertEqual(worker.poll_game(), selection)
        self.live.assert_not_called()

    def test_live_arena_fallback_works_when_client_is_unavailable(self):
        worker, _ = self.make_worker()
        self.client.poll.return_value = {"status": "error"}
        self.live.return_value = {"status": "live", "game_mode": "CHERRY", "champion_id": "157"}
        result = worker.poll_game()
        self.assertTrue(result["is_arena"])
        self.assertFalse(result["is_aram"])
        self.assertEqual(result["champion_id"], "157")
        self.assertNotIn("queue_id", result)

    def test_live_arena_preserves_actual_queue_variant(self):
        worker, _ = self.make_worker()
        self.client.poll.return_value = {**ARENA, "status": "waiting", "queue_id": 1750}
        self.live.return_value = {"status": "live", "game_mode": "CHERRY", "champion_id": "157"}
        result = worker.poll_game()
        self.assertTrue(result["is_arena"])
        self.assertEqual(result["queue_id"], 1750)
        self.assertEqual(result["phase"], "InProgress")

    def test_loading_does_not_merge_previous_modes_live_player(self):
        worker, _ = self.make_worker()
        cases = (
            ({**ARENA, "status": "waiting", "phase": "GameStart"}, "KIWI"),
            ({"status": "waiting", "phase": "GameStart", "is_arena": False,
              "is_aram": True, "game_mode": "KIWI"}, "CHERRY"),
        )
        for selection, stale_mode in cases:
            with self.subTest(stale_mode=stale_mode):
                self.client.poll.return_value = selection
                self.live.return_value = {"status": "live", "game_mode": stale_mode,
                                          "champion_id": "99"}
                self.assertEqual(worker.poll_game(), selection)

    def test_catalog_is_published_before_scanning_with_it(self):
        worker, events = self.make_worker()
        self.arena.load.return_value = CATALOG
        worker.perform_arena_task("arena", (7, False))
        self.assertIsNone(worker.arena_catalog)
        self.assertEqual(list(events.queue), [])
        worker.poll_game = Mock(return_value=dict(ARENA))

        def capture(**kwargs):
            self.assertIs(worker.arena_catalog, CATALOG)
            self.assertIn(("arena", (7, CATALOG)), list(events.queue))
            return {"status": "ok", "lines": []}

        self.capture.scan.side_effect = capture
        with patch("mayhem.worker.detect_offers", return_value=OFFERS):
            self.run_steps(worker, 2)
        self.assertIn(("arena_offers", (7, OFFERS)), list(events.queue))
        self.assertEqual(self.capture.scan.call_args_list, [call(mode="arena"), call(mode="arena")])
        self.aram.load_catalog.assert_not_called()

    def test_champion_requests_and_errors_preserve_their_token_and_identity(self):
        worker, events = self.make_worker()
        data = {"champion_id": "157", "augments": []}
        self.arena.champion_for.return_value = data
        worker.perform_arena_task("arena_champion", (12, "157", False))
        self.arena.champion_for.side_effect = ValueError("Source unavailable")
        worker.perform_arena_task("arena_champion", (13, "99", True))
        self.assertEqual(list(events.queue), [
            ("arena_champion", (12, "157", data)),
            ("arena_champion_error", (13, "99", "Source unavailable")),
        ])
        self.aram.rate_for.assert_not_called()
        self.aram.item_builds_for.assert_not_called()

    def test_catalog_error_is_separate_from_champion_error(self):
        worker, events = self.make_worker()
        self.arena.load.side_effect = ValueError("Catalog unavailable")
        worker.perform_arena_task("arena", (7, True))
        self.assertEqual(list(events.queue), [("arena_error", (7, "Catalog unavailable"))])
        self.assertIsNone(worker.arena_catalog)

    def test_arena_scan_uses_only_arena_catalog_and_separate_events(self):
        worker, events = self.make_worker()
        self.prepare_scan(worker)
        worker.catalog = {"augments": {"1041": {"name": "Goliath"}}}
        with patch("mayhem.worker.detect_offers", return_value=OFFERS) as detect:
            self.run_steps(worker, 2)
        self.assertTrue(all(c.args[1] is CATALOG["augments"] for c in detect.call_args_list))
        self.assertIn(("arena_offers", (7, OFFERS)), list(events.queue))
        self.assertFalse(any(kind in ("offers", "scan_status", "rates") for kind, _ in events.queue))
        self.assertTrue(any(kind == "arena_scan_status" for kind, _ in events.queue))

    def test_catalog_refresh_republishes_unchanged_choices_with_new_token(self):
        worker, events = self.make_worker()
        self.prepare_scan(worker)
        worker.last_offers = worker.pending_offers = ("41", "48")
        worker.arena_catalog_results.put((8, CATALOG))
        with patch("mayhem.worker.detect_offers", return_value=OFFERS):
            self.run_steps(worker, 2)
        self.assertIn(("arena", (8, CATALOG)), list(events.queue))
        self.assertIn(("arena_offers", (8, OFFERS)), list(events.queue))

    def test_champion_change_clears_old_offers_and_requires_two_new_readings(self):
        worker, events = self.make_worker()
        self.prepare_scan(worker)
        worker.last_offers = worker.pending_offers = ("41", "48")
        worker.poll_game.return_value = {**ARENA, "champion_id": "99"}
        with patch("mayhem.worker.detect_offers", return_value=OFFERS):
            self.run_steps(worker, 1)
        self.assertIn(("arena_offers", (7, [])), list(events.queue))
        self.assertNotIn(("arena_offers", (7, OFFERS)), list(events.queue))

    def test_leaving_arena_clears_its_offers_before_aram_scan(self):
        worker, events = self.make_worker()
        self.prepare_scan(worker)
        worker.last_offers = worker.pending_offers = ("41", "48")
        worker.catalog = {"augments": {"1041": {"name": "Goliath"}}}
        worker.poll_game.return_value = {"status": "live", "is_arena": False, "is_aram": True,
                                         "champion_id": "157", "game_mode": "KIWI"}
        with patch("mayhem.worker.detect_offers", return_value=[]):
            self.run_steps(worker, 1)
        self.assertIn(("arena_offers", (7, [])), list(events.queue))
        self.capture.scan.assert_called_once_with()
        self.assertEqual(worker.last_offers, ())

    def test_focus_loss_keeps_arena_choices_without_triggering_aram_ui(self):
        worker, events = self.make_worker()
        self.prepare_scan(worker)
        worker.last_offers = worker.pending_offers = ("41", "48")
        self.capture.scan.return_value = {"status": "waiting"}
        self.run_steps(worker)
        self.assertEqual(worker.last_offers, ("41", "48"))
        self.assertFalse(any(kind in ("offers", "arena_offers", "scan_status") for kind, _ in events.queue))
        self.assertTrue(any(kind == "arena_scan_status" and "Showing last recognized" in message
                            for kind, message in events.queue))

    def test_arena_does_not_scan_with_only_mayhem_catalog(self):
        worker, _ = self.make_worker()
        worker.catalog = CATALOG
        worker.poll_game = Mock(return_value=dict(ARENA))
        self.run_steps(worker)
        self.capture.scan.assert_not_called()

    def test_blocked_arena_http_does_not_delay_polling_and_coalesces_pending_champions(self):
        worker, events = self.make_worker(threaded=True)
        worker.poll_game = Mock(return_value={**ARENA, "status": "champ_select", "phase": "ChampSelect"})
        entered, release = threading.Event(), threading.Event()
        thread_ids = []

        def catalog(*, force):
            thread_ids.append(threading.get_ident())
            entered.set()
            if not release.wait(5):
                raise AssertionError("Blocked request was not released")
            return CATALOG

        self.arena.load.side_effect = catalog
        self.arena.champion_for.return_value = {"champion_id": "99"}

        def shutdown():
            worker.stop.set()
            release.set()
            worker.arena_ready.set()
            for thread in (worker, worker.arena_thread):
                if thread is not None and thread.ident is not None:
                    thread.join(3)
                    self.assertFalse(thread.is_alive())

        self.addCleanup(shutdown)
        worker.queue_arena_task("arena", (7, False))
        self.assertTrue(entered.wait(3))
        worker.queue_arena_task("arena_champion", (1, "157", False))
        worker.queue_arena_task("arena_champion", (2, "99", True))
        worker.start()
        events.wait_until(lambda rows: sum(kind == "game" for kind, _ in rows) >= 2)
        self.assertFalse(release.is_set())
        self.assertEqual(thread_ids, [worker.arena_thread.ident])
        self.assertNotEqual(worker.ident, worker.arena_thread.ident)
        release.set()
        events.wait_until(lambda rows: any(kind == "arena_champion" for kind, _ in rows))
        self.arena.champion_for.assert_called_once_with("99", force=True)
        self.assertIn(("arena_champion", (2, "99", {"champion_id": "99"})), events.snapshot())
        self.capture.scan.assert_not_called()


if __name__ == "__main__":
    unittest.main()
