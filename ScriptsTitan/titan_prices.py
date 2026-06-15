from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator


PriceHistoryPoint = tuple[float, float]
PriceHistoryResult = tuple[list[PriceHistoryPoint], str, str | None]
_HISTORY_GAP_THRESHOLD_SECONDS = 6 * 60 * 60


# ─────────────────────────────────────────────────────────────────────────────
#  Parsing helpers (shared, no deps)
# ─────────────────────────────────────────────────────────────────────────────

def _coerce_ts(value: object) -> float | None:
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1_000_000_000_000:
            ts /= 1000.0
        return ts if ts > 0 else None
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        try:
            return _coerce_ts(float(raw))
        except ValueError:
            pass
        normalized = raw.replace("Z", "+00:00").replace(" ", "T")
        try:
            dt = datetime.fromisoformat(normalized)
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt.timestamp()
    return None


def _coerce_price(value: object) -> float | None:
    if isinstance(value, (int, float, str)):
        try:
            price = float(value)
        except ValueError:
            return None
        return price if 0.0 <= price <= 1.0 else None
    return None


def extract_history_points(payload: object) -> list[tuple[float, float]]:
    rows: object = payload
    if isinstance(payload, dict):
        for key in ("history", "data", "prices"):
            candidate = payload.get(key)
            if isinstance(candidate, list):
                rows = candidate
                break

    if not isinstance(rows, list):
        return []

    points_by_ts: dict[float, float] = {}
    for row in rows:
        ts: float | None = None
        price: float | None = None
        if isinstance(row, dict):
            for ts_key in ("t", "timestamp", "ts", "time"):
                if ts_key in row:
                    ts = _coerce_ts(row.get(ts_key))
                    if ts is not None:
                        break
            for price_key in ("p", "price", "value"):
                if price_key in row:
                    price = _coerce_price(row.get(price_key))
                    if price is not None:
                        break
        elif isinstance(row, (list, tuple)) and len(row) >= 2:
            ts = _coerce_ts(row[0])
            price = _coerce_price(row[1])
        if ts is None or price is None:
            continue
        points_by_ts[ts] = price

    return sorted(points_by_ts.items(), key=lambda item: item[0])


def history_has_large_gap(
    points: list[PriceHistoryPoint],
    start_ts: float,
    end_ts: float,
    gap_threshold_seconds: float = _HISTORY_GAP_THRESHOLD_SECONDS,
) -> bool:
    if not points:
        return True

    window_start = max(0.0, float(start_ts))
    window_end = max(window_start, float(end_ts))
    if window_end <= 0.0:
        return False

    relevant_points = [point for point in points if window_start <= point[0] <= window_end]
    if not relevant_points:
        return True

    first_ts = float(relevant_points[0][0])
    last_ts = float(relevant_points[-1][0])
    if first_ts - window_start > gap_threshold_seconds:
        return True
    if window_end - last_ts > gap_threshold_seconds:
        return True

    prev_ts = first_ts
    for ts, _price in relevant_points[1:]:
        current_ts = float(ts)
        if current_ts - prev_ts > gap_threshold_seconds:
            return True
        prev_ts = current_ts
    return False


# ─────────────────────────────────────────────────────────────────────────────
#  PricesCache — in-memory, client-safe
# ─────────────────────────────────────────────────────────────────────────────

class PricesCache:
    def __init__(self) -> None:
        self._data: dict[str, list[PriceHistoryPoint]] = {}

    def get(self, asset: str) -> list[PriceHistoryPoint]:
        return list(self._data.get(asset, []))

    def latest(self, asset: str) -> PriceHistoryPoint | None:
        points = self._data.get(asset)
        return points[-1] if points else None

    def add_point(self, asset: str, ts: float, price: float) -> None:
        points = self._data.setdefault(asset, [])
        if points and points[-1][0] == ts:
            points[-1] = (ts, price)
        else:
            points.append((ts, price))
        self._on_add_point(asset, ts, price)

    def add_points(self, asset: str, points: list[PriceHistoryPoint]) -> None:
        if not points:
            return
        existing = self._data.get(asset)
        if not existing:
            self._data[asset] = sorted(points, key=lambda p: p[0])
        else:
            existing_ts = {p[0] for p in existing}
            new = [p for p in points if p[0] not in existing_ts]
            if new:
                existing.extend(new)
                existing.sort(key=lambda p: p[0])
        self._on_add_points(asset, points)

    def ingest(self, asset: str, points: list[PriceHistoryPoint]) -> None:
        """Merge points from server without DB write. Client-side entry point."""
        self.add_points(asset, points)

    def get_prices(self, asset: str) -> PriceHistoryResult:
        asset_id = asset.strip()
        if not asset_id:
            return [], "", "Missing asset"

        points = self._data.get(asset_id)
        if points:
            return list(points), "memory_cache", None

        self.ensure(asset_id)
        loaded_points = self._data.get(asset_id)
        if loaded_points:
            return list(loaded_points), "prices_cache", None

        return [], "", "No price history available"

    def ensure(self, asset: str) -> None:
        """Load history if not cached. No-op on client; overridden by PricesCacheSrv."""
        pass

    def refresh(self, asset: str) -> None:
        """Force-fetch latest prices. No-op on client; overridden by PricesCacheSrv."""
        pass

    def ensure_history_range(self, asset: str, start_ts: float, end_ts: float) -> None:
        """Ensure the cached history covers the requested time window."""
        pass

    # Override hooks for subclasses
    def _on_add_point(self, asset: str, ts: float, price: float) -> None:
        pass

    def _on_add_points(self, asset: str, points: list[PriceHistoryPoint]) -> None:
        pass







# ─────────────────────────────────────────────────────────────────────────────
#  PricesCacheSrv — server only, adds DB + Polymarket fetch
# ─────────────────────────────────────────────────────────────────────────────

_CLOB_HISTORY_API = "https://clob.polymarket.com/prices-history"
_MAX_CACHED_POINTS = 2880


def _ts_to_dt(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _dt_to_ts(dt_str: str) -> float:
    normalized = dt_str.replace("Z", "+00:00").replace(" ", "T")
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


class PricesCacheSrv(PricesCache):
    def __init__(self) -> None:
        super().__init__()
        self._db_path: str = ""

    def init_db(self, db_path: str) -> None:
        self._db_path = db_path

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        cx = sqlite3.connect(self._db_path, timeout=10)
        cx.execute("PRAGMA journal_mode=WAL")
        try:
            yield cx
            cx.commit()
        finally:
            cx.close()

    # ── DB write ──────────────────────────────────────────────────────────────

    def upsert(self, asset: str, points: list[PriceHistoryPoint]) -> None:
        if not points or not self._db_path:
            return
        rows = [(_ts_to_dt(ts), _ts_to_dt(ts), float(p), asset)
                for ts, p in points]
        with self._connect() as cx:
            cx.executemany(
                "INSERT OR IGNORE INTO price_history (ts, recorded_at, price, asset) "
                "VALUES (?, ?, ?, ?)",
                rows,
            )

    def delete(self, asset: str) -> None:
        if not self._db_path:
            return
        with self._connect() as cx:
            cx.execute("DELETE FROM price_history WHERE asset = ?", (asset,))

    # ── DB read ───────────────────────────────────────────────────────────────

    def load_from_db(self, asset: str, limit: int = _MAX_CACHED_POINTS) -> list[PriceHistoryPoint]:
        if not self._db_path:
            return []
        with self._connect() as cx:
            rows = cx.execute(
                "SELECT ts, price FROM price_history "
                "WHERE asset = ? ORDER BY ts DESC LIMIT ?",
                (asset, limit),
            ).fetchall()
        return [(_dt_to_ts(str(r[0])), float(r[1])) for r in reversed(rows)]

    # ── Polymarket fetch ──────────────────────────────────────────────────────

    def fetch_from_api(self, asset: str) -> list[PriceHistoryPoint]:
        import titan_state as S
        asset_id = asset.strip()
        if not asset_id:
            return []
        payload = S.safe_get(
            _CLOB_HISTORY_API,
            {"market": asset_id, "interval": "max", "fidelity": 60},
            timeout=20,
            quiet=True,
        )
        if payload is None:
            return []
        return extract_history_points(payload)

    # ── High-level ops ────────────────────────────────────────────────────────

    def ensure(self, asset: str) -> None:
        """Load from DB if not in memory, then fetch from API if still empty."""
        if self._data.get(asset):
            return
        points = self.load_from_db(asset)
        if points:
            self._data[asset] = points
            return
        points = self.fetch_from_api(asset)
        if points:
            self._data[asset] = points
            self.upsert(asset, points)

    def get_prices(self, asset: str) -> PriceHistoryResult:
        asset_id = asset.strip()
        if not asset_id:
            return [], "", "Missing asset"

        cached_points = self._data.get(asset_id)
        if cached_points:
            return list(cached_points), "memory_cache", None

        db_points = self.load_from_db(asset_id)
        if db_points:
            self._data[asset_id] = db_points
            return list(db_points), "db_cache", None

        api_points = self.fetch_from_api(asset_id)
        if api_points:
            self._data[asset_id] = api_points
            self.upsert(asset_id, api_points)
            return list(api_points), "clob_api", None

        return [], "", "No price history available"

    def refresh(self, asset: str) -> None:
        """Force fetch from API, merge into memory and DB."""
        points = self.fetch_from_api(asset)
        if points:
            self.add_points(asset, points)
            self.upsert(asset, points)

    def ensure_history_range(self, asset: str, start_ts: float, end_ts: float) -> None:
        asset_id = asset.strip()
        if not asset_id:
            return

        cached_points = self._data.get(asset_id)
        if not cached_points:
            db_points = self.load_from_db(asset_id)
            if db_points:
                self._data[asset_id] = db_points
                cached_points = db_points

        if cached_points and not history_has_large_gap(cached_points, start_ts, end_ts):
            return

        api_points = self.fetch_from_api(asset_id)
        if api_points:
            self.add_points(asset_id, api_points)

    # ── Override hooks: persist on every write ────────────────────────────────

    def _on_add_point(self, asset: str, ts: float, price: float) -> None:
        self.upsert(asset, [(ts, price)])

    def _on_add_points(self, asset: str, points: list[PriceHistoryPoint]) -> None:
        self.upsert(asset, points)


# ─────────────────────────────────────────────────────────────────────────────
#  Global instance — replaced by PricesCacheSrv on server at startup
# ─────────────────────────────────────────────────────────────────────────────

PRICES: PricesCache = PricesCache()
