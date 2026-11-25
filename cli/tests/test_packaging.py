"""PRD-40 D1/D2 criterion 1 — `gban` installs on a laptop, so it may not pull a database.

Modelled on `fleet/tests/test_packaging.py`, and derived from the BACKEND's own dependency
list for the same reason: a hand-written forbidden set is a list somebody has to remember to
update, and the thing it is guarding against is exactly the dependency nobody thought about.
"""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

CLI = Path(__file__).resolve().parents[1]
REPO = CLI.parent
BACKEND = REPO / "backend" / "pyproject.toml"


def _requires(path: Path) -> list[str]:
    return tomllib.loads(path.read_text(encoding="utf-8"))["project"]["dependencies"]


def _names(requirements: list[str]) -> set[str]:
    return {re.split(r"[<>=!\[ ]", r, 1)[0].strip().lower() for r in requirements if r.strip()}


def test_the_cli_shares_no_dependency_with_the_backend():
    """THE CONTROL: derived from the backend's list, so a dependency added there and copied
    here fails without anyone maintaining a denylist.

    The carve-out is gone with the dependency it named (GRPH-782). `httpx` was declared and
    never imported — `client.py` is `urllib.request` throughout — so every laptop paid for
    httpx, httpcore, h11, anyio, sniffio, certifi and idna to import none of them. The one
    name hand-excluded from a guard whose own argument is "a hand-written forbidden set is a
    list somebody has to remember to update" was a dependency the package did not use.
    Dropping it makes this check strictly stronger."""
    assert BACKEND.is_file(), f"no backend pyproject at {BACKEND} — this guard checked nothing"
    backend, cli = _names(_requires(BACKEND)), _names(_requires(CLI / "pyproject.toml"))
    assert backend, "the backend declares no dependencies — this guard checked nothing"

    shared = backend & cli
    assert not shared, (
        "graphban-cli installs on a laptop and must not pull the server's stack: " + repr(shared))


def test_no_module_imports_a_database_driver_or_a_web_framework():
    """D1: 'never opens a database connection' is enforced by having nothing that could.

    A source sweep rather than an import-time check, because the point is that the capability
    is absent — an import guarded by a flag is still an import somebody can reach.
    """
    sources = sorted((CLI / "src").rglob("*.py"))
    assert sources, f"no python sources under {CLI / 'src'} — this guard scanned nothing"

    forbidden = ("sqlalchemy", "psycopg", "asyncpg", "alembic", "pgvector", "redis",
                 "fastapi", "starlette", "sqlite3")
    offenders = []
    for path in sources:
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.lstrip().startswith(("import ", "from ")):
                continue
            for name in forbidden:
                if re.search(rf"\b{name}\b", line):
                    offenders.append(f"{path.relative_to(CLI)}:{lineno}: {line.strip()}")
    assert not offenders, "gban must not be able to reach a database:\n" + "\n".join(offenders)


#: What oh-my-zsh's git plugin claims in this shape — the reason `gb` had to be given up.
#: An ALIAS BEATS A BINARY on PATH, and nothing inside the process can detect that: by the
#: time the command would have run, the alias did. The deployed walk hit it on the very
#: first command and got `git branch`'s usage text.
GIT_ALIASES = {
    "gb", "gba", "gbd", "gbg", "gbgd", "gbl", "gbm", "gbnm", "gbr", "gbs", "gbsb", "gbsg",
    "gbsn", "gbso", "gbsr", "gbss",
    "grb", "grba", "grbc", "grbd", "grbi", "grbm", "grbo", "grbom", "grbs", "grbum",
    "g", "ga", "gc", "gd", "gf", "gl", "gm", "gp", "gr", "gst", "gco", "gcm", "gpl", "gps",
}


def test_the_entry_point_is_gban_and_does_not_collide():
    """`graphban`/`agentledger` belong to backend, `gbfleet`/`gbagent` to fleet — and the
    whole `gb*` namespace belongs to git.

    Sabotage: name the script `gb` again and this fails. That is the point: the collision is
    invisible from inside the program, so the only place it can be caught is here."""
    scripts = tomllib.loads((CLI / "pyproject.toml").read_text())["project"]["scripts"]
    assert set(scripts) == {"gban"}
    assert not (set(scripts) & GIT_ALIASES), (
        "the entry point is a common git alias; an alias beats a binary on PATH")
    taken = set(tomllib.loads(BACKEND.read_text())["project"]["scripts"])
    taken |= set(tomllib.loads((REPO / "fleet" / "pyproject.toml").read_text()
                               )["project"]["scripts"])
    assert not (set(scripts) & taken), "entry point collides with another package's"


def test_every_instruction_the_tool_prints_names_the_command_that_exists():
    """A rename that leaves "run gb login" in a message tells a person to run something that
    is not installed. Sabotage: put one back and this fails."""
    offenders = []
    for path in sorted((CLI / "src").rglob("*.py")):
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            for stale in ("`gb ", "`gb`", '"gb"', "gb login", "gb doctor", "gb agents"):
                if stale in line:
                    offenders.append(f"{path.relative_to(CLI)}:{lineno}: {line.strip()}")
    assert not offenders, "these name a command that is not installed:\n" + "\n".join(offenders)


def test_every_declared_dependency_is_actually_imported():
    """A declared dependency nobody imports is a cost with no buyer, and — because thin-
    install guards carry exemptions for exactly these names — it weakens the guard that is
    supposed to keep this package small (GRPH-782).

    Sabotage: put `httpx` back in pyproject.toml and this fails."""
    declared = _names(_requires(CLI / "pyproject.toml"))
    if not declared:
        return  # stdlib only, which is the state this package should stay in
    sources = "\n".join(p.read_text() for p in (CLI / "src").rglob("*.py"))
    unused = sorted(d for d in declared
                    if not re.search(rf"^\s*(?:import|from)\s+{re.escape(d)}\b",
                                     sources, re.M))
    assert not unused, f"declared but never imported: {unused}"


def test_the_prd_names_the_files_the_code_actually_uses():
    """The rename swept `cli/src` and stopped there, so PRD-40 D10 went on naming
    `~/.graphban/gb.json` — a path nothing reads — twice (GRPH-782).

    Asserted on the CONSTANTS rather than by sweeping for the retired name, because the PRD's
    v1.1 amendment legitimately discusses `gb` at length: a blanket sweep would either fail on
    the explanation of the rename or be watered down until it caught nothing.

    Sabotage: put `gb.json` back in the PRD and this fails."""
    from gban import config

    prd = (REPO / "docs" / "prd-40-gb-cli.md")
    assert prd.is_file(), f"no PRD at {prd} — this guard checked nothing"
    text = prd.read_text()
    for name in (config.SETTINGS_FILE, config.SESSION_FILE, config.NOT_OURS):
        assert name in text, f"the PRD never names {name}, which D10 is entirely about"
    stale = "/gb.json"
    assert stale not in text, f"the PRD names {stale}, which nothing reads"


def test_the_package_carries_a_licence_and_a_way_back_to_the_source():
    """PyPI metadata is permanent per version: 0.1.0 published without a licence is
    unlicensed on PyPI forever, because a version number can never be reused.

    Caught by reading the built wheel's METADATA before the first upload, which is the only
    moment it is still free to fix. Sabotage: drop either field and this fails."""
    spec = tomllib.loads((CLI / "pyproject.toml").read_text())["project"]
    assert spec.get("license") == "Apache-2.0", "no licence reaches PyPI as 'unlicensed'"
    assert (CLI / "LICENSE").is_file(), "license-files names a file that must exist"
    urls = spec.get("urls") or {}
    assert urls.get("Repository"), "without a URL the PyPI page is a name and a summary"


def test_both_distributions_agree_on_their_licence():
    """`gban` and `gbfleet` ship together and diverge from the repository's FSL for the same
    stated reason. Two answers here would be one of them being wrong."""
    fleet = tomllib.loads((REPO / "fleet" / "pyproject.toml").read_text())["project"]
    cli = tomllib.loads((CLI / "pyproject.toml").read_text())["project"]
    assert cli["license"] == fleet["license"] == "Apache-2.0"

def test_the_readme_ships_and_none_of_its_links_are_repo_relative():
    """The guard `fleet/tests/test_packaging.py` has had all along, and `cli/` did not —
    while carrying a `](../fleet/README.md)` that would have 404'd from the PyPI page.

    Two failures a successful publish hides. Without `readme`, the project page is blank: the
    upload succeeds, nothing errors, and the only symptom is somewhere nobody on this side
    looks. And a repo-relative link resolves on GitHub and resolves to nothing from PyPI, so
    they are absolute even though that reads as needlessly verbose inside the repository.

    Sabotage: put a `](../fleet/README.md)` back and this fails."""
    import re

    spec = tomllib.loads((CLI / "pyproject.toml").read_text())["project"]
    assert spec.get("readme") == "README.md", "no readme means a blank PyPI page"

    readme = (CLI / "README.md").read_text(encoding="utf-8")
    # ANY link that is not absolute, not just `./` and `../`. The narrower pattern missed
    # `](LICENSE)` — which both READMEs carried — and a bare `](docs/…)`, both of which
    # resolve against the PyPI project page and 404 there exactly like `../` does.
    relative = [t for t in re.findall(r"\]\(([^)\s]+)\)", readme)
                if not t.startswith(("http://", "https://", "mailto:", "#"))]
    assert not relative, (
        f"cli/README.md ships to PyPI, where these resolve to nothing: {relative}")


def test_the_install_the_tool_prints_is_the_one_the_readme_gives():
    """It has been wrong twice: first naming a PyPI package that did not exist, then a git
    spec that worked and stopped being the right advice the moment `graphban-fleet` was
    published. A remedy a tool prints is a promise, and the README is where the promise is
    maintained — so they are checked against each other rather than both against memory.

    Sabotage: change either and this fails."""
    from gban.doctor import INSTALL_SUPERVISOR

    readmes = ((CLI / "README.md").read_text(encoding="utf-8")
               + (REPO / "fleet" / "README.md").read_text(encoding="utf-8"))
    assert INSTALL_SUPERVISOR in readmes, (
        f"gban prints {INSTALL_SUPERVISOR!r}, which neither README tells anyone to run")


def test_the_printed_install_names_a_package_that_is_actually_published():
    """The distribution name in the command must be one this repository builds. Checked
    against the pyprojects rather than against PyPI, because a test that reaches the network
    fails for reasons that have nothing to do with the code."""
    from gban.doctor import INSTALL_SUPERVISOR

    built = {tomllib.loads((REPO / d / "pyproject.toml").read_text())["project"]["name"]
             for d in ("cli", "fleet")}
    named = [w for w in INSTALL_SUPERVISOR.split() if w.startswith("graphban")]
    assert named, f"{INSTALL_SUPERVISOR!r} names no package at all"
    assert set(named) <= built, f"{named} is not built here; built are {sorted(built)}"


def test_the_cli_suite_has_a_per_test_ceiling_the_job_bound_cannot_replace():
    """GRPH-1935: this distribution had no per-test ceiling, so a hung cli test was bounded
    only by the `cli-tests` job's `timeout-minutes: 10` — a cancelled job that names no test
    and prints no stack. That is the failure shape GRPH-1928 removed for the backend and
    fleet, and its absence here read as a clean result: 480 passing tests, no bound.

    A WINDOW rather than the literal, so a legitimate retune does not have to edit this test.
    Both ends are measured, and `cli/pyproject.toml` carries the derivation:

    * `> 300` — 300s is the subprocess patience this suite really honours. `codemap._git`
      runs real `git` under `timeout=300`, and `test_setup._built_wheel_names` really builds
      a wheel under the same 300s, where a slow or absent builder degrades to `pytest.skip`.
      A ceiling inside that patience turns a slow builder into a red job — a test defect,
      not a hang.
    * `<= 480` — the `cli-tests` job bound is 600s, so a ceiling at or near it lets the job
      kill fire first and name nothing. 480s still leaves ~120s for checkout, the install and
      the other 479 tests: over 4x the 26.95s the whole suite measured in that job.

    Sabotage: delete `timeout` from `[tool.pytest.ini_options]` and this fails naming the
    missing ceiling; raise it to the job's own 600s and it fails naming the bound it can no
    longer beat. Both arms were run for this item.
    """
    spec = tomllib.loads((CLI / "pyproject.toml").read_text(encoding="utf-8"))
    ini = spec.get("tool", {}).get("pytest", {}).get("ini_options", {})
    ceiling = ini.get("timeout")
    assert isinstance(ceiling, int), (
        "cli/pyproject.toml's [tool.pytest.ini_options] sets no integer `timeout`, so a hung "
        "cli test has NO per-test ceiling: it is bounded only by the `cli-tests` job's "
        "`timeout-minutes: 10`, which cancels the job, names no test and prints no stack "
        f"(GRPH-1935). Got {ceiling!r}.")
    assert 300 < ceiling <= 480, (
        f"`timeout = {ceiling}` is outside the window this suite was measured into. It must "
        "exceed the 300s subprocess patience the tests really honour (`codemap._git`, and "
        "the wheel build in `test_setup._built_wheel_names`), or a slow builder becomes a "
        "red job instead of a skip; and it must stay well under the `cli-tests` job's own "
        "600s bound, or the job kill fires first and names no test — the absence this "
        "ceiling exists to remove.")


def test_the_ceiling_is_enforced_by_a_plugin_that_is_declared_pinned_and_loaded(pytestconfig):
    """An ini key that no installed plugin claims is IGNORED by pytest: one warning line in a
    run that is green anyway. So `timeout = 420` in a venv without pytest-timeout is a
    comment wearing a config key's clothes, and the suite reads as bounded while nothing
    bounds it. Three links, each of which breaks on its own:

    * declared in cli's dev extra at the SAME floor the other two distributions use —
      derived from their pyprojects rather than retyped here, so a fourth answer cannot
      appear silently (this file's own rule for the dependency denylist above);
    * pinned in `cli/uv.lock`, which is what `uv sync` installs and what pip-audit scans
      (`scripts/scan_ci.py` maps `cli/` to pip-audit and bandit);
    * loaded in the very session the ceiling is supposed to bound, and reading the SAME
      number back out of `cli/pyproject.toml` — the two links that cannot pass while the
      ceiling is inert.

    Sabotage: drop `pytest-timeout` from the dev extra and the first assert fails; block the
    plugin (`-p no:timeout`) and the loaded-plugin assert fails. Both arms were run for this
    item.
    """
    def _timeout_reqs(dev: list[str]) -> list[str]:
        return [r for r in dev if _names([r]) == {"pytest-timeout"}]

    spec = tomllib.loads((CLI / "pyproject.toml").read_text(encoding="utf-8"))
    cli_dev = spec["project"]["optional-dependencies"]["dev"]
    declared = _timeout_reqs(cli_dev)
    assert declared, (
        f"cli's dev extra declares no pytest-timeout, so the `timeout` ini key binds nothing "
        f"and pytest ignores it: {cli_dev}")

    others = {}
    for dist in ("backend", "fleet"):
        dev = tomllib.loads((REPO / dist / "pyproject.toml").read_text(encoding="utf-8")
                            )["project"]["optional-dependencies"]["dev"]
        others[dist] = _timeout_reqs(dev)
    assert all(len(reqs) == 1 for reqs in others.values()), (
        f"the distributions this one is meant to match do not each declare exactly one "
        f"pytest-timeout, so this guard compared nothing: {others}")
    assert declared[0] == others["backend"][0] == others["fleet"][0], (
        f"cli declares {declared[0]!r} where backend declares {others['backend'][0]!r} and "
        f"fleet declares {others['fleet'][0]!r} — three answers to one question, and the "
        "lowest of them is the plugin version that actually enforces a ceiling")

    lock = (CLI / "uv.lock").read_text(encoding="utf-8")
    assert re.search(r'^name = "pytest-timeout"$', lock, re.M), (
        "cli/uv.lock pins no pytest-timeout, so `uv sync` builds a venv in which the "
        "`timeout` ini key is inert and pip-audit never sees the plugin it depends on")

    assert pytestconfig.pluginmanager.has_plugin("timeout"), (
        "pytest-timeout is NOT LOADED in this session, so the per-test ceiling is inert: "
        "every test reads as bounded and nothing bounds it")

    # The file is the callee; this is the call. pytest-timeout registers `timeout` as a
    # STRING ini option, so the comparison is on the rendered text — which is also what makes
    # it honest: it compares what pytest will enforce against what the guard above pinned.
    declared_ceiling = spec.get("tool", {}).get("pytest", {}).get("ini_options", {}).get("timeout")
    enforced = pytestconfig.getini("timeout")
    assert str(enforced) == str(declared_ceiling), (
        f"pytest reads `timeout = {enforced!r}` from {pytestconfig.inipath}, but "
        f"cli/pyproject.toml declares {declared_ceiling!r}: the ceiling this suite's guard "
        "pins is not the one pytest-timeout enforces")
