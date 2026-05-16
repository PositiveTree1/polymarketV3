"""
TITAN — SQLite layer for time-series data (price_history, equity_history, watchlist).
Keeps titan_state.json lean by offloading high-cardinality data here.
"""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Iterator, cast
if TYPE_CHECKING:
    from titan_state import TradeStats
    from titan_signals import SignalDict
from titan_trade import TradeRecord

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
                asset    TEXT    NOT NULL,
                ts       DATETIME NOT NULL,
                recorded_at DATETIME NOT NULL,
                price    REAL    NOT NULL,
                PRIMARY KEY (asset, ts)
            );

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
                asset           TEXT,
                type            TEXT     NOT NULL,
                title           TEXT,
                slug            TEXT,
                event_slug      TEXT,
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

            CREATE TABLE IF NOT EXISTS trade_stats (
                id          INTEGER PRIMARY KEY CHECK (id = 1),
                sell_count  INTEGER NOT NULL DEFAULT 0,
                win_count   INTEGER NOT NULL DEFAULT 0,
                loss_count  INTEGER NOT NULL DEFAULT 0,
                sum_pnl     REAL    NOT NULL DEFAULT 0.0,
                sum_wins    REAL    NOT NULL DEFAULT 0.0,
                sum_losses  REAL    NOT NULL DEFAULT 0.0,
                best        REAL    NOT NULL DEFAULT 0.0,
                worst       REAL    NOT NULL DEFAULT 0.0,
                updated_at  DATETIME NOT NULL
            );
        """)
        _migrate_ts_columns(cx)
        _migrate_add_columns(cx)
        _migrate_price_history_table(cx)
        _migrate_strip_signal_wallets(cx)


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
    if isinstance(value, (int, float)):
        return _ts_to_dt(float(value))
    raise TypeError(f"Expected timestamp-compatible value, got {type(value).__name__}")


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
            payload = [(row[0], _normalize_db_ts(row[1]), row[2], row[3]) for row in rows]
        else:
            payload = [(_normalize_db_ts(row[0]), row[1], row[2]) for row in rows]
        cx.executemany(insert_sql, payload)
        cx.execute(f"DROP TABLE {table_name}")
        cx.execute(f"ALTER TABLE {table_name}__new RENAME TO {table_name}")
        for statement in index_sql:
            cx.execute(statement)


def _migrate_add_columns(cx: sqlite3.Connection) -> None:
    existing = {row[1] for row in cx.execute("PRAGMA table_info(trade_history)").fetchall()}
    if "asset" not in existing:
        cx.execute("ALTER TABLE trade_history ADD COLUMN asset TEXT")
    if "slug" not in existing:
        cx.execute("ALTER TABLE trade_history ADD COLUMN slug TEXT")
    if "event_slug" not in existing:
        cx.execute("ALTER TABLE trade_history ADD COLUMN event_slug TEXT")
    if "price" not in existing:
        cx.execute("ALTER TABLE trade_history ADD COLUMN price REAL")
        cx.execute(
            """
            UPDATE trade_history
            SET price = CASE
                WHEN type = 'SELL' THEN COALESCE(exit_price, entry_price)
                ELSE COALESCE(entry_price, exit_price)
            END
            WHERE price IS NULL
            """
        )


def _price_history_uses_asset_key(cx: sqlite3.Connection) -> bool:
    columns = {row[1] for row in cx.execute("PRAGMA table_info(price_history)").fetchall()}
    return "asset" in columns and "cid" not in columns and "outcome" not in columns


def _migrate_price_history_table(cx: sqlite3.Connection) -> None:
    columns = cx.execute("PRAGMA table_info(price_history)").fetchall()
    if not columns:
        return

    column_names = {str(row[1]) for row in columns}
    ts_type = next((str(row[2]).upper() for row in columns if row[1] == "ts"), None)
    if _price_history_uses_asset_key(cx) and ts_type == "DATETIME":
        cx.execute("CREATE INDEX IF NOT EXISTS idx_ph_asset ON price_history (asset)")
        return

    uses_asset_key = "asset" in column_names
    if uses_asset_key:
        rows = cx.execute(
            """
            SELECT asset, ts, recorded_at, price
            FROM price_history
            ORDER BY asset ASC, ts ASC
            """
        ).fetchall()
    else:
        rows = cx.execute(
            """
            SELECT ph.cid, ph.outcome, ph.ts, ph.recorded_at, ph.price
            FROM price_history ph
            ORDER BY ph.cid ASC, ph.outcome ASC, ph.ts ASC
            """
        ).fetchall()

    trade_asset_rows = cx.execute(
        """
        SELECT cid, outcome, asset
        FROM trade_history
        WHERE asset IS NOT NULL AND asset != ''
        ORDER BY id ASC
        """
    ).fetchall()

    asset_by_position: dict[tuple[str, str], str] = {}
    for cid, outcome, asset in trade_asset_rows:
        key = (str(cid or ""), str(outcome or ""))
        asset_str = str(asset or "")
        if key[0] and key[1] and asset_str and key not in asset_by_position:
            asset_by_position[key] = asset_str

    cx.execute("DROP TABLE IF EXISTS price_history__asset_new")
    cx.execute(
        """
        CREATE TABLE price_history__asset_new (
            asset       TEXT     NOT NULL,
            ts          DATETIME NOT NULL,
            recorded_at DATETIME NOT NULL,
            price       REAL     NOT NULL,
            PRIMARY KEY (asset, ts)
        )
        """
    )

    migrated_rows: list[tuple[str, str, str, float]] = []
    if uses_asset_key:
        for asset, ts, recorded_at, price in rows:
            asset_str = str(asset or "")
            if not asset_str:
                continue
            migrated_rows.append((
                asset_str,
                _normalize_db_ts(ts),
                _normalize_db_ts(recorded_at),
                float(price),
            ))
    else:
        for cid, outcome, ts, recorded_at, price in rows:
            asset = asset_by_position.get((str(cid or ""), str(outcome or "")), "")
            if asset:
                migrated_rows.append((
                    asset,
                    _normalize_db_ts(ts),
                    _normalize_db_ts(recorded_at),
                    float(price),
                ))

    if migrated_rows:
        cx.executemany(
            "INSERT OR IGNORE INTO price_history__asset_new (asset, ts, recorded_at, price) VALUES (?, ?, ?, ?)",
            migrated_rows,
        )

    cx.execute("DROP TABLE price_history")
    cx.execute("ALTER TABLE price_history__asset_new RENAME TO price_history")
    cx.execute("CREATE INDEX idx_ph_asset ON price_history (asset)")


def _sanitize_signal_payload(payload: object) -> dict[str, object] | None:
    if not isinstance(payload, dict):
        return None
    sanitized = dict(payload)
    sanitized.pop("wallets", None)
    return sanitized


def _migrate_strip_signal_wallets(cx: sqlite3.Connection) -> None:
    rows = cx.execute("SELECT id, data FROM signals").fetchall()
    updates: list[tuple[str, int]] = []
    for row_id, raw_data in rows:
        try:
            decoded = json.loads(str(raw_data))
            if isinstance(decoded, str):
                decoded = json.loads(decoded)
        except Exception:
            continue
        sanitized = _sanitize_signal_payload(decoded)
        if sanitized is None:
            continue
        if sanitized == decoded:
            continue
        updates.append((json.dumps(sanitized, default=str), int(row_id)))
    if updates:
        cx.executemany("UPDATE signals SET data = ? WHERE id = ?", updates)


def _ts_column_type(cx: sqlite3.Connection, table_name: str) -> str | None:
    rows = cx.execute(f"PRAGMA table_info({table_name})").fetchall()
    for row in rows:
        if row[1] == "ts":
            return str(row[2]).upper() or None
    return None


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
    rows: list[tuple[str, str, str]] = []
    for signal in signals:
        sanitized = _sanitize_signal_payload(signal)
        if sanitized is None:
            continue
        rows.append((now, now, json.dumps(sanitized, default=str)))
    with _connect() as cx:
        cx.executemany("INSERT INTO signals (recorded_at, ts, data) VALUES (?, ?, ?)", rows)


def save_rejects(rejects: list[str], ts: float) -> None:
    if not _DB_PATH:
        return
    now = _ts_to_dt(ts)
    with _connect() as cx:
        cx.executemany(
            "INSERT INTO rejects (recorded_at, ts, reason) VALUES (?, ?, ?)",
            [(now, now, r) for r in rejects],
        )


def _decode_signal_row_payload(data: str) -> "SignalDict | None":
    import json

    decoded = json.loads(data)
    if isinstance(decoded, dict):
        sanitized = _sanitize_signal_payload(decoded)
        if sanitized is None:
            return None
        return cast("SignalDict", sanitized)

    if isinstance(decoded, str):
        decoded_text = decoded.strip()
        if decoded_text.startswith("{"):
            nested = json.loads(decoded_text)
            if isinstance(nested, dict):
                sanitized = _sanitize_signal_payload(nested)
                if sanitized is None:
                    return None
                return cast("SignalDict", sanitized)
        return None

    return None


def load_latest_signals(limit: int = 200) -> list["SignalDict"]:
    if not _DB_PATH:
        return []
    with _connect() as cx:
        latest_ts_row = cx.execute("SELECT MAX(ts) FROM signals").fetchone()
        latest_ts = latest_ts_row[0] if latest_ts_row else None
        if latest_ts is None:
            return []
        rows = cx.execute(
            "SELECT data FROM signals WHERE ts = ? ORDER BY id ASC LIMIT ?",
            (latest_ts, limit),
        ).fetchall()
    signals: list["SignalDict"] = []
    for row in rows:
        signal_data = _decode_signal_row_payload(str(row[0]))
        if signal_data is not None:
            signals.append(signal_data)
    return signals


def load_signal_history(limit: int = 200, min_score: float = 0.0, cid: str | None = None) -> list["SignalDict"]:
    if not _DB_PATH:
        return []
    query = (
        "SELECT ts, data FROM signals "
        "ORDER BY id DESC LIMIT ?"
    )
    with _connect() as cx:
        rows = cx.execute(query, (limit,)).fetchall()

    out: list["SignalDict"] = []
    for snapshot_ts, data in reversed(rows):
        typed_sig = _decode_signal_row_payload(str(data))
        if typed_sig is None:
            continue
        if typed_sig.get("score", 0) < min_score:
            continue
        if cid and typed_sig.get("cid") != cid:
            continue
        typed_sig["snapshot_ts"] = _dt_to_ts(str(snapshot_ts))
        out.append(typed_sig)
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
    "cid", "asset", "type", "title", "slug", "event_slug", "outcome", "price",
    "shares", "bet", "pnl_usdc", "pnl_pct", "reason", "ts",
    "bankroll", "tier", "strategy", "stop_loss_pct", "avg_entry",
    "score", "n_confluence", "is_conviction", "market_url",
)


def _trade_to_row(trade: TradeRecord) -> tuple:
    ts_raw = trade.ts
    ts = _ts_to_dt(float(ts_raw)) if ts_raw is not None else _ts_to_dt(0.0)
    return (
        trade.cid,
        trade.asset,
        trade.type,
        trade.title,
        trade.slug,
        trade.event_slug,
        trade.outcome,
        trade.price,
        trade.shares,
        trade.bet,
        trade.pnl_usdc,
        trade.pnl_pct,
        trade.reason,
        ts,
        trade.bankroll,
        trade.tier,
        trade.strategy,
        trade.stop_loss_pct,
        trade.avg_entry,
        trade.score,
        trade.n_confluence,
        int(trade.is_conviction),
        trade.market_url,
    )


def _row_to_trade(row: sqlite3.Row, wallets: list[dict], audits: list[dict]) -> "TradeRecord":
    row_data = dict(row)

    def _as_str(value: object) -> str:
        return "" if value is None else str(value)

    def _as_float(value: object) -> float:
        if value is None:
            return 0.0
        if isinstance(value, (int, float, str)):
            return float(value)
        raise TypeError(f"Expected float-compatible value, got {type(value).__name__}")

    def _as_int(value: object) -> int:
        if value is None:
            return 0
        if isinstance(value, (int, str)):
            return int(value)
        raise TypeError(f"Expected int-compatible value, got {type(value).__name__}")

    ts_value = row_data.get("ts")
    elite_wallets = [str(wallet["wallet"]) for wallet in wallets]
    whale_names = [str(wallet["name"]) for wallet in wallets if wallet.get("name")]
    whale_buy_cash = {
        str(wallet["wallet"]): float(wallet["cash"])
        for wallet in wallets
        if wallet.get("cash") is not None
    }

    trade = TradeRecord(
        cid=_as_str(row_data.get("cid")),
        asset=_as_str(row_data.get("asset")),
        type=_as_str(row_data.get("type")),
        title=_as_str(row_data.get("title")),
        slug=_as_str(row_data.get("slug")),
        event_slug=_as_str(row_data.get("event_slug")),
        outcome=_as_str(row_data.get("outcome")),
        price=_as_float(
            row_data.get("price")
            if row_data.get("price") is not None
            else (row_data.get("exit_price") if _as_str(row_data.get("type")) == "SELL" else row_data.get("entry_price"))
        ),
        shares=_as_float(row_data.get("shares")),
        bet=_as_float(row_data.get("bet")),
        ts=_dt_to_ts(str(ts_value)) if ts_value else 0.0,
        bankroll=_as_float(row_data.get("bankroll")),
        tier=_as_str(row_data.get("tier")),
        strategy=_as_str(row_data.get("strategy")),
        score=_as_float(row_data.get("score")),
        n_confluence=_as_int(row_data.get("n_confluence")),
        is_conviction=bool(row_data.get("is_conviction")),
        market_url=_as_str(row_data.get("market_url")),
        elite_wallets=elite_wallets,
        whale_names=whale_names,
        whale_buy_cash=whale_buy_cash,
    )

    if row_data.get("pnl_usdc") is not None:
        trade.pnl_usdc = float(row_data["pnl_usdc"])
    if row_data.get("pnl_pct") is not None:
        trade.pnl_pct = float(row_data["pnl_pct"])
    if row_data.get("reason") is not None:
        trade.reason = str(row_data["reason"])
    if row_data.get("stop_loss_pct") is not None:
        trade.stop_loss_pct = float(row_data["stop_loss_pct"])
    if row_data.get("avg_entry") is not None:
        trade.avg_entry = float(row_data["avg_entry"])

    for audit in audits:
        audit_type = str(audit["audit_type"])
        audit_data = json.loads(str(audit["data"]))
        if audit_type in {"audit", "entry_audit", "exit_audit"}:
            trade.audit = audit_data

    return trade


def append_trade(trade: TradeRecord) -> None:
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

        wallets = trade.elite_wallets
        names = trade.whale_names
        cash_map = trade.whale_buy_cash
        wallet_rows = [
            (trade_id, w, names[i] if i < len(names) else None, cash_map.get(w))
            for i, w in enumerate(wallets)
        ]
        if wallet_rows:
            cx.executemany(
                "INSERT INTO trade_history_wallets (trade_id, wallet, name, cash) VALUES (?, ?, ?, ?)",
                wallet_rows,
            )

        if trade.audit:
            cx.execute(
                "INSERT INTO trade_history_audit (trade_id, audit_type, data) VALUES (?, ?, ?)",
                (trade_id, "audit", json.dumps(trade.audit, default=str)),
            )


def bulk_insert_trades(trades: list[TradeRecord]) -> None:
    if not trades or not _DB_PATH:
        return
    for trade in trades:
        append_trade(trade)


def load_trade_history(limit: int = 5000) -> list["TradeRecord"]:
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


def upsert_trade_stats(stats: "TradeStats") -> None:
    if not _DB_PATH:
        return
    now = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with _connect() as cx:
        cx.execute(
            """INSERT INTO trade_stats (id, sell_count, win_count, loss_count,
               sum_pnl, sum_wins, sum_losses, best, worst, updated_at)
               VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
               sell_count=excluded.sell_count, win_count=excluded.win_count,
               loss_count=excluded.loss_count, sum_pnl=excluded.sum_pnl,
               sum_wins=excluded.sum_wins, sum_losses=excluded.sum_losses,
               best=excluded.best, worst=excluded.worst, updated_at=excluded.updated_at""",
            (stats.sell_count, stats.win_count, stats.loss_count,
             stats.sum_pnl, stats.sum_wins, stats.sum_losses,
             stats.best, stats.worst, now),
        )


def load_trade_stats() -> "TradeStats | None":
    """Return a TradeStats populated from DB, or None if no row exists."""
    if not _DB_PATH:
        return None
    with _connect() as cx:
        row = cx.execute(
            "SELECT sell_count, win_count, loss_count, sum_pnl, sum_wins, sum_losses, best, worst "
            "FROM trade_stats WHERE id = 1"
        ).fetchone()
    if row is None:
        return None
    from titan_state import TradeStats
    s = TradeStats()
    s.sell_count, s.win_count, s.loss_count = int(row[0]), int(row[1]), int(row[2])
    s.sum_pnl, s.sum_wins, s.sum_losses = float(row[3]), float(row[4]), float(row[5])
    s.best, s.worst = float(row[6]), float(row[7])
    return s


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
