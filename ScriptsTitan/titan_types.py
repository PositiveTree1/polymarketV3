"""Shared TypedDicts for all data that crosses the API / MCP wire boundary.

Import with TYPE_CHECKING to avoid runtime circular imports:

    from typing import TYPE_CHECKING
    if TYPE_CHECKING:
        from titan_types import AlertDict, TrackedWalletDict, ...
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TypedDict


class AlertDict(TypedDict):
    msg: str


class ErrorDict(TypedDict):
    message: str


class TrackedWalletDict(TypedDict):
    # identity (key injected by get_tracked_wallets)
    wallet:             str
    name:               str
    ts:                 float
    loaded_trade_count: int
    trade_load_limited: bool
    first_loaded_trade_ts: float | None
    last_loaded_trade_ts:  float | None
    # scoring
    score:              float
    win_rate:           float
    wilson_lb:          float
    alpha_per_trade:    float
    wr_source:          str
    # stats
    n_resolved:         int
    n_pos:              int
    total_value:        float
    total_pnl:          float
    pnl_pct:            float
    avg_pos_size:       float
    avg_profit:         float
    avg_bet:            float
    trades_per_hour:    float
    # flags
    verified:           bool
    watchable:          bool
    elite:              bool
    hft:                bool
    vip:                bool
    sports_bot:         bool
    # recent form
    recent_pnl_30d:     float | None
    recent_pnl_7d:      float | None
    recent_ts:          float
    # debug
    detail:             str
    fail_reasons:       list[str]


WhaleDict = TrackedWalletDict  # backward compat alias


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



    
