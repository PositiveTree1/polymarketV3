# Config Reference — Risk Management, Position Management & Kelly Sizing

> MCP tools: `get_config_risk` (read) · `update_config_risk` (write)
> Groups: `position_management` · `timing`
> Kelly constants (`strategy_kelly`) and `position_management_ext` are read via `get_config_risk` but not editable via MCP — edit `titan_config.json` directly.

---

## Overview

Risk management in TITAN has three layers:

```
1. Position limits          ← how many positions, per tracked wallet, per event
2. Exit triggers            ← when to close a position
3. Kelly sizing             ← how much to bet on each signal
```

All three interact. Tight position limits + conservative Kelly = very small exposure per cycle. Wide limits + aggressive Kelly = large concentrated bets.

---

## Group: `position_management`

| Parameter | Default | Type | Description |
|---|---|---|---|
| `MAX_OPEN_POSITIONS` | `5` | int | Hard cap on simultaneous open positions across all strategies. |
| `MAX_POSITIONS_PER_EVENT` | `1` | int | Only 1 position per Polymarket event (prevents doubling up on correlated outcomes). |
| `MAX_POSITIONS_PER_WALLET` | `2` | int | A single elite wallet can appear in at most 2 open positions (recent_form + consensus_basket). |
| `MAX_WATCHLIST_SIZE` | `400` | int | Maximum wallets in the active monitoring pool. |
| `PROFIT_TARGET_PCT` | `0.40` | float (0–1) | Auto-sell at +40% gain even if the tracked wallet is still holding. Locks in gains. |
| `WALLET_EXIT_SELL` | `true` | bool | Mirror tracked wallet exits after the min-hold guard. Not every detected exit fires: HFT/hedge exits are ignored, and some early-loss noise exits are suppressed. |
| `STOP_LOSS_ENABLED` | `true` | bool | **Must stay true.** Prior sessions without stop-losses saw −97%, −99% losses. |
| `STOP_LOSS_PCT` | `-0.30` | float (negative) | Global stop-loss floor at −30%. Note: recent_form and drift_discount override to `null` per their strategy config. |

**Tuning guide:**
- Positions filling up, missing good signals → raise `MAX_OPEN_POSITIONS` (try 7)
- Correlated losses on same event → keep `MAX_POSITIONS_PER_EVENT` at 1
- Exiting winners too early → raise `PROFIT_TARGET_PCT` (try 0.60)
- Taking too many −30% losses → tighten `STOP_LOSS_PCT` (try −0.20) — but more false exits
- Never set `STOP_LOSS_ENABLED=false` — this was the primary cause of catastrophic losses

**Per-strategy stop-loss override:**
Each strategy can set its own `stop_loss_pct`. The per-strategy value takes priority:
- `recent_form`: `null` (no stop — price ceiling is protection)
- `drift_discount`: `null` (entry discount is protection)
- `consensus_basket`: `−0.35` (soft stop — 35% loss triggers exit)
- Global `STOP_LOSS_PCT`: `−0.30` applies only when strategy value is not null

**Important behavior note:**
- TITAN does not rely only on profit target and stop-loss.
- It also checks tracked-wallet exits, market resolution/resolving state, expiring-soon markets, catastrophic loss, and stale trend reversal.
- `MIN_HOLD_MINUTES` is checked first and blocks all exits until the hold threshold is reached.

---

## Group: `timing`

| Parameter | Default | Type | Description |
|---|---|---|---|
| `MIN_HOLD_MINUTES` | `5` | float (minutes) | Minimum hold time before any exit can fire. Prevents selling on noise immediately after entry. |
| `EXIT_COOLDOWN_SECONDS` | `600` | int (seconds) | After closing a position on a market, wait this long before re-entering. 10 minutes. |

**Tuning guide:**
- Tracked wallet exit happening immediately after entry (noise) → raise `MIN_HOLD_MINUTES` (try 10)
- Missing re-entry opportunities after good exits → lower `EXIT_COOLDOWN_SECONDS` (try 300)
- Re-entering too quickly on the same bad market → raise `EXIT_COOLDOWN_SECONDS` (try 1800)

---

## Position Management Extensions (`position_management_ext`)

Not editable via MCP. Edit `titan_config.json` directly.

| Parameter | Default | Description |
|---|---|---|
| `wallet_exit_min_sell_fraction` | `0.30` | A tracked wallet must sell ≥30% of their position to trigger an exit signal. Prevents overreacting to partial profit-taking. |

---

## Kelly Sizing Constants (`strategy_kelly`)

The Kelly formula determines bet size based on signal quality, confidence, and current bankroll. These constants shape how aggressively the formula scales.

Not editable via MCP. Edit `titan_config.json` directly.

### Score Multiplier
Scales Kelly by signal score. Higher-scored signals get larger bets.
```
score_mult = score_mult_base + score_mult_scale × (score / 100)
           = 0.5 + 0.5 × (score / 100)
→ score=55 → 0.775×    score=70 → 0.85×    score=90 → 0.95×
```

| Parameter | Default | Description |
|---|---|---|
| `score_mult_base` | `0.5` | Minimum multiplier (at score=0) |
| `score_mult_scale` | `0.5` | Range of multiplier (0.5 base + 0.5 scale = 1.0× at score=100) |

### Confluence Multiplier
Scales Kelly by number of elite wallets agreeing. More elites = bigger bet.
```
conf_mult = min(conf_mult_cap, 1.0 + (n_elite − 1) × conf_mult_step)
→ 1 elite → 1.0×    2 elites → 1.25×    3 elites → 1.5×    4+ elites → 1.75×
```

| Parameter | Default | Description |
|---|---|---|
| `conf_mult_step` | `0.25` | Per-elite increment |
| `conf_mult_cap` | `1.75` | Maximum confluence multiplier |

### Tier Multipliers
Each signal tier gets a fixed multiplier applied to the base Kelly bet.

| Tier | Multiplier | Description |
|---|---|---|
| `CONVICTION` | `1.6` | Highest conviction — tracked wallet commits large size |
| `ALERT` | `1.2` | Standard auto-trade tier |
| `STRONG` | `1.0` | Not auto-traded (display only) |
| `MEDIUM` | `0.7` | Lower confidence |
| `ELITE_ONLY` | `0.9` | Only elite wallets, no verified confluence |
| `HFT` | `0.5` | HFT spike — smallest multiplier, most uncertain |

### Score Floor
Prevents the Kelly bet from being too small to matter at lower scores.
```
score_floor = score_floor_mult × (score / score_floor_base_ref) × score_floor_scale
```

| Parameter | Default | Description |
|---|---|---|
| `score_floor_base_ref` | `55` | Reference score (minimum useful score) |
| `score_floor_scale` | `45` | Scaling factor |
| `score_floor_mult` | `1.5` | Multiplier |

### Large Trade Boost
When a signal contains a tracked wallet's large trade, the bet gets a small boost.
```
large_trade_max_abs_boost  = 1.50   ← max absolute $$ increase from large trade
large_trade_bankroll_cap   = 0.22   ← still capped at 22% of bankroll after boost
```

### Adaptive Caps (small bankroll protection)
When bankroll is small, caps tighten automatically to prevent ruin.

| Bankroll | Max bet ($) | Max bet (%) |
|---|---|---|
| < $8 | $1.50 | 15% |
| < $15 | $2.50 | 18% |
| < $30 | $4.00 | 18% |
| ≥ $30 | `MAX_BET_ABS` | `MAX_BET_PCT` |

Stored in config as:
```json
"adaptive_caps": [[8, 1.5, 0.15], [15, 2.5, 0.18], [30, 4.0, 0.18]]
```

### Full Kelly Formula
```
b = (1 / cur_price) − 1 − ROUND_TRIP_FEE
kelly = max(0, fair_prob − (1 − fair_prob) / b)
f_kelly = kelly × KELLY_FRACTION

raw_bet = bankroll × f_kelly × score_mult × conf_mult × tier_mult × strategy_mult
final_bet = clamp(raw_bet, MIN_BET, min(adaptive_cap, bankroll × adaptive_pct))
```

---

## Exit Logic Summary

Exits are checked every cycle for every open position:

| Priority | Trigger | Condition | Action |
|---|---|---|---|
| 1 | Min hold guard | Position held < `MIN_HOLD_MINUTES` | Block all exits |
| 2 | WebSocket resolution | Resolution monitor confirms market result | Immediate sell |
| 3 | Market resolving | Market appears to be resolving | Sell |
| 4 | Wallet exit | Tracked non-HFT/non-hedge wallet sells enough of the position | Sell (if `WALLET_EXIT_SELL=true`) |
| 5 | Profit target | P&L ≥ `PROFIT_TARGET_PCT` | Sell (if `PROFIT_TARGET_ENABLED=true`) |
| 6 | Strategy/global stop-loss | P&L ≤ effective stop loss | Sell |
| 7 | Expiring soon | Market hours left below the configured guard | Sell |
| 8 | Resolution/gone fallback | Market fetch repeatedly fails but resolution or market-gone state is inferred | Sell |
| 9 | Resolved loss | Fresh price near zero after hold time | Sell |
| 10 | Catastrophic loss guard | P&L ≤ −70% after 3+ minutes | Sell |
| 11 | Stale trend reversal | 45+ min old, 4-point downtrend, >8% drop, and losing >15% | Sell |

---

## Quick Diagnostics

```
get_config_risk             → all current risk parameters
get_positions               → open positions with current P&L and hold time
get_trade_stats             → win_rate, avg_win, avg_loss, expectancy
get_snapshot                → [OPEN POSITIONS] section with live P&L
get_recent_errors           → any stop-loss or exit failures
```
