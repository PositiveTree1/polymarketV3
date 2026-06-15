from __future__ import annotations

import time
from collections import defaultdict
from typing import TYPE_CHECKING, Callable, TypedDict

if TYPE_CHECKING:
    from titan_signals import Signal
    from titan_position import Position
    from titan_trade import TradeRecord
    from titan_wallet import Wallet
    from titan_types import (
        AlertDict, ErrorDict,
        PnlSummaryDict, TradeStatsDict, PortfolioOverviewDict,
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
        self._last_signals: list[Signal] = []
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
            "watchlist_size": len(_TS.get_watchlist()),
            "recent_error_count": recent_errors,
            "auth_enabled": False,
        }

    @mcp_tool(
        description="Returns open Polymarket positions as consolidated position dicts.",
        annotations={"readOnlyHint": True, "openWorldHint": False},
    )
    def get_positions(self) -> list[Position]:
        import titan_state as _TS
        env = _TS.env()
        positions = sorted(
            env.open_positions.values(),
            key=lambda pos: pos.entry_ts,
            reverse=True,
        )
        for pos in positions:
            if not pos.price_history:
                pos.load_prices()
        return positions

    @mcp_tool(
        description="Returns closed positions as consolidated position dicts enriched with price_history from DB.",
        input_schema={"limit": {"type": "integer", "description": "Max number of closed positions to return (default 200)"}},
        annotations={"readOnlyHint": True, "openWorldHint": False},
    )
    def get_closed_positions(self, limit: int = 200) -> list[Position]:
        import titan_db as _DB
        import titan_state as _TS
        from titan_position import group_trades_by_position, build_position_from_trades
        all_trades = _DB.load_trade_history(limit=limit * 2)
        open_assets: set[str] = {pos.asset for pos in _TS.env().open_positions.values() if pos.asset}
        positions = []
        for bucket_trades in group_trades_by_position(all_trades).values():
            has_sell = any(t.type == "SELL" for t in bucket_trades)
            if not has_sell:
                continue
            pos = build_position_from_trades(bucket_trades)
            if pos.asset and pos.asset in open_assets:
                continue
            positions.append(pos)
        positions.sort(key=lambda p: p.exit_ts or p.entry_ts, reverse=True)
        return positions[:limit]

    @mcp_tool(
        description="Returns current wallet-triggered trading signals with confidence scores.",
        input_schema={"min_score": {"type": "number", "description": "Minimum signal score filter. Typical range 0–100 (integer-like values such as 78 or 81 are normal)."}},
        annotations={"readOnlyHint": True, "openWorldHint": False},
    )
    def get_signals(self, min_score: float = 0.0) -> list[Signal]:
        from titan_signals import load_signal_prices_many
        filtered = [s for s in self._last_signals if s.score >= min_score]
        return load_signal_prices_many(filtered)

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
    def get_signal_history(self, limit: int = 200, min_score: float = 0.0, cid: str | None = None) -> list[Signal]:
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
        description=(
            "Returns tracked wallets with performance metrics. "
            "Pass 'search' to filter by name or address prefix (case-insensitive) — use this when asking about a specific wallet. "
            "Pass 'tier' to filter by elite | verified | watchable | vip. "
            "Without filters returns the full roster."
        ),
        input_schema={
            "search": {"type": "string", "description": "Filter by wallet name or address prefix (case-insensitive)"},
            "tier":   {"type": "string", "description": "Filter by tier: elite | verified | watchable | vip"},
        },
        annotations={"readOnlyHint": True, "openWorldHint": False},
    )
    def get_tracked_wallets(self, search: str = "", tier: str = "") -> list[Wallet]:
        import titan_state as _TS
        from titan_config import VIP_WALLETS, VIP_WALLET_NAMES
        from dataclasses import replace
        vip_wallets = {addr.lower() for addr in VIP_WALLETS}
        results: list[Wallet] = []
        search_lower = search.lower()
        for w, wallet in _TS.env().wallet_cache.items():
            vip_name = VIP_WALLET_NAMES.get(w.lower(), "")
            display_name = vip_name if vip_name and (
                not wallet.name or wallet.name.startswith("0x") or wallet.name.endswith("…")
            ) else wallet.name
            if search_lower and search_lower not in display_name.lower() and not w.lower().startswith(search_lower):
                continue
            if tier == "elite"     and not wallet.elite:     continue
            if tier == "verified"  and not wallet.verified:  continue
            if tier == "watchable" and not wallet.watchable: continue
            if tier == "vip"       and w.lower() not in vip_wallets: continue
            out = replace(wallet, name=display_name, vip=w.lower() in vip_wallets)
            results.append(out)
        return results

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
            "watchlist_size": len(_TS.get_watchlist()),
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
            (pos.cur_price or pos.entry_price) * pos.shares
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
            "watchlist_size": len(_TS.get_watchlist()),
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
        description=(
            "Fetches the full price history for a Polymarket asset token. "
            "Returns a list of [timestamp, price] pairs sorted by time. "
            "Forwarded directly to the Polymarket CLOB API."
        ),
        input_schema={"asset": {"type": "string", "description": "Asset token ID (the hex token address for the outcome)."}},
        annotations={"readOnlyHint": True, "openWorldHint": True},
    )
    def get_asset_price_history(self, asset: str) -> list[tuple[float, float]]:
        from titan_prices import PRICES
        PRICES.refresh(asset)
        return PRICES.get(asset)

    @mcp_tool(
        description="Returns the full trade history (buys and sells).",
        annotations={"readOnlyHint": True, "openWorldHint": False},
    )
    def get_trade_history(self) -> list[TradeRecord]:
        import titan_db as _DB
        return _DB.load_trade_history()

    @mcp_tool(
        description=(
            "Returns the full raw titan_config.json. "
            "Prefer the domain-specific get_config_* tools for AI analysis — "
            "they include descriptions for each parameter."
        ),
        annotations={"readOnlyHint": True, "openWorldHint": False},
    )
    def get_config(self) -> dict:
        import titan_config as _C
        import json
        try:
            with open(_C.get_config_file(), encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    # ── domain config readers ─────────────────────────────────────────────────

    @staticmethod
    def _read_cfg() -> dict:
        import titan_config as _C, json
        with open(_C.get_config_file(), encoding="utf-8") as f:
            return json.load(f)

    @staticmethod
    def _write_cfg(cfg: dict, patch_summary: str = "") -> None:
        import titan_config as _C, json, titan_state as _S
        with open(_C.get_config_file(), "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
        _C.reload()
        _S._log(f"Config updated: {patch_summary}", "INFO")

    @staticmethod
    def _flat_group(cfg: dict, group: str) -> dict:
        g = cfg.get(group, {})
        out: dict = {}
        for k, v in g.items():
            if k.startswith("_"):
                continue
            if isinstance(v, dict) and "value" in v:
                out[k] = {"value": v["value"], "description": v.get("_description", "")}
            elif not isinstance(v, dict):
                out[k] = {"value": v, "description": ""}
        return out

    @mcp_tool(
        description=(
            "Returns wallet selection and elite classification thresholds. "
            "wallet_quality: watch/verify win-rate floors, min resolved bets, min PnL per trade. "
            "elite_thresholds: ELITE_MIN_PNL=$40k, ELITE_MIN_PORT=$80k, ELITE_MIN_SCORE=0.72, ELITE_MIN_RESOLVED=20. "
            "elite_polling: ELITE_POLL_LIMIT, ELITE_POLL_MIN_CASH=$50, ELITE_TRADE_MIN_FRACTION=3%. "
            "wallet_selector: scoring weights (wilson=0.3, pnl_pct=0.25, portfolio=0.15), discovery settings, HFT/sports bot TPH thresholds."
        ),
        annotations={"readOnlyHint": True, "openWorldHint": False},
    )
    def get_config_wallets(self) -> dict:
        cfg = self._read_cfg()
        ws  = cfg.get("wallet_selector", {})
        active = ws.get("active_selector", "performance")
        return {
            "wallet_quality":   self._flat_group(cfg, "wallet_quality"),
            "elite_thresholds": self._flat_group(cfg, "elite_thresholds"),
            "elite_polling":    self._flat_group(cfg, "elite_polling"),
            "wallet_selector":  ws.get("selectors", {}).get(active, {}),
        }

    @mcp_tool(
        description=(
            "Returns signal quality gates and scoring constants. "
            "signal_quality: MAX_SIGNAL_AGE_H=0.25 (15min), MIN_SCORE=55, MIN_CONFLUENCE=2. "
            "drift_gates: MAX_DRIFT=5%, MIN_DRIFT=-8%, MAX_ENTRY_SLIPPAGE=3%, STALE_LOSER thresholds. "
            "price_zone_gates: MIN_ENTRY_PRICE=0.20, MAX_ENTRY_PRICE=0.72, IDEAL zone 0.25-0.65. "
            "strategy_scoring: confluence_pts array, recency hot/warm thresholds, price zone bonuses, exit penalties."
        ),
        annotations={"readOnlyHint": True, "openWorldHint": False},
    )
    def get_config_signals(self) -> dict:
        cfg = self._read_cfg()
        return {
            "signal_quality":   self._flat_group(cfg, "signal_quality"),
            "drift_gates":      self._flat_group(cfg, "drift_gates"),
            "price_zone_gates": self._flat_group(cfg, "price_zone_gates"),
            "strategy_scoring": {k: v for k, v in cfg.get("strategy_scoring", {}).items() if not k.startswith("_")},
        }

    @mcp_tool(
        description=(
            "Returns per-strategy configuration for all signal builders. "
            "recent_form: copy recent winners — max_tph=20, min_score=42, age<=45min, price 0.18-0.78, max 4 positions, no stop-loss. "
            "drift_discount: discounted entry 4-12% below tracked wallet entry — age<=6h, price 0.20-0.72, max 3 positions, no stop-loss. "
            "consensus_basket: volume play — min_elite=1, min_score=50, price 0.20-0.72, max 5 positions, stop-loss=-35%. "
            "open_book: disabled — needs 3+ elites holding same outcome. "
            "Also returns active_strategies list, tradeable_tiers, allowed_market_types, signal_builders registry."
        ),
        annotations={"readOnlyHint": True, "openWorldHint": False},
    )
    def get_config_strategies(self) -> dict:
        cfg   = self._read_cfg()
        strat = cfg.get("strategy", {})
        return {
            "active_strategies":    strat.get("ACTIVE_STRATEGIES", []),
            "tradeable_tiers":      strat.get("TRADEABLE_TIERS_LIST", []),
            "allowed_market_types": strat.get("ALLOWED_MARKET_TYPES", []),
            "min_elite_confluence": strat.get("MIN_ELITE_CONFLUENCE"),
            "block_sports":         strat.get("BLOCK_SPORTS"),
            "recent_form":          {k: v for k, v in cfg.get("strategy_recent_form", {}).items()     if not k.startswith("_")},
            "drift_discount":       {k: v for k, v in cfg.get("strategy_drift_discount", {}).items()  if not k.startswith("_")},
            "consensus_basket":     {k: v for k, v in cfg.get("strategy_consensus_basket", {}).items() if not k.startswith("_")},
            "open_book":            {k: v for k, v in cfg.get("strategy_open_book", {}).items()        if not k.startswith("_")},
            "signal_builders":      cfg.get("signal_builders", {}),
        }

    @mcp_tool(
        description=(
            "Returns position management and risk parameters. "
            "position_management: MAX_OPEN_POSITIONS=5, PROFIT_TARGET_PCT=40%, STOP_LOSS_PCT=-30%, "
            "STOP_LOSS_ENABLED=true, WALLET_EXIT_SELL=true, MAX_POSITIONS_PER_EVENT=1, MAX_POSITIONS_PER_WALLET=2. "
            "timing: MIN_HOLD_MINUTES=5, EXIT_COOLDOWN_SECONDS=600. "
            "position_management_ext: wallet_exit_min_sell_fraction=0.3. "
            "strategy_kelly: score_mult, conf_mult_cap=1.75, tier_multipliers (CONVICTION=1.6, ALERT=1.2), adaptive_caps."
        ),
        annotations={"readOnlyHint": True, "openWorldHint": False},
    )
    def get_config_risk(self) -> dict:
        cfg = self._read_cfg()
        return {
            "position_management":     self._flat_group(cfg, "position_management"),
            "timing":                  self._flat_group(cfg, "timing"),
            "position_management_ext": {k: v for k, v in cfg.get("position_management_ext", {}).items() if not k.startswith("_")},
            "strategy_kelly":          {k: v for k, v in cfg.get("strategy_kelly", {}).items()           if not k.startswith("_")},
        }

    @mcp_tool(
        description=(
            "Returns bankroll and bet sizing parameters. "
            "bankroll_and_sizing: BANKROLL_START=$20, MIN_BET=$1, MAX_BET_ABS=$4, MAX_BET_PCT=18%, KELLY_FRACTION=0.2. "
            "sizing: USE_PROPORTIONAL_SIZING=false. "
            "market_quality: MIN_LIQUIDITY=$15k, MIN_VOLUME=$30k, MIN_HOURS_LEFT=4h. "
            "fees: TAKER_FEE_RATE=0."
        ),
        annotations={"readOnlyHint": True, "openWorldHint": False},
    )
    def get_config_sizing(self) -> dict:
        cfg = self._read_cfg()
        return {
            "bankroll_and_sizing": self._flat_group(cfg, "bankroll_and_sizing"),
            "sizing":              self._flat_group(cfg, "sizing"),
            "market_quality":      self._flat_group(cfg, "market_quality"),
            "fees":                self._flat_group(cfg, "fees"),
        }

    @mcp_tool(
        description=(
            "Returns trade sourcing, cache, and discovery parameters. "
            "trade_sourcing: MIN_TRADE_CASH=$200, MAX_TRADES_FETCH=300, HOT_HOURS=1, WARM_HOURS=1, CYCLE_SECONDS=15. "
            "discovery: DISCOVERY_INTERVAL_CYCLES=20. "
            "cache: WALLET_TTL=600s, MARKET_TTL=30s, ACTIVITY_LIMIT=500. "
            "vip_wallets: always-polled addresses (MEPP, 0x8dxd, Wickier, mr.ozi, nojnn, Clear-Corridor). "
            "priority_wallets: extra-polled addresses (currently empty)."
        ),
        annotations={"readOnlyHint": True, "openWorldHint": False},
    )
    def get_config_sourcing(self) -> dict:
        cfg = self._read_cfg()
        return {
            "trade_sourcing":   self._flat_group(cfg, "trade_sourcing"),
            "discovery":        self._flat_group(cfg, "discovery"),
            "cache":            self._flat_group(cfg, "cache"),
            "vip_wallets":      cfg.get("vip_wallets", {}).get("wallets", []),
            "priority_wallets": cfg.get("priority_wallets", {}).get("wallets", []),
        }

    # ── domain config writers ─────────────────────────────────────────────────

    @staticmethod
    def _patch_group(cfg: dict, group: str, patch: dict) -> list[str]:
        g = cfg.setdefault(group, {})
        errors: list[str] = []
        for k, v in patch.items():
            if k.startswith("_"):
                errors.append(f"key {k!r} is reserved")
                continue
            if k not in g:
                errors.append(f"unknown key {k!r} in group {group!r}")
                continue
            entry = g[k]
            if isinstance(entry, dict) and "value" in entry:
                g[k] = {**entry, "value": v}
            else:
                g[k] = v
        return errors

    @mcp_tool(
        description=(
            "Update wallet selection and elite classification thresholds. "
            "group: wallet_quality | elite_thresholds | elite_polling | wallet_selector. "
            "patch: {key: new_value} — only existing keys accepted. "
            "For wallet_selector, patches the active selector's flat params (e.g. wilson_min_watch, min_portfolio_or_pnl). "
            "dry_run=true previews without saving. Returns {ok, errors, applied}."
        ),
        input_schema={
            "group": {"type": "string", "description": "wallet_quality | elite_thresholds | elite_polling | wallet_selector"},
            "patch": {"type": "object", "description": "Key/value pairs to update"},
            "dry_run": {"type": "boolean", "description": "Validate only, do not save"},
        },
        annotations={"readOnlyHint": False, "destructiveHint": False},
    )
    def update_config_wallets(self, group: str, patch: dict, dry_run: bool = False) -> dict:
        allowed = {"wallet_quality", "elite_thresholds", "elite_polling", "wallet_selector"}
        if group not in allowed:
            return {"ok": False, "errors": [f"group must be one of {sorted(allowed)}"], "applied": {}}
        cfg = self._read_cfg()
        _REEVAL_GROUPS = {"wallet_quality", "elite_thresholds", "wallet_selector"}
        if group == "wallet_selector":
            ws = cfg.setdefault("wallet_selector", {})
            active = ws.get("active_selector", "performance")
            target = ws.setdefault("selectors", {}).setdefault(active, {})
            errors = [f"unknown key {k!r} in wallet_selector/{active}" for k in patch if k not in target]
            if errors:
                return {"ok": False, "errors": errors, "applied": {}}
            if not dry_run:
                target.update(patch)
                self._write_cfg(cfg, f"wallets/wallet_selector/{active} {patch}")
                reeval = TitanAPI._reeval_wallets_impl()
                self._emit_config_updated("wallet_selector", patch, reeval)
            return {"ok": True, "errors": [], "applied": patch}
        errors = self._patch_group(cfg, group, patch)
        if errors:
            return {"ok": False, "errors": errors, "applied": {}}
        if not dry_run:
            self._write_cfg(cfg, f"wallets/{group} {patch}")
            if group in _REEVAL_GROUPS:
                reeval = TitanAPI._reeval_wallets_impl()
                self._emit_config_updated(group, patch, reeval)
        return {"ok": True, "errors": [], "applied": patch}

    def _emit_config_updated(self, group: str, patch: dict, reeval: dict) -> None:
        self._emit("titan/config_updated", {
            "domain": "wallets",
            "group": group,
            "patch": patch,
            "reeval": reeval,
            "refresh": ["config", "wallets", "snapshot"],
        })

    @mcp_tool(
        description=(
            "Re-classify all wallets in the DB using the current config thresholds. "
            "Does NOT hit the Polymarket API — re-runs is_selected() on the stored profile_json. "
            "Use after changing wallet quality thresholds to immediately propagate new tiers. "
            "Returns {reclassified, now_watchable, now_unwatchable, total}."
        ),
        input_schema={},
        annotations={"readOnlyHint": False, "destructiveHint": False},
    )
    def reeval_wallets(self) -> dict:
        return TitanAPI._reeval_wallets_impl()

    @staticmethod
    def _reeval_wallets_impl() -> dict:
        import json as _json
        import titan_config as _C
        import titan_db as _DB
        import titan_state as _S

        sel = _C.get_active_selector()
        if sel is None:
            return {"ok": False, "error": "No active selector"}

        with _DB._connect() as cx:
            rows = cx.execute("SELECT address, watchable, profile_json FROM watchlist WHERE profile_json IS NOT NULL").fetchall()

        now_watchable = 0
        now_unwatchable = 0
        reclassified = 0

        updates: list[tuple[int, str]] = []
        profile_updates: list[tuple[str, str]] = []
        vip_wallets = {wallet.lower() for wallet in _C.VIP_WALLETS}
        vip_names = _C.VIP_WALLET_NAMES

        for addr, old_watchable, profile_json in rows:
            try:
                prof = _json.loads(profile_json)
            except Exception:
                continue

            raw = {
                "win_rate":        prof.get("win_rate", 0.0),
                "wilson_lb":       prof.get("wilson_lb", 0.0),
                "n_resolved":      prof.get("n_resolved", 0),
                "total_pnl":       prof.get("total_pnl", 0.0),
                "total_value":     prof.get("total_value", 0.0),
                "avg_profit":      prof.get("avg_profit", 0.0),
                "avg_bet":         prof.get("avg_bet", 0.0),
                "trades_per_hour": prof.get("trades_per_hour", 0.0),
                "alpha_per_trade": prof.get("alpha_per_trade", 0.0),
                "n_pos":           prof.get("n_pos", 0),
                "pnl_pct":         prof.get("pnl_pct", 0.0),
            }
            score = sel.score(raw)
            watchable, verified, elite, fail_reasons = sel.is_selected(raw, score)

            new_watchable = 1 if (watchable or verified) else 0
            if new_watchable != old_watchable:
                reclassified += 1
                if new_watchable:
                    now_watchable += 1
                else:
                    now_unwatchable += 1

            prof["score"]        = round(score, 5)
            prof["verified"]     = verified
            prof["watchable"]    = bool(watchable or verified)
            prof["elite"]        = elite
            prof["vip"]          = addr.lower() in vip_wallets
            vip_name = vip_names.get(addr.lower(), "")
            current_name = str(prof.get("name") or "")
            if vip_name and (not current_name or current_name.startswith("0x") or current_name.endswith("…")):
                prof["name"] = vip_name
            prof["fail_reasons"] = fail_reasons
            if addr in _S.env().wallet_cache:
                _S.env().wallet_cache[addr] = prof
            updates.append((new_watchable, addr))
            profile_updates.append((_json.dumps(prof), addr))

        with _DB._connect() as cx:
            cx.executemany("UPDATE watchlist SET watchable=? WHERE address=?", updates)
            cx.executemany("UPDATE watchlist SET profile_json=? WHERE address=?", profile_updates)

        _S._log(f"reeval_wallets: {reclassified} reclassified ({now_watchable} gained, {now_unwatchable} lost) of {len(rows)} total", "INFO")
        return {"ok": True, "reclassified": reclassified, "now_watchable": now_watchable, "now_unwatchable": now_unwatchable, "total": len(rows)}

    @mcp_tool(
        description=(
            "Update signal quality gates. "
            "group: signal_quality | drift_gates | price_zone_gates. "
            "patch: {key: new_value} — only existing keys accepted. "
            "dry_run=true previews without saving. Returns {ok, errors, applied}."
        ),
        input_schema={
            "group": {"type": "string", "description": "signal_quality | drift_gates | price_zone_gates | strategy_scoring"},
            "patch": {"type": "object", "description": "Key/value pairs to update"},
            "dry_run": {"type": "boolean", "description": "Validate only, do not save"},
        },
        annotations={"readOnlyHint": False, "destructiveHint": False},
    )
    def update_config_signals(self, group: str, patch: dict, dry_run: bool = False) -> dict:
        allowed = {"signal_quality", "drift_gates", "price_zone_gates", "strategy_scoring"}
        if group not in allowed:
            return {"ok": False, "errors": [f"group must be one of {sorted(allowed)}"], "applied": {}}
        cfg = self._read_cfg()
        if group == "strategy_scoring":
            block = cfg.setdefault("strategy_scoring", {})
            errors = [f"key {k!r} is reserved" for k in patch if k.startswith("_")]
            if errors:
                return {"ok": False, "errors": errors, "applied": {}}
            if not dry_run:
                block.update(patch)
                self._write_cfg(cfg, f"signals/strategy_scoring {patch}")
            return {"ok": True, "errors": [], "applied": patch}
        errors = self._patch_group(cfg, group, patch)
        if errors:
            return {"ok": False, "errors": errors, "applied": {}}
        if not dry_run:
            self._write_cfg(cfg, f"signals/{group} {patch}")
        return {"ok": True, "errors": [], "applied": patch}

    @mcp_tool(
        description=(
            "Update per-strategy configuration. "
            "strategy: recent_form | drift_discount | consensus_basket | open_book. "
            "patch: {key: new_value} applied directly to that strategy block. "
            "dry_run=true previews without saving. Returns {ok, errors, applied}."
        ),
        input_schema={
            "strategy": {"type": "string", "description": "recent_form | drift_discount | consensus_basket | open_book"},
            "patch": {"type": "object", "description": "Key/value pairs to update in the strategy block"},
            "dry_run": {"type": "boolean", "description": "Validate only, do not save"},
        },
        annotations={"readOnlyHint": False, "destructiveHint": False},
    )
    def update_config_strategies(self, strategy: str, patch: dict, dry_run: bool = False) -> dict:
        allowed = {"recent_form", "drift_discount", "consensus_basket", "open_book"}
        if strategy not in allowed:
            return {"ok": False, "errors": [f"strategy must be one of {sorted(allowed)}"], "applied": {}}
        cfg   = self._read_cfg()
        block = cfg.setdefault(f"strategy_{strategy}", {})
        errors = [f"key {k!r} is reserved" for k in patch if k.startswith("_")]
        if errors:
            return {"ok": False, "errors": errors, "applied": {}}
        if not dry_run:
            block.update(patch)
            self._write_cfg(cfg, f"strategy/{strategy} {patch}")
        return {"ok": True, "errors": [], "applied": patch}

    @mcp_tool(
        description=(
            "Update position management and risk parameters. "
            "group: position_management | timing. "
            "patch: {key: new_value} — only existing keys accepted. "
            "dry_run=true previews without saving. Returns {ok, errors, applied}."
        ),
        input_schema={
            "group": {"type": "string", "description": "position_management | timing"},
            "patch": {"type": "object", "description": "Key/value pairs to update"},
            "dry_run": {"type": "boolean", "description": "Validate only, do not save"},
        },
        annotations={"readOnlyHint": False, "destructiveHint": False},
    )
    def update_config_risk(self, group: str, patch: dict, dry_run: bool = False) -> dict:
        allowed = {"position_management", "timing", "strategy_kelly"}
        if group not in allowed:
            return {"ok": False, "errors": [f"group must be one of {sorted(allowed)}"], "applied": {}}
        cfg = self._read_cfg()
        if group == "strategy_kelly":
            block = cfg.setdefault("strategy_kelly", {})
            errors = [f"key {k!r} is reserved" for k in patch if k.startswith("_")]
            if errors:
                return {"ok": False, "errors": errors, "applied": {}}
            if not dry_run:
                block.update(patch)
                self._write_cfg(cfg, f"risk/strategy_kelly {patch}")
            return {"ok": True, "errors": [], "applied": patch}
        errors = self._patch_group(cfg, group, patch)
        if errors:
            return {"ok": False, "errors": errors, "applied": {}}
        if not dry_run:
            self._write_cfg(cfg, f"risk/{group} {patch}")
        return {"ok": True, "errors": [], "applied": patch}

    @mcp_tool(
        description=(
            "Update bankroll and bet sizing parameters. "
            "group: bankroll_and_sizing | sizing | market_quality. "
            "patch: {key: new_value} — only existing keys accepted. "
            "dry_run=true previews without saving. Returns {ok, errors, applied}."
        ),
        input_schema={
            "group": {"type": "string", "description": "bankroll_and_sizing | sizing | market_quality"},
            "patch": {"type": "object", "description": "Key/value pairs to update"},
            "dry_run": {"type": "boolean", "description": "Validate only, do not save"},
        },
        annotations={"readOnlyHint": False, "destructiveHint": False},
    )
    def update_config_sizing(self, group: str, patch: dict, dry_run: bool = False) -> dict:
        allowed = {"bankroll_and_sizing", "sizing", "market_quality"}
        if group not in allowed:
            return {"ok": False, "errors": [f"group must be one of {sorted(allowed)}"], "applied": {}}
        cfg = self._read_cfg()
        errors = self._patch_group(cfg, group, patch)
        if errors:
            return {"ok": False, "errors": errors, "applied": {}}
        if not dry_run:
            self._write_cfg(cfg, f"sizing/{group} {patch}")
        return {"ok": True, "errors": [], "applied": patch}

    @mcp_tool(
        description="Per-strategy P&L breakdown: trade count, win rate, total PnL, avg PnL%. Faster than query_db for the most common diagnostic query.",
        annotations={"readOnlyHint": True, "openWorldHint": False},
    )
    def get_strategy_stats(self) -> list[dict]:
        import titan_db as _DB
        rows = _DB.query_db(
            "SELECT strategy, COUNT(*) as trades, "
            "SUM(CASE WHEN pnl_usdc > 0 THEN 1 ELSE 0 END) as wins, "
            "ROUND(SUM(pnl_usdc), 2) as total_pnl, "
            "ROUND(AVG(pnl_pct) * 100, 1) as avg_pct, "
            "ROUND(AVG(CASE WHEN pnl_usdc > 0 THEN pnl_usdc END), 2) as avg_win, "
            "ROUND(AVG(CASE WHEN pnl_usdc < 0 THEN pnl_usdc END), 2) as avg_loss "
            "FROM trade_history WHERE type='SELL' "
            "GROUP BY strategy ORDER BY total_pnl DESC"
        )
        for row in rows:
            trades = row.get("trades") or 0
            wins = row.get("wins") or 0
            row["win_rate"] = round(wins / trades, 3) if trades else 0.0
        return rows

    @mcp_tool(
        description="Frequency map of signal rejection reasons from the last N cycles. Shows which gate is blocking most signals — faster than parsing get_rejects() manually.",
        input_schema={"limit": {"type": "integer", "description": "Number of recent rejects to analyse (default 200)"}},
        annotations={"readOnlyHint": True, "openWorldHint": False},
    )
    def get_reject_summary(self, limit: int = 200) -> dict:
        rejects = self._last_rejects[-limit:]
        by_reason: dict[str, int] = {}
        for r in rejects:
            reason = r.split(":")[0].strip()
            by_reason[reason] = by_reason.get(reason, 0) + 1
        return {"total": len(rejects), "by_reason": dict(sorted(by_reason.items(), key=lambda x: x[1], reverse=True))}

    @mcp_tool(
        description=(
            "TITAN's own copy-trade ROI per tracked wallet — how profitable it has been to follow each wallet's signals. "
            "Different from their Polymarket stats. wallet_names is a JSON-encoded list stored as a string."
        ),
        annotations={"readOnlyHint": True, "openWorldHint": False},
    )
    def get_wallet_copy_roi(self) -> list[dict]:
        import titan_db as _DB
        return _DB.query_db(
            "SELECT wallet_names, COUNT(*) as signals_followed, "
            "SUM(CASE WHEN pnl_usdc > 0 THEN 1 ELSE 0 END) as wins, "
            "ROUND(SUM(pnl_usdc), 2) as total_pnl, "
            "ROUND(AVG(pnl_pct) * 100, 1) as avg_pct "
            "FROM trade_history WHERE type='SELL' "
            "GROUP BY wallet_names ORDER BY total_pnl DESC LIMIT 30"
        )

    @mcp_tool(
        description=(
            "Lists all available knowledge-base documents in the docs/ folder. "
            "Returns a list of {name, path, description} entries. "
            "Call read_doc(path) to retrieve the content of any document."
        ),
        annotations={"readOnlyHint": True, "openWorldHint": False},
    )
    def get_docs(self) -> list[dict]:
        import pathlib
        docs_dir = pathlib.Path(__file__).parent.parent / "docs"
        _DESCRIPTIONS: dict[str, str] = {
            "TITAN_AI_GUIDE.md":             "Entry point — what TITAN is, MCP connection, architecture, improvement ideas",
            "TITAN_CONTEXT.md":              "Full architecture, module map, engine loop, all key parameters",
            "TITAN_STRATEGIES.md":           "Strategy logic — entry/exit rules for recent_form, drift_discount, consensus_basket",
            "TITAN_POLYMARKET_DATA_MODEL.md":"Data structures: WalletObservation, Market, Signal, Position, URL identity rules",
            "MCP_REFERENCE.md":              "All MCP tools with inputs/outputs, SSE events, logging architecture",
            "ANALYSIS_GUIDE.md":             "AI analysis workflow, diagnostic decision tree, safe parameter ranges, SQL queries",
            "config/CONFIG_WALLETS.md":      "Wallet quality thresholds, elite classification, selector weights",
            "config/CONFIG_SIGNALS.md":      "Signal gates, scoring constants, price/drift zones, strategy scoring formulas",
            "config/CONFIG_STRATEGIES.md":   "Per-strategy parameters for all 3 builders + open_book",
            "config/CONFIG_RISK.md":         "Stop-loss, profit target, Kelly formula, timing, position management",
            "config/CONFIG_SIZING.md":       "Bankroll, bet caps, market quality filters, trade sourcing, cache TTLs",
        }
        result = []
        for md in sorted(docs_dir.rglob("*.md")):
            rel = md.relative_to(docs_dir).as_posix()
            result.append({
                "name":        md.name,
                "path":        rel,
                "description": _DESCRIPTIONS.get(rel, ""),
            })
        return result

    @mcp_tool(
        description=(
            "Returns the full content of a knowledge-base document from the docs/ folder. "
            "Use get_docs() first to discover available paths, then call read_doc(path) "
            "with the relative path (e.g. 'TITAN_AI_GUIDE.md' or 'config/CONFIG_RISK.md')."
        ),
        input_schema={"path": {"type": "string", "description": "Relative path within docs/ e.g. 'TITAN_AI_GUIDE.md' or 'config/CONFIG_RISK.md'"}},
        annotations={"readOnlyHint": True, "openWorldHint": False},
    )
    def read_doc(self, path: str) -> str:
        import pathlib
        docs_dir = pathlib.Path(__file__).parent.parent / "docs"
        target = (docs_dir / path).resolve()
        if not str(target).startswith(str(docs_dir.resolve())):
            raise ValueError(f"Path outside docs/: {path}")
        if not target.exists():
            raise FileNotFoundError(f"Doc not found: {path}")
        return target.read_text(encoding="utf-8")

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

    @mcp_tool(
        description="Re-run selector scoring on all cached wallets without hitting the Polymarket API. Returns count of wallets reclassified.",
        annotations={"readOnlyHint": False, "openWorldHint": False},
    )
    def apply_selector(self) -> int:
        import titan_state as _TS
        from titan_wallet import WalletsCacheSrv
        cache = _TS.env().wallet_cache
        if not isinstance(cache, WalletsCacheSrv):
            return 0
        n = cache.reclassify_all()
        wallets = _TS.env().wallet_cache
        self._emit("titan/cycle_complete", {
            "signals": _TS.env().LAST_SIGNALS,
            "wallets": wallets,
            "rejects": _TS.env().LAST_REJECTS,
            "trades":  [],
        })
        return n

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

    # ── event bus ─────────────────────────────────────────────────────────────

    def subscribe(self, event: str, callback: Callable) -> None:
        self._subscribers[event].append(callback)

    def unsubscribe(self, event: str, callback: Callable) -> None:
        try:
            self._subscribers[event].remove(callback)
        except ValueError:
            pass

    def _emit(self, event: str, payload) -> None:
        import titan_state as _TS
        for cb in list(self._subscribers.get(event, [])):
            try:
                cb(payload)
            except Exception as e:
                import traceback
                _TS._log(f"Event callback error [{event}] in {cb.__qualname__}: {e}\n{traceback.format_exc()}", "ERR")

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
            import titan_state as _S
            self._last_signals = DB.load_latest_signals(200)
            self._last_rejects = DB.load_latest_rejects(50)
            msg = f"Startup recovery: signals={len(self._last_signals)} | rejects={len(self._last_rejects)}"
            _S.log_important(msg)
        except Exception as e:
            import traceback, titan_state as _S
            msg = f"⚠ Failed to restore signals/rejects from DB: {e}\n{traceback.format_exc()}"
            _S.log_important(msg)

    def _on_cycle(self, signals, wallets, rejects, trades) -> None:
        from titan_signals import Signal, load_signal_prices_many
        import titan_db as DB
        ts = time.time()
        typed_signals: list[Signal] = [s for s in (signals or []) if isinstance(s, Signal)]
        import titan_state as _S

        if typed_signals:
            DB.save_signals(typed_signals, ts)
            load_signal_prices_many(typed_signals)
            self._last_signals = typed_signals
        else:
            # No new signals — expire any preserved signal past its age limit
            _age_limit = {"consensus_basket": 0.5, "recent_form": 0.75, "drift_discount": 6.0}
            surviving = []
            for s in self._last_signals:
                age_h = (ts - s.newest_ts) / 3600
                limit = _age_limit.get(s.strategy.split("+")[0], 1.0)
                if rejects:
                    _S._log(f"  ❌ Signal removed: {s.outcome} {s.title[:45]} — explicitly rejected", "INFO")
                elif age_h > limit:
                    _S._log(f"  ❌ Signal expired: {s.outcome} {s.title[:45]} — age={age_h:.1f}h > {limit}h", "INFO")
                else:
                    surviving.append(s)
            removed = [s.cid for s in self._last_signals if s not in surviving]
            if removed:
                DB.mark_signals_not_live(removed)
            self._last_signals = surviving
        if rejects:
            DB.save_rejects(rejects, ts)
            for r in reversed(rejects):
                if r in self._last_rejects:
                    self._last_rejects.remove(r)
                self._last_rejects.insert(0, r)
            self._last_rejects = self._last_rejects[:50]
        self._emit("titan/cycle_complete", {
            "signals": typed_signals,
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
            f"Cooldowns={len(_w().cooldown_cids)}  Watchlist={len(_TS.get_watchlist())}  "
            f"Elites={sum(1 for p in _w().wallet_cache.values() if p.get('elite'))}",
            f"  Trades={st.sell_count}({st.win_count}W/{st.loss_count}L) WR={st.win_rate*100:.0f}%",
            "",
        ]

        lines.append("[OPEN POSITIONS]")
        if _w().open_positions:
            for key, pos in _w().open_positions.items():
                cid, outcome = key if isinstance(key, tuple) else (str(key), "?")
                entry    = pos.entry_price
                cur      = pos.cur_price or entry
                pnl_pct  = (cur - entry) / max(entry, 0.001) * 100
                pnl_abs  = (cur - entry) * pos.shares
                held_min = (_t.time() - pos.entry_ts) / 60 if pos.entry_ts else 0.0
                wallets   = pos.elite_names or [w[:10]+"…" for w in pos.elite_wallets]
                lines.append(
                    f"  [{pos.tier}|{pos.score:.0f}pt|{'HFT' if pos.is_hft else '-'}] "
                    f"{pos.title[:60]} [{outcome}] "
                    f"WEntry=${pos.avg_entry or entry:.4f} Entry=${entry:.4f} Now=${cur:.4f} "
                    f"PnL={pnl_pct:+.1f}%(${pnl_abs:+.3f}) Bet=${pos.bet:.2f} "
                    f"Shares={pos.shares:.2f} Held={held_min:.0f}m via={','.join(wallets)}"
                )
        else:
            lines.append("  (no open positions)")
        lines.append("")

        sigs = self._last_signals
        lines.append(f"[SIGNALS ({len(sigs)})]")
        for i, s in enumerate(sigs, 1):
            lines.append(
                f"  #{i} [{s.tier}|{s.score:.0f}] {s.title[:60]} [{s.outcome}] "
                f"Price=${s.cur:.4f} WEntry=${s.avg_entry:.4f} Drift={s.drift*100:+.1f}% "
                f"via={','.join(s.names[:5])}"
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
            typ     = t.type or "?"
            icon    = "BUY" if typ == "BUY" else ("WIN" if (t.pnl_usdc or 0) >= 0 else "LOSS")
            pnl_str = f" PnL=${(t.pnl_usdc or 0):+.4f}({(t.pnl_pct or 0):+.1f}%)" if typ == "SELL" else ""
            wallet_str = ",".join(t.wallet_names[:2]) or "?"
            lines.append(
                f"  [{icon}|{t.tier or '?'}] {t.ts_str or '?'} "
                f"{t.title[:40]} [{t.outcome}] "
                f"Price=${t.price:.4f} Bet=${t.bet:.2f}"
                f"{pnl_str} via={wallet_str}"
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
            f"  Watchlist       : {len(_TS.get_watchlist())}",
            f"  Elite Count     : {sum(1 for p in _w().wallet_cache.values() if p.get('elite'))}",
            "└─────────────────────────────────────────────────────────────────────┘", "",
        ]

        lines.append("┌─ OPEN POSITIONS ────────────────────────────────────────────────────┐")
        if _w().open_positions:
            for key, pos in _w().open_positions.items():
                cid, outcome = key if isinstance(key, tuple) else (str(key), "?")
                entry    = pos.entry_price
                cur      = pos.cur_price or entry
                pnl_pct  = (cur - entry) / max(entry, 0.001) * 100
                pnl_abs  = (cur - entry) * pos.shares
                held_min = (_t.time() - pos.entry_ts) / 60 if pos.entry_ts else 0.0
                wallets   = pos.elite_names or [w[:10]+"…" for w in pos.elite_wallets]
                lines += [
                    f"  [{pos.tier}] {pos.title[:60]}",
                    f"    Outcome: {outcome}  Score: {pos.score:.0f}  HFT: {'YES' if pos.is_hft else 'NO'}",
                    f"    Wallet Entry: ${pos.avg_entry or entry:.4f}  Our Entry: ${entry:.4f}  "
                    f"Now: ${cur:.4f}  P&L: {pnl_pct:+.1f}% (${pnl_abs:+.3f})",
                    f"    Bet: ${pos.bet:.2f}  Shares: {pos.shares:.2f}  "
                    f"Held: {held_min:.0f}min",
                    f"    Elite Wallets: {', '.join(wallets)}",
                    sep2,
                ]
        else:
            lines.append("  (no open positions)")
        lines += ["└─────────────────────────────────────────────────────────────────────┘", ""]

        lines.append("┌─ ACTIVE SIGNALS (last cycle) ───────────────────────────────────────┐")
        sigs = self._last_signals
        for i, s in enumerate(sigs, 1):
            lines += [
                f"  #{i} [{s.tier}] Score:{s.score:.0f}  {s.title[:60]}",
                f"     [{s.outcome}]  CurPrice: ${s.cur:.4f}  "
                f"WalletEntry: ${s.avg_entry:.4f}  Drift: {s.drift*100:+.1f}%",
                f"     via: {', '.join(s.names[:5])}",
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
            typ  = t.type or "?"
            icon = "🛒" if typ == "BUY" else ("✅" if (t.pnl_usdc or 0) >= 0 else "❌")
            pnl_str = f"P&L ${(t.pnl_usdc or 0):+.4f} ({(t.pnl_pct or 0):+.1f}%)" if typ == "SELL" else ""
            wallet_str = ", ".join(t.wallet_names[:2]) or "?"
            lines.append(
                f"  {icon} {t.ts_str or '?'}  {typ:<4}  [{t.tier or '?'}]  "
                f"{t.title[:36]}  [{t.outcome}]"
                f"  Price:${t.price:.4f}  Bet:${t.bet:.2f}"
                f"  {pnl_str}  via:{wallet_str}"
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
