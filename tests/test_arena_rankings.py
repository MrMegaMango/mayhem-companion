import json
import unittest

from mayhem.arena_rankings import MAX_CHAMPIONS, parse_rankings, ranking_url
from mayhem.data import DataUnavailableError


CHAMPIONS = {
    "39": {"id": "39", "name": "Irelia", "raw_name": "Irelia", "icon_url": "https://winrate.lol/img/w64/champion/Irelia.jpg"},
    "62": {"id": "62", "name": "Wukong", "raw_name": "MonkeyKing", "icon_url": "https://winrate.lol/img/w64/champion/MonkeyKing.jpg"},
}
URL = ranking_url("26.23")
ROLLING_URL = ranking_url(None, window="30")
CANONICAL = "https://lolalytics.com/lol/tierlist/arena/?tier=all"


def fixture(*, window="current"):
    return {
        "navigation": {"path": "/lol/tierlist/arena/", "lang": "en", "page": "tierlist",
                       "mode": "arena", "patch": "30" if window == "30" else "16.23",
                       "tier": "all", "region": "all", "view": "list", "status": 200},
        "catalog": {"patch": "16.23", "champions": {"irelia": "Irelia", "wukong": "Wukong"},
                    "champPath": {"39": "irelia", "62": "wukong"}, "champId": {"irelia": 39, "wukong": 62}},
        "statistics": {"queue": 1750, "view": "list", "fields": [], "avgWr": 50, "analysed": 4050,
                       "response": {"valid": True}, "cid": {
                           "39": {"wr": 49.02, "games": 4000}, "62": {"wr": 0, "games": 50}}},
    }


def base36(number):
    if not number:
        return "0"
    result = ""
    while number:
        number, digit = divmod(number, 36)
        result = "0123456789abcdefghijklmnopqrstuvwxyz"[digit] + result
    return result


def page(data=None, *, canonical=CANONICAL, transform=None):
    objects = []
    def encode(value):
        index = len(objects)
        objects.append(None)
        if isinstance(value, dict):
            objects[index] = {key: encode(child) for key, child in value.items()}
        elif isinstance(value, list):
            objects[index] = [encode(child) for child in value]
        else:
            objects[index] = value
        return base36(index)
    encode(fixture() if data is None else data)
    if transform:
        transform(objects)
    return (f'<html><head><link rel="canonical" href="{canonical}"></head><body>'
            '<script type="qwik/json">' + json.dumps({"objs": objects}) + '</script></body></html>')


class ArenaRankingsTests(unittest.TestCase):
    def parse(self, data=None, *, canonical=CANONICAL, url=URL, window="current", **kwargs):
        return parse_rankings(page(data, canonical=canonical), CHAMPIONS, url, window=window, **kwargs)

    def test_current_snapshot_keeps_all_rows_and_source_scope(self):
        result = self.parse(expected_patch="26.23")
        self.assertEqual(result["source"], "Lolalytics")
        self.assertEqual(result["version"], "16.23")
        self.assertEqual(result["data_window"], "current")
        self.assertEqual(result["asset_patch"], "16.23")
        self.assertEqual(result["champion_observations"], 4050)
        self.assertEqual(len(result["rows"]), 2)
        row = result["rows"][0]
        self.assertEqual(row["id"], "39")
        self.assertEqual(row["champion_id"], "39")
        self.assertAlmostEqual(row["win_rate"], .4902)
        self.assertEqual(row["sample_count"], 4000)
        self.assertFalse(row["low_sample"])
        self.assertIsNone(row["first_rate"])
        self.assertIsNone(row["avg_placement"])
        self.assertEqual(row["icon_url"], CHAMPIONS["39"]["icon_url"])
        self.assertIn("all ranks", result["sample_label"])
        self.assertIn("all regions", result["sample_label"])
        self.assertIn("source queue 1750", result["sample_label"])
        self.assertNotIn("augments", result)

    def test_zero_percent_is_preserved_but_zero_samples_are_unknown(self):
        result = self.parse()
        self.assertEqual(result["rows"][1]["win_rate"], 0)
        self.assertTrue(result["rows"][1]["low_sample"])
        data = fixture()
        data["statistics"]["cid"]["62"]["games"] = 0
        row = self.parse(data)["rows"][1]
        self.assertIsNone(row["win_rate"])
        self.assertTrue(row["missing"])

    def test_identity_catalog_return_is_independent_of_caller(self):
        result = self.parse()
        result["champions"]["39"]["name"] = "changed"
        self.assertEqual(CHAMPIONS["39"]["name"], "Irelia")

    def test_rolling_window_does_not_claim_the_asset_patch_as_match_patch(self):
        data = fixture(window="30")
        data["catalog"]["patch"] = "16.24"
        result = self.parse(data, url=ROLLING_URL, canonical=ROLLING_URL, window="30", expected_patch="26.23")
        self.assertEqual(result["data_window"], "30")
        self.assertEqual(result["window_days"], 30)
        self.assertIsNone(result["version"])
        self.assertIsNone(result["source_patch"])
        self.assertEqual(result["asset_patch"], "16.24")
        self.assertIn("span balance patches", result["rate_note"])

    def test_current_sample_patch_is_validated_independently_of_asset_version(self):
        data = fixture()
        data["catalog"]["patch"] = "16.24"
        result = self.parse(data, expected_patch="26.23")
        self.assertEqual(result["version"], "16.23")
        self.assertEqual(result["asset_patch"], "16.24")
        data["catalog"]["patch"] = "unknown"
        with self.assertRaises(DataUnavailableError):
            self.parse(data)

    def test_url_builder_only_supports_fixed_routes_and_windows(self):
        self.assertEqual(ranking_url("16.23"), URL)
        self.assertEqual(URL, CANONICAL + "&patch=16.23")
        self.assertEqual(ROLLING_URL, CANONICAL + "&patch=30")
        for window in ("7", "all", "../30", 30, None):
            with self.subTest(window=window), self.assertRaises(DataUnavailableError):
                ranking_url("16.23", window=window)
        for patch in (None, "30", "16.23&region=na", "unknown"):
            with self.subTest(patch=patch), self.assertRaises(DataUnavailableError):
                ranking_url(patch)

    def test_rejects_wrong_origin_mode_and_duplicate_filters(self):
        for url in (URL.replace("lolalytics.com", "evil.invalid"), URL.replace("arena", "aram"),
                    URL + "&tier=all", URL + "&region=na", URL + "#unexpected",
                    URL.replace("tier=all", "tier=emerald_plus"), CANONICAL):
            with self.subTest(url=url), self.assertRaises(DataUnavailableError):
                self.parse(url=url)
        for canonical in (CANONICAL.replace("lolalytics.com", "evil.invalid"),
                          CANONICAL.replace("arena", "aram"), URL.replace("16.23", "16.22")):
            with self.subTest(canonical=canonical), self.assertRaises(DataUnavailableError):
                self.parse(canonical=canonical)

    def test_rejects_wrong_navigation_filters_and_status(self):
        for key, value in (("path", "/lol/tierlist/"), ("page", "arena"), ("mode", "aram"),
                           ("lang", "ko"), ("tier", "emerald_plus"), ("region", "na"),
                           ("view", "grid"), ("status", 404), ("status", True)):
            data = fixture()
            data["navigation"][key] = value
            with self.subTest(key=key), self.assertRaises(DataUnavailableError):
                self.parse(data)

    def test_rejects_wrong_stats_queue_view_or_invalid_response(self):
        for key, value in (("queue", 1700), ("queue", "1750"), ("view", "grid"),
                           ("response", {"valid": False}), ("response", {"valid": 1})):
            data = fixture()
            data["statistics"][key] = value
            with self.subTest(key=key), self.assertRaises(DataUnavailableError):
                self.parse(data)

    def test_rejects_mismatched_current_patch_and_rolling_windows(self):
        with self.assertRaises(DataUnavailableError):
            self.parse(expected_patch="26.22")
        data = fixture()
        data["navigation"]["patch"] = "16.22"
        with self.assertRaises(DataUnavailableError):
            self.parse(data)
        with self.assertRaises(DataUnavailableError):
            self.parse(fixture(window="30"))
        for data, canonical in ((fixture(), ROLLING_URL), (fixture(window="30"), CANONICAL)):
            with self.subTest(data=data), self.assertRaises(DataUnavailableError):
                self.parse(data, url=ROLLING_URL, canonical=canonical, window="30")

    def test_rejects_untrusted_or_conflicting_champion_identities(self):
        for change in ("unknown", "wrong_name", "wrong_path", "wrong_id", "noninteger_id", "invalid_stat_id"):
            data = fixture()
            if change == "unknown":
                data["statistics"]["cid"]["9999"] = {"wr": 55, "games": 500}
            elif change == "wrong_name":
                data["catalog"]["champions"]["irelia"] = "Yasuo"
            elif change == "wrong_path":
                data["catalog"]["champPath"]["39"] = "yasuo"
            elif change == "wrong_id":
                data["catalog"]["champId"]["irelia"] = 157
            elif change == "noninteger_id":
                data["catalog"]["champId"]["irelia"] = "39"
            else:
                data["statistics"]["cid"]["039"] = data["statistics"]["cid"].pop("39")
            with self.subTest(change=change), self.assertRaises(DataUnavailableError):
                self.parse(data)

    def test_rejects_invalid_numeric_rates_and_samples(self):
        for key, value in (("wr", True), ("wr", -1), ("wr", 101), ("wr", float("nan")),
                           ("wr", float("inf")), ("games", 1.5), ("games", -1), ("games", True)):
            data = fixture()
            data["statistics"]["cid"]["39"][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(DataUnavailableError):
                self.parse(data)
        for key in ("analysed", "avgWr"):
            data = fixture()
            data["statistics"][key] = -1
            with self.subTest(key=key), self.assertRaises(DataUnavailableError):
                self.parse(data)

    def test_rejects_oversized_or_missing_rows_and_resource_catalogs(self):
        for value in ({}, [], {str(i): {} for i in range(MAX_CHAMPIONS + 1)}):
            data = fixture()
            data["statistics"]["cid"] = value
            with self.subTest(rows=type(value).__name__), self.assertRaises(DataUnavailableError):
                self.parse(data)
        for champions in ({}, [], {str(i): {} for i in range(MAX_CHAMPIONS + 1)}):
            with self.assertRaises(DataUnavailableError):
                parse_rankings(page(), champions, URL)

    def test_bounded_snapshot_rejects_duplicate_sections_and_broken_references(self):
        html = page()
        for bad in ("<html></html>", html.replace('</body>', '<script type="qwik/json">{}</script></body>')):
            with self.assertRaises(DataUnavailableError):
                parse_rankings(bad, CHAMPIONS, URL)
        def broken(objects):
            nav = next(obj for obj in objects if isinstance(obj, dict) and "region" in obj)
            nav["region"] = "zzzzzz"
        with self.assertRaises(DataUnavailableError):
            parse_rankings(page(transform=broken), CHAMPIONS, URL)


if __name__ == "__main__":
    unittest.main()
