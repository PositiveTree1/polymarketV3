from __future__ import annotations

import json
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SCRIPTS_DIR = ROOT / "ScriptsTitan"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import titan_config as C
import titan_state as S
from titan_market import _to_decimal_token


@dataclass
class ProbeTarget:
    cid: str
    asset: str
    slug: str
    stored_event_slug: str
    title: str


def _pick_market(payload: object) -> dict[str, object] | None:
    if isinstance(payload, list) and payload:
        first = payload[0]
        return first if isinstance(first, dict) else None
    if isinstance(payload, dict) and payload:
        return payload
    return None


def _read_target_from_db() -> ProbeTarget | None:
    cx = sqlite3.connect("titan_state.db")
    try:
        market_rows = cx.execute("SELECT cid, data FROM markets ORDER BY updated_at DESC").fetchall()
        trade_rows = cx.execute(
            """
            SELECT cid, asset, slug, event_slug, title
            FROM trade_history
            WHERE cid IS NOT NULL AND cid != ''
            ORDER BY ts DESC
            """
        ).fetchall()
    finally:
        cx.close()

    trade_by_cid: dict[str, tuple[str, str, str, str]] = {}
    for cid, asset, slug, event_slug, title in trade_rows:
        cid_str = str(cid or "")
        if cid_str and cid_str not in trade_by_cid:
            trade_by_cid[cid_str] = (
                str(asset or ""),
                str(slug or ""),
                str(event_slug or ""),
                str(title or ""),
            )

    for cid, raw_data in market_rows:
        cid_str = str(cid or "")
        if not cid_str:
            continue
        payload = json.loads(str(raw_data))
        if not isinstance(payload, dict):
            continue
        stored_event_slug = str(payload.get("event_slug") or "")
        if stored_event_slug:
            continue
        asset, trade_slug, trade_event_slug, trade_title = trade_by_cid.get(cid_str, ("", "", "", ""))
        market_slug = str(payload.get("slug") or "")
        title = str(payload.get("title") or "") or trade_title
        slug = market_slug or trade_slug
        if not asset and not slug:
            continue
        return ProbeTarget(
            cid=cid_str,
            asset=asset,
            slug=slug,
            stored_event_slug=stored_event_slug or trade_event_slug,
            title=title,
        )
    return None


def _print_market_result(label: str, market: dict[str, object] | None) -> None:
    print(label)
    if market is None:
        print("  no market returned")
        return
    condition_id = str(market.get("conditionId") or market.get("condition_id") or "")
    slug = str(market.get("slug") or "")
    event_slug = str(market.get("eventSlug") or market.get("event_slug") or "")
    event_obj = market.get("event")
    print("  conditionId:", condition_id)
    print("  slug:", slug)
    print("  eventSlug:", event_slug)
    print("  has_event_obj:", isinstance(event_obj, dict))
    if isinstance(event_obj, dict):
        print("  event.slug:", str(event_obj.get("slug") or ""))
        print("  event.title:", str(event_obj.get("title") or ""))


def main() -> None:
    target = _read_target_from_db()
    if target is None:
        print("No suitable target found in markets/trade_history.")
        return

    print("TARGET")
    print("  cid:", target.cid)
    print("  asset:", target.asset)
    print("  slug:", target.slug)
    print("  stored_event_slug:", target.stored_event_slug)
    print("  title:", target.title)
    print()

    market_by_asset: dict[str, object] | None = None
    if target.asset:
        payload = S.safe_get(
            f"{C.GAMMA_API}/markets",
            {"clob_token_ids": json.dumps([_to_decimal_token(target.asset)]), "limit": 1},
            quiet=True,
        )
        market_by_asset = _pick_market(payload)
    _print_market_result("GAMMA by asset", market_by_asset)
    print()

    market_by_slug: dict[str, object] | None = None
    if target.slug:
        payload = S.safe_get(
            f"{C.GAMMA_API}/markets",
            {"slug": target.slug, "limit": 1},
            quiet=True,
        )
        market_by_slug = _pick_market(payload)
    _print_market_result("GAMMA by slug", market_by_slug)
    print()

    print("DATA API by cid")
    trades = S.safe_get(
        f"{C.DATA_API}/trades",
        {"conditionId": target.cid, "limit": 3},
        quiet=True,
    )
    if not isinstance(trades, list) or not trades:
        print("  no trades returned")
        return
    first = trades[0]
    print("  trade.slug:", str(first.get("slug") or ""))
    print("  trade.eventSlug:", str(first.get("eventSlug") or first.get("event_slug") or ""))
    print("  trade.asset:", str(first.get("asset") or ""))
    print("  trade.outcome:", str(first.get("outcome") or ""))


if __name__ == "__main__":
    main()
