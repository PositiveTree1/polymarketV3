# Config Reference — Bankroll, Bet Sizing, Market Quality & Trade Sourcing

> MCP tools: `get_config_sizing` (read) · `update_config_sizing` (write)
> Groups: `bankroll_and_sizing` · `sizing` · `market_quality`
> Trade sourcing and cache params: `get_config_sourcing` (read-only via MCP)

---

## Group: `bankroll_and_sizing`

Controls the overall bankroll and bet size envelope.

| Parameter | Default | Type | Description |
|---|---|---|---|
| `BANKROLL_START` | `20.0` | float ($) | Starting paper bankroll. Used to calculate total P&L. Does not reset unless manually changed. |
| `MIN_BET` | `1.0` | float ($) | Minimum bet size. Trades below this are rounded up. |
| `MAX_BET_ABS` | `4.0` | float ($) | Hard absolute cap per trade. No single bet exceeds this regardless of Kelly formula. |
| `MAX_BET_PCT` | `0.18` | float (0–1) | Max bet as % of current bankroll. 18% = at $20 bankroll, max is $3.60. Protects from oversizing. |
| `KELLY_FRACTION` | `0.20` | float (0–1) | Fractional Kelly multiplier. 0.2 = 1/5 Kelly — conservative to account for model uncertainty. |

**How MAX_BET_ABS and MAX_BET_PCT interact:**
```
final_bet = clamp(kelly_bet, MIN_BET, min(MAX_BET_ABS, bankroll × MAX_BET_PCT))
→ at $20 bankroll: max = min($4.00, $20 × 0.18) = min($4.00, $3.60) = $3.60
→ at $30 bankroll: max = min($4.00, $30 × 0.18) = min($4.00, $5.40) = $4.00
```

**Adaptive caps override these when bankroll is small** (see [CONFIG_RISK.md](CONFIG_RISK.md#adaptive-caps)):
- < $8 bankroll → max $1.50 / 15%
- < $15 bankroll → max $2.50 / 18%

**Tuning guide:**
- Bets too small to matter → raise `MAX_BET_ABS` (try 6.0) or `MAX_BET_PCT` (try 0.25)
- Losing too much per trade → lower `MAX_BET_ABS` (try 2.0) or tighten `KELLY_FRACTION` (try 0.15)
- Kelly formula producing very small bets → lower `MIN_BET` (try 0.50) or raise `KELLY_FRACTION` (try 0.30)

**KELLY_FRACTION calibration:**
- `0.10` = very conservative — 1/10 Kelly, minimal variance
- `0.20` = default — 1/5 Kelly, balanced
- `0.25` = moderate — 1/4 Kelly
- `0.50` = half Kelly — for high-confidence periods only
- `1.00` = full Kelly — mathematically optimal long-run but high variance, not recommended

---

## Group: `sizing`

| Parameter | Default | Type | Description |
|---|---|---|---|
| `USE_PROPORTIONAL_SIZING` | `false` | bool | If true, scales bet proportionally to tracked wallet's bet relative to their portfolio. Disabled — wrong for small bankrolls. |
| `PROPORTIONAL_WEIGHT` | `0.0` | float (0–1) | Weight of proportional component (0 = fully disabled). |

Leave both at their defaults unless testing a new sizing model.

---

## Group: `market_quality`

Hard filters applied before entering any trade. A market that fails any of these gates is rejected regardless of signal quality.

| Parameter | Default | Type | Description |
|---|---|---|---|
| `MIN_LIQUIDITY` | `15000.0` | float ($) | Minimum liquidity (outstanding yes+no volume). $15k ensures meaningful markets with tight spreads. |
| `MIN_VOLUME` | `30000.0` | float ($) | Minimum total traded volume. $30k filters out low-activity markets. |
| `MIN_HOURS_LEFT` | `4.0` | float (hours) | Market must have at least 4 hours before resolution. Prevents entering dying markets. |

**Why these matter:**
- Low liquidity → wide bid-ask spread → slippage on entry and exit
- Low volume → price manipulation risk, thin order book
- Near expiry → no time for the thesis to play out; also, fee gates become harder to pass

**Tuning guide:**
- Signals rejected for "low liquidity" → lower `MIN_LIQUIDITY` (try 5000) — accept higher slippage
- Missing early opportunities in new markets → lower `MIN_VOLUME` (try 10000)
- Missing short-dated high-confidence markets → lower `MIN_HOURS_LEFT` (try 2.0)
- Check: `get_rejects` often shows "liquidity below threshold" or "hours left" as rejection reasons

---

## Group: `fees`

| Parameter | Default | Type | Description |
|---|---|---|---|
| `TAKER_FEE_RATE` | `0.0` | float (0–1) | Polymarket taker fee. Currently 0. If fees are reintroduced, set to actual rate (e.g. 0.002 for 0.2%). |

`ROUND_TRIP_FEE` is computed automatically as `TAKER_FEE_RATE × 2` (in + out). The Kelly formula uses this to adjust the EV calculation.

---

## Trade Sourcing & Cache Parameters

Read via `get_config_sourcing`. Edit `titan_config.json` directly to change.

### Trade Sourcing

| Parameter | Default | Description |
|---|---|---|
| `MIN_TRADE_CASH` | `200` | Minimum trade size ($) to consider from the public feed. $200 filters noise. |
| `MAX_TRADES_FETCH` | `300` | Max trades to fetch per public feed cycle. |
| `HOT_HOURS` | `1` | Trades in the last N hours are "hot" (get full recency score). |
| `WARM_HOURS` | `1` | Same as HOT_HOURS currently — only want fresh signals. |
| `CYCLE_SECONDS` | `15` | Main loop interval in seconds. 15s = 4 cycles per minute. |

**Tuning guide:**
- Missing small-size wallet signals → lower `MIN_TRADE_CASH` (try 50) — but more noise
- API rate limit pressure → raise `CYCLE_SECONDS` (try 30) or lower `MAX_TRADES_FETCH` (try 150)
- Stale signal problem → lower `HOT_HOURS` / `WARM_HOURS` (already at minimum = 1)

### Discovery

| Parameter | Default | Description |
|---|---|---|
| `DISCOVERY_INTERVAL_CYCLES` | `20` | Run wallet discovery every N cycles. At 15s/cycle = every 5 minutes. |

### Cache

| Parameter | Default | Description |
|---|---|---|
| `WALLET_TTL` | `600` | Wallet profile cache TTL in seconds (10 minutes). |
| `MARKET_TTL` | `30` | Market metadata cache TTL in seconds. |
| `ACTIVITY_LIMIT` | `500` | Max trade history entries per wallet in memory. |

---

## Quick Diagnostics

```
get_config_sizing           → bankroll, bet caps, market quality
get_config_sourcing         → trade sourcing, discovery, cache, VIP wallets
get_pnl_summary             → current bankroll and equity
get_trade_stats             → avg_win, avg_loss — compare to MIN_BET to see if bets are sized sensibly
get_rejects                 → "liquidity below threshold", "volume below threshold" rejection reasons
```
