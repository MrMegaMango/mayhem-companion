"""Feedback stays local until the player opens and sends their own draft."""
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from mayhem import __version__
from mayhem.feedback import (FEEDBACK_EMAIL, MAX_MAILTO_LENGTH, build_report,
                             email_draft, feedback_mode, open_email_draft,
                             technical_details)


class FeedbackTests(unittest.TestCase):
    def test_report_includes_only_opted_in_allowlisted_details(self):
        details = technical_details(game_state="live", view="builds", auto_scan=True,
                                    locked=False, portrait=True)
        details.update(token="secret-token", summoner="private-player", log="private-log")
        private = build_report("Bug", "Cards are blank", mode="Arena", diagnostics=details)
        shared = build_report("Bug", "Cards are blank", include_diagnostics=True,
                              mode="Arena", diagnostics=details)
        self.assertIn(f"App version: {__version__}", private)
        self.assertIn("Game mode: Arena", private)
        self.assertNotIn("Technical details", private)
        self.assertIn("Auto scan: On", shared)
        for value in ("secret-token", "private-player", "private-log"):
            self.assertNotIn(value, shared)

    def test_unrecognized_state_text_is_not_exported(self):
        details = technical_details(game_state="error with credentials", view="private path",
                                    auto_scan=False, locked=False, portrait=False)
        self.assertEqual(details["Game state"], "Other / unavailable")
        self.assertEqual(details["View"], "Other / unavailable")

    def test_mailto_encodes_description_as_body_without_extra_recipients(self):
        body = build_report("Bug", "Text &bcc=other@example.com\nUnicode: 英雄 #1?",
                            "reply@example.com", mode="ARAM Mayhem")
        uri, needs_paste = email_draft("Bug", "ARAM Mayhem", body)
        parsed = urlsplit(uri)
        query = parse_qs(parsed.query)
        self.assertEqual(parsed.scheme, "mailto")
        self.assertEqual(parsed.path, FEEDBACK_EMAIL)
        self.assertEqual(set(query), {"subject", "body"})
        self.assertEqual(query["body"][0].replace("\r\n", "\n"), body)
        self.assertFalse(needs_paste)

    def test_long_unicode_report_opens_short_draft_requiring_full_clipboard_report(self):
        body = build_report("Incorrect stats", "数" * 1000, mode="TFT")
        uri, needs_paste = email_draft("Incorrect stats", "TFT", body)
        self.assertTrue(needs_paste)
        self.assertLessEqual(len(uri), MAX_MAILTO_LENGTH)
        self.assertIn("clipboard", parse_qs(urlsplit(uri).query)["body"][0])

    def test_rejects_empty_or_oversized_message_and_multiline_contact(self):
        for description in (" \n", "a" * 6001):
            with self.assertRaises(ValueError):
                build_report("Bug", description, mode="ARAM")
        with self.assertRaises(ValueError):
            build_report("Bug", "message", "reply@example.com\nBCC: other", mode="ARAM")

    def test_context_distinguishes_mayhem_and_respects_manually_selected_mode(self):
        self.assertEqual(feedback_mode("aram", {"queue_id": 2400}), "ARAM Mayhem")
        self.assertEqual(feedback_mode("aram", {"game_mode": "KIWI"}), "ARAM Mayhem")
        self.assertEqual(feedback_mode("aram", {"queue_id": 450}), "ARAM")
        self.assertEqual(feedback_mode("arena", {"queue_id": 2400}), "Arena")
        self.assertEqual(feedback_mode("tft", {}), "TFT")

    def test_email_handler_failure_is_reported_without_sending_or_retry(self):
        uri, _ = email_draft("Bug", "ARAM", "Test report")
        with patch("mayhem.feedback.os.name", "nt"), \
                patch("mayhem.feedback.os.startfile", create=True, side_effect=OSError) as opener:
            self.assertFalse(open_email_draft(uri))
        opener.assert_called_once_with(uri)
        with self.assertRaises(ValueError):
            open_email_draft("https://example.com")


class FeedbackIntegrationTests(unittest.TestCase):
    def make_app(self):
        from mayhem.app import Companion
        app = object.__new__(Companion)
        app.root = Mock()
        app.display_mode = "arena"
        app.last_game_context = {"is_arena": True, "token": "NEVER SHARE"}
        app.game_status = "live"
        app.view = Mock()
        app.view.get.return_value = "augments"
        app.arena_panel = Mock()
        app.arena_panel.view.get.return_value = "items"
        app.tft_panel = Mock()
        app.tft_panel.view.get.return_value = "comps"
        app.auto = Mock()
        app.auto.get.return_value = True
        app.locked = True
        app.settings = {"display_preference": "portrait", "position": [-464, 313]}
        app._last_display = {"device": "portrait", "bounds": (-1080, 0, 0, 1920),
                             "work_area": (-1080, 0, 0, 1880)}
        app.panel_placement = Mock(return_value=(-464, 313, 780))
        app.feedback_dialog = None
        return app

    def test_opening_feedback_does_not_open_mail_or_unlock_or_show_companion(self):
        app = self.make_app()
        with patch("mayhem.app.FeedbackDialog") as dialog, patch("mayhem.app.open_email_draft") as mail:
            app.show_feedback()
            mail.assert_not_called()
            kwargs = dialog.call_args.kwargs
            self.assertEqual(kwargs["mode"], "Arena")
            self.assertLess(kwargs["position"][0], 0)
            self.assertNotIn("NEVER SHARE", str(kwargs["diagnostics"]))
            self.assertEqual(kwargs["diagnostics"]["View"], "items")
            self.assertTrue(app.locked)
            app.root.deiconify.assert_not_called()
            app.show_feedback()
            dialog.assert_called_once()

    def test_tft_diagnostics_use_tft_view_and_game_state(self):
        app = self.make_app()
        app.display_mode = "tft"
        app.last_game_context = {"status": "tft"}
        with patch("mayhem.app.FeedbackDialog") as dialog:
            app.show_feedback()
        self.assertEqual(dialog.call_args.kwargs["diagnostics"]["View"], "comps")
        self.assertEqual(dialog.call_args.kwargs["diagnostics"]["Game state"], "tft")

    def test_long_report_is_copied_in_full_before_requesting_email_draft(self):
        app = self.make_app()
        with patch("mayhem.app.FeedbackDialog") as dialog, patch("mayhem.app.open_email_draft", return_value=True) as mail:
            app.show_feedback()
            submit = dialog.call_args.kwargs["submit"]
            success, message = submit("Bug", "数" * 1000, "", False)
            self.assertTrue(success)
            self.assertIn("Paste", message)
            self.assertIn("数" * 1000, app.root.clipboard_append.call_args.args[0])
            mail.assert_called_once()

    def test_missing_mail_handler_preserves_form_and_gives_manual_delivery_fallback(self):
        app = self.make_app()
        with patch("mayhem.app.FeedbackDialog") as dialog, patch("mayhem.app.open_email_draft", return_value=False):
            app.show_feedback()
            success, message = dialog.call_args.kwargs["submit"]("Bug", "Blank cards", "", False)
            self.assertFalse(success)
            self.assertIn(FEEDBACK_EMAIL, message)
            self.assertIn("Copy report", message)
            app.feedback_dialog.destroy.assert_not_called()

    def test_tray_feedback_is_handled_on_app_thread(self):
        app = self.make_app()
        app.show_feedback = Mock()
        app.handle_event("tray_action", "feedback")
        app.show_feedback.assert_called_once()


if __name__ == "__main__":
    unittest.main()
