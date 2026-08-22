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
GBFLEET_WALK_PROJECT=walk \
GBFLEET_WALK_JWT=<operator access token> \
GBFLEET_WALK_DB=1 \
GBFLEET_WALK_PSQL="docker exec -i <pg> psql -U postgres -d <db>" \
    .venv/bin/python -m pytest tests/test_acceptance_walk.py -s
```

The child is `fleet/tests/child_standin.py` — a genuine MCP client with the model
removed. It redeems a real single-use seat, reports a real worktree and branch, claims
real work, moves it to review and exits. What it stands in for — argv construction,
config placement, version pinning — is verified against real `claude`, `cursor-agent`
and `grok` binaries in `test_adapters.py`, so nothing about a vendor is being assumed
here; only the tokens are saved.

## Result — 16 passed, 1 blocked, 0 findings

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
