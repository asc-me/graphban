"""The supervisor finishes the merge after sign-off, opt-in (GRPH-846).

`done` is a ledger state, not a git state. After `sign_off` the PR sat as a draft until a
person merged it — and `until` HOLDS every item whose finished dependency is not on the base
(GRPH-798), so each such PR was idle fleet time waiting for a click. This is the click.

Two sabotage anchors the item names:
- compare branch NAMES instead of the head commit to the attested commit → the post-review
  push test in `test_propose.py` must fail;
- default the flag ON → `test_until_without_merge_never_calls_gh` must fail.

And the criterion that makes the feature worth having: after a merge, the GRPH-798 hold on
a dependant clears on the next tick, without a restart — walked here against a real remote.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import httpx
import pytest

from gbfleet import propose as propose_mod
from gbfleet import supervisor as sup
from gbfleet.client import ALLOWED_TOOLS, Graphban, NotPermitted
from gbfleet.supervisor import MERGE_RECHECK_S, Merger, Wave
from gbfleet.until import PLANNER_TOOLS, run

from tests.test_propose import MERGE_OID, REVIEWED, _Forge, _att
from tests.test_supervisor import KEY, _factory
from tests.test_until import _error, _mcp

from conftest import telemetry_ack  # noqa: E402

REPO = Path("/tmp/does-not-matter")


def _done_item(item_id: str, *, reviewed: str = REVIEWED, green: str = REVIEWED,
               branch: str = "gb/w-1", status: str = "done") -> dict:
    return {
        "id": item_id, "status": status, "branch": branch, "pr": None,
        "evidence": [_att("github-actions", green, ("suite_green", True)),
                     _att("fleet.sign_off", reviewed, ("independent_review", True))],
    }


class _Ledger:
    """A fake Graphban `call`: hands out items and records every write."""

    def __init__(self, items: dict[str, dict], *, allowed=None):
        self.items = items
        self.allowed = allowed
        self.calls: list[tuple[str, dict]] = []

    def call(self, tool, **kw):
        if self.allowed is not None and tool not in self.allowed:
            raise NotPermitted(f"this credential may not call {tool!r}")
        self.calls.append((tool, kw))
        if tool == "get_item_details":
            return dict(self.items[kw["id"]])
        if tool == "search_items":
            return {"results": [r for r in self.items.values() if r["status"] == "review"]}
        if tool == "update_item":
            self.items[kw["id"]].setdefault("evidence", []).extend(kw.get("evidence") or [])
            return {"id": kw["id"]}
        raise AssertionError(f"unexpected call {tool}")

    def wrote(self, tool: str) -> list[dict]:
        return [kw for t, kw in self.calls if t == tool]


def _only_gh(fake):
    """Intercept `gh` and nothing else. `propose.subprocess` IS the `subprocess` module, so a
    patch there is global — and the loop under test runs real git through the same door."""
    real = subprocess.run

    def run(argv, **kw):
        return fake(argv, **kw) if argv and argv[0] == "gh" else real(argv, **kw)
    return run


@pytest.fixture()
def forge(monkeypatch):
    def _wire(fake):
        monkeypatch.setattr(propose_mod, "find", lambda: "/usr/bin/gh")
        monkeypatch.setattr(propose_mod.subprocess, "run", _only_gh(fake))
        return fake
    return _wire


# ---- the merger on its own ---------------------------------------------------------------------

def test_an_item_that_leaves_review_as_done_is_merged_and_recorded(forge):
    """Seen in review, gone next tick, `done` when read: merge it, and put the MERGE COMMIT
    on the item — a squash rewrites the SHA, so without that receipt the dependency check
    would hold every dependant forever on a merge that happened."""
    gh = forge(_Forge(draft=True))
    ledger = _Ledger({"SA-417": _done_item("SA-417", status="review")})
    merger = Merger(REPO, ledger, enabled=True)
    wave = Wave()

    merger.note_review([{"id": "SA-417", "branch": "gb/w-1"}])
    ledger.items["SA-417"]["status"] = "done"
    landed = merger.tick(wave, rows=[])

    assert landed is True
    got = wave.merged["SA-417"]
    assert got.ok and got.commit == MERGE_OID
    assert "pr merge" in [" ".join(c[1:3]) for c in gh.calls]
    receipts = ledger.wrote("update_item")
    assert len(receipts) == 1 and receipts[0]["id"] == "SA-417"
    row = receipts[0]["evidence"][0]
    assert row["kind"] == "url" and row["commit"] == MERGE_OID and row["url"].endswith("/pull/9")
    assert "SA-417" not in merger.watching, "a merged item is still being re-asked"


def test_off_by_default_the_merger_asks_nothing(forge):
    gh = forge(_Forge())
    ledger = _Ledger({"SA-417": _done_item("SA-417")})
    merger = Merger(REPO, ledger)

    merger.note_review([{"id": "SA-417", "branch": "gb/w-1"}])
    merger.note_hold([{"id": "SA-417", "commits": [REVIEWED]}])
    landed = merger.tick(Wave(), rows=[])

    assert merger.enabled is False and landed is False
    assert gh.calls == [] and ledger.calls == []


def test_a_bounced_item_is_not_a_candidate(forge):
    """Left review as `next`, not `done`. Nothing to merge, and nothing asked of the forge."""
    gh = forge(_Forge())
    ledger = _Ledger({"SA-417": _done_item("SA-417", status="next")})
    merger = Merger(REPO, ledger, enabled=True)

    merger.note_review([{"id": "SA-417", "branch": "gb/w-1"}])
    merger.tick(Wave(), rows=[])

    assert gh.calls == []
    assert "SA-417" not in merger.watching


def test_a_held_dependency_is_a_candidate(forge):
    """The GRPH-798 hold names a finished item whose PR is the click the dependant waits on.
    That is the merge most worth finishing."""
    gh = forge(_Forge(draft=False))
    ledger = _Ledger({"SA-417": _done_item("SA-417")})
    merger = Merger(REPO, ledger, enabled=True)
    wave = Wave()

    merger.note_hold([{"id": "SA-417", "title": "t", "commits": [REVIEWED]}])
    merger.tick(wave, rows=[])

    assert wave.merged["SA-417"].ok
    assert "pr merge" in [" ".join(c[1:3]) for c in gh.calls]


def test_a_precondition_miss_leaves_the_item_alone_and_is_re_asked_later(forge):
    """Head moved: nothing touched, the reason names it, and the item stays watched — the
    reviewer may re-sign — but is not asked again inside the recheck interval."""
    gh = forge(_Forge(draft=True, head="b" * 40))
    ledger = _Ledger({"SA-417": _done_item("SA-417")})
    merger = Merger(REPO, ledger, enabled=True)
    wave = Wave()
    clock = {"t": 1000.0}

    merger.note_hold([{"id": "SA-417", "commits": [REVIEWED]}])
    merger.tick(wave, rows=[], now=lambda: clock["t"])
    got = wave.merged["SA-417"]
    assert not got.ok and not got.skipped and "reviewed commit" in got.reason
    assert [" ".join(c[1:3]) for c in gh.calls] == ["pr view"]
    assert ledger.wrote("update_item") == []
    assert "SA-417" in merger.watching

    clock["t"] += MERGE_RECHECK_S / 2
    merger.tick(wave, rows=[], now=lambda: clock["t"])
    assert len(gh.calls) == 1, "re-asked the forge inside the recheck interval"

    clock["t"] += MERGE_RECHECK_S
    merger.tick(wave, rows=[], now=lambda: clock["t"])
    assert len(gh.calls) == 2, "never re-asked after the interval"


def test_skipped_is_final_and_pending_is_not(forge):
    gh = forge(_Forge(draft=False, status="BLOCKED"))
    ledger = _Ledger({"SA-417": _done_item("SA-417"), "SA-418": _done_item("SA-418")})
    merger = Merger(REPO, ledger, enabled=True)
    wave = Wave()

    merger.note_hold([{"id": "SA-417", "commits": [REVIEWED]}])
    merger.tick(wave, rows=[])
    assert wave.merged["SA-417"].skipped and "SA-417" not in merger.watching

    forge(_Forge(draft=False, lands=False))
    merger.note_hold([{"id": "SA-418", "commits": [REVIEWED]}])
    merger.tick(wave, rows=[])
    assert wave.merged["SA-418"].pending and "SA-418" in merger.watching


def test_a_merge_refetches_the_base(forge, monkeypatch):
    """The trunk moved; a remote-tracking ref is only as fresh as the last fetch. Without
    this the dependency check goes on measuring the merge as absent."""
    forge(_Forge(draft=False))
    fetched: list[tuple] = []
    monkeypatch.setattr(sup.wt_mod, "refresh_ref", lambda repo, remote, ref: fetched.append((remote, ref)) or True)
    ledger = _Ledger({"SA-417": _done_item("SA-417")})
    merger = Merger(REPO, ledger, enabled=True, remote="origin", base="origin/main")

    merger.note_hold([{"id": "SA-417", "commits": [REVIEWED]}])
    merger.tick(Wave(), rows=[])

    assert fetched == [("origin", "origin/main")]


def test_a_client_without_the_tools_disables_the_merger_once(forge):
    """A supervisor client holds two reads. Under the flag the CLI widens it; if some caller
    does not, the merger says so once and stops asking, rather than a refusal per tick."""
    gh = forge(_Forge())
    ledger = _Ledger({"SA-417": _done_item("SA-417")}, allowed=ALLOWED_TOOLS)
    merger = Merger(REPO, ledger, enabled=True)
    wave = Wave()

    merger.note_hold([{"id": "SA-417", "commits": [REVIEWED]}])
    merger.tick(wave, rows=[])
    merger.tick(wave, rows=[])

    assert "get_item_details" in merger.disabled_because
    assert gh.calls == [] and wave.merged == {}


def test_a_merge_that_cannot_be_recorded_is_a_failure_not_a_silence(forge):
    """Merged on the forge and unrecorded in the ledger is the state GRPH-798 cannot see
    past. It is reported on the wave rather than left for someone to notice."""
    forge(_Forge(draft=False))

    class _NoWrite(_Ledger):
        def call(self, tool, **kw):
            if tool == "update_item":
                raise NotPermitted("may not")
            return super().call(tool, **kw)

    ledger = _NoWrite({"SA-417": _done_item("SA-417")})
    merger = Merger(REPO, ledger, enabled=True)
    wave = Wave()

    merger.note_hold([{"id": "SA-417", "commits": [REVIEWED]}])
    merger.tick(wave, rows=[])

    assert wave.merged["SA-417"].ok
    assert any("merged as" in f and "not recorded" in f for f in wave.failures)


# ---- through the loop ----------------------------------------------------------------------------

def _server(workspace: Path, *, items: dict[str, dict], review_first: bool = True,
            calls: list | None = None, delegations: list | None = None,
            related: dict[str, list] | None = None, clusters: list | None = None):
    """Planner + supervisor clients over one stateful mock Graphban.

    `items` is the ledger; `update_item` appends evidence to it, so a merge receipt written
    on one tick is what `related_work` reports on the next — the stateful half that lets the
    hold-lifting walk below be real rather than arranged.
    """
    seen = {"review_served": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        ack = telemetry_ack(request)
        if ack is not None:
            return ack
        body = json.loads(request.content)
        tool = body["params"]["name"]
        args = body["params"].get("arguments") or {}
        rid = body["id"]
        if calls is not None:
            calls.append((tool, args))
        if tool == "register_agent":
            return _mcp({"agent_id": "GRPH-P1", "active_role": "planner",
                         "eligible_roles": ["planner"], "tools_off_limits": []}, rid)
        if tool == "get_item_details":
            row = items.get(args.get("id"))
            if row is None:
                return _error("not_found", "no such item", rid)
            return _mcp({**row, "brief": {"lane": {"value": "backend"},
                                          "tier": {"value": "cheap"}, "text": "seed"}}, rid)
        if tool == "update_item":
            items[args["id"]].setdefault("evidence", []).extend(args.get("evidence") or [])
            return _mcp({"id": args["id"]}, rid)
        if tool == "related_work":
            return _mcp({"results": [dict(items[i]) | {"link_types": ["dependency"]}
                                     for i in (related or {}).get(args.get("id"), [])]}, rid)
        if tool == "search_items":
            if args.get("status") == "review":
                seen["review_served"] += 1
                # Held by a reviewer on the first read, gone on the next: signed off.
                rows = ([{"id": i, "branch": r.get("branch", ""), "review_claimed_by": "GRPH-R1"}
                         for i, r in items.items() if r["status"] == "review"]
                        if review_first and seen["review_served"] == 1 else [])
                for i, r in items.items():
                    if r["status"] == "review":
                        r["status"] = "done"
                return _mcp({"results": rows}, rid)
            return _mcp({"results": []}, rid)
        if tool == "collision_clusters":
            # A cluster stays offered until its seed has been delegated.
            taken = {d.get("id") for d in (delegations or [])}
            rows = [{"items": list(c)} for c in (clusters or []) if c[0] not in taken]
            return _mcp({"clusters": rows, "total": len(rows)}, rid)
        if tool == "propose_allocation":
            return _mcp({"workers": 1, "reviewers": 0, "mapping": [], "rationale": "fixture"}, rid)
        if tool == "delegate":
            if delegations is not None:
                delegations.append(dict(args))
            return _mcp({"delegation_id": "d1", "state": "open", "brief": {},
                         "enrolment_code": "WORKER-BOUND1" if args.get("seat") else None}, rid)
        if tool == "mint_enrolment":
            return _mcp({"enrolment_code": "WORKER-MINTED", "role": "worker", "seat_id": "s1"}, rid)
        if tool == "retire_wave":
            return _mcp({"seats_revoked": 0, "agents": 0}, rid)
        trees = sorted(p for p in workspace.glob("*") if p.is_dir() and p.name != "logs")
        return _mcp({"agents": [{"id": f"GRPH-A{i + 1}", "worktree": str(p), "state": "idle",
                                 "enrolled": True, "enrolment_id": f"seat-{i + 1}",
                                 "holdings": []} for i, p in enumerate(trees)]}, rid)

    transport = httpx.MockTransport(handler)
    planner = Graphban("http://gb.invalid", KEY, allowed=PLANNER_TOOLS, transport=transport)
    supervisor = Graphban("http://gb.invalid", KEY, allowed=ALLOWED_TOOLS, transport=transport)
    return planner, supervisor


def _run(git_repo, scripts, state, workspace, planner, supervisor, **kw):
    return run(
        git_repo, _factory(scripts, "works_then_exits"),
        planner, supervisor, api_key=KEY, server="http://gb.invalid", adapter="fake",
        state=state, workspace=workspace, poll=0, sleep=lambda _: None, empty_ticks=3,
        **kw,
    )


def test_until_merge_finishes_the_merge_of_a_signed_off_item(
    git_repo: Path, tmp_path: Path, scripts, state: Path, forge,
):
    """THE CALL. The merger is only worth anything if the loop ticks it: an item in review
    on one tick and gone on the next is merged, and the receipt lands on the item."""
    gh = forge(_Forge(draft=True))
    items = {"SA-417": _done_item("SA-417", status="review")}
    calls: list = []
    workspace = tmp_path / "ws"
    planner, supervisor = _server(workspace, items=items, calls=calls)

    result = _run(git_repo, scripts, state, workspace, planner, supervisor, merge=True)

    assert result.reason == "idle", result.detail
    assert "pr merge" in [" ".join(c[1:3]) for c in gh.calls]
    assert result.wave.merged["SA-417"].ok
    receipts = [a for t, a in calls if t == "update_item" and a.get("id") == "SA-417"]
    assert receipts and receipts[0]["evidence"][0]["commit"] == MERGE_OID


def test_until_without_merge_never_calls_gh(
    git_repo: Path, tmp_path: Path, scripts, state: Path, forge,
):
    """DEFAULT OFF. Same wave, same signed-off item, no flag: the forge is never asked and
    nothing is written. Sabotage: default `merge=True` in `run` — this fails."""
    gh = forge(_Forge(draft=True))
    items = {"SA-417": _done_item("SA-417", status="review")}
    calls: list = []
    workspace = tmp_path / "ws"
    planner, supervisor = _server(workspace, items=items, calls=calls)

    result = _run(git_repo, scripts, state, workspace, planner, supervisor)

    assert result.reason == "idle", result.detail
    assert gh.calls == [], "asked gh without --merge"
    assert result.wave.merged == {}
    assert not [a for t, a in calls if t == "update_item"]


# ---- the criterion: a merge lifts the GRPH-798 hold on the next tick ---------------------------

def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          check=True).stdout.strip()


def _clone_with_remote(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A bare `origin`, a clone the wave runs in, and a second clone that plays the forge."""
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    seed = tmp_path / "seed"
    _git(tmp_path, "init", "-q", "-b", "main", str(seed))
    _git(seed, "config", "user.email", "t@t.t")
    _git(seed, "config", "user.name", "t")
    (seed / "README.md").write_text("x\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-qm", "first")
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "-q", "origin", "main")
    repo = tmp_path / "repo"
    _git(tmp_path, "clone", "-q", str(origin), str(repo))
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "t")
    return origin, repo, seed


def test_a_merge_lifts_the_dependency_hold_on_the_next_tick(
    tmp_path: Path, scripts, state: Path, monkeypatch,
):
    """SA-420 depends on SA-417, which is done with its commit only on `gb/dep`. The wave
    HOLDS SA-420 (GRPH-798). Under `--merge` the hold names SA-417 as the merge to finish;
    the forge squashes it onto `main`; the loop re-fetches the base and offers SA-420 on the
    next tick — without a restart, and without anybody clicking anything.

    The squash is real: the forge stand-in commits to `origin/main` from a second clone at
    the moment `gh pr merge` is called, so the merge commit only reaches this clone through
    the re-fetch. Sabotage: drop `refresh_ref` from `Merger.tick` and SA-420 stays held."""
    origin, repo, forge_clone = _clone_with_remote(tmp_path)
    # The dependency's work, on a branch the base does not have.
    _git(repo, "checkout", "-q", "-b", "gb/dep")
    (repo / "dep.txt").write_text("work\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "SA-417: the dependency")
    dep_commit = _git(repo, "rev-parse", "HEAD")
    _git(repo, "push", "-q", "origin", "gb/dep")
    _git(repo, "checkout", "-q", "main")

    landed: dict[str, str] = {}

    class _RealForge(_Forge):
        def __call__(self, argv, **kw):
            if " ".join(argv[1:3]) == "pr merge" and not landed:
                # The squash, on the remote, from somewhere this clone is not.
                _git(forge_clone, "checkout", "-q", "main")
                (forge_clone / "dep.txt").write_text("work\n")
                _git(forge_clone, "add", "-A")
                _git(forge_clone, "commit", "-qm", "SA-417: the dependency (#9)")
                landed["oid"] = _git(forge_clone, "rev-parse", "HEAD")
                _git(forge_clone, "push", "-q", "origin", "main")
                self.pr["state"] = "MERGED"
                self.pr["mergeCommit"] = {"oid": landed["oid"]}
                self.calls.append(argv)
                return subprocess.CompletedProcess(argv, 0, "", "")
            return super().__call__(argv, **kw)

    gh = _RealForge(draft=True, head=dep_commit)
    monkeypatch.setattr(propose_mod, "find", lambda: "/usr/bin/gh")
    monkeypatch.setattr(propose_mod.subprocess, "run", _only_gh(gh))
    # The reap ALSO fetches the base (GRPH-786's staleness check), and with a child exiting
    # between ticks that fetch was bringing the merge commit in — so the first version of
    # this walk passed with the merger's own re-fetch deleted. Neutralised here, so the only
    # fetch left is the one this criterion is about.
    monkeypatch.setattr(sup, "_note_staleness", lambda wave, tree: None)

    items = {
        "SA-417": _done_item("SA-417", reviewed=dep_commit, green=dep_commit, branch="gb/dep"),
        "SA-420": {"id": "SA-420", "status": "next", "branch": "", "pr": None, "evidence": []},
    }
    delegations: list = []
    calls: list = []
    workspace = tmp_path / "ws"
    planner, supervisor = _server(
        workspace, items=items, review_first=False, calls=calls, delegations=delegations,
        related={"SA-420": ["SA-417"]}, clusters=[["SA-420"]],
    )
    result = run(
        repo, _factory(scripts, "works_then_exits"),
        planner, supervisor, api_key=KEY, server="http://gb.invalid", adapter="fake",
        state=state, workspace=workspace, poll=0, sleep=lambda _: None, empty_ticks=3,
        limits=sup.Limits(max_workers=1, max_children=3), merge=True,
    )

    assert landed, "the forge was never asked to merge SA-417"
    assert result.wave.merged["SA-417"].ok
    assert result.wave.merged["SA-417"].commit == landed["oid"]
    assert [d["id"] for d in delegations] == ["SA-420"], (
        f"SA-420 was never offered after the merge; reason={result.reason} {result.detail}")
    # And the receipt is what made it visible: the merge commit is on SA-417 now.
    assert any(e.get("commit") == landed["oid"] for e in items["SA-417"]["evidence"])
    assert _git(repo, "merge-base", "--is-ancestor", landed["oid"], "origin/main") == ""


def test_up_merge_finishes_the_merge_after_the_reap(
    git_repo: Path, tmp_path: Path, scripts, state: Path, forge, monkeypatch,
):
    """`up` runs one wave and ends when its children do. The item this wave's child built
    is signed off by somebody else while — or just after — the child exits, and the merge
    still has to happen inside this run rather than be left to the next one.

    The in-loop tick is SUPPRESSED here — `_wait_out` runs as itself minus the merger — so
    the post-reap tick is the only one that can do this, and a fast child living through
    one loop tick cannot make the test pass for the wrong reason. The in-loop tick is
    pinned separately by `test_up_ticks_the_merger_while_waiting`. Sabotage: guard the
    post-reap tick with `if False` — this fails."""
    from tests.test_supervisor import _seats

    real_wait = sup._wait_out
    monkeypatch.setattr(sup, "_wait_out", lambda *a, merger=None, **k: real_wait(*a, **k))
    gh = forge(_Forge(draft=True))
    items = {"SA-417": _done_item("SA-417")}
    calls: list = []
    workspace = tmp_path / "ws"
    _planner, _supervisor = _server(workspace, items=items, review_first=False, calls=calls)
    client = Graphban("http://gb.invalid", KEY, transport=_planner.transport,
                      allowed=ALLOWED_TOOLS | {"search_items", "get_item_details",
                                               "related_work", "update_item"})
    merger = Merger(git_repo, client, enabled=True)
    merger.note_review([{"id": "SA-417", "branch": "gb/w-1"}])

    wave = sup.up(git_repo, _seats(1), _factory(scripts, "works_then_exits"), client,
                  state=state, workspace=workspace, merger=merger)

    assert wave.ok, wave.failures
    assert wave.merged["SA-417"].ok
    assert "pr merge" in [" ".join(c[1:3]) for c in gh.calls]
    assert [a for t, a in calls if t == "update_item" and a.get("id") == "SA-417"]


def test_up_ticks_the_merger_while_waiting():
    """THE CALL, pinned at source the way this repo pins `allowed=SPAWN_READS`: a merger
    handed to `up` and ticked only after the reap would merge nothing for the whole life of
    a long wave, and a timing-dependent test cannot tell that from a fast child."""
    import inspect

    src = inspect.getsource(sup._wait_out)
    assert "merger.tick(wave)" in src
    body = src.split("while any(child.running for child in children):", 1)[1]
    assert "merger.tick(wave)" in body.split("sleep(poll)", 1)[0], "ticked outside the loop"


# ---- the flags -------------------------------------------------------------------------------------

def test_the_flag_is_off_by_default_on_both_commands():
    """Sabotage: `default=True` on either parser — this fails."""
    from gbfleet.cli import build_parser

    parser = build_parser()
    until = parser.parse_args(["until", "--server", "http://gb.invalid", "--adapter", "claude"])
    up = parser.parse_args(["up", "--server", "http://gb.invalid", "--seats-file", "s",
                            "--adapter", "claude"])
    assert until.merge is False and up.merge is False
    assert parser.parse_args(["until", "--server", "x", "--adapter", "claude", "--merge"]).merge
    assert parser.parse_args(["up", "--server", "x", "--seats-file", "s", "--adapter", "claude",
                              "--merge"]).merge


def test_main_until_passes_the_flag_through(monkeypatch, git_repo: Path):
    """THE CALL: parse_args is not the call. A flag that parsed and was never passed to
    `run` would be the feature's own sabotage case."""
    from gbfleet import cli
    from gbfleet.until import Report

    seen: dict = {}

    class FakeGB:
        def __init__(self, base_url, api_key, allowed=None, **_kw):
            self.allowed = allowed if allowed is not None else ALLOWED_TOOLS

        def close(self):
            pass

    def fake_run(repo, factory, planner, supervisor, **kw):
        seen["merge"] = kw.get("merge")
        seen["planner"] = planner.allowed
        return Report(ok=True, reason="idle", exit=0)

    monkeypatch.setenv("GBFLEET_API_KEY", KEY)
    monkeypatch.setattr(cli, "Graphban", FakeGB)
    monkeypatch.setattr(cli, "run_until", fake_run)
    monkeypatch.setattr(cli, "make_adapter_factory", lambda *a, **k: object())

    base = ["until", "--repo", str(git_repo), "--server", "http://gb.invalid",
            "--adapter", "gbagent"]
    cli.main(base)
    assert seen["merge"] is False
    cli.main(base + ["--merge"])
    assert seen["merge"] is True
    # The planner holds what the merger needs; the supervisor set is untouched.
    assert {"related_work", "update_item", "get_item_details"} <= seen["planner"]
    assert ALLOWED_TOOLS == frozenset({"fleet_status", "propose_allocation"})


def test_main_up_builds_a_merger_only_under_the_flag(monkeypatch, git_repo: Path, tmp_path):
    from gbfleet import cli
    from gbfleet.cli import MERGE_TOOLS, SPAWN_READS

    seen: dict = {}

    class FakeGB:
        def __init__(self, base_url, api_key, allowed=None, **_kw):
            self.allowed = allowed

        def close(self):
            pass

    def fake_up(repo, seats, factory, client, **kw):
        seen["merger"] = kw.get("merger")
        seen["allowed"] = client.allowed
        return Wave(reason="idle")

    seats = tmp_path / "seats.txt"
    seats.write_text("WORKER-AAAAAA\n")
    monkeypatch.setenv("GBFLEET_API_KEY", KEY)
    monkeypatch.setattr(cli, "Graphban", FakeGB)
    monkeypatch.setattr(cli, "up", fake_up)
    monkeypatch.setattr(cli, "make_adapter_factory", lambda *a, **k: object())

    base = ["up", "--repo", str(git_repo), "--server", "http://gb.invalid",
            "--seats-file", str(seats), "--adapter", "gbagent"]
    cli.main(base)
    assert seen["merger"] is None and seen["allowed"] == SPAWN_READS
    cli.main(base + ["--merge"])
    assert isinstance(seen["merger"], Merger) and seen["merger"].enabled
    assert seen["allowed"] == SPAWN_READS | MERGE_TOOLS
    assert "update_item" not in ALLOWED_TOOLS


def test_the_report_names_each_outcome(capsys):
    from gbfleet.cli import report
    from gbfleet.propose import Merged

    wave = Wave()
    wave.merged = {
        "SA-1": Merged(item="SA-1", ok=True, commit=MERGE_OID, url="https://x/pull/1"),
        "SA-2": Merged(item="SA-2", pending=True, reason="auto-merge enabled"),
        "SA-3": Merged(item="SA-3", skipped=True, reason="branch protection blocks it"),
        "SA-4": Merged(item="SA-4", reason="PR head moved", checked=("open", "same-repo")),
    }
    report(wave)
    out = capsys.readouterr().out
    assert f"MERGED SA-1: {MERGE_OID[:12]} https://x/pull/1" in out
    assert "MERGE PENDING SA-2: auto-merge enabled" in out
    assert "MERGE SKIPPED SA-3: branch protection" in out
    assert "MERGE HELD SA-4: PR head moved (checked: open, same-repo)" in out
