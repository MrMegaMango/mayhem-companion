import unittest
from mayhem.matching import detect_offers, match_augments, normalize_title

CATALOG = {
    "stats": {"name": "Stats!"},
    "stats2": {"name": "Stats on Stats!"},
    "stats3": {"name": "Stats on Stats on Stats!"},
    "sword": {"name": "Draw Your Sword"},
    "basic": {"name": "Back To Basics"},
    "wind": {"name": "Windspeaker's Blessing"},
    "scoped": {"name": "Scoped Weapons"},
    "scopier": {"name": "Scopier Weapons"},
    "scopiest": {"name": "Scopiest Weapons"},
}


def line(text, x, y=200, height=24):
    return {"text": text, "x": x, "y": y, "width": 160, "height": height}


class MatchingTests(unittest.TestCase):
    def test_punctuation_and_case_normalization(self):
        results = match_augments(["WINDSPEAKER’S BLESSING"], CATALOG)
        self.assertEqual(results[0]["id"], "wind")
        self.assertEqual(results[0]["similarity"], 1)

    def test_matches_whole_title_not_shorter_stats_substring(self):
        self.assertEqual([m["id"] for m in match_augments(["Stats on Stats!"], CATALOG)], ["stats2"])
        self.assertEqual([m["id"] for m in match_augments(["Stats on Stats on Stats!"], CATALOG)], ["stats3"])

    def test_description_containing_title_is_rejected(self):
        self.assertEqual(match_augments(["Gain Stats on Stats! and other bonuses", "Draw Your Sword and gain power"], CATALOG), [])

    def test_small_ocr_error_is_accepted(self):
        results = match_augments(["Back To Baslcs"], CATALOG)
        self.assertEqual(results[0]["id"], "basic")
        self.assertGreater(results[0]["similarity"], .9)

    def test_short_fuzzy_titles_are_rejected(self):
        self.assertEqual(match_augments(["Stafs!"], CATALOG), [])

    def test_ambiguous_similar_names_are_rejected(self):
        self.assertEqual(match_augments(["Scoper Weapons"], CATALOG), [])

    def test_duplicate_normalized_catalog_names_are_ambiguous(self):
        self.assertEqual(match_augments(["Stats"], {1: "Stats!", 2: "Stats"}), [])

    def test_duplicate_ocr_lines_yield_one_candidate(self):
        results = match_augments(["Back To Baslcs", "Back To Basics", "Back To Basics"], CATALOG)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["similarity"], 1)

    def test_geometry_order_and_bounds_alias(self):
        results = match_augments([line("Draw Your Sword", 900),
                                  {"text": "Back To Basics", "bounds": {"x": 100, "y": 200}},
                                  line("Stats!", 400)], CATALOG)
        self.assertEqual([m["id"] for m in results], ["basic", "stats", "sword"])

    def test_string_catalog_and_aliases(self):
        results = match_augments(["Nice Augment"], {12: {"name": "Canonical Name", "aliases": ["Nice Augment"]}})
        self.assertEqual(results[0]["id"], 12)
        self.assertEqual(results[0]["name"], "Canonical Name")
        self.assertEqual(match_augments(["Stats!"], {1: "Stats!"})[0]["id"], 1)

    def test_malformed_lines_are_ignored(self):
        self.assertEqual(match_augments([None, {}, {"text": 10}], CATALOG), [])


class OfferDetectorTests(unittest.TestCase):
    def test_three_card_row_is_returned_left_to_right(self):
        results = detect_offers([line("Draw Your Sword", 900), line("Stats!", 500, 205), line("Back To Basics", 100, 199)], CATALOG)
        self.assertEqual([m["id"] for m in results], ["basic", "stats", "sword"])

    def test_two_cards_supported(self):
        self.assertEqual(len(detect_offers([line("Stats!", 200), line("Draw Your Sword", 800)], CATALOG)), 2)

    def test_single_title_never_triggers_auto_detection(self):
        self.assertEqual(detect_offers([line("Draw Your Sword", 500)], CATALOG), [])

    def test_vertical_lookup_list_is_not_offer_row(self):
        self.assertEqual(detect_offers([line("Stats!", 100, 100), line("Back To Basics", 100, 200), line("Draw Your Sword", 100, 300)], CATALOG), [])

    def test_text_only_matches_require_manual_review(self):
        self.assertEqual(detect_offers(["Stats!", "Back To Basics", "Draw Your Sword"], CATALOG), [])

    def test_four_matches_on_same_row_are_ambiguous(self):
        self.assertEqual(detect_offers([line("Stats!", 100), line("Back To Basics", 400), line("Draw Your Sword", 700), line("Windspeaker's Blessing", 1000)], CATALOG), [])

    def test_two_distinct_plausible_rows_are_ambiguous(self):
        self.assertEqual(detect_offers([line("Stats!", 100), line("Back To Basics", 500), line("Draw Your Sword", 100, 400), line("Windspeaker's Blessing", 500, 400)], CATALOG), [])

    def test_normalized_coordinates_supported(self):
        results = detect_offers([{"text": "Stats!", "x": .2, "y": .4, "height": .02},
                                 {"text": "Back To Basics", "x": .6, "y": .405, "height": .02}], CATALOG)
        self.assertEqual(len(results), 2)

    def test_tightly_overlapping_text_does_not_make_two_cards(self):
        self.assertEqual(detect_offers([line("Stats!", 100), line("Back To Basics", 110)], CATALOG), [])



class DuplicateTitleOfferTests(unittest.TestCase):
    CATALOG = {"tank": "Tank Engine", "night": "Nightstalking", "shrink": "Shrink Engine"}

    @staticmethod
    def title(text, x, y=.404):
        return {"text": text, "x": x, "y": y, "width": .12, "height": .018}

    def actual_ocr_order(self, card_name="Nightstalking"):
        # Captured order from the live game: the tooltip was returned before
        # the middle card despite appearing much lower on the screen.
        return [self.title("Tank Engine", .278),
                self.title("Nightstalking", .361, .739),
                self.title(card_name, .468),
                self.title("Shrink Engine", .659)]

    def test_earlier_tooltip_does_not_discard_middle_card(self):
        results = detect_offers(self.actual_ocr_order(), self.CATALOG)
        self.assertEqual([item["id"] for item in results], ["tank", "night", "shrink"])
        self.assertEqual([item["x"] for item in results], [.278, .468, .659])
        self.assertTrue(all(item["y"] == .404 for item in results))

    def test_clearer_tooltip_does_not_replace_fuzzy_card_occurrence(self):
        lines = self.actual_ocr_order("NightstaIking")
        # Manual matching still chooses the strongest unique occurrence.
        manual = next(item for item in match_augments(lines, self.CATALOG) if item["id"] == "night")
        self.assertEqual(manual["similarity"], 1)
        self.assertEqual(manual["y"], .739)
        offers = detect_offers(lines, self.CATALOG)
        self.assertEqual([item["id"] for item in offers], ["tank", "night", "shrink"])
        self.assertLess(offers[1]["similarity"], 1)
        self.assertEqual(offers[1]["y"], .404)

    def test_tooltip_input_order_cannot_change_offer_row(self):
        lines = self.actual_ocr_order()
        for reordered in (list(reversed(lines)), lines[1:] + lines[:1]):
            with self.subTest(lines=reordered):
                self.assertEqual([item["id"] for item in detect_offers(reordered, self.CATALOG)],
                                 ["tank", "night", "shrink"])

    def test_same_augment_in_separate_card_positions_invalidates_row(self):
        lines = [self.title("Tank Engine", .278), self.title("Nightstalking", .468),
                 self.title("Nightstalking", .659)]
        self.assertEqual(detect_offers(lines, self.CATALOG), [])

    def test_two_copies_of_one_augment_are_not_two_choices(self):
        self.assertEqual(detect_offers([self.title("Nightstalking", .278),
                                       self.title("Nightstalking", .659)], self.CATALOG), [])

    def test_duplicate_complete_rows_remain_ambiguous(self):
        lines = [self.title("Tank Engine", .278), self.title("Nightstalking", .468),
                 self.title("Shrink Engine", .659)]
        lines += [{**item, "y": .739} for item in lines]
        self.assertEqual(detect_offers(lines, self.CATALOG), [])

    def test_three_card_and_two_card_rows_are_not_arbitrarily_ranked(self):
        lines = [self.title("Tank Engine", .278), self.title("Nightstalking", .468),
                 self.title("Shrink Engine", .659), self.title("Tank Engine", .278, .739),
                 self.title("Nightstalking", .659, .739)]
        self.assertEqual(detect_offers(lines, self.CATALOG), [])
class WrappedTitleTests(unittest.TestCase):
    @staticmethod
    def fragment(text, x, y, width=160, height=24):
        return {"text": text, "x": x, "y": y, "width": width, "height": height}

    def test_centered_pixel_fragments_form_one_long_title(self):
        lines = [self.fragment("Windspeaker's", 100, 200, 190),
                 self.fragment("Blessing", 145, 228, 100)]
        results = match_augments(lines, CATALOG)
        self.assertEqual([item["id"] for item in results], ["wind"])
        self.assertEqual(results[0]["height"], 52)

    def test_normalized_fragments_form_one_long_title(self):
        lines = [self.fragment("Windspeaker's", .1, .3, .2, .03),
                 self.fragment("Blessing", .15, .335, .1, .03)]
        self.assertEqual([item["id"] for item in match_augments(lines, CATALOG)], ["wind"])

    def test_wrapped_title_remains_in_three_card_offer_row(self):
        lines = [self.fragment("Windspeaker's", 100, 200, 190),
                 self.fragment("Blessing", 145, 228, 100),
                 line("Draw Your Sword", 500, 214), line("Back To Basics", 900, 214)]
        self.assertEqual([item["id"] for item in detect_offers(lines, CATALOG)],
                         ["wind", "sword", "basic"])

    def test_wrapped_stats_does_not_also_match_shorter_stats(self):
        lines = [self.fragment("Stats on", 100, 200),
                 self.fragment("Stats!", 100, 228)]
        self.assertEqual([item["id"] for item in match_augments(lines, CATALOG)], ["stats2"])

    def test_wrapped_longest_stats_overrides_exact_prefix_fragment(self):
        lines = [self.fragment("Stats on Stats", 100, 200),
                 self.fragment("on Stats!", 100, 228)]
        self.assertEqual([item["id"] for item in match_augments(lines, CATALOG)], ["stats3"])

    def test_three_line_stats_prefers_complete_name(self):
        lines = [self.fragment("Stats on", 100, 200),
                 self.fragment("Stats on", 100, 228),
                 self.fragment("Stats!", 100, 256)]
        self.assertEqual([item["id"] for item in match_augments(lines, CATALOG)], ["stats3"])

    def test_unrelated_adjacent_text_preserves_exact_original(self):
        lines = [self.fragment("Stats!", 100, 200),
                 self.fragment("Gain power", 100, 228)]
        self.assertEqual([item["id"] for item in match_augments(lines, CATALOG)], ["stats"])

    def test_description_font_does_not_join_into_title(self):
        lines = [self.fragment("Draw Your", 100, 200),
                 self.fragment("Sword", 100, 228, height=12)]
        self.assertEqual(match_augments(lines, CATALOG), [])

    def test_adjacent_cards_are_never_joined(self):
        lines = [self.fragment("Draw Your", 100, 200),
                 self.fragment("Sword", 500, 228)]
        self.assertEqual(match_augments(lines, CATALOG), [])

    def test_distant_vertical_text_is_not_joined(self):
        lines = [self.fragment("Draw Your", 100, 200),
                 self.fragment("Sword", 100, 300)]
        self.assertEqual(match_augments(lines, CATALOG), [])

    def test_intervening_text_prevents_join(self):
        lines = [self.fragment("Draw Your", 100, 200),
                 self.fragment("Gain", 100, 225, height=10),
                 self.fragment("Sword", 100, 238)]
        self.assertEqual(match_augments(lines, CATALOG), [])

    def test_geometry_is_required_to_join(self):
        self.assertEqual(match_augments(["Windspeaker's", "Blessing"], CATALOG), [])


class ArenaOfferTests(unittest.TestCase):
    # Names are intentionally restricted to the Arena augment catalog.
    CATALOG = {"gauntlet": "Jeweled Gauntlet", "goliath": "Goliath",
               "duality": "Master of Duality"}

    @staticmethod
    def title(text, x, y=498):
        # Riot 16.21 Cherry AugmentSelection/UIBase title region in its
        # 1600x1200 reference frame, with the managed cards laid horizontally.
        return {"text": text, "x": x / 1600, "y": y / 1200,
                "width": 288 / 1600, "height": 33 / 1200}

    def test_arena_titles_return_left_to_right_without_level_star_numbers(self):
        lines = [self.title("Master of Duality", 1080),
                 self.title("Jeweled Gauntlet", 232), self.title("Goliath", 656),
                 self.title("II", 777, 266)]
        self.assertEqual([row["id"] for row in detect_offers(lines, self.CATALOG)],
                         ["gauntlet", "goliath", "duality"])

    def test_arena_item_anvils_do_not_match_augment_catalog(self):
        lines = [self.title("Reality Fracture", 232),
                 self.title("Dragonheart", 656), self.title("Demon King's Crown", 1080)]
        self.assertEqual(detect_offers(lines, self.CATALOG), [])

    def test_unknown_arena_title_is_omitted_without_global_or_mayhem_substitution(self):
        lines = [self.title("Jeweled Gauntlet", 232),
                 self.title("Unknown Arena Augment", 656), self.title("Goliath", 1080)]
        self.assertEqual([row["id"] for row in detect_offers(lines, self.CATALOG)],
                         ["gauntlet", "goliath"])


if __name__ == "__main__":
    unittest.main()
