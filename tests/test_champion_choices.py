"""Selection-context and rate-scope tests; no game interaction or HTTP."""
import copy
import gc
import tkinter as tk
from tkinter import ttk
import unittest
from unittest.mock import Mock, patch

from mayhem.champion_choices import ChampionChoicesPanel, display_metrics, ordered_rows


NAMES = {"1": {"name": "Annie", "icon_url": "https://example.test/annie.webp"},
         "2": {"name": "Olaf"}, "3": {"name": "Galio"}}
DATA = {"source": "ARAMKit", "game_mode": "ARAM Mayhem", "scope": "champion", "bracket": "all",
        "version": "16.21", "data_date": "2026-10-25", "stale": False,
        "source_url": "https://example.test/champions", "rows": [
            {"champion_id": "1", "name": "Annie", "icon_url": "https://example.test/annie.webp",
             "win_rate": .55, "sample_count": 800, "missing": False, "low_sample": False},
            {"champion_id": "2", "name": "Olaf", "win_rate": .61, "sample_count": 90,
             "missing": False, "low_sample": True},
            {"champion_id": "3", "name": "Galio", "win_rate": None, "sample_count": 0,
             "missing": True, "low_sample": False}]}


class MetricTests(unittest.TestCase):
    def test_fraction_and_count_formatting_preserve_missing_data(self):
        self.assertEqual(display_metrics(DATA["rows"][0]), ("55.0", "800", False))
        self.assertEqual(display_metrics(DATA["rows"][1]), ("61.0", "90*", True))
        self.assertEqual(display_metrics(DATA["rows"][2]), ("—", "0", False))
        self.assertEqual(display_metrics({}), ("—", "—", False))

    def test_zero_sample_invalid_rates_and_explicit_missing_never_show_win_rate(self):
        for row in ({"sample_count": 0, "win_rate": 1}, {"sample_count": 500, "win_rate": 55},
                    {"sample_count": 500, "win_rate": float("nan")},
                    {"sample_count": 500, "win_rate": True},
                    {"sample_count": 500, "win_rate": .6, "missing": True}):
            with self.subTest(row=row):
                self.assertEqual(display_metrics(row)[0], "—")

    def test_descending_rates_sort_missing_last_and_use_samples_as_tie_breaker(self):
        rows = copy.deepcopy(DATA["rows"])
        rows.append({"champion_id": "4", "name": "Test", "win_rate": .61, "sample_count": 1000})
        self.assertEqual([row["champion_id"] for row in ordered_rows(rows)], ["4", "2", "1", "3"])


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
        self.request_icon = Mock(return_value=None)
        self.panel = ChampionChoicesPanel(self.root, self.request_icon)
        self.panel.pack(fill="x")

    def tearDown(self):
        self.panel.destroy()
        self.panel = None
        self.root.update_idletasks()
        gc.collect()

    def item(self, ident):
        return self.panel.tree.item("c:" + str(ident))

    def test_context_names_render_without_rates_and_none_differs_from_empty(self):
        self.panel.set_context([1, 2, 1], 2, NAMES)
        self.assertEqual(len(self.panel.rows), 2)
        self.assertEqual(self.item(2)["text"], "● Olaf")
        self.assertEqual(self.item(1)["values"], ["—", "—"])
        self.request_icon.assert_not_called()
        self.panel.set_context(None)
        self.assertEqual(self.panel.status.get(), "List unavailable")
        self.panel.set_context([])
        self.assertEqual(self.panel.status.get(), "None available")
        self.assertEqual(self.panel.rows, [])

    def test_data_is_sorted_filtered_and_labels_its_actual_mayhem_scope(self):
        self.panel.set_context([1, 2, 3], 1, NAMES)
        data = copy.deepcopy(DATA)
        data["rows"].append({"champion_id": "999", "name": "Not available", "win_rate": 1, "sample_count": 999})
        self.panel.set_data(data)
        self.assertEqual([row["champion_id"] for row in self.panel.rows], ["2", "1", "3"])
        self.assertEqual(self.item(1)["text"], "● Annie")
        self.assertEqual(self.item(2)["values"], ["61.0", "90*"])
        self.assertIn("ARAM Mayhem", self.panel.source.get())
        self.assertIn("All brackets", self.panel.source.get())
        self.assertIn("16.21", self.panel.source.get())
        self.assertIn("Fewer than 200 games", self.panel.note.get())
        self.assertIn("No rate", self.panel.note.get())

    def test_current_champion_change_preserves_rates_for_same_id_set(self):
        self.panel.set_context([1, 2, 3], 1, NAMES)
        self.panel.set_data(copy.deepcopy(DATA))
        self.panel.set_context([3, 2, 1], 2, NAMES)
        self.assertEqual(self.item(2)["text"], "● Olaf")
        self.assertEqual(self.item(1)["text"], "Annie")
        self.assertEqual(self.item(2)["values"], ["61.0", "90*"])
        self.assertIsNotNone(self.panel.data)

    def test_changed_choices_clear_previous_rates_and_ignore_disjoint_late_reply(self):
        self.panel.set_context([1, 2, 3], 1, NAMES)
        self.panel.set_data(copy.deepcopy(DATA))
        self.panel.set_context([4], 4, {"4": "Twisted Fate"})
        self.assertEqual(self.item(4)["values"], ["—", "—"])
        self.panel.set_data(copy.deepcopy(DATA))
        self.assertEqual(self.item(4)["values"], ["—", "—"])
        self.assertIsNone(self.panel.data)

    def test_missing_available_champion_keeps_name_in_table_without_proxy_rate(self):
        self.panel.set_context([1, 2], 2, NAMES)
        data = copy.deepcopy(DATA)
        data["rows"] = data["rows"][:1]
        self.panel.set_data(data)
        self.assertEqual(self.item(2)["text"], "● Olaf")
        self.assertEqual(self.item(2)["values"], ["—", "—"])

    def test_sample_note_only_exists_when_relevant_and_error_clears_old_rates(self):
        self.panel.set_context([1], 1, NAMES)
        data = copy.deepcopy(DATA)
        self.panel.set_data(data)
        self.assertEqual(self.panel.note.get(), "")
        self.panel.set_error("Could not refresh")
        self.assertEqual(self.panel.status.get(), "Rates unavailable")
        self.assertEqual(self.item(1)["values"], ["—", "—"])
        self.assertEqual(self.panel.note.get(), "Could not refresh")
        self.panel.set_context([1], None, NAMES)
        self.assertEqual(self.panel.status.get(), "Rates unavailable")
        self.assertEqual(self.panel.note.get(), "Could not refresh")

    def test_partial_availability_notice_survives_data_refresh_and_errors(self):
        self.panel.set_context([1, 2], 1, NAMES, incomplete=True)
        self.assertIn("Some choices unavailable", self.panel.note.get())
        self.panel.set_loading()
        self.panel.set_data(copy.deepcopy(DATA))
        self.assertIn("Some choices unavailable", self.panel.note.get())
        self.assertIn("Fewer than 200 games", self.panel.note.get())
        self.panel.set_error("Offline")
        self.assertIn("Some choices unavailable", self.panel.note.get())
        self.assertIn("Offline", self.panel.note.get())
        self.panel.set_context([1, 2], 1, NAMES, incomplete=False)
        self.assertNotIn("Some choices unavailable", self.panel.note.get())

    def test_five_row_limit_and_only_visible_icons_are_requested(self):
        rows = [{"champion_id": str(i), "name": f"Champion {i:02}", "win_rate": .5,
                 "sample_count": 500, "icon_url": f"https://example.test/{i}.webp"} for i in range(20)]
        self.panel.set_context(list(range(20)), 0)
        self.panel.set_data({**DATA, "rows": rows})
        self.assertEqual(int(self.panel.tree.cget("height")), 5)
        self.request_icon.assert_not_called()
        with patch.object(self.panel.tree, "winfo_ismapped", return_value=True), \
                patch.object(self.panel.tree, "yview", return_value=(.25, .5)), \
                patch.object(self.panel.tree, "bbox", return_value=(0, 0, 100, 40)):
            self.panel._visible_icons()
        self.assertEqual({call.args[0] for call in self.request_icon.call_args_list},
                         {f"https://example.test/{i}.webp" for i in range(5, 10)})

    def test_async_icons_do_not_attach_to_replaced_context(self):
        self.panel.set_context([1], 1, NAMES)
        self.panel.set_context([2], 2, NAMES)
        photo = tk.PhotoImage(master=self.root, width=36, height=36)
        self.panel.on_icon("https://example.test/annie.webp", photo)
        self.assertFalse(self.item(2)["image"])

    def test_provider_icon_takes_precedence_over_current_catalog_icon(self):
        names = {"1": {"name": "Annie", "icon": "https://example.test/catalog.webp"}}
        self.panel.set_context([1], 1, names)
        self.panel.set_data(copy.deepcopy(DATA))
        self.assertEqual(self.panel.tree_icons["c:1"], "https://example.test/annie.webp")

    def test_source_link_is_explicit_and_secure(self):
        self.panel.set_context([1], 1, NAMES)
        self.panel.set_data(copy.deepcopy(DATA))
        with patch("mayhem.champion_choices.webbrowser.open") as opened:
            self.panel.open_source()
            opened.assert_called_once_with(DATA["source_url"])
            self.panel.source_url = "file:///unknown"
            self.panel.open_source()
            self.assertEqual(opened.call_count, 1)


if __name__ == "__main__":
    unittest.main()
