"""
TITAN PROBE — Live Polymarket Data Inspector.
A lightweight, standalone tool to verify live data flow 'in front of your eyes'.
Useful for confirming that the API is hitting and whales are active.
"""

import time
import os
import requests
import sys
from datetime import datetime
from titan_config import DATA_API, HEADERS

# ── ANSI COLORS ──────────────────────────────────────────────────────────────
GREEN = "\033[38;5;82m"
BLUE  = "\033[38;5;75m"
CYAN  = "\033[38;5;86m"
GOLD  = "\033[38;5;220m"
RED   = "\033[38;5;196m"
GRAY  = "\033[38;5;240m"
RESET = "\033[0m"
BOLD  = "\033[1m"

def clear_screen():
    os.system('cls' if os.name == 'nt' else 'clear')

def fetch_top_trades():
    """Fetch recent activity directly from Polymarket Data API."""
    url = f"{DATA_API}/activity?limit=15"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=10)
        return resp.json() if resp.status_code == 200 else []
    except:
        return []

def fetch_top_markets():
    """Fetch high-volume markets."""
    url = f"{DATA_API}/markets?limit=10&active=true&order=volume24hr&dir=desc"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=10)
        return resp.json() if resp.status_code == 200 else []
    except:
        return []

def format_cash(val):
    try:
        f = float(val)
        if f >= 1000: return f"${f/1000:.1f}k"
        return f"${f:.0f}"
    except:
        return "$0"

def probe_loop():
    cycle = 0
    try:
        while True:
            cycle += 1
            trades  = fetch_top_trades()
            markets = fetch_top_markets()
            
            clear_screen()
            now = datetime.now().strftime("%H:%M:%S")
            
            print(f"{CYAN}{BOLD}TITAN LIVE PROBE{RESET} {GRAY}| {now} | Cycle: {cycle}{RESET}")
            print(f"{GRAY}─────────────────────────────────────────────────────────────────────────────{RESET}")
            
            # ── TOP MARKETS ──────────────────
            print(f"{BLUE}{BOLD}📊 HOT MARKETS (24h Volume){RESET}")
            for m in markets[:5]:
                title  = m.get('question', 'Unknown')[:60]
                vol    = format_cash(m.get('volume24hr', 0))
                liq    = format_cash(m.get('liquidity', 0))
                print(f"  {BOLD}{vol:>6}{RESET} | {title:<60} {GRAY}(Liq: {liq}){RESET}")
            
            print(f"\n{GOLD}{BOLD}🐳 RECENT LIVE ACTIVITY{RESET}")
            print(f"  {BOLD}{'TIME':<8} | {'SIZE':<8} | {'SIDE':<4} | {'WALLET':<12} | {'MARKET'}{RESET}")
            print(f"  {GRAY}─────────┼──────────┼──────┼──────────────┼──────────────────────────────{RESET}")
            
            for t in trades[:12]:
                # Extract fields with fallback
                ts_str  = t.get('timestamp', '')
                if ts_str:
                    try:
                        # "2024-05-20T12:00:00Z" -> "12:00:00"
                        dt = datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
                        ts_display = dt.strftime("%H:%M:%S")
                    except:
                        ts_display = "—"
                else:
                    ts_display = "—"
                
                amount  = format_cash(t.get('amount', 0))
                side    = t.get('side', 'BUY')
                color   = GREEN if side == 'BUY' else RED
                wallet  = t.get('proxyWallet', t.get('address', 'unknown'))[:12]
                title   = t.get('title', 'Unknown Market')[:45]
                p_name  = t.get('name', '')
                if p_name and not p_name.endswith('…'):
                    wallet_display = p_name[:12]
                else:
                    wallet_display = wallet
                
                print(f"  {ts_display:<8} | {BOLD}{amount:>8}{RESET} | {color}{side:<4}{RESET} | {CYAN}{wallet_display:<12}{RESET} | {title}")

            print(f"\n{GRAY}Press Ctrl+C to exit...{RESET}", end="", flush=True)
            time.sleep(3)

    except KeyboardInterrupt:
        print(f"\n{CYAN}Probe terminated.{RESET}")

if __name__ == "__main__":
    # Ensure ANSI colors work on Windows
    if os.name == 'nt':
        os.system('') 
    probe_loop()