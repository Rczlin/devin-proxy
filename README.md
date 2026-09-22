# devin-proxy

OpenAI-compatible relay over Devin (Cognition) accounts.

Forwards `POST /v1/chat/completions` and `POST /v1/responses` to the same
Connect-RPC endpoint the Devin CLI uses
(`exa.api_server_pb.ApiServerService/GetChatMessage` on
`server.codeium.com`), so any OpenAI-compatible client can use the models
included with your Devin subscription.

Built on libraries only: `protobuf` (dynamic descriptors, no codegen),
`httpx` (streaming), `fastapi` + `uvicorn` (server), SQLite (state).

## Install

```bash
pip install -r requirements.txt
```

## Run

```bash
python -m devin_proxy --host 127.0.0.1 --port 8317
```

The proxy starts even with zero accounts configured — open the admin
console and add accounts there. On startup it also auto-imports any
credentials found on the machine (see below).

Point any OpenAI client at `http://127.0.0.1:8317/v1`.

The proxy is always locked. Set the master key explicitly:

```bash
python -m devin_proxy --api-key my-secret
```

If `--api-key`/`DEVIN_PROXY_KEY` is not set, a random master key is generated
on first run, persisted in the DB, and printed to the console. The master key
unlocks the admin console (`/admin` → sign in → cookie session) and works as a
caller key on `/v1`. Without a valid credential, `/v1` returns 401 and `/admin`
shows only a generic sign-in page — no console code, model list, or service
details are exposed (`/docs`, `/openapi.json` and `Server` header are off;
`/healthz` reports only `{"ok": true}`).

## Accounts (multi-account pool)

Upstream Devin accounts live in the SQLite DB and are managed entirely
from the admin console (**账号池** page):

- **OAuth login** — "生成登录链接" starts the same PKCE flow as
  `devin auth login --force-manual-token-flow`: open the link, sign in at
  app.devin.ai, paste the shown code back — the proxy exchanges it for a
  session token (`/auth/cli/token`, with `ExchangePKCEAuthorizationCode`
  as fallback) and fetches the account identity/plan. Works headless/SSH.
- **手动添加** — paste a `devin-session-token$…` / api key directly.
- **从本机导入** — scans env vars, token files, Devin CLI
  `credentials.toml` and Devin Desktop state DBs; imports every distinct
  credential as its own account.
- Per-account: enable/disable, delete, connectivity test, identity/plan
  refresh, last error, cooldown countdown, in-flight count, plus a
  **max-concurrency cap** and a **model allowlist** (限制 button) — the
  scheduler skips capped or non-serving accounts and fails over to the
  next eligible one.

### Scheduling

- **Session pinning** — a request's session (explicit `session_id` /
  `x-session-id` header / `user` / `prompt_cache_key` / conversation
  fingerprint / `previous_response_id` chain) sticks to one account.
- **Least-busy pick** — unpinned requests go to the ready account with the
  fewest in-flight requests, then fewest consecutive failures.
- **Failover** — auth/quota/5xx/network errors cool an account down
  (30 s base, exponential to 15 min) and the request retries on the next
  account; the session re-pins to whichever account succeeded.

Credentials are also resolved automatically at startup from:

1. `DEVIN_SESSION_TOKEN` / `WINDSURF_API_KEY` env var (or
   `DEVIN_SESSION_TOKEN_FILE`)
2. Devin CLI credentials (`%APPDATA%\devin\credentials.toml`,
   `~/.local/share/devin/credentials.toml`)
3. Devin Desktop state DB (if the editor is signed in)

An account deleted in the console is tombstoned and will not be
re-imported.

## Docker / GHCR

`.github/workflows/docker.yml` builds and pushes
`ghcr.io/<owner>/devin-proxy` on every push to the default branch
(`latest` tag, `v*` tags, and commit sha) — no secrets to configure,
it uses `GITHUB_TOKEN` with `packages: write`. Multi-arch
(amd64 + arm64).

```bash
# push this repo to GitHub, the Action publishes the image automatically
git remote add origin git@github.com:<you>/devin-proxy.git
git push -u origin main
```

Production deploy — compose only, no local build:

```bash
cp .env.example .env            # set DEVIN_PROXY_KEY (locks /v1 + admin)
mkdir -p data && chown 1000:1000 data   # container runs as uid 1000
docker compose pull
docker compose up -d
```

`docker-compose.yml` pulls `ghcr.io/rczlin/devin-proxy:latest` (built by
the Action, public — no registry login needed), loads `.env`, and
persists the request log + API keys + account pool in `./data`
(bind-mounted to `/data`). No token file needed — the container starts
with an empty pool; open `/admin` → 账号池 and add accounts via OAuth
login (paste-code flow works headless over SSH) or manual token entry.
To seed a token file instead, bind-mount it and set
`DEVIN_SESSION_TOKEN_FILE` in `.env`.

## Admin console

Open `http://127.0.0.1:8317/admin` — a persistent dashboard backed by
SQLite (`%APPDATA%\devin-proxy\devin-proxy.db`, override with
`DEVIN_PROXY_DB`):

- **仪表盘** — request/token totals, success rate, avg latency & TTFT,
  24 h chart, per-model stats, truncated-stream & retry counters
- **请求日志** — every request logged (model, endpoint, account, tokens,
  latency, status, caller key, messages); filter by model/status/flag/text/
  account, paginate, inspect detail, or purge. Retention capped by
  `DEVIN_PROXY_MAX_ROWS` (default 50000). Each row also stores the raw
  request body, a timestamped **upstream event timeline** (every delta /
  tool-call / stop_reason / trailer error / failover) and **every SSE
  payload sent to the client** — open 详情 → 上游事件 / SSE 输出 to
  debug dropped streams. Size capped by `DEVIN_PROXY_LOG_CAP`
  (default 200 KB per blob)
- **断流检测** — a Connect stream that ends without the required
  end-of-stream trailer is logged `truncated` and surfaced to the client
  as an SSE error (never a fake `stop`); non-streaming requests
  transparently retry a mid-stream failure on the next account
- **账号池** — multi-account management (above): OAuth login, manual add,
  local import, enable/disable/delete/test, session-pin list + unbind
- **模型** — all callable model uids/aliases + per-model usage stats
- **Playground** — test any model streaming or not, straight from the UI;
  optionally pin to a specific account
- **API Keys** — mint `sk-dp-…` keys for callers (SHA-256 hashed in the
  DB, shown once); enable/disable/delete. Per-key limits: model allowlist
  (uid or alias — `/v1/models` then returns only those) and a concurrency
  cap (over-limit calls get 429). `/v1` always requires
  `Authorization: Bearer`
- **状态** — pool summary, upstream connectivity check across accounts,
  DB path/size, uptime

## Endpoints

- `POST /v1/chat/completions` — streaming + non-streaming; system prompts,
  multi-turn history, `tools`/`tool_choice`, `temperature`/`top_p`/
  `max_tokens`, `stream_options.include_usage`; thinking models emit
  `reasoning_content` (DeepSeek-style)
- `POST /v1/responses` — full Responses API: `input` items (messages,
  images, `function_call_output`, `item_reference`), `instructions`,
  `tools`, `reasoning.effort`, `previous_response_id` chains,
  `store:false`, streaming SSE (`response.output_text.delta`,
  `response.function_call_arguments.*`, `response.reasoning_summary_*`,
  `response.completed`, …)
- `GET /v1/responses/{id}`, `DELETE /v1/responses/{id}`,
  `GET /v1/responses/{id}/input_items`
- `GET /v1/models` — model uids + aliases (filtered to the caller key's
  allowlist when set)
- `GET /healthz` — `{"ok": true}` only
- `/admin` — sign-in page → cookie session → `/admin/app` console

Model: pass a variant uid (`claude-sonnet-5-medium`, `gpt-5-6-sol-low`,
`swe-2-high`, …) or an alias (`claude`, `sonnet`, `opus`, `gemini`, `gpt`,
`swe`). `reasoning.effort` / `reasoning_effort` (`minimal|low|medium|
high`) remaps the uid's effort suffix when the variant exists.

## Layout

- `devin_proxy/proto.py` — protobuf schema (field numbers of the wire format)
- `devin_proxy/upstream.py` — Connect-RPC client (framing, GetUserJwt, streaming)
- `devin_proxy/creds.py` — local credential discovery (`detect_all`)
- `devin_proxy/accounts.py` — account pool (scheduling, pinning, cooldown,
  failover) + OAuth PKCE login + identity probe
- `devin_proxy/app.py` — FastAPI OpenAI shim, failover driver, request recording
- `devin_proxy/responses.py` — Responses API mapping + SSE + stored chains
- `devin_proxy/store.py` — SQLite persistence (requests, keys, accounts,
  session pins, responses)
- `devin_proxy/admin.py` — admin API router
- `devin_proxy/web/admin.html` — embedded admin SPA
- `devin_proxy/__main__.py` — launcher

Unofficial client of Cognition's backend — use at your own discretion w.r.t.
the Devin terms of service.
