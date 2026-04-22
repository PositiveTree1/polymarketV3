from pycloudflared import try_cloudflare
import sys

try:
    tunnel = try_cloudflare(port=8080)
    print(f"Type: {type(tunnel)}")
    print(f"Dir: {dir(tunnel)}")
    # Most likely it's .url or .tunnel_url or a dict handle
except Exception as e:
    print(f"Error: {e}")
