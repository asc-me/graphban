import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { DocsReader, openDocsReader } from "@/features/docs/DocsReader";
import { FinishScreen } from "@/features/onboarding/QuickStartDialog";
import { StartupPromptsWidget } from "@/features/onboarding/StartupPrompts";
import type { QsStep, StepMark } from "@/features/onboarding/quickStartProgress";
import { onTourRequested } from "@/features/tour/replay";

/** GRPH-1364 — the invitee's Finish, Help ▸ Take the tour, and the start-up prompts widget. */

const done: StepMark = { status: "done" };
const marks = { where: done, project: done, model: done, connect: done, first_job: done } as Record<QsStep, StepMark>;

function finish(invitee: boolean) {
  const onFinish = vi.fn(async () => undefined);
  const onTakeTour = vi.fn();
  render(
    <QueryClientProvider client={new QueryClient()}>
      <FinishScreen
        steps={["project", "model", "connect", "first_job"]}
        marks={marks}
        route="gban"
        onFinish={onFinish}
        onTakeTour={onTakeTour}
        invitee={invitee}
      />
    </QueryClientProvider>,
  );
  return { onFinish, onTakeTour };
}

afterEach(() => {
  cleanup();
  localStorage.clear();
});

describe("GRPH-1364 invitee Finish", () => {
  it("shows the collapsed link card and both buttons; the command uses graphban link's flags", async () => {
    const { onFinish, onTakeTour } = finish(true);
    const card = document.querySelector("details[data-link-setup]") as HTMLDetailsElement;
    expect(card).not.toBeNull();
    expect(card.open).toBe(false);
    expect(screen.getByText(/^graphban link --cloud-url \S+$/)).toBeInTheDocument();

    const prompts = screen.getByRole("button", { name: "Start-up prompts" });
    expect(screen.queryByRole("list", { name: "Start-up prompts" })).toBeNull();
    fireEvent.click(prompts);
    expect(screen.getByRole("list", { name: "Start-up prompts" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Take the tour" }));
    await waitFor(() => expect(onTakeTour).toHaveBeenCalled());
    expect(onFinish).toHaveBeenCalled();
  });

  it("a non-invitee gets neither the link card nor the prompts button", () => {
    finish(false);
    expect(document.querySelector("[data-link-setup]")).toBeNull();
    expect(screen.queryByRole("button", { name: "Start-up prompts" })).toBeNull();
  });
});

describe("GRPH-1364 Help ▸ Take the tour", () => {
  it("replays the tour and closes Help", async () => {
    const replay = vi.fn();
    const off = onTourRequested(replay);
    render(
      <MemoryRouter initialEntries={["/tracker"]}>
        <DocsReader />
      </MemoryRouter>,
    );
    openDocsReader();
    fireEvent.click(await screen.findByRole("button", { name: "Take the tour" }));
    expect(replay).toHaveBeenCalledTimes(1);
    off();
  });
});

describe("GRPH-1364 start-up prompts widget", () => {
  it("stays until dismissed, and stays dismissed", () => {
    render(<StartupPromptsWidget />);
    expect(screen.getByRole("heading", { name: "Start-up prompts" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Dismiss start-up prompts" }));
    expect(screen.queryByRole("heading", { name: "Start-up prompts" })).toBeNull();
    cleanup();
    render(<StartupPromptsWidget />);
    expect(screen.queryByRole("heading", { name: "Start-up prompts" })).toBeNull();
  });
});
