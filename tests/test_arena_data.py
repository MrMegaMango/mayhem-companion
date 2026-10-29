import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from mayhem.arena_data import (
    ArenaProvider, BASE, INDEX_URL, MAX_BYTES, VERSIONS_URL,
    _ddragon, _merge_names, _reference_names, parse_catalog, parse_champion,
)
from mayhem.data import DataUnavailableError


DDRAGON = {"data": {
    "Yasuo": {"key": "157", "id": "Yasuo", "name": "Yasuo"},
    "Lux": {"key": "99", "id": "Lux", "name": "Lux"},
}}
NAMES = _ddragon(DDRAGON)
URL = INDEX_URL + "/yasuo"


def page(body, url=INDEX_URL, version="26.21"):
    return (f'<html><head><title>Arena Stats Patch {version}</title>'
            f'<link rel="canonical" href="{url}"></head><body>{body}</body></html>')


def observation(kind="augment", ident="43", name="Now You See Me", rate="75.0%", count="12", avg="2.50"):
    icon = f"/img/augment/{ident}.png" if kind == "augment" else f"/img/w128/item/{ident}.jpg"
    return (f'<div class="ar-aug-row"><span class="tl-rank">1</span>'
            f'<span class="ar-aug-name itm"><img class="ar-aug-ico aug-gold" src="{icon}" alt="{name}">'
            f'<b>{name}</b><span class="itm-desc">Description for {name}</span></span>'
            f'<span class="tl-num ar-place">{avg}</span><span class="tl-num wr-hi">{rate}</span>'
            f'<span class="tl-num tl-dim">{count}</span>'
            '<span class="ar-share"><span class="tl-num">15.2%</span></span>'
            '<span class="ar-aug-delta">+0.5</span></div>')


def catalog_html():
    champion = ('<a class="tl-row ar-head" href="/en/arena/yasuo">'
                '<span class="tl-champ"><img src="/img/w64/champion/Yasuo.jpg" alt="Yasuo"><b>Yasuo</b></span>'
                '<span class="tl-num ar-place">3.25</span><span class="tl-num wr-hi">60.0%</span>'
                '<span class="tl-num">25.0%</span><span class="tl-num">8.0%</span>'
                '<span class="tl-num">1,240</span></a>')
    # Global item observations must not enter the OCR augment dictionary.
    return page(champion + '<div class="ar-augs"><h2>Augments</h2>' + observation(rate="99.0%") + '</div>'
                '<div class="ar-augs"><h2>Items</h2>' + observation("item", "223031", "Infinity Edge") + '</div>')


def detail_html(augments=None, items=None):
    summary = ''.join(f'<div class="ar-stat"><span class="stat-val">{value}</span>'
                      f'<span class="stat-lbl">{label}</span></div>' for value, label in
                      (("3.97", "Avg. Place"), ("38.7%", "Win Rate"), ("6.5%", "1st Place"), ("31", "Games")))
    if augments is None:
        augments = observation()
    if items is None:
        items = observation("item", "223031", "Infinity Edge", rate="80.0%")
    return page('<h1>Yasuo</h1>' + summary +
                '<div class="ar-augs ar-augs--focus"><h2>Augments taken with Yasuo</h2>' + augments + '</div>'
                '<div class="ar-augs ar-augs--focus"><h2>Items taken with Yasuo</h2>' + items + '</div>', URL)


class ArenaParsingTests(unittest.TestCase):
    def setUp(self):
        self.catalog = parse_catalog(catalog_html(), NAMES)
        self.champion = self.catalog["champions"]["157"]

    def test_catalog_preserves_real_ids_and_separate_rate_rows(self):
        self.assertEqual(self.champion["slug"], "yasuo")
        self.assertEqual(set(self.catalog["champions"]), {"157", "99"})
        self.assertEqual(len(self.catalog["rows"]), 1)
        row = self.catalog["rows"][0]
        self.assertEqual(row["win_rate"], .6)
        self.assertEqual(row["first_rate"], .25)
        self.assertEqual(row["sample_count"], 1240)
        self.assertEqual(row["champion_id"], "157")
        self.assertNotIn("win_rate", self.catalog["augments"]["43"])
        self.assertNotIn("223031", self.catalog["augments"])

    def test_champion_specific_rates_are_fractional_and_not_global(self):
        data = parse_champion(detail_html(), self.champion, URL)
        self.assertEqual(data["champion_id"], "157")
        self.assertEqual(data["summary"]["win_rate"], .387)
        self.assertEqual(data["augments"][0]["win_rate"], .75)
        self.assertEqual(data["items"][0]["win_rate"], .8)
        self.assertEqual(data["items"][0]["icon_url"], BASE + "/img/w128/item/223031.jpg")
        self.assertTrue(data["items"][0]["low_sample"])
        self.assertNotIn("top4_rate", data["items"][0])
        self.assertEqual(data["win_rate_label"], "Win rate")
        self.assertIn("finish cutoff not specified", data["rate_note"])

    def test_empty_observation_section_is_missing_not_global_fallback(self):
        data = parse_champion(detail_html(augments="", items=""), self.champion, URL)
        self.assertEqual(data["augments"], [])
        self.assertEqual(data["items"], [])

    def test_zero_sample_is_unknown_not_zero_percent(self):
        data = parse_champion(detail_html(augments=observation(count="0", rate="0.0%")), self.champion, URL)
        self.assertIsNone(data["augments"][0]["win_rate"])
        self.assertIsNone(data["augments"][0]["avg_placement"])

    def test_rejects_wrong_mode_champion_and_patch(self):
        for html in (detail_html().replace(URL, BASE + "/en/aram/yasuo"),
                     detail_html().replace('<h1>Yasuo</h1>', '<h1>Lux</h1>'),
                     detail_html().replace("Arena Stats", "ARAM Stats"),
                     detail_html().replace("26.21", "unknown")):
            with self.subTest(html=html[:150]), self.assertRaises(DataUnavailableError):
                parse_champion(html, self.champion, URL)

    def test_rejects_wrong_category_or_untrusted_image(self):
        for html in (detail_html(augments=observation("item", "223031")),
                     detail_html().replace("/img/augment/43.png", "https://evil.invalid/43.png"),
                     detail_html().replace("/img/augment/43.png", "/img/augment/../43.png")):
            with self.subTest(html=html[:50]), self.assertRaises(DataUnavailableError):
                parse_champion(html, self.champion, URL)

    def test_rejects_invalid_numeric_values_and_changed_columns(self):
        for row in (observation(rate="120%"), observation(rate="NaN%"), observation(avg="20.0"),
                    observation(count="-1"), observation(count="3.5"),
                    observation().replace('class="tl-num wr-hi"', 'class="changed-column"')):
            with self.subTest(row=row), self.assertRaises(DataUnavailableError):
                parse_champion(detail_html(augments=row), self.champion, URL)

    def test_rejects_missing_section_and_duplicate_rows(self):
        for html in (detail_html().replace("Items taken with Yasuo", "Unknown section"),
                     detail_html(augments=observation() + observation())):
            with self.assertRaises(DataUnavailableError):
                parse_champion(html, self.champion, URL)

    def test_rejects_unknown_catalog_champion_and_empty_page(self):
        for html in (catalog_html().replace("champion/Yasuo.jpg", "champion/Unknown.jpg"), page("")):
            with self.assertRaises(DataUnavailableError):
                parse_catalog(html, NAMES)

    def test_parser_bounds_nesting_and_size(self):
        for html in ("<div>" * 110, "x" * (MAX_BYTES + 1)):
            with self.assertRaises(DataUnavailableError):
                parse_catalog(html, NAMES)

    def test_reference_names_include_cherry_imports_without_aram_duplicates(self):
        reference = _reference_names()
        self.assertGreater(len(reference), 400)
        self.assertEqual(reference["41"]["name"], "Goliath")
        self.assertNotIn("1041", reference)
        self.assertEqual(reference["1320"]["name"], "Upgrade Collector")
        self.assertFalse(any("win_rate" in row for row in reference.values()))
        self.assertEqual(len(reference), len({row["name"] for row in reference.values()}))

    def test_observed_id_wins_conflicting_reference_name(self):
        observed = {"99": {"id": "99", "name": "Goliath", "description": "Source text"}}
        merged = _merge_names(observed, {"41": {"id": "41", "name": "Goliath"}})
        self.assertEqual(merged, observed)


class ArenaProviderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.provider = ArenaProvider(Path(self.temp.name))
        self.resource = "https://ddragon.leagueoflegends.com/cdn/16.21.1/data/en_US/champion.json"
        self.payloads = {VERSIONS_URL: '["16.21.1"]', self.resource: json.dumps(DDRAGON),
                         INDEX_URL: catalog_html(), URL: detail_html()}
        self.calls = []
        self.clock = patch("mayhem.arena_data.time.time", return_value=1000.)
        self.now = self.clock.start()
        self.addCleanup(self.clock.stop)
        self.addCleanup(self.temp.cleanup)

    def download(self, url):
        self.calls.append(url)
        value = self.payloads[url]
        if isinstance(value, Exception):
            raise value
        return value

    def test_one_on_demand_page_and_warm_cache_no_extra_downloads(self):
        with patch.object(self.provider, "_download", side_effect=self.download):
            catalog = self.provider.load()
            self.assertNotIn(URL, self.calls)
            detail = self.provider.champion_for("157")
            detail["items"].clear()
            catalog["champions"].clear()
            self.assertEqual(len(self.provider.champion_for(157)["items"]), 1)
            self.assertEqual(self.calls, [VERSIONS_URL, self.resource, INDEX_URL, URL])

    def test_disk_cache_survives_restart(self):
        with patch.object(self.provider, "_download", side_effect=self.download):
            self.provider.champion_for("157")
        restarted = ArenaProvider(Path(self.temp.name))
        with patch.object(restarted, "_download", side_effect=AssertionError("unexpected download")):
            self.assertEqual(restarted.champion_for("157")["augments"][0]["win_rate"], .75)

    def test_transient_fallback_keeps_whole_original_champion_snapshot(self):
        with patch.object(self.provider, "_download", side_effect=self.download):
            before = self.provider.champion_for("157")
            self.now.return_value = 5000.
            self.payloads[URL] = DataUnavailableError("offline", transient=True)
            after = self.provider.champion_for("157")
            self.assertTrue(after["stale"])
            self.assertEqual(after["version"], before["version"])
            self.assertEqual(after["items"], before["items"])
            self.assertEqual(after["fetched_at"], before["fetched_at"])
            calls = len(self.calls)
            self.provider.champion_for("157")
            self.assertEqual(len(self.calls), calls)

    def test_malformed_or_denied_never_uses_stale_stats(self):
        with patch.object(self.provider, "_download", side_effect=self.download):
            self.provider.champion_for("157")
            self.now.return_value = 5000.
            for bad in (detail_html().replace("75.0%", "NaN%"), DataUnavailableError("HTTP403")):
                self.payloads[URL] = bad
                with self.assertRaises(DataUnavailableError):
                    self.provider.champion_for("157", force=True)

    def test_failed_real_request_has_retry_backoff(self):
        self.payloads[URL] = DataUnavailableError("offline", transient=True)
        with patch.object(self.provider, "_download", side_effect=self.download):
            for _ in range(2):
                with self.assertRaises(DataUnavailableError):
                    self.provider.champion_for("157")
            self.assertEqual(self.calls.count(URL), 1)
            self.now.return_value = 1061.
            self.payloads[URL] = detail_html()
            self.assertFalse(self.provider.champion_for("157")["stale"])
            self.assertEqual(self.calls.count(URL), 2)

    def test_force_bypasses_error_cooldown(self):
        self.payloads[URL] = DataUnavailableError("offline", transient=True)
        with patch.object(self.provider, "_download", side_effect=self.download):
            with self.assertRaises(DataUnavailableError):
                self.provider.champion_for("157")
            self.payloads[URL] = detail_html()
            self.assertFalse(self.provider.champion_for("157", force=True)["stale"])

    def test_latest_empty_section_does_not_backfill_previous_rates(self):
        with patch.object(self.provider, "_download", side_effect=self.download):
            self.provider.champion_for("157")
            self.payloads[URL] = detail_html(augments="", items="")
            result = self.provider.champion_for("157", force=True)
            self.assertEqual(result["augments"], [])
            self.assertEqual(result["items"], [])

    def test_unobserved_champion_is_missing_without_slug_guess(self):
        with patch.object(self.provider, "_download", side_effect=self.download):
            result = self.provider.champion_for("99")
            self.assertTrue(result["missing"])
            self.assertEqual(result["summary"], {})
            self.assertEqual(self.calls, [VERSIONS_URL, self.resource, INDEX_URL])

    def test_rejects_arbitrary_ids_before_network(self):
        with patch.object(self.provider, "_download") as download:
            for ident in (True, None, "../157", "157?mode=aram", "0", "-1", 1.5):
                with self.assertRaises(ValueError):
                    self.provider.champion_for(ident)
            download.assert_not_called()

    def test_corrupted_cache_refetched_instead_of_used(self):
        with patch.object(self.provider, "_download", side_effect=self.download):
            self.provider.champion_for("157")
        cached = self.provider._read(URL)
        cached["content"] = detail_html().replace(URL, INDEX_URL + "/lux")
        self.provider._write(URL, cached)
        restarted = ArenaProvider(Path(self.temp.name))
        self.calls.clear()
        with patch.object(restarted, "_download", side_effect=self.download):
            restarted.champion_for("157")
        self.assertEqual(self.calls, [URL])

    def test_http_failure_classification(self):
        for code, transient in ((403, False), (404, False), (429, True), (503, True)):
            with patch("mayhem.arena_data._OPENER.open", side_effect=HTTPError(URL, code, "error", {}, None)):
                with self.assertRaises(DataUnavailableError) as caught:
                    self.provider._download(URL)
                self.assertEqual(caught.exception.transient, transient)


if __name__ == "__main__":
    unittest.main()
