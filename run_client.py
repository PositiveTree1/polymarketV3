import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "ScriptsTitan"))


def main() -> None:
    parser = argparse.ArgumentParser(prog="run_titan")
    parser.add_argument("--mode", choices=["ui", "server", "client"], default="ui")
    parser.add_argument("--port", type=int, default=8765, help="Server port (--mode server)")
    parser.add_argument("--host", default="127.0.0.1", help="Server host (--mode server)")
    parser.add_argument("--token", default=None, help="Bearer token for auth (--mode server/client)")
    parser.add_argument("--url", default="http://127.0.0.1:8765", help="Server URL (--mode client)")
    args = parser.parse_args()

    from titan_client import TitanClient
    api = TitanClient(base_url=args.url, token=args.token)
    from titan_ui import run_ui
    run_ui(api)  # run_ui calls api.start() after wiring subscriptions internally


if __name__ == "__main__":
    main()
