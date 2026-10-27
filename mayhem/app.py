"""Compact Windows companion. Network and OCR run off the UI thread."""
from __future__ import annotations

import argparse
from collections import Counter
import ctypes
import json
import logging
import os
from pathlib import Path
import queue
import re
import subprocess
import threading
import tkinter as tk
from tkinter import ttk
import webbrowser

from PIL import Image, ImageTk

from .windows import Hotkeys, set_window_style
from .worker import Worker
from .data import resolve_champion_id
from .displays import list_displays, select_display, place_panel
from .tray import TrayIcon
from .icons import IconLoader
from .tft_ui import TftPanel
from .champion_choices import ChampionChoicesPanel

BG, PANEL, FIELD = "#10151e", "#192231", "#222e40"
TEXT, MUTED, ACCENT, AMBER = "#edf4ff", "#9aacc4", "#7ce1bd", "#efc47c"
POLICY_URL = "https://developer.riotgames.com/docs/lol#game-policy"


def app_dir():
    # Outside AppData to avoid packaged-host filesystem virtualization.
    return Path.home() / "MayhemCompanion"


def compact_count(count):
    if count >= 1_000_000:
        return f"{count / 1_000_000:.2f}M"
    if count >= 10_000:
        return f"{count / 1000:.1f}k"
    return f"{count:,}"


class Companion:
    def __init__(self, root):
        self.root = root
        # Build off-screen so startup does not flash a panel on the desktop.
        root.withdraw()
        self.storage = app_dir()
        self.storage.mkdir(parents=True, exist_ok=True)
        self.settings_path = self.storage / "settings.json"
        try:
            self.settings = json.loads(self.settings_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.settings = {}
        self.events, self.requests = queue.Queue(), queue.Queue()
        self._closed = self.recovery_visible = False
        self._autostart_busy = False
        self.stop = threading.Event()
        self.catalog = None
        self.champion = self.live_champion = None
        self.follow = True
        self.hidden = True
        self._placement_job = None
        self._last_display = None
        self.locked = self.game_live = False
        self.auto_visible = self.browse_mode = False
        self.visibility_override = None
        self.game_status = "waiting"
        self.last_active_status = None
        self.champ_select = self.champion_preview = False
        self.token = 0
        self.is_auto_offer = False
        self.offers_active = False
        self.view_automatic = True
        self.offer_ids = [None, None, None]
        self.cards = []
        self.augment_names, self.champion_names = [], []
        self.augment_by_name = {}
        self.drag_start = None
        self.spell_token = 0
        self.spell_champion = None
        self.spell_data = None
        self.choice_token = 0
        self.choice_ids = self.choice_context = None
        self.choice_active = False
        self.build_token = 0
        self.build_champion = None
        self.build_data = None
        self.build_cards = []
        self.item_images = {}
        self.live_spells = []
        self.live_items = None
        self.inventory_champion = None
        self.inventory_current = False
        self.build_order_key = None
        self.display_mode = "aram"
        self.last_game_context = {}
        self.tft_token = 0
        self.tft_item_token = 0
        self.tft_unit = None
        self.tft_loaded = self.tft_pending = False
        self.build()
        self.icons = IconLoader(self.events, self.stop, self.storage / "cache" / "icons")
        self.icons.start()
        self.hotkeys = Hotkeys(root, {101: (ord("M"), self.toggle_visible),
                                     102: (ord("S"), self.rescan),
                                     103: (ord("L"), self.toggle_lock)},
                               on_failure=lambda: self.events.put(("hotkeys_failed", None)))
        self.tray = TrayIcon(self.events)
        self.tray.start()
        if len(self.hotkeys.registered) != 3:
            self.status.set("A shortcut is already in use. Window controls still work.")
        self.worker = Worker(self.events, self.requests, self.stop, self.storage / "cache")
        self.worker.start()
        root.after(100, self.drain)
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(300, lambda: set_window_style(root, False))

    def label(self, parent, text="", size=10, color=TEXT, bold=False, **kwargs):
        return tk.Label(parent, text=text, bg=parent.cget("bg"), fg=color,
                        font=("Segoe UI", size, "bold" if bold else "normal"), **kwargs)

    def button(self, parent, text, command, color=FIELD):
        return tk.Button(parent, text=text, command=command, bg=color, fg=TEXT,
                         activebackground="#35475f", activeforeground=TEXT,
                         bd=0, padx=10, pady=5, font=("Segoe UI", 9), cursor="hand2")

    def build(self):
        root = self.root
        root.title("Mayhem Companion")
        root.configure(bg=BG)
        root.overrideredirect(os.environ.get("MAYHEM_QA_CAPTURE") != "1")
        root.attributes("-topmost", True)
        root.attributes("-alpha", 1.0)
        x, y, height = self.panel_placement(610, self.settings.get("position"))
        root.geometry(f"440x{height}+{x}+{y}")
        self.shell = tk.Frame(root, bg=BG, highlightbackground="#35465f", highlightthickness=1)
        self.shell.pack(fill="both", expand=True)
        header = tk.Frame(self.shell, bg=BG)
        header.pack(fill="x", padx=16, pady=(12, 8))
        brand = self.label(header, "MAYHEM", 10, ACCENT, True)
        self.brand = brand
        brand.pack(side="left")
        self.mode_choice = tk.StringVar(value="Auto")
        mode_box = ttk.Combobox(header, textvariable=self.mode_choice, state="readonly",
                               values=["Auto", "ARAM", "TFT"], width=6)
        mode_box.pack(side="left", padx=(10, 0))
        mode_box.bind("<<ComboboxSelected>>", lambda _: self.change_game_mode())
        self.badge = self.label(header, "WAITING", 8, MUTED)
        self.badge.pack(side="left", padx=10)
        self.button(header, "×", self.close, BG).pack(side="right")
        self.button(header, "—", self.toggle_visible, BG).pack(side="right")
        self.button(header, "?", self.help, BG).pack(side="right")
        for widget in [header, brand, self.badge]:
            widget.bind("<ButtonPress-1>", self.start_drag)
            widget.bind("<B1-Motion>", self.drag)
            widget.bind("<ButtonRelease-1>", self.end_drag)
        self.body = tk.Frame(self.shell, bg=BG)
        self.body.pack(fill="both", expand=True, padx=16)
        champ_row = tk.Frame(self.body, bg=BG)
        champ_row.pack(fill="x", pady=(0, 7))
        self.champ_var = tk.StringVar(value="Auto detect champion")
        self.champ_box = ttk.Combobox(champ_row, textvariable=self.champ_var, font=("Segoe UI", 15), width=20)
        self.champ_box.pack(side="left", fill="x", expand=True)
        self.champ_box.bind("<<ComboboxSelected>>", self.select_champion)
        self.champ_box.bind("<Return>", self.select_champion)
        self.champ_box.bind("<KeyRelease>", self.filter_champions)
        self.button(champ_row, "Auto", self.auto_champion).pack(side="right", padx=(8, 0))
        tabs = tk.Frame(self.body, bg=BG)
        tabs.pack(fill="x", pady=(0, 8))
        self.view = tk.StringVar(value="augments")
        self.augment_tab = self.button(tabs, "Augments", lambda: self.switch_view("augments"), FIELD)
        self.augment_tab.pack(side="left", fill="x", expand=True)
        self.builds_tab = self.button(tabs, "Builds", lambda: self.switch_view("builds"), BG)
        self.builds_tab.pack(side="left", fill="x", expand=True)
        self.spells_tab = self.button(tabs, "Spells", lambda: self.switch_view("spells"), BG)
        self.spells_tab.pack(side="left", fill="x", expand=True)
        self.pages = tk.Frame(self.body, bg=BG)
        self.pages.pack(fill="both", expand=True)
        self.augment_panel = tk.Frame(self.pages, bg=BG)
        self.augment_panel.pack(fill="both", expand=True)
        controls = tk.Frame(self.augment_panel, bg=BG)
        controls.pack(fill="x", pady=(0, 10))
        self.stage = tk.StringVar(value="All picks")
        stage_box = ttk.Combobox(controls, textvariable=self.stage, state="readonly", width=10,
                                values=["All picks", "Pick 1", "Pick 2", "Pick 3", "Pick 4"])
        stage_box.pack(side="left")
        stage_box.bind("<<ComboboxSelected>>", lambda _: self.lookup())
        self.auto = tk.BooleanVar(value=True)
        tk.Checkbutton(controls, text="Auto scan", variable=self.auto, command=self.change_scan,
                       bg=BG, fg=MUTED, activebackground=BG, activeforeground=TEXT,
                       selectcolor=FIELD, font=("Segoe UI", 9)).pack(side="left", padx=10)
        self.lock_button = self.button(controls, "Lock", self.toggle_lock)
        self.lock_button.pack(side="right")
        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure("TCombobox", fieldbackground=FIELD, background=FIELD,
                        foreground=TEXT, arrowcolor=MUTED, bordercolor=FIELD,
                        lightcolor=FIELD, darkcolor=FIELD, padding=6)
        style.map("TCombobox", fieldbackground=[("readonly", FIELD)],
                  foreground=[("readonly", TEXT)], selectbackground=[("!disabled", FIELD)],
                  selectforeground=[("!disabled", TEXT)])
        root.option_add("*TCombobox*Listbox.background", FIELD)
        root.option_add("*TCombobox*Listbox.foreground", TEXT)
        root.option_add("*TCombobox*Listbox.selectBackground", "#36516a")
        root.option_add("*TCombobox*Listbox.font", "{Segoe UI} 10")
        for index in range(3):
            card = tk.Frame(self.augment_panel, bg=PANEL, padx=12, pady=10)
            card.pack(fill="x", pady=(0, 8))
            top = tk.Frame(card, bg=PANEL)
            top.pack(fill="x")
            self.label(top, "•", 10, MUTED).pack(side="left", padx=(0, 8))
            name = tk.StringVar(value="")
            box = ttk.Combobox(top, textvariable=name, font=("Segoe UI", 11), width=29)
            box.pack(side="left", fill="x", expand=True)
            box.bind("<<ComboboxSelected>>", lambda _, i=index: self.select_augment(i))
            box.bind("<Return>", lambda _, i=index: self.select_augment(i))
            box.bind("<KeyRelease>", lambda e, i=index: self.filter_augments(e, i))
            row = tk.Frame(card, bg=PANEL)
            row.pack(fill="x", pady=(4, 0))
            value = self.label(row, "—", 25, TEXT, True)
            value.pack(side="left")
            metadata = self.label(row, "Choose an augment", 9, MUTED, anchor="e", justify="right")
            metadata.pack(side="right", fill="x", expand=True)
            self.cards.append({"var": name, "box": box, "value": value, "meta": metadata})
        self.build_spells(style)
        self.build_items()
        self.status = tk.StringVar(value="Loading augment data…")
        self.label(self.body, size=9, color=MUTED, textvariable=self.status,
                   wraplength=398, anchor="w", justify="left").pack(fill="x", pady=(2, 6))
        footer = tk.Frame(self.body, bg=BG)
        footer.pack(fill="x", side="bottom", pady=(0, 10))
        self.source = tk.StringVar(value="ARAMKit · champion-specific data")
        link = self.label(footer, size=8, color=MUTED, textvariable=self.source, cursor="hand2", anchor="w")
        link.pack(fill="x")
        link.bind("<Button-1>", lambda _: self.open_source())
        buttons = tk.Frame(footer, bg=BG)
        buttons.pack(fill="x", pady=(6, 0))
        self.button(buttons, "Clear", self.clear_offers).pack(side="left")
        self.button(buttons, "Retry data", lambda: self.requests.put(("retry", None))).pack(side="left", padx=6)
        self.label(buttons, "Ctrl+Alt+M  show / hide", 8, MUTED).pack(side="right")
        self.tft_panel = TftPanel(self.shell, lambda: self.load_tft(force=True),
                                  on_resize=lambda: self.root.after_idle(self.fit_window),
                                  request_icon=self.request_tft_icon, request_items=self.load_tft_items)
        root.update_idletasks()
        x, y, height = self.panel_placement(self.shell.winfo_reqheight(), (x, y))
        root.geometry(f"440x{height}+{x}+{y}")

    def request_tft_icon(self, url):
        photo = self.item_images.get(url)
        if url and photo is None:
            self.icons.request(url)
        return photo

    def load_tft(self, force=False):
        if self.tft_pending:
            return
        self.tft_token += 1
        self.tft_item_token += 1
        self.tft_unit = None
        self.tft_pending = True
        self.tft_panel.set_loading()
        self.requests.put(("tft", (self.tft_token, force)))

    def load_tft_items(self, unit, force=False):
        self.tft_item_token += 1
        self.tft_unit = unit
        self.requests.put(("tft_items", (self.tft_item_token, unit, force)))

    def set_game_mode(self, mode):
        if mode == self.display_mode:
            return
        self.display_mode = mode
        self.brand.configure(text="TFT" if mode == "tft" else "MAYHEM")
        if mode == "tft":
            self.body.pack_forget()
            self.tft_panel.pack(fill="both", expand=True, padx=16, pady=(0, 10))
            self.load_tft()
        else:
            self.tft_panel.pack_forget()
            self.body.pack(fill="both", expand=True, padx=16)
            # Offers may have changed while the TFT reference was open.
            self.requests.put(("scan", None))
        self.root.after_idle(self.fit_window)

    def change_game_mode(self):
        choice = self.mode_choice.get()
        mode = "tft" if choice == "TFT" or (choice == "Auto" and
                   self.last_game_context.get("is_tft") is True) else "aram"
        self.set_game_mode(mode)
        if choice != "Auto":
            self.set_manual_visible(True)
        else:
            self.visibility_override = None
            self.browse_mode = False
        self.handle_game(self.last_game_context)

    def start_drag(self, event):
        self.drag_start = (event.x_root - self.root.winfo_x(), event.y_root - self.root.winfo_y())

    def drag(self, event):
        if self.drag_start:
            self.root.geometry(f"+{event.x_root-self.drag_start[0]}+{event.y_root-self.drag_start[1]}")

    def end_drag(self, _event):
        self.drag_start = None
        self.schedule_placement_check()

    def filter_champions(self, event):
        if event.keysym not in ("Return", "Up", "Down", "Escape"):
            query = self.champ_var.get().casefold()
            self.champ_box["values"] = [n for n in self.champion_names if query in n.casefold()]
            # Editing a name must immediately invalidate the old champion's rates.
            self.follow = False
            self.champion = None
            self.lookup()

    def filter_augments(self, event, index):
        if event.keysym not in ("Return", "Up", "Down", "Escape"):
            self.view_automatic = False
            query = self.cards[index]["var"].get().casefold()
            self.cards[index]["box"]["values"] = [n for n in self.augment_names if query in n.casefold()]
            self.offer_ids[index] = None
            self.cards[index]["value"].configure(text="—", fg=TEXT)
            self.cards[index]["meta"].configure(text="Press Enter or select a match", fg=MUTED)
            self.token += 1

    def select_champion(self, _=None):
        if not self.catalog:
            return
        ident = resolve_champion_id(self.champ_var.get().strip(), self.catalog["champions"])
        if not ident:
            self.status.set("Choose a champion from the list, or use Auto.")
            return
        self.follow = False
        self.champion = str(ident)
        self.champ_var.set(self.catalog["champions"][str(ident)]["name"])
        self.badge.configure(text="BROWSE", fg=AMBER)
        self.lookup()

    def auto_champion(self):
        self.follow = True
        self.offers_active = False
        self.champion = self.live_champion
        self.clear_offers()
        self.requests.put(("scan", None))
        self.champ_var.set(self.catalog["champions"][self.champion]["name"]
                           if self.champion and self.catalog else "Auto detect champion")
        active = self.game_live or self.champ_select
        self.badge.configure(text="SELECT" if self.champ_select else "LIVE" if self.game_live else "WAITING",
                             fg=ACCENT if active else MUTED)
        if self.champ_select:
            self.switch_view("spells", automatic=True)
        elif self.game_live:
            self.switch_view("builds", automatic=True)
        self.lookup()

    def select_augment(self, index):
        self.view_automatic = False
        value = self.cards[index]["var"].get().strip().casefold()
        ident = self.augment_by_name.get(value)
        self.offer_ids[index] = ident
        self.is_auto_offer = False
        if ident:
            self.cards[index]["var"].set(self.catalog["augments"][ident]["name"])
        self.lookup()

    def lookup(self):
        self.lookup_spells()
        self.lookup_builds()
        self.token += 1
        for ident, card in zip(self.offer_ids, self.cards):
            card["value"].configure(text="—", fg=TEXT)
            card["meta"].configure(text=("Loading…" if self.champion else "Select a champion")
                                   if ident else "Choose an augment", fg=MUTED)
        if not self.champion or not any(self.offer_ids):
            return
        stage = None if self.stage.get() == "All picks" else int(self.stage.get().split()[-1])
        self.requests.put(("lookup", (self.token, self.champion, list(self.offer_ids), stage)))

    def build_spells(self, style):
        self.spell_panel = tk.Frame(self.pages, bg=BG)
        self.choice_panel = ChampionChoicesPanel(
            self.spell_panel, request_icon=self.request_tft_icon,
            on_resize=lambda: self.root.after_idle(self.fit_window))
        toolbar = tk.Frame(self.spell_panel, bg=BG)
        self.spell_toolbar = toolbar
        toolbar.pack(fill="x", pady=(0, 8))
        self.label(toolbar, "Spell pairs", 10, TEXT, True).pack(side="left")
        self.spell_sort = tk.StringVar(value="Win rate")
        sort_box = ttk.Combobox(toolbar, textvariable=self.spell_sort, state="readonly", width=10,
                               values=["Win rate", "Games", "Pick rate"])
        sort_box.pack(side="right")
        sort_box.bind("<<ComboboxSelected>>", lambda _: self.render_spells())
        self.spell_summary = tk.StringVar(value="Choose a champion to see spell-pair win rates.")
        self.label(self.spell_panel, size=9, color=MUTED, textvariable=self.spell_summary,
                   wraplength=398, justify="left", anchor="w").pack(fill="x", pady=(0, 8))
        style.configure("Spells.Treeview", background=PANEL, fieldbackground=PANEL, foreground=TEXT,
                        borderwidth=0, rowheight=40, font=("Segoe UI", 9))
        style.configure("Spells.Treeview.Heading", background=FIELD, foreground=MUTED,
                        font=("Segoe UI", 8), relief="flat")
        style.map("Spells.Treeview", background=[("selected", "#30425b")], foreground=[("selected", TEXT)])
        table = tk.Frame(self.spell_panel, bg=BG)
        table.pack(fill="both", expand=True)
        self.spell_tree = ttk.Treeview(table, columns=("pair", "win", "games", "pick"),
                                      show="headings", height=1, style="Spells.Treeview", selectmode="none")
        for key, title, width in [("pair", "PAIR", 160), ("win", "WIN %", 67),
                                  ("games", "GAMES", 75), ("pick", "PICK %", 65)]:
            self.spell_tree.heading(key, text=title)
            self.spell_tree.column(key, width=width, minwidth=40, anchor="w" if key == "pair" else "e")
        self.spell_tree.tag_configure("current", background="#203c36", foreground=ACCENT)
        self.spell_tree.tag_configure("low_sample", foreground=AMBER)
        scrollbar = ttk.Scrollbar(table, orient="vertical", command=self.spell_tree.yview)
        self.spell_tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.spell_tree.pack(side="left", fill="both", expand=True)
        self.spell_note = tk.StringVar(value="")
        self.label(self.spell_panel, size=8, color=MUTED, textvariable=self.spell_note,
                   anchor="w").pack(fill="x", pady=(7, 0))

    def update_champion_choices(self, game, *, force=False):
        active = (self.display_mode == "aram" and game.get("status") == "champ_select"
                  and game.get("is_aram") is True)
        if not active:
            if self.choice_active:
                self.choice_active = False
                self.choice_token += 1
                self.choice_ids = self.choice_context = None
                self.choice_panel.set_context(None, None, {})
                self.choice_panel.pack_forget()
                self.spells_tab.configure(text="Spells")
                self.root.after_idle(self.fit_window)
            return
        if not self.choice_active:
            self.choice_active = True
            self.choice_panel.pack(fill="x", before=self.spell_toolbar, pady=(0, 12))
            self.spells_tab.configure(text="Select")
            self.root.after_idle(self.fit_window)
        available = game.get("available_champion_ids")
        ids = tuple(sorted(set(available))) if isinstance(available, list) else None
        current = game.get("champion_id") if not game.get("champion_preview") else None
        names = self.catalog.get("champions", {}) if self.catalog else {}
        incomplete = bool(game.get("availability_incomplete"))
        context = (ids, current, incomplete, id(self.catalog))
        if context != self.choice_context or force:
            self.choice_context = context
            self.choice_panel.set_context(list(ids) if ids is not None else None, current, names,
                                          incomplete=incomplete)
        if ids != self.choice_ids or force:
            self.choice_token += 1
            self.choice_ids = ids
            if ids:
                self.choice_panel.set_loading()
                self.requests.put(("champions", (self.choice_token, list(ids))))

    def build_items(self):
        self.build_panel = tk.Frame(self.pages, bg=BG)
        toolbar = tk.Frame(self.build_panel, bg=BG)
        toolbar.pack(fill="x", pady=(0, 8))
        self.build_group = tk.StringVar(value="")
        self.build_group_box = ttk.Combobox(toolbar, textvariable=self.build_group,
                                            state="readonly", width=17)
        self.build_group_box.pack(side="left")
        self.build_group_box.bind("<<ComboboxSelected>>", lambda _: self.render_builds())
        self.build_sort = tk.StringVar(value="Win rate")
        sort_box = ttk.Combobox(toolbar, textvariable=self.build_sort, state="readonly", width=10,
                               values=["Win rate", "Games", "Pick rate"])
        sort_box.pack(side="right")
        sort_box.bind("<<ComboboxSelected>>", lambda _: self.render_builds())
        self.build_summary = tk.StringVar(value="Choose a champion to see core builds.")
        self.label(self.build_panel, size=9, color=MUTED, textvariable=self.build_summary,
                   wraplength=398, anchor="w", justify="left").pack(fill="x", pady=(0, 8))
        self.build_context_panel = tk.Frame(self.build_panel, bg=BG)
        order_bar = tk.Frame(self.build_context_panel, bg=BG)
        order_bar.pack(fill="x", pady=(0, 6))
        self.label(order_bar, "Core purchase order", 8, MUTED).pack(side="left")
        self.build_order = tk.StringVar(value="")
        self.build_order_box = ttk.Combobox(order_bar, textvariable=self.build_order,
                                           state="readonly", width=23)
        self.build_order_box.pack(side="right")
        self.build_order_box.bind("<<ComboboxSelected>>", lambda _: self.render_builds())
        core = tk.Frame(self.build_context_panel, bg=BG)
        core.pack(fill="x")
        self.build_core_items = []
        for index in range(3):
            core.columnconfigure(index, weight=1, uniform="core")
            cell = tk.Frame(core, bg=BG)
            cell.grid(row=0, column=index, sticky="nsew", padx=3)
            icon = tk.Label(cell, bg=BG, bd=0)
            icon.pack()
            name = self.label(cell, size=8, color=TEXT, wraplength=120, justify="center")
            name.pack(fill="x", pady=(3, 0))
            self.build_core_items.append({"icon": icon, "name": name, "icon_url": None})
        for _ in range(5):
            frame = tk.Frame(self.build_panel, bg=PANEL, padx=12, pady=9)
            top = tk.Frame(frame, bg=PANEL)
            top.pack(fill="x")
            rate = tk.Frame(top, bg=PANEL)
            rate.pack(side="right", anchor="n", padx=(8, 0))
            value = self.label(rate, "—", 21, TEXT, True)
            value.pack(anchor="e")
            self.label(rate, "WIN RATE", 7, MUTED).pack(anchor="e")
            items_panel = tk.Frame(top, bg=PANEL)
            items_panel.pack(side="left", fill="x", expand=True)
            items = []
            for _ in range(3):
                item_row = tk.Frame(items_panel, bg=PANEL)
                item_row.pack(fill="x", pady=2)
                icon_frame = tk.Frame(item_row, bg=FIELD, width=36, height=36)
                icon_frame.pack(side="left", padx=(0, 7))
                icon_frame.pack_propagate(False)
                icon = tk.Label(icon_frame, bg=FIELD, bd=0)
                icon.pack(fill="both", expand=True)
                name = self.label(item_row, size=9, color=TEXT, wraplength=190, anchor="w", justify="left")
                name.pack(side="left", fill="x", expand=True)
                items.append({"frame": item_row, "icon": icon, "name": name, "icon_url": None})
            meta = self.label(frame, size=8, color=MUTED, wraplength=374, anchor="w", justify="left")
            meta.pack(fill="x", pady=(5, 0))
            self.build_cards.append({"frame": frame, "items": items, "value": value, "meta": meta})
        self.build_note = tk.StringVar(value="")
        self.build_note_label = self.label(self.build_panel, size=8, color=MUTED,
                                          textvariable=self.build_note, wraplength=398, anchor="w", justify="left")
        self.build_note_label.pack(fill="x", side="bottom", pady=(7, 0))

    def switch_view(self, view, *, automatic=False):
        self.view_automatic = automatic
        self.view.set(view)
        self.augment_panel.pack_forget()
        self.spell_panel.pack_forget()
        self.build_panel.pack_forget()
        {"augments": self.augment_panel, "spells": self.spell_panel,
         "builds": self.build_panel}[view].pack(fill="both", expand=True)
        self.augment_tab.configure(bg=FIELD if view == "augments" else BG)
        self.spells_tab.configure(bg=FIELD if view == "spells" else BG)
        self.builds_tab.configure(bg=FIELD if view == "builds" else BG)
        if view == "spells":
            self.lookup_spells()
            self.render_spells()
        elif view == "builds":
            self.lookup_builds()
            self.render_builds()
        else:
            self.lookup()
        self.root.after_idle(self.fit_window)

    def lookup_builds(self, force=False):
        if not force and self.build_champion == self.champion:
            return
        self.build_token += 1
        self.build_champion = self.champion
        self.build_data = None
        self.build_order_key = None
        self.build_context_panel.pack_forget()
        self.build_group.set("")
        self.build_group_box["values"] = []
        for card in self.build_cards:
            card["frame"].pack_forget()
        self.build_note.set("")
        self.build_summary.set("Loading core builds…" if self.champion else "Choose a champion to see core builds.")
        if self.view.get() == "builds":
            self.source.set("ARAMKit · core-build data")
            self.root.after_idle(self.fit_window)
        if self.champion:
            self.requests.put(("builds", (self.build_token, self.champion)))

    def completed_items(self):
        if (not self.game_live or self.champion != self.inventory_champion
                or self.live_items is None or self.build_data is None):
            return None
        catalog = self.build_data.get("item_catalog", {})
        return [catalog[item_id].get("canonical_id", item_id) for item_id in self.live_items
                if item_id in catalog and catalog[item_id].get("is_final")
                and not catalog[item_id].get("is_boots")]

    def render_build_item(self, item, name, url):
        item["name"].configure(text=name)
        item["icon_url"] = url
        photo = self.item_images.get(url)
        item["icon"].configure(image=photo if photo is not None else "")
        if url and photo is None:
            self.icons.request(url)

    def render_builds(self):
        self.build_context_panel.pack_forget()
        for card in self.build_cards:
            card["frame"].pack_forget()
            for item in card["items"]:
                item["icon_url"] = None
        for item in self.build_core_items:
            item["icon_url"] = None
        if self.view.get() == "builds":
            self.status.set("Watching for augment choices…" if self.follow and self.game_live and self.auto.get()
                            else "Browsing builds for the selected champion")
        if self.build_data is None:
            if self.view.get() == "builds":
                self.source.set("ARAMKit · core-build data")
            return
        groups = [group for group in self.build_data["groups"] if group["rows"]]
        completed = self.completed_items()
        later = completed is not None and len(completed) >= 3
        if later:
            # Inventory slots do not reveal purchase order. Match an owned
            # core set, then disclose/select the source's actual order below.
            owned = Counter(completed)
            matches = []
            for group in groups:
                for index, profile in enumerate(group["rows"], 1):
                    catalog = self.build_data.get("item_catalog", {})
                    if Counter(catalog.get(item_id, {}).get("canonical_id", item_id)
                               for item_id in profile["item_ids"]) <= owned:
                        matches.append({**group, "label": f"{group['label']} · core {index}",
                                        "profile": profile})
            groups = sorted(matches, key=lambda group: group["profile"]["sample_count"], reverse=True)
        self.build_group_box["values"] = [group["label"] for group in groups]
        if not groups:
            self.build_group.set("")
            self.build_summary.set("No follow-up data matches your completed items." if later
                                   else "No core-build data for this champion.")
            self.build_note.set(f"{len(completed)} completed items detected · no matching core in this sample."
                                if later else "")
        else:
            group = next((group for group in groups if group["label"] == self.build_group.get()), groups[0])
            self.build_group.set(group["label"])
            rows = group["rows"]
            if later:
                profile = group["profile"]
                routes = sorted(profile.get("routes", []), key=lambda route: route["sample_count"], reverse=True)
                labels = [("Most played" if i == 0 else f"Order {i + 1}")
                          + f" · {compact_count(route['sample_count'])}" for i, route in enumerate(routes)]
                order_key = (self.champion, tuple(profile["item_ids"]))
                if self.build_order_key != order_key or self.build_order.get() not in labels:
                    self.build_order.set(labels[0] if labels else "No order data")
                self.build_order_key = order_key
                self.build_order_box["values"] = labels
                route = routes[labels.index(self.build_order.get())] if labels else None
                core = route or profile
                for index, item in enumerate(self.build_core_items):
                    urls = core.get("item_icon_urls", [])
                    self.render_build_item(item, f"{index + 1}. {core['item_names'][index]}",
                                           urls[index] if index < len(urls) else None)
                self.build_context_panel.pack(fill="x", pady=(0, 10), before=self.build_note_label)
                catalog = self.build_data.get("item_catalog", {})
                owned_ids = {catalog.get(item_id, {}).get("canonical_id", item_id) for item_id in self.live_items}
                rows = [row for row in route.get("later_items", [])
                        if catalog.get(row["item_ids"][0], {}).get("canonical_id", row["item_ids"][0])
                        not in owned_ids] if route else []
            else:
                self.build_order_key = None
            sort_key = {"Win rate": "win_rate", "Games": "sample_count", "Pick rate": "pick_rate"}[self.build_sort.get()]
            rows = sorted(rows, key=lambda row: (
                row.get(sort_key) if row.get(sort_key) is not None else -1, row["sample_count"]), reverse=True)
            if later:
                self.build_summary.set(f"Follow-up items · {len(completed)} completed · "
                                       f"{min(len(rows), len(self.build_cards))} of {len(rows)} options"
                                       if rows else "No unowned follow-up items in this order's sample.")
            else:
                self.build_summary.set(f"Core builds · {min(len(rows), len(self.build_cards))} of {len(rows)} sets")
            for card, row in zip(self.build_cards, rows):
                count, rate, pick = row["sample_count"], row["win_rate"], row.get("pick_rate")
                low = count < 200
                urls = row.get("item_icon_urls", [])
                for index, item in enumerate(card["items"]):
                    if index >= len(row["item_names"]):
                        item["frame"].pack_forget()
                        continue
                    item["frame"].pack(fill="x", pady=2)
                    url = urls[index] if index < len(urls) else None
                    self.render_build_item(item, row["item_names"][index], url)
                card["value"].configure(text=f"{rate * 100:.1f}%" if rate is not None and count else "—")
                parts = [f"{compact_count(count)} games"]
                if pick is not None:
                    parts.append(f"{pick * 100:.1f}% of this order" if later
                                 else f"{pick * 100:.1f}% of {group['label']} builds")
                if low:
                    parts.append("LOW SAMPLE")
                card["meta"].configure(text=" · ".join(parts), fg=AMBER if low else MUTED)
                card["frame"].pack(fill="x", pady=(0, 7), before=self.build_note_label)
            self.build_note.set(("Per-item rates after the core order shown · filtered sample."
                                 if later else "Core item sets · filtered sample · not purchase order.")
                                + (" Cached data." if self.build_data.get("stale") else ""))
        if later and not self.inventory_current:
            self.build_note.set(self.build_note.get() + " Inventory unavailable; using last known items.")
        if self.view.get() == "builds":
            self.update_source(self.build_data)
            self.root.after_idle(self.fit_window)

    def panel_placement(self, height, position=None):
        try:
            displays = list_displays()
        except OSError:
            displays = []
        # A momentary monitor-query failure must not send a known portrait
        # placement back to the primary screen. A real unplug still returns
        # the remaining monitors and uses the normal fallback below.
        if not displays and self._last_display is not None:
            displays = [self._last_display]
        if not displays:
            bounds = (0, 0, self.root.winfo_screenwidth(), self.root.winfo_screenheight())
            displays = [{"device": "fallback", "bounds": bounds, "work_area": bounds, "primary": True}]
        display = select_display(displays, preferred_device=self.settings.get("display_device"),
                                 prefer_portrait=self.settings.get("display_preference") == "portrait",
                                 position=position)
        self._last_display = display
        return place_panel(display, 440, height, position)

    def fit_window(self):
        self.root.update_idletasks()
        x, y, height = self.panel_placement(self.shell.winfo_reqheight(),
                                           (self.root.winfo_x(), self.root.winfo_y()))
        self.root.geometry(f"440x{height}+{x}+{y}")

    def cancel_placement_check(self):
        if self._placement_job is not None:
            self.root.after_cancel(self._placement_job)
            self._placement_job = None

    def schedule_placement_check(self, delay=150):
        self.cancel_placement_check()
        if not self._closed and not self.hidden:
            self._placement_job = self.root.after(delay, self.check_placement)

    def check_placement(self):
        self._placement_job = None
        if self._closed or self.hidden:
            return
        try:
            if self.drag_start is not None:
                return
            actual_x, actual_y = self.root.winfo_x(), self.root.winfo_y()
            x, y, height = self.panel_placement(self.shell.winfo_reqheight(), (actual_x, actual_y))
            actual = (actual_x, actual_y, self.root.winfo_width(), self.root.winfo_height())
            if any(abs(current - wanted) > 2 for current, wanted in zip(actual, (x, y, 440, height))):
                self.root.geometry(f"440x{height}+{x}+{y}")
        finally:
            # Windows may relocate a window during mapping, DPI changes or a
            # monitor reconnect. Correct only an actual mismatch; the tolerance
            # avoids repeated geometry updates from DPI rounding.
            self.schedule_placement_check(1500)

    def lookup_spells(self, force=False):
        if not force and self.spell_champion == self.champion:
            return
        self.spell_token += 1
        self.spell_champion = self.champion
        self.spell_data = None
        self.spell_tree.delete(*self.spell_tree.get_children())
        self.spell_note.set("")
        self.spell_summary.set("Loading spell pairs…" if self.champion else "Choose a champion to see spell-pair win rates.")
        if self.champion:
            self.requests.put(("spells", (self.spell_token, self.champion)))

    def render_spells(self):
        self.spell_tree.delete(*self.spell_tree.get_children())
        self.update_spell_status()
        if self.spell_data is None:
            self.spell_tree.configure(height=1)
            self.root.after_idle(self.fit_window)
            return
        rows = list(self.spell_data["rows"])
        self.spell_tree.configure(height=max(1, min(len(rows), 8)))
        self.root.after_idle(self.fit_window)
        if not rows:
            self.spell_summary.set("No spell-pair data for this champion.")
            self.spell_note.set("")
            self.update_source(self.spell_data)
            return
        sort_key = {"Win rate": "win_rate", "Games": "sample_count", "Pick rate": "pick_rate"}[self.spell_sort.get()]
        rows.sort(key=lambda row: (row.get(sort_key) if row.get(sort_key) is not None else -1,
                                   row["sample_count"]), reverse=True)
        live_ids = [spell.get("id") for spell in self.live_spells]
        current = tuple(sorted(str(ident) for ident in live_ids)) if len(live_ids) == 2 and all(live_ids) else ()
        highlight = (self.game_live or self.champ_select) and self.champion == self.live_champion and len(set(current)) == 2
        matched = False
        for index, row in enumerate(rows):
            is_current = highlight and tuple(sorted(row["spell_ids"])) == current
            matched = matched or is_current
            pair = " + ".join(row["spell_names"])
            count, rate, pick = row["sample_count"], row["win_rate"], row.get("pick_rate")
            low = count < 200
            tags = ("current",) if is_current else (("low_sample",) if low else ())
            self.spell_tree.insert("", "end", iid=str(index), values=(
                ("● " if is_current else "") + pair,
                f"{rate * 100:.1f}" if rate is not None and count else "—",
                compact_count(count) + ("*" if low else ""),
                f"{pick * 100:.1f}" if pick is not None else "—"), tags=tags)
        if highlight:
            self.spell_summary.set("● Your current pair" if matched else "Your current pair is not in this sample.")
        else:
            self.spell_summary.set(f"{len(rows)} pairs · champion-specific win rates")
        notes = []
        if any(row["sample_count"] < 200 for row in rows):
            notes.append("* Fewer than 200 games")
        if self.spell_data.get("stale"):
            notes.append("Cached data")
        self.spell_note.set(" · ".join(notes))
        self.update_source(self.spell_data)

    def update_spell_status(self):
        if not self.champion:
            self.status.set("Waiting for your champion selection." if self.champ_select
                            else "Choose a champion or enter champion select.")
        elif (self.game_live or self.champ_select) and self.champion == self.live_champion:
            names = [spell.get("name") for spell in self.live_spells if spell.get("name")]
            phase = "Champion preview" if self.champ_select and self.champion_preview else "Champion select" if self.champ_select else "Live"
            self.status.set(phase + " · " + " + ".join(names) if names else phase)
        else:
            self.status.set("Browsing spell pairs for the selected champion")

    def update_source(self, metadata):
        marker = " · cached/offline" if metadata.get("stale") else ""
        self.source.set(f"ARAMKit · {metadata.get('version', '')} · {metadata.get('data_date', '')}{marker}")

    def clear_offers(self):
        self.offer_ids = [None, None, None]
        self.is_auto_offer = False
        for card in self.cards:
            card["var"].set("")
        self.lookup()

    def change_scan(self):
        self.requests.put(("enabled", self.auto.get()))
        self.status.set("Focus League to scan augment cards." if self.auto.get() else "Auto scan paused. Manual search is available.")

    def rescan(self):
        self.auto.set(True)
        self.requests.put(("enabled", True))
        self.requests.put(("scan", None))
        self.status.set("Scanning when League is in the foreground…")

    def toggle_visible(self):
        self.set_manual_visible(self.hidden)

    def set_manual_visible(self, visible):
        if not visible and 101 not in self.hotkeys.registered and not self.tray.available:
            self.status.set("Cannot hide: the tray and show shortcut are unavailable.")
            return
        self.visibility_override = visible
        self.set_visible(visible)

    def show_from_tray(self):
        # The tray must also recover a click-through panel without a shortcut.
        self.unlock_panel()
        if not self.hidden:
            self.fit_window()
        self.set_manual_visible(True)

    def unlock_panel(self):
        self.locked = False
        self.lock_button.configure(text="Lock")
        set_window_style(self.root, False)

    def check_recovery(self):
        if self.locked and not self.tray.available and 103 not in self.hotkeys.registered:
            self.unlock_panel()
        was_recovering = self.recovery_visible
        self.recovery_visible = not self.tray.available and 101 not in self.hotkeys.registered
        if self.recovery_visible:
            self.show_from_tray()
            self.status.set("Tray and shortcuts unavailable. Restart the companion; this panel will stay open.")
        elif was_recovering:
            self.visibility_override = None
            self.set_visible(self.auto_visible or self.browse_mode)

    def toggle_autostart(self):
        if self._autostart_busy:
            return
        from .autostart import is_enabled
        enable = not is_enabled()
        script = self.storage / "Configure-Autostart.vbs"
        self._autostart_busy = True

        def configure():
            command = [str(Path(os.environ.get("WINDIR", "C:/Windows")) / "System32" / "cscript.exe"),
                       "//B", "//NoLogo", str(script), str(self.storage)]
            if not enable:
                command.append("disable")
            try:
                result = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                        timeout=15, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                success = result.returncode == 0 and is_enabled() == enable
            except (OSError, subprocess.SubprocessError):
                success = False
            self.events.put(("autostart_result", (success, enable)))

        threading.Thread(target=configure, name="mayhem-startup-setting", daemon=True).start()

    def set_visible(self, visible):
        if visible == (not self.hidden):
            if visible and self._placement_job is None:
                self.schedule_placement_check()
            return
        self.hidden = not visible
        if not visible:
            self.cancel_placement_check()
            self.root.withdraw()
        else:
            self.fit_window()
            self.root.deiconify()
            self.root.attributes("-topmost", True)
            set_window_style(self.root, self.locked)
            self.schedule_placement_check()

    def update_visibility(self, game):
        phase = game.get("phase")
        active_phases = ("ChampSelect", "GameStart", "InProgress", "Reconnect")
        in_match = (game.get("status") in ("champ_select", "live", "tft")
                    or phase in active_phases)
        visible = (game.get("is_aram") is True or game.get("is_tft") is True) and in_match
        confirmed = (type(game.get("is_aram")) is bool
                     or type(game.get("is_tft")) is bool
                     or (phase is not None and phase not in active_phases))
        if confirmed and visible != self.auto_visible:
            # A manual hide lasts through this match; a manual show outside a
            # match lasts until the next one starts. Unknown/error polls are
            # not match boundaries and must not reset a manual choice.
            self.visibility_override = None
        if confirmed:
            self.auto_visible = visible
        else:
            # A known active phase may briefly lose queue metadata. With no
            # phase at all, do not leave the panel on the desktop indefinitely.
            visible = self.auto_visible and phase in active_phases
        if self.recovery_visible:
            self.set_visible(True)
        elif self.browse_mode:
            self.set_visible(True if self.visibility_override is None else self.visibility_override)
        else:
            self.set_visible(visible if self.visibility_override is None else self.visibility_override)

    def toggle_lock(self):
        if not self.locked and 103 not in self.hotkeys.registered and not self.tray.available:
            self.status.set("Cannot lock: the tray and unlock shortcut are unavailable.")
            return
        self.locked = not self.locked
        self.lock_button.configure(text="Locked" if self.locked else "Lock")
        self.status.set("Click-through · Ctrl+Alt+L to unlock" if self.locked else "Unlocked · drag MAYHEM to move")
        set_window_style(self.root, self.locked)

    def drain(self):
        try:
            while not self._closed:
                kind, payload = self.events.get_nowait()
                self.handle_event(kind, payload)
        except queue.Empty:
            pass
        except Exception:
            logging.exception("Panel update failed")
            self.status.set("An update failed. Retry data or restart the companion.")
        finally:
            if not self._closed:
                self.root.after(100, self.drain)

    def handle_event(self, kind, payload):
        if kind == "item_icon":
            url, image = payload
            # Tk objects are created only on this UI thread. The image loader
            # is independent of OCR and cannot delay augment recognition.
            try:
                photo = ImageTk.PhotoImage(image.resize((36, 36), Image.Resampling.LANCZOS), master=self.root)
            except Exception:
                logging.debug("Item icon could not be displayed", exc_info=True)
                return
            self.item_images[url] = photo
            for card in self.build_cards:
                for item in card["items"]:
                    if item["icon_url"] == url:
                        item["icon"].configure(image=photo)
            for item in self.build_core_items:
                if item["icon_url"] == url:
                    item["icon"].configure(image=photo)
            self.tft_panel.on_icon(url, photo)
            self.choice_panel.on_icon(url, photo)
        elif kind == "tray_action":
            if payload == "show":
                self.show_from_tray()
            elif payload == "hide":
                self.set_manual_visible(False)
            elif payload == "exit":
                self.close()
            elif payload == "toggle_autostart":
                self.toggle_autostart()
        elif kind == "autostart_result":
            self._autostart_busy = False
            success, enabled = payload
            self.status.set(("Start with League enabled." if enabled else "Start with League disabled.")
                            if success else "Could not change automatic launch. Check the installed startup script.")
        elif kind in ("tray_status", "hotkeys_failed"):
            self.check_recovery()
        elif kind == "catalog":
            refreshing = self.catalog is not None
            self.catalog = payload
            self.champion_names = sorted(x["name"] for x in payload["champions"].values())
            self.augment_names = sorted(x["name"] for x in payload["augments"].values())
            self.augment_by_name = {v["name"].casefold(): str(k) for k, v in payload["augments"].items()}
            self.champ_box["values"] = self.champion_names
            for card in self.cards:
                card["box"]["values"] = self.augment_names
            marker = " · cached/offline" if payload.get("stale") else ""
            self.source.set(f"ARAMKit · {payload.get('version', '')} · {payload.get('data_date', '')}{marker}")
            self.status.set("Waiting for champion select. You can also browse a champion.")
            # Selection can arrive before the public catalog finishes.
            previous_spell_token, previous_build_token = self.spell_token, self.build_token
            if self.last_game_context:
                self.handle_game(self.last_game_context)
            if refreshing or self.spell_token == previous_spell_token:
                self.lookup_spells(force=True)
            if refreshing or self.build_token == previous_build_token:
                self.lookup_builds(force=True)
            self.update_champion_choices(self.last_game_context, force=refreshing)
        elif kind == "game":
            self.handle_game(payload)
        elif kind == "champions":
            token, data = payload
            if self.choice_active and token == self.choice_token:
                self.choice_panel.set_data(data)
        elif kind == "champions_error":
            token, error = payload
            if self.choice_active and token == self.choice_token:
                self.choice_panel.set_error(str(error)[:180])
        elif kind == "tft":
            token, data = payload
            if token == self.tft_token:
                self.tft_pending = False
                self.tft_loaded = True
                self.tft_panel.set_data(data)
        elif kind == "tft_error":
            token, error = payload
            if token == self.tft_token:
                self.tft_pending = False
                self.tft_panel.set_error(str(error)[:180])
        elif kind == "tft_items":
            token, unit, data = payload
            if token == self.tft_item_token and unit == self.tft_unit:
                self.tft_panel.set_items(unit, data)
        elif kind == "tft_items_error":
            token, unit, error = payload
            if token == self.tft_item_token and unit == self.tft_unit:
                self.tft_panel.set_items_error(unit, str(error)[:180])
        elif kind == "offers":
            if self.display_mode == "tft":
                return
            previously_active = self.offers_active
            if not payload:
                self.offers_active = False
                if self.is_auto_offer:
                    self.clear_offers()
                    self.status.set("Waiting for augment choices…")
                if (previously_active and self.follow and self.game_live
                        and self.view.get() == "augments" and self.view_automatic):
                    self.switch_view("builds", automatic=True)
            elif self.auto.get():
                self.offers_active = True
                self.offer_ids = [str(x["id"]) for x in payload[:3]]
                self.offer_ids += [None] * (3 - len(self.offer_ids))
                for ident, card in zip(self.offer_ids, self.cards):
                    card["var"].set(self.catalog["augments"][ident]["name"] if ident else "")
                self.is_auto_offer = True
                self.status.set(f"{len(payload)} titles recognized · shown left to right")
                if not previously_active and self.follow and self.game_live:
                    self.switch_view("augments", automatic=True)
                else:
                    self.lookup()
        elif kind == "rates":
            token, rates = payload
            if token == self.token:
                self.render_rates(rates)
        elif kind == "spells":
            token, data = payload
            if token == self.spell_token and str(data.get("champion_id")) == self.champion:
                self.spell_data = data
                if self.view.get() == "spells":
                    self.render_spells()
        elif kind == "spells_error":
            token, error = payload
            if token == self.spell_token:
                self.spell_data = None
                self.spell_summary.set("Spell data unavailable. Use Retry data.")
                self.spell_note.set(str(error)[:80])
        elif kind == "builds":
            token, data = payload
            if token == self.build_token and str(data.get("champion_id")) == self.champion:
                self.build_data = data
                if self.view.get() == "builds":
                    self.render_builds()
        elif kind == "builds_error":
            token, error = payload
            if token == self.build_token:
                self.build_data = None
                for card in self.build_cards:
                    card["frame"].pack_forget()
                self.build_summary.set("Build data unavailable. Use Retry data.")
                self.build_note.set(str(error)[:120])
                if self.view.get() == "builds":
                    self.root.after_idle(self.fit_window)
        elif kind == "lookup_error":
            token, error = payload
            if token == self.token:
                for card in self.cards:
                    card["value"].configure(text="—", fg=TEXT)
                    card["meta"].configure(text="Data unavailable", fg=AMBER)
                self.status.set(error[:180])
        elif kind == "scan_status":
            if self.view.get() in ("augments", "builds") and self.auto.get():
                message = payload or "Watching for augment choices…"
                if self.view.get() == "builds" and (message.startswith("No augment choices")
                                                       or "titles recognized" in message):
                    message = "Watching for augment choices…"
                if self.locked:
                    message += " · Ctrl+Alt+L to unlock"
                self.status.set(message)
        elif kind == "error":
            self.status.set(str(payload)[:180])

    def handle_game(self, game):
        self.last_game_context = game
        self.update_visibility(game)
        if self.mode_choice.get() == "Auto":
            if game.get("is_tft") is True:
                self.set_game_mode("tft")
            elif game.get("is_aram") is True or game.get("is_tft") is False:
                self.set_game_mode("aram")
        self.update_champion_choices(game)
        if self.display_mode == "tft":
            if game.get("status") == "tft" and self.game_status != "tft":
                self.load_tft()
            self.game_status = "tft" if game.get("status") == "tft" else "waiting"
            self.game_live = self.champ_select = self.champion_preview = False
            self.last_active_status = None
            self.live_champion = None
            self.live_items = self.inventory_champion = None
            self.inventory_current = False
            self.offers_active = False
            self.badge.configure(text="LIVE" if game.get("status") == "tft" else "BROWSE",
                                 fg=ACCENT if game.get("status") == "tft" else MUTED)
            return
        previous_status = self.game_status
        self.game_status = game.get("status", "waiting")
        self.game_live = game.get("status") == "live"
        self.champ_select = game.get("status") == "champ_select"
        self.champion_preview = bool(game.get("champion_preview"))
        active = self.game_live or self.champ_select
        previous_active_status = self.last_active_status
        if active:
            self.last_active_status = self.game_status
        elif game.get("phase") is not None and game["phase"] not in ("GameStart", "InProgress", "Reconnect"):
            self.last_active_status = None
        previous_spells = self.live_spells
        self.live_spells = game.get("summoner_spells", []) if active else []
        previous_items = self.live_items
        previous_inventory_current = self.inventory_current
        self.inventory_current = self.game_live and game.get("item_ids") is not None
        ident = None
        if active and self.catalog:
            candidate = str(game.get("champion_id"))
            ident = candidate if candidate in self.catalog["champions"] else resolve_champion_id(
                game.get("raw_champion") or game.get("champion_name") or "", self.catalog["champions"])
            ident = str(ident) if ident is not None else None
        changed = ident != self.live_champion
        phase_changed = previous_status != self.game_status
        self.live_champion = ident
        if self.game_live and ident:
            if ident != self.inventory_champion:
                self.live_items = None
            self.inventory_champion = ident
            if game.get("item_ids") is not None:
                self.live_items = list(game["item_ids"])
        elif self.champ_select or self.last_active_status is None:
            self.inventory_champion = None
            self.live_items = None
        if self.follow:
            if changed or phase_changed or self.champion != ident:
                self.champion = ident
                self.offers_active = False
                self.clear_offers()
                self.champ_var.set(self.catalog["champions"][ident]["name"] if ident else "Auto detect champion")
                # Levels do not reliably identify pending picks or extra-augment mechanics.
                self.stage.set("All picks")
            self.badge.configure(text="SELECT" if self.champ_select else "LIVE" if ident else "WAITING",
                                 fg=ACCENT if active else MUTED)
            if active and self.game_status != previous_active_status:
                if self.champ_select:
                    self.switch_view("spells", automatic=True)
                elif self.game_live:
                    self.switch_view("builds", automatic=True)
            if not active and not self.locked:
                self.status.set("Waiting for champion select. Choose a champion to browse now.")
        if (changed or phase_changed or previous_spells != self.live_spells) and self.view.get() == "spells":
            self.render_spells()
        elif self.view.get() == "spells":
            self.update_spell_status()
        if (changed or phase_changed or previous_items != self.live_items
                or previous_inventory_current != self.inventory_current) and self.view.get() == "builds":
            self.render_builds()

    def render_rates(self, rates):
        metadata = next((rate for rate in rates if rate), None)
        if metadata and self.view.get() == "augments":
            self.update_source(metadata)
        for rate, card in zip(rates, self.cards):
            if not rate:
                continue
            value, count = rate.get("win_rate"), rate.get("sample_count", 0)
            if value is None or not count:
                card["value"].configure(text="—", fg=MUTED)
                card["meta"].configure(text="No champion-specific data\nfor this pick", fg=MUTED)
            else:
                card["value"].configure(text=f"{value * 100:.1f}%", fg=TEXT)
                qualifier = "LOW SAMPLE" if count < 200 else "champion + augment"
                if rate.get("stale"):
                    qualifier += " · cached"
                card["meta"].configure(text=f"{compact_count(count)} games\n{qualifier}",
                                       fg=AMBER if count < 200 or rate.get("stale") else MUTED)

    def open_source(self):
        url = "https://aramkit.com/en-US/augments"
        if self.champion and self.catalog:
            slug = self.catalog["champions"].get(self.champion, {}).get("slug", "")
            if re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug):
                url = "https://aramkit.com/en-US/champions/" + slug
        webbrowser.open(url)

    def help(self):
        dialog = tk.Toplevel(self.root)
        dialog.title("About Mayhem Companion")
        dialog.configure(bg=BG)
        dialog.attributes("-topmost", True)
        content = (
            "Ctrl+Alt+M   Show / hide\nCtrl+Alt+L   Toggle click-through\nCtrl+Alt+S   Resume scanning\n\n"
            "The panel appears for ARAM and Mayhem champion select and matches, then hides after the match. "
            "Ctrl+Alt+M can open it manually outside a match.\n\n"
            "The header's Auto / ARAM / TFT selector follows the game or opens either reference manually. "
            "TFT appears when a TFT game starts and uses separate TFT data. Comps and items show "
            "average placement, top-four and first-place rates when published. Augment help is an "
            "expert tier/reference lookup, not augment win rates. The displayed queue and rank identify "
            "the sample. This version does not read your TFT board or automatically choose a comp.\n\n"
            "The M icon in the Windows tray also opens the panel. Right-click it for Show, Hide, or Exit. "
            "Showing from the tray unlocks click-through controls.\n\n"
            "Start with League in the tray menu enables a small Windows sign-in watcher. "
            "It launches the companion once when League opens. Exit stays effective until you restart League.\n\n"
            "Use borderless or windowed League. Drag MAYHEM to move the panel outside the augment cards. "
            "Keep League focused for automatic reading. English augment titles are supported.\n\n"
            "Manual lookup: type a champion or augment, choose from the dropdown, then press Enter. "
            "Auto follows your champion in champion select and in game. Pick filters are manual; All picks is the default.\n\n"
            "Spells shows champion-specific spell-pair rates, game counts and pick rates. "
            "Spells opens during champion select, with your current pair highlighted. "
            "These are pair statistics, not individual-spell rates.\n\n"
            "Builds opens during play between augment choices. It shows the most-played core item sets "
            "for your champion, with win rates, pick rates and game counts. Switch build type or sorting above the sets. "
            "The filtered core item sets are not a purchase order. "
            "After three completed non-boot items, Builds matches your inventory to a core set and shows "
            "follow-up item rates, excluding items you own. These are individual later-item rates, not a "
            "six-item combination's win rate. The core purchase order defaults to the most-played order; "
            "choose another order above the icons if it matches what you bought. Boots and components "
            "do not count toward the three-item trigger. "
            "Detected choices open Augments, then Builds returns after they disappear. "
            "Manual tab changes stay until the next selection or game phase.\n\n"
            "Rates come from ARAMKit's third-party sample, not all servers. Low samples are marked; "
            "missing champion data stays blank. Screenshots stay in memory on this computer.\n\n"
            "Riot's developer policy prohibits augment win-rate displays in apps and overlays. "
            "This personal prototype is not Riot-approved.\n\n"
            "Mayhem Companion is not endorsed by Riot Games and does not reflect the views or opinions "
            "of Riot Games or anyone officially involved in producing or managing Riot Games properties. "
            "Riot Games and all associated properties are trademarks or registered trademarks of Riot Games, Inc."
        )
        self.label(dialog, content, 10, MUTED, wraplength=420, justify="left").pack(padx=20, pady=18)
        self.button(dialog, "Riot policy", lambda: webbrowser.open(POLICY_URL)).pack(side="left", padx=20)
        self.button(dialog, "Close", dialog.destroy).pack(side="right", padx=20, pady=10)

    def close(self):
        if self._closed:
            return
        self._closed = True
        self.cancel_placement_check()
        self.stop.set()
        if hasattr(self, "tray"):
            self.tray.close()
        if hasattr(self, "hotkeys"):
            self.hotkeys.close()
        try:
            self.settings["position"] = [self.root.winfo_x(), self.root.winfo_y()]
            self.settings_path.write_text(json.dumps(self.settings), encoding="utf-8")
        except OSError:
            pass
        self.root.destroy()


def main():
    parser = argparse.ArgumentParser(description="Local ARAM Mayhem companion")
    parser.add_argument("--browse", action="store_true", help="Start in manual browse mode")
    parser.add_argument("--auto-start", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    # A second launcher must not create another overlay with unavailable recovery
    # shortcuts. Keep a named mutex alive for this process lifetime.
    instance_mutex = None
    if hasattr(ctypes, "WinDLL"):
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        instance_mutex = kernel32.CreateMutexW(None, False, "Local\\MayhemCompanion.Personal")
        if ctypes.get_last_error() == 183:
            if not args.auto_start:
                ctypes.windll.user32.MessageBoxW(None, "Mayhem Companion is already running.\nUse its M tray icon or Ctrl+Alt+M to open it.", "Mayhem Companion", 0x40)
            return
    storage = app_dir()
    storage.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=storage / "companion.log", level=logging.WARNING,
                        format="%(asctime)s %(levelname)s %(message)s")
    if hasattr(ctypes, "windll"):
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass
    root = tk.Tk()
    app = Companion(root)
    if args.browse:
        app.browse_mode = True
        app.set_visible(True)
        app.follow = False
        app.auto.set(False)
        app.change_scan()
        app.badge.configure(text="BROWSE", fg=AMBER)
    try:
        root.mainloop()
    finally:
        app.close()
        if instance_mutex:
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel32.CloseHandle(instance_mutex)


if __name__ == "__main__":
    main()
