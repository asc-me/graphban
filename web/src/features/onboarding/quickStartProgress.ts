/**
 * Quick-start step marks (GRPH-P67 §2, §4). Pure, so the one rule that matters is testable
 * on its own: **a step's ✓ comes from server state, never from the dialog.** Nothing in here
 * reads which steps the person clicked through.
 *
 * Three states, never two. A check that could not run is `unknown` with the reason, and an
 * unknown step is never drawn ✓ — the quiet reading of a failed read is the reassuring one,
 * which is exactly the reading this module refuses.
 */
import type { Credential, ProjectSetup, SetupStatus, SetupStep } from "@/lib/types";

/** Step ③ (Connect your agent) slots in between `model` and `first_job` with GRPH-1362. */
export type QsStep = "where" | "project" | "model" | "first_job";

export const STEP_META: Record<QsStep, { num: string; label: string }> = {
  where: { num: "⓪", label: "Where" },
  project: { num: "①", label: "Project" },
  model: { num: "②", label: "Model" },
  first_job: { num: "④", label: "First job" },
};

/** Hosted skips ⓪: the answer is always "someone else's server". */
export function stepsFor(hosted: boolean): QsStep[] {
  return hosted ? ["project", "model", "first_job"] : ["where", "project", "model", "first_job"];
}

/** The steps the top-bar chip counts: the numbered ones, ⓪ excluded (as drawn). */
export function countedSteps(steps: QsStep[]): QsStep[] {
  return steps.filter((s) => s !== "where");
}

export interface StepMark {
  status: SetupStatus;
  /** Why a step is `unknown`. */
  reason?: string;
}

export interface MarkInputs {
  /** No project yet: nothing server-side exists to be done. */
  projectId: string;
  setup?: ProjectSetup;
  setupError?: unknown;
  credentials?: Credential[];
  credentialsError?: unknown;
}

function errText(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

function fromServer(step: SetupStep | undefined, missing: string): StepMark {
  if (!step) return { status: "unknown", reason: missing };
  if (step.status === "unknown") return { status: "unknown", reason: step.reason || step.detail };
  // Anything that is not literally `done` is not done — an unexpected status is not a ✓.
  return { status: step.status === "done" ? "done" : "pending" };
}

/** A credential that actually serves chat for this project and answered its probe. */
export function servingChat(credentials: Credential[], projectId: string): Credential | undefined {
  return credentials.find(
    (c) =>
      c.state === "valid" &&
      c.serves.split(",").includes("chat") &&
      (c.used_by.includes(projectId) || c.is_default),
  );
}

export function deriveMarks(i: MarkInputs): Record<QsStep, StepMark> {
  if (!i.projectId) {
    return {
      where: { status: "pending" },
      project: { status: "pending" },
      model: { status: "pending" },
      first_job: { status: "pending" },
    };
  }
  if (i.setupError || !i.setup) {
    const reason = i.setupError
      ? `Could not read setup: ${errText(i.setupError)}`
      : "Setup has not been read yet.";
    const unknown: StepMark = { status: "unknown", reason };
    return { where: unknown, project: unknown, model: unknown, first_job: unknown };
  }
  const by = new Map(i.setup.steps.map((s) => [s.step, s]));
  const missing = (n: number) => `The server did not report step ${n}.`;

  let model: StepMark;
  const unproven = i.credentials?.find(
    (c) =>
      c.state !== "valid" &&
      c.serves.split(",").includes("chat") &&
      c.used_by.includes(i.projectId),
  );
  if (i.setup.offline) model = { status: "done" };
  else if (i.credentials && servingChat(i.credentials, i.projectId)) model = { status: "done" };
  else if (unproven)
    // Saved but never answered: a key nobody could test is not a working model.
    model = {
      status: "unknown",
      reason: `${unproven.label || unproven.kind} is saved but unverified (${unproven.state})${
        unproven.last_error ? `: ${unproven.last_error}` : ""
      }.`,
    };
  else if (i.credentialsError)
    model = { status: "unknown", reason: `Could not read AI providers: ${errText(i.credentialsError)}` };
  else model = { status: "pending" };

  // ④ is the first job's three counters: code graph, memories, items (server steps 2–4).
  const job = [2, 3, 4].map((n) => fromServer(by.get(n), missing(n)));
  const unknownJob = job.find((m) => m.status === "unknown");
  const first_job: StepMark = unknownJob
    ? unknownJob
    : job.every((m) => m.status === "done")
      ? { status: "done" }
      : { status: "pending" };

  return {
    where: fromServer(by.get(0), missing(0)),
    project: fromServer(by.get(1), missing(1)),
    model,
    first_job,
  };
}

/** Where the chip reopens: the first step not ✓, else the last. */
export function firstUnfinished(steps: QsStep[], marks: Record<QsStep, StepMark>): QsStep {
  return steps.find((s) => marks[s].status !== "done") ?? steps[steps.length - 1];
}

/** `Finish setup n/N` — n counts only server-proven ✓. */
export function chipCount(steps: QsStep[], marks: Record<QsStep, StepMark>): { done: number; total: number } {
  const counted = countedSteps(steps);
  return { done: counted.filter((s) => marks[s].status === "done").length, total: counted.length };
}

/** The leading count in a server count step's detail ("12 code node(s) described."). */
export function countFrom(step: SetupStep | undefined): number | null {
  if (!step || step.status === "unknown") return null;
  const m = /^(\d+)\b/.exec(step.detail);
  return m ? Number(m[1]) : null;
}

/** Step ⓪'s inference: the browser's origin. Loopback-arrival from the API joins with GRPH-1365. */
export function inferWhere(origin: string): {
  where: "this_computer" | "dedicated_server";
  evidence: string;
  address: string;
} {
  let host = "";
  try {
    host = new URL(origin).hostname;
  } catch {
    host = "";
  }
  const loopback = host === "localhost" || host === "127.0.0.1" || host === "[::1]" || host === "::1";
  if (loopback) return { where: "this_computer", evidence: "you opened Graphban on localhost", address: "" };
  const shown = origin.replace(/^https?:\/\//, "");
  return { where: "dedicated_server", evidence: `you opened Graphban at ${shown}`, address: origin };
}

/** The stored ⓪ answer, read back from the server's own detail line. */
export function storedWhere(step: SetupStep | undefined): "this_computer" | "dedicated_server" | null {
  if (!step || step.status !== "done") return null;
  if (step.detail.startsWith("dedicated_server")) return "dedicated_server";
  if (step.detail.startsWith("this_computer")) return "this_computer";
  return null;
}

export function normalizeAddress(a: string): string {
  return a.trim().replace(/\/+$/, "");
}
