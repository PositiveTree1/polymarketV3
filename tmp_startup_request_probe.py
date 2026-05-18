from __future__ import annotations

import copy
import json
import os
import sys
from dataclasses import dataclass

try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:
    pass


ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.join(ROOT_DIR, "ScriptsTitan")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import titan_config as C
import titan_state as S
from titan_market import fetch_trades
from titan_persistence import load_state


@dataclass
class RecordedCall:
    url: str
    params: dict[str, object]


def _reset_env_for_true_scratch() -> None:
    env = S.env()
    env.wallet_cache.clear()
    env.watchlist = set(w.lower() for w in C.SEED_WATCHLIST)
    env.open_positions = {}
    env.active_market_cids = set()
    env.cooldown_cids = {}
    env.position_whale_map = {}
    env.signal_first_seen_by_asset = {}
    env.equity_history = []
    env.LAST_SIGNALS = []
    env.LAST_REJECTS = []
    env.cycle_count = 0
    S.market_cache.clear()
    S.market_cache._db_loaded = False


def _capture_fetch_trades_calls(*, hydrate_from_disk: bool) -> list[RecordedCall]:
    recorded: list[RecordedCall] = []

    original_safe_get = S.safe_get
    original_sleep = __import__("time").sleep

    def fake_safe_get(
        url: str,
        params: dict | None = None,
        retries: int = 3,
        timeout: int = 12,
        quiet: bool = False,
    ) -> list | dict | None:
        recorded.append(RecordedCall(url=url, params=dict(params or {})))
        return []

    try:
        if hydrate_from_disk:
            load_state()
        else:
            _reset_env_for_true_scratch()

        import time as _time

        S.safe_get = fake_safe_get
        _time.sleep = lambda _seconds: None
        fetch_trades()
    finally:
        import time as _time

        S.safe_get = original_safe_get
        _time.sleep = original_sleep

    return recorded


def _scenario_name(hydrate_from_disk: bool) -> str:
    return "persisted startup state" if hydrate_from_disk else "true scratch state"


def _print_calls(title: str, calls: list[RecordedCall], limit: int = 12) -> None:
    print(title)
    print(f"  total_calls_captured={len(calls)}")
    if not calls:
        print("  no Polymarket calls captured")
        return
    for idx, call in enumerate(calls[:limit], start=1):
        print(f"  {idx}. {call.url}")
        print(f"     params={json.dumps(call.params, sort_keys=True)}")
    first = calls[0]
    print()
    print("  FIRST CALL")
    print(f"    url={first.url}")
    print(f"    params={json.dumps(first.params, sort_keys=True)}")


def main() -> None:
    persisted_calls = _capture_fetch_trades_calls(hydrate_from_disk=True)
    print(f"SCENARIO: {_scenario_name(True)}")
    _print_calls("Captured Polymarket requests", persisted_calls)
    print()
    print("-" * 80)
    print()

    scratch_calls = _capture_fetch_trades_calls(hydrate_from_disk=False)
    print(f"SCENARIO: {_scenario_name(False)}")
    _print_calls("Captured Polymarket requests", scratch_calls)


if __name__ == "__main__":
    main()
