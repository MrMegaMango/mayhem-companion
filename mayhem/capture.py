"""Read visible League augment titles with Windows' on-device OCR.

Nothing is injected into the game. Capture is restricted to a crop of the
foreground League of Legends.exe client area, and images never leave memory.
Coordinates in returned lines are normalized to the full client/image size.
"""

from __future__ import annotations

import asyncio
import ctypes
from ctypes import wintypes
from pathlib import PureWindowsPath
import sys
from statistics import median
from typing import Any


def _result(status: str, message: str, lines: list | None = None,
            size: tuple[int, int] | list[int] = (0, 0)) -> dict[str, Any]:
    return {"status": status, "message": message, "lines": lines or [],
            "size": list(size)}


def _scaled_size(width: int, height: int, limit: int) -> tuple[int, int]:
    scale = min(1.0, limit / max(width, height))
    return max(1, round(width * scale)), max(1, round(height * scale))


def _line_bounds(words: Any, width: int, height: int) -> dict[str, float] | None:
    rectangles = [word.bounding_rect for word in words]
    if not rectangles:
        return None
    left = min(rect.x for rect in rectangles)
    top = min(rect.y for rect in rectangles)
    right = max(rect.x + rect.width for rect in rectangles)
    bottom = max(rect.y + rect.height for rect in rectangles)
    return {"x": left / width, "y": top / height,
            "width": (right - left) / width, "height": (bottom - top) / height}


def _word_groups(words: Any) -> list[list[Any]]:
    """Windows can join three card titles into one line; split large gaps."""
    words = sorted(words, key=lambda word: word.bounding_rect.x)
    if not words:
        return []
    max_gap = max(24, median(word.bounding_rect.height for word in words) * 2)
    groups = [[words[0]]]
    for word in words[1:]:
        previous = groups[-1][-1].bounding_rect
        if word.bounding_rect.x - (previous.x + previous.width) > max_gap:
            groups.append([])
        groups[-1].append(word)
    return groups


class CaptureReader:
    """A lazy OCR engine; call from a background worker to keep the UI fluid."""

    # Includes the card headings and excludes most of the HUD and chat area.
    CROP = (0.04, 0.08, 0.96, 0.85)
    TITLE_BAND = (0.15, 0.34, 0.85, 0.465)

    def __init__(self) -> None:
        self._engine = None
        self._types = None
        self._windows = None

    def _load_ocr(self) -> None:
        if self._engine is not None:
            return
        if sys.platform != "win32":
            raise RuntimeError("Automatic screen reading requires Windows 10 or 11.")
        try:
            # These imports are deliberately deferred: manual lookup works
            # even when the optional OCR dependencies are unavailable.
            from winrt.windows.globalization import Language
            from winrt.windows.graphics.imaging import (
                BitmapAlphaMode, BitmapPixelFormat, SoftwareBitmap,
            )
            from winrt.windows.media.ocr import OcrEngine
            from winrt.windows.storage.streams import DataWriter
        except ImportError as exc:
            raise RuntimeError(
                "Windows OCR components are missing. Run Setup.ps1 again; "
                "manual augment lookup is still available."
            ) from exc
        try:
            self._engine = OcrEngine.try_create_from_language(Language("en-US"))
        except Exception as exc:
            raise RuntimeError(
                "Windows OCR could not start. Use manual lookup or install "
                "English text recognition in Windows language settings."
            ) from exc
        if self._engine is None:
            raise RuntimeError(
                "English OCR is not installed. Add English text recognition "
                "in Windows language settings, or use manual lookup."
            )
        self._types = (SoftwareBitmap, BitmapPixelFormat, BitmapAlphaMode,
                       DataWriter, OcrEngine)

    async def recognize_image_async(self, image: Any) -> dict[str, Any]:
        """OCR an in-memory Pillow image, useful for deterministic smoke tests."""
        if image.width < 2 or image.height < 2:
            return _result("error", "The image is too small to read.", size=image.size)
        try:
            self._load_ocr()
            from PIL import Image
            SoftwareBitmap, PixelFormat, AlphaMode, DataWriter, OcrEngine = self._types
            # Preserve 4K title lettering; aggressive shrinking loses strokes.
            limit = min(int(OcrEngine.max_image_dimension), 4096)
            output_size = _scaled_size(image.width, image.height, limit)
            prepared = image.convert("RGBA")
            if output_size != prepared.size:
                prepared = prepared.resize(output_size, Image.Resampling.LANCZOS)
            writer = DataWriter()
            bitmap = None
            try:
                writer.write_bytes(prepared.tobytes("raw", "BGRA"))
                buffer = writer.detach_buffer()
                bitmap = SoftwareBitmap.create_copy_with_alpha_from_buffer(
                    buffer, PixelFormat.BGRA8, prepared.width, prepared.height,
                    AlphaMode.IGNORE,
                )
                recognized = await self._engine.recognize_async(bitmap)
                lines = []
                for line in recognized.lines:
                    groups = _word_groups(line.words)
                    for group in groups:
                        bounds = _line_bounds(group, prepared.width, prepared.height)
                        text = " ".join(word.text for word in group).strip()
                        if bounds is not None and text:
                            lines.append({"text": text, **bounds})
                return _result("ok", "Screen read locally", lines, image.size)
            finally:
                if bitmap is not None:
                    bitmap.close()
                writer.close()
        except Exception as exc:
            return _result("error", str(exc), size=image.size)

    def recognize_image(self, image: Any) -> dict[str, Any]:
        """Synchronous OCR wrapper for a worker thread (not an async loop)."""
        return asyncio.run(self.recognize_image_async(image))

    def _load_windows(self) -> tuple[Any, Any]:
        if self._windows is not None:
            return self._windows
        if sys.platform != "win32":
            raise RuntimeError("Automatic screen reading requires Windows 10 or 11.")
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        user32.GetForegroundWindow.argtypes = []
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.IsWindowVisible.argtypes = [wintypes.HWND]
        user32.IsWindowVisible.restype = wintypes.BOOL
        user32.IsIconic.argtypes = [wintypes.HWND]
        user32.IsIconic.restype = wintypes.BOOL
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        user32.GetClientRect.restype = wintypes.BOOL
        user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
        user32.ClientToScreen.restype = wintypes.BOOL
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        if hasattr(user32, "SetThreadDpiAwarenessContext"):
            user32.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
            user32.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
        self._windows = user32, kernel32
        return self._windows

    def _game_rectangle(self) -> tuple[int, tuple[int, int, int, int]] | None:
        user32, kernel32 = self._load_windows()
        hwnd = user32.GetForegroundWindow()
        if not hwnd or not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
            return None
        process_id = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
        handle = kernel32.OpenProcess(0x1000, False, process_id.value)
        if not handle:
            return None
        try:
            length = wintypes.DWORD(32768)
            path = ctypes.create_unicode_buffer(length.value)
            if not kernel32.QueryFullProcessImageNameW(handle, 0, path, ctypes.byref(length)):
                return None
            if PureWindowsPath(path.value).name.casefold() != "league of legends.exe":
                return None
        finally:
            kernel32.CloseHandle(handle)
        rect = wintypes.RECT()
        origin = wintypes.POINT(0, 0)
        if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
            return None
        if not user32.ClientToScreen(hwnd, ctypes.byref(origin)):
            return None
        width, height = rect.right - rect.left, rect.bottom - rect.top
        if width < 640 or height < 360:
            return None
        return hwnd, (origin.x, origin.y, origin.x + width, origin.y + height)

    def scan(self) -> dict[str, Any]:
        """Read only the focused game; otherwise return waiting without capture."""
        prior_dpi_context = None
        user32 = None
        try:
            user32, _ = self._load_windows()
            if hasattr(user32, "SetThreadDpiAwarenessContext"):
                # Physical pixel bounds, including on secondary/DPI-scaled displays.
                prior_dpi_context = user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
            game = self._game_rectangle()
            if game is None:
                return _result("waiting", "Focus League in borderless or windowed mode to scan")
            hwnd, (left, top, right, bottom) = game
            width, height = right - left, bottom - top
            x0, y0, x1, y1 = self.CROP
            # Integer crop coordinates are also used to remap OCR boxes exactly.
            crop = (round(width * x0), round(height * y0),
                    round(width * x1), round(height * y1))
            from PIL import ImageGrab
            image = ImageGrab.grab(
                bbox=(left + crop[0], top + crop[1], left + crop[2], top + crop[3]),
                all_screens=True,
            )
            # Discard the in-memory capture if the player switched apps mid-grab.
            if user32.GetForegroundWindow() != hwnd:
                return _result("waiting", "Waiting for League to stay focused")
            # Whole-screen artwork can make Windows OCR drop every heading.
            # Read the title band first, leaving the wider crop as a fallback.
            band = tuple(round(size * fraction) for size, fraction in
                         zip((width, height, width, height), self.TITLE_BAND))
            title_image = image.crop((band[0] - crop[0], band[1] - crop[1],
                                      band[2] - crop[0], band[3] - crop[1]))
            result = self.recognize_image(title_image)
            read_crop, read_image = band, title_image
            if result["status"] == "ok" and not result["lines"]:
                result = self.recognize_image(image)
                read_crop, read_image = crop, image
            result["size"] = [width, height]
            for line in result["lines"]:
                line["x"] = (read_crop[0] + line["x"] * read_image.width) / width
                line["y"] = (read_crop[1] + line["y"] * read_image.height) / height
                line["width"] *= read_image.width / width
                line["height"] *= read_image.height / height
            if result["status"] == "ok" and not result["lines"]:
                result["status"] = "unreadable"
                result["message"] = "No readable game text. Retrying capture…"
            return result
        except Exception as exc:
            return _result("error", str(exc))
        finally:
            if prior_dpi_context and user32 is not None:
                user32.SetThreadDpiAwarenessContext(prior_dpi_context)


def recognize_image(image: Any) -> dict[str, Any]:
    """Convenience entry point for testing a Pillow image without game capture."""
    return CaptureReader().recognize_image(image)
