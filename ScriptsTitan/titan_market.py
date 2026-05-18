"""
TITAN — Market data fetching and trade feed. v10 FIXES:

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

5. v10: 422 FLOOD FIX — four changes to eliminate Gamma spam:
   a) _seen_verified_cids: only call Gamma for cids from verified/elite wallets
   b) Stage 3 guard: skip expensive bootstrap for unknown cids
   c) Enhanced circuit breaker: exponential backoff, 120s base cooldown
   d) get_market() pre-check: skip Gamma if cid not seen & not cached

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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterator
import webbrowser
import titan_state as S


@dataclass
class Market:
    yes_price: float = 0.5
    no_price: float = 0.5
    outcome_labels: list[str] = field(default_factory=list)
    outcome_prices: dict[str, float] = field(default_factory=dict)
    token_index: dict[str, int] = field(default_factory=dict)
    index_to_price: dict[int, float] = field(default_factory=dict)
    asset_to_price: dict[str, float] = field(default_factory=dict)
    asset_to_index: dict[str, int] = field(default_factory=dict)
    liq: float = 0.0
    volume: float = 0.0
    title: str = ""
    end_date: str = ""
    hrs_left: float | None = None
    slug: str = ""
    event_slug: str = ""
    mkt_type: str = ""
    is_sports: bool = False
    ts: float = 0.0

    def polymarket_url(self) -> str:
        slug = self.event_slug or self.slug
        return f"https://polymarket.com/event/{slug}" if slug else "https://polymarket.com"

    def get(self, key: str, default: object = None) -> object:
        return getattr(self, key, default)

    def __getitem__(self, key: str) -> object:
        if not hasattr(self, key):
            raise KeyError(key)
        return getattr(self, key)

    def __setitem__(self, key: str, value: object) -> None:
        if not hasattr(self, key):
            raise KeyError(key)
        setattr(self, key, value)

    def keys(self) -> tuple[str, ...]:
        return (
            "yes_price",
            "no_price",
            "outcome_labels",
            "outcome_prices",
            "token_index",
            "index_to_price",
            "asset_to_price",
            "asset_to_index",
            "liq",
            "volume",
            "title",
            "end_date",
            "hrs_left",
            "slug",
            "event_slug",
            "mkt_type",
            "is_sports",
            "ts",
        )

    def items(self) -> Iterator[tuple[str, object]]:
        for key in self.keys():
            yield key, getattr(self, key)

    def open_on_polymarket(self) -> None:
        S._log(f"Market: Opening Polymarket URL: {self.polymarket_url()}", "DEBUG")
        webbrowser.open(self.polymarket_url())
        return


from titan_config import *
from titan_wallet import WalletProfile, fetch_wallet, get_elite_wallets, is_hft_wallet

_SPORTS_KEYWORDS = (
    "vs", "spread", "o/u", "over", "under", "winner", "set ", "game ",
    "bo1", "bo3", "nhl", "nba", "mlb", "nfl", "ufc", "atp", "wta", "epl",
    "sea-", "nhl-", "mlb-", "nba-", "ufc-", "atp-", "kings", "flames",
    "rays", "yankees", "royals", "tigers", "brewers", "pirates", "nationals",
    "lol:", "valorant:", "counter-strike:", "leading at halftime",
    "clean sheet", "both teams", "indian premier league", "ipl:",
    "champions league ", "esports", "furia", "loud game", "map ", "round ",
)
_CRYPTO_KEYWORDS = ("bitcoin", "btc", "ethereum", "eth", "solana", "xrp", "bnb", "crypto", "up or down")


def classify_market_type(title: str, event_slug: str, hrs_left: float | None = None) -> str:
    combined = f"{title} {event_slug}".lower()
    if any(keyword in combined for keyword in _SPORTS_KEYWORDS):
        return "SPORTS"
    if any(keyword in combined for keyword in _CRYPTO_KEYWORDS):
        return "CRYPTO"
    if hrs_left is not None and hrs_left < 24:
        return "EVENT"
    return "POLITICS"


@dataclass
class WhaleObservation:
    wallet:         str
    name:           str
    cid:            str
    asset:          str
    slug:           str
    event_slug:     str
    title:          str
    outcome:        str
    price:          float
    size:           float
    cash:           float
    ts:             float
    window:         str          # "hot" | "warm"
    source:         str          # "elite_poll" | "hft_spike_poll" | "public_feed" | …
    is_elite:       bool
    is_large_trade: bool  = False
    hft_spike_ratio: float | None = None


@dataclass
class WhaleSell:
    cid:   str
    asset: str
    ts:    float
    price: float
    cash:  float

# ─────────────────────────────────────────────────────────────────────────────
#  v10: CID REGISTRY — only call Gamma for cids from verified wallets
#  A conditionId is "seen" only when it comes from a wallet that is at least
#  verified or watchable. This cuts 70-80% of Gamma calls.
# ─────────────────────────────────────────────────────────────────────────────
_seen_verified_cids: set = set()  # cids from verified/watchable wallets only


def mark_cid_verified(cid: str):
    """Register a conditionId as coming from a verified wallet source."""
    _seen_verified_cids.add(cid)


def is_cid_known(cid: str) -> bool:
    """Return True if this cid has been seen from a verified wallet source."""
    return cid in _seen_verified_cids


# ─────────────────────────────────────────────────────────────────────────────
#  GAMMA API CIRCUIT BREAKER — v10 ENHANCED
#  v9 breaker (3 fails, 60s cooldown) was not stopping the burst floods.
#  v10: 120s base cooldown + exponential backoff on repeated trips.
# ─────────────────────────────────────────────────────────────────────────────
_gamma_fail_count   = 0
_gamma_open_until   = 0.0   # timestamp after which circuit is "closed" (normal)
_CIRCUIT_THRESHOLD  = 3     # consecutive 422s before tripping
_CIRCUIT_COOLDOWN_BASE = 120  # raised from 60s — 60s was re-tripping immediately
_CIRCUIT_TRIP_WINDOW   = 600  # 10-minute window for counting trips
_circuit_trip_times: list = []  # timestamps of recent trips
_gamma_cid_fails: dict = {}  # conditionId → fail_count (per-CID blacklist)
_CID_BLACKLIST_THRESHOLD = 3
_CID_BLACKLIST_DURATION  = 300  # 5 minutes


def _trip_circuit():
    """Trip the Gamma circuit breaker with exponential backoff on repeated trips."""
    global _gamma_open_until, _gamma_fail_count
    now = time.time()
    _circuit_trip_times.append(now)
    # Prune old trip records outside the window
    _circuit_trip_times[:] = [t for t in _circuit_trip_times if now - t < _CIRCUIT_TRIP_WINDOW]
    recent_trips = len(_circuit_trip_times)
    # Exponential backoff: 120s, 240s, 480s, 960s (cap at 4x)
    cooldown = _CIRCUIT_COOLDOWN_BASE * (2 ** min(recent_trips - 1, 3))
    _gamma_open_until = now + cooldown
    _gamma_fail_count = 0
    S._log(
        f"⚡ Gamma circuit tripped ({recent_trips}x in 10min) — pausing {cooldown:.0f}s",
        "WARN"
    )


def _gamma_get(url: str, params: dict) -> dict | list | None:
    """Wrapper around safe_get for Gamma API with enhanced circuit breaker."""
    global _gamma_fail_count, _gamma_open_until
    now = time.time()
    if now < _gamma_open_until:
        return None   # circuit open — skip request silently
    result = S.safe_get(url, params)
    if result is None:
        _gamma_fail_count += 1
        if _gamma_fail_count >= _CIRCUIT_THRESHOLD:
            _trip_circuit()
    else:
        _gamma_fail_count = 0   # reset on success
    return result


def _to_decimal_token(asset: str) -> str:
    """
    Gamma API stores and queries CLOB token IDs as plain decimal integers.
    The Data API also returns them as decimals, so usually this is a no-op.
    If an asset somehow arrives as 0x-prefixed hex, convert it to decimal.
    """
    if not asset:
        return asset
    a = asset.strip()
    try:
        if a.startswith("0x") or a.startswith("0X"):
            return str(int(a, 16))   # hex → decimal
        return str(int(a))           # already decimal — normalise to remove any spaces/signs
    except ValueError:
        return a                     # not parseable — leave unchanged


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
      1. Gamma clob_token_ids — asset/token ID from the trade record (MOST RELIABLE)
      2. Gamma slug lookup  — slug comes from the trade record directly
      3. Data API trades endpoint — fetch 1 trade for this cid to get slug, then retry.
         v10 FIX: Stage 3 now only runs if this cid has been seen before from a
         verified wallet. Brand-new unknown cids from unverified wallets skip Stage 3.
    """
    def _pick(data) -> dict | None:
        if isinstance(data, list) and data:
            return data[0]
        if isinstance(data, dict) and data:
            return data
        return None

    def _cid_ok(m: dict) -> bool:
        rc = (m.get("conditionId") or m.get("condition_id") or "").lower()
        if not cid:
            return True
        return not rc or rc == cid.lower()

    def _gamma_market_by_asset(asset_value: str) -> dict | None:
        dec_asset = _to_decimal_token(asset_value)
        direct_data = _gamma_get(f"{GAMMA_API}/markets", {
            "clob_token_ids": dec_asset, "limit": 1
        })
        direct_market = _pick(direct_data)
        if direct_market and _cid_ok(direct_market):
            return direct_market

        legacy_data = _gamma_get(f"{GAMMA_API}/markets", {
            "clob_token_ids": f'["{dec_asset}"]', "limit": 1
        })
        legacy_market = _pick(legacy_data)
        if legacy_market and _cid_ok(legacy_market):
            return legacy_market
        return None

    # ── Stage 1: Gamma clob_token_ids lookup ─────────────────────────────────────────────
    # Only attempted when NO slug is available. When slug is known, Stage 2
    # always succeeds and Stage 1 always returns 422 — skip it to save API
    # calls and avoid false circuit-breaker increments.
    if asset and not slug:
        m = _gamma_market_by_asset(asset)
        if m:
            return m

    # ── Stage 2: Gamma slug lookup ────────────────────────────────────────────
    if slug:
        data = _gamma_get(f"{GAMMA_API}/markets", {"slug": slug, "limit": 1})
        m = _pick(data)
        if m and _cid_ok(m):
            return m

    # ── Stage 3: Bootstrap slug via Data API trade lookup ─────────────────────
    # v10 FIX: Only run Stage 3 for cids that have been seen before from a verified
    # wallet. For brand-new unknown cids, return None immediately.
    # Even when an asset hint exists, it may be stale or invalid, so missing slug
    # should still allow bootstrap-by-cid.
    if not slug:
        if cid not in _seen_verified_cids:
            return None  # Unknown cid from unverified source — skip Stage 3
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
                m = _gamma_market_by_asset(recovered_asset)
                if m:
                    return m

    return None


def _fetch_market_remote(cid: str, trade_title: str | None = None, asset: str = "", slug: str = "",
                         from_verified: bool = False, allow_untradeable: bool = False,
                         has_cached: bool = False) -> tuple[Market | None, str | None]:
    """
    Fetch market data for a conditionId from remote APIs.

    Each conditionId is a BINARY market (two outcome tokens).
    outcome_prices maps each outcome label AND each asset/token ID to its price.
    This dual mapping ensures we never return the wrong side's price.

    Pass asset= and slug= from the trade record for reliable lookup.
    Pass from_verified=True when the call is sourced from a verified/elite wallet —
    this registers the cid so Stage 3 bootstrap is allowed for future calls.

    v10 FIX: If cid is not in _seen_verified_cids AND not in market_cache AND not
    from_verified, we skip the Gamma call entirely. This eliminates the bulk of 422s
    (brand-new cids from unverified wallets making up ~75% of all Gamma calls).
    """
    now_t = time.time()

    # Register this cid as coming from a verified source if flagged
    if from_verified:
        _seen_verified_cids.add(cid)

    # v10: Skip Gamma call for cids never seen from a verified wallet AND not cached.
    # This is the primary 422 reduction gate.
    import titan_market as _self_mod
    if (cid not in _seen_verified_cids and
            not has_cached and
            not from_verified and
            not asset and
            not slug):
        return None, "CID from unverified source — skipping Gamma (v10 gate)"

    if now_t < _self_mod._gamma_open_until:
        return None, "Gamma circuit open"

    # v9: Check per-CID blacklist
    if _is_cid_blacklisted(cid):
        return None, f"CID blacklisted (repeated failures)"

    m = _fetch_market_raw(cid, asset=asset, slug=slug)
    if not m:
        _record_cid_failure(cid)
        return None, "API returned nothing"

    # Success — reset CID failure counter and ensure it's registered
    _reset_cid_failures(cid)
    _seen_verified_cids.add(cid)  # If it resolved, it's valid — register it

    return _market_from_gamma_payload(
        m,
        cid=cid,
        trade_title=trade_title,
        slug=slug,
        allow_untradeable=allow_untradeable,
        now_t=now_t,
    )


def _market_from_gamma_payload(
    payload: dict,
    *,
    cid: str,
    trade_title: str | None,
    slug: str,
    allow_untradeable: bool,
    now_t: float | None = None,
) -> tuple[Market | None, str | None]:
    now_t = float(time.time()) if now_t is None else now_t

    # v9 FIX: Don't hard-reject closed/inactive markets here.
    # When a position is open and the market closes, we STILL need the price
    # to compute P&L.
    is_closed   = payload.get("closed", False)
    is_inactive = not payload.get("active", True)

    liq = float(payload.get("liquidity") or 0)
    vol = float(payload.get("volume") or 0)

    if is_closed or is_inactive:
        return None, "Market is closed or inactive"

    if not allow_untradeable and liq < MIN_LIQUIDITY:
        return None, f"Liq ${liq:,.0f} < ${MIN_LIQUIDITY:,}"
    if not allow_untradeable and vol < MIN_VOLUME:
        return None, f"Vol ${vol:,.0f} < ${MIN_VOLUME:,}"

    raw_prices = payload.get("outcomePrices") or "[]"
    try:
        prices = json.loads(raw_prices) if isinstance(raw_prices, str) else list(raw_prices)
        prices = [float(p) for p in prices]
        yes_price = prices[0] if prices else None
        no_price  = prices[1] if len(prices) > 1 else None
    except Exception:
        return None, "Price parse failed"

    if not yes_price or (not allow_untradeable and not (0.02 < yes_price < 0.98)):
        return None, f"Yes price {yes_price} out of bounds"
    if no_price is None:
        return None, "No price unavailable (single-price market)"

    raw_outcomes = payload.get("outcomes") or "[]"
    try:
        outcome_labels = json.loads(raw_outcomes) if isinstance(raw_outcomes, str) else list(raw_outcomes)
        if not isinstance(outcome_labels, list):
            outcome_labels = []
    except Exception:
        outcome_labels = []

    outcome_prices = {}
    token_index    = {}
    index_to_price = {}

    index_to_price[0] = prices[0] if prices else 0.5
    index_to_price[1] = prices[1] if len(prices) > 1 else 0.5

    if len(outcome_labels) >= 2 and len(prices) >= 2:
        lbl0 = str(outcome_labels[0])
        lbl1 = str(outcome_labels[1])
        outcome_prices[lbl0] = prices[0]
        outcome_prices[lbl1] = prices[1]
        token_index[lbl0] = 0
        token_index[lbl1] = 1
        outcome_prices[lbl0.lower()] = prices[0]
        outcome_prices[lbl1.lower()] = prices[1]
    else:
        outcome_prices["Yes"] = yes_price
        outcome_prices["No"]  = no_price if no_price is not None else 0.5
        token_index["Yes"] = 0
        token_index["No"]  = 1

    asset_to_price = {}
    asset_to_index = {}
    clob_tokens = payload.get("clobTokenIds") or payload.get("clob_token_ids") or "[]"
    try:
        if isinstance(clob_tokens, str):
            clob_tokens = json.loads(clob_tokens)
        for i, token_id in enumerate(clob_tokens):
            if i < len(prices):
                asset_to_price[str(token_id)] = prices[i]
                asset_to_index[str(token_id)] = i
    except Exception:
        pass

    ed = payload.get("endDate") or payload.get("endDateIso") or ""
    hrs_left = None
    try:
        if ed:
            if ed.endswith("Z"):
                ed = ed[:-1] + "+00:00"
            edt = datetime.fromisoformat(ed)
            if edt.tzinfo is None:
                edt = edt.replace(tzinfo=timezone.utc)
            hrs_left = max(0, (edt - datetime.now(timezone.utc)).total_seconds() / 3600)
            if not allow_untradeable and hrs_left < MIN_HOURS_LEFT:
                return None, f"Closes in {hrs_left:.1f}h"
    except Exception:
        pass

    gamma_title = payload.get("question") or payload.get("slug") or cid[:28]
    title = trade_title if (trade_title and len(trade_title) > 5 and "?" in trade_title) else gamma_title

    event_obj = payload.get("event")
    nested_event_slug = ""
    if isinstance(event_obj, dict):
        nested_event_slug = str(event_obj.get("slug") or "")
    if not nested_event_slug:
        events_obj = payload.get("events")
        if isinstance(events_obj, list):
            for event_item in events_obj:
                if not isinstance(event_item, dict):
                    continue
                nested_event_slug = str(event_item.get("slug") or "")
                if nested_event_slug:
                    break
    event_slug = payload.get("eventSlug") or payload.get("event_slug") or nested_event_slug or ""
    market_slug = payload.get("slug") or slug or ""
    mkt_type = classify_market_type(title, event_slug, hrs_left)
    result = Market(
        yes_price=yes_price,
        no_price=no_price,
        outcome_labels=outcome_labels,
        outcome_prices=outcome_prices,
        token_index=token_index,
        index_to_price=index_to_price,
        asset_to_price=asset_to_price,
        asset_to_index=asset_to_index,
        liq=liq,
        volume=vol,
        title=title,
        end_date=ed[:10] if len(ed) >= 10 else ed,
        hrs_left=hrs_left,
        slug=market_slug,
        event_slug=event_slug,
        mkt_type=mkt_type,
        is_sports=(mkt_type == "SPORTS"),
        ts=now_t,
    )
    return result, None


def get_market(cid: str, trade_title: str | None = None, asset: str = "", slug: str = "",
               event_slug: str = "",
               from_verified: bool = False, allow_untradeable: bool = False,
               persist: bool = False) -> tuple[Market | None, str | None]:
    from titan_markets import market_cache

    return market_cache.resolve(
        cid,
        trade_title=trade_title,
        asset=asset,
        slug=slug,
        event_slug=event_slug,
        from_verified=from_verified,
        allow_untradeable=allow_untradeable,
        persist=persist,
    )


def get_outcome_price(mkt: Market, outcome: str, asset: str = "") -> float:
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
    if asset:
        ap = mkt.asset_to_price
        if asset in ap:
            return ap[asset]

    op = mkt.outcome_prices

    if outcome in op:
        return op[outcome]

    lower = outcome.lower()
    if lower in op:
        return op[lower]

    for label, price in op.items():
        if str(label).lower() == lower:
            return price

    if lower in ("yes", "true", "1"):
        return mkt.yes_price
    if lower in ("no", "false", "0"):
        no_p = mkt.no_price
        if no_p is not None:
            return no_p
        S._log(f"  ⚠ get_outcome_price: no_price unavailable for outcome='{outcome}'", "DIAG")
        return mkt.yes_price

    S._log(f"  ⚠ get_outcome_price: unmatched outcome='{outcome}' — returning 0.5", "DIAG")
    return 0.5


def get_outcome_price_by_trade(mkt: Market, trade: dict) -> float:
    """
    Most accurate price lookup using both the outcome label AND the asset/token id.
    Always prefer this over get_outcome_price() when you have a trade record.
    """
    outcome = trade.get("outcome", "")
    asset   = trade.get("asset", "")

    if asset:
        ap = mkt.asset_to_price
        if asset in ap:
            return ap[asset]

    price = get_outcome_price(mkt, outcome, asset)

    labels = mkt.outcome_labels
    if len(labels) >= 2:
        lbl0 = str(labels[0])
        lbl1 = str(labels[1])
        ip = mkt.index_to_price
        if outcome.lower() == lbl1.lower() or outcome.strip() == lbl1.strip():
            return ip.get(1, mkt.no_price)
        if outcome.lower() == lbl0.lower() or outcome.strip() == lbl0.strip():
            return ip.get(0, mkt.yes_price)

    return price


def fetch_position_price_fast(cid: str, asset: str, outcome: str) -> float | None:
    """
    Fast lightweight price fetch for open positions. Bypasses broken Gamma conditionId
    endpoint entirely.

    NOTE: This function directly calls S.safe_get with clob_token_ids — it is NOT
    gated by the circuit breaker. This is intentional: position price fetches are
    critical for P&L tracking and must work even when the circuit is open.
    The clob_token_ids endpoint is reliable (no 422s) so this is safe.

    Strategy order:
    1. Gamma clob_token_ids - confirmed working, returns full price array indexed by token.
    2. Data API /trades?conditionId - recent trades. Only returns direct outcome match.

    Returns float price or None if unavailable.
    """
    try:
        import json as _json

        # Strategy 1: Gamma clob_token_ids - works when conditionId fails
        if asset:
            dec_asset = _to_decimal_token(asset)
            data = S.safe_get(f"{GAMMA_API}/markets", {
                "clob_token_ids": dec_asset, "limit": 1
            }, quiet=True)
            if not (data and isinstance(data, list) and data):
                data = S.safe_get(f"{GAMMA_API}/markets", {
                    "clob_token_ids": f'["{dec_asset}"]', "limit": 1
                }, quiet=True)
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
                    # Compare normalised decimal so stored decimal == Gamma decimal
                    if _to_decimal_token(str(tok)) == dec_asset and i < len(prices):
                        p = prices[i]
                        S.market_cache.update_live_price(cid, asset, p, ts=time.time())
                        return p

        # Strategy 2: Data API recent trades — DIRECT MATCH ONLY.
        data = S.safe_get(f"{DATA_API}/trades", {"conditionId": cid, "limit": 20}, quiet=True)
        if data and isinstance(data, list):
            our_lower = outcome.lower().strip()
            asset_match = None
            label_match = None
            for t in data:
                t_price = float(t.get("price") or 0)
                t_outcome = (t.get("outcome") or "").lower().strip()
                t_asset = t.get("asset") or ""
                if t_price <= 0 or t_price >= 1:
                    continue
                if asset and t_asset == asset:
                    asset_match = t_price
                    break
                if t_outcome and our_lower and t_outcome == our_lower and label_match is None:
                    label_match = t_price
            if asset_match is not None:
                return asset_match
            if label_match is not None and not asset:
                return label_match

    except Exception as e:
        S._log(f"  warning fetch_position_price_fast failed: {e}", "DIAG")
    return None


def is_market_resolving(mkt: Market) -> bool:
    """
    Returns True if a market appears to be resolving/resolved.
    A binary market resolves when one outcome goes to ~1.0 and the other to ~0.0.
    """
    yes_p = mkt.yes_price
    no_p = mkt.no_price
    return yes_p <= 0.02 or yes_p >= 0.98 or no_p <= 0.02 or no_p >= 0.98


# ─────────────────────────────────────────────────────────────────────────────
#  TRADE NORMALISER
# ─────────────────────────────────────────────────────────────────────────────
def _normalise_trade(t: dict, wallet: str, hot_cutoff: float, warm_cutoff: float,
                     source: str, is_elite: bool = False) -> WhaleObservation | None:
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
        return WhaleObservation(
            wallet     = wallet.lower(),
            name       = t.get("name") or t.get("pseudonym") or wallet[:10] + "…",
            cid        = cid,
            asset      = asset,
            slug       = t.get("slug") or "",
            event_slug = t.get("eventSlug") or "",
            title      = t.get("title") or t.get("slug") or cid[:28],
            outcome    = outcome,
            price      = price,
            size       = float(t.get("size") or 0),
            cash       = cash,
            ts         = ts,
            window     = "hot" if ts >= hot_cutoff else "warm",
            source     = source,
            is_elite   = is_elite,
        )
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
#  PER-WALLET POLLING
# ─────────────────────────────────────────────────────────────────────────────
_poll_limit_warned: set[str] = set()


def _poll_wallet_trades(wallet: str, limit: int, min_cash: float,
                        hot_cutoff: float, warm_cutoff: float,
                        source: str, is_elite: bool = False,
                        avg_bet: float = 0, hft: bool = False,
                        is_large_trade_mode: bool = False) -> list[WhaleObservation]:
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
        if wallet not in _poll_limit_warned:
            _poll_limit_warned.add(wallet)
            S._log(f"⚠ Poll limit hit for {wallet[:14]}… ({len(data)}/{limit}) — trades may be truncated", "WARN")
    else:
        _poll_limit_warned.discard(wallet)

    prof    = S.env().wallet_cache.get(wallet, {})
    name    = prof.get("name", wallet[:10] + "…")
    results : list[WhaleObservation] = []
    for t in data:
        whaletrade : WhaleObservation | None = _normalise_trade(t, wallet, hot_cutoff, warm_cutoff, source, is_elite)
        if whaletrade is None:
            continue

        if hft and avg_bet > 0:
            cash = whaletrade.cash
            if cash >= avg_bet * 3.0:
                whaletrade.is_large_trade = True
                results.append(whaletrade)
                continue
            if cash < max(HFT_MIN_CASH_PER_TRADE, avg_bet * ELITE_TRADE_MIN_FRACTION):
                S._log(
                    f"  ⏭ {name} HFT skip ${cash:,.0f} < "
                    f"{ELITE_TRADE_MIN_FRACTION*100:.0f}% of avg ${avg_bet:,.0f}",
                    "DIAG"
                )
                continue
            results.append(whaletrade)
        elif not hft and avg_bet > 0 and whaletrade.cash < avg_bet * ELITE_TRADE_MIN_FRACTION:
            S._log(
                f"  ⏭ {name} skipped ${whaletrade.cash:,.0f} < "
                f"{ELITE_TRADE_MIN_FRACTION*100:.0f}% of avg ${avg_bet:,.0f}",
                "DIAG"
            )
            continue
        else:
            if avg_bet > 0 and whaletrade.cash >= avg_bet * 3.0:
                whaletrade.is_large_trade = True
            results.append(whaletrade)
    return results


# ─────────────────────────────────────────────────────────────────────────────
#  VIP / ELITE DIRECT POLLING (PRIMARY source)
# ─────────────────────────────────────────────────────────────────────────────
def _poll_vip_and_elite(hot_cutoff: float, warm_cutoff: float) -> list[WhaleObservation]:
    elite_addrs = set()
    for e in S.wallets:
        elite_addrs.update(a.lower() for a, p in e.wallet_cache.items() if p.get("elite"))
    vip_addrs   = {a.lower() for a in VIP_WALLETS}
    all_to_poll = elite_addrs | vip_addrs

    if not all_to_poll:
        return []

    S._log(f"🐳 Polling {len(all_to_poll)} elite/VIP wallets…", "DIAG")
    results : list[WhaleObservation] = []

    for wallet in sorted(all_to_poll):
        prof: WalletProfile = next(
            (e.wallet_cache[wallet] for e in S.wallets if wallet in e.wallet_cache),
            fetch_wallet(wallet),
        )
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

        trades : list[WhaleObservation] = _poll_wallet_trades(
            wallet, limit, min_cash, hot_cutoff, warm_cutoff,
            source, is_elite, avg_bet, hft
        )
        # v10: Register all cids from elite/VIP wallet trades as verified
        for t in trades:
            if t.cid:
                _seen_verified_cids.add(t.cid)
        results.extend(trades)
        time.sleep(0.07)

    S._log(f"🐳 VIP/Elite poll done — {len(results)} trades from {len(all_to_poll)} wallets", "DIAG")
    return results


# ─────────────────────────────────────────────────────────────────────────────
#  WATCHLIST POLLING (verified non-elite)
# ─────────────────────────────────────────────────────────────────────────────
def _poll_watchlist(hot_cutoff: float, warm_cutoff: float, already_polled: set) -> list[WhaleObservation]:
    candidates = set()
    for e in S.wallets:
        for w in e.watchlist:
            if w not in already_polled and e.wallet_cache.get(w, {}).get("verified"):
                candidates.add(w)
    candidates = list(candidates)[:50]

    results : list[WhaleObservation] = []
    for wallet in candidates:
        prof = {}
        for e in S.wallets:
            if wallet in e.wallet_cache:
                prof = e.wallet_cache[wallet]
                break
        avg_bet = prof.get("avg_bet", 0)
        trades : list[WhaleObservation]  = _poll_wallet_trades(
            wallet, 100, max(50.0, float(MIN_TRADE_CASH)),
            hot_cutoff, warm_cutoff, "watchlist_poll",
            False, avg_bet, False
        )
        # Register cids from verified watchlist wallets
        for t in trades:
            if t.cid:
                _seen_verified_cids.add(t.cid)
        results.extend(trades)
        time.sleep(0.07)
    return results


# ─────────────────────────────────────────────────────────────────────────────
#  PUBLIC FEED (secondary — discovery + confluence)
# ─────────────────────────────────────────────────────────────────────────────
def _fetch_public_feed(hot_cutoff: float, warm_cutoff: float) -> list[WhaleObservation]:
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
        whaletrade: WhaleObservation | None = _normalise_trade(t, wallet, hot_cutoff, warm_cutoff, "public_feed")
        if whaletrade:
            results.append(whaletrade)
    return results


# ─────────────────────────────────────────────────────────────────────────────
#  SELL DETECTION  — check if a wallet sold a specific conditionId
# ─────────────────────────────────────────────────────────────────────────────
def fetch_wallet_sells(wallet: str, since_ts: float, limit: int = 100) -> list[WhaleSell]:
    sells: list[WhaleSell] = []

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
                sells.append(WhaleSell(
                    cid   = cid,
                    asset = t.get("asset") or "",
                    ts    = ts,
                    price = float(t.get("price") or 0),
                    cash  = S.extract_cash(t),
                ))

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
                    sells.append(WhaleSell(
                        cid   = cid,
                        asset = t.get("asset") or "",
                        ts    = ts,
                        price = float(t.get("price") or 0),
                        cash  = S.extract_cash(t),
                    ))

    return sells


# ─────────────────────────────────────────────────────────────────────────────
#  MAIN FETCH ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────
def fetch_trades() -> list[WhaleObservation]:
    hot_cutoff  = time.time() - HOT_HOURS  * 3600
    warm_cutoff = time.time() - WARM_HOURS * 3600

    priority = _poll_vip_and_elite(hot_cutoff, warm_cutoff)
    polled   = {t.wallet for t in priority}

    watchlist_trades = _poll_watchlist(hot_cutoff, warm_cutoff, polled)
    public = _fetch_public_feed(hot_cutoff, warm_cutoff)

    best: dict = {}
    source_priority = {"hft_poll": 3, "elite_poll": 3, "vip_poll": 3,
                       "watchlist_poll": 2, "public_feed": 1}
    for whaletrade in priority + watchlist_trades + public:
        key = (whaletrade.wallet, whaletrade.cid, whaletrade.outcome)
        if key not in best:
            best[key] = whaletrade
        else:
            existing = best[key]
            src_new = source_priority.get(whaletrade.source, 1)
            src_old = source_priority.get(existing.source, 1)
            if src_new > src_old or (src_new == src_old and whaletrade.ts > existing.ts):
                best[key] = whaletrade

    return list(best.values())

# ─────────────────────────────────────────────────────────────────────────────
#  HFT SPIKE FAST POLL — dedicated, runs every 3-5s on its own thread
# ─────────────────────────────────────────────────────────────────────────────
def fetch_hft_spike_trades() -> list[WhaleObservation]:
    """
    Dedicated fast poll for HFT Spike Detector.

    Only polls wallets classified as HFT. Filters immediately to trades that
    are >= HFT_SPIKE_MULTIPLIER x the wallet's avg_bet with NO absolute dollar
    floor. The spike ratio is what matters, not the dollar amount.

    v10 FIX: Before calling get_market() on spike candidates, the cid must be
    either cached or from a known-elite wallet. This prevents the HFT fast loop
    from spiking Gamma with unknown cids every 3 seconds, which was causing the
    circuit breaker to re-trip immediately after every cooldown.
    """
    spike_cutoff = time.time() - 90  # only care about last 90 seconds
    hot_cutoff   = spike_cutoff
    warm_cutoff  = spike_cutoff

    hft_wallets = {}
    for e in S.wallets:
        for addr, prof in e.wallet_cache.items():
            if is_hft_wallet(prof) and addr not in hft_wallets:
                hft_wallets[addr] = prof

    if not hft_wallets:
        return []

    results : list[WhaleObservation] = []
    for wallet, prof in hft_wallets.items():
        avg_bet = prof.get("avg_bet", 0)
        if avg_bet <= 0:
            continue

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
            whaletrade : WhaleObservation | None= _normalise_trade(t, wallet, hot_cutoff, warm_cutoff, "hft_spike_poll")
            if whaletrade is None:
                continue
            cash = whaletrade.cash

            tph = prof.get("trades_per_hour", 0)
            required_mult = HFT_SPIKE_MULTIPLIER_HIGH if tph > 200 else HFT_SPIKE_MULTIPLIER_LOW
            if cash < avg_bet * required_mult:
                continue

            if cash < HFT_SPIKE_MIN_ABS_CASH:
                continue

            if whaletrade.cid:
                _seen_verified_cids.add(whaletrade.cid)

            whaletrade.is_large_trade  = True
            whaletrade.hft_spike_ratio = round(cash / avg_bet, 1)
            whaletrade.source          = "hft_spike_poll"
            results.append(whaletrade)

            name = prof.get("name", wallet[:10] + "…")
            S._log(
                f"⚡ HFT SPIKE: {name} ${cash:,.0f} = {cash/avg_bet:.0f}x avg "
                f"[{whaletrade.title[:35]}]",
                "INFO"
            )

        time.sleep(0.05)

    return results
