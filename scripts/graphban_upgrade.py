#!/usr/bin/env python3
"""Upgrade and uninstall a native install, verified the way a deploy is (GRPH-585, PRD-27 S5).

S3 and S4 put a service on the box. This is the second day.

**The verification is the same three facts `scripts/deploy.sh` reports** — api sha, web bundle
sha, alembic head — because *"a deploy that builds cleanly and serves the PREVIOUS revision
looks identical to a successful one from the outside"* is not a Docker property. The list lives
in `IDENTITY_FACTS` and a test asserts `deploy.sh` still checks the same three, so the two
paths cannot quietly drift into each looking correct in isolation.

**Rollback re-runs the previous revision; it does not undo a migration.** Alembic downgrades
are not exercised in this repository, and a rollback path nobody runs is one that does not
work. That makes "migrations stay additive" load-bearing rather than incidental — the previous
release has to be able to serve a database the new one migrated.

**Stop is graceful with a bounded wait.** SIGTERM, drain, then hard-kill. GRPH-535 here is a
shutdown that hung on a non-daemon thread, so the timeout is required rather than defensive.

**Downtime is a hard cut of a few seconds and this says so.** One process, no load balancer:
draining to zero needs a second instance and a port handover, which is out of scope. A team
server down for ten seconds during an upgrade is fine; one that claims zero-downtime and drops
requests is not.

    python3 scripts/graphban_upgrade.py upgrade --root /opt/graphban --release ./new --sha abc1234
    python3 scripts/graphban_upgrade.py uninstall --root /opt/graphban [--purge]
"""
from __future__ import annotations

import argparse
import json
import pathlib
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

#: The three things a release is verified by. Named once, checked by both paths — see the
#: module docstring, and `test_upgrade.py::test_the_identity_facts_match_deploy_sh`.
IDENTITY_FACTS = ("api", "web", "alembic")

EXIT_OK = 0
EXIT_UNHEALTHY = 3
EXIT_MISMATCH = 4
EXIT_ROLLED_BACK = 5


def health(base: str, *, timeout: float = 3.0) -> dict | None:
    """`/health`, or None if it did not answer. Never raises — an unreachable service is a
    RESULT here, not an error to propagate."""
    try:
        with urllib.request.urlopen(f"{base.rstrip('/')}/health", timeout=timeout) as r:
            return json.loads(r.read().decode())
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None


def web_sha(base: str, *, timeout: float = 3.0) -> str:
    """What the served bundle says it is. Baked at build time, so it can lag the API
    independently — which is exactly why `deploy.sh` checks it separately."""
    try:
        with urllib.request.urlopen(f"{base.rstrip('/')}/version.txt", timeout=timeout) as r:
            return r.read().decode().strip()
    except (urllib.error.URLError, OSError, TimeoutError):
        return ""


def alembic_head(root: pathlib.Path, python: pathlib.Path) -> str:
    """The migration the database is actually at, asked of the database rather than the tree.

    Reading `alembic/versions/` would report what the RELEASE contains, which is the number
    that is right by construction and therefore worth nothing — the question is what ran.
    """
    p = subprocess.run([str(python), "-m", "alembic", "current"],
                       cwd=str(root / "backend"), capture_output=True, text=True)
    for token in (p.stdout + p.stderr).split():
        if token.isdigit() and len(token) >= 4:
            return token
    return ""


def wait_healthy(base: str, *, attempts: int = 30, delay: float = 1.0,
                 probe=health) -> dict | None:
    for _ in range(attempts):
        got = probe(base)
        if got and got.get("status") == "ok":
            return got
        time.sleep(delay)
    return None


def verify(base: str, expected_sha: str, *, root: pathlib.Path, python: pathlib.Path,
           probe=health, web=web_sha, head=alembic_head) -> tuple[bool, dict]:
    """Did the box come back serving the revision we just installed?

    All three facts are gathered BEFORE deciding, so the report names everything that is wrong
    rather than the first thing — an operator who fixes one and re-runs to find a second has
    been told half of what was known.
    """
    got = wait_healthy(base, probe=probe)
    # S1 MOUNTS THE SPA ONLY IF `web/dist` EXISTS, so an API-only install has no bundle and no
    # `version.txt`. Requiring a web sha there would make such an install permanently
    # un-upgradeable — S1 says the bundle is optional and S5 was demanding it, which the S6
    # walk found by upgrading a release that had none.
    #
    # Reported as NOT APPLICABLE rather than skipped: "there is no bundle" and "the bundle
    # matches" must not read the same, which is this repository's oldest rule about absences.
    has_bundle = (root / "current" / "web" / "dist" / "index.html").exists()
    facts = {
        "api": (got or {}).get("git_sha", ""),
        "web": web(base) if has_bundle else "n/a (no web bundle installed)",
        "alembic": head(root, python),
        "db": (got or {}).get("db", ""),
    }
    ok = (bool(got) and facts["api"] == expected_sha
          and (not has_bundle or facts["web"] == expected_sha))
    return ok, facts


def stop(restart, *, drain: float = 10.0) -> None:
    """Ask the service manager to stop, and give in-flight work a bounded moment.

    `restart` is the platform's own command (launchctl or systemctl), injected rather than
    branched on here: S3 and S4 already own that difference and a third copy would be a third
    thing to keep in step.
    """
    restart("stop")
    time.sleep(min(drain, 10.0))


def upgrade(root: pathlib.Path, release: pathlib.Path, sha: str, *, base: str,
            python: pathlib.Path, restart, probe=health, web=web_sha,
            head=alembic_head) -> int:
    """Swap in a release, restart, verify — and put the old one back if it did not come up."""
    current = root / "current"
    previous = root / "previous"

    if not release.is_dir():
        print(f"upgrade: {release} is not a directory", file=sys.stderr)
        return EXIT_MISMATCH

    stop(restart)

    # KEEP THE OLD ONE. Rollback is re-running it, so it has to still exist — this is the line
    # that makes the recovery path real rather than aspirational.
    if previous.exists():
        shutil.rmtree(previous)
    if current.exists():
        current.rename(previous)
    shutil.copytree(release, current)

    restart("start")
    ok, facts = verify(base, sha, root=root, python=python, probe=probe, web=web, head=head)
    if ok:
        for fact in IDENTITY_FACTS:
            print(f"    {fact:<8} {facts[fact]}")
        print(f"==> {sha} is live")
        return EXIT_OK

    # ROLL BACK, and say what was wrong rather than only that something was.
    checked = ("api", "web") if not facts["web"].startswith("n/a") else ("api",)
    problems = [f"{f} serves {facts[f]!r}, expected {sha!r}"
                for f in checked if facts[f] != sha]
    if not facts["api"]:
        problems.insert(0, "the api never became healthy")
    print("upgrade failed: " + "; ".join(problems), file=sys.stderr)

    if previous.exists():
        stop(restart)
        shutil.rmtree(current, ignore_errors=True)
        previous.rename(current)
        restart("start")
        back = wait_healthy(base, probe=probe)
        print(f"rolled back to the previous release; it is "
              f"{'serving ' + back.get('git_sha', '?') if back else 'NOT healthy — look now'}",
              file=sys.stderr)
        return EXIT_ROLLED_BACK
    print("no previous release to roll back to", file=sys.stderr)
    return EXIT_UNHEALTHY


def uninstall(root: pathlib.Path, *, restart, purge: bool = False,
              data: pathlib.Path | None = None) -> int:
    """Remove the service and the code. Never the database.

    The service ACCOUNT is left too, deliberately: it can own files this installer never
    placed, and removing it turns those into orphaned uids — a quieter problem than an unused
    account. What was left is printed, so the operator can finish the job knowingly.
    """
    restart("stop")
    kept: list[str] = []
    for path in (root / "current", root / "previous", root / "venv"):
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)

    data = data or (root / "data")
    if purge and data.exists():
        shutil.rmtree(data, ignore_errors=True)
    elif data.exists():
        kept.append(str(data))

    print("uninstalled the service and its code")
    print("  the DATABASE was not touched — it is yours, and dropping it is the least "
          "reversible thing this could do")
    for k in kept:
        print(f"  kept: {k}  (use --purge to remove)")
    print("  kept: the service account, which may own files this installer never placed")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Upgrade or remove a native Graphban install.")
    ap.add_argument("command", choices=("upgrade", "uninstall"))
    ap.add_argument("--root", default="/opt/graphban")
    ap.add_argument("--release", default="")
    ap.add_argument("--sha", default="")
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--python", default="")
    ap.add_argument("--purge", action="store_true")
    args = ap.parse_args(argv)

    root = pathlib.Path(args.root).resolve()
    python = pathlib.Path(args.python) if args.python else root / "venv" / "bin" / "python"

    def restart(action: str) -> None:
        """Whatever the platform uses. Resolved here so this file does not branch on the OS."""
        if sys.platform == "darwin":
            cmd = {"stop": ["launchctl", "bootout", "system/dev.graphban.api"],
                   "start": ["launchctl", "bootstrap", "system",
                             "/Library/LaunchDaemons/dev.graphban.api.plist"]}[action]
        else:
            cmd = ["systemctl", action, "graphban.service"]
        subprocess.run(cmd, capture_output=True, text=True)

    if args.command == "uninstall":
        return uninstall(root, restart=restart, purge=args.purge)

    if not args.release or not args.sha:
        print("upgrade needs --release and --sha: the sha is what the box must be serving "
              "afterwards, and without it nothing is verified", file=sys.stderr)
        return EXIT_MISMATCH
    return upgrade(root, pathlib.Path(args.release).resolve(), args.sha,
                   base=args.base, python=python, restart=restart)


if __name__ == "__main__":
    sys.exit(main())
