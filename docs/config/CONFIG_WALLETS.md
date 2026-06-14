# Config Reference — Wallet Selection & Elite Classification

> MCP tools: `get_config_wallets` (read) · `update_config_wallets` (write)
> Groups: `wallet_quality` · `elite_thresholds` · `elite_polling`
> Selector config is read-only via MCP (requires code change to add a new selector type).

---

## Overview

Wallet classification is a 3-tier funnel:

```
All wallets seen in feed
        │
        ▼ wallet_quality gates
   watchable=True          ← monitored, not yet trusted
        │
        ▼ higher wallet_quality gates
   verified=True           ← trusted enough to copy
        │
        ▼ elite_thresholds gates
   elite=True              ← highest conviction, required by consensus_basket
```

A wallet flagged `hft=True` is a separate classification — it trades too fast to copy safely and is excluded from recent_form signals (max_tph gate).

---

## Group: `wallet_quality`

Controls who enters the watchable and verified tiers.

| Parameter | Default | Type | Description |
|---|---|---|---|
| `MIN_WIN_RATE_WATCH` | `0.53` | float (0–1) | Minimum win rate to become watchable. 53% = slightly above coin flip. |
| `MIN_WIN_RATE_VER` | `0.56` | float (0–1) | Minimum win rate to become verified. Must exceed watchable threshold. |
| `MIN_RESOLVED_BETS` | `10` | int | Minimum number of resolved (closed) bets. Prevents small-sample flukes. |
| `MIN_PNL` | `0.0` | float ($) | Minimum lifetime PnL. 0 = any profitable wallet qualifies. |
| `WILSON_MIN_WATCH` | `0.45` | float (0–1) | Wilson lower-bound confidence floor for watchable. Statistically accounts for small samples. |
| `WILSON_MIN_VER` | `0.49` | float (0–1) | Wilson lower-bound confidence floor for verified. |
| `MIN_AVG_PROFIT_PER_TRADE` | `2.0` | float ($) | Average dollar profit per resolved trade. Filters wallets with many tiny wins. |
| `MIN_AVG_BET_SIZE` | `10.0` | float ($) | Average bet size. Filters out small-stake wallets whose signals are noisy. |

**Wilson Lower Bound** — why it matters:
A wallet with 8/10 wins (80%) looks great, but 10 bets is a tiny sample. Wilson LB for 8/10 at 95% CI ≈ 0.49. That same wallet needs 20+ bets before the LB reaches 0.55. This prevents copying a lucky newcomer.

**Tuning guide:**
- Too few watchable wallets → lower `MIN_WIN_RATE_WATCH` or `WILSON_MIN_WATCH`
- Too many low-quality signals → raise `MIN_AVG_PROFIT_PER_TRADE` or `MIN_RESOLVED_BETS`

---

## Group: `elite_thresholds`

Controls who gets the `elite=True` flag. Elite wallets are required by `consensus_basket` and boost signal scoring significantly (+8 pts per elite in drift_discount).

| Parameter | Default | Type | Description |
|---|---|---|---|
| `ELITE_MIN_PNL` | `40000.0` | float ($) | Minimum lifetime PnL. $40k proves sustained profitability. |
| `ELITE_MIN_PORT` | `80000.0` | float ($) | Minimum current portfolio value. Ensures the wallet is still active and meaningful. |
| `ELITE_MIN_SCORE` | `0.72` | float (0–1) | Minimum composite wallet score (weighted Wilson + PnL + portfolio + trade count + alpha). |
| `ELITE_MIN_RESOLVED` | `20` | int | Minimum resolved bets. More than wallet_quality (10) — requires proven track record. |

**Tuning guide:**
- Too few elites (0–3 in roster) → lower `ELITE_MIN_PNL` (try 25000) or `ELITE_MIN_PORT` (try 50000)
- Elite roster filling with low-quality wallets → raise `ELITE_MIN_SCORE` (try 0.75)
- Check current roster: `get_config_wallets` → `wallet_selector.elite_min_pnl` shows live selector values

---

## Group: `elite_polling`

Controls how often and how deeply elite wallets are polled.

| Parameter | Default | Type | Description |
|---|---|---|---|
| `ELITE_POLL_LIMIT` | `100` | int | Max trades to fetch per elite wallet per cycle. |
| `ELITE_POLL_MIN_CASH` | `50.0` | float ($) | Minimum trade size to consider from an elite wallet. $50 filters out small test bets. |
| `ELITE_TRADE_MIN_FRACTION` | `0.03` | float (0–1) | Minimum trade as fraction of wallet's average bet. 3% prevents copying dust trades. |

**Tuning guide:**
- Missing elite signals → lower `ELITE_POLL_MIN_CASH` (try 20)
- Too many low-value elite signals → raise `ELITE_TRADE_MIN_FRACTION` (try 0.05)

---

## Wallet Selector — Performance Selector Parameters

The active selector is `performance`. Its full parameter set (not editable via MCP — requires config file edit):

| Parameter | Default | Description |
|---|---|---|
| `discovery_use_large_trades` | `true` | Discover new wallets from large public trades |
| `discovery_large_trade_limit` | `200` | Max large trades to scan per discovery cycle |
| `min_trade_cash_discovery` | `5000.0` | Only flag wallets placing ≥$5k trades for discovery |
| `discovery_trade_side` | `"BUY"` | Only discover from buy trades (not exits) |
| `discovery_use_leaderboard` | `true` | Also discover from Polymarket leaderboard |
| `discovery_leaderboard_limit` | `100` | Top N wallets from leaderboard |
| `discovery_leaderboard_category` | `"OVERALL"` | Leaderboard category |
| `discovery_leaderboard_order_by` | `"PNL"` | Sort leaderboard by PnL |
| `leaderboard_periods` | `["ALL","MONTH","WEEK"]` | Which time windows to pull |
| `weight_wilson` | `0.30` | Weight of Wilson LB in composite score |
| `weight_pnl_pct` | `0.25` | Weight of PnL percentage in composite score |
| `weight_portfolio` | `0.15` | Weight of portfolio value |
| `weight_trade_count` | `0.10` | Weight of trade count |
| `weight_open_positions` | `0.10` | Weight of open positions |
| `weight_alpha` | `0.10` | Weight of alpha per trade |
| `hft_tph_threshold` | `50.0` | Trades-per-hour above this → classified as HFT bot |
| `sports_bot_tph_threshold` | `100.0` | TPH above this in sports markets → sports bot flag |

**Scoring formula:**
```
score = (wilson_lb × 0.30) + (pnl_pct_norm × 0.25) + (portfolio_norm × 0.15)
      + (trade_count_norm × 0.10) + (open_pos_norm × 0.10) + (alpha_norm × 0.10)
```
All components are normalised to 0–1 before weighting.

---

## VIP Wallets (always polled, every cycle)

These 6 wallets are polled on every single cycle regardless of watchlist rotation:

| Name | Address | Notes |
|---|---|---|
| MEPP | `0x6d9fc316...` | $299k alpha trader |
| 0x8dxd | `0x63ce3421...` | $2.21M |
| Wickier | `0x1cc16713...` | — |
| mr.ozi | `0x614dc8d3...` | Alpha trader |
| nojnn | `0x7f9e2d1d...` | Alpha trader |
| Clear-Corridor | `0xdf17f4a8...` | Alpha trader |

To add a VIP wallet, edit `vip_wallets.wallets` in `titan_config.json` directly (not via MCP).

---

## Quick Diagnostics

```
get_config_wallets          → see all current thresholds
get_tracked_wallets         → see live wallet roster with tier flags
get_snapshot                → elite roster section shows active elites with scores
get_rejects                 → rejection reasons often reference wallet quality gates
```
