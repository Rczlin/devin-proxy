"""Manual OAuth flow debugger.

  python oauth_debug.py start           -> prints login URL, saves flow state
  python oauth_debug.py complete <code> -> exchanges code, then tests the token
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import httpx

from devin_proxy import accounts, upstream, proto

FLOW_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         ".oauth_flow.json")


def start():
    f = accounts.start_flow()
    flow = accounts._flows[f["id"]]
    with open(FLOW_FILE, "w") as fp:
        json.dump({"id": f["id"], "state": f["state"],
                   "verifier": flow["verifier"],
                   "webapp": flow["webapp"],
                   "created": time.time()}, fp)
    print("OPEN THIS URL:\n")
    print(f["url"])
    print(f"\nflow id: {f['id']}  (saved to {FLOW_FILE})")


def _try(url, body, client, headers=None):
    hdrs = {"Content-Type": "application/json",
            "Accept": "application/json"}
    hdrs.update(headers or {})
    print(f"\n=== POST {url}")
    print("    body:", {k: (v[:20] + "..." if isinstance(v, str) and len(v) > 20 else v)
                        for k, v in body.items()})
    try:
        r = client.post(url, json=body, headers=hdrs, timeout=30)
        print("    status:", r.status_code)
        print("    resp:", r.text[:600])
        return r
    except Exception as e:
        print("    EXC:", repr(e))
        return None


TOKEN_KEYS = ("token", "api_key", "apiKey", "session_token", "sessionToken",
              "access_token", "accessToken")


def complete(code):
    flow = json.load(open(FLOW_FILE))
    verifier, webapp, state = flow["verifier"], flow["webapp"], flow["state"]
    client = httpx.Client(timeout=httpx.Timeout(60, connect=15))

    variants = [
        {"code": code, "code_verifier": verifier},
        {"code": code, "code_verifier": verifier, "redirect_uri": ""},
        {"code": code, "code_verifier": verifier, "state": state},
        {"code": code, "codeVerifier": verifier},
        {"code": code, "code_verifier": verifier, "redirect_uri": "",
         "state": state},
    ]
    urls = [
        "https://api.devin.ai/auth/cli/token",
        "https://api.devin.ai/exa.seat_management_pb.SeatManagementService/"
        "ExchangePKCEAuthorizationCode",
        "https://server.codeium.com/exa.seat_management_pb."
        "SeatManagementService/ExchangePKCEAuthorizationCode",
    ]
    token, cred = None, {}
    for url in urls:
        hdrs = {"Connect-Protocol-Version": "1"} if "exa." in url else None
        for body in variants:
            r = _try(url, body, client, headers=hdrs)
            if r is None or r.status_code != 200:
                continue
            try:
                d = r.json()
            except Exception:
                print("    (not json)")
                continue
            token = next((d[k] for k in TOKEN_KEYS if d.get(k)), None)
            cred = d
            if token:
                break
        if token:
            break
    if not token:
        print("\n!! no token from any endpoint")
        sys.exit(1)
    print("\n== TOKEN OK:", token[:12] + "..." + token[-6:],
          " len:", len(token))
    print("   keys in response:", sorted(cred.keys()))

    api_server = (cred.get("api_server_url") or cred.get("apiServerUrl")
                  or accounts.DEFAULT_API_SERVER)
    print("\n== GetUserStatus @", api_server)
    info = accounts.fetch_identity(client, api_server, token)
    print("   identity:", info)

    print("\n== GetUserJwt @", api_server)
    try:
        jwt = upstream.get_user_jwt(client, api_server, token,
                                    force_refresh=True)
        print("   jwt ok, len:", len(jwt))
    except Exception as e:
        print("   JWT FAILED:", repr(e))
        sys.exit(1)

    print("\n== GetChatMessage (1-token prompt)")
    req = upstream.build_request(
        token, jwt, "claude-sonnet-5-medium", None,
        [upstream.make_prompt("user", "say hi in one word")])
    try:
        for ev in upstream.stream_chat(client, api_server, token, req,
                                       timeout=60):
            if isinstance(ev, dict):
                print("   ERR:", ev)
            else:
                print("   msg:",
                      {"mid": ev.message_id, "text": ev.delta_text[:80],
                       "stop": ev.stop_reason})
    except Exception as e:
        print("   CHAT FAILED:", repr(e))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
    elif sys.argv[1] == "start":
        start()
    elif sys.argv[1] == "complete":
        complete(sys.argv[2])
    else:
        print(__doc__)
