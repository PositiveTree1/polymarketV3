"""
TITAN — Global mutable state, logging, and HTTP helper.
Single-wallet edition: one WalletEnv, one config, zero noise.
"""

import time
import requests
import threading
import os
from collections import deque
from datetime import datetime
from typing import TYPE_CHECKING, TypedDict
import titan_config as C
from titan_config import *

if TYPE_CHECKING:
    from titan_wallet import Wallet
    from titan_market import Market
    from titan_position import Position
    from titan_signals import Signal

from titan_markets import MarketCache, market_cache

_local = threading.local()

# ── Logging ───────────────────────────────────────────────────────────────────
LOG_DIR         = os.path.join(os.path.dirname(os.path.dirname(__file__)), "Logs")
LOG_FILE        = os.path.join(LOG_DIR, "titan.log")
VERBOSE_LOG_FILE = os.path.join(LOG_DIR, "titan_verbose.log")
SERVER_LOG_FILE = os.path.join(LOG_DIR, "titan_server.log")
os.makedirs(LOG_DIR, exist_ok=True)

def load_logs_from_disk() -> list[str]:
    if not os.path.exists(LOG_FILE):
        return []
    try:
        with open(LOG_FILE, "r", encoding="utf-8") as f:
            return [l.strip() for l in f.readlines()[-2000:]]
    except Exception:
        return []


# ── Single Wallet Environment ─────────────────────────────────────────────────
class TradeStats:
    """Running totals updated on every trade — no iteration over history needed."""
    def __init__(self):
        self.sell_count:  int   = 0
        self.win_count:   int   = 0
        self.loss_count:  int   = 0
        self.sum_pnl:     float = 0.0
        self.sum_wins:    float = 0.0
        self.sum_losses:  float = 0.0  # absolute value
        self.best:        float = 0.0
        self.worst:       float = 0.0

    @property
    def win_rate(self) -> float:
        return self.win_count / self.sell_count if self.sell_count else 0.0

    @property
    def avg_win(self) -> float:
        return self.sum_wins / self.win_count if self.win_count else 0.0

    @property
    def avg_loss(self) -> float:
        return self.sum_losses / self.loss_count if self.loss_count else 0.0

    @property
    def expectancy(self) -> float:
        return (self.win_rate * self.avg_win) - ((1 - self.win_rate) * self.avg_loss)

    def record_sell(self, pnl: float) -> None:
        self.sell_count += 1
        self.sum_pnl    += pnl
        if pnl >= 0:
            self.win_count += 1
            self.sum_wins  += pnl
            self.best       = max(self.best, pnl)
        else:
            self.loss_count += 1
            self.sum_losses += abs(pnl)
            self.worst       = min(self.worst, pnl)


class WalletEnv:
    def __init__(self):
        self.index:               int                                    = 0
        self.wallet_cache:        dict[str, "Wallet"]                   = {}
        self.SYSTEM_LOGS:         list[str]                             = load_logs_from_disk()
        self.logged_signals:      dict[str, float]                      = {}
        self.cycle_count:         int                                    = 0
        self.active_signal_cids:  dict[str, set[str]]                   = {}
        self.LAST_SIGNALS:        list["Signal"]                        = []
        self.feed_responded:      bool                                   = False
        self.LAST_REJECTS:        list[str]                             = []
        self.WHALE_EXIT_HISTORY:  list[str]                             = []
        self.paper_bankroll:      float                                  = BANKROLL_START
        self.open_positions:      dict[tuple[str, str], "Position"]     = {}
        self.trade_stats:         TradeStats                             = TradeStats()
        self.session_pnl:         float                                  = 0.0
        self.active_market_cids:  set[str]                              = set()
        self.cooldown_cids:       dict[str, float]                      = {}
        self.position_wallet_map:  dict[str, set[str]]                   = {}
        self.signal_first_seen_by_asset: dict[str, float]               = {}
        self.equity_history:      list[tuple[float, float]]             = []

# The one wallet
_wallet = WalletEnv()


# Shared caches
_shared_wallet_cache: dict[str, "Wallet"] = {}
_wallet.wallet_cache = _shared_wallet_cache
_http_trace_lock = threading.Lock()
_recent_http_traces = deque(maxlen=400)


# Compatibility shim: engine code that calls S.wallets[i] or S.env()
wallets    = [_wallet]   # single-element list — legacy code still works
active_idx = 0

def env() -> WalletEnv:
    return _wallet


def get_watchlist() -> list[str]:
    """Return addresses currently marked watchable=True in wallet_cache."""
    return [w for w, p in _shared_wallet_cache.items() if p.watchable]

def __getattr__(name):
    if hasattr(_wallet, name):
        return getattr(_wallet, name)
    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")


def _build_http_caller_chain(depth: int = 5) -> str:
    import inspect as _inspect
    _skip = {"safe_get", "_gamma_get", "<module>"}
    chain = []
    for frame_info in _inspect.stack()[1:12]:
        fn  = frame_info.function
        mod = frame_info.filename.replace("\\", "/").split("/")[-1]
        if fn in _skip:
            continue
        chain.append(f"{mod}::{fn}")
        if len(chain) >= depth:
            break
    return " → ".join(chain) if chain else "?"


def _store_http_trace(url: str, params: dict | None, status_code: int | None,
                      body: str, caller: str, ok: bool) -> None:
    entry = {
        "ts": time.time(),
        "url": url,
        "params": dict(params or {}),
        "status_code": status_code,
        "ok": ok,
        "caller": caller,
        "body": body,
    }
    with _http_trace_lock:
        _recent_http_traces.append(entry)


def get_recent_http_traces(*, since_ts: float = 0.0, limit: int = 40,
                           filters: list[str] | None = None) -> list[dict]:
    filters = [str(f).lower() for f in (filters or []) if f]
    with _http_trace_lock:
        traces = list(_recent_http_traces)
    out = []
    for entry in reversed(traces):
        if entry.get("ts", 0) < since_ts:
            continue
        if filters:
            hay = " ".join([
                str(entry.get("url", "")),
                str(entry.get("caller", "")),
                str(entry.get("params", "")),
                str(entry.get("body", ""))[:1000],
            ]).lower()
            if not any(f in hay for f in filters):
                continue
        out.append({
            "ts": entry.get("ts"),
            "url": entry.get("url"),
            "params": entry.get("params"),
            "status_code": entry.get("status_code"),
            "ok": entry.get("ok"),
            "caller": entry.get("caller"),
            "body": entry.get("body"),
        })
        if len(out) >= limit:
            break
    out.reverse()
    return out


# UI callbacks
on_log            = None
on_position_open  = None
on_position_close = None
on_cycle_complete = None
on_heartbeat      = None


# ── Logging ───────────────────────────────────────────────────────────────────
def _log(msg: str, level: str = "INFO") -> None:
    ts   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] [{level:5}] {msg}"
    if level == "VERB":
        # Verbose HTTP traffic — separate file only, never pollutes main log or UI
        try:
            with open(VERBOSE_LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass
        return
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


def log_important(msg: str) -> None:
    """Print to stdout AND write to titan_server.log. Use for startup/shutdown lines."""
    ts   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        with open(SERVER_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    _log(msg, "INFO")


def safe_get(url: str, params: dict | None = None, retries: int = 3, timeout: int = 12, quiet: bool = False) -> list | dict | None:
    for i in range(retries):
        try:
            r = requests.get(url, params=params, headers=C.HEADERS, timeout=timeout)
            if r.status_code == 429:
                wait = 2 ** (i + 1)
                if not quiet:
                    _log(f"⚠ Rate limited â€” sleeping {wait}s", "WARN")
                time.sleep(wait)
                continue

            caller = _build_http_caller_chain(depth=5)
            try:
                body = r.text.replace("\n", " ")
            except Exception:
                body = ""

            if r.status_code == 200:
                data = r.json()
                _store_http_trace(url, params, 200, body[:4000], caller, True)
                if VERBOSE_HTTP:
                    _pstr = ", ".join(f"{k}={v}" for k, v in (params or {}).items())
                    _count = f" | items: {len(data)}" if isinstance(data, list) else ""
                    _body = body[:300]
                    _log(
                        f"⚠ HTTP 200 {url} | {caller}"
                        f" | params: {_pstr}{_count}"
                        f" | body: {_body}",
                        "VERB"
                    )
                return data

            _store_http_trace(url, params, r.status_code, body[:4000], caller, False)
            if not quiet:
                param_str = ""
                if params:
                    parts = [f"{k}={v}" for k, v in params.items()]
                    param_str = " | params: " + ", ".join(parts)
                _log(
                    f"⚠ HTTP {r.status_code} from {url}"
                    f" | caller: {caller}{param_str}"
                    f" | body: {body}",
                    "DIAG"
                )
            return None
        except requests.exceptions.Timeout:
            if not quiet:
                caller = _build_http_caller_chain(depth=5)
                param_str = ("?" + "&".join(f"{k}={v}" for k, v in params.items())) if params else ""
                _log(f"⚠ Timeout (attempt {i+1}/{retries}): {url}{param_str} | caller: {caller}", "DIAG")
            time.sleep(1.5)
        except requests.exceptions.ConnectionError as e:
            if not quiet:
                caller = _build_http_caller_chain(depth=5)
                param_str = ("?" + "&".join(f"{k}={v}" for k, v in params.items())) if params else ""
                _log(f"⚠ ConnectionError (attempt {i+1}/{retries}): {url}{param_str} | {e} | caller: {caller}", "DIAG")
            time.sleep(2)
        except Exception as e:
            if not quiet:
                caller = _build_http_caller_chain(depth=5)
                if not url.startswith("http"):
                    _log(
                        f"⚠ Request error: invalid URL '{url}' — base URL not set "
                        f"(DATA_API is empty, config not loaded yet?) | caller: {caller} | error: {e}",
                        "DIAG"
                    )
                else:
                    param_str = ("?" + "&".join(f"{k}={v}" for k, v in (params or {}).items())) if params else ""
                    _log(f"⚠ Request error (attempt {i+1}/{retries}): {url}{param_str} | {type(e).__name__}: {e} | caller: {caller}", "DIAG")
            time.sleep(0.5)
    if not quiet:
        caller = _build_http_caller_chain(depth=5)
        param_str = ("?" + "&".join(f"{k}={v}" for k, v in (params or {}).items())) if params else ""
        _log(f"⚠ All {retries} attempts failed: {url}{param_str} | caller: {caller}", "ERR")
    return None

# ── Cash extraction ───────────────────────────────────────────────────────────
def extract_cash(t: dict[str, str | float | int | None]) -> float:
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
