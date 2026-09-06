# PRD-40 — `gb`: a client for the human at a terminal

**Ledger id:** GRPH-P40
**Status:** approved — v1.0, approval earned by finishing the grill: two rounds, eight questions each, both answers graded, every dimension closed. The answers are absorbed below and one changed a decision (D10: the two tools now share a directory and no file).
**Depends on:** PRD-17 (roles, the roster) · PRD-19 (seats) · PRD-22 (the supervisor and its licence boundary) · AL-59 (session tokens and revocation) · GRPH-774 (a human can re-task an agent)
**Touches:** a new `cli/` package (`graphban-cli`, entry point `gb`) · `backend/app/routers/*` (only if a needed act has no route) · `docs/api-reference.md` · `fleet/README.md`
**Complemented by:** `graphban` (the operator's DB-side tool) · `gbfleet` (the supervisor) · the web Fleet view

---

## 1. Overview

Five surfaces exist and none of them is for a person at a shell prompt.

| entry point | who | talks to |
|---|---|---|
| `graphban` / `agentledger` | a self-host operator | the local **database**; runs in the container |
| `gbfleet` | a human running a fleet | Graphban over HTTP (two tools) plus local processes |
| `gbagent` | a spawned child | a model endpoint |
| the web app | a human in a browser | REST |
| `/api/mcp` | agents | the ledger |

So a human who wants to issue a seat, see why an agent is stuck, or re-task one has exactly
one option: open a browser. That is the gap this closes, and the trigger was a real question —
"how do I create a supervisor?" — whose honest answer is that there is nothing to create, you
run a process, and nothing in any surface says so.

`gb` is a thin HTTP client. It holds no state the server does not, computes nothing the server
computes, and stops at the edge of what a browser is genuinely better at.

### 1.1 What this is not

- **Not a second web app.** No board, no PRD editor, no search. Every verb has to be something
  a human currently opens a browser for, or a diagnosis nothing else gives.
- **Not a rewrite of `graphban`.** That tool talks to the database from inside the container
  and stays exactly as it is. Mixing "against the local DB" and "over HTTP from a laptop" into
  one command is an ambiguity that ends in somebody purging the wrong instance.
- **Not a supervisor.** `gb fleet …` hands off to `gbfleet` and adds nothing of its own.
- **Not an agent surface.** Agents have MCP. Nothing here is for them.

## 2. Problem

### 2.1 The human has no terminal client

Sixteen fleet routes are session-authenticated and reachable only from the web app. Issuing
seats, reading the roster, re-tasking an agent, ending a wave: all browser-only. A person who
lives in a terminal — which is where every other part of this system is driven from — has to
change tools to do the smallest administrative act.

### 2.2 The setup question has no single answer

"Is my fleet set up correctly?" is currently answered by `gbfleet doctor` for the local half —
repo, workspace, adapter binary, seats file — and by nothing at all for the other half: whether
the credential is valid, whether the project exists, whether any agent is quarantined, whether
an agent is being refused every call it makes. The two halves fail in each other's terms, and
diagnosing this morning's stuck worker needed a database query.

### 2.3 Discoverability is the actual complaint

`gbfleet --help` tells a reader what the supervisor does once they know the supervisor is the
thing they want. Nothing tells them that. A single front door with a `--help` that lists the
verbs is most of the fix, and it is the part no amount of documentation has achieved.

### 2.4 The operator tools cannot be laptop-installed

`graphban` lives in `backend/`, which pulls fastapi, sqlalchemy, pgvector, psycopg, alembic,
redis and cryptography. `fleet/` refuses those deliberately and a test derives the forbidden
set from the backend's own list. So the existing human-ish CLI cannot be installed where humans
work, and the one that can is licensed and scoped to be extractable.

## 3. Goals

1. A human can do, from a terminal, the administrative acts that today require the web app.
2. One command answers "is this set up correctly", across the ledger and the local fleet.
3. `gb --help` is a complete answer to "what can I do from here".
4. It installs on a laptop without a database driver.
5. It adds no second definition of any rule the server already enforces.

## 4. Non-Goals

- Reading or editing PRDs, items, memory or the board.
- Any offline or cached mode. A tool that answers from a stale cache is worse than one that
  says the server is unreachable.
- Replacing `gbfleet` or `graphban` as names.
- Scripting conveniences that encode policy — no "spawn N until the backlog is empty".

## 5. Key decisions

| # | Decision | Detail |
|---|---|---|
| D1 | `gb` is a new, thin, HTTP-only client | It never opens a database connection, and there is no code path that could. That is the whole distinction from `graphban`, and it is enforced by the package having no driver to import. |
| D2 | It lives in its own package, `cli/` → `graphban-cli` | Not `backend/`, whose dependencies cannot go on a laptop (§2.4). Not `fleet/`, which is Apache-2.0 and deliberately extractable (PRD-22 §8) — putting ledger commands there would entangle the one component the repository has decided it may give away. A third package is a real cost and this is what it buys. |
| D3 | `gb login` obtains a session and stores it at `~/.graphban/session.json`, chmod 600 | The human routes are JWT-only, so an API key cannot reach them. Access token in memory for the invocation; refresh token on disk. **One exchange per invocation, before any verb runs**: `POST /api/auth/refresh` issues a new pair but does NOT consume the presented token — validity is keyed on `user.token_version`, which moves only on logout or a password change — so two `gb` processes never disturb each other and `session.json` is rewritten only by `gb login`, never per call. An expired token and a revoked one both return 401 and are NOT told apart: the person does the same thing either way, and a distinction here would be false precision. `gb logout` posts to the server's logout (204, no body, bumps `token_version`, killing every token on every device); a 401 means it was already dead and is not an error; **no response at all still deletes the file**, exits 0, and says what did not happen — a person who typed logout, saw an error and left a live token on disk is the outcome worth avoiding. Never logged, never echoed, never in argv. |
| D4 | An API key is accepted where it is sufficient, and refused clearly where it is not | `GRAPHBAN_API_KEY` works for the MCP-shaped reads a key can already make. A key-only caller reaching a JWT route is told which act needs a session and that `gb login` is the way, rather than a 401. |
| D5 | `gb fleet …` is a PASS-THROUGH to the `gbfleet` binary | A subprocess, not an import. It keeps the licence boundary intact, keeps `gb` free of the supervisor's dependencies, and means the supervisor's own `--help` stays authoritative. **`gbfleet`'s exit code is returned unchanged, including zero.** Exit codes here carry meaning — 75 is `EXIT_STUCK`, 69 an unreachable model endpoint, 55 qwen's budget — and folding them into a "pass-through failed" code would destroy a taxonomy the supervisor's own tests pin. `gb` contributes only codes that cannot collide: 3 for an expired session, 4 for `gbfleet` not installed. The child gets `--server` and `--project` as arguments and the credential as `GBFLEET_API_KEY` in its inherited environment — never argv, which is world-readable in `ps`, and never a temp file, which puts a credential at rest with a cleanup path to get wrong. Same-user `/proc` readability is the boundary that already exists: PRD-22 D-k says plainly the supervisor is not a security boundary. |
| D6 | v1 is exactly six verb groups | `login/logout/whoami`, `doctor`, `seats`, `agents`, `keys`, `fleet`. Each is either browser-only today or a diagnosis. Anything else waits for somebody to want it. |
| D7 | `gb doctor` answers both halves and says which half each line came from | Ledger side: credential valid, project reachable, roster summary, agents quarantined or being refused. Local side: whatever `gbfleet doctor` reports, run as a subprocess. A line that does not say where it came from sends people to the wrong machine. **Neither half may silence the other**: a half that could not be checked prints UNKNOWN with its reason, never a pass and never a fail, mirroring `gbfleet doctor`'s existing three states. The three ledger failures are three distinct lines because they send a reader to three different places — `UNKNOWN ledger unreachable: <error>`, `FAIL ledger credential rejected`, `FAIL ledger project 'x' not found for this credential`. Exit code is the WORST finding across both halves, never whichever ran last. |
| D8 | Refusals are shown as the server phrased them | The `hint` field is already the machine-readable next step (AL-47) and is prose today — "pass agent_id — the value register_agent returned", "register again with a fresh enrolment code from the Fleet view". Re-wording it in the client creates a second copy of a rule that will drift, and the server's phrasing is the one under test. If a hint ever arrived structured, `gb` prints the structure and looks broken — which is correct, because the alternative is a translation table in the client; the fix belongs on the server, and a test asserting hints are non-empty prose belongs there too. The client never parses a detail string to decide anything. |
| D9 | Human output by default, `--json` for machines | Nothing is parsed out of the human format anywhere. |
| D10 | Config precedence: flag, then environment, then `~/.graphban/gb.json` — and the two tools share a DIRECTORY, not a file | **Amended by the grill.** The draft had `gb` reading `graphban`'s `config.json` and ignoring the keys it did not know. That is true and insufficient: the risk is not `gb` misreading a database password, it is the password living in a file that now has a second consumer and a second reason to be copied. So `gb` reads `~/.graphban/gb.json` (`url`, `project`) and `~/.graphban/session.json` (the refresh token), and **never opens `config.json` at all** — a property a test asserts by filename rather than a promise about which keys get parsed. `graphban` runs in a container against the database; `gb` runs on a laptop against HTTP; the credential that must not cross that line lives in its own file, so copying a config never carries it. `gb login` writes `gb.json` if it is absent. |
| D11 | Every verb maps to ONE existing route, called ONCE | Literal, and checked: `gb seats issue` is `POST /api/fleet/seats` (the wave name defaults server-side, validation is server-side, the codes come back in the response); `gb agents` is `GET /api/fleet`; `gb agents role` is `PUT /api/fleet/agents/{id}/role`; `gb keys` is `POST /api/fleet/keys`. No verb needs a validate-then-create sequence. If one ever does, that ordering IS a rule, and a rule in the client is the second definition this exists to prevent — the answer is a server route that performs the transaction. The token exchange of D3 is not an exception: it is transport, carries no policy, and runs before any verb. |

### D3 — the auth decision, stated plainly

This adds a second credential path to a repository that has been careful to have one. The
argument for it: the acts a human needs are session-gated by design — a role change is an
authority act and the audit trail must name a person, not a key. An API key that could re-task
an agent would be a key that can promote its own agents, which is the containment PRD-19 exists
for, wearing a different hat.

So the token file is the cost, and it is bounded: refresh token only, `chmod 600`, revoked
server-side by `gb logout`, and a stale file produces a clear "session expired, run `gb login`"
rather than a mysterious 401. What it must never become is a place to keep an API key.

## 6. Acceptance criteria

Each is a test. Sabotage the call, not the model.

1. `gb` imports no database driver and no web framework. Asserted by walking the installed
   package's imports, the way `fleet/tests/test_packaging.py` derives its forbidden set.
2. `gb login` writes `~/.graphban/session.json` at mode 600 and stores no access token; a
   second invocation reuses it without prompting. Sabotage: store the access token and the
   mode test still passes while the file outlives its own expiry.
3. `gb logout` calls the server's logout and the stored refresh token stops working — checked
   against the server, not by deleting the file.
4. An expired session produces "session expired, run `gb login`" and exit code 3, not a 401
   traceback.
5. `GRAPHBAN_API_KEY` alone can run the verbs a key can reach, and `gb agents role` with only
   a key says which act needs a session.
6. `gb doctor` labels every line with the side it came from, and reports the ledger half even
   when `gbfleet` is not installed.
7. `gb fleet up --help` shows the supervisor's own help, proving the pass-through rather than
   a re-implementation. With `gbfleet` absent, `gb fleet` says how to install it and exits 4.
8. `gb agents` shows an agent's last refusal when there is one, so the stuck-worker case is
   answerable without a browser or a database. `last_refusal` is one JSON column on `agents`
   (`{tool, reason, count, at}`, migration 0115, GRPH-774), already on the roster row that
   `GET /api/fleet` returns — so this renders a field and adds no route. Refusal HISTORY lives
   in `events` where `action = 'role_refused'` and is deliberately not a v1 verb: the current
   state answers "why is this agent stuck", which is the question that prompted this PRD.
9. `gb agents role <id> planner` on a credential that permits only `worker` prints the server's
   own refusal, unedited, and exits non-zero.
10. `--json` output is parsed in the test rather than eyeballed; the human format is asserted
    to be absent from it. **No automated assertion reads the human format at all**, which is
    what keeps it free to improve — a server that rewords a success message breaks no test.
11. The credential appears in no `gb` output: not in `--json`, not in any verbose mode, not in
    an error. Sabotage: echo the environment passed to `gbfleet` and this fails.
12. Every verb's HTTP call names a route that exists in `docs/api-reference.md`. Sabotage: add
    a verb calling an invented path and this fails.
13. A walk on the deployed instance: `gb login`, `gb doctor`, `gb seats issue`, `gb agents`,
    `gb agents role`, recorded as `note` evidence.

## 7. Phasing

**PR 1 — the package and the session.** `cli/`, `gb login/logout/whoami`, config precedence,
the import guard. Criteria 1–5, 10, 11.

**PR 2 — the diagnosis.** `gb doctor`, both halves, and `gb fleet` as a pass-through.
Criteria 6, 7.

**PR 3 — the acts.** `gb seats`, `gb agents`, `gb keys`. Criteria 8, 9, 12. Then criterion 13.

## 8. Risks and open questions

### Risks

- **A second credential path.** Answered in D3, but it stays the largest thing here: a token
  file on a laptop is a token file on a laptop.
- **Verb drift.** A route gains a parameter and the CLI silently lags. D11 keeps verbs mapped
  one-to-one, and criterion 11 checks the paths exist, but nothing checks the parameters.
- **Scope.** Every "while you're there" verb makes this a second web app. The v1 list is short
  on purpose and should be defended.
- **A third package to release.** Version skew between `gb` and the server is a support
  question this repository does not have yet.

### Open questions

1. Should `gb` eventually absorb `gbfleet`'s verbs rather than pass through — one binary — or
   is the pass-through the permanent shape? D5 says pass-through for now and is not sure.
2. Should `gb login` support a non-interactive mode for CI, and if so, with what credential?
   An API key cannot reach the JWT routes, so CI would need a service session, which is a
   bigger decision than this PRD.
3. Does `gb doctor` belong in `gb` at all, or should `gbfleet doctor` learn the ledger half and
   `gb doctor` simply call it? That would put both halves in the extractable package.
4. **Closed by the grill: no.** One file for two trust models is how a laptop token ends up in
   a container. D10 now shares a directory and nothing else, and the separation is cheap today
   and expensive later.
