# TITAN — Strategy Reference

> Complete reference for all three trading strategies in v10.
> Each strategy has its own config block in `titan_config.json` and can be enabled/disabled independently.

---

## Overview

Three strategies run in parallel every cycle. They share the same trade feed but apply different logic to filter, score and size bets. A signal produced by multiple strategies is merged — the higher-scored version wins, and the `strategy` tag is concatenated (e.g. `drift_discount+recent_form`).

```
ACTIVE_STRATEGIES = ["recent_form", "drift_discount", "consensus_basket"]
```

Any strategy can be removed from this list or disabled via its `"enabled": false` flag.

---

## Strategy 1 — Recent Form Copy (`recent_form`)

### Philosophy
Copy wallets that have been profitable in the last 30 days, regardless of their all-time standing. A wallet doesn't need to be "elite" — any verified wallet with positive recent PnL qualifies. The bet is scaled by the source whale's recent win rate.

### Entry Criteria
| Gate | Logic |
|---|---|
| Wallet qualification | Verified OR elite, AND `is_recent_form_qualified()` passes |
| Recent PnL 30d | `recent_pnl_30d >= min_pnl_30d` (default 0 — must be non-negative) |
| Recent PnL 7d | `recent_pnl_7d >= min_pnl_7d` (default −50 — small losses allowed) |
| HFT exclusion | Wallet TPH must be below `max_tph` (default 20) — filters out bots |
| Signal age | Trade must be within `max_signal_age_h` (default 45 min) |
| Price zone | `price_min` ≤ current price ≤ `price_max` (default 18¢–78¢, slightly wider) |
| Slippage | Current price must not be more than `2× MAX_ENTRY_SLIPPAGE` above whale entry |
| EV floor | Raw expected value must be > 0.5% |
| Hedge check | Wallet must not be a known hedge bot |

### Exit
- No stop loss by default (`stop_loss_pct: null`)
- The price ceiling (78¢) acts as the protection instead
- Follows whale exits and profit target normally

### Bet Sizing
- Kelly bet scaled by `source_recent_wr` (estimated from wallet's recent PnL)
- Multiplier: `max(0.8, min(1.6, (wr - 0.50) * 4 + 0.8))`
- A whale with 55% recent win rate → 0.8× multiplier
- A whale with 70% recent win rate → 1.4× multiplier

### Tier Assignment
| Score | Tier |
|---|---|
| ≥ `ALERT_SCORE` | ALERT |
| ≥ `STRONG_SCORE` | STRONG |
| < `STRONG_SCORE` | MEDIUM |

### Config Block (`strategy_recent_form`)
| Key | Default | Description |
|---|---|---|
| `enabled` | `true` | Enable/disable this strategy |
| `max_tph` | `20` | Max trades-per-hour for source wallet (HFT exclusion) |
| `min_pnl_30d` | `0` | Minimum 30-day PnL for source wallet |
| `min_pnl_7d` | `-50` | Minimum 7-day PnL (allows small recent dips) |
| `max_signal_age_h` | `0.75` | Maximum trade age in hours |
| `min_score` | `42` | Minimum score to emit a signal |
| `price_min` | `0.18` | Minimum market price to enter |
| `price_max` | `0.78` | Maximum market price to enter |
| `stop_loss_pct` | `null` | Stop loss (null = no stop) |

---

## Strategy 2 — Drift Discount (`drift_discount`)

### Philosophy
Enter when the current price has dropped 4–12% below the whale's entry price, and the whale is still holding. The whale's thesis hasn't changed — we get the same bet at a discount. This strategy deliberately looks back up to 6 hours, much longer than the other two.

### Entry Criteria
| Gate | Logic |
|---|---|
| Wallet qualification | Verified OR elite |
| Signal age | Trade within `max_signal_age_h` (default 6 hours) |
| Discount direction | Current price must be **below** whale's avg entry |
| Min discount | `(avg_entry − cur) / avg_entry ≥ min_discount_pct` (default 4%) |
| Max discount | Same ratio ≤ `max_discount_pct` (default 12%) — beyond this the market is disagreeing |
| Price zone | `price_min` ≤ current price ≤ `price_max` (default 20¢–72¢) |
| Still holding check | Optionally calls `fetch_wallet_sells()` to confirm whale hasn't exited since buying |
| Hedge check | Wallet must not be a known hedge bot |

### Still-Holding Check (`require_still_holding_check`)
When enabled (default true), TITAN calls the Polymarket API to verify each whale hasn't sold since their buy. If **all** whales have exited → signal rejected. If partial exits → those wallets are removed, signal continues with remaining holders.

### Scoring Formula
This strategy uses a custom formula instead of the shared `score_signal()`:

```
base_score = 60
+ discount_contribution: int(discount * 200)   # 4% → +8, 10% → +20
+ elite_bonus: n_elite_wallets * 8
+ price_zone_bonus: +5 if in ideal zone (25¢–65¢)
= capped at 100
```

### Exit
- No stop loss by default (`stop_loss_pct: null`)
- Whale exit sell still applies
- If price drops below `price_min` after entry, no forced exit — rely on whale mirror

### Bet Sizing
- Kelly bet scaled by discount percentage
- Multiplier: `max(0.9, min(1.5, 1.0 + discount * 5))`
- 4% discount → 1.2× multiplier
- 10% discount → 1.5× multiplier

### Config Block (`strategy_drift_discount`)
| Key | Default | Description |
|---|---|---|
| `enabled` | `true` | Enable/disable this strategy |
| `min_discount_pct` | `0.04` | Minimum price drop below whale entry (4%) |
| `max_discount_pct` | `0.12` | Maximum drop — beyond this the market is disagreeing (12%) |
| `max_signal_age_h` | `6.0` | How far back to look for whale trades |
| `price_min` | `0.20` | Minimum current price |
| `price_max` | `0.72` | Maximum current price |
| `require_still_holding_check` | `true` | Verify whale hasn't sold since buying |
| `stop_loss_pct` | `null` | Stop loss (null = no stop) |

---

## Strategy 3 — Consensus Basket (`consensus_basket`)

### Philosophy
The original TITAN conviction strategy, slightly relaxed. Requires at least one elite wallet (down from two in the old `conviction_only`), enforces the full sequence of gates (drift, EV, age, slippage, fee, stale-loser), but uses a smaller bet cap. This is the most conservative of the three strategies by gate count.

### Entry Criteria (all must pass in order)
| Gate | Logic |
|---|---|
| Elite wallet present | At least `min_elite_confluence` elite wallets (default 1) |
| Counter-whale check | If >60% of elite flow is on the opposite outcome → reject |
| Market fetch | Must resolve market data from Gamma API |
| Price zone | `price_min` ≤ current price ≤ `price_max` (default 20¢–72¢) |
| Sports gate | Sports markets need ≥1 genuine (non-sports-bot) elite |
| Signal age | Elite trade must be within `max_signal_age_h` (default 30 min) |
| Slippage | Current price ≤ whale entry + `MAX_ENTRY_SLIPPAGE` (or HFT variant) |
| Drift | Current price within `[MIN_DRIFT, MAX_DRIFT]` range relative to whale entry |
| EV floor | Raw EV must be > 1% |
| Stale loser gate | Old signal + price already down → reject |
| Fee gate | Net return after fees must be positive; price must be below 96.5¢ |
| Near-expiry | Skip if market closes within 1 hour (unless HFT + large trade) |
| Hedge bot | Must not be a hedge wallet on both sides of the market |

### HFT Spike Promotion
Wallets with a spike trade (`hft_spike_ratio ≥ 20` or `is_large_trade=True`) are temporarily promoted to the elite set even if not normally elite, as long as they are verified or HFT-flagged.

### Tier Assignment
| Condition | Tier |
|---|---|
| HFT signal + large trade | CONVICTION |
| Large trade + score ≥ ALERT_SCORE | CONVICTION |
| Score ≥ ALERT_SCORE | ALERT |
| Score ≥ STRONG_SCORE | STRONG |
| No verified wallets alongside elite | ELITE_ONLY |
| Default | MEDIUM |
| Whale exits detected on ALERT/CONVICTION | Downgraded to STRONG |
| Signal age > `max_age_h` and not CONVICTION | STALE |

### Exit
- Soft stop loss at −35% by default (`stop_loss_pct: -0.35`)
- Full whale exit mirroring applies

### Bet Sizing
- Standard Kelly (no strategy multiplier — flat sizing)
- Hard cap: `max_bet_abs` = 1.20 USD (overrides the global `MAX_BET_ABS` when lower)

### Config Block (`strategy_consensus_basket`)
| Key | Default | Description |
|---|---|---|
| `enabled` | `true` | Enable/disable this strategy |
| `min_elite_confluence` | `1` | Minimum elite wallets required |
| `max_signal_age_h` | `0.5` | Maximum trade age in hours (30 min) |
| `price_min` | `0.20` | Minimum current price |
| `price_max` | `0.72` | Maximum current price |
| `min_score` | `50` | Minimum score to emit a signal |
| `stop_loss_pct` | `-0.35` | Soft stop loss at −35% |
| `max_bet_abs` | `1.20` | Absolute max bet size in USD |

---

## Shared Scoring System (used by Recent Form & Consensus Basket)

Score is computed by `score_signal()` and capped at 100.

| Component | Max | How it's calculated |
|---|---|---|
| Wallet quality | 30 | `avg_wscore × 30` (avg score of elite wallets, 0–1 scale) |
| Confluence | 18 | 4+ whales=18 / 3=14 / 2=10 / 1=6 / 0=0. Elite-only mode floors at 8. Large trade adds +4. |
| Recency | 20 | Hot/HFT window: 20pts if <15min, down to 2pts at >4h. Warm window: 6pts if <4h, 1pt at >8h. HFT signal within mirror delay: +5 (capped at 20). |
| Price window | 15 | Negative drift (price below entry): 8 + abs(drift)×25. Positive drift: 15pts if <4%, down to 0pts if >15%. |
| Market quality | 10 | Liquidity (up to 5pts) + volume (up to 3pts) + time-to-close (0–2pts) |
| Conviction bonus | 5 | 5pts if trade ≥ MASSIVE_TRADE / 2pts if ≥ LARGE_TRADE. Large trade adds +3. |
| Price zone bonus | 7 | +5 if in ideal zone (25¢–65¢) / +2 if in acceptable zone / −10 if outside |
| Multi-whale bonus | 8 | 3+ elite=8pts / 2 elite=5pts / 1=0pts |
| Exit penalty | −8× | −8 per whale on the same side who has already sold |
| Weekly PnL penalty | −10 max | If sum of sourcing whales' 7d PnL is negative: penalty scales from 0 to −10 |

---

## Shared Entry Gates (applied by the dispatcher after all strategies run)

Even after a strategy produces a signal, the dispatcher enforces:

- **Price zone hard block**: current price must be in `[MIN_ENTRY_PRICE, MAX_ENTRY_PRICE]`
- **Hedge bot detection**: if a wallet appears on both YES and NO sides of the same market → all signals from that market are blocked and the wallet is added to `_KNOWN_HEDGE_WALLETS`
- **One outcome per market**: only the highest-scored outcome per conditionId makes it through (dedup)

---

## Bet Sizing — Kelly Formula

All strategies use the same base Kelly calculation:

```
b = (1 / cur_price) - 1 - ROUND_TRIP_FEE          # profit per dollar at risk
kelly = max(0, fair_value - (1 - fair_value) / b)   # Kelly fraction
f_kelly = kelly * KELLY_FRACTION                    # fractional Kelly

score_mult = 0.5 + 0.5 * (score / 100)             # 0.5× at score=0, 1.0× at score=100
conf_mult = min(1.75, 1.0 + (n_elite - 1) * 0.25)  # up to 1.75× at 4+ elites
tier_mult = CONVICTION:1.6 / ALERT:1.2 / STRONG:1.0 / MEDIUM:0.7 / HFT:0.5
strategy_mult = (see per-strategy multiplier above)

raw_bet = bankroll × f_kelly × score_mult × conf_mult × tier_mult × strategy_mult
final_bet = clamp(raw_bet, MIN_BET, min(MAX_BET_ABS, bankroll × MAX_BET_PCT))
```

Adaptive caps tighten automatically when bankroll is small:

| Bankroll | Max bet abs | Max bet % |
|---|---|---|
| < $8 | $1.50 | 15% |
| < $15 | $2.50 | 18% |
| < $30 | $4.00 | 18% |
| ≥ $30 | `MAX_BET_ABS` | `MAX_BET_PCT` |

---

## Strategy Comparison Summary

| | Recent Form | Drift Discount | Consensus Basket |
|---|---|---|---|
| **Wallet requirement** | Verified + recent PnL > 0 | Verified or elite | Elite required |
| **Min confluence** | 1 recent-form wallet | 1 verified wallet | `min_elite_confluence` (default 1) |
| **Signal age window** | 45 min | 6 hours | 30 min |
| **Price zone** | 18¢–78¢ | 20¢–72¢ | 20¢–72¢ |
| **Key gate** | Recent PnL 30d ≥ 0 | Price 4–12% below entry | Full drift/EV/fee/stale gate sequence |
| **Stop loss** | None | None | −35% soft stop |
| **Max bet** | Global cap | Global cap | $1.20 hard cap |
| **Bet multiplier** | Recent win rate (0.8–1.6×) | Discount size (0.9–1.5×) | None (flat) |
| **Scoring** | Shared `score_signal()` | Custom formula (base 60 + discount) | Shared `score_signal()` |
| **Best for** | Hot momentum from recently hot wallets | Entry timing / value entries | Conservative high-conviction plays |
