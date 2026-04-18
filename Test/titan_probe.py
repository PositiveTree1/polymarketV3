import sys
import time
import requests
import json
from datetime import datetime

DATA_API  = "https://data-api.polymarket.com/v1"
GAMMA_API = "https://gamma-api.polymarket.com"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
    "Origin": "https://polymarket.com",
    "Referer": "https://polymarket.com/",
}

def safe_get(url, params=None):
    try:
        r = requests.get(url, params=params, headers=HEADERS, timeout=10)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        print(f"Error fetching {url}: {e}")
    return None

def diagnostic_mode():
    print("\n--- Diagnostic Mode: Fetching Top Markets & Traders ---")
    data = safe_get(f"{GAMMA_API}/markets", {"limit": 100, "active": "true"})
    
    if not data:
        print("Failed to fetch markets.")
        return
        
    markets = sorted(data, key=lambda x: float(x.get("volume") or 0), reverse=True)[:5]
    print(f"Top {len(markets)} Markets by volume:")
    
    candidates = set()
    for m in markets:
        vol = float(m.get('volume') or 0)
        print(f" - {m.get('question')} | Vol: ${vol:,.0f} | CID: {m.get('conditionId')[:10]}...")
        
        # Look at biggest holders by fetching large recent trades
        trades = safe_get(f"{DATA_API}/trades", {
            "conditionId": m.get("conditionId"), 
            "limit": 50, 
            "filterType": "CASH", 
            "side": "BUY", 
            "filterAmount": 500
        })
        
        if trades and isinstance(trades, list):
            for t in trades:
                w = (t.get("proxyWallet") or "").lower()
                if w and len(w) == 42:
                    candidates.add(w)
        time.sleep(0.5)
        
    print(f"\nFound {len(candidates)} candidate wallets from these trades.")
    
    for w in list(candidates)[:15]:
        print(f"\nEvaluating wallet: {w}")
        positions = safe_get(f"{DATA_API}/positions", {"user": w, "limit": 100})
        if not positions or not isinstance(positions, list): 
            print(" | No position data found.")
            continue
            
        pnl = sum(float(p.get("cashPnl") or 0) for p in positions)
        print(f" | Total PnL: ${pnl:+.2f}")
        
        if pnl > 0:
            print(f" | Wallet is mathematically profitable ($+), fetching recent trades...")
            t_data = safe_get(f"{DATA_API}/trades", {"user": w, "limit": 5})
            if t_data and isinstance(t_data, list):
                for td in t_data:
                    ts = datetime.fromtimestamp(float(td.get('timestamp', 0))).strftime("%H:%M:%S")
                    buy_or_sell = td.get('side', '?')
                    usd_val = float(td.get('usdcSize') or 0)
                    if usd_val == 0:
                        usd_val = float(td.get('size') or 0) * float(td.get('price') or 0)
                    title = td.get('title') or td.get('slug') or 'Unknown Market'
                    print(f"  [{ts}] {buy_or_sell} {td.get('outcome')} | ${usd_val:.2f} | {title[:40]}")
        else:
            print(" | Wallet PnL is negative, skipping trades.")
        time.sleep(0.5)

def live_market_mode():
    import os
    print("\n--- Live Market Price Check (Real-time) ---")
    data = safe_get(f"{GAMMA_API}/markets", {"limit": 100, "active": "true"})
    if not data:
        print("Failed to fetch markets.")
        return
        
    # Get highest volume market to monitor
    m_base = sorted(data, key=lambda x: float(x.get("volume") or 0), reverse=True)[0]
    cid = m_base.get("conditionId")
    
    print(f"Monitoring: {m_base.get('question')}")
    print("Press Ctrl+C to stop.")
    time.sleep(1)

    try:
        while True:
            # Refresh data for this specific market
            m_list = safe_get(f"{GAMMA_API}/markets", {"condition_id": cid})
            if not m_list:
                time.sleep(2)
                continue
                
            m = m_list[0] if isinstance(m_list, list) else m_list
            
            # Clear screen (OS dependent)
            os.system('cls' if os.name == 'nt' else 'clear')
            
            print("====================================")
            print("   TITAN LIVE MONITORING TOOL       ")
            print("====================================")
            print(f"Market: {m.get('question')}")
            print(f"Volume: ${float(m.get('volume') or 0):,.2f} | Liquidity: ${float(m.get('liquidity') or 0):,.2f}")
            print(f"Condition ID: {cid}")
            print("-" * 36)
            
            raw = m.get("outcomePrices") or "[]"
            prices = json.loads(raw) if isinstance(raw, str) else raw
            
            if len(prices) >= 2:
                yp = float(prices[0])
                np_ = float(prices[1])
                now = datetime.now().strftime("%H:%M:%S")
                print(f"[{now}] YES: ${yp:.4f}  |  NO: ${np_:.4f}")
                
                # Visual Bar (simple representation)
                bar_len = 30
                yes_chars = int(yp * bar_len)
                no_chars = bar_len - yes_chars
                print(f"Progress: [{'#' * yes_chars}{'-' * no_chars}]")
            else:
                print("Price data unavailable in this cycle.")
                
            print("-" * 36)
            print("Ctrl+C to return to main menu.")
            
            time.sleep(2)
    except KeyboardInterrupt:
        print("\nReturning to main menu...")

def main():
    print("====================================")
    print("   TITAN PROBE (Diagnostic Tool)    ")
    print("====================================")
    print("1) Diagnostic Mode (Top Markets -> Holders -> P&L -> Recent Trades)")
    print("2) Live Market Price Check (Trending Market Stats & Pricing)")
    choice = input("Enter choice (1/2): ").strip()
    
    if choice == '1':
        diagnostic_mode()
    elif choice == '2':
        live_market_mode()
    else:
        print("Invalid choice. Exiting.")

if __name__ == '__main__':
    main()
