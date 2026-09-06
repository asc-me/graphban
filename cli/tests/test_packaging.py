"""PRD-40 D1/D2 criterion 1 — `gb` installs on a laptop, so it may not pull a database.

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
    assert not offenders, "gb must not be able to reach a database:\n" + "\n".join(offenders)


def test_the_entry_point_is_gb_and_does_not_collide():
    """`graphban`/`agentledger` belong to backend, `gbfleet`/`gbagent` to fleet."""
    scripts = tomllib.loads((CLI / "pyproject.toml").read_text())["project"]["scripts"]
    assert set(scripts) == {"gb"}
    taken = set(tomllib.loads(BACKEND.read_text())["project"]["scripts"])
    taken |= set(tomllib.loads((REPO / "fleet" / "pyproject.toml").read_text()
                               )["project"]["scripts"])
    assert not (set(scripts) & taken), "entry point collides with another package's"
