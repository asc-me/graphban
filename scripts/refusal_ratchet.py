#!/usr/bin/env python3
"""Refusal ratchets and coverage (GRPH-1158 / PRD-P61 §6).

Two counts, computed by an AST scan of `backend/app`, and both must not go up:

  (a) `Refused` raise sites WITHOUT a `rule_id` — a refusal that names no rule;
  (b) `HTTPException` raises with status 403 or 409 — a policy refusal written outside the
      `Refused` type, which is how a new refusal would dodge (a) and the coverage line.

Coverage is "N of M refusal sites name their rule": M is every `Refused` raise site, N those
carrying a `rule_id`. Ratchet (b) is what stops a refusal being written as a bare 403/409 to
stay out of M.

    refusal_ratchet.py check                 exit 1 if a count went up (every build)
    refusal_ratchet.py publish OUT.json      write the numbers (a passing build on main ONLY)

`publish` re-runs `check` first and writes nothing on failure, and CI only calls it from the
gate job on a push to main, after every suite passed: a PR build, green or red, never
publishes, so a failing tree can never produce a number. Lower a BASELINE in the PR that
lowers the count; raising one is the review comment.
"""
from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "backend" / "app"

#: The most recent counts. Ratchets: a PR may lower these, never raise them.
BASELINE = {"refused_without_rule_id": 0, "bare_403_409": 59}

_POLICY_STATUS = (403, 409)


def _status_of(call: ast.Call) -> int | None:
    node = call.args[0] if call.args else next(
        (k.value for k in call.keywords if k.arg == "status_code"), None)
    if isinstance(node, ast.Constant) and isinstance(node.value, int):
        return node.value
    if isinstance(node, ast.Attribute):  # status.HTTP_403_FORBIDDEN
        parts = node.attr.split("_")
        if len(parts) > 1 and parts[0] == "HTTP" and parts[1].isdigit():
            return int(parts[1])
    return None


def _name(call: ast.Call) -> str:
    f = call.func
    return f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""


def _has_rule(call: ast.Call) -> bool:
    for k in call.keywords:
        if k.arg == "rule_id":
            return not (isinstance(k.value, ast.Constant) and k.value.value in (None, ""))
    return False


def scan(root: Path = ROOT) -> dict:
    refused = mapped = bare = 0
    rule_ids: set[str] = set()
    for path in sorted(root.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(), str(path))):
            if not (isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call)):
                continue
            call, name = node.exc, _name(node.exc)
            if name == "Refused":
                refused += 1
                mapped += _has_rule(call)
                for k in call.keywords:
                    if k.arg == "rule_id" and isinstance(k.value, ast.Constant) and k.value.value:
                        rule_ids.add(k.value.value)
            elif name == "HTTPException" and _status_of(call) in _POLICY_STATUS:
                bare += 1
    return {"refusal_sites": refused, "naming_a_rule": mapped,
            "refused_without_rule_id": refused - mapped, "bare_403_409": bare,
            "rule_ids": sorted(rule_ids)}


def check(counts: dict) -> list[str]:
    return [f"{k} is {counts[k]}, baseline {v}: the ratchet only goes down"
            for k, v in BASELINE.items() if counts[k] > v]


def main(argv: list[str]) -> int:
    counts = scan()
    problems = check(counts)
    print(f"{counts['naming_a_rule']} of {counts['refusal_sites']} refusal sites name their "
          f"rule; {counts['bare_403_409']} bare 403/409 HTTPException raises")
    for p in problems:
        print("FAIL:", p, file=sys.stderr)
    if problems:
        return 1
    if argv[:1] == ["publish"]:
        if len(argv) != 2:
            print("usage: refusal_ratchet.py publish OUT.json", file=sys.stderr)
            return 2
        out = {"n": counts["naming_a_rule"], "m": counts["refusal_sites"]}
        Path(argv[1]).write_text(json.dumps(out, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
