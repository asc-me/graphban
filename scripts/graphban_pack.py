#!/usr/bin/env python3
"""Build the directory a native install can swap in (P32 / PRD-27).

A Graphban *release* is not a git tag and is not GitHub's source zip. Native
`graphban_host.py upgrade --release ./new --sha <rev>` copytree's a directory of:

- backend + Alembic
- prebuilt `web/dist`, or an explicit `SPA` file saying `n/a`
- a `GIT_SHA` file (short sha of the packed commit)
- **no** `.env`

Packing a ref uses a detached worktree, for the same reason `deploy.sh` does:
rsync/copytree of a working tree will ship whatever is on disk, including an
agent's uncommitted files and whoever's `.env`.

    python3 scripts/graphban_pack.py 2026.09.1
    python3 scripts/graphban_pack.py --from ./tree --sha abc1234 --out ./dist-release

This does not upload to GitHub and does not run the swap. Attach the tarball
with `gh release upload <tag> dist-release/graphban-<ver>.tar.gz`.
"""
from __future__ import annotations

import argparse
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile

PLACEHOLDER_VERSIONS = frozenset({"", "unknown", "0.1.0"})
HOST_SCRIPTS = (
    "graphban_host.py",
    "graphban_upgrade.py",
    "graphban_preflight.py",
    "graphban_service.py",
    "graphban_systemd.py",
)
BACKEND_DROP = frozenset({
    ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", ".mine", "tests",
    "Dockerfile", "railway.json", ".dockerignore",
})
_VERSION_RE = re.compile(r'^__version__\s*=\s*"([^"]+)"', re.M)


def is_env_secret(name: str) -> bool:
    """`.env` and `.env.local` etc. `.env.example` is the template, not a secret."""
    return name == ".env" or (name.startswith(".env.") and name != ".env.example")


def read_version(src: pathlib.Path) -> str:
    path = src / "backend" / "app" / "version.py"
    if not path.is_file():
        return ""
    m = _VERSION_RE.search(path.read_text(encoding="utf-8"))
    return m.group(1).strip() if m else ""


def detect_sha(src: pathlib.Path, explicit: str = "") -> str:
    if explicit:
        return explicit.strip()
    p = subprocess.run(["git", "-C", str(src), "rev-parse", "--short", "HEAD"],
                       capture_output=True, text=True)
    if p.returncode == 0 and p.stdout.strip():
        return p.stdout.strip()
    sha_file = src / "GIT_SHA"
    if sha_file.is_file():
        return sha_file.read_text(encoding="utf-8").strip()
    return ""


def _backend_ignore(directory: str, names: list[str]) -> list[str]:
    return [n for n in names
            if n in BACKEND_DROP or n.endswith(".pyc") or is_env_secret(n)
            or n.startswith(".pytest") or n.endswith(".db") or n.endswith(".db.lock")]


def env_files(tree: pathlib.Path) -> list[pathlib.Path]:
    found: list[pathlib.Path] = []
    for dirpath, dirnames, filenames in os.walk(tree):
        dirnames[:] = [d for d in dirnames if d not in {".git"}]
        for name in filenames:
            if is_env_secret(name):
                found.append(pathlib.Path(dirpath) / name)
    return found


def build_web(src: pathlib.Path, sha: str) -> int:
    """`pnpm build` then stamp `version.txt`. The installer does not build."""
    web = src / "web"
    if not (web / "package.json").is_file():
        print("pack: no web/package.json — pass --api-only for an API-only cut",
              file=sys.stderr)
        return 1
    if not (web / "node_modules").is_dir():
        inst = subprocess.run(["pnpm", "install", "--frozen-lockfile"], cwd=str(web))
        if inst.returncode != 0:
            return inst.returncode
    built = subprocess.run(["pnpm", "build"], cwd=str(web))
    if built.returncode != 0:
        return built.returncode
    dist = web / "dist"
    if not (dist / "index.html").is_file():
        print("pack: pnpm build did not produce web/dist/index.html", file=sys.stderr)
        return 1
    (dist / "version.txt").write_text(sha + "\n", encoding="utf-8")
    return 0


def materialize(ref: str, repo: pathlib.Path) -> pathlib.Path:
    """Detached worktree of `ref`. Caller must `dematerialize`."""
    dest = pathlib.Path(tempfile.mkdtemp(prefix="graphban-pack-"))
    dest.rmdir()
    p = subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "--detach", str(dest), ref],
        capture_output=True, text=True,
    )
    if p.returncode != 0:
        shutil.rmtree(dest, ignore_errors=True)
        print(f"pack: could not check out {ref!r}:\n{(p.stderr or p.stdout).strip()}",
              file=sys.stderr)
        raise RuntimeError("worktree")
    return dest


def dematerialize(repo: pathlib.Path, tree: pathlib.Path) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "remove", "--force", str(tree)],
        capture_output=True, text=True,
    )
    shutil.rmtree(tree, ignore_errors=True)


def _fail(dest: pathlib.Path | None, msg: str) -> tuple[int, None]:
    if dest is not None and dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    print(f"pack: {msg}", file=sys.stderr)
    return 1, None


def pack(src: pathlib.Path, out: pathlib.Path, *, sha: str, version: str,
         api_only: bool = False, build=build_web) -> tuple[int, pathlib.Path | None]:
    """Lay out `out/graphban-<version>/` and tar it. `build` is injectable so the
    CALL tests can pin the web step without pnpm.
    """
    src = src.resolve()
    sha = (sha or "").strip()
    version = (version or "").strip()
    if version in PLACEHOLDER_VERSIONS:
        return _fail(None, "refusing a placeholder version "
                     f"{version or '(empty)'} — not a release")
    if not sha or sha == "unknown":
        return _fail(None, "no sha — /health would report unknown and an upgrade "
                     "could never verify")
    if not (src / "backend" / "pyproject.toml").is_file():
        return _fail(None, f"{src}/backend/pyproject.toml is missing")
    alembic = src / "backend" / "alembic"
    versions = alembic / "versions"
    if not (src / "backend" / "alembic.ini").is_file() or not versions.is_dir():
        return _fail(None, "backend Alembic tree is missing")
    if not any(versions.glob("*.py")):
        return _fail(None, "backend/alembic/versions has no migrations")
    if not (src / "LICENSE.md").is_file():
        return _fail(None, "LICENSE.md is missing")

    dest = out / f"graphban-{version}"
    if dest.exists():
        return _fail(None, f"{dest} already exists")
    dest.mkdir(parents=True)

    shutil.copytree(src / "backend", dest / "backend", ignore=_backend_ignore)
    shutil.copy2(src / "LICENSE.md", dest / "LICENSE.md")
    example = src / ".env.example"
    if example.is_file():
        shutil.copy2(example, dest / ".env.example")
    scripts_src = src / "scripts"
    scripts_dest = dest / "scripts"
    scripts_dest.mkdir()
    for name in HOST_SCRIPTS:
        path = scripts_src / name
        if not path.is_file():
            return _fail(dest, f"scripts/{name} is missing — an unpacked "
                         "release could not install or upgrade")
        shutil.copy2(path, scripts_dest / name)

    (dest / "GIT_SHA").write_text(sha + "\n", encoding="utf-8")

    if api_only:
        (dest / "SPA").write_text("n/a\n", encoding="utf-8")
    else:
        rc = build(src, sha)
        if rc != 0:
            return _fail(dest, "web build failed — not shipping an implicit API-only cut. "
                         "Pass --api-only if that is the intent")
        dist_src = src / "web" / "dist"
        if not (dist_src / "index.html").is_file():
            return _fail(dest, "web/dist/index.html is missing after the build")
        dist_dest = dest / "web" / "dist"
        shutil.copytree(dist_src, dist_dest, ignore=shutil.ignore_patterns(
            ".DS_Store", "__pycache__"))
        (dist_dest / "version.txt").write_text(sha + "\n", encoding="utf-8")
        (dest / "SPA").write_text("present\n", encoding="utf-8")

    leaked = env_files(dest)
    if leaked:
        rel = ", ".join(str(p.relative_to(dest)) for p in leaked)
        return _fail(dest, f"refusing to ship secrets: {rel}")

    tarball = out / f"graphban-{version}.tar.gz"
    with tarfile.open(tarball, "w:gz") as tf:
        tf.add(dest, arcname=dest.name)
    print(f"packed {dest}")
    print(f"tarball {tarball}")
    return 0, dest


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Pack a native Graphban release directory (backend, web/dist, "
                    "GIT_SHA, no .env).")
    ap.add_argument("ref", nargs="?", default="",
                    help="git ref to pack from a detached worktree")
    ap.add_argument("--from", dest="src", default="",
                    help="existing tree; skips the worktree (tests / already-exported)")
    ap.add_argument("--out", default="dist-release",
                    help="directory that will contain graphban-<version>/ and the tarball")
    ap.add_argument("--sha", default="")
    ap.add_argument("--api-only", action="store_true",
                    help="do not build the SPA; write SPA n/a instead of omitting it")
    args = ap.parse_args(argv)

    if not args.src and not args.ref:
        print("pack: need a ref (python3 scripts/graphban_pack.py 2026.09.1) "
              "or --from a tree", file=sys.stderr)
        return 2

    worktree: pathlib.Path | None = None
    repo: pathlib.Path | None = None
    try:
        if args.src:
            src = pathlib.Path(args.src).resolve()
        else:
            top = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                                 capture_output=True, text=True)
            if top.returncode != 0:
                print("pack: not a git checkout; pass --from", file=sys.stderr)
                return 2
            repo = pathlib.Path(top.stdout.strip())
            try:
                worktree = materialize(args.ref, repo)
            except RuntimeError:
                return 1
            src = worktree

        version = read_version(src)
        sha = detect_sha(src, args.sha)
        out = pathlib.Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        rc, _ = pack(src, out, sha=sha, version=version, api_only=args.api_only)
        return rc
    finally:
        if worktree is not None and repo is not None:
            dematerialize(repo, worktree)


if __name__ == "__main__":
    sys.exit(main())
