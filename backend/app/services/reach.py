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
#: (`render as`, `render the`). This is the base matcher's list, from before GRPH-1037
#: narrowed it to a verb allowlist. That allowlist fails open: every verb it lacks
#: (`reset`, then `remove` and `detach`) is a destructive command the guard goes quiet on.
#: The default below is the base matcher again. It fires on a platform name followed by
#: any other word. The only things that stay quiet are the named shapes under it.
_NOT_SUBCOMMANDS = (
    # reading, not changing
    "logs|get|status|version|--help"
    # articles, conjunctions, prepositions and copulas — grammar, not a command
    "|a|an|the|as|at|by|for|from|in|into|of|off|on|onto|out|over|to|up-to|with|within"
    "|and|or|but|if|so|than|then|that|this|these|those|which|while|when|whether"
    "|is|are|was|were|be|been|being|it|its|them|their|there|they"
    "|not|no|never|only|also|just|still|already|rather|instead"
)

#: The one base hit that is not a mutation. `kubectl port-forward` opens a tunnel.
#: It stays its own token, rather than joining `_NOT_SUBCOMMANDS`, so the test's
#: EXEMPT list and this token are the same decision. Delete it and the exempt row fires.
_EXEMPT_SUBCOMMANDS = "port-forward"

#: Nouns a diagram uses for a platform (`Railway stack`, `Railway project`).
#: They are also real subcommands (`vercel project rm`, `heroku stack:set`),
#: so the list never applies on its own — only inside the two spans below.
_DETERMINERS = r"the|a|an|our|their|this|that"
_DESCRIPTIVE_NOUNS = (
    "stack|stacks|project|projects|dashboard|dashboards|team|teams"
    "|account|accounts|doc|docs|topology|diagram|legend"
)

#: A diagram legend glues ONE word to the platform (`compose/Railway stack`,
#: `api.railway stack`). The base matcher sees a word boundary at the slash or
#: the dot and reads `Railway stack` as a subcommand.
#:
#: The character in front of the name is not enough. A binary invoked by its
#: path has a slash there too (`/usr/local/bin/kubectl delete`, `./bin/railway
#: up`, `~/bin/fly deploy`). Those are commands. The glue is one word whose
#: own previous character is a boundary — start, whitespace, or an opening
#: quote — not another path separator. `./bin/railway` fails that, because
#: `bin` is preceded by `/`. `infra/terraform` is one word and a slash, the
#: same shape as `compose/Railway`; it stays a legend only when the tail below
#: is descriptive, and `terraform destroy` is not that tail.
#:
#: A legend's tail is descriptive: the end of the line, sentence punctuation,
#: or one descriptive noun that itself ends the clause (punctuation, an opening
#: `(`, or the end of the line). `:` is not that punctuation — `stack:set` is
#: a subcommand. The `(` tail belongs to the legend glue only. A path-prefixed
#: binary before `(` is still a command (`Run ./bin/railway projects (prod)`).
#: Treating every slash as glue swallows that line, which the parity sweep
#: catches. Put `(?<![/.])` back on the platform pattern and the path-prefixed
#: commands go quiet too.
#:
#: Python `re` cannot say both lookarounds in one assertion, so this span is
#: scrubbed before the matcher runs. Delete the span and `compose/Railway
#: stack` fires.
_CLAUSE_END = r"(?:\s*$|\s*[,.;!?·—–)]|\s+\()"
_LEGEND_GLUE = r"(?:^|(?<=[\s(\[{}\"\'`]))[\w-]+[/.]"
_LEGEND_SPAN = re.compile(
    rf"{_LEGEND_GLUE}(?:{_PLATFORMS})(?:"
    rf"\s+(?:{_DESCRIPTIVE_NOUNS})\b(?={_CLAUSE_END})"
    rf"|{_CLAUSE_END})",
    re.I | re.M,
)

#: Sentence subject, not an invocation: "The Railway project will deploy next
#: week". Three things have to be true, and dropping any one swallows a
#: command.
#:
#: The determiner is what makes it a subject — without it, `gcloud projects
#: delete` and `vercel project rm` go quiet. It has to START the clause: the
#: line, or the words right after sentence punctuation (`.!?`), with at most
#: one conjunction or adverb in between (`Done. But the Railway project
#: will`). A word before the determiner is the verb of an imperative, and
#: punctuation after the noun does not make that an object into a subject:
#: `Delete the vercel project.`, `Wipe the supabase project.`, `Tear down the
#: kubectl projects, now`. Putting the punctuation or end-of-line tail back
#: scrubs those, which the parity sweep catches.
#:
#: The noun has to be followed by a modal or copula (`will`, `is`, `was`,
#: `has`, ...). Without that, a determiner in front of a real subcommand is
#: scrubbed too: `Run the heroku stack:set`, `Use the gcloud projects delete`.
#: `:` is not a modal.
#:
#: A passive in the rest of the clause is the instruction, not a subject:
#: `THE VERCEL PROJECT WILL BE REMOVED`, `will be purged`, `will get wiped`,
#: `is removed`, `was deleted, so delete it again`. The shape is the auxiliary,
#: not a list of participles. A list fails open: every participle it lacks
#: (`purged`, `nuked`, `decommissioned`) is a destructive sentence the guard
#: goes quiet on. Putting the seven-word list back is what the parity rows
#: for those sentences catch.
#:
#: The shape, anywhere in the clause up to sentence punctuation: `be`, `been`,
#: `being`, `get`, `got`, `gets` or `getting`, then at most two words, then a
#: word ending in `-ed` or `-en`, or `down` / `off` / `out`. Or a copula
#: (`is`, `are`, `was`, `were`) followed straight away by such a word. An
#: adverb may sit between the modal and `be` (`should now be deleted`,
#: `will soon be deleted`); `to be` and `going to be` are the same shape.
#:
#: A destructive verb from `_MUTATIONS` later in that same clause is also the
#: instruction (`will ship next week, so drop the database`). The word
#: immediately after the modal is not "later": `will deploy` is the
#: descriptive future this span exists to keep quiet. Drop the later check
#: and that compound row goes quiet, which the parity sweep catches.
#:
#: A possessive (`Railway's dashboard`) needs no span. The matcher requires
#: whitespace after the platform name, and `'` is not whitespace.
#:
#: The prefix is not one width, so this span is scrubbed rather than written
#: as a lookbehind. Delete the scrub and the future-deploy sentence fires.
_SUBJECT_TAIL = (
    "will|would|shall|should|can|could|may|might|must"
    "|is|are|was|were|be|been|being"
    "|has|have|had|does|do|did"
)
_SUBJECT_CONJUNCTION = (
    "and|but|or|nor|so|yet|then|also|now|still|thus|however"
)
#: The passive shape. Not a participle list.
#:
#: `be` / `get` (and been/being/got/gets/getting), then at most two words,
#: then a word ending `-ed` or `-en`, or `down` / `off` / `out`. A copula
#: (`is` / `are` / `was` / `were`) followed straight away by such a word is
#: the same shape: `is removed`, `was deleted`.
#:
#: `reset` is the one unchanged participle the sweep already requires
#: (`will be reset`). It has no `-ed` / `-en` ending, and `down` / `off` /
#: `out` do not follow it. That one word is not a list to grow: the next
#: unchanged participle is a red parity row until the ending names it.
#: `will deploy` has no auxiliary and stays a subject. `will be ready`
#: does not match this shape and stays quiet too.
_PASSIVE_AUX = r"be|been|being|get|got|gets|getting"
_PASSIVE_ENDING = r"(?:\w*(?:ed|en)|down|off|out|reset)"
_PASSIVE_SHAPE = re.compile(
    rf"\b(?:{_PASSIVE_AUX})\b(?:\s+\w+){{0,2}}\s+{_PASSIVE_ENDING}\b"
    rf"|\b(?:is|are|was|were)\s+{_PASSIVE_ENDING}\b",
    re.I,
)
_SUBJECT_SPAN = re.compile(
    rf"(?:^|[.!?]\s+)(?:(?:{_SUBJECT_CONJUNCTION})\s+)?"
    rf"(?:{_DETERMINERS})\s+(?:{_PLATFORMS})\s+(?:{_DESCRIPTIVE_NOUNS})\b"
    rf"(?=\s+(?:{_SUBJECT_TAIL})\b)",
    re.I | re.M,
)

_FLAG = r"--?[\w.-]+"
#: The base matcher, plus the port-forward exemption. Legend and sentence-subject
#: are scrubbed before this runs; they are not lookarounds on this pattern.
#: `vercel remove`, `fly postgres detach`, `heroku pg:reset` and
#: `az network nsg rule delete` all fire on the first word after the platform.
#: There is no verb list: a missing verb is a command the guard would not see.
#:
#: `docker compose` is not in `_PLATFORMS`. `compose` alone is how a legend says
#: `compose/Railway`, and a slash is not a subcommand. The two-word CLI is matched
#: on its own, fail-closed the same way: any word that is not grammar, not a verb
#: list. Flags may sit between `compose` and that word (`--profile prod down`).
#:
#: Two things this deliberately does NOT do. It does not ignore backticks or fenced
#: blocks: the commands that caused the original incident are written that way
#: (`railway up`), and stripping them would blind the guard to the case it exists
#: for. It does not treat a declared `reach=repo` as a reason to stay quiet: every
#: item this fires on is already declared `repo`, and the incident was one of those
#: whose prose was a real command. Weighting the declaration would delete the guard.
_PLATFORM_COMMAND = (
    rf"\b(?:{_PLATFORMS})\s+"
    rf"(?!(?:{_NOT_SUBCOMMANDS}|{_EXEMPT_SUBCOMMANDS})\b)[a-z][\w-]+"
    rf"|\bdocker(?:\s+|-)compose(?:\s+{_FLAG}(?:\s+\S+)?)*\s+"
    rf"(?!(?:{_NOT_SUBCOMMANDS})\b)[a-z][\w-]+"
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


def _destructive_later(clause: str) -> bool:
    """A `_MUTATIONS` verb after the modal and its immediate complement.

    `will deploy` is the complement. That word is why the subject span exists,
    and counting it would fire on `The Railway project will deploy next week`.
    `so drop the database` is later in the same clause. Drop this check and
    that compound row goes quiet.
    """
    words = clause.split()
    if len(words) < 3:
        return False
    return re.search(rf"\b(?:{_MUTATIONS})\b", " ".join(words[2:]), re.I) is not None


def _scrub_subject(line: str) -> str:
    """Blank a determiner-plus-noun subject. Keep it when the clause is an order.

    The span is only the subject. The decision reads the rest of the clause,
    up to sentence punctuation, and refuses to scrub a passive shape or a
    later destructive verb. Both are instructions. A participle list is not
    how either is recognised.
    """
    def keep(match: re.Match[str]) -> str:
        clause = re.split(r"[.!?]", line[match.end():], maxsplit=1)[0]
        if _PASSIVE_SHAPE.search(clause) or _destructive_later(clause):
            return match.group(0)
        return " "

    return _SUBJECT_SPAN.sub(keep, line)


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
            # Legend and sentence-subject are variable width, so they cannot be
            # lookarounds on the pattern. Scrub those spans, then search. Other
            # rules see the original line: scrubbing must not hide them.
            if name == "platform-command":
                haystack = _scrub_subject(_LEGEND_SPAN.sub(" ", stripped))
            else:
                haystack = stripped
            if pattern.search(haystack):
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
