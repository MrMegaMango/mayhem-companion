import queue
import unittest
from unittest.mock import Mock, patch

from mayhem.worker import Worker


class SpellWorkerTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
