# 🐳 TITAN — Whale Mirror Engine

> Automated paper-trading bot for [Polymarket](https://polymarket.com) prediction markets.  
> Monitors selected wallets and mirrors their trades in real time.

![Python](https://img.shields.io/badge/Python-3.10%2B-blue?logo=python) ![Platform](https://img.shields.io/badge/Platform-Windows-lightgrey?logo=windows) ![Mode](https://img.shields.io/badge/Mode-Paper%20Trading-green)

---

## What is TITAN?

TITAN watches the Polymarket CLOB feed 24/7, identifies wallets with a proven track record (high win rate, positive PnL, large resolved bet history), and automatically paper-trades alongside them. When a whale buys, TITAN buys. When the whale sells, TITAN sells.

**It does not place real on-chain orders.** All trades are simulated against a virtual bankroll. Use it to validate a strategy before risking real money.

---

## Features

- **Whale detection** — Scores every wallet in the public feed and auto-promotes the best performers to Elite status
- **Two signal paths** — Standard 15s cycle for regular signals + 3s HFT fast loop for spike detection
- **Multi-strategy engine (v10)** — Three parallel strategies: Recent Form, Drift Discount, Consensus Basket
- **Auto-trading** — Fires paper buys/sells automatically on CONVICTION, ALERT, and HFT signals
- **Live desktop UI** — 9-tab Tkinter dashboard: signals, alerts, positions, P&L graph, whale roster, diagnostics, config editor
- **Hot-reload config** — Edit parameters in-app and they take effect on the next cycle, no restart needed
- **Telegram bot** — Optional remote monitoring: get P&L screenshots, open a web dashboard, or ask questions in plain text
- **Web dashboard** — JSON API + HTML page served locally, tunnelable via Cloudflare
- **MCP server** — Exposes the full engine as a standard [Model Context Protocol](https://modelcontextprotocol.io) server so any MCP-aware LLM client (Claude Desktop, Cursor, custom agents) can query and control TITAN natively

---

## Screenshots

> *(P&L tab, Signals tab, Positions tab)*

---

## Requirements

- Python 3.10+
- Windows (the `ImageGrab` Telegram screenshot feature is Windows-only; rest is cross-platform)

---

## Installation

```bash
# 1. Clone the repo
git clone https://github.com/Alaphilippe/polymarketV3.git
cd polymarketV3

# 2. Create a virtual environment
python -m venv .venv

# 3. Activate it
# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate

# 4. Install dependencies
pip install -r ScriptsTitan/requirements.txt
```

### Optional dependencies

| Feature | Package | Install |
|---|---|---|
| Telegram bot | *(configure in the repo-root `titan_config.json`)* | built-in via `requests` |
| Remote web dashboard | `pycloudflared` | `pip install pycloudflared` |

---

## Quick Start

TITAN has three run modes, all launched from the repo root via `run_titan.py`:

### Mode 1 — Desktop UI (in-process)

```bash
python run_titan.py --mode ui
```

Starts the engine and opens the Tkinter dashboard in the same process. The UI talks to the engine via a direct Python reference — zero latency, no network.

### Mode 2 — Headless MCP server

```bash
python run_titan.py --mode server
# Optional:
python run_titan.py --mode server --port 8080 --token mysecrettoken
```

Starts the engine without any UI and exposes it as a standard MCP server on `http://127.0.0.1:8765`. Any MCP client (Claude Desktop, Cursor, a custom agent) can connect, discover tools, and call them:

```
POST /mcp   ← JSON-RPC 2.0 requests (tools/list, tools/call, resources/read, …)
GET  /mcp   ← SSE stream for server-initiated notifications (price ticks, cycle events, …)
```

**To add TITAN to Claude Desktop**, add this to your `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "titan": {
      "url": "http://127.0.0.1:8765/mcp"
    }
  }
}
```

### Mode 3 — Remote UI (client mode)

```bash
python run_titan.py --mode client --url http://127.0.0.1:8765
```

Opens the full Tkinter dashboard but connects to a **remote** `--mode server` process over HTTP instead of running the engine locally. The UI is identical to `--mode ui`; only the data source changes. Useful for running the engine on a headless server and viewing it from your desktop.

---

## Configuration

All settings live in the repo-root `titan_config.json`. You can edit it:
- **In-app**: open the **CONFIG** tab, edit the JSON, click **SAVE & RELOAD**
- **Manually**: edit the file directly — changes are picked up on next hot-reload or restart
- **Via MCP**: call the `update_config` tool with a patch dict (unknown keys and nested values are rejected for safety)

### Key parameters

| Parameter | Default | Description |
|---|---|---|
| `BANKROLL_START` | `20.00` | Starting virtual bankroll in USD |
| `MIN_SCORE` | `50` | Minimum signal score to display |
| `ALERT_SCORE` | `68` | Score threshold to trigger an auto-trade |
| `MIN_ELITE_CONFLUENCE` | `2` | Minimum number of elite wallets on a signal |
| `MIN_ENTRY_PRICE` | `0.20` | Only enter markets priced above 20¢ |
| `MAX_ENTRY_PRICE` | `0.72` | Only enter markets priced below 72¢ |
| `PROFIT_TARGET_PCT` | `0.20` | Auto-exit at +20% |
| `STOP_LOSS_PCT` | `-0.30` | Hard stop at −30% |
| `WHALE_EXIT_SELL` | `true` | Mirror whale exits immediately |
| `MAX_OPEN_POSITIONS` | *(set in json)* | Max simultaneous open positions |

**Too few signals?** Try: lower `MIN_SCORE` to 40, lower `MIN_TRADE_CASH`, lower `MIN_LIQUIDITY`, raise `MAX_SIGNAL_AGE_H`.

---

## UI Overview

| Tab | Description |
|---|---|
| 🎯 SIGNALS | Live table of all scored signals this cycle |
| 🚨 ALERTS | Detailed view of tradeable signals (auto-buy status, score breakdown, whale intel) |
| 📋 POSITIONS | Open paper positions with live P&L, hold time, price chart. Double-click for full detail. |
| 📈 P&L | Equity curve, win rate, expectancy, full trade history |
| 🐳 WHALES | Whale roster filtered by tier (All / Elite / Verified / HFT) |
| 📊 ANALYSIS | Per-cycle summary: signal counts by tier, elite metrics, account stats |
| 🔍 DIAG | Why signals were rejected, active cooldowns, failed wallet scores |
| 📜 LOG | Full system log + **"Copy Snapshot for AI"** button |
| ⚙ CONFIG | In-app JSON editor with parameter guide panel |

---

## MCP Tools

When running in `--mode server`, the following tools are available to any MCP client:

| Tool | Read/Write | Description |
|---|---|---|
| `get_status` | read | Engine health, uptime, cycle count, error count |
| `get_portfolio_overview` | read | Equity, bankroll, session P&L, position count — best first call |
| `get_positions` | read | Open positions. `brief=true` (default) returns clean summary; `brief=false` returns full dict |
| `get_signals` | read | Whale-triggered signals. Filter by `min_score` (typical range 0–100) |
| `get_alerts` | read | Recent WARN/ERROR/ALERT log entries |
| `get_whales` | read | Elite whale roster with performance metrics |
| `get_pnl_summary` | read | Bankroll, P&L, equity curve tail, cooldown/watchlist state |
| `get_trade_history` | read | Full buy/sell history |
| `get_closed_positions` | read | Closed positions enriched with price history |
| `get_recent_errors` | read | Structured ERROR/CRITICAL log events |
| `get_config` | read | Current live config |
| `get_logs` | read | Raw log tail |
| `get_snapshot` | read | Full engine state snapshot (AI-digestible, compressed by default) |
| `update_config` | write | Patch config. Rejects unknown keys and nested values. Supports `dry_run=true`. |

Push notifications are streamed over SSE (`GET /mcp`): price ticks, cycle completions, position opens/closes, alerts.

---

## How Signals Work

Signals are grouped by `(market, outcome)`. Each one is scored 0–100:

| Component | Max pts |
|---|---|
| Wallet quality | 30 |
| Confluence (# agreeing whales) | 18 |
| Recency | 20 |
| Price in ideal zone | 15 |
| Market quality (liq/vol) | 10 |
| Conviction bonus | 5 |
| Exit penalty | −8× |

**Tiers:**

| Tier | Meaning | Auto-trade |
|---|---|---|
| 💎 CONVICTION | Whale commits ≥ 0.5% portfolio or ≥ $1000 | Yes |
| 🚨 ALERT | High-score signal, all gates passed | Yes |
| ⚡ HFT | HFT wallet spike (20–40× their average) | Yes |
| 🟡 STRONG | Good signal, below auto-trade threshold | Display only |
| 🔵 MEDIUM | Lower confidence | Display only |

---

## Exit Logic

TITAN's philosophy: **follow the whale out**.

1. If the whale who triggered the buy **sells** → immediate exit (`WHALE_EXIT_SELL`)
2. Profit target hit (+20% default) → exit even if whale still holds
3. Trailing stop activates at +15%, trails 10% from peak
4. Hard stop loss at −30% (optional, toggleable)
5. Min hold guard prevents premature exits
6. Cooldown blocks re-entry on the same market after a close

---

## Telegram Bot (Optional)

Set your bot token and chat ID in the repo-root `titan_config.json`. Once enabled:

| Message | Response |
|---|---|
| `pl` or `pnl` | Sends a screenshot of the P&L tab |
| `dash` or `dashboard` | Opens a Cloudflare tunnel and sends a web dashboard link |
| Any other text | Answered by Groq LLaMA-3.3-70b with live system context |

The bot also pushes notifications on boot, every buy, every sell, and on errors.

---

## Architecture

```
run_titan.py              ← Entry point. --mode ui | server | client
ScriptsTitan/
  titan_api.py            ← Single public interface to all engine functionality
  titan_server.py         ← MCP Streamable HTTP server (POST+GET /mcp, SSE)
  titan_client.py         ← MCP client shim — duck-types TitanAPI, speaks HTTP
  titan_ui.py             ← Tkinter dashboard. Accepts TitanAPI or TitanClient.
  titan_engine.py         ← Main orchestration. 15s loop + 3s HFT loop.
  titan_signals.py        ← Signal building + multi-strategy scoring.
  titan_wallet.py         ← Wallet fetching, scoring, elite discovery.
  titan_market.py         ← Market metadata + CLOB price feeds.
  titan_trader.py         ← Paper trade execution + Kelly sizing.
  titan_state.py          ← Shared singleton state (positions, bankroll, logs).
  titan_config.py         ← Config loader + hot-reload.
  titan_persistence.py    ← Save/load state & whale roster to disk.
  titan_telegram.py       ← Optional Telegram bot.
```

The UI has no direct knowledge of the engine. It only calls `TitanAPI` methods. In `--mode ui` it holds a direct Python reference; in `--mode client` it holds a `TitanClient` that proxies every call over HTTP to a running server. Both implement the same interface.

---

## Debugging with AI

The **LOG tab** has a "Copy Full Snapshot for AI" button. It copies a structured dump of:
- Current bankroll and all open positions
- All signals from the last cycle with score breakdowns
- All signal rejections with reasons
- The full elite roster
- Last 100 trades
- Last 600 system log lines

Paste it directly into any AI chat for instant context-aware debugging.

In `--mode server`, any connected MCP client (e.g. Claude Desktop) can call `get_snapshot` directly and has access to all live tools without copy-pasting anything.

---

## Disclaimer

TITAN is a research and paper-trading tool. It does not execute real trades. Past whale performance does not guarantee future results. Use at your own risk.
