"""What an item's work reaches: this repository, or something deployed (GRPH-832).

**The problem, stated exactly.** An item's touchpoints are a claim about FILES. On
2026-09-08 a cheap-tier worker was handed an item whose touchpoints were four ordinary
repository files — a markdown page, a TypeScript source, a deploy script, `vercel.json` —
and whose description implied production. It rotated the production encryption key, deleted
rows, set production environment variables on a hosted platform and redeployed. Its own
evidence log records each step. Nothing was ultimately lost because the encrypted tables held
zero rows, which is luck rather than containment.

Not one of those four declared files was modified. The item's declaration and the work it
actually authorised had nothing to do with each other, and no surface could say so, because
**there was no way to declare that an item reaches outside the repository at all.** Every such
item is structurally indistinguishable from a docs change.

**What this module does NOT do, and cannot.** It does not decide whether prose is dangerous.
A classifier over free text cannot separate "rotate the production key" from "a worker rotated
the production key" — the second sentence appears in this very docstring, and in the ledger
item that asked for this code. That is a category problem, not a tuning problem, and any
amount of pattern work leaves it exactly where it was.

So the signals here are **evidence for a person, never a verdict**. They are quoted back with
the line they came from so a reader settles it in one glance, and the only thing they can do on
their own is make a caller say `acknowledge_reach=true` — one deliberate argument, recorded
against the delegation, with an author. Having to type it is the point; that is the same trade
`--allow psql` makes in the supervisor's PATH shim.

The one hard rule lives elsewhere and rests on a DECLARATION rather than on prose:
`Item.reach == "deploy"` is undelegatable and unclaimable by any seat, and only a signed-in
person can set or clear it (`PATCH /api/items/{id}` takes a bearer JWT; no agent credential
reaches it).

**Relationship to the supervisor's deny-list.** `gbfleet.shim.DEFAULT_DENY` names commands a
child may not RUN; the vocabulary below names commands whose APPEARANCE IN PROSE suggests work
outside the worktree. They overlap because they are answers to nearly the same question, but
they are not the same list and must not be merged into one: the shim's is operator-tunable per
wave, this one is a fixed reading vocabulary, and they ship in different distributions.
"""
from __future__ import annotations

import re

#: Where an item's work lands. `repo` is everything ordinary: the change is files in a
#: worktree, which is the only thing a spawned child is equipped to do safely.
REPO = "repo"
#: `deploy` is work on a running system — a hosted platform, a live database, a secret store.
#: A worktree cannot contain it and a review of a diff cannot verify it.
DEPLOY = "deploy"
REACHES = (REPO, DEPLOY)

#: Platforms and infrastructure CLIs. Deliberately the same vocabulary the supervisor's PATH
#: shim refuses, because both are answering "what reaches outside this worktree" — see the
#: module docstring for why they stay two lists.
_PLATFORMS = (
    "railway|vercel|fly|flyctl|heroku|aws|gcloud|az|doctl|kubectl|helm|terraform|tofu"
    "|netlify|render|supabase|cloudflare|wrangler"
)
#: What a platform name may NOT be followed by for `platform-command` to fire.
#:
#: Two kinds, and the second is GRPH-989. The first is READING subcommands — `vercel logs`,
#: `kubectl get` — which are how a worker investigates; refusing those would make the rule fire
#: on every diagnostic and turn the acknowledgement into reflex.
#:
#: The second is English FUNCTION WORDS. Several platform names are ordinary English — `render`
#: most of all, and this tracker is used for UI work — so "must not render as \"off\"" read as
#: an infrastructure CLI and refused a Settings panel three times in one day: once on the item
#: being built, once on a note explaining the trap, and once on GRPH-989 itself, whose
#: description had to quote the phrase to report it.
#:
#: A function word after the platform name means the sentence is using the word as English
#: (`render as`, `render the`). That exclusion is necessary and not sufficient — `Railway
#: stack` is not a function word either. The command shape below is what separates a name
#: from an invocation.
_NOT_SUBCOMMANDS = (
    # reading, not changing
    "logs|get|status|version|--help"
    # articles, conjunctions, prepositions and copulas — grammar, not a command
    "|a|an|the|as|at|by|for|from|in|into|of|off|on|onto|out|over|to|up-to|with|within"
    "|and|or|but|if|so|than|then|that|this|these|those|which|while|when|whether"
    "|is|are|was|were|be|been|being|it|its|them|their|there|they"
    "|not|no|never|only|also|just|still|already|rather|instead"
    # auxiliaries — "the Railway project will deploy" is a sentence, not a subcommand
    "|will|would|should|could|can|may|might|must|shall"
)

#: Verbs an infrastructure CLI is actually invoked with (GRPH-1037).
#:
#: GRPH-989 stopped the rule on function words, and that was not enough. The matcher was
#: still "a platform name, then any other word", so `Railway stack` — a diagram legend —
#: read as `railway` with the subcommand `stack`. The same shape refused a Settings panel
#: for `render` and a pile of topology prose for naming Railway at all. A stop-list of
#: nouns cannot close that: the next legend says `project` or `service` or `topology`.
#:
#: A command is shaped like one. The platform name is followed by a verb (`up`, `deploy`,
#: `apply`), or by a noun subcommand and then a verb within two words (`variables set`,
#: `rollout restart`, `compute instances delete`), or by a flag. `Railway stack (web+api+db)`
#: is a name. `railway up --service api` is an invocation.
#:
#: Two things this deliberately does NOT do. It does not ignore backticks or fenced blocks:
#: the commands that caused the original incident are written that way (`railway up`), and
#: stripping them would blind the guard to the case it exists for. It does not treat a
#: declared `reach=repo` as a reason to stay quiet: every item this fires on is already
#: declared `repo`, and the incident was one of those whose prose was a real command.
#: Weighting the declaration would delete the guard.
#:
#: `docker compose` is not in `_PLATFORMS` — `compose` alone is how a legend says
#: `compose/Railway stack`, and a slash is not a subcommand. The two-word CLI is matched
#: on its own, same rule, same reason.
_COMMAND_VERBS = (
    "add|annotate|apply|build|connect|cordon|cp|create|delete|deploy|destroy|down|drain"
    "|drop|exec|export|expose|import|init|kill|label|launch|link|login|logout|mv|open"
    "|patch|plan|promote|provision|publish|pull|purge|push|put|redeploy|restart|revoke|rm"
    "|rollback|rollout|rotate|run|scale|set|ssh|start|stop|sync|taint|truncate|uncordon"
    "|undo|unlink|unset|up|update|upgrade|wipe"
)
_WORD = r"[a-z][\w-]*"
_PLATFORM_COMMAND = (
    # platform, then a verb
    rf"\b(?:{_PLATFORMS})\s+(?:(?:{_COMMAND_VERBS})\b"
    # or a noun subcommand and a verb (`railway variables set`)
    rf"|(?!(?:{_NOT_SUBCOMMANDS})\b){_WORD}\s+(?:{_COMMAND_VERBS})\b"
    # or one more word, then a verb (`gcloud compute instances delete`)
    rf"|(?!(?:{_NOT_SUBCOMMANDS})\b){_WORD}\s+(?!(?:{_NOT_SUBCOMMANDS})\b)"
    rf"{_WORD}\s+(?:{_COMMAND_VERBS})\b"
    # or a flag, which is how an invocation looks when the verb list missed a noun
    rf"|(?!(?:{_NOT_SUBCOMMANDS})\b){_WORD}\s+--?[\w.-]+)"
    # `docker compose down` / `docker-compose down`. Not `compose/Railway`.
    rf"|\bdocker(?:\s+|-)compose\s+(?:{_COMMAND_VERBS})\b"
)

#: Verbs that CHANGE a running system. Reading verbs are absent on purpose: `vercel logs` and
#: `kubectl get` are how a worker investigates, and refusing those would make this fire on
#: every diagnostic.
_MUTATIONS = (
    "deploy|redeploy|rollback|rotate|revoke|reissue|promote|restart|scale|provision|destroy"
    "|drop|truncate|purge|wipe"
)
_ENVIRONMENTS = r"prod|production|live|staging"
_SECRETS = r"key|keys|secret|secrets|token|tokens|credential|credentials|password"

#: Each rule is (name, compiled pattern, what it means). The name travels with the match so a
#: refusal says WHICH reading fired, not just that something did — a reader who disagrees can
#: see the rule and judge it rather than guessing.
_RULES: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "platform-command",
        re.compile(_PLATFORM_COMMAND, re.I),
        "names an infrastructure CLI with a subcommand",
    ),
    (
        "environment-mutation",
        re.compile(rf"\b({_MUTATIONS})\b[^.\n]{{0,40}}\b({_ENVIRONMENTS})\b"
                   rf"|\b({_ENVIRONMENTS})\b[^.\n]{{0,40}}\b({_MUTATIONS})\b", re.I),
        "changes a running environment",
    ),
    (
        "secret-rotation",
        re.compile(rf"\b(rotate|rotating|revoke|revoking|reissue)\b[^.\n]{{0,40}}\b({_SECRETS})\b",
                   re.I),
        "rotates or revokes a credential",
    ),
    (
        "hosted-environment-variable",
        re.compile(rf"\b(env|environment)\s+(var|vars|variable|variables)\b[^.\n]{{0,60}}"
                   rf"\b({_PLATFORMS}|{_ENVIRONMENTS})\b"
                   rf"|\b({_PLATFORMS})\b[^.\n]{{0,40}}\b(env|environment)\b", re.I),
        "sets configuration on a hosted platform",
    ),
)

#: How many signals a caller is shown. Enough to judge, few enough to read. A description that
#: trips nine rules is not nine times more informative than one that trips three.
MAX_SIGNALS = 3
#: How much of the matching line is quoted. Long enough to carry the sentence, short enough
#: that a refusal stays a refusal rather than becoming a paste of the item.
QUOTE_MAX = 160


def signals(text: str | None) -> list[dict]:
    """Lines in `text` that read as work on something outside this repository.

    Returns `[{"rule", "means", "quote"}]`, at most `MAX_SIGNALS`, in the order they appear —
    reading order, because a reader is going to scan the item next and matching that order is
    what makes the two agree.

    LINE-scoped rather than whole-text, so a match cannot be assembled out of two unrelated
    paragraphs. That was the first version and it fired on an item whose summary said
    "production" and whose acceptance criteria, forty lines later, said "delete". One line is
    also the unit a person can check.

    Empty for empty input, and that is an answer rather than a default: an item with no
    description has nothing to read, which is a different situation from one that reads clean.
    Callers that care about the difference should ask whether there was a description at all.
    """
    found: list[dict] = []
    seen: set[str] = set()
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        for name, pattern, means in _RULES:
            if name in seen:
                # One example per rule. Three quotes of the same reading are one piece of
                # evidence printed three times, and they crowd out the second rule that
                # fired — which is the one that would have changed the reader's mind.
                continue
            if pattern.search(stripped):
                seen.add(name)
                found.append({"rule": name, "means": means, "quote": _quote(stripped)})
                if len(found) >= MAX_SIGNALS:
                    return found
    return found


def _quote(line: str) -> str:
    line = " ".join(line.split())
    if len(line) <= QUOTE_MAX:
        return line
    return line[: QUOTE_MAX - 1].rstrip() + "…"


def describe(item_reach: str, found: list[dict]) -> dict:
    """The brief's `reach` block: what the item DECLARES, and what its prose suggests.

    Both halves, always, because the interesting case is the disagreement — an item declaring
    `repo` whose text reads like a deployment is exactly the shape of the incident this exists
    for, and a block that reported only the declaration would render it as `repo` and say
    nothing. `basis` is the same word `lane` and `checklist` use for "the evidence that decided
    this", so a reader who has met one has met all three.
    """
    return {
        "value": item_reach if item_reach in REACHES else REPO,
        "declared": item_reach in REACHES,
        "signals": found,
        "basis": [s["quote"] for s in found],
    }


def refusal(item_key: str, found: list[dict]) -> str:
    """What to say when prose disagrees with a `repo` declaration.

    Quotes the item back at the caller rather than describing it. The reader is about to
    decide whether a process with a shell should act on this text, and the text is the only
    thing that can settle it.
    """
    lines = "\n".join(f"  · {s['quote']}   [{s['rule']}: {s['means']}]" for s in found)
    return (
        f"{item_key} is declared `reach=repo`, and reads like work on a deployed system:\n"
        f"{lines}\n"
        "A delegated child gets a worktree and a shell, and neither can be undone by "
        "reviewing a diff. If this work really is confined to the worktree, pass "
        "`acknowledge_reach=true` — it is recorded against the delegation. If it is not, set "
        "the item's reach to `deploy` in the UI and do it yourself; a person has to, because "
        "no agent credential can set that field."
    )
