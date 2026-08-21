"""What this package promises about itself, made falsifiable.

PRD-22 D-e and G6. Three claims live here and each one is the sort that stops being
true quietly: the version it reports, the size of what it drags onto a laptop, and
its separation from the backend distribution it shares a repository with.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import gbfleet
from gbfleet import UNINSTALLED

REPO = Path(__file__).resolve().parents[2]
FLEET_PYPROJECT = REPO / "fleet" / "pyproject.toml"
BACKEND_PYPROJECT = REPO / "backend" / "pyproject.toml"
FLEET_SRC = REPO / "fleet" / "src"

# The one dependency both distributions may share. Everything else in the backend's
# list is the reason this package is not inside `backend/`.
ALLOWED_SHARED = {"httpx"}


def _toml(path: Path) -> dict:
    return tomllib.loads(path.read_text(encoding="utf-8"))


def _dep_names(specs: list[str]) -> set[str]:
    """`fastapi>=0.115` and `psycopg[binary]>=3.2` both reduce to their package name."""
    return {re.split(r"[<>=!~\[;\s]", s, maxsplit=1)[0].strip().lower() for s in specs}


def _console_script(name: str) -> Path:
    """The installed entry point in whichever venv is running these tests.

    Checks existence here rather than in each caller: a missing script otherwise
    surfaces as a FileNotFoundError from inside subprocess, which names the path but
    not the reason, and every caller would need the same guard to say so.
    """
    script = Path(sys.executable).parent / name
    assert script.exists(), (
        f"console script `{name}` is not installed at {script}. "
        "Run: uv pip install -e '.[dev]' from fleet/"
    )
    return script


# --- the version it reports -------------------------------------------------------


def test_the_suite_refuses_to_run_against_an_uninstalled_package():
    """The fallback must not be able to pass for a real version.

    `__version__` degrades to `UNINSTALLED` when package metadata is missing, which is
    the honest thing for a source checkout to report — but it also means every version
    assertion below would be comparing two fallbacks and agreeing. This test is the one
    that notices, so a suite run without an install fails loudly rather than vacuously.
    """
    assert gbfleet.__version__ != UNINSTALLED, (
        "graphban-fleet is not installed in this environment, so the version tests "
        "below would assert nothing. Run: uv pip install -e '.[dev]' from fleet/"
    )


def test_the_reported_version_is_the_one_pyproject_declares():
    """One source of truth, and something that objects when a second appears.

    Also catches a stale editable install, where the metadata on disk has drifted from
    the file a reader would check.
    """
    declared = _toml(FLEET_PYPROJECT)["project"]["version"]
    assert gbfleet.__version__ == declared


def test_the_entry_point_is_wired_and_reports_that_version():
    """Proves `[project.scripts]` resolves, which importing the module cannot."""
    script = _console_script("gbfleet")
    result = subprocess.run(
        [str(script), "--version"], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"gbfleet {gbfleet.__version__}"


def test_a_bare_invocation_says_what_is_wrong_rather_than_only_failing():
    """Exit 2 with a bare usage string reads identically to subcommands failing to
    register. It has to name which of the two happened."""
    script = _console_script("gbfleet")
    result = subprocess.run([str(script)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 2
    assert "no commands are available" in result.stderr


# --- the size of what it installs -------------------------------------------------


def test_the_install_surface_stays_thin():
    """G6: no fastapi, no sqlalchemy, no pgvector on a laptop.

    The forbidden set is DERIVED from the backend's dependency list rather than written
    out here, so a heavy dependency added to `graphban-api` tomorrow is covered by this
    test today without anyone remembering to extend it. The cost is that fleet cannot
    quietly share a new dependency with the backend — which is the point: that should be
    a decision someone makes, not one that happens.
    """
    backend_deps = _dep_names(_toml(BACKEND_PYPROJECT)["project"]["dependencies"])
    fleet_deps = _dep_names(_toml(FLEET_PYPROJECT)["project"]["dependencies"])

    forbidden = backend_deps - ALLOWED_SHARED
    assert forbidden, "backend declared no dependencies — this guard would assert nothing"

    leaked = fleet_deps & forbidden
    assert not leaked, (
        f"graphban-fleet declares {sorted(leaked)}, which graphban-api also pulls. "
        "PRD-22 D-e: a laptop running vendor CLIs needs none of the backend stack. "
        "If one of these is genuinely needed, add it to ALLOWED_SHARED with a reason."
    )


def test_importing_gbfleet_does_not_drag_in_the_backend_stack():
    """The declared list is not the whole story — a transitive pull or a stray import
    of `app.*` would leave the list thin and the runtime fat."""
    backend_deps = _dep_names(_toml(BACKEND_PYPROJECT)["project"]["dependencies"])
    forbidden = (backend_deps - ALLOWED_SHARED) | {"app"}

    probe = (
        "import sys, json; import gbfleet.cli; "
        "print(json.dumps(sorted({m.split('.')[0] for m in sys.modules})))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr

    loaded = set(json.loads(result.stdout))
    assert "gbfleet" in loaded, "probe did not import the package it was meant to test"

    leaked = sorted(loaded & forbidden)
    assert not leaked, f"importing gbfleet pulled in {leaked}"


# --- separation from the backend distribution -------------------------------------


def test_the_console_script_names_do_not_collide():
    """`graphban` and `agentledger` are claimed by backend/pyproject.toml, and
    `agentledger` is kept indefinitely so an operator's runbook keeps working (AL-262).
    Two distributions installing the same script name is a coin toss at install time."""
    fleet_scripts = set(_toml(FLEET_PYPROJECT)["project"].get("scripts", {}))
    backend_scripts = set(_toml(BACKEND_PYPROJECT)["project"].get("scripts", {}))
    assert fleet_scripts and backend_scripts
    assert not fleet_scripts & backend_scripts


def test_no_fleet_module_imports_the_backend():
    """Static companion to the import probe: same repo is exactly what makes
    `from app.services import fleet` an easy and undetectable mistake, and a lazy
    import inside a function would not show up at import time."""
    sources = sorted(FLEET_SRC.rglob("*.py"))
    assert sources, f"no python sources under {FLEET_SRC} — this guard scanned nothing"

    offenders = []
    for path in sources:
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.match(r"\s*(from\s+app[\s.]|import\s+app[\s.]|from\s+app$)", line):
                offenders.append(f"{path.relative_to(REPO)}:{lineno}: {line.strip()}")
    assert not offenders, "fleet must not import the backend package:\n" + "\n".join(
        offenders
    )


def test_the_fleet_licence_is_deliberately_not_the_repo_licence():
    """PRD-22 §8, decided. `fleet/` is Apache-2.0 while the repository is
    FSL-1.1-Apache-2.0 — a divergence that looks like an oversight and would be
    'tidied up' by anyone who did not know it was a choice. See fleet/LICENSE."""
    assert _toml(FLEET_PYPROJECT)["project"]["license"] == "Apache-2.0"
    licence = REPO / "fleet" / "LICENSE"
    assert licence.exists()
    assert "Apache License" in licence.read_text(encoding="utf-8")
    assert _toml(BACKEND_PYPROJECT)["project"]["license"] != "Apache-2.0"
