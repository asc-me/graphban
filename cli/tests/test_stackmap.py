"""`gban stack map` — the app-topology producer (GRPH-P55 §3, GRPH-1100)."""
from __future__ import annotations

import json
import os
import subprocess
import tomllib
from pathlib import Path

import pytest

from gban import config, stackmap
from gban.cli import main
from gban.client import EXIT_REFUSED
from gban.stackmap import (
    MapError, canonical, image_ref_name, literal_hosts, load_table, match_env,
    match_image, match_sdk, parse_yaml_subset, producer_version,
)

ROOT = Path(__file__).resolve().parents[2]
REFERENCE = ROOT / "docs" / "api-reference.md"
_GIT_ENV = {
    "GIT_AUTHOR_NAME": "Graphban Test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Graphban Test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(_GIT_ENV)
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "commit.gpgsign=false", *args],
        check=True, capture_output=True, text=True, env=env,
    )


def _commit(repo: Path) -> str:
    _git(repo, "init", "-b", "main")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "init")
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _table_cases() -> list[tuple[str, str, str]]:
    table = load_table(None)
    cases: list[tuple[str, str, str]] = []
    for row in table["image"]:
        for name in row["names"]:
            cases.append(("image", str(name), str(row["id"])))
    for row in table["sdk"]:
        for name in row.get("packages") or []:
            cases.append(("sdk", str(name), str(row["id"])))
        prefix = str(row.get("prefix") or "")
        if prefix:
            cases.append(("sdk", prefix + "client-probe", str(row["id"])))
    for row in table["env"]:
        for name in row["names"]:
            cases.append(("env", str(name), str(row["id"])))
    return cases


def _ids(kind: str, name: str, row_id: str) -> str:
    return f"{kind}-{name}-{row_id}".replace("/", "_")


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    """A home with no server, project, or key, so a laptop's gban config cannot post."""
    monkeypatch.setenv(config.HOME_ENV, str(tmp_path / "home"))
    for var in (config.URL_ENV, config.PROJECT_ENV, config.API_KEY_ENV, "GBFLEET_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


def _write_app(repo: Path) -> None:
    (repo / "compose.yml").write_text(
        "services:\n"
        "  api:\n"
        "    build: ./api\n"
        "    depends_on:\n"
        "      db:\n"
        "        condition: service_healthy\n"
        "      cache:\n"
        "        condition: service_started\n"
        "      payments:\n"
        "        condition: service_started\n"
        "    environment:\n"
        "      DATABASE_URL: postgresql+psycopg://user:super-secret-value-should-not-leak@db:5432/app\n"
        "  web:\n"
        "    build:\n"
        "      context: ./web\n"
        "    depends_on:\n"
        "      - api\n"
        "    environment:\n"
        "      - REDIS_URL=redis://cache:6379/0\n"
        "      - PLAIN_NAME\n"
        "  db:\n"
        "    image: postgres:16\n"
        "  cache:\n"
        "    image: redis:7\n"
        "  edge:\n"
        "    image: caddy:2\n"
        "  legacy:\n"
        "    image: nginx:alpine\n"
        "    external: true\n"
        "  worker:\n"
        "    extends:\n"
        "      file: other/compose.yml\n"
        "      service: worker\n",
        encoding="utf-8",
    )
    api = repo / "api"
    api.mkdir()
    (api / "requirements.txt").write_text("psycopg[binary]==3.1\nstripe==8.0\n", encoding="utf-8")
    (api / "Dockerfile").write_text("FROM python:3.12\n", encoding="utf-8")
    (api / "app.py").write_text(
        "URL = 'https://only-in-source.example/v1'\n", encoding="utf-8")
    web = repo / "web"
    web.mkdir()
    (web / "package.json").write_text(
        json.dumps({"dependencies": {"pg": "8.0.0", "@aws-sdk/client-s3": "3.0.0"}}),
        encoding="utf-8",
    )
    (repo / ".env.example").write_text(
        "STRIPE_API_URL=https://api.stripe.com/v1\n"
        "STRIPE_API_KEY=sk_live_SHOULDNOTLEAK\n"
        "OPENAI_API_KEY=sk-SHOULDNOTLEAK\n"
        "DATABASE_URL=postgres://localhost/app\n",
        encoding="utf-8",
    )
    (repo / ".env").write_text(
        "LEAK_URL=https://only-in-dotenv-leak.example\n", encoding="utf-8")


def _node(record: dict, group: str, id_: str) -> dict:
    found = [node for node in record[group] if node["id"] == id_]
    assert found, f"no {group} node {id_}"
    return found[0]


def _edge(record: dict, src: str, dst: str, protocol: str) -> dict:
    found = [edge for edge in record["edges"]
             if edge["from"] == src and edge["to"] == dst and edge["protocol"] == protocol]
    assert found, f"no edge {src} -> {dst} ({protocol})"
    return found[0]


def test_the_fixture_record_names_services_hosts_and_no_secrets(tmp_path):
    """Acceptance §8 items 2 and 7, plus the planner note on bare hosts."""
    repo = tmp_path / "app"
    repo.mkdir()
    _write_app(repo)
    sha = _commit(repo)
    record, warnings = stackmap.build_record(repo)
    text = canonical(record)
    again, _ = stackmap.build_record(repo)

    assert warnings == []
    assert canonical(again) == text
    assert record["producer_id"] == "gban"
    assert record["producer_version"] == producer_version()
    assert record["mapped_sha"] == sha
    assert "project_id" not in record

    api = _node(record, "components", "api")
    web = _node(record, "components", "web")
    assert api["dir"] == "api" and api["provenance"]["rule"] == "compose.build"
    assert web["dir"] == "web"
    db = _node(record, "infrastructure", "db")
    assert db["kind"] == "postgres"
    assert db["provenance"] == {"file": "compose.yml", "rule": "compose.image", "line": 22}
    cache = _node(record, "infrastructure", "cache")
    assert cache["kind"] == "redis"
    edge = _node(record, "infrastructure", "edge")
    assert edge["kind"] == "unclassified image"
    assert edge["label"] == "edge"
    assert "caddy" not in text

    labels = {node["label"] for node in record["unresolved"]}
    assert {"payments", "legacy", "worker"} <= labels
    # The `stripe` package and STRIPE_API_URL's api.stripe.com are one service (GRPH-1102).
    assert _node(record, "external", "stripe")["label"] == "Stripe"
    assert not [n for n in record["external"] if "stripe.com" in (n["id"] + n["label"])]
    assert [n["id"] for n in record["external"] if n["label"] == "Stripe"] == ["stripe"]
    assert _node(record, "external", "aws")["provenance"]["file"] == "web/package.json"

    depends = _edge(record, "api", "db", "postgres")
    assert depends["label"] == "depends_on"
    assert depends["provenance"]["rule"] == "compose.depends_on"
    assert depends["provenance"]["file"] == "compose.yml"
    stripe = _edge(record, "api", "stripe", "https")
    assert stripe["provenance"]["file"] == "api/requirements.txt"
    assert stripe["provenance"]["rule"] == "sdk.stripe"
    web_aws = _edge(record, "web", "aws", "https")
    assert web_aws["provenance"]["file"] == "web/package.json"
    assert web_aws["provenance"]["rule"] == "sdk.aws-sdk"
    # pg is the same postgres node compose already opened, not a second one.
    assert [node["id"] for node in record["infrastructure"] if node.get("kind") == "postgres"] == ["db"]
    assert _edge(record, "web", "db", "postgres")["provenance"]["file"] == "web/package.json"
    assert _edge(record, "web", "cache", "redis")["env"] == "REDIS_URL"
    # The env template's edge lands on the same node, so api has one https edge to Stripe.
    assert len([e for e in record["edges"] if e["from"] == "api" and e["to"] == "stripe"]) == 1

    assert "://" not in text
    for secret in (
        "https://api.stripe.com",
        "super-secret-value-should-not-leak",
        "sk_live_SHOULDNOTLEAK",
        "sk-SHOULDNOTLEAK",
        "only-in-dotenv-leak.example",
        "only-in-source.example",
        "postgresql+psycopg",
    ):
        assert secret not in text


def test_two_services_building_one_directory_keep_both_edges(tmp_path):
    repo = tmp_path / "shared"
    repo.mkdir()
    (repo / "compose.yml").write_text(
        "services:\n"
        "  a:\n"
        "    build: ./app\n"
        "    depends_on:\n"
        "      - db\n"
        "  b:\n"
        "    build: ./app\n"
        "    depends_on:\n"
        "      - cache\n"
        "  db:\n"
        "    image: postgres:16\n"
        "  cache:\n"
        "    image: redis:7\n",
        encoding="utf-8",
    )
    _commit(repo)
    record, _ = stackmap.build_record(repo)
    assert [node["id"] for node in record["components"]] == ["a"]
    assert _edge(record, "a", "db", "postgres")
    assert _edge(record, "a", "cache", "redis")


def test_a_project_overlay_row_is_used(tmp_path):
    repo = tmp_path / "overlay"
    repo.mkdir()
    overlay = repo / ".graphban"
    overlay.mkdir()
    (overlay / "stack-sdks.toml").write_text(
        '[[sdk]]\n'
        'id = "posthog"\n'
        'packages = ["posthog"]\n'
        'node = "posthog"\n'
        'group = "external"\n'
        'label = "PostHog"\n'
        'protocol = "https"\n',
        encoding="utf-8",
    )
    (repo / "requirements.txt").write_text("posthog==1\n", encoding="utf-8")
    _commit(repo)
    record, _ = stackmap.build_record(repo)
    assert _node(record, "external", "posthog")["label"] == "PostHog"
    assert match_sdk("posthog", load_table(None)["sdk"]) is None


_CASES = _table_cases()


@pytest.mark.parametrize("kind,name,row_id", _CASES, ids=[_ids(*case) for case in _CASES])
def test_every_shipped_table_row_matches(kind, name, row_id):
    table = load_table(None)
    if kind == "image":
        assert match_image(f"{name}:9", table["image"])["id"] == row_id
    elif kind == "sdk":
        assert match_sdk(name, table["sdk"])["id"] == row_id
    else:
        assert match_env(name, table["env"])["id"] == row_id


def test_image_match_is_the_name_not_a_substring():
    table = load_table(None)
    assert match_image("library/postgres:16", table["image"])["id"] == "postgres"
    assert match_image("pgvector/pgvector:pg16", table["image"])["id"] == "postgres"
    assert image_ref_name("pgvector/pgvector:pg16") == "pgvector/pgvector"
    assert match_image("notpostgres:16", table["image"]) is None
    assert match_image("my-postgres-extra:1", table["image"]) is None


def test_literal_hosts_keep_the_host_and_drop_the_url():
    assert literal_hosts(
        "postgresql+psycopg://user:super-secret-value-should-not-leak@db:5432/app"
    ) == [("postgres", "db")]
    assert literal_hosts("${OPENAI_BASE_URL:-https://api.openai.com/v1}") == [
        ("https", "api.openai.com")]
    assert literal_hosts(
        "postgresql+psycopg://${POSTGRES_USER:-agentledger}:"
        "${POSTGRES_PASSWORD:-agentledger}@db:5432/${POSTGRES_DB:-agentledger}"
    ) == [("postgres", "db")]
    assert literal_hosts("not a url") == []


def test_yaml_subset_accepts_crlf_and_does_not_expand_anchors():
    marked = parse_yaml_subset("services:\r\n  db:\r\n    image: postgres:16\r\n")
    assert marked.value["services"].value["db"].value["image"].value == "postgres:16"
    aliased = parse_yaml_subset("a: &name value\nb: *name\nmerged:\n  <<: *name\n  image: redis:7\n")
    assert aliased.value["b"].value == "*name"
    assert aliased.value["merged"].value["image"].value == "redis:7"
    assert "<<" not in aliased.value["merged"].value


def test_an_empty_or_missing_table_is_not_a_silent_empty_map(tmp_path, monkeypatch):
    missing = tmp_path / "absent.toml"
    monkeypatch.setattr(stackmap, "_shipped_path", lambda: missing)
    with pytest.raises(MapError, match="missing"):
        load_table(None)
    empty = tmp_path / "stack_sdks.toml"
    empty.write_text("# no rows\n", encoding="utf-8")
    monkeypatch.setattr(stackmap, "_shipped_path", lambda: empty)
    with pytest.raises(MapError, match="no rows"):
        load_table(None)


def test_no_commit_refuses_and_posts_nothing(isolated, monkeypatch, capsys):
    repo = isolated / "bare"
    repo.mkdir()
    (repo / "compose.yml").write_text("services:\n  db:\n    image: postgres:16\n", encoding="utf-8")
    _git(repo, "init", "-b", "main")
    monkeypatch.setattr("gban.client.Client", lambda *a, **k: (_ for _ in ()).throw(AssertionError("posted")))
    assert main(["stack", "map", "--dry-run", "--repo", str(repo)]) == EXIT_REFUSED
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "no commit" in captured.err


def test_nothing_to_map_is_a_refusal(tmp_path):
    repo = tmp_path / "empty"
    repo.mkdir()
    (repo / "README.md").write_text("nothing here\n", encoding="utf-8")
    _commit(repo)
    with pytest.raises(MapError, match="nothing to map"):
        stackmap.build_record(repo)


def test_dry_run_prints_the_record_and_does_not_construct_a_client(isolated, monkeypatch, capsys):
    repo = isolated / "app"
    repo.mkdir()
    _write_app(repo)
    _commit(repo)

    def boom(*_args, **_kwargs):
        raise AssertionError("dry-run constructed a client")

    monkeypatch.setattr("gban.client.Client", boom)
    assert main(["stack", "map", "--dry-run", "--repo", str(repo)]) == 0
    out = capsys.readouterr().out
    record = json.loads(out)
    assert record["producer_id"] == "gban"
    assert "://" not in out
    assert out.endswith("\n")


def test_a_post_uses_the_documented_api_key_route(isolated, monkeypatch, capsys):
    repo = isolated / "app"
    repo.mkdir()
    _write_app(repo)
    _commit(repo)
    constructed: list[tuple] = []
    calls: list[tuple] = []

    class FakeClient:
        def __init__(self, url, token="", api_key=""):
            constructed.append((url, token, api_key))

        def call(self, method, path, body=None):
            calls.append((method, path, body))
            return {"posted_by": "server", "mapped_at": "not-part-of-the-request"}

    monkeypatch.setattr("gban.client.Client", FakeClient)
    monkeypatch.setenv("GBFLEET_API_KEY", "gb_sk_from_the_seat")
    monkeypatch.setenv("GRAPHBAN_API_KEY", "gb_sk_should_not_win")
    code = main(["--server", "http://gb.invalid", "--project", "demo",
                 "stack", "map", "--repo", str(repo)])
    assert code == 0
    assert constructed == [("http://gb.invalid", "", "gb_sk_from_the_seat")]
    assert len(calls) == 1
    method, path, body = calls[0]
    assert method == "POST"
    assert path == "/api/platform/stack/app"
    assert f"`{path}`" in REFERENCE.read_text(encoding="utf-8")
    assert body["project_id"] == "demo"
    assert "://" not in canonical(body)
    out = capsys.readouterr().out
    assert "mapped_at" not in out
    assert "posted_by" not in out
    assert json.loads(out)["project_id"] == "demo"


def test_graphban_api_key_is_used_when_the_seat_key_is_absent(isolated, monkeypatch):
    repo = isolated / "app"
    repo.mkdir()
    _write_app(repo)
    _commit(repo)
    seen: list[str] = []

    class FakeClient:
        def __init__(self, url, token="", api_key=""):
            seen.append(api_key)

        def call(self, method, path, body=None):
            return {}

    monkeypatch.setattr("gban.client.Client", FakeClient)
    monkeypatch.setenv("GRAPHBAN_API_KEY", "gb_sk_graphban")
    assert main(["--server", "http://gb.invalid", "--project", "demo",
                 "stack", "map", "--repo", str(repo)]) == 0
    assert seen == ["gb_sk_graphban"]


def test_posting_without_a_project_or_a_key_refuses(isolated, monkeypatch, capsys):
    repo = isolated / "app"
    repo.mkdir()
    _write_app(repo)
    _commit(repo)
    monkeypatch.setattr(
        "gban.client.Client", lambda *a, **k: (_ for _ in ()).throw(AssertionError("posted")))
    monkeypatch.setattr("gban.doctor.resolve_supervisor_key", lambda *a, **k: "")
    assert main(["--server", "http://gb.invalid", "stack", "map", "--repo", str(repo)]) == EXIT_REFUSED
    err = capsys.readouterr().err
    assert "needs a project" in err

    assert main(["--server", "http://gb.invalid", "--project", "demo",
                 "stack", "map", "--repo", str(repo)]) == EXIT_REFUSED
    assert "API key" in capsys.readouterr().err


def test_stack_without_map_refuses(isolated, capsys):
    assert main(["stack"]) == EXIT_REFUSED
    assert "stack map" in capsys.readouterr().err


def test_this_repo_compose_maps_without_leaking_defaults():
    """The checkout this command will actually be run against."""
    record, _warnings = stackmap.build_record(ROOT)
    text = canonical(record)
    assert _node(record, "infrastructure", "db")["kind"] == "postgres"
    assert _node(record, "components", "api")["dir"] == "backend"
    assert _node(record, "components", "web")["dir"] == "web"
    db_edge = _edge(record, "api", "db", "postgres")
    assert db_edge["provenance"]["rule"] == "compose.depends_on"
    assert db_edge["env"] == "DATABASE_URL"
    assert _edge(record, "web", "api", "depends_on")
    assert "://" not in text
    assert "https://api.openai.com" not in text
    assert "http://host.docker.internal" not in text
    # api.openai.com is the openai row's domain; host.docker.internal is this machine.
    assert _node(record, "external", "openai")["label"] == "OpenAI"
    assert not [n for n in record["external"] + record["unresolved"]
                if n["id"] in {"api.openai.com", "host.docker.internal"}]
    assert _node(record, "infrastructure", stackmap.LOCAL_NODE)["kind"] == "local"
    assert _edge(record, "api", stackmap.LOCAL_NODE, "https")["env"] == "OLLAMA_BASE_URL"
    # Only tracked files: ignored build output (dist-release/, …) never draws a deployable.
    assert sorted(n["dir"] for n in record["components"]) == ["backend", "web"]
    for secret in (
        "dev-secret-change-me",
        "POSTGRES_PASSWORD",
        "JWT_SECRET",
        "super-secret",
        "sk_live_",
        "sk-",
    ):
        assert secret not in text
    assert canonical(record) == canonical(stackmap.build_record(ROOT)[0])


def _local_repo(tmp_path, env_example: str):
    repo = tmp_path / "local"
    repo.mkdir()
    (repo / "compose.yml").write_text(
        "services:\n  app:\n    build: ./app\n", encoding="utf-8")
    (repo / "app").mkdir()
    (repo / "app" / "requirements.txt").write_text("httpx==0.27\n", encoding="utf-8")
    (repo / ".env.example").write_text(env_example, encoding="utf-8")
    _commit(repo)
    return repo


def test_every_local_host_is_this_machine_not_a_service(tmp_path):
    """GRPH-1102: host.docker.internal was external while localhost was unresolved."""
    repo = _local_repo(tmp_path,
        "OLLAMA_BASE_URL=http://host.docker.internal:11434\n"
        "API_URL=http://localhost:8000\n"
        "LOOP_URL=http://127.0.0.1:9000\n"
        "BIND_URL=http://0.0.0.0:7000\n")
    record, _ = stackmap.build_record(repo)
    assert record["external"] == []
    assert record["unresolved"] == []
    assert [n["id"] for n in record["infrastructure"]] == [stackmap.LOCAL_NODE]
    local = [e for e in record["edges"] if e["to"] == stackmap.LOCAL_NODE]
    assert {e["from"] for e in local} == {"app"}


def test_a_local_host_still_names_the_row_its_variable_belongs_to(tmp_path):
    repo = _local_repo(tmp_path, "DATABASE_URL=postgres://localhost/app\n")
    record, _ = stackmap.build_record(repo)
    assert [(n["id"], n["kind"]) for n in record["infrastructure"]] == [("postgres", "postgres")]


_DOMAIN_ROWS = [row for row in load_table(None)["sdk"] if row.get("domains")]


@pytest.mark.parametrize("row", _DOMAIN_ROWS, ids=[r["id"] for r in _DOMAIN_ROWS])
def test_every_domain_row_claims_its_hosts(row):
    from gban.stackmap import match_domain
    rows = load_table(None)["sdk"]
    for domain in row["domains"]:
        assert match_domain(f"api.{domain}", rows)["node"] == row["node"]
        assert match_domain(domain, rows)["node"] == row["node"]
    assert match_domain(f"not{row['domains'][0]}", rows) is None


def test_every_external_sdk_row_declares_its_domains():
    missing = [r["id"] for r in load_table(None)["sdk"]
               if r.get("group") == "external" and not r.get("domains")]
    assert missing == []


def test_ignored_and_untracked_files_are_not_mapped(tmp_path):
    repo = tmp_path / "tracked"
    repo.mkdir()
    (repo / ".gitignore").write_text("dist-release/\n", encoding="utf-8")
    (repo / "app").mkdir()
    (repo / "app" / "Dockerfile").write_text("FROM node:22\n", encoding="utf-8")
    _commit(repo)
    for stray in ("dist-release/pkg-1/backend", "scratch"):
        (repo / stray).mkdir(parents=True)
        (repo / stray / "Dockerfile").write_text("FROM python:3.12\n", encoding="utf-8")
    record, _ = stackmap.build_record(repo)
    assert [n["dir"] for n in record["components"]] == ["app"]


def _root_template_repo(tmp_path, compose: str | None) -> Path:
    repo = tmp_path / "rooted"
    repo.mkdir()
    for name in ("api", "web"):
        (repo / name).mkdir()
        (repo / name / "requirements.txt").write_text("stripe==8.0\n", encoding="utf-8")
    if compose is not None:
        (repo / "compose.yml").write_text(compose, encoding="utf-8")
    (repo / ".env.example").write_text("OPENAI_API_KEY=\n", encoding="utf-8")
    _commit(repo)
    return repo


def test_a_root_template_name_goes_to_the_service_that_declares_it(tmp_path):
    """GRPH-1104: a root .env.example drew web -> openai when only api reads it."""
    repo = _root_template_repo(tmp_path,
        "services:\n"
        "  api:\n"
        "    build: ./api\n"
        "    environment:\n"
        "      - OPENAI_API_KEY\n"
        "  web:\n"
        "    build: ./web\n")
    record, warnings = stackmap.build_record(repo)
    to_openai = [(e["from"], e["to"]) for e in record["edges"] if e["to"] == "openai"]
    assert to_openai == [("api", "openai")]
    assert not [e for e in record["edges"] if e["from"] == "web" and e["to"] != "stripe"]
    assert warnings == []
    assert canonical(record) == canonical(stackmap.build_record(repo)[0])


def test_a_root_template_with_no_compose_goes_to_every_component_and_says_so(tmp_path):
    repo = _root_template_repo(tmp_path, None)
    record, warnings = stackmap.build_record(repo)
    assert sorted(n["id"] for n in record["components"]) == ["api", "web"]
    assert sorted(e["from"] for e in record["edges"] if e["to"] == "openai") == ["api", "web"]
    assert [w for w in warnings if ".env.example" in w]


def test_a_root_template_read_through_env_file_goes_to_its_reader(tmp_path):
    repo = _root_template_repo(tmp_path,
        "services:\n"
        "  api:\n"
        "    build: ./api\n"
        "  web:\n"
        "    build: ./web\n"
        "    env_file: .env.example\n")
    record, warnings = stackmap.build_record(repo)
    assert [e["from"] for e in record["edges"] if e["to"] == "openai"] == ["web"]
    assert warnings == []


@pytest.mark.parametrize("worker_env", [
    "    environment:\n      - OPENAI_API_KEY\n",
    "    env_file: .env.example\n",
])
def test_a_root_template_named_only_by_an_image_service_is_not_every_component(
        tmp_path, worker_env):
    """GRPH-1104 bounce: a service that builds nothing still counts as compose naming it."""
    repo = tmp_path / "imaged"
    repo.mkdir()
    for name in ("api", "web"):
        (repo / name).mkdir()
        (repo / name / "requirements.txt").write_text("stripe==8.0\n", encoding="utf-8")
    (repo / "compose.yml").write_text(
        "services:\n"
        "  api:\n"
        "    build: ./api\n"
        "  web:\n"
        "    build: ./web\n"
        "  worker:\n"
        "    image: alpine\n" + worker_env, encoding="utf-8")
    (repo / ".env.example").write_text("OPENAI_API_KEY=\nANTHROPIC_API_KEY=\n",
                                       encoding="utf-8")
    _commit(repo)
    record, warnings = stackmap.build_record(repo)
    assert not [e for e in record["edges"]
                if e["from"] in ("api", "web") and e["to"] in ("openai", "anthropic")]
    assert [e["from"] for e in record["edges"] if e["to"] == "openai"] == ["worker"]
    assert not [w for w in warnings if ".env.example" in w]
    assert {"openai", "anthropic"} <= {n["id"] for n in record["external"]}


def test_this_repo_root_template_draws_no_web_edge_to_a_model_provider():
    record, _ = stackmap.build_record(ROOT)
    providers = {"anthropic", "openai", stackmap.LOCAL_NODE}
    assert not [e for e in record["edges"] if e["from"] == "web" and e["to"] in providers]
    assert _edge(record, "web", "api", "depends_on")
    ids = {n["id"] for n in record["external"] + record["infrastructure"]}
    assert providers <= ids


# ---- what a post says it did (GRPH-1103) ----------------------------------------------------
# A real HTTP server on loopback, so the confirmation and the refusal go through the same
# transport and error parsing a person's post does.


@pytest.fixture()
def fake_server():
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    state = {"requests": [], "status": 200, "reply": {}}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            state["requests"].append((self.path, self.headers.get("X-API-Key"), json.loads(raw)))
            reply = state["reply"](json.loads(raw)) if callable(state["reply"]) else state["reply"]
            data = json.dumps(reply).encode()
            self.send_response(state["status"])
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state["url"] = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()


def _mapped_app(isolated, monkeypatch) -> tuple[Path, str]:
    repo = isolated / "app"
    repo.mkdir()
    _write_app(repo)
    sha = _commit(repo)
    monkeypatch.setenv("GBFLEET_API_KEY", "gb_sk_seat")
    return repo, sha


def test_a_real_post_says_it_posted_and_where(isolated, monkeypatch, capsys, fake_server):
    repo, sha = _mapped_app(isolated, monkeypatch)
    fake_server["reply"] = lambda body: {**body, "mapped_at": "2026-10-23T10:00:00Z",
                                         "posted_by": "seat-1"}
    assert main(["--server", fake_server["url"], "--project", "demo",
                 "stack", "map", "--repo", str(repo)]) == 0
    assert len(fake_server["requests"]) == 1
    captured = capsys.readouterr()
    assert captured.out == canonical(fake_server["requests"][0][2])
    assert (f"gban: posted demo app map at {sha[:7]} to {fake_server['url']}"
            in captured.err)
    assert "posted_by seat-1" in captured.err
    assert "dry run" not in captured.err


def test_dry_run_says_nothing_was_posted(isolated, monkeypatch, capsys, fake_server):
    repo, _ = _mapped_app(isolated, monkeypatch)
    assert main(["--server", fake_server["url"], "--project", "demo",
                 "stack", "map", "--dry-run", "--repo", str(repo)]) == 0
    captured = capsys.readouterr()
    assert fake_server["requests"] == []
    assert "gban: dry run, nothing posted" in captured.err
    assert "posted demo" not in captured.err
    assert json.loads(captured.out)["project_id"] == "demo"


def test_a_refused_post_prints_the_reasons_and_not_the_record(isolated, monkeypatch, capsys,
                                                              fake_server):
    repo, _ = _mapped_app(isolated, monkeypatch)
    fake_server["status"] = 422
    fake_server["reply"] = {"detail": [{"path": "edges[2].to", "reason": "unresolved node"},
                                       {"path": "mapped_sha", "reason": "unknown is not a commit"}]}
    assert main(["--server", fake_server["url"], "--project", "demo",
                 "stack", "map", "--repo", str(repo)]) == EXIT_REFUSED
    assert len(fake_server["requests"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "edges[2].to: unresolved node" in captured.err
    assert "mapped_sha: unknown is not a commit" in captured.err
    assert "posted demo" not in captured.err
