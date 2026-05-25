"""
TITAN — SignalBuilderBase abstraction.

Each concrete builder wraps one of the three strategy functions in titan_signals.py
and owns a typed parameter dataclass. Builders are instantiated from config and
live-reloaded: titan_config.reload() rebuilds instances, next cycle picks up new params.

Mirrors the WalletSelector pattern in titan_selector.py.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, fields


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

    def build(self, trades: list, wallets: dict, whale_exits: dict) -> tuple[list, list[str]]:
        from titan_signals import _build_consensus_basket_signals
        return _build_consensus_basket_signals(trades, wallets, whale_exits)


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

    def build(self, trades: list, wallets: dict, whale_exits: dict) -> tuple[list, list[str]]:
        from titan_signals import _build_recent_form_signals
        return _build_recent_form_signals(trades, wallets, whale_exits)


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

    def build(self, trades: list, wallets: dict, whale_exits: dict) -> tuple[list, list[str]]:
        from titan_signals import _build_drift_discount_signals
        return _build_drift_discount_signals(trades, wallets, whale_exits)


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
    """
    Instantiate active builders from the signal_builders config block.
    Falls back to empty list if block is absent (caller handles fallback).
    """
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
