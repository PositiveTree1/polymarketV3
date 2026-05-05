# Titan MCP Server Spec

This document describes the HTTP endpoints that Titan's local server must expose
so the RAG assistant can fetch resources on demand during the tool-loop.

The RAG calls these endpoints **only when the router LLM asks for them**.

---

## Base URL

```
http://127.0.0.1:8080
```

---

## Unified endpoint

```
GET /mcp?r=<resource_name>[&<extra_params>]
```

All resources go through this single endpoint.
Response is always **plain UTF-8 text**, truncated server-side when needed.
HTTP 200 on success. HTTP 5xx or connection error = RAG treats resource as unavailable.

---

## Resources

### `snapshot` — Full Titan runtime state

```
GET /mcp?r=snapshot
```

The existing `/snapshot` endpoint, migrated here.
Returns the full Titan state dump: positions, open orders, P&L, strategy status, etc.
Titan writes the snapshot to a temp file and returns its path — the RAG reads the file.

**Response:** plain text, as large as needed (RAG will truncate to token budget).

---

### `config` — Active configuration

```
GET /mcp?r=config
```

Returns the content of `titan_config.json` (or equivalent active config).
Should reflect the **currently loaded** config, not just the file on disk.

**Response:** JSON as plain text. Target ≤ 8 000 chars.

---

### `logs` — Recent log tail

```
GET /mcp?r=logs[&lines=<n>]
```

Returns the last `n` lines of the main Titan log (default: 200).
Apply the same compaction as `utils._compact_log_lines` if possible (deduplicate, strip noise).

**Response:** plain text. Target ≤ 6 000 chars.

---

### `code` — Source file content *(future)*

```
GET /mcp?r=code&f=<relative_path>
```

Returns the content of a source file from the Titan repo.
`f` is a path relative to the Titan project root (e.g. `titan/fetch.py`).
Titan should refuse paths outside the project root (no path traversal).

**Response:** plain text. Target ≤ 12 000 chars. Return `ERROR: file not found` if missing.

---

## Error format

If a resource cannot be produced, return HTTP 200 with body:

```
ERROR: <reason>
```

The RAG will include this as-is in the context so the LLM knows the resource was unavailable.

---

## Implementation notes (Titan side)

- All endpoints are **read-only** — no mutations.
- Add to the existing FastAPI / Flask / http.server that already serves `/snapshot`.
- No auth needed (localhost only).
- Truncate responses server-side if you want to control size; RAG also truncates client-side.
- `/snapshot` (old path) can be kept as an alias during transition, then removed.

---

## RAG router — what the LLM returns to request resources

The router LLM call returns JSON in this shape:

```json
{"action": "fetch", "resources": ["snapshot", "logs"]}
```

or for a direct answer:

```json
{"action": "answer", "text": "The retry delay is 5 seconds."}
```

The RAG fetches all requested resources, concatenates them into context, then makes a second LLM call for the final answer.
