from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "ScriptsTitan"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from titan_market import Market
from titan_markets import MarketCache


class MarketCacheAssetLookupTest(unittest.TestCase):
    def test_resolve_asset_returns_populated_market(self) -> None:
        asset = os.environ.get("POLYMARKET_ASSET", "").strip()
        if not asset:
            asset = "44078112436319577968481683057376078936091504313447027987212912701572942457716"

        cache = MarketCache()
        market = cache.get_market_by_asset(asset)
        self.assertIsNotNone(market)

        resolved_market = market
        assert resolved_market is not None
        self.assertIsInstance(resolved_market, Market)
        self.assertTrue(resolved_market.title)
        self.assertTrue(resolved_market.slug or resolved_market.event_slug)
        self.assertIn(asset, resolved_market.asset_to_price)
        self.assertIn(asset, resolved_market.asset_to_index)
        self.assertGreaterEqual(len(resolved_market.outcome_labels), 2)
        self.assertGreaterEqual(len(resolved_market.index_to_price), 2)


if __name__ == "__main__":
    unittest.main()
