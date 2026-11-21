import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { HeroDrawing } from "@/features/onboarding/WizardGraphic";

const dash = () => screen.getByTestId("pw-hero").querySelector('path[stroke-dasharray="4 6"]');
const lit = () => screen.getByTestId("pw-hero").querySelector("[data-lit]");
const animated = () =>
  [...screen.getByTestId("pw-hero").querySelectorAll<SVGElement>("*")].filter((el) => !!el.style.animation);

describe("maintenance line graphic (GRPH-1749)", () => {
  it("lights FIXES → TAG-L1 when the line is on", () => {
    render(<HeroDrawing spec={{ kind: "single", from: "FIXES", to: "CORE-L1", lit: true }} still />);
    expect(lit()).not.toBeNull();
    expect(dash()).toBeNull();
    expect(screen.getByText("CORE-L1")).toBeInTheDocument();
    expect(screen.getByText("FIXES")).toBeInTheDocument();
  });

  it("draws the amber dashed stall when fixes stay unscoped", () => {
    render(<HeroDrawing spec={{ kind: "single", from: "FIXES", to: "CORE", broken: true }} still />);
    expect(dash()).not.toBeNull();
    expect(lit()).toBeNull();
    expect(screen.getByText("CORE")).toBeInTheDocument();
  });

  it("drops the stall blink when the caller asks for a still frame", () => {
    render(<HeroDrawing spec={{ kind: "single", from: "FIXES", to: "CORE", broken: true }} still={false} />);
    expect(animated().length).toBeGreaterThan(0);
    render(<HeroDrawing spec={{ kind: "single", from: "FIXES", to: "CORE", broken: true }} still />);
    // The second drawing is the caller's reduced-motion frame: no animation property at all.
    const heroes = screen.getAllByTestId("pw-hero");
    const stillAnimated = [...heroes[1]!.querySelectorAll<SVGElement>("*")].filter((el) => !!el.style.animation);
    expect(stillAnimated).toEqual([]);
  });
});
