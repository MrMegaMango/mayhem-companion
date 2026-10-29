"""Local UI preview using production widgets and real cached public statistics.

Usage: python tools/preview_ui.py --cache PATH --view augments
Views are augments, select, builds, followups, arena, tft, tft-items,
and tft-augments. F1..F8 switch views. --command-file can read JSON with
{"view": "select"} or {"close": true}; a revision field repeats a command.

No app constructor, League/OCR integration, hotkeys, tray, analytics,
workers, network requests, settings access, or screen/input automation.
Cache is read only; preview state is isolated in a TemporaryDirectory.
"""
from __future__ import annotations
import argparse
import copy
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import sys
import tempfile
import threading
from types import SimpleNamespace
import tkinter as tk
import webbrowser

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["MAYHEM_QA_CAPTURE"] = "1"
from mayhem.data import DataUnavailableError, StatsProvider, _catalog_resource, _version_row

VIEWS = ("augments", "select", "builds", "followups", "arena", "tft", "tft-items", "tft-augments")
AUGMENTS = ("1045", "1030", "1048")
CHOICES = ("157", "1", "33", "51", "4", "63", "55", "10")


class CachedStats(StatsProvider):
    """Production validators/normalizers, with all refreshes and writes disabled."""
    def __init__(self, cache, scratch):
        super().__init__(scratch)
        self.cache_dir = cache
        version = _version_row(self._read_json(cache / "last-champion-63-all.json"))
        self._active_version = version
        self._catalog = {kind: self._fetch(self._resource_url(version, kind), _catalog_resource)[0]
                         for kind in ("champions", "augments")}
        self._catalog.update(self._metadata(version, True))
        self._catalog_until = math.inf

    def _fetch(self, url, validator, ttl=None):
        cached = self._cached(url, validator)
        if cached is None:
            raise DataUnavailableError(f"Preview cache is missing: {url}", transient=True)
        return copy.deepcopy(validator(cached["data"])), True

    def _download(self, url):
        raise RuntimeError("Networking is disabled in the UI preview")

    def _write_json(self, path, data):
        pass


def cached_arena(cache):
    from mayhem.arena_data import ArenaProvider
    class CachedArena(ArenaProvider):
        def __init__(self):
            self.cache_dir, self._memory, self._catalog = cache / "arena", {}, None

        def _get(self, url, parse, **kwargs):
            entry = self._read(url)
            if entry is None:
                raise DataUnavailableError(f"Preview cache is missing: {url}", transient=True)
            value = parse(entry["content"])
            if isinstance(value, dict) and "source" in value:
                value.update(stale=True, fetched_at=datetime.fromtimestamp(
                    entry["fetched_at"], timezone.utc).isoformat(timespec="seconds"))
            return value

        def _download(self, url):
            raise RuntimeError("Networking is disabled in the UI preview")

        def _write(self, url, value):
            pass
    provider = CachedArena()
    return provider.load(), provider.champion_for("157")


def summary(provider, cache):
    catalog = provider.load_catalog()
    spells, builds = provider.summoner_spells_for("157"), provider.item_builds_for("63")
    choices = provider.champion_rates_for(list(CHOICES))
    return {"source": catalog["source"], "version": catalog["version"], "data_date": catalog["data_date"],
            "augment_examples": [provider.rate_for("63", ident) for ident in AUGMENTS],
            "spell_pairs": len(spells["rows"]), "champion_choices": len(choices["rows"]),
            "core_groups": len(builds["groups"]),
            "core_sets": sum(len(group["rows"]) for group in builds["groups"]),
            "tft_snapshot_available": (cache / "tft" / "snapshot.json").is_file(),
            "arena_snapshot_available": (cache / "arena").is_dir()}


def preview_class():
    from PIL import Image, ImageTk
    from mayhem.app import Companion, ACCENT
    from mayhem.icons import _decode_image

    class Preview(Companion):
        def __init__(self, root, options, provider, scratch):
            # Never call Companion.__init__: it starts real services.
            self.root, self.options, self.provider = root, options, provider
            self.storage, self.settings_path, self.settings = scratch, scratch / "settings.json", {}
            self.events, self.requests, self.stop = queue.Queue(), queue.Queue(), threading.Event()
            for name in ("_closed", "recovery_visible", "_autostart_busy", "hidden", "locked", "game_live",
                         "auto_visible", "browse_mode", "champ_select", "champion_preview", "is_auto_offer",
                         "offers_active", "choice_active", "inventory_current", "tft_loaded", "tft_pending",
                         "arena_pending"):
                setattr(self, name, False)
            for name in ("feedback_dialog", "analytics_dialog", "champion", "live_champion", "_placement_job",
                         "_last_display", "visibility_override", "last_active_status", "drag_start",
                         "spell_champion", "spell_data", "choice_ids", "choice_context", "build_champion",
                         "build_data", "live_items", "inventory_champion", "build_order_key", "tft_unit",
                         "arena_champion", "analytics"):
                setattr(self, name, None)
            for name in ("token", "spell_token", "choice_token", "build_token", "tft_token", "tft_item_token",
                         "arena_token", "arena_champion_token"):
                setattr(self, name, 0)
            self.catalog = provider.load_catalog()
            self.follow = self.view_automatic = True
            self.game_status, self.display_mode = "waiting", "aram"
            self.offer_ids = [None] * 3
            self.cards, self.build_cards, self.augment_names, self.champion_names, self.live_spells = [], [], [], [], []
            self.augment_by_name, self.item_images, self.last_game_context = {}, {}, {}
            self.current_preview, self.command_seen = options.view, None
            self.icons = type("NoWorker", (), {"request": lambda *args: False})()
            for method in ("help", "show_feedback", "show_analytics", "change_scan", "toggle_lock",
                           "auto_champion", "select_champion", "filter_champions", "select_augment",
                           "filter_augments", "clear_offers", "open_source", "start_drag", "drag", "end_drag",
                           "lookup", "lookup_builds", "lookup_spells", "rescan", "load_tft", "load_arena",
                           "load_arena_champion", "toggle_autostart", "save_settings", "sync_analytics"):
                setattr(self, method, lambda *args, **kwargs: None)
            self.build()
            root.title("Mayhem Companion — UI preview (cached examples)")
            root.attributes("-topmost", False)
            root.protocol("WM_DELETE_WINDOW", self.close)
            self.auto.set(False)
            for number, view in enumerate(VIEWS, 1):
                root.bind(f"<F{number}>", lambda _event, selected=view: self.show_preview(selected))
            self.show_preview(options.view)
            if options.command_file:
                root.after(200, self.poll_command)
            root.after(options.timeout * 1000, self.close)

        def close(self):
            if not self._closed:
                self._closed = True
                self.stop.set()
                self.root.destroy()

        def toggle_visible(self):
            self.root.iconify()

        def change_game_mode(self):
            self.show_preview({"Arena": "arena", "TFT": "tft"}.get(self.mode_choice.get(), "augments"))

        def panel_placement(self, height, position=None):
            return self.options.x, self.options.y, min(self.options.max_height, max(610, height))

        def fit_window(self):
            if self._closed:
                return
            super().fit_window()
            self.root.update_idletasks()
            if self.options.ready:
                self.options.ready.write_text(json.dumps({
                    "pid": os.getpid(), "title": self.root.title(), "geometry": self.root.geometry(),
                    "view": self.current_preview, "version": self.catalog["version"],
                    "data_date": self.catalog["data_date"], "scratch": str(self.storage),
                    "cache": str(self.options.cache), "source": str(ROOT)}, indent=2), encoding="utf-8")

        def request_tft_icon(self, url):
            if not isinstance(url, str) or not url:
                return None
            if url not in self.item_images:
                path = self.options.cache / "icons" / (hashlib.sha256(url.encode()).hexdigest() + ".img")
                try:
                    pixels = _decode_image(path.read_bytes())
                    pixels.thumbnail((36, 36), Image.Resampling.LANCZOS)
                    self.item_images[url] = ImageTk.PhotoImage(pixels, master=self.root)
                except (OSError, ValueError, SyntaxError):
                    return None
            return self.item_images[url]

        def render_build_item(self, item, name, url):
            self.request_tft_icon(url)
            super().render_build_item(item, name, url)

        def load_tft_items(self, unit, force=False):
            for path in sorted(self.options.cache.glob("builds-*.json")) + sorted((self.options.cache / "tft").glob("builds-*.json")):
                data = json.loads(path.read_text(encoding="utf-8"))
                if str(data.get("unit", {}).get("id")) == str(unit) or str(unit) in path.name:
                    self.tft_panel.set_items(unit, data)
                    return
            self.tft_panel.set_items_error(unit, "No item-build snapshot for this unit in the preview cache.")

        def show_preview(self, view):
            if view not in VIEWS:
                raise ValueError(f"Unknown preview: {view}")
            self.current_preview = view
            for panel in (self.body, self.tft_panel, self.arena_panel, self.choice_panel):
                panel.pack_forget()
            self.badge.configure(text="EXAMPLE", fg=ACCENT)
            self.game_live = self.champ_select = self.inventory_current = self.choice_active = False
            self.live_items = self.inventory_champion = None
            self.spells_tab.configure(text="Spells")
            if view == "arena":
                self.display_mode = "arena"
                self.mode_choice.set("Arena")
                self.brand.configure(text="ARENA")
                self.arena_panel.pack(fill="both", expand=True, padx=16)
                catalog, data = cached_arena(self.options.cache)
                self.arena_panel.set_catalog(catalog)
                self.arena_panel.choose_champion("157")
                self.arena_panel.set_champion_data("157", data)
                self.arena_panel.message.set("EXAMPLE · local public-data snapshot")
            elif view.startswith("tft"):
                self.display_mode = "tft"
                self.mode_choice.set("TFT")
                self.brand.configure(text="TFT")
                self.tft_panel.pack(fill="both", expand=True, padx=16, pady=(0, 10))
                data = json.loads((self.options.cache / "tft" / "snapshot.json").read_text(encoding="utf-8"))
                self.tft_panel.view.set({"tft": "comps", "tft-items": "items", "tft-augments": "augments"}[view])
                if view == "tft-items":
                    self.tft_panel.item_unit_id = "DA_18_Ahri"
                self.tft_panel.set_data(data)
                self.tft_panel.message.set("EXAMPLE · local public-data snapshot")
            else:
                self.display_mode = "aram"
                self.mode_choice.set("ARAM")
                self.brand.configure(text="MAYHEM")
                self.body.pack(fill="both", expand=True, padx=16)
                self.champion = self.live_champion = "157" if view == "select" else "63"
                self.champ_var.set(self.catalog["champions"][self.champion]["name"])
                self.champ_box["values"] = [self.champ_var.get()]
                if view == "augments":
                    self.offer_ids = list(AUGMENTS)
                    for ident, card in zip(AUGMENTS, self.cards):
                        card["var"].set(self.catalog["augments"][ident]["name"])
                        card["box"]["values"] = [card["var"].get()]
                    self.switch_view("augments")
                    self.render_rates([self.provider.rate_for("63", ident) for ident in AUGMENTS])
                elif view == "select":
                    self.champ_select = self.choice_active = True
                    self.spell_data = self.provider.summoner_spells_for(self.champion)
                    self.switch_view("spells")
                    self.choice_panel.pack(fill="x", before=self.spell_toolbar, pady=(0, 12))
                    self.choice_panel.set_context(list(CHOICES), self.champion, self.catalog["champions"])
                    self.choice_panel.set_data(self.provider.champion_rates_for(list(CHOICES)))
                    self.spells_tab.configure(text="Select")
                else:
                    self.build_data = self.provider.item_builds_for(self.champion)
                    self.build_group.set("")
                    if view == "followups":
                        profile = next(row for group in self.build_data["groups"] for row in group["rows"]
                                       if any(route.get("later_items") for route in row.get("routes", [])))
                        self.game_live = self.inventory_current = True
                        self.inventory_champion, self.live_items = self.champion, list(profile["item_ids"])
                    self.switch_view("builds")
                self.status.set("Example selection · cached public statistics")
            self.root.after_idle(self.fit_window)

        def smoke_ui(self):
            """Invoke native callbacks directly; never inject input events."""
            report = {"checks": [], "views": {}, "errors": []}
            callback_errors = []
            self.root.report_callback_exception = lambda kind, value, tb: callback_errors.append(str(value))
            original_height = self.options.max_height
            try:
                self.show_preview("augments")
                self.fit_window()
                initial = bool(self.augment_options.winfo_manager())
                self.toggle_augment_options()
                assert bool(self.augment_options.winfo_manager()) != initial
                self.toggle_augment_options()
                assert bool(self.augment_options.winfo_manager()) == initial
                report["checks"].append("augment options toggle")
                self.augment_names = [row["name"] for row in self.catalog["augments"].values()]
                self.augment_by_name = {row["name"].casefold(): ident for ident, row in self.catalog["augments"].items()}
                card = self.cards[0]
                self.edit_augment(0, True)
                assert card["editor"].winfo_manager()
                card["var"].set("not a real augment")
                Companion.filter_augments(self, SimpleNamespace(keysym="a"), 0)
                assert self.offer_ids[0] is None and card["value"].cget("text") == "—"
                saved_lookup = self.lookup
                self.lookup = lambda: Companion.lookup(self)
                try:
                    Companion.select_augment(self, 0)
                    assert self.offer_ids[0] is None and card["editor"].winfo_manager()
                    card["var"].set(self.catalog["augments"][AUGMENTS[0]]["name"])
                    Companion.select_augment(self, 0)
                    assert self.offer_ids[0] == AUGMENTS[0] and not card["editor"].winfo_manager()
                finally:
                    self.lookup = saved_lookup
                self.edit_augment(0, True)
                self.edit_augment(0, False)
                assert not card["editor"].winfo_manager()
                report["checks"].append("augment edit open, invalid, valid, close")
                self.options.max_height = 700
                self.show_preview("builds")
                self.fit_window()
                self.root.update_idletasks()
                assert self.root.winfo_height() <= 700, "Build window exceeds 700px"
                for widget in (self.more_button, self.build_note_label):
                    bottom = widget.winfo_rooty() - self.root.winfo_rooty() + widget.winfo_height()
                    assert bottom <= self.root.winfo_height(), "Build footer extends outside client area"
                before = self.build_scroll.yview()
                result = self.scroll_build_results(SimpleNamespace(delta=-120))
                self.root.update_idletasks()
                after = self.build_scroll.yview()
                assert before != (0.0, 1.0), "Build fixture does not need scrolling"
                assert result == "break" and after[0] > before[0], "Scroll callback did not advance"
                report["checks"].append("700px build window, visible footer, native scroll callback")
                report["constrained_build"] = {"geometry": self.root.geometry(), "viewport_height": self.build_scroll.winfo_height()}
                self.options.max_height = original_height
                for view in VIEWS:
                    self.show_preview(view)
                    self.fit_window()
                    self.root.update_idletasks()
                    panel = self.arena_panel if view == "arena" else self.tft_panel if view.startswith("tft") else self.body
                    assert panel.winfo_manager(), f"{view} panel not displayed"
                    assert self.badge.cget("text") == "EXAMPLE"
                    report["views"][view] = self.root.geometry()
                report["checks"].append("all eight cached view transitions")
                assert not callback_errors, "; ".join(callback_errors)
            except Exception as error:
                report["errors"].append(f"{type(error).__name__}: {error}")
            finally:
                report["callback_errors"] = callback_errors
                self.options.max_height = original_height
                self.smoke_failed = bool(report["errors"] or callback_errors)
                print(json.dumps(report, indent=2), flush=True)
                self.close()

        def poll_command(self):
            if self._closed:
                return
            try:
                content = self.options.command_file.read_text(encoding="utf-8")
                if content != self.command_seen:
                    command, self.command_seen = json.loads(content), content
                    if command.get("close"):
                        self.close()
                        return
                    if command.get("view") in VIEWS:
                        self.show_preview(command["view"])
            except (OSError, ValueError):
                pass
            self.root.after(200, self.poll_command)
    return Preview


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cache", required=True, type=Path, help="Existing public-data cache, opened read only")
    parser.add_argument("--view", choices=VIEWS, default="augments")
    parser.add_argument("--command-file", type=Path)
    parser.add_argument("--ready", type=Path, help="Optional status JSON; parent directory must exist")
    parser.add_argument("--x", type=int, default=100)
    parser.add_argument("--y", type=int, default=50)
    parser.add_argument("--max-height", type=int, default=1400)
    parser.add_argument("--timeout", type=int, default=1200)
    parser.add_argument("--check-data", action="store_true", help="Validate cached data without opening a window")
    parser.add_argument("--smoke-ui", action="store_true", help="Check native callbacks, print JSON, then close")
    options = parser.parse_args()
    if not options.cache.is_dir():
        parser.error("--cache must identify an existing public-data cache")
    webbrowser.open = lambda *args, **kwargs: False
    with tempfile.TemporaryDirectory(prefix="mayhem-ui-preview-") as directory:
        scratch = Path(directory)
        provider = CachedStats(options.cache, scratch)
        if options.check_data:
            print(json.dumps(summary(provider, options.cache), indent=2))
            return
        root = tk.Tk()
        root.withdraw()
        preview = preview_class()(root, options, provider, scratch)
        root.deiconify()
        if options.smoke_ui:
            root.after(100, preview.smoke_ui)
        root.mainloop()
        if options.smoke_ui and getattr(preview, "smoke_failed", True):
            raise SystemExit(1)


if __name__ == "__main__":
    main()
