# TITAN — Polymarket Mirror Engine: Full Context Document

> Use this file to bootstrap a new AI session without re-explaining the project.
> Entry point: `run_titan.py` | Version: v10 Multi-Strategy | Mode: Single Wallet (paper trading)
> Single-wallet instance: `_wallet = WalletEnv()` in `titan_state.py`. Shared cache: `_shared_wallet_cache` (assigned to `_wallet.wallet_cache`). No separate watchlist object — "watchable" is a boolean flag on `WalletProfile`; `get_watchlist()` filters the cache.
>
> **AI connecting via MCP? Start here instead: [TITAN_AI_GUIDE.md](TITAN_AI_GUIDE.md)**

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
List of Selected Wallets         ← elite / verified / HFT tiers
                                   (watchable = flag on WalletProfile, not a separate object)
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

There is also a separate control and integration layer around that trading pipeline:

```
Tkinter UI Client Process / External MCP Client
      │
      ▼
TitanAPI                         ← typed application facade over engine/state
      │
      ├── direct in-process calls (single-process UI mode)
      └── MCP server transport (split client/server mode)
              │
              ▼
        JSON-RPC tools + SSE notifications
```


---

## Architecture — Module Map

| File | Role |
|---|---|
| `run_titan.py` | CLI entry point (ui / server / client modes) |
| `ScriptsTitan/titan_api.py` | Orchestrates trader, market data, signals, telegram |
| `ScriptsTitan/titan_engine.py` | Core trading loop. Calls all other modules. |
| `ScriptsTitan/titan_state.py` | Single `WalletEnv` instance (`_wallet`), shared `_shared_wallet_cache`, open positions, bankroll, trade history, logs |
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

## UI Notes

- `Position Detail` mirrors `Signal Detail` header styling for the highlighted `OUTCOME` pill.
- `Held` in `Position Detail` is display-only formatting: under 60 minutes it shows `N min`; at 60+ minutes it shows `D:HH:MM`.
- The `SELECTED WALLETS` list in `Position Detail` uses `buy_trade.wallet_names` / `pos.wallet_names` for display names.
- Double-clicking a wallet row in `Position Detail` or `Signal Detail` opens `Wallet Detail`. In remote client mode the UI may first call `get_tracked_wallets(search="<wallet address>")` to hydrate a missing wallet into the local cache.

---

## API Layer

`TitanAPI` is the main application boundary above the engine. It is not the raw Polymarket API; it is TITAN's own typed facade for reading runtime state and issuing control actions.

Responsibilities of `ScriptsTitan/titan_api.py`:

- Starts the engine and wires callbacks for logs, cycle completion, heartbeats, and position open/close events
- Exposes read APIs for positions, signals, wallets, P&L, logs, snapshot text, config, DB-backed history, and portfolio summaries
- Exposes control APIs such as `force_cycle()`, `pause()`, `resume()`, and `update_config()`
- Maintains an internal event bus via `subscribe()` / `unsubscribe()` so both the UI and the MCP server can react to engine events
- Marks MCP-callable methods with the `@mcp_tool(...)` decorator so the server can publish them as tools automatically

Think of the layering as:

`titan_engine` = trading runtime  
`titan_state` / DB = runtime state + persistence  
`TitanAPI` = stable programmatic interface over both

This distinction matters because the UI should stay thin. `titan_ui.py` renders and invokes the API; business logic belongs behind `TitanAPI` or lower-level engine modules.

---

## MCP Layer

TITAN can run as an MCP server so external tools or another TITAN process can inspect and control it remotely.

### What MCP means here

- `ScriptsTitan/titan_server.py` exposes `TitanAPI` over HTTP at `/mcp`
- `POST /mcp` handles JSON-RPC 2.0 request/response calls such as `initialize`, `tools/list`, `tools/call`, `resources/list`, and `resources/read`
- `GET /mcp` opens an SSE stream for server notifications such as log messages, heartbeats, cycle completion, and position events
- Sessions are tracked with `MCP-Session-Id`
- Optional bearer-token auth can protect the server

### MCP tools, resources, and prompts

The server derives its tool list from `TitanAPI` methods decorated with `@mcp_tool`. That means adding a new API method and decorating it is usually enough to publish a new MCP tool. Currently **30 tools** are exposed.

Built-in MCP resources currently include:

- `titan://config` — live config JSON
- `titan://snapshot` — compressed runtime snapshot
- `titan://logs` — recent log tail
- `titan://wallets` — tracked wallet roster

The server also exposes prompt templates: `titan_analysis`, `titan_signal_review`, `titan_wallet_brief`.

**Full tool reference:** [MCP_REFERENCE.md](MCP_REFERENCE.md)
**Analysis workflow:** [ANALYSIS_GUIDE.md](ANALYSIS_GUIDE.md)

### Client/server runtime modes

There are two important ways TITAN can be used:

- Single-process UI mode: `run_titan.py --mode ui` starts the engine and the Tkinter UI in the same process, with the UI calling `TitanAPI` directly
- Split client/server mode: `run_titan.py --mode server` starts the engine plus MCP server in one process, and `run_titan.py --mode client` starts a separate UI process that connects through `TitanClient`

Because `TitanClient` mirrors the `TitanAPI` surface, most UI code can work against either local or remote backends with minimal branching.

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

Tiers are boolean flags on `WalletProfile` stored in `_shared_wallet_cache`. There is no separate watchlist object — `get_watchlist()` filters the cache for `watchable=True`.

| Flag | Criteria |
|---|---|
| `elite` | High score, min PnL, min resolved bets, min win rate, min portfolio value |
| `verified` | Lower thresholds than elite, still trusted |
| `watchable` | Being monitored, not yet confirmed (replaces old Watchlist concept) |
| `hft` | High trades-per-hour (≥100 TPH), different signal path |

`WATCH` in the UI means `watchable=True`. It is a monitoring status: the wallet passed basic quality gates and stays in the tracked roster, but it is not yet trusted like a `VERIFIED` or `ELITE` wallet. Answer general questions about WATCH as "on the radar, lower confidence, monitored for future evidence"; only ask for a wallet name/address when the user asks why a specific wallet has WATCH status.

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

The underlying `_build_*` functions remain in `titan_signals.py` as private helpers; builders call them with their typed params injected. Builder params are editable at runtime from the **🔨 SIGN. CRAFT** tab in the UI — changes hot-reload on the next cycle.

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
| Min hold guard (`MIN_HOLD_MINUTES`) | Blocks all exits before this time |
| Tracked wallet sells → `WALLET_EXIT_SELL=true` | Paper-sell after min-hold, but HFT/hedge exits and some early-noise exits are ignored |
| Profit target hit (`PROFIT_TARGET_PCT`, default 40%) | Sells even if wallet still holds |
| Stop loss (`STOP_LOSS_ENABLED`, default -30%) | Strategy/global hard floor |
| Market resolving/resolved or expiring soon | Forced exit |
| Catastrophic loss / stale trend reversal | Safety exits |
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

Tabs in order as shown in the notebook:

| Tab | Content |
|---|---|
| SELECTOR | Wallet selector parameter editor. Live-reload selector config; shows selector tier thresholds. |
| WALLETS | Selected wallet roster with score, WR, Wilson LB, PnL, TPH. Filterable by tier. |
| SIGN. CRAFT | Signal builder parameter editor (also: signal builder config, strategy params, builder config). Active builder checkboxes; per-builder param grids; Apply hot-reloads config. |
| SIGNALS | Live treeview of all signals this cycle. Score, drift, wallet count, mode. |
| ALERTS | Formatted detail for tradeable signals (ALERT/STRONG/HFT/CONVICTION). Auto-buy status shown. MCP equivalent: `get_tradeable_signals` — returns same data structured for AI. |
| POSITIONS | Open paper positions. Live P&L, hold time, wallet source. Double-click for detail popup. |
| P&L | Equity curve graph. Stats grid (total PnL, win rate, expectancy). Trade history table. |
| ANALYSIS | Cycle summary: signal counts by tier, elite roster, account stats. |
| DIAG | Rejections (why signals were blocked), cooldowns, failed wallet scores. |
| LOG | Full system log. "Copy Snapshot for AI" button copies entire state as text. |
| CONFIG | JSON editor for `titan_config.json`. Save & hot-reload. Guide panel on right. |

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
| `MIN_SCORE` | 55 | Signal score threshold to display |
| `ALERT_SCORE` | 70 | Score threshold to auto-trade |
| `MIN_ELITE_CONFLUENCE` | 2 | Minimum elite wallets needed for CONVICTION tier |
| `PROFIT_TARGET_PCT` | 0.40 | +40% auto-exit |
| `STOP_LOSS_PCT` | −0.30 | −30% hard stop (per-strategy overrides apply) |
| `STOP_LOSS_ENABLED` | true | Toggle stop loss — must stay true |
| `WALLET_EXIT_SELL` | true | Mirror wallet exits |
| `MIN_ENTRY_PRICE` | 0.20 | Min price to enter (20¢) |
| `MAX_ENTRY_PRICE` | 0.72 | Max price to enter (72¢) |
| `EXIT_COOLDOWN_SECONDS` | 600 | 10-minute cooldown before re-entry on same market |
| `MAX_OPEN_POSITIONS` | 5 | Hard cap on simultaneous open positions |
| `MAX_SIGNAL_AGE_H` | 0.25 | Signal age limit — 15 minutes |

Config is hot-reloaded — changes take effect on the next engine cycle without restarting.

**Detailed parameter reference:** [config/](config/) — one file per domain:
- [CONFIG_WALLETS.md](config/CONFIG_WALLETS.md) — wallet quality, elite thresholds, selector
- [CONFIG_SIGNALS.md](config/CONFIG_SIGNALS.md) — signal gates, scoring constants
- [CONFIG_STRATEGIES.md](config/CONFIG_STRATEGIES.md) — per-strategy params
- [CONFIG_RISK.md](config/CONFIG_RISK.md) — position management, stop-loss, Kelly
- [CONFIG_SIZING.md](config/CONFIG_SIZING.md) — bankroll, bet caps, market quality

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
