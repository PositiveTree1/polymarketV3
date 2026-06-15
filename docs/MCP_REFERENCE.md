# TITAN MCP Tool Reference

> Server: `http://127.0.0.1:8765`
> Protocol: MCP 2025-11-25 (JSON-RPC 2.0 over HTTP + SSE)
> Total tools: 37
> All tools are methods on `TitanAPI` decorated with `@mcp_tool`.

---

## Connection

```json
POST /mcp
{
  "jsonrpc": "2.0", "id": 1,
  "method": "initialize",
  "params": {
    "protocolVersion": "2025-11-25",
    "clientInfo": {"name": "my-client", "version": "1.0"}
  }
}
```

Response includes `MCP-Session-Id` header — include it in all subsequent requests.

SSE stream: `GET /mcp` with `Accept: text/event-stream`

---

## Resources (read-only URIs)

| URI | Content | MIME |
|---|---|---|
| `titan://config` | Full `titan_config.json` | `application/json` |
| `titan://snapshot` | Compressed runtime snapshot | `text/plain` |
| `titan://logs` | Last 200 log lines | `text/plain` |
| `titan://wallets` | Elite wallet roster | `application/json` |

Access via `resources/read` with `{"uri": "titan://snapshot"}`.

---

## Prompt Templates

| Name | Description |
|---|---|
| `titan_analysis` | Inject current snapshot, ask for position/performance analysis |
| `titan_signal_review` | Inject current signals, ask for action recommendations |
| `titan_wallet_brief` | Inject wallet roster, ask for wallet activity summary |

---

## Tools — Status & Health

### `get_status`
Engine health and key runtime counters. **Call this first.**

Returns: `EngineStatus`
```
running           bool    Is engine active?
paused            bool    Is engine paused?
cycle_count       int     Total cycles since start
uptime_s          float   Seconds since start
last_cycle_at     float   Unix timestamp of last cycle
open_positions    int     Current open position count
watchlist_size    int     Wallets in monitoring pool
recent_error_count int    ERR/CRITICAL lines in last 50 logs
auth_enabled      bool    Is bearer token auth active?
```

### `get_portfolio_overview`
Single-page health check. Best starting point.

Returns:
```
running           bool
bankroll          float   Current paper bankroll ($)
open_value        float   Mark-to-market value of open positions ($)
total_equity      float   bankroll + open_value
session_pnl       float   P&L since last restart ($)
total_pnl         float   bankroll − BANKROLL_START ($)
open_positions    int
watchlist_size    int
cycle_count       int
recent_error_count int
```

---

## Tools — Positions

### `get_positions`
All open positions with live P&L and price history.

Returns: list of `Position` objects
```
title             str     Market title
outcome           str     YES or NO
entry_price       float   Price at entry (0–1)
cur_price         float   Current price (0–1)
shares            float   Number of shares held
bet               float   Dollar amount invested
entry_ts          float   Entry timestamp (unix)
strategy          str     e.g. "recent_form", "drift_discount+consensus_basket"
tier              str     CONVICTION / ALERT / STRONG / MEDIUM
score             float   Signal score at entry
elite_wallets     list    Wallet addresses that triggered this
elite_names       list    Known names of those wallets
is_hft            bool    Was this an HFT-triggered position?
avg_entry         float   Tracked wallet's average entry price
price_history     list    [[ts, price], ...] for chart rendering
```

### `get_closed_positions`
Closed position history with P&L.

Inputs:
```
limit   int   Max positions to return (default 200)
```

Returns: same structure as `get_positions` plus exit fields (`exit_ts`, `pnl_usdc`, `pnl_pct`).

---

## Tools — Signals

### `get_signals`
Current live signals from the last engine cycle.

Inputs:
```
min_score   float   Filter by minimum score (default 0.0)
```

Returns: list of `Signal` objects
```
title         str     Market title
outcome       str     YES / NO
score         float   0–100
tier          str     CONVICTION / ALERT / STRONG / MEDIUM / ELITE_ONLY
strategy      str     which builder(s) produced this
cur           float   Current market price
avg_entry     float   Tracked wallet's average entry price
drift         float   (cur − avg_entry) / avg_entry
names         list    Names of contributing wallets
cid           str     Condition ID (market identifier)
asset         str     Token ID
newest_ts     float   Timestamp of most recent contributing trade
```

### `get_signal_history`
Historical signal records from SQLite.

Inputs:
```
limit       int     Max rows (default 200)
min_score   float   Filter by score
cid         str     Filter by market condition ID
```

### `get_rejects`
Recent signal rejection reasons (last 50).

Returns: list of strings. Common patterns:
- `"score too low: 43 < 55"`
- `"price out of zone: 0.81 > 0.72"`
- `"age too old: 1.2h > 0.25h"`
- `"drift too high: 0.08 > 0.05"`
- `"max positions reached"`
- `"in cooldown"`

### `get_reject_summary`
Frequency map of rejection reasons — faster than parsing `get_rejects()` manually.

Inputs:
```
limit   int   Number of recent rejects to analyse (default 200)
```

Returns: `{"total": int, "by_reason": {"reason": count, ...}}` sorted by count desc.

---

## Tools — Wallets

### `get_tracked_wallets`
Current wallet roster with tier flags and performance metrics.

Inputs:
```
search   str   Filter by wallet name or address prefix (case-insensitive). Use this when asking about a specific wallet.
tier     str   Filter by tier: elite | verified | watchable | vip
```

Without filters returns the full roster. **Always pass `search=` when asking about a specific wallet by name** — much faster than loading the full list.

Client/UI note:
- Remote UI flows can call `get_tracked_wallets(search="<wallet address>")` to hydrate a missing wallet into the local cache before opening Wallet Detail from a position or signal popup.

Returns: list of `TrackedWalletDict`
```
wallet        str     Address
name          str     Known name (if any)
elite         bool    Elite tier
verified      bool    Verified tier
watchable     bool    Watchable tier
hft           bool    HFT bot flag
vip           bool    Configured VIP wallet flag
score         float   Composite wallet score (0–1)
win_rate      float   Historical win rate
total_pnl     float   Lifetime PnL ($)
trades_per_hour float  TPH
recent_pnl_30d float  30-day PnL
recent_pnl_7d  float  7-day PnL
```

---

## Tools — P&L & Statistics

### `get_pnl_summary`
Bankroll, session P&L, equity history, cooldowns.

Returns:
```
bankroll          float   Current bankroll
bankroll_start    float   Starting bankroll (BANKROLL_START)
session_pnl       float   This session's P&L
total_pnl         float   All-time P&L
equity_history    list    [[ts, equity], ...] last 2000 points
cooldown_cids     dict    {cid: expiry_ts} markets in cooldown
active_market_cids list   CIDs with open positions
watchlist_size    int
```

### `get_trade_stats`
Aggregate trade statistics.

Returns:
```
sell_count    int     Total closed trades
win_count     int     Profitable trades
loss_count    int     Losing trades
sum_pnl       float   Total P&L across all trades
best          float   Best single trade P&L
worst         float   Worst single trade P&L
win_rate      float   win_count / sell_count
avg_win       float   Average win size
avg_loss      float   Average loss size
expectancy    float   avg_win × win_rate − avg_loss × loss_rate
```

### `get_strategy_stats`
Per-strategy P&L breakdown. Faster than `query_db` for the most common diagnostic query.

Returns: list of `{strategy, trades, wins, win_rate, total_pnl, avg_pct, avg_win, avg_loss}`

### `get_wallet_copy_roi`
TITAN's own copy-trade ROI per tracked wallet — how profitable it has been to **follow** each wallet. Different from their Polymarket stats.

Returns: list of `{wallet_names, signals_followed, wins, total_pnl, avg_pct}` top 30 by PnL.
Note: `wallet_names` is a JSON-encoded list stored as a string.

### `get_trade_history`
Full trade history (buys and sells).

Returns: list of `TradeRecord`
```
type          str     BUY / SELL
ts_str        str     Human-readable timestamp
title         str     Market title
outcome       str
price         float   Entry/exit price
bet           float   Dollar size
pnl_usdc      float   P&L in USD (SELL only)
pnl_pct       float   P&L % (SELL only)
tier          str
wallet_names  list    Source wallet names
strategy      str     Which strategy produced this trade
```

---

## Tools — Logs & Diagnostics

### `get_logs`
Recent engine log lines from memory buffer.

Inputs:
```
lines   int   Number of lines (default 200)
```

Returns: string (newline-separated log lines)

Format: `[YYYY-MM-DD HH:MM:SS] [LEVEL] message`

Levels: `INFO`, `WARN`, `ERR`, `CRITICAL`, `DATA`, `DIAG`, `VERB`

### `get_recent_errors`
Structured list of recent ERROR and CRITICAL log entries.

Inputs:
```
limit   int   Max entries (default 20)
```

Returns: list of `{"message": str}`

### `get_alerts`
Log lines containing ALERT, WARN, or ERR keywords.

Returns: list of `{"msg": str}`

### `get_snapshot`
AI-digestible full state snapshot.

Inputs:
```
compressed   bool   Compact format (default true)
```

Returns: string containing:
- ACCOUNT section (bankroll, P&L, cycles, positions)
- OPEN POSITIONS with live P&L
- SIGNALS from last cycle
- SIGNAL REJECTIONS
- ELITE ROSTER
- TRADE HISTORY (last 100)

---

## Tools — Database

### `get_db_schema`
Current SQLite schema as a compact string. **Call before `query_db`.**

Returns: string listing all tables and columns.

### `query_db`
Execute a read-only SELECT against `titan_state.db`.

Inputs:
```
sql   str   SELECT statement only (INSERT/UPDATE/DELETE rejected)
```

Returns: list of row dicts.

Example:
```sql
SELECT strategy, COUNT(*) as trades, SUM(pnl_usdc) as total_pnl
FROM trade_history WHERE type='SELL'
GROUP BY strategy ORDER BY total_pnl DESC
```

### `get_asset_price_history`
Polymarket price history for a specific outcome token.

Inputs:
```
asset   str   Asset token ID (hex address)
```

Returns: list of `[timestamp, price]` pairs.

---

## Tools — Configuration (Read)

### `get_config`
Full raw `titan_config.json`. Use domain-specific tools for AI analysis.

### `get_config_wallets`
Wallet quality thresholds, elite classification, selector weights.
See [config/CONFIG_WALLETS.md](config/CONFIG_WALLETS.md).

### `get_config_signals`
Signal gates, scoring constants, price/drift zones.
See [config/CONFIG_SIGNALS.md](config/CONFIG_SIGNALS.md).

### `get_config_strategies`
Per-strategy parameters for all 3 builders + global strategy settings.
See [config/CONFIG_STRATEGIES.md](config/CONFIG_STRATEGIES.md).

### `get_config_risk`
Position management, stop-loss, profit target, Kelly constants, timing.
See [config/CONFIG_RISK.md](config/CONFIG_RISK.md).

### `get_config_sizing`
Bankroll, bet caps, Kelly fraction, market quality filters, fees.
See [config/CONFIG_SIZING.md](config/CONFIG_SIZING.md).

### `get_config_sourcing`
Trade sourcing, discovery interval, cache TTLs, VIP/priority wallets.

---

## Tools — Configuration (Write)

All write tools support `dry_run=true` to validate without saving.
All changes are written to `titan_config.json` AND hot-reloaded immediately.
All changes are logged: `Config updated: <domain>/<group> {key: value}`.

### `update_config_wallets`
Inputs:
```
group     str    wallet_quality | elite_thresholds | elite_polling | wallet_selector
patch     dict   {key: new_value} — only existing keys accepted
dry_run   bool   default false
```

Changing `wallet_quality`, `elite_thresholds`, or `wallet_selector` automatically triggers a full wallet re-classification (`_reeval_wallets_impl`) after saving — all cached profiles are re-scored against the new thresholds immediately without any API calls.

### `reeval_wallets`
Re-classify all wallets in the DB using current config thresholds. Does **not** hit the Polymarket API — re-runs `is_selected()` on every stored `profile_json`. Use manually if you edited `titan_config.json` directly (bypassing MCP) or need to force a re-sync.

Returns:
```
ok               bool
reclassified     int    Number of wallets whose tier changed
now_watchable    int    Wallets that gained watchable/verified status
now_unwatchable  int    Wallets that lost watchable/verified status
total            int    Total profiles evaluated
```

### `update_config_signals`
Inputs:
```
group     str    signal_quality | drift_gates | price_zone_gates | strategy_scoring
patch     dict
dry_run   bool
```

### `update_config_strategies`
Inputs:
```
strategy  str    recent_form | drift_discount | consensus_basket | open_book
patch     dict   Applied directly to strategy block — any key accepted
dry_run   bool
```

### `update_config_risk`
Inputs:
```
group     str    position_management | timing | strategy_kelly
patch     dict
dry_run   bool
```

### `update_config_sizing`
Inputs:
```
group     str    bankroll_and_sizing | sizing | market_quality
patch     dict
dry_run   bool
```

---

## SSE Notifications

While connected to `GET /mcp` (SSE stream), the server pushes:

| Event method | Payload | When |
|---|---|---|
| `notifications/message` | `{level, logger, data}` | Every engine log line |
| `titan/heartbeat` | `{ts, cycle_count, ...}` | Every 10 seconds |
| `titan/cycle_complete` | `{signals, wallets, rejects, trades}` | After each 15s cycle |
| `titan/position_open` | Position object | When a trade is entered |
| `titan/position_close` | `{pos, pnl_usdc, pnl_pct}` | When a position is closed |
| `titan/config_updated` | `{domain, group, patch, reeval, refresh}` | After wallet selector/quality/elite config changes |
| `notifications/resources/updated` | `{uri}` | After cycle, and after wallet config changes for affected resources |

---

## Logging Architecture

Understanding the log pipeline helps interpret `notifications/message` events and `get_logs` output.

### Flow

```
Engine module calls _log(msg, level)          [titan_state.py]
        │
        ├── VERB level → titan_verbose.log only (never in memory or SSE)
        │
        ├── Append to SYSTEM_LOGS (in-memory, max 5000 lines)
        ├── Append to Logs/titan.log (disk)
        └── Fire on_log callback
                │
                ▼
        TitanAPI._on_log()                    [titan_api.py]
                │
                ├── Emit "notifications/message" event on internal bus
                └── If ERR/ERROR/CRITICAL → trigger Telegram alert
                        │
                        ▼
                titan_server._on_log()        [titan_server.py] (server mode only)
                        │
                        ├── Append to Logs/titan_server.log (disk)
                        ├── If ERR/ERROR/CRITICAL → print to stdout
                        └── Broadcast SSE notification/message to connected clients
```

### Log Levels

| Level | Meaning | In titan.log | In titan_server.log | Stdout | Telegram |
|---|---|---|---|---|---|
| `INFO` | Normal operation | Yes | Yes | No | No |
| `WARN` | Degraded state, non-fatal | Yes | Yes | No | No |
| `ERR` / `ERROR` | Failure, exception | Yes | Yes | **Yes** | **Yes** |
| `CRITICAL` | Critical failure | Yes | Yes | **Yes** | **Yes** |
| `DATA` | Structured data lines | Yes | Yes | No | No |
| `DIAG` | Diagnostics | Yes | Yes | No | No |
| `VERB` | Verbose HTTP traffic | verbose.log only | No | No | No |

### Log Files

| File | Content |
|---|---|
| `Logs/titan.log` | All engine logs (all levels except VERB) |
| `Logs/titan_server.log` | Server session logs — startup, client connections, all engine logs, config changes |
| `Logs/titan_verbose.log` | Raw HTTP request/response traffic (only when `VERBOSE_HTTP=true`) |
| `Logs/titan_server_YYYYMMDD_HHMMSS.log` | Rotated backups — created on each server start |

### Startup / Shutdown in Logs

These lines always appear in both stdout and `titan_server.log`:
```
[2026-06-14 11:00:49] Startup: markets=156 | wallets=400 ...
[2026-06-14 11:00:49] Startup recovery: signals=0 | rejects=50
[2026-06-14 11:00:49] MCP 2025-11-25  127.0.0.1:8765  tools=30  auth=no
[2026-06-14 11:35:22] [INFO ] Client connected: claude-code v1.2  proto=2025-11-25  sid=abc123
[2026-06-14 11:35:22] [INFO ] SSE stream opened  sid=abc123  addr=127.0.0.1
[2026-06-14 11:35:40] [INFO ] SSE stream closed  sid=abc123  addr=127.0.0.1
[2026-06-14 12:00:00] Server stopped
```

### Config Changes in Logs

Every `update_config_*` call logs:
```
[2026-06-14 11:35:22] [INFO ] Config updated: risk/timing {'MIN_HOLD_MINUTES': 6}
```
This provides a full audit trail of all parameter changes.

---

## Example Analysis Session

```python
# 1. Connect
initialize(protocolVersion="2025-11-25", clientInfo={name: "analyst"})

# 2. Quick health check
status = get_status()
overview = get_portfolio_overview()

# 3. If errors, investigate
if overview["recent_error_count"] > 0:
    errors = get_recent_errors(limit=10)

# 4. Full state
snapshot = get_snapshot(compressed=True)

# 5. Understand current signals
signals = get_signals(min_score=0)
rejects = get_rejects()

# 6. Check performance
stats = get_trade_stats()
# If win_rate < 0.45 or expectancy < 0: investigate config

# 7. Read relevant config
if stats["win_rate"] < 0.45:
    risk_cfg = get_config_risk()
    signal_cfg = get_config_signals()

# 8. Propose a change
update_config_signals(
    group="signal_quality",
    patch={"MIN_CONFLUENCE": 2},
    dry_run=True
)

# 9. Apply
update_config_signals(
    group="signal_quality",
    patch={"MIN_CONFLUENCE": 2},
    dry_run=False
)
```
