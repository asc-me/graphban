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

**Remove and rekey (GRPH-1976).** `remove` uninstalls the service, revokes its key, deletes
the env file, and deletes the checkout only when its marker names this drain and it holds
nothing that is not provably disposable (`checkout_verdict`); any other checkout is kept and
named with the reason. `rekey` mints a new PRD-limited key, swaps the env file atomically,
restarts the service, and revokes the old key only after the server has seen the new key used
(`last_used`), and only when no other installed drain's env file holds the same old key.
Neither prints a key value. The server keeps only a hash, so the key's id is recorded beside
the env file (`<name>.key-id`, not a secret). **A key is revoked only by that recorded id,
never by a guess**: a drain without the record (one `create` did not make, often holding the
person's own `gban setup` key) has its key left alive, and both commands list the person's
live keys by id and label and send them to Settings to revoke the right one.

**Preflight refuses with a named reason, before anything is cut, minted or written**
(GRPH-1975). `draft` refuses an unsupported platform, a service or env file that already
exists, a plan for another PRD, an empty captured PATH, an adapter that PATH does not
resolve, install flags that differ from the dry run's, and an `until` already holding the
checkout. `create` then reads the PRD's coverage through the session and refuses a PRD that
is not approved or not shaped. Only after all of that does it clone, mint and install.
"""
from __future__ import annotations

import hmac
import os
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, TextIO

from . import client as client_mod
from . import lock as lock_mod
from . import service
from .adapters import ADAPTERS

#: Written into a checkout `create` cloned, naming the drain that owns it (grill decision 4).
#: `drain remove` reads it before deleting a directory; a checkout without it is the
#: person's, and is never deleted.
MARKER = "gbfleet-drain"

#: Flags that belong to the drain being created, never copied from `--like`.
_OWN_FLAGS = {"--repo": True, "--prd": True, "--dry-run": False, "--line": True}

#: The supervisors `drain create` installs into. Task Scheduler has `service install`, but not
#: this command: P96 scopes `create` to launchd and systemd --user.
SUPPORTED_KINDS = {"launchd": "macOS (launchd)", "systemd": "Linux (systemd --user)"}

#: The PRD status `create` will mint a key for. A draft or in-review PRD has no settled
#: sections for a drain to work, and a closed one has nothing left.
APPROVED = "approved"

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
    #: The PATH the unit will capture. Checked here, and handed to `make_plan` unchanged.
    path_env: str = ""

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
    kind = _supported_kind()
    # First, before any `git` runs: with no PATH those fail too, and the refusal would
    # name a missing checkout instead of the PATH.
    path_env = os.environ.get("PATH", "")
    if not path_env.strip():
        raise Refused(
            "the PATH this service would capture is empty, so it could run no adapter (and "
            "no git) at all. Run create from a shell whose PATH finds the adapter")
    name = opts.name or prd.lower()
    unit = service.unit_path_for(name, kind)
    if unit.exists():
        raise Refused(
            f"a service named {name!r} is already installed ({unit}). See "
            f"`gbfleet service status --name {name}`; pick another --name, or remove that "
            "drain first. create never overwrites one")
    env = service.env_path_for(name)
    if env.exists():
        raise Refused(
            f"{env} already exists, so a drain named {name!r} already has a key file. Pick "
            "another --name, or remove that drain first; create never overwrites a key")

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
    adapter = service.value_of(until_args, "--adapter")
    if adapter is None:
        raise Refused("no adapter: pass --adapter, or --like a drain that has one")
    planned = _prds_in(until_args)
    if planned != [prd]:
        raise Refused(
            f"the plan is for {', '.join(planned) or 'no PRD'}, not the requested {prd}; "
            "not installing a drain for a PRD nobody asked for")
    _check_adapter_on(adapter, path_env)
    drift = flag_drift(until_args, service.install_args(until_args))
    if drift:
        raise Refused(f"the install flags differ from the dry run's at {drift}; not installing")
    if not clone_from:
        _check_no_until(repo)
    return Draft(
        name=name, prd=prd, kind=kind, repo=repo, clone_from=clone_from, supplied=supplied,
        until_args=until_args, binary=service._this_gbfleet(),
        label=f"{name}@{socket.gethostname()}", every=opts.every,
        project=service.value_of(until_args, "--project") or "",
        unit_path=unit, env_path=env, path_env=path_env)


# ---- preflight (GRPH-1975) -----------------------------------------------------------------

def _supported_kind() -> str:
    found = service.host()
    if found.kind in SUPPORTED_KINDS:
        return found.kind
    this = f"this host has {found.kind}" if found.kind else f"this host has neither: {found.why}"
    raise Refused(
        f"drain create installs into {' or '.join(SUPPORTED_KINDS.values())}; {this}. "
        "Nothing was written")


def _prds_in(args: list[str]) -> list[str]:
    """Every `--prd` value among `until`'s own flags (the `-- command` tail is the child's)."""
    flags, _tail = _split_remainder(args)
    out: list[str] = []
    for i, arg in enumerate(flags):
        if arg == "--prd":
            out.append(flags[i + 1] if i + 1 < len(flags) else "")
        elif arg.startswith("--prd="):
            out.append(arg.split("=", 1)[1])
    return out


def _check_adapter_on(adapter: str, path_env: str) -> None:
    """The adapter's binary must resolve on the PATH the unit will capture, not on ours."""
    known = ADAPTERS.get(adapter)
    if known is None:
        raise Refused(f"--adapter {adapter!r} is not one of {', '.join(sorted(ADAPTERS))}")
    if shutil.which(known.binary, path=path_env) is None:
        raise Refused(
            f"adapter {adapter!r} needs {known.binary!r}, which does not resolve on the PATH "
            f"this service would capture ({path_env}). Install it, or run create from a shell "
            "whose PATH finds it")


def _pairs(args: list[str]) -> list[tuple[str, str]]:
    """(flag, value) in order; a bare flag has value "", a `-- tail` is one entry."""
    flags, tail = _split_remainder(args)
    out: list[tuple[str, str]] = []
    i = 0
    while i < len(flags):
        arg = flags[i]
        if arg.startswith("--") and "=" in arg:
            flag, _, value = arg.partition("=")
            out.append((flag, value))
        elif i + 1 < len(flags) and not flags[i + 1].startswith("--"):
            out.append((arg, flags[i + 1]))
            i += 1
        else:
            out.append((arg, ""))
        i += 1
    if tail:
        out.append(("--", " ".join(tail[1:])))
    return out


def flag_drift(dry: list[str], install: list[str]) -> str:
    """"" when byte-identical; else the first differing flag, with both values."""
    if dry == install:
        return ""
    a, b = _pairs(dry), _pairs(install)
    for i in range(max(len(a), len(b))):
        left = a[i] if i < len(a) else None
        right = b[i] if i < len(b) else None
        if left != right:
            flag = (left or right)[0]  # type: ignore[index]
            return (f"{flag} (dry run: {left[1] if left else 'absent'!r}, "
                    f"install: {right[1] if right else 'absent'!r})")
    # Same pairs, different bytes: `--x=1` against `--x 1`. Still not identical.
    return f"the argv spelling ({dry} vs {install})"


def _check_no_until(repo: Path) -> None:
    """Refuse a checkout another supervisor holds. Asks the lock `until` takes; never takes it."""
    try:
        lock_mod.probe(repo)
    except lock_mod.RepoLocked as exc:
        raise Refused(f"another `until` (or `mcp`) is running against {repo}: {exc}") from None
    except OSError as exc:
        # Could not ask is not "free": a drain installed beside a live one double-spawns.
        raise Refused(f"cannot tell whether a supervisor holds {repo}: {exc}") from None


def check_prd(ledger: object, prd: str) -> None:
    """The PRD must be approved and shaped. Read through the session; an unreadable reply
    is a refusal, never a pass."""
    try:
        cov = ledger.prd_coverage(prd)  # type: ignore[attr-defined]
    except (client_mod.ServerUnreachable, client_mod.ToolFailed) as exc:
        raise Refused(f"cannot read {prd}'s coverage to check it is approved: {exc}") from None
    status = str(cov.get("status") or "")
    if status != APPROVED:
        raise Refused(
            f"{prd} is {status or 'of unknown status'}, not {APPROVED}. Approve it first; "
            "a drain works an approved PRD's items")
    if cov.get("shaped") is not True:
        raise Refused(
            f"prd_coverage reports {prd} shaped: {cov.get('shaped')!r}, so it has no sections "
            "for items to sit under. Give it `##` sections and decompose it first")


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
    try:
        check_prd(ledger, d.prd)
    except Refused as exc:
        print(f"gbfleet drain create: {exc}. Nothing was written.", file=err)
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
                                 path_env=d.path_env, every=d.every)
    except service.Refused as exc:
        print(f"gbfleet drain create: {exc}", file=err)
        return EXIT_REFUSED
    # The dry run is a promise about what runs. A difference here is a bug in this module,
    # and installing anyway would break the promise quietly.
    # Preflight compared `install_args` already; this is the same promise, kept on the plan
    # that is actually installed.
    drift = flag_drift(d.until_args, plan.until_args)
    if drift:
        print(f"gbfleet drain create: the install flags differ from the dry run's at {drift}; "
              "not installing", file=err)
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
    _record_key_id(d.name, str(minted.get("id") or ""))
    for warning in done.warnings:
        print(f"WARNING {warning}", file=err)
    state = done.state or service.status(d.name, kind=d.kind)
    if show_status is not None:
        show_status(state)
    else:
        print(state.line(), file=out)
    return 0


# ---- remove and rekey (GRPH-1976) ----------------------------------------------------------

#: How long `rekey` waits for the restarted service's first call with the new key before it
#: leaves the old key alive and says so. `until` calls the server as it starts, so a minute is
#: a service that did not come up, not a slow one.
FIRST_CALL_WINDOW = 120.0
FIRST_CALL_POLL = 3.0


def key_id_path(name: str) -> Path:
    """The id of the key in `<name>.env`. Not a secret: an id revokes nothing by itself."""
    return service.CONFIG_DIR / f"{name}.key-id"


def _record_key_id(name: str, key_id: str) -> None:
    if not key_id:
        return
    path = key_id_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{key_id}\n", encoding="utf-8")


def _recorded_key_id(name: str) -> str:
    try:
        return key_id_path(name).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _read_key(env_path: Path) -> str:
    """The key in an env file, or "". Held in memory only; never printed or logged."""
    try:
        text = env_path.read_text(encoding="utf-8")
    except OSError:
        return ""
    for line in text.splitlines():
        if line.startswith(f"{service.API_KEY_ENV}="):
            return line.split("=", 1)[1].strip()
    return ""


def _same_key(a: str, b: str) -> bool:
    """Constant-time equality, so neither value is compared in a way that prints or leaks."""
    return bool(a) and bool(b) and hmac.compare_digest(a.encode(), b.encode())


def sharers(name: str, key: str, kind: str) -> list[str]:
    """The other installed drains whose env file holds this same key (a copied shared key)."""
    return [other for other in service.installed_names(kind)
            if other != name and _same_key(_read_key(service.env_path_for(other)), key)]


@dataclass
class KeyLookup:
    """What `resolve_key_id` found. `key_id` is set only from a recorded id; never a guess."""
    key_id: str = ""
    why: str = ""
    #: The person's live keys as (id, label), for the revoke-it-yourself guidance. Ids and
    #: labels only: the listing's prefix is never carried, let alone a value.
    candidates: list[tuple[str, str]] = field(default_factory=list)


def resolve_key_id(ledger: object, name: str, key: str) -> KeyLookup:
    """The id of the key in `name`'s env file, from `<name>.key-id` only.

    No automatic revocation without a recorded id (the owner's decision after the GRPH-1976
    bounce): a drain `create` did not make often holds the person's own `gban setup` key, and
    a prefix match can name an unrelated live key. Without a record nothing is matched;
    the caller leaves the key alive and prints `candidates` for the person to choose from.
    """
    try:
        rows = ledger.list_keys()  # type: ignore[attr-defined]
    except (client_mod.ServerUnreachable, client_mod.ToolFailed) as exc:
        return KeyLookup(why=f"cannot list your keys: {exc}")
    # A key another drain's record names is that drain's; it is never offered for revoking.
    owned = {_recorded_key_id(p.stem) for p in service.CONFIG_DIR.glob("*.key-id")
             if p.stem != name} - {""}
    live = [(str(r.get("id") or ""), str(r.get("name") or ""))
            for r in rows if not r.get("revoked") and r.get("id")
            and str(r.get("id")) not in owned]
    recorded = _recorded_key_id(name)
    if not recorded:
        return KeyLookup(why=f"no {key_id_path(name).name} records which key it is",
                         candidates=live)
    row = next((r for r in rows if str(r.get("id")) == recorded), None)
    if row is None:
        return KeyLookup(why=f"the recorded key {recorded} is not one of your keys",
                         candidates=live)
    prefix = str(row.get("prefix") or "")
    if prefix and not key.startswith(prefix):
        return KeyLookup(why=f"the recorded key {recorded} is not the key in {name}'s env file",
                         candidates=live)
    return KeyLookup(key_id=recorded)


def print_left_alive(lookup: KeyLookup, out: TextIO) -> None:
    """The old key stays alive: say so, list the candidates by id and label, point at Settings."""
    print(f"old key: left ALIVE, not revoked: {lookup.why}. gbfleet never revokes a key it "
          "cannot name from its own record.", file=out)
    if lookup.candidates:
        print("  your live keys (id, label):", file=out)
        for key_id, label in lookup.candidates:
            print(f"    {key_id}  {label or '(no label)'}", file=out)
    print("  Revoke the one this drain used in Settings > API keys, if no one else uses it "
          "(it may be your `gban setup` key).", file=out)


#: The only ignored paths `remove` treats as disposable: regenerable caches and environments.
#: A directory here matches at any depth; any other ignored file keeps the checkout.
DISPOSABLE_DIRS = frozenset({".venv", "node_modules", "__pycache__", ".pytest_cache",
                             ".mypy_cache", ".ruff_cache"})
DISPOSABLE_SUFFIXES = (".pyc",)


def _disposable(path: str) -> bool:
    parts = path.rstrip("/").split("/")
    return any(p in DISPOSABLE_DIRS for p in parts[:-1]) or parts[-1] in DISPOSABLE_DIRS \
        or parts[-1].endswith(DISPOSABLE_SUFFIXES)


def _named(paths: list[str], limit: int = 5) -> str:
    shown = ", ".join(paths[:limit])
    return shown + (f" and {len(paths) - limit} more" if len(paths) > limit else "")


def checkout_verdict(repo: Path, name: str) -> str:
    """"" when `repo` may be deleted, else why it is kept. Deleted only when it holds nothing
    that is not provably disposable: it is the top of a checkout, its marker names `name`, it
    has no linked worktree, no uncommitted change, no untracked file (whatever
    `status.showUntrackedFiles` says), no ignored file outside `DISPOSABLE_DIRS` /
    `DISPOSABLE_SUFFIXES`, no stash, no tag on an unpushed commit, and no commit on any ref
    that no remote-tracking ref reaches. Anything unreadable is a keep, never a delete."""
    if _toplevel(repo) != repo.resolve():
        return "it is not the top of a git checkout"
    code, git_dir = _git(repo, "rev-parse", "--absolute-git-dir")
    if code != 0:
        return f"cannot find its git dir: {git_dir}"
    marker = Path(git_dir) / MARKER
    try:
        owner = marker.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return f"it has no .git/{MARKER} marker, so drain create did not make it"
    except OSError as exc:
        return f"cannot read its .git/{MARKER} marker: {exc}"
    if owner != name:
        return f"its .git/{MARKER} marker names drain {owner!r}, not {name!r}"
    # Any linked worktree, live or not: deleting the main checkout destroys the .git it runs on.
    code, listed = _git(repo, "worktree", "list", "--porcelain")
    if code != 0:
        return f"cannot list its worktrees: {listed}"
    trees = [line[len("worktree "):] for line in listed.splitlines()
             if line.startswith("worktree ")]
    linked = [t for t in trees if Path(t).resolve() != repo.resolve()]
    if linked:
        return f"it has linked worktree(s): {_named(linked)}"
    # Explicit, so the repo's status.showUntrackedFiles cannot hide an untracked file.
    code, dirty = _git(repo, "status", "--porcelain", "--untracked-files=all")
    if code != 0:
        return f"cannot read its status: {dirty}"
    if dirty:
        lines = dirty.splitlines()
        untracked = [line[3:] for line in lines if line.startswith("?? ")]
        if len(untracked) < len(lines):
            return "it has uncommitted changes"
        return f"it has untracked file(s): {_named(untracked)}"
    code, ignored = _git(repo, "ls-files", "-z", "--others", "--ignored", "--exclude-standard")
    if code != 0:
        return f"cannot list its ignored files: {ignored}"
    kept = [p for p in ignored.split("\0") if p and not _disposable(p)]
    if kept:
        return f"it has ignored file(s) that are not caches: {_named(kept)}"
    code, stashed = _git(repo, "stash", "list")
    if code != 0:
        return f"cannot read its stash: {stashed}"
    if stashed:
        return "it has stashed changes"
    code, tags = _git(repo, "for-each-ref", "--format=%(refname:short)", "refs/tags")
    if code != 0:
        return f"cannot list its tags: {tags}"
    for tag in tags.splitlines():
        code, ahead = _git(repo, "rev-list", f"refs/tags/{tag}", "--not", "--remotes")
        if code != 0:
            return f"cannot tell whether tag {tag} is pushed: {ahead}"
        if ahead:
            return f"its tag {tag} points at a commit that is on no remote"
    # Every ref and every HEAD, not just branches: a commit only a note, a detached HEAD or an
    # odd ref reaches is still work.
    code, ahead = _git(repo, "rev-list", "--all", "--not", "--remotes")
    if code != 0:
        return f"cannot tell whether it has unpushed commits: {ahead}"
    if ahead:
        return f"it has {len(ahead.splitlines())} unpushed commit(s)"
    return ""


def _open(server: str, verb: str, err: TextIO) -> tuple[object | None, int]:
    try:
        return open_ledger(server), 0
    except client_mod.NoSession as exc:
        print(f"gbfleet drain {verb}: {exc}. Run `gban --server {server} login`, then run "
              "this again. Nothing was changed.", file=err)
        return None, EXIT_NO_SESSION
    except (client_mod.ServerUnreachable, client_mod.ToolFailed) as exc:
        print(f"gbfleet drain {verb}: cannot open the gban session: {exc}. Nothing was "
              "changed.", file=err)
        return None, EXIT_REFUSED


def remove(name: str, *, out: TextIO | None = None, err: TextIO | None = None) -> int:
    """Uninstall, revoke, delete the env file, and delete the checkout only if it is safe."""
    out = out or sys.stdout
    err = err or sys.stderr
    try:
        kind = _supported_kind()
    except Refused as exc:
        print(f"gbfleet drain remove: {exc}", file=err)
        return EXIT_REFUSED
    unit = service.unit_path_for(name, kind)
    env = service.env_path_for(name)
    if not unit.exists() and not env.exists():
        print(f"gbfleet drain remove: no drain named {name!r} is installed here ({kind})",
              file=err)
        return EXIT_REFUSED
    # Read before uninstall: it deletes the unit and the env file this reads from.
    args = service.until_args_of(name, kind) or []
    repo = service.value_of(args, "--repo")
    server = service.value_of(args, "--server") or client_mod.gban_url()
    key = _read_key(env)
    others = sharers(name, key, kind) if key else []
    ledger, lookup = None, KeyLookup()
    if key and not others:
        ledger, code = _open(server, "remove", err)
        if ledger is None:
            return code
        lookup = resolve_key_id(ledger, name, key)
    key = ""

    failed = False
    try:
        removed = service.uninstall(name, kind=kind)
    except service.Refused as exc:
        print(f"gbfleet drain remove: {exc}", file=err)
        return EXIT_REFUSED
    print(f"service: uninstalled {name} ({kind})", file=out)
    for path in removed:
        print(f"  deleted {path}", file=out)
    if others:
        print(f"key: NOT revoked: the env file of {', '.join(others)} holds the same key, and "
              "revoking it would stop them. Rekey them, then revoke it", file=out)
    elif ledger is not None:
        if lookup.key_id:
            try:
                ledger.revoke_key(lookup.key_id)  # type: ignore[attr-defined]
                print(f"key: revoked ({lookup.key_id})", file=out)
            except (client_mod.ServerUnreachable, client_mod.ToolFailed) as exc:
                failed = True
                print(f"gbfleet drain remove: key {lookup.key_id} was NOT revoked: {exc}. "
                      "Revoke it in Settings", file=err)
        else:
            print_left_alive(lookup, out)
    else:
        print(f"key: {env} held no {service.API_KEY_ENV}, so there was none to revoke",
              file=out)
    try:
        key_id_path(name).unlink()
    except OSError:
        pass

    if not repo:
        print("checkout: none named in the unit; nothing to delete", file=out)
    elif not Path(repo).exists():
        print(f"checkout: {repo} is already gone", file=out)
    else:
        path = Path(repo)
        verdict = checkout_verdict(path, name)
        if verdict:
            print(f"checkout: kept {path}: {verdict}", file=out)
        else:
            shutil.rmtree(path)
            print(f"checkout: deleted {path}", file=out)
    return 1 if failed else 0


def _env_plan(name: str, kind: str, env: Path) -> service.Plan:
    """A plan that carries only the env path, for `service.write_env`'s atomic swap."""
    return service.Plan(name=name, kind=kind, label=service.label_for(name),
                        unit_path=service.unit_path_for(name, kind), env_path=env,
                        log_path=env, err_path=env, binary="", until_args=[], repo=env.parent,
                        common_dir=env.parent, path_env="")


def restart(name: str, kind: str) -> str:
    """Restart the installed service so it reads the env file again. Loads it if it is not."""
    unit = service.unit_path_for(name, kind)
    if kind == "launchd":
        domain = service._launchd_gui_domain()
        if domain is None:
            raise Refused("launchd domain needs a numeric uid; os.getuid is absent")
        code, said = service._run(["launchctl", "kickstart", "-k",
                                   f"{domain}/{service.label_for(name)}"])
    else:
        code, said = service._run(["systemctl", "--user", "restart", service.unit_name(name)])
    if code != 0:
        return service._load_existing(name, kind, unit)
    return said


def first_call_seen(ledger: object, key_id: str, *, window: float | None = None,
                    poll: float | None = None) -> bool:
    """True once the server reports `last_used` on the new key: an authenticated call made it.

    Nobody else holds the new key, so the first use is the restarted service's.
    """
    window = FIRST_CALL_WINDOW if window is None else window
    poll = FIRST_CALL_POLL if poll is None else poll
    waited = 0.0
    while True:
        try:
            rows = ledger.list_keys()  # type: ignore[attr-defined]
        except (client_mod.ServerUnreachable, client_mod.ToolFailed):
            rows = []
        row = next((r for r in rows if str(r.get("id")) == key_id), None)
        if row is not None and row.get("last_used"):
            return True
        if waited >= window:
            return False
        time.sleep(poll)
        waited += poll


def rekey(name: str, *, out: TextIO | None = None, err: TextIO | None = None) -> int:
    """A new PRD-limited key for one drain; the old one revoked once the new one is in use."""
    out = out or sys.stdout
    err = err or sys.stderr
    try:
        kind = _supported_kind()
    except Refused as exc:
        print(f"gbfleet drain rekey: {exc}", file=err)
        return EXIT_REFUSED
    unit = service.unit_path_for(name, kind)
    env = service.env_path_for(name)
    args = service.until_args_of(name, kind)
    if not unit.exists() or args is None:
        print(f"gbfleet drain rekey: no drain named {name!r} is installed here ({kind})",
              file=err)
        return EXIT_REFUSED
    prd = service.value_of(args, "--prd") or ""
    if not prd:
        print(f"gbfleet drain rekey: {name}'s unit runs no --prd, and a PRD-limited key needs "
              "one. Nothing was changed", file=err)
        return EXIT_REFUSED
    old = _read_key(env)
    if not old:
        print(f"gbfleet drain rekey: {env} holds no {service.API_KEY_ENV}. Nothing was "
              "changed", file=err)
        return EXIT_REFUSED
    server = service.value_of(args, "--server") or client_mod.gban_url()
    project = service.value_of(args, "--project") or ""
    others = sharers(name, old, kind)
    ledger, code = _open(server, "rekey", err)
    if ledger is None:
        return code
    # Named before the new key's id overwrites the record.
    lookup = KeyLookup() if others else resolve_key_id(ledger, name, old)

    label = f"{name}@{socket.gethostname()}"
    try:
        minted = ledger.mint_prd_key(prd, label, project=project)  # type: ignore[attr-defined]
    except (client_mod.ServerUnreachable, client_mod.ToolFailed) as exc:
        print(f"gbfleet drain rekey: the server did not mint a key: {exc}. Nothing was "
              "changed", file=err)
        return EXIT_REFUSED
    new = str(minted.get("plaintext") or "")
    new_id = str(minted.get("id") or "")
    if not new or not new_id or _same_key(new, old):
        print("gbfleet drain rekey: the server returned no usable new key. Nothing was "
              "changed", file=err)
        return EXIT_REFUSED
    try:
        warnings = service.write_env(_env_plan(name, kind, env), new)
    except OSError as exc:
        print(f"gbfleet drain rekey: could not write {env}: {exc}. It still holds the old "
              f"key; key {new_id} was minted and not stored, revoke it in Settings", file=err)
        return EXIT_REFUSED
    finally:
        new = old = ""
    _record_key_id(name, new_id)
    print(f"key: minted ({new_id}, labelled {label}), written to {env}", file=out)
    for warning in warnings:
        print(f"WARNING {warning}", file=err)

    try:
        restart(name, kind)
    except Refused as exc:
        print(f"gbfleet drain rekey: {exc}. The old key is left alive", file=err)
        return 1
    if not first_call_seen(ledger, new_id):
        print(f"gbfleet drain rekey: the restarted service has made no call with key {new_id} "
              "yet, so the old key is left alive. See `gbfleet service status --name "
              f"{name}`, then revoke the old key in Settings", file=err)
        return 1
    print(f"service: restarted; its first call with key {new_id} was seen", file=out)
    if others:
        print(f"old key: NOT revoked: the env file of {', '.join(others)} holds it too, and "
              "revoking it would stop them. Rekey them, then revoke it in Settings", file=out)
        return 0
    if not lookup.key_id:
        print_left_alive(lookup, out)
        return 0
    old_id = lookup.key_id
    try:
        ledger.revoke_key(old_id)  # type: ignore[attr-defined]
    except (client_mod.ServerUnreachable, client_mod.ToolFailed) as exc:
        print(f"gbfleet drain rekey: old key {old_id} was NOT revoked: {exc}. Revoke it in "
              "Settings", file=err)
        return 1
    print(f"old key: revoked ({old_id})", file=out)
    return 0
