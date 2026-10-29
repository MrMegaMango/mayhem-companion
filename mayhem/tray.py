"""Windows notification-area icon; callbacks communicate only through a queue."""
from __future__ import annotations

import importlib
import logging
import sys
import threading


def create_icon_image():
    """A small, high-contrast M drawn locally without fonts or external assets."""
    from PIL import Image, ImageDraw
    image = Image.new("RGBA", (128, 128), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((4, 4, 124, 124), radius=25, fill="#192231", outline="#405471", width=3)
    draw.polygon([(28, 96), (28, 33), (44, 33), (64, 63), (84, 33),
                  (100, 33), (100, 96), (82, 96), (82, 65), (64, 90),
                  (46, 65), (46, 96)], fill="#7ce1bd")
    return image.resize((64, 64), Image.Resampling.LANCZOS)


def _verified_icon_class(base, native=None):
    # pystray 0.19.5's Windows backend discards Shell_NotifyIconW's BOOL.
    # Check only adding an icon: a failed removal during shutdown is harmless.
    import ctypes
    native = native or importlib.import_module("pystray._util.win32")

    class VerifiedIcon(base):
        def _on_notify(self, wparam, lparam):
            # Startup configuration completes asynchronously (and can change
            # outside the app). Rebuild checkmarks just before opening a menu.
            if lparam == native.WM_RBUTTONUP:
                self.update_menu()
            return super()._on_notify(wparam, lparam)

        def _message(self, code, flags, **kwargs):
            result = native.Shell_NotifyIcon(code, native.NOTIFYICONDATAW(
                cbSize=ctypes.sizeof(native.NOTIFYICONDATAW),
                hWnd=self._hwnd, hID=id(self), uFlags=flags, **kwargs))
            if code == native.NIM_ADD and not result:
                raise OSError("Windows could not add the companion to the notification area")
            return result

    return VerifiedIcon


def _autostart_checked(_item):
    try:
        return bool(importlib.import_module("mayhem.autostart").is_enabled())
    except (ImportError, OSError):
        logging.debug("Could not read automatic startup status", exc_info=True)
        return False


def _native_icon_factory(on_action):
    # Importing pystray selects an OS backend; keep it out of module import and
    # pure unit tests. Windows supports running Icon.run on a background thread.
    pystray = importlib.import_module("pystray")
    menu = pystray.Menu(
        pystray.MenuItem("Show companion", lambda _icon, _item: on_action("show"), default=True),
        pystray.MenuItem("Hide companion", lambda _icon, _item: on_action("hide")),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Start with League", lambda _icon, _item: on_action("toggle_autostart"),
                         checked=_autostart_checked),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Privacy & usage", lambda _icon, _item: on_action("privacy")),
        pystray.MenuItem("Send feedback", lambda _icon, _item: on_action("feedback")),
        pystray.MenuItem("Exit", lambda _icon, _item: on_action("exit")),
    )
    icon_class = _verified_icon_class(pystray.Icon) if sys.platform == "win32" else pystray.Icon
    return icon_class("mayhem_companion", icon=create_icon_image(),
                      title="Mayhem Companion", menu=menu)


class TrayIcon:
    """Nonblocking tray lifecycle, independent of Tk and the game's UI.

    Emits ('tray_status', bool) when readiness changes or startup fails, and
    ('tray_action', 'show'|'hide'|'toggle_autostart'|'privacy'|'feedback'|'exit') for menu/default actions.
    The app must drain these events on its own UI thread. A closed instance is
    not restarted; construct a new one if a later restart is needed.
    """

    def __init__(self, output_queue, *, _icon_factory=None, _join_timeout=2.0):
        self.output = output_queue
        self._factory = _icon_factory or _native_icon_factory
        self._join_timeout = _join_timeout
        self._lock = threading.RLock()
        self._thread = None
        self._icon = None
        self._closed = False
        self._available = False
        self._reported_status = None

    @property
    def available(self):
        with self._lock:
            return self._available and not self._closed

    def _status(self, available):
        with self._lock:
            self._available = bool(available) and not self._closed
            if self._reported_status != self._available:
                self._reported_status = self._available
                self.output.put(("tray_status", self._available))

    def _action(self, action):
        with self._lock:
            if not self._closed and action in ("show", "hide", "toggle_autostart", "privacy", "feedback", "exit"):
                self.output.put(("tray_action", action))

    @staticmethod
    def _stop_icon(icon):
        # Hide immediately even if message-loop shutdown takes a moment.
        try:
            icon.visible = False
        except Exception:
            logging.debug("Could not hide the tray icon", exc_info=True)
        try:
            icon.stop()
        except Exception:
            logging.exception("Could not stop the tray icon")

    def _setup(self, icon):
        try:
            with self._lock:
                closed = self._closed
                if not closed:
                    icon.visible = True
                    self._status(True)
            if closed:
                # close() can race with Icon.run starting. A stop made before
                # its loop starts has no effect, so repeat it once ready.
                self._stop_icon(icon)
        except Exception:
            logging.exception("Could not show the tray icon")
            self._status(False)
            self._stop_icon(icon)

    def _run(self):
        icon = None
        try:
            icon = self._factory(self._action)
            with self._lock:
                self._icon = icon
                closed = self._closed
            if not closed:
                icon.run(setup=self._setup)
        except Exception:
            logging.exception("Tray icon failed")
        finally:
            self._status(False)
            if icon is not None:
                self._stop_icon(icon)
            with self._lock:
                if self._icon is icon:
                    self._icon = None

    def start(self):
        """Start once without waiting for native icon setup or its message loop."""
        with self._lock:
            if self._closed or self._thread is not None:
                return
            self._thread = threading.Thread(target=self._run, name="mayhem-tray", daemon=True)
            self._thread.start()

    def close(self):
        """Hide the icon, stop its native loop, and join its daemon thread once."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._status(False)
            icon, thread = self._icon, self._thread
        if icon is not None:
            self._stop_icon(icon)
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=self._join_timeout)
            if thread.is_alive():
                logging.warning("Tray shutdown is still pending; its thread is a daemon")
