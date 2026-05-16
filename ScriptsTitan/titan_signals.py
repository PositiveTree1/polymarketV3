"""
TITAN — Signal building. v10 MULTI-STRATEGY OVERHAUL:

KEY CHANGES FROM v9:
  1. MULTI-STRATEGY DISPATCHER: build_signals() now dispatches to three
     sub-builders: _build_recent_form_signals(), _build_drift_discount_signals(),
     and _build_consensus_basket_signals(). Each produces signals with a
     'strategy' tag. Duplicate (cid, outcome) pairs are deduped keeping the
     highest-scoring version but merging strategy tags.

  2. WALLET QUALITY GATES MOVED BEFORE get_market(): The single biggest source
     of 422 spam. Previously: group trades → call get_market() → filter wallets.
     Now: filter wallets first → only call get_market() for survivors.
     Estimated 70-80% reduction in Gamma calls.

  3. RECENT FORM COPY STRATEGY: Follows wallets with positive recent_pnl_30d/7d.
     Lower score threshold (42), longer age window (45 min), HFT bots excluded.

  4. DRIFT DISCOUNT STRATEGY: Enters when current price is 4-12% below whale's
     average entry. Checks whale is still holding via fetch_wallet_sells().
     6-hour signal age window — the discount opportunity persists longer.

  5. CONSENSUS BASKET STRATEGY: Relaxed version of conviction_only.
     Single elite allowed, smaller bets ($1.20 cap), soft -35% stop loss.

  6. STRATEGY-AWARE STOP LOSS: Each signal carries stop_loss_pct from its
     strategy config. None = no stop loss, -0.35 = soft stop for consensus_basket.

ARCHITECTURE:
  Each (conditionId, outcome) pair is ONE binary market position.
  A conditionId has exactly two outcomes: token 0 (yes_price) and token 1 (no_price).
  The `asset` field on a trade IS the token ID — map it to price via asset_to_price.
"""

import time
import re
import math
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from typing import TypedDict, cast
import titan_state as S
import titan_config as C
from titan_config import *
from titan_market import get_market, get_outcome_price, get_outcome_price_by_trade, fetch_wallet_sells, mark_cid_verified, WhaleObservation, Market
from titan_wallet import is_hft_wallet, get_whale_weekly_pnl, is_recent_form_qualified


@dataclass
class Signal:
    # ── market identity ───────────────────────────────────────────────────────
    cid:            str
    asset:          str
    outcome:        str
    title:          str
    slug:           str
    event_slug:     str
    mkt_type:       str
    is_sports:      bool

    # ── strategy ─────────────────────────────────────────────────────────────
    strategy:       str          # "recent_form" | "drift_discount" | "consensus_basket"
    stop_loss_pct:  float | None

    # ── whale observations ────────────────────────────────────────────────────
    ver:            dict[str, WhaleObservation]
    elite_ver:      dict[str, WhaleObservation]
    n_ver:          int
    n_elite:        int
    n_confluence:   int
    n_total:        int

    # ── price & flow ─────────────────────────────────────────────────────────
    avg_entry:      float
    cur:            float
    drift:          float
    slippage:       float
    total_flow:     float
    ver_flow:       float
    max_bet_cash:   float
    opposing_flow:  float

    # ── timing ────────────────────────────────────────────────────────────────
    newest_ts:      float
    oldest_ts:      float
    first_seen_ts:  float
    age_h:          float
    window:         str          # "hot" | "warm"

    mkt:            Market

    # ── quality flags ─────────────────────────────────────────────────────────
    avg_wscore:         float
    is_hft:             bool
    has_large_trade:    bool
    conviction_detail:  str
    elite_only_mode:    bool
    sports_conviction_mult: float
    exits_detected:     list
    exits_same_side:    list

    # ── scoring (set after construction) ──────────────────────────────────────
    score:  float = 0.0
    bd:     dict  = field(default_factory=dict)
    tier:   str   = ""
    bet:    float = 0.0
    names:  list  = field(default_factory=list)

    # ── strategy-specific extras ───────────────────────────────────────────────
    source_recent_wr:   float | None = None
    drift_discount_pct: float | None = None
    price_history:      list[tuple[float, float]] = field(default_factory=list)

    @property
    def age_min(self) -> float:
        return self.age_h * 60

    @property
    def max_bet(self) -> float:
        return self.max_bet_cash

    @property
    def is_conviction(self) -> bool:
        return self.has_large_trade

    def to_dict(self) -> "SignalDict":
        payload = asdict(self)
        payload.pop("wallets", None)
        return cast("SignalDict", payload)

    def get_prices(self) -> list[tuple[float, float]]:
        self.load_prices()
        return self.price_history

    def load_prices(self) -> None:
        from titan_prices import PRICES
        points, _, _ = PRICES.get_prices(self.asset)
        self.price_history = points
        return


class _SignalDictRequired(TypedDict):
    cid:                    str
    asset:                  str
    outcome:                str
    title:                  str
    slug:                   str
    event_slug:             str
    mkt_type:               str
    is_sports:              bool
    strategy:               str
    stop_loss_pct:          float | None
    ver:                    dict
    elite_ver:              dict
    n_ver:                  int
    n_elite:                int
    n_confluence:           int
    n_total:                int
    avg_entry:              float
    cur:                    float
    drift:                  float
    slippage:               float
    total_flow:             float
    ver_flow:               float
    max_bet_cash:           float
    opposing_flow:          float
    newest_ts:              float
    oldest_ts:              float
    first_seen_ts:          float
    age_h:                  float
    window:                 str
    mkt:                    dict
    avg_wscore:             float
    is_hft:                 bool
    has_large_trade:        bool
    conviction_detail:      str
    elite_only_mode:        bool
    sports_conviction_mult: float
    exits_detected:         list
    exits_same_side:        list
    score:                  float
    bd:                     dict
    tier:                   str
    bet:                    float
    names:                  list
    source_recent_wr:       float | None
    drift_discount_pct:     float | None
    price_history:          list[tuple[float, float]]


class SignalDict(_SignalDictRequired, total=False):
    snapshot_ts:            float


def get_signal_prices(signal: SignalDict) -> list[tuple[float, float]]:
    load_signal_prices(signal)
    return list(signal.get("price_history") or [])


def load_signal_prices(signal: SignalDict) -> None:
    if signal.get("price_history"):
        return
    from titan_prices import PRICES
    asset_id = str(signal.get("asset") or "")
    if not asset_id:
        return
    points, _, _ = PRICES.get_prices(asset_id)
    if points:
        signal["price_history"] = points
    return


def load_signal_prices_many(signals: list[SignalDict]) -> list[SignalDict]:
    for signal in signals:
        load_signal_prices(signal)
    return signals


def _hft_spike_ratio_value(trade: WhaleObservation) -> float:
    ratio = trade.hft_spike_ratio
    if ratio is None:
        return 0.0
    return ratio


# ─────────────────────────────────────────────────────────────────────────────
#  MARKET TYPE CLASSIFICATION
# ─────────────────────────────────────────────────────────────────────────────
_SPORTS_PATTERNS = re.compile(
    r'\b(vs\.?|spread|o/u|over|under|winner|set \d|game \d|bo[13]|'
    r'nhl|nba|mlb|nfl|ufc|atp|wta|epl|sea-|nhl-|mlb-|nba-|ufc-|atp-|'
    r'kings|flames|rays|yankees|royals|tigers|brewers|pirates|nationals|'
    r'lol:|valorant:|counter-strike:|'
    r'win on \d{4}-\d{2}-\d{2}|win on 20\d\d|'
    r'leading at halftime|clean sheet|both teams|'
    r'indian premier league|ipl:|champions league [a-z]|'
    r'esports|furia|loud game|map \d|round \d)\b',
    re.IGNORECASE
)
_CRYPTO_PATTERNS = re.compile(
    r'\b(bitcoin|btc|ethereum|eth|solana|xrp|bnb|crypto|up or down)\b',
    re.IGNORECASE
)

def classify_market(title: str, event_slug: str, hrs_left: float | None = None) -> str:
    """
    Classify a market into SPORTS, CRYPTO, POLITICS, or EVENT.
    Sports markets require stricter entry criteria.
    """
    combined = f"{title} {event_slug}"
    if _SPORTS_PATTERNS.search(combined):
        return "SPORTS"
    if _CRYPTO_PATTERNS.search(combined):
        return "CRYPTO"
    if hrs_left is not None and hrs_left < 24:
        return "EVENT"
    return "POLITICS"


# ─────────────────────────────────────────────────────────────────────────────
#  PRE-ENTRY EXPECTED VALUE
# ─────────────────────────────────────────────────────────────────────────────
def estimate_expected_value(cur_price: float, avg_entry: float, liq: float,
                           bet_size: float, market_type: str, avg_wscore: float = 0.85) -> dict:
    """
    Estimate expected value of entering a trade.
    Returns dict with ev_dollar, ev_pct, spread_cost, impact_cost.
    Only enter if ev_dollar > 0.
    """
    if liq > 100_000:
        spread_pct = 0.005
    elif liq > 20_000:
        spread_pct = 0.015
    else:
        spread_pct = 0.03

    if liq > 0:
        scale = 0.3 if bet_size < 10 else (0.5 if bet_size < 50 else 1.0)
        impact_pct = min(0.03, (bet_size / liq) * scale)
    else:
        impact_pct = 0.02

    total_friction = spread_pct + impact_pct + ROUND_TRIP_FEE

    score_discount = max(0.0, (0.85 - avg_wscore) * 0.15) if avg_wscore > 0 else 0.05
    fair_prob = max(0.05, min(0.97, avg_entry - score_discount))
    ev_per_dollar = (fair_prob / cur_price) - 1.0
    ev_after_friction = ev_per_dollar - total_friction
    ev_dollar = ev_after_friction * bet_size

    return {
        "ev_dollar": round(ev_dollar, 4),
        "ev_pct": round(ev_after_friction * 100, 2),
        "spread_cost": round(spread_pct * 100, 2),
        "impact_cost": round(impact_pct * 100, 2),
        "total_friction": round(total_friction * 100, 2),
        "fair_prob": round(fair_prob * 100, 1),
        "tradeable": ev_after_friction > 0.0,
    }


# ─────────────────────────────────────────────────────────────────────────────
#  HEDGE BOT TRACKING (persistent across cycles)
# ─────────────────────────────────────────────────────────────────────────────
_KNOWN_HEDGE_WALLETS: set = set()


def get_known_hedge_wallets() -> set:
    return _KNOWN_HEDGE_WALLETS


def restore_known_hedge_wallets(wallets_iter):
    _KNOWN_HEDGE_WALLETS.update(str(w).lower() for w in wallets_iter)


# ─────────────────────────────────────────────────────────────────────────────
#  EXIT MONITORING
# ─────────────────────────────────────────────────────────────────────────────
def check_whale_exits(cid_to_wallet_sets: dict, entry_times: dict | None = None) -> dict:
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

    cid_wallet_buy_cash: dict = {}
    for key, pos in S.env().open_positions.items():
        cid = pos.cid or key[0]
        if pos.whale_buy_cash:
            cid_wallet_buy_cash[cid] = pos.whale_buy_cash
        else:
            for w in pos.elite_wallets:
                w_lower = w.lower()
                if cid not in cid_wallet_buy_cash:
                    cid_wallet_buy_cash[cid] = {}
                cid_wallet_buy_cash[cid][w_lower] = pos.bet

    asset_to_cid: dict = {}
    for key, pos in S.env().open_positions.items():
        cid = pos.cid or key[0]
        if pos.asset:
            asset_to_cid[pos.asset] = cid

    for wallet in all_wallets:
        sells = fetch_wallet_sells(wallet, global_cutoff, limit=200)
        if not sells:
            time.sleep(0.1)
            continue

        for sell in sells:
            ts    = sell.ts
            cid   = sell.cid
            asset = sell.asset
            sell_cash = sell.cash

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
                continue

            buy_cash = cid_wallet_buy_cash.get(cid, {}).get(wallet.lower(), 0)
            if buy_cash > 0 and sell_cash > 0:
                sell_fraction = sell_cash / buy_cash
                _min_frac = float(getattr(C, "position_management_ext", {}).get("whale_exit_min_sell_fraction", 0.30))
                if sell_fraction < _min_frac:
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
        if s.cid == cid or s.asset == cid:
            return False
    return True


# ─────────────────────────────────────────────────────────────────────────────
#  SCORING
# ─────────────────────────────────────────────────────────────────────────────
def score_signal(s: "Signal") -> dict:
    _sc = getattr(C, "strategy_scoring", {})
    bd = {}

    bd["wallet"] = round(s.avg_wscore * _sc.get("wallet_max_pts", 30), 1)

    _conf_pts = _sc.get("confluence_pts", [0, 6, 10, 14, 18])
    bd["conf"] = _conf_pts[min(s.n_confluence, len(_conf_pts) - 1)]
    if s.elite_only_mode:
        bd["conf"] = max(bd["conf"], _sc.get("elite_only_conf_floor", 8))
    if s.has_large_trade:
        bd["conf"] = min(_conf_pts[-1], bd["conf"] + _sc.get("large_trade_conf_bonus", 4))

    age_h  = (time.time() - s.newest_ts) / 3600
    if s.window in ("hot", "hft"):
        _hot = _sc.get("recency_hot_thresholds", [[0.25,20],[0.5,17],[1,14],[2,9],[3,6],[4,4],[None,2]])
        bd["rec"] = next(pts for (thr, pts) in _hot if thr is None or age_h < thr)
    else:
        _warm = _sc.get("recency_warm_thresholds", [[4,6],[6,4],[8,2],[None,1]])
        bd["rec"] = next(pts for (thr, pts) in _warm if thr is None or age_h < thr)

    if s.is_hft and (time.time() - s.newest_ts) <= HFT_MIRROR_DELAY_MAX_SECONDS:
        bd["rec"] = min(_sc.get("hft_recency_cap", 20), bd["rec"] + _sc.get("hft_recency_bonus", 5))

    drift = s.drift
    if drift < 0:
        _base = _sc.get("opp_neg_drift_base", 8)
        _scale = _sc.get("opp_neg_drift_scale", 25)
        _max = _sc.get("opp_neg_drift_max", 15)
        bd["opp"] = max(0, min(_max, _base + int(abs(drift) * _scale)))
    else:
        _pos = _sc.get("opp_pos_drift_thresholds", [[0.04,15],[0.08,12],[0.12,8],[0.15,4],[None,0]])
        bd["opp"] = next(pts for (thr, pts) in _pos if thr is None or drift < thr)

    mkt   = s.mkt
    liq_p = min(_sc.get("liq_quality_max", 5.0), mkt["liq"] / _sc.get("liq_quality_scale", 8000) * _sc.get("liq_quality_max", 5.0))
    vol_p = min(_sc.get("vol_quality_max", 3.0), mkt["volume"] / _sc.get("vol_quality_scale", 40000) * _sc.get("vol_quality_max", 3.0))
    hrs = mkt.hrs_left
    _tq_pts = _sc.get("time_quality_pts", [2, 1, 0])
    _tq_thr = _sc.get("time_quality_thresholds", [72, 24])
    t_p = _tq_pts[0] if (hrs is None or hrs > _tq_thr[0]) else _tq_pts[1] if hrs > _tq_thr[1] else _tq_pts[2]
    bd["mkt"] = round(liq_p + vol_p + t_p, 1)

    bd["bonus"] = (_sc.get("conviction_bonus_massive", 5) if s.max_bet_cash >= MASSIVE_TRADE
                   else _sc.get("conviction_bonus_large", 2) if s.max_bet_cash >= LARGE_TRADE else 0)
    if s.has_large_trade:
        bd["bonus"] = min(10, bd["bonus"] + _sc.get("large_trade_bonus", 3))

    if C.IDEAL_PRICE_MIN <= s.cur <= C.IDEAL_PRICE_MAX:
        bd["price_zone"] = _sc.get("price_zone_ideal_bonus", 5)
    elif C.MIN_ENTRY_PRICE <= s.cur <= C.MAX_ENTRY_PRICE:
        bd["price_zone"] = _sc.get("price_zone_acceptable_bonus", 2)
    else:
        bd["price_zone"] = _sc.get("price_zone_outside_penalty", -10)

    n_el = s.n_elite
    _mw = _sc.get("multi_whale_pts", [0, 0, 5, 8])
    bd["multi_whale"] = _mw[min(n_el, len(_mw) - 1)]

    bd["exit_penalty"] = len(s.exits_same_side) * _sc.get("exit_penalty_per_whale", -8)

    weekly_pnl_total = sum(
        get_whale_weekly_pnl(w)
        for w in s.elite_ver.keys()
    )
    if weekly_pnl_total < -1.0:
        bd["weekly_penalty"] = max(_sc.get("weekly_pnl_penalty_min", -10), round(weekly_pnl_total, 0))
    else:
        bd["weekly_penalty"] = 0

    bd["total"] = round(min(100, max(0, sum(v for k, v in bd.items() if k != "total"))), 1)
    return bd


# ─────────────────────────────────────────────────────────────────────────────
#  BET SIZING
# ─────────────────────────────────────────────────────────────────────────────
def _adaptive_bet_caps():
    br = S.env().paper_bankroll
    _caps = getattr(C, "strategy_kelly", {}).get("adaptive_caps", [])
    for threshold, max_abs, max_pct in _caps:
        if br < threshold:
            return max_abs, max_pct
    return MAX_BET_ABS, MAX_BET_PCT


def kelly_bet(signal: Signal, wallets: dict, score: float | None = None) -> float:
    """
    v10 KELLY BET SIZING — strategy-aware.
    Applies strategy-specific caps from titan_config strategy blocks.
    """
    cur        = signal.cur
    fair_value = signal.avg_entry
    score      = score if score is not None else signal.score
    tier       = signal.tier if signal.tier else "MEDIUM"
    is_large   = signal.has_large_trade
    avg_wscore = signal.avg_wscore
    n_elite    = signal.n_elite
    strategy   = signal.strategy

    fair_value = max(0.05, min(0.95, fair_value))
    cur        = max(0.05, min(0.95, cur))

    _kc = getattr(C, "strategy_kelly", {})
    _sc = getattr(C, "strategy_scoring", {})
    _wscore_ref  = _sc.get("score_discount_wscore_ref", 0.85)
    _disc_factor = _sc.get("score_discount_factor", 0.15)
    score_discount = max(0.0, (_wscore_ref - avg_wscore) * _disc_factor)
    kelly_fair = max(0.05, min(0.95, fair_value - score_discount))

    b     = max(0.05, (1.0 / cur) - 1.0 - ROUND_TRIP_FEE)
    kelly = max(0.0, kelly_fair - (1 - kelly_fair) / b)
    f_kelly = kelly * KELLY_FRACTION

    _smult_base  = _kc.get("score_mult_base", 0.5)
    _smult_scale = _kc.get("score_mult_scale", 0.5)
    smult = _smult_base + _smult_scale * (score / 100)
    _conf_step = _kc.get("conf_mult_step", 0.25)
    _conf_cap  = _kc.get("conf_mult_cap", 1.75)
    conf_mult = min(_conf_cap, 1.0 + (n_elite - 1) * _conf_step)

    _tier_defaults = {"CONVICTION": 1.6, "ALERT": 1.2, "STRONG": 1.0, "MEDIUM": 0.7, "ELITE_ONLY": 0.9, "HFT": 0.5}
    _tier_cfg = _kc.get("tier_multipliers", _tier_defaults)
    tier_mult = _tier_cfg.get(tier, 0.8)

    # v10: Strategy-specific bet multiplier
    strategy_mult = _strategy_bet_multiplier(signal)

    kelly_bet_raw = S.env().paper_bankroll * f_kelly * smult * conf_mult * tier_mult * strategy_mult

    _sf_ref   = _kc.get("score_floor_base_ref", 55)
    _sf_scale = _kc.get("score_floor_scale", 45)
    _sf_mult  = _kc.get("score_floor_mult", 1.5)
    score_floor = MIN_BET * (1 + max(0, (score - _sf_ref) / _sf_scale) * _sf_mult)

    br = S.env().paper_bankroll
    max_abs, max_pct = _adaptive_bet_caps()

    # v10: Strategy-specific absolute cap
    if "consensus_basket" in strategy:
        cb_cfg = getattr(C, "strategy_consensus_basket", {})
        max_abs = min(max_abs, float(cb_cfg.get("max_bet_abs", 1.20)))

    _lt_boost = _kc.get("large_trade_max_abs_boost", 1.5)
    _lt_br_cap = _kc.get("large_trade_bankroll_cap", 0.22)
    if is_large and not signal.is_hft:
        max_abs = min(max_abs * _lt_boost, br * _lt_br_cap)

    bet = max(score_floor, kelly_bet_raw)
    return round(min(max_abs, br * max_pct, max(MIN_BET, bet)), 2)


def _strategy_bet_multiplier(sig: Signal) -> float:
    """
    v10: Compute a strategy-specific bet size multiplier.
    recent_form: scale by recent win rate of the sourcing whale.
    drift_discount: scale by discount percentage.
    open_book: scale by consensus count.
    consensus_basket: no multiplier (volume strategy, flat sizing).
    """
    strategy = sig.strategy
    if "recent_form" in strategy:
        wr = sig.source_recent_wr or 0.55
        return max(0.8, min(1.6, (wr - 0.50) * 4 + 0.8))
    elif "drift_discount" in strategy:
        discount = sig.drift_discount_pct or 0.0
        return max(0.9, min(1.5, 1.0 + discount * 5))
    elif "open_book" in strategy:
        return max(1.0, min(1.8, 0.8 + sig.n_confluence * 0.2))
    else:
        return 1.0


# ─────────────────────────────────────────────────────────────────────────────
#  SHARED SIGNAL CONSTRUCTION HELPERS
# ─────────────────────────────────────────────────────────────────────────────
_EMPTY_W = {"score": 0.10, "verified": False, "watchable": False, "elite": False, "hft": False}

def _get_market_for_signal(cid, trade_title, asset_hint, slug_hint, from_verified=True):
    """Call get_market with verified flag so cid is registered."""
    mark_cid_verified(cid)
    return get_market(cid, trade_title, asset=asset_hint, slug=slug_hint, from_verified=from_verified)


def _build_names(elite_wallets, verified_wallets, all_ver, is_hft_signal, has_large_trade):
    """Build display names for a signal."""
    elite_names = []
    for w in list(elite_wallets.keys())[:3]:
        name = (S.env().wallet_cache.get(w, {}).get("name") or
                all_ver.get(w, {}).get("name") or
                w[:10] + "…")
        elite_names.append(name)

    conf_names = []
    for w in list(verified_wallets.keys())[:2]:
        name = (S.env().wallet_cache.get(w, {}).get("name") or
                all_ver.get(w, {}).get("name") or
                w[:10] + "…")
        conf_names.append(name)

    names = elite_names + ([f"+{len(conf_names)}conf"] if conf_names else [])
    if is_hft_signal:
        names = ["⚡" + n for n in names]
    if has_large_trade:
        names = ["💎" + n if not n.startswith("💎") else n for n in names[:1]] + names[1:]
    return names


def _check_price_zone(cur: float, price_min: float, price_max: float,
                      outcome: str, title: str, rejects: list) -> bool:
    """Return True if price passes zone gate, False and append reject if not."""
    if cur < price_min:
        rejects.append(
            f"  {outcome:<12} {title[:40]}\n"
            f"    ↳ Price {cur:.3f} < {price_min:.2f} floor — too speculative"
        )
        return False
    if cur > price_max:
        rejects.append(
            f"  {outcome:<12} {title[:40]}\n"
            f"    ↳ Price {cur:.3f} > {price_max:.2f} ceiling — near-certainty trap"
        )
        return False
    return True


# ─────────────────────────────────────────────────────────────────────────────
#  STRATEGY 1: RECENT FORM COPY
# ─────────────────────────────────────────────────────────────────────────────
def _build_recent_form_signals(raw_trades: list, wallets: dict,
                                whale_exits: dict) -> tuple:
    """
    Build signals from wallets with positive recent form (last 30 days profitable).

    Key differences from consensus_basket:
    - Wallet gate: is_recent_form_qualified() replaces elite-only check
    - Any verified wallet with good recent form qualifies (not just elite)
    - Lower score threshold (42), longer age window (45 min)
    - No stop loss (price ceiling is protection)
    - Bet scaled by source whale's recent win rate
    """
    cfg = getattr(C, "strategy_recent_form", {})
    if not cfg.get("enabled", True):
        return [], []

    max_tph         = float(cfg.get("max_tph", 20))
    min_pnl_30d     = float(cfg.get("min_pnl_30d", 0))
    min_pnl_7d      = float(cfg.get("min_pnl_7d", -50))
    max_age_h       = float(cfg.get("max_signal_age_h", 0.75))
    min_score       = float(cfg.get("min_score", 42))
    price_min       = float(cfg.get("price_min", 0.18))
    price_max       = float(cfg.get("price_max", 0.78))
    stop_loss_pct   = cfg.get("stop_loss_pct", None)

    now_t    = time.time()
    signals  = []
    rejects  = []

    # Pre-filter: only keep trades from recent-form qualified wallets
    qualified_trades = []
    for t in raw_trades:
        w = t.wallet
        prof = wallets.get(w, _EMPTY_W)
        if not prof.get("verified") and not prof.get("elite"):
            continue
        if not is_recent_form_qualified(prof, min_pnl_30d, min_pnl_7d, max_tph):
            continue
        qualified_trades.append(t)

    if not qualified_trades:
        return [], []

    cid_groups = defaultdict(list)
    for t in qualified_trades:
        cid_groups[(t.cid, t.outcome)].append(t)

    for (cid, outcome), group in cid_groups.items():
        by_w: dict = {}
        for t in group:
            w = t.wallet
            if w not in by_w or t.ts > by_w[w].ts:
                by_w[w] = t

        title = next(
            (t.title for t in sorted(by_w.values(), key=lambda x: x.ts, reverse=True)
             if t.title),
            next(iter(by_w.values())).title
        )

        # Classify wallets — recent form strategy: elite OR verified with good recent form
        rf_qualified = {}
        for w, t in by_w.items():
            prof = wallets.get(w, _EMPTY_W)
            if is_recent_form_qualified(prof, min_pnl_30d, min_pnl_7d, max_tph):
                rf_qualified[w] = t

        if not rf_qualified:
            rejects.append(
                f"  {outcome:<12} {title[:40]}\n"
                f"    ↳ [RF] No recent-form qualified wallets"
            )
            continue

        # Hedge check
        if all(w in _KNOWN_HEDGE_WALLETS for w in rf_qualified):
            rejects.append(
                f"  {outcome:<12} {title[:40]}\n"
                f"    ↳ [RF] All wallets are known hedge bots"
            )
            continue

        # Age gate — use newest qualified wallet's trade time
        newest_ts = max(t.ts for t in rf_qualified.values())
        oldest_ts = min(t.ts for t in rf_qualified.values())
        age_h = (now_t - newest_ts) / 3600
        if age_h > max_age_h:
            rejects.append(
                f"  {outcome:<12} {title[:40]}\n"
                f"    ↳ [RF] Stale: {age_h:.1f}h > {max_age_h}h"
            )
            continue

        # Market data — only call Gamma since these wallets are verified
        asset_hint = next((t.asset for t in rf_qualified.values() if t.asset), "")
        slug_hint  = next((t.slug for t in rf_qualified.values() if t.slug), "")

        mkt, mkt_fail = _get_market_for_signal(cid, title, asset_hint, slug_hint)
        if not mkt:
            rejects.append(f"  {outcome:<12} {title[:40]}\n    ↳ [RF] Market: {mkt_fail}")
            continue

        cur = get_outcome_price(mkt, outcome, asset=asset_hint)

        if not _check_price_zone(cur, price_min, price_max, outcome, title, rejects):
            continue

        # Market type classification
        event_slug = next((t.event_slug for t in group if t.event_slug), "")
        if not event_slug:
            event_slug = mkt.event_slug
        mkt_type = classify_market(title, event_slug, mkt.hrs_left)

        # Avg entry from recent-form wallets
        entries = [(t.price, t.cash) for t in rf_qualified.values()]
        total_w = sum(c for _, c in entries)
        if total_w == 0:
            continue
        avg_entry = sum(p * w for p, w in entries) / total_w

        drift    = (cur - avg_entry) / max(avg_entry, 0.01)
        slippage = drift

        # Slippage gate (looser for RF — we're not trying to time the exact entry)
        if slippage > MAX_ENTRY_SLIPPAGE * 2:
            rejects.append(
                f"  {outcome:<12} {title[:40]}\n"
                f"    ↳ [RF] Slippage +{slippage*100:.1f}% > {MAX_ENTRY_SLIPPAGE*200:.0f}% RF max"
            )
            continue

        # EV sanity check
        fair_prob = max(0.02, min(0.97, avg_entry))
        potential_win = (1.0 / max(cur, 0.01)) - 1.0
        raw_ev = fair_prob * potential_win - (1.0 - fair_prob)
        if raw_ev < 0.005:
            rejects.append(
                f"  {outcome:<12} {title[:40]}\n"
                f"    ↳ [RF] EV too low: {raw_ev*100:+.1f}%"
            )
            continue

        # Compute source recent win rate for bet sizing
        # Use average recent_pnl_30d across qualified wallets as proxy
        avg_recent_wr = 0.55  # default
        rf_pnl_vals = [wallets.get(w, {}).get("recent_pnl_30d", 0) or 0 for w in rf_qualified]
        if rf_pnl_vals:
            # Map pnl to approximate win rate: pnl > 0 → above 0.55, scaled
            avg_pnl = sum(rf_pnl_vals) / len(rf_pnl_vals)
            avg_recent_wr = max(0.50, min(0.75, 0.55 + avg_pnl / 10000))

        # Classify as elite or verified for display purposes
        elite_wallets_rf = {w: t for w, t in rf_qualified.items()
                            if wallets.get(w, _EMPTY_W).get("elite")}
        verified_wallets_rf = {w: t for w, t in rf_qualified.items()
                               if wallets.get(w, _EMPTY_W).get("verified") and w not in elite_wallets_rf}
        all_ver = {**elite_wallets_rf, **verified_wallets_rf}

        # Use elite wallets for scoring if available, else all rf_qualified
        scoring_elites = elite_wallets_rf if elite_wallets_rf else rf_qualified
        avg_wscore = sum(wallets.get(w, _EMPTY_W).get("score", 0.10) for w in scoring_elites) / len(scoring_elites)

        exits_here = whale_exits.get(cid, [])
        exits_same_side = list(set(exits_here) & set(all_ver.keys()))

        total_flow = sum(t.cash for t in by_w.values())
        max_bet_cash = max(t.cash for t in rf_qualified.values())
        window = "hot" if any(t.window == "hot" for t in rf_qualified.values()) else "warm"

        sig = Signal(
            cid=cid, asset=asset_hint, outcome=outcome, title=title,
            slug=mkt.slug or slug_hint, event_slug=event_slug, mkt_type=mkt_type, is_sports=(mkt_type == "SPORTS"),
            strategy="recent_form", stop_loss_pct=stop_loss_pct,
            ver=all_ver, elite_ver=elite_wallets_rf,
            n_ver=len(all_ver), n_elite=len(elite_wallets_rf),
            n_confluence=len(all_ver), n_total=len(by_w),
            avg_entry=avg_entry, cur=cur, drift=drift, slippage=slippage,
            total_flow=total_flow, ver_flow=total_flow,
            max_bet_cash=max_bet_cash, opposing_flow=0.0,
            newest_ts=newest_ts, oldest_ts=oldest_ts, first_seen_ts=oldest_ts, age_h=age_h, window=window,
            avg_wscore=avg_wscore, is_hft=False,
            has_large_trade=max_bet_cash >= LARGE_TRADE, conviction_detail="",
            elite_only_mode=len(verified_wallets_rf) == 0,
            exits_detected=exits_here, exits_same_side=exits_same_side,
            sports_conviction_mult=1.0,
            source_recent_wr=avg_recent_wr,
            mkt=mkt,
        )

        bd    = score_signal(sig)
        total = bd["total"]

        if total < min_score:
            rejects.append(
                f"  {outcome:<12} {title[:40]}\n"
                f"    ↳ [RF] Score {total:.0f} < {min_score} "
                f"[W:{bd.get('wallet',0):.0f} C:{bd.get('conf',0)} R:{bd.get('rec',0)}]"
            )
            continue

        tier = "ALERT" if total >= ALERT_SCORE else "STRONG" if total >= STRONG_SCORE else "MEDIUM"
        bet  = kelly_bet(sig, wallets, score=total)

        names = _build_names(elite_wallets_rf, verified_wallets_rf, all_ver, False, sig.has_large_trade)
        sig.score = total; sig.bd = bd; sig.tier = tier; sig.bet = bet; sig.names = names
        signals.append(sig)

    return signals, rejects


# ─────────────────────────────────────────────────────────────────────────────
#  STRATEGY 2: DRIFT DISCOUNT
# ─────────────────────────────────────────────────────────────────────────────
def _build_drift_discount_signals(raw_trades: list, wallets: dict,
                                   whale_exits: dict) -> tuple:
    """
    Build signals where current price is 4-12% below verified whale's entry.

    The whale bought earlier, price dipped, they're still holding.
    We get the same bet at a discount. The whale's thesis hasn't changed.

    Key parameters:
    - Signal age up to 6 hours (much longer than conviction_only's 15 min)
    - Drift must be negative (price below whale entry)
    - Magnitude: 4-12% below entry (4% = real discount, >12% = market disagrees)
    - Optional: check whale is still holding via fetch_wallet_sells()
    """
    cfg = getattr(C, "strategy_drift_discount", {})
    if not cfg.get("enabled", True):
        return [], []

    min_discount    = float(cfg.get("min_discount_pct", 0.04))
    max_discount    = float(cfg.get("max_discount_pct", 0.12))
    max_age_h       = float(cfg.get("max_signal_age_h", 6.0))
    price_min       = float(cfg.get("price_min", 0.20))
    price_max       = float(cfg.get("price_max", 0.72))
    check_holding   = bool(cfg.get("require_still_holding_check", True))
    stop_loss_pct   = cfg.get("stop_loss_pct", None)

    now_t   = time.time()
    signals = []
    rejects = []

    # Pre-filter: only verified/elite wallets
    verified_trades = [
        t for t in raw_trades
        if wallets.get(t.wallet, _EMPTY_W).get("verified") or
           wallets.get(t.wallet, _EMPTY_W).get("elite")
    ]

    if not verified_trades:
        return [], []

    cid_groups = defaultdict(list)
    for t in verified_trades:
        cid_groups[(t.cid, t.outcome)].append(t)

    for (cid, outcome), group in cid_groups.items():
        by_w: dict = {}
        for t in group:
            w = t.wallet
            if w not in by_w or t.ts > by_w[w].ts:
                by_w[w] = t

        title = next(
            (t.title for t in sorted(by_w.values(), key=lambda x: x.ts, reverse=True)
             if t.title),
            next(iter(by_w.values())).title
        )

        elite_wallets = {w: t for w, t in by_w.items() if wallets.get(w, _EMPTY_W).get("elite")}
        verified_wallets = {w: t for w, t in by_w.items()
                           if wallets.get(w, _EMPTY_W).get("verified") and w not in elite_wallets}
        all_ver = {**elite_wallets, **verified_wallets}

        if not all_ver:
            continue

        if all(w in _KNOWN_HEDGE_WALLETS for w in all_ver):
            continue

        # Age gate — for drift discount, we look back up to 6h
        newest_ts = max(t.ts for t in all_ver.values())
        oldest_ts = min(t.ts for t in all_ver.values())
        age_h = (now_t - newest_ts) / 3600
        if age_h > max_age_h:
            continue  # Too old — skip silently (would generate too many rejects)

        # Market data
        asset_hint = next((t.asset for t in all_ver.values() if t.asset), "")
        slug_hint  = next((t.slug for t in all_ver.values() if t.slug), "")

        mkt, mkt_fail = _get_market_for_signal(cid, title, asset_hint, slug_hint)
        if not mkt:
            continue  # Skip silently — not a real reject, just Gamma unavailable

        cur = get_outcome_price(mkt, outcome, asset=asset_hint)

        # Compute discount
        entries = [(t.price, t.cash) for t in all_ver.values()]
        total_w = sum(c for _, c in entries)
        if total_w == 0:
            continue
        avg_whale_entry = sum(p * w for p, w in entries) / total_w

        discount = (avg_whale_entry - cur) / max(avg_whale_entry, 0.01)

        # Discount gate — this is the core filter
        if discount < min_discount:
            rejects.append(
                f"  {outcome:<12} {title[:40]}\n"
                f"    ↳ [DD] Discount {discount*100:.1f}% < {min_discount*100:.0f}% min "
                f"(whale @{avg_whale_entry:.3f}, now @{cur:.3f})"
            )
            continue
        if discount > max_discount:
            rejects.append(
                f"  {outcome:<12} {title[:40]}\n"
                f"    ↳ [DD] Discount {discount*100:.1f}% > {max_discount*100:.0f}% max — market disagrees"
            )
            continue

        # Price zone check
        if not _check_price_zone(cur, price_min, price_max, outcome, title, rejects):
            continue

        # "Still holding" check — verify whale hasn't exited since their buy
        if check_holding:
            exited_whales = []
            for w, t in all_ver.items():
                buy_ts = t.ts
                sells = fetch_wallet_sells(w, buy_ts - 60, limit=50)
                for s in sells:
                    if (s.cid == cid or s.asset == asset_hint) and s.ts > buy_ts:
                        exited_whales.append(w)
                        break
                time.sleep(0.05)

            if exited_whales:
                exited_names = [S.env().wallet_cache.get(w, {}).get("name", w[:10]) for w in exited_whales[:2]]
                # If ALL whales exited, reject
                if len(exited_whales) >= len(all_ver):
                    rejects.append(
                        f"  {outcome:<12} {title[:40]}\n"
                        f"    ↳ [DD] All whales exited: {exited_names}"
                    )
                    continue
                # Partial exit — remove exited wallets, continue with remaining
                for w in exited_whales:
                    all_ver.pop(w, None)
                    elite_wallets.pop(w, None)
                    verified_wallets.pop(w, None)
                # Recompute avg entry without exited wallets
                entries = [(t.price, t.cash) for t in all_ver.values()]
                total_w = sum(c for _, c in entries)
                if total_w > 0:
                    avg_whale_entry = sum(p * w for p, w in entries) / total_w
                    discount = (avg_whale_entry - cur) / max(avg_whale_entry, 0.01)

        event_slug = next((t.event_slug for t in group if t.event_slug), "")
        if not event_slug:
            event_slug = mkt.event_slug
        mkt_type = classify_market(title, event_slug, mkt.hrs_left)

        drift = (cur - avg_whale_entry) / max(avg_whale_entry, 0.01)
        avg_wscore = sum(wallets.get(w, _EMPTY_W).get("score", 0.10) for w in all_ver) / max(len(all_ver), 1)

        exits_here = whale_exits.get(cid, [])
        exits_same_side = list(set(exits_here) & set(all_ver.keys()))

        total_flow   = sum(t.cash for t in by_w.values())
        max_bet_cash = max(t.cash for t in all_ver.values())
        window       = "warm"  # DD signals are inherently not "hot"

        # Score: base 60 + discount * 200 + elite count bonus
        n_el = len(elite_wallets)
        drift_score = min(100, 60 + int(discount * 200) + n_el * 8)
        # Adjust for price zone
        if C.IDEAL_PRICE_MIN <= cur <= C.IDEAL_PRICE_MAX:
            drift_score = min(100, drift_score + 5)

        tier = "ALERT" if drift_score >= ALERT_SCORE else "STRONG" if drift_score >= STRONG_SCORE else "MEDIUM"
        if age_h > MAX_SIGNAL_AGE_H:
            tier = "STRONG"  # Don't mark DD as STALE — the whole point is older signals

        sig = Signal(
            cid=cid, asset=asset_hint, outcome=outcome, title=title,
            slug=mkt.slug or slug_hint, event_slug=event_slug, mkt_type=mkt_type, is_sports=(mkt_type == "SPORTS"),
            strategy="drift_discount", stop_loss_pct=stop_loss_pct,
            ver=all_ver, elite_ver=elite_wallets,
            n_ver=len(all_ver), n_elite=len(elite_wallets),
            n_confluence=len(all_ver), n_total=len(by_w),
            avg_entry=avg_whale_entry, cur=cur, drift=drift, slippage=drift,
            total_flow=total_flow, ver_flow=total_flow,
            max_bet_cash=max_bet_cash, opposing_flow=0.0,
            newest_ts=newest_ts, oldest_ts=oldest_ts, first_seen_ts=oldest_ts, age_h=age_h, window=window,
            avg_wscore=avg_wscore, is_hft=False,
            has_large_trade=max_bet_cash >= LARGE_TRADE, conviction_detail="",
            elite_only_mode=len(verified_wallets) == 0,
            exits_detected=exits_here, exits_same_side=exits_same_side,
            sports_conviction_mult=1.0,
            drift_discount_pct=discount,
            mkt=mkt,
        )

        bet = kelly_bet(sig, wallets, score=drift_score)
        names = _build_names(elite_wallets, verified_wallets, all_ver, False, sig.has_large_trade)
        sig.score = drift_score; sig.bd = {"total": drift_score}; sig.tier = tier; sig.bet = bet; sig.names = names
        signals.append(sig)
        S._log(
            f"  📉 DD signal: {title[:35]} [{outcome}] "
            f"whale@{avg_whale_entry:.3f} now@{cur:.3f} discount={discount*100:.1f}%",
            "DIAG"
        )

    return signals, rejects


# ─────────────────────────────────────────────────────────────────────────────
#  STRATEGY 3: CONSENSUS BASKET (near-copy of original conviction_only)
# ─────────────────────────────────────────────────────────────────────────────
def _build_consensus_basket_signals(trades: list, wallets: dict,
                                     whale_exits: dict) -> tuple:
    """
    Consensus basket: relaxed version of the original conviction_only strategy.

    Key differences:
    - MIN_ELITE_CONFLUENCE = 1 (single elite ok)
    - Smaller bet cap ($1.20 abs max)
    - Soft stop loss at -35%
    - Still requires the full drift/EV/age/price-zone gate sequence
    """
    cfg = getattr(C, "strategy_consensus_basket", {})
    if not cfg.get("enabled", True):
        return [], []

    min_confluence = int(cfg.get("min_elite_confluence", 1))
    max_age_h      = float(cfg.get("max_signal_age_h", 0.5))
    price_min      = float(cfg.get("price_min", 0.20))
    price_max      = float(cfg.get("price_max", 0.72))
    min_score_cfg  = float(cfg.get("min_score", 50))
    stop_loss_pct  = cfg.get("stop_loss_pct", -0.35)

    cid_groups = defaultdict(list)
    for t in trades:
        cid_groups[(t.cid, t.outcome)].append(t)

    now_t    = time.time()
    signals  = []
    rejects  = []

    for (cid, outcome), group in cid_groups.items():
        by_w: dict = {}
        for t in group:
            w = t.wallet
            if w not in by_w or t.ts > by_w[w].ts:
                by_w[w] = t

        title = next(
            (t.title for t in sorted(by_w.values(), key=lambda x: x.ts, reverse=True)
             if t.title),
            next(iter(by_w.values())).title
        )

        # ── Classify wallets ──────────────────────────────────────────────────
        elite_wallets    = {w: t for w, t in by_w.items() if wallets.get(w, _EMPTY_W).get("elite")}
        verified_wallets = {w: t for w, t in by_w.items()
                            if wallets.get(w, _EMPTY_W).get("verified") and w not in elite_wallets}

        # Spike promotion (same as original)
        _any_hft_trade_tagged = any(
            t.is_large_trade or _hft_spike_ratio_value(t) > 0
            for t in by_w.values()
        )
        if _any_hft_trade_tagged and not elite_wallets:
            for w, t in by_w.items():
                spike_r  = _hft_spike_ratio_value(t)
                is_large = t.is_large_trade
                if spike_r < 20 and not is_large:
                    continue
                if w in elite_wallets:
                    continue
                prof = wallets.get(w, _EMPTY_W)
                if not (is_hft_wallet(prof) or prof.get("verified")):
                    continue
                elite_wallets[w] = t

        hft_wallets = {w: t for w, t in by_w.items()
                       if (wallets.get(w, _EMPTY_W).get("hft") or
                           is_hft_wallet(wallets.get(w, _EMPTY_W)))
                       and w in {**elite_wallets, **verified_wallets}}
        all_ver = {**elite_wallets, **verified_wallets}
        n_ver   = len(all_ver)

        is_hft_signal = len(hft_wallets) > 0 or _any_hft_trade_tagged

        # Counter-whale check
        opposite_elite_cash = 0.0
        our_elite_cash = sum(t.cash for t in elite_wallets.values()) if elite_wallets else 0.0
        for (other_cid, other_outcome), other_group in cid_groups.items():
            if other_cid == cid and other_outcome != outcome:
                for t in other_group:
                    w = t.wallet
                    if wallets.get(w, _EMPTY_W).get("elite") or wallets.get(w, _EMPTY_W).get("verified"):
                        opposite_elite_cash += t.cash
                break

        _opp_block = float(getattr(C, "strategy_consensus_basket", {}).get("opposition_ratio_block", 0.60))
        if opposite_elite_cash > 0 and our_elite_cash > 0:
            opposition_ratio = opposite_elite_cash / (our_elite_cash + opposite_elite_cash)
            if opposition_ratio > _opp_block:
                rejects.append(
                    f"  {outcome:<12} {title[:40]}\n"
                    f"    ↳ [CB] Counter-whale: {opposition_ratio*100:.0f}% opposing flow"
                )
                continue

        if not elite_wallets:
            rejects.append(
                f"  {outcome:<12} {title[:40]}\n"
                f"    ↳ [CB] No elite — {n_ver} verified / {len(by_w)} total"
            )
            continue

        n_elite_raw = len(elite_wallets)
        if n_elite_raw < min_confluence:
            rejects.append(
                f"  {outcome:<12} {title[:40]}\n"
                f"    ↳ [CB] Only {n_elite_raw} elite (need {min_confluence}+)"
            )
            continue

        # Market data — only for elite-sourced signals
        asset_hint = next((t.asset for t in elite_wallets.values() if t.asset), "")
        slug_hint  = next((t.slug for t in elite_wallets.values() if t.slug), "")

        mkt, mkt_fail = _get_market_for_signal(cid, title, asset_hint, slug_hint)
        if not mkt:
            _is_hft_pre = any(_hft_spike_ratio_value(t) > 0 or t.is_large_trade for t in by_w.values())
            if _is_hft_pre and len(hft_wallets) > 0:
                _trade_price = next(iter(elite_wallets.values())).price
                mkt = Market(
                    yes_price=_trade_price, no_price=1.0 - _trade_price,
                    outcome_prices={"Yes": _trade_price, "No": 1.0 - _trade_price},
                    asset_to_price={asset_hint: _trade_price} if asset_hint else {},
                    asset_to_index={},
                    liq=10_000, volume=50_000, title=title,
                    hrs_left=48.0, slug=slug_hint or "", event_slug="",
                    end_date="", ts=now_t, outcome_labels=[], token_index={},
                    index_to_price={0: _trade_price, 1: 1.0 - _trade_price},
                )
            else:
                rejects.append(f"  {outcome:<12} {title[:40]}\n    ↳ [CB] Market: {mkt_fail}")
                continue

        cur = get_outcome_price(mkt, outcome, asset=asset_hint)

        if not _check_price_zone(cur, price_min, price_max, outcome, title, rejects):
            continue

        # Sports gate
        event_slug = next((t.event_slug for t in group if t.event_slug), "")
        if not event_slug:
            event_slug = mkt.event_slug
        mkt_type = classify_market(title, event_slug, mkt.hrs_left)

        if mkt_type == "SPORTS":
            genuine_sports_elites = {
                w: t for w, t in elite_wallets.items()
                if not S.env().wallet_cache.get(w, {}).get("sports_bot", False)
            }
            if len(genuine_sports_elites) < 1:
                rejects.append(
                    f"  {outcome:<12} {title[:40]}\n"
                    f"    ↳ [CB] Sports: no genuine elites"
                )
                continue

        # Elite avg entry
        elite_entries = [(t.price, t.cash) for t in elite_wallets.values()]
        elite_total_w = sum(c for _, c in elite_entries)
        if elite_total_w == 0:
            continue
        elite_avg_entry = sum(p * w for p, w in elite_entries) / elite_total_w

        newest_elite_ts = max(t.ts for t in elite_wallets.values())
        age_h_elite     = (now_t - newest_elite_ts) / 3600

        # Age gate
        if age_h_elite > max_age_h:
            rejects.append(
                f"  {outcome:<12} {title[:40]}\n"
                f"    ↳ [CB] Stale: {age_h_elite:.1f}h > {max_age_h}h"
            )
            continue

        slippage = (cur - elite_avg_entry) / max(elite_avg_entry, 0.01)
        _max_slip = HFT_MAX_ENTRY_SLIPPAGE if is_hft_signal else MAX_ENTRY_SLIPPAGE
        if slippage > _max_slip:
            rejects.append(
                f"  {outcome:<12} {title[:40]}\n"
                f"    ↳ [CB] Slippage +{slippage*100:.1f}%"
            )
            continue

        drift = (cur - elite_avg_entry) / max(elite_avg_entry, 0.01)
        _max_drift = HFT_MAX_DRIFT if is_hft_signal else MAX_DRIFT
        _min_drift = HFT_MIN_DRIFT if is_hft_signal else MIN_DRIFT
        if drift > _max_drift:
            rejects.append(f"  {outcome:<12} {title[:40]}\n    ↳ [CB] Drift +{drift*100:.1f}%")
            continue
        if drift < _min_drift:
            rejects.append(f"  {outcome:<12} {title[:40]}\n    ↳ [CB] Drift {drift*100:+.1f}%")
            continue

        # EV check
        fair_prob = max(0.02, min(0.97, elite_avg_entry))
        potential_win  = (1.0 / max(cur, 0.01)) - 1.0
        raw_ev = fair_prob * potential_win - (1.0 - fair_prob)
        if raw_ev < 0.01:
            rejects.append(
                f"  {outcome:<12} {title[:40]}\n"
                f"    ↳ [CB] EV {raw_ev*100:+.1f}% < 1%"
            )
            continue

        # Stale loser gate
        if age_h_elite > STALE_LOSER_AGE_H and drift < STALE_LOSER_DRIFT:
            rejects.append(f"  {outcome:<12} {title[:40]}\n    ↳ [CB] Stale loser")
            continue

        # Fee gate
        net_return = (1.0 / max(cur, 0.01) - 1.0) - ROUND_TRIP_FEE
        if net_return <= 0 or cur > 0.965:
            rejects.append(f"  {outcome:<12} {title[:40]}\n    ↳ [CB] Fee gate")
            continue

        # Large trade detection
        has_large_trade = False
        conviction_detail = ""
        _CONVICTION_PORTFOLIO_PCT = float(getattr(C, "strategy_consensus_basket", {}).get("conviction_portfolio_pct", 0.005))
        _CONVICTION_ABS_FLOOR = float(LARGE_TRADE)
        for w, t in {**elite_wallets, **verified_wallets}.items():
            prof = wallets.get(w, {})
            portfolio = prof.get("total_value", 0) or prof.get("total_pnl", 0)
            cash = t.cash
            avg_b = prof.get("avg_bet", 0)
            if portfolio > 0 and cash >= portfolio * _CONVICTION_PORTFOLIO_PCT and cash >= _CONVICTION_ABS_FLOOR:
                w_name = S.env().wallet_cache.get(w, {}).get("name", w[:10])
                conviction_detail = f"{w_name} ${cash:,.0f} = {cash/portfolio*100:.1f}% portfolio"
                has_large_trade = True
                break
            if cash >= MASSIVE_TRADE:
                has_large_trade = True
                break
            if t.is_large_trade or _hft_spike_ratio_value(t) >= 20:
                has_large_trade = True
                break

        is_hft_signal = len(hft_wallets) > 0 and (now_t - newest_elite_ts) <= HFT_MIRROR_DELAY_MAX_SECONDS

        # HFT gate
        if is_hft_signal and not has_large_trade:
            all_elites_are_hft = all(
                (_wp := wallets.get(w)) and
                (_wp.get("hft") or _wp.get("trades_per_hour", 0) >= HFT_MIN_TRADES_PER_HOUR)
                for w in elite_wallets
            )
            if all_elites_are_hft:
                rejects.append(f"  {outcome:<12} {title[:40]}\n    ↳ [CB] HFT-only noise")
                continue

        # Hours left gate
        hrs_left_gate = mkt.hrs_left
        if hrs_left_gate is not None and hrs_left_gate < 1.0:
            if not (is_hft_signal and has_large_trade):
                rejects.append(f"  {outcome:<12} {title[:40]}\n    ↳ [CB] Near-expiry")
                continue

        window = "hot" if any(t.window == "hot" for t in all_ver.values()) else "warm"
        if is_hft_signal:
            window = "hft"

        total_flow   = sum(t.cash for t in by_w.values())
        ver_flow     = sum(t.cash for t in all_ver.values())
        max_bet_cash = max(t.cash for t in all_ver.values())
        newest_ts    = max(t.ts for t in all_ver.values())
        oldest_ts    = min(t.ts for t in all_ver.values())
        age_h        = (now_t - newest_ts) / 3600

        avg_wscore = sum(wallets.get(w, _EMPTY_W).get("score", 0.10) for w in elite_wallets) / len(elite_wallets)

        exits_here   = whale_exits.get(cid, [])
        exits_same_side = list(set(exits_here) & set(all_ver.keys()))

        elite_only_mode = len(verified_wallets) == 0
        n_confluence    = len(all_ver)

        sig = Signal(
            cid=cid, asset=asset_hint, outcome=outcome, title=title,
            slug=mkt.slug or slug_hint, event_slug=event_slug, mkt_type=mkt_type, is_sports=(mkt_type == "SPORTS"),
            strategy="consensus_basket", stop_loss_pct=stop_loss_pct,
            ver=all_ver, elite_ver=elite_wallets,
            n_ver=n_ver, n_elite=len(elite_wallets),
            n_confluence=n_confluence, n_total=len(by_w),
            avg_entry=elite_avg_entry, cur=cur, drift=drift, slippage=slippage,
            total_flow=total_flow, ver_flow=ver_flow,
            max_bet_cash=max_bet_cash, opposing_flow=opposite_elite_cash,
            newest_ts=newest_ts, oldest_ts=oldest_ts, first_seen_ts=oldest_ts, age_h=age_h, window=window,
            avg_wscore=avg_wscore, is_hft=is_hft_signal,
            has_large_trade=has_large_trade, conviction_detail=conviction_detail,
            elite_only_mode=elite_only_mode,
            exits_detected=exits_here, exits_same_side=exits_same_side,
            sports_conviction_mult=1.0,
            mkt=mkt,
        )

        bd    = score_signal(sig)
        total = bd["total"]

        if total < min_score_cfg:
            rejects.append(
                f"  {outcome:<12} {title[:40]}\n"
                f"    ↳ [CB] Score {total:.0f} < {min_score_cfg}"
            )
            continue

        if is_hft_signal and has_large_trade:
            tier = "CONVICTION"
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

        if exits_here and tier in ("ALERT", "CONVICTION"):
            tier = "STRONG"
        if age_h > max_age_h and tier != "CONVICTION":
            tier = "STALE"

        bet   = kelly_bet(sig, wallets, score=total)
        names = _build_names(elite_wallets, verified_wallets, all_ver, is_hft_signal, has_large_trade)
        sig.score = total; sig.bd = bd; sig.tier = tier; sig.bet = bet; sig.names = names
        signals.append(sig)

    return signals, rejects


# ─────────────────────────────────────────────────────────────────────────────
#  MAIN MULTI-STRATEGY DISPATCHER
# ─────────────────────────────────────────────────────────────────────────────
def build_signals(trades: list, wallets: dict, whale_exits: dict) -> tuple[list[Signal], list[str]]:
    """
    Multi-strategy signal dispatcher.
    Returns (signals, rejects).
    Each signal has a 'strategy' tag so logs and UI can show origin.

    Dispatches to:
      - _build_recent_form_signals()   (if "recent_form" in ACTIVE_STRATEGIES)
      - _build_drift_discount_signals() (if "drift_discount" in ACTIVE_STRATEGIES)
      - _build_consensus_basket_signals() (if "consensus_basket" in ACTIVE_STRATEGIES)

    Dedup: same (cid, outcome) from multiple strategies → keep highest-scored,
    merge strategy tags so the log shows "drift_discount+recent_form".
    """
    active = getattr(C, "ACTIVE_STRATEGIES", ["consensus_basket"])

    all_signals = []
    all_rejects = []

    if "recent_form" in active:
        sigs, rejs = _build_recent_form_signals(trades, wallets, whale_exits)
        all_signals.extend(sigs)
        all_rejects.extend(rejs)

    if "drift_discount" in active:
        sigs, rejs = _build_drift_discount_signals(trades, wallets, whale_exits)
        all_signals.extend(sigs)
        all_rejects.extend(rejs)

    if "consensus_basket" in active:
        sigs, rejs = _build_consensus_basket_signals(trades, wallets, whale_exits)
        all_signals.extend(sigs)
        all_rejects.extend(rejs)

    # Price zone enforcement at dispatcher level (belt-and-suspenders)
    price_filtered = []
    for sig in all_signals:
        cur = sig.cur
        if cur < C.MIN_ENTRY_PRICE or cur > C.MAX_ENTRY_PRICE:
            all_rejects.append(
                f"  {sig.outcome:<12} {sig.title[:40]}\n"
                f"    ↳ [DISPATCHER] Price zone block: {cur:.3f} outside [{C.MIN_ENTRY_PRICE:.2f},{C.MAX_ENTRY_PRICE:.2f}]"
            )
            continue
        price_filtered.append(sig)
    all_signals = price_filtered

    # Dedup: same (cid, outcome) from multiple strategies
    # Keep higher-scored; merge strategy tags from both
    deduped: dict[tuple, Signal] = {}
    for sig in all_signals:
        key = (sig.cid, sig.outcome)
        if key not in deduped:
            deduped[key] = sig
        else:
            existing = deduped[key]
            merged_strategy = f"{existing.strategy}+{sig.strategy}"
            if sig.score > existing.score:
                sig.strategy = merged_strategy
                deduped[key] = sig
            else:
                existing.strategy = merged_strategy

    # Hedge bot detection across all deduped signals
    _KNOWN_HEDGE_WALLETS_copy = set(_KNOWN_HEDGE_WALLETS)
    cid_wallets: dict = {}
    for sig in deduped.values():
        cid = sig.cid
        if cid not in cid_wallets:
            cid_wallets[cid] = {}
        for w in sig.elite_ver:
            if w not in cid_wallets[cid]:
                cid_wallets[cid][w] = []
            cid_wallets[cid][w].append(sig.outcome)

    hedge_cids: set = set()
    for cid, wmap in cid_wallets.items():
        for w, outcomes in wmap.items():
            if len(outcomes) >= 2:
                wname = S.env().wallet_cache.get(w, {}).get("name", w[:10] + "…")
                if w not in _KNOWN_HEDGE_WALLETS:
                    _KNOWN_HEDGE_WALLETS.add(w)
                    S._log(f"  🚫 HEDGE BOT FLAGGED: {wname} both {outcomes}", "WARN")
                hedge_cids.add(cid)
                break

    seen_cids: set = set()
    final_signals  = []
    first_seen_by_asset = S.env().signal_first_seen_by_asset
    tp = {"CONVICTION": 6, "HFT": 5, "ALERT": 4, "STRONG": 3, "ELITE_ONLY": 2, "MEDIUM": 1, "STALE": 0}
    sorted_signals = sorted(deduped.values(), key=lambda x: (tp.get(x.tier, 0), x.score), reverse=True)

    for s in sorted_signals:
        if s.cid in hedge_cids:
            all_rejects.append(
                f"  {s.outcome:<12} {s.title[:40]}\n"
                f"    ↳ HEDGE bot market — skipping BOTH sides"
            )
            continue
        sig_elites = set(s.elite_ver.keys())
        if sig_elites and sig_elites.issubset(_KNOWN_HEDGE_WALLETS):
            all_rejects.append(
                f"  {s.outcome:<12} {s.title[:40]}\n"
                f"    ↳ All elites are known hedge bots"
            )
            continue
        if s.cid in seen_cids:
            all_rejects.append(
                f"  {s.outcome:<12} {s.title[:40]}\n"
                f"    ↳ Deduped (another outcome scored higher)"
            )
            continue
        asset_id = str(s.asset or "")
        if asset_id:
            existing_first_seen = first_seen_by_asset.get(asset_id)
            if existing_first_seen is None:
                first_seen_by_asset[asset_id] = s.oldest_ts
                s.first_seen_ts = s.oldest_ts
            else:
                merged_first_seen = min(existing_first_seen, s.oldest_ts)
                first_seen_by_asset[asset_id] = merged_first_seen
                s.first_seen_ts = merged_first_seen
        seen_cids.add(s.cid)
        final_signals.append(s)

    S.env().active_signal_cids.clear()
    for s in final_signals:
        S.env().active_signal_cids[s.cid] = set(s.ver.keys())

    S.env().LAST_SIGNALS = final_signals
    S.env().LAST_REJECTS = all_rejects
    return final_signals, all_rejects
