/** Typed value editors (GRPH-1598): each param is edited as its type and saved as that type. */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import * as React from "react";
import { describe, expect, it, vi } from "vitest";

import { api } from "@/lib/api";
import type { EffectivePredicate, RuleParamValue } from "@/lib/types";

import { RuleSource } from "./RuleSource";
import { RuleValueEditor } from "./RuleValueEditor";

vi.mock("@/lib/api", () => ({ api: { pathCounts: vi.fn(), exclusiveAreasPreview: vi.fn() } }));
vi.mock("@/features/ProjectContext", () => ({
  useProjectCtx: () => ({ activeId: "core", active: { id: "core" }, projects: [], setActiveId: () => {} }),
}));

function Harness({ ruleId, name, initial, spy, effective }: { ruleId: string; name: string; initial: RuleParamValue; spy: (v: RuleParamValue) => void; effective?: EffectivePredicate[] }) {
  const [v, setV] = React.useState<RuleParamValue>(initial);
  return (
    <QueryClientProvider client={new QueryClient()}>
      <RuleValueEditor
        ruleId={ruleId}
        name={name}
        value={v}
        effectivePredicates={effective}
        onChange={(nv) => {
          setV(nv);
          spy(nv);
        }}
        onError={() => {}}
      />
    </QueryClientProvider>
  );
}

describe("RuleValueEditor", () => {
  it("a list gains and loses an entry and stays a list", async () => {
    const user = userEvent.setup();
    const spy = vi.fn();
    render(<Harness ruleId="x.y" name="engines" initial={["sqlite", "postgres"]} spy={spy} />);
    await user.click(screen.getByRole("button", { name: "Remove engines 1" }));
    expect(spy).toHaveBeenLastCalledWith(["postgres"]);
    await user.click(screen.getByRole("button", { name: "Add entry" }));
    await user.type(screen.getByLabelText("engines 2"), "mysql");
    expect(spy).toHaveBeenLastCalledWith(["postgres", "mysql"]);
  });

  it("a path prefix that matches nothing is warned about; a graph that can't count never says 0", async () => {
    vi.mocked(api.pathCounts).mockResolvedValue({ indexed: true, paths: [{ path: "nope/", normalised: "nope", files: 0 }] });
    const { unmount } = render(<Harness ruleId="plan.exclusive_areas" name="paths" initial={["nope/"]} spy={() => {}} />);
    vi.mocked(api.exclusiveAreasPreview).mockResolvedValue({ serialised: [] });
    await waitFor(() => expect(screen.getByTestId("rules-path-count")).toHaveTextContent("matches no files"));
    unmount();
    vi.mocked(api.pathCounts).mockResolvedValue({ indexed: false, paths: [{ path: "a", normalised: "a", files: null }] });
    render(<Harness ruleId="plan.exclusive_areas" name="paths" initial={["a"]} spy={() => {}} />);
    await waitFor(() => expect(screen.getByTestId("rules-path-count")).toHaveTextContent("can't count"));
    expect(screen.getByTestId("rules-path-count")).not.toHaveTextContent(/\b0\b/);
    expect(screen.getByTestId("rules-counts-unavailable")).toBeInTheDocument();
  });

  it("generated files: Add to defaults keeps the built-ins, Replace drops them", async () => {
    const user = userEvent.setup();
    const spy = vi.fn();
    vi.mocked(api.pathCounts).mockResolvedValue({ indexed: false, paths: [] });
    render(<Harness ruleId="build.generated_files" name="paths" initial={["only/this"]} spy={spy} />);
    expect(screen.getByRole("radio", { name: "Replace defaults" })).toBeChecked();
    await user.click(screen.getByRole("radio", { name: "Add to defaults" }));
    const last = spy.mock.calls.at(-1)![0] as string[];
    expect(last).toContain("AGENTS.md");
    expect(last).toContain("only/this");
  });

  it("done predicates: the floor is fixed rows and scans_clean is suggested", async () => {
    const user = userEvent.setup();
    const spy = vi.fn();
    render(<Harness ruleId="merge.done_predicates" name="predicates" initial={["suite_green"]} spy={spy} />);
    expect(screen.getByTestId("rules-predicate-floor")).toHaveTextContent("suite_green");
    expect(screen.queryByRole("textbox")).toBeNull();
    await user.click(screen.getByRole("button", { name: /scans_clean/ }));
    expect(spy).toHaveBeenLastCalledWith(["suite_green", "scans_clean"]);
  });

  it("done predicates: a deployment floor of scans_clean is two fixed rows and adding docs_built saves all three", async () => {
    const user = userEvent.setup();
    const spy = vi.fn();
    const effective: EffectivePredicate[] = [
      { name: "scans_clean", source: "deployment" },
      { name: "suite_green", source: "deployment", when: "item has a linked PR" },
    ];
    render(<Harness ruleId="merge.done_predicates" name="predicates" initial={["suite_green"]} spy={spy} effective={effective} />);
    expect(screen.getAllByTestId("rules-predicate-floor").map((e) => e.textContent)).toEqual(["suite_green · required", "scans_clean · required"]);
    await user.click(screen.getByRole("button", { name: "Add entry" }));
    await user.type(screen.getByLabelText("additions 1"), "docs_built");
    expect(spy).toHaveBeenLastCalledWith(["suite_green", "scans_clean", "docs_built"]);
  });

  it("done predicates: a box with no extra floor shows only suite_green", () => {
    render(<Harness ruleId="merge.done_predicates" name="predicates" initial={["suite_green"]} spy={() => {}} effective={[{ name: "suite_green", source: "deployment", when: "item has a linked PR" }]} />);
    expect(screen.getAllByTestId("rules-predicate-floor")).toHaveLength(1);
  });

  it("a number steps and rejects a non-whole value; a boolean is a switch; an enum marks the risky card", async () => {
    const user = userEvent.setup();
    const spy = vi.fn();
    const { unmount } = render(<Harness ruleId="x.y" name="min_effort" initial={3} spy={spy} />);
    await user.click(screen.getByRole("button", { name: "Increase min_effort" }));
    expect(spy).toHaveBeenLastCalledWith(4);
    await user.clear(screen.getByLabelText("min_effort"));
    await user.type(screen.getByLabelText("min_effort"), "1.5");
    expect(screen.getByRole("alert")).toHaveTextContent("whole number");
    unmount();
    const b = vi.fn();
    const r = render(<Harness ruleId="x.y" name="strict" initial={false} spy={b} />);
    await user.click(screen.getByRole("switch", { name: "strict" }));
    expect(b).toHaveBeenLastCalledWith(true);
    r.unmount();
    render(<Harness ruleId="build.undeclared_files" name="mode" initial="hold" spy={() => {}} />);
    expect(screen.getByText("RISKY")).toBeInTheDocument();
  });

  it("a number shows its unit and range and refuses a value below it; an undeclared number says so", async () => {
    const user = userEvent.setup();
    const spy = vi.fn();
    const { unmount } = render(<Harness ruleId="x.y" name="min_tests_failed" initial={1} spy={spy} />);
    expect(screen.getByText("failing tests")).toBeInTheDocument();
    expect(screen.getByTestId("rules-number-range-min_tests_failed")).toHaveTextContent("Range: 1 or more failing tests.");
    expect(screen.getByRole("button", { name: "Decrease min_tests_failed" })).toBeDisabled();
    await user.clear(screen.getByLabelText("min_tests_failed"));
    await user.type(screen.getByLabelText("min_tests_failed"), "0");
    expect(screen.getByRole("alert")).toHaveTextContent("must be 1 or more failing tests");
    expect(spy).not.toHaveBeenCalledWith(0);
    unmount();
    render(<Harness ruleId="x.y" name="mystery" initial={2} spy={() => {}} />);
    expect(screen.getByTestId("rules-number-range-mystery")).toHaveTextContent("declares no unit or range");
  });

  it("a map is a table of rows", async () => {
    const user = userEvent.setup();
    const spy = vi.fn();
    render(<Harness ruleId="plan.path_classes" name="classes" initial={{ docs: ["*.md"] }} spy={spy} />);
    await user.click(screen.getByRole("button", { name: "Remove classes row 1" }));
    expect(spy).toHaveBeenLastCalledWith({});
  });
});

describe("RuleSource", () => {
  it("names what Reset inherits, and a locked value has no Reset", async () => {
    const user = userEvent.setup();
    const reset = vi.fn();
    const { rerender } = render(<RuleSource source="project" testId="s" inherits="the platform default" onReset={reset} />);
    await user.click(screen.getByRole("button", { name: "Reset to the platform default" }));
    expect(reset).toHaveBeenCalled();
    rerender(<RuleSource source="org" locked testId="s" inherits="the platform default" onReset={reset} />);
    expect(screen.queryByRole("button")).toBeNull();
    expect(screen.getByTestId("s")).toHaveTextContent("LOCKED");
  });
});
