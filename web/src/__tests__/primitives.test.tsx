// @ts-nocheck — reads Node fs at runtime; vitest runs in Node.
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import buttonSrc from "../components/ui/button.tsx?raw";
import dialogSrc from "../components/ui/dialog.tsx?raw";
import dropdownSrc from "../components/ui/dropdown-menu.tsx?raw";
import inputSrc from "../components/ui/input.tsx?raw";
import {
  Dialog,
  DialogContent,
  DialogTitle,
} from "../components/ui/dialog";
import { Button } from "../components/ui/button";

/**
 * GRPH-910 primitive component contract — source markers plus one render check for dialog a11y.
 * Sabotage recipes are documented inline; deleting the guarded strings fails the suite.
 */

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const cssSrc = readFileSync(resolve(root, "index.css"), "utf8");

describe("primitive components (GRPH-910)", () => {
  it("Button carries gb-pressable for pointer-only press scale", () => {
    render(<Button type="button">Save</Button>);
    expect(screen.getByRole("button", { name: "Save" }).className).toMatch(/gb-pressable/);
    expect(buttonSrc).toMatch(/gb-pressable/);
  });

  it("Input exposes a focus-visible ring, not a border swap alone", () => {
    expect(inputSrc).toMatch(/focus-visible:outline-2/);
    expect(inputSrc).toMatch(/focus-visible:outline-offset-2/);
    expect(inputSrc).toMatch(/focus-visible:outline-focus/);
  });

  it("Dialog close control has an accessible name", () => {
    render(
      <Dialog open>
        <DialogContent>
          <DialogTitle>Example</DialogTitle>
        </DialogContent>
      </Dialog>,
    );
    expect(screen.getByRole("button", { name: "Close" })).toBeInTheDocument();
  });

  it("Dropdown menu item styles data-[highlighted] with fill and stronger text", () => {
    expect(dropdownSrc).toMatch(/data-\[highlighted\]:bg-surface-4/);
    expect(dropdownSrc).toMatch(/data-\[highlighted\]:text-fg/);
  });

  it("sabotage: removing DialogClose aria-label would fail the accessible-name test above", () => {
    expect(dialogSrc).toMatch(/aria-label="Close"/);
  });

  it("sabotage: removing gb-pressable or the :active scale rule fails press contract", () => {
    expect(buttonSrc).toMatch(/gb-pressable/);
    expect(cssSrc).toMatch(/\.gb-pressable:active:not\(:disabled\)[\s\S]*?transform:\s*scale\(0\.97\)/);
  });
});
