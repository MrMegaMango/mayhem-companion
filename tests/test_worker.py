import queue
import unittest
from unittest.mock import Mock, patch

from mayhem.worker import Worker


class SpellWorkerTests(unittest.TestCase):
    def setUp(self):
        patcher = patch("mayhem.worker.ClientReader")
        self.client = patcher.start().return_value
        self.client.poll.return_value = {"status": "waiting"}
        self.addCleanup(patcher.stop)

    def run_request(self, provider):
        events, requests, stop = queue.Queue(), queue.Queue(), Mock()
        stop.wait.side_effect = [False, True]
        requests.put(("spells", (9, "157")))
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
        self.assertIn(("game", {"status": "waiting"}), result)

    def test_focus_loss_retains_last_offers_and_marks_scan_paused(self):
        events, requests, stop = queue.Queue(), queue.Queue(), Mock()
        stop.wait.side_effect = [False, True]
        provider = Mock()
        provider.load_catalog.return_value = {"augments": {}}
        with patch("mayhem.worker.StatsProvider", return_value=provider), \
                patch("mayhem.worker.poll_active_player", return_value={"status": "live"}), \
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
                        patch("mayhem.worker.poll_active_player", return_value={"status": "live"}), \
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

    def test_live_detection_survives_unavailable_client(self):
        self.client.poll.return_value = {"status": "error"}
        with patch("mayhem.worker.StatsProvider"), \
                patch("mayhem.worker.poll_active_player", return_value={"status": "live"}) as live:
            worker = Worker(queue.Queue(), queue.Queue(), Mock(), ".")
            self.assertEqual(worker.poll_game(), {"status": "live"})
        live.assert_called_once_with(timeout=0.5)


if __name__ == "__main__":
    unittest.main()
