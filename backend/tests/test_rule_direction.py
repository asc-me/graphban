"""P91 S1: primitive stage/direction contract, the shared draft validator, `direction`."""
from app.services import rule_pack as rp


def _draft(**kw):
    d = {"id": "x.y", "stage": "Build", "title": "t", "summary": "s",
         "primitive": "generated_files", "claims": "checked"}
    d.update(kw)
    return d


def test_every_primitive_declares_stage_and_direction():
    assert len(rp.PRIMITIVES) == 16
    for p in rp.PRIMITIVES.values():
        assert p.stage in rp.STAGES and p.on_direction in rp.DIRECTIONS


def test_validator_refuses_overclaim():
    assert "only provides" in rp.validate_draft(_draft(claims="enforced"))
    t = _draft(claims="enforced")
    assert rp._check_template(t, rp.PRIMITIVE_API) == rp.validate_draft(t)


def test_validator_names_contract_stage():
    r = rp.validate_draft(_draft(stage="Plan"))
    assert "'Build'" in r and "stage" in r


def test_validator_other_refusals():
    assert "does not exist" in rp.validate_draft(_draft(primitive="nope"))
    assert "major mismatch" in rp.validate_draft(_draft(), api="2.0.0")
    assert "required param" not in rp.validate_draft(_draft())
    assert rp.validate_draft(_draft(params={"paths": ["a"]})) == ""


def test_direction_generated_files():
    d = rp.direction
    assert d("generated_files", {"paths": ["a", "b"]}, {"paths": ["a"]}) == "tighten"
    assert d("generated_files", {"paths": ["a"]}, {"paths": ["a", "b"]}) == "loosen"
    assert d("generated_files", None, {"paths": ["a"]}) == "loosen"


def test_direction_others():
    d = rp.direction
    assert d("exclusive_areas", None, {"paths": ["a"]}) == "tighten"
    assert d("exclusive_areas", {"paths": ["a"]}, {"paths": ["a", "b"]}) == "tighten"
    assert d("sabotage_receipt", {"min_effort": 3}, {"min_effort": 5}) == "tighten"
    assert d("undeclared_files", {"mode": "warn"}, {"mode": "hold"}) == "not_proposable"
    assert d("nope", None, {}) == "not_proposable"


def test_sabotage_receipt_direction_is_contract_driven():
    assert rp.PRIMITIVES["generated_files"].on_direction == "loosen"
