"""Thread/event regressions; network, local APIs and capture are all mocked."""

import queue
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch

from mayhem.data import DataUnavailableError
from mayhem.worker import Worker


CATALOG = {"champions": {"157": {"name": "Yasuo"}}, "augments": {"1": {"name": "Test"}}}
SELECTING = {"status": "champ_select", "phase": "ChampSelect", "is_aram": True,
             "champion_id": "157", "available_champion_ids": ["157", "62"]}


class EventLog(queue.Queue):
    """Observe emissions without sleeps or consuming another assertion's events."""

    def __init__(self):
        super().__init__()
        self.changed = threading.Condition()
        self.history = []

    def put(self, item, *args, **kwargs):
        super().put(item, *args, **kwargs)
        with self.changed:
            self.history.append(item)
            self.changed.notify_all()

    def wait_until(self, predicate, timeout=3):
        deadline = time.monotonic() + timeout
        with self.changed:
            while not predicate(self.history):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AssertionError(f"Expected worker event was not emitted: {self.history!r}")
                self.changed.wait(remaining)
            return list(self.history)

    def snapshot(self):
        with self.changed:
            return list(self.history)


class WorkerLatencyTests(unittest.TestCase):
    def make_worker(self, champion_providers=None):
        provider = Mock()
        provider.load_catalog.return_value = CATALOG
        provider.summoner_spells_for.return_value = {"champion_id": "62", "rows": []}
        provider.item_builds_for.return_value = {"champion_id": "62", "groups": []}
        provider.rate_for.side_effect = lambda champion, ident, stage: {
            "champion_id": champion, "augment_id": ident, "stage": stage,
        }
        champion_providers = champion_providers or [Mock()]
        for champion_provider in champion_providers:
            champion_provider.champion_rates_for.return_value = {"rows": []}
        factory = self.enterContext(patch("mayhem.worker.StatsProvider",
                                         side_effect=[provider, *champion_providers]))
        self.enterContext(patch("mayhem.worker.ClientReader"))
        self.enterContext(patch("mayhem.worker.CaptureReader"))
        self.enterContext(patch("mayhem.worker.poll_active_player",
                                side_effect=AssertionError("Unexpected local API call")))
        events, requests, stop = EventLog(), queue.Queue(), threading.Event()
        worker = Worker(events, requests, stop, ".")
        worker.poll_game = Mock(return_value=dict(SELECTING))
        releases = []

        def shutdown():
            stop.set()
            for release in releases:
                release.set()
            worker.data_ready.set()
            for thread in (worker, worker.data_thread, worker.champion_thread, worker.tft_thread):
                if thread is not None and thread.ident is not None:
                    thread.join(3)
                    self.assertFalse(thread.is_alive(), f"Worker thread did not stop: {thread.name}")

        self.addCleanup(shutdown)
        return worker, events, requests, provider, champion_providers, releases, shutdown, factory

    def assert_public_request_does_not_block_local_work(self, task):
        worker, events, requests, provider, _, releases, _, _ = self.make_worker()
        entered, release = threading.Event(), threading.Event()
        releases.append(release)
        executing_threads = []

        def blocked(*_):
            executing_threads.append(threading.get_ident())
            entered.set()
            if not release.wait(5):
                raise AssertionError("Test did not release the public request")
            return CATALOG if task == "catalog" else {"champion_id": "157", "rows": []}

        method = {"catalog": provider.load_catalog, "spells": provider.summoner_spells_for,
                  "builds": provider.item_builds_for}[task]
        method.side_effect = blocked
        if task != "catalog":
            requests.put((task, (11, "157")))
        worker.start()
        self.assertTrue(entered.wait(3), "Public request did not start")
        seen = events.wait_until(lambda items: any(kind == "game" for kind, _ in items))
        game_count = sum(kind == "game" for kind, _ in seen)
        requests.put(("champions", (41, ["157", "62"])))
        events.wait_until(lambda items: any(kind == "champions" and payload[0] == 41
                                           for kind, payload in items))
        events.wait_until(lambda items: sum(kind == "game" for kind, _ in items) > game_count)
        self.assertFalse(release.is_set(), "Assertions must run while HTTP is still blocked")
        self.assertEqual(executing_threads, [worker.data_thread.ident])
        self.assertNotEqual(worker.data_thread.ident, worker.ident)
        self.assertNotEqual(worker.champion_thread.ident, worker.ident)
        worker.capture.scan.assert_not_called()

    def test_blocked_initial_catalog_does_not_delay_local_game_or_champion_dispatch(self):
        self.assert_public_request_does_not_block_local_work("catalog")

    def test_blocked_spell_request_does_not_delay_local_game_or_champion_dispatch(self):
        self.assert_public_request_does_not_block_local_work("spells")

    def test_blocked_build_request_does_not_delay_local_game_or_champion_dispatch(self):
        self.assert_public_request_does_not_block_local_work("builds")

    def test_aram_queue_coalesces_each_kind_without_discarding_other_views(self):
        worker, events, _, provider, _, releases, _, _ = self.make_worker()
        entered, release = threading.Event(), threading.Event()
        releases.append(release)

        def catalog():
            entered.set()
            if not release.wait(5):
                raise AssertionError("Test did not release the catalog")
            return CATALOG

        provider.load_catalog.side_effect = catalog
        worker.queue_aram_task("catalog", None)
        self.assertTrue(entered.wait(3))
        for task, payload in (
            ("spells", (1, "157")), ("builds", (2, "157")),
            ("lookup", (3, "157", ["1"], None)),
            ("spells", (10, "62")), ("builds", (20, "62")),
            ("lookup", (30, "62", ["2"], 2)),
        ):
            worker.queue_aram_task(task, payload)
        release.set()
        observed = events.wait_until(lambda items: {kind for kind, _ in items} >= {"spells", "builds", "rates"})
        provider.summoner_spells_for.assert_called_once_with("62")
        provider.item_builds_for.assert_called_once_with("62")
        provider.rate_for.assert_called_once_with("62", "2", 2)
        self.assertEqual({kind: payload[0] for kind, payload in observed
                          if kind in ("spells", "builds", "rates")},
                         {"spells": 10, "builds": 20, "rates": 30})


    def test_catalog_handoff_is_published_by_polling_thread_before_new_offers(self):
        worker, events, _, _, _, _, _, _ = self.make_worker()
        loaded = threading.Event()
        put = worker.catalog_results.put

        def complete(data):
            put(data)
            loaded.set()

        with patch.object(worker.catalog_results, "put", side_effect=complete):
            worker.queue_aram_task("catalog", None)
            self.assertTrue(loaded.wait(3))
        self.assertIsNone(worker.catalog)
        self.assertFalse(any(kind == "catalog" for kind, _ in events.snapshot()))
        worker.queue_aram_task = Mock()
        worker.prewarm_champions = Mock()
        worker.poll_game.return_value = {"status": "live", "phase": "InProgress", "is_aram": True}
        worker.pending_offers = ("1", "2")
        emit = worker.emit
        emit_threads = []

        def record(kind, payload):
            emit_threads.append((kind, threading.get_ident()))
            emit(kind, payload)

        def capture():
            self.assertIs(worker.catalog, CATALOG)
            self.assertTrue(any(kind == "catalog" for kind, _ in events.snapshot()))
            return {"status": "ok", "lines": []}

        worker.emit = record
        worker.capture.scan.side_effect = capture
        with patch("mayhem.worker.detect_offers", return_value=[{"id": "1"}, {"id": "2"}]):
            worker.start()
            observed = events.wait_until(lambda items: any(kind == "offers" for kind, _ in items))
        kinds = [kind for kind, _ in observed]
        self.assertLess(kinds.index("catalog"), kinds.index("offers"))
        self.assertTrue(all(ident == worker.ident for kind, ident in emit_threads
                            if kind in ("catalog", "offers")))

    def test_failed_speculative_warmup_is_silent_and_demand_gets_fresh_provider(self):
        failed, recovered = Mock(), Mock()
        worker, events, _, _, _, _, _, factory = self.make_worker(champion_providers=[failed, recovered])
        entered = threading.Event()

        def warmup(_ids):
            entered.set()
            raise DataUnavailableError("Warmup offline", transient=True)

        failed.champion_rates_for.side_effect = warmup
        recovered.champion_rates_for.return_value = {"rows": [{"champion_id": "157", "win_rate": .58}]}
        worker.prewarm_champions()
        self.assertTrue(entered.wait(3))
        worker.queue_champion_task((9, ["157"]))
        observed = events.wait_until(lambda items: any(kind == "champions" for kind, _ in items))
        self.assertEqual(observed, [("champions", (9, recovered.champion_rates_for.return_value))])
        failed.champion_rates_for.assert_called_once_with([])
        recovered.champion_rates_for.assert_called_once_with(["157"])
        self.assertIs(worker.champion_provider, recovered)
        self.assertEqual(factory.call_count, 3)

    def test_successful_warmup_is_silent_and_reuses_provider_for_visible_choices(self):
        worker, events, _, _, champions, _, _, factory = self.make_worker()
        warmed = threading.Event()

        def rates(ids):
            if not ids:
                warmed.set()
            return {"rows": [{"champion_id": ident} for ident in ids]}

        champions[0].champion_rates_for.side_effect = rates
        worker.prewarm_champions()
        self.assertTrue(warmed.wait(3))
        worker.queue_champion_task((7, ["62"]))
        observed = events.wait_until(lambda items: any(kind == "champions" for kind, _ in items))
        self.assertEqual(observed, [("champions", (7, {"rows": [{"champion_id": "62"}]}))])
        self.assertEqual(champions[0].champion_rates_for.call_args_list, [call([]), call(["62"])])
        self.assertEqual(factory.call_count, 2)


    def test_warmup_never_replaces_pending_visible_choice_request(self):
        worker, events, _, _, champions, releases, _, _ = self.make_worker()
        entered, release = threading.Event(), threading.Event()
        releases.append(release)

        def rates(ids):
            if ids == ["157"]:
                entered.set()
                if not release.wait(5):
                    raise AssertionError("Test did not release the champion request")
            return {"rows": [{"champion_id": ident} for ident in ids]}

        champions[0].champion_rates_for.side_effect = rates
        worker.queue_champion_task((1, ["157"]))
        self.assertTrue(entered.wait(3))
        worker.queue_champion_task((2, ["62"]))
        worker.prewarm_champions()
        release.set()
        observed = events.wait_until(lambda items: sum(kind == "champions" for kind, _ in items) == 2)
        self.assertEqual([payload[0] for kind, payload in observed if kind == "champions"], [1, 2])
        self.assertEqual(champions[0].champion_rates_for.call_args_list, [call(["157"]), call(["62"])])

    def wait_champion_idle(self, worker):
        with worker.champion_requests.all_tasks_done:
            self.assertTrue(worker.champion_requests.all_tasks_done.wait_for(
                lambda: worker.champion_requests.unfinished_tasks == 0, timeout=3))

    def test_speculative_warmup_cannot_clear_a_real_demand_failure_backoff(self):
        failed, unexpected_replacement = Mock(), Mock()
        worker, events, _, _, _, _, _, factory = self.make_worker(
            champion_providers=[failed, unexpected_replacement])
        failed.champion_rates_for.side_effect = DataUnavailableError("Demand offline", transient=True)
        worker.queue_champion_task((1, ["157"]))
        events.wait_until(lambda items: any(kind == "champions_error" and payload[0] == 1
                                           for kind, payload in items))
        self.wait_champion_idle(worker)
        worker.prewarm_champions()
        self.wait_champion_idle(worker)
        worker.queue_champion_task((2, ["62"]))
        observed = events.wait_until(lambda items: any(kind == "champions_error" and payload[0] == 2
                                                      for kind, payload in items))
        self.assertEqual(observed, [("champions_error", (1, "Demand offline")),
                                    ("champions_error", (2, "Demand offline"))])
        self.assertEqual(failed.champion_rates_for.call_args_list, [call(["157"]), call(["62"])])
        self.assertIs(worker.champion_provider, failed)
        self.assertEqual(factory.call_count, 2)

    def test_stopping_during_public_request_does_not_start_queued_downloads(self):
        worker, _, _, provider, _, releases, shutdown, _ = self.make_worker()
        entered, release = threading.Event(), threading.Event()
        releases.append(release)

        def catalog():
            entered.set()
            if not release.wait(5):
                raise AssertionError("Test did not release the catalog")
            return CATALOG

        provider.load_catalog.side_effect = catalog
        worker.queue_aram_task("catalog", None)
        self.assertTrue(entered.wait(3))
        worker.queue_aram_task("spells", (1, "157"))
        shutdown()
        provider.summoner_spells_for.assert_not_called()


    def test_poll_cadence_uses_completion_time_and_is_faster_only_around_selection(self):
        cases = (
            ({"status": "champ_select"}, Worker.SELECT_POLL_SECONDS),
            ({"status": "waiting", "phase": "ChampSelect"}, Worker.SELECT_POLL_SECONDS),
            ({"status": "waiting", "phase": "Matchmaking"}, Worker.SELECT_POLL_SECONDS),
            ({"status": "waiting", "phase": "ReadyCheck"}, Worker.SELECT_POLL_SECONDS),
            ({"status": "live", "phase": "InProgress"}, Worker.DEFAULT_POLL_SECONDS),
            ({"status": "tft", "phase": "InProgress"}, Worker.DEFAULT_POLL_SECONDS),
            ({"status": "waiting", "phase": "Lobby"}, Worker.DEFAULT_POLL_SECONDS),
            ({"status": "waiting"}, Worker.DEFAULT_POLL_SECONDS),
        )
        self.assertLess(Worker.SELECT_POLL_SECONDS, Worker.DEFAULT_POLL_SECONDS)
        for game, delay in cases:
            with self.subTest(game=game):
                worker, _, _, _, _, _, _, _ = self.make_worker()
                worker.queue_aram_task = Mock()
                worker.prewarm_champions = Mock()
                worker.poll_game.return_value = game
                worker.enabled = False
                # A two-second poll finishes at 12; the cooldown must not be
                # measured from the iteration's starting timestamp of 10.
                with patch.object(worker.stop, "wait", side_effect=[False, True]), \
                        patch("mayhem.worker.time", SimpleNamespace(monotonic=Mock(side_effect=[10, 12]))):
                    worker.run()
                self.assertAlmostEqual(worker.next_poll, 12 + delay)


if __name__ == "__main__":
    unittest.main()
