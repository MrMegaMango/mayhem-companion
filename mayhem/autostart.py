"""Small Windows process-name watcher for starting the companion with League.

This module is intentionally stdlib-only: no UI, game APIs, credentials, or
process-memory access. It never creates or changes the Startup shortcut.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Callable


STARTUP_SHORTCUT_NAME = "Mayhem Companion - Start with League.lnk"
WATCHER_MUTEX_NAME = r"Local\MayhemCompanion.LeagueAutostart.v1"
POLL_INTERVAL = 2.0
_LEAGUE_PROCESS_NAMES = frozenset((
    "leagueclient.exe", "leagueclientux.exe", "league of legends.exe",
))
_TH32CS_SNAPPROCESS = 0x00000002
_ERROR_NO_MORE_FILES = 18
_ERROR_ALREADY_EXISTS = 183
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class _ProcessEntry32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260),
    ]


class _NativeWindows:
    def __init__(self):
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        self.kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        for name in ("Process32FirstW", "Process32NextW"):
            function = getattr(self.kernel32, name)
            function.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry32W)]
            function.restype = wintypes.BOOL
        self.kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        self.kernel32.CreateMutexW.restype = wintypes.HANDLE
        self.kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel32.CloseHandle.restype = wintypes.BOOL

    def league_running(self) -> bool:
        snapshot = self.kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
        if snapshot in (None, _INVALID_HANDLE_VALUE):
            raise OSError("Process list is temporarily unavailable")
        try:
            entry = _ProcessEntry32W()
            entry.dwSize = ctypes.sizeof(entry)
            available = self.kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
            while available:
                if entry.szExeFile.casefold() in _LEAGUE_PROCESS_NAMES:
                    return True
                available = self.kernel32.Process32NextW(snapshot, ctypes.byref(entry))
            if ctypes.get_last_error() != _ERROR_NO_MORE_FILES:
                # A failed/partial snapshot must not look like League closed:
                # that would rearm launching after the user chose tray Exit.
                raise OSError("Process list is temporarily unavailable")
            return False
        finally:
            self.kernel32.CloseHandle(snapshot)

    def acquire_mutex(self, name: str = WATCHER_MUTEX_NAME):
        ctypes.set_last_error(0)
        handle = self.kernel32.CreateMutexW(None, False, name)
        if not handle:
            raise OSError("The startup watcher could not start")
        if ctypes.get_last_error() == _ERROR_ALREADY_EXISTS:
            self.kernel32.CloseHandle(handle)
            return None
        return handle

    def close_handle(self, handle):
        self.kernel32.CloseHandle(handle)


def startup_shortcut_path() -> Path:
    """Resolve the current user's actual Startup folder, including redirection."""
    if sys.platform == "win32":
        try:
            shell32 = ctypes.WinDLL("shell32", use_last_error=True)
            get_folder = shell32.SHGetFolderPathW
            get_folder.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.HANDLE,
                                   wintypes.DWORD, wintypes.LPWSTR]
            get_folder.restype = ctypes.c_long
            folder = ctypes.create_unicode_buffer(260)
            if get_folder(None, 0x0007, None, 0, folder) == 0 and folder.value:
                return Path(folder.value) / STARTUP_SHORTCUT_NAME
        except (OSError, AttributeError):
            pass
    roaming = os.environ.get("APPDATA")
    base = Path(roaming) if roaming else Path.home() / "AppData" / "Roaming"
    return base / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / STARTUP_SHORTCUT_NAME


def is_enabled() -> bool:
    """Whether this user's opt-in Startup shortcut exists; never modify it."""
    try:
        return startup_shortcut_path().is_file()
    except OSError:
        return False


def launch_companion() -> None:
    if sys.platform != "win32":
        raise OSError("Automatic League startup is available on Windows only")
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = 0  # SW_HIDE: do not flash a console when launching.
    subprocess.Popen(
        [sys.executable, "-m", "mayhem", "--auto-start"],
        cwd=str(Path(__file__).resolve().parent.parent),
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        startupinfo=startup, creationflags=subprocess.CREATE_NO_WINDOW,
        close_fds=True, shell=False,
    )


def run_watcher(*, process_source: Callable[[], bool],
                launch: Callable[[], None] | None = None,
                sleep: Callable[[float], None] | None = None,
                enabled: Callable[[], bool] | None = None,
                interval: float = POLL_INTERVAL) -> None:
    """Launch once per observed League session, preserving tray Exit intent.

    Transient process-query failures do not count as a closed League session.
    A failed launch is retried on the next poll. The Startup shortcut controls
    the watcher's lifetime; removing it does not close a running companion.
    """
    if not isinstance(interval, (int, float)) or not 0 < interval <= 60:
        raise ValueError("Watcher interval must be between zero and 60 seconds")
    launch = launch or launch_companion
    sleep = sleep or time.sleep
    enabled = enabled or is_enabled
    handled_session = False
    while enabled():
        try:
            active = process_source()
        except Exception:
            # Quiet recovery; no process details or repeating log messages.
            sleep(interval)
            continue
        if not active:
            handled_session = False
        elif not handled_session:
            try:
                launch()
            except Exception:
                pass
            else:
                handled_session = True
        sleep(interval)


def main() -> int:
    if sys.platform != "win32" or not is_enabled():
        return 0
    try:
        native = _NativeWindows()
        handle = native.acquire_mutex()
    except OSError:
        return 1
    if handle is None:
        return 0
    try:
        run_watcher(process_source=native.league_running)
    except KeyboardInterrupt:
        pass
    finally:
        native.close_handle(handle)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
