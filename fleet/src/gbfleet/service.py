"""Running a drain under the machine's own supervisor (GRPH-844, GRPH-852).

`gbfleet until` is a long-running process that, until now, existed only for as long as the
terminal or harness that started it. That is not a detail — it is the root of GRPH-842: the
wave was a background task a harness owned, so the harness stopped it when the machine ran
short of memory, and the children went with it. Nothing in the fleet could say *run this
under launchd, restart it if it dies, and put the log here*.

The API server has had this since PRD-? (`scripts/graphban_host.py`). This is the fleet's,
and it is a SUBCOMMAND where that one is a stdlib script, for a reason that does not
generalise: `graphban_host.py` has to create the venv the console script would live in, so it
cannot be that console script. `uv tool install graphban-fleet` already gives gbfleet its own
environment, so the binary is there before anyone asks it to write a unit.

**User domain only.** LaunchAgent, `systemd --user`, Task Scheduler (`schtasks`) on Windows.
A root / Session-0 installer is a different program with different failure modes:
`docs/native-install.md` records that a privileged install was never walked even for the
server, and on Windows a `sc.exe` service runs where no vendor CLI's login lives (GRPH-852).

**Nothing in this file may write a credential into a unit file.** `until` takes its key only
from `$GBFLEET_API_KEY`, so the key goes in an owner-only environment file beside the unit and
the unit references it: `EnvironmentFile=` on systemd, and on launchd / Task Scheduler —
which have no equivalent — a shell that sources it. That asymmetry is annoying and it is the
correct trade: `scripts/graphban_systemd.py` refuses a secret in a unit because a unit is
world-readable, and putting one in `EnvironmentVariables` would be the same mistake wearing a
plist or a task XML.

**The PATH is load-bearing and is the thing most likely to be got wrong.** A launchd job does
not inherit your shell's environment; it gets a minimal PATH. Every vendor CLI the fleet exists
to run — `claude`, `cursor-agent`, `qwen`, `grok` — lives somewhere that PATH does not contain
(`~/.local/bin`, `/opt/homebrew/bin`). A service installed without capturing it starts
cleanly, is reported running, and fails every adapter resolution: the exact "installs, appears
to start, does nothing" shape `graphban_service.py` was written against after a daemon served
the wrong port. So the installing shell's PATH is recorded in the unit, and `status` prints it.
"""
from __future__ import annotations

import os
import plistlib
import shlex
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from .hostos import WINDOWS, is_owner_only, restrict_to_owner, user_tag
from .state import repo_root

#: One service per name, so two clones of a repository can each have one. The default is what
#: an operator with a single drain gets without thinking about it.
DEFAULT_NAME = "drain"

#: Every unit this program writes is named `gbfleet-<name>.service`,
#: `dev.graphban.fleet.<name>.plist`, or `gbfleet-<name>.xml` (Task Scheduler), and that is
#: not cosmetic: it is what makes them ENUMERABLE. A `doctor` that only ever asks about the
#: default name reports "none installed" on a box with a broken `drain-b` on it, which is the
#: absence-reads-as-clean failure this repository names in its own guide.
UNIT_PREFIX = "gbfleet-"

#: launchd labels are a reverse-DNS namespace shared by the whole machine. `dev.graphban.api`
#: is the server's; the fleet takes its own leaf rather than a suffix on that one, because
#: `launchctl bootout dev.graphban.api` should not be able to reach a fleet.
LABEL_PREFIX = "dev.graphban.fleet"

#: Default Windows logon policy. Named in every status line so it is never guessed:
#: InteractiveToken ≈ LaunchAgent (dies at logout). "Run whether logged on" would be the
#: linger analogue — and then every vendor CLI that needs DPAPI / a profile MUST be walked;
#: until that walk exists, that mode is not offered (GRPH-852).
LOGON_WHEN_LOGGED_ON = "run only when the user is logged on"

#: Marker written into the task XML so `status` can read PATH back without assuming. A Task
#: Scheduler schema has no EnvironmentVariables element the way a launchd plist does.
_PATH_COMMENT = "gbfleet-path:"

#: `schtasks /Query /V` reports this Last Result while the action is still running.
_SCHED_S_TASK_RUNNING = 267009

#: Durable, and deliberately NOT `state.state_root()`. That lives under `tempfile.gettempdir()`,
#: which is exactly right for a lock that must not survive a reboot and exactly wrong for a
#: service's credential: `/tmp` is cleared on boot on many Linuxes and swept by macOS, and a
#: service whose environment file vanishes starts, fails to authenticate, and restarts forever.
CONFIG_DIR = Path.home() / ".config" / "gbfleet"

#: Seconds between runs, and it is the one number here an operator should actually think
#: about. `until` EXITS when there is no ready work — that is success, not a crash — so a
#: service restarts it, and the delay IS the polling interval.
#:
#: **Every cycle costs a planner agent row.** `services/fleet.py:register_agent` says it
#: plainly: *"Always creates a row. Never reuses one by label"* — for a good reason, since two
#: terminals on one key are two agents. So a 15s restart is 240 rows an hour against a project
#: with nothing to do, which is the shape of the PRD-39 walk finding (63 registrations, no
#: ready work). Five minutes trades pickup latency for that, and `--every` moves it.
RESTART_SEC = 300

#: How long `install` waits before asking whether the job survived. Not tuning: `until`
#: reaches the repo lock, the server and its first refusal in well under a second, so an
#: immediate read reports "running" for a service that is already gone.
SETTLE = 2.0

#: What `until` is given if the operator passes nothing else. Nothing: an empty install is
#: refused, because a service running `gbfleet until` with no server, project or repo is a
#: crash loop with a unit file.
_REQUIRED_ARGS = ("--repo",)

API_KEY_ENV = "GBFLEET_API_KEY"

#: Shortest key the literal-in-unit check will look for. See `install`: below this a substring
#: search over the unit is noise rather than evidence.
_MIN_KEY = 8


#: Reimplemented rather than imported from `scripts/graphban_systemd.py`, which does the same
#: job for the server: `fleet/` is a separate distribution with its own install surface (PRD-22
#: D-e) and cannot import from `scripts/`. The markers are kept identical on purpose — a leak
#: this list catches for the server and not for the fleet is a leak.
SECRET_MARKERS = ("secret", "password", "token", "api_key", "apikey", "private")


def secrets_in(rendered: bytes) -> list[str]:
    """Anything in a unit that looks like a credential. Over what LANDS ON DISK, parsed back.

    **Assignments / element names, never raw substrings**, and that distinction is not
    fussiness: the first version grepped the rendered text and refused every install on
    macOS, because `SECRET_MARKERS` contains `private` and every path under `/private/var`
    matches it. A guard that fires on `/private/tmp` is a guard someone will disable.

    So the bytes are parsed back into the shape they were written in — a plist is walked for
    KEYS, the way `scripts/graphban_service.py` walks it; a Task Scheduler XML is walked for
    element local-names the same way; and a unit file is split on the assignments the way
    `scripts/graphban_systemd.py` splits it. `EnvironmentFile` is exempt: it names the path
    that keeps the key OUT, and a marker firing on the fix is worse than no marker.

    This finds a credential put somewhere credentials go. It cannot find one smuggled through
    an argument, which is why `install` ALSO checks for the literal key it is about to write.
    """
    if rendered[:6] == b"bplist":
        try:
            return _plist_secrets(plistlib.loads(rendered))
        except (ValueError, plistlib.InvalidFileException):
            return []
    is_utf16 = rendered[:2] in (b"\xff\xfe", b"\xfe\xff")
    is_xml = is_utf16 or rendered.lstrip()[:5] == b"<?xml"
    if is_xml and not is_utf16 and b"<plist" in rendered[:200]:
        try:
            return _plist_secrets(plistlib.loads(rendered))
        except (ValueError, plistlib.InvalidFileException):
            return []
    if is_xml:
        # Task Scheduler XML (and anything else XML-shaped that is not a plist). Walk element
        # NAMES — text content is where WorkingDirectory paths live, and "private" in a path
        # is the false positive this function exists to refuse.
        try:
            return _xml_secrets(rendered)
        except ET.ParseError:
            return []
    found: list[str] = []
    for raw in rendered.decode("utf-8", "replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().lower()
        if key == "environmentfile":
            continue
        target = value.partition("=")[0] if key == "environment" else key
        if any(marker in target.lower() for marker in SECRET_MARKERS):
            found.append(line)
    return found


def _plist_secrets(node, path: str = "") -> list[str]:
    """Keys that look like a credential, at any depth. Keys, because that is where one goes.

    `EnvironmentVariables` is a nested dict and is the obvious place somebody would put a key,
    which is exactly why the walk has to descend rather than check the top level.
    """
    found: list[str] = []
    if isinstance(node, dict):
        for k, v in node.items():
            if any(marker in str(k).lower() for marker in SECRET_MARKERS):
                found.append(f"{path}{k}")
            found += _plist_secrets(v, f"{path}{k}.")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            found += _plist_secrets(v, f"{path}{i}.")
    return found


def _xml_secrets(rendered: bytes) -> list[str]:
    """Element local-names that look like a credential, at any depth."""
    text = rendered.decode("utf-16" if rendered[:2] in (b"\xff\xfe", b"\xfe\xff")
                           else "utf-8", "replace")
    root = ET.fromstring(text)
    found: list[str] = []

    def walk(node: ET.Element, path: str) -> None:
        local = node.tag.rsplit("}", 1)[-1]
        here = f"{path}{local}"
        if any(marker in local.lower() for marker in SECRET_MARKERS):
            found.append(here)
        for child in node:
            walk(child, f"{here}.")

    walk(root, "")
    return found


class Refused(RuntimeError):
    """The install cannot be made correctly. Says which fact makes it impossible."""


@dataclass(frozen=True)
class Host:
    """Which user-domain supervisor this machine has, or why it has none."""

    kind: str = ""      #: "launchd" | "systemd" | "schtasks" | ""
    why: str = ""       #: filled only when kind is empty

    @property
    def supervised(self) -> bool:
        return bool(self.kind)


def host() -> Host:
    """What supervises a user job here.

    Windows is Task Scheduler (`schtasks`), never a Session-0 `sc.exe` service (GRPH-852). A
    Windows Service runs where no vendor CLI's login lives; the analogue of `--user` /
    LaunchAgent is a per-user scheduled task with InteractiveToken. An unsupervised host —
    no `schtasks`, no launchctl, no usable systemd — stays UNKNOWN, never "not installed".
    """
    if WINDOWS:
        if shutil.which("schtasks"):
            return Host("schtasks")
        return Host("", "no schtasks on PATH; Task Scheduler is how a drain outlives the "
                        "terminal on Windows")
    if sys.platform == "darwin":
        if shutil.which("launchctl"):
            return Host("launchd")
        return Host("", "no launchctl on PATH")
    if not shutil.which("systemctl"):
        return Host("", "no systemctl on PATH; this box has no systemd to hand a unit to")
    done = _run(["systemctl", "--user", "show-environment"])
    if done[0] != 0:
        # A container, a box with no user bus, or a session systemd cannot reach. Named
        # rather than guessed: the message it printed is the only thing that distinguishes
        # them, and writing a unit nobody will load is the failure this refuses.
        return Host("", f"`systemctl --user` is not usable here: {done[1] or 'no output'}")
    return Host("systemd")


def _run(cmd: list[str], timeout: float = 20.0) -> tuple[int, str]:
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)
    return done.returncode, (done.stdout + done.stderr).strip()


def linger() -> bool | None:
    """Whether this user's services survive their last logout. None if we cannot ask.

    **The headless gotcha, and the reason `status` prints it every time.** A `systemd --user`
    unit belongs to the user's session manager, which is torn down when the last session ends
    — so a service installed over SSH runs beautifully until you disconnect, and is then
    stopped, enabled, and not running. Everything reads fine. `loginctl enable-linger` is the
    one-line fix and nothing else substitutes for it.

    macOS has no equivalent question: a LaunchAgent in `~/Library/LaunchAgents` is bound to
    the GUI login session, not to an ssh session, so `None` there means "not applicable" —
    which `status` renders rather than hiding.
    """
    if sys.platform == "darwin" or not shutil.which("loginctl"):
        return None
    code, out = _run(["loginctl", "show-user", str(os.getuid()), "--property=Linger"])
    if code != 0 or "=" not in out:
        return None
    return out.rsplit("=", 1)[1].strip().lower() == "yes"


@dataclass(frozen=True)
class Plan:
    """Everything a install would write, computed before anything is written."""

    name: str
    kind: str
    label: str
    unit_path: Path
    env_path: Path
    log_path: Path
    err_path: Path
    binary: str
    until_args: list[str]
    repo: Path
    common_dir: Path
    path_env: str
    every: int = RESTART_SEC

    @property
    def argv(self) -> list[str]:
        return [self.binary, "until", *self.until_args]


def label_for(name: str) -> str:
    return f"{LABEL_PREFIX}.{name}"


def task_name(name: str) -> str:
    """Task Scheduler `/TN` name. Same prefix as the systemd unit stem."""
    return f"{UNIT_PREFIX}{name}"


def unit_name(name: str) -> str:
    """The systemd unit's file name. Prefixed so `installed_names` can find it again."""
    return f"{UNIT_PREFIX}{name}.service"


def unit_dir(kind: str) -> Path:
    if kind == "launchd":
        return Path.home() / "Library" / "LaunchAgents"
    if kind == "schtasks":
        # Ours, not Task Scheduler's private store: enumerable the same way the other two
        # platforms are, and redirectable in tests so a suite never touches the real scheduler
        # (GRPH-844 hygiene, GRPH-852).
        return CONFIG_DIR / "tasks"
    return Path.home() / ".config" / "systemd" / "user"


def unit_path_for(name: str, kind: str) -> Path:
    if kind == "launchd":
        return unit_dir(kind) / f"{label_for(name)}.plist"
    if kind == "schtasks":
        return unit_dir(kind) / f"{task_name(name)}.xml"
    return unit_dir(kind) / unit_name(name)


def runner_path_for(name: str) -> Path:
    """Windows cmd wrapper that sources the env file and sets PATH. Not used on POSIX."""
    return CONFIG_DIR / f"{name}.cmd"


def installed_names(kind: str = "") -> list[str]:
    """Every drain installed here, whatever it is called.

    The reason `status` and `doctor` do not take a name and ask about one: an operator who ran
    `--name nightly` and forgot has a service this program wrote and cannot see, and the
    quiet reading — "none installed" — is the reassuring one.
    """
    kind = kind or host().kind
    if not kind:
        return []
    directory = unit_dir(kind)
    if not directory.is_dir():
        return []
    names: list[str] = []
    if kind == "launchd":
        for path in directory.glob(f"{LABEL_PREFIX}.*.plist"):
            names.append(path.name[len(LABEL_PREFIX) + 1:-len(".plist")])
    elif kind == "schtasks":
        for path in directory.glob(f"{UNIT_PREFIX}*.xml"):
            names.append(path.name[len(UNIT_PREFIX):-len(".xml")])
    else:
        for path in directory.glob(f"{UNIT_PREFIX}*.service"):
            names.append(path.name[len(UNIT_PREFIX):-len(".service")])
    return sorted(n for n in names if n)


def env_path_for(name: str) -> Path:
    return CONFIG_DIR / f"{name}.env"


def make_plan(
    until_args: list[str],
    *,
    name: str = DEFAULT_NAME,
    kind: str = "",
    binary: str | None = None,
    path_env: str | None = None,
    every: int = RESTART_SEC,
) -> Plan:
    """Work out what would be written, refusing anything that cannot be right.

    Every refusal here is a failure that would otherwise be discovered by a service that
    starts, restarts, and never does any work.
    """
    kind = kind or host().kind
    if not kind:
        raise Refused(host().why)
    if every < 1:
        raise Refused(
            f"--every must be at least 1 second (got {every}); a zero delay makes a unit that "
            "fails at startup respawn as fast as the supervisor can fork it")
    if not until_args:
        raise Refused(
            "nothing to run: pass `until`'s own arguments after the command, e.g.\n"
            "  gbfleet service install -- --repo /srv/graphban --server http://host:8080 "
            "--project graphban --adapter claude --max-workers 2")
    missing = [flag for flag in _REQUIRED_ARGS if flag not in until_args]
    if missing:
        raise Refused(
            f"{' and '.join(missing)} must be given: a unit has no working directory worth "
            "inheriting, so a service without an explicit repository would drain whatever "
            "the supervisor happened to start it in")
    repo = _value_of(until_args, "--repo")
    if repo is None or not Path(repo).is_absolute():
        raise Refused(
            f"--repo must be an absolute path (got {repo!r}). A relative one is resolved "
            "against the supervisor's working directory, not yours, and the service would "
            "silently work on a different repository or none at all")
    root = Path(repo)
    if not root.exists():
        raise Refused(f"--repo {root} does not exist")
    # RESOLVED, and the resolved form is what the unit carries. `/srv/repo/fleet/..` is
    # absolute and passes every check above while reading as a different directory to
    # everyone who opens the file, including the person debugging why the wrong repository
    # is being drained.
    root = root.resolve()
    until_args = _replace_value(until_args, "--repo", str(root))
    binary = binary or _this_gbfleet()
    try:
        common = repo_root(root)
    except Exception as exc:  # noqa: BLE001 — reported as a refusal, not a traceback
        raise Refused(f"--repo {root} is not inside a git repository ({exc})") from exc
    return Plan(
        name=name,
        kind=kind,
        label=label_for(name),
        unit_path=unit_path_for(name, kind),
        env_path=env_path_for(name),
        log_path=CONFIG_DIR / "logs" / f"{name}.log",
        err_path=CONFIG_DIR / "logs" / f"{name}.err.log",
        binary=binary,
        until_args=list(until_args),
        repo=root,
        common_dir=common,
        # Captured, never defaulted. See the module docstring: the vendor CLIs are not on a
        # supervisor's PATH, and every one of them is the reason this service exists.
        path_env=path_env if path_env is not None else os.environ.get("PATH", ""),
        every=every,
    )


def _this_gbfleet() -> str:
    """The gbfleet that will run in the unit. THIS one, wherever possible.

    Three sources, in this order, and the order is the point:

    1. `sys.argv[0]`, when it is an executable called `gbfleet`. The operator typed a program;
       installing a different one is not a thing to do quietly.
    2. The console script beside `sys.executable` — the same environment's `bin/gbfleet`. This
       is the `python -m gbfleet.cli` case, where argv[0] is a source file no supervisor can
       exec, and it is also every venv that is not on PATH. Without it, a checkout's own
       gbfleet is invisible while a `uv tool` copy elsewhere gets installed instead.
    3. `shutil.which`, the ambient answer, last.

    Reaching PATH first would install a DIFFERENT gbfleet than the one that is running — a
    `uv tool` copy while you stand in a checkout's venv, which is exactly the situation this
    gets developed in.
    """
    running = Path(sys.argv[0])
    if running.name.startswith("gbfleet") and running.is_file():
        resolved = running.resolve()
        # `os.access(X_OK)` is not the right question on Windows: a console script is a
        # launcher `.exe`, and the extensionless name is not what CreateProcess will run.
        if WINDOWS or os.access(resolved, os.X_OK):
            return str(resolved)
    # NOT `.resolve()` — a venv's `bin/python` is a symlink to the interpreter it was made
    # from, so resolving lands in that interpreter's bin and the venv's own console script
    # becomes invisible. Measured: it fell through to PATH and picked the `uv tool` copy.
    # pip writes `gbfleet.exe` on Windows, not an extensionless shim.
    for script in ("gbfleet", "gbfleet.exe"):
        sibling = Path(sys.executable).parent / script
        if sibling.is_file() and (WINDOWS or os.access(sibling, os.X_OK)):
            return str(sibling)
    found = shutil.which("gbfleet")
    if not found:
        raise Refused(
            "cannot find an absolute path to gbfleet. A unit cannot run a name it has to "
            "look up on a PATH it does not have — install it with "
            "`uv tool install graphban-fleet` and try again")
    return str(Path(found).resolve())


def _replace_value(args: list[str], flag: str, value: str) -> list[str]:
    out = list(args)
    for i, arg in enumerate(out):
        if arg == flag and i + 1 < len(out):
            out[i + 1] = value
            return out
        if arg.startswith(f"{flag}="):
            out[i] = f"{flag}={value}"
            return out
    return out


def _value_of(args: list[str], flag: str) -> str | None:
    for i, arg in enumerate(args):
        if arg == flag:
            return args[i + 1] if i + 1 < len(args) else None
        if arg.startswith(f"{flag}="):
            return arg.split("=", 1)[1]
    return None


def render(plan: Plan) -> bytes:
    if plan.kind == "launchd":
        return _render_launchd(plan)
    if plan.kind == "schtasks":
        return _render_schtasks(plan)
    return _render_systemd(plan).encode()


def _iso_duration(seconds: int) -> str:
    """Task Scheduler wants `PnYnMnDTnHnMnS`. Prefer whole minutes when the delay is one."""
    if seconds >= 60 and seconds % 60 == 0:
        return f"PT{seconds // 60}M"
    return f"PT{seconds}S"


def _render_schtasks_runner(plan: Plan) -> str:
    """cmd.exe wrapper: source the owner-only env file, set PATH, run until once.

    Task Scheduler has no EnvironmentFile and no Restart=always. The env file stays out of
    the task XML (world-readable via `schtasks /Query /XML` on some hosts); PATH is set here
    AND marked in the XML comment so `status` can read it back. The task's LogonTrigger
    Repetition is what restarts after `until` exits — including exit 0, which is idle success.
    """
    env = str(plan.env_path)
    path = plan.path_env.replace('"', "")
    binary = str(plan.binary)
    args = " ".join(f'"{a}"' if (" " in a or "\\" in a) else a for a in plan.until_args)
    # `for /f` reads KEY=VALUE lines; the env file is exactly one such line today.
    return (
        "@echo off\r\n"
        f"rem {_PATH_COMMENT}{path}\r\n"
        f'if exist "{env}" for /f "usebackq eol=# tokens=1,* delims==" %%a in ("{env}") '
        f'do set "%%a=%%b"\r\n'
        f'set "PATH={path}"\r\n'
        f'"{binary}" until {args}\r\n'
    )


def _render_schtasks(plan: Plan) -> bytes:
    """Task Scheduler 1.2 XML. InteractiveToken, never a Session-0 service.

    **LogonType=InteractiveToken** is the LaunchAgent analogue: the task runs in the logged-on
    user's session, where vendor CLI logins live, and stops at logout. S4U / password-based
    "run whether logged on" is the linger analogue and is NOT offered until a walk proves the
    vendor CLIs survive it — DPAPI and profile-scoped settings die there (GRPH-852).

    **No `sc.exe`, no service account.** A Windows Service is Session 0; that is the refusal
    this replaces, not an implementation option.

    Repetition on the LogonTrigger is `--every`: `until` exits when idle, IgnoreNew skips a
    fire while a run is still going, and the next interval starts the next cycle.
    """
    runner = runner_path_for(plan.name)
    interval = _iso_duration(plan.every)
    # UTF-16 LE with BOM is what `schtasks /Create /XML` accepts most reliably.
    path_comment = f"{_PATH_COMMENT}{plan.path_env}"
    xml = f"""<?xml version="1.0" encoding="UTF-16"?>
<!-- {path_comment} -->
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <URI>\\{task_name(plan.name)}</URI>
    <Description>Graphban fleet drain ({plan.name}) — user-domain Task Scheduler, not a Session-0 service</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <Repetition>
        <Interval>{interval}</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
    </LogonTrigger>
    <RegistrationTrigger>
      <Enabled>true</Enabled>
    </RegistrationTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RestartOnFailure>
      <Interval>{interval}</Interval>
      <Count>3</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{_xml_escape(os.environ.get("ComSpec", r"C:\\Windows\\System32\\cmd.exe"))}</Command>
      <Arguments>/d /c "{_xml_escape(str(runner))}"</Arguments>
      <WorkingDirectory>{_xml_escape(str(plan.repo))}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""
    return xml.encode("utf-16")


def _xml_escape(value: str) -> str:
    return (value.replace("&", "&amp;")
                 .replace("<", "&lt;")
                 .replace(">", "&gt;")
                 .replace('"', "&quot;"))


def _unit_text(unit: bytes) -> str:
    """Decode a unit for literal searches. UTF-16 for Task Scheduler; UTF-8 otherwise."""
    if unit[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return unit.decode("utf-16", "replace")
    return unit.decode("utf-8", "replace")


def _render_systemd(plan: Plan) -> str:
    """The unit.

    **No `User=`.** A `--user` unit already runs as that user, and naming one makes systemd
    try to set supplementary groups it may not have — every start dies `216/GROUP`, *Failed at
    step GROUP … Operation not permitted*. Learned on a real install of the server's unit and
    written down there; repeated here because the next person to add `User=` will be doing it
    for the same reasonable-sounding reason.

    `EnvironmentFile` rather than `Environment=`: the key lives in a file only this user can
    read, and a unit file may never carry one.
    """
    return f"""[Unit]
Description=Graphban fleet drain ({plan.name})
Documentation=https://github.com/asc-me/graphban/blob/main/fleet/README.md
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory={plan.repo}
# The vendor CLIs are not on a supervisor's PATH. Captured from the shell that installed
# this; without it every adapter resolution fails and the service busy-restarts.
Environment=PATH={plan.path_env}
# The API key, in a file only this user can read. NEVER `Environment=` — a unit file is
# world-readable, and `scripts/graphban_systemd.py:secrets_in` exists to keep it that way.
EnvironmentFile={plan.env_path}
ExecStart={' '.join(shlex.quote(a) for a in plan.argv)}
# `until` EXITS when there is no ready work, and that is success, not a crash. `on-failure`
# would therefore stop the drain the first time the backlog emptied. `always` plus a delay
# makes it a poller — and the delay is the polling interval, not just a spin guard. Each
# cycle registers one planner agent, so this number is a real cost, not a formality.
Restart=always
RestartSec={plan.every}
StandardOutput=append:{plan.log_path}
StandardError=append:{plan.err_path}

[Install]
WantedBy=default.target
"""


def _render_launchd(plan: Plan) -> bytes:
    """The job description.

    **`RunAtLoad` and `KeepAlive` are both required** — one alone gives you a service that
    looks installed and is not running. **No `UserName`**: that is a LaunchDaemon key, and
    naming it on an agent asks launchd for a privilege it will not give. Both facts are the
    server's, paid for on a real install (`scripts/graphban_service.py`).

    `sh -c` is here for one reason: launchd has no `EnvironmentFile`, and the alternative is
    `EnvironmentVariables` with a live API key inside a plist. The shell sources the same
    owner-only file the systemd unit reads, so there is one place a credential lives on this
    machine rather than two shapes of one.
    """
    sourced = (
        f"set -a; . {shlex.quote(str(plan.env_path))}; set +a; "
        f"exec {' '.join(shlex.quote(a) for a in plan.argv)}"
    )
    job = {
        "Label": plan.label,
        "ProgramArguments": ["/bin/sh", "-c", sourced],
        "WorkingDirectory": str(plan.repo),
        "RunAtLoad": True,
        "KeepAlive": True,
        # launchd's `Restart=always` plus systemd's `RestartSec`. Without it launchd uses its
        # own 10s floor, and the two platforms would back off at different rates from the
        # same unit description — a difference nobody would find until one of them was the
        # one hammering a server that was down.
        "ThrottleInterval": plan.every,
        "StandardOutPath": str(plan.log_path),
        "StandardErrorPath": str(plan.err_path),
        # PATH only. Nothing here is a secret and nothing here may become one.
        "EnvironmentVariables": {"PATH": plan.path_env},
    }
    return plistlib.dumps(job, sort_keys=True)


@dataclass
class Installed:
    """What actually happened, including the parts that are warnings."""

    plan: Plan
    running: bool | None = None
    detail: str = ""
    warnings: list[str] = field(default_factory=list)
    #: The full reading taken after the load, so the caller can tell an idle drain from a
    #: dead one without asking a second time and getting a different answer.
    state: "Status | None" = None


def write_env(plan: Plan, api_key: str) -> list[str]:
    """The credential file. Returns warnings, never raises on a permission it could not set."""
    plan.env_path.parent.mkdir(parents=True, exist_ok=True)
    restrict_to_owner(plan.env_path.parent)
    plan.env_path.write_text(f"{API_KEY_ENV}={api_key}\n", encoding="utf-8")
    restrict_to_owner(plan.env_path)
    # Asked for, then CHECKED. The platform where the request quietly does nothing is exactly
    # the platform where nobody was looking (GRPH-584), and this file holds a live key.
    if not is_owner_only(plan.env_path):
        return [f"{plan.env_path} could not be restricted to you and may be readable by "
                f"others on this host; it contains a live API key"]
    return []


def install(plan: Plan, api_key: str) -> Installed:
    """Write, load, and then ask the supervisor whether it is actually running.

    **Accepted is not running**, on both platforms. `launchctl bootstrap` returning 0 means
    launchd took the job, not that it stayed up; `systemctl --user enable --now` succeeds for
    a unit that exits immediately. So the last step is a fresh `status()` read, and its answer
    is what gets reported.
    """
    if not api_key:
        raise Refused(
            f"${API_KEY_ENV} is not set in this shell. The service needs it, and it is read "
            "from your environment now rather than typed on a command line, because argv is "
            "world-readable")
    unit = render(plan)
    # Two guards, and the second is the one that cannot be fooled. `secrets_in` finds a
    # credential put where credentials go; this finds THIS key wherever it ended up —
    # smuggled through an `until` argument, say, which is a shape no marker list covers.
    #
    # Length-gated, because a substring search over a whole unit file is only meaningful for
    # a string long enough not to occur by accident. Found on the walk: `GBFLEET_API_KEY=x`
    # refused the install, since "x" appears in every path in the file. A real key is
    # `gbk_` plus entropy; anything shorter than `_MIN_KEY` is not one, and letting a
    # nonsense key through this check costs nothing — it will fail at the server instead.
    #
    # Decoded, not raw bytes: Task Scheduler XML is UTF-16, and a UTF-8 search over it would
    # miss a key that is sitting in the file in plain sight (GRPH-852).
    if len(api_key) >= _MIN_KEY and api_key in _unit_text(unit):
        raise Refused(
            "refusing to write a unit that contains the API key itself. It reached the file "
            "through an argument rather than the environment file; a unit is world-readable")
    leaked = secrets_in(unit)
    if leaked:
        raise Refused(
            "refusing to write a unit that would carry a credential: "
            + "; ".join(leaked)
            + ". A unit file is world-readable; the key belongs in the environment file")
    warnings = write_env(plan, api_key)
    plan.log_path.parent.mkdir(parents=True, exist_ok=True)
    plan.unit_path.parent.mkdir(parents=True, exist_ok=True)
    plan.unit_path.write_bytes(unit)
    if plan.kind == "schtasks":
        # The runner holds PATH and sources the env file. Written beside the env file, removed
        # with uninstall, never handed to a test that did not ask for a real scheduler.
        runner = runner_path_for(plan.name)
        runner.write_text(_render_schtasks_runner(plan), encoding="utf-8", newline="\r\n")
        restrict_to_owner(runner)
        detail = _load_schtasks(plan)
    elif plan.kind == "launchd":
        detail = _load_launchd(plan)
    else:
        detail = _load_systemd(plan)
    # A beat before asking. `until` reaches the lock, the server and its first decision in
    # well under a second, so an immediate read is a coin flip between "running" and whatever
    # it became — and the walk that found this got "running" from `install` and "NOT running"
    # from `status` one command later, describing the same healthy service two ways.
    time.sleep(SETTLE)
    state = status(plan.name, kind=plan.kind)
    if plan.kind == "systemd" and linger() is False:
        warnings.append(
            "this user has no linger, so systemd stops your services when your last session "
            f"ends — a drain installed over ssh dies at logout. Fix it with "
            f"`loginctl enable-linger {user_tag()}`")
    if plan.kind == "schtasks":
        warnings.append(
            f"logout policy: {LOGON_WHEN_LOGGED_ON} — the LaunchAgent analogue; the task "
            "stops when this user logs out. \"Run whether logged on\" is not offered: vendor "
            "CLIs that need a profile have not been walked in that mode")
    return Installed(plan=plan, running=state.running, detail=detail, warnings=warnings,
                     state=state)


def _load_launchd(plan: Plan) -> str:
    domain = f"gui/{os.getuid()}"
    # Idempotent: a re-install replaces a running job rather than failing against it. The
    # error from booting out something that is not loaded is not interesting.
    _run(["launchctl", "bootout", f"{domain}/{plan.label}"])
    code, out = _run(["launchctl", "bootstrap", domain, str(plan.unit_path)])
    if code != 0:
        # BOOTOUT IS ASYNCHRONOUS. Bootstrapping into the gap fails with a message that names
        # the wrong problem ("service already loaded"), and an identical retry a moment later
        # succeeds. The server's installer learned this the same way.
        code, out = _run(["launchctl", "bootstrap", domain, str(plan.unit_path)])
    return out


def _load_systemd(plan: Plan) -> str:
    _run(["systemctl", "--user", "daemon-reload"])
    code, out = _run(["systemctl", "--user", "enable", "--now", unit_name(plan.name)])
    if code != 0:
        return out
    return _run(["systemctl", "--user", "restart", unit_name(plan.name)])[1]


def _load_schtasks(plan: Plan) -> str:
    """Register the XML with Task Scheduler. Never `sc.exe`.

    `/F` replaces an existing task of the same name so re-install is idempotent. `/Create`
    alone does not always start it (RegistrationTrigger should, and does not always); `/Run`
    is the enable --now equivalent.
    """
    tn = task_name(plan.name)
    code, out = _run(["schtasks", "/Create", "/TN", tn, "/XML", str(plan.unit_path), "/F"])
    if code != 0:
        return out
    run_code, run_out = _run(["schtasks", "/Run", "/TN", tn])
    return (out + ("\n" + run_out if run_out else "")).strip() or f"registered {tn}"


@dataclass(frozen=True)
class Status:
    """Three answers about a service, never two.

    `installed=None` and `running=None` both mean *nobody could ask*, which is not the same as
    *no* — a box with no user-domain supervisor and a box with a stopped service look
    identical to anything that reports a bare boolean.
    """

    name: str
    kind: str
    installed: bool | None = None
    running: bool | None = None
    unit_path: Path | None = None
    detail: str = ""
    linger: bool | None = None
    path_env: str = ""
    #: What it exited with last time, or None if nobody could say. **The field that makes
    #: `running: False` readable.** `until` exits when there is no ready work, so a healthy
    #: drain is NOT running for most of every cycle — and reporting that as a fault would
    #: make the check cry wolf at exactly the moment the fleet is behaving correctly. Zero
    #: means idle between runs; non-zero means it died.
    last_exit: int | None = None
    #: Windows logout policy, named rather than guessed (GRPH-852). Empty on POSIX.
    logon_policy: str = ""

    @property
    def idle(self) -> bool:
        """Installed, not running, and last exited cleanly: the normal state between cycles."""
        return bool(self.installed) and self.running is False and self.last_exit == 0

    def line(self) -> str:
        where = self.kind
        if self.logon_policy:
            where = f"{self.kind}, {self.logon_policy}"
        if not self.kind:
            return f"{self.name}: unknown — {self.detail}"
        if not self.installed:
            return f"{self.name}: not installed ({where})"
        if self.running is None:
            return f"{self.name}: installed at {self.unit_path}, running unknown — {self.detail}"
        if self.running:
            return f"{self.name}: running ({where}, {self.unit_path})"
        if self.idle:
            return (f"{self.name}: idle between runs, last exit 0 ({where}, "
                    f"{self.unit_path})")
        exit_code = "unknown" if self.last_exit is None else str(self.last_exit)
        return f"{self.name}: NOT running, last exit {exit_code} ({where}, {self.unit_path})"


def status(name: str = DEFAULT_NAME, *, kind: str = "") -> Status:
    found = host()
    kind = kind or found.kind
    policy = LOGON_WHEN_LOGGED_ON if kind == "schtasks" else ""
    if not kind:
        return Status(name=name, kind="", detail=found.why)
    unit = unit_path_for(name, kind)
    if not unit.exists():
        return Status(name=name, kind=kind, installed=False, unit_path=unit,
                      linger=linger(), path_env=_path_in(unit, kind),
                      logon_policy=policy)
    if kind == "launchd":
        alive, detail, last_exit = _running_launchd(label_for(name))
    elif kind == "schtasks":
        alive, detail, last_exit = _running_schtasks(name)
    else:
        alive, detail, last_exit = _running_systemd(name)
    return Status(name=name, kind=kind, installed=True, running=alive, unit_path=unit,
                  detail=detail, linger=linger(), path_env=_path_in(unit, kind),
                  last_exit=last_exit, logon_policy=policy)


def _path_in(unit: Path, kind: str) -> str:
    """The PATH the installed unit carries, read back from disk rather than assumed.

    Printed by `status` because a service that cannot find `claude` is the failure this whole
    module is most likely to produce, and the evidence is one line in a file nobody opens.
    """
    if not unit.exists():
        return ""
    try:
        if kind == "launchd":
            job = plistlib.loads(unit.read_bytes())
            return str((job.get("EnvironmentVariables") or {}).get("PATH", ""))
        if kind == "schtasks":
            raw = unit.read_bytes()
            text = raw.decode("utf-16" if raw[:2] in (b"\xff\xfe", b"\xfe\xff")
                              else "utf-8", "replace")
            for line in text.splitlines():
                if _PATH_COMMENT in line:
                    return line.split(_PATH_COMMENT, 1)[1].split("-->")[0].strip()
            return ""
        for line in unit.read_text(encoding="utf-8").splitlines():
            if line.startswith("Environment=PATH="):
                return line.split("=", 2)[2]
    except (OSError, ValueError):
        return ""
    return ""


def _running_systemd(name: str) -> tuple[bool | None, str, int | None]:
    code, out = _run(["systemctl", "--user", "is-active", unit_name(name)])
    if out in ("active", "activating"):
        return True, out, None
    if out in ("inactive", "failed", "deactivating"):
        return False, out, _last_exit_systemd(name)
    return None, out or f"systemctl said nothing (exit {code})", None


def _last_exit_systemd(name: str) -> int | None:
    """`ExecMainStatus` — what the last run returned. None when systemd will not say."""
    code, out = _run(["systemctl", "--user", "show", unit_name(name),
                      "--property=ExecMainStatus"])
    if code != 0 or "=" not in out:
        return None
    try:
        return int(out.rsplit("=", 1)[1].strip())
    except ValueError:
        return None


def _running_launchd(label: str) -> tuple[bool | None, str, int | None]:
    """`launchctl list` prints `PID Status Label`, and a crash-looping job is still listed.

    A `-` in the PID column is a job launchd knows about and is not running — which is why
    presence in the listing is not the answer. The server's installer reported "installed"
    against exactly that row.

    The middle column is the LAST EXIT STATUS, and for this service it is the whole story: a
    drain that exits 0 is idle between cycles, and one that exits non-zero is broken. Both
    look identical from the PID column alone.
    """
    code, out = _run(["launchctl", "list"])
    if code != 0:
        return None, out, None
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[2] == label:
            try:
                last = int(parts[1])
            except ValueError:
                last = None
            if parts[0] == "-":
                return False, f"last exit {parts[1]}", last
            return True, f"pid {parts[0]}", last
    return False, "not listed by launchd", None


def _running_schtasks(name: str) -> tuple[bool | None, str, int | None]:
    """`schtasks /Query /V /FO LIST` — Status and Last Result.

    Last Result `267009` (`SCHED_S_TASK_RUNNING`) means the action is still going; that is
    not an exit code. Ready + Last Result 0 is idle between cycles, the same reading
    launchd's `- 0 label` row gives.
    """
    code, out = _run(["schtasks", "/Query", "/TN", task_name(name), "/V", "/FO", "LIST"])
    if code != 0:
        lowered = out.lower()
        if "cannot find" in lowered or "does not exist" in lowered or "cannot be found" in lowered:
            return False, "not registered with Task Scheduler", None
        return None, out or f"schtasks said nothing (exit {code})", None
    status_word = ""
    last_raw = ""
    for line in out.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip().lower()
        value = value.strip()
        if key == "status":
            status_word = value
        elif key in ("last result", "last run result"):
            last_raw = value
    last: int | None = None
    if last_raw:
        try:
            # schtasks prints decimal, sometimes with leading spaces; also accept 0xHEX.
            last = int(last_raw, 0)
        except ValueError:
            last = None
    if last == _SCHED_S_TASK_RUNNING:
        last = None
    word = status_word.lower()
    if word.startswith("running"):
        return True, status_word or "Running", last
    if word.startswith("ready") or word.startswith("queued") or word.startswith("disabled"):
        return False, f"last exit {last_raw or 'unknown'}", last
    if not status_word:
        return None, out or "schtasks returned no Status line", last
    return None, status_word, last


def uninstall(name: str = DEFAULT_NAME, *, kind: str = "") -> list[str]:
    """Stop it and remove what was written. Returns what was actually removed.

    **The environment file goes too.** It exists only to feed this unit, and a live API key
    left in `~/.config` after an uninstall is the kind of leftover nobody goes looking for.
    On Windows the cmd runner that sources it is removed with it.
    """
    found = host()
    kind = kind or found.kind
    if not kind:
        raise Refused(found.why)
    removed: list[str] = []
    unit = unit_path_for(name, kind)
    if kind == "launchd":
        _run(["launchctl", "bootout", f"gui/{os.getuid()}/{label_for(name)}"])
    elif kind == "schtasks":
        _run(["schtasks", "/Delete", "/TN", task_name(name), "/F"])
    else:
        _run(["systemctl", "--user", "disable", "--now", unit_name(name)])
    paths = [unit, env_path_for(name)]
    if kind == "schtasks":
        paths.append(runner_path_for(name))
    for path in paths:
        if path.exists():
            path.unlink()
            removed.append(str(path))
    if kind == "systemd":
        _run(["systemctl", "--user", "daemon-reload"])
    return removed
