"""AL-218: the `graphban` sync CLI — thin wrapper over the code-graph sync services.

The service calls are monkeypatched (they're covered by test_code_push / test_export_import);
these tests pin the CLI's own behaviour: config persistence, link resolution, guards.
"""
import json

import pytest

from app import cli


class _DummyDB:
    """Enough Session surface for the CLI's own paths. `link` writes the `sync_link`
    row as well as the config file (AL-281), so the double has to accept a write —
    what it records is covered for real in test_cli_link_signal.py."""

    def get(self, *a, **k):
        return None

    def add(self, *a, **k):
        pass

    def commit(self):
        pass

    def refresh(self, *a, **k):
        pass

    def close(self):
        pass


@pytest.fixture
def cfg_path(tmp_path, monkeypatch):
    p = tmp_path / "config.json"
    monkeypatch.setenv("AGENTLEDGER_CONFIG", str(p))
    monkeypatch.setattr(cli, "_session", lambda: _DummyDB())
    return p


def _fake_provision(captured):
    def fake(db, **kwargs):
        captured.update(kwargs)
        return {"provisioned": True, "email": "op@example.com", "password": "pw",
                "project_id": "p1", "project_name": "My Project", "project_tag": "MP",
                "memory_write_mode": "trusted",
                "key_scope": kwargs.get("key_scope", "project"),
                "key_tiers": kwargs.get("key_tiers") or [],
                "api_key": "gb_sk_test"}
    return fake


def test_init_defaults_to_a_project_scoped_key(cfg_path, monkeypatch):
    from app import bootstrap
    captured = {}
    monkeypatch.setattr(bootstrap, "provision", _fake_provision(captured))
    assert cli.main(["init"]) == 0
    assert captured["key_scope"] == "project"


def test_init_key_scope_global_reaches_provision(cfg_path, monkeypatch, capsys):
    from app import bootstrap
    captured = {}
    monkeypatch.setattr(bootstrap, "provision", _fake_provision(captured))
    assert cli.main(["init", "--key-scope", "global"]) == 0
    assert captured["key_scope"] == "global"
    assert "Global key" in capsys.readouterr().out


def test_init_rejects_unknown_key_scope(cfg_path):
    with pytest.raises(SystemExit) as e:
        cli.main(["init", "--key-scope", "world"])
    assert e.value.code == 2  # argparse choice failure, not a traceback


def test_init_key_tiers_accepts_comma_list_and_repeats(cfg_path, monkeypatch, capsys):
    from app import bootstrap
    captured = {}
    monkeypatch.setattr(bootstrap, "provision", _fake_provision(captured))
    assert cli.main(["init", "--key-tiers", "prd,misc", "--key-tiers", "fleet"]) == 0
    assert captured["key_tiers"] == ["prd", "misc", "fleet"]
    assert "prd, misc, fleet" in capsys.readouterr().out


def test_init_key_tiers_default_is_none(cfg_path, monkeypatch):
    from app import bootstrap
    captured = {}
    monkeypatch.setattr(bootstrap, "provision", _fake_provision(captured))
    assert cli.main(["init"]) == 0
    assert captured["key_tiers"] is None


def test_link_persists_target_chmod_600(cfg_path):
    assert cli.main(["link", "--cloud-url", "https://c.example/", "--api-key", "gb_sk_x", "--project", "core"]) == 0
    saved = json.loads(cfg_path.read_text())
    assert saved == {"cloud_url": "https://c.example", "api_key": "gb_sk_x", "project": "core"}  # slash trimmed
    assert oct(cfg_path.stat().st_mode)[-3:] == "600"  # credential file is not world-readable


def test_link_requires_both_url_and_key(cfg_path):
    # --api-key is the flag (identity). The parenthetical used to say "sync credential"
    # while Settings → Cloud / Sync names the same object a link key.
    with pytest.raises(SystemExit, match=r"--api-key \(the org-issued link key\)") as e:
        cli.main(["link", "--cloud-url", "https://c.example/"])  # missing --api-key
    assert "sync credential" not in str(e.value)


def test_link_help_names_the_org_issued_link_key(capsys):
    # THE CALL. Parent `graphban -h` does not list subparser help strings.
    # `graphban link -h` is what prints description= (and --api-key as the flag).
    with pytest.raises(SystemExit) as e:
        cli.main(["link", "-h"])
    assert e.value.code == 0
    out = capsys.readouterr().out
    assert "org-issued link key" in out
    assert "--api-key" in out
    assert "org-issued credential" not in out


def test_status_reports_not_linked(cfg_path, capsys):
    assert cli.main(["status"]) == 0
    assert "Not linked" in capsys.readouterr().out


def test_status_linked_shows_never_synced(cfg_path, capsys):
    cli.main(["link", "--cloud-url", "https://c.example", "--api-key", "gb_sk_x"])
    capsys.readouterr()
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "https://c.example" in out and "Last sync : never" in out


def test_sync_passes_the_stored_link_to_push(cfg_path, monkeypatch):
    cli.main(["link", "--cloud-url", "https://c.example", "--api-key", "gb_sk_x"])
    seen = {}

    def fake_push(db, *, project_id, cloud_url, api_key, **kw):
        seen.update(project_id=project_id, cloud_url=cloud_url, api_key=api_key)
        return {"pushed": 3, "removed": 0, "unchanged": 10}

    from app.services import code_sync
    monkeypatch.setattr(code_sync, "push", fake_push)
    assert cli.main(["sync"]) == 0
    assert seen == {"project_id": "core", "cloud_url": "https://c.example", "api_key": "gb_sk_x"}


def test_sync_reports_not_linked_cleanly(cfg_path, monkeypatch):
    from app.services import code_sync

    def boom(*a, **k):
        raise code_sync.NotLinked("no cloud sync target configured")

    monkeypatch.setattr(code_sync, "push", boom)
    with pytest.raises(SystemExit):
        cli.main(["sync"])  # not linked -> clean exit, not a traceback


def test_purge_requires_confirmation(cfg_path):
    cli.main(["link", "--cloud-url", "https://c.example", "--api-key", "gb_sk_x"])
    with pytest.raises(SystemExit):
        cli.main(["purge"])  # no --yes


def test_export_writes_a_bundle(cfg_path, tmp_path, monkeypatch):
    from app.services import code_graph
    monkeypatch.setattr(code_graph, "export_graph",
                        lambda db, pid: {"nodes": [{"path": "a.py"}], "edges": []})
    out = tmp_path / "graph.json"
    assert cli.main(["export", "--project", "core", "--out", str(out)]) == 0
    bundle = json.loads(out.read_text())
    assert bundle["bundle_version"] == 1 and bundle["project_id"] == "core"
    assert bundle["nodes"] == [{"path": "a.py"}]


def test_import_reads_a_bundle_into_describe_code(cfg_path, tmp_path, monkeypatch):
    src = tmp_path / "graph.json"
    src.write_text(json.dumps({"project_id": "core", "nodes": [{"path": "a.py"}], "edges": []}))
    seen = {}

    def fake_describe(db, *, project_id, nodes, edges, prune):
        seen.update(project_id=project_id, nodes=nodes, prune=prune)
        return {"nodes_upserted": len(nodes), "edges_upserted": 0}

    from app.services import code_graph
    monkeypatch.setattr(code_graph, "describe_code", fake_describe)
    assert cli.main(["import", "--in", str(src), "--prune"]) == 0
    assert seen["project_id"] == "core" and seen["prune"] is True and len(seen["nodes"]) == 1
