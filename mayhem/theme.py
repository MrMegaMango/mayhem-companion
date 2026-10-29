"""Shared visual tokens for the desktop companion."""
from tkinter import ttk
BG, PANEL, FIELD = "#0b111b", "#121d2b", "#1b2a3d"
TEXT, MUTED, ACCENT, AMBER = "#edf4fa", "#8fa3b8", "#87e4c3", "#efc47c"
BORDER = "#28394c"

def install(root):
    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure("TCombobox", fieldbackground=FIELD, background=FIELD,
                    foreground=TEXT, arrowcolor=MUTED, bordercolor=BORDER,
                    lightcolor=FIELD, darkcolor=FIELD, padding=(9, 7),
                    font=("Segoe UI", 10), arrowsize=12)
    style.map("TCombobox", fieldbackground=[("readonly", FIELD)],
              foreground=[("readonly", TEXT)], selectbackground=[("!disabled", FIELD)],
              selectforeground=[("!disabled", TEXT)], bordercolor=[("focus", ACCENT)])
    style.configure("Quiet.TCombobox", padding=(7, 4), bordercolor=BG,
                    background=BG, fieldbackground=BG, font=("Segoe UI", 9))
    style.map("Quiet.TCombobox", fieldbackground=[("readonly", BG)],
              foreground=[("readonly", MUTED)])
    style.configure("Champion.TCombobox", padding=(10, 9), font=("Segoe UI", 17))
    style.configure("Vertical.TScrollbar", background=FIELD, troughcolor=BG,
                    bordercolor=BG, lightcolor=FIELD, darkcolor=FIELD,
                    arrowcolor=MUTED, arrowsize=10, width=10)
    style.map("Vertical.TScrollbar", background=[("active", BORDER)])
    style.configure("Treeview", bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER, relief="flat")
    root.option_add("*TCombobox*Listbox.background", FIELD)
    root.option_add("*TCombobox*Listbox.foreground", TEXT)
    root.option_add("*TCombobox*Listbox.selectBackground", "#315447")
    root.option_add("*TCombobox*Listbox.selectForeground", TEXT)
    root.option_add("*TCombobox*Listbox.font", "{Segoe UI} 11")
    return style
