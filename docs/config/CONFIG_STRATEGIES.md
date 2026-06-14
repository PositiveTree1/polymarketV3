# Config Reference — Strategy Parameters

> MCP tools: `get_config_strategies` (read) · `update_config_strategies` (write)
> Strategy arg: `recent_form` · `drift_discount` · `consensus_basket` · `open_book`
> Full strategy logic: [../TITAN_STRATEGIES.md](../TITAN_STRATEGIES.md)

---

## Overview

Three strategies run in parallel every cycle. Each produces signals independently. When the same market appears in multiple strategies, the higher-scored version wins and the strategy tag is concatenated (e.g. `drift_discount+recent_form`).

```
Active strategies: ["recent_form", "drift_discount", "consensus_basket"]
```

To disable a strategy: `update_config_strategies strategy=recent_form patch={"enabled": false}`

---

## Global Strategy Settings

Stored in the `strategy` config block. Edit `titan_config.json` directly (not via MCP update tools).

| Parameter | Default | Description |
|---|---|---|
| `ACTIVE_STRATEGIES` | `["recent_form","drift_discount","consensus_basket"]` | Which builders run each cycle |
| `TRADEABLE_TIERS_LIST` | `["CONVICTION","ALERT"]` | Only these tiers trigger auto-trades |
| `ALLOWED_MARKET_TYPES` | `["POLITICS","EVENT"]` | Market categories allowed. CRYPTO removed (bot-dominated). |
| `MIN_ELITE_CONFLUENCE` | `2` | Global minimum elite wallets for CONVICTION tier |
| `BLOCK_SPORTS` | `true` | Reject all sports markets unless MIN_SPORTS_CONFLUENCE met |
| `MIN_SPORTS_CONFLUENCE` | `4` | Required elites for sports (rarely met → effectively blocked) |
| `SPORTS_STOP_LOSS_PCT` | `-0.15` | Tighter stop for any sports trade that gets through |
| `SPORTS_MAX_BET_PCT` | `0.03` | Max 3% bankroll on sports |
| `PROFIT_TARGET_ENABLED` | `true` | Enable profit target exits |

---

## Strategy 1: `recent_form`

**Philosophy:** Copy wallets that have been profitable in the last 30 days. Does not require "elite" status — any verified wallet with positive recent PnL qualifies. HFT bots explicitly excluded.

| Parameter | Default | Type | Description |
|---|---|---|---|
| `enabled` | `true` | bool | Enable/disable this strategy entirely |
| `max_tph` | `20` | int | Max trades-per-hour for source wallet. Excludes HFT bots (which trade 50–200 TPH). |
| `min_pnl_30d` | `0` | float ($) | Min 30-day PnL. 0 = must not be losing money recently. |
| `min_pnl_7d` | `-50` | float ($) | Min 7-day PnL. −50 allows small recent dips (weekly variance). |
| `max_signal_age_h` | `0.75` | float (hours) | Max trade age. 45 min — wider than global 15 min since recent_form IS the freshness filter. |
| `min_score` | `42` | float | Min score to emit. Lower than global `MIN_SCORE` (55) — recency is the quality gate here. |
| `price_min` | `0.18` | float (0–1) | Min entry price. Slightly below global floor (18¢ vs 20¢). |
| `price_max` | `0.78` | float (0–1) | Max entry price. Slightly above global ceiling (78¢ vs 72¢). |
| `max_positions` | `4` | int | Max simultaneous positions from this strategy. |
| `bet_multiplier_base` | `1.0` | float | Base Kelly multiplier. |
| `stop_loss_pct` | `null` | float or null | No stop loss. Price ceiling at 78¢ is the protection. |

**Bet sizing note:**
The multiplier adjusts based on the source wallet's recent win rate:
```
multiplier = max(0.8, min(1.6, (recent_wr - 0.50) × 4 + 0.8))
→ 55% WR wallet → 0.8× multiplier
→ 70% WR wallet → 1.4× multiplier
```

**Tuning guide:**
- No recent_form signals → lower `min_pnl_30d` (try −100) or raise `max_signal_age_h` (try 1.0)
- Too many low-quality signals → raise `min_pnl_30d` (try 50) or lower `max_tph` (try 10)
- Strategy over-trading → lower `max_positions` (try 2)

---

## Strategy 2: `drift_discount`

**Philosophy:** Enter when the current price has dropped 4–12% below the whale's entry price, and the whale is still holding. We get the same bet at a discount. Looks back up to 6 hours — much longer than other strategies.

| Parameter | Default | Type | Description |
|---|---|---|---|
| `enabled` | `true` | bool | Enable/disable this strategy |
| `min_discount_pct` | `0.04` | float (0–1) | Minimum price drop below whale entry. 4% = meaningful discount. |
| `max_discount_pct` | `0.12` | float (0–1) | Maximum drop. Beyond 12% the market is likely disagreeing with the whale, not just dipping. |
| `max_signal_age_h` | `6.0` | float (hours) | How far back to look for whale trades. Long window is deliberate — looking for held positions. |
| `price_min` | `0.20` | float (0–1) | Min current price (at time of our entry). |
| `price_max` | `0.72` | float (0–1) | Max current price. |
| `max_positions` | `3` | int | Max simultaneous positions from this strategy. |
| `require_still_holding_check` | `true` | bool | Verify whale hasn't sold since their buy. API call per signal. |
| `stop_loss_pct` | `null` | float or null | No stop loss. Entry discount + price ceiling = protection. |

**Scoring formula (custom — not shared score_signal):**
```
base = 60
+ int(discount × 200)     → 4% discount → +8,  10% → +20
+ n_elite_wallets × 8     → 1 elite → +8,  2 elites → +16
+ 5 if price in ideal zone (0.25–0.65)
= capped at 100
```

**Bet sizing note:**
```
multiplier = max(0.9, min(1.5, 1.0 + discount × 5))
→ 4% discount → 1.2×    10% discount → 1.5×
```

**Tuning guide:**
- No drift_discount signals → lower `min_discount_pct` (try 0.02) or raise `max_discount_pct` (try 0.18)
- Whale already sold by the time we check → `require_still_holding_check=true` is correct behaviour (signal correctly rejected)
- Too many 6-hour-old signals → lower `max_signal_age_h` (try 3.0)

---

## Strategy 3: `consensus_basket`

**Philosophy:** The most conservative strategy. Requires at least 1 elite wallet, enforces the full gate sequence (drift, EV, fee, slippage, stale loser), uses smaller bets. Statistically defensible through volume — many small bets with >50% accuracy smooth out variance.

| Parameter | Default | Type | Description |
|---|---|---|---|
| `enabled` | `true` | bool | Enable/disable this strategy |
| `min_elite_confluence` | `1` | int | Minimum elite wallets required. Can raise to 2 for higher confidence. |
| `max_signal_age_h` | `0.5` | float (hours) | Max trade age. 30 min — stricter than recent_form. |
| `price_min` | `0.20` | float (0–1) | Min entry price. |
| `price_max` | `0.72` | float (0–1) | Max entry price. |
| `min_score` | `50` | float | Min score to emit. |
| `max_positions` | `5` | int | Max simultaneous positions from this strategy. |
| `max_bet_abs` | `1.20` | float ($) | Hard absolute bet cap. Overrides global `MAX_BET_ABS` when lower. |
| `stop_loss_pct` | `-0.35` | float | Soft stop at −35%. Harder stop than recent_form/drift_discount. |
| `conviction_portfolio_pct` | `0.005` | float (0–1) | Trade ≥ 0.5% of whale's portfolio triggers CONVICTION tier. |
| `opposition_ratio_block` | `0.60` | float (0–1) | If ≥60% of elite flow is on the opposing outcome → reject signal entirely. |

**Tier assignment (unique to consensus_basket):**
```
HFT spike + large trade       → CONVICTION
large trade + score ≥ ALERT   → CONVICTION
score ≥ ALERT_SCORE           → ALERT
score ≥ STRONG_SCORE          → STRONG
only elite wallets present    → ELITE_ONLY
signal stale + not CONVICTION → STALE
```

**Tuning guide:**
- consensus_basket not trading → check `min_elite_confluence` — if no elites in roster, no signals
- Too conservative (low bet sizes) → raise `max_bet_abs` (try 2.0) or lower `min_score` (try 42)
- Too many small losing bets → raise `min_score` (try 60) or `min_elite_confluence` (try 2)
- Counter-whale blocking too many signals → lower `opposition_ratio_block` (try 0.75)

---

## Strategy 4: `open_book` (DISABLED)

Currently disabled. Requires 3+ elite wallets currently holding the same outcome. Needs confirmed API rate limit headroom.

| Parameter | Default | Description |
|---|---|---|
| `enabled` | `false` | Currently disabled |
| `min_consensus` | `3` | Min elites currently holding same outcome |
| `wallets_per_cycle` | `2` | Wallets to scan for open positions per cycle (API limit management) |
| `price_min` | `0.25` | Min price |
| `price_max` | `0.70` | Max price |
| `min_hours_left` | `6.0` | Market must have ≥6h left |
| `max_positions` | `2` | Max positions |
| `stop_loss_pct` | `null` | No stop loss |

To enable: `update_config_strategies strategy=open_book patch={"enabled": true}`
But first verify rate limit capacity — this strategy makes additional API calls every cycle.

---

## Signal Builders Registry

The `signal_builders` config block controls which builders are instantiated at startup.

```json
"active_builders": ["consensus_basket", "recent_form", "drift_discount"]
```

Each entry in `active_builders` must have a matching entry in `builders` with its full parameter set. Changes to `builders` entries hot-reload on the next cycle.

---

## Quick Diagnostics

```
get_config_strategies                      → see all strategy params and active list
get_signals(min_score=0)                   → each signal has a "strategy" field showing which builder produced it
get_snapshot                               → [SIGNALS] section tags each signal with strategy
get_rejects                                → rejection reasons trace back to specific strategy gates
```
