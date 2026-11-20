"""Rankings filter truthfulness and native loading/error lifecycle."""
import copy
import gc
import tkinter as tk
import unittest
from unittest.mock import Mock, patch

from mayhem.augment_rankings import AugmentRankingsPanel, ranked_rows
from mayhem.theme import install


ROWS = [
    {"id": "1", "name": "Steady", "win_rate": .55, "sample_count": 400, "rarity": "silver"},
    {"id": "2", "name": "Highest", "win_rate": .72, "sample_count": 200, "rarity": "gold"},
    {"id": "3", "name": "Sparse", "win_rate": .99, "sample_count": 3, "rarity": "gold"},
    {"id": "4", "name": "Tie with more games", "win_rate": .55, "sample_count": 900, "rarity": "prismatic"},
]


class RankOrderingTests(unittest.TestCase):
    def test_highest_rate_and_ties_sort_without_promoting_tiny_samples(self):
        original = copy.deepcopy(ROWS)
        self.assertEqual([r["id"] for r in ranked_rows(ROWS)], ["2", "4", "1"])
        self.assertEqual([r["id"] for r in ranked_rows(ROWS, True)], ["3", "2", "4", "1"])
        self.assertEqual(ROWS, original)

    def test_rarity_is_an_independent_filter(self):
        self.assertEqual([r["id"] for r in ranked_rows(ROWS, False, "Gold")], ["2"])
        self.assertEqual([r["id"] for r in ranked_rows(ROWS, True, "Gold")], ["3", "2"])
        self.assertEqual([r["id"] for r in ranked_rows(ROWS, False, "Silver")], ["1"])

    def test_missing_invalid_zero_samples_never_rank_and_zero_rate_is_valid(self):
        invalid = [{"win_rate": value, "sample_count": 200} for value in (None, True, 80, -1, float("nan"))]
        invalid += [{"win_rate": .8, "sample_count": value} for value in (None, True, -1, 0, 1.5)]
        invalid += [{"win_rate": .8, "sample_count": 200, "missing": True}]
        zero = {"id": "zero", "win_rate": 0, "sample_count": 200}
        self.assertEqual(ranked_rows([*invalid, zero], True), [zero])


class RankingsPanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.root = tk.Tk()
        except tk.TclError as error:
            raise unittest.SkipTest(f"Tk display unavailable: {error}")
        cls.root.withdraw()
        install(cls.root)

    @classmethod
    def tearDownClass(cls):
        gc.collect()
        cls.root.destroy()
        cls.root = None
        gc.collect()

    def setUp(self):
        self.retry, self.icons = Mock(), Mock(return_value=None)
        self.panel = AugmentRankingsPanel(self.root, request_icon=self.icons, on_retry=self.retry)
        self.panel.pack(fill="x")
        self.addCleanup(self.cleanup_panel)

    def cleanup_panel(self):
        self.root.update_idletasks()
        self.panel.destroy()
        self.panel = None
        gc.collect()

    def test_small_samples_are_opt_in_and_visibly_marked(self):
        self.panel.set_data({"rows": copy.deepcopy(ROWS)})
        self.assertEqual([r["id"] for r in self.panel.rows], ["2", "4", "1"])
        self.assertIn("200+ games", self.panel.note.get())
        self.panel.include_small.set(True)
        self.panel.render()
        self.assertEqual(self.panel.tree.item("0", "values"), ("99.0", "3*"))
        self.assertIn("low_sample", self.panel.tree.item("0", "tags"))
        self.assertIn("fewer than 200", self.panel.note.get())

    def test_loading_and_error_clear_old_champion_results_and_retry_is_contextual(self):
        self.panel.set_data({"rows": copy.deepcopy(ROWS)})
        self.panel.set_loading("Brand")
        self.assertEqual(self.panel.rows, [])
        self.assertEqual(self.panel.tree.get_children(), ())
        self.assertFalse(self.panel.retry_button.winfo_manager())
        self.panel.set_error("Source unavailable")
        self.assertEqual(self.panel.rows, [])
        self.assertEqual(self.panel.note.get(), "Source unavailable")
        self.assertTrue(self.panel.retry_button.winfo_manager())
        self.panel.retry_button.invoke()
        self.retry.assert_called_once_with()
        self.panel.set_data({"rows": copy.deepcopy(ROWS)})
        self.assertFalse(self.panel.retry_button.winfo_manager())

    def test_empty_sample_and_filtered_empty_are_distinct(self):
        self.panel.set_data({"rows": []})
        self.assertIn("No champion-specific", self.panel.note.get())
        self.panel.set_data({"rows": [ROWS[2]]})
        self.assertIn("filters", self.panel.note.get())
        self.panel.include_small.set(True)
        self.panel.render()
        self.assertEqual(len(self.panel.rows), 1)

    def test_hidden_and_scrolled_lists_request_only_visible_icons(self):
        rows = [{"id": str(i), "name": f"Augment {i:02}", "win_rate": .5,
                 "sample_count": 200, "icon_url": f"https://example.test/{i}.png"}
                for i in range(30)]
        self.panel.set_data({"rows": rows})
        self.root.update_idletasks()
        self.icons.assert_not_called()
        with patch.object(self.panel.tree, "winfo_ismapped", return_value=True), \
                patch.object(self.panel.tree, "yview", return_value=(.5, .7)), \
                patch.object(self.panel.tree, "bbox", return_value=(0, 0, 400, 56)):
            self.panel._visible_icons()
        self.assertEqual([c.args[0] for c in self.icons.call_args_list],
                         [f"https://example.test/{i}.png" for i in range(15, 21)])

    def test_long_names_keep_all_words_when_wrapped(self):
        name = "A very long augment name with multiple words"
        self.panel.set_data({"rows": [{"id": "1", "name": name, "win_rate": .5, "sample_count": 200}]})
        rendered = self.panel.tree.item("0", "text")
        self.assertEqual(" ".join(rendered.split()), name)
        self.assertIn("\n", rendered)
        self.assertLessEqual(len(rendered.splitlines()), 3)


if __name__ == "__main__":
    unittest.main()
