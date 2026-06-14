"""MCP Streamable HTTP server for TitanAPI (spec 2025-11-25).

POST /mcp  — JSON-RPC 2.0 requests + responses
GET  /mcp  — SSE stream for server-initiated notifications

Usage:
    python titan_server.py [--port 8765] [--token mytoken]
"""
from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time
import uuid
from collections import deque
from dataclasses import asdict, is_dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

from titan_api import TitanAPI

# ── logging ───────────────────────────────────────────────────────────────────
_LOG_DIR  = Path(__file__).parent.parent / "Logs"
_LOG_DIR.mkdir(exist_ok=True)
from titan_state import SERVER_LOG_FILE as _LOG_FILE  # single source of truth

def _log(msg: str, level: str = "INFO") -> None:
    from datetime import datetime
    import titan_state as _S
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [{level:5}] {msg}"
    try:
        with open(_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    _S._log(msg, level)

def _print(msg: str) -> None:
    from datetime import datetime
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line)
    try:
        with open(_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass

# ── constants ─────────────────────────────────────────────────────────────────

_PROTO_VERSION = "2025-11-25"
_PROTO_FALLBACK = "2024-11-05"
_SSE_RING_SIZE  = 500          # events kept for Last-Event-ID resumption
_KEEPALIVE_S    = 15           # SSE heartbeat interval

# ── session / SSE state ───────────────────────────────────────────────────────

class _SSEClient:
    def __init__(self, sid: str, last_event_id: int) -> None:
        self.sid = sid
        self.last_event_id = last_event_id
        self.q: queue.Queue[str | None] = queue.Queue()

    def push(self, data: str) -> None:
        self.q.put(data)

    def close(self) -> None:
        self.q.put(None)


class _SessionManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, dict] = {}       # sid → session meta
        self._sse_clients: dict[str, _SSEClient] = {}
        self._ring: deque[tuple[int, str]] = deque(maxlen=_SSE_RING_SIZE)
        self._event_id: int = 0

    def new_session(self) -> str:
        sid = uuid.uuid4().hex
        with self._lock:
            self._sessions[sid] = {"initialized": False, "proto": _PROTO_VERSION}
        return sid

    def get_session(self, sid: str) -> dict | None:
        return self._sessions.get(sid)

    def mark_initialized(self, sid: str, proto: str) -> None:
        with self._lock:
            if sid in self._sessions:
                self._sessions[sid]["initialized"] = True
                self._sessions[sid]["proto"] = proto

    def add_sse_client(self, sid: str, last_id: int) -> _SSEClient:
        client = _SSEClient(sid, last_id)
        with self._lock:
            self._sse_clients[sid] = client
            # replay missed events
            for eid, data in self._ring:
                if eid > last_id:
                    client.push(data)
        return client

    def remove_sse_client(self, sid: str) -> None:
        with self._lock:
            self._sse_clients.pop(sid, None)

    def broadcast(self, notification: dict) -> None:
        with self._lock:
            self._event_id += 1
            eid = self._event_id
            frame = f"id: {eid}\ndata: {json.dumps(notification)}\n\n"
            self._ring.append((eid, frame))
            for client in list(self._sse_clients.values()):
                client.push(frame)


_sessions = _SessionManager()


def _to_serializable(value: object) -> object:
    from titan_position import Position as _Position

    if isinstance(value, _Position):
        return value.to_dict()
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, list):
        return [_to_serializable(item) for item in value]
    if isinstance(value, tuple):
        return [_to_serializable(item) for item in value]
    if isinstance(value, dict):
        return {key: _to_serializable(item) for key, item in value.items()}
    return value


# ── tool registry (built from @mcp_tool decorators) ──────────────────────────

def _build_tool_list(api: TitanAPI) -> list[dict]:
    tools = []
    for name in dir(api):
        method = getattr(api, name, None)
        if callable(method) and hasattr(method, "_mcp_tool"):
            meta = method._mcp_tool.copy()
            if "required" not in meta.get("inputSchema", {}):
                meta["inputSchema"]["required"] = []
            if name == "query_db":
                try:
                    import titan_db as _DB
                    schema = _DB.get_schema_description()
                    meta = {**meta, "description": meta["description"] + f" Schema: {schema}"}
                except Exception:
                    pass
            tools.append(meta)
    return tools


# ── resource definitions ──────────────────────────────────────────────────────

_RESOURCES = [
    {
        "uri": "titan://config",
        "name": "Titan Config",
        "description": "Live titan_config.json content",
        "mimeType": "application/json",
    },
    {
        "uri": "titan://snapshot",
        "name": "Titan Snapshot",
        "description": "Current runtime snapshot (AI-digestible)",
        "mimeType": "text/plain",
    },
    {
        "uri": "titan://logs",
        "name": "Titan Logs",
        "description": "Recent engine log tail",
        "mimeType": "text/plain",
    },
    {
        "uri": "titan://wallets",
        "name": "Elite wallet Roster",
        "description": "Current elite wallet list with performance metrics",
        "mimeType": "application/json",
    },
]


def _read_resource(uri: str, api: TitanAPI) -> str:
    if uri == "titan://config":
        return json.dumps(api.get_config(), indent=2)
    if uri == "titan://snapshot":
        return api.get_snapshot(compressed=True)
    if uri == "titan://logs":
        return api.get_logs(lines=200)
    if uri == "titan://wallets":
        return json.dumps(api.get_tracked_wallets(), indent=2)
    return f"ERROR: unknown resource URI '{uri}'"


# ── prompt definitions ─────────────────────────────────────────────────────────

_PROMPTS = [
    {
        "name": "titan_analysis",
        "description": "Analyse Titan's current open positions and engine state.",
        "arguments": [],
    },
    {
        "name": "titan_signal_review",
        "description": "Review current whale signals and recommend actions.",
        "arguments": [],
    },
    {
        "name": "titan_whale_brief",
        "description": "Summarise recent elite whale activity.",
        "arguments": [],
    },
]


def _get_prompt(name: str, api: TitanAPI) -> dict:
    if name == "titan_analysis":
        snapshot = api.get_snapshot(compressed=True)
        return {
            "description": _PROMPTS[0]["description"],
            "messages": [
                {
                    "role": "user",
                    "content": {
                        "type": "text",
                        "text": f"Here is the current Titan engine state:\n\n{snapshot}\n\nAnalyse the open positions and overall performance.",
                    },
                }
            ],
        }
    if name == "titan_signal_review":
        sigs = api.get_signals()
        sigs_text = json.dumps(sigs, indent=2)
        return {
            "description": _PROMPTS[1]["description"],
            "messages": [
                {
                    "role": "user",
                    "content": {
                        "type": "text",
                        "text": f"Review these Titan signals and recommend which to act on:\n\n{sigs_text}",
                    },
                }
            ],
        }
    if name == "titan_whale_brief":
        wallets = api.get_tracked_wallets()
        wallets_text = json.dumps(wallets, indent=2)
        return {
            "description": _PROMPTS[2]["description"],
            "messages": [
                {
                    "role": "user",
                    "content": {
                        "type": "text",
                        "text": f"Summarise recent activity from these elite Polymarket wallets:\n\n{wallets_text}",
                    },
                }
            ],
        }
    raise KeyError(f"unknown prompt '{name}'")


# ── JSON-RPC helpers ──────────────────────────────────────────────────────────

def _ok(id: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": id, "result": result}


def _err(id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": id, "error": {"code": code, "message": message}}


def _text_result(text: str, structured: dict | None = None) -> dict:
    r: dict = {"content": [{"type": "text", "text": text}]}
    if structured is not None:
        r["structuredContent"] = structured
    return r


# ── request dispatcher ────────────────────────────────────────────────────────

def _dispatch(body: dict, sid: str | None, api: TitanAPI) -> dict | None:
    method = body.get("method", "")
    params = body.get("params") or {}
    rid    = body.get("id")      # None for notifications

    # ── initialize ────────────────────────────────────────────────────────────
    if method == "initialize":
        client_proto = params.get("protocolVersion", _PROTO_VERSION)
        negotiated   = _PROTO_VERSION if client_proto >= _PROTO_FALLBACK else _PROTO_FALLBACK
        if sid:
            _sessions.mark_initialized(sid, negotiated)
        client_info  = params.get("clientInfo", {})
        client_name  = client_info.get("name", "unknown")
        client_ver   = client_info.get("version", "?")
        _log(f"Client connected: {client_name} v{client_ver}  proto={negotiated}  sid={sid}", "INFO")
        return _ok(rid, {
            "protocolVersion": negotiated,
            "capabilities": {
                "tools":     {"listChanged": True},
                "resources": {"listChanged": True, "subscribe": True},
                "prompts":   {"listChanged": False},
                "logging":   {},
            },
            "serverInfo": {"name": "titan", "version": "1.0"},
        })

    # ── notifications/initialized (client→server, no response) ───────────────
    if method == "notifications/initialized":
        return None

    # ── ping ─────────────────────────────────────────────────────────────────
    if method == "ping":
        return _ok(rid, {})

    # ── tools/list ───────────────────────────────────────────────────────────
    if method == "tools/list":
        return _ok(rid, {"tools": _build_tool_list(api)})

    # ── tools/call ───────────────────────────────────────────────────────────
    if method == "tools/call":
        tool_name = params.get("name", "")
        arguments = params.get("arguments") or {}
        method_fn = getattr(api, tool_name, None)
        if method_fn is None or not hasattr(method_fn, "_mcp_tool"):
            return _err(rid, -32601, f"Unknown tool: {tool_name}")
        try:
            result = method_fn(**arguments)
        except TypeError as e:
            return _err(rid, -32602, f"Invalid arguments for {tool_name}: {e}")
        except Exception as e:
            return _err(rid, -32603, f"Tool error: {e}")

        if result is None:
            return _ok(rid, _text_result(f"{tool_name} completed."))
        if isinstance(result, str):
            return _ok(rid, _text_result(result))
        serializable = _to_serializable(result)
        if isinstance(serializable, (list, dict)):
            if isinstance(serializable, list):
                structured: dict = {"result": serializable}
                text = json.dumps(serializable, indent=2, default=str)
            else:
                structured = serializable
                text = json.dumps(serializable, indent=2, default=str)
            return _ok(rid, _text_result(text, structured))
        return _ok(rid, _text_result(str(result)))

    # ── resources/list ───────────────────────────────────────────────────────
    if method == "resources/list":
        return _ok(rid, {"resources": _RESOURCES})

    # ── resources/read ───────────────────────────────────────────────────────
    if method == "resources/read":
        uri = params.get("uri", "")
        content = _read_resource(uri, api)
        mime = next((r["mimeType"] for r in _RESOURCES if r["uri"] == uri), "text/plain")
        return _ok(rid, {
            "contents": [{"uri": uri, "mimeType": mime, "text": content}]
        })

    # ── resources/subscribe ──────────────────────────────────────────────────
    if method == "resources/subscribe":
        return _ok(rid, {})

    # ── resources/unsubscribe ─────────────────────────────────────────────────
    if method == "resources/unsubscribe":
        return _ok(rid, {})

    # ── prompts/list ─────────────────────────────────────────────────────────
    if method == "prompts/list":
        return _ok(rid, {"prompts": _PROMPTS})

    # ── prompts/get ──────────────────────────────────────────────────────────
    if method == "prompts/get":
        name = params.get("name", "")
        try:
            prompt = _get_prompt(name, api)
        except KeyError as e:
            return _err(rid, -32601, str(e))
        return _ok(rid, prompt)

    # ── logging/setLevel ─────────────────────────────────────────────────────
    if method == "logging/setLevel":
        return _ok(rid, {})

    return _err(rid, -32601, f"Method not found: {method}")


# ── HTTP handler ──────────────────────────────────────────────────────────────

class _Handler(BaseHTTPRequestHandler):
    api: TitanAPI
    token: str | None

    def log_message(self, fmt: str, *args: Any) -> None:
        pass  # suppress default Apache-style logging

    def _check_auth(self) -> bool:
        if not self.token:
            return True
        auth = self.headers.get("Authorization", "")
        return auth == f"Bearer {self.token}"

    def _get_or_create_sid(self) -> str:
        sid = self.headers.get("MCP-Session-Id")
        if sid and _sessions.get_session(sid):
            return sid
        return _sessions.new_session()

    def _send_json(self, status: int, body: dict, sid: str | None = None) -> None:
        data = json.dumps(body, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        if sid:
            self.send_header("MCP-Session-Id", sid)
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/mcp":
            self._send_json(404, _err(None, -32600, "Not found"))
            return
        if not self._check_auth():
            self._send_json(401, _err(None, -32600, "Unauthorized"))
            return

        sid = self._get_or_create_sid()
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            self._send_json(400, _err(None, -32700, "Parse error"), sid)
            return

        response = _dispatch(body, sid, self.api)
        if response is None:
            # notification — no response body
            self.send_response(204)
            self.send_header("MCP-Session-Id", sid)
            self.end_headers()
        else:
            self._send_json(200, response, sid)

    def do_GET(self) -> None:  # noqa: N802
        if self.path not in ("/mcp", "/mcp?"):
            self._send_json(404, _err(None, -32600, "Not found"))
            return
        if not self._check_auth():
            self._send_json(401, _err(None, -32600, "Unauthorized"))
            return
        if self.headers.get("Accept", "") not in ("text/event-stream", ""):
            self._send_json(406, _err(None, -32600, "Accept must be text/event-stream"))
            return

        sid = self._get_or_create_sid()
        try:
            last_id = int(self.headers.get("Last-Event-ID", "0"))
        except ValueError:
            last_id = 0

        client = _sessions.add_sse_client(sid, last_id)
        _log(f"SSE stream opened  sid={sid}  addr={self.client_address[0]}", "INFO")

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("MCP-Session-Id", sid)
        self.end_headers()

        try:
            while True:
                try:
                    frame = client.q.get(timeout=_KEEPALIVE_S)
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    continue
                if frame is None:
                    break
                self.wfile.write(frame.encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            _sessions.remove_sse_client(sid)
            _log(f"SSE stream closed  sid={sid}  addr={self.client_address[0]}", "INFO")


# ── event bus → SSE bridge ────────────────────────────────────────────────────

def _wire_events(api: TitanAPI) -> None:
    def _notify(method: str):
        def _cb(payload) -> None:
            _sessions.broadcast({"jsonrpc": "2.0", "method": method, "params": payload})
        return _cb

    def _on_log(payload: dict) -> None:
        from datetime import datetime
        level = payload.get("level", "INFO")
        msg   = payload.get("data", "")
        line  = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [{level:5}] {msg}"
        try:
            with open(_LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass
        if str(level).upper() in {"ERR", "ERROR", "CRITICAL"}:
            print(line, flush=True)
        _sessions.broadcast({
            "jsonrpc": "2.0",
            "method": "notifications/message",
            "params": {
                "level": str(level).lower(),
                "logger": "titan",
                "data": msg,
            },
        })

    def _on_cycle(payload: dict) -> None:
        import dataclasses
        def _ser(obj):
            return dataclasses.asdict(obj) if dataclasses.is_dataclass(obj) and not isinstance(obj, type) else obj
        trades_serial  = [_ser(t) for t in payload.get("trades", [])]
        signals_serial = [_ser(s) for s in payload.get("signals", [])]
        _sessions.broadcast({
            "jsonrpc": "2.0",
            "method": "titan/cycle_complete",
            "params": {
                "signals": signals_serial,
                "wallets": payload.get("wallets", []),
                "rejects": payload.get("rejects", []),
                "trades": trades_serial,
                "cycle": None,
                "elapsed_ms": None,
            },
        })
        # also notify that snapshot / logs resources changed
        for uri in ("titan://snapshot", "titan://logs"):
            _sessions.broadcast({
                "jsonrpc": "2.0",
                "method": "notifications/resources/updated",
                "params": {"uri": uri},
            })

    def _on_position(method: str):
        def _cb(payload) -> None:
            _sessions.broadcast({"jsonrpc": "2.0", "method": method, "params": _to_serializable(payload)})
        return _cb

    api.subscribe("notifications/message", _on_log)
    api.subscribe("titan/cycle_complete",  _on_cycle)
    api.subscribe("titan/heartbeat",       _notify("titan/heartbeat"))
    api.subscribe("titan/position_open",   _on_position("titan/position_open"))
    api.subscribe("titan/position_close",  _on_position("titan/position_close"))


# ── server entry ──────────────────────────────────────────────────────────────

def _rotate_server_log() -> None:
    from datetime import datetime
    import shutil
    log = Path(_LOG_FILE)
    if log.exists():
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        shutil.move(str(log), str(log.parent / f"titan_server_{ts}.log"))


def run_server(api: TitanAPI, host: str = "127.0.0.1", port: int = 8765, token: str | None = None) -> None:
    tool_count = len(_build_tool_list(api))
    _print(f"MCP {_PROTO_VERSION}  {host}:{port}  tools={tool_count}  auth={'yes' if token else 'no'}")

    _wire_events(api)

    class _ThreadingServer(ThreadingMixIn, HTTPServer):
        daemon_threads = True

    handler_cls = type("_H", (_Handler,), {"api": api, "token": token})
    server = _ThreadingServer((host, port), handler_cls)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        _print("Server stopped")


if __name__ == "__main__":
    import argparse

    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    p = argparse.ArgumentParser(prog="titan_server")
    p.add_argument("--host",  default="127.0.0.1")
    p.add_argument("--port",  type=int, default=8765)
    p.add_argument("--token", default=None, help="Optional bearer token for auth")
    args = p.parse_args()

    _api = TitanAPI(enable_telegram=True)
    _api.start()
    run_server(_api, host=args.host, port=args.port, token=args.token)
