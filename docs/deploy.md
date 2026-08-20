# Deploy runbook

How to deploy Graphban to the self-hosted LAN box, verify the exact revision
went live, and recover when a deploy goes wrong. This is the proven path — every
step here has been run in anger.

> The hosted (Railway) path is separate and tracked under the `railway` items;
> this runbook covers the current `rsync` + `docker compose` self-host deploy.

## What runs where

- **Target:** `ubuntu-srv` (LAN `192.168.50.81`), remapped ports because the box
  already runs another Postgres and had `:8000` busy — the **server's** `.env`
  sets `DB_PORT=5433`, `API_PORT=8001`, `WEB_PORT=8080`
  (`CORS_ORIGINS=http://192.168.50.81:8080,...`, stub providers).
- **Services:** `docker compose` — `db` (pgvector), `api`, `web` (nginx SPA).
- **Schema:** Alembic migrations run automatically on API startup (Postgres); the
  container comes up, migrates `0001 → head`, then serves.

## First run (local / a fresh box)

`./start.sh` brings the stack up and, on an instance with **no users yet**, provisions an
operator, a project, and one agent credential — then prints the MCP client config and
writes `~/.graphban/config.json` (chmod 600). Safe to re-run: it keys off the same
"no users" signal `seed()` uses, so a second run changes nothing.

It **refuses** in two cases, both deliberate:

- `HOSTED_MODE=true` — it mints a credential without authenticating anyone, which on a
  multi-tenant deployment is a hole, not a convenience. Invite the first operator instead.
- `SEED_ON_START=true` — seeding creates users during lifespan startup, so it would win
  the race and you would silently get the prototype dataset instead of your own project.

The password and API key are shown **once**. Keys are stored only as a hash and cannot be
recovered; if you lose them, mint a new key in Settings → API Keys.

Issuing that first credential is an authority gate, which is why it is a script an
operator runs rather than something an agent does — see PRD-14.

## Deploy

```bash
scripts/deploy.sh              # ships origin/main
scripts/deploy.sh <ref>        # ships any ref
```

**It syncs from a detached worktree at `../agentledger-wt-deploy`, not from your checkout.**
`rsync` ships whatever is on disk, and that stopped being a safe assumption the day agents
began working in the same clone: on 2026-08-20 a deploy was one command from shipping 17 files
and 281 lines of a fleet agent's UNCOMMITTED work to the live box, stamped `b71535f` — a sha
that described none of it. Release identity is the thing this runbook verifies, so a tree that
disagrees with its sha defeats the whole procedure.

The worktree removes both directions of that coupling: nothing can be mid-edit where nobody
works, and a deploy no longer touches the shared checkout at all. It is created on first run.
The script REFUSES if that worktree is somehow dirty rather than shipping it — anything there
means something is wrong that a deploy must not paper over.

It also ends the `GIT_SHA` trap documented in the invariants below: the sha is read from the
worktree instead of exported by hand into two separate statements, which is the step that
silently shipped `git_sha: unknown` on 2026-08-06.

Finally it VERIFIES rather than trusting: it polls `/health`, then fails unless the api and
the web bundle both report the sha it just shipped. A deploy that builds cleanly and serves
the previous revision looks identical to a successful one from the outside.

<details><summary>What it runs, if you need to do it by hand</summary>

```bash
git -C ../agentledger-wt-deploy checkout --detach origin/main
GIT_SHA=$(git -C ../agentledger-wt-deploy rev-parse --short HEAD)

# ALWAYS exclude .env and sync (see invariants).
rsync -az --delete \
  --exclude .git --exclude .env --exclude sync \
  --exclude node_modules --exclude dist --exclude __pycache__ \
  --exclude .venv --exclude .serena \
  ../agentledger-wt-deploy/ ubuntu-srv:~/agentledger/

ssh ubuntu-srv "cd ~/agentledger && GIT_SHA=$GIT_SHA docker compose up -d --build"
```

</details>

Migrations apply on API startup, so a schema change ships with the same command.

## Naming: what is frozen on a deployed box

The product rename does **not** touch anything the existing data is keyed by. On this
server the deploy path, the Postgres role, the database, and the volume all stay
`agentledger` — they predate the rename and live data is keyed by them.

> The tier-4 cosmetic sweep renamed these *in this document* while leaving the box
> untouched, so every command here pointed at a deploy directory and a Postgres role that
> do not exist — including the recovery commands, which would have failed at the worst
> possible moment. The section explaining which identifiers must never be renamed had its
> own identifiers renamed, and nothing caught it for four days.
>
> `test_infra_identity.py` now checks this document too. Freezing the values in
> `docker-compose.yml` was never enough: people follow the runbook, not the compose file.

- The volume is `agentledger_agentledger_pgdata` — `<compose-project>_<volume-key>`.
  Renaming either half orphans it and Postgres comes up empty.
- `POSTGRES_USER`/`_PASSWORD`/`_DB` are baked in at initdb. The server pins all three in
  its `.env`, so it is insulated regardless, but the compose defaults are frozen too for
  clones that never wrote one.
- `docker-compose.yml` pins `name: agentledger`, which decouples the compose project
  name from the directory name. **The repo directory is therefore safe to rename** —
  before that pin it was not, and doing so would have looked exactly like data loss.

`backend/tests/test_infra_identity.py` guards all of this against a future cosmetic sweep.

## Invariants (violating these has broken a deploy)

- **`--exclude .env`** — there is no local `.env`, so a bare `rsync --delete`
  would DELETE the server's `.env`. Then compose reverts to default ports (5432
  conflicts with the box's other Postgres) **and** the persisted Postgres volume
  keeps the *old* password, so the API dies at startup with
  `password authentication failed for user "agentledger"` (exit 3; Python's
  block-buffered stdout hides the traceback). Never sync over the server `.env`.
- **`--exclude sync`** — the server's `~/agentledger/sync/` is a root-owned
  container-written mount; rsync fails `exit 23` (`mkdir ... Permission denied`)
  without this exclude.
- **Pass `GIT_SHA`** on both the local `export` and the remote `docker compose`
  so the build arg reaches the image — otherwise `/health` reports
  `git_sha: "unknown"` and you cannot tell what is running.

  It has to be **two statements**, exactly as written above. Collapsing them into
  the one-liner `GIT_SHA=$(git rev-parse --short HEAD) ssh host "... GIT_SHA=$GIT_SHA ..."`
  looks equivalent and is not: the shell expands `$GIT_SHA` inside the quotes
  *before* applying the prefix assignment, so the remote receives an empty value.
  The build succeeds, the deploy looks clean, and release identity is silently
  lost — done exactly this way on 2026-08-06.

- **`git pull` first.** A deploy from a stale tree does not fail. It rsyncs the same
  bytes it sent last time, every Docker layer hits the cache, the containers restart
  cleanly, and the box keeps serving the previous revision. Done exactly this way on
  2026-08-15, one step after being warned about it in the same session.

  **What it looks like** — the tells are in the build output, and both are easy to
  read past:

  ```
  #20 [web stage-1 4/4] RUN echo "99504cf" > .../version.txt   CACHED
  #28 [api 7/7] RUN pip install --no-cache-dir .               CACHED
  ```

  That `echo` bakes the revision into the image. Seeing it `CACHED` **printing the
  sha you already had** means the build arg never changed, which means the source
  never changed. A real deploy rebuilds `COPY app ./app` and re-runs `pip install`;
  if those are cached, nothing shipped.

  This is the same failure as the `GIT_SHA` one-liner above, one step earlier: there,
  release identity is lost; here, release identity is *accurate about the wrong
  revision*. And it is the more dangerous of the two, because `git_sha: "unknown"`
  announces itself while `git_sha: "<the sha from last week>"` is indistinguishable
  from "already up to date" unless you know what you expected it to say. That is why
  Verify compares against `origin/main` rather than against your own `HEAD` — a check
  that compares the deploy to itself cannot catch this.

## Verify (release identity)

`/health` reports the exact running revision — always check it after a deploy:

```bash
ssh ubuntu-srv 'curl -s http://localhost:8001/health'
# {"status":"ok","service":"graphban-api","version":"0.1.0","git_sha":"<sha>","db":"ok"}
```

- `git_sha` must match **`git rev-parse --short origin/main`**, not merely "what you
  deployed". Checking it against your own `HEAD` compares the deploy to itself and
  passes for whatever you shipped — including the revision you shipped last time.
  See the stale-tree invariant below.
- `db: "ok"` confirms the API reached Postgres (readiness). `status` is `degraded`
  if the DB is unreachable — the API still answers 200 (liveness), so the
  container healthcheck tracks the process, not a DB blip.
- The web bundle's revision is at `http://localhost:8080/version.txt`.

Confirm the migration chain landed:

```bash
ssh ubuntu-srv 'cd ~/agentledger && docker compose exec -T db \
  psql -U agentledger -d agentledger -tc "SELECT version_num FROM alembic_version;"'
```

**Post-deploy note:** for the first few seconds after restart the API is warming;
an MCP/REST call may transient-fail once with an `internal` error whose hint says
"safe to retry once" — it is. Retrying succeeds.

## Recover

- **Server `.env` was clobbered / wrong ports:** recreate `~/agentledger/.env`
  with the remapped ports (`DB_PORT=5433 API_PORT=8001 WEB_PORT=8080` + the CORS
  origins and `POSTGRES_PASSWORD`) **before** `up`.
- **Postgres password mismatch** (volume kept an old password): reset it over the
  local socket (no password needed there), non-destructive:
  ```bash
  ssh ubuntu-srv 'cd ~/agentledger && docker compose exec -T db \
    psql -U agentledger -d agentledger -c "ALTER USER agentledger WITH PASSWORD '\''agentledger'\'';"'
  ```
- **Silent API crash at startup:** stdout is block-buffered, so reproduce with the
  traceback visible:
  ```bash
  ssh ubuntu-srv 'cd ~/agentledger && docker compose run --rm -e PYTHONUNBUFFERED=1 \
    --no-deps api python -c "import app.main"'
  ```

## Rollback

Deploys are just a git revision + a rebuild, so rolling back is redeploying an
earlier one:

```bash
git checkout <previous-good-sha>
export GIT_SHA=$(git rev-parse --short HEAD)
rsync ... ubuntu-srv:~/agentledger/          # same excludes as above
ssh ubuntu-srv "cd ~/agentledger && GIT_SHA=$GIT_SHA docker compose up -d --build"
# verify /health git_sha now shows the rollback target
```

A **backward-incompatible migration** is the one thing a code rollback doesn't
undo — the DB stays migrated. Prefer additive, backward-compatible migrations so a
code rollback is always safe; if a destructive migration must ship, snapshot the
volume first (`docker compose exec db pg_dump ...`).

## Railway (hosted) — code readiness

The hosted multi-tenant offering runs on Railway. The application code is Railway-ready
(this is Phase 5, slice A); provisioning the actual project/services is a separate,
account-touching step (the remaining `railway` items). What the code already handles:

- **Two services, one repo.** `backend/railway.json` and `web/railway.json` declare a
  Dockerfile build + healthcheck per service. Create two Railway services with root
  directories `backend/` and `web/`; each picks up its `railway.json`.
- **`$PORT`.** Both images honor Railway's injected `$PORT` — the API via
  `uvicorn --port ${PORT:-8000}`, the web via nginx's envsubst template
  (`nginx.conf.template`, `listen ${PORT}`). Locally (`docker compose`) the defaults
  (8000 / 80) keep working unchanged.
- **`DATABASE_URL`.** Railway's Postgres hands out a `postgres://…` URL; config rewrites
  `postgres://` / `postgresql://` to the psycopg3 driver automatically — paste it as-is.
- **Web → API address.** Set `API_UPSTREAM` on the web service to the backend's private
  address (e.g. `${{backend.RAILWAY_PRIVATE_DOMAIN}}:8000`); it defaults to `api:8000`
  for compose.
- **Migrations** run on API startup (same as self-host). Run a single API replica during
  a migration deploy to avoid two instances racing `upgrade head`.

### Required environment (backend service, hosted)

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

### The first operator (hosted)

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

**The email must already be in `PLATFORM_ADMIN_EMAILS`**, or the command refuses. Platform
admin is an env allowlist, not a flag on the row, so an account created with any other
address signs in perfectly and can never open the operator console — which looks like a
product bug rather than a configuration one. Note that Railway *stages* variable edits:
until you apply them they are absent from the running service while appearing saved.

### `/data/sync`

Drive/filesystem sync is a self-host convenience. On Railway either leave it
unconfigured (it stays dormant with no Drive folder set) or attach a volume at
`/data/sync` if you want it — it is not required for the hosted app to run.

## Code-graph sync (local → cloud) — the `graphban` CLI

> Renamed from `agentledger` (AL-262/AL-263). **Both console scripts work and the old one
> is kept indefinitely**, so any command already in a runbook keeps running. Config is now
> written to `~/.graphban/config.json` and read from there first, falling back to
> `~/.agentledger/config.json`; override with `GRAPHBAN_CONFIG` (or the older
> `AGENTLEDGER_CONFIG`). Newly minted keys start `gb_sk_`; existing `al_sk_` keys keep
> working and never need re-issuing.

A linked local instance builds its code graph on-box and pushes the *result* to a
cloud tenant (the AL-134 hybrid). The `graphban` console script drives that sync
directly against the instance database, so run it where `DATABASE_URL` points at your
instance — inside the backend container is simplest:

```bash
# Link once. Records the link in BOTH places (AL-281): `~/.graphban/config.json`
# (chmod 600; override via GRAPHBAN_CONFIG) is what the CLI's own commands read, and
# the `sync_link` row is what everything server-side reads. The DB write is required —
# run this where DATABASE_URL points at the instance, or the command exits non-zero
# rather than reporting a link the server can't see.
docker compose exec backend graphban link \
  --cloud-url https://cloud.example/ --api-key gb_sk_… --project core

docker compose exec backend graphban status   # link + last-synced state
docker compose exec backend graphban sync      # incremental push (only changed paths ship)
docker compose exec backend graphban purge --yes   # delete this project's graph from the cloud
```

Air-gapped / no direct connection? Move the graph as a portable, vector-free bundle
(the receiver re-embeds on import):

```bash
docker compose exec backend graphban export --project core --out graph.json
docker compose exec backend graphban import --in graph.json --prune
```

`sync` is incremental and resumable (content-hash manifest per path); a project with
its privacy toggle off (`sync_graph=false`) is skipped. The push respects the same
server-side tenant boundary as the API — the cloud stamps `org_id` from the credential,
never the payload.
