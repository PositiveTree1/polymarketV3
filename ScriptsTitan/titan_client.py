"""MCP HTTP client that duck-types TitanAPI.

Used by `run_titan.py --mode client` to connect the Tkinter UI to a remote
`titan_server.py` process over Streamable HTTP (MCP spec 2025-11-25).
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from collections import defaultdict
from typing import Callable


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

    def _post(self, body: dict) -> dict:
        self._ready.wait(timeout=6)
        data = json.dumps(body).encode()
        req = urllib.request.Request(
            f"{self._base_url}/mcp",
            data=data,
            headers=self._headers(),
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            if not self._sid:
                self._sid = resp.headers.get("MCP-Session-Id")
            return json.loads(resp.read())

    def _call_tool(self, name: str, arguments: dict | None = None) -> object:
        response = self._post({
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        })
        if "error" in response:
            raise RuntimeError(response["error"]["message"])
        result = response.get("result", {})
        structured = result.get("structuredContent")
        if structured is not None:
            return structured.get("result", structured)
        content = result.get("content", [{}])
        text = content[0].get("text", "") if content else ""
        try:
            return json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return text

    def _init_async(self) -> None:
        try:
            self._initialize()
        except Exception as e:
            print(f"[TitanClient] MCP handshake failed ({self._base_url}): {e}")
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
        except Exception:
            pass

    # ── SSE subscription thread ───────────────────────────────────────────────

    def _start_sse(self) -> None:
        self._sse_running = True
        self._sse_thread = threading.Thread(target=self._sse_loop, daemon=True, name="titan-sse")
        self._sse_thread.start()

    def _sse_loop(self) -> None:
        while self._sse_running:
            try:
                self._sse_connect()
            except Exception:
                pass
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
        conn = http.client.HTTPConnection(host, port, timeout=None)
        try:
            conn.request("GET", parsed.path or "/mcp", headers=headers)
            resp = conn.getresponse()
            if resp.status != 200:
                raise RuntimeError(f"SSE HTTP {resp.status}")
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

    def _dispatch_notification(self, msg: dict) -> None:
        method = msg.get("method", "")
        params = msg.get("params") or {}
        for cb in list(self._subscribers.get(method, [])):
            try:
                cb(params)
            except Exception:
                pass

    # ── TitanAPI duck-typed interface ─────────────────────────────────────────

    def start(self) -> None:
        self._start_sse()

    def stop(self) -> None:
        self._sse_running = False

    def status(self) -> dict:
        return self._call_tool("status")  # type: ignore[return-value]

    def get_positions(self, brief: bool = True) -> list[dict]:
        return self._call_tool("get_positions", {"brief": brief})  # type: ignore[return-value]

    def get_closed_positions(self, limit: int = 200) -> list[dict]:
        return self._call_tool("get_closed_positions", {"limit": limit})  # type: ignore[return-value]

    def get_signals(self, min_score: float = 0.0) -> list[dict]:
        return self._call_tool("get_signals", {"min_score": min_score})  # type: ignore[return-value]

    def get_signal_history(self, limit: int = 200, min_score: float = 0.0, cid: str | None = None) -> list[dict]:
        args: dict = {"limit": limit, "min_score": min_score}
        if cid:
            args["cid"] = cid
        return self._call_tool("get_signal_history", args)  # type: ignore[return-value]

    def get_rejects(self) -> list[str]:
        return self._call_tool("get_rejects")  # type: ignore[return-value]

    def get_alerts(self) -> list[dict]:
        return self._call_tool("get_alerts")  # type: ignore[return-value]

    def get_whales(self) -> list[dict]:
        return self._call_tool("get_whales")  # type: ignore[return-value]

    def get_pnl_summary(self) -> dict:
        return self._call_tool("get_pnl_summary")  # type: ignore[return-value]

    def get_trade_history(self) -> list[dict]:
        return self._call_tool("get_trade_history")  # type: ignore[return-value]

    def get_config(self) -> dict:
        return self._call_tool("get_config")  # type: ignore[return-value]

    def get_logs(self, lines: int = 200) -> str:
        return self._call_tool("get_logs", {"lines": lines})  # type: ignore[return-value]

    def get_snapshot(self, compressed: bool = True) -> str:
        return self._call_tool("get_snapshot", {"compressed": compressed})  # type: ignore[return-value]

    def get_status(self) -> dict:
        return self._call_tool("get_status")  # type: ignore[return-value]

    def get_portfolio_overview(self) -> dict:
        return self._call_tool("get_portfolio_overview")  # type: ignore[return-value]

    def get_recent_errors(self, limit: int = 20) -> list[dict]:
        return self._call_tool("get_recent_errors", {"limit": limit})  # type: ignore[return-value]

    def force_cycle(self) -> None:
        self._call_tool("force_cycle")

    def pause(self) -> None:
        self._call_tool("pause")

    def resume(self) -> None:
        self._call_tool("resume")

    def update_config(self, patch: dict, dry_run: bool = False) -> dict:
        return self._call_tool("update_config", {"patch": patch, "dry_run": dry_run})  # type: ignore[return-value]

    def subscribe(self, event: str, callback: Callable) -> None:
        self._subscribers[event].append(callback)

    def unsubscribe(self, event: str, callback: Callable) -> None:
        try:
            self._subscribers[event].remove(callback)
        except ValueError:
            pass
