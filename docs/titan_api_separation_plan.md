# TITAN — API / UI Separation Plan

---

## Implementation status

> Update this section at the start of every coding session.
> Mark each step when it is **fully working and committed**, not just when code exists.

### Phase 1 — Extract TitanAPI
| # | Task | Status | Notes |
|---|---|---|---|
| 1.1 | Create `titan_api.py` with `TitanAPI` class skeleton + `@mcp_tool` decorator | ⬜ todo | |
| 1.2 | Implement all read-only query methods (get_positions, get_signals, get_alerts, get_whales, get_pnl_summary, get_trade_history, get_config, get_logs) | ⬜ todo | Pull from `titan_state` / engine internals |
| 1.3 | Implement lifecycle methods: `start`, `stop`, `status` | ⬜ todo | Wrap `titan_engine.start()` |
| 1.4 | Implement action methods: `force_cycle`, `pause`, `resume`, `update_config` | ⬜ todo | |
| 1.5 | Implement `get_snapshot()` — move `build_ai_debug_snapshot` from `titan_ui.py` | ⬜ todo | Remove from `titan_ui.py` after moving |
| 1.6 | Implement `subscribe` / `unsubscribe` event bus (in-process callbacks) | ⬜ todo | Replaces `on_log_cb`, `on_cycle_complete_cb`, etc. |
| 1.7 | Update `titan_mcp.py` to delegate to `TitanAPI` (interim — not deleted yet) | ⬜ todo | Keeps existing `/mcp?r=snapshot` working |
| 1.8 | Smoke-test: engine starts and runs headlessly via `TitanAPI` with no Tkinter import | ⬜ todo | |

### Phase 2 — Decouple UI
| # | Task | Status | Notes |
|---|---|---|---|
| 2.1 | Refactor `titan_ui.py`: accept API object via constructor, remove engine/state imports | ⬜ todo | Keep all `telegram_notifier.*` calls untouched |
| 2.2 | Replace all direct `_TS.env().*` reads with `api.get_*()` calls | ⬜ todo | |
| 2.3 | Replace `on_log_cb` / `on_cycle_complete_cb` / `on_position_open_cb` with `api.subscribe(...)` | ⬜ todo | Telegram callbacks rewired here, same calls |
| 2.4 | Add `run_titan.py` entry point with `--mode ui` | ⬜ todo | |
| 2.5 | Smoke-test: full UI session, all tabs functional, Telegram notifications firing | ⬜ todo | |

### Phase 3 — MCP server (Streamable HTTP)
| # | Task | Status | Notes |
|---|---|---|---|
| 3.1 | Implement `titan_server.py`: `POST /mcp` dispatcher (`initialize`, `tools/list`, `tools/call`) | ⬜ todo | stdlib only |
| 3.2 | Implement `GET /mcp` SSE stream with `MCP-Session-Id` and `Last-Event-ID` resumption | ⬜ todo | In-memory ring buffer for missed events |
| 3.3 | Wire `TitanAPI` event bus → SSE broadcast to all connected clients | ⬜ todo | `titan/*` custom notifications |
| 3.4 | Implement `resources/list` + `resources/read` (config, snapshot, logs, whales) | ⬜ todo | Replaces `titan_mcp.py` resources |
| 3.5 | Implement `resources/subscribe` + `notifications/resources/updated` | ⬜ todo | |
| 3.6 | Implement `prompts/list` + `prompts/get` (titan_analysis, titan_signal_review, titan_whale_brief) | ⬜ todo | |
| 3.7 | Add optional API key bearer token auth (header `Authorization: Bearer <token>`) | ⬜ todo | Off by default for localhost |
| 3.8 | Implement `titan_client.py`: duck-types `TitanAPI`, speaks MCP over HTTP, consumes SSE stream | ⬜ todo | |
| 3.9 | Add `--mode server` and `--mode client` to `run_titan.py` | ⬜ todo | |
| 3.10 | Delete `titan_mcp.py` | ⬜ todo | Only after 3.4 is confirmed working |
| 3.11 | Smoke-test: Claude Desktop connects, discovers tools, calls `get_positions` and `force_cycle` | ⬜ todo | |
| 3.12 | Smoke-test: `--mode client` GUI works identically to `--mode ui` | ⬜ todo | |

### Phase 4 — Polish
| # | Task | Status | Notes |
|---|---|---|---|
| 4.1 | OAuth 2.1 bearer auth for internet-facing deployment (Cloudflare tunnel) | ⬜ todo | RFC 9728 metadata endpoint |
| 4.2 | Sampling hook: `TitanAPI` can invoke LLM via connected MCP client | ⬜ todo | Requires client `sampling` capability |
| 4.3 | Progress notifications for long-running tool calls | ⬜ todo | `force_cycle` streams progress frames |
| 4.4 | Optional additive Telegram MCP tools (`send_telegram_alert`) on `TitanAPI` | ⬜ todo | Additive only — existing calls unchanged |
| 4.5 | Update `docs/guide.txt` with new architecture | ⬜ todo | |

### Status legend
| Symbol | Meaning |
|---|---|
| ⬜ todo | Not started |
| 🔄 in progress | Work started, not committed |
| ✅ done | Fully working and committed to git |
| ⏸ blocked | Waiting on something — add a note |

---

## Goal

Decouple the Titan engine from the Tkinter UI so that:

1. A single `TitanAPI` class owns all business logic and exposes a clean Python interface.
2. The UI becomes a thin consumer that **only** calls `TitanAPI` methods — it no longer imports `titan_engine`, `titan_state`, or any other engine module directly.
3. `TitanAPI` is served as a **standard MCP server** (spec version `2025-11-25`, the current stable release) so any MCP-aware LLM client (Claude Desktop, Cursor, custom agents) can discover and call Titan tools with zero custom integration.
4. The UI can work in two modes transparently:
   - **In-process** — direct Python reference to `TitanAPI`, zero latency.
   - **Remote client** — speaks MCP over HTTP to a running `titan_server.py` process.

---

## Current architecture problems

| Problem | Detail |
|---|---|
| `titan_ui.py` imports `titan_engine`, `titan_state` directly | UI and engine are tightly coupled; can't run headlessly |
| `titan_mcp.py` is a custom HTTP resource server | Non-standard; read-only snapshots only; not discoverable by LLM clients |
| `build_ai_debug_snapshot()` lives in `titan_ui.py` | `titan_mcp.py` has to import the UI just to build a snapshot |
| No single authoritative API surface | Logic scattered across `titan_engine`, `titan_state`, `titan_wallet`, `titan_trader`, etc. |

---

## MCP protocol version

We target **MCP spec `2025-11-25`** (current stable). The `initialize` handshake
declares this version. The server must also accept `2024-11-05` from older clients
and negotiate down gracefully.

---

## Target architecture

```
┌────────────────────────────────────────────────────────┐
│                   titan_server.py                       │
│  Streamable HTTP transport  (default :8765)             │
│  POST /mcp  — JSON-RPC 2.0 requests & responses        │
│  GET  /mcp  — SSE stream for server-initiated messages  │
│  OAuth 2.1 bearer token auth (optional, LAN default)    │
└─────────────────────┬──────────────────────────────────┘
                      │  MCP / Streamable HTTP
         ┌────────────┴──────────────┐
         │                           │
┌────────▼─────┐           ┌─────────▼────────┐
│  titan_ui.py │           │  any MCP client   │
│  Tkinter GUI │           │  Claude Desktop   │
│  TitanClient │           │  Cursor, curl…    │
│  (thin shim) │           └──────────────────┘
└──────────────┘
         │ (in-process mode: direct Python call)
┌────────▼──────────────────────────────────────────────┐
│                    titan_api.py                        │
│  class TitanAPI                                        │
│  Single public interface to all engine functionality   │
└────────────────────────────────────────────────────────┘
         │
         ├── titan_engine.py
         ├── titan_state.py
         ├── titan_wallet.py
         ├── titan_trader.py
         ├── titan_signals.py
         └── … (all existing modules, unchanged)
```

---

## New files

### `ScriptsTitan/titan_api.py`

Single class `TitanAPI`. All methods are plain Python (no HTTP, no Tk).
Each method is decorated with `@mcp_tool(...)` carrying its description,
JSON Schema, and behavioural annotations (see Tool annotations below).

```python
class TitanAPI:
    # Lifecycle
    def start(self) -> None
    def stop(self) -> None
    def status(self) -> dict           # running?, cycle count, uptime

    # Read-only queries  (annotated readOnly=True)
    def get_positions(self) -> list[dict]
    def get_signals(self) -> list[dict]
    def get_alerts(self) -> list[dict]
    def get_whales(self) -> list[dict]
    def get_pnl_summary(self) -> dict
    def get_trade_history(self) -> list[dict]
    def get_config(self) -> dict
    def get_logs(self, lines: int = 200) -> str
    def get_snapshot(self, compressed: bool = True) -> str

    # Mutating actions  (annotated readOnly=False)
    def force_cycle(self) -> None
    def pause(self) -> None
    def resume(self) -> None
    def update_config(self, patch: dict) -> dict   # returns merged config

    # Push / subscription (in-process mode only)
    def subscribe(self, event: str, callback: callable) -> None
    def unsubscribe(self, event: str, callback: callable) -> None
```

---

### `ScriptsTitan/titan_server.py`

Implements the **MCP Streamable HTTP transport** (`2025-11-25`).

**Single endpoint, dual method:**
- `POST /mcp` — all JSON-RPC 2.0 requests and responses (tools/list, tools/call, initialize, etc.)
- `GET  /mcp` — server opens an SSE stream for server-initiated notifications

This is the key change from the older HTTP+SSE spec (which used two separate paths).
Both methods hit the same path `/mcp`. Session continuity is managed via
the `MCP-Session-Id` header. Clients that send `Last-Event-ID` get stream resumption.

Uses only stdlib (`http.server` + `json` + `threading`). Can be upgraded to
`aiohttp` later without changing the API layer.

#### MCP lifecycle

```
Client                          Server
  │── POST /mcp initialize ────►│  negotiate version + capabilities
  │◄─ 200 result ───────────────│
  │── POST /mcp notifications/  │
  │        initialized ────────►│  client signals ready
  │── GET  /mcp ───────────────►│  open SSE stream
  │◄─ 200 text/event-stream ────│
  │                             │  (stream stays open)
  │── POST /mcp tools/list ────►│
  │◄─ 200 result (tool list) ───│
  │── POST /mcp tools/call ────►│
  │◄─ 200 result / SSE frame ───│  (long calls can stream progress via SSE)
```

#### `initialize` handshake

```json
// Client → Server
{
  "jsonrpc": "2.0", "id": 0, "method": "initialize",
  "params": {
    "protocolVersion": "2025-11-25",
    "capabilities": { "roots": { "listChanged": true } },
    "clientInfo": { "name": "claude-desktop", "version": "1.0" }
  }
}

// Server → Client
{
  "jsonrpc": "2.0", "id": 0, "result": {
    "protocolVersion": "2025-11-25",
    "capabilities": {
      "tools":     { "listChanged": true },
      "resources": { "listChanged": true, "subscribe": true },
      "logging":   {}
    },
    "serverInfo": { "name": "titan", "version": "1.0" }
  }
}
```

#### `tools/list` — tool discovery with annotations

```json
{
  "jsonrpc": "2.0", "id": 1, "result": { "tools": [
    {
      "name": "get_positions",
      "description": "Returns all open Polymarket positions with entry price, current price, and unrealised PnL.",
      "inputSchema": { "type": "object", "properties": {}, "required": [] },
      "annotations": { "readOnlyHint": true, "openWorldHint": false }
    },
    {
      "name": "get_signals",
      "description": "Returns current whale-triggered trading signals with confidence scores.",
      "inputSchema": {
        "type": "object",
        "properties": {
          "min_score": { "type": "number", "description": "Minimum signal score filter (0–1)" }
        }
      },
      "annotations": { "readOnlyHint": true, "openWorldHint": false }
    },
    {
      "name": "force_cycle",
      "description": "Triggers an immediate engine analysis cycle. Use sparingly.",
      "inputSchema": { "type": "object", "properties": {}, "required": [] },
      "annotations": { "readOnlyHint": false, "destructiveHint": false, "idempotentHint": true }
    },
    {
      "name": "update_config",
      "description": "Patches the live engine configuration. Returns the merged config.",
      "inputSchema": {
        "type": "object",
        "properties": {
          "patch": { "type": "object", "description": "Key/value pairs to merge into the config" }
        },
        "required": ["patch"]
      },
      "annotations": { "readOnlyHint": false, "destructiveHint": false }
    }
  ]}
}
```

**Tool annotations** (spec `2025-11-25`) are hints for LLM clients:
- `readOnlyHint: true` — safe to call freely, no side effects
- `destructiveHint: true` — may have irreversible effects (we have none, but good to declare false)
- `idempotentHint: true` — calling N times is same as calling once
- `openWorldHint: false` — result is fully deterministic from Titan's own state (not an open internet call)

> **Note:** The spec states annotations are *hints*, not guarantees. LLM clients
> should not rely on them for security decisions, but they are valuable for
> guiding autonomous agents on which tools are safe to call without confirmation.

#### `tools/call` — structured output

```json
// Request
{
  "jsonrpc": "2.0", "id": 2, "method": "tools/call",
  "params": { "name": "get_positions", "arguments": {} }
}

// Response — MCP content envelope
// "text" for human-readable / LLM-digestible output
// "json" (structured) for machine processing — both can be returned together
{
  "jsonrpc": "2.0", "id": 2, "result": {
    "content": [
      { "type": "text", "text": "3 open positions: YES 0.62 …" }
    ],
    "structuredContent": {
      "positions": [{ "market": "...", "side": "YES", "entry": 0.58, "current": 0.62 }]
    }
  }
}
```

Returning both `content` (text) and `structuredContent` (JSON) is idiomatic MCP:
the LLM reads the text, a machine client can parse the structured object.

#### Tool registration via decorator

```python
@mcp_tool(
    description="Returns all open positions with entry price and unrealised PnL.",
    annotations={"readOnlyHint": True, "openWorldHint": False}
)
def get_positions(self) -> list[dict]: ...

@mcp_tool(
    description="Returns recent engine log lines.",
    input_schema={"lines": {"type": "integer", "default": 200}},
    annotations={"readOnlyHint": True, "openWorldHint": False}
)
def get_logs(self, lines: int = 200) -> str: ...
```

The server inspects all decorated methods at startup — no manual manifest.

---

### `ScriptsTitan/titan_client.py`

Thin shim used by the UI in remote mode. Implements the same duck-typed interface
as `TitanAPI` but translates each call into a `POST /mcp tools/call` request
and subscribes to the `GET /mcp` SSE stream for push events.

```python
class TitanClient:
    def __init__(self, base_url: str = "http://127.0.0.1:8765", token: str | None = None):
        ...
    # Identical method signatures to TitanAPI
```

---

## MCP primitives: Tools vs Resources vs Prompts

The `2025-11-25` spec defines three first-class primitives. Titan should use all three:

| Primitive | Who controls invocation | How Titan uses it |
|---|---|---|
| **Tools** | LLM / agent decides when to call | `get_positions`, `get_signals`, `force_cycle`, `update_config`, etc. |
| **Resources** | Client app surfaces to user | Live config file, current snapshot, log file — read-only context an LLM can load |
| **Prompts** | User explicitly selects | "Analyse my current positions", "Explain the last whale signal" — templated prompts with Titan context pre-filled |

### Resources to expose (`resources/list` + `resources/read`)

```
titan://config          → titan_config.json contents
titan://snapshot        → current runtime snapshot (replaces titan_mcp.py /mcp?r=snapshot)
titan://logs            → recent log tail
titan://whales          → current whale roster
```

Resources are **subscribed** via `resources/subscribe` — the server sends
`notifications/resources/updated` when config changes or a new cycle completes.
This is cleaner than the current polling approach.

### Prompts to expose (`prompts/list` + `prompts/get`)

```
titan_analysis          → "Here is the current state: {snapshot}. Analyse the open positions."
titan_signal_review     → "Review these signals: {signals}. Which should I act on?"
titan_whale_brief       → "Summarise whale activity: {whales}"
```

Prompts are user-triggered templates. When a user picks one in Claude Desktop,
the client calls `prompts/get`, the server fills in the current Titan data,
and the LLM gets a pre-loaded context prompt.

---

## Push notifications (API → UI / clients)

### Streamable HTTP: unified SSE on `GET /mcp`

In the `2025-11-25` spec the SSE stream is **not a separate endpoint** — it is
a `GET` to the same `/mcp` path. The server responds with `text/event-stream`
and sends JSON-RPC notification objects (no `id` field) as frames.

```
GET /mcp HTTP/1.1
Accept: text/event-stream
MCP-Session-Id: abc123
Last-Event-ID: 42        ← client can resume after disconnect
```

Each SSE frame uses a standard MCP notification method:

| Notification method | When sent | Payload |
|---|---|---|
| `notifications/message` | log line from `_log()` | `{level, logger, data}` |
| `notifications/tools/list_changed` | a tool is added/removed | *(empty params)* |
| `notifications/resources/updated` | config changed, new snapshot | `{uri}` |
| `notifications/resources/list_changed` | new resource available | *(empty params)* |
| `titan/cycle_complete` *(custom)* | 15 s engine cycle done | `{cycle, signals, elapsed_ms}` |
| `titan/hft_cycle` *(custom)* | 3 s HFT cycle done | `{cycle}` |
| `titan/position_open` *(custom)* | new trade executed | full position dict |
| `titan/position_close` *(custom)* | position closed | position + pnl |
| `titan/alert` *(custom)* | signal alert triggered | alert dict |
| `titan/price_tick` *(custom)* | price feed update | `{market, yes, no}` |
| `titan/whale_discovered` *(custom)* | new elite wallet found | wallet address |

Standard methods (`notifications/*`) are consumed natively by any MCP client.
Custom `titan/*` methods are consumed by `TitanClient` and the UI.

### Stream resumption

SSE frames include an `id:` field (incrementing integer). If the connection drops,
`TitanClient` reconnects with `Last-Event-ID: <last_seen>` and the server replays
missed events from an in-memory ring buffer (last N events, configurable).

### In-process vs remote — identical UI code

```python
# In-process (--mode ui):
api.subscribe("titan/on_log", my_callback)    # direct Python call, zero latency

# Remote (--mode client):
# TitanClient opens GET /mcp SSE stream
# background thread deserialises frames → dispatches to same subscribe() callbacks
```

---

## Sampling (server-initiated LLM calls) — future capability

The MCP spec defines `sampling/createMessage`: a server can ask the connected LLM
to generate a completion. For Titan this opens interesting possibilities:

- **Auto-commentary:** after each cycle, Titan sends its signal data to the LLM
  and asks "should I act on this?" — LLM replies, response stored in state.
- **Whale interpretation:** Titan asks the LLM to summarise an unusual whale pattern
  before deciding whether to mirror it.

This requires the client to declare `sampling` capability in `initialize`.
Claude Desktop supports it. It is **not** in scope for Phase 3 but the architecture
should not block it — `TitanAPI` will have a hook point for it.

---

## Authentication

For **local / LAN use** (the primary case): no auth, bind to `127.0.0.1`.

For **remote / internet exposure** (e.g., via Cloudflare tunnel):
the `2025-11-25` spec mandates **OAuth 2.1** with `Authorization: Bearer <token>` on
every request. The server must implement:
- `GET /.well-known/oauth-authorization-server` — metadata discovery (RFC 9728)
- Token validation on every POST/GET to `/mcp`
- Origin header validation to prevent DNS rebinding attacks

Phase 3 ships with **API key auth** (a single shared bearer token in config) as a
lightweight stepping stone. Full OAuth 2.1 is Phase 4+.

---

## Telegram — backward compatibility guarantee

`titan_telegram.py` and `TelegramNotifier` are **not touched at any phase**.
The class name, all method signatures, and all call sites in `titan_ui.py` stay identical:

| Call site in `titan_ui.py` | Stays as-is |
|---|---|
| `telegram.TelegramNotifier()` on boot | ✅ unchanged |
| `telegram_notifier.notify_boot()` | ✅ unchanged |
| `telegram_notifier.notify_buy(pos, s_name)` | ✅ unchanged |
| `telegram_notifier.notify_sell(pos, pnl_usdc, pnl_pct)` | ✅ unchanged |
| `telegram_notifier.notify_error(msg)` | ✅ unchanged |
| `telegram_notifier.send_photo(buf, caption)` | ✅ unchanged |
| `telegram_notifier.send_dashboard_button(url)` | ✅ unchanged |
| `telegram_notifier._send(reply, is_markdown)` | ✅ unchanged |
| `telegram_notifier.start_polling(handle_tg_message)` | ✅ unchanged |

### How this works through the refactor

Telegram notifications are fired from **`titan_ui.py`** today, not from the engine.
After Phase 2, `titan_ui.py` still exists and still owns the `telegram_notifier` instance —
it just gets its data from `api.subscribe(...)` events instead of direct state reads.
The wiring is unchanged; only the data source changes.

```python
# Before (Phase 1, unchanged):
def on_position_open_cb(pos):
    telegram_notifier.notify_buy(pos)

# After (Phase 2, same call — triggered by the same event, different subscription):
api.subscribe("titan/position_open", lambda pos: telegram_notifier.notify_buy(pos))
```

### Future: optional Telegram methods on TitanAPI

Phase 3+ **may** add `TitanAPI` wrapper methods such as `send_telegram_alert(msg)` as
extra MCP tools so LLM clients can also send Telegram messages. These are **additive only**
— they delegate to the same `TelegramNotifier` instance. The existing direct calls in
`titan_ui.py` continue to work unchanged alongside them.

---

## Changes to existing files

### `titan_ui.py`

- Remove all `import titan_engine`, `import titan_state`, `import titan_wallet` etc.
- Accept a single constructor argument: an object implementing the `TitanAPI` interface.
- Replace all direct state reads with `api.get_*()` calls.
- Replace callback registration with `api.subscribe(...)`.
- `build_ai_debug_snapshot()` moves to `TitanAPI.get_snapshot()`.
- **`titan_telegram` import and all `telegram_notifier.*` calls: untouched.**

### `titan_mcp.py`

Replaced by `titan_server.py`. The resources it served (`snapshot`, `config`, `logs`, `code`)
become MCP Resources accessible via `resources/read`. Delete after Phase 3.

### Entry point `run_titan.py`

```
python run_titan.py --mode ui               # Tkinter GUI + in-process API
python run_titan.py --mode server           # headless MCP server on :8765
python run_titan.py --mode server --port 8080 --token mytoken
python run_titan.py --mode client --url http://127.0.0.1:8765
```

---

## Migration phases

### Phase 1 — Extract TitanAPI

1. Create `titan_api.py` with `TitanAPI` class + `@mcp_tool` decorator.
2. Move all business-logic calls from `titan_ui.py` into `TitanAPI` methods.
3. Move `build_ai_debug_snapshot` → `TitanAPI.get_snapshot()`.
4. Update existing `titan_mcp.py` to delegate to `TitanAPI` (interim, not deleted yet).
5. UI still works — instantiates `TitanAPI` directly.

**Deliverable:** Engine runs cleanly without importing Tkinter.

---

### Phase 2 — Decouple UI

1. Refactor `titan_ui.py` — constructor injection of API object, remove engine imports.
2. Replace callbacks with `api.subscribe(...)`.
3. Add `--mode ui` entry point.

**Deliverable:** `titan_ui.py` has zero knowledge of engine internals.

---

### Phase 3 — MCP server (Streamable HTTP)

1. Implement `titan_server.py`:
   - `POST /mcp`: `initialize`, `notifications/initialized`, `tools/list`, `tools/call`, `resources/list`, `resources/read`, `prompts/list`, `prompts/get`
   - `GET /mcp`: SSE stream with `MCP-Session-Id` and `Last-Event-ID` resumption
   - API key bearer token auth (optional, off by default for LAN)
2. Implement `titan_client.py` (duck-types `TitanAPI`, speaks MCP over HTTP).
3. Register Titan tools, resources, and prompts on `TitanAPI`.
4. Add `--mode server` and `--mode client` entry-point branches.
5. Delete `titan_mcp.py`.

**Deliverable:** `python run_titan.py --mode server` → Claude Desktop can add Titan as an MCP server and call all tools natively.

---

### Phase 4 — Polish + optional features

1. OAuth 2.1 bearer auth for internet-facing deployment.
2. Sampling hook: `TitanAPI` can call the LLM via the connected client.
3. Tool progress notifications for long-running calls (`force_cycle` streams progress frames).
4. Pagination on `tools/list` / `resources/list` if catalogue grows.
5. Update `docs/guide.txt`.

---

## Design decisions

| Decision | Choice | Rationale |
|---|---|---|
| MCP spec version | `2025-11-25` (current stable) | Latest; includes Streamable HTTP, tool annotations, structured output |
| HTTP transport | Streamable HTTP (single `/mcp` endpoint, POST + GET/SSE) | Spec-recommended for remote servers; replaces deprecated dual-path HTTP+SSE |
| MCP primitives | Tools + Resources + Prompts | All three are first-class in the spec; each serves a distinct use case for LLM clients |
| Tool annotations | `readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint` | Guide LLM agents on which tools are safe to call autonomously |
| Structured output | Both `content[text]` and `structuredContent` on tool results | LLM reads text; machine client parses JSON; no ambiguity |
| Push transport | SSE on `GET /mcp`, standard `notifications/*` methods + custom `titan/*` | Standard clients consume standard notifications; UI consumes all |
| SSE resumption | `id:` frame field + `Last-Event-ID` + in-memory ring buffer | Handles network hiccups without losing price ticks or log lines |
| Auth (Phase 3) | API key bearer token, localhost-only default | Simple, secure for LAN; OAuth 2.1 deferred to Phase 4 |
| UI/API coupling | Duck-typed interface (`TitanAPI` / `TitanClient` interchangeable) | No ABC needed; clean and simple |
| HTTP library | stdlib `http.server` for Phase 3 | Zero deps; swap to `aiohttp` in Phase 4 if async is needed |

---

## File summary after migration

```
ScriptsTitan/
  titan_api.py          ← NEW: single public interface + @mcp_tool decorators
  titan_server.py       ← NEW: MCP Streamable HTTP server
  titan_client.py       ← NEW: MCP client shim (duck-types TitanAPI)
  titan_ui.py           ← MODIFIED: no engine imports, accepts API object
  run_titan.py          ← NEW: --mode ui|server|client entry point
  titan_mcp.py          ← REMOVED in Phase 3
  titan_engine.py       ← unchanged
  titan_state.py        ← unchanged
  titan_*.py            ← unchanged
```
