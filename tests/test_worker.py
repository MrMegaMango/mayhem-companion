import queue
import threading
import unittest
from unittest.mock import Mock, patch

from mayhem.worker import Worker


class SpellWorkerTests(unittest.TestCase):
    def setUp(self):
        patcher = patch("mayhem.worker.ClientReader")
        self.client = patcher.start().return_value
        self.client.poll.return_value = {"status": "waiting"}
        self.addCleanup(patcher.stop)

    def run_request(self, provider, task="spells"):
        events, requests, stop = queue.Queue(), queue.Queue(), Mock()
        stop.wait.side_effect = [False, True]
        requests.put((task, (9, "157")))
        with patch("mayhem.worker.StatsProvider", return_value=provider), \
                patch("mayhem.worker.poll_active_player", return_value={"status": "waiting"}):
            worker = Worker(events, requests, stop, ".")
            worker.run()
        return list(events.queue)

    def test_spell_lookup_preserves_token_for_stale_result_detection(self):
        provider = Mock()
        provider.summoner_spells_for.return_value = {"champion_id": "157", "rows": []}
        result = self.run_request(provider)
        provider.summoner_spells_for.assert_called_once_with("157")
        self.assertIn(("spells", (9, {"champion_id": "157", "rows": []})), result)

    def test_spell_fetch_failure_is_scoped_to_spell_request(self):
        provider = Mock()
        provider.summoner_spells_for.side_effect = ValueError("Unavailable")
        result = self.run_request(provider)
        self.assertIn(("spells_error", (9, "Unavailable")), result)
        self.assertIn(("game", {"status": "waiting", "phase": None, "is_aram": None}), result)

    def test_build_lookup_preserves_token_for_stale_result_detection(self):
        provider = Mock()
        data = {"champion_id": "157", "rows": [{"items": [3031, 3046], "win_rate": 54.2}]}
        provider.item_builds_for.return_value = data
        events = self.run_request(provider, "builds")
        provider.item_builds_for.assert_called_once_with("157")
        self.assertIn(("builds", (9, data)), events)

    def test_build_failure_is_scoped_to_its_request(self):
        provider = Mock()
        provider.item_builds_for.side_effect = ValueError("Build data unavailable")
        events = self.run_request(provider, "builds")
        self.assertIn(("builds_error", (9, "Build data unavailable")), events)
        self.assertIn(("game", {"status": "waiting", "phase": None, "is_aram": None}), events)

    def test_focus_loss_retains_last_offers_and_marks_scan_paused(self):
        events, requests, stop = queue.Queue(), queue.Queue(), Mock()
        stop.wait.side_effect = [False, True]
        provider = Mock()
        provider.load_catalog.return_value = {"augments": {}}
        with patch("mayhem.worker.StatsProvider", return_value=provider), \
                patch("mayhem.worker.poll_active_player", return_value={"status": "live", "game_mode": "ARAM"}), \
                patch("mayhem.worker.CaptureReader") as capture:
            capture.return_value.scan.return_value = {"status": "waiting", "message": "Focus League"}
            worker = Worker(events, requests, stop, ".")
            worker.last_offers = ("1", "2", "3")
            worker.last_seen = 0
            worker.run()
        self.assertEqual(worker.last_offers, ("1", "2", "3"))
        self.assertNotIn(("offers", []), list(events.queue))
        self.assertTrue(any(kind == "scan_status" and "Showing last recognized" in value
                            for kind, value in events.queue))

    def test_failed_capture_keeps_last_choices_and_discloses_failure(self):
        for status in ("unreadable", "error"):
            with self.subTest(status=status):
                events, requests, stop = queue.Queue(), queue.Queue(), Mock()
                stop.wait.side_effect = [False, True]
                provider = Mock()
                provider.load_catalog.return_value = {"augments": {}}
                with patch("mayhem.worker.StatsProvider", return_value=provider), \
                        patch("mayhem.worker.poll_active_player", return_value={"status": "live", "game_mode": "ARAM"}), \
                        patch("mayhem.worker.CaptureReader") as capture:
                    capture.return_value.scan.return_value = {"status": status, "message": "Capture unavailable."}
                    worker = Worker(events, requests, stop, ".")
                    worker.last_offers = ("1", "2", "3")
                    worker.last_seen = 0
                    worker.run()
                self.assertEqual(worker.last_offers, ("1", "2", "3"))
                self.assertNotIn(("offers", []), list(events.queue))
                self.assertIn(("scan_status", "Capture unavailable. Showing last recognized choices."), list(events.queue))

    def test_champion_select_emits_context_without_live_api_or_ocr(self):
        selection = {"status": "champ_select", "champion_id": "157",
                     "summoner_spells": [{"id": "4"}, {"id": "32"}]}
        self.client.poll.return_value = selection
        events, requests, stop = queue.Queue(), queue.Queue(), Mock()
        stop.wait.side_effect = [False, True]
        with patch("mayhem.worker.StatsProvider"), \
                patch("mayhem.worker.poll_active_player") as live, \
                patch("mayhem.worker.CaptureReader") as capture:
            worker = Worker(events, requests, stop, ".")
            worker.run()
        self.assertIn(("game", selection), list(events.queue))
        live.assert_not_called()
        capture.return_value.scan.assert_not_called()

    def test_champion_select_without_pick_does_not_fall_back_to_old_live_champion(self):
        self.client.poll.return_value = {"status": "champ_select", "champion_id": None}
        with patch("mayhem.worker.StatsProvider"), patch("mayhem.worker.poll_active_player") as live:
            worker = Worker(queue.Queue(), queue.Queue(), Mock(), ".")
            self.assertIsNone(worker.poll_game()["champion_id"])
        live.assert_not_called()

    def test_tft_uses_lcu_only_and_clears_aram_offers_without_running_ocr(self):
        selection = {"status": "tft", "is_tft": True, "is_aram": False, "phase": "InProgress"}
        self.client.poll.return_value = selection
        events, requests, stop = queue.Queue(), queue.Queue(), Mock()
        stop.wait.side_effect = [False, True]
        with patch("mayhem.worker.StatsProvider"), patch("mayhem.worker.poll_active_player") as live, \
                patch("mayhem.worker.CaptureReader") as capture, patch("mayhem.worker.TftProvider") as tft:
            worker = Worker(events, requests, stop, ".")
            worker.last_offers = ("1", "2", "3")
            worker.run()
        self.assertIn(("game", selection), list(events.queue))
        self.assertIn(("offers", []), list(events.queue))
        live.assert_not_called()
        capture.return_value.scan.assert_not_called()
        tft.assert_not_called()

    def test_tft_request_uses_separate_provider_and_returns_matching_token(self):
        events, requests, stop = queue.Queue(), queue.Queue(), Mock()
        stop.wait.side_effect = [False, True]
        requests.put(("tft", (12, True)))
        data = {"source": "TFT source", "comps": []}
        with patch("mayhem.worker.StatsProvider") as aram, patch("mayhem.worker.TftProvider") as tft, \
                patch("mayhem.worker.threading.Thread") as thread, \
                patch("mayhem.worker.poll_active_player", return_value={"status": "waiting"}):
            tft.return_value.load.return_value = data
            worker = Worker(events, requests, stop, ".")
            worker.run()
            tft.assert_not_called()  # TFT HTTP cannot block the polling/OCR thread.
            thread.return_value.start.assert_called_once()
            worker.perform_tft_task(*worker.tft_requests.get_nowait())
        tft.return_value.load.assert_called_once_with(force=True)
        self.assertIn(("tft", (12, data)), list(events.queue))
        aram.return_value.item_builds_for.assert_not_called()

    def test_explicit_live_tft_mode_fallback_never_exposes_little_legend_as_aram_champion(self):
        with patch("mayhem.worker.StatsProvider"), patch("mayhem.worker.poll_active_player",
                return_value={"status": "live", "game_mode": "TFT", "champion_name": "Little Legend"}):
            worker = Worker(queue.Queue(), queue.Queue(), Mock(), ".")
            result = worker.poll_game()
        self.assertEqual(result["status"], "tft")
        self.assertTrue(result["is_tft"])
        self.assertFalse(result["is_aram"])
        self.assertIsNone(result["champion_name"])

    def test_lingering_tft_api_cannot_be_merged_into_new_aram_context(self):
        selection = {"status": "waiting", "phase": "GameStart", "is_tft": False,
                     "is_aram": True, "game_mode": "KIWI"}
        self.client.poll.return_value = selection
        with patch("mayhem.worker.StatsProvider"), patch("mayhem.worker.poll_active_player",
                return_value={"status": "live", "game_mode": "TFT", "champion_name": "Little Legend"}):
            worker = Worker(queue.Queue(), queue.Queue(), Mock(), ".")
            self.assertEqual(worker.poll_game(), selection)

    def test_tft_error_is_scoped_and_polling_continues(self):
        events, requests, stop = queue.Queue(), queue.Queue(), Mock()
        stop.wait.side_effect = [False, True]
        requests.put(("tft", (12, False)))
        with patch("mayhem.worker.StatsProvider"), patch("mayhem.worker.TftProvider") as tft, \
                patch("mayhem.worker.threading.Thread"), \
                patch("mayhem.worker.poll_active_player", return_value={"status": "waiting"}):
            tft.return_value.load.side_effect = ValueError("No TFT data")
            worker = Worker(events, requests, stop, ".")
            worker.run()
            worker.perform_tft_task(*worker.tft_requests.get_nowait())
        self.assertIn(("tft_error", (12, "No TFT data")), list(events.queue))
        self.assertTrue(any(kind == "game" for kind, _ in events.queue))

    def test_tft_item_lookup_keeps_unit_token_and_errors_separate(self):
        events, requests = queue.Queue(), queue.Queue()
        with patch("mayhem.worker.StatsProvider"), patch("mayhem.worker.TftProvider") as tft:
            worker = Worker(events, requests, Mock(), ".")
            data = {"unit_id": "Ahri", "rows": []}
            tft.return_value.items_for.return_value = data
            worker.perform_tft_task("tft_items", (7, "Ahri"))
            tft.return_value.items_for.side_effect = ValueError("No unit sample")
            worker.perform_tft_task("tft_items", (8, "Akali"))
        self.assertIn(("tft_items", (7, "Ahri", data)), list(events.queue))
        self.assertIn(("tft_items_error", (8, "Akali", "No unit sample")), list(events.queue))

    def test_live_detection_survives_unavailable_client(self):
        self.client.poll.return_value = {"status": "error"}
        with patch("mayhem.worker.StatsProvider"), \
                patch("mayhem.worker.poll_active_player", return_value={"status": "live", "game_mode": "KIWI"}) as live:
            worker = Worker(queue.Queue(), queue.Queue(), Mock(), ".")
            self.assertEqual(worker.poll_game(), {"status": "live", "game_mode": "KIWI", "phase": None, "is_aram": True})
        live.assert_called_once_with(timeout=0.5)

    def test_lobby_and_end_phases_override_lingering_game_api(self):
        with patch("mayhem.worker.StatsProvider"), patch("mayhem.worker.poll_active_player") as live:
            worker = Worker(queue.Queue(), queue.Queue(), Mock(), ".")
            for phase in ("None", "Lobby", "Matchmaking", "ReadyCheck", "WaitingForStats", "EndOfGame"):
                self.client.poll.return_value = {"status": "waiting", "phase": phase, "is_aram": False}
                self.assertEqual(worker.poll_game()["phase"], phase)
            live.assert_not_called()

    def test_loading_keeps_aram_context_without_live_api_data(self):
        selection = {"status": "waiting", "phase": "GameStart", "is_aram": True, "game_mode": "KIWI"}
        self.client.poll.return_value = selection
        with patch("mayhem.worker.StatsProvider"), \
                patch("mayhem.worker.poll_active_player", return_value={"status": "waiting"}):
            worker = Worker(queue.Queue(), queue.Queue(), Mock(), ".")
            self.assertEqual(worker.poll_game(), selection)

    def test_client_mode_is_preserved_into_live_context(self):
        self.client.poll.return_value = {"status": "waiting", "phase": "InProgress", "is_aram": False}
        with patch("mayhem.worker.StatsProvider"), \
                patch("mayhem.worker.poll_active_player", return_value={"status": "live", "game_mode": "ARAM"}):
            worker = Worker(queue.Queue(), queue.Queue(), Mock(), ".")
            result = worker.poll_game()
        self.assertFalse(result["is_aram"])
        self.assertEqual(result["phase"], "InProgress")

    def test_other_modes_do_not_run_augment_ocr(self):
        events, requests, stop = queue.Queue(), queue.Queue(), Mock()
        stop.wait.side_effect = [False, True]
        with patch("mayhem.worker.StatsProvider"), \
                patch("mayhem.worker.poll_active_player", return_value={"status": "live", "game_mode": "CLASSIC"}), \
                patch("mayhem.worker.CaptureReader") as capture:
            worker = Worker(events, requests, stop, ".")
            worker.run()
        capture.return_value.scan.assert_not_called()
        self.assertFalse(worker.last_game["is_aram"])


class ChampionDataTests(unittest.TestCase):
    def test_champion_request_is_lazy_and_does_not_fetch_on_polling_thread(self):
        events, requests, stop = queue.Queue(), queue.Queue(), Mock()
        stop.wait.side_effect = [False, True]
        requests.put(("champions", (7, ["157", "63"])))
        with patch("mayhem.worker.StatsProvider") as provider, \
                patch("mayhem.worker.ClientReader") as client, \
                patch("mayhem.worker.threading.Thread") as thread:
            client.return_value.poll.return_value = {"status": "champ_select", "is_aram": True}
            worker = Worker(events, requests, stop, ".")
            worker.run()
            provider.assert_called_once()
            provider.return_value.champion_rates_for.assert_not_called()
            thread.return_value.start.assert_called_once()
            self.assertEqual(worker.champion_requests.get_nowait(), (7, ["157", "63"]))
            self.assertTrue(any(kind == "game" for kind, _ in events.queue))

    def test_slow_fetch_coalesces_pending_choices_on_its_own_thread(self):
        events, stop = queue.Queue(), threading.Event()
        entered, release = threading.Event(), threading.Event()
        separate_provider = Mock()
        calls = []

        def rates(ids):
            calls.append(ids)
            if len(calls) == 1:
                entered.set()
                if not release.wait(3):
                    raise RuntimeError("Test fetch was not released")
            return {"rows": [{"champion_id": ident} for ident in ids]}

        separate_provider.champion_rates_for.side_effect = rates
        with patch("mayhem.worker.StatsProvider", side_effect=[Mock(), separate_provider]), \
                patch("mayhem.worker.ClientReader"):
            worker = Worker(events, queue.Queue(), stop, ".")
            try:
                worker.queue_champion_task((1, ["157"]))
                self.assertTrue(entered.wait(2))
                worker.queue_champion_task((2, ["63"]))
                worker.queue_champion_task((3, ["99"]))
                self.assertIsNot(worker.champion_provider, worker.provider)
                release.set()
                self.assertEqual(events.get(timeout=2)[1][0], 1)
                self.assertEqual(events.get(timeout=2)[1][0], 3)
                self.assertEqual(calls, [["157"], ["99"]])
            finally:
                release.set()
                stop.set()
                worker.champion_thread.join(3)
                self.assertFalse(worker.champion_thread.is_alive())

    def test_champion_data_failure_preserves_request_token(self):
        with patch("mayhem.worker.StatsProvider") as provider, patch("mayhem.worker.ClientReader"):
            events = queue.Queue()
            worker = Worker(events, queue.Queue(), Mock(), ".")
            provider.return_value.champion_rates_for.side_effect = ValueError("Unavailable")
            worker.perform_champion_task((4, ["157"]))
        self.assertEqual(events.get_nowait(), ("champions_error", (4, "Unavailable")))


class OfferExpiryTests(unittest.TestCase):
    @staticmethod
    def ok(offers=()):
        return {"status": "ok", "lines": [{"id": ident} for ident in offers]}

    def run_scans(self, captures, times, *, rescan=False, initial_offers=("1", "2", "3")):
        self.assertEqual(len(captures), len(times))
        events, requests, stop = queue.Queue(), queue.Queue(), Mock()
        if rescan:
            requests.put(("scan", None))
        stop.wait.side_effect = [False] * len(times) + [True]
        provider = Mock()
        provider.load_catalog.return_value = {"augments": {}}
        with patch("mayhem.worker.StatsProvider", return_value=provider), \
                patch("mayhem.worker.ClientReader"), \
                patch("mayhem.worker.CaptureReader") as capture, \
                patch("mayhem.worker.detect_offers", side_effect=lambda lines, catalog: lines), \
                patch("mayhem.worker.time.monotonic", side_effect=[t for t in times for _ in range(2)]), \
                patch("mayhem.worker.logging.exception"):
            capture.return_value.scan.side_effect = captures
            worker = Worker(events, requests, stop, ".")
            worker.next_poll = float("inf")
            worker.last_game = {"status": "live", "is_aram": True}
            worker.last_offers = initial_offers
            worker.last_seen = 0
            worker.run()
            self.assertEqual(capture.return_value.scan.call_count, len(captures))
        return worker, list(events.queue)

    def test_slow_capture_leaves_full_cooldown_before_next_scan(self):
        cases = (
            (self.ok(("1", "2", "3")), Worker.ACTIVE_SCAN_SECONDS),
            (self.ok(), Worker.IDLE_SCAN_SECONDS),
            ({"status": "waiting"}, Worker.IDLE_SCAN_SECONDS),
            ({"status": "unreadable"}, Worker.IDLE_SCAN_SECONDS),
            ({"status": "error"}, 10),
            (OSError("Capture unavailable"), 10),
        )
        for result, cooldown in cases:
            with self.subTest(result=result):
                clock, starts = [10.0], []
                duration = 1.4

                def wait(seconds):
                    clock[0] += seconds
                    return len(starts) >= 2

                def scan():
                    starts.append(clock[0])
                    clock[0] += duration
                    if isinstance(result, Exception):
                        raise result
                    return result

                provider, stop = Mock(), Mock()
                provider.load_catalog.return_value = {"augments": {}}
                stop.wait.side_effect = wait
                with patch("mayhem.worker.StatsProvider", return_value=provider), \
                        patch("mayhem.worker.ClientReader"), \
                        patch("mayhem.worker.CaptureReader") as capture, \
                        patch("mayhem.worker.detect_offers", side_effect=lambda lines, catalog: lines), \
                        patch("mayhem.worker.time.monotonic", side_effect=lambda: clock[0]), \
                        patch("mayhem.worker.logging.exception"):
                    capture.return_value.scan.side_effect = scan
                    worker = Worker(queue.Queue(), queue.Queue(), stop, ".")
                    worker.next_poll = float("inf")
                    worker.last_game = {"status": "live", "is_aram": True}
                    worker.run()
                gap = starts[1] - (starts[0] + duration)
                self.assertGreaterEqual(gap + 1e-9, cooldown)
                self.assertLess(gap, cooldown + 0.051)

    def test_empty_scans_clear_once_after_debounce_and_return_to_idle_cadence(self):
        worker, events = self.run_scans([self.ok()] * 5, [10, 10.4, 10.8, 11.21, 12.1])
        self.assertEqual(worker.last_offers, ())
        self.assertEqual(events.count(("offers", [])), 1)
        self.assertAlmostEqual(worker.next_scan, 12.1 + Worker.IDLE_SCAN_SECONDS)

    def test_successful_empty_scans_before_threshold_keep_choices_and_fast_cadence(self):
        worker, events = self.run_scans([self.ok()] * 3, [10, 10.4, 10.8])
        self.assertEqual(worker.last_offers, ("1", "2", "3"))
        self.assertNotIn(("offers", []), events)
        self.assertAlmostEqual(worker.next_scan, 10.8 + Worker.ACTIVE_SCAN_SECONDS)

    def test_one_empty_frame_never_clears_even_long_after_last_recognition(self):
        worker, events = self.run_scans([self.ok()], [100])
        self.assertEqual(worker.empty_frames, 1)
        self.assertEqual(worker.last_offers, ("1", "2", "3"))
        self.assertNotIn(("offers", []), events)

    def test_titles_during_reroll_restart_absence_debounce(self):
        captures = [self.ok(), self.ok(), self.ok(("1", "2", "3")), self.ok(), self.ok()]
        worker, events = self.run_scans(captures, [10, 10.4, 10.8, 11.2, 11.6])
        self.assertEqual(worker.last_offers, ("1", "2", "3"))
        self.assertEqual(worker.empty_frames, 2)
        self.assertEqual(worker.empty_since, 11.2)
        self.assertNotIn(("offers", []), events)

    def test_pause_unreadable_and_capture_error_reset_absence_without_clearing(self):
        for status in ("waiting", "unreadable", "error"):
            with self.subTest(status=status):
                captures = [self.ok(), self.ok(), {"status": status}, self.ok(), self.ok()]
                worker, events = self.run_scans(captures, [10, 10.4, 10.8, 21, 21.4])
                self.assertEqual(worker.last_offers, ("1", "2", "3"))
                self.assertEqual(worker.empty_since, 21)
                self.assertNotIn(("offers", []), events)

    def test_ocr_exception_resets_absence_and_preserves_choices(self):
        captures = [self.ok(), self.ok(), OSError("Capture unavailable"), self.ok(), self.ok()]
        worker, events = self.run_scans(captures, [10, 10.4, 10.8, 21, 21.4])
        self.assertEqual(worker.last_offers, ("1", "2", "3"))
        self.assertEqual(worker.empty_since, 21)
        self.assertNotIn(("offers", []), events)

    def test_rerolled_titles_still_require_two_reads_without_empty_handoff(self):
        captures = [self.ok(), self.ok(("4", "5", "6")), self.ok(("4", "5", "6"))]
        worker, events = self.run_scans(captures, [10, 10.4, 10.8])
        self.assertEqual(worker.last_offers, ("4", "5", "6"))
        self.assertIn(("offers", [{"id": "4"}, {"id": "5"}, {"id": "6"}]), events)
        self.assertNotIn(("offers", []), events)

    def test_rescan_preserves_visible_choices_until_same_ids_are_confirmed(self):
        offers = ("1", "2", "3")
        worker, events = self.run_scans([self.ok(offers)], [10], rescan=True)
        self.assertEqual(worker.last_offers, offers)
        self.assertTrue(worker.force_offer_emit)
        self.assertFalse(any(kind == "offers" for kind, _ in events))
        worker, events = self.run_scans([self.ok(offers)] * 3, [10, 10.4, 10.8], rescan=True)
        self.assertEqual([value for kind, value in events if kind == "offers"],
                         [[{"id": "1"}, {"id": "2"}, {"id": "3"}]])
        self.assertFalse(worker.force_offer_emit)

    def test_rescan_only_hands_off_to_empty_after_confirmed_disappearance(self):
        worker, events = self.run_scans([self.ok()] * 3, [10, 10.4, 10.8], rescan=True)
        self.assertEqual(worker.last_offers, ("1", "2", "3"))
        self.assertNotIn(("offers", []), events)
        worker, events = self.run_scans([self.ok()] * 5, [10, 10.4, 10.8, 11.21, 12.1], rescan=True)
        self.assertEqual(events.count(("offers", [])), 1)
        self.assertEqual(worker.last_offers, ())
        self.assertFalse(worker.force_offer_emit)

    def test_rescan_after_manual_browsing_can_confirm_absence_without_prior_auto_choices(self):
        worker, events = self.run_scans([self.ok()] * 4, [10, 10.4, 10.8, 11.21],
                                       rescan=True, initial_offers=())
        self.assertEqual(events.count(("offers", [])), 1)
        self.assertFalse(worker.force_offer_emit)

    def test_rescan_focus_pause_and_failures_do_not_create_empty_handoff(self):
        for status in ("waiting", "unreadable", "error"):
            with self.subTest(status=status):
                captures = [{"status": status}, self.ok(("1", "2", "3")), self.ok(("1", "2", "3"))]
                worker, events = self.run_scans(captures, [10, 21, 21.4], rescan=True)
                self.assertNotIn(("offers", []), events)
                self.assertEqual([value for kind, value in events if kind == "offers"],
                                 [[{"id": "1"}, {"id": "2"}, {"id": "3"}]])
                self.assertFalse(worker.force_offer_emit)


if __name__ == "__main__":
    unittest.main()
