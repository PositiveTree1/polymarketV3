from __future__ import annotations
import tkinter as tk
from tkinter import font as tkfont, scrolledtext
import threading
import json
import requests
import os
from datetime import datetime
from titan_client import TitanClient
from titan_protocol import TitanBackend

# ── backends ──────────────────────────────────────────────────────────────────
ACTIVE_BACKEND = "local"   # "ollama" | "groq" | "openai" | "gemini" | "local"

BACKENDS: dict[str, dict] = {
    "groq": {
        "type":     "openai_compat",
        "base_url": "https://api.groq.com/openai/v1",
        "api_key":  os.environ.get("LLM_KEY_GROQ"),
        "model":    "llama-3.3-70b-versatile",
    },
    "local": {
        "type":     "openai_compat",
        "base_url": "http://localhost:8000/v1",
        "api_key":  "not-needed",
        "model":    "gemma4:4b",
    },
    "openai": {
        "type":     "openai_compat",
        "base_url": "https://api.openai.com/v1",
        "api_key":  os.environ.get("LLM_KEY_OPENAI"),
        "model":    "gpt-4o-mini",
    },
    "gemini": {
        "type":    "gemini",
        "api_key": os.environ.get("LLM_KEY_GEMINI"),
        "model":   "gemini-2.5-flash",
    },
    "ollama": {
        "type":     "ollama",
        "base_url": "http://localhost:11434/api/chat",
        "model":    "gemma4:e4b",
    },
}

_backend    = BACKENDS[ACTIVE_BACKEND]
_btype      = _backend["type"]
MODEL       = _backend["model"]
TITAN_MCP_URL = os.environ.get("TITAN_MCP_URL", "http://127.0.0.1:8765")
TOOL_LOOP_MAX_STEPS = 6

# ── design tokens ─────────────────────────────────────────────────────────────
BG_DARK  = "#080810"; BG_MID   = "#0d0d1a"; BG_LIGHT  = "#13132a"
BG_INPUT = "#060612"; FG_MAIN  = "#cccccc"; FG_ACCENT = "#00ff88"
FG_USER  = "#00aaff"; FG_AI    = "#ccffee"; FG_SYS    = "#556655"
FG_WARN  = "#ffaa00"; FG_ERR   = "#ff4444"; BORDER    = "#1a2a4a"

SYSTEM_PROMPT = """\
You are TITAN AI, a quantitative trading analyst for the TITAN Polymarket engine.
Parse the [LIVE SYSTEM SNAPSHOT] carefully. Answer questions about positions, P&L,
signals, and whale wallets. Be concise, sharp, and data-first. Under 400 words.

If your runtime supports MCP or tool use, a Titan MCP server may be available from
the running server at http://127.0.0.1:8765/mcp. Prefer Titan MCP tools for fresh
live data when the snapshot may be stale. Relevant tool names can include:
status, get_status, get_positions, get_closed_positions, get_signals,
get_signal_history, get_rejects, get_alerts, get_whales, get_pnl_summary,
get_trade_history, get_snapshot, get_portfolio_overview, get_recent_errors,
force_cycle, pause, resume, and update_config.

If MCP/tool use is not actually available in your host runtime, say so plainly and
answer from the provided snapshot and conversation context instead of pretending you
queried tools.
"""


def _save_ai_request_snapshot(messages: list[dict]) -> None:
    log_dir = "Logs"
    os.makedirs(log_dir, exist_ok=True)
    fname = os.path.join(log_dir, f"titan_snapshot_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt")
    lines = [
        f"TIMESTAMP: {datetime.now().isoformat(timespec='seconds')}",
        f"BACKEND: {ACTIVE_BACKEND}",
        f"MODEL: {MODEL}",
        "",
        "[OUTBOUND AI MESSAGES]",
        "",
    ]
    for i, msg in enumerate(messages, start=1):
        role = str(msg.get("role", "")).upper()
        content = str(msg.get("content", ""))
        lines.append(f"--- MESSAGE {i} [{role}] ---")
        lines.append(content)
        lines.append("")
    with open(fname, "w", encoding="utf-8") as f:
        f.write("\n".join(lines).rstrip() + "\n")


def _json_dump(value) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def _coerce_tool_schema(tool: dict) -> dict:
    schema = dict(tool.get("inputSchema") or {})
    if schema.get("type") != "object":
        schema = {"type": "object", "properties": {}, "required": []}
    schema.setdefault("properties", {})
    schema.setdefault("required", [])
    return schema


class TitanMCPBridge:
    def __init__(self, base_url: str = TITAN_MCP_URL):
        self._client = TitanClient(base_url=base_url)
        self._tools_cache: list[dict] | None = None
        self._lock = threading.Lock()

    def available_tools(self) -> list[dict]:
        with self._lock:
            if self._tools_cache is None:
                tools = self._client.list_tools()
                safe_tools = []
                for tool in tools:
                    annotations = tool.get("annotations") or {}
                    if annotations.get("readOnlyHint", False):
                        safe_tools.append(tool)
                self._tools_cache = safe_tools
            return list(self._tools_cache)

    def openai_tools(self) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": _coerce_tool_schema(tool),
                },
            }
            for tool in self.available_tools()
        ]

    def call(self, name: str, arguments: dict | None = None) -> object:
        allowed = {tool["name"] for tool in self.available_tools()}
        if name not in allowed:
            raise RuntimeError(f"Tool not allowed: {name}")
        return self._client.call_tool(name, arguments or {})


# ── unified ask function ──────────────────────────────────────────────────────

def _ask_openai_compat(messages: list[dict], on_token, on_done, on_error) -> None:
    url     = _backend["base_url"].rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {_backend['api_key']}", "Content-Type": "application/json"}
    payload = {"model": MODEL, "messages": messages, "stream": True, "temperature": 0.2}
    try:
        resp = requests.post(url, headers=headers, json=payload, stream=True, timeout=120)
        if resp.status_code != 200:
            on_error(f"HTTP {resp.status_code}: {resp.text[:200]}")
            return
        full = ""
        for line in resp.iter_lines():
            if not line or line == b"data: [DONE]":
                continue
            raw = line.decode("utf-8").removeprefix("data: ")
            try:
                tok = json.loads(raw)["choices"][0]["delta"].get("content", "")
                if tok:
                    full += tok
                    on_token(tok)
            except Exception:
                continue
        on_done(full)
    except Exception as e:
        on_error(str(e))


def _openai_chat_once(messages: list[dict], tools: list[dict] | None = None) -> dict:
    url = _backend["base_url"].rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {_backend['api_key']}", "Content-Type": "application/json"}
    payload = {"model": MODEL, "messages": messages, "stream": False, "temperature": 0.2}
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    resp = requests.post(url, headers=headers, json=payload, timeout=120)
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:400]}")
    body = resp.json()
    choices = body.get("choices") or []
    if not choices:
        raise RuntimeError("No choices returned by model")
    return choices[0].get("message") or {}


def _ollama_chat_once(messages: list[dict], tools: list[dict] | None = None) -> dict:
    payload = {
        "model": MODEL,
        "messages": messages,
        "stream": False,
        "think": False,
        "keep_alive": -1,
        "options": {"num_ctx": 8192, "temperature": 0.2, "top_k": 20, "num_predict": 1024},
    }
    if tools:
        payload["tools"] = tools
    resp = requests.post(_backend["base_url"], json=payload, timeout=120)
    if resp.status_code != 200:
        raise RuntimeError(f"Ollama HTTP {resp.status_code}: {resp.text[:400]}")
    body = resp.json()
    return body.get("message") or {}


def _message_tool_calls(msg: dict) -> list[dict]:
    tool_calls = msg.get("tool_calls") or []
    return [tc for tc in tool_calls if isinstance(tc, dict)]


def _tool_call_name(tc: dict) -> str:
    fn = tc.get("function") or {}
    return str(fn.get("name") or tc.get("name") or "")


def _tool_call_args(tc: dict) -> dict:
    fn = tc.get("function") or {}
    args = fn.get("arguments")
    if isinstance(args, dict):
        return args
    if isinstance(args, str) and args.strip():
        return json.loads(args)
    return {}


def _tool_call_id(tc: dict, idx: int) -> str:
    return str(tc.get("id") or f"toolcall_{idx}")


def _append_openai_tool_roundtrip(messages: list[dict], assistant_msg: dict, tool_calls: list[dict], bridge: TitanMCPBridge, on_tool_call=None) -> None:
    messages.append({
        "role": "assistant",
        "content": assistant_msg.get("content", "") or "",
        "tool_calls": tool_calls,
    })
    for idx, tool_call in enumerate(tool_calls, start=1):
        name = _tool_call_name(tool_call)
        args = _tool_call_args(tool_call)
        if on_tool_call:
            on_tool_call(name, args)
        result = bridge.call(name, args)
        messages.append({
            "role": "tool",
            "tool_call_id": _tool_call_id(tool_call, idx),
            "name": name,
            "content": _json_dump(result),
        })


def _append_ollama_tool_roundtrip(messages: list[dict], assistant_msg: dict, tool_calls: list[dict], bridge: TitanMCPBridge, on_tool_call=None) -> None:
    messages.append({
        "role": "assistant",
        "content": assistant_msg.get("content", "") or "",
        "tool_calls": tool_calls,
    })
    for idx, tool_call in enumerate(tool_calls, start=1):
        name = _tool_call_name(tool_call)
        args = _tool_call_args(tool_call)
        if on_tool_call:
            on_tool_call(name, args)
        result = bridge.call(name, args)
        messages.append({
            "role": "tool",
            "tool_call_id": _tool_call_id(tool_call, idx),
            "name": name,
            "content": _json_dump(result),
        })


def _dispatch_with_titan_tools(messages: list[dict], bridge: TitanMCPBridge, on_token, on_done, on_error, on_tool_call=None) -> bool:
    if _btype not in {"openai_compat", "ollama", "local"}:
        return False

    try:
        tool_messages = [dict(m) for m in messages]
        tools = bridge.openai_tools()
        if not tools:
            raise RuntimeError("No read-only Titan MCP tools are available")

        final_text = ""
        for _ in range(TOOL_LOOP_MAX_STEPS):
            if _btype == "ollama":
                assistant_msg = _ollama_chat_once(tool_messages, tools)
                tool_calls = _message_tool_calls(assistant_msg)
                if tool_calls:
                    _append_ollama_tool_roundtrip(tool_messages, assistant_msg, tool_calls, bridge, on_tool_call)
                    continue
            else:
                assistant_msg = _openai_chat_once(tool_messages, tools)
                tool_calls = _message_tool_calls(assistant_msg)
                if tool_calls:
                    _append_openai_tool_roundtrip(tool_messages, assistant_msg, tool_calls, bridge, on_tool_call)
                    continue

            final_text = assistant_msg.get("content", "") or ""
            break

        if not final_text:
            raise RuntimeError("Tool loop finished without a final assistant response")
        on_token(final_text)
        on_done(final_text)
        return True
    except Exception:
        return False


def _ask_gemini(messages: list[dict], on_token, on_done, on_error) -> None:
    api_key = _backend["api_key"]
    model   = _backend["model"]
    url     = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:streamGenerateContent?alt=sse&key={api_key}"
    contents = []
    for m in messages:
        if m["role"] == "system":
            contents.append({"role": "user", "parts": [{"text": f"[SYSTEM]\n{m['content']}"}]})
            contents.append({"role": "model", "parts": [{"text": "Understood."}]})
        elif m["role"] == "assistant":
            contents.append({"role": "model", "parts": [{"text": m["content"]}]})
        else:
            contents.append({"role": "user", "parts": [{"text": m["content"]}]})
    payload = {"contents": contents, "generationConfig": {"temperature": 0.2}}
    try:
        resp = requests.post(url, json=payload, stream=True, timeout=120)
        if resp.status_code != 200:
            on_error(f"Gemini HTTP {resp.status_code}: {resp.text[:200]}")
            return
        full = ""
        for line in resp.iter_lines():
            if not line:
                continue
            raw = line.decode("utf-8").removeprefix("data: ").strip()
            if not raw:
                continue
            try:
                tok = json.loads(raw)["candidates"][0]["content"]["parts"][0]["text"]
                if tok:
                    full += tok
                    on_token(tok)
            except Exception:
                continue
        on_done(full)
    except Exception as e:
        on_error(str(e))


def _ask_ollama(messages: list[dict], on_token, on_done, on_error) -> None:
    url     = _backend["base_url"]
    payload = {
        "model": MODEL, "messages": messages, "stream": True,
        "think": False, "keep_alive": -1,
        "options": {"num_ctx": 8192, "temperature": 0.2, "top_k": 20, "num_predict": 1024},
    }
    try:
        resp = requests.post(url, json=payload, stream=True, timeout=120)
        if resp.status_code != 200:
            on_error(f"Ollama HTTP {resp.status_code}: {resp.text[:200]}")
            return
        full = ""
        for line in resp.iter_lines():
            if not line:
                continue
            try:
                chunk = json.loads(line.decode("utf-8"))
                tok = chunk.get("message", {}).get("content", "")
                if tok:
                    full += tok
                    on_token(tok)
                if chunk.get("done"):
                    break
            except Exception:
                continue
        on_done(full)
    except requests.exceptions.ConnectionError:
        on_error("Cannot connect to Ollama. Is it running?\n  → Run: ollama serve")
    except Exception as e:
        on_error(str(e))


def _dispatch(messages: list[dict], on_token, on_done, on_error) -> None:
    if _btype == "ollama":
        _ask_ollama(messages, on_token, on_done, on_error)
    elif _btype == "gemini":
        _ask_gemini(messages, on_token, on_done, on_error)
    else:
        _ask_openai_compat(messages, on_token, on_done, on_error)


# ── AI client ─────────────────────────────────────────────────────────────────

class TitanAIClient:
    def __init__(self, engine_module: TitanBackend | None = None):
        self._engine: TitanBackend | None = engine_module
        self._messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
        self._lock = threading.Lock()
        self._mcp = TitanMCPBridge()

    def _build_request_messages(self, full_msg: str) -> list[dict]:
        history: list[dict] = []
        for message in self._messages:
            if message.get("role") == "system":
                continue
            history.append(dict(message))
        return [{"role": "system", "content": SYSTEM_PROMPT}, *history, {"role": "user", "content": full_msg}]


    def ask(self, user_msg: str, on_token, on_done, on_error, on_tool_call=None) -> None:
        snapshot = ""
        if self._engine is not None:
            try:
                snapshot = self._engine.get_snapshot(compressed=True)
            except Exception as exc:
                snapshot = f"(snapshot error: {exc})"

        full_msg = f"[LIVE SYSTEM SNAPSHOT]\n{snapshot}\n\n[USER QUESTION]\n{user_msg}"

        with self._lock:
            if len(self._messages) > 21:
                self._messages = [self._messages[0]] + self._messages[-10:]
            snapshot_messages = self._build_request_messages(full_msg)

        def _run():
            try:
                _save_ai_request_snapshot(snapshot_messages)
            except Exception:
                pass

            def _done(full: str):
                with self._lock:
                    self._messages.append({"role": "user", "content": user_msg})
                    self._messages.append({"role": "assistant", "content": full})
                on_done()

            def _err(msg: str):
                on_error(msg)

            if _dispatch_with_titan_tools(snapshot_messages, self._mcp, on_token, _done, _err, on_tool_call):
                return
            _dispatch(snapshot_messages, on_token, _done, _err)

        threading.Thread(target=_run, daemon=True).start()

    def clear_history(self) -> None:
        with self._lock:
            self._messages = [{"role": "system", "content": SYSTEM_PROMPT}]


# ── UI panel ──────────────────────────────────────────────────────────────────

class AIPanel:
    def __init__(self, parent: tk.Widget, engine_module: TitanBackend | None = None):
        self._engine: TitanBackend | None = engine_module
        self._client = TitanAIClient(engine_module=engine_module)
        self._busy   = False

        mono    = tkfont.Font(family="Courier", size=9)
        mono_sm = tkfont.Font(family="Courier", size=8)
        bold_hd = tkfont.Font(family="Courier", size=10, weight="bold")

        frame = tk.Frame(parent, bg=BG_DARK, width=400)
        frame.pack(fill="both", expand=True)
        frame.pack_propagate(False)

        hdr = tk.Frame(frame, bg=BG_MID, pady=6)
        hdr.pack(fill="x")
        tk.Label(hdr, text="🤖  TITAN AI", fg=FG_ACCENT, bg=BG_MID, font=bold_hd).pack(side="left", padx=10)

        # backend selector
        self._backend_var = tk.StringVar(value=ACTIVE_BACKEND)
        bk_menu = tk.OptionMenu(hdr, self._backend_var, *BACKENDS.keys(), command=self._switch_backend)
        bk_menu.config(bg=BG_LIGHT, fg=FG_ACCENT, font=mono_sm, relief="flat",
                       activebackground=BG_MID, highlightthickness=0)
        bk_menu["menu"].config(bg=BG_MID, fg=FG_MAIN)
        bk_menu.pack(side="left", padx=4)

        self._status_var = tk.StringVar(value=f"⬤ {ACTIVE_BACKEND}/{MODEL}")
        tk.Label(hdr, textvariable=self._status_var, fg=FG_SYS, bg=BG_MID, font=mono_sm).pack(side="right", padx=8)

        btn_bar = tk.Frame(frame, bg=BG_MID, pady=3)
        btn_bar.pack(fill="x")
        self._quick_btns = []
        for label, prompt in [
            ("📊 Portfolio",  "Summarise my current portfolio with every open position, P&L%, hold time, and whales behind it."),
            ("⚠ Risk Check",  "Risk assessment: over-concentrated positions, fee-negative bets, what to close first?"),
            ("🐋 Whales",     "Top 5 verified whales by score. What markets are they active in?"),
            ("📈 P&L",        "Detailed P&L: closed PnL, win rate, average profit, best/worst trade, trend."),
            ("🔍 Diagnose",   "Check last 100 log lines for errors, warnings, or unusual patterns."),
        ]:
            b = tk.Button(btn_bar, text=label, bg=BG_LIGHT, fg=FG_ACCENT, font=mono_sm,
                          relief="flat", padx=6, command=lambda p=prompt: self._send(p))
            b.pack(side="left", padx=2, pady=2)
            self._quick_btns.append(b)

        chat_frame = tk.Frame(frame, bg=BG_DARK)
        chat_frame.pack(fill="both", expand=True, padx=4, pady=(4, 0))
        self._chat = scrolledtext.ScrolledText(
            chat_frame, bg=BG_INPUT, fg=FG_MAIN, font=mono,
            selectbackground="#1a2a4a", wrap=tk.WORD, state="disabled", relief="flat",
        )
        self._chat.pack(fill="both", expand=True)
        self._chat.tag_configure("user",   foreground=FG_USER,  font=bold_hd)
        self._chat.tag_configure("ai",     foreground=FG_AI)
        self._chat.tag_configure("system", foreground=FG_SYS)
        self._chat.tag_configure("error",  foreground=FG_ERR)
        self._chat.tag_configure("ts",     foreground="#334455")
        self._chat.tag_configure("bold",   foreground="#ffffff", font=bold_hd)
        self._chat.tag_configure("bullet", foreground=FG_ACCENT)
        self._chat.tag_configure("tool",   foreground=FG_WARN,  font=mono_sm)

        inp_frame = tk.Frame(frame, bg=BG_MID, pady=4)
        inp_frame.pack(fill="x", padx=4, pady=4)
        self._inp = tk.Text(inp_frame, bg=BG_INPUT, fg=FG_MAIN, font=mono,
                            height=3, wrap="word", relief="flat",
                            insertbackground=FG_ACCENT,
                            highlightthickness=1, highlightbackground=BORDER)
        self._inp.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self._inp.bind("<Return>", self._on_enter)
        self._inp.bind("<Shift-Return>", lambda e: None)

        btn_col = tk.Frame(inp_frame, bg=BG_MID)
        btn_col.pack(side="right")
        self._send_btn = tk.Button(btn_col, text="SEND\n⏎", bg="#1a332a", fg=FG_ACCENT,
                                   font=bold_hd, padx=8, relief="flat",
                                   command=lambda: self._send(self._inp.get("1.0", "end").strip()))
        self._send_btn.pack(pady=(0, 4))
        tk.Button(btn_col, text="CLR", bg="#1a1a30", fg="#556677", font=mono_sm,
                  padx=8, relief="flat", command=self._clear).pack()

        self._write_system(f"🤖 TITAN AI — backend: {ACTIVE_BACKEND} / {MODEL}")

    def _switch_backend(self, name: str) -> None:
        global ACTIVE_BACKEND, _backend, _btype, MODEL
        ACTIVE_BACKEND = name
        _backend       = BACKENDS[name]
        _btype         = _backend["type"]
        MODEL          = _backend["model"]
        self._client.clear_history()
        self._status_var.set(f"⬤ {name}/{MODEL}")
        self._write_system(f"🔀 Switched to {name} / {MODEL}")

    def _write(self, text: str, tag: str = "ai") -> None:
        self._chat.configure(state="normal")
        self._chat.insert(tk.END, text, tag)
        self._chat.see(tk.END)
        self._chat.configure(state="disabled")

    def _write_system(self, msg: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        self._write(f"[{ts}] ", "ts")
        self._write(msg + "\n", "system")

    def _on_enter(self, event) -> str | None:
        if event.state & 0x1:
            return None
        text = self._inp.get("1.0", "end").strip()
        if text:
            self._send(text)
        return "break"

    def _send(self, text: str) -> None:
        if not text or self._busy:
            return
        self._inp.delete("1.0", tk.END)
        self._busy = True
        self._set_controls("disabled")
        self._status_var.set(f"⬤ thinking… [{ACTIVE_BACKEND}]")

        ts = datetime.now().strftime("%H:%M:%S")
        self._write(f"\n[{ts}] ", "ts")
        self._write(f"You: {text}\n", "user")

        self._write(f"[{datetime.now().strftime('%H:%M:%S')}] ", "ts")
        self._write("TITAN AI: ", "user")

        def on_token(tok: str) -> None:
            self._chat.configure(state="normal")
            self._chat.insert(tk.END, tok, "ai")
            self._chat.see(tk.END)
            self._chat.configure(state="disabled")

        def on_tool_call(name: str, args: dict) -> None:
            args_str = ", ".join(f"{k}={v!r}" for k, v in args.items()) if args else ""
            label = f"{name}({args_str})" if args_str else name
            self._chat.configure(state="normal")
            self._chat.insert(tk.END, f"\n  ⚙ MCP → {label}\n", "tool")
            self._chat.see(tk.END)
            self._chat.configure(state="disabled")

        def on_done() -> None:
            self._prettify_last_message()
            self._write("\n", "ai")
            self._busy = False
            self._set_controls("normal")
            self._status_var.set(f"⬤ ready  [{ACTIVE_BACKEND}/{MODEL}]")

        def on_error(msg: str) -> None:
            self._write(f"\n⚠ {msg}\n", "error")
            self._busy = False
            self._set_controls("normal")
            self._status_var.set("⬤ error")

        self._client.ask(text, on_token, on_done, on_error, on_tool_call)

    def _set_controls(self, state: str) -> None:
        try:
            self._send_btn.configure(state=state)
            self._inp.configure(state=state)
            for b in self._quick_btns:
                b.configure(state=state)
        except Exception:
            pass

    def _clear(self) -> None:
        self._client.clear_history()
        self._chat.configure(state="normal")
        self._chat.delete("1.0", tk.END)
        self._chat.configure(state="disabled")
        self._write_system("🔄 Conversation cleared.")

    def _prettify_last_message(self) -> None:
        try:
            self._chat.configure(state="normal")
            start_idx = self._chat.search("TITAN AI:", "end", backwards=True)
            if not start_idx:
                return
            content_start = self._chat.index(f"{start_idx} + 9 chars")
            end_idx = "end"
            curr = content_start
            while True:
                idx = self._chat.search("* ", curr, stopindex=end_idx)
                if not idx:
                    break
                prefix = self._chat.get(self._chat.index(f"{idx} linestart"), idx)
                if not prefix.strip():
                    self._chat.delete(idx, f"{idx} + 1 chars")
                    self._chat.insert(idx, "•", "bullet")
                curr = f"{idx} + 1 chars"
            curr = content_start
            while True:
                b_start = self._chat.search("**", curr, stopindex=end_idx)
                if not b_start:
                    break
                b_end = self._chat.search("**", f"{b_start} + 2 chars", stopindex=end_idx)
                if not b_end:
                    break
                self._chat.tag_add("bold", f"{b_start} + 2 chars", b_end)
                self._chat.delete(b_end, f"{b_end} + 2 chars")
                self._chat.delete(b_start, f"{b_start} + 2 chars")
                curr = b_start
            self._chat.configure(state="disabled")
        except Exception:
            pass


# ── public API ────────────────────────────────────────────────────────────────

def attach_ai_panel(root: tk.Tk, engine_module: TitanBackend | None = None) -> tuple[AIPanel, tk.Frame]:
    pw = tk.PanedWindow(root, orient="horizontal", bg="#080810",
                        sashwidth=5, sashrelief="flat", sashpad=0, handlesize=0)
    pw.pack(fill="both", expand=True)
    left  = tk.Frame(pw, bg="#080810")
    right = tk.Frame(pw, bg="#080810")
    pw.add(left,  minsize=800, stretch="always")
    pw.add(right, minsize=360, stretch="never")
    panel = AIPanel(right, engine_module=engine_module)
    return panel, left


if __name__ == "__main__":
    root = tk.Tk()
    root.title("🤖 TITAN AI — Standalone")
    root.geometry("500x700")
    root.configure(bg="#080810")
    AIPanel(root, engine_module=None)
    root.mainloop()
