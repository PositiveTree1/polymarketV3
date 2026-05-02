# TITAN — Whale Mirror Engine: Full Context Document

> Use this file to bootstrap a new AI session without re-explaining the project.
> Entry point: `titan_ui.py` | Version: v10 Multi-Strategy | Mode: Single Wallet (paper trading)

---

## What TITAN Does

TITAN is an **automated paper-trading bot** that monitors Polymarket prediction markets and mirrors the trades of elite "whale" wallets. It buys when high-performing whales buy, and sells when they sell (or when profit/stop targets are hit). It runs continuously in the background and is controlled through a Tkinter desktop UI.

It does **not** place real on-chain orders — it simulates trades against a virtual bankroll and tracks P&L.

---

## Architecture — Module Map

| File | Role |
|---|---|
| `titan_ui.py` | Tkinter desktop GUI. All 9 tabs. Boot screen. Renders data from engine via callbacks. |
| `titan_engine.py` | Orchestration. Two loops: main 15s cycle + HFT 3s cycle. Calls all other modules. |
| `titan_state.py` | Shared mutable state (`_wallet` singleton: open positions, bankroll, trade history, logs). |
| `titan_config.py` | Config loader. Reads `titan_config.json` at runtime; hot-reload supported. |
| `titan_wallet.py` | Fetches & scores wallet profiles from Polymarket API. Discovers new whales. |
| `titan_market.py` | Fetches market metadata, trade feeds, CLOB prices. `fetch_position_price_fast()` for live prices. |
| `titan_signals.py` | Builds, scores and filters trading signals from the trade feed. Multi-strategy. |
| `titan_trader.py` | Executes paper trades (open/close positions). Kelly sizing, bet caps. |
| `titan_persistence.py` | Saves/loads state (positions, bankroll, whale roster) to/from disk as JSON. |
| `titan_telegram.py` | Optional Telegram bot: sends buy/sell/error notifications, responds to commands. |
| `titan_config.json` | All tunable parameters. Edit via CONFIG tab in UI or directly. |

---

## Engine Loops

### Main Loop (every ~15 seconds)
1. Fetch recent public trades from Polymarket CLOB feed
2. Poll VIP/elite wallet activity
3. Score wallets seen in the feed (quality, win rate, PnL, Wilson lower bound)
4. Build signals grouped by (market, outcome) — see Signal Types below
5. Score signals 0–100 via multi-strategy scoring
6. Auto-trade any signal at ALERT tier or above
7. Check open positions for exits

### HFT Fast Loop (every 3 seconds)
- Polls only known HFT wallets
- Detects outsized "spike" trades (20–40x that wallet's average)
- Fires an immediate paper-buy if the spike passes the entry gates

### Background Tasks (every N cycles)
- `discover_new_whales()` — scans top market holders for undiscovered elites
- `scan_top_market_holders()` — same, more targeted
- `_rescore_watchlist()` — refreshes stale wallet scores
- `_refresh_recent_form_scores()` — updates 30d/7d PnL for Recent Form strategy (6h TTL)

---

## Wallet Classification

| Tier | Criteria |
|---|---|
| **ELITE** | High score, min PnL, min resolved bets, min win rate, min portfolio value |
| **Verified** | Lower thresholds than elite, still trusted |
| **Watchlist** | Being monitored, not yet confirmed |
| **HFT** | High trades-per-hour (≥100 TPH), different signal path |

Wallet score is based on: win rate, Wilson lower bound, total PnL, avg bet size, portfolio size, resolved bets count.

---

## Signal Types & Tiers

Signals are grouped by `(condition_id, outcome)` — one signal per market side.

| Tier | Description | Auto-trade? |
|---|---|---|
| `CONVICTION` | Whale commits ≥ 0.5% portfolio OR ≥ $1000 in one trade | Yes |
| `ALERT` | High-score signal meeting all gates | Yes |
| `HFT` | HFT wallet spike (20–40x avg bet) | Yes |
| `STRONG` | Score above STRONG_SCORE threshold | No (displayed only) |
| `MEDIUM` | Lower score, informational | No |
| `ELITE_ONLY` | Only elite wallets, not enough confluence | No |

Auto-trade fires for: `CONVICTION`, `ALERT`, `HFT` (configurable via `TRADEABLE_TIERS_LIST`).

---

## Signal Score Breakdown (0–100)

| Component | Max |
|---|---|
| Wallet quality | 30 |
| Confluence (# of whales agreeing) | 18 |
| Recency | 20 |
| Price window (is price in 20–72¢ zone?) | 15 |
| Market quality (liquidity, volume) | 10 |
| Conviction bonus | 5 |
| Exit penalty (whale already selling) | −8× |

---

## Multi-Strategy Engine (v10)

Three strategies run in parallel via `build_signals()`:

| Strategy | Logic |
|---|---|
| `recent_form` | Weights wallets by their 30d/7d PnL, recency-boosted |
| `drift_discount` | Favors signals where price has drifted from whale entry (opportunity window) |
| `consensus_basket` | Requires multiple elite wallets agreeing (confluence gate) |

Active strategies are set in `ACTIVE_STRATEGIES` in config.

---

## Exit Logic

TITAN follows an explicit philosophy: **follow the whale out**.

| Trigger | Behavior |
|---|---|
| Whale sells → `WHALE_EXIT_SELL=true` | Immediate paper-sell, no questions asked |
| Profit target hit (`PROFIT_TARGET_PCT`, default 20%) | Sells even if whale still holds |
| Trailing stop | Activates at +15%, trails 10% from peak |
| Stop loss (`STOP_LOSS_ENABLED`, default -30%) | Optional hard floor |
| Min hold guard (`MIN_HOLD_MINUTES`) | Won't sell before this time regardless |
| Exit cooldown (`EXIT_COOLDOWN_SECONDS`) | After a position closes, blocks re-entry on same market for N minutes |

---

## Position Sizing

- Kelly fraction sizing (fractional Kelly, configurable)
- `MIN_BET` / `MAX_BET_ABS` hard caps
- `MAX_BET_PCT` as % of current bankroll
- CONVICTION trades get full Kelly; others get fractional
- Optional proportional sizing based on whale bet size relative to portfolio

---

## Entry Gates (all must pass to open a position)

- Price in 20–72¢ zone (`MIN_ENTRY_PRICE` / `MAX_ENTRY_PRICE`)
- Market liquidity ≥ `MIN_LIQUIDITY`
- Market volume ≥ `MIN_VOLUME`
- Market not closing too soon (`MIN_HOURS_LEFT`)
- No position already open on this market
- Not in cooldown for this market
- `MAX_OPEN_POSITIONS` not exceeded
- Drift within `MIN_DRIFT` / `MAX_DRIFT` range
- Not a sports market if `BLOCK_SPORTS=true`

---

## UI Tabs

| Tab | Content |
|---|---|
| SIGNALS | Live treeview of all signals this cycle. Score, drift, whale count, mode. |
| ALERTS | Formatted detail for tradeable signals (ALERT/STRONG/HFT/CONVICTION). Auto-buy status shown. |
| POSITIONS | Open paper positions. Live P&L, hold time, whale source. Double-click for detail popup. Price chart. |
| P&L | Equity curve graph. Stats grid (total PnL, win rate, expectancy). Trade history table. |
| WHALES | Whale roster with score, WR, Wilson LB, PnL, TPH. Filterable by tier. |
| ANALYSIS | Cycle summary: signal counts by tier, elite roster, account stats. |
| DIAG | Rejections (why signals were blocked), cooldowns, failed wallet scores. |
| LOG | Full system log. "Copy Snapshot for AI" button copies the entire state as text. |
| CONFIG | JSON editor for `titan_config.json`. Save & hot-reload. Guide panel on right. |

---

## Telegram Bot Integration (`titan_telegram.py`)

Optional. Commands sent to the bot:

| Command | Response |
|---|---|
| `pl` / `pnl` | Screenshot of P&L tab sent as photo |
| `dash` / `dashboard` | Starts Cloudflare tunnel (pycloudflared) + sends URL button |
| Any other text | Forwarded to Groq LLaMA-3.3-70b with system snapshot as context |

The bot also sends notifications on: boot, buy, sell, errors.

---

## Web Dashboard

A minimal HTTP server runs on `127.0.0.1:8080` serving:
- `GET /api/data` → JSON with stats, P&L history, top whales, signals, open positions, recent trades
- `GET /` → serves `dashboard.html` from the same directory

Accessible remotely via Cloudflare tunnel triggered by the Telegram `dash` command.

---

## Key Config Parameters

| Parameter | Default | Meaning |
|---|---|---|
| `BANKROLL_START` | $20.00 | Starting paper bankroll |
| `MIN_SCORE` | 50 | Signal score threshold to display |
| `ALERT_SCORE` | 68 | Score threshold to auto-trade |
| `MIN_ELITE_CONFLUENCE` | 2 | Minimum elite wallets needed for a signal |
| `PROFIT_TARGET_PCT` | 0.20 | +20% auto-exit |
| `STOP_LOSS_PCT` | −0.30 | −30% hard stop |
| `STOP_LOSS_ENABLED` | true | Toggle stop loss |
| `WHALE_EXIT_SELL` | true | Mirror whale exits |
| `MIN_ENTRY_PRICE` | 0.20 | Min price to enter (20¢) |
| `MAX_ENTRY_PRICE` | 0.72 | Max price to enter (72¢) |
| `EXIT_COOLDOWN_SECONDS` | varies | Cooldown before re-entry on same market |
| `MAX_OPEN_POSITIONS` | varies | Hard cap on simultaneous open positions |

Config is hot-reloaded — changes take effect on the next engine cycle without restarting.

---

## "Copy Snapshot for AI" Feature

The LOG tab has a button that copies a full machine-readable snapshot to clipboard:
- Account stats (bankroll, session PnL, cycle count)
- All open positions (entry, current price, P&L, held time, whale names)
- Active signals from last cycle with score breakdown
- Signal rejections with rejection reasons
- Full elite roster with metrics
- Last 100 trades
- Last 600 system log lines

This is the fastest way to give an AI full live context about TITAN's current state.

---

## File Locations

All files live in `C:\Users\jlala\source\repos\polymarketV3\ScriptsTitan\`.

Persistent data:
- `titan_config.json` — all configuration
- State file and whale roster paths are configured inside `titan_config.json`
- Logs saved to `Logs/` directory (configurable via `titan_state.LOG_DIR`)

---

## Running the App

```
cd C:\Users\jlala\source\repos\polymarketV3\ScriptsTitan
.\.venv\Scripts\python.exe titan_ui.py
```

Or activate the venv and run `python titan_ui.py`.

Dependencies are in `requirements.txt` (7 packages: certifi, charset-normalizer, idna, pillow, pyperclip, requests, urllib3). Telegram and Cloudflare features require additional optional packages.
