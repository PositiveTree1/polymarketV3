from __future__ import annotations

"""
TITAN — SQLite layer for time-series data (price_history, equity_history, watchlist).
Keeps titan_state.json lean by offloading high-cardinality data here.
"""

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Iterator
if TYPE_CHECKING:
    from titan_state import TradeStats
    from titan_signals import Signal
    from titan_trade import TradeRecord
    from titan_market import Market
    from titan_wallet import Wallet, RawTrade, TradeClosure


@dataclass
class WalletTradeRow:
    id:             int
    wallet:         str
    condition_id:   str
    asset:          str
    side:           str
    outcome:        str
    title:          str
    slug:           str
    event_slug:     str
    entry_ts:       float
    entry_price:    float
    entry_size:     float
    entry_cash:     float
    source:         str
    status:         str
    close_ts:       float | None
    close_price:    float | None
    close_cash:     float | None
    redeem_value:   float | None
    realised_pnl:   float | None
    hold_minutes:   float | None
    fee_estimate:   float | None
    close_type:     str | None
    close_source:   str | None
    cur_price:      float | None
    cash_pnl:       float | None
    redeemable:     bool

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
                data        TEXT     NOT NULL,
                live        INTEGER  NOT NULL DEFAULT 0,
                cid         TEXT     NOT NULL DEFAULT ''
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

            CREATE TABLE IF NOT EXISTS markets (
                cid         TEXT     NOT NULL PRIMARY KEY,
                updated_at  DATETIME NOT NULL,
                data        TEXT     NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_markets_updated_at ON markets (updated_at);

            CREATE TABLE IF NOT EXISTS wallet_trades_meta (
                wallet              TEXT NOT NULL PRIMARY KEY,
                backfill_oldest_ts  REAL,
                refresh_ok_until_ts REAL,
                updated_at          DATETIME NOT NULL
            );

            CREATE TABLE IF NOT EXISTS wallet_trades (
                id             INTEGER  PRIMARY KEY AUTOINCREMENT,
                wallet         TEXT     NOT NULL,
                condition_id   TEXT     NOT NULL,
                asset          TEXT     NOT NULL,
                side           TEXT     NOT NULL,
                outcome        TEXT,
                title          TEXT,
                entry_ts       DATETIME NOT NULL,
                entry_price    REAL     NOT NULL,
                entry_size     REAL     NOT NULL,
                entry_cash     REAL     NOT NULL,
                source         TEXT     NOT NULL,
                status         TEXT     NOT NULL,
                close_ts       DATETIME,
                close_price    REAL,
                close_cash     REAL,
                redeem_value   REAL,
                realised_pnl   REAL,
                hold_minutes   REAL,
                fee_estimate   REAL,
                close_type     TEXT,
                close_source   TEXT,
                UNIQUE(wallet, condition_id, asset, side, entry_ts)
            );
            CREATE INDEX IF NOT EXISTS idx_wt_wallet_entry_ts   ON wallet_trades (wallet, entry_ts DESC);
            CREATE INDEX IF NOT EXISTS idx_wt_wallet_status_ts  ON wallet_trades (wallet, status, entry_ts DESC);
            CREATE INDEX IF NOT EXISTS idx_wt_wallet_close_ts   ON wallet_trades (wallet, close_ts DESC);
        """)
        _migrate_ts_columns(cx)
        # Add removed column to existing DBs that predate it
        cols = [r[1] for r in cx.execute("PRAGMA table_info(signals)").fetchall()]
        if "live" not in cols:
            cx.execute("ALTER TABLE signals ADD COLUMN live INTEGER NOT NULL DEFAULT 0")
        if "cid" not in cols:
            cx.execute("ALTER TABLE signals ADD COLUMN cid TEXT NOT NULL DEFAULT ''")
        _migrate_add_columns(cx)
        _migrate_price_history_table(cx)
        scanned_rows, updated_rows = _migrate_strip_signal_embedded_market_data(cx)
        deleted = _compact_price_history(cx)
    if updated_rows:
        cleanup_line = f"Signal cleanup: scanned={scanned_rows} updated={updated_rows}"
        try:
            import titan_state as S
            S._log(cleanup_line, "INFO")
        except Exception:
            pass


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
    wl_cols = {row[1] for row in cx.execute("PRAGMA table_info(watchlist)").fetchall()}
    if "watchable" not in wl_cols:
        cx.execute("ALTER TABLE watchlist ADD COLUMN watchable INTEGER NOT NULL DEFAULT 1")
    if "profile_json" not in wl_cols:
        cx.execute("ALTER TABLE watchlist ADD COLUMN profile_json TEXT")
    if "status" not in wl_cols:
        cx.execute("ALTER TABLE watchlist ADD COLUMN status INTEGER NOT NULL DEFAULT 1")
        # Backfill from existing data: elite=3, verified=2, watchable=1, else=0
        cx.execute("""
            UPDATE watchlist SET status = CASE
                WHEN json_extract(profile_json, '$.status') IS NOT NULL
                    THEN CAST(json_extract(profile_json, '$.status') AS INTEGER)
                WHEN json_extract(profile_json, '$.elite') = 1 THEN 3
                WHEN json_extract(profile_json, '$.verified') = 1 THEN 2
                WHEN watchable = 1 THEN 1
                ELSE 0
            END
        """)

    meta_cols = {row[1] for row in cx.execute("PRAGMA table_info(wallet_trades_meta)").fetchall()}
    if "refresh_ok_until_ts" not in meta_cols:
        cx.execute("ALTER TABLE wallet_trades_meta ADD COLUMN refresh_ok_until_ts REAL")
        # Migrate: wallets that had backfill_done=1 get refresh_ok_until_ts = updated_at epoch
        # so they don't re-backfill, but a server gap will force a re-sweep on next poll
        if "backfill_done" in meta_cols:
            cx.execute("""
                UPDATE wallet_trades_meta
                SET refresh_ok_until_ts = CAST(strftime('%s', updated_at) AS REAL)
                WHERE backfill_done = 1
            """)

    wt_cols = {row[1] for row in cx.execute("PRAGMA table_info(wallet_trades)").fetchall()}
    if "slug" not in wt_cols:
        cx.execute("ALTER TABLE wallet_trades ADD COLUMN slug TEXT NOT NULL DEFAULT ''")
    if "event_slug" not in wt_cols:
        cx.execute("ALTER TABLE wallet_trades ADD COLUMN event_slug TEXT NOT NULL DEFAULT ''")
    if "cur_price" not in wt_cols:
        cx.execute("ALTER TABLE wallet_trades ADD COLUMN cur_price REAL")
    if "cash_pnl" not in wt_cols:
        cx.execute("ALTER TABLE wallet_trades ADD COLUMN cash_pnl REAL")
    if "redeemable" not in wt_cols:
        cx.execute("ALTER TABLE wallet_trades ADD COLUMN redeemable INTEGER")

    meta_cols2 = {row[1] for row in cx.execute("PRAGMA table_info(wallet_trades_meta)").fetchall()}
    if "pos_ts" not in meta_cols2:
        cx.execute("ALTER TABLE wallet_trades_meta ADD COLUMN pos_ts REAL")
    if "pos_n" not in meta_cols2:
        cx.execute("ALTER TABLE wallet_trades_meta ADD COLUMN pos_n INTEGER")
    if "pos_init" not in meta_cols2:
        cx.execute("ALTER TABLE wallet_trades_meta ADD COLUMN pos_init REAL")
    if "pos_cur" not in meta_cols2:
        cx.execute("ALTER TABLE wallet_trades_meta ADD COLUMN pos_cur REAL")
    if "pos_cash_pnl" not in meta_cols2:
        cx.execute("ALTER TABLE wallet_trades_meta ADD COLUMN pos_cash_pnl REAL")

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


def _compact_price_history(cx: sqlite3.Connection) -> int:
    """Delete middle points of price plateaux — keep only first and last ts per contiguous run."""
    cur = cx.execute("""
        DELETE FROM price_history
        WHERE (asset, ts) IN (
            SELECT asset, ts FROM (
                SELECT asset, ts,
                    LAG(price)  OVER (PARTITION BY asset ORDER BY ts) AS prev_price,
                    LEAD(price) OVER (PARTITION BY asset ORDER BY ts) AS next_price,
                    price
                FROM price_history
            )
            WHERE prev_price = price AND next_price = price
        )
    """)
    return cur.rowcount


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
    sanitized.pop("mkt", None)
    sanitized.pop("title", None)
    sanitized.pop("slug", None)
    sanitized.pop("event_slug", None)
    sanitized.pop("mkt_type", None)
    sanitized.pop("is_sports", None)
    sanitized.pop("price_history", None)
    sanitized["ver"] = _sanitize_signal_observation_map(payload.get("ver"))
    sanitized["elite_ver"] = _sanitize_signal_observation_map(payload.get("elite_ver"))
    return sanitized


def _migrate_strip_signal_embedded_market_data(cx: sqlite3.Connection) -> tuple[int, int]:
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
    return len(rows), len(updates)


def _sanitize_signal_observation_map(value: object) -> dict[str, dict[str, object]]:
    if not isinstance(value, dict):
        return {}
    sanitized: dict[str, dict[str, object]] = {}
    for wallet_raw, observation_raw in value.items():
        wallet = str(wallet_raw or "")
        if not wallet or not isinstance(observation_raw, dict):
            continue
        sanitized[wallet] = _sanitize_signal_observation_payload(observation_raw, wallet)
    return sanitized


def _f(v: object) -> float:
    return float(v) if isinstance(v, (int, float)) else 0.0


def _sanitize_signal_observation_payload(
    payload: dict[str, object],
    wallet: str,
) -> dict[str, object]:
    hft = payload.get("hft_spike_ratio")
    return {
        "wallet": wallet,
        "name": str(payload.get("name") or ""),
        "asset": str(payload.get("asset") or ""),
        "price": _f(payload.get("price")),
        "size": _f(payload.get("size")),
        "cash": _f(payload.get("cash")),
        "ts": _f(payload.get("ts")),
        "window": str(payload.get("window") or ""),
        "source": str(payload.get("source") or ""),
        "is_elite": bool(payload.get("is_elite")),
        "is_large_trade": bool(payload.get("is_large_trade")),
        "hft_spike_ratio": float(hft) if isinstance(hft, (int, float)) else None,
    }


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


def upsert_wallet_profile(addr: str, wallet: "Wallet") -> None:
    if not _DB_PATH:
        return
    now = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    status_val = int(wallet.status)
    watchable  = 1 if wallet.is_active else 0
    blob       = json.dumps(wallet.to_db_dict())
    with _connect() as cx:
        cx.execute(
            """
            INSERT INTO watchlist (address, added_at, watchable, status, profile_json)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(address) DO UPDATE SET
                watchable    = excluded.watchable,
                status       = excluded.status,
                profile_json = excluded.profile_json
            """,
            (addr.lower(), now, watchable, status_val, blob),
        )


def load_watchable_wallets() -> dict[str, dict | None]:
    """Return all status>=WATCH(1) addresses, ordered by score DESC.
    Value is a raw dict if profile_json exists, else None. Caller uses Wallet.from_db() to hydrate.
    """
    if not _DB_PATH:
        return {}
    with _connect() as cx:
        rows = cx.execute(
            """
            SELECT address, profile_json
            FROM watchlist
            WHERE status >= 1
            ORDER BY COALESCE(json_extract(profile_json, '$.score'), 0) DESC
            """,
        ).fetchall()
    result: dict[str, dict | None] = {}
    for addr, blob in rows:
        if blob:
            try:
                result[addr] = json.loads(blob)
            except Exception:
                result[addr] = None
        else:
            result[addr] = None
    return result


def clear_wallet_profile(addr: str) -> None:
    """Remove profile_json and mark status=REJECTED(0) for a wallet that failed verification."""
    if not _DB_PATH:
        return
    with _connect() as cx:
        cx.execute(
            "UPDATE watchlist SET watchable=0, status=0, profile_json=NULL WHERE address=?",
            (addr.lower(),),
        )


def purge_non_watchable(keep_seed: set[str] | None = None) -> int:
    """
    Delete watchlist rows where watchable=0 and no profile_json.
    Also wipe profile_json from watchable=0 rows that have stale profiles (old code wrote everything).
    Returns total rows deleted.
    """
    if not _DB_PATH:
        return 0
    seeds = {a.lower() for a in (keep_seed or set())}
    with _connect() as cx:
        # Clear stale profiles on non-watchable rows (left over from old code)
        cx.execute("UPDATE watchlist SET profile_json=NULL WHERE status=0")
        # Delete stub rows (no profile, not a seed)
        if seeds:
            placeholders = ",".join("?" * len(seeds))
            rows = cx.execute(
                f"DELETE FROM watchlist WHERE status=0 AND profile_json IS NULL AND address NOT IN ({placeholders})",
                list(seeds),
            )
        else:
            rows = cx.execute("DELETE FROM watchlist WHERE status=0 AND profile_json IS NULL")
        return rows.rowcount


def set_watchable(addr: str, flag: bool) -> None:
    if not _DB_PATH:
        return
    now = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    status_val = 1 if flag else 0
    with _connect() as cx:
        cx.execute(
            "INSERT INTO watchlist (address, added_at, watchable, status) VALUES (?, ?, ?, ?) ON CONFLICT(address) DO UPDATE SET watchable=excluded.watchable, status=excluded.status",
            (addr.lower(), now, status_val, status_val),
        )


# ── signals ──────────────────────────────────────────────────────────────────

def save_signals(signals: list["Signal"], ts: float) -> None:
    if not _DB_PATH:
        return
    now = _ts_to_dt(ts)
    rows: list[tuple[str, str, str, str]] = []
    for signal in signals:
        payload = signal.to_json_dict()
        sanitized = _sanitize_signal_payload(payload)
        if sanitized is None:
            continue
        rows.append((now, now, json.dumps(sanitized, default=str), signal.cid))
    with _connect() as cx:
        cx.execute("UPDATE signals SET live = 0 WHERE live = 1")
        cx.executemany("INSERT INTO signals (recorded_at, ts, data, live, cid) VALUES (?, ?, ?, 1, ?)", rows)


def save_rejects(rejects: list[str], ts: float) -> None:
    if not _DB_PATH:
        return
    now = _ts_to_dt(ts)
    with _connect() as cx:
        cx.executemany(
            "INSERT INTO rejects (recorded_at, ts, reason) VALUES (?, ?, ?)",
            [(now, now, r) for r in rejects],
        )


def _decode_signal_row_payload(data: str) -> dict | None:
    decoded = json.loads(data)
    if isinstance(decoded, dict):
        return _sanitize_signal_payload(decoded)
    if isinstance(decoded, str):
        decoded_text = decoded.strip()
        if decoded_text.startswith("{"):
            nested = json.loads(decoded_text)
            if isinstance(nested, dict):
                return _sanitize_signal_payload(nested)
    return None


def _signal_from_row(payload: dict, snapshot_ts: float | None = None) -> "Signal | None":
    from titan_signals import Signal
    try:
        sig = Signal.from_dict(payload)
        if snapshot_ts is not None:
            sig.snapshot_ts = snapshot_ts
        return sig
    except Exception as e:
        import traceback
        try:
            import titan_state as S
            S._log(f"[titan_db] _signal_from_row failed: {e}\n{traceback.format_exc()}", "ERR")
        except Exception:
            pass
        return None


def mark_signals_not_live(cids: list[str]) -> None:
    if not _DB_PATH or not cids:
        return
    with _connect() as cx:
        cx.executemany("UPDATE signals SET live = 0 WHERE live = 1 AND cid = ?", [(cid,) for cid in cids])


def load_latest_signals(limit: int = 200) -> list["Signal"]:
    if not _DB_PATH:
        return []
    with _connect() as cx:
        rows = cx.execute(
            "SELECT data FROM signals WHERE live = 1 ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    signals: list[Signal] = []
    for row in rows:
        payload = _decode_signal_row_payload(str(row[0]))
        if payload is not None:
            sig = _signal_from_row(payload)
            if sig is not None:
                signals.append(sig)
    return signals


def load_signal_history(limit: int = 200, min_score: float = 0.0, cid: str | None = None) -> list["Signal"]:
    if not _DB_PATH:
        return []
    with _connect() as cx:
        rows = cx.execute("SELECT ts, data FROM signals ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    out: list[Signal] = []
    for snapshot_ts_raw, data in reversed(rows):
        payload = _decode_signal_row_payload(str(data))
        if payload is None:
            continue
        if _f(payload.get("score")) < min_score:
            continue
        if cid and payload.get("cid") != cid:
            continue
        sig = _signal_from_row(payload, snapshot_ts=_dt_to_ts(str(snapshot_ts_raw)))
        if sig is not None:
            out.append(sig)
    return out


def _market_to_payload(market: object) -> dict[str, object]:
    return {
        "yes_price": float(getattr(market, "yes_price")),
        "no_price": float(getattr(market, "no_price")),
        "outcome_labels": list(getattr(market, "outcome_labels")),
        "outcome_prices": dict(getattr(market, "outcome_prices")),
        "token_index": dict(getattr(market, "token_index")),
        "index_to_price": dict(getattr(market, "index_to_price")),
        "asset_to_price": dict(getattr(market, "asset_to_price")),
        "asset_to_index": dict(getattr(market, "asset_to_index")),
        "liq": float(getattr(market, "liq")),
        "volume": float(getattr(market, "volume")),
        "title": str(getattr(market, "title") or ""),
        "end_date": str(getattr(market, "end_date") or ""),
        "hrs_left": getattr(market, "hrs_left"),
        "slug": str(getattr(market, "slug") or ""),
        "event_slug": str(getattr(market, "event_slug") or ""),
        "mkt_type": str(getattr(market, "mkt_type") or ""),
        "is_sports": bool(getattr(market, "is_sports")),
        "ts": float(getattr(market, "ts")),
    }


def _market_from_payload(payload: dict[str, object]) -> "Market":
    from titan_market import Market

    hrs_left_raw = payload.get("hrs_left")
    hrs_left: float | None = float(hrs_left_raw) if isinstance(hrs_left_raw, (int, float)) else None

    outcome_labels_raw = payload.get("outcome_labels")
    outcome_labels = [str(item) for item in outcome_labels_raw] if isinstance(outcome_labels_raw, list) else []

    def _string_float_map(value: object) -> dict[str, float]:
        if not isinstance(value, dict):
            return {}
        return {str(k): float(v) if isinstance(v, (int, float)) else 0.0 for k, v in value.items()}

    def _string_int_map(value: object) -> dict[str, int]:
        if not isinstance(value, dict):
            return {}
        return {str(k): int(v) if isinstance(v, (int, float)) else 0 for k, v in value.items()}

    def _int_float_map(value: object) -> dict[int, float]:
        if not isinstance(value, dict):
            return {}
        return {int(k) if isinstance(k, (int, float, str)) else 0: float(v) if isinstance(v, (int, float)) else 0.0 for k, v in value.items()}

    return Market(
        yes_price=_f(payload.get("yes_price")) or 0.5,
        no_price=_f(payload.get("no_price")) or 0.5,
        outcome_labels=outcome_labels,
        outcome_prices=_string_float_map(payload.get("outcome_prices")),
        token_index=_string_int_map(payload.get("token_index")),
        index_to_price=_int_float_map(payload.get("index_to_price")),
        asset_to_price=_string_float_map(payload.get("asset_to_price")),
        asset_to_index=_string_int_map(payload.get("asset_to_index")),
        liq=_f(payload.get("liq")),
        volume=_f(payload.get("volume")),
        title=str(payload.get("title") or ""),
        end_date=str(payload.get("end_date") or ""),
        hrs_left=hrs_left,
        slug=str(payload.get("slug") or ""),
        event_slug=str(payload.get("event_slug") or ""),
        mkt_type=str(payload.get("mkt_type") or ""),
        is_sports=bool(payload.get("is_sports")),
        ts=_f(payload.get("ts")),
    )



def upsert_market(cid: str, market: object) -> None:
    if not _DB_PATH or not cid:
        return
    now = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    payload = json.dumps(_market_to_payload(market), separators=(",", ":"), default=str)
    with _connect() as cx:
        cx.execute(
            """
            INSERT INTO markets (cid, updated_at, data)
            VALUES (?, ?, ?)
            ON CONFLICT(cid) DO UPDATE SET
                updated_at=excluded.updated_at,
                data=excluded.data
            """,
            (cid, now, payload),
        )


def load_markets() -> dict[str, "Market"]:
    if not _DB_PATH:
        return {}
    with _connect() as cx:
        rows = cx.execute("SELECT cid, data FROM markets ORDER BY cid ASC").fetchall()
    markets: dict[str, "Market"] = {}
    for cid_raw, data_raw in rows:
        cid = str(cid_raw or "")
        if not cid:
            continue
        try:
            payload = json.loads(str(data_raw))
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        try:
            markets[cid] = _market_from_payload(payload)
        except Exception:
            continue
    return markets


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
    from titan_trade import TradeRecord

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
    wallet_names = [str(wallet["name"]) for wallet in wallets if wallet.get("name")]
    wallet_buy_cash = {
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
        wallet_names=wallet_names,
        wallet_buy_cash=wallet_buy_cash,
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
        names = trade.wallet_names
        cash_map = trade.wallet_buy_cash
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


# ── wallet_trades ─────────────────────────────────────────────────────────────

def upsert_wallet_trades(wallet: str, trades: "list[RawTrade]") -> int:
    if not _DB_PATH or not trades:
        return 0
    rows = [
        (
            wallet.lower(),
            t.condition_id,
            t.asset,
            t.side,
            t.outcome,
            t.title,
            t.slug,
            t.event_slug,
            _ts_to_dt(t.timestamp),
            t.price,
            t.size,
            t.cash,
            t.source,
            "OPEN",
        )
        for t in trades
    ]
    with _connect() as cx:
        cur = cx.executemany(
            """
            INSERT OR IGNORE INTO wallet_trades
                (wallet, condition_id, asset, side, outcome, title, slug, event_slug,
                 entry_ts, entry_price, entry_size, entry_cash, source, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        return cur.rowcount


def get_wallet_last_trade_ts(wallet: str) -> float | None:
    if not _DB_PATH:
        return None
    with _connect() as cx:
        row = cx.execute(
            "SELECT entry_ts FROM wallet_trades WHERE wallet=? ORDER BY entry_ts DESC LIMIT 1",
            (wallet.lower(),),
        ).fetchone()
    if row is None:
        return None
    return _dt_to_ts(row[0])


def get_wallet_fetch_state(wallet: str) -> tuple[float | None, float | None]:
    """Return (backfill_oldest_ts, refresh_ok_until_ts).
    backfill_oldest_ts:  oldest entry_ts stored so far (None = never started).
    refresh_ok_until_ts: unix ts of last successful full refresh (None = never done).
                         If now - refresh_ok_until_ts is large the server was down and
                         the next poll must do a full paginated sweep to cover the gap.
    """
    if not _DB_PATH:
        return None, None
    with _connect() as cx:
        row = cx.execute(
            "SELECT backfill_oldest_ts, refresh_ok_until_ts FROM wallet_trades_meta WHERE wallet=?",
            (wallet.lower(),),
        ).fetchone()
    if row is None:
        return None, None
    return row[0], row[1]


def update_wallet_fetch_state(
    wallet: str,
    oldest_ts: float | None,
    refresh_ok_until_ts: float | None,
) -> None:
    if not _DB_PATH:
        return
    now = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with _connect() as cx:
        cx.execute(
            """
            INSERT INTO wallet_trades_meta (wallet, backfill_oldest_ts, refresh_ok_until_ts, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(wallet) DO UPDATE SET
                backfill_oldest_ts  = COALESCE(excluded.backfill_oldest_ts, backfill_oldest_ts),
                refresh_ok_until_ts = COALESCE(excluded.refresh_ok_until_ts, refresh_ok_until_ts),
                updated_at          = excluded.updated_at
            """,
            (wallet.lower(), oldest_ts, refresh_ok_until_ts, now),
        )


def get_wallet_last_activity_ts(wallet: str) -> float | None:
    if not _DB_PATH:
        return None
    with _connect() as cx:
        row = cx.execute(
            "SELECT close_ts FROM wallet_trades WHERE wallet=? AND close_ts IS NOT NULL ORDER BY close_ts DESC LIMIT 1",
            (wallet.lower(),),
        ).fetchone()
    if row is None:
        return None
    return _dt_to_ts(row[0])


def get_wallet_trade_count(wallet: str) -> int:
    if not _DB_PATH:
        return 0
    with _connect() as cx:
        row = cx.execute(
            "SELECT COUNT(*) FROM wallet_trades WHERE wallet=?",
            (wallet.lower(),),
        ).fetchone()
    return int(row[0]) if row else 0


def get_wallet_resolved_trade_count(wallet: str) -> int:
    if not _DB_PATH:
        return 0
    with _connect() as cx:
        row = cx.execute(
            "SELECT COUNT(*) FROM wallet_trades WHERE wallet=? AND status IN ('REDEEMED','SOLD')",
            (wallet.lower(),),
        ).fetchone()
    return int(row[0]) if row else 0


@dataclass
class RealisedPoint:
    close_ts:     float
    realised_pnl: float




def get_wallet_realised_pnl(wallet: str) -> float:
    if not _DB_PATH:
        return 0.0
    with _connect() as cx:
        row = cx.execute(
            "SELECT COALESCE(SUM(realised_pnl), 0.0) FROM wallet_trades WHERE wallet=? AND realised_pnl IS NOT NULL",
            (wallet.lower(),),
        ).fetchone()
    return float(row[0]) if row else 0.0


def apply_wallet_trade_closures(wallet: str, closures: "list[TradeClosure]") -> int:
    if not _DB_PATH or not closures:
        return 0
    updated = 0
    missed = 0
    with _connect() as cx:
        for c in closures:
            close_dt = _ts_to_dt(c.close_ts)
            hold_minutes: float | None = None
            row = cx.execute(
                """
                SELECT id, entry_ts FROM wallet_trades
                WHERE wallet=? AND condition_id=? AND asset=? AND status='OPEN'
                ORDER BY entry_ts ASC LIMIT 1
                """,
                (wallet.lower(), c.condition_id, c.asset),
            ).fetchone()
            if row is None:
                if c.asset:
                    missed += 1
                    if missed <= 3:
                        sample = cx.execute(
                            "SELECT condition_id, asset, side FROM wallet_trades WHERE wallet=? LIMIT 3",
                            (wallet.lower(),),
                        ).fetchall()
                        try:
                            import titan_state as _S
                            _S._log(
                                f"apply_wallet_trade_closures: no match for cid={c.condition_id!r} asset={c.asset!r} | "
                                f"db sample: {sample}",
                                "DATA",
                            )
                        except Exception:
                            pass
                    continue
                # asset empty on REDEEM rows — fall back to condition_id only match
                row = cx.execute(
                    """
                    SELECT id, entry_ts FROM wallet_trades
                    WHERE wallet=? AND condition_id=? AND status='OPEN'
                    ORDER BY entry_ts ASC LIMIT 1
                    """,
                    (wallet.lower(), c.condition_id),
                ).fetchone()
                if row is None:
                    continue
            trade_id, entry_ts_str = row
            try:
                entry_ts = _dt_to_ts(entry_ts_str)
                hold_minutes = (c.close_ts - entry_ts) / 60.0
            except Exception:
                pass
            cur = cx.execute(
                """
                UPDATE wallet_trades SET
                    status       = ?,
                    close_ts     = ?,
                    close_price  = ?,
                    close_cash   = ?,
                    redeem_value = ?,
                    realised_pnl = ?,
                    hold_minutes = ?,
                    close_type   = ?,
                    close_source = 'activity'
                WHERE id = ?
                """,
                (
                    "REDEEMED" if c.close_type == "REDEEM" else "SOLD",
                    close_dt,
                    c.close_price,
                    c.close_cash,
                    c.close_cash if c.close_type == "REDEEM" else None,
                    c.realised_pnl,
                    hold_minutes,
                    c.close_type,
                    trade_id,
                ),
            )
            updated += cur.rowcount
    return updated


def update_wallet_positions(wallet: str, pos_data: list) -> None:
    """
    Persist a synthetic position snapshot from the /positions API response.
    - Stores aggregate metrics (n, init, cur, cash_pnl) + timestamp in wallet_trades_meta.
    - Updates cur_price, cash_pnl, redeemable on OPEN wallet_trades rows that match.
    """
    if not _DB_PATH or not isinstance(pos_data, list):
        return
    now = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    now_ts = datetime.now(tz=timezone.utc).timestamp()

    pos_n       = len(pos_data)
    pos_init    = sum(float(p.get("initialValue") or 0) for p in pos_data)
    pos_cur     = sum(float(p.get("currentValue") or 0) for p in pos_data)
    pos_cash_pnl = sum(float(p.get("cashPnl") or 0) for p in pos_data)

    with _connect() as cx:
        cx.execute(
            """
            INSERT INTO wallet_trades_meta (wallet, pos_ts, pos_n, pos_init, pos_cur, pos_cash_pnl, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(wallet) DO UPDATE SET
                pos_ts       = excluded.pos_ts,
                pos_n        = excluded.pos_n,
                pos_init     = excluded.pos_init,
                pos_cur      = excluded.pos_cur,
                pos_cash_pnl = excluded.pos_cash_pnl,
                updated_at   = excluded.updated_at
            """,
            (wallet.lower(), now_ts, pos_n, pos_init, pos_cur, pos_cash_pnl, now),
        )
        for p in pos_data:
            cid  = str(p.get("conditionId") or "")
            asset = str(p.get("asset") or "")
            if not cid and not asset:
                continue
            cur_price  = p.get("curPrice")
            cash_pnl   = p.get("cashPnl")
            redeemable = 1 if p.get("redeemable") else 0
            cx.execute(
                """
                UPDATE wallet_trades
                SET cur_price = ?, cash_pnl = ?, redeemable = ?
                WHERE wallet = ? AND condition_id = ? AND asset = ? AND status = 'OPEN'
                """,
                (cur_price, cash_pnl, redeemable, wallet.lower(), cid, asset),
            )


def get_wallet_synthetic_position(wallet: str) -> "tuple[float, int, float, float, float] | None":
    """
    Return (pos_ts, pos_n, pos_init, pos_cur, pos_cash_pnl) from the last stored snapshot.
    Returns None if no snapshot exists yet.
    """
    if not _DB_PATH:
        return None
    with _connect() as cx:
        row = cx.execute(
            "SELECT pos_ts, pos_n, pos_init, pos_cur, pos_cash_pnl FROM wallet_trades_meta WHERE wallet=?",
            (wallet.lower(),),
        ).fetchone()
    if row is None or row[0] is None:
        return None
    return (float(row[0]), int(row[1] or 0), float(row[2] or 0), float(row[3] or 0), float(row[4] or 0))


def load_wallet_trade_rows(wallet: str) -> "list[WalletTradeRow]":
    if not _DB_PATH:
        return []
    with _connect() as cx:
        rows = cx.execute(
            """
            SELECT id, wallet, condition_id, asset, side,
                   COALESCE(outcome, ''), COALESCE(title, ''),
                   COALESCE(slug, ''), COALESCE(event_slug, ''),
                   entry_ts, entry_price, entry_size, entry_cash,
                   source, status,
                   close_ts, close_price, close_cash,
                   redeem_value, realised_pnl, hold_minutes, fee_estimate,
                   close_type, close_source,
                   cur_price, cash_pnl, redeemable
            FROM wallet_trades
            WHERE wallet=?
            ORDER BY entry_ts ASC
            """,
            (wallet.lower(),),
        ).fetchall()
    result: list[WalletTradeRow] = []
    for r in rows:
        (id_, wlt, cid, asset, side, outcome, title, slug, event_slug,
         entry_ts_raw, entry_price, entry_size, entry_cash,
         source, status,
         close_ts_raw, close_price, close_cash,
         redeem_value, realised_pnl, hold_minutes, fee_estimate,
         close_type, close_source,
         cur_price_, cash_pnl_, redeemable_) = r
        result.append(WalletTradeRow(
            id=int(id_),
            wallet=str(wlt),
            condition_id=str(cid),
            asset=str(asset),
            side=str(side),
            outcome=str(outcome),
            title=str(title),
            slug=str(slug),
            event_slug=str(event_slug),
            entry_ts=_dt_to_ts(entry_ts_raw) if isinstance(entry_ts_raw, str) else float(entry_ts_raw or 0),
            entry_price=float(entry_price or 0),
            entry_size=float(entry_size or 0),
            entry_cash=float(entry_cash or 0),
            source=str(source),
            status=str(status),
            close_ts=_dt_to_ts(close_ts_raw) if isinstance(close_ts_raw, str) else (float(close_ts_raw) if close_ts_raw is not None else None),
            close_price=float(close_price) if close_price is not None else None,
            close_cash=float(close_cash) if close_cash is not None else None,
            redeem_value=float(redeem_value) if redeem_value is not None else None,
            realised_pnl=float(realised_pnl) if realised_pnl is not None else None,
            hold_minutes=float(hold_minutes) if hold_minutes is not None else None,
            fee_estimate=float(fee_estimate) if fee_estimate is not None else None,
            close_type=str(close_type) if close_type is not None else None,
            close_source=str(close_source) if close_source is not None else None,
            cur_price=float(cur_price_) if cur_price_ is not None else None,
            cash_pnl=float(cash_pnl_) if cash_pnl_ is not None else None,
            redeemable=bool(redeemable_) if redeemable_ is not None else False,
        ))
    return result
