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
    # ws_ping_interval=None: the admin live feed sends its own heartbeat —
    # some gateways/tunnels drop ws control frames, and a server ping whose
    # pong never returns would get the connection killed every ~40s.
    # permessage-deflate off for the same class of middlebox (mangled
    # compressed frames); admin frames are ~1KB so it buys nothing anyway.
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning",
                server_header=False,
                ws_ping_interval=None, ws_per_message_deflate=False)


if __name__ == "__main__":
    main()
