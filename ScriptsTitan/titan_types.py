"""Shared TypedDicts for all data that crosses the API / MCP wire boundary.

Import with TYPE_CHECKING to avoid runtime circular imports:

    from typing import TYPE_CHECKING
    if TYPE_CHECKING:
        from titan_types import AlertDict, WhaleDict, ...
"""
from __future__ import annotations

from typing import TypedDict


class AlertDict(TypedDict):
    msg: str


class ErrorDict(TypedDict):
    message: str


class PositionBriefDict(TypedDict):
    key:          str
    title:        str
    outcome:      str
    strategy:     str
    tier:         str
    entry_price:  float
    current_price: float
    bet:          float
    shares:       float
    pnl_pct:      float
    pnl_usd:      float
    held_minutes: float
    source_whales: list[str]
    risk_flag:    str


class WhaleDict(TypedDict):
    # identity (key injected by get_whales)
    wallet:             str
    name:               str
    ts:                 float
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
    sports_bot:         bool
    # recent form
    recent_pnl_30d:     float | None
    recent_pnl_7d:      float | None
    recent_ts:          float
    # debug
    detail:             str
    fail_reasons:       list[str]


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


class _TradeRecordRequired(TypedDict):
    cid:          str
    asset:        str
    type:         str          # "BUY" | "SELL"
    title:        str
    outcome:      str
    entry_price:  float
    shares:       float
    bet:          float
    ts:           float
    ts_str:       str
    bankroll:     float
    tier:         str
    strategy:     str
    score:        float
    n_confluence: int
    is_conviction: bool
    market_url:   str
    entry_ts:     float
    elite_wallets: list[str]
    whale_names:  list[str]
    whale_buy_cash: dict[str, float]


class TradeRecordDict(_TradeRecordRequired, total=False):
    exit_price:     float
    exit_ts:        float
    pnl_usdc:       float
    pnl_pct:        float
    reason:         str
    stop_loss_pct:  float
    avg_entry:      float
    entry_audit:    dict
    exit_audit:     dict
    # get_closed_positions extras
    price_history:          list[tuple[float, float]]
    price_history_source:   str
    price_history_error:    str | None
