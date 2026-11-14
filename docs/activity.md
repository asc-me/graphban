# Activity

**Activity** is the audit ledger: who did what, when. Every accepted mutation, plus the refusals
the ledger records, is one row attributed to the person, key and agent behind it. Agent (MCP) and user (REST) actions both land here.

## Main actions

- **Filter by facet**: Person (for an agent action that is the API key; the row's `via` line
  names the person), Surface (mcp, rest, public, system) and Object (the target type; most MCP
  writes record none, so `untyped` is a real and large bucket). A facet list is a top-N by count.
- **Read the histogram** and lens tiles for the selection, then open a row (J/K move, Enter
  opens, Esc closes).
- **Event panel**: the actor, target and trace when recorded. It can pivot on target or actor;
  where none was stored it says so. It cannot show which fields changed because no diff is written.
- Refusals link to the rule that refused them.
- A log-export note shows whether telemetry is on and draining; configure it in settings.

You only see projects you can read. Switch project in the nav to narrow it.

## Reading an empty result

With no filter, an empty ledger means nothing has ever been recorded for the projects you can
read. With filters, it is "nothing in this selection", a filter result, not an empty history.

## Related

[mcp.md](mcp.md), [live.md](live.md).
