"""
TITAN — Persistence layer.
Saves: titan_state.json and titan_whales.json
"""

import os
import json
import threading
from datetime import datetime
import titan_state as S
from titan_config import STATE_FILE, WHALE_FILE, BANKROLL_START, SEED_WATCHLIST


def _get_state_file(idx): return STATE_FILE.replace(".json", f"_{idx}.json")
def _get_whale_file(idx): return WHALE_FILE.replace(".json", f"_{idx}.json")

def save_state():
    for i in range(10):
        try:
            S._local.engine_idx = i
            state = {
                "bankroll":           S.paper_bankroll,
                "session_pnl":        S.session_pnl,
                "trade_history":      S.trade_history[-1000:],
                "open_positions":     {f"{k[0]}|||{k[1]}": v for k, v in S.open_positions.items()},
                "active_market_cids": list(S.active_market_cids),
                "cooldown_cids":      S.cooldown_cids,
                "position_whale_map": {k: list(v) for k, v in S.position_whale_map.items()},
                "watchlist":          list(S.watchlist),
                "saved_at":           datetime.now().isoformat(),
            }
            with open(_get_state_file(i), "w") as f:
                json.dump(state, f, indent=2)
        except Exception as e:
            S._log(f"⚠ Save failed for w{i}: {e}", "WARN")
    S._local.engine_idx = S.active_idx


def save_whale_roster():
    for i in range(10):
        try:
            S._local.engine_idx = i
            saveable = {
                addr: {k: v for k, v in profile.items() if k != "ts"}
                for addr, profile in S.wallet_cache.items()
                if profile.get("verified") or profile.get("watchable")
            }
            with open(_get_whale_file(i), "w") as f:
                json.dump(saveable, f, indent=2)
        except Exception as e:
            S._log(f"⚠ Whale save failed w{i}: {e}", "WARN")
    S._local.engine_idx = S.active_idx


def save_whale_roster_async():
    threading.Thread(target=save_whale_roster, daemon=True).start()


def load_state():
    for i in range(10):
        S._local.engine_idx = i
        _load_trading_state(i)
        _load_whale_roster(i)
    S._local.engine_idx = S.active_idx


def _load_trading_state(idx):
    fname = _get_state_file(idx)
    # Legacy fallback for loading
    if not os.path.exists(fname):
        if idx == 0 and os.path.exists(STATE_FILE):
            fname = STATE_FILE
        else:
            S._log(f"📂 No saved state for w{idx} — fresh start", "INFO")
            return
    try:
        with open(fname) as f:
            state = json.load(f)

        S.paper_bankroll     = float(state.get("bankroll", BANKROLL_START))
        S.session_pnl        = float(state.get("session_pnl", 0.0))
        S.trade_history      = state.get("trade_history", [])
        S.active_market_cids = set(state.get("active_market_cids", []))
        S.cooldown_cids      = state.get("cooldown_cids", {})
        S.position_whale_map = {k: set(v) for k, v in state.get("position_whale_map", {}).items()}

        saved_wl = state.get("watchlist", [])
        if saved_wl:
            S.watchlist.update(saved_wl)

        raw = state.get("open_positions", {})
        S.open_positions = {}
        for composite_key, pos in raw.items():
            parts = composite_key.split("|||", 1)
            if len(parts) == 2:
                S.open_positions[(parts[0], parts[1])] = pos

        for key, pos in S.open_positions.items():
            cid = pos.get("cid", key[0])
            S.active_market_cids.add(cid)

        S._log(
            f"📂 State loaded: bankroll=${S.paper_bankroll:.2f} | "
            f"{len(S.trade_history)} trades | {len(S.open_positions)} open | "
            f"{len(S.cooldown_cids)} cooldowns | {len(S.watchlist)} watchlist",
            "INFO"
        )
    except Exception as e:
        S._log(f"⚠ State load failed ({e}) — fresh start", "WARN")


def _load_whale_roster(idx):
    fname = _get_whale_file(idx)
    if not os.path.exists(fname):
        if idx == 0 and os.path.exists(WHALE_FILE):
            fname = WHALE_FILE
        else:
            return
    try:
        with open(fname) as f:
            saved = json.load(f)
        loaded = 0
        for addr, profile in saved.items():
            addr = addr.lower()
            if addr not in S.wallet_cache:
                profile["ts"] = 0
                S.wallet_cache[addr] = profile
                if profile.get("watchable"):
                    S.watchlist.add(addr)
                loaded += 1
        S._log(f"📂 Whale roster: {loaded} loaded ({len(S.wallet_cache)} total)", "INFO")
    except Exception as e:
        S._log(f"⚠ Whale load failed ({e})", "WARN")