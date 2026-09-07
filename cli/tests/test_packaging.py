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


def test_the_cli_shares_no_dependency_with_the_backend_but_httpx():
    """THE CONTROL: derived from the backend's list, so a dependency added there and copied
    here fails without anyone maintaining a denylist."""
    assert BACKEND.is_file(), f"no backend pyproject at {BACKEND} — this guard checked nothing"
    backend, cli = _names(_requires(BACKEND)), _names(_requires(CLI / "pyproject.toml"))
    assert backend, "the backend declares no dependencies — this guard checked nothing"

    shared = (backend & cli) - {"httpx"}
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
