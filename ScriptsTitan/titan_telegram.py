import requests
import time
from datetime import datetime

# ═══════════════════════════════════════════════════════════════════════════════
#  TITAN TELEGRAM NOTIFIER — STANDALONE
# ═══════════════════════════════════════════════════════════════════════════════

TOKEN   = "8550520117:AAGzm33Eo1aJt96awbCjR76Vq698WstpefM"
CHAT_ID = "7745229461"

class TelegramNotifier:
    """
    Handles outbound notifications for trade events and errors.
    """

    def __init__(self, token=TOKEN, chat_id=CHAT_ID):
        self._token   = token
        self._chat_id = chat_id
        self._api     = f"https://api.telegram.org/bot{self._token}/sendMessage"

    def _escape(self, text: str) -> str:
        """Escape MarkdownV2 special characters."""
        if not text: return ""
        # Characters that MUST be escaped in MarkdownV2
        # See: https://core.telegram.org/bots/api#markdownv2-style
        parse_chars = r"_*[]()~`>#+-=|{}.!"
        for char in parse_chars:
            text = text.replace(char, f"\\{char}")
        return text

    def _send(self, raw_text: str, is_markdown: bool = True):
        """Send message, optionally as MarkdownV2."""
        try:
            payload = {
                "chat_id": self._chat_id,
                "text": raw_text,
            }
            if is_markdown:
                payload["parse_mode"] = "MarkdownV2"
            
            resp = requests.post(self._api, json=payload, timeout=10)
            if resp.status_code != 200:
                print(f"Telegram API Error ({resp.status_code}): {resp.text}")
                # Fallback to plain text if MarkdownV2 fails (best effort)
                if is_markdown:
                    payload.pop("parse_mode")
                    requests.post(self._api, json=payload, timeout=10)
            return resp.status_code == 200
        except Exception as e:
            print(f"Telegram connection error: {e}")
            return False

    def notify_buy(self, pos: dict, w_idx: int = 0, s_name: str = ""):
        """Send formatted buy alert."""
        title   = self._escape(pos.get('title', 'Unknown Market')[:80])
        side    = self._escape(pos.get('outcome', '?'))
        entry   = self._escape(f"{pos.get('entry_price', 0):.4f}")
        bet     = self._escape(f"{pos.get('bet', 0):.2f}")
        tier    = self._escape(pos.get('tier', 'SINGLE'))
        score   = self._escape(f"{pos.get('score', 0):.0f}")
        w_idx_e = self._escape(str(w_idx))
        s_name_e = self._escape(s_name)
        
        # Use raw f-strings to allow backslashes for Telegram MarkdownV2 escaping
        msg = (
            fr"🚀 *TITAN BUY ALERT \— Wallet {w_idx_e}*" + "\n"
            fr"_{s_name_e}_" + "\n"
            f"━━━━━━━━━━━━━━━\n"
            f"📍 *Market:* {title}\n"
            f"📊 *Outcome:* {side}\n"
            f"💰 *Entry:* ${entry}\n"
            f"💵 *Size:* ${bet}\n"
            fr"🔥 *Tier:* {tier} \({score} pts\)"
        )
        return self._send(msg)

    def notify_sell(self, pos: dict, pnl_usdc: float, pnl_pct: float, w_idx: int = 0, s_name: str = ""):
        """Send formatted sell/exit alert."""
        title   = self._escape(pos.get('title', 'Unknown Market')[:80])
        side    = self._escape(pos.get('outcome', '?'))
        emoji   = "✅" if pnl_usdc >= 0 else "❌"
        verdict = "PROFIT" if pnl_usdc >= 0 else "LOSS"
        reason  = self._escape(pos.get('reason', 'Target reached'))
        pnl_s   = self._escape(f"{pnl_usdc:+.4f}")
        pct_s   = self._escape(f"{pnl_pct*100:+.1f}")
        w_idx_e = self._escape(str(w_idx))
        s_name_e = self._escape(s_name)

        msg = (
            fr"{emoji} *TITAN EXIT \— Wallet {w_idx_e}*" + "\n"
            fr"_{s_name_e}_" + "\n"
            f"━━━━━━━━━━━━━━━\n"
            f"📍 *Market:* {title}\n"
            f"📊 *Outcome:* {side}\n"
            fr"📈 *PnL:* ${pnl_s} \({pct_s}%\)\n"
            f"🔎 *Reason:* {reason}"
        )
        return self._send(msg)


    def notify_error(self, error_msg: str):
        """Send system error alert."""
        safe_msg = self._escape(error_msg[:200])
        msg = (
            f"⚠️ *TITAN SYSTEM ERROR*\n"
            f"━━━━━━━━━━━━━━━\n"
            f"🚨 {safe_msg}"
        )
        return self._send(msg)

    def notify_boot(self):
        """Heartbeat on startup."""
        ts = self._escape(datetime.now().strftime("%H:%M:%S"))
        # Escaping literal dots in "Whales..."
        msg = (
            f"⚡ *TITAN ONLINE*\n"
            f"Mode: Paper Trading\n"
            f"Time: {ts}\n"
            fr"Status: Monitoring Whales\.\.\."
        )
        return self._send(msg)

    def send_photo(self, photo_path_or_bytes, caption=""):
        """Send a photo with optional caption."""
        api_url = f"https://api.telegram.org/bot{self._token}/sendPhoto"
        try:
            payload = {"chat_id": self._chat_id}
            if caption:
                payload["caption"] = self._escape(caption)
                payload["parse_mode"] = "MarkdownV2"
            
            if isinstance(photo_path_or_bytes, str):
                with open(photo_path_or_bytes, 'rb') as f:
                    files = {'photo': f}
                    resp = requests.post(api_url, data=payload, files=files, timeout=15)
            else:
                files = {'photo': photo_path_or_bytes}
                resp = requests.post(api_url, data=payload, files=files, timeout=15)
                
            if resp.status_code != 200:
                print(f"Telegram sendPhoto Error ({resp.status_code}): {resp.text}")
            return resp.status_code == 200
        except Exception as e:
            print(f"Telegram sendPhoto exception: {e}")
            return False

    def start_polling(self, on_message_callback):
        """Start polling for messages in a background thread."""
        def poll():
            offset = 0
            api_url = f"https://api.telegram.org/bot{self._token}/getUpdates"
            while True:
                try:
                    resp = requests.get(api_url, params={"offset": offset, "timeout": 30}, timeout=35)
                    if resp.status_code == 200:
                        data = resp.json()
                        for result in data.get("result", []):
                            offset = result["update_id"] + 1
                            if "message" in result:
                                msg = result["message"]
                                text = msg.get("text", "")
                                # We only process messages from the authorized user
                                if str(msg.get("chat", {}).get("id")) == str(self._chat_id):
                                    on_message_callback(text)
                except Exception as e:
                    print(f"Telegram polling error: {e}")
                    time.sleep(5)
                time.sleep(1)
                
        import threading
        t = threading.Thread(target=poll, daemon=True)
        t.start()

if __name__ == "__main__":
    # Self-test
    tn = TelegramNotifier()
    if tn.notify_boot():
        print("✅ Telegram notification sent successfully.")
    else:
        print("❌ Telegram notification failed. Check token/chat_id.")
