"""Does every MCP tool emit the `outputSchema` it declares? (GRPH-495)

**The manifest is a contract read by something that cannot inspect the handler.** An agent
sees `outputSchema` and the tool description and has no other way to learn what comes back.
A declared-but-absent key does not error — `payload.get("graded")` returns `None`, which is
falsy — so a caller doing exactly what the description says gets a plausible wrong answer
forever, and nothing anywhere reads as missing.

That is not hypothetical. GRPH-485 shipped `graded` and `ungraded_reason` into
`answer_grill`'s schema AND into its description ("`graded: false` means the grader could not
be asked"), while the handler builds its dict by hand and returns neither. Green suite, green
CI, 23 calls in the test run, never once emitted. The only test looking at output schemas
(`test_api.py`) asserts that each one has `"type": "object"`.

WHY OBSERVED RATHER THAN STATIC. A static reading of `_call_tool` resolves 32 of 54 tools —
the rest return through service functions that build dicts from ORM rows. A ratchet that
silently skipped 22 tools would be the same defect wearing a badge, so this watches what the
suite actually sends.

WHY THE UNION OVER A RUN, NOT PER CALL. Measured before choosing: `heartbeat` legitimately
omits the whole item block on a call with nothing claimed, and `fleet_status` omits `seats`
for a caller that minted none. A per-call rule would force those to emit nulls, so the rule
is that each declared key must appear in *some* call. Two tools would fail an otherwise
tempting stricter rule, which is why it is not the rule.

FAILS CLOSED, three ways, because a probe that reports nothing must never read as a pass:
  - no probe data at all is a failure, not a skip;
  - a tool the run never exercised is a failure (on a full run), because an unexercised tool
    is exactly how a new one would sail past this;
  - the aggregate is written by workers and read by the controller, so a worker that died
    without writing takes the count down and trips the coverage check.
"""
from __future__ import annotations

import json
import os
import pathlib
import tempfile

#: Set by the controller in `pytest_configure`; xdist workers inherit it through the
#: environment when execnet spawns them, which is how the two halves find each other.
ENV_DIR = "GB_SCHEMA_PROBE_DIR"

_seen: dict[str, set[str]] = {}
_installed = False


def install() -> None:
    """Wrap `_call_tool` so every MCP tool result is recorded. Idempotent."""
    global _installed
    if _installed:
        return
    from app import mcp_server

    original = mcp_server._call_tool

    def recording(db, name, args, key, *a, **kw):
        result = original(db, name, args, key, *a, **kw)
        if isinstance(result, dict):
            # Resolve the alias exactly as `_call_tool` does one line into its own body.
            # Without this the probe files calls under the name the caller used, and a tool
            # exercised only through its old name reads as never exercised — which is what
            # the first run of this ratchet reported about `report_graphban_issue`. A guard
            # that manufactures its own findings gets switched off.
            canonical = mcp_server.TOOL_ALIASES.get(name, name)
            _seen.setdefault(canonical, set()).update(result)
        return result

    mcp_server._call_tool = recording
    _installed = True


def dump() -> None:
    """Write this process's observations. Called from each worker's session finish."""
    directory = os.environ.get(ENV_DIR)
    if not directory:
        return
    path = pathlib.Path(directory) / f"{os.getpid()}.json"
    path.write_text(json.dumps({k: sorted(v) for k, v in _seen.items()}), encoding="utf-8")


def _declared() -> dict[str, set[str]]:
    from app import mcp_server

    return {name: set((schema or {}).get("properties") or {})
            for name, schema in mcp_server._OUTPUT_SCHEMAS.items()}


def report(full_run: bool) -> list[str]:
    """Aggregate every worker's observations and return failure lines (empty = conforms)."""
    directory = os.environ.get(ENV_DIR)
    files = sorted(pathlib.Path(directory).glob("*.json")) if directory else []

    emitted: dict[str, set[str]] = {}
    for f in files:
        try:
            for name, keys in json.loads(f.read_text(encoding="utf-8")).items():
                emitted.setdefault(name, set()).update(keys)
        except (OSError, ValueError) as e:
            return [f"schema probe: could not read {f} ({e}) — treating as a failure, "
                    "because unreadable evidence is not evidence of conformance"]

    if not emitted:
        if not full_run:
            # A SELECTION that happens to call no MCP tool records nothing, and that means
            # nothing. Failing here made `pytest tests/test_prd_sync.py` exit 1 on a green
            # suite — a ratchet that cries on every subset run is one people switch off,
            # which is the failure this ratchet was written to prevent, committed by it.
            return []
        return ["schema probe: recorded nothing at all on a FULL run. Either no MCP tool ran "
                "or the probe failed to install — both are failures here, because a probe "
                "with no data would otherwise report a clean manifest."]

    declared = _declared()
    lines: list[str] = []
    for name in sorted(declared):
        if name not in emitted:
            if full_run:
                lines.append(f"  {name}: never exercised, so its schema is unverifiable. Add "
                             "a test that calls it — an unexercised tool is how a wrong "
                             "schema gets in.")
            continue
        missing = sorted(declared[name] - emitted[name])
        if missing:
            lines.append(f"  {name}: declares {', '.join(missing)} — never emitted by any of "
                         f"the {len(emitted[name])} keys seen across this run.")

    if lines:
        head = [f"outputSchema conformance failed ({len(lines)} tool(s)) — the manifest "
                "promises a field the handler does not return:"]
        return head + lines
    return []


def make_dir() -> str:
    return tempfile.mkdtemp(prefix="gb-schema-probe-")
