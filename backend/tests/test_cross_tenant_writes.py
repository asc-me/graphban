"""Every project-scoped WRITE refuses a stranger (GRPH-447).

`test_cross_tenant.py` enumerates the project-scoped READS from the OpenAPI schema and proves
each one blocks another org. Writes were left as a hand-written list of roughly eight, and the
file says so — because enumerating writes needs something reads did not: **a valid request
body per schema.**

Measured against `app.openapi()`: 21 project-scoped writes take `project_id` as a query or
path parameter, and 23 more carry it in the request BODY — `POST /api/items`,
`POST /api/api-keys`, and most of the agent surface. Enumerating parameters alone would have
covered 21 of 44 while reading exactly like a complete sweep, which is the same
absence-reads-as-clean defect one level up. That is why GRPH-438 deliberately shipped the
reads half and said the writes were not covered.

**A leaking write is worse than a leaking read.** The reads sweep guards against another
tenant's data being seen; this one guards against it being CHANGED.

The bodies are derived from the schema — required properties filled with type-appropriate
dummies — rather than hand-written, because a hand list drifts the same way the one this
replaces did.
"""
from __future__ import annotations

import pytest

from tests.test_cross_tenant import _blocked, tenants  # noqa: F401  (fixture re-export)

#: Routes needing more than their schema's required fields to get past validation. A 422 is
#: neither blocked nor leaked, so it can never count as a pass — an unprobeable route is not a
#: safe route, and each entry here exists so a real question can be asked.
PROBE_EXTRA: dict[str, dict] = {
    # `access` is validated in the handler against write/read/none rather than declared as an
    # enum in the schema, so the generated dummy cannot know. One entry, so the route gets
    # asked the question instead of sitting here at 422 looking covered.
    "PUT /api/projects/{project_id}/members/{user_id}": {"access": "read"},
}

#: Deliberately empty. Every project-scoped write blocks a cross-org caller today; an entry
#: here would need a reason here, so an exemption is a reviewable act rather than a hole.
WRITE_EXEMPT: dict[str, str] = {}

#: Streaming responses are consumed differently and a probe would hold the connection. They
#: are covered by their non-streaming siblings, which share the same authorization path.
SKIP_STREAMING = {"/api/agent/chat/stream", "/api/agent/code/stream"}


def _resolve(schema: dict, comps: dict, depth: int = 0) -> dict:
    while "$ref" in schema and depth < 10:
        schema = comps[schema["$ref"].split("/")[-1]]
        depth += 1
    return schema


def _dummy(schema: dict, comps: dict, depth: int = 0):
    """A type-appropriate value. Enough to pass validation, never enough to be meaningful —
    the question being asked is about authorization, and a request that 403s never reaches
    anything that would care what the value was."""
    schema = _resolve(schema, comps, depth)
    if depth > 4:
        return "probe"
    for key in ("anyOf", "oneOf", "allOf"):
        if key in schema:
            options = [o for o in schema[key] if _resolve(o, comps).get("type") != "null"]
            return _dummy(options[0], comps, depth + 1) if options else "probe"
    if schema.get("enum"):
        return schema["enum"][0]
    if "default" in schema:
        return schema["default"]
    return {"string": "probe", "integer": 1, "number": 1.0, "boolean": True,
            "array": [], "object": {}}.get(schema.get("type"), "probe")


def _project_scoped_writes(client) -> dict[str, dict]:
    """Every mutating route that names a project, from the SCHEMA rather than `app.routes`.

    Same reason as the reads sweep: on this FastAPI version the routers mount as
    `_IncludedRouter` objects, so walking `app.routes` finds four and reports a clean sweep of
    nothing.
    """
    spec = client.app.openapi()
    comps = spec.get("components", {}).get("schemas", {})
    found: dict[str, dict] = {}
    for path, ops in spec["paths"].items():
        for verb in ("post", "put", "patch", "delete"):
            op = ops.get(verb)
            if not op:
                continue
            params = {p["name"] for p in op.get("parameters", [])}
            raw = (op.get("requestBody", {}).get("content", {})
                     .get("application/json", {}).get("schema"))
            schema = _resolve(raw, comps) if raw else {}
            props = schema.get("properties", {})
            in_param = "project_id" in params or "{project_id}" in path
            if not in_param and "project_id" not in props:
                continue
            body = None
            if raw is not None:
                required = set(schema.get("required", []))
                body = {n: _dummy(p, comps) for n, p in props.items() if n in required}
            found[f"{verb.upper()} {path}"] = {
                "verb": verb, "path": path, "in_param": in_param, "body": body,
            }
    return found


# ── the control ───────────────────────────────────────────────────────────────

def test_the_enumeration_finds_the_writes_it_claims_to(client):
    """Every assertion below loops over this set, so an enumerator that silently returned
    nothing would turn the whole file into a pass. Pinned by count and by naming one route of
    each kind — a parameter one and a body one."""
    found = _project_scoped_writes(client)
    assert len(found) >= 40, f"only {len(found)} project-scoped writes found — enumeration broke"
    assert "POST /api/items" in found, "the body-carried case is missing"
    assert found["POST /api/items"]["body"] is not None
    assert any(v["in_param"] for v in found.values()), "the parameter case is missing"


def test_both_kinds_are_actually_present(client):
    """The ticket's whole point: a parameters-only sweep covers half the surface and reads as
    complete. If the body half ever drops to zero this file has quietly become that."""
    found = _project_scoped_writes(client)
    by_body = [k for k, v in found.items() if not v["in_param"]]
    assert len(by_body) >= 15, f"only {len(by_body)} writes carry project_id in the body"


# ── the sweep ─────────────────────────────────────────────────────────────────

def test_every_project_scoped_write_blocks_another_org(client, tenants):
    """The point of the file. A leak here is not an information leak — it is a write into
    another tenant's project."""
    alex, pb = tenants["alex"], tenants["p_b"]
    leaked, unprobeable = [], []

    for name, spec in sorted(_project_scoped_writes(client).items()):
        if name in WRITE_EXEMPT or spec["path"] in SKIP_STREAMING:
            continue
        url = spec["path"].replace("{project_id}", pb)
        if "{" in url:  # another resource id we do not have — give it something shaped right
            import re
            url = re.sub(r"\{[^}]+\}", "probe", url)
        body = dict(spec["body"] or {})
        if spec["in_param"] and "{project_id}" not in spec["path"]:
            url += ("&" if "?" in url else "?") + f"project_id={pb}"
        if spec["body"] is not None and not spec["in_param"]:
            body["project_id"] = pb
        body.update(PROBE_EXTRA.get(name, {}))

        payload = body if spec["body"] is not None else None
        r = client.request(spec["verb"].upper(), url, json=payload, headers=alex)
        if r.status_code == 401:
            # WRONG CREDENTIAL, NOT A REFUSAL. Part of this surface takes an API key rather
            # than a JWT, and a 401 there says "you brought the wrong kind of key" — which
            # tells us nothing about whether the route would let org A into org B. Ask again
            # with org A's key, so the question actually gets put. Still 401 afterwards means
            # unprobed, and unprobed is not safe.
            r = client.request(spec["verb"].upper(), url, json=payload,
                               headers={"X-API-Key": tenants["alex_key"]})
        if r.status_code == 401:
            unprobeable.append(f"{name} -> 401 with both a JWT and an API key")
        elif r.status_code == 422:
            unprobeable.append(f"{name} -> 422 {r.text[:140]}")
        elif not _blocked(r.status_code):
            leaked.append(f"{name} -> {r.status_code} {r.text[:140]}")

    assert not leaked, f"cross-org WRITES that did not block: {leaked}"
    assert not unprobeable, (
        f"these could not be probed, so they are untested: {unprobeable}. Add the missing "
        "fields to PROBE_EXTRA — a 422 is neither blocked nor leaked, and a route that sits "
        "here looking covered while never being called is the defect this file exists to "
        "close.")


def test_an_unprobeable_route_is_reported_rather_than_skipped(client, tenants, monkeypatch):
    """The `unprobeable` branch, driven rather than trusted.

    In the green state nothing 422s, so that branch never runs and a sabotage pass cannot
    tell whether it works — neutering it changes nothing precisely because there is nothing
    to collect. So remove the one PROBE_EXTRA entry and confirm the sweep FAILS: a route that
    cannot be asked the question must be reported, never quietly counted as safe.

    This is the whole thesis of the file one level down. A sweep that skips what it cannot
    probe reads exactly like a sweep that found nothing wrong.
    """
    monkeypatch.setitem(PROBE_EXTRA, "PUT /api/projects/{project_id}/members/{user_id}", {})
    with pytest.raises(AssertionError) as e:
        test_every_project_scoped_write_blocks_another_org(client, tenants)
    assert "could not be probed" in str(e.value)
    assert "members" in str(e.value)


def test_a_route_that_refuses_both_credentials_is_reported_too(client, tenants):
    """The other branch nothing exercises in the green state.

    Part of this surface takes an API key rather than a JWT, so the sweep retries a 401 with
    org A's key. If that also 401s the route was never actually asked the question — and
    "refused my credential" is not "refused a stranger". Driven by handing the sweep a key
    that cannot authenticate anywhere, so both attempts fail and the branch has to fire.
    """
    broken = dict(tenants)
    broken["alex_key"] = "gb_sk_not_a_real_key"
    with pytest.raises(AssertionError) as e:
        test_every_project_scoped_write_blocks_another_org(client, broken)
    assert "401 with both a JWT and an API key" in str(e.value)
