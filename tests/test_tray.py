"""Tray lifecycle and callback tests with a fake native message loop."""
import ctypes
import queue
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from mayhem.tray import TrayIcon, _autostart_checked, _native_icon_factory, _verified_icon_class


class FakeIcon:
    def __init__(self, *, show_error=False, run_error=False, return_after_setup=False):
        self._visible = False
        self.show_error = show_error
        self.run_error = run_error
        self.return_after_setup = return_after_setup
        self.entered = threading.Event()
        self.ready = threading.Event()
        self.stopped = threading.Event()
        self.finished = threading.Event()
        self.actions = None

    @property
    def visible(self):
        return self._visible

    @visible.setter
    def visible(self, value):
        if value and self.show_error:
            raise OSError("Tray unavailable")
        self._visible = value

    def run(self, setup):
        self.entered.set()
        try:
            if self.run_error:
                raise OSError("Native loop failed")
            setup(self)
            self.ready.set()
            if not self.return_after_setup:
                self.stopped.wait(timeout=3)
        finally:
            self.finished.set()

    def stop(self):
        self.stopped.set()


class TrayTests(unittest.TestCase):
    def test_windows_menu_rechecks_startup_setting_before_opening(self):
        calls = []
        class Base:
            def update_menu(self):
                calls.append("refresh")
            def _on_notify(self, wparam, lparam):
                calls.append("open")
        icon = _verified_icon_class(Base, SimpleNamespace(WM_RBUTTONUP=516))()
        icon._on_notify(0, 516)
        self.assertEqual(calls, ["refresh", "open"])
        calls.clear()
        icon._on_notify(0, 514)
        self.assertEqual(calls, ["open"])

    def make_tray(self, icon=None):
        events = queue.Queue()
        icon = icon or FakeIcon()
        def factory(action):
            icon.actions = action
            return icon
        tray = TrayIcon(events, _icon_factory=factory)
        self.addCleanup(tray.close)
        return tray, icon, events

    def test_start_reports_ready_only_after_icon_visible(self):
        tray, icon, events = self.make_tray()
        self.assertFalse(tray.available)
        tray.start()
        self.assertEqual(events.get(timeout=1), ("tray_status", True))
        self.assertTrue(icon.visible)
        self.assertTrue(tray.available)
        self.assertTrue(tray._thread.daemon)

    def test_menu_actions_only_enqueue_requests(self):
        tray, icon, events = self.make_tray()
        tray.start()
        self.assertEqual(events.get(timeout=1), ("tray_status", True))
        for action in ("show", "hide", "toggle_autostart", "feedback", "exit"):
            icon.actions(action)
            self.assertEqual(events.get(timeout=1), ("tray_action", action))
        self.assertTrue(icon.visible)
        self.assertFalse(icon.stopped.is_set())

    def test_close_removes_icon_and_joins_native_loop(self):
        tray, icon, events = self.make_tray()
        tray.start()
        events.get(timeout=1)
        tray.close()
        tray.close()
        self.assertFalse(tray.available)
        self.assertFalse(icon.visible)
        self.assertTrue(icon.finished.is_set())
        self.assertFalse(tray._thread.is_alive())
        self.assertEqual(list(events.queue), [("tray_status", False)])

    def test_actions_after_close_are_ignored(self):
        tray, icon, events = self.make_tray()
        tray.start()
        events.get(timeout=1)
        tray.close()
        events.get(timeout=1)
        for action in ("show", "toggle_autostart", "feedback"):
            icon.actions(action)
        self.assertTrue(events.empty())

    def test_close_before_start_never_constructs_an_icon(self):
        factory = Mock()
        tray = TrayIcon(queue.Queue(), _icon_factory=factory)
        tray.close()
        tray.start()
        factory.assert_not_called()

    def test_start_is_nonblocking_even_while_factory_is_loading(self):
        entered, release = threading.Event(), threading.Event()
        icon, events = FakeIcon(), queue.Queue()
        def factory(_):
            entered.set()
            release.wait(timeout=2)
            return icon
        tray = TrayIcon(events, _icon_factory=factory, _join_timeout=.05)
        try:
            tray.start()
            self.assertTrue(entered.wait(timeout=1))
            self.assertFalse(tray.available)
            tray.close()
            release.set()
            tray._thread.join(timeout=1)
            self.assertFalse(tray._thread.is_alive())
            self.assertFalse(icon.entered.is_set())
            self.assertFalse(icon.visible)
        finally:
            release.set()
            tray.close()

    def test_start_twice_uses_one_native_loop(self):
        icon = FakeIcon()
        factory = Mock(return_value=icon)
        tray = TrayIcon(queue.Queue(), _icon_factory=factory)
        self.addCleanup(tray.close)
        tray.start()
        tray.start()
        self.assertTrue(icon.ready.wait(timeout=1))
        factory.assert_called_once()

    def test_close_racing_loop_start_stops_from_setup(self):
        gate, entered = threading.Event(), threading.Event()
        class DelayedIcon(FakeIcon):
            def run(self, setup):
                entered.set()
                gate.wait(timeout=2)
                # Emulate pystray: stopping before run starts has no effect.
                self.stopped.clear()
                super().run(setup)
        tray, icon, events = self.make_tray(DelayedIcon())
        tray._join_timeout = .05
        tray.start()
        self.assertTrue(entered.wait(timeout=1))
        tray.close()
        gate.set()
        tray._thread.join(timeout=1)
        self.assertFalse(tray._thread.is_alive())
        self.assertFalse(icon.visible)
        self.assertNotIn(("tray_status", True), list(events.queue))

    def test_setup_failure_reports_unavailable_and_stops_loop(self):
        tray, icon, events = self.make_tray(FakeIcon(show_error=True))
        with self.assertLogs(level="ERROR"):
            tray.start()
            tray._thread.join(timeout=1)
        self.assertEqual(list(events.queue), [("tray_status", False)])
        self.assertFalse(tray.available)
        self.assertTrue(icon.stopped.is_set())
        self.assertFalse(tray._thread.is_alive())

    def test_import_or_native_loop_failure_reports_unavailable(self):
        for factory in (Mock(side_effect=ImportError("missing")), Mock(return_value=FakeIcon(run_error=True))):
            with self.subTest(factory=factory):
                events = queue.Queue()
                tray = TrayIcon(events, _icon_factory=factory)
                with self.assertLogs(level="ERROR"):
                    tray.start()
                    tray._thread.join(timeout=1)
                self.assertEqual(list(events.queue), [("tray_status", False)])
                self.assertFalse(tray.available)
                tray.close()

    def test_unexpected_loop_exit_revokes_ready_status(self):
        tray, icon, events = self.make_tray(FakeIcon(return_after_setup=True))
        tray.start()
        tray._thread.join(timeout=1)
        self.assertEqual(list(events.queue), [("tray_status", True), ("tray_status", False)])
        self.assertFalse(tray.available)
        self.assertFalse(icon.visible)

    def test_native_menu_has_default_show_and_separate_exit(self):
        class Menu:
            SEPARATOR = object()
            def __init__(self, *items):
                self.items = items
        fake = SimpleNamespace(Menu=Menu, MenuItem=lambda text, action, **kwargs:
                               SimpleNamespace(text=text, action=action, **kwargs), Icon=Mock())
        action = Mock()
        with patch("mayhem.tray._verified_icon_class", return_value=fake.Icon), \
                patch("mayhem.tray.importlib.import_module", return_value=fake):
            _native_icon_factory(action)
        options = fake.Icon.call_args.kwargs
        entries = options["menu"].items
        self.assertEqual([entries[i].text for i in (0, 1, 3, 5, 6)],
                         ["Show companion", "Hide companion", "Start with League", "Send feedback", "Exit"])
        self.assertTrue(entries[0].default)
        self.assertIs(entries[2], Menu.SEPARATOR)
        self.assertIs(entries[4], Menu.SEPARATOR)
        self.assertIs(entries[3].checked, _autostart_checked)
        for index in (0, 1, 3, 5, 6):
            entries[index].action(None, None)
        self.assertEqual([call.args[0] for call in action.call_args_list],
                         ["show", "hide", "toggle_autostart", "feedback", "exit"])
        self.assertEqual(options["icon"].size, (64, 64))

    def test_autostart_checked_reads_current_configuration_each_time(self):
        module = SimpleNamespace(is_enabled=Mock(side_effect=[False, True]))
        with patch("mayhem.tray.importlib.import_module", return_value=module) as loader:
            self.assertFalse(_autostart_checked(None))
            self.assertTrue(_autostart_checked(None))
        self.assertEqual(module.is_enabled.call_count, 2)
        loader.assert_called_with("mayhem.autostart")

    def test_autostart_checked_handles_missing_or_unreadable_configuration(self):
        for error in (ImportError("missing"), OSError("unreadable")):
            with self.subTest(error=error), \
                    patch("mayhem.tray.importlib.import_module", side_effect=error):
                self.assertFalse(_autostart_checked(None))

    def test_native_add_failure_raises_but_cleanup_failure_does_not(self):
        class NotifyData(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("hWnd", ctypes.c_void_p),
                        ("hID", ctypes.c_uint), ("uFlags", ctypes.c_uint)]
        native = SimpleNamespace(NIM_ADD=0, NOTIFYICONDATAW=NotifyData,
                                 Shell_NotifyIcon=Mock(return_value=False))
        icon = _verified_icon_class(object, native)()
        icon._hwnd = 1234
        with self.assertRaisesRegex(OSError, "notification area"):
            icon._message(0, 2)
        self.assertFalse(icon._message(2, 0))
        native.Shell_NotifyIcon.return_value = True
        self.assertTrue(icon._message(0, 2))


if __name__ == "__main__":
    unittest.main()
