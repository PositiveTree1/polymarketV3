"""
TITAN — Wallet scoring, HFT detection, and whale performance tracking. v10 OVERHAUL.

HFT DETECTION:
  Some elite wallets (e.g. swisstony) are high-frequency traders.
  They trade many small positions very rapidly. Traditional metrics
  (avg_bet, avg_profit_per_trade) unfairly penalise them.
  is_hft_wallet() detects HFT behaviour and adjusts polling accordingly.

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

  6. is_recent_form_qualified(): Gate function for Recent Form Copy strategy.
"""

import time
import math
from collections.abc import Mapping
from typing import TypedDict
import titan_state as S
import titan_config as C
from titan_config import *
import titan_db as DB


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
    recent_pnl_30d:     float
    recent_pnl_7d:      float


class WalletProfile(TypedDict):
    # ── identity ──────────────────────────────────────────────────────────────
    name:               str
    ts:                 float
    loaded_trade_count: int
    trade_load_limited: bool
    loaded_trade_pnl:     float
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

    # ── recent form ───────────────────────────────────────────────────────────
    recent_pnl_30d:     float | None
    recent_pnl_7d:      float | None
    recent_ts:          float

    # ── leaderboard ───────────────────────────────────────────────────────────
    lb_rank:            int | None
    lb_vol:             float | None

    # ── debug ─────────────────────────────────────────────────────────────────
    detail:             str
    fail_reasons:       list[str]


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


def is_hft_wallet(profile: WalletProfile) -> bool:
    """
    Return True if this wallet behaves like a high-frequency trader.

    HFT heuristics (any one is sufficient):
      - avg_bet < $50 AND n_resolved > 100  (many small bets)
      - trades_per_hour > HFT_MIN_TRADES_PER_HOUR (if we have that metric)
      - explicitly tagged via "hft": True in cache
    """
    if profile.get("hft"):
        return True
    avg_bet  = profile.get("avg_bet", 0)
    n_res    = profile.get("n_resolved", 0)
    tph      = profile.get("trades_per_hour", 0)
    if tph >= HFT_MIN_TRADES_PER_HOUR:
        return True
    if avg_bet > 0 and avg_bet < 50 and n_res > 100:
        return True
    return False


def is_sports_bot(profile: WalletProfile) -> bool:
    """
    Return True if this wallet is likely a sports market-making bot.

    Heuristics:
      - Elite wallet with very high TPH (>= SPORTS_BOT_MIN_TPH, default 150)
      - Explicitly tagged as sports_bot in cache
      - Name matches known sports bot patterns
      - High trades per hour combined with low avg_bet (non-elite version)
    """
    if profile.get("sports_bot"):
        return True

    tph     = profile.get("trades_per_hour", 0)
    avg_bet = profile.get("avg_bet", 0)

    try:
        import titan_config as _C
        sports_bot_tph = getattr(_C, "SPORTS_BOT_MIN_TPH", 150)
    except Exception:
        sports_bot_tph = 150

    if tph >= sports_bot_tph:
        return True

    name = profile.get("name", "").lower()
    _SPORTS_BOT_NAMES = {
        "gamblingisallyouneed", "swisstony", "rn1", "cannae", "lilybaeum",
        "billdenter", "billdenter2026", "elkmonkey", "billyel", "sportsguy",
        "texaskid", "ferrarichampions", "ferrarichampions2026", "snakeball",
    }
    for sbn in _SPORTS_BOT_NAMES:
        if sbn in name:
            return True

    if tph >= 50 and avg_bet > 0 and avg_bet < 100:
        return True

    return False


def alpha_per_trade(profile: WalletProfile) -> float:
    """
    Calculate the average alpha (profit) per resolved trade.
    Genuine alpha traders have alpha_per_trade >= $20.
    Market makers have tiny alpha_per_trade despite high win rates.
    """
    pnl   = profile.get("total_pnl", 0)
    n_res = profile.get("n_resolved", 0)
    if n_res <= 0:
        return 0.0
    return pnl / n_res


# ─────────────────────────────────────────────────────────────────────────────
#  WHALE PERFORMANCE TRACKER
# ─────────────────────────────────────────────────────────────────────────────
_whale_performance: dict[str, WhalePerformanceRecord] = {}


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


def get_whale_performance(wallet: str) -> WhalePerformanceRecord:
    """Get copy-trading performance record for a whale."""
    _empty: WhalePerformanceRecord = {"wins": 0, "losses": 0, "total_pnl": 0.0, "n_trades": 0, "recent_trades": []}
    return _whale_performance.get(wallet.lower(), _empty)


def get_whale_weekly_pnl(wallet: str) -> float:
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
        name = S.env().wallet_cache.get(w, {}).get("name", w[:10] + "…")
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


def is_recent_form_qualified(profile: WalletProfile,
                              min_pnl_30d: float = 0,
                              min_pnl_7d: float = -50,
                              max_tph: float = 20) -> bool:
    """
    Gate for Recent Form Copy strategy.
    Wallet must be profitable recently AND not be an HFT bot.
    """
    tph = profile.get("trades_per_hour", 0)
    if tph > max_tph:
        return False
    pnl_30d = profile.get("recent_pnl_30d", None)
    pnl_7d  = profile.get("recent_pnl_7d", None)
    if pnl_30d is None or pnl_7d is None:
        return False  # no recent data available yet
    return pnl_30d >= min_pnl_30d and pnl_7d >= min_pnl_7d


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
    redeems = S.safe_get(f"{C.DATA_API}/activity", {
        "user": wallet, "type": "REDEEM",
        "limit": _limit, "sortBy": "TIMESTAMP", "sortDirection": "DESC",
    }) or []
    if isinstance(redeems, dict):
        redeems = redeems.get("data", [])

    won_cids   = set()
    won_assets = set()
    total_redeem_value = 0.0
    for r in redeems:
        cid   = r.get("conditionId") or ""
        asset = r.get("asset") or ""
        if cid:   won_cids.add(cid)
        if asset: won_assets.add(asset)
        total_redeem_value += float(r.get("usdcSize") or r.get("size") or 0)

    trades_raw = S.safe_get(f"{C.DATA_API}/activity", {
        "user": wallet, "type": "TRADE", "side": "BUY",
        "limit": _limit, "sortBy": "TIMESTAMP", "sortDirection": "DESC",
    }) or []
    if isinstance(trades_raw, dict):
        trades_raw = trades_raw.get("data", [])
    loaded_trade_count = len(trades_raw)
    trade_load_limited = loaded_trade_count >= _limit
    loaded_trade_ts = [
        float(t.get("timestamp") or 0.0)
        for t in trades_raw
        if float(t.get("timestamp") or 0.0) > 0.0
    ]
    first_loaded_trade_ts = min(loaded_trade_ts) if loaded_trade_ts else None
    last_loaded_trade_ts = max(loaded_trade_ts) if loaded_trade_ts else None

    # Estimate trades_per_hour from timestamp spread
    trades_per_hour = 0.0
    if len(trades_raw) >= 10:
        ts_list = sorted([float(t.get("timestamp") or 0) for t in trades_raw if t.get("timestamp")], reverse=True)
        if len(ts_list) >= 2:
            span_hours = (ts_list[0] - ts_list[-1]) / 3600
            if span_hours > 0:
                trades_per_hour = len(ts_list) / span_hours

    # v10: Compute time-windowed PnL for Recent Form strategy
    now_t   = time.time()
    days_30 = now_t - 30 * 86400
    days_7  = now_t - 7  * 86400

    trades_30d = [t for t in trades_raw if float(t.get("timestamp") or 0) >= days_30]
    trades_7d  = [t for t in trades_raw if float(t.get("timestamp") or 0) >= days_7]

    redeems_30d = [r for r in redeems if float(r.get("timestamp") or 0) >= days_30]
    redeems_7d  = [r for r in redeems if float(r.get("timestamp") or 0) >= days_7]

    def _cash(t):
        v = float(t.get("usdcSize") or 0) or float(t.get("size") or 0) * float(t.get("price") or 0)
        return v

    loaded_trade_pnl = total_redeem_value - sum(_cash(t) for t in trades_raw)

    recent_pnl_30d = (
        sum(float(r.get("usdcSize", 0) or 0) for r in redeems_30d) -
        sum(_cash(t) for t in trades_30d)
    )
    recent_pnl_7d = (
        sum(float(r.get("usdcSize", 0) or 0) for r in redeems_7d) -
        sum(_cash(t) for t in trades_7d)
    )

    trade_by_key = {}
    total_spent  = 0.0
    for t in trades_raw:
        cid   = t.get("conditionId") or ""
        asset = t.get("asset") or ""
        cash  = float(t.get("usdcSize") or 0) or float(t.get("size") or 0) * float(t.get("price") or 0)
        total_spent += cash
        for key in [cid, asset]:
            if key and key not in trade_by_key:
                trade_by_key[key] = {"cash": cash, "cid": cid, "asset": asset}

    positions_raw = S.safe_get(f"{C.DATA_API}/positions", {
        "user": wallet, "limit": 500,
        "sortBy": "CURRENT", "sortDirection": "ASC",
    }) or []
    if isinstance(positions_raw, dict):
        positions_raw = positions_raw.get("data", [])

    price_by_key = {}
    for p in positions_raw:
        entry = {
            "cur":        float(p.get("curPrice", 0.5) or 0.5),
            "redeemable": p.get("redeemable", False),
            "cashPnl":    float(p.get("cashPnl", 0) or 0),
        }
        for k in [p.get("conditionId") or "", p.get("asset") or ""]:
            if k:
                price_by_key[k] = entry

    all_won   = won_cids | won_assets
    lost_keys = set()
    for key, td in trade_by_key.items():
        if td["cid"] in all_won or td["asset"] in all_won:
            continue
        pos = price_by_key.get(key)
        if pos:
            cur      = pos["cur"]
            cash_pnl = pos.get("cashPnl", 0)
            if cur <= 0.02:
                lost_keys.add(key)
            elif pos.get("redeemable") and cash_pnl < 0:
                lost_keys.add(key)
            elif cash_pnl < -1.0 and cur < 0.10:
                lost_keys.add(key)

    wins   = len(all_won)
    losses = len(lost_keys)
    total  = wins + losses

    # When no matches between REDEEM and BUY trades (window mismatch: wallet traded >LIMIT times),
    # fall back to using REDEEM count as wins against total open positions.
    if total == 0 and len(all_won) > 0 and len(positions_raw) > 0:
        n_open = len(positions_raw)
        wins   = len(all_won)
        total  = wins + n_open
        avg_bet = total_spent / len(trade_by_key) if trade_by_key else 0
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
            "recent_pnl_30d": round(recent_pnl_30d, 2),
            "recent_pnl_7d":  round(recent_pnl_7d, 2),
        }

    resolved_keys    = all_won | lost_keys
    resolved_spend   = sum(td["cash"] for k, td in trade_by_key.items() if k in resolved_keys)
    n_res_with_spend = sum(1 for k in resolved_keys if k in trade_by_key)
    avg_bet = resolved_spend / n_res_with_spend if n_res_with_spend > 0 else 0

    if total > 0 and resolved_spend > 0:
        avg_profit = round((total_redeem_value - resolved_spend) / total, 2)
    else:
        avg_profit = -1

    if avg_bet == 0 and trade_by_key:
        avg_bet = total_spent / len(trade_by_key)

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
            "recent_pnl_30d": round(recent_pnl_30d, 2),
            "recent_pnl_7d":  round(recent_pnl_7d, 2),
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
        "source": "resolved_history",
        "avg_profit": avg_profit,
        "avg_bet":    round(avg_bet, 2),
        "trades_per_hour": round(trades_per_hour, 2),
        "recent_pnl_30d": round(recent_pnl_30d, 2),
        "recent_pnl_7d":  round(recent_pnl_7d, 2),
    }


# ─────────────────────────────────────────────────────────────────────────────
#  WALLET SCORING
# ─────────────────────────────────────────────────────────────────────────────
def get_compute_and_store_wallet(wallet: str) -> WalletProfile:
    wallet = wallet.lower()
    now_t  = time.time()
    is_vip = wallet in {addr.lower() for addr in C.VIP_WALLETS}
    vip_name = C.VIP_WALLET_NAMES.get(wallet, "")
    cached = S.env().wallet_cache.get(wallet)
    if cached and (now_t - (cached.get("ts") or 0)) < WALLET_TTL:
        return cached

    existing_name    = (cached or {}).get("name") or ""
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

    null: WalletProfile = {
        "score": 0.10, "win_rate": 0.0, "wilson_lb": 0.0, "alpha_per_trade": 0.0,
        "n_resolved": 0, "n_pos": 0, "total_value": 0.0,
        "total_pnl": 0.0, "pnl_pct": 0.0, "avg_pos_size": 0.0,
        "avg_profit": 0.0, "avg_bet": 0.0, "trades_per_hour": 0.0,
        "recent_pnl_30d": None, "recent_pnl_7d": None, "recent_ts": 0.0,
        "loaded_trade_count": 0, "trade_load_limited": False,
        "loaded_trade_pnl": 0.0,
        "first_loaded_trade_ts": None, "last_loaded_trade_ts": None,
        "verified": False, "watchable": False, "elite": False, "hft": False, "vip": is_vip, "sports_bot": False,
        "name": keep_name or (wallet[:10] + "…"), "ts": now_t,
        "lb_rank": None, "lb_vol": None,
        "detail": "No data", "wr_source": "none",
        "fail_reasons": ["no_data"],
    }

    if pos_data is None or not isinstance(pos_data, list):
        if cached:
            cached["ts"] = now_t - WALLET_TTL + 60
            S.env().wallet_cache[wallet] = cached
            return cached
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

    wr_data    = fetch_real_winrate(wallet)
    wr         = wr_data["win_rate"]
    wb         = wr_data["wilson_lb"]
    n_res      = wr_data["total"]
    wr_src     = wr_data["source"]
    avg_profit = wr_data.get("avg_profit", 0)
    avg_bet    = wr_data.get("avg_bet", 0)
    tph        = wr_data.get("trades_per_hour", 0)
    loaded_trade_count = int(wr_data.get("loaded_trade_count", 0))
    trade_load_limited = bool(wr_data.get("trade_load_limited", False))
    loaded_trade_pnl = float(wr_data.get("loaded_trade_pnl", 0.0))
    first_loaded_trade_ts = wr_data.get("first_loaded_trade_ts")
    last_loaded_trade_ts = wr_data.get("last_loaded_trade_ts")
    # v10: store recent-form fields with their own TTL
    recent_pnl_30d = wr_data.get("recent_pnl_30d", None)
    recent_pnl_7d  = wr_data.get("recent_pnl_7d", None)

    avg_profit_estimated = False
    if avg_profit <= 0 and n_res >= 10 and pnl > 0:
        avg_profit = round((pnl * 0.5) / n_res, 2)
        avg_profit_estimated = True

    apt = loaded_trade_pnl / n_res if n_res > 0 else 0.0

    # ── delegate scoring and tiering to the active WalletSelector ────────────
    import titan_config as _C
    sel = _C.get_active_selector()

    raw_for_selector = {
        "win_rate":       wr,
        "wilson_lb":      wb,
        "n_resolved":     n_res,
        "n_pos":          n_pos,
        "total_pnl":      pnl,
        "total_value":    cur,
        "pnl_pct":        pct,
        "avg_profit":     avg_profit,
        "avg_bet":        avg_bet,
        "trades_per_hour": tph,
        "alpha_per_trade": apt,
    }

    if sel is not None:
        score = sel.score(raw_for_selector)
        watchable, verified, elite, fail_reasons = sel.is_selected(raw_for_selector, score)
        from titan_selector import PerformanceSelector
        hft_detected      = sel.is_hft(tph, avg_bet, n_res) if isinstance(sel, PerformanceSelector) else False
        sports_bot_detected = False
        _check_name = keep_name or existing_name or (wallet[:10] + "…")
        if isinstance(sel, PerformanceSelector):
            sports_bot_detected = sel.is_sports_bot(_check_name, tph)
    else:
        # fallback: hardcoded defaults so the system keeps running if selector fails to load
        score = (
            0.30 * wb +
            0.25 * min(1.0, max(0, pct / 30)) +
            0.15 * min(1.0, cur / 25_000) +
            0.10 * min(1.0, n_res / 20) +
            0.10 * min(1.0, n_pos / 10) +
            0.10 * min(1.0, max(0, avg_profit) / 50)
        )
        fail_reasons: list[str] = []
        watchable = wr >= 0.53 and wb >= 0.45 and n_res >= 10 and pnl >= 0
        verified  = watchable and wr >= 0.56 and wb >= 0.49
        elite     = False
        hft_detected       = tph >= HFT_MIN_TRADES_PER_HOUR
        sports_bot_detected = False

    if existing_is_real:
        final_name = existing_name
    elif keep_name:
        final_name = keep_name
    elif existing_name and _is_auto_wallet_name(existing_name):
        final_name = existing_name
    else:
        final_name = wallet[:10] + "…"

    est_tag   = "~" if avg_profit_estimated else ""
    hft_tag   = "⚡HFT" if hft_detected else ""
    sports_tag = "🏈SPORTS" if sports_bot_detected else ""
    rf_tag    = f" RF30d:${recent_pnl_30d:+.0f}" if recent_pnl_30d is not None else ""

    result: WalletProfile = {
        "score": round(score, 5), "win_rate": wr, "wilson_lb": wb,
        "n_resolved": n_res, "n_pos": n_pos,
        "total_value": cur, "total_pnl": pnl, "pnl_pct": pct,
        "avg_pos_size": avg_sz, "avg_profit": avg_profit, "avg_bet": avg_bet,
        "trades_per_hour": round(tph, 2),
        "alpha_per_trade": round(apt, 2),
        "hft": hft_detected,
        "vip": is_vip,
        "sports_bot": sports_bot_detected,
        "verified": verified, "watchable": watchable, "elite": elite,
        "loaded_trade_count": loaded_trade_count,
        "trade_load_limited": trade_load_limited,
        "loaded_trade_pnl": loaded_trade_pnl,
        "first_loaded_trade_ts": float(first_loaded_trade_ts) if first_loaded_trade_ts is not None else None,
        "last_loaded_trade_ts": float(last_loaded_trade_ts) if last_loaded_trade_ts is not None else None,
        "name": final_name, "ts": now_t, "wr_source": wr_src,
        "fail_reasons": fail_reasons,
        "recent_pnl_30d": round(recent_pnl_30d, 2) if recent_pnl_30d is not None else None,
        "recent_pnl_7d":  round(recent_pnl_7d, 2) if recent_pnl_7d is not None else None,
        "recent_ts": now_t,
        "lb_rank": lb_rank, "lb_vol": lb_vol,
        "detail": (
            f"Score:{score:.2f} WR:{wr*100:.0f}% WilsonLB:{wb*100:.0f}% "
            f"Res:{n_res} Port:${cur:,.0f} PnL:${pnl:+,.0f}({pct:+.1f}%) "
            f"AvgProfit:{est_tag}${avg_profit:.1f} AvgBet:${avg_bet:.0f} "
            f"AlphaPT:${apt:.1f} TPH:{tph:.1f} [{wr_src}] "
            f"{'🔥ELITE' if elite else '✅VER' if verified else '👁WATCH' if watchable else '❌'}"
            f"{hft_tag}{sports_tag}{rf_tag}"
        ),
    }

    def _tier(p: WalletProfile | None) -> str:
        if not p:
            return "NEW"
        if p.get("elite"):     return "🔥ELITE"
        if p.get("verified"):  return "✅VER"
        if p.get("watchable"): return "👁WATCH"
        return "❌REJ"

    tier_before = _tier(cached)
    tier_after  = _tier(result)
    if tier_before != tier_after:
        reasons_str = ", ".join(fail_reasons) if fail_reasons else ""
        stats_str = f"PnL=${pnl:+,.0f} Port=${cur:,.0f} Score={score:.2f} WR={wr*100:.0f}% Res={n_res}"
        if tier_after in ("🔥ELITE", "✅VER", "👁WATCH"):
            msg  = f"⬆ {tier_before}→{tier_after} {final_name} ({wallet[:12]}…) | {stats_str}"
            level = "INFO"
        else:
            msg  = f"⬇ {tier_before}→{tier_after} {final_name} ({wallet[:12]}…) | {reasons_str} | {stats_str}"
            level = "WARN"
        print(f"[WALLET] {msg}", flush=True)
        S._log(msg, level)

    _TRACKED = ("elite", "verified", "watchable", "score", "win_rate", "wilson_lb",
                "total_pnl", "name", "hft", "vip", "sports_bot", "recent_pnl_30d", "recent_pnl_7d",
                "loaded_trade_count", "trade_load_limited", "first_loaded_trade_ts", "last_loaded_trade_ts", "ts")
    _changed = cached is None or any(result.get(k) != cached.get(k) for k in _TRACKED)
    S.env().wallet_cache[wallet] = result
    if watchable and _changed:
        DB.upsert_wallet_profile(wallet, result)
    elif not watchable and (cached or {}).get("watchable"):
        DB.clear_wallet_profile(wallet)
    return result


# ─────────────────────────────────────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────────────────────────────────────
def get_elite_wallets() -> list[str]:
    return [w.lower() for w, p in S.env().wallet_cache.items() if p.get("elite")]


def _refresh_recent_form_scores() -> None:
    """
    v10: Refresh recent_pnl_30d / recent_pnl_7d for verified wallets whose
    recent_ts is older than 6 hours. Called every 50 cycles from the engine.
    This is additive to the normal wallet cache refresh.
    """
    now_t = time.time()
    stale_threshold = now_t - 6 * 3600
    refreshed = 0
    for wallet, profile in list(S.env().wallet_cache.items()):
        if not profile.get("verified"):
            continue
        recent_ts = profile.get("recent_ts", 0)
        if recent_ts >= stale_threshold:
            continue
        try:
            wr_data = fetch_real_winrate(wallet)
            profile["recent_pnl_30d"] = wr_data.get("recent_pnl_30d")
            profile["recent_pnl_7d"]  = wr_data.get("recent_pnl_7d")
            profile["recent_ts"]      = now_t
            S.env().wallet_cache[wallet] = profile
            refreshed += 1
            time.sleep(0.12)
        except Exception as e:
            S._log(f"Recent form refresh failed for {wallet}: {e}", "ERR")
    if refreshed:
        S._log(f"♻ Recent form refreshed for {refreshed} wallets", "DATA")


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
        if prof.get("verified"):
            discovered += 1
            tag = "🔥ELITE" if prof["elite"] else ("⚡HFT" if prof.get("hft") else "✅VER")
            S._log(
                f"🆕 {tag} {w[:14]}… "
                f"Score:{prof['score']:.2f} WR:{prof['win_rate']*100:.0f}% "
                f"PnL:${prof['total_pnl']:+,.0f} TPH:{prof.get('trades_per_hour',0):.1f}",
                "INFO"
            )
        time.sleep(0.12)

    wl = S.get_watchlist()
    if len(wl) > MAX_WATCHLIST_SIZE:
        import titan_db as DB
        verified_set = {w for w in wl if S.env().wallet_cache.get(w, {}).get("verified")}
        unverified   = [w for w in wl if w not in verified_set]
        keep_unver   = max(0, MAX_WATCHLIST_SIZE - len(verified_set))
        for w in unverified[keep_unver:]:
            S.env().wallet_cache[w]["watchable"] = False
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
            if prof.get("watchable"):
                added += 1
                if prof.get("verified"):
                    tag = "🔥ELITE" if prof["elite"] else ("⚡HFT" if prof.get("hft") else "✅VER")
                    S._log(f"🆕 {tag} from market scan: {w[:14]}…", "INFO")
            time.sleep(0.12)
        S._log(f"🔍 Market scan done — {added} added", "DATA")
    except Exception as e:
        S._log(f"⚠ Market scan failed: {e}", "WARN")
