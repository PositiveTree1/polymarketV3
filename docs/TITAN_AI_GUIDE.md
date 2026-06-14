# TITAN — AI Analysis Guide

> Entry point for any AI model connecting via MCP.
> Works with small models (Gemma, Mistral) and large models (Claude, GPT-4) alike.
> No prior context needed — this document is self-contained.

---

## What is TITAN?

TITAN is an automated **paper-trading bot** that mirrors the trades of high-performing wallets on [Polymarket](https://polymarket.com) — a decentralised prediction market platform.

It does **not** place real money trades. It simulates a virtual bankroll and tracks P&L to evaluate strategy performance.

**Core loop (every 15 seconds):**
1. Fetch recent trades from Polymarket
2. Score wallets — identify elite performers
3. Build signals — which markets are elites betting on, and how aggressively
4. Gate signals — apply quality, price, and market filters
5. Paper-trade qualifying signals
6. Monitor positions for exits

---

## How to Connect (MCP)

TITAN exposes its full runtime state and configuration via an MCP server at `http://127.0.0.1:8765`.

**To start an analysis session:**

```
1. Call:  initialize  (protocolVersion: "2025-11-25")
2. Call:  tools/list  to see all available tools (currently 30)
3. Start with:  get_status  →  get_snapshot  →  get_logs
```

Full tool reference: [MCP_REFERENCE.md](MCP_REFERENCE.md)

---

## Quick Status Check (3 calls)

| Step | Tool | What to look for |
|---|---|---|
| 1 | `get_status` | `running`, `cycle_count`, `recent_error_count` |
| 2 | `get_portfolio_overview` | `total_equity`, `session_pnl`, `open_positions` |
| 3 | `get_snapshot` | Full snapshot — positions, signals, elite roster, last trades |

If `recent_error_count > 0`, follow with `get_recent_errors`.

---

## Document Map

Use these docs for deeper context. Load only what you need.

| Document | When to load |
|---|---|
| This file | Always — entry point |
| [TITAN_CONTEXT.md](TITAN_CONTEXT.md) | Full architecture, module map, engine loop detail |
| [TITAN_STRATEGIES.md](TITAN_STRATEGIES.md) | Strategy logic — entry/exit rules for each builder |
| [MCP_REFERENCE.md](MCP_REFERENCE.md) | All 30 MCP tools with inputs/outputs and examples |
| [ANALYSIS_GUIDE.md](ANALYSIS_GUIDE.md) | Step-by-step AI analysis workflow + change proposals |
| [config/CONFIG_WALLETS.md](config/CONFIG_WALLETS.md) | Wallet quality and elite threshold parameters |
| [config/CONFIG_SIGNALS.md](config/CONFIG_SIGNALS.md) | Signal gates, scoring constants, price/drift zones |
| [config/CONFIG_STRATEGIES.md](config/CONFIG_STRATEGIES.md) | Per-strategy configuration (recent_form, drift_discount, consensus_basket) |
| [config/CONFIG_RISK.md](config/CONFIG_RISK.md) | Position management, stop-loss, Kelly sizing, timing |
| [config/CONFIG_SIZING.md](config/CONFIG_SIZING.md) | Bankroll, bet caps, market quality filters |
| [TITAN_POLYMARKET_DATA_MODEL.md](TITAN_POLYMARKET_DATA_MODEL.md) | Data structures: WhaleObservation, Market, Signal, Position, URL identity rules |
| [MCP_REFERENCE.md#logging-architecture](MCP_REFERENCE.md#logging-architecture) | Log flow, levels, files, startup lines |

---

## System Architecture (one-page summary)

```
Polymarket API
      │
      ▼
Wallet Selector        ← score and classify wallets (elite / verified / watchable)
      │
      ▼
Signal Builders        ← 3 parallel strategies produce scored signals (0–100)
  ├── recent_form      ← copy wallets profitable in last 30 days
  ├── drift_discount   ← enter when price dips 4–12% below whale entry
  └── consensus_basket ← require ≥1 elite, enforce full gate sequence
      │
      ▼
Trade Gates            ← price 20–72¢, liquidity, volume, cooldown, position limits
      │
      ▼
Kelly Sizing           ← fractional Kelly × score × confluence × tier multipliers
      │
      ▼
Paper Positions        ← tracked with live P&L, whale exit monitoring
      │
      ▼
Exit Logic             ← whale exits, profit target (+40%), stop-loss (per strategy)
```

---

## Signal Tiers

| Tier | Meaning | Auto-traded? |
|---|---|---|
| `CONVICTION` | Whale commits ≥$1000 or ≥0.5% portfolio | Yes |
| `ALERT` | High-score signal, all gates pass | Yes |
| `STRONG` | Score above STRONG_SCORE (62) | Displayed only |
| `MEDIUM` | Lower score, informational | Displayed only |
| `ELITE_ONLY` | Only elite wallets, no verified confluence | Displayed only |

---

## Configuration at a Glance

All parameters live in `titan_config.json` (repo root) and hot-reload without restart.

**Most impactful parameters for tuning:**

| Parameter | Default | Effect |
|---|---|---|
| `MIN_SCORE` | 55 | Lower → more signals, more noise |
| `MAX_SIGNAL_AGE_H` | 0.25 (15 min) | Higher → more signals, more stale |
| `MIN_ENTRY_PRICE` | 0.20 | Hard floor — never enter below this |
| `MAX_ENTRY_PRICE` | 0.72 | Hard ceiling — avoids near-certainty trap |
| `MIN_CONFLUENCE` | 2 | Lower → single-whale trades allowed |
| `MAX_OPEN_POSITIONS` | 5 | Higher → more concurrent positions |
| `STOP_LOSS_PCT` | -0.30 | Less negative → tighter stop (-0.20) |
| `PROFIT_TARGET_PCT` | 0.40 | Lower → takes gains earlier |
| `MIN_LIQUIDITY` | 15000 | Lower → enters lower-volume markets |

To change a parameter live, use the `update_config_*` MCP tools.
For detailed parameter descriptions see the `config/` docs above.

---

## How to Propose a Config Change

1. Read current state: `get_snapshot` + `get_trade_stats` + `get_recent_errors`
2. Read relevant config: e.g., `get_config_signals` for signal quality issues
3. Identify the problem (too few signals / too many losses / wrong position sizes)
4. Validate your change: `update_config_* ... dry_run=true`
5. Apply: `update_config_* ... dry_run=false`
6. The change hot-reloads within 15 seconds (next engine cycle)
7. Monitor: watch `get_logs` and `get_signals` for effect

Full workflow: [ANALYSIS_GUIDE.md](ANALYSIS_GUIDE.md)

---

## Known Loss Patterns (from config history)

These are the documented root causes of losses in prior sessions:

| Pattern | Description | Fix |
|---|---|---|
| Near-certainty trap | Entered above 85¢ — tiny upside, catastrophic downside | `MAX_ENTRY_PRICE` hard ceiling at 0.72 |
| No stop loss | Whale never sold, position went to 0 | `STOP_LOSS_ENABLED=true`, `-30%` global |
| Stale signals | 30-min age allowed absorbed prices | `MAX_SIGNAL_AGE_H=0.25` (15 min) |
| HFT spike copy | Copied one leg of a hedged arb | HFT excluded from recent_form (max_tph=20) |
| Inflated EV | `fair_prob = whale_entry + 0.05` bypassed gate | Fixed in scoring |
| Too many positions | $7 bankroll ÷ 5 = $1.40 avg — too small | Adaptive Kelly caps for small bankrolls |

---

## Improvement Ideas

The following improvements would help an AI analyse and tune TITAN more effectively. They are not yet implemented.

### Observability
1. **Per-strategy P&L breakdown** — `get_trade_stats` currently aggregates all strategies. Splitting by `recent_form` / `drift_discount` / `consensus_basket` would show which strategies are winning.
2. **Signal-to-trade conversion rate** — how many ALERT signals were actually traded vs blocked by gates. Helps identify over-filtering.
3. **Parameter change log endpoint** — `get_config_changes(n=20)` to retrieve recent config edits with timestamps. Currently changes are only in `titan_server.log`.
4. **Position age histogram** — distribution of how long winning vs losing positions are held. Informs `MIN_HOLD_MINUTES` and `EXIT_COOLDOWN_SECONDS`.

### Config Intelligence
5. **Parameter range hints in MCP** — `get_config_signals` currently returns current values. Adding `min`, `max`, `recommended_range` to each parameter would allow AI to propose bounded changes safely.
6. **Dry-run simulation** — given a set of config changes, replay the last N trades and show estimated P&L delta. Would let an AI evaluate proposals before applying them.
7. **Auto-detect regime changes** — if `recent_error_count` spikes or win rate drops below 40% for 10+ cycles, emit a structured alert event via SSE so a listening AI can react proactively.

### Analysis
8. **Rejection reason analytics** — `get_rejects` returns raw strings. Grouping by rejection type (price gate / age gate / score gate / cooldown / position limit) would make it easy to see which gate is blocking most signals.
9. **Elite wallet performance delta** — compare elite wallet PnL from week ago vs now. A whale going cold should reduce their signal weight before the scoring naturally catches up.
10. **Market type P&L split** — separate P&L for POLITICS vs EVENT vs SPORTS markets. If one type is consistently losing, `ALLOWED_MARKET_TYPES` is the lever.
