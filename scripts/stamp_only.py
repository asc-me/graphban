#!/usr/bin/env python3
"""Is this change nothing but a release stamp? (CI decides what to run on the answer.)

A stamp (`graphban_release.py stamp`) rewrites one version string in each of three files —
`backend/app/version.py`, `backend/pyproject.toml`, `web/package.json` — and nothing else.
Every path filter in CI fires on it, so a stamp PR paid for both backend suites, the frontend
build and the fleet suite: ~15 minutes of wall clock and a self-hosted slot to test that a
string changed. The wheel install is the one job a stamp can actually break, and it keeps
running.

Decided from the DIFF, never from the branch name or the PR title. `chore/stamp-*` is a
convention, and a convention is a thing a later commit on the same branch can silently
stop honouring. The rule here is strict in the safe direction: anything that is not
precisely a stamp — one more file, one more line, a version that disagrees between the
three, a placeholder, a non-CalVer — answers `false` and the full suites run.

Usage in the workflow:  python3 scripts/stamp_only.py --base <sha> --head <sha>
Writes `stamp_only=true|false` to $GITHUB_OUTPUT when set; prints the reason either way.
"""
from __future__ import annotations

import argparse
import os
import pathlib
import re
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import graphban_release as rel  # noqa: E402

#: The three identity files a stamp writes, each with the regex that matches its version line.
STAMPED = {
    str(rel.VERSION_PY): rel._VERSION_PY_RE,
    str(rel.PYPROJECT): rel._PYPROJECT_VER_RE,
    str(rel.PACKAGE_JSON): rel._PACKAGE_VER_RE,
}
_FILE = re.compile(r"^diff --git a/(\S+) b/(\S+)$")


def stamp_only(diff: str) -> tuple[bool, str]:
    """(is it only a stamp, why). Pure: takes `git diff base head` text."""
    files: dict[str, dict[str, list[str]]] = {}
    current: str | None = None
    for line in diff.splitlines():
        m = _FILE.match(line)
        if m:
            current = m.group(2)
            files[current] = {"-": [], "+": []}
            continue
        if current is None or line.startswith(("+++", "---", "@@", "index ", "\\ ")):
            continue
        if line[:1] in "+-":
            files[current][line[0]].append(line[1:])
    if not files:
        return False, "empty diff"
    extra = sorted(set(files) - set(STAMPED))
    if extra:
        return False, f"touches {', '.join(extra)}"
    missing = sorted(set(STAMPED) - set(files))
    if missing:
        return False, f"a stamp writes all three; {', '.join(missing)} unchanged"
    old: set[str] = set()
    new: set[str] = set()
    for path, hunks in files.items():
        pattern = STAMPED[path]
        if len(hunks["-"]) != 1 or len(hunks["+"]) != 1:
            return False, f"{path}: {len(hunks['-'])} removed / {len(hunks['+'])} added lines, not one each"
        before, after = pattern.search(hunks["-"][0]), pattern.search(hunks["+"][0])
        if not before or not after:
            return False, f"{path}: a changed line is not its version line"
        old.add(before.group(1))
        new.add(after.group(1))
    if len(new) != 1:
        return False, f"the three files disagree on the new version: {sorted(new)}"
    if len(old) != 1:
        return False, f"the three files disagreed on the old version: {sorted(old)}"
    version = next(iter(new))
    if version in rel.PLACEHOLDERS or rel.parse_calver(version) is None:
        return False, f"{version!r} is not a CalVer release version"
    return True, f"stamp {next(iter(old))} -> {version}, nothing else"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--base", required=True)
    ap.add_argument("--head", required=True)
    ap.add_argument("--repo", default=".")
    args = ap.parse_args(argv)
    if not args.base or set(args.base) <= {"0"}:
        ok, why = False, "no base commit to diff against"
    else:
        out = subprocess.run(["git", "-C", args.repo, "diff", f"{args.base}..{args.head}"],
                             capture_output=True, text=True)
        if out.returncode != 0:
            ok, why = False, f"git diff failed: {out.stderr.strip()[:200]}"
        else:
            ok, why = stamp_only(out.stdout)
    print(f"stamp_only={'true' if ok else 'false'}  ({why})")
    gh = os.environ.get("GITHUB_OUTPUT")
    if gh:
        with open(gh, "a", encoding="utf-8") as f:
            f.write(f"stamp_only={'true' if ok else 'false'}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
