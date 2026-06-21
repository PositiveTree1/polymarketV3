from __future__ import annotations
import tkinter as tk
from tkinter import font as tkfont, scrolledtext
import threading
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Literal, TypeAlias
import litellm
from titan_client import TitanClient, _log
from titan_protocol import TitanBackend

litellm.drop_params = True        # ignore unsupported params silently
litellm._turn_on_debug()  # type: ignore[attr-defined]

WidgetState: TypeAlias = Literal["normal", "disabled"]

# ── ai config (persisted) ─────────────────────────────────────────────────────
_AI_CONFIG_PATH = Path(__file__).parent.parent / "titan_ai_config.json"

_DEFAULT_LOCAL_MODELS = ["qwen/qwen3.6-35b-a3b", "gemma@q4_k_xl", "gemma@q8_0", "gemma@q5_k_m", "gpt-oss-20b"]

def _load_ai_config() -> dict:
    if _AI_CONFIG_PATH.exists():
        try:
            return json.loads(_AI_CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"local_models": _DEFAULT_LOCAL_MODELS, "last_local_model": _DEFAULT_LOCAL_MODELS[0]}

def _save_ai_config(cfg: dict) -> None:
    try:
        _AI_CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    except Exception as e:
        _log(f"AI config save failed: {e}", "ERR")

_ai_config = _load_ai_config()

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
        "base_url": "http://localhost:1234/v1",
        "api_key":  "not-needed",
        "model":    _ai_config["last_local_model"],
    },
    "openai": {
        "type":     "openai_compat",
        "base_url": "https://api.openai.com/v1",
        "api_key":  os.environ.get("LLM_KEY_OPENAI"),
        "model":    "gpt-4o-mini",
    },
    "gemini": {
        "type":    "gemini",
        "api_key": os.environ.get("GEMINI_API_KEY"),
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
You are TITAN AI, a quantitative trading analyst for the TITAN Polymarket paper-trading engine.
Parse the [LIVE SYSTEM SNAPSHOT] carefully. Answer questions about positions, P&L,
signals, and wallets. Be concise, sharp, and data-first. Under 400 words.

You have access to a live TITAN MCP server. Always prefer calling MCP tools for fresh
data rather than relying on the snapshot alone. The full list of available tools is
appended below — use it to answer questions accurately.

When asked about a specific wallet by name, always call get_tracked_wallets(search="name")
rather than loading the full roster — it filters server-side and returns instantly.

A knowledge base of documentation is available via two tools:
  get_docs()         — lists all available docs with descriptions
  read_doc(path)     — returns the full content of a doc

At the start of every session, call read_doc("TITAN_AI_GUIDE.md") to load the
entry point. It contains the architecture overview, signal tiers, known loss
patterns, and links to all other docs. Then call the specific config or strategy
doc before proposing any parameter change.
"""


def _save_ai_request_snapshot(messages: list[dict]) -> None:
    log_dir = "Logs"
    os.makedirs(log_dir, exist_ok=True)
    fname = os.path.join(log_dir, f"titan_snapshot_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt")
    with open(fname, "w", encoding="utf-8") as f:
        f.write(json.dumps(messages, ensure_ascii=False, indent=2) + "\n")


def _json_dump(value) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def _summarize_tool_result(value: object) -> str:
    if isinstance(value, str):
        return f"{len(value)} chars"
    if isinstance(value, list):
        return f"list[{len(value)}]"
    if isinstance(value, dict):
        preview_keys = ", ".join(str(k) for k in list(value.keys())[:4])
        more = "…" if len(value) > 4 else ""
        return f"dict[{len(value)}] {preview_keys}{more}".strip()
    if value is None:
        return "null"
    return type(value).__name__


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
                self._tools_cache = self._client.list_tools()
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
        _log(f"AI MCP bridge: name={name!r} allowed={sorted(allowed)}", "DEBUG")
        if name not in allowed:
            raise RuntimeError(f"Tool not allowed: {name!r} (available: {sorted(allowed)})")
        safe_arguments = arguments or {}
        try:
            return self._client.call_tool(name, safe_arguments)
        except Exception as exc:
            _log(f"MCP tool call failed: {name} args={safe_arguments} error={exc}", "ERR")
            raise


# ── litellm model string ──────────────────────────────────────────────────────

def _litellm_model() -> str:
    b = _backend
    btype = b["type"]
    model = b["model"]
    if btype == "ollama":
        return f"ollama/{model}"
    if btype == "gemini":
        return f"gemini/{model}"
    if btype == "openai_compat":
        name = ACTIVE_BACKEND
        if name == "groq":
            return f"groq/{model}"
        if name == "openai":
            return model
        # local / LM Studio: pass model name as-is — litellm provider is set
        # explicitly via custom_llm_provider in _litellm_kwargs() to avoid litellm
        # misreading the model name (e.g. "qwen/..." triggering Qwen prompt templates)
        return model
    return model


def _litellm_kwargs() -> dict:
    b = _backend
    kwargs: dict = {"api_key": b.get("api_key") or "not-needed", "timeout": 120}
    btype = b.get("type")
    if btype in ("openai_compat", "ollama") and b.get("base_url"):
        kwargs["api_base"] = b["base_url"]
    if btype == "gemini" and b.get("api_key"):
        kwargs["api_key"] = b["api_key"]
    # For local openai-compat endpoints, force the openai provider so litellm never
    # applies a vendor-specific prompt template based on the model name string.
    if btype == "openai_compat" and ACTIVE_BACKEND not in ("openai", "groq"):
        kwargs["custom_llm_provider"] = "openai"
    return kwargs


# ── helpers ───────────────────────────────────────────────────────────────────

def _strip_think(text: str) -> str:
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


def _clean_text(text: str) -> str:
    return _strip_think(text or "").strip()


def _tc_name(tc) -> str:
    fn = getattr(tc, "function", None) or {}
    if hasattr(fn, "name"):
        return fn.name or ""
    return str(fn.get("name", ""))


def _tc_args(tc) -> dict:
    fn = getattr(tc, "function", None) or {}
    args = fn.arguments if hasattr(fn, "arguments") else fn.get("arguments")
    if isinstance(args, dict):
        return args
    if isinstance(args, str) and args.strip():
        try:
            return json.loads(args)
        except Exception:
            return {}
    return {}


def _tc_id(tc, idx: int) -> str:
    return str(getattr(tc, "id", None) or f"toolcall_{idx}")


# ── tool loop (used by both streaming and non-streaming paths) ────────────────

def _run_tool_loop(
    messages: list[dict],
    bridge: TitanMCPBridge,
    on_tool_call=None,
    on_tool_result=None,
) -> str:
    tools = bridge.openai_tools()
    model = _litellm_model()
    kwargs = _litellm_kwargs()

    for step in range(TOOL_LOOP_MAX_STEPS):
        resp = litellm.completion(
            model=model,
            messages=messages,
            tools=tools,
            tool_choice="auto",
            temperature=0.2,
            stream=False,
            **kwargs,
        )
        msg = resp.choices[0].message
        tool_calls = msg.tool_calls or []

        if not tool_calls:
            return _clean_text(msg.content or "")

        # append assistant turn with tool_calls
        messages.append({
            "role": "assistant",
            "content": msg.content or "",
            "tool_calls": [
                {
                    "id":       _tc_id(tc, i),
                    "type":     "function",
                    "function": {
                        "name":      _tc_name(tc),
                        "arguments": json.dumps(_tc_args(tc), ensure_ascii=False),
                    },
                }
                for i, tc in enumerate(tool_calls, 1)
            ],
        })

        for idx, tc in enumerate(tool_calls, 1):
            name = _tc_name(tc)
            args = _tc_args(tc)
            _log(f"AI tool[{idx}/{step+1}]: {name}({args})", "DEBUG")
            if on_tool_call:
                on_tool_call(name, args)
            try:
                result = bridge.call(name, args)
                summary = _summarize_tool_result(result)
                _log(f"AI tool[{idx}/{step+1}]: {name} → {summary}", "INFO")
                if on_tool_result:
                    on_tool_result(name, args, True, summary)
            except Exception as exc:
                _log(f"AI tool[{idx}/{step+1}]: {name} FAILED — {exc}", "ERR")
                if on_tool_result:
                    on_tool_result(name, args, False, str(exc))
                result = {"error": str(exc)}
            messages.append({
                "role":         "tool",
                "tool_call_id": _tc_id(tc, idx),
                "name":         name,
                "content":      _json_dump(result),
            })

    _log("AI tool loop: max steps reached without final answer", "WARN")
    return ""


# ── streaming fallback (no tools) ─────────────────────────────────────────────

def _dispatch_stream(messages: list[dict], on_token, on_done, on_error) -> None:
    model  = _litellm_model()
    kwargs = _litellm_kwargs()
    try:
        stream = litellm.completion(
            model=model,
            messages=messages,
            temperature=0.2,
            stream=True,
            **kwargs,
        )
        full = ""
        for chunk in stream:
            tok = (chunk.choices[0].delta.content or "") if chunk.choices else ""
            if tok:
                full += tok
                on_token(tok)
        on_done(full)
    except Exception as exc:
        on_error(str(exc))


_JINJA_TOOL_ERROR = (
    "Error rendering prompt with jinja template",
    "Cannot call something that is not a function",
    "prompt template",
)


def _is_no_tool_support(exc: BaseException) -> bool:
    msg = str(exc)
    return any(s in msg for s in _JINJA_TOOL_ERROR)


def _dispatch_with_titan_tools(
    messages: list[dict],
    bridge: TitanMCPBridge,
    on_token, on_done, on_error,
    on_tool_call=None,
    on_tool_result=None,
) -> bool:
    try:
        tool_messages = [dict(m) for m in messages]
        final_text = _run_tool_loop(tool_messages, bridge, on_tool_call, on_tool_result)
        if not final_text:
            raise RuntimeError("Tool loop returned empty response")
        on_token(final_text)
        on_done(final_text)
        return True
    except Exception as exc:
        if _is_no_tool_support(exc):
            _log(f"Model {MODEL!r} does not support tool calls — falling back to stream. Fix: use a model with a tool-call prompt template.", "WARN")
        else:
            _log(f"AI tool dispatch failed: {exc}", "ERR")
        return False


# ── AI client ─────────────────────────────────────────────────────────────────

class TitanAIClient:
    def __init__(self, engine_module: TitanBackend | None = None):
        self._engine: TitanBackend | None = engine_module
        self._messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
        self._lock = threading.Lock()
        self._mcp = TitanMCPBridge()
        self._tools_unsupported = False   # set True after first jinja/template rejection

    def _build_system_prompt(self) -> str:
        try:
            tools = self._mcp.available_tools()
            tool_lines = "\n".join(
                f"  {t['name']}: {t.get('description', '').splitlines()[0]}"
                for t in tools
            )
            tool_section = f"\nAVAILABLE MCP TOOLS ({len(tools)}):\n{tool_lines}\n"
        except Exception as exc:
            tool_section = f"\n(MCP tool list unavailable: {exc})\n"
        return SYSTEM_PROMPT + tool_section

    def _build_request_messages(self, full_msg: str) -> list[dict]:
        history: list[dict] = []
        for message in self._messages:
            if message.get("role") == "system":
                continue
            history.append(dict(message))
        return [{"role": "system", "content": self._build_system_prompt()}, *history, {"role": "user", "content": full_msg}]

    def ask(self, user_msg: str, on_token, on_done, on_error, on_tool_call=None, on_tool_result=None) -> None:
        snapshot = ""
        if self._engine is not None:
            try:
                snapshot = self._engine.get_snapshot(compressed=True)
            except Exception as exc:
                _log(f"AI snapshot build failed: {exc}", "ERR")
                snapshot = f"(snapshot error: {exc})"

        full_msg = f"[LIVE SYSTEM SNAPSHOT]\n{snapshot}\n\n[USER QUESTION]\n{user_msg}"

        with self._lock:
            if len(self._messages) > 21:
                self._messages = [self._messages[0]] + self._messages[-10:]
            snapshot_messages = self._build_request_messages(full_msg)

        def _run():
            try:
                _save_ai_request_snapshot(snapshot_messages)
            except Exception as e:
                _log(f"AI snapshot save failed: {e}", "ERR")

            def _done_final(full: str) -> None:
                with self._lock:
                    self._messages.append({"role": "user", "content": user_msg})
                    self._messages.append({"role": "assistant", "content": full})
                on_done()

            def _on_token_and_done(final_text: str) -> None:
                on_token(final_text)
                _done_final(final_text)

            ok = _dispatch_with_titan_tools(
                snapshot_messages, self._mcp,
                _on_token_and_done, lambda _: None,
                on_error, on_tool_call, on_tool_result,
            )
            if not ok:
                # fallback: stream without tools
                chunks: list[str] = []
                def _buf(tok: str) -> None:
                    chunks.append(tok)
                    on_token(tok)
                def _stream_done(full: str) -> None:
                    _done_final(_clean_text(full or "".join(chunks)))
                _dispatch_stream(snapshot_messages, _buf, _stream_done, on_error)

        threading.Thread(target=_run, daemon=True).start()

    def clear_history(self) -> None:
        with self._lock:
            self._messages = [{"role": "system", "content": SYSTEM_PROMPT}]


# ── UI panel ──────────────────────────────────────────────────────────────────

class AIPanel:
    def __init__(self, parent: tk.Misc, engine_module: TitanBackend | None = None):
        self._engine: TitanBackend | None = engine_module
        self._client = TitanAIClient(engine_module=engine_module)
        self._busy   = False

        mono    = tkfont.Font(family="Courier", size=9)
        mono_sm = tkfont.Font(family="Courier", size=8)
        bold_hd = tkfont.Font(family="Courier", size=10, weight="bold")

        frame = tk.Frame(parent, bg=BG_DARK, width=400)
        frame.pack(fill="both", expand=True)
        frame.pack_propagate(False)

        self._hdr = hdr = tk.Frame(frame, bg=BG_MID, pady=6)
        hdr.pack(fill="x")
        tk.Label(hdr, text="🤖  TITAN AI", fg=FG_ACCENT, bg=BG_MID, font=bold_hd).pack(side="left", padx=10)

        # backend selector
        self._backend_var = tk.StringVar(value=ACTIVE_BACKEND)
        bk_menu = tk.OptionMenu(hdr, self._backend_var, *BACKENDS.keys())
        self._backend_var.trace_add("write", self._on_backend_var_changed)
        bk_menu.config(bg=BG_LIGHT, fg=FG_ACCENT, font=mono_sm, relief="flat",
                       activebackground=BG_MID, highlightthickness=0)
        bk_menu["menu"].config(bg=BG_MID, fg=FG_MAIN)
        bk_menu.pack(side="left", padx=4)

        self._status_var = tk.StringVar(value=f"⬤ {ACTIVE_BACKEND}/{MODEL}")
        tk.Label(hdr, textvariable=self._status_var, fg=FG_SYS, bg=BG_MID, font=mono_sm).pack(side="right", padx=8)

        # local model selector row (only visible when backend=local)
        self._model_row = tk.Frame(frame, bg=BG_MID, pady=2)
        self._model_var = tk.StringVar(value=BACKENDS["local"]["model"])
        self._model_menu = tk.OptionMenu(self._model_row, self._model_var,
                                         *_ai_config["local_models"])
        self._model_var.trace_add("write", self._on_local_model_changed)
        self._model_menu.config(bg=BG_LIGHT, fg=FG_ACCENT, font=mono_sm, relief="flat",
                                activebackground=BG_MID, highlightthickness=0)
        self._model_menu["menu"].config(bg=BG_MID, fg=FG_MAIN)
        self._model_menu.pack(side="left", padx=(10, 2))

        self._new_model_var = tk.StringVar()
        new_model_entry = tk.Entry(self._model_row, textvariable=self._new_model_var,
                                   bg=BG_INPUT, fg=FG_MAIN, font=mono_sm, width=22,
                                   insertbackground=FG_ACCENT, relief="flat",
                                   highlightthickness=1, highlightbackground=BORDER)
        new_model_entry.pack(side="left", padx=2)
        new_model_entry.bind("<Return>", lambda _e: self._save_new_local_model())
        tk.Button(self._model_row, text="+ Save", bg=BG_LIGHT, fg=FG_ACCENT, font=mono_sm,
                  relief="flat", padx=4, command=self._save_new_local_model).pack(side="left", padx=2)

        self._update_model_row_visibility()

        btn_bar = tk.Frame(frame, bg=BG_MID, pady=3)
        btn_bar.pack(fill="x")
        self._quick_btns = []
        for label, prompt in [
            ("📊 Portfolio",  "Summarise my current portfolio with every open position, P&L%, hold time, and wallets behind it."),
            ("⚠ Risk Check",  "Risk assessment: over-concentrated positions, fee-negative bets, what to close first?"),
            ("🐋 Wallets",    "Top 5 verified wallets by score. What markets are they active in?"),
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

    def _on_backend_var_changed(self, *_args: str) -> None:
        self._switch_backend(self._backend_var.get())
        self._update_model_row_visibility()

    def _update_model_row_visibility(self) -> None:
        if ACTIVE_BACKEND == "local":
            self._model_row.pack(fill="x", after=self._hdr)
        else:
            self._model_row.pack_forget()

    def _on_local_model_changed(self, *_args: str) -> None:
        name = self._model_var.get()
        if not name:
            return
        global MODEL
        BACKENDS["local"]["model"] = name
        MODEL = name
        _ai_config["last_local_model"] = name
        _save_ai_config(_ai_config)
        self._client.clear_history()
        self._status_var.set(f"⬤ local/{name}")
        self._write_system(f"🔀 Local model → {name}")

    def _save_new_local_model(self) -> None:
        name = self._new_model_var.get().strip()
        if not name:
            return
        models: list[str] = _ai_config.setdefault("local_models", [])
        if name not in models:
            models.insert(0, name)
            _save_ai_config(_ai_config)
            menu = self._model_menu["menu"]
            menu.add_command(label=name, command=tk._setit(self._model_var, name))
            self._write_system(f"💾 Saved model: {name}")
        self._new_model_var.set("")
        self._model_var.set(name)

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

        def _ui(fn):
            import traceback
            def _safe():
                try:
                    fn()
                except Exception:
                    tb = traceback.format_exc()
                    _log(f"AI UI crash:\n{tb}", "ERR")
                    try:
                        self._chat.configure(state="normal")
                        self._chat.insert(tk.END, f"\n[UI ERR] {tb}\n", "error")
                        self._chat.see(tk.END)
                        self._chat.configure(state="disabled")
                        self._busy = False
                        self._set_controls("normal")
                        self._status_var.set("⬤ ui-error")
                    except Exception:
                        pass
            self._chat.after(0, _safe)

        def on_token(tok: str) -> None:
            def _fn():
                self._chat.configure(state="normal")
                self._chat.insert(tk.END, tok, "ai")
                self._chat.see(tk.END)
                self._chat.configure(state="disabled")
            _ui(_fn)

        def on_tool_call(name: str, args: dict) -> None:
            args_str = ", ".join(f"{k}={v!r}" for k, v in args.items()) if args else ""
            label = f"{name}({args_str})" if args_str else name
            _log(f"AI MCP start: {label}", "INFO")
            def _fn():
                self._status_var.set(f"⬤ mcp… {name}")
                self._chat.configure(state="normal")
                self._chat.insert(tk.END, f"\n  ⚙ MCP → {label}\n", "tool")
                self._chat.see(tk.END)
                self._chat.configure(state="disabled")
            _ui(_fn)

        def on_tool_result(name: str, args: dict, ok: bool, detail: str) -> None:
            args_str = ", ".join(f"{k}={v!r}" for k, v in args.items()) if args else ""
            label = f"{name}({args_str})" if args_str else name
            level = "INFO" if ok else "ERR"
            direction = "←" if ok else "✖"
            prefix = "✓ MCP" if ok else "✗ MCP"
            _log(f"AI MCP {'done' if ok else 'failed'}: {label} | {detail}", level)
            def _fn():
                self._status_var.set(f"⬤ thinking… [{ACTIVE_BACKEND}]")
                self._chat.configure(state="normal")
                self._chat.insert(tk.END, f"  {prefix} {direction} {name}  {detail}\n", "tool" if ok else "error")
                self._chat.see(tk.END)
                self._chat.configure(state="disabled")
            _ui(_fn)

        def on_done() -> None:
            def _fn():
                self._prettify_last_message()
                self._write("\n", "ai")
                self._busy = False
                self._set_controls("normal")
                self._status_var.set(f"⬤ ready  [{ACTIVE_BACKEND}/{MODEL}]")
            _ui(_fn)

        def on_error(msg: str) -> None:
            def _fn():
                self._write(f"\n⚠ {msg}\n", "error")
                self._busy = False
                self._set_controls("normal")
                self._status_var.set("⬤ error")
            _ui(_fn)

        self._client.ask(text, on_token, on_done, on_error, on_tool_call, on_tool_result)

    def _set_controls(self, state: WidgetState) -> None:
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
