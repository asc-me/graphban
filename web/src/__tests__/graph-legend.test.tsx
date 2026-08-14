import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { FleetLegend } from "@/features/code/FleetLegend";
import { nodesHeldBy } from "@/lib/graph/presence";
import type { FleetPresence, HeldArea } from "@/lib/types";

function held(over: Partial<HeldArea> = {}): HeldArea {
  return {
    area: "a.py",
    item_id: "GRPH-1",
    expires_at: "2026-08-14T12:10:00.000Z",
    agent_id: "GRPH-A1",
    agent_label: "claude @ mbp",
    active_role: "worker",
    state: "working",
    user_id: "u1",
    user_initials: "AC",
    user_color: "#a78bfa",
    node_paths: ["a.py"],
    predicted: false,
    ...over,
  };
}

const presence = (rows: HeldArea[], off: HeldArea[] = []): FleetPresence => ({
  served_at: "2026-08-14T12:00:00.000Z",
  heartbeat_interval_seconds: 50,
  held: rows,
  off_map: off,
  truncated: false,
  total: rows.length + off.length,
});

describe("nodesHeldBy", () => {
  it("returns every node that human holds, across their agents", () => {
    const p = presence([
      held({ agent_id: "GRPH-A1", node_paths: ["a.py"] }),
      held({ agent_id: "GRPH-A2", node_paths: ["b.py", "c.py"] }),
    ]);
    expect([...nodesHeldBy(p, "u1")].sort()).toEqual(["a.py", "b.py", "c.py"]);
  });

  it("excludes other humans' nodes", () => {
    const p = presence([held({ user_id: "u1" }), held({ user_id: "u2", node_paths: ["z.py"] })]);
    expect([...nodesHeldBy(p, "u1")]).toEqual(["a.py"]);
  });

  it("is EMPTY for a user holding nothing, never a fallback to everything", () => {
    // A solo that outlives its holder must light nothing. Falling back to the full set would
    // read as "your teammate is everywhere" at exactly the moment they left.
    expect(nodesHeldBy(presence([held()]), "ghost").size).toBe(0);
    expect(nodesHeldBy(presence([held()]), null).size).toBe(0);
    expect(nodesHeldBy(undefined, "u1").size).toBe(0);
  });
});

describe("FleetLegend", () => {
  it("renders nothing when the fleet is idle and nothing is unplaceable", () => {
    // An empty chrome strip reads as a broken feature rather than a quiet codebase.
    const { container } = render(
      <FleetLegend presence={presence([])} soloUser={null} onSolo={() => {}} />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("shows one chip per HUMAN, not per agent", async () => {
    render(
      <FleetLegend
        presence={presence([
          held({ agent_id: "GRPH-A1", user_id: "u1", node_paths: ["a.py"] }),
          held({ agent_id: "GRPH-A2", user_id: "u1", node_paths: ["b.py"] }),
        ])}
        soloUser={null}
        onSolo={() => {}}
      />,
    );
    // Someone running two windows is still one teammate.
    expect(await screen.findByTitle("2 agents, 2 nodes held")).toBeInTheDocument();
    expect(screen.getAllByText("2a · 2n")).toHaveLength(1);
  });

  it("solos a user on click and clears the solo when the same chip is clicked again", async () => {
    const onSolo = vi.fn();
    const { rerender } = render(
      <FleetLegend presence={presence([held()])} soloUser={null} onSolo={onSolo} />,
    );
    await userEvent.click(screen.getByRole("button", { pressed: false }));
    expect(onSolo).toHaveBeenCalledWith("u1");

    rerender(<FleetLegend presence={presence([held()])} soloUser="u1" onSolo={onSolo} />);
    await userEvent.click(screen.getByRole("button", { pressed: true }));
    expect(onSolo).toHaveBeenLastCalledWith(null);
  });

  it("surfaces the off-map count next to the fleet it describes", async () => {
    render(
      <FleetLegend
        presence={presence([held()], [held({ area: "vercel env", reason: "undescribed" })])}
        soloUser={null}
        onSolo={() => {}}
      />,
    );
    // The difference between "nobody is working here" and "we could not place what they are
    // working on" — only one of those is good news, so the count is never silent.
    expect(await screen.findByText("1 held area not on this map")).toBeInTheDocument();
  });

  it("opens a tray showing the RAW area text and why it could not be placed", async () => {
    render(
      <FleetLegend
        presence={presence([], [held({ area: "vercel env", reason: "undescribed" })])}
        soloUser={null}
        onSolo={() => {}}
      />,
    );
    await userEvent.click(screen.getByText("1 held area not on this map"));
    // Verbatim: `vercel env` and `AGENTS.md` are different kinds of thing and the server
    // cannot tell them apart from the string, so a human is shown exactly what it saw.
    expect(await screen.findByText("vercel env")).toBeInTheDocument();
    expect(screen.getByText("undescribed")).toBeInTheDocument();
  });

  it("still renders the off-map tray when nobody is holding anything placeable", () => {
    // The all-off-map case is the one where a silent legend would be most misleading.
    render(
      <FleetLegend
        presence={presence([], [held({ area: "AGENTS.md", reason: "undescribed" })])}
        soloUser={null}
        onSolo={() => {}}
      />,
    );
    expect(screen.getByText("1 held area not on this map")).toBeInTheDocument();
  });
});
