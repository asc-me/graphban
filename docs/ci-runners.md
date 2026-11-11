# CI runners

CI runs on our own machines. Only three workflows still use GitHub-hosted runners:

- **`Fleet + CLI tests (Windows)`** (`windows-latest`). We have no Windows runner.
- **`release-cli.yml` and `release-fleet.yml`** (`ubuntu-latest`). They publish to PyPI
  with trusted publishing, which is kept on GitHub's runners.

| Label | Runners | Used by |
| --- | --- | --- |
| `graphban-ledger` | `ubuntu-srv-ledger` | `ci` (attests to the ledger at `127.0.0.1:8080` on ubuntu-srv), `security-scans` |
| `graphban-ci` | `ubuntu-srv-ci-1..3`, `rog-om-01-ci-1..3` | every other job in `ci.yml`, and `stale-base.yml` |

## Rules for jobs on these runners

- **This repo is public.** A fork's pull request never runs on our machines. The `changes`
  job is skipped for a fork's PR, and every job that needs it is skipped too. The repo
  setting **Require approval for all external contributors** is also on.
- **No fixed host ports.** Several jobs share one machine, and ubuntu-srv's own Postgres (the
  ledger) holds 5432. Service containers publish a random host port, and steps read it from
  `job.services.<name>.ports['<port>']`.
- **No `sudo` in a job.** System packages are installed on the hosts once. Chromium's
  libraries come from apt on ubuntu-srv (`npx playwright install-deps chromium`) and from
  pacman on rog-om-01 (Arch-based). That is why the bench step runs
  `playwright install chromium` without `--with-deps`.

## Hosts

- **ubuntu-srv** (Ubuntu 25.10, 16 cores, 30 GB). Runners are system services:
  `actions.runner.asc-me-graphban.<name>.service`. This machine also runs the ledger, so
  heavy CI shares it with the ledger.
- **rog-om-01** (Omarchy/Arch, 16 cores, 38 GB). Runners are user services with linger:
  `systemctl --user status actions-runner-<name>`.

To add a runner, get a registration token
(`gh api -X POST repos/asc-me/graphban/actions/runners/registration-token`) and run
`config.sh --unattended --labels graphban-ci` in a fresh `~/actions-runner-<name>`. On
rog-om-01, copy `bin/runsvc.sh` to the runner root before you start its user unit.
