"""Champion-specific augment rankings, without network work on Tk's thread."""
from __future__ import annotations

import math
import tkinter as tk
from tkinter import font as tkfont, ttk

from .theme import BG, PANEL, FIELD, TEXT, MUTED, AMBER, BORDER


MIN_GAMES = 200
RARITIES = {"Silver": "silver", "Gold": "gold", "Prismatic": "prismatic"}


def ranked_rows(rows, include_small=False, rarity="All rarities"):
    """Missing observations are never ranked; the sample filter is explicit."""
    result = []
    for row in rows:
        rate, count = row.get("win_rate"), row.get("sample_count")
        if (row.get("missing") or type(rate) not in (int, float) or not math.isfinite(rate)
                or not 0 <= rate <= 1 or type(count) is not int or count <= 0):
            continue
        if not include_small and count < MIN_GAMES:
            continue
        if rarity in RARITIES and row.get("rarity") != RARITIES[rarity]:
            continue
        result.append(row)
    return sorted(result, key=lambda row: (-row["win_rate"], -row["sample_count"],
                                          str(row.get("name", "")).casefold()))


def compact_count(value):
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    if value >= 10_000:
        return f"{value / 1000:.1f}k"
    return f"{value:,}"


class AugmentRankingsPanel(tk.Frame):
    """A reference list, distinct from the three augment offers in game."""

    def __init__(self, parent, request_icon=None, on_resize=None, on_retry=None):
        super().__init__(parent, bg=BG)
        self.request_icon = request_icon or (lambda _url: None)
        self.on_resize = on_resize or (lambda: None)
        self.on_retry = on_retry or (lambda: None)
        self.data = None
        self.rows = []
        self.photos, self.icon_rows = {}, {}
        self._closed = False
        self._idle_jobs = set()
        self._error = ""
        self._loading = False
        self.champion_name = ""
        self.include_small = tk.BooleanVar(self, False)
        self.rarity = tk.StringVar(self, "All rarities")
        self.status = tk.StringVar(self)
        self.note = tk.StringVar(self)
        self.name_font = tkfont.Font(self, family="Segoe UI", size=10)
        self._build()
        self.render()

    def _build(self):
        header = tk.Frame(self, bg=BG)
        header.pack(fill="x", pady=(2, 8))
        tk.Label(header, text="Top augments", bg=BG, fg=TEXT,
                 font=("Segoe UI", 11, "bold")).pack(side="left")
        tk.Label(header, textvariable=self.status, bg=BG, fg=MUTED,
                 font=("Segoe UI", 8)).pack(side="right")
        controls = tk.Frame(self, bg=BG)
        controls.pack(fill="x", pady=(0, 8))
        box = ttk.Combobox(controls, textvariable=self.rarity, state="readonly", width=13,
                           values=["All rarities", *RARITIES])
        box.pack(side="left")
        box.bind("<<ComboboxSelected>>", lambda _: self.render())
        tk.Checkbutton(controls, text="Include small samples", variable=self.include_small,
                       command=self.render, bg=BG, fg=MUTED, activebackground=BG,
                       activeforeground=TEXT, selectcolor=FIELD, font=("Segoe UI", 9),
                       highlightthickness=0, bd=0).pack(side="right")
        style = ttk.Style(self)
        style.configure("AugmentRanks.Treeview", background=PANEL, fieldbackground=PANEL,
                        foreground=TEXT, rowheight=56, borderwidth=0, font=self.name_font)
        style.configure("AugmentRanks.Treeview.Heading", background=PANEL, foreground=MUTED,
                        borderwidth=0, relief="flat", padding=(7, 8), font=("Segoe UI", 8, "bold"))
        style.map("AugmentRanks.Treeview", background=[("selected", FIELD)],
                  foreground=[("selected", TEXT)])
        table = tk.Frame(self, bg=PANEL, highlightbackground=BORDER, highlightthickness=1)
        table.pack(fill="x")
        self.tree = ttk.Treeview(table, columns=("win", "games"), show="tree headings",
                                selectmode="none", style="AugmentRanks.Treeview", height=1)
        self.tree.heading("#0", text="AUGMENT", anchor="w")
        self.tree.heading("win", text="WIN % ↓", anchor="e")
        self.tree.heading("games", text="GAMES", anchor="e")
        self.tree.column("#0", width=241, minwidth=205, stretch=True)
        self.tree.column("win", width=65, minwidth=60, stretch=False, anchor="e")
        self.tree.column("games", width=77, minwidth=68, stretch=False, anchor="e")
        self.tree.tag_configure("low_sample", foreground=AMBER)
        self.tree.pack(side="left", fill="x", expand=True)
        self.scrollbar = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        self.scrollbar.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=self._scrolled)
        self.tree.bind("<Map>", lambda _: self._schedule(self._visible_icons))
        self.note_label = tk.Label(self, textvariable=self.note, bg=BG, fg=MUTED,
                                   font=("Segoe UI", 9), justify="left", anchor="w", wraplength=398)
        self.note_label.pack(fill="x", pady=(7, 0))
        self.retry_button = tk.Button(self, text="Retry", command=self.on_retry, bg=FIELD,
                                      fg=TEXT, activebackground=BORDER, activeforeground=TEXT,
                                      font=("Segoe UI", 9), padx=10, pady=5, bd=0,
                                      highlightthickness=0, cursor="hand2")

    def _schedule(self, callback):
        if self._closed:
            return
        job = None
        def run():
            self._idle_jobs.discard(job)
            if not self._closed:
                callback()
        job = self.after_idle(run)
        self._idle_jobs.add(job)

    def destroy(self):
        self._closed = True
        for job in tuple(self._idle_jobs):
            try:
                self.after_cancel(job)
            except tk.TclError:
                pass
        self._idle_jobs.clear()
        self.photos.clear()
        super().destroy()

    def set_loading(self, champion_name=""):
        self.champion_name = champion_name or ""
        self.data, self._error, self._loading = None, "", True
        self.render()

    def set_data(self, data):
        self.data, self._error, self._loading = data, "", False
        self.render()

    def set_error(self, message):
        self.data, self._loading = None, False
        self._error = str(message).replace("\n", " ")[:180]
        self.render()

    def _wrapped_name(self, name):
        # Reserve space for a 36px icon and the Treeview's indentation.
        limit = max(140, int(self.tree.column("#0", "width")) - 56)
        lines, current = [], ""
        for word in str(name).split():
            trial = (current + " " + word).strip()
            if current and self.name_font.measure(trial) > limit:
                lines.append(current)
                current = word
            else:
                current = trial
        if current:
            lines.append(current)
        return "\n".join(lines)

    def render(self):
        self.tree.delete(*self.tree.get_children())
        self.icon_rows = {}
        all_rows = (self.data or {}).get("rows", [])
        self.rows = ranked_rows(all_rows, self.include_small.get(), self.rarity.get())
        self.tree.configure(height=max(1, min(7, len(self.rows))))
        for index, row in enumerate(self.rows):
            low = row["sample_count"] < MIN_GAMES
            self.tree.insert("", "end", iid=str(index),
                             text=self._wrapped_name(row.get("name", f"Augment {row.get('id', '')}")),
                             values=(f"{row['win_rate'] * 100:.1f}",
                                     compact_count(row["sample_count"]) + ("*" if low else "")),
                             tags=("low_sample",) if low else ())
            url = row.get("icon_url")
            if url:
                self.icon_rows[str(index)] = url
                if url in self.photos:
                    self.tree.item(str(index), image=self.photos[url])
        if self._loading:
            self.status.set("Loading…")
            self.note.set("Loading champion-specific augment rates…")
        elif self._error:
            self.status.set("Unavailable")
            self.note.set(self._error)
        elif self.data is None:
            self.status.set("")
            self.note.set("Choose a champion to see augment win rates.")
        else:
            self.status.set(f"{len(self.rows)} augments")
            if not ranked_rows(all_rows, True):
                self.note.set("No champion-specific augment rates in this sample.")
            elif not self.rows:
                self.note.set("No augments match these filters. Try another rarity or include small samples.")
            else:
                self.note.set("All picks · * fewer than 200 games" if self.include_small.get()
                              else "All picks · 200+ games")
        self.retry_button.pack_forget()
        if self._error:
            self.retry_button.pack(anchor="e", pady=(6, 0))
        self._schedule(self._visible_icons)
        self._schedule(self.on_resize)

    def _scrolled(self, first, last):
        self.scrollbar.set(first, last)
        self._visible_icons()

    def _visible_icons(self):
        if self._closed or not self.tree.winfo_ismapped() or not self.rows:
            return
        first, last = self.tree.yview()
        start = int(first * len(self.rows))
        end = min(len(self.rows), math.ceil(last * len(self.rows)), start + 8)
        for index in range(start, end):
            iid = str(index)
            url = self.icon_rows.get(iid)
            if url and self.tree.exists(iid) and self.tree.bbox(iid):
                photo = self.photos.get(url)
                if photo is None:
                    photo = self.request_icon(url)
                    if photo is not None:
                        self.photos[url] = photo
                if photo is not None:
                    self.tree.item(iid, image=photo)

    def on_icon(self, url, photo):
        if self._closed:
            return
        self.photos[url] = photo
        for iid, wanted in self.icon_rows.items():
            if wanted == url and self.tree.exists(iid):
                self.tree.item(iid, image=photo)
