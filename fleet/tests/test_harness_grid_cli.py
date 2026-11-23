"""`gbfleet harness grid` prints the cells with a baseline column (GRPH-1862)."""
import httpx
import pytest

from gbfleet import cli
from gbfleet.client import Graphban


def _cell(cap, baseline, finished=8):
    side = {"finished": finished, "signed_off": 6, "rate": 0.75}
    return {"key": {"vendor": "anthropic", "model": "sonnet", "binary_version": "1",
                    "capability": cap, "size_band": "S"},
            "finished": finished, "signed_off": 6, "rate": 0.75, "baseline": baseline,
            "samples": {"natural": side, "probe": {"finished": 0, "signed_off": 0, "rate": None}}}


PAYLOAD = {"floor": 5, "cells": [
    _cell("A1", {"suite_id": "template", "suite_version": "0.1.0", "n": 2, "green": 1,
                 "rate": 0.5, "below_floor": True}),
    _cell("B5", None)]}


def test_grid_prints_cells_with_baseline_column(monkeypatch, capsys):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["key"] = request.headers.get("x-api-key")
        seen["url"] = str(request.url)
        return httpx.Response(200, json=PAYLOAD)

    real = Graphban.__init__

    def init(self, *a, **kw):
        real(self, *a, transport=httpx.MockTransport(handler), **kw)

    monkeypatch.setattr(Graphban, "__init__", init)
    monkeypatch.setenv("GBFLEET_API_KEY", "gbk_agent")
    assert cli.main(["harness", "grid", "--server", "http://gb.invalid", "--project", "p1"]) == 0
    out = capsys.readouterr().out
    assert seen["key"] == "gbk_agent" and "/api/harness/grid" in seen["url"]
    assert "baseline" in out.splitlines()[0]
    assert "anthropic:sonnet A1\t8\tnatural\t75%\t50% n=2 template v0.1.0 (below floor)" in out
    assert "B5\t8\tnatural\t75%\tno baseline" in out


def test_grid_needs_an_agent_key(monkeypatch, capsys):
    monkeypatch.delenv("GBFLEET_API_KEY", raising=False)
    assert cli.main(["harness", "grid", "--server", "http://x.invalid", "--project", "p"]) == 2
