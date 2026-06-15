"""Shared TypedDicts for data that crosses the API / MCP wire boundary.

Wallet data is represented internally as titan_wallet.Wallet.
At the wire boundary, call wallet.to_wire() to get a plain dict.

Import with TYPE_CHECKING to avoid runtime circular imports:

    from typing import TYPE_CHECKING
    if TYPE_CHECKING:
        from titan_types import AlertDict, ...
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TypedDict


class AlertDict(TypedDict):
    msg: str


class ErrorDict(TypedDict):
    message: str


class PnlSummaryDict(TypedDict):
    bankroll:           float
    bankroll_start:     float
    session_pnl:        float
    total_pnl:          float
    equity_history:     list[tuple[float, float]]
    cooldown_cids:      dict[str, float]
    active_market_cids: list[str]
    watchlist_size:     int


class TradeStatsDict(TypedDict):
    sell_count:  int
    win_count:   int
    loss_count:  int
    sum_pnl:     float
    best:        float
    worst:       float
    win_rate:    float
    avg_win:     float
    avg_loss:    float
    expectancy:  float


class PortfolioOverviewDict(TypedDict):
    running:            bool
    bankroll:           float
    open_value:         float
    total_equity:       float
    session_pnl:        float
    total_pnl:          float
    open_positions:     int
    watchlist_size:     int
    cycle_count:        int
    recent_error_count: int



    
