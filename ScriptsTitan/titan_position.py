from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

import titan_state as S
import titan_config as C

if TYPE_CHECKING:
    from titan_trade import TradeRecord


@dataclass
class Position:
    # ── identity ──────────────────────────────────────────────────────────────
    key:                  str   = ""
    status:               str   = ""
    type:                 str   = ""
    cid:                  str   = ""
    asset:                str   = ""
    title:                str   = ""
    slug:                 str   = ""
    event_slug:           str   = ""
    outcome:              str   = ""
    market_url:           str   = ""

    # ── pricing ───────────────────────────────────────────────────────────────
    entry_price:          float = 0.0
    current_price:        float = 0.0
    cur_price:            float = 0.0
    shares:               float = 0.0
    bet:                  float = 0.0
    exit_price:           float = 0.0
    avg_entry:            float = 0.0

    # ── timestamps ────────────────────────────────────────────────────────────
    entry_ts:             float = 0.0
    exit_ts:              float = 0.0

    # ── strategy ──────────────────────────────────────────────────────────────
    bankroll:             float = 0.0
    tier:                 str   = ""
    strategy:             str   = ""
    stop_loss_pct:        float = 0.0
    score:                float = 0.0
    conviction_detail:    str   = ""
    is_hft:               bool  = False
    is_conviction:        bool  = False

    # ── whale info ────────────────────────────────────────────────────────────
    whale_wallets:        list[str]         = field(default_factory=list)
    elite_wallets:        list[str]         = field(default_factory=list)
    elite_names:          list[str]         = field(default_factory=list)
    whale_names:          list[str]         = field(default_factory=list)
    whale_buy_cash:       dict[str, float]  = field(default_factory=dict)
    n_elite:              int   = 0
    n_confluence:         int   = 0
    ver_flow:             float = 0.0

    # ── market snapshot ───────────────────────────────────────────────────────
    mkt_type:             str   = ""
    is_sports:            bool  = False
    liq:                  float = 0.0
    volume:               float = 0.0
    hrs_left:             float = 0.0
    end_date:             str   = ""

    # ── price history ─────────────────────────────────────────────────────────
    price_history:        list[tuple[float, float]] = field(default_factory=list)
    price_history_source: str        = ""
    price_history_error:  str        = ""

    # ── runtime state ─────────────────────────────────────────────────────────
    peak_pnl_pct:         float = 0.0
    market_fail_count:    int   = 0
    exits:                list  = field(default_factory=list)
    reason:               str   = ""
    ev_info:              dict  = field(default_factory=dict)
    entry_audit:          dict  = field(default_factory=dict)
    exit_audit:           dict  = field(default_factory=dict)

    # ── pnl (closed positions) ────────────────────────────────────────────────
    pnl_usdc:             float = 0.0
    pnl_pct:              float = 0.0

    # ── strategy-specific ─────────────────────────────────────────────────────
    whale_avg_entry:      float = 0.0
    drift_discount_pct:   float = 0.0
    source_recent_wr:     float = 0.0

    # ── computed properties ───────────────────────────────────────────────────
    @property
    def entry_dt(self) -> datetime | None:
        return datetime.fromtimestamp(self.entry_ts) if self.entry_ts else None

    @property
    def exit_dt(self) -> datetime | None:
        return datetime.fromtimestamp(self.exit_ts) if self.exit_ts else None

    # ── serialization ─────────────────────────────────────────────────────────
    def to_dict(self) -> dict:
        return {
            "key":                  self.key,
            "status":               self.status,
            "type":                 self.type,
            "cid":                  self.cid,
            "asset":                self.asset,
            "title":                self.title,
            "slug":                 self.slug,
            "event_slug":           self.event_slug,
            "outcome":              self.outcome,
            "market_url":           self.market_url,
            "entry_price":          self.entry_price,
            "current_price":        self.current_price,
            "cur_price":            self.cur_price,
            "shares":               self.shares,
            "bet":                  self.bet,
            "exit_price":           self.exit_price,
            "avg_entry":            self.avg_entry,
            "entry_ts":             self.entry_ts,
            "exit_ts":              self.exit_ts,
            "bankroll":             self.bankroll,
            "tier":                 self.tier,
            "strategy":             self.strategy,
            "stop_loss_pct":        self.stop_loss_pct,
            "score":                self.score,
            "conviction_detail":    self.conviction_detail,
            "is_hft":               self.is_hft,
            "is_conviction":        self.is_conviction,
            "whale_wallets":        self.whale_wallets,
            "elite_wallets":        self.elite_wallets,
            "elite_names":          self.elite_names,
            "whale_names":          self.whale_names,
            "whale_buy_cash":       self.whale_buy_cash,
            "n_elite":              self.n_elite,
            "n_confluence":         self.n_confluence,
            "ver_flow":             self.ver_flow,
            "mkt_type":             self.mkt_type,
            "is_sports":            self.is_sports,
            "liq":                  self.liq,
            "volume":               self.volume,
            "hrs_left":             self.hrs_left,
            "end_date":             self.end_date,
            "price_history":        self.price_history,
            "price_history_source": self.price_history_source,
            "price_history_error":  self.price_history_error,
            "peak_pnl_pct":         self.peak_pnl_pct,
            "market_fail_count":    self.market_fail_count,
            "exits":                self.exits,
            "reason":               self.reason,
            "ev_info":              self.ev_info,
            "entry_audit":          self.entry_audit,
            "exit_audit":           self.exit_audit,
            "pnl_usdc":             self.pnl_usdc,
            "pnl_pct":              self.pnl_pct,
            "whale_avg_entry":      self.whale_avg_entry,
            "drift_discount_pct":   self.drift_discount_pct,
            "source_recent_wr":     self.source_recent_wr,
        }

    @classmethod
    def from_dict(cls, d: dict) -> Position:
        return cls(
            key=                  str(d.get("key", "")),
            status=               str(d.get("status", "")),
            type=                 str(d.get("type", "")),
            cid=                  str(d.get("cid", "")),
            asset=                str(d.get("asset", "")),
            title=                str(d.get("title", "")),
            slug=                 str(d.get("slug", "")),
            event_slug=           str(d.get("event_slug", "")),
            outcome=              str(d.get("outcome", "")),
            market_url=           str(d.get("market_url", "")),
            entry_price=          float(d.get("entry_price") or 0.0),
            current_price=        float(d.get("current_price") or 0.0),
            cur_price=            float(d.get("cur_price") or 0.0),
            shares=               float(d.get("shares") or 0.0),
            bet=                  float(d.get("bet") or 0.0),
            exit_price=           float(d.get("exit_price") or 0.0),
            avg_entry=            float(d.get("avg_entry") or 0.0),
            entry_ts=             float(d.get("entry_ts") or 0.0),
            exit_ts=              float(d.get("exit_ts") or 0.0),
            bankroll=             float(d.get("bankroll") or 0.0),
            tier=                 str(d.get("tier", "")),
            strategy=             str(d.get("strategy", "")),
            stop_loss_pct=        float(d.get("stop_loss_pct") or 0.0),
            score=                float(d.get("score") or 0.0),
            conviction_detail=    str(d.get("conviction_detail", "")),
            is_hft=               bool(d.get("is_hft", False)),
            is_conviction=        bool(d.get("is_conviction", False)),
            whale_wallets=        [str(w) for w in d.get("whale_wallets", [])],
            elite_wallets=        [str(w) for w in d.get("elite_wallets", [])],
            elite_names=          [str(n) for n in d.get("elite_names", [])],
            whale_names=          [str(n) for n in d.get("whale_names", [])],
            whale_buy_cash=       {str(k): float(v) for k, v in d.get("whale_buy_cash", {}).items()},
            n_elite=              int(d.get("n_elite") or 0),
            n_confluence=         int(d.get("n_confluence") or 0),
            ver_flow=             float(d.get("ver_flow") or 0.0),
            mkt_type=             str(d.get("mkt_type", "")),
            is_sports=            bool(d.get("is_sports", False)),
            liq=                  float(d.get("liq") or 0.0),
            volume=               float(d.get("volume") or 0.0),
            hrs_left=             float(d.get("hrs_left") or 0.0),
            end_date=             str(d.get("end_date", "")),
            price_history=        list(d.get("price_history", [])),
            price_history_source= str(d.get("price_history_source", "")),
            price_history_error=  str(d.get("price_history_error") or ""),
            peak_pnl_pct=         float(d.get("peak_pnl_pct") or 0.0),
            market_fail_count=    int(d.get("market_fail_count") or 0),
            exits=                list(d.get("exits", [])),
            reason=               str(d.get("reason") or ""),
            ev_info=              dict(d.get("ev_info") or {}),
            entry_audit=          dict(d.get("entry_audit") or {}),
            exit_audit=           dict(d.get("exit_audit") or {}),
            pnl_usdc=             float(d.get("pnl_usdc") or 0.0),
            pnl_pct=              float(d.get("pnl_pct") or 0.0),
            whale_avg_entry=      float(d.get("whale_avg_entry") or 0.0),
            drift_discount_pct=   float(d.get("drift_discount_pct") or 0.0),
            source_recent_wr=     float(d.get("source_recent_wr") or 0.0),
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
        from titan_market import fetch_position_price_history
        import titan_db as _DB
        ph = fetch_position_price_history(self.asset)
        if ph:
            self.set_price_history(ph, "clob_api_lazy")
            _DB.upsert_price_history(self.asset, ph)

    # ── factories ─────────────────────────────────────────────────────────────
    @classmethod
    def from_open_dict(cls, key: object, d: dict) -> Position:
        entry_price = float(d.get("entry_price") or 0.0)
        current_price = float(d.get("cur_price") or entry_price)
        return cls(
            key=                  str(key),
            status=               "open",
            type=                 "OPEN",
            cid=                  str(d.get("cid", "")),
            asset=                str(d.get("asset", "")),
            title=                str(d.get("title", "")),
            slug=                 str(d.get("slug", "")),
            event_slug=           str(d.get("event_slug", "")),
            outcome=              str(d.get("outcome", "")),
            market_url=           str(d.get("market_url", "")),
            entry_price=          entry_price,
            current_price=        current_price,
            cur_price=            current_price,
            shares=               float(d.get("shares") or 0.0),
            bet=                  float(d.get("bet") or 0.0),
            entry_ts=             float(d.get("entry_ts") or 0.0),
            tier=                 str(d.get("tier", "")),
            strategy=             str(d.get("strategy", "")),
            elite_wallets=        [str(w) for w in d.get("elite_wallets", [])],
            whale_names=          [str(n) for n in d.get("elite_names", [])],
            whale_buy_cash=       {str(k): float(v) for k, v in d.get("whale_buy_cash", {}).items()},
            price_history=        list(d.get("price_history", [])),
            price_history_source= str(d.get("price_history_source", "")),
        )

    @classmethod
    def from_trade_record(cls, trade: "TradeRecord", price_history: list[tuple[float, float]]) -> Position:
        entry_audit = trade.entry_audit
        slug = str(trade.slug or _audit_value(entry_audit, "market_snapshot", "slug"))
        event_slug = str(trade.event_slug or _audit_value(entry_audit, "market_snapshot", "event_slug"))
        exit_price = float(trade.exit_price or trade.entry_price or 0.0)
        return cls(
            key=                  str((trade.cid, trade.outcome)),
            status=               "closed",
            type=                 "SELL",
            cid=                  trade.cid,
            asset=                trade.asset,
            title=                trade.title,
            slug=                 slug,
            event_slug=           event_slug,
            outcome=              trade.outcome,
            market_url=           trade.market_url,
            entry_price=          float(trade.entry_price or 0.0),
            current_price=        exit_price,
            cur_price=            exit_price,
            exit_price=           exit_price,
            shares=               float(trade.shares or 0.0),
            bet=                  float(trade.bet or 0.0),
            entry_ts=             float(trade.entry_ts or 0.0),
            exit_ts=              float(trade.exit_ts or 0.0),
            tier=                 trade.tier,
            strategy=             trade.strategy,
            elite_wallets=        [str(w) for w in trade.elite_wallets],
            whale_names=          [str(n) for n in trade.whale_names],
            whale_buy_cash=       {str(k): float(v) for k, v in trade.whale_buy_cash.items()},
            price_history=        price_history,
            price_history_source= "db_closed" if price_history else "none",
            price_history_error=  "old trade, no asset" if not trade.asset else "",
            pnl_usdc=             float(trade.pnl_usdc or 0.0),
            pnl_pct=              float(trade.pnl_pct or 0.0),
            bankroll=             float(trade.bankroll or 0.0),
            score=                float(trade.score or 0.0),
            n_confluence=         int(trade.n_confluence or 0),
            is_conviction=        trade.is_conviction,
            avg_entry=            float(trade.avg_entry or 0.0),
            stop_loss_pct=        float(trade.stop_loss_pct or 0.0),
            entry_audit=          dict(trade.entry_audit),
            exit_audit=           dict(trade.exit_audit),
            reason=               trade.reason,
        )


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


def normalize_open_position(key: object, d: dict) -> Position:
    return Position.from_open_dict(key, d)


def normalize_closed_position(trade: "TradeRecord", price_history: list[tuple[float, float]]) -> Position:
    return Position.from_trade_record(trade, price_history)
