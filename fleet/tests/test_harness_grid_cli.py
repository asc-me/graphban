"""`gbfleet harness grid` prints the cells with a baseline column (GRPH-1862)."""
import httpx
import pytest

from gbfleet import cli
from gbfleet.client import Graphban


def _cell(cap, baseline, finished=8, version="1", band="S"):
    side = {"finished": finished, "signed_off": 6, "rate": 0.75}
    return {"key": {"vendor": "anthropic", "model": "sonnet", "binary_version": version,
                    "capability": cap, "size_band": band},
            "finished": finished, "signed_off": 6, "rate": 0.75, "baseline": baseline,
            "samples": {"natural": side, "probe": {"finished": 0, "signed_off": 0, "rate": None}}}


PAYLOAD = {"floor": 5, "cells": [
    _cell("A1", {"suite_id": "template", "suite_version": "0.1.0", "n": 2, "green": 1,
                 "rate": 0.5, "below_floor": True}),
    _cell("B5", None),
    _cell("B5", None, version="2"),
    _cell("B5", None, band="M")]}


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
    assert ("anthropic:sonnet@1 A1 band S\t8\tnatural\t75%\t50% n=2 template v0.1.0 (below floor)"
            in out)
    assert "anthropic:sonnet@1 B5 band S\t8\tnatural\t75%\tno baseline" in out
    # Separate cells (another binary, another band) print as distinguishable lines.
    rows = [line.split("\t")[0] for line in out.splitlines()[1:]]
    assert len(rows) == len(set(rows)) == 4
    assert "anthropic:sonnet@2 B5 band S" in rows and "anthropic:sonnet@1 B5 band M" in rows


def test_grid_needs_an_agent_key(monkeypatch, capsys):
    monkeypatch.delenv("GBFLEET_API_KEY", raising=False)
    assert cli.main(["harness", "grid", "--server", "http://x.invalid", "--project", "p"]) == 2
