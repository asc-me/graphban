"""GRPH-866 — the catalog GET /api/fleet draws is the committed matrix, not an empty list."""
from app.services import fleet_matrix


def test_unregistered_harnesses_are_catalogued_but_not_mixable():
    names = {row["harness"] for row in fleet_matrix.payload()["rows"]}
    assert "codex" in names
    assert "codex" not in fleet_matrix.harnesses()
    assert "gbagent" in fleet_matrix.harnesses()


def test_duplicate_toml_rows_are_one_cell():
    keys = [(r["harness"], r["model"], r["tier"]) for r in fleet_matrix.payload()["rows"]]
    assert len(keys) == len(set(keys))
