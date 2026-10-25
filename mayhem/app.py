"""Compact Windows companion. Network and OCR run off the UI thread."""
from __future__ import annotations

import argparse
import ctypes
import json
import logging
import os
from pathlib import Path
import queue
import re
import threading
import tkinter as tk
from tkinter import ttk
import webbrowser

from .windows import Hotkeys, set_window_style
from .worker import Worker
from .data import resolve_champion_id

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
        self.storage = app_dir()
        self.storage.mkdir(parents=True, exist_ok=True)
        self.settings_path = self.storage / "settings.json"
        try:
            self.settings = json.loads(self.settings_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.settings = {}
        self.events, self.requests = queue.Queue(), queue.Queue()
        self.stop = threading.Event()
        self.catalog = None
        self.champion = self.live_champion = None
        self.follow = True
        self.hidden = self.locked = self.game_live = False
        self.token = 0
        self.is_auto_offer = False
        self.offer_ids = [None, None, None]
        self.cards = []
        self.augment_names, self.champion_names = [], []
        self.augment_by_name = {}
        self.drag_start = None
        self.spell_token = 0
        self.spell_champion = None
        self.spell_data = None
        self.live_spells = []
        self.build()
        self.hotkeys = Hotkeys(root, {101: (ord("M"), self.toggle_visible),
                                     102: (ord("S"), self.rescan),
                                     103: (ord("L"), self.toggle_lock)})
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
        x, y = max(0, root.winfo_screenwidth() - 470), 80
        saved = self.settings.get("position")
        if isinstance(saved, list) and len(saved) == 2:
            try:
                x = max(0, min(int(saved[0]), root.winfo_screenwidth() - 440))
                y = max(0, min(int(saved[1]), root.winfo_screenheight() - 200))
            except (ValueError, TypeError):
                pass
        root.geometry(f"440x610+{x}+{y}")
        self.shell = tk.Frame(root, bg=BG, highlightbackground="#35465f", highlightthickness=1)
        self.shell.pack(fill="both", expand=True)
        header = tk.Frame(self.shell, bg=BG)
        header.pack(fill="x", padx=16, pady=(12, 8))
        brand = self.label(header, "MAYHEM", 10, ACCENT, True)
        brand.pack(side="left")
        self.badge = self.label(header, "WAITING", 8, MUTED)
        self.badge.pack(side="left", padx=10)
        self.button(header, "×", self.close, BG).pack(side="right")
        self.button(header, "—", self.toggle_visible, BG).pack(side="right")
        self.button(header, "?", self.help, BG).pack(side="right")
        for widget in [header, brand, self.badge]:
            widget.bind("<ButtonPress-1>", self.start_drag)
            widget.bind("<B1-Motion>", self.drag)
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
        root.update_idletasks()
        root.geometry(f"440x{self.shell.winfo_reqheight()}+{x}+{y}")

    def start_drag(self, event):
        self.drag_start = (event.x_root - self.root.winfo_x(), event.y_root - self.root.winfo_y())

    def drag(self, event):
        if self.drag_start:
            self.root.geometry(f"+{event.x_root-self.drag_start[0]}+{event.y_root-self.drag_start[1]}")

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
        self.champion = self.live_champion
        self.clear_offers()
        self.requests.put(("scan", None))
        self.champ_var.set(self.catalog["champions"][self.champion]["name"]
                           if self.champion and self.catalog else "Auto detect champion")
        self.badge.configure(text="LIVE" if self.game_live else "WAITING", fg=ACCENT if self.game_live else MUTED)
        self.lookup()

    def select_augment(self, index):
        value = self.cards[index]["var"].get().strip().casefold()
        ident = self.augment_by_name.get(value)
        self.offer_ids[index] = ident
        self.is_auto_offer = False
        if ident:
            self.cards[index]["var"].set(self.catalog["augments"][ident]["name"])
        self.lookup()

    def lookup(self):
        self.lookup_spells()
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
        toolbar = tk.Frame(self.spell_panel, bg=BG)
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
                                      show="headings", height=7, style="Spells.Treeview", selectmode="none")
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

    def switch_view(self, view):
        self.view.set(view)
        self.augment_panel.pack_forget()
        self.spell_panel.pack_forget()
        (self.spell_panel if view == "spells" else self.augment_panel).pack(fill="both", expand=True)
        self.augment_tab.configure(bg=FIELD if view == "augments" else BG)
        self.spells_tab.configure(bg=FIELD if view == "spells" else BG)
        if view == "spells":
            self.lookup_spells()
            self.render_spells()
        else:
            self.lookup()
        self.root.after_idle(self.fit_window)

    def fit_window(self):
        self.root.update_idletasks()
        height = min(self.shell.winfo_reqheight(), self.root.winfo_screenheight() - 60)
        y = max(0, min(self.root.winfo_y(), self.root.winfo_screenheight() - height - 40))
        self.root.geometry(f"440x{height}+{self.root.winfo_x()}+{y}")

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
        highlight = self.game_live and self.champion == self.live_champion and len(set(current)) == 2
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
            self.status.set("Choose a champion or wait for a game.")
        elif self.game_live and self.champion == self.live_champion:
            names = [spell.get("name") for spell in self.live_spells if spell.get("name")]
            self.status.set("Live · " + " + ".join(names) if names else "Live champion detected")
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
        if not self.hidden and 101 not in self.hotkeys.registered:
            self.status.set("Cannot hide: Ctrl+Alt+M is in use by another app.")
            return
        self.hidden = not self.hidden
        if self.hidden:
            self.root.withdraw()
        else:
            self.root.deiconify()
            self.root.attributes("-topmost", True)
            set_window_style(self.root, self.locked)

    def toggle_lock(self):
        if not self.locked and 103 not in self.hotkeys.registered:
            self.status.set("Cannot lock: Ctrl+Alt+L is unavailable.")
            return
        self.locked = not self.locked
        self.lock_button.configure(text="Locked" if self.locked else "Lock")
        self.status.set("Click-through · Ctrl+Alt+L to unlock" if self.locked else "Unlocked · drag MAYHEM to move")
        set_window_style(self.root, self.locked)

    def drain(self):
        try:
            while True:
                kind, payload = self.events.get_nowait()
                self.handle_event(kind, payload)
        except queue.Empty:
            pass
        except Exception:
            logging.exception("Panel update failed")
            self.status.set("An update failed. Retry data or restart the companion.")
        finally:
            self.root.after(100, self.drain)

    def handle_event(self, kind, payload):
        if kind == "catalog":
            self.catalog = payload
            self.champion_names = sorted(x["name"] for x in payload["champions"].values())
            self.augment_names = sorted(x["name"] for x in payload["augments"].values())
            self.augment_by_name = {v["name"].casefold(): str(k) for k, v in payload["augments"].items()}
            self.champ_box["values"] = self.champion_names
            for card in self.cards:
                card["box"]["values"] = self.augment_names
            marker = " · cached/offline" if payload.get("stale") else ""
            self.source.set(f"ARAMKit · {payload.get('version', '')} · {payload.get('data_date', '')}{marker}")
            self.status.set("Waiting for a game. You can also browse a champion and augments.")
            self.lookup_spells(force=True)
        elif kind == "game":
            self.handle_game(payload)
        elif kind == "offers":
            if not payload:
                if self.is_auto_offer:
                    self.clear_offers()
                    self.status.set("Waiting for augment choices…")
            elif self.follow:
                self.offer_ids = [str(x["id"]) for x in payload[:3]]
                self.offer_ids += [None] * (3 - len(self.offer_ids))
                for ident, card in zip(self.offer_ids, self.cards):
                    card["var"].set(self.catalog["augments"][ident]["name"] if ident else "")
                self.is_auto_offer = True
                self.status.set(f"{len(payload)} titles recognized · shown left to right")
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
        elif kind == "lookup_error":
            token, error = payload
            if token == self.token:
                for card in self.cards:
                    card["value"].configure(text="—", fg=TEXT)
                    card["meta"].configure(text="Data unavailable", fg=AMBER)
                self.status.set(error[:180])
        elif kind == "scan_status":
            if self.view.get() == "augments" and self.auto.get() and not self.is_auto_offer and not self.locked:
                self.status.set(payload or "Watching for augment choices…")
        elif kind == "error":
            self.status.set(str(payload)[:180])

    def handle_game(self, game):
        self.game_live = game.get("status") == "live"
        previous_spells = self.live_spells
        self.live_spells = game.get("summoner_spells", []) if self.game_live else []
        ident = None
        if self.game_live and self.catalog:
            ident = resolve_champion_id(game.get("raw_champion") or game.get("champion_name") or "",
                                        self.catalog["champions"])
            ident = str(ident) if ident is not None else None
        changed = ident != self.live_champion
        self.live_champion = ident
        if self.follow:
            if changed or self.champion != ident:
                self.champion = ident
                self.clear_offers()
                self.champ_var.set(self.catalog["champions"][ident]["name"] if ident else "Auto detect champion")
                # Levels do not reliably identify pending picks or extra-augment mechanics.
                self.stage.set("All picks")
            self.badge.configure(text="LIVE" if ident else "WAITING", fg=ACCENT if ident else MUTED)
            if not self.game_live and not self.locked:
                self.status.set("Waiting for a game. Choose a champion to browse now.")
        if (changed or previous_spells != self.live_spells) and self.view.get() == "spells":
            self.render_spells()
        elif self.view.get() == "spells":
            self.update_spell_status()

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
            "Use borderless or windowed League. Drag MAYHEM to move the panel outside the augment cards. "
            "Keep League focused for automatic reading. English augment titles are supported.\n\n"
            "Manual lookup: type a champion or augment, choose from the dropdown, then press Enter. "
            "Auto returns to your live champion. Pick filters are manual; All picks is the default.\n\n"
            "Spells shows champion-specific spell-pair rates, game counts and pick rates. "
            "Your current pair is highlighted when detected. These are pair statistics, not individual-spell rates.\n\n"
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
        self.stop.set()
        if hasattr(self, "hotkeys"):
            self.hotkeys.close()
        try:
            self.settings_path.write_text(json.dumps({"position": [self.root.winfo_x(), self.root.winfo_y()]}), encoding="utf-8")
        except OSError:
            pass
        self.root.destroy()


def main():
    parser = argparse.ArgumentParser(description="Local ARAM Mayhem companion")
    parser.add_argument("--browse", action="store_true", help="Start in manual browse mode")
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
            ctypes.windll.user32.MessageBoxW(None, "Mayhem Companion is already running.\nPress Ctrl+Alt+M to show or hide it.", "Mayhem Companion", 0x40)
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
        app.follow = False
        app.auto.set(False)
        app.change_scan()
        app.badge.configure(text="BROWSE", fg=AMBER)
    try:
        root.mainloop()
    finally:
        if instance_mutex:
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel32.CloseHandle(instance_mutex)


if __name__ == "__main__":
    main()
