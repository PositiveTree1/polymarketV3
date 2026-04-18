"""
╔══════════════════════════════════════════════════════════════════════════════╗
║             TITAN V3 — AI SIDEKICK  (titan_ai.py)                           ║
║                                                                              ║
║  Embeds as a side panel inside titan_ui.py OR runs standalone.               ║
║  Powered by local Ollama. Engine/UI work perfectly without this file.        ║
║                                                                              ║
║  Usage from titan_ui.py:                                                     ║
║      from titan_ai import attach_ai_panel                                    ║
║      attach_ai_panel(root, engine)                                           ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

from __future__ import annotations
import tkinter as tk
from tkinter import font as tkfont, scrolledtext
import threading
import time
import json
import requests
from datetime import datetime

# ── Ollama settings ───────────────────────────────────────────────────────────
OLLAMA_URL   = "http://localhost:11434/api/chat"
OLLAMA_MODEL = "gemma4:e4b"   # change to any model you have pulled

# ── Design tokens (match titan_ui dark palette) ───────────────────────────────
BG_DARK    = "#080810"
BG_MID     = "#0d0d1a"
BG_LIGHT   = "#13132a"
BG_INPUT   = "#060612"
FG_MAIN    = "#cccccc"
FG_ACCENT  = "#00ff88"
FG_USER    = "#00aaff"
FG_AI      = "#ccffee"
FG_SYS     = "#556655"
FG_WARN    = "#ffaa00"
FG_ERR     = "#ff4444"
BORDER     = "#1a2a4a"


SYSTEM_PROMPT = """\
You are TITAN AI, a high-level quantitative trading analyst specifically calibrated for the \
TITAN V5 Polymarket engine.

IDENTITY & EXPERTISE:
• You have deep knowledge of Polymarket's order book dynamics and whale behavior.
• You are an expert in Kelly Criterion betting and risk-adjusted returns.
• You analyze signals with a skeptical, data-first mindset.

YOUR MISSION:
• Fully parse the [LIVE SYSTEM SNAPSHOT] provided in each message.
• CRITICAL: Look at the [STATISTICS] and [OPEN POSITIONS] sections for account data.
• Answer user questions regarding positions, P&L, signals, and whale wallets.
• CRITIQUE the system: If a position looks risky or a signal is weak, say so.
• Provide actionable advice on parameter tweaks (MIN_SCORE, KELLY, etc.).
• Use clear formatting. Bold key terms using **markdown**. Use bullet points.

CORE LOGIC KNOWLEDGE:
• Entry: Needs ≥2 verified whales (or elite single-whale if incontested).
• Sizing: Blended Proportional and Kelly minus a 3.6% round-trip fee.
• Exits: 35% profit target, whale-sell detection, or nearing expiry (<1.5h).
• Minimums: Under $500 bankroll makes sizing difficult due to fee drag.

Always reference the live data as the 'Truth'. Be concise, sharp, and professional.
Keep reports under 400 words.
"""


# ═══════════════════════════════════════════════════════════════════════════════
#  CORE AI CLIENT
# ═══════════════════════════════════════════════════════════════════════════════
class TitanAIClient:
    """Thread-safe Ollama chat client with system-context injection."""

    def __init__(self):
        self._messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
        self._lock = threading.Lock()
        self._warmed = False

    def warmup(self):
        """Pre-load model into VRAM (called once in background)."""
        try:
            requests.post(OLLAMA_URL, json={
                "model": OLLAMA_MODEL,
                "messages": [{"role": "user", "content": ""}],
                "keep_alive": -1,
                "stream": False,
            }, timeout=8)
            self._warmed = True
        except Exception:
            pass

    def ask(self, user_msg: str, snapshot: str, on_token, on_done, on_error):
        """
        Send a message to the AI in a background thread.
        Prepends the live snapshot so the model always has fresh context.
        Calls on_token(str) for each streamed chunk, on_done() when finished.
        """
        full_msg = f"[LIVE SYSTEM SNAPSHOT]\n{snapshot}\n\n[USER QUESTION]\n{user_msg}"

        with self._lock:
            # 1. Truncate previous snapshots in history to save context space
            # The AI only needs the CURRENT snapshot to answer the CURRENT question.
            for m in self._messages:
                if m["role"] == "user" and "[LIVE SYSTEM SNAPSHOT]" in m["content"]:
                    # Keep the user question, but remove the massive snapshot
                    parts = m["content"].split("[USER QUESTION]")
                    if len(parts) > 1:
                        m["content"] = f"[LIVE SYSTEM SNAPSHOT]\n(TRUNCATED: See latest message for current state)\n\n[USER QUESTION]" + parts[1]

            # 2. Keep context window manageable: trim to last 10 exchanges + system
            if len(self._messages) > 21:
                self._messages = [self._messages[0]] + self._messages[-10:]
            
            self._messages.append({"role": "user", "content": full_msg})

        def _run():
            payload = {
                "model":      OLLAMA_MODEL,
                "messages":   self._messages,
                "stream":     True,
                "think":      False,
                "keep_alive": -1,
                "options": {
                    "num_ctx":     8192,   # Increased context slightly for better reasoning
                    "temperature": 0.2,    # Slightly lower for more precise analysis
                    "top_k":       20,
                    "num_predict": 1024,
                },
            }
            try:
                resp = requests.post(OLLAMA_URL, json=payload, stream=True, timeout=120)
                if resp.status_code != 200:
                    on_error(f"Ollama error {resp.status_code}: {resp.text[:200]}")
                    with self._lock:
                        self._messages.pop()
                    return

                full_reply = ""
                for line in resp.iter_lines():
                    if not line:
                        continue
                    try:
                        chunk = json.loads(line.decode("utf-8"))
                        content = chunk.get("message", {}).get("content", "")
                        if content:
                            # Filter out common thinking tokens explicitly
                            clean_content = content.replace("[ACTION]", "")
                            if clean_content:
                                full_reply += clean_content
                                on_token(clean_content)
                        if chunk.get("done"):
                            break
                    except json.JSONDecodeError:
                        continue

                with self._lock:
                    self._messages.append({"role": "assistant", "content": full_reply})
                on_done()

            except requests.exceptions.ConnectionError:
                on_error("Cannot connect to Ollama. Is it running?\n  → Run: ollama serve")
                with self._lock:
                    self._messages.pop()
            except Exception as e:
                on_error(f"Error: {e}")
                with self._lock:
                    self._messages.pop()

        threading.Thread(target=_run, daemon=True).start()

    def clear_history(self):
        with self._lock:
            self._messages = [{"role": "system", "content": SYSTEM_PROMPT}]


# ═══════════════════════════════════════════════════════════════════════════════
#  TKINTER PANEL
# ═══════════════════════════════════════════════════════════════════════════════
class AIPanel:
    """
    Self-contained Tkinter side panel.
    Call attach(parent_frame, engine_module) to wire it up.
    """

    def __init__(self, parent: tk.Widget, engine_module=None):
        self._engine  = engine_module
        self._client  = TitanAIClient()
        self._busy    = False

        mono    = tkfont.Font(family="Courier", size=9)
        mono_sm = tkfont.Font(family="Courier", size=8)
        bold_hd = tkfont.Font(family="Courier", size=10, weight="bold")

        # ── Outer container ──────────────────────────────────────────────────
        frame = tk.Frame(parent, bg=BG_DARK, width=400)
        frame.pack(fill="both", expand=True)
        frame.pack_propagate(False)

        # ── Header ───────────────────────────────────────────────────────────
        hdr = tk.Frame(frame, bg=BG_MID, pady=6)
        hdr.pack(fill="x")
        tk.Label(hdr, text="🤖  TITAN AI  SIDEKICK",
                 fg=FG_ACCENT, bg=BG_MID, font=bold_hd).pack(side="left", padx=10)
        tk.Label(hdr, text=f"model: {OLLAMA_MODEL}",
                 fg=FG_SYS, bg=BG_MID, font=mono_sm).pack(side="left")
        self._status_var = tk.StringVar(value="⬤ idle")
        tk.Label(hdr, textvariable=self._status_var,
                 fg="#556655", bg=BG_MID, font=mono_sm).pack(side="right", padx=8)

        # ── Quick-action buttons ──────────────────────────────────────────────
        btn_bar = tk.Frame(frame, bg=BG_MID, pady=3)
        btn_bar.pack(fill="x")
        self._quick_btns = []
        for label, prompt in [
            ("📊 Portfolio",  "Summarise my current portfolio. List every open position with entry, current price, P&L%, hold time, and the whale wallets behind it. Flag any concerning ones."),
            ("⚠ Risk Check",  "Perform a risk assessment: are any positions over-concentrated in the same event? Are any bets fee-negative given the current bankroll? What should I close first?"),
            ("🐋 Whales",     "Who are my top 5 verified whales by Wilson LB? What markets are they active in right now according to the active signals section?"),
            ("📈 P&L Report", "Give me a detailed P&L report: total closed PnL, win rate, average profit, best and worst trade. What's the trend?"),
            ("🔍 Diagnose",   "Look at the last 100 log lines. Are there any errors, warnings, or unusual patterns I should investigate?"),
        ]:
            b = tk.Button(btn_bar, text=label, bg=BG_LIGHT, fg=FG_ACCENT,
                          font=mono_sm, relief="flat", padx=6,
                          command=lambda p=prompt: self._send(p))
            b.pack(side="left", padx=2, pady=2)
            self._quick_btns.append(b)

        # ── Chat display ──────────────────────────────────────────────────────
        chat_frame = tk.Frame(frame, bg=BG_DARK)
        chat_frame.pack(fill="both", expand=True, padx=4, pady=(4, 0))

        self._chat = scrolledtext.ScrolledText(
            chat_frame, bg=BG_INPUT, fg=FG_MAIN, font=mono,
            selectbackground="#1a2a4a", wrap=tk.WORD,
            state="disabled", relief="flat",
        )
        self._chat.pack(fill="both", expand=True)
        self._chat.tag_configure("user",    foreground=FG_USER,   font=bold_hd)
        self._chat.tag_configure("ai",      foreground=FG_AI)
        self._chat.tag_configure("system",  foreground=FG_SYS)
        self._chat.tag_configure("warn",    foreground=FG_WARN)
        self._chat.tag_configure("error",   foreground=FG_ERR)
        self._chat.tag_configure("ts",      foreground="#334455")
        self._chat.tag_configure("bold",    foreground="#ffffff", font=bold_hd)
        self._chat.tag_configure("bullet",  foreground=FG_ACCENT)

        # ── Input row ─────────────────────────────────────────────────────────
        inp_frame = tk.Frame(frame, bg=BG_MID, pady=4)
        inp_frame.pack(fill="x", padx=4, pady=4)

        self._inp = tk.Text(inp_frame, bg=BG_INPUT, fg=FG_MAIN, font=mono,
                            height=3, wrap="word", relief="flat",
                            insertbackground=FG_ACCENT,
                            highlightthickness=1, highlightbackground=BORDER)
        self._inp.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self._inp.bind("<Return>", self._on_enter)
        self._inp.bind("<Shift-Return>", lambda e: None)  # allow newlines

        btn_col = tk.Frame(inp_frame, bg=BG_MID)
        btn_col.pack(side="right")
        self._send_btn = tk.Button(
            btn_col, text="SEND\n⏎", bg="#1a332a", fg=FG_ACCENT, font=bold_hd,
            padx=8, relief="flat", command=lambda: self._send(self._inp.get("1.0", "end").strip())
        )
        self._send_btn.pack(pady=(0, 4))
        tk.Button(btn_col, text="CLR", bg="#1a1a30", fg="#556677", font=mono_sm,
                  padx=8, relief="flat",
                  command=self._clear).pack()

        # ── Boot ──────────────────────────────────────────────────────────────
        self._write_system("🤖 TITAN AI online. Ask anything about your positions, P&L, or strategy.")
        self._write_system(f"   Model: {OLLAMA_MODEL}  |  Warming up VRAM…")
        threading.Thread(target=self._warmup_thread, daemon=True).start()

    # ── Private helpers ───────────────────────────────────────────────────────
    def _warmup_thread(self):
        self._client.warmup()
        self._write_system("   ✅ Model warm. Ready.")
        self._status_var.set(f"⬤ ready  [{OLLAMA_MODEL}]")

    def _write(self, text, tag="ai"):
        self._chat.configure(state="normal")
        self._chat.insert(tk.END, text, tag)
        self._chat.see(tk.END)
        self._chat.configure(state="disabled")

    def _write_system(self, msg):
        ts = datetime.now().strftime("%H:%M:%S")
        self._write(f"[{ts}] ", "ts")
        self._write(msg + "\n", "system")

    def _on_enter(self, event):
        if event.state & 0x1:   # Shift held → normal newline
            return
        text = self._inp.get("1.0", "end").strip()
        if text:
            self._send(text)
        return "break"

    def _send(self, text: str):
        if not text or self._busy:
            return
        self._inp.delete("1.0", tk.END)
        self._busy = True
        self._set_controls_state("disabled")
        self._status_var.set("⬤ thinking…")

        ts = datetime.now().strftime("%H:%M:%S")
        self._write(f"\n[{ts}] ", "ts")
        self._write(f"You: {text}\n", "user")

        # Build snapshot
        snapshot = ""
        if self._engine and hasattr(self._engine, "get_system_snapshot"):
            try:
                snapshot = self._engine.get_system_snapshot()
            except Exception as e:
                snapshot = f"(snapshot error: {e})"

        ts_ai = datetime.now().strftime("%H:%M:%S")
        self._write(f"[{ts_ai}] ", "ts")
        self._write("TITAN AI: ", "user")

        def on_token(tok):
            # We schedule updates on the main thread via root.after or equivalent if needed
            # but standard tkinter handles text inserts somewhat thread-safely in basic cases.
            self._chat.configure(state="normal")
            self._chat.insert(tk.END, tok, "ai")
            self._chat.see(tk.END)
            self._chat.configure(state="disabled")

        def on_done():
            self._prettify_last_message()
            self._write("\n", "ai")
            self._busy = False
            self._set_controls_state("normal")
            self._status_var.set(f"⬤ ready  [{OLLAMA_MODEL}]")

        def on_error(msg):
            self._write(f"\n⚠ {msg}\n", "error")
            self._busy = False
            self._set_controls_state("normal")
            self._status_var.set("⬤ error")

        self._client.ask(text, snapshot, on_token, on_done, on_error)

    def _set_controls_state(self, state):
        try:
            self._send_btn.configure(state=state)
            self._inp.configure(state=state)
            for b in self._quick_btns:
                b.configure(state=state)
        except Exception:
            pass

    def _clear(self):
        self._client.clear_history()
        self._chat.configure(state="normal")
        self._chat.delete("1.0", tk.END)
        self._chat.configure(state="disabled")
        self._write_system("🔄 Conversation cleared. Context reset.")

    def _prettify_last_message(self):
        """Scan the last AI message for **bold** and * bullets."""
        try:
            self._chat.configure(state="normal")
            
            # 1. Find the last AI message start
            # We look for the last "TITAN AI:" label
            start_idx = self._chat.search("TITAN AI:", "end", backwards=True)
            if not start_idx: return
            
            content_start = self._chat.index(f"{start_idx} + 9 chars")
            end_idx = "end"

            # 2. Render Bullets (replace * at start of line with cleaner ⬤)
            curr = content_start
            while True:
                idx = self._chat.search("* ", curr, stopindex=end_idx)
                if not idx: break
                # Check if it's at start of line (possibly with whitespace)
                line_start = self._chat.index(f"{idx} linestart")
                # If the only thing between line_start and idx is whitespace, it's a bullet
                prefix = self._chat.get(line_start, idx)
                if not prefix.strip():
                    self._chat.delete(idx, f"{idx} + 1 chars")
                    self._chat.insert(idx, "•", "bullet")
                curr = f"{idx} + 1 chars"

            # 3. Render Bold (**text**)
            curr = content_start
            while True:
                # Find start **
                b_start = self._chat.search("**", curr, stopindex=end_idx)
                if not b_start: break
                
                # Find end **
                b_end = self._chat.search("**", f"{b_start} + 2 chars", stopindex=end_idx)
                if not b_end: break
                
                # Apply bold tag to content
                self._chat.tag_add("bold", f"{b_start} + 2 chars", b_end)
                
                # Remove the ** markers (end first so b_start index stays valid)
                self._chat.delete(b_end, f"{b_end} + 2 chars")
                self._chat.delete(b_start, f"{b_start} + 2 chars")
                
                curr = b_start # continue from where we were

            self._chat.configure(state="disabled")
        except Exception as e:
            print(f"Prettify error: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
#  PUBLIC API — called from titan_ui.py
# ═══════════════════════════════════════════════════════════════════════════════
def attach_ai_panel(root: tk.Tk, engine_module=None) -> AIPanel:
    """
    Injects the AI side panel into the running Titan V3 UI.
    Call this AFTER the main notebook (nb) has been created but before mainloop.

    The panel is added as a right-side pane separated by a sash (PanedWindow).
    The existing notebook shrinks to accommodate it.
    """
    # We need to reparent the notebook into a PanedWindow.
    # Because the notebook is already packed into root, we create a new layout.

    # Find the notebook widget to grab its current parent
    # titan_ui.py packs it directly into root — so we use root as parent.
    pw = tk.PanedWindow(root, orient="horizontal", bg="#080810",
                        sashwidth=5, sashrelief="flat",
                        sashpad=0, handlesize=0)
    pw.pack(fill="both", expand=True)

    # The main content area (left pane)
    left = tk.Frame(pw, bg="#080810")
    pw.add(left, minsize=800, stretch="always")

    # AI panel (right pane)
    right = tk.Frame(pw, bg="#080810")
    pw.add(right, minsize=360, stretch="never")

    panel = AIPanel(right, engine_module=engine_module)
    return panel, left


# ═══════════════════════════════════════════════════════════════════════════════
#  STANDALONE MODE — python titan_ai.py
# ═══════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    root = tk.Tk()
    root.title("🤖 TITAN AI — Standalone")
    root.geometry("500x700")
    root.configure(bg="#080810")
    panel = AIPanel(root, engine_module=None)
    root.mainloop()
