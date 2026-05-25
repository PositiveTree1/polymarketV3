# Signal Builder Refactoring Plan

## Implementation Status

| Phase | Task | Status |
|-------|------|--------|
| 1 | Create `titan_signal_builder.py` — `SignalBuilderBase` ABC + `BuilderParams` base | ✅ Done |
| 1 | Add `signal_builders` config block to `titan_config.json` + `titan_config.py` | ✅ Done |
| 2 | Implement `ConsensusBasketParams` dataclass (all current CB params) | ✅ Done |
| 2 | Implement `ConsensusBasketBuilder.build()` — wraps `_build_consensus_basket_signals` | ✅ Done |
| 2 | Implement `RecentFormParams` + `RecentFormBuilder.build()` | ✅ Done |
| 2 | Implement `DriftDiscountParams` + `DriftDiscountBuilder.build()` | ✅ Done |
| 3 | Wire `titan_signals.py` — `build_signals()` delegates to registered builders | ✅ Done |
| 3 | Wire `titan_config.py` — load builder config blocks, instantiate active builders | ✅ Done |
| 4 | UI tab — Signal Builder panel (Tab 11) with per-builder param editing | ✅ Done |

---

## Goal

Cleanly separate the logic that generates signals from the rest of the system. Introduce a `SignalBuilderBase` ABC with concrete implementations, each with its own independently configurable parameters. Mirror the structure established for `WalletSelector` in `titan_selector.py`.

Clear pipeline:

```
Polymarket API
      ↓
WalletSelector  →  List of selected wallets   [done]
      ↓
SignalBuilderBase  →  List of Signals          [this plan]
      ↓
TradeMaker  →  List of Trades / Positions
      ↓
Accounting
```

---

## Current Architecture (Problem)

```
titan_signals.py
  build_signals()           — dispatcher, hard-codes which builders to call
  _build_recent_form_signals()    — strategy impl, reads C.strategy_recent_form dict inline
  _build_drift_discount_signals() — strategy impl, reads C.strategy_drift_discount dict inline
  _build_consensus_basket_signals() — strategy impl, reads C.strategy_consensus_basket dict inline
```

- Strategy logic is buried in module-level functions; no shared interface
- Parameters are bare dicts (`getattr(C, "strategy_X", {})`), not typed structures
- Adding a new strategy requires editing `build_signals()` dispatcher inline
- No abstraction boundary between "what a builder is" and "the engine that runs them"
- UI for per-strategy parameters does not exist

---

## Target Architecture

```
titan_signal_builder.py
  SignalBuilderBase (ABC)
    builder_id: str
    display_name: str
    params: BuilderParams
    build(trades, wallets, wallet_exits) -> tuple[list[Signal], list[str]]

  ConsensusBasketBuilder(SignalBuilderBase)   ← wraps _build_consensus_basket_signals
  RecentFormBuilder(SignalBuilderBase)        ← wraps _build_recent_form_signals
  DriftDiscountBuilder(SignalBuilderBase)     ← wraps _build_drift_discount_signals

titan_signals.py
  build_signals()           — iterates registered builders, delegates, dedupes (unchanged semantics)
```

Each builder:
- Owns a typed `@dataclass` of parameters
- Is instantiated from config via `build_signal_builder(builder_id, params_dict)`
- Is live-reloadable: `titan_config.reload()` rebuilds instances; next cycle picks up new params

---

## New Components

### 1. `titan_signal_builder.py` (new file)

#### Base classes

```python
@dataclass
class BuilderParams:
    """Base for all builder parameter blocks. Subclassed per builder."""
    enabled: bool = True

    @classmethod
    def from_dict(cls, d: dict) -> "BuilderParams":
        valid = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in valid})


class SignalBuilderBase(ABC):
    builder_id: str
    display_name: str

    def __init__(self, params: BuilderParams) -> None:
        self.params = params

    @abstractmethod
    def build(
        self,
        trades: list,
        wallets: dict,
        wallet_exits: dict,
    ) -> tuple[list, list[str]]:
        """Return (signals, rejects)."""

    @classmethod
    def registry_entry(cls) -> tuple[str, type]:
        return cls.builder_id, cls
```

#### First concrete builder: `ConsensusBasketBuilder`

Parameters extracted from current `strategy_consensus_basket` dict:

```python
@dataclass
class ConsensusBasketParams(BuilderParams):
    enabled: bool = True
    min_elite_confluence: int = 1
    max_signal_age_h: float = 0.5
    price_min: float = 0.20
    price_max: float = 0.72
    min_score: float = 50.0
    max_positions: int = 5
    max_bet_abs: float = 1.20
    stop_loss_pct: float | None = -0.35
    opposition_ratio_block: float = 0.60
    conviction_portfolio_pct: float = 0.005
```

`ConsensusBasketBuilder.build()` calls the existing `_build_consensus_basket_signals()` after injecting params.

#### `RecentFormBuilder`

```python
@dataclass
class RecentFormParams(BuilderParams):
    enabled: bool = True
    max_tph: float = 20.0
    min_pnl_30d: float = 0.0
    min_pnl_7d: float = -50.0
    max_signal_age_h: float = 0.75
    min_score: float = 42.0
    price_min: float = 0.18
    price_max: float = 0.78
    max_positions: int = 4
    stop_loss_pct: float | None = None
```

#### `DriftDiscountBuilder`

```python
@dataclass
class DriftDiscountParams(BuilderParams):
    enabled: bool = True
    min_discount_pct: float = 0.04
    max_discount_pct: float = 0.12
    max_signal_age_h: float = 6.0
    price_min: float = 0.20
    price_max: float = 0.72
    max_positions: int = 3
    require_still_holding_check: bool = True
    stop_loss_pct: float | None = None
```

---

### 2. Future builders (examples, not implemented now)

| Builder ID | Description | Key params |
|------------|-------------|------------|
| `momentum_spike` | Detects sudden volume burst on a market | `lookback_min`, `min_volume_ratio` |
| `contrarian` | Bets against consensus on high-confidence markets | `min_divergence_pct`, `min_market_volume` |
| `open_book` | Aggregates broader wallet consensus (currently `strategy_open_book`, disabled) | `min_consensus`, `wallets_per_cycle` |
| `hft_mirror` | Dedicated HFT spike builder separated from consensus logic | `min_spike_ratio`, `max_mirror_delay_s` |

---

## Config Structure

### Current (per-strategy dicts mixed into JSON)

```json
{
  "strategy_recent_form": { "enabled": true, "max_tph": 20, ... },
  "strategy_drift_discount": { "enabled": true, "min_discount_pct": 0.04, ... },
  "strategy_consensus_basket": { "enabled": true, "min_elite_confluence": 1, ... },
  "ACTIVE_STRATEGIES": ["recent_form", "drift_discount", "consensus_basket"]
}
```

### Target (builders block, parallel to wallet_selector)

```json
{
  "signal_builders": {
    "_group": "Signal Builders",
    "active_builders": ["consensus_basket", "recent_form", "drift_discount"],
    "builders": {
      "consensus_basket": {
        "enabled": true,
        "min_elite_confluence": 1,
        "max_signal_age_h": 0.5,
        "price_min": 0.20,
        "price_max": 0.72,
        "min_score": 50,
        "max_bet_abs": 1.20,
        "stop_loss_pct": -0.35
      },
      "recent_form": {
        "enabled": true,
        "max_tph": 20,
        "min_pnl_30d": 0,
        "min_pnl_7d": -50,
        "max_signal_age_h": 0.75,
        "min_score": 42,
        "price_min": 0.18,
        "price_max": 0.78
      },
      "drift_discount": {
        "enabled": true,
        "min_discount_pct": 0.04,
        "max_discount_pct": 0.12,
        "max_signal_age_h": 6.0,
        "price_min": 0.20,
        "price_max": 0.72,
        "require_still_holding_check": true
      }
    }
  }
}
```

Old `strategy_*` keys kept during migration, removed after.

---

## Wire-up Changes

### `titan_signals.py` — `build_signals()`

```python
# Before (hard-coded)
if "recent_form" in active:
    sigs, rejs = _build_recent_form_signals(trades, wallets, whale_exits)
    ...

# After (builder registry)
for builder in C.get_active_builders():
    sigs, rejs = builder.build(trades, wallets, whale_exits)
    all_signals.extend(sigs)
    all_rejects.extend(rejs)
```

The three `_build_*` functions stay in `titan_signals.py` as internal helpers; builders call them passing their typed params. No business logic moves — only the dispatch and param-passing changes.

### `titan_config.py`

```python
# New module-level state
signal_builders: dict = {}     # raw config block
_active_builders: list = []    # list[SignalBuilderBase]

def get_active_builders() -> list:
    return _active_builders

# In reload():
from titan_signal_builder import build_builders as _build_builders
_active_builders = _build_builders(flat.get("signal_builders", {}))
```

---

## UI Changes

Add a **Signal Builders** tab (Tab 12, after existing SELECTOR tab):

```
┌─ Signal Builders ──────────────────────────────────┐
│  Active builders: [✓] Consensus Basket              │
│                   [✓] Recent Form                   │
│                   [✓] Drift Discount                │
│                                                      │
│  ── Selected builder parameters ─────────────────── │
│  Builder: [Consensus Basket ▾]                       │
│                                                      │
│  Min elite confluence:  [1   ]                       │
│  Max signal age (h):    [0.5 ]                       │
│  Price min:             [0.20]                       │
│  Price max:             [0.72]                       │
│  Min score:             [50  ]                       │
│  Max bet ($):           [1.20]                       │
│  Stop loss %:           [-35 ]                       │
│                                                      │
│           [Apply]  changes take effect next cycle    │
└──────────────────────────────────────────────────────┘
```

Selecting a different builder in the dropdown loads its params. Applying writes to `signal_builders.builders.<id>` section in config JSON. No restart required.

---

## Files Affected

| File | Change |
|------|--------|
| `titan_signal_builder.py` | **New** — `SignalBuilderBase`, `BuilderParams`, 3 concrete builders, registry, `build_builders()` factory |
| `titan_signals.py` | `build_signals()` delegates to `C.get_active_builders()` instead of hard-coded dispatch |
| `titan_config.py` | Add `signal_builders` block loading, `get_active_builders()`, rebuild on `reload()` |
| `titan_config.json` | Add `signal_builders` block; keep `strategy_*` keys for backward compat initially |
| `titan_ui.py` | Add Tab 12 — Signal Builders panel |
| `signal_builder_plan.md` | This file — update status as phases complete |

---

## Invariants to Preserve

- All three `_build_*` functions stay in `titan_signals.py` as private helpers (builders call them)
- Deduplication, hedge-bot detection, price-zone enforcement in `build_signals()` dispatcher stay unchanged
- `score_signal()`, `kelly_bet()`, `check_wallet_exist()` are not touched
- `Signal` dataclass is not touched
- `ACTIVE_STRATEGIES` config key is replaced by `signal_builders.active_builders`; backward compat: if `signal_builders` block absent, fall back to `ACTIVE_STRATEGIES`

---

## Implementation Steps

### Phase 1 — Infrastructure
1. Create `titan_signal_builder.py` with `BuilderParams` base, `SignalBuilderBase` ABC, registry dict, `build_builders()` factory
2. Add `signal_builders` block to `titan_config.json` (copy current values from `strategy_*` blocks)
3. Add `signal_builders` loading + `get_active_builders()` to `titan_config.py`

### Phase 2 — Concrete builders
4. Implement `ConsensusBasketParams` + `ConsensusBasketBuilder` (calls `_build_consensus_basket_signals` with params injected)
5. Implement `RecentFormParams` + `RecentFormBuilder`
6. Implement `DriftDiscountParams` + `DriftDiscountBuilder`

### Phase 3 — Wire dispatcher
7. In `build_signals()`: replace hard-coded `if "X" in active` with `for builder in C.get_active_builders(): builder.build(...)`
8. Test that all three strategies still fire correctly with same output

### Phase 4 — UI
9. Add Signal Builders tab to `titan_ui.py` (mirroring SELECTOR tab structure)
10. Load/save via `signal_builders` config section
11. Dropdown switches which builder's params are shown
12. Apply button hot-reloads config, rebuilds builder instances

### Phase 5 — Cleanup
13. Remove `ACTIVE_STRATEGIES` + `strategy_*` keys from `titan_config.json` (or keep with `_deprecated` prefix)
14. Update `TITAN_CONTEXT.md` with final architecture
