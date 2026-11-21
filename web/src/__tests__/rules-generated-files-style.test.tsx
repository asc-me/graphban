// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";

import { GENERATED_FILES_RULE, RuleValueEditor } from "@/features/rules/RuleValueEditor";

vi.mock("@/features/ProjectContext", () => ({
  useProjectCtx: () => ({ activeId: "core", active: { id: "core", tag: "CORE", name: "Core" }, projects: [], setActiveId: () => {} }),
}));

function Harness({ onValue }: { onValue: (v: string[]) => void }) {
  const [value, setValue] = useState<string[]>(["a.lock"]);
  return (
    <RuleValueEditor
      ruleId={GENERATED_FILES_RULE}
      name="paths"
      value={value}
      onChange={(v) => {
        setValue(v as string[]);
        onValue(v as string[]);
      }}
      onError={() => {}}
    />
  );
}

const card = (name: string) => screen.getByRole("radio", { name }).closest("label") as HTMLElement;

describe("generated files mode cards", () => {
  it("draws option cards whose selection follows the mode", async () => {
    const onValue = vi.fn();
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <Harness onValue={onValue} />
      </QueryClientProvider>,
    );
    expect(screen.getByRole("radiogroup", { name: "Generated files" })).toBeTruthy();
    // ["a.lock"] lacks the defaults, so it starts in replace mode.
    expect(card("Replace defaults").className).toContain("border-accent");
    expect(card("Add to defaults").className).not.toContain("border-accent");
    expect(screen.getByRole("radio", { name: "Add to defaults" }).className).toContain("accent-accent");
    await userEvent.click(screen.getByRole("radio", { name: "Add to defaults" }));
    expect(card("Add to defaults").className).toContain("border-accent");
    expect(card("Replace defaults").className).not.toContain("border-accent");
    const added = onValue.mock.calls.at(-1)![0] as string[];
    expect(added).toContain("a.lock");
    expect(added.length).toBeGreaterThan(1);
    await userEvent.click(screen.getByRole("radio", { name: "Replace defaults" }));
    expect(onValue.mock.calls.at(-1)![0]).toEqual(["a.lock"]);
  });
});
