"""
TITAN — Persistence layer. Single-wallet edition.
Saves: titan_state.json, titan_state.db
"""

import os, json, threading, time
from datetime import datetime
from typing import cast
import titan_state as S
import titan_db as DB
import titan_prices
from titan_prices import PricesCacheSrv
from titan_config import STATE_FILE, STATE_DB, BANKROLL_START, MAX_WATCHLIST_SIZE
from titan_wallet import WalletProfile


def save_state():
    try:
        env = S.env()

        state = {
            "bankroll":           env.paper_bankroll,
            "session_pnl":        env.session_pnl,
            "active_market_cids": list(env.active_market_cids),
            "cooldown_cids":      env.cooldown_cids,
            "position_wallet_map": {k: list(v) for k, v in env.position_wallet_map.items()},
            "signal_first_seen_by_asset": env.signal_first_seen_by_asset,
            "saved_at":           datetime.now().isoformat(),
        }
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        S._log(f"⚠ Save failed: {e}", "WARN")


def save_wallet_roster():
    try:
        saved = 0
        for addr, profile in S.env().wallet_cache.items():
            if profile.get("verified") or profile.get("watchable"):
                DB.upsert_wallet_profile(addr, profile)
                saved += 1
        if saved:
            S._log(f"💾 Wallet roster saved: {saved} profiles to DB", "DATA")
    except Exception as e:
        S._log(f"⚠ Wallet roster save failed: {e}", "WARN")


def save_wallet_roster_async():
    threading.Thread(target=save_wallet_roster, daemon=True).start()


def load_state():
    DB.init_db(STATE_DB)
    S.market_cache.load_all_from_db(force=True)
    srv = PricesCacheSrv()
    srv.init_db(STATE_DB)
    titan_prices.PRICES = srv
    from titan_config import SEED_WATCHLIST as _SEEDS
    pruned = DB.purge_non_watchable(keep_seed=set(_SEEDS))
    if pruned:
        S._log(f"🗑 Pruned {pruned} non-watchable wallet stubs from DB", "INFO")
    _load_wallets_from_db()
    _refresh_elite_ver_wallets()
    ri = _load_trading_state()
    wl = S.get_watchlist()
    line = (
        f"Startup: markets={len(S.market_cache)} | "
        f"wallets={len(S.env().wallet_cache)} watchable={len(wl)} | "
        f"trades={ri['trades']} Open Positions={ri['open_positions']} closed={ri['closed_trades']} | "
        f"equity_pts={ri['equity_points']} cooldowns={ri['cooldowns']}"
    )
    S.log_important(line)


def _load_trading_state() -> dict[str, int]:
    recovery_info = {
        "watchlist": 0,
        "trades": 0,
        "open_positions": 0,
        "closed_trades": 0,
        "equity_points": 0,
        "cooldowns": 0,
    }
    if not os.path.exists(STATE_FILE):
        S._log("📂 No saved state — fresh start", "INFO")
        return recovery_info
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
        env.position_wallet_map = {k: set(v) for k, v in state.get("position_wallet_map", {}).items()}
        env.signal_first_seen_by_asset = {
            str(asset): float(ts)
            for asset, ts in state.get("signal_first_seen_by_asset", {}).items()
            if str(asset)
        }

        recovery_info["watchlist"] = len(S.get_watchlist())

        loaded_stats = DB.load_trade_stats()
        if loaded_stats is not None:
            env.trade_stats = cast(S.TradeStats, loaded_stats)
        else:
            _rebuild_trade_stats(env)
            DB.upsert_trade_stats(env.trade_stats)

        from titan_position import group_trades_by_position, build_position_from_trades
        all_trades = DB.load_trade_history(limit=5000)
        recovery_info["trades"] = len(all_trades)
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
        recovery_info["open_positions"] = n_pos
        recovery_info["closed_trades"] = env.trade_stats.sell_count
        recovery_info["cooldowns"] = len(env.cooldown_cids)
        if n_pos:
            S._log(f"📂 Rebuilt {n_pos} open position(s) from DB trades", "INFO")
        else:
            S._log("📂 No open positions in DB", "INFO")

        S._log(
            f"📂 State loaded: bankroll=${env.paper_bankroll:.2f} | "
            f"{env.trade_stats.sell_count} closed trades | {n_pos} open | "
            f"{len(env.cooldown_cids)} cooldowns | {len(S.get_watchlist())} watchlist",
            "INFO"
        )

        db_equity = DB.load_equity_history()
        if db_equity and len(db_equity) >= 2:
            env.equity_history = db_equity
            recovery_info["equity_points"] = len(db_equity)
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
                recovery_info["equity_points"] = len(env.equity_history)
                S._log(
                    f"📈 Equity curve migrated from JSON: {len(env.equity_history)} points",
                    "INFO"
                )
            else:
                _rebuild_equity_from_trades(env)
                recovery_info["equity_points"] = len(env.equity_history)

    except Exception as e:
        S._log(f"⚠ State load failed ({e}) — fresh start", "WARN")
    return recovery_info


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




def _make_stub(addr: str, detail: str) -> "WalletProfile":
    from titan_config import VIP_WALLETS
    is_vip = addr.lower() in {wallet.lower() for wallet in VIP_WALLETS}
    return cast(WalletProfile, {  # type: ignore[arg-type]
        "score": 0.10, "win_rate": 0.0, "wilson_lb": 0.0, "alpha_per_trade": 0.0,
        "n_resolved": 0, "n_pos": 0, "total_value": 0.0,
        "total_pnl": 0.0, "pnl_pct": 0.0, "avg_pos_size": 0.0,
        "avg_profit": 0.0, "avg_bet": 0.0, "trades_per_hour": 0.0,
        "recent_pnl_30d": None, "recent_pnl_7d": None, "recent_ts": 0.0,
        "verified": False, "watchable": True, "elite": False, "hft": False, "vip": is_vip, "sports_bot": False,
        "name": addr[:10] + "…", "ts": 0.0,
        "lb_rank": None, "lb_vol": None,
        "detail": detail, "wr_source": "none", "fail_reasons": [],
    })


def _refresh_elite_ver_wallets() -> None:
    from titan_wallet import get_compute_and_store_wallet
    import time
    targets = [
        (addr, p) for addr, p in S.env().wallet_cache.items()
        if p.get("elite") or p.get("verified")
    ]
    if not targets:
        return
    total = len(targets)
    def _startup(msg: str) -> None:
        print(f"[STARTUP] {msg}", flush=True)
        S._log(f"[STARTUP] {msg}", "INFO")

    _startup(f"Refreshing {total} ELITE/VER wallets from Polymarket…")
    for i, (addr, p) in enumerate(targets, 1):
        tier = "ELITE" if p.get("elite") else "VER"
        name = p.get("name") or addr[:14] + "…"
        _startup(f"  {i}/{total} {tier} {name}")
        try:
            get_compute_and_store_wallet(addr)
            time.sleep(0.5)
        except Exception as e:
            import traceback
            S._log(f"⚠ Refresh failed for {addr[:14]}…: {e}\n{traceback.format_exc()}", "WARN")
    _startup(f"ELITE/VER refresh done — server ready")
    save_wallet_roster()


def _load_wallets_from_db() -> None:
    from titan_config import SEED_WATCHLIST, VIP_WALLETS, VIP_WALLET_NAMES
    vip_wallets = {wallet.lower() for wallet in VIP_WALLETS}
    try:
        profiles = DB.load_watchable_wallets(MAX_WATCHLIST_SIZE)
        with_profile = 0
        legacy = 0
        for addr, profile in profiles.items():
            if addr in S.env().wallet_cache:
                continue
            if profile is not None:
                profile["vip"] = addr.lower() in vip_wallets
                vip_name = VIP_WALLET_NAMES.get(addr.lower(), "")
                current_name = str(profile.get("name") or "")
                if vip_name and (not current_name or current_name.startswith("0x") or current_name.endswith("…")):
                    profile["name"] = vip_name
                if profile.get("elite") or profile.get("verified") or profile.get("lb_rank") is None:
                    profile["ts"] = 0.0
                e = "🔥ELITE" if profile.get("elite") else ("✅VER" if profile.get("verified") else "👁WATCH")
                S._log(f"📂 LOAD {addr[:14]}… {e} elite={profile.get('elite')} verified={profile.get('verified')} watchable={profile.get('watchable')}", "DIAG")
                S.env().wallet_cache[addr] = cast(WalletProfile, profile)
                with_profile += 1
            else:
                S.env().wallet_cache[addr] = _make_stub(addr, "legacy")
                legacy += 1

        # Ensure SEED_WATCHLIST addresses are always watchable, within the cap
        seeds_added = 0
        for addr in SEED_WATCHLIST:
            a = addr.lower()
            if a not in S.env().wallet_cache and len(S.get_watchlist()) < MAX_WATCHLIST_SIZE:
                S.env().wallet_cache[a] = _make_stub(a, "seed")
                DB.set_watchable(a, True)
                seeds_added += 1

        S._log(
            f"📂 Wallets loaded: {with_profile} with profile, {legacy} legacy, {seeds_added} seeds | "
            f"watchable={len(S.get_watchlist())}",
            "INFO",
        )
    except Exception as e:
        S._log(f"⚠ Wallet load failed ({e})", "WARN")
