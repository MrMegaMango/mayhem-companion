"""Pure geometry tests; no game or desktop is captured by this test suite."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from PIL import Image

from mayhem.capture import _line_bounds, _scaled_size, _word_groups, CaptureReader


class CaptureGeometryTests(unittest.TestCase):
    def test_resizing_preserves_aspect_and_does_not_upscale(self):
        self.assertEqual(_scaled_size(3840, 2160, 2500), (2500, 1406))
        self.assertEqual(_scaled_size(1920, 1080, 2500), (1920, 1080))

    def test_ocr_line_combines_every_word(self):
        words = [SimpleNamespace(bounding_rect=SimpleNamespace(
            x=x, y=y, width=w, height=h))
            for x, y, w, h in [(200, 100, 50, 20), (260, 102, 100, 22)]]
        bounds = _line_bounds(words, 1000, 500)
        self.assertEqual(bounds, {"x": 0.2, "y": 0.2, "width": 0.16, "height": 0.048})

    def test_empty_line_is_not_a_card(self):
        self.assertIsNone(_line_bounds([], 1000, 500))

    def test_same_row_titles_split_at_card_gaps(self):
        words = [SimpleNamespace(text=text, bounding_rect=SimpleNamespace(
            x=x, y=100, width=width, height=20))
            for text, x, width in [("Draw", 20, 50), ("Your", 80, 45),
                                   ("Sword", 135, 65), ("Goliath", 500, 80)]]
        groups = _word_groups(words)
        self.assertEqual([[word.text for word in group] for group in groups],
                         [["Draw", "Your", "Sword"], ["Goliath"]])

    def test_crop_excludes_desktop_and_most_hud(self):
        x0, y0, x1, y1 = CaptureReader.CROP
        self.assertTrue(0 < x0 < x1 < 1)
        self.assertTrue(0 < y0 < y1 < 1)


class CaptureStateTests(unittest.TestCase):
    def make_reader(self):
        reader = CaptureReader()
        user32 = Mock()
        user32.GetForegroundWindow.return_value = 7
        reader._load_windows = Mock(return_value=(user32, Mock()))
        reader._game_rectangle = Mock(return_value=(7, (0, 0, 1920, 1080)))
        patcher = patch("PIL.ImageGrab.grab", return_value=Image.new("RGB", (1766, 832)))
        reader.grab = patcher.start()
        self.addCleanup(patcher.stop)
        reader.recognize_image = Mock(return_value={"status": "ok", "message": "", "lines": []})
        return reader, user32

    def test_empty_ocr_is_capture_failure_not_empty_offer_screen(self):
        reader, _ = self.make_reader()
        result = reader.scan()
        self.assertEqual(result["status"], "unreadable")
        self.assertIn("Retrying", result["message"])

    def test_focus_switch_discards_captured_frame(self):
        reader, user32 = self.make_reader()
        user32.GetForegroundWindow.return_value = 8
        self.assertEqual(reader.scan()["status"], "waiting")
        reader.recognize_image.assert_not_called()

    def test_capture_does_not_start_without_game_focus(self):
        reader, _ = self.make_reader()
        reader._game_rectangle.return_value = None
        self.assertEqual(reader.scan()["status"], "waiting")
        reader.grab.assert_not_called()

    def test_title_band_is_read_before_complex_full_screen(self):
        reader, _ = self.make_reader()
        reader.recognize_image.return_value = {"status": "ok", "lines": [
            {"text": "Pinball", "x": .5, "y": .5, "width": .1, "height": .1}]}
        result = reader.scan()
        image = reader.recognize_image.call_args.args[0]
        self.assertEqual(image.size, (1344, 135))
        reader.recognize_image.assert_called_once()
        self.assertAlmostEqual(result["lines"][0]["x"], .5)
        self.assertAlmostEqual(result["lines"][0]["y"], (367 + 67.5) / 1080)

    def test_empty_title_band_tries_wider_layout(self):
        reader, _ = self.make_reader()
        reader.recognize_image.side_effect = [
            {"status": "ok", "lines": []},
            {"status": "ok", "lines": [{"text": "Pinball", "x": .5, "y": .5, "width": .1, "height": .1}]}]
        result = reader.scan()
        self.assertEqual(reader.recognize_image.call_count, 2)
        self.assertEqual(result["status"], "ok")


if __name__ == "__main__":
    unittest.main()
