"""Informational ARAM champion choices; no network or game actions on this view."""
from __future__ import annotations

import math
import tkinter as tk
from tkinter import ttk
import webbrowser


from .theme import BG, PANEL, FIELD, TEXT, MUTED, ACCENT, AMBER, BORDER


def _number(value):
    return value if (isinstance(value, (int, float)) and not isinstance(value, bool)
                     and math.isfinite(value)) else None


def _count(value):
    value = _number(value)
    return int(value) if value is not None and value >= 0 and value == int(value) else None


def _rate(row):
    value = _number(row.get("win_rate"))
    return value if (not row.get("missing") and (_count(row.get("sample_count")) or 0) > 0
                     and value is not None and 0 <= value <= 1) else None


def _compact_count(value):
    if value is None:
        return "—"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    if value >= 10_000:
        return f"{value / 1000:.1f}k"
    return f"{value:,}"


def display_metrics(row):
    rate, count = _rate(row), _count(row.get("sample_count"))
    low = count is not None and 0 < count < 200
    return (f"{rate * 100:.1f}" if rate is not None else "—",
            _compact_count(count) + ("*" if low else ""), low)


def ordered_rows(rows):
    return sorted(rows, key=lambda row: (_rate(row) is None, -(_rate(row) or 0),
                                        -(_count(row.get("sample_count")) or 0),
                                        str(row.get("name", "")).casefold()))


class ChampionChoicesPanel(tk.Frame):
    """set_context defines authoritative IDs; set_data supplies their rates.

    request_icon(url) may return a cached 36px PhotoImage while queuing a miss.
    Every call and on_icon callback belongs on the Tk thread. This component
    never fetches data, changes champion selection, or sends game input.
    """

    def __init__(self, parent, request_icon=None, on_resize=None):
        super().__init__(parent, bg=BG)
        self.request_icon = request_icon or (lambda _url: None)
        self.on_resize = on_resize or (lambda: None)
        self.champion_ids = None
        self.current_id = None
        self.names, self.rates, self.photos, self.tree_icons = {}, {}, {}, {}
        self.rows = []
        self.data = None
        self.source_url = None
        self.incomplete = False
        self._data_notes, self._error_text = [], ""
        self._closed = False
        self._idle_jobs = set()
        self.title = tk.StringVar(self, "Available champions")
        self.status = tk.StringVar(self, "List unavailable")
        self.source = tk.StringVar(self, "ARAM Mayhem win rates")
        self.note = tk.StringVar(self)
        self._build()
        self._render()

    def _label(self, parent, variable, color=MUTED, size=9, bold=False, **kwargs):
        return tk.Label(parent, textvariable=variable, bg=parent.cget("bg"), fg=color,
                        font=("Segoe UI", size, "bold" if bold else "normal"), **kwargs)

    def _build(self):
        heading = tk.Frame(self, bg=BG)
        heading.pack(fill="x", pady=(2, 9))
        self._label(heading, self.title, TEXT, 11, bold=True, anchor="w").pack(side="left")
        self._label(heading, self.status, MUTED, 8, anchor="e").pack(side="right")
        style = ttk.Style(self)
        style.configure("Choices.Treeview", background=PANEL, fieldbackground=PANEL,
                        foreground=TEXT, rowheight=48, borderwidth=0, font=("Segoe UI", 11))
        style.configure("Choices.Treeview.Heading", background=PANEL, foreground=MUTED,
                        borderwidth=0, relief="flat", padding=(8, 8), font=("Segoe UI", 8, "bold"))
        style.map("Choices.Treeview", background=[("selected", FIELD)],
                  foreground=[("selected", TEXT)])
        table = tk.Frame(self, bg=PANEL, highlightbackground=BORDER, highlightthickness=1)
        table.pack(fill="x")
        self.tree = ttk.Treeview(table, columns=("win", "games"), show="tree headings",
                                selectmode="none", style="Choices.Treeview", height=1)
        self.tree.heading("#0", text="CHAMPION", anchor="w")
        self.tree.heading("win", text="WIN % ↓", anchor="e")
        self.tree.heading("games", text="GAMES", anchor="e")
        self.tree.column("#0", width=222, minwidth=135, stretch=True, anchor="w")
        self.tree.column("win", width=65, minwidth=60, stretch=False, anchor="e")
        self.tree.column("games", width=85, minwidth=75, stretch=False, anchor="e")
        self.tree.tag_configure("highest", background="#16342f", font=("Segoe UI", 11, "bold"))
        self.tree.tag_configure("current", foreground=ACCENT)
        self.tree.tag_configure("low_sample", foreground=AMBER)
        self.tree.pack(side="left", fill="x", expand=True)
        self.scrollbar = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        self.scrollbar.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=self._scrolled)
        self.tree.bind("<Map>", lambda _: self._schedule(self._visible_icons))
        self.source_label = self._label(self, self.source, anchor="w", justify="left",
                                        wraplength=398, cursor="hand2")
        self.source_label.pack(fill="x", pady=(8, 0))
        self.source_label.bind("<Button-1>", lambda _: self.open_source())
        self.note_label = self._label(self, self.note, anchor="w", justify="left", wraplength=398)

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

    def set_context(self, champion_ids, current_id=None, names=None, *, incomplete=False):
        ids = None if champion_ids is None else tuple(dict.fromkeys(
            str(value) for value in champion_ids if value is not None and str(value)))
        changed = ((ids is None) != (self.champion_ids is None)
                   or (ids is not None and set(ids) != set(self.champion_ids or ())))
        self.champion_ids = ids
        self.current_id = str(current_id) if current_id is not None else None
        self.incomplete = bool(incomplete)
        if names is not None:
            self.names = {str(key): value for key, value in names.items()}
        if changed:
            self.data, self.rates, self.source_url = None, {}, None
            self._error_text = ""
            self.source.set("ARAM Mayhem win rates")
        self.status.set("List unavailable" if ids is None else "None available" if not ids
                        else "Rates unavailable" if self._error_text
                        else "" if self.data is not None else "Rates pending")
        self._render()

    def set_loading(self):
        if self.champion_ids:
            self.status.set("Loading rates…")
            self._error_text = ""
            self._render_note()

    def set_data(self, data):
        if not self.champion_ids:
            return
        allowed = set(self.champion_ids)
        incoming = {}
        for row in data.get("rows", []):
            ident = str(row.get("champion_id", ""))
            if ident in allowed:
                incoming[ident] = dict(row)
        # A late response for a disjoint previous bench cannot replace this one.
        if data.get("rows") and not incoming:
            return
        self.data, self.rates = data, incoming
        self._error_text = ""
        self.source_url = data.get("source_url")
        pieces = ["ARAM Mayhem", str(data.get("source") or "ARAMKit")]
        if data.get("bracket") in ("all", "high"):
            pieces.append("All brackets" if data["bracket"] == "all" else "High bracket")
        if data.get("version"):
            pieces.append(f"Patch {data['version']}")
        if data.get("data_date"):
            pieces.append(str(data["data_date"]))
        if data.get("stale"):
            pieces.append("CACHED")
        self.source.set(" · ".join(pieces))
        self.status.set("Cached rates" if data.get("stale") else "")
        self._render()

    def set_error(self, text):
        self.data, self.rates, self.source_url = None, {}, None
        self.source.set("ARAM Mayhem win rates")
        self.status.set("Rates unavailable")
        self._error_text = str(text).replace("\n", " ")[:180]
        self._render()

    def _render_note(self):
        notes = ["Some choices unavailable"] if self.incomplete else []
        notes.extend(self._data_notes)
        if self._error_text:
            notes.append(self._error_text)
        self.note.set(" · ".join(notes))
        self.note_label.pack_forget()
        if notes:
            self.note_label.pack(fill="x", pady=(2, 0))

    def _context_row(self, ident):
        catalog = self.names.get(ident, {})
        if isinstance(catalog, str):
            catalog = {"name": catalog}
        row = {"champion_id": ident, "name": catalog.get("name") or f"Champion {ident}",
               "icon": catalog.get("icon") or catalog.get("icon_url"),
               "win_rate": None, "sample_count": None, "missing": True}
        row.update(self.rates.get(ident, {}))
        return row

    def _render(self):
        position = self.tree.yview()[0] if self.tree.get_children() else 0
        self.tree.delete(*self.tree.get_children())
        self.tree_icons = {}
        self.rows = ordered_rows([self._context_row(ident) for ident in self.champion_ids or ()])
        self.title.set("Available champions" + (f" · {len(self.rows)}" if self.rows else ""))
        self.tree.configure(height=max(1, min(5, len(self.rows))))
        low_sample, missing = False, False
        rates = [_rate(row) for row in self.rows if _rate(row) is not None]
        highest = max(rates) if len(rates) > 1 else None
        for row in self.rows:
            ident = str(row["champion_id"])
            iid = "c:" + ident
            win, count, low = display_metrics(row)
            low_sample = low_sample or low
            missing = missing or win == "—"
            current = ident == self.current_id
            tags = []
            if highest is not None and _rate(row) == highest and (_count(row.get("sample_count")) or 0) >= 200:
                tags.append("highest")
            if low:
                tags.append("low_sample")
            elif current:
                tags.append("current")
            self.tree.insert("", "end", iid=iid, text=("● " if current else "") + str(row["name"]),
                             values=(win, count), tags=tags)
            url = row.get("icon_url") or row.get("icon")
            if url:
                self.tree_icons[iid] = url
                photo = self.photos.get(url)
                if photo is not None:
                    self.tree.item(iid, image=photo)
        self.tree.yview_moveto(position)
        notes = []
        if low_sample:
            notes.append("* Fewer than 200 games")
        if missing and self.data is not None:
            notes.append("— No rate in this sample")
        self._data_notes = notes
        self._render_note()
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
        end = min(len(self.rows), math.ceil(last * len(self.rows)), start + 6)
        for row in self.rows[start:end]:
            iid = "c:" + str(row["champion_id"])
            url = self.tree_icons.get(iid)
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
        for iid, wanted in self.tree_icons.items():
            if wanted == url and self.tree.exists(iid):
                self.tree.item(iid, image=photo)

    def open_source(self):
        if isinstance(self.source_url, str) and self.source_url.startswith("https://"):
            webbrowser.open(self.source_url)
