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
    DATA_BASE, MANIFEST_TTL, MANIFEST_URL, REQUEST_TIMEOUT, RETRY_DELAY,
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
SUMMONER_SPELLS = {
    "4": {"name": "Flash"}, "32": {"name": "Mark"}, "6": {"name": "Ghost"},
}
ITEMS = {
    "0": {"name": "No Boots"}, "3031": {"name": "Infinity Edge", "icon": f"{DATA_BASE}/assets/item/3031-1dd45a28b705.webp"},
    "3087": {"name": "Statikk Shiv", "icon": "assets/item/3087-15854b8b753d.webp"}, "6673": {"name": "Immortal Shieldbow"},
    "3153": {"name": "Blade of the Ruined King"},
}
DETAIL = {
    "champion": {"id": 157, "stats": {"sampleCount": 1000, "winRate": 0.58}},
    "augments": {
        "all": [{"id": 3078, "sampleCount": 400, "winRate": 0.67, "augmentWinRate": 0.54, "tier": "S", "rank": 1}],
        "stages": {"1": [{"id": 3078, "sampleCount": 90, "winRate": 0.69, "augmentWinRate": 0.54}], "2": []},
    },
}
SUMMONER_DETAIL = {
    **DETAIL,
    "summoners": [
        {"rank": 1, "spells": [{"id": 4}, {"id": 32}], "sampleCount": 900, "winRate": 0.5864, "pickRate": 0.93},
        {"rank": 2, "spells": [{"id": 4}, {"id": 6}], "sampleCount": 90, "winRate": 0.567, "pickRate": 0.0576},
    ],
}


ITEM_DETAIL = {
    **DETAIL,
    "builds": {
        "filtered": {
            "sampleCount": 1000, "pickRate": 0.9,
            "archetypes": [{
                "key": "crit", "rank": 1, "sampleCount": 800, "winRate": 0.60, "pickRate": 0.8,
                "profiles": [{
                    "rank": 2, "itemSet": [{"id": 3087}, {"id": 3031}, {"id": 6673}],
                    "sampleCount": 600, "winRate": 0.62, "pickRate": 0.75,
                    "routes": [{
                        "rank": 1, "purchaseOrder": [{"id": 3031}, {"id": 3087}, {"id": 6673}],
                        "sampleCount": 50, "winRate": 0.52, "pickRate": 0.0833,
                    }, {
                        "rank": 2, "purchaseOrder": [{"id": 3087}, {"id": 3031}, {"id": 6673}],
                        "sampleCount": 550, "winRate": 0.6291, "pickRate": 0.9167,
                    }],
                }, {
                    "rank": 1, "itemSet": [{"id": 3153}, {"id": 3031}, {"id": 6673}],
                    "sampleCount": 120, "winRate": 0.65, "pickRate": 0.15,
                    "routes": [],
                }],
            }],
        },
        "unfiltered": {"archetypes": []},
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
            resource_url("summoner-spells"): SUMMONER_SPELLS,
            resource_url("items"): ITEMS,
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

    def test_summoner_statistics_remain_pairs_with_names_from_resources(self):
        self.responses[detail_url()] = SUMMONER_DETAIL
        self.responses[resource_url("summoner-spells")] = {**SUMMONER_SPELLS, "32": {"name": "Resource snowball name"}}
        result = self.provider.summoner_spells_for(157)
        self.assertEqual(result["scope"], "champion")
        self.assertEqual(result["champion_id"], "157")
        self.assertEqual(result["version"], "16.21")
        self.assertEqual(result["data_date"], "2026-10-24")
        self.assertEqual(result["source_url"], "https://aramkit.com/en-US/champions/yasuo")
        self.assertFalse(result["stale"])
        self.assertEqual(result["rows"], [
            {"spell_ids": ["4", "32"], "spell_names": ["Flash", "Resource snowball name"],
             "win_rate": 0.5864, "sample_count": 900, "pick_rate": 0.93, "low_sample": False},
            {"spell_ids": ["4", "6"], "spell_names": ["Flash", "Ghost"],
             "win_rate": 0.567, "sample_count": 90, "pick_rate": 0.0576, "low_sample": True},
        ])

    def test_augment_calls_do_not_request_spell_resources(self):
        self.responses[detail_url()] = SUMMONER_DETAIL
        self.provider.rate_for(157, 3078)
        self.assertNotIn(resource_url("summoner-spells"), self.requests)

    def test_missing_summoner_pairs_return_empty_rows_without_resource_request(self):
        for summoners in ("absent", []):
            with self.subTest(summoners=summoners), tempfile.TemporaryDirectory() as directory:
                data = copy.deepcopy(DETAIL)
                if summoners != "absent":
                    data["summoners"] = summoners
                self.responses[detail_url()] = data
                result = StatsProvider(Path(directory)).summoner_spells_for(157)
                self.assertEqual(result["rows"], [])
                self.assertFalse(result["stale"])
        self.assertNotIn(resource_url("summoner-spells"), self.requests)

    def test_summoner_zero_sample_and_absent_rates_are_not_invented(self):
        data = copy.deepcopy(SUMMONER_DETAIL)
        data["summoners"][0]["sampleCount"] = 0
        del data["summoners"][1]["winRate"]
        del data["summoners"][1]["pickRate"]
        self.responses[detail_url()] = data
        rows = self.provider.summoner_spells_for(157)["rows"]
        self.assertIsNone(rows[0]["win_rate"])
        self.assertEqual(rows[0]["sample_count"], 0)
        self.assertTrue(rows[0]["low_sample"])
        self.assertIsNone(rows[1]["win_rate"])
        self.assertIsNone(rows[1]["pick_rate"])

    def test_summoner_rates_counts_and_pair_ids_are_validated(self):
        for key, invalid in [
            ("winRate", -0.1), ("winRate", 1.1), ("winRate", True),
            ("winRate", "0.5"), ("winRate", float("nan")),
            ("pickRate", 1.1), ("pickRate", "0.5"), ("pickRate", True),
            ("sampleCount", -1), ("sampleCount", 1.5), ("sampleCount", True),
            ("sampleCount", None),
            ("spells", [{"id": 4}]), ("spells", [{"id": 4}, {"id": 4}]),
            ("spells", [{"id": 4}, {"id": "../32"}]),
            ("spells", [{"id": 4}, {"id": True}]),
            ("spells", [{"id": 4}, {}]), ("spells", [{"id": 4}, "32"]),
        ]:
            with self.subTest(key=key, value=invalid), tempfile.TemporaryDirectory() as directory:
                data = copy.deepcopy(SUMMONER_DETAIL)
                data["summoners"][0][key] = invalid
                self.responses[detail_url()] = data
                with self.assertRaises(DataUnavailableError):
                    StatsProvider(Path(directory)).summoner_spells_for(157)

    def test_malformed_and_duplicate_summoner_pairs_are_errors(self):
        reversed_pair = copy.deepcopy(SUMMONER_DETAIL["summoners"][0])
        reversed_pair["spells"].reverse()
        for invalid in (None, {}, [None], [SUMMONER_DETAIL["summoners"][0], reversed_pair]):
            with self.subTest(rows=invalid), tempfile.TemporaryDirectory() as directory:
                self.responses[detail_url()] = {**DETAIL, "summoners": invalid}
                with self.assertRaises(DataUnavailableError):
                    StatsProvider(Path(directory)).summoner_spells_for(157)

    def test_invalid_spell_rows_do_not_break_existing_augment_statistics(self):
        self.responses[detail_url()] = {**DETAIL, "summoners": "malformed"}
        self.assertEqual(self.provider.rate_for(157, 3078)["win_rate"], 0.67)
        with self.assertRaises(DataUnavailableError):
            self.provider.summoner_spells_for(157)

    def test_spell_names_must_exist_in_verified_resource(self):
        self.responses[detail_url()] = SUMMONER_DETAIL
        for resources in ({"4": {"name": "Flash"}}, {**SUMMONER_SPELLS, "32": {"name": " "}}):
            with self.subTest(resources=resources), tempfile.TemporaryDirectory() as directory:
                self.responses[resource_url("summoner-spells")] = resources
                with self.assertRaises(DataUnavailableError):
                    StatsProvider(Path(directory)).summoner_spells_for(157)

    def test_legacy_champion_cache_only_needs_new_spell_resource(self):
        self.responses[detail_url()] = SUMMONER_DETAIL
        self.provider.rate_for(157, 3078)
        count = len(self.requests)
        second = StatsProvider(Path(self.tmp.name))
        second.summoner_spells_for(157)
        self.assertEqual(self.requests[count:], [resource_url("summoner-spells")])
        second.summoner_spells_for(157)
        self.assertEqual(len(self.requests), count + 1)

    def test_cached_spell_pairs_work_offline_with_stale_metadata(self):
        self.responses[detail_url()] = SUMMONER_DETAIL
        expected = self.provider.summoner_spells_for(157)
        self.clock.return_value += MANIFEST_TTL + 1
        self.responses[MANIFEST_URL] = URLError("offline")
        count = len(self.requests)
        result = StatsProvider(Path(self.tmp.name)).summoner_spells_for(157)
        self.assertTrue(result["stale"])
        self.assertEqual(result["rows"], expected["rows"])
        self.assertEqual(result["version"], "16.21")
        self.assertEqual(self.requests[count:], [MANIFEST_URL])

    def test_offline_without_spell_names_reports_unavailable_not_empty(self):
        self.responses[detail_url()] = SUMMONER_DETAIL
        self.provider.rate_for(157, 3078)
        self.responses[resource_url("summoner-spells")] = URLError("offline")
        with self.assertRaises(DataUnavailableError) as raised:
            self.provider.summoner_spells_for(157)
        self.assertTrue(raised.exception.transient)

    def test_stale_champion_uses_its_original_spell_resource_version(self):
        self.responses[detail_url()] = SUMMONER_DETAIL
        first = self.provider.summoner_spells_for(157)
        self.next_version()
        self.responses[detail_url(NEXT_VERSION)] = TimeoutError("offline")
        result = self.provider.summoner_spells_for(157)
        self.assertTrue(result["stale"])
        self.assertEqual(result["version"], "16.21")
        self.assertEqual(result["rows"], first["rows"])
        self.assertNotIn(resource_url("summoner-spells", NEXT_VERSION), self.requests)

    def test_new_spell_resource_failure_falls_back_to_entire_old_snapshot(self):
        self.responses[detail_url()] = SUMMONER_DETAIL
        first = self.provider.summoner_spells_for(157)
        self.next_version()
        new_data = copy.deepcopy(SUMMONER_DETAIL)
        new_data["summoners"][0]["winRate"] = 0.75
        self.responses[detail_url(NEXT_VERSION)] = new_data
        spell_url = resource_url("summoner-spells", NEXT_VERSION)
        self.responses[spell_url] = URLError("offline")
        result = self.provider.summoner_spells_for(157)
        self.assertTrue(result["stale"])
        self.assertEqual(result["version"], "16.21")
        self.assertEqual(result["data_date"], "2026-10-24")
        self.assertEqual(result["rows"], first["rows"])
        attempts = len(self.requests)
        self.provider.summoner_spells_for(157)
        self.assertEqual(len(self.requests), attempts)
        self.clock.return_value += RETRY_DELAY + 1
        self.responses[spell_url] = {**SUMMONER_SPELLS, "4": {"name": "New Flash name"}}
        recovered = self.provider.summoner_spells_for(157)
        self.assertFalse(recovered["stale"])
        self.assertEqual(recovered["version"], "16.22")
        self.assertEqual(recovered["data_date"], "2026-11-01")
        self.assertEqual(recovered["rows"][0]["win_rate"], 0.75)
        self.assertEqual(recovered["rows"][0]["spell_names"][0], "New Flash name")

    def test_malformed_new_spell_resource_does_not_silently_use_old_pairs(self):
        self.responses[detail_url()] = SUMMONER_DETAIL
        self.provider.summoner_spells_for(157)
        self.next_version()
        self.responses[detail_url(NEXT_VERSION)] = SUMMONER_DETAIL
        self.responses[resource_url("summoner-spells", NEXT_VERSION)] = {"4": {"name": "Flash"}}
        with self.assertRaises(DataUnavailableError) as raised:
            self.provider.summoner_spells_for(157)
        self.assertFalse(raised.exception.transient)

    def test_summoner_pairs_honor_bracket_and_reject_invalid_inputs(self):
        self.responses[detail_url(bracket="high")] = SUMMONER_DETAIL
        result = self.provider.summoner_spells_for("157", bracket="high")
        self.assertEqual(result["bracket"], "high")
        self.assertIn(detail_url(bracket="high"), self.requests)
        count = len(self.requests)
        for champion_id, bracket in ((True, "all"), ("../157", "all"), (157, "../all")):
            with self.subTest(champion_id=champion_id, bracket=bracket), self.assertRaises(ValueError):
                self.provider.summoner_spells_for(champion_id, bracket)
        self.assertEqual(len(self.requests), count)



    def test_item_sets_and_routes_keep_distinct_rates_and_order(self):
        self.responses[detail_url()] = ITEM_DETAIL
        result = self.provider.item_builds_for(157)
        self.assertEqual((result["source"], result["scope"], result["champion_id"]), ("ARAMKit", "champion", "157"))
        self.assertEqual((result["version"], result["data_date"], result["stale"]), ("16.21", "2026-10-24", False))
        self.assertEqual(result["source_url"], "https://aramkit.com/en-US/champions/yasuo")
        self.assertEqual(result["build_filter"], "filtered")
        group = result["groups"][0]
        self.assertEqual((group["key"], group["label"], group["sample_count"]), ("crit", "Crit", 800))
        core = group["rows"][0]
        self.assertEqual(core["rank"], 2)
        self.assertEqual(core["item_ids"], ["3087", "3031", "6673"])
        self.assertEqual(core["item_names"], ["Statikk Shiv", "Infinity Edge", "Immortal Shieldbow"])
        self.assertEqual((core["win_rate"], core["sample_count"], core["pick_rate"]), (0.62, 600, 0.75))
        self.assertFalse(core["ordered"])
        self.assertFalse(core["low_sample"])
        self.assertEqual(core["pick_rate_scope"], "build_group")
        route = core["routes"][0]
        self.assertEqual(route["item_ids"], ["3031", "3087", "6673"])
        self.assertEqual(route["item_names"][0], "Infinity Edge")
        self.assertEqual((route["win_rate"], route["sample_count"], route["pick_rate"]), (0.52, 50, 0.0833))
        self.assertTrue(route["ordered"])
        self.assertTrue(route["low_sample"])
        self.assertEqual(route["pick_rate_scope"], "core_set")

    def test_item_resources_are_on_demand_and_legacy_cache_is_reused(self):
        self.responses[detail_url()] = {**ITEM_DETAIL, "summoners": SUMMONER_DETAIL["summoners"]}
        self.provider.rate_for(157, 3078)
        self.provider.summoner_spells_for(157)
        self.assertNotIn(resource_url("items"), self.requests)
        count = len(self.requests)
        StatsProvider(Path(self.tmp.name)).item_builds_for(157)
        self.assertEqual(self.requests[count:], [resource_url("items")])
        self.provider.item_builds_for(157)
        self.assertEqual(len(self.requests), count + 1)

    def test_missing_item_builds_never_use_unfiltered_or_global_data(self):
        for builds in ("absent", {}, {"filtered": {}}, {"filtered": {"archetypes": []}},
                       {"unfiltered": ITEM_DETAIL["builds"]["filtered"]}):
            with self.subTest(builds=builds), tempfile.TemporaryDirectory() as directory:
                data = copy.deepcopy(DETAIL)
                if builds != "absent":
                    data["builds"] = builds
                self.responses[detail_url()] = data
                result = StatsProvider(Path(directory)).item_builds_for(157)
                self.assertEqual(result["groups"], [])
                self.assertFalse(result["stale"])
        self.assertNotIn(resource_url("items"), self.requests)

    def test_item_zero_samples_and_absent_rates_are_not_invented(self):
        data = copy.deepcopy(ITEM_DETAIL)
        group = data["builds"]["filtered"]["archetypes"][0]
        group["sampleCount"] = 0
        core = group["profiles"][0]
        core["sampleCount"] = 0
        core["routes"][0]["sampleCount"] = 0
        del group["profiles"][1]["winRate"]
        del group["profiles"][1]["pickRate"]
        self.responses[detail_url()] = data
        group = self.provider.item_builds_for(157)["groups"][0]
        self.assertIsNone(group["win_rate"])
        self.assertIsNone(group["rows"][0]["win_rate"])
        self.assertIsNone(group["rows"][0]["routes"][0]["win_rate"])
        self.assertTrue(group["rows"][0]["low_sample"])
        self.assertIsNone(group["rows"][1]["win_rate"])
        self.assertIsNone(group["rows"][1]["pick_rate"])

    def test_item_rates_counts_and_ranks_are_validated_at_every_level(self):
        for level in ("group", "core", "route"):
            for field, invalid in [
                ("winRate", -0.1), ("winRate", 1.1), ("winRate", True),
                ("winRate", "0.5"), ("winRate", float("nan")), ("winRate", float("inf")),
                ("pickRate", -0.1), ("pickRate", 1.1), ("pickRate", True),
                ("sampleCount", -1), ("sampleCount", 1.5), ("sampleCount", True),
                ("sampleCount", None), ("rank", 0), ("rank", True), ("rank", 1.5),
            ]:
                with self.subTest(level=level, field=field, invalid=invalid), tempfile.TemporaryDirectory() as directory:
                    data = copy.deepcopy(ITEM_DETAIL)
                    row = data["builds"]["filtered"]["archetypes"][0]
                    if level != "group":
                        row = row["profiles"][0]
                    if level == "route":
                        row = row["routes"][0]
                    row[field] = invalid
                    self.responses[detail_url()] = data
                    with self.assertRaises(DataUnavailableError):
                        StatsProvider(Path(directory)).item_builds_for(157)

    def test_item_ids_and_route_membership_are_validated(self):
        for field in ("itemSet", "purchaseOrder"):
            for invalid in (None, [], [{"id": 3031}], [None] * 3,
                            [{"id": 3031}, {"id": 3087}, {"id": True}],
                            [{"id": 3031}, {"id": 3087}, {"id": "../6673"}],
                            [{"id": 3031}, {"id": 3087}, {"id": 0}]):
                with self.subTest(field=field, invalid=invalid), tempfile.TemporaryDirectory() as directory:
                    data = copy.deepcopy(ITEM_DETAIL)
                    row = data["builds"]["filtered"]["archetypes"][0]["profiles"][0]
                    if field == "purchaseOrder":
                        row = row["routes"][0]
                    row[field] = invalid
                    self.responses[detail_url()] = data
                    with self.assertRaises(DataUnavailableError):
                        StatsProvider(Path(directory)).item_builds_for(157)
        data = copy.deepcopy(ITEM_DETAIL)
        data["builds"]["filtered"]["archetypes"][0]["profiles"][0]["routes"][0]["purchaseOrder"][0]["id"] = 3153
        self.responses[detail_url()] = data
        with self.assertRaises(DataUnavailableError):
            self.provider.item_builds_for(157)

    def test_malformed_and_duplicate_item_groups_profiles_and_routes_are_errors(self):
        for mode in ("builds", "filtered", "groups", "group", "key", "profiles", "profile",
                     "routes", "route", "duplicate_group", "duplicate_core", "duplicate_route"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                data = copy.deepcopy(ITEM_DETAIL)
                filtered = data["builds"]["filtered"]
                group = filtered["archetypes"][0]
                core = group["profiles"][0]
                if mode == "builds":
                    data["builds"] = None
                elif mode == "filtered":
                    data["builds"]["filtered"] = None
                elif mode == "groups":
                    filtered["archetypes"] = {}
                elif mode == "group":
                    filtered["archetypes"] = [None]
                elif mode == "key":
                    group["key"] = "../ap"
                elif mode == "profiles":
                    group["profiles"] = None
                elif mode == "profile":
                    group["profiles"] = [None]
                elif mode == "routes":
                    core["routes"] = {}
                elif mode == "route":
                    core["routes"] = [None]
                elif mode == "duplicate_group":
                    filtered["archetypes"] *= 2
                elif mode == "duplicate_core":
                    duplicate = copy.deepcopy(core)
                    duplicate["itemSet"].reverse()
                    group["profiles"].append(duplicate)
                elif mode == "duplicate_route":
                    core["routes"].append(copy.deepcopy(core["routes"][0]))
                self.responses[detail_url()] = data
                with self.assertRaises(DataUnavailableError):
                    StatsProvider(Path(directory)).item_builds_for(157)

    def test_invalid_item_data_does_not_break_augment_statistics(self):
        self.responses[detail_url()] = {**DETAIL, "builds": "malformed"}
        self.assertEqual(self.provider.rate_for(157, 3078)["win_rate"], 0.67)
        with self.assertRaises(DataUnavailableError):
            self.provider.item_builds_for(157)

    def test_item_names_must_exist_in_verified_resources(self):
        self.responses[detail_url()] = ITEM_DETAIL
        for resources in ({"3031": {"name": "Infinity Edge"}},
                          {**ITEMS, "3031": {"name": " "}},
                          {**ITEMS, "../999": {"name": "Invalid"}}):
            with self.subTest(resources=resources), tempfile.TemporaryDirectory() as directory:
                self.responses[resource_url("items")] = resources
                with self.assertRaises(DataUnavailableError):
                    StatsProvider(Path(directory)).item_builds_for(157)

    def test_item_builds_honor_bracket_and_reject_invalid_arguments(self):
        self.responses[detail_url(bracket="high")] = ITEM_DETAIL
        result = self.provider.item_builds_for("157", "high")
        self.assertEqual(result["bracket"], "high")
        self.assertIn(detail_url(bracket="high"), self.requests)
        count = len(self.requests)
        for champion_id, bracket in ((True, "all"), ("../157", "all"), (157, "../all")):
            with self.subTest(champion_id=champion_id, bracket=bracket), self.assertRaises(ValueError):
                self.provider.item_builds_for(champion_id, bracket)
        self.assertEqual(len(self.requests), count)

    def test_cached_item_builds_work_offline_with_stale_metadata(self):
        self.responses[detail_url()] = ITEM_DETAIL
        expected = self.provider.item_builds_for(157)
        self.clock.return_value += MANIFEST_TTL + 1
        self.responses[MANIFEST_URL] = URLError("offline")
        count = len(self.requests)
        result = StatsProvider(Path(self.tmp.name)).item_builds_for(157)
        self.assertTrue(result["stale"])
        self.assertEqual(result["groups"], expected["groups"])
        self.assertEqual(result["version"], "16.21")
        self.assertEqual(self.requests[count:], [MANIFEST_URL])

    def test_offline_without_item_names_reports_unavailable_not_empty(self):
        self.responses[detail_url()] = ITEM_DETAIL
        self.provider.rate_for(157, 3078)
        self.responses[resource_url("items")] = URLError("offline")
        with self.assertRaises(DataUnavailableError) as raised:
            self.provider.item_builds_for(157)
        self.assertTrue(raised.exception.transient)

    def test_stale_item_champion_uses_its_original_resource_version(self):
        self.responses[detail_url()] = ITEM_DETAIL
        first = self.provider.item_builds_for(157)
        self.next_version()
        self.responses[detail_url(NEXT_VERSION)] = TimeoutError("offline")
        result = self.provider.item_builds_for(157)
        self.assertTrue(result["stale"])
        self.assertEqual(result["version"], "16.21")
        self.assertEqual(result["groups"], first["groups"])
        self.assertNotIn(resource_url("items", NEXT_VERSION), self.requests)

    def test_item_resource_failure_falls_back_to_entire_snapshot_then_recovers(self):
        self.responses[detail_url()] = ITEM_DETAIL
        first = self.provider.item_builds_for(157)
        self.next_version()
        new_data = copy.deepcopy(ITEM_DETAIL)
        new_data["builds"]["filtered"]["archetypes"][0]["profiles"][0]["winRate"] = 0.75
        self.responses[detail_url(NEXT_VERSION)] = new_data
        item_url = resource_url("items", NEXT_VERSION)
        self.responses[item_url] = URLError("offline")
        result = self.provider.item_builds_for(157)
        self.assertTrue(result["stale"])
        self.assertEqual(result["version"], "16.21")
        self.assertEqual(result["data_date"], "2026-10-24")
        self.assertEqual(result["groups"], first["groups"])
        attempts = len(self.requests)
        result["groups"][0]["rows"][0]["item_names"][0] = "Mutated"
        self.assertEqual(self.provider.item_builds_for(157)["groups"], first["groups"])
        self.assertEqual(len(self.requests), attempts)
        self.clock.return_value += RETRY_DELAY + 1
        self.responses[item_url] = {**ITEMS, "3087": {"name": "New item name", "icon": "assets/item/3087-0123456789ab.webp"}}
        recovered = self.provider.item_builds_for(157)
        self.assertFalse(recovered["stale"])
        self.assertEqual(recovered["version"], "16.22")
        self.assertEqual(recovered["data_date"], "2026-11-01")
        self.assertEqual(recovered["groups"][0]["rows"][0]["win_rate"], 0.75)
        self.assertEqual(recovered["groups"][0]["rows"][0]["item_names"][0], "New item name")
        self.assertEqual(recovered["groups"][0]["rows"][0]["item_icon_urls"][0], f"{DATA_BASE}/assets/item/3087-0123456789ab.webp")

    def test_invalid_new_item_resources_never_silently_use_old_builds(self):
        for mode in ("malformed", "not_found"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                self.responses[MANIFEST_URL] = manifest()
                self.responses[detail_url()] = ITEM_DETAIL
                provider = StatsProvider(Path(directory))
                provider.item_builds_for(157)
                self.next_version()
                self.responses[detail_url(NEXT_VERSION)] = ITEM_DETAIL
                item_url = resource_url("items", NEXT_VERSION)
                self.responses[item_url] = (
                    {"3031": {"name": "Infinity Edge"}} if mode == "malformed"
                    else HTTPError(item_url, 404, "Not found", {}, None)
                )
                with self.assertRaises(DataUnavailableError) as raised:
                    provider.item_builds_for(157)
                self.assertFalse(raised.exception.transient)

    def test_empty_new_item_snapshot_does_not_reuse_previous_builds(self):
        self.responses[detail_url()] = ITEM_DETAIL
        self.provider.item_builds_for(157)
        self.next_version()
        self.responses[detail_url(NEXT_VERSION)] = DETAIL
        result = self.provider.item_builds_for(157)
        self.assertEqual(result["groups"], [])
        self.assertEqual(result["version"], "16.22")
        self.assertFalse(result["stale"])
        self.assertNotIn(resource_url("items", NEXT_VERSION), self.requests)



    def test_item_icon_urls_align_with_core_and_purchase_order_names(self):
        self.responses[detail_url()] = ITEM_DETAIL
        result = self.provider.item_builds_for(157)
        core = result["groups"][0]["rows"][0]
        shiv = f"{DATA_BASE}/assets/item/3087-15854b8b753d.webp"
        edge = f"{DATA_BASE}/assets/item/3031-1dd45a28b705.webp"
        self.assertEqual(core["item_icon_urls"], [shiv, edge, None])
        self.assertEqual(core["routes"][0]["item_icon_urls"], [edge, shiv, None])
        self.assertEqual(len(core["item_icon_urls"]), len(core["item_names"]))
        self.assertEqual(len(core["item_icon_urls"]), len(core["item_ids"]))
        self.assertFalse(any("/assets/" in url for url in self.requests))

    def test_invalid_optional_item_icons_do_not_invalidate_statistics(self):
        for invalid in (
            None, "", True, 123, [], {},
            "http://data.aramkit.com/assets/item/3087-15854b8b753d.webp",
            "https://other.example/assets/item/3087-15854b8b753d.webp",
            "https://data.aramkit.com.evil.example/assets/item/3087-15854b8b753d.webp",
            "https://data.aramkit.com:443/assets/item/3087-15854b8b753d.webp",
            "https://user@data.aramkit.com/assets/item/3087-15854b8b753d.webp",
            "//data.aramkit.com/assets/item/3087-15854b8b753d.webp",
            "assets/item/../3087-15854b8b753d.webp",
            "assets/item/%2e%2e/3087-15854b8b753d.webp",
            "assets/item/3087-15854b8b753d.webp?tracking=1",
            "assets/item/3087-15854b8b753d.webp#part",
            "assets/item/3087-15854b8b753d.webp ",
            "assets/item/3087.webp",
            "assets/item/3087-15854b8b753d.png",
            "assets/champion/3087-15854b8b753d.webp",
        ):
            with self.subTest(icon=invalid), tempfile.TemporaryDirectory() as directory:
                self.responses[detail_url()] = ITEM_DETAIL
                self.responses[resource_url("items")] = {**ITEMS, "3087": {**ITEMS["3087"], "icon": invalid}}
                core = StatsProvider(Path(directory)).item_builds_for(157)["groups"][0]["rows"][0]
                self.assertIsNone(core["item_icon_urls"][0])
                self.assertEqual((core["win_rate"], core["sample_count"]), (0.62, 600))
                self.assertEqual(core["item_names"][0], "Statikk Shiv")


if __name__ == "__main__":
    unittest.main()
