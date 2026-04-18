"""
TITAN — Signal building. v9 OVERHAUL:

KEY CHANGES FROM v8:
  1. MARKET TYPE CLASSIFICATION: Sports/Crypto/Politics/Event detection.
     Sports markets require stricter multi-whale confluence.
  2. PORTFOLIO-RELATIVE CONVICTION: A $500 bet from a $600k whale is NOT
     conviction. Conviction = whale commits >= 0.5% of their portfolio.
  3. HEDGE BOT TRACKING: Wallets that buy both sides are permanently flagged
     and their trades are never copied.
  4. PRE-ENTRY EV CHECK: Estimates expected value after spread/friction costs.
     Only enters trades with positive expected value.
  5. WHALE EXIT INTELLIGENCE: Distinguishes real exits from scalp cycling.

ARCHITECTURE:
  Each (conditionId, outcome) pair is ONE binary market position.
  A conditionId has exactly two outcomes: token 0 (yes_price) and token 1 (no_price).
  The `asset` field on a trade IS the token ID — map it to price via asset_to_price.
"""

import time
import re
import math
from collections import defaultdict
import titan_state as S
from titan_config import *
from titan_market import get_market, get_outcome_price, get_outcome_price_by_trade, fetch_wallet_sells
from titan_wallet import is_hft_wallet


# ─────────────────────────────────────────────────────────────────────────────
#  MARKET TYPE CLASSIFICATION
# ─────────────────────────────────────────────────────────────────────────────
_SPORTS_PATTERNS = re.compile(
    r'\b(vs\.?|spread|o/u|over|under|winner|set \d|game \d|bo[13]|'
    r'nhl|nba|mlb|nfl|ufc|atp|wta|epl|sea-|nhl-|mlb-|nba-|ufc-|atp-|'
    r'kings|flames|rays|yankees|royals|tigers|brewers|pirates|nationals|'
    r'lol:|valorant:|counter-strike:)\b',
    re.IGNORECASE
)
_CRYPTO_PATTERNS = re.compile(
    r'\b(bitcoin|btc|ethereum|eth|solana|xrp|bnb|crypto|up or down)\b',
    re.IGNORECASE
)

def classify_market(title: str, event_slug: str, hrs_left: float = None) -> str:
    """
    Classify a market into SPORTS, CRYPTO, POLITICS, or EVENT.
    Sports markets require stricter entry criteria.
    """
    combined = f"{title} {event_slug}"
    if _SPORTS_PATTERNS.search(combined):
        return "SPORTS"
    if _CRYPTO_PATTERNS.search(combined):
        return "CRYPTO"
    # Short-duration markets (< 24h to resolve) that aren't sports/crypto = EVENT
    if hrs_left is not None and hrs_left < 24:
        return "EVENT"
    return "POLITICS"


# ─────────────────────────────────────────────────────────────────────────────
#  PRE-ENTRY EXPECTED VALUE
# ─────────────────────────────────────────────────────────────────────────────
def estimate_expected_value(cur_price: float, avg_entry: float, liq: float,
                           bet_size: float, market_type: str) -> dict:
    """
    Estimate expected value of entering a trade.
    Returns dict with ev_dollar, ev_pct, spread_cost, impact_cost.
    Only enter if ev_dollar > 0.
    """
    # Spread cost estimate: tighter in liquid markets
    if liq > 100_000:
        spread_pct = 0.005   # 0.5% for very liquid
    elif liq > 20_000:
        spread_pct = 0.015   # 1.5% for medium
    else:
        spread_pct = 0.03    # 3% for illiquid

    # Impact cost: how much our bet moves the price
    if liq > 0:
        impact_pct = min(0.05, (bet_size / liq) * 2.0)
    else:
        impact_pct = 0.05

    # Total friction = spread + impact + round-trip fee
    total_friction = spread_pct + impact_pct + ROUND_TRIP_FEE

    # Expected edge from whale signal
    # If whale bought at avg_entry and price is now cur_price:
    # Our edge = (1/cur_price - 1) - friction  (for binary market paying $1 on win)
    # But we need to estimate probability of winning
    # Use avg_entry as fair value proxy (whale's implied probability)
    fair_prob = max(0.05, min(0.95, avg_entry))
    payout_if_win = (1.0 / max(cur_price, 0.01)) - 1.0  # profit per dollar if we win
    payout_if_lose = -1.0  # lose entire bet

    ev_per_dollar = fair_prob * payout_if_win + (1 - fair_prob) * payout_if_lose
    ev_after_friction = ev_per_dollar - total_friction
    ev_dollar = ev_after_friction * bet_size

    return {
        "ev_dollar": round(ev_dollar, 4),
        "ev_pct": round(ev_after_friction * 100, 2),
        "spread_cost": round(spread_pct * 100, 2),
        "impact_cost": round(impact_pct * 100, 2),
        "total_friction": round(total_friction * 100, 2),
        "fair_prob": round(fair_prob * 100, 1),
        "tradeable": ev_after_friction > 0.005,  # require > 0.5% EV
    }


# ─────────────────────────────────────────────────────────────────────────────
#  HEDGE BOT TRACKING (persistent across cycles)
# ─────────────────────────────────────────────────────────────────────────────
_KNOWN_HEDGE_WALLETS: set = set()  # wallets caught buying both sides


# ─────────────────────────────────────────────────────────────────────────────
#  EXIT MONITORING
# ─────────────────────────────────────────────────────────────────────────────
def check_whale_exits(cid_to_wallet_sets: dict, entry_times: dict = None) -> dict:
    """
    Returns {conditionId: [wallet_addresses_that_sold]}.
    Only checks SELL activity for wallets on open positions.
    """
    exits       = defaultdict(list)
    all_wallets = set()
    for wallets_set in cid_to_wallet_sets.values():
        all_wallets.update(wallets_set)

    if not entry_times:
        entry_times = {}

    global_cutoff = (
        min(entry_times.values()) - 300 if entry_times
        else time.time() - 24 * 3600
    )

    for wallet in all_wallets:
        sells = fetch_wallet_sells(wallet, global_cutoff, limit=200)
        if not sells:
            time.sleep(0.1)
            continue

        for sell in sells:
            ts  = sell["ts"]
            cid = sell["cid"]

            if not cid or cid not in cid_to_wallet_sets:
                asset = sell.get("asset", "")
                if asset and asset in cid_to_wallet_sets:
                    cid = asset
                else:
                    continue

            if wallet not in cid_to_wallet_sets.get(cid, set()):
                continue
            our_entry_ts = entry_times.get(cid, 0)
            if ts <= our_entry_ts:
                continue
            if wallet not in exits[cid]:
                exits[cid].append(wallet)
                w_name = S.env().wallet_cache.get(wallet, {}).get("name", wallet[:10])
                prof   = S.env().wallet_cache.get(wallet, {})
                tag    = "🔥" if prof.get("elite") else "✅" if prof.get("verified") else "👁"
                S.env().WHALE_EXIT_HISTORY.append(
                    f"[{time.strftime('%H:%M')}] {tag} {w_name} SOLD"
                )
                if len(S.env().WHALE_EXIT_HISTORY) > 30:
                    del S.env().WHALE_EXIT_HISTORY[:-30]
                S._log(f"🐋 EXIT DETECTED: {w_name} sold cid={cid[:20]}…", "INFO")

        time.sleep(0.08)

    if exits:
        S._log(f"🐋 Exit check done: {sum(len(v) for v in exits.values())} exits on {len(exits)} markets", "INFO")

    return exits


def whale_still_holding(wallet: str, cid: str) -> bool:
    """Returns True if this wallet has NOT sold this conditionId in the last 48h."""
    since = time.time() - 48 * 3600
    sells = fetch_wallet_sells(wallet, since, limit=100)
    for s in sells:
        if s["cid"] == cid or s.get("asset") == cid:
            return False
    return True


# ─────────────────────────────────────────────────────────────────────────────
#  SCORING
# ─────────────────────────────────────────────────────────────────────────────
def score_signal(s: dict) -> dict:
    bd = {}

    bd["wallet"] = round(s["avg_wscore"] * 30, 1)

    n_confluence = s.get("n_confluence", 0)
    bd["conf"] = (
        18 if n_confluence >= 4 else
        14 if n_confluence == 3 else
        10 if n_confluence == 2 else
        6  if n_confluence == 1 else
        0
    )
    if s.get("elite_only_mode"):
        bd["conf"] = max(bd["conf"], 8)

    # Large trade bonus on confluence
    if s.get("has_large_trade"):
        bd["conf"] = min(18, bd["conf"] + 4)

    age_h  = (time.time() - s["newest_ts"]) / 3600
    window = s.get("window", "warm")
    if window in ("hot", "hft"):
        bd["rec"] = (20 if age_h < 0.25 else 17 if age_h < 0.5 else
                     14 if age_h < 1   else  9 if age_h < 2   else
                      6 if age_h < 3   else  4 if age_h < 4   else 2)
    else:
        bd["rec"] = (6 if age_h < 4 else 4 if age_h < 6 else 2 if age_h < 8 else 1)

    # HFT recency bonus
    if s.get("is_hft") and (time.time() - s["newest_ts"]) <= HFT_MIRROR_DELAY_MAX_SECONDS:
        bd["rec"] = min(20, bd["rec"] + 5)

    drift = s["drift"]
    if drift < 0:
        bd["opp"] = max(0, min(15, 8 + int(abs(drift) * 25)))
    else:
        bd["opp"] = (15 if drift < 0.04 else 12 if drift < 0.08 else
                     8  if drift < 0.12 else  4 if drift < 0.15 else 0)

    mkt   = s["mkt"]
    liq_p = min(5.0, mkt["liq"]    / 8_000 * 5)
    vol_p = min(3.0, mkt["volume"] / 40_000 * 3)
    hrs   = mkt.get("hrs_left")
    t_p   = 2 if (hrs is None or hrs > 72) else 1 if hrs > 24 else 0
    bd["mkt"] = round(liq_p + vol_p + t_p, 1)

    mb = s["max_bet_cash"]
    bd["bonus"] = (5 if mb >= MASSIVE_TRADE else 2 if mb >= LARGE_TRADE else 0)
    # Extra bonus for large trades (3x+ whale avg)
    if s.get("has_large_trade"):
        bd["bonus"] = min(10, bd["bonus"] + 3)

    bd["exit_penalty"] = -(len(s.get("exits_same_side", [])) * 8)

    bd["total"] = round(min(100, max(0, sum(bd.values()))), 1)
    return bd


# ─────────────────────────────────────────────────────────────────────────────
#  BET SIZING
# ─────────────────────────────────────────────────────────────────────────────
def _adaptive_bet_caps():
    br = S.env().paper_bankroll
    if br < 30:    return 5.00,  0.12
    elif br < 75:  return 10.00, 0.12
    elif br < 200: return 22.00, 0.12
    elif br < 500: return 50.00, 0.15
    else:          return 120.00, 0.18


def kelly_bet(signal: dict, wallets: dict, score: float = None) -> float:
    cur        = signal["cur"]
    fair_value = signal["avg_entry"]
    score      = score if score is not None else signal.get("score", 50)
    tier       = signal.get("tier", "MEDIUM")
    is_large   = signal.get("has_large_trade", False)
    is_hft_sig = signal.get("is_hft", False)

    fair_value = max(0.03, min(0.97, fair_value))
    cur        = max(0.03, min(0.97, cur))

    if cur >= fair_value:
        return MIN_BET

    b     = max(0.05, (1.0 / cur) - 1.0 - ROUND_TRIP_FEE)
    kelly = max(0.0, fair_value - (1 - fair_value) / b)
    f_kelly = kelly * KELLY_FRACTION

    n_conf = signal.get("n_confluence", 0)
    cmult  = min(2.0, 1.0 + n_conf * 0.2)

    smult = 0.6 + 0.4 * (score / 100)

    total_ver_flow  = sum(t["cash"] for t in signal["ver"].values())
    total_portfolio = sum(
        wallets.get(w, {"total_value": 0}).get("total_value", 0)
        for w in signal["ver"]
    )
    port_mult = 1.0
    if total_portfolio > 0:
        port_fraction = total_ver_flow / total_portfolio
        port_mult = (1.6 if port_fraction >= 0.30 else
                     1.4 if port_fraction >= 0.15 else
                     1.2 if port_fraction >= 0.05 else 1.0)

    # v8: Separate sizing for HFT vs conviction vs large trades
    if is_large and not is_hft_sig:
        # Large conviction trade: use fuller Kelly, higher tier mult
        tier_mult = {"ALERT": 1.5, "STRONG": 1.3, "MEDIUM": 1.0, "ELITE_ONLY": 1.2, "HFT": 1.3}.get(tier, 1.0)
    elif is_hft_sig and not is_large:
        # Pure HFT mirror: use smaller sizing (whales are trading tiny)
        tier_mult = {"ALERT": 0.6, "STRONG": 0.5, "MEDIUM": 0.4, "ELITE_ONLY": 0.5, "HFT": 0.7}.get(tier, 0.5)
    else:
        tier_mult = {"ALERT": 1.2, "STRONG": 1.0, "MEDIUM": 0.7, "ELITE_ONLY": 0.9, "HFT": 1.1}.get(tier, 0.8)

    kelly_bet_raw = S.env().paper_bankroll * f_kelly * smult * cmult * port_mult * tier_mult

    prop_bet = MIN_BET
    if USE_PROPORTIONAL_SIZING:
        total_portfolio2 = sum(
            wallets.get(w, {"total_value": 0}).get("total_value", 0)
            for w in signal["ver"]
        )
        if total_portfolio2 > 0:
            whale_pct = sum(t["cash"] for t in signal["ver"].values()) / total_portfolio2
            prop_bet  = S.env().paper_bankroll * whale_pct * tier_mult * cmult
        bet = kelly_bet_raw * (1 - PROPORTIONAL_WEIGHT) + prop_bet * PROPORTIONAL_WEIGHT
    else:
        bet = kelly_bet_raw

    max_abs, max_pct = _adaptive_bet_caps()

    # For large conviction trades, allow higher absolute max
    if is_large and not is_hft_sig:
        max_abs = min(max_abs * 2.5, S.env().paper_bankroll * 0.25)

    # For pure HFT (non-large), cap lower
    if is_hft_sig and not is_large:
        max_abs = min(max_abs * 0.5, 2.0)

    return round(min(max_abs, S.env().paper_bankroll * max_pct, max(MIN_BET, bet)), 2)


# ─────────────────────────────────────────────────────────────────────────────
#  SIGNAL BUILDER
# ─────────────────────────────────────────────────────────────────────────────
def build_signals(trades: list, wallets: dict, whale_exits: dict):
    """
    Build and score trading signals from the trade feed.

    Groups trades by (conditionId, outcome). Each group = one binary market side.
    Requires at least one elite whale per group to build a signal.
    Emits at most ONE signal per conditionId (best scoring outcome wins).

    v8 KEY FIX: avg_entry uses asset-based price lookup (token ID → price)
    when available. Falls back to t["price"] (always correct for the traded outcome).
    cur price uses get_outcome_price with asset hint.
    """
    # Group trades by (conditionId, outcome)
    cid_groups = defaultdict(list)
    for t in trades:
        cid_groups[(t["cid"], t["outcome"])].append(t)

    now_t    = time.time()
    signals  = []
    rejects  = []
    _EMPTY_W = {"score": 0.10, "verified": False, "watchable": False, "elite": False, "hft": False}

    for (cid, outcome), group in cid_groups.items():
        # Deduplicate: keep most recent trade per wallet
        by_w: dict = {}
        for t in group:
            w = t["wallet"]
            if w not in by_w or t["ts"] > by_w[w]["ts"]:
                by_w[w] = t

        title = next(
            (t["title"] for t in by_w.values() if t.get("title") and "?" in str(t.get("title", ""))),
            next(iter(by_w.values()))["title"]
        )

        # ── Classify wallets ──────────────────────────────────────────────────
        elite_wallets    = {w: t for w, t in by_w.items() if wallets.get(w, _EMPTY_W).get("elite")}
        verified_wallets = {w: t for w, t in by_w.items()
                            if wallets.get(w, _EMPTY_W).get("verified") and w not in elite_wallets}
        hft_wallets      = {w: t for w, t in by_w.items()
                            if (wallets.get(w, _EMPTY_W).get("hft") or
                                is_hft_wallet(wallets.get(w, _EMPTY_W)))
                            and w in {**elite_wallets, **verified_wallets}}
        all_ver = {**elite_wallets, **verified_wallets}
        n_ver   = len(all_ver)

        # ── GATE: must have at least one elite whale ──────────────────────────
        if not elite_wallets:
            watchable_count = len([w for w in by_w if wallets.get(w, _EMPTY_W).get("watchable")])
            rejects.append(
                f"  {outcome:<12} {title[:40]}\n"
                f"    ↳ No elite — {n_ver} verified / {watchable_count} watchable / {len(by_w)} total"
            )
            continue

        # ── Market data ───────────────────────────────────────────────────────
        trade_title = title
        # v9 FIX: pass slug + asset hint so get_market can resolve via Gamma slug/token lookup
        asset_hint = next(
            (t.get("asset", "") for t in elite_wallets.values() if t.get("asset")),
            ""
        )
        slug_hint = next(
            (t.get("slug", "") for t in elite_wallets.values() if t.get("slug")),
            ""
        )
        mkt, mkt_fail = get_market(cid, trade_title, asset=asset_hint, slug=slug_hint)
        if not mkt:
            rejects.append(f"  {outcome:<12} {title[:40]}\n    ↳ Market: {mkt_fail}")
            continue
        cur = get_outcome_price(mkt, outcome, asset=asset_hint)

        # SANITY CHECK: if cur suspiciously equals the exact default (~0.545),
        # log it so we can diagnose. Don't block — but if all 4 signals have
        # this price it means get_market() returned the wrong market.
        if abs(cur - 0.545) < 0.002:
            labels = mkt.get("outcome_labels", [])
            S._log(
                f"  ⚠ Price sanity: {title[:30]} [{outcome}] cur={cur:.4f} — "
                f"check if market API returned correct conditionId. labels={labels}",
                "DIAG"
            )
        # ── Event slug extraction (needed for market type classification) ───
        event_slug_sig = next(
            (t.get("event_slug", "") for t in group if t.get("event_slug")), ""
        )
        if not event_slug_sig:
            event_slug_sig = mkt.get("event_slug", "")

        # ── Market type classification ─────────────────────────────────────
        mkt_type = classify_market(title, event_slug_sig, mkt.get("hrs_left"))

        # ── Elite avg entry ───────────────────────────────────────────────
        # t["price"] is ALWAYS the correct price for the outcome that was traded
        elite_entries = []
        for w, t in elite_wallets.items():
            t_asset = t.get("asset", "")
            if t_asset and mkt.get("asset_to_price", {}).get(t_asset):
                entry_price = t["price"]
            else:
                entry_price = t["price"]
            elite_entries.append((entry_price, t["cash"]))

        # ── Large trade detection (v9: PORTFOLIO-RELATIVE) ─────────────────
        # v8 used avg_bet × 10 which labelled everything as CONVICTION.
        # v9 uses portfolio fraction: a trade is conviction when the whale
        # commits >= 0.5% of their portfolio to a single position.
        # Also requires an absolute floor ($1000) to filter tiny portfolios.
        _CONVICTION_PORTFOLIO_PCT = 0.005   # 0.5% of portfolio
        _CONVICTION_ABS_FLOOR    = float(LARGE_TRADE)   # $1000 from config
        has_large_trade = False
        conviction_detail = ""
        for w, t in {**elite_wallets, **verified_wallets}.items():
            prof       = wallets.get(w, {})
            portfolio  = prof.get("total_value", 0) or prof.get("total_pnl", 0)
            cash       = t["cash"]
            avg_b      = prof.get("avg_bet", 0)

            # Portfolio-relative: whale commits significant fraction
            if portfolio > 0 and cash >= portfolio * _CONVICTION_PORTFOLIO_PCT and cash >= _CONVICTION_ABS_FLOOR:
                w_name = S.env().wallet_cache.get(w, {}).get("name", w[:10])
                conviction_detail = f"{w_name} ${cash:,.0f} = {cash/portfolio*100:.1f}% of ${portfolio:,.0f} portfolio"
                has_large_trade = True
                break

            # Absolute massive trade (regardless of portfolio)
            if cash >= MASSIVE_TRADE:
                w_name = S.env().wallet_cache.get(w, {}).get("name", w[:10])
                conviction_detail = f"{w_name} ${cash:,.0f} MASSIVE trade"
                has_large_trade = True
                break

            # Legacy fallback: 20x avg_bet AND absolute floor
            if avg_b > 0 and cash >= avg_b * 20.0 and cash >= _CONVICTION_ABS_FLOOR:
                w_name = S.env().wallet_cache.get(w, {}).get("name", w[:10])
                conviction_detail = f"{w_name} ${cash:,.0f} = {cash/avg_b:.0f}x avg_bet"
                has_large_trade = True
                break

        elite_total_w = sum(cash for _, cash in elite_entries)
        if elite_total_w == 0:
            continue
        elite_avg_entry = sum(p * w for p, w in elite_entries) / elite_total_w

        newest_elite_ts = max(t["ts"] for t in elite_wallets.values())
        age_h_elite     = (now_t - newest_elite_ts) / 3600

        # ── Age gate ──────────────────────────────────────────────────────────
        if age_h_elite > MAX_SIGNAL_AGE_H:
            rejects.append(
                f"  {outcome:<12} {title[:40]}\n"
                f"    ↳ Stale: {age_h_elite:.1f}h > {MAX_SIGNAL_AGE_H}h max"
            )
            continue

        # ── Slippage gate ─────────────────────────────────────────────────────
        slippage = (cur - elite_avg_entry) / max(elite_avg_entry, 0.01)
        if slippage > MAX_ENTRY_SLIPPAGE:
            rejects.append(
                f"  {outcome:<12} {title[:40]}\n"
                f"    ↳ Slippage +{slippage*100:.1f}% > max {MAX_ENTRY_SLIPPAGE*100:.0f}% "
                f"(entry ${elite_avg_entry:.4f} → now ${cur:.4f})"
            )
            continue

        # ── Drift gate ────────────────────────────────────────────────────────
        drift = (cur - elite_avg_entry) / max(elite_avg_entry, 0.01)
        if drift < MIN_DRIFT:
            rejects.append(
                f"  {outcome:<12} {title[:40]}\n"
                f"    ↳ Drift {drift*100:+.1f}% < min {MIN_DRIFT*100:.0f}%"
            )
            continue

        if age_h_elite > STALE_LOSER_AGE_H and drift < STALE_LOSER_DRIFT:
            rejects.append(
                f"  {outcome:<12} {title[:40]}\n"
                f"    ↳ Stale loser: {age_h_elite:.1f}h old, {drift*100:.1f}%"
            )
            continue

        # ── Fee gate ──────────────────────────────────────────────────────────
        net_return = (1.0 / max(cur, 0.01) - 1.0) - ROUND_TRIP_FEE
        if net_return <= 0 or cur > 0.965:
            rejects.append(
                f"  {outcome:<12} {title[:40]}\n"
                f"    ↳ Fee gate: cur={cur:.3f} net={net_return*100:.1f}%"
            )
            continue

        # ── HFT gate: ONLY signal when HFT wallets break their own pattern ───
        # An HFT wallet betting $13 on a tennis match is noise — they do that
        # hundreds of times/hour. Only signal when they bet >> their own average.
        # This is the whole point: their $1,785 bet on Rosario No is the alpha.
        is_hft_signal = len(hft_wallets) > 0 and (now_t - newest_elite_ts) <= HFT_MIRROR_DELAY_MAX_SECONDS
        if is_hft_signal and not has_large_trade:
            # Check if ALL the elites are HFT — if so, require large trade
            all_elites_are_hft = all(
                wallets.get(w, {}).get("hft") or
                wallets.get(w, {}).get("trades_per_hour", 0) >= HFT_MIN_TRADES_PER_HOUR
                for w in elite_wallets
            )
            if all_elites_are_hft:
                # Pure HFT signal with no large trade = skip entirely
                # (was previously traded with $2 cap — not worth the noise)
                rejects.append(
                    f"  {outcome:<12} {title[:40]}\n"
                    f"    ↳ HFT-only noise: no large trade (max cash ${max(t['cash'] for t in all_ver.values()):.0f} "
                    f"vs avg_bet ${min(wallets.get(w,{}).get('avg_bet',0) for w in hft_wallets):.0f})"
                )
                continue

        # ── Sports market multi-whale gate ───────────────────────────────
        # Sports markets require >= 2 independent elite whales.
        # Single-whale sports bets are too close to coin flips.
        if mkt_type == "SPORTS" and len(elite_wallets) < 2:
            rejects.append(
                f"  {outcome:<12} {title[:40]}\n"
                f"    ↳ SPORTS single-whale: need 2+ elites, have {len(elite_wallets)}"
            )
            continue

        # ── Build signal ──────────────────────────────────────────────────
        window = "hot" if any(t.get("window") == "hot" for t in all_ver.values()) else "warm"
        if is_hft_signal:
            window = "hft"

        total_flow   = sum(t["cash"] for t in by_w.values())
        ver_flow     = sum(t["cash"] for t in all_ver.values())
        max_bet_cash = max(t["cash"] for t in all_ver.values())
        newest_ts    = max(t["ts"] for t in all_ver.values())
        age_h        = (now_t - newest_ts) / 3600

        avg_wscore = sum(
            wallets.get(w, _EMPTY_W).get("score", 0.10) for w in elite_wallets
        ) / len(elite_wallets)

        exits_on_this   = whale_exits.get(cid, [])
        our_wallets     = set(all_ver.keys())
        exits_same_side = list(set(exits_on_this) & our_wallets)

        event_slug_sig = next(
            (t.get("event_slug", "") for t in group if t.get("event_slug")), ""
        )
        if not event_slug_sig:
            event_slug_sig = mkt.get("event_slug", "")

        elite_only_mode = len(verified_wallets) == 0
        n_confluence    = len(verified_wallets)

        sig = {
            "slug":           cid,
            "cid":            cid,
            "title":          title,
            "event_slug":     event_slug_sig,
            "outcome":        outcome,
            "asset":          asset_hint,   # v8: store asset for price refresh
            "mkt":            mkt,
            "mkt_type":       mkt_type,     # v9: SPORTS/CRYPTO/POLITICS/EVENT
            "ver":            all_ver,
            "elite_ver":      elite_wallets,
            "n_ver":          n_ver,
            "n_elite":        len(elite_wallets),
            "n_confluence":   n_confluence,
            "n_total":        len(by_w),
            "avg_entry":      elite_avg_entry,
            "cur":            cur,
            "drift":          drift,
            "slippage":       slippage,
            "total_flow":     total_flow,
            "ver_flow":       ver_flow,
            "max_bet_cash":   max_bet_cash,
            "max_bet":        max_bet_cash,
            "newest_ts":      newest_ts,
            "age_h":          age_h,
            "age_min":        age_h * 60,
            "avg_wscore":     avg_wscore,
            "window":         window,
            "is_hft":         is_hft_signal,
            "has_large_trade": has_large_trade,
            "conviction_detail": conviction_detail if has_large_trade else "",
            "elite_only_mode": elite_only_mode,
            "wallets":        wallets,
            "exits_detected": exits_on_this,
            "exits_same_side": exits_same_side,
        }

        bd    = score_signal(sig)
        total = bd["total"]

        if total < MIN_SCORE:
            rejects.append(
                f"  {outcome:<12} {title[:40]}\n"
                f"    ↳ Score {total:.0f} < {MIN_SCORE} "
                f"[W:{bd.get('wallet',0):.0f} C:{bd.get('conf',0)} "
                f"R:{bd.get('rec',0)} O:{bd.get('opp',0)} "
                f"M:{bd.get('mkt',0):.0f} B:{bd.get('bonus',0)}]"
            )
            continue

        # ── Tier ─────────────────────────────────────────────────────────────
        if is_hft_signal and not has_large_trade:
            tier = "HFT"
        elif has_large_trade and total >= ALERT_SCORE:
            tier = "CONVICTION"   # v8: new tier for large trades
        elif total >= ALERT_SCORE:
            tier = "ALERT"
        elif total >= STRONG_SCORE:
            tier = "STRONG"
        elif elite_only_mode:
            tier = "ELITE_ONLY"
        else:
            tier = "MEDIUM"

        if exits_on_this and tier in ("ALERT", "CONVICTION"):
            tier = "STRONG"
        if age_h > MAX_SIGNAL_AGE_H:
            tier = "STALE"

        bet = kelly_bet(sig, wallets, score=total)

        # Build whale names for display
        elite_names = []
        for w in list(elite_wallets.keys())[:3]:
            name = (S.env().wallet_cache.get(w, {}).get("name") or
                    all_ver[w].get("name") or
                    wallets.get(w, _EMPTY_W).get("name") or
                    w[:10] + "…")
            elite_names.append(name)

        conf_names = []
        for w in list(verified_wallets.keys())[:2]:
            name = (S.env().wallet_cache.get(w, {}).get("name") or
                    all_ver[w].get("name") or
                    wallets.get(w, _EMPTY_W).get("name") or
                    w[:10] + "…")
            conf_names.append(name)

        names = elite_names + ([f"+{len(conf_names)}conf"] if conf_names else [])
        if is_hft_signal:
            names = ["⚡" + n for n in names]
        if has_large_trade:
            names = ["💎" + n if not n.startswith("💎") else n for n in names[:1]] + names[1:]

        sig.update({"score": total, "bd": bd, "tier": tier, "bet": bet, "names": names})
        signals.append(sig)

    # ── Per-conditionId dedup: ONE signal per market, best outcome wins ────────
    # EXCEPTION: if a SINGLE whale has bought BOTH outcomes of the same market,
    # this is likely an arbitrage/hedge bot (like swisstony). Flag both as HEDGE
    # and only trade the higher-scoring side.
    tp = {"CONVICTION": 6, "HFT": 5, "ALERT": 4, "STRONG": 3, "ELITE_ONLY": 2, "MEDIUM": 1, "STALE": 0}
    signals.sort(key=lambda x: (tp.get(x["tier"], 0), x["score"]), reverse=True)

    # Find CIDs where the SAME elite wallet appears on both outcomes
    # v9: PERMANENTLY flag hedge wallets and skip ALL their signals
    cid_wallets: dict = {}
    for s in signals:
        cid = s["cid"]
        if cid not in cid_wallets:
            cid_wallets[cid] = {}
        for w in s.get("elite_ver", {}):
            if w not in cid_wallets[cid]:
                cid_wallets[cid][w] = []
            cid_wallets[cid][w].append(s["outcome"])

    hedge_cids: set = set()
    for cid, wmap in cid_wallets.items():
        for w, outcomes in wmap.items():
            if len(outcomes) >= 2:
                wname = S.env().wallet_cache.get(w, {}).get("name", w[:10] + "…")
                # v9: Permanently flag this wallet as a hedge bot
                if w not in _KNOWN_HEDGE_WALLETS:
                    _KNOWN_HEDGE_WALLETS.add(w)
                    S._log(
                        f"  🚫 HEDGE BOT FLAGGED: {wname} bought both {outcomes} — "
                        f"all future signals from this wallet will be ignored",
                        "WARN"
                    )
                else:
                    S._log(
                        f"  ♻ Known hedge bot {wname} on both {outcomes} — skipping",
                        "DIAG"
                    )
                hedge_cids.add(cid)
                break

    seen_cids: set = set()
    final_signals  = []
    for s in signals:
        # v9: Skip ALL signals from hedge CIDs (both sides), not just the lower-scoring one
        if s["cid"] in hedge_cids:
            rejects.append(
                f"  {s['outcome']:<12} {s['title'][:40]}\n"
                f"    ↳ HEDGE bot market — skipping BOTH sides"
            )
            continue
        # v9: Skip signals where the sole elite is a known hedge wallet
        sig_elites = set(s.get("elite_ver", {}).keys())
        if sig_elites and sig_elites.issubset(_KNOWN_HEDGE_WALLETS):
            rejects.append(
                f"  {s['outcome']:<12} {s['title'][:40]}\n"
                f"    ↳ All elites are known hedge bots — skipping"
            )
            continue
        if s["cid"] in seen_cids:
            rejects.append(
                f"  {s['outcome']:<12} {s['title'][:40]}\n"
                f"    ↳ Deduped: another outcome on this conditionId scored higher"
            )
            continue
        seen_cids.add(s["cid"])
        final_signals.append(s)
    signals = final_signals

    S.env().active_signal_cids.clear()
    for s in signals:
        S.env().active_signal_cids[s["cid"]] = set(s.get("ver", {}).keys())

    S.env().LAST_SIGNALS = signals
    S.env().LAST_REJECTS = rejects
    return signals, rejects