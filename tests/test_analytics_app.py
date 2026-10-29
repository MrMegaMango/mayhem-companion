"""Quiet startup and usage boundaries use fake views and never send HTTP."""
import unittest
from unittest.mock import Mock, patch

import test_app


class AnalyticsAppTests(unittest.TestCase):
    def setUp(self):
        style = patch("mayhem.app.set_window_style")
        style.start()
        self.addCleanup(style.stop)

    def view(self):
        app = test_app.StateTests().make_view()
        app.analytics = Mock()
        app.analytics.status.return_value = {"configured": True, "consent": "unset"}
        app.settings = {"display_preference": "portrait", "position": [-464, 313]}
        app.analytics_dialog = None
        return app

    def test_game_phases_never_open_a_usage_prompt(self):
        for game in ({"status": "waiting", "phase": "Lobby"},
                     {"status": "waiting", "phase": "Matchmaking"},
                     {"status": "champ_select", "phase": "ChampSelect", "is_aram": True},
                     {"status": "live", "phase": "InProgress", "is_aram": True},
                     {"status": "waiting", "phase": "EndOfGame"}):
            with self.subTest(game=game):
                app = self.view()
                app.show_analytics = Mock()
                app.handle_game(game)
                for scheduled in app.root.after_idle.call_args_list:
                    scheduled.args[0]()
                app.show_analytics.assert_not_called()

    def test_manual_panel_open_does_not_prompt_or_change_sharing(self):
        app = self.view()
        app.show_analytics = Mock()
        app.set_manual_visible(True)
        self.assertFalse(app.hidden)
        app.show_analytics.assert_not_called()
        app.analytics.set_enabled.assert_not_called()
        app.root.after_idle.assert_not_called()

    def test_first_automatic_arena_visibility_is_not_mislabeled_aram(self):
        app = self.view()
        app.display_mode = "aram"
        app.last_game_context = {"is_arena": True, "status": "champ_select"}
        app.set_visible(True)
        app.analytics.set_usage.assert_called_with(True, "arena")
        app.set_visible(False)
        app.analytics.set_usage.assert_called_with(False, "arena")

    def test_aram_after_other_modes_uses_current_context(self):
        for previous in ("arena", "tft"):
            app = self.view()
            app.display_mode = previous
            app.last_game_context = {"is_aram": True, "queue_id": 2400, "status": "champ_select"}
            app.set_visible(True)
            app.analytics.set_usage.assert_called_with(True, "mayhem")

    def test_invalid_endpoint_still_opens_disabled_privacy_controls(self):
        app = self.view()
        app.analytics.endpoint = ""
        app.analytics.status.return_value = {"configured": False, "consent": "unset"}
        app.panel_placement = Mock(return_value=(-464, 313, 700))
        app._last_display = {"work_area": (-1080, 0, 0, 1920)}
        with patch("mayhem.app.load_analytics_endpoint", return_value="https://["), \
             patch("mayhem.app.AnalyticsDialog") as dialog:
            app.show_analytics()
        self.assertEqual(dialog.call_args.kwargs["endpoint_label"], "Not configured")
        self.assertTrue(app.hidden)

    def test_manual_mode_and_mayhem_variant_are_preserved(self):
        app = self.view()
        app.last_game_context = {"queue_id": 2400, "game_mode": "KIWI"}
        self.assertEqual(app.analytics_mode(), "mayhem")
        app.display_mode = "tft"
        app.mode_choice.get.return_value = "TFT"
        self.assertEqual(app.analytics_mode(), "tft")

    def test_tray_privacy_does_not_enable_sharing(self):
        app = self.view()
        app.show_analytics = Mock()
        app.handle_event("tray_action", "privacy")
        app.show_analytics.assert_called_once()
        app.analytics.set_enabled.assert_not_called()

    def test_shutdown_finishes_analytics_without_a_network_call_on_ui_thread(self):
        app = self.view()
        app.stop = Mock()
        app.settings_path = Mock()
        app.root.winfo_x.return_value = -464
        app.root.winfo_y.return_value = 313
        app.close()
        app.analytics.close.assert_called_once()
        app.root.destroy.assert_called_once()


if __name__ == "__main__":
    unittest.main()
