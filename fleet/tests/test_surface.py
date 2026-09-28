"""What a child loads, and the rule that a missing list is not an empty one."""
from __future__ import annotations

import io
import json
from pathlib import Path

from gbfleet import doctor
from gbfleet.surface import discrepancies, scan, summary


def _which(present: set[str]):
    def find(binary: str):
        return f"/bin/{binary}" if binary in present else None
    return find


def test_grok_inspect_keeps_names_and_drops_everything_else(tmp_path: Path):
    def fake(_repo: Path) -> dict:
        return {
            "skills": [
                {"name": "commit", "description": "holds a secret",
                 "source": {"type": "user", "path": "/Users/alex/.grok/skills/commit/SKILL.md"}},
                {"name": "wip", "compatibilityStatus": "disabled", "source": {"type": "user"}},
            ],
            "mcpServers": [
                {"name": "gmail", "target": "https://example.test/token",
                 "source": {"type": "claudeJson", "path": "/Users/alex/.claude.json"}},
            ],
        }

    inventory = scan(tmp_path, which=_which({"grok"}), inspect_grok=fake)
    grok = next(row for row in inventory["harnesses"] if row["vendor"] == "grok")

    assert grok["skills_status"] == "checked"
    assert grok["skills"] == [{"name": "commit", "source": "user"}]
    assert grok["disabled"] == [{"name": "wip", "source": "user"}]
    assert {"name": "gmail", "source": "claudeJson"} in grok["mcps"]
    assert {"name": "graphban", "source": "seat"} in grok["mcps"]
    blob = json.dumps(grok)
    assert "secret" not in blob
    assert "token" not in blob
    assert "/Users/" not in blob


def test_a_missing_binary_is_not_an_empty_list(tmp_path: Path):
    inventory = scan(tmp_path, which=_which(set()), inspect_grok=lambda _repo: {})
    claude = next(row for row in inventory["harnesses"] if row["vendor"] == "claude")

    assert claude["installed"] is False
    assert claude["skills_status"] == "unknown"
    assert claude["skills"] == []
    gaps = discrepancies(inventory["harnesses"])
    assert gaps["skills"]["rows"] == []
    assert "not a discrepancy" in gaps["skills"]["reason"]


def test_a_partial_miss_is_not_absence_and_a_checked_miss_is(tmp_path: Path):
    harnesses = [
        {"vendor": "grok", "skills_status": "checked", "skills": [{"name": "commit"}],
         "disabled": [], "mcps_status": "checked",
         "mcps": [{"name": "graphban"}, {"name": "context7"}]},
        {"vendor": "claude", "skills_status": "partial", "skills": [{"name": "review"}],
         "disabled": [], "mcps_status": "checked", "mcps": [{"name": "graphban"}]},
        {"vendor": "gbagent", "skills_status": "checked", "skills": [], "disabled": [],
         "mcps_status": "checked", "mcps": [{"name": "graphban"}]},
    ]
    gaps = discrepancies(harnesses)

    skill_names = {row["name"]: row for row in gaps["skills"]["rows"]}
    # claude's scan did not list commit. That is not evidence claude lacks it.
    assert "claude" not in skill_names["commit"]["absent"]
    assert skill_names["commit"]["absent"] == ["gbagent"]
    # review was found on the partial scan and is absent from both complete lists.
    assert skill_names["review"]["partial_present"] == ["claude"]
    assert set(skill_names["review"]["absent"]) == {"grok", "gbagent"}

    mcp_names = {row["name"]: row for row in gaps["mcps"]["rows"]}
    assert mcp_names["context7"]["present"] == ["grok"]
    assert set(mcp_names["context7"]["absent"]) == {"claude", "gbagent"}
    assert "graphban" not in mcp_names


def test_one_complete_list_does_not_read_as_no_discrepancy():
    gaps = discrepancies([
        {"vendor": "grok", "skills_status": "checked", "skills": [{"name": "commit"}],
         "disabled": [], "mcps_status": "unknown", "mcps": []},
    ])
    assert gaps["skills"]["rows"] == []
    assert "only grok" in gaps["skills"]["reason"]
    text = summary({"harnesses": [
        {"vendor": "grok", "skills_status": "checked", "skills": [{"name": "commit"}],
         "disabled": [], "skills_reason": "", "mcps_status": "unknown", "mcps": [],
         "mcps_reason": "not measured"},
    ]})
    assert "0 skill" not in text
    assert "only grok" in text


def test_doctor_skips_the_scan_unless_asked(tmp_path: Path):
    called = {"n": 0}

    def boom(_repo: Path):
        called["n"] += 1
        return {"harnesses": []}

    report = doctor.run(repo=tmp_path, out=io.StringIO(), surface_scan=boom)
    assert called["n"] == 0
    assert "harness surface" not in [f.name for f in report.findings]

    report = doctor.run(
        repo=tmp_path, out=io.StringIO(), surface=True, surface_scan=boom,
    )
    assert called["n"] == 1
    assert any(f.name == "harness surface" and f.status == "UNKNOWN" for f in report.findings)


def test_a_disabled_skill_is_not_counted_as_loaded():
    gaps = discrepancies([
        {"vendor": "grok", "skills_status": "checked", "skills": [],
         "disabled": [{"name": "wip"}], "mcps_status": "checked", "mcps": []},
        {"vendor": "claude", "skills_status": "checked", "skills": [{"name": "wip"}],
         "disabled": [], "mcps_status": "checked", "mcps": []},
    ])
    row = gaps["skills"]["rows"][0]
    assert row["name"] == "wip"
    assert row["present"] == ["claude"]
    assert row["disabled_on"] == ["grok"]
    assert row["absent"] == []
