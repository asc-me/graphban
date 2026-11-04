#!/usr/bin/env python3
"""Write `docs/rule-primitive-contract.json` from `rule_pack.contract()` (GRPH-1303).

The rules repo's pack CI loads its templates against this file, so it is generated from the one
function and never copied by hand. `backend/tests/test_rule_pack.py` fails when the committed file
differs from `contract()`; regenerate with `scripts/gen_rule_contract.py` when a primitive changes.
"""
from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "rule-primitive-contract.json"
sys.path.insert(0, str(ROOT / "backend"))


def render() -> str:
    from app.services import rule_pack
    return json.dumps(rule_pack.contract(), indent=2, sort_keys=True) + "\n"


if __name__ == "__main__":
    OUT.write_text(render())
    print(f"wrote {OUT.relative_to(ROOT)}")
