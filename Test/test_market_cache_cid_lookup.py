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


class MarketCacheCidLookupTest(unittest.TestCase):
    def test_resolve_cid_returns_populated_market(self) -> None:
        cid = os.environ.get("POLYMARKET_CID", "").strip()
        if not cid:
            cid = "0x8daeebd9b8136dcd7718af674ed6a10b6ff0288fc77df2cc9b3f8c04ffa691ba"

        cache = MarketCache()
        market = cache.get_market_by_cid(cid)
        self.assertIsNotNone(market)

        resolved_market = market
        assert resolved_market is not None
        self.assertIsInstance(resolved_market, Market)
        self.assertTrue(resolved_market.title)
        self.assertTrue(resolved_market.slug or resolved_market.event_slug)
        self.assertGreaterEqual(len(resolved_market.outcome_labels), 2)
        self.assertGreaterEqual(len(resolved_market.index_to_price), 2)


if __name__ == "__main__":
    unittest.main()
