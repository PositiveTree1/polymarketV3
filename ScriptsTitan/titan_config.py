"""
TITAN — Configuration loader. Single-wallet edition.
All settings live in titan_config.json.

v10: Added multi-strategy config blocks (strategy_recent_form, strategy_drift_discount,
     strategy_open_book, strategy_consensus_basket) and ACTIVE_STRATEGIES list.
"""

import json, os

_CONFIG_DIR  = os.path.dirname(__file__)
_CONFIG_FILE = os.path.join(_CONFIG_DIR, "titan_config.json")

def get_config_file():
    return _CONFIG_FILE

# ── Explicit declarations (overwritten at runtime by reload()) ────────────────
BANKROLL_START: float = 0;  MIN_BET: float = 0;  MAX_BET_ABS: float = 0
MAX_BET_PCT: float = 0;     KELLY_FRACTION: float = 0
TAKER_FEE_RATE: float = 0;  ROUND_TRIP_FEE: float = 0
MIN_TRADE_CASH: float = 0;  MAX_TRADES_FETCH: int = 0
HOT_HOURS: float = 0;       WARM_HOURS: float = 0;  CYCLE_SECONDS: int = 0
HFT_POLL_LIMIT: int = 0;    HFT_MIN_TRADES_PER_HOUR: int = 0
HFT_MIRROR_DELAY_MAX_SECONDS: int = 0;  HFT_MIN_CASH_PER_TRADE: float = 0
HFT_SPIKE_MIN_ABS_CASH: float = 700; HFT_SPIKE_MULTIPLIER_LOW: float = 30; HFT_SPIKE_MULTIPLIER_HIGH: float = 40
HFT_MAX_DRIFT: float = 0.04; HFT_MIN_DRIFT: float = -0.08; HFT_MAX_ENTRY_SLIPPAGE: float = 0.03
ELITE_POLL_LIMIT: int = 0;  ELITE_POLL_MIN_CASH: float = 0
ELITE_TRADE_MIN_FRACTION: float = 0
MAX_SIGNAL_AGE_H: float = 0;  MIN_SCORE: float = 0;  STRONG_SCORE: float = 0
ALERT_SCORE: float = 0;  MIN_CONFLUENCE: int = 0
MAX_DRIFT: float = 0;  MIN_DRIFT: float = 0;  MAX_ENTRY_SLIPPAGE: float = 0
STALE_LOSER_AGE_H: float = 0;  STALE_LOSER_DRIFT: float = 0
MIN_LIQUIDITY: float = 0;  MIN_VOLUME: float = 0;  MIN_HOURS_LEFT: float = 0
MIN_WIN_RATE_WATCH: float = 0;  MIN_WIN_RATE_VER: float = 0
MIN_RESOLVED_BETS: int = 0;  MIN_PNL: float = 0
WILSON_MIN_WATCH: float = 0;  WILSON_MIN_VER: float = 0
MIN_AVG_PROFIT_PER_TRADE: float = 0;  MIN_AVG_BET_SIZE: float = 0
ELITE_MIN_PNL: float = 0;  ELITE_MIN_PORT: float = 0
ELITE_MIN_SCORE: float = 0;  ELITE_MIN_RESOLVED: int = 0
LARGE_TRADE: float = 0;  MASSIVE_TRADE: float = 0
MAX_OPEN_POSITIONS: int = 0;  MAX_POSITIONS_PER_EVENT: int = 0
MAX_POSITIONS_PER_WHALE: int = 0;  MAX_WATCHLIST_SIZE: int = 0
PROFIT_TARGET_PCT: float = 0;  WHALE_EXIT_SELL: bool = True
STOP_LOSS_ENABLED: bool = True;  STOP_LOSS_PCT: float = -0.30
MIN_HOLD_MINUTES: float = 0;  EXIT_COOLDOWN_SECONDS: int = 0
USE_PROPORTIONAL_SIZING: bool = False;  PROPORTIONAL_WEIGHT: float = 0
DISCOVERY_INTERVAL_CYCLES: int = 0
WALLET_TTL: int = 0;  MARKET_TTL: int = 0;  ACTIVITY_LIMIT: int = 0
STRATEGY_MODE: str = "multi_strategy";  TRADEABLE_TIERS_LIST: list = ["CONVICTION", "ALERT"]
ALLOWED_MARKET_TYPES: list = ["POLITICS", "EVENT"]
MIN_ELITE_CONFLUENCE: int = 2;  BLOCK_SPORTS: bool = True
SPORTS_BOT_MIN_TPH: int = 100
# v10: Price zone gates — only trade in the meaningful uncertainty zone
MIN_ENTRY_PRICE: float = 0.20;  MAX_ENTRY_PRICE: float = 0.72
IDEAL_PRICE_MIN: float = 0.25;  IDEAL_PRICE_MAX: float = 0.65
PROFIT_TARGET_ENABLED: bool = True
VIP_WALLETS: list = [];  PRIORITY_WALLETS: list = [];  SEED_WATCHLIST: list = []
DATA_API: str = "";  GAMMA_API: str = "";  HEADERS: dict = {}
STATE_FILE: str = "";  WHALE_FILE: str = ""

# v10: Multi-strategy configuration dicts
ACTIVE_STRATEGIES: list = ["recent_form", "drift_discount", "consensus_basket"]
strategy_recent_form: dict = {}
strategy_drift_discount: dict = {}
strategy_open_book: dict = {}
strategy_consensus_basket: dict = {}


def _load_json():
    if not os.path.exists(_CONFIG_FILE):
        return {}
    with open(_CONFIG_FILE, "r", encoding="utf-8") as f:
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
        # v10: strategy config blocks — store as flat dict directly
        if group_key.startswith("strategy_"):
            strat_flat = {}
            for key, val in group_val.items():
                if not key.startswith("_"):
                    strat_flat[key] = val
            flat[group_key] = strat_flat
            continue
        for key, entry in group_val.items():
            if key.startswith("_"):
                continue
            if isinstance(entry, dict) and "value" in entry:
                flat[key] = entry["value"]
            elif not isinstance(entry, dict):
                flat[key] = entry
    return flat


def reload():
    raw  = _load_json()
    flat = _extract(raw)
    g    = globals()

    g["STRATEGY_MODE"]         = flat.get("STRATEGY_MODE", "multi_strategy")
    g["TRADEABLE_TIERS_LIST"]  = flat.get("TRADEABLE_TIERS_LIST", ["CONVICTION", "ALERT"])
    g["ALLOWED_MARKET_TYPES"]  = flat.get("ALLOWED_MARKET_TYPES", ["POLITICS", "EVENT"])
    g["MIN_ELITE_CONFLUENCE"]  = int(flat.get("MIN_ELITE_CONFLUENCE", 2))
    g["BLOCK_SPORTS"]          = bool(flat.get("BLOCK_SPORTS", True))
    g["SPORTS_BOT_MIN_TPH"]    = int(flat.get("SPORTS_BOT_MIN_TPH", 100))
    # v10: Price zone gates
    g["MIN_ENTRY_PRICE"]       = float(flat.get("MIN_ENTRY_PRICE", 0.20))
    g["MAX_ENTRY_PRICE"]       = float(flat.get("MAX_ENTRY_PRICE", 0.72))
    g["IDEAL_PRICE_MIN"]       = float(flat.get("IDEAL_PRICE_MIN", 0.25))
    g["IDEAL_PRICE_MAX"]       = float(flat.get("IDEAL_PRICE_MAX", 0.65))
    g["PROFIT_TARGET_ENABLED"] = bool(flat.get("PROFIT_TARGET_ENABLED", True))

    # v10: Active strategies and per-strategy config blocks
    g["ACTIVE_STRATEGIES"] = flat.get("ACTIVE_STRATEGIES", ["recent_form", "drift_discount", "consensus_basket"])

    _default_rf = {
        "enabled": True, "max_tph": 20, "min_pnl_30d": 0, "min_pnl_7d": -50,
        "max_signal_age_h": 0.75, "min_score": 42, "price_min": 0.18, "price_max": 0.78,
        "max_positions": 4, "bet_multiplier_base": 1.0, "stop_loss_pct": None,
    }
    _default_dd = {
        "enabled": True, "min_discount_pct": 0.04, "max_discount_pct": 0.12,
        "max_signal_age_h": 6.0, "price_min": 0.20, "price_max": 0.72,
        "max_positions": 3, "require_still_holding_check": True, "stop_loss_pct": None,
    }
    _default_ob = {
        "enabled": False, "min_consensus": 3, "wallets_per_cycle": 2,
        "price_min": 0.25, "price_max": 0.70, "min_hours_left": 6.0,
        "max_positions": 2, "stop_loss_pct": None,
    }
    _default_cb = {
        "enabled": True, "min_elite_confluence": 1, "max_signal_age_h": 0.5,
        "price_min": 0.20, "price_max": 0.72, "min_score": 50,
        "max_positions": 5, "max_bet_abs": 1.20, "stop_loss_pct": -0.35,
    }

    g["strategy_recent_form"]     = {**_default_rf,  **flat.get("strategy_recent_form", {})}
    g["strategy_drift_discount"]  = {**_default_dd,  **flat.get("strategy_drift_discount", {})}
    g["strategy_open_book"]       = {**_default_ob,  **flat.get("strategy_open_book", {})}
    g["strategy_consensus_basket"]= {**_default_cb,  **flat.get("strategy_consensus_basket", {})}

    for key, val in flat.items():
        if key not in ("vip_wallets", "priority_wallets", "seed_watchlist") and not key.startswith("strategy_"):
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