# devin-proxy

OpenAI-compatible relay over a Devin (Cognition) Pro account.

Forwards `POST /v1/chat/completions` to the same Connect-RPC endpoint the
Devin CLI uses (`exa.api_server_pb.ApiServerService/GetChatMessage` on
`server.codeium.com`), so any OpenAI-compatible client can use the models
included with your Devin subscription.

Built on libraries only: `protobuf` (dynamic descriptors, no codegen),
`httpx` (streaming), `fastapi` + `uvicorn` (server).

## Install

```bash
pip install -r requirements.txt
```

## Credentials

Resolved automatically, in order:

1. `DEVIN_SESSION_TOKEN` / `WINDSURF_API_KEY` env var
2. Devin CLI credentials (`%APPDATA%\devin\credentials.toml`,
   `~/.local/share/devin/credentials.toml`)
3. Devin Desktop state DB (if the editor is signed in)

If the Devin CLI or Devin Desktop is already signed in, nothing else is
needed — the proxy reuses the existing session token.

## Run

```bash
python -m devin_proxy --host 127.0.0.1 --port 8317
```

Point any OpenAI client at `http://127.0.0.1:8317/v1`.

Optional shared-secret for clients (`Authorization: Bearer <key>`):

```bash
python -m devin_proxy --api-key my-secret
```

`--api-key` also locks the admin console (enter the key to log in).

## Admin console

Open `http://127.0.0.1:8317/admin` — a persistent dashboard backed by
SQLite (`%APPDATA%\devin-proxy\devin-proxy.db`, override with
`DEVIN_PROXY_DB`):

- **仪表盘** — request/token totals, success rate, avg latency & TTFT,
  24 h chart, per-model stats
- **请求日志** — every request logged (model, tokens, latency, status,
  caller key, messages); filter by model/status/text, paginate, inspect
  detail, or purge. Retention capped by `DEVIN_PROXY_MAX_ROWS`
  (default 50000)
- **模型** — all callable model uids/aliases + per-model usage stats
- **Playground** — test any model streaming or not, straight from the UI
  (goes through the admin channel, still logged)
- **API Keys** — mint `sk-dp-…` keys for callers (SHA-256 hashed in the
  DB, shown once); enable/disable/delete. Once any key exists, `/v1`
  requires `Authorization: Bearer`
- **状态** — credential source, upstream, DB path/size, uptime,
  one-click upstream connectivity check

## Endpoints

- `POST /v1/chat/completions` — streaming + non-streaming; system prompts,
  multi-turn history, `tools`/`tool_choice`, `temperature`/`top_p`/
  `max_tokens`, `stream_options.include_usage`; thinking models emit
  `reasoning_content` (DeepSeek-style)
- `GET /v1/models` — known model uids + aliases
- `GET /healthz`
- `/admin/*` — console + JSON API

Model: pass a variant uid (`claude-sonnet-5-medium`, `gpt-5-6-sol-low`,
`swe-2-high`, …) or an alias (`claude`, `sonnet`, `opus`, `gemini`, `gpt`,
`swe`).

## Layout

- `devin_proxy/proto.py` — protobuf schema (field numbers of the wire format)
- `devin_proxy/upstream.py` — Connect-RPC client (framing, GetUserJwt, streaming)
- `devin_proxy/creds.py` — credential resolution
- `devin_proxy/app.py` — FastAPI OpenAI shim + request recording
- `devin_proxy/store.py` — SQLite persistence (requests, API keys)
- `devin_proxy/admin.py` — admin API router
- `devin_proxy/web/admin.html` — embedded admin SPA
- `devin_proxy/__main__.py` — launcher

Unofficial client of Cognition's backend — use at your own discretion w.r.t.
the Devin terms of service.
