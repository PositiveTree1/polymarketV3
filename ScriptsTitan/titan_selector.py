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
from typing import TYPE_CHECKING
import titan_config as C

if TYPE_CHECKING:
    from titan_wallet import Wallet


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


@dataclass
class DiscoveryConfig:
    use_large_trades: bool
    large_trade_limit: int
    min_trade_cash: float
    trade_side: str
    use_leaderboard: bool
    leaderboard_limit: int
    leaderboard_category: str
    leaderboard_order_by: str
    leaderboard_periods: list[str]


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
    def score(self, wallet: "Wallet") -> float:
        """Return composite 0-1 score for a wallet."""

    @abstractmethod
    def is_selected(self, wallet: "Wallet", score: float) -> tuple["WalletTier", list[str]]:
        """Return (status, fail_reasons)."""

    # Fetch candidate wallet addresses from large recent buy trades and leaderboard
    # snapshots, returning a de-duplicated input set for later evaluation. This
    # method does discovery only: it does not score wallets or decide whether they
    # are watchable, verified, or elite; that happens later via score()/is_selected().
    def discover(self) -> list[str]:
        """
        Return a list of candidate wallet addresses.
        Default implementation: leaderboard + high-value trades.
        Override for a different discovery source.
        """
        import titan_state as S

        candidates: set[str] = set()
        discovery = self.discovery_config()

        if discovery.use_large_trades:
            top_trades = S.safe_get(f"{C.DATA_API}/trades", {
                "limit": discovery.large_trade_limit,
                "filterType": "CASH",
                "filterAmount": discovery.min_trade_cash,
                "side": discovery.trade_side,
            })
            if top_trades and isinstance(top_trades, list):
                for t in top_trades:
                    w = (t.get("proxyWallet") or "").lower()
                    if w and len(w) == 42 and w.startswith("0x"):
                        candidates.add(w)

        if discovery.use_leaderboard:
            for period in discovery.leaderboard_periods:
                lb_data = S.safe_get(f"{C.DATA_API}/leaderboard", {
                    "limit": discovery.leaderboard_limit,
                    "timePeriod": period,
                    "category": discovery.leaderboard_category,
                    "orderBy": discovery.leaderboard_order_by,
                })
                if lb_data and isinstance(lb_data, list):
                    for entry in lb_data:
                        w = (entry.get("proxyWallet") or entry.get("address") or "").lower()
                        if w and len(w) == 42 and w.startswith("0x"):
                            candidates.add(w)
                time.sleep(0.25)

        return list(candidates)

    def discovery_config(self) -> DiscoveryConfig:
        return DiscoveryConfig(
            use_large_trades=True,
            large_trade_limit=200,
            min_trade_cash=5000.0,
            trade_side="BUY",
            use_leaderboard=True,
            leaderboard_limit=100,
            leaderboard_category="OVERALL",
            leaderboard_order_by="PNL",
            leaderboard_periods=["ALL", "MONTH", "WEEK"],
        )


# ─────────────────────────────────────────────────────────────────────────────
#  PERFORMANCE SELECTOR
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class PerformanceSelectorParams(SelectorParams):
    # Discovery
    discovery_use_large_trades: bool = True
    discovery_large_trade_limit: int = 200
    min_trade_cash_discovery: float = 5000.0
    discovery_trade_side: str = "BUY"
    discovery_use_leaderboard: bool = True
    discovery_leaderboard_limit: int = 100
    discovery_leaderboard_category: str = "OVERALL"
    discovery_leaderboard_order_by: str = "PNL"
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
    hft_enabled: bool = True
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
    Replicates and supersedes the original get_compute_and_store_wallet() tiering logic.
    """

    selector_id = "performance"
    display_name = "Performance (High Win Rate + PnL)"

    def __init__(self, params: PerformanceSelectorParams) -> None:
        super().__init__(params)

    @property
    def p(self) -> PerformanceSelectorParams:
        return self.params  # type: ignore[return-value]

    def discovery_config(self) -> DiscoveryConfig:
        p = self.p
        return DiscoveryConfig(
            use_large_trades=p.discovery_use_large_trades,
            large_trade_limit=p.discovery_large_trade_limit,
            min_trade_cash=p.min_trade_cash_discovery,
            trade_side=p.discovery_trade_side,
            use_leaderboard=p.discovery_use_leaderboard,
            leaderboard_limit=p.discovery_leaderboard_limit,
            leaderboard_category=p.discovery_leaderboard_category,
            leaderboard_order_by=p.discovery_leaderboard_order_by,
            leaderboard_periods=p.leaderboard_periods,
        )

    def score(self, wallet: "Wallet") -> float:
        p = self.p
        return (
            p.weight_wilson         * wallet.wilson_lb +
            p.weight_pnl_pct        * min(1.0, max(0.0, wallet.pnl_pct / 30)) +
            p.weight_portfolio      * min(1.0, wallet.total_value / 25_000) +
            p.weight_trade_count    * min(1.0, wallet.n_resolved / 20) +
            p.weight_open_positions * min(1.0, wallet.n_pos / 10) +
            p.weight_alpha          * min(1.0, max(0.0, wallet.avg_profit) / 50)
        )

    def is_selected(self, wallet: "Wallet", score: float) -> tuple["WalletTier", list[str]]:
        from titan_wallet import WalletTier
        p          = self.p
        wr         = wallet.win_rate
        wb         = wallet.wilson_lb
        n_res      = wallet.n_resolved
        pnl        = wallet.total_pnl
        cur        = wallet.total_value
        avg_profit = wallet.avg_profit
        avg_bet    = wallet.avg_bet
        tph        = wallet.trades_per_hour
        apt        = wallet.alpha_per_trade

        fail_reasons: list[str] = []

        hft_detected = tph >= p.hft_tph_threshold or (avg_bet > 0 and avg_bet < 50 and n_res > 100)
        if hft_detected and not p.hft_enabled:
            return WalletTier.REJECTED, ["HFT_DISABLED"]

        watchable_ok = (
            wr    >= p.min_win_rate_watch and
            wb    >= p.wilson_min_watch   and
            n_res >= p.min_resolved_bets  and
            pnl   >= p.min_pnl
        )
        if wr    < p.min_win_rate_watch: fail_reasons.append(f"WR {wr*100:.0f}%<{p.min_win_rate_watch*100:.0f}%")
        if wb    < p.wilson_min_watch:   fail_reasons.append(f"WilsonLB {wb*100:.0f}%<{p.wilson_min_watch*100:.0f}%")
        if n_res < p.min_resolved_bets:  fail_reasons.append(f"Resolved {n_res}<{p.min_resolved_bets}")
        if pnl   < p.min_pnl:           fail_reasons.append(f"PnL ${pnl:+,.0f}")
        if hft_detected:
            roi_ok  = True
            port_ok = cur >= p.min_portfolio_or_pnl or pnl >= p.min_portfolio_or_pnl
        else:
            bet_ok  = avg_bet == 0 or avg_bet >= p.min_avg_bet
            roi_ok  = avg_profit >= p.min_avg_profit and bet_ok
            port_ok = cur >= p.min_portfolio_or_pnl or pnl >= p.min_portfolio_or_pnl

        verified_ok = (
            watchable_ok and
            wr >= p.min_win_rate_ver and
            wb >= p.wilson_min_ver   and
            roi_ok and port_ok
        )

        if watchable_ok and not roi_ok:
            fail_reasons.append(
                f"ROI: avg_profit=${avg_profit:.1f}<${p.min_avg_profit}"
                + (f", avg_bet=${avg_bet:.0f}<${p.min_avg_bet:.0f}" if avg_bet > 0 and avg_bet < p.min_avg_bet else "")
            )
        if watchable_ok and not port_ok:
            fail_reasons.append(f"PORT: cur=${cur:,.0f} pnl=${pnl:+,.0f}")
        if watchable_ok and wr < p.min_win_rate_ver:
            fail_reasons.append(f"VER_WR {wr*100:.0f}%<{p.min_win_rate_ver*100:.0f}%")

        portfolio_proxy = max(cur, pnl)
        elite_ok = (
            verified_ok and
            pnl             >= p.elite_min_pnl      and
            portfolio_proxy >= p.elite_min_portfolio and
            score           >= p.elite_min_score     and
            n_res           >= p.elite_min_resolved  and
            apt             >= p.elite_alpha_per_trade
        )

        if verified_ok and not elite_ok:
            reasons: list[str] = []
            if pnl             < p.elite_min_pnl:      reasons.append(f"PnL ${pnl:+,.0f}<${p.elite_min_pnl:,.0f}")
            if portfolio_proxy < p.elite_min_portfolio: reasons.append(f"Port ${portfolio_proxy:,.0f}<${p.elite_min_portfolio:,.0f}")
            if score           < p.elite_min_score:     reasons.append(f"Score {score:.2f}<{p.elite_min_score}")
            if n_res           < p.elite_min_resolved:  reasons.append(f"Resolved {n_res}<{p.elite_min_resolved}")
            if apt             < p.elite_alpha_per_trade: reasons.append(f"Alpha ${apt:.1f}<${p.elite_alpha_per_trade}")
            if reasons:
                fail_reasons.append("NOT_ELITE: " + ", ".join(reasons))

        if elite_ok:        status = WalletTier.ELITE
        elif verified_ok:   status = WalletTier.VERIFIED
        elif watchable_ok:  status = WalletTier.WATCH
        else:               status = WalletTier.REJECTED
        return status, fail_reasons

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
