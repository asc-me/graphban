"""GRPH-1429: `build.test_scope` `engines` — which engines `agent_full` needs full runs on."""
from __future__ import annotations

from types import SimpleNamespace

from gbfleet import propose as propose_mod
from gbfleet.supervisor import Merger


def _full(engine, **kw):
    return {"kind": "test", "engine": engine, "scope": "full", "passed": 10, "failed": 0, **kw}


class _Client:
    def __init__(self, params):
        self.params = params

    def call(self, tool, **_):
        return {"toggles": [{"rule_id": "build.test_scope",
                             "params": {"value": self.params}}]}


def _engines(params):
    return Merger._test_engines(SimpleNamespace(client=_Client(params)))


def _local_full(item, params):
    return propose_mod.full_local_receipts(item, _engines(params))


def test_engines_unset_needs_postgres_and_sqlite():
    params = {"mode": "agent_full"}
    assert _engines(params) == ["postgres", "sqlite"]
    assert _local_full({"evidence": [_full("postgres"), _full("sqlite")]}, params)
    assert not _local_full({"evidence": [_full("postgres")]}, params)


def test_engines_mysql_is_met_by_one_full_mysql_run_and_not_by_sqlite():
    params = {"mode": "agent_full", "engines": ["mysql"]}
    assert _engines(params) == ["mysql"]
    assert _local_full({"evidence": [_full("mysql")]}, params)
    assert not _local_full({"evidence": [_full("sqlite")]}, params)
    assert not _local_full({"evidence": [_full("mysql", scope="partial")]}, params)
