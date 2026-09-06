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
gb seats issue --roles worker,reviewer
gb fleet up --seats-file seats.txt --adapter claude   # hands off to gbfleet
```

## What it is not

It is **not a second web app**: no board, no PRD editor, no search. Every verb is either
something a human currently opens a browser for, or a diagnosis nothing else gives.

It is **not `graphban`**, which talks to the local database from inside the container and
stays exactly as it is. Mixing "against the DB in the container" and "over HTTP from a laptop"
into one command is an ambiguity that ends with somebody purging the wrong instance.

It **holds no state the server does not** and computes nothing the server computes. Every verb
is one endpoint, called once (PRD-40 D11); an ordering between two calls would be a rule, and
a rule in the client is a second definition of something the server already enforces.
