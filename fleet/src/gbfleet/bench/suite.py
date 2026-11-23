"""The bench suite manifest and its loader.

A manifest is TOML: a `[suite]` header (`id`, `version` semver) and `[[task]]` rows.
A build task's reference solution is a ref in the suite's repo; it is never copied
into a child's worktree.
"""

from __future__ import annotations

import hashlib
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

#: Mirror of `CAPABILITY_LEAVES` in backend/app/services/harness.py (PRD-41). gbfleet must
#: not import the backend; a test reads that file as text and asserts these are equal.
CAPABILITY_LEAVES: tuple[str, ...] = (
    "A1", "A2", "A3", "A4", "A5",
    "B1", "B2", "B3", "B4", "B5", "B6", "B7", "B8",
    "C1", "C2", "C3",
    "H1", "H2", "H3", "H4", "H5",
    "E1", "E2", "E3",
    "F1", "F2", "F3",
)

KINDS = ("build", "review")
EXPECTS = ("bounce", "sign_off")
_SEMVER = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")


class SuiteError(ValueError):
    """A manifest the loader refuses. Names the task id and the field where there is one."""


@dataclass(frozen=True)
class Check:
    command: str
    tests: tuple[str, ...] = ()


@dataclass(frozen=True)
class Task:
    id: str
    kind: str
    base_sha: str
    title: str
    description: str
    acceptance: tuple[str, ...]
    touchpoints: tuple[str, ...]
    capabilities: tuple[str, ...]
    size_band: str
    check: Check | None = None
    #: build: the reference solution's ref in the suite's repo.
    reference: str = ""
    #: review: "bounce" or "sign_off", and the planted-defect (bounce) or clean (sign_off) ref.
    expect: str = ""
    ref: str = ""


@dataclass(frozen=True)
class Suite:
    id: str
    version: str
    tasks: tuple[Task, ...] = field(default_factory=tuple)


def _fail(task_id: str, name: str, why: str) -> SuiteError:
    return SuiteError(f"task {task_id!r}: field {name!r} {why}")


def _str(row: dict, tid: str, name: str, *, required: bool = True) -> str:
    val = row.get(name, "")
    if not isinstance(val, str):
        raise _fail(tid, name, "must be a string")
    if required and not val.strip():
        raise _fail(tid, name, "is required")
    return val.strip()


def _strs(row: dict, tid: str, name: str, *, required: bool = False) -> tuple[str, ...]:
    val = row.get(name, [])
    if not isinstance(val, list) or not all(isinstance(v, str) and v.strip() for v in val):
        raise _fail(tid, name, "must be a list of non-empty strings")
    if required and not val:
        raise _fail(tid, name, "is required")
    return tuple(v.strip() for v in val)


def _task(row: dict, index: int) -> Task:
    tid = row.get("id")
    if not isinstance(tid, str) or not tid.strip():
        raise SuiteError(f"task #{index + 1}: field 'id' is required")
    tid = tid.strip()
    kind = _str(row, tid, "kind")
    if kind not in KINDS:
        raise _fail(tid, "kind", f"must be one of {', '.join(KINDS)}, not {kind!r}")
    caps = _strs(row, tid, "capabilities", required=True)
    for cap in caps:
        if cap not in CAPABILITY_LEAVES:
            raise _fail(tid, "capabilities", f"names {cap!r}, which is not a PRD-41 leaf")
    brief = row.get("brief")
    if not isinstance(brief, dict):
        raise _fail(tid, "brief", "is required (title, description, acceptance)")
    title = _str(brief, tid, "title")
    description = _str(brief, tid, "description")
    acceptance = _strs(brief, tid, "acceptance")

    check: Check | None = None
    raw_check = row.get("check")
    if raw_check is not None:
        if not isinstance(raw_check, dict):
            raise _fail(tid, "check", "must be a table with command and tests")
        check = Check(_str(raw_check, tid, "command"), _strs(raw_check, tid, "tests"))

    base_sha = _str(row, tid, "base_sha", required=False)
    reference = expect = ref = ""
    if kind == "build":
        if not base_sha:
            raise _fail(tid, "base_sha", "is required for a build task")
        if check is None:
            raise _fail(tid, "check", "is required for a build task")
        reference = _str(row, tid, "reference")
    else:
        if not base_sha:
            raise _fail(tid, "base_sha", "is required for a review task")
        expect = _str(row, tid, "expect")
        if expect not in EXPECTS:
            raise _fail(tid, "expect", f"must be one of {', '.join(EXPECTS)}, not {expect!r}")
        ref = _str(row, tid, "ref")
    return Task(
        id=tid, kind=kind, base_sha=base_sha, title=title, description=description,
        acceptance=acceptance, touchpoints=_strs(row, tid, "touchpoints"),
        capabilities=caps, size_band=_str(row, tid, "size_band"), check=check,
        reference=reference, expect=expect, ref=ref,
    )


def parse(text: str) -> Suite:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise SuiteError(f"not valid TOML: {exc}") from exc
    head = data.get("suite")
    if not isinstance(head, dict):
        raise SuiteError("missing [suite] header")
    sid, version = head.get("id"), head.get("version")
    if not isinstance(sid, str) or not sid.strip():
        raise SuiteError("[suite] field 'id' is required")
    if not isinstance(version, str) or not _SEMVER.match(version):
        raise SuiteError(f"[suite] field 'version' must be semver, not {version!r}")
    rows = data.get("task", [])
    if not isinstance(rows, list):
        raise SuiteError("'task' must be an array of tables ([[task]])")
    tasks = tuple(_task(r, i) for i, r in enumerate(rows))
    seen: set[str] = set()
    for t in tasks:
        if t.id in seen:
            raise SuiteError(f"task {t.id!r}: field 'id' is duplicated")
        seen.add(t.id)
    return Suite(id=sid.strip(), version=version, tasks=tasks)


def load(path: str | Path) -> Suite:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise SuiteError(f"cannot read {path}: {exc}") from exc
    return parse(text)


#: The shipped template suite (PRD-98 section 2) lives in package data next to its fixture-repo
#: generator, README and `hash.toml`, which records the content hash beside the version.
TEMPLATE_ID = "gbfleet-template"
#: A suite that claims a leaf needs at least this many tasks for it (PRD-41's floor).
LEAF_FLOOR = 5
_HASHED = ("suite.toml", "fixture.py", "README.md")


def template_dir() -> Path:
    return Path(__file__).with_name("template")


def template_manifest() -> Path:
    return template_dir() / "suite.toml"


def content_hash(directory: Path | None = None) -> str:
    """sha256 over the template's manifest, fixture generator and README, name-prefixed."""
    d = directory or template_dir()
    h = hashlib.sha256()
    for name in _HASHED:
        h.update(name.encode() + b"\0" + (d / name).read_bytes().replace(b"\r\n", b"\n") + b"\0")
    return h.hexdigest()


def recorded_hash(directory: Path | None = None) -> tuple[str, str]:
    """(version, content_hash) as recorded in `hash.toml`."""
    data = tomllib.loads(((directory or template_dir()) / "hash.toml").read_text(encoding="utf-8"))
    return str(data.get("version", "")), str(data.get("content_hash", ""))


def leaf_floor_problems(suite: Suite) -> list[str]:
    """One line per claimed leaf with fewer than LEAF_FLOOR tasks. Empty means fine."""
    counts: dict[str, int] = {}
    for t in suite.tasks:
        for cap in t.capabilities:
            counts[cap] = counts.get(cap, 0) + 1
    return [f"leaf {leaf} is claimed by {n} task(s); at least {LEAF_FLOOR} are needed"
            for leaf, n in sorted(counts.items()) if n < LEAF_FLOOR]
