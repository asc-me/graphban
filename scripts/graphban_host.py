#!/usr/bin/env python3
"""The command PRD-27 named: install, upgrade and uninstall a native Graphban (GRPH-601).

S1–S6 built the parts. This is the operator-facing composition. The four platform scripts
remain the owners of their own units; this file is the thing that produces a *running
install* from a release directory, and the thing that upgrades one without throwing away
the operator's `.env` or leaving the venv on yesterday's packages.

**This is not `graphban`.** The backend console-script already owns that name (`app.cli`,
code-graph sync). This file is stdlib-only and runs *before* the venv exists, which is the
constraint the PRD already picked — it cannot import `app`.

    python3 scripts/graphban_host.py install --root /opt/graphban --from .
    python3 scripts/graphban_host.py upgrade --root /opt/graphban --release ./new --sha abc1234
    python3 scripts/graphban_host.py uninstall --root /opt/graphban

`--user-domain` / `--user-scope` exist so the mechanism can be proven without root. The
shipped service is still a LaunchDaemon / system unit.
"""
from __future__ import annotations

import argparse
import getpass
import pathlib
import plistlib
import shutil
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import graphban_preflight as pf  # noqa: E402
import graphban_service as gs  # noqa: E402
import graphban_systemd as gsd  # noqa: E402
import graphban_upgrade as up  # noqa: E402

IGNORE = shutil.ignore_patterns(
    ".git", ".venv", "venv", "node_modules", "__pycache__",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", ".mine", "*.pyc",
)


def find_env(src: pathlib.Path) -> pathlib.Path | None:
    for path in (src / "backend" / ".env", src / ".env"):
        if path.is_file():
            return path
    return None


def load_env(path: pathlib.Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def detect_sha(src: pathlib.Path, explicit: str) -> str:
    if explicit:
        return explicit
    p = subprocess.run(["git", "-C", str(src), "rev-parse", "--short", "HEAD"],
                       capture_output=True, text=True)
    sha = p.stdout.strip()
    return sha if p.returncode == 0 and sha else ""


def place_release(src: pathlib.Path, dest: pathlib.Path) -> None:
    shutil.copytree(src, dest, ignore=IGNORE)


def ensure_venv(root: pathlib.Path, backend: pathlib.Path, *,
                runner=subprocess.run) -> tuple[int, pathlib.Path | None]:
    """Create `root/venv` if needed and install the backend into it.

    The venv lives OUTSIDE the swapped release (see test_install_layout.py). A release that
    needs new packages still has to be installed into that venv — leaving it untouched is
    the gap the S6 walk named and did not close.
    """
    python = root / "venv" / "bin" / "python"
    if not python.exists():
        p = runner([sys.executable, "-m", "venv", str(root / "venv")],
                   capture_output=True, text=True)
        if p.returncode != 0:
            print(f"install: could not create the venv:\n{p.stderr or p.stdout}",
                  file=sys.stderr)
            return 1, None
    p = runner([str(python), "-m", "pip", "install", "-e", str(backend)],
               capture_output=True, text=True)
    if p.returncode != 0:
        print(f"install: pip install failed:\n{p.stderr or p.stdout}", file=sys.stderr)
        return 1, None
    return 0, python


def write_service(*, platform: str, root: pathlib.Path, python: pathlib.Path,
                  user: str, port: int, host: str, git_sha: str,
                  user_scope: bool, logs: pathlib.Path | None = None):
    """Render the platform unit carrying the revision the box must serve.

    `GIT_SHA` has to be a real environment variable for the same reason `PORT` does:
    `/health` reports `settings.resolved_git_sha`, and an upgrade that cannot read the
    sha it just installed will either pass on `unknown` or roll back a healthy box.
    """
    if platform == "darwin":
        data = gs.plist_dict(root=root, python=python, user=user, port=port, host=host,
                             git_sha=git_sha, user_domain=user_scope, logs=logs)
        return gs.plist_path(user_domain=user_scope), gs.render(data), "plist"
    text = gsd.unit_text(root=root, python=python, user=user, port=port, host=host,
                         git_sha=git_sha, user_scope=user_scope)
    return gsd.unit_path(user_scope=user_scope), text, "unit"


def install_service(kind: str, path: pathlib.Path, payload, *, user_scope: bool) -> int:
    if kind == "plist":
        leaked = gs.secrets_in(plistlib.loads(payload))
        if leaked:
            print(f"refusing: the plist would carry {', '.join(leaked)}", file=sys.stderr)
            return 2
        return gs.install(path, payload, user_domain=user_scope)
    leaked = gsd.secrets_in(payload)
    if leaked:
        print(f"refusing: the unit would carry {leaked}", file=sys.stderr)
        return 2
    return gsd.install(path, payload, user_scope=user_scope)


def rewire(*, platform: str, root: pathlib.Path, python: pathlib.Path, user: str,
           port: int, host: str, git_sha: str, user_scope: bool,
           logs: pathlib.Path | None = None) -> None:
    """Rewrite the unit so the *next* start carries this release's sha."""
    path, payload, kind = write_service(
        platform=platform, root=root, python=python, user=user, port=port, host=host,
        git_sha=git_sha, user_scope=user_scope, logs=logs)
    path.parent.mkdir(parents=True, exist_ok=True)
    if kind == "plist":
        path.write_bytes(payload)
    else:
        path.write_text(payload, encoding="utf-8")
    path.chmod(0o644)


def install(src: pathlib.Path, root: pathlib.Path, sha: str, *,
            port: int, host: str, user: str, user_scope: bool,
            platform: str, logs: pathlib.Path | None = None,
            preflight=pf.main, venv=ensure_venv, service=install_service) -> int:
    """First install. Refuses when the root is already occupied or `.env` is missing."""
    if (root / "current").exists():
        print(f"install: {root / 'current'} already exists — this is not an upgrade.\n"
              f"  Use `graphban_host.py upgrade --root {root} --release … --sha …`",
              file=sys.stderr)
        return 1

    env_src = find_env(src)
    if env_src is None:
        print("install: no .env found (looked at backend/.env and .env).\n"
              "  Copy .env.example, set DATABASE_URL to the Postgres this box already "
              "runs, and set JWT_SECRET. This installer will not generate a secret — "
              "a service that starts with one nobody recorded is a trap.",
              file=sys.stderr)
        return 2

    env = load_env(env_src)
    dsn = env.get("DATABASE_URL") or env.get("database_url") or ""
    if not dsn:
        print("install: .env has no DATABASE_URL, so preflight cannot run.\n"
              "  Native installs do not create Postgres; they verify one.",
              file=sys.stderr)
        return 2

    rc = preflight(["--database-url", dsn, "--port", str(port), "--host", host])
    if rc != 0:
        return rc

    if not sha:
        print("install: no --sha and `git rev-parse` did not produce one, so /health "
              "would report unknown and an upgrade could never verify",
              file=sys.stderr)
        return 1

    place_release(src, root / "current")
    dest_env = root / "current" / "backend" / ".env"
    dest_env.parent.mkdir(parents=True, exist_ok=True)
    if dest_env.resolve() != env_src.resolve():
        shutil.copy2(env_src, dest_env)
    (root / "current" / "GIT_SHA").write_text(sha + "\n", encoding="utf-8")

    erc, python = venv(root, root / "current" / "backend")
    if erc != 0 or python is None:
        shutil.rmtree(root / "current", ignore_errors=True)
        return 1

    path, payload, kind = write_service(
        platform=platform, root=root, python=python, user=user, port=port, host=host,
        git_sha=sha, user_scope=user_scope, logs=logs)
    rc = service(kind, path, payload, user_scope=user_scope)
    if rc != 0:
        shutil.rmtree(root / "current", ignore_errors=True)
    return rc


def host_upgrade(root: pathlib.Path, release: pathlib.Path, sha: str, *,
                 base: str, port: int, host: str, user: str, user_scope: bool,
                 platform: str, logs: pathlib.Path | None = None,
                 venv=ensure_venv) -> int:
    python = root / "venv" / "bin" / "python"

    def sync_deps(r: pathlib.Path, current: pathlib.Path) -> int:
        rc, _ = venv(r, current / "backend")
        return rc

    def do_rewire(git_sha: str) -> None:
        rewire(platform=platform, root=root, python=python, user=user, port=port,
               host=host, git_sha=git_sha, user_scope=user_scope, logs=logs)

    return up.upgrade(
        root, release, sha, base=base, python=python,
        restart=up.platform_restart(user_scope=user_scope, platform=platform),
        rewire=do_rewire, sync_deps=sync_deps,
    )


def host_uninstall(root: pathlib.Path, *, user_scope: bool, platform: str,
                   purge: bool = False) -> int:
    if platform == "darwin":
        gs.uninstall(gs.plist_path(user_domain=user_scope), user_domain=user_scope)
    else:
        gsd.uninstall(gsd.unit_path(user_scope=user_scope), user_scope=user_scope)
    return up.uninstall(
        root, restart=up.platform_restart(user_scope=user_scope, platform=platform),
        purge=purge,
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Install, upgrade or remove a native Graphban (no Docker).")
    ap.add_argument("command", choices=("preflight", "install", "upgrade",
                                        "uninstall", "status"))
    ap.add_argument("--root", default="/opt/graphban")
    ap.add_argument("--from", dest="src", default=".",
                    help="release directory to install from (a checkout or unpacked tarball)")
    ap.add_argument("--release", default="", help="incoming release for upgrade")
    ap.add_argument("--sha", default="")
    ap.add_argument("--base", default="")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--bind", default="127.0.0.1")
    ap.add_argument("--user", default="")
    ap.add_argument("--logs", default="")
    ap.add_argument("--purge", action="store_true")
    ap.add_argument("--user-domain", action="store_true",
                    help="macOS: LaunchAgent in the user domain (verification without root)")
    ap.add_argument("--user-scope", action="store_true",
                    help="Linux: systemd --user unit (verification without root)")
    args, rest = ap.parse_known_args(argv)

    if args.command == "preflight":
        return pf.main(rest if rest else None)

    root = pathlib.Path(args.root).resolve()
    user_scope = args.user_domain or args.user_scope
    platform = sys.platform
    user = args.user or (getpass.getuser() if user_scope else "graphban")
    logs = pathlib.Path(args.logs) if args.logs else None
    base = args.base or f"http://{args.bind}:{args.port}"

    if args.command == "status":
        if platform == "darwin":
            return gs.status()
        print("active" if gsd.is_active(user_scope=user_scope) else "not active")
        return 0 if gsd.is_active(user_scope=user_scope) else 1

    if args.command == "uninstall":
        return host_uninstall(root, user_scope=user_scope, platform=platform,
                              purge=args.purge)

    if args.command == "upgrade":
        if not args.release or not args.sha:
            print("upgrade needs --release and --sha", file=sys.stderr)
            return up.EXIT_MISMATCH
        return host_upgrade(
            root, pathlib.Path(args.release).resolve(), args.sha,
            base=base, port=args.port, host=args.bind, user=user,
            user_scope=user_scope, platform=platform, logs=logs,
        )

    src = pathlib.Path(args.src).resolve()
    sha = detect_sha(src, args.sha)
    return install(
        src, root, sha, port=args.port, host=args.bind, user=user,
        user_scope=user_scope, platform=platform, logs=logs,
    )


if __name__ == "__main__":
    sys.exit(main())
