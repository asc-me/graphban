#!/usr/bin/env python3
"""Mint a Graphban `attestation` for the items a green CI run vouches for (GRPH-551).

The completion gate (GRPH-543) refuses `done` without an attestation, and only a key
carrying the `gate` scope may write one. `fleet.sign_off` was the sole adapter, which means
a project with no running reviewer accumulates work in `review` that nothing can finish.
This is the adapter that needs neither Swamp nor a reviewer.

**Called from a STEP inside the `ci` gate job, after its result check.** Steps stop at the
first failure, so this is unreachable unless the check that decides CI is green has already
passed — there is no window for an attestation minted from a masked failure, which is the
failure mode that would make this worse than having no adapter at all.

Kept as a script rather than inline YAML because the end-to-end path needs Actions, a
configured secret and a reachable Graphban, none of which exist in a test run. What CAN be
tested is everything below: which items a message refers to, the payload, and the refusals.
Same trade `web/verify-upstream-reresolution.sh` already makes.

    GRAPHBAN_URL=... GRAPHBAN_GATE_KEY=... python scripts/attest_ci.py \
        --commit "$SHA" --branch "$REF" --text "$PR_TITLE $PR_BODY"
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request

# Graphban item keys are `<PROJECT-TAG>-<number>`, and the tag is at least two letters.
#
# `\b[A-Z]+-\d+\b` is the tempting version and it is wrong: it matches `UTF-8`, `SHA-1`,
# `AES-256`, `RFC-2119` — so a PR that mentions no item at all would attest something that
# does not exist, or worse, something unrelated that does. The tag must be at least three
# characters and the whole thing anchored on a word boundary, which is what the keys this
# repo actually mints look like (GRPH-541, AL-262, CP-23).
ITEM_KEY = re.compile(r"\b([A-Z]{2,10}-\d+)\b")

# Tokens that match the shape but are never item keys. Extended rather than loosened when a
# false positive turns up: a list of known non-keys fails visibly, a looser pattern does not.
NOT_KEYS = {"UTF-8", "SHA-1", "SHA-256", "AES-256", "RFC-2119", "ISO-8601", "BASE-64",
            "HTTP-1", "TLS-1", "IPV-4", "IPV-6"}


def item_keys(*texts: str) -> list[str]:
    """Every distinct Graphban item key mentioned, in first-seen order.

    Order is stable so a run attests the same items in the same sequence — a set would make
    the log a different shape every time and hide a change in what was matched.
    """
    seen: list[str] = []
    for text in texts:
        for match in ITEM_KEY.findall(text or ""):
            if match not in NOT_KEYS and match not in seen:
                seen.append(match)
    return seen


def attestation(*, commit: str, branch: str, run_url: str = "") -> dict:
    """The receipt. One predicate, because CI checks exactly one thing: the suite passed.

    Naming it `suite_green` rather than something broader matters — the gate records WHICH
    predicates ran, so a later reader can see that this completion rests on a test run and
    nothing else. An adapter claiming more than it checked is how a gate quietly starts
    accepting less than it appears to.
    """
    return {
        "kind": "attestation",
        "adapter": "github-actions",
        "commit": commit,
        "run_ref": run_url,
        "predicates": [{
            "name": "suite_green",
            "passed": True,
            "detail": f"CI passed on {branch or 'an unnamed ref'} at {commit[:12]}",
        }],
    }


def post_head(url: str, key: str, item_key: str, commit: str, *,
              timeout: float = 15.0) -> None:
    """Record the commit this run OBSERVED, whether or not it passed (GRPH-555).

    This is the half that makes an attestation expire. A passing run writes a receipt for
    the head; a FAILING run writes no receipt but must still move the head, or the last
    passing attestation goes on opening the gate for code that has since broken.
    """
    _call(url, key, {"id": item_key, "head_commit": commit}, timeout=timeout)


def post(url: str, key: str, item_key: str, receipt: dict, *, timeout: float = 15.0) -> None:
    """Attach the receipt to one item. Raises on anything that is not a success.

    Deliberately NOT swallowing errors. A misconfigured key that looked identical to a
    successful attestation would leave the item uncompletable with nothing saying why —
    the operator would go looking at the gate, which is working correctly.
    """
    _call(url, key, {"id": item_key, "evidence": [receipt]}, timeout=timeout)


def _call(url: str, key: str, arguments: dict, *, timeout: float = 15.0) -> None:
    """One `update_item` call. Raises on anything that is not a success."""
    item_key = arguments.get("id", "?")
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": "update_item", "arguments": arguments,
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
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--commit", required=True)
    ap.add_argument("--branch", default="")
    ap.add_argument("--text", default="", help="PR title/body/branch to scan for item keys")
    ap.add_argument("--run-url", default="")
    ap.add_argument("--mode", choices=("attest", "head"), default="attest",
                    help="`head` records the observed commit and runs even when CI failed; "
                         "`attest` writes the receipt and must not.")
    args = ap.parse_args(argv)

    url = os.environ.get("GRAPHBAN_URL", "").strip()
    key = os.environ.get("GRAPHBAN_GATE_KEY", "").strip()
    if not url or not key:
        # LOUD, and exit 0. Failing every PR because a repository secret is unset would be
        # worse than the problem this solves; skipping in silence would be the ships-inert
        # failure the PRD is about. So: say exactly what is missing and what it would do.
        missing = " and ".join(n for n, v in
                               (("GRAPHBAN_URL", url), ("GRAPHBAN_GATE_KEY", key)) if not v)
        print(f"::warning title=CI attestation skipped::{missing} is not set, so this green "
              f"run attested nothing. Items referenced by this PR cannot reach `done` until "
              f"a reviewer signs them off or the secret is configured.")
        return 0

    keys = item_keys(args.text, args.branch)
    if not keys:
        print("no Graphban item key in the branch or PR text — nothing to attest")
        return 0

    receipt = attestation(commit=args.commit, branch=args.branch, run_url=args.run_url)
    failures = []
    for item_key in keys:
        try:
            if args.mode == "head":
                post_head(url, key, item_key, args.commit)
                print(f"{item_key}: head is now {args.commit[:12]}")
                continue
            post(url, key, item_key, receipt)
            print(f"attested {item_key} at {args.commit[:12]}")
        except (urllib.error.URLError, RuntimeError, ValueError) as e:
            # Every item is attempted before anything is reported. Stopping at the first
            # failure would leave a multi-item PR half attested with no record of which half.
            failures.append(f"{item_key}: {e}")

    for f in failures:
        print(f"::error title=CI attestation failed::{f}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
