# Plan: Wallet Intelligence Overhaul

## Goals
1. Explicit wallet verdict (VER / ELITE / WATCH / REJECTED) — persisted, survives reclassification
2. VER→ELITE promotion uses richer trade data — switch from `/activity` (500 limit) to `/trades` (10k limit) with incremental loading
3. Persist trade history for VER+ELITE wallets — accumulate over time, never re-fetch what we already have

---

## What already exists in titan_db.py

### `watchlist` table
- Stores `address`, `added_at`, `watchable`, `profile_json`
- `profile_json` is the full `Wallet.to_db_dict()` blob — includes `verified`, `elite`, `score`, `win_rate`, etc.
- Functions: `upsert_wallet_profile`, `load_watchable_wallets`, `clear_wallet_profile`, `set_watchable`
- **Already covers**: wallet tier persistence, profile reload on boot

### `Wallet` dataclass fields already present
- `loaded_trade_count`, `trade_load_limited` — tracks how many trades were loaded and whether we hit the limit
- `first_loaded_trade_ts`, `last_loaded_trade_ts` — timestamps of oldest/newest loaded trade
- `loaded_trade_pnl` — PnL computed from loaded trades

### What is NOT yet present
- No `wallet_trades` table — raw trade rows per wallet are not persisted
- No `RawTrade` dataclass — trade data stays as raw dicts inside `fetch_real_winrate`
- No incremental loader — `/activity` is called with a fixed limit, no pagination, no delta logic
- `/trades` endpoint is not used at all (only `/activity` for BUY trades)
- No explicit `WalletVerdict` object — verdict is implied by the flags but not a first-class persisted object

---

## Remaining work (gap)

### Phase 1 — `wallet_trades` table in titan_db.py (new)
```sql
CREATE TABLE wallet_trades (
    id           INTEGER  PRIMARY KEY AUTOINCREMENT,
    wallet       TEXT     NOT NULL,
    condition_id TEXT     NOT NULL,
    asset        TEXT     NOT NULL,
    side         TEXT     NOT NULL,    -- BUY | SELL
    size         REAL     NOT NULL,
    price        REAL     NOT NULL,
    cash         REAL     NOT NULL,
    ts           DATETIME NOT NULL,
    outcome      TEXT,
    title        TEXT,
    UNIQUE(wallet, condition_id, asset, side, ts)
);
CREATE INDEX idx_wt_wallet_ts ON wallet_trades (wallet, ts DESC);
```

New DB functions:
- `upsert_wallet_trades(wallet: str, trades: list[RawTrade]) -> int` — bulk INSERT OR IGNORE, returns new row count
- `get_wallet_last_trade_ts(wallet: str) -> float | None` — used by incremental loader
- `get_wallet_trade_count(wallet: str) -> int` — for display

### Phase 2 — `RawTrade` dataclass in titan_wallet.py (new)
```python
@dataclass
class RawTrade:
    condition_id: str
    asset:        str
    side:         str
    size:         float
    price:        float
    cash:         float
    timestamp:    float
    outcome:      str
    title:        str
```

### Phase 3 — `fetch_wallet_trades_incremental` in titan_wallet.py (new)
Uses `/trades` endpoint (limit=10000, not `/activity`).

Logic:
- `last_known_ts is None` → first load, paginate offset=0 then 10000, collect all
- `last_known_ts is set` → load page 0 only, stop processing when ts <= last_known_ts
- Dedup by `(condition_id, asset, side, ts)`
- Returns `list[RawTrade]`

Config addition in `titan_config.py`:
```python
TRADES_LIMIT = 10000   # for /trades endpoint (vs ACTIVITY_LIMIT=500 for /activity)
```

### Phase 4 — Wire it into `get_compute_and_store_wallet`
After `reclassify()`, if `result.verified or result.elite`:
```python
last_ts = DB.get_wallet_last_trade_ts(wallet)
new_trades = fetch_wallet_trades_incremental(wallet, last_ts)
if new_trades:
    n = DB.upsert_wallet_trades(wallet, new_trades)
    # optionally update n_resolved from DB count for better scoring
```

Use `DB.get_wallet_trade_count(wallet)` to override `n_resolved` when it exceeds the live-fetch count — this feeds back into `elite_min_resolved` and `weight_trade_count` scoring.

### Phase 5 — `Wallet` dataclass: two new fields
```python
stored_trade_count:   int           # rows in wallet_trades for this wallet
stored_last_trade_ts: float | None  # newest ts in wallet_trades
```
Populate after DB write. Expose in `to_wire()` / `to_db_dict()`.

### Phase 6 — `WalletVerdict` (optional / later)
The current `watchlist.profile_json` already persists the full tier state.
A formal `WalletVerdict` with `auto/manual` flag and `override_note` is only needed
when we want manual overrides that survive reclassification. Defer until UI needs it.

---

## Key invariant
Incremental loader reads `last_known_ts` from **DB**, not in-memory `Wallet`.
- First run on a VER wallet: loads ~20k trades (2 pages × 10k), stores all
- Subsequent refreshes: loads page 0 only, stops at first trade ≤ last_known_ts, stores delta
- Demotion from VER: stops accumulating, keeps historical rows (no delete)

## Files touched
| File | Change |
|---|---|
| `ScriptsTitan/titan_db.py` | `wallet_trades` table + 3 functions + migration in `init_db` |
| `ScriptsTitan/titan_wallet.py` | `RawTrade`, `fetch_wallet_trades_incremental`, update `get_compute_and_store_wallet`, 2 new `Wallet` fields |
| `titan_config.py` | Add `TRADES_LIMIT = 10000` |
