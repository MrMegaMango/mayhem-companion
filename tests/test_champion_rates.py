"""Aggregate champion selection statistics, using synthetic public responses."""

import copy
from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from mayhem.data import (
    DATA_BASE, MANIFEST_TTL, MANIFEST_URL, REQUEST_TIMEOUT, RETRY_DELAY,
    DataUnavailableError, StatsProvider,
)


VERSION = {"version": "16.21", "dataPath": "data/16.21-test",
           "resourcePath": "resources/16.21-test", "dataDate": "2026-10-25"}
NEXT_VERSION = {"version": "16.22", "dataPath": "data/16.22-test",
                "resourcePath": "resources/16.22-test", "dataDate": "2026-11-01"}
CHAMPIONS = {
    "157": {"name": "Yasuo", "icon": "assets/champion/157-617df344cd83.webp"},
    "62": {"name": "Wukong", "icon": f"{DATA_BASE}/assets/champion/62-0123456789ab.webp"},
    "121": {"name": "Kha'Zix"},
}
RANKINGS = {"rows": [
    {"id": 157, "sampleCount": 1725959, "winRate": 0.5846},
    {"id": 62, "sampleCount": 199, "winRate": 0.5119},
]}


def rankings_url(version=VERSION, bracket="all"):
    return f"{DATA_BASE}/{version['dataPath']}/stats/{bracket}/champion-rankings.json"


def resources_url(version=VERSION):
    return f"{DATA_BASE}/{version['resourcePath']}/en-US/resources/champions.json"


class ChampionRatesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = self.enterContext(patch("mayhem.data.time.time", return_value=1000000.0))
        self.responses = {
            MANIFEST_URL: {"latest": VERSION["version"], "versions": [VERSION]},
            rankings_url(): copy.deepcopy(RANKINGS),
            resources_url(): copy.deepcopy(CHAMPIONS),
        }
        self.requests = []
        self.enterContext(patch("mayhem.data.urlopen", side_effect=self.fetch))
        self.provider = StatsProvider(Path(self.tmp.name))

    def fetch(self, request, timeout):
        self.assertEqual(timeout, REQUEST_TIMEOUT)
        url = request.full_url
        self.requests.append(url)
        if url not in self.responses:
            raise AssertionError(f"Unexpected request: {url}")
        response = self.responses[url]
        if isinstance(response, BaseException):
            raise response
        return BytesIO(json.dumps(response).encode())

    def next_version(self):
        self.clock.return_value += MANIFEST_TTL + 1
        self.responses[MANIFEST_URL] = {"latest": NEXT_VERSION["version"], "versions": [NEXT_VERSION]}
        self.responses[resources_url(NEXT_VERSION)] = copy.deepcopy(CHAMPIONS)
        self.responses[rankings_url(NEXT_VERSION)] = copy.deepcopy(RANKINGS)

    def test_single_aggregate_preserves_requested_order_and_source_semantics(self):
        result = self.provider.champion_rates_for([62, "157", 62])
        self.assertEqual(self.requests, [MANIFEST_URL, rankings_url(), resources_url()])
        self.assertEqual([row["champion_id"] for row in result["rows"]], ["62", "157"])
        self.assertEqual(result["rows"][1], {
            "champion_id": "157", "name": "Yasuo",
            "icon_url": f"{DATA_BASE}/assets/champion/157-617df344cd83.webp",
            "win_rate": 0.5846, "sample_count": 1725959, "missing": False, "low_sample": False,
        })
        self.assertTrue(result["rows"][0]["low_sample"])
        self.assertEqual(result["source"], "ARAMKit")
        self.assertEqual(result["source_url"], "https://aramkit.com/en-US/champions")
        self.assertEqual((result["scope"], result["game_mode"], result["bracket"]),
                         ("champion", "ARAM Mayhem", "all"))
        self.assertEqual(result["sample_scope"], "champion_appearances")
        self.assertEqual((result["version"], result["data_date"], result["stale"]),
                         ("16.21", "2026-10-25", False))

    def test_changed_selection_uses_memory_and_new_instance_uses_disk_without_requests(self):
        first = self.provider.champion_rates_for([157])
        with patch.object(self.provider, "_read_json", side_effect=AssertionError("Unexpected cache read")):
            self.assertEqual(self.provider.champion_rates_for([62])["rows"][0]["name"], "Wukong")
            first["rows"][0]["name"] = "Mutated"
            self.assertEqual(self.provider.champion_rates_for([157])["rows"][0]["name"], "Yasuo")
        second = StatsProvider(Path(self.tmp.name)).champion_rates_for([121, 157])
        self.assertEqual(second["rows"][1]["win_rate"], 0.5846)
        self.assertEqual(len(self.requests), 3)
        self.assertFalse(any("champion-details" in url or "augments.json" in url for url in self.requests))

    def test_missing_known_and_unknown_champions_do_not_invent_rates(self):
        result = self.provider.champion_rates_for([121, 999999])
        self.assertEqual([row["name"] for row in result["rows"]], ["Kha'Zix", "Champion 999999"])
        for row in result["rows"]:
            self.assertIsNone(row["win_rate"])
            self.assertIsNone(row["icon_url"])
            self.assertTrue(row["missing"])
            self.assertEqual(row["sample_count"], 0)
        self.assertEqual(self.provider.champion_rates_for([])["rows"], [])

    def test_zero_samples_explicit_no_data_and_absent_rates_are_missing(self):
        for row in ({"id": 157, "sampleCount": 0, "winRate": 0.8},
                    {"id": 157, "sampleCount": 100, "winRate": 0.8, "hasData": False},
                    {"id": 157, "hasData": False}, {"id": 157, "sampleCount": 100}):
            with self.subTest(row=row), tempfile.TemporaryDirectory() as directory:
                self.responses[rankings_url()] = {"rows": [row]}
                result = StatsProvider(Path(directory)).champion_rates_for([157])["rows"][0]
                self.assertTrue(result["missing"])
                self.assertIsNone(result["win_rate"])

    def test_zero_and_one_rates_are_valid_observations_and_200_samples_is_not_low(self):
        self.responses[rankings_url()] = {"rows": [
            {"id": 157, "sampleCount": 200, "winRate": 0},
            {"id": 62, "sampleCount": 200, "winRate": 1},
        ]}
        rows = self.provider.champion_rates_for([157, 62])["rows"]
        self.assertEqual([row["win_rate"] for row in rows], [0.0, 1.0])
        self.assertFalse(any(row["missing"] or row["low_sample"] for row in rows))

    def test_high_bracket_has_its_own_snapshot_link_and_never_falls_back_to_all(self):
        self.provider.champion_rates_for([157])
        self.responses[rankings_url(bracket="high")] = {"rows": [
            {"id": 157, "sampleCount": 2000, "winRate": 0.61},
        ]}
        high = self.provider.champion_rates_for([157, 62], "high")
        self.assertEqual(high["bracket"], "high")
        self.assertEqual(high["source_url"], "https://aramkit.com/en-US/champions?dataset=high")
        self.assertEqual(high["rows"][0]["win_rate"], 0.61)
        self.assertTrue(high["rows"][1]["missing"])
        self.assertEqual(len(self.requests), 4)
        with tempfile.TemporaryDirectory() as directory:
            other = StatsProvider(Path(directory))
            other.champion_rates_for([157])
            self.responses[rankings_url(bracket="high")] = URLError("offline")
            with self.assertRaises(DataUnavailableError):
                other.champion_rates_for([157], "high")

    def test_invalid_input_is_rejected_before_network_access(self):
        for ids, bracket in ((157, "all"), ("157", "all"), (None, "all"),
                             ([True], "all"), ([0], "all"), (["../157"], "all"),
                             (["0157"], "all"), ([157.0], "all"), ([157], "../all")):
            with self.subTest(ids=ids, bracket=bracket), self.assertRaises(ValueError):
                self.provider.champion_rates_for(ids, bracket)
        self.assertEqual(self.requests, [])

    def test_bad_rows_counts_rates_ids_and_duplicates_are_rejected(self):
        valid = RANKINGS["rows"][0]
        invalid = [None, [], {}, {"rows": {}}, {"rows": [None]}, {"rows": [valid, valid]}]
        for key, value in (("id", True), ("id", 0), ("id", "../157"),
                           ("sampleCount", None), ("sampleCount", True), ("sampleCount", -1),
                           ("sampleCount", 2.5), ("winRate", True), ("winRate", -0.1),
                           ("winRate", 1.01), ("winRate", "0.6"), ("winRate", float("nan")),
                           ("winRate", float("inf")), ("hasData", "false")):
            invalid.append({"rows": [{**valid, key: value}]})
        for data in invalid:
            with self.subTest(data=data), tempfile.TemporaryDirectory() as directory:
                self.responses[rankings_url()] = data
                with self.assertRaises(DataUnavailableError) as caught:
                    StatsProvider(Path(directory)).champion_rates_for([157])
                self.assertFalse(caught.exception.transient)

    def test_names_must_be_present_and_match_all_observed_champions(self):
        for resources in ({"157": {"name": "Yasuo"}},
                          {**CHAMPIONS, "62": {"name": " "}},
                          {**CHAMPIONS, "../1": {"name": "Invalid"}}):
            with self.subTest(resources=resources), tempfile.TemporaryDirectory() as directory:
                self.responses[resources_url()] = resources
                with self.assertRaises(DataUnavailableError):
                    StatsProvider(Path(directory)).champion_rates_for([157])

    def test_invalid_optional_portraits_are_ignored_without_losing_rates(self):
        for icon in (None, True, [], "http://data.aramkit.com/assets/champion/157-617df344cd83.webp",
                     "https://evil.example/assets/champion/157-617df344cd83.webp",
                     "https://data.aramkit.com.evil.example/assets/champion/157-617df344cd83.webp",
                     "https://data.aramkit.com:443/assets/champion/157-617df344cd83.webp",
                     "assets/champion/157-617df344cd83.webp?x=1", "assets/champion/../157-617df344cd83.webp",
                     "assets/champion/62-617df344cd83.webp", "assets/item/157-617df344cd83.webp",
                     "assets/champion/157.webp", "assets/champion/157-617df344cd83.png"):
            with self.subTest(icon=icon), tempfile.TemporaryDirectory() as directory:
                self.responses[resources_url()] = {**CHAMPIONS, "157": {"name": "Yasuo", "icon": icon}}
                row = StatsProvider(Path(directory)).champion_rates_for([157])["rows"][0]
                self.assertIsNone(row["icon_url"])
                self.assertEqual(row["win_rate"], 0.5846)

    def test_expired_manifest_offline_uses_cached_snapshot_and_marks_stale(self):
        first = self.provider.champion_rates_for([157])
        self.clock.return_value += MANIFEST_TTL + 1
        self.responses[MANIFEST_URL] = URLError("offline")
        result = StatsProvider(Path(self.tmp.name)).champion_rates_for([157])
        self.assertTrue(result["stale"])
        self.assertEqual(result["rows"], first["rows"])
        self.assertEqual(self.requests[3:], [MANIFEST_URL])

    def test_transient_new_rankings_or_names_failure_falls_back_whole_snapshot_and_recovers(self):
        for failed_kind in ("rankings", "names"):
            with self.subTest(failed_kind=failed_kind), tempfile.TemporaryDirectory() as directory:
                self.responses[MANIFEST_URL] = {"latest": VERSION["version"], "versions": [VERSION]}
                provider = StatsProvider(Path(directory))
                first = provider.champion_rates_for([157, 62])
                self.next_version()
                self.responses[rankings_url(NEXT_VERSION)]["rows"][0]["winRate"] = 0.7
                self.responses[resources_url(NEXT_VERSION)]["157"]["name"] = "New Yasuo"
                failed_url = rankings_url(NEXT_VERSION) if failed_kind == "rankings" else resources_url(NEXT_VERSION)
                recovered_response = self.responses[failed_url]
                self.responses[failed_url] = URLError("offline")
                fallback = provider.champion_rates_for([157, 62])
                self.assertTrue(fallback["stale"])
                self.assertEqual((fallback["version"], fallback["data_date"]), ("16.21", "2026-10-25"))
                self.assertEqual(fallback["rows"], first["rows"])
                attempts = len(self.requests)
                fallback["rows"][0]["name"] = "Mutated"
                self.assertEqual(provider.champion_rates_for([157, 62])["rows"], first["rows"])
                self.assertEqual(len(self.requests), attempts)
                self.clock.return_value += RETRY_DELAY + 1
                self.responses[failed_url] = recovered_response
                recovered = provider.champion_rates_for([157])
                self.assertFalse(recovered["stale"])
                self.assertEqual(recovered["version"], "16.22")
                self.assertEqual((recovered["rows"][0]["name"], recovered["rows"][0]["win_rate"]), ("New Yasuo", 0.7))

    def test_malformed_or_denied_new_snapshot_never_silently_uses_previous_rates(self):
        for failure in ("malformed", "denied", "names"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                self.responses[MANIFEST_URL] = {"latest": VERSION["version"], "versions": [VERSION]}
                provider = StatsProvider(Path(directory))
                provider.champion_rates_for([157])
                self.next_version()
                if failure == "names":
                    self.responses[resources_url(NEXT_VERSION)] = {"157": {"name": "Yasuo"}}
                else:
                    url = rankings_url(NEXT_VERSION)
                    self.responses[url] = {"rows": "invalid"} if failure == "malformed" else HTTPError(url, 404, "Not found", {}, None)
                with self.assertRaises(DataUnavailableError) as caught:
                    provider.champion_rates_for([157])
                self.assertFalse(caught.exception.transient)

    def test_empty_new_dataset_marks_missing_instead_of_reusing_old_rows(self):
        self.provider.champion_rates_for([157])
        self.next_version()
        self.responses[rankings_url(NEXT_VERSION)] = {"rows": []}
        result = self.provider.champion_rates_for([157])
        self.assertEqual(result["version"], "16.22")
        self.assertFalse(result["stale"])
        self.assertTrue(result["rows"][0]["missing"])

    def test_offline_without_snapshot_backs_off_across_selection_changes_then_recovers(self):
        self.responses[rankings_url()] = URLError("offline")
        with self.assertRaises(DataUnavailableError):
            self.provider.champion_rates_for([157])
        attempts = len(self.requests)
        with self.assertRaises(DataUnavailableError) as caught:
            self.provider.champion_rates_for([62])
        self.assertTrue(caught.exception.transient)
        self.assertEqual(len(self.requests), attempts)
        self.clock.return_value += RETRY_DELAY + 1
        self.responses[rankings_url()] = RANKINGS
        self.assertEqual(self.provider.champion_rates_for([157])["rows"][0]["win_rate"], 0.5846)


if __name__ == "__main__":
    unittest.main()
