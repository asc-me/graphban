import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { Preferences } from "@/features/harness/Preferences";
import type { FleetPolicy } from "@/lib/types";

const policySave = vi.fn();

vi.mock("@/lib/api", () => ({
  setActiveProjectId: vi.fn(),
  api: {
    saveFleetPolicy: (...a: unknown[]) => policySave(...a),
    saveFleetProfile: vi.fn(),
    clearFleetProfile: vi.fn(),
  },
}));

/**
 * GRPH-1716 — `PUT /api/fleet/policy` replaces the whole policy, and the Preferences form only
 * shows part of it. What it does not show is the project's capacity routing rule, so a body
 * built from those boxes alone quietly reset it: a person who saved an unrelated preference
 * lost their ladder. These pin the fields the form has no input for, which is the half a test
 * that only reads the boxes back cannot see.
 */
const STORED: FleetPolicy = {
  local_only: false,
  reviewer_cross_vendor: true,
  allowed_harnesses: ["gbagent"],
  caps: { per_item_tokens: 120000 },
  cluster_ceiling: { frontend: 2, mixed: 3 },
  ladder: ["subscription", "local"],
  routing_mode: "custom_hierarchy",
  subscription_order: ["s2", "s1"],
  tie_break: "cheapest",
  when_exhausted: "hold_until_reset",
};

function renderPreferences(policy: FleetPolicy | null) {
  return render(
    <Preferences projectId="core" scope="core" profile={null} policy={policy} onSaved={vi.fn()} />,
  );
}

const sent = () => policySave.mock.calls[0][0];

beforeEach(() => {
  vi.clearAllMocks();
  policySave.mockResolvedValue({ project_id: "core", policy: STORED });
});

describe("Harness Preferences policy save", () => {
  it("saves an unrelated preference and reads the routing mode, ladder and order back unchanged", async () => {
    const user = userEvent.setup();
    renderPreferences(STORED);

    await user.clear(screen.getByLabelText("Allowed harnesses"));
    await user.type(screen.getByLabelText("Allowed harnesses"), "claude");
    await user.click(screen.getByRole("button", { name: /Save policy/ }));
    await waitFor(() => expect(policySave).toHaveBeenCalledTimes(1));

    // The edit landed...
    expect(sent().allowed_harnesses).toEqual(["claude"]);
    // ...and nothing the form does not show moved.
    expect(sent().routing_mode).toBe("custom_hierarchy");
    expect(sent().ladder).toEqual(["subscription", "local"]);
    expect(sent().subscription_order).toEqual(["s2", "s1"]);
    expect(sent().tie_break).toBe("cheapest");
    expect(sent().when_exhausted).toBe("hold_until_reset");
    expect(sent().cluster_ceiling).toEqual({ frontend: 2, mixed: 3 });
    expect(sent()).toEqual({ ...STORED, project_id: "core", allowed_harnesses: ["claude"] });
  });

  it("clears a cap the boxes no longer show rather than sending the stored one back", async () => {
    // The merge is over the READ policy, so a cap has to be dropped from the copy: keeping it
    // would make emptying the box a no-op, and "saving with everything off stores no policy"
    // would stop being reachable from this form.
    const user = userEvent.setup();
    renderPreferences(STORED);

    await user.clear(screen.getByLabelText("Per-item token cap"));
    await user.click(screen.getByRole("button", { name: /Save policy/ }));
    await waitFor(() => expect(policySave).toHaveBeenCalledTimes(1));

    expect(sent()).not.toHaveProperty("caps");
    expect(sent().routing_mode).toBe("custom_hierarchy");
    expect(sent().ladder).toEqual(["subscription", "local"]);
  });

  it("sends only the form's fields when the project has no policy at all", async () => {
    // Control arm: the merge adds nothing of its own. A body carrying invented defaults would
    // turn "no constraint" into a constraint nobody set.
    const user = userEvent.setup();
    renderPreferences(null);

    await user.click(screen.getByLabelText(/Local only/));
    await user.click(screen.getByRole("button", { name: /Save policy/ }));
    await waitFor(() => expect(policySave).toHaveBeenCalledTimes(1));

    expect(sent()).toEqual({
      project_id: "core", local_only: true, reviewer_cross_vendor: false, allowed_harnesses: [],
    });
  });
});
