"""
titan_api_inspector.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Standalone Polymarket API inspector — mirrors EVERY fetch that TITAN
performs and saves all results in ONE structured JSON file.

Run it once: python titan_api_inspector.py

Output: titan_api_report_<timestamp>.json

Structure:
  {
    "meta": { run info, totals, issue summary },
    "steps": {
      "01_vip_trades":      { per-wallet results },
      "02_public_feed":     { ... },
      "03_positions":       { per-wallet results },
      "04_activity_buys":   { per-wallet results },
      "05_activity_redeems":{ per-wallet results },
      "06_activity_sells":  { per-wallet results },
      "07_leaderboards":    { per-period results },
      "08_market_data":     { per-cid results },
      "09_exit_checks":     { per-wallet results },
      "10_gamma_markets":   { ... }
    }
  }

No titan modules are imported. 100% standalone.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import json
import os
import time
import requests
from datetime import datetime, timezone

# ─────────────────────────────────────────────────────────────────────────────
#  CONFIGURATION  (copy from titan_config.py — edit here if you changed those)
# ─────────────────────────────────────────────────────────────────────────────
DATA_API  = "https://data-api.polymarket.com/v1"
GAMMA_API = "https://gamma-api.polymarket.com"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
    "Origin": "https://polymarket.com",
    "Referer": "https://polymarket.com/",
}

VIP_WALLETS = [
    "0x507e52ef684ca2dd91f90a9d26d149dd3288beae",   # GamblingIsAllYouNeed
    "0x204f72f35326db932158cba6adff0b9a1da95e14",   # swisstony
    "0x6d9fc316c3b8377060a44b852ba664adbfd59790",   # MEPP
    "0x63ce342161250d705dc0b16df89036c8e5f9ba9a",   # 0x8dxd
    "0x1cc16713196d456f86fa9c7387dd326a7f73b8df",   # Wickier
]

INSPECT_TRADE_LIMIT    = 30
INSPECT_ACTIVITY_LIMIT = 50
MIN_TRADE_CASH         = 500
HOT_HOURS              = 4
WARM_HOURS             = 12
MAX_MARKETS_TO_INSPECT = 5
MAX_WALLETS_TO_INSPECT = 3

# ─────────────────────────────────────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────────────────────────────────────
RUN_TS      = datetime.now().strftime("%Y%m%d_%H%M%S")
OUT_FILE    = f"titan_api_report_{RUN_TS}.json"

# The master report object — everything goes in here
REPORT = {
    "meta":  {},
    "steps": {},
}


def _ts():
    return datetime.now().strftime("%H:%M:%S")


def _extract_cash(t: dict) -> float:
    """Mirrors titan_state.extract_cash — tries every known field name."""
    price = float(t.get("price") or 0)
    for field in ("usdcSize", "amount", "cashSize", "collateralAmount", "dollarSize"):
        v = t.get(field)
        if v is not None:
            val = float(v or 0)
            if val > 0:
                return val
    size = float(t.get("size") or 0)
    if size > 0 and 0.01 < price < 0.99:
        return round(size * price, 6)
    return size


def _detect_issues(data, context=""):
    issues = []
    if data is None:
        issues.append("RESPONSE IS NONE — API call failed or returned nothing")
        return issues

    if isinstance(data, dict):
        if "error" in data or "message" in data:
            issues.append(f"API returned error dict: {list(data.keys())}")
        if "data" in data and isinstance(data["data"], list):
            issues.append(
                f"Response is wrapped dict with 'data' key "
                f"({len(data['data'])} items inside) — TITAN unwraps this"
            )
        return issues

    if not isinstance(data, list):
        issues.append(f"Unexpected type: {type(data).__name__} (expected list or dict)")
        return issues

    if len(data) == 0:
        issues.append("Empty list returned — no records")
        return issues

    first = data[0] if data else {}
    if not isinstance(first, dict):
        issues.append(f"Items are {type(first).__name__}, not dict")
        return issues

    if context == "trades":
        # Check which cash field is actually present
        cash_fields_found = [f for f in ("usdcSize", "amount", "cashSize", "collateralAmount", "dollarSize") if f in first]
        if not cash_fields_found:
            issues.append(
                "NO cash field found — usdcSize/amount/cashSize/collateralAmount/dollarSize all missing. "
                "extract_cash() will fall back to size*price."
            )
        else:
            issues.append(f"Cash field in use: '{cash_fields_found[0]}'")

        for field in ["conditionId", "outcome", "price", "timestamp", "proxyWallet", "pseudonym", "title", "slug"]:
            if field not in first:
                issues.append(f"Missing field '{field}' in trade records")

        prices = [float(t.get("price") or 0) for t in data if t.get("price")]
        bad_prices = [p for p in prices if not (0.02 < p < 0.98)]
        if bad_prices:
            issues.append(f"{len(bad_prices)}/{len(prices)} prices outside (0.02–0.98) range")

        pseudonyms = [t.get("pseudonym") or "" for t in data]
        auto_names = [n for n in pseudonyms if n and "-" in n and n.replace("-", "").isalpha()]
        real_names = [n for n in pseudonyms if n and not ("-" in n and n.replace("-", "").isalpha())]
        issues.append(
            f"Name types: {len(real_names)} real / {len(auto_names)} auto-generated "
            f"(Adjective-Noun) / {len([n for n in pseudonyms if not n])} blank"
        )

    if context == "positions":
        for field in ["conditionId", "asset", "currentValue", "initialValue", "cashPnl", "curPrice", "redeemable"]:
            if field not in first:
                issues.append(f"Missing field '{field}' in position records")

    if context == "activity":
        cash_fields_found = [f for f in ("usdcSize", "amount", "cashSize", "collateralAmount", "dollarSize") if f in first]
        if not cash_fields_found:
            issues.append("NO cash field found in activity records — extract_cash() will use size*price fallback")
        else:
            issues.append(f"Cash field in use: '{cash_fields_found[0]}'")
        for field in ["conditionId", "asset", "timestamp", "type"]:
            if field not in first:
                issues.append(f"Missing field '{field}' in activity records")

    if context == "market":
        for field in ["conditionId", "question", "outcomePrices", "liquidity", "volume", "active", "closed", "endDate", "slug"]:
            if field not in first:
                issues.append(f"Missing field '{field}' in market record")
        op = first.get("outcomePrices")
        if op:
            try:
                parsed = json.loads(op) if isinstance(op, str) else op
                if not isinstance(parsed, list) or len(parsed) < 1:
                    issues.append("outcomePrices parsed but empty or not a list")
            except Exception as e:
                issues.append(f"outcomePrices not parseable as JSON: {e}")

    if context == "leaderboard":
        for field in ["proxyWallet", "pnl", "profit"]:
            if field not in first:
                issues.append(f"Missing field '{field}' in leaderboard records")

    return issues


def _field_inventory(data):
    if isinstance(data, dict) and "data" in data:
        data = data["data"]
    if not isinstance(data, list) or not data:
        if isinstance(data, dict):
            return sorted(data.keys())
        return []
    all_keys = set()
    for item in data[:20]:
        if isinstance(item, dict):
            all_keys.update(item.keys())
    return sorted(all_keys)


def _make_fetch_record(label, url, params, data, elapsed_ms, issues, extra=None):
    """Build a single fetch record to embed in the report."""
    item_count = None
    if isinstance(data, list):
        item_count = len(data)
    elif isinstance(data, dict) and "data" in data:
        item_count = len(data["data"])
    elif isinstance(data, dict):
        item_count = 1

    has_error = any(
        "NONE" in i or "Missing" in i or "Unexpected" in i or "error" in i.lower()
        for i in issues
    )

    record = {
        "label":       label,
        "url":         url,
        "params":      params,
        "timestamp":   _ts(),
        "elapsed_ms":  round(elapsed_ms),
        "item_count":  item_count,
        "ok":          not has_error,
        "issues":      issues,
        "field_keys":  _field_inventory(data),
        "diagnostics": extra or {},
        "response":    data,
    }

    status = "✅" if not has_error else "⚠️ "
    print(
        f"  {status} [{_ts()}] {label} → "
        f"{item_count if item_count is not None else 'null'} records | "
        f"{round(elapsed_ms)}ms"
    )
    for iss in issues:
        print(f"       📌 {iss}")

    return record


def _get(url, params=None, retries=2, timeout=15):
    for attempt in range(retries):
        try:
            t0 = time.time()
            r  = requests.get(url, params=params, headers=HEADERS, timeout=timeout)
            elapsed = (time.time() - t0) * 1000
            if r.status_code == 429:
                wait = 2 ** (attempt + 1)
                print(f"       ⏳ Rate limited — sleeping {wait}s")
                time.sleep(wait)
                continue
            if r.status_code == 200:
                try:
                    return r.json(), elapsed
                except Exception:
                    return None, elapsed
            print(f"       ⚠ HTTP {r.status_code}")
            return None, elapsed
        except requests.exceptions.Timeout:
            time.sleep(1)
        except Exception as e:
            print(f"       ⚠ Request error: {e}")
            time.sleep(0.5)
    return None, 0.0


def _save_report():
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(REPORT, f, indent=2, default=str)


# ─────────────────────────────────────────────────────────────────────────────
#  STEP 1 — VIP WALLET TRADE POLLS
# ─────────────────────────────────────────────────────────────────────────────
def step1_vip_wallet_trades():
    print("\n" + "═"*60)
    print("STEP 1 — VIP / Elite wallet direct trade polls")
    print("  mirrors: titan_market._poll_elite_wallets()")
    print("═"*60)

    warm_cutoff = time.time() - WARM_HOURS * 3600
    hot_cutoff  = time.time() - HOT_HOURS  * 3600
    all_trades  = []
    step_data   = {}

    for wallet in VIP_WALLETS:
        url    = f"{DATA_API}/trades"
        params = {
            "user":         wallet,
            "limit":        INSPECT_TRADE_LIMIT,
            "side":         "BUY",
            "filterType":   "CASH",
            "filterAmount": 200,
        }
        data, ms = _get(url, params)
        issues   = _detect_issues(data, context="trades")

        extra = {}
        if isinstance(data, list) and data:
            in_window  = [t for t in data if float(t.get("timestamp") or 0) >= warm_cutoff]
            hot        = [t for t in data if float(t.get("timestamp") or 0) >= hot_cutoff]
            pseudonyms = list({t.get("pseudonym") or "" for t in data if t.get("pseudonym")})
            real_names = [n for n in pseudonyms if "-" not in n]
            auto_names = [n for n in pseudonyms if "-" in n]
            # Show which cash field actually has values
            cash_sample = [_extract_cash(t) for t in data[:5]]
            extra = {
                "trades_in_warm_window": len(in_window),
                "trades_in_hot_window":  len(hot),
                "hit_limit":             len(data) >= INSPECT_TRADE_LIMIT,
                "distinct_pseudonyms":   pseudonyms[:10],
                "real_name_count":       len(real_names),
                "auto_name_count":       len(auto_names),
                "cash_sample_extracted": cash_sample,
                "sample_trade":          data[0] if data else None,
            }
            all_trades.extend(in_window)

        step_data[wallet] = _make_fetch_record(
            f"VIP trades — {wallet[:14]}…", url, params, data, ms, issues, extra
        )
        time.sleep(0.2)

    REPORT["steps"]["01_vip_trades"] = step_data
    return all_trades


# ─────────────────────────────────────────────────────────────────────────────
#  STEP 2 — PUBLIC FEED
# ─────────────────────────────────────────────────────────────────────────────
def step2_public_feed():
    print("\n" + "═"*60)
    print("STEP 2 — Public trade feed")
    print("  mirrors: titan_market.fetch_trades() → public feed section")
    print("═"*60)

    url    = f"{DATA_API}/trades"
    params = {
        "limit":        500,
        "filterType":   "CASH",
        "filterAmount": MIN_TRADE_CASH,
        "side":         "BUY",
    }
    data, ms = _get(url, params)
    issues   = _detect_issues(data, context="trades")

    extra        = {}
    feed_wallets = set()

    if isinstance(data, list) and data:
        warm_cutoff  = time.time() - WARM_HOURS * 3600
        hot_cutoff   = time.time() - HOT_HOURS  * 3600
        in_window    = [t for t in data if float(t.get("timestamp") or 0) >= warm_cutoff]
        hot          = [t for t in in_window if float(t.get("timestamp") or 0) >= hot_cutoff]
        feed_wallets = {(t.get("proxyWallet") or "").lower() for t in data if t.get("proxyWallet")}

        all_pseudonyms = [t.get("pseudonym") or "" for t in data]
        auto_names = [n for n in all_pseudonyms if n and "-" in n and n.replace("-", "").isalpha()]
        real_names = [n for n in all_pseudonyms if n and not ("-" in n and n.replace("-", "").isalpha())]

        # Test extract_cash on first 10 trades
        cash_sample = [{"raw": t, "extracted": _extract_cash(t)} for t in data[:3]]

        extra = {
            "total_raw":          len(data),
            "hit_limit":          len(data) >= 500,
            "in_warm_window":     len(in_window),
            "in_hot_window":      len(hot),
            "distinct_wallets":   len(feed_wallets),
            "name_real":          len(real_names),
            "name_auto":          len(auto_names),
            "name_blank":         len([n for n in all_pseudonyms if not n]),
            "sample_auto_names":  list({n for n in auto_names})[:8],
            "sample_real_names":  list({n for n in real_names if n})[:8],
            "cash_sample":        cash_sample,
            "sample_trade":       data[0] if data else None,
        }

    REPORT["steps"]["02_public_feed"] = _make_fetch_record(
        "Public feed trades", url, params, data, ms, issues, extra
    )
    return list(feed_wallets)


# ─────────────────────────────────────────────────────────────────────────────
#  STEP 3 — WALLET POSITIONS
# ─────────────────────────────────────────────────────────────────────────────
def step3_wallet_positions(wallets):
    print("\n" + "═"*60)
    print("STEP 3 — Wallet positions")
    print("  mirrors: titan_wallet.fetch_wallet() → /positions")
    print("═"*60)

    step_data = {}
    for wallet in wallets[:MAX_WALLETS_TO_INSPECT]:
        url    = f"{DATA_API}/positions"
        params = {"user": wallet, "limit": 500, "sortBy": "CASHPNL", "sortDirection": "DESC"}
        data, ms = _get(url, params)
        issues   = _detect_issues(data, context="positions")

        extra = {}
        if isinstance(data, list) and data:
            cur  = sum(float(p.get("currentValue") or 0) for p in data)
            init = sum(float(p.get("initialValue") or 0) for p in data)
            pnl  = sum(float(p.get("cashPnl")      or 0) for p in data)
            extra = {
                "n_positions":   len(data),
                "total_cur":     round(cur, 2),
                "total_initial": round(init, 2),
                "total_pnl":     round(pnl, 2),
                "pnl_pct":       round(pnl / init * 100, 2) if init > 0 else None,
                "redeemable":    sum(1 for p in data if p.get("redeemable")),
                "sample":        data[0] if data else None,
            }

        step_data[wallet] = _make_fetch_record(
            f"Positions — {wallet[:14]}…", url, params, data, ms, issues, extra
        )
        time.sleep(0.15)

    REPORT["steps"]["03_positions"] = step_data


# ─────────────────────────────────────────────────────────────────────────────
#  STEP 4 — WALLET ACTIVITY: BUYS
# ─────────────────────────────────────────────────────────────────────────────
def step4_wallet_activity_buys(wallets):
    print("\n" + "═"*60)
    print("STEP 4 — Wallet activity: BUY trades")
    print("  mirrors: titan_wallet.fetch_real_winrate() → TRADE/BUY")
    print("═"*60)

    step_data = {}
    for wallet in wallets[:MAX_WALLETS_TO_INSPECT]:
        url    = f"{DATA_API}/activity"
        params = {
            "user": wallet, "type": "TRADE", "side": "BUY",
            "limit": INSPECT_ACTIVITY_LIMIT,
            "sortBy": "TIMESTAMP", "sortDirection": "DESC",
        }
        data, ms = _get(url, params)
        raw = data
        if isinstance(data, dict) and "data" in data:
            data = data["data"]
        issues = _detect_issues(data, context="activity")
        if isinstance(raw, dict) and "data" in raw:
            issues.append("Response is wrapped in dict — TITAN unwraps via .get('data', [])")

        extra = {}
        if isinstance(data, list) and data:
            total_spent = sum(_extract_cash(t) for t in data)
            extra = {
                "n_buy_trades":   len(data),
                "total_spent":    round(total_spent, 2),
                "distinct_cids":  len({t.get("conditionId") for t in data if t.get("conditionId")}),
                "cash_sample":    [{"extracted": _extract_cash(t), "fields": {k: t.get(k) for k in ("usdcSize","amount","size","price") if k in t}} for t in data[:3]],
                "sample":         data[0] if data else None,
            }

        step_data[wallet] = _make_fetch_record(
            f"Activity BUY — {wallet[:14]}…", url, params, data, ms, issues, extra
        )
        time.sleep(0.15)

    REPORT["steps"]["04_activity_buys"] = step_data


# ─────────────────────────────────────────────────────────────────────────────
#  STEP 5 — WALLET ACTIVITY: REDEEMS
# ─────────────────────────────────────────────────────────────────────────────
def step5_wallet_activity_redeems(wallets):
    print("\n" + "═"*60)
    print("STEP 5 — Wallet activity: REDEEMs (win detection)")
    print("  mirrors: titan_wallet.fetch_real_winrate() → REDEEM")
    print("═"*60)

    step_data = {}
    for wallet in wallets[:MAX_WALLETS_TO_INSPECT]:
        url    = f"{DATA_API}/activity"
        params = {
            "user": wallet, "type": "REDEEM",
            "limit": INSPECT_ACTIVITY_LIMIT,
            "sortBy": "TIMESTAMP", "sortDirection": "DESC",
        }
        data, ms = _get(url, params)
        raw = data
        if isinstance(data, dict) and "data" in data:
            data = data["data"]
        issues = _detect_issues(data, context="activity")

        extra = {}
        if isinstance(data, list):
            total_won = sum(_extract_cash(r) for r in data)
            extra = {
                "n_redeems":     len(data),
                "total_won":     round(total_won, 2),
                "distinct_cids": len({r.get("conditionId") for r in data if r.get("conditionId")}),
                "cash_sample":   [{"extracted": _extract_cash(r), "fields": {k: r.get(k) for k in ("usdcSize","amount","size","price") if k in r}} for r in data[:3]],
                "sample":        data[0] if data else None,
            }

        step_data[wallet] = _make_fetch_record(
            f"Activity REDEEM — {wallet[:14]}…", url, params, data, ms, issues, extra
        )
        time.sleep(0.15)

    REPORT["steps"]["05_activity_redeems"] = step_data


# ─────────────────────────────────────────────────────────────────────────────
#  STEP 6 — WALLET ACTIVITY: SELLS
# ─────────────────────────────────────────────────────────────────────────────
def step6_wallet_activity_sells(wallets):
    print("\n" + "═"*60)
    print("STEP 6 — Wallet activity: SELLs (exit detection)")
    print("  mirrors: titan_signals.check_whale_exits() and whale_still_holding()")
    print("═"*60)

    step_data = {}
    for wallet in wallets[:MAX_WALLETS_TO_INSPECT]:
        url    = f"{DATA_API}/activity"
        params = {
            "user": wallet, "type": "TRADE", "side": "SELL",
            "limit": INSPECT_ACTIVITY_LIMIT,
            "sortBy": "TIMESTAMP", "sortDirection": "DESC",
        }
        data, ms = _get(url, params)
        raw = data
        if isinstance(data, dict) and "data" in data:
            data = data["data"]
        issues = _detect_issues(data, context="activity")

        extra = {}
        if isinstance(data, list):
            now        = time.time()
            recent_24h = [t for t in data if float(t.get("timestamp") or 0) > now - 86400]
            extra = {
                "n_sells_total":  len(data),
                "sells_last_24h": len(recent_24h),
                "distinct_cids":  len({t.get("conditionId") for t in data if t.get("conditionId")}),
                "sample":         data[0] if data else None,
            }

        step_data[wallet] = _make_fetch_record(
            f"Activity SELL — {wallet[:14]}…", url, params, data, ms, issues, extra
        )
        time.sleep(0.15)

    REPORT["steps"]["06_activity_sells"] = step_data


# ─────────────────────────────────────────────────────────────────────────────
#  STEP 7 — LEADERBOARDS
# ─────────────────────────────────────────────────────────────────────────────
def step7_leaderboards():
    print("\n" + "═"*60)
    print("STEP 7 — Leaderboard fetches")
    print("  mirrors: titan_wallet.discover_new_whales() → leaderboard calls")
    print("═"*60)

    lb_configs = [
        ("ALL",   {"limit": 50, "timePeriod": "ALL",   "orderBy": "PNL"}),
        ("MONTH", {"limit": 50, "timePeriod": "MONTH", "orderBy": "PNL"}),
        ("WEEK",  {"limit": 50, "timePeriod": "WEEK",  "orderBy": "PNL"}),
    ]
    all_candidate_wallets = set()
    step_data = {}

    for period, params in lb_configs:
        url      = f"{DATA_API}/leaderboard"
        data, ms = _get(url, params)
        issues   = _detect_issues(data, context="leaderboard")

        extra = {}
        if isinstance(data, list) and data:
            wallets_found = {
                (e.get("proxyWallet") or e.get("address") or "").lower()
                for e in data
                if e.get("proxyWallet") or e.get("address")
            }
            all_candidate_wallets.update(wallets_found)
            pnls = [float(e.get("pnl") or e.get("profit") or 0) for e in data]
            id_fields = {
                field: sum(1 for e in data if e.get(field))
                for field in ["proxyWallet", "address", "wallet", "user", "pseudonym", "name"]
            }
            extra = {
                "n_entries":        len(data),
                "distinct_wallets": len(wallets_found),
                "top_pnl":          round(max(pnls), 2) if pnls else None,
                "median_pnl":       round(sorted(pnls)[len(pnls)//2], 2) if pnls else None,
                "wallet_id_fields": id_fields,
                "sample":           data[0] if data else None,
            }

        step_data[period] = _make_fetch_record(
            f"Leaderboard {period}", url, params, data, ms, issues, extra
        )
        time.sleep(0.3)

    REPORT["steps"]["07_leaderboards"] = step_data
    return list(all_candidate_wallets)


# ─────────────────────────────────────────────────────────────────────────────
#  STEP 8 — MARKET DATA
# ─────────────────────────────────────────────────────────────────────────────
def step8_market_data(trades):
    print("\n" + "═"*60)
    print("STEP 8 — Market data (gamma API)")
    print("  mirrors: titan_market.get_market(cid)")
    print("═"*60)

    cids = list({t.get("conditionId") for t in trades if t.get("conditionId")})
    if not cids:
        print("  ⚠ No conditionIds found in step-1 trades — skipping market fetch")
        REPORT["steps"]["08_market_data"] = {"note": "No conditionIds found in VIP trades"}
        return

    print(f"  Found {len(cids)} unique conditionIds — inspecting first {MAX_MARKETS_TO_INSPECT}")
    step_data = {}

    for cid in cids[:MAX_MARKETS_TO_INSPECT]:
        url    = f"{GAMMA_API}/markets"
        params = {"condition_id": cid}
        data, ms = _get(url, params)
        issues   = _detect_issues(data, context="market")

        extra = {}
        m = None
        if isinstance(data, list) and data:
            m = data[0]
        elif isinstance(data, dict):
            m = data

        if m:
            raw_prices = m.get("outcomePrices") or "[]"
            try:
                prices = json.loads(raw_prices) if isinstance(raw_prices, str) else raw_prices
                yes_p  = float(prices[0]) if prices else None
                no_p   = float(prices[1]) if len(prices) > 1 else None
            except Exception as e:
                prices = yes_p = no_p = None
                issues.append(f"outcomePrices parse error: {e}")

            ed = m.get("endDate") or m.get("endDateIso") or ""
            hrs_left = None
            try:
                if ed:
                    if ed.endswith("Z"):
                        ed = ed[:-1] + "+00:00"
                    edt = datetime.fromisoformat(ed)
                    if edt.tzinfo is None:
                        edt = edt.replace(tzinfo=timezone.utc)
                    hrs_left = round((edt - datetime.now(timezone.utc)).total_seconds() / 3600, 1)
            except Exception as e:
                issues.append(f"endDate parse error ({ed!r}): {e}")

            extra = {
                "titan_yes_price":    yes_p,
                "titan_no_price":     no_p,
                "liquidity":          float(m.get("liquidity") or 0),
                "volume":             float(m.get("volume") or 0),
                "active":             m.get("active"),
                "closed":             m.get("closed"),
                "hrs_left":           hrs_left,
                "title":              m.get("question") or m.get("slug"),
                "raw_outcomePrices":  raw_prices,
                "price_in_bounds":    yes_p is not None and 0.02 < yes_p < 0.98,
                "all_market_fields":  sorted(m.keys()),
            }

            if yes_p is not None and not (0.02 < yes_p < 0.98):
                issues.append(f"Yes price {yes_p} is out of bounds — TITAN will reject this market")
            if float(m.get("liquidity") or 0) < 1000:
                issues.append(f"Liquidity ${float(m.get('liquidity') or 0):,.0f} < $1000 — TITAN will reject")
            if m.get("closed"):
                issues.append("Market is CLOSED — TITAN will reject")

        step_data[cid] = _make_fetch_record(
            f"Market — {cid[:20]}…", url, params, data, ms, issues, extra
        )
        time.sleep(0.2)

    REPORT["steps"]["08_market_data"] = step_data


# ─────────────────────────────────────────────────────────────────────────────
#  STEP 9 — WHALE EXIT CHECK
# ─────────────────────────────────────────────────────────────────────────────
def step9_whale_exit_check(trades):
    print("\n" + "═"*60)
    print("STEP 9 — Whale exit / still-holding check")
    print("  mirrors: titan_signals.check_whale_exits() + whale_still_holding()")
    print("═"*60)

    cids_by_wallet = {}
    for t in trades:
        w   = t.get("proxyWallet", "").lower() or ""
        cid = t.get("conditionId") or ""
        if w and cid and w in {v.lower() for v in VIP_WALLETS}:
            cids_by_wallet.setdefault(w, set()).add(cid)

    step_data = {}
    for wallet in list(cids_by_wallet.keys())[:2]:
        url    = f"{DATA_API}/activity"
        params = {
            "user": wallet, "type": "TRADE", "side": "SELL",
            "limit": 200, "sortBy": "TIMESTAMP", "sortDirection": "DESC",
        }
        data, ms = _get(url, params)
        raw = data
        if isinstance(data, dict) and "data" in data:
            data = data["data"]
        issues = _detect_issues(data, context="activity")

        extra = {}
        if isinstance(data, list):
            our_cids   = cids_by_wallet.get(wallet, set())
            cutoff_48h = time.time() - 48 * 3600
            still_holding = {}
            for cid in list(our_cids)[:5]:
                sold = any(
                    t.get("conditionId") == cid and float(t.get("timestamp") or 0) > cutoff_48h
                    for t in data
                )
                still_holding[cid] = not sold
            extra = {
                "n_sell_records": len(data),
                "cids_we_hold":   list(our_cids)[:5],
                "still_holding":  still_holding,
                "sample_sell":    data[0] if data else None,
            }

        step_data[wallet] = _make_fetch_record(
            f"Exit check — {wallet[:14]}…", url, params, data, ms, issues, extra
        )
        time.sleep(0.15)

    REPORT["steps"]["09_exit_checks"] = step_data


# ─────────────────────────────────────────────────────────────────────────────
#  STEP 10 — GAMMA ACTIVE MARKETS
# ─────────────────────────────────────────────────────────────────────────────
def step10_gamma_active_markets():
    print("\n" + "═"*60)
    print("STEP 10 — Gamma API: active markets list")
    print("  mirrors: titan_wallet.scan_top_market_holders() → gamma /markets")
    print("═"*60)

    url    = f"{GAMMA_API}/markets"
    params = {"limit": 20, "active": "true"}
    data, ms = _get(url, params)
    issues   = _detect_issues(data, context="market")

    extra = {}
    if isinstance(data, list) and data:
        volumes = [float(m.get("volume") or 0) for m in data]
        extra = {
            "n_markets":       len(data),
            "top_volume":      round(max(volumes), 2) if volumes else None,
            "avg_volume":      round(sum(volumes) / len(volumes), 2) if volumes else None,
            "active_count":    sum(1 for m in data if m.get("active")),
            "closed_count":    sum(1 for m in data if m.get("closed")),
            "field_inventory": sorted({k for m in data for k in m.keys()}),
            "sample":          data[0] if data else None,
        }

    REPORT["steps"]["10_gamma_markets"] = _make_fetch_record(
        "Gamma active markets", url, params, data, ms, issues, extra
    )


# ─────────────────────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────────────────────
def main():
    print("═"*60)
    print("  TITAN API INSPECTOR")
    print(f"  Run: {RUN_TS}")
    print(f"  Output: {OUT_FILE}")
    print("═"*60)

    vip_trades   = step1_vip_wallet_trades()
    feed_wallets = step2_public_feed()

    deep_inspect = list({w.lower() for w in VIP_WALLETS}) + [
        w for w in feed_wallets if w not in {v.lower() for v in VIP_WALLETS}
    ]
    step3_wallet_positions(deep_inspect)
    step4_wallet_activity_buys(deep_inspect)
    step5_wallet_activity_redeems(deep_inspect)
    step6_wallet_activity_sells(deep_inspect)
    step7_leaderboards()
    step8_market_data(vip_trades)
    step9_whale_exit_check(vip_trades)
    step10_gamma_active_markets()

    # ── Build meta summary ────────────────────────────────────────────────────
    all_records = []
    def _collect(obj):
        if isinstance(obj, dict):
            if "issues" in obj and "ok" in obj:
                all_records.append(obj)
            else:
                for v in obj.values():
                    _collect(v)
    _collect(REPORT["steps"])

    total  = len(all_records)
    ok     = sum(1 for r in all_records if r["ok"])
    warn   = total - ok
    avg_ms = sum(r["elapsed_ms"] for r in all_records) / total if total else 0

    REPORT["meta"] = {
        "run_timestamp":  RUN_TS,
        "output_file":    OUT_FILE,
        "total_fetches":  total,
        "ok":             ok,
        "warnings":       warn,
        "avg_latency_ms": round(avg_ms),
        "issue_summary": [
            {"step_label": r["label"], "issues": r["issues"]}
            for r in all_records
            if not r["ok"]
        ],
    }

    _save_report()

    print("\n" + "═"*60)
    print(f"INSPECTION COMPLETE — {RUN_TS}")
    print("═"*60)
    print(f"  Total API calls : {total}")
    print(f"  Clean           : {ok}")
    print(f"  With issues     : {warn}")
    print(f"  Avg latency     : {round(avg_ms)}ms")
    print(f"  Output file     : {OUT_FILE}")
    print()

    if warn:
        print("  Issues detected:")
        for r in all_records:
            bad = [i for i in r["issues"] if
                   "NONE" in i or "Missing" in i or "Unexpected" in i or "error" in i.lower()]
            if bad:
                print(f"    {r['label']}")
                for b in bad:
                    print(f"       ⚠ {b}")

    print("═"*60)


if __name__ == "__main__":
    main()