from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

import requests


ROOT_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT_DIR / "ScriptsTitan"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import titan_config as C


@dataclass(frozen=True)
class TradeRecord:
    condition_id: str
    asset: str
    side: str
    size: float
    price: float
    cash: float
    timestamp: float
    outcome: str
    title: str
    proxy_wallet: str
    slug: str
    event_slug: str

    def unique_key(self) -> tuple[str, str, str, float, float, float]:
        return (
            self.condition_id,
            self.asset,
            self.side,
            self.timestamp,
            self.size,
            self.price,
        )


@dataclass(frozen=True)
class PageRequest:
    wallet: str
    endpoint: str
    limit: int
    offset: int
    side: str | None


@dataclass(frozen=True)
class PageResult:
    page_index: int
    http_status: int
    trades: tuple[TradeRecord, ...]
    elapsed_ms: float
    error_text: str = ""

    @property
    def count(self) -> int:
        return len(self.trades)

    @property
    def min_timestamp(self) -> float | None:
        if not self.trades:
            return None
        return min(trade.timestamp for trade in self.trades)

    @property
    def max_timestamp(self) -> float | None:
        if not self.trades:
            return None
        return max(trade.timestamp for trade in self.trades)

    @property
    def first_key(self) -> tuple[str, str, str, float, float, float] | None:
        if not self.trades:
            return None
        return self.trades[0].unique_key()

    @property
    def last_key(self) -> tuple[str, str, str, float, float, float] | None:
        if not self.trades:
            return None
        return self.trades[-1].unique_key()


@dataclass(frozen=True)
class ValidationSummary:
    endpoint: str
    pages_fetched: int
    raw_trades: int
    unique_trades: int
    duplicate_trades: int
    repeated_page_detected: bool
    requested_limit: int
    observed_max_page_size: int
    first_error_status: int | None
    first_error_text: str
    oldest_timestamp: float | None
    newest_timestamp: float | None


@dataclass(frozen=True)
class FilterProbeResult:
    name: str
    status_code: int
    count: int
    min_timestamp: float | None
    max_timestamp: float | None
    changed_window: bool
    error_text: str


@dataclass(frozen=True)
class EndpointConfig:
    endpoint: str
    limit: int
    extra_params: dict[str, str]


def _required_str(payload: Mapping[str, object], key: str) -> str:
    raw_value: object = payload.get(key, "")
    return str(raw_value or "")


def _float_value(payload: Mapping[str, object], primary_key: str, fallback_key: str = "") -> float:
    raw_value: object = payload.get(primary_key)
    if raw_value in (None, "") and fallback_key:
        raw_value = payload.get(fallback_key)
    try:
        return float(raw_value or 0.0)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid float field {primary_key!r}: {raw_value!r}") from exc


def parse_trade(payload: Mapping[str, object]) -> TradeRecord:
    size: float = _float_value(payload, "size")
    price: float = _float_value(payload, "price")
    cash: float = _float_value(payload, "usdcSize", "amount")
    if cash <= 0.0:
        cash = size * price
    return TradeRecord(
        condition_id=_required_str(payload, "conditionId"),
        asset=_required_str(payload, "asset"),
        side=_required_str(payload, "side").upper(),
        size=size,
        price=price,
        cash=cash,
        timestamp=_float_value(payload, "timestamp"),
        outcome=_required_str(payload, "outcome"),
        title=_required_str(payload, "title"),
        proxy_wallet=_required_str(payload, "proxyWallet").lower(),
        slug=_required_str(payload, "slug"),
        event_slug=_required_str(payload, "eventSlug"),
    )


def build_session() -> requests.Session:
    session: requests.Session = requests.Session()
    session.headers.update(C.HEADERS)
    return session


def _build_endpoint_config(endpoint: str, limit: int, side: str | None) -> EndpointConfig:
    if endpoint == "trades":
        extra_params: dict[str, str] = {}
        if side is not None:
            extra_params["side"] = side
        return EndpointConfig(endpoint=endpoint, limit=limit, extra_params=extra_params)
    if endpoint == "activity":
        extra_params = {"type": "TRADE"}
        if side is not None:
            extra_params["side"] = side
        return EndpointConfig(endpoint=endpoint, limit=limit, extra_params=extra_params)
    raise ValueError(f"Unsupported endpoint: {endpoint}")


def fetch_trade_page(session: requests.Session, request: PageRequest, page_index: int, timeout_seconds: int) -> PageResult:
    params: dict[str, str | int] = {
        "user": request.wallet,
        "limit": request.limit,
        "offset": request.offset,
    }
    endpoint_config: EndpointConfig = _build_endpoint_config(request.endpoint, request.limit, request.side)
    params.update(endpoint_config.extra_params)

    start_time: float = time.perf_counter()
    response: requests.Response = session.get(f"{C.DATA_API}/{request.endpoint}", params=params, timeout=timeout_seconds)
    elapsed_ms: float = (time.perf_counter() - start_time) * 1000.0
    if response.status_code >= 400:
        return PageResult(
            page_index=page_index,
            http_status=response.status_code,
            trades=tuple(),
            elapsed_ms=elapsed_ms,
            error_text=response.text[:500],
        )

    payload: object = response.json()
    if not isinstance(payload, list):
        raise TypeError(f"Expected list payload from /trades, got {type(payload).__name__}")

    trades: list[TradeRecord] = []
    for item in payload:
        if not isinstance(item, Mapping):
            raise TypeError(f"Expected trade item mapping, got {type(item).__name__}")
        trades.append(parse_trade(item))

    return PageResult(
        page_index=page_index,
        http_status=response.status_code,
        trades=tuple(trades),
        elapsed_ms=elapsed_ms,
    )


def pick_default_wallet() -> str:
    if C.VIP_WALLETS:
        return str(C.VIP_WALLETS[0]).lower()
    raise ValueError("No wallet provided and titan_config has no VIP_WALLETS configured.")


def summarise_pages(endpoint: str, pages: Sequence[PageResult], requested_limit: int) -> ValidationSummary:
    seen_keys: set[tuple[str, str, str, float, float, float]] = set()
    raw_trades: int = 0
    repeated_page_detected: bool = False
    observed_max_page_size: int = 0
    first_error_status: int | None = None
    first_error_text: str = ""
    oldest_timestamp: float | None = None
    newest_timestamp: float | None = None
    previous_first_key: tuple[str, str, str, float, float, float] | None = None
    previous_last_key: tuple[str, str, str, float, float, float] | None = None

    for page in pages:
        if page.http_status >= 400:
            if first_error_status is None:
                first_error_status = page.http_status
                first_error_text = page.error_text
            continue
        raw_trades += page.count
        if page.count > observed_max_page_size:
            observed_max_page_size = page.count
        if page.first_key is not None and page.last_key is not None:
            if page.first_key == previous_first_key and page.last_key == previous_last_key:
                repeated_page_detected = True
            previous_first_key = page.first_key
            previous_last_key = page.last_key

        for trade in page.trades:
            seen_keys.add(trade.unique_key())
            if oldest_timestamp is None or trade.timestamp < oldest_timestamp:
                oldest_timestamp = trade.timestamp
            if newest_timestamp is None or trade.timestamp > newest_timestamp:
                newest_timestamp = trade.timestamp

    unique_trades: int = len(seen_keys)
    return ValidationSummary(
        endpoint=endpoint,
        pages_fetched=len(pages),
        raw_trades=raw_trades,
        unique_trades=unique_trades,
        duplicate_trades=raw_trades - unique_trades,
        repeated_page_detected=repeated_page_detected,
        requested_limit=requested_limit,
        observed_max_page_size=observed_max_page_size,
        first_error_status=first_error_status,
        first_error_text=first_error_text,
        oldest_timestamp=oldest_timestamp,
        newest_timestamp=newest_timestamp,
    )


def format_timestamp(timestamp_value: float | None) -> str:
    if timestamp_value is None or timestamp_value <= 0:
        return "n/a"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(timestamp_value))


def _payload_timestamps(payload: object) -> tuple[int, float | None, float | None]:
    if not isinstance(payload, list):
        return 0, None, None
    timestamps: list[float] = []
    for item in payload:
        if not isinstance(item, Mapping):
            continue
        try:
            timestamps.append(float(item.get("timestamp") or 0.0))
        except (TypeError, ValueError):
            continue
    timestamps = [timestamp for timestamp in timestamps if timestamp > 0.0]
    if not timestamps:
        return len(payload), None, None
    return len(payload), min(timestamps), max(timestamps)


def probe_filter_support(session: requests.Session, wallet: str, timeout_seconds: int) -> list[FilterProbeResult]:
    base_params: dict[str, str | int] = {"user": wallet, "limit": 5}
    candidate_params: list[tuple[str, dict[str, str | int]]] = [
        ("baseline", {}),
        ("startTs", {"startTs": 1740000000}),
        ("endTs", {"endTs": 1740000000}),
        ("since", {"since": 1740000000}),
        ("before", {"before": 1740000000}),
        ("after", {"after": 1740000000}),
        ("startDate", {"startDate": "2025-03-01"}),
        ("endDate", {"endDate": "2025-03-01"}),
        ("timestamp_lt", {"timestamp_lt": 1740000000}),
        ("timestamp_gt", {"timestamp_gt": 1740000000}),
    ]
    results: list[FilterProbeResult] = []
    baseline_min_ts: float | None = None
    baseline_max_ts: float | None = None

    for name, extra in candidate_params:
        params: dict[str, str | int] = dict(base_params)
        params.update(extra)
        response: requests.Response = session.get(f"{C.DATA_API}/trades", params=params, timeout=timeout_seconds)
        error_text: str = response.text[:300] if response.status_code >= 400 else ""
        count: int = 0
        min_timestamp: float | None = None
        max_timestamp: float | None = None
        if response.status_code < 400:
            payload: object = response.json()
            count, min_timestamp, max_timestamp = _payload_timestamps(payload)
        if name == "baseline":
            baseline_min_ts = min_timestamp
            baseline_max_ts = max_timestamp
        changed_window: bool = (
            name != "baseline"
            and (min_timestamp != baseline_min_ts or max_timestamp != baseline_max_ts or count == 0)
        )
        results.append(
            FilterProbeResult(
                name=name,
                status_code=response.status_code,
                count=count,
                min_timestamp=min_timestamp,
                max_timestamp=max_timestamp,
                changed_window=changed_window,
                error_text=error_text,
            )
        )
    return results


def parse_args() -> argparse.Namespace:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description="Validate how much wallet history Polymarket can return for /trades or /activity."
    )
    parser.add_argument("--wallet", type=str, default="", help="Wallet address to probe. Defaults to the first configured VIP wallet.")
    parser.add_argument("--endpoint", choices=["trades", "activity"], default="trades", help="Endpoint to probe.")
    parser.add_argument("--limit", type=int, default=10000, help="Per-request /trades limit.")
    parser.add_argument("--max-pages", type=int, default=2, help="How many offset pages to request.")
    parser.add_argument("--side", type=str, default="", help="Optional side filter, e.g. BUY or SELL.")
    parser.add_argument("--expect-min-unique", type=int, default=0, help="Fail if fewer unique trades are fetched.")
    parser.add_argument("--sleep-ms", type=int, default=150, help="Delay between page requests.")
    parser.add_argument("--timeout-seconds", type=int, default=30, help="HTTP timeout per page request.")
    parser.add_argument("--probe-date-filters", action="store_true", help="Test candidate timestamp/date filters and report whether they affect the result window.")
    return parser.parse_args()


def main() -> int:
    args: argparse.Namespace = parse_args()
    wallet: str = args.wallet.lower() if args.wallet else pick_default_wallet()
    side: str | None = args.side.upper() if args.side else None

    print(f"Wallet: {wallet}")
    print(f"Endpoint: {C.DATA_API}/{args.endpoint}")
    print(f"Limit per page: {args.limit}")
    print(f"Max pages: {args.max_pages}")
    print(f"Side filter: {side or 'ALL'}")

    session: requests.Session = build_session()
    pages: list[PageResult] = []
    filter_results: list[FilterProbeResult] = []

    try:
        for page_index in range(args.max_pages):
            request: PageRequest = PageRequest(
                wallet=wallet,
                endpoint=args.endpoint,
                limit=args.limit,
                offset=page_index * args.limit,
                side=side,
            )
            page: PageResult = fetch_trade_page(session, request, page_index, args.timeout_seconds)
            pages.append(page)

            if page.http_status >= 400:
                print(
                    f"page={page.page_index} status={page.http_status} count=0 "
                    f"elapsed_ms={page.elapsed_ms:.1f} error={page.error_text}"
                )
                break

            print(
                f"page={page.page_index} status={page.http_status} count={page.count} "
                f"elapsed_ms={page.elapsed_ms:.1f} "
                f"newest={format_timestamp(page.max_timestamp)} oldest={format_timestamp(page.min_timestamp)}"
            )

            if page.count < args.limit:
                print("Stopping: page returned fewer rows than requested limit.")
                break

            if page_index + 1 < args.max_pages:
                time.sleep(args.sleep_ms / 1000.0)

        if args.probe_date_filters:
            filter_results = probe_filter_support(session, wallet, args.timeout_seconds)
    finally:
        session.close()

    summary: ValidationSummary = summarise_pages(args.endpoint, pages, args.limit)
    print("")
    print(f"endpoint={summary.endpoint}")
    print(f"pages_fetched={summary.pages_fetched}")
    print(f"requested_limit={summary.requested_limit}")
    print(f"observed_max_page_size={summary.observed_max_page_size}")
    print(f"raw_trades={summary.raw_trades}")
    print(f"unique_trades={summary.unique_trades}")
    print(f"duplicate_trades={summary.duplicate_trades}")
    print(f"repeated_page_detected={summary.repeated_page_detected}")
    print(f"newest_trade={format_timestamp(summary.newest_timestamp)}")
    print(f"oldest_trade={format_timestamp(summary.oldest_timestamp)}")
    if summary.first_error_status is not None:
        print(f"first_error_status={summary.first_error_status}")
        print(f"first_error_text={summary.first_error_text}")

    if summary.repeated_page_detected:
        print("FAIL: offset pagination appears to have returned the same page twice.")
        return 2

    if summary.requested_limit > summary.observed_max_page_size and summary.observed_max_page_size > 0:
        print(
            f"NOTE: requested limit {summary.requested_limit} was not honored; "
            f"largest returned page was {summary.observed_max_page_size}."
        )

    if args.expect_min_unique > 0 and summary.unique_trades < args.expect_min_unique:
        print(
            f"FAIL: expected at least {args.expect_min_unique} unique trades, "
            f"got {summary.unique_trades}."
        )
        return 3

    if args.probe_date_filters:
        print("")
        print("date_filter_probe:")
        for result in filter_results:
            print(
                json.dumps(
                    {
                        "name": result.name,
                        "status_code": result.status_code,
                        "count": result.count,
                        "newest_trade": format_timestamp(result.max_timestamp),
                        "oldest_trade": format_timestamp(result.min_timestamp),
                        "changed_window": result.changed_window,
                        "error_text": result.error_text,
                    }
                )
            )

    print("PASS: /trades returned distinct pages for this wallet probe.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
