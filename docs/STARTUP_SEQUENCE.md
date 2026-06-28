# Titan Server — Startup Sequence

## Overview

Startup runs in two phases: a **sequential blocking phase** that initialises state, then a **parallel phase** that kicks off background threads. The server can accept HTTP connections after ~5–10s but won't trade until the startup wallet refresh completes (15–70s).

---

## Phase 1 — Sequential (blocking)

### 1. DB initialisation — `titan_db.py DB.init_db()`
- Creates SQLite schema (signals, rejects, price_history, equity_history, watchlist, trade_history, wallet_trades, …)
- Runs schema migrations (column additions, timestamp normalisation)
- `wallet_trades` has `cur_price`, `cash_pnl`, `redeemable` columns (synthetic position per row)
- `wallet_trades_meta` has `pos_ts`, `pos_n`, `pos_init`, `pos_cur`, `pos_cash_pnl` columns (synthetic position snapshot)
- Compacts price_history
- **Cost**: 10ms (warm) / 100–500ms (cold first run)

### 2. Market cache load — `titan_markets.py S.market_cache.load_all_from_db()`
- Loads all cached Market objects from the `markets` table
- Deserialises JSON → Market dataclass
- **DB**: SELECT all from `markets`
- **Cost**: 100ms–2s depending on cache size

### 3. Prices + Wallets services init
- Creates `PricesCacheSrv` and `WalletsCacheSrv` server-side instances
- Replaces the lightweight client-side objects
- **Cost**: < 20ms

### 4. Purge non-watchable wallets — `DB.purge_non_watchable()`
- Deletes stale wallet stubs from `watchlist` that have no profile and are not in the seed list
- **DB**: DELETE + UPDATE
- **Cost**: 10–100ms

### 5. Load wallets from DB — `_load_wallets_from_db()`
- Reads **all** wallets with `status >= 1` (WATCH or above) from `watchlist` — no limit
- Deserialises JSON profiles → Wallet dataclass; assigns VIP names from config
- Adds seed wallet stubs for any missing seeds
- **DB**: SELECT watchlist
- **Cost**: 50–500ms

### 6. Load trading state — `_load_trading_state()`

| Sub-step | What | Cost |
|---|---|---|
| Read `titan_state.json` | bankroll, cooldowns, position-wallet mapping | 10–30ms |
| `DB.load_trade_stats()` | aggregated stats (win/loss counts, P&L sums) — rebuilds from history if missing | 10ms (exists) / 500ms–2s (rebuild) |
| `DB.load_trade_history(limit=5000)` | up to 5 000 TradeRecord rows + wallet + audit joins | 200ms–3s |
| Group + rebuild open positions | in-memory, no I/O | 10–100ms |
| Hydrate position wallets | For each wallet tied to an open position: if already in cache (normal case) → free. Only calls the API if the wallet is missing from cache entirely (DB wiped / first boot). | ~0ms normal / 2–30s if DB wiped |
| Load equity history | SELECT equity curve from DB | 10–100ms |

> **Hotspot** (DB wiped only): if a wallet tied to an open position is missing from cache, calls `/v1/leaderboard`, `/positions`, `/activity?type=REDEEM`, `/activity?type=TRADE&side=BUY` — 4 sequential calls to recompute score/tier. Normal operation: all such wallets are already loaded from DB in step 5, so this costs nothing.

### 7. Config reload — `titan_config.py C.reload()`
- Reads `titan_config.json`
- Instantiates active WalletSelector and SignalBuilder
- Sets all global constants (bankroll, bet limits, strategy mode, …)
- **Cost**: 50–200ms

### 8. Reclassify all wallets — `WalletsCacheSrv.reclassify_all()`
- Re-runs the active WalletSelector on every cached wallet (no API calls)
- Updates DB profiles for any wallet whose tier changed
- **DB**: UPDATE for changed wallets
- **Cost**: 200ms–1s

### 9. WebSocket resolution monitor start — `titan_resolution_monitor.start()`
- Opens WebSocket connection to Polymarket order book
- Syncs open positions with latest prices and resolution status
- **API**: 1 WebSocket subscribe + market queries
- **Cost**: 500ms–2s

---

## Phase 2 — Parallel threads

### Thread A — Startup wallet refresh (slow, blocks main loop)
- `_refresh_elite_ver_wallets()` in `titan_persistence.py`
- Iterates all ELITE/VERIFIED wallets last updated > 2 days ago
- Per wallet — API calls depend on synthetic position cache:

| Wallet state | API calls | What is fetched |
|---|---|---|
| Synthetic position fresh (`pos_ts` < `WALLET_TTL/2` ago) | 2 | `/v1/leaderboard` + `/activity?type=REDEEM` + `/activity?type=TRADE` |
| Synthetic position stale or missing (first time) | 3 | above + `/positions` (stores snapshot + updates open row prices) |

- Win rate computed from stored `cur_price`/`cash_pnl`/`redeemable` on DB rows — no extra API call
- Quality metrics (MTM ROI, profit factor, …) read `cur_price` directly from DB rows — no extra API call
- 0.5s sleep between wallets to respect rate limits
- **API**: 2–3 calls × N stale wallets (N typically 10–50)
- **Cost**: 5–30s wall-clock (down from 10–60s)
- Main trading loop waits for this thread before starting

### Thread B — Main trading loop
- `run_loop()` — 15s cycle: fetch public trades → build signals → auto-trade
- Starts only after Thread A completes
- **API**: 1+ call per 15s cycle

### Thread C — HFT watchdog
- Monitors `HFT_ENABLED`; runs fast loop (3s cycle) when HFT wallets are present
- **API**: 1 call per 3s if enabled

### Thread D — Heartbeat
- Emits periodic heartbeat event every 10s to connected MCP clients
- No API or DB calls

---

## Phase 3 — API server

### Load persisted signals — `_load_persisted_signals_rejects()`
- Loads last 200 signals and 50 rejects from DB
- Marks expired signals as `live=0`
- **DB**: SELECT signals/rejects + UPDATE expired
- **Cost**: 50–200ms

### Telegram boot alert (async)
- Background thread sends boot notification if Telegram is configured
- **API**: 1 Telegram call
- **Cost**: 1–3s (non-blocking)

### HTTP server starts — `ThreadingHTTPServer.serve_forever()`
- Listens on configured host:port for MCP JSON-RPC and SSE requests
- Blocks forever

---

## Synthetic position cache

`/positions` is no longer fetched unconditionally on every wallet refresh. Instead:

1. **First fetch** (no snapshot in DB): fetches `/positions` live → stores aggregate metrics in `wallet_trades_meta` (`pos_ts`, `pos_n`, `pos_init`, `pos_cur`, `pos_cash_pnl`) → updates `cur_price`/`cash_pnl`/`redeemable` on each matching OPEN row in `wallet_trades`
2. **Subsequent refreshes within `WALLET_TTL/2`**: reads aggregate from `wallet_trades_meta`; win rate loss inference reads `cur_price`/`cash_pnl`/`redeemable` from `wallet_trades` rows — zero positions API calls
3. **After `WALLET_TTL/2`**: snapshot is stale → fetches live again and repeats

The consensus scanner (`get_wallet_open_positions`) retains its own lightweight live call (limit=100, filtered) since it genuinely needs current prices for signal building.

---

## Critical path

```
DB init → market cache → wallets from DB → trading state
→ config reload → reclassify → WS monitor
→ Thread A (startup refresh) ← main loop waits here
→ main loop ready to trade
```

| Milestone | Typical time |
|---|---|
| Server accepts HTTP connections | 5–10s |
| Fully initialised and trading | 10–40s (improved from 15–70s) |

---

## Bottlenecks

| Step | Worst case | Root cause | Status |
|---|---|---|---|
| Startup wallet refresh (Thread A) | 5–30s | Sequential API fetches, 0.5s sleep per wallet | **Improved**: `/positions` skipped when snapshot fresh; `/value` removed; win rate + quality metrics read from DB rows |
| Trade history load (Step 6) | 500ms–3s | Full table scan + joins for up to 5 000 rows | Open |
| Reclassify all wallets (Step 8) | 200ms–1s | In-memory only, not blocking | Acceptable |
