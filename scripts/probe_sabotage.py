#!/usr/bin/env python3
"""Measure a sabotage instead of believing it, and attest what was OBSERVED (GRPH-566).

`has_effective_sabotage` requires `tests_failed > 0` — but the agent supplies `tests_failed`.
PRD-26 §Mutation probe: *"The strongest gate in the tree still ends in a self-report."* This
applies the named mutation, runs the suite, and records the failure count it actually saw.

**Out of band, and it pushes.** Per §The interface contract, Graphban never calls out: this is
a script that writes an `attestation` in one ordinary authenticated call, exactly as
`scripts/attest_ci.py` does. It never runs inside `update_item`. That placement is not
incidental — it is what puts the probe's execution environment outside the reach of the agent
being gated, and *a probe the gated party can influence measures nothing.*

**A zero is never reported as a clean run.** Every way of arriving at "nothing failed" that is
not a measurement is a REFUSAL instead, because the failure this closes is a confident zero:

  - the mutation did not land, so the suite measured the unmutated tree;
  - it landed more than once, so the suite measured something the receipt does not name;
  - `old == new`, which "passes" while changing nothing;
  - the baseline was already red, so a count means nothing;
  - no tests ran at all, which pytest reports in a line that reads much like success.

Each of those is a way I have personally produced a clean-looking result from a run that
proved nothing, which is why they are refusals rather than warnings.

    GRAPHBAN_URL=... GRAPHBAN_GATE_KEY=... python scripts/probe_sabotage.py \\
        --item GRPH-123 --commit "$SHA" \\
        --file backend/app/services/items.py \\
        --old 'if quiet < limits.disowned_after:' --new 'if False:'
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import shlex
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass

# `passed`/`failed` counts out of pytest's summary line. Anchored on the digits so
# "1 failed, 41 passed" and "42 passed" both read, and so prose mentioning the word
# elsewhere in the output cannot contribute.
_FAILED = re.compile(r"(\d+) failed")
_PASSED = re.compile(r"(\d+) passed")
_ERRORS = re.compile(r"(\d+) errors?")

#: Read from the repository's own declaration rather than guessed. Guessing a test command is
#: the same class of mistake as guessing a vendor flag, and this repo already refuses it.
CONFIG_NAME = ".gbagent.toml"


class ProbeRefused(RuntimeError):
    """The probe cannot produce a measurement, so it produces nothing.

    Distinct from "the mutation broke nothing", which is a RESULT and gets attested as a
    failed predicate. This is "I did not measure anything", and attesting it either way
    would be the self-report the probe replaces.
    """


@dataclass(frozen=True)
class Observation:
    """What was seen, not what was claimed."""

    file: str
    old: str
    new: str
    landed: int
    baseline_failed: int
    observed_failed: int
    restored_clean: bool

    @property
    def effective(self) -> bool:
        """Did the mutation actually break something?

        The whole point of the exercise. `False` here is a legitimate, attestable result —
        the sabotage was a no-op against this suite — and is exactly the case a self-reported
        `tests_failed` cannot be trusted on.
        """
        return self.observed_failed > 0


def check_mutation(text: str, old: str, new: str) -> int:
    """How many times the mutation lands, refusing every count that is not one.

    Zero is the dangerous one: the suite then runs against an unmutated tree and reports a
    clean pass that reads exactly like a surviving mutation. More than one is quieter but no
    better — the receipt names one change and the measurement covers several.
    """
    if old == new:
        raise ProbeRefused(
            "the mutation is a no-op: `old` and `new` are identical, so this would report a "
            "failure count for a tree nobody changed"
        )
    if not old:
        raise ProbeRefused("`old` is empty, so there is nothing to replace")
    landed = text.count(old)
    if landed != 1:
        raise ProbeRefused(
            f"the mutation would land {landed} times, not once — "
            + ("it does not appear in the file at all, so the suite would measure the "
               "UNMUTATED tree and report a clean run"
               if landed == 0 else
               "so the measurement would cover more than the receipt names")
        )
    return landed


def observed_failures(output: str) -> int:
    """Failures in a pytest run, refusing anything that is not a countable result.

    `0` is only ever returned for a run that demonstrably executed tests and none failed.
    A run that collected nothing prints `no tests ran`, which is not a pass — and treating
    it as zero failures is how a mutation gets recorded as ineffective when the truth is
    that the suite never looked at it.
    """
    failed = _FAILED.search(output)
    passed = _PASSED.search(output)
    errors = _ERRORS.search(output)
    if failed is None and passed is None:
        raise ProbeRefused(
            "the suite reported neither passes nor failures, so nothing was measured — "
            f"last line was {output.strip().splitlines()[-1] if output.strip() else '(empty)'!r}"
        )
    count = int(failed.group(1)) if failed else 0
    # Errors are collection or fixture faults, not verdicts on the mutation. Counting them as
    # failures would let a broken fixture masquerade as an effective sabotage.
    if errors and not failed:
        raise ProbeRefused(
            f"the suite produced {errors.group(1)} error(s) and no failures — errors are "
            "collection or fixture faults, and counting them as a verdict on the mutation "
            "would let a broken fixture read as an effective sabotage"
        )
    return count


def _clear_bytecode(root: pathlib.Path) -> None:
    """Stale `.pyc` compiled from mutated source outlives a restore and keeps executing.

    Recorded twice in this repository, in both directions: a correct-looking pass and a
    sabotage that appeared to survive. `inspect.getsource` prints the right code while the
    interpreter runs the wrong one, so it cannot be caught by reading.
    """
    for cache in root.rglob("__pycache__"):
        for pyc in cache.glob("*.pyc"):
            pyc.unlink(missing_ok=True)


def run_suite(command: str, cwd: pathlib.Path, *, timeout: float = 1800.0) -> str:
    """Run the declared verification command and return its output."""
    proc = subprocess.run(shlex.split(command), cwd=str(cwd), capture_output=True,
                          text=True, timeout=timeout)
    return proc.stdout + proc.stderr


def declared_command(root: pathlib.Path) -> tuple[str, pathlib.Path]:
    """The repository's own `[tests]` declaration, never a guess."""
    import tomllib

    path = root / CONFIG_NAME
    if not path.exists():
        raise ProbeRefused(
            f"no {CONFIG_NAME} in {root} and no --tests given, so there is no declared way to "
            "verify this repository — guessing one would measure the wrong suite"
        )
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    tests = data.get("tests") or {}
    command = str(tests.get("command") or "").strip()
    if not command:
        raise ProbeRefused(f"{CONFIG_NAME} declares no [tests].command")
    return command, root / str(tests.get("cwd") or ".")


def probe(root: pathlib.Path, *, file: str, old: str, new: str,
          command: str, cwd: pathlib.Path, runner=run_suite) -> Observation:
    """Apply, measure, restore, and re-verify — refusing anything that is not a measurement.

    The order matters. The baseline runs FIRST, because a failure count taken against an
    already-red suite says nothing about the mutation. The restore is verified LAST, because
    a mutation left in the tree poisons every run after this one, and the poisoning presents
    as somebody else's regression.
    """
    target = root / file
    if not target.exists():
        raise ProbeRefused(f"{file} does not exist under {root}")

    original = target.read_text(encoding="utf-8")
    landed = check_mutation(original, old, new)

    _clear_bytecode(root)
    baseline_failed = observed_failures(runner(command, cwd))
    if baseline_failed:
        raise ProbeRefused(
            f"the baseline is already red ({baseline_failed} failing) — a mutation measured "
            "against it cannot be attributed to the mutation"
        )

    try:
        target.write_text(original.replace(old, new), encoding="utf-8")
        _clear_bytecode(root)
        observed = observed_failures(runner(command, cwd))
    finally:
        target.write_text(original, encoding="utf-8")
        _clear_bytecode(root)

    restored_clean = target.read_text(encoding="utf-8") == original
    if not restored_clean:
        raise ProbeRefused(f"{file} was not restored — refusing to attest from a dirty tree")

    return Observation(file=file, old=old, new=new, landed=landed,
                       baseline_failed=baseline_failed, observed_failed=observed,
                       restored_clean=restored_clean)


def attestation(obs: Observation, *, commit: str, run_ref: str = "") -> dict:
    """The receipt. One predicate, named for what was measured rather than what was hoped.

    `passed` is the OBSERVATION, not a verdict on the agent: a mutation that broke nothing is
    attested as `passed: false` and stays on the record. Omitting it instead would leave the
    item looking un-probed, which is the state a self-report already produces.
    """
    detail = (f"{obs.file}: mutation landed {obs.landed}x against a green baseline; "
              f"{obs.observed_failed} test(s) failed with it applied")
    return {
        "kind": "attestation",
        "adapter": "mutation-probe",
        "commit": commit,
        "run_ref": run_ref,
        "predicates": [{
            "name": "sabotage_observed",
            "passed": obs.effective,
            "detail": detail if obs.effective else detail + " — the mutation broke NOTHING",
        }],
    }


def post(url: str, key: str, item_key: str, receipt: dict, *, timeout: float = 15.0) -> None:
    """Attach the receipt to one item. Raises on anything that is not a success."""
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": "update_item", "arguments": {"id": item_key, "evidence": [receipt]},
    }}).encode()
    req = urllib.request.Request(
        url.rstrip("/") + "/api/mcp", data=body,
        headers={"Content-Type": "application/json", "X-API-Key": key})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode())
    result = payload.get("result") or {}
    if result.get("isError") or "error" in payload:
        detail = (result.get("structuredContent") or {}).get("error") or payload.get("error")
        raise RuntimeError(f"{item_key}: graphban refused the write: {detail}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Measure a sabotage and attest what was observed.")
    ap.add_argument("--item", required=True)
    ap.add_argument("--commit", required=True)
    ap.add_argument("--file", required=True, help="path, relative to --root")
    ap.add_argument("--old", required=True)
    ap.add_argument("--new", required=True)
    ap.add_argument("--root", default=".")
    ap.add_argument("--tests", default="", help=f"defaults to [tests] in {CONFIG_NAME}")
    ap.add_argument("--run-ref", default="")
    args = ap.parse_args(argv)

    root = pathlib.Path(args.root).resolve()
    try:
        if args.tests:
            command, cwd = args.tests, root
        else:
            command, cwd = declared_command(root)
        obs = probe(root, file=args.file, old=args.old, new=args.new,
                    command=command, cwd=cwd)
    except ProbeRefused as exc:
        # Exit 2, distinct from a measured no-op (which is exit 0 and an attestation saying
        # so). An operator must be able to tell "I could not measure" from "I measured
        # nothing" — collapsing them is the defect this whole probe exists to remove.
        print(f"probe refused: {exc}", file=sys.stderr)
        return 2

    receipt = attestation(obs, commit=args.commit, run_ref=args.run_ref)
    print(json.dumps(receipt, indent=2))

    url = os.environ.get("GRAPHBAN_URL", "").strip()
    key = os.environ.get("GRAPHBAN_GATE_KEY", "").strip()
    if not url or not key:
        missing = " and ".join(n for n, v in
                               (("GRAPHBAN_URL", url), ("GRAPHBAN_GATE_KEY", key)) if not v)
        print(f"{missing} is not set — measured, but attested nothing", file=sys.stderr)
        return 0

    try:
        post(url, key, args.item, receipt)
    except (urllib.error.URLError, RuntimeError, ValueError) as exc:
        print(f"attestation failed: {exc}", file=sys.stderr)
        return 1
    print(f"attested {args.item}: sabotage_observed={obs.effective} "
          f"({obs.observed_failed} failed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
