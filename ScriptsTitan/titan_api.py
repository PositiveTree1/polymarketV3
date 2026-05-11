from __future__ import annotations

import time
from collections import defaultdict
from typing import TYPE_CHECKING, Callable, TypedDict

if TYPE_CHECKING:
    from titan_signals import SignalDict
    from titan_types import (
        AlertDict, ErrorDict, PositionBriefDict, WhaleDict,
        PnlSummaryDict, TradeStatsDict, PortfolioOverviewDict, TradeRecordDict,
    )


class EngineStatus(TypedDict):
    running:            bool
    paused:             bool
    cycle_count:        int
    uptime_s:           float | None
    last_cycle_at:      float | None
    open_positions:     int
    watchlist_size:     int
    recent_error_count: int
    auth_enabled:       bool


# ── decorator ─────────────────────────────────────────────────────────────────

def mcp_tool(
    description: str,
    input_schema: dict | None = None,
    annotations: dict | None = None,
):
    def decorator(fn: Callable) -> Callable:
        fn._mcp_tool = {
            "name": fn.__name__,
            "description": description,
            "inputSchema": {"type": "object", "properties": input_schema or {}},
            "annotations": annotations or {},
        }
        return fn
    return decorator


# ── TitanAPI ──────────────────────────────────────────────────────────────────

class TitanAPI:
    def __init__(self, enable_telegram: bool = False) -> None:
        self._running: bool = False
        self._start_time: float | None = None
        self._subscribers: dict[str, list[Callable]] = defaultdict(list)
        self._last_signals: list[SignalDict] = []
        self._last_rejects: list[str] = []
        self._telegram = None
        self._telegram_enabled = False
        if enable_telegram:
            try:
                import titan_telegram as _telegram
                self._telegram = _telegram.TelegramNotifier()
                self._telegram_enabled = True
            except Exception:
                self._telegram = None
                self._telegram_enabled = False

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._running:
            return
        import titan_engine as _engine
        _engine.start(
            log_callback=self._on_log,
            position_open_cb=self._on_position_open,
            position_close_cb=self._on_position_close,
            cycle_cb=self._on_cycle,
            heartbeat_cb=self._on_heartbeat,
        )
        self._running = True
        self._start_time = time.time()
        self._load_persisted_signals_rejects()
        self._send_telegram_boot_status()

    def stop(self) -> None:
        self._running = False

    def status(self) -> dict:
        import titan_state as _TS
        return {
            "running": self._running,
            "cycle_count": _TS.env().cycle_count,
            "uptime_s": (time.time() - self._start_time) if self._start_time else None,
        }

    # ── read-only queries ─────────────────────────────────────────────────────

    @mcp_tool(
        description="Returns engine health and key runtime counters. Call this first to confirm the server is alive.",
        annotations={"readOnlyHint": True, "openWorldHint": False},
    )
    def get_status(self) -> EngineStatus:
        import titan_state as _TS
        env = _TS.env()
        logs = env.SYSTEM_LOGS
        recent_errors = sum(1 for l in logs[-50:] if "ERROR" in l or "CRITICAL" in l)
        last_cycle_at: float | None = getattr(env, "last_cycle_ts", None)
        return {
            "running": self._running,
            "paused": getattr(env, "paused", False),
            "cycle_count": env.cycle_count,
            "uptime_s": round(time.time() - self._start_time, 1) if self._start_time else None,
            "last_cycle_at": last_cycle_at,
            "open_positions": len(env.open_positions),
            "watchlist_size": len(env.watchlist),
            "recent_error_count": recent_errors,
            "auth_enabled": False,
        }

    @mcp_tool(
        description=(
            "Returns open Polymarket positions. "
            "brief=true (default): clean summary fields only. "
            "brief=false: full raw position dict."
        ),
        input_schema={
            "brief": {"type": "boolean", "description": "Return summary fields only (default true)"},
        },
        annotations={"readOnlyHint": True, "openWorldHint": False},
    )
    def get_positions(self, brief: bool = True) -> list[PositionBriefDict]:
        import titan_state as _TS
        positions = []
        for k, v in _TS.env().open_positions.items():
            if brief:
                entry = v.get("entry_price", 0)
                cur   = v.get("cur_price", entry)
                shares = v.get("shares", 0)
                held_min = round((time.time() - v.get("entry_ts", time.time())) / 60, 1)
                pnl_usd = round((cur - entry) * shares, 4)
                pnl_pct = round((cur - entry) / max(entry, 0.001) * 100, 2)
                whales = v.get("elite_names") or [w[:10] for w in v.get("elite_wallets", [])]
                positions.append({
                    "key": str(k),
                    "title": v.get("title", ""),
                    "outcome": v.get("outcome", ""),
                    "strategy": v.get("strategy", ""),
                    "tier": v.get("tier", ""),
                    "entry_price": entry,
                    "current_price": cur,
                    "bet": v.get("bet", 0),
                    "shares": shares,
                    "pnl_pct": pnl_pct,
                    "pnl_usd": pnl_usd,
                    "held_minutes": held_min,
                    "source_whales": whales,
                    "risk_flag": v.get("risk_flag", ""),
                })
            else:
                positions.append({"key": str(k), **v})
        return positions

    @mcp_tool(
        description="Returns closed positions (SELL records) enriched with price_history from DB.",
        input_schema={"limit": {"type": "integer", "description": "Max number of closed positions to return (default 200)"}},
        annotations={"readOnlyHint": True, "openWorldHint": False},
    )
    def get_closed_positions(self, limit: int = 200) -> list[TradeRecordDict]:
        import titan_db as _DB
        sells = [t for t in _DB.load_trade_history(limit=limit) if t.get("type") == "SELL"]
        result = []
        for t in reversed(sells):
            cid = str(t.get("cid") or "")
            outcome = str(t.get("outcome") or "")
            ph = _DB.load_price_history(cid, outcome) if cid and outcome else []
            result.append({
                **t,
                "price_history": ph,
                "price_history_error": "old trade, no cid" if not cid else None,
            })
        return result

    @mcp_tool(
        description="Returns current whale-triggered trading signals with confidence scores.",
        input_schema={"min_score": {"type": "number", "description": "Minimum signal score filter. Typical range 0–100 (integer-like values such as 78 or 81 are normal)."}},
        annotations={"readOnlyHint": True, "openWorldHint": False},
    )
    def get_signals(self, min_score: float = 0.0) -> list[SignalDict]:
        return [s for s in self._last_signals if s.get("score", 0) >= min_score]

    @mcp_tool(
        description=(
            "Returns historical signal records from the SQLite store. "
            "Each row includes the cycle snapshot timestamp plus the full signal payload."
        ),
        input_schema={
            "limit": {"type": "integer", "description": "Max number of historical signal rows to return (default 200)"},
            "min_score": {"type": "number", "description": "Minimum signal score filter."},
            "cid": {"type": "string", "description": "Optional market CID filter."},
        },
        annotations={"readOnlyHint": True, "openWorldHint": False},
    )
    def get_signal_history(self, limit: int = 200, min_score: float = 0.0, cid: str | None = None) -> list[SignalDict]:
        import titan_db as _DB
        return _DB.load_signal_history(limit=limit, min_score=min_score, cid=cid)

    @mcp_tool(
        description="Returns recent signal rejection reasons from the last engine cycles.",
        annotations={"readOnlyHint": True, "openWorldHint": False},
    )
    def get_rejects(self) -> list[str]:
        return list(self._last_rejects)

    @mcp_tool(
        description="Returns recent system alert log entries.",
        annotations={"readOnlyHint": True, "openWorldHint": False},
    )
    def get_alerts(self) -> list[AlertDict]:
        import titan_state as _TS
        logs = _TS.env().SYSTEM_LOGS
        return [{"msg": l} for l in logs if "ALERT" in l or "WARN" in l or "ERR" in l]

    @mcp_tool(
        description="Returns the current elite whale roster with performance metrics.",
        annotations={"readOnlyHint": True, "openWorldHint": False},
    )
    def get_whales(self) -> list[WhaleDict]:
        import titan_state as _TS
        return [
            {"wallet": w, **p}
            for w, p in _TS.env().wallet_cache.items()
        ]

    @mcp_tool(
        description=(
            "Returns bankroll, session P&L, total P&L, and a short equity curve tail (last 50 points). "
            "Also includes cooldown and active market CID lists, and watchlist size."
        ),
        annotations={"readOnlyHint": True, "openWorldHint": False},
    )
    def get_pnl_summary(self) -> PnlSummaryDict:
        import titan_state as _TS
        from titan_config import BANKROLL_START
        env = _TS.env()
        return {
            "bankroll": env.paper_bankroll,
            "bankroll_start": BANKROLL_START,
            "session_pnl": env.session_pnl,
            "total_pnl": env.paper_bankroll - BANKROLL_START,
            "equity_history": env.equity_history[-2000:] if env.equity_history else [],
            "cooldown_cids": dict(env.cooldown_cids),
            "active_market_cids": list(env.active_market_cids),
            "watchlist_size": len(env.watchlist),
        }

    @mcp_tool("Return aggregated trade statistics (win rate, PnL, etc.)")
    def get_trade_stats(self) -> TradeStatsDict:
        import titan_state as _TS
        st = _TS.env().trade_stats
        return {
            "sell_count":  st.sell_count,
            "win_count":   st.win_count,
            "loss_count":  st.loss_count,
            "sum_pnl":     st.sum_pnl,
            "best":        st.best,
            "worst":       st.worst,
            "win_rate":    st.win_rate,
            "avg_win":     st.avg_win,
            "avg_loss":    st.avg_loss,
            "expectancy":  st.expectancy,
        }

    @mcp_tool(
        description=(
            "Returns a single-page portfolio overview: equity, bankroll, session P&L, "
            "open position count, watchlist size, and recent error count. "
            "Best starting point for a quick health check."
        ),
        annotations={"readOnlyHint": True, "openWorldHint": False},
    )
    def get_portfolio_overview(self) -> PortfolioOverviewDict:
        import titan_state as _TS
        from titan_config import BANKROLL_START
        env = _TS.env()
        open_value = sum(
            pos.get("cur_price", pos.get("entry_price", 0)) * pos.get("shares", 0)
            for pos in env.open_positions.values()
        )
        recent_errors = sum(1 for l in env.SYSTEM_LOGS[-50:] if "ERROR" in l or "CRITICAL" in l)
        return {
            "running": self._running,
            "bankroll": round(env.paper_bankroll, 4),
            "open_value": round(open_value, 4),
            "total_equity": round(env.paper_bankroll + open_value, 4),
            "session_pnl": round(env.session_pnl, 4),
            "total_pnl": round(env.paper_bankroll - BANKROLL_START, 4),
            "open_positions": len(env.open_positions),
            "watchlist_size": len(env.watchlist),
            "cycle_count": env.cycle_count,
            "recent_error_count": recent_errors,
        }

    @mcp_tool(
        description="Returns recent ERROR and CRITICAL log entries as structured events.",
        input_schema={"limit": {"type": "integer", "description": "Max entries to return (default 20)"}},
        annotations={"readOnlyHint": True, "openWorldHint": False},
    )
    def get_recent_errors(self, limit: int = 20) -> list[ErrorDict]:
        import titan_state as _TS
        result = []
        for line in reversed(_TS.env().SYSTEM_LOGS):
            if "ERROR" in line or "CRITICAL" in line:
                result.append({"message": line})
                if len(result) >= limit:
                    break
        return result

    @mcp_tool(
        description=(
            "Execute a read-only SELECT query against titan_state.db. "
            "Only SELECT statements are accepted — any other statement raises an error. "
            "The schema is injected at runtime via get_db_schema. "
            "Use get_db_schema first to discover available tables and columns before querying."
        ),
        input_schema={
            "sql": {"type": "string", "description": "A SELECT SQL statement to run against titan_state.db"},
        },
        annotations={"readOnlyHint": True, "openWorldHint": False},
    )
    def query_db(self, sql: str) -> list[dict]:
        import titan_db as _DB
        return _DB.query_db(sql)

    @mcp_tool(
        description=(
            "Returns the current titan_state.db schema as a compact string. "
            "Call this before query_db to discover available tables and their columns. "
            "The schema is built dynamically from sqlite_master so it always reflects the live DB."
        ),
        annotations={"readOnlyHint": True, "openWorldHint": False},
    )
    def get_db_schema(self) -> str:
        import titan_db as _DB
        return _DB.get_schema_description()

    @mcp_tool(
        description="Returns the full trade history (buys and sells).",
        annotations={"readOnlyHint": True, "openWorldHint": False},
    )
    def get_trade_history(self) -> list[TradeRecordDict]:
        import titan_db as _DB
        return _DB.load_trade_history()

    @mcp_tool(
        description="Returns the current live engine configuration.",
        annotations={"readOnlyHint": True, "openWorldHint": False},
    )
    def get_config(self) -> dict:
        import titan_config as _C
        import json, os
        try:
            with open(_C.get_config_file(), encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    @mcp_tool(
        description="Returns recent engine log lines.",
        input_schema={"lines": {"type": "integer", "description": "Number of log lines to return (default 200)"}},
        annotations={"readOnlyHint": True, "openWorldHint": False},
    )
    def get_logs(self, lines: int = 200) -> str:
        import titan_state as _TS
        return "\n".join(_TS.env().SYSTEM_LOGS[-lines:])

    @mcp_tool(
        description="Returns an AI-digestible snapshot of the full engine state.",
        input_schema={"compressed": {"type": "boolean", "description": "If true, returns compact token-optimised format (default true)"}},
        annotations={"readOnlyHint": True, "openWorldHint": False},
    )
    def get_snapshot(self, compressed: bool = True) -> str:
        if compressed:
            return self._build_snapshot_compressed()
        return self._build_snapshot()

    # ── actions ───────────────────────────────────────────────────────────────

    def force_cycle(self) -> None:
        import titan_engine as _engine
        _engine.run_loop.__func__ if hasattr(_engine.run_loop, "__func__") else None
        # trigger via the engine's cycle flag if available, else log intent
        import titan_state as _TS
        _TS._log("⚡ TitanAPI: force_cycle requested", "INFO")

    def pause(self) -> None:
        import titan_state as _TS
        _TS._log("⏸ TitanAPI: pause requested (not yet wired to engine flag)", "WARN")

    def resume(self) -> None:
        import titan_state as _TS
        _TS._log("▶ TitanAPI: resume requested (not yet wired to engine flag)", "WARN")

    @mcp_tool(
        description=(
            "Patches the live engine configuration with scalar values only. "
            "Only existing top-level keys may be updated — unknown keys are rejected. "
            "Set dry_run=true to preview the merged result without writing. "
            "Returns {ok, merged, errors}."
        ),
        input_schema={
            "patch": {"type": "object", "description": "Key/value pairs to merge (scalars only)"},
            "dry_run": {"type": "boolean", "description": "If true, validate and return merged config without saving (default false)"},
        },
        annotations={"readOnlyHint": False, "destructiveHint": False},
    )
    def update_config(self, patch: dict, dry_run: bool = False) -> dict:
        import titan_config as _C
        import json
        cfg_path = _C.get_config_file()
        with open(cfg_path, encoding="utf-8") as f:
            current = json.load(f)

        errors: list[str] = []
        for k, v in patch.items():
            if k not in current:
                errors.append(f"unknown key: {k!r}")
            elif isinstance(v, (dict, list)):
                errors.append(f"nested values not allowed for key {k!r}")
        if errors:
            return {"ok": False, "merged": None, "errors": errors}

        merged = {**current, **patch}
        if not dry_run:
            with open(cfg_path, "w", encoding="utf-8") as f:
                json.dump(merged, f, indent=2)
            _C.reload()
        return {"ok": True, "merged": merged, "errors": []}

    # ── event bus ─────────────────────────────────────────────────────────────

    def subscribe(self, event: str, callback: Callable) -> None:
        self._subscribers[event].append(callback)

    def unsubscribe(self, event: str, callback: Callable) -> None:
        try:
            self._subscribers[event].remove(callback)
        except ValueError:
            pass

    def _emit(self, event: str, payload) -> None:
        for cb in list(self._subscribers.get(event, [])):
            try:
                cb(payload)
            except Exception:
                pass

    def _notify_telegram(self, method: str, *args) -> None:
        if not self._telegram_enabled or self._telegram is None:
            return
        import threading
        fn = getattr(self._telegram, method, None)
        if fn is None:
            return
        threading.Thread(target=fn, args=args, daemon=True).start()

    def _send_telegram_boot_status(self) -> None:
        if not self._telegram_enabled or self._telegram is None:
            return
        import threading
        import titan_state as _TS

        def _run() -> None:
            try:
                if not self._telegram:
                    return
                ok = bool(self._telegram.notify_boot())
            except Exception as exc:
                _TS._log(f"Telegram boot alert failed: {exc}", "WARN")
                return
            if ok:
                _TS._log("Telegram boot alert sent", "INFO")
            else:
                _TS._log("Telegram boot alert failed", "WARN")

        threading.Thread(target=_run, daemon=True).start()

    # ── engine callbacks ──────────────────────────────────────────────────────

    def _load_persisted_signals_rejects(self) -> None:
        try:
            import titan_db as DB
            self._last_signals = DB.load_latest_signals(200)
            self._last_rejects = DB.load_latest_rejects(50)
            import titan_state as _S
            _S._log(f"📂 Restored {len(self._last_signals)} signal(s) and {len(self._last_rejects)} reject(s) from DB", "INFO")
        except Exception as e:
            import titan_state as _S
            _S._log(f"⚠ Failed to restore signals/rejects from DB: {e}", "WARN")

    def _on_cycle(self, signals, wallets, rejects, trades) -> None:
        from titan_signals import Signal
        from typing import cast
        import titan_db as DB
        ts = time.time()
        signal_dicts: list[SignalDict] = [
            s.to_dict() if isinstance(s, Signal) else cast(SignalDict, s)
            for s in (signals or [])
        ]
        self._last_signals = signal_dicts
        if signal_dicts:
            DB.save_signals(cast(list[dict], signal_dicts), ts)
        if rejects:
            DB.save_rejects(rejects, ts)
            for r in reversed(rejects):
                if r in self._last_rejects:
                    self._last_rejects.remove(r)
                self._last_rejects.insert(0, r)
            self._last_rejects = self._last_rejects[:50]
        self._emit("titan/cycle_complete", {
            "signals": signals,
            "wallets": wallets,
            "rejects": rejects,
            "trades": trades,
        })

    def _on_heartbeat(self, payload: dict) -> None:
        self._emit("titan/heartbeat", payload)

    def _on_log(self, msg: str, level: str = "INFO") -> None:
        self._emit("notifications/message", {"level": level, "data": msg})
        if str(level).upper() in {"ERR", "ERROR", "CRITICAL"}:
            self._notify_telegram("notify_error", msg)

    def _on_position_open(self, pos: dict) -> None:
        self._emit("titan/position_open", pos)
        self._notify_telegram("notify_buy", pos)

    def _on_position_close(self, pos: dict, pnl_usdc: float, pnl_pct: float) -> None:
        payload = {"pos": pos, "pnl_usdc": pnl_usdc, "pnl_pct": pnl_pct}
        self._emit("titan/position_close", payload)
        self._notify_telegram("notify_sell", pos, pnl_usdc, pnl_pct)

    # ── snapshot builders (moved from titan_ui.py) ────────────────────────────

    def _build_snapshot_compressed(self) -> str:
        import time as _t
        from datetime import datetime as _dt
        import titan_state as _TS
        from titan_config import BANKROLL_START

        def _w():
            return _TS.env()

        now_str = _dt.now().strftime("%Y-%m-%d %H:%M:%S")
        lines = [f"TITAN COMPRESSED SNAPSHOT — {now_str}", ""]

        br = _w().paper_bankroll
        st = _w().trade_stats
        lines += [
            "[ACCOUNT]",
            f"  Bank=${br:.4f}  Start=${BANKROLL_START:.2f}  "
            f"SessionPnL=${_w().session_pnl:+.4f}  TotalPnL=${br - BANKROLL_START:+.4f}",
            f"  Cycles={_w().cycle_count}  OpenPos={len(_w().open_positions)}  "
            f"Cooldowns={len(_w().cooldown_cids)}  Watchlist={len(_w().watchlist)}  "
            f"Elites={sum(1 for p in _w().wallet_cache.values() if p.get('elite'))}",
            f"  Trades={st.sell_count}({st.win_count}W/{st.loss_count}L) WR={st.win_rate*100:.0f}%",
            "",
        ]

        lines.append("[OPEN POSITIONS]")
        if _w().open_positions:
            for key, pos in _w().open_positions.items():
                cid, outcome = key if isinstance(key, tuple) else (str(key), "?")
                entry    = pos.get("entry_price", 0)
                cur      = pos.get("cur_price", entry)
                pnl_pct  = (cur - entry) / max(entry, 0.001) * 100
                pnl_abs  = (cur - entry) * pos.get("shares", 0)
                held_min = (_t.time() - pos.get("entry_ts", _t.time())) / 60
                whales   = pos.get("elite_names", []) or [w[:10]+"…" for w in pos.get("elite_wallets", [])]
                lines.append(
                    f"  [{pos.get('tier','?')}|{pos.get('score',0):.0f}pt|{'HFT' if pos.get('is_hft') else '-'}] "
                    f"{pos.get('title','?')[:60]} [{outcome}] "
                    f"WEntry=${pos.get('avg_entry',entry):.4f} Entry=${entry:.4f} Now=${cur:.4f} "
                    f"PnL={pnl_pct:+.1f}%(${pnl_abs:+.3f}) Bet=${pos.get('bet',0):.2f} "
                    f"Shares={pos.get('shares',0):.2f} Held={held_min:.0f}m via={','.join(whales)}"
                )
        else:
            lines.append("  (no open positions)")
        lines.append("")

        sigs = self._last_signals
        lines.append(f"[SIGNALS ({len(sigs)})]")
        for i, s in enumerate(sigs, 1):
            lines.append(
                f"  #{i} [{s.get('tier','?')}|{s.get('score',0):.0f}] {s.get('title','?')[:60]} [{s.get('outcome','')}] "
                f"Price=${s.get('cur',0):.4f} WEntry=${s.get('avg_entry',0):.4f} Drift={s.get('drift',0)*100:+.1f}% "
                f"via={','.join(s.get('names', [])[:5])}"
            )
        if not sigs:
            lines.append("  (no signals this cycle)")
        lines.append("")

        rejects = self._last_rejects
        lines.append(f"[REJECTIONS ({len(rejects)})]")
        lines.extend(f"  {r}" for r in rejects) if rejects else lines.append("  (none)")
        lines.append("")

        elites = sorted(
            [(w, p) for w, p in _w().wallet_cache.items() if p.get("elite")],
            key=lambda x: x[1].get("total_pnl", 0), reverse=True
        )
        lines.append(f"[ELITE ROSTER ({len(elites)})]")
        for w, p in elites:
            lines.append(
                f"  {p.get('name', w[:12]):<24} WR={p.get('win_rate',0)*100:.0f}%  "
                f"PnL=${p.get('total_pnl',0):+,.0f}  Score={p.get('score',0):.2f}  "
                f"TPH={p.get('trades_per_hour',0):.1f}  {'⚡HFT' if p.get('hft') else ''}"
            )
        lines.append("")

        import titan_db as _DB
        recent_trades = _DB.load_trade_history(limit=100)
        lines.append("[TRADE HISTORY (last 100)]")
        for t in recent_trades:
            typ     = t.get("type", "?")
            icon    = "BUY" if typ == "BUY" else ("WIN" if (t.get("pnl_usdc") or 0) >= 0 else "LOSS")
            pnl_str = f" PnL=${t.get('pnl_usdc',0):+.4f}({t.get('pnl_pct',0):+.1f}%)" if typ == "SELL" else ""
            whale_str = ",".join(t.get("whale_names", [])[:2]) or "?"
            lines.append(
                f"  [{icon}|{t.get('tier','?')}] {t.get('ts_str','?')} "
                f"{t.get('title','')[:40]} [{t.get('outcome','')}] "
                f"Entry=${t.get('entry_price',0):.4f} Bet=${t.get('bet',0):.2f}"
                f"{pnl_str} via={whale_str}"
            )
        if not recent_trades:
            lines.append("  (no trades yet)")
        lines.append("")

        lines.append(f"END — {now_str}")
        return "\n".join(lines)

    def _build_snapshot(self) -> str:
        import time as _t
        from datetime import datetime as _dt
        import titan_state as _TS
        from titan_config import BANKROLL_START

        def _w():
            return _TS.env()

        now_str = _dt.now().strftime("%Y-%m-%d %H:%M:%S")
        sep  = "═" * 72
        sep2 = "─" * 72
        lines = [sep, f"  TITAN — FULL AI DEBUG SNAPSHOT  —  {now_str}", sep, ""]

        lines += [
            "┌─ ACCOUNT ───────────────────────────────────────────────────────────┐",
            f"  Bankroll        : ${_w().paper_bankroll:.4f}",
            f"  Start Bankroll  : ${BANKROLL_START:.2f}",
            f"  Session P&L     : ${_w().session_pnl:+.4f}",
            f"  Total P&L       : ${_w().paper_bankroll - BANKROLL_START:+.4f}",
            f"  Cycle Count     : {_w().cycle_count}",
            f"  Open Positions  : {len(_w().open_positions)}",
            f"  Cooldowns       : {len(_w().cooldown_cids)}",
            f"  Watchlist       : {len(_w().watchlist)}",
            f"  Elite Count     : {sum(1 for p in _w().wallet_cache.values() if p.get('elite'))}",
            "└─────────────────────────────────────────────────────────────────────┘", "",
        ]

        lines.append("┌─ OPEN POSITIONS ────────────────────────────────────────────────────┐")
        if _w().open_positions:
            for key, pos in _w().open_positions.items():
                cid, outcome = key if isinstance(key, tuple) else (str(key), "?")
                entry    = pos.get("entry_price", 0)
                cur      = pos.get("cur_price", entry)
                pnl_pct  = (cur - entry) / max(entry, 0.001) * 100
                pnl_abs  = (cur - entry) * pos.get("shares", 0)
                held_min = (_t.time() - pos.get("entry_ts", _t.time())) / 60
                whales   = pos.get("elite_names", []) or [w[:10]+"…" for w in pos.get("elite_wallets", [])]
                lines += [
                    f"  [{pos.get('tier','?')}] {pos.get('title','?')[:60]}",
                    f"    Outcome: {outcome}  Score: {pos.get('score',0):.0f}  HFT: {'YES' if pos.get('is_hft') else 'NO'}",
                    f"    Whale Entry: ${pos.get('avg_entry',entry):.4f}  Our Entry: ${entry:.4f}  "
                    f"Now: ${cur:.4f}  P&L: {pnl_pct:+.1f}% (${pnl_abs:+.3f})",
                    f"    Bet: ${pos.get('bet',0):.2f}  Shares: {pos.get('shares',0):.2f}  "
                    f"Held: {held_min:.0f}min",
                    f"    Elite Whales: {', '.join(whales)}",
                    sep2,
                ]
        else:
            lines.append("  (no open positions)")
        lines += ["└─────────────────────────────────────────────────────────────────────┘", ""]

        lines.append("┌─ ACTIVE SIGNALS (last cycle) ───────────────────────────────────────┐")
        sigs = self._last_signals
        for i, s in enumerate(sigs, 1):
            lines += [
                f"  #{i} [{s.get('tier','?')}] Score:{s.get('score',0):.0f}  {s.get('title','?')[:60]}",
                f"     [{s.get('outcome','')}]  CurPrice: ${s.get('cur',0):.4f}  "
                f"WhaleEntry: ${s.get('avg_entry',0):.4f}  Drift: {s.get('drift',0)*100:+.1f}%",
                f"     via: {', '.join(s.get('names', [])[:5])}",
                sep2,
            ]
        if not sigs:
            lines.append("  (no signals this cycle)")
        lines += ["└─────────────────────────────────────────────────────────────────────┘", ""]

        lines.append("┌─ SIGNAL REJECTIONS (last cycle) ────────────────────────────────────┐")
        rejects = self._last_rejects
        lines.extend(f"  {r}" for r in rejects) if rejects else lines.append("  (none)")
        lines += ["└─────────────────────────────────────────────────────────────────────┘", ""]

        elites = sorted(
            [(w, p) for w, p in _w().wallet_cache.items() if p.get("elite")],
            key=lambda x: x[1].get("total_pnl", 0), reverse=True
        )
        lines.append(f"┌─ ELITE ROSTER ({len(elites)} wallets) ──────────────────────────────────────────┐")
        for w, p in elites:
            name = p.get("name", w[:12])
            lines.append(
                f"  {name:<24} WR:{p.get('win_rate',0)*100:.0f}%  "
                f"PnL:${p.get('total_pnl',0):+,.0f}  Score:{p.get('score',0):.2f}  "
                f"TPH:{p.get('trades_per_hour',0):.1f}  {'⚡HFT' if p.get('hft') else ''}"
            )
        lines += ["└─────────────────────────────────────────────────────────────────────┘", ""]

        import titan_db as _DB2
        recent_trades2 = _DB2.load_trade_history(limit=100)
        lines.append("┌─ TRADE HISTORY (last 100) ──────────────────────────────────────────┐")
        for t in recent_trades2:
            typ  = t.get("type", "?")
            icon = "🛒" if typ == "BUY" else ("✅" if (t.get("pnl_usdc") or 0) >= 0 else "❌")
            pnl_str = f"P&L ${t.get('pnl_usdc',0):+.4f} ({t.get('pnl_pct',0):+.1f}%)" if typ == "SELL" else ""
            whale_str = ", ".join(t.get("whale_names", [])[:2]) or "?"
            lines.append(
                f"  {icon} {t.get('ts_str','?')}  {typ:<4}  [{t.get('tier','?')}]  "
                f"{t.get('title','')[:36]}  [{t.get('outcome','')}]"
                f"  Entry:${t.get('entry_price',0):.4f}  Bet:${t.get('bet',0):.2f}"
                f"  {pnl_str}  via:{whale_str}"
            )
        if not recent_trades2:
            lines.append("  (no trades yet)")
        lines += ["└─────────────────────────────────────────────────────────────────────┘", ""]

        lines.append("┌─ RAW SYSTEM LOGS (last 600 lines) ──────────────────────────────────┐")
        lines.extend(_w().SYSTEM_LOGS[-600:])
        lines += ["└─────────────────────────────────────────────────────────────────────┘", ""]

        lines += [sep, f"  END OF SNAPSHOT  —  {now_str}", sep]
        return "\n".join(lines)


if __name__ == "__main__":
    import sys, os
    sys.path.insert(0, os.path.dirname(__file__))
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    api = TitanAPI()
    print("Starting engine via TitanAPI...")
    api.start()
    print("Engine started. Sleeping 20s...")
    time.sleep(20)
    print(api.status())
    print(api.get_snapshot(compressed=True))
