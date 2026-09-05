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
    row = _row("claude", "sonnet", vendor="anthropic", cost_class="cheap")
    key = ("anthropic", "sonnet", "any", "cheap")  # measured cells are keyed by VENDOR (D7)
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


# ---- PR 2: profile and policy arrive on fleet_status, read once at launch (D9, D10, D14) -----

class _Server:
    def __init__(self, status=None, fail=False):
        self.status, self.fail, self.calls = status, fail, 0

    def fleet_status(self, **kw):
        self.calls += 1
        if self.fail:
            raise RuntimeError("connection refused")
        return self.status


def test_read_preferences_takes_the_profile_and_policy_off_fleet_status():
    from gbfleet.mcp import read_preferences

    profile, policy, note, measured = read_preferences(_Server({
        "agents": [],
        "profile": {"user": "u1", "defaults": ["gbagent", "claude"], "weights": {"cost": 1.0}, "excludes": ["grok"]},
        "policy": {"local_only": True, "reviewer_cross_vendor": False, "allowed_harnesses": []},
        "measured": [{"vendor": "gbagent", "model": "q", "lane": "backend", "tier": "cheap",
                      "quality": {"value": 0.8, "n": 5}, "latency": None}],
    }))
    assert measured == {("gbagent", "q", "backend", "cheap"): {"quality": m.Sample(0.8, 5)}}
    assert "measured cells: 1" in note
    assert profile is not None and profile.user == "u1" and profile.defaults == ("gbagent", "claude")
    assert profile.excludes == ("grok",) and profile.normalised() == {"cost": 1.0}
    assert policy.local_only is True and policy.allowed_harnesses == ()
    assert "u1" in note and "policy on" in note


def test_read_preferences_spells_absence_and_unreachability_rather_than_inventing_a_default():
    from gbfleet.mcp import read_preferences

    profile, policy, note, measured = read_preferences(_Server({"agents": [], "profile": None, "policy": None}))
    assert profile is None and policy == m.Policy() and "profile: none" in note and "policy: none" in note
    assert measured == {}
    profile, policy, note, measured = read_preferences(_Server(fail=True))
    assert profile is None and policy == m.Policy() and measured == {}
    assert "unreachable" in note and "connection refused" in note


def test_spawn_under_the_launch_profile_and_policy_explains_who_and_what_decided(fleet: Fleet, monkeypatch):
    """Criterion 6 through spawn: the profile's allowlist names no harness the matrix has, so
    even though `fake` is installed nothing spawns, and the refusal names the profile."""
    monkeypatch.setattr(m, "installed_checker", lambda *a, **k: (lambda r: (r.harness == "fake", "not here")))
    fleet.profile = m.Profile(user="alex", defaults=("nobody",))
    out = _call(fleet, "spawn", tier="cheap", enrolment_code="WORKER-1")
    assert out.get("isError") and "your profile's defaults or excludes removed every row" in out["content"][0]["text"]
    assert fleet.children == []

    fleet.profile = m.Profile(user="alex", defaults=("fake", "ghost"), weights={"cost": 1.0})
    fleet.policy = m.Policy(local_only=True)
    out = _call(fleet, "spawn", tier="cheap", enrolment_code="WORKER-1")
    res = out["structuredContent"]["resolution"]
    assert res["profile"] == {"user": "alex", "defaults": ["fake", "ghost"], "weights": {"cost": 1.0}}
    assert res["dropped"]["policy"] == ["ghost:x (local_only; would have scored 0.60)"]
    assert res["winner"]["harness"] == "fake"


def test_spawn_passes_the_builders_vendor_into_a_reviewer_resolution(git_repo: Path, tmp_path: Path, scripts, state: Path, monkeypatch):
    """Criterion 14 through spawn: under reviewer_cross_vendor the row sharing the builder's
    vendor is dropped; the other vendor's row runs."""
    monkeypatch.setattr(m, "installed_checker", lambda *a, **k: (lambda r: (True, "")))
    seen: list[tuple[str, str]] = []

    def launch_for(name, model="", tuning=None):
        seen.append((name, model))
        return _factory(scripts, "works_then_waits", adapter="fake")

    rows = (_row("fake", "anth-review", vendor="anthropic", role="reviewer", tier="frontier", cost_class="frontier"),
            _row("fake", "xai-review", vendor="xai", role="reviewer", tier="frontier", cost_class="frontier", order=2))
    f = Fleet(repo=git_repo, workspace=tmp_path / "ws", client=_server(tmp_path / "ws"), launch_for=launch_for,
              matrix=_matrix(*rows), policy=m.Policy(reviewer_cross_vendor=True))
    out = _call(f, "spawn", tier="frontier", role="reviewer", builder_vendor="anthropic", enrolment_code="REVIEWER-1")
    got = out["structuredContent"]
    assert seen == [("fake", "xai-review")], got
    assert "reviewer_cross_vendor" in got["resolution"]["dropped"]["policy"][0]


# ---- PR 3: measured cells are per lane and per tier, joined on the vendor (D7, criterion 11) --

def test_a_row_for_any_lane_reads_the_cell_of_the_lane_being_resolved_never_a_pooled_one():
    row = _row("gbagent", "q", vendor="gbagent", cost_class="local", local=True)
    prof = m.Profile(user="u", weights={"quality": 1.0})
    measured = {
        ("gbagent", "q", "backend", "cheap"): {"quality": m.Sample(1.0, 5)},
        ("gbagent", "q", "frontend", "cheap"): {"quality": m.Sample(0.0, 5)},
    }
    back = _matrix(row).resolve(tier="cheap", lane="backend", profile=prof, measured=measured, installed=ALL_INSTALLED)
    front = _matrix(row).resolve(tier="cheap", lane="frontend", profile=prof, measured=measured, installed=ALL_INSTALLED)
    none = _matrix(row).resolve(tier="cheap", profile=prof, measured=measured, installed=ALL_INSTALLED)
    assert back.explain()["winner"]["score"] == 1.0
    assert front.explain()["winner"]["score"] == 0.0
    assert none.explain()["winner"]["axes"]["quality"]["used"] is False, "no lane named: nothing pooled, unmeasured"


def test_measured_of_reads_the_servers_list_and_skips_axes_it_did_not_measure():
    got = m.measured_of([
        {"vendor": "gbagent", "model": "q", "lane": "backend", "tier": "cheap",
         "quality": {"value": 0.75, "n": 4}, "latency": {"value": 0.5, "n": 3, "median_seconds": 1800.0}},
        {"vendor": "undeclared", "model": "undeclared", "lane": "backend", "tier": "cheap",
         "quality": {"value": 1.0, "n": 1}, "latency": None},
        {"broken": True},
    ])
    assert got[("gbagent", "q", "backend", "cheap")] == {"quality": m.Sample(0.75, 4), "latency": m.Sample(0.5, 3)}
    assert got[("undeclared", "undeclared", "backend", "cheap")] == {"quality": m.Sample(1.0, 1)}
    assert len(got) == 2
    assert m.measured_of(None) == {}


def test_doctor_shows_measured_cells_per_lane_for_a_row_that_spans_lanes():
    row = _row("gbagent", "q", vendor="gbagent", cost_class="local", local=True)
    measured = {("gbagent", "q", "backend", "cheap"): {"quality": m.Sample(0.8, 5)},
                ("gbagent", "q", "frontend", "cheap"): {"quality": m.Sample(0.2, 2)}}
    lines = m.doctor_lines(_matrix(row), ALL_INSTALLED, None, None, measured)
    detail = next(d for n, _, d in lines if n == "matrix gbagent:q")
    assert "quality/backend 0.80 (n=5)" in detail and "quality/frontend 0.20 (n=2, unmeasured)" in detail


# ---- PR 3: the Qwen Code adapter (D13, criterion 16) -----------------------------------------

def test_qwen_code_is_registered_and_its_matrix_row_is_verified_by_a_named_walk_with_no_model():
    from gbfleet.adapters import ADAPTERS
    assert "qwen-code" in ADAPTERS and ADAPTERS["qwen-code"].binary == "qwen"
    rows = [r for r in m.load().rows if r.harness == "qwen-code"]
    assert rows and rows[0].status == "verified" and rows[0].latest.item == "GRPH-731"
    assert rows[0].model == "", (
        "qwen 0.23.0 silently replaces an unknown -m with its default; a named model here would be unenforced")
    assert "qwen-code" not in m.unregistered_adapter_files()


def test_qwen_code_launch_keeps_the_seat_out_of_argv_and_the_worktree_and_rewrites_it_to_httpurl(git_repo: Path, tmp_path: Path):
    from gbfleet.adapters import ADAPTERS, Tuning
    from gbfleet.seat import Seat
    from gbfleet.worktree import create

    seat = Seat(code="WORKER-7F3K", server_url="https://gb.invalid", api_key="gbk_secret")
    tree = create(git_repo, tmp_path / "w-qwen", "wave", "1")
    instruction = tmp_path / "instruction.txt"
    instruction.write_text("register with WORKER-7F3K")
    launch = ADAPTERS["qwen-code"].launch(seat, tree, instruction, Path("/usr/bin/true"), model="qwen3.7-plus",
                                          tuning=Tuning(turns="40"))
    joined = " ".join(launch.argv)
    assert "WORKER-7F3K" not in joined and "gbk_secret" not in joined
    assert launch.stdin_file == instruction, "the instruction, and so the code, arrives on stdin"
    assert "--mcp-config" in launch.argv and "--allowed-mcp-server-names" in launch.argv and "graphban" in launch.argv
    assert launch.argv[launch.argv.index("-m") + 1] == "qwen3.7-plus"
    assert launch.argv[launch.argv.index("--max-session-turns") + 1] == "40"
    assert "--bare" not in launch.argv, "--bare drops the model-provider config and the child dies at once"
    assert not str(launch.seat_path).startswith(str(tree.path)), "the seat file lives outside the worktree"
    server = launch.config["mcpServers"]["graphban"]
    assert server == {"httpUrl": "https://gb.invalid/api/mcp", "headers": {"X-API-Key": "gbk_secret"}}
    assert "url" not in server and "type" not in server, "the generic shape is accepted and never connects (measured)"


def test_qwen_code_names_its_exit_codes_and_refuses_an_unsupported_knob():
    from gbfleet.adapters import ADAPTERS, Support, parse_version
    a = ADAPTERS["qwen-code"]
    assert "budget" in a.exit_meaning(55) and "max-wall-time" in a.exit_meaning(55)
    assert a.exit_meaning(0) == "finished"
    assert a.support.permits(parse_version("0.23.0")) and not a.support.permits(parse_version("0.22.9"))
    assert not a.support.permits(parse_version("1.0.0"))
    assert a.tuning == frozenset({"turns"}), "only the turn budget; effort/fallback/window are other vendors' knobs"
    assert a.known_models(Path("/usr/bin/true")) is None, "cannot be asked, and a wrong -m is not refused by the binary either"
    assert a.debug_argv(Path("/tmp/x")) == [], "-d writes to stderr; a path cannot be honoured, so say cannot"


# ---- fix: the doctor resolves under the profile the server serves, as a spawn would ----------

def test_doctor_resolves_under_the_servers_profile_policy_and_measured_cells(git_repo: Path, monkeypatch):
    from gbfleet import doctor as doctor_mod

    def fake_read(client):
        return (m.Profile(user="alex", defaults=("gbagent", "claude"), weights={"cost": 1.0}),
                m.Policy(local_only=True), "profile alex (2 default(s)); policy on; measured cells: 1",
                {("gbagent", "qwen3.6:35b-a3b-coding-mtp-det", "backend", "cheap"): {"quality": m.Sample(0.8, 5)}})
    import gbfleet.mcp as mcp_mod
    monkeypatch.setattr(mcp_mod, "read_preferences", fake_read)
    monkeypatch.setattr(m, "installed_checker", lambda *a, **k: (lambda r: (True, "")))
    report = doctor_mod.run(repo=git_repo, out=io.StringIO(), server="http://gb.invalid", api_key="k", project="p")
    by = {f.name: f for f in report.findings}
    assert by["matrix preferences"].status == "PASS" and "profile alex" in by["matrix preferences"].detail
    assert "profile alex" in by["resolve worker/cheap"].detail, "the resolution line names whose profile decided"
    assert "quality/backend 0.80 (n=5)" in by["matrix gbagent:qwen3.6:35b-a3b-coding-mtp-det"].detail
    assert "local_only" in by["resolve worker/frontier"].detail or by["resolve worker/frontier"].status == "UNKNOWN", (
        "a local_only policy must show on the frontier resolution: every frontier row is cloud")


def test_doctor_without_a_server_says_the_resolutions_assume_nothing(git_repo: Path):
    from gbfleet import doctor as doctor_mod
    report = doctor_mod.run(repo=git_repo, out=io.StringIO())
    by = {f.name: f for f in report.findings}
    assert by["matrix preferences"].status == "UNKNOWN" and "no profile" in by["matrix preferences"].detail
