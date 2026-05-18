from __future__ import annotations

import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SCRIPTS_DIR = ROOT / "ScriptsTitan"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import titan_state as S
from titan_markets import market_cache
from titan_persistence import load_state


@dataclass
class TradeIdentity:
    cid: str
    title: str
    asset: str
    slug: str
    event_slug: str


def _pick_trade_identity() -> TradeIdentity | None:
    query = """
        SELECT cid, title, asset, slug, event_slug
        FROM trade_history
        WHERE cid IS NOT NULL
          AND cid != ''
          AND event_slug IS NOT NULL
          AND event_slug != ''
        ORDER BY ts DESC
        LIMIT 1
    """
    with sqlite3.connect("titan_state.db") as cx:
        row = cx.execute(query).fetchone()
    if row is None:
        return None
    return TradeIdentity(
        cid=str(row[0] or ""),
        title=str(row[1] or ""),
        asset=str(row[2] or ""),
        slug=str(row[3] or ""),
        event_slug=str(row[4] or ""),
    )


def main() -> None:
    load_state()
    identity = _pick_trade_identity()
    if identity is None:
        print("No trade_history row with event_slug found.")
        return

    cached_before = market_cache.peek(identity.cid)
    print("TARGET")
    print("  cid:", identity.cid)
    print("  title:", identity.title)
    print("  asset:", identity.asset)
    print("  slug:", identity.slug)
    print("  trade.event_slug:", identity.event_slug)
    print("  cached_before.event_slug:", cached_before.event_slug if cached_before is not None else "")

    market, err = market_cache.resolve(
        identity.cid,
        trade_title=identity.title,
        asset=identity.asset,
        slug=identity.slug,
        event_slug=identity.event_slug,
        from_verified=True,
        allow_untradeable=True,
        persist=False,
    )
    if market is None:
        print("RESOLVE FAILED")
        print("  error:", err or "")
        return

    print("RESOLVED")
    print("  market.slug:", market.slug)
    print("  market.event_slug:", market.event_slug)
    print("  polymarket_url:", market.polymarket_url())


if __name__ == "__main__":
    main()
