# Wallet Storage Migration — DB Single Source of Truth

## Goal
Remove `titan_whales.json` as a second storage for wallet profiles.
Single source of truth = SQLite DB.
Keep in-memory `wallet_cache` for TTL dedup only (avoids API re-calls within 600s).
On restart load only `watchable=1` wallets, capped at `MAX_WATCHLIST_SIZE`.

## Status

| Step | Status | Notes |
|------|--------|-------|
| 1. Extend DB watchlist table | ✅ done | `watchable` + `profile_json` added via `_migrate_add_columns` |
| 2. New DB functions | ✅ done | `upsert_wallet_profile`, `load_watchable_wallets`, `set_watchable` |
| 3. Replace save_whale_roster | ✅ done | Writes profiles to DB; JSON write removed |
| 4. Replace _load_whale_roster | ✅ done | Reads from DB via `load_watchable_wallets(MAX_WATCHLIST_SIZE)` |
| 5. Watchlist pruning → set_watchable OFF | ✅ done | Pruned addresses toggled off in DB |
| 6. Remove WALLET_FILE references | ✅ done | Removed from titan_config.py; `titan_wallets.json` on disk can be deleted manually |

---

## Step 1 — Extend DB `watchlist` table

File: `ScriptsTitan/titan_db.py`

Current schema:
```sql
CREATE TABLE IF NOT EXISTS watchlist (
    address     TEXT     NOT NULL PRIMARY KEY,
    added_at    DATETIME NOT NULL
);
```

New schema (migration via ALTER TABLE if table already exists):
```sql
CREATE TABLE IF NOT EXISTS watchlist (
    address      TEXT     NOT NULL PRIMARY KEY,
    added_at     DATETIME NOT NULL,
    watchable    INTEGER  NOT NULL DEFAULT 1,
    profile_json TEXT
);
```

Migration logic in `init_db()`: run `ALTER TABLE watchlist ADD COLUMN` for each new column
with `IF NOT EXISTS` guard (SQLite doesn't support that natively — use try/except).

---

## Step 2 — New DB functions

File: `ScriptsTitan/titan_db.py`

```python
def upsert_wallet_profile(addr: str, profile: WalletProfile) -> None:
    # INSERT OR REPLACE with watchable flag from profile, profile serialised as JSON

def load_watchable_wallets(limit: int) -> dict[str, WalletProfile]:
    # SELECT WHERE watchable=1 ORDER BY score DESC LIMIT limit
    # Returns dict[address, WalletProfile]

def set_watchable(addr: str, flag: bool) -> None:
    # UPDATE watchlist SET watchable=? WHERE address=?
```

---

## Step 3 — Replace `save_whale_roster`

File: `ScriptsTitan/titan_persistence.py`

- Remove JSON write (`open(WALLET_FILE, "w")`)
- For each `watchable or verified` entry in `wallet_cache`: call `DB.upsert_wallet_profile(addr, profile)`
- Keep `_whale_performance` copy_performance logic — embed it in profile before saving or keep separate
- Keep hedge wallets logic — move to separate DB table or keep in state JSON (out of scope here)
- Remove `save_whale_roster_async` JSON path; keep async wrapper

---

## Step 4 — Replace `_load_whale_roster`

File: `ScriptsTitan/titan_persistence.py`

- Call `DB.load_watchable_wallets(MAX_WATCHLIST_SIZE)`
- Populate `wallet_cache` with loaded profiles (mark ts = now - WALLET_TTL + 60 so they re-score soon)
- Populate `watchlist` from loaded addresses
- Remove all JSON fallback paths (`os.path.exists(WALLET_FILE)`, `open(WALLET_FILE)`)

---

## Step 5 — Watchlist pruning uses `set_watchable(OFF)`

File: `ScriptsTitan/titan_wallet.py` — `discover_new_wallets()`

Current (lines 723-729): clears and rebuilds `watchlist` set in memory.
Keep that. Additionally: for addresses removed from the in-memory watchlist, call `DB.set_watchable(addr, False)`.
For addresses added, call `DB.set_watchable(addr, True)` (or upsert via `upsert_wallet_profile`).

---

## Step 6 — Remove WALLET_FILE

- `titan_config.py`: remove `WALLET_FILE` constant
- `titan_config.json`: remove `WALLET_FILE` key if present
- `titan_persistence.py`: remove import of `WALLET_FILE`
- Delete `titan_whales.json` from disk if it exists

---

## Key invariants
- `wallet_cache` (in-memory) remains. It is the TTL cache for the current session only. Not persisted.
- DB `watchlist.watchable=1` is the authoritative list of who to poll each cycle.
- On restart: `watchlist` set is populated exclusively from DB `watchable=1` rows, capped at `MAX_WATCHLIST_SIZE` ordered by score DESC.
- Wallets are never deleted from DB. `watchable` is toggled ON/OFF.
- `MAX_WATCHLIST_SIZE` cap applies both at runtime (pruning) and at load time (ORDER BY score LIMIT).
