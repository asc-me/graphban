"""`until --merge` leaves an item `done` only when the merge commit is on the default branch.

GRPH-1348. Sign-off moves an item to `done` before any merge. A skipped merge, or a merge
commit that is not an ancestor of the default branch, used to leave it there: GRPH-1326
was marked done while its PR was an open conflicting draft, and GBC-52 was marked done
when the merge skipped "no PR for" its branch.

Sabotage: delete the `verdict is False` branch in `Merger._require_on_default`. The
ancestor test below stays `done` and records a merge receipt for a commit that is not
on `main`.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from gbfleet import observe
from gbfleet import propose as propose_mod
from gbfleet.client import ObservationRefused, ToolFailed
from gbfleet.service import Installed
from gbfleet.supervisor import Merger, Wave

from tests.test_merge_after_sign_off import REPO, _Ledger, _done_item, _only_gh
from tests.test_propose import REVIEWED, _Forge


def _forge(monkeypatch, fake):
    monkeypatch.setattr(propose_mod, "find", lambda: "/usr/bin/gh")
    monkeypatch.setattr(propose_mod.subprocess, "run", _only_gh(fake))
    return fake


DEAD = "https://github.com/o/r/pull/1"
LIVE = "https://github.com/o/r/pull/9"
_PR_RECEIPT = "draft PR opened by gbfleet"


class _Strict(_Ledger):
    """Applies the server's bounce rule and status writes. `_Ledger` accepts every bounce
    and ignores `status`, which hides the no-PR case the server refuses."""

    def call(self, tool, **kw):
        if tool == "bounce":
            self.calls.append((tool, kw))
            item = self.items[kw["id"]]
            details = [str(e.get("detail") or "") for e in item.get("evidence") or []]
            proposed = any(d.startswith(_PR_RECEIPT) for d in details)
            merged = any(d.startswith("merged by gbfleet") for d in details)
            if not (item["status"] == "done" and proposed and not merged):
                raise ToolFailed(
                    "bounce", "conflict",
                    f"{kw['id']} is done and gbfleet opened no PR for it, so no merge is pending")
            item["status"] = "next"
            return {"id": kw["id"], "status": "next"}
        if tool == "update_item":
            item = self.items[kw["id"]]
            if kw.get("status"):
                item["status"] = kw["status"]
            if "blocker" in kw:
                item["blocker"] = kw["blocker"]
        return super().call(tool, **kw)


def _with_pr(item_id: str, **kw) -> dict:
    row = _done_item(item_id, **kw)
    row["evidence"].append({
        "kind": "url", "url": LIVE,
        "detail": f"{_PR_RECEIPT} for {row['branch']}",
    })
    return row


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True)
    return done.stdout.strip()


def _repo_with_side_commit(tmp_path: Path) -> tuple[Path, str, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "f.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "f.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
    main = _git(repo, "rev-parse", "HEAD")
    subprocess.run(["git", "checkout", "-q", "-b", "side"], cwd=repo, check=True)
    (repo / "f.txt").write_text("side\n", encoding="utf-8")
    subprocess.run(["git", "add", "f.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "side"], cwd=repo, check=True)
    side = _git(repo, "rev-parse", "HEAD")
    subprocess.run(["git", "checkout", "-q", "main"], cwd=repo, check=True)
    return repo, main, side


class _ShaForge:
    """`gh` that reports one PR already merged at a chosen commit."""

    def __init__(self, merge_sha: str, *, head: str = REVIEWED):
        self.pr = {
            "number": 9, "url": LIVE, "state": "MERGED", "isDraft": False,
            "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN",
            "headRefOid": head, "headRefName": "gb/w-1", "baseRefName": "main",
            "isCrossRepository": False, "mergeCommit": {"oid": merge_sha},
        }
        self.calls: list[list[str]] = []

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        verb = " ".join(argv[1:3])
        if verb == "pr view":
            return subprocess.CompletedProcess(argv, 0, json.dumps(self.pr), "")
        if verb == "pr list":
            return subprocess.CompletedProcess(argv, 0, "[]", "")
        raise AssertionError(argv)


class _DeadUrl(_Forge):
    """`pr view` of one recorded URL fails. The branch's live PR still resolves."""

    def __init__(self):
        super().__init__(draft=False, listed=[{"url": LIVE, "state": "OPEN", "number": 9}])

    def __call__(self, argv, **kw):
        if " ".join(argv[1:3]) == "pr view" and len(argv) > 3 and argv[3] == DEAD:
            self.calls.append(argv)
            return subprocess.CompletedProcess(argv, 1, "", "no pull request found")
        return super().__call__(argv, **kw)


def _merged_receipts(item: dict) -> list[dict]:
    return [e for e in item.get("evidence") or []
            if str(e.get("detail") or "").startswith("merged by gbfleet")]


def test_a_signed_off_item_with_no_pr_does_not_stay_done(monkeypatch):
    """The acceptance case. No PR, and nothing to propose: the item leaves `done` and the
    log names it. Bounce is refused — gbfleet opened no PR — so the item is blocked."""
    _forge(monkeypatch, _Forge(draft=True, has_pr=False))
    events = []
    observe_emit = observe.emit
    observe.emit = lambda event, **kw: events.append((event, kw))
    try:
        ledger = _Strict({"SA-417": _done_item("SA-417")})
        merger = Merger(REPO, ledger, enabled=True, base="main", agent_id="GRPH-PLANNER")
        merger.tick(Wave())
    finally:
        observe.emit = observe_emit

    item = ledger.items["SA-417"]
    assert item["status"] == "blocked", item["status"]
    assert "done without a PR" in (item.get("blocker") or "") or "no PR for" in (item.get("blocker") or "")
    assert "SA-417" not in merger.watching
    assert _merged_receipts(item) == []
    left = [kw for kind, kw in events if kind == "merge_left_done"]
    assert left and left[0]["item"] == "SA-417" and "SA-417" in left[0]["detail"]
    assert "no PR" in left[0]["detail"] or "done without a PR" in left[0]["detail"]


def test_a_merge_commit_off_the_default_branch_does_not_stay_done(monkeypatch, tmp_path):
    """Sabotage: delete the `verdict is False` branch in `Merger._require_on_default`.
    This stays `done` and writes a `merged by gbfleet` receipt for `side`."""
    repo, _main, side = _repo_with_side_commit(tmp_path)
    _forge(monkeypatch, _ShaForge(side))
    events = []
    real = observe.emit
    observe.emit = lambda event, **kw: events.append((event, kw))
    try:
        ledger = _Strict({"SA-417": _with_pr("SA-417")})
        merger = Merger(repo, ledger, enabled=True, base="main", agent_id="GRPH-PLANNER")
        merger.tick(Wave())
    finally:
        observe.emit = real

    item = ledger.items["SA-417"]
    assert item["status"] == "next", item["status"]
    assert _merged_receipts(item) == []
    assert "SA-417" not in merger.watching
    left = [kw for kind, kw in events if kind == "merge_left_done"]
    assert left and "not an ancestor of main" in left[0]["detail"]
    assert "SA-417" in left[0]["detail"]


def test_a_merge_commit_on_the_default_branch_stays_done(monkeypatch, tmp_path):
    repo, main, _side = _repo_with_side_commit(tmp_path)
    _forge(monkeypatch, _ShaForge(main))
    ledger = _Strict({"SA-417": _with_pr("SA-417")})
    merger = Merger(repo, ledger, enabled=True, base="main", agent_id="GRPH-PLANNER")

    assert merger.tick(Wave()) is True

    item = ledger.items["SA-417"]
    assert item["status"] == "done"
    assert _merged_receipts(item), item["evidence"]
    assert ledger.wrote("bounce") == []


def test_a_dead_recorded_url_is_found_by_branch_name(monkeypatch):
    """The recorded URL 404s. The branch still has an open PR. The merge step uses it
    instead of giving up with `no PR`."""
    gh = _forge(monkeypatch, _DeadUrl())
    row = _done_item("SA-417")
    row["pr"] = DEAD
    ledger = _Strict({"SA-417": row})
    merger = Merger(REPO, ledger, enabled=True, agent_id="GRPH-PLANNER")

    merger.tick(Wave())

    wave_item = ledger.items["SA-417"]
    assert wave_item["status"] == "done"
    assert _merged_receipts(wave_item), "gave up instead of merging the branch's PR"
    assert "pr list" in [" ".join(c[1:3]) for c in gh.calls]
    assert "pr merge" in [" ".join(c[1:3]) for c in gh.calls]
    assert not any("no PR" in (e.get("detail") or "") for e in wave_item["evidence"]
                   if e.get("kind") == "note")


def test_an_unrecorded_pr_is_found_by_branch_name(monkeypatch):
    """No `pr` field and no receipt. `gh pr view <branch>` misses; `pr list` has it."""
    url = LIVE
    gh = _forge(monkeypatch, _Forge(draft=False, has_pr=False, url=url,
                                   listed=[{"url": url, "state": "OPEN", "number": 9}]))
    ledger = _Strict({"SA-417": _done_item("SA-417")})
    merger = Merger(REPO, ledger, enabled=True)
    wave = Wave()

    merger.tick(wave)

    assert wave.merged["SA-417"].ok, wave.merged["SA-417"].reason
    assert "no PR" not in wave.merged["SA-417"].reason
    assert "pr merge" in [" ".join(c[1:3]) for c in gh.calls]


def test_a_draft_that_cannot_be_marked_ready_leaves_done(monkeypatch):
    _forge(monkeypatch, _Forge(draft=True, ready_ok=False))
    events = []
    real = observe.emit
    observe.emit = lambda event, **kw: events.append((event, kw))
    try:
        ledger = _Strict({"SA-417": _with_pr("SA-417")})
        merger = Merger(REPO, ledger, enabled=True, agent_id="GRPH-PLANNER")
        merger.tick(Wave())
    finally:
        observe.emit = real

    item = ledger.items["SA-417"]
    assert item["status"] == "next"
    assert _merged_receipts(item) == []
    left = [kw for kind, kw in events if kind == "merge_left_done"]
    assert left and "gh pr ready failed" in left[0]["detail"] and "SA-417" in left[0]["detail"]


def test_a_pr_that_is_not_mergeable_leaves_done(monkeypatch):
    """UNKNOWN is "not mergeable yet", not a conflict. A conflict bounces on its own path."""
    _forge(monkeypatch, _Forge(draft=False, mergeable="UNKNOWN", status="UNSTABLE"))
    events = []
    real = observe.emit
    observe.emit = lambda event, **kw: events.append((event, kw))
    try:
        ledger = _Strict({"SA-417": _with_pr("SA-417")})
        merger = Merger(REPO, ledger, enabled=True, agent_id="GRPH-PLANNER")
        merger.tick(Wave())
    finally:
        observe.emit = real

    item = ledger.items["SA-417"]
    assert item["status"] == "next", item
    left = [kw for kind, kw in events if kind == "merge_left_done"]
    assert left and "not mergeable" in left[0]["detail"] and "SA-417" in left[0]["detail"]


def test_observations_role_warning_names_the_route_and_is_silent_on_404():
    from gbfleet.until import observations_role_warning

    class _Key:
        def post_observation(self, *, timeout=10.0, **payload):
            raise ObservationRefused(
                "HTTP 403: this credential is not a supervisor key: recording a cut needs "
                "planner among its roles", status=403)

    text = observations_role_warning(_Key())
    assert "/api/fleet/observations" in text
    assert "planner" in text

    class _Missing:
        def post_observation(self, *, timeout=10.0, **payload):
            raise ObservationRefused("HTTP 404", status=404)

    assert observations_role_warning(_Missing()) == ""

    class _Down:
        def post_observation(self, *, timeout=10.0, **payload):
            raise ObservationRefused("timed out", status=None)

    assert observations_role_warning(_Down()) == ""


def test_dry_run_warns_when_the_key_lacks_the_observations_role(monkeypatch, git_repo, capsys):
    from gbfleet import cli
    from gbfleet import until as until_mod
    from gbfleet.client import Graphban

    def refuse(self, *, timeout=10.0, **payload):
        raise ObservationRefused("HTTP 403: planner role required", status=403)

    def fake_plan(*_a, **_kw):
        return {"would_delegate": [], "clusters_held": 0, "clusters_free": 0,
                "dependency_holds": [], "capped_by_max_workers": False}

    monkeypatch.setenv(cli.API_KEY_ENV, "k")
    monkeypatch.setattr(cli, "make_adapter_factory", lambda *_a, **_k: object())
    monkeypatch.setattr(until_mod, "plan", fake_plan)
    monkeypatch.setattr(Graphban, "post_observation", refuse)

    code = cli.main(["until", "--repo", str(git_repo), "--server", "http://gb.invalid",
                     "--adapter", "claude", "--dry-run"])

    err = capsys.readouterr().err
    assert code == 0
    assert err.startswith("WARNING ")
    assert "/api/fleet/observations" in err and "planner" in err


def test_dry_run_is_silent_when_observations_accept_the_key(monkeypatch, git_repo, capsys):
    from gbfleet import cli
    from gbfleet import until as until_mod
    from gbfleet.client import Graphban

    def missing(self, *, timeout=10.0, **payload):
        raise ObservationRefused("HTTP 404", status=404)

    def fake_plan(*_a, **_kw):
        return {"would_delegate": [], "clusters_held": 0, "clusters_free": 0,
                "dependency_holds": [], "capped_by_max_workers": False}

    monkeypatch.setenv(cli.API_KEY_ENV, "k")
    monkeypatch.setattr(cli, "make_adapter_factory", lambda *_a, **_k: object())
    monkeypatch.setattr(until_mod, "plan", fake_plan)
    monkeypatch.setattr(Graphban, "post_observation", missing)

    code = cli.main(["until", "--repo", str(git_repo), "--server", "http://gb.invalid",
                     "--adapter", "claude", "--dry-run"])

    assert code == 0
    assert "WARNING" not in capsys.readouterr().err


def test_service_install_warns_when_the_key_lacks_the_observations_role(
        monkeypatch, git_repo, capsys):
    from gbfleet import cli
    from gbfleet import service as service_mod
    from gbfleet.client import Graphban

    def refuse(self, *, timeout=10.0, **payload):
        raise ObservationRefused("HTTP 403: planner role required", status=403)

    def fake_install(plan, key):
        return Installed(plan=plan, running=True)

    monkeypatch.setenv(cli.API_KEY_ENV, "k")
    monkeypatch.setattr(Graphban, "post_observation", refuse)
    monkeypatch.setattr(service_mod, "install", fake_install)

    code = cli.main([
        "service", "install", "--",
        "--repo", str(git_repo), "--server", "http://gb.invalid",
        "--project", "p", "--adapter", "claude",
    ])

    err = capsys.readouterr().err
    assert code == 0, err
    assert "WARNING" in err and "/api/fleet/observations" in err and "planner" in err


def test_service_install_dry_run_warns_the_same_way(monkeypatch, git_repo, capsys):
    from gbfleet import cli
    from gbfleet.client import Graphban

    def refuse(self, *, timeout=10.0, **payload):
        raise ObservationRefused("HTTP 403: planner role required", status=403)

    monkeypatch.setenv(cli.API_KEY_ENV, "k")
    monkeypatch.setattr(Graphban, "post_observation", refuse)

    code = cli.main([
        "service", "install", "--dry-run", "--",
        "--repo", str(git_repo), "--server", "http://gb.invalid",
        "--project", "p", "--adapter", "claude",
    ])

    err = capsys.readouterr().err
    assert code == 0, err
    assert "WARNING" in err and "/api/fleet/observations" in err and "planner" in err
