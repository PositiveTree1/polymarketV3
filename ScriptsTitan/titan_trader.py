"""
TITAN — Auto paper trading. Single-wallet edition. v10.

EXIT PHILOSOPHY — FOLLOW THE WHALE (UPDATED v10):
  1. WHALE_EXIT_SELL=true  → exit immediately when the triggering elite sells
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
from titan_market  import get_market, get_outcome_price, is_market_resolving
from titan_signals import classify_market, estimate_expected_value, _KNOWN_HEDGE_WALLETS
from titan_wallet  import is_hft_wallet, record_whale_trade_performance
from titan_persistence import save_state, save_whale_roster_async
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
            data = S.safe_get(f"{GAMMA_API}/markets", {
                "clob_token_ids": f'["{asset}"]', "limit": 1
            })
            if data and isinstance(data, list) and data:
                m = data[0]
                import json as _json
                raw_prices = m.get("outcomePrices") or "[]"
                try:
                    prices = _json.loads(raw_prices) if isinstance(raw_prices, str) else list(raw_prices)
                    prices = [float(p) for p in prices]
                except Exception:
                    prices = []
                clob_tokens = m.get("clobTokenIds") or m.get("clob_token_ids") or "[]"
                try:
                    if isinstance(clob_tokens, str):
                        clob_tokens = _json.loads(clob_tokens)
                except Exception:
                    clob_tokens = []
                for i, tok in enumerate(clob_tokens):
                    if str(tok) == str(asset) and i < len(prices):
                        p = prices[i]
                        if p >= 0.97 or p <= 0.03:
                            S._log(f"  📡 Asset price confirmed: {asset[:20]} = {p:.4f}", "DIAG")
                            return p

        data = S.safe_get(f"{DATA_API}/trades", {
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

        if asset:
            pos_data = S.safe_get(f"{DATA_API}/positions", {
                "asset": asset, "limit": 1,
            })
            if pos_data and isinstance(pos_data, list) and pos_data:
                p = pos_data[0]
                cur_p = float(p.get("curPrice", 0) or 0)
                if cur_p >= 0.97 or cur_p <= 0.03:
                    return cur_p

    except Exception as e:
        S._log(f"  ⚠ Resolution price fetch failed: {e}", "DIAG")
    return None


def _get_current_price(pos: dict) -> tuple:
    """
    Two-stage price fetch for open positions.
    Returns (price, is_resolving).
    """
    from titan_market import fetch_position_price_fast
    cid     = pos.get("cid")
    outcome = pos.get("outcome", "")
    asset   = pos.get("asset", "")
    title   = pos.get("title", "")
    stale_price = pos.get("cur_price", pos.get("entry_price", 0.5))

    fast_price = fetch_position_price_fast(cid, asset, outcome)
    if fast_price is not None:
        resolving = fast_price <= 0.03 or fast_price >= 0.97
        pos["market_fail_count"] = 0
        return fast_price, resolving

    cached = S.market_cache.get(cid)
    if cached and (time.time() - cached.get("ts", 0)) > C.MARKET_TTL:
        S.market_cache.pop(cid, None)

    mkt, err = get_market(cid, title, asset=asset, slug=pos.get("slug", ""))
    if not mkt:
        pos["market_fail_count"] = pos.get("market_fail_count", 0) + 1
        return stale_price, False

    pos["market_fail_count"] = 0
    resolving = is_market_resolving(mkt)

    if asset:
        ap = mkt.get("asset_to_price", {})
        if asset in ap:
            price = ap[asset]
            if price <= 0.03: return price, True
            if price >= 0.97: return price, True
            return price, resolving

    cur = get_outcome_price(mkt, outcome, asset=asset)
    return cur, resolving


def _get_effective_stop_loss(pos: dict) -> float | None:
    """
    v10: Get the effective stop loss percentage for an open position.
    Priority:
      1. Per-position stop_loss_pct (set from signal's strategy config at entry)
      2. Global STOP_LOSS_PCT if STOP_LOSS_ENABLED
      3. None (no stop)
    """
    # Per-position stop loss from signal strategy config
    pos_sl = pos.get("stop_loss_pct")
    if pos_sl is not None:
        return float(pos_sl)
    # Fall back to global config if enabled
    if C.STOP_LOSS_ENABLED:
        return float(C.STOP_LOSS_PCT)
    return None


def _dt_fields(ts: float) -> dict:
    dt = datetime.fromtimestamp(ts)
    return {
        "ts": ts,
        "ts_str": dt.strftime("%Y-%m-%d %H:%M:%S"),
        "ts_iso": dt.isoformat(timespec="seconds"),
        "date": dt.strftime("%Y-%m-%d"),
        "time": dt.strftime("%H:%M:%S"),
    }


def _build_market_url(event_slug: str = "", slug: str = "") -> str:
    if event_slug:
        return f"https://polymarket.com/event/{event_slug}"
    if slug:
        return f"https://polymarket.com/event/{slug}"
    return "https://polymarket.com"


def _compact_market_snapshot(mkt: dict | None, *, fallback_title: str = "", fallback_slug: str = "",
                             fallback_event_slug: str = "") -> dict:
    mkt = mkt or {}
    return {
        "title": mkt.get("title") or mkt.get("question") or fallback_title,
        "slug": mkt.get("slug") or fallback_slug,
        "event_slug": mkt.get("event_slug") or mkt.get("eventSlug") or fallback_event_slug,
        "yes_price": mkt.get("yes_price"),
        "no_price": mkt.get("no_price"),
        "outcome_labels": list(mkt.get("outcome_labels", [])),
        "outcome_prices": dict(mkt.get("outcome_prices", {})),
        "asset_to_price": dict(mkt.get("asset_to_price", {})),
        "liq": mkt.get("liq"),
        "volume": mkt.get("volume"),
        "hrs_left": mkt.get("hrs_left"),
        "end_date": mkt.get("end_date"),
        "ts": mkt.get("ts"),
    }


def _compact_wallet_snapshot(wallet_addrs: list[str]) -> list[dict]:
    rows = []
    for addr in wallet_addrs[:8]:
        prof = S.env().wallet_cache.get(str(addr).lower(), {})
        rows.append({
            "wallet": addr,
            "name": prof.get("name", str(addr)[:10] + "…"),
            "score": prof.get("score"),
            "win_rate": prof.get("win_rate"),
            "total_pnl": prof.get("total_pnl"),
            "verified": prof.get("verified"),
            "elite": prof.get("elite"),
            "hft": prof.get("hft"),
        })
    return rows


def _compact_elite_trade_snapshot(elite_ver: dict) -> list[dict]:
    rows = []
    for addr, trade in list((elite_ver or {}).items())[:8]:
        rows.append({
            "wallet": addr,
            "name": trade.get("name"),
            "title": trade.get("title"),
            "outcome": trade.get("outcome"),
            "price": trade.get("price"),
            "size": trade.get("size"),
            "cash": trade.get("cash"),
            "asset": trade.get("asset"),
            "slug": trade.get("slug"),
            "event_slug": trade.get("event_slug"),
            "ts": trade.get("ts"),
            "source": trade.get("source"),
            "window": trade.get("window"),
        })
    return rows


def _build_entry_audit(sig: dict, cur: float, shares: float, bet: float, ev_info: dict,
                       now_t: float, bankroll_after: float) -> dict:
    dtf = _dt_fields(now_t)
    market = sig.get("mkt", {}) or {}
    event_slug = sig.get("event_slug", "") or market.get("event_slug", "")
    slug = market.get("slug") or sig.get("slug", "")
    http_traces = _collect_action_http_traces(
        since_ts=max(0.0, now_t - 60),
        cid=sig.get("cid", ""),
        asset=sig.get("asset", ""),
        slug=slug,
        event_slug=event_slug,
        limit=12,
    )
    return {
        "captured_at": dtf,
        "market_url": _build_market_url(event_slug, slug),
        "signal_snapshot": {
            "cid": sig.get("cid"),
            "title": sig.get("title"),
            "outcome": sig.get("outcome"),
            "asset": sig.get("asset"),
            "strategy": sig.get("strategy"),
            "tier": sig.get("tier"),
            "score": sig.get("score"),
            "score_breakdown": dict(sig.get("bd", {})),
            "stop_loss_pct": sig.get("stop_loss_pct"),
            "age_h": sig.get("age_h"),
            "age_min": sig.get("age_min"),
            "cur": cur,
            "avg_entry": sig.get("avg_entry", cur),
            "drift": sig.get("drift"),
            "slippage": sig.get("slippage"),
            "total_flow": sig.get("total_flow"),
            "ver_flow": sig.get("ver_flow"),
            "opposing_flow": sig.get("opposing_flow"),
            "n_ver": sig.get("n_ver"),
            "n_elite": sig.get("n_elite"),
            "n_confluence": sig.get("n_confluence"),
            "max_bet_cash": sig.get("max_bet_cash"),
            "is_hft": sig.get("is_hft"),
            "has_large_trade": sig.get("has_large_trade"),
            "is_conviction": sig.get("has_large_trade", False),
            "conviction_detail": sig.get("conviction_detail"),
            "exits_detected": list(sig.get("exits_detected", [])),
            "elite_wallets": list(sig.get("elite_ver", {}).keys()),
            "whale_names": list(sig.get("names", [])),
            "elite_trades": _compact_elite_trade_snapshot(sig.get("elite_ver", {})),
        },
        "market_snapshot": _compact_market_snapshot(
            market,
            fallback_title=sig.get("title", ""),
            fallback_slug=slug,
            fallback_event_slug=event_slug,
        ),
        "wallet_snapshot": _compact_wallet_snapshot(list(sig.get("elite_ver", {}).keys())),
        "pricing_snapshot": {
            "entry_price": cur,
            "shares": shares,
            "bet": bet,
            "fee_rate": TAKER_FEE_RATE,
            "avg_whale_entry": sig.get("avg_entry", cur),
            "bankroll_after_buy": round(bankroll_after, 4),
        },
        "ev_snapshot": dict(ev_info or {}),
        "http_traces": http_traces,
        "decision_summary": (
            f"BUY {sig.get('outcome', '')} via {sig.get('strategy', '?')} "
            f"[{sig.get('tier', '?')}] score={sig.get('score', 0):.0f} "
            f"cur={cur:.4f} whale_avg={sig.get('avg_entry', cur):.4f}"
        ),
    }


def _build_exit_audit(pos: dict, cur: float, pnl_pct: float, reason: str, now_t: float,
                      exit_proceeds: float, pnl_usdc_net: float) -> dict:
    dtf = _dt_fields(now_t)
    hold_minutes = (now_t - pos.get("entry_ts", now_t)) / 60
    http_traces = _collect_action_http_traces(
        since_ts=max(0.0, now_t - 30),
        cid=pos.get("cid", ""),
        asset=pos.get("asset", ""),
        slug=pos.get("slug", ""),
        event_slug=pos.get("event_slug", ""),
        limit=12,
    )
    return {
        "captured_at": dtf,
        "market_url": pos.get("market_url") or _build_market_url(pos.get("event_slug", ""), pos.get("slug", "")),
        "exit_reason": reason,
        "hold_minutes": round(hold_minutes, 2),
        "pricing_snapshot": {
            "entry_price": pos.get("entry_price"),
            "exit_price": cur,
            "shares": pos.get("shares"),
            "bet": pos.get("bet"),
            "gross_exit_value": round(cur * pos.get("shares", 0), 6),
            "net_exit_proceeds": round(exit_proceeds, 6),
            "fee_rate": TAKER_FEE_RATE,
            "pnl_usdc": round(pnl_usdc_net, 4),
            "pnl_pct": round(pnl_pct * 100, 2),
            "peak_pnl_pct": pos.get("peak_pnl_pct"),
        },
        "market_snapshot": {
            "liq": pos.get("liq"),
            "volume": pos.get("volume"),
            "hrs_left": pos.get("hrs_left"),
            "end_date": pos.get("end_date"),
            "cur_price": pos.get("cur_price", cur),
            "market_fail_count": pos.get("market_fail_count", 0),
        },
        "wallet_snapshot": _compact_wallet_snapshot(pos.get("elite_wallets", [])),
        "price_history_tail": list(pos.get("price_history", [])[-120:]),
        "http_traces": http_traces,
        "decision_summary": (
            f"SELL {pos.get('outcome', '')} via {pos.get('strategy', '?')} "
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


def auto_trade(signals: list, whale_exits: dict) -> list:
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
        cid      = pos.get("cid", key[0])
        entry    = pos["entry_price"]
        bet      = pos["bet"]
        shares   = pos["shares"]
        entry_ts = pos.get("entry_ts", 0)

        hold_minutes = (now_t - entry_ts) / 60
        if hold_minutes < C.MIN_HOLD_MINUTES:
            continue

        cur, resolving = _get_current_price(pos)
        pos["cur_price"] = cur

        if "price_history" not in pos:
            pos["price_history"] = []
        pos["price_history"].append((now_t, cur))
        if len(pos["price_history"]) > 1440:
            del pos["price_history"][:-1440]

        pnl_pct = (cur - entry) / max(entry, 0.001)
        reason  = None

        # (a0) WebSocket resolution — HIGHEST PRIORITY
        _ws = _get_ws_monitor()
        if _ws:
            ws_res = _ws.is_resolved_via_ws(cid)
            if ws_res:
                ws_price = ws_res.get("price", cur)
                cur = ws_price
                pos["cur_price"] = cur
                pnl_pct = (cur - entry) / max(entry, 0.001)
                reason = f"WS_RESOLVED price={cur:.4f} via={ws_res.get('event_type','?')}"
                S._log(
                    f"  📡 WS exit triggered: {pos.get('title','?')[:35]} "
                    f"@ {cur:.4f} P&L={pnl_pct*100:+.1f}%",
                    "INFO"
                )

        # (a) Market resolving — always exit
        if not reason and resolving:
            reason = f"MARKET_RESOLVING cur={cur:.3f}"

        # (b) WHALE EXIT — follow the whale
        elif C.WHALE_EXIT_SELL and whale_exits.get(cid):
            exiting         = set(whale_exits[cid])
            elite_entry_set = set(w.lower() for w in pos.get("elite_wallets", []))
            matched_elite   = list(exiting & elite_entry_set)

            if matched_elite:
                non_hft_exiting = [
                    w for w in matched_elite
                    if not (S.env().wallet_cache.get(w, {}).get("hft") or
                            is_hft_wallet(S.env().wallet_cache.get(w, {})) or
                            w in _KNOWN_HEDGE_WALLETS)
                ]
                if non_hft_exiting:
                    whale_losing = cur < pos.get("entry_price", entry) * 0.92
                    early_noise  = whale_losing and pnl_pct > -0.05 and hold_minutes < 20
                    if not early_noise:
                        matched_names = [
                            S.env().wallet_cache.get(a, {}).get("name", a[:10]+"…")
                            for a in non_hft_exiting[:2]
                        ]
                        reason = f"WHALE_SOLD {matched_names}"
                    else:
                        S._log(
                            f"  🐋 Early noise exit ignored: {pos['title'][:30]} "
                            f"pnl={pnl_pct*100:+.1f}% hold={hold_minutes:.0f}min",
                            "DIAG"
                        )
                else:
                    hft_names = [S.env().wallet_cache.get(w, {}).get("name", w[:10]) for w in matched_elite[:2]]
                    S._log(f"  ⚡ HFT/market-maker exit ignored: {hft_names} on {pos['title'][:30]}", "DIAG")

        # (c) PROFIT TARGET
        elif not reason and getattr(C, "PROFIT_TARGET_ENABLED", True) and pnl_pct >= C.PROFIT_TARGET_PCT:
            reason = f"PROFIT_TARGET {pnl_pct*100:.1f}%"
            S._log(
                f"  💰 Profit target: {pos.get('title','?')[:35]} "
                f"P&L={pnl_pct*100:+.1f}% (target={C.PROFIT_TARGET_PCT*100:.0f}%)",
                "INFO"
            )

        # (d) v10 PER-SIGNAL STOP LOSS
        # Uses strategy-specific stop_loss_pct set at entry time.
        # recent_form and drift_discount have stop_loss_pct=None → no stop
        # consensus_basket has stop_loss_pct=-0.35 → soft -35% stop
        # Global STOP_LOSS_PCT is used if per-signal has no config
        if not reason:
            eff_sl = _get_effective_stop_loss(pos)
            if eff_sl is not None and pnl_pct <= eff_sl:
                strat_tag = pos.get("strategy", "?")[:2].upper()
                reason = f"STOP_LOSS[{strat_tag}] {pnl_pct*100:.1f}% (limit={eff_sl*100:.0f}%)"
                S._log(
                    f"  🛑 Stop loss [{strat_tag}]: {pos.get('title','?')[:35]} "
                    f"P&L={pnl_pct*100:+.1f}% (limit={eff_sl*100:.0f}%)",
                    "INFO"
                )

        # (e) Expiring soon
        if not reason:
            mkt_check, _ = get_market(cid, pos.get("title"), asset=pos.get("asset",""), slug=pos.get("slug",""))
            if mkt_check:
                pos["market_fail_count"] = 0
                hrs = mkt_check.get("hrs_left")
                if hrs is not None and hrs < max(MIN_HOURS_LEFT, 0.35):
                    reason = "EXPIRING_SOON"
            else:
                pos["market_fail_count"] = pos.get("market_fail_count", 0) + 1
                fail_count = pos["market_fail_count"]
                if fail_count >= 3:
                    real_p = _try_fetch_resolution_price(
                        cid, pos.get("asset", ""), pos.get("outcome", "")
                    )
                    if real_p is not None:
                        cur = real_p
                        pos["cur_price"] = cur
                        pnl_pct = (cur - entry) / max(entry, 0.001)
                        if real_p <= 0.03 or real_p >= 0.97:
                            reason = f"MARKET_RESOLVED_CONFIRMED cur={cur:.3f}"
                            S._log(
                                f"  📡 Fast resolution check: {pos.get('title','?')[:35]} @ ${cur:.4f}",
                                "INFO"
                            )
                if fail_count >= 10:
                    stale_p = pos.get("cur_price", 0.5)
                    if stale_p <= 0.05 or stale_p >= 0.95:
                        reason = "MARKET_RESOLVED_OR_GONE"
                    elif fail_count >= 20:
                        real_exit_price = _try_fetch_resolution_price(
                            cid, pos.get("asset", ""), pos.get("outcome", "")
                        )
                        if real_exit_price is not None:
                            cur = real_exit_price
                            pos["cur_price"] = cur
                            pnl_pct = (cur - entry) / max(entry, 0.001)
                            S._log(
                                f"  📡 Resolution price fetched from Data API: "
                                f"{pos.get('title','?')[:30]} @ ${cur:.4f}",
                                "INFO"
                            )
                        else:
                            cur = pos.get("entry_price", entry)
                            pos["cur_price"] = cur
                            pnl_pct = 0.0
                            S._log(
                                f"  ⚠ MARKET_GONE with no real price — exiting at entry "
                                f"to avoid phantom PnL: {pos.get('title','?')[:30]}",
                                "WARN"
                            )
                        reason = "MARKET_GONE"

        # (f) Near-zero price = resolved against us
        if not reason and cur <= 0.03 and hold_minutes > 10:
            reason = f"RESOLVED_LOSS cur={cur:.3f}"

        # (g) CATASTROPHIC LOSS GUARD — always fires regardless of strategy
        if not reason and pnl_pct <= -0.70 and hold_minutes > 3:
            reason = f"CATASTROPHIC_LOSS {pnl_pct*100:.1f}% cur={cur:.3f}"
            S._log(
                f"  🛑 Catastrophic loss guard: {pos.get('title','?')[:35]} "
                f"P&L={pnl_pct*100:+.1f}% cur=${cur:.4f}",
                "WARN"
            )

        # (h) STALE TREND REVERSAL EXIT
        if not reason and hold_minutes > 45:
            price_hist = pos.get("price_history", [])
            if len(price_hist) >= 4:
                recent_prices = [p for _, p in price_hist[-4:]]
                if all(recent_prices[i] <= recent_prices[i-1] for i in range(1, 4)):
                    trend_drop = (recent_prices[0] - recent_prices[-1]) / max(recent_prices[0], 0.01)
                    if trend_drop > 0.08 and pnl_pct < -0.15:
                        reason = f"STALE_TREND_REVERSAL drop={trend_drop*100:.0f}% pnl={pnl_pct*100:.0f}%"
                        S._log(
                            f"  📉 Stale trend reversal exit: {pos.get('title','?')[:35]} "
                            f"P&L={pnl_pct*100:+.1f}% price drop={trend_drop*100:.0f}%",
                            "INFO"
                        )

        # (i) Stale-price resolution check
        if not reason and pos.get("market_fail_count", 0) >= 5:
            stale_p = pos.get("cur_price", 0.5)
            if 0.05 < stale_p < 0.95:
                real_p = _try_fetch_resolution_price(
                    cid, pos.get("asset", ""), pos.get("outcome", "")
                )
                if real_p is not None and (real_p >= 0.97 or real_p <= 0.03):
                    cur = real_p
                    pos["cur_price"] = cur
                    pnl_pct = (cur - entry) / max(entry, 0.001)
                    reason = f"MARKET_RESOLVED_CONFIRMED cur={cur:.3f}"
                    S._log(
                        f"  📡 Stale-price resolution confirmed: "
                        f"{pos.get('title','?')[:35]} @ ${cur:.4f} (was ${stale_p:.4f})",
                        "INFO"
                    )

        if reason:
            pos["reason"] = reason
            to_close.append((key, pos, cur, pnl_pct, reason))

    # Process closes
    for key, pos, cur, pnl_pct, reason in to_close:
        cid_out       = key[0]
        shares        = pos["shares"]
        bet           = pos["bet"]
        exit_proceeds = cur * shares * (1 - TAKER_FEE_RATE)
        pnl_usdc_net  = exit_proceeds - bet
        sell_dtf      = _dt_fields(now_t)
        exit_audit    = _build_exit_audit(pos, cur, pnl_pct, reason, now_t, exit_proceeds, pnl_usdc_net)

        S.env().paper_bankroll += bet + pnl_usdc_net
        S.env().session_pnl    += pnl_usdc_net

        trade_record = {
            "cid":           pos.get("cid", key[0]),
            "type":          "SELL",
            "title":         pos["title"],
            "outcome":       pos["outcome"],
            "entry_price":   pos["entry_price"],
            "exit_price":    cur,
            "shares":        shares,
            "bet":           bet,
            "pnl_usdc":      round(pnl_usdc_net, 4),
            "pnl_pct":       round(pnl_pct * 100, 2),
            "reason":        reason,
            "ts":            now_t,
            "ts_str":        sell_dtf["ts_str"],
            "ts_iso":        sell_dtf["ts_iso"],
            "date":          sell_dtf["date"],
            "time":          sell_dtf["time"],
            "entry_ts":      pos.get("entry_ts"),
            "entry_ts_str":  pos.get("entry_ts_str"),
            "entry_ts_iso":  pos.get("entry_ts_iso"),
            "entry_date":    pos.get("entry_date"),
            "entry_time":    pos.get("entry_time"),
            "exit_ts":       now_t,
            "exit_ts_str":   sell_dtf["ts_str"],
            "exit_ts_iso":   sell_dtf["ts_iso"],
            "exit_date":     sell_dtf["date"],
            "exit_time":     sell_dtf["time"],
            "bankroll":      round(S.env().paper_bankroll, 4),
            "tier":          pos.get("tier", "?"),
            "strategy":      pos.get("strategy", "?"),
            "elite_wallets": pos.get("elite_wallets", []),
            "whale_buy_cash": pos.get("whale_buy_cash", {}),
            "whale_names":   [
                S.env().wallet_cache.get(w, {}).get("name", w[:10]+"…")
                for w in pos.get("elite_wallets", [])[:3]
            ],
            "avg_entry":     pos.get("avg_entry", pos.get("entry_price", 0)),
            "market_url":    pos.get("market_url"),
            # price_history is stored in titan_state.db (price_history table)
            "entry_audit":   pos.get("entry_audit"),
            "exit_audit":    exit_audit,
        }
        DB.append_trade(trade_record)
        S.env().trade_stats.record_sell(pnl_usdc_net)
        DB.upsert_trade_stats(S.env().trade_stats)
        S.env().active_market_cids.discard(cid_out)
        S.env().position_whale_map.pop(cid_out, None)
        del S.env().open_positions[key]
        S.env().cooldown_cids[cid_out] = now_t

        _ws_unsub = _get_ws_monitor()
        if _ws_unsub:
            _ws_unsub.unsubscribe_position(cid_out)

        record_whale_trade_performance(pos.get("elite_wallets", []), pnl_usdc_net, won=(pnl_usdc_net >= 0))

        emoji = "✅" if pnl_usdc_net >= 0 else "❌"
        whale_str = ", ".join(trade_record["whale_names"])
        strat_tag = pos.get("strategy", "?")[:2].upper()
        events.append((
            "CLOSE",
            f"{emoji} SELL [{strat_tag}]: {pos['title'][:30]} [{pos['outcome']}] "
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
        es = pos.get("event_slug", "")
        if es:
            open_event_slugs[es] = open_event_slugs.get(es, 0) + 1
    opening_event_slugs: dict = {}

    whale_position_counts: dict = {}
    for pos in S.env().open_positions.values():
        for w in pos.get("elite_wallets", []):
            whale_position_counts[w] = whale_position_counts.get(w, 0) + 1
    opening_whale_counts: dict = {}

    # v10: Track open positions per strategy
    open_per_strategy: dict = {}
    for pos in S.env().open_positions.values():
        strat = pos.get("strategy", "consensus_basket").split("+")[0]
        open_per_strategy[strat] = open_per_strategy.get(strat, 0) + 1

    for sig in signals:
        tier    = sig["tier"]
        cid     = sig["cid"]
        outcome = sig["outcome"]
        key     = (cid, outcome)
        title   = sig["title"]
        strat   = sig.get("strategy", "consensus_basket").split("+")[0]

        if len(S.env().open_positions) + len(opening_this_cycle) >= C.MAX_OPEN_POSITIONS:
            break

        if tier not in TRADEABLE_TIERS:
            continue

        mkt_type = sig.get("mkt_type", "POLITICS")
        if C.BLOCK_SPORTS and mkt_type == "SPORTS":
            continue
        if C.ALLOWED_MARKET_TYPES and mkt_type not in C.ALLOWED_MARKET_TYPES:
            continue

        # v10: Per-strategy position limit
        strat_cfg = getattr(C, f"strategy_{strat}", {})
        strat_max = int(strat_cfg.get("max_positions", C.MAX_OPEN_POSITIONS))
        cur_strat_open = open_per_strategy.get(strat, 0)
        opening_strat  = sum(1 for s in opening_this_cycle
                             if S.env().open_positions.get(s, {}).get("strategy", "").startswith(strat))
        if cur_strat_open + opening_strat >= strat_max:
            S._log(f"  🚫 Strategy [{strat}] at capacity ({strat_max} positions)", "DIAG")
            continue

        # Consensus basket uses its own min_elite_confluence
        if strat == "consensus_basket":
            min_el = int(strat_cfg.get("min_elite_confluence", 1))
        else:
            min_el = 1  # recent_form and drift_discount: 1 verified whale is enough
        if sig.get("n_elite", 0) < min_el:
            continue

        exits_here       = sig.get("exits_detected", [])
        elite_wallet_set = set(sig.get("elite_ver", {}).keys())
        if elite_wallet_set & set(exits_here):
            S._log(f"  🚫 Elite exit-block: {title[:30]} {outcome}", "DIAG")
            continue

        if cid in S.env().active_market_cids:
            continue
        if cid in opening_cids_this_cycle:
            continue
        if key in open_cids or key in opening_this_cycle:
            continue

        event_slug = sig.get("event_slug", "")
        if event_slug:
            already = open_event_slugs.get(event_slug, 0) + opening_event_slugs.get(event_slug, 0)
            if already >= MAX_POSITIONS_PER_EVENT:
                S._log(f"  🚫 Event limit {MAX_POSITIONS_PER_EVENT}: {title[:30]}", "DIAG")
                continue

        sig_elites = list(sig.get("elite_ver", {}).keys())
        if sig_elites and len(sig_elites) == 1:
            w0   = sig_elites[0]
            used = whale_position_counts.get(w0, 0) + opening_whale_counts.get(w0, 0)
            if used >= MAX_POSITIONS_PER_WHALE:
                w_name = S.env().wallet_cache.get(w0, {}).get("name", w0[:10]+"…")
                S._log(f"  🚫 Whale cap ({MAX_POSITIONS_PER_WHALE}): {w_name} — {title[:30]}", "DIAG")
                continue

        if cid in closed_this_cycle_cids:
            continue
        if cid in S.env().cooldown_cids:
            remaining = EXIT_COOLDOWN_SECONDS - (now_t - S.env().cooldown_cids[cid])
            S._log(f"  ⏳ Cooldown {title[:30]}: {remaining/60:.0f}min left", "DIAG")
            continue

        age_h     = sig.get("age_h", 0)
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

        bet = sig["bet"]
        if bet > S.env().paper_bankroll * 0.95 or S.env().paper_bankroll < MIN_BET:
            if S.env().paper_bankroll < MIN_BET:
                events.append(("WARN", f"⚠ Bankroll too low (${S.env().paper_bankroll:.2f})", "#ffaa00"))
            break

        cur    = sig["cur"]
        asset  = sig.get("asset", "")

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
        liq      = sig.get("mkt", {}).get("liq", 0)
        ev_info  = estimate_expected_value(cur, sig.get("avg_entry", cur), liq, bet, mkt_type, sig.get("avg_wscore", 0.85))
        is_spike = sig.get("is_hft", False) and sig.get("has_large_trade", False)
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

        elite_wallet_addrs = list(sig.get("elite_ver", {}).keys())
        all_whale_addrs    = list(sig.get("ver", {}).keys())
        elite_names = [
            S.env().wallet_cache.get(w, {}).get("name", w[:10]+"…")
            for w in elite_wallet_addrs[:3]
        ]

        is_conviction = sig.get("has_large_trade", False)
        mkt_obj       = sig.get("mkt", {})
        resolved_slug = mkt_obj.get("slug") or sig.get("slug", "")

        elite_ver = sig.get("elite_ver", {})
        whale_buy_cash = {
            w.lower(): t.get("cash", 0)
            for w, t in elite_ver.items()
        }

        # v10: Get stop_loss_pct from signal strategy config
        sig_stop_loss = sig.get("stop_loss_pct")  # None for RF/DD, -0.35 for CB
        market_url = _build_market_url(event_slug, resolved_slug)
        entry_audit = _build_entry_audit(sig, cur, shares, bet, ev_info, now_t, S.env().paper_bankroll)

        pos = {
            "title":             title,
            "slug":              resolved_slug,
            "cid":               cid,
            "asset":             asset,
            "event_slug":        event_slug,
            "outcome":           outcome,
            "tier":              tier,
            "strategy":          strat,   # v10: track which strategy opened this position
            "stop_loss_pct":     sig_stop_loss,  # v10: per-signal stop loss
            "score":             sig["score"],
            "entry_price":       cur,
            "cur_price":         cur,
            "shares":            shares,
            "bet":               bet,
            "entry_ts":          now_t,
            "entry_ts_str":      buy_dtf["ts_str"],
            "entry_ts_iso":      buy_dtf["ts_iso"],
            "entry_date":        buy_dtf["date"],
            "entry_time":        buy_dtf["time"],
            "whale_wallets":     all_whale_addrs,
            "elite_wallets":     elite_wallet_addrs,
            "elite_names":       elite_names,
            "whale_buy_cash":    whale_buy_cash,
            "n_elite":           sig.get("n_elite", 0),
            "n_confluence":      sig.get("n_confluence", 0),
            "is_hft":            sig.get("is_hft", False),
            "is_conviction":     is_conviction,
            "mkt_type":          mkt_type,
            "is_sports":         sig.get("is_sports", False),
            "conviction_detail": sig.get("conviction_detail", ""),
            "ev_info":           ev_info,
            "avg_entry":         sig.get("avg_entry", cur),
            "ver_flow":          sig.get("ver_flow", 0),
            "exits":             [],
            "reason":            None,
            "market_fail_count": 0,
            "price_history":     [(now_t, cur)],
            "peak_pnl_pct":      0.0,
            "liq":               sig["mkt"].get("liq", 0),
            "volume":            sig["mkt"].get("volume", 0),
            "hrs_left":          sig["mkt"].get("hrs_left"),
            "end_date":          sig["mkt"].get("end_date", ""),
            "market_url":        market_url,
            "entry_audit":       entry_audit,
        }

        # v10: drift_discount strategy — store whale avg entry for reference
        if strat == "drift_discount":
            pos["whale_avg_entry"] = sig.get("avg_entry", cur)
            pos["drift_discount_pct"] = sig.get("drift_discount_pct", 0)

        # v10: recent_form strategy — store source whale recent win rate
        if strat == "recent_form":
            pos["source_recent_wr"] = sig.get("source_recent_wr", 0.55)

        S.env().open_positions[key]    = pos
        S.env().active_market_cids.add(cid)
        S.env().position_whale_map[cid] = set(all_whale_addrs)

        # WS: subscribe to real-time resolution events
        _ws_sub = _get_ws_monitor()
        if _ws_sub and asset:
            tokens = [asset]
            mkt_cached = S.market_cache.get(cid, {})
            for tid in mkt_cached.get("asset_to_price", {}).keys():
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

        trade_record = {
            "type":          "BUY",
            "title":         title,
            "outcome":       outcome,
            "entry_price":   cur,
            "exit_price":    None,
            "shares":        shares,
            "bet":           bet,
            "pnl_usdc":      None,
            "pnl_pct":       None,
            "reason":        f"AUTO_{tier}",
            "ts":            now_t,
            "ts_str":        buy_dtf["ts_str"],
            "ts_iso":        buy_dtf["ts_iso"],
            "date":          buy_dtf["date"],
            "time":          buy_dtf["time"],
            "entry_ts":      now_t,
            "entry_ts_str":  buy_dtf["ts_str"],
            "entry_ts_iso":  buy_dtf["ts_iso"],
            "entry_date":    buy_dtf["date"],
            "entry_time":    buy_dtf["time"],
            "bankroll":      round(S.env().paper_bankroll, 4),
            "tier":          tier,
            "strategy":      strat,  # v10
            "stop_loss_pct": sig_stop_loss,  # v10
            "elite_wallets": elite_wallet_addrs,
            "whale_names":   elite_names,
            "whale_buy_cash": whale_buy_cash,
            "avg_entry":     sig.get("avg_entry", cur),
            "score":         sig.get("score", 0),
            "n_confluence":  sig.get("n_confluence", 0),
            "is_conviction": is_conviction,
            "market_url":    market_url,
            "entry_audit":   entry_audit,
        }
        DB.append_trade(trade_record)

        n_conf   = sig.get("n_confluence", 0)
        hft_tag  = "⚡HFT " if sig.get("is_hft") else ""
        conv_tag = "💎CONVICTION " if is_conviction else ""
        conf_str = f" +{n_conf}conf" if n_conf else ""
        age_min  = sig.get("age_h", 0) * 60
        sl_str   = f" SL:{sig_stop_loss*100:.0f}%" if sig_stop_loss is not None else " SL:none"

        events.append((
            "OPEN",
            f"🛒 BUY [{strat}]: {title[:30]} [{outcome}] @ ${cur:.4f} "
            f"| ${bet:.2f} | {shares:.1f}sh | [{hft_tag}{conv_tag}{tier} {sig['score']:.0f}pts] "
            f"| {', '.join(elite_names)}{conf_str} | {age_min:.0f}min ago{sl_str}",
            "#00ff88"
        ))
        if S.on_position_open:
            S.on_position_open(pos)

    if events or to_close:
        save_state()

    if S.env().cycle_count % 10 == 0:
        save_whale_roster_async()

    return events
