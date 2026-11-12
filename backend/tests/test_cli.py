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


def _write_cfg(cfg_path, **kw):
    cfg_path.write_text(json.dumps({"cloud_url": "https://c.example", **kw}))


def test_link_has_no_api_key_flag_and_needs_a_console(cfg_path, capsys):
    with pytest.raises(SystemExit):
        cli.main(["link", "--cloud-url", "https://c.example", "--api-key", "gb_sk_x"])
    with pytest.raises(SystemExit, match="--cloud-url"):
        cli.main(["link"])


def test_link_help_describes_the_device_flow(capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["link", "-h"])
    assert e.value.code == 0
    out = capsys.readouterr().out
    assert "device code" in out and "LOCAL=CLOUD" in out
    assert "--api-key" not in out


def test_status_reports_not_linked(cfg_path, capsys):
    assert cli.main(["status"]) == 0
    assert "Not linked" in capsys.readouterr().out


def test_status_linked_shows_never_synced(cfg_path, capsys, monkeypatch):
    from app.services import code_sync
    monkeypatch.setattr(code_sync, "link_status",
                        lambda db: {"linked": True, "cloud_url": "https://c.example", "source": "db"})
    _write_cfg(cfg_path)
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "https://c.example" in out and "Last sync : never" in out


def test_sync_reports_not_linked_cleanly(cfg_path, monkeypatch):
    from app.services import code_sync

    def boom(*a, **k):
        raise code_sync.NotLinked("no cloud sync target configured")

    monkeypatch.setattr(code_sync, "push_mapped", boom)
    with pytest.raises(SystemExit):
        cli.main(["sync"])  # not linked -> clean exit, not a traceback


def test_sync_prints_one_line_per_project_and_refusal_is_non_zero(cfg_path, monkeypatch, capsys):
    from app.services import code_sync
    results = [{"project_id": "core", "status": "done", "nodes": 3, "edges": 2},
               {"project_id": "web", "status": "refused", "reason": "no access"},
               {"project_id": "ops", "status": "unknown", "reason": "could not reach c"}]
    monkeypatch.setattr(code_sync, "push_mapped", lambda db: results)
    assert cli.main(["sync"]) == 1
    lines = capsys.readouterr().out.strip().splitlines()
    assert lines == ["core: done · 3 nodes, 2 edges", "web: refused · no access",
                     "ops: unknown · could not reach c"]


def test_sync_all_done_exits_zero(cfg_path, monkeypatch):
    from app.services import code_sync
    monkeypatch.setattr(code_sync, "push_mapped",
                        lambda db: [{"project_id": "core", "status": "done", "nodes": 1, "edges": 0}])
    assert cli.main(["sync"]) == 0


def test_purge_requires_confirmation(cfg_path):
    _write_cfg(cfg_path)
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
