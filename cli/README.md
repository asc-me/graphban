# graphban-cli

**`gb`** — the client for a human at a terminal.

Five surfaces existed before this and none of them was for a person at a shell prompt:
`graphban` talks to the database from inside the container, `gbfleet` supervises processes,
`gbagent` is a spawned child, the web app is a browser, and `/api/mcp` is for agents. Issuing
a seat, seeing why an agent is stuck, or re-tasking one meant opening a browser.

Specified by [PRD-40](https://github.com/asc-me/graphban/blob/main/docs/prd-40-gb-cli.md).

```bash
gb login --server https://cloud.agentldgr.dev
gb doctor                       # both halves: the ledger, and the local fleet
gb agents                       # the roster, and why an agent is stuck
gb agents role SA-A4 planner    # what used to need a browser
gb seats issue worker worker planner                  # one entry per agent
gb keys                                               # which key is that agent on
gb fleet up --seats-file seats.txt --adapter claude   # hands off to gbfleet
```

`seats issue` takes **one role per agent, repeats included**, because that is the server's
own shape: two agents on one seat share a session and cannot review each other. Each code is
printed once and written nowhere — a CLI that helpfully saved them would invent a second
credential at rest that no route and no test knows about.

`agents` prints what an agent was **last refused, and why**. That line is the reason the verb
exists: a roster saying "idle worker" for an agent being told no on every call it makes is
what made the Super-Arc diagnosis take a database query.

`agents role` re-tasks a live agent **within its credential's ceiling** and never past it. A
role the key does not permit is the server's refusal, printed in the server's own words;
widening a ceiling means minting a different credential, and keeping those two acts apart is
the point of having a ceiling. It lands on the agent's next poll.

## `gb` may already be taken on your machine

`gb` is a common shell alias for `git branch`, and an alias WINS over a binary on `PATH` —
the deployed walk hit this on the first command and got `git branch`'s usage text. Check with
`type gb`; if it is aliased, either `unalias gb` or call it by path. Nothing here can detect
that from inside the process: by the time `gb` runs, the alias did not.

## Why it is in this repository

**Not a second repository**, for the reason [`fleet/README.md`](../fleet/README.md) gives for
the supervisor, with more force: the client↔server contract has no schema anywhere, and a
cross-repo break would present as absence reading clean — `gb` still runs, nothing errors, the
verb quietly stops meaning what it said. The evidence is recent and specific: `ROLES` lost
`reviewer` in one PR while another added a test naming it, and CI caught the pair inside
seventeen minutes because both lived in one repository. Split across two, that lands as a bug
report from somebody whose `gb agents role ... reviewer` started refusing.

**Not inside `backend/`**, because `graphban-api` pulls fastapi, sqlalchemy, pgvector, psycopg,
alembic, redis and cryptography, and this installs on a laptop. `tests/test_packaging.py`
derives its forbidden set from the backend's own dependency list rather than a denylist
somebody maintains.

## What it is not

It is **not a second web app**: no board, no PRD editor, no search. Every verb is either
something a human currently opens a browser for, or a diagnosis nothing else gives.

It is **not `graphban`**, which talks to the local database from inside the container and
stays exactly as it is. Mixing "against the DB in the container" and "over HTTP from a laptop"
into one command is an ambiguity that ends with somebody purging the wrong instance.

It **holds no state the server does not** and computes nothing the server computes. Every verb
is one endpoint, called once (PRD-40 D11); an ordering between two calls would be a rule, and
a rule in the client is a second definition of something the server already enforces.
