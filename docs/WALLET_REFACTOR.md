# Wallet Refactor — `WalletProfile` → `Wallet` dataclass

## Goal

Replace the two parallel TypedDict structures (`WalletProfile` in `titan_wallet.py` and `TrackedWalletDict` in `titan_types.py`) with a single `Wallet` dataclass. All internal code uses typed attribute access. Dicts only appear at serialization boundaries (DB JSON, API wire output).

## Status: NOT STARTED (pre-commit baseline)

---

## Steps

| # | File(s) | Task | Status |
|---|---------|------|--------|
| 1 | `titan_wallet.py` | Define `Wallet` dataclass with all 35 fields + methods (`is_hft`, `is_sports_bot_wallet`, `alpha_per_trade`, `is_recent_form_qualified`, `tier`, `to_wire`, `to_db_dict`, `from_db`) | ⬜ todo |
| 1b | `titan_wallet.py` | Delete `WalletProfile` TypedDict and free-function wrappers (`is_hft_wallet`, `is_sports_bot`, `alpha_per_trade`, `is_recent_form_qualified`) | ⬜ todo |
| 2 | `titan_types.py` | Delete `TrackedWalletDict`, `WhaleDict` alias. Keep `WinRateData` (intermediate result, not stored). Keep other dicts (`AlertDict`, etc.) | ⬜ todo |
| 3 | `titan_state.py` | Change `wallet_cache: dict[str, WalletProfile]` → `dict[str, Wallet]` | ⬜ todo |
| 4 | `titan_persistence.py` | `_make_stub()` returns `Wallet(...)`. `_load_wallets_from_db()` uses `Wallet.from_db()`. Remove all `cast(WalletProfile, ...)` | ⬜ todo |
| 5 | `titan_selector.py` | `score(wallet: Wallet)` and `is_selected(wallet: Wallet, score)` replace dict-based signatures. Remove intermediate `raw_for_selector` dict in caller | ⬜ todo |
| 6 | `titan_wallet.py` | `get_compute_and_store_wallet` returns `Wallet`. Replace all dict literals `{...}` with `Wallet(...)`. Replace `.get("field")` with `.field` throughout | ⬜ todo |
| 7 | `titan_db.py` | `upsert_wallet_profile(addr, wallet.to_db_dict())`. `load_watchable_wallets` returns `dict[str, Wallet \| None]` using `Wallet.from_db()` | ⬜ todo |
| 8 | `titan_market.py` `titan_trader.py` `titan_signals.py` | Replace all `.get("name", ...)`, `.get("elite")` etc. with `.name`, `.elite` etc. | ⬜ todo |
| 9 | `titan_ui.py` | Attribute access everywhere internally. `wallet.to_wire()` only at the JSON API output boundary (`get_tracked_wallets`). Temporary dict usage acceptable until UI is stable | ⬜ todo |

---

## Serialization boundaries

| Boundary | Mechanism |
|----------|-----------|
| SQLite / DB save | `wallet.to_db_dict()` → plain `dict` for JSON blob |
| SQLite / DB load | `Wallet.from_db(d: dict)` → `Wallet` |
| API wire (MCP/HTTP) | `wallet.to_wire()` → plain `dict` matching old `TrackedWalletDict` shape |

## Fields staying as dicts (legitimately)

- `WinRateData` — temporary result of `fetch_real_winrate`, never stored
- `WhalePerformanceRecord` / `WhalePerformanceSummary` — separate domain (copy-trade tracking), refactor separately if needed
- `AlertDict`, `ErrorDict`, `PnlSummaryDict`, `TradeStatsDict`, `PortfolioOverviewDict` — out of scope

## Risks

- `titan_selector.py` `score()` currently receives a raw dict — changing signature touches the abstract base class and all subclasses
- `titan_ui.py` does heavy `.get()` on wallet dicts in many render paths — do last, after all other steps are green
