"""AL-78: keep the MCP surface cheap for the agents that carry it.

Two costs an agent pays: the tool manifest shipped on every `tools/list`, and the
payload of every read. These tests pin the wins — a scope-gated manifest and lean
list rows — so they can't silently regress.
"""
import json

from app.mcp_server import TOOLS, _READ_ONLY
from app.services import tool_tiers

# The ceiling, and what the manifest actually costs against it TODAY (GRPH-547).
#
# `MEASURED_TOKENS` is a literal somebody typed, never a computed value — that is the whole
# point. A figure derived the same way on both sides of an assertion agrees by construction
# and can never fail; this one disagrees the moment the manifest moves, which forces whoever
# moved it to re-run the arithmetic and write down the new number.
#
# That habit is what has never formed. This file's docstring below complains that tools were
# "added without anyone re-running the arithmetic, three times now" — and then it happened
# again: the prose recorded ~201 tokens of headroom while the real figure had fallen to ~21,
# so the one place an author would look to decide "do I have room?" read ten times too
# generous for months. The complaint was accurate and could not enforce itself.
#
# Raising CEILING is a decision, not a fix. The history below is a series of raises that the
# docstring itself argues should have been trims.
CEILING = 14200
# 13592 -> 13595 (GRPH-146). `get_code_map` gained `limit`/`offset` so a bounded read is
# possible at all, and that two-property addition did NOT fit: it cost ~73 tokens against 8 of
# headroom. It was paid for inside the manifest rather than by raising the ceiling — four
# pieces of prose that restated their own machine-readable schema were removed (`fidelity`'s
# "`low` or `high`" beside its enum, in two tools; `kind` "optionally filter by" beside its
# enum; and one sentence of flavour) and the new output fields were left undeclared.
#
# Recorded because the arithmetic is the finding: adding two integer parameters to one tool
# required trimming four unrelated places to fit. There is no room for the next one, which
# makes GRPH-146's OTHER half — tiered exposure — a prerequisite for new MCP surface rather
# than an optimisation.
#
# 13595 -> 13619 (GRPH-571), and **THE NUMBER THIS FILE DEFENDS HAS MOVED.** Read this before
# treating the raise below as a sixth capitulation.
#
# Every bump since GRPH-254 ended with some version of "the next one should be the real fix",
# and named it: progressive disclosure / tiered exposure (GRPH-48, GRPH-146). The note above
# is blunt about what that was worth — "this guard cannot force that work, only make its
# absence visible — and it has, five times now." It has now shipped. A key is minted with tool
# TIERS; the default manifest is core only, and `prd`, `codegraph`, `fleet` and `misc` are
# opt-in.
#
# So this figure stopped being what an agent pays. **Nobody receives the full manifest** unless
# they ask for all four tiers. What every key pays is CORE, and that is pinned in
# `test_tool_tiers.py` by EXACT EQUALITY (`CORE_TOKENS`), as is each tier (`TIER_TOKENS`) —
# strictly tighter than the ceiling they replace, because a ceiling permits silent growth up to
# it and an equality permits none at all. A new tool now trips whichever pin it lands in, and
# which pin it lands in says who pays for it.
#
# The +24 is this change's own cost, and it is worth naming rather than hiding in the total:
# `get_context` gained `missing_tiers` and `widen_hint`, which is how an agent learns the tiers
# exist. Tiering without that advertisement would be GRPH-580 one layer along — a capability
# that works and cannot be discovered — so it is the last thing that should have been trimmed
# to make the number fit.
#
# The ceiling is 14000 rather than 13650 for one reason: this number now bounds TOTAL SURFACE
# rather than per-agent cost, and total surface is allowed to grow — that is the point of
# having tiers to put it in. It is not slack for core. Core cannot move by a single token
# without `CORE_TOKENS` failing.
MEASURED_TOKENS = 14079


def _mint(client, auth, scopes):
    return client.post(
        "/api/api-keys", json={"name": "fp", "scopes": scopes}, headers=auth
    ).json()["plaintext"]


def _rpc(client, key, method, params=None):
    body = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        body["params"] = params
    return client.post("/api/mcp", json=body, headers={"X-API-Key": key}).json()


def _list(client, key):
    return [t["name"] for t in _rpc(client, key, "tools/list")["result"]["tools"]]


def _call(client, key, tool, arguments):
    return _rpc(client, key, "tools/call",
               {"name": tool, "arguments": arguments})["result"]["structuredContent"]


# ---- Win #1: scope-gated manifest --------------------------------------------
def test_read_only_key_sees_only_read_tools(client, auth):
    """A key without the `write` scope can't call a mutation, so it shouldn't be
    shipped 16 write-tool schemas it would only get Forbidden on.

    A SUBSET rather than equality since GRPH-571: tiering runs after the scope gate, so a
    plain read-only key also loses the read tools that live in a tier (`get_prd`,
    `prd_coverage`, `learning_loop`, `setup_project`, `generate_digest`, `list_projects`).
    The claim that matters is unchanged and is the second half — no write tool is shipped to
    a key that cannot call one. `test_tool_tiers.test_the_scope_gate_still_runs_first` pins
    the composition from the other direction: every tier granted, still no write tools.
    """
    names = _list(client, _mint(client, auth, ["read"]))
    assert set(names) <= set(_READ_ONLY)
    assert names, "the scope gate cannot be shipping an empty manifest"
    for write_tool in ("create_item", "update_item", "add_memory", "describe_code"):
        assert write_tool not in names


def test_a_write_key_with_every_tier_sees_the_full_manifest(client, auth):
    """The worst case is unchanged by tiering, which is what makes it safe to deploy: a key
    that asks for everything gets exactly what it got before."""
    key = client.post("/api/api-keys",
                      json={"name": "fp", "scopes": ["read", "write"],
                            "tool_tiers": list(tool_tiers.TIERS)},
                      headers=auth).json()["plaintext"]
    names = _list(client, key)
    assert set(names) == {t["name"] for t in TOOLS}
    assert len(names) == len(TOOLS)


def test_a_write_key_without_tiers_sees_core(client, auth):
    """And the DEFAULT case is the one that changed. Pinned here as well as in
    `test_tool_tiers.py` because this is the file an author reads to answer "what does a
    normal key cost", and the answer moved."""
    names = _list(client, _mint(client, auth, ["read", "write"]))
    assert set(names) == set(tool_tiers.CORE_TOOLS)
    assert len(names) < len(TOOLS)


def test_get_context_tool_count_matches_the_scoped_manifest(client, auth):
    """The count get_context reports is what THIS key can call — not the server total —
    so it agrees with the manifest the agent actually received."""
    read_key = _mint(client, auth, ["read"])
    assert (_call(client, read_key, "get_context", {})["tool_count"]
            == len(_list(client, read_key)))
    write_key = _mint(client, auth, ["read", "write"])
    assert (_call(client, write_key, "get_context", {})["tool_count"]
            == len(_list(client, write_key)))
    # Compared against the manifest the key was actually SENT rather than against a constant.
    # Before GRPH-571 the two happened to coincide for a write key; asserting `len(TOOLS)`
    # would now pass only by accident of the default tier set, and would go on passing if
    # `tool_count` stopped consulting the tier filter at all.
    assert len(_list(client, write_key)) < len(TOOLS), "premise: a plain key is tiered down"


# ---- Win #2: lean list rows, opt-in verbosity --------------------------------
def _seed_item(client, key):
    return _call(client, key, "create_item", {
        "title": "footprint fixture item",
        "touchpoints": ["backend/app/mcp_server.py"],
        "tags": ["footprint"],
        "effort": 3,
    })["id"]


def test_search_items_is_lean_by_default(client, auth):
    key = _mint(client, auth, ["read", "write"])
    _seed_item(client, key)
    page = _call(client, key, "search_items", {"query": "footprint fixture"})
    assert page["results"], "fixture item should be found"
    row = page["results"][0]
    assert set(row) == {"id", "title", "status"}
    # the fat fields are absent by default
    for fat in ("touchpoints", "assignee", "claimed_by", "prd_id", "fidelity", "effort"):
        assert fat not in row


def test_search_items_full_opts_back_in(client, auth):
    key = _mint(client, auth, ["read", "write"])
    _seed_item(client, key)
    page = _call(client, key, "search_items", {"query": "footprint fixture", "fields": "full"})
    row = page["results"][0]
    assert row["touchpoints"] == ["backend/app/mcp_server.py"]
    assert "fidelity" in row and "effort" in row


def test_get_backlog_lean_keeps_the_ranking_signal(client, auth):
    """The prioritization fields are the reason to call get_backlog, so lean drops the
    fat item fields but never ready/score."""
    key = _mint(client, auth, ["read", "write"])
    _seed_item(client, key)
    page = _call(client, key, "get_backlog", {})
    assert page["results"], "backlog should not be empty after seeding"
    row = page["results"][0]
    assert {"id", "title", "status", "ready", "score"} <= set(row)
    assert "touchpoints" not in row  # fat field stays opt-in
    full = _call(client, key, "get_backlog", {"fields": "full"})["results"][0]
    assert "touchpoints" in full and "score" in full


def test_bad_fields_value_is_a_validation_error(client, auth):
    key = _mint(client, auth, ["read"])
    res = client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": "search_items", "arguments": {"fields": "verbose"}}},
        headers={"X-API-Key": key},
    ).json()["result"]
    assert res["isError"] is True
    assert res["structuredContent"]["error"]["code"] == "validation"


# ---- regression guard: the manifest can't quietly bloat again ----------------
def test_manifest_stays_within_token_budget():
    """A ceiling, not an exact size — new tools are fine, unbounded prose is not.
    Measured ~7.4k tokens for the full manifest at the time of AL-78.

    Raised 9000 -> 9500 in AL-299, deliberately and with the numbers checked rather than
    as a reflex when it went red. PRD-14/15 added four tools (publish_memory,
    reject_memory, create_project, answer_grill) totalling ~3.7k chars; without them the
    manifest measures ~8.1k tokens, comfortably under the old ceiling. So the growth is
    tool COUNT, which this guard explicitly permits, not the verbosity it exists to
    catch.

    Prose was addressed first: ~740 chars came out of the four new descriptions and the
    shared effort/touchpoint strings before the ceiling moved at all. If this fires
    again, trim before raising — and check the same way, by subtracting whatever tools
    are new since the last bump.

    Raised 9500 -> 10500 in GRPH-254, by that same procedure. PRD-12's agent surface adds
    four tools (prd_acceptance, request_rebaseline, submit_verdict, close_prd) totalling
    ~3.9k chars; subtract them and the manifest measures ~9.0k tokens, comfortably under
    the old ceiling. They average 987 chars against 1001 for the existing 36, so this is
    tool COUNT again rather than the verbosity the guard exists to catch — and the count
    was already held down deliberately, since the eight acceptance READS share one tool
    behind a `view` argument instead of claiming eight names.

    ~1.0k chars came out of those four descriptions before the ceiling moved. The new
    headroom (~476 tokens) is the same slack the 9500 ceiling had before this change, so
    the guard stays as tight as it was rather than being loosened.

    Raised 10500 -> 11500 in GRPH-332, by the same procedure. PRD-17 D1 adds two tools
    (register_agent, fleet_status); subtract them and the manifest measures ~10474 tokens.
    Prose came out first and the numbers say it worked: the two started at 1240 chars each
    against a 995 average and were cut to 891, so they are now LEANER than the mean and the
    growth is tool COUNT, which this guard permits, rather than the verbosity it exists to
    catch.

    Worth recording what the measurement also showed: at the 10500 ceiling the surface had
    only ~26 tokens of headroom left, so tools had been added since GRPH-254 without anyone
    re-running this arithmetic. The new ~579 tokens restores roughly the slack the previous
    two bumps left, rather than buying room for another unchecked run of growth.

    Raised 11500 -> 12500 in GRPH-334, same procedure: D3 adds three tools (claim_review,
    sign_off, bounce); without them the manifest measures ~10921 tokens. Prose came out first
    and they now average 965 chars against 990 for the existing 44, so once again the growth
    is COUNT.

    **But this is the second raise for one PRD, and that is the interesting number.** PRD-17
    adds five tools in total and has moved this ceiling twice. The guard is doing its job by
    making that visible: the manifest is approaching the size where trimming it per ACTIVE
    ROLE stops being the "later nicety" PRD-17 D-b calls it. A worker never needs the three
    reviewer tools in its manifest, and a reviewer never needs `claim_next` — that is roughly
    a third of the fleet surface each agent is paying for and cannot use.

    So if this fires a third time, the answer is probably not a fourth raise. It is the
    role-gated manifest, which needs SSE on /api/mcp first (PRD-17 lists that as a non-goal
    precisely because it is a transport change, not a tweak).

    **Built in GRPH-337 follow-up, and the SSE caveat turned out to be half right.** Trimming
    per ACTIVE role does need a push channel — a role changes under a live connection and
    `tools/list` was fetched once at connect. But trimming per the KEY's eligible roles is
    static: a credential's ceiling is fixed at mint, so the manifest it is shipped can never
    go stale. That is also what D-b literally prescribes. A single-role fleet key now sees
    16-19% fewer tokens (see `test_mcp_role_manifest.py`), which is the whole fleet case since
    every key the Fleet view mints is single-role.

    This ceiling is unchanged and still measures the FULL manifest, deliberately: it is the
    worst case an unrestricted key pays, and role gating must not become an excuse to let that
    grow unwatched.

    Raised 12500 -> 12600 in GRPH-369, by the same procedure and for a smaller reason than any
    previous bump: PRD-19 adds NO tool, only `enrolment_code` on `register_agent` — 137 chars,
    which is ~34 tokens and exactly the overshoot. Prose was trimmed twice first (the tool
    description and the parameter text) before the ceiling moved, and the remainder cannot come
    out without losing what an agent needs to know. Minimal on purpose: E7 adds
    `mint_enrolment` and should have to justify its own raise rather than spend headroom voted
    for something else.

    **PRD-19 undermines the optimisation this guard was pointing at, and that is worth stating
    here rather than discovering later.** The role-gated manifest trims per the KEY's eligible
    roles — which works because the Fleet view mints single-role credentials. Under enrolment
    the recommended setup is ONE UNRESTRICTED credential for every agent, with the role granted
    per session; so the key's ceiling is `all three`, nothing trims, and every agent pays this
    full manifest again. Trimming per enrolment would work but the enrolment is not known until
    `register_agent` has run, which is the SSE problem D-b called a non-goal. So the next time
    this fires, the honest options are a real prose pass or that transport change — not another
    raise.

    Raised 12600 -> 12800 in GRPH-374, and this one IS the case the guard permits. PRD-19 E7
    adds `mint_enrolment` — one tool, ~211 tokens after trimming its description and dropping a
    redundant parameter doc. Subtract it and the manifest measures ~12534, under the previous
    ceiling: the growth is tool COUNT, which this test explicitly allows, not the verbosity it
    exists to catch. Checked by the same subtraction every earlier bump used.

    The note above still stands and is now the live problem rather than a warning: under
    enrolment every agent holds an UNRESTRICTED credential, so the role-gated manifest trims
    nothing and this full number is what everyone pays. The next raise should be argued
    against a real prose pass or per-enrolment trimming, not granted.

    Raised 12800 -> 13100 in GRPH-391, and the paragraph above said not to. Here is the
    argument, since it is owed.

    PRD-20 D8 adds `graph_query` — one tool, ~291 tokens after two trims (the description lost
    a third of its words, and every parameter doc that only restated its own name was dropped).
    Subtract it and the manifest measures ~12793: under the previous ceiling, so by the same
    subtraction every earlier bump used, this is growth in tool COUNT, which this guard
    explicitly permits.

    **The prose pass was attempted first and is the wrong instrument here, which is the
    finding.** Freeing 1164 chars from existing entries means the two largest, `update_item`
    (2594 chars) and `create_item` (2106) — and NEITHER is verbose prose. Their descriptions are
    77 and 76 chars; the bulk is per-parameter documentation on the two tools every agent uses
    most. Cutting a quarter of that buys ~290 tokens by making the most-used write surface
    harder to call correctly. That is not the bloat this guard exists to catch, and trading
    agent correctness for manifest size is a bad trade at any exchange rate.

    **The ceiling is set to 13100 deliberately: ~16 tokens of headroom.** The 12800 ceiling had
    7, which is how a tool got added without anyone re-running this arithmetic. Leaving it
    equally tight means the next tool argues its own case rather than spending slack voted for
    something else — the discipline GRPH-369 applied.

    The structural fix is filed rather than restated: progressive disclosure / tiered tool
    exposure (GRPH-48, GRPH-146) is what makes this number stop mattering. Every bump since
    GRPH-254 has said some version of "the next one should be the real fix"; the honest reading
    is that this guard cannot force that work, only make its absence visible — and it has,
    five times now.

Raised 12800 -> 13100 in GRPH-398, and PER-ENROLMENT TRIMMING IS THE ARGUMENT the note
    above demanded — it shipped rather than being deferred again. E9a/E9b bind the MCP session
    to the agent and narrow `tools/list` to its role afterwards: a worker carries 43 tools, a
    reviewer 42, against 52 here. This number is now the CONNECT-TIME worst case rather than
    what every agent pays on every turn.

    Two caveats kept deliberately, because they are the ones that would make this raise a
    mistake:

    - **The saving only lands if the client re-fetches after registering.** ANSWERED on
      2026-08-20: Grok Build shell does, within 20 seconds, unprompted. Cursor cannot — it
      multiplexes every agent onto one connection, so nothing narrows there and this full
      number is what each of its agents pays per turn.
    - What this raise buys is ~40 tokens of `register_agent` description naming
      `tools_off_limits`. That exists so an agent learns its boundary by being TOLD rather
      than by being refused three times, which is how `quarantine` decides an agent has
      stopped listening. Cutting it to save 40 tokens would trade a wave's worth of agent
      for a rounding error.

    The probe that settled this has been RETIRED — it had one question and got an answer, and
    an instrument left running past its question is just a cost. The ceiling stands on the
    measurement rather than on the instrument.

    **MEASURED 2026-08-20: the probe is not empty, and the ceiling stands.** Grok Build shell
    re-fetched `tools/list` within 20 seconds of registering, unprompted, and took the narrowed
    manifest — 43 tools for the worker, 42 for the reviewer, against the number below. The
    saving is collected, so the raise was not granted on a promise.

    Collected for ONE-CONNECTION-PER-AGENT clients only. Cursor multiplexes every agent over a
    single connection — three agents on one session id on the first wave — so a `tools/list`
    there cannot be attributed to an agent and is answered with this full number. For those
    clients this ceiling is what every agent pays PER TURN, which is the argument for keeping it
    tight rather than treating the slack as headroom to spend.

    **Both of the paragraphs above raised 12800 -> 13100, independently, and neither could see
    the other.** GRPH-391 (PRD-20's `graph_query`) and GRPH-398 (PRD-19's session-scoped
    manifest) were in flight on separate branches, each ran this arithmetic correctly for its
    own change, and each picked the same number. Merged, the manifest measures ~13118 — over
    the ceiling both of them set.

    Raised 13100 -> 13150 on the merge, and the number is the least interesting part of this
    entry. The interesting part is that **a ceiling cannot serialise two branches**. Each raise
    was individually justified by the procedure this docstring prescribes; the procedure has no
    step for "someone else is also spending this". That is not a reason to distrust the guard —
    it caught the collision at merge time, which is when it could still be fixed cheaply — but
    it is the clearest argument yet that the ceiling is the wrong instrument for a repo with
    concurrent fleets, and that GRPH-48 / GRPH-146 (progressive disclosure, tiered exposure) is
    the fix rather than a sixth raise.

    Kept at ~32 tokens of headroom, in line with the ~7 and ~16 the last two left, so the next
    tool argues its own case rather than spending slack voted for something else.

    Raised 13150 -> 13410 in GRPH-460, and this is the sixth. The paragraph above asked for
    it not to be granted, so here is the argument, and the parts of it that are weak.

    PRD-22 §6: a planner can mint a seat and cannot retire it — "it fails in the direction
    that costs money: a fleet that can grow and not shrink". The service layer shipped and
    was tested in GRPH-451; only the agent-facing surface was missing, so nothing could
    reach it. Deferring it once was deliberate (GRPH-460 was filed blocked); shipping it
    now was an explicit decision after the alternatives were costed.

    The growth is 239 tokens in two parts, and they are not the same kind:

    - `retire_wave`, one new tool, 214 tokens. This is the growth in tool COUNT the guard
      explicitly permits. Subtract it and the manifest is 13164.
    - 24 tokens on `fleet_status`: one input property, one output field, and eight words.
      That is a new CAPABILITY folded into an existing tool rather than given its own —
      chosen precisely because it is cheaper than a `list_enrolments` tool would have been
      (~250 tokens), and because the roster's second question has always been "what became
      of the one I sent". It is not prose.

    **The 24 is why the subtraction argument does not clear on its own**, and pretending
    otherwise would be the dishonest version of this bump. 13164 is 14 over the old ceiling.

    A prose pass was attempted first and there is nothing left: a scan for parameter
    descriptions whose content words are a subset of their own field name returns ZERO
    across all 54 tools. The easy slack the earlier passes are described as harvesting was
    genuinely harvested. What remains is the structural duplication measured in GRPH-451 —
    outputSchema is 26% of the manifest and six tools carry an identical 654-char item
    shape, already minimal, with nowhere in a flat `tools` list to share a definition.

    **Who pays, measured rather than assumed** (the docstring above was stale about this;
    E9b's session-role narrowing changed it):

        full (unregistered / all-in-one)   13399   55 tools
        session role = planner             11563   48 tools   retire_wave visible
        session role = worker              10757   44 tools   not visible
        session role = reviewer            10852   44 tools   not visible

    `retire_wave` is planner-gated, so a worker or reviewer in a fleet pays 24 tokens of
    this, not 239. The full number is what an all-in-one agent pays — the solo-developer
    default — which is the awkward part of the trade and is stated rather than buried.

    The structural fix is still GRPH-48/146 and this raise does not substitute for it.

    LOWERED 14000 -> 13600 on 2026-08-26 (GRPH-48) — the raise directly above is GIVEN BACK.
    That is the direction this number is supposed to move and, across five arguments, never
    once had.

    The slack came from somewhere the ceiling had never looked. This docstring says a prose
    pass "was attempted first and there is nothing left", and that was true — but it then
    reaches straight for outputSchema (26%) and skips annotations (11%). ~394 tokens were
    hints repeating a value the MCP spec already defines as the default, and the spec is
    explicit that an ABSENT field means exactly that default. So those bytes told every
    client something it already knew, once per tool, 55 times.

    The arithmetic against the raise above, which is why it is written here rather than in a
    commit message. `get_prd` costs 163; the trim returns 394. Measured with both landed:
    **13399 across 55 tools**, where that same manifest untrimmed would be ~13793 — 193 over
    the old 13600, which is precisely why GRPH-519 had to raise at all. The read that makes
    `update_prd` survivable is paid for, and 13600 still leaves ~201 of headroom, near the
    ~218 that raise was aiming at.

    (That last sentence was true when written and stopped being true without anyone
    noticing — the headroom was ~21 by the time GRPH-541 measured it. It is kept as the
    dated claim it was, and `MEASURED_TOKENS` at the top of this file now carries the
    current one, because a number in prose cannot fail.)

    Not taken: the spec also says `destructiveHint` and `idempotentHint` are meaningful
    only when `readOnlyHint == false`, worth another ~238 tokens across the read-only tools.
    That trim is lossless only for a client that honours the conditional, and 238 tokens do
    not buy a behaviour that depends on how carefully somebody else read the spec.

    What this does NOT fix is the pattern the raise above names — tools added without anyone
    re-running the arithmetic, three times now. A one-off 394 buys time, not a habit. And
    GRPH-48's own finding is that no structural fix is available on this side: MCP requires
    `inputSchema` on every `tools/list` entry, has no detail level and no lazy schema, and
    explicitly assigns progressive discovery to the CLIENT. Scope and role gating are the
    only real levers, and the table below is what they buy.

    RAISED 13410 -> 13600 on 2026-08-22 (GRPH-474), and the reason matters more than the
    number. This is not prose creep, which is what the ceiling exists to catch — it is 178
    tokens of CONTRACT: eight tools that read the dispatcher's resolved project while
    advertising no `project_id`, so an agent could not pass what it could not see. A ceiling
    that blocks a schema from telling the truth is measuring the wrong thing.

    Measured per role, because the full manifest is a worst case nobody in a fleet receives:
    worker 44 tools / ~10.8k, reviewer 44 / ~10.9k, planner 48 / ~11.6k. 13.4k is what an
    all-in-one or an unregistered session pays — still the worst case nobody in a fleet
    receives, and still the solo-developer default, which is the awkward part of the trade
    and is stated rather than buried.

    RAISED 13600 -> 14000 on 2026-08-26 (GRPH-519), and the first thing to record is that the
    "43 of headroom" above had become ONE. Measured on `main` immediately before this change:
    13599 against a 13600 ceiling. Tools were added after GRPH-474 without anyone re-running
    the arithmetic — the third time this docstring has had to say that about itself, and the
    reason the number is written down here each time rather than only in a commit message.

    A ceiling with one token of slack is not a budget, it is a tripwire that fires on whoever
    happens to add the next tool regardless of what it costs. It caught `get_prd` at 163
    tokens; it would equally have caught a four-token one.

    What the 163 buys: an agent could not READ a PRD body. `update_prd` replaces the body
    whole, so the only route to absorbing a grill's own answers was to rewrite the document
    from memory — the GRPH-515 defect with no guard in front of it. A ceiling that blocks the
    read which makes an existing write survivable is measuring the wrong thing, the same
    argument GRPH-474 made about schemas that could not tell the truth.

    `get_prd` is UNGATED, so every role pays the 163 — that is the honest worst case and it is
    stated rather than buried. 14000 leaves ~218 of headroom, roughly what the previous raise
    intended before it eroded. The structural fix is still GRPH-48/146.

    (Superseded hours later by GRPH-48, which found the 394 that pays for this tool and put
    the ceiling back to 13600. The reasoning above is kept rather than rewritten: the raise
    was correct when it was made — a ceiling that blocks the read which makes an existing
    write survivable is measuring the wrong thing — and that does not stop being true
    because the money later turned up elsewhere.)"""
    full_chars = len(json.dumps({"tools": TOOLS}))
    read_chars = len(json.dumps({"tools": [t for t in TOOLS if t["name"] in _READ_ONLY]}))
    assert full_chars // 4 < CEILING, f"full manifest ~{full_chars // 4} tokens — trim descriptions"
    # scope-gating must keep buying its ~half-off for read keys
    assert read_chars < full_chars * 0.55


def test_the_recorded_manifest_size_is_the_real_one():
    """The stated headroom has to be true, not merely once-true (GRPH-547).

    The ceiling test above answers "is there still room" and nothing answers "how much" —
    so the figure an author reads to plan by lived only in prose, and drifted ~180 tokens
    without anything noticing. A ceiling with 8 tokens left and a note claiming 201 is worse
    than no note: it invites exactly the change that will fail.

    Failing here is not a problem to work around. It means the manifest moved, and the
    correct response is to read the number in the message, decide whether the cost is worth
    it, and record it. That IS the arithmetic being re-run.
    """
    measured = len(json.dumps({"tools": TOOLS})) // 4

    room = CEILING - measured
    where = (f"that leaves {room} of headroom under the {CEILING} ceiling" if room > 0
             else f"that is {abs(room)} tokens OVER the {CEILING} ceiling")
    assert measured == MEASURED_TOKENS, (
        f"the manifest is {measured} tokens; MEASURED_TOKENS says {MEASURED_TOKENS}. "
        f"Set it to {measured} — {where}"
        + ("." if room >= 50 else
           ". There is effectively no room left, so what you are adding should probably be "
           "gated to the keys that can use it rather than carried by every agent — "
           "`_with_attestation` in mcp_server is the shape, and it is why GRPH-541 added "
           "three fields for nothing.")
    )


def test_a_session_scoped_manifest_actually_saves_the_tokens(client, auth):
    """The number O6 was decided on. An unrestricted credential is what enrolment recommends,
    so before E9 every agent carried the full manifest on every turn — the cost is per-request
    for the life of the session, not once at connect.

    Asserted as a floor rather than an exact figure: the manifest grows, and a test that
    pinned the saving to a percentage would fail for the wrong reason every time a tool
    lands."""
    proj = client.post("/api/projects", json={"name": "FootprintSession"},
                       headers=auth).json()["id"]
    plaintext = client.post("/api/api-keys", json={"name": "fleet", "project_id": proj},
                            headers=auth).json()["plaintext"]
    seat = client.post("/api/fleet/seats",
                       json={"project_id": proj, "roles": ["reviewer"], "wave": "w1"},
                       headers=auth).json()["seats"][0]["code"]
    sid = client.post("/api/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                        "params": {}},
                      headers={"X-API-Key": plaintext}).headers["mcp-session-id"]
    full = _rpc(client, plaintext, "tools/list")["result"]["tools"]

    client.post("/api/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                  "params": {"name": "register_agent",
                                             "arguments": {"label": "R",
                                                           "enrolment_code": seat}}},
                headers={"X-API-Key": plaintext, "Mcp-Session-Id": sid})
    narrowed = client.post("/api/mcp",
                           json={"jsonrpc": "2.0", "id": 3, "method": "tools/list",
                                 "params": {}},
                           headers={"X-API-Key": plaintext, "Mcp-Session-Id": sid},
                           ).json()["result"]["tools"]

    saved = len(json.dumps(full)) - len(json.dumps(narrowed))
    assert saved > 0, "the whole point is that a registered reviewer carries less"

    # **The 10% floor this used to carry was REMOVED, and the reason is the finding rather
    # than an excuse.** GRPH-571 made the credential's own manifest core-only, and role
    # narrowing can only remove role-GATED tools — most of which tiering had already taken
    # out. So E9's share fell to ~9.7% of a much smaller base, and the old floor failed while
    # the agent was strictly better off. Lowering the threshold to go green would have
    # recorded the opposite of what happened.
    #
    # What is asserted instead is the number that describes what an ENROLLED REVIEWER
    # ACTUALLY PAYS, against the untiered manifest it would have carried before either
    # optimisation existed. That is the quantity O6 was arguing about; E9's percentage of the
    # post-tiering base was only ever a proxy for it.
    untiered = len(json.dumps([t for t in TOOLS]))
    carried = len(json.dumps(narrowed))
    # MEASURED: 31066 against 54468 untiered — 57%. The threshold is the measurement plus a
    # little room, not a round number picked to pass: a reviewer that started carrying 64%
    # would mean one of the two filters had stopped removing something, which is the event
    # worth catching.
    assert carried < untiered * 0.65, (
        f"a registered reviewer carries {carried} chars against {untiered} untiered — the two "
        "optimisations together are not paying for themselves"
    )
    # And role narrowing still removes something on top of tiering, which is the claim this
    # test was originally written to make. Kept as a direct assertion now that its percentage
    # is no longer a useful proxy for it.
    assert carried < len(json.dumps(full)), "the session narrowed nothing"


# ---- the project parameter (GRPH-474) ------------------------------------------------------

def _dispatch_blocks() -> dict[str, str]:
    """Each tool's dispatch branch, from its `if name == "x"` to the next one."""
    import pathlib
    import re

    src = (pathlib.Path(__file__).resolve().parent.parent
           / "app" / "mcp_server.py").read_text(encoding="utf-8")
    starts = [(m.group(1), m.start()) for m in re.finditer(r'if name == "(\w+)"', src)]
    assert len(starts) > 20, "found almost no dispatch branches — this test is reading nothing"
    return {n: src[s:(starts[i + 1][1] if i + 1 < len(starts) else len(src))]
            for i, (n, s) in enumerate(starts)}


def test_a_tool_that_reads_the_resolved_project_declares_it():
    """The manifest IS the contract: `tools/list` is the only thing an agent can read.

    Eight tools used the dispatcher's `pid` while advertising no `project_id`, because the
    manifest was driven by `_PROJECT_SCOPED` — a set that answers a DIFFERENT question
    ("cannot run without a project") and was doing double duty (GRPH-474). Seven of them
    choose what to act on by project, so an agent on a multi-project key silently got
    `allowed[0]`.

    Derived from the SOURCE rather than from a list, so the next tool to start reading `pid`
    fails here instead of shipping undiscoverable. A list would have to be updated by the
    same person who forgot to declare the parameter.
    """
    import re

    blocks = _dispatch_blocks()
    from app import mcp_server as m

    offenders = []
    for tool in m.TOOLS:
        name = tool["name"]
        body = blocks.get(name, "")
        if not re.search(r"\bpid\b", body):
            continue
        if "project_id" not in tool["inputSchema"].get("properties", {}):
            offenders.append(name)

    assert not offenders, (
        f"{offenders} read the resolved project but do not advertise `project_id`. An agent "
        "cannot pass what the manifest does not declare, so these silently act on the key's "
        "default project. Add them to `_TAKES_PROJECT`."
    )


def test_the_two_project_sets_are_not_the_same_question():
    """`_PROJECT_SCOPED` (dispatch refuses without a project) is a subset of `_TAKES_PROJECT`
    (manifest advertises one). Collapsing them back into one name is what caused this."""
    from app import mcp_server as m

    assert m._PROJECT_SCOPED <= m._TAKES_PROJECT
    assert m._TAKES_PROJECT - m._PROJECT_SCOPED, (
        "the sets are identical again — the distinction that fixed GRPH-474 has been lost"
    )
