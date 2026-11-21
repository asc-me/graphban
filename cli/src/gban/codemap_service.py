"""`gban code map service`: keep the code graph current without anyone remembering (GRPH-1821).

The graph went stale because nothing refreshed it. This installs ONE user-level unit per repo:
a launchd LaunchAgent on macOS, a systemd user timer plus service on Linux. It fires every hour
and once a night, and both fire the same command, `gban code map service run`. `run` picks the
mode: the nightly full reconcile (re-hash every file, retire gone paths) if today's nightly
time has passed and no full run has succeeded since, otherwise `--since` (GRPH-1820), which costs
nothing when origin's default branch has not moved. A machine that was asleep at 03:00 therefore
reconciles at its next tick rather than skipping the night.

**The API key never goes in a unit.** It lives in an owner-only env file the unit references
(`EnvironmentFile=` on systemd; a shell that sources it on launchd, which has no equivalent) —
the same trade `gbfleet service` makes, and the unit is checked for the literal key before it is
written. **A failed run is recorded as failed, never as current**, and there is no in-run retry:
the next cycle is the retry. Every run appends one JSON line to a log under the gban state dir,
and `status` reads that log back; it never claims "current" for a map that has not run.
"""
from __future__ import annotations

import hashlib
import json
import os
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from gban import client as _client
from gban import codemap, config

API_KEY_ENV = "GBFLEET_API_KEY"  # what `gban code map` reads (doctor.SUPERVISOR_KEY_ENV)
DEFAULT_HOURLY_MINUTE = 17
DEFAULT_NIGHTLY = (3, 0)
LABEL_PREFIX = "dev.graphban.codemap"
UNIT_PREFIX = "gban-codemap-"


class Refused(RuntimeError):
    """The install cannot be made correctly; says which fact makes it so."""


# --- identity and paths ---------------------------------------------------------------------

def repo_toplevel(repo: Path) -> Path:
    try:
        top = codemap._git(Path(repo), "rev-parse", "--show-toplevel").decode().strip()
    except codemap.MapError as exc:
        raise Refused(f"{repo} is not inside a git repository ({exc})") from exc
    return Path(top).resolve()


def service_id(repo: Path) -> str:
    """One unit per repo: the same repo always maps to the same id."""
    resolved = Path(repo).resolve()
    slug = re.sub(r"[^A-Za-z0-9]+", "-", resolved.name).strip("-").lower() or "repo"
    return f"{slug}-{hashlib.sha1(str(resolved).encode()).hexdigest()[:8]}"  # nosec B324


def state_dir() -> Path:
    return config.home() / "codemap"


def log_path(sid: str) -> Path:
    return state_dir() / f"{sid}.log"


def env_path(sid: str) -> Path:
    return state_dir() / f"{sid}.env"


def host_kind() -> str:
    if sys.platform == "darwin":
        if shutil.which("launchctl"):
            return "launchd"
        raise Refused("no launchctl on PATH")
    if sys.platform.startswith("linux"):
        if shutil.which("systemctl"):
            return "systemd"
        raise Refused("no systemctl on PATH; this box has no systemd to hand a unit to")
    raise Refused(f"no user-level scheduler supported on {sys.platform} "
                  "(launchd and systemd only)")


def unit_paths(sid: str, kind: str) -> list[Path]:
    home = Path.home()
    if kind == "launchd":
        return [home / "Library" / "LaunchAgents" / f"{LABEL_PREFIX}.{sid}.plist"]
    base = home / ".config" / "systemd" / "user"
    return [base / f"{UNIT_PREFIX}{sid}.service", base / f"{UNIT_PREFIX}{sid}.timer"]


def _run(cmd: list[str], timeout: float = 20.0) -> tuple[int, str]:
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)
    return done.returncode, (done.stdout + done.stderr).strip()


# --- the plan -------------------------------------------------------------------------------

def parse_hourly(text: str | None) -> int:
    if text is None:
        return DEFAULT_HOURLY_MINUTE
    m = re.fullmatch(r":?(\d{1,2})", text.strip())
    if not m or int(m.group(1)) > 59:
        raise Refused(f"--hourly-at {text!r}: expected :MM, a minute 0-59")
    return int(m.group(1))


def parse_nightly(text: str | None) -> tuple[int, int]:
    if text is None:
        return DEFAULT_NIGHTLY
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", text.strip())
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise Refused(f"--nightly-at {text!r}: expected HH:MM, 24-hour local time")
    return int(m.group(1)), int(m.group(2))


@dataclass(frozen=True)
class Plan:
    sid: str
    kind: str
    repo: Path
    argv: list[str]
    path_env: str
    hourly: int
    nightly: tuple[int, int]
    units: list[Path]
    env_file: Path
    log_file: Path
    out_file: Path
    err_file: Path


def _gban_argv() -> list[str]:
    running = Path(sys.argv[0])
    if running.name.startswith("gban") and running.is_file() and os.access(running, os.X_OK):
        return [str(running.resolve())]
    found = shutil.which("gban")
    if found:
        return [str(Path(found).resolve())]
    return [sys.executable, "-m", "gban.cli"]


def make_plan(repo: Path, *, url: str, project: str, hourly: str | None = None,
              nightly: str | None = None, kind: str = "", path_env: str | None = None,
              gban: list[str] | None = None) -> Plan:
    kind = kind or host_kind()
    path_env = os.environ.get("PATH", "") if path_env is None else path_env
    if not path_env.strip():
        # A supervisor's PATH is minimal; an install that captured nothing starts, runs, and
        # cannot find git. Refused here rather than discovered at 03:00.
        raise Refused("PATH is empty in this shell; the unit would not find git. "
                      "Run the install from a shell with your normal PATH")
    if not url:
        raise Refused("no server: pass --server, set $GRAPHBAN_URL, or run `gban login`")
    minute = parse_hourly(hourly)
    night = parse_nightly(nightly)
    if night[1] == minute:
        raise Refused(f"--hourly-at :{minute:02d} and --nightly-at minute {night[1]:02d} would "
                      "fire two runs at once; pick different minutes")
    top = repo_toplevel(repo)
    sid = service_id(top)
    argv = [*(gban or _gban_argv()), "--server", url]
    if project:
        argv += ["--project", project]
    argv += ["code", "map", "service", "run", "--repo", str(top),
             "--nightly-at", f"{night[0]:02d}:{night[1]:02d}"]
    sd = state_dir()
    return Plan(sid=sid, kind=kind, repo=top, argv=argv, path_env=path_env, hourly=minute,
                nightly=night, units=unit_paths(sid, kind), env_file=env_path(sid),
                log_file=log_path(sid), out_file=sd / f"{sid}.out.log",
                err_file=sd / f"{sid}.err.log")


def _shell_line(plan: Plan) -> str:
    return (f"set -a; . {shlex.quote(str(plan.env_file))}; set +a; "
            f"exec {' '.join(shlex.quote(a) for a in plan.argv)}")


def render_launchd(plan: Plan) -> bytes:
    job = {
        "Label": f"{LABEL_PREFIX}.{plan.sid}",
        "ProgramArguments": ["/bin/sh", "-c", _shell_line(plan)],
        "WorkingDirectory": str(plan.repo),
        "RunAtLoad": False,
        "StartCalendarInterval": [
            {"Minute": plan.hourly},
            {"Hour": plan.nightly[0], "Minute": plan.nightly[1]},
        ],
        "StandardOutPath": str(plan.out_file),
        "StandardErrorPath": str(plan.err_file),
        "EnvironmentVariables": {"PATH": plan.path_env},
    }
    return plistlib.dumps(job, sort_keys=True)


def render_systemd(plan: Plan) -> tuple[str, str]:
    service = f"""[Unit]
Description=Graphban code map refresh ({plan.repo.name})
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory={plan.repo}
Environment=PATH={plan.path_env}
# The API key, in a file only this user can read. NEVER `Environment=`: a unit is world-readable.
EnvironmentFile={plan.env_file}
ExecStart={' '.join(shlex.quote(a) for a in plan.argv)}
StandardOutput=append:{plan.out_file}
StandardError=append:{plan.err_file}
"""
    timer = f"""[Unit]
Description=Graphban code map refresh schedule ({plan.repo.name})

[Timer]
OnCalendar=*-*-* *:{plan.hourly:02d}:00
OnCalendar=*-*-* {plan.nightly[0]:02d}:{plan.nightly[1]:02d}:00
Persistent=true

[Install]
WantedBy=timers.target
"""
    return service, timer


def _unit_files(plan: Plan) -> list[tuple[Path, bytes]]:
    if plan.kind == "launchd":
        return [(plan.units[0], render_launchd(plan))]
    service, timer = render_systemd(plan)
    return [(plan.units[0], service.encode()), (plan.units[1], timer.encode())]


# --- install / uninstall --------------------------------------------------------------------

def _private_file(path: Path, text: str) -> list[str]:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, config.PRIVATE)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    config._make_private(path)
    if not config.is_private(path):
        return [f"{path} could not be restricted to you; it contains a live API key"]
    return []


def installed(sid: str, kind: str) -> bool:
    return unit_paths(sid, kind)[0].exists()


def install(plan: Plan, api_key: str) -> list[str]:
    """Write the key file and the unit(s), load them. Returns warnings."""
    if not api_key:
        raise Refused("no project-write API key. Set $GBFLEET_API_KEY or $GRAPHBAN_API_KEY, "
                      "or run `gban setup`")
    files = _unit_files(plan)
    if len(api_key) >= 8:
        for _, data in files:
            if api_key in data.decode("utf-8", "replace"):
                raise Refused("refusing to write a unit that contains the API key itself")
    state_dir().mkdir(parents=True, exist_ok=True)
    warnings = _private_file(plan.env_file, f"{API_KEY_ENV}={api_key}\n")
    for path, data in files:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    if plan.kind == "launchd":
        domain = _gui_domain()
        label = f"{LABEL_PREFIX}.{plan.sid}"
        _run(["launchctl", "bootout", f"{domain}/{label}"])
        code, out = _run(["launchctl", "bootstrap", domain, str(plan.units[0])])
        if code != 0:  # bootout is asynchronous; one retry
            code, out = _run(["launchctl", "bootstrap", domain, str(plan.units[0])])
    else:
        _run(["systemctl", "--user", "daemon-reload"])
        code, out = _run(["systemctl", "--user", "enable", "--now",
                          f"{UNIT_PREFIX}{plan.sid}.timer"])
    if code != 0:
        warnings.append(f"the supervisor did not accept the unit: {out}")
    return warnings


def _gui_domain() -> str:
    """launchd's per-user GUI domain. `os.getuid` is POSIX-only; a launchd plan
    can be built and asserted on any host, so it must not raise elsewhere."""
    getuid = getattr(os, "getuid", None)
    return f"gui/{getuid() if getuid else 0}"


def uninstall(sid: str, kind: str = "") -> list[str]:
    kind = kind or host_kind()
    if kind == "launchd":
        _run(["launchctl", "bootout", f"{_gui_domain()}/{LABEL_PREFIX}.{sid}"])
    else:
        _run(["systemctl", "--user", "disable", "--now", f"{UNIT_PREFIX}{sid}.timer"])
    removed = []
    for path in [*unit_paths(sid, kind), env_path(sid)]:
        if path.exists():
            path.unlink()
            removed.append(str(path))
    if kind == "systemd":
        _run(["systemctl", "--user", "daemon-reload"])
    return removed


# --- the run: one JSON line per run ---------------------------------------------------------

def read_log(sid: str) -> list[dict]:
    try:
        text = log_path(sid).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out = []
    for ln in text.splitlines():
        try:
            doc = json.loads(ln)
        except ValueError:
            continue
        if isinstance(doc, dict):
            out.append(doc)
    return out


def append_log(sid: str, record: dict) -> None:
    path = log_path(sid)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")


def choose_mode(now: datetime, nightly: tuple[int, int], records: list[dict]) -> str:
    """`full` once today's nightly time has passed with no successful full run since it."""
    due = now.replace(hour=nightly[0], minute=nightly[1], second=0, microsecond=0)
    if now < due:
        return "since"
    for rec in reversed(records):
        if rec.get("mode") == "full" and rec.get("outcome") == "ok":
            return "since" if rec.get("ts", 0) >= due.timestamp() else "full"
    return "full"


def _counting(call, state: dict):
    def counted(tool, args):
        reply = call(tool, args)
        if tool == "describe_code" and "nodes" in args:
            state["done"] += len(args["nodes"])
        return reply
    return counted


def refresh(repo: Path, call, mode: str) -> dict:
    """One run. Never raises for a map failure: the outcome says what happened."""
    rec: dict = {"mode": mode, "outcome": "failed"}
    try:
        ref, target = codemap.default_target(repo)
    except codemap.FetchError as exc:
        rec.update(stage="fetch", reason=str(exc))
        return rec
    except codemap.MapError as exc:
        rec.update(stage="target", reason=str(exc), fetch_ok=True)
        return rec
    rec.update(fetch_ok=True, sha=target, ref=ref)
    state = {"done": 0}
    total = 0
    try:
        counted = _counting(call, state)
        current = call("get_code_map", {"limit": 0})
        existing = {n["path"]: n for n in current.get("nodes", [])}
        pinned, why = codemap.pinned_revision(current)
        if mode == "since" and pinned == target:
            rec.update(outcome="ok", nodes=0, note="current")
            return rec
        reason = "" if mode == "full" else codemap.fallback_reason(repo, pinned, why, target)
        if mode == "full" or reason:
            built = codemap.build(repo, target)
            planned = codemap.plan(built, existing)
        else:
            built = {"revision": target}
            planned = codemap.plan_since(repo, pinned, target, existing)
        total = len(planned["nodes"])
        codemap.apply(counted, built, planned)
        rec.update(outcome="ok", nodes=total)
    except codemap.MapError as exc:
        _failed(rec, "map", str(exc), state["done"], total)
    except (_client.Unreachable, _client.Refused, _client.NoSession) as exc:
        # The server is down, refused the key, or there is no session: still one log line,
        # recorded as failed (or partial), so status never reads the previous run as current.
        _failed(rec, "server", f"{type(exc).__name__}: {exc}", state["done"], total)
    return rec


def _failed(rec: dict, stage: str, reason: str, done: int, total: int) -> None:
    rec.update(stage=stage, reason=reason, nodes_done=done, nodes_total=total)
    if done > 0:
        rec["outcome"] = "partial"


def record_run(sid: str, repo: Path, call, nightly: tuple[int, int],
               now: datetime | None = None) -> dict:
    now = now or datetime.now()
    mode = choose_mode(now, nightly, read_log(sid))
    rec = refresh(repo, call, mode)
    rec["ts"] = now.timestamp()
    rec["at"] = now.isoformat(timespec="seconds")
    append_log(sid, rec)
    return rec


# --- status ---------------------------------------------------------------------------------

def _local_default(repo: Path) -> str:
    """origin's default branch as last fetched. No fetch: status must not touch the network."""
    try:
        return codemap._git(repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD").decode().strip()
    except codemap.MapError:
        for cand in ("origin/main", "origin/master"):
            try:
                codemap._git(repo, "rev-parse", "--verify", "--quiet", cand)
                return cand
            except codemap.MapError:
                continue
    return ""


def behind(repo: Path, mapped: str) -> int | None:
    ref = _local_default(repo)
    if not ref or not mapped:
        return None
    try:
        return int(codemap._git(repo, "rev-list", "--count", f"{mapped}..{ref}").decode().strip())
    except (codemap.MapError, ValueError):
        return None


def _stamp(ts: float) -> str:
    return datetime.fromtimestamp(ts).isoformat(timespec="seconds")


def status_lines(repo: Path, kind: str = "") -> list[str]:
    top = repo_toplevel(repo)
    sid = service_id(top)
    kind = kind or host_kind()
    unit = unit_paths(sid, kind)[0]
    head = [f"{sid}: " + (f"installed ({kind}, {unit})" if unit.exists()
                          else f"not installed ({kind})")]
    records = read_log(sid)
    if not records:
        return ["never run", *head]
    last = records[-1]
    lines: list[str] = []
    when = last.get("at", "?")
    if last.get("outcome") == "failed":
        lines.append(f"last run failed: {last.get('stage', '?')}: {last.get('reason', '')} ({when})")
    elif last.get("outcome") == "partial":
        lines.append(f"partial: {last.get('nodes_done', 0)} of {last.get('nodes_total', 0)} "
                     f"nodes at {str(last.get('sha', ''))[:12]} ({when}): "
                     f"{last.get('reason', '')}")
    else:
        lines.append(f"last run ok ({last.get('mode')}, {when}): mapped {str(last.get('sha', ''))[:12]}")
    mapped = ""
    for rec in reversed(records):
        if rec.get("outcome") == "ok" and rec.get("sha"):
            mapped = rec["sha"]
            break
    if mapped:
        n = behind(top, mapped)
        fetched = next((r for r in reversed(records) if r.get("fetch_ok")), None)
        if n is None:
            lines.append(f"behind: unknown (mapped {mapped[:12]} is not comparable with origin)")
        elif last.get("stage") == "fetch" and fetched:
            lines.append(f"behind by {n} commits as of the last successful fetch at "
                         f"{fetched.get('at', _stamp(fetched.get('ts', 0)))}")
        else:
            lines.append(f"behind by {n} commits")
    else:
        lines.append("no run has completed; the map is not known to be current")
    return [*lines, *head]
