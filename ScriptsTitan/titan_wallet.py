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
from typing import TypedDict, Any
import titan_state as S
import titan_config as C
from titan_config import *
import titan_db as DB


class WalletTier(IntEnum):
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
        }[self]
    
    def __str__(self) -> str:
        return self.display()


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
    verified:           bool
    watchable:          bool
    elite:              bool
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

    def tier(self) -> WalletTier:
        if self.elite:     return WalletTier.ELITE
        if self.verified:  return WalletTier.VERIFIED
        if self.watchable: return WalletTier.WATCH
        return WalletTier.REJECTED

    def tag(self) -> str:
        return self.tier().display() + (" ⚡HFT" if self.hft else "")
        
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
        score, watchable, verified, elite, hft, sports_bot, fail_reasons = w.apply_selector(sel)
        est_tag    = "~" if avg_profit_estimated else ""
        hft_tag    = " ⚡HFT" if hft else ""
        sports_tag = " 🏈SPORTS" if sports_bot else ""
        rf_tag     = f" RF30d:${self.recent_pnl_30d:+.0f}" if self.recent_pnl_30d is not None else ""
        tier_disp  = (WalletTier.ELITE if elite else WalletTier.VERIFIED if verified else WalletTier.WATCH if watchable else WalletTier.REJECTED).display()
        return _replace(
            self,
            score=round(score, 5),
            avg_profit=avg_profit,
            alpha_per_trade=round(apt, 2),
            verified=verified,
            watchable=watchable,
            elite=elite,
            hft=hft,
            sports_bot=sports_bot,
            fail_reasons=fail_reasons,
            detail=(
                f"Score:{score:.2f} WR:{self.win_rate*100:.0f}% WilsonLB:{self.wilson_lb*100:.0f}% "
                f"Res:{self.n_resolved} Port:${self.total_value:,.0f} PnL:${self.total_pnl:+,.0f}({self.pnl_pct:+.1f}%) "
                f"AvgProfit:{est_tag}${avg_profit:.1f} AvgBet:${self.avg_bet:.0f} "
                f"AlphaPT:${apt:.1f} TPH:{self.trades_per_hour:.1f} [{self.wr_source}] "
                f"{tier_disp}{hft_tag}{sports_tag}{rf_tag}"
            ),
        )

    def apply_selector(self, sel) -> "tuple[float, bool, bool, bool, bool, bool, list[str]]":
        """Returns (score, watchable, verified, elite, hft, sports_bot, fail_reasons)."""
        from titan_selector import PerformanceSelector
        if sel is not None:
            score = sel.score(self)
            watchable, verified, elite, fail_reasons = sel.is_selected(self, score)
            hft        = sel.is_hft(self.trades_per_hour, self.avg_bet, self.n_resolved) if isinstance(sel, PerformanceSelector) else False
            sports_bot = sel.is_sports_bot(self.name, self.trades_per_hour) if isinstance(sel, PerformanceSelector) else False
        else:
            wb = self.wilson_lb
            score = (
                0.30 * wb +
                0.25 * min(1.0, max(0, self.pnl_pct / 30)) +
                0.15 * min(1.0, self.total_value / 25_000) +
                0.10 * min(1.0, self.n_resolved / 20) +
                0.10 * min(1.0, self.n_pos / 10) +
                0.10 * min(1.0, max(0, self.avg_profit) / 50)
            )
            fail_reasons = []
            watchable    = self.win_rate >= 0.53 and wb >= 0.45 and self.n_resolved >= 10 and self.total_pnl >= 0
            verified     = watchable and self.win_rate >= 0.56 and wb >= 0.49
            elite        = False
            hft          = C.HFT_ENABLED and self.trades_per_hour >= HFT_MIN_TRADES_PER_HOUR
            sports_bot   = False
        return score, watchable, verified, elite, hft, sports_bot, fail_reasons

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
            "n_resolved":           self.n_resolved,
            "n_pos":                self.n_pos,
            "total_value":          self.total_value,
            "total_pnl":            self.total_pnl,
            "pnl_pct":              self.pnl_pct,
            "avg_pos_size":         self.avg_pos_size,
            "avg_profit":           self.avg_profit,
            "avg_bet":              self.avg_bet,
            "trades_per_hour":      self.trades_per_hour,
            "verified":             self.verified,
            "watchable":            self.watchable,
            "elite":                self.elite,
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
            n_resolved=int(d.get("n_resolved") or 0),
            n_pos=int(d.get("n_pos") or 0),
            total_value=float(d.get("total_value") or 0.0),
            total_pnl=float(d.get("total_pnl") or 0.0),
            pnl_pct=float(d.get("pnl_pct") or 0.0),
            avg_pos_size=float(d.get("avg_pos_size") or 0.0),
            avg_profit=float(d.get("avg_profit") or 0.0),
            avg_bet=float(d.get("avg_bet") or 0.0),
            trades_per_hour=float(d.get("trades_per_hour") or 0.0),
            verified=bool(d.get("verified") or False),
            watchable=bool(d.get("watchable") or False),
            elite=bool(d.get("elite") or False),
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
        )

    @classmethod
    def make_stub(cls, addr: str, detail: str, *, watchable: bool = True) -> "Wallet":
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
            n_resolved=0,
            n_pos=0,
            total_value=0.0,
            total_pnl=0.0,
            pnl_pct=0.0,
            avg_pos_size=0.0,
            avg_profit=0.0,
            avg_bet=0.0,
            trades_per_hour=0.0,
            verified=False,
            watchable=watchable,
            elite=False,
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
#  WIN RATE CALCULATION
# ─────────────────────────────────────────────────────────────────────────────
def fetch_real_winrate(wallet: str) -> WinRateData:
    """
    Compute win rate from resolved trades.
    Returns win_rate, wilson_lb, total resolved, avg_profit, avg_bet.

    v10: Also computes recent_pnl_30d and recent_pnl_7d for Recent Form strategy.
    """
    _limit = C.ACTIVITY_LIMIT or 500

    ### Redeems trades
    redeems = S.safe_get(f"{C.DATA_API}/activity", {
        "user": wallet, "type": "REDEEM",
        "limit": _limit, "sortBy": "TIMESTAMP", "sortDirection": "DESC",
    }) or []
    if isinstance(redeems, dict):
        redeems = redeems.get("data", [])
    redeem_keys = set()
    total_redeem_value = 0.0
    for r in redeems:
        cid   = r.get("conditionId") or ""
        asset = r.get("asset") or ""
        if cid or asset:
            redeem_keys.add((cid, asset))
        total_redeem_value += float(r.get("usdcSize") or r.get("size") or 0)

    ### Buy trades
    buy_trades = S.safe_get(f"{C.DATA_API}/activity", {
        "user": wallet, "type": "TRADE", "side": "BUY",
        "limit": _limit, "sortBy": "TIMESTAMP", "sortDirection": "DESC",
    }) or []
    if isinstance(buy_trades, dict):
        buy_trades = buy_trades.get("data", [])
    loaded_trade_count = len(buy_trades)
    trade_load_limited = loaded_trade_count >= _limit
    loaded_trade_ts = [
        float(t.get("timestamp") or 0.0)
        for t in buy_trades
        if float(t.get("timestamp") or 0.0) > 0.0
    ]
    first_loaded_trade_ts = min(loaded_trade_ts) if loaded_trade_ts else None
    last_loaded_trade_ts = max(loaded_trade_ts) if loaded_trade_ts else None

    # Estimate trades_per_hour from timestamp spread
    trades_per_hour = 0.0
    if len(buy_trades) >= 10:
        ts_list = sorted([float(t.get("timestamp") or 0) for t in buy_trades if t.get("timestamp")], reverse=True)
        if len(ts_list) >= 2:
            span_hours = (ts_list[0] - ts_list[-1]) / 3600
            if span_hours > 0:
                trades_per_hour = len(ts_list) / span_hours

    # v10: Compute time-windowed PnL for Recent Form strategy
    now_t   = time.time()
    days_30 = now_t - 30 * 86400
    days_7  = now_t - 7  * 86400

    trades_30d = [t for t in buy_trades if float(t.get("timestamp") or 0) >= days_30]
    trades_7d  = [t for t in buy_trades if float(t.get("timestamp") or 0) >= days_7]

    redeems_30d = [r for r in redeems if float(r.get("timestamp") or 0) >= days_30]
    redeems_7d  = [r for r in redeems if float(r.get("timestamp") or 0) >= days_7]

    def _cash(t):
        v = float(t.get("usdcSize") or 0) or float(t.get("size") or 0) * float(t.get("price") or 0)
        return v

    loaded_trade_pnl = total_redeem_value - sum(_cash(t) for t in buy_trades)

    recent_pnl_30d: float | None = (
        sum(float(r.get("usdcSize", 0) or 0) for r in redeems_30d) -
        sum(_cash(t) for t in trades_30d)
        if first_loaded_trade_ts is not None and first_loaded_trade_ts <= days_30
        else None
    )
    recent_pnl_7d: float | None = (
        sum(float(r.get("usdcSize", 0) or 0) for r in redeems_7d) -
        sum(_cash(t) for t in trades_7d)
        if first_loaded_trade_ts is not None and first_loaded_trade_ts <= days_7
        else None
    )

    trade_by_pos = {}
    total_spent  = 0.0
    for t in buy_trades:
        cid   = t.get("conditionId") or ""
        asset = t.get("asset") or ""
        cash  = float(t.get("usdcSize") or 0) or float(t.get("size") or 0) * float(t.get("price") or 0)
        total_spent += cash
        pos_key = (cid, asset)
        if pos_key != ("", "") and pos_key not in trade_by_pos:
            trade_by_pos[pos_key] = {"cash": cash, "cid": cid, "asset": asset}

    ### Current Positions
    positions_raw = S.safe_get(f"{C.DATA_API}/positions", {
        "user": wallet, "limit": _limit,
        "sortBy": "CURRENT", "sortDirection": "ASC",
    }) or []
    if isinstance(positions_raw, dict):
        positions_raw = positions_raw.get("data", [])

    current_price_by_pos = {}
    for p in positions_raw:
        entry = {
            "cur":        float(p.get("curPrice", 0.5) or 0.5),
            "redeemable": p.get("redeemable", False),
            "cashPnl":    float(p.get("cashPnl", 0) or 0),
        }
        cid = p.get("conditionId") or ""
        asset = p.get("asset") or ""
        if cid or asset:
            current_price_by_pos[(cid, asset)] = entry

    lost_positions = set()
    for pos_key, td in trade_by_pos.items():
        if pos_key in redeem_keys:
            continue
        pos = current_price_by_pos.get(pos_key)
        if pos:
            cur      = pos["cur"]
            cash_pnl = pos.get("cashPnl", 0)
            if cur <= 0.02:
                lost_positions.add(pos_key)
            elif pos.get("redeemable") and cash_pnl < 0:
                lost_positions.add(pos_key)
            elif cash_pnl < -1.0 and cur < 0.10:
                lost_positions.add(pos_key)

    wins   = len(redeem_keys)
    losses = len(lost_positions)
    total  = wins + losses

    # When no matches between REDEEM and BUY trades (window mismatch: wallet traded >LIMIT times),
    # fall back to using REDEEM count as wins against total open positions.
    if total == 0 and len(redeem_keys) > 0 and len(positions_raw) > 0:
        n_open = len(positions_raw)
        wins   = len(redeem_keys)
        total  = wins + n_open
        avg_bet = total_spent / len(trade_by_pos) if trade_by_pos else 0
        if avg_bet == 0:
            open_costs = [float(p.get("initialValue", 0) or p.get("currentValue", 0) or 0) for p in positions_raw]
            open_costs = [s for s in open_costs if s > 0]
            if open_costs:
                avg_bet = sum(open_costs) / len(open_costs)
        wr = wins / total
        wb = wilson_lower_bound(wins, total)
        avg_profit = round(total_redeem_value / wins, 2) if wins > 0 else -1
        return {
            "wins": wins, "losses": n_open, "total": total,
            "loaded_trade_count": loaded_trade_count,
            "trade_load_limited": trade_load_limited,
            "loaded_trade_pnl": round(loaded_trade_pnl, 2),
            "first_loaded_trade_ts": first_loaded_trade_ts,
            "last_loaded_trade_ts": last_loaded_trade_ts,
            "win_rate": round(wr, 4), "wilson_lb": round(wb, 4),
            "source": "redeem_window_fallback",
            "avg_profit": avg_profit, "avg_bet": round(avg_bet, 2),
            "trades_per_hour": round(trades_per_hour, 2),
            "recent_pnl_30d": round(recent_pnl_30d, 2) if recent_pnl_30d is not None else None,
            "recent_pnl_7d":  round(recent_pnl_7d, 2) if recent_pnl_7d is not None else None,
        }

    resolved_keys    = redeem_keys | lost_positions
    resolved_spend   = sum(td["cash"] for k, td in trade_by_pos.items() if k in resolved_keys)
    n_res_with_spend = sum(1 for k in resolved_keys if k in trade_by_pos)
    avg_bet = resolved_spend / n_res_with_spend if n_res_with_spend > 0 else 0

    if total > 0 and resolved_spend > 0:
        avg_profit = round((total_redeem_value - resolved_spend) / total, 2)
    else:
        avg_profit = -1

    if avg_bet == 0 and trade_by_pos:
        avg_bet = total_spent / len(trade_by_pos)

    if total == 0:
        n_open    = len(positions_raw)
        open_wins = sum(1 for p in positions_raw if float(p.get("cashPnl", 0) or 0) > 0)
        wr_open   = open_wins / n_open if n_open > 0 else 0
        wb        = wilson_lower_bound(open_wins, n_open)
        if avg_bet == 0 and n_open > 0:
            open_costs = [float(p.get("initialValue", 0) or p.get("currentValue", 0) or 0) for p in positions_raw]
            open_costs = [s for s in open_costs if s > 0]
            if open_costs:
                avg_bet = sum(open_costs) / len(open_costs)
        return {
            "wins": open_wins, "losses": n_open - open_wins,
            "total": n_open,
            "loaded_trade_count": loaded_trade_count,
            "trade_load_limited": trade_load_limited,
            "loaded_trade_pnl": round(loaded_trade_pnl, 2),
            "first_loaded_trade_ts": first_loaded_trade_ts,
            "last_loaded_trade_ts": last_loaded_trade_ts,
            "win_rate": wr_open,
            "wilson_lb": wb * 0.5, "source": "open_positions_proxy",
            "avg_profit": avg_profit, "avg_bet": round(avg_bet, 2),
            "trades_per_hour": round(trades_per_hour, 2),
            "recent_pnl_30d": round(recent_pnl_30d, 2) if recent_pnl_30d is not None else None,
            "recent_pnl_7d":  round(recent_pnl_7d, 2) if recent_pnl_7d is not None else None,
        }

    wr = wins / total
    wb = wilson_lower_bound(wins, total)
    return {
        "wins": wins, "losses": losses, "total": total,
        "loaded_trade_count": loaded_trade_count,
        "trade_load_limited": trade_load_limited,
        "loaded_trade_pnl": round(loaded_trade_pnl, 2),
        "first_loaded_trade_ts": first_loaded_trade_ts,
        "last_loaded_trade_ts": last_loaded_trade_ts,
        "win_rate": round(wr, 4), "wilson_lb": round(wb, 4),
        "source": "redeems + inferred losses from current positions",
        "avg_profit": avg_profit,
        "avg_bet":    round(avg_bet, 2),
        "trades_per_hour": round(trades_per_hour, 2),
        "recent_pnl_30d": round(recent_pnl_30d, 2) if recent_pnl_30d is not None else None,
        "recent_pnl_7d":  round(recent_pnl_7d, 2) if recent_pnl_7d is not None else None,
    }


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
            msg, level = f"⬇ {tag_before}→{tag_after} {name} ({addr[:12]}…) | {reasons_str} | {stats_str}", "WARN"
    else:
        msg, level = f"~ {tag_before}→{tag_after} {name} ({addr[:12]}…) | {stats_str}", "INFO"
    S._log(f"[WALLET] {msg}", level, terminal=True)


def get_compute_and_store_wallet(wallet: str) -> Wallet:
    wallet = wallet.lower()
    now_t  = time.time()
    is_vip = wallet in {addr.lower() for addr in C.VIP_WALLETS}
    vip_name = C.VIP_WALLET_NAMES.get(wallet, "")
    cached = S.env().wallet_cache.get(wallet)
    if cached is not None and (now_t - cached.ts) < WALLET_TTL:
        return cached

    existing_name    = cached.name if cached is not None else ""
    existing_is_real = bool(existing_name) and not _is_auto_wallet_name(existing_name)
    keep_name        = existing_name if existing_is_real else vip_name

    lb_data = S.safe_get(f"{C.DATA_API}/v1/leaderboard", {"user": wallet, "timePeriod": "ALL"})
    lb_row  = lb_data[0] if lb_data and isinstance(lb_data, list) else None
    if lb_row and not keep_name:
        lb_name = str(lb_row.get("userName") or "").strip()
        if lb_name and not _is_auto_wallet_name(lb_name):
            keep_name = lb_name

    if not keep_name:
        keep_name = resolve_wallet_display_name(wallet)

    pos_data = S.safe_get(f"{C.DATA_API}/positions", {
        "user": wallet, "limit": 500,
        "sortBy": "CASHPNL", "sortDirection": "DESC",
    })

    if pos_data is None or not isinstance(pos_data, list):
        if cached is not None:
            cached.ts = now_t - WALLET_TTL + 60
            S.env().wallet_cache[wallet] = cached
            return cached
        null = Wallet.make_stub(wallet, "No data", watchable=False)
        null.ts   = now_t
        null.name = keep_name or (wallet[:10] + "…")
        null.vip  = is_vip
        null.fail_reasons = ["no_data"]
        S.env().wallet_cache[wallet] = null
        return null

    n_pos  = len(pos_data)
    init   = sum(float(p.get("initialValue") or 0) for p in pos_data)
    cur    = sum(float(p.get("currentValue") or 0) for p in pos_data)
    pnl    = sum(float(p.get("cashPnl")      or 0) for p in pos_data)

    value_data = S.safe_get(f"{C.DATA_API}/value", {"user": wallet})
    if value_data and isinstance(value_data, list) and len(value_data) > 0:
        cur = float(value_data[0].get("value") or cur)

    pct    = pnl / init * 100 if init > 0 else 0
    avg_sz = init / n_pos if n_pos > 0 else 0

    lb_rank: int | None = None
    lb_vol:  float | None = None
    if lb_row:
        if lb_row.get("pnl") is not None:
            pnl = float(lb_row["pnl"])
        try:
            lb_rank = int(lb_row["rank"]) if lb_row.get("rank") is not None else None
        except (ValueError, TypeError):
            lb_rank = None
        if lb_row.get("vol") is not None:
            lb_vol = float(lb_row["vol"])

    wr_data            = fetch_real_winrate(wallet)
    wr                 = wr_data["win_rate"]
    wb                 = wr_data["wilson_lb"]
    n_res              = wr_data["total"]
    wr_src             = wr_data["source"]
    avg_profit         = wr_data["avg_profit"]
    avg_bet            = wr_data["avg_bet"]
    tph                = wr_data["trades_per_hour"]
    loaded_trade_count = wr_data["loaded_trade_count"]
    trade_load_limited = wr_data["trade_load_limited"]
    loaded_trade_pnl   = wr_data["loaded_trade_pnl"]
    first_loaded_trade_ts = wr_data["first_loaded_trade_ts"]
    last_loaded_trade_ts  = wr_data["last_loaded_trade_ts"]
    recent_pnl_30d     = wr_data["recent_pnl_30d"]
    recent_pnl_7d      = wr_data["recent_pnl_7d"]

    if existing_is_real:
        final_name = existing_name
    elif keep_name:
        final_name = keep_name
    elif existing_name and _is_auto_wallet_name(existing_name):
        final_name = existing_name
    else:
        final_name = wallet[:10] + "…"

    sel = C.get_active_selector()
    _draft = Wallet(
        addr=wallet, name=final_name, ts=now_t,
        loaded_trade_count=loaded_trade_count, trade_load_limited=trade_load_limited,
        loaded_trade_pnl=loaded_trade_pnl,
        first_loaded_trade_ts=float(first_loaded_trade_ts) if first_loaded_trade_ts is not None else None,
        last_loaded_trade_ts=float(last_loaded_trade_ts) if last_loaded_trade_ts is not None else None,
        score=0.0, win_rate=wr, wilson_lb=wb, alpha_per_trade=0.0, wr_source=wr_src,
        n_resolved=n_res, n_pos=n_pos, total_value=cur, total_pnl=pnl, pnl_pct=pct,
        avg_pos_size=avg_sz, avg_profit=avg_profit, avg_bet=avg_bet, trades_per_hour=round(tph, 2),
        verified=False, watchable=False, elite=False, hft=False, vip=is_vip, sports_bot=False, dead=False,
        recent_pnl_30d=round(recent_pnl_30d, 2) if recent_pnl_30d is not None else None,
        recent_pnl_7d=round(recent_pnl_7d, 2) if recent_pnl_7d is not None else None,
        recent_ts=now_t, lb_rank=lb_rank, lb_vol=lb_vol, detail="", fail_reasons=[],
    )
    result = _draft.reclassify(sel)

    _log_wallet_change(cached, result, result.fail_reasons, final_name, wallet)

    _changed = cached is None or any(
        getattr(result, k) != getattr(cached, k)
        for k in ("elite", "verified", "watchable", "score", "win_rate", "wilson_lb",
                  "total_pnl", "name", "hft", "vip", "sports_bot", "dead", "recent_pnl_30d", "recent_pnl_7d",
                  "loaded_trade_count", "trade_load_limited", "first_loaded_trade_ts", "last_loaded_trade_ts", "ts")
    )
    S.env().wallet_cache[wallet] = result
    if result.watchable and _changed:
        DB.upsert_wallet_profile(wallet, result.to_db_dict())
    elif not result.watchable and cached is not None and cached.watchable:
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
        return [addr for addr, w in self._data.items() if w.elite]

    def get_watchlist(self) -> list[str]:
        return [addr for addr, w in self._data.items() if w.watchable]

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
                _log_wallet_change(w, result, result.fail_reasons, w.name, addr)
                updated += 1

            self._data[addr] = result
            if result.watchable:
                DB.upsert_wallet_profile(addr, result.to_db_dict())
            elif not result.watchable and w.watchable:
                DB.clear_wallet_profile(addr)

        S.log_important(f"🎯 reclassify_all done: {updated} wallet(s) changed out of {len(self._data)}")
        return updated

    def refresh_recent_form(self) -> None:
        """Refresh recent_pnl_30d / recent_pnl_7d for stale verified wallets. No classification."""
        now_t = time.time()
        stale_threshold = now_t - 6 * 3600
        refreshed = 0
        for addr, profile in list(self._data.items()):
            if not profile.verified:
                continue
            if profile.recent_ts >= stale_threshold:
                continue
            try:
                wr_data = fetch_real_winrate(addr)
                profile.recent_pnl_30d = wr_data["recent_pnl_30d"]
                profile.recent_pnl_7d  = wr_data["recent_pnl_7d"]
                profile.recent_ts      = now_t
                self._data[addr] = profile
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
        if prof.verified:
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
        verified_set = {w for w in wl if (p := S.env().wallet_cache.get(w)) and p.verified}
        unverified   = [w for w in wl if w not in verified_set]
        keep_unver   = max(0, MAX_WATCHLIST_SIZE - len(verified_set))
        for w in unverified[keep_unver:]:
            S.env().wallet_cache[w].watchable = False
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
            if prof.watchable:
                added += 1
                if prof.verified:
                    tag = prof.tag()
                    S._log(f"🆕 {tag} from market scan: {w[:14]}…", "INFO")
            time.sleep(0.12)
        S._log(f"🔍 Market scan done — {added} added", "DATA")
    except Exception as e:
        S._log(f"⚠ Market scan failed: {e}", "WARN")
