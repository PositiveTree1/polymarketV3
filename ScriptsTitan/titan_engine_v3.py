"""
TITAN — Main orchestration engine.
"""

import time
import threading

from titan_config import *
import titan_state as _S
from titan_state import _log, safe_get

def __getattr__(name):
    return getattr(_S.env(), name)

from titan_persistence import load_state, save_state, save_whale_roster, save_whale_roster_async
from titan_wallet import fetch_wallet, get_elite_wallets, discover_new_whales, scan_top_market_holders, get_whale_performance_summary
from titan_market import get_market, fetch_trades
from titan_signals import build_signals, check_whale_exits, _adaptive_bet_caps
from titan_trader import auto_trade


def _rescore_watchlist():
    now_t = time.time()
    must  = set(w.lower() for w in VIP_WALLETS) | set(w.lower() for w in PRIORITY_WALLETS)
    stale = [
        w for w in list(_S.env().watchlist)
        if w not in must
        and (now_t - _S.env().wallet_cache.get(w, {}).get("ts", 0)) >= WALLET_TTL
    ]
    to_score = list(must) + stale
    if not to_score:
        return
    _log(f"♻ Re-scoring {len(to_score)} wallets…", "DATA")
    for w in to_score:
        try:
            fetch_wallet(w)
            time.sleep(0.15)
        except Exception:
            pass
    new_elite = sum(1 for w in _S.env().wallet_cache if _S.env().wallet_cache[w].get("elite"))
    _log(f"♻ Re-score done | {new_elite} elite total", "DATA")
    save_whale_roster_async()


def analyse(trades):
    _S.env().cycle_count += 1

    # We already have these on lines 54-59

    if _S.env().cycle_count % DISCOVERY_INTERVAL_CYCLES == 0 and _S.env().index == 0:
        threading.Thread(target=discover_new_whales, daemon=True).start()
    if _S.env().cycle_count % 5 == 0 and _S.env().index == 0:
        threading.Thread(target=scan_top_market_holders, daemon=True).start()
    if _S.env().cycle_count % 20 == 2 and _S.env().index == 0:
        threading.Thread(target=_rescore_watchlist, daemon=True).start()

    # Score all wallets seen in the feed
    feed_wallets = {t["wallet"] for t in trades}
    wallets      = {}
    ver_count    = 0
    elite_count  = 0

    for w in feed_wallets:
        p = fetch_wallet(w)

        # Preserve real names from trade records
        trade_name = next(
            (t["name"] for t in trades
             if t["wallet"].lower() == w and t["name"] and not t["name"].endswith("…")),
            None
        )
        def _is_auto(n):
            parts = n.split("-")
            if len(parts) != 2: return False
            a, b = parts
            return a and b and a[0].isupper() and b[0].isupper() and a.isalpha() and b.isalpha()

        if trade_name:
            current = p.get("name", "")
            current_real = current and not current.endswith("…") and not _is_auto(current)
            if not _is_auto(trade_name):
                p["name"] = trade_name
                _S.env().wallet_cache[w] = p
            elif not current_real:
                p["name"] = trade_name
                _S.env().wallet_cache[w] = p

        wallets[w] = p
        if p["verified"]:  ver_count   += 1
        if p["elite"]:     elite_count += 1
        time.sleep(0.04)

    if _S.env().cycle_count % 10 == 0:
        elite_ws = get_elite_wallets()
        if elite_ws:
            hft_count = sum(1 for w in elite_ws if _S.env().wallet_cache.get(w, {}).get("hft"))
            names = [_S.env().wallet_cache.get(w, {}).get("name", w[:10] + "…") for w in elite_ws[:8]]
            _log(f"🔥 Elite ({len(elite_ws)}, ⚡{hft_count} HFT): {', '.join(names)}", "INFO")
        else:
            _log("⚠ No elite wallets yet", "WARN")

    # Build whale exit monitoring input
    cid_to_wallet_sets = {cid: set(ws) for cid, ws in _S.env().position_whale_map.items()}
    entry_times = {
        pos.get("cid", key[0]): pos.get("entry_ts", 0)
        for key, pos in _S.env().open_positions.items()
    }
    whale_exits = {}
    if cid_to_wallet_sets:
        whale_exits = check_whale_exits(cid_to_wallet_sets, entry_times)

    signals, rejects = build_signals(trades, wallets, whale_exits)
    _log(
        f"🎯 {len(signals)} signals | {len(rejects)} rejects | "
        f"{ver_count} verified ({elite_count} elite) wallets",
        "INFO"
    )

    trade_events = auto_trade(signals, whale_exits)
    for ev_type, msg, _color in trade_events:
        level = "TRADE" if ev_type in ("OPEN", "CLOSE") else "WARN"
        _log(msg, level)

    # Sample portfolio equity every cycle for continuous P&L curve
    # equity = cash in hand + mark-to-market value of all open positions
    open_value = sum(
        pos.get("cur_price", pos.get("entry_price", 0)) * pos.get("shares", 0)
        for pos in _S.env().open_positions.values()
    )
    total_equity = _S.env().paper_bankroll + open_value
    _S.env().equity_history.append((time.time(), total_equity))
    # Keep last 5000 samples (~21h at 15s cycles)
    if len(_S.env().equity_history) > 5000:
        del _S.env().equity_history[:500]

    # v9: Whale performance report card every 50 cycles
    if _S.env().cycle_count % 50 == 0 and _S.env().cycle_count > 0:
        perf_summary = get_whale_performance_summary()
        if perf_summary:
            _log("\n📊 WHALE PERFORMANCE REPORT CARD:", "INFO")
            for rec in perf_summary[:10]:  # show worst-first
                emoji = "✅" if rec['total_pnl'] >= 0 else "❌"
                _log(
                    f"  {emoji} {rec['name']:<18} "
                    f"{rec['wins']}W/{rec['losses']}L "
                    f"WR:{rec['win_rate']*100:.0f}% "
                    f"PnL:${rec['total_pnl']:+.4f} "
                    f"Avg:${rec['avg_pnl']:+.4f}",
                    "INFO"
                )

    save_whale_roster_async()

    if _S.on_cycle_complete:
        _S.on_cycle_complete(signals, wallets, rejects, trades, getattr(_S._local, "engine_idx", _S.active_idx))


def run_loop():
    while True:
        try:
            trades = fetch_trades()
            if not trades:
                _log("⚠ No trades from any source", "WARN")
                trades = []
                
            orig_idx = getattr(_S._local, "engine_idx", _S.active_idx)
            for i in range(10):
                _S._local.engine_idx = i
                analyse(trades)
            _S._local.engine_idx = orig_idx
        except Exception as e:
            import traceback
            _log(f"Cycle error: {e}\n{traceback.format_exc()[:400]}", "ERR")
        time.sleep(CYCLE_SECONDS)


def start(log_callback=None, position_open_cb=None, position_close_cb=None, cycle_cb=None):
    _S.on_log            = log_callback
    _S.on_position_open  = position_open_cb
    _S.on_position_close = position_close_cb
    _S.on_cycle_complete = cycle_cb

    load_state()
    _log("🚀 TITAN v9 — Whale Mirror Engine (OVERHAUL)", "INFO")
    _log("   Quality > Quantity | Portfolio-relative conviction | EV gating", "INFO")
    max_abs, max_pct = _adaptive_bet_caps()
    _log(
        f"   Bankroll: ${_S.env().paper_bankroll:.2f}  MaxBet: {max_pct*100:.0f}% / ${max_abs:.2f}  "
        f"MaxPos: {MAX_OPEN_POSITIONS}",
        "INFO"
    )
    _log(
        f"   Elite gate: PnL≥${ELITE_MIN_PNL:,.0f}  Port≥${ELITE_MIN_PORT:,.0f}  "
        f"Score≥{ELITE_MIN_SCORE}  Res≥{ELITE_MIN_RESOLVED}",
        "INFO"
    )
    _log(
        f"   v9 gates: Liq≥${MIN_LIQUIDITY:,.0f}  Vol≥${MIN_VOLUME:,.0f}  "
        f"MaxAge≤{MAX_SIGNAL_AGE_H}h  Slippage≤{MAX_ENTRY_SLIPPAGE*100:.0f}%  Drift≤{MAX_DRIFT*100:.0f}%",
        "INFO"
    )
    _log(
        f"   StopLoss: {STOP_LOSS_PCT*100:.0f}%  ProfitTarget: {PROFIT_TARGET_PCT*100:.0f}%  "
        f"TrailingStop: activates at +15%",
        "INFO"
    )
    _log("─" * 60, "DATA")

    t = threading.Thread(target=run_loop, daemon=True)
    t.start()
    return t


def get_system_snapshot() -> str:
    from datetime import datetime as _dt
    now = _dt.now().strftime("%Y-%m-%d %H:%M:%S")

    sells     = [t for t in _S.env().trade_history if t.get("type") == "SELL"]
    wins      = [t for t in sells if (t.get("pnl_usdc") or 0) >= 0]
    total_pnl = _S.env().paper_bankroll - BANKROLL_START
    win_rate  = (len(wins) / len(sells) * 100) if sells else 0

    lines = [
        "═" * 70,
        f"  TITAN SNAPSHOT — {now}",
        "═" * 70,
        "",
        f"Bankroll  : ${_S.env().paper_bankroll:.2f}  (start ${BANKROLL_START:.2f})",
        f"Total PnL : ${total_pnl:+.4f}",
        f"Win Rate  : {win_rate:.1f}%  ({len(wins)}W/{len(sells)-len(wins)}L)",
        f"Open      : {len(_S.env().open_positions)}  Cycle: {_S.env().cycle_count}",
        "",
        "[OPEN POSITIONS]",
    ]

    for key, pos in _S.env().open_positions.items():
        try:
            cid, outcome = (key if isinstance(key, tuple)
                            else str(key).split("|||", 1) + ["?"])
        except Exception:
            cid, outcome = str(key), "?"
        entry = pos.get("entry_price", 0)
        cur   = pos.get("cur_price", entry)
        pnl   = (cur - entry) / max(entry, 0.001) * 100
        held  = (time.time() - pos.get("entry_ts", time.time())) / 60
        hft   = "⚡" if pos.get("is_hft") else ""
        conv  = "💎" if pos.get("is_conviction") else ""
        lines.append(
            f"  {conv}{hft}[{pos.get('tier','?')}] {pos.get('title','?')[:46]} / {outcome}"
            f"  P&L:{pnl:+.1f}%  Held:{held:.0f}min  ${pos.get('bet',0):.2f}"
            f"  Entry:${entry:.4f} Cur:${cur:.4f}"
        )

    lines += ["", "[ELITE ROSTER]"]
    elites = sorted(
        [(w, p) for w, p in _S.env().wallet_cache.items() if p.get("elite")],
        key=lambda x: x[1].get("total_pnl", 0), reverse=True
    )
    for w, p in elites[:15]:
        hft = "⚡" if p.get("hft") else ""
        lines.append(
            f"  {hft}{p.get('name', w[:10]+'…'):<22} "
            f"Score:{p.get('score',0):.2f}  WR:{p.get('win_rate',0)*100:.0f}%  "
            f"PnL:${p.get('total_pnl',0):+,.0f}  TPH:{p.get('trades_per_hour',0):.1f}"
        )

    lines += ["", "[RECENT SIGNALS]"]
    for s in _S.env().LAST_SIGNALS[:5]:
        hft  = "⚡HFT " if s.get("is_hft") else ""
        conv = "💎 " if s.get("has_large_trade") else ""
        mkt_type = s.get("mkt_type", "?")
        ev_info = s.get("ev_info", {})
        ev_str = f"EV:{ev_info.get('ev_pct', 0):+.1f}%" if ev_info else ""
        lines.append(
            f"  {conv}{hft}[{s.get('tier')}|{mkt_type}] {s.get('title','')[:36]} [{s.get('outcome')}] "
            f"Score:{s.get('score',0):.0f}  Slip:{s.get('slippage',0)*100:+.1f}%  {ev_str}"
        )

    # v9: Whale performance report card
    perf_summary = get_whale_performance_summary()
    if perf_summary:
        lines += ["", "[WHALE REPORT CARD]"]
        for rec in perf_summary[:8]:
            emoji = "✅" if rec['total_pnl'] >= 0 else "❌"
            lines.append(
                f"  {emoji} {rec['name']:<18} "
                f"{rec['wins']}W/{rec['losses']}L  "
                f"WR:{rec['win_rate']*100:.0f}%  "
                f"PnL:${rec['total_pnl']:+.4f}"
            )

    lines += ["", "[LAST 15 LOGS]"]
    meaningful = [l for l in _S.env().SYSTEM_LOGS[-40:]
                  if "dedup" not in l.lower() and "polling" not in l.lower()][-15:]
    lines.extend(f"  {l}" for l in meaningful)

    lines.append("═" * 70)
    return "\n".join(lines)