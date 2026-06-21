# Plan: Replace wallet tier bools with a single `status` field

> Prerequisite refactor. Lands ALONE, app verified running, BEFORE the trade-history
> work in PLAN_WALLET_INTELLIGENCE.md.

## Decision summary
- Replace `verified` / `watchable` / `elite` (3 bools) with ONE field `status: WalletTier`.
- `dead` STAYS a separate bool — it is a liveness axis, orthogonal to quality tier.
  (A wallet can be `status=VERIFIED` and `dead=True`.)
- Add `ERROR` to `WalletTier`. Final enum: `ERROR | REJECTED | WATCH | VERIFIED | ELITE`.
- DB: add a real `status INTEGER` column to `watchlist`, backfill on `init_db`, switch
  queries from `watchable=1` to `status >= WATCH`.

## Target enum (titan_wallet.py)
```python
class WalletTier(IntEnum):
    ERROR    = -1     # API returned no data / fetch failed (NEW)
    REJECTED = 0
    WATCH    = 1
    VERIFIED = 2
    ELITE    = 3

    def display(self) -> str: ...   # add ERROR -> "⚠ERR"
```
Helper predicates on Wallet (replace scattered combo reads):
```python
@property
def is_watchable(self) -> bool: return self.status >= WalletTier.WATCH
@property
def is_verified(self) -> bool:  return self.status >= WalletTier.VERIFIED
@property
def is_elite(self) -> bool:     return self.status == WalletTier.ELITE
```
> These keep `if w.is_elite:` etc. reading naturally and collapse `verified or elite`
> combos into `w.is_verified` (since ELITE > VERIFIED, `>=` is correct).

## Core dataclass change (titan_wallet.py)
- Remove fields `verified`, `watchable`, `elite`. Add `status: WalletTier`.
- Keep `dead`, `hft`, `vip`, `sports_bot` as-is.
- `tier()` becomes trivial: `return self.status`. Keep the method (called widely) or
  alias. `tag()` unchanged (uses `tier().display()`).

## The TWO write paths that must change
1. **`Wallet.reclassify()` / `apply_selector()`** — primary path.
   - `selector.is_selected()` currently returns `(watchable, verified, elite, fail_reasons)`.
     CHANGE signature to `(status: WalletTier, fail_reasons: list[str])`.
   - `apply_selector()` returns `(score, status, hft, sports_bot, fail_reasons)`.
   - The fallback (no-selector) branch builds `status` from the same thresholds.
2. **`titan_api.py:846-849`** — a DUPLICATE of the selector logic operating on raw
   `prof` dicts (not Wallet objects). It writes `prof["verified"/"watchable"/"elite"]`.
   This is a reclassify-without-API-refetch path. CHANGE to write `prof["status"]` and
   recompute the watchlist column delta from status instead of `watchable or verified`.
   (Consider unifying with reclassify() later; out of scope here — just port it.)

## Selector change (titan_selector.py)
- `WalletSelector.is_selected` abstract signature -> `tuple[WalletTier, list[str]]`.
- `PerformanceSelector.is_selected`: keep the existing gate math (watchable/verified/elite
  booleans as LOCAL vars), then collapse at the end:
  ```python
  if elite_ok:       status = WalletTier.ELITE
  elif verified_ok:  status = WalletTier.VERIFIED
  elif watchable_ok: status = WalletTier.WATCH
  else:              status = WalletTier.REJECTED
  return status, fail_reasons
  ```
  (The local intermediate bools are fine — they're not stored.)

## Serialization (titan_wallet.py) — back-compat matters
- `to_wire()`: emit `"status": int(self.status)`. ALSO emit legacy
  `"verified"/"watchable"/"elite"` derived from status for one release, so any
  unmigrated wire consumer still works. (titan_client uses from_db, so low risk, but
  cheap insurance.)
- `from_db()`: prefer `d["status"]` if present; ELSE derive from legacy bools:
  ```python
  status = (ELITE if d.get("elite") else VERIFIED if d.get("verified")
            else WATCH if d.get("watchable") else REJECTED)
  ```
  This makes old profile_json blobs load with zero migration of the blob itself.
- `make_stub(watchable=True)`: change param to `status: WalletTier = WalletTier.WATCH`.
  Audit the 2 callers (`titan_persistence._make_dead_wallet`, error stubs in
  `get_compute_and_store_wallet`) — error stubs should pass `WalletTier.ERROR`.

## DB change (titan_db.py)
- Schema: `ALTER TABLE watchlist ADD COLUMN status INTEGER NOT NULL DEFAULT 1` in the
  `_migrate_add_columns` migration.
- Backfill on init: `UPDATE watchlist SET status = (CASE
    WHEN json_extract(profile_json,'$.elite')=1 THEN 3
    WHEN json_extract(profile_json,'$.verified')=1 THEN 2
    WHEN watchable=1 THEN 1 ELSE 0 END)` where status not yet set.
- `upsert_wallet_profile`: write `status` column from `profile.get("status")`. Keep the
  `watchable` column in sync (`watchable = 1 if status >= 1 else 0`) so nothing else breaks.
- `load_watchable_wallets`: `WHERE status >= 1` (keep ORDER BY json score).
- `clear_wallet_profile`: set `status=0` alongside watchable=0.
- `set_watchable(addr, flag)`: also set `status` (WATCH if flag else REJECTED) — or add
  `set_status(addr, status)` and migrate the 2 callers.

## State / persistence (titan_state.py, titan_persistence.py)
- `titan_state.get_watchlist()`: `[w for w,p in cache.items() if p.is_watchable]`.
- `titan_persistence`:
  - `save_wallet_roster` line 44: `if wallet.is_watchable:` (covers verified+elite).
  - `_make_dead_wallet`: `replace(base, status=WalletTier.WATCH, dead=True, ...)`.
  - pin logic (327/354): `replace(w, status=max(w.status, WalletTier.WATCH))`.
  - `_refresh_elite_ver_wallets` (383/399): `if p.is_verified` (>= VERIFIED).
  - log line 438: print `wallet.status.display()`.

## UI / API / server / client
- `titan_ui.py`: replace the manual tier-rank ladder (3684-3691) and the icon ternaries
  (1032, 1072) with `p.status` / `p.status.display()`. The tier filter dropdown
  (3696-3699) compares against `WalletTier` instead of bools. `dead` handling stays.
  Counts (4096-4099, 3750-3751) use `is_verified`/`is_elite`.
- `titan_api.py get_tracked_wallets` tier filter (390-392): map "elite"/"verified"/
  "watchable" string param -> WalletTier comparison.
- `titan_server.py`: no change (only calls to_wire()).
- `titan_client.py`: no change (from_db() handles it).

## signals / trader / engine / market
Pure reads — mechanical swaps:
- `w.elite` -> `w.is_elite`; `w.verified` -> `w.is_verified`.
- `w.elite or w.verified` -> `w.is_verified` (>= VERIFIED includes ELITE).
- `not w.verified and not w.elite` -> `not w.is_verified`.
- Filters `[w for w if w.elite]` -> `[w for w if w.is_elite]`.
- `get_elite()` / `get_watchlist()` in WalletsCache use the predicates.

## Implementation status (2026-06-21)

| File | Status | Notes |
|---|---|---|
| titan_wallet.py | ✅ DONE | WalletTier enum +ERROR, predicates, status field, reclassify/apply_selector/from_db/to_wire/make_stub |
| titan_selector.py | ✅ DONE | is_selected -> (WalletTier, reasons) |
| titan_db.py | ✅ DONE | status column + backfill migration + query swaps |
| titan_state.py | ✅ DONE | get_watchlist predicate |
| titan_persistence.py | ✅ DONE | dead/pin/refresh/save/log swaps |
| titan_api.py | ✅ DONE | dup write path + tier filter + elite counts |
| titan_ui.py | ✅ DONE | tier ladder, icons, filter dropdown, counts, analysis/diagnostics/header stats |
| titan_signals.py | ✅ DONE | is_elite/is_verified |
| titan_signal_builder.py | ✅ DONE | all 7 occurrences (elite_wallets/verified_wallets dicts, gate checks) |
| titan_trader.py | ✅ DONE | wire dict properties |
| titan_engine.py | ✅ DONE | rescore count, cycle counts, cache injection, elite roster |
| titan_market.py | ✅ DONE | elite_addrs, is_elite local, verified gate |

## Verification
1. App boots (`run_titan.py --mode ui`), no exceptions on wallet load from DB.
2. WALLETS tab shows correct tiers + filter dropdown works.
3. A reclassify pass (rescore) promotes/demotes correctly and persists `status`.
4. Old DB rows (pre-migration) load with correct derived status.
5. Restart -> statuses survive (column written, not just derived).

## NOT in scope (deferred to PLAN_WALLET_INTELLIGENCE.md)
- wallet_trades table, RawTrade, incremental /trades loader, n_resolved from DB.
- Manual override / WalletVerdict (auto vs manual). Single `status` is the foundation
  that a future manual-override flag would sit on.
