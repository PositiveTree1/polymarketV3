"""
TITAN — WalletSelector abstraction.

Separates the logic for identifying wallets of interest from the rest of the system.
Each selector has its own parameter dataclass loaded from the `selector` config block.
Parameters are live-reloaded: tuning a value in the UI takes effect on the next cycle.

Flow:
    PerformanceSelector.discover()  →  candidate wallet addresses
    PerformanceSelector.score()     →  0-1 composite score
    PerformanceSelector.is_selected() →  final binary gate (sets tier flags)
"""

from __future__ import annotations

import time
import math
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, fields


# ─────────────────────────────────────────────────────────────────────────────
#  BASE
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class SelectorParams:
    """Base for all selector parameter blocks."""

    @classmethod
    def from_dict(cls, d: dict) -> "SelectorParams":
        valid = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in valid})


class WalletSelector(ABC):
    """
    Base class for all wallet-of-interest selectors.

    Subclasses implement score() and is_selected(). The params dataclass is
    reloaded from config before each discovery cycle — no restart needed.
    """

    selector_id: str
    display_name: str

    def __init__(self, params: SelectorParams) -> None:
        self.params = params

    @abstractmethod
    def score(self, raw: dict) -> float:
        """Return composite 0-1 score for a wallet's raw position/winrate data."""

    @abstractmethod
    def is_selected(self, raw: dict, score: float) -> tuple[bool, bool, bool, list[str]]:
        """
        Return (watchable, verified, elite, fail_reasons).
        raw: position/winrate data dict as fetched from Polymarket.
        score: result of self.score(raw).
        """

    def discover(self) -> list[str]:
        """
        Return a list of candidate wallet addresses.
        Default implementation: leaderboard + high-value trades.
        Override for a different discovery source.
        """
        import titan_state as S
        from titan_config import DATA_API

        candidates: set[str] = set()

        top_trades = S.safe_get(f"{DATA_API}/trades", {
            "limit": 200, "filterType": "CASH",
            "filterAmount": self._discovery_min_cash(), "side": "BUY",
        })
        if top_trades and isinstance(top_trades, list):
            for t in top_trades:
                w = (t.get("proxyWallet") or "").lower()
                if w and len(w) == 42 and w.startswith("0x"):
                    candidates.add(w)

        for lb_params in [
            {"limit": 100, "timePeriod": "ALL",   "category": "OVERALL", "orderBy": "PNL"},
            {"limit": 100, "timePeriod": "MONTH", "category": "OVERALL", "orderBy": "PNL"},
            {"limit": 100, "timePeriod": "WEEK",  "category": "OVERALL", "orderBy": "PNL"},
        ]:
            lb_data = S.safe_get(f"{DATA_API}/leaderboard", lb_params)
            if lb_data and isinstance(lb_data, list):
                for entry in lb_data:
                    w = (entry.get("proxyWallet") or entry.get("address") or "").lower()
                    if w and len(w) == 42 and w.startswith("0x"):
                        candidates.add(w)
            time.sleep(0.25)

        return list(candidates)

    def _discovery_min_cash(self) -> float:
        return getattr(self.params, "min_trade_cash_discovery", 5000.0)


# ─────────────────────────────────────────────────────────────────────────────
#  PERFORMANCE SELECTOR
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class PerformanceSelectorParams(SelectorParams):
    # Discovery
    min_trade_cash_discovery: float = 5000.0
    leaderboard_periods: list[str] = field(default_factory=lambda: ["ALL", "MONTH", "WEEK"])

    # WATCHABLE gate
    min_win_rate_watch: float = 0.53
    wilson_min_watch: float = 0.45
    min_resolved_bets: int = 10
    min_pnl: float = 0.0

    # VERIFIED gate
    min_win_rate_ver: float = 0.56
    wilson_min_ver: float = 0.49
    min_avg_profit: float = 2.0
    min_avg_bet: float = 10.0
    min_portfolio_or_pnl: float = 500.0

    # ELITE gate
    elite_min_pnl: float = 40_000.0
    elite_min_portfolio: float = 80_000.0
    elite_min_score: float = 0.72
    elite_min_resolved: int = 20
    elite_alpha_per_trade: float = 1.0

    # Scoring weights (must sum to 1.0)
    weight_wilson: float = 0.30
    weight_pnl_pct: float = 0.25
    weight_portfolio: float = 0.15
    weight_trade_count: float = 0.10
    weight_open_positions: float = 0.10
    weight_alpha: float = 0.10

    # HFT / bot filters
    hft_tph_threshold: float = 50.0
    sports_bot_tph_threshold: float = 100.0


_SPORTS_BOT_NAMES = frozenset({
    "gamblingisallyouneed", "swisstony", "rn1", "cannae", "lilybaeum",
    "billdenter", "billdenter2026", "elkmonkey", "billyel", "sportsguy",
    "texaskid", "ferrarichampions", "ferrarichampions2026", "snakeball",
})


class PerformanceSelector(WalletSelector):
    """
    Selects wallets based on statistical performance: win rate, PnL, portfolio size.
    Replicates and supersedes the original fetch_wallet() tiering logic.
    """

    selector_id = "performance"
    display_name = "Performance (High Win Rate + PnL)"

    def __init__(self, params: PerformanceSelectorParams) -> None:
        super().__init__(params)

    @property
    def p(self) -> PerformanceSelectorParams:
        return self.params  # type: ignore[return-value]

    def score(self, raw: dict) -> float:
        p = self.p
        wb         = raw.get("wilson_lb", 0.0)
        pct        = raw.get("pnl_pct", 0.0)
        cur        = raw.get("total_value", 0.0)
        n_res      = raw.get("n_resolved", 0)
        n_pos      = raw.get("n_pos", 0)
        avg_profit = raw.get("avg_profit", 0.0)
        return (
            p.weight_wilson         * wb +
            p.weight_pnl_pct        * min(1.0, max(0.0, pct / 30)) +
            p.weight_portfolio      * min(1.0, cur / 25_000) +
            p.weight_trade_count    * min(1.0, n_res / 20) +
            p.weight_open_positions * min(1.0, n_pos / 10) +
            p.weight_alpha          * min(1.0, max(0.0, avg_profit) / 50)
        )

    def is_selected(self, raw: dict, score: float) -> tuple[bool, bool, bool, list[str]]:
        p          = self.p
        wr         = raw.get("win_rate", 0.0)
        wb         = raw.get("wilson_lb", 0.0)
        n_res      = raw.get("n_resolved", 0)
        pnl        = raw.get("total_pnl", 0.0)
        cur        = raw.get("total_value", 0.0)
        avg_profit = raw.get("avg_profit", 0.0)
        avg_bet    = raw.get("avg_bet", 0.0)
        tph        = raw.get("trades_per_hour", 0.0)
        apt        = raw.get("alpha_per_trade", 0.0)

        fail_reasons: list[str] = []

        watchable = (
            wr    >= p.min_win_rate_watch and
            wb    >= p.wilson_min_watch   and
            n_res >= p.min_resolved_bets  and
            pnl   >= p.min_pnl
        )
        if wr    < p.min_win_rate_watch: fail_reasons.append(f"WR {wr*100:.0f}%<{p.min_win_rate_watch*100:.0f}%")
        if wb    < p.wilson_min_watch:   fail_reasons.append(f"WilsonLB {wb*100:.0f}%<{p.wilson_min_watch*100:.0f}%")
        if n_res < p.min_resolved_bets:  fail_reasons.append(f"Resolved {n_res}<{p.min_resolved_bets}")
        if pnl   < p.min_pnl:           fail_reasons.append(f"PnL ${pnl:+,.0f}")

        hft_detected = tph >= p.hft_tph_threshold or (avg_bet > 0 and avg_bet < 50 and n_res > 100)
        if hft_detected:
            roi_ok  = True
            port_ok = cur >= p.min_portfolio_or_pnl or pnl >= p.min_portfolio_or_pnl
        else:
            roi_ok  = avg_profit >= p.min_avg_profit and avg_bet >= p.min_avg_bet
            port_ok = cur >= p.min_portfolio_or_pnl or pnl >= p.min_portfolio_or_pnl

        verified = (
            watchable and
            wr >= p.min_win_rate_ver and
            wb >= p.wilson_min_ver   and
            roi_ok and port_ok
        )

        if watchable and not roi_ok:
            fail_reasons.append(
                f"ROI: avg_profit=${avg_profit:.1f}<${p.min_avg_profit}, avg_bet=${avg_bet:.0f}"
            )
        if watchable and not port_ok:
            fail_reasons.append(f"PORT: cur=${cur:,.0f} pnl=${pnl:+,.0f}")
        if watchable and wr < p.min_win_rate_ver:
            fail_reasons.append(f"VER_WR {wr*100:.0f}%<{p.min_win_rate_ver*100:.0f}%")

        portfolio_proxy = max(cur, pnl)
        elite = (
            verified and
            pnl             >= p.elite_min_pnl      and
            portfolio_proxy >= p.elite_min_portfolio and
            score           >= p.elite_min_score     and
            n_res           >= p.elite_min_resolved  and
            apt             >= p.elite_alpha_per_trade
        )

        if verified and not elite:
            reasons: list[str] = []
            if pnl             < p.elite_min_pnl:      reasons.append(f"PnL ${pnl:+,.0f}<${p.elite_min_pnl:,.0f}")
            if portfolio_proxy < p.elite_min_portfolio: reasons.append(f"Port ${portfolio_proxy:,.0f}<${p.elite_min_portfolio:,.0f}")
            if score           < p.elite_min_score:     reasons.append(f"Score {score:.2f}<{p.elite_min_score}")
            if n_res           < p.elite_min_resolved:  reasons.append(f"Resolved {n_res}<{p.elite_min_resolved}")
            if apt             < p.elite_alpha_per_trade: reasons.append(f"Alpha ${apt:.1f}<${p.elite_alpha_per_trade}")
            if reasons:
                fail_reasons.append("NOT_ELITE: " + ", ".join(reasons))

        return watchable, verified, elite, fail_reasons

    def is_sports_bot(self, name: str, tph: float) -> bool:
        p = self.p
        lname = name.lower()
        return (
            tph >= p.sports_bot_tph_threshold or
            any(sbn in lname for sbn in _SPORTS_BOT_NAMES) or
            (tph >= 50 and 0 < tph < 100)  # mid-rate bots
        )

    def is_hft(self, tph: float, avg_bet: float, n_res: int) -> bool:
        p = self.p
        return tph >= p.hft_tph_threshold or (avg_bet > 0 and avg_bet < 50 and n_res > 100)


# ─────────────────────────────────────────────────────────────────────────────
#  REGISTRY  — add new selectors here
# ─────────────────────────────────────────────────────────────────────────────

_SELECTOR_CLASSES: dict[str, type[WalletSelector]] = {
    PerformanceSelector.selector_id: PerformanceSelector,
}

_PARAM_CLASSES: dict[str, type[SelectorParams]] = {
    PerformanceSelector.selector_id: PerformanceSelectorParams,
}


def build_selector(selector_id: str, params_dict: dict) -> WalletSelector:
    """Instantiate a selector by id, populating params from a dict."""
    cls = _SELECTOR_CLASSES.get(selector_id)
    if cls is None:
        raise ValueError(f"Unknown selector id: {selector_id!r}. Available: {list(_SELECTOR_CLASSES)}")
    param_cls = _PARAM_CLASSES[selector_id]
    params = param_cls.from_dict(params_dict)
    return cls(params)


def available_selectors() -> list[dict]:
    """Return display info for all registered selectors (for UI dropdown)."""
    return [
        {"id": sid, "name": cls.display_name}
        for sid, cls in _SELECTOR_CLASSES.items()
    ]
