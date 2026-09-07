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
