"""What a wave spent, and the budget that ends it (GRPH-834).

Reported as O7: *"Items resolve to `claude:opus` whenever the brief says frontier, and several
did. A budget concept exists server-side — `gban fleet` preserves exit code 55 — but `until`
has no budget flag and its terminal JSON reports no spend. For unattended operation that is the
number an operator most needs and least has."*

**In tokens, not currency, and that is a decision rather than a shortcut.** PRD-41 §7 already
defines the unit of cost for this system as tokens-to-sign-off. A dollar figure would need a
price table per vendor and model, which goes stale silently — and a stale price is worse than
no price, because it is a number an operator will act on. Tokens are what the vendors actually
report and what this package can actually count.

**Two totals, never one.** A child whose vendor prints no result record contributes nothing
here, and that is not zero. `412k tokens` reads as a wave's total; it is not the total if four
of six children were never counted, and a summary that could not say so would understate every
mixed-adapter wave by however much the silent half cost. So the summary carries `reported` and
`unreported` counts beside the numbers, and the budget is only ever compared against what was
actually measured.

Which is also why `--budget` refuses an adapter that reports nothing: see
`gbfleet.adapters.reports_tokens`. A budget that can never be exceeded never fires.
"""
from __future__ import annotations


def totals(spend: dict[str, dict], spawned: int) -> dict:
    """The wave summary's `spend` block.

    `spawned` is passed in rather than derived from `spend`, because the gap between them IS
    the answer: a wave that started six children and has two records did not spend what those
    two records say.
    """
    rows = list(spend.values())
    tokens_in = sum(int(r.get("tokens_in") or 0) for r in rows)
    tokens_out = sum(int(r.get("tokens_out") or 0) for r in rows)
    return {
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "tokens": tokens_in + tokens_out,
        # The honest headline. Without these two the number above reads as the wave's cost.
        "reported": len(rows),
        "unreported": max(0, spawned - len(rows)),
        "by_child": [
            {"branch": branch, "adapter": row.get("adapter", ""),
             "tokens_in": row.get("tokens_in"), "tokens_out": row.get("tokens_out"),
             "turns_used": row.get("turns_used")}
            for branch, row in sorted(spend.items())
        ],
    }


def spent(spend: dict[str, dict]) -> int:
    """Tokens measured so far. What a budget is compared against, and nothing else.

    Deliberately not an estimate that fills in for the children that said nothing. Guessing
    what an unreported child cost would make the budget fire on invented numbers, which is a
    worse failure than not firing: the wave ends and the operator cannot tell why.
    """
    return sum(int(r.get("tokens_in") or 0) + int(r.get("tokens_out") or 0)
               for r in spend.values())


def over(spend: dict[str, dict], budget: int | None) -> str:
    """The sentence to end a wave on, or "" while it is still within budget.

    Names the measured total AND how many children were not counted, because "over budget at
    equal to or above the cap" is a claim the operator will check, and it is only true of the
    children that reported.
    """
    if not budget:
        return ""
    used = spent(spend)
    if used < budget:
        return ""
    return (f"budget {budget} tokens reached: {used} measured across {len(spend)} child(ren) "
            "that reported. Not spawning further")
