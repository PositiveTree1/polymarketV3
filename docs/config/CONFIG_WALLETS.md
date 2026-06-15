# Config Reference — Wallet Selection & Elite Classification

> MCP tools: `get_config_wallets` (read) · `update_config_wallets` (write) · `reeval_wallets` (force re-classify)
> Groups: `wallet_quality` · `elite_thresholds` · `elite_polling` · `wallet_selector`

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

## What `WATCH` Means

`WATCH` is the first tracked-wallet status. It means TITAN has found a wallet worth monitoring, but the wallet is not yet trusted enough to be treated as a strong copy-trading source.

A `WATCH` wallet:

1. Passed the basic watchable quality gates, such as minimum win rate, Wilson lower-bound confidence, resolved-bet count, and non-negative PnL.
2. Is kept in the tracked wallet roster and refreshed periodically.
3. Can be shown in the UI and diagnostics so its behavior can be observed over time.
4. Is below `VERIFIED` or `ELITE`, so its signals should carry less trust and usually need stronger confirmation from better wallets.

Think of `WATCH` as "on the radar". It is not a ban, and it is not a high-conviction endorsement. It is a candidate wallet that has enough evidence to keep monitoring, but not enough evidence to rely on alone.

Tier meaning:

| Status | Meaning |
|---|---|
| `WATCH` / `watchable=True` | Monitor this wallet; early candidate, lower confidence |
| `VERIFIED` / `verified=True` | Trusted enough to contribute copy-trade signals |
| `ELITE` / `elite=True` | Highest-quality wallet; receives strongest weight and can drive elite strategies |

When answering "what is WATCH status?" in general, do not ask for a specific wallet. Only ask for a wallet name/address if the user wants to know why one specific wallet is WATCH instead of VERIFIED or ELITE.

---

## Group: `wallet_quality`

Controls who enters the watchable and verified tiers.

| Parameter | Current | Type | Description |
|---|---|---|---|
| `MIN_WIN_RATE_WATCH` | `0.53` | float (0–1) | Minimum win rate to become watchable. 53% = slightly above coin flip. |
| `MIN_WIN_RATE_VER` | `0.56` | float (0–1) | Minimum win rate to become verified. Must exceed watchable threshold. |
| `MIN_RESOLVED_BETS` | `10` | int | Minimum number of resolved (closed) bets. Prevents small-sample flukes. |
| `MIN_PNL` | `0.0` | float ($) | Minimum lifetime PnL. 0 = any profitable wallet qualifies. |
| `WILSON_MIN_WATCH` | `0.30` | float (0–1) | Wilson lower-bound confidence floor for watchable. Statistically accounts for small samples. |
| `WILSON_MIN_VER` | `0.38` | float (0–1) | Wilson lower-bound confidence floor for verified. |
| `MIN_AVG_PROFIT_PER_TRADE` | `0.5` | float ($) | Average dollar profit per resolved trade. Filters wallets with many tiny wins. |
| `MIN_AVG_BET_SIZE` | `10.0` | float ($) | Average bet size. Filters out small-stake wallets whose signals are noisy. |

**Wilson Lower Bound** — why it matters:
A wallet with 8/10 wins (80%) looks great, but 10 bets is a tiny sample. Wilson LB for 8/10 at 95% CI ≈ 0.49. At 30 bets with 80% WR the LB reaches ~0.62. The 0.30 floor requires roughly 80% WR over 15+ bets, or 65%+ WR over 50+ bets. This prevents copying a lucky newcomer.

**Threshold change behaviour:**
Changing `wallet_quality` or `wallet_selector` thresholds via MCP automatically triggers `_reeval_wallets_impl()` — all ~4700 cached wallet profiles are re-scored against the new thresholds immediately, without any API calls. The `watchable`, `verified`, `elite`, and `fail_reasons` fields in the DB are updated in the same request.

**Tuning guide:**
- Too few watchable wallets → lower `WILSON_MIN_WATCH` (most impactful lever) or `MIN_WIN_RATE_WATCH`
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

The active selector is `performance`. Its full parameter set is editable via `update_config_wallets(group="wallet_selector", patch={...})`. Changes automatically trigger a full wallet re-classification.

| Parameter | Current | Description |
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
| `min_win_rate_watch` | `0.53` | Mirrors `wallet_quality.MIN_WIN_RATE_WATCH` — used by selector at runtime |
| `wilson_min_watch` | `0.30` | Mirrors `wallet_quality.WILSON_MIN_WATCH` — used by selector at runtime |
| `min_resolved_bets` | `10` | Mirrors `wallet_quality.MIN_RESOLVED_BETS` |
| `min_pnl` | `0.0` | Mirrors `wallet_quality.MIN_PNL` |
| `min_win_rate_ver` | `0.56` | Mirrors `wallet_quality.MIN_WIN_RATE_VER` |
| `wilson_min_ver` | `0.38` | Mirrors `wallet_quality.WILSON_MIN_VER` |
| `min_avg_profit` | `0.5` | Mirrors `wallet_quality.MIN_AVG_PROFIT_PER_TRADE` |
| `min_avg_bet` | `10.0` | Mirrors `wallet_quality.MIN_AVG_BET_SIZE` |
| `min_portfolio_or_pnl` | `100.0` | Min current portfolio OR lifetime PnL to pass VERIFIED gate |
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
All components are normalised to 0-1 before weighting.

---

## Bootstrap and Refresh Flow

When TITAN starts, the tracked-wallet roster is rebuilt in this order:

1. Load persisted `watchable=True` wallet profiles from the state DB, capped by `MAX_WATCHLIST_SIZE`.
2. Add configured seed wallets if there is still room. `SEED_WATCHLIST` is built from `seed_watchlist` plus all `vip_wallets` and `priority_wallets` in `titan_config.json`.
3. During each main analysis cycle, score every wallet seen in the fresh public trade feed.
4. Every `DISCOVERY_INTERVAL_CYCLES` cycles, call `discover_new_wallets()`. The active selector discovers candidates from large recent Polymarket BUY trades and the Polymarket leaderboard, then scores them before adding only qualifying wallets.
5. Every 5 cycles, `scan_top_market_holders()` scans high-volume active markets and scores wallets from recent BUY trades on those markets.
6. Every 20 cycles, `_rescore_watchlist()` refreshes stale tracked wallets plus VIP and priority wallets.

So from a completely empty DB, the first tracked wallets come from configured seeds/VIP/priority wallets and from the first public trade-feed cycles. Discovery then expands the roster from large trades, leaderboard entries, and high-volume market scans. Wallets are not blindly requested every cycle: cached profiles are reused until `WALLET_TTL` expires, while VIP/priority wallets are deliberately re-polled more often.

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

## Wallet Profile Fields Reference

These are the fields available on every tracked wallet profile, as returned by `get_tracked_wallets`.

| Field | Type | Description |
|---|---|---|
| `wallet` | str | Raw on-chain address (0x…) |
| `name` | str | Human-readable name; derived from Polymarket profile, VIP config, or truncated address |
| `score` | float (0–1) | Composite selector score — see scoring formula above |
| `win_rate` | float (0–1) | Fraction of resolved trades that were profitable (wins / n_resolved) |
| `wilson_lb` | float (0–1) | Wilson lower bound at 95% confidence — statistically conservative win-rate estimate |
| `wr_source` | str | How win_rate was derived: `"resolved"` (from closed trades), `"open_positions_proxy"` (estimated from open positions when resolved data is sparse), or `"none"` (insufficient data) |
| `n_resolved` | int | Number of closed (resolved) trades used in the win-rate calculation |
| `total_pnl` | float ($) | Lifetime cumulative profit/loss in USD |
| `pnl_pct` | float (%) | Lifetime PnL as a percentage of initial capital |
| `total_value` | float ($) | Current portfolio value in USD |
| `avg_bet` | float ($) | Average size of a single bet in USD |
| `avg_profit` | float ($) | Average dollar profit per resolved trade |
| `alpha_per_trade` | float ($) | total_pnl / n_resolved — average net edge per trade, sign-aware |
| `trades_per_hour` | float | Trading frequency (TPH) over the observation window |
| `n_pos` | int | Number of currently open positions |
| `recent_pnl_30d` | float ($) \| None | Rolling 30-day PnL; refreshed every 6 h for verified+ wallets |
| `recent_pnl_7d` | float ($) \| None | Rolling 7-day PnL; refreshed every 6 h for verified+ wallets |
| `lb_rank` | int \| None | Polymarket leaderboard rank (None if not on leaderboard) |
| `lb_vol` | float ($) \| None | Polymarket leaderboard volume (None if not on leaderboard) |
| `watchable` | bool | Passed the basic quality gates — on the radar |
| `verified` | bool | Trusted enough to contribute copy-trade signals |
| `elite` | bool | Highest-quality wallet; drives elite-only strategies |
| `hft` | bool | Classified as a high-frequency trader (TPH ≥ `hft_tph_threshold`) — excluded from copy signals |
| `sports_bot` | bool | Classified as a sports market-making bot — excluded from copy signals |
| `vip` | bool | Manually configured VIP wallet — polled every cycle regardless of rotation |
| `fail_reasons` | list[str] | Human-readable list of gates the wallet did not pass (e.g. `"WR 45%<53%"`) |
| `detail` | str | One-line formatted summary used in diagnostics and the UI detail popup |
| `ts` | float | Unix timestamp of the last full profile refresh |

---

## Quick Diagnostics

```
get_config_wallets          → see all current thresholds
get_tracked_wallets         → see live wallet roster with tier flags
get_snapshot                → elite roster section shows active elites with scores
get_rejects                 → rejection reasons often reference wallet quality gates
```
