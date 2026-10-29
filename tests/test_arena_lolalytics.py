import json
import unittest

from mayhem.arena_lolalytics import MAX_BYTES, MAX_OBJECTS, MIN_ID_ANCHORS, champion_url, parse_champion
from mayhem.data import DataUnavailableError


CHAMPION = {"id": "39", "name": "Irelia", "raw_name": "Irelia"}
URL = "https://lolalytics.com/lol/irelia/arena/build/?tier=all&patch=16.21"
CANONICAL = "https://lolalytics.com/lol/irelia/arena/build/?tier=all"
REFERENCE = {"9001": {"id": "9001", "name": "Mystic Punch"},
             "58": {"id": "58", "name": "Other Augment"}}


def fixture():
    return {
        "navigation": {"path": "/lol/irelia/arena/build/", "cid": 39,
                       "champion": "irelia", "champName": "Irelia", "lang": "en",
                       "page": "arena", "mode": "arena", "patch": "16.21",
                       "tier": "all", "region": "all", "status": 200},
        "catalog": {"patch": "16.21", "champions": {"irelia": "Irelia"},
                    "champPath": {"39": "irelia"},
                    "augments": {"58": ["Mystic Punch", 2], "63": ["Unknown Augment", 1]},
                    "items": {"223153": "Blade of The Ruined King", "443090": "Reaper's Toll"}},
        "statistics": {
            "header": {"cid": 39, "n": 21871, "wr": 48.15, "place1": 13.5},
            "augment": {"augment0": [[58, 61.04, 19.01, 4158], [63, 54.14, 17.50, 3827],
                                     [0, 15.81, 38.4, 8398]],
                        "augment1": [[58, 99.99, 4.0, 100]]},
            "prismatic": [[443090, 54.15, 19.79, 4329]],
            "item": [[223153, 53.55, 49.63, 10854, 9], [443090, 66.51, 0.97, 212, 13]],
            "response": {"valid": True, "duration": "29"}},
    }


def base36(number):
    if number == 0:
        return "0"
    result = ""
    while number:
        number, digit = divmod(number, 36)
        result = "0123456789abcdefghijklmnopqrstuvwxyz"[digit] + result
    return result


def snapshot(data):
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
    encode(data)
    return {"objs": objects, "refs": {}, "ctx": {}, "subs": []}


def page(data=None, *, canonical=CANONICAL, transform=None):
    encoded = snapshot(fixture() if data is None else data)
    if transform:
        transform(encoded)
    return (f'<html><head><link rel="canonical" href="{canonical}"></head>'
            '<body><script type="qwik/json">' + json.dumps(encoded) + '</script></body></html>')


def anchored_fixture(anchor_count=MIN_ID_ANCHORS):
    data = fixture()
    names = {"58": ["Mystic Punch", 2]}
    reference = {"58": {"id": "58", "name": "Mystic Punch", "rarity": "prismatic"},
                 "1344": {"id": "1344", "name": "Final Form", "rarity": "prismatic"}}
    for i in range(anchor_count - 1):
        ident, name = str(10000 + i), f"Anchor {i}"
        names[ident] = [name, 1]
        reference[ident] = {"id": ident, "name": name, "rarity": "gold"}
    data["catalog"]["augments"] = names
    data["statistics"]["augment"]["augment0"] = [
        [58, 61.04, 19.01, 4158], [1344, 56.09, 3.83, 838],
        [0, 15.81, 38.4, 8398], [999999, 77, 0.1, 5]]
    return data, reference


class LolalyticsArenaTests(unittest.TestCase):
    def parse(self, html=None, reference=None, **kwargs):
        return parse_champion(html or page(), CHAMPION, URL,
                              REFERENCE if reference is None else reference, **kwargs)

    def test_aggregate_champion_rates_map_names_not_numeric_ids(self):
        result = self.parse(expected_patch="26.21")
        self.assertEqual(result["source"], "Lolalytics")
        self.assertEqual(result["version"], "16.21")
        self.assertEqual(result["champion_id"], "39")
        self.assertEqual(result["summary"]["sample_count"], 21871)
        self.assertAlmostEqual(result["summary"]["win_rate"], .4815)
        self.assertIsNone(result["summary"]["first_rate"])
        self.assertIsNone(result["summary"]["avg_placement"])
        self.assertEqual(len(result["augments"]), 1)
        augment = result["augments"][0]
        self.assertEqual(augment["id"], "9001")
        self.assertEqual(augment["name"], "Mystic Punch")
        self.assertAlmostEqual(augment["win_rate"], .6104)
        self.assertEqual(augment["sample_count"], 4158)
        self.assertEqual(augment["rarity"], "prismatic")
        self.assertIsNone(augment["icon_url"])
        self.assertIn("queue unspecified", result["sample_label"])
        self.assertIn("finish cutoff not specified", result["rate_note"])

    def test_preserves_prismatic_observation_without_averaging_later_purchase(self):
        result = self.parse()
        self.assertEqual(len(result["items"]), 2)
        prism = next(row for row in result["items"] if row["id"] == "443090")
        self.assertAlmostEqual(prism["win_rate"], .5415)
        self.assertEqual(prism["sample_count"], 4329)
        self.assertEqual(prism["category"], "prismatic")
        self.assertIsNone(prism["icon_url"])

    def test_unknown_and_ambiguous_names_are_omitted(self):
        self.assertEqual(self.parse(reference={})["augments"], [])
        duplicate = {**REFERENCE, "9002": {"id": "9002", "name": "Mystic-Punch"}}
        self.assertEqual(self.parse(reference=duplicate)["augments"], [])

    def test_only_observed_allowlisted_item_icons_are_attached(self):
        html = page().replace("<body>", '<body><img src="https://cdn5.lolalytics.com/item64/443090.webp">')
        items = self.parse(html)["items"]
        self.assertEqual(items[0]["icon_url"], "https://cdn5.lolalytics.com/item64/443090.webp")
        self.assertIsNone(items[1]["icon_url"])
        html = page().replace("<body>", '<body><img src="https://evil.invalid/item64/443090.webp">')
        self.assertIsNone(self.parse(html)["items"][0]["icon_url"])

    def test_zero_samples_are_unknown_rates(self):
        data = fixture()
        data["statistics"]["header"].update(n=0, wr=0)
        data["statistics"]["augment"]["augment0"][0][3] = 0
        result = self.parse(page(data))
        self.assertIsNone(result["summary"]["win_rate"])
        self.assertIsNone(result["augments"][0]["win_rate"])
        self.assertTrue(result["augments"][0]["low_sample"])

    def test_rejects_wrong_champion_mode_and_sample_filters(self):
        for key, value in (("cid", 157), ("champName", "Yasuo"), ("mode", "aram"),
                           ("page", "build"), ("tier", "emerald_plus"), ("region", "na"),
                           ("lang", "ko"), ("status", 404), ("status", True)):
            data = fixture()
            data["navigation"][key] = value
            with self.subTest(key=key), self.assertRaises(DataUnavailableError):
                self.parse(page(data))
        data = fixture()
        data["statistics"]["header"]["cid"] = 157
        with self.assertRaises(DataUnavailableError):
            self.parse(page(data))

    def test_rejects_incorrect_patch_and_catalog_patch(self):
        with self.assertRaises(DataUnavailableError):
            self.parse(expected_patch="26.20")
        for section in ("navigation", "catalog"):
            data = fixture()
            data[section]["patch"] = "16.20"
            with self.subTest(section=section), self.assertRaises(DataUnavailableError):
                self.parse(page(data))
        self.parse(expected_patch="16.21")

    def test_rejects_untrusted_canonical_and_inconsistent_champion_route(self):
        for canonical in (CANONICAL.replace("irelia", "yasuo"), CANONICAL.replace("arena", "aram"),
                          CANONICAL.replace("lolalytics.com", "evil.invalid")):
            with self.subTest(canonical=canonical), self.assertRaises(DataUnavailableError):
                self.parse(page(canonical=canonical))
        data = fixture()
        data["catalog"]["champPath"]["39"] = "yasuo"
        with self.assertRaises(DataUnavailableError):
            self.parse(page(data))

    def test_rejects_invalid_numeric_data(self):
        for index, value in ((0, -1), (1, 100.01), (1, float("nan")), (2, -3),
                             (3, -1), (3, 1.5), (3, True), (3, 1_000_000_001)):
            data = fixture()
            data["statistics"]["augment"]["augment0"][0][index] = value
            with self.subTest(index=index, value=value), self.assertRaises(DataUnavailableError):
                self.parse(page(data))

    def test_rejects_duplicate_observations_and_columns(self):
        data = fixture()
        data["statistics"]["augment"]["augment0"].append([58, 40, 10, 100])
        with self.assertRaises(DataUnavailableError):
            self.parse(page(data))
        data = fixture()
        data["statistics"]["augment"]["augment0"][0].append(50)
        with self.assertRaises(DataUnavailableError):
            self.parse(page(data))

    def test_rejects_two_source_ids_mapping_to_same_reference(self):
        data = fixture()
        data["catalog"]["augments"]["63"] = ["Mystic Punch", 2]
        with self.assertRaises(DataUnavailableError):
            self.parse(page(data))

    def test_invalid_missing_and_circular_references(self):
        def transform(ref):
            def run(encoded):
                nav = next(obj for obj in encoded["objs"] if isinstance(obj, dict) and "champName" in obj)
                nav["cid"] = ref
            return run
        for ref in ("zzzzzz", "-1", "12!", 12):
            with self.subTest(ref=ref), self.assertRaises(DataUnavailableError):
                self.parse(page(transform=transform(ref)))
        def circular(encoded):
            for index, obj in enumerate(encoded["objs"]):
                if isinstance(obj, dict) and "champName" in obj:
                    obj["cid"] = base36(index)
                    break
        with self.assertRaises(DataUnavailableError):
            self.parse(page(transform=circular))

    def test_rejects_missing_ambiguous_and_oversized_snapshots(self):
        html = page()
        for bad in ("<html></html>", html.replace('</body>', '<script type="qwik/json">{}</script></body>'),
                    "x" * (MAX_BYTES + 1)):
            with self.subTest(length=len(bad)), self.assertRaises(DataUnavailableError):
                self.parse(bad)
        def oversized(encoded):
            encoded["objs"] = [None] * (MAX_OBJECTS + 1)
        with self.assertRaises(DataUnavailableError):
            self.parse(page(transform=oversized))

    def test_unrelated_qwik_nodes_are_not_decoded(self):
        def unrelated(encoded):
            encoded["objs"].append({"some_unrelated_component": "not-a-reference"})
        self.assertEqual(self.parse(page(transform=unrelated))["champion_id"], "39")

    def test_recovers_unlabelled_ids_only_after_full_catalog_alignment(self):
        data, reference = anchored_fixture()
        result = self.parse(page(data), reference=reference)
        rows = {row["id"]: row for row in result["augments"]}
        self.assertEqual(set(rows), {"58", "1344"})  # ID 0 and unknown IDs stay omitted.
        self.assertEqual(rows["1344"]["name"], "Final Form")
        self.assertAlmostEqual(rows["1344"]["win_rate"], .5609)
        self.assertEqual(rows["1344"]["sample_count"], 838)
        self.assertEqual(rows["1344"]["rarity"], "prismatic")

    def test_crafting_choice_labels_are_valid_identity_anchors(self):
        data, reference = anchored_fixture()
        data["catalog"]["augments"]["10000"][1] = 4
        rows = self.parse(page(data), reference=reference)["augments"]
        self.assertEqual({row["id"] for row in rows}, {"58", "1344"})

    def test_tiny_catalog_does_not_enable_numeric_recovery(self):
        data, reference = anchored_fixture(MIN_ID_ANCHORS - 1)
        self.assertEqual([row["id"] for row in self.parse(page(data), reference=reference)["augments"]], ["58"])

    def test_any_unobserved_anchor_conflict_disables_numeric_recovery(self):
        for change in ("unknown_name", "remapped_id", "duplicate_name", "ambiguous_reference", "bad_shape"):
            data, reference = anchored_fixture()
            if change == "unknown_name":
                data["catalog"]["augments"]["10000"][0] = "Unrecognized source name"
            elif change == "remapped_id":
                entry = reference.pop("10000")
                reference["50000"] = {**entry, "id": "50000"}
            elif change == "duplicate_name":
                data["catalog"]["augments"]["10000"][0] = "Anchor 1"
            elif change == "ambiguous_reference":
                reference["50000"] = {"id": "50000", "name": "Anchor 0", "rarity": "gold"}
            else:
                data["catalog"]["augments"]["10000"].append("unexpected")
            with self.subTest(change=change):
                self.assertEqual([row["id"] for row in self.parse(page(data), reference=reference)["augments"]], ["58"])

    def test_unlabelled_entry_requires_unique_name_identity_and_explicit_rarity(self):
        for change in ("ambiguous_name", "ambiguous_id", "missing_rarity", "invalid_rarity"):
            data, reference = anchored_fixture()
            if change == "ambiguous_name":
                reference["51344"] = {"id": "51344", "name": "Final-Form", "rarity": "prismatic"}
            elif change == "ambiguous_id":
                reference[1344] = {"id": "1344", "name": "Different Final Form", "rarity": "prismatic"}
            elif change == "missing_rarity":
                reference["1344"].pop("rarity")
            else:
                reference["1344"]["rarity"] = "unknown"
            with self.subTest(change=change):
                self.assertEqual([row["id"] for row in self.parse(page(data), reference=reference)["augments"]], ["58"])

    def test_champion_url_handles_internal_patch_and_verified_special_routes(self):
        self.assertEqual(champion_url(CHAMPION, "26.21"), URL)
        self.assertEqual(champion_url(CHAMPION, "16.21"), URL)
        for ident, raw, route in (("62", "MonkeyKing", "wukong"), ("20", "Nunu", "nunu"),
                                  ("31", "Chogath", "chogath")):
            self.assertIn(f"/lol/{route}/arena/build/", champion_url({"id": ident, "raw_name": raw}, "26.21"))
        for raw in ("../irelia", "https://evil.invalid", "", "Irelia?x=1"):
            with self.assertRaises(DataUnavailableError):
                champion_url({"id": "39", "raw_name": raw}, "26.21")


if __name__ == "__main__":
    unittest.main()
