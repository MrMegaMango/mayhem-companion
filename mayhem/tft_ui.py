"""Compact, manually browsed TFT reference panel; all data is source supplied."""
from __future__ import annotations

import math
import tkinter as tk
from tkinter import ttk
import webbrowser

BG, PANEL, FIELD = "#10151e", "#192231", "#222e40"
TEXT, MUTED, ACCENT, AMBER = "#edf4ff", "#9aacc4", "#7ce1bd", "#efc47c"
VIEWS = (("comps", "Comps"), ("items", "Item builds"), ("augments", "Augments"))
SORT_KEYS = {"Average place": "avg_place", "Top 4": "top4_rate",
             "1st place": "first_rate", "Games": "games"}


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) else None


def compact_count(value):
    value = _number(value)
    if value is None or value < 0:
        return "—"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    if value >= 10_000:
        return f"{value / 1000:.1f}k"
    return f"{int(value):,}"


def metric_values(row):
    """The provider normalizes percentages to fractions; never guess the scale."""
    result = {"avg_place": "—", "top4_rate": "—", "first_rate": "—",
              "games": compact_count(row.get("games"))}
    if _number(row.get("games")) == 0:
        return result
    average = _number(row.get("avg_place"))
    if average is not None and 1 <= average <= 8:
        result["avg_place"] = f"{average:.2f}"
    for key in ("top4_rate", "first_rate"):
        rate = _number(row.get(key))
        if rate is not None and 0 <= rate <= 1:
            result[key] = f"{rate * 100:.1f}%"
    return result


def row_summary(row):
    values = metric_values(row)
    labels = (("avg_place", "avg"), ("top4_rate", "top 4"),
              ("first_rate", "1st"), ("games", "games"))
    return " · ".join(f"{values[key]} {label}" for key, label in labels if values[key] != "—")


def _search_text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return " ".join(_search_text(value.get(key, "")) for key in
                        ("name", "traits", "units", "unit", "carry", "items", "category"))
    if isinstance(value, (list, tuple)):
        return " ".join(_search_text(part) for part in value)
    return ""


def filtered_rows(rows, query="", sort="Source order"):
    terms = query.casefold().split()
    result = [row for row in rows if isinstance(row, dict)
              and all(term in _search_text(row).casefold() for term in terms)]
    key = SORT_KEYS.get(sort)
    if key:
        def sort_key(row):
            value = _number(row.get(key))
            if key != "games" and _number(row.get("games")) == 0:
                value = None
            return value is None, (value if key == "avg_place" else -value) if value is not None else 0
        result.sort(key=sort_key)
    return result


def selection_choices(rows):
    """Source IDs disambiguate equal display names without silently guessing."""
    counts = {}
    for row in rows:
        name = str(row.get("name", ""))
        counts[name.casefold()] = counts.get(name.casefold(), 0) + 1
    return [(str(row.get("name", "")) + (f" ({row['id']})" if counts[str(row.get("name", "")).casefold()] > 1 else ""), row)
            for row in rows]


def resolve_choice(rows, label):
    matches = [row for choice, row in selection_choices(rows) if choice.casefold() == label.strip().casefold()]
    return matches[0] if len(matches) == 1 else None


def source_metadata(data, view):
    """Exact per-view metadata overrides shared catalog metadata."""
    meta = {**data, **data.get("view_meta", {}).get(view, {})}
    pieces = [str(meta.get("source") or "TFT data")]
    patch = meta.get("patch") or meta.get("version")
    date = meta.get("updated") or meta.get("data_date")
    if patch:
        pieces.append(f"Patch {patch}")
    elif meta.get("set"):
        pieces.append(str(meta["set"]))
    if meta.get("queue") or meta.get("mode"):
        pieces.append(str(meta.get("queue") or meta["mode"]))
    if meta.get("rank"):
        pieces.append(str(meta["rank"]))
    if date:
        pieces.append(str(date).replace("T", " ").replace("+00:00", " UTC"))
    if meta.get("scope") and meta["scope"] not in ("comp_archetype", "unit_three_item_build"):
        pieces.append(str(meta["scope"]))
    if meta.get("stale"):
        pieces.append("CACHED")
    return " · ".join(pieces), meta.get("source_url")


class TftPanel(tk.Frame):
    """Main-thread widget; callbacks queue work and never fetch on this thread.

    request_callback() refreshes the dataset. request_icon(url) returns an
    already cached PhotoImage or None while enqueueing an optional download.
    """

    def __init__(self, parent, request_callback, on_resize=None, request_icon=None,
                 request_items=None):
        super().__init__(parent, bg=BG)
        self.request_callback = request_callback
        self.on_resize = on_resize or (lambda: None)
        self.request_icon = request_icon or (lambda _url: None)
        self.request_items = request_items or (lambda _unit, force=False: None)
        self.data, self.source_url, self.selected_id = None, None, None
        self.item_data, self.item_unit_id = None, None
        self.item_loading = False
        self.rows, self.visible_rows, self.icon_widgets = [], [], []
        self.photos, self.tree_icons = {}, {}
        self._compare = False
        self.view = tk.StringVar(value="comps")
        self.query = tk.StringVar()
        self.unit_choice = tk.StringVar()
        self.sort = tk.StringVar(value="Games")
        self.message = tk.StringVar(value="Loading TFT data…")
        self.source = tk.StringVar(value="TFT · loading source")
        self._build()

    @staticmethod
    def label(parent, text="", size=10, color=TEXT, bold=False, **kwargs):
        return tk.Label(parent, text=text, bg=parent.cget("bg"), fg=color,
                        font=("Segoe UI", size, "bold" if bold else "normal"), **kwargs)

    @staticmethod
    def button(parent, text, command, color=FIELD):
        return tk.Button(parent, text=text, command=command, bg=color, fg=TEXT,
                         activebackground="#35475f", activeforeground=TEXT, bd=0,
                         padx=9, pady=5, font=("Segoe UI", 9), cursor="hand2")

    def _build(self):
        tabs = tk.Frame(self, bg=BG)
        tabs.pack(fill="x", pady=(0, 8))
        self.tabs = {}
        for key, title in VIEWS:
            button = self.button(tabs, title, lambda v=key: self.switch_view(v),
                                 FIELD if key == "comps" else BG)
            button.pack(side="left", fill="x", expand=True)
            self.tabs[key] = button
        self.unit_frame = tk.Frame(self, bg=BG)
        self.label(self.unit_frame, "Unit", 9, MUTED).pack(side="left", padx=(0, 8))
        self.unit_box = ttk.Combobox(self.unit_frame, textvariable=self.unit_choice,
                                     font=("Segoe UI", 11))
        self.unit_box.pack(side="left", fill="x", expand=True)
        self.unit_box.bind("<<ComboboxSelected>>", self._select_unit)
        self.unit_box.bind("<Return>", self._select_unit)
        self.unit_box.bind("<KeyRelease>", self._unit_edited)
        search_row = self.search_row = tk.Frame(self, bg=BG)
        search_row.pack(fill="x", pady=(0, 6))
        self.search = tk.Entry(search_row, textvariable=self.query, bg=FIELD, fg=TEXT,
                               insertbackground=TEXT, relief="flat", font=("Segoe UI", 11))
        self.search.pack(side="left", fill="x", expand=True, ipady=6)
        self.search.bind("<KeyRelease>", self._search_changed)
        self.search.bind("<Escape>", lambda _: self._clear_search())
        self.button(search_row, "×", self._clear_search).pack(side="left", padx=(4, 0))
        self.compare_button = self.button(search_row, "Compare 3", self.toggle_compare)
        options = tk.Frame(self, bg=BG)
        options.pack(fill="x", pady=(0, 6))
        self.result_count = self.label(options, "Loading…", 9, MUTED, anchor="w")
        self.result_count.pack(side="left", fill="x", expand=True)
        self.sort_box = ttk.Combobox(options, textvariable=self.sort, state="readonly",
                                     values=["Source order"], width=14, font=("Segoe UI", 9))
        self.sort_box.pack(side="right")
        self.sort_box.bind("<<ComboboxSelected>>", lambda _: self.render_list())
        style = ttk.Style(self)
        style.configure("Tft.Treeview", background=PANEL, fieldbackground=PANEL,
                        foreground=TEXT, rowheight=38, borderwidth=0, font=("Segoe UI", 10))
        style.map("Tft.Treeview", background=[("selected", "#31495d")],
                  foreground=[("selected", TEXT)])
        self.list_frame = tk.Frame(self, bg=BG)
        self.list_frame.pack(fill="x", pady=(0, 8))
        self.tree = ttk.Treeview(self.list_frame, show="tree", height=4,
                                 selectmode="browse", style="Tft.Treeview")
        self.tree.column("#0", width=350, stretch=True)
        self.tree.pack(side="left", fill="x", expand=True)
        self.tree_scroll = ttk.Scrollbar(self.list_frame, orient="vertical", command=self.tree.yview)
        self.tree_scroll.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=self._tree_scrolled)
        self.tree.bind("<Map>", lambda _: self.after_idle(self._visible_tree_icons))
        self.tree.bind("<<TreeviewSelect>>", self._selected)
        self.offer_frame = tk.Frame(self, bg=BG)
        self.offer_vars, self.offer_boxes = [], []
        for index in range(3):
            row = tk.Frame(self.offer_frame, bg=BG)
            row.pack(fill="x", pady=(0, 4))
            self.label(row, str(index + 1), 9, MUTED).pack(side="left", padx=(0, 6))
            variable = tk.StringVar()
            box = ttk.Combobox(row, textvariable=variable, font=("Segoe UI", 10))
            box.pack(side="left", fill="x", expand=True)
            box.bind("<<ComboboxSelected>>", lambda _, i=index: self._offer_selected(i))
            box.bind("<Return>", lambda _, i=index: self._offer_selected(i))
            box.bind("<KeyRelease>", lambda event, i=index: self._offer_edited(event, i))
            self.offer_vars.append(variable)
            self.offer_boxes.append(box)
        self.detail_outer = tk.Frame(self, bg=BG)
        self.detail_outer.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(self.detail_outer, bg=BG, height=360,
                                highlightthickness=0, bd=0)
        self.canvas.pack(side="left", fill="both", expand=True)
        detail_scroll = ttk.Scrollbar(self.detail_outer, orient="vertical", command=self.canvas.yview)
        detail_scroll.pack(side="right", fill="y")
        self.canvas.configure(yscrollcommand=detail_scroll.set)
        self.detail = tk.Frame(self.canvas, bg=BG)
        self.detail_window = self.canvas.create_window((0, 0), window=self.detail, anchor="nw")
        self.detail.bind("<Configure>", lambda _: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda event: self.canvas.itemconfigure(self.detail_window, width=event.width))
        self.canvas.bind("<MouseWheel>", self._wheel)
        status = tk.Frame(self, bg=BG)
        status.pack(fill="x", pady=(7, 4))
        self.label(status, textvariable=self.message, size=9, color=MUTED,
                   wraplength=295, justify="left", anchor="w").pack(side="left", fill="x", expand=True)
        self.button(status, "Refresh", self.refresh).pack(side="right")
        link = self.label(self, textvariable=self.source, size=8, color=MUTED,
                           anchor="w", justify="left", wraplength=390, cursor="hand2")
        link.pack(fill="x", pady=(0, 9))
        link.bind("<Button-1>", lambda _: self.open_source())
        self._empty_detail("Loading TFT compositions…")

    def _wheel(self, event):
        bounds = self.canvas.bbox("all")
        if bounds and bounds[3] > self.canvas.winfo_height():
            self.canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")
        return "break"

    def _tree_scrolled(self, first, last):
        self.tree_scroll.set(first, last)
        self._visible_tree_icons()

    def _visible_tree_icons(self):
        if not self.tree.winfo_exists() or not self.tree.winfo_ismapped() or not self.visible_rows:
            return
        first, last = self.tree.yview()
        start = int(first * len(self.visible_rows))
        end = min(len(self.visible_rows), math.ceil(last * len(self.visible_rows)), start + 8)
        for index in range(start, end):
            iid = str(index)
            url = self.tree_icons.get(iid)
            if url and self.tree.exists(iid) and self.tree.bbox(iid):
                photo = self._get_photo(url)
                if photo is not None:
                    self.tree.item(iid, image=photo)

    def set_loading(self):
        self.message.set("Refreshing TFT data…" if self.data else "Loading TFT data…")

    def refresh(self):
        self.set_loading()
        if self.view.get() == "items" and self.item_unit_id:
            self.item_data = None
            self.item_loading = True
            self._configure_view()
            self.request_items(self.item_unit_id, force=True)
        else:
            self.request_callback()

    def set_error(self, text):
        self.message.set(str(text))
        if self.data is None:
            self.result_count.configure(text="Data unavailable")
            self._empty_detail("TFT data could not be loaded. Use Refresh to try again.")

    def set_data(self, data):
        self.data = data
        self.item_data = None
        self.item_loading = False
        self.message.set("Cached data · refresh unavailable" if data.get("stale") else "")
        self._configure_view()

    def set_items(self, unit_id, data):
        if str(unit_id) != self.item_unit_id:
            return
        self.item_data = data
        self.item_loading = False
        if self.view.get() == "items":
            self.message.set("Cached builds · refresh unavailable" if data.get("stale") else "")
            self._configure_view()

    def set_items_error(self, unit_id, text):
        if str(unit_id) != self.item_unit_id:
            return
        self.item_loading = False
        if self.view.get() == "items":
            self.message.set(str(text))
            if self.item_data is None:
                self._empty_detail("Item builds could not be loaded. Use Refresh to try again.")

    def _select_unit(self, _event=None):
        units = (self.data or {}).get("units", [])
        unit = resolve_choice(units, self.unit_choice.get())
        if unit is None:
            self.message.set("Select an exact unit name.")
            return
        if str(unit["id"]) != self.item_unit_id:
            self.item_unit_id = str(unit["id"])
            self.item_data = None
            self.item_loading = False
            self.query.set("")
            self.selected_id = None
        self.unit_choice.set(next(label for label, candidate in selection_choices(units) if candidate["id"] == unit["id"]))
        self._configure_view()

    def _unit_edited(self, event):
        if event.keysym in ("Return", "Up", "Down", "Escape"):
            return
        query = self.unit_choice.get().casefold()
        names = [label for label, _ in selection_choices((self.data or {}).get("units", []))]
        self.unit_box.configure(values=[name for name in names if query in name.casefold()])
        # An edited unit must never keep another unit's rates on screen.
        self.item_unit_id = None
        self.item_data = None
        self.item_loading = False
        self.rows = []
        self.render_list()
        self._empty_detail("Select a unit to load its three-item builds.")

    def switch_view(self, view):
        if view not in dict(VIEWS):
            return
        self.view.set(view)
        self.query.set("")
        self.sort.set("1st place" if view == "items" else "Games" if view == "comps" else "Source order")
        self.selected_id = None
        self._compare = False
        self._configure_view()

    def _configure_view(self):
        view = self.view.get()
        for key, button in self.tabs.items():
            button.configure(bg=FIELD if key == view else BG)
        self.compare_button.pack_forget()
        if view == "augments":
            self.compare_button.pack(side="right", padx=(6, 0))
        self.unit_frame.pack_forget()
        queue_items = None
        active_data = self.data or {}
        if view == "items":
            self.unit_frame.pack(fill="x", pady=(0, 7), before=self.search_row)
            units = (self.data or {}).get("units", [])
            unit_choices = selection_choices(units)
            self.unit_box.configure(values=[label for label, _ in unit_choices])
            selected = next((unit for unit in units if str(unit.get("id")) == self.item_unit_id), None)
            if selected is None and units:
                selected = units[0]
                self.item_unit_id = str(selected["id"])
                self.item_data = None
                self.item_loading = False
            if selected:
                self.unit_choice.set(next(label for label, candidate in unit_choices if candidate["id"] == selected["id"]))
            active_data = self.item_data or self.data or {}
            self.rows = list((self.item_data or {}).get("items", []))
            if selected and self.item_data is None and not self.item_loading:
                self.item_loading = True
                queue_items = self.item_unit_id
                self.message.set(f"Loading {selected['name']} item builds…")
        else:
            self.rows = list((self.data or {}).get(view, []))
            self.message.set("Cached data · refresh unavailable" if (self.data or {}).get("stale") else "")
        sorts = ["Source order"] + [name for name, key in SORT_KEYS.items()
                                    if any(_number(row.get(key)) is not None for row in self.rows)]
        # An empty unit sample must not reset the user's item-sort preference.
        if view == "items" and self.sort.get() in SORT_KEYS and self.sort.get() not in sorts:
            sorts.append(self.sort.get())
        self.sort_box.configure(values=sorts)
        if self.sort.get() not in sorts and not (view == "items" and self.item_data is None):
            self.sort.set("Source order")
        names = [label for label, _ in selection_choices((self.data or {}).get("augments", []))]
        for box in self.offer_boxes:
            box.configure(values=names)
        metadata, self.source_url = source_metadata(active_data, view)
        self.source.set(metadata)
        self._layout_compare()
        self.render_list()
        if view == "items" and self.item_loading:
            self._empty_detail("Loading three-item builds for this unit…")
        self.after_idle(self.on_resize)
        if queue_items:
            self.request_items(queue_items)

    def _clear_search(self):
        self.query.set("")
        self.render_list()

    def _search_changed(self, event):
        if event.keysym not in ("Up", "Down", "Return", "Escape"):
            self.selected_id = None
            self.render_list()

    def render_list(self):
        self.tree.delete(*self.tree.get_children())
        self.tree_icons = {}
        self.visible_rows = filtered_rows(self.rows, self.query.get(), self.sort.get())
        self.result_count.configure(text=f"{len(self.visible_rows)} {dict(VIEWS)[self.view.get()].lower()}")
        selected = None
        for index, row in enumerate(self.visible_rows):
            iid, suffix = str(index), ""
            metrics = metric_values(row)
            if metrics["avg_place"] != "—":
                suffix = f"  ·  {metrics['avg_place']} avg"
            elif row.get("tier"):
                suffix = f"  ·  {row['tier']} tier"
            self.tree.insert("", "end", iid=iid, text=str(row.get("name", "Unnamed")) + suffix)
            url = row.get("icon_url") or row.get("unit", {}).get("icon_url")
            if url:
                self.tree_icons[iid] = url
                photo = self.photos.get(url)
                if photo is not None:
                    self.tree.item(iid, image=photo)
            if str(row.get("id", row.get("name"))) == self.selected_id:
                selected = iid
        if self.visible_rows:
            selected = selected or "0"
            self.tree.selection_set(selected)
            self.tree.see(selected)
            row = self.visible_rows[int(selected)]
            self.selected_id = str(row.get("id", row.get("name")))
        else:
            self.selected_id = None
        self.render_detail()
        self.after_idle(self._visible_tree_icons)

    def _selected(self, _event=None):
        selection = self.tree.selection()
        if not selection or int(selection[0]) >= len(self.visible_rows):
            return
        row = self.visible_rows[int(selection[0])]
        self.selected_id = str(row.get("id", row.get("name")))
        self.render_detail()

    def _clear_detail(self):
        self.icon_widgets = []
        for widget in self.detail.winfo_children():
            widget.destroy()
        self.canvas.yview_moveto(0)

    def _empty_detail(self, text):
        self._clear_detail()
        self.label(self.detail, text, 10, MUTED, wraplength=350,
                   justify="left", anchor="w").pack(fill="x", padx=10, pady=18)

    def render_detail(self):
        if self._compare and self.view.get() == "augments":
            self._render_offers()
            return
        selection = self.tree.selection()
        if not selection or int(selection[0]) >= len(self.visible_rows):
            self._empty_detail("No matching results." if self.query.get()
                               else "No data is available for this view.")
            return
        self._clear_detail()
        row = self.visible_rows[int(selection[0])]
        self._card(row)
        self._bind_wheel(self.detail)

    def _card(self, row):
        card = tk.Frame(self.detail, bg=PANEL, padx=10, pady=9)
        card.pack(fill="x", pady=(0, 7))
        heading = tk.Frame(card, bg=PANEL)
        heading.pack(fill="x")
        icon_url = row.get("icon_url") or row.get("unit", {}).get("icon_url")
        if icon_url:
            self._icon(heading, icon_url).pack(side="left", padx=(0, 8))
        title = str(row.get("name", "Unnamed"))
        if self.view.get() == "items":
            unit = row.get("unit") or (self.item_data or {}).get("unit") or {}
            title = f"{unit['name']} · Item build" if unit.get("name") else "Item build"
        self.label(heading, title, 12, TEXT, True,
                   wraplength=285, justify="left", anchor="w").pack(side="left", fill="x", expand=True)
        if row.get("tier"):
            self.label(heading, str(row["tier"]), 15, ACCENT, True).pack(side="right", padx=(6, 0))
        metrics = metric_values(row)
        if any(metrics[key] != "—" for key in ("avg_place", "top4_rate", "first_rate", "games")):
            metric_row = tk.Frame(card, bg=PANEL)
            metric_row.pack(fill="x", pady=(9, 3))
            for index, (key, title) in enumerate((("avg_place", "Avg place ↓"), ("top4_rate", "Top 4"),
                                                 ("first_rate", "1st place"), ("games", "Games"))):
                cell = tk.Frame(metric_row, bg=PANEL)
                cell.grid(row=0, column=index, sticky="nsew")
                metric_row.columnconfigure(index, weight=1)
                self.label(cell, metrics[key], 16, ACCENT if key == "avg_place" else TEXT, True).pack()
                self.label(cell, title, 8, MUTED).pack()
        if self.view.get() == "comps" and row.get("units"):
            self._tiles(card, self._resolve(row["units"], "units"), columns=5)
        elif self.view.get() == "items" and row.get("items"):
            self._tiles(card, self._resolve(row["items"], "item_catalog"), columns=3)
        scope_labels = {"unit_build": "Item set on this unit · not purchase order",
                        "unit_three_item_build": "Three-item set · all star levels · not purchase order",
                        "comp": "Composition sample", "comp_archetype": "Archetype sample · one common board shown",
                        "expert_tier": "Expert tier · placement rates unavailable",
                        "reference": "No expert tier available · reference only"}
        note = row.get("stats_note") or scope_labels.get(row.get("scope"), "")
        if not note and self.view.get() == "augments" and row.get("tier"):
            note = "Expert tier · placement rates unavailable"
        count = _number(row.get("games"))
        low_sample = count is not None and 0 < count < 200
        if low_sample:
            note = (note + " · " if note else "") + "LOW SAMPLE"
        if note:
            self.label(card, str(note), 8, AMBER if low_sample else MUTED, wraplength=350,
                       anchor="w", justify="left").pack(fill="x", pady=(4, 0))
        traits = row.get("traits", [])
        if traits:
            names = [str(value.get("name", "")) if isinstance(value, dict) else str(value) for value in traits]
            self.label(card, " · ".join(names), 9, ACCENT, wraplength=350,
                       anchor="w", justify="left").pack(fill="x", pady=(7, 0))
        if row.get("description") and row.get("scope") not in ("comp_archetype", "unit_three_item_build"):
            self.label(card, str(row["description"]), 9, MUTED, wraplength=350,
                       anchor="w", justify="left").pack(fill="x", pady=(7, 0))
        return card

    def _resolve(self, entries, kind):
        raw = (self.data or {}).get(kind, [])
        catalog = raw if isinstance(raw, dict) else {str(item.get("id")): item for item in raw}
        result = []
        for item in entries:
            if isinstance(item, dict):
                result.append({**catalog.get(str(item.get("id")), {}), **item})
            elif str(item) in catalog:
                result.append(catalog[str(item)])
            else:
                result.append({"id": str(item), "name": str(item)})
        return result

    def _tiles(self, parent, items, columns=5):
        grid = tk.Frame(parent, bg=PANEL)
        grid.pack(fill="x", pady=(8, 3))
        for column in range(columns):
            grid.columnconfigure(column, weight=1, uniform="tiles")
        for index, item in enumerate(items):
            tile = tk.Frame(grid, bg=PANEL)
            tile.grid(row=index // columns, column=index % columns, sticky="nsew", pady=(0, 7))
            self._icon(tile, item.get("icon_url")).pack()
            self.label(tile, str(item.get("name", "")), 8, TEXT,
                       wraplength=100 if columns == 3 else 64,
                       justify="center").pack(fill="x", pady=(2, 0))
            if item.get("items"):
                names = [i.get("name", "") if isinstance(i, dict) else str(i) for i in item["items"]]
                self.label(tile, " · ".join(names), 7, MUTED, wraplength=64).pack(fill="x")

    def _get_photo(self, url):
        if not url:
            return None
        photo = self.photos.get(url)
        if photo is None:
            photo = self.request_icon(url)
            if photo is not None:
                self.photos[url] = photo
        return photo

    def _icon(self, parent, url):
        photo = self._get_photo(url)
        icon = self.label(parent, "", 10, MUTED)
        if photo is not None:
            icon.configure(image=photo)
        if url:
            self.icon_widgets.append((icon, url))
        return icon

    def on_icon(self, url, photo):
        self.photos[url] = photo
        for widget, wanted in self.icon_widgets:
            if wanted == url and widget.winfo_exists():
                widget.configure(image=photo)
        for iid, wanted in self.tree_icons.items():
            if wanted == url and self.tree.exists(iid):
                self.tree.item(iid, image=photo)

    def toggle_compare(self):
        self._compare = not self._compare
        self._layout_compare()
        self.render_detail()

    def _layout_compare(self):
        active = self._compare and self.view.get() == "augments"
        self.compare_button.configure(text="Browse" if active else "Compare 3")
        self.list_frame.pack_forget()
        self.offer_frame.pack_forget()
        if active:
            self.offer_frame.pack(fill="x", pady=(0, 8), before=self.detail_outer)
        else:
            self.list_frame.pack(fill="x", pady=(0, 8), before=self.detail_outer)
        self.after_idle(self.on_resize)

    def set_offers(self, offers):
        """Populate explicit user choices; no automatic screen or board reading."""
        self.switch_view("augments")
        catalog = (self.data or {}).get("augments", [])
        choices = selection_choices(catalog)
        for index, variable in enumerate(self.offer_vars):
            offer = offers[index] if index < len(offers) else ""
            ident = str(offer.get("id", "")) if isinstance(offer, dict) else str(offer)
            matched = next((row for row in catalog if str(row.get("id")) == ident), None)
            matched = matched or resolve_choice(catalog, ident)
            variable.set(next(label for label, row in choices if row["id"] == matched["id"]) if matched else "")
        self._compare = True
        self._layout_compare()
        self._render_offers()

    def _offer_selected(self, _index):
        self._render_offers()

    def _offer_edited(self, event, index):
        if event.keysym in ("Return", "Up", "Down", "Escape"):
            return
        query = self.offer_vars[index].get().casefold()
        names = [label for label, _ in selection_choices((self.data or {}).get("augments", []))]
        self.offer_boxes[index].configure(values=[name for name in names if query in name.casefold()])
        self._render_offers()

    def _render_offers(self):
        self._clear_detail()
        catalog = (self.data or {}).get("augments", [])
        count = 0
        for index, variable in enumerate(self.offer_vars):
            choice = variable.get().strip().casefold()
            row = resolve_choice(catalog, choice)
            if row:
                self._card(row)
                count += 1
            elif choice:
                self.label(self.detail, f"Choice {index + 1}: select an exact augment name.", 9, MUTED,
                           anchor="w", justify="left", wraplength=350).pack(fill="x", pady=8)
        if not count and not any(variable.get() for variable in self.offer_vars):
            self._empty_detail("Select up to three augment offers above.")
        self._bind_wheel(self.detail)

    def _bind_wheel(self, widget):
        widget.bind("<MouseWheel>", self._wheel)
        for child in widget.winfo_children():
            self._bind_wheel(child)

    def open_source(self):
        if isinstance(self.source_url, str) and self.source_url.startswith("https://"):
            webbrowser.open(self.source_url)


Panel = TftPanel
