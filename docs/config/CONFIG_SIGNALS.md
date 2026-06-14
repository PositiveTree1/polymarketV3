# Config Reference — Signal Quality, Scoring & Price Gates

> MCP tools: `get_config_signals` (read) · `update_config_signals` (write)
> Groups: `signal_quality` · `drift_gates` · `price_zone_gates`
> Scoring constants (`strategy_scoring`) are in this file but not editable via MCP (deep nested structure).

---

## Overview

A signal is created when one or more wallets bet on the same market outcome. Before it can trigger a trade, it must pass three layers of gates:

```
Raw wallet trade observation
        │
        ▼  signal_quality gates
   Signal created (score 0–100)
        │
        ▼  price_zone_gates
   Price in 20–72¢ zone?
        │
        ▼  drift_gates
   Price drift from tracked wallet entry acceptable?
        │
        ▼
   Signal passed → auto-trade if tier ≥ ALERT
```

---

## Group: `signal_quality`

Top-level gates applied to every signal regardless of strategy.

| Parameter | Default | Type | Description |
|---|---|---|---|
| `MAX_SIGNAL_AGE_H` | `0.25` | float (hours) | **Key parameter.** Maximum age of the tracked wallet's trade. 0.25 = 15 minutes. Older signals are likely already price-absorbed. |
| `MIN_SCORE` | `55` | float (0–100) | Minimum score to display a signal. Signals below this are silently dropped. |
| `STRONG_SCORE` | `62` | float (0–100) | Score threshold for STRONG tier. |
| `ALERT_SCORE` | `70` | float (0–100) | Score threshold for ALERT tier (auto-traded). |
| `MIN_CONFLUENCE` | `2` | int | Minimum number of qualifying wallets agreeing on the same outcome. 1 = single-wallet allowed. |

**Tuning guide:**
- Getting 0 signals → lower `MIN_SCORE` (try 45) and/or raise `MAX_SIGNAL_AGE_H` (try 0.5)
- Too many low-quality trades → raise `MIN_SCORE` and `MIN_CONFLUENCE`
- Signals appear but nothing auto-trades → check `ALERT_SCORE` vs signal scores in `get_signals`
- Single-wallet signals causing losses → raise `MIN_CONFLUENCE` to 2 or 3

**Important:** Each strategy also has its own `min_score` gate (lower than this global one). The global gate is the final filter — a strategy-level signal must survive both.

---

## Group: `price_zone_gates`

The 20–72¢ zone is the core of TITAN's risk management. It prevents two documented failure patterns:
- Below 20¢ → lottery ticket territory, high variance, low liquidity
- Above 72¢ → near-certainty trap: tiny upside (28¢ max), catastrophic downside if wrong

| Parameter | Default | Type | Description |
|---|---|---|---|
| `MIN_ENTRY_PRICE` | `0.20` | float (0–1) | **Hard floor.** Never enter below 20¢. |
| `MAX_ENTRY_PRICE` | `0.72` | float (0–1) | **Hard ceiling.** Never enter above 72¢. Near-certainty trap above this. |
| `IDEAL_PRICE_MIN` | `0.25` | float (0–1) | Sweet spot lower bound. Signals in 25–65¢ range get +5 score bonus. |
| `IDEAL_PRICE_MAX` | `0.65` | float (0–1) | Sweet spot upper bound. |

**Score effect of price zone:**
```
price in IDEAL zone (0.25–0.65)      → +5 pts
price in ACCEPTABLE zone (0.20–0.72) → +2 pts
price outside zone                   → −10 pts (hard gate also blocks it)
```

**Tuning guide:**
- Missing opportunities in high-confidence markets → raise `MAX_ENTRY_PRICE` slightly (max recommended: 0.80)
- Taking too many losses at low prices → raise `MIN_ENTRY_PRICE` (try 0.25)
- Never change these without understanding the loss history in `_ANALYSIS` section of titan_config.json

---

## Group: `drift_gates`

Drift = (current_price − tracked_wallet_entry_price) / tracked_wallet_entry_price

A positive drift means price rose after the tracked wallet bought (edge partially absorbed).
A negative drift means price fell (discount opportunity, used by drift_discount strategy).

| Parameter | Default | Type | Description |
|---|---|---|---|
| `MAX_DRIFT` | `0.05` | float | Max positive drift from tracked wallet entry. 5% = if price already moved 5% up, edge is gone. |
| `MIN_DRIFT` | `-0.08` | float | Max negative drift. −8% = allow some adverse drift but not too much. |
| `MAX_ENTRY_SLIPPAGE` | `0.03` | float | Max difference between current price and tracked wallet entry at moment of trade. 3% slippage cap. |
| `HFT_MAX_DRIFT` | `0.02` | float | Tighter drift for HFT signals (2% — must be very fresh). |
| `HFT_MIN_DRIFT` | `-0.05` | float | Tighter floor for HFT. |
| `HFT_MAX_ENTRY_SLIPPAGE` | `0.02` | float | Tighter slippage for HFT. |
| `STALE_LOSER_AGE_H` | `0.50` | float (hours) | Exit stale positions that are losing after 30 minutes. |
| `STALE_LOSER_DRIFT` | `-0.08` | float | Drift threshold below which a position is considered a stale loser. |

**Tuning guide:**
- Signals rejected for "drift too high" → raise `MAX_DRIFT` (try 0.08) — but accept more slippage risk
- Missing drift discount entries → lower `MIN_DRIFT` more negative (try −0.15) — but increases momentum risk
- Stale positions holding too long → lower `STALE_LOSER_AGE_H` (try 0.25)

---

## Scoring Constants (`strategy_scoring`)

These control the exact point values for each component of `score_signal()`. They are stored in `titan_config.json` under `strategy_scoring` and loaded as a dict. **Not editable via MCP** — edit `titan_config.json` directly.

### Wallet Quality (max 30 pts)
```
wallet_max_pts = 30
score = avg_wscore × wallet_max_pts
```
`avg_wscore` is the average composite wallet score (0–1) of all contributing wallets.

### Confluence (max 18 pts)
```
confluence_pts = [0, 6, 10, 14, 18]   ← index = number of wallets (capped at 4)
0 wallets → 0    1 wallet → 6    2 wallets → 10    3 wallets → 14    4+ wallets → 18

elite_only_conf_floor = 8              ← if only elite wallets (no verified), floor at 8
large_trade_conf_bonus = 4             ← +4 if any wallet placed a large trade (≥$1000)
```

### Recency (max 20 pts)
```
Hot window thresholds (age_h → pts):
  < 0.25h  → 20    < 0.5h → 17    < 1h → 14    < 2h → 9    < 3h → 6    < 4h → 4    else → 2

Warm window thresholds:
  < 4h → 6    < 6h → 4    < 8h → 2    else → 1

HFT signal within mirror delay: +5 (capped at 20)
```

### Price Window (max 15 pts)
```
Negative drift (price below entry — discount):
  pts = min(opp_neg_drift_max, opp_neg_drift_base + abs(drift) × opp_neg_drift_scale)
  defaults: base=8, scale=25, max=15

Positive drift thresholds (price above entry — edge absorbed):
  < 4%  → 15    < 8%  → 12    < 12% → 8    < 15% → 4    else → 0
```

### Market Quality (max 10 pts)
```
Liquidity:  min(liq_quality_max, liq / liq_quality_scale)   → max 5 pts  (scale=$8000)
Volume:     min(vol_quality_max, vol / vol_quality_scale)   → max 3 pts  (scale=$40000)
Time:       72h+ left → 2pts   24–72h → 1pt   <24h → 0pts
```

### Conviction & Multi-Wallet Bonuses
```
conviction_bonus_massive = 5    ← trade ≥ MASSIVE_TRADE ($5000)
conviction_bonus_large   = 2    ← trade ≥ LARGE_TRADE ($1000)
large_trade_bonus        = 3    ← signal contains a large trade

multi_wallet_pts = [0, 0, 5, 8]  ← 0/1/2/3+ elite wallets → 0/0/5/8 pts
```

### Penalties
```
exit_penalty_per_wallet   = −8   ← per tracked wallet already selling this outcome
weekly_pnl_penalty_min   = −10  ← max penalty if source wallets have negative 7d PnL
```

### Price Zone Bonuses
```
price_zone_ideal_bonus      = +5   ← price in 0.25–0.65 range
price_zone_acceptable_bonus = +2   ← price in 0.20–0.72 range
price_zone_outside_penalty  = −10  ← price outside 0.20–0.72 (hard gate also fires)
```

---

## Quick Diagnostics

```
get_config_signals          → all current gate values
get_signals(min_score=0)    → see all signals including those just below ALERT threshold
get_rejects                 → rejection reasons — "score too low", "price out of zone", etc.
get_snapshot                → [REJECTIONS] section lists last-cycle rejects with reasons
```
