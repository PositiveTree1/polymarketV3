"""
╔══════════════════════════════════════════════════════════════════════════════╗
║  TITAN — SINGLE WALLET UI                                                    ║
║                                                                              ║
║  Tabs: SIGNALS · ALERTS · POSITIONS · P&L · WHALES · ANALYSIS · DIAG · LOG · CONFIG
╚══════════════════════════════════════════════════════════════════════════════╝
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk, font, scrolledtext
import threading
import time
import math
import importlib
import json
from dataclasses import dataclass, fields, is_dataclass
from datetime import datetime
from typing import TYPE_CHECKING, cast
from titan_protocol import TitanBackend
import os
import webbrowser
from pathlib import Path
from titan_ui_charts import PnLChart, PositionChart, init_chart_fonts

if TYPE_CHECKING:
    from titan_signals import SignalDict
    from titan_types import TradeRecordDict

try:
    import pyperclip
    HAS_PYPERCLIP = True
except ImportError:
    HAS_PYPERCLIP = False

try:
    import titan_telegram as telegram
    telegram_notifier = telegram.TelegramNotifier()
    HAS_TELEGRAM = True
    threading.Thread(target=telegram_notifier.notify_boot, daemon=True).start()
except ImportError:
    telegram_notifier = None
    HAS_TELEGRAM = False

try:
    from titan_ai import AIPanel
    HAS_AI = True
except ImportError:
    AIPanel = None
    HAS_AI = False


_GUIDE_FILE = Path(__file__).resolve().parent.parent / "docs" / "guide.txt"


def _load_guide_text():
    try:
        return _GUIDE_FILE.read_text(encoding="utf-8")
    except Exception as e:
        return f"Guide unavailable.\n{_GUIDE_FILE}\n{e}"


_GUIDE = _load_guide_text()


# ═══════════════════════════════════════════════════════════════════════════════
#  LOADING SCREEN
# ═══════════════════════════════════════════════════════════════════════════════
TITAN_ASCII = [
    "  ████████╗██╗████████╗ █████╗ ███╗   ██╗",
    "     ██╔══╝██║╚══██╔══╝██╔══██╗████╗  ██║",
    "     ██║   ██║   ██║   ███████║██╔██╗ ██║",
    "     ██║   ██║   ██║   ██╔══██║██║╚██╗██║",
    "     ██║   ██║   ██║   ██║  ██║██║ ╚████║",
    "     ╚═╝   ╚═╝   ╚═╝   ╚═╝  ╚═╝╚═╝  ╚═══╝",
    "",
    "    🐳  WHALE MIRROR ENGINE  —  SINGLE WALLET  🐳",
    "",
]

_ngrok_url = None  # Global for the dashboard tunnel

def show_loading_screen(root, api, on_complete):
    frame = tk.Frame(root, bg="#080810")
    frame.place(relx=0, rely=0, relwidth=1, relheight=1)
    inner = tk.Frame(frame, bg="#080810")
    inner.place(relx=0.5, rely=0.5, anchor="center")

    mono_big  = font.Font(family="Courier", size=11, weight="bold")
    mono_anim = font.Font(family="Courier", size=10)
    mono_sm   = font.Font(family="Courier", size=9)

    for line in TITAN_ASCII:
        col = "#00ff88" if ("TITAN" in line or "WHALE" in line) else "#1a4a2a"
        tk.Label(inner, text=line, fg=col, bg="#080810", font=mono_big, pady=0).pack()

    tk.Label(inner, text="", bg="#080810").pack()
    tk.Label(inner, text="─" * 52, fg="#1a3a2a", bg="#080810", font=mono_sm).pack()
    tk.Label(inner, text="", bg="#080810").pack()

    status_var = tk.StringVar(value="")
    tk.Label(inner, textvariable=status_var, fg="#00cc66", bg="#080810",
             font=mono_anim, width=52, anchor="w").pack()

    pb_frame  = tk.Frame(inner, bg="#080810")
    pb_frame.pack(pady=6)
    pb_canvas = tk.Canvas(pb_frame, width=420, height=16, bg="#0a0a18",
                          highlightthickness=1, highlightbackground="#1a3a2a")
    pb_canvas.pack()

    tick_var = tk.StringVar(value="")
    tk.Label(inner, textvariable=tick_var, fg="#334433", bg="#080810", font=mono_sm).pack(pady=2)

    SPINNERS    = ["⠋","⠙","⠹","⠸","⠼","⠴","⠦","⠧","⠇","⠏"]
    spin_idx    = [0]

    BOOT_TASKS = [
        ("Checking engine status...", lambda: api.get_status()),
        ("Loading P&L and bankroll state...", lambda: api.get_pnl_summary()),
        ("Fetching verified whale roster...", lambda: api.get_whales()),
        ("Syncing open positions...", lambda: api.get_positions(brief=False)),
        ("Downloading latest signals...", lambda: api.get_signals()),
        ("TITAN ONLINE — Follow The Whale", lambda: None),
    ]
    total_steps = len(BOOT_TASKS)
    current_step = [0]

    def draw_bar(fraction):
        pb_canvas.delete("all")
        fill_w = int(420 * fraction)
        pb_canvas.create_rectangle(0, 0, 420, 16, fill="#0a0a18", outline="")
        if fill_w > 0:
            pb_canvas.create_rectangle(0, 0, fill_w, 16, fill="#00aa55", outline="")
        if fill_w > 4:
            pb_canvas.create_rectangle(fill_w - 2, 0, fill_w, 16, fill="#00ff88", outline="")
        pb_canvas.create_text(210, 8, text=f"{int(fraction*100)}%",
                               fill="#ffffff", font=("Courier", 8))

    def update_ui():
        step_idx = current_step[0]
        if step_idx >= total_steps:
            status_var.set("  ✅  ALL SYSTEMS NOMINAL")
            draw_bar(1.0)
            tick_var.set("")
            root.after(600, lambda: (frame.destroy(), on_complete()))
            return

        frac = step_idx / total_steps
        draw_bar(frac)
        spinner = SPINNERS[spin_idx[0] % len(SPINNERS)]
        spin_idx[0] += 1
        label = BOOT_TASKS[step_idx][0]

        if step_idx == total_steps - 1:
            status_var.set(f"  🚀  {label}")
            tick_var.set("━" * 48)
        else:
            status_var.set(f"  {spinner}  {label}")
            tick_var.set("")

        root.after(80, update_ui)

    def run_tasks():
        for i, (label, task) in enumerate(BOOT_TASKS):
            current_step[0] = i
            try:
                task()
            except Exception:
                pass
            time.sleep(0.3)  # Small visual delay so the user can read the step
        current_step[0] = total_steps

    draw_bar(0.0)
    threading.Thread(target=run_tasks, daemon=True).start()
    root.after(80, update_ui)


# ═══════════════════════════════════════════════════════════════════════════════
#  ROOT WINDOW
# ═══════════════════════════════════════════════════════════════════════════════

def run_ui(api: TitanBackend) -> None:
    import ast as _ast
    _cfg: dict = {}

    def _open_pos_dict() -> dict:
        try:
            return {_ast.literal_eval(p["key"]): p for p in api.get_positions(brief=False)}
        except Exception as e:
            _log_ui_error("open positions cache", e)
            return {}
    def _wallet_cache() -> dict:
        try:
            return {w["wallet"]: w for w in api.get_whales()}
        except Exception as e:
            _log_ui_error("wallet cache", e)
            return {}


    root = tk.Tk()

    root.title("🐳 TITAN — Whale Mirror Engine")
    root.configure(bg="#080810")
    root.geometry("1600x960")
    root.minsize(1200, 700)
    
    mono    = font.Font(family="Courier", size=9)
    mono_sm = font.Font(family="Courier", size=8)
    mono_lg = font.Font(family="Courier", size=10)
    bold_hd = font.Font(family="Courier", size=10, weight="bold")
    title_f = font.Font(family="Courier", size=12, weight="bold")
    mono_xs = font.Font(family="Courier", size=7)
    init_chart_fonts(mono=mono, mono_sm=mono_sm, bold_hd=bold_hd, mono_xs=mono_xs)
    
    # ── Header row 1: title + subtitle ───────────────────────────────────────────
    hdr1 = tk.Frame(root, bg="#0a0a1a", pady=3)
    hdr1.pack(fill="x")

    app_title_var    = tk.StringVar(value="🐳 TITAN — Whale Mirror Engine")
    app_subtitle_var = tk.StringVar(value="v10 CONVICTION-ONLY | 2+ Elites | 20-72¢ Zone | -30% Stop | ENGINE ACTIVE")

    tk.Label(hdr1, textvariable=app_title_var,
             fg="#00ff88", bg="#0a0a1a", font=title_f).pack(side="left", padx=12)
    tk.Label(hdr1, textvariable=app_subtitle_var,
             fg="#2a5a3a", bg="#0a0a1a", font=mono).pack(side="left")

    # ── Header row 2: stats bar ───────────────────────────────────────────────────
    hdr2 = tk.Frame(root, bg="#0a0a1a", pady=2)
    hdr2.pack(fill="x")

    sf = tk.Frame(hdr2, bg="#0a0a1a")
    sf.pack(side="left", padx=6)

    hb_var   = tk.StringVar(value="⬤ waiting…")
    hb_label = tk.Label(sf, textvariable=hb_var, fg="#667788", bg="#0a0a1a", font=mono, padx=6)
    hb_label.pack(side="left")

    cycle_var    = tk.StringVar(value="Cycle: 0")
    ver_var      = tk.StringVar(value="Verified: —")
    elite_var    = tk.StringVar(value="Elite: 0")
    bank_var     = tk.StringVar(value="Bank: $20.00")
    pnl_var      = tk.StringVar(value="P&L: $0.00")
    sig_var      = tk.StringVar(value="Signals: —")
    pos_var      = tk.StringVar(value="Pos: —")
    status_var   = tk.StringVar(value="⏳ Booting…")
    cooldown_var = tk.StringVar(value="CD: 0")

    for v, c in [
        (cycle_var,    "#556677"),
        (ver_var,      "#00cc77"),
        (elite_var,    "#ff8844"),
        (bank_var,     "#00aaff"),
        (pnl_var,      "#00ff88"),
        (sig_var,      "#00aacc"),
        (pos_var,      "#ff8844"),
        (cooldown_var, "#888844"),
        (status_var,   "#778899"),
    ]:
        tk.Label(sf, textvariable=v, fg=c, bg="#0a0a1a", font=mono, padx=5).pack(side="left")
    
    # ── Body ──────────────────────────────────────────────────────────────────────
    body_pw = tk.PanedWindow(root, orient="horizontal", bg="#080810",
                              sashwidth=5, sashrelief="flat", handlesize=0, bd=0)
    body_pw.pack(fill="both", expand=True, padx=6, pady=4)
    
    nb_frame = tk.Frame(body_pw, bg="#080810")
    body_pw.add(nb_frame, minsize=860, stretch="always")

    # ── AI side panel (right pane) ────────────────────────────────────────────────
    ai_frame = tk.Frame(body_pw, bg="#080810")
    if HAS_AI:
        body_pw.add(ai_frame, minsize=360, stretch="never")

    nb = ttk.Notebook(nb_frame)
    nb.pack(fill="both", expand=True)
    
    sty = ttk.Style()
    sty.theme_use("clam")
    sty.configure("Treeview", background="#0c0c18", fieldbackground="#0c0c18",
        foreground="#cccccc", font=mono, rowheight=26)
    sty.configure("Treeview.Heading", background="#13132a", foreground="#00ff88", font=bold_hd)
    sty.map("Treeview", background=[("selected", "#1a2a4a")])
    sty.configure("TNotebook", background="#080810", borderwidth=0)
    sty.configure("TNotebook.Tab", background="#0d0d1a", foreground="#556677",
        font=mono, padding=[12, 6])
    sty.map("TNotebook.Tab",
        background=[("selected", "#1a1a30")],
        foreground=[("selected", "#00ff88")])
    

    # ───────────────────────────────────────────────────────────────────────────────
    #  ChartFrame — reusable wrapper: canvas chart + collapsible data panel
    # ───────────────────────────────────────────────────────────────────────────────
    class ChartFrame(tk.Frame):
        """
        Hosts any tk.Canvas-based chart plus a toggleable right-side data panel.
        Usage:
            cf = ChartFrame(parent, get_data_rows=lambda: [("label","value"), ...])
            canvas = tk.Canvas(cf.chart_area, ...)
            canvas.pack(fill="both", expand=True)
            cf.btn_bar   # tk.Frame — add extra buttons here
        Call cf.refresh_panel() to sync the panel without flicker.
        """
        _PANEL_W = 200

        def __init__(self, parent, get_data_rows, col_headers=("Date / Label", "Value"), **kwargs):
            super().__init__(parent, bg="#080810", **kwargs)
            self._get_data_rows  = get_data_rows
            self._panel_visible  = False
            self._last_row_count = 0

            # Outer horizontal container (chart | panel)
            self._outer = tk.Frame(self, bg="#080810")
            self._outer.pack(fill="both", expand=True)

            # Chart area — caller packs their canvas here
            self.chart_area = tk.Frame(self._outer, bg="#080810")
            self.chart_area.pack(side="left", fill="both", expand=True)

            # Data panel (hidden by default)
            self._panel = tk.Frame(self._outer, bg="#0a0a1a", width=self._PANEL_W)
            self._panel.pack_propagate(False)

            cols = col_headers
            self._tree = ttk.Treeview(self._panel, columns=cols, show="headings", height=20)
            self._tree.heading(cols[0], text=cols[0])
            self._tree.heading(cols[1], text=cols[1])
            self._tree.column(cols[0], width=110, anchor="center")
            self._tree.column(cols[1], width=80,  anchor="center")
            self._tree.tag_configure("pos", foreground="#00ff55", background="#001800")
            self._tree.tag_configure("neg", foreground="#ff5555", background="#1a0000")
            self._tree.tag_configure("neu", foreground="#aaaaaa", background="#0a0a1a")
            vsb = tk.Scrollbar(self._panel, command=self._tree.yview)
            self._tree.configure(yscrollcommand=vsb.set)
            vsb.pack(side="right", fill="y")
            self._tree.pack(fill="both", expand=True)

            # Button bar below the chart area
            self.btn_bar = tk.Frame(self, bg="#080810")
            self.btn_bar.pack(fill="x")

            self._toggle_btn = tk.Button(
                self.btn_bar, text="▶  Data",
                bg="#0d0d22", fg="#00aaff", activebackground="#1a1a33",
                activeforeground="#00ccff", relief="flat", bd=0,
                font=("Courier", 8), padx=8, pady=2,
                command=self.toggle_panel,
            )
            self._toggle_btn.pack(side="left")

        def toggle_panel(self):
            if self._panel_visible:
                self._panel.pack_forget()
                self._panel_visible  = False
                self._last_row_count = 0
                self._toggle_btn.config(text="▶  Data")
            else:
                self._panel.pack(side="left", fill="y", padx=(4, 0), in_=self._outer)
                self._panel_visible  = True
                self._last_row_count = 0
                self._toggle_btn.config(text="◀  Data")
                self._do_refresh()

        def refresh_panel(self, *, reset: bool = False):
            if self._panel_visible:
                self._do_refresh(reset=reset)

        def _do_refresh(self, *, reset: bool = False):
            rows = self._get_data_rows()
            if reset:
                self._tree.delete(*self._tree.get_children())
                self._last_row_count = 0
            if len(rows) == self._last_row_count:
                return
            n_existing = len(self._tree.get_children())
            for label, value in rows[n_existing:]:
                try:
                    fv = float(value.replace("$", "").replace("%", "").replace("+", ""))
                    tag = "pos" if fv > 0 else ("neg" if fv < 0 else "neu")
                except ValueError:
                    tag = "neu"
                self._tree.insert("", "end", values=(label, value), tags=(tag,))
            self._last_row_count = len(rows)
            children = self._tree.get_children()
            if children:
                self._tree.see(children[-1])

    # ═══════════════════════════════════════════════════════════════════════════════
    #  TAB 1: LIVE SIGNALS
    # ═══════════════════════════════════════════════════════════════════════════════

    tab_live = tk.Frame(nb, bg="#080810")
    nb.add(tab_live, text="  🎯 SIGNALS  ")
    
    sig_cols = ("Sc","Market (Full Title)","Side","WEntry$","Now$","Drift%","Age","Flow$","Whales","Mode")
    sig_tree = ttk.Treeview(tab_live, columns=sig_cols, show="headings", height=10)
    sw = {"Sc":45,"Market (Full Title)":420,"Side":110,"WEntry$":72,"Now$":72,
          "Drift%":65,"Age":50,"Flow$":80,"Whales":55,"Mode":65}
    for c in sig_cols:
        sig_tree.heading(c, text=c)
        sig_tree.column(c, width=sw[c], anchor="w" if c == "Market (Full Title)" else "center")
    
    sig_tree.tag_configure("CONVICTION", foreground="#00ff55", background="#001500")
    sig_tree.tag_configure("ALERT",      foreground="#00ff55", background="#001500")
    sig_tree.tag_configure("STRONG",     foreground="#ffdd00", background="#181400")
    sig_tree.tag_configure("MEDIUM",     foreground="#55aaff", background="#000d1a")
    sig_tree.tag_configure("HFT",        foreground="#ff55ff", background="#150015")
    sig_tree.tag_configure("ELITE_ONLY", foreground="#ff8844", background="#1a0d00")
    sig_tree.tag_configure("STALE",      foreground="#555555", background="#0c0c18")
    
    sig_vsb = tk.Scrollbar(tab_live, command=sig_tree.yview)
    sig_tree.configure(yscrollcommand=sig_vsb.set)
    sig_vsb.pack(side="right", fill="y")
    sig_tree.pack(fill="x", padx=4, pady=(4,2))
    _signal_tree_items: dict[str, SignalDict] = {}
    
    lf = tk.Frame(tab_live, bg="#080810")
    lf.pack(fill="both", expand=True, padx=4)
    sig_log = tk.Text(lf, bg="#060610", fg="#44ff44", font=mono,
                      selectbackground="#1a2a4a", wrap="word")
    sb_ = tk.Scrollbar(lf, command=sig_log.yview, bg="#0d0d1a")
    sig_log.configure(yscrollcommand=sb_.set)
    sb_.pack(side="right", fill="y")
    sig_log.pack(fill="both", expand=True)
    
    _live_subtitle_var = tk.StringVar(value="Follow The Whale: BUY when whale buys, SELL when whale sells | connecting...")
    tk.Label(tab_live, textvariable=_live_subtitle_var,
             fg="#335544", bg="#080810", font=mono, pady=2).pack()

    sig_btn_bar = tk.Frame(tab_live, bg="#080810")
    sig_btn_bar.pack(fill="x", padx=4, pady=(0,4))

    _sig_hist_btn_var = tk.StringVar(value="📜 SHOW HISTORY")
    _debug_mode = [False]
    _debug_btn_var = tk.StringVar(value="🐞 DEBUG OFF")

    def _toggle_signal_history():
        _show_signal_history[0] = not _show_signal_history[0]
        _sig_hist_btn_var.set("📂 SHOW LIVE" if _show_signal_history[0] else "📜 SHOW HISTORY")
        if not _show_signal_history[0]:
            _signal_history_cache[0] = []
        _pending_update[0] = True

    def _toggle_debug_mode() -> None:
        _debug_mode[0] = not _debug_mode[0]
        _debug_btn_var.set("🐞 DEBUG ON" if _debug_mode[0] else "🐞 DEBUG OFF")
        log(f"Signals debug mode {'enabled' if _debug_mode[0] else 'disabled'}", "INFO")

    tk.Button(sig_btn_bar, textvariable=_sig_hist_btn_var, bg="#1a1a00", fg="#ffcc44",
              font=mono_sm, command=_toggle_signal_history).pack(side="left", padx=4, pady=2)
    tk.Button(sig_btn_bar, textvariable=_debug_btn_var, bg="#1a1320", fg="#d8b4ff",
              font=mono_sm, command=_toggle_debug_mode).pack(side="left", padx=4, pady=2)
    tk.Label(sig_btn_bar, text="Current cycle or recent DB history", fg="#334455",
             bg="#080810", font=mono_sm).pack(side="left", padx=8)
    
    
    # ═══════════════════════════════════════════════════════════════════════════════
    #  TAB 2: SNIPER ALERTS
    # ═══════════════════════════════════════════════════════════════════════════════
    tab_alerts = tk.Frame(nb, bg="#080810")
    nb.add(tab_alerts, text="  🚨 ALERTS  ")
    alert_txt = scrolledtext.ScrolledText(tab_alerts, bg="#060610",
        fg="#00ff88", font=mono_lg, selectbackground="#1a2a4a", wrap=tk.WORD)
    alert_txt.pack(fill="both", expand=True, padx=4, pady=4)
    
    
    # ═══════════════════════════════════════════════════════════════════════════════
    #  TAB 3: OPEN POSITIONS
    # ═══════════════════════════════════════════════════════════════════════════════
    tab_positions = tk.Frame(nb, bg="#080810")
    nb.add(tab_positions, text="  📋 POSITIONS  ")
    
    pos_hdr = tk.Frame(tab_positions, bg="#001820", pady=4)
    pos_hdr.pack(fill="x", padx=4, pady=(4,0))
    tk.Label(pos_hdr, text="🤖  OPEN POSITIONS  (auto-trading active — following whale exits)",
             fg="#00aaff", bg="#001820", font=bold_hd).pack(side="left", padx=8)
    tk.Label(pos_hdr,
             text=f"Exits: whale sells → immediate | +{_cfg.get("PROFIT_TARGET_PCT", 0.20)*100:.0f}% target | "
                  f"{_cfg.get("MIN_HOLD_MINUTES", 15)}min hold guard | {_cfg.get("EXIT_COOLDOWN_SECONDS", 300)//60}min cooldown",
             fg="#334455", bg="#001820", font=mono_sm).pack(side="left", padx=4)
    
    pos_cols = ("Market","Side","WEntry$","Entry$","Now$","P&L%","P&L$","Bet$","Hold","Bought From","Score","Status")
    pos_tree = ttk.Treeview(tab_positions, columns=pos_cols, show="headings", height=9)
    pw = {"Market":300,"Side":100,"WEntry$":72,"Entry$":72,"Now$":72,
          "P&L%":70,"P&L$":72,"Bet$":60,"Hold":50,"Bought From":180,"Score":55,"Status":120}
    for c in pos_cols:
        pos_tree.heading(c, text=c)
        pos_tree.column(c, width=pw[c], anchor="w" if c in ("Market","Bought From") else "center")
    pos_tree.tag_configure("PROFIT",  foreground="#00ff55", background="#001800")
    pos_tree.tag_configure("LOSS",    foreground="#ff5555", background="#1a0000")
    pos_tree.tag_configure("HOLD",    foreground="#ffaa00", background="#1a1400")
    pos_tree.tag_configure("NEUTRAL", foreground="#aaaaaa", background="#0c0c18")
    pos_tree.tag_configure("CLOSED_WIN",  foreground="#007733", background="#000f00")
    pos_tree.tag_configure("CLOSED_LOSS", foreground="#882222", background="#0f0000")
    
    pos_vsb = tk.Scrollbar(tab_positions, command=pos_tree.yview)
    pos_tree.configure(yscrollcommand=pos_vsb.set)
    pos_vsb.pack(side="right", fill="y")
    pos_tree.pack(fill="x", padx=4, pady=(4,2))
    
    
    def show_position_detail(key, pos):
        """Show a floating detail popup for an open position."""
        import time as _t
        win = tk.Toplevel(root)
        win.title(f"Position Detail — {pos.get('title','')[:50]}")
        win.configure(bg="#060615")
        win.geometry("820x640")
        win.resizable(True, True)
    
        mono10  = font.Font(family="Courier", size=10)
        mono9   = font.Font(family="Courier", size=9)
        bold11  = font.Font(family="Courier", size=11, weight="bold")
        bold9   = font.Font(family="Courier", size=9, weight="bold")
    
        entry    = pos.get("entry_price", 0)
        w_entry  = pos.get("avg_entry", entry)
        cur      = pos.get("cur_price", entry)
        shares   = pos.get("shares", 0)
        bet      = pos.get("bet", 0)
        pnl_pct  = (cur - entry) / max(entry, 0.001) * 100
        pnl_usd  = (cur - entry) * shares
        hold_min = (_t.time() - pos.get("entry_ts", _t.time())) / 60
        title    = pos.get("title", "")
        outcome  = pos.get("outcome", key[1] if isinstance(key, tuple) else "")
        cid      = pos.get("cid", key[0] if isinstance(key, tuple) else "")
        slug     = pos.get("slug", "") or pos.get("event_slug", "")
        entry_ts = pos.get("entry_ts")
        entry_ts_text = str(pos.get("entry_ts_str", "") or "")
        if not entry_ts_text and isinstance(entry_ts, (int, float)) and float(entry_ts) > 0:
            entry_ts_text = datetime.fromtimestamp(float(entry_ts)).strftime("%Y-%m-%d %H:%M:%S")
    
        # Header
        hf = tk.Frame(win, bg="#0a0a20", pady=8)
        hf.pack(fill="x", padx=8, pady=(8,0))
        pnl_color = "#00ff55" if pnl_pct >= 0 else "#ff5555"
        tier_icon = "💎" if pos.get("is_conviction") else ("⚡" if pos.get("is_hft") else "")
        tk.Label(hf, text=f"{tier_icon}[{pos.get('tier','?')}]  {title}",
                 fg="#00aaff", bg="#0a0a20", font=bold11, wraplength=780, justify="left").pack(anchor="w", padx=12)
        if entry_ts_text:
            tk.Label(hf, text=f"ENTRY TIME  {entry_ts_text}",
                     fg="#ffdd44", bg="#0a0a20", font=bold11, wraplength=780, justify="left").pack(anchor="w", padx=12, pady=(2,0))
        tk.Label(hf, text=f"Side: {outcome}   Score: {pos.get('score',0):.0f}pts   CID: {cid[:30]}…",
                 fg="#556677", bg="#0a0a20", font=mono9).pack(anchor="w", padx=12)
    
        # Stats grid
        sf2 = tk.Frame(win, bg="#060615")
        sf2.pack(fill="x", padx=8, pady=6)
    
        def stat_cell(parent, label, value, color="#aaaacc", col=0, row=0):
            f = tk.Frame(parent, bg="#0d0d20", bd=1, relief="solid")
            f.grid(row=row, column=col, padx=4, pady=3, sticky="nsew")
            tk.Label(f, text=label, fg="#445566", bg="#0d0d20", font=mono9, pady=2).pack()
            tk.Label(f, text=value, fg=color, bg="#0d0d20", font=bold9, pady=2).pack()
    
        stats_data = [
            ("Whale Entry",   f"${w_entry:.4f}",          "#ffaa44"),
            ("Our Entry",     f"${entry:.4f}",             "#aaaaff"),
            ("Current Price", f"${cur:.4f}",               pnl_color),
            ("P&L $",         f"${pnl_usd:+.4f}",          pnl_color),
            ("P&L %",         f"{pnl_pct:+.2f}%",          pnl_color),
            ("Bet Size",      f"${bet:.2f}",               "#00aaff"),
            ("Shares",        f"{shares:.2f}",             "#aaaacc"),
            ("Held",          f"{hold_min:.0f} min",       "#888888"),
            ("Liq",           f"${pos.get('liq',0):,.0f}", "#446688"),
            ("Score",         f"{pos.get('score',0):.0f}", "#ffdd44"),
            ("Tier",          pos.get('tier','?'),          "#ff8844"),
            ("Type",          "HFT⚡" if pos.get('is_hft') else ("💎CONVICTION" if pos.get('is_conviction') else "STANDARD"), "#aaaacc"),
        ]
        for i, (lbl, val, col) in enumerate(stats_data):
            sf2.columnconfigure(i % 4, weight=1)
            stat_cell(sf2, lbl, val, col, i % 4, i // 4)
    
        # Whales
        wf = tk.Frame(win, bg="#060615")
        wf.pack(fill="x", padx=8)
        tk.Label(wf, text="WHALE WALLETS", fg="#00ff88", bg="#060615", font=bold9).pack(anchor="w", padx=4, pady=(4,2))
        seen_wallets: set[str] = set()
        elite_wallets: list[str] = []
        for wallet_addr in list(pos.get("elite_wallets", [])) + list(pos.get("whale_wallets", [])):
            wallet_key = str(wallet_addr).lower()
            if wallet_key in seen_wallets:
                continue
            seen_wallets.add(wallet_key)
            elite_wallets.append(str(wallet_addr))
        elite_names   = pos.get("elite_names", [])
        for i, w_addr in enumerate(elite_wallets[:8]):
            name  = (elite_names[i] if i < len(elite_names) else None) or _wallet_cache().get(w_addr, {}).get("name", w_addr[:16]+"…")
            prof  = _wallet_cache().get(w_addr, {})
            hft_t = "⚡" if prof.get("hft") else ""
            wr    = prof.get("win_rate", 0) * 100
            pnl_w = prof.get("total_pnl", 0)
            tk.Label(wf, text=f"  {hft_t}{name:<22} WR:{wr:.0f}%  PnL:${pnl_w:+,.0f}  Score:{prof.get('score',0):.2f}",
                     fg="#00cc88", bg="#060615", font=mono9).pack(anchor="w", padx=12)
    
        # Links
        lf = tk.Frame(win, bg="#060615")
        lf.pack(fill="x", padx=8, pady=6)
    
        market_url = f"https://polymarket.com/event/{slug}" if slug else "https://polymarket.com"
    
        def open_polymarket():
            webbrowser.open(market_url)
    
        def copy_title():
            try:
                win.clipboard_clear()
                win.clipboard_append(title)
                win.update()
            except Exception as e:
                _log_ui_error("copy position title", e, "WARN")

        def inspect_raw_data() -> None:
            show_raw_data_popup(pos, title=f"Position Raw Data - {title}")

        def open_properties() -> None:
            show_properties_popup(pos, title=f"Position Properties - {title}", subtitle="Double-click nested rows to inspect them.")
    
        tk.Button(lf, text="🌐 Open on Polymarket", bg="#0a1a3a", fg="#00aaff",
                  font=mono9, padx=10, command=open_polymarket).pack(side="left", padx=4)
        tk.Button(lf, text="📋 Copy Title", bg="#1a2a1a", fg="#00ff88",
                  font=mono9, padx=10, command=copy_title).pack(side="left", padx=4)
        tk.Button(lf, text="🔎 Inspect Raw", bg="#201a2a", fg="#d0b0ff",
                  font=mono9, padx=10, command=inspect_raw_data).pack(side="left", padx=4)
        tk.Button(lf, text="🧩 Properties", bg="#2a2012", fg="#ffcc88",
                  font=mono9, padx=10, command=open_properties).pack(side="left", padx=4)
    
        url_lbl = tk.Label(lf, text=market_url[:80], fg="#334455", bg="#060615", font=mono9)
        url_lbl.pack(side="left", padx=8)
    
        # Mini price chart
        cf = tk.Frame(win, bg="#060615")
        cf.pack(fill="both", expand=True, padx=8, pady=(4,8))
        tk.Label(cf, text="PRICE HISTORY", fg="#00ff88", bg="#060615", font=bold9).pack(anchor="w", padx=4)
        chart_canvas = tk.Canvas(cf, bg="#050510", highlightthickness=1,
                                  highlightbackground="#1a2a4a", height=160)
        chart_canvas.pack(fill="both", expand=True, padx=4, pady=4)
    
        def draw_detail_chart():
            chart_canvas.delete("all")
            w = chart_canvas.winfo_width()
            h = chart_canvas.winfo_height()
            if w < 20 or h < 20:
                return
            ph = pos.get("price_history", [])
            if len(ph) < 2:
                chart_canvas.create_text(w//2, h//2, text="Insufficient price history",
                                         fill="#334455", font=mono9)
                return
            prices = [p for _, p in ph]
            px, py = 50, 20
            cw = w - px - 10
            ch = h - py - 25
            mn = min(prices + [entry])
            mx = max(prices + [entry])
            spread = max(mx - mn, 0.002)
            mn -= spread * 0.1; mx += spread * 0.1
            def tx(i): return px + (i / max(len(prices)-1, 1)) * cw
            def ty(v): return py + (1 - (v - mn) / (mx - mn)) * ch
            # Grid
            for i in range(5):
                v = mn + (mx - mn) * i / 4
                y = ty(v)
                chart_canvas.create_line(px, y, w-10, y, fill="#0d1020", dash=(2,4))
                chart_canvas.create_text(px-4, y, text=f"${v:.3f}", fill="#334455",
                                         font=("Courier", 7), anchor="e")
            # Entry line
            ey = ty(entry)
            chart_canvas.create_line(px, ey, w-10, ey, fill="#665500", dash=(4,4))
            chart_canvas.create_text(w-8, ey, text="ENTRY", fill="#998833",
                                     font=("Courier", 7), anchor="e")
            # Price line
            coords = []
            for i, p in enumerate(prices):
                coords += [tx(i), ty(p)]
            if len(coords) >= 4:
                last_c = "#00ff55" if prices[-1] >= entry else "#ff5555"
                chart_canvas.create_line(coords, fill=last_c, width=2, smooth=len(prices)>=8)
            # End dot
            xe = tx(len(prices)-1); ye = ty(prices[-1])
            dot_c = "#00ff55" if prices[-1] >= entry else "#ff5555"
            chart_canvas.create_oval(xe-4, ye-4, xe+4, ye+4, fill=dot_c, outline="#ffffff", width=1)
            chart_canvas.create_text(xe+6, ye, text=f"${prices[-1]:.4f}",
                                     fill=dot_c, font=("Courier", 8), anchor="w")
            n_pts = len(prices)
            chart_canvas.create_text(px, h-8, text=f"{n_pts} price points", fill="#334455",
                                     font=("Courier", 7), anchor="w")
    
        win.after(200, draw_detail_chart)
        chart_canvas.bind("<Configure>", lambda e: win.after(50, draw_detail_chart))
    
    
    def show_trade_history_detail(trade):
        """Show a floating detail popup for a closed trade."""
        win = tk.Toplevel(root)
        typ = trade.get("type", "?")
        win.title(f"Trade Detail — {trade.get('title','')[:50]}")
        win.configure(bg="#060615")
        win.geometry("760x500")
    
        mono9  = font.Font(family="Courier", size=9)
        bold9  = font.Font(family="Courier", size=9, weight="bold")
        bold11 = font.Font(family="Courier", size=11, weight="bold")
    
        pnl_u = trade.get("pnl_usdc") or 0
        pnl_p = trade.get("pnl_pct")  or 0
        pnl_color = "#00ff55" if pnl_u >= 0 else "#ff5555"
        title = trade.get("title", "Unknown")
        slug  = trade.get("slug", "") or trade.get("event_slug", "")
    
        hf = tk.Frame(win, bg="#0a0a20", pady=8)
        hf.pack(fill="x", padx=8, pady=(8,0))
        icon = "🛒" if typ == "BUY" else ("✅" if pnl_u >= 0 else "❌")
        tk.Label(hf, text=f"{icon} [{typ}] [{trade.get('tier','?')}]  {title}",
                 fg="#00aaff" if typ == "BUY" else pnl_color, bg="#0a0a20", font=bold11,
                 wraplength=730, justify="left").pack(anchor="w", padx=12)
        tk.Label(hf, text=f"Outcome: {trade.get('outcome','')}   Time: {trade.get('ts_str','')}",
                 fg="#556677", bg="#0a0a20", font=mono9).pack(anchor="w", padx=12)
    
        sf2 = tk.Frame(win, bg="#060615")
        sf2.pack(fill="x", padx=8, pady=6)
    
        def stat_cell(parent, label, value, color="#aaaacc", col=0, row=0):
            f = tk.Frame(parent, bg="#0d0d20", bd=1, relief="solid")
            f.grid(row=row, column=col, padx=4, pady=3, sticky="nsew")
            tk.Label(f, text=label, fg="#445566", bg="#0d0d20", font=mono9, pady=2).pack()
            tk.Label(f, text=value, fg=color, bg="#0d0d20", font=bold9, pady=2).pack()
    
        w_entry = trade.get("avg_entry", trade.get("entry_price", 0))
        entry_p = trade.get("entry_price", 0)
        exit_p  = trade.get("exit_price", 0)
        stats_data = [
            ("Whale Entry",  f"${w_entry:.4f}",                          "#ffaa44"),
            ("Our Entry",    f"${entry_p:.4f}",                          "#aaaaff"),
            ("Exit Price",   f"${exit_p:.4f}" if exit_p else "—",        pnl_color if typ=="SELL" else "#888888"),
            ("P&L $",        f"${pnl_u:+.4f}" if typ=="SELL" else "—",   pnl_color),
            ("P&L %",        f"{pnl_p:+.1f}%" if typ=="SELL" else "—",   pnl_color),
            ("Bet Size",     f"${trade.get('bet',0):.2f}",               "#00aaff"),
            ("Tier",         trade.get("tier","?"),                      "#ff8844"),
            ("Bankroll @",   f"${trade.get('bankroll',0):.3f}",          "#778899"),
        ]
        for i, (lbl, val, col) in enumerate(stats_data):
            sf2.columnconfigure(i % 4, weight=1)
            stat_cell(sf2, lbl, val, col, i % 4, i // 4)
    
        # Whales — show name + how much each whale put into this trade
        wf = tk.Frame(win, bg="#060615")
        wf.pack(fill="x", padx=8)
        whale_names = trade.get("whale_names", [])
        whale_addrs = trade.get("elite_wallets", [])
        whale_cash  = trade.get("whale_buy_cash", {})  # addr → $ amount
        if whale_names or whale_addrs:
            tk.Label(wf, text="VIA WHALES:", fg="#00ff88", bg="#060615", font=mono9).pack(anchor="w", padx=12, pady=(4,2))
            for i, name in enumerate(whale_names[:6]):
                addr = whale_addrs[i] if i < len(whale_addrs) else ""
                cash_val = whale_cash.get(addr.lower(), whale_cash.get(addr, 0))
                cash_str = f"  —  put in ${cash_val:,.0f}" if cash_val > 0 else ""
                prof = _wallet_cache().get(addr.lower(), {})
                wr   = prof.get("win_rate", 0) * 100
                pnl_w = prof.get("total_pnl", 0)
                detail = f"WR:{wr:.0f}%  PnL:${pnl_w:+,.0f}" if wr or pnl_w else ""
                tk.Label(wf,
                         text=f"  🐋 {name:<22}{cash_str}   {detail}",
                         fg="#00cc88", bg="#060615", font=mono9).pack(anchor="w", padx=16)
    
        # Exit reason
        reason = trade.get("reason", "")
        if reason:
            tk.Label(win, text=f"Exit reason: {reason}",
                     fg="#ffaa44", bg="#060615", font=mono9).pack(anchor="w", padx=20)
    
        # Links
        lf = tk.Frame(win, bg="#060615")
        lf.pack(fill="x", padx=8, pady=8)
        market_url = f"https://polymarket.com/event/{slug}" if slug else "https://polymarket.com"
    
        def open_polymarket():
            webbrowser.open(market_url)
    
        def copy_title():
            try:
                win.clipboard_clear()
                win.clipboard_append(title)
                win.update()
            except Exception as e:
                _log_ui_error("copy trade title", e, "WARN")

        def inspect_raw_data() -> None:
            show_raw_data_popup(trade, title=f"Trade Raw Data - {title}")

        def open_properties() -> None:
            show_properties_popup(trade, title=f"Trade Properties - {title}", subtitle="Double-click nested rows to inspect them.")
    
        tk.Button(lf, text="🌐 Open on Polymarket", bg="#0a1a3a", fg="#00aaff",
                  font=mono9, padx=10, command=open_polymarket).pack(side="left", padx=4)
        tk.Button(lf, text="📋 Copy Title", bg="#1a2a1a", fg="#00ff88",
                  font=mono9, padx=10, command=copy_title).pack(side="left", padx=4)
        tk.Button(lf, text="🔎 Inspect Raw", bg="#201a2a", fg="#d0b0ff",
                  font=mono9, padx=10, command=inspect_raw_data).pack(side="left", padx=4)
        tk.Button(lf, text="🧩 Properties", bg="#2a2012", fg="#ffcc88",
                  font=mono9, padx=10, command=open_properties).pack(side="left", padx=4)
    
    
    def show_whale_detail(wallet: str, whale: dict[str, object]) -> None:
        win = tk.Toplevel(root)
        whale_name = str(whale.get("name", wallet[:16] + "…"))
        win.title(f"Whale Detail — {whale_name[:50]}")
        win.configure(bg="#060615")
        win.geometry("760x560")
        win.resizable(True, True)

        mono10 = font.Font(family="Courier", size=10)
        mono9 = font.Font(family="Courier", size=9)
        bold9 = font.Font(family="Courier", size=9, weight="bold")
        bold11 = font.Font(family="Courier", size=11, weight="bold")

        score = float(whale.get("score", 0.0) or 0.0)
        total_pnl = float(whale.get("total_pnl", 0.0) or 0.0)
        pnl_color = "#00ff55" if total_pnl >= 0 else "#ff5555"
        verified = bool(whale.get("verified"))
        elite = bool(whale.get("elite"))
        hft = bool(whale.get("hft"))
        icon = "🔥" if elite else ("✅" if verified else "👁")

        hf = tk.Frame(win, bg="#0a0a20", pady=8)
        hf.pack(fill="x", padx=8, pady=(8, 0))
        tk.Label(hf, text=f"{icon} {whale_name}{' ⚡HFT' if hft else ''}",
                 fg="#00aaff" if verified or elite else "#aaaaaa", bg="#0a0a20",
                 font=bold11, wraplength=730, justify="left").pack(anchor="w", padx=12)
        tk.Label(hf, text=f"Wallet: {wallet}",
                 fg="#556677", bg="#0a0a20", font=mono9, wraplength=730,
                 justify="left").pack(anchor="w", padx=12)

        sf2 = tk.Frame(win, bg="#060615")
        sf2.pack(fill="x", padx=8, pady=6)

        def stat_cell(parent: tk.Misc, label: str, value: str, color: str = "#aaaacc", col: int = 0, row: int = 0) -> None:
            f = tk.Frame(parent, bg="#0d0d20", bd=1, relief="solid")
            f.grid(row=row, column=col, padx=4, pady=3, sticky="nsew")
            tk.Label(f, text=label, fg="#445566", bg="#0d0d20", font=mono9, pady=2).pack()
            tk.Label(f, text=value, fg=color, bg="#0d0d20", font=bold9, pady=2).pack()

        stats_data = [
            ("Score", f"{score:.2f}", "#ffdd44"),
            ("Win Rate", f"{float(whale.get('win_rate', 0.0) or 0.0) * 100:.0f}%", "#00ff88"),
            ("Wilson LB", f"{float(whale.get('wilson_lb', 0.0) or 0.0) * 100:.0f}%", "#88ccff"),
            ("Resolved", f"{int(whale.get('n_resolved', 0) or 0)}", "#aaaacc"),
            ("Portfolio", f"${float(whale.get('total_value', 0.0) or 0.0):,.0f}", "#00aaff"),
            ("PnL", f"${total_pnl:+,.0f}", pnl_color),
            ("Avg Bet", f"${float(whale.get('avg_bet', 0.0) or 0.0):,.0f}", "#ffaa44"),
            ("TPH", f"{float(whale.get('trades_per_hour', 0.0) or 0.0):.1f}", "#aaaacc"),
            ("7d PnL", f"${float(whale.get('recent_pnl_7d', 0.0) or 0.0):+,.0f}", "#88ccff"),
            ("30d PnL", f"${float(whale.get('recent_pnl_30d', 0.0) or 0.0):+,.0f}", "#88ccff"),
            ("Status", "ELITE" if elite else ("VERIFIED" if verified else "WATCH / REJECT"), "#ff8844"),
            ("Type", "HFT" if hft else "STANDARD", "#aaaacc"),
        ]
        for i, (lbl, val, col) in enumerate(stats_data):
            sf2.columnconfigure(i % 4, weight=1)
            stat_cell(sf2, lbl, val, col, i % 4, i // 4)

        info_f = tk.Frame(win, bg="#060615")
        info_f.pack(fill="x", padx=8, pady=(0, 6))
        tk.Label(info_f, text="DETAILS", fg="#00ff88", bg="#060615", font=bold9).pack(anchor="w", padx=4, pady=(4, 2))
        detail_lines = [
            f"  Verified: {'yes' if verified else 'no'}",
            f"  Elite: {'yes' if elite else 'no'}",
            f"  HFT: {'yes' if hft else 'no'}",
            f"  Watchable: {'yes' if bool(whale.get('watchable')) else 'no'}",
        ]
        fail_reasons = whale.get("fail_reasons")
        if isinstance(fail_reasons, list) and fail_reasons:
            detail_lines.append(f"  Fail reasons: {', '.join(str(x) for x in fail_reasons[:8])}")
        for line in detail_lines:
            tk.Label(info_f, text=line, fg="#cccccc", bg="#060615", font=mono10,
                     anchor="w", justify="left", wraplength=720).pack(anchor="w", padx=12)

        lf = tk.Frame(win, bg="#060615")
        lf.pack(fill="x", padx=8, pady=6)
        profile_wallet = str(whale.get("wallet", wallet))
        profile_url = f"https://polymarket.com/profile/{profile_wallet}"

        def copy_wallet() -> None:
            try:
                win.clipboard_clear()
                win.clipboard_append(wallet)
                win.update()
            except Exception as e:
                _log_ui_error("copy whale wallet", e, "WARN")

        def open_polymarket_profile() -> None:
            webbrowser.open(profile_url)

        def inspect_raw_data() -> None:
            show_raw_data_popup({"wallet": wallet, **whale}, title=f"Whale Raw Data - {whale_name}")

        def open_properties() -> None:
            show_properties_popup({"wallet": wallet, **whale},
                                  title=f"Whale Properties - {whale_name}",
                                  subtitle="Double-click nested rows to inspect them.")

        tk.Button(lf, text="🌐 Open in Polymarket", bg="#0a1a3a", fg="#00aaff",
                  font=mono9, padx=10, command=open_polymarket_profile).pack(side="left", padx=4)
        tk.Button(lf, text="📋 Copy Wallet", bg="#1a2a1a", fg="#00ff88",
                  font=mono9, padx=10, command=copy_wallet).pack(side="left", padx=4)
        tk.Button(lf, text="🔎 Inspect Raw", bg="#201a2a", fg="#d0b0ff",
                  font=mono9, padx=10, command=inspect_raw_data).pack(side="left", padx=4)
        tk.Button(lf, text="🧩 Properties", bg="#2a2012", fg="#ffcc88",
                  font=mono9, padx=10, command=open_properties).pack(side="left", padx=4)
    
    
    def _clean_tree_market_title(value: object) -> str:
        raw = str(value).strip()
        parts = raw.split(" ", 1)
        if len(parts) == 2 and parts[0] and not parts[0][0].isalnum():
            return parts[1].strip()
        return raw

    def _closed_position_selection_key(pos: TradeRecordDict) -> tuple[str, str, str, str]:
        return (
            str(pos.get("cid") or ""),
            str(pos.get("title") or ""),
            str(pos.get("outcome") or ""),
            str(pos.get("exit_ts") or pos.get("ts") or ""),
        )

    _closed_tree_items: dict[str, TradeRecordDict] = {}

    def _load_selected_position_chart() -> None:
        sel = pos_tree.selection()
        if not sel:
            pos_graph.load([], "", 0.0)
            return

        vals = pos_tree.item(sel[0])["values"]
        if not vals:
            pos_graph.load([], "", 0.0)
            return

        mkt_name = str(vals[0]).replace("ðŸ’Ž", "").replace("âš¡", "")
        mkt_name = _clean_tree_market_title(vals[0])
        outcome = str(vals[1])

        if _show_closed[0]:
            pos = _find_selected_closed_position()
            if pos is None:
                msg = f"Closed position match not found for {mkt_name[:48]} [{outcome}]."
                pos_graph.load([], mkt_name, 0.0, msg)
                if _last_chart_warn[0] != msg:
                    pos_log_write(msg, "WARN")
                    _last_chart_warn[0] = msg
                return

            history = pos.get("price_history", [])
            entry_price = float(pos.get("entry_price") or 0.0)
            title = str(pos.get("title", mkt_name))
            if history:
                _last_chart_warn[0] = ""
                pos_graph.load(history, title, entry_price, entry_ts=float(pos.get("entry_ts") or 0) or None)
                pos_chart_frame.refresh_panel(reset=True)
                return

            detail = str(pos.get("price_history_error") or "Closed position has no chart history.")
            pos_graph.load([], title, entry_price, detail, entry_ts=float(pos.get("entry_ts") or 0) or None)
            pos_chart_frame.refresh_panel(reset=True)
            warn_msg = f"Closed chart empty: {title[:80]} [{outcome}] | {detail}"
            if _last_chart_warn[0] != warn_msg:
                pos_log_write(warn_msg, "WARN")
                _last_chart_warn[0] = warn_msg
            return

        for _, pos in _open_pos_dict().items():
            title = str(pos.get("title", ""))
            if title[:48] in mkt_name or mkt_name[:30] in title:
                if str(pos.get("outcome", "")) == outcome:
                    _last_chart_warn[0] = ""
                    pos_graph.load(pos.get("price_history", []), title, pos.get("entry_price", 0), entry_ts=float(pos.get("entry_ts") or 0) or None)
                    pos_chart_frame.refresh_panel(reset=True)
                    return

        _last_chart_warn[0] = ""
        pos_graph.load([], mkt_name, 0.0, f"Open position match not found for {mkt_name[:48]} [{outcome}].")
        pos_chart_frame.refresh_panel(reset=True)

    def _find_selected_closed_position() -> TradeRecordDict | None:
        sel = pos_tree.selection()
        if not sel:
            return None
        return _closed_tree_items.get(str(sel[0]))

    def _on_pos_double_click(event):
        sel = pos_tree.selection()
        if not sel:
            return
        vals = pos_tree.item(sel[0])["values"]
        if not vals:
            return
        mkt_name = _clean_tree_market_title(vals[0])
        outcome = str(vals[1])
        if _show_closed[0]:
            pos = _find_selected_closed_position()
            if pos:
                show_trade_history_detail(pos)
            return
        for key, pos in _open_pos_dict().items():
            title_cmp = pos.get("title", "")
            if title_cmp[:48] in mkt_name or mkt_name[:30] in title_cmp:
                if pos.get("outcome", "") == outcome or outcome in pos.get("outcome", ""):
                    show_position_detail(key, pos)
                    return
        for key, pos in _open_pos_dict().items():
            if mkt_name[:20] in pos.get("title", ""):
                show_position_detail(key, pos)
                return

    pos_tree.bind("<Double-1>", _on_pos_double_click)
    pos_tree.bind("<<TreeviewSelect>>", lambda event: _load_selected_position_chart())
    
    pos_split = tk.Frame(tab_positions, bg="#080810")
    pos_split.pack(fill="both", expand=True, padx=4)
    
    def _pos_chart_data_rows():
        from datetime import datetime as _dt
        hist = pos_graph._history
        if not hist:
            return []
        entry = pos_graph._baseline_value
        return [
            (_dt.fromtimestamp(ts).strftime("%m/%d %H:%M"), f"${v:.4f}  ({(v-entry)/max(entry,0.001)*100:+.1f}%)")
            for ts, v in hist
        ]

    pos_chart_frame = ChartFrame(pos_split, get_data_rows=_pos_chart_data_rows,
                                 col_headers=("Time", "Price / P&L"))
    pos_chart_frame.pack(side="left", fill="both", expand=True)

    pos_graph = PositionChart(pos_chart_frame.chart_area, height=240)
    pos_graph.pack(fill="both", expand=True, padx=2, pady=2)
    _last_chart_warn = [""]

    pos_btn_bar = pos_chart_frame.btn_bar
    
    def _open_selected_market():
        sel = pos_tree.selection()
        if sel:
            vals = pos_tree.item(sel[0])['values']
            if vals:
                mkt_name = str(vals[0]).replace('💎', '').replace('⚡', '')
                outcome  = str(vals[1])
                for key, pos in _open_pos_dict().items():
                    title_cmp = pos.get('title', '')
                    if title_cmp[:48] in mkt_name or mkt_name[:30] in title_cmp:
                        slug = pos.get('slug', '') or pos.get('event_slug', '')
                        if slug:
                            webbrowser.open(f"https://polymarket.com/event/{slug}")
                            return
        webbrowser.open("https://polymarket.com")
    
    def _copy_selected_title():
        sel = pos_tree.selection()
        if sel:
            vals = pos_tree.item(sel[0])['values']
            if vals:
                mkt_name = str(vals[0]).replace('💎', '').replace('⚡', '')
                for key, pos in _open_pos_dict().items():
                    if pos.get('title', '')[:48] in mkt_name or mkt_name[:30] in pos.get('title', ''):
                        try:
                            root.clipboard_clear()
                            root.clipboard_append(pos.get('title', mkt_name))
                            root.update()
                        except Exception as e:
                            _log_ui_error("copy selected position title", e, "WARN")
                        return
    
    tk.Button(pos_btn_bar, text="🌐 POLYMARKET", bg="#0a1a3a", fg="#00aaff",
              font=mono_sm, command=_open_selected_market).pack(side="left", padx=4, pady=4)
    tk.Button(pos_btn_bar, text="📋 COPY TITLE", bg="#1a2a1a", fg="#00ff88",
              font=mono_sm, command=_copy_selected_title).pack(side="left", padx=4, pady=4)

    _closed_btn_var = tk.StringVar(value="📜 SHOW CLOSED")
    def _toggle_closed():
        _show_closed[0] = not _show_closed[0]
        _closed_btn_var.set("📂 SHOW OPEN" if _show_closed[0] else "📜 SHOW CLOSED")
        render_open_positions()
    tk.Button(pos_btn_bar, textvariable=_closed_btn_var, bg="#1a1a00", fg="#ffcc44",
              font=mono_sm, command=_toggle_closed).pack(side="left", padx=4, pady=4)

    tk.Label(pos_btn_bar, text="Double-click for detail", fg="#334455",
             bg="#080810", font=mono_sm).pack(side="left", padx=8)
    
    pos_log_frame = tk.Frame(pos_split, bg="#080810", width=380)
    pos_log_frame.pack(side="right", fill="both", expand=False)
    pos_log_frame.pack_propagate(False)
    
    pos_log = tk.Text(pos_log_frame, bg="#050508", fg="#cccccc", font=mono,
                      selectbackground="#1a2a4a", wrap="word")
    pos_sb  = tk.Scrollbar(pos_log_frame, command=pos_log.yview, bg="#0d0d1a")
    pos_log.configure(yscrollcommand=pos_sb.set)
    pos_sb.pack(side="right", fill="y")
    pos_log.pack(fill="both", expand=True)
    
    pos_log.tag_configure("BUY",    foreground="#00ff88")
    pos_log.tag_configure("SELL_W", foreground="#00ff55")
    pos_log.tag_configure("SELL_L", foreground="#ff5555")
    pos_log.tag_configure("WARN",   foreground="#ffaa00")
    pos_log.tag_configure("INFO",   foreground="#aaaaaa")
    
    
    def pos_log_write(msg, tag="INFO"):
        try:
            pos_log.configure(state="normal")
            ts = datetime.now().strftime("%H:%M:%S")
            pos_log.insert(tk.END, f"[{ts}] {msg}\n", tag)
            pos_log.see(tk.END)
            pos_log.configure(state="disabled")
        except Exception:
            pass
    
    
    # ═══════════════════════════════════════════════════════════════════════════════
    #  TAB 4: P&L GRAPH
    # ═══════════════════════════════════════════════════════════════════════════════

    tab_pnl = tk.Frame(nb, bg="#080810")
    nb.add(tab_pnl, text="  📈 P&L  ")
    
    stats_frame = tk.Frame(tab_pnl, bg="#0d0d1a", pady=6)
    stats_frame.pack(fill="x", padx=4, pady=(4,0))
    
    stat_vars = {}
    stat_labels = [
        ("total_pnl",   "Total P&L",   "#00ff88"),
        ("session_pnl", "Session P&L", "#00aaff"),
        ("win_rate",    "Win Rate",     "#ffdd00"),
        ("avg_pnl",     "Avg Trade",   "#00ccaa"),
        ("best",        "Best Trade",  "#00ff55"),
        ("worst",       "Worst Trade", "#ff5555"),
        ("n_trades",    "Trades Done", "#aaaaaa"),
        ("bankroll",    "Bankroll",    "#00aaff"),
        ("expectancy",  "Expectancy",  "#ff8844"),
    ]
    for key, label, color in stat_labels:
        f = tk.Frame(stats_frame, bg="#0d0d1a")
        f.pack(side="left", padx=10)
        tk.Label(f, text=label, fg="#335544", bg="#0d0d1a", font=mono_sm).pack()
        sv = tk.StringVar(value="—")
        stat_vars[key] = sv
        tk.Label(f, textvariable=sv, fg=color, bg="#0d0d1a", font=bold_hd).pack()
    
    def _pnl_chart_data_rows():
        from datetime import datetime as _dt
        pnl_summary = api.get_pnl_summary()
        eq_hist      = pnl_summary["equity_history"]
        bankroll_start = pnl_summary["bankroll_start"]
        return [
            (_dt.fromtimestamp(ts).strftime("%m/%d %H:%M"), f"${v - bankroll_start:+.4f}")
            for ts, v in eq_hist
        ]

    pnl_chart_frame = ChartFrame(tab_pnl, get_data_rows=_pnl_chart_data_rows,
                                 col_headers=("Date", "P&L $"))
    pnl_chart_frame.pack(fill="both", expand=True, padx=8, pady=(4,0))

    pnl_graph = PnLChart(pnl_chart_frame.chart_area)
    pnl_graph.pack(fill="both", expand=True)

    hist_cols = ("Time","Type","Market","Side","WEntry$","Entry$","Exit$","P&L$","P&L%","Via","Bankroll$")
    hist_tree = ttk.Treeview(tab_pnl, columns=hist_cols, show="headings", height=7)
    hw = {"Time":65,"Type":48,"Market":240,"Side":90,"WEntry$":68,
          "Entry$":68,"Exit$":68,"P&L$":72,"P&L%":65,"Via":150,"Bankroll$":78}
    for c in hist_cols:
        hist_tree.heading(c, text=c)
        hist_tree.column(c, width=hw[c], anchor="w" if c in ("Market","Via") else "center")
    hist_tree.tag_configure("WIN",  foreground="#00ff55", background="#001800")
    hist_tree.tag_configure("LOSS", foreground="#ff5555", background="#1a0000")
    hist_tree.tag_configure("BUY",  foreground="#00aaff", background="#000d1a")
    
    hist_vsb = tk.Scrollbar(tab_pnl, command=hist_tree.yview)
    hist_tree.configure(yscrollcommand=hist_vsb.set)
    hist_vsb.pack(side="right", fill="y")
    hist_tree.pack(fill="x", padx=4, pady=(0,4))
    
    def _on_hist_double_click(event):
        sel = hist_tree.selection()
        if not sel:
            return
        vals = hist_tree.item(sel[0])['values']
        if not vals:
            return
        ts_str   = str(vals[0])
        mkt_name = str(vals[2]) if len(vals) > 2 else ""
        outcome  = str(vals[3]) if len(vals) > 3 else ""
        # Find matching trade in history
        for t in reversed(api.get_trade_history()[-500:]):
            if t.get('ts_str', '') == ts_str:
                show_trade_history_detail(t)
                return
        # Fallback: match by title+outcome
        for t in reversed(api.get_trade_history()[-500:]):
            if mkt_name[:20] in t.get('title', '') and t.get('outcome', '') == outcome:
                show_trade_history_detail(t)
                return
    
    hist_tree.bind("<Double-1>", _on_hist_double_click)

    def draw_pnl_graph():
        pnl_summary = api.get_pnl_summary()
        pnl_graph.load(pnl_summary["equity_history"], pnl_summary["bankroll_start"])
    
    
    def refresh_pnl_tab():
        st   = api.get_trade_stats()
        pnl  = api.get_pnl_summary()
        unrealised_pnl = sum(
            (pos.get("cur_price", pos.get("entry_price", 0)) - pos.get("entry_price", 0))
            * pos.get("shares", 0)
            for pos in _open_pos_dict().values()
        )
        realised_pnl = st["sum_pnl"]
        total_pnl    = realised_pnl + unrealised_pnl
        win_rate     = st["win_rate"] * 100
        avg_pnl      = total_pnl / max(st["sell_count"], 1)
        open_val     = sum(
            pos.get("cur_price", pos.get("entry_price", 0)) * pos.get("shares", 0)
            for pos in _open_pos_dict().values()
        )

        stat_vars["total_pnl"].set(f"${total_pnl:+.4f}  (R:{realised_pnl:+.2f} U:{unrealised_pnl:+.2f})")
        stat_vars["session_pnl"].set(f"${pnl['session_pnl']:+.4f}")
        stat_vars["win_rate"].set(f"{win_rate:.0f}%  ({st['win_count']}W/{st['loss_count']}L)")
        stat_vars["avg_pnl"].set(f"${avg_pnl:+.4f}")
        stat_vars["best"].set(f"${st['best']:+.4f}")
        stat_vars["worst"].set(f"${st['worst']:+.4f}")
        stat_vars["n_trades"].set(str(st["sell_count"]))
        stat_vars["bankroll"].set(f"${pnl['bankroll'] + open_val:.4f}")
        stat_vars["expectancy"].set(f"${st['expectancy']:+.4f}")

        history = api.get_trade_history()
        hist_tree.delete(*hist_tree.get_children())
        for t in reversed(history[-200:]):
            whale_str = ", ".join(t.get("whale_names", [])[:2]) or "—"
            w_entry   = f"${t.get('avg_entry', t.get('entry_price', 0)):.4f}"
            if t.get("type") == "BUY":
                hist_tree.insert("", "end", values=(
                    t.get("ts_str","—"), "BUY", t.get("title","")[:40], t.get("outcome",""),
                    w_entry, f"${t.get('entry_price',0):.4f}",
                    "—", "—", "—", whale_str, f"${t.get('bankroll',0):.3f}",
                ), tags=("BUY",))
            elif t.get("type") == "SELL":
                pnl_u = t.get("pnl_usdc") or 0
                pnl_p = t.get("pnl_pct")  or 0
                tag   = "WIN" if pnl_u >= 0 else "LOSS"
                hist_tree.insert("", "end", values=(
                    t.get("ts_str","—"), "SELL", t.get("title","")[:40], t.get("outcome",""),
                    w_entry, f"${t.get('entry_price',0):.4f}",
                    f"${t.get('exit_price',0):.4f}" if t.get("exit_price") else "—",
                    f"${pnl_u:+.4f}", f"{pnl_p:+.1f}%", whale_str,
                    f"${t.get('bankroll',0):.3f}",
                ), tags=(tag,))
    
        draw_pnl_graph()
        pnl_chart_frame.refresh_panel()

    
    # ═══════════════════════════════════════════════════════════════════════════════
    #  TAB 5: WHALE ROSTER
    # ═══════════════════════════════════════════════════════════════════════════════

    tab_whales = tk.Frame(nb, bg="#080810")
    nb.add(tab_whales, text="  🐳 WHALES  ")
    
    wh_header = tk.Frame(tab_whales, bg="#0d0d1a", pady=4)
    wh_header.pack(fill="x", padx=4, pady=(4,0))
    tk.Label(wh_header, text="WHALE ROSTER", fg="#00ff88", bg="#0d0d1a", font=bold_hd).pack(side="left", padx=8)
    wh_filter_var = tk.StringVar(value="ALL")
    for val, label in [("ALL","All"),("ELITE","🔥 Elite"),("VER","✅ Verified"),("HFT","⚡ HFT")]:
        tk.Radiobutton(wh_header, text=label, variable=wh_filter_var, value=val,
                       bg="#0d0d1a", fg="#aaaaaa", selectcolor="#0d0d1a",
                       activebackground="#0d0d1a", font=mono,
                       command=lambda: _pending_update.__setitem__(0, True)
                       ).pack(side="left", padx=4)
    
    wh_cols = ("Name","Wallet","Score","WinRate","WilsonLB","Res","Portfolio","PnL","AvgBet","TPH","Status","HFT")
    wh_tree = ttk.Treeview(tab_whales, columns=wh_cols, show="headings")
    ww = {"Name":130,"Wallet":180,"Score":58,"WinRate":65,"WilsonLB":72,
          "Res":50,"Portfolio":100,"PnL":90,"AvgBet":78,"TPH":55,"Status":80,"HFT":40}
    for c in wh_cols:
        wh_tree.heading(c, text=c)
        wh_tree.column(c, width=ww[c], anchor="center")
    wh_tree.tag_configure("ELITE", foreground="#00ff55", background="#001500")
    wh_tree.tag_configure("VER",   foreground="#ffdd00", background="#181400")
    wh_tree.tag_configure("PAR",   foreground="#55aaff", background="#000d1a")
    wh_tree.tag_configure("REJ",   foreground="#554444", background="#0c0c18")
    wh_vsb = tk.Scrollbar(tab_whales, command=wh_tree.yview)
    wh_tree.configure(yscrollcommand=wh_vsb.set)
    wh_vsb.pack(side="right", fill="y")
    wh_tree.pack(fill="both", expand=True, padx=4, pady=4)
    _whale_tree_items: dict[str, tuple[str, dict[str, object]]] = {}
    
    
    # ═══════════════════════════════════════════════════════════════════════════════
    #  TAB 6: ANALYSIS
    # ═══════════════════════════════════════════════════════════════════════════════
    tab_analysis = tk.Frame(nb, bg="#080810")
    nb.add(tab_analysis, text="  📊 ANALYSIS  ")
    analysis_txt = scrolledtext.ScrolledText(tab_analysis, bg="#060610",
        fg="#aaaacc", font=mono, selectbackground="#1a2a4a", wrap=tk.WORD)
    analysis_txt.pack(fill="both", expand=True, padx=4, pady=4)
    
    
    # ═══════════════════════════════════════════════════════════════════════════════
    #  TAB 7: DIAGNOSTICS
    # ═══════════════════════════════════════════════════════════════════════════════
    tab_diag = tk.Frame(nb, bg="#080810")
    nb.add(tab_diag, text="  🔍 DIAG  ")
    diag_txt = scrolledtext.ScrolledText(tab_diag, bg="#060610",
        fg="#889988", font=mono_sm, selectbackground="#1a2a4a", wrap=tk.WORD)
    diag_txt.pack(fill="both", expand=True, padx=4, pady=4)
    
    
    # ═══════════════════════════════════════════════════════════════════════════════
    #  LOGGING
    # ═══════════════════════════════════════════════════════════════════════════════
    LOG_COLORS = {
        "INFO":  "#00ff88",
        "DEBUG": "#ffdd44",
        "DATA":  "#44aaff",
        "WARN":  "#ffaa00",
        "ERR":   "#ff4444",
        "TRADE": "#00ffcc",
        "POS":   "#ff8844",
        "DIAG":  "#556655",
        "SIG":   "#ffdd44",
        "ALERT": "#00ff55",
    }
    for tag_name, color in LOG_COLORS.items():
        sig_log.tag_configure(tag_name, foreground=color)
    
    
    def log(msg, level="INFO"):
        try:
            if level == "DEBUG" and not _debug_mode[0]:
                return
            sig_log.configure(state="normal")
            ts  = datetime.now().strftime("%H:%M:%S")
            tag = level if level in LOG_COLORS else "INFO"
            sig_log.insert(tk.END, f"[{ts}] {msg}\n", tag)
            line_count = int(sig_log.index("end-1c").split(".")[0])
            if line_count > 3000:
                sig_log.delete("1.0", "600.0")
            sig_log.see(tk.END)
            sig_log.configure(state="disabled")
        except Exception:
            pass

    def _log_ui_error(context: str, error: Exception, level: str = "ERR") -> None:
        log(f"[{context}] {error}", level)
    
    
    # ═══════════════════════════════════════════════════════════════════════════════
    #  TAB 8: SYSTEM LOG
    # ═══════════════════════════════════════════════════════════════════════════════
    tab_log = tk.Frame(nb, bg="#080810")
    nb.add(tab_log, text="  📜 LOG  ")
    
    log_tool_bar = tk.Frame(tab_log, bg="#0d0d1a", pady=4)
    log_tool_bar.pack(fill="x")
    
    copy_btn_var = tk.StringVar(value="📋 COPY FULL SNAPSHOT FOR AI")
    
    from titan_api import TitanAPI as _TitanAPI
    _snapshot_api = _TitanAPI()
    
    
    def build_ai_debug_snapshot_compressed() -> str:
        _snapshot_api._last_signals = _last_signals
        _snapshot_api._last_rejects = _last_rejects
        return _snapshot_api._build_snapshot_compressed()
    
    
    def build_ai_debug_snapshot(compressed: bool = False) -> str:
        _snapshot_api._last_signals = _last_signals
        _snapshot_api._last_rejects = _last_rejects
        if compressed:
            return _snapshot_api._build_snapshot_compressed()
        return _snapshot_api._build_snapshot()
    
    
    def copy_all_logs():
        snapshot = build_ai_debug_snapshot()
        copied   = False
    
        # Try pyperclip first
        if HAS_PYPERCLIP:
            try:
                pyperclip.copy(snapshot)
                copied = True
            except Exception as e:
                _log_ui_error("copy debug snapshot via pyperclip", e, "WARN")
    
        # Fallback: tkinter clipboard
        if not copied:
            try:
                root.clipboard_clear()
                root.clipboard_append(snapshot)
                root.update()   # flush so the clipboard is actually set
                copied = True
            except Exception as e:
                _log_ui_error("copy debug snapshot via tkinter clipboard", e, "WARN")
    
        if copied:
            n_lines = snapshot.count("\n")
            log(f"📋 Full AI debug snapshot copied ({n_lines} lines)", "INFO")
            copy_btn_var.set("✅ COPIED!")
            root.after(2000, lambda: copy_btn_var.set("📋 COPY FULL SNAPSHOT FOR AI"))
        else:
            # Last resort: save to file and tell user
            save_snapshot_to_file()
            log("⚠ Clipboard unavailable — snapshot saved to file instead. Install pyperclip for clipboard support.", "WARN")
            copy_btn_var.set("💾 SAVED TO FILE (clipboard failed)")
            root.after(3000, lambda: copy_btn_var.set("📋 COPY FULL SNAPSHOT FOR AI"))
    
    
    def save_snapshot_to_file():
        snapshot = build_ai_debug_snapshot()
        log_dir  = "Logs"
        os.makedirs(log_dir, exist_ok=True)
        fname = os.path.join(log_dir, f"titan_snapshot_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt")
        try:
            with open(fname, "w", encoding="utf-8") as f:
                f.write(snapshot)
            log(f"📄 Snapshot saved to {fname}", "INFO")
        except Exception as e:
            log(f"⚠ Snapshot save failed: {e}", "ERR")
    
    
    tk.Button(log_tool_bar, textvariable=copy_btn_var, bg="#1a332a", fg="#00ff88",
              font=bold_hd, padx=12, command=copy_all_logs).pack(side="left", padx=10)
    tk.Button(log_tool_bar, text="💾 SAVE SNAPSHOT TO FILE", bg="#1a2a3a", fg="#00aaff",
              font=mono, padx=8, command=save_snapshot_to_file).pack(side="left", padx=4)
    tk.Label(log_tool_bar,
             text="Copies everything: positions · signals · elites · trades · exits · raw logs.",
             fg="#445566", bg="#0d0d1a", font=mono_sm).pack(side="left")
    
    full_log = scrolledtext.ScrolledText(tab_log, bg="#050508", fg="#66ffaa", font=mono_sm,
                                         selectbackground="#1a2a4a", wrap=tk.NONE)
    full_log.pack(fill="both", expand=True, padx=4, pady=4)
    
    
    # ═══════════════════════════════════════════════════════════════════════════════
    #  TAB 9: CONFIG EDITOR
    # ═══════════════════════════════════════════════════════════════════════════════
    import json as _json
    import importlib as _importlib
    

    tab_config = tk.Frame(nb, bg="#080810")
    nb.add(tab_config, text="  ⚙ CONFIG  ")
    
    cfg_toolbar = tk.Frame(tab_config, bg="#0d1a0d", pady=6)
    cfg_toolbar.pack(fill="x")
    
    cfg_status_var = tk.StringVar(value="  Loaded from repo-root titan_config.json")
    
    
    def _reload_config_from_json():
        try:
            import titan_config as _tc
            _tc.reload()
            cfg_status_var.set(f"  ✅ Reloaded at {datetime.now().strftime('%H:%M:%S')} — takes effect next cycle")
            log("⚙ Config hot-reloaded from repo-root titan_config.json", "INFO")
        except Exception as e:
            cfg_status_var.set(f"  ❌ Reload failed: {e}")
    
    
    def _save_config():
        raw = cfg_editor.get("1.0", tk.END).strip()
        try:
            parsed = _json.loads(raw)
        except _json.JSONDecodeError as e:
            cfg_status_var.set(f"  ❌ JSON syntax error: {e}")
            return
        try:
            import titan_config as _tc
            with open(_tc.get_config_file(), "w", encoding="utf-8") as f:
                _json.dump(parsed, f, indent=2)
            _reload_config_from_json()
            _load_config_into_editor()
        except Exception as e:
            cfg_status_var.set(f"  ❌ Save failed: {e}")


    def _load_config_into_editor():
        try:
            import titan_config as _tc
            fpath = _tc.get_config_file()
            with open(fpath, "r", encoding="utf-8") as f:
                raw = _json.load(f)
            pretty = _json.dumps(raw, indent=2)
            cfg_editor.configure(state="normal")
            cfg_editor.delete("1.0", tk.END)
            cfg_editor.insert("1.0", pretty)
            cfg_status_var.set(f"  Loaded from {fpath}")
            _highlight_json()
        except Exception as e:
            cfg_status_var.set(f"  ❌ Load failed: {e}")
    
    
    tk.Button(cfg_toolbar, text="💾 SAVE & RELOAD", bg="#002a00", fg="#00ff88",
              font=bold_hd, padx=14, command=_save_config).pack(side="left", padx=10)
    tk.Button(cfg_toolbar, text="↺ Discard", bg="#1a1a2a", fg="#778899",
              font=mono, padx=8, command=_load_config_into_editor).pack(side="left", padx=4)
    tk.Label(cfg_toolbar, textvariable=cfg_status_var,
             fg="#556677", bg="#0d1a0d", font=mono).pack(side="left", padx=10)
    
    cfg_body = tk.Frame(tab_config, bg="#080810")
    cfg_body.pack(fill="both", expand=True, padx=4, pady=4)
    
    cfg_editor_frame = tk.Frame(cfg_body, bg="#080810")
    cfg_editor_frame.pack(side="left", fill="both", expand=True)
    
    cfg_ref_frame = tk.Frame(cfg_body, bg="#0d0d1a", width=340)
    cfg_ref_frame.pack(side="right", fill="y", padx=(4,0))
    cfg_ref_frame.pack_propagate(False)
    
    tk.Label(cfg_ref_frame, text="TITAN GUIDE", fg="#00ff88", bg="#0d0d1a",
             font=bold_hd, pady=8).pack()
    
    cfg_ref_scroll = scrolledtext.ScrolledText(
        cfg_ref_frame, bg="#0d0d1a", fg="#778899", font=mono_sm,
        wrap="word", selectbackground="#1a2a4a", state="normal"
    )
    cfg_ref_scroll.insert("1.0", _GUIDE)
    cfg_ref_scroll.configure(state="disabled")
    cfg_ref_scroll.pack(fill="both", expand=True, padx=4, pady=4)
    
    cfg_editor = tk.Text(
        cfg_editor_frame, bg="#06080a", fg="#aaddaa",
        font=("Courier", 9), selectbackground="#1a2a4a",
        insertbackground="#00ff88", wrap="none", undo=True
    )
    cfg_editor_vsb = tk.Scrollbar(cfg_editor_frame, command=cfg_editor.yview, bg="#0d0d1a")
    cfg_editor_hsb = tk.Scrollbar(cfg_editor_frame, orient="horizontal",
                                   command=cfg_editor.xview, bg="#0d0d1a")
    cfg_editor.configure(yscrollcommand=cfg_editor_vsb.set, xscrollcommand=cfg_editor_hsb.set)
    cfg_editor_vsb.pack(side="right", fill="y")
    cfg_editor_hsb.pack(side="bottom", fill="x")
    cfg_editor.pack(fill="both", expand=True)
    
    cfg_editor.tag_configure("jkey",  foreground="#00ddaa")
    cfg_editor.tag_configure("jstr",  foreground="#88ccff")
    cfg_editor.tag_configure("jnum",  foreground="#ffcc44")
    cfg_editor.tag_configure("jbool", foreground="#ff8844")
    
    
    def _highlight_json(event=None):
        import re as _re
        for tag in ("jkey","jstr","jnum","jbool"):
            cfg_editor.tag_remove(tag, "1.0", tk.END)
        content = cfg_editor.get("1.0", tk.END)
        for m in _re.finditer(r'"([^"]+)"\s*:', content):
            cfg_editor.tag_add("jkey", f"1.0 + {m.start()} chars", f"1.0 + {m.end()-1} chars")
        for m in _re.finditer(r':\s*"([^"]*)"', content):
            cfg_editor.tag_add("jstr", f"1.0 + {m.start(1)-1} chars", f"1.0 + {m.end(1)+1} chars")
        for m in _re.finditer(r':\s*(-?\d+\.?\d*)', content):
            cfg_editor.tag_add("jnum", f"1.0 + {m.start(1)} chars", f"1.0 + {m.end(1)} chars")
        for m in _re.finditer(r':\s*(true|false|null)', content):
            cfg_editor.tag_add("jbool", f"1.0 + {m.start(1)} chars", f"1.0 + {m.end(1)} chars")
    
    
    cfg_editor.bind("<KeyRelease>", _highlight_json)
    _load_config_into_editor()
    
    
    # ═══════════════════════════════════════════════════════════════════════════════
    #  RENDERERS
    # ═══════════════════════════════════════════════════════════════════════════════
    _last_signals: list[SignalDict] = []
    _last_wallets        = {}
    _last_rejects        = []
    _last_trades         = []
    _cycle_num           = [0]
    _pending_update      = [False]
    _show_signal_history = [False]
    _signal_history_cache: list[list[SignalDict]] = [[]]
    _last_hb_ts          = [0.0]
    _HB_DEAD_SECS = 60
    _HB_BLINK_MS  = 600

    def _require_row_object(value: object, label: str) -> dict[str, object]:
        if isinstance(value, dict):
            return value
        raise TypeError(f"{label} must be an object, got {type(value).__name__}")

    def _require_signal_rows(value: object) -> list[SignalDict]:
        if not isinstance(value, list):
            raise TypeError(f"signals must be a list, got {type(value).__name__}")
        rows: list[SignalDict] = []
        for idx, item in enumerate(value):
            rows.append(cast("SignalDict", _require_row_object(item, f"signal[{idx}]")))
        return rows

    def _signal_ev_pct(signal: SignalDict) -> float:
        ev_info = signal.get("ev_info")
        if isinstance(ev_info, dict):
            ev_pct = ev_info.get("ev_pct")
            if isinstance(ev_pct, (int, float)):
                return float(ev_pct)
        return 0.0

    def _signal_age_minutes(signal: SignalDict) -> float:
        age_min = signal.get("age_min")
        if isinstance(age_min, (int, float)):
            return float(age_min)
        age_h = signal.get("age_h")
        if isinstance(age_h, (int, float)):
            return float(age_h) * 60.0
        return 0.0

    @dataclass(frozen=True)
    class _InspectorRow:
        name: str
        value_text: str
        child_value: object | None

    def _format_property_value(value: object) -> str:
        if value is None:
            return ""
        if isinstance(value, float):
            return f"{value:.6f}"
        if isinstance(value, (int, bool, str)):
            return str(value)
        return str(value)

    def _describe_property_value(value: object) -> tuple[str, object | None]:
        if isinstance(value, dict):
            if not value:
                return "{}", None
            return f"{len(value)} field(s) - double-click", value
        if isinstance(value, list):
            if not value:
                return "[]", None
            return f"{len(value)} item(s) - double-click", value
        if isinstance(value, tuple):
            if not value:
                return "()", None
            return f"{len(value)} item(s) - double-click", list(value)
        if isinstance(value, set):
            if not value:
                return "set()", None
            return f"{len(value)} item(s) - double-click", sorted(value, key=str)
        if is_dataclass(value) and not isinstance(value, type):
            return f"{type(value).__name__} - double-click", value
        return _format_property_value(value), None

    def _build_property_rows(value: object) -> list[_InspectorRow]:
        rows: list[_InspectorRow] = []
        if isinstance(value, dict):
            for key in sorted(value.keys(), key=str):
                text, child_value = _describe_property_value(value[key])
                rows.append(_InspectorRow(str(key), text, child_value))
            return rows
        if isinstance(value, list):
            for idx, item in enumerate(value):
                text, child_value = _describe_property_value(item)
                rows.append(_InspectorRow(f"[{idx}]", text, child_value))
            return rows
        if isinstance(value, tuple):
            for idx, item in enumerate(value):
                text, child_value = _describe_property_value(item)
                rows.append(_InspectorRow(f"[{idx}]", text, child_value))
            return rows
        if is_dataclass(value) and not isinstance(value, type):
            for field_info in fields(value):
                field_value = getattr(value, field_info.name)
                text, child_value = _describe_property_value(field_value)
                rows.append(_InspectorRow(field_info.name, text, child_value))
            return rows
        text, child_value = _describe_property_value(value)
        rows.append(_InspectorRow("value", text, child_value))
        return rows

    def _previewable_mapping(value: object) -> dict[str, object] | None:
        if isinstance(value, dict):
            return {str(key): value[key] for key in sorted(value.keys(), key=str)}
        if is_dataclass(value) and not isinstance(value, type):
            return {field_info.name: getattr(value, field_info.name) for field_info in fields(value)}
        if isinstance(value, (list, tuple)):
            seq = list(value)
            if not seq:
                return {}
            if (
                len(seq) == 2
                and isinstance(seq[0], (int, float))
                and isinstance(seq[1], (int, float))
            ):
                ts_value = float(seq[0])
                if ts_value > 0:
                    try:
                        dt_text = datetime.fromtimestamp(ts_value).strftime("%Y-%m-%d %H:%M:%S")
                    except Exception:
                        dt_text = str(seq[0])
                    return {"Date": dt_text, "Value": seq[1]}
            return {f"Value {idx + 1}": item for idx, item in enumerate(seq[:4])}
        return None

    def _list_preview_columns(value: object) -> list[str]:
        if not isinstance(value, list) or not value:
            return []
        preview_fields: list[str] = []
        seen: set[str] = set()
        for item in value:
            mapping = _previewable_mapping(item)
            if mapping is None:
                continue
            for key, raw in mapping.items():
                if key in seen:
                    continue
                _, child_value = _describe_property_value(raw)
                if child_value is not None:
                    continue
                seen.add(key)
                preview_fields.append(key)
                if len(preview_fields) >= 4:
                    return preview_fields
        return preview_fields

    def show_properties_popup(
        value: object,
        title: str = "Properties",
        subtitle: str = "",
        parent: tk.Misc | None = None,
    ) -> None:
        owner = parent if parent is not None else root
        detail_win = tk.Toplevel(root)
        detail_win.title(title[:80] if title else "Properties")
        detail_win.configure(bg="#080810")
        detail_win.geometry("600x550")

        tk.Label(detail_win, text=title or "Properties", fg="#00ff88", bg="#080810",
                 font=bold_hd, anchor="w", justify="left").pack(fill="x", padx=10, pady=(10, 4))
        if subtitle:
            tk.Label(detail_win, text=subtitle, fg="#556677", bg="#080810",
                     font=mono_sm, anchor="w", justify="left").pack(fill="x", padx=10, pady=(0, 6))

        table_wrap = tk.Frame(detail_win, bg="#080810")
        table_wrap.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        preview_fields = _list_preview_columns(value)
        prop_cols = ("Property", *preview_fields) if preview_fields else ("Property", "Value")
        prop_tree = ttk.Treeview(table_wrap, columns=prop_cols, show="headings")
        prop_tree.heading("Property", text="Property")
        prop_tree.column("Property", width=210, anchor="w", stretch=False)
        if preview_fields:
            for field_name in preview_fields:
                prop_tree.heading(field_name, text=field_name)
                prop_tree.column(field_name, width=150, anchor="w", stretch=True)
        else:
            prop_tree.heading("Value", text="Value")
            prop_tree.column("Value", width=730, anchor="w", stretch=True)

        prop_vsb = tk.Scrollbar(table_wrap, command=prop_tree.yview)
        prop_hsb = tk.Scrollbar(table_wrap, orient="horizontal", command=prop_tree.xview)
        prop_tree.configure(yscrollcommand=prop_vsb.set, xscrollcommand=prop_hsb.set)

        prop_vsb.pack(side="right", fill="y")
        prop_hsb.pack(side="bottom", fill="x")
        prop_tree.pack(fill="both", expand=True)

        detail_items: dict[str, object] = {}
        if preview_fields and isinstance(value, list):
            for idx, item in enumerate(value):
                mapping = _previewable_mapping(item)
                if mapping is None:
                    text, child_value = _describe_property_value(item)
                    item_id = prop_tree.insert("", "end", values=(f"[{idx}]", text, "", "", "")[:len(prop_cols)])
                    if child_value is not None:
                        detail_items[str(item_id)] = child_value
                    continue
                row_values = [f"[{idx}]"]
                for field_name in preview_fields:
                    raw_value = mapping.get(field_name)
                    text, child_value = _describe_property_value(raw_value)
                    row_values.append(text if child_value is None else text)
                item_id = prop_tree.insert("", "end", values=tuple(row_values))
                detail_items[str(item_id)] = item
        else:
            for row in _build_property_rows(value):
                item_id = prop_tree.insert("", "end", values=(row.name, row.value_text))
                if row.child_value is not None:
                    detail_items[str(item_id)] = row.child_value

        help_var = tk.StringVar(value="Double-click a row to inspect nested values.")
        tk.Label(detail_win, textvariable=help_var, fg="#445566", bg="#080810",
                 font=mono_sm, anchor="w").pack(fill="x", padx=10, pady=(0, 10))

        def _open_selected_property(event: tk.Event[tk.Misc]) -> None:
            item_id = prop_tree.identify_row(event.y)
            if not item_id:
                selection = prop_tree.selection()
                if not selection:
                    return
                item_id = str(selection[0])
            else:
                prop_tree.selection_set(item_id)
                prop_tree.focus(item_id)
            child_value = detail_items.get(str(item_id))
            if child_value is None:
                help_var.set("Selected property is a scalar value.")
                return
            values = prop_tree.item(item_id).get("values", [])
            prop_name = str(values[0]) if values else "Property"
            show_properties_popup(
                child_value,
                title=f"{title} / {prop_name}",
                subtitle=prop_name,
                parent=detail_win,
            )

        prop_tree.bind("<Double-1>", _open_selected_property)

        if isinstance(owner, (tk.Tk, tk.Toplevel)):
            try:
                owner_x = owner.winfo_rootx()
                owner_y = owner.winfo_rooty()
                detail_win.geometry(f"+{owner_x + 40}+{owner_y + 40}")
            except Exception:
                pass
        detail_win.lift()
        detail_win.focus_set()

    def show_raw_data_popup(value: object, title: str = "Raw Data") -> None:
        win = tk.Toplevel(root)
        win.title(title[:80] if title else "Raw Data")
        win.configure(bg="#060615")
        win.geometry("760x560")
        win.resizable(True, True)

        tk.Label(win, text=title or "Raw Data", fg="#00ff88", bg="#060615",
                 font=bold_hd, anchor="w", justify="left").pack(fill="x", padx=10, pady=(10, 4))

        raw_txt = scrolledtext.ScrolledText(
            win,
            bg="#040410",
            fg="#778899",
            font=("Courier", 8),
            wrap="word",
        )
        raw_txt.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        raw_txt.insert("1.0", json.dumps(value, indent=2, default=str))
        raw_txt.focus_set()

    def show_market_detail(market: dict[str, object], signal_title: str = "") -> None:
        win = tk.Toplevel(root)
        market_title = str(market.get("title", signal_title or "Market"))
        market_slug = str(market.get("slug", ""))
        win.title(f"Market Detail — {market_title[:50]}")
        win.configure(bg="#060615")
        win.geometry("760x560")
        win.resizable(True, True)

        mono10 = font.Font(family="Courier", size=10)
        mono9 = font.Font(family="Courier", size=9)
        bold9 = font.Font(family="Courier", size=9, weight="bold")
        bold11 = font.Font(family="Courier", size=11, weight="bold")

        liq = float(market.get("liq", 0.0) or 0.0)
        volume = float(market.get("volume", 0.0) or 0.0)
        yes_price = float(market.get("yes_price", 0.0) or 0.0)
        no_price = float(market.get("no_price", 0.0) or 0.0)
        hrs_left_obj = market.get("hrs_left")
        hrs_left_text = f"{float(hrs_left_obj):.1f}h" if isinstance(hrs_left_obj, (int, float)) else "—"
        ts_value = float(market.get("ts", 0.0) or 0.0)
        ts_text = datetime.fromtimestamp(ts_value).strftime("%Y-%m-%d %H:%M:%S") if ts_value > 0 else "—"

        hf = tk.Frame(win, bg="#0a0a20", pady=8)
        hf.pack(fill="x", padx=8, pady=(8, 0))
        tk.Label(hf, text=f"📈 {market_title}",
                 fg="#00aaff", bg="#0a0a20", font=bold11,
                 wraplength=730, justify="left").pack(anchor="w", padx=12)
        tk.Label(hf, text=f"Slug: {market_slug or '—'}   Event: {market.get('event_slug', '—')}",
                 fg="#556677", bg="#0a0a20", font=mono9,
                 wraplength=730, justify="left").pack(anchor="w", padx=12)

        sf2 = tk.Frame(win, bg="#060615")
        sf2.pack(fill="x", padx=8, pady=6)

        def stat_cell(parent: tk.Misc, label: str, value: str, color: str = "#aaaacc", col: int = 0, row: int = 0) -> None:
            f = tk.Frame(parent, bg="#0d0d20", bd=1, relief="solid")
            f.grid(row=row, column=col, padx=4, pady=3, sticky="nsew")
            tk.Label(f, text=label, fg="#445566", bg="#0d0d20", font=mono9, pady=2).pack()
            tk.Label(f, text=value, fg=color, bg="#0d0d20", font=bold9, pady=2).pack()

        stats_data = [
            ("Liquidity", f"${liq:,.0f}", "#00ff88"),
            ("Volume", f"${volume:,.0f}", "#88ccff"),
            ("Hours Left", hrs_left_text, "#ffdd44"),
            ("End Date", str(market.get("end_date", "—") or "—"), "#ff8844"),
            ("Slug", market_slug or "—", "#aaaaff"),
            ("Event Slug", str(market.get("event_slug", "—") or "—"), "#aaaacc"),
            ("Yes Price", f"${yes_price:.4f}" if yes_price > 0 else "—", "#00ff88"),
            ("No Price", f"${no_price:.4f}" if no_price > 0 else "—", "#ff8844"),
            ("Volume", f"${volume:,.0f}", "#88ccff"),
            ("Timestamp", ts_text, "#88ccff"),
            ("Outcome Labels", f"{len(market.get('outcome_labels', []) or [])}", "#aaaacc"),
        ]
        for i, (lbl, val, col) in enumerate(stats_data):
            sf2.columnconfigure(i % 4, weight=1)
            stat_cell(sf2, lbl, val, col, i % 4, i // 4)

        info_f = tk.Frame(win, bg="#060615")
        info_f.pack(fill="x", padx=8, pady=(0, 6))
        tk.Label(info_f, text="DETAILS", fg="#00ff88", bg="#060615", font=bold9).pack(anchor="w", padx=4, pady=(4, 2))
        detail_lines = [
            f"  Title: {market_title}",
            f"  End date: {market.get('end_date', '—')}",
            f"  Hours left: {hrs_left_text}",
        ]
        outcome_labels = market.get("outcome_labels")
        if isinstance(outcome_labels, list) and outcome_labels:
            detail_lines.append(f"  Outcomes: {', '.join(str(x) for x in outcome_labels[:8])}")
        for line in detail_lines:
            tk.Label(info_f, text=line, fg="#cccccc", bg="#060615", font=mono10,
                     anchor="w", justify="left", wraplength=720).pack(anchor="w", padx=12)

        lf = tk.Frame(win, bg="#060615")
        lf.pack(fill="x", padx=8, pady=6)

        def copy_title() -> None:
            try:
                win.clipboard_clear()
                win.clipboard_append(market_title)
                win.update()
            except Exception as e:
                _log_ui_error("copy market title", e, "WARN")

        def inspect_raw_data() -> None:
            show_raw_data_popup(market, title=f"Market Raw Data - {market_title}")

        def open_properties() -> None:
            show_properties_popup(market,
                                  title=f"Market Properties - {market_title}",
                                  subtitle="Double-click nested rows to inspect them.")

        tk.Button(lf, text="📋 Copy Title", bg="#1a2a1a", fg="#00ff88",
                  font=mono9, padx=10, command=copy_title).pack(side="left", padx=4)
        tk.Button(lf, text="🔎 Inspect Raw", bg="#201a2a", fg="#d0b0ff",
                  font=mono9, padx=10, command=inspect_raw_data).pack(side="left", padx=4)
        tk.Button(lf, text="🧩 Properties", bg="#2a2012", fg="#ffcc88",
                  font=mono9, padx=10, command=open_properties).pack(side="left", padx=4)

    def _show_signal_detail(signal: SignalDict) -> None:
        signal_title = str(signal.get("title", "Signal"))
        signal_outcome = str(signal.get("outcome", ""))
        popup_title = signal_title if not signal_outcome else f"{signal_title} [{signal_outcome}]"
        win = tk.Toplevel(root)
        win.title(f"Signal Detail — {signal_title[:50]}")
        win.configure(bg="#060615")
        win.geometry("760x560")
        win.resizable(True, True)

        mono10 = font.Font(family="Courier", size=10)
        mono9 = font.Font(family="Courier", size=9)
        bold9 = font.Font(family="Courier", size=9, weight="bold")
        bold11 = font.Font(family="Courier", size=11, weight="bold")

        score = float(signal.get("score", 0.0) or 0.0)
        drift = float(signal.get("drift", 0.0) or 0.0)
        cur = float(signal.get("cur", 0.0) or 0.0)
        avg_entry = float(signal.get("avg_entry", 0.0) or 0.0)
        bet = float(signal.get("bet", 0.0) or 0.0)
        total_flow = float(signal.get("total_flow", 0.0) or 0.0)
        ver_flow = float(signal.get("ver_flow", 0.0) or 0.0)
        n_elite = int(signal.get("n_elite", 0) or 0)
        n_ver = int(signal.get("n_ver", 0) or 0)
        n_total = int(signal.get("n_total", 0) or 0)
        tier = str(signal.get("tier", "?"))
        strategy = str(signal.get("strategy", "?"))
        newest_ts = float(signal.get("newest_ts", 0.0) or 0.0)
        newest_ts_text = datetime.fromtimestamp(newest_ts).strftime("%Y-%m-%d %H:%M:%S") if newest_ts > 0 else "—"
        pnl_color = "#00ff55" if drift <= 0 else "#ffcc44"
        icon = "💎" if tier == "CONVICTION" else ("⚡" if bool(signal.get("is_hft")) else "🎯")

        hf = tk.Frame(win, bg="#0a0a20", pady=8)
        hf.pack(fill="x", padx=8, pady=(8, 0))
        tk.Label(hf, text=f"{icon} [{tier}] {signal_title}",
                 fg="#00aaff", bg="#0a0a20", font=bold11,
                 wraplength=730, justify="left").pack(anchor="w", padx=12)
        tk.Label(hf, text=f"OUTCOME: {signal_outcome or '—'}",
                 fg="#fff4b0", bg="#4a2a00", font=font.Font(family="Courier", size=11, weight="bold"),
                 padx=10, pady=4,
                 wraplength=730, justify="left").pack(anchor="w", padx=12, pady=(4, 4))
        tk.Label(hf, text=f"Score: {score:.0f}   Strategy: {strategy}",
                 fg="#556677", bg="#0a0a20", font=mono9,
                 wraplength=730, justify="left").pack(anchor="w", padx=12)

        sf2 = tk.Frame(win, bg="#060615")
        sf2.pack(fill="x", padx=8, pady=6)

        def stat_cell(parent: tk.Misc, label: str, value: str, color: str = "#aaaacc", col: int = 0, row: int = 0) -> None:
            f = tk.Frame(parent, bg="#0d0d20", bd=1, relief="solid")
            f.grid(row=row, column=col, padx=4, pady=3, sticky="nsew")
            tk.Label(f, text=label, fg="#445566", bg="#0d0d20", font=mono9, pady=2).pack()
            tk.Label(f, text=value, fg=color, bg="#0d0d20", font=bold9, pady=2).pack()

        stats_data = [
            ("Whale Entry", f"${avg_entry:.4f}", "#ffaa44"),
            ("Current Price", f"${cur:.4f}", "#aaaaff"),
            ("Drift", f"{drift * 100:+.1f}%", pnl_color),
            ("Age", f"{_signal_age_minutes(signal):.0f} min", "#888888"),
            ("Bet Size", f"${bet:.2f}", "#00aaff"),
            ("Total Flow", f"${total_flow:,.0f}", "#00ff88"),
            ("Verified Flow", f"${ver_flow:,.0f}", "#88ccff"),
            ("Elite / Verified", f"{n_elite} / {n_ver}", "#ffdd44"),
            ("Confluence", f"{int(signal.get('n_confluence', 0) or 0)}", "#aaaacc"),
            ("Total Whales", f"{n_total}", "#aaaacc"),
            ("Window", str(signal.get("window", "?")).upper(), "#ff8844"),
            ("Stop Loss", "OFF" if signal.get("stop_loss_pct") is None else f"{float(signal.get('stop_loss_pct', 0.0) or 0.0) * 100:.0f}%", "#aaaacc"),
            ("Score", f"{score:.0f}", "#ffdd44"),
            ("Tier", tier, "#ff8844"),
            ("Large Trade", "YES" if bool(signal.get("has_large_trade")) else "NO", "#00ff88" if bool(signal.get("has_large_trade")) else "#aaaaaa"),
            ("Newest TS", newest_ts_text, "#88ccff"),
        ]
        for i, (lbl, val, col) in enumerate(stats_data):
            sf2.columnconfigure(i % 4, weight=1)
            stat_cell(sf2, lbl, val, col, i % 4, i // 4)

        info_f = tk.Frame(win, bg="#060615")
        info_f.pack(fill="x", padx=8, pady=(0, 6))
        tk.Label(info_f, text="DETAILS", fg="#00ff88", bg="#060615", font=bold9).pack(anchor="w", padx=4, pady=(4, 2))
        detail_lines = [
            f"  CID: {signal.get('cid', '')}",
            f"  Market type: {signal.get('mkt_type', '?')}",
            f"  Sports: {'yes' if bool(signal.get('is_sports')) else 'no'}",
            f"  HFT: {'yes' if bool(signal.get('is_hft')) else 'no'}",
            f"  Conviction: {'yes' if bool(signal.get('has_large_trade')) else 'no'}",
        ]
        names = signal.get("names")
        if isinstance(names, list) and names:
            detail_lines.append(f"  Via: {', '.join(str(x) for x in names[:6])}")
        exits = signal.get("exits_detected")
        if isinstance(exits, list) and exits:
            detail_lines.append(f"  Exit alerts: {len(exits)}")
        conviction_detail = signal.get("conviction_detail")
        if isinstance(conviction_detail, str) and conviction_detail.strip():
            detail_lines.append(f"  Conviction detail: {conviction_detail}")
        for line in detail_lines:
            tk.Label(info_f, text=line, fg="#cccccc", bg="#060615", font=mono10,
                     anchor="w", justify="left", wraplength=720).pack(anchor="w", padx=12)

        lf = tk.Frame(win, bg="#060615")
        lf.pack(fill="x", padx=8, pady=6)
        event_slug = str(signal.get("event_slug", "") or "")
        signal_url = f"https://polymarket.com/event/{event_slug}" if event_slug else ""

        def copy_title() -> None:
            try:
                win.clipboard_clear()
                win.clipboard_append(signal_title)
                win.update()
            except Exception as e:
                _log_ui_error("copy signal title", e, "WARN")

        def open_polymarket_signal() -> None:
            if not signal_url:
                log("[signal detail] signal slug missing", "WARN")
                return
            webbrowser.open(signal_url)

        def inspect_raw_data() -> None:
            show_raw_data_popup(signal, title=f"Signal Raw Data - {popup_title}")

        def open_properties() -> None:
            show_properties_popup(signal,
                                  title=f"Signal Properties - {popup_title}",
                                  subtitle="Double-click nested rows to inspect them.")

        def open_market_detail() -> None:
            market_value = signal.get("mkt")
            if not isinstance(market_value, dict):
                log("[signal detail] market payload missing", "WARN")
                return
            show_market_detail(cast(dict[str, object], market_value), signal_title=signal_title)

        tk.Button(lf, text="🌐 Polymarket", bg="#0a1a3a", fg="#00aaff",
                  font=mono9, padx=10, command=open_polymarket_signal).pack(side="left", padx=4)
        tk.Button(lf, text="📈 Market", bg="#10203a", fg="#88ccff",
                  font=mono9, padx=10, command=open_market_detail).pack(side="left", padx=4)
        tk.Button(lf, text="📋 Copy Title", bg="#1a2a1a", fg="#00ff88",
                  font=mono9, padx=10, command=copy_title).pack(side="left", padx=4)
        tk.Button(lf, text="🔎 Inspect Raw", bg="#201a2a", fg="#d0b0ff",
                  font=mono9, padx=10, command=inspect_raw_data).pack(side="left", padx=4)
        tk.Button(lf, text="🧩 Properties", bg="#2a2012", fg="#ffcc88",
                  font=mono9, padx=10, command=open_properties).pack(side="left", padx=4)

    def _on_signal_double_click(event: tk.Event[tk.Misc]) -> None:
        log(
            f"[signal dblclick] x={event.x} y={event.y} "
            f"selection={list(sig_tree.selection())} "
            f"children={len(sig_tree.get_children())}",
            "DEBUG",
        )
        item_id = sig_tree.identify_row(event.y)
        if not item_id:
            log("[signal dblclick] no row from identify_row, falling back to selection", "DEBUG")
            selection = sig_tree.selection()
            if not selection:
                log("[signal dblclick] no selection available", "WARN")
                return
            item_id = str(selection[0])
        else:
            sig_tree.selection_set(item_id)
            sig_tree.focus(item_id)
        log(f"[signal dblclick] resolved item_id={item_id}", "DEBUG")
        signal = _signal_tree_items.get(str(item_id))
        if signal is None:
            log(f"[signal dblclick] no backing signal found for item_id={item_id}", "WARN")
            return
        log(
            f"[signal dblclick] opening detail for "
            f"title={signal.get('title', '?')} outcome={signal.get('outcome', '')}",
            "DEBUG",
        )
        try:
            _show_signal_detail(signal)
        except Exception as e:
            log(f"[signal dblclick] popup failed: {e}", "ERR")

    def _build_wallet_cache(value: object) -> dict[str, dict[str, object]]:
        if not isinstance(value, list):
            raise TypeError(f"whales must be a list, got {type(value).__name__}")
        wallet_cache: dict[str, dict[str, object]] = {}
        for idx, item in enumerate(value):
            whale_row = _require_row_object(item, f"whale[{idx}]")
            wallet_value = whale_row.get("wallet")
            if not isinstance(wallet_value, str):
                raise TypeError(f"whale[{idx}].wallet must be a string")
            whale_profile = {key: row_value for key, row_value in whale_row.items() if key != "wallet"}
            wallet_cache[wallet_value] = whale_profile
        return wallet_cache

    def _hb_tick():
        try:
            elapsed = time.time() - _last_hb_ts[0]
            if _last_hb_ts[0] == 0.0:
                hb_var.set("⬤")
                hb_label.configure(fg="#667788")
            elif elapsed > _HB_DEAD_SECS:
                hb_var.set("⬤")
                hb_label.configure(fg="#ff2222")
            else:
                hb_var.set("⬤")
                hb_label.configure(fg="#00ff88")
        except Exception:
            pass
        root.after(_HB_BLINK_MS, _hb_tick)
    def _on_hb(_p):
        _last_hb_ts[0] = time.time()
    api.subscribe("titan/heartbeat", _on_hb)
    api.start()
    root.after(_HB_BLINK_MS, _hb_tick)
    _show_closed    = [False]
    
    
    def render_signals(signals):
        sig_tree.delete(*sig_tree.get_children())
        _signal_tree_items.clear()
        for row in signals:
            try:
                if isinstance(row, dict) and "signal" in row:
                    s = row.get("signal") or {}
                    recorded_at = row.get("recorded_at", "")
                    hist_suffix = f" HIST {recorded_at[11:16]}" if isinstance(recorded_at, str) and len(recorded_at) >= 16 else " HIST"
                else:
                    s = row
                    hist_suffix = ""
                hft_tag  = "⚡" if s.get("is_hft") else ""
                exit_tag = " ⚠EXIT" if s.get("exits_detected") else ""
                mode_str = f"{hft_tag}{s.get('window','?').upper()}{exit_tag}{hist_suffix}"
                full_title = f"{s.get('title','?')}  [{s.get('outcome','')}]"
                item_id = sig_tree.insert("", "end", values=(
                    f"{s.get('score',0):.0f}",
                    full_title[:90],
                    s.get("outcome", ""),
                    f"${s.get('avg_entry',0):.4f}",
                    f"${s.get('cur',0):.4f}",
                    f"{(s.get('drift') or 0)*100:+.1f}%",
                    f"{_signal_age_minutes(cast('SignalDict', s)):.0f}m",
                    f"${s.get('total_flow',0):,.0f}",
                    f"{s.get('n_ver',0)}/{s.get('n_total',0)}",
                    mode_str,
                ), tags=(s.get("tier", ""),))
                _signal_tree_items[str(item_id)] = cast("SignalDict", s)
            except Exception as e:
                log(f"[render_signals row error] {e}", "ERR")
        log(
            f"[render_signals] tree_rows={len(sig_tree.get_children())} "
            f"backing_rows={len(_signal_tree_items)} history_mode={_show_signal_history[0]}",
            "DEBUG",
        )

    sig_tree.bind("<Double-1>", _on_signal_double_click)
    
    
    def render_alerts(signals, wallets):
        alert_txt.configure(state="normal")
        alert_txt.delete("1.0", tk.END)
        ts  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        top = [s for s in signals if s["tier"] in ("CONVICTION","ALERT","STRONG","HFT","ELITE_ONLY")]
    
        if not top:
            med = [s for s in signals if s["tier"] == "MEDIUM"]
            alert_txt.insert(tk.END,
                f"\n  {'═'*66}\n  No ALERT/STRONG/HFT signals  —  {ts}\n  {'─'*66}\n")
            if med:
                alert_txt.insert(tk.END, f"  {len(med)} MEDIUM signal(s):\n\n")
                for s in med:
                    alert_txt.insert(tk.END,
                        f"    • {s['title'][:60]}\n"
                        f"      [{s['outcome']}] @ ${s['cur']:.4f} (whale entry ${s['avg_entry']:.4f}) | "
                        f"drift {s['drift']*100:+.1f}% | score {s['score']:.0f}\n"
                        f"      via: {', '.join(s.get('names', [])[:3])}\n\n")
            else:
                alert_txt.insert(tk.END, "  0 signals. Check DIAGNOSTICS tab.\n")
            alert_txt.insert(tk.END, f"  {'═'*66}\n")
            alert_txt.configure(state="disabled")
            return
    
        alert_txt.insert(tk.END,
            f"{'█'*70}\n  🚨  SNIPER ALERTS  —  {ts}\n"
            f"  {len(top)} tradeable signal(s)\n"
            f"{'█'*70}\n\n"
        )
    
        for i, s in enumerate(top, 1):
            mkt  = s["mkt"]
            hrs  = mkt.get("hrs_left")
            hrs_s = f"{hrs:.0f}h" if hrs else "open"
    
            tier_icons = {
                "CONVICTION": "💎💎💎 BIG CONVICTION",
                "ALERT":      "🟢🟢🟢",
                "STRONG":     "🟡🟡",
                "HFT":        "⚡⚡ HFT SPIKE",
                "ELITE_ONLY": "🔥 ELITE-ONLY",
            }
            icon = tier_icons.get(s["tier"], "🔵")
    
            d = s["drift"]
            if abs(d) < 0.02:   fresh = "⚡ VERY FRESH"
            elif abs(d) < 0.06: fresh = "✅ FRESH"
            elif abs(d) < 0.10: fresh = "✅ ACCEPTABLE"
            else:                fresh = "⚠ STALE"
    
            in_market  = s["cid"] in api.get_pnl_summary()["active_market_cids"]
            trade_note = "🤖 AUTO-BOUGHT" if in_market else "⏳ Watching (below ALERT threshold)"
            cd_note    = ""
            if s["cid"] in api.get_pnl_summary()["cooldown_cids"]:
                remaining = _cfg.get("EXIT_COOLDOWN_SECONDS", 300) - (time.time() - api.get_pnl_summary()["cooldown_cids"][s["cid"]])
                cd_note   = f"\n  ⏳ COOLDOWN: {remaining/60:.0f}min remaining\n"
    
            exit_warn = "\n  ⚠ EXIT ALERT: Whale selling detected.\n" if s.get("exits_detected") else ""
            bd = s["bd"]
    
            elite_detail = []
            for w, t in list(s.get("elite_ver", {}).items())[:5]:
                wname = _wallet_cache().get(w, {}).get("name") or w[:14]+"…"
                wprof = _wallet_cache().get(w, {})
                elite_detail.append(
                    f"    🔥 {wname:<20} WR:{wprof.get('win_rate',0)*100:.0f}%  "
                    f"PnL:${wprof.get('total_pnl',0):+,.0f}  Score:{wprof.get('score',0):.2f}  "
                    f"Entry:${t['price']:.4f}  Cash:${t['cash']:,.0f}  "
                    f"{'⚡HFT' if wprof.get('hft') else ''}"
                )
    
            alert_txt.insert(tk.END,
                f"{'═'*70}\n"
                f"  {icon}  #{i} [{s['tier']}]  Score: {s['score']:.0f}/100  [{s['window'].upper()}]\n"
                f"  {trade_note}\n"
                f"{'═'*70}\n"
                f"{exit_warn}{cd_note}\n"
                f"  MARKET\n  {'─'*50}\n"
                f"  {s['title']}\n"
                f"  Outcome: {s['outcome']}\n"
                f"  Liq ${mkt['liq']:,.0f}  Vol ${mkt['volume']:,.0f}  Closes {mkt['end_date']} ({hrs_s})\n"
                f"  https://polymarket.com/event/{mkt.get('slug','')}\n\n"
                f"  ACTION\n  {'─'*50}\n"
                f"  Buy {s['outcome'].upper()} @ ${s['cur']:.4f} ({s['cur']*100:.1f}¢)\n"
                f"  Whale avg entry:  ${s['avg_entry']:.4f}  →  Now: ${s['cur']:.4f}\n"
                f"  Drift: {s['drift']*100:+.1f}%  {fresh}\n"
                f"  Auto-size: ${s['bet']:.2f}  ({s['bet']/max(api.get_pnl_summary()["bankroll"],0.01)*100:.1f}% bankroll)\n"
                f"  Shares: ~{s['bet']/max(s['cur'],0.01):.1f}\n\n"
                f"  WHALE INTEL  ({s['n_elite']} elite / {s['n_ver']} total verified)\n  {'─'*50}\n"
            )
            for line in elite_detail:
                alert_txt.insert(tk.END, line + "\n")
            alert_txt.insert(tk.END,
                f"\n  Total verified flow: ${s['ver_flow']:,.0f}  "
                f"Largest single: ${s.get('max_bet_cash',0):,.0f}\n"
                f"  Age: {_signal_age_minutes(s):.0f}min ago\n\n"
                f"  SCORE BREAKDOWN\n  {'─'*50}\n"
                f"  Wallet quality   {bd.get('wallet',0):>5.1f}/30\n"
                f"  Confluence       {bd.get('conf',0):>5}/18\n"
                f"  Recency          {bd.get('rec',0):>5}/20\n"
                f"  Price window     {bd.get('opp',0):>5}/15\n"
                f"  Market quality   {bd.get('mkt',0):>5.1f}/10\n"
                f"  Conviction       {bd.get('bonus',0):>5}/5\n"
                f"  Exit penalty     {bd.get('exit_penalty',0):>5}\n"
                f"  {'─'*24}\n"
                f"  TOTAL            {bd.get('total',0):>5.0f}/100\n\n"
            )
    
        alert_txt.configure(state="disabled")
    
    
    def render_open_positions():
        now_t = time.time()
        prev_sel_key = None
        sel = pos_tree.selection()
        if sel:
            if _show_closed[0]:
                prev_closed_pos = _closed_tree_items.get(str(sel[0]))
                if prev_closed_pos:
                    prev_sel_key = _closed_position_selection_key(prev_closed_pos)
            else:
                vals = pos_tree.item(sel[0])['values']
                if vals:
                    prev_sel_key = (str(vals[0])[:30], str(vals[1]))

        pos_tree.delete(*pos_tree.get_children())
        new_item_map = {}

        if _show_closed[0]:
            _closed_tree_items.clear()
            closed: list[TradeRecordDict] = api.get_closed_positions(limit=200)
            for pos in closed:
                entry   = pos.get("entry_price") or 0
                w_entry = pos.get("avg_entry") or entry
                exit_p  = pos.get("exit_price") or entry
                pnl_usd = pos.get("pnl_usdc") or 0
                pnl_pct = pos.get("pnl_pct") or ((exit_p - entry) / max(entry, 0.001) * 100)
                entry_ts  = pos.get("entry_ts") or pos.get("ts", now_t)
                exit_ts   = pos.get("exit_ts") or pos.get("ts", now_t)
                hold_min  = (exit_ts - entry_ts) / 60
                whale_str = ", ".join((pos.get("whale_names") or [])[:2])
                tag       = "CLOSED_WIN" if pnl_usd >= 0 else "CLOSED_LOSS"
                reason    = pos.get("reason") or "CLOSED"
                title_str = pos.get("title", "")
                outcome_str = pos.get("outcome", "")
                iid = pos_tree.insert("", "end", values=(
                    title_str[:48],
                    outcome_str,
                    f"${w_entry:.4f}",
                    f"${entry:.4f}",
                    f"${exit_p:.4f}",
                    f"{pnl_pct:+.1f}%",
                    f"${pnl_usd:+.3f}",
                    f"${pos.get('bet') or 0:.2f}",
                    f"{hold_min:.0f}m",
                    whale_str[:30],
                    f"{pos.get('score') or 0:.0f}",
                    reason[:18],
                ), tags=(tag,))
                _closed_tree_items[iid] = pos
                new_item_map[_closed_position_selection_key(pos)] = iid
            pos_var.set(f"Pos: {len(closed)} closed")
        else:
            _closed_tree_items.clear()
            for key, pos in sorted(_open_pos_dict().items(),
                                   key=lambda x: x[1].get("entry_ts", 0), reverse=True):
                entry    = pos.get("entry_price", 0)
                w_entry  = pos.get("avg_entry", entry)
                cur      = pos.get("cur_price", entry)
                shares   = pos.get("shares", 0)
                pnl_pct  = (cur - entry) / max(entry, 0.001) * 100
                pnl_usd  = (cur - entry) * shares
                hold_min = (now_t - pos.get("entry_ts", now_t)) / 60

                if hold_min < _cfg.get("MIN_HOLD_MINUTES", 15):
                    ws_str = f"🔒 HOLD {_cfg.get("MIN_HOLD_MINUTES", 15) - hold_min:.0f}m"
                    tag    = "HOLD"
                elif pnl_pct >= _cfg.get("PROFIT_TARGET_PCT", 0.20) * 100 * 0.7:
                    ws_str = f"✅ {pnl_pct:+.1f}% → SELL?"
                    tag    = "PROFIT"
                elif pnl_pct >= 2:
                    ws_str = f"✅ +{pnl_pct:.1f}%"
                    tag    = "PROFIT"
                elif pnl_pct <= -5:
                    ws_str = f"⚠ {pnl_pct:.1f}%"
                    tag    = "LOSS"
                else:
                    ws_str = "→ Holding"
                    tag    = "NEUTRAL"

                elite_names = pos.get("elite_names", [])
                if not elite_names:
                    elite_names = [_wallet_cache().get(w, {}).get("name", w[:10]+"…")
                                  for w in pos.get("elite_wallets", [])[:3]]
                whale_str = ", ".join(elite_names[:2])
                if pos.get("n_confluence", 0):
                    whale_str += f" +{pos['n_confluence']}conf"
                if pos.get("is_hft"):
                    whale_str = "⚡" + whale_str

                hft_tag   = "⚡" if pos.get("is_hft") else ""
                conv_tag  = "💎" if pos.get("is_conviction") else ""
                title_str = f"{conv_tag}{hft_tag}{pos.get('title','')}"
                outcome_str = pos.get("outcome","")

                iid = pos_tree.insert("", "end", values=(
                    title_str[:48],
                    outcome_str,
                    f"${w_entry:.4f}",
                    f"${entry:.4f}",
                    f"${cur:.4f}",
                    f"{pnl_pct:+.1f}%",
                    f"${pnl_usd:+.3f}",
                    f"${pos.get('bet',0):.2f}",
                    f"{hold_min:.0f}m",
                    whale_str[:30],
                    f"{pos.get('score',0):.0f}",
                    ws_str,
                ), tags=(tag,))
                new_item_map[(title_str[:30], outcome_str)] = iid

            pos_var.set(f"Pos: {len(_open_pos_dict())} open")

        if prev_sel_key and prev_sel_key in new_item_map:
            iid_to_select = new_item_map[prev_sel_key]
            pos_tree.selection_set(iid_to_select)
            pos_tree.see(iid_to_select)
    
    
    def render_whales(wallets):
        all_wallets = dict(_wallet_cache())
        all_wallets.update(wallets)
        filt = wh_filter_var.get()
    
        wh_tree.delete(*wh_tree.get_children())
        _whale_tree_items.clear()
        for w, p in sorted(all_wallets.items(), key=lambda x: x[1].get("score", 0), reverse=True):
            if p.get("total_pnl", 0) < 0 and not p.get("elite"):
                continue
            if p.get("score", 0) <= 0.10 and not p.get("watchable"):
                continue
            if filt == "ELITE" and not p.get("elite"):    continue
            if filt == "VER"   and not p.get("verified"): continue
            if filt == "HFT"   and not p.get("hft"):      continue
    
            if p.get("elite"):             tag = "ELITE"
            elif p.get("verified"):        tag = "VER"
            elif p.get("score", 0) >= 0.4: tag = "PAR"
            else:                          tag = "REJ"
    
            in_watch = False
            status = ("🔥 ELITE"  if p.get("elite") else
                      "✅ VER"    if p.get("verified") else
                      "👁 WATCH"  if in_watch else "❌")
            item_id = wh_tree.insert("", "end", values=(
                p.get("name", w[:10]+"…"),
                w[:26]+"…",
                f"{p.get('score',0):.2f}",
                f"{p.get('win_rate',0)*100:.0f}%",
                f"{p.get('wilson_lb',0)*100:.0f}%",
                p.get("n_resolved", 0),
                f"${p.get('total_value',0):,.0f}",
                f"${p.get('total_pnl',0):+,.0f}",
                f"${p.get('avg_bet',0):,.0f}",
                f"{p.get('trades_per_hour',0):.1f}",
                status,
                "⚡" if p.get("hft") else "",
            ), tags=(tag,))
            _whale_tree_items[str(item_id)] = (w, cast(dict[str, object], p))

    def _on_whale_double_click(event: tk.Event[tk.Misc]) -> None:
        item_id = wh_tree.identify_row(event.y)
        if not item_id:
            selection = wh_tree.selection()
            if not selection:
                return
            item_id = str(selection[0])
        else:
            wh_tree.selection_set(item_id)
            wh_tree.focus(item_id)
        whale_item = _whale_tree_items.get(str(item_id))
        if whale_item is None:
            log(f"[whale dblclick] no whale found for item_id={item_id}", "WARN")
            return
        wallet, whale = whale_item
        show_whale_detail(wallet, whale)

    wh_tree.bind("<Double-1>", _on_whale_double_click)
    
    
    def render_analysis(signals, trades, wallets):
        analysis_txt.configure(state="normal")
        analysis_txt.delete("1.0", tk.END)
        ts     = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ver    = {w: p for w, p in wallets.items() if p.get("verified")}
        elites = {w["wallet"]: w for w in api.get_whales()}
        hot_t  = sum(1 for t in trades if t.get("window") == "hot")
        hft_t  = sum(1 for t in trades if t.get("source") in ("hft_spike_poll",))
        st        = api.get_trade_stats()
        total_pnl = st["sum_pnl"]
        wins_n    = st["win_count"]
        wr_pct    = st["win_rate"] * 100
        n_sells   = st["sell_count"]

        analysis_txt.insert(tk.END,
            f"{'═'*78}\n  ANALYSIS  —  {ts}\n{'═'*78}\n\n"
            f"PAPER TRADING ACCOUNT\n{'─'*50}\n"
            f"  Bankroll:     ${api.get_pnl_summary()["bankroll"]:.4f}  (start ${api.get_pnl_summary()["bankroll_start"]:.2f})\n"
            f"  Session P&L:  ${api.get_pnl_summary()["session_pnl"]:+.4f}\n"
            f"  Total P&L:    ${total_pnl:+.4f}\n"
            f"  Trades:       {n_sells} closed  WR:{wr_pct:.0f}% ({wins_n}W/{n_sells - wins_n}L)\n"
            f"  Open:         {len(_open_pos_dict())} positions\n\n"
            f"TRADE FEED (this cycle)\n{'─'*50}\n"
            f"  Total: {len(trades)}  hot:{hot_t}  hft_spikes:{hft_t}\n"
            f"  Verified: {len(ver)}  Elite: {len(elites)}  Watchlist: {api.get_pnl_summary()['watchlist_size']}\n"
            f"  Signals: {len(signals)}\n"
            f"    💎 CONVICTION: {sum(1 for s in signals if s['tier']=='CONVICTION')}\n"
            f"    ⚡ HFT:    {sum(1 for s in signals if s['tier']=='HFT')}\n"
            f"    🚨 ALERT:  {sum(1 for s in signals if s['tier']=='ALERT')}\n"
            f"    🟡 STRONG: {sum(1 for s in signals if s['tier']=='STRONG')}\n"
            f"    🔵 MEDIUM: {sum(1 for s in signals if s['tier']=='MEDIUM')}\n\n"
            f"ELITE ROSTER\n{'─'*50}\n"
        )
        for w, p in sorted(elites.items(), key=lambda x: x[1].get("total_pnl",0), reverse=True):
            hft = "⚡HFT " if p.get("hft") else ""
            analysis_txt.insert(tk.END,
                f"  {hft}{p.get('name',w[:14]):<24} "
                f"Score:{p.get('score',0):.2f}  WR:{p.get('win_rate',0)*100:.0f}%  "
                f"PnL:${p.get('total_pnl',0):+,.0f}  TPH:{p.get('trades_per_hour',0):.1f}\n"
            )
        analysis_txt.configure(state="disabled")
    
    
    def render_diagnostics(rejects, trades, wallets):
        diag_txt.configure(state="normal")
        diag_txt.delete("1.0", tk.END)
        ts    = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        now_t = time.time()
    
        cd_lines = []
        for cid, cd_ts in api.get_pnl_summary()["cooldown_cids"].items():
            remaining = _cfg.get("EXIT_COOLDOWN_SECONDS", 300) - (now_t - cd_ts)
            mkt       = {}
            title     = mkt.get("title", cid[:30])
            cd_lines.append(f"  ⏳ {title[:45]}  {remaining/60:.0f}min left")
    
        diag_txt.insert(tk.END, f"{'═'*72}\n  DIAGNOSTICS  —  {ts}\n{'═'*72}\n\n")
        if cd_lines:
            diag_txt.insert(tk.END, f"COOLDOWNS ({len(cd_lines)})\n{'─'*72}\n")
            for line in cd_lines:
                diag_txt.insert(tk.END, line + "\n")
            diag_txt.insert(tk.END, "\n")
    
        diag_txt.insert(tk.END, f"REJECTIONS ({len(rejects)})\n{'─'*72}\n\n")
        for r in rejects:
            diag_txt.insert(tk.END, r + "\n\n")
    
        failed = [(w, p) for w, p in wallets.items() if not p.get("verified")]
        failed.sort(key=lambda x: x[1].get("score", 0), reverse=True)
        diag_txt.insert(tk.END, f"\n{'═'*72}\n  FAILED WALLETS (top 20)\n{'═'*72}\n")
        for w, p in failed[:20]:
            diag_txt.insert(tk.END,
                f"  {p.get('name',w[:14]):<22} "
                f"Score:{p.get('score',0):.2f}  WR:{p.get('win_rate',0)*100:.0f}%  "
                f"FAIL: {', '.join(p.get('fail_reasons', []))}\n"
            )
        diag_txt.configure(state="disabled")
    
    
    # ═══════════════════════════════════════════════════════════════════════════════
    #  ENGINE CALLBACKS
    # ═══════════════════════════════════════════════════════════════════════════════
    def on_log_cb(msg, level="INFO"):
        root.after(0, lambda: log(msg, level))
        if level == "ERR" and telegram_notifier is not None:
            threading.Thread(target=telegram_notifier.notify_error, args=(msg,), daemon=True).start()
    
    
    def on_position_open_cb(pos):
        def _update():
            elite_names = pos.get("elite_names", [])
            whale_str   = ", ".join(elite_names[:3]) or "?"
            pos_log_write(
                f"🛒 AUTO-BUY: {pos['title'][:80]} [{pos['outcome']}] "
                f"@ ${pos['entry_price']:.4f} (whale entry ${pos.get('avg_entry',pos['entry_price']):.4f}) "
                f"| ${pos['bet']:.2f} | [{pos['tier']} {pos['score']:.0f}pts] "
                f"| via {whale_str}",
                "BUY"
            )
            nb.select(tab_positions)
        root.after(0, _update)
        if telegram_notifier is not None:
            threading.Thread(target=telegram_notifier.notify_buy, args=(pos,), daemon=True).start()
    
    
    def on_position_close_cb(pos, pnl_usdc, pnl_pct):
        def _update():
            tag   = "SELL_W" if pnl_usdc >= 0 else "SELL_L"
            emoji = "✅" if pnl_usdc >= 0 else "❌"
            whale_str = ", ".join(pos.get("elite_names", [])[:2]) or "?"
            pos_log_write(
                f"{emoji} AUTO-SELL: {pos['title'][:80]} [{pos['outcome']}] "
                f"| Entry ${pos['entry_price']:.4f} → Exit ${pos.get('cur_price',0):.4f} "
                f"| P&L ${pnl_usdc:+.4f} ({pnl_pct*100:+.1f}%) "
                f"| {pos.get('reason','')} | via {whale_str}",
                tag
            )
        root.after(0, _update)
        if telegram_notifier is not None:
            threading.Thread(target=telegram_notifier.notify_sell, args=(pos, pnl_usdc, pnl_pct), daemon=True).start()
    
    
    def on_cycle_complete_cb(signals, wallets, rejects, trades):
        nonlocal _last_signals, _last_wallets, _last_rejects, _last_trades
        _last_signals = signals
        _last_wallets = wallets
        
        if rejects:
            for r in reversed(rejects):
                if r in _last_rejects:
                    _last_rejects.remove(r)
                _last_rejects.insert(0, r)
            _last_rejects = _last_rejects[:50]
            
        _last_trades  = trades
        _pending_update[0] = True
    
    
    # ═══════════════════════════════════════════════════════════════════════════════
    #  UI REFRESH LOOP  (fetch in bg thread → apply on main thread)
    # ═══════════════════════════════════════════════════════════════════════════════
    _fetch_running = [False]

    def ui_refresh():
        if not _fetch_running[0]:
            _fetch_running[0] = True
            threading.Thread(target=_fetch_and_apply, daemon=True).start()
        root.after(2000, ui_refresh)

    def _fetch_and_apply():
        try:
            data: dict = {}
            try: data["pnl"]      = api.get_pnl_summary()
            except Exception as e: data["pnl"] = {}; log(f"[fetch pnl] {e}", "ERR")
            try: data["logs"]     = api.get_logs(lines=600)
            except Exception as e: data["logs"] = ""; log(f"[fetch logs] {e}", "ERR")
            try: data["pos"]      = _open_pos_dict()
            except Exception as e: data["pos"] = {}; log(f"[fetch pos] {e}", "ERR")
            try: data["wallets"]  = _wallet_cache()
            except Exception as e: data["wallets"] = {}; log(f"[fetch wallets] {e}", "ERR")
            if not _show_signal_history[0]:
                try:
                    data["signals_live"] = _require_signal_rows(api.get_signals())
                except Exception as e: data["signals_live"] = []; log(f"[fetch live signals] {e}", "ERR")
            if _show_signal_history[0] and (_pending_update[0] or not _signal_history_cache[0]):
                try:
                    data["signal_history"] = api.get_signal_history(limit=200)
                except Exception as e: data["signal_history"] = []; log(f"[fetch signal history] {e}", "ERR")
            if _show_closed[0]:
                try: data["closed"] = api.get_closed_positions(limit=200)
                except Exception as e: data["closed"] = []; log(f"[fetch closed] {e}", "ERR")
            root.after(0, lambda: _ui_apply(data))
        finally:
            _fetch_running[0] = False

    def _ui_apply(data: dict):
        try:
            _ui_apply_inner(data)
        except Exception as e:
            import traceback
            log(f"[_ui_apply crash] {e}\n{traceback.format_exc()[:400]}", "ERR")

    def _ui_apply_inner(data: dict):
        pnl     = data.get("pnl", {})
        logs    = data.get("logs", "")
        pos     = data.get("pos", {})
        wallets = data.get("wallets", {})
        closed  = data.get("closed", [])
        signals_live = data.get("signals_live")
        signal_history = data.get("signal_history", [])
        if signals_live is not None:
            _last_signals[:] = signals_live
        if signal_history:
            _signal_history_cache[0] = signal_history

        signals = signals_live if (not _show_signal_history[0] and signals_live is not None) else _last_signals
        signal_rows = _signal_history_cache[0] if _show_signal_history[0] else signals

        try:
            render_signals(signal_rows)
        except Exception as _e:
            log(f"[render_signals error] {_e}", "ERR")

        sig_var.set(f"Sigs: {len(signal_rows)}")

        if _pending_update[0]:
            _pending_update[0] = False
            _cycle_num[0] += 1
            _wallets_from_cycle = _last_wallets
            rejects = _last_rejects
            trades  = _last_trades
            n_ver   = sum(1 for p in _wallets_from_cycle.values() if p.get("verified"))
            n_elite = sum(1 for p in wallets.values() if p.get("elite"))
            for fn in (
                lambda: render_alerts(signals, _wallets_from_cycle),
                lambda: render_analysis(signals, trades, _wallets_from_cycle),
                lambda: render_diagnostics(rejects, trades, _wallets_from_cycle),
            ):
                try:
                    fn()
                except Exception as e:
                    _log_ui_error("secondary renderer", e)
            ver_var.set(f"Ver: {n_ver}")
            elite_var.set(f"Elite: {n_elite}")

        try: render_open_positions()
        except Exception as _e: log(f"[render_open_positions error] {_e}", "ERR")
        try: refresh_pnl_tab()
        except Exception as _e: log(f"[refresh_pnl_tab error] {_e}", "ERR")
        try: render_whales(_last_wallets)
        except Exception as _e: log(f"[render_whales error] {_e}", "ERR")

        cycle_var.set(f"Cycle: {_cycle_num[0]}")

        open_value = sum(
            p.get("cur_price", p.get("entry_price", 0)) * p.get("shares", 0)
            for p in pos.values()
        )
        bankroll   = pnl.get("bankroll", 0.0)
        bk_start   = pnl.get("bankroll_start", 0.0)
        total_equity = bankroll + open_value
        if bk_start:
            sl_on = _cfg.get("STOP_LOSS_ENABLED", True)
            _live_subtitle_var.set(
                f"Follow The Whale: BUY when whale buys, SELL when whale sells | "
                f"Bankroll ${bk_start:.2f} | StopLoss: {'ON' if sl_on else 'OFF (whale-exit only)'}"
            )
        n_open = len(pos)
        if n_open > 0:
            bank_var.set(f"Equity: ${total_equity:.2f} (${bankroll:.2f}+{n_open}pos)")
        else:
            bank_var.set(f"Bank: ${bankroll:.2f}")
        pnl_var.set(f"P&L: ${total_equity - bk_start:+.3f}")
        cooldown_var.set(f"CD: {len(pnl.get('cooldown_cids', {}))}")

        if logs:
            full_log.configure(state="normal")
            full_log.delete("1.0", tk.END)
            for line in logs.splitlines()[-600:]:
                full_log.insert(tk.END, line + "\n")
            full_log.see(tk.END)
            full_log.configure(state="disabled")

        # Update position chart
        sel = pos_tree.selection()
        if not sel:
            children = pos_tree.get_children()
            if children:
                pos_tree.selection_set(children[0])
                sel = pos_tree.selection()
        if sel:
            _load_selected_position_chart()
            return
        if sel:
            vals = pos_tree.item(sel[0])['values']
            if vals:
                mkt_name = str(vals[0]).replace('💎', '').replace('⚡', '')
                outcome  = str(vals[1])
                if _show_closed[0]:
                    for p in closed:
                        if p['title'][:48] in mkt_name or mkt_name in p['title'][:48]:
                            if p.get('outcome') == outcome:
                                pos_graph.load(p.get("price_history", []), p['title'], p.get('entry_price', 0), entry_ts=float(p.get('entry_ts') or 0) or None)
                                break
                else:
                    for _, p in pos.items():
                        if p['title'][:48] in mkt_name or mkt_name in p['title'][:48]:
                            if p['outcome'] == outcome:
                                pos_graph.load(p.get("price_history", []), p['title'], p['entry_price'], entry_ts=float(p.get('entry_ts') or 0) or None)
                                break
    
    
    # ═══════════════════════════════════════════════════════════════════════════════
    #  FAST PRICE UPDATER
    # ═══════════════════════════════════════════════════════════════════════════════
    def fast_price_updater():
        from titan_market import fetch_position_price_fast
        import titan_state as _ts_mut
        import time
        _last_equity_record = [0.0]
        while True:
            try:
                positions = list(_ts_mut.env().open_positions.items())
                if positions:
                    updated = False
                    for key, pos in positions:
                        cid = pos.get("cid", key[0])
                        outcome = pos.get("outcome", key[1])
                        asset = pos.get("asset", "")
                        fast_p = fetch_position_price_fast(cid, asset, outcome)
                        if fast_p is not None and fast_p != pos.get("cur_price"):
                            pos["cur_price"] = fast_p
                            if "price_history" not in pos:
                                pos["price_history"] = []
                            pos["price_history"].append((time.time(), fast_p))
                            if len(pos["price_history"]) > 2880:
                                del pos["price_history"][:-2880]
                            updated = True
                    if updated:
                        _pending_update[0] = True
    
                # Record equity history every 5 seconds for the P&L graph,
                # but ONLY when the value actually changed — this prevents the
                # "flat plateau + vertical spike" artifact on the P&L graph.
                now = time.time()
                if now - _last_equity_record[0] >= 5.0:
                    _last_equity_record[0] = now
                    open_val = sum(
                        p.get("cur_price", p.get("entry_price", 0)) * p.get("shares", 0)
                        for p in _open_pos_dict().values()
                    )
                    eq = api.get_pnl_summary()["bankroll"] + open_val
                    # Only append if changed by > $0.005 OR it has been > 60s since last record
                    hist = api.get_pnl_summary()["equity_history"]
                    if not hist or abs(eq - hist[-1][1]) > 0.005 or (now - hist[-1][0]) > 60:
                        hist.append((now, eq))
                        if len(hist) > 10000:
                            del hist[:1000]
            except Exception as e:
                root.after(0, lambda err=e: _log_ui_error("fast price updater", err))
            time.sleep(3.0)
    
    # ═══════════════════════════════════════════════════════════════════════════════
    #  BOOT
    # ═══════════════════════════════════════════════════════════════════════════════
    def on_boot_complete():
        threading.Thread(target=fast_price_updater, daemon=True).start()
        api.subscribe("notifications/message", lambda p: on_log_cb(p.get("msg") or p.get("data", ""), p.get("level", "INFO")))
        api.subscribe("titan/position_open",   lambda p: on_position_open_cb(p))
        api.subscribe("titan/position_close",  lambda p: on_position_close_cb(p["pos"], p["pnl_usdc"], p["pnl_pct"]))
        api.subscribe("titan/cycle_complete",  lambda p: on_cycle_complete_cb(p["signals"], p["wallets"], p["rejects"], p["trades"]))
        root.after(1000, ui_refresh)
        status_var.set("🟢 LIVE — Follow The Whale | HFT Spike + Conviction")

        # ── Attach AI panel ───────────────────────────────────────────────────────
        if AIPanel is not None:
            ai_panel_cls = AIPanel
            ai_panel_cls(ai_frame, engine_module=api)

        def _boot_log():
            try:
                nonlocal _last_signals, _last_rejects, _last_wallets
                try:
                    _last_signals = _require_signal_rows(api.get_signals())
                except Exception as e:
                    root.after(0, lambda err=e: _log_ui_error("boot load signals", err, "WARN"))
                try:
                    _last_rejects = api.get_rejects() or []
                except Exception as e:
                    root.after(0, lambda err=e: _log_ui_error("boot load rejects", err, "WARN"))
                try:
                    _last_wallets = _build_wallet_cache(api.get_whales())
                except Exception as e:
                    root.after(0, lambda err=e: _log_ui_error("boot load whales", err, "WARN"))
                root.after(0, lambda: log(f"📂 Boot signals: {len(_last_signals)} signal(s), {len(_last_rejects)} reject(s), {len(_last_wallets)} whale(s)", "INFO"))
                if _last_signals or _last_rejects:
                    _pending_update[0] = True
                n_pos   = len(api.get_positions())
                n_whale = len(api.get_whales())
                eq_hist = api.get_pnl_summary().get("equity_history", [])
                n_eq    = len(eq_hist)
                if n_eq >= 2:
                    from datetime import datetime as _dt
                    first_ts = _dt.fromtimestamp(eq_hist[0][0]).strftime("%Y-%m-%d %H:%M")
                    root.after(0, lambda: log(f"📂 Boot: {n_pos} position(s) | {n_whale} whale(s) | equity history: {n_eq} pts from {first_ts}", "INFO"))
                else:
                    root.after(0, lambda: log(f"📂 Boot: {n_pos} position(s) | {n_whale} whale(s) | equity history: empty", "INFO"))
            except Exception as _be:
                _msg = str(_be)
                root.after(0, lambda: log(f"📂 Boot summary unavailable: {_msg}", "WARN"))
        threading.Thread(target=_boot_log, daemon=True).start()
    
        if telegram_notifier is not None:
            notifier = telegram_notifier
            def handle_tg_message(text: str):
                cmd = text.strip().lower()
                if cmd in ("pl", "pnl", "p&l"):
                    def _take_screenshot():
                        try:
                            from PIL import ImageGrab
                            import io
                            if root.state() == 'iconic':
                                root.deiconify()
                            root.attributes('-topmost', True)
                            nb.select(tab_pnl)
                            root.update()
                            
                            def _do_grab():
                                x = root.winfo_rootx()
                                y = root.winfo_rooty()
                                w = root.winfo_width()
                                h = root.winfo_height()
                                bbox = (x, y, x+w, y+h)
                                img = ImageGrab.grab(bbox)
                                buf = io.BytesIO()
                                img.save(buf, format='PNG')
                                buf.seek(0)
                                root.attributes('-topmost', False)
                                threading.Thread(target=notifier.send_photo, args=(buf, "Titan P&L Graph"), daemon=True).start()
                                
                            root.after(200, _do_grab)
                        except ImportError:
                            threading.Thread(target=notifier.notify_error, args=("PIL not installed.",), daemon=True).start()
                        except Exception as e:
                            print(f"Failed to capture PnL screenshot: {e}")
                    root.after(10, _take_screenshot)
    
                elif cmd in ("dash", "dashboard", "app"):
                    def _start_app_and_send():
                        global _ngrok_url
                        if not _ngrok_url:
                            try:
                                pycloudflared_module = importlib.import_module("pycloudflared")
                                try_cloudflare = getattr(pycloudflared_module, "try_cloudflare")
                                print("☁️ Starting Cloudflare tunnel...")
                                tunnel = try_cloudflare(port=8080)
                                # Try multiple possible attribute names (pycloudflared uses .tunnel)
                                _ngrok_url = getattr(tunnel, 'tunnel', getattr(tunnel, 'url', getattr(tunnel, 'tunnel_url', None)))
                                if not _ngrok_url:
                                    _ngrok_url = str(tunnel)
                                print(f"🔗 Tunnel established: {_ngrok_url}")
                            except ImportError:
                                notifier.notify_error("pycloudflared not installed. Please 'pip install pycloudflared' to enable the dashboard Web App.")
                                return
                            except Exception as e:
                                notifier.notify_error(f"Failed to start Cloudflare tunnel: {e}")
                                return
                        notifier.send_dashboard_button(_ngrok_url)
                    threading.Thread(target=_start_app_and_send, daemon=True).start()
                else:
                    def _ask_groq():
                        import requests, json
                        snapshot = api.get_snapshot(compressed=True)
                        prompt = f"System Snapshot:\n{snapshot}\n\nUser: {text}"
                        try:
                            resp = requests.post(
                                "https://api.groq.com/openai/v1/chat/completions",
                                headers={
                                    "Authorization": "Bearer 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",
                                    "Content-Type": "application/json"
                                },
                                json={
                                    "model": "llama-3.3-70b-versatile",
                                    "messages": [
                                        {"role": "system", "content": "You are TITAN AI, a trading bot assistant. Keep answers brief, under 200 words. Reference the system snapshot."},
                                        {"role": "user", "content": prompt}
                                    ]
                                },
                                timeout=30
                            )
                            if resp.status_code == 200:
                                reply = resp.json()["choices"][0]["message"]["content"]
                                notifier._send(reply, is_markdown=False)
                            else:
                                notifier._send(f"AI Error: {resp.status_code} - {resp.text}", is_markdown=False)
                        except Exception as e:
                            notifier._send(f"AI Exception: {e}", is_markdown=False)
                    threading.Thread(target=_ask_groq, daemon=True).start()
    
            notifier.start_polling(handle_tg_message)
    
            # Background server for dashboard
            import http.server
            import json
            class DashboardHandler(http.server.SimpleHTTPRequestHandler):
                def log_message(self, format, *args):
                    pass
                def do_GET(self):
                    if self.path == '/api/data':
                        self.send_response(200)
                        self.send_header('Content-Type', 'application/json')
                        self.send_header('Access-Control-Allow-Origin', '*')
                        self.end_headers()
    
                        whales = sorted(_wallet_cache().values(), key=lambda x: x.get("score", 0), reverse=True)[:10]
                        signals: list[SignalDict] = _last_signals[:15] if _last_signals else []
    
                        # Calculate equity
                        open_value = sum(pos.get("cur_price", pos.get("entry_price", 0)) * pos.get("shares", 0) for pos in _open_pos_dict().values())
                        total_equity = api.get_pnl_summary()["bankroll"] + open_value
    
                        data = {
                            "last_update": int(time.time() * 1000),
                            "stats": {
                                "equity": total_equity,
                                "bankroll": api.get_pnl_summary()["bankroll"],
                                "start_bankroll": api.get_pnl_summary()["bankroll_start"],
                                "session_pnl": api.get_pnl_summary()["session_pnl"],
                                "open_pos_count": len(_open_pos_dict()),
                                "total_trades": api.get_trade_stats()["sell_count"]
                            },
                            "pnl_history": [round(v, 4) for _, v in (api.get_pnl_summary()["equity_history"][-200:] if api.get_pnl_summary()["equity_history"] else [])],
                            "whales": [
                                {"wallet": w.get("wallet", ""), "name": w.get("name", "Unknown"), "pnl": w.get("total_pnl", 0), "volume": w.get("volume", 0), "score": w.get("score", 0)} for w in whales
                            ],
                            "signals": [
                                {"question": s.get("title", ""), "outcome": s.get("outcome", ""), "suggested_bet": s.get("bet", 0), "current_price": s.get("cur", 0), "ev_edge": _signal_ev_pct(s) / 100, "confluence_count": s.get("n_confluence", 0)} for s in signals
                            ],
                            "open_positions": [
                                {"title": p.get("title", ""), "outcome": p.get("outcome", ""), "entry": p.get("entry_price", 0), "cur": p.get("cur_price", 0), "shares": p.get("shares", 0), "pnl": (p.get("cur_price",0) - p.get("entry_price",0)) * p.get("shares",0)}
                                for p in sorted(_open_pos_dict().values(), key=lambda x: x.get("entry_ts", 0), reverse=True)
                            ],
                            "history": [
                                {"title": p.get("title", ""), "outcome": p.get("outcome", ""), "pnl": p.get("pnl_usdc", 0), "pct": (p.get("pnl_pct") or 0) / 100}
                                for p in api.get_closed_positions(limit=10)
                            ]
                        }
                        self.wfile.write(json.dumps(data).encode('utf-8'))
                    elif self.path == '/' or self.path.endswith('.html'):
                        self.send_response(200)
                        self.send_header('Content-Type', 'text/html')
                        self.end_headers()
                        try:
                            with open("dashboard.html", "rb") as f:
                                self.wfile.write(f.read())
                        except Exception:
                            self.wfile.write(b"Dashboard HTML missing")
                    else:
                        self.send_error(404)
    
            threading.Thread(target=lambda: http.server.HTTPServer(('127.0.0.1', 8080), DashboardHandler).serve_forever(), daemon=True).start()
    

    show_loading_screen(root, api, on_boot_complete)
    root.mainloop()


if __name__ == "__main__":
    import sys, os as _os
    sys.path.insert(0, _os.path.dirname(__file__))
    from titan_api import TitanAPI as _TitanAPI
    api = _TitanAPI()
    api.start()
    run_ui(api)
