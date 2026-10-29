"""Arena-only statistics and recognized offers; no network work on Tk's thread."""
from __future__ import annotations

import math
import tkinter as tk
from tkinter import messagebox, ttk
import webbrowser

from .data import resolve_champion_id

from .theme import BG, PANEL, FIELD, TEXT, MUTED, ACCENT, AMBER, BORDER


def number(value):
    return value if type(value) in (int, float) and math.isfinite(value) else None


def metric(row, key):
    if key == "sample_count":
        value = number(row.get(key, row.get("games")))
        return value if value is not None and value >= 0 and int(value) == value else None
    count = row.get("sample_count", row.get("games"))
    if count is not None and (metric(row, "sample_count") or 0) <= 0:
        return None
    value = number(row.get(key, row.get("avg_placement") if key == "avg_place" else None))
    if key.endswith("rate") and (value is None or not 0 <= value <= 1):
        return None
    if key == "avg_place" and (value is None or not 1 <= value <= 16):
        return None
    return value


def formatted(row, key):
    value = metric(row, key)
    if value is None or value < 0:
        return "—"
    if key.endswith("rate"):
        return f"{value * 100:.1f}%"
    if key == "avg_place":
        return f"{value:.2f}"
    return f"{int(value):,}"


def ordered_rows(rows, query="", sort="Win rate"):
    key = {"Win rate": "win_rate", "1st place": "first_rate", "Average place": "avg_place",
           "Games": "sample_count"}.get(sort, "win_rate")
    terms = query.casefold().split()
    result = [row for row in rows if all(term in str(row.get("name", "")).casefold() for term in terms)]
    result.sort(key=lambda row: (metric(row, key) is None,
                                (metric(row, key) or 0) * (1 if key == "avg_place" else -1),
                                -(metric(row, "sample_count") or 0)))
    return result


def champion_rows(catalog):
    """Attach observations by champion ID, leaving unobserved names unrated."""
    by_id = {str(row.get("champion_id", row.get("id"))): row
             for row in catalog.get("rows", [])}
    return [{**row, **by_id.get(str(identifier), {})}
            for identifier, row in catalog.get("champions", {}).items()]


class ArenaPanel(tk.Frame):
    def __init__(self, parent, request_catalog, request_champion, *, on_resize=None,
                 request_icon=None, scan_variable=None, change_scan=None, request_scan=None):
        super().__init__(parent, bg=BG)
        self.request_catalog, self.request_champion = request_catalog, request_champion
        self.on_resize = on_resize or (lambda: None)
        self.request_icon = request_icon or (lambda url: None)
        self.request_scan = request_scan or (lambda: None)
        self.catalog = self.data = None
        self.champion = self.live_champion = None
        self.follow = True
        self.last_game = {}
        self.offers = []
        self.automatic_view = True
        self.rows = []
        self.photos, self.icon_rows = {}, {}
        self.source_url = None
        self.champion_name = tk.StringVar(value="Auto detect champion")
        self.view = tk.StringVar(value="champions")
        self.query = tk.StringVar()
        self.sort = tk.StringVar(value="Win rate")
        self.message = tk.StringVar(value="Loading Arena data…")
        self.source = tk.StringVar(value="Arena")
        self.detail = tk.StringVar()
        self.variant = tk.StringVar()
        row = tk.Frame(self, bg=BG)
        row.pack(fill="x", pady=(0, 12))
        self.champion_box = ttk.Combobox(row, textvariable=self.champion_name, font=("Segoe UI", 16, "bold"), width=19)
        self.champion_box.pack(side="left", fill="x", expand=True)
        self.champion_box.bind("<<ComboboxSelected>>", self.select_champion)
        self.champion_box.bind("<Return>", self.select_champion)
        self.champion_box.bind("<KeyRelease>", self.filter_champions)
        self.button(row, "Auto", self.follow_game).pack(side="right", padx=(6, 0))
        tabs = tk.Frame(self, bg=PANEL, padx=3, pady=3)
        tabs.pack(fill="x", pady=(0, 12))
        self.tabs = {}
        for key, title in (("champions", "Champions"), ("augments", "Augments"), ("items", "Items")):
            self.tabs[key] = self.button(tabs, title, lambda key=key: self.switch_view(key))
            self.tabs[key].pack(side="left", expand=True, fill="x", padx=1)
        controls = tk.Frame(self, bg=BG)
        self.controls = controls
        controls.pack(fill="x", pady=(0, 8))
        self.search = ttk.Entry(controls, textvariable=self.query, width=16)
        self.search.pack(side="left", expand=True, fill="x")
        self.search.bind("<KeyRelease>", lambda _: self.render())
        sort = ttk.Combobox(controls, textvariable=self.sort, state="readonly", width=12,
                            values=["Win rate", "1st place", "Average place", "Games"])
        sort.pack(side="right", padx=(6, 0))
        sort.bind("<<ComboboxSelected>>", lambda _: self.render())
        self.offer_frame = tk.Frame(self, bg=BG)
        self.cards, self.offer_labels, self.offer_badges = [], [], []
        for index in range(3):
            frame = tk.Frame(self.offer_frame, bg=PANEL, padx=14, pady=12,
                             highlightbackground=BORDER, highlightthickness=1)
            frame.pack(fill="x", pady=(0, 10))
            top = tk.Frame(frame, bg=PANEL)
            top.pack(fill="x", pady=(0, 6))
            self.label(top, f"CHOICE {index + 1}", 8, color=MUTED, bold=True).pack(side="left")
            badge = self.label(top, "", 8, color=ACCENT, bold=True)
            badge.pack(side="right")
            name = self.label(frame, "", 13, bold=True, anchor="w", wraplength=365, justify="left")
            name.pack(fill="x")
            metric_row = tk.Frame(frame, bg=PANEL)
            metric_row.pack(fill="x", pady=(5, 0))
            value = self.label(metric_row, "", 30, bold=True, anchor="w")
            value.pack(side="left")
            caption = self.label(metric_row, "", 9, color=MUTED)
            caption.pack(side="left", anchor="s", padx=(9, 0), pady=(0, 7))
            note = self.label(frame, "", 9, color=MUTED, anchor="w", wraplength=365, justify="left")
            note.pack(fill="x", pady=(3, 0))
            self.cards.append((frame, name, value, note))
            self.offer_labels.append(caption)
            self.offer_badges.append(badge)
        self.table_frame = tk.Frame(self, bg=PANEL, highlightbackground=BORDER, highlightthickness=1)
        style = ttk.Style(self)
        style.configure("Arena.Treeview", rowheight=46, background=PANEL, fieldbackground=PANEL,
                        foreground=TEXT, borderwidth=0, font=("Segoe UI", 10))
        style.configure("Arena.Treeview.Heading", background=PANEL, foreground=MUTED,
                        relief="flat", padding=(6, 8), font=("Segoe UI", 8, "bold"))
        style.map("Arena.Treeview", background=[("selected", FIELD)], foreground=[("selected", TEXT)])
        style.configure("Arena.TEntry", fieldbackground=FIELD, foreground=TEXT, insertcolor=TEXT)
        self.search.configure(style="Arena.TEntry")
        self.tree = ttk.Treeview(self.table_frame, style="Arena.Treeview", columns=("win", "first", "games"),
                                 show="tree headings", height=7, selectmode="browse")
        self.tree.heading("#0", text="Champion")
        self.tree.column("#0", width=168, minwidth=105, stretch=True)
        for key, title, width in (("win", "Win rate", 78), ("first", "1ST", 50), ("games", "GAMES", 82)):
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, minwidth=width, stretch=False, anchor="e")
        self.tree.tag_configure("highest", background="#16342f", font=("Segoe UI", 10, "bold"))
        self.tree.tag_configure("low_sample", foreground=AMBER)
        self.tree.tag_configure("current", foreground=ACCENT)
        self.tree.pack(side="left", fill="both", expand=True)
        self.scrollbar = ttk.Scrollbar(self.table_frame, orient="vertical", command=self.tree.yview)
        self.scrollbar.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=self._tree_scrolled)
        self.tree.bind("<<TreeviewSelect>>", self.show_detail)
        self.tree.bind("<Double-1>", self.open_champion_row)
        self.detail_label = self.label(self, "", 9, color=MUTED, textvariable=self.detail,
                                       wraplength=395, anchor="w", justify="left")
        self.detail_label.pack(fill="x", pady=(6, 0))
        self.variant_label = self.label(self, size=9, color=MUTED, textvariable=self.variant, wraplength=395,
                                        anchor="w", justify="left")
        self.message_label = self.label(self, size=9, color=MUTED, textvariable=self.message, wraplength=395,
                                        anchor="w", justify="left")
        self.source_label = self.label(self, size=8, color=MUTED, textvariable=self.source, wraplength=395,
                                       anchor="w", justify="left", cursor="hand2")
        self.source_label.pack(fill="x", pady=(5, 0))
        self.source_label.bind("<Button-1>", lambda _: self.open_source())
        footer = tk.Frame(self, bg=BG)
        footer.pack(fill="x", pady=(8, 10))
        self.button(footer, "Retry data", self.retry).pack(side="left")
        self.button(footer, "About rates", self.about_rates).pack(side="right")
        if scan_variable is not None:
            tk.Checkbutton(footer, text="Auto scan", variable=scan_variable, command=change_scan,
                           bg=BG, fg=MUTED, activebackground=BG, activeforeground=TEXT,
                           selectcolor=FIELD, font=("Segoe UI", 9)).pack(side="left", padx=8)
        self.message.trace_add("write", self._sync_status_labels)
        self.variant.trace_add("write", self._sync_status_labels)
        self._sync_status_labels()
        self.render()

    @staticmethod
    def label(parent, text="", size=10, color=TEXT, bold=False, **kwargs):
        return tk.Label(parent, text=text, bg=parent.cget("bg"), fg=color,
                        font=("Segoe UI", size, "bold" if bold else "normal"), **kwargs)

    @staticmethod
    def button(parent, text, command):
        return tk.Button(parent, text=text, command=command, bg=FIELD, fg=TEXT, bd=0,
                         activebackground=BORDER, activeforeground=TEXT, padx=10, pady=8,
                         font=("Segoe UI", 10), cursor="hand2", highlightthickness=0)

    def set_loading(self):
        self.message.set("Loading Arena data…")

    def _sync_status_labels(self, *_args):
        for variable, widget in ((self.variant, self.variant_label), (self.message, self.message_label)):
            if variable.get():
                widget.pack(fill="x", pady=(5, 0), before=self.source_label)
            else:
                widget.pack_forget()
        self.on_resize()

    def set_catalog(self, catalog):
        self.catalog = catalog
        self.champion_box["values"] = sorted(row["name"] for row in catalog.get("champions", {}).values())
        self.message.set("")
        self.handle_game(self.last_game)
        self.render()

    def set_error(self, message):
        self.message.set(str(message)[:220])

    def set_champion_data(self, champion, data):
        if str(champion) != self.champion:
            return
        self.data = data
        self.message.set("")
        self.render()

    def set_champion_error(self, champion, error):
        if str(champion) == self.champion:
            self.data = None
            self.render()
            self.message.set(f"Arena data unavailable: {str(error)[:170]}")

    def choose_champion(self, champion, *, force=False):
        champion = str(champion) if champion is not None else None
        if champion == self.champion and not force:
            return
        changed = champion != self.champion
        self.champion = champion
        self.data = None
        if changed:
            self.offers = []
        entry = (self.catalog or {}).get("champions", {}).get(champion, {})
        self.champion_name.set(entry.get("name", "Auto detect champion"))
        self.request_champion(champion, force=force)
        self.message.set("Loading champion's Arena statistics…" if champion else "Choose a champion to see augments and items.")
        self.render()

    def select_champion(self, _event=None):
        champion = resolve_champion_id(self.champion_name.get().strip(), (self.catalog or {}).get("champions", {}))
        if champion is None:
            return
        self.follow = False
        self.choose_champion(champion)

    def filter_champions(self, event):
        if event.keysym in ("Return", "Up", "Down", "Escape"):
            return
        query = self.champion_name.get().casefold()
        self.champion_box["values"] = sorted(row["name"] for row in (self.catalog or {}).get("champions", {}).values()
                                            if query in row["name"].casefold())
        self.follow = False
        self.champion = None
        self.data, self.offers = None, []
        self.request_champion(None)
        self.message.set("Choose a champion to see augments and items.")
        self.render()

    def follow_game(self):
        self.follow = True
        self.automatic_view = True
        self.choose_champion(self.live_champion)
        self.handle_game(self.last_game)
        self.request_scan()

    def handle_game(self, game):
        previous_game = self.last_game
        previous_status = self.last_game.get("status")
        self.last_game = game
        active = game.get("is_arena") is True and game.get("status") in ("champ_select", "live")
        champions = (self.catalog or {}).get("champions", {})
        champion = str(game.get("champion_id")) if active else None
        if champion not in champions:
            champion = resolve_champion_id(game.get("raw_champion") or game.get("champion_name") or "", champions) if active else None
        self.live_champion = str(champion) if champion is not None else None
        variant = {1740: "Bravery Arena", 1750: "Arena 3×6", 1704: "Arena PBE 2v2"}.get(game.get("queue_id"))
        self.variant.set(f"Playing {variant} · statistics use the source sample below." if active and variant else "")
        if self.follow:
            self.choose_champion(self.live_champion)
            if active and previous_status != game.get("status"):
                self.switch_view("champions" if game.get("status") == "champ_select" else "items", automatic=True)
            if not active:
                changed = bool(self.offers) or previous_game != game
                self.offers = []
                if changed:
                    self.render()

    def set_offers(self, offers):
        if not self.follow or self.last_game.get("is_arena") is not True or self.last_game.get("status") != "live":
            return
        previous = bool(self.offers)
        self.offers = list(offers[:3])
        if self.offers and not previous:
            self.switch_view("augments", automatic=True)
        elif previous and not self.offers and self.automatic_view:
            self.switch_view("items", automatic=True)
        else:
            self.render()

    def switch_view(self, view, automatic=False):
        self.view.set(view)
        self.automatic_view = automatic
        self.query.set("")
        self.detail.set("")
        self.render()

    def retry(self):
        self.request_catalog(force=True)
        if self.champion:
            self.choose_champion(self.champion, force=True)

    def current_metadata(self):
        data = self.catalog if self.view.get() == "champions" else self.data
        return data or self.catalog or {}

    def metadata(self):
        data = self.current_metadata()
        label = str(data.get("win_rate_label") or "Win rate")
        bits = [str(data.get("source") or "Arena")]
        sample = str(data.get("sample_label") or "Arena")
        if sample not in bits:
            bits.append(sample)
        for key in ("version", "patch", "queue", "rank", "data_date"):
            if data.get(key) and str(data[key]) not in bits:
                bits.append(str(data[key]))
        if data.get("stale"):
            bits.append("CACHED")
        self.source.set(" · ".join(bits))
        self.source_url = data.get("source_url")
        self.tree.heading("win", text=label)
        return label

    def about_rates(self):
        data = self.current_metadata()
        details = [str(data.get("rate_note") or "Rates describe the Arena source's match sample.")]
        if data.get("sample_label"):
            details.append(str(data["sample_label"]))
        if (self.catalog or {}).get("catalog_note"):
            details.append(str(self.catalog["catalog_note"]))
        if data.get("fetched_at"):
            details.append("Fetched " + str(data["fetched_at"]))
        messagebox.showinfo("Arena statistics", "\n\n".join(details), parent=self.winfo_toplevel())

    def render(self):
        view = self.view.get()
        for key, button in self.tabs.items():
            button.configure(bg=FIELD if key == view else PANEL,
                             fg=ACCENT if key == view else MUTED,
                             font=("Segoe UI", 10, "bold" if key == view else "normal"))
        label = self.metadata()
        self.detail.set("")
        self.offer_frame.pack_forget()
        self.table_frame.pack_forget()
        if view == "augments" and self.offers:
            self.controls.pack_forget()
            self.offer_frame.pack(fill="x", before=self.detail_label)
            rates = {str(row["id"]): row for row in (self.data or {}).get("augments", [])}
            observed = [metric(rates.get(str(offer["id"]), {}), "win_rate") for offer in self.offers]
            observed = [rate for rate in observed if rate is not None]
            highest = max(observed) if len(observed) > 1 else None
            for index, (frame, title, value, note) in enumerate(self.cards):
                if index >= len(self.offers):
                    frame.pack_forget()
                    continue
                frame.pack(fill="x", pady=(0, 10))
                offer = self.offers[index]
                row = rates.get(str(offer["id"]), {})
                title.configure(text=offer["name"])
                count = metric(row, "sample_count")
                leading = (highest is not None and metric(row, "win_rate") == highest
                           and count is not None and count >= 200)
                value.configure(text=formatted(row, "win_rate"), fg=ACCENT if leading else TEXT)
                frame.configure(highlightbackground=ACCENT if leading else BORDER)
                self.offer_labels[index].configure(text=label)
                self.offer_badges[index].configure(text="HIGHEST WR" if leading else "")
                if count:
                    summary = f"{formatted(row, 'sample_count')} games"
                    if metric(row, "first_rate") is not None:
                        summary = f"{formatted(row, 'first_rate')} 1st · " + summary
                    if count < 200:
                        summary += " · LOW SAMPLE"
                else:
                    summary = "No champion-specific data" if self.data is not None else "Loading champion-specific data…"
                note.configure(text=summary, fg=AMBER if count and count < 200 else MUTED)
            self.detail.set("Champion + augment · picks/levels not separated")
        else:
            self.controls.pack(fill="x", pady=(0, 8), before=self.detail_label)
            self.table_frame.pack(fill="both", expand=True, before=self.detail_label)
            rows = champion_rows(self.catalog or {}) if view == "champions" else (self.data or {}).get(view, [])
            self.rows = ordered_rows(rows, self.query.get(), self.sort.get())
            show_first = any(metric(row, "first_rate") is not None for row in rows)
            self.tree.configure(displaycolumns=("win", "first", "games") if show_first else ("win", "games"))
            self.tree.column("#0", width=168 if show_first else 218)
            self.tree.delete(*self.tree.get_children())
            self.icon_rows = {}
            self.tree.heading("#0", text={"champions": "All champions", "items": "Item", "augments": "Augment"}[view])
            self.tree.configure(height=max(2, min(7, len(self.rows))))
            observed = [metric(row, "win_rate") for row in self.rows if metric(row, "win_rate") is not None]
            highest = max(observed) if len(observed) > 1 and self.sort.get() == "Win rate" else None
            for index, row in enumerate(self.rows):
                item = str(index)
                options = {}
                # Request only the initially visible rows; more are queued when scrolled.
                url = row.get("icon_url")
                if url and index < 9:
                    photo = self.photos.get(url) or self.request_icon(url)
                    if photo is not None:
                        self.photos[url] = photo
                        options["image"] = photo
                if url:
                    self.icon_rows.setdefault(url, []).append(item)
                count = metric(row, "sample_count")
                name = row.get("name", str(row.get("id", "")))
                tags = []
                if highest is not None and metric(row, "win_rate") == highest and count is not None and count >= 200:
                    tags.append("highest")
                if count and count < 200:
                    tags.append("low_sample")
                if view == "champions" and str(row.get("id")) == self.live_champion:
                    name = "● " + name
                    if not (count and count < 200):
                        tags.append("current")
                options["tags"] = tags
                self.tree.insert("", "end", iid=item, text=name, values=(formatted(row, "win_rate"),
                                 formatted(row, "first_rate"), formatted(row, "sample_count") + ("*" if count and count < 200 else "")), **options)
            if not self.rows:
                self.detail.set("No matching champions." if view == "champions" else
                                "No champion-specific rows available." if self.champion else "Choose a champion above.")
            elif view == "items":
                self.detail.set("Champion + individual item · * fewer than 200 games")
            elif view == "champions":
                self.detail.set("All source champions · double-click to browse · * fewer than 200 games")
            else:
                self.detail.set("Champion + augment · picks/levels not separated · * fewer than 200 games")
        self.on_resize()

    def show_detail(self, _event=None):
        selection = self.tree.selection()
        if not selection:
            return
        index = int(selection[0])
        if not 0 <= index < len(self.rows):
            return
        row = self.rows[index]
        text = f"{row.get('name', '')} · {formatted(row, 'avg_place')} average place"
        if row.get("description"):
            text += "\n" + str(row["description"])[:300]
        self.detail.set(text)
        self.on_resize()

    def open_champion_row(self, _event=None):
        selected = self.tree.selection()
        if self.view.get() == "champions" and selected:
            self.follow = False
            self.choose_champion(self.rows[int(selected[0])]["id"])
            self.switch_view("items")

    def on_icon(self, url, photo):
        self.photos[url] = photo
        for item in self.icon_rows.get(url, []):
            if self.tree.exists(item):
                self.tree.item(item, image=photo)

    def _tree_scrolled(self, first, last):
        self.scrollbar.set(first, last)
        self._visible_tree_icons()

    def _visible_tree_icons(self):
        if not self.tree.winfo_exists() or not self.tree.winfo_ismapped() or not self.rows:
            return
        first, last = self.tree.yview()
        start = int(first * len(self.rows))
        end = min(len(self.rows), math.ceil(last * len(self.rows)), start + 9)
        for index in range(start, end):
            item = str(index)
            url = self.rows[index].get("icon_url")
            if url and self.tree.exists(item) and self.tree.bbox(item):
                photo = self.photos.get(url) or self.request_icon(url)
                if photo is not None:
                    self.photos[url] = photo
                    self.tree.item(item, image=photo)

    def open_source(self):
        if isinstance(self.source_url, str) and self.source_url.startswith("https://"):
            webbrowser.open(self.source_url)
