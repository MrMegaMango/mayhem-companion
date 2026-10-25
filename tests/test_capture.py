"""Pure geometry tests; no game or desktop is captured by this test suite."""
import unittest
from types import SimpleNamespace

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


if __name__ == "__main__":
    unittest.main()
