"""
TITAN — Real-Time Resolution Monitor.

PROBLEM SOLVED:
  The old system only detected resolution via price polling (Gamma REST API,
  every 12s cycle). If a football match ends and the Gamma circuit breaker is
  open (or the market's conditionId returns 422), the bot sits blind with a
  stale price of ~0.78 while the market has already resolved to 0.0 or 1.0.

SOLUTION:
  WebSocket subscription to Polymarket's CLOB market channel.
  When custom_feature_enabled=True, the server sends a `market_resolved` event
  the instant a market settles. This fires BEFORE the Gamma API reflects it.

  URL: wss://ws-subscriptions-clob.polymarket.com/ws/market
  No authentication required.
  Subscribe by token ID (asset), not conditionId.

  Additionally: we watch for price_change / last_trade_price / best_bid_ask
  events where prices move to ≥0.97 or ≤0.03 — this catches near-resolution
  even before the official market_resolved message arrives.

ARCHITECTURE:
  - Runs as a daemon thread, started by titan_engine.start()
  - Maintains a WebSocket connection with auto-reconnect
  - Subscribes to token IDs of ALL open positions
  - When a position's token resolves: sets a flag in S.env().ws_resolved_cids
    so the main loop's auto_trade() can pick it up next cycle
  - Also fires immediate resolution via _handle_ws_resolution()
  - Dynamically subscribes/unsubscribes as positions open/close
"""

import asyncio
import json
import threading
import time
import websockets
import websockets.exceptions

import titan_state as S
from titan_state import _log

WS_URI     = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
PING_INTERVAL = 10   # seconds — Polymarket requires PING within 10s or drops conn
RECONNECT_DELAY_BASE = 2
RECONNECT_DELAY_MAX  = 30

# Shared state: cids that the WS confirmed as resolved
# { cid: {"price": float, "outcome": str, "ts": float} }
# titan_trader.py checks this dict on every position-exit loop.
ws_resolved_cids: dict = {}

# Currently subscribed token IDs → cid mapping
_subscribed_tokens: dict = {}   # token_id → cid
_subscribed_cids:   dict = {}   # cid → set of token_ids

_ws_loop: asyncio.AbstractEventLoop | None = None
_ws_conn = None    # current websockets.WebSocketClientProtocol
_ws_lock = threading.Lock()


# ─────────────────────────────────────────────────────────────────────────────
#  Public API — called from titan_engine / titan_trader
# ─────────────────────────────────────────────────────────────────────────────

def subscribe_position(cid: str, token_ids: list[str]):
    """
    Subscribe to WebSocket updates for an open position.
    token_ids = list of asset token IDs (usually 1 or 2 per market).
    Called immediately after a BUY is executed.
    """
    with _ws_lock:
        tokens_to_add = []
        for tid in token_ids:
            if tid and tid not in _subscribed_tokens:
                _subscribed_tokens[tid] = cid
                tokens_to_add.append(tid)
        if cid not in _subscribed_cids:
            _subscribed_cids[cid] = set()
        _subscribed_cids[cid].update(t for t in token_ids if t)

    if tokens_to_add and _ws_loop:
        asyncio.run_coroutine_threadsafe(
            _send_subscribe(tokens_to_add), _ws_loop
        )


def unsubscribe_position(cid: str):
    """
    Unsubscribe from a position's tokens after closing.
    """
    with _ws_lock:
        tokens = list(_subscribed_cids.pop(cid, set()))
        for t in tokens:
            _subscribed_tokens.pop(t, None)

    if tokens and _ws_loop:
        asyncio.run_coroutine_threadsafe(
            _send_unsubscribe(tokens), _ws_loop
        )


def is_resolved_via_ws(cid: str) -> dict | None:
    """
    Returns resolution info if the WebSocket confirmed this market resolved.
    { "price": float, "outcome": str, "ts": float } or None.
    """
    return ws_resolved_cids.get(cid)


def get_subscribed_count() -> int:
    return len(_subscribed_cids)


# ─────────────────────────────────────────────────────────────────────────────
#  WebSocket async internals
# ─────────────────────────────────────────────────────────────────────────────

async def _send_subscribe(token_ids: list[str]):
    global _ws_conn
    if not _ws_conn:
        return
    try:
        msg = json.dumps({
            "assets_ids": token_ids,
            "type": "market",
            "operation": "subscribe",
            "custom_feature_enabled": True,
        })
        await _ws_conn.send(msg)
        _log(f"📡 WS: subscribed to {len(token_ids)} token(s)", "DIAG")
    except Exception as e:
        _log(f"📡 WS send_subscribe error: {e}", "DIAG")


async def _send_unsubscribe(token_ids: list[str]):
    global _ws_conn
    if not _ws_conn:
        return
    try:
        msg = json.dumps({
            "assets_ids": token_ids,
            "operation": "unsubscribe",
        })
        await _ws_conn.send(msg)
    except Exception:
        pass


def _handle_ws_resolution(token_id: str, price: float, event_type: str, raw: dict):
    """
    Called when the WS tells us a market has resolved or a price has gone to ~0/1.
    Updates the shared resolution dict and logs prominently.
    """
    cid = _subscribed_tokens.get(token_id, "")
    if not cid:
        return

    # Find the position to get outcome label
    outcome = ""
    title   = ""
    for (pos_cid, pos_outcome), pos in S.env().open_positions.items():
        if pos_cid == cid:
            outcome = pos_outcome
            title   = pos.title or "?"
            break

    ws_resolved_cids[cid] = {
        "price":      price,
        "outcome":    outcome,
        "ts":         time.time(),
        "event_type": event_type,
    }

    # Update the market cache immediately so the main loop sees it
    cached = S.market_cache.get(cid)
    if cached:
        cached["ts"] = time.time()  # force cache refresh next call
        # If price is near 1.0 for our token, force the yes/no price update
        if price >= 0.97:
            if cached.get("asset_to_price"):
                cached["asset_to_price"][token_id] = price
        elif price <= 0.03:
            if cached.get("asset_to_price"):
                cached["asset_to_price"][token_id] = price

    emoji = "✅" if price >= 0.97 else "❌"
    _log(
        f"📡 WS RESOLUTION {emoji}: [{event_type}] {title[:35]} "
        f"token={token_id[:20]}… price={price:.4f}",
        "INFO"
    )


def _process_message(data: dict):
    """
    Process a single WS message. Called from the async receiver loop.
    """
    event_type = data.get("event_type") or data.get("type") or ""

    # ── market_resolved: definitive resolution event ──────────────────────
    if event_type == "market_resolved":
        # Payload has asset_id and winning_side or outcome_index
        asset_id = data.get("asset_id") or data.get("token_id") or ""
        price    = float(data.get("price") or 0)
        # At resolution, winning token goes to 1.0, losing to 0.0.
        # If price not in payload, determine from outcome index.
        if price == 0:
            # Try to infer from outcome field
            outcome_idx = data.get("outcome_index")
            if outcome_idx is not None:
                price = 1.0  # The resolved outcome is always worth $1
        if asset_id:
            _handle_ws_resolution(asset_id, price, "market_resolved", data)
        return

    # ── price_change / last_trade_price / best_bid_ask: near-resolution ───
    if event_type in ("price_change", "last_trade_price", "best_bid_ask"):
        asset_id = data.get("asset_id") or data.get("token_id") or ""
        # price_change: use "price" field
        # best_bid_ask: use "best_bid" or "best_ask" as proxy
        price = float(data.get("price") or data.get("best_bid") or data.get("best_ask") or 0)

        if not asset_id or price == 0:
            return

        # Only fire if the price is at/near resolution levels
        if price >= 0.97 or price <= 0.03:
            if asset_id in _subscribed_tokens:
                _handle_ws_resolution(asset_id, price, event_type, data)
        else:
            # Update the market cache with the fresh price so the REST
            # polling benefits from it too
            cid = _subscribed_tokens.get(asset_id, "")
            if cid:
                cached = S.market_cache.get(cid)
                if cached and cached.get("asset_to_price") is not None:
                    cached["asset_to_price"][asset_id] = price
                    cached["ts"] = time.time()


async def _ws_run_forever():
    """
    Main async loop: connect, subscribe to all current positions, receive messages,
    reconnect on any error with exponential back-off.

    IMPORTANT: Polymarket's WS server drops connections that don't send a valid
    subscription message quickly. When there are no open positions we have no
    token IDs to subscribe to, so we skip connecting entirely and poll every
    few seconds until a position exists. This prevents the noisy
    "reconnecting in 2s" spam seen when the bot has no open trades.
    """
    global _ws_conn
    delay = RECONNECT_DELAY_BASE

    while True:
        # Wait until we have at least one token to subscribe to.
        # No positions = no point connecting (server will drop us immediately).
        with _ws_lock:
            current_tokens = list(_subscribed_tokens.keys())

        if not current_tokens:
            await asyncio.sleep(5)
            continue

        try:
            _log(f"📡 WS: connecting ({len(current_tokens)} token(s) to subscribe)…", "DIAG")
            async with websockets.connect(
                WS_URI,
                ping_interval=None,   # we handle PING ourselves
                close_timeout=5,
                open_timeout=10,
            ) as ws:
                _ws_conn = ws
                delay = RECONNECT_DELAY_BASE  # reset on successful connect

                # Send subscription immediately on connect — server drops idle conns
                with _ws_lock:
                    current_tokens = list(_subscribed_tokens.keys())
                if current_tokens:
                    await ws.send(json.dumps({
                        "assets_ids":             current_tokens,
                        "type":                   "market",
                        "custom_feature_enabled": True,
                    }))
                    _log(f"📡 WS: subscribed to {len(current_tokens)} token(s)", "INFO")
                else:
                    # Tokens disappeared between the check and connect — just close
                    _ws_conn = None
                    continue

                # Heartbeat coroutine — must PING every <10s or server drops conn
                async def _heartbeat():
                    while True:
                        await asyncio.sleep(PING_INTERVAL)
                        try:
                            await ws.send("PING")
                        except Exception:
                            break

                hb_task = asyncio.create_task(_heartbeat())

                try:
                    async for raw_msg in ws:
                        if raw_msg == "PONG":
                            continue
                        try:
                            data = json.loads(raw_msg)
                            if isinstance(data, list):
                                for item in data:
                                    _process_message(item)
                            elif isinstance(data, dict):
                                _process_message(data)
                        except Exception as parse_err:
                            _log(f"📡 WS parse error: {parse_err}", "DIAG")

                        # If all positions closed while we were connected, disconnect cleanly
                        with _ws_lock:
                            if not _subscribed_tokens:
                                _log("📡 WS: no positions left — disconnecting cleanly", "DIAG")
                                break
                finally:
                    hb_task.cancel()
                    _ws_conn = None

        except websockets.exceptions.ConnectionClosed as e:
            _ws_conn = None
            _log(f"📡 WS connection closed: {e} — reconnecting in {delay}s", "DIAG")
        except Exception as e:
            _ws_conn = None
            _log(f"📡 WS error: {e} — reconnecting in {delay}s", "DIAG")
            await asyncio.sleep(delay)
            delay = min(delay * 2, RECONNECT_DELAY_MAX)


def _thread_entry():
    """Entry point for the daemon thread that owns the asyncio event loop."""
    global _ws_loop
    loop = asyncio.new_event_loop()
    _ws_loop = loop
    asyncio.set_event_loop(loop)
    loop.run_until_complete(_ws_run_forever())


def start():
    """
    Start the WebSocket resolution monitor as a background daemon thread.
    Called once from titan_engine.start() after load_state() and C.reload().
    """
    t = threading.Thread(target=_thread_entry, daemon=True, name="titan-ws-monitor")
    t.start()
    _log("📡 WS resolution monitor started", "INFO")
    return t


# ─────────────────────────────────────────────────────────────────────────────
#  Helpers for engine: sync open positions on startup / position changes
# ─────────────────────────────────────────────────────────────────────────────

def sync_open_positions():
    """
    Subscribe to WS tokens for all currently open positions.
    Call this once on startup after load_state(), and again whenever a position opens.
    """
    for (cid, outcome), pos in S.env().open_positions.items():
        asset = pos.asset
        tokens = []
        if asset:
            tokens.append(asset)
        # Also try to get the sibling token from the market cache
        mkt = S.market_cache.get(cid)
        if mkt is not None:
            for tid in mkt.asset_to_price.keys():
                if tid not in tokens:
                    tokens.append(tid)
        if tokens:
            subscribe_position(cid, tokens)
