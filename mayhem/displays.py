"""Read display geometry and place the panel without assuming a primary screen.

The native boundary only inventories monitors. These helpers do not move,
activate, resize, or otherwise control any window. The app establishes its DPI
awareness before requesting geometry, and applies the returned coordinates.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import math
from numbers import Real
import sys
from typing import Mapping


def _rect(value):
    if (not isinstance(value, (list, tuple)) or len(value) != 4 or
            any(isinstance(n, bool) or not isinstance(n, int) for n in value)):
        return None
    left, top, right, bottom = value
    return tuple(value) if left < right and top < bottom else None


def _point(value):
    if (not isinstance(value, (list, tuple)) or len(value) != 2 or
            any(isinstance(n, bool) or not isinstance(n, Real) or not math.isfinite(n) for n in value)):
        return None
    return tuple(round(n) for n in value)


def _contains(rect, point):
    return point is not None and rect[0] <= point[0] < rect[2] and rect[1] <= point[1] < rect[3]


def _valid_display(display):
    return (isinstance(display, Mapping) and isinstance(display.get("device"), str) and
            _rect(display.get("bounds")) is not None and _rect(display.get("work_area")) is not None)


def list_displays() -> list[dict]:
    """Return attached Windows display device names, bounds/work areas, primary.

    Rectangles use virtual-screen coordinates, including negative coordinates.
    Returns an empty list on non-Windows systems. Native enumeration failures
    raise OSError so callers can retain their current position instead.
    """
    if sys.platform != "win32":
        return []

    class MonitorInfo(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                    ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD),
                    ("szDevice", wintypes.WCHAR * 32)]

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC,
        ctypes.POINTER(wintypes.RECT), wintypes.LPARAM,
    )
    user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(MonitorInfo)]
    user32.GetMonitorInfoW.restype = wintypes.BOOL
    user32.EnumDisplayMonitors.argtypes = [
        wintypes.HDC, ctypes.POINTER(wintypes.RECT), callback_type, wintypes.LPARAM,
    ]
    user32.EnumDisplayMonitors.restype = wintypes.BOOL
    displays = []
    failures = []

    @callback_type
    def collect(monitor, _dc, _bounds, _data):
        info = MonitorInfo()
        info.cbSize = ctypes.sizeof(info)
        if not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            failures.append(ctypes.get_last_error())
            return True
        bounds, work = info.rcMonitor, info.rcWork
        display = {
            "device": info.szDevice,
            "bounds": (bounds.left, bounds.top, bounds.right, bounds.bottom),
            "work_area": (work.left, work.top, work.right, work.bottom),
            "primary": bool(info.dwFlags & 1),
        }
        if _valid_display(display):
            displays.append(display)
        return True

    if not user32.EnumDisplayMonitors(None, None, collect, 0):
        raise ctypes.WinError(ctypes.get_last_error())
    if not displays and failures:
        raise ctypes.WinError(failures[0])
    return sorted(displays, key=lambda display: (*display["bounds"][:2], display["device"]))


def select_display(displays, preferred_device=None, prefer_portrait=False, position=None):
    """Choose within the preferred orientation, then saved device/position.

    A portrait preference overrides a saved device now assigned to landscape.
    When no portrait is attached, keep the saved device/position/primary fallback.
    The returned value is one of the supplied display dictionaries, or None.
    """
    valid = [display for display in displays if _valid_display(display)]
    if not valid:
        return None
    candidates = valid
    if prefer_portrait:
        portrait = [display for display in valid
                    if display["bounds"][3] - display["bounds"][1] >
                    display["bounds"][2] - display["bounds"][0]]
        if portrait:
            candidates = portrait
    if isinstance(preferred_device, str):
        preferred = next((display for display in candidates
                          if display["device"].casefold() == preferred_device.casefold()), None)
        if preferred is not None:
            return preferred
    point = _point(position)
    positioned = next((display for display in candidates if _contains(display["bounds"], point)), None)
    if positioned is not None:
        return positioned
    return next((display for display in candidates if display.get("primary")), candidates[0])


def place_panel(display, width, height, position=None) -> tuple[int, int, int]:
    """Return x/y/fitted height inside the selected display's work area.

    Defaults to 24px from the right and 80px below the work-area top. A saved
    position is honored only if its origin lies on this display, then clamped
    for taskbars, panel growth, or resolution changes. Width is caller-owned.
    """
    if not _valid_display(display):
        raise ValueError("A display with valid bounds and work_area is required")
    if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0
           for value in (width, height)):
        raise ValueError("Panel width and height must be positive integers")
    left, top, right, bottom = display["work_area"]
    height = min(height, bottom - top)
    point = _point(position)
    if _contains(display["bounds"], point):
        x, y = point
    else:
        x, y = right - width - 24, top + 80
    x = min(max(left, x), max(left, right - width))
    y = min(max(top, y), bottom - height)
    return x, y, height
