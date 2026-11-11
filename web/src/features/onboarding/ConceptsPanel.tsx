/**
 * Graphban concepts (GRPH-P67 v1.1 §4): six concept panels and Go deeper, inside the
 * quick-start dialog. Copy follows `docs/design/prd-67-concepts-prompts.md` Part A and Part B.
 *
 * Static: nothing here reads or writes server state. Every illustration is sample data and
 * says so with an `EXAMPLE` label; there is no CANDIDATE-style warning pill, only muted ones.
 */
import * as React from "react";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/cn";

export const CONCEPT_COUNT = 6;

type Concept = {
  rail: string;
  title: string;
  lede: string;
  rules: string[];
  art: () => React.ReactNode;
};

const mono = "font-mono text-[10px] uppercase tracking-wide";

function Example({ children }: { children: React.ReactNode }) {
  return (
    <div className="rounded-[10px] border border-line bg-surface-2 p-3" data-testid="concept-art">
      <p className={cn(mono, "mb-2 text-faint")}>EXAMPLE</p>
      {children}
    </div>
  );
}

function Chip({ children, className }: { children: React.ReactNode; className?: string }) {
  return (
    <span className={cn("rounded border border-line px-1.5 py-0.5 font-mono text-[10px] text-muted", className)}>
      {children}
    </span>
  );
}

function Loop() {
  const nodes: [string, string][] = [
    ["YOU", "Spec"],
    ["AGENT", "Build"],
    ["AGENT", "Review"],
    ["GRAPHBAN", "Done"],
  ];
  return (
    <Example>
      <ol className="flex items-center gap-2" aria-label="The loop">
        {nodes.map(([role, label], i) => (
          <li key={label} className="flex flex-1 items-center gap-2">
            <div className="flex-1 rounded-[8px] border border-line-2 p-2 text-center">
              <Chip>{role}</Chip>
              <p className="mt-1 text-[12px]">{label}</p>
              {label === "Review" && <p className="mt-1 font-mono text-[10px] text-faint">a different agent</p>}
            </div>
            {i < nodes.length - 1 && <span aria-hidden className="text-faint">→</span>}
          </li>
        ))}
      </ol>
      <p className="mt-2 text-center font-mono text-[10px] text-faint">Review ↺ bounce → Build</p>
      <div className="mt-2 rounded-[8px] border border-dashed border-line-2 p-2 text-center text-[12px] text-muted">
        <Chip>YOU</Chip> Close as merged
      </div>
    </Example>
  );
}

const STATES: [string, string, string[]][] = [
  ["backlog", "#8b949e", ["CORE-15"]],
  ["next", "#7ca2ff", ["CORE-14 Paginate the export list"]],
  ["in_progress", "#c6f24e", ["CORE-12 Redact tokens in exports"]],
  ["review", "#e0b34a", ["CORE-9 Add export to the CLI"]],
  ["blocked", "#ff6b6b", ["CORE-7 Choose the export file format"]],
  ["done", "#5fd07a", ["CORE-5"]],
];

function Tracker() {
  return (
    <Example>
      <p className="mb-2 font-mono text-[10px] text-muted">
        <span className="text-fg">Board</span> · List
      </p>
      <div className="grid grid-cols-6 gap-1.5">
        {STATES.map(([name, color, items]) => (
          <div key={name} className="min-w-0 rounded-[6px] border border-line p-1.5">
            <p className="flex items-center gap-1 font-mono text-[9px]">
              <span aria-hidden className="inline-block h-2 w-2 rounded-full" style={{ background: color }} />
              {name}
            </p>
            {items.map((it) => (
              <p key={it} className="mt-1 break-words text-[10px] text-muted">
                {it}
              </p>
            ))}
            {name === "in_progress" && (
              <p className="mt-1 break-words font-mono text-[9px] text-faint">
                agent-2 · lease 4m
                <br />
                api/export.py, api/redact.py
              </p>
            )}
            {name === "blocked" && <p className="mt-1 text-[10px] text-st-blocked">Waiting on a decision</p>}
          </div>
        ))}
      </div>
    </Example>
  );
}

function Specs() {
  return (
    <Example>
      <div className="space-y-1.5 text-[12px]">
        <p>
          <span className="font-mono">CORE-P3</span> Export traces <Chip>DRAFT</Chip>
        </p>
        <p className="text-muted">
          ↓ <code className="font-mono">/graphban-grill</code> questions until every section is testable
        </p>
        <p>
          <Chip className="text-st-done">APPROVED</Chip>
        </p>
        <p className="text-muted">↓</p>
        <ul className="grid grid-cols-3 gap-1.5">
          {["CORE-15", "CORE-16", "CORE-17"].map((k, i) => (
            <li key={k} className="rounded-[6px] border border-line p-1.5">
              <span className="font-mono">{k}</span> <Chip>backlog</Chip>
              <p className="mt-1 text-[10px] text-faint">section {i + 1}</p>
            </li>
          ))}
        </ul>
        <p className="rounded-[6px] border border-dashed border-line-2 p-1.5 text-muted">
          <code className="font-mono">request_rebaseline</code> → back to the grill. The current baseline governs until a
          new one is approved.
        </p>
      </div>
    </Example>
  );
}

function Memory() {
  const rows: [string, string, string, boolean][] = [
    ["agent:auto-extract", "Lesson from CORE-12", "CANDIDATE", false],
    ["agent:grill", "Decision from CORE-P3", "CANDIDATE", false],
    ["user:alex", "API errors carry a hint field", "PUBLISHED", true],
  ];
  return (
    <Example>
      <ul className="space-y-1.5">
        {rows.map(([origin, text, status, ok], i) => (
          <li key={text} className="flex flex-wrap items-center gap-2 rounded-[6px] border border-line p-1.5 text-[12px]">
            <Chip>{origin}</Chip>
            <span className="flex-1">{text}</span>
            {i === 0 && <Chip className="text-faint">judge: not judged</Chip>}
            <span
              className={cn("rounded border px-1.5 font-mono text-[10px]", ok ? "border-st-done text-st-done" : "border-line text-muted")}
            >
              {status}
            </span>
          </li>
        ))}
      </ul>
      <p className="mt-2 text-center font-mono text-[10px] text-muted">CANDIDATE → YOU · publish / reject → PUBLISHED</p>
    </Example>
  );
}

function CodeGraph() {
  const scans: [string, string, string][] = [
    ["web/", "CLEAN AT 3f9c2a1", "text-st-done"],
    ["api/", "HAS FINDINGS", "text-st-review"],
    ["fleet/", "STALE", "text-muted border-dashed"],
    ["cli/", "NEVER SCANNED", "text-faint border-dashed"],
  ];
  return (
    <Example>
      <div className="grid grid-cols-2 gap-3">
        <div className="space-y-1 font-mono text-[10px] text-muted">
          <p>api/ (module)</p>
          <p className="pl-3">→ api/export.py (file)</p>
          <p className="pl-6">imports · calls · tested_by</p>
          <p className="pl-6">export_traces() · redact()</p>
          <p className="pl-3 rounded border border-dashed border-line-2 text-faint">api/legacy.py · stale</p>
        </div>
        <ul className="space-y-1">
          {scans.map(([path, state, cls]) => (
            <li key={path} className={cn("flex justify-between rounded border border-line px-1.5 py-1 font-mono text-[10px]", cls)}>
              <span>{path}</span>
              <span>{state}</span>
            </li>
          ))}
        </ul>
      </div>
    </Example>
  );
}

export const RULE_LEVELS: [string, string, string, string][] = [
  ["ENFORCED", "review.not_own_work", "Not your own work", "the server refuses"],
  ["CHECKED", "merge.ci_green", "Merged work passed CI", "CI reports, nothing refuses"],
  ["EXTERNAL", "plan.budget_cap", "Budget cap", "held outside the server, Graphban can't verify this"],
  ["ADVISORY", "gitops.base_branch", "Base branch", "told to agents in every get_context, nothing refuses"],
  ["UNMEASURED", "gitops.pr_title_pattern", "", "no value set"],
];
const LEVEL_ICON: Record<string, string> = { ENFORCED: "■", CHECKED: "◆", EXTERNAL: "◇", ADVISORY: "○", UNMEASURED: "?" };

function Rules() {
  return (
    <Example>
      <p className="mb-2 font-mono text-[10px] text-muted">
        Spec → Plan → Build → Review → Merge → Release → Learn
      </p>
      <ul className="space-y-1">
        {RULE_LEVELS.map(([level, id, title, note]) => (
          <li key={level} className="flex items-baseline gap-2 rounded border border-line px-1.5 py-1 text-[11px]">
            <span aria-hidden className="font-mono">{LEVEL_ICON[level]}</span>
            <span className="w-[84px] flex-none font-mono text-[10px]">{level}</span>
            <code className="font-mono text-[10px] text-muted">{id}</code>
            <span className="text-muted">{title ? `${title}: ${note}` : note}</span>
          </li>
        ))}
      </ul>
    </Example>
  );
}

export const CONCEPTS: Concept[] = [
  {
    rail: "01 The loop",
    title: "The loop.",
    lede: "You say what done means. Agents build and review it, and nobody signs off their own work.",
    rules: [
      "The builder moves an item to `review`. It never sets `done`.",
      "Signing off needs evidence and the commit that was reviewed. A bounce says what failed.",
      "Working alone? Review waits for you, or for a second agent.",
    ],
    art: Loop,
  },
  {
    rail: "02 The tracker",
    title: "The tracker.",
    lede: "Every piece of work is an item in one of six states, shown as a board or a list. Agents claim the next ready item, and a claim holds its files.",
    rules: [
      "Board is the default layout and List is the other. A cluster claim reserves the files it touches, so two agents never edit the same ones.",
      "Agents heartbeat while they work. A lapsed lease frees the item for the next agent.",
      "Stopping early releases the item. A blocker that's still true tomorrow is `blocked`.",
    ],
    art: Tracker,
  },
  {
    rail: "03 Specs",
    title: "Specs.",
    lede: "Work that needs a decision starts as a spec. The grill asks questions until it's testable, and only then does it split into items.",
    rules: [
      "Approval is the grill finishing. Nobody sets it by hand.",
      "Each item carries its section's framing, so the agent doesn't re-read the whole spec.",
      "A change of scope reopens the grill. Closing a spec is final, and later work is a new spec.",
    ],
    art: Specs,
  },
  {
    rail: "04 Memory",
    title: "Memory.",
    lede: "Agents write down what the next agent should know. In the default review mode, nothing they write is trusted until you publish it.",
    rules: [
      "Candidates stay out of agent search until they're published.",
      "A judge scores candidates. Projects set to `auto` or `trusted` publish some without you.",
      "A lesson pulled from a closed item always waits for you, in every mode.",
    ],
    art: Memory,
  },
  {
    rail: "05 Code graph",
    title: "Code graph.",
    lede: "Your agent describes the code it works in: modules, files and symbols, joined by imports, calls and tests. Agents read the graph before they read the tree.",
    rules: [
      "The graph is built on the machine that has the checkout. A node your agent didn't re-describe is marked stale, not deleted.",
      "Each scanner reports for a path at a commit. There's no overall score.",
      "A missing scan isn't a clean one, and a stale result isn't either.",
    ],
    art: CodeGraph,
  },
  {
    rail: "06 Rules",
    title: "Rules.",
    lede: "Your delivery process is a set of rules, each on the stage it governs. Every rule says what actually holds it, so a written rule never passes for an enforced one.",
    rules: [
      "A stage reads as strong as its weakest rule. Advisory is never drawn as enforced.",
      "A person turns a rule on or off, with a reason. A recommendation never does.",
      "A refusal names the rule that refused it, or says it isn't mapped yet.",
    ],
    art: Rules,
  },
];

/** Index 0..5 are concepts; 6 is Go deeper. */
export const GO_DEEPER = CONCEPT_COUNT;

export function conceptCaption(index: number): string {
  return index >= GO_DEEPER ? "CONCEPTS · GO DEEPER" : `CONCEPTS · ${index + 1}/6 · ${CONCEPTS[index].title.replace(/\.$/, "").toUpperCase()}`;
}

export function ConceptsPanel({
  index,
  onIndex,
  onExit,
  onFinish,
  onFinishLater,
}: {
  index: number;
  onIndex: (i: number) => void;
  /** Back on concept 01: return to Sections. */
  onExit: () => void;
  /** Finish concepts: return to Sections. */
  onFinish: () => void;
  onFinishLater: () => void;
}) {
  const deeper = index >= GO_DEEPER;
  const c = deeper ? null : CONCEPTS[index];
  const labels = [...CONCEPTS.map((x) => x.rail), "Go deeper"];
  return (
    <section aria-labelledby="qs-concept-h" data-concept={index}>
      <ol aria-label="Concepts" className="mb-3 flex flex-wrap gap-x-3 gap-y-1">
        {labels.map((l, i) => (
          <li
            key={l}
            aria-current={i === index ? "step" : undefined}
            className={cn("font-mono text-[10px] uppercase tracking-wide", i === index ? "text-accent" : "text-faint")}
          >
            {l}
          </li>
        ))}
      </ol>
      <p className={cn(mono, "text-faint")}>{deeper ? "GO DEEPER" : `CONCEPT ${index + 1} OF ${CONCEPT_COUNT}`}</p>
      <h2 id="qs-concept-h" className="mt-1 text-[16px] font-semibold">
        {c ? c.title : "Go deeper."}
      </h2>
      <p className="mt-2 text-[13px] text-muted">
        {c ? c.lede : "That's the short version. The concepts page covers why each idea exists and how they fit together."}
      </p>
      <div className="mt-3">
        {c ? (
          c.art()
        ) : (
          <a
            href="https://graphban.dev/concepts"
            target="_blank"
            rel="noreferrer"
            className="block rounded-[10px] border border-line-2 bg-surface-2 p-4 hover:border-accent"
          >
            <p className="font-mono text-[13px] text-fg">graphban.dev/concepts</p>
            <p className="mt-1 text-[12px] text-muted">
              The core ideas behind agent-first delivery, with the reasoning and worked examples.
            </p>
            <p className="mt-2 flex flex-wrap gap-1.5">
              {["loop", "tracker", "specs", "memory", "code graph", "rules"].map((n) => (
                <Chip key={n} className="text-faint">
                  {n}
                </Chip>
              ))}
            </p>
          </a>
        )}
      </div>
      {deeper && <p className="mt-2 text-[12px] text-muted">Opens in a new tab. Your setup stays where you left it.</p>}
      {c && (
        <ul className="mt-3 space-y-1" aria-label="Rules of thumb">
          {c.rules.map((r) => (
            <li key={r} className="text-[12px] text-muted">
              — {r.replaceAll("`", "")}
            </li>
          ))}
        </ul>
      )}
      <div className="mt-5 flex items-center gap-2 border-t border-line pt-4">
        <Button variant="ghost" size="sm" type="button" onClick={onFinishLater}>
          Finish later
        </Button>
        <div className="flex-1" />
        <Button variant="outline" size="sm" type="button" onClick={() => (index === 0 ? onExit() : onIndex(index - 1))}>
          {index === 0 ? "← Sections" : "← Back"}
        </Button>
        <Button size="sm" type="button" onClick={() => (deeper ? onFinish() : onIndex(index + 1))}>
          {deeper ? "Finish concepts" : "Next →"}
        </Button>
      </div>
    </section>
  );
}
