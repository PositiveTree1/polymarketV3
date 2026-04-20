"""
TITAN — Persistence layer. Single-wallet edition.
Saves: titan_state.json and titan_whales.json
"""

import os, json, threading, time
from datetime import datetime
import titan_state as S
from titan_config import STATE_FILE, WHALE_FILE, BANKROLL_START, SEED_WATCHLIST


def save_state():
    try:
        env = S.env()
        state = {
            "bankroll":           env.paper_bankroll,
            "session_pnl":        env.session_pnl,
            "trade_history":      env.trade_history[-1000:],
            "open_positions":     {f"{k[0]}|||{k[1]}": v for k, v in env.open_positions.items()},
            "active_market_cids": list(env.active_market_cids),
            "cooldown_cids":      env.cooldown_cids,
            "position_whale_map": {k: list(v) for k, v in env.position_whale_map.items()},
            "watchlist":          list(env.watchlist),
            "equity_history":     env.equity_history[-2000:],
            "saved_at":           datetime.now().isoformat(),
        }
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        S._log(f"⚠ Save failed: {e}", "WARN")


def save_whale_roster():
    try:
        from titan_wallet import _whale_performance
        from titan_signals import get_known_hedge_wallets

        saveable = {
            addr: {k: v for k, v in profile.items() if k != "ts"}
            for addr, profile in S.env().wallet_cache.items()
            if profile.get("verified") or profile.get("watchable")
        }
        for addr in saveable:
            perf = _whale_performance.get(addr.lower())
            if perf:
                saveable[addr]["copy_performance"] = perf

        hedge = list(get_known_hedge_wallets())
        if hedge:
            saveable["__hedge_wallets__"] = {"hedge_set": hedge}

        with open(WHALE_FILE, "w") as f:
            json.dump(saveable, f, indent=2)
    except Exception as e:
        S._log(f"⚠ Whale save failed: {e}", "WARN")


def save_whale_roster_async():
    threading.Thread(target=save_whale_roster, daemon=True).start()


def load_state():
    _load_whale_roster()
    _load_trading_state()


def _load_trading_state():
    if not os.path.exists(STATE_FILE):
        S._log("📂 No saved state — fresh start", "INFO")
        return
    try:
        with open(STATE_FILE) as f:
            state = json.load(f)

        env = S.env()
        env.paper_bankroll     = float(state.get("bankroll", BANKROLL_START))
        if env.paper_bankroll <= 0:
            env.paper_bankroll = BANKROLL_START
        env.session_pnl        = float(state.get("session_pnl", 0.0))
        env.trade_history      = state.get("trade_history", [])
        env.active_market_cids = set(state.get("active_market_cids", []))
        env.cooldown_cids      = state.get("cooldown_cids", {})
        env.position_whale_map = {k: set(v) for k, v in state.get("position_whale_map", {}).items()}

        saved_wl = state.get("watchlist", [])
        if saved_wl:
            env.watchlist.update(saved_wl)

        raw = state.get("open_positions", {})
        env.open_positions = {}
        for composite_key, pos in raw.items():
            parts = composite_key.split("|||", 1)
            if len(parts) == 2:
                env.open_positions[(parts[0], parts[1])] = pos

        for key, pos in env.open_positions.items():
            cid = pos.get("cid", key[0])
            env.active_market_cids.add(cid)

        S._log(
            f"📂 State loaded: bankroll=${env.paper_bankroll:.2f} | "
            f"{len(env.trade_history)} trades | {len(env.open_positions)} open | "
            f"{len(env.cooldown_cids)} cooldowns | {len(env.watchlist)} watchlist",
            "INFO"
        )

        saved_equity = state.get("equity_history", [])
        if saved_equity and len(saved_equity) >= 2:
            env.equity_history = [tuple(p) for p in saved_equity]
            S._log(
                f"📈 Equity curve restored: {len(env.equity_history)} points "
                f"(${saved_equity[0][1]:.2f} → ${saved_equity[-1][1]:.2f})",
                "INFO"
            )
        else:
            _rebuild_equity_from_trades(env)

    except Exception as e:
        S._log(f"⚠ State load failed ({e}) — fresh start", "WARN")


def _rebuild_equity_from_trades(env):
    trades_with_br = [
        t for t in env.trade_history
        if t.get("bankroll") and t.get("ts")
    ]
    if not trades_with_br:
        env.equity_history = []
        return
    trades_with_br.sort(key=lambda t: t["ts"])
    points = [(trades_with_br[0]["ts"] - 1, float(BANKROLL_START))]
    for t in trades_with_br:
        points.append((float(t["ts"]), float(t["bankroll"])))
    points.append((time.time(), env.paper_bankroll))
    env.equity_history = points
    S._log(
        f"📈 Equity curve rebuilt from {len(trades_with_br)} trade records "
        f"(${points[0][1]:.2f} → ${points[-1][1]:.2f})",
        "INFO"
    )


def _load_whale_roster():
    if not os.path.exists(WHALE_FILE):
        S._log("📂 No whale roster found — starting fresh discovery", "INFO")
        return
    try:
        from titan_wallet import _whale_performance
        from titan_signals import restore_known_hedge_wallets

        with open(WHALE_FILE) as f:
            saved = json.load(f)

        hedge_entry = saved.pop("__hedge_wallets__", {})
        if hedge_entry.get("hedge_set"):
            restore_known_hedge_wallets(hedge_entry["hedge_set"])
            S._log(f"📂 Hedge wallets restored: {len(hedge_entry['hedge_set'])}", "INFO")

        loaded = 0
        for addr, profile in saved.items():
            addr = addr.lower()
            if addr not in S.env().wallet_cache:
                profile["ts"] = 0
                perf = profile.pop("copy_performance", None)
                if perf:
                    _whale_performance[addr] = perf
                S.env().wallet_cache[addr] = profile
                loaded += 1
                if profile.get("watchable"):
                    S.env().watchlist.add(addr)

        S._log(f"📂 Whale roster: {loaded} loaded ({len(S.env().wallet_cache)} total)", "INFO")
    except Exception as e:
        S._log(f"⚠ Whale load failed ({e})", "WARN")
