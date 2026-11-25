"""What this package promises about itself, made falsifiable.

PRD-22 D-e and G6. Four claims live here and each one is the sort that stops being
true quietly: the version it reports, the size of what it drags onto a laptop,
its separation from the backend distribution it shares a repository with, and the
per-test ceiling its own suite runs under.
"""

from __future__ import annotations

import json
import re
import subprocess
import os
import sys
import tomllib
from pathlib import Path

import gbfleet
from gbfleet import UNINSTALLED
from conftest import console_script  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
FLEET_PYPROJECT = REPO / "fleet" / "pyproject.toml"
BACKEND_PYPROJECT = REPO / "backend" / "pyproject.toml"
FLEET_SRC = REPO / "fleet" / "src"

# The one dependency both distributions may share. Everything else in the backend's
# list is the reason this package is not inside `backend/`.
ALLOWED_SHARED = {"httpx"}

# The window the per-test ceiling in fleet/pyproject.toml's `[tool.pytest.ini_options]` has
# to sit inside (GRPH-1934). A window and not the literal `600`, so retuning the ceiling
# stays a pyproject edit. Restated here rather than imported from backend/tests/
# test_ci_shape.py, which owns the backend's equivalent: fleet must not import the backend
# distribution (see test_no_fleet_module_imports_the_backend), and the two floors rest on
# different measurements.
#   floor — above this suite's longest documented wait, a `subprocess.run(timeout=180)`
#     around `grok mcp doctor` in tests/test_grok_seat.py (tests/test_adopt.py joins a
#     sweeper thread at 120s). Under it a slow vendor binary becomes a red job, which is a
#     test defect and not a hang. `timeout = 0` is pytest-timeout's own "no ceiling", so the
#     floor refuses it as hard as an absent key does.
#   ceiling — exclusive, and one hour: the largest bound the job-level guard in
#     backend/tests/test_ci_shape.py allows any suite job (MAX_TIMEOUT_MINUTES = 60). At or
#     above the job's own `timeout-minutes`, CI kills the job first and names no test — the
#     failure the per-test ceiling exists to prevent.
MIN_PER_TEST_SECONDS = 200
MAX_PER_TEST_SECONDS = 3600


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
    # `.exe` on Windows: pip writes a launcher, not an extensionless shim. Asking for
    # the bare name there fails for a reason that has nothing to do with packaging.
    script = console_script(name)
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
    register. It has to name which of the two happened, and point somewhere."""
    script = _console_script("gbfleet")
    result = subprocess.run([str(script)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 2
    assert "no command given" in result.stderr
    assert "--help" in result.stderr


def test_the_commands_that_exist_are_registered():
    """The other half of the same failure: subcommands that silently fail to register
    leave a CLI whose only symptom is a usage string with nothing in it."""
    script = _console_script("gbfleet")
    result = subprocess.run([str(script), "--help"], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0
    assert "up" in result.stdout


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


def test_the_readme_ships_and_none_of_its_links_are_repo_relative():
    """Two failures that a successful publish hides.

    Without `readme`, the PyPI project page for graphban-fleet is blank — the upload
    succeeds, nothing errors, and the only symptom is somewhere nobody on this side
    looks. And a `](../docs/...)` link resolves on GitHub and 404s from PyPI, so the
    links are absolute even though that reads as needlessly verbose inside the repo.
    """
    assert _toml(FLEET_PYPROJECT)["project"].get("readme") == "README.md"

    readme = (REPO / "fleet" / "README.md").read_text(encoding="utf-8")
    # ANY link that is not absolute, not just `./` and `../`. The narrower pattern missed
    # `](LICENSE)` — which both READMEs carried — and a bare `](docs/…)`, both of which
    # resolve against the PyPI project page and 404 there exactly like `../` does.
    relative = [t for t in re.findall(r"\]\(([^)\s]+)\)", readme)
                if not t.startswith(("http://", "https://", "mailto:", "#"))]
    assert not relative, (
        f"fleet/README.md ships to PyPI, where these resolve to nothing: {relative}"
    )


# --- the ceiling this suite runs under ---------------------------------------------


def test_the_per_test_ceiling_is_still_configured():
    """GRPH-1934: nothing asserted that this distribution still had a per-test ceiling.

    GRPH-1928 added `timeout = 600` here and `timeout = 300` to backend/pyproject.toml.
    Reviewers measuring the backend one found that deleting it left 459 tests green at
    rc=0, and nothing read this one either.

    The silence is the trap: pytest-timeout stays installed (a dev extra here, pinned 2.4.0
    by fleet/uv.lock), so pytest does not even warn `Unknown config option: timeout`. The
    plugin loads, the suite goes fully green, and a hang is bounded only by the job-level
    `timeout-minutes` in .github/workflows/ci.yml — which kills the job and names no test,
    the behaviour GRPH-1928 was filed to remove. An absence reading as a clean result.

    This lives in the fleet suite rather than beside the backend's guard in
    backend/tests/test_ci_shape.py because that is the only place it can fire: the `fleet`
    paths-filter includes `fleet/**`, while the `backend` filter does not list
    fleet/pyproject.toml, so a fleet-only PR that deleted the ceiling would never run the
    backend suite. The constants above are restated rather than imported for the same
    reason — see MIN_PER_TEST_SECONDS.

    Sabotage: delete `timeout = 600` from fleet/pyproject.toml; set it to 0; set it to
    7200; quote it (`timeout = "600"`).
    """
    ini = _toml(FLEET_PYPROJECT).get("tool", {}).get("pytest", {}).get("ini_options")
    assert ini, (
        "fleet/pyproject.toml has no [tool.pytest.ini_options], so the per-test ceiling is "
        "gone with it (GRPH-1934)."
    )

    timeout = ini.get("timeout")
    assert timeout is not None, (
        "fleet/pyproject.toml's [tool.pytest.ini_options] sets no `timeout`, so there is no "
        "per-test ceiling: a hung test is bounded only by the job-level `timeout-minutes`, "
        "which kills the job and names no test (GRPH-1934). pytest-timeout is still "
        "installed, so pytest warns about nothing and the suite stays green without it."
    )
    assert isinstance(timeout, int) and MIN_PER_TEST_SECONDS <= timeout < MAX_PER_TEST_SECONDS, (
        f"fleet/pyproject.toml sets timeout = {timeout!r}, which is not an integer second "
        f"count in {MIN_PER_TEST_SECONDS}..<{MAX_PER_TEST_SECONDS}. 0 is pytest-timeout's own "
        "no-ceiling; at or above the job-level `timeout-minutes` the job dies first and names "
        "no test (see MIN_PER_TEST_SECONDS / MAX_PER_TEST_SECONDS)."
    )
