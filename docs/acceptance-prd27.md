# PRD-27 acceptance walk — a native install, end to end

**Status:** walked on macOS 2026-08-28 (GRPH-587). Linux verified for S4 separately against
systemd 257; the full Linux walk is named as not-done below rather than implied.

Each slice of PRD-27 was verified alone. This is the walk that runs them in sequence, which is
a different question — GRPH-503 is the precedent: *"every walk invoked the CLI directly with
`--agent-id`, so the adapter's argv was verified and the thing the argv is FOR never was."*

**It found three defects, and one of them was found before the walk ran a single command.**

## Results

| # | Step | Result |
|---|---|---|
| 1 | preflight, no database there | ✅ exit 3, named the DSN and `pg_isready` |
| 2 | preflight, real Postgres | ✅ exit 0 — PostgreSQL 16.14, pgvector 0.8.5 enabled, port free |
| 3 | install the service | ⚠️ **failed twice first** — see findings 2 and 3 |
| 4 | `/health` reports the installed sha | ✅ `git_sha: aaaa111`, `db: ok` |
| 5 | kill the process | ✅ launchd restarted it, pid 45316 → 45740 |
| 6 | upgrade `aaaa111` → `bbbb222` | ⚠️ **rolled back the first time** — see finding 3 |
| 6b | upgrade, after the fix | ✅ api `bbbb222`, web `n/a (no bundle)`, alembic `0092` |
| 7 | upgrade to a release that cannot start | ✅ refused and rolled back; `bbbb222` serving again |
| 8 | uninstall | ✅ nothing serving, nothing in `launchctl list`, **database intact at 745,472 bytes** |

## Finding 1 — the slices did not compose, found by reading them side by side

| slice | path |
|---|---|
| S3 launchd | `WorkingDirectory = root/backend` |
| S4 systemd | `WorkingDirectory = root/backend` |
| S5 upgrade | swaps `root/current` |

An upgrade replaced `root/current` while the service went on reading `root/backend`. It failed
**safe** — the identity check saw the old sha and rolled back — but it could never succeed. The
upgrade path was unusable against an install the installer itself produced.

Every test in all three slices passed, because each was only ever exercised on its own.

Fixed to one layout: releases in `root/current`, service working directory
`root/current/backend`, and the venv at `root/venv` — **outside** the swapped release, because
a swap would otherwise replace the very interpreter the unit names. `test_install_layout.py`
now asserts the three agree.

## Finding 2 — the macOS install reported success for a crash-looping service

`launchctl list` prints `PID  Status  Label`, and a failing job is still listed:

```
-	1	dev.graphban.walk
```

The check asked only whether the **label appeared**, so it answered yes for a service dying on
`ModuleNotFoundError` every few seconds. Then, once that was fixed, it still passed — because
it looked immediately after `bootstrap`, when launchd has just forked and the job *does* have a
pid for the instant before it exits.

**S4 had already learned exactly this on systemd** and grown a settle plus an `NRestarts`
check. The lesson was never carried back to S3. That cross-slice gap is what a walk is for.

Now: a real pid, checked after a settle. A broken install exits 1 and prints the listing.

## Finding 3 — S1 made the web bundle optional; S5 required it

S1 mounts the SPA only if `web/dist` exists, so an API-only install has no bundle and no
`version.txt`. S5 compared a web sha unconditionally, so that install was **permanently
un-upgradeable** — the upgrade rolled back reporting `web serves '', expected 'bbbb222'`, which
reads like a broken deploy rather than a missing feature.

The web fact is now **not applicable** when no bundle is installed, and says so:

```
    api      bbbb222
    web      n/a (no web bundle installed)
    alembic  0092
```

Reported rather than skipped — "there is no bundle" and "the bundle matches" must not read the
same.

## What this walk did NOT establish

- **The full Linux walk.** S4 was verified on real systemd 257 — unit accepted, service
  started, `Environment=PORT` reached the process, `kill -9` restarted it, a broken unit
  refused, everything removed — but steps 6–8 (upgrade, rollback, uninstall) were walked only
  on macOS. The code is platform-agnostic; that is an argument, not a measurement.
- **A privileged install.** Both platforms were walked in the user domain, so no `sudo` was
  needed on anyone's machine. The shipped service is a system daemon/unit; the domain differs,
  the unit contents do not.
- **A real SPA bundle.** The walk ran API-only, which is what surfaced finding 3. A walk with a
  bundle would exercise the web sha comparison rather than its absence.
- **Dependency changes across an upgrade.** The venv lives outside the release and is not
  refreshed by `upgrade`, so a release needing new packages would start against the old ones.
  Named here rather than discovered later.

## Since S7 (GRPH-601)

`scripts/graphban_host.py` is the command the PRD named. It places `root/current`, creates
`root/venv`, puts `GIT_SHA` on the unit, keeps the operator's `.env` across a swap, and
refreshes the venv from the new tree. The four measurements above are still measurements —
S7 closed the holes in the code, not the walks that were not run.
