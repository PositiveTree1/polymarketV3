from __future__ import annotations

import json
import sys

import requests


DEFAULT_ASSET = "44078112436319577968481683057376078936091504313447027987212912701572942457716"
GAMMA_API = "https://gamma-api.polymarket.com"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
    "Origin": "https://polymarket.com",
    "Referer": "https://polymarket.com/",
}


def main() -> None:
    asset = sys.argv[1].strip() if len(sys.argv) > 1 else DEFAULT_ASSET
    response = requests.get(
        f"{GAMMA_API}/markets",
        params={"clob_token_ids": asset},
        headers=HEADERS,
        timeout=20,
    )

    print(f"URL: {response.url}")
    print(f"Status: {response.status_code}")

    try:
        payload = response.json()
    except ValueError:
        print(response.text)
        return

    print(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
