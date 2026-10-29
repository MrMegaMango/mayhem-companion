"""A small, user-initiated feedback draft; delivery is supplied by the app."""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Callable, Mapping

from .feedback import CATEGORIES, MAX_DESCRIPTION, MAX_CONTACT

BG, PANEL, FIELD = "#10151e", "#192231", "#222e40"
TEXT, MUTED, ACCENT, AMBER = "#edf4ff", "#9aacc4", "#7ce1bd", "#efc47c"

ReportBuilder = Callable[[str, str, str, bool], str]
Submitter = Callable[[str, str, str, bool], tuple[bool, str]]


class FeedbackDialog(tk.Toplevel):
    """Nonmodal draft editor with explicit review and email hand-off.

    Callbacks receive category, description, optional contact, and technical
    details opt-in. They must perform no blocking network requests. A successful
    hand-off is not a successful submission: ``submit`` should describe the
    external draft opening. No logs, screenshots, or files are collected here.
    """

    def __init__(self, parent, *, version: str, mode: str,
                 diagnostics: Mapping[str, str], submit: Submitter,
                 build_report: ReportBuilder, destination_label: str,
                 position: tuple[int, int] | None = None,
                 work_area: tuple[int, int, int, int] | None = None,
                 screenshot_hint: str = ""):
        super().__init__(parent)
        # A transient of a withdrawn root is also hidden on some window
        # managers. This must work from the tray while the panel stays hidden.
        self.withdraw()
        self.title("Send feedback — Mayhem Companion")
        self.configure(bg=BG)
        self.attributes("-topmost", True)
        self.resizable(True, True)
        self.minsize(410, 550)
        self._submit = submit
        self._build_report = build_report
        self._preview_visible = False
        self._work_area = work_area
        self._diagnostics = dict(diagnostics)
        self.category = tk.StringVar(self, value=CATEGORIES[0])
        self.contact = tk.StringVar(self, value="")
        self.include_diagnostics = tk.BooleanVar(self, value=False)
        self.status = tk.StringVar(self, value="Review and send the draft in your email app.")
        self.count = tk.StringVar(self, value=f"0 / {MAX_DESCRIPTION:,}")

        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True, padx=20, pady=(16, 10))
        body.columnconfigure(0, weight=1)
        body.rowconfigure(5, weight=1)
        self._label(body, "Send feedback", size=16, bold=True).grid(row=0, column=0, sticky="w")
        self._label(body, f"Mayhem Companion {version}  ·  {mode}", color=MUTED).grid(
            row=1, column=0, sticky="w", pady=(3, 14))

        self._label(body, "Type", bold=True).grid(row=2, column=0, sticky="w")
        self.category_box = ttk.Combobox(body, textvariable=self.category,
                                        values=CATEGORIES, state="readonly")
        self.category_box.grid(row=3, column=0, sticky="ew", pady=(5, 13))
        self.category_box.bind("<<ComboboxSelected>>", self._refresh_preview)

        message_header = tk.Frame(body, bg=BG)
        message_header.grid(row=4, column=0, sticky="ew", pady=(0, 5))
        self._label(message_header, "Description", bold=True).pack(side="left")
        self._label(message_header, textvariable=self.count, color=MUTED, size=8).pack(side="right")
        message_frame = tk.Frame(body, bg=FIELD)
        message_frame.grid(row=5, column=0, sticky="nsew")
        self.description = self._text(message_frame, height=7)
        self.description.pack(side="left", fill="both", expand=True)
        message_scroll = ttk.Scrollbar(message_frame, command=self.description.yview)
        message_scroll.pack(side="right", fill="y")
        self.description.configure(yscrollcommand=message_scroll.set)
        self.description.bind("<<Modified>>", self._message_changed)
        self.description.bind("<Tab>", self._next_field)
        self.description.bind("<Shift-Tab>", self._previous_field)

        self._label(body, "Reply address (optional)", bold=True).grid(
            row=6, column=0, sticky="w", pady=(13, 5))
        self.contact_entry = tk.Entry(body, textvariable=self.contact, bg=FIELD, fg=TEXT,
                                      insertbackground=TEXT, relief="flat",
                                      font=("Segoe UI", 10), highlightthickness=1,
                                      highlightbackground=FIELD, highlightcolor=ACCENT)
        self.contact_entry.grid(row=7, column=0, sticky="ew", ipady=5)
        self.contact.trace_add("write", lambda *_: self._refresh_preview())

        options = tk.Frame(body, bg=BG)
        options.grid(row=8, column=0, sticky="ew", pady=(12, 4))
        self.diagnostics_check = tk.Checkbutton(
            options, text="Include technical details", variable=self.include_diagnostics,
            command=self._refresh_preview, bg=BG, fg=TEXT, selectcolor=FIELD,
            activebackground=BG, activeforeground=TEXT, font=("Segoe UI", 9),
            bd=0, padx=0, state="normal" if self._diagnostics else "disabled")
        self.diagnostics_check.pack(side="left")
        self.review_button = self._button(options, "Review report", self._toggle_preview, compact=True)
        self.review_button.pack(side="right")

        self.preview_frame = tk.Frame(body, bg=PANEL)
        self.preview = self._text(self.preview_frame, height=8, background=PANEL)
        self.preview.configure(state="disabled", font=("Consolas", 9))
        self.preview.pack(side="left", fill="both", expand=True)
        preview_scroll = ttk.Scrollbar(self.preview_frame, command=self.preview.yview)
        preview_scroll.pack(side="right", fill="y")
        self.preview.configure(yscrollcommand=preview_scroll.set)

        self._label(body, destination_label, color=MUTED, size=9,
                    wraplength=420, justify="left").grid(row=10, column=0, sticky="ew", pady=(8, 2))
        if screenshot_hint:
            self._label(body, screenshot_hint, color=MUTED, size=9,
                        wraplength=420, justify="left").grid(row=11, column=0, sticky="ew", pady=(0, 5))

        footer = tk.Frame(body, bg=BG)
        footer.grid(row=12, column=0, sticky="ew", pady=(10, 0))
        self.copy_button = self._button(footer, "Copy report", self._copy)
        self.copy_button.pack(side="left")
        self.submit_button = self._button(footer, "Open email draft", self._continue, primary=True)
        self.submit_button.pack(side="right")
        self.status_label = self._label(body, textvariable=self.status, color=MUTED, size=9,
                                        wraplength=420, justify="left")
        self.status_label.grid(row=13, column=0, sticky="ew", pady=(10, 0))
        self.bind("<Escape>", lambda _: self.destroy())
        self.protocol("WM_DELETE_WINDOW", self.destroy)

        self.update_idletasks()
        self._fit(max(470, self.winfo_reqwidth()), max(580, self.winfo_reqheight()), position)
        self.deiconify()
        self.lift()
        self.description.focus_set()

    @staticmethod
    def _label(parent, text="", *, size=10, color=TEXT, bold=False, **kwargs):
        return tk.Label(parent, text=text, bg=parent.cget("bg"), fg=color,
                        font=("Segoe UI", size, "bold" if bold else "normal"),
                        anchor="w", **kwargs)

    @staticmethod
    def _button(parent, text, command, *, primary=False, compact=False):
        return tk.Button(parent, text=text, command=command,
                         bg=ACCENT if primary else FIELD,
                         fg=BG if primary else TEXT, activebackground="#35475f",
                         activeforeground=TEXT, bd=0, padx=10,
                         pady=4 if compact else 8, font=("Segoe UI", 9),
                         cursor="hand2")

    @staticmethod
    def _text(parent, *, height, background=FIELD):
        return tk.Text(parent, bg=background, fg=TEXT, insertbackground=TEXT,
                       selectbackground="#405471", relief="flat", wrap="word",
                       undo=True, height=height, width=35, padx=9, pady=7,
                       font=("Segoe UI", 10), highlightthickness=1,
                       highlightbackground=background, highlightcolor=ACCENT)

    def _next_field(self, _event):
        self.contact_entry.focus_set()
        return "break"

    def _previous_field(self, _event):
        self.category_box.focus_set()
        return "break"

    def _values(self):
        return (self.category.get(), self.description.get("1.0", "end-1c").strip(),
                self.contact.get().strip(), bool(self.include_diagnostics.get()))

    def _message_changed(self, _event=None):
        if not self.description.edit_modified():
            return
        self.description.edit_modified(False)
        size = len(self.description.get("1.0", "end-1c"))
        self.count.set(f"{size:,} / {MAX_DESCRIPTION:,}")
        self._refresh_preview()

    def _validated_values(self):
        values = self._values()
        if values[0] not in CATEGORIES:
            self._set_status("Choose a feedback type.", error=True)
            self.category_box.focus_set()
            return None
        if not values[1]:
            self._set_status("Add a short description before continuing.", error=True)
            self.description.focus_set()
            return None
        if len(values[1]) > MAX_DESCRIPTION:
            self._set_status(f"Keep the description to {MAX_DESCRIPTION:,} characters or fewer.", error=True)
            self.description.focus_set()
            return None
        if len(values[2]) > MAX_CONTACT or "\r" in values[2] or "\n" in values[2]:
            self._set_status(f"Keep the reply address on one line, at most {MAX_CONTACT} characters.", error=True)
            self.contact_entry.focus_set()
            return None
        return values

    def _set_status(self, message, *, error=False):
        self.status.set(message)
        self.status_label.configure(fg=AMBER if error else MUTED)

    def _refresh_preview(self, _event=None):
        if not self._preview_visible:
            return
        try:
            report = self._build_report(*self._values())
        except ValueError as exc:
            report = str(exc)
        except Exception:
            report = "The report could not be prepared. Your draft is still here."
        self.preview.configure(state="normal")
        self.preview.delete("1.0", "end")
        self.preview.insert("1.0", report)
        self.preview.configure(state="disabled")

    def _toggle_preview(self):
        self._preview_visible = not self._preview_visible
        if self._preview_visible:
            self.preview_frame.grid(row=9, column=0, sticky="ew", pady=(6, 0))
            self.review_button.configure(text="Hide report")
            self._refresh_preview()
        else:
            self.preview_frame.grid_remove()
            self.review_button.configure(text="Review report")
        self.update_idletasks()
        height = max(580, self.winfo_reqheight())
        self._fit(max(410, self.winfo_width()), height, (self.winfo_x(), self.winfo_y()))

    def _fit(self, width, height, position):
        if self._work_area is not None:
            left, top, right, bottom = self._work_area
            # Leave room for the native title bar and border. The description
            # row flexes when the report preview needs space on a short screen.
            width = min(width, right - left - 16)
            height = min(height, bottom - top - 60)
            x, y = position if position is not None else (left + 8, top + 40)
            position = (max(left + 8, min(x, right - width - 8)),
                        max(top + 40, min(y, bottom - height - 20)))
        geometry = f"{width}x{height}"
        if position is not None:
            # '+-464' is an absolute negative virtual-desktop coordinate.
            geometry += f"+{int(position[0])}+{int(position[1])}"
        self.geometry(geometry)

    def _copy(self):
        values = self._validated_values()
        if values is None:
            return
        try:
            report = self._build_report(*values)
            self.clipboard_clear()
            self.clipboard_append(report)
        except ValueError as exc:
            self._set_status(str(exc), error=True)
        except Exception:
            self._set_status("Could not copy the report. Your draft is still here.", error=True)
        else:
            self._set_status("Report copied. Paste it into your feedback email.")

    def _continue(self):
        values = self._validated_values()
        if values is None:
            return
        try:
            success, message = self._submit(*values)
        except ValueError as exc:
            self._set_status(str(exc), error=True)
        except Exception:
            self._set_status("Could not open an email draft. Your feedback is still here.", error=True)
        else:
            self._set_status(message, error=not success)
