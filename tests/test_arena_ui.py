"""Arena mode isolation, source scope and asynchronous view state."""
import copy
import gc
import tkinter as tk
from tkinter import ttk
import unittest
from unittest.mock import Mock, patch

from mayhem.arena_ui import ArenaPanel, champion_rows, formatted, ordered_rows


CATALOG = {
    "source": "Fixture Arena", "source_url": "https://example.test/arena", "version": "test-patch",
    "win_rate_label": "Win rate", "sample_label": "Arena · source sample (rank/region unspecified)",
    "rate_note": "Source win rate; finish cutoff not specified. Items are individual, not full builds.",
    "fetched_at": "2026-10-28T18:00:00+00:00", "stale": False,
    "champions": {"157": {"id": "157", "name": "Yasuo"}, "63": {"id": "63", "name": "Brand"},
                  "99": {"id": "99", "name": "Lux"}},
    "rows": [{"id": "157", "champion_id": "157", "name": "Yasuo", "win_rate": .56,
              "first_rate": .16, "sample_count": 300, "avg_placement": 3.2},
             {"id": "63", "champion_id": "63", "name": "Brand", "win_rate": .62,
              "first_rate": .19, "sample_count": 180, "avg_placement": 2.9}],
    "augments": {"1": {"id": "1", "name": "Goliath"}, "2": {"id": "2", "name": "Jeweled Gauntlet"}},
}
DATA = {
    **{key: value for key, value in CATALOG.items() if key not in ("champions", "rows", "augments")},
    "champion_id": "157", "source_url": "https://example.test/arena/yasuo",
    "augments": [{"id": "1", "name": "Goliath", "win_rate": .63, "sample_count": 210,
                  "avg_placement": 2.8}],
    "items": [{"id": "i1", "name": "Popular item", "win_rate": .51, "sample_count": 900,
               "avg_placement": 3.5},
              {"id": "i2", "name": "Higher rate item", "win_rate": .72, "sample_count": 150,
               "avg_placement": 2.1}],
}
LIVE = {"status": "live", "is_arena": True, "champion_id": "157", "queue_id": 1700}
OFFERS = [{"id": "1", "name": "Goliath"}, {"id": "2", "name": "Jeweled Gauntlet"}]


class ArenaPresentationTests(unittest.TestCase):
    def test_names_join_to_observations_by_id_and_missing_stays_missing(self):
        rows = champion_rows(CATALOG)
        self.assertEqual(len(rows), 3)
        self.assertEqual(formatted(next(row for row in rows if row["id"] == "157"), "win_rate"), "56.0%")
        self.assertEqual(formatted(next(row for row in rows if row["id"] == "99"), "win_rate"), "—")

    def test_fraction_rates_missing_metrics_and_invalid_samples(self):
        self.assertEqual(formatted({"win_rate": .612, "sample_count": 200}, "win_rate"), "61.2%")
        for value in (52, True, float("nan"), -1):
            self.assertEqual(formatted({"win_rate": value, "sample_count": 200}, "win_rate"), "—")
        for count in (0, -1, True, 1.5):
            self.assertEqual(formatted({"win_rate": .5, "sample_count": count}, "win_rate"), "—")
        self.assertEqual(formatted({"avg_placement": 3.125, "sample_count": 200}, "avg_place"), "3.12")
        self.assertEqual(formatted({"sample_count": 200}, "first_rate"), "—")

    def test_default_sort_is_highest_win_rate_not_games_with_missing_last(self):
        rows = [*DATA["items"], {"name": "Unknown item"}]
        self.assertEqual([row["name"] for row in ordered_rows(rows)],
                         ["Higher rate item", "Popular item", "Unknown item"])
        self.assertEqual([row["name"] for row in ordered_rows(rows, "item", "Games")],
                         ["Popular item", "Higher rate item", "Unknown item"])


class ArenaPanelTests(unittest.TestCase):
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
        self.catalog_request, self.champion_request, self.icon_request = Mock(), Mock(), Mock(return_value=None)
        self.panel = ArenaPanel(self.root, self.catalog_request, self.champion_request,
                                request_icon=self.icon_request)
        self.panel.pack(fill="both", expand=True)
        self.addCleanup(self.cleanup_panel)

    def cleanup_panel(self):
        self.root.update_idletasks()
        self.panel.destroy()
        self.panel = None
        gc.collect()

    def start_live(self):
        self.panel.set_catalog(copy.deepcopy(CATALOG))
        self.panel.handle_game(copy.deepcopy(LIVE))
        self.panel.set_champion_data("157", copy.deepcopy(DATA))

    def test_catalog_ranking_table_has_rates_and_unobserved_champions(self):
        self.panel.set_catalog(copy.deepcopy(CATALOG))
        self.assertEqual([row["id"] for row in self.panel.rows], ["63", "157", "99"])
        self.assertEqual(self.panel.tree.item("0", "values"), ("62.0%", "19.0%", "180*"))
        self.assertEqual(self.panel.tree.item("2", "values"), ("—", "—", "—"))

    def test_catalog_after_live_context_resolves_champion_and_keeps_loading_message(self):
        self.panel.handle_game(copy.deepcopy(LIVE))
        self.panel.set_catalog(copy.deepcopy(CATALOG))
        self.champion_request.assert_called_with("157", force=False)
        self.assertEqual(self.panel.champion, "157")
        self.assertIn("Loading champion", self.panel.message.get())
        self.assertEqual(self.panel.view.get(), "items")

    def test_offers_show_only_champion_augment_rate_and_missing_stays_dash(self):
        self.start_live()
        self.panel.set_offers(copy.deepcopy(OFFERS))
        self.assertEqual(self.panel.view.get(), "augments")
        self.assertEqual(self.panel.cards[0][2].cget("text"), "63.0%")
        self.assertEqual(self.panel.offer_labels[0].cget("text"), "Win rate")
        self.assertEqual(self.panel.cards[1][2].cget("text"), "—")
        self.assertEqual(self.panel.offer_labels[1].cget("text"), "Win rate")
        self.assertEqual(self.panel.cards[1][3].cget("text"), "No champion-specific data")
        self.assertIn("picks/levels not separated", self.panel.detail.get())
        self.panel.set_offers([])
        self.assertEqual(self.panel.view.get(), "items")

    def test_offer_emphasis_preserves_slot_order_and_clears_on_refresh(self):
        self.start_live()
        data = copy.deepcopy(DATA)
        data["augments"].append({"id": "2", "name": "Jeweled Gauntlet",
                                 "win_rate": .67, "sample_count": 900})
        self.panel.set_champion_data("157", data)
        self.panel.set_offers(copy.deepcopy(OFFERS))
        self.assertEqual([card[1].cget("text") for card in self.panel.cards[:2]],
                         ["Goliath", "Jeweled Gauntlet"])
        self.assertEqual(self.panel.offer_badges[0].cget("text"), "")
        self.assertEqual(self.panel.offer_badges[1].cget("text"), "HIGHEST WR")
        self.panel.retry()
        self.assertEqual(self.panel.offer_badges[1].cget("text"), "")
        self.assertEqual(self.panel.cards[1][2].cget("text"), "—")

    def test_sparse_offer_leader_never_highlights_lower_rate(self):
        self.start_live()
        data = copy.deepcopy(DATA)
        data["augments"].append({"id": "2", "name": "Jeweled Gauntlet",
                                 "win_rate": .67, "sample_count": 90})
        self.panel.set_champion_data("157", data)
        self.panel.set_offers(copy.deepcopy(OFFERS))
        self.assertEqual([label.cget("text") for label in self.panel.offer_badges[:2]], ["", ""])
        self.assertIn("LOW SAMPLE", self.panel.cards[1][3].cget("text"))
        self.panel.set_offers([OFFERS[0]])
        self.assertEqual(self.panel.offer_badges[0].cget("text"), "")

    def test_manual_tab_survives_reroll_and_offer_disappearance(self):
        self.start_live()
        self.panel.set_offers(copy.deepcopy(OFFERS))
        self.panel.switch_view("champions")
        self.panel.set_offers(list(reversed(OFFERS)))
        self.panel.set_offers([])
        self.assertEqual(self.panel.view.get(), "champions")

    def test_retry_same_champion_preserves_offers_and_refetches(self):
        self.start_live()
        self.panel.set_offers(copy.deepcopy(OFFERS))
        self.panel.retry()
        self.catalog_request.assert_called_once_with(force=True)
        self.champion_request.assert_called_with("157", force=True)
        self.assertEqual(self.panel.offers, OFFERS)
        self.assertIsNone(self.panel.data)
        self.assertIn("Loading", self.panel.cards[0][3].cget("text"))

    def test_typing_clears_metrics_and_old_champion_reply_cannot_restore_them(self):
        self.start_live()
        self.panel.champion_name.set("Bra")
        self.panel.filter_champions(Mock(keysym="a"))
        self.panel.set_champion_data("157", copy.deepcopy(DATA))
        self.assertFalse(self.panel.follow)
        self.assertIsNone(self.panel.champion)
        self.assertIsNone(self.panel.data)
        self.assertEqual(self.panel.rows, [])
        self.champion_request.assert_called_with(None)

    def test_non_arena_context_clears_followed_champion_and_rejects_offers(self):
        self.start_live()
        self.panel.set_offers(copy.deepcopy(OFFERS))
        self.panel.handle_game({"status": "live", "is_arena": False, "is_aram": True, "champion_id": "157"})
        self.panel.set_offers(copy.deepcopy(OFFERS))
        self.assertIsNone(self.panel.champion)
        self.assertIsNone(self.panel.data)
        self.assertEqual(self.panel.offers, [])

    def test_manual_champion_stays_selected_while_live_champion_changes(self):
        self.start_live()
        self.panel.champion_name.set("Brand")
        self.panel.select_champion()
        self.panel.handle_game({**LIVE, "champion_id": "99"})
        self.assertEqual(self.panel.champion, "63")
        self.assertEqual(self.panel.live_champion, "99")
        self.panel.request_scan = Mock()
        self.panel.follow_game()
        self.assertEqual(self.panel.champion, "99")
        self.panel.request_scan.assert_called_once_with()

    def test_source_scope_and_rate_definition_are_available_without_relabeling_top_four(self):
        self.start_live()
        self.assertIn("rank/region unspecified", self.panel.source.get())
        self.assertNotIn("Top 4", self.panel.source.get())
        with patch("mayhem.arena_ui.messagebox.showinfo") as dialog:
            self.panel.about_rates()
        text = dialog.call_args.args[1]
        self.assertIn("finish cutoff not specified", text)
        self.assertIn("Fetched 2026-10-28", text)

    def test_unchanged_inactive_poll_does_not_reset_manual_table_selection(self):
        self.panel.set_catalog(copy.deepcopy(CATALOG))
        game = {"status": "waiting", "phase": "Lobby", "is_arena": False}
        self.panel.handle_game(game)
        self.panel.tree.selection_set("1")
        with patch.object(self.panel, "render", wraps=self.panel.render) as render:
            self.panel.handle_game(dict(game))
        render.assert_not_called()
        self.assertEqual(self.panel.tree.selection(), ("1",))

    def test_scrolling_requests_new_visible_icons_without_queueing_entire_roster(self):
        data = copy.deepcopy(CATALOG)
        data["champions"] = {str(i): {"id": str(i), "name": f"Champion {i:02}",
                                    "icon_url": f"https://example.test/icon/{i}.png"} for i in range(1, 31)}
        data["rows"] = []
        self.panel.set_catalog(data)
        self.assertLessEqual(len({call.args[0] for call in self.icon_request.call_args_list}), 9)
        self.icon_request.reset_mock()
        with patch.object(self.panel.tree, "winfo_ismapped", return_value=True), \
                patch.object(self.panel.tree, "yview", return_value=(.5, .7)), \
                patch.object(self.panel.tree, "bbox", return_value=(0, 0, 400, 40)):
            self.panel._visible_tree_icons()
        self.assertEqual([call.args[0] for call in self.icon_request.call_args_list],
                         [f"https://example.test/icon/{i}.png" for i in range(16, 22)])


if __name__ == "__main__":
    unittest.main()
