from __future__ import annotations

import json
import sys
from dataclasses import dataclass

import requests


DEFAULT_CID = "0x8daeebd9b8136dcd7718af674ed6a10b6ff0288fc77df2cc9b3f8c04ffa691ba"
DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
    "Origin": "https://polymarket.com",
    "Referer": "https://polymarket.com/",
}


JsonObject = dict[str, object]


@dataclass
class HttpResult:
    url: str
    status_code: int
    body: str
    payload: object | None


def _get_json(url: str, *, params: JsonObject | None = None) -> HttpResult:
    response = requests.get(url, params=params, headers=HEADERS, timeout=20)
    try:
        payload: object | None = response.json()
    except ValueError:
        payload = None
    return HttpResult(
        url=str(response.url),
        status_code=response.status_code,
        body=response.text,
        payload=payload,
    )


def _extract_slug_from_trades(payload: object) -> str:
    if not isinstance(payload, list) or not payload:
        return ""
    first = payload[0]
    if not isinstance(first, dict):
        return ""
    slug_value = first.get("slug")
    if isinstance(slug_value, str):
        return slug_value
    return ""


def _extract_first_market(payload: object) -> JsonObject | None:
    if isinstance(payload, list) and payload:
        first = payload[0]
        if isinstance(first, dict):
            return first
        return None
    if isinstance(payload, dict) and payload:
        return payload
    return None


def _print_result(label: str, result: HttpResult) -> None:
    print(label)
    print(f"URL: {result.url}")
    print(f"Status: {result.status_code}")
    if result.payload is None:
        print("Body:")
        print(result.body[:2000])
    else:
        print("JSON:")
        print(json.dumps(result.payload, indent=2, sort_keys=True)[:12000])
    print()


def main() -> None:
    cid = sys.argv[1].strip() if len(sys.argv) > 1 else DEFAULT_CID

    direct_result = _get_json(
        f"{GAMMA_API}/markets",
        params={"conditionId": cid, "limit": 1},
    )
    _print_result("Direct Gamma /markets by conditionId", direct_result)

    trades_result = _get_json(
        f"{DATA_API}/trades",
        params={"conditionId": cid, "limit": 3},
    )
    _print_result("Data API /trades bootstrap by conditionId", trades_result)

    slug = _extract_slug_from_trades(trades_result.payload)
    if not slug:
        print("No trade slug found for this conditionId.")
        return

    slug_result = _get_json(
        f"{GAMMA_API}/markets",
        params={"slug": slug, "limit": 1},
    )
    _print_result("Gamma /markets by slug", slug_result)

    market = _extract_first_market(slug_result.payload)
    if market is None:
        print("No market blob returned from Gamma slug lookup.")
        return

    print("Resolved market blob:")
    print(json.dumps(market, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
