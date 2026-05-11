# TITAN Logging Architecture

The TITAN logging system is built to be decoupled. Engine components generate logs without knowing whether the application is running as a desktop GUI or a headless MCP server. 

## 🌊 The Log Flow

1. **Generation (`titan_state.py`)**
   Every module in the engine imports the `_log` function from `titan_state.py`. 
   When `_log(msg, level)` is called, it:
   * Formats the string with a timestamp.
   * Checks if it's a `VERB` (verbose) log. If so, it writes *only* to `Logs/titan_verbose.log` and stops (to avoid spamming the UI/memory).
   * Appends the line to the in-memory `SYSTEM_LOGS` list (capped at 5000 lines).
   * Appends the line to disk at `Logs/titan.log`.
   * Fires a registered callback to pass the log up the chain.

2. **API Layer (`titan_api.py`)**
   The `TitanAPI` class registers itself as the callback handler when starting the engine. 
   When it receives a log, it:
   * Emits an internal pub/sub event: `"notifications/message"`.
   * If the level is `ERR`, `ERROR`, or `CRITICAL`, it immediately triggers a Telegram alert (if configured).

3. **Server Transport (`titan_server.py`) - *If running as a server***
   If TITAN is running as a headless MCP server, `titan_server.py` subscribes to the API's `"notifications/message"` event. 
   It packages the log into a standard JSON-RPC `2025-11-25` MCP notification and broadcasts it over the Server-Sent Events (SSE) stream to any connected clients.
   ```json
   {
     "jsonrpc": "2.0",
     "method": "notifications/message",
     "params": {
       "level": "info",
       "logger": "titan",
       "data": "[2026-05-11 08:00:00] [INFO ] Engine started..."
     }
   }
   ```

## Log Message Types (Levels)

The system utilizes several standard message types (levels) to categorize log output:

* **ERR / ERROR / CRITICAL**: Indicates a failure, exception, or critical issue that requires attention. Triggers Telegram alerts if configured.
* **WRN / WARN**: Indicates a potential issue, degraded performance, or unexpected state that isn't immediately fatal.
* **INF / INFO**: Standard informational messages about normal engine operation (e.g., startup, regular state changes).
* **DBG / DEBUG**: Detailed diagnostic information useful for troubleshooting during development or investigation.
* **VRB / VERB**: Highly verbose output (e.g., raw API responses, high-frequency loop data). Written only to `Logs/titan_verbose.log` and not kept in memory or broadcast to clients.

