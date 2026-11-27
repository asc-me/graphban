"""Every blocker gbfleet writes passes the server's real blocker rule (GRPH-1969).

GRPH-1960 refuses an `update_item(blocker=...)` that names nothing it waits on. GRPH-1965
made the supervisor's merge-did-not-land blocker start with `MERGE_BLOCKER_PREFIX` so the
rule accepts it. The fleet suite can only check that against a COPY of the server's regexes
(`fleet/` cannot import `backend`), so a change to `_PERSON_OR_DECISION` or `_GBFLEET_HOLD`
here would leave both suites green while the fleet's write is refused again.

This side can see both. It reads gbfleet's source as TEXT (the backend venv does not install
`gbfleet`) and runs the real `refuse_unnamed_blocker` on what it finds. Nothing falls back to a
default string: a missing file, a renamed constant, or a new blocker writer fails the test.

In scope: every `blocker=` keyword and `"blocker":` dict entry in `fleet/src/gbfleet/*.py`.
Empty strings are clears, not blockers, and are skipped. `OpenItem(blocker=...)` builds a
read model from a row the server already holds, so it writes nothing and is skipped.
"""
from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.errors import Refused
from app.services.items import blocker_names_person_or_decision, refuse_unnamed_blocker

FLEET = Path(__file__).resolve().parents[2] / "fleet" / "src" / "gbfleet"

#: Every non-empty blocker gbfleet writes, as (module, source of the written expression).
#: A new writer turns `test_every_blocker_writer_is_known` red until it is added here AND
#: given a marker check below — that is the point.
KNOWN_WRITERS = {
    ("supervisor.py", "merge_blocker(reason)"),
    ("until.py", "external_hold_text(checks)"),
    ("until.py", "text"),
    ("until.py", "f'planner decision: duplicate or superseded — {detail}'"),
}


def _tree(name: str) -> ast.Module:
    path = FLEET / name
    if not path.is_file():
        pytest.fail(f"{path} is missing: the fleet/backend blocker contract cannot be checked")
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _module_str(name: str, const: str) -> str:
    """The string a module-level `const = "..."` assigns. Fails if it is not there."""
    for node in _tree(name).body:
        targets = (node.targets if isinstance(node, ast.Assign)
                   else [node.target] if isinstance(node, ast.AnnAssign) else [])
        if any(isinstance(t, ast.Name) and t.id == const for t in targets):
            value = node.value
            if isinstance(value, ast.Constant) and isinstance(value.value, str) and value.value:
                return value.value
            pytest.fail(f"{name}: {const} is no longer a non-empty string literal")
    pytest.fail(f"{name} no longer assigns {const} at module level")


def _function(name: str, func: str) -> ast.FunctionDef:
    for node in ast.walk(_tree(name)):
        if isinstance(node, ast.FunctionDef) and node.name == func:
            return node
    pytest.fail(f"{name} no longer defines {func}")


def _returns_use(fn: ast.FunctionDef, const: str) -> bool:
    """Every `return` in `fn` builds its value from `const`."""
    returns = [n for n in ast.walk(fn) if isinstance(n, ast.Return) and n.value is not None]
    return bool(returns) and all(
        any(isinstance(n, ast.Name) and n.id == const for n in ast.walk(r.value))
        for r in returns)


def _writers() -> list[tuple[str, ast.expr, ast.FunctionDef | None]]:
    """(module, written expression, innermost enclosing function) for each blocker write."""
    found = []
    for path in sorted(FLEET.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        owner: dict[ast.AST, ast.FunctionDef] = {}
        for fn in ast.walk(tree):  # outer functions first, so inner ones overwrite
            if isinstance(fn, ast.FunctionDef):
                for child in ast.walk(fn):
                    owner[child] = fn
        for node in ast.walk(tree):
            values = []
            if isinstance(node, ast.Call):
                if ast.unparse(node.func).endswith("OpenItem"):
                    continue  # a read model, not a write
                values = [k.value for k in node.keywords if k.arg == "blocker"]
            elif isinstance(node, ast.Dict):
                values = [v for k, v in zip(node.keys, node.values)
                          if isinstance(k, ast.Constant) and k.value == "blocker"]
            for v in values:
                if isinstance(v, ast.Constant) and v.value == "":
                    continue  # a clear
                found.append((path.name, v, owner.get(node)))
    if not found:
        pytest.fail(f"no blocker writers found under {FLEET}: the scan is broken")
    return found


def _accepted(text: str) -> bool:
    """The server's real rule, on an item with no wait tag. Refused → False."""
    item = SimpleNamespace(id=-1, key="GRPH-0", tags=[])
    try:
        refuse_unnamed_blocker(None, item, text, tags=[])
    except Refused:
        return False
    return True


def test_the_rule_still_refuses_a_bare_reason():
    """Non-vacuity: the predicate this file relies on can say no."""
    assert not _accepted("rebase conflict in backend/app/services/prds.py")


def test_every_blocker_writer_is_known():
    seen = {(name, ast.unparse(v)) for name, v, _ in _writers()}
    assert seen == KNOWN_WRITERS, (
        "gbfleet's blocker writers changed. Add the new one to KNOWN_WRITERS and give it a "
        f"check in this file. new={seen - KNOWN_WRITERS} gone={KNOWN_WRITERS - seen}")


def test_merge_blocker_prefix_names_a_person():
    prefix = _module_str("supervisor.py", "MERGE_BLOCKER_PREFIX")
    assert blocker_names_person_or_decision(prefix), prefix
    assert _accepted(prefix)
    assert _returns_use(_function("supervisor.py", "merge_blocker"), "MERGE_BLOCKER_PREFIX")


def test_dependency_hold_mark_is_a_gbfleet_hold():
    mark = _module_str("deps.py", "HOLD_MARK")
    assert _accepted(mark), mark
    assert _returns_use(_function("deps.py", "hold_text"), "HOLD_MARK")
    # Both `blocker=text` writers in until.py build `text` from the dependency hold.
    texts = [fn for name, v, fn in _writers() if (name, ast.unparse(v)) == ("until.py", "text")]
    assert len(texts) == 2, texts
    for fn in texts:
        assert fn is not None
        built = [n.value for n in ast.walk(fn) if isinstance(n, ast.Assign)
                 and any(isinstance(t, ast.Name) and t.id == "text" for t in n.targets)]
        assert built, fn.name
        for value in built:
            src = ast.unparse(value)
            assert "hold_text(" in src or "HOLD_MARK" in src, (fn.name, src)


def test_external_check_hold_mark_is_a_gbfleet_hold():
    mark = _module_str("until.py", "EXTERNAL_HOLD_MARK")
    assert _accepted(mark), mark
    assert _returns_use(_function("until.py", "external_hold_text"), "EXTERNAL_HOLD_MARK")


def test_planner_decision_blocker_names_a_decision():
    [value] = [v for name, v, _ in _writers() if name == "until.py"
               and isinstance(v, ast.JoinedStr)]
    head = value.values[0]
    assert isinstance(head, ast.Constant) and isinstance(head.value, str)
    assert _accepted(head.value), head.value
