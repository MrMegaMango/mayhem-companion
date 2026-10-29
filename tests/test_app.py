"""State regression tests use a fake view; no game interaction or desktop input."""
import json
from pathlib import Path
import queue
import unittest
from unittest.mock import MagicMock, Mock, call, patch

from mayhem.app import Companion
from mayhem.theme import ACCENT, BORDER, MUTED, TEXT


class StateTests(unittest.TestCase):
    def setUp(self):
        style = patch("mayhem.app.set_window_style")
        style.start()
        self.addCleanup(style.stop)

    def make_view(self):
        app = object.__new__(Companion)
        app.catalog = {"champions": {"157": {"name": "Yasuo", "riotId": "Yasuo", "slug": "yasuo"}}}
        app.worker = Mock()
        app.requests = queue.Queue()
        app.events = queue.Queue()
        app._closed = app.recovery_visible = False
        app._autostart_busy = False
        app.storage = Path("C:/Users/fixture/MayhemCompanion")
        app.champ_var, app.badge, app.stage = Mock(), Mock(), Mock()
        app.status, app.source, app.root = Mock(), Mock(), Mock()
        app.follow = True
        app.champion = "157"
        app.live_champion = "157"
        app.locked = False
        app.hidden = True
        app._placement_job = None
        app._last_display = None
        app.drag_start = None
        app.auto_visible = app.browse_mode = False
        app.visibility_override = None
        app.hotkeys = Mock()
        app.hotkeys.registered = [101, 102, 103]
        app.tray = Mock()
        app.tray.available = True
        app.lock_button = Mock()
        app.game_live = True
        app.game_status = "live"
        app.last_active_status = "live"
        app.champ_select = app.champion_preview = False
        app.switch_view = Mock()
        app.fit_window = Mock()
        app.token = 5
        app.is_auto_offer = True
        app.offers_active = True
        app.view_automatic = True
        app.offer_ids = ["1", "2", "3"]
        app.cards = [{"var": Mock(), "value": Mock(), "meta": Mock(),
                      "rank": Mock(), "frame": Mock()} for _ in range(3)]
        app.spell_token = 7
        app.spell_champion = "157"
        app.spell_data = None
        app.choice_token = 0
        app.choice_ids = app.choice_context = None
        app.choice_active = False
        app.choice_panel, app.spells_tab, app.spell_toolbar = Mock(), Mock(), Mock()
        app.build_token = 9
        app.build_champion = "157"
        app.build_data = None
        app.build_cards = [{"frame": Mock(), "value": Mock(), "meta": Mock(),
                            "items": [{"frame": Mock(), "icon": Mock(), "name": Mock(), "icon_url": None} for _ in range(3)]}
                           for _ in range(5)]
        app.build_context_panel = Mock()
        app.build_core_items = [{"icon": Mock(), "name": Mock(), "icon_url": None} for _ in range(3)]
        app.build_order, app.build_order_box = Mock(), MagicMock()
        app.build_order_key = None
        app.live_items = None
        app.inventory_champion = None
        app.inventory_current = False
        app.display_mode = "aram"
        app.last_game_context = {}
        app.tft_token = 0
        app.tft_item_token = 0
        app.tft_unit = None
        app.tft_loaded = app.tft_pending = False
        app.arena_token = app.arena_champion_token = 0
        app.arena_champion = None
        app.arena_pending = False
        app.arena_panel = Mock()
        app.mode_choice = Mock()
        app.mode_choice.get.return_value = "Auto"
        app.tft_panel, app.brand, app.body = Mock(), Mock(), Mock()
        app.item_images = {}
        app.icons = Mock()
        app.build_group, app.build_sort = Mock(), Mock()
        app.build_group.get.return_value = "AP"
        app.build_sort.get.return_value = "Games"
        app.build_group_box = MagicMock()
        app.build_summary, app.build_note, app.build_note_label = Mock(), Mock(), Mock()
        app.live_spells = []
        app.spell_tree = Mock()
        app.spell_tree.get_children.return_value = []
        app.spell_summary, app.spell_note = Mock(), Mock()
        app.spell_sort = Mock()
        app.spell_sort.get.return_value = "Win rate"
        app.view = Mock()
        app.view.get.return_value = "augments"
        app.auto = Mock()
        app.auto.get.return_value = True
        return app

    def test_game_updates_resolve_locally_without_http(self):
        app = self.make_view()
        app.handle_game({"status": "live", "raw_champion": "game_character_displayname_Yasuo"})
        self.assertEqual(app.champion, "157")
        self.assertFalse(app.worker.provider.mock_calls)

    def test_champion_choice_list_loads_once_and_current_swap_only_changes_highlight(self):
        app = self.make_view()
        game = {"status": "champ_select", "is_aram": True, "champion_id": "157",
                "available_champion_ids": ["157", "63"]}
        app.update_champion_choices(game)
        app.update_champion_choices(game)
        app.update_champion_choices({**game, "available_champion_ids": ["63", "157"], "champion_id": "63"})
        self.assertEqual(list(app.requests.queue), [("champions", (1, ["157", "63"]))])
        app.choice_panel.pack.assert_called_once()
        app.choice_panel.set_loading.assert_called_once()
        app.choice_panel.set_context.assert_called_with(["157", "63"], "63", app.catalog["champions"], incomplete=False)
        app.spells_tab.configure.assert_called_with(text="Select")

    def test_rerolled_champion_choices_reject_old_reply_and_error(self):
        app = self.make_view()
        game = {"status": "champ_select", "is_aram": True, "champion_id": "157",
                "available_champion_ids": ["157", "63"]}
        app.update_champion_choices(game)
        app.update_champion_choices({**game, "available_champion_ids": ["157", "99"]})
        app.handle_event("champions", (1, {"rows": [{"champion_id": "63"}]}))
        app.handle_event("champions_error", (1, "Old error"))
        app.choice_panel.set_data.assert_not_called()
        app.choice_panel.set_error.assert_not_called()
        data = {"rows": [{"champion_id": "99"}]}
        app.handle_event("champions", (2, data))
        app.choice_panel.set_data.assert_called_once_with(data)

    def test_unknown_choices_clear_old_list_without_claiming_empty_or_refetching(self):
        app = self.make_view()
        game = {"status": "champ_select", "is_aram": True, "champion_id": "157",
                "available_champion_ids": ["157"]}
        app.update_champion_choices(game)
        app.update_champion_choices({**game, "available_champion_ids": None})
        app.choice_panel.set_context.assert_called_with(None, "157", app.catalog["champions"], incomplete=False)
        self.assertEqual(app.requests.qsize(), 1)
        app.handle_event("champions", (1, {"rows": []}))
        app.choice_panel.set_data.assert_not_called()
        app.update_champion_choices({**game, "available_champion_ids": []})
        app.choice_panel.set_context.assert_called_with([], "157", app.catalog["champions"], incomplete=False)
        self.assertEqual(app.requests.qsize(), 1)

    def test_choice_panel_hides_and_invalidates_data_when_match_starts(self):
        app = self.make_view()
        app.update_champion_choices({"status": "champ_select", "is_aram": True,
                                      "available_champion_ids": ["157"], "champion_id": "157"})
        app.update_champion_choices({"status": "live", "is_aram": True})
        app.handle_event("champions", (1, {"rows": []}))
        app.choice_panel.set_data.assert_not_called()
        app.choice_panel.pack_forget.assert_called_once()
        app.spells_tab.configure.assert_called_with(text="Spells")
        self.assertFalse(app.choice_active)
        self.assertIsNone(app.choice_ids)

    def test_unknown_or_other_modes_never_load_choice_rates(self):
        app = self.make_view()
        game = {"status": "champ_select", "available_champion_ids": ["157"]}
        for mode in (None, False):
            app.update_champion_choices({**game, "is_aram": mode})
        app.display_mode = "tft"
        app.update_champion_choices({**game, "is_aram": True})
        self.assertTrue(app.requests.empty())
        app.choice_panel.pack.assert_not_called()

    def test_manual_browsing_does_not_replace_actual_current_choice_and_retry_is_explicit(self):
        app = self.make_view()
        app.follow, app.champion = False, "99"
        game = {"status": "champ_select", "is_aram": True, "champion_id": "157",
                "available_champion_ids": ["157", "63"]}
        app.update_champion_choices(game)
        app.handle_event("champions_error", (1, "Offline"))
        app.update_champion_choices(game)
        self.assertEqual(app.requests.qsize(), 1)
        app.update_champion_choices(game, force=True)
        self.assertEqual(app.requests.qsize(), 2)
        self.assertEqual(app.champion, "99")
        app.choice_panel.set_context.assert_called_with(["157", "63"], "157", app.catalog["champions"], incomplete=False)

    def test_partial_availability_note_changes_without_refetching_same_champions(self):
        app = self.make_view()
        game = {"status": "champ_select", "is_aram": True, "champion_id": "157",
                "available_champion_ids": ["157"], "availability_incomplete": True}
        app.update_champion_choices(game)
        app.choice_panel.set_context.assert_called_with(["157"], "157", app.catalog["champions"], incomplete=True)
        app.update_champion_choices({**game, "availability_incomplete": False})
        self.assertEqual(app.requests.qsize(), 1)
        app.choice_panel.set_context.assert_called_with(["157"], "157", app.catalog["champions"], incomplete=False)

    def test_choices_and_rates_can_arrive_before_catalog_without_waiting_for_current_champion(self):
        for current in (None, "157"):
            with self.subTest(current=current):
                app = self.make_view()
                catalog = {**app.catalog, "augments": {}}
                app.catalog = None
                app.champ_box = MagicMock()
                for card in app.cards:
                    card["box"] = MagicMock()
                game = {"status": "champ_select", "is_aram": True, "phase": "ChampSelect",
                        "champion_id": current, "available_champion_ids": ["157", "63"]}
                app.handle_game(game)
                self.assertTrue(app.choice_active)
                self.assertIsNone(app.champion)
                token = app.choice_token
                rates = {"rows": [{"champion_id": "157", "win_rate": .58}]}
                app.handle_event("champions", (token, rates))
                app.handle_event("catalog", catalog)
                self.assertEqual(app.champion, current)
                self.assertEqual(app.choice_token, token)
                app.choice_panel.set_data.assert_called_once_with(rates)
                app.choice_panel.set_loading.assert_called_once()
                app.choice_panel.set_context.assert_called_with(["157", "63"], current,
                                                                catalog["champions"], incomplete=False)
                if current:
                    self.assertTrue(any(kind == "spells" and value[1] == current
                                        for kind, value in app.requests.queue))

    def test_late_catalog_after_leaving_select_does_not_restore_choices(self):
        app = self.make_view()
        catalog = {**app.catalog, "augments": {}}
        app.catalog = None
        app.champ_box = MagicMock()
        for card in app.cards:
            card["box"] = MagicMock()
        app.handle_game({"status": "champ_select", "is_aram": True, "phase": "ChampSelect",
                         "champion_id": None, "available_champion_ids": ["157", "63"]})
        app.handle_game({"status": "waiting", "phase": "Lobby"})
        token = app.choice_token
        app.handle_event("catalog", catalog)
        self.assertFalse(app.choice_active)
        self.assertTrue(app.hidden)
        self.assertIsNone(app.champion)
        self.assertEqual(app.choice_token, token)

    def test_tft_auto_mode_loads_its_own_panel_once_and_stays_on_vertical_placement_path(self):
        app = self.make_view()
        tft = {"status": "tft", "is_tft": True, "is_aram": False, "phase": "InProgress"}
        app.handle_game(tft)
        app.handle_game(tft)
        self.assertEqual(app.display_mode, "tft")
        self.assertFalse(app.hidden)
        app.body.pack_forget.assert_called_once()
        app.tft_panel.pack.assert_called_once()
        self.assertEqual(list(app.requests.queue), [("tft", (1, False))])
        app.fit_window.assert_called()
        self.assertFalse(app.game_live)
        self.assertIsNone(app.live_champion)

    def test_tft_failure_does_not_retry_every_game_poll_and_manual_retry_preserves_token(self):
        app = self.make_view()
        tft = {"status": "tft", "is_tft": True, "is_aram": False, "phase": "InProgress"}
        app.handle_game(tft)
        app.handle_event("tft_error", (1, "Offline"))
        for _ in range(4):
            app.handle_game(tft)
        self.assertEqual(app.requests.qsize(), 1)
        app.load_tft(force=True)
        self.assertEqual(app.tft_token, 2)
        app.handle_event("tft", (1, {"source": "old"}))
        app.tft_panel.set_data.assert_not_called()
        data = {"source": "current"}
        app.handle_event("tft", (2, data))
        app.tft_panel.set_data.assert_called_once_with(data)

    def test_aram_after_tft_restores_aram_panel_and_spells_without_tft_data(self):
        app = self.make_view()
        app.handle_game({"status": "tft", "is_tft": True, "phase": "InProgress"})
        app.handle_game({"status": "champ_select", "is_tft": False, "is_aram": True,
                         "phase": "ChampSelect", "champion_id": "157"})
        self.assertEqual(app.display_mode, "aram")
        app.tft_panel.pack_forget.assert_called_once()
        self.assertEqual(app.champion, "157")
        app.switch_view.assert_called_with("spells", automatic=True)
        self.assertIn(("scan", None), list(app.requests.queue))

    def test_tft_visibility_hides_at_end_but_retains_manual_hide_and_transient_context(self):
        app = self.make_view()
        tft = {"status": "tft", "phase": "InProgress", "is_tft": True, "is_aram": False}
        app.update_visibility(tft)
        app.update_visibility({"status": "error", "is_tft": None, "is_aram": None, "phase": "InProgress"})
        self.assertFalse(app.hidden)
        app.toggle_visible()
        app.update_visibility(tft)
        self.assertTrue(app.hidden)
        app.update_visibility({"status": "waiting", "phase": "EndOfGame"})
        app.update_visibility(tft)
        self.assertFalse(app.hidden)
        app.update_visibility({"status": "waiting", "phase": "EndOfGame"})
        self.assertTrue(app.hidden)

    def test_manual_tft_browsing_does_not_receive_aram_augment_choices(self):
        app = self.make_view()
        app.mode_choice.get.return_value = "TFT"
        app.change_game_mode()
        app.handle_event("offers", [{"id": "new-aram-offer"}])
        self.assertEqual(app.display_mode, "tft")
        app.switch_view.assert_not_called()
        app.tft_panel.set_offers.assert_not_called()

    def test_tft_item_results_require_current_unit_and_token_and_reload_invalidates_them(self):
        app = self.make_view()
        app.load_tft_items("Ahri")
        app.load_tft_items("Akali")
        app.handle_event("tft_items", (1, "Ahri", {"rows": []}))
        app.handle_event("tft_items", (2, "Ahri", {"rows": []}))
        app.tft_panel.set_items.assert_not_called()
        app.handle_event("tft_items", (2, "Akali", {"rows": []}))
        app.tft_panel.set_items.assert_called_once_with("Akali", {"rows": []})
        app.load_tft(force=True)
        app.handle_event("tft_items_error", (2, "Akali", "old failure"))
        app.tft_panel.set_items_error.assert_not_called()

    def test_return_to_auto_leaves_manual_browsing_and_hides_in_lobby(self):
        app = self.make_view()
        app.last_game_context = {"status": "waiting", "phase": "Lobby"}
        app.mode_choice.get.return_value = "TFT"
        app.change_game_mode()
        self.assertFalse(app.hidden)
        app.mode_choice.get.return_value = "Auto"
        app.change_game_mode()
        self.assertTrue(app.hidden)
        self.assertIsNone(app.visibility_override)
        self.assertFalse(app.browse_mode)

    def test_portrait_preference_moves_old_primary_position_to_selected_display(self):
        app = self.make_view()
        app.settings = {"display_device": "portrait", "display_preference": "portrait"}
        displays = [
            {"device": "main", "bounds": (0, 0, 3840, 2160), "work_area": (0, 0, 3840, 2100), "primary": True},
            {"device": "portrait", "bounds": (-1080, -400, 0, 1520), "work_area": (-1080, -400, 0, 1480), "primary": False},
        ]
        with patch("mayhem.app.list_displays", return_value=displays):
            x, y, height = app.panel_placement(800, [3370, 80])
        self.assertGreaterEqual(x, -1080)
        self.assertLessEqual(x + 440, 0)
        self.assertGreaterEqual(y, -400)
        self.assertLessEqual(y + height, 1480)

    def test_monitor_query_failure_preserves_last_display_but_real_unplug_uses_fallback(self):
        app = self.make_view()
        app.settings = {"display_preference": "portrait"}
        portrait = {"device": "portrait", "bounds": (-1080, 233, 0, 2153),
                    "work_area": (-1080, 233, 0, 2105), "primary": False}
        landscape = {"device": "main", "bounds": (0, 0, 3840, 2160),
                     "work_area": (0, 0, 3840, 2088), "primary": True}
        with patch("mayhem.app.list_displays", side_effect=[[portrait, landscape], OSError("temporary"), [landscape]]):
            expected = app.panel_placement(600, [-464, 313])
            self.assertEqual(app.panel_placement(600, [-464, 313]), expected)
            self.assertGreater(app.panel_placement(600, [-464, 313])[0], 0)

    def test_visible_position_check_repairs_windows_relocation_and_keeps_one_timer(self):
        app = self.make_view()
        app.hidden = False
        app.shell = Mock()
        app.shell.winfo_reqheight.return_value = 900
        app.root.winfo_x.return_value, app.root.winfo_y.return_value = 3000, 100
        app.root.winfo_width.return_value, app.root.winfo_height.return_value = 440, 900
        app.panel_placement = Mock(return_value=(-464, 313, 900))
        app.check_placement()
        app.root.geometry.assert_called_once_with("440x900+-464+313")
        app.root.after.assert_called_once_with(1500, app.check_placement)
        app.set_visible(True)
        app.set_visible(True)
        app.root.after.assert_called_once()

    def test_correct_position_and_dpi_rounding_do_not_keep_resizing_window(self):
        app = self.make_view()
        app.hidden = False
        app.shell = Mock()
        app.root.winfo_x.return_value, app.root.winfo_y.return_value = -463, 314
        app.root.winfo_width.return_value, app.root.winfo_height.return_value = 441, 901
        app.panel_placement = Mock(return_value=(-464, 313, 900))
        app.check_placement()
        app.root.geometry.assert_not_called()

    def test_hide_cancels_position_check_and_stale_callback_does_not_restart_it(self):
        app = self.make_view()
        app.hidden = False
        app._placement_job = "placement-timer"
        app.set_visible(False)
        app.root.after_cancel.assert_called_once_with("placement-timer")
        app.check_placement()
        app.root.after.assert_not_called()

    def test_drag_defers_correction_until_release_and_does_not_disable_future_checks(self):
        app = self.make_view()
        app.hidden = False
        app.drag_start = (10, 10)
        app.panel_placement = Mock()
        app.check_placement()
        app.panel_placement.assert_not_called()
        app.end_drag(None)
        self.assertIsNone(app.drag_start)
        app.root.after.assert_called_with(150, app.check_placement)

    def test_close_preserves_display_preference_and_negative_position(self):
        app = self.make_view()
        app.settings = {"display_device": "portrait", "display_preference": "portrait"}
        app.stop, app.settings_path = Mock(), Mock()
        app.root.winfo_x.return_value = -1000
        app.root.winfo_y.return_value = -320
        app.close()
        saved = json.loads(app.settings_path.write_text.call_args.args[0])
        self.assertEqual(saved["position"], [-1000, -320])
        self.assertEqual(saved["display_device"], "portrait")
        self.assertEqual(saved["display_preference"], "portrait")
        app.close()
        app.tray.close.assert_called_once()
        app.root.destroy.assert_called_once()

    def test_tray_show_unlocks_panel_and_survives_idle_poll(self):
        app = self.make_view()
        app.locked = True
        app.handle_event("tray_action", "show")
        self.assertFalse(app.hidden)
        self.assertFalse(app.locked)
        app.update_visibility({"status": "waiting", "phase": "None", "is_aram": None})
        self.assertFalse(app.hidden)

    def test_tray_can_hide_and_reopen_without_show_shortcut(self):
        app = self.make_view()
        app.hotkeys.registered = []
        app.handle_event("tray_action", "show")
        app.handle_event("tray_action", "hide")
        self.assertTrue(app.hidden)
        app.handle_event("tray_action", "show")
        self.assertFalse(app.hidden)

    def test_both_recovery_controls_failing_keeps_panel_accessible(self):
        app = self.make_view()
        app.hotkeys.registered = []
        app.tray.available = False
        app.handle_event("tray_status", False)
        app.update_visibility({"status": "waiting", "phase": "None", "is_aram": None})
        self.assertFalse(app.hidden)
        app.set_manual_visible(False)
        self.assertFalse(app.hidden)
        app.tray.available = True
        app.handle_event("tray_status", True)
        self.assertTrue(app.hidden)
        self.assertFalse(app.recovery_visible)

    def test_shortcut_failure_with_working_tray_does_not_open_hidden_panel(self):
        app = self.make_view()
        app.hotkeys.registered = []
        app.handle_event("hotkeys_failed", None)
        self.assertTrue(app.hidden)
        app.root.deiconify.assert_not_called()

    def test_tray_failure_unlocks_when_only_show_shortcut_survives(self):
        app = self.make_view()
        app.locked = True
        app.hotkeys.registered = [101]
        app.tray.available = False
        app.handle_event("tray_status", False)
        self.assertFalse(app.locked)
        self.assertTrue(app.hidden)
        app.lock_button.configure.assert_called_with(text="Lock")
        app.root.deiconify.assert_not_called()

    def test_tray_exit_stops_services_and_does_not_schedule_after_destroy(self):
        app = self.make_view()
        app.settings = {}
        app.stop, app.settings_path = Mock(), Mock()
        app.root.winfo_x.return_value = -464
        app.root.winfo_y.return_value = 313
        app.events.put(("tray_action", "exit"))
        app.events.put(("game", {"status": "live"}))
        app.drain()
        self.assertTrue(app._closed)
        app.stop.set.assert_called_once()
        app.tray.close.assert_called_once()
        app.hotkeys.close.assert_called_once()
        app.root.destroy.assert_called_once()
        app.root.after.assert_not_called()
        self.assertEqual(app.events.qsize(), 1)

    def test_start_with_league_configures_on_helper_thread_and_reports_result(self):
        app = self.make_view()
        with patch("mayhem.autostart.is_enabled", return_value=False), \
                patch("mayhem.app.threading.Thread") as thread, \
                patch("mayhem.autostart.configure_startup", return_value=True) as configure:
            app.handle_event("tray_action", "toggle_autostart")
            self.assertTrue(app._autostart_busy)
            thread.return_value.start.assert_called_once()
            configure.assert_not_called()
            thread.call_args.kwargs["target"]()
        configure.assert_called_once_with(True, app.storage)
        self.assertEqual(app.events.get_nowait(), ("autostart_result", (True, True)))
        app.handle_event("autostart_result", (True, True))
        self.assertFalse(app._autostart_busy)

    def test_disable_start_with_league_and_ignore_repeated_click_while_busy(self):
        app = self.make_view()
        with patch("mayhem.autostart.is_enabled", return_value=True) as enabled, \
                patch("mayhem.app.threading.Thread") as thread, \
                patch("mayhem.autostart.configure_startup", return_value=True) as configure:
            app.toggle_autostart()
            app.toggle_autostart()
            thread.assert_called_once()
            self.assertEqual(enabled.call_count, 1)
            thread.call_args.kwargs["target"]()
        configure.assert_called_once_with(False, app.storage)
        self.assertEqual(app.events.get_nowait(), ("autostart_result", (True, False)))

    def test_startup_config_failure_is_reported_and_releases_busy_state(self):
        app = self.make_view()
        with patch("mayhem.autostart.is_enabled", return_value=False), \
                patch("mayhem.app.threading.Thread") as thread, \
                patch("mayhem.autostart.configure_startup", return_value=False):
            app.toggle_autostart()
            thread.call_args.kwargs["target"]()
        kind, payload = app.events.get_nowait()
        app.handle_event(kind, payload)
        self.assertFalse(app._autostart_busy)
        self.assertIn("Could not change", app.status.set.call_args.args[0])

    def test_automatic_duplicate_launch_exits_without_a_popup(self):
        from mayhem.app import main
        fake_ctypes = Mock()
        fake_ctypes.get_last_error.return_value = 183
        with patch("mayhem.app.ctypes", fake_ctypes), \
                patch("sys.argv", ["mayhem", "--auto-start"]), patch("mayhem.app.tk.Tk") as root:
            main()
        root.assert_not_called()
        fake_ctypes.windll.user32.MessageBoxW.assert_not_called()

    def test_hidden_on_desktop_lobby_other_modes_and_unknown_mode(self):
        app = self.make_view()
        for game in ({"status": "waiting"},
                     {"status": "waiting", "phase": "Lobby", "is_aram": True},
                     {"status": "champ_select", "is_aram": False},
                     {"status": "live", "is_aram": False},
                     {"status": "live", "is_aram": None}):
            app.update_visibility(game)
            self.assertTrue(app.hidden)
        app.root.deiconify.assert_not_called()

    def test_shows_at_aram_select_stays_through_loading_and_hides_at_end(self):
        app = self.make_view()
        for game in ({"status": "champ_select", "phase": "ChampSelect", "is_aram": True},
                     {"status": "waiting", "phase": "GameStart", "is_aram": True},
                     {"status": "live", "phase": "InProgress", "is_aram": True},
                     {"status": "waiting", "phase": "Reconnect", "is_aram": True}):
            app.update_visibility(game)
            self.assertFalse(app.hidden)
        app.root.deiconify.assert_called_once()
        app.update_visibility({"status": "waiting", "phase": "EndOfGame", "is_aram": False})
        self.assertTrue(app.hidden)
        app.root.withdraw.assert_called_once()

    def test_manual_hide_survives_polls_and_resets_next_match(self):
        app = self.make_view()
        selected = {"status": "champ_select", "is_aram": True}
        app.update_visibility(selected)
        app.toggle_visible()
        app.update_visibility(selected)
        app.update_visibility({"status": "waiting", "phase": "GameStart", "is_aram": True})
        app.update_visibility({"status": "live", "is_aram": True})
        self.assertTrue(app.hidden)
        app.update_visibility({"status": "waiting", "phase": "Lobby", "is_aram": False})
        app.update_visibility(selected)
        self.assertFalse(app.hidden)

    def test_manual_open_outside_match_is_preserved_until_next_match_ends(self):
        app = self.make_view()
        app.toggle_visible()
        app.update_visibility({"status": "waiting", "phase": "None", "is_aram": False})
        self.assertFalse(app.hidden)
        app.update_visibility({"status": "champ_select", "is_aram": True})
        app.update_visibility({"status": "waiting", "phase": "Lobby", "is_aram": False})
        self.assertTrue(app.hidden)

    def test_metadata_failure_does_not_cancel_manual_hide_within_a_match(self):
        app = self.make_view()
        selection = {"status": "champ_select", "phase": "ChampSelect", "is_aram": True}
        app.update_visibility(selection)
        app.toggle_visible()
        app.update_visibility({"status": "champ_select", "phase": "ChampSelect", "is_aram": None})
        app.update_visibility({"status": "error", "phase": None, "is_aram": None})
        app.update_visibility(selection)
        self.assertTrue(app.hidden)
        app.root.deiconify.assert_called_once()
        # An authoritative idle phase, even without mode metadata, ends it.
        app.update_visibility({"status": "waiting", "phase": "None", "is_aram": None})
        app.update_visibility(selection)
        self.assertFalse(app.hidden)

    def test_explicit_browse_mode_remains_available_and_can_be_hidden(self):
        app = self.make_view()
        app.browse_mode = True
        app.update_visibility({"status": "waiting", "is_aram": False})
        self.assertFalse(app.hidden)
        app.toggle_visible()
        app.update_visibility({"status": "waiting", "is_aram": False})
        self.assertTrue(app.hidden)

    def test_disconnected_game_clears_previous_champion_and_rates(self):
        app = self.make_view()
        app.handle_game({"status": "waiting"})
        self.assertIsNone(app.champion)
        self.assertEqual(app.offer_ids, [None, None, None])
        for card in app.cards:
            card["value"].configure.assert_called_with(text="—", fg=TEXT)

    def test_champion_select_resolves_id_opens_spells_and_fetches_before_game(self):
        app = self.make_view()
        app.champion = app.live_champion = app.spell_champion = None
        app.handle_game({"status": "champ_select", "champion_id": "157",
                         "summoner_spells": [{"id": "4", "name": "Flash"}, {"id": "32", "name": "Mark"}]})
        self.assertEqual(app.champion, "157")
        self.assertFalse(app.game_live)
        self.assertTrue(app.champ_select)
        self.assertEqual(app.live_spells[0]["id"], "4")
        app.switch_view.assert_called_once_with("spells", automatic=True)
        self.assertEqual(app.badge.configure.call_args.kwargs["text"], "SELECT")
        self.assertEqual(app.requests.get_nowait(), ("spells", (8, "157")))

    def test_champion_trade_invalidates_old_pair_data_and_fetches_new_champion(self):
        app = self.make_view()
        app.catalog["champions"]["22"] = {"name": "Ashe", "riotId": "Ashe", "slug": "ashe"}
        app.game_status = "champ_select"
        app.handle_game({"status": "champ_select", "champion_id": "22"})
        self.assertEqual(app.champion, "22")
        self.assertEqual(app.requests.get_nowait(), ("spells", (8, "22")))
        app.handle_event("spells", (7, {"champion_id": "157", "rows": []}))
        self.assertIsNone(app.spell_data)

    def test_champion_select_without_pick_clears_previous_match(self):
        app = self.make_view()
        app.handle_game({"status": "champ_select", "champion_id": None})
        self.assertIsNone(app.champion)
        self.assertEqual(app.offer_ids, [None, None, None])
        app.switch_view.assert_called_once_with("spells", automatic=True)

    def test_unknown_champion_id_does_not_reuse_last_champion(self):
        app = self.make_view()
        app.handle_game({"status": "champ_select", "champion_id": "99999"})
        self.assertIsNone(app.champion)

    def test_tab_switch_happens_once_per_phase_and_manual_browse_is_preserved(self):
        app = self.make_view()
        selection = {"status": "champ_select", "champion_id": "157"}
        app.handle_game(selection)
        app.handle_game(selection)
        app.switch_view.assert_called_once_with("spells", automatic=True)
        app.handle_game({"status": "live", "champion_name": "Yasuo"})
        app.switch_view.assert_called_with("builds", automatic=True)
        app.follow = False
        app.switch_view.reset_mock()
        app.handle_game({"status": "champ_select", "champion_id": None})
        self.assertEqual(app.champion, "157")
        app.switch_view.assert_not_called()

    def test_spell_change_during_select_updates_current_pair_without_refetch(self):
        app = self.make_view()
        app.game_status = "champ_select"
        app.view.get.return_value = "spells"
        app.render_spells = Mock()
        app.handle_game({"status": "champ_select", "champion_id": "157",
                         "summoner_spells": [{"id": "4", "name": "Flash"}, {"id": "6", "name": "Ghost"}]})
        app.render_spells.assert_called_once()
        self.assertTrue(app.requests.empty())

    def test_return_to_auto_discards_manual_choices_and_requests_new_scan(self):
        app = self.make_view()
        app.follow = False
        app.auto_champion()
        self.assertTrue(app.follow)
        self.assertEqual(app.offer_ids, [None, None, None])
        self.assertEqual(app.requests.get_nowait(), ("scan", None))

    def test_stale_lookup_cannot_replace_current_selection(self):
        app = self.make_view()
        app.render_rates = Mock()
        app.handle_event("rates", (4, [{"win_rate": 1.0}]))
        app.render_rates.assert_not_called()

    def test_event_failure_still_schedules_next_drain(self):
        app = self.make_view()
        app.events.put(("game", {}))
        app.handle_event = Mock(side_effect=ValueError("bad event"))
        with self.assertLogs(level="ERROR"):
            app.drain()
        app.root.after.assert_called_once_with(100, app.drain)

    def test_missing_data_stays_blank_and_zero_rate_is_valid(self):
        app = self.make_view()
        app.render_rates([{"win_rate": None, "sample_count": 0},
                          {"win_rate": 0.0, "sample_count": 20}, None])
        app.cards[0]["value"].configure.assert_called_with(text="—", fg=MUTED)
        app.cards[1]["value"].configure.assert_called_with(text="0.0%", fg=TEXT)

    def test_highest_rate_emphasis_requires_a_comparison_and_sufficient_sample(self):
        for counts, expected in (((500, 600, 700), [0]), ((199, 600, 700), []),
                                 ((500, 0, 0), [])):
            with self.subTest(counts=counts):
                app = self.make_view()
                app.render_rates([{"win_rate": rate, "sample_count": count}
                                  for rate, count in zip((.80, .60, .55), counts)])
                emphasized = [i for i, card in enumerate(app.cards)
                              if card["rank"].configure.call_args.kwargs.get("text") == "HIGHEST WR"]
                self.assertEqual(emphasized, expected)
                for i, card in enumerate(app.cards):
                    card["frame"].configure.assert_called_with(
                        highlightbackground=ACCENT if i in expected else BORDER)

    def test_new_lookup_clears_old_winner_before_replacement_rates_arrive(self):
        app = self.make_view()
        app.stage.get.return_value = "All picks"
        app.render_rates([{"win_rate": .60, "sample_count": 500},
                          {"win_rate": .55, "sample_count": 500}, None])
        app.lookup()
        for card in app.cards:
            card["rank"].configure.assert_called_with(text="")
            card["frame"].configure.assert_called_with(highlightbackground=BORDER)
        self.assertEqual(app.requests.get_nowait(), ("lookup", (6, "157", ["1", "2", "3"], None)))

    def test_manual_edit_clears_comparison_and_rejects_pending_rates(self):
        app = self.make_view()
        app.augment_names = ["Goliath", "Tank Engine"]
        app.cards[1]["var"].get.return_value = "tank"
        app.cards[1]["box"] = MagicMock()
        app.render_rates([{"win_rate": .60, "sample_count": 500},
                          {"win_rate": .55, "sample_count": 500}, None])
        app.filter_augments(Mock(keysym="k"), 1)
        self.assertEqual(app.offer_ids, ["1", None, "3"])
        self.assertEqual(app.token, 6)
        self.assertFalse(app.view_automatic)
        for card in app.cards:
            card["rank"].configure.assert_called_with(text="")
            card["frame"].configure.assert_called_with(highlightbackground=BORDER)
        app.render_rates = Mock()
        app.handle_event("rates", (5, [{"win_rate": .99, "sample_count": 500}]))
        app.render_rates.assert_not_called()

    def test_confirmed_manual_augment_requests_rates_and_closes_editor(self):
        app = self.make_view()
        app.cards[0]["var"].get.return_value = " goliath "
        app.catalog["augments"] = {"1": {"name": "Goliath"}}
        app.augment_by_name = {"goliath": "1"}
        app.lookup, app.edit_augment = Mock(), Mock()
        app.select_augment(0)
        self.assertEqual(app.offer_ids, ["1", "2", "3"])
        self.assertFalse(app.is_auto_offer)
        app.cards[0]["var"].set.assert_called_once_with("Goliath")
        app.lookup.assert_called_once()
        app.edit_augment.assert_called_once_with(0, False)

    def test_unmatched_manual_augment_stays_open_for_correction(self):
        app = self.make_view()
        app.cards[0]["var"].get.return_value = "golia"
        app.augment_by_name = {"goliath": "1"}
        app.lookup, app.edit_augment = Mock(), Mock()
        app.select_augment(0)
        self.assertIsNone(app.offer_ids[0])
        self.assertNotIn(call(0, False), app.edit_augment.call_args_list)

    def test_champion_change_fetches_spells_without_any_augment_selected(self):
        app = self.make_view()
        app.offer_ids = [None] * 3
        app.champion = "41"
        app.lookup()
        self.assertEqual(app.requests.get_nowait(), ("spells", (8, "41")))
        self.assertEqual(app.requests.get_nowait(), ("builds", (10, "41")))
        app.lookup()
        self.assertTrue(app.requests.empty())

    def test_champion_edit_clears_spells_and_invalidates_inflight_result(self):
        app = self.make_view()
        app.champion = None
        app.lookup_spells()
        self.assertEqual(app.spell_token, 8)
        app.handle_event("spells", (7, {"champion_id": "157", "rows": []}))
        self.assertIsNone(app.spell_data)
        app.spell_tree.delete.assert_called_once()

    def test_spell_response_must_match_champion_and_request(self):
        app = self.make_view()
        app.render_spells = Mock()
        app.view.get.return_value = "spells"
        app.handle_event("spells", (7, {"champion_id": "41", "rows": []}))
        app.render_spells.assert_not_called()
        app.handle_event("spells", (7, {"champion_id": "157", "rows": []}))
        app.render_spells.assert_called_once()

    def test_spell_pairs_highlight_order_independently_and_mark_low_sample(self):
        app = self.make_view()
        app.live_spells = [{"id": "32"}, {"id": "4"}]
        app.spell_data = {"rows": [
            {"spell_ids": ["4", "32"], "spell_names": ["Flash", "Mark"], "win_rate": .58, "sample_count": 1000, "pick_rate": .9},
            {"spell_ids": ["4", "6"], "spell_names": ["Flash", "Ghost"], "win_rate": .75, "sample_count": 4, "pick_rate": .001},
        ]}
        app.render_spells()
        inserts = app.spell_tree.insert.call_args_list
        self.assertEqual(inserts[0].kwargs["values"][0], "Flash + Ghost")
        self.assertEqual(inserts[0].kwargs["values"][2], "4*")
        self.assertEqual(inserts[1].kwargs["tags"], ("current",))
        self.assertIn("Your current pair", app.spell_summary.set.call_args.args[0])

    def test_spell_zero_samples_are_blank_and_unknown_pair_does_not_highlight(self):
        app = self.make_view()
        app.live_spells = [{"id": None}, {"id": "4"}]
        app.spell_data = {"rows": [
            {"spell_ids": ["4", "6"], "spell_names": ["Flash", "Ghost"], "win_rate": None, "sample_count": 0, "pick_rate": None},
        ]}
        app.render_spells()
        result = app.spell_tree.insert.call_args.kwargs
        self.assertEqual(result["values"][1], "—")
        self.assertNotIn("current", result["tags"])

    def test_spell_pair_highlights_during_champion_select(self):
        app = self.make_view()
        app.game_live = False
        app.champ_select = True
        app.live_spells = [{"id": "32", "name": "Mark"}, {"id": "4", "name": "Flash"}]
        app.spell_data = {"rows": [
            {"spell_ids": ["4", "32"], "spell_names": ["Flash", "Mark"], "win_rate": .58,
             "sample_count": 1000, "pick_rate": .9}]}
        app.render_spells()
        self.assertEqual(app.spell_tree.insert.call_args.kwargs["tags"], ("current",))
        self.assertEqual(app.status.set.call_args.args[0], "Champion select · Mark + Flash")

    def test_background_augment_response_does_not_replace_spells_source_date(self):
        app = self.make_view()
        app.view.get.return_value = "spells"
        app.render_rates([{"win_rate": .5, "sample_count": 200, "version": "old"}])
        app.source.set.assert_not_called()

    def test_auto_scan_still_populates_choices_for_manual_champion(self):
        app = self.make_view()
        app.follow = False
        app.catalog["augments"] = {"1": {"name": "Goliath"}, "2": {"name": "Tank Engine"}}
        app.lookup = Mock()
        app.handle_event("offers", [{"id": "1"}, {"id": "2"}])
        self.assertEqual(app.offer_ids, ["1", "2", None])
        app.lookup.assert_called_once()

    def test_scan_paused_message_is_visible_with_retained_choices(self):
        app = self.make_view()
        app.handle_event("scan_status", "Scan paused · Showing last recognized choices.")
        app.status.set.assert_called_once()
        self.assertEqual(app.offer_ids, ["1", "2", "3"])

    def test_locked_panel_still_discloses_paused_scan(self):
        app = self.make_view()
        app.locked = True
        app.handle_event("scan_status", "Scan paused · Showing last recognized choices.")
        message = app.status.set.call_args.args[0]
        self.assertIn("Scan paused", message)
        self.assertIn("Ctrl+Alt+L to unlock", message)
        self.assertEqual(app.offer_ids, ["1", "2", "3"])

    def test_confirmed_choice_boundaries_switch_once_and_rerolls_keep_manual_tab(self):
        app = self.make_view()
        app.catalog["augments"] = {"1": {"name": "Goliath"}, "2": {"name": "Tank Engine"}}
        app.offers_active = False
        app.view.get.return_value = "builds"
        app.lookup = Mock()
        app.handle_event("offers", [{"id": "1"}])
        app.switch_view.assert_called_once_with("augments", automatic=True)
        app.switch_view.reset_mock()
        # A reroll updates rates without pulling the user off another tab.
        app.handle_event("offers", [{"id": "2"}])
        app.switch_view.assert_not_called()
        app.lookup.assert_called_once()
        app.view.get.return_value = "augments"
        app.handle_event("offers", [])
        app.switch_view.assert_called_once_with("builds", automatic=True)
        app.switch_view.reset_mock()
        app.handle_event("offers", [])
        app.switch_view.assert_not_called()

    def test_manual_tab_or_manual_champion_is_not_changed_when_choices_end(self):
        for manual_view, follow in ((True, True), (False, False)):
            app = self.make_view()
            app.view_automatic = not manual_view
            app.follow = follow
            app.handle_event("offers", [])
            app.switch_view.assert_not_called()

    def test_manual_augment_edit_does_not_rearm_choice_detection(self):
        app = self.make_view()
        app.catalog["augments"] = {"1": {"name": "Goliath"}}
        app.is_auto_offer = False
        app.view_automatic = False
        app.lookup = Mock()
        app.handle_event("offers", [{"id": "1"}])
        app.switch_view.assert_not_called()
        app.handle_event("offers", [])
        app.switch_view.assert_not_called()

    def test_manual_champion_receives_choices_without_changing_build_tab(self):
        app = self.make_view()
        app.follow = app.offers_active = False
        app.view.get.return_value = "builds"
        app.catalog["augments"] = {"1": {"name": "Goliath"}}
        app.lookup = Mock()
        app.handle_event("offers", [{"id": "1"}])
        app.switch_view.assert_not_called()
        self.assertEqual(app.offer_ids, ["1", None, None])

    def test_build_response_requires_token_and_champion_and_does_not_change_tabs(self):
        app = self.make_view()
        app.render_builds = Mock()
        app.view.get.return_value = "builds"
        data = {"champion_id": "157", "groups": []}
        app.handle_event("builds", (8, data))
        app.handle_event("builds", (9, {"champion_id": "41", "groups": []}))
        self.assertIsNone(app.build_data)
        app.handle_event("builds", (9, data))
        app.render_builds.assert_called_once()
        app.switch_view.assert_not_called()
        app.view.get.return_value = "augments"
        app.render_builds.reset_mock()
        app.handle_event("builds", (9, data))
        app.render_builds.assert_not_called()
        app.source.set.assert_not_called()

    def test_champion_change_invalidates_builds_immediately_and_fetches_once(self):
        app = self.make_view()
        app.champion = "41"
        app.lookup_builds()
        app.lookup_builds()
        self.assertEqual(app.requests.get_nowait(), ("builds", (10, "41")))
        self.assertTrue(app.requests.empty())
        app.handle_event("builds", (9, {"champion_id": "157", "groups": []}))
        self.assertIsNone(app.build_data)
        for card in app.build_cards:
            card["frame"].pack_forget.assert_called_once()

    def test_build_cards_default_to_games_and_keep_sets_low_samples_and_source_clear(self):
        app = self.make_view()
        app.view.get.return_value = "builds"
        app.build_data = {"version": "16.21", "data_date": "2026-10-25", "stale": True, "groups": [
            {"label": "AP", "rows": [
                {"item_names": ["Rare One", "Rare Two", "Rare Three"], "win_rate": .99,
                 "sample_count": 10, "pick_rate": .001},
                {"item_names": ["Liandry's Torment", "Blackfire Torch", "Rylai's Crystal Scepter"],
                 "win_rate": .52, "sample_count": 10000, "pick_rate": .6},
                {"item_names": ["A", "B", "C"], "win_rate": None, "sample_count": 0, "pick_rate": None}]}]}
        app.render_builds()
        self.assertIn("Liandry", app.build_cards[0]["items"][0]["name"].configure.call_args.kwargs["text"])
        app.build_cards[0]["value"].configure.assert_called_with(text="52.0%")
        self.assertIn("60.0% of AP builds", app.build_cards[0]["meta"].configure.call_args.kwargs["text"])
        self.assertIn("LOW SAMPLE", app.build_cards[1]["meta"].configure.call_args.kwargs["text"])
        app.build_cards[2]["value"].configure.assert_called_with(text="—")
        app.build_cards[3]["frame"].pack.assert_not_called()
        self.assertIn("cached/offline", app.source.set.call_args.args[0])
        app.icons.request.assert_not_called()

    def test_build_icons_use_cached_images_and_request_missing_ones_without_hiding_names(self):
        app = self.make_view()
        cached = object()
        urls = ["https://data.aramkit.com/assets/item/1-aaa.webp", None,
                "https://data.aramkit.com/assets/item/3-bbb.webp"]
        app.item_images[urls[0]] = cached
        app.build_data = {"groups": [{"label": "AP", "rows": [
            {"item_names": ["First", "Second", "Third"], "item_icon_urls": urls,
             "win_rate": .52, "sample_count": 1000, "pick_rate": .6}]}]}
        app.render_builds()
        items = app.build_cards[0]["items"]
        items[0]["icon"].configure.assert_called_with(image=cached)
        items[1]["icon"].configure.assert_called_with(image="")
        items[1]["name"].configure.assert_called_with(text="Second")
        app.icons.request.assert_called_once_with(urls[2])

    def followup_view(self):
        app = self.make_view()
        app.view.get.return_value = "builds"
        app.inventory_champion = "157"
        app.inventory_current = True
        app.live_items = ["1", "2", "3"]
        # StringVars behave like the real widgets so subsequent renders retain
        # the user's core and order choices.
        for variable, initial in ((app.build_group, "AP"), (app.build_order, "")):
            variable.get.return_value = initial
            variable.set.side_effect = lambda value, variable=variable: setattr(variable.get, "return_value", value)
        later = lambda ident, name, count, rate: {
            "item_ids": [ident], "item_names": [name], "item_icon_urls": [None],
            "sample_count": count, "win_rate": rate, "pick_rate": .3}
        core = {"item_ids": ["1", "2", "3"], "item_names": ["One", "Two", "Three"],
                "sample_count": 1000, "win_rate": .51, "pick_rate": .8}
        app.build_data = {"groups": [{"label": "AP", "rows": [{**core, "routes": [
            {**core, "later_items": [later("4", "Four", 400, .56), later("5", "Five", 100, .6)]},
            {**core, "item_ids": ["3", "2", "1"], "item_names": ["Three", "Two", "One"],
             "sample_count": 200, "later_items": [later("6", "Six", 90, .58)]}]}]}],
            "item_catalog": {str(i): {"is_final": True, "is_boots": False, "canonical_id": str(i)}
                             for i in range(1, 7)}}
        app.build_note.get.side_effect = lambda: app.build_note.set.call_args.args[0]
        return app

    def test_three_completed_items_switch_to_matching_followups_with_true_item_rates(self):
        app = self.followup_view()
        app.render_builds()
        self.assertIn("Follow-up items", app.build_summary.set.call_args.args[0])
        app.build_cards[0]["items"][0]["name"].configure.assert_called_with(text="Four")
        app.build_cards[0]["value"].configure.assert_called_with(text="56.0%")
        app.build_cards[0]["items"][1]["frame"].pack_forget.assert_called()
        self.assertIn("LOW SAMPLE", app.build_cards[1]["meta"].configure.call_args.kwargs["text"])
        self.assertIn("Most played", app.build_order.get())
        app.build_core_items[0]["name"].configure.assert_called_with(text="1. One")
        self.assertIn("of this order", app.build_cards[0]["meta"].configure.call_args.kwargs["text"])

    def test_boots_components_and_unknown_items_do_not_trigger_followups(self):
        app = self.followup_view()
        app.build_data["item_catalog"]["4"]["is_boots"] = True
        app.build_data["item_catalog"]["5"]["is_final"] = False
        app.live_items = ["1", "2", "4", "5", "999"]
        self.assertEqual(app.completed_items(), ["1", "2"])
        app.render_builds()
        self.assertIn("Core builds", app.build_summary.set.call_args.args[0])

    def test_owned_and_transformed_followup_items_are_removed_after_purchase(self):
        app = self.followup_view()
        app.build_data["item_catalog"]["44"] = {"is_final": True, "canonical_id": "4"}
        app.render_builds()
        app.handle_game({"status": "live", "champion_id": "157", "item_ids": ["1", "2", "3", "44"]})
        app.build_cards[0]["items"][0]["name"].configure.assert_called_with(text="Five")
        self.assertIn("4 completed", app.build_summary.set.call_args.args[0])
        app.requests.queue.clear()
        app.handle_game({"status": "live", "champion_id": "157", "item_ids": ["1", "2", "3"]})
        app.build_cards[0]["items"][0]["name"].configure.assert_called_with(text="Four")
        self.assertTrue(app.requests.empty(), "Inventory changes must not refetch champion statistics")

    def test_route_selector_uses_that_routes_rates_and_survives_inventory_update(self):
        app = self.followup_view()
        app.render_builds()
        selected = app.build_order_box.__setitem__.call_args.args[1]
        app.build_order.set(selected[1])
        app.render_builds()
        app.build_core_items[0]["name"].configure.assert_called_with(text="1. Three")
        app.build_cards[0]["items"][0]["name"].configure.assert_called_with(text="Six")
        app.build_cards[0]["value"].configure.assert_called_with(text="58.0%")
        app.handle_game({"status": "live", "champion_id": "157", "item_ids": ["1", "2", "3", "4"]})
        self.assertEqual(app.build_order.get(), selected[1])

    def test_no_matching_core_and_all_owned_followups_do_not_show_unrelated_rates(self):
        app = self.followup_view()
        app.live_items = ["1", "4", "5"]
        app.render_builds()
        self.assertIn("No follow-up data matches", app.build_summary.set.call_args.args[0])
        app.build_cards[0]["frame"].pack.assert_not_called()
        app.live_items = ["1", "2", "3", "4", "5"]
        app.render_builds()
        self.assertIn("No unowned", app.build_summary.set.call_args.args[0])
        app.build_cards[0]["frame"].pack.assert_not_called()

    def test_missing_inventory_preserves_last_snapshot_but_explicit_empty_clears_it(self):
        app = self.followup_view()
        app.handle_game({"status": "live", "champion_id": "157", "item_ids": None})
        self.assertEqual(app.live_items, ["1", "2", "3"])
        self.assertIn("last known items", app.build_note.get())
        app.handle_game({"status": "live", "champion_id": "157", "item_ids": []})
        self.assertEqual(app.live_items, [])
        self.assertIn("Core builds", app.build_summary.set.call_args.args[0])
        app.handle_game({"status": "waiting", "phase": "EndOfGame"})
        self.assertIsNone(app.live_items)
        self.assertIsNone(app.inventory_champion)

    def test_transformed_source_core_matches_the_same_canonical_inventory(self):
        app = self.followup_view()
        app.build_data["item_catalog"]["11"] = {"is_final": True, "canonical_id": "1"}
        app.build_data["groups"][0]["rows"][0]["item_ids"] = ["11", "2", "3"]
        app.render_builds()
        self.assertIn("Follow-up items", app.build_summary.set.call_args.args[0])

    def test_manual_other_champion_does_not_use_live_inventory(self):
        app = self.followup_view()
        app.champion = "41"
        app.follow = False
        app.render_builds()
        self.assertIn("Core builds", app.build_summary.set.call_args.args[0])

    def test_delayed_icon_updates_only_slots_still_showing_that_item(self):
        app = self.make_view()
        app.build_cards[0]["items"][0]["icon_url"] = "new-url"
        app.build_cards[1]["items"][2]["icon_url"] = "old-url"
        source = Mock()
        photo = object()
        with patch("mayhem.app.ImageTk.PhotoImage", return_value=photo):
            app.handle_event("item_icon", ("old-url", source))
        self.assertIs(app.item_images["old-url"], photo)
        app.build_cards[0]["items"][0]["icon"].configure.assert_not_called()
        app.build_cards[1]["items"][2]["icon"].configure.assert_called_once_with(image=photo)
        app.switch_view.assert_not_called()
        app.source.set.assert_not_called()

    def test_unrenderable_icon_does_not_clear_build_rates_or_break_event_loop(self):
        app = self.make_view()
        with patch("mayhem.app.ImageTk.PhotoImage", side_effect=RuntimeError("image unavailable")):
            app.handle_event("item_icon", ("url", Mock()))
        self.assertFalse(app.item_images)
        app.build_cards[0]["value"].configure.assert_not_called()

    def test_build_error_cannot_clear_a_newer_champions_data(self):
        app = self.make_view()
        app.build_data = {"champion_id": "157", "groups": []}
        app.handle_event("builds_error", (8, "old failure"))
        self.assertIsNotNone(app.build_data)
        app.handle_event("builds_error", (9, "current failure"))
        self.assertIsNone(app.build_data)
        self.assertIn("unavailable", app.build_summary.set.call_args.args[0])

    def test_build_view_keeps_scan_failure_visible_without_navigation(self):
        app = self.make_view()
        app.view.get.return_value = "builds"
        app.handle_event("scan_status", "Scan paused · focus League to resume.")
        self.assertIn("Scan paused", app.status.set.call_args.args[0])
        app.switch_view.assert_not_called()

    def test_entering_pending_build_page_clears_another_pages_source(self):
        app = self.make_view()
        app.view.get.return_value = "builds"
        app.render_builds()
        app.source.set.assert_called_with("ARAMKit · core-build data")

    def test_temporary_poll_failure_keeps_manual_tab_but_match_end_rearms_builds(self):
        app = self.make_view()
        app.view_automatic = False
        app.view.get.return_value = "spells"
        app.handle_game({"status": "waiting"})
        app.handle_game({"status": "live", "champion_name": "Yasuo"})
        app.switch_view.assert_not_called()
        app.handle_game({"status": "waiting", "phase": "EndOfGame"})
        app.handle_game({"status": "live", "champion_name": "Yasuo"})
        app.switch_view.assert_called_once_with("builds", automatic=True)


if __name__ == "__main__":
    unittest.main()
