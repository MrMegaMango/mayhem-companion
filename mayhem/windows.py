"""Window styles and isolated RegisterHotKey messages; no hooks or injection."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import logging
import os
import queue
import threading

WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
MOD_CONTROL_ALT_NOREPEAT = 0x4003


class _NativeHotkeyApi:
    """Small Win32 boundary that can be replaced by a queue in unit tests."""

    def __init__(self):
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
        self.user32.RegisterHotKey.restype = wintypes.BOOL
        self.user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
        self.user32.UnregisterHotKey.restype = wintypes.BOOL
        self.user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT]
        self.user32.PeekMessageW.restype = wintypes.BOOL
        self.user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
        self.user32.GetMessageW.restype = wintypes.BOOL
        self.user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        self.user32.PostThreadMessageW.restype = wintypes.BOOL
        self.kernel32.GetCurrentThreadId.argtypes = []
        self.kernel32.GetCurrentThreadId.restype = wintypes.DWORD

    def initialize_queue(self):
        # A thread must own a message queue before PostThreadMessage can target it.
        message = wintypes.MSG()
        self.user32.PeekMessageW(ctypes.byref(message), None, 0, 0, 0)
        return int(self.kernel32.GetCurrentThreadId())

    def register(self, identifier, key):
        return bool(self.user32.RegisterHotKey(None, identifier, MOD_CONTROL_ALT_NOREPEAT, key))

    def unregister(self, identifier):
        self.user32.UnregisterHotKey(None, identifier)

    def next_message(self):
        message = wintypes.MSG()
        result = self.user32.GetMessageW(ctypes.byref(message), None, 0, 0)
        if result == -1:
            raise OSError(ctypes.get_last_error(), "The hotkey message loop failed")
        if result == 0:
            return None
        return int(message.message), int(message.wParam), int(message.lParam)

    def post(self, thread_id, message, wparam=0, lparam=0):
        return bool(self.user32.PostThreadMessageW(thread_id, message, wparam, lparam))


class Hotkeys:
    """Register and receive hotkeys on a dedicated thread, dispatch on Tk's thread.

    Registration results are known before the constructor returns, allowing the
    caller to disable hide/lock when their recovery shortcuts are unavailable.
    No Tk method or user callback is called by the native message-loop thread.
    """

    def __init__(self, root, actions, *, _api_factory=None, _ready_timeout=2.0):
        self.root, self.actions = root, dict(actions)
        self.registered = []
        self._registration_result = []
        self._events = queue.Queue()
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._closed = False
        self._after_id = None
        self._api = None
        self._thread_id = None
        self._thread = None
        self._failure = None
        self._api_factory = _api_factory or _NativeHotkeyApi
        if _api_factory is None and not hasattr(ctypes, "windll"):
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="mayhem-hotkeys")
        self._thread.start()
        if not self._ready.wait(_ready_timeout):
            # Fail closed: a delayed registration must not enable hide or lock.
            self._failure = "Hotkey registration did not finish in time"
            self._stop.set()
            self._post_quit()
        elif self._failure is None and not self._stop.is_set():
            self.registered = list(self._registration_result)
        if self.registered:
            self._after_id = self.root.after(50, self.poll)

    def _run(self):
        owned = []
        try:
            self._api = self._api_factory()
            self._thread_id = self._api.initialize_queue()
            for identifier, (key, _) in self.actions.items():
                if self._stop.is_set():
                    break
                if self._api.register(identifier, key):
                    owned.append(identifier)
            self._registration_result = list(owned)
            self._ready.set()
            if not owned:
                return
            while not self._stop.is_set():
                message = self._api.next_message()
                if message is None:
                    if not self._stop.is_set():
                        raise RuntimeError("The hotkey message loop stopped unexpectedly")
                    break
                kind, identifier, _ = message
                if kind == WM_HOTKEY and identifier in owned:
                    self._events.put(("hotkey", identifier))
        except Exception as exc:
            self._failure = str(exc)
            if not self._stop.is_set():
                logging.exception("Global shortcut listener failed")
                self._events.put(("failed", self._failure))
        finally:
            if self._api is not None:
                for identifier in owned:
                    try:
                        self._api.unregister(identifier)
                    except Exception:
                        logging.exception("Could not unregister a global shortcut")
            self._ready.set()

    def _post_quit(self):
        if self._api is not None and self._thread_id is not None:
            try:
                self._api.post(self._thread_id, WM_QUIT)
            except Exception:
                logging.exception("Could not stop the global shortcut listener")

    def _recover_window(self):
        """Make the panel usable if its only recovery shortcut disappears."""
        self.registered.clear()
        try:
            self.root.deiconify()
            self.root.attributes("-topmost", True)
            set_window_style(self.root, False)
        except Exception:
            logging.exception("Could not restore the panel after shortcut failure")

    def poll(self):
        self._after_id = None
        if self._closed:
            return
        failed = False
        try:
            # Bound each pass so an event burst cannot starve Tk's other work.
            for _ in range(100):
                kind, value = self._events.get_nowait()
                if kind == "failed":
                    failed = True
                    break
                action = self.actions.get(value)
                if kind == "hotkey" and value in self.registered and action:
                    try:
                        action[1]()
                    except Exception:
                        logging.exception("Global shortcut action failed")
                    if self._closed:
                        return
        except queue.Empty:
            pass
        if failed or (self._thread is not None and not self._thread.is_alive() and not self._stop.is_set()):
            self._recover_window()
            self._stop.set()
            return
        if not self._closed:
            self._after_id = self.root.after(50, self.poll)

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        if self._after_id is not None:
            try:
                self.root.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None
        self._post_quit()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=0.5)
        self.registered.clear()

def set_window_style(root, locked=False):
    if not hasattr(ctypes, "windll"):
        return
    user32 = ctypes.windll.user32
    user32.GetParent.argtypes = [wintypes.HWND]
    user32.GetParent.restype = wintypes.HWND
    hwnd = user32.GetParent(root.winfo_id()) or root.winfo_id()
    get_style, set_style = user32.GetWindowLongPtrW, user32.SetWindowLongPtrW
    get_style.argtypes = [wintypes.HWND, ctypes.c_int]
    get_style.restype = ctypes.c_ssize_t
    set_style.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
    set_style.restype = ctypes.c_ssize_t
    flags = 0x20 | 0x08000000  # Transparent hit testing + no activation.
    style = get_style(hwnd, -20) | 0x80000  # Layered windows support transparent hit testing.
    set_style(hwnd, -20, style | flags if locked else style & ~flags)
    user32.SetLayeredWindowAttributes.argtypes = [wintypes.HWND, wintypes.DWORD, wintypes.BYTE, wintypes.DWORD]
    user32.SetLayeredWindowAttributes(hwnd, 0, 255, 2)
    # Exclude this panel from capture so OCR cannot read its own copied titles.
    user32.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
    user32.SetWindowDisplayAffinity(hwnd, 0 if os.environ.get("MAYHEM_QA_CAPTURE") == "1" else 0x11)
