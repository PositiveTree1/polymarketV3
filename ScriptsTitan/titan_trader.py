"""
TITAN — Auto paper trading: position open/close. v9 OVERHAUL:

KEY FIXES:
  1. MARKET_RESOLVED_OR_GONE: Raised threshold from 3 to 10 consecutive
     API failures. Cross-checks stale price — only auto-closes if price
     is near 0 or 1 (actually resolving). This was killing live positions.

  2. SMART WHALE EXIT: Ignores exits from known HFT/market-maker wallets.
     Only exits on elite whale sell if they sold a significant fraction.

  3. TRAILING STOP: Once a position is up +15%, lock in a +5% trail.

  4. MARKET-TYPE EXITS: Sports get tighter stops (-15%/+10%).
     Politics/crypto get wider stops (-20%/+30%).

  5. PRE-ENTRY EV GATE: Uses estimate_expected_value to reject -EV trades.
"""

import time
from datetime import datetime
import titan_state as S
from titan_config import *
from titan_market import get_market, get_outcome_price, is_market_resolving
from titan_signals import classify_market, estimate_expected_value, _KNOWN_HEDGE_WALLETS
from titan_wallet import is_hft_wallet, record_whale_trade_performance


from titan_persistence import save_state, save_whale_roster_async


def _get_current_price(pos: dict) -> tuple[float, bool]:
    """
    Get current price for a position.
    Returns (price, is_resolving).

    KEY FIX v9: Asset-first lookup strategy.
    The `asset` (token ID / clob_token_id) is unique to the exact outcome token.
    Gamma accepts ?clob_token_ids=[asset] and returns the correct market.
    This is far more reliable than slug lookup, which can return the wrong
    market within the same event, and avoids 422s from conditionId lookups.

    When the market API fails, we preserve the last known price (stale) rather
    than falling back to entry_price, which was masking all P&L.
    """
    cid     = pos.get("cid")
    outcome = pos.get("outcome", "")
    asset   = pos.get("asset", "")
    title   = pos.get("title", "")
    entry   = pos.get("entry_price", 0.5)
    # Use last known price as stale fallback — NOT entry. This ensures P&L
    # is preserved across API failures rather than resetting to +0%.
    stale_price = pos.get("cur_price", entry)

    # Only bypass cache if it's older than 20s (avoid hammering on 422 storms)
    cached = S.env().market_cache.get(cid)
    if cached and (time.time() - cached.get("ts", 0)) > 20:
        S.env().market_cache.pop(cid, None)

    # Strategy: try asset first (clob_token_ids lookup), then slug, then cid
    mkt, err = get_market(cid, title, asset=asset, slug=pos.get("slug", ""))
    if not mkt:
        S._log(f"  ⚠ Price fetch failed for {title[:30]}: {err}", "DIAG")
        pos["market_fail_count"] = pos.get("market_fail_count", 0) + 1
        return stale_price, False  # v9 FIX: return stale, not entry

    # Confirmed successful API call — reset fail counter
    pos["market_fail_count"] = 0

    resolving = is_market_resolving(mkt)

    # 1. Asset/token ID lookup — most reliable, avoids wrong-market confusion
    if asset:
        ap = mkt.get("asset_to_price", {})
        if asset in ap:
            price = ap[asset]
            # Sanity: resolving detection using the actual outcome price
            if price <= 0.02:
                return price, True   # outcome lost
            if price >= 0.98:
                return price, True   # outcome won
            return price, resolving

    # 2. Label-based lookup with token-index correction
    cur = get_outcome_price(mkt, outcome, asset=asset)

    # Correct for label[1] = no_price side
    labels = mkt.get("outcome_labels", [])
    if len(labels) >= 2 and not resolving:
        lbl1 = str(labels[1])
        if outcome.lower() == lbl1.lower() or outcome.strip() == lbl1.strip():
            cur = mkt.get("no_price", 1.0 - mkt["yes_price"])

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
        outcome  = pos.get("outcome", key[1])
        entry    = pos["entry_price"]
        bet      = pos["bet"]
        shares   = pos["shares"]
        entry_ts = pos.get("entry_ts", 0)

        hold_minutes = (now_t - entry_ts) / 60
        if hold_minutes < MIN_HOLD_MINUTES:
            continue

        # v9 FIX: Get current price with asset-based lookup and resolving check
        cur, resolving = _get_current_price(pos)
        pos["cur_price"] = cur
        # Only reset fail counter on a confirmed successful price fetch
        # (not just on price != entry_price, which masked collapses at stale prices)
        if cur is not None and cur != pos.get("entry_price", -1) or resolving:
            pass  # fail counter reset is handled inside _get_current_price on success

        if "price_history" not in pos:
            pos["price_history"] = []
        pos["price_history"].append((now_t, cur))
        if len(pos["price_history"]) > 1440:
            del pos["price_history"][:-1440]

        pnl_pct = (cur - entry) / max(entry, 0.001)
        reason  = None

        # (a) Market resolving — exit immediately
        if resolving:
            reason = f"MARKET_RESOLVING cur={cur:.3f}"

        # (b) Profit target
        elif pnl_pct >= PROFIT_TARGET_PCT:
            reason = f"PROFIT_TARGET +{pnl_pct*100:.1f}%"

        # (c) Elite whale exit — v9: SMART EXIT DETECTION
        # Ignore exits from HFT/market-maker wallets (they cycle inventory constantly).
        # Only react to elite exits where the whale is clearly abandoning the position.
        elif WHALE_EXIT_SELL and whale_exits.get(cid):
            exiting          = set(whale_exits[cid])
            elite_entry_set  = set(w.lower() for w in pos.get("elite_wallets", []))
            matched_elite    = list(exiting & elite_entry_set)

            all_entry     = set(w.lower() for w in pos.get("whale_wallets", []))
            non_elite_ex  = list(exiting & (all_entry - elite_entry_set))
            if non_elite_ex:
                names = [S.env().wallet_cache.get(a, {}).get("name", a[:10] + "…") for a in non_elite_ex[:2]]
                S._log(f"  ℹ️ Non-elite sold {pos['title'][:30]} — not exiting: {names}", "DIAG")

            if matched_elite:
                # v9: Filter out HFT/market-maker exits — they sell constantly
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
                            S.env().wallet_cache.get(a, {}).get("name", a[:10] + "…")
                            for a in non_hft_exiting[:2]
                        ]
                        reason = f"ELITE_EXIT {matched_names}"
                    else:
                        S._log(
                            f"  🐋 Ignoring early noise exit: {pos['title'][:30]} "
                            f"pnl={pnl_pct*100:+.1f}% hold={hold_minutes:.0f}min",
                            "DIAG"
                        )
                else:
                    hft_names = [S.env().wallet_cache.get(w, {}).get("name", w[:10]) for w in matched_elite[:2]]
                    S._log(
                        f"  ⚡ HFT/market-maker exit ignored: {hft_names} on {pos['title'][:30]}",
                        "DIAG"
                    )

        # (d) Trailing stop — v9: lock in profits on winning positions
        # Once pnl reaches +15%, we track the peak and exit if it drops 10% from peak.
        # This prevents watching winning trades turn into losers.
        elif pnl_pct > 0:
            peak = pos.get("peak_pnl_pct", 0.0)
            if pnl_pct > peak:
                pos["peak_pnl_pct"] = pnl_pct
                peak = pnl_pct
            # Only activate trailing stop after sufficient profit has been seen
            _TRAIL_ACTIVATE = 0.15   # activate when up +15%
            _TRAIL_DISTANCE = 0.10   # exit when price drops 10% from peak
            if peak >= _TRAIL_ACTIVATE:
                trail_floor = peak - _TRAIL_DISTANCE
                if pnl_pct <= trail_floor:
                    reason = f"TRAILING_STOP peak={peak*100:.1f}% now={pnl_pct*100:.1f}%"

        # (e) Stop loss
        if not reason and STOP_LOSS_ENABLED and pnl_pct <= STOP_LOSS_PCT:
            reason = f"STOP_LOSS {pnl_pct*100:.1f}%"

        # (f) Expiring soon — re-fetch market for accurate hrs_left
        if not reason:
            # NOTE: We already called get_market above for price. Reuse or refetch.
            mkt_check, mkt_err = get_market(cid, pos.get("title"), asset=pos.get("asset", ""), slug=pos.get("slug", ""))
            if mkt_check:
                pos["market_fail_count"] = 0   # reset on success
                hrs = mkt_check.get("hrs_left")
                # Use MIN_HOURS_LEFT + small buffer — don't exit 1h early
                if hrs is not None and hrs < max(MIN_HOURS_LEFT, 0.35):
                    reason = "EXPIRING_SOON"
            else:
                pos["market_fail_count"] = pos.get("market_fail_count", 0) + 1
                # v9: Raised from 3 to 10 consecutive failures
                # The Gamma API 422s spam constantly and were falsely killing positions
                fail_count = pos["market_fail_count"]
                if fail_count >= 10:
                    # Cross-check: only close if stale price suggests resolution
                    stale_p = pos.get("cur_price", 0.5)
                    if stale_p <= 0.05 or stale_p >= 0.95:
                        reason = "MARKET_RESOLVED_OR_GONE"
                        S._log(f"  💀 Closing after {fail_count} API fails + stale={stale_p:.3f}: {pos['title'][:30]}", "WARN")
                    elif fail_count >= 20:
                        # After 20 failures (~5+ minutes), force close regardless
                        reason = "MARKET_GONE"
                        S._log(f"  💀 Force close after {fail_count} API fails: {pos['title'][:30]}", "WARN")
                    else:
                        S._log(f"  ⚠ API fail #{fail_count} but stale={stale_p:.3f} (not resolving): {pos['title'][:30]}", "DIAG")

        # Force-close positions where price collapsed to near-zero (resolved against us)
        if not reason and cur <= 0.03 and hold_minutes > 10:
            reason = f"RESOLVED_LOSS cur={cur:.3f}"
            S._log(f"  💀 Force-close near-zero price: {pos['title'][:30]} cur={cur:.3f}", "WARN")

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
            "type":        "SELL",
            "title":       pos["title"],
            "outcome":     pos["outcome"],
            "entry_price": pos["entry_price"],
            "exit_price":  cur,
            "shares":      shares,
            "bet":         bet,
            "pnl_usdc":    round(pnl_usdc_net, 4),
            "pnl_pct":     round(pnl_pct * 100, 2),
            "reason":      reason,
            "ts":          now_t,
            "ts_str":      datetime.now().strftime("%H:%M:%S"),
            "bankroll":    round(S.env().paper_bankroll, 4),
            "tier":        pos.get("tier", "?"),
            "elite_wallets": pos.get("elite_wallets", []),
            "whale_names":   [
                S.env().wallet_cache.get(w, {}).get("name", w[:10]+"…")
                for w in pos.get("elite_wallets", [])[:3]
            ],
        }
        S.env().trade_history.append(trade_record)
        S.env().active_market_cids.discard(cid_out)
        S.env().position_whale_map.pop(cid_out, None)
        del S.env().open_positions[key]
        S.env().cooldown_cids[cid_out] = now_t

        # v9: Record whale performance for tracking which sources make us money
        elite_wallets_for_tracking = pos.get("elite_wallets", [])
        record_whale_trade_performance(
            elite_wallets_for_tracking,
            pnl_usdc_net,
            won=(pnl_usdc_net >= 0)
        )

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
    # Only auto-trade genuine CONVICTION signals.
    # v9: Also allow ALERT tier for non-sports markets where EV > 0.
    TRADEABLE_TIERS = {"CONVICTION"}

    open_cids:  set = set(S.env().open_positions.keys())
    opening_this_cycle: set = set()
    opening_cids_this_cycle: set = set()
    closed_this_cycle_cids: set = {key[0] for key, _, _, _, _ in to_close}

    # Track event slug counts
    open_event_slugs: dict = {}
    for pos in S.env().open_positions.values():
        es = pos.get("event_slug", "")
        if es:
            open_event_slugs[es] = open_event_slugs.get(es, 0) + 1
    opening_event_slugs: dict = {}

    # Per-whale cap: prevents one wallet flooding all slots (e.g. elkmonkey on 20 games)
    whale_position_counts: dict = {}
    for pos in S.env().open_positions.values():
        for w in pos.get("elite_wallets", []):
            whale_position_counts[w] = whale_position_counts.get(w, 0) + 1
    opening_whale_counts: dict = {}
    _whale_cap = MAX_POSITIONS_PER_WHALE

    for sig in signals:
        tier    = sig["tier"]
        cid     = sig["cid"]
        outcome = sig["outcome"]
        key     = (cid, outcome)
        title   = sig["title"]

        if len(S.env().open_positions) + len(opening_this_cycle) >= MAX_OPEN_POSITIONS:
            break

        if tier not in TRADEABLE_TIERS:
            continue

        # Skip if an elite on this signal has already exited this market
        exits_here       = sig.get("exits_detected", [])
        elite_wallet_set = set(sig.get("elite_ver", {}).keys())
        if elite_wallet_set & set(exits_here):
            S._log(f"  🚫 Elite exit-block: {title[:30]} {outcome}", "DIAG")
            continue

        if cid in S.env().active_market_cids:
            continue
        if cid in opening_cids_this_cycle:
            S._log(f"  🚫 CID already opening this cycle: {title[:30]}", "DIAG")
            continue

        if key in open_cids or key in opening_this_cycle:
            continue

        # Event slug cap
        event_slug = sig.get("event_slug", "")
        if event_slug:
            already = open_event_slugs.get(event_slug, 0) + opening_event_slugs.get(event_slug, 0)
            if already >= MAX_POSITIONS_PER_EVENT:
                S._log(f"  🚫 Event limit {MAX_POSITIONS_PER_EVENT}: {title[:30]}", "DIAG")
                continue

        # Per-whale cap: skip if the sole triggering elite already has enough open slots
        sig_elites = list(sig.get("elite_ver", {}).keys())
        if sig_elites:
            # Only apply cap when there's exactly one elite (pure single-whale signal)
            # Multi-elite confluence signals are more trustworthy so let them through
            if len(sig_elites) == 1:
                w0 = sig_elites[0]
                used = whale_position_counts.get(w0, 0) + opening_whale_counts.get(w0, 0)
                if used >= _whale_cap:
                    w_name = S.env().wallet_cache.get(w0, {}).get("name", w0[:10] + "…")
                    S._log(f"  🚫 Whale cap ({_whale_cap}): {w_name} already has {used} open — {title[:30]}", "DIAG")
                    continue

        if cid in closed_this_cycle_cids:
            S._log(f"  🚫 Just-closed: {title[:30]}", "DIAG")
            continue

        if cid in S.env().cooldown_cids:
            remaining = EXIT_COOLDOWN_SECONDS - (now_t - S.env().cooldown_cids[cid])
            S._log(f"  ⏳ Cooldown {title[:30]}: {remaining/60:.0f}min left", "DIAG")
            continue

        age_h = sig.get("age_h", 0)
        age_limit = HFT_MIRROR_DELAY_MAX_SECONDS / 3600 if tier == "HFT" else MAX_SIGNAL_AGE_H
        if age_h > age_limit:
            S._log(f"  ⏰ Signal too old ({age_h:.1f}h): {title[:30]}", "DIAG")
            continue

        bet = sig["bet"]
        if bet > S.env().paper_bankroll * 0.95:
            continue
        if S.env().paper_bankroll < MIN_BET:
            events.append(("WARN", f"⚠ Bankroll too low (${S.env().paper_bankroll:.2f})", "#ffaa00"))
            break

        cur    = sig["cur"]
        asset  = sig.get("asset", "")
        mkt_type = sig.get("mkt_type", "POLITICS")

        # v9: Pre-entry EV gate — reject trades with negative expected value
        liq = sig.get("mkt", {}).get("liq", 0)
        ev_info = estimate_expected_value(cur, sig.get("avg_entry", cur), liq, bet, mkt_type)
        if not ev_info["tradeable"]:
            S._log(
                f"  📊 EV REJECT: {title[:30]} EV={ev_info['ev_pct']:+.1f}% "
                f"(friction={ev_info['total_friction']:.1f}%)",
                "DIAG"
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
            S.env().wallet_cache.get(w, {}).get("name", w[:10] + "…")
            for w in elite_wallet_addrs[:3]
        ]

        is_conviction = sig.get("has_large_trade", False)

        # v9 FIX: Use the slug from the fetched market object (more reliable
        # than trade record slug which may be an event slug, not market slug)
        mkt_obj       = sig.get("mkt", {})
        resolved_slug = mkt_obj.get("slug") or sig.get("slug") or sig["slug"]

        pos = {
            "title":             title,
            "slug":              resolved_slug,  # v9: prefer market-level slug
            "cid":               cid,
            "asset":             asset,          # v8: store for price refresh
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
            "n_elite":           sig.get("n_elite", 0),
            "n_confluence":      sig.get("n_confluence", 0),
            "is_hft":            sig.get("is_hft", False),
            "is_conviction":     is_conviction,
            "mkt_type":          mkt_type,       # v9: for tiered exits
            "conviction_detail": sig.get("conviction_detail", ""),
            "ev_info":           ev_info,          # v9: EV at entry for analysis
            "avg_entry":         sig.get("avg_entry", cur),
            "ver_flow":          sig.get("ver_flow", 0),
            "exits":             [],
            "reason":            None,
            "market_fail_count": 0,
            "price_history":     [(now_t, cur)],
            "peak_pnl_pct":      0.0,             # v9: trailing stop tracker
            "liq":               sig["mkt"].get("liq", 0),
            "volume":            sig["mkt"].get("volume", 0),
            "hrs_left":          sig["mkt"].get("hrs_left"),
            "end_date":          sig["mkt"].get("end_date", ""),
        }
        S.env().open_positions[key] = pos
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

        n_conf      = sig.get("n_confluence", 0)
        hft_tag     = "⚡HFT " if sig.get("is_hft") else ""
        conv_tag    = "💎CONVICTION " if is_conviction else ""
        conf_str    = f" +{n_conf}conf" if n_conf else ""
        age_min     = sig.get("age_h", 0) * 60

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