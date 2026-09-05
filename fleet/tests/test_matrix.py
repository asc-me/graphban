"""PRD-37 PR 1 — the preference matrix and the resolver (criteria 1, 2, 5, 7, 8, 9, 10, 12, 13, 15).

Profile and policy come from the server in PR 2; here they are built directly, because the
resolver's contract is the same whoever hands them in."""
from __future__ import annotations

import io
from pathlib import Path

import pytest

from gbfleet import matrix as m
from gbfleet.adapters import ADAPTERS
from gbfleet.mcp import Fleet, handle
from gbfleet.tiers import TierTable
from gbfleet import doctor
from tests.test_supervisor import _factory, _server


def _row(harness="h", model="m", *, vendor="v", lane="any", role="worker", tier="cheap",
         status="unverified", order=1, cost_class="cheap", local=False, evidence=()) -> m.Row:
    return m.Row(harness=harness, model=model, vendor=vendor, lane=lane, role=role, tier=tier,
                 status=status, order=order, cost_class=cost_class, local=local,
                 evidence=tuple(evidence))


def _matrix(*rows: m.Row) -> m.Matrix:
    return m.Matrix(rows=tuple(rows), path=Path("<test>"))


ALL_INSTALLED = lambda r: (True, "")  # noqa: E731
EV = m.Evidence(item="GRPH-1", date="2026-09-01", outcome="signed_off")


# ---- the committed matrix (criteria 1, 2) ------------------------------------------------------

def test_the_shipped_matrix_loads_and_every_verified_row_names_its_evidence():
    mat = m.load()
    assert mat.path.name == "matrix.toml"
    verified = [r for r in mat.rows if r.status == "verified"]
    assert verified, "the shipped matrix has nothing verified — PRD-24's walk is evidence"
    for r in verified:
        assert r.evidence and r.latest.item.startswith("GRPH-")


def test_every_adapter_file_without_a_registration_has_a_row_saying_so():
    """Criterion 2: codex.py exists and is not in ADAPTERS; the matrix must say so rather than
    let the absence read as 'no such harness'."""
    mat = m.load()
    orphans = m.unregistered_adapter_files()
    assert "codex" in orphans
    for name in orphans:
        assert name not in ADAPTERS
        assert any(r.harness == name and r.status == "unregistered" for r in mat.rows), name
    for r in mat.rows:
        if r.status == "unregistered":
            assert r.harness not in ADAPTERS, f"{r.harness} is registered; the row is stale"


def test_a_verified_row_without_evidence_is_refused_at_load(tmp_path: Path):
    p = tmp_path / "m.toml"
    p.write_text('[[rows]]\nharness="gbagent"\nmodel="x"\nvendor="g"\nlane="any"\nrole="worker"\n'
                 'tier="cheap"\nstatus="verified"\norder=1\ncost_class="local"\nlocal=true\nevidence=[]\n')
    with pytest.raises(m.MatrixError, match="evidence"):
        m.load(p)


def test_an_unregistered_row_for_a_registered_harness_is_refused_at_load(tmp_path: Path):
    p = tmp_path / "m.toml"
    p.write_text('[[rows]]\nharness="gbagent"\nmodel=""\nvendor="g"\nlane="any"\nrole="worker"\n'
                 'tier="cheap"\nstatus="unregistered"\norder=1\ncost_class="local"\nlocal=true\nevidence=[]\n')
    with pytest.raises(m.MatrixError):
        m.load(p)


# ---- resolution order (criteria 5, 7, 8) ---------------------------------------------------------

def test_policy_filters_before_profile_and_the_explanation_shows_the_score_it_lost_with():
    """D15: a project's local_only removes the frontier row the user weighted for quality —
    the dropped line carries the score it would have had, so taste visibly loses to a rule."""
    local = _row("gbagent", "q", vendor="gbagent", cost_class="local", local=True, status="verified", evidence=[EV])
    cloud = _row("claude", "opus", vendor="anthropic", cost_class="frontier")
    prof = m.Profile(user="alex", defaults=("claude", "gbagent"), weights={"quality": 1.0, "cost": 0.0})
    res = _matrix(local, cloud).resolve(tier="cheap", profile=prof, policy=m.Policy(local_only=True), installed=ALL_INSTALLED)
    assert res.winner is local
    out = res.explain()
    assert out["source"] == "matrix"
    assert out["dropped"]["policy"] == ["claude:opus (local_only; would have scored 0.00)"]
    assert out["eligible"] == {"matrix": 2, "after_policy": 1, "after_profile": 1, "after_failed": 1, "after_installed": 1}


def test_the_profile_defaults_are_an_allowlist_and_a_zero_weight_is_not_an_exclusion():
    a = _row("claude", "sonnet", cost_class="cheap")
    b = _row("cursor-agent", "composer", cost_class="cheap", order=2)
    prof = m.Profile(user="u", defaults=("cursor-agent",), weights={"cost": 0.0, "quality": 0.0})
    res = _matrix(a, b).resolve(tier="cheap", profile=prof, installed=ALL_INSTALLED)
    assert res.winner is b
    assert res.explain()["dropped"]["profile"] == ["claude:sonnet (not in your defaults)"]
    assert res.explain()["profile"]["weights"] == {}, "all-zero weights mean indifferent, not empty"


def test_a_failed_row_never_spawns_even_when_it_is_the_only_one_and_the_refusal_names_the_step():
    bad = _row("gbagent", "qwen3-coder:30b", status="failed", cost_class="local", local=True,
               evidence=[m.Evidence("GRPH-497", "2026-08-25", "failed")])
    res = _matrix(bad).resolve(tier="cheap", installed=ALL_INSTALLED)
    assert res.winner is None
    assert res.refused == "every remaining row is marked failed"
    assert res.explain()["winner"] is None and res.explain()["refused"] == res.refused


def test_installed_is_checked_last_so_the_winner_on_score_is_named_when_it_is_missing_here():
    best = _row("gbagent", "q", cost_class="local", local=True, status="verified", evidence=[EV])
    other = _row("claude", "sonnet", cost_class="cheap", order=2)
    res = _matrix(best, other).resolve(tier="cheap", installed=lambda r: (r.harness != "gbagent", "gbagent is not on PATH"))
    assert res.winner is other
    assert res.explain()["dropped"]["installed"] == ["gbagent:q (gbagent is not on PATH)"]


def test_an_empty_resolution_is_a_refusal_with_the_emptying_step_never_a_silent_default():
    res = _matrix(_row("claude", "opus", tier="frontier")).resolve(tier="cheap", installed=ALL_INSTALLED)
    assert res.winner is None and "no worker row for tier 'cheap'" in res.refused
    res = _matrix(_row("claude", "opus")).resolve(tier="cheap", policy=m.Policy(allowed_harnesses=("gbagent",)), installed=ALL_INSTALLED)
    assert res.refused == "project policy removed every row"
    res = _matrix(_row("claude", "opus")).resolve(tier="cheap", profile=m.Profile(user="u", excludes=("claude",)), installed=ALL_INSTALLED)
    assert res.refused == "your profile's defaults or excludes removed every row"
    res = _matrix(_row("claude", "opus")).resolve(tier="cheap", installed=lambda r: (False, "no claude binary"))
    assert res.refused == "no eligible row is installed on this machine"


def test_reviewer_cross_vendor_drops_the_builders_vendor_for_reviewers_only():
    anth = _row("claude", "opus", vendor="anthropic", role="reviewer", tier="frontier", cost_class="frontier")
    xai = _row("grok", "grok-4.5", vendor="xai", role="reviewer", tier="frontier", cost_class="frontier", order=2)
    pol = m.Policy(reviewer_cross_vendor=True)
    res = _matrix(anth, xai).resolve(tier="frontier", role="reviewer", policy=pol, installed=ALL_INSTALLED, builder_vendor="anthropic")
    assert res.winner is xai
    assert "reviewer_cross_vendor" in res.explain()["dropped"]["policy"][0]
    worker = _row("claude", "opus", vendor="anthropic", tier="frontier", cost_class="frontier")
    assert _matrix(worker).resolve(tier="frontier", policy=pol, installed=ALL_INSTALLED, builder_vendor="anthropic").winner is worker


# ---- scoring, samples and ties (criteria 9, 10, 12) ----------------------------------------------

def test_measured_axes_below_min_sample_contribute_nothing_and_say_so():
    row = _row("claude", "sonnet", cost_class="cheap")
    key = ("claude", "sonnet", "any", "cheap")
    prof = m.Profile(user="u", weights={"quality": 1.0})
    thin = {key: {"quality": m.Sample(value=1.0, n=m.MIN_SAMPLE - 1)}}
    fat = {key: {"quality": m.Sample(value=1.0, n=m.MIN_SAMPLE)}}
    res_thin = _matrix(row).resolve(tier="cheap", profile=prof, measured=thin, installed=ALL_INSTALLED)
    res_fat = _matrix(row).resolve(tier="cheap", profile=prof, measured=fat, installed=ALL_INSTALLED)
    assert res_thin.explain()["winner"]["score"] == 0.0
    assert res_thin.explain()["winner"]["axes"]["quality"] == {"value": None, "n": 4, "used": False, "note": "unmeasured"}
    assert res_fat.explain()["winner"]["score"] == 1.0
    assert res_fat.explain()["winner"]["axes"]["quality"]["used"] is True


def test_weights_are_normalised_and_cost_favours_local_over_cheap_over_frontier():
    local = _row("gbagent", "q", cost_class="local", local=True)
    cheap = _row("claude", "sonnet", cost_class="cheap", order=2)
    frontier = _row("claude", "opus", cost_class="frontier", order=3)
    prof = m.Profile(user="u", weights={"cost": 3.0, "locality": 1.0})
    res = _matrix(frontier, cheap, local).resolve(tier="cheap", profile=prof, installed=ALL_INSTALLED)
    out = res.explain()
    assert out["profile"]["weights"] == {"cost": 0.75, "locality": 0.25}
    assert [s[0].key for s in res.scored] == ["gbagent:q", "claude:sonnet", "claude:opus"]
    assert out["winner"]["score"] == 1.0 and out["runner_up"]["harness"] == "claude"


def test_ties_break_verified_then_the_users_defaults_order_then_matrix_order():
    a = _row("claude", "sonnet", cost_class="cheap", order=1)
    b = _row("cursor-agent", "composer", cost_class="cheap", order=2)
    c = _row("grok", "fast", cost_class="cheap", order=3, status="verified", evidence=[EV])
    mat = _matrix(a, b, c)
    assert mat.resolve(tier="cheap", installed=ALL_INSTALLED).winner is c, "verified wins a tie"
    prof = m.Profile(user="u", defaults=("cursor-agent", "claude"))
    assert mat.resolve(tier="cheap", profile=prof, installed=ALL_INSTALLED).winner is b, "then the user's own order"
    assert mat.resolve(tier="cheap", profile=m.Profile(user="u", defaults=("claude", "cursor-agent")), installed=ALL_INSTALLED).winner is a
    assert _matrix(b, a).resolve(tier="cheap", installed=ALL_INSTALLED).winner is a, "then the matrix's order field"


# ---- spawn goes through the matrix when the tier has no flag (criteria 13, 15) -------------------

@pytest.fixture
def fleet(git_repo: Path, tmp_path: Path, scripts, state: Path) -> Fleet:
    workspace = tmp_path / "ws"
    seen: list[tuple[str, str]] = []

    def launch_for(name, model="", tuning=None):
        seen.append((name, model))
        return _factory(scripts, "works_then_waits", adapter=name)

    rows = (_row("fake", "qwen-local", cost_class="local", local=True, status="verified", evidence=[EV]),
            _row("fake", "opus", tier="frontier", cost_class="frontier"),
            _row("ghost", "x", cost_class="cheap", order=2))
    f = Fleet(repo=git_repo, workspace=workspace, client=_server(workspace), launch_for=launch_for,
              tiers=TierTable.parse(["frontier=fake:flagged"]), matrix=_matrix(*rows))
    f.seen = seen  # type: ignore[attr-defined]
    return f


def _call(fleet: Fleet, tool: str, **args) -> dict:
    reply = handle(fleet, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": tool, "arguments": args}})
    return reply["result"]


def test_spawn_resolves_an_unflagged_tier_through_the_matrix_and_the_reply_explains(fleet: Fleet, monkeypatch):
    monkeypatch.setattr(m, "installed_checker", lambda *a, **k: (lambda r: (r.harness == "fake", "ghost is not installed")))
    out = _call(fleet, "spawn", tier="cheap", enrolment_code="WORKER-1")
    got = out["structuredContent"]
    assert fleet.seen == [("fake", "qwen-local")]  # type: ignore[attr-defined]
    assert got["tier"] == "cheap" and got["adapter"] == "fake" and got["model"] == "qwen-local"
    res = got["resolution"]
    assert res["source"] == "matrix"
    assert res["winner"]["harness"] == "fake" and res["winner"]["status"] == "verified"
    assert res["dropped"]["installed"] == ["ghost:x (ghost is not installed)"]
    assert res["profile"] == "none", "PR 1 carries no profile; the reply says so rather than inventing one"


def test_a_tier_flag_beats_the_matrix_and_the_reply_says_the_source_was_the_flag(fleet: Fleet):
    out = _call(fleet, "spawn", tier="frontier", enrolment_code="WORKER-1")
    got = out["structuredContent"]
    assert fleet.seen == [("fake", "flagged")]  # type: ignore[attr-defined]
    assert got["resolution"] == {"source": "flag", "tier": "frontier", "adapter": "fake", "model": "flagged"}


def test_a_refused_resolution_is_a_tool_error_naming_the_step_and_spawns_nothing(fleet: Fleet, monkeypatch):
    monkeypatch.setattr(m, "installed_checker", lambda *a, **k: (lambda r: (False, "nothing here")))
    out = _call(fleet, "spawn", tier="cheap", enrolment_code="WORKER-1")
    assert out.get("isError")
    text = out["content"][0]["text"]
    assert "no eligible row is installed on this machine" in text and "nothing here" in text
    assert fleet.children == [] and fleet.seen == []  # type: ignore[attr-defined]


# ---- doctor (criterion 1's operator view) ------------------------------------------------------

def test_doctor_lists_every_row_and_what_each_tier_resolves_to(git_repo: Path):
    report = doctor.run(repo=git_repo, out=io.StringIO())
    names = {f.name for f in report.findings}
    assert "matrix" in names
    assert "matrix codex" in names and "matrix gbagent:qwen3.6:35b-a3b-coding-mtp-det" in names
    for role in m.ROLES:
        for tier in m.TIERS:
            assert f"resolve {role}/{tier}" in names
    by = {f.name: f for f in report.findings}
    assert by["matrix codex"].status == "UNKNOWN"
    assert "unregistered" in by["matrix codex"].detail


def test_doctor_names_the_row_that_breaks_a_custom_matrix(git_repo: Path, tmp_path: Path):
    p = tmp_path / "m.toml"
    p.write_text('[[rows]]\nharness="gbagent"\nmodel="x"\nvendor="g"\nlane="any"\nrole="worker"\n'
                 'tier="cheap"\nstatus="verified"\norder=1\ncost_class="local"\nlocal=true\nevidence=[]\n')
    report = doctor.run(repo=git_repo, out=io.StringIO(), matrix_path=str(p))
    by = {f.name: f for f in report.findings}
    assert by["matrix"].status == "FAIL" and by["matrix"].remedy
    assert "evidence" in by["matrix"].detail


# ---- until resolves through the matrix when its --request tier has no flag -------------------

def test_until_resolves_an_unflagged_request_through_the_matrix_and_the_log_says_how(
    git_repo: Path, tmp_path: Path, scripts, state: Path, monkeypatch,
):
    from gbfleet.until import run
    from tests.test_until import _clients, KEY

    monkeypatch.setattr(m, "installed_checker", lambda *a, **k: (lambda r: (r.harness == "fake", "not here")))
    workspace = tmp_path / "ws"
    delegations: list = []
    launched: list[tuple[str, str]] = []

    def launch_for(name, model=""):
        launched.append((name, model))
        return _factory(scripts, "works_then_exits", adapter=name)

    planner, supervisor = _clients(workspace, clusters=1, cluster_items=[["GRPH-7"]], delegations=delegations)
    mat = _matrix(_row("ghost", "x", cost_class="cheap"),
                  _row("fake", "qwen-local", cost_class="local", local=True, order=2))
    result = run(
        git_repo, _factory(scripts, "works_then_exits"),
        planner, supervisor, api_key=KEY, server="http://gb.invalid", adapter="fake",
        state=state, workspace=workspace, poll=0, sleep=lambda _: None, empty_ticks=1,
        request="cheap", tiers=TierTable(), launch_for=launch_for, matrix=mat,
    )
    assert result.spawned == 1, result.detail
    assert launched == [("fake", "qwen-local")], "the matrix, not the default factory, chose the launch"
    assert [d["tier"] for d in delegations] == ["cheap"]
