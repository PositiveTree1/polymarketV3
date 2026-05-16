from dataclasses import dataclass, field
from datetime import datetime
from typing import Mapping, Any
import webbrowser

import titan_state as S

@dataclass
class TradeRecord:
    cid:                  str = ""
    asset:                str = ""
    type:                 str = ""  # "BUY" | "SELL"
    title:                str = ""
    outcome:              str = ""
    price:                float = 0.0
    shares:               float = 0.0
    bet:                  float = 0.0
    ts:                   float = 0.0
    bankroll:             float = 0.0
    tier:                 str = ""
    strategy:             str = ""
    score:                float = 0.0
    n_confluence:         int = 0
    is_conviction:        bool = False
    market_url:           str = ""
    elite_wallets:        list[str] = field(default_factory=list)
    whale_names:          list[str] = field(default_factory=list)
    whale_buy_cash:       dict[str, float] = field(default_factory=dict)
    slug:                 str = ""
    event_slug:           str = ""
    pnl_usdc:             float | None = None
    pnl_pct:              float | None = None
    reason:               str = ""
    stop_loss_pct:        float | None = None
    avg_entry:            float | None = None
    audit:                dict[str, Any] = field(default_factory=dict)
    # get_closed_positions extras
    price_history:        list[tuple[float, float]] = field(default_factory=list)
    price_history_source: str = ""
    price_history_error:  str | None = None

    @property
    def timestamp(self) -> datetime:
        return datetime.fromtimestamp(self.ts)
    
    @property
    def ts_str(self) -> str:
        return self.timestamp.strftime("%Y-%m-%d %H:%M:%S")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "TradeRecord":
        raw_price_history = value.get("price_history")
        price_history: list[tuple[float, float]] = []
        if isinstance(raw_price_history, list):
            for point in raw_price_history:
                if isinstance(point, (list, tuple)) and len(point) == 2:
                    ts_value = point[0]
                    price_value = point[1]
                    if isinstance(ts_value, (int, float)) and isinstance(price_value, (int, float)):
                        price_history.append((float(ts_value), float(price_value)))

        raw_whale_buy_cash = value.get("whale_buy_cash")
        whale_buy_cash: dict[str, float] = {}
        if isinstance(raw_whale_buy_cash, Mapping):
            for key, cash_value in raw_whale_buy_cash.items():
                if isinstance(cash_value, (int, float)):
                    whale_buy_cash[str(key)] = float(cash_value)

        entry_audit = value.get("entry_audit")
        exit_audit = value.get("exit_audit")
        return cls(
            cid=str(value.get("cid") or ""),
            asset=str(value.get("asset") or ""),
            type=str(value.get("type") or ""),
            title=str(value.get("title") or ""),
            outcome=str(value.get("outcome") or ""),
            price=float(value.get("price") or 0.0),
            shares=float(value.get("shares") or 0.0),
            bet=float(value.get("bet") or 0.0),
            ts=float(value.get("ts") or 0.0),
            bankroll=float(value.get("bankroll") or 0.0),
            tier=str(value.get("tier") or ""),
            strategy=str(value.get("strategy") or ""),
            score=float(value.get("score") or 0.0),
            n_confluence=int(value.get("n_confluence") or 0),
            is_conviction=bool(value.get("is_conviction") or False),
            market_url=str(value.get("market_url") or ""),
            elite_wallets=[str(item) for item in value.get("elite_wallets", []) if isinstance(value.get("elite_wallets", []), list)],
            whale_names=[str(item) for item in value.get("whale_names", []) if isinstance(value.get("whale_names", []), list)],
            whale_buy_cash=whale_buy_cash,
            slug=str(value.get("slug") or ""),
            event_slug=str(value.get("event_slug") or ""),
            pnl_usdc=float(value["pnl_usdc"]) if value.get("pnl_usdc") is not None else None,
            pnl_pct=float(value["pnl_pct"]) if value.get("pnl_pct") is not None else None,
            reason=str(value.get("reason") or ""),
            stop_loss_pct=float(value["stop_loss_pct"]) if value.get("stop_loss_pct") is not None else None,
            avg_entry=float(value["avg_entry"]) if value.get("avg_entry") is not None else None,
            audit=dict(entry_audit) if isinstance(entry_audit, Mapping) else {},
            price_history=price_history,
            price_history_source=str(value.get("price_history_source") or ""),
            price_history_error=str(value.get("price_history_error")) if value.get("price_history_error") is not None else None,
        )
    
    def open_on_polymarket(self) -> None:
        S._log(f"Trade: Opening Polymarket URL: {self.market_url}", "DEBUG")
        webbrowser.open(self.market_url)
        return
    
    def get_prices(self) -> list[tuple[float, float]]:
        self.load_prices()
        return self.price_history
    
    def load_prices(self) -> None:
        from titan_prices import PRICES
        points, source, error = PRICES.get_prices(self.asset)
        self.price_history = points
        self.price_history_source = source
        self.price_history_error = error
        return


    
