"""
TITAN — Configuration loader.
All settings live in titan_config.json.
Edit titan_config.json and click RELOAD CONFIG in the UI.
"""

import json
import os

_CONFIG_DIR = os.path.dirname(__file__)

def get_config_file():
    import titan_state as S
    idx = getattr(S, "active_idx", 0)
    return os.path.join(_CONFIG_DIR, f"titan_config_{idx}.json")

# Fallback for legacy scripts
_CONFIG_FILE = os.path.join(_CONFIG_DIR, "titan_config_0.json")

# ── Explicit Declarations (for IDE/Linter) ────────────────────────────────────
# These are overwritten at runtime by reload()
BANKROLL_START: float = 0
MIN_BET: float = 0
MAX_BET_ABS: float = 0
MAX_BET_PCT: float = 0
KELLY_FRACTION: float = 0
TAKER_FEE_RATE: float = 0
ROUND_TRIP_FEE: float = 0

MIN_TRADE_CASH: float = 0
MAX_TRADES_FETCH: int = 0
HOT_HOURS: float = 0
WARM_HOURS: float = 0
CYCLE_SECONDS: int = 0

HFT_POLL_LIMIT: int = 0
HFT_MIN_TRADES_PER_HOUR: int = 0
HFT_MIRROR_DELAY_MAX_SECONDS: int = 0
HFT_MIN_CASH_PER_TRADE: float = 0

ELITE_POLL_LIMIT: int = 0
ELITE_POLL_MIN_CASH: float = 0
ELITE_TRADE_MIN_FRACTION: float = 0

MAX_SIGNAL_AGE_H: float = 0
MIN_SCORE: float = 0
STRONG_SCORE: float = 0
ALERT_SCORE: float = 0
MIN_CONFLUENCE: int = 0

MAX_DRIFT: float = 0
MIN_DRIFT: float = 0
MAX_ENTRY_SLIPPAGE: float = 0
STALE_LOSER_AGE_H: float = 0
STALE_LOSER_DRIFT: float = 0

MIN_LIQUIDITY: float = 0
MIN_VOLUME: float = 0
MIN_HOURS_LEFT: float = 0

MIN_WIN_RATE_WATCH: float = 0
MIN_WIN_RATE_VER: float = 0
MIN_RESOLVED_BETS: int = 0
MIN_PNL: float = 0
WILSON_MIN_WATCH: float = 0
WILSON_MIN_VER: float = 0
MIN_AVG_PROFIT_PER_TRADE: float = 0
MIN_AVG_BET_SIZE: float = 0

ELITE_MIN_PNL: float = 0
ELITE_MIN_PORT: float = 0
ELITE_MIN_SCORE: float = 0
ELITE_MIN_RESOLVED: int = 0

LARGE_TRADE: float = 0
MASSIVE_TRADE: float = 0

MAX_OPEN_POSITIONS: int = 0
MAX_POSITIONS_PER_EVENT: int = 0
MAX_POSITIONS_PER_WHALE: int = 0
MAX_WATCHLIST_SIZE: int = 0
PROFIT_TARGET_PCT: float = 0
WHALE_EXIT_SELL: bool = True
STOP_LOSS_ENABLED: bool = True
STOP_LOSS_PCT: float = 0

MIN_HOLD_MINUTES: float = 0
EXIT_COOLDOWN_SECONDS: int = 0

USE_PROPORTIONAL_SIZING: bool = True
PROPORTIONAL_WEIGHT: float = 0

DISCOVERY_INTERVAL_CYCLES: int = 0

WALLET_TTL: int = 0
MARKET_TTL: int = 0
ACTIVITY_LIMIT: int = 0

VIP_WALLETS: list[str] = []
PRIORITY_WALLETS: list[str] = []
SEED_WATCHLIST: list[str] = []

DATA_API: str = ""
GAMMA_API: str = ""
HEADERS: dict = {}
STATE_FILE: str = ""
WHALE_FILE: str = ""
# ─────────────────────────────────────────────────────────────────────────────



def _load_json():
    import shutil
    fpath = get_config_file()
    if not os.path.exists(fpath):
        legacy = os.path.join(_CONFIG_DIR, "titan_config.json")
        if os.path.exists(legacy):
            shutil.copy2(legacy, fpath)
        else:
            return {} # Fallback
    with open(fpath, "r", encoding="utf-8") as f:
        return json.load(f)


def _extract(cfg):
    flat = {}
    for group_key, group_val in cfg.items():
        if group_key.startswith("_"):
            continue
        if not isinstance(group_val, dict):
            continue
        if "wallets" in group_val and isinstance(group_val["wallets"], list):
            flat[group_key] = group_val["wallets"]
            continue
        for key, entry in group_val.items():
            if key.startswith("_"):
                continue
            if isinstance(entry, dict) and "value" in entry:
                flat[key] = entry["value"]
    return flat


def reload():
    raw  = _load_json()
    flat = _extract(raw)
    g    = globals()

    for key, val in flat.items():
        if key not in ("vip_wallets", "priority_wallets", "seed_watchlist"):
            g[key] = val

    g["ROUND_TRIP_FEE"] = g.get("TAKER_FEE_RATE", 0.0) * 2

    def _addrs(lst):
        out = []
        for item in lst:
            if isinstance(item, dict):
                out.append(item["address"].lower())
            else:
                out.append(str(item).lower())
        return out

    vip_raw  = flat.get("vip_wallets", [])
    pri_raw  = flat.get("priority_wallets", [])
    seed_raw = flat.get("seed_watchlist", [])

    g["VIP_WALLETS"]      = _addrs(vip_raw)
    g["PRIORITY_WALLETS"] = _addrs(pri_raw)
    g["SEED_WATCHLIST"]   = list(dict.fromkeys(
        [a.lower() for a in (_addrs(seed_raw) + g["VIP_WALLETS"] + g["PRIORITY_WALLETS"])]
    ))

    # ── API endpoints (not in JSON) ───────────────────────────────────────────
    g["DATA_API"]  = "https://data-api.polymarket.com"
    g["GAMMA_API"] = "https://gamma-api.polymarket.com"
    g["HEADERS"]   = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept":     "application/json",
        "Origin":     "https://polymarket.com",
        "Referer":    "https://polymarket.com/",
    }
    g["STATE_FILE"] = "titan_state.json"
    g["WHALE_FILE"] = "titan_whales.json"


reload()