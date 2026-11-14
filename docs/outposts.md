# Outposts

**Outposts** lists the machines your agents run on, what each has installed, and how to set up another.

## What the page is for

Hosts are grouped from each agent's declared host, or the label after `@`. Each shows build info
as the agent declared it at register/heartbeat: vendor, model, tier, OS if given, worktree and
branch. "Unspecified" is a real group; it is not localhost.

## Main actions

- **Set up a host**: the guide shows three tools for one machine: `gban` (the CLI; log in once,
  issue seats, read why an agent is stuck, register the machine as an outpost), `gbfleet` (the
  supervisor, only for machines that run waves; it cuts a worktree and branch per seat and runs
  claude, cursor-agent, grok or gbagent adapters) and `gbagent` (a local-model agent for
  machines with a GPU or a local model server).
- Install, add the supervisor, and **check the machine** (`gban doctor` runs the local half;
  it confirms git, adapters on PATH and that the server accepts the key).
- Name the machine when you register: Outposts groups by the `register_agent` label.

## Reading an empty result

"No host has checked in" is not a list of zero machines. Nothing has registered yet. A host
appears the first time an agent on it calls `register_agent`.

## Related

[fleet.md](fleet.md), [fleet-remote.md](fleet-remote.md), [stack.md](stack.md).
