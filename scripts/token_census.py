#!/usr/bin/env python3
"""Where an agent's tokens actually go: attribute every tool result to its call.

GRPH-462. This exists because a one-session measurement contradicted two things
everyone assumes, and a finding from a sample of one is not a basis for building
anything. Point it at transcripts and it will either hold that finding up or refuse it.

    scripts/token_census.py ~/.claude/projects/*/*.jsonl
    scripts/token_census.py --json <transcript>          # machine-readable
    scripts/token_census.py --classifier                 # print the rules and exit

**The classifier is the deliverable, not the totals.** Bucketing is where bias creeps
in: decide that `git show` is "source inspection" and source inspection wins; decide it
is "git" and it does not. So every rule is in one table below, each row is one regex and
one kind, and `--classifier` prints them. Disagree with a row and you can re-run it.

**Two numbers that look similar and mean opposite things.** "90% of file looks were
re-looks" and "0.5% of file looks were exact repeats" can both be true of one session,
and only the second says anything about waste. Reading a 5,000-line file twice to answer
two different questions is correct behaviour. Reading it twice with the identical
command is not. They are counted separately and never added together.

**Tokens are estimated as len/4.** No offline tokenizer is available and the same
estimate is used by `test_mcp_footprint.py`, so the numbers are comparable to the
manifest budget the repo already reasons about. Everything here is relative anyway — the
question is which bucket dominates, and a constant factor does not move that.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import re
import statistics
import sys
from dataclasses import dataclass, field
from pathlib import Path

# --- the classifier ------------------------------------------------------------------
#
# Ordered. First match wins, so put the specific before the general. Each entry is
# (kind, matches-a-tool-name, matches-a-bash-command).

TOOL_KINDS: list[tuple[str, str | None, str | None]] = [
    # Reading the codebase, whatever the mechanism. This is the bucket the finding is
    # about, so it is deliberately drawn WIDE: dedicated tools and the shell commands
    # that do the same job. Narrowing it would flatter the thesis.
    ("source", r"^(Read|Grep|Glob)$", None),
    ("source", r"^Bash$", r"^\s*(cat|head|tail|sed|awk|less|bat|wc|nl)\b"),
    ("source", r"^Bash$", r"^\s*(grep|rg|ag|ack|ugrep)\b"),
    ("source", r"^Bash$", r"^\s*(find|ls|tree|fd)\b"),
    # Verification. Piped through tail/grep in practice, which is why it is cheaper than
    # people expect — that is a finding, not a measurement artifact, so it stays here
    # rather than being normalised away.
    ("test", r"^Bash$", r"\b(pytest|vitest|jest|npm (run )?test|pnpm test|go test|cargo test)\b"),
    ("test", r"^Bash$", r"\b(tsc|typecheck|mypy|ruff|eslint)\b"),
    # The ledger this repo is. Separated from other MCP because it is the thing being
    # evaluated: if reading source dominates, the pitch is that the ledger should absorb
    # some of it.
    ("ledger", r"^mcp__(agentledger|graphban)__", None),
    ("mcp_other", r"^mcp__", None),
    # Git that READS history is not source inspection and not plumbing — `git show` and
    # `git log -p` return code, and lumping them either way would settle the question by
    # definition. Its own bucket; judge it in the report.
    ("git_read", r"^Bash$", r"^\s*git\s+(show|log|diff|blame|cat-file)\b"),
    ("git_other", r"^Bash$", r"^\s*git\b"),
    ("remote", r"^Bash$", r"^\s*(ssh|scp|rsync|docker\s+(exec|compose\s+exec))\b"),
    ("network", r"^(WebFetch|WebSearch)$", None),
    ("network", r"^Bash$", r"^\s*(curl|wget|gh|http)\b"),
    # Writes return almost nothing, which is itself worth showing: the expensive half of
    # editing is finding the place, not making the change.
    ("write", r"^(Edit|Write|NotebookEdit|MultiEdit)$", None),
    ("write", r"^Bash$", r"^\s*(cat\s*>|tee|python3?\s+-\s*<<|mkdir|cp|mv|rm|chmod)\b"),
    ("agent", r"^(Task|Agent)$", None),
    ("todo", r"^(TodoWrite|TaskCreate|TaskUpdate)$", None),
]

#: Commands whose output is bounded by construction. Counted, but flagged, because a
#: bucket that is cheap ONLY because everything in it is piped through `tail` is a
#: different claim from a bucket that is cheap by nature.
TRUNCATED = re.compile(r"\|\s*(tail|head)\b|--quiet|-q\b")


#: Prefixes that carry no work and hide the command that does. Stripped before
#: classifying, repeatedly, because `cd x && cd y && grep ...` happens.
#:
#: **This was found by auditing, not by design.** The first version anchored every rule
#: on `^` and 30.8% of all tokens landed in the residual bucket — nearly all of it
#: `cd <repo> && <the real command>`. Source inspection read as 35.9% with that hole and
#: the residual was almost as large as it was, so the headline number was being set by a
#: regex detail rather than by the data. A bucket that big is not a rounding error; it is
#: the finding, sitting in the wrong column.
_NOISE = re.compile(
    r"""^\s*(
          cd\s+[^&;|\n]+ (&&|;|\n)       # cd somewhere first
        | [A-Za-z_][A-Za-z0-9_]*=("[^"]*"|'[^']*'|[^\s]*)\s*(&&|;|\n|\s)
        | export\s+[^&;|\n]+ (&&|;|\n)
        | echo\s+("[^"]*"|'[^']*'|[^&;|\n]+) (&&|;|\n)
        | set\s+-[a-z]+\s*(&&|;|\n)
        | source\s+[^&;|\n]+ (&&|;|\n)
        | \.\s+[^&;|\n]+ (&&|;|\n)
    )\s*""",
    re.VERBOSE,
)


def effective_command(command: str) -> str:
    """The part of a shell line that actually did the work.

    Strips the navigation and setup that precedes it. Where a compound still remains,
    the FIRST substantive verb wins — a defensible single answer, stated so it can be
    disagreed with, since `cd x && pytest && git status` has no unarguable single kind.
    """
    previous = None
    while previous != command:
        previous = command
        command = _NOISE.sub("", command, count=1)
    return command.strip()


def classify(name: str, command: str) -> str:
    command = effective_command(command)
    for kind, tool_pattern, cmd_pattern in TOOL_KINDS:
        if tool_pattern and not re.search(tool_pattern, name):
            continue
        if cmd_pattern and not re.search(cmd_pattern, command):
            continue
        if tool_pattern or cmd_pattern:
            return kind
    return "shell_other" if name == "Bash" else "other"


#: What a call was AIMED at, for the repeat analysis. Heuristic and deliberately
#: conservative: a path-shaped token, or the Read/Grep target. Calls with no recoverable
#: target are excluded from the same-target counts rather than guessed at, because a bad
#: guess here manufactures exactly the "re-reading" conclusion the finding disputes.
_PATHY = re.compile(r"[\w./-]*/[\w./-]+\.\w+|\b[\w-]+\.(py|ts|tsx|md|yml|yaml|json|toml|sh)\b")


def target_of(name: str, inp: dict, command: str) -> str | None:
    for key in ("file_path", "path", "notebook_path"):
        if inp.get(key):
            return str(inp[key])
    if name in {"Grep", "Glob"} and inp.get("pattern"):
        return f"{inp.get('path') or '.'}::{inp['pattern']}"
    found = _PATHY.findall(command)
    if not found:
        return None
    first = found[0]
    return first if isinstance(first, str) else command[:80]


def tokens(text: str) -> int:
    return len(text) // 4


@dataclass
class Call:
    kind: str
    name: str
    command: str
    target: str | None
    fingerprint: str
    result_tokens: int
    truncated: bool


@dataclass
class Census:
    session: str
    calls: list[Call] = field(default_factory=list)

    @property
    def total(self) -> int:
        return sum(c.result_tokens for c in self.calls)

    def by_kind(self) -> dict[str, tuple[int, int]]:
        out: dict[str, list[int]] = collections.defaultdict(lambda: [0, 0])
        for call in self.calls:
            out[call.kind][0] += call.result_tokens
            out[call.kind][1] += 1
        return {k: (v[0], v[1]) for k, v in sorted(out.items(), key=lambda kv: -kv[1][0])}

    def repeats(self, kind: str = "source") -> dict:
        """Exact repeats versus same-target-different-question.

        The distinction the whole finding turns on. An exact repeat is the identical
        call made twice — recoverable by a cache. A same-target repeat is the same file
        asked a different question, which is how you read a large file correctly.
        """
        calls = [c for c in self.calls if c.kind == kind]
        prints = collections.Counter(c.fingerprint for c in calls)
        targets = collections.Counter(c.target for c in calls if c.target)
        exact = sum(n - 1 for n in prints.values() if n > 1)
        same_target = sum(n - 1 for n in targets.values() if n > 1)
        wasted = sum(
            c.result_tokens for c in calls
            if prints[c.fingerprint] > 1
        ) - sum(
            next(c.result_tokens for c in calls if c.fingerprint == fp)
            for fp, n in prints.items() if n > 1
        )
        return {
            "calls": len(calls),
            "exact_repeats": exact,
            "exact_repeat_pct": round(100 * exact / len(calls), 2) if calls else 0.0,
            "same_target_repeats": same_target,
            "same_target_pct": round(100 * same_target / len(calls), 2) if calls else 0.0,
            "recoverable_tokens": max(0, wasted),
            "distinct_targets": len(targets),
            "busiest": targets.most_common(5),
        }

    def per_answer(self, kind: str = "source") -> dict:
        """The DISTRIBUTION of what one question costs, not the average.

        The average hides the thing worth finding: pulling forty lines to read one
        signature is waste, and it disappears into a healthy-looking mean sitting beside
        a handful of whole-file reads.
        """
        sizes = sorted(c.result_tokens for c in self.calls if c.kind == kind)
        if not sizes:
            return {}
        quantile = lambda p: sizes[min(len(sizes) - 1, int(len(sizes) * p))]  # noqa: E731
        top_decile = sizes[int(len(sizes) * 0.9):]
        return {
            "n": len(sizes),
            "mean": round(statistics.mean(sizes)),
            # What share of the bucket comes from its biggest tenth of answers. The
            # question the average cannot answer: is this a broad cost spread over
            # thousands of small lookups, or a handful of enormous ones? Those need
            # completely different fixes, and the mean reads the same either way.
            "top_decile_share": round(100 * sum(top_decile) / sum(sizes), 1),
            "p50": quantile(0.50),
            "p90": quantile(0.90),
            "p99": quantile(0.99),
            "max": sizes[-1],
            "under_100": sum(1 for s in sizes if s < 100),
            "over_2000": sum(1 for s in sizes if s > 2000),
        }


def read(path: Path) -> Census:
    census = Census(session=path.stem[:8])
    pending: dict[str, tuple[str, str, str | None, str, bool, str]] = {}

    with path.open(errors="replace") as handle:
        for line in handle:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            message = record.get("message") or {}
            content = message.get("content")
            if not isinstance(content, list):
                continue

            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    inp = block.get("input") or {}
                    name = block.get("name") or ""
                    command = str(inp.get("command") or "")
                    fingerprint = hashlib.sha1(
                        json.dumps([name, inp], sort_keys=True, default=str).encode()
                    ).hexdigest()
                    pending[block.get("id")] = (
                        classify(name, command), name, target_of(name, inp, command),
                        fingerprint, bool(TRUNCATED.search(command)), command,
                    )
                elif block.get("type") == "tool_result":
                    call = pending.pop(block.get("tool_use_id"), None)
                    if call is None:
                        continue
                    body = block.get("content")
                    if isinstance(body, list):
                        body = "".join(
                            b.get("text", "") for b in body if isinstance(b, dict)
                        )
                    kind, name, target, fingerprint, truncated, command = call
                    census.calls.append(Call(
                        kind=kind, name=name, command=command, target=target,
                        fingerprint=fingerprint, result_tokens=tokens(str(body or "")),
                        truncated=truncated,
                    ))
    return census


def report(censuses: list[Census], out=sys.stdout) -> None:
    print(f"\n{'session':10} {'calls':>7} {'tokens':>10}  top three buckets", file=out)
    print("-" * 78, file=out)
    for c in censuses:
        kinds = c.by_kind()
        top = "  ".join(
            f"{k} {100 * v[0] / c.total:.0f}%" for k, v in list(kinds.items())[:3]
        ) if c.total else "-"
        print(f"{c.session:10} {len(c.calls):>7} {c.total:>10,}  {top}", file=out)

    merged = Census(session="ALL")
    for c in censuses:
        merged.calls.extend(c.calls)

    print(f"\nACROSS {len(censuses)} SESSIONS — {len(merged.calls):,} calls, "
          f"{merged.total:,} tokens of tool results\n", file=out)
    print(f"  {'kind':14} {'tokens':>11} {'share':>7} {'calls':>8} {'per call':>9}", file=out)
    print("  " + "-" * 54, file=out)
    for kind, (tok, n) in merged.by_kind().items():
        print(f"  {kind:14} {tok:>11,} {100 * tok / merged.total:>6.1f}% "
              f"{n:>8,} {tok // max(n, 1):>9,}", file=out)

    rep = merged.repeats("source")
    print(f"\nSOURCE INSPECTION — {rep['calls']:,} calls over "
          f"{rep['distinct_targets']:,} distinct targets", file=out)
    print(f"  exact repeats        {rep['exact_repeats']:>7,}  ({rep['exact_repeat_pct']}%)  "
          f"<- what a cache could recover", file=out)
    print(f"  same target, differently {rep['same_target_repeats']:>3,}  "
          f"({rep['same_target_pct']}%)  <- correct behaviour, not waste", file=out)
    print(f"  recoverable tokens   {rep['recoverable_tokens']:>7,}  "
          f"({100 * rep['recoverable_tokens'] / max(merged.total, 1):.2f}% of everything)", file=out)

    dist = merged.per_answer("source")
    if dist:
        print(f"\n  cost of one answer: p50 {dist['p50']:,} · p90 {dist['p90']:,} · "
              f"p99 {dist['p99']:,} · max {dist['max']:,} (mean {dist['mean']:,})", file=out)
        print(f"  {dist['under_100']:,} answers under 100 tokens · "
              f"{dist['over_2000']:,} over 2,000", file=out)
        print(f"  the biggest tenth of answers carry {dist['top_decile_share']}% of "
              "all source tokens", file=out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("transcripts", nargs="*", type=Path)
    parser.add_argument("--corpus", metavar="MIN_MB", type=float, default=None,
                        help="every transcript under ~/.claude/projects at least this "
                             "big, in a deterministic order — so two runs compare the "
                             "same sessions. A shell glob does not guarantee that, and "
                             "comparing two different corpora is how a reclassification "
                             "appears to change the total.")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--classifier", action="store_true", help="print the rules and exit")
    parser.add_argument("--sample", metavar="KIND",
                        help="show the biggest calls landing in one bucket, so the "
                             "classifier can be audited rather than trusted")
    args = parser.parse_args(argv)

    if args.classifier:
        print(f"{'kind':12} {'tool matches':28} bash command matches")
        print("-" * 92)
        for kind, tool, cmd in TOOL_KINDS:
            print(f"{kind:12} {tool or '':28} {cmd or ''}")
        print(f"{'shell_other':12} {'(any other Bash)':28}")
        print(f"{'other':12} {'(any other tool)':28}")
        return 0

    paths = list(args.transcripts)
    if args.corpus is not None:
        root = Path.home() / ".claude" / "projects"
        paths = sorted(
            (p for p in root.glob("*/*.jsonl")
             if p.stat().st_size >= args.corpus * 1_000_000),
            key=lambda p: str(p),
        )
    if not paths:
        parser.error("name at least one transcript, or pass --corpus")

    censuses = [read(p) for p in paths if p.exists()]
    censuses = [c for c in censuses if c.calls]
    if not censuses:
        print("no tool calls found in those transcripts", file=sys.stderr)
        return 1

    if args.sample:
        merged = Census(session="ALL")
        for c in censuses:
            merged.calls.extend(c.calls)
        picked = sorted(
            (c for c in merged.calls if c.kind == args.sample),
            key=lambda c: -c.result_tokens,
        )
        total = sum(c.result_tokens for c in picked)
        print(f"{args.sample}: {len(picked):,} calls, {total:,} tokens\n")
        seen: dict[str, list[int]] = collections.defaultdict(lambda: [0, 0])
        for call in picked:
            head = " ".join(call.command.split()[:2]) or call.name
            seen[head][0] += call.result_tokens
            seen[head][1] += 1
        print(f"  {'leading command':32} {'tokens':>10} {'share':>7} {'calls':>7}")
        print("  " + "-" * 60)
        for head, (tok, n) in sorted(seen.items(), key=lambda kv: -kv[1][0])[:20]:
            print(f"  {head[:32]:32} {tok:>10,} {100 * tok / max(total, 1):>6.1f}% {n:>7,}")
        return 0

    if args.json:
        merged = Census(session="ALL")
        for c in censuses:
            merged.calls.extend(c.calls)
        print(json.dumps({
            "sessions": [
                {"session": c.session, "calls": len(c.calls), "tokens": c.total,
                 "by_kind": c.by_kind()} for c in censuses
            ],
            "total": {"calls": len(merged.calls), "tokens": merged.total,
                      "by_kind": merged.by_kind(),
                      "source_repeats": merged.repeats("source"),
                      "source_per_answer": merged.per_answer("source")},
        }, indent=2, default=str))
        return 0

    report(censuses)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
