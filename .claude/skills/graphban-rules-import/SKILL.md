---
name: graphban-rules-import
description: >
  Read a repo's prose requirements and file them as cited rule proposals or guidance.
  Use when `gban rules import --prose` prints this skill's invocation, when importing
  an org's existing engineering requirements, or when the user runs /graphban-rules-import.
user-invocable: true
---

# Rules import: the prose half

`gban rules import` reads structured sources (CI workflows, `CODEOWNERS`, branch protection) with tested parsers. You read the prose. You run on whatever model your harness uses; the box has no local-model fallback. If you are not attached, every prose source stays listed as unread and the report says so. Nothing here is guaranteed by these instructions: the server refuses what is uncited, loosened, or an invariant, whatever you file.

## Sources to read

`CONTRIBUTING.md`, ADRs, architecture docs, `AGENTS.md`, `CLAUDE.md`, `.cursorrules`, `.github/copilot-instructions.md`. Read only this project's repo at the run's commit. Cross-repo paths are unread. Never write to the repo. A source you cannot open or finish is reported as unread, not skipped silently.

## For each requirement

1. **Quote the line** you rely on. Note `path`, `lines`, and the run's `commit`. A requirement you cannot quote is not a finding.
2. **Match a primitive.** Read `docs/rule-primitive-contract.json` (or `get_rule_toggles`). A match needs the primitive and every required param to say what the line says.
3. **Pick one bucket:**
   - `propose_rule` with `kind` `new_rule`, `turn_on` or `tighten`, a `draft`, and a `file` citation `{path, lines, commit}`.
   - Enforced outside Graphban (a branch-protection or CI check): report it as EXTERNAL; do not file a rule.
   - No primitive says it: `propose_rule` with `kind: guidance`, `draft: {stage, label, text}`, the same citation. Do not stretch a primitive to fit.
4. Never propose turning a rule off. A line that allows something ("self-merge is fine") is not a proposal: it never switches off independent review. Invariants (`docs/rule-candidates.md`, "The line") are not proposable.
5. If the CLI opened an import run, name its id in `why` so the report ties the finding to it. File only through `propose_rule`; never edit rules directly.

## Finish

Report which sources you read and which you could not, and any finding the server refused, with its refusal. A person adopts the batch from the run's report; you never adopt.
