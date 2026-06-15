"""MCP HTTP client that duck-types TitanAPI.

Used by `run_titan.py --mode client` to connect the Tkinter UI to a remote
`titan_server.py` process over Streamable HTTP (MCP spec 2025-11-25).
"""
from __future__ import annotations

import json
import threading
import time
import urllib.request
import urllib.error
from collections.abc import Mapping
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Callable
from titan_trade import TradeRecord

# ── logging ───────────────────────────────────────────────────────────────────
_LOG_DIR  = Path(__file__).parent.parent / "Logs"
_LOG_DIR.mkdir(exist_ok=True)
_LOG_FILE = _LOG_DIR / "titan_client.log"

_debug_enabled: bool = False

def _log(msg: str, level: str = "INFO") -> None:
    if level == "DEBUG" and not _debug_enabled:
        return
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [{level:5}] {msg}"
    if level.upper() in {"ERR", "ERROR", "CRITICAL"}:
        print(line, flush=True)
    try:
        with open(_LOG_FILE, "ab") as f:
            f.write((line + "\n").encode("utf-8"))
    except Exception:
        pass

def _print(msg: str) -> None:
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line)
    try:
        with open(_LOG_FILE, "ab") as f:
            f.write((line + "\n").encode("utf-8"))
    except Exception:
        pass

if TYPE_CHECKING:
    from titan_signals import Signal
    from titan_position import Position
    from titan_wallet import Wallet
    from titan_trade import TradeRecord
    from titan_types import (
        AlertDict, ErrorDict,
        PnlSummaryDict, TradeStatsDict, PortfolioOverviewDict,
    )


class TitanClient:
    def __init__(self, base_url: str = "http://127.0.0.1:8765", token: str | None = None) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._sid: str | None = None
        self._id_counter = 0
        self._id_lock = threading.Lock()
        self._subscribers: dict[str, list[Callable]] = defaultdict(list)
        self._sse_thread: threading.Thread | None = None
        self._sse_running = False
        self._last_event_id: int = 0

        self._ready = threading.Event()
        self._server_offline = False
        _print(f"Connecting to {self._base_url}")
        threading.Thread(target=self._init_async, daemon=True, name="titan-init").start()

    # ── internal helpers ──────────────────────────────────────────────────────

    def _next_id(self) -> int:
        with self._id_lock:
            self._id_counter += 1
            return self._id_counter

    def _headers(self, extra: dict | None = None) -> dict:
        h = {"Content-Type": "application/json"}
        if self._token:
            h["Authorization"] = f"Bearer {self._token}"
        if self._sid:
            h["MCP-Session-Id"] = self._sid
        if extra:
            h.update(extra)
        return h

    def _require_mapping(self, value: object, context: str) -> Mapping[str, object]:
        if isinstance(value, Mapping):
            return value
        raise RuntimeError(f"Invalid {context}: expected object, got {type(value).__name__}")

    def _extract_text_result(self, result: Mapping[str, object]) -> object:
        content_value = result.get("content")
        if not isinstance(content_value, list) or not content_value:
            return ""

        first_item = content_value[0]
        if not isinstance(first_item, Mapping):
            return ""

        text_value = first_item.get("text")
        if not isinstance(text_value, str):
            return ""

        try:
            return json.loads(text_value)
        except (json.JSONDecodeError, TypeError):
            return text_value

    def _post(self, body: dict) -> dict:
        self._ready.wait(timeout=6)
        data = json.dumps(body).encode()
        req = urllib.request.Request(
            f"{self._base_url}/mcp",
            data=data,
            headers=self._headers(),
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                if not self._sid:
                    self._sid = resp.headers.get("MCP-Session-Id")
                payload = json.loads(resp.read())
                if not isinstance(payload, dict):
                    raise RuntimeError(f"Invalid JSON-RPC response type: {type(payload).__name__}")
                if self._server_offline:
                    self._server_offline = False
                    _print(f"Server reconnected ({self._base_url})")
                return payload
        except urllib.error.URLError as e:
            if not self._server_offline:
                self._server_offline = True
                _log(f"Server unreachable ({self._base_url}): {e.reason}", "ERR")
            raise

    def _call_tool(self, name: str, arguments: dict | None = None) -> object:
        response = self._post({
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        })
        if "error" in response:
            error_value = self._require_mapping(response["error"], "JSON-RPC error")
            message_value = error_value.get("message")
            if isinstance(message_value, str):
                raise RuntimeError(message_value)
            raise RuntimeError(f"Tool call failed without message for {name}")

        result_value = response.get("result")
        if result_value is None:
            return None
        if not isinstance(result_value, Mapping):
            return result_value

        structured_value = result_value.get("structuredContent")
        if isinstance(structured_value, Mapping):
            if "result" in structured_value:
                return structured_value["result"]
            return structured_value
        if structured_value is not None:
            return structured_value

        return self._extract_text_result(result_value)

    def _list_tools_raw(self) -> list[dict]:
        response = self._post({
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "tools/list",
            "params": {},
        })
        if "error" in response:
            error_value = self._require_mapping(response["error"], "JSON-RPC error")
            message_value = error_value.get("message")
            if isinstance(message_value, str):
                raise RuntimeError(message_value)
            raise RuntimeError("tools/list failed without message")
        result_value = self._require_mapping(response.get("result"), "tools/list result")
        tools_value = result_value.get("tools")
        if isinstance(tools_value, list):
            return [tool for tool in tools_value if isinstance(tool, dict)]
        return []

    def _init_async(self) -> None:
        try:
            self._initialize()
            _print(f"MCP handshake OK ({self._base_url})")
        except Exception as e:
            _log(f"MCP handshake failed ({self._base_url}): {e}", "ERR")
        finally:
            self._ready.set()

    def _initialize(self) -> None:
        response = self._post({
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "titan_client", "version": "1.0"},
            },
        })
        if "error" in response:
            raise RuntimeError(f"MCP initialize failed: {response['error']['message']}")
        # send notifications/initialized (fire-and-forget, server returns 204)
        try:
            data = json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}).encode()
            req = urllib.request.Request(
                f"{self._base_url}/mcp",
                data=data,
                headers=self._headers(),
                method="POST",
            )
            urllib.request.urlopen(req, timeout=5).close()
        except Exception as e:
            _log(f"notifications/initialized failed: {e}", "WARN")

    # ── SSE subscription thread ───────────────────────────────────────────────

    def _start_sse(self) -> None:
        self._sse_running = True
        self._sse_thread = threading.Thread(target=self._sse_loop, daemon=True, name="titan-sse")
        self._sse_thread.start()

    def _sse_loop(self) -> None:
        while self._sse_running:
            try:
                self._sse_connect()
            except Exception as e:
                _log(f"SSE connection error: {e}", "WARN")
            if self._sse_running:
                time.sleep(3)

    def _sse_connect(self) -> None:
        import http.client, urllib.parse
        parsed = urllib.parse.urlparse(self._base_url)
        host = parsed.hostname
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        headers = self._headers({"Accept": "text/event-stream"})
        if self._last_event_id:
            headers["Last-Event-ID"] = str(self._last_event_id)
        if not host:
            raise RuntimeError("Invalid MCP URL, missing hostname")
        
        conn = http.client.HTTPConnection(host, port, timeout=None)
        try:
            conn.request("GET", parsed.path or "/mcp", headers=headers)
            resp = conn.getresponse()
            if resp.status != 200:
                raise RuntimeError(f"SSE HTTP {resp.status}")
            _log(f"SSE stream connected ({self._base_url})")
            buf = ""
            while self._sse_running:
                raw = resp.fp.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                if line.startswith("id:"):
                    try:
                        self._last_event_id = int(line[3:].strip())
                    except ValueError:
                        pass
                elif line.startswith("data:"):
                    buf = line[5:].strip()
                elif line == "" and buf:
                    try:
                        msg = json.loads(buf)
                        self._dispatch_notification(msg)
                    except json.JSONDecodeError:
                        pass
                    buf = ""
        finally:
            conn.close()

    _LOGGABLE_EVENTS  = {"titan/position_open", "titan/position_close", "titan/cycle_complete"}
    _DEBUG_EVENTS     = {"titan/heartbeat"}

    def _dispatch_notification(self, msg: dict) -> None:
        method = msg.get("method", "")
        params = msg.get("params") or {}
        if method in self._LOGGABLE_EVENTS:
            _log(f"← {method} {params}")
        elif method in self._DEBUG_EVENTS:
            _log(f"← {method} {params}", "DEBUG")
        for cb in list(self._subscribers.get(method, [])):
            try:
                cb(params)
            except Exception as e:
                _log(f"Notification callback error [{method}]: {e}", "ERR")

    # ── TitanAPI duck-typed interface ─────────────────────────────────────────

    def start(self) -> None:
        self._start_sse()

    def stop(self) -> None:
        self._sse_running = False

    def status(self) -> dict:
        return self._call_tool("status")  # type: ignore[return-value]

    def get_positions(self) -> list[Position]:
        from titan_position import Position as _Position
        raw = self._call_tool("get_positions")
        if isinstance(raw, list):
            return [_Position.from_dict(p) if isinstance(p, dict) else p for p in raw]
        return []

    def list_tools(self) -> list[dict]:
        return self._list_tools_raw()

    def call_tool(self, name: str, arguments: dict | None = None) -> object:
        return self._call_tool(name, arguments)  # type: ignore[return-value]

    def get_closed_positions(self, limit: int = 200) -> list[Position]:
        from titan_position import Position as _Position
        raw = self._call_tool("get_closed_positions", {"limit": limit})
        if isinstance(raw, list):
            return [_Position.from_dict(p) if isinstance(p, dict) else p for p in raw]
        return []

    def get_signals(self, min_score: float = 0.0) -> list[Signal]:
        return self._call_tool("get_signals", {"min_score": min_score})  # type: ignore[return-value]

    def get_signal_history(self, limit: int = 200, min_score: float = 0.0, cid: str | None = None) -> list[Signal]:
        args: dict = {"limit": limit, "min_score": min_score}
        if cid:
            args["cid"] = cid
        return self._call_tool("get_signal_history", args)  # type: ignore[return-value]

    def get_rejects(self) -> list[str]:
        return self._call_tool("get_rejects")  # type: ignore[return-value]

    def get_alerts(self) -> list[AlertDict]:
        return self._call_tool("get_alerts")  # type: ignore[return-value]

    def apply_selector(self) -> int:
        result = self._call_tool("apply_selector")
        return int(result) if isinstance(result, (int, float)) else 0

    def get_tracked_wallets(self, search: str = "", tier: str = "") -> list["Wallet"]:
        from titan_wallet import Wallet as _Wallet
        params: dict[str, str] = {}
        if search:
            params["search"] = search
        if tier:
            params["tier"] = tier
        raw = self._call_tool("get_tracked_wallets", params or None)
        if isinstance(raw, list):
            return [_Wallet.from_db(d["wallet"], d) if isinstance(d, dict) else d for d in raw]
        return []

    def get_pnl_summary(self) -> PnlSummaryDict:
        return self._call_tool("get_pnl_summary")  # type: ignore[return-value]

    def get_asset_price_history(self, asset: str) -> list[tuple[float, float]]:
        result = self._call_tool("get_asset_price_history", {"asset": asset})
        if not isinstance(result, list):
            return []
        return [(float(ts), float(v)) for ts, v in result]

    def get_trade_history(self) -> list[TradeRecord]:
        raw_value = self._call_tool("get_trade_history")
        if not isinstance(raw_value, list):
            raise RuntimeError(f"Invalid get_trade_history payload: expected list, got {type(raw_value).__name__}")
        return [
            item if isinstance(item, TradeRecord)
            else TradeRecord.from_mapping(item)
            for item in raw_value
            if isinstance(item, Mapping | TradeRecord)
        ]

    def get_trade_stats(self) -> TradeStatsDict:
        return self._call_tool("get_trade_stats")  # type: ignore[return-value]

    def get_config(self) -> dict:
        return self._call_tool("get_config")  # type: ignore[return-value]

    def get_logs(self, lines: int = 200) -> str:
        return self._call_tool("get_logs", {"lines": lines})  # type: ignore[return-value]

    def get_snapshot(self, compressed: bool = True) -> str:
        return self._call_tool("get_snapshot", {"compressed": compressed})  # type: ignore[return-value]

    def get_status(self) -> dict:
        return self._call_tool("get_status")  # type: ignore[return-value]

    def get_portfolio_overview(self) -> PortfolioOverviewDict:
        return self._call_tool("get_portfolio_overview")  # type: ignore[return-value]

    def get_recent_errors(self, limit: int = 20) -> list[ErrorDict]:
        return self._call_tool("get_recent_errors", {"limit": limit})  # type: ignore[return-value]

    def force_cycle(self) -> None:
        _log("force_cycle requested")
        self._call_tool("force_cycle")

    def pause(self) -> None:
        _log("pause requested")
        self._call_tool("pause")

    def resume(self) -> None:
        _log("resume requested")
        self._call_tool("resume")

    def update_config(self, patch: dict, dry_run: bool = False) -> dict:
        _log(f"update_config patch={patch} dry_run={dry_run}")
        return self._call_tool("update_config", {"patch": patch, "dry_run": dry_run})  # type: ignore[return-value]

    def subscribe(self, event: str, callback: Callable) -> None:
        self._subscribers[event].append(callback)

    def unsubscribe(self, event: str, callback: Callable) -> None:
        try:
            self._subscribers[event].remove(callback)
        except ValueError:
            pass
