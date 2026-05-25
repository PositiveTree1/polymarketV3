# Wallet Selector Refactoring Plan

## Implementation Status

| Phase | Task | Status |
|-------|------|--------|
| 1 | Create `titan_selector.py` — `WalletSelector` ABC + `SelectorParams` base | ✅ Done |
| 1 | Add `wallet_selector` config block to `titan_config.json` + `titan_config.py` | ✅ Done |
| 2 | Implement `PerformanceSelectorParams` dataclass | ✅ Done |
| 2 | Implement `PerformanceSelector.discover()` | ✅ Done |
| 2 | Implement `PerformanceSelector.score()` + `is_selected()` | ✅ Done |
| 3 | Wire `titan_wallet.py` — `get_compute_and_store_wallet` delegates score/tier to selector | ✅ Done |
| 3 | Wire `titan_wallet.py` — `discover_new_wallets` delegates to `selector.discover()` | ✅ Done |
| 3 | Wire `titan_engine.py` — call `discover_new_wallets` | ✅ Done |
| 3 | Wire `titan_market.py` — read tier from `WalletProfile` (no change needed; tier flags unchanged) | ✅ Done |
| 3 | Wire `titan_signals.py` — consume tier field (no change needed; reads `.verified`/`.elite` from profile) | ✅ Done |
| 4 | Rename `WhaleDict` → `TrackedWalletDict` in `titan_types.py` (alias kept) | ✅ Done |
| 4 | Rename `get_whales` → `get_tracked_wallets` in API, protocol, client, server, UI, AI | ✅ Done |
| 5 | UI panel for selector params (Tab 10 in titan_ui.py) | ✅ Done |

> **Implementation complete.**

---

## Goal

Cleanly separate the logic that identifies "wallets of interest" from the rest of the system. Replace the concept of "whale" with the more neutral "tracked wallet" or "wallet of interest". Introduce a `WalletSelector` base class with concrete implementations, each with its own independently configurable parameters.

---

## Current Architecture (Problem)

```
titan_wallet.py → discover_new_whales() / get_compute_and_store_wallet()
titan_market.py → _poll_vip_and_elite() / _poll_watchlist()
titan_signals.py → filters wallets inline per strategy
titan_config.json → thresholds scattered throughout
```

- Selection logic is embedded inside `titan_wallet.py` (`get_compute_and_store_wallet`, `discover_new_whales`)
- Parameters are mixed into `titan_config.json` without clear ownership
- Signal strategies re-filter wallets locally instead of consuming a clean "selected wallet" result
- The name "whale" implies large capital — wrong for all future selectors

---

## Target Architecture

```
Polymarket data
     ↓
WalletSelector (pluggable, configured per UI section)
     ↓
TrackedWallet list  ←  stored / cached
     ↓
Signals + Reporting
```

### Naming Changes

| Old | New |
|-----|-----|
| `whale` / `WhaleDict` | `tracked_wallet` / `TrackedWallet` |
| `get_whales()` | `get_tracked_wallets()` |
| `discover_new_whales()` | `discover_candidates()` |
| `get_compute_and_store_wallet()` (qualification) | moved into selector |
| `ELITE`, `VIP`, `WATCHLIST` tiers | selectors own their tier semantics |

---

## New Components

### 1. `titan_selector.py` (new file)

#### Base class

```python
class WalletSelector(ABC):
    selector_id: str          # unique key, used in config and UI
    display_name: str         # shown in UI dropdown
    params: SelectorParams    # live, dynamic config object

    @abstractmethod
    def score(self, candidate: WalletCandidate) -> float | None:
        """Return 0-1 score, or None to exclude."""

    @abstractmethod
    def is_selected(self, candidate: WalletCandidate) -> bool:
        """Final binary gate after scoring."""

    def discover(self) -> list[WalletCandidate]:
        """Override to change discovery source. Default: leaderboard + high-value trades."""
```

#### Params base class

```python
@dataclass
class SelectorParams:
    """Base for all selector parameter blocks. Subclassed per selector."""
    pass
```

Parameters for each selector live in their own `@dataclass`, loaded from the selector's config section. When the user edits a parameter in the UI, the dataclass is updated in place — the next selection cycle picks up the new values automatically (no restart).

---

### 2. First concrete selector: `PerformanceSelector`

This replaces the current `get_compute_and_store_wallet` tiering logic (WATCHABLE → VERIFIED → ELITE).

```python
@dataclass
class PerformanceSelectorParams(SelectorParams):
    # Discovery
    min_trade_cash_discovery: float      # currently $5000
    leaderboard_periods: list[str]       # ["ALL", "MONTH", "WEEK"]

    # WATCHABLE gate
    min_win_rate_watch: float            # 0.53
    wilson_min_watch: float              # 0.45
    min_resolved_bets: int               # 10
    min_pnl: float                       # 0.0

    # VERIFIED gate
    min_win_rate_ver: float              # 0.56
    wilson_min_ver: float                # 0.49
    min_avg_profit: float                # 2.0
    min_avg_bet: float                   # 10.0
    min_portfolio_or_pnl: float          # 500.0

    # ELITE gate
    elite_min_pnl: float                 # 40_000
    elite_min_portfolio: float           # 80_000
    elite_min_score: float               # 0.72
    elite_min_resolved: int              # 20
    elite_alpha_per_trade: float         # 1.0

    # Scoring weights
    weight_wilson: float                 # 0.30
    weight_pnl_pct: float               # 0.25
    weight_portfolio: float              # 0.15
    weight_trade_count: float            # 0.10
    weight_open_positions: float         # 0.10
    weight_alpha: float                  # 0.10

    # HFT / bot filters
    hft_tph_threshold: float             # 50.0
    sports_bot_tph_threshold: float      # 100.0

class PerformanceSelector(WalletSelector):
    selector_id = "performance"
    display_name = "Performance (High Win Rate + PnL)"
```

---

### 3. Future selectors (examples, not implemented now)

| Selector ID | Description | Key params |
|-------------|-------------|------------|
| `small_wallet` | Identifies wallets with small capital but high ROI | max_portfolio, min_roi |
| `momentum` | Recent activity spike + positive recent PnL | lookback_hours, min_recent_pnl |
| `contrarian` | Wallets betting against consensus on high-conviction markets | min_divergence_pct |
| `hft_follower` | Specifically targets identified HFT bots | min_tph, known_hft_list |

---

## Config Structure

### Current (scattered)
Parameters are mixed into `titan_config.json` as top-level keys (`MIN_WIN_RATE_WATCH`, etc.) or hardcoded in `titan_wallet.py`.

### Target
Each selector owns a named config block:

```json
{
  "active_selector": "performance",
  "selectors": {
    "performance": {
      "min_win_rate_watch": 0.53,
      "wilson_min_watch": 0.45,
      "min_resolved_bets": 10,
      "min_pnl": 0.0,
      "min_win_rate_ver": 0.56,
      "wilson_min_ver": 0.49,
      "min_avg_profit": 2.0,
      "min_avg_bet": 10.0,
      "min_portfolio_or_pnl": 500.0,
      "elite_min_pnl": 40000,
      "elite_min_portfolio": 80000,
      "elite_min_score": 0.72,
      "elite_min_resolved": 20,
      "elite_alpha_per_trade": 1.0,
      "weight_wilson": 0.30,
      "weight_pnl_pct": 0.25,
      "weight_portfolio": 0.15,
      "weight_trade_count": 0.10,
      "weight_open_positions": 0.10,
      "weight_alpha": 0.10,
      "hft_tph_threshold": 50.0,
      "sports_bot_tph_threshold": 100.0
    }
  }
}
```

Config is loaded as a live dataclass. The UI writes directly to this section; no restart required.

---

## UI Changes

Add a **Wallet Selector** panel in the config UI (separate section):

```
┌─ Wallet Selector ──────────────────────────────────┐
│  Active selector: [Performance ▾]                   │
│                                                      │
│  ── Performance Parameters ──────────────────────── │
│  Min win rate (watch):    [0.53]                     │
│  Wilson LB (watch):       [0.45]                     │
│  Min resolved bets:       [10  ]                     │
│  Min PnL (watch):         [0   ]                     │
│  ...                                                 │
│  Elite min PnL:           [40000]                    │
│  Elite min portfolio:     [80000]                    │
│  ...                                                 │
│           [Apply]  changes take effect next cycle    │
└──────────────────────────────────────────────────────┘
```

Switching selector in the dropdown:
- Saves the current selector's params
- Loads the new selector's param panel
- Sets `active_selector` in config
- Takes effect on next discovery cycle

---

## Implementation Steps

### Phase 1 — Types and base infrastructure
1. Add `TrackedWallet` dataclass to `titan_types.py` (replaces `WhaleDict`)
2. Create `titan_selector.py` with `WalletSelector` ABC and `SelectorParams` base
3. Add `SelectorConfig` section to `titan_config.py` / `titan_config.json`

### Phase 2 — PerformanceSelector
4. Implement `PerformanceSelectorParams` dataclass (all current thresholds)
5. Implement `PerformanceSelector.discover()` — wraps current `discover_new_whales`
6. Implement `PerformanceSelector.score()` + `is_selected()` — wraps current `get_compute_and_store_wallet` tiering

### Phase 3 — Wire into the pipeline
7. In `titan_wallet.py`: replace direct calls to `discover_new_whales` / `get_compute_and_store_wallet` with `active_selector.discover()` + `active_selector.is_selected()`
8. In `titan_market.py`: polling tier decisions (`_poll_vip_and_elite`, `_poll_watchlist`) read from `TrackedWallet` tier field set by selector, not hardcoded logic
9. In `titan_signals.py`: strategies consume `TrackedWallet` objects; wallet-level filtering criteria come from `TrackedWallet.tier`, not re-implemented inline

### Phase 4 — Rename
10. Sweep `whale` references: rename `WhaleDict` → `TrackedWallet`, `get_whales` → `get_tracked_wallets`, `discover_new_whales` → `discover_candidates`, API endpoint updated
11. Update `titan_api.py` response key (`whales` → `tracked_wallets`) — check API consumers

### Phase 5 — UI
12. Add Wallet Selector panel to config UI
13. Implement live param reload: on "Apply", write to config JSON, selector dataclass re-reads params next cycle

---

## Files Affected

| File | Change |
|------|--------|
| `titan_types.py` | Add `TrackedWallet`, deprecate `WhaleDict` |
| `titan_selector.py` | **New** — base class + `PerformanceSelector` |
| `titan_wallet.py` | Strip out selection logic, delegate to selector; rename whale → tracked_wallet |
| `titan_market.py` | Read tier from `TrackedWallet`, not hardcoded logic |
| `titan_signals.py` | Consume `TrackedWallet.tier` instead of inline re-filtering |
| `titan_config.py` | Add `SelectorConfig` section, live reload on write |
| `titan_config.json` | Add `selectors` block |
| `titan_api.py` | Rename `get_whales` → `get_tracked_wallets` |
| `ScriptsTitan/titan_config.py` | Add selector param UI panel |

---

## Invariants to Preserve

- Discovery cycle interval and caching (TTL, max watchlist size) stay in `titan_config.json` as system params, not selector params
- HFT and sports bot detection stay in `PerformanceSelector` — they are selection criteria, not global rules
- Polling hierarchy (elite > watchlist > public) stays in `titan_market.py` — it is a fetch strategy, not selection logic; selector sets the tier, market layer uses the tier
- `recent_form_qualified()` stays as a signal-level filter, not a selector filter (it answers "should I copy this trade now?" not "is this wallet worth tracking?")
