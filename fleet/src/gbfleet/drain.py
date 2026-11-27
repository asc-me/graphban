"""`gbfleet drain create`: one PRD, its own checkout, its own key, its own service (GRPH-P96).

Before this, starting a second drain was five hand steps — clone, mint a key in the UI, paste
it into a shell, copy the first drain's flags out of its unit, `gbfleet service install` — and
the one that went wrong was the key: a project key, shared, pasted. `create` does all five
from one command and keeps the key off every surface a person or a log can read.

**The unit is the record of what runs.** `--like` reads the flags of an installed drain back
from its unit file (`service.until_args_of`), not from a config of its own, so there is no
second copy to drift. Explicit flags override what was copied.

**The key is minted, never shown.** It is minted with the person's `gban` session, limited to
the PRD (GRPH-1973), written to `~/.config/gbfleet/<name>.env` (owner-only, atomic, by
`service.write_env`), and named in output only by its id. `service.install` refuses a unit that
contains the value.

**No session, nothing written.** The session is checked before the checkout is cut, so a
person without `gban login` gets the instruction and an unchanged disk.
"""
from __future__ import annotations

import socket
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, TextIO

from . import client as client_mod
from . import service

#: Written into a checkout `create` cloned, naming the drain that owns it (grill decision 4).
#: `drain remove` reads it before deleting a directory; a checkout without it is the
#: person's, and is never deleted.
MARKER = "gbfleet-drain"

#: Flags that belong to the drain being created, never copied from `--like`.
_OWN_FLAGS = {"--repo": True, "--prd": True, "--dry-run": False, "--line": True}

#: Exit codes. 3 matches `gban`'s EXIT_NO_SESSION.
EXIT_REFUSED = 2
EXIT_NO_SESSION = 3


class Refused(RuntimeError):
    """`create` cannot be done correctly. Nothing has been written."""


@dataclass
class Options:
    prd: str
    like: str = ""
    repo: str = ""
    name: str = ""
    server: str = ""
    project: str = ""
    adapter: str = ""
    tier: list[str] = field(default_factory=list)
    max_workers: int | None = None
    allow: list[str] = field(default_factory=list)
    merge: bool = False
    every: int = service.RESTART_SEC
    dry_run: bool = False


@dataclass
class Draft:
    """Everything `create` will do, worked out before it does any of it."""

    name: str
    prd: str
    kind: str
    repo: Path
    #: Clone from this remote into `repo`; "" when `repo` already is a usable checkout.
    clone_from: str
    #: True when the person named `--repo`. Such a checkout never gets the marker.
    supplied: bool
    until_args: list[str]
    binary: str
    label: str
    every: int
    project: str
    unit_path: Path
    env_path: Path

    @property
    def runs(self) -> str:
        return f"{self.binary} until {' '.join(self.until_args)}"


# ---- argv editing --------------------------------------------------------------------------

def _drop(args: list[str], flag: str, takes_value: bool) -> list[str]:
    out: list[str] = []
    skip = False
    for arg in args:
        if skip:
            skip = False
            continue
        if arg == flag:
            skip = takes_value
            continue
        if takes_value and arg.startswith(f"{flag}="):
            continue
        out.append(arg)
    return out


def _set(args: list[str], flag: str, value: str) -> list[str]:
    """`flag value`, replacing any copy already there."""
    return [*_drop(args, flag, True), flag, value]


def _split_remainder(args: list[str]) -> tuple[list[str], list[str]]:
    """`until`'s own flags, and the `-- command` tail that is passed to each child unchanged."""
    if "--" in args:
        i = args.index("--")
        return args[:i], args[i:]
    return list(args), []


def until_args_for(opts: Options, copied: list[str], repo: Path) -> list[str]:
    """The `until` argv: `--repo` and `--prd` first, then `--like`'s flags, then overrides."""
    flags, tail = _split_remainder(copied)
    for flag, takes_value in _OWN_FLAGS.items():
        flags = _drop(flags, flag, takes_value)
    if opts.server:
        flags = _set(flags, "--server", opts.server)
    if opts.project:
        flags = _set(flags, "--project", opts.project)
    if opts.adapter:
        flags = _set(flags, "--adapter", opts.adapter)
    if opts.max_workers is not None:
        flags = _set(flags, "--max-workers", str(opts.max_workers))
    # Repeatable flags: naming any replaces every copied one. Adding to a list the person
    # cannot see would make the explicit flag mean something other than what was typed.
    for flag, values in (("--tier", opts.tier), ("--allow", opts.allow)):
        if values:
            flags = _drop(flags, flag, True)
            for value in values:
                flags += [flag, value]
    if opts.merge and "--merge" not in flags:
        flags.append("--merge")
    return ["--repo", str(repo), "--prd", opts.prd, *flags, *tail]


# ---- git -----------------------------------------------------------------------------------

def _git(cwd: Path, *args: str) -> tuple[int, str]:
    try:
        done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                              timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)
    return done.returncode, (done.stdout if done.returncode == 0 else done.stderr).strip()


def _toplevel(path: Path) -> Path | None:
    code, out = _git(path, "rev-parse", "--show-toplevel")
    return Path(out).resolve() if code == 0 and out else None


def _remote(checkout: Path) -> str:
    code, out = _git(checkout, "remote", "get-url", "origin")
    return out if code == 0 else ""


def _same_remote(a: str, b: str) -> bool:
    def norm(url: str) -> str:
        url = url.strip().rstrip("/")
        return url[:-4] if url.endswith(".git") else url
    return bool(a) and norm(a) == norm(b)


def _usable_checkout(path: Path, remote: str) -> str:
    """"" when `path` is a clean checkout of `remote`, else why it is not."""
    top = _toplevel(path)
    if top is None or top != path.resolve():
        return f"{path} exists and is not the top of a git checkout"
    have = _remote(path)
    if not _same_remote(have, remote):
        return f"{path} is a checkout of {have or 'no origin'}, not {remote}"
    code, out = _git(path, "status", "--porcelain")
    if code != 0:
        return f"cannot read the status of {path}: {out}"
    if out:
        return f"{path} has uncommitted changes; a drain must start from a clean checkout"
    return ""


def _write_marker(checkout: Path, name: str) -> None:
    code, git_dir = _git(checkout, "rev-parse", "--absolute-git-dir")
    if code != 0:
        raise Refused(f"cloned {checkout} but cannot find its git dir: {git_dir}")
    (Path(git_dir) / MARKER).write_text(f"{name}\n", encoding="utf-8")


# ---- the plan ------------------------------------------------------------------------------

def draft(opts: Options, *, cwd: Path | None = None) -> Draft:
    """Work out the drain. Reads the disk and the installed units; writes nothing."""
    prd = opts.prd.strip()
    if not prd:
        raise Refused("--prd is required")
    found = service.host()
    if not found.kind:
        raise Refused(found.why)
    kind = found.kind
    name = opts.name or prd.lower()
    for existing in (service.unit_path_for(name, kind), service.env_path_for(name)):
        if existing.exists():
            raise Refused(
                f"a drain named {name!r} already exists ({existing}). Pick another --name, or "
                "remove that drain first; create never overwrites one")

    copied: list[str] = []
    like_repo = ""
    if opts.like:
        got = service.until_args_of(opts.like, kind)
        if got is None:
            raise Refused(
                f"--like {opts.like}: no installed drain of that name ({kind}), or its unit "
                "carries no `until` invocation this can read")
        copied = got
        like_repo = service.value_of(got, "--repo") or ""

    server = opts.server or service.value_of(copied, "--server") or client_mod.gban_url()
    opts = Options(**{**opts.__dict__, "server": server})
    if not server:
        raise Refused("no server: pass --server, --like a drain that has one, or `gban login`")

    origin = _toplevel(cwd or Path.cwd()) or (Path(like_repo) if like_repo else None)
    supplied = bool(opts.repo)
    if supplied:
        repo = Path(opts.repo).expanduser()
        if not repo.is_absolute():
            repo = (cwd or Path.cwd()) / repo
        repo = repo.resolve()
    else:
        if origin is None:
            raise Refused("not inside a git checkout, and no --repo or --like to clone from")
        repo = origin.with_name(f"{origin.name}-{prd.lower()}").resolve()

    remote = _remote(origin) if origin is not None else ""
    clone_from = ""
    if repo.exists():
        if remote:
            why = _usable_checkout(repo, remote)
            if why:
                raise Refused(why)
        elif _toplevel(repo) != repo:
            raise Refused(f"{repo} exists and is not the top of a git checkout")
    elif supplied:
        raise Refused(f"--repo {repo} does not exist. Omit --repo and create clones one")
    else:
        if not remote:
            raise Refused(f"{origin} has no `origin` remote to clone {repo} from")
        clone_from = remote

    until_args = until_args_for(opts, copied, repo)
    if service.value_of(until_args, "--adapter") is None:
        raise Refused("no adapter: pass --adapter, or --like a drain that has one")
    return Draft(
        name=name, prd=prd, kind=kind, repo=repo, clone_from=clone_from, supplied=supplied,
        until_args=until_args, binary=service._this_gbfleet(),
        label=f"{name}@{socket.gethostname()}", every=opts.every,
        project=service.value_of(until_args, "--project") or "",
        unit_path=service.unit_path_for(name, kind), env_path=service.env_path_for(name))


def print_plan(d: Draft, out: TextIO, *, dry_run: bool) -> None:
    would = "would " if dry_run else ""
    print(f"drain {d.name} for {d.prd} ({d.kind})", file=out)
    if d.clone_from:
        print(f"  {would}clone {d.clone_from} into {d.repo} (marked {MARKER})", file=out)
    else:
        print(f"  {would}use the existing checkout {d.repo}", file=out)
    print(f"  {would}mint a key limited to {d.prd}, labelled {d.label}", file=out)
    print(f"  {would}write {d.env_path} (owner-only)", file=out)
    print(f"  {would}install {d.unit_path}", file=out)
    print(f"runs  {d.runs}", file=out)
    print(f"every {d.every}s", file=out)


# ---- create --------------------------------------------------------------------------------

#: Seam: how the person's session is opened. Tests replace it with a fake ledger.
open_ledger: Callable[[str], object] = client_mod.LedgerSession.open


def create(opts: Options, *, out: TextIO | None = None, err: TextIO | None = None,
           show_status: Callable[[object], None] | None = None,
           cwd: Path | None = None) -> int:
    out = out or sys.stdout
    err = err or sys.stderr
    try:
        d = draft(opts, cwd=cwd)
    except (Refused, service.Refused) as exc:
        print(f"gbfleet drain create: {exc}", file=err)
        return EXIT_REFUSED

    if opts.dry_run:
        print_plan(d, out, dry_run=True)
        return 0

    server = service.value_of(d.until_args, "--server") or ""
    try:
        ledger = open_ledger(server)
    except client_mod.NoSession as exc:
        print(f"gbfleet drain create: {exc}. Run `gban --server {server} login`, then "
              "run this again. Nothing was written.", file=err)
        return EXIT_NO_SESSION
    except (client_mod.ServerUnreachable, client_mod.ToolFailed) as exc:
        print(f"gbfleet drain create: cannot open the gban session: {exc}", file=err)
        return EXIT_REFUSED

    if d.clone_from:
        d.repo.parent.mkdir(parents=True, exist_ok=True)
        code, said = _git(d.repo.parent, "clone", "-q", d.clone_from, str(d.repo))
        if code != 0:
            print(f"gbfleet drain create: clone failed: {said}", file=err)
            return EXIT_REFUSED
        _write_marker(d.repo, d.name)

    try:
        plan = service.make_plan(d.until_args, name=d.name, kind=d.kind, binary=d.binary,
                                 every=d.every)
    except service.Refused as exc:
        print(f"gbfleet drain create: {exc}", file=err)
        return EXIT_REFUSED
    # The dry run is a promise about what runs. A difference here is a bug in this module,
    # and installing anyway would break the promise quietly.
    if plan.until_args != d.until_args:
        print("gbfleet drain create: the service would run different arguments than the "
              f"plan ({plan.until_args} vs {d.until_args}); not installing", file=err)
        return EXIT_REFUSED

    try:
        minted = ledger.mint_prd_key(d.prd, d.label, project=d.project)
    except (client_mod.ServerUnreachable, client_mod.ToolFailed) as exc:
        print(f"gbfleet drain create: the server did not mint a key: {exc}", file=err)
        return EXIT_REFUSED
    key = str(minted.get("plaintext") or "")
    key_id = str(minted.get("id") or "unknown id")
    if not key:
        print("gbfleet drain create: the server returned no key", file=err)
        return EXIT_REFUSED

    print_plan(d, out, dry_run=False)
    try:
        done = service.install(plan, key)
    except service.Refused as exc:
        print(f"gbfleet drain create: {exc}. Key {key_id} was minted and not stored; "
              "revoke it in Settings", file=err)
        return EXIT_REFUSED
    finally:
        key = ""
    print(f"key: minted ({key_id}), written to {plan.env_path}", file=out)
    for warning in done.warnings:
        print(f"WARNING {warning}", file=err)
    state = done.state or service.status(d.name, kind=d.kind)
    if show_status is not None:
        show_status(state)
    else:
        print(state.line(), file=out)
    return 0
