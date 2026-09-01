# Using Graphban with Swamp

[Swamp](https://swamp-club.com/) is a separate product: agents author typed models of
external systems, wire them into workflows, and every method run produces immutable data.
Graphban is the tracker, memory, and MCP surface those agents already talk to.

**They sit beside each other. They are not one install.** Graphban's core never learns the
word "Swamp." No Swamp code is copied into this repo. `docker compose up` still runs fully
offline.

**The joint loop is live.** `@graphban/item-lifecycle` lives in its own repository
([asc-me/graphban-swamp](https://github.com/asc-me/graphban-swamp), GRPH-613 / GRPH-623).
Swamp calls Graphban over HTTP: adapters push an `attestation`; Graphban never calls out.
Design and the licensing boundary: [swamp-integration.md](swamp-integration.md). Spec:
**GRPH-P26**.

## What works today

| Half | What you get |
| --- | --- |
| Graphban | Tracker, memory, MCP, a completion gate that refuses `done` without an attestation |
| Reviewer | `fleet.sign_off` mints an attestation (offline, no Swamp) |
| CI | `scripts/attest_ci.py` on the `ci` gate job (`suite_green`) |
| Swamp `complete` | Attest adapter `swamp` and move to `done`, or refuse naming the predicate |
| Swamp `probe` | Apply a mutation, run the suite, restore; attest `sabotage_observed` from the **count**, not a typed `tests_failed` |
| Swamp `conform` | Score plan steps; empty list is a refusal (nobody looked); justification required when not `implemented` |

## 1. Graphban

This project's ledger is the self-host on **ubuntu-srv** (`http://ubuntu-srv:8080`).
`cloud.graphban.dev` is a different database — CI pointed there 404ed every `GRPH-*` key.

Local empty stack: [getting-started.md](getting-started.md). Deploy: [deploy.md](deploy.md).

## 2. Connect an agent (MCP)

Mint an **Agent key** in **Settings → API Keys**. It talks to the tracker. It does **not**
carry `gate` — an agent that could attest its own work would make the completion gate
theatre. Full client table: [mcp.md](mcp.md).

## 3. Make completion work without Swamp

`update_item(status="done")` refuses unless a valid `attestation` exists for the commit.
Ordinary agent keys cannot write one.

- **Reviewer:** mint a **Gate key** (`read`+`write`+`gate`). `sign_off` with a `commit`.
- **CI:** this repo's `ci` job runs on the self-hosted runner `graphban-ledger` on
  ubuntu-srv. Repository **variable** `GRAPHBAN_URL=http://127.0.0.1:8080` (origin only —
  the script appends `/api/mcp`). **Secret** `GRAPHBAN_GATE_KEY` minted **on that
  instance**. Missing settings skip loudly (exit 0); a green log is not evidence of an
  attestation.

`REQUIRED_PREDICATES` is empty by default. CI emits `suite_green`. Swamp `probe` emits
`sabotage_observed`. Swamp `complete` emits `sabotage_effective`.

## 4. Install Swamp (the machine that runs agents)

Not inside Graphban's Docker stack. Not on GitHub-hosted runners.

```bash
curl -fsSL https://swamp-club.com/install.sh | sh    # review first
exec $SHELL
swamp version
swamp doctor install
```

## 5. Wire this checkout to the adapter

Once per clone. Do **not** `swamp repo init --force` on a tree that already has a vault.

```bash
git clone git@github.com:asc-me/graphban-swamp.git ../graphban-swamp   # sibling, not this tree
cd /path/to/agentledger
swamp repo init --tool none                    # skip if .swamp.yaml already exists
swamp extension source add ../graphban-swamp   # or an absolute path
swamp vault create local_encryption secrets    # skip if vault "secrets" exists
swamp vault put secrets graphban-api-key       # Gate key from ubuntu-srv; stdin; ~46 chars
```

Definition names are **lowercase**. The Graphban key stays uppercase on `itemKey`:

```bash
swamp model create @graphban/item-lifecycle grph-123 \
  --global-arg itemKey=GRPH-123 \
  --global-arg graphbanUrl=http://ubuntu-srv:8080 \
  --global-arg 'graphbanApiKey=${{ vault.get("secrets", "graphban-api-key") }}'
```

```bash
# complete — refuses without sabotage when effort >= 3
swamp model method run grph-123 complete --input commit="$(git rev-parse HEAD)"

# probe — measure a mutation (paths relative to this checkout)
swamp model method run grph-123 probe \
  --input commit="$(git rev-parse HEAD)" \
  --input file=../graphban-swamp/extensions/models/graphban.ts \
  --input 'old=export const ADVERSARIAL_EFFORT_THRESHOLD = 3;' \
  --input 'new=export const ADVERSARIAL_EFFORT_THRESHOLD = 99;' \
  --input 'tests=node --experimental-strip-types --test ../graphban-swamp/extensions/models/graphban_test.ts'
```

`probe` refuses (no Graphban write) if the mutation does not land exactly once, the
baseline is already red, or the tree is not restored. A mutation that breaks nothing is
attested `sabotage_observed` `passed: false` — omitted would look un-probed.

```bash
swamp model method run grph-123 conform \
  --input commit="$(git rev-parse HEAD)" \
  --input 'steps=[{"id":"d1","score":"implemented"},{"id":"d2","score":"deviated","justification":"section renamed"}]'
```

Empty `steps` is a refusal: nobody looked is not a clean pass.

Commit `.swamp.yaml`. Do not commit `.swamp/`, vault ciphertext, or a nested copy of
`graphban-swamp/`.

## Licensing

Graphban is `FSL-1.1-Apache-2.0`. Swamp is AGPL-3.0 plus an extension exception that
**forbids copies of any Swamp source**. The adapter is original TypeScript in
`graphban-swamp`. Do not paste files from `systeminit/swamp` into either repo.

## Related

- [swamp-integration.md](swamp-integration.md) — why the pair is stronger; remaining P26 gates
- [mcp.md](mcp.md) — scopes, attestation, completion refusals
- [getting-started.md](getting-started.md)
- [configuration.md](configuration.md) — `REQUIRED_PREDICATES`, `PR_COOLDOWN_SECONDS`
- [Swamp manual](https://swamp-club.com/manual) · [adapter repo](https://github.com/asc-me/graphban-swamp)
