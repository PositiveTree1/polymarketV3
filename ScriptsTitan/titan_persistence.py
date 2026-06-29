"""
TITAN — Persistence layer. Single-wallet edition.
Saves: titan_state.json, titan_state.db
"""

import os, json, threading, time
from dataclasses import replace
from datetime import datetime
from typing import cast
import titan_state as S
import titan_db as DB
import titan_prices
from titan_monitor_job import start_monitored_thread
from titan_prices import PricesCacheSrv
from titan_config import STATE_FILE, STATE_DB, BANKROLL_START
from titan_wallet import Wallet, WalletsCacheSrv

_STARTUP_ELITE_VER_REFRESH_MAX_AGE_S = 2 * 24 * 3600


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
        for addr, wallet in S.env().wallet_cache.items():
            if wallet.is_active:
                DB.upsert_wallet_profile(addr, wallet)
                saved += 1
        if saved:
            S._log(f"💾 Wallet roster saved: {saved} profiles to DB", "DATA")
    except Exception as e:
        S._log(f"⚠ Wallet roster save failed: {e}", "WARN")


def save_wallet_roster_async():
    start_monitored_thread(
        job_name="save_wallet_roster",
        target=save_wallet_roster,
        warn_after=5.0,
        thread_name="titan-save-wallet-roster",
        log_label="Wallet roster save",
    )


def load_state() -> None:
    startup_t0 = time.perf_counter()
    S.log_important("━━━  TITAN startup  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    DB.init_db(STATE_DB)
    S.market_cache.load_all_from_db(force=True)
    prices_srv = PricesCacheSrv()
    prices_srv.init_db(STATE_DB)
    titan_prices.PRICES = prices_srv
    wallets_srv = WalletsCacheSrv()
    S.env().wallet_cache = wallets_srv
    S._shared_wallet_cache = wallets_srv
    from titan_config import SEED_WATCHLIST as _SEEDS
    pruned = DB.purge_non_watchable(keep_seed=set(_SEEDS))
    if pruned:
        S._log(f"🗑 Pruned {pruned} non-watchable wallet stubs from DB", "INFO")
    S.env().hft_wallet_addrs = DB.load_hft_wallets()
    _load_wallets_from_db()
    n_w   = len(S.env().wallet_cache)
    cache = S.env().wallet_cache
    n_elite    = sum(1 for p in cache.values() if p.is_elite)
    n_verified = sum(1 for p in cache.values() if p.is_verified)
    n_watch    = sum(1 for p in cache.values() if p.is_watchable)
    n_hft = len(S.env().hft_wallet_addrs)
    S.log_important(
        f"  DB:      {len(S.market_cache)} markets | "
        f"{n_w} wallets loaded  ({n_elite} elite / {n_verified} verified / {n_watch} watch) | "
        f"{n_hft} known HFT wallets"
    )
    ri = _load_trading_state()
    S.log_important(
        f"  State:   {ri['trades']} trades | {ri['open_positions']} open positions | "
        f"{ri['closed_trades']} closed | {ri['equity_points']} equity pts | "
        f"{ri['cooldowns']} cooldowns"
    )
    S.log_important(f"  Load time: {time.perf_counter() - startup_t0:.2f}s")


def _load_trading_state() -> dict[str, int]:
    phase_t0 = time.perf_counter()
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
            S.log_important("Startup recovery: trade stats restored from DB | phase=trading_state")
        else:
            S.log_important("Startup recovery: rebuilding trade stats from trade history | phase=trading_state")
            _rebuild_trade_stats(env)
            DB.upsert_trade_stats(env.trade_stats)

        from titan_position import group_trades_by_position, build_position_from_trades
        S.log_important("Startup recovery: loading trade history from DB | phase=trading_state")
        all_trades = DB.load_trade_history(limit=5000)
        recovery_info["trades"] = len(all_trades)
        S.log_important(f"Startup recovery: trade history loaded | phase=trading_state | trades={len(all_trades)}")
        groups = group_trades_by_position(all_trades)
        env.open_positions = {}
        S.log_important(f"Startup recovery: rebuilding open positions | phase=trading_state | groups={len(groups)}")
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
        linked_position_wallets: list[str] = []
        preferred_position_wallet_names: dict[str, str] = {}
        for pos in env.open_positions.values():
            linked_position_wallets.extend(pos.elite_wallets)
            linked_position_wallets.extend(pos.tracked_wallets)
            for index, wallet_addr in enumerate(pos.elite_wallets):
                wallet_key = str(wallet_addr).lower()
                if index < len(pos.wallet_names):
                    preferred_position_wallet_names[wallet_key] = str(pos.wallet_names[index])
        S.log_important(
            f"Startup recovery: hydrating open-position wallets | phase=trading_state | "
            f"positions={n_pos} linked_wallets={len(linked_position_wallets)}"
        )
        dead_position_wallets = ensure_linked_wallets_cached(
            linked_position_wallets,
            preferred_names=preferred_position_wallet_names,
            reason="startup open positions",
        )
        dead_position_set = {wallet_addr.lower() for wallet_addr in dead_position_wallets}
        for pos in env.open_positions.values():
            pos.buy_trade.dead_wallets = [
                wallet_addr for wallet_addr in pos.elite_wallets + pos.tracked_wallets
                if str(wallet_addr).lower() in dead_position_set
            ]
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

        S.log_important("Startup recovery: loading equity history | phase=trading_state")
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
        S.log_important(
            f"Startup recovery: trading_state complete | open_positions={n_pos} "
            f"dead_wallets={len(dead_position_wallets)} equity_points={recovery_info['equity_points']} "
            f"elapsed={time.perf_counter() - phase_t0:.2f}s"
        )

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




def _make_stub(addr: str, detail: str) -> Wallet:
    return Wallet.make_stub(addr, detail)


def _make_dead_wallet(addr: str, preferred_name: str, reason: str, base_wallet: Wallet | None = None) -> Wallet:
    display_name = preferred_name.strip()
    if not display_name:
        if base_wallet is not None and base_wallet.name and not str(base_wallet.name).upper().startswith("DEAD WALLET"):
            display_name = str(base_wallet.name)
        else:
            display_name = addr[:10] + "…"
    dead_name = display_name if display_name.upper().startswith("DEAD WALLET") else f"DEAD WALLET {display_name}"
    from titan_wallet import WalletTier as _WT
    base = base_wallet if base_wallet is not None else Wallet.make_stub(addr, "dead_wallet", status=_WT.WATCH)
    dead_wallet = replace(
        base,
        name=dead_name,
        ts=time.time(),
        status=_WT.WATCH,
        dead=True,
        detail=f"DEAD WALLET: {reason}",
        fail_reasons=["dead_wallet"],
    )
    S.env().wallet_cache[addr] = dead_wallet
    DB.upsert_wallet_profile(addr, dead_wallet)
    return dead_wallet


def ensure_linked_wallets_cached(
    wallet_addrs: list[str],
    *,
    preferred_names: dict[str, str] | None = None,
    reason: str,
) -> list[str]:
    from titan_wallet import get_compute_and_store_wallet

    stats = {
        "requested": 0,
        "recovered": 0,
        "pinned_watchable": 0,
        "dead": 0,
        "failed": 0,
    }
    dead_wallets: list[str] = []
    seen_wallets: set[str] = set()
    for wallet_addr in wallet_addrs:
        wallet_key = str(wallet_addr).lower().strip()
        if not wallet_key or wallet_key in seen_wallets:
            continue
        seen_wallets.add(wallet_key)
        preferred_name = ""
        if preferred_names is not None:
            preferred_name = str(preferred_names.get(wallet_key) or "")
        cached_wallet = S.env().wallet_cache.get(wallet_key)
        if cached_wallet is not None:
            if cached_wallet.dead:
                dead_wallets.append(wallet_key)
                stats["dead"] += 1
                continue
            if not cached_wallet.is_watchable:
                from titan_wallet import WalletTier as _WT
                pinned_wallet = replace(cached_wallet, status=max(cached_wallet.status, _WT.WATCH))
                S.env().wallet_cache[wallet_key] = pinned_wallet
                DB.upsert_wallet_profile(wallet_key, pinned_wallet)
                stats["pinned_watchable"] += 1
                S._log(f"[wallet recovery] {reason}: pinned cached wallet {wallet_key} as watchable", "INFO")
            continue
        stats["requested"] += 1
        try:
            wallet = get_compute_and_store_wallet(wallet_key)
        except Exception as e:
            S._log(f"[wallet recovery] {reason}: failed to hydrate {wallet_key}: {e}", "WARN")
            _make_dead_wallet(wallet_key, preferred_name, reason)
            dead_wallets.append(wallet_key)
            stats["dead"] += 1
            stats["failed"] += 1
            continue
        stats["recovered"] += 1
        if wallet.dead:
            dead_wallets.append(wallet_key)
            stats["dead"] += 1
            continue
        if "no_data" in wallet.fail_reasons:
            _make_dead_wallet(wallet_key, preferred_name, reason, wallet)
            dead_wallets.append(wallet_key)
            stats["dead"] += 1
            continue
        if not wallet.is_watchable:
            from titan_wallet import WalletTier as _WT
            pinned_wallet = replace(wallet, status=max(wallet.status, _WT.WATCH))
            S.env().wallet_cache[wallet_key] = pinned_wallet
            DB.upsert_wallet_profile(wallet_key, pinned_wallet)
            stats["pinned_watchable"] += 1
            S._log(f"[wallet recovery] {reason}: pinned {wallet_key} as watchable", "INFO")
    if stats["requested"]:
        S._log(
            f"[wallet recovery] {reason}: requested={stats['requested']} recovered={stats['recovered']} "
            f"pinned={stats['pinned_watchable']} dead={stats['dead']} failed={stats['failed']}",
            "INFO",
        )
    return dead_wallets


def _refresh_elite_ver_wallets() -> None:
    from titan_wallet import get_compute_and_store_wallet
    import time
    stale_before = time.time() - _STARTUP_ELITE_VER_REFRESH_MAX_AGE_S

    def _startup(msg: str) -> None:
        S._log(f"[STARTUP] {msg}", "INFO", terminal=True)

    def _fmt_date(ts: float | None) -> str:
        if not ts or ts <= 0.0:
            return "?"
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")

    targets = [
        (addr, p) for addr, p in S.env().wallet_cache.items()
        if p.is_ranked and (p.ts <= stale_before or (p.is_elite and (not p.pnl_series or p.pos_top5_pnl_share is None or p.trd_top5_pnl_share is None or p.pos_median_roi is None or p.trd_median_roi is None)))
    ]
    elite_ver_total = sum(1 for p in S.env().wallet_cache.values() if p.is_ranked)
    fresh_count = elite_ver_total - len(targets)
    if not targets:
        _startup(f"ELITE/VER startup refresh summary: fresh={fresh_count} refresh=0 total={elite_ver_total}")
        _startup("Skipping ELITE/VER refresh at startup - no wallet older than 2 days")
        return
    total = len(targets)
    _startup(f"ELITE/VER startup refresh summary: fresh={fresh_count} refresh={total} total={elite_ver_total}")
    _startup(f"Refreshing {total} stale ELITE/VER wallets from Polymarket...")
    for i, (addr, p) in enumerate(targets, 1):
        tier_before = p.tier()
        name = p.name or addr[:14] + "..."
        try:
            refreshed = get_compute_and_store_wallet(addr, force_refresh=True)
            if refreshed.is_active:
                DB.upsert_wallet_profile(addr, refreshed)
            tier_after = refreshed.tier()
            tier_change = f" {tier_before}=>{tier_after}" if tier_before != tier_after else f" {tier_after}"
            added = refreshed.loaded_trade_count - (p.loaded_trade_count or 0)
            added_text = f"+{added}" if added > 0 else "="
            trade_count_text = f"{refreshed.loaded_trade_count}{'*' if refreshed.trade_load_limited else ''}({added_text})"
            first_trade_text = _fmt_date(refreshed.first_loaded_trade_ts)
            last_trade_text  = _fmt_date(refreshed.last_loaded_trade_ts)
            _startup(
                f"  {i}/{total}{tier_change} {name} "
                f"trades={trade_count_text} range={first_trade_text}->{last_trade_text}"
            )
        except Exception as e:
            import traceback
            S._log(f"Refresh failed for {addr[:14]}...: {e}\n{traceback.format_exc()}", "WARN")
    _startup("ELITE/VER refresh done - server ready")
    save_wallet_roster()


def _load_wallets_from_db() -> None:
    from titan_config import SEED_WATCHLIST, VIP_WALLETS, VIP_WALLET_NAMES
    from titan_wallet import Wallet
    vip_wallets = {w.lower() for w in VIP_WALLETS}
    profiles = DB.load_watchable_wallets()
    with_profile = 0
    legacy = 0
    failed = 0
    for addr, raw in profiles.items():
        if addr in S.env().wallet_cache:
            continue
        if raw is not None:
            try:
                wallet = Wallet.from_db(addr, raw)
                wallet.vip = addr.lower() in vip_wallets
                vip_name = VIP_WALLET_NAMES.get(addr.lower(), "")
                if vip_name and (not wallet.name or wallet.name.startswith("0x") or wallet.name.endswith("…")):
                    wallet.name = vip_name
                if wallet.ts < 0.0:
                    wallet.ts = 0.0
                display = wallet.name if wallet.name and not wallet.name.startswith("0x") else f"{addr[:14]}…"
                S._log(f"📂 LOAD {display} {wallet.status.display()}", "DIAG")
                S.env().wallet_cache[addr] = wallet
                with_profile += 1
            except Exception as e:
                import traceback
                S._log(f"⚠ Wallet load failed {addr[:14]}… ({e})\n{traceback.format_exc()}", "WARN")
                failed += 1
        else:
            S.env().wallet_cache[addr] = _make_stub(addr, "legacy")
            legacy += 1

    seeds_added = 0
    for addr in SEED_WATCHLIST:
        a = addr.lower()
        if a not in S.env().wallet_cache:
            S.env().wallet_cache[a] = _make_stub(a, "seed")
            DB.set_watchable(a, True)
            seeds_added += 1

    S._log(
        f"📂 Wallets loaded: {with_profile} with profile, {legacy} legacy, {seeds_added} seeds, {failed} failed | "
        f"watchable={len(S.get_watchlist())}",
        "INFO",
    )
