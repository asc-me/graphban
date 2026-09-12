"""GRPH-865 — mix is a share of recent launches, not a second weight axis.

The resolver is a pure function; the CALL is the two spawn sites that must pass
`mix=` from the per-item brief. A mix stored on the profile and never handed in
is winner-take-all wearing a distribution costume.
"""
from __future__ import annotations

from pathlib import Path

from gbfleet import matrix as m


ALL_INSTALLED = lambda r: (True, "")  # noqa: E731
ROOT = Path(__file__).resolve().parents[1] / "src" / "gbfleet"


def _row(harness, model="m", *, vendor=None, order=1, cost_class="cheap", local=False) -> m.Row:
    return m.Row(harness=harness, model=model, vendor=vendor or harness, lane="any",
                 tier="cheap", status="unverified", order=order, cost_class=cost_class,
                 local=local)


def _matrix(*rows: m.Row) -> m.Matrix:
    return m.Matrix(rows=tuple(rows), path=Path("<test>"))


def test_mix_picks_the_harness_furthest_below_its_target_even_when_it_scored_worse():
    # Locality 1.0, claude local → claude wins on score. Mix must still pick grok:
    # claude is 16/20 against a 0.4 target.
    claude = _row("claude", "sonnet", cost_class="frontier", local=True, order=1)
    grok = _row("grok", "grok-4.6", cost_class="cheap", local=False, order=2)
    prof = m.Profile(user="u", mix={"claude": 0.4, "grok": 0.4},
                     weights={"locality": 1.0})
    res = _matrix(claude, grok).resolve(
        tier="cheap", profile=prof, installed=ALL_INSTALLED,
        mix={"n": 20, "by_harness": {"claude": 16, "grok": 2}, "unreported": 2},
    )
    assert res.winner is grok
    assert res.runner_up is claude
    mix_stage = next(s for s in res.stages if s["stage"] == "mix")
    assert mix_stage["applied"] is True
    assert mix_stage["n"] == 20
    assert res.explain()["winner"]["axes"]["mix"]["count"] == 2


def test_mix_below_the_floor_is_unmeasured_and_score_wins():
    claude = _row("claude", "sonnet", local=True)
    grok = _row("grok", "g", order=2)
    prof = m.Profile(user="u", mix={"claude": 0.4, "grok": 0.4}, weights={"locality": 1.0})
    res = _matrix(claude, grok).resolve(
        tier="cheap", profile=prof, installed=ALL_INSTALLED,
        mix={"n": 2, "by_harness": {"claude": 2}, "unreported": 0},
    )
    assert res.winner is claude
    mix_stage = next(s for s in res.stages if s["stage"] == "mix")
    assert mix_stage["applied"] is False
    assert "below floor" in (mix_stage["reason"] or "")
    assert "mix" not in (res.explain()["winner"]["axes"] or {})


def test_no_mix_on_the_profile_is_byte_identical_to_score_only():
    a = _row("claude", "sonnet", local=True)
    b = _row("grok", "g", order=2)
    prof = m.Profile(user="u", weights={"locality": 1.0})
    kwargs = dict(tier="cheap", profile=prof, installed=ALL_INSTALLED)
    with_none = _matrix(a, b).resolve(**kwargs, mix={"n": 20, "by_harness": {"grok": 20}})
    without = _matrix(a, b).resolve(**kwargs)
    assert with_none.winner is without.winner is a
    assert [s["stage"] for s in with_none.stages] == [s["stage"] for s in without.stages]


def test_zero_n_is_not_everyone_at_zero_percent():
    """0/0 must not prefer the first mix key. Unmeasured → score."""
    claude = _row("claude", "sonnet", local=True)
    grok = _row("grok", "g", order=2)
    prof = m.Profile(user="u", mix={"grok": 1.0}, weights={"locality": 1.0})
    res = _matrix(claude, grok).resolve(
        tier="cheap", profile=prof, installed=ALL_INSTALLED,
        mix={"n": 0, "by_harness": {}, "unreported": 0},
    )
    assert res.winner is claude
    assert next(s for s in res.stages if s["stage"] == "mix")["applied"] is False


def test_unnamed_harness_has_target_zero_and_does_not_win_while_a_named_one_is_short():
    gbagent = _row("gbagent", "q", local=True)
    grok = _row("grok", "g", order=2)
    prof = m.Profile(user="u", mix={"grok": 1.0}, weights={"locality": 1.0})
    res = _matrix(gbagent, grok).resolve(
        tier="cheap", profile=prof, installed=ALL_INSTALLED,
        mix={"n": 10, "by_harness": {"gbagent": 10}, "unreported": 0},
    )
    assert res.winner is grok


def test_both_spawn_paths_pass_mix_from_the_brief():
    """Sabotage the CALL: drop mix= from mcp.py / until.py and this fails, even if
    resolve() itself is thoroughly tested. The dest assertion, not the callee."""
    mcp = (ROOT / "mcp.py").read_text(encoding="utf-8")
    until = (ROOT / "until.py").read_text(encoding="utf-8")
    needle = 'mix=(brief or {}).get("mix")'
    assert needle in mcp, "mcp spawn resolve dropped mix="
    assert needle in until, "until resolve dropped mix="
    # Next to spend, so a copy that passes mix from fleet_status at launch cannot
    # satisfy this by inventing a different source.
    assert 'spend=(brief or {}).get("spend")' in mcp
    assert 'spend=(brief or {}).get("spend")' in until
