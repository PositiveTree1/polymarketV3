from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path


def _repo_root() -> Path:
    return Path(__file__).resolve().parent


def _load_db_path() -> Path:
    scripts_dir = _repo_root() / "ScriptsTitan"
    sys.path.insert(0, str(scripts_dir))
    import titan_config as config  # type: ignore[import-not-found]

    return (_repo_root() / config.STATE_DB).resolve()


def _extract_condition_id(audit_payload: object) -> str | None:
    if not isinstance(audit_payload, dict):
        return None

    direct_params = audit_payload.get("params")
    if isinstance(direct_params, dict):
        direct_condition_id = direct_params.get("conditionId")
        if isinstance(direct_condition_id, str) and direct_condition_id.strip():
            return direct_condition_id.strip()

    http_traces = audit_payload.get("http_traces")
    if not isinstance(http_traces, list):
        return None

    for trace in http_traces:
        if not isinstance(trace, dict):
            continue
        params = trace.get("params")
        if not isinstance(params, dict):
            continue
        condition_id = params.get("conditionId")
        if isinstance(condition_id, str) and condition_id.strip():
            return condition_id.strip()

    return None


def backfill_trade_history_cids(db_path: Path) -> tuple[int, int, int]:
    updated_count = 0
    missing_audit_count = 0
    no_condition_id_count = 0

    with sqlite3.connect(str(db_path), timeout=10) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        rows = connection.execute(
            """
            SELECT th.id, tha.data
            FROM trade_history AS th
            LEFT JOIN trade_history_audit AS tha
                ON tha.trade_id = th.id
            WHERE th.cid IS NULL OR th.cid = ''
            ORDER BY th.id ASC
            """
        ).fetchall()

        condition_ids_by_trade_id: dict[int, str | None] = {}
        saw_audit_by_trade_id: dict[int, bool] = {}

        for trade_id_raw, audit_data_raw in rows:
            trade_id = int(trade_id_raw)
            saw_audit_by_trade_id[trade_id] = saw_audit_by_trade_id.get(trade_id, False) or audit_data_raw is not None

            if trade_id in condition_ids_by_trade_id and condition_ids_by_trade_id[trade_id]:
                continue
            if not isinstance(audit_data_raw, str) or not audit_data_raw.strip():
                condition_ids_by_trade_id.setdefault(trade_id, None)
                continue

            try:
                audit_payload = json.loads(audit_data_raw)
            except json.JSONDecodeError:
                condition_ids_by_trade_id.setdefault(trade_id, None)
                continue

            condition_id = _extract_condition_id(audit_payload)
            if condition_id:
                condition_ids_by_trade_id[trade_id] = condition_id
            else:
                condition_ids_by_trade_id.setdefault(trade_id, None)

        update_rows = [
            (condition_id, trade_id)
            for trade_id, condition_id in condition_ids_by_trade_id.items()
            if condition_id
        ]

        if update_rows:
            connection.executemany(
                "UPDATE trade_history SET cid = ? WHERE id = ? AND (cid IS NULL OR cid = '')",
                update_rows,
            )
            updated_count = len(update_rows)

        for trade_id, condition_id in condition_ids_by_trade_id.items():
            if condition_id:
                continue
            if not saw_audit_by_trade_id.get(trade_id, False):
                missing_audit_count += 1
            else:
                no_condition_id_count += 1

        connection.commit()

    return updated_count, missing_audit_count, no_condition_id_count


def main() -> int:
    db_path = _load_db_path()
    if not db_path.exists():
        print(f"DB not found: {db_path}")
        return 1

    updated_count, missing_audit_count, no_condition_id_count = backfill_trade_history_cids(db_path)
    print(f"DB: {db_path}")
    print(f"Updated trade_history.cid rows: {updated_count}")
    print(f"Skipped trades with no audit: {missing_audit_count}")
    print(f"Skipped trades with audit but no conditionId: {no_condition_id_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
