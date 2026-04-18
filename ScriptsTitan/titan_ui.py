"""
╔══════════════════════════════════════════════════════════════════════════════╗
║  TITAN V7 — UI MODULE                                                        ║
║                                                                              ║
║  MAJOR V7 UPGRADES:                                                          ║
║  • Positions: full title, who bought, whale entry price, live P&L in USD     ║
║  • Signals: full market title, whale names, live price vs entry               ║
║  • Sniper Alerts: expanded info with market link, whale roster                ║
║  • Whale tab: HFT tag, TPH, avg bet, expandable                              ║
║  • COPY FOR AI: full structured dump with all context                        ║
║  • Price bug FIXED: sports markets now show correct prices                   ║
║  • Sell detection FIXED: positions now actually close                        ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

import tkinter as tk
from tkinter import ttk, font, scrolledtext
import threading
import time
import math
from datetime import datetime
import titan_engine_v3 as engine
import webbrowser

try:
    import pyperclip
    HAS_PYPERCLIP = True
except ImportError:
    HAS_PYPERCLIP = False

try:
    import titan_ai as ai_module
    HAS_AI = True
except ImportError:
    HAS_AI = False

try:
    import titan_telegram as telegram
    telegram_notifier = telegram.TelegramNotifier()
    HAS_TELEGRAM = True
    threading.Thread(target=telegram_notifier.notify_boot, daemon=True).start()
except ImportError:
    telegram_notifier = None
    HAS_TELEGRAM = False


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
    "    🐳  V7   —  WHALE MIRROR ENGINE  🐳",
    "",
]

BOOT_STEPS = [
    ("Initialising auto paper trading engine",   0.18),
    ("Loading verified whale roster",            0.22),
    ("Calibrating drift detection matrices",     0.18),
    ("Setting up P&L graph renderer",            0.20),
    ("Connecting to Polymarket CLOB feed",        0.20),
    ("Arming exit-monitoring sentinels",          0.18),
    ("Loading saved P&L state from disk",         0.22),
    ("TITAN V7 ONLINE — Price Fix Applied",      0.10),
]


def show_loading_screen(root, on_complete):
    frame = tk.Frame(root, bg="#080810")
    frame.place(relx=0, rely=0, relwidth=1, relheight=1)
    inner = tk.Frame(frame, bg="#080810")
    inner.place(relx=0.5, rely=0.5, anchor="center")

    mono_big  = font.Font(family="Courier", size=11, weight="bold")
    mono_anim = font.Font(family="Courier", size=10)
    mono_sm   = font.Font(family="Courier", size=9)

    for line in TITAN_ASCII:
        col = "#00ff88" if ("TITAN" in line or "WHALE" in line or "AUTO" in line or "V7" in line) else "#1a4a2a"
        tk.Label(inner, text=line, fg=col, bg="#080810",
                 font=mono_big, pady=0).pack()

    tk.Label(inner, text="", bg="#080810").pack()
    tk.Label(inner, text="─" * 52, fg="#1a3a2a", bg="#080810", font=mono_sm).pack()
    tk.Label(inner, text="", bg="#080810").pack()

    status_var = tk.StringVar(value="")
    tk.Label(inner, textvariable=status_var, fg="#00cc66", bg="#080810",
             font=mono_anim, width=52, anchor="w").pack()

    pb_frame  = tk.Frame(inner, bg="#080810")
    pb_frame.pack(pady=6)
    pb_canvas = tk.Canvas(pb_frame, width=420, height=16,
                          bg="#0a0a18", highlightthickness=1,
                          highlightbackground="#1a3a2a")
    pb_canvas.pack()

    tick_var = tk.StringVar(value="")
    tk.Label(inner, textvariable=tick_var, fg="#334433",
             bg="#080810", font=mono_sm).pack(pady=2)

    SPINNERS    = ["⠋","⠙","⠹","⠸","⠼","⠴","⠦","⠧","⠇","⠏"]
    spin_idx    = [0]
    total_steps = len(BOOT_STEPS)

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

    def animate_step(step_idx, sub_frame):
        if step_idx >= total_steps:
            status_var.set("  ✅  ALL SYSTEMS NOMINAL")
            draw_bar(1.0)
            tick_var.set("")
            root.after(600, lambda: (frame.destroy(), on_complete()))
            return
        label, duration = BOOT_STEPS[step_idx]
        base_frac  = step_idx / total_steps
        step_frac  = 1.0 / total_steps
        total_sub  = 12
        if sub_frame > total_sub:
            animate_step(step_idx + 1, 0)
            return
        frac    = base_frac + step_frac * (sub_frame / total_sub)
        draw_bar(frac)
        spinner = SPINNERS[spin_idx[0] % len(SPINNERS)]
        spin_idx[0] += 1
        if step_idx == total_steps - 1 and sub_frame > total_sub // 2:
            status_var.set(f"  🚀  {label}")
            tick_var.set("━" * 48)
        else:
            status_var.set(f"  {spinner}  {label}...")
            tick_var.set("")
        delay_ms = int(duration * 1000 / total_sub)
        root.after(delay_ms, lambda: animate_step(step_idx, sub_frame + 1))

    draw_bar(0.0)
    root.after(120, lambda: animate_step(0, 0))


# ═══════════════════════════════════════════════════════════════════════════════
#  ROOT WINDOW
# ═══════════════════════════════════════════════════════════════════════════════
root = tk.Tk()
root.title("🐳 TITAN V7 — Whale Mirror Engine")
root.configure(bg="#080810")
root.geometry("1920x1020")
root.minsize(1400, 800)

mono    = font.Font(family="Courier", size=9)
mono_sm = font.Font(family="Courier", size=8)
mono_lg = font.Font(family="Courier", size=10)
bold_hd = font.Font(family="Courier", size=10, weight="bold")
title_f = font.Font(family="Courier", size=12, weight="bold")
mono_xs = font.Font(family="Courier", size=7)

# ── Header bar ────────────────────────────────────────────────────────────────
hdr = tk.Frame(root, bg="#0a0a1a", pady=5)
hdr.pack(fill="x")

app_title_var = tk.StringVar(value="🐳 TITAN V7 [Wallet 0]")
app_subtitle_var = tk.StringVar(value="STRATEGY: BASE STRAT | ENGINE ACTIVE")

tk.Label(hdr, textvariable=app_title_var,
         fg="#00ff88", bg="#0a0a1a", font=title_f).pack(side="left", padx=12)
tk.Label(hdr, textvariable=app_subtitle_var,
         fg="#1a3a2a", bg="#0a0a1a", font=mono).pack(side="left")

sf = tk.Frame(hdr, bg="#0a0a1a")
sf.pack(side="right", padx=12)

cycle_var    = tk.StringVar(value="Cycle: 0")
wallet_var   = tk.StringVar(value="Wallets: —")
ver_var      = tk.StringVar(value="Verified: —")
bank_var     = tk.StringVar(value="Bank: $20.00")
pnl_var      = tk.StringVar(value="P&L: $0.00")
sig_var      = tk.StringVar(value="Signals: —")
pos_var      = tk.StringVar(value="Pos: —")
status_var   = tk.StringVar(value="⏳ Booting…")
cooldown_var = tk.StringVar(value="CD: 0")
elite_var    = tk.StringVar(value="Elite: 0")

for v, c in [
    (cycle_var,    "#556677"),
    (wallet_var,   "#557755"),
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

# ── Wallet Switcher ───────────────────────────────────────────────────────────
WALLET_NAMES = {
    0: "Base Strat",
    1: "HFT Ultra-Agg",
    2: "The Sniper",
    3: "Conservative",
    4: "Big Whale",
    5: "Scalper",
    6: "Diamond Hand",
    7: "Degen",
    8: "Micro-Noise",
    9: "Strict Elite"
}

wallet_bar = tk.Frame(root, bg="#050510", pady=3)
wallet_bar.pack(fill="x")

active_wallet_var = tk.IntVar(value=0)

def _switch_wallet(idx):
    import titan_state
    import titan_config
    if getattr(titan_state, "active_idx", 0) == idx:
        return
    titan_state.active_idx = idx
    
    app_title_var.set(f"🐳 TITAN V7 [Wallet {idx}]")
    app_subtitle_var.set(f"STRATEGY: {WALLET_NAMES.get(idx, 'Unknown').upper()} | ENGINE ACTIVE")
    
    try:
        titan_config.reload()
        if "cfg_editor" in globals():
            _load_config_into_editor()
    except Exception as e:
        print(f"Config reload failed: {e}")
    
    try:
        sig_tree.delete(*sig_tree.get_children())
        pos_tree.delete(*pos_tree.get_children())
        hist_tree.delete(*hist_tree.get_children())
        wh_tree.delete(*wh_tree.get_children())
    except Exception:
        pass
        
    _pending_update[0] = True

for i in range(10):
    wname = f"Wallet {i}"
    rb = tk.Radiobutton(wallet_bar, text=wname, variable=active_wallet_var, value=i,
                        bg="#050510", fg="#00ff88", selectcolor="#1a3a2a",
                        activebackground="#050510", activeforeground="#ffffff",
                        font=bold_hd, indicatoron=0, width=12,
                        command=lambda idx=i: _switch_wallet(idx))
    rb.pack(side="left", padx=2, pady=2)

# ── Body layout ───────────────────────────────────────────────────────────────
body_pw = tk.PanedWindow(root, orient="horizontal", bg="#080810",
                          sashwidth=5, sashrelief="flat", handlesize=0, bd=0)
body_pw.pack(fill="both", expand=True, padx=6, pady=4)

nb_frame = tk.Frame(body_pw, bg="#080810")
body_pw.add(nb_frame, minsize=1000, stretch="always")

ai_frame = tk.Frame(body_pw, bg="#080810")
body_pw.add(ai_frame, minsize=360, stretch="never")

nb = ttk.Notebook(nb_frame)
nb.pack(fill="both", expand=True)

sty = ttk.Style()
sty.theme_use("clam")
sty.configure("Treeview", background="#0c0c18", fieldbackground="#0c0c18",
    foreground="#cccccc", font=mono, rowheight=26)
sty.configure("Treeview.Heading", background="#13132a",
    foreground="#00ff88", font=bold_hd)
sty.map("Treeview", background=[("selected", "#1a2a4a")])
sty.configure("TNotebook", background="#080810", borderwidth=0)
sty.configure("TNotebook.Tab", background="#0d0d1a", foreground="#556677",
    font=mono, padding=[12, 6])
sty.map("TNotebook.Tab",
    background=[("selected", "#1a1a30")],
    foreground=[("selected", "#00ff88")])


# ═══════════════════════════════════════════════════════════════════════════════
#  UI COMPONENT: LIVE POSITION PRICE CHART
# ═══════════════════════════════════════════════════════════════════════════════
class PositionChart(tk.Canvas):
    PAD_X, PAD_Y = 60, 28

    def __init__(self, parent, **kwargs):
        super().__init__(parent, bg="#050510", highlightthickness=1,
                         highlightbackground="#1a2a4a", **kwargs)
        self._history     = []
        self._title       = ""
        self._entry_price = 0.0
        self._zoom_start  = 0
        self._dirty       = False
        self._last_len    = 0
        self.bind("<Configure>", lambda e: self._mark_dirty())
        self.bind("<MouseWheel>", self._on_scroll)
        self.bind("<Motion>",     self._on_motion)
        self.bind("<Leave>",      lambda e: self.delete("crosshair"))

    def _mark_dirty(self):
        self._dirty = True

    def load(self, history, title, entry_price):
        new_len = len(history) if history else 0
        if (history != self._history or title != self._title or
                entry_price != self._entry_price or new_len != self._last_len):
            self._history     = list(history) if history else []
            self._title       = title
            self._entry_price = entry_price
            self._last_len    = new_len
            self._zoom_start  = 0
            self._dirty = True
        if self._dirty:
            self._redraw()
            self._dirty = False

    def _on_scroll(self, event):
        n = len(self._history)
        if n < 4: return
        step = max(1, n // 8)
        if event.delta > 0:
            self._zoom_start = min(self._zoom_start + step, n - 4)
        else:
            self._zoom_start = max(0, self._zoom_start - step)
        self._redraw()

    def _on_motion(self, event):
        self.delete("crosshair")
        visible = self._history[self._zoom_start:]
        if len(visible) < 2: return
        self.update_idletasks()
        w  = self.winfo_width()  or 600
        h  = self.winfo_height() or 220
        px, py = self.PAD_X, self.PAD_Y
        prices = [p[1] for p in visible]
        lo, hi = min(prices), max(prices)
        if hi == lo: hi += 0.01; lo -= 0.01
        chart_w = w - 2*px
        if chart_w <= 0: return
        idx = int((event.x - px) / chart_w * (len(visible)-1))
        idx = max(0, min(len(visible)-1, idx))
        p   = visible[idx][1]
        cy  = h - (py + (h - 2*py) * ((p - lo) / (hi - lo)))
        self.create_line(event.x, py, event.x, h-py, fill="#334455", tags="crosshair")
        self.create_line(px, cy, w-px, cy, fill="#334455", dash=(2,4), tags="crosshair")
        entry_pnl = (p - self._entry_price) / max(self._entry_price, 0.001) * 100
        lbl = f"${p:.4f}  ({entry_pnl:+.1f}%)"
        bw = len(lbl)*7 + 8
        self.create_rectangle(event.x+4, cy-10, event.x+4+bw, cy+10,
                               fill="#0d1a2a", outline="#1a3a5a", tags="crosshair")
        color = "#00ff55" if entry_pnl >= 0 else "#ff5555"
        self.create_text(event.x+7, cy, text=lbl, fill=color,
                         font=mono_sm, anchor="w", tags="crosshair")

    def _redraw(self):
        self.delete("chart")
        self.delete("crosshair")
        self.update_idletasks()
        w  = self.winfo_width()  or 600
        h  = self.winfo_height() or 220
        px, py = self.PAD_X, self.PAD_Y
        cx_, cy_ = w // 2, h // 2
        visible = self._history[self._zoom_start:]
        if not visible:
            self.create_text(cx_, cy_,
                             text="Select a position to view its live price chart",
                             fill="#334455", font=mono, tags="chart")
            return
        times  = [p[0] for p in visible]
        prices = [p[1] for p in visible]
        lo, hi = min(prices), max(prices)
        if hi == lo: hi += 0.005; lo -= 0.005
        def gx(i): return px + (i / max(len(visible)-1, 1)) * (w - 2*px)
        def gy(v): return h - (py + (h - 2*py) * ((v - lo) / (hi - lo)))
        # Grid lines
        for pv in [lo, lo + (hi-lo)*0.25, lo + (hi-lo)*0.5, lo + (hi-lo)*0.75, hi]:
            yv = gy(pv)
            self.create_line(px, yv, w-px, yv, fill="#0d142a", dash=(2,4), tags="chart")
            self.create_text(px-4, yv, text=f"{pv:.4f}", fill="#334455",
                             anchor="e", font=mono_xs, tags="chart")
        # Time labels
        n_lbl = min(6, len(visible))
        for j in range(n_lbl):
            idx = int(j / max(n_lbl-1,1) * (len(visible)-1))
            ts  = time.strftime("%H:%M", time.localtime(times[idx]))
            self.create_text(gx(idx), h-py+12, text=ts, fill="#334455",
                             font=mono_xs, tags="chart")
        # Entry price line
        ey = gy(self._entry_price)
        if py < ey < h-py:
            self.create_line(px, ey, w-px, ey, fill="#665500", dash=(4,4), tags="chart")
            self.create_text(px-4, ey, text=f"{self._entry_price:.4f}",
                             fill="#998833", anchor="e", font=mono_xs, tags="chart")
        # Area fill
        if len(prices) >= 2:
            poly = [px, h-py]
            for i, p in enumerate(prices):
                poly += [gx(i), gy(p)]
            poly += [gx(len(prices)-1), h-py]
            cur_p = prices[-1]
            fill_col = "#001a0d" if cur_p >= self._entry_price else "#1a0000"
            self.create_polygon(poly, fill=fill_col, outline="", smooth=False, tags="chart")
        # Price line
        coords = []
        for i, p in enumerate(prices): coords += [gx(i), gy(p)]
        use_smooth = len(prices) >= 6
        if len(coords) >= 4:
            cp   = prices[-1]
            line_col = "#00ff88" if cp >= self._entry_price else "#ff5555"
            self.create_line(coords, fill=line_col, width=2, smooth=use_smooth, tags="chart")
        elif len(coords) == 2:
            self.create_oval(coords[0]-4, coords[1]-4, coords[0]+4, coords[1]+4,
                             fill="#00ff88", outline="#ffffff", tags="chart")
        # Buy marker
        bx, by = gx(0), gy(prices[0])
        self.create_text(bx, by-14, text="▲ BUY", fill="#ffdd00", font=mono_sm, tags="chart")
        self.create_oval(bx-4, by-4, bx+4, by+4, fill="#ffdd00", outline="", tags="chart")
        # Current dot
        cp = prices[-1]
        dpx_c, dpy_c = gx(len(prices)-1), gy(cp)
        dot_col = "#00ff88" if cp >= self._entry_price else "#ff5555"
        self.create_oval(dpx_c-5, dpy_c-5, dpx_c+5, dpy_c+5,
                         fill=dot_col, outline="#ffffff", width=1, tags="chart")
        # Header text
        pct   = (cp - self._entry_price) / max(self._entry_price, 0.001) * 100
        color = "#00ff55" if pct >= 0 else "#ff5555"
        self.create_text(px, 10, text=f"📈 {self._title[:60]}",
                         fill="#00ff88", anchor="w", font=bold_hd, tags="chart")
        self.create_text(w-px, 10,
                         text=f"Now ${cp:.4f}  ({pct:+.1f}%)   Entry ${self._entry_price:.4f}   Lo ${lo:.4f}   Hi ${hi:.4f}",
                         fill=color, anchor="e", font=mono_sm, tags="chart")
        hint = (f"↔ {len(visible)}/{len(self._history)} pts | Scroll=zoom | Hover=crosshair")
        self.create_text(w//2, h-8, text=hint, fill="#334455", font=mono_xs, tags="chart")


# ═══════════════════════════════════════════════════════════════════════════════
#  TAB 1: LIVE SIGNALS
# ═══════════════════════════════════════════════════════════════════════════════
tab_live = tk.Frame(nb, bg="#080810")
nb.add(tab_live, text="  🎯 SIGNALS  ")

# Extended columns with more info
sig_cols = ("Sc","Market (Full Title)","Side","WEntry$","Now$","Drift%","Age","Flow$","Whales","Mode")
sig_tree = ttk.Treeview(tab_live, columns=sig_cols, show="headings", height=10)
sw = {"Sc":45,"Market (Full Title)":400,"Side":110,"WEntry$":72,"Now$":72,
      "Drift%":65,"Age":50,"Flow$":80,"Whales":55,"Mode":65}
for c in sig_cols:
    sig_tree.heading(c, text=c)
    sig_tree.column(c, width=sw[c], anchor="w" if c == "Market (Full Title)" else "center")

sig_tree.tag_configure("ALERT",     foreground="#00ff55", background="#001500")
sig_tree.tag_configure("STRONG",    foreground="#ffdd00", background="#181400")
sig_tree.tag_configure("MEDIUM",    foreground="#55aaff", background="#000d1a")
sig_tree.tag_configure("HFT",       foreground="#ff55ff", background="#150015")
sig_tree.tag_configure("ELITE_ONLY",foreground="#ff8844", background="#1a0d00")
sig_tree.tag_configure("STALE",     foreground="#555555", background="#0c0c0c")

sig_vsb = tk.Scrollbar(tab_live, command=sig_tree.yview)
sig_tree.configure(yscrollcommand=sig_vsb.set)
sig_vsb.pack(side="right", fill="y")
sig_tree.pack(fill="x", padx=4, pady=(4,2))

lf = tk.Frame(tab_live, bg="#080810")
lf.pack(fill="both", expand=True, padx=4)
sig_log = tk.Text(lf, bg="#060610", fg="#44ff44", font=mono,
                  selectbackground="#1a2a4a", wrap="word")
sb_ = tk.Scrollbar(lf, command=sig_log.yview, bg="#0d0d1a")
sig_log.configure(yscrollcommand=sb_.set)
sb_.pack(side="right", fill="y")
sig_log.pack(fill="both", expand=True)

tk.Label(tab_live,
    text=f"V7: Price bug FIXED (sports markets correct) · Sell detection FIXED · "
         f"Bankroll ${engine.BANKROLL_START:.2f} · MaxBet {engine.MAX_BET_PCT*100:.0f}% "
         f"· ProfitTarget +{engine.PROFIT_TARGET_PCT*100:.0f}% · StopLoss {engine.STOP_LOSS_PCT*100:.0f}%",
    fg="#335544", bg="#080810", font=mono, pady=2).pack()


# ═══════════════════════════════════════════════════════════════════════════════
#  TAB 2: SNIPER ALERTS
# ═══════════════════════════════════════════════════════════════════════════════
tab_alerts = tk.Frame(nb, bg="#080810")
nb.add(tab_alerts, text="  🚨 ALERTS  ")
alert_txt = scrolledtext.ScrolledText(tab_alerts, bg="#060610",
    fg="#00ff88", font=mono_lg, selectbackground="#1a2a4a", wrap=tk.WORD)
alert_txt.pack(fill="both", expand=True, padx=4, pady=4)


# ═══════════════════════════════════════════════════════════════════════════════
#  TAB 3: OPEN POSITIONS  (MAJOR UPGRADE)
# ═══════════════════════════════════════════════════════════════════════════════
tab_positions = tk.Frame(nb, bg="#080810")
nb.add(tab_positions, text="  📋 POSITIONS  ")

pos_hdr = tk.Frame(tab_positions, bg="#001820", pady=4)
pos_hdr.pack(fill="x", padx=4, pady=(4,0))
tk.Label(pos_hdr, text="🤖  OPEN POSITIONS  (auto-trading active)",
         fg="#00aaff", bg="#001820", font=bold_hd).pack(side="left", padx=8)
tk.Label(pos_hdr,
         text=f"Exits: +{engine.PROFIT_TARGET_PCT*100:.0f}% target | "
              f"{engine.STOP_LOSS_PCT*100:.0f}% stop | elite whale sells | "
              f"{engine.MIN_HOLD_MINUTES}min hold guard | {engine.EXIT_COOLDOWN_SECONDS//60}min cooldown",
         fg="#334455", bg="#001820", font=mono_sm).pack(side="left", padx=4)

# V7: Greatly expanded columns
pos_cols = ("Market","Side","WEntry$","Entry$","Now$","P&L%","P&L$","Bet$","Hold","Bought From","Score","Status")
pos_tree = ttk.Treeview(tab_positions, columns=pos_cols, show="headings", height=9)
pw = {"Market":300,"Side":100,"WEntry$":72,"Entry$":72,"Now$":72,
      "P&L%":70,"P&L$":72,"Bet$":60,"Hold":50,"Bought From":180,"Score":55,"Status":120}
for c in pos_cols:
    pos_tree.heading(c, text=c)
    pos_tree.column(c, width=pw[c], anchor="w" if c in ("Market","Bought From") else "center")
pos_tree.tag_configure("PROFIT",  foreground="#00ff55", background="#001800")
pos_tree.tag_configure("LOSS",    foreground="#ff5555", background="#1a0000")
pos_tree.tag_configure("EXIT",    foreground="#ffffff", background="#2a1a00")
pos_tree.tag_configure("HOLD",    foreground="#ffaa00", background="#1a1400")
pos_tree.tag_configure("NEUTRAL", foreground="#aaaaaa", background="#0c0c18")

pos_vsb = tk.Scrollbar(tab_positions, command=pos_tree.yview)
pos_tree.configure(yscrollcommand=pos_vsb.set)
pos_vsb.pack(side="right", fill="y")
pos_tree.pack(fill="x", padx=4, pady=(4,2))

pos_split = tk.Frame(tab_positions, bg="#080810")
pos_split.pack(fill="both", expand=True, padx=4)

pos_chart_frame = tk.Frame(pos_split, bg="#080810")
pos_chart_frame.pack(side="left", fill="both", expand=True)

pos_graph = PositionChart(pos_chart_frame, height=240)
pos_graph.pack(fill="both", expand=True, padx=2, pady=2)

pos_btn_bar = tk.Frame(pos_chart_frame, bg="#080810")
pos_btn_bar.pack(fill="x")


def force_close():
    sel = pos_tree.selection()
    if not sel: return
    mkt = pos_tree.item(sel[0])['values'][0]
    engine._log(f"⚠ Manual REQUEST to close: {mkt}", "WARN")


tk.Button(pos_btn_bar, text="✖ REQUEST CLOSE", bg="#2a0000", fg="#ff5555",
          font=mono_sm, command=force_close).pack(side="left", padx=4, pady=4)
tk.Button(pos_btn_bar, text="🌐 POLYMARKET", bg="#0a1a3a", fg="#00aaff",
          font=mono_sm,
          command=lambda: webbrowser.open("https://polymarket.com")).pack(side="left", padx=4, pady=4)

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

graph_frame = tk.Frame(tab_pnl, bg="#080810")
graph_frame.pack(fill="both", expand=True, padx=8, pady=4)
graph_canvas = tk.Canvas(graph_frame, bg="#06060f", highlightthickness=1,
                          highlightbackground="#1a1a30")
graph_canvas.pack(fill="both", expand=True)

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


def draw_pnl_graph():
    graph_canvas.delete("all")
    w = graph_canvas.winfo_width()
    h = graph_canvas.winfo_height()
    if w < 10 or h < 10:
        return

    # Use continuous equity history (cash + open position mark-to-market)
    # Falls back to sell-based curve if equity_history not available yet
    eq_hist = getattr(engine, "equity_history", [])
    if len(eq_hist) >= 2:
        points    = [v for _, v in eq_hist]
        use_sells = False
    else:
        sells = [
            t for t in engine.trade_history
            if t.get("type") == "SELL" and t.get("bankroll") is not None
        ]
        if not sells:
            graph_canvas.create_text(w // 2, h // 2,
                text="No trades yet — graph appears after first cycle",
                fill="#334433", font=("Courier", 10), anchor="center")
            return
        points    = [engine.BANKROLL_START] + [float(t["bankroll"]) for t in sells]
        use_sells = True

    if len(points) < 2:
        return

    pad_l, pad_r, pad_t, pad_b = 60, 20, 20, 40
    plot_w = w - pad_l - pad_r
    plot_h = h - pad_t - pad_b

    min_v = min(points)
    max_v = max(points)
    spread = max(max_v - min_v, 0.5)
    min_v -= spread * 0.1
    max_v += spread * 0.1

    def to_x(i): return pad_l + (i / max(len(points) - 1, 1)) * plot_w
    def to_y(v): return pad_t + (1 - (v - min_v) / (max_v - min_v)) * plot_h

    for i in range(7):
        val = min_v + (max_v - min_v) * i / 6
        y   = to_y(val)
        graph_canvas.create_line(pad_l, y, w - pad_r, y, fill="#1a1a28", dash=(2, 4))
        graph_canvas.create_text(pad_l - 4, y, text=f"${val:.3f}",
                                  fill="#335544", font=("Courier", 8), anchor="e")

    y0 = to_y(engine.BANKROLL_START)
    graph_canvas.create_line(pad_l, y0, w - pad_r, y0, fill="#2a4a2a", dash=(4, 3))
    graph_canvas.create_text(pad_l - 4, y0, text=f"${engine.BANKROLL_START:.2f}",
                              fill="#00aa44", font=("Courier", 8), anchor="e")

    # Draw filled area under curve
    poly_pts = [pad_l, to_y(points[0])]
    for i, v in enumerate(points):
        poly_pts += [to_x(i), to_y(v)]
    poly_pts += [to_x(len(points)-1), pad_t + plot_h, pad_l, pad_t + plot_h]
    last_v = points[-1]
    fill_col = "#001a0a" if last_v >= engine.BANKROLL_START else "#1a0000"
    graph_canvas.create_polygon(poly_pts, fill=fill_col, outline="", smooth=False)

    # Draw line segments coloured by direction
    for i in range(1, len(points)):
        x1 = to_x(i - 1); y1 = to_y(points[i - 1])
        x2 = to_x(i);     y2 = to_y(points[i])
        color = "#00ff55" if points[i] >= points[i-1] else "#ff5555"
        graph_canvas.create_line(x1, y1, x2, y2, fill=color, width=2)

    # Dot only at start and end (too many points for per-point dots)
    graph_canvas.create_oval(pad_l-3, to_y(points[0])-3, pad_l+3, to_y(points[0])+3,
                              fill="#aaaaaa", outline="")
    x_end = to_x(len(points) - 1)
    y_end = to_y(last_v)
    cur_color = "#00ff55" if last_v >= engine.BANKROLL_START else "#ff5555"
    graph_canvas.create_oval(x_end-4, y_end-4, x_end+4, y_end+4,
                              fill=cur_color, outline="#ffffff")

    diff = last_v - engine.BANKROLL_START
    # Calculate open positions value
    open_value = sum(
        pos.get("cur_price", pos.get("entry_price", 0)) * pos.get("shares", 0)
        for pos in engine.open_positions.values()
    )
    label = f"${last_v:.3f} ({diff:+.3f})"
    if open_value > 0:
        label += f"  [${engine.paper_bankroll:.2f} cash + ${open_value:.2f} positions]"
    graph_canvas.create_text(min(x_end + 8, w - 200), y_end,
        text=label, fill=cur_color, font=("Courier", 9), anchor="w")

    mode_label = "Equity (cash + positions)" if not use_sells else "Bankroll (closed trades)"
    graph_canvas.create_text(w // 2, h - 8, text=mode_label,
                              fill="#335544", font=("Courier", 8))


def refresh_pnl_tab():
    history = engine.trade_history
    sells   = [t for t in history if t.get("type") == "SELL" and t.get("pnl_usdc") is not None]

    realised_pnl = sum(t["pnl_usdc"] for t in sells)
    # Include unrealised P&L from open positions
    unrealised_pnl = sum(
        (pos.get("cur_price", pos.get("entry_price", 0)) - pos.get("entry_price", 0))
        * pos.get("shares", 0)
        for pos in engine.open_positions.values()
    )
    total_pnl = realised_pnl + unrealised_pnl
    wins      = [t for t in sells if t["pnl_usdc"] >= 0]
    losses    = [t for t in sells if t["pnl_usdc"] < 0]
    win_rate  = len(wins) / max(len(sells), 1) * 100
    avg_pnl   = total_pnl / max(len(sells), 1)
    best      = max((t["pnl_usdc"] for t in sells), default=0)
    worst     = min((t["pnl_usdc"] for t in sells), default=0)
    avg_win   = sum(t["pnl_usdc"] for t in wins) / max(len(wins), 1)
    avg_loss  = sum(abs(t["pnl_usdc"]) for t in losses) / max(len(losses), 1)
    expectancy = (win_rate/100 * avg_win) - ((1 - win_rate/100) * avg_loss) if sells else 0

    stat_vars["total_pnl"].set(f"${total_pnl:+.4f}  (R:{realised_pnl:+.2f} U:{unrealised_pnl:+.2f})")
    stat_vars["session_pnl"].set(f"${engine.session_pnl:+.4f}")
    stat_vars["win_rate"].set(f"{win_rate:.0f}%  ({len(wins)}W/{len(losses)}L)")
    stat_vars["avg_pnl"].set(f"${avg_pnl:+.4f}")
    stat_vars["best"].set(f"${best:+.4f}")
    stat_vars["worst"].set(f"${worst:+.4f}")
    stat_vars["n_trades"].set(str(len(sells)))
    # Bankroll stat = true equity (cash + open positions mark-to-market)
    open_val = sum(
        pos.get("cur_price", pos.get("entry_price", 0)) * pos.get("shares", 0)
        for pos in engine.open_positions.values()
    )
    stat_vars["bankroll"].set(f"${engine.paper_bankroll + open_val:.4f}")
    stat_vars["expectancy"].set(f"${expectancy:+.4f}")

    hist_tree.delete(*hist_tree.get_children())
    for t in reversed(history[-200:]):
        whale_str = ", ".join(t.get("whale_names", [])[:2]) or "—"
        w_entry   = f"${t.get('avg_entry', t.get('entry_price', 0)):.4f}"
        if t.get("type") == "BUY":
            hist_tree.insert("", "end", values=(
                t.get("ts_str", "—"), "BUY",
                t.get("title","")[:40], t.get("outcome",""),
                w_entry,
                f"${t.get('entry_price',0):.4f}",
                "—", "—", "—",
                whale_str,
                f"${t.get('bankroll',0):.3f}",
            ), tags=("BUY",))
        elif t.get("type") == "SELL":
            pnl_u = t.get("pnl_usdc") or 0
            pnl_p = t.get("pnl_pct")  or 0
            tag   = "WIN" if pnl_u >= 0 else "LOSS"
            hist_tree.insert("", "end", values=(
                t.get("ts_str", "—"), "SELL",
                t.get("title","")[:40], t.get("outcome",""),
                w_entry,
                f"${t.get('entry_price',0):.4f}",
                f"${t.get('exit_price',0):.4f}" if t.get("exit_price") else "—",
                f"${pnl_u:+.4f}",
                f"{pnl_p:+.1f}%",
                whale_str,
                f"${t.get('bankroll',0):.3f}",
            ), tags=(tag,))

    draw_pnl_graph()


# ═══════════════════════════════════════════════════════════════════════════════
#  TAB 5: WHALE ROSTER  (UPGRADED)
# ═══════════════════════════════════════════════════════════════════════════════
tab_whales = tk.Frame(nb, bg="#080810")
nb.add(tab_whales, text="  🐳 WHALES  ")

wh_header = tk.Frame(tab_whales, bg="#0d0d1a", pady=4)
wh_header.pack(fill="x", padx=4, pady=(4,0))
tk.Label(wh_header, text="WHALE ROSTER", fg="#00ff88", bg="#0d0d1a", font=bold_hd).pack(side="left", padx=8)
wh_filter_var = tk.StringVar(value="ALL")
for val, label in [("ALL","All"), ("ELITE","🔥 Elite"), ("VER","✅ Verified"), ("HFT","⚡ HFT")]:
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


def log(msg, level="INFO", wallet_idx=None):
    try:
        sig_log.configure(state="normal")
        ts  = datetime.now().strftime("%H:%M:%S")
        tag = level if level in LOG_COLORS else "INFO"
        prefix = f"[W{wallet_idx+1}] " if wallet_idx is not None else ""
        sig_log.insert(tk.END, f"[{ts}] {prefix}{msg}\n", tag)
        line_count = int(sig_log.index("end-1c").split(".")[0])
        if line_count > 3000:
            sig_log.delete("1.0", "600.0")
        sig_log.see(tk.END)
        sig_log.configure(state="disabled")
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════════════════════
#  TAB 8: SYSTEM LOG
# ═══════════════════════════════════════════════════════════════════════════════
tab_log = tk.Frame(nb, bg="#080810")
nb.add(tab_log, text="  📜 LOG  ")

log_tool_bar = tk.Frame(tab_log, bg="#0d0d1a", pady=4)
log_tool_bar.pack(fill="x")


def build_ai_debug_snapshot():
    """Full structured snapshot for AI debug — everything in one block."""
    import time as _t
    from datetime import datetime as _dt

    now_str = _dt.now().strftime("%Y-%m-%d %H:%M:%S")
    sep  = "═" * 72
    sep2 = "─" * 72
    lines = [sep, f"  TITAN V7 — FULL AI DEBUG SNAPSHOT  —  {now_str}", sep, ""]

    # Account
    lines += [
        "┌─ ACCOUNT & SESSION ─────────────────────────────────────────────────┐",
        f"  Bankroll        : ${engine.paper_bankroll:.4f}",
        f"  Start Bankroll  : ${engine.BANKROLL_START:.2f}",
        f"  Session P&L     : ${engine.session_pnl:+.4f}",
        f"  Total P&L       : ${engine.paper_bankroll - engine.BANKROLL_START:+.4f}",
        f"  Cycle Count     : {engine.cycle_count}",
        f"  Open Positions  : {len(engine.open_positions)}",
        f"  Cooldowns Active: {len(engine.cooldown_cids)}",
        f"  Watchlist Size  : {len(engine.watchlist)}",
        f"  Elite Count     : {sum(1 for p in engine.wallet_cache.values() if p.get('elite'))}",
        "└─────────────────────────────────────────────────────────────────────┘", "",
    ]

    # Config
    lines += [
        "┌─ CONFIG ────────────────────────────────────────────────────────────┐",
        f"  Kelly={engine.KELLY_FRACTION*100:.0f}%  Fee={engine.TAKER_FEE_RATE*100:.1f}%  "
        f"MinBet=${engine.MIN_BET}  MaxBet=${engine.MAX_BET_ABS}",
        f"  MinScore={engine.MIN_SCORE}  AlertScore={engine.ALERT_SCORE}  StrongScore={engine.STRONG_SCORE}",
        f"  MinDrift={engine.MIN_DRIFT*100:.0f}%  MaxDrift={engine.MAX_DRIFT*100:.0f}%  "
        f"MaxSlippage={engine.MAX_ENTRY_SLIPPAGE*100:.0f}%",
        f"  ProfitTarget={engine.PROFIT_TARGET_PCT*100:.0f}%  StopLoss={engine.STOP_LOSS_PCT*100:.0f}%  "
        f"HoldGuard={engine.MIN_HOLD_MINUTES}min  Cooldown={engine.EXIT_COOLDOWN_SECONDS//60}min",
        f"  HFT: mirror within {engine.HFT_MIRROR_DELAY_MAX_SECONDS}s  "
        f"min_cash=${engine.HFT_MIN_CASH_PER_TRADE}  min_tph={engine.HFT_MIN_TRADES_PER_HOUR}",
        f"  Elite: PnL≥${engine.ELITE_MIN_PNL:,}  Port≥${engine.ELITE_MIN_PORT:,}  "
        f"Score≥{engine.ELITE_MIN_SCORE}  Res≥{engine.ELITE_MIN_RESOLVED}",
        "└─────────────────────────────────────────────────────────────────────┘", "",
    ]

    # Open positions with full detail
    lines.append("┌─ OPEN POSITIONS ────────────────────────────────────────────────────┐")
    if engine.open_positions:
        for key, pos in engine.open_positions.items():
            try:
                cid, outcome = key if isinstance(key, tuple) else (str(key), "?")
            except Exception:
                cid, outcome = str(key), "?"
            entry    = pos.get("entry_price", 0)
            w_entry  = pos.get("avg_entry", entry)
            cur      = pos.get("cur_price", entry)
            pnl_pct  = (cur - entry) / max(entry, 0.001) * 100
            pnl_abs  = (cur - entry) * pos.get("shares", 0)
            held_min = (_t.time() - pos.get("entry_ts", _t.time())) / 60
            whales   = pos.get("elite_names", []) or [w[:10]+"…" for w in pos.get("elite_wallets", [])]
            lines += [
                f"  [{pos.get('tier','?')}] {pos.get('title','?')[:60]}",
                f"    Outcome: {outcome}  Score: {pos.get('score',0):.0f}  "
                f"HFT: {'YES' if pos.get('is_hft') else 'NO'}",
                f"    Whale Entry: ${w_entry:.4f}  Our Entry: ${entry:.4f}  "
                f"Now: ${cur:.4f}  P&L: {pnl_pct:+.1f}% (${pnl_abs:+.3f})",
                f"    Bet: ${pos.get('bet',0):.2f}  Shares: {pos.get('shares',0):.2f}  "
                f"Held: {held_min:.0f}min  EntryAt: {pos.get('entry_ts_str','?')}",
                f"    Liq: ${pos.get('liq',0):,.0f}  Vol: ${pos.get('volume',0):,.0f}  "
                f"HrsLeft: {pos.get('hrs_left','?')}  Ends: {pos.get('end_date','?')}",
                f"    CID: {cid}",
                f"    Elite Whales: {', '.join(whales)}",
                f"    All Whales: {', '.join(str(w)[:14] for w in pos.get('whale_wallets', [])[:6])}",
                sep2,
            ]
    else:
        lines.append("  (no open positions)")
    lines += ["└─────────────────────────────────────────────────────────────────────┘", ""]

    # Cooldowns
    lines.append("┌─ ACTIVE COOLDOWNS ──────────────────────────────────────────────────┐")
    if engine.cooldown_cids:
        for cid, ts in engine.cooldown_cids.items():
            remaining = max(0, engine.EXIT_COOLDOWN_SECONDS - (_t.time() - ts))
            mkt_title = engine.market_cache.get(cid, {}).get("title", cid[:30])
            lines.append(f"  ⏳ {mkt_title[:52]}  —  {remaining/60:.0f}min left")
    else:
        lines.append("  (none)")
    lines += ["└─────────────────────────────────────────────────────────────────────┘", ""]

    # Signals
    lines.append("┌─ ACTIVE SIGNALS (last cycle) ───────────────────────────────────────┐")
    sigs = _last_signals if _last_signals else []
    if sigs:
        for i, s in enumerate(sigs, 1):
            bd = s.get("bd", {})
            lines += [
                f"  #{i} [{s.get('tier','?')}] Score:{s.get('score',0):.0f}  {s.get('title','?')[:60]}",
                f"     Outcome: {s.get('outcome','')}  CurPrice: ${s.get('cur',0):.4f}  "
                f"WhaleEntry: ${s.get('avg_entry',0):.4f}  Drift: {s.get('drift',0)*100:+.1f}%",
                f"     Age: {s.get('age_min',0):.0f}min  Bet: ${s.get('bet',0):.2f}  "
                f"VerWhales: {s.get('n_ver',0)}  Elite: {s.get('n_elite',0)}  "
                f"Conf: {s.get('n_confluence',0)}",
                f"     Flow: ${s.get('ver_flow',0):,.0f}  Liq: ${s.get('mkt',{}).get('liq',0):,.0f}  "
                f"Vol: ${s.get('mkt',{}).get('volume',0):,.0f}",
                f"     Score Breakdown — W:{bd.get('wallet',0):.1f} C:{bd.get('conf',0)} "
                f"R:{bd.get('rec',0)} O:{bd.get('opp',0)} M:{bd.get('mkt',0):.1f} "
                f"B:{bd.get('bonus',0)} Exit:{bd.get('exit_penalty',0)}",
                f"     Whales: {', '.join(s.get('names', [])[:5])}",
                f"     HFT: {'YES' if s.get('is_hft') else 'NO'}  "
                f"Window: {s.get('window','?')}  EventSlug: {s.get('event_slug','?')[:30]}",
                sep2,
            ]
    else:
        lines.append("  (no signals this cycle)")
    lines += ["└─────────────────────────────────────────────────────────────────────┘", ""]

    # Rejections
    lines.append("┌─ SIGNAL REJECTIONS (last cycle) ────────────────────────────────────┐")
    rejects = _last_rejects if _last_rejects else []
    lines.extend(f"  {r}" for r in rejects) if rejects else lines.append("  (none)")
    lines += ["└─────────────────────────────────────────────────────────────────────┘", ""]

    # Elite roster
    elites = sorted(
        [(w, p) for w, p in engine.wallet_cache.items() if p.get("elite")],
        key=lambda x: x[1].get("total_pnl", 0), reverse=True
    )
    lines.append(f"┌─ ELITE ROSTER ({len(elites)} wallets) ─────────────────────────────────────────┐")
    lines.append(f"  {'Name':<24} {'WR':>5} {'WLB':>5} {'Res':>5} {'Portfolio':>12} "
                 f"{'PnL':>12} {'AvgBet':>8} {'TPH':>6} {'Score':>6} {'HFT':>4}")
    lines.append(f"  {sep2}")
    for w, p in elites:
        name = p.get("name", w[:12])
        lines.append(
            f"  {name:<24} {p.get('win_rate',0)*100:>4.0f}% {p.get('wilson_lb',0)*100:>4.0f}%"
            f" {p.get('n_resolved',0):>5} ${p.get('total_value',0):>10,.0f}"
            f" ${p.get('total_pnl',0):>+11,.0f} ${p.get('avg_bet',0):>6.0f}"
            f"  {p.get('trades_per_hour',0):>5.1f}  {p.get('score',0):>5.2f}"
            f"  {'⚡' if p.get('hft') else '  '}"
        )
    lines += ["└─────────────────────────────────────────────────────────────────────┘", ""]

    # Verified roster
    ver_wallets = sorted(
        [(w, p) for w, p in engine.wallet_cache.items() if p.get("verified") and not p.get("elite")],
        key=lambda x: x[1].get("score", 0), reverse=True
    )
    lines.append(f"┌─ VERIFIED ROSTER ({len(ver_wallets)} non-elite wallets) ─────────────────────────┐")
    for w, p in ver_wallets[:30]:
        name = p.get("name", w[:12])
        lines.append(
            f"  {name:<24} WR:{p.get('win_rate',0)*100:.0f}%  "
            f"PnL:${p.get('total_pnl',0):+,.0f}  "
            f"Score:{p.get('score',0):.2f}  "
            f"TPH:{p.get('trades_per_hour',0):.1f}  "
            f"{'⚡HFT' if p.get('hft') else ''}"
        )
    lines += ["└─────────────────────────────────────────────────────────────────────┘", ""]

    # Trade history
    lines.append("┌─ TRADE HISTORY (last 100) ──────────────────────────────────────────┐")
    for t in engine.trade_history[-100:]:
        typ  = t.get("type", "?")
        icon = "🛒" if typ == "BUY" else ("✅" if (t.get("pnl_usdc") or 0) >= 0 else "❌")
        pnl_str = f"P&L ${t.get('pnl_usdc',0):+.4f} ({t.get('pnl_pct',0):+.1f}%)" if typ == "SELL" else ""
        whale_str = ", ".join(t.get("whale_names", [])[:2]) or "?"
        lines.append(
            f"  {icon} {t.get('ts_str','?')}  {typ:<4}  [{t.get('tier','?')}]  "
            f"{t.get('title','')[:36]}  [{t.get('outcome','')}]"
            f"  Entry:${t.get('entry_price',0):.4f}  Bet:${t.get('bet',0):.2f}"
            f"  {pnl_str}  via:{whale_str}"
        )
    if not engine.trade_history:
        lines.append("  (no trades yet)")
    lines += ["└─────────────────────────────────────────────────────────────────────┘", ""]

    # Whale exit history
    lines.append("┌─ WHALE EXIT HISTORY (last 30) ──────────────────────────────────────┐")
    for ev in engine.WHALE_EXIT_HISTORY:
        lines.append(f"  {ev}")
    if not engine.WHALE_EXIT_HISTORY:
        lines.append("  (no exits detected yet)")
    lines += ["└─────────────────────────────────────────────────────────────────────┘", ""]

    # Raw logs
    lines.append("┌─ RAW SYSTEM LOGS (last 600 lines) ──────────────────────────────────┐")
    lines.extend(engine.SYSTEM_LOGS[-600:])
    lines += ["└─────────────────────────────────────────────────────────────────────┘", ""]

    lines += [sep, f"  END OF SNAPSHOT  —  {now_str}", sep]
    return "\n".join(lines)


def copy_all_logs():
    snapshot = build_ai_debug_snapshot()
    copied   = False
    if HAS_PYPERCLIP:
        try:
            pyperclip.copy(snapshot)
            copied = True
        except Exception:
            pass
    if not copied:
        try:
            root.clipboard_clear()
            root.clipboard_append(snapshot)
            copied = True
        except Exception:
            pass
    if copied:
        n_lines = snapshot.count("\n")
        engine._log(f"📋 Full AI debug snapshot copied ({n_lines} lines)", "INFO")
    else:
        engine._log("⚠ Copy failed — install pyperclip: pip install pyperclip", "ERR")


def save_snapshot_to_file():
    """Save snapshot to a timestamped file in the Logs directory."""
    snapshot = build_ai_debug_snapshot()
    import titan_state
    log_dir = getattr(titan_state, "LOG_DIR", "Logs")
    if not os.path.exists(log_dir):
        os.makedirs(log_dir, exist_ok=True)
        
    fname = os.path.join(log_dir, f"titan_snapshot_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt")
    try:
        with open(fname, "w", encoding="utf-8") as f:
            f.write(snapshot)
        engine._log(f"📄 Snapshot saved to {fname}", "INFO")
    except Exception as e:
        engine._log(f"⚠ Snapshot save failed: {e}", "ERR")


tk.Button(log_tool_bar, text="📋 COPY FULL SNAPSHOT FOR AI", bg="#1a332a", fg="#00ff88",
          font=bold_hd, padx=12, command=copy_all_logs).pack(side="left", padx=10)
tk.Button(log_tool_bar, text="💾 SAVE SNAPSHOT TO FILE", bg="#1a2a3a", fg="#00aaff",
          font=mono, padx=8, command=save_snapshot_to_file).pack(side="left", padx=4)
tk.Label(log_tool_bar,
         text="Copies everything: positions · signals · elites · trades · exits · raw logs (600 lines).",
         fg="#445566", bg="#0d0d1a", font=mono_sm).pack(side="left")

full_log = scrolledtext.ScrolledText(tab_log, bg="#050508", fg="#66ffaa", font=mono_sm,
                                     selectbackground="#1a2a4a", wrap=tk.NONE)
full_log.pack(fill="both", expand=True, padx=4, pady=4)


# ═══════════════════════════════════════════════════════════════════════════════
#  TAB 9: CONFIG EDITOR
# ═══════════════════════════════════════════════════════════════════════════════
import json as _json
import importlib as _importlib
import titan_config as _cfg

tab_config = tk.Frame(nb, bg="#080810")
nb.add(tab_config, text="  ⚙ CONFIG  ")

cfg_toolbar = tk.Frame(tab_config, bg="#0d1a0d", pady=6)
cfg_toolbar.pack(fill="x")

cfg_status_var = tk.StringVar(value="  Loaded from titan_config.json")


def _reload_config_from_json():
    try:
        _cfg.reload()
        _importlib.reload(_cfg)
        cfg_status_var.set(f"  ✅ Reloaded at {datetime.now().strftime('%H:%M:%S')} — takes effect next cycle")
        engine._log("⚙ Config hot-reloaded from titan_config.json", "INFO")
    except Exception as e:
        cfg_status_var.set(f"  ❌ Reload failed: {e}")
        engine._log(f"⚙ Config reload failed: {e}", "ERR")


def _save_config():
    raw = cfg_editor.get("1.0", tk.END).strip()
    try:
        parsed = _json.loads(raw)
    except _json.JSONDecodeError as e:
        cfg_status_var.set(f"  ❌ JSON syntax error: {e}")
        return
    try:
        with open(_cfg.get_config_file(), "w", encoding="utf-8") as f:
            _json.dump(parsed, f, indent=2)
        _reload_config_from_json()
        _load_config_into_editor()
    except Exception as e:
        cfg_status_var.set(f"  ❌ Save failed: {e}")


def _load_config_into_editor():
    try:
        fpath = _cfg.get_config_file()
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

tk.Label(cfg_ref_frame, text="TITAN V7 GUIDE", fg="#00ff88", bg="#0d0d1a",
         font=bold_hd, pady=8).pack()

_GUIDE = """HOW TITAN WORKS (EVERY CYCLE)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CYCLE = every CYCLE_SECONDS (15s)

STEP 1: ELITE + VIP POLL (primary)
  Every VIP/elite wallet polled.
  Fetches last N BUY trades.
  HFT wallets get HFT_POLL_LIMIT.

STEP 2: WATCHLIST POLL
  Verified non-elite wallets.
  Up to 25 per cycle.

STEP 3: PUBLIC FEED (secondary)
  Bulk site-wide BUY trades.
  Finds new wallets for scoring.

STEP 4: WALLET SCORING (cached)
  WR, WilsonLB, PnL computed.
  Cached WALLET_TTL seconds.

STEP 5: SIGNAL BUILDING
  Group by (market, side).
  Need ≥1 ELITE whale.
  Check: drift, slippage, age.
  Score 0-100:
    ≥ALERT_SCORE → auto-buy
    ≥STRONG_SCORE → display
    ≥MIN_SCORE → shown

STEP 6: AUTO TRADE
  Opens on ALERT/HFT signals.
  Guards: MAX_OPEN_POSITIONS,
  cooldown, pre-entry check.

STEP 7: EXIT CHECK (every cycle)
  • +PROFIT_TARGET_PCT → sell
  • Elite original whale sold → sell
  • STOP_LOSS_PCT → sell
  • Market expires < 1.5h → sell

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
V7 FIXES:
  ✅ PRICE FIX: Sports markets
     (Over/Under/team names) now
     show correct prices. Was
     stuck at 0.5450/0.4550.

  ✅ SELL FIX: Exit detection
     now tries /trades?side=SELL
     and /activity fallback.

  ✅ NO LOG SPAM: price warnings
     removed for clean logs.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
WHALE DOMINANCE:
  swisstony = HFT football arb
  bot. He fires MANY signals.
  Other elites you should see:
  GamblingIsAllYouNeed, MEPP,
  0x8dxd, Wickier, BillBenter,
  mwenya + any newly discovered.

  If ONLY sports: that's because
  those are the most liquid and
  the elites are active there.
  Non-sports signals appear when
  your elite whales bet on them.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TOO FEW SIGNALS?
  ↓ MIN_SCORE (try 20)
  ↓ MIN_TRADE_CASH (try 30)
  ↓ MIN_LIQUIDITY (try 100)
  ↓ MIN_VOLUME (try 200)
  ↑ MAX_SIGNAL_AGE_H (try 3)

SCORE BREAKDOWN (0-100):
  Wallet quality  /30
  Confluence      /18
  Recency         /20
  Price window    /15
  Market quality  /10
  Conviction       /5
  Exit penalty    -8x
"""

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
cfg_editor.configure(yscrollcommand=cfg_editor_vsb.set,
                     xscrollcommand=cfg_editor_hsb.set)
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
_last_signals   = {i: [] for i in range(10)}
_last_wallets   = {i: {} for i in range(10)}
_last_rejects   = {i: [] for i in range(10)}
_last_trades    = {i: [] for i in range(10)}
_cycle_num      = [0]
_pending_update = [False]


def render_signals(signals):
    sig_tree.delete(*sig_tree.get_children())
    for s in signals:
        hft_tag  = "⚡" if s.get("is_hft") else ""
        exit_tag = " ⚠EXIT" if s.get("exits_detected") else ""
        mode_str = f"{hft_tag}{s['window'].upper()}{exit_tag}"
        # Full title in the market column
        full_title = f"{s['title']}  [{s['outcome']}]"
        sig_tree.insert("", "end", values=(
            f"{s['score']:.0f}",
            full_title[:80],
            s["outcome"],
            f"${s['avg_entry']:.4f}",
            f"${s['cur']:.4f}",
            f"{s['drift']*100:+.1f}%",
            f"{s['age_min']:.0f}m",
            f"${s['total_flow']:,.0f}",
            f"{s['n_ver']}/{s['n_total']}",
            mode_str,
        ), tags=(s["tier"],))


def render_alerts(signals, wallets):
    alert_txt.configure(state="normal")
    alert_txt.delete("1.0", tk.END)
    ts  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    top = [s for s in signals if s["tier"] in ("CONVICTION", "ALERT", "STRONG", "HFT", "ELITE_ONLY")]

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
        f"  {len(top)} tradeable signal(s)  [V7: Price Fix Active]\n"
        f"{'█'*70}\n\n"
    )

    for i, s in enumerate(top, 1):
        mkt   = s["mkt"]
        hrs   = mkt.get("hrs_left")
        hrs_s = f"{hrs:.0f}h" if hrs else "open"

        tier_icons = {
            "CONVICTION": "💎💎💎 BIG",
            "ALERT":     "🟢🟢🟢",
            "STRONG":    "🟡🟡",
            "HFT":       "⚡⚡ HFT",
            "ELITE_ONLY":"🔥 ELITE-ONLY",
        }
        icon = tier_icons.get(s["tier"], "🔵")

        d = s["drift"]
        if abs(d) < 0.02:    fresh = "⚡ VERY FRESH"
        elif abs(d) < 0.06:  fresh = "✅ FRESH"
        elif abs(d) < 0.10:  fresh = "✅ ACCEPTABLE"
        else:                 fresh = "⚠ STALE"

        in_market     = s["cid"] in engine.active_market_cids
        trade_note    = "🤖 AUTO-BOUGHT" if in_market else "⏳ Watching (below ALERT threshold)"
        cooldown_note = ""
        if s["cid"] in engine.cooldown_cids:
            remaining = engine.EXIT_COOLDOWN_SECONDS - (time.time() - engine.cooldown_cids[s["cid"]])
            cooldown_note = f"\n  ⏳ COOLDOWN: {remaining/60:.0f}min remaining\n"

        exit_warn = "\n  ⚠ EXIT ALERT: Whale selling detected — signal downgraded by auto-trader.\n" if s.get("exits_detected") else ""
        bd = s["bd"]

        # Build detailed whale list
        elite_detail = []
        for w, t in list(s.get("elite_ver", {}).items())[:5]:
            wname = S_wallet_name(w)
            wprof = engine.wallet_cache.get(w, {})
            elite_detail.append(
                f"    🔥 {wname:<20} WR:{wprof.get('win_rate',0)*100:.0f}%  "
                f"PnL:${wprof.get('total_pnl',0):+,.0f}  Score:{wprof.get('score',0):.2f}  "
                f"Entry:${t['price']:.4f}  Cash:${t['cash']:,.0f}  "
                f"{'⚡HFT' if wprof.get('hft') else ''}"
            )
        for w, t in list(s.get("ver", {}).items()):
            if w in s.get("elite_ver", {}): continue
            wname = S_wallet_name(w)
            wprof = engine.wallet_cache.get(w, {})
            elite_detail.append(
                f"    ✅ {wname:<20} WR:{wprof.get('win_rate',0)*100:.0f}%  "
                f"PnL:${wprof.get('total_pnl',0):+,.0f}  Entry:${t['price']:.4f}  "
                f"Cash:${t['cash']:,.0f}"
            )

        alert_txt.insert(tk.END,
            f"{'═'*70}\n"
            f"  {icon}  #{i} [{s['tier']}]  Score: {s['score']:.0f}/100  [{s['window'].upper()}]\n"
            f"  {trade_note}\n"
            f"{'═'*70}\n"
            f"{exit_warn}{cooldown_note}\n"
            f"  MARKET\n  {'─'*50}\n"
            f"  {s['title']}\n"
            f"  Outcome: {s['outcome']}\n"
            f"  Liq ${mkt['liq']:,.0f}  Vol ${mkt['volume']:,.0f}  Closes {mkt['end_date']} ({hrs_s})\n"
            f"  https://polymarket.com/event/{mkt.get('slug','')}\n\n"
            f"  ACTION\n  {'─'*50}\n"
            f"  Buy {s['outcome'].upper()} @ ${s['cur']:.4f} ({s['cur']*100:.1f}¢)\n"
            f"  Whale avg entry:  ${s['avg_entry']:.4f}  →  Now: ${s['cur']:.4f}\n"
            f"  Drift: {s['drift']*100:+.1f}%  {fresh}\n"
            f"  Auto-size: ${s['bet']:.2f}  ({s['bet']/max(engine.paper_bankroll,0.01)*100:.1f}% bankroll)\n"
            f"  Shares: ~{s['bet']/max(s['cur'],0.01):.1f}\n\n"
            f"  WHALE INTEL  ({s['n_elite']} elite / {s['n_ver']} total verified)\n  {'─'*50}\n"
        )
        for line in elite_detail:
            alert_txt.insert(tk.END, line + "\n")
        alert_txt.insert(tk.END,
            f"\n  Total verified flow: ${s['ver_flow']:,.0f}  "
            f"Largest single: ${s.get('max_bet_cash',0):,.0f}\n"
            f"  Age: {s['age_min']:.0f}min ago\n\n"
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


def S_wallet_name(w: str) -> str:
    """Helper to get wallet display name."""
    return engine.wallet_cache.get(w, {}).get("name") or w[:14] + "…"


def render_open_positions():
    now_t = time.time()
    prev_sel_key = None
    sel = pos_tree.selection()
    if sel:
        vals = pos_tree.item(sel[0])['values']
        if vals:
            prev_sel_key = (str(vals[0])[:30], str(vals[1]))

    pos_tree.delete(*pos_tree.get_children())
    new_item_map = {}

    for key, pos in sorted(engine.open_positions.items(),
                           key=lambda x: x[1].get("entry_ts", 0), reverse=True):
        entry    = pos.get("entry_price", 0)
        w_entry  = pos.get("avg_entry", entry)
        cur      = pos.get("cur_price", entry)
        shares   = pos.get("shares", 0)
        pnl_pct  = (cur - entry) / max(entry, 0.001) * 100
        pnl_usd  = (cur - entry) * shares
        hold_min = (now_t - pos.get("entry_ts", now_t)) / 60
        hold_guard = hold_min < engine.MIN_HOLD_MINUTES

        if hold_guard:
            ws_str = f"🔒 HOLD {engine.MIN_HOLD_MINUTES - hold_min:.0f}m"
            tag    = "HOLD"
        elif pnl_pct >= engine.PROFIT_TARGET_PCT * 100 * 0.7:
            ws_str = f"✅ {pnl_pct:+.1f}% → SELL?"
            tag    = "PROFIT"
        elif pnl_pct >= 2:
            ws_str = f"✅ +{pnl_pct:.1f}%"
            tag    = "PROFIT"
        elif pnl_pct <= engine.STOP_LOSS_PCT * 100 * 0.7:
            ws_str = f"⚠ STOP? {pnl_pct:.1f}%"
            tag    = "LOSS"
        elif pnl_pct <= -3:
            ws_str = f"⚠ {pnl_pct:.1f}%"
            tag    = "LOSS"
        else:
            ws_str = "→ Flat"
            tag    = "NEUTRAL"

        elite_names = pos.get("elite_names", [])
        if not elite_names:
            elite_names = [engine.wallet_cache.get(w, {}).get("name", w[:10]+"…")
                          for w in pos.get("elite_wallets", [])[:3]]
        whale_str = ", ".join(elite_names[:2])
        if pos.get("n_confluence", 0):
            whale_str += f" +{pos['n_confluence']}conf"
        if pos.get("is_hft"):
            whale_str = "⚡" + whale_str

        hft_tag   = "⚡" if pos.get("is_hft") else ""
        title_str = f"{hft_tag}{pos.get('title','')}"
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

    if prev_sel_key and prev_sel_key in new_item_map:
        iid_to_select = new_item_map[prev_sel_key]
        pos_tree.selection_set(iid_to_select)
        pos_tree.see(iid_to_select)

    pos_var.set(f"Pos: {len(engine.open_positions)} open")


def render_whales(wallets):
    all_wallets = dict(engine.wallet_cache)
    all_wallets.update(wallets)

    filt = wh_filter_var.get()

    wh_tree.delete(*wh_tree.get_children())
    for w, p in sorted(all_wallets.items(),
                       key=lambda x: x[1].get("score", 0), reverse=True):
        if p.get("total_pnl", 0) < 0 and not p.get("elite"):
            continue
        if p.get("score", 0) <= 0.10 and not p.get("watchable"):
            continue

        # Filter
        if filt == "ELITE" and not p.get("elite"):
            continue
        if filt == "VER" and not p.get("verified"):
            continue
        if filt == "HFT" and not p.get("hft"):
            continue

        in_watch = w in engine.watchlist
        if p.get("elite"):             tag = "ELITE"
        elif p.get("verified"):        tag = "VER"
        elif p.get("score", 0) >= 0.4: tag = "PAR"
        else:                          tag = "REJ"

        status = ("🔥 ELITE" if p.get("elite") else
                  "✅ VER"   if p.get("verified") else
                  "👁 WATCH" if in_watch else "❌")
        hft_mark = "⚡" if p.get("hft") else ""
        wh_tree.insert("", "end", values=(
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
            hft_mark,
        ), tags=(tag,))


def render_analysis(signals, trades, wallets):
    analysis_txt.configure(state="normal")
    analysis_txt.delete("1.0", tk.END)
    ts  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ver = {w: p for w, p in wallets.items() if p.get("verified")}
    elites = {w: p for w, p in engine.wallet_cache.items() if p.get("elite")}
    hot_t  = sum(1 for t in trades if t.get("window") == "hot")
    warm_t = sum(1 for t in trades if t.get("window") == "warm")
    hft_t  = sum(1 for t in trades if t.get("source") in ("hft_poll",))

    sells     = [t for t in engine.trade_history
                 if t.get("type") == "SELL" and t.get("pnl_usdc") is not None]
    total_pnl = sum(t["pnl_usdc"] for t in sells)
    wins_n    = len([t for t in sells if t["pnl_usdc"] >= 0])
    wr_pct    = wins_n / max(len(sells), 1) * 100
    n_cd      = len(engine.cooldown_cids)
    watchable_n = len([w for w, p in wallets.items() if p.get("watchable")])

    # Whale dominance stats
    whale_trade_counts = {}
    for t in trades:
        w = t.get("wallet","")
        if w and engine.wallet_cache.get(w, {}).get("elite"):
            wname = engine.wallet_cache[w].get("name", w[:10])
            whale_trade_counts[wname] = whale_trade_counts.get(wname, 0) + 1

    analysis_txt.insert(tk.END,
        f"{'═'*78}\n  ANALYSIS  —  {ts}  [TITAN V7]\n{'═'*78}\n\n"
        f"PAPER TRADING ACCOUNT\n{'─'*50}\n"
        f"  Starting bankroll: ${engine.BANKROLL_START:.2f}\n"
        f"  Current bankroll:  ${engine.paper_bankroll:.4f}\n"
        f"  Session P&L:       ${engine.session_pnl:+.4f}\n"
        f"  Total P&L:         ${total_pnl:+.4f}\n"
        f"  Completed trades:  {len(sells)}\n"
        f"  Win rate:          {wr_pct:.0f}%  ({wins_n}W/{len(sells)-wins_n}L)\n"
        f"  Open positions:    {len(engine.open_positions)}\n"
        f"  Cooldowns active:  {n_cd}\n\n"
        f"TRADE FEED (this cycle)\n{'─'*50}\n"
        f"  Total trades:    {len(trades)}  (hot:{hot_t}  warm:{warm_t}  hft:{hft_t})\n"
        f"  Unique wallets:  {len(wallets)}\n"
        f"  Watchable:       {watchable_n}\n"
        f"  Verified whales: {len(ver)}\n"
        f"  Elite whales:    {len(elites)}\n"
        f"  Watchlist size:  {len(engine.watchlist)}\n"
        f"  Signals:         {len(signals)}\n"
        f"    💎 CONVICTION: {sum(1 for s in signals if s['tier']=='CONVICTION')}\n"
        f"    ⚡ HFT:    {sum(1 for s in signals if s['tier']=='HFT')}\n"
        f"    🚨 ALERT:  {sum(1 for s in signals if s['tier']=='ALERT')}\n"
        f"    🟡 STRONG: {sum(1 for s in signals if s['tier']=='STRONG')}\n"
        f"    🔵 MEDIUM: {sum(1 for s in signals if s['tier']=='MEDIUM')}\n\n"
        f"WHALE DOMINANCE (elite trades in feed)\n{'─'*50}\n"
    )
    for wname, cnt in sorted(whale_trade_counts.items(), key=lambda x: -x[1])[:10]:
        bar = "█" * min(cnt, 30)
        analysis_txt.insert(tk.END, f"  {wname:<24} {bar} {cnt}\n")

    analysis_txt.insert(tk.END, f"\nELITE ROSTER\n{'─'*50}\n")
    for w, p in sorted(elites.items(), key=lambda x: x[1].get("total_pnl",0), reverse=True):
        hft = "⚡HFT " if p.get("hft") else ""
        analysis_txt.insert(tk.END,
            f"  {hft}{p.get('name',w[:14]):<24} "
            f"Score:{p.get('score',0):.2f}  WR:{p.get('win_rate',0)*100:.0f}%  "
            f"PnL:${p.get('total_pnl',0):+,.0f}  TPH:{p.get('trades_per_hour',0):.1f}  "
            f"AvgBet:${p.get('avg_bet',0):,.0f}\n"
        )

    if signals:
        analysis_txt.insert(tk.END, f"\nSIGNALS\n{'─'*50}\n")
        for i, s in enumerate(signals, 1):
            bd      = s["bd"]
            in_pos  = s["cid"] in engine.active_market_cids
            in_note = " [🤖 IN POSITION]" if in_pos else ""
            on_cd   = s["cid"] in engine.cooldown_cids
            cd_note = " [⏳ COOLDOWN]" if on_cd else ""
            analysis_txt.insert(tk.END,
                f"\n  #{i} [{s['tier']}] {s['score']:.0f}  {s['title'][:55]}{in_note}{cd_note}\n"
                f"     [{s['outcome']}]  WhaleEntry:${s['avg_entry']:.4f}→Now:${s['cur']:.4f}"
                f"  drift {s['drift']*100:+.1f}%  age {s['age_min']:.0f}min\n"
                f"     W:{bd.get('wallet',0):.1f} C:{bd.get('conf',0)}"
                f" R:{bd.get('rec',0)} O:{bd.get('opp',0)}"
                f" M:{bd.get('mkt',0):.1f} B:{bd.get('bonus',0)}"
                f" Exit:{bd.get('exit_penalty',0)}\n"
                f"     via: {', '.join(s['names'][:4])}\n"
            )
    analysis_txt.configure(state="disabled")


def render_diagnostics(rejects, trades, wallets):
    diag_txt.configure(state="normal")
    diag_txt.delete("1.0", tk.END)
    ts    = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    now_t = time.time()

    cd_lines = []
    for cid, cd_ts in engine.cooldown_cids.items():
        remaining = engine.EXIT_COOLDOWN_SECONDS - (now_t - cd_ts)
        mkt       = engine.market_cache.get(cid, {})
        title     = mkt.get("title", cid[:30])
        cd_lines.append(f"  ⏳ {title[:45]}  {remaining/60:.0f}min left")

    diag_txt.insert(tk.END,
        f"{'═'*72}\n  DIAGNOSTICS  —  {ts}  [V7]\n{'═'*72}\n\n"
    )
    if cd_lines:
        diag_txt.insert(tk.END, f"COOLDOWNS ACTIVE ({len(cd_lines)})\n{'─'*72}\n")
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
            f"Score:{p.get('score',0):.2f}  "
            f"WR:{p.get('win_rate',0)*100:.0f}%  "
            f"Res:{p.get('n_resolved',0)}  "
            f"FAIL: {', '.join(p.get('fail_reasons', []))}\n"
        )
    diag_txt.configure(state="disabled")


# ═══════════════════════════════════════════════════════════════════════════════
#  ENGINE CALLBACKS
# ═══════════════════════════════════════════════════════════════════════════════
def on_log_cb(msg, level="INFO", wallet_idx=None):
    root.after(0, lambda: log(msg, level, wallet_idx=wallet_idx))
    if level == "ERR" and HAS_TELEGRAM:
        threading.Thread(target=telegram_notifier.notify_error, args=(msg,), daemon=True).start()


def on_position_open_cb(pos):
    import titan_state
    w_idx = getattr(titan_state._local, "engine_idx", titan_state.active_idx)
    s_name = WALLET_NAMES.get(w_idx, "Unknown")
    
    def _update():
        elite_names = pos.get("elite_names", [])
        whale_str = ", ".join(elite_names[:3]) or "?"
        pos_log_write(
            f"🛒 AUTO-BUY [W{w_idx}]: {pos['title'][:35]} [{pos['outcome']}] "
            f"@ ${pos['entry_price']:.4f} (whale entry ${pos.get('avg_entry',pos['entry_price']):.4f}) "
            f"| ${pos['bet']:.2f} | [{pos['tier']} {pos['score']:.0f}pts] "
            f"| via {whale_str}",
            "BUY"
        )
        nb.select(tab_positions)
    root.after(0, _update)
    if HAS_TELEGRAM:
        threading.Thread(target=telegram_notifier.notify_buy, args=(pos, w_idx, s_name), daemon=True).start()


def on_position_close_cb(pos, pnl_usdc, pnl_pct):
    import titan_state
    w_idx = getattr(titan_state._local, "engine_idx", titan_state.active_idx)
    s_name = WALLET_NAMES.get(w_idx, "Unknown")
    
    def _update():
        tag   = "SELL_W" if pnl_usdc >= 0 else "SELL_L"
        emoji = "✅" if pnl_usdc >= 0 else "❌"
        elite_names = pos.get("elite_names", [])
        whale_str = ", ".join(elite_names[:2]) or "?"
        pos_log_write(
            f"{emoji} AUTO-SELL [W{w_idx}]: {pos['title'][:32]} [{pos['outcome']}] "
            f"| Entry ${pos['entry_price']:.4f} → Exit ${pos.get('cur_price',0):.4f} "
            f"| P&L ${pnl_usdc:+.4f} ({pnl_pct*100:+.1f}%) "
            f"| {pos.get('reason','')} | via {whale_str}",
            tag
        )
    root.after(0, _update)
    if HAS_TELEGRAM:
        threading.Thread(target=telegram_notifier.notify_sell, args=(pos, pnl_usdc, pnl_pct, w_idx, s_name), daemon=True).start()


def on_cycle_complete_cb(signals, wallets, rejects, trades, wallet_idx=0):
    global _last_signals, _last_wallets, _last_rejects, _last_trades
    _last_signals[wallet_idx] = signals
    _last_wallets[wallet_idx] = wallets
    _last_rejects[wallet_idx] = rejects
    _last_trades[wallet_idx]  = trades
    import titan_state
    if getattr(titan_state, "active_idx", 0) == wallet_idx:
        _pending_update[0] = True


# ═══════════════════════════════════════════════════════════════════════════════
#  UI REFRESH LOOP
# ═══════════════════════════════════════════════════════════════════════════════
def ui_refresh():
    global _cycle_num
    
    import titan_state
    idx = getattr(titan_state, "active_idx", 0)

    if _pending_update[0]:
        _pending_update[0] = False
        _cycle_num[0] += 1

        signals = _last_signals.get(idx, [])
        wallets = _last_wallets.get(idx, {})
        rejects = _last_rejects.get(idx, [])
        trades  = _last_trades.get(idx, [])

        n_ver   = sum(1 for p in wallets.values() if p.get("verified"))
        n_elite = sum(1 for p in engine.wallet_cache.values() if p.get("elite"))

        for fn in (
            lambda: render_signals(signals),
            lambda: render_alerts(signals, wallets),
            lambda: render_analysis(signals, trades, wallets),
            lambda: render_diagnostics(rejects, trades, wallets),
        ):
            try:
                fn()
            except Exception as e:
                pass

        wallet_var.set(f"Wallets: {len(wallets)}")
        ver_var.set(f"Ver: {n_ver}")
        elite_var.set(f"Elite: {n_elite}")
        sig_var.set(f"Sigs: {len(signals)}")

    try:
        render_open_positions()
    except Exception:
        pass
    try:
        refresh_pnl_tab()
    except Exception:
        pass
    try:
        render_whales(_last_wallets)
    except Exception:
        pass

    cycle_var.set(f"Cycle: {_cycle_num[0]}")
    # Show true portfolio equity = cash + mark-to-market open positions
    open_value = sum(
        pos.get("cur_price", pos.get("entry_price", 0)) * pos.get("shares", 0)
        for pos in engine.open_positions.values()
    )
    total_equity = engine.paper_bankroll + open_value
    n_open = len(engine.open_positions)
    if n_open > 0:
        bank_var.set(f"Equity: ${total_equity:.2f} (${engine.paper_bankroll:.2f}+{n_open}pos)")
    else:
        bank_var.set(f"Bank: ${engine.paper_bankroll:.2f}")
    total_pnl = total_equity - engine.BANKROLL_START
    pnl_color = "#00ff88" if total_pnl >= 0 else "#ff5555"
    pnl_var.set(f"P&L: ${total_pnl:+.3f}")
    cooldown_var.set(f"CD: {len(engine.cooldown_cids)}")

    # Update Full Log
    current_full = int(full_log.index("end-1c").split(".")[0])
    if len(engine.SYSTEM_LOGS) > current_full:
        full_log.configure(state="normal")
        new_lines = engine.SYSTEM_LOGS[current_full:]
        for line in new_lines:
            full_log.insert(tk.END, line + "\n")
        full_log.see(tk.END)
        full_log.configure(state="disabled")

    # Active chart — load chart for selected position
    sel = pos_tree.selection()
    if not sel:
        children = pos_tree.get_children()
        if children:
            pos_tree.selection_set(children[0])
            sel = pos_tree.selection()

    if sel:
        vals = pos_tree.item(sel[0])['values']
        if vals:
            mkt_name = str(vals[0])
            outcome  = str(vals[1])
            matched_pos = None
            for (c, o), p in engine.open_positions.items():
                if p['title'][:48] in mkt_name or mkt_name in p['title'][:48]:
                    if p['outcome'] == outcome:
                        matched_pos = p
                        break
            if matched_pos:
                history = matched_pos.get("price_history", [])
                pos_graph.load(history, matched_pos['title'], matched_pos['entry_price'])

    root.after(1000, ui_refresh)


# ═══════════════════════════════════════════════════════════════════════════════
#  BOOT
# ═══════════════════════════════════════════════════════════════════════════════
def on_boot_complete():
    engine.start(
        log_callback      = on_log_cb,
        position_open_cb  = on_position_open_cb,
        position_close_cb = on_position_close_cb,
        cycle_cb          = on_cycle_complete_cb,
    )
    if HAS_AI:
        try:
            ai_module.AIPanel(ai_frame, engine_module=engine)
        except Exception as e:
            engine._log(f"AI panel failed to load: {e}", "WARN")
    else:
        tk.Label(ai_frame,
                 text="🤖 AI Sidekick\n\nInstall titan_ai.py\nto enable.",
                 fg="#334455", bg="#080810", font=mono, justify="center"
                 ).pack(expand=True)

    root.after(1000, ui_refresh)
    status_var.set("🟢 LIVE — V7 Active · Prices Fixed · Sells Active")

try:
    from PIL import ImageGrab
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

def handle_telegram_message(msg_text):
    text = msg_text.strip().lower()
    if text == "pl":
        if not HAS_TELEGRAM:
            return
        if not HAS_PIL:
            telegram_notifier.notify_error("Pillow not installed. Cannot take screenshot.")
            return

        def _do_screenshot():
            try:
                was_iconic = root.state() == 'iconic'
                if was_iconic:
                    root.deiconify()
                root.lift()
                root.attributes('-topmost', True)
                nb.select(tab_pnl)
                
                def capture_wallet(idx):
                    if idx >= 10:
                        root.attributes('-topmost', False)
                        if was_iconic:
                            root.iconify()
                        return
                    
                    _switch_wallet(idx)
                    root.update()
                    # take screenshot inside after so renderer runs
                    def _grab():
                        try:
                            x = root.winfo_rootx()
                            y = root.winfo_rooty()
                            w = root.winfo_width()
                            h = root.winfo_height()
                            img = ImageGrab.grab(bbox=(x, y, x+w, y+h))
                            
                            from io import BytesIO
                            bio = BytesIO()
                            img.save(bio, format='PNG')
                            bio.seek(0)
                            
                            strat_name = WALLET_NAMES.get(idx, 'Unknown')
                            caption = f"📈 *Wallet {idx} P&L*\nStrategy: {strat_name}"
                            telegram_notifier.send_photo(bio, caption=caption)
                        except Exception as e:
                            telegram_notifier.notify_error(f"Screenshot error idx {idx}: {e}")
                        
                        root.after(300, lambda: capture_wallet(idx + 1))
                        
                    root.after(300, _grab)

                capture_wallet(0)
            except Exception as e:
                telegram_notifier.notify_error(f"Master screenshot error: {e}")
        
        # tkinter GUI operations must be properly queued
        root.after(0, _do_screenshot)

if HAS_TELEGRAM and hasattr(telegram_notifier, 'start_polling'):
    telegram_notifier.start_polling(handle_telegram_message)

show_loading_screen(root, on_boot_complete)
root.mainloop()