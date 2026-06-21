"""
TITAN — Main orchestration engine. Single-wallet edition. v10 MULTI-STRATEGY.

Two loops run in parallel:
  • run_loop()      — main 15s cycle: public feed + elite poll + signals + trade
  • _hft_fast_loop()— 3s cycle: polls only HFT wallets, looks for outsized spikes

v10 CHANGES:
  1. MULTI-STRATEGY: build_signals() now dispatches to recent_form, drift_discount,
     and consensus_basket sub-builders. Engine logs strategy tags in per-trade output.
  2. RECENT FORM REFRESH: Every 50 cycles, _refresh_recent_form_scores() runs
     in a background thread to keep recent_pnl_30d/7d current (6h TTL).
  3. STRATEGY LOG BANNER: start() prints active strategies and their key params.
  4. CYCLE STATS: per-cycle log now shows signal counts per strategy.

EXIT PHILOSOPHY (unchanged from v9):
  Follow the wallet. If the tracked wallet who triggered our buy has NOT sold, we do NOT
  sell regardless of stop-loss or profit target — unless the market is resolving.
  WALLET_EXIT_SELL controls this. Each signal now also carries stop_loss_pct from
  its strategy config (None = no stop loss).
"""

import time
import threading

from titan_config import *
import titan_config as C
import titan_state as S
from titan_state import _log, safe_get

import titan_db as DB
from titan_monitor_job import monitored_step, start_monitored_thread
from titan_persistence import load_state, save_state, save_wallet_roster, save_wallet_roster_async
from titan_wallet  import (get_compute_and_store_wallet, get_elite_wallets, discover_new_wallets,
                           scan_top_market_holders, get_wallet_performance_summary,
                           _refresh_recent_form_scores)
from titan_market  import get_market, fetch_trades, fetch_hft_spike_trades
from titan_signals import build_signals, check_wallet_exist, _adaptive_bet_caps
from titan_trader  import auto_trade

_HFT_FAST_CYCLE = 3  # seconds between HFT polls
_startup_wallet_refresh_done = threading.Event()


def _run_startup_wallet_refresh() -> None:
    from titan_persistence import _refresh_elite_ver_wallets

    try:
        _refresh_elite_ver_wallets()
    except Exception as e:
        import traceback
        _log(f"Startup wallet refresh failed: {e}\n{traceback.format_exc()[:2000]}", "ERR")
    finally:
        _startup_wallet_refresh_done.set()


def _wait_for_startup_wallet_refresh(loop_name: str) -> None:
    if _startup_wallet_refresh_done.is_set():
        return
    _log(f"{loop_name} waiting for startup wallet refresh to finish", "INFO")
    while not _startup_wallet_refresh_done.wait(timeout=10):
        _log(f"{loop_name} still waiting for startup wallet refresh", "INFO")


def _rescore_watchlist():
    now_t = time.time()
    must  = set(w.lower() for w in VIP_WALLETS) | set(w.lower() for w in PRIORITY_WALLETS)
    stale = [
        w for w in S.get_watchlist()
        if w not in must
        and (now_t - (p3.ts if (p3 := S.env().wallet_cache.get(w)) is not None else 0)) >= WALLET_TTL
    ]
    to_score = list(must) + stale
    if not to_score:
        return
    _log(f"♻ Re-scoring {len(to_score)} wallets…", "DATA")
    from titan_wallet import _reclassify_in_progress as _rip
    for w in to_score:
        if _rip.is_set():
            _log("♻ Re-score interrupted — reclassify_all in progress", "DATA")
            return
        try:
            get_compute_and_store_wallet(w)
            time.sleep(0.15)
        except Exception as e:
            _log(f"Re-score failed for {w}: {e}", "ERR")
    new_elite = sum(1 for profile in S.env().wallet_cache.values() if profile.is_elite)
    _log(f"♻ Re-score done | {new_elite} elite total", "DATA")
    save_wallet_roster_async()


def analyse(trades: list, is_hft_loop: bool = False) -> None:
    S.env().cycle_count += 1
    step_warn_after = 3.0 if is_hft_loop else 10.0

    if S.env().cycle_count % DISCOVERY_INTERVAL_CYCLES == 0:
        start_monitored_thread(
            job_name="wallet_discovery",
            target=discover_new_wallets,
            warn_after=10.0,
            log_label="Wallet discovery",
        )
    if S.env().cycle_count % 5 == 0:
        start_monitored_thread(
            job_name="market_holder_scan",
            target=scan_top_market_holders,
            warn_after=10.0,
            log_label="Market holder scan",
        )
    if S.env().cycle_count % 20 == 2:
        start_monitored_thread(
            job_name="watchlist_rescore",
            target=_rescore_watchlist,
            warn_after=10.0,
            log_label="Watchlist rescore",
        )

    # v10: Refresh recent form scores (6h TTL) for Recent Form strategy
    if S.env().cycle_count % 50 == 3:
        start_monitored_thread(
            job_name="recent_form_refresh",
            target=_refresh_recent_form_scores,
            warn_after=10.0,
            log_label="Recent form refresh",
        )

    # Score wallets seen in the feed
    from titan_market import WalletObservation as _WO
    bad = [t for t in trades if not isinstance(t, _WO)]
    if bad:
        _log(f"⚠ analyse() got non-WalletObservation items: {[type(x).__name__ for x in bad[:3]]}", "ERR")
        trades = [t for t in trades if isinstance(t, _WO)]
    feed_wallets = {t.wallet for t in trades}
    wallets      = {}
    ver_count    = 0
    elite_count  = 0

    def _is_auto(n):
        parts = n.split("-")
        if len(parts) != 2: return False
        a, b = parts
        return a and b and a[0].isupper() and b[0].isupper() and a.isalpha() and b.isalpha()

    with monitored_step("analyse.wallet_scoring", warn_after=step_warn_after):
        for w in feed_wallets:
            p = get_compute_and_store_wallet(w)
            trade_name = next(
                (t.name for t in trades
                 if t.wallet.lower() == w and t.name and not t.name.endswith("…")),
                None
            )
            if trade_name:
                current = p.name
                current_real = current and not current.endswith("…") and not _is_auto(current)
                if not _is_auto(trade_name) or not current_real:
                    p.name = trade_name
                    S.env().wallet_cache[w] = p

            cached = S.env().wallet_cache.get(w)
            if cached:
                if not p.recent_pnl_30d and cached.recent_pnl_30d:
                    p.recent_pnl_30d = cached.recent_pnl_30d
                if not p.recent_pnl_7d and cached.recent_pnl_7d:
                    p.recent_pnl_7d = cached.recent_pnl_7d
                if not p.recent_ts and cached.recent_ts:
                    p.recent_ts = cached.recent_ts

            wallets[w] = p
            if p.is_verified:  ver_count  += 1
            if p.is_elite:     elite_count += 1
            time.sleep(0.04)

    # CRITICAL FIX: Inject all known elite/verified wallets from cache into the
    # wallets dict. Without this, HFT fast loop cycles only see 1-3 wallets
    # (just those in the spike batch), so build_signals always gets "0 elite"
    # even when the spiking wallet IS elite and well-known.
    # We don't re-fetch them (too slow) — just pass the cached profile.
    for w, cached_profile in S.env().wallet_cache.items():
        if w not in wallets and cached_profile.is_ranked:
            wallets[w] = cached_profile
            if cached_profile.is_verified: ver_count  += 1
            if cached_profile.is_elite:    elite_count += 1

    if S.env().cycle_count % 10 == 0:
        elite_ws = get_elite_wallets()
        if elite_ws:
            hft_count = sum(1 for w in elite_ws if (p2 := S.env().wallet_cache.get(w)) and p2.is_hft())
            names = [(p2.name if (p2 := S.env().wallet_cache.get(w)) else None) or w[:10]+"…" for w in elite_ws[:8]]
            _log(f"🔥 Elite ({len(elite_ws)}, ⚡{hft_count} HFT): {', '.join(names)}", "INFO")

    # Wallet exit monitoring
    cid_to_wallet_sets = {cid: set(ws) for cid, ws in S.env().position_wallet_map.items()}
    entry_times = {
        (pos.cid or key[0]): pos.entry_ts
        for key, pos in S.env().open_positions.items()
    }
    wallet_exits = {}
    with monitored_step("analyse.wallet_exit_check", warn_after=step_warn_after):
        if cid_to_wallet_sets:
            wallet_exits = check_wallet_exist(cid_to_wallet_sets, entry_times)
        elif S.env().open_positions:
            rebuilt = {}
            for key, pos in S.env().open_positions.items():
                cid = pos.cid or key[0]
                lwallets = set(pos.elite_wallets + pos.tracked_wallets)
                if lwallets:
                    rebuilt[cid] = lwallets
            if rebuilt:
                wallet_exits = check_wallet_exist(rebuilt, entry_times)

    with monitored_step("analyse.build_signals", warn_after=step_warn_after):
        signals, rejects = build_signals(trades, wallets, wallet_exits)

    # v10: Break down signal count by strategy for the cycle log
    strat_counts: dict = {}
    for sig in signals:
        primary = sig.strategy.split("+")[0]
        strat_counts[primary] = strat_counts.get(primary, 0) + 1
    strat_str = " ".join(f"{s}:{n}" for s, n in strat_counts.items()) if strat_counts else "none"

    prev_signals = S.env().LAST_SIGNALS
    no_trades    = not trades

    if no_trades:
        feed_note = f" | ⏸ Polymarket /trades down — {len(prev_signals)} stale signal(s) not tradeable" if prev_signals else " | ⏸ Polymarket /trades down"
    else:
        feed_note = ""

    _log(
        f"🎯 {len(signals)} signals [{strat_str}] | {len(rejects)} rejects | "
        f"{ver_count} verified ({elite_count} elite) wallets{feed_note}",
        "INFO"
    )

    if no_trades and prev_signals:
        for ps in prev_signals:
            _log(f"  ⏸ Stale (not traded): {ps.outcome:<12} {ps.title[:45]} score={ps.score:.2f}", "DIAG")
    elif rejects:
        prefix = "HFT Spike" if is_hft_loop else "Signal"
        for r in rejects:
            _log(f"  ❌ {prefix} rejected: {r.replace(chr(10), ' ')}", "DIAG")

    # Log signals that were active last cycle but didn't survive this cycle
    if trades and prev_signals:
        new_cids = {s.cid for s in signals}
        for ps in prev_signals:
            if ps.cid not in new_cids:
                matched_reject = next((r for r in rejects if ps.title[:20] in r or ps.cid[:12] in r), None)
                age_h = (time.time() - ps.newest_ts) / 3600
                if matched_reject:
                    _log(f"  ❌ Signal dropped: {ps.outcome} {ps.title[:45]} → {matched_reject.replace(chr(10), ' ')}", "DIAG")
                else:
                    _log(f"  ❌ Signal expired: {ps.outcome} {ps.title[:45]} — age={age_h:.1f}h, no trades in feed this cycle", "DIAG")

    with monitored_step("analyse.auto_trade", warn_after=step_warn_after):
        trade_events = auto_trade(signals, wallet_exits)
    for ev_type, msg, _color in trade_events:
        level = "TRADE" if ev_type in ("OPEN", "CLOSE") else "WARN"
        _log(msg, level)

    # Sample portfolio equity every cycle
    open_value = sum(
        (pos.cur_price or pos.entry_price) * pos.shares
        for pos in S.env().open_positions.values()
    )
    current_equity = S.env().paper_bankroll + open_value
    _eq = S.env().equity_history
    _now = time.time()
    if not _eq or abs(current_equity - _eq[-1][1]) > 0.001:
        point = (_now, current_equity)
        _eq.append(point)
        DB.upsert_equity_history([point])
    if len(_eq) > 5000:
        del _eq[:500]

    # Wallet report card every 50 cycles
    if S.env().cycle_count % 50 == 0 and S.env().cycle_count > 0:
        perf = get_wallet_performance_summary()
        if perf:
            _log("📊 WALLET PERFORMANCE (copy-trade outcomes):", "INFO")
            for rec in perf[:10]:
                emoji = "✅" if rec["total_pnl"] >= 0 else "❌"
                week_tag = f"  7d:${rec['weekly_pnl']:+.2f}({rec['weekly_trades']}t)" if rec.get("weekly_trades") else ""
                _log(
                    f"  {emoji} {rec['name']:<18} {rec['wins']}W/{rec['losses']}L "
                    f"WR:{rec['win_rate']*100:.0f}% PnL:${rec['total_pnl']:+.4f}{week_tag}",
                    "INFO"
                )

    # Session stats every 100 cycles
    if S.env().cycle_count % 100 == 0 and S.env().cycle_count > 0:
        st = S.env().trade_stats
        _log(
            f"📊 SESSION [{S.env().cycle_count} cycles]: {st.sell_count} closed | "
            f"{st.win_count}W/{st.loss_count}L | "
            f"WR:{st.win_rate*100:.0f}%",
            "INFO"
        )

    if S.env().cycle_count % 4 == 0:
        save_wallet_roster_async()

    if S.on_cycle_complete:
        S.on_cycle_complete(signals, wallets, rejects, trades)


# def _position_price_loop():
#     from titan_trader import _get_current_price
#     while True:
#         try:
#             for _key, pos in list(S.env().open_positions.items()):
#                 cur, _, fetched_ts = _get_current_price(pos)
#                 if fetched_ts:
#                     pos.cur_price = cur
#                     pos.cur_price_ts = fetched_ts
#         except Exception as e:
#             _log(f"position price loop error: {e}", "WARN")
#         time.sleep(5)


def _heartbeat_loop():
    while True:
        try:
            if S.on_heartbeat:
                S.on_heartbeat({"ts": time.time(), "cycle": S.env().cycle_count})
        except Exception:
            pass
        time.sleep(10)


def run_loop():
    C.reload()
    _wait_for_startup_wallet_refresh("Main loop")
    while True:
        try:
            C.reload()
            trades = fetch_trades()
            if not trades:
                import titan_state as _S
                if _S.env().feed_responded:
                    _log(f"⚠ Polymarket /trades responded but 0 trades matched (HOT={C.HOT_HOURS}h WARM={C.WARM_HOURS}h MIN_CASH={C.MIN_TRADE_CASH}) — signal build skipped", "ERR")
                else:
                    _log(f"⚠ Polymarket /trades no response (HOT={C.HOT_HOURS}h WARM={C.WARM_HOURS}h MIN_CASH={C.MIN_TRADE_CASH}) — signal build skipped", "ERR")
                trades = []
            analyse(trades)
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            _log(f"Cycle error: {e}\n{tb[:2000]}", "ERR")
            try:
                with open("cycle_error.log", "w") as _f:
                    _f.write(tb)
            except Exception:
                pass
        time.sleep(CYCLE_SECONDS)


# ── HFT Fast Loop ─────────────────────────────────────────────────────────────
def _hft_fast_loop():
    """Runs every 3s until HFT_ENABLED turns False, then exits."""
    S.log_important("⚡ HFT fast loop started (3s cycle)")
    _wait_for_startup_wallet_refresh("HFT fast loop")
    _no_hft_warned = False
    while C.HFT_ENABLED:
        try:
            hft_count = sum(
                1 for prof in S.env().wallet_cache.values()
                if prof.is_hft()
            )
            if hft_count == 0:
                if not _no_hft_warned:
                    _log("⚡ HFT fast loop: no HFT wallets yet — waiting for discovery", "WARN")
                    _no_hft_warned = True
            else:
                _no_hft_warned = False
                if not C.HFT_ENABLED:
                    break
                spike_trades = fetch_hft_spike_trades()
                if spike_trades:
                    _log(f"⚡ HFT fast loop: processing {len(spike_trades)} spike(s)…", "DIAG")
                    analyse(spike_trades, is_hft_loop=True)
        except Exception as e:
            import traceback
            _log(f"HFT fast loop error: {e}\n{traceback.format_exc()[:300]}", "ERR")
        for _ in range(_HFT_FAST_CYCLE * 10):
            if not C.HFT_ENABLED:
                break
            time.sleep(0.1)
    S.log_important("⚡ HFT fast loop stopped (hft_enabled=false)")


def _hft_watchdog():
    """Monitors HFT_ENABLED and starts/stops _hft_fast_loop thread accordingly."""
    _running: threading.Thread | None = None
    C.reload()
    while True:
        if C.HFT_ENABLED:
            if _running is None or not _running.is_alive():
                S.log_important("⚡ HFT watchdog: starting HFT fast loop")
                _running = start_monitored_thread(
                    job_name="hft_fast_loop",
                    target=_hft_fast_loop,
                    warn_after=float(_HFT_FAST_CYCLE),
                    thread_name="titan-hft-fast-loop",
                    log_label="HFT loop",
                )
        else:
            if _running is not None and _running.is_alive():
                S.log_important("⚡ HFT watchdog: waiting for HFT fast loop to stop…")
        time.sleep(_HFT_FAST_CYCLE)


def start(log_callback=None, position_open_cb=None, position_close_cb=None, cycle_cb=None, heartbeat_cb=None):
    S.on_log            = log_callback
    S.on_position_open  = position_open_cb
    S.on_position_close = position_close_cb
    S.on_cycle_complete = cycle_cb
    S.on_heartbeat      = heartbeat_cb

    _startup_wallet_refresh_done.clear()
    load_state()
    C.reload()

    from titan_wallet import WalletsCacheSrv as _WCS
    _cache = S.env().wallet_cache
    if isinstance(_cache, _WCS):
        n = _cache.reclassify_all()
        n_elite    = sum(1 for p in _cache.values() if p.is_elite)
        n_verified = sum(1 for p in _cache.values() if p.is_verified)
        n_watch    = sum(1 for p in _cache.values() if p.is_watchable)
        _log(
            f"  Wallets: {n_elite} elite / {n_verified} verified / {n_watch} watch"
            + (f"  ({n} reclassified)" if n else ""),
            "INFO", terminal=True,
        )

    active = getattr(C, "ACTIVE_STRATEGIES", [])
    max_abs, max_pct = _adaptive_bet_caps()
    _log(
        f"  Engine:  strategies={', '.join(active)}  "
        f"bankroll=${S.env().paper_bankroll:.2f}  maxBet={max_pct*100:.0f}%/${max_abs:.2f}  "
        f"maxPos={MAX_OPEN_POSITIONS}",
        "INFO", terminal=True,
    )
    _log(
        f"  Config:  price=[{MIN_ENTRY_PRICE:.2f},{MAX_ENTRY_PRICE:.2f}]  "
        f"stopLoss={'ON' if STOP_LOSS_ENABLED else 'OFF'}  "
        f"profitTarget={PROFIT_TARGET_PCT*100:.0f}%  "
        f"elite≥PnL${ELITE_MIN_PNL:,.0f}/Port${ELITE_MIN_PORT:,.0f}/Score{ELITE_MIN_SCORE}",
        "INFO", terminal=True,
    )
    _log("━" * 68, "DATA", terminal=True)

    # Start WebSocket resolution monitor
    try:
        import titan_resolution_monitor as _rm
        _rm.start()
        _rm.sync_open_positions()
    except Exception as _e:
        _log(f"⚠ WS resolution monitor failed to start: {_e}", "WARN")

    t_refresh = start_monitored_thread(
        job_name="startup_wallet_refresh",
        target=_run_startup_wallet_refresh,
        warn_after=30.0,
        thread_name="titan-startup-wallet-refresh",
        log_label="Startup refresh",
    )
    t_main = start_monitored_thread(
        job_name="main_loop",
        target=run_loop,
        warn_after=float(CYCLE_SECONDS),
        thread_name="titan-main-loop",
        log_label="Main",
    )
    t_hft = start_monitored_thread(
        job_name="hft_watchdog",
        target=_hft_watchdog,
        warn_after=float(_HFT_FAST_CYCLE) * 2.0,
        thread_name="titan-hft-watchdog",
        log_label="HFT watchdog",
    )
    start_monitored_thread(
        job_name="heartbeat_loop",
        target=_heartbeat_loop,
        warn_after=12.0,
        thread_name="titan-heartbeat-loop",
        log_label="Heartbeat",
    )
    # start_monitored_thread(
    #     job_name="position_price_loop",
    #     target=_position_price_loop,
    #     warn_after=10.0,
    #     thread_name="titan-position-price",
    #     log_label="Position price",
    # )
    return t_main


def get_system_snapshot() -> str:
    from datetime import datetime as _dt
    now = _dt.now().strftime("%Y-%m-%d %H:%M:%S")
    st        = S.env().trade_stats
    total_pnl = S.env().paper_bankroll - BANKROLL_START
    win_rate  = st.win_rate * 100
    open_value = sum(
        (pos.cur_price or pos.entry_price) * pos.shares
        for pos in S.env().open_positions.values()
    )
    current_equity = S.env().paper_bankroll + open_value
    active = getattr(C, "ACTIVE_STRATEGIES", [])
    lines = [
        "═" * 70,
        f"  TITAN v10 SNAPSHOT — {now}",
        f"  Strategies: {', '.join(active)}",
        "═" * 70, "",
        "[STATISTICS]",
        f"Net Worth : ${current_equity:.2f} (Total Equity)",
        f"Open Value: ${open_value:.2f}",
        f"Bankroll  : ${S.env().paper_bankroll:.2f}  (start ${BANKROLL_START:.2f})",
        f"Total PnL : ${total_pnl:+.4f}",
        f"Win Rate  : {win_rate:.1f}%  ({st.win_count}W/{st.loss_count}L)",
        f"Open      : {len(S.env().open_positions)}  Cycle: {S.env().cycle_count}",
        "", "[OPEN POSITIONS]",
    ]
    for key, pos in S.env().open_positions.items():
        entry = pos.entry_price
        cur   = pos.cur_price or entry
        pnl   = (cur - entry) / max(entry, 0.001) * 100
        held  = (time.time() - pos.entry_ts) / 60 if pos.entry_ts else 0.0
        hft   = "⚡" if pos.is_hft else ""
        conv  = "💎" if pos.is_conviction else ""
        strat = pos.strategy[:2].upper()
        lines.append(
            f"  {conv}{hft}[{pos.tier}|{strat}] {pos.title[:44]} / {key[1] if isinstance(key,tuple) else '?'}"
            f"  P&L:{pnl:+.1f}%  Held:{held:.0f}min  ${pos.bet:.2f}"
        )
    lines += ["", "[ELITE ROSTER]"]
    elites = sorted(
        [(w, p) for w, p in S.env().wallet_cache.items() if p.is_elite],
        key=lambda x: x[1].total_pnl, reverse=True
    )
    for w, p in elites[:15]:
        hft = "⚡" if p.hft else ""
        rf_tag = f" RF30d:${p.recent_pnl_30d:+.0f}" if p.recent_pnl_30d is not None else ""
        lines.append(
            f"  {hft}{p.name or (w[:10]+'…'):<22} "
            f"Score:{p.score:.2f}  WR:{p.win_rate*100:.0f}%  "
            f"PnL:${p.total_pnl:+,.0f}  TPH:{p.trades_per_hour:.1f}{rf_tag}"
        )
    lines += ["", "[LAST 15 LOGS]"]
    meaningful = [l for l in S.env().SYSTEM_LOGS[-40:]
                  if "dedup" not in l.lower() and "polling" not in l.lower()][-15:]
    lines.extend(f"  {l}" for l in meaningful)
    lines.append("═" * 70)
    return "\n".join(lines)
