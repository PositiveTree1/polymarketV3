"""MCP server for the Titan RAG assistant.

Serves GET /mcp?r=<resource> on http://127.0.0.1:8080.
All responses are plain UTF-8 text; HTTP 200 always (errors as "ERROR: ...").

Resources
---------
snapshot  – full compressed Titan runtime state (file path returned)
config    – active titan_config.json content
logs      – recent compacted log tail  (?lines=N, default 200)
code      – source file content        (?f=<relative_path>)
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import titan_state as _TS
from titan_utils import _compact_log_lines

_LOG_MAX_CHARS = 6_000
_CONFIG_MAX_CHARS = 8_000
_CODE_MAX_CHARS = 12_000
_DEFAULT_LOG_LINES = 200

_ROOT_DIR = Path(os.path.dirname(os.path.dirname(__file__)))
_SCRIPTS_DIR = Path(os.path.dirname(__file__))


# ── resource handlers ────────────────────────────────────────────────────────

def _handle_snapshot() -> str:
    try:
        from titan_ui import build_ai_debug_snapshot
        snapshot = build_ai_debug_snapshot(compressed=True)
        log_dir = getattr(_TS, "LOG_DIR", str(_ROOT_DIR / "Logs"))
        os.makedirs(log_dir, exist_ok=True)
        fname = os.path.join(log_dir, f"titan_snapshot_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt")
        with open(fname, "w", encoding="utf-8") as f:
            f.write(snapshot)
        return fname
    except Exception as e:
        return f"ERROR: {e}"


def _handle_config() -> str:
    try:
        cfg_path = _ROOT_DIR / "titan_config.json"
        text = cfg_path.read_text(encoding="utf-8", errors="ignore")
        if len(text) > _CONFIG_MAX_CHARS:
            text = text[:_CONFIG_MAX_CHARS] + "\n[truncated]"
        return text
    except FileNotFoundError:
        return "ERROR: titan_config.json not found"
    except Exception as e:
        return f"ERROR: {e}"


def _handle_logs(lines: int = _DEFAULT_LOG_LINES) -> str:
    log_file = Path(getattr(_TS, "LOG_FILE", str(_ROOT_DIR / "Logs" / "titan.log")))
    try:
        all_lines = log_file.read_text(encoding="utf-8", errors="ignore").splitlines()
    except FileNotFoundError:
        return f"ERROR: log file not found: {log_file}"
    except Exception as e:
        return f"ERROR: {e}"

    tail = "\n".join(all_lines[-lines:])
    compacted = _compact_log_lines(tail, max_lines=lines)
    if len(compacted) > _LOG_MAX_CHARS:
        compacted = compacted[:_LOG_MAX_CHARS] + "\n[truncated]"
    return compacted


def _handle_code(rel_path: str) -> str:
    if not rel_path:
        return "ERROR: missing parameter f"
    try:
        target = (_SCRIPTS_DIR / rel_path).resolve()
        scripts_resolved = _SCRIPTS_DIR.resolve()
        root_resolved = _ROOT_DIR.resolve()
        if not (str(target).startswith(str(scripts_resolved)) or
                str(target).startswith(str(root_resolved))):
            return "ERROR: path traversal not allowed"
        if not target.is_file():
            return "ERROR: file not found"
        text = target.read_text(encoding="utf-8", errors="ignore")
        if len(text) > _CODE_MAX_CHARS:
            text = text[:_CODE_MAX_CHARS] + "\n[truncated]"
        return text
    except Exception as e:
        return f"ERROR: {e}"


def dispatch(path: str) -> str:
    """Resolve an MCP path string to a response body. Usable without an HTTP context."""
    parsed = urlparse(path)
    params = parse_qs(parsed.query)

    if parsed.path != "/mcp":
        return f"ERROR: unknown path '{parsed.path}'"

    resource = (params.get("r") or [""])[0]

    if resource == "snapshot":
        return _handle_snapshot()
    elif resource == "config":
        return _handle_config()
    elif resource == "logs":
        try:
            lines = int((params.get("lines") or [str(_DEFAULT_LOG_LINES)])[0])
        except ValueError:
            lines = _DEFAULT_LOG_LINES
        return _handle_logs(lines)
    elif resource == "code":
        return _handle_code((params.get("f") or [""])[0])
    else:
        return f"ERROR: unknown resource '{resource}'"


