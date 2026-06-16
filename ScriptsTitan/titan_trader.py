"""
TITAN — Auto paper trading. Single-wallet edition. v10.

EXIT PHILOSOPHY — FOLLOW THE WHALE (UPDATED v10):
  1. WALLET_EXIT_SELL=true  → exit immediately when the triggering elite sells
  2. PROFIT_TARGET_PCT     → take profit even if whale still holds (optional guard)
  3. Per-signal STOP LOSS  → each signal carries stop_loss_pct from its strategy:
       recent_form:      None (no stop loss — price ceiling is protection)
       drift_discount:   None (entered at discount — no stop needed)
       consensus_basket: -0.35 (soft stop at -35%)
       Global fallback:  STOP_LOSS_PCT (-0.30) if strategy has no config
  4. Market resolving       → always exit (it's done)
  5. Market expiring        → exit when < MIN_HOURS_LEFT
  6. Catastrophic loss guard → always exit at -70% regardless of strategy

v10 CHANGES:
  - Position dict now stores 'strategy' field from signal
  - Stop loss uses per-position stop_loss_pct (set at entry from signal)
  - Open position log shows strategy tag [RF|DD|CB]
  - Trade history BUY record includes strategy field
"""

import time
from datetime import datetime
import titan_state as S
from titan_config import *
import titan_config as C
from titan_position import Position, get_effective_stop_loss
from titan_market  import get_market, get_outcome_price, is_market_resolving, Market
from titan_prices  import PRICES
from titan_signals import estimate_expected_value, _KNOWN_HEDGE_WALLETS, Signal
from titan_wallet  import record_whale_trade_performance
from titan_persistence import save_state, save_wallet_roster_async
from titan_trade import TradeRecord
import titan_db as DB


# Optional: resolution monitor (lazy import to avoid circular)
def _get_ws_monitor():
    try:
        import titan_resolution_monitor as _rm
        return _rm
    except ImportError:
        return None


def _try_fetch_resolution_price(cid: str, asset: str, outcome: str) -> float | None:
    """
    FIX: Phantom profit prevention + stale-price resolution detection.
    When Gamma API is down (422 flood), we use the Data API which continues
    working even after market resolution, independent of Gamma API.

    Returns the current outcome price (near 0 or 1 if resolved), or None if unknown.
    """
    try:
        if asset:
            from titan_market import Market
            p = Market.fetch_price_for_asset(asset)
            if p is not None and (p >= 0.97 or p <= 0.03):
                S._log(f"  📡 Asset price confirmed: {asset[:20]} = {p:.4f}", "DIAG")
                return p

        data = S.safe_get(f"{C.DATA_API}/trades", {
            "conditionId": cid,
            "limit": 10,
        })
        if data and isinstance(data, list):
            our_lower = outcome.lower().strip()
            for t in data:
                price = float(t.get("price") or 0)
                t_outcome = (t.get("outcome") or "").lower().strip()
                t_asset = t.get("asset") or ""
                if price <= 0 or price >= 1:
                    continue
                is_our_token = (asset and t_asset == asset)
                is_our_label = (our_lower and t_outcome == our_lower)
                if (is_our_token or is_our_label) and (price >= 0.97 or price <= 0.03):
                    return price


    except Exception as e:
        S._log(f"  ⚠ Resolution price fetch failed: {e}", "DIAG")
    return None


def _get_current_price(pos: Position) -> tuple:
    """
    Two-stage price fetch for open positions.
    Returns (price, is_resolving, fetched_ts) where fetched_ts is time.time() when
    a live price was received from Polymarket, or 0.0 when falling back to stale.
    """
    from titan_market import fetch_position_price_fast
    cid     = pos.cid
    outcome = pos.outcome
    asset   = pos.asset
    title   = pos.title
    stale_price = pos.cur_price or pos.entry_price or 0.5

    fast_price = fetch_position_price_fast(cid, asset, outcome)
    if fast_price is not None:
        resolving = fast_price <= 0.03 or fast_price >= 0.97
        pos.market_fail_count = 0
        return fast_price, resolving, time.time()

    cached = S.market_cache.get(cid)
    if cached and (time.time() - cached.ts) > C.MARKET_TTL:
        S.market_cache.pop(cid, None)

    mkt, err = get_market(cid, title, asset=asset, slug=pos.slug, event_slug=pos.event_slug, persist=True)
    if not mkt:
        pos.market_fail_count += 1
        return stale_price, False, 0.0

    pos.market_fail_count = 0
    resolving = is_market_resolving(mkt)

    if asset:
        ap = mkt.asset_to_price
        if asset in ap:
            price = ap[asset]
            if price <= 0.03: return price, True, time.time()
            if price >= 0.97: return price, True, time.time()
            return price, resolving, time.time()

    cur = get_outcome_price(mkt, outcome, asset=asset)
    return cur, resolving, time.time()



def _dt_fields(ts: float) -> dict:
    dt = datetime.fromtimestamp(ts)
    return {
        "ts": ts,
        "ts_str": dt.strftime("%Y-%m-%d %H:%M:%S"),
        "ts_iso": dt.isoformat(timespec="seconds"),
        "date": dt.strftime("%Y-%m-%d"),
        "time": dt.strftime("%H:%M:%S"),
    }


def _with_latest_price_point(points: list[tuple[float, float]], now_t: float, price: float) -> list[tuple[float, float]]:
    if not points:
        return [(now_t, price)]
    last_ts, last_price = points[-1]
    if abs(last_ts - now_t) < 0.5:
        points[-1] = (now_t, price)
        return points
    if last_price != price:
        points.append((now_t, price))
    return points



def _build_market_url(event_slug: str = "", slug: str = "") -> str:
    if event_slug:
        return f"https://polymarket.com/event/{event_slug}"
    if slug:
        return f"https://polymarket.com/event/{slug}"
    return "https://polymarket.com"


def _compact_market_snapshot(mkt: Market | None, *, fallback_title: str = "", fallback_slug: str = "",
                             fallback_event_slug: str = "") -> dict:
    if mkt is None:
        return {
            "title": fallback_title, "slug": fallback_slug, "event_slug": fallback_event_slug,
            "yes_price": 0.5, "no_price": 0.5, "outcome_labels": [], "outcome_prices": {},
            "asset_to_price": {}, "liq": None, "volume": None, "hrs_left": None,
            "end_date": None, "mkt_type": "", "is_sports": False, "ts": None,
        }
    return {
        "title": mkt.title or fallback_title,
        "slug": mkt.slug or fallback_slug,
        "event_slug": mkt.event_slug or fallback_event_slug,
        "yes_price": mkt.yes_price,
        "no_price": mkt.no_price,
        "outcome_labels": list(mkt.outcome_labels),
        "outcome_prices": dict(mkt.outcome_prices),
        "asset_to_price": dict(mkt.asset_to_price),
        "liq": mkt.liq,
        "volume": mkt.volume,
        "hrs_left": mkt.hrs_left,
        "end_date": mkt.end_date,
        "mkt_type": mkt.mkt_type,
        "is_sports": mkt.is_sports,
        "ts": mkt.ts,
    }


def _compact_wallet_snapshot(wallet_addrs: list[str]) -> list[dict]:
    rows = []
    for addr in wallet_addrs[:8]:
        prof = S.env().wallet_cache.get(str(addr).lower())
        rows.append({
            "wallet": addr,
            "name":      prof.name      if prof is not None else str(addr)[:10] + "…",
            "score":     prof.score     if prof is not None else None,
            "win_rate":  prof.win_rate  if prof is not None else None,
            "total_pnl": prof.total_pnl if prof is not None else None,
            "verified":  prof.verified  if prof is not None else None,
            "elite":     prof.elite     if prof is not None else None,
            "hft":       prof.hft       if prof is not None else None,
        })
    return rows


def _compact_elite_trade_snapshot(elite_ver: dict) -> list[dict]:
    rows = []
    for addr, trade in list((elite_ver or {}).items())[:8]:
        rows.append({
            "wallet": addr,
            "name": trade.name,
            "title": trade.title,
            "outcome": trade.outcome,
            "price": trade.price,
            "size": trade.size,
            "cash": trade.cash,
            "asset": trade.asset,
            "slug": trade.slug,
            "event_slug": trade.event_slug,
            "ts": trade.ts,
            "source": trade.source,
            "window": trade.window,
        })
    return rows


def _build_entry_audit(sig: Signal, cur: float, shares: float, bet: float, ev_info: dict,
                       now_t: float, bankroll_after: float) -> dict:
    dtf = _dt_fields(now_t)
    market = sig.mkt
    event_slug = sig.event_slug or market.event_slug
    slug = market.slug or sig.slug
    http_traces = _collect_action_http_traces(
        since_ts=max(0.0, now_t - 60),
        cid=sig.cid,
        asset=sig.asset,
        slug=slug,
        event_slug=event_slug,
        limit=12,
    )
    return {
        "captured_at": dtf,
        "market_url": _build_market_url(event_slug, slug),
        "signal_snapshot": {
            "cid": sig.cid,
            "title": sig.title,
            "outcome": sig.outcome,
            "asset": sig.asset,
            "strategy": sig.strategy,
            "tier": sig.tier,
            "score": sig.score,
            "score_breakdown": dict(sig.bd),
            "stop_loss_pct": sig.stop_loss_pct,
            "age_h": sig.age_h,
            "age_min": sig.age_min,
            "cur": cur,
            "avg_entry": sig.avg_entry,
            "drift": sig.drift,
            "slippage": sig.slippage,
            "total_flow": sig.total_flow,
            "ver_flow": sig.ver_flow,
            "opposing_flow": sig.opposing_flow,
            "n_ver": sig.n_ver,
            "n_elite": sig.n_elite,
            "n_confluence": sig.n_confluence,
            "max_bet_cash": sig.max_bet_cash,
            "is_hft": sig.is_hft,
            "has_large_trade": sig.has_large_trade,
            "is_conviction": sig.has_large_trade,
            "conviction_detail": sig.conviction_detail,
            "exits_detected": list(sig.exits_detected),
            "elite_wallets": list(sig.elite_ver.keys()),
            "wallet_names": list(sig.names),
            "elite_trades": _compact_elite_trade_snapshot(sig.elite_ver),
        },
        "market_snapshot": _compact_market_snapshot(
            market,
            fallback_title=sig.title,
            fallback_slug=slug,
            fallback_event_slug=event_slug,
        ),
        "wallet_snapshot": _compact_wallet_snapshot(list(sig.elite_ver.keys())),
        "pricing_snapshot": {
            "entry_price": cur,
            "shares": shares,
            "bet": bet,
            "fee_rate": TAKER_FEE_RATE,
            "avg_wallet_entry": sig.avg_entry,
            "bankroll_after_buy": round(bankroll_after, 4),
        },
        "ev_snapshot": dict(ev_info or {}),
        "http_traces": http_traces,
        "decision_summary": (
            f"BUY {sig.outcome} via {sig.strategy} "
            f"[{sig.tier}] score={sig.score:.0f} "
            f"cur={cur:.4f} whale_avg={sig.avg_entry:.4f}"
        ),
    }


def _build_exit_audit(pos: Position, cur: float, pnl_pct: float, reason: str, now_t: float,
                      exit_proceeds: float, pnl_usdc_net: float) -> dict:
    dtf = _dt_fields(now_t)
    hold_minutes = (now_t - pos.entry_ts) / 60
    http_traces = _collect_action_http_traces(
        since_ts=max(0.0, now_t - 30),
        cid=pos.cid,
        asset=pos.asset,
        slug=pos.slug,
        event_slug=pos.event_slug,
        limit=12,
    )
    return {
        "captured_at": dtf,
        "market_url": pos.market_url or _build_market_url(pos.event_slug, pos.slug),
        "exit_reason": reason,
        "hold_minutes": round(hold_minutes, 2),
        "pricing_snapshot": {
            "entry_price": pos.entry_price,
            "exit_price": cur,
            "shares": pos.shares,
            "bet": pos.bet,
            "gross_exit_value": round(cur * pos.shares, 6),
            "net_exit_proceeds": round(exit_proceeds, 6),
            "fee_rate": TAKER_FEE_RATE,
            "pnl_usdc": round(pnl_usdc_net, 4),
            "pnl_pct": round(pnl_pct * 100, 2),
            "peak_pnl_pct": pos.peak_pnl_pct,
        },
        "market_snapshot": {
            "liq": pos.liq,
            "volume": pos.volume,
            "hrs_left": pos.hrs_left,
            "end_date": pos.end_date,
            "cur_price": pos.cur_price or cur,
            "market_fail_count": pos.market_fail_count,
        },
        "wallet_snapshot": _compact_wallet_snapshot(pos.elite_wallets),
        "price_history_tail": list(pos.price_history[-120:]),
        "http_traces": http_traces,
        "decision_summary": (
            f"SELL {pos.outcome} via {pos.strategy} "
            f"reason={reason} exit={cur:.4f} pnl={pnl_pct*100:+.1f}%"
        ),
    }


def _collect_action_http_traces(*, since_ts: float, cid: str = "", asset: str = "",
                                slug: str = "", event_slug: str = "", limit: int = 12) -> list[dict]:
    filters = [cid, asset, slug, event_slug, "/markets", "/trades", "/activity", "/positions"]
    traces = S.get_recent_http_traces(since_ts=since_ts, limit=limit, filters=filters)
    compact = []
    for t in traces:
        compact.append({
            "ts": t.get("ts"),
            "url": t.get("url"),
            "params": t.get("params"),
            "status_code": t.get("status_code"),
            "ok": t.get("ok"),
            "caller": t.get("caller"),
            "body": t.get("body"),
        })
    return compact

# Returns UI/log event tuples as (level, message, color).
def auto_trade(signals: list[Signal], wallet_exits: dict) -> list[tuple[str, str, str]]:
    now_t = time.time()

    # Expire stale cooldowns
    S.env().cooldown_cids = {
        cid: ts for cid, ts in S.env().cooldown_cids.items()
        if now_t - ts < EXIT_COOLDOWN_SECONDS
    }

    events   = []
    to_close = []

    # ─────────────────────────────────────────────────────────────────────────
    # STEP 1: Check exits on open positions
    # ─────────────────────────────────────────────────────────────────────────
    for key, pos in list(S.env().open_positions.items()):
        cid      = pos.cid or key[0]
        entry    = pos.entry_price
        bet      = pos.bet
        shares   = pos.shares
        entry_ts = pos.entry_ts

        hold_minutes = (now_t - entry_ts) / 60
        if hold_minutes < C.MIN_HOLD_MINUTES:
            continue

        cur, resolving, fetched_ts = _get_current_price(pos)
        if fetched_ts:
            pos.cur_price = cur
            pos.cur_price_ts = fetched_ts

        pos.price_history.append((now_t, cur))
        if len(pos.price_history) > 1440:
            del pos.price_history[:-1440]
        PRICES.add_point(pos.asset, now_t, cur)

        pnl_pct = (cur - entry) / max(entry, 0.001)
        reason  = None

        # (a0) WebSocket resolution — HIGHEST PRIORITY
        _ws = _get_ws_monitor()
        if _ws:
            ws_res = _ws.is_resolved_via_ws(cid)
            if ws_res:
                ws_price = ws_res.get("price", cur)
                cur = ws_price
                pos.cur_price = cur
                pos.cur_price_ts = now_t
                pnl_pct = (cur - entry) / max(entry, 0.001)
                reason = f"WS_RESOLVED price={cur:.4f} via={ws_res.get('event_type','?')}"
                S._log(
                    f"  📡 WS exit triggered: {pos.title[:35]} "
                    f"@ {cur:.4f} P&L={pnl_pct*100:+.1f}%",
                    "INFO"
                )

        # (a) Market resolving — always exit
        if not reason and resolving:
            reason = f"MARKET_RESOLVING cur={cur:.3f}"

        # (b) WHALE EXIT — follow the whale
        elif C.WALLET_EXIT_SELL and wallet_exits.get(cid):
            exiting         = set(wallet_exits[cid])
            elite_entry_set = set(w.lower() for w in pos.elite_wallets)
            matched_elite   = list(exiting & elite_entry_set)

            if matched_elite:
                non_hft_exiting = [
                    w for w in matched_elite
                    if w not in _KNOWN_HEDGE_WALLETS
                    and not ((_wp := S.env().wallet_cache.get(w)) and _wp.is_hft())
                ]
                if non_hft_exiting:
                    whale_losing = cur < pos.entry_price * 0.92
                    early_noise  = whale_losing and pnl_pct > -0.05 and hold_minutes < 20
                    if not early_noise:
                        matched_names = [
                            (p.name if (p := S.env().wallet_cache.get(a)) else None) or a[:10]+"…"
                            for a in non_hft_exiting[:2]
                        ]
                        reason = f"WHALE_SOLD {matched_names}"
                    else:
                        S._log(
                            f"  🐋 Early noise exit ignored: {pos.title[:30]} "
                            f"pnl={pnl_pct*100:+.1f}% hold={hold_minutes:.0f}min",
                            "DIAG"
                        )
                else:
                    hft_names = [(p.name if (p := S.env().wallet_cache.get(w)) else None) or w[:10] for w in matched_elite[:2]]
                    S._log(f"  ⚡ HFT/market-maker exit ignored: {hft_names} on {pos.title[:30]}", "DIAG")

        # (c) PROFIT TARGET
        elif not reason and getattr(C, "PROFIT_TARGET_ENABLED", True) and pnl_pct >= C.PROFIT_TARGET_PCT:
            reason = f"PROFIT_TARGET {pnl_pct*100:.1f}%"
            S._log(
                f"  💰 Profit target: {pos.title[:35]} "
                f"P&L={pnl_pct*100:+.1f}% (target={C.PROFIT_TARGET_PCT*100:.0f}%)",
                "INFO"
            )

        # (d) v10 PER-SIGNAL STOP LOSS
        # Uses strategy-specific stop_loss_pct set at entry time.
        # recent_form and drift_discount have stop_loss_pct=None → no stop
        # consensus_basket has stop_loss_pct=-0.35 → soft -35% stop
        # Global STOP_LOSS_PCT is used if per-signal has no config
        if not reason:
            eff_sl = get_effective_stop_loss(pos)
            if eff_sl is not None and pnl_pct <= eff_sl:
                strat_tag = pos.strategy[:2].upper()
                reason = f"STOP_LOSS[{strat_tag}] {pnl_pct*100:.1f}% (limit={eff_sl*100:.0f}%)"
                S._log(
                    f"  🛑 Stop loss [{strat_tag}]: {pos.title[:35]} "
                    f"P&L={pnl_pct*100:+.1f}% (limit={eff_sl*100:.0f}%)",
                    "INFO"
                )

        # (e) Expiring soon
        if not reason:
            mkt_check, _ = get_market(cid, pos.title, asset=pos.asset, slug=pos.slug, event_slug=pos.event_slug, persist=True)
            if mkt_check:
                pos.market_fail_count = 0
                hrs = mkt_check.hrs_left
                if hrs is not None and hrs < max(MIN_HOURS_LEFT, 0.35):
                    reason = "EXPIRING_SOON"
            else:
                pos.market_fail_count += 1
                fail_count = pos.market_fail_count
                if fail_count >= 3:
                    real_p = _try_fetch_resolution_price(cid, pos.asset, pos.outcome)
                    if real_p is not None:
                        cur = real_p
                        pos.cur_price = cur
                        pos.cur_price_ts = now_t
                        pnl_pct = (cur - entry) / max(entry, 0.001)
                        if real_p <= 0.03 or real_p >= 0.97:
                            reason = f"MARKET_RESOLVED_CONFIRMED cur={cur:.3f}"
                            S._log(
                                f"  📡 Fast resolution check: {pos.title[:35]} @ ${cur:.4f}",
                                "INFO"
                            )
                if fail_count >= 10:
                    stale_p = pos.cur_price
                    if stale_p <= 0.05 or stale_p >= 0.95:
                        reason = "MARKET_RESOLVED_OR_GONE"
                    elif fail_count >= 20:
                        real_exit_price = _try_fetch_resolution_price(cid, pos.asset, pos.outcome)
                        if real_exit_price is not None:
                            cur = real_exit_price
                            pos.cur_price = cur
                            pnl_pct = (cur - entry) / max(entry, 0.001)
                            S._log(
                                f"  📡 Resolution price fetched from Data API: "
                                f"{pos.title[:30]} @ ${cur:.4f}",
                                "INFO"
                            )
                        else:
                            cur = pos.entry_price
                            pos.cur_price = cur
                            pos.cur_price_ts = now_t
                            pnl_pct = 0.0
                            S._log(
                                f"  ⚠ MARKET_GONE with no real price — exiting at entry "
                                f"to avoid phantom PnL: {pos.title[:30]}",
                                "WARN"
                            )
                        reason = "MARKET_GONE"

        # (f) Near-zero price = resolved against us — only when price is fresh
        if not reason and cur <= 0.03 and hold_minutes > 10 and pos.market_fail_count == 0:
            reason = f"RESOLVED_LOSS cur={cur:.3f}"

        # (g) CATASTROPHIC LOSS GUARD — always fires regardless of strategy
        if not reason and pnl_pct <= -0.70 and hold_minutes > 3:
            reason = f"CATASTROPHIC_LOSS {pnl_pct*100:.1f}% cur={cur:.3f}"
            S._log(
                f"  🛑 Catastrophic loss guard: {pos.title[:35]} "
                f"P&L={pnl_pct*100:+.1f}% cur=${cur:.4f}",
                "WARN"
            )

        # (h) STALE TREND REVERSAL EXIT
        if not reason and hold_minutes > 45:
            if len(pos.price_history) >= 4:
                recent_prices = [p for _, p in pos.price_history[-4:]]
                if all(recent_prices[i] <= recent_prices[i-1] for i in range(1, 4)):
                    trend_drop = (recent_prices[0] - recent_prices[-1]) / max(recent_prices[0], 0.01)
                    if trend_drop > 0.08 and pnl_pct < -0.15:
                        reason = f"STALE_TREND_REVERSAL drop={trend_drop*100:.0f}% pnl={pnl_pct*100:.0f}%"
                        S._log(
                            f"  📉 Stale trend reversal exit: {pos.title[:35]} "
                            f"P&L={pnl_pct*100:+.1f}% price drop={trend_drop*100:.0f}%",
                            "INFO"
                        )

        # (i) Stale-price resolution check
        if not reason and pos.market_fail_count >= 5:
            stale_p = pos.cur_price
            if 0.05 < stale_p < 0.95:
                real_p = _try_fetch_resolution_price(cid, pos.asset, pos.outcome)
                if real_p is not None and (real_p >= 0.97 or real_p <= 0.03):
                    cur = real_p
                    pos.cur_price = cur
                    pnl_pct = (cur - entry) / max(entry, 0.001)
                    reason = f"MARKET_RESOLVED_CONFIRMED cur={cur:.3f}"
                    S._log(
                        f"  📡 Stale-price resolution confirmed: "
                        f"{pos.title[:35]} @ ${cur:.4f} (was ${stale_p:.4f})",
                        "INFO"
                    )

        if reason:
            pos.reason = reason
            to_close.append((key, pos, cur, pnl_pct, reason))

    # Process closes
    for key, pos, cur, pnl_pct, reason in to_close:
        cid_out       = key[0]
        shares        = pos.shares
        bet           = pos.bet
        exit_proceeds = cur * shares * (1 - TAKER_FEE_RATE)
        pnl_usdc_net  = exit_proceeds - bet
        sell_dtf      = _dt_fields(now_t)
        exit_audit    = _build_exit_audit(pos, cur, pnl_pct, reason, now_t, exit_proceeds, pnl_usdc_net)

        S.env().paper_bankroll += bet + pnl_usdc_net
        S.env().session_pnl    += pnl_usdc_net

        trade_record = TradeRecord(
            cid=pos.cid or key[0],
            asset=pos.asset,
            type="SELL",
            title=pos.title,
            outcome=pos.outcome,
            price=cur,
            shares=shares,
            bet=bet,
            pnl_usdc=round(pnl_usdc_net, 4),
            pnl_pct=round(pnl_pct * 100, 2),
            reason=reason,
            ts=now_t,
            bankroll=round(S.env().paper_bankroll, 4),
            tier=pos.tier,
            strategy=pos.strategy,
            elite_wallets=pos.elite_wallets,
            wallet_buy_cash=pos.wallet_buy_cash,
            wallet_names=[
                (p.name if (p := S.env().wallet_cache.get(w)) else None) or w[:10]+"…"
                for w in pos.elite_wallets[:3]
            ],
            dead_wallets=pos.dead_wallets,
            avg_entry=pos.avg_entry or pos.entry_price,
            market_url=pos.market_url,
            audit=exit_audit,
        )
        DB.append_trade(trade_record)
        S.env().trade_stats.record_sell(pnl_usdc_net)
        DB.upsert_trade_stats(S.env().trade_stats)
        S.env().active_market_cids.discard(cid_out)
        S.env().position_wallet_map.pop(cid_out, None)
        del S.env().open_positions[key]
        S.env().cooldown_cids[cid_out] = now_t

        _ws_unsub = _get_ws_monitor()
        if _ws_unsub:
            _ws_unsub.unsubscribe_position(cid_out)

        record_whale_trade_performance(pos.elite_wallets, pnl_usdc_net, won=(pnl_usdc_net >= 0))

        emoji = "✅" if pnl_usdc_net >= 0 else "❌"
        whale_str = ", ".join(trade_record.wallet_names)
        strat_tag = pos.strategy[:2].upper()
        events.append((
            "CLOSE",
            f"{emoji} SELL [{strat_tag}]: {pos.title[:30]} [{pos.outcome}] "
            f"@ ${cur:.4f} | P&L ${pnl_usdc_net:+.3f} ({pnl_pct*100:+.1f}%) | {reason} | via {whale_str}",
            "#00ff55" if pnl_usdc_net >= 0 else "#ff5555"
        ))
        if S.on_position_close:
            S.on_position_close(pos, pnl_usdc_net, pnl_pct)

    # ─────────────────────────────────────────────────────────────────────────
    # STEP 2: Open new positions
    # ─────────────────────────────────────────────────────────────────────────
    TRADEABLE_TIERS = set(C.TRADEABLE_TIERS_LIST)

    open_cids:  set = set(S.env().open_positions.keys())
    opening_this_cycle: set = set()
    opening_cids_this_cycle: set = set()
    closed_this_cycle_cids: set = {key[0] for key, _, _, _, _ in to_close}

    open_event_slugs: dict = {}
    for pos in S.env().open_positions.values():
        if pos.event_slug:
            open_event_slugs[pos.event_slug] = open_event_slugs.get(pos.event_slug, 0) + 1
    opening_event_slugs: dict = {}

    whale_position_counts: dict = {}
    for pos in S.env().open_positions.values():
        for w in pos.elite_wallets:
            whale_position_counts[w] = whale_position_counts.get(w, 0) + 1
    opening_whale_counts: dict = {}

    # v10: Track open positions per strategy
    open_per_strategy: dict = {}
    for pos in S.env().open_positions.values():
        strat = pos.strategy.split("+")[0] or "consensus_basket"
        open_per_strategy[strat] = open_per_strategy.get(strat, 0) + 1

    for sig in signals:
        tier    = sig.tier
        cid     = sig.cid
        outcome = sig.outcome
        key     = (cid, outcome)
        title   = sig.title
        strat   = sig.strategy.split("+")[0]

        if len(S.env().open_positions) + len(opening_this_cycle) >= C.MAX_OPEN_POSITIONS:
            break

        if tier not in TRADEABLE_TIERS:
            continue

        mkt_type = sig.mkt_type
        if C.BLOCK_SPORTS and mkt_type == "SPORTS":
            continue
        if C.ALLOWED_MARKET_TYPES and mkt_type not in C.ALLOWED_MARKET_TYPES:
            continue

        # v10: Per-strategy position limit
        strat_cfg = getattr(C, f"strategy_{strat}", {})
        strat_max = int(strat_cfg.get("max_positions", C.MAX_OPEN_POSITIONS))
        cur_strat_open = open_per_strategy.get(strat, 0)
        opening_strat  = sum(1 for s in opening_this_cycle
                             if (p := S.env().open_positions.get(s)) and p.strategy.startswith(strat))
        if cur_strat_open + opening_strat >= strat_max:
            S._log(f"  🚫 Strategy [{strat}] at capacity ({strat_max} positions)", "DIAG")
            continue

        # Consensus basket uses its own min_elite_confluence
        if strat == "consensus_basket":
            min_el = int(strat_cfg.get("min_elite_confluence", 1))
        else:
            min_el = 1  # recent_form and drift_discount: 1 verified whale is enough
        if sig.n_elite < min_el:
            continue

        exits_here       = sig.exits_detected
        elite_wallet_set = set(sig.elite_ver.keys())
        if elite_wallet_set & set(exits_here):
            S._log(f"  🚫 Elite exit-block: {title[:30]} {outcome}", "DIAG")
            continue

        if cid in S.env().active_market_cids:
            continue
        if cid in opening_cids_this_cycle:
            continue
        if key in open_cids or key in opening_this_cycle:
            continue

        event_slug = sig.event_slug
        if event_slug:
            already = open_event_slugs.get(event_slug, 0) + opening_event_slugs.get(event_slug, 0)
            if already >= MAX_POSITIONS_PER_EVENT:
                S._log(f"  🚫 Event limit {MAX_POSITIONS_PER_EVENT}: {title[:30]}", "DIAG")
                continue

        sig_elites = list(sig.elite_ver.keys())
        if sig_elites and len(sig_elites) == 1:
            w0   = sig_elites[0]
            used = whale_position_counts.get(w0, 0) + opening_whale_counts.get(w0, 0)
            if used >= MAX_POSITIONS_PER_WALLET:
                w_name = (p.name if (p := S.env().wallet_cache.get(w0)) else None) or w0[:10]+"…"
                S._log(f"  🚫 Whale cap ({MAX_POSITIONS_PER_WALLET}): {w_name} — {title[:30]}", "DIAG")
                continue

        if cid in closed_this_cycle_cids:
            continue
        if cid in S.env().cooldown_cids:
            remaining = EXIT_COOLDOWN_SECONDS - (now_t - S.env().cooldown_cids[cid])
            S._log(f"  ⏳ Cooldown {title[:30]}: {remaining/60:.0f}min left", "DIAG")
            continue

        age_h     = sig.age_h
        # Drift discount strategy has much longer age window — don't use global MAX_SIGNAL_AGE_H
        if strat == "drift_discount":
            age_limit = float(strat_cfg.get("max_signal_age_h", 6.0))
        elif tier == "HFT":
            age_limit = HFT_MIRROR_DELAY_MAX_SECONDS / 3600
        else:
            age_limit = float(strat_cfg.get("max_signal_age_h", MAX_SIGNAL_AGE_H))
        if age_h > age_limit:
            S._log(f"  ⏰ Signal too old ({age_h:.1f}h): {title[:30]}", "DIAG")
            continue

        bet = sig.bet
        if bet > S.env().paper_bankroll * 0.95 or S.env().paper_bankroll < MIN_BET:
            if S.env().paper_bankroll < MIN_BET:
                events.append(("WARN", f"⚠ Bankroll too low (${S.env().paper_bankroll:.2f})", "#ffaa00"))
            break

        cur    = sig.cur
        asset  = sig.asset

        # Price zone gate (belt-and-suspenders)
        price_min = float(strat_cfg.get("price_min", C.MIN_ENTRY_PRICE))
        price_max = float(strat_cfg.get("price_max", C.MAX_ENTRY_PRICE))
        if cur < price_min or cur > price_max:
            S._log(
                f"  🚫 Price zone block [{strat}]: {title[:30]} cur={cur:.3f} "
                f"zone=[{price_min:.2f},{price_max:.2f}]",
                "DIAG"
            )
            continue

        # EV gate — skip for HFT spike signals (momentum, not mispricing)
        liq = sig.mkt.liq
        ev_info  = estimate_expected_value(cur, sig.avg_entry, liq, bet, mkt_type, sig.avg_wscore)
        is_spike = sig.is_hft and sig.has_large_trade
        # Drift discount: EV may appear slightly negative due to drift, but the discount IS the edge
        is_dd    = (strat == "drift_discount")
        if not ev_info["tradeable"] and not is_spike and not is_dd:
            S._log(
                f"  📊 EV REJECT: {title[:30]} EV={ev_info['ev_pct']:+.1f}% "
                f"(friction={ev_info['total_friction']:.1f}%)",
                "INFO"
            )
            continue
        else:
            S._log(
                f"  ✅ EV OK [{strat}]: {title[:30]} EV={ev_info['ev_pct']:+.1f}% "
                f"fair_prob={ev_info['fair_prob']:.0f}% friction={ev_info['total_friction']:.1f}%",
                "DIAG"
            )

        shares = (bet / max(cur, 0.01)) * (1 - TAKER_FEE_RATE)
        S.env().paper_bankroll -= bet
        buy_dtf = _dt_fields(now_t)

        elite_wallet_addrs = list(sig.elite_ver.keys())
        all_whale_addrs    = list(sig.ver.keys())
        elite_names = [
            (p.name if (p := S.env().wallet_cache.get(w)) else None) or w[:10]+"…"
            for w in elite_wallet_addrs[:3]
        ]

        is_conviction = sig.has_large_trade
        mkt_obj       = sig.mkt
        resolved_slug = mkt_obj.slug or sig.slug

        elite_ver = sig.elite_ver
        wallet_buy_cash = {
            w.lower(): t.cash
            for w, t in elite_ver.items()
        }

        # v10: Get stop_loss_pct from signal strategy config
        sig_stop_loss = sig.stop_loss_pct  # None for RF/DD, -0.35 for CB
        market_url = _build_market_url(event_slug, resolved_slug)
        entry_audit = _build_entry_audit(sig, cur, shares, bet, ev_info, now_t, S.env().paper_bankroll)

        trade_record = TradeRecord(
            cid=cid,
            asset=asset,
            type="BUY",
            title=title,
            slug=resolved_slug,
            event_slug=event_slug,
            outcome=outcome,
            price=cur,
            shares=shares,
            bet=bet,
            reason=f"AUTO_{tier}",
            ts=now_t,
            bankroll=round(S.env().paper_bankroll, 4),
            tier=tier,
            strategy=strat,
            stop_loss_pct=sig_stop_loss,
            elite_wallets=elite_wallet_addrs,
            wallet_names=elite_names,
            dead_wallets=[],
            wallet_buy_cash=wallet_buy_cash,
            avg_entry=sig.avg_entry,
            score=sig.score,
            n_confluence=sig.n_confluence,
            is_conviction=is_conviction,
            market_url=market_url,
            audit=entry_audit,
        )

        trade_record.load_prices()

        DB.append_trade(trade_record)

        pos = Position(
            buy_trade=          trade_record,
            key=               str(key),
            status=            "open",
            type=              "OPEN",
            # signal-only fields not on TradeRecord
            tracked_wallets=     all_whale_addrs,
            elite_names=       elite_names,
            n_elite=           sig.n_elite,
            is_hft=            sig.is_hft,
            mkt_type=          mkt_type,
            is_sports=         sig.is_sports,
            conviction_detail= sig.conviction_detail,
            ev_info=           ev_info,
            ver_flow=          sig.ver_flow,
            whale_avg_entry=   sig.avg_entry if strat == "drift_discount" else 0.0,
            drift_discount_pct=float(sig.drift_discount_pct or 0) if strat == "drift_discount" else 0.0,
            source_recent_wr=  float(sig.source_recent_wr or 0) if strat == "recent_form" else 0.0,
            market_fail_count= 0,
            peak_pnl_pct=      0.0,
        )

        S.env().open_positions[key]    = pos
        S.env().active_market_cids.add(cid)
        S.env().position_wallet_map[cid] = set(all_whale_addrs)

        # WS: subscribe to real-time resolution events
        _ws_sub = _get_ws_monitor()
        if _ws_sub and asset:
            tokens = [asset]
            mkt_cached = S.market_cache.get(cid)
            if mkt_cached is not None:
                for tid in mkt_cached.asset_to_price.keys():
                    if tid != asset and tid not in tokens:
                        tokens.append(tid)
            _ws_sub.subscribe_position(cid, tokens)

        opening_this_cycle.add(key)
        opening_cids_this_cycle.add(cid)
        open_cids.add(key)
        if event_slug:
            opening_event_slugs[event_slug] = opening_event_slugs.get(event_slug, 0) + 1
        for w in elite_wallet_addrs:
            opening_whale_counts[w] = opening_whale_counts.get(w, 0) + 1
        open_per_strategy[strat] = open_per_strategy.get(strat, 0) + 1

        n_conf   = sig.n_confluence
        hft_tag  = "⚡HFT " if sig.is_hft else ""
        conv_tag = "💎CONVICTION " if is_conviction else ""
        conf_str = f" +{n_conf}conf" if n_conf else ""
        age_min  = sig.age_h * 60
        sl_str   = f" SL:{sig_stop_loss*100:.0f}%" if sig_stop_loss is not None else " SL:none"

        events.append((
            "OPEN",
            f"🛒 BUY [{strat}]: {title[:30]} [{outcome}] @ ${cur:.4f} "
            f"| ${bet:.2f} | {shares:.1f}sh | [{hft_tag}{conv_tag}{tier} {sig.score:.0f}pts] "
            f"| {', '.join(elite_names)}{conf_str} | {age_min:.0f}min ago{sl_str}",
            "#00ff88"
        ))
        if S.on_position_open:
            S.on_position_open(pos)

    if events or to_close:
        save_state()

    if S.env().cycle_count % 10 == 0:
        save_wallet_roster_async()

    return events
