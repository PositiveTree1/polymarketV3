"""
TITAN — Wallet scoring, HFT detection, and whale performance tracking. v10 OVERHAUL.

HFT DETECTION:
  Some elite wallets (e.g. swisstony) are high-frequency traders.
  They trade many small positions very rapidly. Traditional metrics
  (avg_bet, avg_profit_per_trade) unfairly penalise them.
  Wallet.is_hft() detects HFT behaviour and adjusts polling accordingly.

v9 ADDITIONS:
  1. SPORTS BOT DETECTION: Identifies wallets that predominantly trade
     sports markets. These wallets are market makers — their edge comes from
     speed and spread, not from prediction accuracy.

  2. WHALE PERFORMANCE TRACKER: Tracks which wallet' copied trades actually
     made us money. Auto-demotes wallets with consistently negative ROI.

  3. PER-TRADE ALPHA METRIC: alpha_per_trade = total_pnl / n_resolved.
     Wallets need alpha_per_trade >= $20 to be considered genuine alpha.

v10 ADDITIONS:
  4. RECENT FORM SCORING: recent_pnl_30d and recent_pnl_7d fields added to
     wallet cache. Computed from the /activity endpoint filtered by timestamp.
     Used by the Recent Form Copy strategy.

  5. get_wallet_open_positions(): Fetches current open positions for a wallet.
     Used by the Open Book consensus scanner.

  6. Wallet.is_recent_form_qualified(): Gate function for Recent Form Copy strategy.
"""

import time
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import IntEnum
from typing import TypedDict, Any, Mapping
import titan_state as S
import titan_config as C
from titan_config import *
import titan_db as DB


class WalletTier(IntEnum):
    ERROR    = -1
    REJECTED = 0
    WATCH    = 1
    VERIFIED = 2
    ELITE    = 3

    def display(self) -> str:
        return {
            WalletTier.ELITE:    "🔥ELITE",
            WalletTier.VERIFIED: "✅VER",
            WalletTier.WATCH:    "👁WATCH",
            WalletTier.REJECTED: "❌REJ",
            WalletTier.ERROR:    "⚠ERR",
        }[self]

    def __str__(self) -> str:
        return self.display()


def _status_from_dict(d: dict) -> "WalletTier":
    """Derive WalletTier from a DB/wire dict. Prefers 'status' key; falls back to legacy bools."""
    raw = d.get("status")
    if raw is not None:
        try:
            return WalletTier(int(raw))
        except (ValueError, KeyError):
            pass
    if d.get("elite"):    return WalletTier.ELITE
    if d.get("verified"): return WalletTier.VERIFIED
    if d.get("watchable"): return WalletTier.WATCH
    return WalletTier.REJECTED


@dataclass
class Wallet:
    # ── identity ──────────────────────────────────────────────────────────────
    addr:               str
    name:               str
    ts:                 float
    loaded_trade_count: int
    trade_load_limited: bool
    loaded_trade_pnl:   float
    first_loaded_trade_ts: float | None
    last_loaded_trade_ts:  float | None

    # ── scoring ───────────────────────────────────────────────────────────────
    score:              float
    win_rate:           float
    wilson_lb:          float
    alpha_per_trade:    float
    wr_source:          str
    winrate_trades_loaded:  int
    winrate_redeems_loaded: int

    # ── stats ─────────────────────────────────────────────────────────────────
    n_resolved:         int
    n_pos:              int
    total_value:        float
    total_pnl:          float
    pnl_pct:            float
    avg_pos_size:       float
    avg_profit:         float
    avg_bet:            float
    trades_per_hour:    float

    # ── flags ─────────────────────────────────────────────────────────────────
    status:             WalletTier
    hft:                bool
    vip:                bool
    sports_bot:         bool
    dead:               bool

    # ── recent form ───────────────────────────────────────────────────────────
    recent_pnl_30d:     float | None
    recent_pnl_7d:      float | None
    recent_ts:          float

    # ── leaderboard ───────────────────────────────────────────────────────────
    lb_rank:            int | None
    lb_vol:             float | None

    # ── debug ─────────────────────────────────────────────────────────────────
    detail:             str
    fail_reasons:       list[str] = field(default_factory=list)

    # ── stored sample metrics (Step 7) ────────────────────────────────────────
    stored_trade_count:    int = 0
    stored_last_trade_ts:  float | None = None
    stored_resolved_count: int = 0
    stored_realised_pnl:   float = 0.0
    quality_confidence:    float = 0.0
    data_quality:          str = "D"
    trimmed_roi:           float | None = None
    profit_factor:         float | None = None
    mtm_roi:               float | None = None
    median_24h_markout:    float | None = None
    positive_24h_markout_rate: float | None = None
    trd_top5_pnl_share:    float | None = None
    pos_top5_pnl_share:    float | None = None
    trd_median_roi:        float | None = None
    pos_median_roi:        float | None = None

    # ── in-memory trade cache (not persisted) ─────────────────────────────────
    trade_rows: "list[DB.WalletTradeRow]" = field(default_factory=list)
    pnl_series: "list[DB.RealisedPoint]" = field(default_factory=list)

    # ── methods ───────────────────────────────────────────────────────────────

    def is_hft(self) -> bool:
        if self.hft:
            return True
        if self.trades_per_hour >= HFT_MIN_TRADES_PER_HOUR:
            return True
        if self.avg_bet > 0 and self.avg_bet < 50 and self.n_resolved > 100:
            return True
        return False

    def is_sports_bot_wallet(self) -> bool:
        if self.sports_bot:
            return True
        try:
            import titan_config as _C
            sports_bot_tph = getattr(_C, "SPORTS_BOT_MIN_TPH", 150)
        except Exception:
            sports_bot_tph = 150
        if self.trades_per_hour >= sports_bot_tph:
            return True
        _SPORTS_BOT_NAMES = {
            "gamblingisallyouneed", "swisstony", "rn1", "cannae", "lilybaeum",
            "billdenter", "billdenter2026", "elkmonkey", "billyel", "sportsguy",
            "texaskid", "ferrarichampions", "ferrarichampions2026", "snakeball",
        }
        lname = self.name.lower()
        if any(sbn in lname for sbn in _SPORTS_BOT_NAMES):
            return True
        if self.trades_per_hour >= 50 and self.avg_bet > 0 and self.avg_bet < 100:
            return True
        return False

    def calc_alpha_per_trade(self) -> float:
        if self.n_resolved <= 0:
            return 0.0
        return self.total_pnl / self.n_resolved

    def is_recent_form_qualified(self,
                                  min_pnl_30d: float = 0,
                                  min_pnl_7d: float = -50,
                                  max_tph: float = 20) -> bool:
        if self.trades_per_hour > max_tph:
            return False
        if self.recent_pnl_30d is None or self.recent_pnl_7d is None:
            return False
        return self.recent_pnl_30d >= min_pnl_30d and self.recent_pnl_7d >= min_pnl_7d

    @property
    def is_active(self) -> bool:
        """Any tier worth processing: WATCH, VERIFIED, or ELITE."""
        return self.status.value > 0

    @property
    def is_watchable(self) -> bool:
        return self.status == WalletTier.WATCH

    @property
    def is_verified(self) -> bool:
        return self.status == WalletTier.VERIFIED

    @property
    def is_elite(self) -> bool:
        return self.status == WalletTier.ELITE

    @property
    def is_ranked(self) -> bool:
        """True for VERIFIED or ELITE — any tier above WATCH."""
        return self.status == WalletTier.VERIFIED or self.status == WalletTier.ELITE

    def tier(self) -> WalletTier:
        return self.status

    def tag(self) -> str:
        return self.status.display() + (" ⚡HFT" if self.hft else "")
        
    def reclassify(self, sel) -> "Wallet":
        from dataclasses import replace as _replace
        avg_profit = self.avg_profit
        avg_profit_estimated = False
        if avg_profit <= 0 and self.n_resolved >= 10 and self.total_pnl > 0:
            avg_profit = round((self.total_pnl * 0.5) / self.n_resolved, 2)
            avg_profit_estimated = True
        apt = self.loaded_trade_pnl / self.n_resolved if self.n_resolved > 0 else 0.0
        w = _replace(
            self,
            avg_profit=avg_profit,
            alpha_per_trade=round(apt, 2),
        ) if avg_profit != self.avg_profit or round(apt, 2) != self.alpha_per_trade else self
        score, status, hft, sports_bot, fail_reasons = w.apply_selector(sel)
        est_tag    = "~" if avg_profit_estimated else ""
        hft_tag    = " ⚡HFT" if hft else ""
        sports_tag = " 🏈SPORTS" if sports_bot else ""
        rf_tag     = f" RF30d:${self.recent_pnl_30d:+.0f}" if self.recent_pnl_30d is not None else ""
        return _replace(
            self,
            score=round(score, 5),
            avg_profit=avg_profit,
            alpha_per_trade=round(apt, 2),
            status=status,
            hft=hft,
            sports_bot=sports_bot,
            fail_reasons=fail_reasons,
            detail=(
                f"Score:{score:.2f} WR:{self.win_rate*100:.0f}% WilsonLB:{self.wilson_lb*100:.0f}% "
                f"Res:{self.n_resolved} Port:${self.total_value:,.0f} PnL:${self.total_pnl:+,.0f}({self.pnl_pct:+.1f}%) "
                f"AvgProfit:{est_tag}${avg_profit:.1f} AvgBet:${self.avg_bet:.0f} "
                f"AlphaPT:${apt:.1f} TPH:{self.trades_per_hour:.1f} [{self.wr_source}] "
                f"{status.display()}{hft_tag}{sports_tag}{rf_tag}"
            ),
        )

    def apply_selector(self, sel) -> "tuple[float, WalletTier, bool, bool, list[str]]":
        """Returns (score, status, hft, sports_bot, fail_reasons)."""
        from titan_selector import PerformanceSelector
        if sel is not None:
            score = sel.score(self)
            status, fail_reasons = sel.is_selected(self, score)
            hft        = sel.is_hft(self.trades_per_hour, self.avg_bet, self.n_resolved) if isinstance(sel, PerformanceSelector) else False
            sports_bot = sel.is_sports_bot(self.name, self.trades_per_hour) if isinstance(sel, PerformanceSelector) else False
        else:
            wb = self.wilson_lb
            use_pos = C.USE_POSITIONS_API
            w_wilson = 0.30 + (0 if use_pos else 0.25 + 0.15 + 0.10)
            score = (
                w_wilson * wb +
                (0.25 * min(1.0, max(0, self.pnl_pct / 30))   if use_pos else 0.0) +
                (0.15 * min(1.0, self.total_value / 25_000)    if use_pos else 0.0) +
                0.10 * min(1.0, self.n_resolved / 20) +
                (0.10 * min(1.0, self.n_pos / 10)              if use_pos else 0.0) +
                0.10 * min(1.0, max(0, self.avg_profit) / 50)
            )
            fail_reasons = []
            watchable_ok = self.win_rate >= 0.53 and wb >= 0.45 and self.n_resolved >= 10 and self.total_pnl >= 0
            verified_ok  = watchable_ok and self.win_rate >= 0.56 and wb >= 0.49
            if verified_ok:   status = WalletTier.VERIFIED
            elif watchable_ok: status = WalletTier.WATCH
            else:              status = WalletTier.REJECTED
            hft        = C.HFT_ENABLED and self.trades_per_hour >= HFT_MIN_TRADES_PER_HOUR
            sports_bot = False
        return score, status, hft, sports_bot, fail_reasons

    def to_wire(self) -> dict[str, Any]:
        """Produce the API wire dict (TrackedWalletDict shape). Only call at JSON boundary."""
        return {
            "wallet":               self.addr,
            "name":                 self.name,
            "ts":                   self.ts,
            "loaded_trade_count":   self.loaded_trade_count,
            "loaded_trade_pnl":     self.loaded_trade_pnl,
            "trade_load_limited":   self.trade_load_limited,
            "first_loaded_trade_ts": self.first_loaded_trade_ts,
            "last_loaded_trade_ts":  self.last_loaded_trade_ts,
            "score":                self.score,
            "win_rate":             self.win_rate,
            "wilson_lb":            self.wilson_lb,
            "alpha_per_trade":      self.alpha_per_trade,
            "wr_source":            self.wr_source,
            "winrate_trades_loaded":  self.winrate_trades_loaded,
            "winrate_redeems_loaded": self.winrate_redeems_loaded,
            "n_resolved":           self.n_resolved,
            "n_pos":                self.n_pos,
            "total_value":          self.total_value,
            "total_pnl":            self.total_pnl,
            "pnl_pct":              self.pnl_pct,
            "avg_pos_size":         self.avg_pos_size,
            "avg_profit":           self.avg_profit,
            "avg_bet":              self.avg_bet,
            "trades_per_hour":      self.trades_per_hour,
            "status":               int(self.status),
            "hft":                  self.hft,
            "vip":                  self.vip,
            "sports_bot":           self.sports_bot,
            "dead":                 self.dead,
            "recent_pnl_30d":       self.recent_pnl_30d,
            "recent_pnl_7d":        self.recent_pnl_7d,
            "recent_ts":            self.recent_ts,
            "lb_rank":              self.lb_rank,
            "lb_vol":               self.lb_vol,
            "detail":               self.detail,
            "fail_reasons":         self.fail_reasons,
            "stored_trade_count":   self.stored_trade_count,
            "stored_last_trade_ts": self.stored_last_trade_ts,
            "stored_resolved_count": self.stored_resolved_count,
            "stored_realised_pnl":  self.stored_realised_pnl,
            "quality_confidence":   self.quality_confidence,
            "data_quality":         self.data_quality,
            "trimmed_roi":          self.trimmed_roi,
            "profit_factor":        self.profit_factor,
            "mtm_roi":              self.mtm_roi,
            "median_24h_markout":   self.median_24h_markout,
            "positive_24h_markout_rate": self.positive_24h_markout_rate,
            "trd_top5_pnl_share":   self.trd_top5_pnl_share,
            "pos_top5_pnl_share":   self.pos_top5_pnl_share,
            "trd_median_roi":       self.trd_median_roi,
            "pos_median_roi":       self.pos_median_roi,
            "pnl_series":           [[p.close_ts, p.realised_pnl] for p in self.pnl_series],
        }

    def to_db_dict(self) -> dict[str, Any]:
        """Produce the dict stored as JSON in SQLite. Identical shape to to_wire() minus addr (stored as column)."""
        d = self.to_wire()
        d.pop("wallet", None)
        return d

    @classmethod
    def from_db(cls, addr: str, d: dict[str, Any]) -> "Wallet":
        """Reconstruct a Wallet from a DB JSON blob."""
        return cls(
            addr=addr,
            name=str(d.get("name") or addr[:10] + "…"),
            ts=float(d.get("ts") or 0.0),
            loaded_trade_count=int(d.get("loaded_trade_count") or 0),
            trade_load_limited=bool(d.get("trade_load_limited") or False),
            loaded_trade_pnl=float(d.get("loaded_trade_pnl") or 0.0),
            first_loaded_trade_ts=d.get("first_loaded_trade_ts"),
            last_loaded_trade_ts=d.get("last_loaded_trade_ts"),
            score=float(d.get("score") or 0.10),
            win_rate=float(d.get("win_rate") or 0.0),
            wilson_lb=float(d.get("wilson_lb") or 0.0),
            alpha_per_trade=float(d.get("alpha_per_trade") or 0.0),
            wr_source=str(d.get("wr_source") or "none"),
            winrate_trades_loaded=int(d.get("winrate_trades_loaded") or 0),
            winrate_redeems_loaded=int(d.get("winrate_redeems_loaded") or 0),
            n_resolved=int(d.get("n_resolved") or 0),
            n_pos=int(d.get("n_pos") or 0),
            total_value=float(d.get("total_value") or 0.0),
            total_pnl=float(d.get("total_pnl") or 0.0),
            pnl_pct=float(d.get("pnl_pct") or 0.0),
            avg_pos_size=float(d.get("avg_pos_size") or 0.0),
            avg_profit=float(d.get("avg_profit") or 0.0),
            avg_bet=float(d.get("avg_bet") or 0.0),
            trades_per_hour=float(d.get("trades_per_hour") or 0.0),
            status=_status_from_dict(d),
            hft=bool(d.get("hft") or False),
            vip=bool(d.get("vip") or False),
            sports_bot=bool(d.get("sports_bot") or False),
            dead=bool(d.get("dead") or False),
            recent_pnl_30d=d.get("recent_pnl_30d"),
            recent_pnl_7d=d.get("recent_pnl_7d"),
            recent_ts=float(d.get("recent_ts") or 0.0),
            lb_rank=d.get("lb_rank"),
            lb_vol=d.get("lb_vol"),
            detail=str(d.get("detail") or ""),
            fail_reasons=list(d.get("fail_reasons") or []),
            stored_trade_count=int(d.get("stored_trade_count") or 0),
            stored_last_trade_ts=d.get("stored_last_trade_ts"),
            stored_resolved_count=int(d.get("stored_resolved_count") or 0),
            stored_realised_pnl=float(d.get("stored_realised_pnl") or 0.0),
            quality_confidence=float(d.get("quality_confidence") or 0.0),
            data_quality=str(d.get("data_quality") or "D"),
            trimmed_roi=d.get("trimmed_roi"),
            profit_factor=d.get("profit_factor"),
            mtm_roi=d.get("mtm_roi"),
            median_24h_markout=d.get("median_24h_markout"),
            positive_24h_markout_rate=d.get("positive_24h_markout_rate"),
            trd_top5_pnl_share=d.get("trd_top5_pnl_share") or d.get("top_5_pnl_share"),
            pos_top5_pnl_share=d.get("pos_top5_pnl_share"),
            trd_median_roi=d.get("trd_median_roi"),
            pos_median_roi=d.get("pos_median_roi"),
            pnl_series=[DB.RealisedPoint(close_ts=float(r[0]), realised_pnl=float(r[1]))
                        for r in (d.get("pnl_series") or []) if len(r) == 2],
        )

    @classmethod
    def make_stub(cls, addr: str, detail: str, *, status: WalletTier = WalletTier.WATCH) -> "Wallet":
        is_vip = addr.lower() in {a.lower() for a in C.VIP_WALLETS}
        return cls(
            addr=addr,
            name=addr[:10] + "…",
            ts=0.0,
            loaded_trade_count=0,
            trade_load_limited=False,
            loaded_trade_pnl=0.0,
            first_loaded_trade_ts=None,
            last_loaded_trade_ts=None,
            score=0.10,
            win_rate=0.0,
            wilson_lb=0.0,
            alpha_per_trade=0.0,
            wr_source="none",
            winrate_trades_loaded=0,
            winrate_redeems_loaded=0,
            n_resolved=0,
            n_pos=0,
            total_value=0.0,
            total_pnl=0.0,
            pnl_pct=0.0,
            avg_pos_size=0.0,
            avg_profit=0.0,
            avg_bet=0.0,
            trades_per_hour=0.0,
            status=status,
            hft=False,
            vip=is_vip,
            sports_bot=False,
            dead=False,
            recent_pnl_30d=None,
            recent_pnl_7d=None,
            recent_ts=0.0,
            lb_rank=None,
            lb_vol=None,
            detail=detail,
            fail_reasons=[],
        )


class WhalePerformanceRecord(TypedDict):
    wins:           int
    losses:         int
    total_pnl:      float
    n_trades:       int
    recent_trades:  list[tuple[float, float]]


class WhalePerformanceSummary(TypedDict):
    wallet:         str
    name:           str
    n_trades:       int
    wins:           int
    losses:         int
    win_rate:       float
    total_pnl:      float
    avg_pnl:        float
    weekly_pnl:     float
    weekly_trades:  int


class WalletOpenPosition(TypedDict):
    cid:        str
    outcome:    str
    asset:      str
    cur:        float
    size:       float


class WinRateData(TypedDict):
    wins:               int
    losses:             int
    total:              int
    loaded_trade_count: int
    loaded_trade_pnl:     float
    first_loaded_trade_ts: float | None
    last_loaded_trade_ts:  float | None
    trade_load_limited: bool
    win_rate:           float
    wilson_lb:          float
    source:             str
    avg_profit:         float
    avg_bet:            float
    trades_per_hour:    float
    recent_pnl_30d:     float | None
    recent_pnl_7d:      float | None
    winrate_trades_loaded:  int
    winrate_redeems_loaded: int
    pnl_series:             "list[DB.RealisedPoint]"
    pos_top5_pnl_share:     "float | None"
    pos_median_roi:         "float | None"


@dataclass
class RawTrade:
    condition_id: str
    asset:        str
    side:         str
    size:         float
    price:        float
    cash:         float
    timestamp:    float
    outcome:      str
    title:        str
    source:       str          # trades | activity
    slug:         str = ""
    event_slug:   str = ""


@dataclass
class TradeClosure:
    condition_id: str
    asset:        str
    side:         str
    close_type:   str          # REDEEM | SELL
    close_ts:     float
    close_price:  float | None
    close_cash:   float
    realised_pnl: float | None


@dataclass
class WalletQualityMetrics:
    resolved_positions:         int
    open_positions:             int
    median_position_roi:        float | None
    trimmed_mean_position_roi:  float | None
    money_weighted_roi:         float | None
    position_weighted_roi:      float | None
    profit_factor:              float | None
    wilson_winrate_lb:          float
    realised_win_rate:          float
    mtm_roi:                    float | None
    open_mtm_pnl:               float
    median_24h_markout:         float | None
    positive_24h_markout_rate:  float | None
    trd_top5_pnl_share:         float | None
    trd_median_roi:             float | None
    profitable_rolling_50_rate: float | None
    sample_quality_factor:      float
    concentration_factor:       float
    open_risk_factor:           float
    data_truncation_factor:     float
    confidence:                 float
    data_quality:               str


def compute_wallet_quality_metrics(
    rows: "list[DB.WalletTradeRow]",
    trade_load_limited: bool = False,
) -> WalletQualityMetrics:

    resolved  = [r for r in rows if r.status in ("REDEEMED", "SOLD") and r.realised_pnl is not None]
    open_rows = [r for r in rows if r.status == "OPEN"]

    n_resolved_with_pnl = len(resolved)
    n_resolved = sum(1 for r in rows if r.status in ("REDEEMED", "SOLD"))
    n_open = len(open_rows)

    # ── per-position ROI ──────────────────────────────────────────────────────
    pos_rois: list[float] = []
    gross_profit = 0.0
    gross_loss   = 0.0
    wins = 0
    for r in resolved:
        cost = r.entry_cash
        if cost <= 0.0:
            continue
        pnl  = r.realised_pnl  # type: ignore[assignment]
        roi  = pnl / cost
        pos_rois.append(roi)
        if pnl > 0:
            gross_profit += pnl
            wins += 1
        else:
            gross_loss += abs(pnl)

    median_roi: float | None = None
    trimmed_roi: float | None = None
    if pos_rois:
        sorted_rois = sorted(pos_rois)
        mid = len(sorted_rois) // 2
        median_roi = sorted_rois[mid] if len(sorted_rois) % 2 else (sorted_rois[mid - 1] + sorted_rois[mid]) / 2
        trim_n = max(1, len(sorted_rois) // 10)
        trimmed = sorted_rois[trim_n:-trim_n] if len(sorted_rois) > 2 * trim_n else sorted_rois
        trimmed_roi = sum(trimmed) / len(trimmed) if trimmed else None

    # ── win rate / Wilson ─────────────────────────────────────────────────────
    realised_wr = wins / n_resolved_with_pnl if n_resolved_with_pnl > 0 else 0.0
    wilson_lb   = wilson_lower_bound(wins, n_resolved_with_pnl)

    # ── profit factor ─────────────────────────────────────────────────────────
    profit_factor: float | None = None
    if gross_loss > 0:
        profit_factor = round(gross_profit / gross_loss, 4)
    elif gross_profit > 0:
        profit_factor = 10.0

    # ── money-weighted / position-weighted ROI ────────────────────────────────
    total_resolved_cost = sum(r.entry_cash for r in resolved if r.entry_cash > 0)
    total_resolved_pnl  = sum(r.realised_pnl for r in resolved if r.realised_pnl is not None)  # type: ignore[misc]
    money_weighted_roi: float | None = (
        total_resolved_pnl / total_resolved_cost if total_resolved_cost > 0 else None
    )
    position_weighted_roi: float | None = (
        sum(pos_rois) / len(pos_rois) if pos_rois else None
    )

    # ── MTM: realised + open unrealised ──────────────────────────────────────
    open_mtm_pnl = 0.0
    for r in open_rows:
        cur_price = r.cur_price
        if cur_price is not None and r.entry_cash > 0:
            cost      = r.entry_cash
            cur_value = r.entry_size * cur_price
            open_mtm_pnl += cur_value - cost
    total_cost = sum(r.entry_cash for r in rows if r.entry_cash > 0)
    mtm_pnl    = total_resolved_pnl + open_mtm_pnl
    mtm_roi: float | None = mtm_pnl / total_cost if total_cost > 0 else None

    # ── concentration: top-5 market PnL share ────────────────────────────────
    # Use all closed rows (REDEEMED/SOLD) with entry_cash > 0; fall back to
    # redeem_value/close_cash when realised_pnl was not stored by the activity feed.
    all_closed = [r for r in rows if r.status in ("REDEEMED", "SOLD") and r.entry_cash > 0 and (r.realised_pnl is not None or r.redeem_value or r.close_cash)]
    market_pnl: dict[str, float] = {}
    for r in all_closed:
        pnl = r.realised_pnl if r.realised_pnl is not None else (r.redeem_value or r.close_cash or 0.0) - r.entry_cash
        market_pnl[r.condition_id] = market_pnl.get(r.condition_id, 0.0) + pnl
    trd_top5_pnl_share: float | None = None
    if market_pnl:
        total_mkt_pnl = sum(market_pnl.values())
        if total_mkt_pnl != 0.0:
            top5 = sorted(market_pnl.values(), reverse=True)[:5]
            trd_top5_pnl_share = sum(top5) / abs(total_mkt_pnl)

    # ── rolling-50 win rate ───────────────────────────────────────────────────
    profitable_rolling_50_rate: float | None = None
    if n_resolved_with_pnl >= 50:
        wins_50 = sum(1 for r in resolved[-50:] if (r.realised_pnl or 0) > 0)
        profitable_rolling_50_rate = wins_50 / 50.0

    # ── confidence: data completeness ─────────────────────────────────────────
    # Three independent completeness signals, all must be satisfied:
    # 1. We have realised PnL data (closures backfilled) — without it metrics are meaningless
    # 2. We have enough resolved trades to be statistically meaningful (>=30)
    # 3. We fetched the full history (not truncated by page cap)
    concentration_factor   = 1.0
    open_risk_factor       = 1.0
    data_truncation_factor = 1.0
    sample_quality_factor  = 1.0

    if trade_load_limited:
        confidence = round(min(0.6, n_resolved / max(n_resolved * 2, 1)), 4)
    elif n_resolved >= 30:
        confidence = 1.0
    else:
        confidence = round(n_resolved / 30, 4)

    if n_resolved >= 200:
        data_quality = "A"
    elif n_resolved >= 80:
        data_quality = "B"
    elif n_resolved >= 30:
        data_quality = "C"
    else:
        data_quality = "D"

    return WalletQualityMetrics(
        resolved_positions=n_resolved,
        open_positions=n_open,
        median_position_roi=round(median_roi, 6) if median_roi is not None else None,
        trimmed_mean_position_roi=round(trimmed_roi, 6) if trimmed_roi is not None else None,
        money_weighted_roi=round(money_weighted_roi, 6) if money_weighted_roi is not None else None,
        position_weighted_roi=round(position_weighted_roi, 6) if position_weighted_roi is not None else None,
        profit_factor=profit_factor,
        wilson_winrate_lb=round(wilson_lb, 6),
        realised_win_rate=round(realised_wr, 6),
        mtm_roi=round(mtm_roi, 6) if mtm_roi is not None else None,
        open_mtm_pnl=round(open_mtm_pnl, 4),
        median_24h_markout=None,
        positive_24h_markout_rate=None,
        trd_top5_pnl_share=round(trd_top5_pnl_share, 4) if trd_top5_pnl_share is not None else None,
        trd_median_roi=round(median_roi, 6) if median_roi is not None else None,
        profitable_rolling_50_rate=round(profitable_rolling_50_rate, 4) if profitable_rolling_50_rate is not None else None,
        sample_quality_factor=sample_quality_factor,
        concentration_factor=concentration_factor,
        open_risk_factor=open_risk_factor,
        data_truncation_factor=data_truncation_factor,
        confidence=confidence,
        data_quality=data_quality,
    )


def _is_auto_wallet_name(name: str) -> bool:
    n = name.strip()
    if not n:
        return True
    if n.lower().startswith("0x"):
        return True
    if n.endswith("\u2026"):
        return True
    parts = n.split("-")
    if len(parts) != 2:
        return False
    a, b = parts
    return a and b and a[0].isupper() and b[0].isupper() and a.isalpha() and b.isalpha()


def _payload_rows(data: object) -> list[Mapping[str, object]]:
    rows_raw: object = data
    if isinstance(data, Mapping):
        rows_raw = data.get("data") or data.get("items") or data.get("results") or []
    if not isinstance(rows_raw, list):
        return []
    return [row for row in rows_raw if isinstance(row, Mapping)]


def _name_from_payload(row: Mapping[str, object]) -> str:
    for key in ("name", "pseudonym", "username", "displayName", "profileName"):
        raw = row.get(key)
        if isinstance(raw, str):
            candidate = raw.strip()
            if candidate and not _is_auto_wallet_name(candidate):
                return candidate
    user_raw = row.get("user")
    if isinstance(user_raw, Mapping):
        return _name_from_payload(user_raw)
    return ""


def resolve_wallet_display_name(wallet: str) -> str:
    endpoints = [
        (f"{C.DATA_API}/trades", {"user": wallet, "limit": 10}),
        (f"{C.DATA_API}/activity", {"user": wallet, "limit": 20}),
    ]
    for url, params in endpoints:
        try:
            data = S.safe_get(url, params, retries=1, timeout=8, quiet=True)
        except Exception as exc:
            S._log(f"Wallet name lookup failed for {wallet[:12]}... via {url}: {exc}", "WARN")
            continue
        for row in _payload_rows(data):
            name = _name_from_payload(row)
            if name:
                return name
    return ""


def wilson_lower_bound(wins: int, total: int, z: float = 1.96) -> float:
    if total == 0:
        return 0.0
    p  = wins / total
    n  = total
    lb = (p + z*z/(2*n) - z * math.sqrt((p*(1-p) + z*z/(4*n))/n)) / (1 + z*z/n)
    return max(0.0, lb)




# ─────────────────────────────────────────────────────────────────────────────
#  WALLET PERFORMANCE TRACKER
# ─────────────────────────────────────────────────────────────────────────────
_whale_performance: dict[str, WhalePerformanceRecord] = {}

# Set while reclassify_all() is running so _rescore_watchlist() yields immediately.
import threading as _threading
_reclassify_in_progress = _threading.Event()


def record_whale_trade_performance(wallet_addrs: list[str], pnl_usdc: float, won: bool) -> None:
    """
    Record the outcome of a copied trade for the whale(s) that sourced it.
    Called when a position is closed. Tracks 7-day rolling window for recency.
    """
    now_t = time.time()
    week_ago = now_t - 7 * 86400

    for w in wallet_addrs:
        w = w.lower()
        if w not in _whale_performance:
            _whale_performance[w] = {
                "wins": 0, "losses": 0, "total_pnl": 0.0, "n_trades": 0,
                "recent_trades": [],
            }
        rec = _whale_performance[w]
        rec["n_trades"] += 1
        rec["total_pnl"] += pnl_usdc
        if won:
            rec["wins"] += 1
        else:
            rec["losses"] += 1

        rec.setdefault("recent_trades", []).append((now_t, pnl_usdc))
        rec["recent_trades"] = [(ts, p) for ts, p in rec["recent_trades"] if ts >= week_ago]


def get_wallet_weekly_pnl(wallet: str) -> float:
    """Return the 7-day rolling PnL for a whale from our copy-trades."""
    rec = _whale_performance.get(wallet.lower())
    if not rec:
        return 0.0
    week_ago = time.time() - 7 * 86400
    recent = rec.get("recent_trades", [])
    return sum(p for ts, p in recent if ts >= week_ago)


def get_wallet_performance_summary() -> list[WhalePerformanceSummary]:
    """
    Return a sorted summary of all whale performance records.
    Sorted by total PnL (worst first for easy identification of bad sources).
    """
    summary = []
    week_ago = time.time() - 7 * 86400
    for w, rec in _whale_performance.items():
        cached = S.env().wallet_cache.get(w)
        name = cached.name if cached is not None else w[:10] + "…"
        wr = rec["wins"] / rec["n_trades"] if rec["n_trades"] > 0 else 0
        recent = rec.get("recent_trades", [])
        weekly_pnl = sum(p for ts, p in recent if ts >= week_ago)
        weekly_trades = sum(1 for ts, _ in recent if ts >= week_ago)
        summary.append({
            "wallet": w,
            "name": name,
            "n_trades": rec["n_trades"],
            "wins": rec["wins"],
            "losses": rec["losses"],
            "win_rate": round(wr, 2),
            "total_pnl": round(rec["total_pnl"], 4),
            "avg_pnl": round(rec["total_pnl"] / max(rec["n_trades"], 1), 4),
            "weekly_pnl": round(weekly_pnl, 4),
            "weekly_trades": weekly_trades,
        })
    return sorted(summary, key=lambda x: x["total_pnl"])


# ─────────────────────────────────────────────────────────────────────────────
#  v10: RECENT FORM FUNCTIONS
# ─────────────────────────────────────────────────────────────────────────────
def get_wallet_open_positions(wallet: str) -> list[WalletOpenPosition]:
    """
    Fetch current open positions for a wallet.
    Returns list of {cid, outcome, asset, cur_price, size} dicts.
    Used by Open Book consensus scanner.
    Results are NOT cached (need live data for consensus accuracy).
    """
    data = S.safe_get(f"{C.DATA_API}/positions", {
        "user":          wallet,
        "limit":         100,
        "sortBy":        "CURRENT",
        "sortDirection": "DESC",
        "sizeThreshold": 0.1,
    })
    if not data or not isinstance(data, list):
        return []
    results = []
    for p in data:
        cur = float(p.get("curPrice", 0) or 0)
        if cur <= 0.02 or cur >= 0.98:
            continue  # resolving/resolved — not useful for consensus
        results.append({
            "cid":     p.get("conditionId") or "",
            "outcome": p.get("outcome") or "",
            "asset":   p.get("asset") or "",
            "cur":     cur,
            "size":    float(p.get("size", 0) or 0),
        })
    return results



# ─────────────────────────────────────────────────────────────────────────────
#  INCREMENTAL TRADE PAGINATORS
# ─────────────────────────────────────────────────────────────────────────────

def _raw_trade_from_api_row(
    r: Mapping[str, object],
    condition_id: str,
    asset: str,
    side: str,
    ts: float,
) -> RawTrade:
    size  = float(r.get("size") or 0.0)   # type: ignore[arg-type]
    price = float(r.get("price") or 0.0)  # type: ignore[arg-type]
    cash  = float(r.get("usdcSize") or 0.0) or size * price  # type: ignore[arg-type]
    return RawTrade(
        condition_id=condition_id, asset=asset, side=side,
        size=size, price=price, cash=cash, timestamp=ts,
        outcome=str(r.get("outcome") or ""),
        title=str(r.get("title") or ""),
        source="trades",
        slug=str(r.get("slug") or ""),
        event_slug=str(r.get("eventSlug") or ""),
    )


def fetch_wallet_trades_incremental(
    wallet: str,
    newest_known_ts: float | None,
    backfill_oldest_ts: float | None,
    refresh_ok_until_ts: float | None,
    refresh_page_size: int = 0,
    max_pages: int | None = None,
) -> tuple[list[RawTrade], float | None, float | None]:
    """
    Returns (new_trades, new_backfill_oldest_ts, new_refresh_ok_until_ts).

    REFRESH (refresh_ok_until_ts is not None):
      Paginates all pages, stops at newest_known_ts watermark per page.
      Covers gaps from long server downtime correctly.
      Returns (trades, None, now) on success — caller should persist new_refresh_ok_until_ts.

    BACKFILL (refresh_ok_until_ts is None):
      Paginates all pages. Skips pages whose full range is already stored.
      Returns (trades, oldest_ts_reached, now_if_finished_else_None).
      Caller persists new_backfill_oldest_ts and new_refresh_ok_until_ts.

    max_pages: cap total pages fetched (shallow fetch for WATCH stage). None = unlimited.
    """
    results: list[RawTrade] = []
    seen: set[tuple[str, str, str, float]] = set()
    prev_ids: set[str] = set()
    new_oldest_ts: float | None = None
    now_ts = time.time()
    pages_fetched = 0

    def _page(offset: int, page_size: int) -> list | None:
        data = S.safe_get(f"{C.DATA_API}/trades", {
            "user": wallet, "limit": page_size, "offset": offset,
        })
        if data is None:
            return None
        if isinstance(data, dict):
            data = data.get("data") or []
        if not isinstance(data, list):
            return []
        return data

    def _fingerprints(data: list) -> set[str]:
        return {
            f"{r.get('conditionId')}|{r.get('asset')}|{r.get('side')}|{r.get('timestamp')}"
            for r in data
        }

    if refresh_ok_until_ts is not None:
        # REFRESH MODE: use estimated page size, keep paging until watermark hit
        ps = max(10, min(refresh_page_size, C.TRADES_LIMIT)) if refresh_page_size > 0 else C.TRADES_LIMIT
        prev_fps: set[str] = set()
        success = False
        for offset in range(0, C.TRADES_MAX_OFFSET + 1, ps):
            if max_pages is not None and pages_fetched >= max_pages:
                break
            data = _page(offset, ps)
            pages_fetched += 1
            if data is None or not data:
                break
            fps = _fingerprints(data)
            if fps and fps == prev_fps:
                break
            prev_fps = fps
            watermark_hit = False
            for r in data:
                ts = float(r.get("timestamp") or 0.0)
                if ts <= 0.0:
                    continue
                if newest_known_ts is not None and ts <= newest_known_ts:
                    watermark_hit = True
                    break
                key = (str(r.get("conditionId") or ""), str(r.get("asset") or ""), str(r.get("side") or ""), ts)
                if key in seen:
                    continue
                seen.add(key)
                results.append(_raw_trade_from_api_row(r, key[0], key[1], key[2], ts))
            if watermark_hit or len(data) < ps:
                success = True
                break
        return results, None, now_ts if success else None

    # BACKFILL MODE: paginate all pages, skip already-covered pages
    backfill_finished = False
    prev_fps2: set[str] = set()
    for offset in range(0, C.TRADES_MAX_OFFSET + 1, C.TRADES_LIMIT):
        if max_pages is not None and pages_fetched >= max_pages:
            break
        data = _page(offset, C.TRADES_LIMIT)
        pages_fetched += 1
        if data is None:
            S._log(f"fetch_wallet_trades_incremental: API error at offset {offset} for {wallet[:14]}", "WARN")
            break
        if not data:
            backfill_finished = True
            break
        fps = _fingerprints(data)
        if fps and fps == prev_fps2:
            backfill_finished = True
            break
        prev_fps2 = fps

        page_min_ts = min(
            (float(r.get("timestamp") or 0.0) for r in data if r.get("timestamp")),
            default=0.0,
        )

        # Skip this page entirely if it falls above what we already stored
        if backfill_oldest_ts is not None and page_min_ts > backfill_oldest_ts:
            if len(data) < C.TRADES_LIMIT:
                backfill_finished = True
                break
            continue

        for r in data:
            ts = float(r.get("timestamp") or 0.0)
            if ts <= 0.0:
                continue
            key = (str(r.get("conditionId") or ""), str(r.get("asset") or ""), str(r.get("side") or ""), ts)
            if key in seen:
                continue
            seen.add(key)
            if new_oldest_ts is None or ts < new_oldest_ts:
                new_oldest_ts = ts
            results.append(_raw_trade_from_api_row(r, key[0], key[1], key[2], ts))

        if len(data) < C.TRADES_LIMIT:
            backfill_finished = True
            break
    else:
        backfill_finished = True  # exhausted all available offsets

    return results, new_oldest_ts, now_ts if backfill_finished else None


def fetch_wallet_activity_closures_incremental(
    wallet: str,
    newest_activity_ts: float | None,
) -> list[TradeClosure]:
    """
    Always paginates all available pages. Stops within a page once newest_activity_ts watermark is hit.
    Works correctly for both first load and after a long server downtime.
    """
    results: list[TradeClosure] = []
    seen: set[tuple[str, str, str, float]] = set()
    prev_ids: set[str] = set()

    for offset in range(0, C.ACTIVITY_MAX_OFFSET + 1, C.ACTIVITY_LIMIT):
        data = S.safe_get(f"{C.DATA_API}/activity", {
            "user": wallet, "type": "REDEEM",
            "limit": C.ACTIVITY_LIMIT, "offset": offset,
            "sortBy": "TIMESTAMP", "sortDirection": "DESC",
        })
        if data is None:
            S._log(f"fetch_wallet_activity_closures_incremental: API error at offset {offset} for {wallet[:14]}", "WARN")
            break
        if isinstance(data, dict):
            data = data.get("data") or []
        if not isinstance(data, list) or not data:
            break

        page_ids = {str(r.get("id") or "") for r in data}
        if page_ids and page_ids == prev_ids:
            break
        prev_ids = page_ids

        watermark_hit = False
        for r in data:
            ts = float(r.get("timestamp") or 0.0)
            if ts <= 0.0:
                continue
            if newest_activity_ts is not None and ts <= newest_activity_ts:
                watermark_hit = True
                break
            cid   = str(r.get("conditionId") or "")
            asset = str(r.get("asset") or "")
            side  = str(r.get("side") or "BUY")
            key   = (cid, asset, side, ts)
            if key in seen:
                continue
            seen.add(key)
            close_cash = float(r.get("usdcSize") or 0.0)
            entry_cash = float(r.get("size") or 0.0) * float(r.get("price") or 0.0)
            realised: float | None = None
            if entry_cash > 0.0:
                realised = close_cash - entry_cash
            results.append(TradeClosure(
                condition_id=cid, asset=asset, side=side,
                close_type="REDEEM", close_ts=ts,
                close_price=None, close_cash=close_cash, realised_pnl=realised,
            ))

        if watermark_hit or len(data) < C.ACTIVITY_LIMIT:
            break

    return results


# ─────────────────────────────────────────────────────────────────────────────
#  WIN RATE CALCULATION
# ─────────────────────────────────────────────────────────────────────────────

def load_and_refresh_wallet_trades(
    wallet_obj: "Wallet",
    positions_raw: list | None = None,
    with_closures: bool = False,
) -> WinRateData:
    """
    Single entry point for trade data + win-rate computation.

    1. Load trade_rows from DB if not already in memory.
    2. Calculate how many new trades to fetch: tph × gap_hours × 1.5, rounded up.
       - If refresh_ok_until_ts is None (never fetched): full backfill mode.
       - If gap < 15 min: skip API call entirely, use cached rows.
       - Otherwise: one targeted page fetch in ~90% of cases.
    3. Fetch new trades, upsert to DB, extend trade_rows in memory.
    4. If with_closures=True: fetch new REDEEM activity since last known closure,
       apply to trade rows so realised_pnl is up to date.
    5. Compute and return WinRateData from the full trade_rows.
    """
    import math
    addr   = wallet_obj.addr
    now_ts = time.time()

    # ── Step 1: load rows from DB once ────────────────────────────────────────
    if not wallet_obj.trade_rows:
        wallet_obj.trade_rows = DB.load_wallet_trade_rows(addr)
    rows = wallet_obj.trade_rows

    # ── Step 2: decide page count ─────────────────────────────────────────────
    backfill_oldest_ts, refresh_ok_until_ts = DB.get_wallet_fetch_state(addr)
    newest_known_ts: float | None = max((r.entry_ts for r in rows if r.entry_ts > 0), default=None)

    if refresh_ok_until_ts is None:
        # First time ever — full backfill, no page cap
        page_size = C.TRADES_LIMIT
        smart_max_pages = None
    else:
        gap_hours = (now_ts - refresh_ok_until_ts) / 3600.0
        if gap_hours < 0.25:
            # Data is fresh — skip API call
            return _compute_winrate_from_rows(rows, positions_raw)
        tph = wallet_obj.trades_per_hour or 0.0
        if tph > 0:
            needed = math.ceil(tph * gap_hours * 1.5)
            page_size  = min(max(10, needed), C.TRADES_LIMIT)
            smart_max_pages = math.ceil(needed / page_size)
        else:
            page_size = C.TRADES_LIMIT
            smart_max_pages = 1

    # ── Step 3: fetch new trades ──────────────────────────────────────────────
    new_trades, new_oldest_ts, new_refresh_ts = fetch_wallet_trades_incremental(
        addr, newest_known_ts, backfill_oldest_ts, refresh_ok_until_ts, page_size,
        max_pages=smart_max_pages,
    )
    if new_trades:
        DB.upsert_wallet_trades(addr, new_trades)
        wallet_obj.trade_rows = DB.load_wallet_trade_rows(addr)
    resolved_oldest = new_oldest_ts if new_oldest_ts is not None else backfill_oldest_ts
    DB.update_wallet_fetch_state(addr, resolved_oldest, new_refresh_ts)

    # ── Step 4: fetch closures (REDEEM activity) if requested ─────────────────
    if with_closures:
        last_activity_ts = DB.get_wallet_last_activity_ts(addr)
        closures = fetch_wallet_activity_closures_incremental(addr, last_activity_ts)
        if closures:
            DB.apply_wallet_trade_closures(addr, closures)
            wallet_obj.trade_rows = DB.load_wallet_trade_rows(addr)

    return _compute_winrate_from_rows(wallet_obj.trade_rows, positions_raw)


def _compute_winrate_from_rows(
    rows: "list[DB.WalletTradeRow]",
    positions_raw: list | None,
) -> WinRateData:
    now_t   = time.time()
    days_30 = now_t - 30 * 86400
    days_7  = now_t - 7  * 86400

    buy_rows      = [r for r in rows if r.side == "BUY"]
    redeemed_rows = [r for r in rows if r.status in ("REDEEMED", "SOLD")]
    open_rows     = [r for r in rows if r.status == "OPEN"]

    loaded_trade_count    = len(buy_rows)
    entry_ts_list         = [r.entry_ts for r in buy_rows if r.entry_ts > 0]
    first_loaded_trade_ts = min(entry_ts_list) if entry_ts_list else None
    last_loaded_trade_ts  = max(entry_ts_list) if entry_ts_list else None

    trades_per_hour = 0.0
    if len(entry_ts_list) >= 10:
        ts_sorted  = sorted(entry_ts_list, reverse=True)
        span_hours = (ts_sorted[0] - ts_sorted[-1]) / 3600
        if span_hours > 0:
            trades_per_hour = len(ts_sorted) / span_hours

    total_redeem_value   = sum(r.redeem_value or r.close_cash or 0.0 for r in redeemed_rows)
    resolved_entry_cash  = sum(r.entry_cash for r in redeemed_rows)
    total_entry_cash     = sum(r.entry_cash for r in buy_rows)
    # PnL only over closed positions — open buy costs must not be subtracted
    loaded_trade_pnl     = total_redeem_value - resolved_entry_cash

    recent_pnl_30d: float | None = None
    recent_pnl_7d:  float | None = None
    if first_loaded_trade_ts is not None and first_loaded_trade_ts <= days_30:
        red_30d = [r for r in redeemed_rows if (r.close_ts or 0) >= days_30]
        recent_pnl_30d = (
            sum(r.redeem_value or r.close_cash or 0.0 for r in red_30d) -
            sum(r.entry_cash for r in red_30d)
        )
    if first_loaded_trade_ts is not None and first_loaded_trade_ts <= days_7:
        red_7d = [r for r in redeemed_rows if (r.close_ts or 0) >= days_7]
        recent_pnl_7d = (
            sum(r.redeem_value or r.close_cash or 0.0 for r in red_7d) -
            sum(r.entry_cash for r in red_7d)
        )

    redeem_keys: set[tuple[str, str]] = {(r.condition_id, r.asset) for r in redeemed_rows}
    n_redeems = len(redeemed_rows)

    if positions_raw is not None:
        current_price_by_pos: dict[tuple[str, str], dict] = {
            (p.get("conditionId") or "", p.get("asset") or ""): {
                "cur":        float(p.get("curPrice", 0.5) or 0.5),
                "redeemable": bool(p.get("redeemable", False)),
                "cashPnl":    float(p.get("cashPnl", 0) or 0),
            }
            for p in positions_raw
            if p.get("conditionId") or p.get("asset")
        }
        n_open_known = len(positions_raw)
    else:
        current_price_by_pos = {
            (r.condition_id, r.asset): {
                "cur":        r.cur_price or 0.5,
                "redeemable": r.redeemable,
                "cashPnl":    r.cash_pnl or 0.0,
            }
            for r in open_rows
            if r.cur_price is not None
        }
        n_open_known = len(open_rows)

    lost_positions: set[tuple[str, str]] = set()
    for r in open_rows:
        pos_key = (r.condition_id, r.asset)
        if pos_key in redeem_keys:
            continue
        pos = current_price_by_pos.get(pos_key)
        if pos:
            cur_p    = pos["cur"]
            cash_pnl = pos["cashPnl"]
            if cur_p <= 0.02:
                lost_positions.add(pos_key)
            elif pos["redeemable"] and cash_pnl < 0:
                lost_positions.add(pos_key)
            elif cash_pnl < -1.0 and cur_p < 0.10:
                lost_positions.add(pos_key)

    wins   = len(redeem_keys)
    losses = len(lost_positions)
    total  = wins + losses

    lost_entry_cash = sum(r.entry_cash for r in open_rows if (r.condition_id, r.asset) in lost_positions)

    avg_bet    = total_entry_cash / loaded_trade_count if loaded_trade_count > 0 else 0.0
    avg_profit = round((total_redeem_value - resolved_entry_cash - lost_entry_cash) / total, 2) if total > 0 else -1

    # pnl_series: one point per closed trade row (REDEEMED/SOLD).
    # Rows missing close value are treated as full loss (-entry_cash).
    # Inferred lost open positions are appended at -entry_cash using entry_ts as close_ts.
    # n_resolved = total closed points so it always matches len(pnl_series).
    redeemed_series: list[DB.RealisedPoint] = []
    for r in redeemed_rows:
        close_ts = r.close_ts or r.entry_ts
        pnl = (r.redeem_value or r.close_cash or 0.0) - r.entry_cash if r.entry_cash > 0 else 0.0
        redeemed_series.append(DB.RealisedPoint(close_ts=close_ts, realised_pnl=pnl))
    lost_series: list[DB.RealisedPoint] = [
        DB.RealisedPoint(close_ts=r.entry_ts, realised_pnl=-r.entry_cash)
        for r in open_rows
        if (r.condition_id, r.asset) in lost_positions and r.entry_cash > 0
    ]
    series = sorted(redeemed_series + lost_series, key=lambda p: p.close_ts)
    n_resolved_closed = len(series)

    pos_top5_pnl_share: float | None = None
    pos_median_roi: float | None = None
    if series:
        pos_market_pnl: dict[str, float] = {}
        for r in redeemed_rows:
            pnl = (r.redeem_value or r.close_cash or 0.0) - r.entry_cash if r.entry_cash > 0 else 0.0
            pos_market_pnl[r.condition_id] = pos_market_pnl.get(r.condition_id, 0.0) + pnl
        for r in open_rows:
            if (r.condition_id, r.asset) in lost_positions and r.entry_cash > 0:
                pos_market_pnl[r.condition_id] = pos_market_pnl.get(r.condition_id, 0.0) - r.entry_cash
        total_pos_pnl = sum(pos_market_pnl.values())
        if total_pos_pnl != 0.0:
            top5_vals = sorted(pos_market_pnl.values(), reverse=True)[:5]
            pos_top5_pnl_share = round(sum(top5_vals) / abs(total_pos_pnl), 4)
    pos_rois: list[float] = [
        ((r.redeem_value or r.close_cash or 0.0) - r.entry_cash) / r.entry_cash
        for r in redeemed_rows if r.entry_cash > 0
    ] + [
        -1.0
        for r in open_rows if (r.condition_id, r.asset) in lost_positions and r.entry_cash > 0
    ]
    if pos_rois:
        s = sorted(pos_rois)
        mid = len(s) // 2
        pos_median_roi = round(s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2, 6)

    def _result(w: int, l: int, t: int, src: str) -> WinRateData:
        wr = w / t if t > 0 else 0.0
        wb = wilson_lower_bound(w, t)
        return {
            "wins": w, "losses": l, "total": n_resolved_closed,
            "loaded_trade_count": loaded_trade_count,
            "trade_load_limited": False,
            "loaded_trade_pnl":   round(loaded_trade_pnl, 2),
            "first_loaded_trade_ts": first_loaded_trade_ts,
            "last_loaded_trade_ts":  last_loaded_trade_ts,
            "win_rate": round(wr, 4), "wilson_lb": round(wb, 4),
            "source": src,
            "avg_profit": avg_profit, "avg_bet": round(avg_bet, 2),
            "trades_per_hour": round(trades_per_hour, 2),
            "recent_pnl_30d": round(recent_pnl_30d, 2) if recent_pnl_30d is not None else None,
            "recent_pnl_7d":  round(recent_pnl_7d,  2) if recent_pnl_7d  is not None else None,
            "winrate_trades_loaded":  loaded_trade_count,
            "winrate_redeems_loaded": n_redeems,
            "pnl_series": series,
            "pos_top5_pnl_share": pos_top5_pnl_share,
            "pos_median_roi": pos_median_roi,
        }

    if total == 0 and wins > 0 and n_open_known > 0:
        return _result(wins, n_open_known, wins + n_open_known, "redeem_window_fallback")

    if total == 0:
        open_wins = sum(1 for p in current_price_by_pos.values() if p["cashPnl"] > 0)
        wr_open   = open_wins / n_open_known if n_open_known > 0 else 0.0
        wb        = wilson_lower_bound(open_wins, n_open_known)
        return {
            **_result(open_wins, n_open_known - open_wins, n_open_known, "open_positions_proxy"),
            "win_rate": wr_open,
            "wilson_lb": wb * 0.5,
        }

    return _result(wins, losses, total, "redeems+inferred_losses")


# ─────────────────────────────────────────────────────────────────────────────
#  SELECTOR SCORING — single place that applies selector logic to a Wallet
# ─────────────────────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────────────────────
#  WALLET SCORING
# ─────────────────────────────────────────────────────────────────────────────
def _log_wallet_change(before: Wallet | None, after: Wallet, fail_reasons: list[str], name: str, addr: str) -> None:
    tier_before = before.tier() if before is not None else None
    tier_after  = after.tier()
    hft_changed = (before.hft if before is not None else False) != after.hft
    if tier_before == tier_after and not hft_changed:
        return
    tag_before  = before.tag() if before is not None else "NEW"
    tag_after   = after.tag()
    stats_str   = f"PnL=${after.total_pnl:+,.0f} Port=${after.total_value:,.0f} Score={after.score:.2f} WR={after.win_rate*100:.0f}% Res={after.n_resolved}"
    reasons_str = ", ".join(fail_reasons) if fail_reasons else ""
    if tier_before != tier_after:
        promoted = tier_after > tier_before if tier_before is not None else True
        if promoted:
            msg, level = f"⬆ {tag_before}→{tag_after} {name} ({addr[:12]}…) | {stats_str}", "INFO"
        else:
            is_empty_stub = after.score == 0.0 and after.n_resolved == 0 and after.total_pnl == 0.0
            level = "DIAG" if is_empty_stub else "WARN"
            msg = f"⬇ {tag_before}→{tag_after} {name} ({addr[:12]}…) | {reasons_str} | {stats_str}"
    else:
        msg, level = f"~ {tag_before}→{tag_after} {name} ({addr[:12]}…) | {stats_str}", "INFO"
    S._log(f"[WALLET] {msg}", level, terminal=True)


def get_compute_and_store_wallet(
    wallet: str,
    lb_row: "dict | None" = None,
    force_refresh: bool = False,
) -> Wallet:
    """
    Classify and cache a wallet using a 3-stage cost model:

    Stage 1 — NEW→WATCH:
        Only leaderboard data (lb_row). No positions call, no trade fetch.
        If lb_row is absent (wallet not on leaderboard), create a WATCH stub
        and defer deeper evaluation to the next cycle (stage 2).

    Stage 2 — WATCH→VERIFIED:
        Positions call + trade fetch (smart page count from tph × gap_hours).
        Uses the full VERIFIED gate: win_rate, wilson_lb, resolved bets, portfolio/PnL.

    Stage 3 — VERIFIED/ELITE: closure fetch + deep quality analysis.
        Trade rows already loaded in stage 2; gap=0 so no extra API call.
    """
    from dataclasses import replace as _replace

    wallet   = wallet.lower()
    now_t    = time.time()
    is_vip   = wallet in {addr.lower() for addr in C.VIP_WALLETS}
    vip_name = C.VIP_WALLET_NAMES.get(wallet, "")
    cached        = S.env().wallet_cache.get(wallet)
    cached_origin = cached  # preserved for _log_wallet_change throughout all stages

    if not force_refresh and cached is not None and (now_t - cached.ts) < WALLET_TTL:
        return cached

    existing_name    = cached.name if cached is not None else ""
    existing_is_real = bool(existing_name) and not _is_auto_wallet_name(existing_name)
    keep_name        = existing_name if existing_is_real else vip_name

    sel = C.get_active_selector()
    if sel is None:
        raise RuntimeError("get_compute_and_store_wallet requires an active selector, but none is configured.")

    # ── Extract leaderboard data ──────────────────────────────────────────────
    # lb_row may be passed in from a bulk discovery call (free); if not, fetch it.
    if lb_row is None:
        lb_data = S.safe_get(f"{C.DATA_API}/v1/leaderboard", {"user": wallet, "timePeriod": "ALL"})
        lb_row  = lb_data[0] if lb_data and isinstance(lb_data, list) else None

    lb_pnl:  float | None = None
    lb_vol:  float | None = None
    lb_rank: int   | None = None
    if lb_row:
        if not keep_name:
            lb_name = str(lb_row.get("userName") or "").strip()
            if lb_name and not _is_auto_wallet_name(lb_name):
                keep_name = lb_name
        if lb_row.get("pnl") is not None:
            lb_pnl = float(lb_row["pnl"])
        try:
            lb_rank = int(lb_row["rank"]) if lb_row.get("rank") is not None else None
        except (ValueError, TypeError):
            lb_rank = None
        if lb_row.get("vol") is not None:
            lb_vol = float(lb_row["vol"])

    final_name = existing_name if existing_is_real else (keep_name or wallet[:10] + "…")

    # ══════════════════════════════════════════════════════════════════════════
    # STAGE 1 — leaderboard gate: runs for every wallet on every cycle
    # ══════════════════════════════════════════════════════════════════════════
    watch_ok, fail_reasons = sel.is_watch_eligible(lb_pnl, lb_vol, lb_rank)
    if not watch_ok and not is_vip:
        stub = Wallet.make_stub(wallet, f"lb_pnl=${lb_pnl:+,.0f}" if lb_pnl is not None else "no_lb_data",
                                status=WalletTier.REJECTED)
        stub.ts        = now_t
        stub.name      = final_name
        stub.vip       = is_vip
        stub.lb_rank   = lb_rank
        stub.lb_vol    = lb_vol
        stub.fail_reasons = fail_reasons
        S.env().wallet_cache[wallet] = stub
        return stub

    # Pass stage 1 — build a WATCH stub and fall through to stage 2 immediately
    stub = Wallet.make_stub(wallet, "lb_watch_pending", status=WalletTier.WATCH)
    stub.ts        = now_t
    stub.name      = final_name
    stub.vip       = is_vip
    stub.lb_rank   = lb_rank
    stub.lb_vol    = lb_vol
    stub.total_pnl = lb_pnl if lb_pnl is not None else 0.0
    stub.fail_reasons = []
    cached = stub

    # ══════════════════════════════════════════════════════════════════════════
    # STAGE 2 — positions (optional) + shallow trade fetch → VERIFIED gate
    # ══════════════════════════════════════════════════════════════════════════
    if not keep_name:
        keep_name = resolve_wallet_display_name(wallet)
    final_name = existing_name if existing_is_real else (keep_name or wallet[:10] + "…")

    if C.USE_POSITIONS_API:
        _syn = DB.get_wallet_synthetic_position(wallet)
        _pos_age   = now_t - _syn[0] if _syn else float("inf")
        _pos_stale = _pos_age > WALLET_TTL / 2

        if _pos_stale:
            pos_data = S.safe_get(f"{C.DATA_API}/positions", {
                "user": wallet, "limit": 500,
                "sortBy": "CASHPNL", "sortDirection": "DESC",
            })
            if pos_data is None or not isinstance(pos_data, list):
                cached.ts = now_t - WALLET_TTL + 60
                S.env().wallet_cache[wallet] = cached
                return cached
            DB.update_wallet_positions(wallet, pos_data)
            n_pos = len(pos_data)
            init  = sum(float(p.get("initialValue") or 0) for p in pos_data)
            cur   = sum(float(p.get("currentValue") or 0) for p in pos_data)
        else:
            pos_data = None
            _, n_pos, init, cur, _ = _syn  # type: ignore[misc]
    else:
        pos_data = None
        n_pos    = 0
        init     = 0.0
        cur      = 0.0

    total_pnl = lb_pnl if lb_pnl is not None else 0.0
    pct    = total_pnl / init * 100 if init > 0 else 0
    avg_sz = init / n_pos if n_pos > 0 else 0

    _trade_carrier = cached
    wr_data = load_and_refresh_wallet_trades(_trade_carrier, pos_data)

    _draft = Wallet(
        addr=wallet, name=final_name, ts=now_t,
        loaded_trade_count=wr_data["loaded_trade_count"],
        trade_load_limited=wr_data["trade_load_limited"],
        loaded_trade_pnl=wr_data["loaded_trade_pnl"],
        first_loaded_trade_ts=wr_data["first_loaded_trade_ts"],
        last_loaded_trade_ts=wr_data["last_loaded_trade_ts"],
        score=0.0, win_rate=wr_data["win_rate"], wilson_lb=wr_data["wilson_lb"],
        alpha_per_trade=0.0, wr_source=wr_data["source"],
        winrate_trades_loaded=wr_data["winrate_trades_loaded"],
        winrate_redeems_loaded=wr_data["winrate_redeems_loaded"],
        n_resolved=wr_data["total"], n_pos=n_pos,
        total_value=cur, total_pnl=total_pnl, pnl_pct=pct, avg_pos_size=avg_sz,
        avg_profit=wr_data["avg_profit"], avg_bet=wr_data["avg_bet"],
        trades_per_hour=round(wr_data["trades_per_hour"], 2),
        status=WalletTier.WATCH, hft=False, vip=is_vip, sports_bot=False, dead=False,
        recent_pnl_30d=wr_data["recent_pnl_30d"],
        recent_pnl_7d=wr_data["recent_pnl_7d"],
        recent_ts=now_t, lb_rank=lb_rank, lb_vol=lb_vol, detail="", fail_reasons=[],
        pnl_series=wr_data["pnl_series"],
        pos_top5_pnl_share=wr_data["pos_top5_pnl_share"],
        pos_median_roi=wr_data["pos_median_roi"],
    )
    result = _draft.reclassify(sel)
    _log_wallet_change(cached_origin, result, result.fail_reasons, final_name, wallet)
    _changed = any(
        getattr(result, k) != getattr(cached_origin, k)
        for k in ("status", "score", "win_rate", "wilson_lb", "total_pnl", "name",
                   "hft", "loaded_trade_count", "ts")
    ) if cached_origin is not None else True
    S.env().wallet_cache[wallet] = result
    if result.is_active and _changed:
        DB.upsert_wallet_profile(wallet, result)
    elif not result.is_active and cached.is_active:
        DB.clear_wallet_profile(wallet)
    if not result.is_ranked:
        return result
    # Wallet reached VERIFIED or ELITE — fall through to stage 3 for full backfill
    cached = result

    # ══════════════════════════════════════════════════════════════════════════
    # STAGE 3 — VERIFIED/ELITE: full fetch + deep analysis
    # ══════════════════════════════════════════════════════════════════════════
    if not keep_name:
        keep_name = resolve_wallet_display_name(wallet)
    final_name = existing_name if existing_is_real else (keep_name or wallet[:10] + "…")

    if C.USE_POSITIONS_API:
        _syn = DB.get_wallet_synthetic_position(wallet)
        _pos_age   = now_t - _syn[0] if _syn else float("inf")
        _pos_stale = _pos_age > WALLET_TTL / 2

        if _pos_stale:
            pos_data = S.safe_get(f"{C.DATA_API}/positions", {
                "user": wallet, "limit": 500,
                "sortBy": "CASHPNL", "sortDirection": "DESC",
            })
            if pos_data is None or not isinstance(pos_data, list):
                if cached is not None:
                    cached.ts = now_t - WALLET_TTL + 60
                    S.env().wallet_cache[wallet] = cached
                    return cached
                null = Wallet.make_stub(wallet, "No data", status=WalletTier.ERROR)
                null.ts   = now_t
                null.name = final_name
                null.vip  = is_vip
                null.fail_reasons = ["no_data"]
                S.env().wallet_cache[wallet] = null
                return null
            DB.update_wallet_positions(wallet, pos_data)
            n_pos = len(pos_data)
            init  = sum(float(p.get("initialValue") or 0) for p in pos_data)
            cur   = sum(float(p.get("currentValue") or 0) for p in pos_data)
        else:
            pos_data = None
            _, n_pos, init, cur, _ = _syn  # type: ignore[misc]
    else:
        pos_data = None
        n_pos    = 0
        init     = 0.0
        cur      = 0.0

    total_pnl = lb_pnl if lb_pnl is not None else 0.0
    pct    = total_pnl / init * 100 if init > 0 else 0
    avg_sz = init / n_pos if n_pos > 0 else 0

    _trade_carrier = cached if cached is not None else Wallet.make_stub(wallet, "")
    wr_data = load_and_refresh_wallet_trades(_trade_carrier, pos_data, with_closures=True)

    trade_rows     = _trade_carrier.trade_rows
    total_stored   = len(trade_rows)
    total_resolved = sum(1 for r in trade_rows if r.status in ("REDEEMED", "SOLD"))

    _draft = Wallet(
        addr=wallet, name=final_name, ts=now_t,
        loaded_trade_count=wr_data["loaded_trade_count"],
        trade_load_limited=wr_data["trade_load_limited"],
        loaded_trade_pnl=wr_data["loaded_trade_pnl"],
        first_loaded_trade_ts=float(wr_data["first_loaded_trade_ts"]) if wr_data["first_loaded_trade_ts"] is not None else None,
        last_loaded_trade_ts=float(wr_data["last_loaded_trade_ts"]) if wr_data["last_loaded_trade_ts"] is not None else None,
        score=0.0, win_rate=wr_data["win_rate"], wilson_lb=wr_data["wilson_lb"], alpha_per_trade=0.0,
        wr_source=wr_data["source"],
        winrate_trades_loaded=wr_data["winrate_trades_loaded"],
        winrate_redeems_loaded=wr_data["winrate_redeems_loaded"],
        n_resolved=wr_data["total"], n_pos=n_pos, total_value=cur, total_pnl=total_pnl, pnl_pct=pct,
        avg_pos_size=avg_sz, avg_profit=wr_data["avg_profit"], avg_bet=wr_data["avg_bet"],
        trades_per_hour=round(wr_data["trades_per_hour"], 2),
        status=WalletTier.REJECTED, hft=False, vip=is_vip, sports_bot=False, dead=False,
        recent_pnl_30d=round(wr_data["recent_pnl_30d"], 2) if wr_data["recent_pnl_30d"] is not None else None,
        recent_pnl_7d=round(wr_data["recent_pnl_7d"], 2) if wr_data["recent_pnl_7d"] is not None else None,
        recent_ts=now_t, lb_rank=lb_rank, lb_vol=lb_vol, detail="", fail_reasons=[],
        pnl_series=wr_data["pnl_series"],
        pos_top5_pnl_share=wr_data["pos_top5_pnl_share"],
        pos_median_roi=wr_data["pos_median_roi"],
    )
    result = _draft.reclassify(sel)

    if result.is_ranked:
        try:
            qm         = compute_wallet_quality_metrics(trade_rows, trade_load_limited=result.trade_load_limited)
            stored_pnl = DB.get_wallet_realised_pnl(wallet)
            last_trade_ts_stored = max((r.entry_ts for r in trade_rows if r.entry_ts > 0), default=None)
            result = _replace(
                result,
                trade_rows=trade_rows,
                stored_trade_count=total_stored,
                stored_last_trade_ts=last_trade_ts_stored,
                stored_resolved_count=total_resolved,
                stored_realised_pnl=round(stored_pnl, 4),
                quality_confidence=qm.confidence,
                data_quality=qm.data_quality,
                trimmed_roi=qm.trimmed_mean_position_roi,
                profit_factor=qm.profit_factor,
                mtm_roi=qm.mtm_roi,
                median_24h_markout=qm.median_24h_markout,
                positive_24h_markout_rate=qm.positive_24h_markout_rate,
                trd_top5_pnl_share=qm.trd_top5_pnl_share,
                trd_median_roi=qm.trd_median_roi,
            )

            _, refresh_ok_until_ts = DB.get_wallet_fetch_state(wallet)
            bf_tag = f"✓refresh@{time.strftime('%H:%M', time.localtime(refresh_ok_until_ts))}" if refresh_ok_until_ts else "⬇backfill"
            S._log(
                f"wallet_trades {result.tag()} {result.name} [{bf_tag}] | "
                f"wr_input: {result.winrate_trades_loaded} trades + {result.winrate_redeems_loaded} redeems | "
                f"db: {total_stored} total / {total_resolved} resolved",
                "DATA",
            )
        except Exception as _e:
            S._log(f"wallet_trades persistence failed for {wallet[:14]}: {_e}", "WARN")

        # Deep-analysis ELITE gate: always runs, even if trade fetch failed (confidence=0 will demote)
        if result.is_elite:
            from titan_selector import PerformanceSelector as _PS
            if isinstance(sel, _PS):
                deep_ok, deep_reasons = sel.is_elite_deep_ok(result)
                if not deep_ok:
                    result = _replace(result, status=WalletTier.VERIFIED,
                                      fail_reasons=result.fail_reasons + [f"DEEP_GATE: {', '.join(deep_reasons)}"])
                    S._log(f"[WALLET] ⬇ ELITE→VER {result.name} ({wallet[:12]}…) deep gate failed: {deep_reasons}", "WARN", terminal=True)

    _log_wallet_change(cached_origin, result, result.fail_reasons, final_name, wallet)

    _changed = cached is None or any(
        getattr(result, k) != getattr(cached, k)
        for k in ("status", "score", "win_rate", "wilson_lb",
                  "total_pnl", "name", "hft", "vip", "sports_bot", "dead", "recent_pnl_30d", "recent_pnl_7d",
                  "loaded_trade_count", "trade_load_limited", "first_loaded_trade_ts", "last_loaded_trade_ts", "ts",
                  "stored_trade_count", "stored_resolved_count", "stored_realised_pnl",
                  "quality_confidence", "data_quality", "trimmed_roi", "profit_factor", "mtm_roi")
    )
    S.env().wallet_cache[wallet] = result
    if result.is_active and _changed:
        DB.upsert_wallet_profile(wallet, result)
    elif not result.is_active and cached is not None and cached.is_active:
        DB.clear_wallet_profile(wallet)
    return result


# ─────────────────────────────────────────────────────────────────────────────
#  WalletsCache — in-memory, client-safe  (mirrors PricesCache pattern)
# ─────────────────────────────────────────────────────────────────────────────

class WalletsCache:
    """Thin dict wrapper for the wallet roster. Client-safe: no DB or API calls."""

    def __init__(self) -> None:
        self._data: dict[str, Wallet] = {}

    # ── dict protocol ─────────────────────────────────────────────────────────

    def __getitem__(self, addr: str) -> Wallet:
        return self._data[addr]

    def __setitem__(self, addr: str, wallet: Wallet) -> None:
        self._data[addr] = wallet

    def __contains__(self, addr: object) -> bool:
        return addr in self._data

    def __len__(self) -> int:
        return len(self._data)

    def get(self, addr: str, default: Wallet | None = None) -> Wallet | None:
        return self._data.get(addr, default)

    def items(self):
        return self._data.items()

    def values(self):
        return self._data.values()

    def keys(self):
        return self._data.keys()

    # ── queries ───────────────────────────────────────────────────────────────

    def get_elite(self) -> list[str]:
        return [addr for addr, w in self._data.items() if w.is_elite]

    def get_watchlist(self) -> list[str]:
        return [addr for addr, w in self._data.items() if w.is_watchable]

    def ingest(self, addr: str, wallet: Wallet) -> None:
        """Store a wallet from wire data. Client-side entry point — no DB write."""
        self._data[addr] = wallet


class WalletsCacheSrv(WalletsCache):
    """Server-side wallet cache: adds DB persistence and selector reclassification."""

    def __setitem__(self, addr: str, wallet: Wallet) -> None:
        self._data[addr] = wallet

    def reclassify_all(self) -> int:
        """Re-run selector scoring on every cached wallet. No Poly API calls. Returns count updated."""
        import titan_config as _C
        import titan_state as _S
        _S.log_important(f"♻ reclassify_all called — {len(self._data)} wallets, HFT_ENABLED={_C.HFT_ENABLED}")
        _reclassify_in_progress.set()
        try:
            return self._reclassify_all_inner(_C)
        finally:
            _reclassify_in_progress.clear()

    def _reclassify_all_inner(self, _C) -> int:
        sel = _C.get_active_selector()
        updated = 0
        for addr, w in list(self._data.items()):
            result = w.reclassify(sel)
            if w.tier() != result.tier() or w.hft != result.hft:
                updated += 1
                _log_wallet_change(w, result, result.fail_reasons, w.name, addr)

            self._data[addr] = result
            if result.is_active:
                DB.upsert_wallet_profile(addr, result)
            elif not result.is_active and w.is_active:
                DB.clear_wallet_profile(addr)

        S.log_important(f"🎯 reclassify_all done: {updated} wallet(s) changed out of {len(self._data)}")
        return updated

    def refresh_recent_form(self) -> None:
        """Refresh recent_pnl_30d / recent_pnl_7d for stale verified wallets. No classification."""
        now_t = time.time()
        stale_threshold = now_t - 6 * 3600
        refreshed = 0
        for addr, profile in list(self._data.items()):
            if not profile.is_ranked:
                continue
            if profile.recent_ts >= stale_threshold:
                continue
            try:
                wr_data = load_and_refresh_wallet_trades(profile)
                profile.recent_pnl_30d = wr_data["recent_pnl_30d"]
                profile.recent_pnl_7d  = wr_data["recent_pnl_7d"]
                profile.recent_ts      = now_t
                self._data[addr] = profile
                DB.upsert_wallet_profile(addr, profile)
                refreshed += 1
                time.sleep(0.12)
            except Exception as e:
                S._log(f"Recent form refresh failed for {addr}: {e}", "ERR")
        if refreshed:
            S._log(f"♻ Recent form refreshed for {refreshed} wallets", "DATA")


# ─────────────────────────────────────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────────────────────────────────────
def get_elite_wallets() -> list[str]:
    return S.env().wallet_cache.get_elite()


def _refresh_recent_form_scores() -> None:
    cache = S.env().wallet_cache
    if isinstance(cache, WalletsCacheSrv):
        cache.refresh_recent_form()


# Discover candidate wallets from the active selector, score/cache each new
# wallet, add watchable ones to the watchlist, then prune the watchlist to
# keep verified wallets first within the size cap.
def discover_new_wallets() -> None:
    S._log("🔍 Running wallet discovery…", "DATA")

    import titan_config as _C
    sel = _C.get_active_selector()
    if sel is None:
        raise RuntimeError("Wallet discovery requires an active selector, but none is configured.")
    candidates = set(sel.discover())

    current_watchlist = set(S.get_watchlist())
    new_cands = candidates - current_watchlist
    S._log(f"🔍 {len(candidates)} candidates, {len(new_cands)} new", "DATA")

    discovered = 0
    for w in list(new_cands)[:25]:
        prof = get_compute_and_store_wallet(w)
        if prof.is_ranked:
            discovered += 1
            tag = prof.tag()
            S._log(
                f"🆕 {tag} {w[:14]}… "
                f"Score:{prof.score:.2f} WR:{prof.win_rate*100:.0f}% "
                f"PnL:${prof.total_pnl:+,.0f} TPH:{prof.trades_per_hour:.1f}",
                "INFO"
            )
        time.sleep(0.12)

    wl = S.get_watchlist()
    if len(wl) > MAX_WATCHLIST_SIZE:
        import titan_db as DB
        verified_set = {w for w in wl if (p := S.env().wallet_cache.get(w)) and p.is_ranked}
        unverified   = [w for w in wl if w not in verified_set]
        keep_unver   = max(0, MAX_WATCHLIST_SIZE - len(verified_set))
        for w in unverified[keep_unver:]:
            from dataclasses import replace as _r
            S.env().wallet_cache[w] = _r(S.env().wallet_cache[w], status=WalletTier.REJECTED)
            DB.set_watchable(w, False)
        S._log(f"🧹 Watchlist pruned to {MAX_WATCHLIST_SIZE} ({len(unverified[keep_unver:])} toggled off)", "DATA")

    S._log(f"🔍 Discovery done — {discovered} new. Watchlist: {len(S.get_watchlist())}", "DATA")


def scan_top_market_holders() -> None:
    S._log("🔍 Scanning top market holders…", "DATA")
    try:
        data = S.safe_get(f"{C.GAMMA_API}/markets", {"limit": 100, "active": "true"})
        if not data or not isinstance(data, list):
            return
        markets    = sorted(data, key=lambda x: float(x.get("volume") or 0), reverse=True)[:20]
        candidates = set()
        for m in markets:
            cid = m.get("conditionId")
            if not cid:
                continue
            trades = S.safe_get(f"{C.DATA_API}/trades", {
                "conditionId": cid, "limit": 50,
                "filterType": "CASH", "side": "BUY", "filterAmount": 500,
            })
            if trades and isinstance(trades, list):
                for t in trades:
                    w = (t.get("proxyWallet") or "").lower()
                    if w and w.startswith("0x") and len(w) == 42:
                        candidates.add(w)
            time.sleep(0.08)
        new_cands = candidates - set(S.get_watchlist())
        added = 0
        for w in list(new_cands)[:20]:
            prof = get_compute_and_store_wallet(w)
            if prof.is_active:
                added += 1
                if prof.is_ranked:
                    tag = prof.tag()
                    S._log(f"🆕 {tag} from market scan: {w[:14]}…", "INFO")
            time.sleep(0.12)
        S._log(f"🔍 Market scan done — {added} added", "DATA")
    except Exception as e:
        S._log(f"⚠ Market scan failed: {e}", "WARN")
