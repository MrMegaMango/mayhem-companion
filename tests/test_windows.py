import ctypes
import queue
import threading
import time
import unittest
from unittest.mock import patch

from mayhem.windows import Hotkeys, WM_HOTKEY, WM_QUIT, _NativeHotkeyApi


class FakeRoot:
    def __init__(self):
        self.main_thread = threading.get_ident()
        self.scheduled = {}
        self.next_id = 0
        self.visible = False
        self.topmost = False

    def _assert_ui_thread(self):
        if threading.get_ident() != self.main_thread:
            raise AssertionError("Tk called from a background thread")

    def after(self, delay, callback):
        self._assert_ui_thread()
        self.next_id += 1
        self.scheduled[self.next_id] = callback
        return self.next_id

    def after_cancel(self, identifier):
        self._assert_ui_thread()
        self.scheduled.pop(identifier, None)

    def fire(self):
        self._assert_ui_thread()
        pending = list(self.scheduled.values())
        self.scheduled.clear()
        for callback in pending:
            callback()

    def deiconify(self):
        self._assert_ui_thread()
        self.visible = True

    def attributes(self, option, value):
        self._assert_ui_thread()
        if option == "-topmost":
            self.topmost = value


class FakeApi:
    def __init__(self, denied=(), ready_gate=None):
        self.messages = queue.Queue()
        self.denied = set(denied)
        self.ready_gate = ready_gate
        self.owner = None
        self.registered, self.unregistered = [], []
        self.received = threading.Event()

    def initialize_queue(self):
        self.owner = threading.get_ident()
        if self.ready_gate:
            self.ready_gate.wait(2)
        return self.owner

    def register(self, identifier, key):
        if threading.get_ident() != self.owner:
            raise AssertionError("Registration must happen on message-loop thread")
        if identifier in self.denied:
            return False
        self.registered.append(identifier)
        return True

    def unregister(self, identifier):
        if threading.get_ident() != self.owner:
            raise AssertionError("Unregistration must happen on message-loop thread")
        self.unregistered.append(identifier)

    def next_message(self):
        message = self.messages.get(timeout=2)
        self.received.set()
        if isinstance(message, Exception):
            raise message
        if message[0] == WM_QUIT:
            return None
        return message

    def post(self, thread_id, message, wparam=0, lparam=0):
        if thread_id != self.owner:
            raise AssertionError("Message targeted another thread")
        self.messages.put((message, wparam, lparam))
        return True


def wait_for(predicate, timeout=1):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        threading.Event().wait(.005)
    return bool(predicate())


class HotkeyTests(unittest.TestCase):
    def make(self, callback=None, **kwargs):
        root, api = FakeRoot(), FakeApi(**kwargs)
        keys = Hotkeys(root, {101: (ord("M"), callback or (lambda: None)),
                              103: (ord("L"), lambda: None)}, _api_factory=lambda: api)
        self.addCleanup(keys.close)
        return root, api, keys

    def test_registration_results_are_available_before_return(self):
        root, api, keys = self.make(denied=(103,))
        self.assertEqual(keys.registered, [101])
        self.assertNotEqual(api.owner, root.main_thread)
        self.assertEqual(len(root.scheduled), 1)

    def test_callback_only_runs_when_ui_drains_queue(self):
        calls = []
        root, api, keys = self.make(callback=lambda: calls.append(threading.get_ident()))
        api.post(api.owner, WM_HOTKEY, 101)
        self.assertTrue(wait_for(lambda: not keys._events.empty()))
        self.assertEqual(calls, [])
        root.fire()
        self.assertEqual(calls, [root.main_thread])

    def test_unknown_or_unregistered_shortcut_cannot_dispatch(self):
        calls = []
        root, api, keys = self.make(callback=lambda: calls.append(1), denied=(103,))
        api.post(api.owner, WM_HOTKEY, 103)
        api.post(api.owner, WM_HOTKEY, 999)
        api.post(api.owner, 123, 101)
        api.post(api.owner, WM_HOTKEY, 101)
        self.assertTrue(wait_for(lambda: not keys._events.empty()))
        root.fire()
        self.assertEqual(calls, [1])

    def test_close_unregisters_on_owner_thread_and_cancels_timer(self):
        root, api, keys = self.make()
        keys.close()
        self.assertFalse(keys._thread.is_alive())
        self.assertEqual(api.unregistered, [101, 103])
        self.assertEqual(keys.registered, [])
        self.assertEqual(root.scheduled, {})
        keys.close()  # Idempotent.

    def test_closing_from_shortcut_does_not_schedule_again(self):
        root, api, keys = self.make()
        keys.actions[101] = (ord("M"), keys.close)
        api.post(api.owner, WM_HOTKEY, 101)
        self.assertTrue(wait_for(lambda: not keys._events.empty()))
        root.fire()
        self.assertEqual(root.scheduled, {})
        self.assertFalse(keys._thread.is_alive())

    def test_failed_listener_restores_hidden_locked_panel(self):
        root, api, keys = self.make()
        with self.assertLogs(level="ERROR"):
            api.messages.put(RuntimeError("Simulated message-loop failure"))
            self.assertTrue(wait_for(lambda: not keys._thread.is_alive()))
        with patch("mayhem.windows.set_window_style") as style:
            root.fire()
        self.assertTrue(root.visible)
        self.assertTrue(root.topmost)
        style.assert_called_once_with(root, False)
        self.assertEqual(keys.registered, [])
        self.assertEqual(root.scheduled, {})

    def test_action_failure_does_not_kill_listener_or_ui_poll(self):
        def fail():
            raise RuntimeError("Simulated callback failure")
        root, api, keys = self.make(callback=fail)
        api.post(api.owner, WM_HOTKEY, 101)
        self.assertTrue(wait_for(lambda: not keys._events.empty()))
        with self.assertLogs(level="ERROR"):
            root.fire()
        self.assertTrue(keys._thread.is_alive())
        self.assertEqual(len(root.scheduled), 1)

    def test_registration_timeout_fails_closed_and_cleans_up(self):
        gate = threading.Event()
        root, api = FakeRoot(), FakeApi(ready_gate=gate)
        keys = Hotkeys(root, {101: (ord("M"), lambda: None)},
                       _api_factory=lambda: api, _ready_timeout=.01)
        self.addCleanup(keys.close)
        self.assertEqual(keys.registered, [])
        self.assertEqual(root.scheduled, {})
        gate.set()
        self.assertTrue(wait_for(lambda: not keys._thread.is_alive()))
        self.assertEqual(api.registered, [])

    def test_denied_all_shortcuts_never_enables_hide_lock(self):
        root, api, keys = self.make(denied=(101, 103))
        self.assertEqual(keys.registered, [])
        self.assertEqual(root.scheduled, {})
        self.assertTrue(wait_for(lambda: not keys._thread.is_alive()))

    @unittest.skipUnless(hasattr(ctypes, "windll"), "Windows message loop test")
    def test_native_thread_message_dispatch_without_keyboard_or_game_input(self):
        root = FakeRoot()
        calls = []
        # Ctrl+Alt+F24 uses an isolated test ID. No keyboard event is generated.
        identifier = 0x6A01
        keys = Hotkeys(root, {identifier: (0x87, lambda: calls.append(threading.get_ident()))})
        self.addCleanup(keys.close)
        if identifier not in keys.registered:
            self.skipTest("Ctrl+Alt+F24 is already registered")
        self.assertIsInstance(keys._api, _NativeHotkeyApi)
        self.assertTrue(keys._api.post(keys._thread_id, WM_HOTKEY, identifier))
        self.assertTrue(wait_for(lambda: not keys._events.empty()))
        self.assertEqual(calls, [])
        root.fire()
        self.assertEqual(calls, [root.main_thread])
        keys.close()
        self.assertFalse(keys._thread.is_alive())


if __name__ == "__main__":
    unittest.main()
