"""python -m devin_proxy [--host 127.0.0.1] [--port 8317]"""
import argparse
import os

import uvicorn

from .app import create_app


def main():
    p = argparse.ArgumentParser(prog="devin-proxy")
    p.add_argument("--host", default=os.environ.get("DEVIN_PROXY_HOST", "127.0.0.1"))
    p.add_argument("--port", type=int, default=int(os.environ.get("DEVIN_PROXY_PORT", "8317")))
    p.add_argument("--api-key", default=os.environ.get("DEVIN_PROXY_KEY"),
                   help="require clients to send Authorization: Bearer <key>")
    args = p.parse_args()

    app = create_app(api_key=args.api_key)
    s = app.state.pool.summary()
    print(f"devin-proxy   accounts: {s['total']} ({s['ready']} ready)")
    print(f"OpenAI base:  http://{args.host}:{args.port}/v1")
    print(f"admin UI:     http://{args.host}:{args.port}/admin")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
