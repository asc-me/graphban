---
name: graphban-rules-import
description: >
  Read a repo's prose requirements and file them as cited rule proposals or guidance.
  Use when `gban rules import --prose` prints this skill's invocation, when importing
  an org's existing engineering requirements, or when the user runs /graphban-rules-import.
user-invocable: true
---

# Rules import: the prose half

A person runs `gban rules import --prose`. It opens an import run at the repo's commit, lists every prose source as unread, and prints `/graphban-rules-import import_run=<id> commit=<sha>`. You read the prose. You run on whatever model your harness uses; the box has no local-model fallback. If no agent attaches, every prose source stays listed as unread and the run's report is not clean.

These instructions guarantee nothing. The server refuses what is uncited, cited at another commit, loosening, or an invariant, whatever you file.

## Sources to read

The run's `sources_unread` whose `reader` is `agent`: `CONTRIBUTING.md`, ADRs, `docs/architecture*`, `AGENTS.md`, `CLAUDE.md`, `.cursorrules`, `.github/copilot-instructions.md`. Read them at the run's commit, in this project's repo only. Cross-repo paths stay unread. Never write to the repo, and never edit those files.

## For each requirement

1. **Quote the line** you rely on. A requirement you cannot quote is not a finding.
2. **Match a primitive.** Read the primitive contract (`docs/rule-primitive-contract.json`, or `get_rule_toggles` for what is on). A match needs the primitive and every required param to say what the line says.
3. **File it through `propose_rule`** with `import_run: <id>`, `quote: "<the line>"`, and a `file` citation `{path, lines, commit: <the run's commit>}`. Pick one `kind`:
   - `new_rule`, `turn_on` or `tighten`, with a `draft`, when a primitive says it.
   - Never `external`. Only the import's parsers record an EXTERNAL rule, from the forge and CI configuration itself. A prose line saying something is enforced elsewhere is filed as guidance, and the server does that for you if you send `external`.
   - `kind: guidance`, with `draft: {stage, label, text}`, when no primitive says it. Do not stretch a primitive to fit.
4. Never propose turning a rule off. A line that allows something ("self-merge is fine") is not a proposal: it never switches off independent review. Invariants (`docs/rule-candidates.md`, "The line") are not proposable.

## Finish

1. Call `propose_rule` with `import_run: <id>` and `sources_read: [paths]` naming each source you read to the end. Leave out any source you could not open or finish. It stays unread, and that is the honest report.
2. Report which sources you read and which you could not, and every finding the server refused, with its refusal. The refusal is also kept on the run.

File only through `propose_rule`, and never edit rules directly. A person adopts the batch from the run's report. You never adopt.
