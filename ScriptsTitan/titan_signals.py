
import time
import re
import math
from collections import defaultdict
from dataclasses import dataclass, field, asdict
import titan_state as S
import titan_config as C
from titan_config import *
from titan_market import get_market, get_outcome_price, get_outcome_price_by_trade, fetch_wallet_sells, mark_cid_verified, WalletObservation, Market
from titan_wallet import Wallet, get_wallet_weekly_pnl


@dataclass
class Signal:
    # ── market identity ───────────────────────────────────────────────────────
    cid:            str
    asset:          str
    outcome:        str
    _title:         str
    _slug:          str
    _event_slug:    str

    # ── strategy ─────────────────────────────────────────────────────────────
    strategy:       str          # "recent_form" | "drift_discount" | "consensus_basket"
    stop_loss_pct:  float | None

    # ── whale observations ────────────────────────────────────────────────────
    ver:            dict[str, WalletObservation]
    elite_ver:      dict[str, WalletObservation]
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

    @property
    def title(self) -> str:
        return self.mkt.title or self._title

    @title.setter
    def title(self, value: str) -> None:
        self._title = value

    @property
    def slug(self) -> str:
        return self.mkt.slug or self._slug

    @slug.setter
    def slug(self, value: str) -> None:
        self._slug = value

    @property
    def event_slug(self) -> str:
        return self.mkt.event_slug or self._event_slug

    @event_slug.setter
    def event_slug(self, value: str) -> None:
        self._event_slug = value

    @property
    def mkt_type(self) -> str:
        return self.mkt.mkt_type

    @property
    def is_sports(self) -> bool:
        return self.mkt.is_sports

    snapshot_ts: float | None = None

    def to_json_dict(self) -> dict:
        payload = asdict(self)
        payload.pop("wallets", None)
        payload.pop("_title", None)
        payload.pop("_slug", None)
        payload.pop("_event_slug", None)
        payload.pop("mkt", None)
        payload.pop("snapshot_ts", None)
        payload["title"] = self.title
        payload["slug"] = self.slug
        payload["event_slug"] = self.event_slug
        payload["mkt_type"] = self.mkt_type
        payload["is_sports"] = self.is_sports
        return payload

    @classmethod
    def from_dict(cls, data: dict) -> "Signal":
        cid = str(data.get("cid") or "")
        mkt = S.market_cache.peek(cid) if cid else None
        if mkt is None:
            mkt_raw = data.get("mkt")
            if isinstance(mkt_raw, dict):
                from titan_db import _market_from_payload
                mkt = _market_from_payload(mkt_raw)
            else:
                mkt = Market(
                    title=str(data.get("title") or ""),
                    slug=str(data.get("slug") or ""),
                    event_slug=str(data.get("event_slug") or ""),
                    mkt_type=str(data.get("mkt_type") or ""),
                    is_sports=bool(data.get("is_sports")),
                )

        outcome = str(data.get("outcome") or "")
        asset = str(data.get("asset") or "")

        def _whale_obs_map(raw: object) -> dict[str, WalletObservation]:
            if not isinstance(raw, dict):
                return {}
            out: dict[str, WalletObservation] = {}
            for wallet, obs in raw.items():
                if not isinstance(obs, dict):
                    continue
                out[str(wallet)] = WalletObservation(
                    wallet=str(obs.get("wallet") or wallet),
                    name=str(obs.get("name") or ""),
                    cid=cid,
                    asset=str(obs.get("asset") or asset),
                    slug=mkt.slug,
                    event_slug=mkt.event_slug,
                    title=mkt.title,
                    outcome=outcome,
                    price=float(obs.get("price") or 0.0),
                    size=float(obs.get("size") or 0.0),
                    cash=float(obs.get("cash") or 0.0),
                    ts=float(obs.get("ts") or 0.0),
                    window=str(obs.get("window") or ""),
                    source=str(obs.get("source") or ""),
                    is_elite=bool(obs.get("is_elite")),
                    is_large_trade=bool(obs.get("is_large_trade")),
                    hft_spike_ratio=float(obs["hft_spike_ratio"]) if obs.get("hft_spike_ratio") is not None else None,
                )
            return out

        price_history_raw = data.get("price_history") or []
        price_history: list[tuple[float, float]] = [
            (float(p[0]), float(p[1])) for p in price_history_raw if isinstance(p, (list, tuple)) and len(p) == 2
        ]

        return cls(
            cid=str(data.get("cid") or ""),
            asset=str(data.get("asset") or ""),
            outcome=str(data.get("outcome") or ""),
            _title=str(data.get("title") or ""),
            _slug=str(data.get("slug") or ""),
            _event_slug=str(data.get("event_slug") or ""),
            strategy=str(data.get("strategy") or ""),
            stop_loss_pct=float(data["stop_loss_pct"]) if data.get("stop_loss_pct") is not None else None,
            ver=_whale_obs_map(data.get("ver")),
            elite_ver=_whale_obs_map(data.get("elite_ver")),
            n_ver=int(data.get("n_ver") or 0),
            n_elite=int(data.get("n_elite") or 0),
            n_confluence=int(data.get("n_confluence") or 0),
            n_total=int(data.get("n_total") or 0),
            avg_entry=float(data.get("avg_entry") or 0.0),
            cur=float(data.get("cur") or 0.0),
            drift=float(data.get("drift") or 0.0),
            slippage=float(data.get("slippage") or 0.0),
            total_flow=float(data.get("total_flow") or 0.0),
            ver_flow=float(data.get("ver_flow") or 0.0),
            max_bet_cash=float(data.get("max_bet_cash") or 0.0),
            opposing_flow=float(data.get("opposing_flow") or 0.0),
            newest_ts=float(data.get("newest_ts") or 0.0),
            oldest_ts=float(data.get("oldest_ts") or 0.0),
            first_seen_ts=float(data.get("first_seen_ts") or 0.0),
            age_h=float(data.get("age_h") or 0.0),
            window=str(data.get("window") or ""),
            mkt=mkt,
            avg_wscore=float(data.get("avg_wscore") or 0.0),
            is_hft=bool(data.get("is_hft")),
            has_large_trade=bool(data.get("has_large_trade")),
            conviction_detail=str(data.get("conviction_detail") or ""),
            elite_only_mode=bool(data.get("elite_only_mode")),
            sports_conviction_mult=float(data.get("sports_conviction_mult") or 1.0),
            exits_detected=list(data.get("exits_detected") or []),
            exits_same_side=list(data.get("exits_same_side") or []),
            score=float(data.get("score") or 0.0),
            bd=dict(data.get("bd") or {}),
            tier=str(data.get("tier") or ""),
            bet=float(data.get("bet") or 0.0),
            names=list(data.get("names") or []),
            source_recent_wr=float(data["source_recent_wr"]) if data.get("source_recent_wr") is not None else None,
            drift_discount_pct=float(data["drift_discount_pct"]) if data.get("drift_discount_pct") is not None else None,
            price_history=price_history,
            snapshot_ts=float(data["snapshot_ts"]) if data.get("snapshot_ts") is not None else None,
        )

    def get_prices(self) -> list[tuple[float, float]]:
        self.load_prices()
        return self.price_history

    def load_prices(self) -> None:
        from titan_prices import PRICES
        points, _, _ = PRICES.get_prices(self.asset)
        self.price_history = points


def load_signal_prices_many(signals: list["Signal"]) -> list["Signal"]:
    for signal in signals:
        signal.load_prices()
    return signals


def _hft_spike_ratio_value(trade: WalletObservation) -> float:
    ratio = trade.hft_spike_ratio
    if ratio is None:
        return 0.0
    return ratio


# ─────────────────────────────────────────────────────────────────────────────
#  MARKET TYPE CLASSIFICATION
# ─────────────────────────────────────────────────────────────────────────────
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
def check_wallet_exist(cid_to_wallet_sets: dict, entry_times: dict | None = None) -> dict:
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
        if pos.wallet_buy_cash:
            cid_wallet_buy_cash[cid] = pos.wallet_buy_cash
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
                _min_frac = float(getattr(C, "position_management_ext", {}).get("wallet_exit_min_sell_fraction", 0.30))
                if sell_fraction < _min_frac:
                    S._log(
                        f"  🐋 Partial trim ignored: {(p.name if (p := S.env().wallet_cache.get(wallet)) else None) or wallet[:10]} "
                        f"sold {sell_fraction*100:.0f}% of position (need ≥30%)",
                        "DIAG"
                    )
                    continue

            if wallet not in exits[cid]:
                exits[cid].append(wallet)
                prof   = S.env().wallet_cache.get(wallet)
                w_name = (prof.name if prof is not None else None) or wallet[:10]
                tag    = "🔥" if (prof and prof.is_elite) else "✅" if (prof and prof.is_verified) else "👁"
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
    _mw = _sc.get("multi_wallet_pts", [0, 0, 5, 8])
    bd["multi_whale"] = _mw[min(n_el, len(_mw) - 1)]

    bd["exit_penalty"] = len(s.exits_same_side) * _sc.get("exit_penalty_per_wallet", -8)

    weekly_pnl_total = sum(
        get_wallet_weekly_pnl(w)
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
_EMPTY_W: Wallet = Wallet.make_stub("0x0000000000000000000000000000000000000000", "empty")

def _get_market_for_signal(cid, trade_title, asset_hint, slug_hint, event_slug_hint="", from_verified=True):
    """Call get_market with verified flag so cid is registered."""
    mark_cid_verified(cid)
    return get_market(
        cid,
        trade_title,
        asset=asset_hint,
        slug=slug_hint,
        event_slug=event_slug_hint,
        from_verified=from_verified,
    )


def _build_names(elite_wallets, verified_wallets, all_ver, is_hft_signal, has_large_trade):
    """Build display names for a signal."""
    elite_names = []
    for w in list(elite_wallets.keys())[:3]:
        cached = S.env().wallet_cache.get(w)
        name = (cached.name if cached is not None else None) or all_ver.get(w, {}).get("name") or w[:10] + "…"
        elite_names.append(name)

    conf_names = []
    for w in list(verified_wallets.keys())[:2]:
        cached = S.env().wallet_cache.get(w)
        name = (cached.name if cached is not None else None) or all_ver.get(w, {}).get("name") or w[:10] + "…"
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
#  MAIN MULTI-STRATEGY DISPATCHER
# ─────────────────────────────────────────────────────────────────────────────
def build_signals(trades: list, wallets: dict, wallet_exits: dict) -> tuple[list[Signal], list[str]]:
    all_signals = []
    all_rejects = []

    for builder in C.get_active_builders():
        sigs, rejs = builder.build(trades, wallets, wallet_exits)
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
                wname = (p.name if (p := S.env().wallet_cache.get(w)) else None) or w[:10] + "…"
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
