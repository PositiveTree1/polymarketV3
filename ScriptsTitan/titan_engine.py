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
from titan_persistence import load_state, save_state, save_wallet_roster, save_wallet_roster_async
from titan_wallet  import (get_compute_and_store_wallet, get_elite_wallets, discover_new_wallets,
                           scan_top_market_holders, get_wallet_performance_summary,
                           _refresh_recent_form_scores)
from titan_market  import get_market, fetch_trades, fetch_hft_spike_trades
from titan_signals import build_signals, check_wallet_exist, _adaptive_bet_caps
from titan_trader  import auto_trade

_HFT_FAST_CYCLE = 3  # seconds between HFT polls


def _rescore_watchlist():
    now_t = time.time()
    must  = set(w.lower() for w in VIP_WALLETS) | set(w.lower() for w in PRIORITY_WALLETS)
    stale = [
        w for w in S.get_watchlist()
        if w not in must
        and (now_t - S.env().wallet_cache.get(w, {}).get("ts", 0)) >= WALLET_TTL
    ]
    to_score = list(must) + stale
    if not to_score:
        return
    _log(f"♻ Re-scoring {len(to_score)} wallets…", "DATA")
    for w in to_score:
        try:
            get_compute_and_store_wallet(w)
            time.sleep(0.15)
        except Exception as e:
            _log(f"Re-score failed for {w}: {e}", "ERR")
    new_elite = sum(1 for w in S.env().wallet_cache if S.env().wallet_cache[w].get("elite"))
    _log(f"♻ Re-score done | {new_elite} elite total", "DATA")
    save_wallet_roster_async()


def analyse(trades: list, is_hft_loop: bool = False) -> None:
    S.env().cycle_count += 1

    if S.env().cycle_count % DISCOVERY_INTERVAL_CYCLES == 0:
        threading.Thread(target=discover_new_wallets, daemon=True).start()
    if S.env().cycle_count % 5 == 0:
        threading.Thread(target=scan_top_market_holders, daemon=True).start()
    if S.env().cycle_count % 20 == 2:
        threading.Thread(target=_rescore_watchlist, daemon=True).start()

    # v10: Refresh recent form scores (6h TTL) for Recent Form strategy
    if S.env().cycle_count % 50 == 3:
        threading.Thread(target=_refresh_recent_form_scores, daemon=True).start()

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

    for w in feed_wallets:
        p = get_compute_and_store_wallet(w)
        trade_name = next(
            (t.name for t in trades
             if t.wallet.lower() == w and t.name and not t.name.endswith("…")),
            None
        )
        if trade_name:
            current = p.get("name", "")
            current_real = current and not current.endswith("…") and not _is_auto(current)
            if not _is_auto(trade_name) or not current_real:
                p["name"] = trade_name
                S.env().wallet_cache[w] = p

        cached = S.env().wallet_cache.get(w)
        if cached:
            if not p["recent_pnl_30d"] and cached["recent_pnl_30d"]:
                p["recent_pnl_30d"] = cached["recent_pnl_30d"]
            if not p["recent_pnl_7d"] and cached["recent_pnl_7d"]:
                p["recent_pnl_7d"] = cached["recent_pnl_7d"]
            if not p["recent_ts"] and cached["recent_ts"]:
                p["recent_ts"] = cached["recent_ts"]

        wallets[w] = p
        if p["verified"]:  ver_count  += 1
        if p["elite"]:     elite_count += 1
        time.sleep(0.04)

    # CRITICAL FIX: Inject all known elite/verified wallets from cache into the
    # wallets dict. Without this, HFT fast loop cycles only see 1-3 wallets
    # (just those in the spike batch), so build_signals always gets "0 elite"
    # even when the spiking wallet IS elite and well-known.
    # We don't re-fetch them (too slow) — just pass the cached profile.
    for w, cached_profile in S.env().wallet_cache.items():
        if w not in wallets and (cached_profile.get("elite") or cached_profile.get("verified")):
            wallets[w] = cached_profile
            if cached_profile.get("verified"): ver_count  += 1
            if cached_profile.get("elite"):    elite_count += 1

    if S.env().cycle_count % 10 == 0:
        elite_ws = get_elite_wallets()
        if elite_ws:
            hft_count = sum(1 for w in elite_ws if S.env().wallet_cache.get(w, {}).get("hft"))
            names = [S.env().wallet_cache.get(w, {}).get("name", w[:10]+"…") for w in elite_ws[:8]]
            _log(f"🔥 Elite ({len(elite_ws)}, ⚡{hft_count} HFT): {', '.join(names)}", "INFO")

    # Wallet exit monitoring
    cid_to_wallet_sets = {cid: set(ws) for cid, ws in S.env().position_wallet_map.items()}
    entry_times = {
        (pos.cid or key[0]): pos.entry_ts
        for key, pos in S.env().open_positions.items()
    }
    wallet_exits = {}
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
    """
    Runs every 3 seconds. Polls only HFT wallets looking for outsized spike trades.
    """
    _log("⚡ HFT fast loop started (3s cycle)", "INFO")
    _log("⚡ HFT fast loop waiting 45s for wallet_cache to populate…", "INFO")
    time.sleep(45)
    _log("⚡ HFT fast loop active", "INFO")

    _no_hft_warned = False
    while True:
        try:
            hft_count = sum(
                1 for addr, prof in S.env().wallet_cache.items()
                if prof.get("hft") or prof.get("trades_per_hour", 0) >= HFT_MIN_TRADES_PER_HOUR
            )
            if hft_count == 0:
                if not _no_hft_warned:
                    _log("⚡ HFT fast loop: no HFT wallets yet — waiting for discovery", "WARN")
                    _no_hft_warned = True
                time.sleep(_HFT_FAST_CYCLE)
                continue
            _no_hft_warned = False

            spike_trades = fetch_hft_spike_trades()
            if spike_trades:
                _log(f"⚡ HFT fast loop: processing {len(spike_trades)} spike(s)…", "DIAG")
                C.reload()
                analyse(spike_trades, is_hft_loop=True)
        except Exception as e:
            import traceback
            _log(f"HFT fast loop error: {e}\n{traceback.format_exc()[:300]}", "ERR")
        time.sleep(_HFT_FAST_CYCLE)


def start(log_callback=None, position_open_cb=None, position_close_cb=None, cycle_cb=None, heartbeat_cb=None):
    S.on_log            = log_callback
    S.on_position_open  = position_open_cb
    S.on_position_close = position_close_cb
    S.on_cycle_complete = cycle_cb
    S.on_heartbeat      = heartbeat_cb

    load_state()
    C.reload()

    _log("🚀 TITAN v10 — Multi-Strategy tracked wallet Mirror Engine", "INFO")

    # v10: Print active strategies and their key params
    active = getattr(C, "ACTIVE_STRATEGIES", [])
    _log(f"   Active strategies: {', '.join(active)}", "INFO")
    for strat in active:
        cfg = getattr(C, f"strategy_{strat}", {})
        if not cfg:
            continue
        if strat == "recent_form":
            _log(
                f"   [{strat}] min_pnl_30d=${cfg.get('min_pnl_30d',0):+.0f} "
                f"max_tph={cfg.get('max_tph',20)} price=[{cfg.get('price_min',0.18):.2f},{cfg.get('price_max',0.78):.2f}] "
                f"min_score={cfg.get('min_score',42)} max_age={cfg.get('max_signal_age_h',0.75)}h",
                "INFO"
            )
        elif strat == "drift_discount":
            _log(
                f"   [{strat}] discount=[{cfg.get('min_discount_pct',0.04)*100:.0f}%,{cfg.get('max_discount_pct',0.12)*100:.0f}%] "
                f"max_age={cfg.get('max_signal_age_h',6.0)}h "
                f"price=[{cfg.get('price_min',0.20):.2f},{cfg.get('price_max',0.72):.2f}]",
                "INFO"
            )
        elif strat == "consensus_basket":
            _log(
                f"   [{strat}] min_elite={cfg.get('min_elite_confluence',1)} "
                f"max_bet=${cfg.get('max_bet_abs',1.20):.2f} "
                f"stop={cfg.get('stop_loss_pct',None)} "
                f"price=[{cfg.get('price_min',0.20):.2f},{cfg.get('price_max',0.72):.2f}]",
                "INFO"
            )

    max_abs, max_pct = _adaptive_bet_caps()
    _log(
        f"   Bankroll: ${S.env().paper_bankroll:.2f}  MaxBet: {max_pct*100:.0f}% / ${max_abs:.2f}  "
        f"MaxPos: {MAX_OPEN_POSITIONS}",
        "INFO"
    )
    _log(
        f"   Elite gate: PnL≥${ELITE_MIN_PNL:,.0f}  Port≥${ELITE_MIN_PORT:,.0f}  "
        f"Score≥{ELITE_MIN_SCORE}  Res≥{ELITE_MIN_RESOLVED}",
        "INFO"
    )
    _log(
        f"   Price zone: [{MIN_ENTRY_PRICE:.2f}, {MAX_ENTRY_PRICE:.2f}]  "
        f"Ideal: [{IDEAL_PRICE_MIN:.2f}, {IDEAL_PRICE_MAX:.2f}]",
        "INFO"
    )
    _log(
        f"   StopLoss: {'ON' if STOP_LOSS_ENABLED else 'OFF (wallet-exit only)'}  "
        f"ProfitTarget: {PROFIT_TARGET_PCT*100:.0f}%  WalletExitSell: {WALLET_EXIT_SELL}",
        "INFO"
    )
    _log("─" * 60, "DATA")

    # Start WebSocket resolution monitor
    try:
        import titan_resolution_monitor as _rm
        _rm.start()
        _rm.sync_open_positions()
    except Exception as _e:
        _log(f"⚠ WS resolution monitor failed to start: {_e}", "WARN")

    t_main = threading.Thread(target=run_loop, daemon=True)
    t_hft  = threading.Thread(target=_hft_fast_loop, daemon=True)
    t_hb   = threading.Thread(target=_heartbeat_loop, daemon=True)
    t_main.start()
    t_hft.start()
    t_hb.start()
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
        [(w, p) for w, p in S.env().wallet_cache.items() if p.get("elite")],
        key=lambda x: x[1].get("total_pnl", 0), reverse=True
    )
    for w, p in elites[:15]:
        hft = "⚡" if p.get("hft") else ""
        rf_tag = f" RF30d:${p.get('recent_pnl_30d',0):+.0f}" if p.get("recent_pnl_30d") is not None else ""
        lines.append(
            f"  {hft}{p.get('name', w[:10]+'…'):<22} "
            f"Score:{p.get('score',0):.2f}  WR:{p.get('win_rate',0)*100:.0f}%  "
            f"PnL:${p.get('total_pnl',0):+,.0f}  TPH:{p.get('trades_per_hour',0):.1f}{rf_tag}"
        )
    lines += ["", "[LAST 15 LOGS]"]
    meaningful = [l for l in S.env().SYSTEM_LOGS[-40:]
                  if "dedup" not in l.lower() and "polling" not in l.lower()][-15:]
    lines.extend(f"  {l}" for l in meaningful)
    lines.append("═" * 70)
    return "\n".join(lines)