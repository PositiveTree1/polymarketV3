"""
TITAN — SQLite layer for time-series data (price_history, equity_history, watchlist).
Keeps titan_state.json lean by offloading high-cardinality data here.
"""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

_DB_PATH: str = ""


def init_db(db_path: str) -> None:
    global _DB_PATH
    _DB_PATH = db_path
    with _connect() as cx:
        cx.executescript("""
            CREATE TABLE IF NOT EXISTS signals (
                id          INTEGER  PRIMARY KEY AUTOINCREMENT,
                recorded_at DATETIME NOT NULL,
                ts          DATETIME NOT NULL,
                data        TEXT     NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_signals_ts ON signals (ts);

            CREATE TABLE IF NOT EXISTS rejects (
                id          INTEGER  PRIMARY KEY AUTOINCREMENT,
                recorded_at DATETIME NOT NULL,
                ts          DATETIME NOT NULL,
                reason      TEXT     NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_rejects_ts ON rejects (ts);

            CREATE TABLE IF NOT EXISTS price_history (
                cid      TEXT    NOT NULL,
                outcome  TEXT    NOT NULL,
                ts       DATETIME NOT NULL,
                recorded_at DATETIME NOT NULL,
                price    REAL    NOT NULL,
                PRIMARY KEY (cid, outcome, ts)
            );
            CREATE INDEX IF NOT EXISTS idx_ph_cid_outcome ON price_history (cid, outcome);

            CREATE TABLE IF NOT EXISTS equity_history (
                ts          DATETIME NOT NULL PRIMARY KEY,
                recorded_at DATETIME NOT NULL,
                equity      REAL     NOT NULL
            );

            CREATE TABLE IF NOT EXISTS watchlist (
                address     TEXT     NOT NULL PRIMARY KEY,
                added_at    DATETIME NOT NULL
            );

            CREATE TABLE IF NOT EXISTS trade_history (
                id              INTEGER  PRIMARY KEY AUTOINCREMENT,
                cid             TEXT,
                type            TEXT     NOT NULL,
                title           TEXT,
                outcome         TEXT,
                entry_price     REAL,
                exit_price      REAL,
                shares          REAL,
                bet             REAL,
                pnl_usdc        REAL,
                pnl_pct         REAL,
                reason          TEXT,
                ts              DATETIME NOT NULL,
                ts_str          TEXT,
                bankroll        REAL,
                tier            TEXT,
                strategy        TEXT,
                stop_loss_pct   REAL,
                avg_entry       REAL,
                score           REAL,
                n_confluence    INTEGER,
                is_conviction   INTEGER,
                market_url      TEXT,
                entry_ts        DATETIME,
                exit_ts         DATETIME
            );
            CREATE INDEX IF NOT EXISTS idx_th_cid ON trade_history (cid);
            CREATE INDEX IF NOT EXISTS idx_th_ts  ON trade_history (ts);

            CREATE TABLE IF NOT EXISTS trade_history_wallets (
                trade_id    INTEGER  NOT NULL REFERENCES trade_history(id),
                wallet      TEXT     NOT NULL,
                name        TEXT,
                cash        REAL
            );
            CREATE INDEX IF NOT EXISTS idx_thw_trade ON trade_history_wallets (trade_id);

            CREATE TABLE IF NOT EXISTS trade_history_audit (
                trade_id    INTEGER  NOT NULL REFERENCES trade_history(id),
                audit_type  TEXT     NOT NULL,
                data        TEXT     NOT NULL
            );
        """)
        _migrate_ts_columns(cx)


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    cx = sqlite3.connect(_DB_PATH, timeout=10)
    cx.execute("PRAGMA journal_mode=WAL")
    try:
        yield cx
        cx.commit()
    finally:
        cx.close()


def _ts_to_dt(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")


def _dt_to_ts(value: str) -> float:
    normalized = value.strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(normalized.replace(" ", "T"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.timestamp()


def _normalize_db_ts(value: object) -> str:
    if isinstance(value, str):
        return _ts_to_dt(_dt_to_ts(value))
    return _ts_to_dt(float(value))


def _migrate_ts_columns(cx: sqlite3.Connection) -> None:
    migrations = (
        (
            "signals",
            """
            CREATE TABLE signals__new (
                id          INTEGER  PRIMARY KEY AUTOINCREMENT,
                recorded_at DATETIME NOT NULL,
                ts          DATETIME NOT NULL,
                data        TEXT     NOT NULL
            )
            """,
            "INSERT INTO signals__new (id, recorded_at, ts, data) VALUES (?, ?, ?, ?)",
            "SELECT id, recorded_at, ts, data FROM signals ORDER BY id ASC",
            ("CREATE INDEX idx_signals_ts ON signals (ts)",),
        ),
        (
            "rejects",
            """
            CREATE TABLE rejects__new (
                id          INTEGER  PRIMARY KEY AUTOINCREMENT,
                recorded_at DATETIME NOT NULL,
                ts          DATETIME NOT NULL,
                reason      TEXT     NOT NULL
            )
            """,
            "INSERT INTO rejects__new (id, recorded_at, ts, reason) VALUES (?, ?, ?, ?)",
            "SELECT id, recorded_at, ts, reason FROM rejects ORDER BY id ASC",
            ("CREATE INDEX idx_rejects_ts ON rejects (ts)",),
        ),
        (
            "price_history",
            """
            CREATE TABLE price_history__new (
                cid         TEXT     NOT NULL,
                outcome     TEXT     NOT NULL,
                ts          DATETIME NOT NULL,
                recorded_at DATETIME NOT NULL,
                price       REAL     NOT NULL,
                PRIMARY KEY (cid, outcome, ts)
            )
            """,
            "INSERT OR IGNORE INTO price_history__new (cid, outcome, ts, recorded_at, price) VALUES (?, ?, ?, ?, ?)",
            "SELECT cid, outcome, ts, recorded_at, price FROM price_history ORDER BY cid ASC, outcome ASC, ts ASC",
            ("CREATE INDEX idx_ph_cid_outcome ON price_history (cid, outcome)",),
        ),
        (
            "equity_history",
            """
            CREATE TABLE equity_history__new (
                ts          DATETIME NOT NULL PRIMARY KEY,
                recorded_at DATETIME NOT NULL,
                equity      REAL     NOT NULL
            )
            """,
            "INSERT OR IGNORE INTO equity_history__new (ts, recorded_at, equity) VALUES (?, ?, ?)",
            "SELECT ts, recorded_at, equity FROM equity_history ORDER BY ts ASC",
            (),
        ),
    )
    for table_name, create_sql, insert_sql, select_sql, index_sql in migrations:
        if _ts_column_type(cx, table_name) == "DATETIME":
            continue
        rows = cx.execute(select_sql).fetchall()
        cx.execute(f"DROP TABLE IF EXISTS {table_name}__new")
        cx.execute(create_sql)
        if table_name in {"signals", "rejects"}:
            payload = [(row[0], row[1], _normalize_db_ts(row[2]), row[3]) for row in rows]
        elif table_name == "price_history":
            payload = [(row[0], row[1], _normalize_db_ts(row[2]), row[3], row[4]) for row in rows]
        else:
            payload = [(_normalize_db_ts(row[0]), row[1], row[2]) for row in rows]
        cx.executemany(insert_sql, payload)
        cx.execute(f"DROP TABLE {table_name}")
        cx.execute(f"ALTER TABLE {table_name}__new RENAME TO {table_name}")
        for statement in index_sql:
            cx.execute(statement)


def _ts_column_type(cx: sqlite3.Connection, table_name: str) -> str | None:
    rows = cx.execute(f"PRAGMA table_info({table_name})").fetchall()
    for row in rows:
        if row[1] == "ts":
            return str(row[2]).upper() or None
    return None


# ── price_history ────────────────────────────────────────────────────────────

def upsert_price_history(cid: str, outcome: str, points: list[tuple[float, float]]) -> None:
    """Write (ts, price) pairs for a position. Ignores duplicates."""
    if not points or not _DB_PATH:
        return
    rows = [(cid, outcome, _ts_to_dt(float(ts)), _ts_to_dt(float(ts)), float(p))
            for ts, p in points]
    with _connect() as cx:
        cx.executemany(
            "INSERT OR IGNORE INTO price_history (cid, outcome, ts, recorded_at, price) "
            "VALUES (?, ?, ?, ?, ?)",
            rows,
        )


def load_price_history(cid: str, outcome: str, limit: int = 2880) -> list[tuple[float, float]]:
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
    return [(_dt_to_ts(str(r[0])), float(r[1])) for r in reversed(rows)]


def delete_price_history(cid: str, outcome: str) -> None:
    """Remove all price history for a closed position."""
    if not _DB_PATH:
        return
    with _connect() as cx:
        cx.execute(
            "DELETE FROM price_history WHERE cid = ? AND outcome = ?",
            (cid, outcome),
        )


# ── equity_history ───────────────────────────────────────────────────────────

def upsert_equity_history(points: list[tuple[float, float]]) -> None:
    """Write (ts, equity) pairs. Ignores duplicates."""
    if not points or not _DB_PATH:
        return
    rows = [(_ts_to_dt(float(ts)), _ts_to_dt(float(ts)), float(eq)) for ts, eq in points]
    with _connect() as cx:
        cx.executemany(
            "INSERT OR IGNORE INTO equity_history (ts, recorded_at, equity) "
            "VALUES (?, ?, ?)",
            rows,
        )


def load_equity_history(limit: int = 4000) -> list[tuple[float, float]]:
    """Return [(ts, equity), ...] ordered oldest-first, capped at limit."""
    if not _DB_PATH:
        return []
    with _connect() as cx:
        rows = cx.execute(
            "SELECT ts, equity FROM equity_history "
            "ORDER BY ts DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [(_dt_to_ts(str(r[0])), float(r[1])) for r in reversed(rows)]


# ── watchlist ────────────────────────────────────────────────────────────────

def upsert_watchlist(addresses: set[str]) -> None:
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


def load_watchlist() -> set[str]:
    """Return the full set of watched addresses."""
    if not _DB_PATH:
        return set()
    with _connect() as cx:
        rows = cx.execute("SELECT address FROM watchlist").fetchall()
    return {r[0] for r in rows}


def remove_from_watchlist(addresses: set[str]) -> None:
    if not addresses or not _DB_PATH:
        return
    with _connect() as cx:
        cx.executemany(
            "DELETE FROM watchlist WHERE address = ?",
            [(addr.lower(),) for addr in addresses],
        )


# ── signals ──────────────────────────────────────────────────────────────────

def save_signals(signals: list[dict], ts: float) -> None:
    if not _DB_PATH:
        return
    now = _ts_to_dt(ts)
    with _connect() as cx:
        cx.executemany(
            "INSERT INTO signals (recorded_at, ts, data) VALUES (?, ?, ?)",
            [(now, now, json.dumps(s, default=str)) for s in signals],
        )


def save_rejects(rejects: list[str], ts: float) -> None:
    if not _DB_PATH:
        return
    now = _ts_to_dt(ts)
    with _connect() as cx:
        cx.executemany(
            "INSERT INTO rejects (recorded_at, ts, reason) VALUES (?, ?, ?)",
            [(now, now, r) for r in rejects],
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
            "snapshot_ts": _dt_to_ts(str(snapshot_ts)),
            "signal": sig,
        })
    return out


def get_schema_description() -> str:
    """Return a human-readable schema string for all tables in the DB."""
    if not _DB_PATH:
        return "(DB not initialised)"
    with _connect() as cx:
        tables = cx.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        parts: list[str] = []
        for (tbl,) in tables:
            cols = cx.execute(f"PRAGMA table_info({tbl})").fetchall()
            col_defs = ", ".join(f"{c[1]} {c[2]}" for c in cols)
            parts.append(f"{tbl}({col_defs})")
    return "; ".join(parts)


# ── trade_history ────────────────────────────────────────────────────────────

_TRADE_SCALAR_COLS = (
    "cid", "type", "title", "outcome", "entry_price", "exit_price",
    "shares", "bet", "pnl_usdc", "pnl_pct", "reason", "ts", "ts_str",
    "bankroll", "tier", "strategy", "stop_loss_pct", "avg_entry",
    "score", "n_confluence", "is_conviction", "market_url",
    "entry_ts", "exit_ts",
)


def _trade_to_row(trade: dict) -> tuple:
    ts_raw = trade.get("ts")
    ts = _ts_to_dt(float(ts_raw)) if ts_raw is not None else _ts_to_dt(0.0)
    entry_ts_raw = trade.get("entry_ts")
    exit_ts_raw = trade.get("exit_ts")
    return (
        trade.get("cid"),
        trade.get("type", ""),
        trade.get("title"),
        trade.get("outcome"),
        trade.get("entry_price"),
        trade.get("exit_price"),
        trade.get("shares"),
        trade.get("bet"),
        trade.get("pnl_usdc"),
        trade.get("pnl_pct"),
        trade.get("reason"),
        ts,
        trade.get("ts_str"),
        trade.get("bankroll"),
        trade.get("tier"),
        trade.get("strategy"),
        trade.get("stop_loss_pct"),
        trade.get("avg_entry"),
        trade.get("score"),
        trade.get("n_confluence"),
        int(trade.get("is_conviction") or 0),
        trade.get("market_url"),
        _ts_to_dt(float(entry_ts_raw)) if entry_ts_raw is not None else None,
        _ts_to_dt(float(exit_ts_raw)) if exit_ts_raw is not None else None,
    )


def _row_to_trade(row: sqlite3.Row, wallets: list[dict], audits: list[dict]) -> dict:
    trade: dict = dict(row)
    ts_str = trade.get("ts")
    trade["ts"] = _dt_to_ts(str(ts_str)) if ts_str else 0.0
    if trade.get("entry_ts"):
        trade["entry_ts"] = _dt_to_ts(str(trade["entry_ts"]))
    if trade.get("exit_ts"):
        trade["exit_ts"] = _dt_to_ts(str(trade["exit_ts"]))
    trade["is_conviction"] = bool(trade.get("is_conviction"))

    elite_wallets = [w["wallet"] for w in wallets]
    whale_names = [w["name"] for w in wallets if w.get("name")]
    whale_buy_cash = {w["wallet"]: w["cash"] for w in wallets if w.get("cash") is not None}
    trade["elite_wallets"] = elite_wallets
    trade["whale_names"] = whale_names
    trade["whale_buy_cash"] = whale_buy_cash

    for audit in audits:
        trade[audit["audit_type"]] = json.loads(audit["data"])

    trade.pop("id", None)
    return trade


def append_trade(trade: dict) -> None:
    if not _DB_PATH:
        return
    with _connect() as cx:
        placeholders = ", ".join("?" * len(_TRADE_SCALAR_COLS))
        cols = ", ".join(_TRADE_SCALAR_COLS)
        cur = cx.execute(
            f"INSERT INTO trade_history ({cols}) VALUES ({placeholders})",
            _trade_to_row(trade),
        )
        trade_id = cur.lastrowid

        wallets = trade.get("elite_wallets") or []
        names = trade.get("whale_names") or []
        cash_map = trade.get("whale_buy_cash") or {}
        wallet_rows = [
            (trade_id, w, names[i] if i < len(names) else None, cash_map.get(w))
            for i, w in enumerate(wallets)
        ]
        if wallet_rows:
            cx.executemany(
                "INSERT INTO trade_history_wallets (trade_id, wallet, name, cash) VALUES (?, ?, ?, ?)",
                wallet_rows,
            )

        for audit_type in ("entry_audit", "exit_audit"):
            audit_data = trade.get(audit_type)
            if audit_data is not None:
                cx.execute(
                    "INSERT INTO trade_history_audit (trade_id, audit_type, data) VALUES (?, ?, ?)",
                    (trade_id, audit_type, json.dumps(audit_data, default=str)),
                )


def bulk_insert_trades(trades: list[dict]) -> None:
    if not trades or not _DB_PATH:
        return
    for trade in trades:
        append_trade(trade)


def load_trade_history(limit: int = 5000) -> list[dict]:
    if not _DB_PATH:
        return []
    with _connect() as cx:
        cx.row_factory = sqlite3.Row
        rows = cx.execute(
            "SELECT * FROM trade_history ORDER BY ts ASC LIMIT ?", (limit,)
        ).fetchall()
        if not rows:
            return []
        trade_ids = [r["id"] for r in rows]
        placeholders = ",".join("?" * len(trade_ids))
        wallet_rows = cx.execute(
            f"SELECT * FROM trade_history_wallets WHERE trade_id IN ({placeholders})",
            trade_ids,
        ).fetchall()
        audit_rows = cx.execute(
            f"SELECT * FROM trade_history_audit WHERE trade_id IN ({placeholders})",
            trade_ids,
        ).fetchall()

    wallets_by_id: dict[int, list[dict]] = {}
    for wr in wallet_rows:
        wallets_by_id.setdefault(wr["trade_id"], []).append(dict(wr))

    audits_by_id: dict[int, list[dict]] = {}
    for ar in audit_rows:
        audits_by_id.setdefault(ar["trade_id"], []).append(dict(ar))

    return [
        _row_to_trade(r, wallets_by_id.get(r["id"], []), audits_by_id.get(r["id"], []))
        for r in rows
    ]


def get_trade_count() -> int:
    if not _DB_PATH:
        return 0
    with _connect() as cx:
        row = cx.execute("SELECT COUNT(*) FROM trade_history").fetchone()
    return int(row[0]) if row else 0


def delete_all_trades() -> None:
    if not _DB_PATH:
        return
    with _connect() as cx:
        cx.executescript("""
            DELETE FROM trade_history_audit;
            DELETE FROM trade_history_wallets;
            DELETE FROM trade_history;
        """)


def query_db(sql: str) -> list[dict]:
    """Execute a read-only SELECT and return rows as dicts."""
    if not _DB_PATH:
        return []
    stripped = sql.strip().upper()
    if not stripped.startswith("SELECT"):
        raise ValueError("Only SELECT statements are allowed")
    with _connect() as cx:
        cur = cx.execute(sql)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def load_latest_rejects(limit: int = 200) -> list[str]:
    if not _DB_PATH:
        return []
    with _connect() as cx:
        rows = cx.execute(
            "SELECT reason FROM rejects ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [r[0] for r in reversed(rows)]
