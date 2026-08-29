# Configuration

Configuration is via environment variables. In Docker, set them in `.env` (copy from
`.env.example`); locally, export them before running the API. The backend reads them through
`backend/app/config.py` (pydantic-settings).

## Database

| Var | Default | Notes |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql+psycopg://graphban:graphban@localhost:5432/graphban` | Use `sqlite:///./dev.db` for zero-infra dev. Postgres runs Alembic migrations; SQLite uses `create_all`. |

## Auth

| Var | Default | Notes |
| --- | --- | --- |
| `JWT_SECRET` | dev placeholder | **Set a long random value in production** (≥ 32 bytes) |
| `ACCESS_TOKEN_MINUTES` | `30` | Access token lifetime |
| `REFRESH_TOKEN_DAYS` | `14` | Refresh token lifetime |
| `JWT_ALGORITHM` | `HS256` | Signing algorithm. Changing it invalidates every issued token |
| `REQUIRE_STRONG_SECRET` | `false` | **Refuses to boot** on a weak or default `JWT_SECRET` rather than warning. Set it on anything exposed |
| `MIN_PASSWORD_LENGTH` | `8` | Minimum on signup and password change |
| `SIGNUP_MODE` | `open` | `open` or `invite_only`. **`open` means anyone who can reach the instance can create an account** — the hosted deployment runs `invite_only` |
| `PLATFORM_ADMIN_EMAILS` | *(empty)* | Comma-separated. Who reaches the operator plane. Empty means nobody, which is the safe default and not an oversight |
| `SECRET_ENCRYPTION_KEY` | *(empty)* | At-rest encryption for stored provider keys. **Empty means BYOK credentials are stored unencrypted**; set it before anyone saves one |
| `GITHUB_WEBHOOK_SECRET` | *(empty)* | Verifies webhook signatures. Empty means an unsigned payload is accepted, so set it wherever the webhook is reachable |
| `TRUSTED_HOPS` | `0` (compose sets `1`) | How many proxies stand between the app and the internet, for per-IP rate limiting (GRPH-553). The app reads that many hops from the **right** of `X-Forwarded-For` — only ever a value a proxy actually observed, never the client-supplied prefix nginx appends to. **1** for the compose stack (client → nginx → app); **2** behind an edge that already sets the header (client → edge → nginx → app). `0` disables it and every caller shares one bucket. Fails **closed**: a header shorter than the chain, one whose hop is not an address, or a request whose socket peer is not on the deployment's own network all fall back to the socket peer and log why — the last of those is what stops a caller that reaches the app port directly from choosing its own bucket (GRPH-478) |
| `TRUSTED_PROXY` | `false` | **Deprecated — use `TRUSTED_HOPS`.** Reads the *first* `X-Forwarded-For` hop, which is whatever the client sent when the proxy appends rather than overwrites — and the bundled nginx appends (GRPH-439). Still honoured so an existing deployment's config does not silently change meaning, but it now logs that the bucket key is spoofable. `TRUSTED_HOPS` wins when both are set |
| `FORWARDED_ALLOW_IPS` | *(uvicorn)* | `127.0.0.1` | Which peer's `X-Forwarded-Proto`/`Host` uvicorn believes. **Not a `Settings` field** — uvicorn reads it from the environment itself. The default is loopback, which is wrong whenever the proxy is a separate service or container: the header is then discarded and generated URLs come out `http` (GRPH-477). Set the private range the proxy reaches the API on; `*` trusts every caller and is safe only while nothing can reach the API directly — check that first, and see [deploy-railway.md](deploy-railway.md). |
| `PR_COOLDOWN_SECONDS` | `600` | How long after a PR is linked an item must wait before it can move to `done`, so CI has time to run (GRPH-567). An item with **no** linked PR is never delayed. `0` disables it, which is right where there is no CI — a delay protecting nothing is friction. The default is this repository's own pipeline: its two backend jobs take 4m34s and 8m20s, so lower it if yours is faster |
| `REQUIRED_PREDICATES` | *(empty)* | Comma-separated predicate names an attestation must carry, **passing**, before an item may reach `done` — e.g. `conformance,adversarial` (GRPH-569). Empty means any sound attestation completes, which is what an install with only the CI adapter needs, so turn this on only once an adapter actually emits the name. Names are unioned across adapters, so CI can answer one and a reviewer another. A required name that nothing attested refuses completion as *never checked*, which is reported differently from a check that ran and **failed** |
| `MCP_DEFAULT_TOOL_TIERS` | *(empty)* | Tool tiers granted to **every** key on top of its own, comma-separated from `prd,codegraph,fleet,misc` (GRPH-571). Empty is the default and correct: a key is shipped the *core* manifest and opts into the rest at mint. This exists as the undo — a key minted before migration 0093 has no tiers, so its manifest shrinks the moment you deploy, and setting this to all four restores exactly the old manifest deployment-wide while you work out which keys need what. Nothing is ever **forbidden** by a tier: a tool left out of the manifest still runs when called, so the symptom of getting this wrong is an agent not knowing a tool exists, not an error. Additive only — it can never remove a tier a key was minted with |
| `LOGIN_RATE_PER_MIN` | `10` | Per-IP and per-email login attempts |
| `ORG_RATE_PER_MIN` | `300` | Per-org API calls |
| `INVITE_EXPIRY_DAYS` | `14` | How long an unaccepted invite stays usable |

## Email (SMTP)

Invites and password flows go nowhere without these. An unset `SMTP_HOST` does not fail —
mail is simply not sent, so an invite can be "issued" and never arrive.

| Var | Default | Notes |
| --- | --- | --- |
| `SMTP_HOST` | *(empty)* | Empty disables sending entirely |
| `SMTP_PORT` | `587` | |
| `SMTP_USER` | *(empty)* | |
| `SMTP_PASSWORD` | *(empty)* | |
| `SMTP_FROM` | `Graphban <no-reply@graphban.dev>` | Envelope sender |
| `SMTP_STARTTLS` | `true` | |
| `APP_BASE_URL` | `http://localhost:5173` | The base of links in outbound mail. Wrong here means invites point at localhost |

## Deployment mode and operations

| Var | Default | Notes |
| --- | --- | --- |
| `HOSTED_MODE` | `false` | Multi-tenant posture: org gates on, first-run bootstrap refused, share links accept only the token |
| `LOG_LEVEL` | `INFO` | |
| `LOG_JSON` | `false` | Structured logs. On for anything with log search |
| `REDIS_URL` | *(empty)* | Empty keeps rate-limit state in process, which is per-worker rather than per-deployment |
| `SYNC_DIR` | `/data/sync` | Where the Drive/file sync writes |
| `SYNC_CLOUD_URL`, `SYNC_API_KEY` | *(empty)* | Local → cloud code-graph push |
| `UPSTREAM_FEEDBACK_URL` | `https://feedback.asc-me.dev/api/public/requests` | Where `report_graphban_issue` sends |
| `UPSTREAM_FEEDBACK_PROJECT` | `agentledger` | |
| `UPSTREAM_FEEDBACK_TOKEN` | *(empty)* | |

## AI providers

| Var | Default | Notes |
| --- | --- | --- |
| `CHAT_PROVIDER` | `stub` | `stub \| ollama \| anthropic` — switchable live in Settings |
| `EMBED_PROVIDER` | `stub` | `stub \| ollama \| openai` — deploy-time (must match `EMBED_DIM`) |
| `EMBED_DIM` | `384` | Vector dimension: stub 384, nomic-embed-text 768, OpenAI 1536 |
| `OLLAMA_BASE_URL` | `http://localhost:11434` (Docker: `host.docker.internal`) | |
| `OLLAMA_CHAT_MODEL` | `llama3.1:8b` | |
| `OLLAMA_EMBED_MODEL` | `nomic-embed-text` | |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | Any OpenAI-compatible endpoint |
| `OPENAI_API_KEY` | — | For OpenAI-compatible embeddings |
| `OPENAI_EMBED_MODEL` | `text-embedding-3-small` | |
| `ANTHROPIC_API_KEY` | — | Read by the `anthropic` SDK |
| `ANTHROPIC_MODEL` | `claude-opus-4-8` | |
| `OLLAMA_AUTH_KEY` | *(empty)* | Bearer token, for an Ollama behind a reverse proxy |
| `LLM_TIMEOUT_SECONDS` | `90` | Per model call. A local coding model can exceed this |
| `EMBED_MAX_RETRIES` | `2` | Retries before an embedding write gives up |
| `REQUIRE_REAL_EMBEDDINGS` | `false` | **Refuses to write a stub vector.** Without it a misconfigured provider silently fills the index with vectors that match nothing |

See [AI providers](ai-providers.md) for the details (and why embeddings are deploy-time).

## App behavior

| Var | Default | Notes |
| --- | --- | --- |
| `CREDENTIAL_RETRY_SECONDS` | `60` | How often the credential retry loop re-asks providers that could not be reached (PRD-25). `0` disables the background task entirely — what the test suite runs at, so a timer never fires mid-test. |
| `SEED_ON_START` | `false` | Load the demo dataset on an empty DB. Default off — the app starts empty and you sign up in the UI. Set `true` for a populated demo. |
| `PUBLIC_SUBMIT_ENABLED` | `true` | Allow the public feedback endpoints |
| `CORS_ORIGINS` | `http://localhost:8080,http://localhost:5173` | Comma-separated allowed origins |

## Docker Compose

`docker-compose.yml` reads these (all optional; defaults work):

| Var | Default |
| --- | --- |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | `agentledger` |
| `DB_PORT` | `5432` |
| `API_PORT` | `8000` |
| `WEB_PORT` | `8080` |

## Frontend

The dev server proxies `/api` to the backend; override the target with `VITE_API_PROXY`
(default `http://localhost:8000`). In the built image, nginx proxies `/api` and `/health` to
the `api` service.

## Wire names during the Graphban rename

The product is being renamed from Graphban to Graphban. Every wire-facing name accepts
**both** forms, and nothing that was ever valid stops working:

| Surface | Produced now | Old — still accepted |
| --- | --- | --- |
| API key prefix | `gb_sk_` | `al_sk_` |
| CLI console script | `graphban` (both installed) | `graphban` |
| CLI config | written to `~/.graphban/config.json`, `GRAPHBAN_CONFIG` | read from `~/.agentledger/config.json`, `AGENTLEDGER_CONFIG` |
| MCP server name | `graphban` | — (the `mcp__*` tool namespace comes from *your* client config key, not from the server) |
| Upstream report tool | `report_graphban_issue` | `report_agentledger_issue` (dispatches, not advertised) |

API keys are stored only as a SHA-256 hash and cannot be rewritten, so the accepted-prefix
list only ever grows — **an existing `al_sk_` key never needs re-issuing.** An existing
`~/.agentledger/config.json` is likewise read where it lies and never moved or deleted;
removing it is the operator's call.

The accept-both change shipped and was deployed to every instance *before* production
moved, so no instance could ever be handed a credential it did not understand.
