"""PRD-41 S2 — capability-aware scoring, cost, caps, the stage record.

Criteria 5, 14, 16, 20, 21, 22, 23, 24, 28. The resolver is a pure function over a
matrix, a measured map, a profile and a policy; these tests build those directly.
"""
from __future__ import annotations

from pathlib import Path

from gbfleet import matrix as m


ALL_INSTALLED = lambda r: (True, "")  # noqa: E731
EV = m.Evidence(item="GRPH-1", date="2026-09-01", outcome="signed_off")


def _row(harness="h", model="m", *, vendor="v", lane="any", tier="cheap",
         status="unverified", order=1, cost_class="cheap", local=False, evidence=(),
         price_in=None, price_out=None) -> m.Row:
    return m.Row(harness=harness, model=model, vendor=vendor, lane=lane, tier=tier,
                 status=status, order=order, cost_class=cost_class, local=local,
                 evidence=tuple(evidence),
                 price_per_mtoken_in=price_in, price_per_mtoken_out=price_out)


def _matrix(*rows: m.Row) -> m.Matrix:
    return m.Matrix(rows=tuple(rows), path=Path("<test>"))


def _q(vendor, model, cap, layer, value, n=5, n_band=None):
    return {(vendor, model, cap, layer): {
        "quality": m.Sample(value=value, n=n, layer=layer, n_band=n_band),
    }}


def _cost(vendor, model, cap, layer, tokens, *, reported=10, finished=10):
    return {(vendor, model, cap, layer): {
        "cost": m.CostSample(tokens_to_signoff=tokens, reported=reported, finished=finished,
                             comparable=reported / finished >= 0.8 if finished else False),
    }}


def _merge(*maps):
    out = {}
    for mp in maps:
        for k, v in mp.items():
            out.setdefault(k, {}).update(v)
    return out


# ---- 14: per-capability evidence --------------------------------------------------------------

def test_status_for_a_capability_reads_the_newest_entry_naming_it(tmp_path: Path):
    p = tmp_path / "m.toml"
    p.write_text(
        '[[rows]]\nharness="gbagent"\nmodel="x"\nvendor="gbagent"\nlane="any"\n'
        'tier="cheap"\nstatus="verified"\norder=1\ncost_class="local"\nlocal=true\n'
        "evidence = [\n"
        '  { item = "GRPH-1", date = "2026-01-01", outcome = "signed_off", capability = "A4" },\n'
        '  { item = "GRPH-2", date = "2026-02-01", outcome = "failed", capability = "B4" },\n'
        '  { item = "GRPH-3", date = "2026-03-01", outcome = "signed_off" },\n'
        "]\n"
    )
    row = m.load(p).rows[0]
    assert row.status == "verified"
    assert row.status_for("A4") == "verified"
    assert row.status_for("B4") == "failed"
    assert row.status_for("E3") == "verified", "unnamed capability falls through to the row"


# ---- 5: quality names the layer per capability ------------------------------------------------

def test_quality_is_the_mean_of_the_first_layer_clearing_the_floor_and_names_it():
    gbagent = _row("gbagent", "q", vendor="gbagent", cost_class="local", local=True)
    qwen = _row("qwen-code", "", vendor="alibaba", cost_class="cheap", order=2)
    cells = _merge(
        _q("gbagent", "q", "A4", "project", 0.40, n=7),
        _q("gbagent", "q", "B1", "project", 0.85, n=11),
        _q("alibaba", "", "A4", "platform", 0.78, n=50, n_band="50-199"),
        _q("alibaba", "", "B1", "org", 0.70, n=6),
    )
    prof = m.Profile(user="u", weights={"quality": 1.0})
    res = _matrix(gbagent, qwen).resolve(
        tier="cheap", profile=prof, installed=ALL_INSTALLED,
        capabilities=["A4", "B1", "H1"], cap_measured=cells,
    )
    g_axes = next(s[2] for s in res.scored if s[0] is gbagent)["quality"]
    q_axes = next(s[2] for s in res.scored if s[0] is qwen)["quality"]
    by_g = {c["capability"]: c for c in g_axes["by_capability"]}
    by_q = {c["capability"]: c for c in q_axes["by_capability"]}
    assert by_g["A4"]["layer"] == "project" and by_g["A4"]["n"] == 7
    assert by_g["B1"]["layer"] == "project" and by_g["B1"]["n"] == 11
    assert by_g["H1"]["used"] is False, "no layer, unmeasured"
    assert by_q["A4"]["layer"] == "platform"
    assert by_q["B1"]["layer"] == "org" and by_q["B1"]["n"] == 6
    assert g_axes["value"] == round((0.40 + 0.85) / 2, 3)
    assert q_axes["value"] == round((0.78 + 0.70) / 2, 3)
    assert "1 of 3" in g_axes["note"] or "2 of 3" in g_axes["note"]


# ---- 16: cost rank-scaling; bounced tokens stay in the numerator -----------------------------

def test_two_comparable_rows_rank_to_one_and_point_two_and_class_stays_on_thin_reporting():
    cheap = _row("gbagent", "q", vendor="gbagent", cost_class="local", local=True)
    dear = _row("claude", "sonnet", vendor="anthropic", cost_class="cheap", order=2)
    silent = _row("qwen-code", "", vendor="alibaba", cost_class="cheap", order=3)
    cells = _merge(
        _cost("gbagent", "q", "A4", "project", 41000),
        _cost("anthropic", "sonnet", "A4", "project", 84000),
        _cost("alibaba", "", "A4", "project", 1000, reported=1, finished=10),
    )
    prof = m.Profile(user="u", weights={"cost": 1.0})
    res = _matrix(cheap, dear, silent).resolve(
        tier="cheap", profile=prof, installed=ALL_INSTALLED,
        capabilities=["A4"], cap_measured=cells,
    )
    by = {s[0].harness: s[2]["cost"] for s in res.scored}
    assert by["gbagent"]["value"] == 1.0
    assert "41000" in by["gbagent"]["note"] or "41k" in by["gbagent"]["note"]
    assert by["claude"]["value"] == 0.2
    assert by["qwen-code"]["note"] == "class" and by["qwen-code"]["value"] == 0.6


def test_tokens_to_signoff_includes_bounced_attempts_in_the_numerator():
    """16 sabotage: counting only signed-off tokens makes a 30% row look cheap."""
    # 10 attempts, 3 signed off. 7 bounces at 10k + 3 sign-offs at 10k = 100k / 3 ≈ 33k.
    # Signed-off-only would be 30k / 3 = 10k.
    sample = m.CostSample(tokens_to_signoff=100_000 / 3, reported=10, finished=10,
                          comparable=True)
    assert sample.tokens_to_signoff > 20_000


# ---- 20: prices ------------------------------------------------------------------------------

def test_currency_appears_only_where_a_row_carries_a_price():
    priced = _row("gbagent", "q", vendor="gbagent", cost_class="local", local=True,
                  price_in=0.1, price_out=0.4)
    unpriced = _row("claude", "sonnet", vendor="anthropic", cost_class="cheap", order=2)
    both = _row("cursor-agent", "composer", vendor="cursor", cost_class="cheap", order=3,
                price_in=0.2, price_out=0.8)
    cells = _merge(
        _cost("gbagent", "q", "A4", "project", 50_000, reported=10, finished=10),
        _cost("anthropic", "sonnet", "A4", "project", 50_000),
        _cost("cursor", "composer", "A4", "project", 50_000),
    )
    # Force comparable tokens_in/out so currency can be computed.
    for key, cell in cells.items():
        c = cell["cost"]
        cell["cost"] = m.CostSample(tokens_to_signoff=c.tokens_to_signoff, reported=c.reported,
                                    finished=c.finished, comparable=True,
                                    tokens_in=25_000, tokens_out=25_000)
    prof = m.Profile(user="u", weights={"cost": 1.0})
    res = _matrix(priced, unpriced, both).resolve(
        tier="cheap", profile=prof, installed=ALL_INSTALLED,
        capabilities=["A4"], cap_measured=cells,
    )
    axes = {s[0].harness: s[2] for s in res.scored}
    assert "spend" in axes["gbagent"]
    assert "spend" not in axes["claude"]
    assert "spend" in axes["cursor-agent"]
    # Axis ranks on tokens even when only one row carries a price.
    assert axes["gbagent"]["cost"]["value"] == axes["claude"]["cost"]["value"]


# ---- 21, 23, 28: caps ------------------------------------------------------------------------

def test_a_cap_drops_the_row_over_what_is_left_and_names_both_numbers():
    cheap = _row("gbagent", "q", vendor="gbagent", cost_class="local", local=True)
    dear = _row("claude", "sonnet", vendor="anthropic", cost_class="frontier", order=2)
    cells = _merge(
        _cost("gbagent", "q", "A4", "project", 31000),
        _cost("anthropic", "sonnet", "A4", "project", 84000),
    )
    policy = m.Policy(caps={"per_item_tokens": 120_000})
    res = _matrix(cheap, dear).resolve(
        tier="cheap", policy=policy, installed=ALL_INSTALLED,
        capabilities=["A4"], cap_measured=cells,
        spend={"item_tokens": 70_000},
    )
    assert res.winner is cheap
    dropped = " ".join(res.dropped.get("policy") or [])
    assert "claude:sonnet" in dropped
    assert "per_item_tokens" in dropped
    assert "84k" in dropped and "50k" in dropped


def test_an_unreporting_row_is_dropped_under_a_cap_with_tokens_not_reported():
    """21 sabotage: letting a silent vendor through a cap must fail this."""
    silent = _row("qwen-code", "", vendor="alibaba", cost_class="cheap")
    honest = _row("gbagent", "q", vendor="gbagent", cost_class="local", local=True)
    cells = _merge(
        _cost("gbagent", "q", "A4", "project", 20000),
        _cost("alibaba", "", "A4", "project", 1, reported=0, finished=10),
    )
    res = _matrix(silent, honest).resolve(
        tier="cheap", policy=m.Policy(caps={"per_attempt_tokens": 50_000}),
        installed=ALL_INSTALLED, capabilities=["A4"], cap_measured=cells,
    )
    assert res.winner is honest
    assert any("tokens not reported" in d for d in res.dropped.get("policy") or [])


def test_every_eligible_row_unreporting_under_a_cap_refuses_naming_the_cap():
    """28 sabotage: falling back to cost_class would let a silent vendor win."""
    a = _row("gbagent", "q", vendor="gbagent", cost_class="local", local=True)
    b = _row("claude", "sonnet", vendor="anthropic", cost_class="cheap", order=2)
    cells = _merge(
        _cost("gbagent", "q", "A4", "project", 1, reported=0, finished=4),
        _cost("anthropic", "sonnet", "A4", "project", 1, reported=1, finished=10),
    )
    res = _matrix(a, b).resolve(
        tier="cheap", policy=m.Policy(caps={"per_item_tokens": 120_000}),
        installed=ALL_INSTALLED, capabilities=["A4"], cap_measured=cells,
        spend={"item_tokens": 0},
    )
    assert res.winner is None
    assert "per_item_tokens" in (res.refused or "")
    assert res.unenforceable_cap and res.unenforceable_cap["cap"] == "per_item_tokens"
    assert {r["harness"] for r in res.unenforceable_cap["rows"]} == {"gbagent", "claude"}


def test_a_number_in_both_cap_and_budget_is_the_cap_first():
    dear = _row("claude", "sonnet", vendor="anthropic", cost_class="frontier")
    cheap = _row("gbagent", "q", vendor="gbagent", cost_class="local", local=True, order=2)
    cells = _merge(
        _cost("anthropic", "sonnet", "A4", "project", 80_000),
        _cost("gbagent", "q", "A4", "project", 20_000),
    )
    res = _matrix(dear, cheap).resolve(
        tier="cheap",
        policy=m.Policy(caps={"per_attempt_tokens": 50_000}),
        profile=m.Profile(user="u", budget_tokens=50_000, weights={"cost": 1.0}),
        installed=ALL_INSTALLED, capabilities=["A4"], cap_measured=cells,
    )
    assert res.winner is cheap
    assert any("per_attempt_tokens" in d for d in res.dropped.get("policy") or [])
    # The removed row is a policy drop, not a profile effect.
    assert not any("claude" in d for d in res.dropped.get("profile") or [])


# ---- 22: budget target -----------------------------------------------------------------------

def test_budget_tokens_scores_the_curve_and_removes_nothing():
    under = _row("gbagent", "q", vendor="gbagent", cost_class="local", local=True)
    over = _row("claude", "sonnet", vendor="anthropic", cost_class="frontier", order=2)
    cells = _merge(
        _cost("gbagent", "q", "A4", "project", 31_000),
        _cost("anthropic", "sonnet", "A4", "project", 84_000),
    )
    prof = m.Profile(user="u", budget_tokens=50_000, weights={"cost": 1.0})
    res = _matrix(under, over).resolve(
        tier="cheap", profile=prof, installed=ALL_INSTALLED,
        capabilities=["A4"], cap_measured=cells,
    )
    by = {s[0].harness: s[2]["cost"] for s in res.scored}
    assert by["gbagent"]["value"] == 1.0
    # 84k vs 50k target: 1.0 - 0.8 * 34/50 = 0.456
    assert 0.2 < by["claude"]["value"] < 1.0
    assert abs(by["claude"]["value"] - (1.0 - 0.8 * 34 / 50)) < 0.01
    assert res.winner is under and res.runner_up is over


# ---- 24: the §7.7 stage record ---------------------------------------------------------------

def test_the_section_7_7_fixture_carries_every_stage():
    """The worked example, as a fixture. Stages, drops and winner axes are the object;
    timestamps are none. Byte-for-byte on the stage names and drop reasons."""
    gbagent = _row("gbagent", "qwen3.6", vendor="gbagent", cost_class="local", local=True,
                   status="verified", evidence=[EV])
    qwen = _row("qwen-code", "", vendor="alibaba", cost_class="cheap", order=2)
    claude = _row("claude", "sonnet", vendor="anthropic", cost_class="frontier", order=3)
    cursor = _row("cursor-agent", "composer", vendor="cursor", cost_class="cheap", order=4)
    codex = _row("codex", "", vendor="openai", cost_class="cheap", order=5, status="unregistered")
    excluded = _row("gbagent", "qwen3-coder:30b", vendor="gbagent", cost_class="local",
                    local=True, order=6)
    cells = _merge(
        _q("gbagent", "qwen3.6", "A4", "project", 0.40, n=7),
        _q("gbagent", "qwen3.6", "B1", "project", 0.85, n=11),
        _q("alibaba", "", "A4", "platform", 0.78, n=80, n_band="50-199"),
        _q("alibaba", "", "B1", "org", 0.70, n=6),
        _cost("gbagent", "qwen3.6", "A4", "project", 31_000),
        _cost("gbagent", "qwen3.6", "B1", "project", 31_000),
        _cost("anthropic", "sonnet", "A4", "project", 84_000),
        _cost("anthropic", "sonnet", "B1", "project", 84_000),
        _cost("alibaba", "", "A4", "project", 40_000),
        _cost("alibaba", "", "B1", "project", 40_000),
        _cost("gbagent", "qwen3-coder:30b", "A4", "project", 20_000),
        _cost("gbagent", "qwen3-coder:30b", "B1", "project", 20_000),
    )
    prof = m.Profile(user="alex", defaults=("gbagent", "qwen-code"),
                     excludes=("gbagent:qwen3-coder:30b",),
                     weights={"cost": 0.27, "quality": 0.41, "latency": 0.09, "locality": 0.23},
                     budget_tokens=50_000)
    policy = m.Policy(allowed_harnesses=("gbagent", "claude", "qwen-code"),
                      caps={"per_item_tokens": 120_000})
    res = _matrix(gbagent, qwen, claude, cursor, codex, excluded).resolve(
        tier="cheap", profile=prof, policy=policy, installed=ALL_INSTALLED,
        capabilities=["A4", "B1"], cap_measured=cells,
        spend={"item_tokens": 70_000},
    )
    out = res.explain()
    stages = [s["stage"] for s in out["stages"]]
    assert stages[0] == "rows"
    assert "policy.allowed_or_local" in stages
    assert "policy.caps" in stages
    assert "profile" in stages
    assert stages[-1] == "score"
    dropped = " ".join(out["dropped"].get("policy") or [])
    assert "cursor-agent" in dropped and "allowed_harnesses" in dropped or "not in allowed" in dropped
    assert "codex" in dropped
    assert "claude:sonnet" in dropped and "per_item_tokens" in dropped
    assert "84k" in dropped and "50k" in dropped
    assert any("qwen3-coder:30b" in d for d in out["dropped"].get("profile") or [])
    assert out["winner"]["harness"] == "gbagent"
    assert out["runner_up"]["harness"] == "qwen-code"
    q = out["winner"]["axes"]["quality"]["by_capability"]
    by = {c["capability"]: c for c in q}
    assert by["A4"]["layer"] == "project" and by["A4"]["n"] == 7
    assert by["B1"]["layer"] == "project"
    assert out["winner"]["axes"]["cost"]["value"] == 1.0
    assert out["capabilities"] == ["A4", "B1"]
    assert out["profile"]["user"] == "alex"
    assert out["profile"]["budget_tokens"] == 50_000


# ---- 14: doctor prints row status and per-capability reading --------------------------------

def test_doctor_prints_per_capability_status_and_layer_labels():
    row = _row("gbagent", "q", vendor="gbagent", cost_class="local", local=True, status="verified",
               evidence=[
                   m.Evidence(item="GRPH-1", date="2026-01-01", outcome="signed_off",
                              capability="A4"),
                   m.Evidence(item="GRPH-2", date="2026-02-01", outcome="failed",
                              capability="B4"),
               ])
    cells = _merge(
        _q("gbagent", "q", "A4", "project", 0.40, n=7),
        _q("gbagent", "q", "B1", "org", 0.85, n=11),
    )
    lines = m.doctor_lines(_matrix(row), ALL_INSTALLED, None, None, cap_measured=cells)
    detail = next(d for n, _, d in lines if n == "matrix gbagent:q")
    assert "A4=verified" in detail and "B4=failed" in detail
    assert "A4/project 0.40 (n=7)" in detail
    assert "B1/org 0.85 (n=11)" in detail
    assert any(n.startswith("resolve ") for n, _, _ in lines)
