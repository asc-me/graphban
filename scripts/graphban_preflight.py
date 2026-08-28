#!/usr/bin/env python3
"""Verify what a native install needs, and refuse clearly when it is missing (GRPH-578).

PRD-27 S2. The installer VERIFIES Postgres and never installs, upgrades or reconfigures one.
The machine this targets very often already runs Postgres — this repository's own server does,
which is why its ports are remapped — and an installer that adopts or restarts somebody else's
database is a support burden and a data risk out of proportion to the convenience.

**Standard library only, and it shells out to `psql`.** Two answers given during the grill
contradicted each other: that this would use psycopg "which the backend already depends on",
and that it is a standalone script with no third-party imports because it runs BEFORE the venv
exists. The second is the binding one — psycopg is not installed at preflight time — so the
check goes through `psql`, and a missing `psql` is its own refusal rather than a traceback.

**Every path either verifies something or refuses.** There is no "nothing to report": a
preflight that could not run its checks must never look like one that passed, which is the
failure this repository names as an absence reading as a clean result. A successful run prints
the versions it found, so it is distinguishable from a run that checked nothing.

**It refuses rather than fixes.** Enabling an extension is a schema change to a database the
operator owns.

    python3 scripts/graphban_preflight.py --database-url postgresql://user@host/db --port 8000
"""
from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
import sys
import urllib.parse
from dataclasses import dataclass, field

# Distinct codes, because collapsing them sends the operator to the wrong place: "install a
# package", "start the server", "run one SQL statement" and "free a port" are four different
# afternoons. 0 is the ONLY success.
EXIT_OK = 0
EXIT_NO_PSQL = 2
EXIT_UNREACHABLE = 3
EXIT_NO_VECTOR = 4
EXIT_VECTOR_NOT_ENABLED = 5
EXIT_PORT_BUSY = 6


@dataclass
class Result:
    """What was checked, and what to do about it.

    `found` exists so a PASS can say what it saw. A preflight that prints nothing on success
    is indistinguishable from one whose checks did not run.
    """

    code: int
    problem: str = ""
    remedy: str = ""
    found: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.code == EXIT_OK


def install_hint() -> str:
    """The pgvector line for this platform, named rather than guessed at generically."""
    if sys.platform == "darwin":
        return "brew install pgvector"
    return ("apt install postgresql-16-pgvector    # or: dnf install pgvector_16\n"
            "    (match the number to your server's major version)")


def dsn_parts(database_url: str) -> dict:
    """Host, port, database and user out of a SQLAlchemy-style URL.

    **The `+psycopg` driver suffix needs no special handling, and saying so is the point.**
    An earlier version stripped it, with a comment explaining that `psql` does not understand
    that scheme — which is true and irrelevant: `psql` never receives the URL. It is handed
    `-h/-p/-U/-d` from the fields below, and `urlsplit` parses a compound scheme correctly.

    The strip was removed after a sabotage that reinstated the raw URL broke nothing. It was
    dead code wearing a justification, which is worse than dead code — the next reader would
    have trusted a guard that had never once fired.
    """
    u = urllib.parse.urlsplit(database_url)
    return {
        "host": u.hostname or "localhost",
        "port": u.port or 5432,
        "database": (u.path or "/postgres").lstrip("/") or "postgres",
        "user": u.username or os.environ.get("USER", "postgres"),
        "password": u.password or "",
        "safe": f"{u.hostname or 'localhost'}:{u.port or 5432}/"
                f"{(u.path or '/postgres').lstrip('/')} as {u.username or 'postgres'}",
    }


def run_psql(parts: dict, sql: str, *, timeout: float = 10.0) -> tuple[int, str]:
    """One `psql -tAc`, returning (returncode, output). Never raises for a failed query."""
    env = dict(os.environ)
    if parts["password"]:
        env["PGPASSWORD"] = parts["password"]
    cmd = ["psql", "-h", str(parts["host"]), "-p", str(parts["port"]),
           "-U", str(parts["user"]), "-d", str(parts["database"]), "-tAc", sql]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)
    return p.returncode, (p.stdout + p.stderr).strip()


def port_free(host: str, port: int) -> bool:
    """Is the API's port available? Checked by binding, not by asking."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def preflight(database_url: str, *, api_port: int = 8000, api_host: str = "127.0.0.1",
              psql=run_psql, have_psql=None, free=port_free) -> Result:
    """Every prerequisite, in the order that makes the failures useful.

    Ordered so the FIRST failure is the one to act on: there is no point reporting a missing
    extension on a server nothing can reach, and no point reporting either on a machine with
    no client to ask with.

    The three collaborators are injected so the decisions are testable without a server, a
    port, or a `psql` — a preflight only ever exercised against a working machine is one whose
    refusals nobody has read.
    """
    parts = dsn_parts(database_url)
    found: list[str] = []

    if (shutil.which("psql") is not None) if have_psql is None else have_psql:
        pass
    else:
        return Result(EXIT_NO_PSQL,
                      "`psql` is not on PATH, so nothing here can talk to Postgres.",
                      "Install the Postgres client:\n    " + (
                          "brew install libpq && brew link --force libpq"
                          if sys.platform == "darwin" else
                          "apt install postgresql-client"))

    rc, out = psql(parts, "select version()")
    if rc != 0:
        return Result(EXIT_UNREACHABLE,
                      f"cannot reach Postgres at {parts['safe']}.",
                      f"Check it is running and reachable:\n"
                      f"    pg_isready -h {parts['host']} -p {parts['port']}\n"
                      f"  psql said: {out.splitlines()[0] if out else '(no output)'}")
    found.append(out.split(" on ")[0] if out else "PostgreSQL (version unreported)")

    rc, out = psql(parts, "select default_version from pg_available_extensions "
                          "where name = 'vector'")
    if rc != 0 or not out.strip():
        return Result(EXIT_NO_VECTOR,
                      "the `vector` extension is not available on this server.",
                      "Install pgvector for your server's major version:\n    " + install_hint())
    found.append(f"pgvector {out.strip()} available")

    rc, out = psql(parts, "select extversion from pg_extension where extname = 'vector'")
    if rc != 0 or not out.strip():
        return Result(EXIT_VECTOR_NOT_ENABLED,
                      f"pgvector is installed but not enabled in `{parts['database']}`.",
                      "Enable it in that database — this installer will not, because it is a "
                      "schema change to a database you own:\n"
                      f"    psql -h {parts['host']} -p {parts['port']} -U {parts['user']} "
                      f"-d {parts['database']} -c 'CREATE EXTENSION vector;'")
    found.append(f"pgvector {out.strip()} enabled in {parts['database']}")

    if not free(api_host, api_port):
        return Result(EXIT_PORT_BUSY,
                      f"port {api_port} on {api_host} is already in use.",
                      f"Free it, or choose another with PORT=... :\n"
                      f"    lsof -nP -iTCP:{api_port} -sTCP:LISTEN")
    found.append(f"port {api_port} free")

    return Result(EXIT_OK, found=found)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Verify what a native Graphban install needs.")
    ap.add_argument("--database-url", default=os.environ.get("DATABASE_URL", ""))
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    ap.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    args = ap.parse_args(argv)

    if not args.database_url:
        # Not a check that passed — a check that could not run. Same class as everything else
        # here: the quiet reading must never be the reassuring one.
        print("preflight: no DATABASE_URL given and none in the environment, so nothing was "
              "verified.\n  Pass --database-url, or set it before running.", file=sys.stderr)
        return EXIT_UNREACHABLE

    result = preflight(args.database_url, api_port=args.port, api_host=args.host)
    if result.ok:
        print("preflight: ready")
        for line in result.found:
            print(f"  ok  {line}")
        return EXIT_OK

    print(f"preflight: {result.problem}", file=sys.stderr)
    print(f"  {result.remedy}", file=sys.stderr)
    for line in result.found:
        print(f"  (ok  {line})", file=sys.stderr)
    return result.code


if __name__ == "__main__":
    sys.exit(main())
