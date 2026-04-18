"""
TITAN — Global mutable state, logging, and HTTP helper.
"""

import time
import requests
import threading
import os
from datetime import datetime
from titan_config import *

_local = threading.local()

# ── Logging Configuration ─────────────────────────────────────────────────────
# We define these early so functions can use them
LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "Logs")
LOG_FILE = os.path.join(LOG_DIR, "titan.log")

if not os.path.exists(LOG_DIR):
    os.makedirs(LOG_DIR, exist_ok=True)

def load_logs_from_disk():
    """Load the last 2000 lines from the log file into the system logs."""
    if not os.path.exists(LOG_FILE):
        return []
    try:
        with open(LOG_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
            return [l.strip() for l in lines[-2000:]]
    except Exception as e:
        print(f"Failed to load logs from disk: {e}")
        return []

# ── Wallet Environment ────────────────────────────────────────────────────────
class WalletEnv:
    def __init__(self, index=0):
        self.index = index
        self.wallet_cache = {}   
        self.logged_signals = {}
        
        # Load historical logs from disk for this wallet
        disk_logs = load_logs_from_disk()
        prefix = f"[W{index+1}]"
        self.SYSTEM_LOGS = [l for l in disk_logs if prefix in l]
        
        self.watchlist = set(w.lower() for w in SEED_WATCHLIST)
        self.cycle_count = 0
        self.active_signal_cids = {}
        self.LAST_SIGNALS = []
        self.LAST_REJECTS = []
        self.WHALE_EXIT_HISTORY = []
        
        self.paper_bankroll = BANKROLL_START
        self.open_positions = {}   
        self.trade_history = []
        self.session_pnl = 0.0
        
        self.active_market_cids = set()
        self.cooldown_cids = {}
        self.position_whale_map = {}   
        self.equity_history = []

wallets = [WalletEnv(i) for i in range(10)]
active_idx = 0
market_cache = {}

def env() -> WalletEnv:
    idx = getattr(_local, "engine_idx", active_idx)
    return wallets[idx]

def __getattr__(name):
    e = env()
    if hasattr(e, name):
        return getattr(e, name)
    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")



# UI callbacks
on_log            = None
on_position_open  = None
on_position_close = None
on_cycle_complete = None


# ── Logging ───────────────────────────────────────────────────────────────────

def _log(msg, level="INFO"):
    ts   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    e = env()
    prefix = f"[W{e.index+1}]"
    line = f"[{ts}] {prefix} [{level:5}] {msg}"
    
    e.SYSTEM_LOGS.append(line)
    if len(e.SYSTEM_LOGS) > 5000:
        del e.SYSTEM_LOGS[:500]
        
    # Write to file
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as ex:
        print(f"Failed to write to log file: {ex}")

    if on_log:
        on_log(msg, level, e.index)
    else:
        print(line)


# ── HTTP ──────────────────────────────────────────────────────────────────────
def safe_get(url, params=None, retries=3, timeout=12):
    """Resilient GET with exponential backoff on 429."""
    for i in range(retries):
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=timeout)
            if r.status_code == 429:
                wait = 2 ** (i + 1)
                _log(f"⚠ Rate limited — sleeping {wait}s", "WARN")
                time.sleep(wait)
                continue
            if r.status_code == 200:
                return r.json()
            _log(f"⚠ HTTP {r.status_code} from {url[:60]}", "DIAG")
            return None
        except requests.exceptions.Timeout:
            time.sleep(1.5)
        except requests.exceptions.ConnectionError:
            time.sleep(2)
        except Exception as e:
            _log(f"⚠ Request error: {e}", "DIAG")
            time.sleep(0.5)
    return None


# ── Cash extraction ────────────────────────────────────────────────────────────
def extract_cash(t: dict) -> float:
    """
    Extract USDC value from a trade dict.
    Tries all known field names before falling back to size * price.
    """
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