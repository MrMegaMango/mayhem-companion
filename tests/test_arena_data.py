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
ALTERNATIVE_URL = "https://lolalytics.com/lol/yasuo/arena/build/?tier=all&patch=16.21"
ROLLING_URL = "https://lolalytics.com/lol/yasuo/arena/build/?tier=all&patch=30"
RANKING_URL = "https://lolalytics.com/lol/tierlist/arena/?tier=all&patch=16.21"
ROLLING_RANKING_URL = "https://lolalytics.com/lol/tierlist/arena/?tier=all&patch=30"


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


def alternative_snapshot(*, augment_samples=(2000,), item_samples=(3000,), version="16.21", stale=False):
    return {"source": "Lolalytics", "source_url": ALTERNATIVE_URL, "version": version,
            "champion_id": "157", "champion_name": "Yasuo", "stale": stale,
            "sample_label": "Arena · all ranks · all regions · queue unspecified",
            "summary": {"sample_count": 20000},
            "augments": [{"id": str(43 + i), "win_rate": .61, "sample_count": samples}
                         for i, samples in enumerate(augment_samples)],
            "items": [{"id": str(223031 + i), "win_rate": .52, "sample_count": samples}
                      for i, samples in enumerate(item_samples)]}


def ranking_snapshot(*, counts=(20000, 15000), version="16.21", stale=False, window="current"):
    rows = [{**NAMES[ident], "champion_id": ident, "win_rate": .51, "sample_count": samples,
             "missing": not bool(samples), "low_sample": samples < 200}
            for ident, samples in zip(("157", "99"), counts)]
    return {"source": "Lolalytics", "source_url": ROLLING_RANKING_URL if window == "30" else RANKING_URL,
            "version": None if window == "30" else version, "source_patch": None if window == "30" else version,
            "asset_patch": version, "data_window": window, "stale": stale,
            "sample_label": "Arena · all ranks · all regions", "rows": rows,
            **({"window_days": 30} if window == "30" else {})}


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

    def test_matching_observed_name_keeps_reference_rarity_when_source_omits_it(self):
        observed = {"2107": {"id": "2107", "name": "Hellbent", "rarity": "", "description": "Source text"}}
        reference = {"2107": {"id": "2107", "name": "Hellbent", "rarity": "prismatic"}}
        merged = _merge_names(observed, reference)
        self.assertEqual(merged["2107"]["rarity"], "prismatic")
        self.assertEqual(merged["2107"]["description"], "Source text")
        self.assertEqual(observed["2107"]["rarity"], "")
        reference["2107"]["name"] = "Another augment"
        self.assertEqual(_merge_names(observed, reference)["2107"]["rarity"], "")

    def test_observed_id_wins_conflicting_reference_name(self):
        observed = {"99": {"id": "99", "name": "Goliath", "description": "Source text"}}
        merged = _merge_names(observed, {"41": {"id": "41", "name": "Goliath"}})
        self.assertEqual(merged, observed)


class ArenaProviderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.provider = ArenaProvider(Path(self.temp.name))
        self.fallback_patch = patch.object(self.provider, "_fallback_champion", return_value=None)
        self.fallback = self.fallback_patch.start()
        self.addCleanup(self.fallback_patch.stop)
        self.ranking_patch = patch.object(self.provider, "_ranking_catalog", return_value=None)
        self.ranking = self.ranking_patch.start()
        self.addCleanup(self.ranking_patch.stop)
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
        with patch.object(restarted, "_fallback_champion", return_value=None), \
                patch.object(restarted, "_ranking_catalog", return_value=None), \
                patch.object(restarted, "_download", side_effect=AssertionError("unexpected download")):
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

    def test_champion_omitted_from_index_loads_validated_detail_page(self):
        url = INDEX_URL + "/lux"
        self.payloads[url] = detail_html(augments="").replace("Yasuo", "Lux").replace(URL, url)
        with patch.object(self.provider, "_download", side_effect=self.download):
            result = self.provider.champion_for("99")
            self.assertEqual(result["champion_id"], "99")
            self.assertEqual(result["champion_name"], "Lux")
            self.assertEqual(result["source_url"], url)
            self.assertEqual(result["summary"]["sample_count"], 31)
            self.assertEqual(result["augments"], [])
            self.assertEqual(len(result["items"]), 1)
            self.assertTrue(result["items"][0]["low_sample"])
            self.provider.champion_for("99")
            self.assertEqual(self.calls, [VERSIONS_URL, self.resource, INDEX_URL, url])

    def test_unlisted_route_uses_internal_name_and_still_validates_display_name(self):
        champions = copy.deepcopy(DDRAGON)
        champions["data"]["MonkeyKing"] = {"key": "62", "id": "MonkeyKing", "name": "Wukong"}
        self.payloads[self.resource] = json.dumps(champions)
        url = INDEX_URL + "/monkeyking"
        self.payloads[url] = detail_html().replace("Yasuo", "Wukong").replace(URL, url)
        with patch.object(self.provider, "_download", side_effect=self.download):
            result = self.provider.champion_for("62")
            self.assertEqual(result["champion_name"], "Wukong")
            self.assertEqual(self.calls[-1], url)

    def test_unlisted_detail_rejects_wrong_champion_mode_or_canonical(self):
        url = INDEX_URL + "/lux"
        valid = detail_html().replace("Yasuo", "Lux").replace(URL, url)
        with patch.object(self.provider, "_download", side_effect=self.download):
            for html in (detail_html(), valid.replace("<h1>Lux</h1>", "<h1>Yasuo</h1>"),
                         valid.replace("Arena Stats", "ARAM Stats")):
                with self.subTest(html=html[:150]):
                    self.payloads[url] = html
                    with self.assertRaises(DataUnavailableError):
                        self.provider.champion_for("99", force=True)

    def test_unlisted_detail_request_failure_is_not_reported_as_empty_data(self):
        url = INDEX_URL + "/lux"
        for error in (DataUnavailableError("Arena source request failed (HTTP 404)"),
                      DataUnavailableError("offline", transient=True)):
            with self.subTest(error=str(error)):
                self.payloads[url] = error
                with patch.object(self.provider, "_download", side_effect=self.download):
                    with self.assertRaises(DataUnavailableError) as raised:
                        self.provider.champion_for("99", force=True)
                    self.assertIs(raised.exception, error)

    def test_rejects_arbitrary_ids_before_network(self):
        with patch.object(self.provider, "_download") as download:
            for ident in (True, None, "../157", "157?mode=aram", "0", "-1", 1.5):
                with self.assertRaises(ValueError):
                    self.provider.champion_for(ident)
            download.assert_not_called()

    def test_empty_augments_uses_whole_fallback_snapshot(self):
        self.payloads[URL] = detail_html(augments="")
        snapshot = alternative_snapshot()
        self.fallback.return_value = snapshot
        with patch.object(self.provider, "_download", side_effect=self.download):
            result = self.provider.champion_for("157", force=True)
        self.assertEqual(result, snapshot)
        self.fallback.assert_called_once()
        self.assertTrue(self.fallback.call_args.kwargs["force"])
        self.assertEqual(self.fallback.call_args.args[0]["id"], "157")

    def test_primary_failure_uses_available_champion_fallback(self):
        self.payloads[URL] = DataUnavailableError("offline", transient=True)
        snapshot = alternative_snapshot()
        self.fallback.return_value = snapshot
        with patch.object(self.provider, "_download", side_effect=self.download):
            self.assertEqual(self.provider.champion_for("157"), snapshot)

    def test_nonempty_primary_still_checks_alternative(self):
        with patch.object(self.provider, "_download", side_effect=self.download):
            self.assertEqual(self.provider.champion_for("157")["source"], "winrate.lol")
        self.fallback.assert_called_once()

    def test_empty_fallback_does_not_discard_primary_items(self):
        self.payloads[URL] = detail_html(augments="")
        self.fallback.return_value = alternative_snapshot(augment_samples=(), item_samples=())
        with patch.object(self.provider, "_download", side_effect=self.download):
            result = self.provider.champion_for("157")
        self.assertEqual(result["source"], "winrate.lol")
        self.assertEqual(len(result["items"]), 1)

    def lookup_alternative(self, snapshot, primary=None, *, force=True):
        self.fallback.return_value = snapshot
        if primary is not None:
            self.payloads[URL] = primary
        with patch.object(self.provider, "_download", side_effect=self.download):
            return self.provider.champion_for("157", force=force)

    def test_sparse_nonempty_primary_uses_complete_better_covered_snapshot(self):
        snapshot = alternative_snapshot(augment_samples=(2000, 400), item_samples=(3000, 900))
        snapshot["items"][0]["id"] = "443090"
        result = self.lookup_alternative(snapshot)
        self.assertEqual(result, snapshot)
        self.assertEqual(result["source_url"], ALTERNATIVE_URL)
        self.assertNotIn("223031", {row["id"] for row in result["items"]})

    def test_more_supported_augments_outweigh_more_tiny_observations(self):
        primary = detail_html(augments=observation(count="200"))
        snapshot = alternative_snapshot(augment_samples=(1,) * 30)
        self.assertEqual(self.lookup_alternative(snapshot, primary)["source"], "winrate.lol")

    def test_better_covered_primary_is_retained(self):
        primary = detail_html(augments=observation(count="200") +
                              observation(ident="44", name="Second augment", count="200"))
        self.assertEqual(self.lookup_alternative(alternative_snapshot(), primary)["source"], "winrate.lol")

    def test_broader_coverage_wins_when_both_sources_have_small_samples(self):
        snapshot = alternative_snapshot(augment_samples=(8, 9), item_samples=(12,))
        self.assertEqual(self.lookup_alternative(snapshot), snapshot)

    def test_more_usable_item_data_breaks_equal_augment_coverage(self):
        snapshot = alternative_snapshot(augment_samples=(12,), item_samples=(200,))
        self.assertEqual(self.lookup_alternative(snapshot), snapshot)

    def test_more_item_choices_break_equal_supported_row_counts(self):
        snapshot = alternative_snapshot(augment_samples=(12,), item_samples=(8, 9))
        self.assertEqual(self.lookup_alternative(snapshot), snapshot)

    def test_better_sample_support_breaks_equal_coverage(self):
        for augments, items in (((13,), (12,)), ((12,), (13,))):
            with self.subTest(augment_samples=augments, item_samples=items):
                snapshot = alternative_snapshot(augment_samples=augments, item_samples=items)
                snapshot["summary"]["sample_count"] = 31
                self.assertEqual(self.lookup_alternative(snapshot), snapshot)

    def test_exact_coverage_tie_keeps_primary_regardless_of_win_rates(self):
        for rate in (0., 1.):
            with self.subTest(rate=rate):
                snapshot = alternative_snapshot(augment_samples=(12,), item_samples=(12,))
                snapshot["summary"]["sample_count"] = 31
                snapshot["augments"][0]["win_rate"] = rate
                self.assertEqual(self.lookup_alternative(snapshot)["source"], "winrate.lol")

    def test_one_extremely_popular_row_cannot_dominate_sample_support(self):
        primary = detail_html(augments=observation(count="2000"))
        snapshot = alternative_snapshot(augment_samples=(1000000,), item_samples=(12,))
        snapshot["summary"]["sample_count"] = 31
        self.assertEqual(self.lookup_alternative(snapshot, primary)["source"], "winrate.lol")

    def test_champion_sample_breaks_otherwise_equal_coverage(self):
        snapshot = alternative_snapshot(augment_samples=(12,), item_samples=(12,))
        self.assertEqual(self.lookup_alternative(snapshot), snapshot)

    def test_zero_percent_with_real_samples_is_usable(self):
        snapshot = alternative_snapshot()
        snapshot["augments"][0]["win_rate"] = 0.
        self.assertEqual(self.lookup_alternative(snapshot), snapshot)

    def test_missing_invalid_or_unobserved_rows_do_not_inflate_coverage(self):
        for field, value in (("win_rate", None), ("win_rate", float("nan")),
                             ("win_rate", float("inf")), ("win_rate", -0.1),
                             ("win_rate", 1.1), ("win_rate", "0.5"), ("win_rate", True),
                             ("sample_count", 0), ("sample_count", -1)):
            with self.subTest(field=field, value=value):
                snapshot = alternative_snapshot(augment_samples=(12, 5000), item_samples=(12,))
                snapshot["summary"]["sample_count"] = 31
                snapshot["augments"][1][field] = value
                self.assertEqual(self.lookup_alternative(snapshot)["source"], "winrate.lol")

    def test_alternative_cannot_erase_an_existing_category_on_same_patch(self):
        for augments, items in (((5000,) * 20, ()), ((), (5000,) * 20)):
            with self.subTest(augments=bool(augments)):
                snapshot = alternative_snapshot(augment_samples=augments, item_samples=items)
                self.assertEqual(self.lookup_alternative(snapshot)["source"], "winrate.lol")

    def test_newer_patch_wins_without_comparing_sample_counts(self):
        primary = detail_html(augments=observation(count="5000"),
                              items=observation("item", "223031", "Infinity Edge", count="5000"))
        snapshot = alternative_snapshot(augment_samples=(1,), item_samples=(1,), version="16.22")
        self.assertEqual(self.lookup_alternative(snapshot, primary), snapshot)

    def test_fresh_empty_current_patch_does_not_revive_previous_patch_observations(self):
        primary = detail_html(augments="", items="").replace("26.21", "26.23")
        snapshot = alternative_snapshot(augment_samples=(5000,) * 20, version="16.22")
        result = self.lookup_alternative(snapshot, primary)
        self.assertEqual(result["source"], "winrate.lol")
        self.assertEqual(result["version"], "26.23")
        self.assertEqual(result["augments"], [])
        self.assertEqual(result["items"], [])
        self.assertFalse(result["stale"])

    def test_newer_empty_alternative_replaces_older_fresh_primary(self):
        snapshot = alternative_snapshot(augment_samples=(), item_samples=(), version="16.22")
        self.assertEqual(self.lookup_alternative(snapshot), snapshot)

    def test_older_patch_never_displaces_newer_primary_for_more_samples(self):
        snapshot = alternative_snapshot(augment_samples=(5000,) * 20, version="16.20")
        self.assertEqual(self.lookup_alternative(snapshot)["source"], "winrate.lol")

    def test_stale_alternative_does_not_displace_fresh_primary(self):
        self.assertEqual(self.lookup_alternative(alternative_snapshot(stale=True))["source"], "winrate.lol")

    def test_fresh_alternative_replaces_larger_stale_same_patch_snapshot(self):
        primary = detail_html(augments=observation(count="5000"))
        self.lookup_alternative(None, primary)
        self.now.return_value = 5000.
        self.payloads[URL] = DataUnavailableError("offline", transient=True)
        snapshot = alternative_snapshot(augment_samples=(1,), item_samples=(1,))
        self.assertEqual(self.lookup_alternative(snapshot), snapshot)

    def real_alternative(self):
        self.fallback_patch.stop()
        self.payloads[ALTERNATIVE_URL] = "alternative page"
        snapshot = alternative_snapshot()
        return patch("mayhem.arena_lolalytics.parse_champion",
                     side_effect=lambda *args, **kwargs: copy.deepcopy(snapshot))

    def test_real_alternative_lookup_uses_memory_and_disk_cache(self):
        with self.real_alternative() as parse, \
                patch.object(self.provider, "_download", side_effect=self.download):
            first = self.provider.champion_for("157")
            self.assertEqual(first["source"], "Lolalytics")
            first["augments"].clear()
            self.assertEqual(len(self.provider.champion_for("157")["augments"]), 1)
            self.assertEqual(self.calls, [VERSIONS_URL, self.resource, INDEX_URL, URL, ALTERNATIVE_URL])
            restarted = ArenaProvider(Path(self.temp.name))
            with patch.object(restarted, "_ranking_catalog", return_value=None), \
                    patch.object(restarted, "_download", side_effect=AssertionError("unexpected download")):
                self.assertEqual(restarted.champion_for("157")["source"], "Lolalytics")
            self.assertEqual(parse.call_args.kwargs["expected_patch"], "26.21")

    def test_failed_alternative_keeps_primary_and_retries_after_cooldown(self):
        with self.real_alternative(), patch.object(self.provider, "_download", side_effect=self.download):
            self.payloads[ALTERNATIVE_URL] = DataUnavailableError("offline", transient=True)
            self.assertEqual(self.provider.champion_for("157")["source"], "winrate.lol")
            self.assertEqual(self.provider.champion_for("157")["source"], "winrate.lol")
            self.assertEqual(self.calls.count(ALTERNATIVE_URL), 1)
            self.now.return_value = 1061.
            self.payloads[ALTERNATIVE_URL] = "alternative page"
            self.assertEqual(self.provider.champion_for("157")["source"], "Lolalytics")
            self.assertEqual(self.calls.count(ALTERNATIVE_URL), 2)

    def test_force_refresh_bypasses_alternative_error_cooldown(self):
        with self.real_alternative(), patch.object(self.provider, "_download", side_effect=self.download):
            self.payloads[ALTERNATIVE_URL] = DataUnavailableError("offline", transient=True)
            self.assertEqual(self.provider.champion_for("157")["source"], "winrate.lol")
            self.payloads[ALTERNATIVE_URL] = "alternative page"
            self.assertEqual(self.provider.champion_for("157", force=True)["source"], "Lolalytics")
            self.assertEqual(self.calls.count(ALTERNATIVE_URL), 2)
            self.assertEqual(self.calls.count(URL), 2)

    def test_corrupted_cache_refetched_instead_of_used(self):
        with patch.object(self.provider, "_download", side_effect=self.download):
            self.provider.champion_for("157")
        cached = self.provider._read(URL)
        cached["content"] = detail_html().replace(URL, INDEX_URL + "/lux")
        self.provider._write(URL, cached)
        restarted = ArenaProvider(Path(self.temp.name))
        self.calls.clear()
        with patch.object(restarted, "_fallback_champion", return_value=None), \
                patch.object(restarted, "_ranking_catalog", return_value=None), \
                patch.object(restarted, "_download", side_effect=self.download):
            restarted.champion_for("157")
        self.assertEqual(self.calls, [URL])

    def test_better_covered_rankings_keep_source_rows_and_identity_resources(self):
        alternative = ranking_snapshot()
        self.ranking.return_value = alternative
        with patch.object(self.provider, "_download", side_effect=self.download):
            result = self.provider.load()
        self.assertEqual(result["source"], "Lolalytics")
        self.assertEqual(result["rows"], alternative["rows"])
        self.assertEqual(result["source_url"], alternative["source_url"])
        self.assertEqual(set(result["champions"]), {"157", "99"})
        self.assertEqual(result["champions"]["157"]["slug"], "yasuo")
        self.assertEqual(result["augments"]["43"]["name"], "Now You See Me")
        self.assertNotIn("win_rate", result["augments"]["43"])
        self.assertGreater(len(result["augments"]), 400)
        self.assertEqual(result["data_window"], "current")

    def test_primary_rankings_win_on_coverage_tie_independent_of_rates(self):
        alternative = ranking_snapshot(counts=(1240,))
        alternative["rows"][0]["win_rate"] = 1.
        self.ranking.return_value = alternative
        with patch.object(self.provider, "_download", side_effect=self.download):
            self.assertEqual(self.provider.load()["source"], "winrate.lol")

    def test_rankings_prefer_supported_rows_over_more_small_samples(self):
        self.ranking.return_value = ranking_snapshot(counts=(12, 12))
        with patch.object(self.provider, "_download", side_effect=self.download):
            self.assertEqual(self.provider.load()["source"], "winrate.lol")

    def test_rankings_compare_patch_before_sample_coverage(self):
        with patch.object(self.provider, "_download", side_effect=self.download):
            self.ranking.return_value = ranking_snapshot(version="16.20")
            self.assertEqual(self.provider.load()["source"], "winrate.lol")
            self.ranking.return_value = ranking_snapshot(version="16.22", counts=())
            latest = self.provider.load()
            self.assertEqual(latest["source"], "Lolalytics")
            self.assertEqual(latest["version"], "16.22")
            self.assertEqual(latest["rows"], [])

    def test_rankings_keep_fresh_primary_over_larger_stale_alternative(self):
        self.ranking.return_value = ranking_snapshot(stale=True)
        with patch.object(self.provider, "_download", side_effect=self.download):
            self.assertEqual(self.provider.load()["source"], "winrate.lol")

    def test_rankings_use_fresh_alternative_over_larger_stale_primary(self):
        with patch.object(self.provider, "_download", side_effect=self.download):
            self.provider.load()
            self.now.return_value = 5000.
            self.payloads[INDEX_URL] = DataUnavailableError("offline", transient=True)
            self.ranking.return_value = ranking_snapshot(counts=(1,))
            result = self.provider.load()
            self.assertEqual(result["source"], "Lolalytics")
            self.assertEqual(result["rows"][0]["sample_count"], 1)

    def test_alternative_rankings_work_when_primary_index_fails(self):
        self.payloads[INDEX_URL] = DataUnavailableError("offline", transient=True)
        self.ranking.return_value = ranking_snapshot()
        with patch.object(self.provider, "_download", side_effect=self.download):
            result = self.provider.load()
            detail = self.provider.champion_for("157")
        self.assertEqual(result["source"], "Lolalytics")
        self.assertEqual(result["champions"], NAMES)
        self.assertEqual(result["augments"]["41"]["name"], "Goliath")
        self.assertEqual(detail["champion_id"], "157")
        self.assertEqual(self.calls.count(INDEX_URL), 1)

    def test_failed_rankings_preserve_original_primary_error(self):
        error = DataUnavailableError("primary denied")
        self.payloads[INDEX_URL] = error
        with patch.object(self.provider, "_download", side_effect=self.download):
            with self.assertRaises(DataUnavailableError) as raised:
                self.provider.load()
        self.assertIs(raised.exception, error)

    def test_rolling_catalog_never_fetches_primary_and_keeps_reference_names(self):
        self.ranking.return_value = ranking_snapshot(window="30")
        with patch.object(self.provider, "_download", side_effect=self.download):
            result = self.provider.load(force=True, window="30")
        self.assertEqual(self.calls, [VERSIONS_URL, self.resource])
        self.assertIsNone(result["version"])
        self.assertEqual(result["window_days"], 30)
        self.assertEqual(result["champions"], NAMES)
        self.assertGreater(len(result["augments"]), 400)
        self.ranking.assert_called_once()
        self.assertEqual(self.ranking.call_args.args[1], "16.21")
        self.assertEqual(self.ranking.call_args.kwargs, {"force": True, "window": "30"})

    def test_failed_rolling_rankings_do_not_reuse_current_catalog(self):
        with patch.object(self.provider, "_download", side_effect=self.download):
            self.provider.load()
            with self.assertRaisesRegex(DataUnavailableError, "30-day champion rankings"):
                self.provider.load(window="30")
        self.assertEqual(self.calls.count(INDEX_URL), 1)

    def test_failed_rolling_champion_does_not_reuse_current_statistics(self):
        with patch.object(self.provider, "_download", side_effect=self.download):
            self.provider.champion_for("157")
            before = list(self.calls)
            self.ranking.return_value = ranking_snapshot(window="30")
            with self.assertRaisesRegex(DataUnavailableError, "30-day champion statistics"):
                self.provider.champion_for("157", window="30", force=True)
        self.assertEqual(self.calls, before)
        self.assertEqual(self.fallback.call_args.kwargs, {"force": True, "window": "30"})

    def test_rolling_and_current_champion_caches_are_separate(self):
        self.fallback_patch.stop()
        self.ranking.side_effect = lambda names, patch, **kw: ranking_snapshot(window=kw["window"])
        self.payloads.update({ALTERNATIVE_URL: "current page", ROLLING_URL: "rolling page"})
        def parse(html, champion, url, augments, **kwargs):
            result = alternative_snapshot()
            result.update(source_url=url, data_window=kwargs["window"], asset_patch="16.21")
            if kwargs["window"] == "30":
                result.update(version=None, source_patch=None, window_days=30)
                result["summary"]["sample_count"] = 70000
            return result
        with patch("mayhem.arena_lolalytics.parse_champion", side_effect=parse) as parser, \
                patch.object(self.provider, "_download", side_effect=self.download):
            current = self.provider.champion_for("157")
            rolling = self.provider.champion_for("157", window="30")
            self.assertEqual(current["summary"]["sample_count"], 20000)
            self.assertEqual(rolling["summary"]["sample_count"], 70000)
            self.assertEqual(rolling["data_window"], "30")
            self.assertEqual(self.provider.champion_for("157"), current)
            self.assertEqual(self.provider.champion_for("157", window="30"), rolling)
            self.assertEqual(self.calls.count(ALTERNATIVE_URL), 1)
            self.assertEqual(self.calls.count(ROLLING_URL), 1)
            self.provider.champion_for("157", window="30", force=True)
            self.assertEqual(self.calls.count(ROLLING_URL), 2)
            self.assertEqual(self.calls.count(ALTERNATIVE_URL), 1)
            self.assertEqual(parser.call_args.kwargs["window"], "30")

    def test_real_ranking_catalog_caches_each_window_separately(self):
        self.ranking_patch.stop()
        self.payloads.update({RANKING_URL: "current rankings", ROLLING_RANKING_URL: "rolling rankings"})
        def parse(html, names, url, **kwargs):
            return ranking_snapshot(window=kwargs["window"])
        with patch("mayhem.arena_rankings.parse_rankings", side_effect=parse) as parser, \
                patch.object(self.provider, "_download", side_effect=self.download):
            current = self.provider.load()
            rolling = self.provider.load(window="30")
            self.assertEqual(current["data_window"], "current")
            self.assertEqual(rolling["data_window"], "30")
            self.provider.load()
            self.provider.load(window="30")
            self.assertEqual(self.calls.count(RANKING_URL), 1)
            self.assertEqual(self.calls.count(ROLLING_RANKING_URL), 1)
            self.provider.load(force=True, window="30")
            self.assertEqual(self.calls.count(ROLLING_RANKING_URL), 2)
            self.assertEqual(self.calls.count(RANKING_URL), 1)
            self.assertEqual(parser.call_args.kwargs, {"expected_patch": "16.21", "window": "30"})
            restarted = ArenaProvider(Path(self.temp.name))
            with patch.object(restarted, "_download", side_effect=AssertionError("unexpected download")):
                self.assertEqual(restarted.load(window="30")["data_window"], "30")

    def test_rejected_rolling_ranking_page_never_silently_returns_current(self):
        self.ranking_patch.stop()
        self.payloads[ROLLING_RANKING_URL] = "bad rolling rankings"
        with patch("mayhem.arena_rankings.parse_rankings", side_effect=DataUnavailableError("wrong mode")), \
                patch.object(self.provider, "_download", side_effect=self.download):
            with self.assertRaisesRegex(DataUnavailableError, "30-day champion rankings"):
                self.provider.load(window="30")
        self.assertNotIn(INDEX_URL, self.calls)

    def test_http_failure_classification(self):
        for code, transient in ((403, False), (404, False), (429, True), (503, True)):
            with patch("mayhem.arena_data._OPENER.open", side_effect=HTTPError(URL, code, "error", {}, None)):
                with self.assertRaises(DataUnavailableError) as caught:
                    self.provider._download(URL)
                self.assertEqual(caught.exception.transient, transient)


if __name__ == "__main__":
    unittest.main()
