"""What is wrong before a single child is spawned.

GRPH-599. `up` and `stdio` both begin by creating worktrees and spending money, and
every trap found while porting this to Windows is one an operator meets on their first
run — several of them silently, or attributed to the wrong component. A repo that
commits `.grok/config.toml` refuses at worktree creation; a rejected api key arrives
mid-wave looking like a partition; an adapter that cannot honour `--debug` says so after
it has already spawned.

None of those needs a child running to be answerable.

**Three outcomes, not two.** A check that could not run reports `UNKNOWN` with the
reason, and `UNKNOWN` is neither counted as success nor left out of the summary. Two
outcomes would force every unanswerable question into one of the answers, and this
whole port has been a catalogue of what that costs: a skip that reads as verified, an
absent file that reads as a clean tree, a `0o600` that means nothing on the platform it
was printed on.

Nothing here mints a seat. The supervisor may not (PRD-22 §4), and a diagnostic that
quietly acquired one would be the authority model leaking out through the back.
"""

from __future__ import annotations

import os
import platform
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import __version__, headroom, hostos
from . import service as service_mod
from .adapters import ADAPTERS, AdapterError, resolve
from .client import Graphban, ServerUnreachable
from .lock import RepoLocked, probe
from .seat import codes_from_text, parse_seat_line
from .state import NotARepository, UnsupportedPlatform, repo_root, state_root
from .worktree import SEAT_FILES, _tracked_at

PASS = "PASS"
FAIL = "FAIL"
UNKNOWN = "UNKNOWN"

#: The floor the package declares. Named here so the check reports the same number the
#: install would have refused on, rather than a second opinion about it.
MINIMUM_PYTHON = (3, 12)


@dataclass
class Finding:
    name: str
    status: str
    detail: str = ""
    remedy: str = ""

    def line(self) -> str:
        out = f"  [{self.status:7}] {self.name}"
        if self.detail:
            out += f" — {self.detail}"
        return out


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)

    def add(self, name: str, status: str, detail: str = "", remedy: str = "") -> Finding:
        found = Finding(name, status, detail, remedy)
        self.findings.append(found)
        return found

    @property
    def failed(self) -> list[Finding]:
        return [f for f in self.findings if f.status == FAIL]

    @property
    def unknown(self) -> list[Finding]:
        return [f for f in self.findings if f.status == UNKNOWN]

    @property
    def ok(self) -> bool:
        """FAIL is the only thing that stops a run.

        `UNKNOWN` deliberately does not: refusing to start because a check could not be
        made would ground the fleet on a slow network or an unreadable temp directory.
        It is printed loudly instead, and the caller decides.
        """
        return not self.failed

    def render(self, out) -> None:
        for finding in self.findings:
            print(finding.line(), file=out)
            if finding.remedy and finding.status != PASS:
                print(f"            {finding.remedy}", file=out)
        print("", file=out)
        counts = (
            f"{len(self.findings) - len(self.failed) - len(self.unknown)} ok, "
            f"{len(self.failed)} failed, {len(self.unknown)} unknown"
        )
        print(f"  {counts}", file=out)
        if self.unknown:
            # Said separately because a summary line that reads "0 failed" while three
            # questions went unanswered is the shape this module exists to avoid.
            print(
                "  unknown is not ok: these were not checked, not checked and found fine",
                file=out,
            )


def check_host(report: Report) -> None:
    version = ".".join(str(p) for p in sys.version_info[:3])
    if os.name not in ("posix", "nt"):
        report.add("host platform", FAIL, f"os.name={os.name!r}",
                   "gbfleet implements POSIX and Windows only; see hostos.py")
    else:
        report.add("host platform", PASS, f"{platform.system()} ({os.name})")

    if sys.version_info[:2] < MINIMUM_PYTHON:
        wanted = ".".join(str(p) for p in MINIMUM_PYTHON)
        report.add("python version", FAIL, f"{version}, needs >= {wanted}",
                   f"install Python {wanted} and reinstall gbfleet into it")
    else:
        report.add("python version", PASS, version)


#: Environment names under which a claude child can authenticate without the Keychain.
#: Presence is all that is read; the values are never printed.
CLAUDE_TOKEN_ENVS = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY")

#: What each vendor does at startup OUTSIDE the worktree, measured under
#: `grok --sandbox workspace` on 2026-09-10 (GRPH-838). Absent here means not measured.
SANDBOX_STARTUP_WRITES = {
    "qwen-code": "chmod ~/.qwen/extension-store/staging",
    "cursor-agent": "mkdir ~/.cursor/projects/<worktree slug>",
}


def check_sandbox(report: Report, adapter: str = "") -> None:
    """GRPH-838: the parent's kernel sandbox is every child's sandbox.

    A Grok session running `[sandbox] profile = "workspace"` hands that Seatbelt profile to
    `gbfleet mcp` and on to every vendor it spawns. Four children died at exit 1 before
    registering, each with a different vendor's symptom in stderr, and the common cause was
    in none of them. This asks the kernel directly, and says per vendor what the answer means
    — because "sandboxed" is a PASS for one vendor, a FAIL for three, and a question for the
    rest. Vendors measured under a sandbox get a verdict; the others get UNKNOWN, which is
    not the same as clean.
    """
    answer = hostos.sandboxed()
    name = "kernel sandbox"
    if answer is None:
        report.add(name, UNKNOWN, f"no way to ask on {platform.system()}",
                   "a child inherits any sandbox this process runs under; a child that exits 1 "
                   "before registering with EPERM outside the worktree is the symptom")
        return
    if not answer:
        report.add(name, PASS, "none")
        return
    home = Path.home()
    recipe = (
        "keep the sandbox and grant the vendor's home — a Grok custom profile with "
        f'extends = "workspace" and read_write = ["{home / ".qwen"}", "{home / ".cursor"}"] '
        "(absolute paths) — or run the parent with its sandbox off"
    )
    if adapter == "grok":
        report.add(name, PASS,
                   "inherited from the parent; the grok child is launched --sandbox off so it "
                   "does not re-apply the profile inside this one, and runs inside it")
    elif adapter == "claude":
        if any(os.environ.get(k) for k in CLAUDE_TOKEN_ENVS):
            report.add(name, PASS,
                       "inherited from the parent; the claude child has a token in its "
                       "environment, which is the one login a sandbox cannot hide")
        else:
            report.add(name, FAIL,
                       "inherited from the parent; a claude child cannot reach the Keychain "
                       "and reads 'Not logged in' even on a machine that is logged in, and no "
                       "path grant restores it",
                       "run the parent with its sandbox off, or give the child "
                       f"{' or '.join(CLAUDE_TOKEN_ENVS)} in its environment")
    elif adapter in SANDBOX_STARTUP_WRITES:
        report.add(name, FAIL,
                   f"inherited from the parent; a {adapter} child dies at startup on "
                   f"`{SANDBOX_STARTUP_WRITES[adapter]}` (EPERM) unless that directory is "
                   "writable from inside", recipe)
    elif adapter:
        report.add(name, UNKNOWN,
                   f"inherited from the parent, and {adapter} has not been measured under one",
                   "if the child exits 1 before registering, its stderr tail names the path "
                   "it could not write; grant it or run the parent with its sandbox off")
    else:
        report.add(name, UNKNOWN,
                   "inherited from the parent; what that does to a child depends on the vendor",
                   "pass --adapter for the verdict on the one you intend to run")


def check_memory(report: Report, per_child: int = headroom.DEFAULT_CHILD_MEMORY) -> None:
    """How many children this machine has room for right now (GRPH-842).

    Here because it is the limit an operator is least able to see and most likely to hit.
    The failure it explains looked like a fleet bug: a wave stopped mid-run with
    "the system is running low on memory", the supervisor gone and its children with it,
    and nothing anywhere saying the box was full.

    UNKNOWN when the host cannot be asked, and `Report.ok` deliberately does not fail on
    it — the gate does not bind on an unmeasured host either, so grounding the fleet here
    would be a stricter rule than the one actually enforced.

    Never FAIL. A full machine is a state the operator can fix in ten seconds and the
    fleet still runs one child on it; refusing to start would turn a slow wave into no
    wave. The gate handles it at spawn time and this says so in advance.
    """
    available = hostos.available_memory()
    name = "memory headroom"
    if available is None:
        report.add(name, UNKNOWN, f"no way to ask on {platform.system()}",
                   "the spawn-time gate does not bind on this host; watch max-workers "
                   "yourself, or the OS will pick which process dies")
        return
    fits = headroom.fits(available, per_child)
    detail = (
        f"{headroom.gigabytes(available)} available, "
        f"{headroom.gigabytes(headroom.RESERVE)} reserved, "
        f"{headroom.gigabytes(per_child)} per child: room for {fits}"
    )
    if fits:
        report.add(name, PASS, detail)
    else:
        # PASS is wrong and FAIL is wrong. The wave will run, with one child, and the
        # operator should know that before it does rather than from the summary after.
        report.add(name, UNKNOWN, detail + " beyond the first",
                   "close what else is running, or expect a one-child wave — the gate "
                   "never blocks the first child")


def check_credential_protection(report: Report) -> None:
    """Whether a seat file can actually be kept to its owner HERE.

    `chmod` is a silent no-op on Windows, on FAT32 and on many network shares, and the
    seat carries a live api key. GRPH-584 made the supervisor report that at spawn; this
    reports it before the operator commits to a run.
    """
    try:
        with tempfile.NamedTemporaryFile(delete=False) as handle:
            probe = Path(handle.name)
        try:
            if hostos.restrict_to_owner(probe) and hostos.is_owner_only(probe):
                report.add("credential files can be kept private", PASS)
            else:
                report.add(
                    "credential files can be kept private", FAIL,
                    f"could not restrict {probe.parent} to this user",
                    "seat files carry a live api key; move the workspace off this "
                    "filesystem, or accept that other users on this host can read them",
                )
        finally:
            probe.unlink(missing_ok=True)
    except OSError as exc:
        report.add("credential files can be kept private", UNKNOWN, str(exc))


def check_repository(report: Report, repo: Path) -> Path | None:
    try:
        root = repo_root(repo)
    except NotARepository as exc:
        report.add("repository", FAIL, str(exc), "run from inside a git repository")
        return None
    except UnsupportedPlatform as exc:
        report.add("repository", FAIL, str(exc))
        return None
    report.add("repository", PASS, str(root))

    # The GRPH-581 trap. A repo that commits a seat path has that file destroyed in every
    # worktree, including any `[permission]` deny rules it sets for its own agents.
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True
        )
        if head.returncode != 0:
            report.add("repository does not commit a seat path", UNKNOWN, "no commits yet")
        else:
            tracked = _tracked_at(root, head.stdout.strip(), SEAT_FILES)
            if tracked:
                report.add(
                    "repository does not commit a seat path", FAIL, f"commits {tracked}",
                    "the supervisor must write a child's seat there and would truncate "
                    "your file; use user-scope config instead (grok mcp add --scope user)",
                )
            else:
                report.add("repository does not commit a seat path", PASS)
    except OSError as exc:
        report.add("repository does not commit a seat path", UNKNOWN, str(exc))
    return root


def check_state_and_lock(report: Report, repo: Path | None) -> None:
    try:
        root = state_root()
    except (OSError, UnsupportedPlatform) as exc:
        report.add("state directory", FAIL, str(exc))
        return
    report.add("state directory", PASS, str(root))

    if repo is None:
        report.add("supervisor lock is free", UNKNOWN, "no repository to lock")
        return
    try:
        # `probe`, not `hold`. hold() writes our pid and truncates on the way out, so a
        # planted crash record would vanish and the next `up` would start blind beside
        # live children. Asking whether the flock is taken does not need to become the
        # holder (GRPH-599).
        probe(repo)
        report.add("supervisor lock is free", PASS)
    except RepoLocked as exc:
        # Named, not merely refused: "someone has this" sends the operator looking for a
        # stale file, and the pid is what actually ends the search.
        report.add("supervisor lock is free", FAIL, str(exc.holder and exc.holder.pid),
                   "another gbfleet holds this repository — stop it, or use another checkout")
    except OSError as exc:
        report.add("supervisor lock is free", UNKNOWN, str(exc))


def check_workspace(report: Report, workspace: Path) -> None:
    try:
        workspace.mkdir(parents=True, exist_ok=True)
        probe = workspace / ".gbfleet-doctor"
        probe.write_text("x", encoding="utf-8")
        probe.unlink()
        report.add("workspace is writable", PASS, str(workspace))
    except OSError as exc:
        report.add("workspace is writable", FAIL, f"{workspace}: {exc}")


def check_adapter(report: Report, name: str) -> None:
    if name not in ADAPTERS:
        report.add("adapter", FAIL, f"{name!r} is not one of {sorted(ADAPTERS)}")
        return
    adapter = ADAPTERS[name]

    # Answered BEFORE resolve, and regardless of whether it succeeds. Whether a vendor
    # has a debug flag is a property of the adapter, not of the binary being installed —
    # and an operator choosing a vendor wants to know before installing it, which is
    # exactly when resolve fails. Reporting it only on success made the answer available
    # only to people who did not need it yet (found by CI, where cursor-agent is absent).
    if adapter.debug_argv(Path("probe.log")):
        report.add(f"adapter {name} supports --debug", PASS)
    else:
        report.add(
            f"adapter {name} supports --debug", UNKNOWN,
            "this vendor has no debug flag",
            "--debug will give this adapter output sampling only, nothing more",
        )

    try:
        found = resolve(name)
    except AdapterError as exc:
        report.add(f"adapter {name}", FAIL, str(exc))
        return
    report.add(f"adapter {name}", PASS, f"{found.binary} — {found.version}")


def check_project(report: Report, server: str, api_key: str | None, project: str) -> None:
    """GRPH-718. A key that reads several projects resolves a call naming none to its
    DEFAULT, which on the walk was not the project the seats were minted on: the child
    registered elsewhere and the supervisor polled a roster it never appeared on."""
    if not api_key:
        report.add("project", UNKNOWN, "no key to ask with")
        return
    client = Graphban(base_url=server, api_key=api_key, allowed=frozenset({"get_context"}))
    try:
        ctx = client.call("get_context")
    except Exception as exc:  # noqa: BLE001 - reported, never fatal here
        report.add("project", UNKNOWN, f"get_context: {str(exc)[:120]}")
        return
    finally:
        client.close()
    readable = [p for p in (ctx.get("readable_projects") or []) if isinstance(p, str)]
    default = ctx.get("project_id")
    if project:
        if readable and project not in readable:
            report.add("project", FAIL, f"--project {project!r} is not readable by this key "
                       f"(readable: {', '.join(readable)})", "mint a key scoped to that project")
        else:
            report.add("project", PASS, f"--project {project}")
    elif len(readable) > 1:
        report.add("project", FAIL,
                   f"this key reads {len(readable)} projects and no --project was given; calls "
                   f"will land on {default!r}, and a child on a seat minted elsewhere never "
                   "appears on the roster this supervisor polls",
                   f"pass --project <id> (one of: {', '.join(readable)})")
    else:
        report.add("project", PASS, f"{default} (the key's only project)")


def check_server(report: Report, server: str, api_key: str | None) -> None:
    if not api_key:
        report.add("api key", FAIL, "$GBFLEET_API_KEY is not set",
                   "export GBFLEET_API_KEY=<a key with the supervisor's scope>")
        report.add("server reachable", UNKNOWN, "no key to try it with")
        return
    report.add("api key", PASS, "set")

    client = Graphban(base_url=server, api_key=api_key)
    try:
        client.fleet_status()
        report.add("server reachable", PASS, server)
    except ServerUnreachable as exc:
        report.add("server reachable", FAIL, f"{server}: {exc}",
                   "the supervisor can run offline, but it cannot START offline")
    except Exception as exc:  # noqa: BLE001 - the tool error shape is the server's
        report.add("server accepts this key", FAIL, str(exc)[:200],
                   "the key must carry the supervisor's scope (fleet_status, "
                   "propose_allocation)")
    finally:
        client.close()


def check_seats(report: Report, seats_file: str | None) -> None:
    if not seats_file:
        report.add("seats file", UNKNOWN, "none given",
                   "`up` needs one; `stdio` mints per spawn through the planner")
        return
    path = Path(seats_file)
    if not path.exists():
        report.add("seats file", FAIL, f"{path} does not exist")
        return
    try:
        # Same rule `up` uses (`cli.read_seats` → `codes_from_text`). Counting every
        # non-blank line made a file of only `#` comments PASS with N seats while
        # `up` read [] and exited 2 (GRPH-599).
        codes = codes_from_text(path.read_text(encoding="utf-8"))
        for line in codes:
            parse_seat_line(line)
    except OSError as exc:
        report.add("seats file", FAIL, f"{path}: {exc}")
        return
    except ValueError as exc:
        # `up` refuses the same line with exit 2; a doctor that counted it would PASS a
        # file the wave then refuses.
        report.add("seats file", FAIL, str(exc))
        return
    if not codes:
        report.add("seats file", FAIL, f"{path} has no seats",
                   "a wave with no seats spawns nothing and reports nothing")
        return
    report.add("seats file", PASS, f"{len(codes)} seat(s) in {path}")


def run(
    *,
    repo: Path,
    workspace: Path | None = None,
    adapter: str = "",
    server: str = "",
    api_key: str | None = None,
    seats_file: str | None = None,
    out=None,
    project: str = "",
    matrix_path: str | None = None,
) -> Report:
    """Every check that can be made without spawning anything.

    `out` resolves at CALL time, not as a default. `out=sys.stdout` in the signature
    binds whatever stdout was when this module was imported, so anything that redirects
    it afterwards — a test harness, a caller capturing output, a log wrapper — gets
    nothing while the report goes somewhere nobody is looking.
    """
    out = sys.stdout if out is None else out
    report = Report()
    print(f"gbfleet {__version__} doctor\n", file=out)

    check_host(report)
    check_memory(report)
    check_sandbox(report, adapter)
    check_credential_protection(report)
    root = check_repository(report, repo)
    check_state_and_lock(report, root)
    check_workspace(
        report,
        Path(workspace) if workspace
        else (root.parent / f"{root.name}-gbfleet" if root else Path("gbfleet-workspace")),
    )
    if adapter:
        check_adapter(report, adapter)
    else:
        report.add("adapter", UNKNOWN, "none named",
                   "pass --adapter to check the vendor you intend to run")
    if server:
        check_server(report, server, api_key)
    if server:
        check_project(report, server, api_key, project)
    else:
        report.add("server reachable", UNKNOWN, "no --server given")
    check_seats(report, seats_file)
    check_supervision_mode(report)
    check_service(report)
    check_matrix(report, matrix_path, server=server, api_key=api_key, project=project)

    report.render(out)
    return report


def check_service(report: Report, name: str = "") -> None:
    """Whether a drain on this box outlives the terminal that started it (GRPH-844).

    The other half of "why is nothing spawning". `check_supervision_mode` says which KIND of
    supervision a wave runs under; this says whether anything supervises the supervisor. A
    fleet whose `until` died with an ssh session looks, from the ledger, exactly like a fleet
    with no ready work.

    Three answers. `not installed` is a PASS, not a failure — running `until` in a terminal is
    a legitimate and common way to work, and failing everyone who does it would make the check
    noise. What is NOT a pass is a service that exists and is not running, or a host nobody
    can ask.
    """
    found = service_mod.host()
    if not found.kind:
        report.add("fleet service", UNKNOWN, found.why,
                   "nothing here can supervise a drain; `gbfleet until` lives and dies with "
                   "the process that starts it, including a harness that may stop it")
        return
    # EVERY drain on this box, not the one whose name we happened to guess. A check that asks
    # about the default name only reports "none installed" beside a broken `nightly`.
    names = [name] if name else service_mod.installed_names(found.kind)
    if not names:
        report.add("fleet service", PASS,
                   f"none installed ({found.kind}); drains run in the foreground",
                   "`gbfleet service install -- <until args>` if you want one that outlives "
                   "this terminal")
        return
    for one in names:
        _one_service(report, service_mod.status(one, kind=found.kind))


def _one_service(report: Report, state) -> None:
    label = f"fleet service ({state.name})"
    if not state.installed:
        report.add(label, PASS, f"not installed ({state.kind})")
        return
    if state.running is None:
        report.add(label, UNKNOWN, f"installed at {state.unit_path}, {state.detail}")
        return
    if not state.path_env:
        # Checked BEFORE the run state, because it is a property of the unit rather than of
        # the last run: a drain with no PATH resolves no adapter whether it happens to be
        # running, idle or dead at the moment somebody looks.
        report.add(label, FAIL, f"installed ({state.kind}) with an EMPTY PATH",
                   "reinstall it from a shell that can see your vendor CLIs; every adapter "
                   "resolution in that service fails")
        return
    if state.idle:
        # NOT a fault, and getting this wrong would have been worse than having no check.
        # `until` exits when there is no ready work, so a healthy drain is not running for
        # most of every cycle; a FAIL here cries wolf exactly when the fleet is correct.
        report.add(label, PASS,
                   f"idle between runs, last exit 0 ({state.kind}, {state.unit_path})")
        return
    if not state.running:
        # `state.detail` already carries the exit code on both platforms; naming it twice
        # read as "last exit 3 — last exit 3".
        report.add(label, FAIL, f"installed and NOT running ({state.detail})",
                   f"read {service_mod.CONFIG_DIR / 'logs'}; a unit that loads and dies is "
                   "usually a PATH with no vendor CLI on it, a key file that went away, or "
                   "another supervisor already holding this repository's lock")
        return
    if state.linger is False:
        # Running now, gone at logout. Reported as UNKNOWN rather than PASS because the thing
        # being asked about — does this survive without me — has not been established.
        report.add(label, UNKNOWN, f"running, but this user has no linger",
                   f"`loginctl enable-linger {hostos.user_tag()}`, or systemd stops it when "
                   "your last session ends — which on a headless box means at logout")
        return
    report.add(label, PASS, f"running ({state.kind}, {state.unit_path})")


def check_supervision_mode(report: Report) -> None:
    """PRD-39 G4 / D-e: the two supervision modes have names, and the cheap one is the default.

    `deterministic` is `gbfleet up` AND `gbfleet until` — no LLM in the loop; `up` runs the
    seats it is handed, `until` mints just in time and stops when there is no ready work, no
    unsigned review and no live lease. `driven` is a planner holding the local stdio server
    (`gbfleet mcp`): a frontier context polls child state and adjudicates bounces for the
    length of the wave, which is the expensive loop. Deterministic is the default; driven is
    the escalation for bounce adjudication and resume. (The first version of this line called
    `until` driven — it is the opposite, and is the whole point of `until`.)
    """
    report.add(
        "supervision mode",
        PASS,
        "deterministic (gbfleet up, gbfleet until: no LLM in the loop) is the default; "
        "driven (a planner on gbfleet mcp) escalates for bounce adjudication and resume",
    )


def _probe_suggestion_lines(report: Report, suggestions: list, *, looked_up: bool) -> None:
    """Criterion 9: a probe suggestion is on the page AND in doctor.

    An empty list is "none" only after a successful lookup. Not looking it up and
    printing "none" would be the absence that reads as a clean result.
    """
    if not looked_up:
        report.add(
            "probe suggestions", UNKNOWN, "not looked up",
            "pass --server and set GBFLEET_API_KEY; nothing runs without a person starting it",
        )
        return
    if not suggestions:
        report.add("probe suggestions", PASS, "none")
        return
    for sug in suggestions:
        if not isinstance(sug, dict):
            continue
        vendor = sug.get("vendor") or ""
        model = sug.get("model") or ""
        trigger = sug.get("trigger") or "new_row"
        version = sug.get("binary_version") or ""
        report.add(
            f"probe {vendor}:{model}",
            PASS,
            f"{trigger}"
            + (f" binary_version={version}" if version else "")
            + " — nothing runs without a person starting it",
        )


def check_matrix(report: Report, matrix_path: str | None = None, *, server: str = "",
                 api_key: str | None = None, project: str = "") -> None:
    """PRD-37 D11: every row against this machine, then what each tier resolves to UNDER THE
    OPERATOR'S PROFILE — read off `fleet_status` exactly as `mcp` and `until` read it at
    launch, so the doctor's answer is the answer a spawn would give. An adapter file that is
    not registered is a line here, never a silence (criterion 2)."""
    from . import matrix as matrix_mod
    from .mcp import read_status

    try:
        mat = matrix_mod.load(Path(matrix_path) if matrix_path else None)
    except Exception as exc:  # noqa: BLE001 - a bad matrix is the finding
        report.add("matrix", FAIL, f"could not load: {str(exc)[:200]}",
                   "fix the row the message names; a verified row needs evidence")
        return
    report.add("matrix", PASS, f"{len(mat.rows)} row(s) from {mat.path}")
    for name in matrix_mod.unregistered_adapter_files():
        if not any(r.harness == name and r.status == "unregistered" for r in mat.rows):
            report.add(f"matrix {name}", FAIL, "an adapter file exists but is not registered and "
                       "has no matrix row saying so", f"add a row with status = \"unregistered\" for {name}")
    profile, policy, measured, bands, cap_measured = None, None, None, None, None
    suggestions: list = []
    looked_up = False
    if server and api_key:
        client = Graphban(base_url=server, api_key=api_key, project_id=project or None)
        try:
            profile, policy, note, measured, bands, cap_measured, suggestions = read_status(client)
            looked_up = "unreachable" not in note
        finally:
            client.close()
        report.add("matrix preferences", PASS if "unreachable" not in note else UNKNOWN, note,
                   "" if "unreachable" not in note else "the resolutions below assume no profile and no policy")
    else:
        report.add("matrix preferences", UNKNOWN, "no server or key: resolving with no profile, no policy, nothing measured",
                   "pass --server and set GBFLEET_API_KEY to see what a spawn would actually resolve")
    _probe_suggestion_lines(report, suggestions, looked_up=looked_up)
    installed = matrix_mod.installed_checker()
    for name, status, detail in matrix_mod.doctor_lines(mat, installed, profile, policy,
                                                        measured, bands, cap_measured):
        report.add(name, status, detail)
