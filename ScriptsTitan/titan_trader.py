"""
TITAN — Auto paper trading. Single-wallet edition.

EXIT PHILOSOPHY — FOLLOW THE WHALE:
  When you enter because a whale bought, you exit when THAT whale sells.
  That's it. No stop-loss, no profit target kills you early if the whale is
  still in the trade. The whale has more information than any price algorithm.

  Rules:
    1. WHALE_EXIT_SELL=true  → exit immediately when the triggering elite sells
    2. PROFIT_TARGET_PCT     → take profit even if whale still holds (optional guard)
    3. STOP_LOSS_ENABLED     → if false, stops are completely disabled
    4. Market resolving       → always exit (it's done)
    5. Market expiring        → exit when < MIN_HOURS_LEFT
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




def _try_fetch_resolution_price(cid: str, asset: str, outcome: str) -> float | None:
    """
    FIX: Phantom profit prevention + stale-price resolution detection.
    When Gamma API is down (422 flood), we use the Data API which continues
    working even after market resolution, independent of Gamma API.

    Returns the current outcome price (near 0 or 1 if resolved), or None if unknown.
    """
    try:
        # Strategy 1: Asset/token ID lookup via clob_token_ids (most reliable)
        # The token price goes to 1.0 if that outcome won, 0.0 if it lost.
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

        # Strategy 2: Data API trades for this conditionId — look for near-0/1 prices
        # which appear when a market resolves (losing tokens trade near 0.01)
        data = S.safe_get(f"{DATA_API}/trades", {
            "conditionId": cid,
            "limit": 10,
        })
        if data and isinstance(data, list):
            for t in data:
                price = float(t.get("price") or 0)
                t_outcome = t.get("outcome", "")
                t_asset = t.get("asset", "")
                if price >= 0.97:
                    if (t_outcome.lower() == outcome.lower() or
                            (asset and t_asset == asset)):
                        return price  # We hold the winning side
                    else:
                        return 0.01  # We hold the losing side
                elif price <= 0.03:
                    if (t_outcome.lower() == outcome.lower() or
                            (asset and t_asset == asset)):
                        return price  # We hold the losing side
                    else:
                        return 0.99  # We hold the winning side

        # Strategy 3: Positions endpoint — check our own position's current value
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

    Stage 1 (fast — every cycle): fetch_position_price_fast() using:
      a) Data API /positions endpoint (direct token value)
      b) Gamma clob_token_ids (bypasses broken conditionId 422s)
      c) Data API recent trades WITH price inversion for opposite outcomes

    Stage 2 (slow — when fast path fails): full get_market() Gamma refresh.

    Returns (price, is_resolving).
    """
    from titan_market import fetch_position_price_fast
    cid     = pos.get("cid")
    outcome = pos.get("outcome", "")
    asset   = pos.get("asset", "")
    title   = pos.get("title", "")
    stale_price = pos.get("cur_price", pos.get("entry_price", 0.5))

    # Stage 1: Fast path — runs every cycle, lightweight
    fast_price = fetch_position_price_fast(cid, asset, outcome)
    if fast_price is not None:
        resolving = fast_price <= 0.03 or fast_price >= 0.97
        pos["market_fail_count"] = 0
        return fast_price, resolving

    # Stage 2: Full Gamma refresh (bust stale cache first)
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

        # (a) Market resolving — always exit
        if resolving:
            reason = f"MARKET_RESOLVING cur={cur:.3f}"

        # (b) WHALE EXIT — the most important signal.
        # If the whale who triggered our entry has sold, we follow immediately.
        # This fires regardless of STOP_LOSS_ENABLED or profit targets.
        elif C.WHALE_EXIT_SELL and whale_exits.get(cid):
            exiting         = set(whale_exits[cid])
            elite_entry_set = set(w.lower() for w in pos.get("elite_wallets", []))
            matched_elite   = list(exiting & elite_entry_set)

            if matched_elite:
                # Filter out HFT/market-maker exits — they sell constantly as part of arb
                non_hft_exiting = [
                    w for w in matched_elite
                    if not (S.env().wallet_cache.get(w, {}).get("hft") or
                            is_hft_wallet(S.env().wallet_cache.get(w, {})) or
                            w in _KNOWN_HEDGE_WALLETS)
                ]
                if non_hft_exiting:
                    # Ignore early noise: if we're barely underwater and have barely held
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

        # (c) Profit target — disabled. Let whale decide when to exit.
        # When the market resolves YES, price hits 0.98+ and MARKET_RESOLVING fires.
        # elif pnl_pct >= C.PROFIT_TARGET_PCT:  # DISABLED

        # (d) Trailing stop — disabled. Trust the whale.
        # If they're still in, there's still upside. Price volatility is not an exit signal.

        # (e) Dynamic stop loss based on market type.
        # Sports: tight -25% stop (games resolve fast, no recovery possible).
        # Crypto: -35% stop (volatile but can recover, whale may still be right).
        # Politics/Event: -50% stop (or rely on whale exit — slow-moving markets).
        # STOP_LOSS_ENABLED=true enables this regardless of whale status.
        # CRITICAL: For sports, always apply stop loss — whale can't exit fast enough.
        mkt_type_pos = pos.get("mkt_type", "POLITICS")
        if mkt_type_pos == "SPORTS":
            _sport_stop = -0.25   # -25% for sports — fast stop
            if not reason and pnl_pct <= _sport_stop:
                reason = f"SPORTS_STOP_LOSS {pnl_pct*100:.1f}%"
        elif not reason and C.STOP_LOSS_ENABLED and pnl_pct <= C.STOP_LOSS_PCT:
            reason = f"STOP_LOSS {pnl_pct*100:.1f}%"

        # (f) Expiring soon
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
                    # After just 3 failures, try to get resolution price from Data API
                    # This is cheap and catches resolved markets quickly
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
                        # FIX: MARKET_GONE phantom profit bug.
                        # Before triggering exit, try to fetch the real final price
                        # from the Data API trades endpoint (works even after resolution,
                        # independent of Gamma API). If we get a real price, use it.
                        # If not, force exit at entry_price (0% PnL) — conservative
                        # but honest: we know nothing about the real outcome.
                        # This prevents the Sweeny-style phantom profit where a stale
                        # pre-resolution high price was used as the exit price.
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
                            # No reliable price available — exit at entry (0% PnL)
                            # This is conservative but prevents phantom profits.
                            cur = pos.get("entry_price", entry)
                            pos["cur_price"] = cur
                            pnl_pct = 0.0
                            S._log(
                                f"  ⚠ MARKET_GONE with no real price — exiting at entry "
                                f"to avoid phantom PnL: {pos.get('title','?')[:30]}",
                                "WARN"
                            )
                        reason = "MARKET_GONE"

        # (g) Near-zero price = resolved against us
        if not reason and cur <= 0.03 and hold_minutes > 10:
            reason = f"RESOLVED_LOSS cur={cur:.3f}"

        # (g2) CATASTROPHIC LOSS GUARD — exit any position down > 70%.
        # Covers the 0.03-0.10 range that is resolved-but-not-detected.
        # Sports: games end, prices go to ~0.03, whale can't exit in time.
        if not reason and pnl_pct <= -0.70 and hold_minutes > 3:
            reason = f"CATASTROPHIC_LOSS {pnl_pct*100:.1f}% cur={cur:.3f}"
            S._log(
                f"  🛑 Catastrophic loss guard: {pos.get('title','?')[:35]} "
                f"P&L={pnl_pct*100:+.1f}% cur=${cur:.4f}",
                "WARN"
            )

        # (h) Stale-price resolution check: position is deep ITM (entry was low,
        # stale price is still mid-range) AND Gamma API keeps failing.
        # Example: "Bitcoin $80k April 20-26 [Yes]" — entered at 0.71, market
        # resolved YES at ~0.99 but Gamma returns 422 so price never updates.
        # After 5 API failures, try Data API to confirm resolution.
        if not reason and pos.get("market_fail_count", 0) >= 5:
            stale_p = pos.get("cur_price", 0.5)
            # Only probe if price is in ambiguous mid-range (not already near resolved)
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

        S.env().paper_bankroll += bet + pnl_usdc_net
        S.env().session_pnl    += pnl_usdc_net

        trade_record = {
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
            "ts_str":        datetime.now().strftime("%H:%M:%S"),
            "bankroll":      round(S.env().paper_bankroll, 4),
            "tier":          pos.get("tier", "?"),
            "elite_wallets": pos.get("elite_wallets", []),
            "whale_names":   [
                S.env().wallet_cache.get(w, {}).get("name", w[:10]+"…")
                for w in pos.get("elite_wallets", [])[:3]
            ],
            "avg_entry":     pos.get("avg_entry", pos.get("entry_price", 0)),
        }
        S.env().trade_history.append(trade_record)
        S.env().active_market_cids.discard(cid_out)
        S.env().position_whale_map.pop(cid_out, None)
        del S.env().open_positions[key]
        S.env().cooldown_cids[cid_out] = now_t

        record_whale_trade_performance(pos.get("elite_wallets", []), pnl_usdc_net, won=(pnl_usdc_net >= 0))

        emoji = "✅" if pnl_usdc_net >= 0 else "❌"
        whale_str = ", ".join(trade_record["whale_names"])
        events.append((
            "CLOSE",
            f"{emoji} SELL: {pos['title'][:30]} [{pos['outcome']}] "
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

    for sig in signals:
        tier    = sig["tier"]
        cid     = sig["cid"]
        outcome = sig["outcome"]
        key     = (cid, outcome)
        title   = sig["title"]

        if len(S.env().open_positions) + len(opening_this_cycle) >= C.MAX_OPEN_POSITIONS:
            break

        if tier not in TRADEABLE_TIERS:
            continue

        mkt_type = sig.get("mkt_type", "POLITICS")
        if C.BLOCK_SPORTS and mkt_type == "SPORTS":
            continue
        if C.ALLOWED_MARKET_TYPES and mkt_type not in C.ALLOWED_MARKET_TYPES:
            continue

        if sig.get("n_elite", 0) < C.MIN_ELITE_CONFLUENCE:
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
        age_limit = HFT_MIRROR_DELAY_MAX_SECONDS / 3600 if tier == "HFT" else MAX_SIGNAL_AGE_H
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

        # EV gate — skip for HFT spike signals (momentum, not mispricing)
        liq      = sig.get("mkt", {}).get("liq", 0)
        ev_info  = estimate_expected_value(cur, sig.get("avg_entry", cur), liq, bet, mkt_type)
        is_spike = sig.get("is_hft", False) and sig.get("has_large_trade", False)
        if not ev_info["tradeable"] and not is_spike:
            S._log(
                f"  📊 EV REJECT: {title[:30]} EV={ev_info['ev_pct']:+.1f}% "
                f"(friction={ev_info['total_friction']:.1f}%)",
                "INFO"
            )
            continue
        else:
            S._log(
                f"  ✅ EV OK: {title[:30]} EV={ev_info['ev_pct']:+.1f}% "
                f"fair_prob={ev_info['fair_prob']:.0f}% friction={ev_info['total_friction']:.1f}%",
                "DIAG"
            )

        shares = (bet / max(cur, 0.01)) * (1 - TAKER_FEE_RATE)
        S.env().paper_bankroll -= bet

        elite_wallet_addrs = list(sig.get("elite_ver", {}).keys())
        all_whale_addrs    = list(sig.get("ver", {}).keys())
        elite_names = [
            S.env().wallet_cache.get(w, {}).get("name", w[:10]+"…")
            for w in elite_wallet_addrs[:3]
        ]

        is_conviction = sig.get("has_large_trade", False)
        mkt_obj       = sig.get("mkt", {})
        resolved_slug = mkt_obj.get("slug") or sig.get("slug", "")

        # Build per-whale buy cash map for exit detection — maps wallet → their buy cash on this market
        elite_ver = sig.get("elite_ver", {})
        whale_buy_cash = {
            w.lower(): t.get("cash", 0)
            for w, t in elite_ver.items()
        }

        pos = {
            "title":             title,
            "slug":              resolved_slug,
            "cid":               cid,
            "asset":             asset,
            "event_slug":        event_slug,
            "outcome":           outcome,
            "tier":              tier,
            "score":             sig["score"],
            "entry_price":       cur,
            "cur_price":         cur,
            "shares":            shares,
            "bet":               bet,
            "entry_ts":          now_t,
            "entry_ts_str":      datetime.now().strftime("%H:%M:%S"),
            "whale_wallets":     all_whale_addrs,
            "elite_wallets":     elite_wallet_addrs,
            "elite_names":       elite_names,
            "whale_buy_cash":    whale_buy_cash,   # per-whale buy cash for exit detection
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
        }
        S.env().open_positions[key]    = pos
        S.env().active_market_cids.add(cid)
        S.env().position_whale_map[cid] = set(all_whale_addrs)

        opening_this_cycle.add(key)
        opening_cids_this_cycle.add(cid)
        open_cids.add(key)
        if event_slug:
            opening_event_slugs[event_slug] = opening_event_slugs.get(event_slug, 0) + 1
        for w in elite_wallet_addrs:
            opening_whale_counts[w] = opening_whale_counts.get(w, 0) + 1

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
            "ts_str":        datetime.now().strftime("%H:%M:%S"),
            "bankroll":      round(S.env().paper_bankroll, 4),
            "tier":          tier,
            "elite_wallets": elite_wallet_addrs,
            "whale_names":   elite_names,
            "avg_entry":     sig.get("avg_entry", cur),
            "score":         sig.get("score", 0),
            "n_confluence":  sig.get("n_confluence", 0),
            "is_conviction": is_conviction,
        }
        S.env().trade_history.append(trade_record)

        n_conf   = sig.get("n_confluence", 0)
        hft_tag  = "⚡HFT " if sig.get("is_hft") else ""
        conv_tag = "💎CONVICTION " if is_conviction else ""
        conf_str = f" +{n_conf}conf" if n_conf else ""
        age_min  = sig.get("age_h", 0) * 60

        events.append((
            "OPEN",
            f"🛒 BUY: {title[:30]} [{outcome}] @ ${cur:.4f} "
            f"| ${bet:.2f} | {shares:.1f}sh | [{hft_tag}{conv_tag}{tier} {sig['score']:.0f}pts] "
            f"| {', '.join(elite_names)}{conf_str} | {age_min:.0f}min ago",
            "#00ff88"
        ))
        if S.on_position_open:
            S.on_position_open(pos)

    if events or to_close:
        save_state()

    if S.env().cycle_count % 10 == 0:
        save_whale_roster_async()

    return events