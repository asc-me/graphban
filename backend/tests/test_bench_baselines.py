"""A posted baseline reaches the Harness grid (GRPH-1876)."""
from datetime import datetime, timedelta, timezone

from app.db import SessionLocal
from app.models import AttemptTelemetry

IDENT = {"model": "sonnet", "binary_version": "1.2.3",
         "suite_id": "template", "suite_version": "0.1.0"}
OUT = [{"task_id": "t1", "outcome": "check_green", "capabilities": ["A1"]}]


def _env(client, auth, *, natural=True):
    proj = client.post("/api/projects", json={"name": "BaseSrc"}, headers=auth).json()["id"]
    key = client.post("/api/api-keys", json={"name": "g", "scopes": ["read", "write"]},
                      headers=auth).json()["plaintext"]
    if natural:
        with SessionLocal() as db:
            db.add(AttemptTelemetry(
                id="at-b-1", project_id=proj, vendor="anthropic", model="sonnet",
                binary_version="1.2.3", capabilities=["A1"], outcome="signed_off",
                tokens_in=10, tokens_out=10,
                derived_at=datetime.now(timezone.utc) - timedelta(days=1)))
            db.commit()
    return proj, {"X-API-Key": key}


def _post(client, proj, h, **over):
    body = {"vendor": "claude", **IDENT, "outcomes": OUT, **over}
    r = client.post(f"/api/bench/results?project_id={proj}", headers=h, json=body)
    assert r.status_code < 300, r.text
    return r.json()


def _rows(client, proj, h, cap="A1"):
    cells = client.get(f"/api/harness/grid?project_id={proj}", headers=h).json()["cells"]
    return [x for c in cells for x in (c.get("leaves") or [c])
            if x["key"]["capability"] == cap and x.get("kind") != "family"]


def test_a_claude_baseline_attaches_to_the_anthropic_cell(client, auth):
    proj, h = _env(client, auth)
    assert _post(client, proj, h)["vendor"] == "anthropic"
    rows = _rows(client, proj, h)
    assert len(rows) == 1 and rows[0].get("layer") != "baseline"
    assert rows[0]["baseline"]["n"] == 1


def test_a_model_with_no_natural_attempts_gets_its_own_row(client, auth):
    proj, h = _env(client, auth, natural=False)
    _post(client, proj, h)
    rows = _rows(client, proj, h)
    assert len(rows) == 1
    row = rows[0]
    assert row["layer"] == "baseline" and row["finished"] == 0
    assert row["key"]["vendor"] == "anthropic"
    b = row["baseline"]
    assert (b["suite_id"], b["suite_version"], b["n"], b["rate"]) == ("template", "0.1.0", 1, 1.0)


def test_a_baseline_at_another_binary_version_does_not_attach(client, auth):
    proj, h = _env(client, auth)
    _post(client, proj, h, binary_version="9.9.9")
    rows = _rows(client, proj, h)
    natural = [r for r in rows if r.get("layer") != "baseline"]
    assert natural[0]["baseline"] is None
    assert any(r.get("layer") == "baseline" and r["key"]["binary_version"] == "9.9.9"
               for r in rows)
