# TITAN — AI Analysis & Tuning Guide

> How to systematically analyse TITAN's performance and propose safe config changes.
> This guide is written for AI models connecting via MCP.
> Works with any model that can call tools and reason about numbers.

---

## Principles

1. **Read before writing.** Always understand current state before proposing changes.
2. **One change at a time.** TITAN's cycle is 15 seconds — changes take effect immediately. Change one parameter, observe for several minutes.
3. **Dry-run first.** Every `update_config_*` tool supports `dry_run=true`. Use it.
4. **Know the loss history.** Prior sessions lost money to specific documented patterns. Don't reopen those failure modes.
5. **Check rejects.** Most tuning problems are visible in `get_rejects()` — it shows exactly why signals are being blocked.

---

## Session Start Checklist (run in order)

```
1. get_status           → confirm running=true, note recent_error_count
2. get_portfolio_overview → note total_equity, session_pnl, open_positions
3. get_recent_errors    → (if recent_error_count > 0)
4. get_snapshot         → full state in one call
5. get_trade_stats      → win_rate, expectancy, sell_count
```

If `sell_count < 5`: insufficient data for statistical analysis. Note the pattern but don't tune aggressively.

---

## Diagnostic Decision Tree

### Problem: No signals / very few signals

```
get_signals(min_score=0)   → how many signals exist at all?
get_rejects()              → what is blocking them?
```

**Common causes and fixes:**

| Rejection reason | Root cause | Fix |
|---|---|---|
| "age too old: Xh > 0.25h" | Signal age gate too tight | `update_config_signals signal_quality MAX_SIGNAL_AGE_H 0.5` |
| "score too low: X < 55" | MIN_SCORE too high | `update_config_signals signal_quality MIN_SCORE 45` |
| "price out of zone" | Price zone too tight | Check if markets are legitimately outside 20–72¢ |
| "drift too high" | MAX_DRIFT too tight | `update_config_signals drift_gates MAX_DRIFT 0.08` |
| "no elite wallets" | Elite roster empty | Check `get_tracked_wallets` — are any wallets `elite=true`? |
| "min confluence not met" | MIN_CONFLUENCE too high | `update_config_signals signal_quality MIN_CONFLUENCE 1` |
| "max positions reached" | All slots full | Check `get_positions` — stuck positions? |

**Warning:** Lowering gates to get more signals only helps if the underlying signals are good. Check the elite roster first — if there are no quality wallets, more signals = more noise.

---

### Problem: Many signals but nothing auto-trades

```
get_signals(min_score=0)   → look at "tier" field on each signal
get_config_risk()          → check TRADEABLE_TIERS_LIST
```

**Common causes:**
- All signals are STRONG/MEDIUM but TRADEABLE_TIERS_LIST only includes CONVICTION/ALERT
- Signals score below ALERT_SCORE (70) — check score distribution
- `get_config_signals` → `signal_quality.ALERT_SCORE` — if set too high, lower it (try 65)

---

### Problem: Win rate too low (< 45%)

```
get_trade_stats()          → win_rate, avg_win, avg_loss
get_trade_history()        → look for patterns in losing trades
get_snapshot()             → TRADE HISTORY section
query_db("SELECT strategy, COUNT(*) as n, AVG(pnl_pct) as avg_pnl FROM trade_history WHERE type='SELL' GROUP BY strategy")
```

**Diagnostic questions:**
1. Is one strategy responsible for most losses?
   → If `recent_form` losses dominate: raise `recent_form.min_pnl_30d` (try 100) or lower `max_positions` (try 2)
   → If `consensus_basket` losses dominate: raise `min_elite_confluence` to 2
   → If `drift_discount` losses dominate: lower `max_discount_pct` (try 0.08) — avoiding extreme discounts

2. Are losses concentrated in a price range?
   → Query: `SELECT price, pnl_pct FROM trade_history WHERE type='SELL' ORDER BY pnl_pct`
   → If losses at high prices → lower `MAX_ENTRY_PRICE` (try 0.65)
   → If losses at low prices → raise `MIN_ENTRY_PRICE` (try 0.25)

3. Are losses from specific wallets?
   → Check `wallet_names` in losing trades
   → Consider raising `ELITE_MIN_PNL` if verified (non-elite) wallets are causing losses

4. Are stop-losses firing at -30%?
   → Normal behaviour — the stop-loss is working
   → If too frequent: raise `MIN_CONFLUENCE` or `MIN_SCORE`

---

### Problem: Good win rate but low expectancy (small wins, big losses)

```
get_trade_stats()          → compare avg_win vs avg_loss
```

**Diagnosis:**
- If `avg_win < avg_loss × 1.5`: profit target too tight, stop-loss too wide
  → Raise `PROFIT_TARGET_PCT` (try 0.60)
  → Tighten `STOP_LOSS_PCT` (try -0.20)

- If wins are exiting too early (whale still holding):
  → Raise `PROFIT_TARGET_PCT` (try 0.60 or 0.80)
  → Or disable profit target temporarily: `PROFIT_TARGET_ENABLED=false` (follow whale only)

---

### Problem: Positions not closing (held too long)

```
get_positions()            → check hold time (entry_ts vs now)
```

**Diagnosis:**
- Whale exit not firing: check `WHALE_EXIT_SELL=true` in `get_config_risk()`
- Stop-loss not firing: check `STOP_LOSS_ENABLED=true` and `STOP_LOSS_PCT`
- Stale positions: `STALE_LOSER_AGE_H=0.5` — positions losing after 30min with drift < −8% should exit

---

### Problem: Too many positions open simultaneously

```
get_positions()            → count open positions
get_config_risk()          → MAX_OPEN_POSITIONS
```

If positions are consistently hitting the `MAX_OPEN_POSITIONS` cap and good signals are being rejected:
- Raise `MAX_OPEN_POSITIONS` (try 7)
- OR tighten strategy `max_positions` per builder to rebalance across strategies

---

### Problem: Elite roster too small (0–3 elites)

```
get_tracked_wallets()      → filter for elite=true
get_config_wallets()       → elite_thresholds section
```

**Fix:** Lower thresholds gradually:
```
update_config_wallets group=elite_thresholds patch={"ELITE_MIN_PNL": 25000}
```

Wait 1–2 discovery cycles (each 20 × 15s = 5 min) for new elites to appear.

---

## Safe Parameter Ranges

These are the bounds within which changes are considered safe based on the system's loss history.

### Signal Quality
| Parameter | Current | Safe range | Never go below/above |
|---|---|---|---|
| `MAX_SIGNAL_AGE_H` | 0.25 | 0.15 – 1.0 | Never > 2.0 (stale signals) |
| `MIN_SCORE` | 55 | 40 – 70 | Never < 35 |
| `ALERT_SCORE` | 70 | 60 – 80 | Never > 90 (no signals) |
| `MIN_CONFLUENCE` | 2 | 1 – 4 | Never 0 |

### Price Zones
| Parameter | Current | Safe range | Never go below/above |
|---|---|---|---|
| `MIN_ENTRY_PRICE` | 0.20 | 0.18 – 0.30 | Never < 0.15 (lottery tickets) |
| `MAX_ENTRY_PRICE` | 0.72 | 0.65 – 0.82 | Never > 0.85 (near-certainty trap) |

### Risk
| Parameter | Current | Safe range | Notes |
|---|---|---|---|
| `STOP_LOSS_PCT` | -0.30 | -0.15 to -0.40 | Never `null` globally |
| `PROFIT_TARGET_PCT` | 0.40 | 0.20 – 1.00 | Higher = follow whale longer |
| `MAX_OPEN_POSITIONS` | 5 | 3 – 10 | More = more diversification |

### Sizing
| Parameter | Current | Safe range | Notes |
|---|---|---|---|
| `KELLY_FRACTION` | 0.20 | 0.10 – 0.35 | Never > 0.50 (ruin risk) |
| `MAX_BET_ABS` | 4.0 | 1.0 – 8.0 | Scale with bankroll |
| `MAX_BET_PCT` | 0.18 | 0.10 – 0.25 | Never > 0.30 |

---

## Proposing a Change — Standard Workflow

```
Step 1: state the problem
  → "Win rate is 38% over 15 trades. recent_form strategy losing most."

Step 2: read relevant data
  → get_config_strategies() → recent_form block
  → query_db("SELECT * FROM trade_history WHERE type='SELL' AND strategy LIKE '%recent_form%'")

Step 3: form a hypothesis
  → "recent_form min_pnl_30d=0 allows wallets with breakeven recent performance.
     Raising to 50 would require wallets to be clearly profitable recently."

Step 4: dry-run
  → update_config_strategies(strategy="recent_form", patch={"min_pnl_30d": 50}, dry_run=True)
  → Confirm: {"ok": true, "errors": [], "applied": {"min_pnl_30d": 50}}

Step 5: apply
  → update_config_strategies(strategy="recent_form", patch={"min_pnl_30d": 50}, dry_run=False)
  → Confirm log: "Config updated: strategy/recent_form {'min_pnl_30d': 50}"

Step 6: monitor
  → wait 2–3 cycles (30–45 seconds)
  → get_signals(min_score=0) → did recent_form signal count change?
  → get_rejects() → are more signals rejected for "min_pnl_30d"?
```

---

## Changes to Avoid

These changes re-open documented failure modes and should never be made:

| Change | Why dangerous |
|---|---|
| `STOP_LOSS_ENABLED=false` | Primary cause of -97%, -99% losses in prior sessions |
| `MAX_ENTRY_PRICE > 0.85` | Near-certainty trap — 94¢ entry = $0.06 upside, $0.94 downside |
| `MAX_SIGNAL_AGE_H > 2.0` | Stale signals have already had their price impact absorbed |
| `MIN_CONFLUENCE=0` | Single-wallet signals have no corroboration |
| `KELLY_FRACTION > 0.50` | Half Kelly is the maximum for uncertain models |
| Enabling HFT in TRADEABLE_TIERS_LIST | HFT bots have hedged positions we can't see — copying them is copying one leg of an arb |

---

## Useful SQL Queries

Run these with `query_db()` after calling `get_db_schema()` to verify table structure.

**P&L by strategy:**
```sql
SELECT strategy, COUNT(*) n, ROUND(SUM(pnl_usdc),2) total, ROUND(AVG(pnl_pct)*100,1) avg_pct
FROM trade_history WHERE type='SELL'
GROUP BY strategy ORDER BY total DESC
```

**Win rate by price range:**
```sql
SELECT
  CASE WHEN price < 0.30 THEN '<30c'
       WHEN price < 0.50 THEN '30-50c'
       WHEN price < 0.65 THEN '50-65c'
       ELSE '>65c' END as range,
  COUNT(*) n,
  ROUND(AVG(CASE WHEN pnl_usdc > 0 THEN 1.0 ELSE 0.0 END)*100,1) win_rate
FROM trade_history WHERE type='SELL'
GROUP BY range ORDER BY range
```

**Worst positions (for pattern analysis):**
```sql
SELECT title, outcome, price, pnl_usdc, pnl_pct, strategy, wallet_names
FROM trade_history WHERE type='SELL'
ORDER BY pnl_pct ASC LIMIT 10
```

**Recent signal counts per strategy (last 24h):**
```sql
SELECT strategy, COUNT(*) signals
FROM signal_history
WHERE saved_at > strftime('%s','now') - 86400
GROUP BY strategy
```
