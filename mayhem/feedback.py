"""User-reviewed feedback drafts. No HTTP, log harvesting, or credentials."""
from __future__ import annotations

import os
import platform
import webbrowser
from urllib.parse import quote, urlencode

from . import __version__

FEEDBACK_EMAIL = "amangousa@gmail.com"
ISSUES_URL = "https://github.com/MrMegaMango/mayhem-companion/issues/new/choose"
CATEGORIES = ("Bug", "Suggestion", "Incorrect stats")
MAX_DESCRIPTION = 6000
MAX_CONTACT = 254
# Some Windows email handlers truncate long protocol URLs. Keep a complete
# report on the clipboard and open a short draft instead of losing its tail.
MAX_MAILTO_LENGTH = 1800
DIAGNOSTIC_KEYS = ("OS", "Python", "Game state", "View", "Auto scan",
                   "Click-through", "Display preference")


def feedback_mode(display_mode, game):
    """Describe the selected view without treating ordinary ARAM as Mayhem."""
    if display_mode == "arena":
        return "Arena"
    if display_mode == "tft":
        return "TFT"
    if game.get("game_mode") == "KIWI" or game.get("queue_id") == 2400:
        return "ARAM Mayhem"
    return "ARAM"


def technical_details(*, game_state, view, auto_scan, locked, portrait):
    """Construct a small allowlist; never accept a game payload or log text."""
    states = {"waiting", "champ_select", "loading", "live", "arena", "tft", "ended", "offline"}
    views = {"augments", "builds", "spells", "champions", "items", "comps"}
    return {
        "OS": f"{platform.system()} {platform.release()}",
        "Python": platform.python_version(),
        "Game state": game_state if game_state in states else "Other / unavailable",
        "View": view if view in views else "Other / unavailable",
        "Auto scan": "On" if auto_scan else "Off",
        "Click-through": "On" if locked else "Off",
        "Display preference": "Portrait" if portrait else "Default",
    }


def build_report(category, description, contact="", include_diagnostics=False, *,
                 mode, diagnostics=None):
    if category not in CATEGORIES:
        raise ValueError("Choose a feedback type.")
    description = description.strip()
    if not description:
        raise ValueError("Describe the problem or suggestion first.")
    if len(description) > MAX_DESCRIPTION:
        raise ValueError(f"Keep the description under {MAX_DESCRIPTION:,} characters.")
    contact = contact.strip()
    if len(contact) > MAX_CONTACT or "\r" in contact or "\n" in contact:
        raise ValueError(f"Keep the reply address on one line, at most {MAX_CONTACT} characters.")
    if mode not in {"ARAM", "ARAM Mayhem", "Arena", "TFT"}:
        raise ValueError("Unknown game mode.")
    sections = [
        "Mayhem Companion feedback",
        f"Type: {category}\nApp version: {__version__}\nGame mode: {mode}",
        description,
    ]
    if contact:
        sections.append(f"Reply address: {contact}")
    if include_diagnostics and diagnostics:
        # Defense in depth: even a future caller passing extra metadata cannot
        # accidentally add player identity, local paths, auth, or raw logs.
        details = [f"{key}: {str(diagnostics[key])[:100]}"
                   for key in DIAGNOSTIC_KEYS if key in diagnostics]
        if details:
            sections.append("Technical details\n" + "\n".join(details))
    return "\n\n".join(sections) + "\n"


def email_draft(category, mode, report):
    """Return (mailto URI, needs paste). Does not launch or send anything."""
    if category not in CATEGORIES or mode not in {"ARAM", "ARAM Mayhem", "Arena", "TFT"}:
        raise ValueError("Unknown feedback type or game mode.")
    subject = f"[Mayhem Companion] {category} - {mode}"

    def uri(body):
        return "mailto:" + FEEDBACK_EMAIL + "?" + urlencode(
            {"subject": subject, "body": body.replace("\r\n", "\n").replace("\n", "\r\n")},
            quote_via=quote)

    full = uri(report)
    if len(full) <= MAX_MAILTO_LENGTH:
        return full, False
    return uri("Paste the complete feedback report from your clipboard here.\n"
               "You can attach a screenshot before sending.\n"), True


def open_email_draft(uri):
    """Ask the OS to open an email draft; success never means it was sent."""
    if not uri.startswith("mailto:" + FEEDBACK_EMAIL + "?"):
        raise ValueError("Unexpected feedback destination.")
    try:
        if os.name == "nt":
            os.startfile(uri)
            return True
        return bool(webbrowser.open(uri))
    except (OSError, webbrowser.Error):
        return False
