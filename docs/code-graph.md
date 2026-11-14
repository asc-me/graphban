# Code graph

The **Code graph** page draws the structure agents have described for this project: modules,
files and symbols, with docs and config muted, and who currently holds which areas.

## What the page is for

Use it to see what depends on a piece of code before changing it, and to spot two agents working
on the same files. The graph is only what agents have described; it is not a scan of your repo.

## Main actions

- **Toggle edge types**: imports, calls, owns, tested-by, references. Collapse components when
  the graph is too wide.
- **Find** a file, symbol or module by name, then select it. Arrow keys move between nodes, Enter
  selects, Shift+Enter widens the lit rings by one, Escape clears.
- **Inspect** a node: its summary, neighbours, and linked work items. Widen the highlight by
  rings, or isolate to hide everything outside them.
- **Trace a path** from the selected node to another. The result can be "not on the map",
  "no path over the shown edge types" (try enabling more types), or "no route between them" (both
  are mapped but sit in different components).
- **Hubs** ranks nodes by what depends on them ("what breaks if this changes") or what they drag with them.
- **Area matrix** lays out areas by dependency instead of as a free graph.
- **Ask the codebase** answers questions only from the described graph.
- The **fleet legend** names who holds what. Overlapping held clouds are two agents on the same
  files. An area the graph cannot place is still listed, not dropped.

## Reading an empty result

"No code described yet" means nothing has been described, not that the project has no structure.
Agents populate the graph by calling `describe_code` as they work. A node marked "not described"
is referenced but has no summary yet. "No edges to rank" in Hubs means no edges are described.

## Related

[links-graph.md](links-graph.md), [stack.md](stack.md), [fleet.md](fleet.md).
