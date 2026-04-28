"""
TITAN — Global mutable state, logging, and HTTP helper.
Single-wallet edition: one WalletEnv, one config, zero noise.
"""

import time
import requests
import threading
import os
from datetime import datetime
from titan_config import *

_local = threading.local()

# ── Logging ───────────────────────────────────────────────────────────────────
LOG_DIR  = os.path.join(os.path.dirname(os.path.dirname(__file__)), "Logs")
LOG_FILE = os.path.join(LOG_DIR, "titan.log")
os.makedirs(LOG_DIR, exist_ok=True)

def load_logs_from_disk():
    if not os.path.exists(LOG_FILE):
        return []
    try:
        with open(LOG_FILE, "r", encoding="utf-8") as f:
            return [l.strip() for l in f.readlines()[-2000:]]
    except Exception:
        return []


# ── Single Wallet Environment ─────────────────────────────────────────────────
class WalletEnv:
    def __init__(self):
        self.index        = 0
        self.wallet_cache = {}          # shared reference — set after init
        self.SYSTEM_LOGS  = load_logs_from_disk()
        self.logged_signals     = {}
        self.watchlist          = set(w.lower() for w in SEED_WATCHLIST)
        self.cycle_count        = 0
        self.active_signal_cids = {}
        self.LAST_SIGNALS       = []
        self.LAST_REJECTS       = []
        self.WHALE_EXIT_HISTORY = []
        self.paper_bankroll     = BANKROLL_START
        self.open_positions     = {}
        self.trade_history      = []
        self.session_pnl        = 0.0
        self.active_market_cids = set()
        self.cooldown_cids      = {}
        self.position_whale_map = {}
        self.equity_history     = []

# The one wallet
_wallet = WalletEnv()

# Shared caches
_shared_wallet_cache = {}
_wallet.wallet_cache = _shared_wallet_cache
market_cache = {}

# Compatibility shim: engine code that calls S.wallets[i] or S.env()
wallets    = [_wallet]   # single-element list — legacy code still works
active_idx = 0

def env() -> WalletEnv:
    return _wallet

def __getattr__(name):
    if hasattr(_wallet, name):
        return getattr(_wallet, name)
    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")


# UI callbacks
on_log            = None
on_position_open  = None
on_position_close = None
on_cycle_complete = None


# ── Logging ───────────────────────────────────────────────────────────────────
def _log(msg, level="INFO"):
    ts   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] [{level:5}] {msg}"
    _wallet.SYSTEM_LOGS.append(line)
    if len(_wallet.SYSTEM_LOGS) > 5000:
        del _wallet.SYSTEM_LOGS[:500]
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    if on_log:
        on_log(msg, level)
    else:
        print(line)


# ── HTTP ──────────────────────────────────────────────────────────────────────
def safe_get(url, params=None, retries=3, timeout=12, quiet=False):
    for i in range(retries):
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=timeout)
            if r.status_code == 429:
                wait = 2 ** (i + 1)
                if not quiet:
                    _log(f"⚠ Rate limited — sleeping {wait}s", "WARN")
                time.sleep(wait)
                continue
            if r.status_code == 200:
                return r.json()
            if not quiet:
                _log(f"⚠ HTTP {r.status_code} from {url[:60]}", "DIAG")
            return None
        except requests.exceptions.Timeout:
            time.sleep(1.5)
        except requests.exceptions.ConnectionError:
            time.sleep(2)
        except Exception as e:
            if not quiet:
                _log(f"⚠ Request error: {e}", "DIAG")
            time.sleep(0.5)
    return None


# ── Cash extraction ───────────────────────────────────────────────────────────
def extract_cash(t: dict) -> float:
    price = float(t.get("price") or 0)
    for field in ("usdcSize", "amount", "cashSize", "collateralAmount", "dollarSize"):
        v = t.get(field)
        if v is not None:
            val = float(v or 0)
            if val > 0:
                return val
    size = float(t.get("size") or 0)
    if size > 0 and 0.01 < price < 0.99:
        return round(size * price, 6)
    return size