import json
import os

def generate_configs():
    base_file = os.path.join(os.path.dirname(__file__), "titan_config.json")
    
    with open(base_file, "r", encoding="utf-8") as f:
        base_cfg = json.load(f)

    def _set(cfg, category, key, val):
        if category in cfg and key in cfg[category]:
            cfg[category][key]["value"] = val

    # Helper to save
    def save_cfg(idx, cfg):
        path = os.path.join(os.path.dirname(__file__), f"titan_config_{idx}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=4)
        print(f"Created titan_config_{idx}.json")

    # W0 is default (we still create it from base if it doesnt exist, but UI falls back anyway)
    import copy
    
    # ── Wallet 1: Ultra-Aggressive HFT Tracker ──
    cfg1 = copy.deepcopy(base_cfg)
    _set(cfg1, "hft_polling", "HFT_MIN_CASH_PER_TRADE", 2)
    _set(cfg1, "trade_sourcing", "MIN_TRADE_CASH", 5)
    _set(cfg1, "stop_loss_profit_target", "STOP_LOSS_PCT", -0.05)
    _set(cfg1, "stop_loss_profit_target", "PROFIT_TARGET_PCT", 0.05)
    _set(cfg1, "bankroll_and_sizing", "MIN_BET", 0.5)
    save_cfg(1, cfg1)

    # ── Wallet 2: The Sniper (High Conviction only) ──
    cfg2 = copy.deepcopy(base_cfg)
    _set(cfg2, "signal_quality", "MIN_SCORE", 75)
    _set(cfg2, "signal_quality", "ALERT_SCORE", 85)
    _set(cfg2, "signal_quality", "STRONG_SCORE", 95)
    _set(cfg2, "market_filters", "MIN_LIQUIDITY", 50000)
    _set(cfg2, "market_filters", "MIN_VOLUME", 100000)
    save_cfg(2, cfg2)

    # ── Wallet 3: The Conservative (Low Kelly, Wide Stop) ──
    cfg3 = copy.deepcopy(base_cfg)
    _set(cfg3, "bankroll_and_sizing", "KELLY_FRACTION", 0.02)
    _set(cfg3, "bankroll_and_sizing", "MAX_BET_PCT", 0.05)
    _set(cfg3, "stop_loss_profit_target", "STOP_LOSS_PCT", -0.30)
    save_cfg(3, cfg3)

    # ── Wallet 4: Big Whale Mirror (Only follows huge prints) ──
    cfg4 = copy.deepcopy(base_cfg)
    _set(cfg4, "elite_polling", "ELITE_TRADE_MIN_FRACTION", 0.20)
    _set(cfg4, "elite_polling", "ELITE_POLL_MIN_CASH", 2000)
    _set(cfg4, "trade_sourcing", "MIN_TRADE_CASH", 500)
    save_cfg(4, cfg4)

    # ── Wallet 5: The Scalper (Tiny targets) ──
    cfg5 = copy.deepcopy(base_cfg)
    _set(cfg5, "stop_loss_profit_target", "PROFIT_TARGET_PCT", 0.03)
    _set(cfg5, "stop_loss_profit_target", "STOP_LOSS_PCT", -0.03)
    _set(cfg5, "bankroll_and_sizing", "MIN_BET", 10.0)
    _set(cfg5, "bankroll_and_sizing", "MAX_BET_PCT", 0.50)
    save_cfg(5, cfg5)

    # ── Wallet 6: Long Term Holder (Diamond Hands) ──
    cfg6 = copy.deepcopy(base_cfg)
    _set(cfg6, "stop_loss_profit_target", "STOP_LOSS_PCT", -0.90)
    _set(cfg6, "market_filters", "MIN_HOURS_LEFT", 120.0) # Only takes bets ending > 5 days from now
    save_cfg(6, cfg6)

    # ── Wallet 7: Degen (Max Kelly) ──
    cfg7 = copy.deepcopy(base_cfg)
    _set(cfg7, "bankroll_and_sizing", "KELLY_FRACTION", 0.35)
    _set(cfg7, "bankroll_and_sizing", "MAX_BET_PCT", 0.40)
    _set(cfg7, "signal_quality", "MIN_SCORE", 30)
    save_cfg(7, cfg7)

    # ── Wallet 8: The Noise Trader (Matches EVERYTHING small) ──
    cfg8 = copy.deepcopy(base_cfg)
    _set(cfg8, "trade_sourcing", "MIN_TRADE_CASH", 1)
    _set(cfg8, "signal_quality", "MIN_SCORE", 15)
    _set(cfg8, "bankroll_and_sizing", "MAX_BET_ABS", 1.0)
    save_cfg(8, cfg8)

    # ── Wallet 9: Strict Elite Only ──
    cfg9 = copy.deepcopy(base_cfg)
    _set(cfg9, "elite_polling", "ELITE_POLL_MIN_CASH", 500)
    # Require multiple checks
    _set(cfg9, "market_filters", "MAX_ENTRY_SLIPPAGE", 0.005)
    _set(cfg9, "market_filters", "MAX_DRIFT", 0.02)
    save_cfg(9, cfg9)

if __name__ == "__main__":
    generate_configs()
