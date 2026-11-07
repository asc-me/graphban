"""The optional release rule `release.requires_scans_clean` (GRPH-1114).

Off when the file is absent or false, and says so. This repo's checked-in file is on
(GRPH-1119). When on, `publish` runs the S3 scanner on the exact commit it is
about to pack — every component, not a diff — and refuses before pack and before
`gh release create` (which is what makes the tag) unless every required tool is clean.

The refusal tests drive the REAL `scan_release_tree` (detached worktree + `scan_ci`) with fake
tools on a PATH that holds nothing else, so a never-run tool is genuinely absent rather than a
stubbed verdict, and treating it as clean would pass the cut.
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import stat
import subprocess
import sys
import textwrap

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))
import graphban_release as rel  # noqa: E402
import scan_ci  # noqa: E402

VER = "2026.10.3"
RULE = rel.RULE_SCANS_CLEAN

TREE = {
    "backend/app/version.py": f'__version__ = "{VER}"\n',
    "backend/pyproject.toml": f'[project]\nname = "graphban-api"\nversion = "{VER}"\n',
    "backend/uv.lock": "fastapi==0.1\n",
    "web/package.json": f'{{\n  "name": "graphban-web",\n  "version": "{VER}"\n}}\n',
    "web/pnpm-lock.yaml": "lockfileVersion: 9\n",
    "fleet/pyproject.toml": '[project]\nname = "graphban-fleet"\nversion = "0.17.0"\n',
    "fleet/uv.lock": "httpx==1.0\n",
    "cli/pyproject.toml": '[project]\nname = "graphban-cli"\nversion = "0.8.0"\n',
    "cli/uv.lock": "click==8.0\n",
}

# Each fake obeys $FAKE_<NAME> = clean (default) | findings | empty, and logs its cwd.
_FAKES = {
    "uv": r'''
        out = pathlib.Path(sys.argv[sys.argv.index("-o") + 1])
        out.write_text(pathlib.Path("uv.lock").read_text())
    ''',
    "pip-audit": r'''
        if mode == "empty":
            sys.exit(0)
        vulns = [{"id": "PYSEC-1", "fix_versions": ["9.9"]}] if mode == "findings" else []
        print(json.dumps({"dependencies": [{"name": "dep", "version": "1", "vulns": vulns}]}))
        sys.exit(1 if vulns else 0)
    ''',
    "bandit": r'''
        if mode == "empty":
            sys.exit(0)
        results = ([{"test_id": "B602", "issue_text": "shell=True", "issue_severity": "HIGH",
                     "filename": "./x.py", "line_number": 3}] if mode == "findings" else [])
        print(json.dumps({"results": results, "errors": []}))
        sys.exit(1 if results else 0)
    ''',
    "pnpm": r'''
        if mode == "empty":
            sys.exit(0)
        adv = ({"1": {"module_name": "lodash", "severity": "high", "title": "pollution"}}
               if mode == "findings" else {})
        print(json.dumps({"advisories": adv}))
        sys.exit(1 if adv else 0)
    ''',
}


@pytest.fixture()
def tools(tmp_path, monkeypatch):
    """Fake scanners on a PATH that holds them and git, nothing else — so a real pnpm or
    pip-audit on the host can never stand in for one a test removed."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "tools.log"
    for name, body in _FAKES.items():
        env = "FAKE_" + name.upper().replace("-", "_")
        script = bindir / name
        script.write_text(
            f"#!{sys.executable}\n"
            "import json, os, pathlib, sys\n"
            f"mode = os.environ.get({env!r}, 'clean')\n"
            "with open(os.environ['FAKE_LOG'], 'a') as fh:\n"
            "    fh.write(os.path.basename(sys.argv[0]) + ' ' + os.getcwd() + '\\n')\n"
            + textwrap.dedent(body))
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
    git_dir = pathlib.Path(shutil.which("git")).parent
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{git_dir}")
    monkeypatch.setenv("FAKE_LOG", str(log))
    return bindir


def _git(repo: pathlib.Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


def make_repo(tmp_path: pathlib.Path, rules: dict | str | None = None) -> pathlib.Path:
    src = tmp_path / "src"
    files = dict(TREE)
    if rules is not None:
        files[rel.RULES_FILE.as_posix()] = (rules if isinstance(rules, str)
                                            else json.dumps(rules))
    for path, text in files.items():
        p = src / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    _git(src.parent, "init", "-q", "-b", "main", str(src))
    _git(src, "config", "user.email", "rel@test")
    _git(src, "config", "user.name", "rel")
    _git(src, "config", "commit.gpgsign", "false")
    _git(src, "add", "-A")
    _git(src, "commit", "-q", "-m", f"Stamp product version {VER}")
    _git(src, "remote", "add", "origin", str(origin))
    _git(src, "push", "-q", "-u", "origin", "main")
    return src


class FakeGH:
    def __init__(self):
        self.calls: list[list[str]] = []
        self.releases: dict[str, dict] = {}

    def __call__(self, args):
        self.calls.append(list(args))
        if args[:2] == ["release", "view"]:
            body = self.releases.get(args[2])
            if body is None:
                return subprocess.CompletedProcess(args, 1, "", "not found")
            return subprocess.CompletedProcess(args, 0, json.dumps(body), "")
        if args[:2] == ["release", "create"]:
            self.releases[args[2]] = {
                "tagName": args[2], "isDraft": False, "isPrerelease": False,
                "assets": [{"name": pathlib.Path(args[3]).name}]}
            return subprocess.CompletedProcess(args, 0, "", "")
        return subprocess.CompletedProcess(args, 1, "", "unexpected")

    def created(self) -> bool:
        return any(c[:2] == ["release", "create"] for c in self.calls)


def publish(src, tmp_path, *, dry_run=False, **kw):
    packed: list[str] = []

    def pack(ref, dest, api_only=False):
        packed.append(ref)
        dest.mkdir(parents=True, exist_ok=True)
        (dest / rel.asset_name(VER)).write_bytes(b"tarball")
        return 0

    gh = FakeGH()
    rc = rel.cmd_publish(src, None, ref="origin/main", out=tmp_path / "dist", fetch=False,
                         api_only=True, dry_run=dry_run, pack=pack, gh_run=gh, **kw)
    return rc, packed, gh


def _never(*a, **k):
    raise AssertionError("the rule is off — publish must not scan")


# ---- off (the default) -----------------------------------------------------------------

@pytest.mark.parametrize("rules", [None, {RULE: False}], ids=["no-file", "false"])
def test_off_publish_proceeds_and_says_the_rule_is_off(tmp_path, capsys, rules):
    src = make_repo(tmp_path, rules)
    rc, packed, gh = publish(src, tmp_path, scan=_never)
    assert rc == 0
    assert packed and gh.created()
    assert f"rule {RULE}: off" in capsys.readouterr().out


def test_off_dry_run_says_the_rule_is_off_and_packs_nothing(tmp_path, capsys):
    src = make_repo(tmp_path)
    rc, packed, gh = publish(src, tmp_path, dry_run=True, scan=_never)
    assert rc == 0
    assert packed == [] and not gh.created()
    out = capsys.readouterr().out
    assert f"rule {RULE}: off" in out
    assert "would pack" in out


def test_this_repo_ships_the_rule_on():
    """GRPH-1119 turned the checked-in file on. Absent file is still the code default, off."""
    data = json.loads((REPO / rel.RULES_FILE).read_text(encoding="utf-8"))
    assert data == {RULE: True}
    assert rel.RULE_DEFAULTS[RULE] is False


@pytest.mark.parametrize("text, why", [
    ("{not json", "not JSON"),
    ('{"release.requires_scans_clean": "yes"}', "must be true or false"),
    ('{"release.requires_scan_clean": true}', "unknown rule"),
    ("[true]", "not an object"),
])
def test_an_unreadable_rules_file_refuses_rather_than_reading_as_off(tmp_path, capsys,
                                                                     text, why):
    src = make_repo(tmp_path, text)
    rc, packed, gh = publish(src, tmp_path, scan=_never)
    assert rc == 1
    assert packed == [] and not gh.created()
    assert why in capsys.readouterr().err


def test_the_rule_is_read_at_the_commit_being_cut_not_cwd(tmp_path, capsys):
    src = make_repo(tmp_path, {RULE: True})
    (src / rel.RULES_FILE).write_text(json.dumps({RULE: False}), encoding="utf-8")
    seen = []
    rc, _, _ = publish(src, tmp_path, scan=lambda repo, sha: seen.append(sha) or (True, "ok"))
    assert rc == 0
    assert seen == [_git(src, "rev-parse", "origin/main")]
    assert f"rule {RULE}: on" in capsys.readouterr().out


# ---- on: THE CALL, through the real scanner ---------------------------------------------

def test_THE_CALL_on_and_clean_publishes_after_scanning_every_component(tmp_path, tools,
                                                                        capsys):
    src = make_repo(tmp_path, {RULE: True})
    sha = _git(src, "rev-parse", "origin/main")
    rc, packed, gh = publish(src, tmp_path)
    out = capsys.readouterr().out
    assert rc == 0, out
    assert packed == [sha] and gh.created()
    assert f"scans_clean passed at {sha[:12]}" in out
    for prefix in scan_ci.COMPONENTS:
        for tool in scan_ci.TOOLS[prefix]:
            assert f"{tool} {prefix} clean" in out
    # Scanned a detached worktree of the commit, not the checkout publish ran from.
    cwds = (tmp_path / "tools.log").read_text().splitlines()
    assert cwds and not any(str(src) in line for line in cwds)
    assert _git(src, "worktree", "list").count("\n") == 0, "the scan worktree was left behind"


@pytest.mark.parametrize("env, state, tool, prefix", [
    ({"FAKE_BANDIT": "findings"}, "failed check", "bandit", "backend/"),
    ({"FAKE_PNPM": "findings"}, "failed check", "npm-audit", "web/"),
    ({"FAKE_PIP_AUDIT": "empty"}, "never-run", "pip-audit", "backend/"),
    ({"remove": "pnpm"}, "never-run", "npm-audit", "web/"),
], ids=["bandit-findings", "npm-findings", "pip-audit-no-output", "pnpm-not-installed"])
def test_THE_CALL_on_and_failing_refuses_before_pack_and_release(tmp_path, tools, capsys,
                                                                 monkeypatch, env, state,
                                                                 tool, prefix):
    for k, v in env.items():
        if k == "remove":
            (tools / v).unlink()
        else:
            monkeypatch.setenv(k, v)
    src = make_repo(tmp_path, {RULE: True})
    rc, packed, gh = publish(src, tmp_path)
    cap = capsys.readouterr()
    assert rc == 1
    assert packed == [], "packed a cut the rule refused"
    assert not gh.created(), "created the GitHub Release (and so the tag) anyway"
    assert f"{state} {tool} {prefix}" in cap.out
    assert "does not pass scans_clean" in cap.err
    assert "not tagging" in cap.err


def test_on_and_failing_dry_run_says_publish_would_refuse(tmp_path, tools, capsys,
                                                          monkeypatch):
    monkeypatch.setenv("FAKE_BANDIT", "findings")
    src = make_repo(tmp_path, {RULE: True})
    rc, packed, gh = publish(src, tmp_path, dry_run=True)
    assert rc == 1
    assert packed == [] and not gh.created()
    assert "publish would refuse" in capsys.readouterr().err


# ---- the scanner half -------------------------------------------------------------------

def test_evaluate_tree_requires_every_component_whatever_the_commit_touched(tmp_path, tools):
    """A release ships every component; there is no diff to narrow it."""
    src = make_repo(tmp_path)
    sha = _git(src, "rev-parse", "HEAD")
    v = scan_ci.evaluate_tree(src, head=sha, timeout=20)
    assert v.passed, v.detail
    assert {(r.tool, r.prefix) for r in v.results} == {
        (t, p) for p in scan_ci.COMPONENTS for t in scan_ci.TOOLS[p]}


def test_evaluate_tree_on_a_checkout_that_is_not_the_commit_is_stale(tmp_path, tools):
    src = make_repo(tmp_path)
    old = _git(src, "rev-parse", "HEAD")
    (src / "README.md").write_text("x\n")
    _git(src, "add", "-A")
    _git(src, "commit", "-q", "-m", "later")
    v = scan_ci.evaluate_tree(src, head=old, timeout=20)
    assert not v.passed
    assert {r.state for r in v.results} == {scan_ci.STALE}
    assert "release commit is" in v.detail
    assert (tmp_path / "tools.log").exists() is False, "ran tools on the wrong tree"


def test_a_missing_component_is_never_run_not_clean(tmp_path, tools):
    src = make_repo(tmp_path)
    shutil.rmtree(src / "cli")
    _git(src, "add", "-A")
    _git(src, "commit", "-q", "-m", "drop cli")
    v = scan_ci.evaluate_tree(src, head=_git(src, "rev-parse", "HEAD"), timeout=20)
    assert not v.passed
    assert "never-run pip-audit cli/" in v.detail


def test_runbook_names_the_rule_and_where_it_lives():
    text = (REPO / "docs/release.md").read_text(encoding="utf-8")
    assert RULE in text
    assert rel.RULES_FILE.as_posix() in text
    assert "evaluate_tree" in text


# ---- highlighted notes: fleet / cli releases and features (GRPH-1359) -------------------

def _land(src: pathlib.Path, subject: str, edits: dict[str, str]) -> None:
    for path, text in edits.items():
        p = src / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    _git(src, "add", "-A")
    _git(src, "commit", "-q", "-m", subject)


def history(tmp_path: pathlib.Path) -> pathlib.Path:
    """A tagged cut, then a fleet bump, a cli bump, a feature, a fix and a cli dep-only edit."""
    src = make_repo(tmp_path)
    _git(src, "tag", "-a", VER, "-m", VER)
    _land(src, "Bump graphban-fleet (#1359)", {
        # Title says nothing about the version on purpose: it is read from the diff.
        "fleet/pyproject.toml": '[project]\nname = "graphban-fleet"\nversion = "0.18.12"\n'})
    _land(src, "Ship gban (#1360)", {
        "cli/pyproject.toml": '[project]\nname = "graphban-cli"\nversion = "0.9.0"\n'})
    _land(src, "Attached box fetches its own notes (GRPH-1344) (#1347)", {"a.txt": "a\n"})
    _land(src, "Fix the summary pill (GRPH-1345) (#1348)", {"b.txt": "b\n"})
    _land(src, "cli: pin click (#1361)", {
        "cli/pyproject.toml": ('[project]\nname = "graphban-cli"\nversion = "0.9.0"\n'
                               'dependencies = ["click==8.1"]\n')})
    return src


KINDS = {"GRPH-1344": "feature", "GRPH-1345": "fix"}


def test_THE_CALL_since_lines_are_tagged_and_highlights_lead(tmp_path, capsys):
    src = history(tmp_path)
    asked: list[list[str]] = []

    def kinds(ids):
        asked.append(list(ids))
        return {i: KINDS.get(i, "") for i in ids}

    body, prev, _ = rel.collect_notes(src, "2026.10.4", "HEAD", "abc1234", kinds=kinds)
    assert prev == VER
    assert asked == [["GRPH-1344", "GRPH-1345"]]
    since = body[body.index(f"**Since {VER}**"):].split("\n\n")[0].splitlines()[1:]
    assert since == [
        "- #1359 [fleet 0.18.12] Bump graphban-fleet",
        "- #1360 [cli 0.9.0] Ship gban",
        "- #1347 [feature] Attached box fetches its own notes (GRPH-1344)",
        "- #1348 Fix the summary pill (GRPH-1345)",
        "- #1361 cli: pin click",
    ]
    hl = body[body.index("**Highlights**"):body.index(f"**Since {VER}**")]
    assert hl.strip().splitlines() == [
        "**Highlights**",
        "* fleet 0.18.12 released (#1359)",
        "* cli 0.9.0 released (#1360)",
        "* Feature: #1347 Attached box fetches its own notes (GRPH-1344)",
    ]
    # Highlights are not `- ` lines, so "N changes" on any panel still counts five.
    assert sum(1 for line in body.splitlines() if line.startswith("- ")) == 5
    assert "could not classify" not in capsys.readouterr().err


def test_no_highlights_block_when_nothing_is_a_release_or_feature(tmp_path):
    src = make_repo(tmp_path)
    _git(src, "tag", "-a", VER, "-m", VER)
    _land(src, "Fix the summary pill (GRPH-1345) (#1348)", {"b.txt": "b\n"})
    body, _, _ = rel.collect_notes(src, "2026.10.4", "HEAD", "abc1234",
                                   kinds=lambda ids: {i: "fix" for i in ids})
    assert "**Highlights**" not in body
    assert "- #1348 Fix the summary pill (GRPH-1345)" in body


def test_unreadable_kinds_still_produce_notes_and_the_command_says_why(tmp_path, capsys):
    src = history(tmp_path)

    def down(ids):
        raise rel.KindsUnreadable("http://box:8080 unreachable for GRPH-1344: refused")

    body, _, changes = rel.collect_notes(src, "2026.10.4", "HEAD", "abc1234", kinds=down)
    err = capsys.readouterr().err
    assert "could not classify features" in err
    assert "http://box:8080 unreachable for GRPH-1344: refused" in err
    assert changes is not None and len(changes) == 5
    assert "[feature]" not in body
    # Package releases come from git, not the server, so they are still marked.
    assert "- #1359 [fleet 0.18.12] Bump graphban-fleet" in body
    assert "**Highlights**" in body


def test_the_notes_command_names_a_missing_server_config(tmp_path, capsys, monkeypatch):
    src = history(tmp_path)
    monkeypatch.delenv("GRAPHBAN_URL", raising=False)
    monkeypatch.delenv("GRAPHBAN_API_KEY", raising=False)
    monkeypatch.setenv("GRAPHBAN_HOME", str(tmp_path / "no-home"))
    assert rel.cmd_notes(src, "2026.10.4", ref="HEAD", fetch=False) == 0
    out = capsys.readouterr()
    assert "**Since 2026.10.3**" in out.out
    assert "could not classify features — no Graphban URL" in out.err


def test_server_kinds_reads_item_kind_and_treats_404_as_no_kind(monkeypatch):
    import io
    import urllib.error

    seen: list[tuple[str, str]] = []

    def fake_open(req, timeout=0):
        seen.append((req.full_url, req.get_header("X-api-key")))
        if req.full_url.endswith("GRPH-9"):
            raise urllib.error.HTTPError(req.full_url, 404, "nf", {}, io.BytesIO(b""))
        return io.BytesIO(json.dumps({"id": "GRPH-1", "kind": "feature"}).encode())

    monkeypatch.setattr(rel.urllib.request, "urlopen", fake_open)
    got = rel.server_kinds(["GRPH-1", "GRPH-9"], config=lambda: ("http://box:8080/api", "k"))
    assert got == {"GRPH-1": "feature", "GRPH-9": ""}
    assert seen == [("http://box:8080/api/items/GRPH-1", "k"),
                    ("http://box:8080/api/items/GRPH-9", "k")]

    def refused(req, timeout=0):
        raise urllib.error.HTTPError(req.full_url, 401, "no", {}, io.BytesIO(b""))

    monkeypatch.setattr(rel.urllib.request, "urlopen", refused)
    with pytest.raises(rel.KindsUnreadable, match="401"):
        rel.server_kinds(["GRPH-1"], config=lambda: ("http://box:8080", "k"))
