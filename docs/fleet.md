# Fleet

The **Fleet** page shows who is working on this project and what they can be spawned as.
This is the user guide to the page. The operator runbook for running a remote fleet is
[fleet-remote.md](fleet-remote.md); adapters are in [fleet-adapters.md](fleet-adapters.md).

It has two tabs: **Fleet management** and **Harness config**.

## Fleet management

- **Roster**: the agents registered on this project and what each holds. Offline agents fade
  rather than vanish, because one that died holding a branch is what you need to see. Rows can
  read "not enrolled", "branch orphaned" or "holding nothing".
- **Provision a wave**: mint one seat per agent. A seat grants a role for one session and expires;
  paste it into the agent's prompt, not its MCP config. A wave is one round of work: its name
  becomes the branch prefix. **End wave** revokes every seat and releases every lease it
  issued, is irreversible, and names which wave. A seat is not an API key: the key is minted on
  Settings → API keys. A wave key minted here is swept by End wave; a key you made yourself is never swept.
- **Review queue**: items waiting for a second pair of eyes, with who built each.
- **Ready work**: non-colliding work and why anything held back is waiting.
- Prompt snippets and install steps for an all-in-one agent or planner, and supervisor setup.

## Harness config

- **Tier map**: harness × tier, editable for this deployment. An override is stored beside the
  committed matrix, so clearing one falls back to the packaged model. "Inherit from performance
  grading" fills only cells grading has measured; unmeasured cells are left as they were and say
  so. Save is off until the draft differs from what is stored.
- **Catalog**: the harness × model × tier table is the committed matrix and is read-only here.
- **Mix**: your share of recent spawns. Off means the scorer always picks the winner.
  Unregistered harnesses cannot take a share. Performance and rankings are on [harness.md](harness.md).

## Reading an empty result

"No ready work to partition" means nothing is ready, not that everything is done. "Nothing
waiting for a second pair of eyes" is about the review queue only. If no API key reaches the
project, the page says so and points to Settings.
