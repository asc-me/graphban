"""GRPH-1429: `build.test_scope` `engines` — which engines `agent_full` needs full runs on."""
from __future__ import annotations

from types import SimpleNamespace

from gbfleet.supervisor import Merger


def _full(engine, **kw):
    return {"kind": "test", "engine": engine, "scope": "full", "passed": 10, "failed": 0, **kw}


class _Client:
    def __init__(self, params):
        self.params = params
        self.calls: list[str] = []

    def call(self, tool, **_):
        self.calls.append(tool)
        return {"toggles": [{"rule_id": "build.test_scope",
                             "params": {"value": self.params}}]}


def _merger(params):
    m = SimpleNamespace(client=_Client(params))
    m._test_scope_rule = lambda: Merger._test_scope_rule(m)
    return m


def _local_full(item, params):
    return Merger._local_full(_merger(params), item)


def test_engines_unset_needs_postgres_and_sqlite():
    params = {"mode": "agent_full"}
    assert Merger._test_scope_rule(_merger(params)) == ("agent_full", ["postgres", "sqlite"])
    assert _local_full({"evidence": [_full("postgres"), _full("sqlite")]}, params)
    assert not _local_full({"evidence": [_full("postgres")]}, params)


def test_engines_mysql_is_met_by_one_full_mysql_run_and_not_by_sqlite():
    params = {"mode": "agent_full", "engines": ["mysql"]}
    assert Merger._test_scope_rule(_merger(params)) == ("agent_full", ["mysql"])
    assert _local_full({"evidence": [_full("mysql")]}, params)
    assert not _local_full({"evidence": [_full("sqlite")]}, params)
    assert not _local_full({"evidence": [_full("mysql", scope="partial")]}, params)


def test_engines_apply_only_under_agent_full():
    params = {"mode": "agent_and_pr", "engines": ["mysql"]}
    assert not _local_full({"evidence": [_full("mysql")]}, params)


def test_an_item_without_a_full_run_never_reads_the_rule():
    m = _merger({"mode": "agent_full"})
    partial = {"kind": "test", "engine": "sqlite", "scope": "partial", "passed": 5, "failed": 0}
    assert not Merger._local_full(m, {"evidence": [partial]})
    assert m.client.calls == []


def test_a_full_run_reads_the_rule_once():
    m = _merger({"mode": "agent_full"})
    assert Merger._local_full(m, {"evidence": [_full("postgres"), _full("sqlite")]})
    assert m.client.calls == ["get_rule_toggles"]
