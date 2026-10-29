"""Offline checks for a relocated Windows bundle, without starting the app.

The only file written is the explicitly requested report. No settings, game
APIs, screen capture, tray icon, analytics, or network clients are started.
"""
from __future__ import annotations

import importlib
import ipaddress
import json
from pathlib import Path
import sys
import traceback
from typing import Callable
from urllib.parse import urlsplit

from . import __version__


def _tk_runtime() -> dict:
    import tkinter as tk
    from tkinter import ttk
    from PIL import Image, ImageTk

    root = tk.Tk()
    root.withdraw()
    try:
        image = ImageTk.PhotoImage(Image.new("RGB", (8, 8), "#7ce1bd"), master=root)
        label = ttk.Label(root, text="Mayhem Companion", image=image, compound="left")
        label.pack()
        root.update_idletasks()
        if image.width() != 8 or image.height() != 8:
            raise RuntimeError("Tk image initialization failed")
    finally:
        root.destroy()
    return {"status": "passed"}


def _arena_reference() -> dict:
    from .arena_data import _reference_names

    reference = json.loads(Path(__file__).with_name("arena_augments.json").read_text(encoding="utf-8"))
    if not isinstance(reference, dict) or reference.get("reference_only") is not True:
        raise ValueError("Arena reference resource is invalid")
    names = _reference_names()
    if not names:
        raise ValueError("Arena reference resource did not load")
    return {"status": "passed", "entries": len(names)}


def _analytics_configuration() -> dict:
    from .app import load_analytics_endpoint

    # Loading this pure function does not instantiate Companion or Analytics.
    configuration = json.loads(Path(__file__).with_name("analytics_config.json").read_text(encoding="utf-8"))
    if not isinstance(configuration, dict) or not isinstance(configuration.get("endpoint"), str):
        raise ValueError("Analytics configuration is invalid")
    endpoint = configuration["endpoint"]
    if load_analytics_endpoint() != endpoint:
        raise ValueError("Bundled analytics configuration did not load")
    if endpoint:
        parsed = urlsplit(endpoint)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("Analytics endpoint is not a public HTTPS endpoint")
        host = parsed.hostname.casefold()
        if host == "localhost" or host.endswith((".localhost", ".local")):
            raise ValueError("Analytics endpoint must not point to the local machine")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            if not address.is_global:
                raise ValueError("Analytics endpoint must not point to a private address")
    return {"status": "passed", "configured": bool(endpoint)}


def _tray_backend() -> dict:
    pystray = importlib.import_module("pystray")
    backend = importlib.import_module("pystray._win32")
    importlib.import_module("pystray._util.win32")
    if pystray.Icon is not backend.Icon:
        raise RuntimeError("The Windows tray backend was not selected")
    # LGPL library stays replaceable instead of being duplicated in the PYZ.
    external_source = Path(pystray.__file__).suffix == ".py"
    if getattr(sys, "frozen", False) and not external_source:
        raise RuntimeError("The tray library must load from replaceable Python source")
    from .tray import create_icon_image
    image = create_icon_image()
    try:
        if image.size != (64, 64):
            raise RuntimeError("The tray image did not render")
    finally:
        image.close()
    return {"status": "passed", "replaceable_source": external_source}


def _ocr_runtime() -> dict:
    # Import every native dependency before distinguishing an absent OS language
    # capability from an incomplete Python bundle.
    importlib.import_module("winrt.windows.foundation")
    importlib.import_module("winrt.windows.foundation.collections")
    importlib.import_module("winrt.windows.graphics.imaging")
    importlib.import_module("winrt.windows.storage.streams")
    from winrt.windows.globalization import Language
    from winrt.windows.media.ocr import OcrEngine
    from PIL import Image, ImageDraw, ImageFont
    from .capture import CaptureReader

    if not OcrEngine.is_language_supported(Language("en-US")):
        return {"status": "host_requirement_missing", "code": "english_ocr_not_installed"}

    image = Image.new("RGB", (640, 140), "white")
    try:
        font = ImageFont.load_default(size=48)
        ImageDraw.Draw(image).text((24, 36), "Infernal Conduit", font=font, fill="black")
        result = CaptureReader().recognize_image(image)
    finally:
        image.close()
    if result.get("status") != "ok":
        return {"status": "failed", "code": "ocr_recognition_failed"}
    recognized = " ".join(" ".join(line.get("text", "") for line in result.get("lines", [])).casefold().split())
    matched = "infernal conduit" in recognized
    return {"status": "passed" if matched else "failed",
            "code": "synthetic_text_recognized" if matched else "synthetic_text_not_recognized",
            "matched": matched}


def collect_report() -> tuple[dict, int]:
    """Return a small report with no paths, account data, or raw exception text."""
    checks = {}
    if sys.platform != "win32":
        checks["platform"] = {"status": "failed", "code": "windows_required"}
    else:
        callbacks: tuple[tuple[str, Callable[[], dict]], ...] = (
            ("tk", _tk_runtime),
            ("arena_reference", _arena_reference),
            ("analytics_configuration", _analytics_configuration),
            ("tray_backend", _tray_backend),
            ("ocr", _ocr_runtime),
        )
        for name, callback in callbacks:
            try:
                checks[name] = callback()
            except Exception as error:
                checks[name] = {"status": "failed", "code": "runtime_check_failed",
                                "error_type": type(error).__name__,
                                "frames": [{"module": Path(frame.filename).name, "line": frame.lineno}
                                           for frame in traceback.extract_tb(error.__traceback__)]}

    if any(check["status"] == "failed" for check in checks.values()):
        status, exit_code = "failed", 1
    elif any(check["status"] == "host_requirement_missing" for check in checks.values()):
        status, exit_code = "host_requirement_missing", 2
    else:
        status, exit_code = "passed", 0
    return {"schema_version": 1, "app_version": __version__,
            "frozen": bool(getattr(sys, "frozen", False)),
            "status": status, "exit_code": exit_code, "checks": checks}, exit_code


def run_self_test(report_path: Path) -> int:
    """Write the requested JSON report and return a process exit status."""
    report_path = Path(report_path)
    if not report_path.is_absolute():
        return 2
    report, exit_code = collect_report()
    try:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except OSError:
        return 1
    return exit_code
