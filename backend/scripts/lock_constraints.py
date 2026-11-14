#!/usr/bin/env python3
"""Hold a shipped install to `backend/uv.lock` (GRPH-1615).

`uv.lock` is what pip-audit scans (scripts/scan_ci.py exports it), so it is the set the
security gate calls clean. Neither shipped install read it: the Dockerfile ran a bare
`pip install .` and the native host ran `pip install -e backend` into an existing venv, where
pip keeps any dependency that is already satisfied. A CVE pin landed in the lock (dnspython
2.9.2) and reached no box — the scan read a file nothing installed from.

This file closes that from both ends, stdlib only, because the native host runs it before
the venv has anything in it and the Docker build context has no `uv`:

    python lock_constraints.py write uv.lock constraints.txt   # lock -> pip `-c` file
    python lock_constraints.py check uv.lock [--strict]        # installed == lock, or exit 1

`write` pins every registry package in the lock to its locked version. Under `pip install -c`
an installed version that disagrees is not "already satisfied", so pip moves it to the lock.
`check` runs in the target interpreter after the install and refuses any locked package whose
installed version differs — the install is held to the lock, not just pointed at it.
"""
from __future__ import annotations

import pathlib
import re
import sys
import tomllib

# Installed by the interpreter or venv itself, never by the lock. Anything else installed and
# absent from the lock is reported: it is a package the scan never saw.
BOOTSTRAP = {"pip", "setuptools", "wheel"}


def normalize(name: str) -> str:
    """PEP 503: the lock writes `email-validator`; a dist may call itself `email_validator`."""
    return re.sub(r"[-_.]+", "-", name).lower()


def locked(lock: pathlib.Path) -> dict[str, str]:
    """Registry packages in the lock, `{normalized name: version}`.

    The project itself (`source = { editable = "." }`) is skipped: it is what is being
    installed, and its version is the release stamp, not a pin.
    """
    data = tomllib.loads(lock.read_text(encoding="utf-8"))
    out: dict[str, str] = {}
    for pkg in data.get("package", []):
        if "registry" not in pkg.get("source", {}):
            continue
        name = normalize(pkg["name"])
        if name in out and out[name] != pkg["version"]:
            # A lock that forks one package by marker cannot be a flat constraints file.
            raise SystemExit(f"lock_constraints: {name} is locked at both "
                             f"{out[name]} and {pkg['version']}")
        out[name] = pkg["version"]
    if not out:
        raise SystemExit(f"lock_constraints: {lock} pins no registry packages")
    return out


def constraints(lock: pathlib.Path) -> str:
    pins = locked(lock)
    return "".join(f"{name}=={pins[name]}\n" for name in sorted(pins))


def write(lock: pathlib.Path, out: pathlib.Path) -> pathlib.Path:
    out.write_text(constraints(lock), encoding="utf-8")
    return out


def installed() -> dict[str, str]:
    from importlib import metadata

    return {normalize(d.metadata["Name"]): d.version
            for d in metadata.distributions() if d.metadata["Name"]}


def drift(lock: pathlib.Path, have: dict[str, str], *, strict: bool = False,
          project: str = "graphban-api") -> list[str]:
    """Every way `have` disagrees with the lock. Empty is the only pass.

    A locked package that is not installed is fine — the lock also carries the `dev` and
    `cloud` extras and platform-only wheels. An installed one at another version is not.
    `strict` also refuses installed packages the lock does not name: right for a fresh image,
    wrong for a native venv that keeps a dependency an older release dropped.
    """
    pins = locked(lock)
    out = []
    for name, version in sorted(have.items()):
        if name in pins:
            if version != pins[name]:
                out.append(f"{name} {version} installed, uv.lock pins {pins[name]}")
        elif strict and name not in BOOTSTRAP and name != normalize(project):
            out.append(f"{name} {version} installed, not in uv.lock (the scan never saw it)")
    return out


def main(argv: list[str]) -> int:
    if len(argv) == 3 and argv[0] == "write":
        write(pathlib.Path(argv[1]), pathlib.Path(argv[2]))
        return 0
    if len(argv) in (2, 3) and argv[0] == "check" and argv[2:] in ([], ["--strict"]):
        bad = drift(pathlib.Path(argv[1]), installed(), strict=argv[2:] == ["--strict"])
        for line in bad:
            print(f"lock drift: {line}", file=sys.stderr)
        if bad:
            return 1
        print(f"installed packages match {argv[1]}")
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
