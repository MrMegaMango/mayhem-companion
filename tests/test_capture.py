"""Pure geometry tests; no game or desktop is captured by this test suite."""
import ctypes
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, call, patch
from PIL import Image

from mayhem.capture import (
    _BitmapInfo, _line_bounds, _scaled_size, _word_groups, CaptureReader,
)


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
        reader.grab = reader._grab_region = Mock(side_effect=lambda box: Image.new(
            "RGB", (box[2] - box[0], box[3] - box[1])))
        reader.recognize_image = Mock(return_value={"status": "ok", "message": "", "lines": []})
        return reader, user32

    def test_empty_ocr_is_capture_failure_not_empty_offer_screen(self):
        reader, _ = self.make_reader()
        result = reader.scan()
        self.assertEqual(result["status"], "unreadable")
        self.assertIn("Retrying", result["message"])

    def test_focus_switch_discards_captured_frame(self):
        reader, user32 = self.make_reader()
        user32.GetForegroundWindow.side_effect = [7, 8]
        self.assertEqual(reader.scan()["status"], "waiting")
        reader.grab.assert_called_once()
        reader.recognize_image.assert_not_called()

    def test_focus_switch_before_grab_prevents_capture(self):
        reader, user32 = self.make_reader()
        user32.GetForegroundWindow.return_value = 8
        self.assertEqual(reader.scan()["status"], "waiting")
        reader.grab.assert_not_called()

    def test_focus_switch_before_fallback_prevents_wider_capture(self):
        reader, user32 = self.make_reader()
        user32.GetForegroundWindow.side_effect = [7, 7, 8]
        self.assertEqual(reader.scan()["status"], "waiting")
        reader.grab.assert_called_once()
        reader.recognize_image.assert_called_once()

    def test_focus_switch_during_fallback_discards_wider_frame(self):
        reader, user32 = self.make_reader()
        user32.GetForegroundWindow.side_effect = [7, 7, 7, 8]
        self.assertEqual(reader.scan()["status"], "waiting")
        self.assertEqual(reader.grab.call_count, 2)
        reader.recognize_image.assert_called_once()

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
        reader.grab.assert_called_once_with((288, 367, 1632, 502))
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
        self.assertEqual(reader.grab.call_args_list,
                         [call((288, 367, 1632, 502)), call((77, 86, 1843, 918))])
        self.assertEqual(result["status"], "ok")
        self.assertAlmostEqual(result["lines"][0]["x"], .5)
        self.assertAlmostEqual(result["lines"][0]["y"], (86 + 416) / 1080)
        self.assertAlmostEqual(result["lines"][0]["width"], .1 * 1766 / 1920)
        self.assertAlmostEqual(result["lines"][0]["height"], .1 * 832 / 1080)

    def test_ocr_error_does_not_trigger_wider_capture(self):
        reader, _ = self.make_reader()
        reader.recognize_image.return_value = {"status": "error", "message": "OCR unavailable", "lines": []}
        self.assertEqual(reader.scan()["status"], "error")
        reader.grab.assert_called_once()

    def test_arena_uses_complete_outer_title_boxes_and_normalized_coordinates(self):
        reader, _ = self.make_reader()
        # Current Cherry UIBase has a 1600x1200 reference resolution, title
        # y=498..531, and 288px title boxes offset 38px within each card.
        reader._game_rectangle.return_value = (7, (0, 0, 1600, 1200))
        reader.recognize_image.return_value = {"status": "ok", "lines": [
            {"text": "Jeweled Gauntlet", "x": (232 - 192) / 1216,
             "y": (498 - 408) / 150, "width": 288 / 1216, "height": 33 / 150}]}
        result = reader.scan(mode="arena")
        reader.grab.assert_called_once_with((192, 408, 1408, 558))
        title = result["lines"][0]
        self.assertAlmostEqual(title["x"], 232 / 1600)
        self.assertAlmostEqual(title["y"], 498 / 1200)
        self.assertAlmostEqual(title["width"], 288 / 1600)
        self.assertAlmostEqual(title["height"], 33 / 1200)
        self.assertEqual(result["size"], [1600, 1200])

    def test_arena_empty_title_band_retains_fallback_and_focus_guards(self):
        reader, user32 = self.make_reader()
        user32.GetForegroundWindow.side_effect = [7, 7, 8]
        self.assertEqual(reader.scan(mode="arena")["status"], "waiting")
        reader.grab.assert_called_once_with((230, 367, 1690, 502))
        reader.recognize_image.assert_called_once()

    def test_unknown_mode_never_captures(self):
        reader, _ = self.make_reader()
        self.assertEqual(reader.scan(mode="unknown")["status"], "error")
        reader._load_windows.assert_not_called()
        reader.grab.assert_not_called()

    def test_negative_monitor_origin_is_added_to_capture_not_result(self):
        reader, _ = self.make_reader()
        reader._game_rectangle.return_value = (7, (-1920, -1080, 0, 0))
        reader.recognize_image.return_value = {"status": "ok", "lines": [
            {"text": "Pinball", "x": .5, "y": .5, "width": .1, "height": .1}]}
        result = reader.scan()
        reader.grab.assert_called_once_with((-1632, -713, -288, -578))
        self.assertEqual(result["size"], [1920, 1080])
        self.assertAlmostEqual(result["lines"][0]["x"], .5)
        self.assertAlmostEqual(result["lines"][0]["y"], (367 + 67.5) / 1080)

    def test_native_failure_becomes_error_and_restores_dpi_context(self):
        reader, user32 = self.make_reader()
        user32.SetThreadDpiAwarenessContext.return_value = 99
        reader.grab.side_effect = RuntimeError("Windows could not capture the game region.")
        result = reader.scan()
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["lines"], [])
        self.assertIn("could not capture", result["message"])
        user32.SetThreadDpiAwarenessContext.assert_called_with(99)


class NativeRegionCaptureTests(unittest.TestCase):
    """Exercise the native boundary with fake handles and an in-memory DIB."""

    def make_reader(self):
        reader, native = CaptureReader(), Mock()
        reader._load_gdi = Mock(return_value=(native.user32, native.gdi32))
        native.user32.GetDC.return_value = 101
        native.gdi32.CreateCompatibleDC.return_value = 202
        native.gdi32.SelectObject.return_value = 404
        native.gdi32.BitBlt.return_value = True
        pixels = ctypes.create_string_buffer(16)
        header = {}

        def create_dib(dc, info_pointer, usage, bits_pointer, section, offset):
            info = ctypes.cast(info_pointer, ctypes.POINTER(_BitmapInfo)).contents.bmiHeader
            header.update(size=info.biSize, width=info.biWidth, height=info.biHeight,
                          planes=info.biPlanes, depth=info.biBitCount,
                          compression=info.biCompression)
            ctypes.cast(bits_pointer, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.addressof(pixels)
            return 303

        def flush():
            # Pixels become readable only after flushing the queued BitBlt.
            pixels.raw = bytes((0, 0, 255, 0, 0, 255, 0, 0,
                                255, 0, 0, 0, 255, 255, 255, 0))
            return True

        native.gdi32.CreateDIBSection.side_effect = create_dib
        native.gdi32.GdiFlush.side_effect = flush
        # Simulate freeing the bitmap to prove the returned image owns its data.
        native.gdi32.DeleteObject.side_effect = lambda _: ctypes.memset(pixels, 0, 16)
        return reader, native, header

    def test_region_copy_uses_exact_bounds_and_detached_top_down_pixels(self):
        reader, native, header = self.make_reader()
        image = reader._grab_region((-50, 20, -48, 22))
        self.addCleanup(image.close)
        native.gdi32.BitBlt.assert_called_once_with(202, 0, 0, 2, 2, 101, -50, 20, 0x00CC0020)
        self.assertEqual(header, {"size": 40, "width": 2, "height": -2,
                                  "planes": 1, "depth": 32, "compression": 0})
        self.assertEqual(image.size, (2, 2))
        self.assertEqual(image.getpixel((0, 0)), (255, 0, 0))
        self.assertEqual(image.getpixel((1, 0)), (0, 255, 0))
        self.assertEqual(image.getpixel((0, 1)), (0, 0, 255))
        self.assertEqual(image.getpixel((1, 1)), (255, 255, 255))
        self.assertEqual(native.mock_calls[-4:], [
            call.gdi32.SelectObject(202, 404), call.gdi32.DeleteDC(202),
            call.gdi32.DeleteObject(303), call.user32.ReleaseDC(None, 101),
        ])

    def test_invalid_or_unbounded_regions_never_allocate_native_resources(self):
        reader, _, _ = self.make_reader()
        for box in ((0, 0, 0, 2), (0, 0, 2, -1), (0, 0, 16385, 2),
                    (0, 0, 16384, 16384), (0, 0, 2**31, 2),
                    (0, 0, True, 2), (0, 0, 2.0, 2), (0, 0, 2), None):
            with self.subTest(box=box), self.assertRaises(ValueError):
                reader._grab_region(box)
        reader._load_gdi.assert_not_called()

    def test_partial_allocation_and_drawing_failures_release_acquired_handles(self):
        cases = (
            ("GetDC", False, False, False, False),
            ("CreateCompatibleDC", True, False, False, False),
            ("CreateDIBSection", True, True, False, False),
            ("SelectObject", True, True, True, False),
            ("BitBlt", True, True, True, True),
            ("GdiFlush", True, True, True, True),
        )
        for failure, screen, memory, bitmap, selected in cases:
            with self.subTest(failure=failure):
                reader, native, _ = self.make_reader()
                api = native.user32 if failure == "GetDC" else native.gdi32
                operation = getattr(api, failure)
                operation.side_effect = None
                operation.return_value = 0
                with self.assertRaisesRegex(RuntimeError, "Windows could not capture"):
                    reader._grab_region((0, 0, 2, 2))
                expected = []
                if selected:
                    expected.append(call.gdi32.SelectObject(202, 404))
                if memory:
                    expected.append(call.gdi32.DeleteDC(202))
                if bitmap:
                    expected.append(call.gdi32.DeleteObject(303))
                if screen:
                    expected.append(call.user32.ReleaseDC(None, 101))
                if expected:
                    self.assertEqual(native.mock_calls[-len(expected):], expected)
                self.assertEqual(native.gdi32.DeleteDC.call_count, int(memory))
                self.assertEqual(native.gdi32.DeleteObject.call_count, int(bitmap))
                self.assertEqual(native.user32.ReleaseDC.call_count, int(screen))

    def test_missing_dib_pointer_and_invalid_selected_handle_are_rejected(self):
        for failure in ("bits", "selected"):
            with self.subTest(failure=failure):
                reader, native, _ = self.make_reader()
                if failure == "bits":
                    native.gdi32.CreateDIBSection.side_effect = None
                    native.gdi32.CreateDIBSection.return_value = 303
                else:
                    native.gdi32.SelectObject.return_value = ctypes.c_void_p(-1).value
                with self.assertRaises(RuntimeError):
                    reader._grab_region((0, 0, 2, 2))
                native.gdi32.DeleteObject.assert_called_once_with(303)
                native.gdi32.DeleteDC.assert_called_once_with(202)
                native.user32.ReleaseDC.assert_called_once_with(None, 101)

    def test_conversion_failure_is_sanitized_and_releases_all_resources(self):
        reader, native, _ = self.make_reader()
        with patch("PIL.Image.frombytes", side_effect=OSError("private native detail")):
            with self.assertRaises(RuntimeError) as caught:
                reader._grab_region((0, 0, 2, 2))
        self.assertNotIn("private native detail", str(caught.exception))
        native.gdi32.SelectObject.assert_called_with(202, 404)
        native.gdi32.DeleteObject.assert_called_once_with(303)
        native.gdi32.DeleteDC.assert_called_once_with(202)
        native.user32.ReleaseDC.assert_called_once_with(None, 101)


if __name__ == "__main__":
    unittest.main()
