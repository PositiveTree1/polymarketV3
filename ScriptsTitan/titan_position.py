from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any
import webbrowser

import titan_state as S
import titan_config as C

if TYPE_CHECKING:
    from titan_market import Market
    from titan_trade import TradeRecord


def _trade_to_dict(trade: "TradeRecord | None") -> "dict | None":
    if trade is None:
        return None
    from dataclasses import asdict
    return asdict(trade)


def _trade_from_dict(d: "dict | None") -> "TradeRecord | None":
    if not d:
        return None
    from titan_trade import TradeRecord
    return TradeRecord.from_mapping(d)


@dataclass
class Position:

    # ── trades ────────────────────────────────────────────────────────────────
    buy_trade:            "TradeRecord"         = field(repr=False)
    sell_trade:           "TradeRecord | None"  = field(default=None, repr=False)
    _bankroll:            float = field(default=0.0, repr=False)
    _reason:              str   = field(default="", repr=False)

    # ── identity ──────────────────────────────────────────────────────────────
    key:                  str   = ""
    status:               str   = ""
    type:                 str   = ""

    # ── pricing ───────────────────────────────────────────────────────────────
    cur_price:            float = 0.0

    # ── strategy ──────────────────────────────────────────────────────────────
    conviction_detail:    str   = ""
    is_hft:               bool  = False

    # ── whale info ────────────────────────────────────────────────────────────
    whale_wallets:        list[str]         = field(default_factory=list)
    elite_names:          list[str]         = field(default_factory=list)
    n_elite:              int   = 0
    ver_flow:             float = 0.0

    # ── market snapshot ───────────────────────────────────────────────────────
    mkt_type:             str   = ""
    is_sports:            bool  = False

    # ── price history ─────────────────────────────────────────────────────────
    price_history:        list[tuple[float, float]] = field(default_factory=list)
    price_history_source: str        = ""
    price_history_error:  str        = ""

    # ── runtime state ─────────────────────────────────────────────────────────
    peak_pnl_pct:         float = 0.0
    market_fail_count:    int   = 0
    exits:                list  = field(default_factory=list)
    ev_info:              dict  = field(default_factory=dict)

    # ── strategy-specific ─────────────────────────────────────────────────────
    whale_avg_entry:      float = 0.0
    drift_discount_pct:   float = 0.0
    source_recent_wr:     float = 0.0

    # ── computed properties ───────────────────────────────────────────────────
    def __post_init__(self) -> None:
        if self.buy_trade is None:
            raise ValueError("Position requires buy_trade")
        if self.buy_trade.type != "BUY":
            raise ValueError(f"Position buy_trade must be BUY, got {self.buy_trade.type!r}")
        self.key = self.buy_trade.cid

    @property
    def entry_dt(self) -> datetime:
        return datetime.fromtimestamp(self.entry_ts)

    @property
    def exit_dt(self) -> datetime:
        return datetime.fromtimestamp(self.exit_ts)

    @property
    def cid(self) -> str:
        if self.buy_trade and self.buy_trade.cid:
            return self.buy_trade.cid
        if self.sell_trade and self.sell_trade.cid:
            return self.sell_trade.cid
        raise ValueError("Position has no trade cid")

    @cid.setter
    def cid(self, value: str) -> None:
        if self.buy_trade is not None:
            self.buy_trade.cid = value
            return
        if self.sell_trade is not None:
            self.sell_trade.cid = value
            return
        raise ValueError("Cannot set cid without a trade")

    @property
    def asset(self) -> str:
        if self.buy_trade and self.buy_trade.asset:
            return self.buy_trade.asset
        if self.sell_trade and self.sell_trade.asset:
            return self.sell_trade.asset
        raise ValueError("Position has no trade asset")

    @asset.setter
    def asset(self, value: str) -> None:
        if self.buy_trade is not None:
            self.buy_trade.asset = value
            return
        if self.sell_trade is not None:
            self.sell_trade.asset = value
            return
        raise ValueError("Cannot set asset without a trade")

    @property
    def title(self) -> str:
        if self.buy_trade and self.buy_trade.title:
            return self.buy_trade.title
        if self.sell_trade and self.sell_trade.title:
            return self.sell_trade.title
        raise ValueError("Position has no trade title")

    @title.setter
    def title(self, value: str) -> None:
        if self.buy_trade is not None:
            self.buy_trade.title = value
            return
        if self.sell_trade is not None:
            self.sell_trade.title = value
            return
        raise ValueError("Cannot set title without a trade")

    @property
    def slug(self) -> str:
        if self.buy_trade and self.buy_trade.slug:
            return self.buy_trade.slug
        if self.sell_trade and self.sell_trade.slug:
            return self.sell_trade.slug
        raise ValueError("Position has no trade slug")

    @slug.setter
    def slug(self, value: str) -> None:
        if self.buy_trade is not None:
            self.buy_trade.slug = value
            return
        if self.sell_trade is not None:
            self.sell_trade.slug = value
            return
        raise ValueError("Cannot set slug without a trade")

    @property
    def event_slug(self) -> str:
        if self.buy_trade and self.buy_trade.event_slug:
            return self.buy_trade.event_slug
        if self.sell_trade and self.sell_trade.event_slug:
            return self.sell_trade.event_slug
        raise ValueError("Position has no trade event_slug")

    @event_slug.setter
    def event_slug(self, value: str) -> None:
        if self.buy_trade is not None:
            self.buy_trade.event_slug = value
            return
        if self.sell_trade is not None:
            self.sell_trade.event_slug = value
            return
        raise ValueError("Cannot set event_slug without a trade")

    @property
    def outcome(self) -> str:
        if self.buy_trade and self.buy_trade.outcome:
            return self.buy_trade.outcome
        if self.sell_trade and self.sell_trade.outcome:
            return self.sell_trade.outcome
        raise ValueError("Position has no trade outcome")

    @outcome.setter
    def outcome(self, value: str) -> None:
        if self.buy_trade is not None:
            self.buy_trade.outcome = value
            return
        if self.sell_trade is not None:
            self.sell_trade.outcome = value
            return
        raise ValueError("Cannot set outcome without a trade")

    @property
    def bankroll(self) -> float:
        if self.sell_trade:
            return float(self.sell_trade.bankroll or 0.0)
        return float(self.buy_trade.bankroll or 0.0)

    @bankroll.setter
    def bankroll(self, value: float) -> None:
        self._bankroll = float(value)

    @property
    def reason(self) -> str:
        if self.sell_trade and self.sell_trade.reason:
            return self.sell_trade.reason
        return self._reason

    @reason.setter
    def reason(self, value: str) -> None:
        self._reason = value
    
    @property
    def entry_price(self) -> float:
        return self.buy_trade.price
    
    @property
    def exit_price(self) -> float:
        return self.sell_trade.price if self.sell_trade else 0.0
    
    @property
    def entry_ts(self) -> float: 
        return self.buy_trade.ts
    
    @property
    def entry_audit(self) -> dict[str, object] | None: 
        return self.buy_trade.audit

    @property
    def shares(self) -> float:
        return float(self.buy_trade.shares)

    @property
    def bet(self) -> float:
        return float(self.buy_trade.bet)

    @property
    def avg_entry(self) -> float:
        if self.buy_trade.avg_entry is not None:
            return float(self.buy_trade.avg_entry)
        return 0.0

    @property
    def tier(self) -> str:
        return self.buy_trade.tier

    @property
    def strategy(self) -> str:
        return self.buy_trade.strategy

    @property
    def stop_loss_pct(self) -> float:
        if self.buy_trade.stop_loss_pct is not None:
            return float(self.buy_trade.stop_loss_pct)
        return 0.0

    @property
    def score(self) -> float:
        return float(self.buy_trade.score)

    @property
    def is_conviction(self) -> bool:
        return bool(self.buy_trade.is_conviction)

    @property
    def elite_wallets(self) -> list[str]:
        return list(self.buy_trade.elite_wallets)

    @property
    def whale_names(self) -> list[str]:
        return list(self.buy_trade.whale_names)

    @property
    def whale_buy_cash(self) -> dict[str, float]:
        return dict(self.buy_trade.whale_buy_cash)

    @property
    def n_confluence(self) -> int:
        return int(self.buy_trade.n_confluence)
    
    @property
    def exit_audit(self) -> dict[str, object] | None: 
        if self.sell_trade:
            return self.sell_trade.audit
        return None
    
    @property
    def exit_ts(self) -> float: 
        if self.sell_trade:
            return self.sell_trade.ts
        return 0.0
    
    @property
    def pnl_usdc(self) -> float: 
        if self.sell_trade:
            return self.sell_trade.pnl_usdc if self.sell_trade.pnl_usdc is not None else 0.0
        return 0.0
    
    @property
    def pnl_pct(self) -> float: 
        if self.sell_trade:
            return self.sell_trade.pnl_pct if self.sell_trade.pnl_pct is not None else 0.0
        return 0.0
    
    @property
    def market_url(self) -> str: 
        if self.buy_trade:
            return self.buy_trade.market_url
        return ""

    @property
    def market(self) -> "Market":
        if self.buy_trade is None:
            raise ValueError("Position has no buy_trade")
        cid = self.buy_trade.cid
        if not cid:
            raise ValueError("Position buy_trade has no cid")
        market = S.market_cache.get(cid)
        if market is None:
            raise LookupError(f"Market not loaded for cid={cid}")
        return market

    @property
    def liq(self) -> float:
        return float(self.market.liq)

    @property
    def volume(self) -> float:
        return float(self.market.volume)

    @property
    def hrs_left(self) -> float:
        hrs_left = self.market.hrs_left
        return float(hrs_left) if hrs_left is not None else 0.0

    @property
    def end_date(self) -> str:
        return str(self.market.end_date or "")

    

    # ── serialization ─────────────────────────────────────────────────────────
    def to_dict(self) -> dict:
        return {
            "key":                  self.key,
            "status":               self.status,
            "type":                 self.type,
            "cur_price":            self.cur_price,
            "conviction_detail":    self.conviction_detail,
            "is_hft":               self.is_hft,
            "whale_wallets":        self.whale_wallets,
            "elite_names":          self.elite_names,
            "n_elite":              self.n_elite,
            "ver_flow":             self.ver_flow,
            "mkt_type":             self.mkt_type,
            "is_sports":            self.is_sports,
            "price_history":        self.price_history,
            "price_history_source": self.price_history_source,
            "price_history_error":  self.price_history_error,
            "peak_pnl_pct":         self.peak_pnl_pct,
            "market_fail_count":    self.market_fail_count,
            "exits":                self.exits,
            "ev_info":              self.ev_info,
            "whale_avg_entry":      self.whale_avg_entry,
            "drift_discount_pct":   self.drift_discount_pct,
            "source_recent_wr":     self.source_recent_wr,
            "buy_trade":            _trade_to_dict(self.buy_trade),
            "sell_trade":           _trade_to_dict(self.sell_trade),
        }

    @classmethod
    def from_dict(cls, d: dict) -> Position:
        buy_trade = _trade_from_dict(d.get("buy_trade"))
        if buy_trade is None:
            raise ValueError("Position.from_dict requires buy_trade")
        return cls(
            key=                  str(d.get("key", "")),
            status=               str(d.get("status", "")),
            type=                 str(d.get("type", "")),
            cur_price=            float(d.get("cur_price") or 0.0),
            _bankroll=            float(d.get("bankroll") or 0.0),
            conviction_detail=    str(d.get("conviction_detail", "")),
            is_hft=               bool(d.get("is_hft", False)),
            whale_wallets=        [str(w) for w in d.get("whale_wallets", [])],
            elite_names=          [str(n) for n in d.get("elite_names", [])],
            n_elite=              int(d.get("n_elite") or 0),
            ver_flow=             float(d.get("ver_flow") or 0.0),
            mkt_type=             str(d.get("mkt_type", "")),
            is_sports=            bool(d.get("is_sports", False)),
            price_history=        list(d.get("price_history", [])),
            price_history_source= str(d.get("price_history_source", "")),
            price_history_error=  str(d.get("price_history_error") or ""),
            peak_pnl_pct=         float(d.get("peak_pnl_pct") or 0.0),
            market_fail_count=    int(d.get("market_fail_count") or 0),
            exits=                list(d.get("exits", [])),
            _reason=              str(d.get("reason") or ""),
            ev_info=              dict(d.get("ev_info") or {}),
            whale_avg_entry=      float(d.get("whale_avg_entry") or 0.0),
            drift_discount_pct=   float(d.get("drift_discount_pct") or 0.0),
            source_recent_wr=     float(d.get("source_recent_wr") or 0.0),
            buy_trade=            buy_trade,
            sell_trade=           _trade_from_dict(d.get("sell_trade")),
        )

    # ── position logic ────────────────────────────────────────────────────────
    def get_effective_stop_loss(self) -> float | None:
        if self.stop_loss_pct:
            return self.stop_loss_pct
        if C.STOP_LOSS_ENABLED:
            return float(C.STOP_LOSS_PCT)
        return None

    def set_price_history(self, points: list[tuple[float, float]], source: str) -> None:
        self.price_history = points
        self.price_history_source = source
        S._log(
            f"  Price history source [{source}] for {self.title[:30]} "
            f"asset={self.asset[:20]} points={len(points)}",
            "DIAG",
        )

    def ensure_price_history(self) -> None:
        if self.price_history and not self.price_history_source:
            self.price_history_source = "unknown_existing"
            return
        if self.price_history or not self.asset:
            return
        import titan_db as _DB
        from titan_market import fetch_asset_price_history
        ph = _DB.load_price_history(self.asset)
        if ph:
            self.set_price_history(ph, "db")
            return
        ph = fetch_asset_price_history(self.asset)
        if ph:
            self.set_price_history(ph, "clob_api")
            _DB.upsert_price_history(self.asset, ph)

    # ── factories ─────────────────────────────────────────────────────────────
    def add_trade(self, trade: "TradeRecord") -> None:
        if trade.type == "BUY":
            self.buy_trade    = trade
            self.cur_price    = self.cur_price or self.entry_price
        elif trade.type == "SELL":
            self.sell_trade   = trade
            self.bankroll     = float(trade.bankroll or 0.0)
            self.reason       = trade.reason or self.reason
            self.status       = "closed"
            self.type         = "SELL"
            if not self.slug:
                self.slug = str(trade.slug or _audit_value(trade.audit, "market_snapshot", "slug"))
            if not self.event_slug:
                self.event_slug = str(trade.event_slug or _audit_value(trade.audit, "market_snapshot", "event_slug"))

    def open_on_polymarket(self) -> None:
        S._log(f"Opening Polymarket URL: {self.market_url}", "DEBUG")
        webbrowser.open(self.market_url)
        return


def _audit_value(audit: object, *path: str) -> str:
    current = audit
    for part in path:
        if not isinstance(current, dict):
            return ""
        current = current.get(part)
    return "" if current is None else str(current)


# ── module-level aliases kept for call sites not yet migrated to methods ──────
def get_effective_stop_loss(pos: Position) -> float | None:
    return pos.get_effective_stop_loss()


def set_position_price_history(pos: Position, points: list[tuple[float, float]], source: str) -> None:
    pos.set_price_history(points, source)


def group_trades_by_position(trades: "list[TradeRecord]") -> "dict[str, list[TradeRecord]]":
    """Group trades into per-position buckets.
    Key is asset token when available, else 'title|||outcome' as fallback for old records."""
    groups: dict[str, list[TradeRecord]] = {}
    for t in trades:
        key = t.asset if t.asset else f"{t.title}|||{t.outcome}"
        groups.setdefault(key, []).append(t)
    return groups


def build_position_from_trades(trades: "list[TradeRecord]") -> Position:
    pos = Position( buy_trade = trades[0] )
    for t in trades[1:]:
        pos.add_trade(t)
    pos.ensure_price_history()
    return pos
