# Task Status — Strategy Variables to JSON

**Goal:** Extract every hardcoded strategy variable from Python into `titan_config.json` so all tuning can be done without touching code, with hot-reload support.

**Status: COMPLETE** ✓

---

## What Was Done

### Documentation (prior session)
1. **`TITAN_STRATEGIES.md`** — Complete strategy reference document.
2. **`TITAN_CONTEXT.md`** — Full app context document for AI bootstrapping.
3. **`README.md`** — GitHub README.
4. **`requirements.txt`** — Generated from `.venv`.

### Code task (this session)

All hardcoded strategy variables have been extracted to `titan_config.json`.

#### `titan_config.json` changes
- Added `strategy_scoring` block — all `score_signal()` constants (wallet pts, confluence pts, recency thresholds, opportunity formula, market quality scales, price zone bonuses, multi-whale pts, exit penalty, weekly PnL penalty floor, score discount formula)
- Added `strategy_kelly` block — all `kelly_bet()` constants (score/conf/tier multipliers, score floor formula, large trade boost, adaptive caps array)
- Updated `strategy_consensus_basket` block — added `conviction_portfolio_pct` (0.005) and `opposition_ratio_block` (0.60)
- Added `position_management_ext` block — added `whale_exit_min_sell_fraction` (0.30)

#### `titan_config.py` changes
- Added `strategy_scoring: dict = {}`, `strategy_kelly: dict = {}`, `position_management_ext: dict = {}` declarations
- Added loading of all three new blocks in `reload()`

#### `titan_signals.py` changes
- `score_signal()` — all magic numbers replaced with `_sc.get("key", default)`
- `kelly_bet()` — score discount, smult, conf_mult, tier_mult, score_floor, large_trade boost all read from `strategy_kelly` / `strategy_scoring`
- `_adaptive_bet_caps()` — reads `adaptive_caps` array from `strategy_kelly`; falls back to `MAX_BET_ABS`/`MAX_BET_PCT` for `br >= 30`
- `_build_consensus_basket_signals()` — `_CONVICTION_PORTFOLIO_PCT` and `opposition_ratio > 0.60` both read from `strategy_consensus_basket`
- `check_whale_exits()` — `sell_fraction < 0.30` reads `whale_exit_min_sell_fraction` from `position_management_ext`

---

## Files Touched

| File | Change |
|---|---|
| `titan_config.json` | Added `strategy_scoring`, `strategy_kelly`, `position_management_ext`; updated `strategy_consensus_basket` |
| `titan_config.py` | Added 3 new dict declarations + loading in `reload()` |
| `titan_signals.py` | Replaced all hardcodes in `score_signal()`, `kelly_bet()`, `_adaptive_bet_caps()`, `_build_consensus_basket_signals()`, `check_whale_exits()` |

---

## Risk / Notes

- All changes are backwards-compatible: `cfg.get("key", default)` keeps current behaviour if the JSON key is missing.
- No default values were changed — only extracted. The diff is clean and auditable.
- Hot-reload works automatically: edit `titan_config.json`, hit reload in CONFIG tab, changes take effect next cycle.
