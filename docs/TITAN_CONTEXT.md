# TITAN — Polymarket Mirror Engine: Full Context Document

> Use this file to bootstrap a new AI session without re-explaining the project.
> Entry point: `run_titan.py` | Version: v10 Multi-Strategy | Mode: Single Wallet (paper trading)

---

## Main Goal & Pipeline

TITAN is an **automated paper-trading bot** that monitors Polymarket prediction markets and mirrors the trades of high-performing wallets. It runs continuously and is controlled through a Tkinter desktop UI.

It does **not** place real on-chain orders — it simulates trades against a virtual bankroll and tracks P&L.

The system is designed as a clear sequential pipeline:

```
Polymarket API
      │
      ▼
Wallet Selector Classes          ← fetch, score, and classify wallets
      │
      ▼
List of Selected Wallets         ← elite / verified / watchlist / HFT tiers
      │
      ▼
Signal Builder Classes           ← build signals from selected wallets' trades
      │                             (multi-strategy: recent_form, drift_discount, consensus_basket)
      ▼
List of Signals                  ← scored 0–100, tiered (CONVICTION / ALERT / HFT / STRONG / MEDIUM)
      │
      ▼
Trade Maker Classes              ← entry gates, Kelly sizing, paper execution
      │
      ▼
List of Trades / Open Positions  ← live positions with exit logic
      │
      ▼
Accounting                       ← P&L, equity curve, win rate, expectancy
```


---

## Architecture — Module Map

| File | Role |
|---|---|
| `run_titan.py` | CLI entry point (ui / server / client modes) |
| `ScriptsTitan/titan_api.py` | Orchestrates trader, market data, signals, telegram |
| `ScriptsTitan/titan_engine.py` | Core trading loop. Calls all other modules. |
| `ScriptsTitan/titan_state.py` | Shared mutable state (open positions, bankroll, trade history, logs) |
| `ScriptsTitan/titan_config.py` | Loads `titan_config.json`; hot-reload supported |
| `ScriptsTitan/titan_selector.py` | **Wallet Selector** — fetches, scores, classifies wallets from Polymarket API |
| `ScriptsTitan/titan_wallet.py` | Wallet balance, addresses, trade history integration |
| `ScriptsTitan/titan_market.py` | Fetches market metadata, trade feeds, CLOB prices |
| `ScriptsTitan/titan_markets.py` | Market cache with CID/asset lookups (Gamma protocol) |
| `ScriptsTitan/titan_prices.py` | Outcome price fetching and caching |
| `ScriptsTitan/titan_signal_builder.py` | **Signal Builder ABC** — `SignalBuilderBase`, `BuilderParams`, 3 concrete builders, registry, `build_builders()` factory |
| `ScriptsTitan/titan_signals.py` | Signal scoring, filtering, deduplication. `build_signals()` delegates to registered builders. |
| `ScriptsTitan/titan_trader.py` | **Trade Maker** — executes paper trades. Kelly sizing, entry gates, exit logic. |
| `ScriptsTitan/titan_position.py` | Position lifecycle (entry, exit, P&L calc, strategy tags) |
| `ScriptsTitan/titan_trade.py` | Trade record structure and trade history |
| `ScriptsTitan/titan_db.py` | SQLite persistence (price series, equity curve, watchlist) |
| `ScriptsTitan/titan_persistence.py` | Save/load state to JSON |
| `ScriptsTitan/titan_ui.py` | Tkinter desktop GUI — all tabs |
| `ScriptsTitan/titan_ui_charts.py` | Equity curve and performance charts |
| `ScriptsTitan/titan_telegram.py` | Optional Telegram notifications and commands |
| `ScriptsTitan/titan_ai.py` | LLM integration (Claude API calls for analysis) |
| `ScriptsTitan/titan_server.py` | MCP streamable HTTP server (JSON-RPC 2.0 + SSE) |
| `ScriptsTitan/titan_client.py` | HTTP client for connecting to TitanServer |
| `titan_config.json` | All tunable parameters (repo root) |

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
- `discover_new_wallets()` — scans top market holders for undiscovered elites
- `_rescore_watchlist()` — refreshes stale wallet scores
- `_refresh_recent_form_scores()` — updates 30d/7d PnL for Recent Form strategy (6h TTL)

---

## Wallet Selector — Classification Tiers

| Tier | Criteria |
|---|---|
| **ELITE** | High score, min PnL, min resolved bets, min win rate, min portfolio value |
| **Verified** | Lower thresholds than elite, still trusted |
| **Watchlist** | Being monitored, not yet confirmed |
| **HFT** | High trades-per-hour (≥100 TPH), different signal path |

Wallet score is based on: win rate, Wilson lower bound, total PnL, avg bet size, portfolio size, resolved bets count.

---

## Signal Builder — Signal Types & Tiers

Signals are grouped by `(condition_id, outcome)` — one signal per market side.

| Tier | Description | Auto-trade? |
|---|---|---|
| `CONVICTION` | Wallet commits ≥ 0.5% portfolio OR ≥ $1000 in one trade | Yes |
| `ALERT` | High-score signal meeting all gates | Yes |
| `HFT` | HFT wallet spike (20–40x avg bet) | Yes |
| `STRONG` | Score above STRONG_SCORE threshold | No (displayed only) |
| `MEDIUM` | Lower score, informational | No |
| `ELITE_ONLY` | Only elite wallets, not enough confluence | No |

Auto-trade fires for: `CONVICTION`, `ALERT`, `HFT` (configurable via `TRADEABLE_TIERS_LIST`).

### Signal Score Breakdown (0–100)

| Component | Max |
|---|---|
| Wallet quality | 30 |
| Confluence (# of selected wallets agreeing) | 18 |
| Recency | 20 |
| Price window (is price in 20–72¢ zone?) | 15 |
| Market quality (liquidity, volume) | 10 |
| Conviction bonus | 5 |
| Exit penalty (wallet already selling) | −8× |

### Multi-Strategy Engine (v10) — Signal Builder Architecture

> Full strategy reference: [TITAN_STRATEGIES.md](TITAN_STRATEGIES.md)

Three builders run in parallel via `build_signals()`, each implemented as a `SignalBuilderBase` subclass in `titan_signal_builder.py`:

| Builder ID | Class | Logic |
|---|---|---|
| `recent_form` | `RecentFormBuilder` | Weights wallets by their 30d/7d PnL, recency-boosted |
| `drift_discount` | `DriftDiscountBuilder` | Favors signals where price has drifted from wallet entry (opportunity window) |
| `consensus_basket` | `ConsensusBasketBuilder` | Requires multiple elite wallets agreeing (confluence gate) |

Each builder owns a typed `@dataclass` of parameters (`RecentFormParams`, `DriftDiscountParams`, `ConsensusBasketParams`). Active builders are configured via `signal_builders.active_builders` in `titan_config.json`. The `build_signals()` dispatcher iterates `C.get_active_builders()` — adding a new strategy requires only a new `SignalBuilderBase` subclass; no changes to the dispatcher.

The underlying `_build_*` functions remain in `titan_signals.py` as private helpers; builders call them with their typed params injected. Builder params are editable at runtime from the **Signal Builders** tab (Tab 12) in the UI — changes hot-reload on the next cycle.

---

## Trade Maker — Entry Gates & Exit Logic

### Entry Gates (all must pass)
- Price in 20–72¢ zone (`MIN_ENTRY_PRICE` / `MAX_ENTRY_PRICE`)
- Market liquidity ≥ `MIN_LIQUIDITY`
- Market volume ≥ `MIN_VOLUME`
- Market not closing too soon (`MIN_HOURS_LEFT`)
- No position already open on this market
- Not in cooldown for this market
- `MAX_OPEN_POSITIONS` not exceeded
- Drift within `MIN_DRIFT` / `MAX_DRIFT` range
- Not a sports market if `BLOCK_SPORTS=true`

### Position Sizing
- Kelly fraction sizing (fractional Kelly, configurable)
- `MIN_BET` / `MAX_BET_ABS` hard caps
- `MAX_BET_PCT` as % of current bankroll
- CONVICTION trades get full Kelly; others get fractional
- Optional proportional sizing based on wallet bet size relative to portfolio

### Exit Logic

TITAN follows an explicit philosophy: **follow the selected wallet out**.

| Trigger | Behavior |
|---|---|
| Tracked wallet sells → `WALLET_EXIT_SELL=true` | Immediate paper-sell |
| Profit target hit (`PROFIT_TARGET_PCT`, default 20%) | Sells even if wallet still holds |
| Trailing stop | Activates at +15%, trails 10% from peak |
| Stop loss (`STOP_LOSS_ENABLED`, default -30%) | Optional hard floor |
| Min hold guard (`MIN_HOLD_MINUTES`) | Won't sell before this time regardless |
| Exit cooldown (`EXIT_COOLDOWN_SECONDS`) | Blocks re-entry on same market for N minutes |

---

## Accounting

Tracked in `titan_state.py`, persisted via `titan_db.py` and `titan_persistence.py`:

- Bankroll (starting + running)
- Session P&L and cumulative P&L
- Equity curve (time-series, SQLite)
- Per-trade: entry price, exit price, size, hold time, P&L, strategy tag
- Aggregate stats: win rate, expectancy, total trades, open positions count

Displayed in the **P&L tab** of the UI (equity curve chart + stats grid + trade history table).

---

## UI Tabs

| Tab | Content |
|---|---|
| SIGNALS | Live treeview of all signals this cycle. Score, drift, wallet count, mode. |
| ALERTS | Formatted detail for tradeable signals (ALERT/STRONG/HFT/CONVICTION). Auto-buy status shown. |
| POSITIONS | Open paper positions. Live P&L, hold time, wallet source. Double-click for detail popup. |
| P&L | Equity curve graph. Stats grid (total PnL, win rate, expectancy). Trade history table. |
| WALLETS | Selected wallet roster with score, WR, Wilson LB, PnL, TPH. Filterable by tier. |
| ANALYSIS | Cycle summary: signal counts by tier, elite roster, account stats. |
| DIAG | Rejections (why signals were blocked), cooldowns, failed wallet scores. |
| LOG | Full system log. "Copy Snapshot for AI" button copies entire state as text. |
| CONFIG | JSON editor for `titan_config.json`. Save & hot-reload. Guide panel on right. |
| SIGNAL BUILDERS | Per-builder parameter editor. Dropdown selects builder; Apply hot-reloads config and rebuilds builder instances. |

---

## Telegram Bot Integration

Optional. Commands sent to the bot:

| Command | Response |
|---|---|
| `pl` / `pnl` | Screenshot of P&L tab sent as photo |
| `dash` / `dashboard` | Starts Cloudflare tunnel + sends URL button |
| Any other text | Forwarded to LLM with system snapshot as context |

The bot also sends notifications on: boot, buy, sell, errors.

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
| `WALLET_EXIT_SELL` | true | Mirror wallet exits |
| `MIN_ENTRY_PRICE` | 0.20 | Min price to enter (20¢) |
| `MAX_ENTRY_PRICE` | 0.72 | Max price to enter (72¢) |
| `EXIT_COOLDOWN_SECONDS` | varies | Cooldown before re-entry on same market |
| `MAX_OPEN_POSITIONS` | varies | Hard cap on simultaneous open positions |

Config is hot-reloaded — changes take effect on the next engine cycle without restarting.

---

## "Copy Snapshot for AI" Feature

The LOG tab has a button that copies a full machine-readable snapshot to clipboard:
- Account stats (bankroll, session PnL, cycle count)
- All open positions (entry, current price, P&L, held time, wallet names)
- Active signals from last cycle with score breakdown
- Signal rejections with rejection reasons
- Full elite roster with metrics
- Last 100 trades
- Last 600 system log lines

---

## File Locations

Core Titan code lives in `ScriptsTitan/`.

Persistent data:
- `titan_config.json` — all configuration (repo root)
- State file and wallet roster paths configured inside `titan_config.json`
- Logs saved to `Logs/` directory (configurable via `titan_state.LOG_DIR`)

---

## Running the App

```
cd C:\Users\jlala\source\repos\polymarketV3
.\.venv\Scripts\python.exe run_titan.py
```

Or activate the venv and run `python run_titan.py`.
