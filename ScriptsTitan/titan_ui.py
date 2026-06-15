"""
╔══════════════════════════════════════════════════════════════════════════════╗
║  TITAN — SINGLE WALLET UI                                                    ║
║                                                                              ║
║  Tabs: SIGNALS · ALERTS · POSITIONS · P&L · WALLETS · ANALYSIS · DIAG · LOG · CONFIG
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
import traceback
from dataclasses import dataclass, fields, is_dataclass
from datetime import datetime
from types import TracebackType
from typing import TYPE_CHECKING
from titan_protocol import TitanBackend
from titan_client import _log as _client_log
from titan_types import PnlSummaryDict, TradeStatsDict
    
import os
import webbrowser
from pathlib import Path
from titan_ui_charts import PnLChart, PositionChart, ChartMarker, init_chart_fonts
from titan_wallet import WalletTier

if TYPE_CHECKING:
    from titan_signals import Signal
    from titan_position import Position
    from titan_trade import TradeRecord
    from titan_market import Market, WalletObservation
    from titan_wallet import Wallet

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


@dataclass
class UiRefreshData:
    pnl: PnlSummaryDict | None = None
    logs: str | None = None
    positions: list[Position] | None = None
    wallets: dict[str, Wallet] | None = None
    signals_live: list[Signal] | None = None
    signal_history: list[Signal] | None = None
    closed_positions: list[Position] | None = None
    trade_stats: TradeStatsDict | None = None
    trade_history: list[TradeRecord] | None = None


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
    "    🐳  TRACKED WALLETS MIRROR ENGINE  —  SINGLE WALLET  🐳",
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
        col = "#00ff88" if ("TITAN" in line or "WALLET" in line) else "#1a4a2a"
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
        ("Connecting to engine...", lambda: api.get_status()),
        ("Loading P&L and bankroll state...", lambda: api.get_pnl_summary()),
        ("Fetching tracked wallet roster...", lambda: api.get_tracked_wallets()),
        ("Syncing open positions...", lambda: api.get_positions()),
        ("Downloading latest signals...", lambda: api.get_signals()),
        ("TITAN ONLINE — Follow The Wallet", lambda: None),
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
            except Exception as e:
                _client_log(f"Boot task '{label}' failed: {e}", "ERR")
        current_step[0] = total_steps

    draw_bar(0.0)
    threading.Thread(target=run_tasks, daemon=True).start()
    root.after(80, update_ui)


# ═══════════════════════════════════════════════════════════════════════════════
#  ROOT WINDOW
# ═══════════════════════════════════════════════════════════════════════════════

def run_ui(api: TitanBackend) -> None:
    _cfg: dict = {}
    _open_positions_cache: list[Position] | None = None
    _wallet_cache_state: dict[str, Wallet] | None = None

    def _open_positions() -> list[Position]:
        if _open_positions_cache is not None:
            return _open_positions_cache
        try:
            return api.get_positions()
        except Exception as e:
            _log_ui_error("open positions cache", e)
            return []
    def _wallet_cache() -> "dict[str, Wallet]":
        if _wallet_cache_state is not None:
            return _wallet_cache_state
        try:
            return {w.addr: w for w in api.get_tracked_wallets()}
        except Exception as e:
            _log_ui_error("wallet cache", e)
            return {}

    def _ensure_wallet_in_cache(wallet_addr: str) -> "Wallet | None":
        wallet_key = wallet_addr.lower()
        cached_wallet = _wallet_cache().get(wallet_key)
        if cached_wallet is not None:
            return cached_wallet
        log(f"[wallet cache] wallet {wallet_key} missing locally; attempting recovery", "DEBUG")
        try:
            fetched_wallets = api.get_tracked_wallets(search=wallet_key)
        except Exception as e:
            _log_ui_error(f"fetch wallet {wallet_key}", e, "WARN")
            return None
        for fetched_wallet in fetched_wallets:
            fetched_addr = str(fetched_wallet.addr).lower()
            _wallet_cache()[fetched_addr] = fetched_wallet
        if not fetched_wallets:
            log(f"[wallet cache] recovery failed for {wallet_key}: backend returned no wallet match", "WARN")
            return None
        recovered_wallet = _wallet_cache().get(wallet_key)
        if recovered_wallet is None:
            log(f"[wallet cache] recovery failed for {wallet_key}: backend returned results but target wallet was not cached", "WARN")
            return None
        log(f"[wallet cache] recovered wallet {wallet_key} into local cache", "INFO")
        return recovered_wallet

    def _warm_position_wallet_cache(pos: Position) -> None:
        seen_wallets: set[str] = set()
        missing_wallets: list[str] = []
        for wallet_addr in pos.elite_wallets + pos.tracked_wallets:
            wallet_key = str(wallet_addr).lower()
            if not wallet_key or wallet_key in seen_wallets:
                continue
            seen_wallets.add(wallet_key)
            if _wallet_cache().get(wallet_key) is None:
                missing_wallets.append(wallet_key)
        if not missing_wallets:
            return
        log(f"[position detail] warming wallet cache for {len(missing_wallets)} wallet(s)", "DEBUG")
        for wallet_addr in missing_wallets:
            _ensure_wallet_in_cache(wallet_addr)


    root = tk.Tk()

    root.title("🐳 TITAN — Tracked Wallet Mirror Engine")
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

    app_title_var    = tk.StringVar(value="🐳 TITAN — Tracked Wallet Mirror Engine")
    app_subtitle_var = tk.StringVar(value="Follow The Wallet | ⚡ HFT: ON | v10 CONVICTION-ONLY | 2+ Elites | 20-72¢ Zone | -30% Stop | ENGINE ACTIVE")

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
    
    sig_cols = ("Sc","Market (Full Title)","Side","WEntry$","Now$","Drift%","Age","Flow$","Wallets","Mode")
    sig_tree = ttk.Treeview(tab_live, columns=sig_cols, show="headings", height=10)
    sw = {"Sc":45,"Market (Full Title)":420,"Side":110,"WEntry$":72,"Now$":72,
          "Drift%":65,"Age":50,"Flow$":80,"Wallets":55,"Mode":65}
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
    sig_tree.pack(fill="both", expand=True, padx=4, pady=(4,2))
    _signal_tree_items: dict[str, Signal] = {}
    
    _live_subtitle_var = tk.StringVar(value="Follow The Wallet: BUY when wallet buys, SELL when wallet sells | connecting...")
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
    tk.Label(sig_btn_bar, text="Current cycle or recent DB history", fg="#334455",
             bg="#080810", font=mono_sm).pack(side="left", padx=8)
    
    
    # ═══════════════════════════════════════════════════════════════════════════════
    #  TAB 2: SNIPER ALERTS
    # ═══════════════════════════════════════════════════════════════════════════════
    tab_alerts = tk.Frame(nb, bg="#080810")
    alert_txt = scrolledtext.ScrolledText(tab_alerts, bg="#060610",
        fg="#00ff88", font=mono_lg, selectbackground="#1a2a4a", wrap=tk.WORD)
    alert_txt.pack(fill="both", expand=True, padx=4, pady=4)
    
    
    # ═══════════════════════════════════════════════════════════════════════════════
    #  TAB 3: OPEN POSITIONS
    # ═══════════════════════════════════════════════════════════════════════════════
    tab_positions = tk.Frame(nb, bg="#080810")
    
    pos_hdr = tk.Frame(tab_positions, bg="#001820", pady=4)
    pos_hdr.pack(fill="x", padx=4, pady=(4,0))
    tk.Label(pos_hdr, text="🤖  OPEN POSITIONS  (auto-trading active — following wallet exits)",
             fg="#00aaff", bg="#001820", font=bold_hd).pack(side="left", padx=8)
    tk.Label(pos_hdr,
             text=f"Exits: wallet sells → immediate | +{_cfg.get("PROFIT_TARGET_PCT", 0.20)*100:.0f}% target | "
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
    pos_tree.tag_configure("DEAD",    foreground="#fff1f1", background="#7a0018")
    pos_tree.tag_configure("CLOSED_WIN",  foreground="#007733", background="#000f00")
    pos_tree.tag_configure("CLOSED_LOSS", foreground="#882222", background="#0f0000")
    
    pos_vsb = tk.Scrollbar(tab_positions, command=pos_tree.yview)
    pos_tree.configure(yscrollcommand=pos_vsb.set)
    pos_vsb.pack(side="right", fill="y")
    pos_tree.pack(fill="x", padx=4, pady=(4,2))
    
    
    def show_position_detail(pos: Position) -> None:
        """Show a floating detail popup for a position."""
        import time as _t
        win = tk.Toplevel(root)
        win.title(f"Position Detail — {pos.title[:50]}")
        win.configure(bg="#060615")
        win.geometry("820x760")
        win.resizable(True, True)
        _warm_position_wallet_cache(pos)

        mono10  = font.Font(family="Courier", size=10)
        mono9   = font.Font(family="Courier", size=9)
        bold11  = font.Font(family="Courier", size=11, weight="bold")
        bold9   = font.Font(family="Courier", size=9, weight="bold")

        entry    = pos.entry_price
        w_entry  = pos.avg_entry or entry
        cur      = pos.cur_price or entry
        shares   = pos.shares
        bet      = pos.bet
        pnl_pct  = (cur - entry) / max(entry, 0.001) * 100
        pnl_usd  = (cur - entry) * shares
        end_ts   = pos.exit_ts if pos.exit_ts > 0 else time.time()
        hold_min = (end_ts - pos.entry_ts) / 60 if pos.entry_ts else 0.0
        title    = pos.title
        outcome  = pos.outcome
        cid      = pos.cid
        slug     = pos.slug or pos.event_slug
        entry_ts_text = pos.entry_dt.strftime("%Y-%m-%d %H:%M:%S") if pos.entry_ts > 0 else ""
        exit_ts_text : str = pos.exit_dt.strftime("%Y-%m-%d %H:%M:%S") if pos.exit_ts > 0 else ""
        price_label = "Exit Price" if pos.exit_ts else "Current Price"

        # Header
        hf = tk.Frame(win, bg="#0a0a20", pady=8)
        hf.pack(fill="x", padx=8, pady=(8,0))
        pnl_color = "#00ff55" if pnl_pct >= 0 else "#ff5555"
        tier_icon = "💎" if pos.is_conviction else ("⚡" if pos.is_hft else "")
        tk.Label(hf, text=f"{tier_icon}[{pos.tier}]  {title}",
                 fg="#00aaff", bg="#0a0a20", font=bold11, wraplength=780, justify="left").pack(anchor="w", padx=12)
        if pos.dead_wallets:
            dead_wallet_names = [
                (p.name if (p := _wallet_cache().get(str(wallet_addr).lower())) else None) or str(wallet_addr)[:16] + "…"
                for wallet_addr in pos.dead_wallets[:2]
            ]
            dead_wallet_text = ", ".join(dead_wallet_names)
            if len(pos.dead_wallets) > 2:
                dead_wallet_text += f" +{len(pos.dead_wallets) - 2}"
            tk.Label(hf, text=f"☠ DEAD WALLET  EXIT TRIGGER BLIND  {dead_wallet_text}",
                     fg="#fff1f1", bg="#7a0018", font=font.Font(family="Courier", size=11, weight="bold"),
                     padx=10, pady=4, wraplength=780, justify="left").pack(anchor="w", padx=12, pady=(0, 4))
        tk.Label(hf, text=f"OUTCOME: {outcome or '—'}",
                 fg="#fff4b0", bg="#4a2a00", font=font.Font(family="Courier", size=11, weight="bold"),
                 padx=10, pady=4,
                 wraplength=780, justify="left").pack(anchor="w", padx=12, pady=(4, 4))
        if entry_ts_text:
            tk.Label(hf, text=f"ENTRY TIME  {entry_ts_text}",
                     fg="#ffdd44", bg="#0a0a20", font=bold11, wraplength=780, justify="left").pack(anchor="w", padx=12, pady=(2,0))

        if exit_ts_text:
            tk.Label(hf, text=f"EXIT TIME   {exit_ts_text}",
                     fg="#ff8844", bg="#0a0a20", font=bold11, wraplength=780, justify="left").pack(anchor="w", padx=12, pady=(2,0))
        tk.Label(hf, text=f"Score: {pos.score:.0f}pts   CID: {cid[:30]}…",
                 fg="#556677", bg="#0a0a20", font=mono9).pack(anchor="w", padx=12)

        # Stats grid
        sf2 = tk.Frame(win, bg="#060615")
        sf2.pack(fill="x", padx=8, pady=6)

        def stat_cell(parent, label, value, color="#aaaacc", col=0, row=0):
            f = tk.Frame(parent, bg="#0d0d20", bd=1, relief="solid")
            f.grid(row=row, column=col, padx=4, pady=3, sticky="nsew")
            tk.Label(f, text=label, fg="#445566", bg="#0d0d20", font=mono9, pady=2).pack()
            tk.Label(f, text=value, fg=color, bg="#0d0d20", font=bold9, pady=2).pack()

        hold_label = f"{hold_min:.0f} min"
        if hold_min >= 60:
            total_minutes = max(int(hold_min), 0)
            hold_days = total_minutes // (24 * 60)
            hold_hours = (total_minutes % (24 * 60)) // 60
            hold_minutes = total_minutes % 60
            hold_label = f"{hold_days}:{hold_hours:02d}:{hold_minutes:02d}"

        stats_data = [
            ("Wallet Entry",   f"${w_entry:.4f}",      "#ffaa44"),
            ("Our Entry",     f"${entry:.4f}",         "#aaaaff"),
            (price_label,     f"${cur:.4f}",           pnl_color),
            ("P&L $",         f"${pnl_usd:+.4f}",      pnl_color),
            ("P&L %",         f"{pnl_pct:+.2f}%",      pnl_color),
            ("Bet Size",      f"${bet:.2f}",           "#00aaff"),
            ("Shares",        f"{shares:.2f}",         "#aaaacc"),
            ("Held",          hold_label,              "#888888"),
            ("Liq",           f"${pos.liq:,.0f}",      "#446688"),
            ("Score",         f"{pos.score:.0f}",      "#ffdd44"),
            ("Tier",          pos.tier,                "#ff8844"),
            ("Type",          "HFT⚡" if pos.is_hft else ("💎CONVICTION" if pos.is_conviction else "STANDARD"), "#aaaacc"),
        ]
        for i, (lbl, val, col) in enumerate(stats_data):
            sf2.columnconfigure(i % 4, weight=1)
            stat_cell(sf2, lbl, val, col, i % 4, i // 4)

        # Wallets
        wf = tk.Frame(win, bg="#060615")
        wf.pack(fill="x", padx=8)
        tk.Label(wf, text="SELECTED WALLETS", fg="#00ff88", bg="#060615", font=bold9).pack(anchor="w", padx=4, pady=(4,2))
        seen_wallets: set[str] = set()
        elite_wallets: list[str] = []
        for wallet_addr in pos.elite_wallets + pos.tracked_wallets:
            wallet_key = str(wallet_addr).lower()
            if wallet_key in seen_wallets:
                continue
            seen_wallets.add(wallet_key)
            elite_wallets.append(str(wallet_addr))
        wallet_cols = ("name", "cash", "wr", "pnl", "score")
        wallet_tree = ttk.Treeview(wf, columns=wallet_cols, show="headings", height=3)
        wallet_tree.heading("name", text="Name")
        wallet_tree.heading("cash", text="Cash")
        wallet_tree.heading("wr", text="WR")
        wallet_tree.heading("pnl", text="PnL")
        wallet_tree.heading("score", text="Score")
        wallet_tree.column("name", width=250, anchor="w", stretch=True)
        wallet_tree.column("cash", width=90, anchor="e", stretch=False)
        wallet_tree.column("wr", width=70, anchor="e", stretch=False)
        wallet_tree.column("pnl", width=90, anchor="e", stretch=False)
        wallet_tree.column("score", width=70, anchor="e", stretch=False)
        wallet_tree.pack(fill="x", padx=4, pady=(0,4))

        wallet_names = pos.wallet_names
        wallet_cash = pos.wallet_buy_cash
        wallet_tree_addrs: list[str] = []
        for i, w_addr in enumerate(elite_wallets[:8]):
            wallet_lookup = w_addr.lower()
            prof = _wallet_cache().get(wallet_lookup)
            if prof is not None and prof.dead:
                name = prof.name or (w_addr[:16] + "…")
            else:
                name = (wallet_names[i] if i < len(wallet_names) else None) or (prof.name if prof else w_addr[:16] + "…")
            wr = (prof.win_rate if prof else 0.0) * 100
            pnl_w = prof.total_pnl if prof else 0.0
            cash_value = wallet_cash.get(wallet_lookup, wallet_cash.get(w_addr, 0.0))
            wallet_tree.insert(
                "",
                "end",
                values=(
                    f"{'☠ ' if prof and prof.dead else ''}{'⚡' if prof and prof.hft else ''}{name}",
                    f"${cash_value:,.0f}",
                    f"{wr:.0f}%",
                    f"${pnl_w:+,.0f}",
                    f"{prof.score if prof else 0.0:.2f}",
                ),
            )
            wallet_tree_addrs.append(wallet_lookup)

        def _open_selected_wallet(event: tk.Event[tk.Misc]) -> None:
            item_id = wallet_tree.identify_row(event.y)
            if not item_id:
                return
            idx = wallet_tree.index(item_id)
            if idx >= len(wallet_tree_addrs):
                return
            addr = wallet_tree_addrs[idx]
            prof = _ensure_wallet_in_cache(addr)
            if prof is None:
                log(f"[position detail] wallet recovery failed for {addr}", "WARN")
                return
            show_whale_detail(addr, prof)

        wallet_tree.bind("<Double-1>", _open_selected_wallet)
    
        tf = tk.Frame(win, bg="#060615")
        tf.pack(fill="x", padx=8, pady=(6, 0))
        tk.Label(tf, text="TRADES", fg="#00ff88", bg="#060615", font=bold9).pack(anchor="w", padx=4, pady=(4,2))

        trade_cols = ("type", "time", "price", "shares", "bet", "pnl")
        trade_tree = ttk.Treeview(tf, columns=trade_cols, show="headings", height=2)
        trade_tree.heading("type", text="Type")
        trade_tree.heading("time", text="Time")
        trade_tree.heading("price", text="Price")
        trade_tree.heading("shares", text="Shares")
        trade_tree.heading("bet", text="Bet")
        trade_tree.heading("pnl", text="P&L")
        trade_tree.column("type", width=60, anchor="center")
        trade_tree.column("time", width=150, anchor="w")
        trade_tree.column("price", width=80, anchor="e")
        trade_tree.column("shares", width=80, anchor="e")
        trade_tree.column("bet", width=80, anchor="e")
        trade_tree.column("pnl", width=90, anchor="e")
        trade_tree.pack(fill="x", padx=4, pady=(0,4))

        position_trades: list[TradeRecord] = [pos.buy_trade]
        if pos.sell_trade is not None:
            position_trades.append(pos.sell_trade)

        trade_items: dict[str, TradeRecord] = {}
        for trade in position_trades:
            pnl_value = "â€”"
            if trade.type == "SELL":
                pnl_value = f"${(trade.pnl_usdc or 0.0):+.4f}"
            item_id = trade_tree.insert(
                "",
                "end",
                values=(
                    trade.type or "?",
                    trade.ts_str,
                    f"${trade.price:.4f}",
                    f"{trade.shares:.2f}",
                    f"${trade.bet:.2f}",
                    pnl_value,
                ),
            )
            trade_items[str(item_id)] = trade

        def _open_selected_trade(_event: tk.Event[tk.Misc]) -> None:
            selection = trade_tree.selection()
            if not selection:
                return
            trade = trade_items.get(str(selection[0]))
            if trade is None:
                return
            show_trade_history_detail(trade)

        trade_tree.bind("<Double-1>", _open_selected_trade)

        # Links
        lf = tk.Frame(win, bg="#060615")
        lf.pack(fill="x", padx=8, pady=6)


        def open_polymarket() -> None:
            log(f"Opening Polymarket URL: {pos.market_url}", "DEBUG")
            pos.open_on_polymarket()

        def open_market_detail() -> None:
            market: Market | None = None
            try:
                market = _market_payload_from_value(pos.market)
            except LookupError:
                pass
            if market is None:
                market = _load_market_payload(cid=pos.cid, asset=pos.asset)
            if market is None:
                log("[position detail] market payload missing", "WARN")
                return
            show_market_detail(market, signal_title=title)
    
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
        tk.Button(lf, text="📈 Market", bg="#10203a", fg="#88ccff",
                  font=mono9, padx=10, command=open_market_detail).pack(side="left", padx=4)
        tk.Button(lf, text="📋 Copy Title", bg="#1a2a1a", fg="#00ff88",
                  font=mono9, padx=10, command=copy_title).pack(side="left", padx=4)
        tk.Button(lf, text="🔎 Inspect Raw", bg="#201a2a", fg="#d0b0ff",
                  font=mono9, padx=10, command=inspect_raw_data).pack(side="left", padx=4)
        tk.Button(lf, text="🧩 Properties", bg="#2a2012", fg="#ffcc88",
                  font=mono9, padx=10, command=open_properties).pack(side="left", padx=4)
        
        url_lbl = tk.Label(lf, text=pos.market_url[:80], fg="#334455", bg="#060615", font=mono9)
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
            ph = pos.price_history
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
    
    
    def show_trade_history_detail(trade: TradeRecord ) -> None:
        """Show a floating detail popup for a closed trade."""
        win = tk.Toplevel(root)
        typ = trade.type or "?"
        win.title(f"Trade Detail — {trade.title[:50]}")
        win.configure(bg="#060615")
        win.geometry("760x500")

        mono9  = font.Font(family="Courier", size=9)
        bold9  = font.Font(family="Courier", size=9, weight="bold")
        bold11 = font.Font(family="Courier", size=11, weight="bold")


        pnl_u = trade.pnl_usdc or 0
        pnl_p = trade.pnl_pct or 0
        pnl_color = "#00ff55" if pnl_u >= 0 else "#ff5555"
        title = trade.title or "Unknown"
        slug  = trade.slug or trade.event_slug
    
        hf = tk.Frame(win, bg="#0a0a20", pady=8)
        hf.pack(fill="x", padx=8, pady=(8,0))
        icon = "🛒" if typ == "BUY" else ("✅" if pnl_u >= 0 else "❌")
        tk.Label(hf, text=f"{icon} [{typ}] [{trade.tier or '?'}]  {title}",
                 fg="#00aaff" if typ == "BUY" else pnl_color, bg="#0a0a20", font=bold11,
                 wraplength=730, justify="left").pack(anchor="w", padx=12)
        tk.Label(hf, text=f"Outcome: {trade.outcome}   Time: {trade.ts_str}",
                 fg="#556677", bg="#0a0a20", font=mono9).pack(anchor="w", padx=12)
    
        sf2 = tk.Frame(win, bg="#060615")
        sf2.pack(fill="x", padx=8, pady=6)
    
        def stat_cell(parent, label, value, color="#aaaacc", col=0, row=0):
            f = tk.Frame(parent, bg="#0d0d20", bd=1, relief="solid")
            f.grid(row=row, column=col, padx=4, pady=3, sticky="nsew")
            tk.Label(f, text=label, fg="#445566", bg="#0d0d20", font=mono9, pady=2).pack()
            tk.Label(f, text=value, fg=color, bg="#0d0d20", font=bold9, pady=2).pack()
        stats_data = [
            ("Wallet Entry",  f"${trade.avg_entry:.4f}",                          "#ffaa44"),
            ("Price",        f"${trade.price:.4f}",                          "#aaaaff"),
            #("Exit Price",   f"${exit_p:.4f}" if exit_p else "—",        pnl_color if typ=="SELL" else "#888888"),
            ("P&L $",        f"${pnl_u:+.4f}" if typ=="SELL" else "—",   pnl_color),
            ("P&L %",        f"{pnl_p:+.1f}%" if typ=="SELL" else "—",   pnl_color),
            ("Bet Size",     f"${trade.bet:.2f}",                        "#00aaff"),
            ("Tier",         trade.tier or "?",                          "#ff8844"),
            ("Bankroll @",   f"${trade.bankroll:.3f}",                   "#778899"),
        ]
        for i, (lbl, val, col) in enumerate(stats_data):
            sf2.columnconfigure(i % 4, weight=1)
            stat_cell(sf2, lbl, val, col, i % 4, i // 4)
    
        # Wallets — show name + how much each wallet put into this trade
        wf = tk.Frame(win, bg="#060615")
        wf.pack(fill="x", padx=8)
        wallet_names = trade.wallet_names
        whale_addrs = trade.elite_wallets
        whale_cash  = trade.wallet_buy_cash  # addr → $ amount
        if wallet_names or whale_addrs:
            tk.Label(wf, text="VIA WALLETS:", fg="#00ff88", bg="#060615", font=mono9).pack(anchor="w", padx=12, pady=(4,2))
            for i, name in enumerate(wallet_names[:6]):
                addr = whale_addrs[i] if i < len(whale_addrs) else ""
                cash_val = whale_cash.get(addr.lower(), whale_cash.get(addr, 0))
                cash_str = f"  —  put in ${cash_val:,.0f}" if cash_val > 0 else ""
                prof = _wallet_cache().get(addr.lower())
                wr   = (prof.win_rate if prof else 0) * 100
                pnl_w = prof.total_pnl if prof else 0
                detail = f"WR:{wr:.0f}%  PnL:${pnl_w:+,.0f}" if wr or pnl_w else ""
                tk.Label(wf,
                         text=f"  🐋 {name:<22}{cash_str}   {detail}",
                         fg="#00cc88", bg="#060615", font=mono9).pack(anchor="w", padx=16)
    
        # Exit reason
        reason = trade.reason
        if reason:
            tk.Label(win, text=f"Exit reason: {reason}",
                     fg="#ffaa44", bg="#060615", font=mono9).pack(anchor="w", padx=20)
    
        # Links
        lf = tk.Frame(win, bg="#060615")
        lf.pack(fill="x", padx=8, pady=8)
        #market_url = f"https://polymarket.com/event/{slug}" if slug else "https://polymarket.com"
    
        def open_polymarket():
            trade.open_on_polymarket()

        def open_market_detail() -> None:
            market: Market | None = _load_market_payload(cid=trade.cid, asset=trade.asset)
            if market is None:
                log("[trade detail] market payload missing", "WARN")
                return
            show_market_detail(market, signal_title=title)
    
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
    
    
    def show_whale_detail(wallet: str, whale: "Wallet") -> None:
        win = tk.Toplevel(root)
        whale_name = whale.name
        win.title(f"Whale Detail — {whale_name[:50]}")
        win.configure(bg="#060615")
        win.geometry("760x672")
        win.resizable(True, True)

        mono10 = font.Font(family="Courier", size=10)
        mono9 = font.Font(family="Courier", size=9)
        bold9 = font.Font(family="Courier", size=9, weight="bold")
        bold11 = font.Font(family="Courier", size=11, weight="bold")

        score      = whale.score
        total_pnl  = whale.total_pnl
        pnl_color  = "#00ff55" if total_pnl >= 0 else "#ff5555"
        verified   = whale.verified
        elite      = whale.elite
        hft        = whale.hft
        vip        = whale.vip
        icon       = "🔥" if elite else ("✅" if verified else "👁")

        hf = tk.Frame(win, bg="#0a0a20", pady=8)
        hf.pack(fill="x", padx=8, pady=(8, 0))
        tk.Label(hf, text=f"{icon} {whale_name}{' ⭐VIP' if vip else ''}{' ⚡HFT' if hft else ''}",
                 fg="#00aaff" if verified or elite else "#aaaaaa", bg="#0a0a20",
                 font=bold11, wraplength=730, justify="left").pack(anchor="w", padx=12)
        tk.Label(hf, text=f"Wallet: {wallet}",
                 fg="#8899bb", bg="#0a0a20", font=mono9, wraplength=730,
                 justify="left").pack(anchor="w", padx=12)

        sf2 = tk.Frame(win, bg="#060615")
        sf2.pack(fill="x", padx=8, pady=6)

        def stat_cell(parent: tk.Misc, label: str, value: str, color: str = "#aaaacc", col: int = 0, row: int = 0) -> None:
            f = tk.Frame(parent, bg="#161628", bd=1, relief="solid")
            f.grid(row=row, column=col, padx=4, pady=3, sticky="nsew")
            tk.Label(f, text=label, fg="#7788bb", bg="#161628", font=mono9, pady=2).pack()
            tk.Label(f, text=value, fg=color, bg="#161628", font=bold9, pady=2).pack()

        sports_bot  = whale.sports_bot
        pnl_pct     = whale.pnl_pct or 0.0
        avg_profit  = whale.avg_profit or 0.0
        alpha_pt    = whale.alpha_per_trade or 0.0
        type_parts  = []
        if vip:        type_parts.append("VIP")
        if hft:        type_parts.append("HFT")
        if sports_bot: type_parts.append("SPORTS")
        type_label = " ".join(type_parts) if type_parts else "STANDARD"

        for c in range(4):
            sf2.columnconfigure(c, weight=1)

        def section_header(parent: tk.Misc, text: str, grid_row: int) -> None:
            tk.Label(parent, text=f"── {text} ──", fg="#8899cc", bg="#060615",
                     font=mono9, anchor="w").grid(
                row=grid_row, column=0, columnspan=4, sticky="w", padx=6, pady=(6, 1))

        global_cells = [
            ("Score",    f"{score:.2f}",                                                       "#ffdd44"),
            ("Status",   "ELITE" if elite else ("VERIFIED" if verified else "WATCH / REJECT"), "#ff8844"),
            ("Type",     type_label,                                                            "#aaaacc"),
            ("LB Rank",  f"#{whale.lb_rank:,}" if whale.lb_rank else "—",                     "#aaaacc"),
            ("Portfolio",f"${whale.total_value:,.0f}",                                         "#00aaff"),
            ("PnL",      f"${total_pnl:+,.0f}",                                                pnl_color),
            ("PnL %",    f"{pnl_pct:+.1f}%", "#00ff88" if pnl_pct >= 0 else "#ff5555"),
            ("Volume",   f"${whale.lb_vol:,.0f}" if whale.lb_vol else "—",                    "#88ccff"),
        ]

        trade_count_str = f"{whale.loaded_trade_count}{'*' if whale.trade_load_limited else ''}"
        loaded_cells = [
            ("Win Rate",    f"{whale.win_rate * 100:.0f}%",                                    "#00ff88"),
            ("Wilson LB",   f"{whale.wilson_lb * 100:.0f}%",                                   "#88ccff"),
            ("Resolved",    f"{whale.n_resolved}",                                              "#aaaacc"),
            ("Loaded",      trade_count_str, "#ff8844" if whale.trade_load_limited else "#aaaacc"),
            ("TPH",         f"{whale.trades_per_hour:.1f}",                                     "#aaaacc"),
            ("Avg Bet",     f"${whale.avg_bet:,.0f}",                                           "#ffaa44"),
            ("Avg Profit",  f"${avg_profit:+.1f}", "#00ff88" if avg_profit >= 0 else "#ff5555"),
            ("Alpha/Trade", f"${alpha_pt:+.1f}",   "#00ff88" if alpha_pt >= 0 else "#ff5555"),
            ("30d PnL",     f"${(whale.recent_pnl_30d or 0.0):+,.0f}",                        "#88ccff"),
            ("7d PnL",      f"${(whale.recent_pnl_7d or 0.0):+,.0f}",                         "#88ccff"),
        ]

        grid_row = 0
        section_header(sf2, "GLOBAL", grid_row); grid_row += 1
        for i, (lbl, val, col) in enumerate(global_cells):
            stat_cell(sf2, lbl, val, col, i % 4, grid_row + i // 4)
        grid_row += math.ceil(len(global_cells) / 4)

        section_header(sf2, "LOADED TRADES", grid_row); grid_row += 1
        for i, (lbl, val, col) in enumerate(loaded_cells):
            stat_cell(sf2, lbl, val, col, i % 4, grid_row + i // 4)

        info_f = tk.Frame(win, bg="#060615")
        info_f.pack(fill="x", padx=8, pady=(0, 6))
        tk.Label(info_f, text="DETAILS", fg="#00ff88", bg="#060615", font=bold9).pack(anchor="w", padx=4, pady=(4, 2))
        detail_lines = [
            f"  Verified: {'yes' if verified else 'no'}",
            f"  Elite: {'yes' if elite else 'no'}",
            f"  VIP: {'yes' if vip else 'no'}",
            f"  HFT: {'yes' if hft else 'no'}",
            f"  Sports bot: {'yes' if sports_bot else 'no'}",
            f"  Watchable: {'yes' if whale.watchable else 'no'}",
        ]
        if whale.fail_reasons:
            detail_lines.append(f"  Fail reasons: {', '.join(str(x) for x in whale.fail_reasons[:8])}")
        for line in detail_lines:
            tk.Label(info_f, text=line, fg="#ddeeff", bg="#060615", font=mono10,
                     anchor="w", justify="left", wraplength=720).pack(anchor="w", padx=12)

        lf = tk.Frame(win, bg="#060615")
        lf.pack(fill="x", padx=8, pady=6)
        profile_wallet = whale.addr
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
            show_raw_data_popup(whale.to_wire(), title=f"Whale Raw Data - {whale_name}")

        def open_properties() -> None:
            show_properties_popup(whale.to_wire(),
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

    def _position_cid_or_empty(pos: Position) -> str:
        if pos.buy_trade is not None and pos.buy_trade.cid:
            return str(pos.buy_trade.cid)
        if pos.sell_trade is not None and pos.sell_trade.cid:
            return str(pos.sell_trade.cid)
        return ""

    def _closed_position_selection_key(pos: Position) -> tuple[str, str, str, str]:
        return (
            _position_cid_or_empty(pos),
            str(pos.title or ""),
            str(pos.outcome or ""),
            str(pos.exit_ts or ""),
        )

    _closed_tree_items: dict[str, Position] = {}
    _open_tree_items: dict[str, Position] = {}
    _last_chart_signature: list[object | None] = [None]
    _closed_chart_loads_inflight: set[tuple[str, float, float]] = set()

    def _closed_position_history_request_key(pos: Position) -> tuple[str, float, float]:
        return (str(pos.asset or ""), float(pos.entry_ts or 0.0), float(pos.exit_ts or 0.0))

    def _request_closed_position_chart_history(pos: Position) -> None:
        request_key = _closed_position_history_request_key(pos)
        if request_key in _closed_chart_loads_inflight:
            return

        asset = str(pos.asset or "").strip()
        if not asset:
            pos.price_history = []
            pos.price_history_source = ""
            pos.price_history_error = "Closed position has no asset"
            return

        _closed_chart_loads_inflight.add(request_key)
        pos.price_history_error = "Loading chart history..."

        def _load_history() -> None:
            points: list[tuple[float, float]] = []
            error: str | None = None
            try:
                points = api.get_asset_price_history(asset)
            except Exception as e:
                error = str(e)

            start_ts = float(pos.entry_ts or 0.0)
            end_ts = float(pos.exit_ts or 0.0)
            if points and start_ts > 0.0 and end_ts > 0.0 and end_ts >= start_ts:
                points = [point for point in points if start_ts <= point[0] <= end_ts]

            def _apply_history() -> None:
                _closed_chart_loads_inflight.discard(request_key)
                pos.price_history = points
                pos.price_history_source = "asset_history"
                pos.price_history_error = None if points else (error or "Closed position has no chart history.")
                if _show_closed[0]:
                    selected_pos = _find_selected_closed_position()
                    if selected_pos is pos:
                        _last_chart_signature[0] = None
                        _load_selected_position_chart()

            root.after(0, _apply_history)

        threading.Thread(target=_load_history, daemon=True).start()

    def _load_selected_position_chart() -> None:
        sel = pos_tree.selection()
        if not sel:
            empty_signature = ("empty",)
            if _last_chart_signature[0] != empty_signature:
                pos_graph.load([], "", 0.0)
                _last_chart_signature[0] = empty_signature
            return

        vals = pos_tree.item(sel[0])["values"]
        if not vals:
            empty_signature = ("empty",)
            if _last_chart_signature[0] != empty_signature:
                pos_graph.load([], "", 0.0)
                _last_chart_signature[0] = empty_signature
            return

        mkt_name = str(vals[0]).replace("ðŸ’Ž", "").replace("âš¡", "")
        mkt_name = _clean_tree_market_title(vals[0])
        outcome = str(vals[1])

        if _show_closed[0]:
            pos = _find_selected_closed_position()
            if pos is None:
                msg = f"Closed position match not found for {mkt_name[:48]} [{outcome}]."
                missing_signature = ("closed-missing", mkt_name, outcome, msg)
                if _last_chart_signature[0] != missing_signature:
                    pos_graph.load([], mkt_name, 0.0, msg)
                    pos_chart_frame.refresh_panel(reset=True)
                    _last_chart_signature[0] = missing_signature
                if _last_chart_warn[0] != msg:
                    pos_log_write(msg, "WARN")
                    _last_chart_warn[0] = msg
                return

            history = pos.price_history
            entry_price = pos.entry_price
            title = pos.title or mkt_name
            if not history and pos.price_history_error != "Loading chart history...":
                _request_closed_position_chart_history(pos)
                history = pos.price_history
            if history:
                history_signature = (
                    "closed",
                    _position_cid_or_empty(pos),
                    title,
                    outcome,
                    len(history),
                    float(history[-1][0]),
                )
                if _last_chart_signature[0] == history_signature:
                    return
                _last_chart_warn[0] = ""
                pos_graph.load(
                    history,
                    title,
                    entry_price,
                    entry_ts=pos.entry_ts or None,
                    exit_ts=pos.exit_ts or None,
                    exit_price=pos.exit_price or None,
                )
                pos_chart_frame.refresh_panel(reset=True)
                _last_chart_signature[0] = history_signature
                return

            detail = pos.price_history_error or "Closed position has no chart history."
            empty_history_signature = (
                "closed-empty",
                _position_cid_or_empty(pos),
                title,
                outcome,
                detail,
            )
            if _last_chart_signature[0] != empty_history_signature:
                pos_graph.load(
                    [],
                    title,
                    entry_price,
                    detail,
                    entry_ts=pos.entry_ts or None,
                    exit_ts=pos.exit_ts or None,
                    exit_price=pos.exit_price or None,
                )
                pos_chart_frame.refresh_panel(reset=True)
                _last_chart_signature[0] = empty_history_signature
            if detail != "Loading chart history...":
                warn_msg = f"Closed chart empty: {title[:80]} [{outcome}] | {detail}"
                if _last_chart_warn[0] != warn_msg:
                    pos_log_write(warn_msg, "WARN")
                    _last_chart_warn[0] = warn_msg
            return

        for pos in _open_positions():
            title = pos.title
            if title[:48] in mkt_name or mkt_name[:30] in title:
                if pos.outcome == outcome:
                    history_signature = (
                        "open",
                        _position_cid_or_empty(pos),
                        title,
                        outcome,
                        len(pos.price_history),
                        float(pos.price_history[-1][0]) if pos.price_history else 0.0,
                    )
                    if _last_chart_signature[0] == history_signature:
                        return
                    _last_chart_warn[0] = ""
                    pos_graph.load(pos.price_history, title, pos.entry_price, entry_ts=pos.entry_ts or None)
                    pos_chart_frame.refresh_panel(reset=True)
                    _last_chart_signature[0] = history_signature
                    return

        _last_chart_warn[0] = ""
        missing_signature = ("open-missing", mkt_name, outcome)
        if _last_chart_signature[0] != missing_signature:
            pos_graph.load([], mkt_name, 0.0, f"Open position match not found for {mkt_name[:48]} [{outcome}].")
            pos_chart_frame.refresh_panel(reset=True)
            _last_chart_signature[0] = missing_signature

    def _find_selected_closed_position() -> Position | None:
        sel = pos_tree.selection()
        if not sel:
            return None
        return _closed_tree_items.get(str(sel[0]))

    def _find_selected_open_position() -> Position | None:
        sel = pos_tree.selection()
        if not sel:
            return None
        return _open_tree_items.get(str(sel[0]))

    def _on_pos_double_click(event):
        if _show_closed[0]:
            pos = _find_selected_closed_position()
        else:
            pos = _find_selected_open_position()
        if pos:
            show_position_detail(pos)

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
                for pos in _open_positions():
                    if pos.title[:48] in mkt_name or mkt_name[:30] in pos.title:
                        slug = pos.event_slug
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
                for pos in _open_positions():
                    if pos.title[:48] in mkt_name or mkt_name[:30] in pos.title:
                        try:
                            root.clipboard_clear()
                            root.clipboard_append(pos.title or mkt_name)
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
    
    def pos_log_write(msg, tag="INFO"):
        log(f"[positions] {msg}", tag)
    
    
    # ═══════════════════════════════════════════════════════════════════════════════
    #  TAB 4: P&L GRAPH
    # ═══════════════════════════════════════════════════════════════════════════════

    tab_pnl = tk.Frame(nb, bg="#080810")
    
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
        pnl_summary = _latest_pnl
        if pnl_summary is None:
            return []
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

    hist_cols = ("Time","Type","Market","Side","Price$","P&L$","P&L%","Via","Bankroll$")
    hist_tree = ttk.Treeview(tab_pnl, columns=hist_cols, show="headings", height=7)
    hw = {"Time":65,"Type":48,"Market":280,"Side":90,"Price$":68,
          "P&L$":72,"P&L%":65,"Via":170,"Bankroll$":78}
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
    _hist_tree_items: dict[str, TradeRecord] = {}
    
    def _on_hist_double_click(event):
        sel = hist_tree.selection()
        if not sel:
            return
        trade = _hist_tree_items.get(str(sel[0]))
        if trade:
            show_trade_history_detail(trade)
    
    hist_tree.bind("<Double-1>", _on_hist_double_click)

    def draw_pnl_graph(pnl_summary: PnlSummaryDict) -> None:
        pnl_graph.load(pnl_summary["equity_history"], pnl_summary["bankroll_start"])
    
    
    def refresh_pnl_tab(
        pnl: PnlSummaryDict,
        st: TradeStatsDict,
        history: list[TradeRecord],
    ) -> None:
        unrealised_pnl = sum(
            ((pos.cur_price or pos.entry_price) - pos.entry_price) * pos.shares
            for pos in _open_positions()
        )
        realised_pnl = st["sum_pnl"]
        total_pnl    = realised_pnl + unrealised_pnl
        win_rate     = st["win_rate"] * 100
        avg_pnl      = total_pnl / max(st["sell_count"], 1)
        open_val     = sum(
            (pos.cur_price or pos.entry_price) * pos.shares
            for pos in _open_positions()
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

        hist_tree.delete(*hist_tree.get_children())
        _hist_tree_items.clear()
        for t in reversed(history[-200:]):
            whale_str = ", ".join(t.wallet_names[:2]) or "—"
            if t.type == "BUY":
                iid = hist_tree.insert("", "end", values=(
                    t.ts_str or "—", "BUY", t.title[:40], t.outcome,
                    f"${t.price:.4f}",
                    "—", "—", whale_str, f"${t.bankroll:.3f}",
                ), tags=("BUY",))
                _hist_tree_items[iid] = t
            elif t.type == "SELL":
                pnl_u = t.pnl_usdc or 0
                pnl_p = t.pnl_pct or 0
                tag   = "WIN" if pnl_u >= 0 else "LOSS"
                iid = hist_tree.insert("", "end", values=(
                    t.ts_str or "—", "SELL", t.title[:40], t.outcome,
                    f"${t.price:.4f}",
                    f"${pnl_u:+.4f}", f"{pnl_p:+.1f}%", whale_str,
                    f"${t.bankroll:.3f}",
                ), tags=(tag,))
                _hist_tree_items[iid] = t
    
        draw_pnl_graph(pnl)
        pnl_chart_frame.refresh_panel()

    
    # ═══════════════════════════════════════════════════════════════════════════════
    #  TAB 5: WALLET ROSTER
    # ═══════════════════════════════════════════════════════════════════════════════

    tab_wallets = tk.Frame(nb, bg="#080810")
    
    wh_header = tk.Frame(tab_wallets, bg="#0d0d1a", pady=4)
    wh_header.pack(fill="x", padx=4, pady=(4,0))
    tk.Label(wh_header, text="WALLET ROSTER", fg="#00ff88", bg="#0d0d1a", font=bold_hd).pack(side="left", padx=8)
    wh_filter_var = tk.StringVar(value="ALL")
    for val, label in [("ALL","All"),("ELITE","🔥 Elite"),("VER","✅ Verified"),("HFT","⚡ HFT"),("VIP","⭐ VIP")]:
        tk.Radiobutton(wh_header, text=label, variable=wh_filter_var, value=val,
                       bg="#0d0d1a", fg="#aaaaaa", selectcolor="#0d0d1a",
                       activebackground="#0d0d1a", font=mono,
                       command=lambda: _pending_update.__setitem__(0, True)
                       ).pack(side="left", padx=4)
    
    wh_cols = ("Name","Score","WinRate","WilsonLB","Res","Portfolio","Rank","Volume","PnL","AvgBet","TPH","Status","HFT","VIP")
    wh_tree = ttk.Treeview(tab_wallets, columns=wh_cols, show="headings")
    ww = {"Name":130,"Rank":50,"Volume":90,"Score":58,"WinRate":65,"WilsonLB":72,
          "Res":50,"Portfolio":100,"PnL":90,"AvgBet":78,"TPH":55,"Status":80,"HFT":40,"VIP":40}
    for c in wh_cols:
        wh_tree.heading(c, text=c)
        wh_tree.column(c, width=ww[c], anchor="center")
    wh_tree.tag_configure("DEAD_WALLET",           foreground="#fff1f1", background="#7a0018")
    wh_tree.tag_configure(WalletTier.ELITE.name,    foreground="#00ff55", background="#001500")
    wh_tree.tag_configure(WalletTier.VERIFIED.name, foreground="#ffdd00", background="#181400")
    wh_tree.tag_configure(WalletTier.WATCH.name,    foreground="#55aaff", background="#000d1a")
    wh_tree.tag_configure(WalletTier.REJECTED.name, foreground="#554444", background="#0c0c18")
    wh_vsb = tk.Scrollbar(tab_wallets, command=wh_tree.yview)
    wh_tree.configure(yscrollcommand=wh_vsb.set)
    wh_vsb.pack(side="right", fill="y")
    wh_tree.pack(fill="both", expand=True, padx=4, pady=4)
    _whale_tree_items: dict[str, tuple[str, "Wallet"]] = {}
    
    
    # ═══════════════════════════════════════════════════════════════════════════════
    #  TAB 6: ANALYSIS
    # ═══════════════════════════════════════════════════════════════════════════════
    tab_analysis = tk.Frame(nb, bg="#080810")
    analysis_txt = scrolledtext.ScrolledText(tab_analysis, bg="#060610",
        fg="#aaaacc", font=mono, selectbackground="#1a2a4a", wrap=tk.WORD)
    analysis_txt.pack(fill="both", expand=True, padx=4, pady=4)
    
    
    # ═══════════════════════════════════════════════════════════════════════════════
    #  TAB 7: DIAGNOSTICS
    # ═══════════════════════════════════════════════════════════════════════════════
    tab_diag = tk.Frame(nb, bg="#080810")
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

    def _is_scrolled_to_end(widget: tk.Text) -> bool:
        try:
            return float(widget.yview()[1]) >= 0.999
        except Exception:
            return True

    def _top_scroll_fraction(widget: tk.Text) -> float:
        try:
            return float(widget.yview()[0])
        except Exception:
            return 0.0

    def _restore_scroll(widget: tk.Text, *, was_at_end: bool, top_fraction: float) -> None:
        try:
            if was_at_end:
                widget.see(tk.END)
            else:
                widget.yview_moveto(top_fraction)
        except Exception:
            pass
    
    
    def log(msg, level="INFO"):
        try:
            if level == "DEBUG" and not _debug_mode[0]:
                return
            live_log.configure(state="normal")
            was_at_end = _is_scrolled_to_end(live_log)
            ts  = datetime.now().strftime("%H:%M:%S")
            tag = level if level in LOG_COLORS else "INFO"
            live_log.insert(tk.END, f"[{ts}] {msg}\n", tag)
            line_count = int(live_log.index("end-1c").split(".")[0])
            if line_count > 3000:
                live_log.delete("1.0", "600.0")
            if was_at_end:
                live_log.see(tk.END)
            live_log.configure(state="disabled")
        except Exception:
            pass

    def _log_ui_error(context: str, error: BaseException, level: str = "ERR") -> None:
        import urllib.error as _ue
        if isinstance(error, (_ue.URLError, ConnectionRefusedError, OSError)):
            from titan_client import TitanClient
            if isinstance(api, TitanClient) and api._server_offline:
                return
        tb = error.__traceback__
        if tb is not None:
            last_frame = traceback.extract_tb(tb)[-1]
            location = f"{last_frame.filename}:{last_frame.lineno}"
            detail = f"{type(error).__name__}: {error} @ {location}"
        else:
            detail = f"{type(error).__name__}: {error}"
        stack = "".join(traceback.format_exception(type(error), error, tb)).strip()
        message = f"[{context}] {detail}\n{stack}"
        try:
            log(message, level)
        except Exception:
            try:
                import titan_state as _ts_mut
                _ts_mut._log(message, level)
            except Exception:
                pass

    def _report_tk_callback_exception(
        exc_type: type[BaseException],
        exc_value: BaseException,
        exc_traceback: TracebackType | None,
    ) -> None:
        if exc_value.__traceback__ is not exc_traceback:
            try:
                exc_value = exc_value.with_traceback(exc_traceback)
            except Exception:
                pass
        _log_ui_error("tk callback", exc_value)

    root.report_callback_exception = _report_tk_callback_exception
    
    
    # ═══════════════════════════════════════════════════════════════════════════════
    #  TAB 8: SYSTEM LOG
    # ═══════════════════════════════════════════════════════════════════════════════
    tab_log = tk.Frame(nb, bg="#080810")
    
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

    _log_debug_btn_var = tk.StringVar(value="🐞 DEBUG OFF")
    def _toggle_log_debug():
        import titan_client as _tc
        _debug_mode[0] = not _debug_mode[0]
        _tc._debug_enabled = _debug_mode[0]
        _log_debug_btn_var.set("🐞 DEBUG ON" if _debug_mode[0] else "🐞 DEBUG OFF")
        _debug_btn_var.set("🐞 DEBUG ON" if _debug_mode[0] else "🐞 DEBUG OFF")
    tk.Button(log_tool_bar, textvariable=_log_debug_btn_var, bg="#1a1320", fg="#d8b4ff",
              font=mono_sm, command=_toggle_log_debug).pack(side="left", padx=4)
    tk.Label(log_tool_bar,
             text="Copies everything: positions · signals · elites · trades · exits · raw logs.",
             fg="#445566", bg="#0d0d1a", font=mono_sm).pack(side="left")
    
    log_nb = ttk.Notebook(tab_log)
    log_nb.pack(fill="both", expand=True, padx=4, pady=4)

    def _make_log_listbox(parent: tk.Frame, fg: str) -> tk.Listbox:
        vsb = ttk.Scrollbar(parent, orient="vertical")
        hsb = ttk.Scrollbar(parent, orient="horizontal")
        lb = tk.Listbox(
            parent,
            bg="#050508", fg=fg, font=mono_sm,
            selectbackground="#1a2a4a", selectforeground=fg,
            activestyle="none", bd=0, highlightthickness=0,
            yscrollcommand=vsb.set, xscrollcommand=hsb.set,
        )
        vsb.config(command=lb.yview)
        hsb.config(command=lb.xview)
        hsb.pack(side="bottom", fill="x")
        vsb.pack(side="right",  fill="y")
        lb.pack(side="left", fill="both", expand=True)
        return lb

    live_frame = tk.Frame(log_nb, bg="#0d0d1a")
    live_hdr = tk.Frame(live_frame, bg="#0d0d1a")
    live_hdr.pack(fill="x", padx=2, pady=(2,0))
    tk.Label(live_hdr, text="LIVE", bg="#0d0d1a", fg="#445566", font=mono_sm,
             anchor="w").pack(side="left")
    live_log = tk.Text(live_frame, bg="#060610", fg="#44ff44", font=mono,
                       selectbackground="#1a2a4a", wrap="word")
    live_vsb = tk.Scrollbar(live_frame, command=live_log.yview, bg="#0d0d1a")
    live_log.configure(yscrollcommand=live_vsb.set)
    live_vsb.pack(side="right", fill="y")
    live_log.pack(fill="both", expand=True)
    for tag_name, color in LOG_COLORS.items():
        live_log.tag_configure(tag_name, foreground=color)
    log_nb.add(live_frame, text="Live")

    srv_frame = tk.Frame(log_nb, bg="#0d0d1a")
    tk.Label(srv_frame, text="SERVER", bg="#0d0d1a", fg="#445566", font=mono_sm,
             anchor="w").pack(fill="x", padx=2)
    full_log = _make_log_listbox(srv_frame, "#66ffaa")
    log_nb.add(srv_frame, text="Server")

    cli_frame = tk.Frame(log_nb, bg="#0d0d1a")
    tk.Label(cli_frame, text="CLIENT", bg="#0d0d1a", fg="#445566", font=mono_sm,
             anchor="w").pack(fill="x", padx=2)
    client_log = _make_log_listbox(cli_frame, "#aaddff")
    log_nb.add(cli_frame, text="Client")

    _CLIENT_LOG_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "Logs", "titan_client.log")
    _client_log_mtime: list[float] = [0.0]
    _client_log_cache: list[str]   = [""]   # cached result of last read

    def _read_client_log(lines: int = 600) -> str:
        if not os.path.exists(_CLIENT_LOG_FILE):
            return ""
        try:
            mtime = os.path.getmtime(_CLIENT_LOG_FILE)
            if mtime == _client_log_mtime[0]:
                return _client_log_cache[0]
            with open(_CLIENT_LOG_FILE, "r", encoding="utf-8") as f:
                raw = [l.rstrip("\r\n") for l in f.readlines()[-lines:]]
            if not _debug_mode[0]:
                raw = [l[:200] for l in raw]
            result = "\n".join(raw)
            _client_log_mtime[0] = mtime
            _client_log_cache[0] = result
            return result
        except Exception:
            return ""
    
    
    # ═══════════════════════════════════════════════════════════════════════════════
    #  TAB 9: CONFIG EDITOR
    # ═══════════════════════════════════════════════════════════════════════════════
    import json as _json
    import importlib as _importlib
    

    tab_config = tk.Frame(nb, bg="#080810")
    
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
    #  TAB 10: WALLET SELECTOR
    # ═══════════════════════════════════════════════════════════════════════════════
    tab_selector = tk.Frame(nb, bg="#080810")

    _sel_status_var = tk.StringVar(value="")
    _hft_enabled_var = tk.StringVar(value="ON")

    def _current_hft_enabled() -> bool:
        raw_value = _sel_fields.get("hft_enabled").get().strip().lower() if "hft_enabled" in _sel_fields else ""
        return raw_value in {"1", "true", "yes", "on"}

    def _refresh_hft_status_ui() -> None:
        hft_on = _current_hft_enabled()
        _hft_enabled_var.set("ON" if hft_on else "OFF")
        app_subtitle_var.set(
            "Follow The Wallet"
            f" | ⚡ HFT: {_hft_enabled_var.get()}"
            " | v10 CONVICTION-ONLY | 2+ Elites | 20-72¢ Zone | -30% Stop | ENGINE ACTIVE"
        )
        status_var.set(f"🟢 LIVE — Follow The Wallet | ⚡ HFT: {_hft_enabled_var.get()}")
        _live_subtitle_var.set(
            "Follow The Wallet"
            f" | ⚡ HFT: {_hft_enabled_var.get()}"
            " | BUY when wallet buys, SELL when wallet sells | connecting..."
        )

    def _sel_load():
        """Read wallet_selector section from config JSON and populate widgets."""
        try:
            import titan_config as _tc, json as _j
            with open(_tc.get_config_file(), encoding="utf-8") as _f:
                _raw = _j.load(_f)
            ws = _raw.get("wallet_selector", {})
            active = ws.get("active_selector", "performance")
            _sel_var.set(active)
            params = (ws.get("selectors") or {}).get(active, {})
            _list_keys_load = {"leaderboard_periods"}
            for key, var in _sel_fields.items():
                val = params.get(key, "")
                if key in _list_keys_load and isinstance(val, list):
                    var.set(", ".join(str(v) for v in val))
                else:
                    var.set("" if val == "" else str(val))
            _refresh_hft_status_ui()
            _sel_status_var.set(f"  Loaded · active: {active}")
        except Exception as e:
            _sel_status_var.set(f"  ❌ Load failed: {e}")

    def _sel_save():
        """Write current widget values back into wallet_selector section and save."""
        try:
            import titan_config as _tc, json as _j
            cfg_path = _tc.get_config_file()
            with open(cfg_path, encoding="utf-8") as _f:
                _raw = _j.load(_f)
            active = _sel_var.get().strip()
            if "wallet_selector" not in _raw:
                _raw["wallet_selector"] = {"_group": "Wallet Selector"}
            _raw["wallet_selector"]["active_selector"] = active
            if "selectors" not in _raw["wallet_selector"]:
                _raw["wallet_selector"]["selectors"] = {}
            if active not in _raw["wallet_selector"]["selectors"]:
                _raw["wallet_selector"]["selectors"][active] = {}
            target = _raw["wallet_selector"]["selectors"][active]
            _int_keys = {
                "discovery_large_trade_limit",
                "discovery_leaderboard_limit",
                "min_resolved_bets",
                "elite_min_resolved",
            }
            _list_keys = {"leaderboard_periods"}
            _bool_keys = {"discovery_use_large_trades", "discovery_use_leaderboard", "hft_enabled"}
            for key, var in _sel_fields.items():
                raw_val = var.get().strip()
                if not raw_val:
                    continue
                if key in _list_keys:
                    target[key] = [v.strip() for v in raw_val.split(",")]
                elif key in _bool_keys:
                    target[key] = raw_val.lower() in {"1", "true", "yes", "on"}
                elif key in _int_keys:
                    target[key] = int(raw_val)
                else:
                    try:
                        target[key] = float(raw_val)
                    except ValueError:
                        target[key] = raw_val
            with open(cfg_path, "w", encoding="utf-8") as _f:
                _j.dump(_raw, _f, indent=4)
            _tc.reload()
            _refresh_hft_status_ui()
            _sel_status_var.set(f"  ⏳ Reclassifying wallets…")
            log("🎯 Wallet selector config saved — reclassifying…", "INFO")
            def _reclassify():
                try:
                    n = api.apply_selector()
                    root.after(0, lambda: _sel_status_var.set(f"  ✅ Applied — {n} wallets reclassified"))
                    root.after(0, lambda: log(f"🎯 Selector applied: {n} wallets reclassified instantly", "INFO"))
                except Exception as _e:
                    root.after(0, lambda err=_e: _sel_status_var.set(f"  ❌ Reclassify failed: {err}"))
            import threading as _th
            _th.Thread(target=_reclassify, daemon=True).start()
        except Exception as e:
            _sel_status_var.set(f"  ❌ Save failed: {e}")

    # ── toolbar ───────────────────────────────────────────────────────────────
    sel_toolbar = tk.Frame(tab_selector, bg="#0d0d1a", pady=6)
    sel_toolbar.pack(fill="x")

    tk.Label(sel_toolbar, text="Active selector:", fg="#778899", bg="#0d0d1a",
             font=mono).pack(side="left", padx=(12, 4))

    _sel_var = tk.StringVar(value="performance")
    _sel_choices = ["performance"]
    try:
        from titan_selector import available_selectors as _avail_sel
        _sel_choices = [s["id"] for s in _avail_sel()]
    except Exception:
        pass
    sel_dropdown = tk.OptionMenu(sel_toolbar, _sel_var, *_sel_choices, command=lambda _: _sel_load())
    sel_dropdown.config(bg="#1a1a2a", fg="#00ff88", font=mono, activebackground="#0d0d1a",
                        highlightthickness=0)
    sel_dropdown.pack(side="left", padx=4)

    tk.Button(sel_toolbar, text="💾 SAVE & APPLY", bg="#002a00", fg="#00ff88",
              font=bold_hd, padx=14, command=_sel_save).pack(side="left", padx=10)
    tk.Button(sel_toolbar, text="↺ Reload", bg="#1a1a2a", fg="#778899",
              font=mono, padx=8, command=_sel_load).pack(side="left", padx=4)
    tk.Label(sel_toolbar, textvariable=_sel_status_var, fg="#556677",
             bg="#0d0d1a", font=mono).pack(side="left", padx=10)

    # ── params grid ───────────────────────────────────────────────────────────
    sel_canvas = tk.Canvas(tab_selector, bg="#080810", highlightthickness=0)
    sel_vsb = tk.Scrollbar(tab_selector, orient="vertical", command=sel_canvas.yview, bg="#0d0d1a")
    sel_canvas.configure(yscrollcommand=sel_vsb.set)
    sel_vsb.pack(side="right", fill="y")
    sel_canvas.pack(fill="both", expand=True)

    sel_inner = tk.Frame(sel_canvas, bg="#080810")
    sel_canvas_win = sel_canvas.create_window((0, 0), window=sel_inner, anchor="nw")
    sel_inner.bind("<Configure>", lambda e: sel_canvas.configure(scrollregion=sel_canvas.bbox("all")))
    sel_canvas.bind("<Configure>", lambda ev: sel_canvas.itemconfig(sel_canvas_win, width=ev.width))

    _PARAM_META: list[tuple[str, str, str, str]] = [
        # (field_key, label, section_header_or_"", description)
        ("",                          "",                                "── Bot filters ──",     ""),
        ("hft_enabled",               "HFT wallets enabled",            "",
         "Master switch for HFT wallet classification and the HFT fast loop. "
         "false → all wallets are classified normally (never tagged HFT), HFT spike trading stops immediately, "
         "and the HFT fast loop thread exits. true → normal HFT detection and polling resumes."),
        ("",                          "",                                "── Discovery ──",       ""),
        ("discovery_use_large_trades","Use large trade feed",           "",
         "Calls the Polymarket trades API each cycle and collects wallets behind every large buy above min_trade_cash_discovery. "
         "Best way to find wallets actively deploying capital right now. Disable only if you rely solely on the leaderboard."),
        ("discovery_large_trade_limit","Large trade fetch limit",       "",
         "How many recent large trades to pull per discovery call — each trade yields one candidate wallet address. "
         "200 is a safe default; going higher gives broader coverage but increases API call time and rate-limit risk."),
        ("min_trade_cash_discovery",  "Large trade min cash ($)",       "",
         "Only trades at or above this USD size are used as discovery candidates. "
         "Raise it (e.g. $10 000) to focus on serious positions and cut retail noise; lower it to cast a wider net at the cost of more low-quality candidates."),
        ("discovery_trade_side",      "Large trade side",               "",
         "Which side of the market to scan. BUY captures wallets taking a conviction position — the main alpha signal. "
         "SELL captures exits. BOTH captures all activity. BUY is almost always the right choice for alpha discovery."),
        ("discovery_use_leaderboard", "Use leaderboard feed",           "",
         "Also pulls the Polymarket leaderboard for each configured time period and adds those ranked wallets as candidates. "
         "Captures proven long-term performers who may not appear in today's large trades. Use alongside the trade feed for maximum coverage."),
        ("discovery_leaderboard_limit","Leaderboard rows per period",   "",
         "How many top-ranked wallets to pull per leaderboard period. Each period is a separate API call. "
         "Total leaderboard candidates = this value multiplied by the number of configured periods."),
        ("discovery_leaderboard_category", "Leaderboard category",      "",
         "Which Polymarket leaderboard ranking to query. OVERALL covers all markets and is the broadest view of alpha generators. "
         "Narrower categories like CRYPTO exist but OVERALL gives the most relevant universe for copy trading."),
        ("discovery_leaderboard_order_by", "Leaderboard order by",      "",
         "Sort field used when calling the leaderboard API. PNL ranks wallets by total profit — the most relevant signal. "
         "VOLUME would surface high-frequency traders, which is usually not what you want here. Keep as PNL."),
        ("leaderboard_periods",       "Leaderboard periods (CSV)",      "",
         "Time windows queried on the leaderboard, e.g. ALL,MONTH,WEEK. ALL catches proven long-term performers; WEEK catches wallets running hot right now. "
         "Multiple periods ensure both types are discovered each cycle without requiring a restart."),
        ("",                          "",                                "── Watchable gate ──",  ""),
        ("min_win_rate_watch",        "Min win rate",                   "",
         "First hard gate: the wallet's raw win rate (resolved wins / total resolved bets) must meet this to enter the watchlist at all. "
         "0.53 = 53% wins. Any wallet below this is rejected immediately. Too low and you watch losers; too high and you miss real edges."),
        ("wilson_min_watch",          "Wilson lower bound",             "",
         "Second hard gate: the Wilson lower-bound confidence interval on the win rate must meet this. "
         "Unlike raw win rate, Wilson LB accounts for sample size — 5 wins from 5 bets scores much lower than 100 wins from 188 bets. "
         "This prevents lucky short streaks from entering the watchlist."),
        ("min_resolved_bets",         "Min resolved bets",              "",
         "Wallet must have at least this many fully settled bets before it is evaluated at all. "
         "With fewer bets the win rate and Wilson LB are statistically meaningless. 10 is the practical minimum; 20+ gives much stronger confidence."),
        ("min_pnl",                   "Min PnL ($)",                    "",
         "Total realised cash PnL across all resolved positions must be at or above this. "
         "0 means break-even or better. A positive value like $500 ensures the wallet has demonstrated real monetary edge, not just a winning percentage on micro-bets."),
        ("",                          "",                                "── Verified gate ──",   ""),
        ("min_win_rate_ver",          "Min win rate (verified)",        "",
         "Stricter win rate applied on top of the watchable gate for a wallet to reach verified status. "
         "Verified wallets are polled more frequently and their signals carry more weight in the engine. "
         "0.56 is 3 points above the watchable floor — meaningful but not extreme."),
        ("wilson_min_ver",            "Wilson lower bound (verified)",  "",
         "Stricter Wilson LB for verified status. Because verified wallets drive actual copy trades, confidence in their win rate must be higher. "
         "0.49 means the lower bound of the 95% confidence interval on their win rate is still above 49% — strong statistical evidence of edge."),
        ("min_avg_profit",            "Min avg profit/trade ($)",       "",
         "Average dollar profit per resolved trade (total PnL / resolved bets) must meet this. "
         "Blocks wallets that win many tiny bets — a 60% win rate on $0.10 trades is useless to copy. "
         "This check is bypassed for HFT wallets where per-trade profit naturally compresses due to volume."),
        ("min_avg_bet",               "Min avg bet ($)",                "",
         "Average bet size must be at least this. A wallet betting $2 per trade cannot generate meaningful absolute PnL regardless of win rate. "
         "Also ensures the wallet's positions are large enough to be worth copying at our own bet sizing. Bypassed for detected HFT wallets."),
        ("min_portfolio_or_pnl",      "Min portfolio or PnL ($)",       "",
         "Either the wallet's current total open position value OR its lifetime PnL must exceed this — whichever is larger is used. "
         "The OR logic is intentional: a wallet that banked $2 000 but is flat today still qualifies, as does one actively holding $2 000 open. "
         "Ensures verified wallets are economically meaningful, not just statistically good."),
        ("",                          "",                                "── Elite gate ──",      ""),
        ("elite_min_pnl",             "Elite min total PnL ($)",        "",
         "Lifetime cash PnL must exceed this for elite status. Elite wallets receive the highest polling priority and strongest copy signal weight. "
         "$40 000 default means only wallets that have extracted serious money from the market qualify — not just a lucky month."),
        ("elite_min_portfolio",       "Elite min portfolio ($)",        "",
         "max(current open value, lifetime PnL) must exceed this. A $80 000 threshold ensures elite wallets are not just historically profitable "
         "but currently deploying major capital — strong evidence of active conviction rather than past glory."),
        ("elite_min_score",           "Elite min composite score",      "",
         "The wallet's 0–1 composite score (weighted sum of Wilson LB, PnL %, portfolio, trade count, open positions, alpha/trade) must meet this. "
         "0.72 means the wallet scores well across all dimensions simultaneously. Adjust the weights below to change what this score rewards."),
        ("elite_min_resolved",        "Elite min resolved bets",        "",
         "Minimum settled bets for elite status. Combined with the Wilson LB gate this ensures the wallet's edge is both large and statistically well-evidenced. "
         "20 bets is a solid floor; below that the score is too unstable to trust with elite-level copy weight."),
        ("elite_alpha_per_trade",     "Elite min alpha/trade ($)",      "",
         "Alpha per trade = total PnL / resolved bets — the cleanest measure of per-bet dollar edge. "
         "$40 000 PnL across 40 000 bets is $1/trade (thin). $40 000 from 200 bets is $200/trade (elite). "
         "Default 1.0 sets a minimal floor; raise it to demand genuine per-trade impact."),
        ("",                          "",                                "── Scoring weights ──", ""),
        ("weight_wilson",             "Weight: Wilson LB",              "",
         "Share of the 0–1 composite score driven by Wilson lower-bound win rate. At 0.30 this is the dominant factor — "
         "a wallet with a high and statistically confident win rate scores well here. All six weights must sum to 1.0."),
        ("weight_pnl_pct",            "Weight: PnL %",                  "",
         "Share driven by PnL as a percentage of initial invested capital. Capped internally at 30% for normalisation — "
         "a 30%+ return scores full marks. Rewards relative return so a small but highly efficient wallet can still rank well."),
        ("weight_portfolio",          "Weight: Portfolio size",         "",
         "Share driven by current open portfolio value, normalised against $25 000. "
         "Rewards wallets actively deploying capital right now. An idle wallet with a great historical record scores zero here, keeping the list biased toward active participants."),
        ("weight_trade_count",        "Weight: Trade count",            "",
         "Share driven by number of resolved bets, normalised against 20 bets. "
         "Small bonus for wallets with more data, reinforcing statistical confidence alongside Wilson LB. Keep this low — quantity alone is not quality."),
        ("weight_open_positions",     "Weight: Open positions",         "",
         "Share driven by number of currently open positions, normalised against 10. "
         "Wallets holding many active bets right now are more likely to generate copyable signals this cycle. A wallet that has not traded in weeks scores zero here."),
        ("weight_alpha",              "Weight: Alpha/trade",            "",
         "Share driven by average profit per resolved trade, normalised against $50/trade. "
         "Directly rewards dollar edge per bet. Combined with weight_wilson this creates a score that values both consistency and magnitude of edge."),
        ("hft_tph_threshold",         "HFT trades/hour threshold",      "",
         "Wallets exceeding this trades-per-hour rate are tagged HFT (high-frequency trader). "
         "HFT is also triggered if avg_bet < $50 with more than 100 resolved bets. "
         "The HFT tag bypasses the avg_profit and avg_bet verified-gate checks — HFT edge comes from volume not per-trade size — and adjusts their polling frequency upward."),
        ("sports_bot_tph_threshold",  "Sports bot trades/hour threshold","",
         "Wallets above this TPH are tagged as sports bots. Sports bots are market makers in sports/politics markets — their edge is speed and spread, not prediction accuracy. "
         "They are excluded from copy trading even if they pass all scoring gates. "
         "A wallet is also tagged sports bot if it matches a known sports bot name or has a mid-range TPH (50–100) with predominantly sports market activity."),
    ]

    _sel_fields: dict[str, tk.StringVar] = {}
    row_idx = 0
    for field_key, label, section, desc in _PARAM_META:
        if section:
            tk.Label(sel_inner, text=section, fg="#00ff88", bg="#080810",
                     font=bold_hd, pady=6, padx=16).grid(
                row=row_idx, column=0, columnspan=3, sticky="w")
            row_idx += 1
            continue
        var = tk.StringVar()
        _sel_fields[field_key] = var
        if field_key == "hft_enabled":
            var.trace_add("write", lambda *_args: _refresh_hft_status_ui())
        tk.Label(sel_inner, text=label, fg="#aaaacc", bg="#080810",
                 font=mono, anchor="w", width=30).grid(
            row=row_idx, column=0, sticky="w", padx=(24, 8), pady=2)
        tk.Entry(sel_inner, textvariable=var, bg="#0d0d1a", fg="#ffcc44",
                 font=mono, width=18, insertbackground="#00ff88").grid(
            row=row_idx, column=1, sticky="w", pady=2, padx=(0, 16))
        tk.Label(sel_inner, text=desc, fg="#556677", bg="#080810",
                 font=mono, anchor="w", wraplength=520, justify="left").grid(
            row=row_idx, column=2, sticky="w", pady=2)
        row_idx += 1

    _sel_load()


    # ═══════════════════════════════════════════════════════════════════════════════
    #  TAB 11: SIGNAL BUILDERS
    # ═══════════════════════════════════════════════════════════════════════════════
    tab_sb = tk.Frame(nb, bg="#080810")

    _sb_status_var = tk.StringVar(value="")

    # Per-builder param metadata: (field_key, label, type, description)
    # type: "float", "int", "bool", "float_none" (None allowed)
    _SB_PARAM_META: dict[str, list[tuple[str, str, str, str]]] = {
        "consensus_basket": [
            ("min_elite_confluence",    "Min elite confluence",          "int",
             "How many distinct elite wallets must have bought the same outcome before a signal fires. "
             "Default 1 = a single elite is enough. Raise to 2+ to require independent agreement — fewer signals but higher conviction."),
            ("max_signal_age_h",        "Max signal age (h)",            "float",
             "Signal is rejected if the most recent elite trade on it is older than this (hours). "
             "Default 0.5h (30 min). Older trades indicate stale conviction; lower = fresher entries only."),
            ("price_min",               "Price min",                     "float",
             "Outcome is skipped if its current price is below this. "
             "Default 0.20 — below 20¢ the market implies <20% probability, too speculative to copy reliably."),
            ("price_max",               "Price max",                     "float",
             "Outcome is skipped if its current price is above this. "
             "Default 0.72 — above 72¢ the upside is thin and the outcome is near-certain, limiting edge."),
            ("min_score",               "Min score",                     "float",
             "Minimum composite signal score (0–100) after all scoring factors are applied. "
             "Default 50. Signals below this are dropped even if all other gates pass. Raise to filter weaker setups."),
            ("max_positions",           "Max positions",                 "int",
             "Maximum simultaneous open positions allowed from this builder. "
             "Enforced externally by the portfolio manager — once reached, new signals are suppressed until a slot frees up."),
            ("max_bet_abs",             "Max bet ($)",                   "float",
             "Hard ceiling on bet size applied inside kelly_bet(). Overrides the global MAX_BET_ABS for this builder. "
             "Default $1.20 — Consensus Basket uses small bets because the signal relies on a single elite confirmation."),
            ("stop_loss_pct",           "Stop loss % (blank = none)",    "float_none",
             "Soft stop loss stored on each signal. Position is exited if it drops this far from entry. "
             "Default -0.35 (exit at -35%). Leave blank for no stop. Must be negative."),
            ("opposition_ratio_block",  "Opposition ratio block",        "float",
             "Signal is rejected if opposite-side elite cash / total elite cash > this ratio. "
             "Default 0.60 — if 60%+ of smart-money flow is betting against our side, the trade is skipped. Raise to tolerate more disagreement."),
            ("conviction_portfolio_pct","Conviction portfolio %",        "float",
             "A wallet trade qualifies as 'large conviction' if it is >= this fraction of the wallet's portfolio AND >= $500. "
             "Default 0.005 (0.5%). Signals with at least one conviction trade receive a score bonus. Raise to demand larger relative bets."),
        ],
        "recent_form": [
            ("max_tph",                 "Max trades/hour (HFT filter)",  "float",
             "Wallets with trades-per-hour above this are excluded from Recent Form qualification. "
             "Default 20 — above this the wallet is effectively HFT and its directional signals are noise. Passed directly to is_recent_form_qualified()."),
            ("min_pnl_30d",             "Min PnL 30d ($)",               "float",
             "Wallet must have recent_pnl_30d >= this to qualify. Default 0 = break-even or better over 30 days. "
             "Raise to require wallets currently on a profitable streak."),
            ("min_pnl_7d",              "Min PnL 7d ($)",                "float",
             "Wallet must have recent_pnl_7d >= this. Default -50 allows a small recent drawdown. "
             "Set to 0 or above to require the wallet to be profitable right now, not just over the month."),
            ("max_signal_age_h",        "Max signal age (h)",            "float",
             "Trade older than this (hours) is rejected. Default 0.75h (45 min) — slightly looser than Consensus Basket "
             "because Recent Form needs fewer wallets and can tolerate slightly older data."),
            ("min_score",               "Min score",                     "float",
             "Minimum signal score after all scoring factors. Default 42, intentionally lower than Consensus Basket (50) "
             "— Recent Form targets emerging wallets whose scores haven't peaked yet."),
            ("price_min",               "Price min",                     "float",
             "Outcome skipped if price < this. Default 0.18, slightly looser than Consensus Basket, "
             "allowing entry on slightly lower-probability outcomes where momentum wallets may have early edge."),
            ("price_max",               "Price max",                     "float",
             "Outcome skipped if price > this. Default 0.78, slightly higher than Consensus Basket — "
             "recent-form wallets sometimes take late high-probability positions that still carry edge."),
            ("max_positions",           "Max positions",                 "int",
             "Maximum simultaneous open positions from this builder. Default 4, tighter than Consensus Basket — "
             "Recent Form signals carry less multi-wallet confirmation so fewer concurrent bets reduces correlated risk."),
            ("stop_loss_pct",           "Stop loss % (blank = none)",    "float_none",
             "Stop loss stored on each signal. Default None (no stop). "
             "Leave blank to hold through volatility; set a negative value (e.g. -0.30) to limit downside on weaker signals."),
        ],
        "drift_discount": [
            ("min_discount_pct",        "Min discount %",                "float",
             "Signal only fires if (wallet_entry_price - current_price) / wallet_entry_price >= this. "
             "Default 0.04 — the market must have drifted at least 4 points below the wallet's entry to confirm a real discount."),
            ("max_discount_pct",        "Max discount %",                "float",
             "Signal is rejected if the discount exceeds this. Default 0.12 — beyond 12 points the market "
             "is likely pricing in new negative information; the discount becomes a warning, not an opportunity."),
            ("max_signal_age_h",        "Max signal age (h)",            "float",
             "Wallet trade older than this is excluded. Default 6.0h — much looser than other builders because "
             "a discount opportunity can develop hours after the original entry and still be valid."),
            ("price_min",               "Price min",                     "float",
             "Current price (after drift) must be >= this. Default 0.20 — even discounted, below 20¢ is too speculative."),
            ("price_max",               "Price max",                     "float",
             "Current price must be <= this. Default 0.72 — if price is still high despite drift the discount is negligible."),
            ("max_positions",           "Max positions",                 "int",
             "Maximum simultaneous open positions from this builder. Default 3 — mispricing opportunities are rarer "
             "than consensus, so fewer slots are needed."),
            ("require_still_holding_check", "Require still-holding check", "bool",
             "If True, calls fetch_wallet_sells() to verify the whale hasn't exited since their buy. "
             "If all tracked wallets have sold, the signal is rejected. Partial exits are removed from scoring. "
             "Disable (False) only to skip the API call if latency is a concern — the signal becomes much riskier."),
            ("stop_loss_pct",           "Stop loss % (blank = none)",    "float_none",
             "Stop loss stored on each signal. Default None — Drift Discount positions are held longer by design "
             "and a stop could trigger on normal volatility before the discount closes. Set only if you want a hard floor."),
        ],
    }

    _SB_IDS = ["consensus_basket", "recent_form", "drift_discount"]
    _SB_LABELS = {"consensus_basket": "Consensus Basket", "recent_form": "Recent Form", "drift_discount": "Drift Discount"}

    _sb_builder_var = tk.StringVar(value="consensus_basket")
    # Nested: _sb_fields[builder_id][field_key] = StringVar
    _sb_fields: dict[str, dict[str, tk.StringVar]] = {bid: {} for bid in _SB_IDS}
    _sb_enabled_vars: dict[str, tk.BooleanVar] = {bid: tk.BooleanVar(value=True) for bid in _SB_IDS}

    def _sb_load():
        try:
            import titan_config as _tc, json as _j
            with open(_tc.get_config_file(), encoding="utf-8") as _f:
                _raw = _j.load(_f)
            sb = _raw.get("signal_builders", {})
            active_builders = sb.get("active_builders", _SB_IDS)
            builders_cfg = sb.get("builders", {})
            for bid in _SB_IDS:
                _sb_enabled_vars[bid].set(bid in active_builders)
                params = builders_cfg.get(bid, {})
                for field_key, _, _, _ in _SB_PARAM_META.get(bid, []):
                    var = _sb_fields[bid].get(field_key)
                    if var is None:
                        continue
                    val = params.get(field_key, "")
                    var.set("" if val is None else str(val))
            _sb_status_var.set("  Loaded")
        except Exception as e:
            _sb_status_var.set(f"  ❌ Load failed: {e}")

    def _sb_save():
        try:
            import titan_config as _tc, json as _j
            cfg_path = _tc.get_config_file()
            with open(cfg_path, encoding="utf-8") as _f:
                _raw = _j.load(_f)
            if "signal_builders" not in _raw:
                _raw["signal_builders"] = {"_group": "Signal Builders", "builders": {}}
            sb = _raw["signal_builders"]
            sb["active_builders"] = [bid for bid in _SB_IDS if _sb_enabled_vars[bid].get()]
            if "builders" not in sb:
                sb["builders"] = {}
            for bid in _SB_IDS:
                if bid not in sb["builders"]:
                    sb["builders"][bid] = {}
                target = sb["builders"][bid]
                target["enabled"] = _sb_enabled_vars[bid].get()
                for field_key, _, ftype, _ in _SB_PARAM_META.get(bid, []):
                    var = _sb_fields[bid].get(field_key)
                    if var is None:
                        continue
                    raw_val = var.get().strip()
                    if ftype == "float_none":
                        target[field_key] = float(raw_val) if raw_val else None
                    elif ftype == "int":
                        target[field_key] = int(raw_val) if raw_val else 0
                    elif ftype == "bool":
                        target[field_key] = raw_val.lower() in ("true", "1", "yes")
                    else:
                        try:
                            target[field_key] = float(raw_val) if raw_val else 0.0
                        except ValueError:
                            target[field_key] = raw_val
            with open(cfg_path, "w", encoding="utf-8") as _f:
                _j.dump(_raw, _f, indent=4)
            _tc.reload()
            _sb_status_var.set("  ✅ Saved & reloaded — takes effect next cycle")
            log("🔨 Signal builder config saved and reloaded", "INFO")
        except Exception as e:
            _sb_status_var.set(f"  ❌ Save failed: {e}")

    def _sb_show_builder(bid: str):
        for frame in _sb_builder_frames.values():
            frame.pack_forget()
        _sb_builder_frames[bid].pack(fill="both", expand=True, padx=8, pady=4)

    # ── toolbar ───────────────────────────────────────────────────────────────
    sb_toolbar = tk.Frame(tab_sb, bg="#0d0d1a", pady=6)
    sb_toolbar.pack(fill="x")

    tk.Label(sb_toolbar, text="Builder:", fg="#778899", bg="#0d0d1a", font=mono).pack(side="left", padx=(12, 4))

    sb_dropdown = tk.OptionMenu(sb_toolbar, _sb_builder_var,
                                *[_SB_LABELS[b] for b in _SB_IDS],
                                command=lambda v: _sb_show_builder(
                                    next(b for b in _SB_IDS if _SB_LABELS[b] == v)))
    sb_dropdown.config(bg="#1a1a2a", fg="#00ff88", font=mono, activebackground="#0d0d1a", highlightthickness=0)
    sb_dropdown.pack(side="left", padx=4)

    tk.Button(sb_toolbar, text="💾 SAVE & APPLY", bg="#002a00", fg="#00ff88",
              font=bold_hd, padx=14, command=_sb_save).pack(side="left", padx=10)
    tk.Button(sb_toolbar, text="↺ Reload", bg="#1a1a2a", fg="#778899",
              font=mono, padx=8, command=_sb_load).pack(side="left", padx=4)
    tk.Label(sb_toolbar, textvariable=_sb_status_var, fg="#556677", bg="#0d0d1a", font=mono).pack(side="left", padx=10)

    # ── active builders checkboxes ────────────────────────────────────────────
    sb_active_frame = tk.Frame(tab_sb, bg="#0d0d1a", pady=4)
    sb_active_frame.pack(fill="x", padx=12)
    tk.Label(sb_active_frame, text="Active builders:", fg="#778899", bg="#0d0d1a", font=mono).pack(side="left", padx=(0, 8))
    for bid in _SB_IDS:
        tk.Checkbutton(sb_active_frame, text=_SB_LABELS[bid],
                       variable=_sb_enabled_vars[bid],
                       fg="#00ff88", bg="#0d0d1a", selectcolor="#0d0d1a",
                       activebackground="#0d0d1a", font=mono).pack(side="left", padx=8)

    # ── per-builder param frames ───────────────────────────────────────────────
    sb_body = tk.Frame(tab_sb, bg="#080810")
    sb_body.pack(fill="both", expand=True)

    _sb_builder_frames: dict[str, tk.Frame] = {}
    for bid in _SB_IDS:
        frm = tk.Frame(sb_body, bg="#080810")
        _sb_builder_frames[bid] = frm
        tk.Label(frm, text=f"── {_SB_LABELS[bid]} parameters ──",
                 fg="#00ff88", bg="#080810", font=bold_hd, pady=8, padx=16).grid(
            row=0, column=0, columnspan=3, sticky="w")
        row_idx = 1
        for field_key, label, _, desc in _SB_PARAM_META.get(bid, []):
            var = tk.StringVar()
            _sb_fields[bid][field_key] = var
            tk.Label(frm, text=label, fg="#aaaacc", bg="#080810",
                     font=mono, anchor="w", width=30).grid(
                row=row_idx, column=0, sticky="w", padx=(24, 8), pady=2)
            tk.Entry(frm, textvariable=var, bg="#0d0d1a", fg="#ffcc44",
                     font=mono, width=18, insertbackground="#00ff88").grid(
                row=row_idx, column=1, sticky="w", pady=2, padx=(0, 16))
            tk.Label(frm, text=desc, fg="#556677", bg="#080810",
                     font=mono, anchor="w", wraplength=520, justify="left").grid(
                row=row_idx, column=2, sticky="w", pady=2)
            row_idx += 1

    _sb_show_builder("consensus_basket")
    _sb_load()

    # ── Tab order ─────────────────────────────────────────────────────────────
    nb.add(tab_selector,  text="  🎯 SELECTOR  ")
    nb.add(tab_wallets,   text="  🐳 WALLETS  ")
    nb.add(tab_sb,        text="  🔨 SIGN. CRAFT  ")
    nb.add(tab_live,      text="  📡 SIGNALS  ")
    nb.add(tab_alerts,    text="  🚨 ALERTS  ")
    nb.add(tab_positions, text="  📋 POSITIONS  ")
    nb.add(tab_pnl,       text="  📈 P&L  ")
    nb.add(tab_analysis,  text="  📊 ANALYSIS  ")
    nb.add(tab_diag,      text="  🔍 DIAG  ")
    nb.add(tab_log,       text="  📜 LOG  ")
    nb.add(tab_config,    text="  ⚙ CONFIG  ")

    _tab_keys_by_widget: dict[str, str] = {
        str(tab_live): "signals",
        str(tab_alerts): "alerts",
        str(tab_positions): "positions",
        str(tab_pnl): "pnl",
        str(tab_wallets): "wallets",
        str(tab_analysis): "analysis",
        str(tab_diag): "diag",
        str(tab_log): "log",
    }

    def _selected_tab_key() -> str:
        return _tab_keys_by_widget.get(nb.select(), "")

    # ═══════════════════════════════════════════════════════════════════════════════
    #  RENDERERS
    # ═══════════════════════════════════════════════════════════════════════════════
    _last_signals: list[Signal] = []
    _last_wallets: dict[str, "Wallet"] = {}
    _last_rejects: list[str] = []
    _last_trades: list[object] = []
    _cycle_num           = [0]
    _pending_update      = [False]
    _show_signal_history = [False]
    _signal_history_cache: list[list[Signal]] = [[]]
    _latest_pnl: PnlSummaryDict | None = None
    _latest_trade_stats: TradeStatsDict | None = None
    _latest_trade_history: list[TradeRecord] = []
    _latest_closed_positions: list[Position] = []
    _last_hb_ts          = [0.0]
    _HB_DEAD_SECS = 60
    _HB_BLINK_MS  = 600

    def _require_row_object(value: object, label: str) -> dict[str, object]:
        if isinstance(value, dict):
            return value
        raise TypeError(f"{label} must be an object, got {type(value).__name__}")

    def _require_signal_rows(value: object) -> list[Signal]:
        from titan_signals import Signal as _Signal
        if not isinstance(value, list):
            raise TypeError(f"signals must be a list, got {type(value).__name__}")
        out: list[Signal] = []
        for item in value:
            if isinstance(item, _Signal):
                out.append(item)
            elif isinstance(item, dict):
                try:
                    out.append(_Signal.from_dict(item))
                except Exception:
                    pass
        return out

    def _signal_ev_pct(signal: Signal) -> float:
        return 0.0

    def _signal_age_minutes(signal: Signal) -> float:
        return float(signal.age_min)

    def _market_payload_from_value(value: object) -> Market | None:
        from titan_market import Market as _Market
        if isinstance(value, _Market):
            return value
        return None

    def _load_market_payload(*, cid: str = "", asset: str = "", slug: str = "") -> Market | None:
        import titan_market as market_api
        mkt, _ = market_api.get_market(cid, asset=asset, slug=slug, persist=True)
        return mkt

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

    def _normalise_property_value(value: object) -> object:
        mapping = _previewable_mapping(value)
        if mapping is not None:
            return {key: _normalise_property_value(item) for key, item in mapping.items()}
        if isinstance(value, list):
            return [_normalise_property_value(item) for item in value]
        if isinstance(value, tuple):
            return [_normalise_property_value(item) for item in value]
        return value

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
        detail_win.configure(bg="#0d0d1a")
        detail_win.geometry("660x520")
        detail_win.resizable(True, True)

        # ── header bar ────────────────────────────────────────────────────────
        hdr = tk.Frame(detail_win, bg="#0a0a20", pady=6)
        hdr.pack(fill="x")
        breadcrumb = title if not subtitle else f"{title.rsplit(' / ', 1)[0]}  ›  {subtitle}"
        tk.Label(hdr, text=breadcrumb, fg="#00ff88", bg="#0a0a20",
                 font=bold_hd, anchor="w").pack(side="left", padx=12)
        tk.Label(hdr, text="✕", fg="#334455", bg="#0a0a20",
                 font=bold_hd, cursor="hand2").pack(side="right", padx=10)

        # thin separator
        tk.Frame(detail_win, bg="#1a2a3a", height=1).pack(fill="x")

        # ── treeview ──────────────────────────────────────────────────────────
        prop_style = "Prop.Treeview"
        sty = ttk.Style()
        sty.configure(prop_style,
            background="#0c0c1e", fieldbackground="#0c0c1e",
            foreground="#aabbcc", font=mono, rowheight=24,
            borderwidth=0)
        sty.configure(f"{prop_style}.Heading",
            background="#111128", foreground="#4488aa",
            font=mono_sm, relief="flat")
        sty.map(prop_style,
            background=[("selected", "#1a2a4a")],
            foreground=[("selected", "#00ff88")])

        table_wrap = tk.Frame(detail_win, bg="#0c0c1e")
        table_wrap.pack(fill="both", expand=True, padx=8, pady=8)

        preview_fields = _list_preview_columns(value)
        prop_cols = ("Property", *preview_fields) if preview_fields else ("Property", "Value")
        prop_tree = ttk.Treeview(table_wrap, columns=prop_cols, show="headings", style=prop_style)
        prop_tree.heading("Property", text="PROPERTY")
        prop_tree.column("Property", width=200, minwidth=120, anchor="w", stretch=False)
        if preview_fields:
            for field_name in preview_fields:
                prop_tree.heading(field_name, text=field_name.upper())
                prop_tree.column(field_name, width=140, anchor="w", stretch=True)
        else:
            prop_tree.heading("Value", text="VALUE")
            prop_tree.column("Value", width=420, anchor="w", stretch=True)

        prop_vsb = tk.Scrollbar(table_wrap, orient="vertical", command=prop_tree.yview,
                                bg="#0c0c1e", troughcolor="#0a0a1a", width=10)
        prop_hsb = tk.Scrollbar(table_wrap, orient="horizontal", command=prop_tree.xview,
                                bg="#0c0c1e", troughcolor="#0a0a1a", width=10)
        prop_tree.configure(yscrollcommand=prop_vsb.set, xscrollcommand=prop_hsb.set)
        prop_vsb.pack(side="right", fill="y")
        prop_hsb.pack(side="bottom", fill="x")
        prop_tree.pack(fill="both", expand=True)

        # alternating row tags
        prop_tree.tag_configure("odd",  background="#0c0c1e")
        prop_tree.tag_configure("even", background="#0e0e22")
        prop_tree.tag_configure("drillable", foreground="#55aadd")

        detail_items: dict[str, object] = {}
        if preview_fields and isinstance(value, list):
            for idx, item in enumerate(value):
                tag = "odd" if idx % 2 == 0 else "even"
                mapping = _previewable_mapping(item)
                if mapping is None:
                    text, child_value = _describe_property_value(item)
                    item_id = prop_tree.insert("", "end", tags=(tag,),
                                               values=(f"[{idx}]", text, "", "", "")[:len(prop_cols)])
                    if child_value is not None:
                        detail_items[str(item_id)] = child_value
                    continue
                row_values = [f"[{idx}]"]
                for field_name in preview_fields:
                    raw_value = mapping.get(field_name)
                    text, child_value = _describe_property_value(raw_value)
                    row_values.append(text)
                item_id = prop_tree.insert("", "end", tags=(tag,), values=tuple(row_values))
                detail_items[str(item_id)] = item
        else:
            for idx, row in enumerate(_build_property_rows(value)):
                tag = "odd" if idx % 2 == 0 else "even"
                if row.child_value is not None:
                    tag = "drillable"
                item_id = prop_tree.insert("", "end", tags=(tag,),
                                           values=(row.name, row.value_text))
                if row.child_value is not None:
                    detail_items[str(item_id)] = row.child_value

        value_panel = tk.Frame(detail_win, bg="#0a0a18")
        value_panel.pack(fill="x", padx=8, pady=(0, 4))
        tk.Label(value_panel, text="SELECTED VALUE", fg="#4488aa", bg="#0a0a18",
                 font=mono_sm, anchor="w").pack(anchor="w", padx=2)
        selected_value = tk.Text(
            value_panel,
            height=3,
            bg="#0c0c1e",
            fg="#cfd8e3",
            insertbackground="#cfd8e3",
            selectbackground="#1a2a4a",
            wrap="word",
            font=mono_sm,
            relief="flat",
            bd=1,
        )
        selected_value.pack(fill="x")

        # ── status bar ────────────────────────────────────────────────────────
        tk.Frame(detail_win, bg="#1a2a3a", height=1).pack(fill="x")
        status_bar = tk.Frame(detail_win, bg="#080816", pady=4)
        status_bar.pack(fill="x")
        help_var = tk.StringVar(value="↵ double-click a highlighted row to drill down")
        tk.Label(status_bar, textvariable=help_var, fg="#334455", bg="#080816",
                 font=mono_sm, anchor="w").pack(side="left", padx=10)
        n = len(value) if isinstance(value, (list, dict)) else ""
        if n:
            tk.Label(status_bar, text=f"{n} items", fg="#223344", bg="#080816",
                     font=mono_sm).pack(side="right", padx=10)

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
                help_var.set("scalar — no nested data")
                return
            values = prop_tree.item(item_id).get("values", [])
            prop_name = str(values[0]) if values else "Property"
            show_properties_popup(
                child_value,
                title=f"{title} / {prop_name}",
                subtitle=prop_name,
                parent=detail_win,
            )

        def _update_selected_value(_event: tk.Event[tk.Misc] | None = None) -> None:
            selection = prop_tree.selection()
            value_text = ""
            if selection:
                values = prop_tree.item(selection[0]).get("values", [])
                if len(values) >= 2:
                    value_text = str(values[1])
            selected_value.delete("1.0", tk.END)
            selected_value.insert("1.0", value_text)

        prop_tree.bind("<<TreeviewSelect>>", _update_selected_value)
        prop_tree.bind("<Double-1>", _open_selected_property)
        _update_selected_value()

        if isinstance(owner, (tk.Tk, tk.Toplevel)):
            try:
                detail_win.geometry(f"+{owner.winfo_rootx() + 40}+{owner.winfo_rooty() + 40}")
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
        raw_value = _normalise_property_value(value)
        raw_txt.insert("1.0", json.dumps(raw_value, indent=2, default=str))
        raw_txt.focus_set()

    def show_market_detail(market: Market, signal_title: str = "") -> None:
        win = tk.Toplevel(root)
        market_title = market.title or signal_title or "Market"
        market_slug = market.slug
        win.title(f"Market Detail — {market_title[:50]}")
        win.configure(bg="#060615")
        win.geometry("760x560")
        win.resizable(True, True)

        mono10 = font.Font(family="Courier", size=10)
        mono9 = font.Font(family="Courier", size=9)
        bold9 = font.Font(family="Courier", size=9, weight="bold")
        bold11 = font.Font(family="Courier", size=11, weight="bold")

        liq       = market.liq
        volume    = market.volume
        yes_price = market.yes_price
        no_price  = market.no_price
        hrs_left_text = f"{market.hrs_left:.1f}h" if market.hrs_left is not None else "—"
        ts_text = datetime.fromtimestamp(market.ts).strftime("%Y-%m-%d %H:%M:%S") if market.ts > 0 else "—"

        hf = tk.Frame(win, bg="#0a0a20", pady=8)
        hf.pack(fill="x", padx=8, pady=(8, 0))
        tk.Label(hf, text=f"📈 {market_title}",
                 fg="#00aaff", bg="#0a0a20", font=bold11,
                 wraplength=730, justify="left").pack(anchor="w", padx=12)
        event_slug = market.event_slug or "—"
        # tk.Label(hf, text=f"Slug: {market_slug or '—'}",
        #          fg="#556677", bg="#0a0a20", font=mono9,
        #          wraplength=730, justify="left").pack(anchor="w", padx=12)
        # tk.Label(hf, text=f"Event slug: {event_slug}",
        #          fg="#556677", bg="#0a0a20", font=mono9,
        #          wraplength=730, justify="left").pack(anchor="w", padx=12)

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
            ("End Date", market.end_date or "—", "#ff8844"),
            ("Yes Price", f"${yes_price:.4f}" if yes_price > 0 else "—", "#00ff88"),
            ("No Price", f"${no_price:.4f}" if no_price > 0 else "—", "#ff8844"),
            ("Timestamp", ts_text, "#88ccff"),
            ("Market Type", market.mkt_type or "—", "#ffdd44"),
            ("Outcome Labels", f"{len(market.outcome_labels)}", "#aaaacc"),
        ]
        for i, (lbl, val, col) in enumerate(stats_data):
            sf2.columnconfigure(i % 4, weight=1)
            stat_cell(sf2, lbl, val, col, i % 4, i // 4)

        info_f = tk.Frame(win, bg="#060615")
        info_f.pack(fill="x", padx=8, pady=(0, 6))
        tk.Label(info_f, text="DETAILS", fg="#00ff88", bg="#060615", font=bold9).pack(anchor="w", padx=4, pady=(4, 2))
        detail_lines = [
            f"  Title: {market_title}",
            f"  Slug: {market_slug or '—'}",
            f"  Event slug: {event_slug}",
        ]
        if market.outcome_labels:
            detail_lines.append(f"  Outcomes: {', '.join(str(x) for x in market.outcome_labels[:8])}")
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

        tk.Button(lf, text="🌐 Open Polymarket", bg="#1a1a2a", fg="#00aaff",
                  font=mono9, padx=10, command=market.open_on_polymarket).pack(side="left", padx=4)
        tk.Button(lf, text="📋 Copy Title", bg="#1a2a1a", fg="#00ff88",
                  font=mono9, padx=10, command=copy_title).pack(side="left", padx=4)
        tk.Button(lf, text="🔎 Inspect Raw", bg="#201a2a", fg="#d0b0ff",
                  font=mono9, padx=10, command=inspect_raw_data).pack(side="left", padx=4)
        tk.Button(lf, text="🧩 Properties", bg="#2a2012", fg="#ffcc88",
                  font=mono9, padx=10, command=open_properties).pack(side="left", padx=4)

    def _show_signal_detail(signal: Signal) -> None:
        signal_title = str(signal.title)
        signal_outcome = str(signal.outcome)
        popup_title = signal_title if not signal_outcome else f"{signal_title} [{signal_outcome}]"
        win = tk.Toplevel(root)
        win.title(f"Signal Detail — {signal_title[:50]}")
        win.configure(bg="#060615")
        win.geometry("760x1050")
        win.resizable(True, True)

        mono10 = font.Font(family="Courier", size=10)
        mono9 = font.Font(family="Courier", size=9)
        bold9 = font.Font(family="Courier", size=9, weight="bold")
        bold11 = font.Font(family="Courier", size=11, weight="bold")

        score = float(signal.score or 0.0)
        drift = float(signal.drift or 0.0)
        cur = float(signal.cur or 0.0)
        avg_entry = float(signal.avg_entry or 0.0)
        bet = float(signal.bet or 0.0)
        total_flow = float(signal.total_flow or 0.0)
        ver_flow = float(signal.ver_flow or 0.0)
        n_elite = int(signal.n_elite or 0)
        n_ver = int(signal.n_ver or 0)
        n_total = int(signal.n_total or 0)
        tier = str(signal.tier)
        strategy = str(signal.strategy)
        newest_ts = float(signal.newest_ts or 0.0)
        newest_ts_text = datetime.fromtimestamp(newest_ts).strftime("%Y-%m-%d %H:%M:%S") if newest_ts > 0 else "—"
        first_seen_ts = float(signal.first_seen_ts or 0.0)
        first_seen_ts_text = datetime.fromtimestamp(first_seen_ts).strftime("%Y-%m-%d %H:%M:%S") if first_seen_ts > 0 else "—"
        pnl_color = "#00ff55" if drift <= 0 else "#ffcc44"
        icon = "💎" if tier == "CONVICTION" else ("⚡" if signal.is_hft else "🎯")

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
            ("Confluence", f"{int(signal.n_confluence or 0)}", "#aaaacc"),
            ("Total Wallet", f"{n_total}", "#aaaacc"),
            ("Window", str(signal.strategy).upper(), "#ff8844"),
            ("Stop Loss", "OFF" if signal.stop_loss_pct is None else f"{float(signal.stop_loss_pct or 0.0) * 100:.0f}%", "#aaaacc"),
            ("Score", f"{score:.0f}", "#ffdd44"),
            ("Tier", tier, "#ff8844"),
            ("Large Trade", "YES" if signal.has_large_trade else "NO", "#00ff88" if signal.has_large_trade else "#aaaaaa"),
            ("First Seen TS", first_seen_ts_text, "#ffaa44"),
            ("Newest TS", newest_ts_text, "#88ccff"),
        ]
        for i, (lbl, val, col) in enumerate(stats_data):
            sf2.columnconfigure(i % 4, weight=1)
            stat_cell(sf2, lbl, val, col, i % 4, i // 4)

        info_f = tk.Frame(win, bg="#060615")
        info_f.pack(fill="x", padx=8, pady=(0, 6))
        tk.Label(info_f, text="DETAILS", fg="#00ff88", bg="#060615", font=bold9).pack(anchor="w", padx=4, pady=(4, 2))
        detail_lines = [
            f"  CID: {signal.cid}",
            f"  Market type: {signal.mkt_type}",
            f"  Sports: {'yes' if signal.is_sports else 'no'}",
            f"  HFT: {'yes' if signal.is_hft else 'no'}",
            f"  Conviction: {'yes' if signal.has_large_trade else 'no'}",
        ]
        names = signal.names
        if isinstance(names, list) and names:
            detail_lines.append(f"  Via: {', '.join(str(x) for x in names[:6])}")
        exits = signal.exits_detected
        if isinstance(exits, list) and exits:
            detail_lines.append(f"  Exit alerts: {len(exits)}")
        conviction_detail = signal.conviction_detail
        if isinstance(conviction_detail, str) and conviction_detail.strip():
            detail_lines.append(f"  Conviction detail: {conviction_detail}")
        for line in detail_lines:
            tk.Label(info_f, text=line, fg="#cccccc", bg="#060615", font=mono10,
                     anchor="w", justify="left", wraplength=720).pack(anchor="w", padx=12)

        # ── Wallet list ───────────────────────────────────────────────────────────
        wlist_f = tk.Frame(win, bg="#060615")
        wlist_f.pack(fill="x", padx=8, pady=(0, 6))
        tk.Label(wlist_f, text="WALLETS", fg="#00ff88", bg="#060615", font=bold9).pack(anchor="w", padx=4, pady=(4, 2))

        wallet_cols = ("name", "price", "size", "cash", "status")
        wallet_tree = ttk.Treeview(wlist_f, columns=wallet_cols, show="headings", height=3)
        wallet_tree.heading("name",   text="Name")
        wallet_tree.heading("price",  text="Price")
        wallet_tree.heading("size",   text="Size")
        wallet_tree.heading("cash",   text="Cash")
        wallet_tree.heading("status", text="Status")
        wallet_tree.column("name",   width=220, anchor="w",      stretch=True)
        wallet_tree.column("price",  width=80,  anchor="e",      stretch=False)
        wallet_tree.column("size",   width=80,  anchor="e",      stretch=False)
        wallet_tree.column("cash",   width=90,  anchor="e",      stretch=False)
        wallet_tree.column("status", width=70,  anchor="center", stretch=False)
        wallet_tree.pack(fill="x", padx=4, pady=(0, 4))

        _signal_wallet_addrs: list[str] = []
        seen_wl: set[str] = set()
        all_obs: list[tuple[str, "WalletObservation", bool]] = []
        for addr, obs in (signal.elite_ver or {}).items():
            key = addr.lower()
            if key not in seen_wl:
                seen_wl.add(key)
                all_obs.append((addr, obs, True))
        for addr, obs in (signal.ver or {}).items():
            key = addr.lower()
            if key not in seen_wl:
                seen_wl.add(key)
                all_obs.append((addr, obs, False))

        for addr, obs, is_elite_obs in all_obs:
            prof = _wallet_cache().get(addr.lower())
            display_name = (prof.name if prof else None) or obs.name or addr[:16] + "…"
            status_label = "ELITE" if is_elite_obs else "VER"
            wallet_tree.insert(
                "", "end",
                values=(
                    display_name,
                    f"${obs.price:.4f}",
                    f"{obs.size:.2f}",
                    f"${obs.cash:,.0f}",
                    status_label,
                ),
            )
            _signal_wallet_addrs.append(addr.lower())

        def _on_wallet_list_dblclick(event: tk.Event[tk.Misc]) -> None:
            item_id = wallet_tree.identify_row(event.y)
            if not item_id:
                return
            idx = wallet_tree.index(item_id)
            if idx >= len(_signal_wallet_addrs):
                return
            addr = _signal_wallet_addrs[idx]
            prof = _wallet_cache().get(addr)
            if prof is None:
                log(f"[signal detail] wallet {addr} not in cache", "WARN")
                return
            show_whale_detail(addr, prof)

        wallet_tree.bind("<Double-1>", _on_wallet_list_dblclick)

        lf = tk.Frame(win, bg="#060615")
        lf.pack(fill="x", padx=8, pady=6)
        event_slug = str(signal.event_slug or "")
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
            market_payload = _market_payload_from_value(signal.mkt)
            if market_payload is None:
                market_payload = _load_market_payload(
                    cid=str(signal.cid or ""),
                    asset=str(signal.asset or ""),
                    slug=str(signal.slug or ""),
                )
            if market_payload is None:
                log("[signal detail] market payload missing", "WARN")
                return
            show_market_detail(market_payload, signal_title=signal_title)

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

        price_history: list[tuple[float, float]] = list(signal.price_history or [])
        oldest_ts = float(signal.oldest_ts or 0.0)

        # Ensure a price point exists at the whale entry timestamp so the BUY
        # marker lands at the correct (x, y).  Only insert if the history has no
        # point within 30 s of oldest_ts.
        if oldest_ts > 0 and avg_entry > 0:
            has_entry_point = any(abs(ts - oldest_ts) < 30 for ts, _ in price_history)
            if not has_entry_point:
                price_history = sorted(price_history + [(oldest_ts, avg_entry)], key=lambda p: p[0])

        def _price_chart_rows() -> list[tuple[str, str]]:
            from datetime import datetime as _dt
            return [
                (_dt.fromtimestamp(ts).strftime("%m/%d %H:%M"), f"${v:.4f}")
                for ts, v in price_history
            ]

        chart_frame = ChartFrame(win, get_data_rows=_price_chart_rows, col_headers=("Time", "Price"))
        chart_frame.pack(fill="both", expand=True, padx=8, pady=(4, 8))

        markers: list[ChartMarker] = []
        if first_seen_ts > 0 and first_seen_ts != oldest_ts:
            markers.append(ChartMarker(ts=first_seen_ts, label="👁 first seen", color="#ffdd44"))
        if oldest_ts > 0:
            markers.append(ChartMarker(ts=oldest_ts, label="🐋 first", color="#ffaa44"))
        if newest_ts > 0 and newest_ts != oldest_ts:
            markers.append(ChartMarker(ts=newest_ts, label="🐋 last", color="#ff6600"))

        price_chart = PositionChart(chart_frame.chart_area)
        price_chart.pack(fill="both", expand=True)
        price_chart.set_markers(markers)
        price_chart.load(
            history=price_history or None,
            title=signal_title,
            entry_price=avg_entry,
            entry_ts=oldest_ts if oldest_ts > 0 else None,
            empty_message="No price history available",
        )

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
            f"title={signal.title} outcome={signal.outcome}",
            "DEBUG",
        )
        try:
            _show_signal_detail(signal)
        except Exception as e:
            log(f"[signal dblclick] popup failed: {e}", "ERR")

    def _build_wallet_cache(value: object) -> "dict[str, Wallet]":
        if not isinstance(value, list):
            raise TypeError(f"wallet must be a list, got {type(value).__name__}")
        return {w.addr: w for w in value}

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
                from titan_signals import Signal as _Signal
                if isinstance(row, dict) and "signal" in row:
                    s = row.get("signal")
                    if not isinstance(s, _Signal):
                        continue
                    recorded_at = row.get("recorded_at", "")
                    hist_suffix = f" HIST {recorded_at[11:16]}" if isinstance(recorded_at, str) and len(recorded_at) >= 16 else " HIST"
                else:
                    s = row
                    hist_suffix = ""
                if not isinstance(s, _Signal):
                    continue
                hft_tag  = "⚡" if s.is_hft else ""
                exit_tag = " ⚠EXIT" if s.exits_detected else ""
                mode_str = f"{hft_tag}{s.strategy.upper()}{exit_tag}{hist_suffix}"
                full_title = f"{s.title}  [{s.outcome}]"
                item_id = sig_tree.insert("", "end", values=(
                    f"{s.score:.0f}",
                    full_title[:90],
                    s.outcome,
                    f"${s.avg_entry:.4f}",
                    f"${s.cur:.4f}",
                    f"{(s.drift or 0)*100:+.1f}%",
                    f"{_signal_age_minutes(s):.0f}m",
                    f"${s.total_flow:,.0f}",
                    f"{s.n_ver}/{s.n_total}",
                    mode_str,
                ), tags=(s.tier,))
                _signal_tree_items[str(item_id)] = s
            except Exception as e:
                log(f"[render_signals row error] {e}", "ERR")
        log(
            f"[render_signals] tree_rows={len(sig_tree.get_children())} "
            f"backing_rows={len(_signal_tree_items)} history_mode={_show_signal_history[0]}",
            "DEBUG",
        )

    sig_tree.bind("<Double-1>", _on_signal_double_click)
    
    
    def render_alerts(
        signals: list[Signal],
        wallets: dict[str, Wallet],
        pnl_summary: PnlSummaryDict,
    ) -> None:
        alert_txt.configure(state="normal")
        alert_txt.delete("1.0", tk.END)
        ts  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        top = [s for s in signals if s.tier in ("CONVICTION","ALERT","STRONG","HFT","ELITE_ONLY")]

        if not top:
            med = [s for s in signals if s.tier == "MEDIUM"]
            alert_txt.insert(tk.END,
                f"\n  {'═'*66}\n  No ALERT/STRONG/HFT signals  —  {ts}\n  {'─'*66}\n")
            if med:
                alert_txt.insert(tk.END, f"  {len(med)} MEDIUM signal(s):\n\n")
                for s in med:
                    alert_txt.insert(tk.END,
                        f"    • {s.title[:60]}\n"
                        f"      [{s.outcome}] @ ${s.cur:.4f} (whale entry ${s.avg_entry:.4f}) | "
                        f"drift {s.drift*100:+.1f}% | score {s.score:.0f}\n"
                        f"      via: {', '.join((s.names or [])[:3])}\n\n")
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
            mkt  = s.mkt
            hrs_obj = mkt.hrs_left if mkt is not None else None
            hrs = float(hrs_obj) if isinstance(hrs_obj, (int, float)) else 0.0
            hrs_s = f"{hrs:.0f}h" if hrs else "open"
    
            tier_icons = {
                "CONVICTION": "💎💎💎 BIG CONVICTION",
                "ALERT":      "🟢🟢🟢",
                "STRONG":     "🟡🟡",
                "HFT":        "⚡⚡ HFT SPIKE",
                "ELITE_ONLY": "🔥 ELITE-ONLY",
            }
            icon = tier_icons.get(s.tier, "🔵")

            d = s.drift
            if abs(d) < 0.02:   fresh = "⚡ VERY FRESH"
            elif abs(d) < 0.06: fresh = "✅ FRESH"
            elif abs(d) < 0.10: fresh = "✅ ACCEPTABLE"
            else:                fresh = "⚠ STALE"
    
            in_market  = s.cid in pnl_summary["active_market_cids"]
            trade_note = "🤖 AUTO-BOUGHT" if in_market else "⏳ Watching (below ALERT threshold)"
            cd_note    = ""
            if s.cid in pnl_summary["cooldown_cids"]:
                remaining = _cfg.get("EXIT_COOLDOWN_SECONDS", 300) - (time.time() - pnl_summary["cooldown_cids"][s.cid])
                cd_note   = f"\n  ⏳ COOLDOWN: {remaining/60:.0f}min remaining\n"

            exit_warn = "\n  ⚠ EXIT ALERT: Whale selling detected.\n" if s.exits_detected else ""
            bd = s.bd

            elite_detail = []
            for w, t in list((s.elite_ver or {}).items())[:5]:
                wprof = _wallet_cache().get(w)
                wname = (wprof.name if wprof else None) or w[:14]+"…"
                elite_detail.append(
                    f"    🔥 {wname:<20} WR:{wprof.win_rate*100:.0f}%  "
                    f"PnL:${wprof.total_pnl:+,.0f}  Score:{wprof.score:.2f}  "
                    f"Entry:${t.price:.4f}  Cash:${t.cash:,.0f}  "
                    f"{'⚡HFT' if wprof.hft else ''}"
                ) if wprof else elite_detail.append(
                    f"    🔥 {wname:<20} Entry:${t.price:.4f}  Cash:${t.cash:,.0f}"
                )
    
            alert_txt.insert(tk.END,
                f"{'═'*70}\n"
                f"  {icon}  #{i} [{s.tier}]  Score: {s.score:.0f}/100  [{s.strategy.upper()}]\n"
                f"  {trade_note}\n"
                f"{'═'*70}\n"
                f"{exit_warn}{cd_note}\n"
                f"  MARKET\n  {'─'*50}\n"
                f"  {s.title}\n"
                f"  Outcome: {s.outcome}\n"
                f"  Liq ${mkt.liq:,.0f}  Vol ${mkt.volume:,.0f}  Closes {mkt.end_date} ({hrs_s})\n"
                f"  https://polymarket.com/event/{mkt.slug}\n\n"
                f"  ACTION\n  {'─'*50}\n"
                f"  Buy {s.outcome.upper()} @ ${s.cur:.4f} ({s.cur*100:.1f}¢)\n"
                f"  Whale avg entry:  ${s.avg_entry:.4f}  →  Now: ${s.cur:.4f}\n"
                f"  Drift: {s.drift*100:+.1f}%  {fresh}\n"
                f"  Auto-size: ${s.bet:.2f}  ({s.bet/max(pnl_summary['bankroll'],0.01)*100:.1f}% bankroll)\n"
                f"  Shares: ~{s.bet/max(s.cur,0.01):.1f}\n\n"
                f"  WALLET INTEL  ({s.n_elite} elite / {s.n_ver} total verified)\n  {'─'*50}\n"
            )
            for line in elite_detail:
                alert_txt.insert(tk.END, line + "\n")
            alert_txt.insert(tk.END,
                f"\n  Total verified flow: ${s.ver_flow:,.0f}  "
                f"Largest single: ${s.max_bet_cash or 0:,.0f}\n"
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
    
    
    def render_open_positions(closed_positions: list[Position]) -> None:
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
            _open_tree_items.clear()
            _closed_tree_items.clear()
            for pos in closed_positions:
                entry   = pos.entry_price
                w_entry = pos.avg_entry or entry
                exit_p  = pos.exit_price or entry
                pnl_usd = pos.pnl_usdc
                pnl_pct = pos.pnl_pct or ((exit_p - entry) / max(entry, 0.001) * 100)
                hold_min  = (pos.exit_ts - pos.entry_ts) / 60 if pos.exit_ts and pos.entry_ts else 0.0
                whale_str = ", ".join(
                    (p.name if (p := _wallet_cache().get(w)) else None) or w[:10]+"…"
                    for w in pos.elite_wallets[:2]
                )
                tag       = "CLOSED_WIN" if pnl_usd >= 0 else "CLOSED_LOSS"
                reason    = pos.reason or "CLOSED"
                iid = pos_tree.insert("", "end", values=(
                    pos.title[:48],
                    pos.outcome,
                    f"${w_entry:.4f}",
                    f"${entry:.4f}",
                    f"${exit_p:.4f}",
                    f"{pnl_pct:+.1f}%",
                    f"${pnl_usd:+.3f}",
                    f"${pos.bet:.2f}",
                    f"{hold_min:.0f}m",
                    whale_str[:30],
                    f"{pos.score:.0f}",
                    reason[:18],
                ), tags=(tag,))
                _closed_tree_items[iid] = pos
                new_item_map[_closed_position_selection_key(pos)] = iid
            pos_var.set(f"Pos: {len(closed_positions)} closed")
        else:
            _closed_tree_items.clear()
            _open_tree_items.clear()
            for pos in sorted(_open_positions(),
                              key=lambda x: x.entry_ts, reverse=True):
                entry    = pos.entry_price
                w_entry  = pos.avg_entry or entry
                cur      = pos.cur_price or entry
                shares   = pos.shares
                pnl_pct  = (cur - entry) / max(entry, 0.001) * 100
                pnl_usd  = (cur - entry) * shares
                hold_min = (now_t - pos.entry_ts) / 60 if pos.entry_ts else 0.0

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

                elite_names: list[str] = []
                for index, wallet_addr in enumerate(pos.elite_wallets[:3]):
                    wallet_key = str(wallet_addr).lower()
                    prof = _wallet_cache().get(wallet_key)
                    if prof is not None and prof.dead:
                        elite_names.append(prof.name or wallet_key[:10]+"…")
                    elif index < len(pos.elite_names) and pos.elite_names[index]:
                        elite_names.append(str(pos.elite_names[index]))
                    else:
                        elite_names.append((prof.name if prof else None) or wallet_key[:10]+"…")
                whale_str = ", ".join(elite_names[:2])
                if pos.n_confluence:
                    whale_str += f" +{pos.n_confluence}conf"
                if pos.is_hft:
                    whale_str = "⚡" + whale_str

                hft_tag   = "⚡" if pos.is_hft else ""
                conv_tag  = "💎" if pos.is_conviction else ""
                title_str = f"{conv_tag}{hft_tag}{pos.title}"
                outcome_str = pos.outcome
                if pos.dead_wallets:
                    ws_str = "☠ DEAD WALLET - NO EXIT SIGNAL"
                    tag = "DEAD"
                    title_str = f"☠ {title_str}"

                iid = pos_tree.insert("", "end", values=(
                    title_str[:48],
                    outcome_str,
                    f"${w_entry:.4f}",
                    f"${entry:.4f}",
                    f"${cur:.4f}",
                    f"{pnl_pct:+.1f}%",
                    f"${pnl_usd:+.3f}",
                    f"${pos.bet:.2f}",
                    f"{hold_min:.0f}m",
                    whale_str[:30],
                    f"{pos.score:.0f}",
                    ws_str,
                ), tags=(tag,))
                _open_tree_items[iid] = pos
                new_item_map[(title_str[:30], outcome_str)] = iid

            pos_var.set(f"Pos: {len(_open_positions())} open")

        if prev_sel_key and prev_sel_key in new_item_map:
            iid_to_select = new_item_map[prev_sel_key]
            pos_tree.selection_set(iid_to_select)
            pos_tree.see(iid_to_select)
    
    
    def render_wallets(wallets):
        all_wallets = dict(_wallet_cache())
        all_wallets.update(wallets)
        filt = wh_filter_var.get()
    
        wh_tree.delete(*wh_tree.get_children())
        _whale_tree_items.clear()
        def _wallet_sort_key(item: tuple[str, "Wallet"]) -> tuple[int, float]:
            p = item[1]
            if p.dead:        tier_rank = -1
            elif p.elite:     tier_rank = 0
            elif p.verified: tier_rank = 1
            elif p.watchable: tier_rank = 2
            else:            tier_rank = 3
            return (tier_rank, -p.score)

        for w, p in sorted(all_wallets.items(), key=_wallet_sort_key):
            if p.total_pnl < 0 and not (p.dead or p.elite or p.watchable): continue
            if p.score <= 0.10 and not (p.dead or p.watchable):   continue
            if filt == "ELITE" and not p.elite:        continue
            if filt == "VER"   and not p.verified:     continue
            if filt == "HFT"   and not p.hft:          continue
            if filt == "VIP"   and not p.vip:          continue

            row_tag = "DEAD_WALLET" if p.dead else p.tier().name
            item_id = wh_tree.insert("", "end", values=(
                p.name or w[:10]+"…",
                f"{p.score:.2f}",
                f"{p.win_rate*100:.0f}%",
                f"{p.wilson_lb*100:.0f}%",
                p.n_resolved,
                f"${p.total_value:,.0f}",
                f"#{p.lb_rank:,}" if p.lb_rank is not None else "—",
                f"${p.lb_vol:,.0f}" if p.lb_vol is not None else "—",
                f"${p.total_pnl:+,.0f}",
                f"${p.avg_bet:,.0f}",
                f"{p.trades_per_hour:.1f}",
                "DEAD WALLET" if p.dead else p.tier().display(),
                "⚡ HFT" if p.hft else "",
                "⭐" if p.vip else "",
            ), tags=(row_tag,))
            _whale_tree_items[str(item_id)] = (w, p)

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
    
    
    def render_analysis(
        signals: list[Signal],
        trades: list[object],
        wallets: dict[str, Wallet],
        pnl_summary: PnlSummaryDict,
        trade_stats: TradeStatsDict,
    ) -> None:
        analysis_txt.configure(state="normal")
        analysis_txt.delete("1.0", tk.END)
        ts     = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ver    = {w: p for w, p in wallets.items() if p.verified}
        elites = {w.addr: w for w in wallets.values() if w.elite}
        hot_t  = sum(1 for t in trades if isinstance(t, dict) and t.get("window") == "hot")
        hft_t  = sum(1 for t in trades if isinstance(t, dict) and t.get("source") in ("hft_spike_poll",))
        total_pnl = trade_stats["sum_pnl"]
        wins_n    = trade_stats["win_count"]
        wr_pct    = trade_stats["win_rate"] * 100
        n_sells   = trade_stats["sell_count"]

        analysis_txt.insert(tk.END,
            f"{'═'*78}\n  ANALYSIS  —  {ts}\n{'═'*78}\n\n"
            f"PAPER TRADING ACCOUNT\n{'─'*50}\n"
            f"  Bankroll:     ${pnl_summary['bankroll']:.4f}  (start ${pnl_summary['bankroll_start']:.2f})\n"
            f"  Session P&L:  ${pnl_summary['session_pnl']:+.4f}\n"
            f"  Total P&L:    ${total_pnl:+.4f}\n"
            f"  Trades:       {n_sells} closed  WR:{wr_pct:.0f}% ({wins_n}W/{n_sells - wins_n}L)\n"
            f"  Open:         {len(_open_positions())} positions\n\n"
            f"TRADE FEED (this cycle)\n{'─'*50}\n"
            f"  Total: {len(trades)}  hot:{hot_t}  hft_spikes:{hft_t}\n"
            f"  Verified: {len(ver)}  Elite: {len(elites)}  Watchlist: {pnl_summary['watchlist_size']}\n"
            f"  Signals: {len(signals)}\n"
            f"    💎 CONVICTION: {sum(1 for s in signals if s.tier=='CONVICTION')}\n"
            f"    ⚡ HFT:    {sum(1 for s in signals if s.tier=='HFT')}\n"
            f"    🚨 ALERT:  {sum(1 for s in signals if s.tier=='ALERT')}\n"
            f"    🟡 STRONG: {sum(1 for s in signals if s.tier=='STRONG')}\n"
            f"    🔵 MEDIUM: {sum(1 for s in signals if s.tier=='MEDIUM')}\n\n"
            f"ELITE ROSTER\n{'─'*50}\n"
        )
        for w, p in sorted(elites.items(), key=lambda x: x[1].total_pnl, reverse=True):
            hft = "⚡HFT " if p.hft else ""
            analysis_txt.insert(tk.END,
                f"  {hft}{p.name or w[:14]:<24} "
                f"Score:{p.score:.2f}  WR:{p.win_rate*100:.0f}%  "
                f"PnL:${p.total_pnl:+,.0f}  TPH:{p.trades_per_hour:.1f}\n"
            )
        analysis_txt.configure(state="disabled")
    
    
    def render_diagnostics(
        rejects: list[str],
        trades: list[object],
        wallets: dict[str, Wallet],
        pnl_summary: PnlSummaryDict,
    ) -> None:
        diag_txt.configure(state="normal")
        diag_txt.delete("1.0", tk.END)
        ts    = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        now_t = time.time()
    
        cd_lines = []
        cid_to_title = {}
        for trade in trades:
            if not isinstance(trade, dict):
                continue
            cid = trade.get("cid")
            title = trade.get("title")
            if cid and title and cid not in cid_to_title:
                cid_to_title[str(cid)] = str(title)
        for cid, cd_ts in pnl_summary["cooldown_cids"].items():
            remaining = _cfg.get("EXIT_COOLDOWN_SECONDS", 300) - (now_t - cd_ts)
            title = cid_to_title.get(str(cid), str(cid))
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
    
        failed = [(w, p) for w, p in wallets.items() if not p.verified]
        failed.sort(key=lambda x: x[1].score, reverse=True)
        diag_txt.insert(tk.END, f"\n{'═'*72}\n  FAILED WALLETS (top 20)\n{'═'*72}\n")
        for w, p in failed[:20]:
            diag_txt.insert(tk.END,
                f"  {p.name or w[:14]:<22} "
                f"Score:{p.score:.2f}  WR:{p.win_rate*100:.0f}%  "
                f"FAIL: {', '.join(p.fail_reasons)}\n"
            )
        diag_txt.configure(state="disabled")
    
    
    # ═══════════════════════════════════════════════════════════════════════════════
    #  ENGINE CALLBACKS
    # ═══════════════════════════════════════════════════════════════════════════════
    def on_log_cb(msg, level="INFO"):
        root.after(0, lambda: log(msg, level))
        if level == "ERR" and telegram_notifier is not None:
            threading.Thread(target=telegram_notifier.notify_error, args=(msg,), daemon=True).start()

    import titan_state as _ts_mut
    _ts_mut.on_log = on_log_cb
    
    
    def on_position_open_cb(pos):
        def _update():
            whale_str = ", ".join(pos.elite_names[:3]) or "?"
            pos_log_write(
                f"🛒 AUTO-BUY: {pos.title[:80]} [{pos.outcome}] "
                f"@ ${pos.entry_price:.4f} (whale entry ${pos.avg_entry or pos.entry_price:.4f}) "
                f"| ${pos.bet:.2f} | [{pos.tier} {pos.score:.0f}pts] "
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
            whale_str = ", ".join(pos.elite_names[:2]) or "?"
            pos_log_write(
                f"{emoji} AUTO-SELL: {pos.title[:80]} [{pos.outcome}] "
                f"| Entry ${pos.entry_price:.4f} → Exit ${pos.cur_price:.4f} "
                f"| P&L ${pnl_usdc:+.4f} ({pnl_pct*100:+.1f}%) "
                f"| {pos.reason} | via {whale_str}",
                tag
            )
        root.after(0, _update)
        if telegram_notifier is not None:
            threading.Thread(target=telegram_notifier.notify_sell, args=(pos, pnl_usdc, pnl_pct), daemon=True).start()
    
    
    def on_cycle_complete_cb(signals, wallets, rejects, trades):
        nonlocal _last_signals, _last_wallets, _last_rejects, _last_trades
        # Always read from api — it applies age-expiry and reject-clearing logic
        _last_signals = api.get_signals()
        _last_wallets = wallets
        
        if rejects:
            for r in reversed(rejects):
                if r in _last_rejects:
                    _last_rejects.remove(r)
                _last_rejects.insert(0, r)
            _last_rejects = _last_rejects[:50]
            
        _last_trades  = trades
        _pending_update[0] = True

    def on_config_updated_cb(payload: dict) -> None:
        if payload.get("domain") != "wallets":
            return
        group = str(payload.get("group") or "")
        if group not in {"wallet_selector", "wallet_quality", "elite_thresholds"}:
            return

        def _reload_selector_tab() -> None:
            try:
                _sel_load()
                _sel_status_var.set(f"  Reloaded from MCP update: {group}")
            except Exception as e:
                _log_ui_error("selector live reload", e, "WARN")

        root.after(0, _reload_selector_tab)
    
    
    # ═══════════════════════════════════════════════════════════════════════════════
    #  UI REFRESH LOOP  (fetch in bg thread → apply on main thread)
    # ═══════════════════════════════════════════════════════════════════════════════
    _fetch_running = [False]
    _last_rendered_srv_log: list[str] = [""]   # [last_line]
    _last_rendered_cli_log: list[str] = [""]

    def _fill_log_listbox(lb: tk.Listbox, lines: list[str]) -> None:
        lb.delete(0, tk.END)
        lb.insert(tk.END, *lines)
        lb.see(tk.END)

    def _render_logs(logs: str) -> None:
        srv_lines = logs.splitlines()[-600:]
        srv_last  = srv_lines[-1] if srv_lines else ""
        if srv_last != _last_rendered_srv_log[0]:
            _last_rendered_srv_log[0] = srv_last
            _fill_log_listbox(full_log, srv_lines)

        cli_lines = _read_client_log(600).splitlines()
        cli_last  = cli_lines[-1] if cli_lines else ""
        if cli_last != _last_rendered_cli_log[0]:
            _last_rendered_cli_log[0] = cli_last
            _fill_log_listbox(client_log, cli_lines)

    def _render_selected_tab() -> None:
        selected_tab = _selected_tab_key()
        pnl_summary = _latest_pnl
        trade_stats = _latest_trade_stats
        if selected_tab == "signals":
            signal_rows = _signal_history_cache[0] if _show_signal_history[0] else _last_signals
            render_signals(signal_rows)
        elif selected_tab == "alerts" and pnl_summary is not None:
            render_alerts(_last_signals, _last_wallets, pnl_summary)
        elif selected_tab == "positions":
            render_open_positions(_latest_closed_positions)
        elif selected_tab == "pnl" and pnl_summary is not None and trade_stats is not None:
            refresh_pnl_tab(pnl_summary, trade_stats, _latest_trade_history)
        elif selected_tab == "wallets":
            render_wallets(_last_wallets)
        elif selected_tab == "analysis" and pnl_summary is not None and trade_stats is not None:
            render_analysis(_last_signals, _last_trades, _last_wallets, pnl_summary, trade_stats)
        elif selected_tab == "diag" and pnl_summary is not None:
            render_diagnostics(_last_rejects, _last_trades, _last_wallets, pnl_summary)

    def _request_refresh() -> None:
        if not _fetch_running[0]:
            _fetch_running[0] = True
            selected_tab = _selected_tab_key()
            threading.Thread(target=_fetch_and_apply, args=(selected_tab,), daemon=True).start()

    def ui_refresh() -> None:
        _request_refresh()
        root.after(2000, ui_refresh)

    def _fetch_and_apply(selected_tab: str) -> None:
        try:
            data = UiRefreshData()
            try:
                data.pnl = api.get_pnl_summary()
            except Exception as e:
                root.after(0, lambda err=e: _log_ui_error("fetch pnl", err))
            try:
                data.positions = api.get_positions()
            except Exception as e:
                root.after(0, lambda err=e: _log_ui_error("fetch positions", err))
            if selected_tab in {"wallets", "alerts", "analysis", "diag"} or _pending_update[0]:
                try:
                    data.wallets = _build_wallet_cache(api.get_tracked_wallets())
                except Exception as e:
                    root.after(0, lambda err=e: _log_ui_error("fetch wallets", err))
            if selected_tab == "signals":
                if not _show_signal_history[0]:
                    try:
                        data.signals_live = _require_signal_rows(api.get_signals())
                    except Exception as e:
                        root.after(0, lambda err=e: _log_ui_error("fetch live signals", err))
                elif _pending_update[0] or not _signal_history_cache[0]:
                    try:
                        data.signal_history = _require_signal_rows(api.get_signal_history(limit=200))
                    except Exception as e:
                        root.after(0, lambda err=e: _log_ui_error("fetch signal history", err))
            if selected_tab == "positions" and _show_closed[0]:
                try:
                    data.closed_positions = api.get_closed_positions(limit=200)
                except Exception as e:
                    root.after(0, lambda err=e: _log_ui_error("fetch closed positions", err))
            if selected_tab in {"pnl", "analysis"}:
                try:
                    data.trade_stats = api.get_trade_stats()
                except Exception as e:
                    root.after(0, lambda err=e: _log_ui_error("fetch trade stats", err))
            if selected_tab == "pnl":
                try:
                    data.trade_history = api.get_trade_history()
                except Exception as e:
                    root.after(0, lambda err=e: _log_ui_error("fetch trade history", err))
            if selected_tab == "log":
                try:
                    data.logs = api.get_logs(lines=600)
                except Exception as e:
                    root.after(0, lambda err=e: _log_ui_error("fetch logs", err))
            root.after(0, lambda: _ui_apply(data))
        finally:
            _fetch_running[0] = False

    def _ui_apply(data: UiRefreshData) -> None:
        try:
            _ui_apply_inner(data)
        except Exception as e:
            import traceback
            log(f"[_ui_apply crash] {e}\n{traceback.format_exc()[:400]}", "ERR")

    def _ui_apply_inner(data: UiRefreshData) -> None:
        nonlocal _last_wallets, _latest_pnl, _latest_trade_stats, _latest_trade_history
        nonlocal _latest_closed_positions, _open_positions_cache, _wallet_cache_state
        if data.pnl is not None:
            _latest_pnl = data.pnl
        pnl = _latest_pnl
        if pnl is None:
            return
        if data.positions is not None:
            _open_positions_cache = data.positions
        pos = _open_positions()
        if data.wallets is not None:
            _wallet_cache_state = data.wallets
            _last_wallets = data.wallets
        wallets = _last_wallets
        if data.trade_stats is not None:
            _latest_trade_stats = data.trade_stats
        if data.trade_history is not None:
            _latest_trade_history = data.trade_history
        if data.closed_positions is not None:
            _latest_closed_positions = data.closed_positions
        if data.signals_live is not None:
            signals_live = data.signals_live
            _last_signals[:] = signals_live
        else:
            signals_live = None
        if data.signal_history:
            _signal_history_cache[0] = data.signal_history

        signals = signals_live if (not _show_signal_history[0] and signals_live is not None) else _last_signals
        signal_rows = _signal_history_cache[0] if _show_signal_history[0] else signals

        sig_var.set(f"Sigs: {len(signal_rows)}")
        roster_wallets = wallets
        n_ver = sum(1 for p in roster_wallets.values() if p.verified)
        n_elite = sum(1 for p in roster_wallets.values() if p.elite)
        ver_var.set(f"Ver: {n_ver}")
        elite_var.set(f"Elite: {n_elite}")

        if _pending_update[0]:
            _pending_update[0] = False
            _cycle_num[0] += 1

        cycle_var.set(f"Cycle: {_cycle_num[0]}")

        open_value = sum(
            (p.cur_price or p.entry_price) * p.shares
            for p in pos
        )
        bankroll   = pnl.get("bankroll", 0.0)
        bk_start   = pnl.get("bankroll_start", 0.0)
        total_equity = bankroll + open_value
        if bk_start:
            sl_on = _cfg.get("STOP_LOSS_ENABLED", True)
            _live_subtitle_var.set(
                f"Follow The Wallet | ⚡ HFT: {_hft_enabled_var.get()} | BUY when wallet buys, SELL when wallet sells | "
                f"Bankroll ${bk_start:.2f} | StopLoss: {'ON' if sl_on else 'OFF (wallet-exit only)'}"
            )
        n_open = len(pos)
        if n_open > 0:
            bank_var.set(f"Equity: ${total_equity:.2f} (${bankroll:.2f}+{n_open}pos)")
        else:
            bank_var.set(f"Bank: ${bankroll:.2f}")
        pnl_var.set(f"P&L: ${total_equity - bk_start:+.3f}")
        cooldown_var.set(f"CD: {len(pnl.get('cooldown_cids', {}))}")

        try:
            _render_selected_tab()
        except Exception as _e:
            _log_ui_error("render selected tab", _e)

        if data.logs is not None and _selected_tab_key() == "log":
            _render_logs(data.logs)

        if _selected_tab_key() == "positions":
            sel = pos_tree.selection()
            if not sel:
                children = pos_tree.get_children()
                if children:
                    pos_tree.selection_set(children[0])
                    sel = pos_tree.selection()
            if sel:
                _load_selected_position_chart()
    
    
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
                        cid = pos.cid or key[0]
                        outcome = pos.outcome or key[1]
                        asset = pos.asset
                        fast_p = fetch_position_price_fast(cid, asset, outcome)
                        if fast_p is not None and fast_p != pos.cur_price:
                            pos.cur_price = fast_p
                            now_ts = time.time()
                            pos.price_history.append((now_ts, fast_p))
                            if len(pos.price_history) > 2880:
                                del pos.price_history[:-2880]
                            from titan_prices import PRICES
                            PRICES.ingest(asset, [(now_ts, fast_p)])
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
                        (p.cur_price or p.entry_price) * p.shares
                        for p in _open_positions()
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

    def _on_tab_changed(_event: tk.Event[tk.Misc]) -> None:
        try:
            _render_selected_tab()
        except Exception as e:
            _log_ui_error("tab change render", e, "WARN")
        _request_refresh()

    nb.bind("<<NotebookTabChanged>>", _on_tab_changed)
    
    # ═══════════════════════════════════════════════════════════════════════════════
    #  BOOT
    # ═══════════════════════════════════════════════════════════════════════════════
    def on_boot_complete():
        threading.Thread(target=fast_price_updater, daemon=True).start()
        api.subscribe("notifications/message", lambda p: on_log_cb(p.get("msg") or p.get("data", ""), p.get("level", "INFO")))
        def _pos_from_payload(p) -> "Position":
            from titan_position import Position
            return Position.from_dict(p) if isinstance(p, dict) else p

        api.subscribe("titan/position_open",   lambda p: on_position_open_cb(_pos_from_payload(p)))
        api.subscribe("titan/position_close",  lambda p: on_position_close_cb(_pos_from_payload(p["pos"]), p["pnl_usdc"], p["pnl_pct"]))
        api.subscribe("titan/cycle_complete",  lambda p: on_cycle_complete_cb(p["signals"], p["wallets"], p["rejects"], p["trades"]))
        api.subscribe("titan/config_updated",  on_config_updated_cb)
        root.after(1000, ui_refresh)
        _refresh_hft_status_ui()

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
                    _last_wallets = _build_wallet_cache(api.get_tracked_wallets())
                except Exception as e:
                    root.after(0, lambda err=e: _log_ui_error("boot load wallets", err, "WARN"))
                root.after(0, lambda: log(f"📂 Boot signals: {len(_last_signals)} signal(s), {len(_last_rejects)} reject(s), {len(_last_wallets)} wallet(s)", "INFO"))
                if _last_signals or _last_rejects:
                    _pending_update[0] = True
                n_pos   = len(api.get_positions())
                n_whale = len(api.get_tracked_wallets())
                eq_hist = api.get_pnl_summary().get("equity_history", [])
                n_eq    = len(eq_hist)
                if n_eq >= 2:
                    from datetime import datetime as _dt
                    first_ts = _dt.fromtimestamp(eq_hist[0][0]).strftime("%Y-%m-%d %H:%M")
                    root.after(0, lambda: log(f"📂 Boot: {n_pos} position(s) | {n_whale} wallet(s) | equity history: {n_eq} pts from {first_ts}", "INFO"))
                else:
                    root.after(0, lambda: log(f"📂 Boot: {n_pos} position(s) | {n_whale} wallet(s) | equity history: empty", "INFO"))
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
    
                        wallets = sorted(_wallet_cache().values(), key=lambda x: x.score, reverse=True)[:10]
                        signals: list[Signal] = _last_signals[:15] if _last_signals else []
    
                        # Calculate equity
                        open_value = sum((pos.cur_price or pos.entry_price) * pos.shares for pos in _open_positions())
                        total_equity = api.get_pnl_summary()["bankroll"] + open_value
    
                        data = {
                            "last_update": int(time.time() * 1000),
                            "stats": {
                                "equity": total_equity,
                                "bankroll": api.get_pnl_summary()["bankroll"],
                                "start_bankroll": api.get_pnl_summary()["bankroll_start"],
                                "session_pnl": api.get_pnl_summary()["session_pnl"],
                                "open_pos_count": len(_open_positions()),
                                "total_trades": api.get_trade_stats()["sell_count"]
                            },
                            "pnl_history": [round(v, 4) for _, v in (api.get_pnl_summary()["equity_history"][-200:] if api.get_pnl_summary()["equity_history"] else [])],
                            "wallets": [
                                {"wallet": w.addr, "name": w.name or "Unknown", "pnl": w.total_pnl, "volume": w.lb_vol or 0, "score": w.score} for w in wallets
                            ],
                            "signals": [
                                {"question": s.title, "outcome": s.outcome, "suggested_bet": s.bet, "current_price": s.cur, "ev_edge": _signal_ev_pct(s) / 100, "confluence_count": s.n_confluence} for s in signals
                            ],
                            "open_positions": [
                                {"title": p.title, "outcome": p.outcome, "entry": p.entry_price, "cur": p.cur_price, "shares": p.shares, "pnl": ((p.cur_price or 0) - p.entry_price) * p.shares}
                                for p in sorted(_open_positions(), key=lambda x: x.entry_ts, reverse=True)
                            ],
                            "history": [
                                {"title": p.title, "outcome": p.outcome, "pnl": p.pnl_usdc, "pct": (p.pnl_pct or 0) / 100}
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
