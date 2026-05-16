"""
TITAN — Persistence layer. Single-wallet edition.
Saves: titan_state.json, titan_whales.json, titan_state.db
"""

import os, json, threading, time
from datetime import datetime
from typing import cast
import titan_state as S
import titan_db as DB
from titan_config import STATE_FILE, WHALE_FILE, STATE_DB, BANKROLL_START, SEED_WATCHLIST
from titan_wallet import WalletProfile


def save_state():
    try:
        env = S.env()

        if env.watchlist:
            DB.upsert_watchlist(env.watchlist)

        state = {
            "bankroll":           env.paper_bankroll,
            "session_pnl":        env.session_pnl,
            "active_market_cids": list(env.active_market_cids),
            "cooldown_cids":      env.cooldown_cids,
            "position_whale_map": {k: list(v) for k, v in env.position_whale_map.items()},
            "signal_first_seen_by_asset": env.signal_first_seen_by_asset,
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
    DB.init_db(STATE_DB)
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
        env.active_market_cids = set(state.get("active_market_cids", []))
        env.cooldown_cids      = state.get("cooldown_cids", {})
        env.position_whale_map = {k: set(v) for k, v in state.get("position_whale_map", {}).items()}
        env.signal_first_seen_by_asset = {
            str(asset): float(ts)
            for asset, ts in state.get("signal_first_seen_by_asset", {}).items()
            if str(asset)
        }

        db_wl = DB.load_watchlist()
        if db_wl:
            env.watchlist.update(db_wl)
        else:
            saved_wl = state.get("watchlist", [])
            if saved_wl:
                env.watchlist.update(saved_wl)
                DB.upsert_watchlist(env.watchlist)
                S._log(f"📂 Watchlist migrated from JSON: {len(saved_wl)} addresses", "INFO")

        json_trades = state.get("trade_history", [])
        if json_trades and DB.get_trade_count() == 0:
            DB.bulk_insert_trades(json_trades)
            S._log(f"📂 Trade history migrated from JSON: {len(json_trades)} records", "INFO")

        loaded_stats = DB.load_trade_stats()
        if loaded_stats is not None:
            env.trade_stats = cast(S.TradeStats, loaded_stats)
        else:
            _rebuild_trade_stats(env)
            DB.upsert_trade_stats(env.trade_stats)

        _migrate_null_cid_trades(state)

        from titan_position import group_trades_by_position, build_position_from_trades
        all_trades = DB.load_trade_history(limit=5000)
        groups = group_trades_by_position(all_trades)
        env.open_positions = {}
        for bucket_trades in groups.values():
            has_sell = any(t.type == "SELL" for t in bucket_trades)
            if has_sell:
                continue
            pos = build_position_from_trades(bucket_trades)
            key = (pos.cid, pos.outcome)
            env.open_positions[key] = pos
            env.active_market_cids.add(pos.cid or pos.asset)

        n_pos = len(env.open_positions)
        if n_pos:
            S._log(f"📂 Rebuilt {n_pos} open position(s) from DB trades", "INFO")
        else:
            S._log("📂 No open positions in DB", "INFO")

        S._log(
            f"📂 State loaded: bankroll=${env.paper_bankroll:.2f} | "
            f"{env.trade_stats.sell_count} closed trades | {n_pos} open | "
            f"{len(env.cooldown_cids)} cooldowns | {len(env.watchlist)} watchlist",
            "INFO"
        )

        db_equity = DB.load_equity_history()
        if db_equity and len(db_equity) >= 2:
            env.equity_history = db_equity
            S._log(
                f"📈 Equity curve restored from DB: {len(db_equity)} points "
                f"(${db_equity[0][1]:.2f} → ${db_equity[-1][1]:.2f})",
                "INFO"
            )
        else:
            saved_equity = state.get("equity_history", [])
            if saved_equity and len(saved_equity) >= 2:
                env.equity_history = [tuple(p) for p in saved_equity]
                DB.upsert_equity_history(env.equity_history)
                S._log(
                    f"📈 Equity curve migrated from JSON: {len(env.equity_history)} points",
                    "INFO"
                )
            else:
                _rebuild_equity_from_trades(env)

    except Exception as e:
        S._log(f"⚠ State load failed ({e}) — fresh start", "WARN")


def _rebuild_trade_stats(env) -> None:
    from titan_state import TradeStats
    stats = TradeStats()
    for t in DB.load_trade_history():
        pnl_usdc = t.pnl_usdc
        if pnl_usdc is not None and t.type == "SELL":
            stats.record_sell(float(pnl_usdc))
    env.trade_stats = stats


def _rebuild_equity_from_trades(env):
    trades_with_br = [
        t for t in DB.load_trade_history()
        if t.bankroll and t.ts
    ]
    if not trades_with_br:
        env.equity_history = []
        return
    trades_with_br.sort(key=lambda t: t.ts)
    points = [(trades_with_br[0].ts - 1, float(BANKROLL_START))]
    for t in trades_with_br:
        points.append((float(t.ts), float(t.bankroll)))
    points.append((time.time(), env.paper_bankroll))
    env.equity_history = points
    S._log(
        f"📈 Equity curve rebuilt from {len(trades_with_br)} trade records "
        f"(${points[0][1]:.2f} → ${points[-1][1]:.2f})",
        "INFO"
    )


def _migrate_null_cid_trades(state: dict) -> None:
    """Repair corrupt cids in trade_history using two sources of truth:
    1. JSON open_positions: authority for open BUY cids (matched by title+outcome).
    2. Consistency: if a SELL's cid doesn't match any BUY with the same title+outcome, null it out.
    """
    import sqlite3
    with sqlite3.connect(STATE_DB) as cx:
        rows = cx.execute("SELECT id, type, title, outcome, cid FROM trade_history").fetchall()

        # Build title+outcome -> set of cids seen on BUY trades
        buy_cids: dict[tuple[str, str], set[str]] = {}
        for _, typ, title, outcome, cid in rows:
            if typ == "BUY" and cid:
                buy_cids.setdefault((title or "", outcome or ""), set()).add(cid)

        # Override with JSON open_positions as definitive source for open BUYs
        json_authority: dict[tuple[str, str], dict] = {}
        for d in state.get("open_positions", {}).values():
            cid = str(d.get("cid") or "")
            if not cid:
                continue
            key = (str(d.get("title") or ""), str(d.get("outcome") or ""))
            json_authority[key] = {
                "cid":        cid,
                "asset":      str(d.get("asset") or ""),
                "slug":       str(d.get("slug") or ""),
                "event_slug": str(d.get("event_slug") or ""),
                "market_url": str(d.get("market_url") or ""),
            }
            buy_cids[key] = {cid}

        updated = 0
        for row_id, typ, title, outcome, db_cid in rows:
            key = (title or "", outcome or "")
            info = json_authority.get(key)
            if info and db_cid != info["cid"]:
                cx.execute(
                    "UPDATE trade_history SET cid=?, asset=?, slug=?, event_slug=?, market_url=? WHERE id=?",
                    (info["cid"], info["asset"], info["slug"], info["event_slug"], info["market_url"], row_id),
                )
                updated += 1
            elif typ == "SELL" and db_cid and db_cid not in (buy_cids.get(key) or set()):
                # SELL cid points to a different market — corrupt, detach it
                cx.execute("UPDATE trade_history SET cid=NULL WHERE id=?", (row_id,))
                updated += 1

    if updated:
        S._log(f"📂 Migrated {updated} trade record(s): repaired cids from JSON+consistency check", "INFO")


def _load_whale_roster():
    if not os.path.exists(WHALE_FILE):
        S._log(f"📂 No whale roster found at {WHALE_FILE} — starting fresh discovery", "INFO")
        return
    try:
        from titan_wallet import _whale_performance
        from titan_signals import restore_known_hedge_wallets

        with open(WHALE_FILE) as f:
            saved = json.load(f)

        hedge_entry = saved.pop("__hedge_wallets__", {})
        if hedge_entry.get("hedge_set"):
            restore_known_hedge_wallets(hedge_entry["hedge_set"])

        loaded = 0
        for addr, profile in saved.items():
            addr = addr.lower()
            if addr not in S.env().wallet_cache:
                profile["ts"] = 0
                perf = profile.pop("copy_performance", None)
                if perf:
                    _whale_performance[addr] = perf
                S.env().wallet_cache[addr] = cast(WalletProfile, profile)
                loaded += 1
                if profile.get("watchable"):
                    S.env().watchlist.add(addr)

        if loaded:
            S._log(f"📂 Loaded {loaded} whale(s) from {WHALE_FILE} ({len(S.env().wallet_cache)} total in cache)", "INFO")
        else:
            S._log(f"📂 No new whales loaded from {WHALE_FILE} (all already cached or file empty)", "INFO")
    except Exception as e:
        S._log(f"⚠ Whale load failed ({e})", "WARN")
