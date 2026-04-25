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
import titan_config as C
from titan_config import *
from titan_market import get_market, get_outcome_price, get_outcome_price_by_trade, fetch_wallet_sells
from titan_wallet import is_hft_wallet, get_whale_weekly_pnl


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

    # Impact cost: how much our bet moves the price.
    # FIX: was * 2.0 which wildly overstated impact for small bets (e.g. $2 in $50k pool).
    # Paper trading bets are tiny — real market impact is negligible.
    # Use 0.3x multiplier for bets under $10, linear above.
    if liq > 0:
        scale = 0.3 if bet_size < 10 else (0.5 if bet_size < 50 else 1.0)
        impact_pct = min(0.03, (bet_size / liq) * scale)
    else:
        impact_pct = 0.02

    # Total friction = spread + impact + round-trip fee
    total_friction = spread_pct + impact_pct + ROUND_TRIP_FEE

    # FIX (Bug 1): Whale's implied probability = their entry price.
    # They paid avg_entry cents for a $1 payout — that IS their probability estimate.
    # Binary EV: we buy at cur_price. If outcome hits, we gain (1 - cur_price).
    # If it misses, we lose cur_price. Edge is valid when fair_prob > cur_price.
    fair_prob = max(0.05, min(0.95, avg_entry))
    ev_per_dollar = fair_prob * (1.0 - cur_price) - (1.0 - fair_prob) * cur_price
    ev_after_friction = ev_per_dollar - total_friction
    ev_dollar = ev_after_friction * bet_size

    return {
        "ev_dollar": round(ev_dollar, 4),
        "ev_pct": round(ev_after_friction * 100, 2),
        "spread_cost": round(spread_pct * 100, 2),
        "impact_cost": round(impact_pct * 100, 2),
        "total_friction": round(total_friction * 100, 2),
        "fair_prob": round(fair_prob * 100, 1),
        "tradeable": ev_after_friction > 0.0,  # require positive EV (signal-level gate already checks 1-2%)
    }


# ─────────────────────────────────────────────────────────────────────────────
#  HEDGE BOT TRACKING (persistent across cycles)
# ─────────────────────────────────────────────────────────────────────────────
_KNOWN_HEDGE_WALLETS: set = set()  # wallets caught buying both sides


def get_known_hedge_wallets() -> set:
    """Return the hedge wallet set for persistence."""
    return _KNOWN_HEDGE_WALLETS


def restore_known_hedge_wallets(wallets_iter):
    """Restore hedge wallets from saved state."""
    _KNOWN_HEDGE_WALLETS.update(str(w).lower() for w in wallets_iter)


# ─────────────────────────────────────────────────────────────────────────────
#  EXIT MONITORING
# ─────────────────────────────────────────────────────────────────────────────
def check_whale_exits(cid_to_wallet_sets: dict, entry_times: dict = None) -> dict:
    """
    Returns {conditionId: [wallet_addresses_that_sold]}.
    Only checks SELL activity for wallets on open positions.

    IMPROVED exit detection:
    - Matches sells by BOTH conditionId AND asset/token ID (the token ID is the
      most reliable identifier — whales may have multiple positions in the same market).
    - Verifies the sell happened AFTER our entry (not a pre-existing sell).
    - Checks sell size vs open position size to detect partial vs full exits.
      We only trigger on sells that are >= 30% of the whale's original buy cash
      (they could be trimming, not exiting fully).
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

    # Build a map from (cid -> wallet -> buy_cash) so we can verify sell size
    # Use per-whale buy cash stored on the position when available (most accurate)
    cid_wallet_buy_cash: dict = {}
    for key, pos in S.env().open_positions.items():
        cid = pos.get("cid", key[0])
        whale_buy_cash = pos.get("whale_buy_cash", {})
        if whale_buy_cash:
            # Use the precise per-whale buy cash stored at entry time
            cid_wallet_buy_cash[cid] = whale_buy_cash
        else:
            # Fallback: distribute our total bet across all elite wallets equally
            for w in pos.get("elite_wallets", []):
                w_lower = w.lower()
                if cid not in cid_wallet_buy_cash:
                    cid_wallet_buy_cash[cid] = {}
                cid_wallet_buy_cash[cid][w_lower] = pos.get("bet", 0)

    # Build asset→cid reverse map so we can match sells by token ID
    asset_to_cid: dict = {}
    for key, pos in S.env().open_positions.items():
        cid = pos.get("cid", key[0])
        asset = pos.get("asset", "")
        if asset:
            asset_to_cid[asset] = cid

    for wallet in all_wallets:
        sells = fetch_wallet_sells(wallet, global_cutoff, limit=200)
        if not sells:
            time.sleep(0.1)
            continue

        for sell in sells:
            ts    = sell["ts"]
            cid   = sell["cid"]
            asset = sell.get("asset", "")
            sell_cash = sell.get("cash", 0)

            # Resolve CID: try direct match, then asset token match, then asset→cid map
            if cid not in cid_to_wallet_sets:
                if asset and asset in cid_to_wallet_sets:
                    cid = asset
                elif asset and asset in asset_to_cid:
                    cid = asset_to_cid[asset]
                else:
                    continue

            if wallet not in cid_to_wallet_sets.get(cid, set()):
                continue

            our_entry_ts = entry_times.get(cid, 0)
            if ts <= our_entry_ts:
                # Sell happened before or at our entry — not an exit of our position
                continue

            # Partial sell check: only trigger if they sold at least 30% of what
            # we tracked as their buy-side cash. This prevents triggering on tiny
            # trims while the whale is still substantially in the position.
            buy_cash = cid_wallet_buy_cash.get(cid, {}).get(wallet.lower(), 0)
            if buy_cash > 0 and sell_cash > 0:
                sell_fraction = sell_cash / buy_cash
                if sell_fraction < 0.30:
                    S._log(
                        f"  🐋 Partial trim ignored: {S.env().wallet_cache.get(wallet,{}).get('name',wallet[:10])} "
                        f"sold {sell_fraction*100:.0f}% of position (need ≥30%)",
                        "DIAG"
                    )
                    continue

            if wallet not in exits[cid]:
                exits[cid].append(wallet)
                w_name = S.env().wallet_cache.get(wallet, {}).get("name", wallet[:10])
                prof   = S.env().wallet_cache.get(wallet, {})
                tag    = "🔥" if prof.get("elite") else "✅" if prof.get("verified") else "👁"
                size_str = f" ${sell_cash:.0f}" if sell_cash > 0 else ""
                S.env().WHALE_EXIT_HISTORY.append(
                    f"[{time.strftime('%H:%M')}] {tag} {w_name} SOLD{size_str}"
                )
                if len(S.env().WHALE_EXIT_HISTORY) > 30:
                    del S.env().WHALE_EXIT_HISTORY[:-30]
                S._log(f"🐋 EXIT DETECTED: {w_name} sold cid={cid[:20]}… ${sell_cash:.0f}", "INFO")

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

    # Weekly performance penalty: if our copy-trades from these elite whales
    # have been net-negative in the last 7 days, apply a score penalty.
    # This prevents us from blindly following whales who are currently cold.
    weekly_pnl_total = sum(
        get_whale_weekly_pnl(w)
        for w in s.get("elite_ver", {}).keys()
    )
    if weekly_pnl_total < -1.0:
        # Every $1 of weekly loss = -1 score point, capped at -10
        bd["weekly_penalty"] = max(-10, round(weekly_pnl_total, 0))
    else:
        bd["weekly_penalty"] = 0

    bd["total"] = round(min(100, max(0, sum(v for k, v in bd.items() if k != "total"))), 1)
    return bd


# ─────────────────────────────────────────────────────────────────────────────
#  BET SIZING
# ─────────────────────────────────────────────────────────────────────────────
def _adaptive_bet_caps():
    br = S.env().paper_bankroll
    if br < 15:    return 2.00,  0.15   # very low bankroll — be careful
    elif br < 30:  return 5.00,  0.20   # small bankroll — allow up to 20%
    elif br < 75:  return 12.00, 0.20
    elif br < 200: return 30.00, 0.20
    elif br < 500: return 60.00, 0.20
    else:          return 150.00, 0.20


def kelly_bet(signal: dict, wallets: dict, score: float = None) -> float:
    cur        = signal["cur"]
    fair_value = signal["avg_entry"]
    score      = score if score is not None else signal.get("score", 50)
    tier       = signal.get("tier", "MEDIUM")
    is_large   = signal.get("has_large_trade", False)
    is_hft_sig = signal.get("is_hft", False)

    fair_value = max(0.03, min(0.97, fair_value))
    cur        = max(0.03, min(0.97, cur))

    # KEY FIX: The whale's avg_entry is NOT the fair probability ceiling.
    # The whale bought at their entry price because they think the TRUE
    # probability is higher. We assume a 5-15% edge above their entry
    # based on their track record (elite score). This prevents the system
    # from always returning MIN_BET when cur slightly exceeds whale entry.
    avg_wscore = signal.get("avg_wscore", 0.85)
    # Implied true prob = whale entry + edge adjustment based on their score
    # High-score whales (0.95) → +10% edge above entry; lower (0.70) → +5%
    edge_boost = 0.05 + (avg_wscore - 0.70) * 0.25  # 5-12.5% range
    implied_true_prob = min(0.97, fair_value + edge_boost)

    # Use implied_true_prob as our fair value for Kelly calculation
    kelly_fair = implied_true_prob

    # Standard Kelly formula: f = (b*p - q) / b where b = odds-1
    b     = max(0.05, (1.0 / cur) - 1.0 - ROUND_TRIP_FEE)
    kelly = max(0.0, kelly_fair - (1 - kelly_fair) / b)
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

    # Separate sizing for HFT vs conviction vs large trades
    if is_large and not is_hft_sig:
        tier_mult = {"CONVICTION": 2.0, "ALERT": 1.5, "STRONG": 1.3, "MEDIUM": 1.0, "ELITE_ONLY": 1.2, "HFT": 1.3}.get(tier, 1.0)
    elif is_hft_sig and not is_large:
        tier_mult = {"ALERT": 0.6, "STRONG": 0.5, "MEDIUM": 0.4, "ELITE_ONLY": 0.5, "HFT": 0.7}.get(tier, 0.5)
    else:
        tier_mult = {"CONVICTION": 1.6, "ALERT": 1.2, "STRONG": 1.0, "MEDIUM": 0.7, "ELITE_ONLY": 0.9, "HFT": 1.1}.get(tier, 0.8)

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

    # Floor: score-based minimum bet so high-confidence signals always size up
    # Score 65 → $1.50, Score 80 → $2.50, Score 95+ → $4.00
    score_floor = MIN_BET + max(0, (score - 52) / 45) * (MAX_BET_ABS - MIN_BET) * 0.6
    bet = max(bet, score_floor)

    max_abs, max_pct = _adaptive_bet_caps()

    # For large conviction trades, allow higher absolute max
    if is_large and not is_hft_sig:
        max_abs = min(max_abs * 2.5, S.env().paper_bankroll * 0.25)

    # For pure HFT (non-large), cap lower
    if is_hft_sig and not is_large:
        max_abs = min(max_abs * 0.5, 2.0)

    # SPORTS PENALTY: sports are random events — no betting edge, use tiny size
    mkt_type = signal.get("mkt_type", "POLITICS")
    if mkt_type == "SPORTS":
        bet = bet * 0.4   # 40% of calculated size for sports
        max_abs = min(max_abs, S.env().paper_bankroll * 0.05)  # Hard 5% cap for sports

    # Score-tiered hard floor/ceiling: ensure small bets for low-confidence signals
    # Score < 60: cap at 4% bankroll regardless of kelly output
    # Score < 55: cap at 3% bankroll
    if score < 55:
        max_abs = min(max_abs, S.env().paper_bankroll * 0.03)
    elif score < 60:
        max_abs = min(max_abs, S.env().paper_bankroll * 0.04)
    elif score < 65:
        max_abs = min(max_abs, S.env().paper_bankroll * 0.06)
    elif score < 70:
        max_abs = min(max_abs, S.env().paper_bankroll * 0.08)
    # score >= 70: use full max_abs from caps

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

        # Use the most recent trade's title — all Polymarket titles contain "?"
        # so the previous "?" filter was meaningless (always matched first item)
        title = next(
            (t["title"] for t in sorted(by_w.values(), key=lambda x: x["ts"], reverse=True)
             if t.get("title")),
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

        # Compute a preliminary is_hft_signal here so the drift/slippage gates
        # below can apply HFT-specific thresholds. The definitive version is
        # recomputed after newest_elite_ts is known (line ~661).
        _any_hft_trade_tagged = any(
            t.get("is_large_trade") or t.get("hft_spike_ratio", 0) > 0
            for t in by_w.values()
        )
        is_hft_signal = len(hft_wallets) > 0 or _any_hft_trade_tagged

        # ── COUNTER-WHALE ANALYSIS ───────────────────────────────────────────
        # Check if verified whales are buying the OPPOSITE side of this market.
        # Find trades for (cid, opposite_outcome) in cid_groups.
        opposite_elite_cash = 0.0
        our_elite_cash = sum(t["cash"] for t in elite_wallets.values()) if elite_wallets else 0.0
        for (other_cid, other_outcome), other_group in cid_groups.items():
            if other_cid == cid and other_outcome != outcome:
                for t in other_group:
                    w = t["wallet"]
                    if wallets.get(w, _EMPTY_W).get("elite") or wallets.get(w, _EMPTY_W).get("verified"):
                        opposite_elite_cash += t["cash"]
                break

        # If opposing elite flow > 60% of our elite flow, the signal is contested — skip
        if opposite_elite_cash > 0 and our_elite_cash > 0:
            opposition_ratio = opposite_elite_cash / (our_elite_cash + opposite_elite_cash)
            if opposition_ratio > 0.60:
                rejects.append(
                    f"  {outcome:<12} {title[:40]}\n"
                    f"    ↳ Counter-whale: ${opposite_elite_cash:.0f} opposing vs ${our_elite_cash:.0f} ours"
                    f" ({opposition_ratio*100:.0f}% against)"
                )
                continue

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
            # For HFT spike signals: don't kill the signal just because Gamma API is down.
            # We already know the price from the trade record itself.
            # Build a minimal synthetic market object from trade data so the signal survives.
            _is_hft_pre = any(
                t.get("hft_spike_ratio", 0) > 0 or t.get("is_large_trade")
                for t in by_w.values()
            ) and len(hft_wallets) > 0
            if _is_hft_pre:
                # Use trade price as both entry and current price (no drift = fresh)
                _trade_price = next(iter(elite_wallets.values())).get("price", 0.5)
                mkt = {
                    "yes_price": _trade_price, "no_price": 1.0 - _trade_price,
                    "outcome_prices": {"Yes": _trade_price, "No": 1.0 - _trade_price},
                    "asset_to_price": {asset_hint: _trade_price} if asset_hint else {},
                    "liq": 10_000,   # assume liquid — we'll find out on exit
                    "volume": 50_000,
                    "title": trade_title,
                    "hrs_left": 168,  # assume > 7 days — safe for HFT spikes
                    "slug": slug_hint or "",
                    "event_slug": "",
                    "ts": now_t,
                    "outcome_labels": [],
                    "token_index": {},
                    "index_to_price": {0: _trade_price, 1: 1.0 - _trade_price},
                }
                S._log(
                    f"  ⚡ HFT API fallback: {title[:35]} — using trade price ${_trade_price:.4f}",
                    "DIAG"
                )
            else:
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

        # ── SPORTS-SPECIFIC GATES ────────────────────────────────────────────
        # Sports markets (baseball, tennis, soccer, basketball) are inherently random.
        # Even elite whales fail on sports regularly. Apply stricter rules:
        # - Require at least 2 NON-SPORTS-BOT elite whales for sports
        #   Sports bots (RN1, GamblingIsAllYouNeed, swisstony, elkmonkey) are
        #   market-makers. They trade BOTH sides rapidly for spread, not prediction.
        #   Counting them as "2 elites" for sports was the #1 source of losses.
        # - Cap individual position size at 5% for sports (applied in trader)
        if mkt_type == "SPORTS":
            # Filter sports_bot wallets out of the elite count entirely
            genuine_sports_elites = {
                w: t for w, t in elite_wallets.items()
                if not S.env().wallet_cache.get(w, {}).get("sports_bot", False)
            }
            n_genuine = len(genuine_sports_elites)
            # RELAXED: 1+ genuine elite allowed here (tighter 2-elite check applied
            # later at the second sports gate after EV/drift pass).
            if n_genuine < 1:
                sports_bot_names = [
                    S.env().wallet_cache.get(w, {}).get("name", w[:10])
                    for w in elite_wallets if w not in genuine_sports_elites
                ]
                rejects.append(
                    f"  {outcome:<12} {title[:40]}\n"
                    f"    ↳ Sports needs 1+ genuine elite (have {n_genuine} after "
                    f"filtering {len(elite_wallets)-n_genuine} sports bots: {sports_bot_names})"
                )
                continue
            # Sports also need tighter drift — sports markets move fast
            # We'll reuse existing drift gate but flag for tighter sizing

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

            # Legacy fallback: 20x avg_bet AND absolute floor (non-HFT)
            if avg_b > 0 and cash >= avg_b * 20.0 and cash >= _CONVICTION_ABS_FLOOR:
                w_name = S.env().wallet_cache.get(w, {}).get("name", w[:10])
                conviction_detail = f"{w_name} ${cash:,.0f} = {cash/avg_b:.0f}x avg_bet"
                has_large_trade = True
                break

            # HFT-specific path: no $1000 floor. A bot with avg_bet $5 putting
            # $400 (80x their average) IS conviction — that IS the alpha signal.
            # FIX: lowered from 50x to 30x. fetch_hft_spike_trades already
            # pre-filters at 20x (low-freq) or 40x (high-freq >200 TPH).
            # By the time a trade reaches build_signals it has ALREADY cleared
            # that bar — the 50x gate here was double-filtering and blocking
            # legitimate spikes (e.g. 59x avg was passing 40x in market.py
            # but then failing 50x here). 30x is the correct signal threshold.
            is_w_hft = (
                prof.get("hft") or
                prof.get("trades_per_hour", 0) >= HFT_MIN_TRADES_PER_HOUR or
                (avg_b > 0 and avg_b < 50 and prof.get("n_resolved", 0) > 100)
            )
            # Also treat trades pre-tagged by fetch_hft_spike_trades as large
            if t.get("is_large_trade") or t.get("hft_spike_ratio", 0) >= 20:
                w_name = S.env().wallet_cache.get(w, {}).get("name", w[:10])
                spike_ratio = t.get("hft_spike_ratio", cash / max(avg_b, 0.01))
                conviction_detail = f"{w_name} ${cash:,.0f} = {spike_ratio:.0f}x HFT spike (pre-tagged)"
                has_large_trade = True
                break
            if is_w_hft and avg_b > 0 and cash >= avg_b * 30.0:
                w_name = S.env().wallet_cache.get(w, {}).get("name", w[:10])
                conviction_detail = f"{w_name} ${cash:,.0f} = {cash/avg_b:.0f}x HFT avg_bet (no floor)"
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
        # HFT spikes require MUCH tighter slippage — the whole point is catching
        # the momentum early. If we're already 3%+ above their entry, it's too late.
        _max_slip = HFT_MAX_ENTRY_SLIPPAGE if is_hft_signal else MAX_ENTRY_SLIPPAGE
        if slippage > _max_slip:
            rejects.append(
                f"  {outcome:<12} {title[:40]}\n"
                f"    ↳ Slippage +{slippage*100:.1f}% > max {_max_slip*100:.0f}% "
                f"(entry ${elite_avg_entry:.4f} → now ${cur:.4f})"
            )
            continue

        # ── Drift gate ────────────────────────────────────────────────────────
        drift = (cur - elite_avg_entry) / max(elite_avg_entry, 0.01)
        # HFT spikes: tighter drift — we must be very close to their entry price
        _max_drift = HFT_MAX_DRIFT if is_hft_signal else MAX_DRIFT
        _min_drift = HFT_MIN_DRIFT if is_hft_signal else MIN_DRIFT
        if drift > _max_drift:
            rejects.append(
                f"  {outcome:<12} {title[:40]}\n"
                f"    ↳ Drift +{drift*100:.1f}% > max {_max_drift*100:.0f}% "
                f"({'HFT' if is_hft_signal else 'std'} gate)"
            )
            continue
        if drift < _min_drift:
            rejects.append(
                f"  {outcome:<12} {title[:40]}\n"
                f"    ↳ Drift {drift*100:+.1f}% < min {_min_drift*100:.0f}%"
            )
            continue

        # ── Post-drift EV sanity check ───────────────────────────────────────
        # Even if drift is within gate, check if entering NOW still makes money.
        # The whale's avg_entry IS their implied fair probability.
        # If cur >= whale_entry, we need the market to move further in our favour
        # to profit — check that the remaining upside justifies the entry.
        # For a binary at cur price: win $((1/cur) - 1), lose $1.
        # EV > 0 requires: fair_prob > cur  (whale's entry = their fair prob estimate)
        # RELAXED: lowered from 5%/3% to 2%/1% — Polymarket's thin-margin environment
        # rarely produces >3% EV from whale signals. The drift gate already ensures
        # we're close to the whale's entry, so a positive EV direction is sufficient.
        _fair_prob = max(0.02, min(0.97, elite_avg_entry))
        _potential_win  = (1.0 / max(cur, 0.01)) - 1.0   # $gain per $1 bet if wins
        _raw_ev_per_1   = _fair_prob * _potential_win - (1.0 - _fair_prob)
        # For HFT spikes: require slightly higher EV since momentum fades fast
        _ev_threshold = 0.02 if is_hft_signal else 0.01
        if _raw_ev_per_1 < _ev_threshold:
            rejects.append(
                f"  {outcome:<12} {title[:40]}\n"
                f"    ↳ Drift EV too low: whale_entry={elite_avg_entry:.3f} "
                f"cur={cur:.3f} → EV={_raw_ev_per_1*100:+.1f}% < {_ev_threshold*100:.0f}% min"
            )
            continue

        # ── Stale loser gate (previously dead code — now active) ──────────────
        if age_h_elite > STALE_LOSER_AGE_H and drift < STALE_LOSER_DRIFT:
            rejects.append(
                f"  {outcome:<12} {title[:40]}\n"
                f"    ↳ Stale loser: {age_h_elite:.1f}h old, drift {drift*100:.1f}%"
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

        # ── Sports market gate (second check — post drift/EV gates) ──────────
        # Sports are no longer hard-blocked. But we require 2+ GENUINE (non-sports-bot)
        # elite whales, OR a genuinely extreme HFT spike (500x+).
        if mkt_type == "SPORTS":
            _max_spike = max(
                (t.get("hft_spike_ratio", 0) for t in group),
                default=0
            )
            _is_extreme_spike = (
                is_hft_signal and
                has_large_trade and
                _max_spike >= 500 and
                len(elite_wallets) >= 1
            )
            # Re-compute genuine elites (sports_bot filtered) for this final check
            _genuine_sports_elites = {
                w: t for w, t in elite_wallets.items()
                if not S.env().wallet_cache.get(w, {}).get("sports_bot", False)
            }
            if not _is_extreme_spike:
                if len(_genuine_sports_elites) < 2:
                    rejects.append(
                        f"  {outcome:<12} {title[:40]}\n"
                        f"    ↳ SPORTS need 2+ genuine elites (have {len(_genuine_sports_elites)}) "
                        f"or extreme spike ({_max_spike:.0f}x < 500x)"
                    )
                    continue

        # ── Short-duration EVENT/CRYPTO gate ─────────────────────────────
        # Markets closing in < 1h are near-expiry binary traps.
        # The whale may be closing out existing positions, not opening new ones.
        # Exception: HFT spike signals with very large trades (they know the outcome)
        hrs_left_gate = mkt.get("hrs_left")
        if hrs_left_gate is not None and hrs_left_gate < 1.0:
            if not (is_hft_signal and has_large_trade):
                rejects.append(
                    f"  {outcome:<12} {title[:40]}\n"
                    f"    ↳ Near-expiry trap: only {hrs_left_gate:.1f}h left"
                )
                continue

        # ── Crypto short-duration coin-flip gate ──────────────────────────
        # Same-day crypto price-target markets are coin flips:
        #   "Bitcoin reach $79k on April 24?" — hours left, unhedgeable
        #   "Bitcoin above $78k on April 24?" — same
        #   "Bitcoin between $76-78k on April 24?" — same
        # The whale may be HEDGING their real portfolio (buying multiple buckets),
        # NOT making a directional prediction. We have zero edge on these.
        # Gate: any CRYPTO market with < 24h left that matches a price-target pattern.
        _CRYPTO_COINFLIP_RE = re.compile(
            r'\b(up or down|reach \$|above \$|below \$|between \$|'
            r'at \$\d|hit \$|dip to|crash to|'
            r'bitcoin up|ethereum up|xrp up|btc up|eth up)\b',
            re.IGNORECASE
        )
        _is_crypto_coinflip = (
            mkt_type == "CRYPTO" and
            hrs_left_gate is not None and hrs_left_gate < 24.0 and
            _CRYPTO_COINFLIP_RE.search(title)
        )
        if _is_crypto_coinflip:
            # Require 2+ genuine elites (not sports bots) AND a large trade
            _genuine_crypto_elites = {
                w: t for w, t in elite_wallets.items()
                if not S.env().wallet_cache.get(w, {}).get("sports_bot", False)
            }
            if len(_genuine_crypto_elites) < 2 or not has_large_trade:
                rejects.append(
                    f"  {outcome:<12} {title[:40]}\n"
                    f"    ↳ Crypto coin-flip gate ({hrs_left_gate:.1f}h left): "
                    f"need 2+ genuine elites + large trade, "
                    f"have {len(_genuine_crypto_elites)} genuine elite, large={has_large_trade}"
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
        # FIX: n_confluence counts ALL verified wallets (elite + non-elite).
        # Previously only counted non-elite verified, so signals with 3 elite whales
        # got n_confluence=0, killing both the scoring and the MIN_ELITE_CONFLUENCE gate.
        n_confluence    = len(all_ver)

        sig = {
            "slug":           cid,
            "cid":            cid,
            "title":          title,
            "event_slug":     event_slug_sig,
            "outcome":        outcome,
            "asset":          asset_hint,   # v8: store asset for price refresh
            "mkt":            mkt,
            "mkt_type":       mkt_type,     # v9: SPORTS/CRYPTO/POLITICS/EVENT
            "is_sports":      (mkt_type == "SPORTS"),
            "opposing_flow":  opposite_elite_cash,
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
        if is_hft_signal and has_large_trade:
            # HFT spike with large trade = CONVICTION regardless of score.
            # Score threshold is irrelevant for momentum signals — the 40-200x
            # spike IS the signal. Forcing through CONVICTION tier so W8's
            # TRADEABLE_TIERS_LIST = ["CONVICTION", "HFT"] always catches it.
            tier = "CONVICTION"
        elif is_hft_signal and not has_large_trade:
            tier = "HFT"
        elif has_large_trade and total >= ALERT_SCORE:
            tier = "CONVICTION"
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
        # FIX: HFT spike signals already gate age via HFT_MIRROR_DELAY_MAX_SECONDS
        # (checked above in is_hft_signal). Don't also apply the MAX_SIGNAL_AGE_H
        # STALE override to CONVICTION-tier spikes — we'd double-gate and then kill
        # signals that passed the tighter 45s window check.
        if age_h > MAX_SIGNAL_AGE_H and tier != "CONVICTION":
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