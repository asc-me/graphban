"""Cutting a named Graphban version is stamp then publish (GRPH-656).

The loop that was reconstructed each time: write three version files, merge,
pack a tarball, attach it to a GitHub Release (not the source zip), then
Settings → Updates → Install. A helper that stamps while publish still packs
cwd, or creates a GitHub Release with no tarball, is that session wearing
script clothes.
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import subprocess
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "scripts"))
import graphban_release as rel  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parents[2]
VER, NEXT = "2026.09.4", "2026.09.5"


def _git(repo: pathlib.Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def make_src(tmp_path: pathlib.Path, *, version: str = VER) -> pathlib.Path:
    src = tmp_path / "src"
    (src / "backend" / "app").mkdir(parents=True)
    (src / "web").mkdir()
    (src / "fleet").mkdir()
    (src / "backend" / "app" / "version.py").write_text(
        f'__version__ = "{version}"\n', encoding="utf-8")
    (src / "backend" / "pyproject.toml").write_text(
        f'[project]\nname = "graphban-api"\nversion = "{version}"\n',
        encoding="utf-8")
    (src / "web" / "package.json").write_text(
        f'{{\n  "name": "graphban-web",\n  "version": "{version}"\n}}\n',
        encoding="utf-8")
    (src / "fleet" / "pyproject.toml").write_text(
        '[project]\nname = "graphban-fleet"\nversion = "0.1.0"\n',
        encoding="utf-8")
    return src


def make_git_src(tmp_path: pathlib.Path, *, version: str = VER) -> pathlib.Path:
    src = make_src(tmp_path, version=version)
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", str(origin)], check=True,
                   capture_output=True)
    _git(src, "init")
    _git(src, "config", "user.email", "rel@test")
    _git(src, "config", "user.name", "rel")
    _git(src, "config", "commit.gpgsign", "false")
    _git(src, "checkout", "-b", "main")
    _git(src, "add", "-A")
    _git(src, "commit", "-m", f"stamp {version}")
    _git(src, "remote", "add", "origin", str(origin))
    _git(src, "push", "-u", "origin", "main")
    return src


class FakeGH:
    def __init__(self, releases: dict | None = None):
        self.releases = dict(releases or {})
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]) -> subprocess.CompletedProcess:
        self.calls.append(list(args))
        argv = list(args)
        if argv[:2] == ["release", "view"]:
            tag = argv[2]
            body = self.releases.get(tag)
            if body is None:
                return subprocess.CompletedProcess(argv, 1, "", "not found")
            return subprocess.CompletedProcess(
                argv, 0, json.dumps(body), "")
        if argv[:2] == ["release", "create"]:
            tag = argv[2]
            files = _create_files(argv)
            self.releases[tag] = {
                "tagName": tag,
                "isDraft": False,
                "isPrerelease": False,
                "assets": [{"name": pathlib.Path(f).name} for f in files],
            }
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[:2] == ["release", "upload"]:
            tag = argv[2]
            name = pathlib.Path(argv[3]).name
            rels = self.releases.setdefault(tag, {
                "tagName": tag, "isDraft": False, "isPrerelease": False,
                "assets": [],
            })
            rels["assets"] = [a for a in rels["assets"] if a["name"] != name]
            rels["assets"].append({"name": name})
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.CompletedProcess(argv, 1, "", "unexpected")


def _create_files(argv: list[str]) -> list[str]:
    """Positional files after `release create TAG`, skipping flag values.

    `--title 2026.09.4` is the same string as TAG; taking every non-flag token
    would record the title as an asset and hide a missing tarball.
    """
    files: list[str] = []
    skip = False
    takes = {"--title", "--target", "--notes"}
    for a in argv[3:]:
        if skip:
            skip = False
            continue
        if a in takes:
            skip = True
            continue
        if a.startswith("-"):
            continue
        files.append(a)
    return files


# ---- next / stamp ----------------------------------------------------------------

def test_next_same_month_increments_n():
    assert rel.next_calver(VER, dt.date(2026, 9, 2), set()) == NEXT


def test_next_new_month_resets_n():
    assert rel.next_calver(VER, dt.date(2026, 10, 1), set()) == "2026.10.1"


def test_next_skips_a_tag_that_already_exists():
    assert rel.next_calver(VER, dt.date(2026, 9, 2), {NEXT}) == "2026.09.6"


def test_next_placeholder_starts_this_month():
    assert rel.next_calver("0.1.0", dt.date(2026, 9, 2), set()) == "2026.09.1"


def test_stamp_writes_three_files_and_not_fleet(tmp_path):
    src = make_src(tmp_path)
    written = rel.stamp(src, NEXT)
    assert [p.as_posix() for p in written] == [
        "backend/app/version.py",
        "backend/pyproject.toml",
        "web/package.json",
    ]
    assert rel.read_stamped(src) == NEXT
    assert f'version = "{NEXT}"' in (src / "backend/pyproject.toml").read_text()
    assert f'"version": "{NEXT}"' in (src / "web/package.json").read_text()
    fleet = (src / "fleet/pyproject.toml").read_text(encoding="utf-8")
    assert 'version = "0.1.0"' in fleet
    assert NEXT not in fleet


def test_stamp_refuses_placeholder(tmp_path):
    src = make_src(tmp_path)
    with pytest.raises(ValueError, match="placeholder"):
        rel.stamp(src, "0.1.0")
    assert rel.read_stamped(src) == VER


def test_stamp_refuses_v_prefix(tmp_path, capsys):
    src = make_src(tmp_path)
    rc = rel.cmd_stamp(src, "v2026.09.5", today=dt.date(2026, 9, 2))
    assert rc == 1
    assert "no v prefix" in capsys.readouterr().err
    assert rel.read_stamped(src) == VER


def test_stamp_refuses_not_after_current(tmp_path):
    src = make_src(tmp_path)
    with pytest.raises(ValueError, match="not after"):
        rel.stamp(src, VER)
    with pytest.raises(ValueError, match="not after"):
        rel.stamp(src, "2026.08.9")


def test_cmd_stamp_prints_publish_next_step(tmp_path, capsys):
    src = make_src(tmp_path)
    assert rel.cmd_stamp(src, None, today=dt.date(2026, 9, 2)) == 0
    out = capsys.readouterr().out
    assert NEXT in out
    assert "python3 scripts/graphban_release.py publish" in out
    assert "Fleet stays 0.1.0" in out


# ---- publish is the CALL ---------------------------------------------------------

def test_THE_CALL_publish_packs_the_ref_not_cwd_and_attaches_the_tarball(tmp_path):
    """Drop `--target sha` or pack HEAD, and a dirty worktree becomes the cut.

    Drop the tarball from `gh release create` and GitHub ships a source zip
    Updates Install cannot apply — that was 2026.09.1.
    """
    src = make_git_src(tmp_path)
    sha = subprocess.run(
        ["git", "-C", str(src), "rev-parse", "origin/main"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    (src / "backend" / "app" / "dirty.py").write_text("uncommitted\n", encoding="utf-8")
    seen: list[tuple[str, pathlib.Path, bool]] = []
    out = tmp_path / "dist"

    def pack(ref, dest, api_only=False):
        seen.append((ref, dest, api_only))
        dest.mkdir(parents=True, exist_ok=True)
        (dest / rel.asset_name(VER)).write_bytes(b"tarball")
        return 0

    gh = FakeGH()
    rc = rel.cmd_publish(
        src, None, ref="origin/main", out=out, fetch=False, api_only=False,
        dry_run=False, pack=pack, gh_run=gh,
    )
    assert rc == 0
    assert seen, "pack was not called"
    assert seen[0][0] == sha
    assert seen[0][0] != "HEAD"
    create = [c for c in gh.calls if c[:2] == ["release", "create"]]
    assert create, f"no gh release create: {gh.calls}"
    argv = create[0]
    assert rel.asset_name(VER) in " ".join(argv)
    assert "--target" in argv
    assert sha in argv
    assert "--latest" in argv
    assert "--draft" not in argv
    assert VER in gh.releases
    assert rel.asset_name(VER) in rel.release_assets(gh.releases[VER])


def test_publish_refuses_when_ref_is_not_stamped_that_version(tmp_path, capsys):
    src = make_git_src(tmp_path, version=VER)
    rc = rel.cmd_publish(
        src, NEXT, ref="origin/main", out=tmp_path / "dist", fetch=False,
        api_only=True, dry_run=True, pack=lambda *a, **k: 1, gh_run=FakeGH(),
    )
    assert rc == 1
    assert "not 2026.09.5" in capsys.readouterr().err


def test_publish_refuses_a_release_that_already_has_the_tarball(tmp_path, capsys):
    src = make_git_src(tmp_path)
    gh = FakeGH({VER: {
        "tagName": VER, "isDraft": False, "isPrerelease": False,
        "assets": [{"name": rel.asset_name(VER)}],
    }})
    packed = []
    rc = rel.cmd_publish(
        src, None, ref="origin/main", out=tmp_path / "dist", fetch=False,
        api_only=True, dry_run=False,
        pack=lambda *a, **k: packed.append(1) or 0,
        gh_run=gh,
    )
    assert rc == 1
    assert packed == []
    assert "already has" in capsys.readouterr().err


def test_publish_uploads_when_the_release_exists_without_the_tarball(tmp_path):
    """2026.09.1 was a tag whose GitHub Release had no packed asset."""
    src = make_git_src(tmp_path)
    gh = FakeGH({VER: {
        "tagName": VER, "isDraft": False, "isPrerelease": False,
        "assets": [{"name": "source.zip"}],
    }})
    out = tmp_path / "dist"

    def pack(ref, dest, api_only=False):
        dest.mkdir(parents=True, exist_ok=True)
        (dest / rel.asset_name(VER)).write_bytes(b"tarball")
        return 0

    rc = rel.cmd_publish(
        src, None, ref="origin/main", out=out, fetch=False, api_only=True,
        dry_run=False, pack=pack, gh_run=gh,
    )
    assert rc == 0
    uploads = [c for c in gh.calls if c[:2] == ["release", "upload"]]
    assert uploads
    assert rel.asset_name(VER) in rel.release_assets(gh.releases[VER])
    creates = [c for c in gh.calls if c[:2] == ["release", "create"]]
    assert creates == []


def test_publish_does_not_create_a_github_release_when_pack_fails(tmp_path, capsys):
    src = make_git_src(tmp_path)
    gh = FakeGH()
    rc = rel.cmd_publish(
        src, None, ref="origin/main", out=tmp_path / "dist", fetch=False,
        api_only=True, dry_run=False,
        pack=lambda *a, **k: 1,
        gh_run=gh,
    )
    assert rc == 1
    assert gh.calls[:1][0][:2] == ["release", "view"]
    assert not any(c[:2] == ["release", "create"] for c in gh.calls)
    assert "source zip is not a substitute" in capsys.readouterr().err


def test_previous_tag_is_the_latest_earlier_calver():
    tags = {"2026.09.5", "2026.09.6", "2026.09.7", "not-a-cut"}
    assert rel.previous_tag("2026.09.8", tags) == "2026.09.7"
    assert rel.previous_tag("2026.09.1", tags) is None
    assert rel.previous_tag("2026.09.8", set()) is None


def test_parse_merge_note_uses_the_pr_title_and_skips_the_stamp():
    got = rel.parse_merge_note(
        "Merge pull request #576 from asc-me/feat/observe-live-page",
        "live: Observe Live page — humans, leases, recorded PRs (GRPH-673)\n",
    )
    assert got == ("576", "live: Observe Live page — humans, leases, recorded PRs (GRPH-673)")
    assert rel.parse_merge_note(
        "Merge pull request #583 from asc-me/chore/stamp-2026.09.8",
        "stamp: 2026.09.8\n",
    ) is None
    assert rel.parse_merge_note(
        "Merge pull request #583 from asc-me/chore/stamp-2026.09.8",
        "Stamp product version 2026.09.8\n",
    ) is None


def test_notes_name_an_empty_interval_and_a_missing_previous():
    first = rel.notes_for(VER, "abc1234", previous=None, changes=[])
    assert "first named cut" in first
    assert "- #" not in first
    empty = rel.notes_for(NEXT, "abc1234", previous=VER, changes=[])
    assert f"No merges on first-parent between `{VER}`" in empty
    unmeasured = rel.notes_for(NEXT, "abc1234", previous=VER, changes=None)
    assert "unmeasured" in unmeasured
    assert "not empty" in unmeasured


def test_notes_list_merges_since_the_previous_cut():
    body = rel.notes_for(
        NEXT, "abc1234", previous=VER,
        changes=[("576", "live: Observe Live page"), ("580", "docs: PRD-33")],
    )
    assert f"**Since {VER}**" in body
    assert "- #576 live: Observe Live page" in body
    assert "- #580 docs: PRD-33" in body
    assert "abc1234" in body
    assert rel.asset_name(NEXT) in body


def _merge_pr(src: pathlib.Path, *, branch: str, number: str, title: str,
              filename: str) -> None:
    _git(src, "checkout", "-b", branch)
    (src / "backend" / "app" / filename).write_text(f"{title}\n", encoding="utf-8")
    _git(src, "add", "-A")
    _git(src, "commit", "-m", title)
    _git(src, "checkout", "main")
    _git(src, "merge", "--no-ff", branch,
         "-m", f"Merge pull request #{number} from asc-me/{branch}",
         "-m", title)


def test_list_merges_is_first_parent_oldest_first_and_drops_the_stamp(tmp_path):
    src = make_git_src(tmp_path, version=VER)
    _git(src, "tag", "-a", VER, "-m", VER)
    _merge_pr(src, branch="feat/live", number="576",
              title="live: Observe Live page", filename="live.py")
    _merge_pr(src, branch="feat/other", number="580",
              title="docs: PRD-33", filename="prd.md")
    _merge_pr(src, branch="chore/stamp-2026.09.5", number="583",
              title="stamp: 2026.09.5", filename="stamp.txt")
    got = rel.list_merges(src, VER, "HEAD")
    assert got == [
        ("576", "live: Observe Live page"),
        ("580", "docs: PRD-33"),
    ]


def test_THE_CALL_publish_notes_are_the_merges_since_the_last_tag(tmp_path):
    """A boilerplate body with no merge list is the old notes_for — sabotage that."""
    src = make_git_src(tmp_path, version=VER)
    _git(src, "tag", "-a", VER, "-m", VER)
    _git(src, "push", "origin", VER)
    _merge_pr(src, branch="feat/live", number="576",
              title="live: Observe Live page", filename="live.py")
    rel.stamp(src, NEXT)
    _git(src, "add", "-A")
    _git(src, "commit", "-m", f"stamp {NEXT}")
    _git(src, "push", "origin", "main")

    out = tmp_path / "dist"

    def pack(ref, dest, api_only=False):
        dest.mkdir(parents=True, exist_ok=True)
        (dest / rel.asset_name(NEXT)).write_bytes(b"tarball")
        return 0

    gh = FakeGH()
    rc = rel.cmd_publish(
        src, NEXT, ref="origin/main", out=out, fetch=False, api_only=True,
        dry_run=False, pack=pack, gh_run=gh,
    )
    assert rc == 0
    create = [c for c in gh.calls if c[:2] == ["release", "create"]]
    assert create
    argv = create[0]
    assert "--notes" in argv
    notes = argv[argv.index("--notes") + 1]
    assert f"**Since {VER}**" in notes
    assert "#576 live: Observe Live page" in notes
    assert "first named cut" not in notes


def test_dry_run_does_not_pack_or_publish(tmp_path):
    src = make_git_src(tmp_path)
    packed = []
    gh = FakeGH()
    rc = rel.cmd_publish(
        src, None, ref="origin/main", out=tmp_path / "dist", fetch=False,
        api_only=True, dry_run=True,
        pack=lambda *a, **k: packed.append(1) or 0,
        gh_run=gh,
    )
    assert rc == 0
    assert packed == []
    assert not any(c[:2] == ["release", "create"] for c in gh.calls)


# ---- the runbook is the CALL for an agent ---------------------------------------

def test_script_is_executable():
    script = REPO / "scripts" / "graphban_release.py"
    assert script.is_file()
    assert script.stat().st_mode & 0o111, "graphban_release.py is not executable"


def test_THE_CALL_ci_runs_backend_when_scripts_change():
    wf = (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "- 'scripts/**'" in wf


def test_runbook_names_the_two_commands_and_the_traps():
    text = (REPO / "docs" / "release.md").read_text(encoding="utf-8")
    assert "python3 scripts/graphban_release.py stamp" in text
    assert "python3 scripts/graphban_release.py publish" in text
    assert "python3 scripts/graphban_release.py notes" in text
    assert "first-parent merges" in text
    assert "source zip" in text.lower()
    assert "Settings" in text and "Install" in text
    assert "fleet" in text.lower()
    assert "0.1.0" in text
    assert "does not apply" in text.lower() or "Do not apply" in text
    assert "graphban_compose_host.py" in text
    assert "linger" in text


def test_agents_map_routes_here():
    agents = (REPO / "AGENTS.md").read_text(encoding="utf-8")
    assert "docs/release.md" in agents


def test_native_install_defers_packing_to_the_script():
    """A hardcoded `gh release upload 2026.09.N` is why every stamp rewrote this file."""
    text = (REPO / "docs" / "native-install.md").read_text(encoding="utf-8")
    assert "graphban_release.py publish" in text
    assert "gh release upload 2026.09." not in text


def test_compose_helper_unit_does_not_mention_the_docker_socket():
    unit = (REPO / "scripts" / "graphban-compose-host.service").read_text(
        encoding="utf-8")
    assert "graphban_compose_host.py" in unit
    assert " listen " in unit or " listen\\" in unit or "listen " in unit
    assert "docker.sock" not in unit
    assert "The Docker socket" not in unit
    assert "%h/graphban-src" in unit
    assert "%h/agentledger" in unit
