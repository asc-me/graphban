# Harness

**Harness** shows how each model has turned out on this project, per function and effort band.
It is history for people and for gbfleet's guidance, not a chooser: nothing here auto-routes.

## Tabs

- **Performance**: each cell is vendor × model × capability × size band. Under the floor of
  finished attempts a rate is grey: not yet a measurement, and gbfleet is told the same. Toggle
  current-only or every version. Verdict quality counts review competence on checked verdicts,
  not builds. "Unattributed" holds finished attempts whose agent declared no vendor or model.
- **Guidance for gbfleet**: the routing guidance served to gbfleet and the grading rules
  (decided on the server; this page does not grade). It can say confidence was not served, or
  that the server sent no verdict rules.
- **Changes & probes**: recommendation cards that suggest promoting, demoting or reweighting.
  Accepting one writes through the same profile or matrix commit a person would make. Also what
  each harness loads (skills and MCP server names posted by `gbfleet surface` or `gban doctor`;
  a harness that was not checked is said so, and a difference list is shown only once two
  lists were complete). No URLs or credentials.
- **Capacity**: Overview, Entitlements and Routing. Entitlements are needs-attention first, with
  budget left; routing decisions show what the router did with the headroom over 7 days; the
  review reserve is kept back so review is not starved by build.

## Preferences

Your profile (excluded harnesses) saves as your default; a project override or policy (per-item,
per-attempt and per-period token caps) can be set or removed.

## Reading an empty result

If the catalog was not served, that is not an empty matrix. A grey cell is unmeasured, not bad.
"Never measured" ranks below available. A burn-down with no reported spend shows "nothing to
forecast", and spawns with no spend report mean the real figure is lower than shown.

## Related

[fleet.md](fleet.md), [prd-38-harness-telemetry.md](prd-38-harness-telemetry.md).
