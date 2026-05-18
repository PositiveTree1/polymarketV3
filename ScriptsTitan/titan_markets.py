from __future__ import annotations

import time
from typing import TYPE_CHECKING

import titan_db as DB
from titan_config import DATA_API, MARKET_TTL

if TYPE_CHECKING:
    from titan_market import Market


class MarketCache(dict[str, "Market"]):
    def __init__(self) -> None:
        super().__init__()
        self._db_loaded: bool = False

    def load_all_from_db(self, force: bool = False) -> None:
        if self._db_loaded and not force:
            return
        super().clear()
        for cid, market in DB.load_markets().items():
            super().__setitem__(cid, market)
        self._db_loaded = True

    def _position_hints(self, cid: str) -> tuple[str | None, str, str, str]:
        import titan_state as S

        for pos in S.env().open_positions.values():
            if pos.cid != cid:
                continue
            return pos.title, pos.asset, pos.slug, pos.event_slug
        return None, "", "", ""

    def _apply_identity_hints(
        self,
        market: "Market",
        *,
        trade_title: str | None,
        slug: str,
        event_slug: str,
    ) -> bool:
        changed = False
        if trade_title and "?" in trade_title and len(trade_title) > len(market.title):
            market.title = trade_title
            changed = True
        if slug and not market.slug:
            market.slug = slug
            changed = True
        if event_slug and not market.event_slug:
            market.event_slug = event_slug
            changed = True
        return changed

    def _find_cached_by_asset(self, asset: str) -> tuple[str, "Market"] | None:
        normalized_asset = str(asset or "").strip()
        if not normalized_asset:
            return None
        now_t = time.time()
        for cid, market in self.items():
            if normalized_asset not in market.asset_to_price:
                continue
            if (now_t - market.ts) >= MARKET_TTL:
                continue
            return cid, market
        return None

    def _extract_trade_slug_and_asset(self, cid: str) -> tuple[str, str]:
        import titan_state as S

        trades_payload = S.safe_get(
            f"{DATA_API}/trades",
            {"conditionId": cid, "limit": 3},
            quiet=True,
        )
        if not isinstance(trades_payload, list) or not trades_payload:
            return "", ""
        first = trades_payload[0]
        if not isinstance(first, dict):
            return "", ""
        slug = str(first.get("slug") or "")
        asset = str(first.get("asset") or "")
        return slug, asset

    def peek(self, cid: str) -> "Market | None":
        self.load_all_from_db()
        return super().get(cid)

    def store(self, cid: str, market: "Market", *, persist: bool) -> None:
        self.load_all_from_db()
        super().__setitem__(cid, market)
        if persist:
            DB.upsert_market(cid, market)

    def persist(self, cid: str) -> None:
        self.load_all_from_db()
        market = super().get(cid)
        if market is None:
            return
        DB.upsert_market(cid, market)

    def __setitem__(self, cid: str, market: "Market") -> None:
        self.store(cid, market, persist=True)

    def get(self, cid: str, default: "Market | None" = None) -> "Market | None":
        self.load_all_from_db()
        cached = super().get(cid)
        if cached is not None:
            return cached
        if not cid:
            return default
        trade_title, asset, slug, event_slug = self._position_hints(cid)
        loaded, _ = self.resolve(
            cid,
            trade_title=trade_title,
            asset=asset,
            slug=slug,
            event_slug=event_slug,
            from_verified=True,
            allow_untradeable=True,
            persist=True,
        )
        if loaded is not None:
            return loaded
        return default

    def resolve(
        self,
        cid: str,
        *,
        trade_title: str | None = None,
        asset: str = "",
        slug: str = "",
        event_slug: str = "",
        from_verified: bool = False,
        allow_untradeable: bool = False,
        persist: bool = False,
    ) -> tuple["Market | None", str | None]:
        self.load_all_from_db()
        now_t = time.time()
        cached = super().get(cid)
        if cached is not None and (now_t - cached.ts) < MARKET_TTL:
            changed = self._apply_identity_hints(
                cached,
                trade_title=trade_title,
                slug=slug,
                event_slug=event_slug,
            )
            if changed and persist:
                DB.upsert_market(cid, cached)
            elif changed:
                super().__setitem__(cid, cached)
            return cached, None

        import titan_market as market_api

        if from_verified:
            market_api.mark_cid_verified(cid)

        loaded, err = market_api._fetch_market_remote(
            cid,
            trade_title=trade_title,
            asset=asset,
            slug=slug,
            from_verified=from_verified,
            allow_untradeable=allow_untradeable,
            has_cached=cached is not None,
        )
        if loaded is not None:
            self._apply_identity_hints(
                loaded,
                trade_title=trade_title,
                slug=slug,
                event_slug=event_slug,
            )
            self.store(cid, loaded, persist=persist)
            return loaded, None
        if cached is not None:
            changed = self._apply_identity_hints(
                cached,
                trade_title=trade_title,
                slug=slug,
                event_slug=event_slug,
            )
            if changed and persist:
                DB.upsert_market(cid, cached)
            elif changed:
                super().__setitem__(cid, cached)
            return cached, err
        return None, err

    def resolve_asset(
        self,
        asset: str,
        *,
        slug: str = "",
        allow_untradeable: bool = False,
        persist: bool = False,
    ) -> tuple["Market | None", str | None]:
        self.load_all_from_db()
        normalized_asset = str(asset or "").strip()
        if not normalized_asset:
            return None, "Asset is required"

        import titan_market as market_api

        cached_match = self._find_cached_by_asset(normalized_asset)
        if cached_match is not None:
            _, cached_market = cached_match
            return cached_market, None

        raw_market = market_api._fetch_market_raw("", asset=normalized_asset, slug=slug)
        if raw_market is None:
            return None, "API returned nothing"

        cid = str(raw_market.get("conditionId") or raw_market.get("condition_id") or "")
        if not cid:
            return None, "Market missing conditionId"

        loaded, err = market_api._market_from_gamma_payload(
            raw_market,
            cid=cid,
            trade_title=None,
            slug=str(raw_market.get("slug") or slug or ""),
            allow_untradeable=allow_untradeable,
        )
        if loaded is None:
            return None, err

        market_api.mark_cid_verified(cid)
        self.store(cid, loaded, persist=persist)
        return loaded, None

    def get_market_by_asset(self, asset: str) -> "Market | None":
        market, _ = self.resolve_asset(
            asset,
            allow_untradeable=True,
            persist=True,
        )
        return market

    def resolve_cid(
        self,
        cid: str,
        *,
        allow_untradeable: bool = False,
        persist: bool = False,
    ) -> tuple["Market | None", str | None]:
        self.load_all_from_db()
        normalized_cid = str(cid or "").strip()
        if not normalized_cid:
            return None, "ConditionId is required"

        now_t = time.time()
        cached = super().get(normalized_cid)
        if cached is not None:
            if (now_t - cached.ts) < MARKET_TTL:
                return cached, None
            if cached.event_slug or cached.slug:
                return cached, None

        import titan_market as market_api

        slug, asset = self._extract_trade_slug_and_asset(normalized_cid)
        if not slug:
            return None, "No trade slug found for this conditionId"

        raw_market = market_api._fetch_market_raw("", asset=asset, slug=slug)
        if raw_market is None:
            return None, "API returned nothing"

        resolved_cid = str(raw_market.get("conditionId") or raw_market.get("condition_id") or normalized_cid)
        loaded, err = market_api._market_from_gamma_payload(
            raw_market,
            cid=resolved_cid,
            trade_title=None,
            slug=slug,
            allow_untradeable=allow_untradeable,
        )
        if loaded is None:
            return None, err

        market_api.mark_cid_verified(resolved_cid)
        self.store(resolved_cid, loaded, persist=persist)
        return loaded, None

    def get_market_by_cid(self, cid: str) -> "Market | None":
        market, _ = self.resolve_cid(
            cid,
            allow_untradeable=True,
            persist=True,
        )
        return market

    def update_live_price(self, cid: str, asset: str, price: float, *, ts: float | None = None) -> None:
        self.load_all_from_db()
        market = super().get(cid)
        if market is None or not asset:
            return
        market.asset_to_price[asset] = price
        idx = market.asset_to_index.get(asset)
        if idx is not None:
            market.index_to_price[idx] = price
            if idx == 0:
                market.yes_price = price
            elif idx == 1:
                market.no_price = price
        market.ts = float(ts if ts is not None else time.time())
        DB.upsert_market(cid, market)


market_cache = MarketCache()
