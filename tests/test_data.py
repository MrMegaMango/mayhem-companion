"""Provider behavior is exercised with synthetic responses, never live requests."""

import copy
from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from mayhem.data import (
    DATA_BASE, MANIFEST_TTL, MANIFEST_URL, REQUEST_TIMEOUT,
    DataUnavailableError, StatsProvider, resolve_champion_id,
)


VERSION = {"version": "16.21", "dataPath": "data/16.21-test", "resourcePath": "resources/16.21-test", "dataDate": "2026-10-24"}
NEXT_VERSION = {"version": "16.22", "dataPath": "data/16.22-test", "resourcePath": "resources/16.22-test", "dataDate": "2026-11-01"}
CHAMPIONS = {
    "157": {"name": "Yasuo", "riotId": "Yasuo", "slug": "yasuo"},
    "62": {"name": "Wukong", "riotId": "MonkeyKing", "slug": "wukong"},
    "121": {"name": "Kha'Zix", "riotId": "Khazix", "slug": "kha-zix"},
}
AUGMENTS = {"3078": {"name": "The Boom and the Bass", "slug": "the-boom-and-the-bass", "rarity": "gold"}}
DETAIL = {
    "champion": {"id": 157, "stats": {"sampleCount": 1000, "winRate": 0.58}},
    "augments": {
        "all": [{"id": 3078, "sampleCount": 400, "winRate": 0.67, "augmentWinRate": 0.54, "tier": "S", "rank": 1}],
        "stages": {"1": [{"id": 3078, "sampleCount": 90, "winRate": 0.69, "augmentWinRate": 0.54}], "2": []},
    },
}


def manifest(version=VERSION):
    return {"latest": version["version"], "versions": [version]}


def resource_url(kind, version=VERSION):
    return f"{DATA_BASE}/{version['resourcePath']}/en-US/resources/{kind}.json"


def detail_url(version=VERSION, bracket="all"):
    return f"{DATA_BASE}/{version['dataPath']}/stats/{bracket}/champion-details/157.json"


class StatsProviderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = self.enterContext(patch("mayhem.data.time.time", return_value=1000000.0))
        self.responses = {
            MANIFEST_URL: manifest(),
            resource_url("champions"): CHAMPIONS,
            resource_url("augments"): AUGMENTS,
            detail_url(): DETAIL,
            detail_url(bracket="high"): DETAIL,
        }
        self.requests = []
        self.network = self.enterContext(patch("mayhem.data.urlopen", side_effect=self.fetch))
        self.provider = StatsProvider(Path(self.tmp.name))

    def fetch(self, request, timeout):
        self.assertEqual(timeout, REQUEST_TIMEOUT)
        url = request.full_url
        self.requests.append(url)
        response = self.responses.get(url)
        if response is None:
            raise AssertionError(f"Unexpected request: {url}")
        if isinstance(response, BaseException):
            raise response
        return BytesIO(json.dumps(response).encode())

    def next_version(self):
        self.clock.return_value += MANIFEST_TTL + 1
        self.responses[MANIFEST_URL] = manifest(NEXT_VERSION)
        self.responses[resource_url("champions", NEXT_VERSION)] = CHAMPIONS
        self.responses[resource_url("augments", NEXT_VERSION)] = AUGMENTS

    def test_catalog_is_small_and_champion_downloads_are_on_demand(self):
        catalog = self.provider.load_catalog()
        self.assertEqual(catalog["version"], "16.21")
        self.assertEqual(catalog["source"], "ARAMKit")
        self.assertFalse(catalog["stale"])
        self.assertEqual(len(self.requests), 3)
        rate = self.provider.rate_for(157, 3078)
        self.assertEqual(rate["win_rate"], 0.67)
        self.assertEqual(rate["sample_count"], 400)
        self.assertEqual(rate["global_win_rate"], 0.54)
        self.assertEqual(rate["scope"], "champion")
        self.assertEqual(rate["source_url"], "https://aramkit.com/en-US/champions/yasuo")
        self.assertEqual(self.requests[-1], detail_url())
        self.assertEqual(len(self.requests), 4)

    def test_stage_rate_does_not_fall_back_to_overall_or_global(self):
        rate = self.provider.rate_for("157", "3078", stage=1)
        self.assertEqual(rate["win_rate"], 0.69)
        self.assertEqual(rate["sample_count"], 90)
        self.assertTrue(rate["low_sample"])
        missing = self.provider.rate_for(157, 3078, stage=2)
        self.assertIsNone(missing["win_rate"])
        self.assertIsNone(missing["global_win_rate"])
        self.assertTrue(missing["missing"])
        self.assertEqual(missing["sample_count"], 0)

    def test_unknown_augment_has_no_invented_statistic(self):
        rate = self.provider.rate_for(157, 9999)
        self.assertIsNone(rate["win_rate"])
        self.assertEqual(rate["scope"], "champion")

    def test_zero_games_never_displays_a_rate(self):
        data = copy.deepcopy(DETAIL)
        data["augments"]["all"][0]["sampleCount"] = 0
        self.responses[detail_url()] = data
        rate = self.provider.rate_for(157, 3078)
        self.assertIsNone(rate["win_rate"])
        self.assertTrue(rate["missing"])

    def test_cached_catalog_and_champion_need_no_additional_requests(self):
        self.provider.rate_for(157, 3078)
        calls = len(self.requests)
        second = StatsProvider(Path(self.tmp.name))
        self.assertEqual(second.rate_for(157, 3078)["win_rate"], 0.67)
        self.assertEqual(len(self.requests), calls)

    def test_expired_manifest_transient_failure_labels_cached_stats_stale(self):
        self.provider.rate_for(157, 3078)
        self.clock.return_value += MANIFEST_TTL + 1
        self.responses[MANIFEST_URL] = URLError("offline")
        rate = self.provider.rate_for(157, 3078)
        self.assertTrue(rate["stale"])
        self.assertEqual(rate["data_date"], "2026-10-24")
        attempts = len(self.requests)
        self.provider.rate_for(157, 3078)
        self.assertEqual(len(self.requests), attempts)

    def test_transient_new_champion_snapshot_failure_retains_old_metadata(self):
        self.provider.rate_for(157, 3078)
        self.next_version()
        self.responses[detail_url(NEXT_VERSION)] = TimeoutError("timeout")
        rate = self.provider.rate_for(157, 3078)
        self.assertTrue(rate["stale"])
        self.assertEqual(rate["version"], "16.21")
        self.assertEqual(rate["data_date"], "2026-10-24")
        attempts = len(self.requests)
        self.provider.rate_for(157, 3078, stage=1)
        self.assertEqual(len(self.requests), attempts)

    def test_new_instance_honors_original_manifest_expiry(self):
        self.provider.load_catalog()
        self.clock.return_value += MANIFEST_TTL - 10
        second = StatsProvider(Path(self.tmp.name))
        second.load_catalog()
        self.clock.return_value += 11
        self.responses[MANIFEST_URL] = URLError("offline")
        self.assertTrue(second.load_catalog()["stale"])

    def test_transient_new_catalog_failure_falls_back_as_a_coherent_snapshot(self):
        self.provider.rate_for(157, 3078)
        self.next_version()
        self.responses[resource_url("champions", NEXT_VERSION)] = URLError("offline")
        catalog = self.provider.load_catalog()
        self.assertTrue(catalog["stale"])
        self.assertEqual(catalog["version"], "16.21")
        self.assertTrue(self.provider.rate_for(157, 3078)["stale"])

    def test_http_not_found_does_not_silently_reuse_old_stats(self):
        self.provider.rate_for(157, 3078)
        self.next_version()
        url = detail_url(NEXT_VERSION)
        self.responses[url] = HTTPError(url, 404, "Not found", {}, None)
        with self.assertRaises(DataUnavailableError) as raised:
            self.provider.rate_for(157, 3078)
        self.assertFalse(raised.exception.transient)

    def test_no_cached_data_reports_network_unavailability(self):
        self.responses[MANIFEST_URL] = URLError("offline")
        with self.assertRaises(DataUnavailableError) as raised:
            self.provider.load_catalog()
        self.assertTrue(raised.exception.transient)

    def test_bad_rates_and_counts_are_rejected(self):
        for key, invalid in [
            ("winRate", -0.1), ("winRate", 1.1), ("winRate", True),
            ("winRate", "0.5"), ("winRate", float("nan")),
            ("sampleCount", -1), ("sampleCount", 1.5), ("sampleCount", True),
            ("augmentWinRate", 50),
        ]:
            with self.subTest(key=key, value=invalid):
                data = copy.deepcopy(DETAIL)
                data["augments"]["all"][0][key] = invalid
                self.responses[detail_url()] = data
                with self.assertRaises(DataUnavailableError):
                    self.provider.rate_for(157, 3078)

    def test_mismatched_champion_and_duplicate_rows_are_rejected(self):
        for mode in ("wrong_champion", "duplicate"):
            with self.subTest(mode=mode):
                data = copy.deepcopy(DETAIL)
                if mode == "wrong_champion":
                    data["champion"]["id"] = 62
                else:
                    data["augments"]["all"] *= 2
                self.responses[detail_url()] = data
                with self.assertRaises(DataUnavailableError):
                    self.provider.rate_for(157, 3078)

    def test_manifest_cannot_redirect_data_to_arbitrary_paths(self):
        bad = copy.deepcopy(VERSION)
        bad["dataPath"] = "https://other.example/stats"
        self.responses[MANIFEST_URL] = manifest(bad)
        with self.assertRaises(DataUnavailableError):
            self.provider.load_catalog()
        self.assertEqual(self.requests, [MANIFEST_URL])

    def test_invalid_arguments_are_rejected_before_network_access(self):
        for value in ("../157", "0", -1, True, "https://other.example"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.provider.champion_detail(value)
        with self.assertRaises(ValueError):
            self.provider.champion_detail(157, "../other")
        for stage in (0, 5, True, "1", 1.5):
            with self.subTest(stage=stage), self.assertRaises(ValueError):
                self.provider.rate_for(157, 3078, stage)
        self.assertEqual(self.requests, [])

    def test_names_ids_and_live_client_names_resolve(self):
        for name, expected in [
            ("Yasuo", "157"), (157, "157"), (" yasuo ", "157"),
            ("game_character_displayname_Yasuo", "157"),
            ("MonkeyKing", "62"), ("Wukong", "62"), ("Kha'Zix", "121"),
            ("game_character_displayname_Khazix", "121"), ("unknown", None),
            (None, None), (True, None),
        ]:
            with self.subTest(name=name):
                self.assertEqual(resolve_champion_id(name, CHAMPIONS), expected)
        self.assertEqual(self.provider.resolve_champion_id("Yasuo"), "157")

    def test_brackets_use_distinct_cached_urls(self):
        self.provider.rate_for(157, 3078)
        self.provider.rate_for(157, 3078, bracket="high")
        self.assertIn(detail_url(bracket="all"), self.requests)
        self.assertIn(detail_url(bracket="high"), self.requests)

    def test_mutating_returned_data_cannot_damage_cached_results(self):
        catalog = self.provider.load_catalog()
        catalog["champions"]["157"]["name"] = "Wrong"
        detail = self.provider.champion_detail(157)
        detail["augments"]["all"][0]["winRate"] = 1
        self.assertEqual(self.provider.resolve_champion_id("Yasuo"), "157")
        self.assertEqual(self.provider.rate_for(157, 3078)["win_rate"], 0.67)


if __name__ == "__main__":
    unittest.main()
