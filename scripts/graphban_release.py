#!/usr/bin/env python3
"""Stamp, pack, and publish a Graphban CalVer cut.

A release is a named `YYYY.MM.N` on main, an annotated tag of that name, and a
GitHub Release whose asset is `graphban-<tag>.tar.gz`. GitHub's source zip is
not a release. Settings → Updates reads `/releases/latest` and Install applies
that tag — compose via the host helper, native via `graphban_host.py upgrade`.

    python3 scripts/graphban_release.py next
    python3 scripts/graphban_release.py stamp          # writes the three version files
    python3 scripts/graphban_release.py notes          # merges since the previous CalVer
    python3 scripts/graphban_release.py publish        # after the stamp is on origin/main

Does not merge to main, does not apply to a box (Install is the operator gate),
and does not stamp fleet (that distribution stays 0.1.0 until it has its own cut).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import re
import subprocess
import sys

CALVER_RE = re.compile(r"^(\d{4})\.(\d{2})\.(\d+)$")
PLACEHOLDERS = frozenset({"", "unknown", "0.1.0"})
VERSION_PY = pathlib.Path("backend/app/version.py")
PYPROJECT = pathlib.Path("backend/pyproject.toml")
PACKAGE_JSON = pathlib.Path("web/package.json")
FLEET_PYPROJECT = pathlib.Path("fleet/pyproject.toml")
PACK_SCRIPT = pathlib.Path(__file__).resolve().parent / "graphban_pack.py"
_VERSION_PY_RE = re.compile(r'^__version__\s*=\s*"([^"]+)"', re.M)
_PYPROJECT_VER_RE = re.compile(r'^version\s*=\s*"([^"]+)"', re.M)
_PACKAGE_VER_RE = re.compile(r'"version"\s*:\s*"([^"]+)"')
_V_PREFIX = re.compile(r"^v", re.I)


def asset_name(version: str) -> str:
    return f"graphban-{version}.tar.gz"


def parse_calver(raw: str) -> tuple[int, int, int] | None:
    m = CALVER_RE.fullmatch((raw or "").strip())
    if not m:
        return None
    year, month, n = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if month < 1 or month > 12 or n < 1:
        return None
    return year, month, n


def format_calver(year: int, month: int, n: int) -> str:
    return f"{year}.{month:02d}.{n}"


def normalize_tag(raw: str) -> str:
    return _V_PREFIX.sub("", (raw or "").strip())


def read_stamped(repo: pathlib.Path) -> str:
    path = repo / VERSION_PY
    if not path.is_file():
        return ""
    m = _VERSION_PY_RE.search(path.read_text(encoding="utf-8"))
    return m.group(1).strip() if m else ""


def read_stamped_at(repo: pathlib.Path, ref: str, *, git_run=None) -> str:
    """Version.py at `ref`, not cwd — packing cwd is the deploy.sh incident."""
    run = git_run or git
    proc = run(repo, "show", f"{ref}:{VERSION_PY.as_posix()}", check=False)
    if proc.returncode != 0:
        return ""
    m = _VERSION_PY_RE.search(proc.stdout or "")
    return m.group(1).strip() if m else ""


def git(repo: pathlib.Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed: {(proc.stderr or proc.stdout).strip()}"
        )
    return proc


def existing_tags(repo: pathlib.Path, *, git_run=None) -> set[str]:
    run = git_run or git
    proc = run(repo, "tag", "--list", "20*", check=False)
    tags = {normalize_tag(t) for t in (proc.stdout or "").splitlines() if t.strip()}
    remote = run(repo, "ls-remote", "--tags", "origin", "20*", check=False)
    for line in (remote.stdout or "").splitlines():
        #  <sha>\trefs/tags/2026.09.4  or  refs/tags/2026.09.4^{}
        if "refs/tags/" not in line:
            continue
        name = line.split("refs/tags/", 1)[1].strip()
        if name.endswith("^{}"):
            name = name[:-3]
        tags.add(normalize_tag(name))
    return {t for t in tags if parse_calver(t)}


def next_calver(current: str, today: dt.date, tags: set[str]) -> str:
    parsed = parse_calver(current)
    if parsed is None or current in PLACEHOLDERS:
        candidate = format_calver(today.year, today.month, 1)
    else:
        year, month, n = parsed
        if (today.year, today.month) > (year, month):
            candidate = format_calver(today.year, today.month, 1)
        else:
            candidate = format_calver(year, month, n + 1)
    while candidate in tags:
        y, m, n = parse_calver(candidate)  # type: ignore[misc]
        candidate = format_calver(y, m, n + 1)
    return candidate


def ordinal(n: int) -> str:
    if 10 <= (n % 100) <= 20:
        suf = "th"
    else:
        suf = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suf}"


def commit_message(version: str, previous: str) -> str:
    parsed = parse_calver(version)
    n = parsed[2] if parsed else 1
    prev = previous if previous and previous not in PLACEHOLDERS else "the previous cut"
    return (
        f"Stamp product version {version}\n\n"
        f"{ordinal(n).capitalize()} CalVer cut of the month so a box still on {prev} "
        f"can Install from Settings. git_sha remains identity. Fleet stays 0.1.0.\n"
    )


def _fail(msg: str) -> int:
    print(f"release: {msg}", file=sys.stderr)
    return 1


def _replace_one(path: pathlib.Path, pattern: re.Pattern[str], repl: str, *, label: str) -> str:
    text = path.read_text(encoding="utf-8")
    new, count = pattern.subn(repl, text, count=1)
    if count != 1:
        raise ValueError(f"{path} had {count} {label} replacements, want 1")
    path.write_text(new, encoding="utf-8")
    return new


def stamp(repo: pathlib.Path, version: str) -> list[pathlib.Path]:
    """Write the three identity files. Does not commit, push, tag, or touch fleet."""
    if version in PLACEHOLDERS:
        raise ValueError(f"refusing placeholder {version!r}")
    parsed = parse_calver(version)
    if parsed is None:
        raise ValueError(f"not CalVer YYYY.MM.N: {version!r} (no v prefix)")
    current = read_stamped(repo)
    cur_parsed = parse_calver(current)
    if cur_parsed is not None and parsed <= cur_parsed:
        raise ValueError(f"{version} is not after the stamped {current}")
    fleet_before = (repo / FLEET_PYPROJECT).read_text(encoding="utf-8") \
        if (repo / FLEET_PYPROJECT).is_file() else None

    _replace_one(repo / VERSION_PY, _VERSION_PY_RE, f'__version__ = "{version}"',
                 label="__version__")
    _replace_one(repo / PYPROJECT, _PYPROJECT_VER_RE, f'version = "{version}"',
                 label="pyproject version")
    _replace_one(repo / PACKAGE_JSON, _PACKAGE_VER_RE, f'"version": "{version}"',
                 label="package.json version")

    if fleet_before is not None:
        fleet_after = (repo / FLEET_PYPROJECT).read_text(encoding="utf-8")
        if fleet_after != fleet_before:
            raise ValueError("stamp changed fleet — fleet stays 0.1.0")
    written = [VERSION_PY, PYPROJECT, PACKAGE_JSON]
    for rel in written:
        got = {
            VERSION_PY: _VERSION_PY_RE,
            PYPROJECT: _PYPROJECT_VER_RE,
            PACKAGE_JSON: _PACKAGE_VER_RE,
        }[rel].search((repo / rel).read_text(encoding="utf-8"))
        if not got or got.group(1) != version:
            raise ValueError(f"{rel} did not stamp {version}")
    return written


_MERGE_PR = re.compile(r"^Merge pull request #(\d+)\b")
_FIELD_SEP = "\x1f"
_RECORD_SEP = "\x1e"


def previous_tag(version: str, tags: set[str]) -> str | None:
    """Highest CalVer tag strictly before `version`. None is first cut, not empty."""
    parsed = parse_calver(version)
    if parsed is None:
        return None
    earlier = []
    for t in tags:
        p = parse_calver(t)
        if p is not None and p < parsed:
            earlier.append((p, t))
    if not earlier:
        return None
    earlier.sort()
    return earlier[-1][1]


def _is_stamp_merge(subject: str, title: str) -> bool:
    """The stamp PR is this cut, not a change in it."""
    s, t = subject.lower(), title.lower()
    return (
        t.startswith("stamp:")
        or t.startswith("stamp product version")
        or "chore/stamp-" in s
    )


def parse_merge_note(subject: str, body: str) -> tuple[str, str] | None:
    """(pr_number, title) from a GitHub merge commit. None = omit (the stamp)."""
    subject = (subject or "").strip()
    title = ""
    for line in (body or "").splitlines():
        line = line.strip()
        if line:
            title = line
            break
    if not title:
        m_from = re.search(r" from \S+/(\S+)\s*$", subject)
        title = m_from.group(1).replace("-", " ") if m_from else subject
    if not title or _is_stamp_merge(subject, title):
        return None
    m = _MERGE_PR.match(subject)
    pr = m.group(1) if m else ""
    return pr, title


def list_merges(repo: pathlib.Path, previous: str, until: str, *,
                git_run=None) -> list[tuple[str, str]] | None:
    """First-parent merges in `previous..until`, oldest first.

    None means the log could not run (missing tag, etc.) — not an empty cut.
    """
    run = git_run or git
    proc = run(
        repo, "log", "--merges", "--first-parent", "--reverse",
        f"--format=%s{_FIELD_SEP}%b{_RECORD_SEP}",
        f"{previous}..{until}",
        check=False,
    )
    if proc.returncode != 0:
        return None
    out: list[tuple[str, str]] = []
    for rec in (proc.stdout or "").split(_RECORD_SEP):
        rec = rec.strip("\n")
        if not rec.strip():
            continue
        subject, _, body = rec.partition(_FIELD_SEP)
        note = parse_merge_note(subject, body)
        if note is not None:
            out.append(note)
    return out


def changes_section(previous: str | None,
                    changes: list[tuple[str, str]] | None) -> str:
    if previous is None:
        return (
            "**Since last release**\n"
            "No previous CalVer tag — this is the first named cut, not an empty changelog.\n"
        )
    if changes is None:
        return (
            f"**Since {previous}**\n"
            f"Could not list first-parent merges since `{previous}`. "
            "The interval is unmeasured, not empty.\n"
        )
    if not changes:
        return (
            f"**Since {previous}**\n"
            f"No merges on first-parent between `{previous}` and this cut.\n"
        )
    lines = [f"**Since {previous}**"]
    for pr, title in changes:
        if pr:
            lines.append(f"- #{pr} {title}")
        else:
            lines.append(f"- {title}")
    return "\n".join(lines) + "\n"


def notes_for(version: str, sha: str, *, previous: str | None = None,
              changes: list[tuple[str, str]] | None = None) -> str:
    return (
        f"CalVer cut of Graphban (`YYYY.MM.N`). `/health` reports this as "
        f"`version`; `git_sha` is the exact build (`{sha}`).\n\n"
        f"{changes_section(previous, changes)}\n"
        f"**Artifact**\n"
        f"`{asset_name(version)}` is backend + Alembic, prebuilt `web/dist`, "
        f"`GIT_SHA`, no `.env`. GitHub's source zip is not this tree.\n\n"
        f"**Apply**\n"
        f"Compose: Settings → This box → Updates → Install.\n"
        f"Native: unpack and `graphban_host.py upgrade --release ./graphban-{version} "
        f"--sha {sha}`.\n\n"
        f"**Verify**\n"
        f"```\ncurl -s https://<host>/health\n"
        f"# version {version}, git_sha {sha}, db ok\n```\n"
    )


def collect_notes(repo: pathlib.Path, version: str, until: str, sha_short: str, *,
                  git_run=None) -> tuple[str, str | None, list[tuple[str, str]] | None]:
    """Notes body plus the previous tag / merge list used to build it."""
    run = git_run or git
    tags = existing_tags(repo, git_run=run)
    prev = previous_tag(version, tags)
    if prev is None:
        body = notes_for(version, sha_short, previous=None, changes=[])
        return body, None, []
    resolved = run(repo, "rev-parse", "--verify", f"refs/tags/{prev}^{{commit}}",
                   check=False)
    if resolved.returncode != 0:
        fetched = run(repo, "fetch", "origin", f"refs/tags/{prev}:refs/tags/{prev}",
                      check=False)
        if fetched.returncode != 0:
            body = notes_for(version, sha_short, previous=prev, changes=None)
            return body, prev, None
    changes = list_merges(repo, prev, until, git_run=run)
    body = notes_for(version, sha_short, previous=prev, changes=changes)
    return body, prev, changes


def pack_release(ref: str, out: pathlib.Path, *, api_only: bool = False) -> int:
    cmd = [sys.executable, str(PACK_SCRIPT), ref, "--out", str(out)]
    if api_only:
        cmd.append("--api-only")
    return subprocess.run(cmd).returncode


def gh(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(["gh", *args], capture_output=True, text=True)


def _gh_json(proc: subprocess.CompletedProcess) -> dict | list | None:
    if proc.returncode != 0 or not (proc.stdout or "").strip():
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None


def release_assets(view: dict | None) -> list[str]:
    if not view:
        return []
    return [str(a.get("name") or "") for a in (view.get("assets") or [])]


def cmd_next(repo: pathlib.Path, *, today: dt.date, git_run=None) -> int:
    current = read_stamped(repo)
    nxt = next_calver(current, today, existing_tags(repo, git_run=git_run))
    print(nxt)
    return 0


def cmd_stamp(repo: pathlib.Path, version: str | None, *, today: dt.date,
              git_run=None) -> int:
    current = read_stamped(repo)
    if version and version[:1] in "vV":
        return _fail("no v prefix — tags are YYYY.MM.N")
    if not version:
        version = next_calver(current, today, existing_tags(repo, git_run=git_run))
    version = normalize_tag(version)
    try:
        written = stamp(repo, version)
    except ValueError as e:
        return _fail(str(e))
    print(f"stamped {version}")
    for rel in written:
        print(f"  {rel.as_posix()}")
    print()
    print("Commit, open a PR, merge to main, then:")
    print(f"  python3 scripts/graphban_release.py publish {version}")
    print()
    print(commit_message(version, current).rstrip())
    return 0


def cmd_notes(repo: pathlib.Path, version: str | None, *,
              ref: str, fetch: bool, git_run=None) -> int:
    """Print the GitHub Release body that publish would attach. Does not pack."""
    run = git_run or git
    if fetch:
        fetched = run(repo, "fetch", "origin", "main", check=False)
        if fetched.returncode != 0:
            return _fail("could not fetch origin/main")
    resolved = run(repo, "rev-parse", "--verify", f"{ref}^{{commit}}", check=False)
    if resolved.returncode != 0 or not (resolved.stdout or "").strip():
        return _fail(f"could not resolve {ref!r}")
    sha = resolved.stdout.strip()
    short = run(repo, "rev-parse", "--short", sha).stdout.strip()
    stamped = read_stamped_at(repo, sha, git_run=run)
    if not version:
        version = stamped
    version = normalize_tag(version)
    if parse_calver(version) is None or version in PLACEHOLDERS:
        return _fail(f"{ref} is not a named cut ({stamped or 'empty'})")
    notes, _, _ = collect_notes(repo, version, sha, short, git_run=run)
    print(notes.rstrip())
    return 0


def cmd_publish(repo: pathlib.Path, version: str | None, *,
                ref: str, out: pathlib.Path, fetch: bool, api_only: bool,
                dry_run: bool, git_run=None, pack=pack_release, gh_run=gh) -> int:
    """Tag + pack + GitHub Release. Does not merge and does not apply."""
    run = git_run or git
    if fetch:
        fetched = run(repo, "fetch", "origin", "main", check=False)
        if fetched.returncode != 0:
            return _fail("could not fetch origin/main — publish packs a remote "
                         "commit, not this working tree")
    resolved = run(repo, "rev-parse", "--verify", f"{ref}^{{commit}}", check=False)
    if resolved.returncode != 0 or not (resolved.stdout or "").strip():
        return _fail(f"could not resolve {ref!r}")
    sha = resolved.stdout.strip()
    short = run(repo, "rev-parse", "--short", sha).stdout.strip()
    stamped = read_stamped_at(repo, sha, git_run=run)
    if not version:
        version = stamped
    version = normalize_tag(version)
    if parse_calver(version) is None or version in PLACEHOLDERS:
        return _fail(f"{ref} is not a named cut ({stamped or 'empty'}) — stamp "
                     "and merge first")
    if stamped != version:
        return _fail(f"{ref} is stamped {stamped or '(empty)'}, not {version}. "
                     "Publish the commit that carries the version, not cwd")
    want = asset_name(version)
    tarball = out / want

    view_proc = gh_run(["release", "view", version,
                        "--json", "tagName,assets,isDraft,isPrerelease"])
    existing = _gh_json(view_proc) if view_proc.returncode == 0 else None
    if isinstance(existing, dict) and want in release_assets(existing):
        return _fail(f"GitHub already has {version} with {want} — not republishing")

    notes, _prev, _changes = collect_notes(repo, version, sha, short, git_run=run)

    if dry_run:
        print(f"would pack {ref} ({short}) as {want}")
        print(f"would {'upload to' if existing else 'create'} GitHub Release {version} "
              f"--target {sha} with {want}")
        print("would not apply to a box")
        print()
        print(notes.rstrip())
        return 0

    out.mkdir(parents=True, exist_ok=True)
    rc = pack(sha, out, api_only=api_only)
    if rc != 0:
        return _fail(f"pack failed ({rc}) — not creating a GitHub Release without "
                     f"{want}. GitHub's source zip is not a substitute")
    if not tarball.is_file():
        return _fail(f"pack did not write {tarball} — refusing to publish a "
                     "source-zip-only GitHub Release")

    if isinstance(existing, dict):
        up = gh_run(["release", "upload", version, str(tarball), "--clobber"])
        if up.returncode != 0:
            return _fail("gh release upload failed: "
                         + (up.stderr or up.stdout).strip())
        print(f"uploaded {want} onto existing {version}")
    else:
        created = gh_run([
            "release", "create", version, str(tarball),
            "--title", version,
            "--target", sha,
            "--latest",
            "--notes", notes,
        ])
        if created.returncode != 0:
            return _fail("gh release create failed: "
                         + (created.stderr or created.stdout).strip())
        print(f"published {version} at {short}")

    check = gh_run(["release", "view", version,
                    "--json", "tagName,assets,isDraft,isPrerelease"])
    body = _gh_json(check) if check.returncode == 0 else None
    if not isinstance(body, dict) or want not in release_assets(body):
        return _fail(f"GitHub Release {version} does not list {want} — "
                     "Updates Install would have nothing to fetch. "
                     "/releases/latest can lag a few seconds; view this tag")
    if body.get("isDraft") or body.get("isPrerelease"):
        return _fail(f"{version} is draft/prerelease — /releases/latest will not "
                     "advertise it")
    print(f"asset {want}")
    print("Settings → This box → Updates → Check, then Install.")
    print("Do not apply from this script.")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Stamp / publish a Graphban CalVer cut. Does not apply.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("next", help="print the next YYYY.MM.N")

    p_stamp = sub.add_parser("stamp", help="write version.py, pyproject, package.json")
    p_stamp.add_argument("version", nargs="?", default="")

    p_notes = sub.add_parser(
        "notes",
        help="print the GitHub Release body (merges since the previous CalVer)")
    p_notes.add_argument("version", nargs="?", default="")
    p_notes.add_argument("--ref", default="origin/main")
    p_notes.add_argument("--no-fetch", action="store_true")

    p_pub = sub.add_parser("publish",
                           help="pack + GitHub Release for the stamped ref")
    p_pub.add_argument("version", nargs="?", default="")
    p_pub.add_argument("--ref", default="origin/main",
                       help="commit to pack (default origin/main, never cwd)")
    p_pub.add_argument("--out", default="dist-release")
    p_pub.add_argument("--no-fetch", action="store_true")
    p_pub.add_argument("--api-only", action="store_true",
                       help="pass through to the packer (tests / API-only cut)")
    p_pub.add_argument("--dry-run", action="store_true")

    args = ap.parse_args(argv)
    top = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                         capture_output=True, text=True)
    if top.returncode != 0:
        return _fail("not a git checkout")
    repo = pathlib.Path(top.stdout.strip())
    today = dt.date.today()

    if args.cmd == "next":
        return cmd_next(repo, today=today)
    if args.cmd == "stamp":
        return cmd_stamp(repo, args.version or None, today=today)
    if args.cmd == "notes":
        return cmd_notes(
            repo, args.version or None,
            ref=args.ref, fetch=not args.no_fetch,
        )
    if args.cmd == "publish":
        return cmd_publish(
            repo, args.version or None,
            ref=args.ref,
            out=pathlib.Path(args.out),
            fetch=not args.no_fetch,
            api_only=args.api_only,
            dry_run=args.dry_run,
        )
    return _fail(f"unknown command {args.cmd}")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except RuntimeError as e:
        sys.exit(_fail(str(e)))
