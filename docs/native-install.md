# Native install (no Docker)

A team server on a Mac or a Linux box, supervised by launchd or systemd. Docker
compose remains the default and the path the hosted service uses; this is the
other one.

**Not a `.app`.** A menu-bar wrapper around a headless team server is a worse
version of a system service with a GUI nobody is present to see.

Postgres is required and verified, never installed. The installer refuses when
Postgres or `pgvector` is missing, and it will not create, migrate or reconfigure
a cluster.

## One command

From a checkout or an unpacked release, with a `backend/.env` already written
(`DATABASE_URL` pointing at the Postgres this box runs, and a `JWT_SECRET` you
have recorded — the installer will not generate one):

```bash
python3 scripts/graphban_host.py install --root /opt/graphban --from .
```

That is a **stdlib script**, on purpose. The backend console-script is already
named `graphban` and it is the code-graph sync CLI; it also cannot run before
the venv exists. `graphban_host.py` is the thing that creates that venv.

What it does, in order:

1. Preflight — Postgres reachable, `vector` enabled, the API port free.
2. Copies the release to `<root>/current`.
3. Creates `<root>/venv` (outside the swapped release) and `pip install -e` the backend.
4. Writes a LaunchDaemon or systemd system unit carrying `PORT`, `HOST` and `GIT_SHA`.
5. Hands the unit to the supervisor and refuses if the job is not actually running.

```bash
python3 scripts/graphban_host.py upgrade --root /opt/graphban --release ./new --sha <rev>
python3 scripts/graphban_host.py uninstall --root /opt/graphban   # never drops the database
```

Upgrade keeps the previous `backend/.env`, refreshes the venv from the new
tree, rewrites the unit with the new sha, and puts the old release back if
`/health` does not come up serving that sha.

## Packing a release

A git tag is not a release, and GitHub's source zip is not either. Pack the
directory `upgrade --release` consumes:

```bash
python3 scripts/graphban_pack.py 2026.09.3
# dist-release/graphban-2026.09.3/
# dist-release/graphban-2026.09.3.tar.gz
```

That tree is backend + Alembic, prebuilt `web/dist` (or an `SPA` file saying
`n/a` with `--api-only`), a `GIT_SHA` file, and **no** `.env`. The packer
checks the ref out into a detached worktree first, so uncommitted files in the
working tree cannot ride along.

Attach the tarball to the GitHub Release (`gh release upload 2026.09.3
dist-release/graphban-2026.09.3.tar.gz`). The packer does not upload, and it
does not run the swap.

```bash
tar xf graphban-2026.09.3.tar.gz
# write backend/.env from .env.example on a first install
python3 graphban-2026.09.3/scripts/graphban_host.py upgrade \
  --root /opt/graphban --release ./graphban-2026.09.3 --sha "$(cat graphban-2026.09.3/GIT_SHA)"
```

## Layout

```
/opt/graphban/
  current/          live release (swapped on upgrade)
    backend/.env    operator config — survives upgrades
    GIT_SHA
  previous/         last release, for rollback
  venv/             interpreter; lives outside the swap
```

The service working directory is `<root>/current/backend`. That is how
pydantic-settings finds `.env`, and it is the directory upgrade swaps.

## Privileged vs user-domain

The shipped service is a **LaunchDaemon** (`/Library/LaunchDaemons`) or a
systemd **system** unit (`/etc/systemd/system`). Those need root.

`--user-domain` (macOS) and `--user-scope` (Linux) install into the logged-in
user's domain so the mechanism can be proven without sudo. They do not start at
boot on a box nobody is logged into. Pass the same flag on upgrade and
uninstall, or the restart talks to the wrong supervisor.

## Web UI

If `web/dist` is present in the release, the API serves it (one process, no
nginx). The installer does not run `pnpm build`. Ship a prebuilt bundle, or
accept an API-only install — `/health` will report `web  n/a (no web bundle
installed)` rather than pretending the bundle matches.

`GIT_SHA` must be in the process environment for `/health` to report the
installed revision. The unit sets it; stuffing it only into `.env` is wiped the
moment an upgrade replaces the tree, which is why the unit carries it.

## What has been walked

See [acceptance-prd27.md](acceptance-prd27.md). macOS, all eight steps, in the
user domain. Linux: the systemd unit itself (start, restart-on-kill, crash-loop
refusal, removal) against systemd 257. Not walked: a privileged install on
either platform, upgrade/rollback/uninstall on Linux, a release whose
dependencies changed.

## Docker is still the other path

[Getting started](getting-started.md) and [Deploy](deploy.md) are compose.
Nothing here replaces them.
