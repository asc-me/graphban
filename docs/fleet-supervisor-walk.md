# PRD-22 acceptance walk — what it actually did

The walk is `fleet/tests/test_acceptance_walk.py`. It is **skipped unless pointed at a
real server**, because a walk that quietly passed by not running would be the worst
possible version of it.

```bash
# a real instance, built from the branch under test
DATABASE_URL=postgresql+psycopg://... .venv/bin/python -m alembic upgrade head
SEED_ON_START=false DATABASE_URL=... .venv/bin/python -m app.cli init --json
SEED_ON_START=false DATABASE_URL=... .venv/bin/python -m uvicorn app.main:app --port 8099

# then, from fleet/
GBFLEET_WALK_SERVER=http://127.0.0.1:8099 \
GBFLEET_WALK_KEY=gb_sk_... \
GBFLEET_WALK_PROJECT=<a SCRATCH project — the walk refuses a real one> \
GBFLEET_WALK_SEATS="PLANNER-AAAAAA PLANNER-BBBBBB" \
GBFLEET_WALK_DB=1 \
GBFLEET_WALK_PSQL="docker exec -i <pg> psql -U postgres -d <db>" \
    .venv/bin/python -m pytest tests/test_acceptance_walk.py -s
```

Two **planner** seats, because the walk needs a second minter to prove step 14's scope
against. `GBFLEET_WALK_SEATS` takes codes a human issued from the Fleet view — the path
that works against a deployed instance, where nobody has the operator's password on a
command line. `GBFLEET_WALK_JWT=<access token>` does the same over REST if you hold a
session. Issuing that first credential is a human's, by design (PRD-17 §D-e); the walk
has no way to mint its own way in, and that is the point.

The child is `fleet/tests/child_standin.py` — a genuine MCP client with the model
removed. It redeems a real single-use seat, reports a real worktree and branch, claims
real work, moves it to review and exits. What it stands in for — argv construction,
config placement, version pinning — is verified against real `claude`, `cursor-agent`
and `grok` binaries in `test_adapters.py`, so nothing about a vendor is being assumed
here; only the tokens are saved.

## Result — 16 passed, 1 blocked, 0 findings in the supervisor (1 in the walk itself)

Re-run 2026-08-22 against a fresh instance on `9d1936b`, the commit deployed to
`ubuntu-srv`. The first run (2026-08-21) reported 14 passed, 3 blocked; GRPH-460 closed
two of the three.

| # | step | result |
|---|---|---|
| 1 | planner registers; `claim_next` refused | `WALK-A13`, refused with *"claim_next requires role 'worker'"* |
| 2 | planner mints a worker seat and a reviewer seat | two distinct codes |
| 3 | two spawns → two agents, distinct ids, **distinct `enrolment_id`s** | `WALK-A14`, `WALK-A15`, two real seat UUIDs |
| 4 | neither declares parentage | both rows `NULL`, read from the database |
| 5 | a second supervisor refuses, naming the holder | refused, pid named |
| 6 | worker claims, builds, moves to review, exits | claimed `WALK-2` → `review`, `ps` shows none running |
| 7 | **reviewer claims its sibling's item and signs off** | `WALK-A15` signed off `WALK-2`, built by `WALK-A14` |
| 8 | after reap, no seat file survives | inside the worktree and out |
| 9 | killed worker salvaged, commit carries no credential | `gb/wave-kill-9`, key absent from `git log -p` |
| 10 | `orphans` lists the salvaged branch and nothing else | listed, `salvaged: true` |
| 11 | version mismatch refuses **at spawn** | naming the binary and the supported range |
| 12 | a silent child is killed inside the window, adapter named | killed at 3s, adapter reported |
| 13 | server unreachable: no spawns, seat unredeemed | nothing started |
| 14 | `retire_wave` revokes only the caller's seats | planner A revoked its own 2; planner B's 1 untouched |
| 15 | `reissue_enrolment` replaces a dead seat | **BLOCKED** — not on the MCP surface, deliberately |
| 16 | the fleet shrinks to zero without the Fleet view | grew to 1, revoked the seat, named the survivor, stopped it |
| 17 | which files each worker actually touched | measured off the branch: `['half-done.py']` |

**Step 7 is the one that matters.** Two children of one supervisor, on one credential,
holding two seats — and review between them still means something. If parentage had been
declared anywhere in the spawn path, `independent` would have refused there and the fleet
would be unable to review a single thing it built. That is D-b, observed rather than
argued.

**Step 14 needed a second planner before it meant anything.** "Revokes only the caller's
seats" is a claim about what is *spared*, and the first draft had nothing on the other
side of the boundary to spare — every seat in the project belonged to the one planner, so
an unscoped `retire_wave` would have passed it. That is the empty-set form of *absence
reads as clean*, and it is why the walk now registers a planner B, mints it a seat, and
asserts that seat is still `unused` afterwards. Sabotaged both ways — dropping the
`minted_by` filter from `retire_wave`, and again from `fleet_status` — the step fails.

**Step 16 turns on one field.** `retire_wave` returns `agents_still_running`, and the walk
asserts the lingering child is named in it. A server has no process control; the seat dies
and the child keeps executing until something local stops it. Without that field
`{"seats_revoked": 1}` reads as *the wave is over*, which is the misreading that leaves
children building in the dark. Sabotaged to return an empty list, step 16 fails.

**Step 15 is blocked on a decision, not a budget.** `reissue_enrolment` stayed off the MCP
surface: replacing a dead seat is a different capability from retiring your own wave, and
nothing was asking for it. The walk names it by number rather than reporting "16 of 17",
because a walk that passes by omission is the failure this whole exercise exists to catch.

**The branch guard fired for real, unprompted.** Step 16's second supervisor started its
slot counter at 1 and tried to take `gb/wave-1`, which step 9's salvage branch still holds.
It refused — *"forcing would attach this worker to somebody else's history, and
auto-suffixing would make the branch stop identifying the agent"* — and the step names its
own wave instead. Salvage keeping the branch alive is the whole point of salvage; a
supervisor quietly reusing it would have been the bug.

## What the walk found

**The walk was a loaded gun pointed at whichever project you named.** Step 6 has a worker
call `claim_next`, and `claim_next` takes the highest-priority ready item in the project —
not the one the walk created a moment earlier. Pointed at a real project it claims
somebody's work, attaches a fabricated evidence note, moves it to `review`, and has a
sibling sign it off as `done`. Step 6's assertion notices the wrong item afterwards, which
is four writes too late.

It was found the way these things are found — by using it. Two planner seats were issued
by hand for a run against `ubuntu-srv` and arrived minted against `agentledger`: 348 items,
53 of them ready to claim, `GRPH-466` at the head of the queue. The walk would have run.

`_refuse_a_real_project` now runs FIRST, before a single write, and refuses anything
holding an item the walk did not itself write. Verified by pointing it at a project with
one stray item: it refuses in 0.12s, names the item, and the item count is unchanged
afterwards — still `next`, still unclaimed. A second run against the walk's own scratch
project still passes, which is the case the guard must not break.

The near-miss is worth stating plainly: an acceptance walk is the most destructive test in
the repository, because its whole value is that it does real things to a real server. That
is an argument for a guard, not against the walk.


**A worker that registered and exited immediately was reported as a broken adapter.**
`await_registration` checked whether the process was still alive *before* it checked the
roster — so a fast child (register, find nothing to claim, exit) was gone by the first
poll and came back as `exited 0 before registering`.

D-c says exiting on empty is the **normal** end of a worker's life. The supervisor was
calling the most ordinary outcome a failure, and doing it in the most expensive way: the
operator goes and looks at the vendor for a fault that never happened.

No mock caught it, and the reason is worth keeping: mocked children sleep, and mocked
rosters always answer. It took a real server and a real fast child. `await_registration`
now asks the roster before concluding anything, with a control test proving that a child
which exited *without* registering is still reported as broken — the silent drop must
stay loud.

**Separately, `graphban init` provisions an operator who cannot log in** — filed as
GRPH-461. The walk needs a signed-in operator to mint its first seat, and could not get
one: `init` accepts any string as `--email` and reports `provisioned: true`, while
`/api/auth/login` validates with `EmailStr` and refuses it. Found on the first attempt
to run this.

## Delegate to seat (PRD-36)

On 2026-09-05 a live session called `delegate(seat=true)` on GRPH-720, this walk's own doc
item; `gbfleet mcp` spawned gbagent on the cheap tier onto the bound seat, registration
claimed the item, and the child moved it to review. That is criterion 18 observed rather than argued — the
delegation read claimed on the Live board within one poll, then finished when the child
advanced the item.

## Finishing the merge (GRPH-846)

`done` is a ledger state, not a git state. After step 7 the reviewed PR sat as a draft until
a person merged it, and every item `until` held on it (GRPH-798) was idle fleet time waiting
for that click. `--merge` is the click, opt-in, and this section says what was walked and
what was not.

**Walked, in-process, on 2026-09-10** (`fleet/tests/test_merge_after_sign_off.py`): a real
bare `origin`, a clone the wave runs in, and a second clone standing in for the forge. SA-420
depends on SA-417; SA-417 is `done` with its commit only on `gb/dep`. The wave holds SA-420
and names SA-417. Under `--merge` the loop reads SA-417's attestations, asks `gh` for the PR,
checks the head against the `fleet.sign_off` commit and CI's `suite_green` commit, marks the
draft ready, merges — the stand-in squashes onto `origin/main` at that moment, from the second
clone — writes the merge commit on SA-417 as a `url` receipt, re-fetches the base, and offers
SA-420 on the next tick. The re-fetch is load-bearing: with it deleted, the merge commit is
unknown to the clone and SA-420 stays held. That mutation was run and the walk failed; it also
failed with the receipt's `commit` dropped, with the hold not lifted out of the delegated set,
and with the loop's tick removed. A post-review push (same branch, different head) is left
alone with the reason naming both commits; comparing branch names instead fails that test.
Default-on fails the no-flag test on both commands.

**Bounced once, and the walk had passed.** The receipt carried `commit`, but the server's
`normalize_evidence` stored a `url` as `{kind, detail, url}` and dropped it — and the walk's
fake ledger extended the raw payload, so the hold lifted in the test against a ledger the
real server would never have shown. Which is the fake-passing shape AGENTS.md names: the
function was right, the boundary was not. Fixed on the server side — a `url` keeps the
commit it names, pinned in `backend/tests/test_merge_receipt.py` through the real normalizer
AND through `update_item` under a write key with no `gate` scope, which is what the
supervisor holds and why an attestation was never an option — and the fleet's fakes now store
what the server stores rather than what was sent.

**Not walked: a live forge.** No run from this seat reached GitHub — `gh` is refused inside a
fleet child by design, which is the right refusal and also why this is a stand-in. Two things
a live walk must confirm before this is trusted on a real trunk:

1. `gh pr merge --squash --auto` on a PR already `CLEAN` merges at once rather than only
   arming auto-merge. The code handles both — an armed-but-unmerged PR is reported `MERGE
   PENDING` and re-asked — so the live question is only which line the summary prints.
2. `mergeStateStatus` is `UNKNOWN` for a moment after a push while the forge recomputes it.
   The merger reports that as not-yet and re-asks after `MERGE_RECHECK_S`; the live question
   is whether one interval is enough or the first tick after CI needs a second.

Branch protection on the trunk is the backstop and is not bypassed: `BLOCKED` is `MERGE
SKIPPED`, and a merge the forge refuses is the forge's reason on the summary line.

## Stacked slices (GRPH-847)

`until` resolves its base once at startup as the remote's default ref and cuts every child
from it. That is right for independent items. For a PRD whose slices strictly depend on each
other (S1 → S2 → S3), each slice is held (GRPH-798) until the previous one is merged to main,
so the wave serialises on a person's merges.

`--base <branch>` cuts children from `origin/<branch>` instead of the default ref, and the
GRPH-798 dependency check reads "merged into `<branch>`" — so a slice that landed on the
integration branch unblocks the next slice without waiting for a trunk merge. PRs are
proposed against the integration branch. The operator merges it to main once at the end, as
one reviewed PR whose parts were each reviewed already.

**When to use it:** a PRD whose slices are sequential and each depends on the previous one
landing. Create the integration branch on the remote, run `gbfleet until --prd <id> --base
<integration>`, and merge the integration branch to main when the wave finishes.

**When not to:** independent items, or items whose dependencies are already on main. The
default behaviour (cut from the remote's default ref) is right for those — `--base` is the
exception for stacked slices, not the rule. A `--base` that does not exist on the remote
refuses at startup; it does not fall back.
