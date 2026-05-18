from __future__ import annotations

import sqlite3
import sys
import json
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SCRIPTS_DIR = ROOT / "ScriptsTitan"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import titan_state as S
from titan_config import GAMMA_API
from titan_persistence import load_state


@dataclass
class AssetProbeTarget:
    cid: str
    title: str
    asset: str
    slug: str
    trade_event_slug: str


def _pick_targets(limit: int = 5, title_filter: str = "") -> list[AssetProbeTarget]:
    with sqlite3.connect("titan_state.db") as cx:
        trade_rows = cx.execute(
            """
            SELECT cid, title, asset, slug, event_slug
            FROM trade_history
            WHERE asset IS NOT NULL
              AND asset != ''
            ORDER BY ts DESC
            """
        ).fetchall()
        market_rows = cx.execute(
            """
            SELECT cid, data
            FROM markets
            ORDER BY updated_at DESC
            """
        ).fetchall()

    trade_by_cid: dict[str, AssetProbeTarget] = {}
    for row in trade_rows:
        target = AssetProbeTarget(
            cid=str(row[0] or ""),
            title=str(row[1] or ""),
            asset=str(row[2] or ""),
            slug=str(row[3] or ""),
            trade_event_slug=str(row[4] or ""),
        )
        if target.cid and target.cid not in trade_by_cid:
            trade_by_cid[target.cid] = target

    targets: list[AssetProbeTarget] = []
    seen_cids: set[str] = set()
    title_filter_lower = title_filter.lower().strip()

    for cid, raw_data in market_rows:
        cid_str = str(cid or "")
        if not cid_str or cid_str in seen_cids:
            continue
        payload = json.loads(str(raw_data))
        if not isinstance(payload, dict):
            continue
        market_title = str(payload.get("title") or "")
        trade_target = trade_by_cid.get(cid_str)
        title = market_title or (trade_target.title if trade_target is not None else "")
        if title_filter_lower and title_filter_lower not in title.lower():
            continue
        asset = ""
        asset_map = payload.get("asset_to_price")
        if isinstance(asset_map, dict) and asset_map:
            first_key = next(iter(asset_map.keys()))
            asset = str(first_key or "")
        if not asset and trade_target is not None:
            asset = trade_target.asset
        if not asset:
            continue
        targets.append(
            AssetProbeTarget(
                cid=cid_str,
                title=title,
                asset=asset,
                slug=str(payload.get("slug") or "") or (trade_target.slug if trade_target is not None else ""),
                trade_event_slug=(trade_target.trade_event_slug if trade_target is not None else ""),
            )
        )
        seen_cids.add(cid_str)
        if len(targets) >= limit:
            break

    if title_filter_lower:
        return targets

    if targets:
        return targets[:limit]

    for target in trade_by_cid.values():
        if title_filter_lower and title_filter_lower not in target.title.lower():
            continue
        targets.append(target)
        if len(targets) >= limit:
            break
    return targets


def _event_slug_from_market_payload(market: dict[str, object]) -> str:
    direct_slug = str(market.get("eventSlug") or market.get("event_slug") or "")
    if direct_slug:
        return direct_slug

    event_obj = market.get("event")
    if isinstance(event_obj, dict):
        nested_slug = str(event_obj.get("slug") or "")
        if nested_slug:
            return nested_slug

    events_obj = market.get("events")
    if isinstance(events_obj, list):
        for item in events_obj:
            if isinstance(item, dict):
                nested_slug = str(item.get("slug") or "")
                if nested_slug:
                    return nested_slug
    return ""


def _fetch_market_by_asset(asset: str) -> dict[str, object] | None:
    payload = S.safe_get(
        f"{GAMMA_API}/markets",
        {
            "clob_token_ids": asset,
            "limit": 1,
        },
        quiet=True,
    )
    if isinstance(payload, list) and payload:
        first = payload[0]
        if isinstance(first, dict):
            return first
    if isinstance(payload, dict) and payload:
        return payload
    return None


def main() -> None:
    load_state()
    cli_assets = [arg.strip() for arg in sys.argv[1:] if arg.strip()]
    cli_title_filter = ""
    if cli_assets and cli_assets[0].startswith("--title="):
        cli_title_filter = cli_assets[0][8:].strip()
        cli_assets = cli_assets[1:]
    if cli_assets:
        targets = [
            AssetProbeTarget(cid="", title="", asset=asset, slug="", trade_event_slug="")
            for asset in cli_assets
        ]
    else:
        targets = _pick_targets(title_filter=cli_title_filter)

    if not targets:
        print("No asset targets found.")
        return

    for index, target in enumerate(targets, start=1):
        print(f"TARGET {index}")
        print("  cid:", target.cid)
        print("  title:", target.title)
        print("  asset:", target.asset)
        print("  trade.slug:", target.slug)
        print("  trade.event_slug:", target.trade_event_slug)

        market = _fetch_market_by_asset(target.asset)
        if market is None:
            print("  gamma.market: none")
            print()
            continue

        market_slug = str(market.get("slug") or "")
        event_slug = _event_slug_from_market_payload(market)
        print("  gamma.market.slug:", market_slug)
        print("  gamma.event.slug:", event_slug)
        print(
            "  polymarket_url:",
            f"https://polymarket.com/event/{event_slug}" if event_slug else "",
        )
        print()


if __name__ == "__main__":
    main()
