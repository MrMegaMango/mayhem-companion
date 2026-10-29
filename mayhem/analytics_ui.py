"""Usage sharing settings; all transport lives in Analytics."""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Callable

BG, PANEL, FIELD = "#10151e", "#192231", "#222e40"
TEXT, MUTED, ACCENT, AMBER = "#edf4ff", "#9aacc4", "#7ce1bd", "#efc47c"


class AnalyticsDialog(tk.Toplevel):
    """A manually opened settings view that never performs network requests.

    Opening or closing this view never changes the sharing preference. The
    parent owns singleton handling; startup defaults belong to Analytics.
    """

    def __init__(self, parent, analytics, *, position=None, work_area=None,
                 on_change: Callable[[], None] | None = None,
                 endpoint_label="Cloudflare"):
        super().__init__(parent)
        self.withdraw()
        self.analytics = analytics
        self._on_change = on_change
        self._endpoint_label = str(endpoint_label)[:180]
        self._work_area = work_area
        self._poll_job = None
        self._closed = False
        self._details_visible = False
        self._action_error = ""
        self._deletion_seen = False
        self._last_status = None
        self.title("Usage sharing — Mayhem Companion")
        self.configure(bg=BG)
        self.attributes("-topmost", True)
        self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.bind("<Escape>", lambda _: self._close())

        self.body = tk.Frame(self, bg=BG)
        self.body.pack(fill="both", expand=True, padx=20, pady=18)
        heading = tk.Frame(self.body, bg=BG)
        heading.pack(fill="x", pady=(0, 12))
        self._label(heading, "Usage sharing", size=15, bold=True).pack(side="left")
        self.badge = self._label(heading, "OFF", size=9, color=MUTED, bold=True)
        self.badge.pack(side="right")
        self._label(
            self.body, "Basic usage sharing is enabled by default.\n"
            "You can turn it off here.", color=MUTED,
        ).pack(fill="x", pady=(0, 14))
        self._label(
            self.body, "Random installation ID, app version, mode, and session activity.",
        ).pack(fill="x", pady=(0, 10))
        self.detail_button = self._button(self.body, "What is shared?", self._toggle_details)
        self.detail_button.pack(anchor="w", pady=(0, 12))

        # Sharing and deletion controls stay above the expandable explanation.
        actions = tk.Frame(self.body, bg=BG)
        actions.pack(fill="x")
        actions.columnconfigure(0, weight=1, uniform="choice")
        actions.columnconfigure(1, weight=1, uniform="choice")
        self.close_button = self._button(actions, "Close", self._close)
        self.close_button.grid(row=0, column=0, sticky="ew", padx=(0, 5))
        self.share_button = self._button(actions, "Enable usage sharing", self._toggle_sharing)
        self.share_button.grid(row=0, column=1, sticky="ew", padx=(5, 0))
        self.delete_button = self._button(self.body, "Delete data & reset ID", self._delete)
        self.delete_button.pack(fill="x", pady=(10, 0))

        self.status = tk.StringVar(self)
        self.status_label = self._label(self.body, textvariable=self.status, color=MUTED, size=9)
        self.status_label.pack(fill="x", pady=(12, 0))
        self.details_frame = tk.Frame(self.body, bg=PANEL)
        self.details = tk.Text(self.details_frame, width=39, height=9, wrap="word",
                               bg=PANEL, fg=MUTED, relief="flat", font=("Segoe UI", 9),
                               padx=10, pady=10, selectbackground="#405471",
                               highlightthickness=0)
        scrollbar = ttk.Scrollbar(self.details_frame, command=self.details.yview)
        scrollbar.pack(side="right", fill="y")
        self.details.configure(yscrollcommand=scrollbar.set, state="disabled")
        self.details.pack(side="left", fill="both", expand=True)

        self._refresh()
        self._fit(position)
        self.deiconify()
        self.lift()
        # Merely opening settings must not toggle sharing.
        self.focus_set()
        self._schedule_poll()

    @staticmethod
    def _label(parent, text="", *, size=10, color=TEXT, bold=False, **kwargs):
        return tk.Label(parent, text=text, bg=parent.cget("bg"), fg=color,
                        font=("Segoe UI", size, "bold" if bold else "normal"),
                        anchor="w", justify="left", wraplength=420, **kwargs)

    @staticmethod
    def _button(parent, text, command):
        return tk.Button(parent, text=text, command=command, bg=FIELD, fg=TEXT,
                         activebackground="#35475f", activeforeground=TEXT,
                         disabledforeground="#68768a", relief="flat", bd=0,
                         font=("Segoe UI", 9), padx=10, pady=8, cursor="hand2")

    def _state(self):
        try:
            value = self.analytics.status()
            return value if isinstance(value, dict) else {}
        except Exception:
            return {"last_error": "Could not read usage settings."}

    def _refresh(self):
        state = self._state()
        enabled = state.get("consent") == "enabled"
        pending = bool(state.get("deletion_pending"))
        if pending:
            self._deletion_seen = True
        blocked = bool(state.get("blocked"))
        configured = bool(state.get("configured"))
        ident = state.get("installation_id")
        self.badge.configure(text="PAUSED" if blocked else "DELETING" if pending else "ON" if enabled else "OFF",
                             fg=AMBER if blocked or pending else ACCENT if enabled else MUTED)
        self.share_button.configure(text="Turn off" if enabled else "Enable usage sharing",
                                    state="normal" if not pending and (enabled or (configured and not blocked)) else "disabled")
        self.delete_button.configure(state="normal" if ident and not pending else "disabled")

        if self._action_error:
            message = self._action_error
        elif blocked:
            message = str(state.get("last_error") or "Usage sharing is paused.")[:250]
            if pending:
                message += " Deletion is still pending."
        elif pending:
            message = "Sharing is off. Deletion is pending and will retry when connected, even while sharing is off."
        elif state.get("last_error"):
            message = str(state["last_error"])[:250]
        elif self._deletion_seen and not ident and not enabled:
            message = "Deletion confirmed. Sharing is off; enabling it later creates a new random ID."
        elif not configured:
            message = "Not connected yet. Sharing stays off."
        elif enabled:
            count = state.get("queued", 0)
            count = count if isinstance(count, int) and not isinstance(count, bool) else 0
            message = (f"Sharing is on · {count} report{'s' if count != 1 else ''} waiting to send. "
                       "Only time with the panel visible counts.")
        elif ident:
            message = "Sharing is off. Delete data to remove earlier reports and reset this installation’s ID."
        else:
            message = "Sharing is off. Use Enable usage sharing to turn it on."
        self.status.set(message)
        self.status_label.configure(fg=AMBER if blocked or pending or self._action_error or state.get("last_error") else MUTED)
        if self._details_visible and state != self._last_status:
            self._render_details(state)
        self._last_status = state.copy()
        return state

    def _render_details(self, state):
        ident = state.get("installation_id") or "Not created"
        text = (
            "Sharing starts enabled on new installations and existing installations without a saved choice. "
            "A saved off preference stays off. Every feature works with sharing off.\n\n"
            "Shared fields\n"
            "• Random installation ID and session/event IDs\n"
            "• App version\n"
            "• Event: session start, heartbeat, end, or mode used\n"
            "• UTC time rounded to the minute\n"
            "• Session duration rounded to minutes\n"
            "• Mode: ARAM, Mayhem, Arena, TFT, or unknown\n\n"
            "Sessions count time when the companion panel is visible. Reports are sent in small batches.\n\n"
            "Riot IDs, champion choices, match history, screenshots, keystrokes, and raw logs are not collected.\n\n"
            "The stable random ID makes reports pseudonymous, not fully anonymous. "
            f"Destination: {self._endpoint_label}. Cloudflare handles network metadata, including your IP address; "
            "the app’s analytics database does not store IP addresses. Usage events expire after 90 days.\n\n"
            "Turning sharing off stops new reports and clears unsent reports. A request already in flight may finish. "
            "Delete data turns sharing off and removes the stored identity and reports after the server confirms. "
            "Deletion removes active database records; Cloudflare backups may retain earlier states until they expire. "
            "A one-way hash of the deleted ID remains for up to 25 hours to block late uploads. "
            "Deletion retries while offline; keep the app installed until it confirms completion. "
            "Enabling later creates a new random ID.\n\n"
            f"This installation’s ID\n{ident}"
        )
        previous = self.details.yview()[0]
        self.details.configure(state="normal")
        self.details.delete("1.0", "end")
        self.details.insert("1.0", text)
        self.details.configure(state="disabled")
        self.details.yview_moveto(previous)

    def _changed(self):
        if self._on_change is not None:
            self._on_change()

    def _toggle_sharing(self):
        state = self._state()
        enabled = state.get("consent") == "enabled"
        if state.get("deletion_pending") or (not enabled and (not state.get("configured") or state.get("blocked"))):
            self._refresh()
            return
        try:
            self.analytics.set_enabled(not enabled)
        except Exception:
            self._action_error = "Could not save the sharing setting. Please try again."
        else:
            self._action_error = ""
            self._changed()
        self._refresh()
        self._fit()

    def _delete(self):
        state = self._state()
        if not state.get("installation_id") or state.get("deletion_pending"):
            return
        try:
            self.analytics.delete_data()
        except Exception:
            self._action_error = "Could not request deletion. Please try again."
        else:
            self._action_error = ""
            self._changed()
        self._refresh()
        self._fit()

    def _close(self):
        if self._closed:
            return
        self.destroy()

    def _toggle_details(self):
        self._details_visible = not self._details_visible
        if self._details_visible:
            self.details_frame.pack(fill="both", expand=True, pady=(12, 0))
            self.detail_button.configure(text="Hide details")
            self._render_details(self._state())
        else:
            self.details_frame.pack_forget()
            self.detail_button.configure(text="What is shared?")
        self._fit()

    def _fit(self, position=None):
        self.update_idletasks()
        width, height = max(470, self.winfo_reqwidth()), self.winfo_reqheight()
        if position is None and self.winfo_ismapped():
            position = (self.winfo_x(), self.winfo_y())
        if self._work_area is not None:
            left, top, right, bottom = self._work_area
            width = min(width, right - left - 16)
            height = min(height, bottom - top - 60)
            x, y = position if position is not None else (left + 8, top + 40)
            position = (max(left + 8, min(x, right - width - 8)),
                        max(top + 40, min(y, bottom - height - 20)))
        geometry = f"{width}x{height}"
        if position is not None:
            geometry += f"+{int(position[0])}+{int(position[1])}"
        self.geometry(geometry)

    def _schedule_poll(self):
        if not self._closed:
            self._poll_job = self.after(1000, self._poll)

    def _poll(self):
        self._poll_job = None
        if self._closed:
            return
        previous = self._last_status
        current = self._refresh()
        if current != previous:
            self._fit()
        self._schedule_poll()

    def destroy(self):
        self._closed = True
        if self._poll_job is not None:
            try:
                self.after_cancel(self._poll_job)
            except tk.TclError:
                pass
            self._poll_job = None
        super().destroy()
