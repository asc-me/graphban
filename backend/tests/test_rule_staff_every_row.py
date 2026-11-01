"""PRD-P63 §6 primitive 6/7 (GRPH-1273): `review.staff_every_row`, the `staffing_scope`
primitive. `checked`, not `enforced`: the server refuses nothing; `gbfleet until` honours it."""
from __future__ import annotations

import shutil
from pathlib import Path

from app.services import rule_pack as P
from app.services import rule_toggles as T

from tests.test_rule_pack import TRUST, build

FIX = Path(__file__).parent / "fixtures" / "rule_pack"
RULE = "review.staff_every_row"


def _load(path):
    saved = (dict(T.LIBRARY), P.CURRENT)
    try:
        return P.load(*build(path), trusted_keys=TRUST), P
    finally:
        T.LIBRARY.clear()
        T.LIBRARY.update(saved[0])
        P.CURRENT = saved[1]


def test_the_primitive_is_published_checked():
    prim = P.PRIMITIVES["staffing_scope"]
    assert prim.level == "checked"
    assert prim.enforcer.kind == "supervisor"


def test_the_fixture_template_loads_checked():
    pack, _ = _load(FIX / "api1")
    assert RULE not in pack.refused
    t = pack.templates[RULE]
    assert t["primitive"] == "staffing_scope" and t["claims"] == "checked"
    rule = P._library_rule(RULE, t, "")
    assert rule.enforcement == "checked" and rule.default_on is False


def test_a_template_claiming_enforced_is_refused(tmp_path):
    dst = tmp_path / "api1"
    shutil.copytree(FIX / "api1", dst)
    f = dst / "templates" / "staff_every_row.toml"
    f.write_text(f.read_text().replace('claims = "checked"', 'claims = "enforced"'))
    pack, _ = _load(dst)
    assert "only provides 'checked'" in pack.refused[RULE]
