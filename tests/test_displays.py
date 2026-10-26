"""Pure display placement tests; no desktop/window inputs or changes."""
import unittest

from mayhem.displays import place_panel, select_display


def display(device, bounds, work=None, primary=False):
    return {"device": device, "bounds": bounds, "work_area": work or bounds, "primary": primary}


PRIMARY = display("primary", (0, 0, 3840, 2160), (0, 0, 3840, 2120), True)
PORTRAIT = display("portrait", (-1440, -400, 0, 2160), (-1440, -400, 0, 2120))
ABOVE = display("above", (0, -1080, 1920, 0), (0, -1080, 1920, -40))


class DisplaySelectionTests(unittest.TestCase):
    def test_preferred_device_wins_over_saved_position_without_orientation_preference(self):
        self.assertIs(select_display([PRIMARY, PORTRAIT], "PRIMARY", False, (-1000, 0)), PRIMARY)

    def test_reused_windows_device_id_does_not_override_portrait_preference(self):
        landscape = {**PRIMARY, "device": r"\\.\DISPLAY2"}
        portrait = {**PORTRAIT, "device": r"\\.\DISPLAY1"}
        self.assertIs(select_display([landscape, portrait], r"\\.\DISPLAY2", True,
                                     (300, 80)), portrait)

    def test_saved_portrait_device_still_wins_over_another_portrait_position(self):
        second = display("right", (3840, 0, 4920, 1920))
        self.assertIs(select_display([PRIMARY, PORTRAIT, second], "portrait", True,
                                     (4000, 100)), PORTRAIT)

    def test_without_portrait_saved_landscape_device_remains_preferred(self):
        self.assertIs(select_display([PRIMARY, ABOVE], "above", True, (300, 80)), ABOVE)

    def test_reused_landscape_id_uses_saved_position_among_portraits(self):
        second = display("right", (3840, 0, 4920, 1920))
        self.assertIs(select_display([PRIMARY, PORTRAIT, second], "primary", True,
                                     (4000, 100)), second)

    def test_portrait_preference_moves_off_primary(self):
        self.assertIs(select_display([PRIMARY, PORTRAIT], prefer_portrait=True, position=(300, 80)), PORTRAIT)

    def test_saved_negative_position_selects_its_monitor(self):
        self.assertIs(select_display([PRIMARY, PORTRAIT], position=(-1200, -200)), PORTRAIT)

    def test_saved_above_position_selects_its_monitor(self):
        self.assertIs(select_display([PRIMARY, ABOVE], position=(300, -900)), ABOVE)

    def test_unplugged_preferred_portrait_falls_back_to_primary(self):
        self.assertIs(select_display([PRIMARY], "portrait", True, (-1200, -200)), PRIMARY)

    def test_missing_preferred_device_can_select_another_portrait(self):
        self.assertIs(select_display([PRIMARY, PORTRAIT], "unplugged", True), PORTRAIT)

    def test_multiple_portrait_displays_preserve_position(self):
        second = display("right", (3840, 0, 4920, 1920))
        self.assertIs(select_display([PRIMARY, PORTRAIT, second], prefer_portrait=True,
                                     position=(4000, 100)), second)

    def test_orientation_uses_monitor_bounds_not_taskbar_work_area(self):
        landscape = display("landscape", (0, 0, 1200, 1000), (0, 0, 500, 1000))
        self.assertIs(select_display([landscape, PORTRAIT], prefer_portrait=True), PORTRAIT)

    def test_shared_edge_belongs_to_monitor_on_its_right(self):
        self.assertIs(select_display([PORTRAIT, PRIMARY], position=(0, 100)), PRIMARY)

    def test_missing_inventory_and_invalid_positions_are_safe(self):
        self.assertIsNone(select_display([]))
        self.assertIsNone(select_display([{}]))
        self.assertIs(select_display([PRIMARY, PORTRAIT], position=(float("nan"), 0)), PRIMARY)


class PanelPlacementTests(unittest.TestCase):
    def test_default_on_portrait_preserves_negative_coordinates(self):
        self.assertEqual(place_panel(PORTRAIT, 440, 640), (-464, -320, 640))

    def test_saved_position_on_above_monitor_is_preserved(self):
        self.assertEqual(place_panel(ABOVE, 440, 640, (100, -900)), (100, -900, 640))

    def test_saved_primary_position_is_not_reused_on_selected_portrait(self):
        self.assertEqual(place_panel(PORTRAIT, 440, 640, (500, 100)), (-464, -320, 640))

    def test_panel_growth_is_clamped_on_its_actual_monitor(self):
        self.assertEqual(place_panel(PORTRAIT, 440, 900, (-200, 1900)), (-440, 1220, 900))

    def test_top_and_left_taskbars_are_respected(self):
        monitor = display("taskbar", (-1080, 0, 0, 1920), (-1020, 40, 0, 1920))
        self.assertEqual(place_panel(monitor, 440, 640, (-1070, 10)), (-1020, 40, 640))

    def test_tall_panel_fits_work_area_without_primary_screen_clamp(self):
        self.assertEqual(place_panel(ABOVE, 440, 2000), (1456, -1080, 1040))

    def test_bad_saved_position_uses_default(self):
        self.assertEqual(place_panel(PORTRAIT, 440, 640, (True, 0)), (-464, -320, 640))

    def test_invalid_panel_dimensions_are_rejected(self):
        for width, height in [(0, 640), (440, -1), (True, 640), (440, 2.5)]:
            with self.subTest(width=width, height=height), self.assertRaises(ValueError):
                place_panel(PORTRAIT, width, height)


if __name__ == "__main__":
    unittest.main()
