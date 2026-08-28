# Deploying on Railway

The hosted multi-tenant offering runs on Railway. This is the single home for Railway
specifics; `deploy.md` is the general runbook (build, release identity, recover, rollback)
and applies here too.

> **Moved, not copied (GRPH-36).** This lived inside `deploy.md` as a section. The item asked
> for a `deploy-railway.md` "covering services, variables, and the pgvector requirement" —
> written as a new file it would have been a second description of the same variables, and two
> lists of environment variables disagree within a month. GRPH-424 and GRPH-528 are both that
> failure. So the section moved and `deploy.md` points here.

## Before anything else: Postgres must have pgvector

**Railway's stock Postgres does not include the `vector` extension, and without it this app
never starts.** Migration `0001_initial` opens with:

```python
op.execute("CREATE EXTENSION IF NOT EXISTS vector")
```

That is the first statement of the first migration, and migrations run on API startup. On a
plain Postgres it raises `UndefinedFile: extension "vector" is not available`, the lifespan
dies, and the container restarts into the same failure forever.

**Provision the pgvector image, not the default Postgres plugin.** Railway's template
directory carries one; the CI here pins `pgvector/pgvector:pg16`, which is a known-good
version pairing.

Verify before deploying the API — this must print one row:

```bash
railway run --service postgres psql -c "SELECT 1 FROM pg_available_extensions WHERE name = 'vector'"
```

If it prints nothing, the database is wrong. Nothing else in this document will help.

> Why this is called out first: it is invisible in every other environment. SQLite dev and the
> SQLite CI job use `create_all` and never run migrations, so a tree that cannot deploy looks
> completely healthy locally. It is the same shape as the alembic logging defect in GRPH-33 —
> the Postgres-only path is the one nobody exercises by accident.

## Services, and what the code already handles

The hosted multi-tenant offering runs on Railway. The application code is Railway-ready;
provisioning the actual project/services is a separate, account-touching step.

- **Two services, one repo.** `backend/railway.json` and `web/railway.json` declare a
  Dockerfile build + healthcheck per service. Create two Railway services with root
  directories `backend/` and `web/`; each picks up its `railway.json`.
- **`$PORT`.** Both images honor Railway's injected `$PORT` — the API via
  `python -m app.serve` (which reads `$PORT`; it exists so logging is configured before
  uvicorn's first line — see Observability below), the web via nginx's envsubst template
  (`nginx.conf.template`, `listen ${PORT}`). Locally (`docker compose`) the defaults
  (8000 / 80) keep working unchanged.
- **`DATABASE_URL`.** Railway's Postgres hands out a `postgres://…` URL; config rewrites
  `postgres://` / `postgresql://` to the psycopg3 driver automatically — paste it as-is.
- **Web → API address.** Set `API_UPSTREAM` on the web service to the backend's private
  address (e.g. `${{backend.RAILWAY_PRIVATE_DOMAIN}}:8000`); it defaults to `api:8000`
  for compose.
- **Migrations** run on API startup (same as self-host). Run a single API replica during
  a migration deploy to avoid two instances racing `upgrade head`. See Scaling policy —
  today that is not a per-deploy precaution but a standing one.

## Required environment (backend service, hosted)

| Var | Notes |
|-----|-------|
| `HOSTED_MODE=true` | Turns on the org layer, quotas, and tighter tenant isolation. |
| `JWT_SECRET` | Long random string. `REQUIRE_STRONG_SECRET=true` refuses to boot on a weak one. |
| `SECRET_ENCRYPTION_KEY` | Required in hosted mode (encrypts BYOK provider keys); boot fails without it. |
| `DATABASE_URL` | From the Railway Postgres plugin (auto-normalized). |
| `PORT` | Injected by Railway. |
| `PLATFORM_ADMIN_EMAILS` | Comma-separated operator allowlist for manual plan assignment. |
| `APP_BASE_URL` | Public SPA origin, used to build org-invite links. |
| `SMTP_HOST` / `SMTP_*` | Invite email delivery (falls back to console/outbox if unset). |
| `REDIS_URL` | Optional; shared rate-limit store across replicas (in-process fallback otherwise). |
| `CORS_ORIGINS` | The web service's public origin(s). |
| `TRUSTED_PROXY=true` | Behind Railway's edge, so `X-Forwarded-For` is trustworthy. |
| `FORWARDED_ALLOW_IPS` | **Read by uvicorn, not by `Settings`** — so it is set on the service and nothing in the image changes. Without it the API discards `X-Forwarded-Proto` and `request.url.scheme` reads `http` on an HTTPS-only origin (GRPH-477). uvicorn honours forwarded headers only from `forwarded_allow_ips`, default `127.0.0.1`, and on Railway `web` (nginx) and `backend` are separate services — so nginx's requests arrive from an internal address and are never loopback. **The precondition is that nginx is the only way in.** `*` trusts whatever can open a socket to the API, which is safe exactly when nothing but `web` can — verified 2026-08-28: `backend-production-668d.up.railway.app/health` returns Railway's own 404 (`Application not found`, their router, not the app) while `cloud.graphban.dev/health` serves normally, so the public backend domain GRPH-478 found is closed. Set this AFTER confirming that, never before: trusting a forwarded scheme while an open path exists lets a direct caller choose it. A CIDR is tighter than `*` and needs the peer address `web` actually arrives from — the private network runs IPv4 and IPv6, so read it from a request log rather than assuming a range. |
| *(nothing to set)* | Release identity is automatic on Railway. `RAILWAY_GIT_COMMIT_SHA` is injected into the container and read directly — it is **not** in the referenceable variable set, so `GIT_SHA=${{ RAILWAY_GIT_COMMIT_SHA }}` resolves to empty even on a push-triggered deploy. That was tried and measured. Leave `GIT_SHA` unset here; a value baked at build time still wins where one exists, and `/health` reports `unknown` rather than a blank when neither is available. |

## The first operator (hosted)

A fresh hosted instance cannot be logged into, and the cycle is airtight:
`SIGNUP_MODE=invite_only` refuses registration without a token, issuing a platform invite
requires a platform-admin JWT, and a JWT requires an account. `graphban init` deliberately
refuses hosted mode — it mints an API key without authenticating anyone, which is a hole on
a multi-tenant deployment.

Break the cycle from inside the deployment, where having shell access *is* the authority
proof:

```bash
railway ssh --service backend \
  'graphban admin bootstrap-hosted --email you@example.com --org-name your-org'
```

It creates the operator and their org, prints a generated password **once**, and mints no
API key and no project — everything past the first login goes through the product. It
refuses on a second run, so it cannot create a rival operator.

**The address must be one the login route accepts**, and provisioning checks that with the
same validator before writing anything (GRPH-461). That rules out RFC 2606 reserved TLDs —
`example.invalid`, anything under `.test` — which look like fine placeholders in a runbook
and are not usable addresses. Before the check, `init` reported `provisioned: true` and
handed over a password for an account the login route would refuse; the failure only
surfaced at the sign-in screen, where it reads as a mistyped password.

**The email must already be in `PLATFORM_ADMIN_EMAILS`**, or the command refuses. Platform
admin is an env allowlist, not a flag on the row, so an account created with any other
address signs in perfectly and can never open the operator console — which looks like a
product bug rather than a configuration one. Note that Railway *stages* variable edits:
until you apply them they are absent from the running service while appearing saved.

## Observability

`LOG_JSON=true` on both services. Without it the stream is human-readable text, which is
the right default for `docker compose logs` on a self-host box and the wrong one for a log
platform.

**What you get.** One JSON object per line, from both services:

```
nginx    {"ts":"…","request_id":"604a1378…","method":"GET","path":"/api/public/roadmap",
          "status":404,"duration_s":0.003,"upstream_status":"404","upstream_time":"0.002"}
backend   {"ts":"…","level":"INFO","logger":"graphban.access","request_id":"604a1378…",
          "http":{"method":"GET","path":"/api/public/roadmap","status":404,"duration_ms":1.9}}
```

**The `request_id` is the same on both.** nginx generates it and forwards it as
`X-Request-ID`; the API honours an inbound one and echoes it on the response. So a search
for one id returns the full path of a request across the hop — which is where the
2026-08-26 outage actually lived, and the pair to compare there is `status` against
`upstream_status`: nginx returned 499 (client gave up) while the upstream returned nothing.

**Before GRPH-33 there was no request log at all.** `configure_logging` silenced
`uvicorn.access` as a duplicate of something that did not exist; three requests including a
404 produced zero lines. `LOG_JSON=true` also did not make the stream JSON — uvicorn
configures its own loggers before the app's lifespan runs, so its lines stayed plain text
alongside JSON app lines.

**Deliberately absent, and do not add them without reading why:**

- *The query string.* The public share link's token is a query parameter and is the
  credential. Both access logs record the path only (`$uri` in nginx, `scope["path"]` in the
  API). Logging it would copy live secrets into a searchable store no revocation path
  reaches. Pinned by `backend/tests/test_observability.py`.
- *The client IP.* Which forwarded hop is the real caller is open (GRPH-517) — `client_ip`
  reads the leftmost, which is the one an attacker writes. A field named `ip` would be
  authoritative-looking and sometimes attacker-chosen.

Health checks **are** logged. Suppressing the noisy ones is what produced the original hole;
volume belongs to `LOG_LEVEL`, which an operator can lower knowing what they lose.

## Scaling policy

**API replicas: 1.** Not a performance judgement — two preconditions are unmet, and each has
a specific consequence.

| Precondition | State | What >1 replica does today |
|---|---|---|
| Shared rate-limit store (`REDIS_URL`) | Optional, off by default | `ratelimit.allow` falls back to `spam._hits`, an in-process dict. N replicas = N independent buckets, so **every limit is silently N×** — including login and the `/api/public/*` limits GRPH-32 added. The endpoints still read as protected. |
| Migration locking (AL-31) | **Not implemented** — `app/migrate.py` takes no lock | Every replica runs `run_migrations()` on boot. Two booting together race `upgrade head`. |

Before raising it, verify both: `REDIS_URL` set on the backend service, and a lock in
`app/migrate.py`. The web service is nginx over static files and is stateless; replicate it
freely.

**Local-disk state.** The only writer under `app/` outside the CLI is
`drive_sync.LocalFolderBackend`, which serves the optional `/data/sync` volume. A Railway
volume attaches to one instance, so a deployment that uses Drive sync is single-replica for
that reason too. Everything else is in Postgres.

**Resource limits and alerting** are dashboard settings, not repo config — they are the part
of GRPH-33 that cannot be committed. Set them against a measured baseline (Railway's own
HTTP error-rate and response-time panels) rather than a guess.

## `/data/sync`

Drive/filesystem sync is a self-host convenience. On Railway either leave it
unconfigured (it stays dormant with no Drive folder set) or attach a volume at
`/data/sync` if you want it — it is not required for the hosted app to run.
