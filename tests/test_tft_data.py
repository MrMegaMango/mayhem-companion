import json
import tempfile
import time
import unittest
from unittest.mock import patch

from mayhem.data import DataUnavailableError
from mayhem.tft_data import AUGMENT_TIERS_URL, TftProvider, _AugmentIcons, _stats


def fixtures(patch_id=16210, name="Ahri"):
    locale = {
        "Units": {"DA_Ahri": name, "DA_Ahri_desc": "Deal <b>100</b> damage."},
        "Items": {"DA_A": "Item A", "DA_A_desc": "A description", "DA_B": "Item B", "DA_C": "Item C"},
        "Traits": {"DA_Trait": "Coven"},
        "Augments": {"DA_Aug": "A plan", "DA_Aug_desc": "Gain 10 gold."},
    }
    js = "\n".join(f"window.s18{k}i18n = {json.dumps(v)};" for k, v in locale.items())
    data = {"aperture": {"patch": {"_0": patch_id}, "rankGroup": 1, "queue": 1100},
            "statsData": {"lastUpdated": 1792987986, "units": {
                "DA_Ahri": {"count": 2000, "place": 4.5, "top4": 50, "won": 12.5,
                            "topItems": ["DA_A", "DA_B", "DA_C"]}}}}
    page = ('<title>TFT Units Stats - Best Units for Set 18 Patch 18.5</title>'
            '<script src="https://ap.tft.tools/static/s18/en.js"></script>'
            '<img src="https://ap.tft.tools/img/gg17/face/da_ahri.jpg?w=56">'
            '<img src="https://ap.tft.tools/img/items_s14/DA_A.png?w=18">'
            '<script id="__NEXT_DATA__" type="application/json">'
            + json.dumps({"props": {"pageProps": data}}) + '</script>')
    comps = {"groups": [{"full": {
        "code": "123TFTSet18", "count": 1000, "place": 4.2, "top4": 600, "win": 150,
        "carryUnits": [["DA_Ahri", 2.5]], "traits": [["DA_Trait", 2, 900]],
        "comps": [{"units": ["DA_Ahri"], "count": 400, "place": 3.8, "top4": 300, "win": 80}],
    }}]}
    tiers = {"tft_set": "TFTSet18", "tier_list_set": "TFTSet18", "content": {
        "author": {"gameName": "Expert"}, "updated_at": "2026-10-24T00:00:00Z",
        "content": {"tierList": [{"label": "S", "content": [{"id": "DA_Aug", "type": "augment"}]}]}}}
    build = {"unitId": "DA_Ahri", "starLevel": 0,
             "itemTrios": [{"items": ["DA_A", "DA_B", "DA_C"], "count": 400, "place": 3.9, "top4": 62.5, "won": 18.75}]}
    return {
        "https://tactics.tools/units": page,
        "https://ap.tft.tools/static/s18/en.js": js,
        f"https://api.tft.tools/team-compositions/1/{patch_id}": json.dumps(comps),
        AUGMENT_TIERS_URL: json.dumps(tiers),
        "https://tactics.tools/info/augments": '<div id="DA_Aug"><img src="https://ap.tft.tools/img/augments/DA_Aug3.png?w=42"></div>',
        f"https://d3.tft.tools/stats2/unit/1100/DA_Ahri/{patch_id}/1": json.dumps(build),
    }


class TftDataTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.provider = TftProvider(self.tmp.name)
        self.payloads = fixtures()
        self.requests = []

    def get(self, url):
        self.requests.append(url)
        return self.payloads[url]

    def load(self, force=False):
        with patch.object(self.provider, "_get_text", side_effect=self.get):
            return self.provider.load(force=force)

    def builds(self, force=False):
        with patch.object(self.provider, "_get_text", side_effect=self.get):
            return self.provider.items_for("DA_Ahri", force=force)

    def test_counts_and_percentages_use_different_denominators(self):
        comp = _stats({"count": 400, "place": 4, "top4": 240, "win": 80}, percentages=False)
        build = _stats({"count": 400, "place": 4, "top4": 60, "won": 20}, percentages=True)
        for row in (comp, build):
            self.assertEqual(row["top4_rate"], .6)
            self.assertEqual(row["first_rate"], .2)

    def test_rejects_impossible_or_nonfinite_stats(self):
        for row in ({"count": 10, "place": 4, "top4": 9, "win": 10},
                    {"count": 10, "place": float("nan"), "top4": 5, "win": 1},
                    {"count": True, "place": 4, "top4": 1, "win": 0}):
            with self.subTest(row=row), self.assertRaises(DataUnavailableError):
                _stats(row, percentages=False)

    def test_snapshot_scope_and_editorial_augment_tiers(self):
        result = self.load()
        self.assertEqual(len(self.requests), 5)
        self.assertEqual((result["patch"], result["set"], result["rank"], result["queue"]),
                         ("18.5", "Set 18", "Diamond+", "Ranked"))
        self.assertEqual(result["items"], [])
        comp = result["comps"][0]
        self.assertEqual((comp["avg_place"], comp["games"], comp["board_games"]), (4.2, 1000, 400))
        self.assertEqual(comp["units"][0]["name"], "Ahri")
        augment = result["augments"][0]
        self.assertEqual((augment["tier"], augment["scope"]), ("S", "expert_tier"))
        self.assertNotIn("win_rate", augment)
        self.assertNotIn("avg_place", augment)
        self.assertEqual(augment["icon_url"], "https://ap.tft.tools/img/augments/DA_Aug3.png?w=64")

    def test_lazy_builds_use_actual_trio_rates_not_unit_rates(self):
        self.load()
        self.requests.clear()
        result = self.builds()
        self.assertEqual(len(self.requests), 1)
        self.assertTrue(self.requests[0].endswith("/DA_Ahri/16210/1"))
        row = result["items"][0]
        self.assertEqual(len(row["items"]), 3)
        self.assertEqual((row["avg_place"], row["games"], row["top4_rate"], row["first_rate"]), (3.9, 400, .625, .1875))
        self.assertEqual(row["scope"], "unit_three_item_build")
        self.assertEqual(result["source_url"], "https://tactics.tools/units/da_ahri")
        self.assertEqual(result["updated_scope"], "dataset")
        row["avg_place"] = 1
        with patch.object(self.provider, "_get_text", side_effect=AssertionError("network")):
            self.assertEqual(self.provider.items_for("DA_Ahri")["items"][0]["avg_place"], 3.9)

    def test_fresh_disk_snapshot_needs_no_network(self):
        self.load()
        provider = TftProvider(self.tmp.name)
        with patch.object(provider, "_get_text", side_effect=AssertionError("network")):
            self.assertFalse(provider.load()["stale"])

    def test_transport_failure_keeps_entire_old_snapshot_stale(self):
        self.load()
        self.payloads = fixtures(16211, "New Ahri")
        def failing(url):
            if "/team-compositions/" in url:
                raise DataUnavailableError("offline", transient=True)
            return self.get(url)
        with patch.object(self.provider, "_get_text", side_effect=failing):
            result = self.provider.load(force=True)
        self.assertTrue(result["stale"])
        self.assertEqual(result["_patch_id"], 16210)
        self.assertEqual(result["units"][0]["name"], "Ahri")

    def test_wrong_set_or_malformed_stats_fail_even_with_cache(self):
        self.load()
        url = "https://api.tft.tools/team-compositions/1/16210"
        self.payloads[url] = self.payloads[url].replace("TFTSet18", "TFTSet17")
        with self.assertRaises(DataUnavailableError):
            self.load(force=True)

    def test_wrong_set_augment_tiers_become_reference(self):
        self.payloads[AUGMENT_TIERS_URL] = self.payloads[AUGMENT_TIERS_URL].replace("TFTSet18", "TFTSet17")
        augment = self.load()["augments"][0]
        self.assertIsNone(augment["tier"])
        self.assertEqual(augment["scope"], "reference")

    def test_optional_guidance_failure_preserves_stats(self):
        def get(url):
            if url in (AUGMENT_TIERS_URL, "https://tactics.tools/info/augments"):
                raise DataUnavailableError("offline", transient=True)
            return self.get(url)
        with patch.object(self.provider, "_get_text", side_effect=get):
            result = self.provider.load()
        self.assertEqual(len(result["comps"]), 1)
        self.assertEqual(result["augments"][0]["scope"], "reference")

    def test_unit_mismatch_and_missing_item_are_rejected(self):
        self.load()
        url = "https://d3.tft.tools/stats2/unit/1100/DA_Ahri/16210/1"
        for payload in (self.payloads[url].replace('"unitId": "DA_Ahri"', '"unitId": "DA_Other"'),
                        self.payloads[url].replace('"DA_C"', '"DA_Unknown"')):
            self.payloads[url] = payload
            with self.assertRaises(DataUnavailableError):
                self.builds(force=True)

    def test_empty_trios_do_not_substitute_unit_rates(self):
        self.load()
        url = "https://d3.tft.tools/stats2/unit/1100/DA_Ahri/16210/1"
        payload = json.loads(self.payloads[url])
        payload["itemTrios"] = []
        self.payloads[url] = json.dumps(payload)
        self.assertEqual(self.builds()["items"], [])

    def test_build_cache_cannot_cross_patch_boundary(self):
        self.load()
        self.builds()
        self.payloads = fixtures(16211)
        self.load(force=True)
        with patch.object(self.provider, "_get_text", side_effect=DataUnavailableError("offline", transient=True)), self.assertRaises(DataUnavailableError):
            self.provider.items_for("DA_Ahri")

    def test_build_transport_fallback_explicitly_stale(self):
        self.load()
        self.builds()
        with patch.object(self.provider, "_get_text", side_effect=DataUnavailableError("offline", transient=True)):
            self.assertTrue(self.provider.items_for("DA_Ahri", force=True)["stale"])

    def test_snapshot_refresh_revalidates_fresh_same_patch_builds(self):
        # Windows clock resolution can put all requests in the same clock tick.
        with patch("mayhem.tft_data.time.time", return_value=1800000000.0):
            self.load()
            self.builds()
            self.load(force=True)
            self.requests.clear()
            self.builds()
            self.assertEqual(len(self.requests), 1)
            self.requests.clear()
            self.builds()
            self.assertEqual(self.requests, [])

    def test_fresh_build_disk_cache_survives_provider_restart(self):
        self.load()
        self.builds()
        provider = TftProvider(self.tmp.name)
        with patch.object(provider, "_get_text", side_effect=AssertionError("network")):
            self.assertFalse(provider.items_for("DA_Ahri")["stale"])

    def test_pre_revision_build_cache_remains_usable_until_refresh(self):
        snapshot = self.load()
        builds = self.builds()
        snapshot.pop("_snapshot_revision")
        builds.pop("_snapshot_revision")
        self.provider._write("snapshot.json", snapshot)
        self.provider._write("builds-16210-DA_Ahri.json", builds)
        provider = TftProvider(self.tmp.name)
        with patch.object(provider, "_get_text", side_effect=AssertionError("network")):
            self.assertFalse(provider.items_for("DA_Ahri")["stale"])
        with patch.object(provider, "_get_text", side_effect=self.get):
            provider.load(force=True)
            self.requests.clear()
            provider.items_for("DA_Ahri")
        self.assertEqual(len(self.requests), 1)

    def test_corrupt_build_cache_is_ignored(self):
        snapshot = self.load()
        build = self.builds()
        build["items"][0]["first_rate"] = 8
        self.assertFalse(self.provider._valid_build_cache(build, snapshot, "DA_Ahri"))
        build["unit"] = None
        self.assertFalse(self.provider._valid_build_cache(build, snapshot, "DA_Ahri"))

    def test_invalid_unit_never_generates_network_url(self):
        self.load()
        for unit in ("../secret", "https://elsewhere", "DA_Missing"):
            with self.subTest(unit=unit), patch.object(self.provider, "_get_text", side_effect=AssertionError("network")):
                with self.assertRaises((ValueError, DataUnavailableError)):
                    self.provider.items_for(unit)

    def test_future_and_nonfinite_cache_timestamps_are_rejected(self):
        snapshot = self.load()
        for timestamp in (time.time() + 99999, float("nan"), float("inf"), True):
            snapshot["_fetched_at"] = timestamp
            self.assertFalse(self.provider._valid_snapshot(snapshot))

    def test_icon_parser_uses_parent_id_and_validated_host(self):
        parser = _AugmentIcons()
        parser.feed('<div id="DA_Aug"><div><img src="https://evil.example/x.png"></div>'
                    '<img src="https://ap.tft.tools/img/augments/DA_Aug2.png?w=42"></div>')
        self.assertEqual(parser.icons, {"DA_Aug": "https://ap.tft.tools/img/augments/DA_Aug2.png?w=64"})


if __name__ == "__main__":
    unittest.main()
