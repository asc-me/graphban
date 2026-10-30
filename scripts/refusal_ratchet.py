#!/usr/bin/env python3
"""Refusal ratchets and coverage (GRPH-1158 / PRD-P61 §6).

Two counts, computed by an AST scan of `backend/app`, and both must not go up:

  (a) `Refused` sites WITHOUT a `rule_id` — a refusal that names no rule;
  (b) policy refusals written outside the `Refused` type, which is how a new refusal would
      dodge (a) and the coverage line: `HTTPException` raises with status 403 or 409, and
      raises of `errors.Conflict` / `authz.Forbidden` or a class derived from them
      (`PrdClosed`, `MissingAttestation`, `DelegationRefused`...), which the MCP dispatcher
      maps to `conflict` / `unauthorized` with no `rule_id` (GRPH-1197).

A site is every `Refused(...)` construction, raised or handed to the MCP refusal path. Coverage
is "N of M refusal sites name their rule": M is every site, N those whose `rule_id` resolves
to a string. Ratchet (b) is what stops a refusal being written as a bare 403/409 to stay out
of M.

`rule_id` may be a literal or a module-level string constant (`rule_id=OBSERVE_RULE`,
`gitops.OBSERVE_RULE`); the scan resolves both, so the registry test checks the VALUE a
constant holds, not only literals. A `rule_id` the scan cannot resolve is listed under
`unresolved` and does not count as naming a rule.

    refusal_ratchet.py check                       exit 1 if a count went up (every build)
    refusal_ratchet.py publish [OUT.json] [SHA]    write the numbers (a package build ONLY)

`publish` re-runs `check` first and writes nothing on failure. It scans the tree this script
sits in, and is called by what builds the thing we ship (GRPH-1185): `graphban_pack.py` runs
the PACKED tree's copy into `backend/app/data/refusal_coverage.json` and refuses the cut if it
fails, so a failing tree can never produce a number. The file is a build artefact — gitignored,
never committed (main's branch protection rejects a CI push anyway) — and
`services/rules.refusal_coverage` reads it from package data. Lower a BASELINE in the PR that
lowers the count; raising one is the review comment.
"""
from __future__ import annotations

import ast
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "backend" / "app"
PUBLISHED = ROOT / "data" / "refusal_coverage.json"

#: The most recent counts. Ratchets: a PR may lower these, never raise them.
BASELINE = {"refused_without_rule_id": 0, "bare_403_409": 109}

_POLICY_STATUS = (403, 409)
_POLICY_BASES = {"Conflict", "Forbidden"}


def _policy_classes(trees: dict[Path, ast.Module]) -> set[str]:
    """`Conflict`, `Forbidden` and every class derived from them, to a fixed point."""
    names = set(_POLICY_BASES)
    grew = True
    while grew:
        grew = False
        for tree in trees.values():
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef) and node.name not in names and any(
                        (b.id if isinstance(b, ast.Name) else getattr(b, "attr", "")) in names
                        for b in node.bases):
                    names.add(node.name)
                    grew = True
    return names


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


def _constants(tree: ast.Module) -> dict[str, str]:
    """Module-level `NAME = "string"` assignments."""
    out = {}
    for node in tree.body:
        targets, value = [], None
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            out.update({t.id: value.value for t in targets if isinstance(t, ast.Name)})
    return out


class _Resolver:
    """A rule_id expression -> its string, or None (no rule) / ... (cannot tell)."""

    def __init__(self, trees: dict[Path, ast.Module]):
        self.local = {p: _constants(t) for p, t in trees.items()}
        everywhere: dict[str, set[str]] = {}
        for consts in self.local.values():
            for k, v in consts.items():
                everywhere.setdefault(k, set()).add(v)
        # Only a name with one value across the tree resolves from another module.
        self.shared = {k: next(iter(v)) for k, v in everywhere.items() if len(v) == 1}

    def __call__(self, path: Path, node: ast.expr):
        if isinstance(node, ast.Constant):
            return node.value if isinstance(node.value, str) and node.value else None
        if isinstance(node, ast.Name):
            return self.local[path].get(node.id) or self.shared.get(node.id, ...)
        if isinstance(node, ast.Attribute):  # gitops.OBSERVE_RULE
            return self.shared.get(node.attr, ...)
        return ...


def scan(root: Path = ROOT) -> dict:
    trees = {p: ast.parse(p.read_text(), str(p)) for p in sorted(root.rglob("*.py"))}
    resolve = _Resolver(trees)
    policy = _policy_classes(trees)
    refused = mapped = bare = 0
    rule_ids: set[str] = set()
    unresolved: list[str] = []
    for path, tree in trees.items():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _name(node) == "Refused":
                refused += 1
                kw = next((k for k in node.keywords if k.arg == "rule_id"), None)
                value = None if kw is None else resolve(path, kw.value)
                if value is ...:
                    unresolved.append(f"{path.relative_to(root)}:{node.lineno}")
                elif value:
                    mapped += 1
                    rule_ids.add(value)
            elif isinstance(node, ast.Dict):
                # An event's meta naming its rule (`agent_reject_shard`). A pass-through value
                # (`exc.rule_id`) is not a citation, so only what resolves is collected.
                for k, v in zip(node.keys, node.values):
                    if isinstance(k, ast.Constant) and k.value == "rule_id":
                        value = resolve(path, v)
                        if isinstance(value, str):
                            rule_ids.add(value)
            elif (isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call)
                  and _name(node.exc) == "HTTPException"
                  and _status_of(node.exc) in _POLICY_STATUS):
                bare += 1
            elif (isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call)
                  and _name(node.exc) in policy):
                bare += 1
    return {"refusal_sites": refused, "naming_a_rule": mapped,
            "refused_without_rule_id": refused - mapped, "bare_403_409": bare,
            "rule_ids": sorted(rule_ids), "unresolved": unresolved}


def check(counts: dict) -> list[str]:
    return [f"{k} is {counts[k]}, baseline {v}: the ratchet only goes down"
            for k, v in BASELINE.items() if counts[k] > v]


def main(argv: list[str]) -> int:
    counts = scan()
    problems = check(counts)
    print(f"{counts['naming_a_rule']} of {counts['refusal_sites']} refusal sites name their "
          f"rule; {counts['bare_403_409']} bare 403/409 policy raises")
    for p in problems:
        print("FAIL:", p, file=sys.stderr)
    if problems:
        return 1
    if argv[:1] == ["publish"]:
        if len(argv) > 3:
            print("usage: refusal_ratchet.py publish [OUT.json] [SHA]", file=sys.stderr)
            return 2
        out = Path(argv[1]) if len(argv) > 1 else PUBLISHED
        sha = argv[2] if len(argv) > 2 else os.environ.get("GITHUB_SHA", "")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({
            "n": counts["naming_a_rule"], "m": counts["refusal_sites"],
            "bare_403_409": counts["bare_403_409"], "commit": sha,
            "published_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
