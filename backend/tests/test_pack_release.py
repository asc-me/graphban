"""A packed release is the directory native upgrade consumes (P32, GRPH-628).

GitHub's source zip is not a release: no prebuilt `web/dist`, maybe a `.env`, no
`GIT_SHA`. `graphban_host.py upgrade --release` copytree's whatever we hand it, so
the packer is the CALL — a helper that would produce the right tree if invoked,
while `main` packed the dirty checkout, is the deploy.sh incident wearing new clothes.
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import tarfile

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "scripts"))
import graphban_pack as gp  # noqa: E402
import graphban_upgrade as up  # noqa: E402

SHA, VER = "abc1234", "2026.09.1"


def _host_scripts(root: pathlib.Path) -> None:
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    for name in gp.HOST_SCRIPTS:
        (root / "scripts" / name).write_text(f"# {name}\n", encoding="utf-8")


def make_src(tmp_path: pathlib.Path, *, version: str = VER) -> pathlib.Path:
    src = tmp_path / "src"
    (src / "backend" / "app").mkdir(parents=True)
    (src / "backend" / "alembic" / "versions").mkdir(parents=True)
    (src / "backend" / "app" / "version.py").write_text(
        f'__version__ = "{version}"\n', encoding="utf-8")
    (src / "backend" / "app" / "__init__.py").write_text("", encoding="utf-8")
    (src / "backend" / "pyproject.toml").write_text(
        '[project]\nname = "graphban-api"\n', encoding="utf-8")
    (src / "backend" / "alembic.ini").write_text("[alembic]\n", encoding="utf-8")
    (src / "backend" / "alembic" / "versions" / "0001_initial.py").write_text(
        "revision = '0001'\n", encoding="utf-8")
    (src / "backend" / ".env").write_text("JWT_SECRET=packer-secret\n", encoding="utf-8")
    (src / "backend" / ".pytest.db").write_text("junk\n", encoding="utf-8")
    (src / "backend" / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    (src / ".env").write_text("JWT_SECRET=root-secret\n", encoding="utf-8")
    (src / "LICENSE.md").write_text("FSL\n", encoding="utf-8")
    (src / ".env.example").write_text("JWT_SECRET=\n", encoding="utf-8")
    _host_scripts(src)
    return src


def fake_build(src: pathlib.Path, sha: str) -> int:
    dist = src / "web" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html>", encoding="utf-8")
    (dist / "assets").mkdir(exist_ok=True)
    (dist / "assets" / "index.js").write_text("1", encoding="utf-8")
    return 0


def serving(sha: str):
    return (lambda base, **kw: {"status": "ok", "git_sha": sha, "db": "ok"},
            lambda base, **kw: sha)


# ---- the tree a release is ------------------------------------------------------------------

def test_pack_lays_out_backend_web_sha_and_no_env(tmp_path):
    src = make_src(tmp_path)
    out = tmp_path / "out"
    rc, tree = gp.pack(src, out, sha=SHA, version=VER, build=fake_build)
    assert rc == 0
    assert tree == out / f"graphban-{VER}"
    assert (tree / "backend" / "pyproject.toml").is_file()
    assert (tree / "backend" / "alembic" / "versions" / "0001_initial.py").is_file()
    assert (tree / "web" / "dist" / "index.html").is_file()
    assert (tree / "web" / "dist" / "version.txt").read_text(encoding="utf-8").strip() == SHA
    assert (tree / "GIT_SHA").read_text(encoding="utf-8").strip() == SHA
    assert (tree / "SPA").read_text(encoding="utf-8").strip() == "present"
    assert (tree / "LICENSE.md").is_file()
    assert (tree / "scripts" / "graphban_host.py").is_file()
    assert (tree / ".env.example").is_file()
    assert not (tree / "backend" / "tests").exists()
    assert not (tree / "backend" / ".pytest.db").exists()
    assert not (tree / "backend" / "Dockerfile").exists()
    assert gp.env_files(tree) == []
    tarball = out / f"graphban-{VER}.tar.gz"
    assert tarball.is_file()
    with tarfile.open(tarball) as tf:
        names = tf.getnames()
    assert f"graphban-{VER}/GIT_SHA" in names
    assert not any(n.endswith("/.env") or n.endswith(".env") for n in names)


def test_src_dot_env_is_not_shipped(tmp_path):
    """THE COPY THIS EXISTS FOR. copytree of backend without the ignore would put
    the packer's JWT in the tarball, and first install would load it."""
    src = make_src(tmp_path)
    rc, tree = gp.pack(src, tmp_path / "out", sha=SHA, version=VER, build=fake_build)
    assert rc == 0
    assert not (tree / "backend" / ".env").exists()
    assert not (tree / ".env").exists()
    assert "packer-secret" not in (tree / ".env.example").read_text(encoding="utf-8")


def test_placeholder_version_is_refused(tmp_path):
    src = make_src(tmp_path, version="0.1.0")
    rc, tree = gp.pack(src, tmp_path / "out", sha=SHA, version="0.1.0",
                       api_only=True)
    assert rc == 1
    assert tree is None
    assert not (tmp_path / "out" / "graphban-0.1.0").exists()


def test_missing_alembic_is_refused(tmp_path):
    src = make_src(tmp_path)
    (src / "backend" / "alembic" / "versions" / "0001_initial.py").unlink()
    rc, tree = gp.pack(src, tmp_path / "out", sha=SHA, version=VER, api_only=True)
    assert rc == 1
    assert tree is None


def test_api_only_writes_spa_na_and_has_no_dist(tmp_path):
    src = make_src(tmp_path)

    def boom(*a, **k):
        raise AssertionError("api-only must not build the SPA")

    rc, tree = gp.pack(src, tmp_path / "out", sha=SHA, version=VER,
                       api_only=True, build=boom)
    assert rc == 0
    assert (tree / "SPA").read_text(encoding="utf-8").strip() == "n/a"
    assert not (tree / "web" / "dist").exists()


def test_failed_web_build_does_not_ship_an_implicit_api_only_cut(tmp_path):
    src = make_src(tmp_path)
    rc, tree = gp.pack(src, tmp_path / "out", sha=SHA, version=VER,
                       build=lambda *a, **k: 1)
    assert rc == 1
    assert tree is None
    assert not (tmp_path / "out" / f"graphban-{VER}").exists()


def test_THE_CALL_build_web_runs_unless_api_only(tmp_path):
    src = make_src(tmp_path)
    seen: list[str] = []

    def bw(tree, sha):
        seen.append(sha)
        return fake_build(tree, sha)

    rc, tree = gp.pack(src, tmp_path / "out", sha=SHA, version=VER, build=bw)
    assert rc == 0
    assert seen == [SHA]
    assert (tree / "web" / "dist" / "version.txt").read_text(encoding="utf-8").strip() == SHA


# ---- upgrade consumes what pack produced ----------------------------------------------------

def test_upgrade_consumes_a_packed_tree(tmp_path, monkeypatch):
    """Sabotage the CALL: pack a tree, hand it to upgrade. A packer whose layout
    upgrade cannot swap is a tarball nobody can install."""
    src = make_src(tmp_path)
    rc, tree = gp.pack(src, tmp_path / "out", sha=SHA, version=VER, build=fake_build)
    assert rc == 0

    root = tmp_path / "root"
    (root / "current" / "backend").mkdir(parents=True)
    (root / "current" / "backend" / ".env").write_text("JWT_SECRET=operator\n",
                                                       encoding="utf-8")
    (root / "current" / "marker").write_text("old\n", encoding="utf-8")
    monkeypatch.setattr(up.time, "sleep", lambda s: None)
    probe, web = serving(SHA)
    rc = up.upgrade(
        root, tree, SHA, base="http://x", python=pathlib.Path("py"),
        restart=lambda action: None, probe=probe, web=web,
        head=lambda r, p: "0001",
    )
    assert rc == up.EXIT_OK
    assert (root / "current" / "backend" / "pyproject.toml").is_file()
    assert (root / "current" / "web" / "dist" / "index.html").is_file()
    env = (root / "current" / "backend" / ".env").read_text(encoding="utf-8")
    assert "operator" in env
    assert "packer-secret" not in env
    assert (root / "previous" / "marker").read_text(encoding="utf-8") == "old\n"


# ---- packing a ref is a worktree, not cwd ---------------------------------------------------

def _git(repo: pathlib.Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def make_git_src(tmp_path: pathlib.Path) -> pathlib.Path:
    src = make_src(tmp_path)
    (src / "backend" / "app" / "marker.txt").write_text("committed\n", encoding="utf-8")
    (src / ".gitignore").write_text(".env\n", encoding="utf-8")
    _git(src, "init")
    _git(src, "config", "user.email", "pack@test")
    _git(src, "config", "user.name", "pack")
    _git(src, "config", "commit.gpgsign", "false")
    _git(src, "add", "-A")
    _git(src, "commit", "-m", "init")
    return src


def test_THE_CALL_packing_a_ref_does_not_ship_uncommitted_files(tmp_path, monkeypatch):
    """Same trap as deploy.sh: packing the working tree ships in-flight edits.

    Drop `materialize` / pack cwd of `--ref` and this fails, because marker.txt
    on disk is `dirty` while HEAD is `committed`.
    """
    repo = make_git_src(tmp_path)
    (repo / "backend" / "app" / "marker.txt").write_text("dirty\n", encoding="utf-8")
    (repo / "backend" / ".env").write_text("JWT_SECRET=uncommitted\n", encoding="utf-8")
    out = tmp_path / "out"
    monkeypatch.chdir(repo)
    rc = gp.main(["HEAD", "--out", str(out), "--api-only"])
    assert rc == 0
    tree = out / f"graphban-{VER}"
    assert (tree / "backend" / "app" / "marker.txt").read_text(encoding="utf-8") == "committed\n"
    assert gp.env_files(tree) == []


def test_main_refuses_without_ref_or_from(capsys):
    assert gp.main([]) == 2
    assert "need a ref" in capsys.readouterr().err


def test_script_is_executable():
    script = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "graphban_pack.py"
    assert script.is_file()
    assert script.stat().st_mode & 0o111, "graphban_pack.py is not executable"


def test_THE_CALL_ci_runs_backend_when_scripts_change():
    """A packer-only PR that skips the suite is the deploy.sh hole in CI clothes."""
    wf = pathlib.Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"
    assert "- 'scripts/**'" in wf.read_text()
