"""
TITAN — SQLite layer for time-series data (price_history, equity_history, watchlist).
Keeps titan_state.json lean by offloading high-cardinality data here.
"""

import sqlite3, time
from contextlib import contextmanager
from datetime import datetime, timezone

_DB_PATH: str = ""


def init_db(db_path: str):
    global _DB_PATH
    _DB_PATH = db_path
    with _connect() as cx:
        cx.executescript("""
            CREATE TABLE IF NOT EXISTS signals (
                id          INTEGER  PRIMARY KEY AUTOINCREMENT,
                recorded_at DATETIME NOT NULL,
                ts          REAL     NOT NULL,
                data        TEXT     NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_signals_ts ON signals (ts);

            CREATE TABLE IF NOT EXISTS rejects (
                id          INTEGER  PRIMARY KEY AUTOINCREMENT,
                recorded_at DATETIME NOT NULL,
                ts          REAL     NOT NULL,
                reason      TEXT     NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_rejects_ts ON rejects (ts);

            CREATE TABLE IF NOT EXISTS price_history (
                cid      TEXT    NOT NULL,
                outcome  TEXT    NOT NULL,
                ts       REAL    NOT NULL,
                recorded_at DATETIME NOT NULL,
                price    REAL    NOT NULL,
                PRIMARY KEY (cid, outcome, ts)
            );
            CREATE INDEX IF NOT EXISTS idx_ph_cid_outcome ON price_history (cid, outcome);

            CREATE TABLE IF NOT EXISTS equity_history (
                ts          REAL     NOT NULL PRIMARY KEY,
                recorded_at DATETIME NOT NULL,
                equity      REAL     NOT NULL
            );

            CREATE TABLE IF NOT EXISTS watchlist (
                address     TEXT     NOT NULL PRIMARY KEY,
                added_at    DATETIME NOT NULL
            );
        """)


@contextmanager
def _connect():
    cx = sqlite3.connect(_DB_PATH, timeout=10)
    cx.execute("PRAGMA journal_mode=WAL")
    try:
        yield cx
        cx.commit()
    finally:
        cx.close()


def _ts_to_dt(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# ── price_history ────────────────────────────────────────────────────────────

def upsert_price_history(cid: str, outcome: str, points: list):
    """Write (ts, price) pairs for a position. Ignores duplicates."""
    if not points or not _DB_PATH:
        return
    rows = [(cid, outcome, float(ts), _ts_to_dt(float(ts)), float(p))
            for ts, p in points]
    with _connect() as cx:
        cx.executemany(
            "INSERT OR IGNORE INTO price_history (cid, outcome, ts, recorded_at, price) "
            "VALUES (?, ?, ?, ?, ?)",
            rows,
        )


def load_price_history(cid: str, outcome: str, limit: int = 2880) -> list:
    """Return [(ts, price), ...] ordered oldest-first, capped at limit."""
    if not _DB_PATH:
        return []
    with _connect() as cx:
        rows = cx.execute(
            "SELECT ts, price FROM price_history "
            "WHERE cid = ? AND outcome = ? "
            "ORDER BY ts DESC LIMIT ?",
            (cid, outcome, limit),
        ).fetchall()
    return [(r[0], r[1]) for r in reversed(rows)]


def delete_price_history(cid: str, outcome: str):
    """Remove all price history for a closed position."""
    if not _DB_PATH:
        return
    with _connect() as cx:
        cx.execute(
            "DELETE FROM price_history WHERE cid = ? AND outcome = ?",
            (cid, outcome),
        )


# ── equity_history ───────────────────────────────────────────────────────────

def upsert_equity_history(points: list):
    """Write (ts, equity) pairs. Ignores duplicates."""
    if not points or not _DB_PATH:
        return
    rows = [(float(ts), _ts_to_dt(float(ts)), float(eq)) for ts, eq in points]
    with _connect() as cx:
        cx.executemany(
            "INSERT OR IGNORE INTO equity_history (ts, recorded_at, equity) "
            "VALUES (?, ?, ?)",
            rows,
        )


def load_equity_history(limit: int = 4000) -> list:
    """Return [(ts, equity), ...] ordered oldest-first, capped at limit."""
    if not _DB_PATH:
        return []
    with _connect() as cx:
        rows = cx.execute(
            "SELECT ts, equity FROM equity_history "
            "ORDER BY ts DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [(r[0], r[1]) for r in reversed(rows)]


# ── watchlist ────────────────────────────────────────────────────────────────

def upsert_watchlist(addresses: set):
    """Add new addresses; existing ones are ignored (INSERT OR IGNORE)."""
    if not addresses or not _DB_PATH:
        return
    now = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    rows = [(addr.lower(), now) for addr in addresses]
    with _connect() as cx:
        cx.executemany(
            "INSERT OR IGNORE INTO watchlist (address, added_at) VALUES (?, ?)",
            rows,
        )


def load_watchlist() -> set:
    """Return the full set of watched addresses."""
    if not _DB_PATH:
        return set()
    with _connect() as cx:
        rows = cx.execute("SELECT address FROM watchlist").fetchall()
    return {r[0] for r in rows}


def remove_from_watchlist(addresses: set):
    if not addresses or not _DB_PATH:
        return
    with _connect() as cx:
        cx.executemany(
            "DELETE FROM watchlist WHERE address = ?",
            [(addr.lower(),) for addr in addresses],
        )


# ── signals ──────────────────────────────────────────────────────────────────

def save_signals(signals: list[dict], ts: float):
    if not _DB_PATH:
        return
    import json
    now = _ts_to_dt(ts)
    with _connect() as cx:
        cx.executemany(
            "INSERT INTO signals (recorded_at, ts, data) VALUES (?, ?, ?)",
            [(now, ts, json.dumps(s, default=str)) for s in signals],
        )


def save_rejects(rejects: list[str], ts: float):
    if not _DB_PATH:
        return
    now = _ts_to_dt(ts)
    with _connect() as cx:
        cx.executemany(
            "INSERT INTO rejects (recorded_at, ts, reason) VALUES (?, ?, ?)",
            [(now, ts, r) for r in rejects],
        )


def load_latest_signals(limit: int = 200) -> list[dict]:
    if not _DB_PATH:
        return []
    import json
    with _connect() as cx:
        latest_ts_row = cx.execute("SELECT MAX(ts) FROM signals").fetchone()
        latest_ts = latest_ts_row[0] if latest_ts_row else None
        if latest_ts is None:
            return []
        rows = cx.execute(
            "SELECT data FROM signals WHERE ts = ? ORDER BY id ASC LIMIT ?",
            (latest_ts, limit),
        ).fetchall()
    return [json.loads(r[0]) for r in rows]


def load_signal_history(limit: int = 200, min_score: float = 0.0, cid: str | None = None) -> list[dict]:
    if not _DB_PATH:
        return []
    import json
    query = (
        "SELECT recorded_at, ts, data FROM signals "
        "ORDER BY id DESC LIMIT ?"
    )
    with _connect() as cx:
        rows = cx.execute(query, (limit,)).fetchall()

    out: list[dict] = []
    for recorded_at, snapshot_ts, data in reversed(rows):
        sig = json.loads(data)
        if sig.get("score", 0) < min_score:
            continue
        if cid and sig.get("cid") != cid:
            continue
        out.append({
            "recorded_at": recorded_at,
            "snapshot_ts": snapshot_ts,
            "signal": sig,
        })
    return out


def load_latest_rejects(limit: int = 200) -> list[str]:
    if not _DB_PATH:
        return []
    with _connect() as cx:
        rows = cx.execute(
            "SELECT reason FROM rejects ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [r[0] for r in reversed(rows)]
