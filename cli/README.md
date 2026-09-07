# graphban-cli

**`gban`** — the client for a human at a terminal.

Five surfaces existed before this and none of them was for a person at a shell prompt:
`graphban` talks to the database from inside the container, `gbfleet` supervises processes,
`gbagent` is a spawned child, the web app is a browser, and `/api/mcp` is for agents. Issuing
a seat, seeing why an agent is stuck, or re-tasking one meant opening a browser.

Specified by [PRD-40](https://github.com/asc-me/graphban/blob/main/docs/prd-40-gb-cli.md).

## Install

Not on PyPI yet, so it installs from the repository. `uv tool install` puts it on your PATH
in its own environment, which is what you want for a CLI:

```bash
uv tool install "git+https://github.com/asc-me/graphban.git#subdirectory=cli"
```

Add `gbfleet` too if you run waves — it is a separate package, and `gban fleet` hands off to it:

```bash
uv tool install "git+https://github.com/asc-me/graphban.git#subdirectory=fleet"
```

`uv tool update-shell` once, if uv says the bin directory is not on your PATH. Upgrade either
with `uv tool upgrade graphban-cli` (or `--all`); reinstalling from the same URL also works,
since the spec is a branch rather than a pin.

With pip instead, into an environment you already have:

```bash
pip install "graphban-cli @ git+https://github.com/asc-me/graphban.git#subdirectory=cli"
```

`gban` pulls **nothing**: `client.py` is `urllib.request` throughout, and the install lands
exactly one distribution. `gbfleet` brings httpx and its four transitive dependencies, which
is why they are separate packages and not one.

```bash
gban login --server https://cloud.agentldgr.dev
gban doctor                       # both halves: the ledger, and the local fleet
gban agents                       # the roster, and why an agent is stuck
gban agents role SA-A4 planner    # what used to need a browser
gban seats issue worker worker planner                  # one entry per agent
gban keys                                               # which key is that agent on
gban fleet up --seats-file seats.txt --adapter claude   # hands off to gbfleet
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

## `gban login` wants a real terminal

It refuses without one, rather than prompting. `getpass` falls back to a plain **echoing**
read when it cannot turn echo off — it warns, but the warning arrives after the person has
decided to type — so a login through a pipe, a heredoc or an editor's command runner would
put the password in the scrollback. There is no non-interactive login yet (PRD-40 open
question 2: an API key cannot reach the JWT routes, so CI would need a service session).

## Why not `gb`

Because `gb` is already `git branch` on a large share of developer machines, and **an alias
beats a binary on `PATH`**. The deployed walk hit it on the very first command and got git's
usage text; nothing inside the process can detect that, because by the time `gb` would have
run, the alias did not.

It is not one alias but a whole namespace. oh-my-zsh's git plugin — which is where most of
these come from — defines sixteen `gb*` aliases and ten `grb*`, so `gb`, `gba`, `grb` and
`gbl` are all spoken for. `gban` is outside it, still short, and still says which product it
belongs to.

## Licence — Apache-2.0, deliberately not the repository's FSL-1.1

The repository is [FSL-1.1-Apache-2.0](https://github.com/asc-me/graphban/blob/main/LICENSE.md). This directory is
[Apache-2.0](https://github.com/asc-me/graphban/blob/main/cli/LICENSE), for the reasons PRD-22 §8 gives for `fleet/` — every one of which
applies here identically. `gban` is inert without a Graphban server and holds no authority of
its own, so FSL's Competing Use clause protects the server and protects nothing here. It is a
laptop-installed developer CLI, which is exactly the kind of dependency that has to clear a
corporate licence policy scanner.

## Why it is in this repository

**Not a second repository**, for the reason [`fleet/README.md`](https://github.com/asc-me/graphban/blob/main/fleet/README.md) gives for
the supervisor, with more force: the client↔server contract has no schema anywhere, and a
cross-repo break would present as absence reading clean — `gban` still runs, nothing errors, the
verb quietly stops meaning what it said. The evidence is recent and specific: `ROLES` lost
`reviewer` in one PR while another added a test naming it, and CI caught the pair inside
seventeen minutes because both lived in one repository. Split across two, that lands as a bug
report from somebody whose `gban agents role ... reviewer` started refusing.

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
