import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { CloudOrgLinkDialog } from "./CloudOrgLinkDialog";

describe("CloudOrgLinkDialog", () => {
  it("opens at the Plan step by default", () => {
    render(<CloudOrgLinkDialog open onOpenChange={() => {}} />);
    expect(screen.getByText("Choose a plan")).toBeInTheDocument();
    expect(screen.getByText("Monthly")).toBeInTheDocument();
    expect(screen.getByText("Annual")).toBeInTheDocument();
    expect(screen.getByText("Free")).toBeInTheDocument();
    expect(screen.getByText("Pro")).toBeInTheDocument();
    expect(screen.getByText("Team")).toBeInTheDocument();
    expect(screen.getByText("Enterprise")).toBeInTheDocument();
  });

  it("shows the seats-are-people note on the Plan step", () => {
    render(<CloudOrgLinkDialog open onOpenChange={() => {}} />);
    expect(screen.getByText(/Seats are people/)).toBeInTheDocument();
  });

  it("disables the Next button when Enterprise is selected", async () => {
    const user = userEvent.setup();
    render(<CloudOrgLinkDialog open onOpenChange={() => {}} />);
    await user.click(screen.getByText("Enterprise"));
    const btn = screen.getByRole("button", { name: "Contact sales" });
    expect(btn).toBeDisabled();
  });

  it("navigates Plan → Account → Review", async () => {
    const user = userEvent.setup();
    render(<CloudOrgLinkDialog open onOpenChange={() => {}} />);

    await user.click(screen.getByText("Pro"));
    await user.click(screen.getByRole("button", { name: "Next" }));
    expect(screen.getByText("Your organization")).toBeInTheDocument();

    await user.type(screen.getByPlaceholderText("e.g. Acme Corp"), "Acme");
    await user.type(screen.getByPlaceholderText("you@company.com"), "dev@acme.com");
    await user.click(screen.getByRole("button", { name: "Next" }));
    expect(screen.getByText("Review and link")).toBeInTheDocument();
  });

  it("rejects a personal email with inline validation", async () => {
    const user = userEvent.setup();
    render(<CloudOrgLinkDialog open onOpenChange={() => {}} />);

    await user.click(screen.getByRole("button", { name: "Next" }));
    const emailInput = screen.getByPlaceholderText("you@company.com");
    await user.type(emailInput, "user@gmail.com");
    await user.tab();
    expect(screen.getByText(/Use a work email/)).toBeInTheDocument();
  });

  it("accepts a work email without showing validation error", async () => {
    const user = userEvent.setup();
    render(<CloudOrgLinkDialog open onOpenChange={() => {}} />);

    await user.click(screen.getByRole("button", { name: "Next" }));
    const emailInput = screen.getByPlaceholderText("you@company.com");
    await user.type(emailInput, "dev@acme.com");
    await user.tab();
    expect(screen.queryByText(/Use a work email/)).not.toBeInTheDocument();
  });

  it("derives the slug from the org name", async () => {
    const user = userEvent.setup();
    render(<CloudOrgLinkDialog open onOpenChange={() => {}} />);

    await user.click(screen.getByRole("button", { name: "Next" }));
    await user.type(screen.getByPlaceholderText("e.g. Acme Corp"), "Acme Corp");
    expect(screen.getByText(/acme-corp/)).toBeInTheDocument();
  });

  it("shows what-gets-linked disclosure on Review step", async () => {
    const user = userEvent.setup();
    render(<CloudOrgLinkDialog open onOpenChange={() => {}} />);

    await user.click(screen.getByRole("button", { name: "Next" }));
    await user.click(screen.getByRole("button", { name: "Next" }));

    expect(screen.getByText("What gets linked")).toBeInTheDocument();
    expect(screen.getByText(/Source code, raw embeddings/)).toBeInTheDocument();
    expect(screen.getByText(/outbound only/)).toBeInTheDocument();
  });

  it("disables the Create-org button on Review with a tooltip", async () => {
    const user = userEvent.setup();
    render(<CloudOrgLinkDialog open onOpenChange={() => {}} />);

    await user.click(screen.getByRole("button", { name: "Next" }));
    await user.click(screen.getByRole("button", { name: "Next" }));

    const btn = screen.getByRole("button", { name: "Create org" });
    expect(btn).toBeDisabled();
    expect(btn).toHaveAttribute("title", "Cloud org creation is not yet available from self-hosted");
  });

  it("shows 'Already have an account?' link on Review step", async () => {
    const user = userEvent.setup();
    render(<CloudOrgLinkDialog open onOpenChange={() => {}} />);

    await user.click(screen.getByRole("button", { name: "Next" }));
    await user.click(screen.getByRole("button", { name: "Next" }));

    expect(screen.getByText("Already have an account?")).toBeInTheDocument();
  });

  it("starts at Review when startAtLink is true", () => {
    render(<CloudOrgLinkDialog open onOpenChange={() => {}} startAtLink />);
    expect(screen.getByText("Review and link")).toBeInTheDocument();
  });

  it("does not render when open is false", () => {
    render(<CloudOrgLinkDialog open={false} onOpenChange={() => {}} />);
    expect(screen.queryByText("Choose a plan")).not.toBeInTheDocument();
  });

  it("sabotage: three step tabs must exist in the source", () => {
    const sources = import.meta.glob("./CloudOrgLinkDialog.tsx", {
      query: "?raw",
      import: "default",
      eager: true,
    }) as Record<string, string>;
    const src = Object.values(sources)[0] ?? "";
    expect(src).toContain('"plan"');
    expect(src).toContain('"account"');
    expect(src).toContain('"review"');
    expect(src).toContain("Create org");
    expect(src).toContain("Already have an account");
    expect(src).toContain("isWorkEmail");
  });
});
