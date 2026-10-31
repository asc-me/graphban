"""D8 — the fleet roles in the roster generator (GRPH-339 / PRD-17 §7).

The in-session roster (AL-213) primes subagents inside one session: one parent, one vendor,
and roles that are ADVISORY — a prompt a model can simply ignore. These three are different in
kind. Each is a whole process that registers with the server and is refused at the call gate if
it strays, so its role is a property of its credential rather than of its instructions.

**The guard that matters here is that the prompts do not lie about the API.** A role prompt is
the one artefact nothing else validates: it is prose, it ships to agents, and when it drifts
from the tools the failure lands at 3am inside somebody's terminal as `unknown tool` — with the
generated file still looking authoritative. So every tool a fleet prompt tells an agent to call
is checked against the live manifest, and every argument against that tool's schema.
"""
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from gen_subagents import FLEET_ROSTER, ROSTER, TOOLCHAINS, render_files  # noqa: E402

from app.mcp_server import TOOLS, _SCHEMA_BY_NAME  # noqa: E402

FLEET_NAMES = {r["name"] for r in FLEET_ROSTER}
TOOL_NAMES = {t["name"] for t in TOOLS}

# `identifier(` inside backticks — an unambiguous call site. Bare mentions are prose and are
# deliberately not matched: `review` is a status, not a tool.
_CALL = re.compile(r"`([a-z_][a-z0-9_]*)\(")
_ARG = re.compile(r"\b([a-z_][a-z0-9_]*)=")


def _fleet_bodies():
    return {r["name"]: r["body"] for r in FLEET_ROSTER}


# ---- the prompts must not lie -----------------------------------------------------------------

@pytest.mark.parametrize("name,body", sorted(_fleet_bodies().items()))
def test_every_tool_a_prompt_names_actually_exists(name, body):
    """The failure this prevents: a prompt confidently instructing an agent to call something
    that was renamed three PRDs ago, discovered only when a terminal returns `unknown tool`."""
    called = set(_CALL.findall(body))
    unknown = called - TOOL_NAMES
    assert not unknown, f"{name} tells an agent to call {sorted(unknown)}, which do not exist"


@pytest.mark.parametrize("name,body", sorted(_fleet_bodies().items()))
def test_every_argument_a_prompt_names_exists_on_that_tool(name, body):
    """Catches the subtler drift: the tool survives a rename, its parameter does not. An agent
    passing an argument the schema has never heard of is silently ignored — the call succeeds
    and does the wrong thing, which is worse than an error."""
    for call in re.finditer(r"`([a-z_][a-z0-9_]*)\(([^`]*)\)", body):
        tool, argtext = call.group(1), call.group(2)
        if tool not in _SCHEMA_BY_NAME:
            continue
        allowed = set(_SCHEMA_BY_NAME[tool].get("properties", {}))
        used = set(_ARG.findall(argtext))
        unknown = used - allowed
        assert not unknown, f"{name}: {tool} has no {sorted(unknown)} — schema has {sorted(allowed)}"


def test_the_prompts_reference_the_long_poll_that_exists(name=None):
    """`wait_seconds` is the difference between one tool call a minute and twelve. If D7 were
    ever reverted, these prompts would quietly instruct agents into the expensive loop."""
    bodies = _fleet_bodies()
    for role in ("gb-worker", "gb-reviewer"):
        assert "wait_seconds" in bodies[role], f"{role} should park rather than spin"
    assert "wait_seconds" in _SCHEMA_BY_NAME["claim_cluster"]["properties"]
    assert "wait_seconds" in _SCHEMA_BY_NAME["claim_review"]["properties"]


# ---- the loop each role must follow -----------------------------------------------------------

@pytest.mark.parametrize("name", sorted(FLEET_NAMES))
def test_every_fleet_role_registers_before_anything_else(name):
    """An agent that claims without registering is invisible to the roster and ungoverned by
    the role gate — and two terminals on one key are two agents only if both register. It is
    the one instruction whose omission breaks everything downstream silently."""
    body = _fleet_bodies()[name]
    assert "register_agent" in body
    head = body[: body.index("## Loop")] if "## Loop" in body else body
    assert "register_agent" in head, "registration belongs before the loop, not inside it"


@pytest.mark.parametrize("name", ["gb-worker", "gb-reviewer"])
def test_the_claiming_roles_are_told_to_stop_rather_than_spin(name):
    """`claimed: false` is a real answer, not an error. An agent that retries it forever is an
    idle agent burning tokens, which is the failure `wait_seconds` and this instruction exist
    together to prevent."""
    body = _fleet_bodies()[name]
    assert "STOP" in body and "do not spin" in body.lower()


def test_the_orchestrator_is_told_it_cannot_build():
    """The orchestrator plans; it does not quietly do the work. A planner that builds is
    another worker, and the fleet loses the role meant to coordinate it — the server refuses
    it, and the prompt should say so rather than let the agent discover it as a failure."""
    body = _fleet_bodies()["gb-orchestrator"]
    assert "claim_next" in body and "refused" in body.lower()


def test_the_reviewer_is_told_the_ban_is_on_authorship():
    """Not on role. An agent that thinks a promotion would let it sign its own work will try
    it, be refused, and burn a cycle learning what the prompt could have said."""
    body = _fleet_bodies()["gb-reviewer"]
    assert "authorship" in body.lower()


@pytest.mark.parametrize("name", sorted(FLEET_NAMES))
def test_every_fleet_role_knows_what_a_directive_is(name):
    """The downlink only works if the agent adopts what arrives. A role change is not an error
    and does not arrive as one, so an agent that has never been told about `directive` will
    ignore the field and keep working the role it no longer holds."""
    assert "directive" in _fleet_bodies()[name].lower()


# ---- emission ------------------------------------------------------------------------------------

def test_the_fleet_roles_are_emitted_for_every_toolchain():
    """Same generator, same files, same `--check` staleness gate — PRD-17 §9 is explicit that
    the client half is extended rather than replaced."""
    files = render_files()
    for tool in TOOLCHAINS:
        for role in FLEET_NAMES:
            assert any(f".{tool}/agents/{role}" in path for path in files), \
                f"{role} missing for {tool}"


def test_the_in_session_roster_is_untouched():
    """These compose rather than compete: an in-session orchestrator becomes one WORKER in the
    fleet. Replacing the existing roles would break the delegation the repo already uses."""
    files = render_files()
    for role in (r["name"] for r in ROSTER):
        assert any(f".claude/agents/{role}" in path for path in files)


def test_no_generated_file_carries_a_credential():
    """These are committed. A credential in one is a credential in the repository — and the
    Fleet view issues per-wave keys precisely so that never has to happen."""
    for path, content in render_files().items():
        assert "gb_sk_" not in content and "al_sk_" not in content, f"{path} carries a key"


# ---- PRD-19 E6: the prompts carry the seat ----------------------------------------------------

@pytest.mark.parametrize("name", sorted(FLEET_NAMES))
def test_every_fleet_role_is_told_to_redeem_its_seat(name):
    """The seat is the only grant the server can VERIFY — a role_hint is the agent asking. A
    prompt that omitted it would produce an un-enrolled agent that looks fine on the roster,
    holds `all-in-one`, and is refused review against everything its own fleet built."""
    assert "enrolment_code" in _fleet_bodies()[name]


@pytest.mark.parametrize("name", sorted(FLEET_NAMES))
def test_the_prompts_keep_the_unenrolled_fallback(name):
    """Enrolment is the recommended path, not the only one. An agent on a credential whose
    operator never issued seats still has to be told how to be distinguishable, or it silently
    cannot review — which is the failure `instance` was added for."""
    assert "instance" in _fleet_bodies()[name]


# ---- GRPH-1229: the REST roster folds dead, empty agents into an exact count ---------------------

from datetime import datetime, timedelta, timezone  # noqa: E402

#: Pinned response ceiling for 5 full rows plus the page chrome. The unfolded roster of 2,005
#: agents is several hundred KB; this fails if every agent comes back in full.
ROSTER_SIZE_CEILING = 60_000


@pytest.fixture()
def _db(_clean_database):
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _seed(db, project_id, n_gone=2000):
    from app.models import Agent

    now = datetime.now(timezone.utc)
    old = now - timedelta(days=3)
    nxt = [1]

    def mk(**kw):
        a = Agent(id=f"{project_id}-a{nxt[0]}", project_id=project_id, number=nxt[0],
                  active_role="worker", state="idle", **kw)
        nxt[0] += 1
        db.add(a)
        return a

    for _ in range(n_gone):
        mk(last_seen_at=old)
    for _ in range(3):
        mk(last_seen_at=now)
    orphan = mk(last_seen_at=old, branch="gb/orphan")
    # The review holder is OFFLINE and quiet for days: an online holder is kept for being
    # online, so only an offline one proves that holding something alone keeps a row in full.
    # The lease holder is online because a fleet read requeues an offline agent's build lease
    # (`requeue_offline_items`) before the roster is built; the offline-lease case is pinned
    # on `collapse_roster` directly below.
    lessee, reviewer = mk(last_seen_at=now), mk(last_seen_at=old)
    db.flush()
    from app.services import items as items_svc

    leased = items_svc.create_item(db, title="held 0", project_id=project_id,
                                   status="in_progress")
    leased.claimed_by = lessee.id
    leased.claimed_at = now
    in_review = items_svc.create_item(db, title="held 1", project_id=project_id,
                                      status="review")
    in_review.review_claimed_by = reviewer.id
    in_review.review_claimed_at = old
    db.commit()
    return orphan.id, (lessee.id, reviewer.id)


def _proj(client, auth):
    return client.post("/api/projects", json={"name": "Roster"}, headers=auth).json()["id"]


def test_roster_returns_the_live_in_full_and_the_dead_as_an_exact_count(client, auth, _db):
    import json

    pid = _proj(client, auth)
    orphan_id, holder_ids = _seed(_db, pid)
    r = client.get(f"/api/fleet?project_id={pid}", headers=auth)
    body = r.json()
    ids = {a["id"] for a in body["agents"]}
    # 3 online + a lease holder + an offline review holder + 1 orphaned branch.
    assert len(body["agents"]) == 6
    assert orphan_id in ids, "a holder of an orphaned branch is never collapsed away"
    for hid in holder_ids:
        assert hid in ids, "an agent holding a lease or review claim is never collapsed"
    reviewer = next(a for a in body["agents"] if a["id"] == holder_ids[1])
    assert reviewer["state"] == "offline" and reviewer["holdings"][0]["hold"] == "review"
    assert body["collapsed"]["count"] == 2000
    assert body["total"] == 2006 == len(body["agents"]) + body["collapsed"]["count"]
    assert len(json.dumps(body)) < ROSTER_SIZE_CEILING


def test_the_collapsed_agents_are_listed_on_demand_in_pages(client, auth, _db):
    pid = _proj(client, auth)
    orphan_id, holder_ids = _seed(_db, pid, n_gone=30)
    page = client.get(f"/api/fleet/agents/collapsed?project_id={pid}&offset=10&limit=15",
                      headers=auth).json()
    assert page["total"] == 30 and len(page["agents"]) == 15
    listed = {a["id"] for a in page["agents"]}
    assert orphan_id not in listed and not listed & set(holder_ids)


@pytest.mark.parametrize("hold", ["build", "review"])
def test_an_offline_quiet_holder_is_never_collapsed(hold):
    from app.services import fleet

    old = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()

    def row(id_, holdings):
        return {"id": id_, "active_role": "worker", "state": "offline", "holdings": holdings,
                "branch_orphaned": False, "last_seen_at": old}

    out = fleet.collapse_roster({"agents": [row("held", [{"id": "X-1", "hold": hold}]),
                                            row("empty", [])]})
    assert [a["id"] for a in out["agents"]] == ["held"]
    assert out["collapsed"]["count"] == 1


def test_an_old_dismissed_agent_stays_in_full_and_out_of_the_collapsed_count(client, auth, _db):
    from app.models import Agent

    pid = _proj(client, auth)
    _seed(_db, pid, n_gone=5)
    old = datetime.now(timezone.utc) - timedelta(days=3)
    retired = Agent(id=f"{pid}-retired", project_id=pid, number=999, active_role="worker",
                    state="idle", last_seen_at=old, dismissed_at=old)
    _db.add(retired)
    _db.commit()
    body = client.get(f"/api/fleet?project_id={pid}", headers=auth).json()
    row = next((a for a in body["agents"] if a["id"] == retired.id), None)
    assert row is not None and row["dismissed"] is True, \
        "a dismissed agent stays listed, where the view can restore it"
    assert body["collapsed"]["count"] == 5
    assert body["total"] == len(body["agents"]) + body["collapsed"]["count"]
    page = client.get(f"/api/fleet/agents/collapsed?project_id={pid}", headers=auth).json()
    assert page["total"] == 5 and retired.id not in {a["id"] for a in page["agents"]}


def test_mcp_fleet_status_is_not_collapsed(_db, client, auth):
    from app.services import fleet

    pid = _proj(client, auth)
    _seed(_db, pid, n_gone=20)
    assert len(fleet.fleet_status(_db, pid)["agents"]) == 26
