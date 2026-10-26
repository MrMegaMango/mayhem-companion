"""Source-scope and manual-selection regression tests for the TFT panel."""
import copy
import gc
import tkinter as tk
from tkinter import ttk
import unittest
from unittest.mock import Mock

from mayhem.tft_ui import TftPanel, filtered_rows, metric_values, resolve_choice, selection_choices, source_metadata


DATA = {
    "source": "Fixture stats", "source_url": "https://example.test/stats", "patch": "test-patch",
    "updated": "2026-10-25", "rank": "Diamond+", "queue": "Ranked",
    "comps": [{"id": "comp-a", "name": "Arcana test", "units": [{"id": "unit-a", "name": "Ahri"}],
               "avg_place": 4.1, "top4_rate": 0.61, "first_rate": 0.14, "games": 300}],
    "units": [{"id": "unit-a", "name": "Ahri", "avg_place": 3.9},
              {"id": "unit-b", "name": "Bard", "avg_place": 4.2}],
    "items": [],
    "augments": [{"id": "aug-a", "name": "Test augment A", "tier": "S", "description": "Expert fixture A"},
                 {"id": "aug-b", "name": "Test augment B", "tier": "B", "description": "Expert fixture B"}],
    "view_meta": {"augments": {"source": "Fixture expert tiers", "source_url": "https://example.test/tiers",
                               "rank": "Expert tier list", "queue": None}},
}
BUILDS = {"source": "Fixture builds", "patch": "test-patch", "rank": "Diamond+", "queue": "Ranked",
          "unit": {"id": "unit-a", "name": "Ahri"},
          "items": [{"id": "build-a", "name": "Ahri three-item set", "scope": "unit_build",
                     "items": [{"id": "1", "name": "First item"}, {"id": "2", "name": "Second item"},
                               {"id": "3", "name": "Third item"}],
                     "avg_place": 4.2, "top4_rate": 0.53, "first_rate": 0.12, "games": 80}]}


class DataPresentationTests(unittest.TestCase):
    def test_fraction_rates_are_displayed_without_inventing_missing_metrics(self):
        self.assertEqual(metric_values({"avg_place": 4.123, "top4_rate": .52, "games": 200}),
                         {"avg_place": "4.12", "top4_rate": "52.0%", "first_rate": "—", "games": "200"})
        self.assertEqual(metric_values({"tier": "S"})["first_rate"], "—")

    def test_zero_sample_and_invalid_rates_never_appear_as_valid_statistics(self):
        zero = metric_values({"games": 0, "avg_place": 1, "top4_rate": 1, "first_rate": 1})
        self.assertEqual(zero, {"avg_place": "—", "top4_rate": "—", "first_rate": "—", "games": "0"})
        invalid = metric_values({"avg_place": float("nan"), "top4_rate": 52, "first_rate": True, "games": -1})
        self.assertTrue(all(value == "—" for value in invalid.values()))

    def test_search_includes_units_and_items_and_sort_keeps_missing_values_last(self):
        rows = [{"name": "Mystic", "units": [{"name": "Ahri"}], "avg_place": 4.3},
                {"name": "Arcana", "units": [{"name": "Ahri"}], "avg_place": 4.1},
                {"name": "Ahri reference"}, {"name": "Bard"}]
        self.assertEqual([row["name"] for row in filtered_rows(rows, "ahri", "Average place")],
                         ["Arcana", "Mystic", "Ahri reference"])
        self.assertEqual(filtered_rows([BUILDS["items"][0]], "Ahri third"), BUILDS["items"])

    def test_augment_source_overrides_ranked_statistical_source(self):
        text, url = source_metadata(DATA, "augments")
        self.assertIn("Fixture expert tiers", text)
        self.assertIn("Expert tier list", text)
        self.assertNotIn("Diamond+", text)
        self.assertNotIn("Ranked", text)
        self.assertEqual(url, "https://example.test/tiers")
        self.assertIn("Ranked", source_metadata(DATA, "comps")[0])

    def test_duplicate_names_require_a_specific_source_id_label(self):
        rows = [{"id": "Akali_AD", "name": "Akali"}, {"id": "Akali_AP", "name": "Akali"}]
        self.assertIsNone(resolve_choice(rows, "Akali"))
        self.assertEqual(resolve_choice(rows, "Akali (Akali_AP)")["id"], "Akali_AP")
        self.assertEqual(len({label for label, _ in selection_choices(rows)}), 2)


class PanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.root = tk.Tk()
        except tk.TclError as error:
            raise unittest.SkipTest(f"Tk display unavailable: {error}")
        cls.root.withdraw()
        ttk.Style(cls.root).theme_use("clam")

    @classmethod
    def tearDownClass(cls):
        gc.collect()
        cls.root.destroy()
        cls.root = None
        gc.collect()

    def setUp(self):
        self.refresh, self.request_items = Mock(), Mock()
        self.panel = TftPanel(self.root, self.refresh, request_items=self.request_items)
        self.panel.pack(fill="both", expand=True)
        self.addCleanup(self._destroy_panel)

    def _destroy_panel(self):
        # Release Tk variables/callback cycles on the main thread, before
        # later tray tests allocate objects on their native-loop thread.
        self.root.update_idletasks()
        self.panel.destroy()
        self.panel = None
        gc.collect()

    def texts(self):
        def collect(widget):
            own = [str(widget.cget("text"))] if isinstance(widget, tk.Label) else []
            return own + [text for child in widget.winfo_children() for text in collect(child)]
        return "\n".join(collect(self.panel.detail))

    def test_constructor_and_comp_browsing_do_not_load_unit_builds(self):
        self.refresh.assert_not_called()
        self.request_items.assert_not_called()
        self.panel.set_data(copy.deepcopy(DATA))
        self.request_items.assert_not_called()
        self.assertIn("4.10", self.texts())
        self.panel.query.set("no match")
        self.panel.render_list()
        self.assertNotIn("4.10", self.texts())
        self.assertIn("No matching", self.texts())

    def test_item_tab_fetches_only_selected_unit_and_never_reuses_unit_overall_rates(self):
        self.panel.set_data(copy.deepcopy(DATA))
        self.panel.switch_view("items")
        self.assertEqual(self.panel.sort.get(), "Games")
        self.request_items.assert_called_once_with("unit-a")
        self.assertNotIn("3.90", self.texts())
        self.panel.set_items("unit-a", copy.deepcopy(BUILDS))
        self.assertIn("4.20", self.texts())
        self.assertIn("Third item", self.texts())
        self.assertIn("Ahri · Item build", self.texts())
        self.assertNotIn("Ahri three-item set", self.texts())
        self.assertIn("LOW SAMPLE", self.texts())
        self.panel.unit_choice.set("Bard")
        self.panel._select_unit()
        self.request_items.assert_called_with("unit-b")
        self.assertNotIn("Third item", self.texts())
        self.panel.set_items("unit-a", copy.deepcopy(BUILDS))
        self.assertNotIn("4.20", self.texts())

    def test_editing_unit_name_clears_old_metrics_and_ignores_inflight_reply(self):
        self.panel.set_data(copy.deepcopy(DATA))
        self.panel.switch_view("items")
        self.panel.set_items("unit-a", copy.deepcopy(BUILDS))
        self.panel.unit_choice.set("Bar")
        self.panel._unit_edited(Mock(keysym="r"))
        self.panel.set_items("unit-a", copy.deepcopy(BUILDS))
        self.assertIsNone(self.panel.item_unit_id)
        self.assertNotIn("4.20", self.texts())
        self.assertIn("Select a unit", self.texts())

    def test_failed_build_request_can_retry_selected_unit(self):
        self.panel.set_data(copy.deepcopy(DATA))
        self.panel.switch_view("items")
        self.panel.set_items_error("unit-a", "Offline")
        self.assertEqual(self.panel.message.get(), "Offline")
        self.assertIn("could not be loaded", self.texts())
        self.panel.refresh()
        self.assertEqual(self.request_items.call_count, 2)
        self.request_items.assert_called_with("unit-a", force=True)

    def test_manual_augment_comparison_never_invents_win_rates(self):
        self.panel.set_data(copy.deepcopy(DATA))
        self.panel.set_offers(["aug-a", "aug-b", "unknown"])
        self.assertIn("Test augment A", self.texts())
        self.assertIn("Test augment B", self.texts())
        self.assertIn("Expert tier", self.texts())
        self.assertNotIn("Top 4", self.texts())
        self.panel.offer_vars[0].set("Test augment")
        self.panel._offer_edited(Mock(keysym="t"), 0)
        self.assertNotIn("Expert fixture A", self.texts())
        self.assertIn("select an exact", self.texts())

    def test_duplicate_augment_name_never_selects_the_first_tier(self):
        data = copy.deepcopy(DATA)
        data["augments"][1]["name"] = data["augments"][0]["name"]
        self.panel.set_data(data)
        self.panel.set_offers(["aug-b", "Test augment A"])
        self.assertIn("Expert fixture B", self.texts())
        self.assertNotIn("Expert fixture A", self.texts())
        self.assertEqual(self.panel.offer_vars[0].get(), "Test augment A (aug-b)")
        self.assertEqual(self.panel.offer_vars[1].get(), "")
        self.panel.offer_vars[0].set("Test augment A")
        self.panel._render_offers()
        self.assertNotIn("Expert fixture B", self.texts())
        self.assertIn("select an exact", self.texts())

    def test_large_catalog_only_queues_selected_detail_icons_until_rows_are_visible(self):
        data = copy.deepcopy(DATA)
        data["augments"] = [{"id": str(i), "name": f"Augment {i}", "icon_url": f"https://example.test/{i}.png"}
                            for i in range(249)]
        self.panel.request_icon = Mock(return_value=None)
        self.panel.set_data(data)
        self.panel.switch_view("augments")
        requested = {call.args[0] for call in self.panel.request_icon.call_args_list}
        self.assertEqual(requested, {"https://example.test/0.png"})

    def test_default_comp_order_prefers_sample_size_to_a_rare_low_average(self):
        data = copy.deepcopy(DATA)
        rare = {**data["comps"][0], "id": "rare", "name": "Rare board", "games": 36, "avg_place": 2.1}
        data["comps"].insert(0, rare)
        self.panel.set_data(data)
        self.assertEqual(self.panel.sort.get(), "Games")
        self.assertEqual(self.panel.visible_rows[0]["id"], "comp-a")
        self.panel.sort.set("Average place")
        self.panel.render_list()
        self.assertEqual(self.panel.visible_rows[0]["id"], "rare")


if __name__ == "__main__":
    unittest.main()
