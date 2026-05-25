from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections import defaultdict
from dataclasses import dataclass, fields

import titan_state as S
import titan_config as C
from titan_config import *
from titan_market import get_outcome_price, fetch_wallet_sells
from titan_wallet import is_hft_wallet, is_recent_form_qualified


# ─────────────────────────────────────────────────────────────────────────────
#  BASE
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class BuilderParams:
    enabled: bool = True

    @classmethod
    def from_dict(cls, d: dict) -> "BuilderParams":
        valid = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in valid})


class SignalBuilderBase(ABC):
    builder_id: str
    display_name: str

    def __init__(self, params: BuilderParams) -> None:
        self.params = params

    @abstractmethod
    def build(
        self,
        trades: list,
        wallets: dict,
        whale_exits: dict,
    ) -> tuple[list, list[str]]:
        """Return (signals, rejects)."""

    @classmethod
    def registry_entry(cls) -> tuple[str, type]:
        return cls.builder_id, cls


# ─────────────────────────────────────────────────────────────────────────────
#  CONSENSUS BASKET
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ConsensusBasketParams(BuilderParams):
    enabled: bool = True
    min_elite_confluence: int = 1
    max_signal_age_h: float = 0.5
    price_min: float = 0.20
    price_max: float = 0.72
    min_score: float = 50.0
    max_positions: int = 5
    max_bet_abs: float = 1.20
    stop_loss_pct: float | None = -0.35
    opposition_ratio_block: float = 0.60
    conviction_portfolio_pct: float = 0.005


class ConsensusBasketBuilder(SignalBuilderBase):
    builder_id = "consensus_basket"
    display_name = "Consensus Basket"
    params: ConsensusBasketParams

    def build(self, trades: list, wallets: dict, whale_exits: dict) -> tuple[list, list[str]]:
        from titan_signals import Signal, score_signal, kelly_bet, _build_names, _get_market_for_signal, _check_price_zone, _hft_spike_ratio_value, _EMPTY_W, _KNOWN_HEDGE_WALLETS

        min_confluence = self.params.min_elite_confluence
        max_age_h      = self.params.max_signal_age_h
        price_min      = self.params.price_min
        price_max      = self.params.price_max
        min_score_cfg  = self.params.min_score
        stop_loss_pct  = self.params.stop_loss_pct

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

            elite_wallets    = {w: t for w, t in by_w.items() if wallets.get(w, _EMPTY_W).get("elite")}
            verified_wallets = {w: t for w, t in by_w.items()
                                if wallets.get(w, _EMPTY_W).get("verified") and w not in elite_wallets}

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

            opposite_elite_cash = 0.0
            our_elite_cash = sum(t.cash for t in elite_wallets.values()) if elite_wallets else 0.0
            for (other_cid, other_outcome), other_group in cid_groups.items():
                if other_cid == cid and other_outcome != outcome:
                    for t in other_group:
                        w = t.wallet
                        if wallets.get(w, _EMPTY_W).get("elite") or wallets.get(w, _EMPTY_W).get("verified"):
                            opposite_elite_cash += t.cash
                    break

            _opp_block = self.params.opposition_ratio_block
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

            asset_hint = next((t.asset for t in elite_wallets.values() if t.asset), "")
            slug_hint  = next((t.slug for t in elite_wallets.values() if t.slug), "")
            event_slug_hint = next((t.event_slug for t in group if t.event_slug), "")

            mkt, mkt_fail = _get_market_for_signal(cid, title, asset_hint, slug_hint, event_slug_hint)
            if not mkt:
                rejects.append(f"  {outcome:<12} {title[:40]}\n    ↳ [CB] Market: {mkt_fail}")
                continue

            cur = get_outcome_price(mkt, outcome, asset=asset_hint)

            if not _check_price_zone(cur, price_min, price_max, outcome, title, rejects):
                continue

            event_slug = next((t.event_slug for t in group if t.event_slug), "")
            if not event_slug:
                event_slug = mkt.event_slug
            if mkt.is_sports:
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

            elite_entries = [(t.price, t.cash) for t in elite_wallets.values()]
            elite_total_w = sum(c for _, c in elite_entries)
            if elite_total_w == 0:
                continue
            elite_avg_entry = sum(p * w for p, w in elite_entries) / elite_total_w

            newest_elite_ts = max(t.ts for t in elite_wallets.values())
            age_h_elite     = (now_t - newest_elite_ts) / 3600

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

            fair_prob = max(0.02, min(0.97, elite_avg_entry))
            potential_win  = (1.0 / max(cur, 0.01)) - 1.0
            raw_ev = fair_prob * potential_win - (1.0 - fair_prob)
            if raw_ev < 0.01:
                rejects.append(
                    f"  {outcome:<12} {title[:40]}\n"
                    f"    ↳ [CB] EV {raw_ev*100:+.1f}% < 1%"
                )
                continue

            if age_h_elite > STALE_LOSER_AGE_H and drift < STALE_LOSER_DRIFT:
                rejects.append(f"  {outcome:<12} {title[:40]}\n    ↳ [CB] Stale loser")
                continue

            net_return = (1.0 / max(cur, 0.01) - 1.0) - ROUND_TRIP_FEE
            if net_return <= 0 or cur > 0.965:
                rejects.append(f"  {outcome:<12} {title[:40]}\n    ↳ [CB] Fee gate")
                continue

            has_large_trade = False
            conviction_detail = ""
            _CONVICTION_PORTFOLIO_PCT = self.params.conviction_portfolio_pct
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

            if is_hft_signal and not has_large_trade:
                all_elites_are_hft = all(
                    (_wp := wallets.get(w)) and
                    (_wp.get("hft") or _wp.get("trades_per_hour", 0) >= HFT_MIN_TRADES_PER_HOUR)
                    for w in elite_wallets
                )
                if all_elites_are_hft:
                    rejects.append(f"  {outcome:<12} {title[:40]}\n    ↳ [CB] HFT-only noise")
                    continue

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
                cid=cid, asset=asset_hint, outcome=outcome, _title=title,
                _slug=mkt.slug or slug_hint, _event_slug=event_slug,
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
            S.market_cache.persist(cid)

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
#  RECENT FORM
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class RecentFormParams(BuilderParams):
    enabled: bool = True
    max_tph: float = 20.0
    min_pnl_30d: float = 0.0
    min_pnl_7d: float = -50.0
    max_signal_age_h: float = 0.75
    min_score: float = 42.0
    price_min: float = 0.18
    price_max: float = 0.78
    max_positions: int = 4
    stop_loss_pct: float | None = None


class RecentFormBuilder(SignalBuilderBase):
    builder_id = "recent_form"
    display_name = "Recent Form"
    params: RecentFormParams

    def build(self, trades: list, wallets: dict, whale_exits: dict) -> tuple[list, list[str]]:
        from titan_signals import Signal, score_signal, kelly_bet, _build_names, _get_market_for_signal, _check_price_zone, _EMPTY_W, _KNOWN_HEDGE_WALLETS

        max_tph       = self.params.max_tph
        min_pnl_30d   = self.params.min_pnl_30d
        min_pnl_7d    = self.params.min_pnl_7d
        max_age_h     = self.params.max_signal_age_h
        min_score     = self.params.min_score
        price_min     = self.params.price_min
        price_max     = self.params.price_max
        stop_loss_pct = self.params.stop_loss_pct

        now_t    = time.time()
        signals  = []
        rejects  = []

        qualified_trades = []
        for t in trades:
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

            if all(w in _KNOWN_HEDGE_WALLETS for w in rf_qualified):
                rejects.append(
                    f"  {outcome:<12} {title[:40]}\n"
                    f"    ↳ [RF] All wallets are known hedge bots"
                )
                continue

            newest_ts = max(t.ts for t in rf_qualified.values())
            oldest_ts = min(t.ts for t in rf_qualified.values())
            age_h = (now_t - newest_ts) / 3600
            if age_h > max_age_h:
                rejects.append(
                    f"  {outcome:<12} {title[:40]}\n"
                    f"    ↳ [RF] Stale: {age_h:.1f}h > {max_age_h}h"
                )
                continue

            asset_hint = next((t.asset for t in rf_qualified.values() if t.asset), "")
            slug_hint  = next((t.slug for t in rf_qualified.values() if t.slug), "")
            event_slug_hint = next((t.event_slug for t in group if t.event_slug), "")

            mkt, mkt_fail = _get_market_for_signal(cid, title, asset_hint, slug_hint, event_slug_hint)
            if not mkt:
                rejects.append(f"  {outcome:<12} {title[:40]}\n    ↳ [RF] Market: {mkt_fail}")
                continue

            cur = get_outcome_price(mkt, outcome, asset=asset_hint)

            if not _check_price_zone(cur, price_min, price_max, outcome, title, rejects):
                continue

            event_slug = next((t.event_slug for t in group if t.event_slug), "")
            if not event_slug:
                event_slug = mkt.event_slug

            entries = [(t.price, t.cash) for t in rf_qualified.values()]
            total_w = sum(c for _, c in entries)
            if total_w == 0:
                continue
            avg_entry = sum(p * w for p, w in entries) / total_w

            drift    = (cur - avg_entry) / max(avg_entry, 0.01)
            slippage = drift

            if slippage > MAX_ENTRY_SLIPPAGE * 2:
                rejects.append(
                    f"  {outcome:<12} {title[:40]}\n"
                    f"    ↳ [RF] Slippage +{slippage*100:.1f}% > {MAX_ENTRY_SLIPPAGE*200:.0f}% RF max"
                )
                continue

            fair_prob = max(0.02, min(0.97, avg_entry))
            potential_win = (1.0 / max(cur, 0.01)) - 1.0
            raw_ev = fair_prob * potential_win - (1.0 - fair_prob)
            if raw_ev < 0.005:
                rejects.append(
                    f"  {outcome:<12} {title[:40]}\n"
                    f"    ↳ [RF] EV too low: {raw_ev*100:+.1f}%"
                )
                continue

            avg_recent_wr = 0.55
            rf_pnl_vals = [wallets.get(w, {}).get("recent_pnl_30d", 0) or 0 for w in rf_qualified]
            if rf_pnl_vals:
                avg_pnl = sum(rf_pnl_vals) / len(rf_pnl_vals)
                avg_recent_wr = max(0.50, min(0.75, 0.55 + avg_pnl / 10000))

            elite_wallets_rf = {w: t for w, t in rf_qualified.items()
                                if wallets.get(w, _EMPTY_W).get("elite")}
            verified_wallets_rf = {w: t for w, t in rf_qualified.items()
                                   if wallets.get(w, _EMPTY_W).get("verified") and w not in elite_wallets_rf}
            all_ver = {**elite_wallets_rf, **verified_wallets_rf}

            scoring_elites = elite_wallets_rf if elite_wallets_rf else rf_qualified
            avg_wscore = sum(wallets.get(w, _EMPTY_W).get("score", 0.10) for w in scoring_elites) / len(scoring_elites)

            exits_here = whale_exits.get(cid, [])
            exits_same_side = list(set(exits_here) & set(all_ver.keys()))

            total_flow = sum(t.cash for t in by_w.values())
            max_bet_cash = max(t.cash for t in rf_qualified.values())
            window = "hot" if any(t.window == "hot" for t in rf_qualified.values()) else "warm"

            sig = Signal(
                cid=cid, asset=asset_hint, outcome=outcome, _title=title,
                _slug=mkt.slug or slug_hint, _event_slug=event_slug,
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
            S.market_cache.persist(cid)

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
#  DRIFT DISCOUNT
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class DriftDiscountParams(BuilderParams):
    enabled: bool = True
    min_discount_pct: float = 0.04
    max_discount_pct: float = 0.12
    max_signal_age_h: float = 6.0
    price_min: float = 0.20
    price_max: float = 0.72
    max_positions: int = 3
    require_still_holding_check: bool = True
    stop_loss_pct: float | None = None


class DriftDiscountBuilder(SignalBuilderBase):
    builder_id = "drift_discount"
    display_name = "Drift Discount"
    params: DriftDiscountParams

    def build(self, trades: list, wallets: dict, whale_exits: dict) -> tuple[list, list[str]]:
        from titan_signals import Signal, kelly_bet, _build_names, _get_market_for_signal, _check_price_zone, _EMPTY_W, _KNOWN_HEDGE_WALLETS

        min_discount  = self.params.min_discount_pct
        max_discount  = self.params.max_discount_pct
        max_age_h     = self.params.max_signal_age_h
        price_min     = self.params.price_min
        price_max     = self.params.price_max
        check_holding = self.params.require_still_holding_check
        stop_loss_pct = self.params.stop_loss_pct

        now_t   = time.time()
        signals = []
        rejects = []

        verified_trades = [
            t for t in trades
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

            newest_ts = max(t.ts for t in all_ver.values())
            oldest_ts = min(t.ts for t in all_ver.values())
            age_h = (now_t - newest_ts) / 3600
            if age_h > max_age_h:
                continue

            asset_hint = next((t.asset for t in all_ver.values() if t.asset), "")
            slug_hint  = next((t.slug for t in all_ver.values() if t.slug), "")
            event_slug_hint = next((t.event_slug for t in group if t.event_slug), "")

            mkt, mkt_fail = _get_market_for_signal(cid, title, asset_hint, slug_hint, event_slug_hint)
            if not mkt:
                continue

            cur = get_outcome_price(mkt, outcome, asset=asset_hint)

            entries = [(t.price, t.cash) for t in all_ver.values()]
            total_w = sum(c for _, c in entries)
            if total_w == 0:
                continue
            avg_whale_entry = sum(p * w for p, w in entries) / total_w

            discount = (avg_whale_entry - cur) / max(avg_whale_entry, 0.01)

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

            if not _check_price_zone(cur, price_min, price_max, outcome, title, rejects):
                continue

            if check_holding:
                exited_wallets = []
                for w, t in all_ver.items():
                    buy_ts = t.ts
                    sells = fetch_wallet_sells(w, buy_ts - 60, limit=50)
                    for s in sells:
                        if (s.cid == cid or s.asset == asset_hint) and s.ts > buy_ts:
                            exited_wallets.append(w)
                            break
                    time.sleep(0.05)

                if exited_wallets:
                    exited_names = [S.env().wallet_cache.get(w, {}).get("name", w[:10]) for w in exited_wallets[:2]]
                    if len(exited_wallets) >= len(all_ver):
                        rejects.append(
                            f"  {outcome:<12} {title[:40]}\n"
                            f"    ↳ [DD] All wallets exited: {exited_names}"
                        )
                        continue
                    for w in exited_wallets:
                        all_ver.pop(w, None)
                        elite_wallets.pop(w, None)
                        verified_wallets.pop(w, None)
                    entries = [(t.price, t.cash) for t in all_ver.values()]
                    total_w = sum(c for _, c in entries)
                    if total_w > 0:
                        avg_whale_entry = sum(p * w for p, w in entries) / total_w
                        discount = (avg_whale_entry - cur) / max(avg_whale_entry, 0.01)

            event_slug = next((t.event_slug for t in group if t.event_slug), "")
            if not event_slug:
                event_slug = mkt.event_slug

            drift = (cur - avg_whale_entry) / max(avg_whale_entry, 0.01)
            avg_wscore = sum(wallets.get(w, _EMPTY_W).get("score", 0.10) for w in all_ver) / max(len(all_ver), 1)

            exits_here = whale_exits.get(cid, [])
            exits_same_side = list(set(exits_here) & set(all_ver.keys()))

            total_flow   = sum(t.cash for t in by_w.values())
            max_bet_cash = max(t.cash for t in all_ver.values())
            window       = "warm"

            n_el = len(elite_wallets)
            drift_score = min(100, 60 + int(discount * 200) + n_el * 8)
            if C.IDEAL_PRICE_MIN <= cur <= C.IDEAL_PRICE_MAX:
                drift_score = min(100, drift_score + 5)

            tier = "ALERT" if drift_score >= ALERT_SCORE else "STRONG" if drift_score >= STRONG_SCORE else "MEDIUM"
            if age_h > MAX_SIGNAL_AGE_H:
                tier = "STRONG"

            sig = Signal(
                cid=cid, asset=asset_hint, outcome=outcome, _title=title,
                _slug=mkt.slug or slug_hint, _event_slug=event_slug,
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
            S.market_cache.persist(cid)

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
#  REGISTRY & FACTORY
# ─────────────────────────────────────────────────────────────────────────────

_BUILDER_REGISTRY: dict[str, type[SignalBuilderBase]] = {
    cls.builder_id: cls
    for cls in [ConsensusBasketBuilder, RecentFormBuilder, DriftDiscountBuilder]
}

_PARAMS_REGISTRY: dict[str, type[BuilderParams]] = {
    "consensus_basket": ConsensusBasketParams,
    "recent_form": RecentFormParams,
    "drift_discount": DriftDiscountParams,
}


def build_builders(signal_builders_cfg: dict) -> list[SignalBuilderBase]:
    active_ids: list[str] = signal_builders_cfg.get("active_builders", [])
    builders_cfg: dict = signal_builders_cfg.get("builders", {})
    instances: list[SignalBuilderBase] = []

    for builder_id in active_ids:
        cls = _BUILDER_REGISTRY.get(builder_id)
        if cls is None:
            continue
        params_cls = _PARAMS_REGISTRY.get(builder_id, BuilderParams)
        raw_params = builders_cfg.get(builder_id, {})
        params = params_cls.from_dict(raw_params)
        if not params.enabled:
            continue
        instances.append(cls(params))

    return instances
