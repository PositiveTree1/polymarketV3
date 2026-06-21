# Plan: Wallet Intelligence - Trade History Persistence

> Phase 2 work. Prerequisite: bool->status refactor (`PLAN_WALLET_STATUS.md`) - **DONE**.

## Goals
1. Use both `/trades` and `/activity` with the best real-world pagination each endpoint allows.
2. Persist wallet trade history for VER+ELITE wallets and accumulate incrementally without re-fetching what we already know.
3. Use stored trade and resolution history to improve scoring, especially `n_resolved`, realised PnL quality, and trader-quality evaluation.

---

## Current state (as of 2026-06-21)

### `watchlist` table - done
- Columns: `address`, `added_at`, `watchable`, `status` (INTEGER, WalletTier), `profile_json`
- `status` is a real column with backfill migration - no longer derived from bools
- `profile_json` is `Wallet.to_db_dict()` blob - includes `score`, `win_rate`, `status`, etc.
- Functions: `upsert_wallet_profile(addr, wallet: Wallet)`, `load_watchable_wallets`, `clear_wallet_profile`

### `Wallet` dataclass - done
- `status: WalletTier` replaces `verified` / `watchable` / `elite` bools
- `is_elite`, `is_verified`, `is_watchable` are property predicates
- `loaded_trade_count`, `trade_load_limited` track how many trades were loaded
- `first_loaded_trade_ts`, `last_loaded_trade_ts` track oldest/newest loaded trade timestamps
- `loaded_trade_pnl` tracks PnL from the currently loaded live window

### What is NOT yet done
- No `wallet_trades` table - wallet trade rows are not persisted
- No `RawTrade` dataclass - trade data still lives as raw dicts inside `fetch_real_winrate`
- No incremental loader - `/activity` is still called with a fixed limit and no pagination logic
- `/trades` is not yet used for scoring
- `TRADES_LIMIT`, `TRADES_MAX_OFFSET`, `ACTIVITY_MAX_OFFSET` are not yet formal config constants

### Live API findings (measured on 2026-06-21)

Measured with standalone probes against a heavy wallet:

#### `/trades`
- Best working page size observed: `limit=1000`
- Working offsets observed: `0`, `1000`, `2000`, `3000`
- `offset=4000` failed with:
  - `400 {"error":"max historical activity offset of 3000 exceeded"}`
- Practical maximum reachable rows observed: about `4000`
- Candidate date/timestamp filters tested and ignored:
  - `startTs`
  - `endTs`
  - `since`
  - `before`
  - `after`
  - `startDate`
  - `endDate`
  - `timestamp_lt`
  - `timestamp_gt`

#### `/activity?type=TRADE`
- Best working page size observed: `limit=500`
- Working offsets observed: `0`, `500`, `1000`, `1500`, `2000`, `2500`, `3000`
- `offset=3500` failed with the same `400`
- Practical maximum reachable rows observed: about `3500`

#### Implication
- Do **not** design around `2 pages x 10k`
- Use defensive pagination that stops on:
  - short page
  - empty page
  - repeated page
  - HTTP 400 offset ceiling
  - first row timestamp not newer than DB watermark

## Evaluation philosophy

The scoring goal is **not** "reconstruct lifetime performance."

The scoring goal is:

- evaluate the visible trading slice fairly
- compare wallets on the same observable recent sample
- estimate trader quality from partial history

Working assumption:

- every serious wallet is truncated
- `/trades` and `/activity` each expose only the recent visible slice
- absolute lifetime PnL and lifetime win counts are not comparable across wallets

So the correct comparison target is:

```text
visible_recent_window = last reachable ~3000-4000 rows
```

Every main metric should answer:

- inside the visible sample, does this wallet behave like a skilled trader?

Not:

- how much money did this wallet make over its full lifetime?

## Current code audit: `titan_wallet.py` and selector

Current wallet classification still depends on a legacy live-window function:

- `fetch_real_winrate(wallet)` in `titan_wallet.py`
  - pulls `/activity` `REDEEM`
  - pulls `/activity` `TRADE side=BUY`
  - infers losses from current positions when prices are near zero or redeemable with negative PnL
  - falls back to an `open_positions_proxy` win rate when no resolved trades match
  - returns `win_rate`, `wilson_lb`, `avg_profit`, `avg_bet`, `trades_per_hour`, `recent_pnl_30d`, `recent_pnl_7d`
- `Wallet.reclassify()` currently recomputes `alpha_per_trade` from `loaded_trade_pnl / n_resolved`
- `PerformanceSelector.score()` currently weights:
  - `wallet.wilson_lb`
  - `wallet.pnl_pct`
  - `wallet.total_value`
  - `wallet.n_resolved`
  - `wallet.n_pos`
  - `wallet.avg_profit`
- `PerformanceSelector.is_selected()` currently gates on:
  - raw win rate
  - Wilson lower bound
  - total PnL
  - average profit
  - portfolio/current value
  - alpha per trade

These fields must remain for compatibility while the new data model is introduced, but they should stop being the main selector inputs once stored sample metrics exist.

Metrics to demote from selector decision-making:

- `pnl_pct` as a primary quality signal
- `avg_profit` as a primary quality signal
- `alpha_per_trade` derived from `loaded_trade_pnl / n_resolved`
- `open_positions_proxy` win rate
- raw `win_rate` without sample-size adjustment

Leaderboard-derived signals should **not** be removed:

- `total_pnl`
- leaderboard PnL
- leaderboard rank
- leaderboard volume

Reason:

- these are not limited by our missing `/trades` or `/activity` history
- they are independent external signals about wallet scale and observed public performance
- they are still important configurable selector factors

Use them as context / weighting / gates, not as replacements for sample-quality metrics:

- good use: boost confidence when leaderboard PnL agrees with stored sample quality
- good use: gate elite status with configurable minimum leaderboard PnL / rank / volume
- bad use: classify a wallet as skilled only because leaderboard PnL is high while sample ROI, markout, and open MTM are weak

Metrics to keep for display or fallback only:

- `loaded_trade_count`
- `trade_load_limited`
- `first_loaded_trade_ts`
- `last_loaded_trade_ts`
- `recent_pnl_30d`
- `recent_pnl_7d`
- `avg_bet`
- `trades_per_hour`
- `total_value`

## Implementation roadmap

The implementation must be split into steps that can be stopped between commits with the app still running. Each step should compile, preserve the existing UI/API contracts, and avoid changing selector behaviour until the final selector step.

**Legend:** `[ ] TODO` · `[~] IN PROGRESS` · `[x] DONE`

### Step 1 - Add config constants only `[ ] TODO`

Files:

- `ScriptsTitan/titan_config.py`
- `titan_config.json` if config persistence needs defaults

Add constants:

```python
ACTIVITY_LIMIT: int = 500
TRADES_LIMIT: int = 1000
ACTIVITY_MAX_OFFSET: int = 3000
TRADES_MAX_OFFSET: int = 3000
```

> Note: `ACTIVITY_LIMIT` exists but is set to `0`; `TRADES_LIMIT`, `ACTIVITY_MAX_OFFSET`, `TRADES_MAX_OFFSET` are missing.

Acceptance checks:

- app starts
- existing wallet scoring still calls `fetch_real_winrate`
- no selector behaviour changes

### Step 2 - Add typed models without wiring them `[ ] TODO`

File:

- `ScriptsTitan/titan_wallet.py`

Add dataclasses near `WinRateData`:

- `RawTrade`
- `TradeClosure`
- `WalletQualityMetrics`

`WalletQualityMetrics` should contain the new sample-based values but default to neutral/empty values:

```python
@dataclass
class WalletQualityMetrics:
    resolved_positions: int
    open_positions: int
    median_position_roi: float | None
    trimmed_mean_position_roi: float | None
    money_weighted_roi: float | None
    position_weighted_roi: float | None
    profit_factor: float | None
    wilson_winrate_lb: float
    realised_win_rate: float
    mtm_roi: float | None
    open_mtm_pnl: float
    median_24h_markout: float | None
    positive_24h_markout_rate: float | None
    top_5_pnl_share: float | None
    profitable_rolling_50_rate: float | None
    sample_quality_factor: float
    concentration_factor: float
    open_risk_factor: float
    data_truncation_factor: float
    confidence: float
    data_quality: str
```

Acceptance checks:

- type import works
- no runtime path uses these models yet
- app behaviour unchanged

### Step 3 - Add `wallet_trades` table and DB helpers `[ ] TODO`

File:

- `ScriptsTitan/titan_db.py`

Create the table and indexes from Phase 1 below. Add helpers:

- `upsert_wallet_trades`
- `get_wallet_last_trade_ts`
- `get_wallet_last_activity_ts`
- `get_wallet_trade_count`
- `get_wallet_resolved_trade_count`
- `get_wallet_realised_pnl`
- `apply_wallet_trade_closures`

Acceptance checks:

- `init_db()` migrates existing DB without deleting data
- app starts with old scoring
- DB helper smoke test can insert and update a small in-memory or temp DB sample

### Step 4 - Add endpoint paginators, not scoring `[ ] TODO`

File:

- `ScriptsTitan/titan_wallet.py`

Add these functions:

- `fetch_wallet_trades_incremental(wallet: str, last_known_ts: float | None) -> list[RawTrade]`
- `fetch_wallet_activity_closures_incremental(wallet: str, last_activity_ts: float | None) -> list[TradeClosure]`

Rules:

- `/trades`: `limit=1000`, offsets `0,1000,2000,3000`
- `/activity`: `limit=500`, offsets `0,500,1000,1500,2000,2500,3000`
- stop on short page, empty page, repeated page, HTTP 400, or DB watermark
- log failures; do not silently swallow exceptions
- return an empty list on non-fatal API unavailability after logging

Acceptance checks:

- standalone smoke script can fetch rows for one wallet
- no production scoring path uses the new paginators yet
- app still starts and scores exactly as before

### Step 5 - Wire persistence after wallet classification `[ ] TODO`

File:

- `ScriptsTitan/titan_wallet.py`

In `get_compute_and_store_wallet`, after `result = _draft.reclassify(sel)`:

```python
if result.is_ranked:
    last_trade_ts = DB.get_wallet_last_trade_ts(wallet)
    new_trades = fetch_wallet_trades_incremental(wallet, last_trade_ts)
    if new_trades:
        DB.upsert_wallet_trades(wallet, new_trades)

    last_activity_ts = DB.get_wallet_last_activity_ts(wallet)
    closures = fetch_wallet_activity_closures_incremental(wallet, last_activity_ts)
    if closures:
        DB.apply_wallet_trade_closures(wallet, closures)
```

Important:

- do not use stored metrics for selector decisions yet
- if persistence fails, log it and keep the current wallet result
- do not demote a wallet because persistence failed

Acceptance checks:

- app still starts
- verified/elite wallet refresh writes rows
- existing `Wallet` fields remain populated by `fetch_real_winrate`

### Step 6 - Build metric computation from stored rows `[ ] TODO`

Files:

- `ScriptsTitan/titan_db.py`
- `ScriptsTitan/titan_wallet.py`

Add a DB read helper:

- `load_wallet_trade_rows(wallet: str) -> list[WalletTradeRow]`

Add a pure metric function:

- `compute_wallet_quality_metrics(rows: list[WalletTradeRow], open_positions: list[WalletOpenPosition]) -> WalletQualityMetrics`

Implementation notes:

- group rows by `condition_id + asset`
- compute resolved position ROI from closed/redeemed rows
- compute Wilson lower bound from resolved position wins/losses
- compute profit factor from gross positive/negative realised PnL
- compute MTM from current open positions and stored cost basis
- compute concentration from realised PnL grouped by market
- return neutral values when sample size is too small

Acceptance checks:

- unit/smoke test with synthetic rows covers wins, losses, open MTM, concentration
- function is deterministic and does not call APIs
- app still uses legacy selector inputs

### Step 7 - Add new `Wallet` fields as read-only diagnostics `[ ] TODO`

File:

- `ScriptsTitan/titan_wallet.py`

Add fields to `Wallet`, `to_wire`, `to_db_dict`, and `from_db`:

- `stored_trade_count`
- `stored_last_trade_ts`
- `stored_resolved_count`
- `stored_realised_pnl`
- `quality_confidence`
- `data_quality`
- `trimmed_roi`
- `profit_factor`
- `mtm_roi`
- `median_24h_markout`
- `positive_24h_markout_rate`
- `top_5_pnl_share`

Wire values from `compute_wallet_quality_metrics` after persistence. Keep old fields populated.

Acceptance checks:

- cached wallets load from old DB blobs with defaults
- API/wire output remains backward compatible
- UI does not break if it ignores new fields

### Step 8 - Demote inappropriate metrics inside `titan_wallet.py` `[ ] TODO`

File:

- `ScriptsTitan/titan_wallet.py`

Do not delete old fields yet. Change their role:

- keep `fetch_real_winrate` as a legacy fallback and recent-form helper
- rename its use in comments/docs as `legacy_live_window_stats`
- stop using `loaded_trade_pnl / n_resolved` as the preferred `alpha_per_trade` once stored quality metrics exist
- prefer stored `wilson_winrate_lb`, `stored_resolved_count`, and stored realised metrics for wallet detail text when available
- keep `open_positions_proxy` only as display/fallback, never as a high-confidence resolved result

Acceptance checks:

- app still classifies with the old selector
- wallet details show whether stored sample metrics are available
- no existing API field disappears

### Step 9 - Update selector params and scoring to support new metrics `[ ] TODO`

File:

- `ScriptsTitan/titan_selector.py`

Add selector params:

- `min_resolved_positions_good`
- `min_resolved_positions_promising`
- `min_profit_factor`
- `min_trimmed_roi`
- `min_wilson_lb`
- `min_mtm_roi`
- `max_top_5_pnl_share`
- `min_positive_24h_markout_rate`
- `weight_markout`
- `weight_trimmed_roi`
- `weight_profit_factor`
- `weight_wilson`
- `weight_consistency`
- `weight_sizing_quality`

Update `PerformanceSelector.score()`:

- if stored quality metrics exist, use the new `skill_score * factors`
- if stored metrics are missing, fall back to the old score path

Update `PerformanceSelector.is_selected()`:

- GOOD/VERIFIED should require enough resolved positions, positive trimmed ROI, acceptable profit factor, Wilson LB, and open MTM not strongly negative
- ELITE should require stronger confidence, lower concentration, and stronger markout/ROI
- keep leaderboard PnL/rank/volume and portfolio scale as configurable elite factors
- do not let leaderboard PnL alone override weak sample ROI, weak markout, or bad open MTM
- remove `avg_profit` as an elite quality gate once stored metrics are available

Acceptance checks:

- no stored metrics: selector behaves close to current logic
- stored metrics available: selector uses new metrics
- `reclassify_all()` works without API calls
- config live reload still works

### Step 10 - Update config, UI labels, and docs `[ ] TODO`

Files:

- `titan_config.json`
- `docs/config/CONFIG_WALLETS.md`
- `ScriptsTitan/titan_ui.py` if selector fields are surfaced there
- `ScriptsTitan/titan_api.py` if MCP config descriptions mention old weights

Replace old selector language:

- reduce emphasis on total PnL, `avg_profit`, and raw win rate
- document visible-sample scoring and confidence
- expose new thresholds in config docs

Acceptance checks:

- `get_config_wallets` shows the new selector keys
- `update_config_wallets(group="wallet_selector", ...)` accepts the new keys
- UI config tab does not crash if it has not yet rendered every new key

---

## Detailed schema and model notes

### Phase 1 - `wallet_trades` table + DB functions (`titan_db.py`)

We need one persisted row per economic trade lifecycle, not parallel raw copies of `/trades` and `/activity`.

```sql
CREATE TABLE wallet_trades (
    id             INTEGER  PRIMARY KEY AUTOINCREMENT,
    wallet         TEXT     NOT NULL,
    condition_id   TEXT     NOT NULL,
    asset          TEXT     NOT NULL,
    side           TEXT     NOT NULL,    -- BUY | SELL
    outcome        TEXT,
    title          TEXT,

    entry_ts       DATETIME NOT NULL,
    entry_price    REAL     NOT NULL,
    entry_size     REAL     NOT NULL,
    entry_cash     REAL     NOT NULL,

    source         TEXT     NOT NULL,    -- trades | activity
    status         TEXT     NOT NULL,    -- OPEN | REDEEMED | SOLD | PARTIAL | UNKNOWN

    close_ts       DATETIME,
    close_price    REAL,
    close_cash     REAL,
    redeem_value   REAL,
    realised_pnl   REAL,
    hold_minutes   REAL,
    fee_estimate   REAL,

    close_type     TEXT,                 -- REDEEM | SELL
    close_source   TEXT,                 -- activity

    UNIQUE(wallet, condition_id, asset, side, entry_ts)
);

CREATE INDEX idx_wt_wallet_entry_ts ON wallet_trades (wallet, entry_ts DESC);
CREATE INDEX idx_wt_wallet_status_ts ON wallet_trades (wallet, status, entry_ts DESC);
CREATE INDEX idx_wt_wallet_close_ts ON wallet_trades (wallet, close_ts DESC);
```

New DB functions:
- `upsert_wallet_trades(wallet: str, trades: list[RawTrade]) -> int`
  - bulk insert-or-ignore for entry/open rows
- `get_wallet_last_trade_ts(wallet: str) -> float | None`
  - newest stored entry timestamp
- `get_wallet_last_activity_ts(wallet: str) -> float | None`
  - newest stored closure/resolution timestamp
- `get_wallet_trade_count(wallet: str) -> int`
  - total stored economic trades
- `get_wallet_resolved_trade_count(wallet: str) -> int`
  - stored count with terminal outcome
- `get_wallet_realised_pnl(wallet: str) -> float`
  - realised PnL from stored closed trades
- `apply_wallet_trade_closures(wallet: str, closures: list[TradeClosure]) -> int`
  - update matching stored rows with redeem/sell outcomes

### Storage model

- We need **both** endpoints for ingestion:
  - `/trades` for entries / opens
  - `/activity` for closure / resolution events
- We do **not** store both raw feeds separately
- A trade that later resolves via `REDEEM` remains **one stored row**
- That row is enriched with:
  - close date
  - close cash / redeem value
  - sold or redeemed status
  - realised PnL
  - hold duration

This gives enough information for trader-quality evaluation without duplicating history.

### Phase 2 - typed models (`titan_wallet.py`)

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
    source:       str          # trades | activity
```

```python
@dataclass
class TradeClosure:
    condition_id: str
    asset:        str
    side:         str
    close_type:   str          # REDEEM | SELL
    close_ts:     float
    close_price:  float | None
    close_cash:   float
    realised_pnl: float | None
```

### Phase 3 - incremental loaders (`titan_wallet.py`)

Use each endpoint for what it is best at:

- `/trades` for entry/open history
- `/activity` for `REDEEM` events and optional `SELL` closure events

#### `fetch_wallet_trades_incremental(wallet, last_known_ts)`
- first load:
  - paginate `/trades` with `limit=1000`
  - offsets: `0`, `1000`, `2000`, `3000`
- refresh:
  - start at offset `0`
  - stop once rows are not newer than DB watermark
- stop on:
  - short page
  - empty page
  - repeated page
  - HTTP 400 ceiling
- dedup by `(condition_id, asset, side, ts)`
- returns `list[RawTrade]`

#### `fetch_wallet_activity_closures_incremental(wallet, last_activity_ts)`
- fetch `/activity` for:
  - `type=REDEEM`
  - and optionally `type=TRADE&side=SELL` if we want early-exit behaviour
- first load:
  - paginate with `limit=500`
  - offsets: `0`, `500`, `1000`, `1500`, `2000`, `2500`, `3000`
- refresh:
  - start at offset `0`
  - stop once rows are not newer than DB watermark
- normalise to `TradeClosure`
- match closures onto stored rows by `(condition_id, asset)` and chronology

### Config additions (`titan_config.py`)

```python
ACTIVITY_LIMIT: int = 500
TRADES_LIMIT: int = 1000
ACTIVITY_MAX_OFFSET: int = 3000
TRADES_MAX_OFFSET: int = 3000
```

### Phase 4 - wire into `get_compute_and_store_wallet` (`titan_wallet.py`)

After `reclassify()`, if `result.is_verified`:

```python
last_trade_ts = DB.get_wallet_last_trade_ts(wallet)
new_trades = fetch_wallet_trades_incremental(wallet, last_trade_ts)
if new_trades:
    DB.upsert_wallet_trades(wallet, new_trades)

last_activity_ts = DB.get_wallet_last_activity_ts(wallet)
closures = fetch_wallet_activity_closures_incremental(wallet, last_activity_ts)
if closures:
    DB.apply_wallet_trade_closures(wallet, closures)
```

Use stored values to override live-window values when richer:
- `DB.get_wallet_trade_count(wallet)` for total known economic trades
- stored resolved count for `n_resolved`
- stored realised PnL for trader-quality scoring

### Phase 5 - additional `Wallet` fields

```python
stored_trade_count:    int
stored_last_trade_ts:  float | None
stored_resolved_count: int
stored_realised_pnl:   float
```

Populate after DB write. Expose in `to_wire()`, `to_db_dict()`, and `from_db()`.

### Phase 6 - trader-quality metrics built from stored rows

Main metrics should be sample-based trader-quality estimators, not lifetime aggregates.

#### Best primary metrics under partial history

1. Observed resolved position ROI
- Group entries by `condition_id + asset`
- Compute:
  - `position_cost`
  - `redeem_value` or `close_cash`
  - `realised_pnl`
  - `roi = realised_pnl / position_cost`
- Primary outputs:
  - `median_position_roi`
  - `trimmed_mean_position_roi`
- Rationale:
  - mean ROI alone is too easy to distort with one outsized winner

2. Wilson lower-bound win rate
- For resolved positions:
  - win = `realised_pnl > 0`
  - loss = `realised_pnl <= 0`
- Use Wilson lower bound, not raw win rate, as the main reliability metric
- Rationale:
  - penalises small samples correctly

3. Profit factor
- `profit_factor = gross_profit / abs(gross_loss)`
- Better than total PnL because it compares gains to losses inside the same visible sample
- Only trust it with enough resolved positions

4. Mark-to-market ROI, not only redeemed PnL
- Compute:
  - `realised_pnl` from resolved positions
  - `unrealised_pnl = current_position_value - cost_basis`
  - `mtm_pnl = realised_pnl + unrealised_pnl`
  - `mtm_roi = mtm_pnl / total_cost`
- Rationale:
  - prevents wallets from looking good only because losses remain open and unresolved

5. 1h / 24h markout
- For each visible entry:
  - `entry_price`
  - `price_after_1h`
  - `price_after_24h`
- Derived metrics:
  - `median_24h_markout`
  - `mean_24h_markout`
  - `positive_24h_markout_rate`
- Rationale:
  - this is one of the best direct skill metrics and does not require full lifetime history

6. Position-weighted vs money-weighted edge
- Compute both:
  - `position_weighted_roi = average ROI per position`
  - `money_weighted_roi = total PnL / total cost`
- Interpretation:
  - both positive -> strong
  - money-weighted positive, position-weighted weak -> dominated by a few large wins
  - position-weighted positive, money-weighted weak -> often right but sizes badly

7. Concentration penalty
- Compute:
  - `top_1_market_pnl_share`
  - `top_5_market_pnl_share`
- Rationale:
  - penalise wallets whose visible sample is dominated by one to five lucky markets

8. Consistency / drawdown
- Order resolved positions by time
- Compute:
  - `max_drawdown`
  - `longest_losing_streak`
  - `rolling_50_position_roi`
  - `rolling_100_position_roi`
  - `profitable_rolling_50_rate`
- Rationale:
  - consistency across windows is more meaningful than headline PnL

#### Metrics that should not be main ranking inputs

These remain useful for display, but should not dominate rank:

- total PnL computed only from our truncated trade rows
- total volume computed only from our truncated trade rows
- raw number of wins
- raw win rate
- average profit per trade
- raw number of trades

Reason:

- under 3000-4000 visible rows they mostly measure how much history we happened to see, not necessarily trader skill

Important exception:

- leaderboard PnL, leaderboard rank, and leaderboard volume are external signals and should remain available to the selector
- they should be configurable as scale/context factors, especially for elite classification
- they should not replace the sample-quality metrics that measure whether the visible trading slice looks skilled

#### Recommended score structure

```text
skill_score =
    25% markout_score
  + 20% trimmed_resolved_roi
  + 20% profit_factor_score
  + 15% Wilson_winrate_score
  + 10% consistency_score
  + 10% sizing_quality_score
```

Then apply penalties:

```text
final_score =
    skill_score
  * sample_quality_factor
  * concentration_factor
  * open_risk_factor
  * data_truncation_factor
```

Recommended penalty logic:

- `sample_quality_factor`
  - `1.00` if resolved positions >= 300
  - `0.85` if 100-299
  - `0.65` if 30-99
  - `0.35` if <30
- `concentration_factor`
  - `1.00` if top-5 PnL share < 40%
  - `0.85` if 40-60%
  - `0.65` if 60-80%
  - `0.45` if >80%
- `open_risk_factor`
  - `1.00` if open MTM is healthy
  - `0.75` if open MTM is mildly bad
  - `0.50` if open MTM hides large losses
- `data_truncation_factor`
  - should reduce confidence more than skill
  - nearly all active wallets are truncated, so do not over-punish

#### Recommended engine output

Do not output only one scalar rank. Expose skill, confidence, and data quality separately.

Example shape:

```python
{
    "wallet": wallet,
    "score": 73,
    "skill": 78,
    "confidence": 62,
    "data_quality": "B",
    "history": "partial_recent_sample",
    "style": "medium-frequency directional trader",
    "resolved_positions": 184,
    "open_positions": 27,
    "trimmed_roi": 0.118,
    "mtm_roi": 0.074,
    "profit_factor": 1.42,
    "wilson_winrate_lb": 0.54,
    "median_24h_markout": 0.018,
    "positive_24h_markout_rate": 0.57,
    "top_5_pnl_share": 0.38,
    "warning": "History likely truncated; score based on visible recent sample only.",
}
```

#### Practical classification target

GOOD TRADER
- 100+ resolved positions
- positive trimmed ROI
- profit factor > 1.2
- Wilson lower-bound win rate > 0.50-0.52
- positive 24h markout
- open MTM not strongly negative
- top 5 markets do not explain most profit

PROMISING
- 30-100 resolved positions
- positive markout
- positive ROI
- limited confidence

LUCKY / CONCENTRATED
- high PnL
- weak markout
- top 5 markets dominate profit

DANGEROUS
- good realised PnL
- bad open MTM
- negative recent markout

INSUFFICIENT DATA
- too few resolved positions
- or insufficient price / position reconstruction

These are more useful than current live-window-only aggregate metrics for elite selection and wallet quality ranking.

### Phase 7 - `WalletVerdict` (deferred)

The current `watchlist.status` column already persists tier through reclassification.
A formal `WalletVerdict` with `auto/manual` flag and `override_note` is only needed
if we later want manual overrides that survive reclassification.

---

## Key invariant

Incremental loaders read watermarks from **DB**, not in-memory `Wallet`.

- First run on a VER wallet:
  - loads reachable `/trades` history using live working pagination
  - loads reachable `/activity` closure history using live working pagination
  - merges both into one enriched `wallet_trades` row set
- Subsequent refreshes:
  - start from page 0 on both endpoints
  - stop at first row not newer than DB watermark
  - apply only deltas / closures
- Demotion from VER:
  - stops accumulating
  - keeps historical rows
  - no delete

## Files to touch

| File | Change |
|---|---|
| `titan_db.py` | `wallet_trades` table, closure update functions, migration in `init_db` |
| `titan_wallet.py` | `RawTrade`, `TradeClosure`, `WalletQualityMetrics`, both incremental loaders, metric computation, update `get_compute_and_store_wallet`, demote legacy winrate fields |
| `titan_selector.py` | Add new quality-metric selector params and switch scoring/gates to stored sample metrics with legacy fallback |
| `titan_config.py` | Add `TRADES_LIMIT = 1000`, `TRADES_MAX_OFFSET = 3000`, `ACTIVITY_MAX_OFFSET = 3000` |
| `titan_config.json` | Add persisted defaults for new pagination and selector metric thresholds |
| `titan_api.py` | Update MCP config descriptions if they reference old selector weights |
| `titan_ui.py` | Update selector config labels if the new params are shown in the UI |
| `docs/config/CONFIG_WALLETS.md` | Document visible-sample wallet scoring and new selector thresholds |
