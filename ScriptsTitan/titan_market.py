"""
TITAN — Market data fetching and trade feed. v8 FIXES:

1. PRICE BUG FIX: Store token_id→price mapping from trade records.
   The trade's `asset` field IS the token ID. We use this to build a
   definitive asset→price map so we never get the wrong outcome's price.
   get_outcome_price() now takes an optional asset/token_id hint.

2. RESOLVED MARKET DETECTION: Markets where price ≤ 0.02 or ≥ 0.98
   are treated as resolving/resolved → trigger position close.

3. HFT SIGNIFICANT TRADE FILTER: For HFT wallets, only mirror trades
   that are significantly above their rolling average (conviction filter).

4. LARGE TRADE DETECTION: Trades >> whale avg_bet get elevated priority
   and higher bet sizing multiplier.

KEY ARCHITECTURE FACTS about Polymarket:
  - An EVENT is a container (e.g. "Champions League Final").
  - Each EVENT has 1..N MARKETS. Each MARKET has its own conditionId.
  - Each MARKET is binary: exactly TWO outcome tokens (token 0 and token 1).
  - The `outcome` field in a trade is the LABEL of the token bought.
  - The `asset` field in a trade IS the token ID for that outcome.
    This is the most reliable way to map outcome→price.
"""

import time
import json
from datetime import datetime, timezone
import titan_state as S
from titan_config import *
from titan_wallet import fetch_wallet, get_elite_wallets, is_hft_wallet

# ─────────────────────────────────────────────────────────────────────────────
#  GAMMA API CIRCUIT BREAKER — v9 OVERHAUL
#  The old breaker (5 fails, 30s cooldown) was too lenient — the bot was
#  generating 100+ 422s per cycle. Now trips faster and stays off longer.
#  Also tracks per-CID failures to blacklist known-broken conditionIds.
# ─────────────────────────────────────────────────────────────────────────────
_gamma_fail_count   = 0
_gamma_open_until   = 0.0   # timestamp after which circuit is "closed" (normal)
_CIRCUIT_THRESHOLD  = 10    # consecutive 422s before tripping (raised from 3 — trips too fast)
_CIRCUIT_COOLDOWN   = 20    # seconds to pause (lowered from 45 — shorter recovery window)
_gamma_cid_fails: dict = {} # conditionId → fail_count (per-CID blacklist)
_CID_BLACKLIST_THRESHOLD = 3
_CID_BLACKLIST_DURATION  = 300  # 5 minutes (reduced from 10 — allow retry sooner)


def _gamma_get(url: str, params: dict) -> dict | list | None:
    """Wrapper around safe_get for Gamma API with circuit breaker."""
    global _gamma_fail_count, _gamma_open_until
    now = time.time()
    if now < _gamma_open_until:
        return None   # circuit open — skip request silently
    result = S.safe_get(url, params)
    if result is None:
        _gamma_fail_count += 1
        if _gamma_fail_count >= _CIRCUIT_THRESHOLD:
            _gamma_open_until  = now + _CIRCUIT_COOLDOWN
            _gamma_fail_count  = 0
            S._log(f"⚡ Gamma circuit breaker tripped — pausing {_CIRCUIT_COOLDOWN}s", "WARN")
    else:
        _gamma_fail_count = 0   # reset on success
    return result


def _is_cid_blacklisted(cid: str) -> bool:
    """Check if a conditionId is temporarily blacklisted due to repeated failures."""
    entry = _gamma_cid_fails.get(cid)
    if not entry:
        return False
    fails, last_fail_ts = entry
    if fails >= _CID_BLACKLIST_THRESHOLD:
        if time.time() - last_fail_ts < _CID_BLACKLIST_DURATION:
            return True
        else:
            # Blacklist expired — reset
            del _gamma_cid_fails[cid]
    return False


def _record_cid_failure(cid: str):
    """Record a failure for a conditionId."""
    old = _gamma_cid_fails.get(cid, (0, 0))
    _gamma_cid_fails[cid] = (old[0] + 1, time.time())


def _reset_cid_failures(cid: str):
    """Reset failure count on success."""
    _gamma_cid_fails.pop(cid, None)


# ─────────────────────────────────────────────────────────────────────────────
#  MARKET DATA
# ─────────────────────────────────────────────────────────────────────────────
def _fetch_market_raw(cid: str, asset: str = "", slug: str = "") -> dict | None:
    """
    Resolve a conditionId to a raw Gamma market dict.

    CONFIRMED WORKING Gamma API query params (from scan_top_market_holders):
      GET /markets?limit=N&active=true         → works (no conditionId filter)
      GET /markets?slug={slug}                 → works (slug filter)
      GET /markets?clob_token_ids=["{token}"]  → works (token ID filter)

    BROKEN / DO NOT USE:
      GET /markets?conditionId={cid}           → 422 Unprocessable Entity
      GET /markets/{cid}                       → 422 Unprocessable Entity
      GET data-api.polymarket.com/markets      → 404 Not Found

    Resolution order:
      1. Gamma slug lookup  — slug comes from the trade record directly
      2. Gamma clob_token_ids — asset/token ID from the trade record
      3. Data API trades endpoint — fetch 1 trade for this cid to get slug, then retry stage 1
    """
    def _pick(data) -> dict | None:
        if isinstance(data, list) and data:
            return data[0]
        if isinstance(data, dict) and data:
            return data
        return None

    def _cid_ok(m: dict) -> bool:
        rc = (m.get("conditionId") or m.get("condition_id") or "").lower()
        return not rc or rc == cid.lower()

    # ── Stage 1: Gamma clob_token_ids lookup (MOST RELIABLE for known positions)
    # The asset/token ID is globally unique to one specific outcome of one market.
    # This lookup never returns the wrong market. Preferred over slug.
    if asset:
        data = _gamma_get(f"{GAMMA_API}/markets", {
            "clob_token_ids": f'["{asset}"]', "limit": 1
        })
        m = _pick(data)
        if m and _cid_ok(m):
            return m

    # ── Stage 2: Gamma slug lookup ────────────────────────────────────────────
    # Trades carry a `slug` field. Gamma accepts ?slug= as a filter.
    # Slightly less reliable than asset — same event can have multiple markets.
    if slug:
        data = _gamma_get(f"{GAMMA_API}/markets", {"slug": slug, "limit": 1})
        m = _pick(data)
        if m and _cid_ok(m):
            return m

    # ── Stage 3: Bootstrap slug via Data API trade lookup ─────────────────────
    # As a last resort, query the Data API trades endpoint for this conditionId
    # to recover the slug, then retry the Gamma slug lookup.
    if not slug and not asset:
        trades_data = S.safe_get(f"{DATA_API}/trades", {
            "conditionId": cid, "limit": 1, "side": "BUY",
        })
        if trades_data and isinstance(trades_data, list) and trades_data:
            recovered_slug  = trades_data[0].get("slug", "")
            recovered_asset = trades_data[0].get("asset", "") or asset
            if recovered_slug:
                data = _gamma_get(f"{GAMMA_API}/markets", {
                    "slug": recovered_slug, "limit": 1
                })
                m = _pick(data)
                if m and _cid_ok(m):
                    return m
            if recovered_asset and recovered_asset != asset:
                data = _gamma_get(f"{GAMMA_API}/markets", {
                    "clob_token_ids": json.dumps([recovered_asset]), "limit": 1
                })
                m = _pick(data)
                if m and _cid_ok(m):
                    return m

    return None


def get_market(cid: str, trade_title: str = None, asset: str = "", slug: str = ""):
    """
    Fetch and cache market data for a conditionId.

    Each conditionId is a BINARY market (two outcome tokens).
    outcome_prices maps each outcome label AND each asset/token ID to its price.
    This dual mapping ensures we never return the wrong side's price.

    Pass asset= and slug= from the trade record for reliable lookup.
    slug is the most direct route to the Gamma market.
    asset (token ID) is the fallback via clob_token_ids.
    """
    now_t  = time.time()
    cached = S.market_cache.get(cid)
    if cached and (now_t - cached["ts"]) < MARKET_TTL:
        if trade_title and "?" in str(trade_title) and len(trade_title) > len(cached.get("title", "")):
            cached["title"] = trade_title
        return cached, None

    # During circuit-open period, return stale cache if available rather than
    # failing completely. A slightly stale price/liquidity is better than
    # dropping a valid HFT spike entirely.
    import titan_market as _self_mod
    if now_t < _self_mod._gamma_open_until and cached:
        S._log(f"⚡ Gamma circuit open — using stale cache for {cid[:20]}…", "DIAG")
        return cached, None

    # v9: Check per-CID blacklist — skip API call for known-broken conditionIds
    if _is_cid_blacklisted(cid):
        return None, f"CID blacklisted (repeated failures)"

    m = _fetch_market_raw(cid, asset=asset, slug=slug)
    if not m:
        _record_cid_failure(cid)
        return None, "API returned nothing"

    # Success — reset CID failure counter
    _reset_cid_failures(cid)

    # v9 FIX: Don't hard-reject closed/inactive markets here.
    # When a position is open and the market closes, we STILL need the price
    # to compute P&L. The caller (trader) will detect resolution via is_market_resolving().
    # We only reject closed markets during SIGNAL BUILDING (not position management).
    # NOTE: callers that need the closed-check should verify mkt["closed"] themselves.
    is_closed   = m.get("closed", False)
    is_inactive = not m.get("active", True)

    liq = float(m.get("liquidity") or 0)
    vol = float(m.get("volume")    or 0)

    # For closed/inactive markets, skip liquidity/volume gates — we only care
    # about the price for resolution. Return the result without caching so the
    # next active-market call fetches fresh data.
    if is_closed or is_inactive:
        raw_prices = m.get("outcomePrices") or "[]"
        try:
            prices = json.loads(raw_prices) if isinstance(raw_prices, str) else list(raw_prices)
            prices = [float(p) for p in prices]
            yes_price = prices[0] if prices else 0.5
            no_price  = prices[1] if len(prices) > 1 else (1.0 - yes_price)
        except Exception:
            return None, "Price parse failed (closed market)"
        # Return minimal dict for resolution detection only — don't cache
        return {
            "yes_price": yes_price, "no_price": no_price,
            "outcome_labels": [], "outcome_prices": {},
            "asset_to_price": {}, "asset_to_index": {},
            "token_index": {}, "index_to_price": {0: yes_price, 1: no_price},
            "liq": liq, "volume": vol, "title": trade_title or cid[:28],
            "hrs_left": 0, "slug": m.get("slug") or slug or "",
            "end_date": "", "event_slug": "", "ts": now_t,
        }, None

    if liq < MIN_LIQUIDITY:
        return None, f"Liq ${liq:,.0f} < ${MIN_LIQUIDITY:,}"
    if vol < MIN_VOLUME:
        return None, f"Vol ${vol:,.0f} < ${MIN_VOLUME:,}"

    raw_prices = m.get("outcomePrices") or "[]"
    try:
        prices = json.loads(raw_prices) if isinstance(raw_prices, str) else list(raw_prices)
        prices = [float(p) for p in prices]
        yes_price = prices[0] if prices else None
        no_price  = prices[1] if len(prices) > 1 else (1.0 - yes_price if yes_price else None)
    except Exception:
        return None, "Price parse failed"

    if not yes_price or not (0.02 < yes_price < 0.98):
        return None, f"Yes price {yes_price} out of bounds"

    raw_outcomes = m.get("outcomes") or "[]"
    try:
        outcome_labels = json.loads(raw_outcomes) if isinstance(raw_outcomes, str) else list(raw_outcomes)
        if not isinstance(outcome_labels, list):
            outcome_labels = []
    except Exception:
        outcome_labels = []

    # Build outcome_prices dict by label AND by position
    # CRITICAL: index 0 = yes_price (token 0), index 1 = no_price (token 1)
    outcome_prices = {}
    token_index    = {}   # label -> 0 or 1
    index_to_price = {}   # 0 -> yes_price, 1 -> no_price

    index_to_price[0] = prices[0] if prices else 0.5
    index_to_price[1] = prices[1] if len(prices) > 1 else (1.0 - index_to_price[0])

    if len(outcome_labels) >= 2 and len(prices) >= 2:
        lbl0 = str(outcome_labels[0])
        lbl1 = str(outcome_labels[1])
        outcome_prices[lbl0] = prices[0]
        outcome_prices[lbl1] = prices[1]
        token_index[lbl0] = 0
        token_index[lbl1] = 1
        # Lowercase versions for fuzzy matching
        outcome_prices[lbl0.lower()] = prices[0]
        outcome_prices[lbl1.lower()] = prices[1]
    else:
        outcome_prices["Yes"] = yes_price
        outcome_prices["No"]  = no_price if no_price else 1.0 - yes_price
        token_index["Yes"] = 0
        token_index["No"]  = 1

    # Build token_id→price map from clob_token_ids if available
    # This is the MOST reliable mapping: asset (token ID) → price
    asset_to_price = {}
    asset_to_index = {}
    clob_tokens = m.get("clobTokenIds") or m.get("clob_token_ids") or "[]"
    try:
        if isinstance(clob_tokens, str):
            clob_tokens = json.loads(clob_tokens)
        for i, token_id in enumerate(clob_tokens):
            if i < len(prices):
                asset_to_price[str(token_id)] = prices[i]
                asset_to_index[str(token_id)] = i
    except Exception:
        pass

    ed = m.get("endDate") or m.get("endDateIso") or ""
    hrs_left = None
    try:
        if ed:
            if ed.endswith("Z"):
                ed = ed[:-1] + "+00:00"
            edt = datetime.fromisoformat(ed)
            if edt.tzinfo is None:
                edt = edt.replace(tzinfo=timezone.utc)
            hrs_left = max(0, (edt - datetime.now(timezone.utc)).total_seconds() / 3600)
            if hrs_left < MIN_HOURS_LEFT:
                return None, f"Closes in {hrs_left:.1f}h"
    except Exception:
        pass

    gamma_title = m.get("question") or m.get("slug") or cid[:28]
    title = trade_title if (trade_title and len(trade_title) > 5 and "?" in trade_title) else gamma_title

    result = {
        "yes_price":      yes_price,
        "no_price":       no_price,
        "outcome_labels": outcome_labels,
        "outcome_prices": outcome_prices,
        "token_index":    token_index,
        "index_to_price": index_to_price,
        "asset_to_price": asset_to_price,
        "asset_to_index": asset_to_index,
        "liq":            liq,
        "volume":         vol,
        "title":          title,
        "end_date":       ed[:10] if len(ed) >= 10 else ed,
        "hrs_left":       hrs_left,
        # v9 FIX: store the market-level slug (not event slug) for reliable refresh
        "slug":           m.get("slug") or slug or "",
        "event_slug":     m.get("eventSlug") or m.get("event_slug") or "",
        "ts":             now_t,
    }
    S.market_cache[cid] = result
    return result, None


def get_outcome_price(mkt: dict, outcome: str, asset: str = "") -> float:
    """
    Look up the current price for a specific outcome token.

    Priority order:
      1. Asset/token ID match (most reliable - from trade record's `asset` field)
      2. Exact label match in outcome_prices dict
      3. Case-insensitive label match
      4. Known aliases (Yes/No)
      5. Fall back to yes_price (token 0)

    ALWAYS pass asset= when you have it from the trade record.
    """
    # 1. Asset/token ID match — most reliable
    if asset:
        ap = mkt.get("asset_to_price", {})
        if asset in ap:
            return ap[asset]

    op = mkt.get("outcome_prices", {})

    # 2. Exact match
    if outcome in op:
        return op[outcome]

    # 3. Case-insensitive match
    lower = outcome.lower()
    if lower in op:
        return op[lower]

    for label, price in op.items():
        if str(label).lower() == lower:
            return price

    # 4. Known aliases
    if lower in ("yes", "true", "1"):
        return mkt["yes_price"]
    if lower in ("no", "false", "0"):
        return mkt.get("no_price", 1.0 - mkt["yes_price"])

    # 5. Fall back — return yes_price silently
    return mkt["yes_price"]


def get_outcome_price_by_trade(mkt: dict, trade: dict) -> float:
    """
    Most accurate price lookup using both the outcome label AND the asset/token id.
    Always prefer this over get_outcome_price() when you have a trade record.
    """
    outcome = trade.get("outcome", "")
    asset   = trade.get("asset", "")

    # Try asset first (token ID = definitive)
    if asset:
        ap = mkt.get("asset_to_price", {})
        if asset in ap:
            return ap[asset]

    # Fall back to label-based lookup
    price = get_outcome_price(mkt, outcome, asset)

    # Secondary check: if we got yes_price but outcome might be token 1
    labels = mkt.get("outcome_labels", [])
    if len(labels) >= 2:
        lbl0 = str(labels[0])
        lbl1 = str(labels[1])
        if (outcome.lower() == lbl1.lower() or outcome.strip() == lbl1.strip()):
            return mkt.get("no_price", 1.0 - mkt["yes_price"])
        if (outcome.lower() == lbl0.lower() or outcome.strip() == lbl0.strip()):
            return mkt["yes_price"]

    return price


def fetch_position_price_fast(cid: str, asset: str, outcome: str) -> float | None:
    """
    Fast lightweight price fetch for open positions. Bypasses broken Gamma conditionId
    endpoint entirely.

    Strategy order:
    1. Data API /positions?asset=TOKEN - direct curPrice for our token.
    2. Gamma clob_token_ids - confirmed working, returns full price array indexed by token.
    3. Data API /trades?conditionId - recent trades. CRITICAL FIX: if we find the
       OPPOSITE outcome's trade price, INVERT it (1 - price) to get our token's price.
       e.g. we hold 'No' and find 'Yes' at 0.99 -> our 'No' = 1 - 0.99 = 0.01

    Returns float price or None if unavailable.
    """
    try:
        import json as _json

        # Strategy 1: positions endpoint - most direct (our token's current market value)
        if asset:
            pos_data = S.safe_get(f"{DATA_API}/positions", {"asset": asset, "limit": 1})
            if pos_data and isinstance(pos_data, list) and pos_data:
                cur_p = float(pos_data[0].get("curPrice") or pos_data[0].get("cur_price") or 0)
                if 0.001 <= cur_p <= 0.999:
                    return cur_p
                if cur_p < 0.001 and pos_data[0].get("curPrice") is not None:
                    return 0.005   # near-zero = effectively resolved loss
                if cur_p > 0.999 and pos_data[0].get("curPrice") is not None:
                    return 0.995   # near-one = effectively resolved win

        # Strategy 2: Gamma clob_token_ids - works when conditionId fails
        if asset:
            data = S.safe_get(f"{GAMMA_API}/markets", {
                "clob_token_ids": f'["{asset}"]', "limit": 1
            })
            if data and isinstance(data, list) and data:
                m = data[0]
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
                        cached = S.market_cache.get(cid)
                        if cached:
                            cached["asset_to_price"][asset] = p
                            if i == 0:
                                cached["yes_price"] = p
                            else:
                                cached["no_price"] = p
                            cached["ts"] = time.time()
                        return p

        # Strategy 3: Data API recent trades (always works, even post-resolution)
        # KEY FIX: invert price when we find the OPPOSITE outcome's recent trade
        data = S.safe_get(f"{DATA_API}/trades", {"conditionId": cid, "limit": 10})
        if data and isinstance(data, list):
            our_lower = outcome.lower().strip()
            best_direct = None
            best_inverted = None
            for t in data:
                t_price = float(t.get("price") or 0)
                t_outcome = (t.get("outcome") or "").lower().strip()
                t_asset = t.get("asset") or ""
                if t_price <= 0 or t_price >= 1:
                    continue
                # Direct asset match - most reliable
                if asset and t_asset == asset:
                    return t_price
                # Label match
                if t_outcome and our_lower:
                    if t_outcome == our_lower and best_direct is None:
                        best_direct = t_price
                    elif t_outcome != our_lower and best_inverted is None:
                        # Opposite outcome found - invert to get our price
                        best_inverted = 1.0 - t_price
            if best_direct is not None:
                return best_direct
            if best_inverted is not None:
                return best_inverted

    except Exception as e:
        S._log(f"  warning fetch_position_price_fast failed: {e}", "DIAG")
    return None


def is_market_resolving(mkt: dict) -> bool:
    """
    Returns True if a market appears to be resolving/resolved.
    A binary market resolves when one outcome goes to ~1.0 and the other to ~0.0.
    """
    yes_p = mkt.get("yes_price", 0.5)
    no_p  = mkt.get("no_price", 0.5)
    return yes_p <= 0.02 or yes_p >= 0.98 or no_p <= 0.02 or no_p >= 0.98


# ─────────────────────────────────────────────────────────────────────────────
#  TRADE NORMALISER
# ─────────────────────────────────────────────────────────────────────────────
def _normalise_trade(t: dict, wallet: str, hot_cutoff: float, warm_cutoff: float,
                     source: str, is_elite: bool = False) -> dict | None:
    try:
        ts = float(t.get("timestamp") or 0)
        if ts < warm_cutoff:
            return None
        price = float(t.get("price") or 0)
        if not (0.02 < price < 0.98):
            return None
        cid     = t.get("conditionId") or ""
        outcome = t.get("outcome") or ""
        asset   = t.get("asset") or ""
        if not cid or not outcome:
            return None
        cash = S.extract_cash(t)
        if cash <= 0:
            return None
        return {
            "wallet":     wallet.lower(),
            "name":       t.get("name") or t.get("pseudonym") or wallet[:10] + "…",
            "cid":        cid,
            "asset":      asset,   # v8: preserve asset/token ID for definitive price lookup
            "slug":       t.get("slug") or "",
            "event_slug": t.get("eventSlug") or "",
            "title":      t.get("title") or t.get("slug") or cid[:28],
            "outcome":    outcome,
            "price":      price,   # The actual trade price at time of trade
            "size":       float(t.get("size") or 0),
            "cash":       cash,
            "ts":         ts,
            "window":     "hot" if ts >= hot_cutoff else "warm",
            "source":     source,
            "is_elite":   is_elite,
        }
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
#  PER-WALLET POLLING
# ─────────────────────────────────────────────────────────────────────────────
def _poll_wallet_trades(wallet: str, limit: int, min_cash: float,
                        hot_cutoff: float, warm_cutoff: float,
                        source: str, is_elite: bool = False,
                        avg_bet: float = 0, hft: bool = False,
                        is_large_trade_mode: bool = False) -> list:
    data = S.safe_get(f"{DATA_API}/trades", {
        "user":         wallet,
        "limit":        limit,
        "side":         "BUY",
        "filterType":   "CASH",
        "filterAmount": min_cash,
    })
    if not data or not isinstance(data, list):
        return []
    if len(data) >= limit:
        S._log(f"⚠ Poll limit hit for {wallet[:14]}… ({len(data)}/{limit})", "DIAG")

    prof    = S.env().wallet_cache.get(wallet, {})
    name    = prof.get("name", wallet[:10] + "…")
    results = []
    for t in data:
        trade = _normalise_trade(t, wallet, hot_cutoff, warm_cutoff, source, is_elite)
        if trade is None:
            continue

        # For HFT wallets: only mirror trades that are SIGNIFICANTLY above their avg
        # This filters out their routine small trades and catches conviction bets
        if hft and avg_bet > 0:
            cash = trade["cash"]
            # Large trade = well above avg → always take
            if cash >= avg_bet * 3.0:
                trade["is_large_trade"] = True
                results.append(trade)
                continue
            # Below minimum threshold → skip
            if cash < max(HFT_MIN_CASH_PER_TRADE, avg_bet * ELITE_TRADE_MIN_FRACTION):
                S._log(
                    f"  ⏭ {name} HFT skip ${cash:,.0f} < "
                    f"{ELITE_TRADE_MIN_FRACTION*100:.0f}% of avg ${avg_bet:,.0f}",
                    "DIAG"
                )
                continue
            results.append(trade)
        elif not hft and avg_bet > 0 and trade["cash"] < avg_bet * ELITE_TRADE_MIN_FRACTION:
            S._log(
                f"  ⏭ {name} skipped ${trade['cash']:,.0f} < "
                f"{ELITE_TRADE_MIN_FRACTION*100:.0f}% of avg ${avg_bet:,.0f}",
                "DIAG"
            )
            continue
        else:
            # Flag large trades for all wallet types
            if avg_bet > 0 and trade["cash"] >= avg_bet * 3.0:
                trade["is_large_trade"] = True
            results.append(trade)
    return results


# ─────────────────────────────────────────────────────────────────────────────
#  VIP / ELITE DIRECT POLLING (PRIMARY source)
# ─────────────────────────────────────────────────────────────────────────────
def _poll_vip_and_elite(hot_cutoff: float, warm_cutoff: float) -> list:
    elite_addrs = set()
    for e in S.wallets:
        elite_addrs.update(a.lower() for a, p in e.wallet_cache.items() if p.get("elite"))
    vip_addrs   = {a.lower() for a in VIP_WALLETS}
    all_to_poll = elite_addrs | vip_addrs

    if not all_to_poll:
        return []

    S._log(f"🐳 Polling {len(all_to_poll)} elite/VIP wallets…", "DIAG")
    results = []

    for wallet in sorted(all_to_poll):
        # Merge profile stats from whatever wallet has it
        prof = {}
        for e in S.wallets:
            if wallet in e.wallet_cache:
                prof = e.wallet_cache[wallet]
                break
        is_elite = prof.get("elite", False)
        hft      = is_hft_wallet(prof)
        avg_bet  = prof.get("avg_bet", 0)

        if hft:
            min_cash = HFT_MIN_CASH_PER_TRADE
            limit    = HFT_POLL_LIMIT
            source   = "hft_poll"
        else:
            min_cash = ELITE_POLL_MIN_CASH
            limit    = ELITE_POLL_LIMIT
            source   = "elite_poll" if is_elite else "vip_poll"

        trades = _poll_wallet_trades(
            wallet, limit, min_cash, hot_cutoff, warm_cutoff,
            source, is_elite, avg_bet, hft
        )
        results.extend(trades)
        time.sleep(0.07)

    S._log(f"🐳 VIP/Elite poll done — {len(results)} trades from {len(all_to_poll)} wallets", "DIAG")
    return results


# ─────────────────────────────────────────────────────────────────────────────
#  WATCHLIST POLLING (verified non-elite)
# ─────────────────────────────────────────────────────────────────────────────
def _poll_watchlist(hot_cutoff: float, warm_cutoff: float, already_polled: set) -> list:
    candidates = set()
    for e in S.wallets:
        for w in e.watchlist:
            if w not in already_polled and e.wallet_cache.get(w, {}).get("verified"):
                candidates.add(w)
    candidates = list(candidates)[:50]

    results = []
    for wallet in candidates:
        prof = {}
        for e in S.wallets:
            if wallet in e.wallet_cache:
                prof = e.wallet_cache[wallet]
                break
        avg_bet = prof.get("avg_bet", 0)
        trades  = _poll_wallet_trades(
            wallet, 20, max(50.0, float(MIN_TRADE_CASH)),
            hot_cutoff, warm_cutoff, "watchlist_poll",
            False, avg_bet, False
        )
        results.extend(trades)
        time.sleep(0.07)
    return results


# ─────────────────────────────────────────────────────────────────────────────
#  PUBLIC FEED (secondary — discovery + confluence)
# ─────────────────────────────────────────────────────────────────────────────
def _fetch_public_feed(hot_cutoff: float, warm_cutoff: float) -> list:
    pub_data = S.safe_get(f"{DATA_API}/trades", {
        "limit":        MAX_TRADES_FETCH,
        "filterType":   "CASH",
        "filterAmount": MIN_TRADE_CASH,
        "side":         "BUY",
    })
    if not pub_data or not isinstance(pub_data, list):
        S._log("⚠ Public feed returned nothing", "WARN")
        return []
    if len(pub_data) >= MAX_TRADES_FETCH:
        S._log(f"⚠ Public feed hit limit ({MAX_TRADES_FETCH}). Reduce WARM_HOURS.", "WARN")
    results = []
    for t in pub_data:
        wallet = (t.get("proxyWallet") or "").lower()
        if not wallet:
            continue
        trade = _normalise_trade(t, wallet, hot_cutoff, warm_cutoff, "public_feed")
        if trade:
            results.append(trade)
    return results


# ─────────────────────────────────────────────────────────────────────────────
#  SELL DETECTION  — check if a wallet sold a specific conditionId
# ─────────────────────────────────────────────────────────────────────────────
def fetch_wallet_sells(wallet: str, since_ts: float, limit: int = 100) -> list:
    """
    Fetch SELL activity for a wallet since a given timestamp.
    Returns list of {cid, ts, asset, price, cash} dicts.
    """
    sells = []

    # Approach 1: /trades with side=SELL
    data = S.safe_get(f"{DATA_API}/trades", {
        "user":  wallet,
        "side":  "SELL",
        "limit": limit,
    })
    if data and isinstance(data, list):
        for t in data:
            ts  = float(t.get("timestamp") or 0)
            if ts < since_ts:
                continue
            cid = t.get("conditionId") or t.get("asset") or ""
            if cid:
                sells.append({
                    "cid":   cid,
                    "asset": t.get("asset") or "",
                    "ts":    ts,
                    "price": float(t.get("price") or 0),
                    "cash":  S.extract_cash(t),
                })

    # Approach 2: /activity with type=TRADE, side=SELL
    if not sells:
        data2 = S.safe_get(f"{DATA_API}/activity", {
            "user":          wallet,
            "type":          "TRADE",
            "side":          "SELL",
            "limit":         limit,
            "sortBy":        "TIMESTAMP",
            "sortDirection": "DESC",
        })
        if data2 and isinstance(data2, (list, dict)):
            if isinstance(data2, dict):
                data2 = data2.get("data", [])
            for t in data2:
                ts  = float(t.get("timestamp") or 0)
                if ts < since_ts:
                    continue
                cid = t.get("conditionId") or t.get("asset") or ""
                if cid:
                    sells.append({
                        "cid":   cid,
                        "asset": t.get("asset") or "",
                        "ts":    ts,
                        "price": float(t.get("price") or 0),
                        "cash":  S.extract_cash(t),
                    })

    return sells


# ─────────────────────────────────────────────────────────────────────────────
#  MAIN FETCH ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────
def fetch_trades() -> list:
    hot_cutoff  = time.time() - HOT_HOURS  * 3600
    warm_cutoff = time.time() - WARM_HOURS * 3600

    priority = _poll_vip_and_elite(hot_cutoff, warm_cutoff)
    polled   = {t["wallet"] for t in priority}

    watchlist_trades = _poll_watchlist(hot_cutoff, warm_cutoff, polled)
    public    = _fetch_public_feed(hot_cutoff, warm_cutoff)

    # Dedup: keep most recent trade per (wallet, cid, outcome)
    # Priority source trades override public feed trades for the same key
    best: dict = {}
    source_priority = {"hft_poll": 3, "elite_poll": 3, "vip_poll": 3,
                       "watchlist_poll": 2, "public_feed": 1}
    for trade in priority + watchlist_trades + public:
        key = (trade["wallet"], trade["cid"], trade["outcome"])
        if key not in best:
            best[key] = trade
        else:
            existing = best[key]
            src_new = source_priority.get(trade["source"], 1)
            src_old = source_priority.get(existing["source"], 1)
            # Prefer higher-priority source; break ties by recency
            if src_new > src_old or (src_new == src_old and trade["ts"] > existing["ts"]):
                best[key] = trade

    return list(best.values())

# ─────────────────────────────────────────────────────────────────────────────
#  HFT SPIKE FAST POLL — W8 dedicated, runs every 3-5s on its own thread
# ─────────────────────────────────────────────────────────────────────────────
def fetch_hft_spike_trades() -> list:
    """
    Dedicated fast poll for W8 (HFT Spike Detector).

    Only polls wallets classified as HFT. Filters immediately to trades that
    are >= HFT_SPIKE_MULTIPLIER x the wallet's avg_bet with NO absolute dollar
    floor. The spike ratio is what matters, not the dollar amount.

    Called every HFT_FAST_CYCLE_SECONDS (3s) from a dedicated thread in the
    engine — completely independent of the main 15s loop so W8 never misses
    a short-lived HFT spike because the main cycle was busy.
    """
    spike_cutoff = time.time() - 90  # only care about last 90 seconds
    hot_cutoff   = spike_cutoff
    warm_cutoff  = spike_cutoff

    # Gather all known HFT wallets from the shared cache
    hft_wallets = {}
    for e in S.wallets:
        for addr, prof in e.wallet_cache.items():
            if is_hft_wallet(prof) and addr not in hft_wallets:
                hft_wallets[addr] = prof

    if not hft_wallets:
        return []

    results = []
    for wallet, prof in hft_wallets.items():
        avg_bet = prof.get("avg_bet", 0)
        if avg_bet <= 0:
            continue  # no baseline, can't compute spike ratio

        raw = S.safe_get(f"{DATA_API}/trades", {
            "user":         wallet,
            "limit":        15,
            "side":         "BUY",
            "filterType":   "CASH",
            "filterAmount": max(1.0, avg_bet * 0.5),
        })
        if not raw or not isinstance(raw, list):
            time.sleep(0.05)
            continue

        for t in raw:
            trade = _normalise_trade(t, wallet, hot_cutoff, warm_cutoff, "hft_spike_poll")
            if trade is None:
                continue
            cash = trade["cash"]

            # Core gate: spike ratio based on trades/hour
            # Higher-frequency bots need a bigger spike to be meaningful
            tph = prof.get("trades_per_hour", 0)
            required_mult = HFT_SPIKE_MULTIPLIER_HIGH if tph > 200 else HFT_SPIKE_MULTIPLIER_LOW
            if cash < avg_bet * required_mult:
                continue  # routine noise

            # Absolute cash floor — skip tiny spikes even if ratio is met
            if cash < HFT_SPIKE_MIN_ABS_CASH:
                continue

            trade["is_large_trade"]   = True
            trade["hft_spike_ratio"]  = round(cash / avg_bet, 1)
            trade["source"]           = "hft_spike_poll"
            results.append(trade)

            name = prof.get("name", wallet[:10] + "…")
            S._log(
                f"⚡ HFT SPIKE: {name} ${cash:,.0f} = {cash/avg_bet:.0f}x avg "
                f"[{trade.get('title','?')[:35]}]",
                "INFO"
            )

        time.sleep(0.05)

    return results